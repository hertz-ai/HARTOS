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
import os
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

# The tiers a person may issue to themselves.  Every other tier is BOUGHT
# through the upgrade routes (AP2 ledger, Stripe, PhonePe), never self-issued:
# before this, POST /keys passed the body's tier straight through, so any
# signed-in person could mint a pro key with pro quotas for nothing, and with
# the lanes (owner policy 2026-10-12) a paid-tier key runs on the revenue lane
# where the Claude expert serves.  Operators (holds_central_role) may still
# issue a paid key by hand.
SELF_ISSUABLE_TIERS = frozenset(t for t, c in TIER_CONFIG.items() if not c['monthly_price_usd'])


def _invalid_tier_error(tier) -> str:
    """The ONE wording for a tier that is not in the table (service and route)."""
    return f'Invalid tier: {tier}. Valid: {list(TIER_CONFIG.keys())}'


def _mock_payments_allowed(user) -> bool:
    """May this caller's tier upgrade be settled by the MockPaymentGateway?

    The mock authorizes any payment_method string, so for a paying customer
    it is a free tier (review of 0cf610371: a plain user named
    ``gateway: mock``, or hit a node without Stripe, and got enterprise for
    nothing, with the $499 recorded in the ledger as paid).  Only an operator
    (integrations.social.auth.holds_central_role) or a node in the existing
    dev mode (HEVOLVE_DEV_MODE=true, which central forces to false at boot)
    may settle through the mock.
    """
    from integrations.social.auth import holds_central_role
    from security.master_key import is_dev_mode     # the ONE dev-mode switch
    return holds_central_role(user) or is_dev_mode()

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
            return {'error': _invalid_tier_error(tier)}

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
    def revoke_api_key(db: Session, api_key_id: str, user_id: str) -> Optional[Dict]:
        """Revoke (deactivate) ``user_id``'s API key ``api_key_id``; None when
        that person has no such key.  The owner is part of the lookup, as on
        every other key route: by id alone, any signed-in person who knew
        another's key id could switch it off (the review of fd2b4773c read
        it; task #146)."""
        from integrations.social.models import CommercialAPIKey
        key = db.query(CommercialAPIKey).filter_by(
            id=api_key_id, user_id=str(user_id)).first()
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

        from core.auth_local import client_key
        if _check_brute_force(client_key()):
            # Brute-force window = 15 min; surface that as Retry-After.
            return _rate_limit_response('Too many failed attempts', 900)

        raw_key = request.headers.get('X-API-Key', '')
        if not raw_key:
            return jsonify({'success': False, 'error': 'Missing X-API-Key header'}), 401

        db = get_db()
        try:
            key_data = CommercialAPIService.validate_api_key(db, raw_key)
            if not key_data:
                _record_failed_attempt(client_key())
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


def _metered_backend_chat(prompt: str, label: str):
    """The ONE self-POST the metered endpoints make to this node's own /chat.

    Returns ``(ok, text, status_code)``.  ``label`` names the endpoint in
    this function's log lines; it is NOT an agent.

    The customer's call is an agent-less conversation turn (``prompt_id``
    None): the self-POST itself never asks for an agent to be created, and
    ``agent_tools`` False keeps ``Create_Agent`` and ``Agentic_Router`` out
    of the turn's tool set (task #120: with them offered, the model could
    hand the customer the creation interview, a Review Mode agent under
    their id, or a Plan Mode consent question as the billed answer; the
    review of 7e7ce19bc drove it).  Two more flags are deliberate: ``draft_first``
    False, so the 0.8B draft and its classifier never answer a paid call
    (a confident ``is_create_agent`` verdict would mint an agent and hand the
    customer the creation text again; a confident ``delegate=none`` would ship
    the draft as the paid answer), and ``casual_conv`` left False, so the
    turn runs the default tool set, not the casual one that carries
    ``Shell_Command`` and ``Computer_Action`` (a self-issued free key must
    never reach a shell on the serving node).  It
    used to send ``prompt_id`` 'api_intelligence' / 'api_analyze': no
    such prompt file exists on any node, so /chat's ladder logged 'GATHER
    JSON doesnot EXISTS', set create_agent and ran gather_info turn 1/12;
    the model's non-JSON reply to the agent-building prompt went back to
    the customer raw.  Measured live on central 2026-10-13 (e829b072):
    "What is the capital of France?" was answered "I have the name and
    goal, but not every step came through as a step I can build...",
    success true, billed; the same question through /chat with prompt_id
    None answered "Paris is the capital of France."  Guarded by
    tests/unit/test_metered_chat_is_a_conversation.py.

    These two call sites were the 4th and 5th instances of the cdd379ad
    defect: ``cdd379ad`` fixed the speculative-dispatcher legs, ``272c4315``
    called itself "the third instance" (worker_loop) and ``503d8e98``
    consolidated them -- all three caller audits missed the *billed* ones.

    Bare, the self-POST is answered 401 by ``security/middleware.py`` gate 2
    on the central/regional tiers.  ``pooled_post`` does NOT call
    ``raise_for_status()`` (``core/http_pool.py``), so that 401 never raised,
    the callers' ``except`` never fired, ``.json()`` yielded the error body,
    ``.get('response')`` yielded ``''`` and the customer was handed
    ``success: True`` with an empty answer -- and billed for it.

    Deliberately NOT routed through ``dispatch.local_chat_dispatch``: that
    helper applies the daemon yield policy (``is_user_recently_active()`` ->
    ``'deferred'``) which exists to stop *background* work stealing the LLM
    from a human.  A paying customer's synchronous request is not background
    work, so inheriting that gate would trade this bug for a worse one.  What
    is shared with the other callers is the credential helper and the
    canonical /chat client (``core.chat_client.post_chat``: the same pooled
    path, with the request id this function mints passed through).
    """
    import uuid as _uuid
    from core.chat_client import post_chat
    from core.llm_outbound_logger import LANE_REVENUE, LANE_USER
    from integrations.agent_engine.dispatch import _internal_auth_headers

    try:
        # The canonical /chat client (core.chat_client): the same pooled path,
        # with the body normalised.  The id minted below passes through
        # untouched, so this is the wire this function always sent.
        result = post_chat(
            f'http://localhost:{get_port("backend")}/chat',
            {
                'user_id': g.api_key['user_id'],
                # No agent: the key is present (the route 400s without it)
                # and None is an agent-less conversation turn.  See the
                # docstring for what a made-up agent id did to customers.
                'prompt_id': None,
                'prompt': prompt,
                'create_agent': False,
                # Never the draft: see the docstring.  HEVOLVE_DRAFT_FIRST=1
                # in the node's env still overrides this body flag.
                'draft_first': False,
                # Never agent work: /chat withholds Create_Agent and
                # Agentic_Router from this turn (task #120), so the model
                # cannot hand the customer an interview or a consent question
                # as the billed answer.
                'agent_tools': False,
                # casual_conv is deliberately NOT sent (False): the casual
                # tool set carries Shell_Command / Computer_Action.
                # A paying customer's call is a genuine turn, never background:
                # a request id of its own (unique per call, so two calls in the
                # same second cannot share a lane entry) makes /chat and the
                # wire treat it so (core.chat_client: a missing id reads as
                # background), and keys the lane below.
                'request_id': 'api_%d_%s' % (int(time.time()), _uuid.uuid4().hex[:8]),
                # Owner policy 2026-10-12: a paying tier's turn is revenue-
                # bearing (may use the expert model, never throttled); the
                # free tier runs on the free model like any person's turn.
                # /chat believes this field only from the node itself: the
                # key or the system JWT _internal_auth_headers sends
                # (core.request_lane.is_internal_caller).
                'lane': LANE_REVENUE if (g.api_key.get('tier') or 'free') != 'free' else LANE_USER,
            },
            headers=_internal_auth_headers(),
            timeout=120)
    except Exception as e:
        # 502: we could not reach our own backend at all.
        logger.warning(f"Metered {label} /chat transport error: {e}")
        return False, '', 502

    # A /chat answer is read the way the call path reads it
    # (integrations.agentic_router._call_turn_reply, reached through
    # dispatch_to_agent; the one production caller of this pair):
    # chat_turn_result parses the body, and a 200
    # whose body is not /chat's JSON object (a proxy's or server's error
    # page) becomes an 'error' body instead of raising out of this function;
    # chat_turn_answer is the reply when the turn produced one, else None.
    # A turn that fails does not raise: the pipeline answers HTTP 200 with a
    # polite sentence (core.agent_tools.user_facing_error, the LLM_*_REPLY
    # constants, BUILD_INCOMPLETE_REPLY), right for a person and wrong for a
    # billed call.  Measured on central 2026-10-12 18:14Z: the "internal
    # snag" sentence went to a paying customer as success:true and was billed
    # (task #110).  The same reader also refuses an empty reply, 'success'
    # False, an 'error', a 'loading' notice and a route speaking for itself
    # (the review of 2efb73519: this used to be a narrower inline copy of
    # that rule; the review of ca3e314de corrected whose rule it is).
    from integrations.channels.chat_contract import chat_turn_answer, chat_turn_result
    try:
        status, data = chat_turn_result(result)
    except Exception as e:
        # chat_turn_result catches ValueError only (the call path's choice);
        # whatever else a response raises while its body is read is still
        # "no answer" here, with its audit row, instead of a 500 and no row
        # (the review of c8a9e0c28 measured that on a non-200).
        # The row keeps the backend's own status when it is not 200 (a 401
        # stays a 401); an unreadable 200 is "answered, not with an answer",
        # 502, like the other 200s below (the review of fbeb16284).
        logger.warning(f"Metered {label} /chat answer could not be read: {e}")
        status = getattr(result, 'status_code', None)
        return False, '', status if isinstance(status, int) and status != 200 else 502
    if status != 200:
        # WARNING, not DEBUG: central emits nothing below WARNING after boot,
        # which is why this leg was dead silently.  Same reasoning as the
        # sibling fixes in speculative_dispatcher.py and worker_loop.py.
        logger.warning(
            f"Metered {label} /chat returned HTTP {status}")
        return False, '', status

    text = chat_turn_answer(status, data)
    if text is None:
        shown = data.get('response') or data.get('error')
        logger.warning(
            f"Metered {label} /chat answered 200 without an answer: {str(shown)[:120]!r}")
        return False, '', 502
    return True, text, 200


def _metered_backend_failure(endpoint: str, tokens_in: int, elapsed_ms: int,
                             status: int):
    """Record a metered call whose backend produced no answer, and tell the
    customer the truth.

    "No answer" is any of: the self-POST to /chat could not be made or was
    refused (``status`` is the transport's: 502, 401, ...), /chat answered
    200 with a failed-turn sentence or nothing (502: the backend answered,
    not with an answer), the hive bridge had no thought, or the media agent
    generated nothing.  Billing 0 tokens is not a new policy --
    ``require_api_key`` already states it for the 429 path: "No log_usage()
    is called ... the backend was never invoked, so no inference tokens are
    recorded."  The row is still written (audit that the call happened, and
    its real status) but ``cost_credits`` computes to 0 because both token
    counts are 0.  The row does count toward the key's daily rate limit
    (``check_rate_limit`` counts rows, not successes); task #111 (degraded
    mode) is where a dead backend stops consuming the customer's day.

    ``reserve_quota`` has already consumed one unit of monthly quota by this
    point; that is the documented pre-execution reservation and is
    deliberately left alone here.
    """
    CommercialAPIService.log_usage(
        g.api_db, g.api_key['id'], endpoint,
        tokens_in=0, tokens_out=0, compute_ms=elapsed_ms,
        status_code=status)
    return jsonify({
        'success': False,
        'error': 'Intelligence backend unavailable; this call was not billed',
        'usage': {'tokens_in': 0, 'tokens_out': 0, 'compute_ms': elapsed_ms},
    }), 503


@commercial_api_bp.route('/api/v1/intelligence/chat', methods=['POST'])
@require_api_key
def intelligence_chat():
    """Metered intelligence-as-a-service chat endpoint."""
    data = request.get_json() or {}
    prompt = data.get('prompt', '')
    if not prompt:
        return jsonify({'success': False, 'error': 'prompt required'}), 400

    t0 = time.time()
    tokens_in = len(prompt.split())

    ok, response_text, status = _metered_backend_chat(prompt, 'api_intelligence')
    elapsed_ms = int((time.time() - t0) * 1000)

    if not ok:
        return _metered_backend_failure(
            '/v1/intelligence/chat', tokens_in, elapsed_ms, status)

    tokens_out = len(response_text.split())
    CommercialAPIService.log_usage(
        g.api_db, g.api_key['id'], '/v1/intelligence/chat',
        tokens_in=tokens_in, tokens_out=tokens_out,
        compute_ms=elapsed_ms, status_code=200)

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

    ok, response_text, status = _metered_backend_chat(prompt, 'api_analyze')
    elapsed_ms = int((time.time() - t0) * 1000)

    if not ok:
        return _metered_backend_failure(
            '/v1/intelligence/analyze', tokens_in, elapsed_ms, status)

    tokens_out = len(response_text.split())
    CommercialAPIService.log_usage(
        g.api_db, g.api_key['id'], '/v1/intelligence/analyze',
        tokens_in=tokens_in, tokens_out=tokens_out, compute_ms=elapsed_ms,
        status_code=200)

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
    tokens_in = len(prompt_text.split())

    try:
        from integrations.service_tools.media_agent import generate_media, generated_nothing
        result_json = generate_media(
            context=prompt_text,
            output_modality=modality,
            input_text=prompt_text,
        )
        import json
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
        nothing = generated_nothing(result)
    except Exception as e:
        logger.warning(f"Generate endpoint error: {e}")
        result, nothing = None, True

    elapsed_ms = int((time.time() - t0) * 1000)
    if nothing:
        # status error / unavailable / warming_up, or the agent raised: no
        # file and no task to poll.  Counted as a call and reported
        # success:true until task #110; the media agent's own reader
        # (classify_error) names the same statuses.
        return _metered_backend_failure(
            '/v1/intelligence/generate', tokens_in, elapsed_ms, 502)

    CommercialAPIService.log_usage(
        g.api_db, g.api_key['id'], '/v1/intelligence/generate',
        tokens_in=tokens_in, compute_ms=elapsed_ms, status_code=200)

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
    tokens_in = len(query.split())
    result, has_thought = None, False

    try:
        from integrations.agent_engine.world_model_bridge import get_world_model_bridge
        bridge = get_world_model_bridge()
        result = bridge.query_hivemind(query)
        has_thought = bool(result and result.get('thought'))
    except Exception as e:
        logger.warning(f"HiveMind endpoint error: {e}")
        result, has_thought = None, False

    elapsed_ms = int((time.time() - t0) * 1000)
    if not has_thought:
        # No thought: None (no hive core installed, no CCT for the
        # capability, the filter refused, every leg failed) or a reply whose
        # words are '' (every peer silent, the core's answer None).  Measured
        # on central 2026-10-13: {"result": null, "success": true} was
        # counted as a call (task #110).
        return _metered_backend_failure(
            '/v1/intelligence/hivemind', tokens_in, elapsed_ms, 502)

    CommercialAPIService.log_usage(
        g.api_db, g.api_key['id'], '/v1/intelligence/hivemind',
        tokens_in=tokens_in, compute_ms=elapsed_ms, status_code=200)

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
        raw_tier = data.get('tier')
        if raw_tier in (None, ''):
            raw_tier = 'free'
        if not isinstance(raw_tier, str):
            return jsonify({'success': False, 'error': _invalid_tier_error(raw_tier)}), 400
        tier = raw_tier.strip().lower()
        if tier not in TIER_CONFIG:
            return jsonify({'success': False, 'error': _invalid_tier_error(tier)}), 400
        if tier not in SELF_ISSUABLE_TIERS:
            from integrations.social.auth import holds_central_role
            if not holds_central_role(user):
                return jsonify({
                    'success': False,
                    'error': (f"Tier '{tier}' is bought, not issued: create a free key, then "
                              f"POST /api/v1/intelligence/keys/<key_id>/upgrade"),
                }), 400
        result = CommercialAPIService.create_api_key(
            db, str(user.id),
            name=data.get('name', ''),
            tier=tier,
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
        result = CommercialAPIService.revoke_api_key(db, key_id, str(user.id))
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
                'error': 'payment_method required (card token or AP2 method id)',
            }), 400

        # Look up the API key being upgraded — must belong to this user.
        from integrations.social.models import CommercialAPIKey
        api_key = db.query(CommercialAPIKey).filter_by(
            id=key_id, user_id=str(user.id)).first()
        if not api_key:
            return jsonify({'success': False, 'error': 'API key not found'}), 404
        refusal = _not_an_upgrade(api_key, target_tier)
        if refusal is not None:
            return refusal

        target_cfg = TIER_CONFIG[target_tier]
        amount_usd = float(target_cfg['monthly_price_usd'])

        # Route through AP2's PaymentLedger, which registers Stripe and
        # PhonePe at boot from their env keys; the gateway for this call is
        # chosen below, and the mock settles nothing for a customer.
        try:
            from decimal import Decimal as _Decimal
            from integrations.ap2 import payment_ledger, PaymentMethod, PaymentStatus
        except Exception as _imp_err:
            return jsonify({
                'success': False,
                'error': f'AP2 payment surface unavailable: {_imp_err}',
            }), 503

        # The gateway for the SYNCHRONOUS upgrade flow is the ledger's own
        # choice for USD (PaymentLedger.select_gateway: Stripe when it is
        # registered, else Mock).  The caller never picks it: a caller-named
        # ``gateway: mock`` let a plain user settle a $499 tier through the
        # MockPaymentGateway, which authorizes any payment_method string
        # (review of 0cf610371, measured on the real route).  A Mock result
        # is a paid tier for nothing, so it is refused unless the caller is an
        # operator or the node is in dev mode (_mock_payments_allowed).
        #
        # PhonePe is intentionally NOT selected here even when registered —
        # it is a redirect-then-webhook flow.  Selecting it synchronously
        # would mark the tier upgraded before the user actually pays
        # (revenue leak).  PhonePe surfaces as infrastructure (auto-
        # registered, visible in /pricing) so the async upgrade endpoint
        # — to be added when the webhook handler is wired — can use it
        # without further code changes.  Until then, requests for
        # `gateway: 'phonepe'` are rejected with 501.
        requested = str(data.get('gateway') or '').strip().lower()
        if requested == 'phonepe':
            return jsonify({
                'success': False,
                'error': 'PhonePe upgrades require the async redirect '
                         'flow — not yet exposed on /upgrade.  Use '
                         'Stripe (set STRIPE_API_KEY).',
            }), 501
        try:
            from integrations.ap2 import PaymentGateway as _PG
            _gateway_choice = payment_ledger.select_gateway('USD')
        except Exception as _sel_err:
            return jsonify({
                'success': False,
                'error': f'No payment gateway could be selected: {_sel_err}',
            }), 503
        if _gateway_choice == _PG.MOCK and not _mock_payments_allowed(user):
            return jsonify({
                'success': False,
                'error': 'No live USD payment gateway (Stripe) is configured on '
                         'this node (set STRIPE_API_KEY); the dev mock settles '
                         'nothing.',
            }), 503

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

        # Payment succeeded — the bought tier, through the one writer.
        _changed, failure = _apply_bought_tier(db, api_key, target_tier)
        if failure is not None:
            payload, status = _bump_failed(
                f'payment {payment_req.payment_id}', api_key.id, failure,
                payment_request_id=payment_req.payment_id)
            return jsonify(payload), status

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

    Body: ``target_tier``; optional ``redirect_url`` (the page the buyer
    returns to after paying, this node's public URL when absent) and
    ``mobile_number``.  A ``callback_url`` in the body is ignored: the
    server callback is always this node's own
    (integrations.ap2.ap2_protocol.own_gateway_urls), since a caller-named
    one would send PhonePe's signed confirmation elsewhere and the tier
    would never be bumped.  The price is the tier's, converted to INR at
    the operator's rate by the gateway; an ``inr_amount`` in the body is
    ignored too (it used to be stored as the gateway's override, and the
    callback's amount check compared against the same override, so a buyer
    could name their own price: the review of af987a962 measured the
    enterprise tier for one rupee).

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
        refusal = _not_an_upgrade(api_key, target_tier)
        if refusal is not None:
            return refusal

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
        # any caller-supplied data.  The buyer's return page may be the
        # caller's own (the app that started the upgrade); the server
        # callback is never the caller's, and neither is written here
        # when absent: the gateway resolves this node's URLs when it
        # mints the checkout and refuses without them.  Both used to
        # default to the web app's host here, which answers these paths
        # with the web app's 404 (task #122).
        metadata = {
            'api_key_id': str(key_id),
            'current_tier': api_key.tier,
            'target_tier': target_tier,
            'kind': 'tier_upgrade',
            'user_id': str(user.id),
            'mobile_number': data.get('mobile_number'),
            # No 'inr_amount': the gateway prices the tier itself.
        }
        if data.get('redirect_url'):
            metadata['redirect_url'] = str(data['redirect_url'])
        try:
            payment_req = payment_ledger.create_payment_request(
                amount=_Decimal(str(amount_usd)),
                currency='USD',  # PhonePe gateway converts to paise
                description=f'API tier upgrade: {api_key.tier} → {target_tier}',
                requester_agent_id=f'user:{user.id}',
                payment_method=PaymentMethod.INTERNAL_CREDITS,
                gateway=PaymentGateway.PHONEPE,
                metadata=metadata,
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
                # The paise PhonePe was asked for (recorded on the payment
                # by create_payment; the callback checks the capture
                # against the same number).
                'inr_amount_estimated_paise': gateway.expected_paise(payment_req),
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


def _verified_phonepe_envelope(what: str):
    """The decoded envelope of a PhonePe server-to-server POST whose X-VERIFY
    signature checks out against the registered gateway's salt, as
    ``(gateway, envelope, None)``; else ``(None, None, (response, status))``
    for the route to return.  Shared by the payment and the refund callback:
    PhonePe signs both the same way (``sha256(base64 + salt_key) + "###" +
    salt_index``), and the AP2 import, the gateway lookup, the missing-payload
    400, the bad-signature 401 and the undecodable 400 are one rule."""
    import base64 as _b64
    import json as _json

    try:
        from integrations.ap2 import payment_ledger, PaymentGateway
    except Exception as imp_err:
        logger.error(f"PhonePe {what}: AP2 import failed: {imp_err}")
        return None, None, (jsonify({'success': False, 'error': 'AP2 unavailable'}), 503)

    gateway = payment_ledger.gateways.get(PaymentGateway.PHONEPE)
    if gateway is None:
        # If PhonePe isn't registered we shouldn't be receiving callbacks.
        # Log loudly so the operator notices.
        logger.error(f"PhonePe {what} received but gateway is not registered")
        return None, None, (jsonify({'success': False,
                                     'error': 'PhonePe gateway unavailable'}), 503)

    x_verify = request.headers.get('X-VERIFY', '')
    body = request.get_json(silent=True) or {}
    b64_response = body.get('response', '')
    if not b64_response or not x_verify:
        logger.warning(f"PhonePe {what}: missing response body or X-VERIFY header")
        return None, None, (jsonify({'success': False,
                                     'error': 'Missing signature payload'}), 400)

    # Reject any callback that fails signature verification.  Constant-time
    # compare inside verify_callback prevents timing oracles.
    if not gateway.verify_callback(b64_response, x_verify):
        logger.warning(f"PhonePe {what}: X-VERIFY signature mismatch — rejecting")
        return None, None, (jsonify({'success': False, 'error': 'Invalid signature'}), 401)

    try:
        envelope = _json.loads(_b64.b64decode(b64_response).decode('utf-8'))
        if not isinstance(envelope, dict):
            raise ValueError('envelope is not an object')
    except Exception as e:
        logger.warning(f"PhonePe {what}: failed to decode response payload: {e}")
        return None, None, (jsonify({'success': False,
                                     'error': 'Invalid payload encoding'}), 400)
    return gateway, envelope, None


@commercial_api_bp.route('/api/v1/intelligence/phonepe/callback',
                         methods=['POST'])
def phonepe_callback():
    """PhonePe server-to-server webhook: finalize a successful payment.

    PhonePe POSTs `{"response": "<base64>"}` with header
    `X-VERIFY: sha256(base64 + salt_key) + "###" + salt_index`.
    We:
      1. Verify the signature with the registered PhonePe gateway
         (_verified_phonepe_envelope).
      2. Decode the payload, recover our merchantTransactionId.
      3. Look up the payment in the ledger.
      4. Idempotency: if already COMPLETED, 200 immediately.
      5. Defense in depth: re-query PhonePe status API via
         gateway.capture_payment — never trust the webhook body alone.
      6. On confirmed PAYMENT_SUCCESS, bump the API key's tier through the
         one writer and mark COMPLETED; a failed bump leaves the payment
         PROCESSING and answers 500 so PhonePe delivers the callback again
         and the retry finishes it.  PhonePe retries a non-200 answer a
         bounded number of times (its pages give no count for the PG
         callback; three for the in-store one); past them the payment
         stays PROCESSING and a person finishes it from the ERROR line
         (the review of 38a881c35).
    """
    gateway, envelope, refusal = _verified_phonepe_envelope('callback')
    if refusal is not None:
        return refusal
    from integrations.ap2 import payment_ledger, PaymentStatus

    code = envelope.get('code', '')
    callback_data = envelope.get('data') or {}
    merchant_txn_id = callback_data.get('merchantTransactionId', '')
    if not merchant_txn_id:
        logger.warning("PhonePe callback: payload missing merchantTransactionId")
        return jsonify({'success': False, 'error': 'Missing merchantTransactionId'}), 400

    # Recover our payment record by gateway_transaction_id (under the lock).
    payment_req = payment_ledger.find_by_gateway_transaction(merchant_txn_id)
    if payment_req is None:
        logger.warning(f"PhonePe callback: no payment found for txn {merchant_txn_id}")
        # Don't 5xx — PhonePe would retry forever.  Return 200 with success=False.
        return jsonify({'success': False, 'error': 'Payment not found'}), 200

    # Idempotency: if we've already completed (or failed) this payment,
    # do nothing.  PhonePe delivers the callback again when it does not get
    # a 200 OK, so duplicates are normal.
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
    if not status_result.get('success') and 'status' not in status_result:
        # The status API could not be asked (network, timeout): that says
        # nothing about the money.  Leave the payment PROCESSING and answer
        # non-2xx so PhonePe delivers the callback again.
        logger.warning(f"PhonePe callback: status check for "
                       f"{payment_req.payment_id} failed, will retry: "
                       f"{status_result.get('error')}")
        return jsonify({'success': False, 'status': 'status_check_failed',
                        'payment_request_id': payment_req.payment_id}), 503
    if not status_result.get('success') and             status_result.get('status') == 'PAYMENT_PENDING':
        # Not decided yet: the buyer's bank has not answered.  PhonePe sends
        # the final callback when it does; the payment stays PROCESSING.
        return jsonify({'success': False, 'status': 'pending',
                        'payment_request_id': payment_req.payment_id}), 200
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

    # Status check confirmed PAYMENT_SUCCESS.  The amount PhonePe took must be
    # the amount this payment SENT (recorded when the request was minted;
    # a recomputation at today's rate marked a correct capture a mismatch
    # after the operator moved the rate), when the status API reports one.
    _gw_data = (status_result.get('gateway_response') or {}).get('data') or {}
    if _gw_data.get('amount') is not None:
        try:
            _paid = int(_gw_data['amount'])
            _expected = gateway.expected_paise(payment_req)
        except (TypeError, ValueError):
            _paid, _expected = None, None
        if _paid is None or _paid != _expected:
            logger.error(f"MANUAL RECONCILIATION NEEDED: PhonePe captured "
                         f"{_gw_data.get('amount')} paise for "
                         f"{payment_req.payment_id}, which expected "
                         f"{_expected}; not finalized")
            payment_req.update_status(
                PaymentStatus.FAILED,
                f"amount mismatch: captured {_gw_data.get('amount')} paise, "
                f"expected {_expected}")
            payment_ledger.save_ledger()
            return jsonify({'success': False, 'status': 'amount_mismatch',
                            'payment_request_id': payment_req.payment_id}), 200

    # Finalize.
    meta = payment_req.metadata or {}

    # Not every AP2 PhonePe payment is a tier upgrade: an agent's
    # request_payment and a McGroce cart mandate route INR to PhonePe too
    # (PaymentLedger.select_gateway).  The money is confirmed above, so
    # such a payment COMPLETES here -- the tier-upgrade metadata check
    # below would otherwise mark a captured payment FAILED.  A commerce
    # checkout then places its McGroce order (integrations/commerce).
    if meta.get('kind') != 'tier_upgrade':
        # Only the delivery that completes the payment places the order: two
        # concurrent callbacks both pass the COMPLETED check above.
        if not payment_ledger.complete_once(
                payment_req.payment_id, 'PhonePe payment captured via callback'):
            return jsonify({
                'success': True,
                'status': 'already_completed',
                'payment_request_id': payment_req.payment_id,
            }), 200
        order = None
        if meta.get('mandate_id') and meta.get('kind') != 'commerce_checkout':
            # A generic mandate has no order step to consume it; the money is
            # taken, so it is used (a commerce checkout is consumed when its
            # order is placed).
            try:
                from integrations.ap2.ap2_mandate import get_mandate_store
                get_mandate_store().consume(meta['mandate_id'])
            except Exception as e:
                logger.warning(f"PhonePe callback: could not consume mandate "
                               f"{meta.get('mandate_id')}: {e}")
        if meta.get('kind') == 'commerce_checkout':
            try:
                from integrations.commerce.commerce_tools import (
                    complete_redirect_checkout)
                order = complete_redirect_checkout(payment_req.payment_id)
            except Exception:
                logger.exception(
                    "MANUAL RECONCILIATION NEEDED: PhonePe payment "
                    f"{payment_req.payment_id} captured but the commerce "
                    "order step raised")
        return jsonify({
            'success': True,
            'status': 'completed',
            'payment_request_id': payment_req.payment_id,
            'kind': meta.get('kind'),
            'order': order,
        }), 200

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

    # The bought tier, through the one writer (_apply_bought_tier: never a
    # downgrade, whatever order confirmations land in; this rail used to
    # bump with no rule, the review of fd2b4773c).  Money confirmed and the
    # write failed: the payment stays PROCESSING and PhonePe is answered a
    # 500, so its retry runs the status check and the bump again, as
    # Stripe's retry does for the Checkout finalizer; it used to answer 200
    # completed and mark the payment COMPLETED, so the retry was told
    # already_completed and nothing ever re-applied the tier (the review of
    # ec0dbe7be).
    failure = None
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
            _changed, failure = _apply_bought_tier(db, api_key, target_tier)
        finally:
            db.close()
    except Exception as db_err:
        # The traceback stays in the log (a locked database is diagnosed
        # from it); the sentence goes to the reconciliation line.
        logger.exception("PhonePe callback: tier bump for %s raised", api_key_id)
        failure = f'{type(db_err).__name__}: {db_err}'
    if failure is not None:
        payload, status = _bump_failed(
            f'PhonePe payment {payment_req.payment_id}', api_key_id, failure,
            payment_request_id=payment_req.payment_id, status='bump_failed')
        return jsonify(payload), status

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


@commercial_api_bp.route('/api/v1/intelligence/phonepe/refund-callback',
                         methods=['POST'])
def phonepe_refund_callback():
    """PhonePe's server-to-server refund status: acknowledge it and name it
    for a person.

    The refund request (PhonePePaymentGateway.refund_payment) names this
    route as its callback.  It used to name a route that existed nowhere,
    on the web app's host.  The signature is checked like the payment
    callback's; the refund's merchantTransactionId, the original
    transaction, the code and the amount are logged at WARNING for the
    operator to reconcile by hand.  No ledger state changes here: nothing
    in the tree calls refund_payment today, so there is no refund record to
    finish; what this route guarantees is that a refund's confirmation is
    received, verified and acknowledged (200, so PhonePe stops retrying)
    instead of lost to a 404.
    """
    _gateway, envelope, refusal = _verified_phonepe_envelope('refund callback')
    if refusal is not None:
        return refusal
    data = envelope.get('data') or {}
    code = envelope.get('code', '')
    logger.warning("PhonePe refund %s for transaction %s: %s (amount %s paise); "
                   "reconcile by hand, the ledger is not changed by this callback",
                   data.get('merchantTransactionId'), data.get('transactionId'),
                   code, data.get('amount'))
    return jsonify({'success': True, 'status': 'acknowledged',
                    'refund_id': data.get('merchantTransactionId'),
                    'code': code}), 200


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
#
#   POST /stripe/webhook                       → Stripe's signed event
#
# The completion route needs the buyer's browser to reach the success page
# and POST the session id; the deployed landing page has no such page, so
# a buyer was charged and never upgraded (task #122).  Stripe's
# checkout.session.completed event finalizes the same session through the
# same code (_finalize_paid_checkout_session), whichever arrives first.
# ═══════════════════════════════════════════════════════════════

def _stripe_gateway():
    """``(gateway, None)`` when the Stripe gateway is registered and connected
    with its SDK, else ``(None, (response, status))`` for the route to
    return: the ONE lookup for the checkout, the completion and the webhook
    routes, with the STRIPE_API_KEY hint operators need instead of a 500."""
    try:
        from integrations.ap2 import payment_ledger
        from integrations.ap2.ap2_protocol import PaymentGateway
    except Exception as e:
        return None, (jsonify({
            'success': False,
            'error': f'AP2 payment surface unavailable: {e}',
        }), 503)
    gw = payment_ledger.gateways.get(PaymentGateway.STRIPE)
    if gw is None or not getattr(gw, 'connected', False) \
            or getattr(gw, '_stripe', None) is None:
        return None, (jsonify({
            'success': False,
            'error': 'Card checkout (Stripe) is not configured on this node '
                     '(set STRIPE_API_KEY; on central it is a repo secret, '
                     'see deploy/go_live_stripe.md Step 4).',
        }), 503)
    return gw, None


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
        refusal = _not_an_upgrade(api_key, target_tier)
        if refusal is not None:
            return refusal

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

        # Locate the Stripe gateway (the ONE lookup, _stripe_gateway): 503
        # with the STRIPE_API_KEY hint when it is not registered.
        stripe_gw, refusal = _stripe_gateway()
        if refusal is not None:
            return refusal

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


def _session_metadata(session):
    """A Stripe Checkout Session's metadata as a plain dict; ``None`` when
    this node cannot read it.

    From stripe 15.0.0 (2026-03) StripeObject no longer subclasses dict:
    ``.get`` raises AttributeError, where 7.0.0 through 14.4.1 still answer
    it.  The review of 3153761b3 measured a 500 on every paid event with
    15.0.0 and 16.0.0, which is what central's next image installs
    (requirements.txt pins stripe>=7.0.0).  ``to_dict`` exists on every
    version (a StripeObject on all of them, a plain dict in a test) and a
    plain dict is copied.  No metadata is ``{}``, readable and not ours.
    A ``to_dict`` that raises, or a shape that is neither, is ``None``:
    unreadable is not "not ours", and a caller that treated it so answered
    Stripe 200 ignored for a paid tier upgrade, stopping the retries on an
    event no person had seen (the review of fd2b4773c measured it)."""
    meta = getattr(session, 'metadata', None)
    if meta is None:
        return {}
    if isinstance(meta, dict):
        return dict(meta)
    if hasattr(meta, 'to_dict'):
        try:
            return dict(meta.to_dict() or {})
        except Exception as e:
            logger.error("Stripe session %s: metadata could not be read (%s: %s)",
                         getattr(session, 'id', None), type(e).__name__, e)
            return None
    logger.error("Stripe session %s: metadata of type %s cannot be read",
                 getattr(session, 'id', None), type(meta).__name__)
    return None


def _is_tier_upgrade_checkout(meta) -> bool:
    """Is this (readable) Checkout metadata one of this node's tier
    upgrades, as create_upgrade_checkout stamps it?"""
    return isinstance(meta, dict) and meta.get('kind') == 'tier_upgrade_checkout'


def _tier_rank(tier: str) -> float:
    """What a tier costs a month: the order a bought tier is compared in."""
    return float(TIER_CONFIG.get(tier, {}).get('monthly_price_usd', 0.0) or 0.0)


def _tiers_at_least(target_tier: str) -> list:
    """The tiers a key may already hold that make ``target_tier`` no
    upgrade: those costing the same or more.  The ONE expression of the
    rule, read by _is_tier_upgrade (before a charge) and by the bought-tier
    UPDATE's WHERE (at the write), so the two cannot drift."""
    return [t for t in TIER_CONFIG if _tier_rank(t) >= _tier_rank(target_tier)]


def _is_tier_upgrade(current_tier, target_tier) -> bool:
    """The ONE rule for selling a tier, on every rail (the card checkout,
    the synchronous card charge, the PhonePe redirect) and for every writer
    of a key's tier: ``target_tier`` is a paid tier that costs more than the
    one the key is on.  There is no downgrade route (no writer lowers a
    tier; revoke only deactivates), and the pricing page offers every paid
    tier whatever the key holds, so a Pro user choosing starter was charged
    $9 for nothing, and before fd2b4773c the same purchase lowered the key
    (the review of fd2b4773c measured both).  A key on no known tier (None,
    a legacy name) counts as free."""
    if target_tier not in TIER_CONFIG or target_tier == 'free':
        return False
    return str(current_tier or '') not in _tiers_at_least(target_tier)


def _not_an_upgrade(api_key, target_tier):
    """The 400 a rail answers before charging when ``target_tier`` is not an
    upgrade for ``api_key`` (_is_tier_upgrade), else None.  Nothing here is
    a subscription (Checkout is a one-off payment and no tier expires), so
    the sentence names no renewal."""
    current = str(getattr(api_key, 'tier', '') or 'free')
    if _is_tier_upgrade(current, target_tier):
        return None
    return jsonify({
        'success': False,
        'error': f'This key is on the {current} tier; {target_tier} is not an '
                 f'upgrade from it (a tier is never lowered).',
        'current_tier': current,
        'target_tier': target_tier,
    }), 400


def _bump_failed(what_paid: str, api_key_id, failure: str, **payload_extra):
    """The answer when money was taken and the tier write failed: an ERROR
    line a person reconciles by, and a 500 (Stripe and PhonePe deliver
    again on a non-2xx; the browser's reload tries again), never a success
    over a key still on the old tier.  The line says a retry follows, so one
    occurrence is not yet a reconciliation case; a repeating one is (the
    review of 38a881c35)."""
    logger.error(f"MANUAL RECONCILIATION NEEDED if this repeats: {what_paid} paid but "
                 f"the tier bump failed for api_key {api_key_id}: {failure}; the "
                 f"gateway's redelivery or the buyer's reload retries it")
    return dict({
        'success': False,
        'error': f'Payment succeeded but tier bump failed: {failure}',
    }, **payload_extra), 500


def _recorded_checkout_unlocked(session_id: str):
    """The ledger payment that already finalized this Checkout Session, or
    None.  The caller holds payment_ledger.lock (the dict is iterated)."""
    from integrations.ap2 import payment_ledger
    for existing in payment_ledger.payments.values():
        if (existing.metadata or {}).get('stripe_session_id') == session_id:
            return existing
    return None


def _recorded_checkout(session_id: str):
    """_recorded_checkout_unlocked under the ledger's lock: the browser
    route's fast path, answering a replay without asking Stripe.  (An
    unlocked iteration while another thread inserts can raise 'dictionary
    changed size'; find_by_gateway_transaction locks for the same reason.)"""
    from integrations.ap2 import payment_ledger
    with payment_ledger.lock:
        return _recorded_checkout_unlocked(session_id)


def _checkout_payload(payment, api_key, already_completed: bool = False) -> dict:
    """The one answer a finalized Checkout gets, whichever caller asks and
    however many times: the tier and its limits, the amount, the split and
    the receipt ids the landing page's success screen renders
    (UpgradeSuccess.js reads api_key.tier, api_key.rate_limit_per_day,
    api_key.monthly_quota, payment.amount_usd, payment.stripe_payment_intent,
    revenue_split.* and payment_request_id).  A replay used to answer a bare
    already_completed envelope, on which that page threw (the review of
    3153761b3 ran it)."""
    meta = payment.metadata or {}
    amount_usd = float(payment.amount)
    body = {
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
            'stripe_session_id': meta.get('stripe_session_id'),
            'stripe_payment_intent': meta.get('stripe_payment_intent'),
            'status': payment.status.value if hasattr(payment.status, 'value') else str(payment.status),
        },
        'revenue_split': _revenue_split_usd(amount_usd),
    }
    if already_completed:
        body['already_completed'] = True
    return body


def _apply_bought_tier(db, api_key, target_tier: str):
    """Put ``api_key`` on the tier it bought; ``(changed, error)``.  The ONE
    writer of a key's tier for every rail.

    One conditional UPDATE: the row moves only while its tier costs less
    than ``target_tier`` (an unknown or NULL tier counts as free), so a key
    already there, or on a tier that costs more, is left alone WHATEVER
    this process read a moment ago.  Stripe does not order deliveries and
    events queue while the webhook secret is missing, so a starter session
    can be finalized after the pro one; a read-then-write guard here lost
    that race when the two landed together (the starter delivery read
    'free', waited on the ledger lock while the pro bump committed, then
    wrote starter: 16 to 23 of 40 rounds, the review of fd2b4773c).  On a
    failure the session is rolled back, so a later call (Stripe's retry
    after the 500, the buyer's reload) finds the key still below the
    bought tier and finishes the bump.  ``api_key`` is refreshed from the
    row afterwards, so the caller's payload shows what the table holds."""
    from sqlalchemy import or_
    from integrations.social.models import CommercialAPIKey
    if target_tier not in TIER_CONFIG:
        # Only a hand-edited ledger record lacks its target_tier (every
        # writer stamps it); it is a reconciliation case for the caller, not
        # a KeyError outside this try (the review of ec0dbe7be).
        return False, f'bought tier {target_tier!r} is not a tier'
    cfg = TIER_CONFIG[target_tier]
    try:
        rows = db.query(CommercialAPIKey).filter(
            CommercialAPIKey.id == api_key.id,
            or_(CommercialAPIKey.tier.is_(None),
                CommercialAPIKey.tier.notin_(_tiers_at_least(target_tier))),
        ).update({
            CommercialAPIKey.tier: target_tier,
            CommercialAPIKey.rate_limit_per_day: cfg['rate_limit_per_day'],
            CommercialAPIKey.monthly_quota: cfg['monthly_quota'],
        }, synchronize_session=False)
        db.commit()
        db.refresh(api_key)
    except Exception as e:
        try:
            db.rollback()
        except Exception as rb:
            logger.debug("tier bump: rollback after %s also failed: %s", e, rb)
        return False, str(e)
    current = getattr(api_key, 'tier', None)
    if not rows and current != target_tier:
        # Money was taken for a tier the key is already above: a refund
        # case the operator must see.  A replay of an applied session (the
        # key is already there) is routine and says nothing; one INFO line
        # covered both, and central logs nothing below WARNING (the review
        # of ec0dbe7be).
        logger.warning("api_key %s stays on %s: it paid for %s, which costs less; "
                       "the payment is recorded and may need a refund",
                       getattr(api_key, 'id', None), current, target_tier)
    return bool(rows), None


def _finalize_paid_checkout_session(session, db):
    """Record a PAID Stripe Checkout Session in the ledger and bump the tier
    it bought; ``(payload, http_status)``.

    The ONE finalizer, shared by complete_upgrade_checkout (the buyer's
    browser, with their JWT) and stripe_webhook (Stripe's signed event):
    until the webhook, the tier moved only if the success page POSTed the
    session id (task #122).  Both may finish the same session, so the
    ledger record is made under the ledger's lock: the first to insert it
    records the payment, every caller then applies the bought tier
    (_apply_bought_tier: a no-op once the key is there, so a replay after a
    failed bump finishes it, and never a downgrade), and every caller
    answers the same payload (_checkout_payload), with
    ``already_completed`` when the record was there before it.  ``session``
    is Stripe's object (attribute access, as the SDK returns it from
    retrieve() and in an event; its metadata through _session_metadata,
    since the SDK's metadata object is not a dict from 15.0.0 on); the
    metadata was written by create_upgrade_checkout and comes back signed
    by Stripe, so the user_id and api_key_id it carries are trusted here.
    The browser route checks the user_id against the JWT before calling.
    """
    from decimal import Decimal as _Decimal
    from integrations.ap2 import payment_ledger
    from integrations.ap2.ap2_protocol import (
        PaymentGateway, PaymentMethod, PaymentRequest, PaymentStatus,
    )
    from integrations.social.models import CommercialAPIKey

    session_id = getattr(session, 'id', None) or ''
    meta = _session_metadata(session)
    if meta is None:
        return {'success': False, 'error': 'Session metadata could not be read.'}, 500
    if not _is_tier_upgrade_checkout(meta):
        return {'success': False, 'error': 'Session is not a tier upgrade.'}, 400
    api_key_id = meta.get('api_key_id', '')
    user_id = str(meta.get('user_id') or '')
    target_tier = (meta.get('target_tier') or '').lower()
    if target_tier not in TIER_CONFIG or target_tier == 'free':
        return {'success': False, 'error': 'Session has invalid target_tier metadata.'}, 400

    api_key = db.query(CommercialAPIKey).filter_by(
        id=api_key_id, user_id=user_id).first()
    if not api_key:
        return {'success': False, 'error': 'API key not found'}, 404

    amount_usd = float(session.amount_total) / 100.0

    with payment_ledger.lock:
        payment = _recorded_checkout_unlocked(session_id)
        already = payment is not None
        if not already:
            # Record the revenue: a COMPLETED PaymentRequest in the ledger
            # so settle_metered_api_costs + get_api_revenue_stats see it.
            payment = PaymentRequest(
                amount=_Decimal(str(amount_usd)),
                currency=(getattr(session, 'currency', None) or 'usd').upper(),
                description=f'API tier upgrade via Checkout: {api_key.tier} → {target_tier}',
                requester_agent_id=f'user:{user_id}',
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
            payment_ledger.payments[payment.payment_id] = payment
            payment_ledger.save_ledger()

    # The bought tier: applied on the first finalize, and again by a replay
    # when a bump failed after the record was made (a 500 asks Stripe to
    # retry; the retry used to be answered already_completed over a key
    # still on the old tier, the review of 3153761b3).  Never a downgrade.
    _changed, failure = _apply_bought_tier(db, api_key, target_tier)
    if failure is not None:
        return _bump_failed(f'Stripe session {session_id}', api_key_id, failure,
                            payment_request_id=payment.payment_id,
                            session_id=session_id)

    return _checkout_payload(payment, api_key, already_completed=already), 200


@commercial_api_bp.route('/api/v1/intelligence/upgrade/checkout/complete',
                         methods=['POST'])
def complete_upgrade_checkout():
    """Finalize a tier upgrade after the buyer completed Stripe Checkout.

    Body: {"session_id": "cs_..."}

    Idempotent: replays with the same session_id are detected via the
    payment_ledger metadata index and return 200 with the already-applied
    state rather than double-charging or double-upgrading.  The webhook
    (stripe_webhook) usually finalizes the session first; this route then
    answers ``already_completed`` without asking Stripe.

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

        stripe_gw, refusal = _stripe_gateway()
        if refusal is not None:
            return refusal

        # A replay (or the webhook got there first): answer the same page
        # without asking Stripe, to the key's owner only, and finish the
        # bump if the record outlived a failed one (the buyer's first visit
        # recorded the payment and the bump failed; every reload then said
        # success over a key still on free, the review of fd2b4773c).
        existing = _recorded_checkout(session_id)
        if existing is not None:
            from integrations.social.models import CommercialAPIKey
            recorded = existing.metadata or {}
            api_key = db.query(CommercialAPIKey).filter_by(
                id=recorded.get('api_key_id'), user_id=str(user.id)).first()
            if not api_key:
                return jsonify({
                    'success': False,
                    'error': 'Session belongs to a different user.',
                }), 403
            _changed, failure = _apply_bought_tier(db, api_key, recorded.get('target_tier'))
            if failure is not None:
                payload, status = _bump_failed(
                    f'Stripe session {session_id}', api_key.id, failure,
                    payment_request_id=existing.payment_id, session_id=session_id)
                return jsonify(payload), status
            return jsonify(_checkout_payload(existing, api_key, already_completed=True)), 200

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

        # The kind before the user, as before the finalizer was lifted out:
        # a session that is no tier upgrade is a 400 whoever asks.
        meta = _session_metadata(session)
        if meta is None:
            return jsonify({
                'success': False,
                'error': 'Session metadata could not be read.',
            }), 500
        if not _is_tier_upgrade_checkout(meta):
            return jsonify({
                'success': False,
                'error': 'Session is not a tier upgrade.',
            }), 400
        if meta.get('user_id') != str(user.id):
            return jsonify({
                'success': False,
                'error': 'Session belongs to a different user.',
            }), 403

        payload, status = _finalize_paid_checkout_session(session, db)
        return jsonify(payload), status
    finally:
        try:
            db.close()
        except Exception:
            pass


@commercial_api_bp.route('/api/v1/intelligence/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Stripe's signed event: a paid Checkout Session bumps its tier here,
    whether or not the buyer's browser ever reaches the success page.

    Register the endpoint in the Stripe dashboard for the event
    ``checkout.session.completed`` and set its signing secret as
    STRIPE_WEBHOOK_SECRET (deploy/go_live_stripe.md).  The raw body and the
    Stripe-Signature header go to StripePaymentGateway.verify_webhook;
    nothing in the body is read before that.  Answers: 503 when the Stripe
    gateway is not registered or the secret is not set (nothing unsigned is
    ever trusted; Stripe retries non-2xx for days, so the operator can fix
    the configuration without losing the event), 400 when the signature or
    the payload is refused, 200 for an event of another type or an unpaid
    session, and 200 ``ignored`` for a paid session that is not one of
    this node's tier upgrades (another product on the same Stripe account,
    the dashboard's test event); the finalizer's status otherwise: a 500
    (the tier bump failed) asks Stripe to retry, and the retry finds the
    record and finishes the bump; a 4xx from it (malformed tier, no such
    key) is answered 200 with the error named and an ERROR line for a
    person, since a retry cannot fix it.
    """
    stripe_gw, refusal = _stripe_gateway()
    if refusal is not None:
        return refusal
    if not getattr(stripe_gw, 'webhook_secret', ''):
        logger.error("Stripe webhook received but STRIPE_WEBHOOK_SECRET is not "
                     "set; the event is not trusted")
        return jsonify({'success': False,
                        'error': 'STRIPE_WEBHOOK_SECRET is not set on this node'}), 503
    try:
        event = stripe_gw.verify_webhook(request.get_data(),
                                         request.headers.get('Stripe-Signature', ''))
    except ValueError as e:
        logger.warning(f"Stripe webhook refused: {e}")
        return jsonify({'success': False, 'error': 'Invalid signature'}), 400

    event_type = getattr(event, 'type', None)
    if event_type != 'checkout.session.completed':
        return jsonify({'success': True, 'status': 'ignored', 'type': event_type}), 200
    session = event.data.object
    session_id = getattr(session, 'id', None)
    if getattr(session, 'payment_status', None) != 'paid':
        return jsonify({'success': True, 'status': 'unpaid', 'session_id': session_id}), 200
    meta = _session_metadata(session)
    if meta is None:
        # Unreadable is not "not ours": a 500 keeps Stripe delivering (for
        # days) until a person has read the ERROR line _session_metadata
        # wrote; a 200 here lost a paid tier upgrade with the retries
        # stopped (the review of fd2b4773c).
        logger.error("MANUAL RECONCILIATION NEEDED: Stripe session %s paid but its "
                     "metadata could not be read; not finalized, Stripe will retry",
                     session_id)
        return jsonify({'success': False, 'status': 'metadata_unreadable',
                        'session_id': session_id}), 500
    if not _is_tier_upgrade_checkout(meta):
        # Not one of this node's tier upgrades: another product's checkout
        # on the same account, or the dashboard's test event.  Acknowledged
        # and ignored; it is not a reconciliation case.
        logger.info("Stripe webhook: paid session %s is not a tier upgrade; ignored",
                    session_id)
        return jsonify({'success': True, 'status': 'ignored', 'session_id': session_id}), 200

    from integrations.social.models import get_db
    db = get_db()
    try:
        payload, status = _finalize_paid_checkout_session(session, db)
    finally:
        try:
            db.close()
        except Exception:
            pass
    if 400 <= status < 500:
        logger.error(f"MANUAL RECONCILIATION NEEDED: Stripe session {session_id} "
                     f"paid but not finalized: {payload.get('error')}")
        return jsonify(dict(payload, status='not_finalized', session_id=session_id)), 200
    return jsonify(payload), status
