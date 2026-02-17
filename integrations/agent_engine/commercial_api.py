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

from flask import Blueprint, g, jsonify, request
from sqlalchemy.orm import Session

logger = logging.getLogger('hevolve_social')


# ═══════════════════════════════════════════════════════════════
# Tier configuration (deterministic pricing)
# ═══════════════════════════════════════════════════════════════

TIER_CONFIG = {
    'free':       {'rate_limit_per_day': 100,    'monthly_quota': 3000,     'priority': 'low'},
    'starter':    {'rate_limit_per_day': 1000,   'monthly_quota': 30000,    'priority': 'normal'},
    'pro':        {'rate_limit_per_day': 10000,  'monthly_quota': 300000,   'priority': 'high'},
    'enterprise': {'rate_limit_per_day': 100000, 'monthly_quota': 10000000, 'priority': 'critical'},
}

COST_PER_1K_TOKENS = {
    'free': 0.0,
    'starter': 0.5,
    'pro': 0.3,
    'enterprise': 0.2,
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
        """Log a single API call for billing."""
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

        if api_key:
            api_key.usage_this_month = (api_key.usage_this_month or 0) + 1
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

def require_api_key(f):
    """Decorator: requires valid X-API-Key header. Sets g.api_key."""
    @wraps(f)
    def decorated(*args, **kwargs):
        from integrations.social.models import get_db

        if _check_brute_force(request.remote_addr):
            return jsonify({'success': False, 'error': 'Too many failed attempts'}), 429

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
                return jsonify({'success': False, 'error': 'Daily rate limit exceeded'}), 429

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
        result = pooled_post('http://localhost:6777/chat', json={
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
        result = pooled_post('http://localhost:6777/chat', json={
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
