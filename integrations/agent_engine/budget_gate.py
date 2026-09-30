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
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Per-goal budget-check cache.  Daemon ticks call check_goal_budget() on
# every speculative dispatch; py-spy traces showed the SQLAlchemy first()
# + new SQLite connection cycle is the dominant CPU consumer when many
# goals × idle_agents fire in tight succession.  A short TTL is enough
# to break the storm — within 10s the goal's row hasn't materially
# changed (this function is the only writer in the daemon path).  Burst
# under-counting is bounded: at most one un-deducted hit per goal per
# TTL window.  Cache stores the FULL return tuple so callers see the
# exact same shape they'd see from a fresh DB query.
_BUDGET_CACHE_TTL_S = 10.0
_budget_cache: Dict[str, Tuple[float, Tuple[bool, int, str]]] = {}
_budget_cache_lock = threading.Lock()

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


def spark_per_1k(model_name: str) -> int:
    """Spark per 1K tokens for a model name, by family prefix: 0 for the
    local and free-tier families, 2 for a cloud model the map does not know.

    ONE lookup for estimate_llm_cost_spark and for the registry's
    'configured-api' backend, so the pre-dispatch estimate and the expert
    path's reservation (ModelBackend.cost_per_1k_tokens) price the same model
    the same way.
    """
    model_lower = (model_name or '').lower()
    for prefix, cost in _MODEL_COST_MAP.items():
        if prefix in model_lower:
            return cost
    return 2


def _is_local_model() -> bool:
    """Detect whether the active LLM is a local model (zero Spark cost).

    Delegates to port_registry.is_local_llm() which checks whether the
    resolved LLM URL points to localhost/127.0.0.1, or if a local model
    name is configured.
    """
    from core.port_registry import is_local_llm
    return is_local_llm()


def estimate_llm_cost_spark(prompt: str, model_name: str = '') -> int:
    """Estimate Spark cost for an LLM call before execution.

    Uses tiktoken if available (already in codebase), falls back to word-count
    heuristic (~1.3 tokens per word).  Returns integer Spark cost (min 1 for
    paid models, 0 for local/self-hosted models).

    If the active LLM is local (detected via HEVOLVE_LOCAL_LLM_URL env var),
    cost is always 0 — local inference has no metered Spark cost.
    """
    # Local models cost nothing regardless of the model_name parameter.
    # Check env var first (definitive signal that a local backend is active).
    if _is_local_model():
        return 0

    # The configured model when the caller named none (or the legacy 'gpt-4o'
    # default): pricing follows what the node is actually configured to call.
    model_name = _resolve_model_name(model_name)

    # Map model to per-1K cost (check BEFORE token counting — skip work for free models)
    cost_per_1k = spark_per_1k(model_name)

    # Free-tier and local models cost 0 Spark even without the env var.
    # This catches cases where model_name is 'qwen', 'llama', 'phi', etc.
    # but HEVOLVE_LOCAL_LLM_URL is not explicitly set.
    if cost_per_1k == 0:
        return 0

    # Token count (only computed for paid models).  Single source of
    # truth — see core.token_utils.count_tokens_for_text (tiktoken-with-
    # fallback).  Previously this site had its own inline tiktoken
    # try/except with a 1.3-tokens-per-word fallback; the canonical
    # helper uses chars/3.5 which is slightly more accurate on mixed
    # content but produces materially similar Spark cost estimates.
    from core.token_utils import count_tokens_for_text
    token_count = max(1, count_tokens_for_text(prompt, model_name))

    spark_cost = max(1, int((token_count / 1000) * cost_per_1k))
    return spark_cost


# ── Goal budget (row-lock atomic deduction) ──────────────────────────

def check_goal_budget(goal_id: Optional[str],
                      estimated_cost: int) -> Tuple[bool, int, str]:
    """Check and reserve Spark budget for a goal (atomic row lock).

    Extracted from speculative_dispatcher._check_and_reserve_budget().
    Returns: (allowed, remaining_budget, reason)

    TTL cache (``_BUDGET_CACHE_TTL_S``) breaks the daemon-tick storm —
    repeated calls for the same goal within the window return the cached
    tuple without hitting the DB.  Bounds under-counting at one
    un-deducted hit per goal per window; the only writer to
    ``goal.spark_spent`` is this function, so cache freshness is
    self-consistent.
    """
    if not goal_id:
        return True, -1, 'no_goal_constraint'

    # ── Cache fast-path ────────────────────────────────────────────────
    now = time.time()
    with _budget_cache_lock:
        entry = _budget_cache.get(goal_id)
    if entry is not None:
        cached_ts, cached_result = entry
        if (now - cached_ts) < _BUDGET_CACHE_TTL_S:
            cached_allowed, cached_remaining, _ = cached_result
            # Only honor the cache when the cached remaining still covers
            # the current estimated_cost (cost varies per prompt — the
            # check the caller actually cares about is "can I afford
            # THIS one").  Denied results stay denied for the window;
            # allowed results stay allowed only if remaining headroom
            # still covers the new cost.
            if not cached_allowed:
                return cached_result
            if cached_remaining == -1 or cached_remaining >= estimated_cost:
                return cached_result

    try:
        from integrations.social.models import get_db, AgentGoal
        db = get_db()
        try:
            goal = db.query(AgentGoal).filter_by(
                id=goal_id).with_for_update().first()
            if not goal:
                result = (True, -1, 'goal_not_found')
                with _budget_cache_lock:
                    _budget_cache[goal_id] = (now, result)
                return result

            budget = goal.spark_budget or 0
            spent = goal.spark_spent or 0
            remaining = budget - spent

            if remaining < estimated_cost:
                db.rollback()
                result = (False, remaining,
                          f'insufficient_budget ({remaining} < {estimated_cost})')
                with _budget_cache_lock:
                    _budget_cache[goal_id] = (now, result)
                return result

            goal.spark_spent = spent + estimated_cost
            db.commit()
            result = (True, remaining - estimated_cost, 'budget_reserved')
            with _budget_cache_lock:
                _budget_cache[goal_id] = (now, result)
            return result
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"Budget check unavailable: {e}")
        return True, -1, 'budget_system_unavailable'


def charge_goal_work_completed(prompt_id, actions_completed: int = 1) -> bool:
    """Meter COMPLETED work into the goal's spark ledger.

    Steward decision 2026-06-10 (option (a), with EARNED-spark attribution to
    layer on later): local compute stays free at DISPATCH (estimate_llm_cost_
    spark prices local work at 0 so the budget gate never blocks free local
    dispatches), but once a flow has ACTUALLY run to completion the work is
    charged here — so ``goal.spark_spent`` rises only on work genuinely done,
    and the daemon's spark-only completion gate closes goals on real
    transacted spark. Charging at dispatch instead would re-create the
    completed-on-dispatch dashboard lie (reserve happens before work runs).

    Charge = max(1, actions_completed), clamped to remaining budget. A
    budget-starved goal records nothing -> stays incomplete -> the existing
    noop-pause surfaces it (topping up spark_budget is the steward lever).
    Resolves the goal by its stamped prompt_id (agent_daemon stamps
    dispatch.prompt_id_for_goal at dispatch). Never raises — called from the
    recipe pipeline, which must not break on accounting failures.
    """
    if prompt_id is None:
        return False
    try:
        from integrations.social.models import get_db, AgentGoal
        amount = max(1, int(actions_completed or 1))
        db = get_db()
        try:
            goal = (db.query(AgentGoal)
                    .filter(AgentGoal.prompt_id == str(prompt_id),
                            AgentGoal.status == 'active')
                    .with_for_update()
                    .first())
            if not goal:
                return False
            budget = goal.spark_budget or 0
            spent = goal.spark_spent or 0
            charge = min(amount, max(0, budget - spent))
            if charge <= 0:
                logger.info(
                    f"Completed-work charge skipped for goal {goal.id}: "
                    f"budget exhausted ({spent}/{budget}) — top up "
                    f"spark_budget to let this goal complete")
                db.rollback()
                return False
            goal.spark_spent = spent + charge
            db.commit()
            invalidate_goal_budget_cache(str(goal.id))
            logger.info(
                f"Spark charged on COMPLETED work: goal={goal.id} +{charge} "
                f"(actions={actions_completed}, spent={spent + charge}/{budget})")
            return True
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"completed-work spark charge unavailable: {e}")
        return False


def invalidate_goal_budget_cache(goal_id: Optional[str] = None) -> None:
    """Clear the budget-check TTL cache.

    Call this when the goal's spark_budget changes via a non-daemon
    path (admin top-up, manual goal edit, scheduled budget reset).
    Keeps the daemon's cache from holding a stale 'denied' verdict
    after a top-up.  ``goal_id=None`` clears every entry.
    """
    with _budget_cache_lock:
        if goal_id is None:
            _budget_cache.clear()
        else:
            _budget_cache.pop(goal_id, None)


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

# The default callers passed before the configured model became the default
# (agent_daemon still sends it): it means "the model this node calls", never
# literally gpt-4o.
_LEGACY_DEFAULT_MODEL = 'gpt-4o'


def _resolve_model_name(model_name: str) -> str:
    """Resolve the effective model name for cost estimation.

    An explicit name is trusted.  No name (or the legacy 'gpt-4o' default)
    means the ONE configured LLM (core.autogen_config): its model name when
    an API is configured, else the local model, which prices at 0 Spark.
    Nothing here picks a model of its own.
    """
    if model_name and model_name != _LEGACY_DEFAULT_MODEL:
        return model_name

    from core.autogen_config import resolve_llm_backend
    kind, entry = resolve_llm_backend()
    if kind == 'api':
        return entry.get('model') or ''

    # Local kind: the configured local model name when known, else 'llama',
    # which maps to 0 Spark in _MODEL_COST_MAP.
    return os.environ.get('HEVOLVE_LOCAL_LLM_MODEL', '') or 'llama'


def pause_goal_for_budget(goal_id: Optional[str], reason: str) -> bool:
    """Auto-pause a goal whose budget can no longer cover its next dispatch.

    The single home for that decision.  agent_daemon's read-only pre-check and
    pre_dispatch_budget_gate both land here rather than each mutating the row
    their own way.

    Honours ``never_pause`` in the goal's config.  That flag has existed since
    goal_seeding.py:1326 set it on Guardian Convergence and was, until this
    function, read by nothing at all — so a goal declaring itself unpausable
    could still be paused by any code that felt like it.  A never_pause goal
    that runs out of budget is left active and logged loudly instead: it is an
    operator problem, not something to silence by pausing the goal against its
    own declaration.

    Returns True if the goal was actually paused.
    """
    if not goal_id:
        return False
    try:
        # db_session is the canonical write path in models.py: commits on a
        # clean exit, ROLLS BACK on exception, always closes.  Hand-rolling
        # get_db/commit/close here (as an earlier draft of this function did)
        # drops the rollback, which leaves a failed write sitting in the
        # session until close discards it.
        from integrations.social.models import db_session, AgentGoal
        with db_session() as db:
            goal = db.query(AgentGoal).filter_by(id=goal_id).first()
            return apply_budget_pause(goal, reason)
    except Exception as e:
        logger.warning("Could not auto-pause goal %s after budget block: %s",
                       goal_id, e)
        return False


def apply_budget_pause(goal, reason: str) -> bool:
    """Mutate an already-loaded goal row. THE CALLER OWNS THE COMMIT.

    Split from pause_goal_for_budget on purpose. agent_daemon holds one session
    open across its whole dispatch loop and commits once at the end, so it must
    mutate its OWN object: giving it a helper that opens a second session and
    commits mid-loop would add a write transaction while the daemon holds a
    read transaction, on the same SQLite file whose lock contention is what
    started this investigation. Callers with no session (the dispatch gate) get
    the wrapper above; callers with one get this. One decision, two adapters,
    no second transaction.

    Returns True if the goal was actually transitioned.
    """
    from datetime import datetime
    if goal is None or goal.status != 'active':
        return False

    cfg = goal.config_json or {}
    if cfg.get('never_pause'):
        logger.warning(
            "Goal %s is out of budget (%s) but declares never_pause, so it "
            "stays active and will keep being refused. Top up spark_budget "
            "or clear never_pause.", goal.id, reason)
        return False

    goal.status = 'paused'
    cfg['pause_reason'] = (
        f'Auto-paused: budget gate blocked. Reason: {reason}')
    cfg['paused_at'] = datetime.utcnow().isoformat()
    goal.config_json = cfg
    # WARNING, not info: this is the state transition that explains why a goal
    # stopped making progress, and the hevolve loggers run at WARNING in
    # production, where INFO is invisible. A retention sweep that logged its
    # own success at INFO ran unseen on central for exactly this reason.
    logger.warning("Goal %s AUTO-PAUSED by budget gate: %s", goal.id, reason)
    return True


def pre_dispatch_budget_gate(goal_id: Optional[str],
                             prompt: str,
                             model_name: str = '') -> Tuple[bool, str]:
    """Combined pre-dispatch budget gate.

    1. Resolve effective model name (local vs cloud)
    2. Estimate LLM cost
    3. Check goal budget (atomic deduction)
    4. Check platform affordability (cached)

    Returns: (allowed, reason)
    """
    model_name = _resolve_model_name(model_name)
    estimated_cost = estimate_llm_cost_spark(prompt, model_name)

    # Goal-level budget
    allowed, remaining, reason = check_goal_budget(goal_id, estimated_cost)
    if not allowed:
        logger.warning(f"Budget gate BLOCKED: goal={goal_id}, {reason}")
        # A goal that cannot afford its next dispatch cannot afford the one
        # after either, until a HUMAN acts: nothing in the daemon path raises
        # spark_budget.  Top-up is real but operator-driven — see
        # invalidate_goal_budget_cache ("admin top-up, manual goal edit,
        # scheduled budget reset") and charge_goal_work_completed, which calls
        # topping up "the steward lever".  An earlier draft of this comment
        # claimed no top-up path existed at all; that was wrong, and the
        # distinction matters because it is what makes pausing safe rather
        # than terminal.
        #
        # Leaving it 'active' makes the row lie — the daemon keeps selecting
        # it, this gate keeps refusing it, and an operator reading `status`
        # sees a healthy goal.  The cost of pausing is that a top-up alone no
        # longer resumes it; someone has to un-pause.  That is the right
        # trade: a paused row with a reason is visible, an active row that can
        # never dispatch is not.
        #
        # The pause is NOT performed here, deliberately.  This gate's only
        # production caller is dispatch.dispatch_goal, and dispatch_goal is
        # itself called from inside agent_daemon's and coding_daemon's dispatch
        # loops, which hold ONE session open across the whole loop (agent_daemon
        # 1021 -> its single commit at 1712).  Opening a second connection and
        # committing from in here would put a write inside that window, and a
        # long-lived reader is precisely what stops SQLite from checkpointing
        # the WAL — the root cause of the lock storm this whole investigation
        # started from.  An earlier draft of this function did exactly that.
        #
        # So the transition stays with the caller that already owns a session:
        # agent_daemon's pre-check calls apply_budget_pause(goal, reason) on its
        # own object, costing no extra transaction.  pause_goal_for_budget
        # remains for a genuinely session-less caller, and is not used on the
        # hot path.
        #
        # Measured on central 2026-09-01: goal 917cc152 sat 'active' with 2
        # spark left against an 11-spark estimate, re-blocked on every pass,
        # while self_heal/self_build/code_evolution sat at exactly 0 left.
        # agent_daemon already auto-pauses in its own read-only pre-check, but
        # that check estimates cost from a different prompt than the real
        # dispatch, so goals in the gap between the two estimates are refused
        # here and paused on the daemon's NEXT tick, once its own pre-check
        # sees the same shortfall.  One tick later, and no nested write.
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


def meter_llm_call(model: str, tokens_in: int, tokens_out: int,
                   task_source: str = 'own', goal_id: str = None,
                   requester_node_id: str = None) -> Optional[str]:
    """Meter ONE completed LLM call through record_metered_usage.

    Derives the two arguments callers could not supply correctly: this node's
    node_id, and cost_per_1k in USD from the ONE price source (spark_per_1k,
    0 for local and free-tier models, divided by HEVOLVE_SPARK_PER_USD, the
    same rate record_metered_usage converts back with). Both production
    callers had passed keywords record_metered_usage does not accept
    (user_id/model/prompt_tokens/... and provider/model/tokens/...), so every
    call raised TypeError: the coding adapter lost every completion to it and
    the SDK proxy swallowed it and metered nothing (hevolveai Master 11.435
    S1/S2). task_source is 'own' | 'hive' | 'idle' (MeteredAPIUsage column);
    anything but 'own' is settled by the revenue aggregator.

    Never raises: metering must not break the call it meters.
    """
    try:
        node_id = _this_node_id()
        if _is_local_model():
            usd_per_1k = 0.0
        else:
            spark_per_usd = float(os.environ.get('HEVOLVE_SPARK_PER_USD', '100') or 100)
            usd_per_1k = spark_per_1k(_resolve_model_name(model)) / max(spark_per_usd, 1e-9)
        return record_metered_usage(
            node_id=node_id or 'local', model_id=model or 'unknown',
            task_source=task_source, tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0), cost_per_1k=usd_per_1k,
            goal_id=goal_id, requester_node_id=requester_node_id)
    except Exception as e:
        logger.warning(f"LLM metering failed (the call itself is unaffected): {e}")
        return None


def _this_node_id() -> str:
    """This node's id in the domain PeerNode rows are keyed by.

    HEVOLVE_NODE_ID when the operator set it (the advertiser's peer_id honours
    the same override), else the gossip id from its canonical source,
    SyncEngine.canonical_node_id.  The public-key prefix this used to fall
    back to is a different domain: no PeerNode row carries it, so a metered
    row's operator lookup could never succeed.
    """
    node_id = (os.environ.get('HEVOLVE_NODE_ID') or '').strip()
    if node_id and node_id.lower() != 'local':
        return node_id
    try:
        from integrations.social.sync_engine import SyncEngine
        return SyncEngine.canonical_node_id() or ''
    except Exception:
        return ''


def metered_usage_by_model(db) -> list:
    """Calls and tokens per model over every MeteredAPIUsage row.

    /api/gateway/metering read MeteredAPIUsage.provider and .tokens_used,
    neither of which exists on either definition of the table, so every call
    answered 500.  A row records the model, not a provider; the key stays
    'provider' for the response shape the route always had.
    """
    from sqlalchemy import func as sa_func
    from integrations.social.models import MeteredAPIUsage
    rows = db.query(
        MeteredAPIUsage.model_id,
        sa_func.sum(sa_func.coalesce(MeteredAPIUsage.tokens_in, 0)
                    + sa_func.coalesce(MeteredAPIUsage.tokens_out, 0)),
        sa_func.count(MeteredAPIUsage.id),
    ).group_by(MeteredAPIUsage.model_id).all()
    return [{'provider': r[0], 'total_tokens': int(r[1] or 0), 'calls': int(r[2])}
            for r in rows]


# ── Remote compute: each side records its own half on its own node ───
#
# Owner rulings 2026-09-26.  (a) What is metered is work that goes to "hive
# nodes usage that's not their node", tracked inside HARTOS.  (b) The rate is
# "proportinal to compute spent and earned": ONE measured quantity per
# exchange is debited from the requester and credited to the operator of the
# node that served it (before the 90/9/1 split, which revenue_aggregator
# owns).  (c) "for local person'a work zero spark earned": the requester's own
# node, a node proven SAME_USER, or a local model costs 0 and earns 0.
#
# No node writes another node's wallet.  The requesting node debits its
# person when a remote result returns (charge_remote_compute); the serving
# node credits its operator when it serves someone who is not its operator
# (credit_served_compute).  Both halves measure the exchange with
# exchange_tokens from the same request and response, so each side reaches
# the same number without either one trusting the other's report.

REMOTE_COMPUTE_TASK_SOURCE = 'hive_compute'          # requester's debit rows
SERVED_COMPUTE_TASK_SOURCE = 'hive_compute_served'   # server's credit rows
# Ledger rows, complete when written: settlement never pays them.
COMPUTE_LEDGER_TASK_SOURCES = frozenset({
    REMOTE_COMPUTE_TASK_SOURCE, SERVED_COMPUTE_TASK_SOURCE})

# Who asked, on a request one node sends another for compute.  Ids travel
# as-is to nodes the user does not own (19d4c5b02: only content is scrubbed);
# the serving node needs them to tell its own operator from someone else.
REQUESTER_USER_HEADER = 'X-Hart-Requester-User'
REQUESTER_NODE_HEADER = 'X-Hart-Requester-Node'


def spark_per_1k_compute_tokens() -> float:
    """Spark per 1K tokens of compute run on a node the requester does not own.

    Composed from the two conversions HARTOS already has, no new number:
    tokens to GPU time is hosting_reward_service.GPU_SECONDS_PER_1K_TOKENS
    (what gpu_hours_served is credited with), and GPU time to Spark is the
    wallet's own award table, AWARD_TABLE['compute_hour'] (Spark per compute
    hour lent).  spark_per_1k() is NOT this: it prices a paid API by model
    family and says 0 for every local family, so a peer's Qwen would be free.
    """
    from integrations.social.hosting_reward_service import GPU_SECONDS_PER_1K_TOKENS
    from integrations.social.resonance_engine import AWARD_TABLE
    spark_per_hour = float(AWARD_TABLE['compute_hour']['spark'])
    return GPU_SECONDS_PER_1K_TOKENS / 3600.0 * spark_per_hour


def exchange_tokens(prompt, response, usage=None,
                    max_tokens=None) -> Tuple[int, int]:
    """(tokens_in, tokens_out) one compute exchange is measured at.

    The prompt is counted locally from its text (core.token_utils, the one
    counter), and that count caps what the serving side claims for it; the
    completion is capped at the request's ``max_tokens``.  A ``usage`` block
    can only lower the measure, never raise it: an inflated prompt_tokens
    moved a requester from 1000 to 100 Spark for a 1-token exchange.
    Without a usage block the completion is counted from the response text.
    Both nodes call this with the same request and response, so both reach
    the same number.
    """
    from core.token_utils import count_tokens_for_text
    counted_in = count_tokens_for_text(prompt if isinstance(prompt, str) else '')
    counted_out = count_tokens_for_text(response if isinstance(response, str) else '')
    cap_out = None
    try:
        if max_tokens is not None and int(max_tokens) >= 0:
            cap_out = int(max_tokens)
    except (TypeError, ValueError):
        cap_out = None
    tin, tout = counted_in, counted_out
    usage = usage if isinstance(usage, dict) else {}
    try:
        claim_in = int(usage.get('prompt_tokens') or 0)
        claim_out = int(usage.get('completion_tokens') or 0)
    except (TypeError, ValueError):
        claim_in = claim_out = 0
    if claim_in > 0 or claim_out > 0:
        tin = min(max(0, claim_in), counted_in)
        tout = max(0, claim_out)
    if cap_out is not None:
        tout = min(tout, cap_out)
    return tin, tout


def completion_exchange(request_body, response_body) -> Tuple[int, int]:
    """exchange_tokens for one OpenAI-style /chat/completions exchange: the
    prompt is the request's message text, the response the first choice.
    The hive expert's requester and its server both measure through here."""
    request_body = request_body if isinstance(request_body, dict) else {}
    response_body = response_body if isinstance(response_body, dict) else {}
    prompt = '\n'.join(
        m.get('content') for m in (request_body.get('messages') or [])
        if isinstance(m, dict) and isinstance(m.get('content'), str))
    choices = response_body.get('choices') or []
    msg = (choices[0] or {}).get('message') or {} if choices else {}
    content = msg.get('content') if isinstance(msg, dict) else ''
    return exchange_tokens(prompt, content or '', response_body.get('usage'),
                           request_body.get('max_tokens'))


def _node_is_users(user_id: str, node_id: str, operator_id: str) -> bool:
    """Is ``node_id``, operated by ``operator_id``, ``user_id``'s own node?

    Its operator is the user, or this node holds a link to it that
    PeerLink.owned_by proves is the user's (SAME_USER, the rule of 19d4c5b02).
    No second ownership rule lives here.  The requester asks it of the node
    that served it; the server asks it of the requesting node.
    """
    if operator_id and operator_id == user_id:
        return True
    if not node_id:
        return False
    try:
        from core.peer_link.link_manager import get_link_manager
        link = get_link_manager().get_link(node_id)
    except Exception:
        link = None
    return bool(link is not None and link.owned_by(user_id))


def _lock_wallet(db, user_id: str):
    """The user's wallet row, created if missing and locked for this
    transaction (SELECT ... FOR UPDATE; SQLite serializes writers anyway), so
    concurrent exchanges for one person read the carry one at a time."""
    from integrations.social.models import ResonanceWallet
    from integrations.social.resonance_engine import ResonanceService
    ResonanceService.get_or_create_wallet(db, user_id)
    return db.query(ResonanceWallet).filter_by(
        user_id=user_id).with_for_update().first()


def _accrue(db, task_source: str, requester: str, operator: str,
            node_id: str, requester_node: str, tin: int, tout: int,
            model_id: str):
    """Write one exchange row and return (row, whole Spark now due).

    The exact Spark owed is the pair's running total (requester, operator) on
    this node minus the whole Spark already moved; its whole part moves now
    and the fraction waits for the next exchange.  The caller holds the
    wallet lock that serializes this read.
    """
    import math
    from sqlalchemy import func as sa_func
    from integrations.social.models import MeteredAPIUsage
    from integrations.agent_engine.revenue_aggregator import SPARK_PER_USD
    rate = spark_per_1k_compute_tokens()
    exact_prior, moved_prior = db.query(
        sa_func.coalesce(sa_func.sum(
            (sa_func.coalesce(MeteredAPIUsage.tokens_in, 0)
             + sa_func.coalesce(MeteredAPIUsage.tokens_out, 0))
            * MeteredAPIUsage.cost_per_1k_tokens / 1000.0), 0.0),
        sa_func.coalesce(sa_func.sum(MeteredAPIUsage.estimated_spark_cost), 0),
    ).filter(
        MeteredAPIUsage.task_source == task_source,
        MeteredAPIUsage.requester_user_id == requester,
        MeteredAPIUsage.operator_id == operator,
    ).one()
    owed = (float(exact_prior or 0.0) - float(moved_prior or 0)
            + (tin + tout) / 1000.0 * rate)
    amount = max(0, int(math.floor(owed + 1e-9)))
    row = MeteredAPIUsage(
        node_id=node_id,
        operator_id=operator,
        model_id=(model_id or 'remote')[:100],
        task_source=task_source,
        requester_node_id=requester_node or None,
        requester_user_id=requester,
        tokens_in=tin,
        tokens_out=tout,
        cost_per_1k_tokens=rate,
        estimated_spark_cost=amount,
        actual_usd_cost=amount / float(SPARK_PER_USD or 100),
        settlement_status='carried',
    )
    db.add(row)
    db.flush()
    return row, amount


def _ledger_ready() -> bool:
    from integrations.social.models import MeteredAPIUsage
    if hasattr(MeteredAPIUsage, 'requester_user_id'):
        return True
    logger.warning(
        "Remote compute not recorded: MeteredAPIUsage has no requester_user_id "
        "on this install (hevolve_database needs the column)")
    return False


def charge_remote_compute(user_id, serving_node_id, tokens_in, tokens_out,
                          source: str, ref_id: str = '',
                          model_id: str = '') -> int:
    """The requester's half: debit its person for COMPLETED compute on a node
    they do not own.  Pass tokens measured by exchange_tokens.  Returns the
    whole Spark debited (0 when nothing moved).  Never raises.

    - Own node or SAME_USER node, no requester, nothing measured: 0, no row.
    - Serving node's operator unknown: 0, no row (no one would earn it).
    - Otherwise one row (task_source 'hive_compute', node_id = the serving
      node, operator_id = its operator).  'debited' when whole Spark moved,
      'carried' when only a fraction accrued.
    - Insufficient Spark: the work already ran and nothing blocks the person
      (owner: no friction).  spend_spark is all or nothing, so nothing is
      debited; the row is 'unfunded' and its amount is not billed again.
    """
    user_id = str(user_id or '')
    serving_node_id = str(serving_node_id or '')
    tin = max(0, int(tokens_in or 0))
    tout = max(0, int(tokens_out or 0))
    if not user_id or not serving_node_id or (tin + tout) <= 0:
        return 0
    try:
        from integrations.social.models import db_session, PeerNode
        from integrations.social.resonance_engine import ResonanceService
        if not _ledger_ready():
            return 0
        with db_session() as db:
            peer = db.query(PeerNode).filter_by(node_id=serving_node_id).first()
            operator_id = str(peer.node_operator_id) if (
                peer is not None and peer.node_operator_id) else ''
            if _node_is_users(user_id, serving_node_id, operator_id):
                return 0
            if not operator_id:
                logger.info(
                    "Remote compute on %s not charged: no operator known for "
                    "that node, so no one would earn it", serving_node_id)
                return 0
            _lock_wallet(db, user_id)
            row, amount = _accrue(
                db, REMOTE_COMPUTE_TASK_SOURCE, user_id, operator_id,
                serving_node_id, _this_node_id(), tin, tout, model_id or source)
            if amount <= 0:
                return 0
            ok, balance = ResonanceService.spend_spark(
                db, user_id, amount, 'hive_compute_spent', row.id,
                f'Compute on {serving_node_id} ({source} {ref_id})'.strip())
            if not ok:
                row.settlement_status = 'unfunded'
                logger.info(
                    "Remote compute on %s: %s has %s Spark, %d owed; recorded "
                    "unfunded", serving_node_id, user_id, balance, amount)
                return 0
            row.settlement_status = 'debited'
            return amount
    except Exception as e:
        logger.warning("Remote compute charge failed (the work itself is "
                       "unaffected): %s", e)
        return 0


def this_node_operator(db) -> str:
    """Who operates this node: its PeerNode row's operator, else the user
    this node proves SAME_USER links over (link.provable_user_id)."""
    from integrations.social.models import PeerNode
    node_id = _this_node_id()
    if node_id:
        row = db.query(PeerNode).filter_by(node_id=node_id).first()
        if row is not None and row.node_operator_id:
            return str(row.node_operator_id)
    try:
        from core.peer_link.link import provable_user_id
        return str(provable_user_id() or '')
    except Exception:
        return ''


def credit_served_compute(requester_user_id, requester_node_id, tokens_in,
                          tokens_out, source: str, ref_id: str = '',
                          model_id: str = '') -> int:
    """The serving node's half: credit its operator for compute it served to
    someone who is not its operator.  Pass tokens measured by exchange_tokens
    from the request received and the response sent.  Returns the whole
    Spark credited.  Never raises.

    - No requester named, nothing measured, operator unknown, or the
      requester is this node's operator (or proves the requesting node is
      theirs over a SAME_USER link): 0, no row.
    - Otherwise one row (task_source 'hive_compute_served', node_id = this
      node): 'credited' when whole Spark moved, 'carried' otherwise.
    """
    requester = str(requester_user_id or '')
    requester_node = str(requester_node_id or '')
    tin = max(0, int(tokens_in or 0))
    tout = max(0, int(tokens_out or 0))
    if not requester or (tin + tout) <= 0:
        return 0
    try:
        from integrations.social.models import db_session
        from integrations.social.resonance_engine import ResonanceService
        if not _ledger_ready():
            return 0
        with db_session() as db:
            operator_id = this_node_operator(db)
            if not operator_id:
                logger.info("Served compute not credited: this node has no "
                            "known operator")
                return 0
            # The requester's own node served them: its operator is the
            # requester, or the requesting node is linked SAME_USER to this
            # node's user and the requester is that user.
            if _node_is_users(requester, requester_node, operator_id):
                return 0
            _lock_wallet(db, operator_id)
            row, amount = _accrue(
                db, SERVED_COMPUTE_TASK_SOURCE, requester, operator_id,
                _this_node_id(), requester_node, tin, tout, model_id or source)
            if amount <= 0:
                return 0
            ResonanceService.award_spark(
                db, operator_id, amount, 'hive_compute_earned', row.id,
                f'Compute served to {requester} ({source} {ref_id})'.strip())
            row.settlement_status = 'credited'
            return amount
    except Exception as e:
        logger.warning("Served compute credit failed (the work itself is "
                       "unaffected): %s", e)
        return 0


def credit_served_completion(headers, request_body, response_body) -> int:
    """The serving half of a hive expert exchange, for the node's
    /v1/chat/completions route: who asked comes from the requester headers,
    the measure from completion_exchange, the same one the requester's
    SpeculativeDispatcher._charge_hive_expert debits with.  A reply with no
    content did not complete and earns nothing; a call without the headers
    (an SDK client, not a hive peer) earns nothing here.  Never raises."""
    try:
        get = getattr(headers, 'get', None)
        requester = (get(REQUESTER_USER_HEADER) if get else '') or ''
        if not requester:
            return 0
        body = response_body if isinstance(response_body, dict) else {}
        choices = body.get('choices') or []
        msg = ((choices[0] or {}).get('message') or {}) if choices else {}
        if not (isinstance(msg, dict) and msg.get('content')):
            return 0
        tin, tout = completion_exchange(request_body, body)
        model = (request_body or {}).get('model') if isinstance(
            request_body, dict) else ''
        return credit_served_compute(
            requester, get(REQUESTER_NODE_HEADER) or '', tin, tout,
            source='hive_expert', model_id=str(model or 'hive_expert'))
    except Exception as e:
        logger.warning("Served completion credit skipped: %s", e)
        return 0
