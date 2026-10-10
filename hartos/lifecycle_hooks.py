"""
STATE MACHINE IMPLEMENTATION
=====================================

This module manages the ActionState state machine for tracking action lifecycle.
It also provides functions to sync ActionState with SmartLedger TaskStatus.
"""

from enum import Enum
import copy
import hashlib
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Dict, Optional, Any
from core.constants import (AGENT_MENTIONS, BOOKKEEPING_TOOLS,
                            HISTORICAL_TOOL_PLACEHOLDER, tool_reply_failed)
from core.session_cache import TTLCache

try:
    from hartos.helper import PROMPTS_DIR
except ImportError:
    PROMPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'prompts'))

logger = logging.getLogger(__name__)

# Lock protecting _ledger_registry and action_states (accessed by multiple Waitress/Gunicorn threads)
_state_lock = threading.RLock()

def _load_ledger_on_miss(user_prompt: str) -> Optional[Any]:
    """Cache-miss loader: recover ledger from persistent storage."""
    try:
        from agent_ledger.factory import get_or_create_ledger
        user_id, prompt_id = _extract_ownership_from_prompt(user_prompt)
        if user_id and prompt_id:
            return get_or_create_ledger(user_id, prompt_id)
    except Exception:
        pass
    return None

# Global ledger registry for auto-sync (TTL-bounded, auto-recovers from disk on cache miss)
_ledger_registry = TTLCache(
    ttl_seconds=28800, max_size=10000, name='ledger_registry',
    loader=_load_ledger_on_miss,
)

def register_ledger_for_session(user_prompt: str, ledger: Any):
    """Register a ledger instance for a session to enable auto-sync."""
    with _state_lock:
        _ledger_registry[user_prompt] = ledger
    logger.debug(f"Registered ledger for {user_prompt}")

def get_registered_ledger(user_prompt: str) -> Optional[Any]:
    """Get the registered ledger for a session. Auto-recovers from disk on cache miss."""
    with _state_lock:
        return _ledger_registry.get(user_prompt)


# ─── GroupChat registry (Agent Ops Console Phase B) ──────────────────────
# Mirrors _ledger_registry pattern exactly: same TTLCache module, same TTL
# (28800s = 8h), same locking discipline.  Stores autogen GroupChat
# instances per user_prompt so the dashboard's drill-down drawer can read
# `group_chat.messages` live without needing a second SSE topic or a
# parallel message store.
#
# Not loader-backed: GroupChat is in-process and ephemeral (autogen
# rebuilds it per /chat round).  Cache miss returns None; the drawer
# renders "no conversation captured" rather than fabricating a history.
#
# Populated by create_recipe.py at autogen GroupChat() instantiation
# sites (lines 2611, 2620, 3254 as of 2026-05-17).  Read by
# DashboardService.get_agent_chat_tail() in dashboard_service.py.
_groupchat_registry = TTLCache(
    ttl_seconds=28800, max_size=10000, name='groupchat_registry',
)


def register_groupchat_for_session(user_prompt: str, group_chat: Any) -> None:
    """Register an autogen GroupChat for live drill-down access.

    Idempotent: re-registering replaces the prior reference (the same
    GroupChat object is mutated across turns, but a fresh /chat may spin
    a new one — both cases want the latest reference).  No-op + debug
    log if `group_chat` is None so call sites stay safe even when
    autogen instantiation guards fail.
    """
    if group_chat is None:
        logger.debug("register_groupchat_for_session: %s got None — skipping",
                     user_prompt)
        return
    with _state_lock:
        _groupchat_registry[user_prompt] = group_chat
    logger.debug("Registered groupchat for %s", user_prompt)


def get_registered_groupchat(user_prompt: str) -> Optional[Any]:
    """Get the registered GroupChat for a session, or None on cache miss."""
    with _state_lock:
        return _groupchat_registry.get(user_prompt)


def _extract_ownership_from_prompt(user_prompt: str):
    """Extract user_id and prompt_id from the user_prompt key (format: {user_id}_{prompt_id})."""
    parts = user_prompt.split('_', 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return user_prompt, None


def _get_node_id():
    """Get this machine's hostname for task ownership — cached at module level."""
    import platform
    return platform.node()


def _auto_sync_to_ledger(user_prompt: str, action_id: int, state: 'ActionState',
                         result: Any = None):
    """Auto-sync state change to ledger if registered.

    Wires v2.0 ledger features using KNOWN stateful variables in scope:
    - Ownership: claim on IN_PROGRESS, release on terminal (uses user_prompt, hostname)
    - Heartbeat: every state change records liveness
    - Budget/SLA: checked on IN_PROGRESS entry, flagged if breached
    """
    ledger = _ledger_registry.get(user_prompt)
    if ledger is None:
        return True  # Legacy/direct callers may not own a ledger.

    task = None
    task_snapshot = None
    try:
        LedgerTaskStatus = _get_ledger_task_status()
        task_id = f"action_{action_id}"

        if task_id not in ledger.tasks:
            return True  # No ledger projection exists for this action.

        task = ledger.tasks[task_id]
        # Direct resume paths predate SmartLedger's atomic status method.
        # Keep the complete object so a rejected save also rolls back claim,
        # history, and reason mutations in memory.
        task_snapshot = copy.deepcopy(task.__dict__)

        def _rollback_task():
            task.__dict__.clear()
            task.__dict__.update(copy.deepcopy(task_snapshot))

        # Map ActionState to LedgerTaskStatus — complete 1:1 coverage
        STATE_MAP = {
            ActionState.ASSIGNED: LedgerTaskStatus.PENDING,
            ActionState.IN_PROGRESS: LedgerTaskStatus.IN_PROGRESS,
            ActionState.STATUS_VERIFICATION_REQUESTED: LedgerTaskStatus.IN_PROGRESS,
            ActionState.COMPLETED: LedgerTaskStatus.COMPLETED,
            ActionState.PENDING: LedgerTaskStatus.BLOCKED,
            ActionState.ERROR: LedgerTaskStatus.FAILED,
            ActionState.FALLBACK_REQUESTED: LedgerTaskStatus.BLOCKED,
            ActionState.FALLBACK_RECEIVED: LedgerTaskStatus.IN_PROGRESS,
            ActionState.RECIPE_REQUESTED: LedgerTaskStatus.IN_PROGRESS,
            ActionState.RECIPE_RECEIVED: LedgerTaskStatus.COMPLETED,
            ActionState.TERMINATED: LedgerTaskStatus.COMPLETED,
            # #139: a force-give-up is an HONEST failure, never COMPLETED (that was
            # the fake-success bug). A distinct terminal, re-openable for a retry.
            ActionState.GAVE_UP: LedgerTaskStatus.FAILED,
            # VLM / physical action states — still active execution
            ActionState.EXECUTING_MOTION: LedgerTaskStatus.IN_PROGRESS,
            ActionState.SENSOR_CONFIRM: LedgerTaskStatus.IN_PROGRESS,
            # Consent / approval — task is BLOCKED until user responds
            ActionState.PREVIEW_PENDING: LedgerTaskStatus.BLOCKED,
            ActionState.PREVIEW_APPROVED: LedgerTaskStatus.IN_PROGRESS,
        }

        ledger_status = STATE_MAP.get(state)
        if ledger_status:
            # === OWNERSHIP: claim on IN_PROGRESS, release on terminal ===
            if ledger_status == LedgerTaskStatus.IN_PROGRESS and not task.is_owned:
                user_id, prompt_id = _extract_ownership_from_prompt(user_prompt)
                task.claim(
                    node_id=_get_node_id(),
                    user_id=user_id,
                    prompt_id=prompt_id,
                )
                logger.info(f"Claimed ownership of {task_id} for {user_prompt}")

            # Handle PAUSED/BLOCKED → IN_PROGRESS via validated transitions.
            # PAUSED/USER_STOPPED -> RESUMING -> IN_PROGRESS (task.resume() does this)
            # BLOCKED -> PENDING -> IN_PROGRESS (resume not valid from BLOCKED in
            # the state machine — go through PENDING first)
            if (ledger_status == LedgerTaskStatus.IN_PROGRESS
                    and task.status in (LedgerTaskStatus.PAUSED, LedgerTaskStatus.USER_STOPPED)):
                if not task.resume(
                        reason=f"Resumed via ActionState.{state.value}"):
                    _rollback_task()
                    return False
                task.blocked_reason = None
                if ledger.save() is False:
                    _rollback_task()
                    return False
            elif (ledger_status == LedgerTaskStatus.IN_PROGRESS
                    and task.status == LedgerTaskStatus.BLOCKED):
                # BLOCKED -> PENDING -> IN_PROGRESS (validated 2-step path)
                if not task.transition_to(
                        LedgerTaskStatus.PENDING,
                        f"Unblocked via ActionState.{state.value}"):
                    _rollback_task()
                    return False
                if not task.transition_to(
                        LedgerTaskStatus.IN_PROGRESS,
                        f"Resumed via ActionState.{state.value}"):
                    _rollback_task()
                    return False
                task.blocked_reason = None
                if ledger.save() is False:
                    _rollback_task()
                    return False
            elif task.status != ledger_status:
                # Skip no-op transitions (e.g. IN_PROGRESS → IN_PROGRESS when
                # multiple ActionStates map to the same LedgerTaskStatus).
                #
                # #56: the ledger validates transitions deliberately (its Bug-#2
                # fix) and FAILED/COMPLETED/CANCELLED are terminal.  ActionState,
                # by contrast, is authoritative for its OWN retry FSM (ERROR→
                # IN_PROGRESS, TERMINATED→RECIPE_REQUESTED).  When the ledger task
                # is already terminal but ActionState has legitimately moved on,
                # forcing the transition is invalid BY DESIGN — update_task_status
                # would WARN-spam (~100/run) for an expected, benign divergence.
                # This advisory sync skips it quietly (the single-FSM unification,
                # #56 option (a), is the deeper cleanup; the divergence is benign).
                # #128 recovery reconcile: a stalled action can now RECOVER
                # (fallback → terminated) instead of trapping.  If it had been
                # reaped to FAILED meanwhile, the ledger is stale — so when the
                # ActionState reaches a SUCCESS terminal (ledger COMPLETED) over a
                # FAILED task, apply the now-permitted recovery edge instead of
                # silently skipping (which left the recovered goal reading FAILED
                # and capped the completed count).  Every OTHER terminal divergence
                # stays benign-skipped, exactly as #56 intended.
                _recover_failed = (ledger_status == LedgerTaskStatus.COMPLETED
                                   and task.status == LedgerTaskStatus.FAILED)
                if task.is_terminal() and not _recover_failed:
                    logger.debug(
                        "Advisory ledger sync: task %s terminal (%s), ActionState "
                        "moved to %s — skipping invalid transition (authoritative "
                        "FSM is ActionState).", task_id, task.status, state.value)
                else:
                    _apply = True
                    if _recover_failed:
                        if _is_possible_masked_failure(_recover_failed, state):
                            # #139 policy, finally made (task #6): disambiguate
                            # the forced terminal by the BANKED ARTIFACT.
                            _artifact = _banked_artifact_exists(task)
                            if _artifact is False:
                                # No proof-of-work → the force-terminate masked a
                                # genuine failure. The ledger stays FAILED (the
                                # honest signal the daemon's retry loop acts on)
                                # instead of inflating the completed count.
                                _apply = False
                                _MASKED_FAILURE_STATE['prevented'] += 1
                                _n = _MASKED_FAILURE_STATE['prevented']
                                (logger.warning if _n == 1 else logger.debug)(
                                    "#139 masked failure PREVENTED (#%d this run): "
                                    "%s reached TERMINATED with NO banked artifact "
                                    "— ledger stays FAILED (was: silently flipped "
                                    "to COMPLETED).", _n, task_id)
                            else:
                                # True → genuine recovery (artifact banked);
                                # None → coordinates unknown, FAIL OPEN to the
                                # historical reconcile (never block the flywheel
                                # on a heuristic that couldn't run).
                                _MASKED_FAILURE_STATE['count'] += 1
                                _n = _MASKED_FAILURE_STATE['count']
                                # Warn ONCE per process, then debug — #56
                                # de-spammed this exact path; the counter is the
                                # aggregate signal.
                                (logger.warning if _n == 1 else logger.debug)(
                                    "#139 possible MASKED FAILURE (#%d this run): "
                                    "ledger FAILED → COMPLETED for %s via "
                                    "TERMINATED (banked artifact: %s).",
                                    _n, task_id,
                                    'present' if _artifact else 'unknown')
                        else:
                            logger.info(
                                "Reconciled stale ledger FAILED → COMPLETED for %s "
                                "(ActionState %s authoritative — recovered work).",
                                task_id, state.value)
                    if _apply:
                        if not ledger.update_task_status(
                                task_id, ledger_status,
                                result=(result if state == ActionState.COMPLETED
                                        else None),
                                reason=f"ActionState: {state.value}"):
                            _rollback_task()
                            return False

            # === BLOCKED REASON: set specific reason based on ActionState source ===
            if ledger_status == LedgerTaskStatus.BLOCKED:
                if state == ActionState.PREVIEW_PENDING:
                    task.set_blocked_reason('approval_required')
                elif state == ActionState.FALLBACK_REQUESTED:
                    task.set_blocked_reason('input_required')
                elif state == ActionState.PENDING:
                    # ``block_for_user_input`` runs immediately before the
                    # state projection.  Do not overwrite its specific human
                    # dependency with the generic pending reason.
                    if task.blocked_reason != 'input_required':
                        task.set_blocked_reason('dependency')

            # === FAILURE REASON: the same courtesy for the FAILED terminal ===
            # BLOCKED recorded WHY; FAILED recorded nothing, so a reader could
            # see THAT a unit failed and never why.  Measured on live data:
            # blocked_reason 44/1000, failure_reason 0/1000 -- and
            # set_failure_reason() had zero callers anywhere in the tree.
            # Only ERROR and GAVE_UP map to FAILED (see STATE_MAP above), so
            # these two branches are exhaustive for this terminal.
            elif ledger_status == LedgerTaskStatus.FAILED:
                if state == ActionState.ERROR:
                    task.set_failure_reason('error')
                elif state == ActionState.GAVE_UP:
                    # #139: a force-give-up is an HONEST failure -- the action
                    # never verified and the flow completed around it.  Not an
                    # exception, and not retry-exhaustion, so neither ERROR nor
                    # MAX_RETRIES_EXCEEDED would be true.
                    task.set_failure_reason('abandoned')

            # === BOOKKEEPING: liveness, SLA, ownership ===
            # These run AFTER the durable write and must never vote on
            # whether it succeeded.  They used to sit under the same blanket
            # `except Exception` as the persistence above, so one malformed
            # value -- a bad started_at reaching _dt.fromisoformat below is
            # the cheapest example -- made this function return False.  Since
            # set_action_state now RAISES on a false return, that turned a
            # stale timestamp into a StateTransitionError that blocked every
            # transition for the action, including its terminal one: the work
            # was done and persisted, and the agent still could not say so.
            # Narrowed so only the transition and the save decide the verdict.
            try:
                task.heartbeat()

                # SLA: flag breach, post status request, emit notification.
                if task.is_sla_breached() and not task.sla_breached:
                    task.mark_sla_breached()
                    task.post_status("SLA breached — requesting status update from agent")
                    logger.warning(f"SLA breached for {task_id}")
                    try:
                        from core.platform.events import emit_event
                        emit_event('task.sla_breached', {
                            'task_id': task_id,
                            'prompt': user_prompt,
                            'sla_target_s': task.sla_target_s,
                            'deadline': task.deadline,
                            'action': 'status_request',
                        })
                    except Exception:
                        logger.warning('task.sla_breached event for %s not '
                                       'emitted', task_id, exc_info=True)

                # Release ownership on terminal states.
                if LedgerTaskStatus.is_terminal_state(ledger_status) and task.is_owned:
                    # Record time spent using known started_at from scope
                    if task.started_at:
                        from datetime import datetime as _dt
                        try:
                            elapsed = (_dt.now() - _dt.fromisoformat(
                                task.started_at)).total_seconds()
                            task.record_spend(time_s=elapsed)
                        except (TypeError, ValueError):
                            # A stale or hand-edited started_at costs the
                            # spend figure, never the release or the verdict.
                            logger.warning(
                                "%s has an unparseable started_at (%r); "
                                "releasing without a time_s spend record",
                                task_id, task.started_at)
                    task.release()
                    logger.info(f"Released ownership of {task_id}")
            except Exception as _bookkeeping_err:
                logger.warning(
                    "Ledger bookkeeping after a COMMITTED write failed for "
                    "%s (%s); the state change itself stands",
                    task_id, _bookkeeping_err, exc_info=True)

            logger.info(f"Auto-synced {task_id} -> {ledger_status.value} (ActionState: {state.value})")
    except Exception as e:
        if task is not None and task_snapshot is not None:
            try:
                task.__dict__.clear()
                task.__dict__.update(task_snapshot)
            except Exception:
                logger.warning('in-memory task %s not restored after the '
                               'failed ledger sync', task_id, exc_info=True)
        logger.error(f"Failed to auto-sync to ledger: {e}", exc_info=True)
        return False

    # Audit log: record state transition
    try:
        from security.immutable_audit_log import get_audit_log
        get_audit_log().log_event(
            'state_change', actor_id=user_prompt,
            action=f'{task_id} → {state.value}',
            detail={'action_id': action_id, 'state': state.value})
    except Exception:
        pass  # Audit log is best-effort, never blocks state transitions

    # Broadcast state change to EventBus
    try:
        from core.platform.events import emit_event
        from core.event_attribution import owner_user_id
        emit_event('action_state.changed', {
            'action_id': action_id,
            'state': state.value,
            'prompt': user_prompt,
            # #58: user_prompt is the canonical "{user_id}_{prompt_id}" key, so
            # the owner is resolvable for free — stamp it so the P3a SSE guard
            # routes this state change to that user's dashboard live.
            'user_id': owner_user_id(user_prompt=user_prompt),
        })
    except Exception:
        pass

    return True

# Import ledger types for sync function (lazy import to avoid circular deps)
def _get_ledger_task_status():
    """Lazy import to avoid circular dependencies"""
    from agent_ledger import TaskStatus as LedgerTaskStatus
    return LedgerTaskStatus


def block_for_user_input(user_prompt: str, action_id: int,
                         reason: str = "Waiting for user input") -> bool:
    """Block a task in the ledger when the agent needs user consent/input.

    Call this when send_message_to_user is invoked and the action's
    can_perform_without_user_input == "no". The task transitions to BLOCKED
    with BlockedReason.INPUT_REQUIRED until the user responds.
    """
    ledger = _ledger_registry.get(user_prompt)
    if not ledger:
        return False
    LedgerTaskStatus = _get_ledger_task_status()
    task_id = f"action_{action_id}"
    task = ledger.tasks.get(task_id)
    if not task or task.status != LedgerTaskStatus.IN_PROGRESS:
        return False
    before = copy.deepcopy(task.__dict__)
    if not task.block(reason):
        return False
    task.set_blocked_reason('input_required')
    if ledger.save() is False:
        task.__dict__.clear()
        task.__dict__.update(before)
        logger.error(
            "Refusing user-input block for %s: ledger persistence failed",
            task_id)
        return False
    logger.info(f"Blocked {task_id} for user input: {reason}")
    return True


def mark_action_waiting_for_user(user_prompt: str, action_id: int, reason: str) -> bool:
    """Project an existing input dependency into both lifecycle authorities.

    The ledger is blocked first while the action is still in progress; then
    the canonical ActionState projection becomes ``PENDING``.  This preserves
    the existing ``input_required`` reason instead of creating a second queue
    or allowing the generic pending projection to relabel it as a dependency.
    """
    if not block_for_user_input(user_prompt, action_id, reason):
        return False
    return safe_set_state(user_prompt, action_id, ActionState.PENDING, reason)


def resume_blocked_action(user_prompt: str, action_id: int,
                          reason: str = "Block resolved",
                          evidence: Optional[Dict[str, Any]] = None,
                          evidence_key: str = 'unblock_evidence') -> bool:
    """Resume one BLOCKED ledger task through its validated transition.

    User replies and an assigned expert turn are different authorities, but
    both resolve the same ledger block. Typed evidence is optional; callers
    must never label an expert response as human input.
    """
    ledger = _ledger_registry.get(user_prompt)
    if not ledger:
        return False
    LedgerTaskStatus = _get_ledger_task_status()
    task_id = f"action_{action_id}"
    task = ledger.tasks.get(task_id)
    if not task or task.status != LedgerTaskStatus.BLOCKED:
        return False
    before = copy.deepcopy(task.__dict__)
    if not task.resume(reason):
        return False
    evidence_items = None
    if evidence is not None:
        context = getattr(task, 'context', None)
        if isinstance(context, dict):
            evidence_items = context.setdefault(evidence_key, [])
            evidence_items.append(evidence)
    task.blocked_reason = None
    if ledger.save() is False:
        task.__dict__.clear()
        task.__dict__.update(before)
        logger.error(
            "Refusing unblock for %s: ledger persistence failed", task_id)
        return False
    logger.info(f"Resumed blocked {task_id}: {reason}")
    return True


def ledger_holds_no_block(user_prompt: str, action_id: int) -> bool:
    """Whether the registered ledger holds this action's task and it is NOT
    BLOCKED: a user-input block on it never landed (block_for_user_input
    refuses a task that is not in progress) or is already gone, so there is
    nothing durable to resume.

    False when there is no ledger or no such task (nothing is known, so a gate
    stays as it is) and when the task IS blocked (a resume that failed to save
    must be retried, not dropped)."""
    ledger = _ledger_registry.get(user_prompt)
    task = ledger.tasks.get(f"action_{action_id}") if ledger else None
    return (task is not None
            and task.status != _get_ledger_task_status().BLOCKED)


def resume_from_user_input(user_prompt: str, action_id: int,
                           reason: str = "User responded",
                           answer: Optional[str] = None) -> bool:
    """Resume a blocked task with evidence from a genuine user turn."""
    evidence = None
    if answer is not None:
        evidence = {
            'action_id': action_id,
            'answer': str(answer),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
    return resume_blocked_action(
        user_prompt, action_id, reason, evidence=evidence,
        evidence_key='user_input_evidence')

# Add new states to ActionState enum:
class FlowState(Enum):
    DEPENDENCY_ANALYSIS = "dependency_analysis"
    TOPOLOGICAL_SORT = "topological_sort"
    SCHEDULED_JOBS_CREATION = "scheduled_jobs_creation"
    FLOW_RECIPE_CREATION = "flow_recipe_creation"
    FLOW_COMPLETED = "flow_completed"

class ActionState(Enum):
    """Updated state machine to match exact user requirements"""
    ASSIGNED = "assigned"                           # 1. Assign Each Action from array
    IN_PROGRESS = "in_progress"                    # 2. Action execute in progress
    STATUS_VERIFICATION_REQUESTED = "status_verification_requested"  # 3. Status requested to verifier
    COMPLETED = "completed"                        # 4. Action performed successfully and verified
    PENDING = "pending"                           # 5. Action pending completion by verifier
    ERROR = "error"                               # 6. Action error or json error
    FALLBACK_REQUESTED = "fallback_requested"     # 7. Action fallback requested to user
    FALLBACK_RECEIVED = "fallback_received"       # 8. Action fallback received from user
    RECIPE_REQUESTED = "recipe_requested"         # 9. Action recipe json creation requested to AI
    RECIPE_RECEIVED = "recipe_received"           # 10. Action recipe json received with status done
    TERMINATED = "terminated"                     # 11. Action passed to chat instructor and Terminate issued
    EXECUTING_MOTION = "executing_motion"         # 12. Physical action executing via WorldModelBridge
    SENSOR_CONFIRM = "sensor_confirm"             # 13. Waiting for sensor confirmation of physical outcome
    PREVIEW_PENDING = "preview_pending"           # 14. Destructive action awaiting user approval
    PREVIEW_APPROVED = "preview_approved"         # 15. User approved destructive action, proceed
    GAVE_UP = "gave_up"                            # 16. Force-abandoned terminal (stalled/unverified work): an HONEST failure (ledger FAILED), NOT a verified success like TERMINATED. Re-openable (→ASSIGNED/RECIPE_REQUESTED) so a hive peer can retry (#139).


#: The states that mean "this action is waiting for the USER", straight from
#: the enum's own definitions above: PENDING is what mark_action_waiting_for_
#: user projects (the ledger's input_required block), FALLBACK_REQUESTED is a
#: fallback asked of the user, PREVIEW_PENDING is a destructive action awaiting
#: the user's approval.  An action in one of these has paused, not failed; a
#: reader that needs "is this a pause for the user?" asks this set rather than
#: spelling its own.
ACTION_STATES_AWAITING_USER = frozenset({
    ActionState.PENDING, ActionState.FALLBACK_REQUESTED,
    ActionState.PREVIEW_PENDING})


# The two answers to an action's can_perform_without_user_input live in
# core.constants (cheap to import: the A2A card reads them without pulling
# this module's hartos.helper).  Re-exported here for existing callers.
from core.constants import action_is_autonomous, autonomy_needs_user  # noqa: E402,F401


# ── No-progress stall guard for the CREATE loop ───────────────────────────
# create_recipe.get_response_group's main loop can spin to its 300-iteration
# cap (~25 min of wasted compute, observed live) when an action sits in a
# "requested" state whose recipe never arrives.  The late stuck-state guard
# there is BOTH suppressed (it checked whether ANY earlier action saved a
# recipe, so a multi-action flow whose LATER action stalls slips through) AND
# unreachable (the condition branches above it `continue` past it).  This pure
# tracker is the reachable, per-action replacement — call it once per loop
# iteration right after the action state is refreshed.  It lives here
# (autogen-free) so it is unit-testable without the full pipeline.
# Live 2026-06-04: a user chat routed through the speculative-expert CREATE
# path stalled on action 2 (recipe_requested; action 1 already done) and
# looped all 300 -> a generic TERMINATE with no conversation history.
STALL_GUARD_MAX_ITERS = 30  # tight cap: action stuck in a "requested" state

# Looser cap for an action wedged in any OTHER non-terminal state (IN_PROGRESS,
# STATUS_VERIFICATION_REQUESTED, ASSIGNED, PENDING).  Live 2026-06-04: a daemon
# coding goal's action 2 sat in IN_PROGRESS emitting unparseable recipe JSON and
# never reached RECIPE_REQUESTED, so the tight guard below (which only watched
# the "requested" states) never saw it and it spun toward the 300-iter hard cap.
# Set WELL above the proven-safe working zone (a legit action advances its state
# within a handful of group-chat rounds; the old guard let IN_PROGRESS run
# unbounded and nothing tripped a working action) so this can ONLY rescue a
# genuinely-wedged action — never a slow-but-progressing one.
STALL_GUARD_INPROGRESS_ITERS = 120

_STALL_STATES = (ActionState.RECIPE_REQUESTED, ActionState.FALLBACK_REQUESTED)
_TERMINAL_STATES = (ActionState.COMPLETED, ActionState.TERMINATED, ActionState.ERROR, ActionState.GAVE_UP)


def is_terminal_state(state) -> bool:
    """Is this action state one the action can never leave?

    The public reader for `_TERMINAL_STATES`, so callers in other modules ask
    this question instead of re-listing the states.  That tuple is already
    spelled out inline in `validate_state_transition` (the GAVE_UP guard) and
    twice more around the give-up paths; every extra copy is a place the set can
    drift, and drift here means a consumer disagreeing with the state machine
    about whether an action is finished.

    Added for the CREATE user-input gate (agent 88761328396, 2026-09-07), which
    kept asking the user about action 5 for 20 minutes after that action reached
    TERMINATED — see `create_recipe._should_block_on_user_input`.
    """
    return state in _TERMINAL_STATES


# #139 observability counter — bumped each time the FAILED→COMPLETED recovery
# reconcile fires via the TERMINATED (forced) terminal (a possible masked
# failure). Module-level so diag can quantify it without log-scraping; the
# reconcile logs a WARNING only ONCE per process (then debug), because #56
# deliberately de-spammed this path (it WARN-spammed ~100/run) — a per-event
# warning here would re-introduce that.
# 'prevented' (task #6, the #139 policy call finally made): masked failures
# the reconcile now REFUSES to flip — TERMINATED over a FAILED ledger with
# no banked artifact stays FAILED (see _banked_artifact_exists).
_MASKED_FAILURE_STATE = {'count': 0, 'prevented': 0}


def _banked_artifact_exists(task):
    """The action's banked recipe on disk — the PROOF-OF-WORK disambiguator
    for the #139 masked-failure policy (task #6).

    TERMINATED does not carry WHY it terminated (success cleanup vs
    force-kill of a stalled/failed action). The banked action recipe
    (prompts/{prompt_id}_{flow_id}_{action_id}.json, the spark-economy
    ground truth: recipe-existence proves the work happened) does:

      True  -> the recipe was banked; a FAILED->COMPLETED reconcile is a
               GENUINE recovery (the #128 un-trap).
      False -> no artifact; the forced terminal masked a real failure and
               COMPLETED would be a lie.
      None  -> unknown (task lacks recipe coordinates, or the path check
               errored) — callers FAIL OPEN to the historical reconcile
               behaviour, never blocking the flywheel on this heuristic.
    """
    try:
        p = getattr(task, 'recipe_prompt_id', None)
        f = getattr(task, 'recipe_flow_id', None)
        a = getattr(task, 'recipe_action_id', None)
        if p is None or f is None or a is None:
            return None
        from hartos.helper import safe_prompt_path
        return os.path.exists(safe_prompt_path(str(p), str(f), str(a)))
    except Exception:
        return None


def _is_possible_masked_failure(recover_failed, action_state):
    """#139 — observability for the #128 FAILED→COMPLETED recovery reconcile.

    That reconcile un-traps a stalled action whose ledger was reaped to FAILED
    when the ActionState reaches a terminal that maps to ledger COMPLETED. That
    is a GENUINE recovery when the terminal is a verified success (COMPLETED /
    RECIPE_RECEIVED), but a POSSIBLE MASKED FAILURE when it is TERMINATED — the
    forced/give-up terminal #128 routes a stalled-or-failed action through. A
    force-terminated failure flipped to COMPLETED inflates the completed count
    and hides a genuine failure.

    Pure + side-effect-free so it is unit-testable. Observability ONLY: callers
    still apply the reconcile (the flywheel must keep recovering). PREVENTING it
    — i.e. NOT counting a force-terminated failure as COMPLETED — is a policy
    call (#139), deliberately not made here. Returns True only for the
    ambiguous TERMINATED case.
    """
    return bool(recover_failed) and action_state == ActionState.TERMINATED


def stall_guard_step(prev_stuck_key, prev_iters, action_id, state,
                     recipe_exists):
    """Pure, side-effect-free no-progress tracker for the CREATE loop.

    Returns ``(stuck_key, iters, should_break)`` where ``stuck_key`` is the
    opaque ``(action_id, state)`` pair the caller threads back in:
      * Progress signal = the CURRENT action's OWN recipe on disk, OR the action
        reaching a TERMINAL state — either resets the counter to 0.
      * Otherwise the action is non-terminal with no recipe: increment the
        consecutive-stuck counter keyed on ``(action_id, state)``, restarting
        whenever that pair changes (the action advanced to a new state, or a
        different action became current).  ``should_break`` trips once the
        counter exceeds the cap for the state class — the tight
        ``STALL_GUARD_MAX_ITERS`` for the "requested" states (the action is done
        and only the recipe is missing; it should arrive in 1-2 rounds) and the
        looser ``STALL_GUARD_INPROGRESS_ITERS`` for every other non-terminal
        state (a legit action may genuinely work in IN_PROGRESS for a while).

    Keying on ``(action_id, state)`` — not action_id alone — is what lets an
    action wedged in IN_PROGRESS be caught: it never reaches a "requested"
    state, so the old action-id-only guard reset every iteration and never
    fired.  Progress is judged on the CURRENT action's OWN recipe (not "any
    earlier action saved one"), so a later action stalling in a multi-action
    flow is still caught.
    """
    if recipe_exists or state in _TERMINAL_STATES:
        return None, 0, False
    cap = STALL_GUARD_MAX_ITERS if state in _STALL_STATES else STALL_GUARD_INPROGRESS_ITERS
    key = (action_id, state)
    iters = (prev_iters + 1) if prev_stuck_key == key else 1
    return key, iters, iters > cap


# How many times one action may RE-ENTER a state it has already been in before
# the CREATE loop gives up.  6 leaves room for the legitimate retry rounds the
# recipe phase already does (request -> parse fail -> re-request) while still
# catching a cycle long before the 300-iteration hard cap.
CYCLE_GUARD_MAX_REVISITS = 6


def cycle_guard_step(prev_key, prev_entries, action_id, state, recipe_exists):
    """Pure no-NET-progress tracker: catches an action CYCLING through states.

    Complement to ``stall_guard_step``, which catches an action STUCK IN ONE
    state and by design treats every state change as progress and every terminal
    state as a reset.  Those are the right semantics for a state machine that
    advances monotonically — and exactly why an action that goes round in a
    circle escapes it: the key changes on each transition so the counter
    restarts, and revisiting COMPLETED/TERMINATED hard-resets it to 0.

    Live 2026-08-04, one 40s window, action 2:
        assigned -> in_progress -> status_verification_requested -> completed
        -> terminated -> recipe_requested -> terminated -> recipe_requested
        -> recipe_received
    Seven states, no net progress, guard never fired, loop ran toward
    max_iterations=300 and starved the chat hot path.

    Counts ENTRIES into a state, not iterations spent in it.  That distinction
    is load-bearing: an action legitimately working in IN_PROGRESS for the full
    ``STALL_GUARD_INPROGRESS_ITERS`` window is ONE entry and must never trip
    this, while an action bouncing back into RECIPE_REQUESTED again and again
    accrues one count per bounce.

    Returns ``(key, entries, should_break)``:
      * ``key``     — opaque ``(action_id, state)`` the caller threads back, used
                      to spot the next real transition.
      * ``entries`` — opaque ``{state: entry_count}`` for the CURRENT action.
      * resets whenever the current action changes (the pipeline genuinely moved
        on) or that action's OWN recipe lands on disk.

    Terminal states are deliberately NOT special-cased here: re-entering
    COMPLETED/TERMINATED is the cycle's signature, not evidence of progress.  A
    genuinely finished action still resets, because ``current_action`` advances
    and ``action_id`` therefore changes.

    Pure — no I/O, no logging.  Guarded by tests/unit/test_stall_guard.py.
    """
    if recipe_exists:
        return None, {}, False
    prev_action, prev_state = prev_key if prev_key else (None, None)
    if prev_action != action_id:
        return (action_id, state), {state: 1}, False
    entries = dict(prev_entries or {})
    if state != prev_state:                      # a real transition INTO `state`
        entries[state] = entries.get(state, 0) + 1
    return ((action_id, state), entries,
            max(entries.values(), default=0) > CYCLE_GUARD_MAX_REVISITS)


# Minimal valid recipe the model is told to emit as a last resort so a wedged
# action TERMINATES cleanly (status:done + empty recipe) instead of grinding.
_RECIPE_FALLBACK_OBJECT = (
    '{"status":"done","action":"","fallback_action":"","persona":"",'
    '"recipe":[],"can_perform_without_user_input":"yes"}'
)


def recipe_correction_directive(parse_failures: int) -> str:
    """Corrective text appended to a recipe request after the model's PRIOR
    response failed to parse as the required JSON.  Pure — no I/O.

    Why: local small models tend to wrap the recipe object in prose / markdown
    ``` fences, which breaks json_repair and leaves the action grinding in
    IN_PROGRESS (#89, observed live 2026-06-04).  Re-sending the identical
    prompt just gets the same garbage; this escalates instead —

      * ``parse_failures <= 0`` -> '' (a clean first attempt gets no nag).
      * 1 -> demand ONLY the JSON object (no prose, no fences).
      * 2+ -> additionally offer a minimal valid object to emit verbatim, so the
        action can TERMINATE rather than spin to the stall-guard cap.
    """
    if parse_failures <= 0:
        return ''
    msg = ("\n\nIMPORTANT: your previous response could NOT be parsed as JSON. "
           "Respond with ONLY the single JSON object specified above — no prose, "
           "no explanation, no markdown ``` fences. Begin with '{' and end with '}'.")
    if parse_failures >= 2:
        msg += (" If you cannot produce a valid recipe, emit EXACTLY this and "
                "nothing else: " + _RECIPE_FALLBACK_OBJECT)
    return msg


# Canonical prefix of the recipe-creation prompt that
# create_recipe.request_recipe_for_action / request_recipe_for_action_last
# emit to ask an agent for the {"status":"done", ...recipe} JSON that advances
# RECIPE_REQUESTED -> RECIPE_RECEIVED.  ONE source so (a) the deterministic
# speaker-routing in create_recipe.state_transition can NEVER drift from the
# actual prompt text, and (b) lifecycle_hook_track_recipe_request below matches
# the same string.  Lives here (autogen-free) so it stays unit-testable.
RECIPE_CREATE_PROMPT_PREFIX = (
    'Focus on the current task at hand and create a detailed recipe'
)


def is_recipe_creation_request(content) -> bool:
    """True when ``content`` is the recipe-creation prompt.

    Pure + autogen-free so create_recipe.state_transition can call it to route
    recipe requests deterministically to the StatusVerifier instead of letting
    the LLM speaker-selector pick the Assistant/Helper (which echo the prompt
    or reply "I'm not sure I understand", so the action only advances on a
    lucky StatusVerifier round — live 2026-06-07 19:13-19:18).  Uses ``in`` (not
    just ``startswith``) so an agent that echoes the prompt is still detected.
    """
    return isinstance(content, str) and RECIPE_CREATE_PROMPT_PREFIX in content


# Add to lifecycle_hooks.py
class FlowLifecycleState:
    """Track overall flow lifecycle beyond individual actions"""

    def __init__(self):
        self.flows = {}  # {user_prompt: {flow_id: state}}

    def set_flow_state(self, user_prompt, flow_id, state):
        if user_prompt not in self.flows:
            self.flows[user_prompt] = {}
        self.flows[user_prompt][flow_id] = state


flow_lifecycle = FlowLifecycleState()


# Action retry tracking to prevent infinite loops
class ActionRetryTracker:
    """Track retry counts to force ERROR state after threshold"""

    def __init__(self):
        self.pending_counts = {}  # {(user_prompt, action_id): count}
        self.MAX_PENDING_RETRIES = 3  # Force ERROR after 3 pending attempts

    def increment_pending(self, user_prompt, action_id):
        """Increment pending count and return True if threshold exceeded"""
        key = (user_prompt, action_id)
        count = self.pending_counts.get(key, 0) + 1
        self.pending_counts[key] = count

        if count > self.MAX_PENDING_RETRIES:
            logger.warning(f"[RETRY LIMIT] Action {action_id} has been PENDING {count} times - forcing to ERROR state")
            # Emit retry exhaustion event so other subsystems can react
            try:
                from core.platform.events import emit_event
                emit_event('action.retry_exhausted', {
                    'action_id': action_id,
                    'prompt': user_prompt,
                    'retry_count': count,
                    'max_retries': self.MAX_PENDING_RETRIES,
                })
            except Exception:
                pass
            return True  # Exceeded threshold

        logger.info(f"[RETRY TRACKING] Action {action_id} pending count: {count}/{self.MAX_PENDING_RETRIES}")
        return False  # Still under threshold

    def reset_count(self, user_prompt, action_id):
        """Reset counter when action completes or errors"""
        key = (user_prompt, action_id)
        if key in self.pending_counts:
            del self.pending_counts[key]
            logger.info(f"[RETRY TRACKING] Reset pending count for action {action_id}")


retry_tracker = ActionRetryTracker()


# Enforcement functions
def enforce_action_termination(user_prompt, current_action_id):
    """Ensure current action is TERMINATED before proceeding"""
    state = get_action_state(user_prompt, current_action_id)
    if state not in (ActionState.TERMINATED, ActionState.GAVE_UP):
        raise StateTransitionError(
            f"Action {current_action_id} must be terminal (TERMINATED/GAVE_UP) before proceeding (current: {state})")


def enforce_all_actions_terminated(user_prompt, total_actions):
    """Ensure all actions reached TERMINATED before flow completion"""
    for action_id in range(1, total_actions + 1):
        state = get_action_state(user_prompt, action_id)
        if state not in (ActionState.TERMINATED, ActionState.GAVE_UP):
            return False, f"Action {action_id} not terminal (state: {state})"
    return True, "All actions terminal"

class StateTransitionError(Exception):
    """Raised when an invalid state transition is attempted"""
    pass


# 2. UPDATE your set_action_state function to enforce transitions:
def set_action_state(user_prompt: str, action_id: int, state: ActionState,
                     reason: str = "", result: Any = None):
    """Set state of an action with validation."""
    current_state = get_action_state(user_prompt, action_id)

    # Allow same-state transitions (idempotent)
    if current_state == state:
        return

    # Validate transition
    if not validate_state_transition(user_prompt, action_id, state):
        raise StateTransitionError(
            f"Invalid transition: Action {action_id} cannot go from {current_state.value} to {state.value}")

    # The durable ledger is the lifecycle authority. Project ActionState only
    # after that write commits; otherwise callers could receive True while the
    # task remained unfinished on disk.
    #
    # Keep persistence failure explicit for every state, including terminal
    # ones.  Returning false here would be ignored by direct callers and would
    # recreate a silent completion.  The current call graph has one public
    # wrapper (safe_set_state) and one multi-step wrapper
    # (force_state_through_valid_path); both translate this exception to False.
    # The only remaining direct call is the idempotent assignment hook.
    if not _auto_sync_to_ledger(
            user_prompt, action_id, state, result=result):
        raise StateTransitionError(
            f"Ledger persistence failed for Action {action_id} -> "
            f"{state.value}")

    # Perform transition (lock protects check-then-act on shared dict)
    with _state_lock:
        if user_prompt not in action_states:
            action_states[user_prompt] = {}
        action_states[user_prompt][action_id] = state
    logger.info(f"[TARGET] Action {action_id}: {current_state.value} → {state.value} ({reason})")

    # Advisory consent check — log when data_access consent is missing (never blocks)
    if state == ActionState.IN_PROGRESS:
        try:
            from integrations.social.consent_service import ConsentService
            from integrations.social.models import db_session
            with db_session(commit=False) as db:
                # user_prompt is the SESSION KEY f"{user_id}_{prompt_id}";
                # check_consent's parameter is a USER ID and it matches against
                # user_consents.user_id, which holds bare uuids written by the
                # consent UI. Passing the session key made this branch
                # unsatisfiable -- it logged 415 false "no consent" lines over
                # Sep 18-19 while a granted data_access row sat in the table --
                # so resolve the owner with the helper already in this file
                # (:102) instead of inventing a second parse.
                _consent_user, _ = _extract_ownership_from_prompt(user_prompt)
                if not ConsentService.check_consent(db, _consent_user,
                                                    'data_access'):
                    logger.info(
                        f"[CONSENT] No data_access consent for "
                        f"user={_consent_user} (session={user_prompt}), "
                        f"action={action_id} (advisory only)")
        except Exception:
            pass  # Consent check is advisory, never blocks execution

    # Telemetry recording for recipe experience
    try:
        from hartos.recipe_experience import RecipeExperienceRecorder as RER
        if state == ActionState.IN_PROGRESS:
            RER.start_action_timer(user_prompt, action_id)
        elif state in (ActionState.COMPLETED, ActionState.ERROR, ActionState.TERMINATED, ActionState.GAVE_UP):
            RER.stop_action_timer(user_prompt, action_id, state.value)
        if state == ActionState.FALLBACK_RECEIVED:
            RER.record_fallback_used(user_prompt, action_id, reason, True)
    except Exception:
        pass


# 3. ADD these wrapper functions for safe state updates:
def safe_set_state(user_prompt: str, action_id: int, new_state: ActionState,
                   reason: str = "", result: Any = None):
    """Safely set state with error handling"""
    try:
        set_action_state(user_prompt, action_id, new_state, reason,
                         result=result)
        return True
    except StateTransitionError as e:
        logger.error(f"[ERROR] {e}")
        return False


def force_state_through_valid_path(user_prompt: str, action_id: int, target_state: ActionState, reason: str = "",
                                   through_completed: bool = True):
    """Force state to target through valid transitions.

    ``through_completed=False`` refuses (returns False, writes nothing) a path
    that would WRITE COMPLETED on the way.  COMPLETED means "verified with a
    receipt", and its one legitimate writer is commit_verified_action_completion;
    a caller that only wants an action closed, not certified, passes False.
    """
    current_state = get_action_state(user_prompt, action_id)

    # Map of how to reach each target state from any current state
    state_paths = {
        # From ASSIGNED
        (ActionState.ASSIGNED, ActionState.IN_PROGRESS): [ActionState.IN_PROGRESS],
        (ActionState.ASSIGNED, ActionState.STATUS_VERIFICATION_REQUESTED): [ActionState.IN_PROGRESS,
                                                                            ActionState.STATUS_VERIFICATION_REQUESTED],
        (ActionState.ASSIGNED, ActionState.COMPLETED): [ActionState.IN_PROGRESS,
                                                        ActionState.STATUS_VERIFICATION_REQUESTED,
                                                        ActionState.COMPLETED],

        # From IN_PROGRESS
        (ActionState.IN_PROGRESS, ActionState.STATUS_VERIFICATION_REQUESTED): [
            ActionState.STATUS_VERIFICATION_REQUESTED],
        (ActionState.IN_PROGRESS, ActionState.COMPLETED): [ActionState.STATUS_VERIFICATION_REQUESTED,
                                                           ActionState.COMPLETED],

        # From STATUS_VERIFICATION_REQUESTED
        (ActionState.STATUS_VERIFICATION_REQUESTED, ActionState.COMPLETED): [ActionState.COMPLETED],
        (ActionState.STATUS_VERIFICATION_REQUESTED, ActionState.PENDING): [ActionState.PENDING],
        (ActionState.STATUS_VERIFICATION_REQUESTED, ActionState.ERROR): [ActionState.ERROR],

        # From COMPLETED
        (ActionState.COMPLETED, ActionState.TERMINATED): [ActionState.TERMINATED],  # REUSE: skip recipe phase
        (ActionState.COMPLETED, ActionState.FALLBACK_REQUESTED): [ActionState.FALLBACK_REQUESTED],
        (ActionState.COMPLETED, ActionState.FALLBACK_RECEIVED): [ActionState.FALLBACK_REQUESTED,
                                                                 ActionState.FALLBACK_RECEIVED],
        (ActionState.COMPLETED, ActionState.RECIPE_REQUESTED): [ActionState.FALLBACK_REQUESTED,
                                                                ActionState.FALLBACK_RECEIVED,
                                                                ActionState.RECIPE_REQUESTED],

        # From PENDING (two paths based on your flows)
        (ActionState.PENDING, ActionState.COMPLETED): [ActionState.COMPLETED],  # Flow #4
        (ActionState.PENDING, ActionState.ERROR): [ActionState.ERROR],  # Flow #3

        # From ERROR (retry path)
        (ActionState.ERROR, ActionState.IN_PROGRESS): [ActionState.IN_PROGRESS],
        (ActionState.ERROR, ActionState.COMPLETED): [ActionState.IN_PROGRESS, ActionState.STATUS_VERIFICATION_REQUESTED,
                                                     ActionState.COMPLETED],

        # From FALLBACK states
        (ActionState.FALLBACK_REQUESTED, ActionState.FALLBACK_RECEIVED): [ActionState.FALLBACK_RECEIVED],
        (ActionState.FALLBACK_RECEIVED, ActionState.RECIPE_REQUESTED): [ActionState.RECIPE_REQUESTED],

        # From RECIPE states
        (ActionState.RECIPE_REQUESTED, ActionState.RECIPE_RECEIVED): [ActionState.RECIPE_RECEIVED],
        (ActionState.RECIPE_RECEIVED, ActionState.TERMINATED): [ActionState.TERMINATED],

        # Force-to-terminal recovery (2026-06-13).  TERMINATED is the absorbing
        # terminal state, so the flow-complete force-terminate (create_recipe.py
        # ~4587) must drive ANY non-terminal action to TERMINATED.  Without these
        # an action still ASSIGNED / IN_PROGRESS / awaiting-verification / PENDING
        # at the flow boundary could not terminate ('Invalid transition: X ->
        # terminated', 187x/boot on the live build); can_increment then blocked,
        # the pipeline re-ran the same action forever and no goal ever reached
        # recipe-save -> the flywheel never spun + the CPU churned.  Each routes
        # through COMPLETED (TERMINATED's only legal predecessor) so state history
        # stays consistent.  A force-completed action's recipe QUALITY is guarded
        # downstream by trace-banking (#143: real tool calls -> real recipe) +
        # placebo rejection (#140), NOT here -- this is liveness, not quality.
        (ActionState.ASSIGNED, ActionState.TERMINATED): [
            ActionState.IN_PROGRESS, ActionState.STATUS_VERIFICATION_REQUESTED,
            ActionState.COMPLETED, ActionState.TERMINATED],
        (ActionState.IN_PROGRESS, ActionState.TERMINATED): [
            ActionState.STATUS_VERIFICATION_REQUESTED,
            ActionState.COMPLETED, ActionState.TERMINATED],
        (ActionState.STATUS_VERIFICATION_REQUESTED, ActionState.TERMINATED): [
            ActionState.COMPLETED, ActionState.TERMINATED],
        (ActionState.PENDING, ActionState.TERMINATED): [
            ActionState.COMPLETED, ActionState.TERMINATED],
    }

    if current_state == target_state:
        return True

    # FIX-5.1 (2026-04-21): TERMINATED is the absorbing "post-completion"
    # state — RECIPE_RECEIVED → TERMINATED is the canonical last edge
    # (see line 472 above).  If a caller asks for any state that comes
    # BEFORE terminated (COMPLETED, FALLBACK_*, RECIPE_*), it's a
    # no-op "already past this" request from the auto-complete code path
    # in hart_intelligence_entry, not a real transition.  Return True
    # silently instead of logging an ERROR-level red herring that
    # masquerades as a genuine state-machine failure in the logs.
    #
    # Witnessed 2026-04-21T14:06:21 during live UI sweep v6:
    #   got status as:completed
    #   CHECKING FOR FALLBACK current_action=5 action_id=5
    #   Action 5 completed with auto-generated fallback: ...
    #   [ERROR] No valid path from terminated to completed   <-- spam
    _POST_TERMINATED_SYNONYMS = {
        ActionState.COMPLETED,
        ActionState.FALLBACK_REQUESTED,
        ActionState.FALLBACK_RECEIVED,
        ActionState.RECIPE_REQUESTED,
        ActionState.RECIPE_RECEIVED,
    }
    if current_state == ActionState.TERMINATED and target_state in _POST_TERMINATED_SYNONYMS:
        logger.info(
            f"[NOOP] Action {action_id} already terminated; "
            f"{target_state.value} is already satisfied (reason={reason!r})"
        )
        return True

    # Resolve the step sequence.  An enumerated MULTI-STEP shortcut in
    # state_paths wins (it threads through intermediate states that carry
    # side-effects, e.g. ASSIGNED→COMPLETED via IN_PROGRESS).  Otherwise fall
    # back to the DIRECT edge whenever validate_state_transition allows it —
    # this keeps valid_transitions the single source of truth for "is this edge
    # legal" and state_paths a pure shortcut table, instead of two maps that
    # silently drift.  Without the fallback, a recovery edge added to
    # valid_transitions but not mirrored here was unreachable: #128's
    # RECIPE_REQUESTED→TERMINATED couldn't be forced, so the flow-complete force
    # (create_recipe.py:4464) left a stuck recipe_requested action in
    # IN_PROGRESS and the goal never completed.
    path_key = (current_state, target_state)
    if path_key in state_paths:
        path = state_paths[path_key]
    elif validate_state_transition(user_prompt, action_id, target_state):
        path = [target_state]
    else:
        logger.error(f"[ERROR] No valid path from {current_state.value} to {target_state.value}")
        return False

    if not through_completed and ActionState.COMPLETED in path:
        logger.info(
            f"[UNVERIFIED] Action {action_id} is {current_state.value}: reaching "
            f"{target_state.value} would record it COMPLETED without a verified "
            f"receipt; leaving it open (reason={reason!r})")
        return False

    logger.info(f"🔧 Auto-path for Action {action_id}: {current_state.value} → {target_state.value}")
    # Execute each step in the path
    for step_state in path:
        try:
            set_action_state(user_prompt, action_id, step_state, f"auto-path: {reason}")
        except StateTransitionError as e:
            logger.error(f"[ERROR] Auto-path failed at {step_state.value}: {e}")
            return False
    return True


# State tracking
action_states = {}  # {user_prompt: {action_id: current_state}}


def get_action_state(user_prompt: str, action_id: int) -> ActionState:
    """Get current state of an action."""
    with _state_lock:
        return action_states.get(user_prompt, {}).get(action_id, ActionState.ASSIGNED)


def clear_action_states(user_prompt: str, user_tasks=None) -> int:
    """Drop one session's run state so a NEW run starts from the beginning.

    `action_states` is keyed only by user_prompt ("{user_id}_{prompt_id}") — it
    carries no phase dimension and no run id, so CREATE and REUSE for the same
    agent address the SAME cells.  CREATE force-terminates every action at its
    flow boundary (create_recipe.py "[FLOW-COMPLETE] Forcing action N ..."), so
    a REUSE that follows IN THE SAME PROCESS reads TERMINATED for everything and
    `[AUTO-ADVANCE]`s through the whole flow without executing a single tool.

    Measured live 2026-09-05 on agent 90210554431 (4 actions) — same agent, same
    recipe, same request, only a process restart between the two runs:

        same process as CREATE -> 4x AUTO-ADVANCE, action 1 -> 5 in 14 ms,
                                  0 tool calls
        fresh process          -> 0x AUTO-ADVANCE, action stays 1,
                                  google_search executed for real

    Restarting is not a fix: the daemon flywheel runs CREATE and REUSE in one
    long-lived process, which is precisely the failing case.  Clearing at the
    start of a REUSE run is.

    Scoped to the one session and idempotent (an unknown key is a no-op), so it
    can never disturb another agent's in-flight run.  Lives here because
    `set_action_state` is this dict's only writer and `get_action_state` its
    only reader — a caller reaching into the dict itself would be a third
    accessor and a parallel path.

    RUN STATE IS TWO STORES, AND BOTH RESET HERE.  `action_states` (this module)
    says what PHASE each action is in; the session's `Action.current_action` (an
    entry in the caller's `user_tasks` cache) says WHICH action is current.
    Clearing only one is vacuous — [[feedback_mirrored_state_reset]], and
    measured twice:

      • states without pointer — agent 33323830039, 2026-09-07: a 1-action
        recipe driven twice in one process kept `current_action = 2`, so the
        second drive terminated a phantom "Action 2" in 35 ms, ran no tool, and
        answered with a self-introduction.
      • pointer without states — the 90210554431 case above: every action still
        reads TERMINATED and the loop `[AUTO-ADVANCE]`s through the recipe.

    Pass `user_tasks` (the mapping, or the session's own `Action`) and this
    resets both.  Callers that only hold the states — `reuse_recipe.py:1081`,
    which rebuilds the `Action` itself on the next line — keep the one-argument
    form unchanged.

    THE TWO STORES RESET ON DIFFERENT TERMS, because they carry different risk.
    Dropping `action_states` mid-run is safe: the actions re-read as ASSIGNED and
    the run continues where its pointer says.  Resetting the POINTER mid-run
    would truncate the run, so it happens only on the two states that cannot be
    a live continuation:

      * `current_action > len(actions)` — past the end.
      * `current_action == len(actions)` AND that action is TERMINAL — parked on
        a finished final action.  The integer alone is ambiguous here (a run
        still working on its last action looks identical), so the action's own
        state is what separates them; agent 88764372848 is the measured case.

    Anything else is a live continuation and is left alone.  That is what makes
    this function safe to call at ANY run entry point without knowing whether a
    run is already in flight.

    DELIBERATELY NOT HANDLED: a run abandoned MID-recipe — action 3 of 10, not
    terminal — followed by a genuinely NEW request, still resumes at 3.  That one
    is genuinely ambiguous without a run-id from the caller, and this does not
    invent one.

    Returns the number of action entries dropped (0 when there was no session).
    """
    if user_tasks is not None:
        _reset_finished_pointer(user_prompt, user_tasks)

    with _state_lock:
        dropped = len(action_states.pop(user_prompt, {}) or {})
    if dropped:
        logger.info(
            "[STATE-RESET] cleared %d action state(s) for %s — this run starts "
            "from ASSIGNED instead of inheriting the previous phase's terminals",
            dropped, user_prompt)
    return dropped


def _reset_finished_pointer(user_prompt: str, user_tasks) -> bool:
    """Rewind `current_action` to 1 iff the previous run ran off the end.

    Split out of `clear_action_states` for SRP: that function owns the states,
    this owns the pointer, and the caller-facing contract stays one call.  The
    predicate and its safety argument are documented there.

    Accepts the same shapes the sibling `lifecycle_hook_*` functions accept — a
    mapping of sessions, or the session's `Action` itself — so there is one
    accessor idiom in this module rather than a second one here.
    """
    try:
        if hasattr(user_tasks, 'get'):
            task = user_tasks.get(user_prompt)
        elif hasattr(user_tasks, 'current_action'):
            task = user_tasks
        else:
            return False
        if task is None:
            return False

        n = len(getattr(task, 'actions', None) or [])
        current = getattr(task, 'current_action', 1)
        # `not n` guards a recipe with zero actions: 1 > 0 would otherwise read
        # as "past the end" and rewind on every single turn.
        if not n or not isinstance(current, int) or current < 1:
            return False

        if current > n:
            why = f"pointer {current} is past the end of {n} action(s)"
        elif current == n and is_terminal_state(get_action_state(user_prompt, current)):
            # A run PARKED on its final action and a run still WORKING on its
            # final action are the same integer; only the action's own state
            # separates them.  Measured live 2026-09-07 on agent 88764372848
            # ("Nunba Guardian", 5 actions): CREATE ended with current_action=5
            # and action 5 TERMINATED, so the next REUSE drive resumed AT 5 —
            # `Retrieved current_action_id: 5` x7, then STUCK LOOP DETECTED and
            # ASSISTANT-STREAK-ESCALATE — and actions 1-4 (read the log, filter
            # ERROR, pick the newest) never ran.  The agent could not reach its
            # goal because the work was skipped, not because it failed.
            why = (f"final action {current}/{n} is "
                   f"{get_action_state(user_prompt, current).value} — the "
                   f"previous run ended on it")
        else:
            return False

        task.current_action = 1
    except Exception:
        # Never raise into the reuse hot path — a failed reset must degrade to
        # the previous behaviour, not kill the turn.
        return False

    logger.info("[RUN-BOUNDARY] %s: previous run finished (%s) — reset to "
                "action 1", user_prompt, why)
    return True


def validate_state_transition(user_prompt: str, action_id: int, new_state: ActionState) -> bool:
    """Validate state transitions follow the exact sequence"""
    current_state = get_action_state(user_prompt, action_id)

    # #139: GAVE_UP is the universal HONEST give-up terminal — reachable from any
    # NON-verified, not-already-terminal state (the flow-complete force-abandon of a
    # stalled action). A verified action (COMPLETED/RECIPE_RECEIVED) goes to
    # TERMINATED instead, never GAVE_UP. Purely additive: nothing else targets it.
    if new_state == ActionState.GAVE_UP and current_state not in (
            ActionState.COMPLETED, ActionState.RECIPE_RECEIVED,
            ActionState.TERMINATED, ActionState.GAVE_UP):
        return True

    valid_transitions = {
        ActionState.ASSIGNED: [ActionState.IN_PROGRESS, ActionState.ASSIGNED, ActionState.PREVIEW_PENDING],
        ActionState.IN_PROGRESS: [ActionState.STATUS_VERIFICATION_REQUESTED, ActionState.IN_PROGRESS, ActionState.ERROR, ActionState.PENDING],
        ActionState.STATUS_VERIFICATION_REQUESTED: [ActionState.COMPLETED, ActionState.PENDING, ActionState.ERROR, ActionState.STATUS_VERIFICATION_REQUESTED],
        ActionState.COMPLETED: [ActionState.FALLBACK_REQUESTED, ActionState.RECIPE_REQUESTED, ActionState.TERMINATED, ActionState.COMPLETED],  # Allow direct recipe request (autonomous) or termination
        # PENDING -> IN_PROGRESS is the resume edge.  PENDING is a wait (the
        # user's input via mark_action_waiting_for_user, or a 'pending'
        # verdict); once it is answered the action runs again.  Without this
        # edge the ledger resumed (resume_from_user_input: BLOCKED ->
        # IN_PROGRESS) while create_recipe's [EXECUTE-PENDING] request for
        # IN_PROGRESS was refused and the action ran reading PENDING --
        # measured 21:23:20 "cannot go from pending to in_progress" right
        # after "Latest User message: Yes, proceed".
        ActionState.PENDING: [ActionState.COMPLETED, ActionState.ERROR, ActionState.PENDING, ActionState.IN_PROGRESS],
        # FIX: Allow ERROR to reach TERMINATED via FALLBACK_REQUESTED/RECIPE_REQUESTED or directly
        ActionState.ERROR: [ActionState.IN_PROGRESS, ActionState.PENDING, ActionState.ERROR, ActionState.FALLBACK_REQUESTED, ActionState.RECIPE_REQUESTED, ActionState.TERMINATED],
        ActionState.FALLBACK_REQUESTED: [ActionState.FALLBACK_RECEIVED, ActionState.FALLBACK_REQUESTED],
        ActionState.FALLBACK_RECEIVED: [ActionState.RECIPE_REQUESTED, ActionState.FALLBACK_RECEIVED],
        # RECIPE_REQUESTED recovery edges (#128).  The happy path is
        # →RECIPE_RECEIVED, but when the (often 4B) model fails to emit a recipe
        # the pipeline MUST be able to escape recipe_requested, two ways:
        #   • →FALLBACK_REQUESTED — the autonomous-fallback pattern: the verifier
        #     hook returns force_fallback (create_recipe.py:4066).
        #   • →TERMINATED — the TERMINATE handler (lifecycle_hook_track_termination,
        #     ~line 1018) gates on validate_state_transition(..., TERMINATED).
        #   • →ERROR — a raising recipe step routes through ERROR's recovery set.
        # Without these, the action sat in recipe_requested until the stall-guard
        # broke the flow — the live ~9% goal-completion rate (9 987 'Invalid
        # transition: recipe_requested → …' per window).  Mirrors the recovery
        # edges COMPLETED and ERROR already carry; stays targeted (no →IN_PROGRESS).
        ActionState.RECIPE_REQUESTED: [ActionState.RECIPE_RECEIVED, ActionState.RECIPE_REQUESTED, ActionState.FALLBACK_REQUESTED, ActionState.TERMINATED, ActionState.ERROR],
        ActionState.RECIPE_RECEIVED: [ActionState.TERMINATED, ActionState.RECIPE_RECEIVED],
        # Final state, but: (a) an action can be re-opened (→ASSIGNED), and
        # (b) recipe-capture can run AFTER termination (→RECIPE_REQUESTED).  The
        # latter edge fixes the STUCK LOOP (#56): recipe-gen pushed a TERMINATED
        # action toward RECIPE_REQUESTED, find_path failed, and autogen re-ran
        # the same action.  RECIPE_REQUESTED→RECIPE_RECEIVED→TERMINATED already
        # exists, so this just lets the capture flow complete instead of looping.
        ActionState.TERMINATED: [ActionState.ASSIGNED, ActionState.RECIPE_REQUESTED],
        # #139: GAVE_UP re-opens for a retry (mirrors TERMINATED) so the daemon can
        # re-attempt a gave-up action via a hive peer; never →IN_PROGRESS directly.
        ActionState.GAVE_UP: [ActionState.ASSIGNED, ActionState.RECIPE_REQUESTED, ActionState.GAVE_UP],
        # Preview states (opt-in for destructive actions)
        ActionState.PREVIEW_PENDING: [ActionState.PREVIEW_APPROVED, ActionState.ERROR, ActionState.TERMINATED],
        ActionState.PREVIEW_APPROVED: [ActionState.IN_PROGRESS],
    }

    allowed = valid_transitions.get(current_state, [])
    if new_state not in allowed:
        # DEBUG, not ERROR — this function is a PREDICATE.  Every caller uses
        # its return value as a question:
        #     :730  if not validate_state_transition(...):  raise
        #     :901  elif validate_state_transition(...):
        #     :1024/:1059/:1091/:1109/:1119/:1148/:1174/:1197/:1223
        #           if validate_state_transition(...):
        # and the unit tests assert True/False.  Nothing reads this log to
        # make a decision, so a "no" answer is normal control flow, not a
        # failure.
        #
        # It also DOUBLE-LOGGED every genuine failure: set_action_state()
        # calls this at :730, logs here, then raises StateTransitionError
        # (:732), which safe_set_state() catches and logs AGAIN at :777.  One
        # bad set produced two ERROR lines — observed 2026-08-05 as 275 of
        # each for ~275 real events (terminated → pending), i.e. 550 lines
        # that read as 550 failures.
        #
        # After this change: a guarded caller asking permission logs nothing;
        # a genuinely-refused set still logs exactly one ERROR, from :777.
        # Same family as #534 (probe_failed false-positive demoted to INFO).
        # The text is unchanged so the pattern stays greppable at DEBUG.
        logger.debug(f"Invalid transition: {current_state.value} → {new_state.value}")
        return False

    logger.info(f"[OK] Valid transition: {current_state.value} → {new_state.value}")
    return True


def lifecycle_hook_track_action_assignment(user_prompt: str, user_tasks, group_chat=None) -> bool:
    """1. Track when action is assigned from array.

    Args:
        user_prompt: The user prompt key (e.g. "123_456").
        user_tasks: Either a dict of user tasks, an Action object with
            .current_action, or a plain int action_id for simple state setting.
        group_chat: Optional GroupChat object. When provided, checks messages
            to determine if ChatInstructor assigned the action.
    """
    # Support simple (user_prompt, action_id) calls for direct state setting
    if isinstance(user_tasks, int):
        set_action_state(user_prompt, user_tasks, ActionState.ASSIGNED)
        return True

    if hasattr(user_tasks, 'get'):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return False
        current_action_id = current_tasks.current_action
    elif hasattr(user_tasks, 'current_action'):
        current_action_id = user_tasks.current_action
    else:
        return False

    current_state = get_action_state(user_prompt, current_action_id)

    if current_state not in [ActionState.ASSIGNED, ActionState.ERROR]:
        logger.info(f"[LOCKED] Action {current_action_id} in {current_state.value} - skipping assignment hook")
        return False

    # When ChatInstructor assigns action, move from ASSIGNED to IN_PROGRESS
    if (group_chat and group_chat.messages and
        group_chat.messages[-1]['name'] == 'ChatInstructor' and f'Action {current_action_id}' in group_chat.messages[-1]['content']):

        if validate_state_transition(user_prompt, current_action_id, ActionState.IN_PROGRESS):
            safe_set_state(user_prompt, current_action_id, ActionState.IN_PROGRESS,"hook tracking lifecycle_hook_track_action_assignment")
            return True

    return False


def lifecycle_hook_track_status_verification_request(user_prompt: str, user_tasks, group_chat=None) -> bool:
    """3. Track when status verification is requested.

    Args:
        user_prompt: The user prompt key.
        user_tasks: Dict, Action object, or plain int action_id.
        group_chat: Optional GroupChat object.
    """
    # Support simple (user_prompt, action_id) calls
    if isinstance(user_tasks, int):
        force_state_through_valid_path(user_prompt, user_tasks, ActionState.STATUS_VERIFICATION_REQUESTED,
                                       "direct status verification request")
        return True

    if hasattr(user_tasks, 'get'):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return False
        current_action_id = current_tasks.current_action
    elif hasattr(user_tasks, 'current_action'):
        current_action_id = user_tasks.current_action
    else:
        return False

    # When @StatusVerifier is mentioned, move to STATUS_VERIFICATION_REQUESTED
    if (group_chat and group_chat.messages and
        '@StatusVerifier' in group_chat.messages[-1]['content']):

        if validate_state_transition(user_prompt, current_action_id, ActionState.STATUS_VERIFICATION_REQUESTED):
            safe_set_state(user_prompt, current_action_id, ActionState.STATUS_VERIFICATION_REQUESTED,"hook tracking lifecycle_hook_track_status_verification_request")
            return True

    return False


def resolve_receipt(user_prompt: str, evidence) -> Optional[tuple]:
    """The ONE rule for reading a completion receipt:
    ``(messages, index, message, agent)`` or None.

    A receipt lives in one of the lists the REUSE fabrication gate already
    treats as evidence (reuse_recipe._reuse_evidence_msg_lists):

      * the group log (``{'message_index': i, ...}``; ``agent`` is None), or
      * one participant's pairwise buffer
        (``{'source': 'buffer', 'agent': <participant name>,
        'peer': <counterpart name>, 'message_index': i, ...}``).

    Measured 2026-09-24 on nightly 8a11925: the gate credited tool runs that
    lived only in a buffer while every reader of the receipt indexed the group
    log, so 362 actions ended GAVE_UP and 0 committed.  The completion gate,
    the durable evidence record and the learning promotion all read the
    receipt through this function, so they cannot disagree about it again.
    """
    if not isinstance(evidence, dict):
        return None
    index = evidence.get('message_index')
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        return None
    group_chat = get_registered_groupchat(user_prompt)
    if group_chat is None:
        return None
    agent = None
    if evidence.get('source') == 'buffer':
        agent_name, peer_name = evidence.get('agent'), evidence.get('peer')
        # Only a participant of THIS group chat can hold the receipt.
        agent = next((a for a in (getattr(group_chat, 'agents', None) or [])
                      if getattr(a, 'name', None) == agent_name), None)
        conv = getattr(agent, '_oai_messages', None) if agent else None
        if not isinstance(conv, dict):
            return None
        messages = next((msgs for peer, msgs in conv.items()
                         if getattr(peer, 'name', peer) == peer_name), None)
    else:
        messages = getattr(group_chat, 'messages', None)
    if not isinstance(messages, list) or index >= len(messages):
        return None
    message = messages[index]
    if not isinstance(message, dict):
        return None
    return messages, index, message, agent


def evidence_sources(group_chat, agents):
    """The message lists a receipt can live in, each with its address.

    Yields ``(source, messages)``: ``source`` is None for the group log, or
    ``{'source': 'buffer', 'agent': <name>, 'peer': <name>}`` for one
    participant's pairwise buffer -- the address resolve_receipt reads a
    receipt back from.  The group log comes first.  The ONE definition: the
    REUSE fabrication gate and its receipt finder read it through
    reuse_recipe._reuse_evidence_sources, which delegates here, and
    derive_completion_evidence below reads it for CREATE.
    """
    yield None, getattr(group_chat, 'messages', None) or []
    for ag in (agents or []):
        conv = getattr(ag, '_oai_messages', None)
        if isinstance(conv, dict):
            for peer, msgs in conv.items():
                yield ({'source': 'buffer',
                        'agent': getattr(ag, 'name', None),
                        'peer': getattr(peer, 'name', peer)}, msgs)


def tool_call_names(msg_lists) -> Dict[str, str]:
    """call_id -> function name, read off the PROPOSING assistant message.

    A tool result's own ``name`` is the EXECUTING AGENT, never the function,
    so joining on the proposal's tool_call id is the only way to say which
    tool a result belongs to (measured 2026-09-06: the REUSE gate logged
    ``executed=['Assistant']`` reading the result's name).  The one rule, used
    by the completion gate below and by every REUSE evidence reader
    (reuse_recipe._reuse_call_id_to_tool_name delegates here).
    """
    out = {}
    for _ml in (msg_lists or []):
        for m in (_ml or []):
            if not isinstance(m, dict):
                continue
            for tc in (m.get('tool_calls') or []):
                _cid = (tc or {}).get('id')
                _fn = ((tc or {}).get('function') or {}).get('name')
                if _cid and _fn:
                    out[_cid] = _fn
    return out


def action_named_tools(agents, action_text):
    """(every tool these agents can SERVE, the ones the action's TEXT names).

    The ONE rule for "this action names that tool".  Read by the REUSE
    fabrication gate (reuse_recipe._reuse_fabricated_tools, through
    _reuse_registered_and_referenced_tools, which delegates here), by the
    completion gate below and by CREATE's trace banker
    (create_recipe._bank_action_recipe_from_trace).  It lived in
    reuse_recipe until 2026-10-05, so only REUSE asked it: the completion
    gate took ANY tool's reply as the receipt of an action that names a
    tool, so send_message_to_user's "Message sent" for "Please wait while
    I read the file" completed "execute_coding_task: read error log file
    ...", and the banker saved those messages as its recipe (live:
    prompts/26251890627_0_recipe.json, 2026-09-15, replayed by the coding
    daemon every dispatch since).

    "Can serve" is three sources, not two: already REGISTERED
    (_function_map), already in the SCHEMA (llm_config['tools']), and
    ATTACHABLE BY NAME (_hart_core_tools), the source whose absence made
    the REUSE gate blind (live 2026-09-10, agent 88719487304 action 4: an
    action naming execute_coding_task read like a prose action and
    advanced having run nothing).

    "Names" is _text_names_tool's reading, not a bare substring: a tool whose
    name is a plain English word (remember) is named only where the text
    writes it as a call.
    """
    names = set()
    for ag in (agents or []):
        try:
            names.update((getattr(ag, '_function_map', None) or {}).keys())
        except Exception:
            pass
        cfg = getattr(ag, 'llm_config', None)
        if isinstance(cfg, dict):
            for t in (cfg.get('tools') or []):
                fn = ((t or {}).get('function') or {}).get('name')
                if fn:
                    names.add(fn)
        # Closures this leg can attach BY NAME but has not attached yet.
        # attach_for_names(core_tools=...) serves exactly these, unpacking the
        # same (name, description, func) tuples build_core_tool_closures
        # returns -- so read [0] the way it does.
        #
        # Without this the two sources above only see tools ALREADY attached,
        # so a recipe-named closure was invisible until something else had
        # attached it, and _reuse_fabricated_tools returned [] at its
        # `if not referenced` early-return, which sits ABOVE its log line.
        # Measured live 2026-09-10, agent 88719487304 action 4 (rid
        # d60c-223537, taken AFTER 7fd83678f made these closures buildable):
        # watermark 12 -> 13, and it was the ONLY action of nine with no
        # `[FAB-GUARD] action N names tool(s)` verdict line at all.  An action
        # naming execute_coding_task read exactly like a prose action naming
        # nothing, and advanced having run nothing.
        # Population: 87 actions across 35 agents name execute_coding_task.
        for _ct in (getattr(ag, '_hart_core_tools', None) or []):
            try:
                _cn = _ct[0]
            except Exception:
                continue
            if _cn:
                names.add(_cn)
    text = str(action_text or '').lower()
    referenced = [n for n in names if _text_names_tool(text, n)]
    return names, referenced


#: A verb that calls a one-word tool name, with an optional article between:
#: "call remember", "use the remember", "calling remember", "via remember".
#: Not "with": "start with remember the names" is prose.
_TOOL_CALL_CUE = re.compile(
    r'\b(?:call|calls|calling|use|uses|using|run|runs|running|invoke|invokes|'
    r'invoking|execute|executes|executing|via)\s+(?:(?:the|this|your|a|an)\s+)?'
    r'[`"\']?$')
#: ... or the noun after it: "the remember tool", "remember function".
_TOOL_NOUN_AFTER = re.compile(r'[`"\']?\s+(?:tool|function)\b')


def _text_names_tool(text: str, name: str) -> bool:
    """Whether the (lowercased) action ``text`` names the tool ``name``.

    A name shaped like an identifier (an underscore, a digit, or a capital
    inside it: save_data_in_memory, crawl4ai, Generate_video) is not a word of
    prose, so it names the tool wherever it stands, as it always has.  A name
    that is one plain word is an English word too: live 2026-10-10, agent
    54's written reply step said "remember the interrupted one" and read as
    naming the memory tool `remember`, so the completion gate wanted that
    tool's result and the step GAVE_UP on a correct reply.  Such a name counts
    only where the text writes it as a call: followed by "(", after a calling
    verb (_TOOL_CALL_CUE), before "tool" / "function", or in quotes or
    backticks.  Measured on the desktop's 1,087 configs (5,889 actions): of
    1,653 (action, tool) matches, the three it drops are all the English
    "remember"."""
    n = str(name or '').lower()
    if len(n) <= 3 or n not in text:
        return False
    if '_' in name or any(c.isdigit() for c in name) or any(
            c.isupper() for c in name[1:]):
        return True
    for m in re.finditer(r'(?<![a-z0-9_])' + re.escape(n) + r'(?![a-z0-9_])',
                         text):
        after = text[m.end():m.end() + 1]
        if after == '(' or _TOOL_NOUN_AFTER.match(text, m.end()):
            return True
        if _TOOL_CALL_CUE.search(text[max(0, m.start() - 24):m.start()]):
            return True
        if (m.start() and text[m.start() - 1] in '`"\''
                and after and after in '`"\''):
            return True
    return False


def _tools_this_action_names(user_prompt: str, action_id: int):
    """(the action's text, the registered tools it names): the action read
    the way the completion gate reads it -- its ledger description -- and
    the tools this session's group chat can serve."""
    ledger = get_registered_ledger(user_prompt)
    task = (getattr(ledger, 'tasks', None) or {}).get(f'action_{action_id}')
    action_text = str(getattr(task, 'description', '') or '').lower()
    group_chat = get_registered_groupchat(user_prompt)
    return action_text, action_named_tools(
        getattr(group_chat, 'agents', None), action_text)[1]


def _receipt_is_real_work(user_prompt: str, action_id: int, messages,
                          message: dict) -> bool:
    """Whether a role='tool' receipt shows the action's work being done.

    Four things a tool message can carry that are not that work:

      * a FAILED reply (core.constants.tool_reply_failed): the executor's
        "Error: ...", the tool_logging envelope, or a TOOL_FAILURE_RESULTS
        refusal -- which is what an 'incomplete' computer-use run returns.
        The gate used to accept any non-empty tool message, so a refusal
        cited as the receipt completed the action it refused.
      * a BOOKKEEPING call (core.constants.BOOKKEEPING_TOOLS) the action does
        not itself name: live 2026-09-27, CREATE daemon_255bd83f, two
        execute_coding_task actions completed on save_data_in_memory writes of
        {"status": "completed"} the model composed, with no coding run.
      * ANY other tool's reply when the action names a tool
        (action_named_tools): an action that names a tool is done by that
        tool's work.  send_message_to_user's "Message sent" completed
        "execute_coding_task: read error log file ..." (#147).
      * the PLACEHOLDER helper.py back-fills for a call that returned nothing
        (core.constants.HISTORICAL_TOOL_PLACEHOLDER).  tool_reply_failed does
        not cover it by design and the REUSE readers skip it on their own;
        here it is skipped for the same reason, which matters most for
        derive_completion_evidence, where no model chose the message.

    An aggregate message (autogen's ``tool_responses``) is a receipt when any
    one of its results qualifies.  A result whose function cannot be resolved
    is judged on its content alone: it cannot be shown to be bookkeeping.  For
    an action that names a tool it is no receipt: it cannot be shown to be
    that tool's work either, the way the REUSE fabrication gate counts only a
    result it can resolve to a tool the action names.
    """
    names = tool_call_names([messages])
    action_text, named = _tools_this_action_names(user_prompt, action_id)
    responses = message.get('tool_responses')
    results = responses if isinstance(responses, list) and responses else [message]
    for result in results:
        if not isinstance(result, dict):
            continue
        body = result.get('content')
        if body is None:
            body = message.get('content')
        if (not str(body or '').strip() or tool_reply_failed(body)
                or HISTORICAL_TOOL_PLACEHOLDER in str(body)):
            continue
        name = names.get(result.get('tool_call_id') or message.get('tool_call_id'))
        if name in BOOKKEEPING_TOOLS and name.lower() not in action_text:
            continue
        if named and name not in named:
            continue
        return True
    return False


def _verifier_completion_has_conversation_evidence(
        user_prompt: str, action_id: int, json_obj: dict) -> bool:
    """Return whether a completion cites a real, earlier GroupChat result.

    The StatusVerifier JSON alone is never a receipt. The GroupChat registry is
    already the canonical conversation projection used by the Admin agent
    drawer, so this creates no second evidence store.
    """
    evidence = json_obj.get('evidence')
    if not isinstance(evidence, dict):
        return False
    kind = evidence.get('kind')
    if kind not in ('tool_receipt', 'user_visible_result'):
        return False
    resolved = resolve_receipt(user_prompt, evidence)
    if resolved is None:
        return False
    messages, index, message, agent = resolved
    if not str(message.get('content') or '').strip():
        return False
    # Bind the receipt to the action window already used by the stale-verdict
    # guard.  Otherwise a verifier can cite action 1's valid receipt while
    # completing action 2.  A missing dispatch marker is not evidence.  The
    # window is read in the SAME list that holds the receipt, so a buffer
    # (which also carries older turns) is held to the same rule as the log.
    if latest_dispatch_before(messages, index + 1) != action_id:
        return False
    if kind == 'tool_receipt':
        return (message.get('role') == 'tool'
                and _receipt_is_real_work(user_prompt, action_id, messages,
                                          message))
    # A written answer is only ever cited from the group log, and is the work
    # only of an action that names no tool: "I will read the error log file
    # and share what I find" was accepted as the read itself (#147).
    if _tools_this_action_names(user_prompt, action_id)[1]:
        return False
    return agent is None and is_written_answer(message)


# The roles the Assistant's own message carries in a group log.  A plain reply
# is 'user' there -- autogen's manager stores what it RECEIVED from a speaker
# as 'user' -- and a message that carries tool_calls is 'assistant'; a log
# rebuilt from the manager's buffer (#725, reuse_recipe._reuse_sync_group_log)
# can hold a plain reply as 'assistant' too.  Measured live 2026-10-06 (agent
# 54, state_transition's "Last message role" line): user/Assistant 15 times,
# every plain reply, and assistant/Assistant 26 times, the tool-calling ones.
# The receipt of a prose action was accepted only as 'assistant', so a plain
# lesson could never be one.  'tool' and 'function' are somebody's result.
_ASSISTANT_WRITTEN_ROLES = ('assistant', 'user')

# The tags that make a message routing.  The shared list (AGENT_MENTIONS: the
# seats REUSE's loop routes by) plus the two seats the CREATE loop also routes
# to, which that list does not name: 4b049241d moved this rule onto the shared
# list and, with it, let "@ChatInstructor Action 1 is done, please move on"
# complete a prose action again (reviewer finding D1).  They are not added to
# the shared list because REUSE's loop and send_message_to_user read their own
# copies of it for other decisions (task #166).
_ROUTING_MENTIONS = AGENT_MENTIONS + ("@chatinstructor", "@userproxy")


def _answer_value_is_text(value) -> bool:
    """Whether the value of a ``message2userfinal`` / ``message2`` answer key
    is text a person can read: not empty, and not an unfilled ``<...>``
    placeholder (the synthesis steer's own template, ``<your answer here>``).
    The rule of reuse_recipe._reuse_is_written_answer, which has to stay
    literal in that module (its extract-and-exec tests); a test pins the two
    equal."""
    text = str(value or '').strip()
    return bool(text) and not (text.startswith('<') and text.endswith('>'))


# What the pipeline's own prompts show as the example of a message to the
# person (create_recipe.py: "Your message here", "message here", "Your clear
# and useful message here"; reuse_recipe.py shows the last two under
# message2userfinal); a model that sends the example back has sent nothing.
_PROMPT_EXAMPLE_MESSAGES = ('your message here', 'message here',
                            'your clear and useful message here')

# The keys a message to the person travels under: REUSE's two answer keys,
# and message2user, the one CREATE's prompts teach (@user {"message2user":
# ...}), which REUSE's reply filter also reads as the answer.  REUSE's answer
# extractors (reuse_recipe.get_agent_response) unwrap the same keys.
ANSWER_KEYS = ('message2userfinal', 'message2', 'message2user')


#: What may wrap a prompt example and still leave it the example: spaces,
#: quotes, and closing or trailing marks (an ellipsis too).
_EXAMPLE_TRIM = ' .!?,;:"\'…'


def _is_answer_text(value) -> bool:
    """Whether an answer key's value is a message: readable text
    (_answer_value_is_text) that is not one of the prompts' own examples,
    however it is spaced, quoted or punctuated at its ends."""
    return (_answer_value_is_text(value)
            and ' '.join(str(value).split()).strip(_EXAMPLE_TRIM).lower()
            not in _PROMPT_EXAMPLE_MESSAGES)


def _is_control_message(text: str) -> bool:
    """Whether ``text`` is the pipeline's control JSON rather than prose: a
    status verdict object in any dress (bare, in a code fence, single-quoted,
    after a line of prose), or an answer-key envelope whose value is empty, an
    unfilled placeholder or one of the prompts' own examples.  It is the
    parse REUSE's reply filter uses (helper.retrieve_json), in REUSE's order:
    an answer key first (ANSWER_KEYS), so a status object that carries a real
    message to the person is the answer, as it is there; then status.  The
    Assistant's own ``{"status": "completed"}`` report, or the steer's
    template sent back as it stands, is refused here exactly where REUSE
    refuses it.  Fails closed: a text that cannot be read is control.  (A
    lesson that quotes a ``{"status": 404}`` example is refused too, as in
    REUSE.)

    A verdict or an answer key is an object, so text with no brace cannot be
    control and is not handed to the parser, which logs two INFO lines per
    call on prose."""
    if '{' not in text:
        return False
    try:
        from hartos.helper import retrieve_json
        parsed = retrieve_json(text)
    except Exception:
        return True
    if not isinstance(parsed, dict):
        return False
    for key in ANSWER_KEYS:
        if key in parsed:
            return not _is_answer_text(parsed[key])
    return 'status' in parsed


def is_written_answer(message) -> bool:
    """An Assistant message that is written work, as the completion gate, the
    CREATE derivation below and REUSE's receipt finder
    (reuse_recipe._reuse_completion_evidence) judge it.

    REUSE asks this AFTER its own reply filter (reuse_recipe.
    _reuse_message_is_user_answer), which also refuses the pipeline's own text
    (a steer, a dispatch echoed without its marker), the steering and verifier
    seats and a placeholder answer key.  CREATE asks only this, so those
    REUSE-only rules do not reach CREATE yet (task #171); what is here is the
    part the two share.

    It is the Assistant's (the seat name, in either role a log holds it), not
    the dispatch echoed back (a seat name does not always survive a log, and a
    dispatch is the pipeline's text, never the work), not a control JSON (the
    Assistant's own ``{"status": ...}`` report, or the steer's unfilled answer
    template, in any dress: _is_control_message),
    not an unexecuted tool call in the model's own markup, and not routing: a
    message that tags another agent (_ROUTING_MENTIONS: core.constants.
    AGENT_MENTIONS, ``@Helper``, ``@StatusVerifier``..., plus the loop's own
    ``@ChatInstructor`` and ``@UserProxy``) is addressed to that agent whatever
    else it says, the way REUSE reads it.  "Step 1 done. @StatusVerifier
    please verify." is a note, and the pipeline appends its memory-skeleton
    line to exactly such messages (create_recipe.state_transition).  A reply
    tagged to the person (``@user {"message2user": ...}``) is an answer."""
    if not (isinstance(message, dict) and message.get('name') == 'Assistant'
            and message.get('role') in _ASSISTANT_WRITTEN_ROLES):
        return False
    text = str(message.get('content') or '').strip()
    low = text.lower()
    if (not text or text == 'TERMINATE' or _DISPATCH_MARKER.match(text)
            or any(mention in low for mention in _ROUTING_MENTIONS)
            or '<tool_call>' in low or '<function=' in low):
        return False
    return not _is_control_message(text)


def _derive_written_answer(user_prompt: str, action_id: int,
                           group_chat) -> Optional[dict]:
    """The action's written answer, for an action that names no tool.

    The longest answer in the action's dispatch window (the lesson, not the
    one-line handoff that follows it; a tie goes to the newer), judged by the
    same gate a cited answer faces.  Read from the group log only: that gate
    accepts a written answer from no other list.
    """
    msgs = getattr(group_chat, 'messages', None)
    if not isinstance(msgs, list):
        return None
    best, best_size = None, -1
    for index, msg in enumerate(msgs):
        if not is_written_answer(msg):
            continue
        size = len(str(msg.get('content')).strip())
        if size < best_size:
            continue
        candidate = {'message_index': index, 'kind': 'user_visible_result'}
        if _verifier_completion_has_conversation_evidence(
                user_prompt, action_id, {'evidence': candidate}):
            best, best_size = candidate, size
    if best is not None:
        logger.info(
            "[COMPLETION-RECEIPT] action %s in %s: the verdict cited no "
            "answer the gate accepts; the action's written answer at group "
            "log message %s stands in", action_id, user_prompt,
            best['message_index'])
    else:
        logger.warning(
            "[COMPLETION-RECEIPT] action %s in %s: no written answer of "
            "this action in the group log (len=%s)",
            action_id, user_prompt, len(msgs))
    return best


def derive_completion_evidence(user_prompt: str,
                               action_id: int) -> Optional[dict]:
    """The action's own tool receipt, found by the pipeline.

    A completion has to cite a result from this action's dispatch window
    (_verifier_completion_has_conversation_evidence).  REUSE finds that result
    itself (reuse_recipe._reuse_completion_evidence); CREATE left it to the
    verifier MODEL, which has to count message indexes in a transcript of
    hundreds of messages.  Measured live on the 4B, 2026-10-05 23:53 (task
    #162): action 1, "get_data_by_key: read key tutor.progress ...", ran
    get_data_by_key and got a real answer; the verdict cited message 14, a
    get_chat_history reply; and every action of the run was refused three
    times and GAVE_UP -- no recipe banked, no agent built.

    This only LOCATES a candidate.  Every tool message, newest first, in the
    group log and then each participant's buffer (evidence_sources), is judged
    by the very gate a cited receipt faces, so a failed call, a note saved to
    memory, another tool's reply, a placeholder or another action's result is
    no receipt here either.  Returns the evidence in the shape resolve_receipt
    reads, or None.

    An action that names no tool is done by its WRITTEN ANSWER, not by a tool
    reply (_derive_written_answer): measured live on the same 4B 2026-10-06
    01:11, agent 54 as one prose action ("Teach exactly one next step ...
    Write the lesson as your reply to the learner") -- the Assistant wrote a
    good lesson and the verdict that cited the dispatch instead was refused.

    It never raises -- it sits on the verdict path -- and a miss says what
    was looked at.
    """
    try:
        group_chat = get_registered_groupchat(user_prompt)
        if group_chat is None:
            return None
        # REUSE splits the same way: a tool receipt for an action that names a
        # tool, the written answer for one that does not.  A tool reply in a
        # prose action's window is not its receipt.
        if not _tools_this_action_names(user_prompt, action_id)[1]:
            return _derive_written_answer(user_prompt, action_id, group_chat)
        looked = []
        for source, msgs in evidence_sources(
                group_chat, getattr(group_chat, 'agents', None)):
            if not isinstance(msgs, list):
                continue
            where = ('group log' if source is None
                     else f"{source['agent']}->{source['peer']} buffer")
            tool_messages = 0
            for index in range(len(msgs) - 1, -1, -1):
                msg = msgs[index]
                if not isinstance(msg, dict) or msg.get('role') != 'tool':
                    continue
                tool_messages += 1
                candidate = {**(source or {}), 'message_index': index,
                             'kind': 'tool_receipt'}
                if _verifier_completion_has_conversation_evidence(
                        user_prompt, action_id, {'evidence': candidate}):
                    logger.info(
                        "[COMPLETION-RECEIPT] action %s in %s: the verdict "
                        "cited no receipt the gate accepts; the action's own "
                        "tool result at %s message %s stands in",
                        action_id, user_prompt, where, index)
                    return candidate
            looked.append(f"{where}(len={len(msgs)}, "
                          f"tool_messages={tool_messages})")
        logger.warning(
            "[COMPLETION-RECEIPT] action %s in %s: no tool result of this "
            "action's work in %s", action_id, user_prompt,
            ', '.join(looked) or 'any list')
    except Exception:
        logger.warning(
            "[COMPLETION-RECEIPT] action %s in %s: the receipt search failed",
            action_id, user_prompt, exc_info=True)
    return None


def _record_verifier_evidence(user_prompt: str, action_id: int,
                              json_obj: dict) -> bool:
    """Persist the same receipt the completion gate accepted.

    ``verification_evidence`` is the existing distributed-verification field.
    Recording the accepted receipt there lets later recipe and learning code use
    one durable outcome instead of treating an LLM verdict as evidence.
    """
    ledger = get_registered_ledger(user_prompt)
    if ledger is None:
        # This is the verified-completion boundary, not a generic state helper.
        # Its only production callers are CREATE and REUSE, and both register
        # their ledger before execution.  Advancing without that durable
        # authority would turn an in-memory/model verdict into completion.
        logger.error(
            'Cannot persist verifier evidence: no ledger registered for %s',
            user_prompt)
        return False
    task = getattr(ledger, 'tasks', {}).get(f'action_{action_id}')
    if task is None:
        logger.error(
            'Cannot persist verifier evidence: action_%s missing from %s',
            action_id, user_prompt)
        return False
    context = getattr(task, 'context', None)
    if not isinstance(context, dict):
        logger.error(
            'Cannot persist verifier evidence: action_%s has no context',
            action_id)
        return False
    evidence = json_obj['evidence']
    resolved = resolve_receipt(user_prompt, evidence)
    if resolved is None:
        logger.error(
            'Cannot persist verifier evidence: receipt for action_%s is '
            'not readable', action_id)
        return False
    receipt = resolved[2]
    receipt_text = str(receipt.get('content') or '')
    receipt_hash = hashlib.sha256(receipt_text.encode('utf-8')).hexdigest()
    record = {
        'agent': 'StatusVerifier',
        'verdict': True,
        'action_id': action_id,
        'evidence': evidence,
        'receipt_sha256': receipt_hash,
        'receipt_role': str(receipt.get('role') or ''),
        'receipt_agent': str(receipt.get('name') or ''),
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    records = context.setdefault('verification_evidence', [])
    already_persisted = any(
        isinstance(item, dict)
        and item.get('action_id') == action_id
        and item.get('receipt_sha256') == receipt_hash
        for item in records
    )
    if not already_persisted:
        records.append(record)
        if ledger.save() is False:
            records.remove(record)
            logger.error(
                'Refusing completion: verifier evidence was not persisted '
                'for action %s in %s', action_id, user_prompt)
            return False
    return True


def _promote_verified_outcome(user_prompt: str, action_id: int,
                              json_obj: dict) -> None:
    """Credit one outcome after evidence and lifecycle completion commit."""
    ledger = get_registered_ledger(user_prompt)
    task = (getattr(ledger, 'tasks', {}).get(f'action_{action_id}')
            if ledger is not None else None)
    context = getattr(task, 'context', None)
    if not isinstance(context, dict):
        context = {}
    resolved = resolve_receipt(user_prompt, json_obj.get('evidence'))
    # A buffer receipt names the participant that holds it; credit exactly
    # that participant, never one inferred from the group log.
    receipt_agent = resolved[3] if resolved else None
    # CREATE and REUSE already declare these two assistant identities when
    # instrumenting their real GroupChat participant.  Award only wrappers
    # that are live for this session; a verifier verdict remains insufficient.
    try:
        from integrations.agent_lightning import record_verified_outcome_for_agents
        outcome_context = {
            'user_prompt': user_prompt,
            'action_id': action_id,
            'evidence': json_obj['evidence'],
        }
        group_chat = get_registered_groupchat(user_prompt)
        credited = record_verified_outcome_for_agents(
            [receipt_agent] if receipt_agent is not None
            else getattr(group_chat, 'agents', None),
            True, outcome_context)
        if not credited:
            # A False here is NOT "the reward was recorded".  It means no live
            # wrapper owned this chat, which happens whenever Agent Lightning
            # is off (the code default) or the session's wrapper has gone.
            # Phase 3 of the recovery plan requires that we say so rather than
            # let the caller read silence as learning.
            from integrations.agent_lightning.config import is_enabled
            if is_enabled():
                logger.warning(
                    'Verified outcome for action %s in %s was NOT credited: '
                    'Agent Lightning is enabled but no instrumented wrapper '
                    'owns this GroupChat', action_id, user_prompt)
            else:
                logger.debug(
                    'Verified outcome for action %s in %s not credited: '
                    'Agent Lightning is disabled', action_id, user_prompt)
    except Exception:
        logger.exception('Unable to record verified Agent Lightning outcome')
    # Promote the already-persisted action/result pair through the one world
    # model bridge only after this exact receipt committed COMPLETED.  The
    # bridge independently checks the verification envelope, so no dispatcher
    # or worker can bypass the lifecycle by self-attesting success.
    try:
        evidence = json_obj['evidence']
        if resolved is None:
            raise ValueError('receipt not readable')
        receipt = resolved[2]
        user_id, prompt_id = _extract_ownership_from_prompt(user_prompt)
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge,
        )
        get_world_model_bridge().record_interaction(
            user_id=user_id,
            prompt_id=prompt_id or user_prompt,
            prompt=str(getattr(task, 'description', '') or
                       f'Complete action {action_id}'),
            response=str(receipt.get('content') or ''),
            model_id=str(receipt.get('name') or 'verified-agent'),
            goal_id=(context.get('goal_id') or context.get('parent_task_id')),
            verification={
                'verified': True,
                'source': 'status_verifier',
                'outcome': 'success',
                'action_id': action_id,
                'evidence': evidence,
            },
            # The raw chat path already owns history and user-sensor writes.
            # This call promotes that result; it must not duplicate either.
            persist_conversation=False,
            ingest_user_utterance=False,
        )
    except Exception:
        logger.exception('Unable to record verified world-model outcome')


def _flow_has_action(user_prompt: str, action_id: int) -> bool:
    """Whether this session's flow has an action with that id.

    The registered ledger holds one ``action_<id>`` task per action
    (add_actions_to_ledger, for CREATE and REUSE alike), and a completion is
    only ever recorded against it (_record_verifier_evidence), so the flow a
    completion is judged in always has one.
    """
    return f'action_{action_id}' in (
        getattr(get_registered_ledger(user_prompt), 'tasks', None) or {})


def commit_verified_action_completion(user_prompt: str, action_id: int,
                                      evidence: dict,
                                      reason: str = 'verified complete',
                                      claimed_action_id=None) -> bool:
    """Commit one evidence-backed successful action through the canonical FSM.

    CREATE and REUSE use different non-deterministic conversations, but a
    successful action has one deterministic boundary: it must already be in
    ``STATUS_VERIFICATION_REQUESTED`` and cite a receipt from that action's
    current dispatch window.  Keeping the state write, durable ledger evidence,
    Agent Lightning reward, and world-model promotion together prevents a
    caller from advancing the pointer while silently skipping the flywheel.

    ``claimed_action_id`` is the action_id the verdict itself names, when it
    names one.  A verdict completes only the action it names: live 2026-09-27
    (REUSE daemon_goal_..._b18bba6f, 17:35:28) a StatusVerifier verdict for
    action_id 2 -- "System health check completed successfully" -- completed
    action 4.  That is a verdict about ANOTHER ACTION OF THE FLOW.  An id the
    flow does not have names no action, so it cannot be a stale verdict for
    one; it is a wrong number, and live 2026-10-06 the 4B verifier wrote 2, 4
    and 6 on the verdicts of agent 54, whose recipe has ONE action (18
    refusal lines in the two retained logs).
    settled_action_id still decides which action a verdict is ABOUT (its
    recipe, its log line); this decides only whether it may COMPLETE one.  A
    verdict that names no action_id, or one the flow does not have, is not
    contradicting anything.
    """
    if claimed_action_id is not None:
        try:
            _claimed = int(float(claimed_action_id))
        except (TypeError, ValueError):
            _claimed = None
        if (_claimed is not None and _claimed != int(action_id)
                and _flow_has_action(user_prompt, _claimed)):
            logger.warning(
                "Refusing completion of action %s in %s: the verdict names "
                "action %s", action_id, user_prompt, _claimed)
            return False
    if get_action_state(user_prompt, action_id) != \
            ActionState.STATUS_VERIFICATION_REQUESTED:
        logger.warning(
            "Refusing verified completion for action %s in %s from state %s",
            action_id, user_prompt,
            get_action_state(user_prompt, action_id).value)
        return False

    verdict = {
        'status': 'completed',
        'action_id': action_id,
        'evidence': evidence,
    }
    if not _verifier_completion_has_conversation_evidence(
            user_prompt, action_id, verdict):
        logger.warning(
            "Refusing ungrounded completed verdict for action %s in %s",
            action_id, user_prompt)
        return False
    if not validate_state_transition(
            user_prompt, action_id, ActionState.COMPLETED):
        return False
    receipt = resolve_receipt(user_prompt, evidence)[2]
    # The proof must be durable before a completion can release dependents or
    # feed any learning system. A retry deduplicates the same receipt hash.
    if not _record_verifier_evidence(user_prompt, action_id, verdict):
        return False
    if not safe_set_state(
            user_prompt, action_id, ActionState.COMPLETED, reason,
            result=str(receipt.get('content') or '')):
        return False

    # Learning follows both durable evidence and the successful canonical
    # state write. Never credit a receipt which failed either boundary.
    _promote_verified_outcome(user_prompt, action_id, verdict)
    retry_tracker.reset_count(user_prompt, action_id)
    return True


def lifecycle_hook_process_verifier_response(user_prompt: str, json_obj: dict, user_tasks) -> dict:
    """4-6. Process verifier response: completed/pending/error"""
    # isinstance before the membership test: helper.retrieve_json returns
    # json.loads(repair_json(...)), and repair_json turns model prose into a
    # LIST as readily as a dict.  On a list `'status' not in json_obj` is an
    # ELEMENT test, so a list containing the string 'status' passed this guard
    # and `json_obj['status']` below raised
    # "TypeError: list indices must be integers or slices, not str" —
    # measured on central 2026-09-02 12:43:29Z, the first goal turn after the
    # LLM was repaired, delivered to the user as "I couldn't finish that".
    # Same idiom already used for this value at create_recipe.py:2392 and 4921.
    if not isinstance(json_obj, dict) or 'status' not in json_obj:
        return {'action': 'allow', 'message': None}

    if hasattr(user_tasks, 'get') and not hasattr(user_tasks, 'current_action'):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return {'action': 'allow', 'message': None}
        current_action_id = current_tasks.current_action
    elif hasattr(user_tasks, 'current_action'):
        current_action_id = user_tasks.current_action
    else:
        return {'action': 'allow', 'message': None}

    status = json_obj['status'].lower()
    current_state = get_action_state(user_prompt, current_action_id)

    # A completed verdict IS the verification, so an action still IN_PROGRESS
    # when it arrives is moved to STATUS_VERIFICATION_REQUESTED and judged
    # here.  CREATE routes the Assistant's turn to the verifier without an
    # "@StatusVerifier" mention (state_transition's after-Assistant branch), so
    # the state never moved and this hook answered 'allow'; the TERMINATE that
    # follows every verdict then walked the action through COMPLETED with no
    # receipt (live 2026-09-27 14:26:19, daemon_255bd83f action 1).
    if (status in ('completed', 'success')
            and current_state == ActionState.IN_PROGRESS
            and safe_set_state(
                user_prompt, current_action_id,
                ActionState.STATUS_VERIFICATION_REQUESTED,
                "hook tracking lifecycle_hook_process_verifier_response: "
                "verdict arrived")):
        current_state = ActionState.STATUS_VERIFICATION_REQUESTED

    # Must be in STATUS_VERIFICATION_REQUESTED to process verifier response
    if current_state != ActionState.STATUS_VERIFICATION_REQUESTED:
        return {'action': 'allow', 'message': None}

    if status in ('completed', 'success'):
        # The citation is the verifier model's to get right, and a small
        # model's to get wrong; the receipt is the pipeline's to find.  A
        # citation the gate accepts is used as cited, so a model that does
        # count the transcript is unchanged.  The verdict still has to name
        # THIS action (claimed_action_id below).
        evidence = json_obj.get('evidence')
        if not _verifier_completion_has_conversation_evidence(
                user_prompt, current_action_id, json_obj):
            evidence = derive_completion_evidence(
                user_prompt, current_action_id) or evidence
        if not commit_verified_action_completion(
                user_prompt, current_action_id, evidence,
                "hook tracking lifecycle_hook_process_verifier_response",
                claimed_action_id=json_obj.get('action_id')):
            # Bounded, on the same per-action counter a 'pending' verdict
            # uses: an unverifiable claim is not a completion, and repeating
            # it must not loop.  After the bound the action is recorded
            # GAVE_UP -- an honest, retryable failure, never COMPLETED.
            if retry_tracker.increment_pending(user_prompt, current_action_id):
                retry_tracker.reset_count(user_prompt, current_action_id)
                force_state_through_valid_path(
                    user_prompt, current_action_id, ActionState.GAVE_UP,
                    "completion claimed without a verifiable receipt")
                return {
                    'action': 'gave_up',
                    'message': (
                        f"Action {current_action_id} could not be verified: "
                        "no completed verdict for it cited a real result.")
                }
            return {
                'action': 'force_completion',
                'message': (
                    f"Action {current_action_id} is not complete yet: the "
                    f"StatusVerifier must report action_id {current_action_id} "
                    "and cite an earlier tool receipt that did this action's "
                    "work (not a note saved to memory, not a failed call) or a "
                    "user-visible result from this conversation.")
            }
        # Automatically request fallback after completion
        return {
            'action': 'force_fallback',
            'message': f"Action {current_action_id} fallback: ask user what actions should be taken if current actions fail in the future after you get the response from user give the conversation to StatusVerifier agent"
        }

    elif status == 'pending':
        # SAFETY NET: Check if pending count exceeded (prevents infinite retry loops)
        if retry_tracker.increment_pending(user_prompt, current_action_id):
            # Force transition to ERROR if pending too many times
            logger.error(f"[SAFETY NET] Action {current_action_id} exceeded max pending retries - forcing ERROR state")
            status = 'error'  # Override to error
            json_obj['message'] = f"Action failed after {retry_tracker.MAX_PENDING_RETRIES} retry attempts. Original message: {json_obj.get('message', 'No details')}"
            # Fall through to error handling below

        if status == 'pending':  # Still pending (not overridden)
            if validate_state_transition(user_prompt, current_action_id, ActionState.PENDING):
                needs_user = autonomy_needs_user(
                    json_obj.get('can_perform_without_user_input'))
                if needs_user:
                    mark_action_waiting_for_user(
                        user_prompt, current_action_id,
                        json_obj.get('message') or 'Waiting for user input')
                else:
                    safe_set_state(user_prompt, current_action_id, ActionState.PENDING,"hook tracking lifecycle_hook_process_verifier_response")
                return {
                    'action': 'force_completion',
                    'message': (
                        (json_obj.get('message') or f'Action {current_action_id} needs user input')
                        if needs_user else
                        f"Complete pending steps for action {current_action_id} and ask @StatusVerifier to verify completion"
                    )
                }

    if status == 'error':  # Separated to allow fall-through from pending override
        # Reset retry counter on error (will start fresh if retried)
        retry_tracker.reset_count(user_prompt, current_action_id)
        if validate_state_transition(user_prompt, current_action_id, ActionState.ERROR):
            safe_set_state(user_prompt, current_action_id, ActionState.ERROR,"hook tracking lifecycle_hook_process_verifier_response")
            # FIX: Automatically request fallback for failed actions (like completed actions)
            # This allows ERROR to progress toward TERMINATED instead of getting stuck
            return {
                'action': 'force_fallback',
                'message': f"Action {current_action_id} failed: {json_obj.get('message', 'Unknown error')}. Please provide fallback actions for future failures of this type, then we'll create the recipe and move forward."
            }

    return {'action': 'allow', 'message': None}


def lifecycle_hook_track_fallback_request(user_prompt: str, user_tasks, group_chat) -> bool:
    """7. Track when fallback is requested to user"""
    if hasattr(user_tasks, 'get'):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return False
        current_action_id = current_tasks.current_action
    elif hasattr(user_tasks, 'current_action'):
        current_action_id = user_tasks.current_action
    else:
        return False

    # When fallback is requested, move to FALLBACK_REQUESTED
    if (group_chat.messages and
        'fallback' in group_chat.messages[-1]['content'].lower() and
        'ask user' in group_chat.messages[-1]['content'].lower()):

        if validate_state_transition(user_prompt, current_action_id, ActionState.FALLBACK_REQUESTED):
            safe_set_state(user_prompt, current_action_id, ActionState.FALLBACK_REQUESTED,"hook tracking lifecycle_hook_track_fallback_request")
            return True

    return False


def lifecycle_hook_track_user_fallback(user_prompt: str, user_tasks, group_chat) -> bool:
    """8. Track when fallback is received from user"""
    if hasattr(user_tasks, 'get'):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return False
        current_action_id = current_tasks.current_action
    elif hasattr(user_tasks, 'current_action'):
        current_action_id = user_tasks.current_action
    else:
        return False

    current_state = get_action_state(user_prompt, current_action_id)

    # When user responds to fallback request
    if (current_state == ActionState.FALLBACK_REQUESTED and
        group_chat.messages and
        group_chat.messages[-1]['name'] == 'UserProxy'):

        if validate_state_transition(user_prompt, current_action_id, ActionState.FALLBACK_RECEIVED):
            safe_set_state(user_prompt, current_action_id, ActionState.FALLBACK_RECEIVED,"hook tracking lifecycle_hook_track_user_fallback")
            return True

    return False


def lifecycle_hook_track_recipe_request(user_prompt: str, user_tasks, group_chat) -> bool:
    """9. Track when recipe creation is requested"""
    if hasattr(user_tasks, 'get'):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return False
        current_action_id = current_tasks.current_action
    elif hasattr(user_tasks, 'current_action'):
        current_action_id = user_tasks.current_action
    else:
        return False

    # When recipe creation is requested
    if (group_chat.messages and
        is_recipe_creation_request(group_chat.messages[-1].get('content'))):

        if validate_state_transition(user_prompt, current_action_id, ActionState.RECIPE_REQUESTED):
            safe_set_state(user_prompt, current_action_id, ActionState.RECIPE_REQUESTED,"hook tracking lifecycle_hook_track_recipe_request")
            return True

    return False


def lifecycle_hook_track_recipe_completion(user_prompt: str, json_obj: dict, user_tasks) -> dict:
    """10. Track when recipe is received and saved"""

    # Same list-vs-dict guard as lifecycle_hook_process_verifier_response: this
    # one would raise AttributeError on .get rather than TypeError, but it is
    # fed by the same retrieve_json call sites.
    if (not isinstance(json_obj, dict) or 'status' not in json_obj
            or json_obj.get('status', '').lower() != 'done'):
        return {'action': 'allow', 'message': None}

    if hasattr(user_tasks, 'get') and not hasattr(user_tasks, 'current_action'):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return {'action': 'allow', 'message': None}
        current_action_id = current_tasks.current_action
    elif hasattr(user_tasks, 'current_action'):
        current_action_id = user_tasks.current_action
    else:
        return {'action': 'allow', 'message': None}

    current_state = get_action_state(user_prompt, current_action_id)

    if current_state == ActionState.RECIPE_REQUESTED:
        if validate_state_transition(user_prompt, current_action_id, ActionState.RECIPE_RECEIVED):
            safe_set_state(user_prompt, current_action_id, ActionState.RECIPE_RECEIVED,"hook tracking lifecycle_hook_track_recipe_completion")
            return {
                'action': 'save_recipe_and_terminate',
                'message': f"Recipe received for action {current_action_id}. Save and proceed to termination."
            }

    return {'action': 'allow', 'message': None}


# "Execute Action N:" is how CREATE posts an action, while REUSE uses
# "Perform this action -> Action #N:".  Both are projections of the same
# lifecycle dispatch and therefore share this parser.  CREATE also emits
# "Properly Execute Action N:" and retry posts prefixed with "[retry:<tag>]".
# Only a LEADING marker is a dispatch: the
# [EXECUTE-PENDING] dispatch appends the user's text after its own marker, and
# that text can quote an earlier one ("... ,Latest User message: Properly
# Execute Action 6: ...", the Failure=True retry text), so a marker later in a
# message says nothing about which action it posts.  Colon-delimited, so
# action 2 never matches action 20.  Every producer therefore puts its marker
# FIRST, including reuse_recipe._reuse_seed_message (action 1: dispatch, then
# the user's words); a seed built user-words-first was invisible here and no
# REUSE action 1 could commit (2026-09-25).
_DISPATCH_MARKER = re.compile(
    r'\s*(?:'
    r'(?:\[retry:[^\]]*\]\s*)?(?:Properly\s+)?Execute Action '
    r'|Perform this action -> Action #'
    r')(?P<action_id>\d+):')


def dispatch_action_id(content) -> Optional[int]:
    """The action a message dispatches, or None when it is not a dispatch."""
    if not isinstance(content, str):
        return None
    m = _DISPATCH_MARKER.match(content)
    return int(m.group('action_id')) if m else None


def latest_dispatch_before(messages, index) -> Optional[int]:
    """The action id of the latest dispatch posted before ``messages[index]``,
    or None when none precedes it.

    Seeded messages (``_from_shared``) do not count: an earlier run's marker in
    the shared history says nothing about which action this run is on.
    """
    try:
        earlier = messages[:index]
    except Exception:
        logger.warning("latest_dispatch_before: cannot slice messages at %r",
                       index, exc_info=True)
        return None
    for m in reversed(earlier):
        if not isinstance(m, dict) or m.get('_from_shared'):
            continue
        aid = dispatch_action_id(m.get('content'))
        if aid is not None:
            return aid
    return None


def stale_for_unstarted_action(messages, index, user_prompt,
                               action_id) -> Optional[int]:
    """The action ``messages[index]`` belongs to, returned only when it is a
    message left over for an action that has NOT started; otherwise None, and
    callers keep their old behaviour.

    Both conditions must hold: the current action is still ASSIGNED, and the
    latest dispatch before the message names another action.  Measured on
    central 2026-09-13 (#101): after [ADVANCE] N->N+1 the old verdict and
    TERMINATE stay the last messages, and the termination hook and the create
    loop's verdict pickup credited them to N+1 before it ran, so every executed
    action was followed by a phantom completion of the next.  The state
    condition matters as much as the marker: a started action's own rounds
    (a recipe request, a fallback request, a claim rejection, a "continue"
    nudge) carry no dispatch marker of their own, and reading only markers
    handed them to the previous action (#101 review, 2026-09-14).
    """
    try:
        aid = int(action_id)
    except (TypeError, ValueError):
        logger.warning("stale check: action_id %r is not an int; treating the "
                       "message as the current action's", action_id)
        return None
    if get_action_state(user_prompt, aid) != ActionState.ASSIGNED:
        return None
    owner = latest_dispatch_before(messages, index)
    if owner is not None and owner != aid:
        return owner
    return None


def settled_action_id(claimed_action_id, current_action_id) -> int:
    """The action a verdict settles: the one the pipeline posted.

    The model's action_id is advisory.  A verdict for the action on the floor
    that names another id (a mistyped number, a future id, an id copied from
    an earlier verdict) still answers the posted action, so it settles that
    one, and the mismatch is logged as a hallucination signal.  This is the
    one home of the rule: reuse's _advance_or_steer, the create loop's verdict
    pickup and create's state_transition all call it.  Before it,
    state_transition trusted the claimed id, so a verdict naming action 3
    while action 2 ran force-completed action 3 before it was posted, and one
    naming action 1 rewrote action 1's text (both reproduced by
    tests/unit/test_create_loop_end_to_end.py).

    A verdict left over for an action that has not started is not this case;
    stale_for_unstarted_action refuses that one first.
    """
    try:
        current = int(current_action_id)
    except (TypeError, ValueError):
        logger.warning("settled_action_id: the posted action %r is not an int; "
                       "leaving it as it is", current_action_id)
        return current_action_id
    try:
        claimed = int(float(claimed_action_id))
    except (TypeError, ValueError):
        return current
    if claimed != current:
        logger.warning(
            "[HALLUCINATION?] LLM claims action_id=%s but pipeline has %s",
            claimed, current)
    return current


def lifecycle_hook_track_termination(user_prompt: str, user_tasks, group_chat) -> bool:
    """11. Track when action is terminated and passed to chat instructor"""
    if hasattr(user_tasks, 'get'):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return False
        current_action_id = current_tasks.current_action
    elif hasattr(user_tasks, 'current_action'):
        current_action_id = user_tasks.current_action
    else:
        return False

    # When TERMINATE is issued
    if (group_chat.messages and
        group_chat.messages[-1]['content'] == 'TERMINATE'):

        # A TERMINATE left over from the previous action is not this one's
        # (#101): after an advance it is still the last message, and the new
        # action has not started yet.
        _owner = stale_for_unstarted_action(
            group_chat.messages, -1, user_prompt, current_action_id)
        if _owner is not None:
            logger.info(
                "[STALE-TERMINATE] the last TERMINATE belongs to action %s; "
                "not terminating action %s, which has not started",
                _owner, current_action_id)
            return False

        # A TERMINATE closes an action; it never CERTIFIES one.  ChatInstructor
        # answers every verdict with TERMINATE (its default_auto_reply), so
        # this hook -- which runs at the top of each create-loop lap, BEFORE
        # the verdict pickup -- used to walk an unverified action
        # (ASSIGNED/IN_PROGRESS/STATUS_VERIFICATION_REQUESTED/PENDING)
        # through COMPLETED to TERMINATED with no receipt.  That write is the
        # fabricated "[TARGET] Action 1: status_verification_requested ->
        # completed (auto-path: hook tracking lifecycle_hook_track_termination)"
        # of live 2026-09-27 14:26:20 (CREATE daemon_255bd83f): two
        # execute_coding_task actions COMPLETED with zero coding runs.
        #
        # So such an action is left open here, for the verdict pickup and its
        # canonical gate (commit_verified_action_completion) to complete or
        # refuse -- bounded there, ending GAVE_UP rather than looping.  The
        # 2026-06-13 'assigned -> terminated' stall this path once escaped is
        # bounded by that gate and by [EXECUTE-PENDING]'s three attempts.
        # Verified (COMPLETED / RECIPE_RECEIVED) and recovery-edge
        # terminations are unchanged.
        if force_state_through_valid_path(
                user_prompt, current_action_id, ActionState.TERMINATED,
                "hook tracking lifecycle_hook_track_termination",
                through_completed=False):
            return True

    return False


# lifecycle_hook_publish_narration was removed: it published a second
# JSON format ({"type": "narration", ...}) to the same Crossbar topic
# that publish_intermediate_thoughts_to_user / publish_agent_thought
# use for agent-to-agent thinking bubbles. Two publishers emitting
# two shapes to one topic is shadow code — the thinking-prompts
# publisher in create_recipe is the single canonical path. Call that
# from new callers instead of resurrecting this hook.


def lifecycle_hook_can_increment_action(user_prompt: str, user_tasks) -> dict:
    """12. Check if we can increment to next action"""
    if hasattr(user_tasks, 'get') and not hasattr(user_tasks, 'current_action'):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return {'action': 'allow', 'message': None}
        current_action_id = current_tasks.current_action
    elif hasattr(user_tasks, 'current_action'):
        current_action_id = user_tasks.current_action
    else:
        return {'action': 'allow', 'message': None}

    current_state = get_action_state(user_prompt, current_action_id)

    if current_state != ActionState.TERMINATED:
        return {
            'action': 'block',
            'message': f"Cannot increment to next action. Action {current_action_id} must reach TERMINATED state first. Current state: {current_state.value}"
        }

    return {'action': 'allow', 'message': None}



def lifecycle_hook_check_all_actions_terminated(user_prompt: str, user_tasks) -> dict:
    """13. Check if all actions in array are exhausted and can create flow recipe"""
    if hasattr(user_tasks, 'get') and not hasattr(user_tasks, 'current_action'):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return {'action': 'allow', 'message': None}
        total_actions = len(current_tasks.actions)
        current_action = current_tasks.current_action
    elif hasattr(user_tasks, 'current_action'):
        total_actions = len(user_tasks.actions)
        current_action = user_tasks.current_action
    else:
        return {'action': 'allow', 'message': None}

    # Check if all actions completed
    if current_action > total_actions:
        # Verify all actions reached TERMINATED state
        incomplete_actions = []
        for action_id in range(1, total_actions + 1):
            state = get_action_state(user_prompt, action_id)
            if state != ActionState.TERMINATED:
                incomplete_actions.append(f"Action {action_id}: {state.value}")

        if incomplete_actions:
            return {
                'action': 'block_flow_completion',
                'message': f"Cannot create flow recipe. Incomplete actions: {incomplete_actions}"
            }

        return {
            'action': 'create_flow_recipe',
            'message': "All actions completed. Create flow recipe for personas."
        }

    return {'action': 'continue_actions', 'message': None}


def lifecycle_hook_validate_final_agent_creation(user_prompt: str, user_tasks, prompt_id: int) -> dict:
    """16. Final validation before 'Agent created successfully'"""

    # Check 1: All actions reached TERMINATED
    if hasattr(user_tasks, 'get') and not hasattr(user_tasks, 'current_action'):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return {'action': 'block', 'message': 'No tasks found'}
        total_actions = len(current_tasks.actions)
    elif hasattr(user_tasks, 'actions'):
        total_actions = len(user_tasks.actions)
    else:
        return {'action': 'block', 'message': 'No tasks found'}

    for action_id in range(1, total_actions + 1):
        state = get_action_state(user_prompt, action_id)
        if state != ActionState.TERMINATED:
            return {
                'action': 'block',
                'message': f"Action {action_id} not terminated. Current state: {state.value}"
            }

    # Check 2: All recipe files exist
    flow = 0  # Assuming recipe_for_persona logic
    missing_files = []

    for action_id in range(1, total_actions + 1):
        recipe_file = os.path.join(PROMPTS_DIR, f'{prompt_id}_{flow}_{action_id}.json')
        if not os.path.exists(recipe_file):
            missing_files.append(recipe_file)

    if missing_files:
        return {
            'action': 'block',
            'message': f"Missing recipe files: {missing_files}"
        }

    # Check 3: Flow recipe exists
    flow_recipe_file = os.path.join(PROMPTS_DIR, f'{prompt_id}_{flow}_recipe.json')
    if not os.path.exists(flow_recipe_file):
        return {
            'action': 'block',
            'message': f"Missing flow recipe: {flow_recipe_file}"
        }

    # Check 4: Timer tasks executed (if required)
    # This would need additional tracking based on your timer execution logic

    return {
        'action': 'allow',
        'message': "[OK] All validations passed - Agent creation ready"
    }


def debug_action_flow(user_prompt: str, action_id: int):
    """Debug specific action's flow pattern"""
    states = action_states.get(user_prompt, {})
    if action_id not in states:
        logger.info(f"Action {action_id}: Not started")
        return

    current_state = states[action_id]

    # Determine which of the 4 flows this matches
    if current_state == ActionState.TERMINATED:
        logger.info(f"[OK] Action {action_id}: COMPLETED one of the 4 flows")
    elif current_state == ActionState.ERROR:
        logger.info(f"[PROCESSING] Action {action_id}: In ERROR (Flow #2 or #3)")
    elif current_state == ActionState.PENDING:
        logger.info(f"[PROCESSING] Action {action_id}: In PENDING (Flow #3 or #4)")
    else:
        logger.info(f"[PROCESSING] Action {action_id}: In progress ({current_state.value})")


# 6. ADD validation function for the 4 specific flows:
def validate_flow_pattern(user_prompt: str, action_id: int) -> str:
    """Identify which of the 4 flows this action followed"""
    # This would need action history tracking to be fully implemented
    # For now, just return current state info
    current_state = get_action_state(user_prompt, action_id)

    if current_state == ActionState.TERMINATED:
        return "completed_flow"
    elif current_state == ActionState.ERROR:
        return "error_flow_in_progress"
    elif current_state == ActionState.PENDING:
        return "pending_flow_in_progress"
    else:
        return "flow_in_progress"


def debug_lifecycle_status(user_prompt: str):
    """Debug function to show current lifecycle status"""
    states = action_states.get(user_prompt, {})
    logger.info(f"\n[STATUS] Lifecycle Status for {user_prompt}:")
    logger.info("-" * 50)

    for action_id, state in states.items():
        terminated = "[OK] TERMINATED" if state == ActionState.TERMINATED else "[PROCESSING] IN PROGRESS"
        logger.info(f"Action {action_id}: {state.value} {terminated}")


def initialize_deterministic_actions():
    """Initialize the state machine"""
    logger.info("[TARGET] Deterministic action lifecycle initialized")
    return True


def initialize_minimal_lifecycle_hooks():
    """Alias for initialization"""
    return initialize_deterministic_actions()


# =============================================================================
# LEDGER RESTORE FUNCTIONS (reverse sync: Ledger → ActionState)
# =============================================================================

def restore_action_states_from_ledger(user_prompt: str, ledger) -> int:
    """
    Restore action_states from SmartLedger task statuses.

    This is the reverse of sync_action_state_to_ledger — when a user returns
    after cache eviction/expiry, this rebuilds the in-memory action_states
    from the persisted SmartLedger.

    Args:
        user_prompt: The user_prompt key (e.g., "123_456")
        ledger: A SmartLedger instance with tasks loaded from Redis/JSON

    Returns:
        int: Number of action states restored
    """
    try:
        LedgerTaskStatus = _get_ledger_task_status()
    except Exception:
        return 0

    # Reverse mapping: LedgerTaskStatus → ActionState
    REVERSE_MAP = {
        LedgerTaskStatus.PENDING: ActionState.ASSIGNED,
        LedgerTaskStatus.IN_PROGRESS: ActionState.IN_PROGRESS,
        LedgerTaskStatus.COMPLETED: ActionState.TERMINATED,
        LedgerTaskStatus.BLOCKED: ActionState.PENDING,
        LedgerTaskStatus.FAILED: ActionState.ERROR,
        LedgerTaskStatus.PAUSED: ActionState.FALLBACK_REQUESTED,
        LedgerTaskStatus.DELEGATED: ActionState.IN_PROGRESS,
        LedgerTaskStatus.TERMINATED: ActionState.TERMINATED,
    }

    restored = 0
    with _state_lock:
        for task_id, task in ledger.tasks.items():
            if not task_id.startswith('action_'):
                continue
            try:
                action_id = int(task_id.split('_')[1])
            except (IndexError, ValueError):
                continue

            action_state = REVERSE_MAP.get(task.status, ActionState.ASSIGNED)
            if user_prompt not in action_states:
                action_states[user_prompt] = {}
            action_states[user_prompt][action_id] = action_state
            restored += 1

    if restored > 0:
        logger.info(f"Restored {restored} action states from ledger for {user_prompt}")
    return restored


# =============================================================================
# LEDGER SYNC FUNCTIONS
# =============================================================================

def sync_action_state_to_ledger(
    user_prompt: str,
    action_id: int,
    state: ActionState,
    user_ledgers: Dict[str, Any]
) -> bool:
    """
    Sync ActionState changes to SmartLedger TaskStatus.

    This function should be called after every ActionState change to keep
    the ledger in sync. This ensures the ledger accurately reflects the
    current state of all actions.

    Args:
        user_prompt: The user_prompt key (e.g., "123_456")
        action_id: The action ID (1-based)
        state: The new ActionState
        user_ledgers: The global user_ledgers dictionary

    Returns:
        bool: True if sync was successful, False otherwise

    State Mapping (must match _auto_sync_to_ledger):
        ActionState.ASSIGNED → LedgerTaskStatus.PENDING
        ActionState.IN_PROGRESS → LedgerTaskStatus.IN_PROGRESS
        ActionState.STATUS_VERIFICATION_REQUESTED → LedgerTaskStatus.IN_PROGRESS
        ActionState.COMPLETED → LedgerTaskStatus.COMPLETED
        ActionState.PENDING → LedgerTaskStatus.BLOCKED
        ActionState.ERROR → LedgerTaskStatus.FAILED
        ActionState.FALLBACK_REQUESTED → LedgerTaskStatus.BLOCKED
        ActionState.FALLBACK_RECEIVED → LedgerTaskStatus.IN_PROGRESS
        ActionState.RECIPE_REQUESTED → LedgerTaskStatus.IN_PROGRESS
        ActionState.RECIPE_RECEIVED → LedgerTaskStatus.COMPLETED
        ActionState.TERMINATED → LedgerTaskStatus.COMPLETED
        ActionState.EXECUTING_MOTION → LedgerTaskStatus.IN_PROGRESS
        ActionState.SENSOR_CONFIRM → LedgerTaskStatus.IN_PROGRESS
        ActionState.PREVIEW_PENDING → LedgerTaskStatus.BLOCKED
        ActionState.PREVIEW_APPROVED → LedgerTaskStatus.IN_PROGRESS
    """
    if user_prompt not in user_ledgers:
        logger.debug(f"No ledger found for {user_prompt}, skipping sync")
        return False

    ledger = user_ledgers[user_prompt]
    task_id = f"action_{action_id}"

    if task_id not in ledger.tasks:
        logger.debug(f"Task {task_id} not found in ledger, skipping sync")
        return False

    try:
        LedgerTaskStatus = _get_ledger_task_status()

        # Map ActionState to LedgerTaskStatus — must match _auto_sync_to_ledger
        STATE_MAP = {
            ActionState.ASSIGNED: LedgerTaskStatus.PENDING,
            ActionState.IN_PROGRESS: LedgerTaskStatus.IN_PROGRESS,
            ActionState.STATUS_VERIFICATION_REQUESTED: LedgerTaskStatus.IN_PROGRESS,
            ActionState.COMPLETED: LedgerTaskStatus.COMPLETED,
            ActionState.PENDING: LedgerTaskStatus.BLOCKED,
            ActionState.ERROR: LedgerTaskStatus.FAILED,
            ActionState.FALLBACK_REQUESTED: LedgerTaskStatus.BLOCKED,
            ActionState.FALLBACK_RECEIVED: LedgerTaskStatus.IN_PROGRESS,
            ActionState.RECIPE_REQUESTED: LedgerTaskStatus.IN_PROGRESS,
            ActionState.RECIPE_RECEIVED: LedgerTaskStatus.COMPLETED,
            ActionState.TERMINATED: LedgerTaskStatus.COMPLETED,
            # VLM / physical action states
            ActionState.EXECUTING_MOTION: LedgerTaskStatus.IN_PROGRESS,
            ActionState.SENSOR_CONFIRM: LedgerTaskStatus.IN_PROGRESS,
            # Consent / approval
            ActionState.PREVIEW_PENDING: LedgerTaskStatus.BLOCKED,
            ActionState.PREVIEW_APPROVED: LedgerTaskStatus.IN_PROGRESS,
        }

        ledger_status = STATE_MAP.get(state)
        if ledger_status is None:
            logger.warning(f"No mapping for ActionState {state}")
            return False

        # Get current ledger status to avoid unnecessary updates
        task = ledger.tasks[task_id]
        current_ledger_status = task.status
        if current_ledger_status == ledger_status:
            return True  # Already in correct state

        # Handle transitions that need Task methods instead of raw status update:
        # PAUSED/BLOCKED → IN_PROGRESS must go through task.resume()
        if (ledger_status == LedgerTaskStatus.IN_PROGRESS
                and current_ledger_status in (LedgerTaskStatus.PAUSED, LedgerTaskStatus.BLOCKED)):
            task.resume(reason=f"Resumed via ActionState.{state.value}")
            ledger.save()
        else:
            ledger.update_task_status(
                task_id,
                ledger_status,
                reason=f"Synced from ActionState.{state.value}"
            )
        logger.debug(f"Synced {task_id}: ActionState.{state.value} → LedgerTaskStatus.{ledger_status.value}")
        return True

    except Exception as e:
        logger.error(f"Error syncing action state to ledger: {e}")
        return False


def sync_all_actions_to_ledger(user_prompt: str, user_ledgers: Dict[str, Any]) -> int:
    """
    Sync all current ActionStates to the ledger.

    Useful for bulk sync after recovery or initialization.

    Args:
        user_prompt: The user_prompt key
        user_ledgers: The global user_ledgers dictionary

    Returns:
        int: Number of actions successfully synced
    """
    if user_prompt not in action_states:
        return 0

    synced = 0
    for action_id, state in action_states[user_prompt].items():
        if sync_action_state_to_ledger(user_prompt, action_id, state, user_ledgers):
            synced += 1

    logger.info(f"Synced {synced} actions to ledger for {user_prompt}")
    return synced


def get_ledger_status_for_action(user_prompt: str, action_id: int, user_ledgers: Dict[str, Any]) -> Optional[str]:
    """
    Get the current ledger status for an action.

    Args:
        user_prompt: The user_prompt key
        action_id: The action ID
        user_ledgers: The global user_ledgers dictionary

    Returns:
        str: The current ledger status value, or None if not found
    """
    if user_prompt not in user_ledgers:
        return None

    ledger = user_ledgers[user_prompt]
    task_id = f"action_{action_id}"

    if task_id not in ledger.tasks:
        return None

    return ledger.tasks[task_id].status.value
