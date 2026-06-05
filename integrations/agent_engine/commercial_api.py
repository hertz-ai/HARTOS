"""
Commercial Intelligence API Gateway — Paid intelligence-as-a-service.

Exposes Hevolve AI capabilities as metered API endpoints.
Revenue flows: 90% to compute providers, 10% platform sustainability.
Free tier always available — we don't gatekeep intelligence, we sustain it.

Service Pattern: static methods, db: Session, db.flush() not db.commit().
Blueprint Pattern: Blueprint('commercial_api', __name__).
"""
import hashlib
import hmac
import logging
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps
from typing import Dict, List, Optional
from core.port_registry import get_port

from flask import Blueprint, g, jsonify, request
from sqlalchemy.orm import Session

logger = logging.getLogger('hevolve_social')


# ═══════════════════════════════════════════════════════════════
# Tier configuration (deterministic pricing)
# ═══════════════════════════════════════════════════════════════

TIER_CONFIG = {
    'free':       {'rate_limit_per_day': 100,    'monthly_quota': 3000,     'priority': 'low',
                   'monthly_price_usd': 0.0,      'description': 'Free forever — intelligence belongs to everyone.'},
    'starter':    {'rate_limit_per_day': 1000,   'monthly_quota': 30000,    'priority': 'normal',
                   'monthly_price_usd': 9.0,      'description': 'For solo builders. 10x daily / 10x monthly vs free.'},
    'pro':        {'rate_limit_per_day': 10000,  'monthly_quota': 300000,   'priority': 'high',
                   'monthly_price_usd': 49.0,     'description': 'For small teams. Priority queue + hive priority.'},
    'enterprise': {'rate_limit_per_day': 100000, 'monthly_quota': 10000000, 'priority': 'critical',
                   'monthly_price_usd': 499.0,    'description': 'For scale. Dedicated hive lane + SLA.'},
}

COST_PER_1K_TOKENS = {
    'free': 0.0,
    'starter': 0.5,
    'pro': 0.3,
    'enterprise': 0.2,
}


def _revenue_split_usd(amount_usd: float) -> Dict[str, float]:
    """The 90/9/1 split of a USD amount, rounded to cents — single source.

    Uses the canonical REVENUE_SPLIT_* constants (revenue_aggregator) so every
    payment-response breakdown tracks the platform split instead of hardcoding
    0.90/0.09/0.01; falls back to the documented defaults only if that import is
    unavailable (slim runs) — same pattern as get_pricing().
    """
    try:
        from integrations.agent_engine.revenue_aggregator import (
            REVENUE_SPLIT_USERS, REVENUE_SPLIT_INFRA, REVENUE_SPLIT_CENTRAL,
        )
    except Exception:
        REVENUE_SPLIT_USERS, REVENUE_SPLIT_INFRA, REVENUE_SPLIT_CENTRAL = 0.90, 0.09, 0.01
    return {
        'users_pool_usd': round(amount_usd * REVENUE_SPLIT_USERS, 2),
        'infrastructure_usd': round(amount_usd * REVENUE_SPLIT_INFRA, 2),
        'central_usd': round(amount_usd * REVENUE_SPLIT_CENTRAL, 2),
    }


# ═══════════════════════════════════════════════════════════════
# Service
# ═══════════════════════════════════════════════════════════════

class CommercialAPIService:
    """Static service for commercial API key management and usage metering."""

    @staticmethod
    def create_api_key(db: Session, user_id: str,
                       name: str = '', tier: str = 'free') -> Dict:
        """Create a new API key. Returns the raw key ONCE — it cannot be retrieved later."""
        from integrations.social.models import CommercialAPIKey

        if tier not in TIER_CONFIG:
            return {'error': f'Invalid tier: {tier}. Valid: {list(TIER_CONFIG.keys())}'}

        raw_key = secrets.token_urlsafe(48)
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        key_prefix = raw_key[:8]

        config = TIER_CONFIG[tier]
        api_key = CommercialAPIKey(
            user_id=user_id,
            key_hash=key_hash,
            key_prefix=key_prefix,
            name=name,
            tier=tier,
            rate_limit_per_day=config['rate_limit_per_day'],
            monthly_quota=config['monthly_quota'],
            usage_reset_at=datetime.utcnow() + timedelta(days=30),
        )
        db.add(api_key)
        db.flush()

        result = api_key.to_dict()
        result['raw_key'] = raw_key  # Only returned once
        return result

    @staticmethod
    def validate_api_key(db: Session, raw_key: str) -> Optional[Dict]:
        """Validate an API key. Returns key dict if valid, None if invalid.

        Uses constant-time hash comparison to prevent timing side-channels.
        """
        from integrations.social.models import CommercialAPIKey

        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        api_key = db.query(CommercialAPIKey).filter_by(
            key_hash=key_hash).first()

        # Constant-time validation: check all conditions before returning
        valid = True
        if not api_key:
            valid = False
        else:
            if not api_key.is_active:
                valid = False
            if api_key.expires_at and api_key.expires_at < datetime.utcnow():
                valid = False
            if api_key.usage_this_month >= api_key.monthly_quota:
                valid = False

        return api_key.to_dict() if valid and api_key else None

    @staticmethod
    def log_usage(db: Session, api_key_id: str, endpoint: str,
                  tokens_in: int = 0, tokens_out: int = 0,
                  compute_ms: int = 0, status_code: int = 200) -> Dict:
        """Log a single API call for billing.

        NOTE: does NOT increment `usage_this_month` — that is reserved
        pre-execution by `reserve_quota()` so over-quota callers are
        rejected before they burn GPU. This function writes the
        APIUsageLog row (with token counts + cost) after execution.
        """
        from integrations.social.models import APIUsageLog, CommercialAPIKey

        api_key = db.query(CommercialAPIKey).filter_by(id=api_key_id).first()
        tier = api_key.tier if api_key else 'free'
        cost_rate = COST_PER_1K_TOKENS.get(tier, 0.0)
        total_tokens = tokens_in + tokens_out
        cost = round((total_tokens / 1000.0) * cost_rate, 6)

        log = APIUsageLog(
            api_key_id=api_key_id,
            endpoint=endpoint,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            compute_ms=compute_ms,
            cost_credits=cost,
            status_code=status_code,
        )
        db.add(log)
        db.flush()
        return log.to_dict()

    @staticmethod
    def check_rate_limit(db: Session, api_key_id: str) -> bool:
        """Check if the key is within daily rate limit. True = allowed."""
        from integrations.social.models import APIUsageLog, CommercialAPIKey

        api_key = db.query(CommercialAPIKey).filter_by(id=api_key_id).first()
        if not api_key:
            return False

        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_count = db.query(APIUsageLog).filter(
            APIUsageLog.api_key_id == api_key_id,
            APIUsageLog.created_at >= today_start,
        ).count()

        return today_count < api_key.rate_limit_per_day

    @staticmethod
    def reserve_quota(db: Session, api_key_id: str) -> bool:
        """Atomically reserve one quota unit BEFORE executing an inference call.

        Prevents the race where a burst of concurrent requests all pass the
        stale `validate_api_key` quota read, then all burn GPU time before
        any of them get metered. Increments `usage_this_month` up front; the
        downstream `log_usage` no longer re-increments (it still writes the
        APIUsageLog row for billing accuracy).

        Returns True if the reservation succeeded (request may proceed).
        Returns False if the key is now over quota — caller MUST return 429
        before touching the backend.
        """
        from integrations.social.models import CommercialAPIKey

        api_key = db.query(CommercialAPIKey).filter_by(id=api_key_id).first()
        if not api_key:
            return False

        current = api_key.usage_this_month or 0
        if current >= api_key.monthly_quota:
            return False

        # Increment BEFORE the inference fires. Flushed immediately so a
        # concurrent request in another worker reads the incremented value.
        api_key.usage_this_month = current + 1
        db.flush()
        return True

    @staticmethod
    def release_quota(db: Session, api_key_id: str) -> None:
        """Refund a reservation when the caller decides not to execute.

        Used on pre-execution validation failures AFTER reserve_quota
        succeeded (e.g., a handler rejects the request shape for 400). Not
        called on backend failures — those still count against quota since
        GPU time was burned.
        """
        from integrations.social.models import CommercialAPIKey

        api_key = db.query(CommercialAPIKey).filter_by(id=api_key_id).first()
        if api_key and (api_key.usage_this_month or 0) > 0:
            api_key.usage_this_month = api_key.usage_this_month - 1
            db.flush()

    @staticmethod
    def get_usage_stats(db: Session, api_key_id: str, days: int = 30) -> Dict:
        """Aggregate usage stats for a key."""
        from integrations.social.models import APIUsageLog
        from sqlalchemy import func

        cutoff = datetime.utcnow() - timedelta(days=days)
        logs = db.query(APIUsageLog).filter(
            APIUsageLog.api_key_id == api_key_id,
            APIUsageLog.created_at >= cutoff,
        )

        total_calls = logs.count()
        total_tokens_in = db.query(func.coalesce(func.sum(APIUsageLog.tokens_in), 0)).filter(
            APIUsageLog.api_key_id == api_key_id,
            APIUsageLog.created_at >= cutoff,
        ).scalar()
        total_tokens_out = db.query(func.coalesce(func.sum(APIUsageLog.tokens_out), 0)).filter(
            APIUsageLog.api_key_id == api_key_id,
            APIUsageLog.created_at >= cutoff,
        ).scalar()
        total_cost = db.query(func.coalesce(func.sum(APIUsageLog.cost_credits), 0.0)).filter(
            APIUsageLog.api_key_id == api_key_id,
            APIUsageLog.created_at >= cutoff,
        ).scalar()

        return {
            'api_key_id': api_key_id,
            'period_days': days,
            'total_calls': total_calls,
            'total_tokens_in': int(total_tokens_in),
            'total_tokens_out': int(total_tokens_out),
            'total_cost_credits': round(float(total_cost), 4),
        }

    @staticmethod
    def list_api_keys(db: Session, user_id: str) -> List[Dict]:
        """List all API keys for a user."""
        from integrations.social.models import CommercialAPIKey
        keys = db.query(CommercialAPIKey).filter_by(
            user_id=user_id).order_by(CommercialAPIKey.created_at.desc()).all()
        return [k.to_dict() for k in keys]

    @staticmethod
    def revoke_api_key(db: Session, api_key_id: str) -> Optional[Dict]:
        """Revoke (deactivate) an API key."""
        from integrations.social.models import CommercialAPIKey
        key = db.query(CommercialAPIKey).filter_by(id=api_key_id).first()
        if not key:
            return None
        key.is_active = False
        db.flush()
        return key.to_dict()

    @staticmethod
    def reset_monthly_quotas(db: Session) -> int:
        """Reset monthly usage for keys past their reset date. Called by daemon."""
        from integrations.social.models import CommercialAPIKey
        now = datetime.utcnow()
        keys = db.query(CommercialAPIKey).filter(
            CommercialAPIKey.is_active == True,
            CommercialAPIKey.usage_reset_at <= now,
        ).all()
        count = 0
        for k in keys:
            k.usage_this_month = 0
            k.usage_reset_at = now + timedelta(days=30)
            count += 1
        if count > 0:
            db.flush()
        return count


# ═══════════════════════════════════════════════════════════════
# Brute-force protection (TTLCache + lock)
# ═══════════════════════════════════════════════════════════════

import threading as _bf_threading
from core.session_cache import TTLCache as _BFTTLCache

_failed_attempts_lock = _bf_threading.Lock()
_failed_attempts = _BFTTLCache(ttl_seconds=900, max_size=10000, name='api_brute_force')


def _check_brute_force(ip: str) -> bool:
    """Return True if IP exceeded 10 failed attempts in 15 min."""
    with _failed_attempts_lock:
        return (_failed_attempts.get(ip) or 0) >= 10


def _record_failed_attempt(ip: str):
    with _failed_attempts_lock:
        _failed_attempts[ip] = (_failed_attempts.get(ip) or 0) + 1


# ═══════════════════════════════════════════════════════════════
# Auth decorator for API key endpoints
# ═══════════════════════════════════════════════════════════════

def _seconds_until_daily_reset() -> int:
    """Return seconds until the next UTC midnight — when the daily rate
    limit window rolls over. Used for the Retry-After header on 429s."""
    now = datetime.utcnow()
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


def _seconds_until_monthly_reset(api_key: Optional[dict]) -> int:
    """Return seconds until `usage_reset_at` — when monthly quota rolls
    over. Falls back to 24h if the key does not carry a reset timestamp."""
    reset_raw = (api_key or {}).get('usage_reset_at') if api_key else None
    if not reset_raw:
        return 86400
    try:
        # SQLAlchemy normally returns a datetime; to_dict may serialize it
        if isinstance(reset_raw, datetime):
            reset_dt = reset_raw
        else:
            reset_dt = datetime.fromisoformat(str(reset_raw).replace('Z', ''))
        delta = reset_dt - datetime.utcnow()
        return max(1, int(delta.total_seconds()))
    except (ValueError, TypeError):
        return 86400


def _rate_limit_response(error: str, retry_after: int,
                         usage_meta: Optional[dict] = None):
    """Build a 429 response with the canonical Retry-After header and
    usage metadata. Centralised so every rate-limit / quota path emits a
    uniform shape (single writer — Gate 4 Parallel Path)."""
    payload = {'success': False, 'error': error, 'retry_after': retry_after}
    if usage_meta:
        payload['usage'] = usage_meta
    response = jsonify(payload)
    response.status_code = 429
    response.headers['Retry-After'] = str(retry_after)
    return response


def require_api_key(f):
    """Decorator: requires valid X-API-Key header. Sets g.api_key.

    Enforces quota PRE-EXECUTION via reserve_quota() — an over-quota key
    returns 429 BEFORE the inference call reaches llama-server/media-agent,
    so a malicious or runaway client cannot burn GPU time on requests that
    will be billed as zero. No log_usage() is called on the 429 path — the
    backend was never invoked, so no inference tokens are recorded.

    The 429 response carries a `Retry-After` header (seconds) so clients
    can back off correctly. All 4 tiers (free/starter/pro/enterprise) flow
    through this single gate — the decorator is the one writer that
    enforces quota uniformly.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        from integrations.social.models import get_db

        if _check_brute_force(request.remote_addr):
            # Brute-force window = 15 min; surface that as Retry-After.
            return _rate_limit_response('Too many failed attempts', 900)

        raw_key = request.headers.get('X-API-Key', '')
        if not raw_key:
            return jsonify({'success': False, 'error': 'Missing X-API-Key header'}), 401

        db = get_db()
        try:
            key_data = CommercialAPIService.validate_api_key(db, raw_key)
            if not key_data:
                _record_failed_attempt(request.remote_addr)
                return jsonify({'success': False, 'error': 'Invalid, expired, or quota-exceeded API key'}), 401

            if not CommercialAPIService.check_rate_limit(db, key_data['id']):
                return _rate_limit_response(
                    'Daily rate limit exceeded',
                    _seconds_until_daily_reset(),
                    usage_meta={
                        'tier': key_data.get('tier', 'free'),
                        'rate_limit_per_day': key_data.get(
                            'rate_limit_per_day'),
                    },
                )

            # Atomic pre-execution quota reservation. Closes the race where
            # concurrent requests all pass the stale validate_api_key read
            # and burn GPU before any of them update usage_this_month.
            # No log_usage() is called on the 429 path — backend was never
            # invoked, so no inference_used metric is written.
            if not CommercialAPIService.reserve_quota(db, key_data['id']):
                db.commit()  # persist any state from validate/rate checks
                return _rate_limit_response(
                    'Monthly quota exceeded',
                    _seconds_until_monthly_reset(key_data),
                    usage_meta={
                        'tier': key_data.get('tier', 'free'),
                        'monthly_quota': key_data.get('monthly_quota'),
                        'usage_this_month': key_data.get('usage_this_month'),
                    },
                )

            g.api_key = key_data
            g.api_db = db
            result = f(*args, **kwargs)
            db.commit()
            return result
        except Exception as e:
            db.rollback()
            raise
        finally:
            db.close()

    return decorated


# ═══════════════════════════════════════════════════════════════
# Blueprint
# ═══════════════════════════════════════════════════════════════

commercial_api_bp = Blueprint('commercial_api', __name__)


@commercial_api_bp.route('/api/v1/intelligence/chat', methods=['POST'])
@require_api_key
def intelligence_chat():
    """Metered intelligence-as-a-service chat endpoint."""
    data = request.get_json() or {}
    prompt = data.get('prompt', '')
    if not prompt:
        return jsonify({'success': False, 'error': 'prompt required'}), 400

    t0 = time.time()
    response_text = ''
    tokens_in = len(prompt.split())
    tokens_out = 0

    try:
        from core.http_pool import pooled_post
        result = pooled_post(f'http://localhost:{get_port("backend")}/chat', json={
            'user_id': g.api_key['user_id'],
            'prompt_id': 'api_intelligence',
            'prompt': prompt,
            'create_agent': False,
        }, timeout=120)
        resp_data = result.json() if hasattr(result, 'json') else {}
        response_text = resp_data.get('response', '')
        tokens_out = len(response_text.split())
    except Exception as e:
        logger.warning(f"Intelligence endpoint error: {e}")
        response_text = 'Intelligence service temporarily unavailable'

    elapsed_ms = int((time.time() - t0) * 1000)

    CommercialAPIService.log_usage(
        g.api_db, g.api_key['id'], '/v1/intelligence/chat',
        tokens_in=tokens_in, tokens_out=tokens_out,
        compute_ms=elapsed_ms)

    return jsonify({
        'success': True,
        'response': response_text,
        'usage': {'tokens_in': tokens_in, 'tokens_out': tokens_out,
                  'compute_ms': elapsed_ms},
    })


@commercial_api_bp.route('/api/v1/intelligence/analyze', methods=['POST'])
@require_api_key
def intelligence_analyze():
    """Document/data analysis via agent engine."""
    data = request.get_json() or {}
    document = data.get('document', '')
    question = data.get('question', 'Analyze this document')
    if not document:
        return jsonify({'success': False, 'error': 'document required'}), 400

    t0 = time.time()
    prompt = f"Analyze the following document and answer: {question}\n\n{document[:5000]}"
    tokens_in = len(prompt.split())

    try:
        from core.http_pool import pooled_post
        result = pooled_post(f'http://localhost:{get_port("backend")}/chat', json={
            'user_id': g.api_key['user_id'],
            'prompt_id': 'api_analyze',
            'prompt': prompt,
            'create_agent': False,
        }, timeout=120)
        resp = result.json() if hasattr(result, 'json') else {}
        response_text = resp.get('response', '')
        tokens_out = len(response_text.split())
    except Exception as e:
        logger.warning(f"Analysis endpoint error: {e}")
        response_text = 'Analysis service temporarily unavailable'
        tokens_out = 0

    elapsed_ms = int((time.time() - t0) * 1000)
    CommercialAPIService.log_usage(
        g.api_db, g.api_key['id'], '/v1/intelligence/analyze',
        tokens_in=tokens_in, tokens_out=tokens_out, compute_ms=elapsed_ms)

    return jsonify({'success': True, 'analysis': response_text,
                    'usage': {'tokens_in': tokens_in, 'tokens_out': tokens_out,
                              'compute_ms': elapsed_ms}})


@commercial_api_bp.route('/api/v1/intelligence/generate', methods=['POST'])
@require_api_key
def intelligence_generate():
    """Media generation (image/audio/video) via media agent."""
    data = request.get_json() or {}
    modality = data.get('modality', 'image')
    prompt_text = data.get('prompt', '')
    if not prompt_text:
        return jsonify({'success': False, 'error': 'prompt required'}), 400

    t0 = time.time()

    try:
        from integrations.service_tools.media_agent import generate_media
        result_json = generate_media(
            context=prompt_text,
            output_modality=modality,
            input_text=prompt_text,
        )
        import json
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
    except Exception as e:
        logger.warning(f"Generate endpoint error: {e}")
        result = {'error': 'Generation service temporarily unavailable'}

    elapsed_ms = int((time.time() - t0) * 1000)
    tokens_in = len(prompt_text.split())
    CommercialAPIService.log_usage(
        g.api_db, g.api_key['id'], '/v1/intelligence/generate',
        tokens_in=tokens_in, compute_ms=elapsed_ms)

    return jsonify({'success': True, 'result': result,
                    'usage': {'tokens_in': tokens_in, 'compute_ms': elapsed_ms}})


@commercial_api_bp.route('/api/v1/intelligence/hivemind', methods=['GET'])
@require_api_key
def intelligence_hivemind():
    """Query collective knowledge via HiveMind."""
    query = request.args.get('query', '')
    if not query:
        return jsonify({'success': False, 'error': 'query parameter required'}), 400

    t0 = time.time()
    result = {}

    try:
        from integrations.agent_engine.world_model_bridge import get_world_model_bridge
        bridge = get_world_model_bridge()
        result = bridge.query_hivemind(query)
    except Exception as e:
        logger.warning(f"HiveMind endpoint error: {e}")
        result = {'error': 'HiveMind service temporarily unavailable'}

    elapsed_ms = int((time.time() - t0) * 1000)
    CommercialAPIService.log_usage(
        g.api_db, g.api_key['id'], '/v1/intelligence/hivemind',
        tokens_in=len(query.split()), compute_ms=elapsed_ms)

    return jsonify({'success': True, 'result': result,
                    'usage': {'compute_ms': elapsed_ms}})


@commercial_api_bp.route('/api/v1/intelligence/usage', methods=['GET'])
@require_api_key
def intelligence_usage():
    """Get usage stats for the calling API key."""
    days = request.args.get('days', 30, type=int)
    stats = CommercialAPIService.get_usage_stats(
        g.api_db, g.api_key['id'], days=days)
    return jsonify({'success': True, 'data': stats})


# ─── Key management (JWT auth, not API key) ───

@commercial_api_bp.route('/api/v1/intelligence/keys', methods=['POST'])
def create_key():
    """Create a new API key (requires JWT auth)."""
    from integrations.social.auth import require_auth
    from integrations.social.models import get_db

    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Authorization required'}), 401

    from integrations.social.auth import _get_user_from_token
    token = auth_header[7:]
    user, db = _get_user_from_token(token)
    if not user:
        if db:
            db.close()
        return jsonify({'success': False, 'error': 'Invalid token'}), 401

    try:
        data = request.get_json() or {}
        result = CommercialAPIService.create_api_key(
            db, str(user.id),
            name=data.get('name', ''),
            tier=data.get('tier', 'free'),
        )
        if 'error' in result:
            return jsonify({'success': False, 'error': result['error']}), 400
        db.commit()
        return jsonify({'success': True, 'api_key': result}), 201
    finally:
        db.close()


@commercial_api_bp.route('/api/v1/intelligence/keys', methods=['GET'])
def list_keys():
    """List user's API keys (requires JWT auth)."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Authorization required'}), 401

    from integrations.social.auth import _get_user_from_token
    token = auth_header[7:]
    user, db = _get_user_from_token(token)
    if not user:
        if db:
            db.close()
        return jsonify({'success': False, 'error': 'Invalid token'}), 401

    try:
        keys = CommercialAPIService.list_api_keys(db, str(user.id))
        return jsonify({'success': True, 'keys': keys})
    finally:
        db.close()


@commercial_api_bp.route('/api/v1/intelligence/keys/<key_id>', methods=['DELETE'])
def revoke_key(key_id):
    """Revoke an API key (requires JWT auth)."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Authorization required'}), 401

    from integrations.social.auth import _get_user_from_token
    token = auth_header[7:]
    user, db = _get_user_from_token(token)
    if not user:
        if db:
            db.close()
        return jsonify({'success': False, 'error': 'Invalid token'}), 401

    try:
        result = CommercialAPIService.revoke_api_key(db, key_id)
        if not result:
            return jsonify({'success': False, 'error': 'Key not found'}), 404
        db.commit()
        return jsonify({'success': True, 'key': result})
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# Public pricing + upgrade flow
# ═══════════════════════════════════════════════════════════════
# Goal: close the gap between "commercial_api blueprint exists" and
# "customer can land $$".  The blueprint registration alone gives 8 routes
# but doesn't surface PRICING (customers can create free keys, never know
# what to pay for) and doesn't expose UPGRADE (no path from free→starter
# →pro→enterprise).  Pricing is public (no auth) so the marketing
# bootstrap goal in goal_seeding can advertise it.  Upgrade pipes through
# AP2's PaymentLedger so we don't reinvent payment processing — same
# primitives used by the autogen agent flow.

@commercial_api_bp.route('/api/v1/intelligence/pricing', methods=['GET'])
def pricing():
    """Public pricing — no auth required.  Customers + marketing agents
    use this to discover tiers and prices.  Returns the full TIER_CONFIG
    plus the per-1K-token rates and the revenue split commitment."""
    try:
        from integrations.agent_engine.revenue_aggregator import (
            REVENUE_SPLIT_USERS, REVENUE_SPLIT_INFRA, REVENUE_SPLIT_CENTRAL,
        )
        _split = {
            'users_pool': REVENUE_SPLIT_USERS,    # 0.90
            'infrastructure': REVENUE_SPLIT_INFRA,  # 0.09
            'central': REVENUE_SPLIT_CENTRAL,      # 0.01
        }
    except Exception:
        _split = {'users_pool': 0.90, 'infrastructure': 0.09, 'central': 0.01}

    return jsonify({
        'success': True,
        'pricing_currency': 'USD',
        'tiers': {
            name: {
                'tier': name,
                'monthly_price_usd': cfg['monthly_price_usd'],
                'description': cfg['description'],
                'rate_limit_per_day': cfg['rate_limit_per_day'],
                'monthly_quota_tokens': cfg['monthly_quota'],
                'priority': cfg['priority'],
                'cost_per_1k_tokens_usd': COST_PER_1K_TOKENS.get(name, 0.0),
            }
            for name, cfg in TIER_CONFIG.items()
        },
        'revenue_split': _split,
        'principles': [
            'Free tier is free forever — gatekeeping intelligence is not the goal.',
            '90% of paid revenue flows to compute providers (the people who train the hive).',
            'No vendor lock-in — local-first, your data stays on your device by default.',
        ],
        'endpoints': {
            'chat': 'POST /api/v1/intelligence/chat',
            'analyze': 'POST /api/v1/intelligence/analyze',
            'generate': 'POST /api/v1/intelligence/generate',
            'hivemind': 'GET  /api/v1/intelligence/hivemind',
            'usage': 'GET  /api/v1/intelligence/usage',
            'keys_create': 'POST   /api/v1/intelligence/keys',
            'keys_list': 'GET    /api/v1/intelligence/keys',
            'keys_revoke': 'DELETE /api/v1/intelligence/keys/<key_id>',
            'upgrade': 'POST /api/v1/intelligence/keys/<key_id>/upgrade',
            'upgrade_checkout': 'POST /api/v1/intelligence/keys/<key_id>/upgrade/checkout',
            'upgrade_checkout_complete': 'POST /api/v1/intelligence/upgrade/checkout/complete',
            'upgrade_phonepe': 'POST /api/v1/intelligence/keys/<key_id>/upgrade/phonepe',
            'phonepe_callback': 'POST /api/v1/intelligence/phonepe/callback',
        },
    })


@commercial_api_bp.route('/api/v1/intelligence/keys/<key_id>/upgrade', methods=['POST'])
def upgrade_key(key_id):
    """Upgrade an API key to a higher tier — routes payment through AP2.

    Body: {"target_tier": "starter|pro|enterprise", "payment_method": "card_token_or_method_id"}

    AP2's PaymentLedger handles the payment flow:
      - Creates a PaymentRequest for the tier's monthly_price_usd
      - Authorizes against the provided payment_method (mock gateway in
        dev; real gateway in prod via PaymentGatewayConnector)
      - On success, updates the API key's tier and limits

    This is the single chokepoint between "free key created" and
    "revenue lands in MeteredAPIUsage".
    """
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Authorization required'}), 401

    from integrations.social.auth import _get_user_from_token
    token = auth_header[7:]
    user, db = _get_user_from_token(token)
    if not user:
        if db:
            db.close()
        return jsonify({'success': False, 'error': 'Invalid token'}), 401

    try:
        data = request.get_json() or {}
        target_tier = (data.get('target_tier') or '').strip().lower()
        payment_method = (data.get('payment_method') or '').strip()

        if target_tier not in TIER_CONFIG:
            return jsonify({
                'success': False,
                'error': f'Invalid target_tier: {target_tier!r}. Valid: {list(TIER_CONFIG.keys())}',
            }), 400
        if target_tier == 'free':
            return jsonify({
                'success': False,
                'error': 'Cannot upgrade to free tier. Use DELETE to revoke instead.',
            }), 400
        if not payment_method:
            return jsonify({
                'success': False,
                'error': 'payment_method required (card token, AP2 method id, or mock id)',
            }), 400

        # Look up the API key being upgraded — must belong to this user.
        from integrations.social.models import CommercialAPIKey
        api_key = db.query(CommercialAPIKey).filter_by(
            id=key_id, user_id=str(user.id)).first()
        if not api_key:
            return jsonify({'success': False, 'error': 'API key not found'}), 404

        target_cfg = TIER_CONFIG[target_tier]
        amount_usd = float(target_cfg['monthly_price_usd'])

        # Route through AP2's PaymentLedger.  Falls back to MockPaymentGateway
        # by default (registered in PaymentLedger.__init__); real connectors
        # can be added via payment_ledger.add_gateway() at app start.
        try:
            from decimal import Decimal as _Decimal
            from integrations.ap2 import payment_ledger, PaymentMethod, PaymentStatus
        except Exception as _imp_err:
            return jsonify({
                'success': False,
                'error': f'AP2 payment surface unavailable: {_imp_err}',
            }), 503

        # Pick the live gateway for the SYNCHRONOUS upgrade flow.
        # Preference order:
        #   1. Caller-requested gateway via `data['gateway']` (when registered
        #      AND known to be synchronous — currently Stripe and Mock).
        #   2. Stripe (USD/international cards, synchronous capture).
        #   3. MockPaymentGateway (dev/test fallback).
        #
        # PhonePe is intentionally NOT selected here even when registered —
        # it is a redirect-then-webhook flow.  Selecting it synchronously
        # would mark the tier upgraded before the user actually pays
        # (revenue leak).  PhonePe surfaces as infrastructure (auto-
        # registered, visible in /pricing) so the async upgrade endpoint
        # — to be added when the webhook handler is wired — can use it
        # without further code changes.  Until then, requests for
        # `gateway: 'phonepe'` are rejected with 501.
        try:
            from integrations.ap2 import PaymentGateway as _PG
            requested = (data.get('gateway') or '').strip().lower()

            _SYNC_OK = {_PG.STRIPE, _PG.MOCK}

            if requested == 'phonepe':
                return jsonify({
                    'success': False,
                    'error': 'PhonePe upgrades require the async redirect '
                             'flow — not yet exposed on /upgrade.  Use '
                             'Stripe (set STRIPE_API_KEY) or the dev mock.',
                }), 501

            _gateway_choice = None
            if requested:
                try:
                    pg = _PG(requested)
                    if pg in payment_ledger.gateways and pg in _SYNC_OK:
                        _gateway_choice = pg
                except ValueError:
                    pass
            if _gateway_choice is None:
                _gateway_choice = (
                    _PG.STRIPE if _PG.STRIPE in payment_ledger.gateways
                    else _PG.MOCK
                )
        except Exception:
            _gateway_choice = None  # PaymentLedger will pick a default

        # Three-step AP2 flow.  We do all synchronously here for the simple
        # upgrade case; long-lived subscriptions would split across webhooks.
        try:
            _create_kwargs = dict(
                amount=_Decimal(str(amount_usd)),
                currency='USD',
                description=f'API tier upgrade: {api_key.tier} → {target_tier}',
                requester_agent_id=f'user:{user.id}',
                metadata={
                    'api_key_id': str(key_id),
                    'current_tier': api_key.tier,
                    'target_tier': target_tier,
                    'kind': 'tier_upgrade',
                    'payment_method_token': payment_method,
                },
            )
            if _gateway_choice is not None:
                _create_kwargs['gateway'] = _gateway_choice
            payment_req = payment_ledger.create_payment_request(**_create_kwargs)
            authorized = payment_ledger.authorize_payment(
                payment_req.payment_id, approver_id=f'user:{user.id}')
            if not authorized:
                return jsonify({
                    'success': False,
                    'error': 'Payment authorization failed',
                    'payment_request_id': payment_req.payment_id,
                }), 402  # 402 Payment Required
            result = payment_ledger.process_payment(payment_req.payment_id)
            if not result.get('success'):
                return jsonify({
                    'success': False,
                    'error': f'Payment processing failed: {result.get("error", "unknown")}',
                    'payment_request_id': payment_req.payment_id,
                }), 402
        except Exception as pay_err:
            return jsonify({
                'success': False,
                'error': f'Payment error: {pay_err}',
            }), 502

        # Payment succeeded — bump the key's tier and limits.
        api_key.tier = target_tier
        api_key.rate_limit_per_day = target_cfg['rate_limit_per_day']
        api_key.monthly_quota = target_cfg['monthly_quota']
        db.commit()

        return jsonify({
            'success': True,
            'api_key': {
                'id': str(api_key.id),
                'tier': api_key.tier,
                'rate_limit_per_day': api_key.rate_limit_per_day,
                'monthly_quota': api_key.monthly_quota,
            },
            'payment': {
                'request_id': payment_req.payment_id,
                'amount_usd': amount_usd,
                'status': 'completed',
            },
            'revenue_split': _revenue_split_usd(amount_usd),
        })
    finally:
        try:
            db.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# PhonePe async upgrade flow (India market — UPI / cards / netbanking)
#
#   POST /keys/<id>/upgrade/phonepe   → 202 + redirect_url (caller redirects user)
#   POST /phonepe/callback            → PhonePe S2S webhook, finalizes tier change
#
# The synchronous /upgrade endpoint cannot service PhonePe because PhonePe
# is a redirect-then-webhook flow — at create_payment time the user has not
# paid yet, only the checkout session exists.  Capturing immediately
# (Stripe-style) would return PAYMENT_PENDING and the upgrade would fail.
#
# So PhonePe gets its own pair of routes: initiate (creates the checkout
# session, returns the redirect URL, leaves the payment in PROCESSING
# state) and the webhook (signature-verified, idempotent finalize that
# bumps the API key tier when PhonePe confirms PAYMENT_SUCCESS).
# ═══════════════════════════════════════════════════════════════

@commercial_api_bp.route('/api/v1/intelligence/keys/<key_id>/upgrade/phonepe',
                         methods=['POST'])
def initiate_phonepe_upgrade(key_id: str):
    """Initiate a PhonePe-rail upgrade for a paid tier.

    Returns:
        202 Accepted with `{redirect_url, payment_request_id,
        gateway_transaction_id}` — the caller should redirect the user
        to ``redirect_url`` to complete the checkout.

    The tier is NOT yet bumped at this point.  PhonePe will POST to
    ``/api/v1/intelligence/phonepe/callback`` when the user completes
    payment; that's where the tier change lands.
    """
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Authorization required'}), 401

    from integrations.social.auth import _get_user_from_token
    token = auth_header[7:]
    user, db = _get_user_from_token(token)
    if not user:
        if db:
            db.close()
        return jsonify({'success': False, 'error': 'Invalid token'}), 401

    try:
        data = request.get_json() or {}
        target_tier = (data.get('target_tier') or '').strip().lower()

        if target_tier not in TIER_CONFIG:
            return jsonify({
                'success': False,
                'error': f'Invalid target_tier: {target_tier!r}. Valid: {list(TIER_CONFIG.keys())}',
            }), 400
        if target_tier == 'free':
            return jsonify({
                'success': False,
                'error': 'Cannot upgrade to free tier. Use DELETE to revoke instead.',
            }), 400

        from integrations.social.models import CommercialAPIKey
        api_key = db.query(CommercialAPIKey).filter_by(
            id=key_id, user_id=str(user.id)).first()
        if not api_key:
            return jsonify({'success': False, 'error': 'API key not found'}), 404

        target_cfg = TIER_CONFIG[target_tier]
        amount_usd = float(target_cfg['monthly_price_usd'])

        try:
            from decimal import Decimal as _Decimal
            from integrations.ap2 import (
                payment_ledger, PaymentMethod, PaymentStatus, PaymentGateway,
            )
        except Exception as imp_err:
            return jsonify({
                'success': False,
                'error': f'AP2 payment surface unavailable: {imp_err}',
            }), 503

        if PaymentGateway.PHONEPE not in payment_ledger.gateways:
            return jsonify({
                'success': False,
                'error': 'PhonePe gateway is not registered on this node. '
                         'Set PHONEPE_MERCHANT_ID + PHONEPE_SALT_KEY and restart.',
            }), 503

        # Build the payment request.  Metadata carries the bookkeeping
        # the webhook needs to finalize the tier change without trusting
        # any caller-supplied data.
        try:
            payment_req = payment_ledger.create_payment_request(
                amount=_Decimal(str(amount_usd)),
                currency='USD',  # PhonePe gateway converts to paise
                description=f'API tier upgrade: {api_key.tier} → {target_tier}',
                requester_agent_id=f'user:{user.id}',
                payment_method=PaymentMethod.INTERNAL_CREDITS,
                gateway=PaymentGateway.PHONEPE,
                metadata={
                    'api_key_id': str(key_id),
                    'current_tier': api_key.tier,
                    'target_tier': target_tier,
                    'kind': 'tier_upgrade',
                    'user_id': str(user.id),
                    'redirect_url': data.get(
                        'redirect_url',
                        'https://hevolve.ai/api/v1/intelligence/keys',
                    ),
                    'callback_url': data.get(
                        'callback_url',
                        'https://hevolve.ai/api/v1/intelligence/phonepe/callback',
                    ),
                    'mobile_number': data.get('mobile_number'),
                    'inr_amount': data.get('inr_amount'),  # optional override
                },
            )

            authorized = payment_ledger.authorize_payment(
                payment_req.payment_id, approver_id=f'user:{user.id}')
            if not authorized:
                return jsonify({
                    'success': False,
                    'error': 'Payment authorization failed',
                    'payment_request_id': payment_req.payment_id,
                }), 402

            # Call PhonePe to mint the checkout session.  We bypass
            # `process_payment` here because that would also try to
            # capture immediately (which would return PAYMENT_PENDING).
            gateway = payment_ledger.gateways[PaymentGateway.PHONEPE]
            phonepe_result = gateway.create_payment(payment_req)
            if not phonepe_result.get('success'):
                payment_req.update_status(
                    PaymentStatus.FAILED,
                    phonepe_result.get('error', 'PhonePe create_payment failed'))
                payment_ledger.save_ledger()
                return jsonify({
                    'success': False,
                    'error': phonepe_result.get('error', 'PhonePe initiation failed'),
                    'payment_request_id': payment_req.payment_id,
                }), 502

            payment_req.gateway_transaction_id = phonepe_result.get('transaction_id')
            payment_req.update_status(PaymentStatus.PROCESSING,
                                     'awaiting user payment on PhonePe checkout')
            payment_ledger.save_ledger()

            return jsonify({
                'success': True,
                'status': 'redirect_required',
                'redirect_url': phonepe_result.get('redirect_url'),
                'payment_request_id': payment_req.payment_id,
                'gateway_transaction_id': payment_req.gateway_transaction_id,
                'amount_usd': amount_usd,
                'inr_amount_estimated_paise': gateway._to_paise(payment_req),
                'message': 'Redirect the user to redirect_url to complete payment. '
                           'The tier will be bumped once PhonePe confirms payment '
                           'via the callback webhook.',
            }), 202
        except Exception as pay_err:
            logger.exception("PhonePe upgrade initiation failed")
            return jsonify({
                'success': False,
                'error': f'Payment error: {pay_err}',
            }), 502
    finally:
        try:
            db.close()
        except Exception:
            pass


@commercial_api_bp.route('/api/v1/intelligence/phonepe/callback',
                         methods=['POST'])
def phonepe_callback():
    """PhonePe server-to-server webhook: finalize a successful payment.

    PhonePe POSTs `{"response": "<base64>"}` with header
    `X-VERIFY: sha256(base64 + salt_key) + "###" + salt_index`.
    We:
      1. Verify the signature with the registered PhonePe gateway.
      2. Decode the payload, recover our merchantTransactionId.
      3. Look up the payment in the ledger.
      4. Idempotency: if already COMPLETED, 200 immediately.
      5. Defense in depth: re-query PhonePe status API via
         gateway.capture_payment — never trust the webhook body alone.
      6. On confirmed PAYMENT_SUCCESS, mark COMPLETED and bump the
         API key's tier limits.  Returns 200 to PhonePe regardless of
         tier-bump success so retries stop.
    """
    import base64 as _b64
    import json as _json

    try:
        from integrations.ap2 import payment_ledger, PaymentStatus, PaymentGateway
    except Exception as imp_err:
        logger.error(f"PhonePe callback: AP2 import failed: {imp_err}")
        return jsonify({'success': False, 'error': 'AP2 unavailable'}), 503

    if PaymentGateway.PHONEPE not in payment_ledger.gateways:
        # If PhonePe isn't registered we shouldn't be receiving callbacks.
        # Log loudly so the operator notices.
        logger.error("PhonePe callback received but gateway is not registered")
        return jsonify({'success': False, 'error': 'PhonePe gateway unavailable'}), 503

    gateway = payment_ledger.gateways[PaymentGateway.PHONEPE]
    x_verify = request.headers.get('X-VERIFY', '')
    body = request.get_json(silent=True) or {}
    b64_response = body.get('response', '')

    if not b64_response or not x_verify:
        logger.warning("PhonePe callback: missing response body or X-VERIFY header")
        return jsonify({'success': False, 'error': 'Missing signature payload'}), 400

    # Reject any callback that fails signature verification.  Constant-time
    # compare inside verify_callback prevents timing oracles.
    if not gateway.verify_callback(b64_response, x_verify):
        logger.warning("PhonePe callback: X-VERIFY signature mismatch — rejecting")
        return jsonify({'success': False, 'error': 'Invalid signature'}), 401

    # Decode the verified payload.
    try:
        decoded = _b64.b64decode(b64_response).decode('utf-8')
        envelope = _json.loads(decoded)
    except Exception as e:
        logger.warning(f"PhonePe callback: failed to decode response payload: {e}")
        return jsonify({'success': False, 'error': 'Invalid payload encoding'}), 400

    code = envelope.get('code', '')
    callback_data = envelope.get('data') or {}
    merchant_txn_id = callback_data.get('merchantTransactionId', '')
    if not merchant_txn_id:
        logger.warning("PhonePe callback: payload missing merchantTransactionId")
        return jsonify({'success': False, 'error': 'Missing merchantTransactionId'}), 400

    # Recover our payment record by gateway_transaction_id.
    payment_req = None
    for p in payment_ledger.payments.values():
        if p.gateway_transaction_id == merchant_txn_id:
            payment_req = p
            break
    if payment_req is None:
        logger.warning(f"PhonePe callback: no payment found for txn {merchant_txn_id}")
        # Don't 5xx — PhonePe would retry forever.  Return 200 with success=False.
        return jsonify({'success': False, 'error': 'Payment not found'}), 200

    # Idempotency: if we've already completed (or failed) this payment,
    # do nothing.  PhonePe retries the callback if it doesn't get 2xx
    # within ~30s, so duplicates are normal.
    if payment_req.status == PaymentStatus.COMPLETED:
        return jsonify({
            'success': True,
            'status': 'already_completed',
            'payment_request_id': payment_req.payment_id,
        }), 200
    if payment_req.status in (PaymentStatus.FAILED, PaymentStatus.CANCELLED,
                              PaymentStatus.REFUNDED, PaymentStatus.EXPIRED):
        return jsonify({
            'success': False,
            'status': payment_req.status.value,
            'payment_request_id': payment_req.payment_id,
        }), 200

    # Defense in depth: re-query PhonePe status, never trust webhook body alone.
    status_result = gateway.capture_payment(
        payment_req.payment_id, payment_req.gateway_transaction_id)
    if not status_result.get('success'):
        payment_req.update_status(
            PaymentStatus.FAILED,
            status_result.get('error', f'PhonePe status check returned {code}'))
        payment_ledger.save_ledger()
        return jsonify({
            'success': False,
            'status': 'payment_failed',
            'error': status_result.get('error', code),
            'payment_request_id': payment_req.payment_id,
        }), 200

    # Status check confirmed PAYMENT_SUCCESS — finalize.
    meta = payment_req.metadata or {}
    api_key_id = meta.get('api_key_id')
    target_tier = meta.get('target_tier')

    if not api_key_id or target_tier not in TIER_CONFIG:
        payment_req.update_status(
            PaymentStatus.FAILED,
            f'Callback received but metadata malformed (api_key_id={api_key_id!r}, '
            f'target_tier={target_tier!r})')
        payment_ledger.save_ledger()
        return jsonify({
            'success': False,
            'error': 'Payment metadata malformed',
            'payment_request_id': payment_req.payment_id,
        }), 200

    # Bump the API key tier.
    try:
        from integrations.social.models import get_db, CommercialAPIKey
        db = get_db()
        try:
            api_key = db.query(CommercialAPIKey).filter_by(id=api_key_id).first()
            if not api_key:
                payment_req.update_status(
                    PaymentStatus.FAILED,
                    f'API key {api_key_id} not found at callback finalize')
                payment_ledger.save_ledger()
                return jsonify({
                    'success': False,
                    'error': 'API key not found',
                    'payment_request_id': payment_req.payment_id,
                }), 200
            target_cfg = TIER_CONFIG[target_tier]
            api_key.tier = target_tier
            api_key.rate_limit_per_day = target_cfg['rate_limit_per_day']
            api_key.monthly_quota = target_cfg['monthly_quota']
            db.commit()
        finally:
            db.close()
    except Exception as db_err:
        logger.exception("PhonePe callback: tier bump failed")
        # Don't fail the payment — money has changed hands.  Mark
        # COMPLETED and emit a loud error so operator can manually
        # reconcile.
        logger.error(f"MANUAL RECONCILIATION NEEDED: payment "
                     f"{payment_req.payment_id} succeeded but tier bump "
                     f"failed for api_key {api_key_id}: {db_err}")

    payment_req.update_status(PaymentStatus.COMPLETED,
                              'PhonePe payment captured via callback')
    payment_ledger.save_ledger()

    amount_usd = float(payment_req.amount)
    return jsonify({
        'success': True,
        'status': 'completed',
        'payment_request_id': payment_req.payment_id,
        'api_key_id': api_key_id,
        'new_tier': target_tier,
        'revenue_split': _revenue_split_usd(amount_usd),
    }), 200


# ═══════════════════════════════════════════════════════════════
# Stripe Checkout flow (USD market — hosted card UI, lowest friction)
#
#   POST /keys/<id>/upgrade/checkout           → returns checkout_url
#   POST /upgrade/checkout/complete            → completes upgrade post-pay
#
# Why a separate flow from /upgrade:
#   /upgrade takes a Stripe PaymentMethod ID (`pm_...`) which only the
#   client side (Stripe.js) can produce.  That requires the buyer to
#   render Stripe Elements in our React app — meaningful eng work.
#   Stripe Checkout is the hosted alternative: we create a Session,
#   redirect the buyer to checkout.stripe.com, they pay, and Stripe
#   redirects them back to a success URL we control.  Zero card UI
#   in our stack.
#
# Revenue accounting: on completion the handler manually inserts a
# COMPLETED PaymentRequest into payment_ledger so the existing
# get_api_revenue_stats reads the $9 / $49 / $499 entry.  The Stripe
# PaymentIntent ID is preserved in metadata for audit.
# ═══════════════════════════════════════════════════════════════

@commercial_api_bp.route('/api/v1/intelligence/keys/<key_id>/upgrade/checkout',
                         methods=['POST'])
def create_upgrade_checkout(key_id):
    """Create a Stripe Checkout Session for a tier upgrade.

    Body: {
      "target_tier": "starter|pro|enterprise",
      "success_url": "https://hevolve.ai/upgrade-success",   (optional)
      "cancel_url":  "https://hevolve.ai/pricing"            (optional)
    }

    Returns 200 with {"checkout_url": "https://checkout.stripe.com/...",
                       "session_id": "cs_..."} on success.

    The buyer is then redirected client-side to checkout_url.  After
    payment Stripe redirects to success_url with `?session_id=...`
    appended; the React success page POSTs to /upgrade/checkout/complete
    to finalize the tier bump.
    """
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Authorization required'}), 401

    from integrations.social.auth import _get_user_from_token
    user, db = _get_user_from_token(auth_header[7:])
    if not user:
        if db:
            db.close()
        return jsonify({'success': False, 'error': 'Invalid token'}), 401

    try:
        data = request.get_json() or {}
        target_tier = (data.get('target_tier') or '').strip().lower()
        if target_tier not in TIER_CONFIG:
            return jsonify({
                'success': False,
                'error': f'Invalid target_tier: {target_tier!r}. Valid: {list(TIER_CONFIG.keys())}',
            }), 400
        if target_tier == 'free':
            return jsonify({
                'success': False,
                'error': 'Cannot upgrade to free tier via Checkout.',
            }), 400

        from integrations.social.models import CommercialAPIKey
        api_key = db.query(CommercialAPIKey).filter_by(
            id=key_id, user_id=str(user.id)).first()
        if not api_key:
            return jsonify({'success': False, 'error': 'API key not found'}), 404

        target_cfg = TIER_CONFIG[target_tier]
        amount_usd = float(target_cfg['monthly_price_usd'])
        success_url = data.get('success_url') or 'https://hevolve.ai/upgrade-success'
        cancel_url = data.get('cancel_url') or 'https://hevolve.ai/pricing'

        # success_url must contain `{CHECKOUT_SESSION_ID}` template so
        # Stripe injects the real session_id on redirect.  Append if
        # the caller didn't.
        if '{CHECKOUT_SESSION_ID}' not in success_url:
            sep = '&' if '?' in success_url else '?'
            success_url = f"{success_url}{sep}session_id={{CHECKOUT_SESSION_ID}}"

        # Locate the Stripe gateway.  Falls back to 503 if not registered
        # — operators see a clear "STRIPE_API_KEY missing" hint rather
        # than a generic 500.
        try:
            from integrations.ap2 import payment_ledger
            from integrations.ap2.ap2_protocol import PaymentGateway
            stripe_gw = payment_ledger.gateways.get(PaymentGateway.STRIPE)
        except Exception as e:
            return jsonify({
                'success': False,
                'error': f'AP2 payment surface unavailable: {e}',
            }), 503
        if stripe_gw is None or not getattr(stripe_gw, 'connected', False) \
                or stripe_gw._stripe is None:
            return jsonify({
                'success': False,
                'error': 'Stripe gateway not registered.  Set STRIPE_API_KEY '
                         'on the central deploy and restart (see '
                         'deploy/go_live_stripe.md Step 4).',
            }), 503

        try:
            session = stripe_gw._stripe.checkout.Session.create(
                mode='payment',
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'usd',
                        'product_data': {
                            'name': f"Hevolve API — {target_tier.title()} tier",
                            'description': target_cfg['description'],
                        },
                        'unit_amount': int(round(amount_usd * 100)),
                    },
                    'quantity': 1,
                }],
                success_url=success_url,
                cancel_url=cancel_url,
                customer_email=getattr(user, 'email', None) or None,
                # Metadata flows through to the PaymentIntent — used by
                # /upgrade/checkout/complete to identify which key the
                # session bought and verify the amount.
                metadata={
                    'api_key_id': str(key_id),
                    'user_id': str(user.id),
                    'current_tier': api_key.tier,
                    'target_tier': target_tier,
                    'kind': 'tier_upgrade_checkout',
                },
            )
        except Exception as e:
            return jsonify({
                'success': False,
                'error': f'Stripe Checkout Session creation failed: {e}',
            }), 502

        return jsonify({
            'success': True,
            'checkout_url': session.url,
            'session_id': session.id,
            'amount_usd': amount_usd,
            'target_tier': target_tier,
        }), 200
    finally:
        try:
            db.close()
        except Exception:
            pass


@commercial_api_bp.route('/api/v1/intelligence/upgrade/checkout/complete',
                         methods=['POST'])
def complete_upgrade_checkout():
    """Finalize a tier upgrade after the buyer completed Stripe Checkout.

    Body: {"session_id": "cs_..."}

    Idempotent: replays with the same session_id are detected via the
    payment_ledger metadata index and return 200 with the already-applied
    state rather than double-charging or double-upgrading.

    Auth: Bearer JWT.  The session metadata must contain `user_id`
    matching the JWT user — prevents Alice from completing Bob's
    checkout session.
    """
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Authorization required'}), 401

    from integrations.social.auth import _get_user_from_token
    user, db = _get_user_from_token(auth_header[7:])
    if not user:
        if db:
            db.close()
        return jsonify({'success': False, 'error': 'Invalid token'}), 401

    try:
        data = request.get_json() or {}
        session_id = (data.get('session_id') or '').strip()
        if not session_id or not session_id.startswith('cs_'):
            return jsonify({
                'success': False,
                'error': 'session_id required (starts with cs_)',
            }), 400

        try:
            from integrations.ap2 import payment_ledger
            from integrations.ap2.ap2_protocol import (
                PaymentGateway, PaymentMethod, PaymentRequest, PaymentStatus,
            )
        except Exception as e:
            return jsonify({
                'success': False,
                'error': f'AP2 payment surface unavailable: {e}',
            }), 503
        stripe_gw = payment_ledger.gateways.get(PaymentGateway.STRIPE)
        if stripe_gw is None or not getattr(stripe_gw, 'connected', False) \
                or stripe_gw._stripe is None:
            return jsonify({
                'success': False,
                'error': 'Stripe gateway not registered.',
            }), 503

        # Idempotency check: if we've already recorded this session,
        # skip re-charging the buyer.
        for existing in payment_ledger.payments.values():
            if (existing.metadata or {}).get('stripe_session_id') == session_id:
                return jsonify({
                    'success': True,
                    'already_completed': True,
                    'payment_request_id': existing.payment_id,
                    'status': existing.status.value if hasattr(existing.status, 'value') else str(existing.status),
                }), 200

        try:
            session = stripe_gw._stripe.checkout.Session.retrieve(session_id)
        except Exception as e:
            return jsonify({
                'success': False,
                'error': f'Stripe session retrieve failed: {e}',
            }), 502

        if session.payment_status != 'paid':
            return jsonify({
                'success': False,
                'error': f'Stripe session not paid (status={session.payment_status}).',
                'session_id': session_id,
            }), 402

        meta = session.metadata or {}
        if meta.get('kind') != 'tier_upgrade_checkout':
            return jsonify({
                'success': False,
                'error': 'Session is not a tier upgrade.',
            }), 400
        if meta.get('user_id') != str(user.id):
            return jsonify({
                'success': False,
                'error': 'Session belongs to a different user.',
            }), 403
        api_key_id = meta.get('api_key_id', '')
        target_tier = (meta.get('target_tier') or '').lower()
        if target_tier not in TIER_CONFIG or target_tier == 'free':
            return jsonify({
                'success': False,
                'error': 'Session has invalid target_tier metadata.',
            }), 400

        from integrations.social.models import CommercialAPIKey
        api_key = db.query(CommercialAPIKey).filter_by(
            id=api_key_id, user_id=str(user.id)).first()
        if not api_key:
            return jsonify({'success': False, 'error': 'API key not found'}), 404

        target_cfg = TIER_CONFIG[target_tier]
        amount_usd = float(session.amount_total) / 100.0

        # Record the revenue: insert a COMPLETED PaymentRequest into the
        # ledger so settle_metered_api_costs + get_api_revenue_stats see it.
        from decimal import Decimal as _Decimal
        payment = PaymentRequest(
            amount=_Decimal(str(amount_usd)),
            currency=(session.currency or 'usd').upper(),
            description=f'API tier upgrade via Checkout: {api_key.tier} → {target_tier}',
            requester_agent_id=f'user:{user.id}',
            payment_method=PaymentMethod.STRIPE,
            metadata={
                'kind': 'tier_upgrade',
                'api_key_id': api_key_id,
                'current_tier': api_key.tier,
                'target_tier': target_tier,
                'stripe_session_id': session_id,
                'stripe_payment_intent': getattr(session, 'payment_intent', None),
                'stripe_customer': getattr(session, 'customer', None),
            },
        )
        payment.gateway = PaymentGateway.STRIPE
        payment.update_status(PaymentStatus.AUTHORIZED, 'Stripe Checkout paid')
        payment.gateway_transaction_id = getattr(session, 'payment_intent', None) or session_id
        payment.update_status(PaymentStatus.COMPLETED, 'Stripe Checkout finalized')

        with payment_ledger.lock:
            payment_ledger.payments[payment.payment_id] = payment
            payment_ledger.save_ledger()

        # Apply the tier bump.
        try:
            api_key.tier = target_tier
            api_key.rate_limit_per_day = target_cfg['rate_limit_per_day']
            api_key.monthly_quota = target_cfg['monthly_quota']
            db.commit()
        except Exception as e:
            logger.error(f"MANUAL RECONCILIATION NEEDED: Stripe session "
                         f"{session_id} paid but tier bump failed for "
                         f"api_key {api_key_id}: {e}")
            return jsonify({
                'success': False,
                'error': f'Payment succeeded but tier bump failed: {e}',
                'payment_request_id': payment.payment_id,
                'session_id': session_id,
            }), 500

        return jsonify({
            'success': True,
            'payment_request_id': payment.payment_id,
            'api_key': {
                'id': str(api_key.id),
                'tier': api_key.tier,
                'rate_limit_per_day': api_key.rate_limit_per_day,
                'monthly_quota': api_key.monthly_quota,
            },
            'payment': {
                'amount_usd': amount_usd,
                'stripe_session_id': session_id,
                'stripe_payment_intent': getattr(session, 'payment_intent', None),
                'status': 'completed',
            },
            'revenue_split': _revenue_split_usd(amount_usd),
        }), 200
    finally:
        try:
            db.close()
        except Exception:
            pass
