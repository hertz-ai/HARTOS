"""L3 collective earning — SAFE INERT slice (producer + pure aggregator).

See docs/architecture/L3_COLLECTIVE_EARNING_DESIGN.md.  This module is
deliberately INERT — it advances the "every Nunba earns collectively for the
user" architecture WITHOUT touching the risky cross-node legs:

  * extract_earning_delta() — READ-ONLY snapshot of THIS node's local earnings.
  * aggregate_collective_earnings() — PURE function summing a user's earnings
    across THEIR OWN nodes, idempotent + no cross-user leak.

Neither broadcasts, remits, nor mutates anything.  The transport + consent +
central-verify legs are NOT wired here: they block on the steward's
user-scoped-remit (A) vs owned-subset-gossip (B) decision and 2-node live
verification (#150).  Shipping money/privacy across nodes unverified is the
BLOCK-defect class flagged in memory/flywheel_action_banking_gap_2026-06-11.md,
so these pure pieces stay inert until that review.
"""
import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

EARNING_DELTA_VERSION = 1


def extract_earning_delta(db, user_id: str, node_id: str,
                          period_days: int = 30) -> Optional[dict]:
    """Read-only snapshot of THIS node's local earnings for a user + period.

    Reuses the ONE canonical revenue query
    (revenue_aggregator.query_revenue_streams) — no duplicate DB logic (DRY).
    Returns None when user_id or node_id is missing — a node must NEVER emit an
    unattributed earning delta (the #183 lesson: never trust a self-claimed
    identity).  NO mutation, NO broadcast — a pure read."""
    if not user_id or not node_id:
        return None
    try:
        from .revenue_aggregator import query_revenue_streams
        rev = query_revenue_streams(db, period_days=period_days)
    except Exception as e:
        logger.debug(f"earning delta extract failed: {e}")
        return None
    return {
        'version': EARNING_DELTA_VERSION,
        'user_id': str(user_id),
        'node_id': str(node_id),
        'period_days': int(period_days),
        'gross': float(rev.get('total_gross', 0.0)),
        'pool_share_90': float(rev.get('user_pool_share', 0.0)),
        'api_revenue': float(rev.get('api_revenue', 0.0)),
        'ad_revenue': float(rev.get('ad_revenue', 0.0)),
        'hosting_payouts': float(rev.get('hosting_payouts', 0.0)),
        'ts': time.time(),
    }


def aggregate_collective_earnings(deltas: List[dict]) -> Dict[str, dict]:
    """Sum each user's earnings across THEIR OWN nodes — the collective pool.

    PURE (no I/O).  Three invariants the design requires:
      * Privacy — a delta only ever contributes to its OWN user_id's total; one
        user's money never mixes with another's.
      * Idempotent — keyed by (user_id, node_id, period_days); only the LATEST
        (max ts) delta per key counts, so a re-remit of the same node/period
        can NEVER double-credit.
      * Attribution — deltas missing a user_id or node_id are dropped, never
        summed into an "unknown" bucket.

    Returns: {user_id: {pool_share_90, gross, node_count, nodes: [...]}}
    """
    # 1. Dedup: keep the newest delta per (user_id, node_id, period_days).
    latest: Dict[tuple, dict] = {}
    for d in deltas or []:
        try:
            uid = str(d.get('user_id') or '')
            nid = str(d.get('node_id') or '')
            per = int(d.get('period_days') or 0)
            if not uid or not nid:
                continue                       # never aggregate an unattributed delta
            key = (uid, nid, per)
            prev = latest.get(key)
            if prev is None or float(d.get('ts', 0)) >= float(prev.get('ts', 0)):
                latest[key] = d                # last-write-wins by ts
        except Exception:
            continue                           # malformed delta — skip, never crash

    # 2. Sum per user across their deduped node/period deltas.
    out: Dict[str, dict] = {}
    for (uid, nid, _per), d in latest.items():
        acc = out.setdefault(uid, {
            'pool_share_90': 0.0, 'gross': 0.0, 'node_count': 0, 'nodes': set()})
        acc['pool_share_90'] += float(d.get('pool_share_90', 0.0))
        acc['gross'] += float(d.get('gross', 0.0))
        acc['nodes'].add(nid)
    for uid, acc in out.items():
        acc['node_count'] = len(acc['nodes'])
        acc['nodes'] = sorted(acc['nodes'])
        acc['pool_share_90'] = round(acc['pool_share_90'], 6)
        acc['gross'] = round(acc['gross'], 6)
    return out
