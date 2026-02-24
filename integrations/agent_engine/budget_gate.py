"""
Budget Gate — pre-dispatch spend control for LLM calls and agent goals.

Fail-closed economics: don't spend what you don't have.

Functions:
  estimate_llm_cost_spark(prompt, model_name) — token-based cost estimate
  check_goal_budget(goal_id, estimated_cost) — atomic row-lock deduction
  check_platform_affordability() — 7-day net revenue check (cached 60s)
  pre_dispatch_budget_gate(goal_id, prompt, model_name) — combined gate

Pattern extracted from: speculative_dispatcher._check_and_reserve_budget() (lines 314-340)
"""
import logging
import os
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Cost estimation ──────────────────────────────────────────────────

# Approximate Spark cost per 1K tokens by model family.
# Order matters: most-specific prefix first (gpt-4o-mini before gpt-4o before gpt-4).
_MODEL_COST_MAP = {
    'gpt-4o-mini': 1,
    'gpt-4o': 4,
    'gpt-4': 6,
    'gpt-3.5': 1,
    'groq': 0,      # Groq free tier — zero Spark
    'llama': 0,      # Local model — zero metered cost
    'mistral': 0,    # Local model
    'phi': 0,        # Local model
    'qwen': 0,       # Local model
}


def estimate_llm_cost_spark(prompt: str, model_name: str = 'gpt-4o') -> int:
    """Estimate Spark cost for an LLM call before execution.

    Uses tiktoken if available (already in codebase), falls back to word-count
    heuristic (~1.3 tokens per word).  Returns integer Spark cost (min 1).
    """
    # Token count
    token_count = 0
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model(model_name)
        token_count = len(enc.encode(prompt))
    except Exception:
        # Fallback: ~1.3 tokens per word on average
        token_count = max(1, int(len(prompt.split()) * 1.3))

    # Map model to per-1K cost
    cost_per_1k = 2  # default
    model_lower = (model_name or '').lower()
    for prefix, cost in _MODEL_COST_MAP.items():
        if prefix in model_lower:
            cost_per_1k = cost
            break

    spark_cost = max(1, int((token_count / 1000) * cost_per_1k))
    return spark_cost


# ── Goal budget (row-lock atomic deduction) ──────────────────────────

def check_goal_budget(goal_id: Optional[str],
                      estimated_cost: int) -> Tuple[bool, int, str]:
    """Check and reserve Spark budget for a goal (atomic row lock).

    Extracted from speculative_dispatcher._check_and_reserve_budget().
    Returns: (allowed, remaining_budget, reason)
    """
    if not goal_id:
        return True, -1, 'no_goal_constraint'

    try:
        from integrations.social.models import get_db, AgentGoal
        db = get_db()
        try:
            goal = db.query(AgentGoal).filter_by(
                id=goal_id).with_for_update().first()
            if not goal:
                return True, -1, 'goal_not_found'

            budget = goal.spark_budget or 0
            spent = goal.spark_spent or 0
            remaining = budget - spent

            if remaining < estimated_cost:
                db.rollback()
                return False, remaining, f'insufficient_budget ({remaining} < {estimated_cost})'

            goal.spark_spent = spent + estimated_cost
            db.commit()
            return True, remaining - estimated_cost, 'budget_reserved'
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"Budget check unavailable: {e}")
        return True, -1, 'budget_system_unavailable'


# ── Platform affordability (cached 60s) ──────────────────────────────

_affordability_cache: Dict = {}
_CACHE_TTL = 60  # seconds


def check_platform_affordability() -> Tuple[bool, Dict]:
    """Check 7-day platform net revenue flow.

    Uses query_revenue_streams() (revenue_aggregator.py) — single source of truth.
    Caches result for 60s to avoid per-request DB queries.
    Returns: (can_afford, details_dict)
    """
    now = time.time()
    cached = _affordability_cache.get('result')
    if cached and (now - _affordability_cache.get('ts', 0)) < _CACHE_TTL:
        return cached

    try:
        from integrations.social.models import get_db
        from integrations.agent_engine.revenue_aggregator import query_revenue_streams
        db = get_db()
        try:
            streams = query_revenue_streams(db, period_days=7)
            net = streams['total_gross'] - streams['hosting_payouts']
            can_afford = net >= 0
            result = (can_afford, {
                'gross_7d': round(streams['total_gross'], 2),
                'payouts_7d': round(streams['hosting_payouts'], 2),
                'net_7d': round(net, 2),
            })
            _affordability_cache['result'] = result
            _affordability_cache['ts'] = now
            return result
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"Affordability check unavailable: {e}")
        return True, {'reason': 'affordability_check_unavailable'}


# ── Combined gate ────────────────────────────────────────────────────

def pre_dispatch_budget_gate(goal_id: Optional[str],
                             prompt: str,
                             model_name: str = 'gpt-4o') -> Tuple[bool, str]:
    """Combined pre-dispatch budget gate.

    1. Estimate LLM cost
    2. Check goal budget (atomic deduction)
    3. Check platform affordability (cached)

    Returns: (allowed, reason)
    """
    estimated_cost = estimate_llm_cost_spark(prompt, model_name)

    # Goal-level budget
    allowed, remaining, reason = check_goal_budget(goal_id, estimated_cost)
    if not allowed:
        logger.warning(f"Budget gate BLOCKED: goal={goal_id}, {reason}")
        return False, f'goal_budget_exceeded: {reason}'

    # Platform-level affordability
    can_afford, details = check_platform_affordability()
    if not can_afford:
        logger.warning(f"Budget gate BLOCKED: platform not affordable: {details}")
        return False, f'platform_not_affordable: net_7d={details.get("net_7d", "?")}'

    return True, f'allowed (est_cost={estimated_cost}, remaining={remaining})'


# ── Metered API usage recording ──────────────────────────────────────

def record_metered_usage(node_id: str, model_id: str, task_source: str,
                         tokens_in: int, tokens_out: int,
                         cost_per_1k: float,
                         goal_id: str = None,
                         requester_node_id: str = None) -> Optional[str]:
    """Record metered API usage for cost recovery. Returns usage ID or None.

    Called after every non-local LLM call. If task_source != 'own', creates
    a MeteredAPIUsage record so the revenue agent can settle it.
    Only records for metered (non-local) models with cost > 0.
    """
    if cost_per_1k <= 0:
        return None  # Local model — no cost to recover

    actual_usd_cost = ((tokens_in + tokens_out) / 1000.0) * cost_per_1k
    if actual_usd_cost <= 0:
        return None

    # Check daily limit for hive/idle tasks
    if task_source in ('hive', 'idle'):
        try:
            from integrations.agent_engine.compute_config import get_compute_policy
            policy = get_compute_policy(os.environ.get('HEVOLVE_NODE_ID'))
            daily_limit = policy.get('metered_daily_limit_usd', 0.0)
            if daily_limit > 0:
                # Check today's spend
                from integrations.social.models import db_session, MeteredAPIUsage
                from sqlalchemy import func as sa_func
                from datetime import datetime, timedelta
                with db_session() as db:
                    today_start = datetime.utcnow().replace(
                        hour=0, minute=0, second=0, microsecond=0)
                    today_spend = db.query(
                        sa_func.coalesce(sa_func.sum(MeteredAPIUsage.actual_usd_cost), 0)
                    ).filter(
                        MeteredAPIUsage.node_id == node_id,
                        MeteredAPIUsage.task_source.in_(['hive', 'idle']),
                        MeteredAPIUsage.created_at >= today_start,
                    ).scalar() or 0.0
                    if today_spend + actual_usd_cost > daily_limit:
                        logger.warning(
                            f"Metered daily limit exceeded: "
                            f"${today_spend:.2f}+${actual_usd_cost:.2f} > ${daily_limit:.2f}")
                        return None
        except Exception as e:
            logger.debug(f"Daily limit check skipped: {e}")

    # Look up operator_id from PeerNode
    operator_id = None
    try:
        from integrations.social.models import db_session, PeerNode
        with db_session() as db:
            peer = db.query(PeerNode).filter_by(node_id=node_id).first()
            if peer:
                operator_id = peer.node_operator_id
    except Exception:
        pass

    # Estimate Spark cost
    estimated_spark = max(1, int(actual_usd_cost * int(
        os.environ.get('HEVOLVE_SPARK_PER_USD', '100'))))

    # Write MeteredAPIUsage record
    try:
        from integrations.social.models import db_session, MeteredAPIUsage
        with db_session() as db:
            usage = MeteredAPIUsage(
                node_id=node_id,
                operator_id=operator_id,
                model_id=model_id,
                task_source=task_source,
                goal_id=goal_id,
                requester_node_id=requester_node_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_per_1k_tokens=cost_per_1k,
                estimated_spark_cost=estimated_spark,
                actual_usd_cost=actual_usd_cost,
                settlement_status='pending' if task_source != 'own' else 'settled',
            )
            db.add(usage)
            db.commit()
            logger.debug(f"Metered usage recorded: model={model_id}, "
                         f"source={task_source}, cost=${actual_usd_cost:.4f}")
            return usage.id
    except Exception as e:
        logger.debug(f"Metered usage recording failed: {e}")
        return None
