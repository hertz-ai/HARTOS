"""Compute earnings -- the user-visible side of idle-compute earning.

Surface (idle_compute_workstream G3-G5):
  GET  /api/compute/earnings/estimate?tier=standard&has_gpu=0&weekly_hours=168
       -> tier-typical Spark/USD/week range for the self-advertising banner
       (no auth -- used in the pre-opt-in dialog so brand-new visitors see
       a believable "you'd earn ~X this week" preview).

  GET  /api/compute/earnings/<uid>?days=7&limit=20
       -> live rolling earnings for the opted-in user.  Sources rows from
       ResonanceTransaction where source_type matches a compute or API
       cost-recovery prefix -- no shadow ledger.  Auth required so a user
       can only read their own earnings.

  GET  /api/compute/earnings/stream
       -> Server-Sent Events feed forwarding `compute.task_settled` from
       the platform EventBus.  hevolve.ai's earnings drawer + Nunba
       shell tile keep this connection open and animate a row when a
       new settlement lands.  Subscriber pattern mirrors HiveContest's
       /api/hive/contest/ideas/stream (single canonical SSE shape).

The endpoint is the ONLY consumer of:
  - hosting_reward_service.estimate_weekly_spark (pre-opt-in helper)
  - revenue_aggregator's emit of `compute.task_settled` (post-settle)
  - ResonanceTransaction rows with source_type starting `compute:` or
    `api_cost_recovery` (the canonical settled-Spark ledger)

No new tables.  No parallel ledger.  No bespoke event topic.
"""
from __future__ import annotations

import logging

from flask import Blueprint, Response, g, jsonify, request, stream_with_context

from integrations.social.auth import require_auth
from integrations.social.hosting_reward_service import HostingRewardService
from integrations.social.models import get_db

logger = logging.getLogger(__name__)

compute_earnings_bp = Blueprint(
    'compute_earnings', __name__, url_prefix='/api/compute/earnings',
)


# --- Estimate (pre-opt-in banner) ----------------------------------

# Tier names mirror security.system_requirements.NodeTierLevel -- the
# single source of truth.  Built at import time so any future tier
# value automatically flows into the validator.
def _load_valid_tiers() -> tuple:
    try:
        from security.system_requirements import NodeTierLevel
        return tuple(t.value for t in NodeTierLevel)
    except Exception:
        # Fallback covers the current ladder; keeps the module
        # importable even when system_requirements can't load.
        return (
            'embedded', 'observer', 'lite', 'standard', 'full', 'compute_host',
        )


_VALID_TIERS = _load_valid_tiers()


def _parse_bool(raw: str) -> bool:
    return str(raw or '').strip().lower() in ('1', 'true', 'yes', 'y', 'on')


@compute_earnings_bp.route('/estimate', methods=['GET'])
def estimate():
    """Pre-opt-in tier-typical Spark/week estimate.

    Public -- the banner shows this BEFORE a user opts into compute
    contribution, so the dialog can say "you'd earn ~X this week".
    """
    tier = (request.args.get('tier') or 'standard').strip().lower()
    if tier not in _VALID_TIERS:
        tier = 'standard'
    has_gpu = _parse_bool(request.args.get('has_gpu'))
    try:
        weekly_hours = int(request.args.get('weekly_hours', 168))
    except (TypeError, ValueError):
        weekly_hours = 168
    weekly_hours = max(1, min(168, weekly_hours))

    data = HostingRewardService.estimate_weekly_spark(
        tier=tier, has_gpu=has_gpu, weekly_hours=weekly_hours,
    )
    return jsonify({'data': data})


# --- Live earnings (opted-in user reads own ledger) ----------------

# The SQL `LIKE` patterns that mark a ResonanceTransaction as a
# compute / API earning.  Today's only writer:
#   - revenue_aggregator.settle_metered_api_costs -> 'api_cost_recovery'
# When future writers ship (per-inference settle with 'compute:%' prefix
# or per-provider settle with 'api:%'), add the prefix here AND add the
# writer module to the WRITERS comment so the dependency stays explicit.
# WRITERS:
#   integrations/agent_engine/revenue_aggregator.py:settle_metered_api_costs
_EARNING_SOURCE_PREFIXES = ('api_cost_recovery%',)


@compute_earnings_bp.route('/<uid>', methods=['GET'])
@require_auth
def list_earnings(uid):
    """Recent compute/API earnings for the authenticated user.

    Auth: the requester MUST equal `uid` -- no horizontal escalation.
    Returns last `limit` (max 100) settled rows in the last `days`
    (max 90) days.
    """
    requester_id = getattr(g.user, 'id', None)
    if not requester_id or str(requester_id) != str(uid):
        return jsonify({'error': 'forbidden'}), 403

    try:
        days = min(90, max(1, int(request.args.get('days', 7))))
    except (TypeError, ValueError):
        days = 7
    try:
        limit = min(100, max(1, int(request.args.get('limit', 20))))
    except (TypeError, ValueError):
        limit = 20

    db = get_db()
    try:
        from datetime import datetime, timedelta
        from sqlalchemy import or_
        from integrations.social.models import ResonanceTransaction

        cutoff = datetime.utcnow() - timedelta(days=days)
        q = db.query(ResonanceTransaction).filter(
            ResonanceTransaction.user_id == str(uid),
            ResonanceTransaction.currency == 'spark',
            ResonanceTransaction.created_at >= cutoff,
            or_(*[
                ResonanceTransaction.source_type.like(p)
                for p in _EARNING_SOURCE_PREFIXES
            ]),
        ).order_by(ResonanceTransaction.created_at.desc()).limit(limit)

        rows = []
        total = 0
        for tx in q.all():
            amount = int(tx.amount or 0)
            total += amount
            rows.append({
                'id': getattr(tx, 'id', None),
                'amount_spark': amount,
                'source_type': tx.source_type,
                'source_id': tx.source_id,
                'description': tx.description,
                'created_at': tx.created_at.isoformat() if tx.created_at else None,
            })
        return jsonify({
            'data': rows,
            'meta': {
                'count': len(rows),
                'total_spark_in_window': total,
                'days': days,
            },
        })
    except Exception as exc:
        logger.debug(f'/api/compute/earnings list failed: {exc}')
        return jsonify({'error': 'lookup failed'}), 500
    finally:
        db.close()


# --- SSE -- live drawer fan-out -------------------------------------

@compute_earnings_bp.route('/stream', methods=['GET'])
@require_auth
def stream():
    """Server-Sent Events feed for the live earnings drawer.

    AUTH REQUIRED.  Subscribes to platform EventBus topic
    `compute.task_settled` (emitted by
    revenue_aggregator.settle_metered_api_costs after every Spark
    write).  Same shape as HiveContest's
    /api/hive/contest/ideas/stream -- single canonical SSE pattern.

    Privacy: the upstream payload contains a raw `operator_id` (the
    PeerNode operator user id).  This route filters server-side so
    only events whose `operator_id` matches the authenticated session
    are forwarded to the client.  Client-side filtering alone would
    have leaked every node settlement to any anonymous listener --
    caught in self-review before commit.
    """
    user = getattr(g, 'user', None)
    requester_id = getattr(user, 'id', None) if user else None
    if not requester_id:
        return jsonify({'error': 'auth required'}), 401
    requester_id = str(requester_id)

    import json as _json
    import queue as _queue
    import time as _time

    q: _queue.Queue = _queue.Queue(maxsize=200)

    # Same registry-based bus accessor used by api_hive_contest stream.
    try:
        from core.platform.registry import get_registry
        _reg = get_registry()
        bus = _reg.get('events') if _reg.has('events') else None
    except Exception:
        bus = None

    def _on_event(_topic, payload):
        # Defense-in-depth scope guard: forward only this user's
        # settlement events.  Anonymous listeners cannot reach this
        # closure (route is auth'd), but explicit filter prevents a
        # logged-in user from seeing other users' rows even if the
        # auth layer is later bypassed.
        try:
            if payload and str(payload.get('operator_id') or '') == requester_id:
                q.put_nowait(payload)
        except _queue.Full:
            pass

    subscribed = False
    if bus is not None:
        try:
            bus.on('compute.task_settled', _on_event)
            subscribed = True
        except Exception as exc:
            logger.debug(f'compute earnings SSE subscribe failed: {exc}')

    def _generate():
        # initial keepalive flushes proxies / lets client confirm connect
        yield 'event: ping\ndata: {}\n\n'
        last_ping = _time.time()
        try:
            while True:
                try:
                    payload = q.get(timeout=5.0)
                    yield (
                        f'event: compute.task_settled\n'
                        f'data: {_json.dumps(payload)}\n\n'
                    )
                except _queue.Empty:
                    pass
                if _time.time() - last_ping > 15:
                    yield 'event: ping\ndata: {}\n\n'
                    last_ping = _time.time()
        finally:
            if subscribed and bus is not None:
                try:
                    bus.off('compute.task_settled', _on_event)
                except Exception:
                    pass

    headers = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
    }
    return Response(stream_with_context(_generate()), headers=headers)
