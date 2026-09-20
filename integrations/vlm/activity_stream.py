"""Computer-use activity projected from the existing SmartLedger.

The ledger is durable state.  ``on_notification`` is the already subscribed
per-user projection path for desktop, web, Android and PeerLink clients.  This
module deliberately owns neither a socket nor a second event store.

Shape (one task per RUN, not per step):

* ``record_activity`` is called twice per loop iteration, before the action
  (``executing``) and after it (``completed`` / ``blocked`` / ``failed`` /
  ``stopped``).  The first call of a run creates ONE ledger task
  ``computer_use_<run_id>``; every later call rewrites that task's context
  (sequence, action, phase, caption, last error) and fans the step out.
* Only the step OUTCOME is persisted.  The ``executing`` half of a step is
  fan-out only: the safety audit JSONL already records every action before
  it runs (``audit_ref``), so a second durable write per step buys nothing.
  Measured live 2026-09-19 22:07 with the per-step-task shape this replaced:
  three full-ledger saves per step ("Saved 6 tasks to ledger" x3) and 0.94 s
  between the status transition and its save, on the VLM hot path.  The
  ledger's own #145 note records that per-call full dumps starved the UI.
* ``finish_run`` is called ONCE when the loop exits and moves the run task to
  its terminal status from the loop's ``exit_reason``.  Steps never change
  the task status: a COMPLETED task is terminal and could not take the next
  step.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger('hevolve.vlm.activity')


_ACTION_LABELS = {
    'left_click': 'Selecting a control',
    'right_click': 'Opening a context menu',
    'double_click': 'Opening an item',
    'type': 'Entering text',
    'key': 'Using the keyboard',
    'hotkey': 'Using a keyboard shortcut',
    'hover': 'Inspecting a control',
    'mouse_move': 'Moving to a control',
    'scroll_up': 'Scrolling',
    'scroll_down': 'Scrolling',
    'wait': 'Waiting for the app',
    'shell': 'Running a local step',
    'open_file_gui': 'Opening an app or file',
    'write_file': 'Writing a local file',
    'read_file_and_understand': 'Reading a local file',
}

#: Step phases a client may receive.  ``executing`` is the only non-terminal
#: step phase; the rest describe the outcome of ONE step, never of the run.
STEP_PHASES = frozenset({'executing', 'completed', 'blocked', 'failed', 'stopped'})

#: local_loop exit_reason -> the run task's terminal status name.  Kept as
#: names so this module does not import agent_ledger at import time.
_EXIT_STATUS = {
    'done': 'COMPLETED',
    'stopped': 'USER_STOPPED',
    'action_error': 'FAILED',
    'timeout': 'FAILED',
    'max_iterations': 'FAILED',
}

#: exit_reason -> the phase the run-level fan-out message carries.  Clients
#: already reduce these three; a run-level message adds ``run_done``.
_EXIT_PHASE = {
    'done': 'completed',
    'stopped': 'stopped',
}


def safe_action_summary(action: str) -> str:
    """Human-readable action label without prompts, coordinates, or content."""
    return _ACTION_LABELS.get(str(action or '').lower(), 'Working on the computer')


def run_task_id(run_id: str) -> str:
    """The ONE ledger task id for a computer-use run."""
    return f'computer_use_{run_id}'


def _ledger_for(user_id: str, prompt_id: str):
    """Resolve the recipe-owned ledger, recovering through its canonical setup."""
    user_prompt = f'{user_id}_{prompt_id}'
    from hartos.lifecycle_hooks import get_registered_ledger, register_ledger_for_session

    ledger = get_registered_ledger(user_prompt)
    if ledger is not None:
        return ledger

    # Some direct /visual_agent callers do not first enter create_recipe.
    # They still use the same SmartLedger construction and registry rather
    # than a VLM-specific file or cache.
    from agent_ledger import create_ledger_from_actions, get_production_backend
    ledger = create_ledger_from_actions(
        user_id=user_id,
        prompt_id=prompt_id,
        actions=[],
        backend=get_production_backend(),
    )
    register_ledger_for_session(user_prompt, ledger)
    return ledger


def resolve_steering_agent_id(user_id: str, prompt_id: str) -> str:
    """Resolve the goal id required by the existing GroupChat injector.

    A prompt id identifies a run; the injector intentionally addresses a
    database goal.  Do not guess that the two are interchangeable.
    """
    try:
        from integrations.social.models import AgentGoal, get_db
        db = get_db()
        try:
            models = [AgentGoal]
            try:
                from integrations.social.models import CodingGoal
                models.append(CodingGoal)
            except ImportError:
                pass
            # A prompt id is a run key, not an authorization boundary.  It is
            # possible for independent tenants to reuse one, so only return
            # the goal owned by the recipient of this event.  This uses the
            # same owner precedence as the existing steering endpoint.
            from core.event_attribution import goal_owner_user_id
            for model in models:
                # CodingGoal has no prompt_id column. Its existing steering
                # fallback only applies once it carries a prompt id; never let
                # that shape interrupt the AgentGoal lookup above.
                prompt_column = getattr(model, 'prompt_id', None)
                if prompt_column is None:
                    continue
                for goal in db.query(model).filter(
                        prompt_column == str(prompt_id)).all():
                    if goal_owner_user_id(goal) == str(user_id):
                        return str(goal.id)
        finally:
            db.close()
    except Exception:
        logger.debug('computer-use goal resolution unavailable', exc_info=True)
    return ''


def _payload(*, user_id: str, prompt_id: str, run_id: str, task_id: str,
             sequence: int, action: str, phase: str, agent_id: str,
             steering_agent_id: str, caption: str, error: str,
             run_done: bool) -> Dict[str, Any]:
    """The safe client payload: no prompt text, coordinates or screen content."""
    return {
        'type': 'computer_use.update',
        # Each (step, phase) is a distinct transport message; clients reduce
        # by task_id, so one run collapses to one card.
        'msg_id': f'computer-use:{task_id}:{int(sequence)}:{phase}',
        'user_id': user_id,
        # This is a database goal id, not an assumed prompt-id alias.  It is
        # the only id accepted by the existing GroupChat injection endpoint.
        'agent_id': steering_agent_id,
        'executor_id': str(agent_id or ''),
        'prompt_id': prompt_id,
        'task_id': task_id,
        'run_id': run_id,
        'sequence': int(sequence),
        'action': action,
        'summary': safe_action_summary(action),
        'caption': str(caption or '')[:160],
        'phase': phase,
        'error': error[:160] if error else '',
        # True only on the run-level message from finish_run: a client that
        # routes guidance to the run must stop doing so on this message.
        'run_done': run_done,
    }


def _fan_out(user_id: str, payload: Dict[str, Any]) -> None:
    try:
        from integrations.social.realtime import on_notification
        on_notification(user_id, payload)
    except Exception:
        # Persistence succeeded; the Task Ledger remains authoritative and a
        # later refresh recovers the state.  Never roll the task back because
        # a best-effort fanout leg was unavailable.
        logger.exception('computer-use projection failed for task=%s',
                         payload.get('task_id'))


def record_activity(*, user_id: Any, prompt_id: Any, run_id: str,
                    iteration: int, action: str, phase: str,
                    agent_id: str = '', steering_agent_id: str = '',
                    audit_ref: Optional[Dict[str, Any]] = None,
                    error: str = '', caption: str = '') -> Optional[Dict[str, Any]]:
    """Record one step phase on the run's task, then fan it out once.

    ``phase`` is one of STEP_PHASES.  The run task is created IN_PROGRESS on
    the first call; later calls rewrite its context.  Only an outcome phase
    (anything but ``executing``) is written to disk.  The returned dict is
    the safe client payload.  A failed ledger write returns ``None`` and
    therefore emits nothing.
    """
    if not user_id or not prompt_id or not run_id:
        return None
    if phase not in STEP_PHASES:
        logger.warning('computer-use: unknown step phase %r ignored', phase)
        return None
    user_id, prompt_id = str(user_id), str(prompt_id)
    action = str(action or '')
    task_id = run_task_id(run_id)
    summary = safe_action_summary(action)
    context = {
        'kind': 'computer_use',
        'run_id': run_id,
        'prompt_id': prompt_id,
        'agent_id': str(agent_id or ''),
        'sequence': int(iteration),
        'action': action,
        'phase': phase,
        # This is the already-visible compact ribbon caption.  It is carried
        # by the ledger projection so the readable companion/chat card is not
        # less informative than the ephemeral ribbon.
        'caption': str(caption or '')[:160],
        'audit_ref': audit_ref or {},
        'error': str(error or '')[:160],
    }

    try:
        from agent_ledger import Task, TaskStatus, TaskType
        ledger = _ledger_for(user_id, prompt_id)
        task = ledger.get_task(task_id)
        if task is None:
            task = Task(
                task_id=task_id,
                description=summary,
                task_type=TaskType.INTERMEDIATE,
                status=TaskStatus.IN_PROGRESS,
                context=context,
                priority=90,
            )
            task.recipe_prompt_id = prompt_id
            task.recipe_flow_id = None
            task.recipe_action_id = None
            task.owner_user_id = user_id
            task.owner_prompt_id = prompt_id
            task.seal_integrity()
            # Creation is the one write the ``executing`` half of a step
            # pays: the run has to exist before anything can reduce onto it.
            if not ledger.add_task(task):
                return None
        else:
            if not ledger.update_task_context(
                    task_id, context, defer_save=(phase == 'executing')):
                return None
    except Exception:
        logger.exception('computer-use ledger write failed for prompt=%s', prompt_id)
        return None

    payload = _payload(
        user_id=user_id, prompt_id=prompt_id, run_id=run_id, task_id=task_id,
        sequence=iteration, action=action, phase=phase, agent_id=agent_id,
        steering_agent_id=steering_agent_id, caption=caption, error=error,
        run_done=False)
    _fan_out(user_id, payload)
    return payload


def finish_run(*, user_id: Any, prompt_id: Any, run_id: str, exit_reason: str,
               iteration: int = 0, action: str = '', error: str = '',
               agent_id: str = '', steering_agent_id: str = '',
               caption: str = '') -> Optional[Dict[str, Any]]:
    """Move the run's task to its terminal status, once, from ``exit_reason``.

    One save, one fan-out.  ``exit_reason`` is the loop's own vocabulary
    (done / stopped / action_error / timeout / max_iterations); an unknown
    reason is recorded as FAILED with the reason as the error, never as a
    success.  Returns the safe client payload, or ``None`` when the run has
    no task (nothing was ever recorded) or the ledger write failed.
    """
    if not user_id or not prompt_id or not run_id:
        return None
    user_id, prompt_id = str(user_id), str(prompt_id)
    task_id = run_task_id(run_id)
    status_name = _EXIT_STATUS.get(exit_reason, 'FAILED')
    if status_name == 'FAILED' and not error:
        error = f'exit_reason={exit_reason}'
    try:
        from agent_ledger import TaskStatus
        ledger = _ledger_for(user_id, prompt_id)
        task = ledger.get_task(task_id)
        if task is None:
            return None
        target = getattr(TaskStatus, status_name)
        if task.status != target and not ledger.update_task_status(
                task_id, target, error_message=error or None,
                result={'exit_reason': exit_reason, 'steps': int(iteration)},
                reason=f'computer-use run finished: {exit_reason}'):
            return None
    except Exception:
        logger.exception('computer-use run close failed for prompt=%s', prompt_id)
        return None

    payload = _payload(
        user_id=user_id, prompt_id=prompt_id, run_id=run_id, task_id=task_id,
        sequence=iteration, action=action,
        phase=_EXIT_PHASE.get(exit_reason, 'failed'), agent_id=agent_id,
        steering_agent_id=steering_agent_id, caption=caption, error=error,
        run_done=True)
    _fan_out(user_id, payload)
    return payload
