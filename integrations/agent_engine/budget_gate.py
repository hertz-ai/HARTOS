"""
Budget Gate — pre-dispatch spend control for LLM calls and agent goals.

Fail-closed economics: don't spend what you don't have.

Functions:
  estimate_llm_cost_spark(prompt, model_name) — token-based cost estimate
  check_goal_budget(goal_id, estimated_cost, holder) — atomic row-lock
                                       reservation, held under its holder
  release_goal_reservations(goal_id, holder) — the holder gives its
                                       reservation back when its work ends
  check_platform_affordability() — 7-day net revenue check (cached 60s)
  pre_dispatch_budget_gate(goal_id, prompt, model_name, holder) — combined
                                       gate, reserving under holder
  reservation_holder(kind, ident) — the one naming rule for holders
  goal_spark_now(goal_id, row_spark_spent) — spark spent as committed now,
                                       and the part of it spent on work

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
# changed (release_goal_reservations and charge_goal_work_completed clear
# the goal's entry; an admin edit through GoalManager.update_goal does not,
# so a top-up there shows after the window).  Burst
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
    'claude': 4,    # Anthropic, the expert tier on central (2026-10-12): the
                    # gpt-4o class, not the unknown-model default of 2
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
                      estimated_cost: int,
                      holder: Optional[str] = None) -> Tuple[bool, int, str]:
    """Check and reserve Spark budget for a goal (atomic row lock).

    Extracted from speculative_dispatcher._check_and_reserve_budget().
    Returns: (allowed, remaining_budget, reason)

    TTL cache (``_BUDGET_CACHE_TTL_S``) breaks the daemon-tick storm —
    repeated calls for the same goal within the window return the cached
    tuple without hitting the DB.  Bounds under-counting at one
    un-deducted hit per goal per window.  The other writers of
    ``goal.spark_spent`` (release_goal_reservations,
    charge_goal_work_completed) clear the goal's cache entry.

    A deduction is held under ``holder`` (held_reservation) until that
    holder gives it back with release_goal_reservations when its work ends;
    a cached answer deducts nothing and holds nothing.
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
            _hold(goal_id, estimated_cost, holder)
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


# Spark check_goal_budget reserved and nobody has given back yet, per goal.
# The reservation is a pre-check: it stops a goal from dispatching past its
# budget while the turn runs.  It is not work, and the steward's rule
# (charge_goal_work_completed) is that spark_spent rises only on work done.
# Each reservation is held under its HOLDER, the code that made it, and that
# code gives it back when its own work ends: dispatch_goal when its turn
# returns, the speculative expert when its background task ends.  A
# goal-wide release gave back the expert's reservation while the expert
# still ran, and any caller's reservation to any other (review of
# e8b6bf675, Q4 and P6).  Kept in process: the holder releases in the
# process that reserved.  A reservation made in another process (the stdio
# MCP server's dispatch_goal) is not in this ledger, so this process reads
# it as spent on work until it is given back.  One made after a daemon's
# snapshot and still held at its settle completes the goal on spark nobody
# earned (measured, review of 8b32e97b6, F2); one given back in between
# reads as less work.  Task #175.
#
# Two keys under a goal are not holders.  _RELEASING is spark a holder is
# giving back whose write has not committed yet: still held, or a settle
# running meanwhile reads the reservation as work (review of 4bb3bbae0,
# finding 2).  _UNRELEASED is spark whose write failed: every holder is
# unique per call, so it would never be released again under its own name;
# the goal's next release, by any holder, retries it (finding 1).
_held_reservations: Dict[str, Dict[str, int]] = {}
_held_lock = threading.Lock()
_RELEASING = '\x00releasing'
_UNRELEASED = '\x00unreleased'


def reservation_holder(kind: str, ident: str) -> str:
    """The name a reservation is held under: what reserved it, and which
    call of it.  dispatch_goal names one per turn, the speculative expert
    one per speculation."""
    return f'{kind}:{ident}'


def _hold(goal_id, amount: int, holder: Optional[str] = None) -> None:
    if not goal_id or not amount or amount <= 0:
        return
    key, who = str(goal_id), holder or ''
    with _held_lock:
        mine = _held_reservations.setdefault(key, {})
        mine[who] = mine.get(who, 0) + int(amount)


def held_reservation(goal_id) -> int:
    """Spark reserved for ``goal_id`` and not yet released, by any holder."""
    with _held_lock:
        return sum(_held_reservations.get(str(goal_id), {}).values())


def spark_spent_on_work(goal_id, spark_spent) -> int:
    """The goal's spark_spent without what is still held as a reservation.

    The one reading of "spark spent" for the completion gate: both the
    snapshot a daemon takes before a dispatch and the figure it settles on
    go through here.  A hold left by a failed release otherwise inflated the
    snapshot, and the next dispatch's real work read as a noop (review of
    e8b6bf675, P3).
    """
    return max(0, (spark_spent or 0) - held_reservation(goal_id))


def goal_spark_now(goal_id, row_spark_spent=None) -> Tuple[int, int]:
    """(spark_spent as committed now, the part of it spent on work).

    The column is read in a short session of its own, then the ledger.  A
    row loaded earlier goes stale: the agent daemon loads every goal when
    its tick starts, and a holder giving spark back meanwhile lowers the
    column in another session while the ledger already holds nothing, so
    the stale figure read given-back spark as spent.  The budget pre-check
    paused a goal on '12 < 16' with 16 left, and the snapshot made 2 spark
    of real work a noop (review of 8b32e97b6, F1, measured).  A release
    that lands between the two reads can still move this reading by its
    amount, either way (task #175).

    ``row_spark_spent`` (the caller's row) stands in when the column cannot
    be read.
    """
    spent = row_spark_spent
    try:
        from integrations.social.models import get_db, AgentGoal
        db = get_db()
        try:
            value = (db.query(AgentGoal.spark_spent)
                     .filter(AgentGoal.id == str(goal_id)).scalar())
        finally:
            db.close()
        # Anything but an integer (no row, a NULL column) is no reading.
        if isinstance(value, int) and not isinstance(value, bool):
            spent = value
    except Exception as e:
        logger.debug("Spark for goal %s not read fresh, using the loaded "
                     "row: %s", goal_id, e)
    spent = spent or 0
    return spent, spark_spent_on_work(goal_id, spent)


def release_goal_reservations(goal_id, holder: Optional[str] = None) -> int:
    """Give back the spark ``holder`` reserved for ``goal_id``.

    Called by the holder when its work ends, whatever the outcome; other
    holders' reservations on the goal stay.  Without it the reservation stood
    in for work: the completion gate read it as "spark spent this dispatch"
    (18 goals completed on central 2026-10-17 whose every turn had failed),
    and budgets drained by the estimate on every failed or waiting dispatch
    until the gate paused the goal ("insufficient_budget (0 < 4)").  Spark
    charged for completed work meanwhile (charge_goal_work_completed) stays.

    Its own short transaction, row-locked like the reservation, so a
    concurrent work charge is never overwritten.  Never takes spark_spent
    below zero.  The holder's hold moves to _RELEASING before the write, so
    two releases at once give it back once (review of e8b6bf675, P5), and it
    still counts as held until the write commits.  Spark an earlier release
    could not write (_UNRELEASED) is retried with this one.  On a database
    error the whole amount is parked for the goal's next release and 0 is
    returned.  A hold whose goal row is gone is dropped and 0 returned:
    nothing was given back.  Returns the spark given back.

    Between the commit and the ledger update spark_spent is already lowered
    while the spark still counts as held.  A settle reading then sees less
    work than was done (a noop); a snapshot reading then is low, and the
    next settle can count the difference as work (task #175).
    """
    key, who = str(goal_id), holder or ''
    with _held_lock:
        mine = _held_reservations.get(key)
        if not mine:
            return 0
        amount = mine.pop(who, 0) + mine.pop(_UNRELEASED, 0)
        if amount <= 0:
            return 0
        mine[_RELEASING] = mine.get(_RELEASING, 0) + amount
    written, goal = False, None
    try:
        from integrations.social.models import get_db, AgentGoal
        db = get_db()
        try:
            goal = db.query(AgentGoal).filter_by(
                id=goal_id).with_for_update().first()
            if goal is not None:
                goal.spark_spent = max(0, (goal.spark_spent or 0) - amount)
                db.commit()
            written = True
        finally:
            db.close()
    except Exception as e:
        if written:
            # The commit stood; only closing the session failed.  The spark
            # is back, so it is not parked to be given back a second time.
            logger.debug("Session close failed after giving back spark for "
                         "goal %s: %s", goal_id, e)
        else:
            logger.warning("Spark reserved for goal %s not given back yet "
                           "(kept for the goal's next release): %s",
                           goal_id, e)
    finally:
        # Also on an interrupt (KeyboardInterrupt, SystemExit): an amount
        # left under _RELEASING would count as held forever and never be
        # retried (review of 25d31fc1d, item 2).
        with _held_lock:
            mine = _held_reservations.setdefault(key, {})
            left = mine.get(_RELEASING, 0) - amount
            if left > 0:
                mine[_RELEASING] = left
            else:
                mine.pop(_RELEASING, None)
            if not written:
                mine[_UNRELEASED] = mine.get(_UNRELEASED, 0) + amount
            if not mine:
                _held_reservations.pop(key, None)
    if not written:
        return 0
    invalidate_goal_budget_cache(goal_id)
    if goal_id != key:
        invalidate_goal_budget_cache(key)
    if goal is None:
        logger.info("Spark held for goal %s dropped: the goal no longer "
                    "exists", goal_id)
        return 0
    return amount


def retry_unreleased_reservations() -> int:
    """Retry giving back spark whose earlier release could not be written,
    for every goal.

    Any release on the goal retries it, but a goal waiting for held spark
    makes no new dispatch and so no new release of its own.  The agent
    daemon calls this at the start of each tick, before it opens the tick's
    long session, so the write does not wait on that session's lock.
    Returns the spark given back."""
    with _held_lock:
        goals = [k for k, mine in _held_reservations.items()
                 if mine.get(_UNRELEASED, 0) > 0]
    # _UNRELEASED is never a real holder, so each release takes only the
    # parked amount and no holder's live reservation.
    return sum(release_goal_reservations(k, holder=_UNRELEASED)
               for k in goals)


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
                             model_name: str = '',
                             holder: Optional[str] = None) -> Tuple[bool, str]:
    """Combined pre-dispatch budget gate.

    1. Resolve effective model name (local vs cloud)
    2. Estimate LLM cost
    3. Check goal budget (atomic reservation under ``holder``, who gives
       it back with release_goal_reservations when the dispatch ends, a
       refusal here included)
    4. Check platform affordability (cached)

    Returns: (allowed, reason)
    """
    model_name = _resolve_model_name(model_name)
    estimated_cost = estimate_llm_cost_spark(prompt, model_name)

    # Goal-level budget
    allowed, remaining, reason = check_goal_budget(goal_id, estimated_cost,
                                                   holder=holder)
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
            # A family with a real dollar price (Claude) is metered at it,
            # blended over this call's input and output tokens so the stored
            # actual_usd_cost is exact; every other cloud family keeps the
            # Spark-derived rate.
            blended = blended_usd_per_1k(_resolve_model_name(model), tokens_in, tokens_out)
            if blended is not None:
                usd_per_1k = blended
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


# ─── Dollar meter and the Claude day cap (owner policy 2026-10-12) ─────────
#
# "No more than $6 a day on Claude in central", and "Claude can never become
# its own blocker": the cap is enforced where the expert is OFFERED
# (ModelRegistry._offered asks expert_budget_allows), so a turn past the cap
# is served by the node's own model instead of failing.  The ledger it reads
# is MeteredAPIUsage, fed by the wire hook from every response's usage block
# (core.llm_outbound_logger._meter_response) at a real dollar price.
# Topology agnostic: no node-tier branch.  The cap only has anything to cap
# where an expert trio is configured (core.autogen_config), which is central
# by configuration; a node whose LLM is local meters at zero and writes no
# row.  The gate fails CLOSED: an unreadable ledger withholds the expert, and
# withholding is never blocking (the turn runs on the node's own model).
# Kong has no fuse on revenue-lane Claude by design; this day budget is its
# only cap.  Prices are list prices per 1k tokens; the owner can pin them.
# ONE price table and ONE pin reader, owned by core.autogen_config beside the
# expert entry whose autogen ``price`` they also fill; imported here, never a
# second copy.
from core.autogen_config import (  # noqa: E402
    EXPERT_USD_PER_1K as USD_PER_1K_BY_FAMILY, OTHER_MODEL_FAMILY, family_usd_per_1k,
    model_family,
)
CLAUDE_USD_PER_1K_IN_VAR = 'HEVOLVE_CLAUDE_USD_PER_1K_IN'      # read by family_usd_per_1k
CLAUDE_USD_PER_1K_OUT_VAR = 'HEVOLVE_CLAUDE_USD_PER_1K_OUT'
CLAUDE_BUDGET_USD_DAY_VAR = 'HEVOLVE_CLAUDE_BUDGET_USD_DAY'
DEFAULT_CLAUDE_BUDGET_USD_DAY = 6.0
CAPPED_FAMILY = 'claude'


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, '') or default)
    except (TypeError, ValueError):
        return default


def usd_price_per_1k(model_name) -> Optional[Tuple[float, float]]:
    """(input, output) dollars per 1k tokens for a model with a real price
    table row, else None (the Spark-derived rate applies).  The family and
    its (pinned) price come from the one rule in core.autogen_config."""
    family = model_family(model_name)
    if family == OTHER_MODEL_FAMILY:
        return None
    return family_usd_per_1k(family)


def blended_usd_per_1k(model_name, tokens_in, tokens_out) -> Optional[float]:
    """One cost_per_1k that makes ``(tin+tout)/1000 * rate`` equal the exact
    input-plus-output dollar cost of this call; None for an unpriced family."""
    price = usd_price_per_1k(model_name)
    if price is None:
        return None
    tin = max(0, int(tokens_in or 0))
    tout = max(0, int(tokens_out or 0))
    total = tin + tout
    if total <= 0:
        return price[0]
    return (tin * price[0] + tout * price[1]) / total


def usd_spent_since(since, family: str) -> Optional[float]:
    """Dollars this node's own calls spent on models of ``family`` (matched
    by substring, the same rule usd_price_per_1k prices by, so a row named
    'anthropic/claude-sonnet-4-5' counts) since ``since`` (UTC), from the
    metered-usage ledger.  None when the ledger cannot be read: the caller
    fails CLOSED on None (the expert is withheld, the turn runs on the
    node's own model), because withholding is not blocking and an unread
    ledger must not let the cap be overrun."""
    try:
        from sqlalchemy import func as sa_func
        from integrations.social.models import db_session, MeteredAPIUsage
        node_id = _this_node_id() or 'local'
        with db_session() as db:
            total = db.query(
                sa_func.coalesce(sa_func.sum(MeteredAPIUsage.actual_usd_cost), 0.0)
            ).filter(
                MeteredAPIUsage.node_id == node_id,
                MeteredAPIUsage.task_source == 'own',
                sa_func.lower(MeteredAPIUsage.model_id).like('%' + family.lower() + '%'),
                MeteredAPIUsage.created_at >= since,
            ).scalar()
            return float(total or 0.0)
    except Exception as e:
        logger.warning(f"usd_spent_since: ledger unreadable, the expert is withheld: {e}")
        return None


def claude_budget_usd_day() -> float:
    return max(0.0, _env_float(CLAUDE_BUDGET_USD_DAY_VAR, DEFAULT_CLAUDE_BUDGET_USD_DAY))


def claude_usd_spent_today() -> Optional[float]:
    from datetime import datetime
    day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return usd_spent_since(day_start, CAPPED_FAMILY)


def expert_budget_allows(model_name) -> Tuple[bool, str]:
    """May the expert model ``model_name`` be offered right now?

    Only the capped family is ever refused: once today's metered dollars
    reach the day budget, or while the ledger cannot be read.  A refusal is
    not an error anywhere: the selector simply does not offer the expert and
    the turn runs on the node's own model.
    """
    if model_family(model_name) != CAPPED_FAMILY:
        return True, 'not a capped family'
    cap = claude_budget_usd_day()
    spent = claude_usd_spent_today()
    if spent is None:
        return False, f'{CAPPED_FAMILY} ledger unreadable: expert withheld'
    if spent >= cap:
        return False, f'{CAPPED_FAMILY} day budget spent: ${spent:.2f} of ${cap:.2f}'
    return True, f'{CAPPED_FAMILY} day budget: ${spent:.2f} of ${cap:.2f}'


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
