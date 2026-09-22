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

import contextlib
import logging
import uuid
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


def _ribbon(show: bool, text: str = None) -> None:
    """Nunba's AI-control ribbon — the SECOND surface of an announcement.

    Moved here from local_loop 2026-09-21.  It lived beside the loop and was
    poked directly at five sites, while record_activity was called at four; the
    two agreed at exactly ONE of them, so the ribbon and the floating companion
    window told the owner different stories.  The opening "Starting: ..." line
    and every shell command reached only the ribbon; a blocked or failed step
    reached only the topic, leaving the ribbon showing the step's INTENT as if
    it had happened.  Both surfaces now hang off the one announcement below.

    Why it must exist at all: Nunba shows the ribbon from its /execute route,
    which only the http tier calls.  The inprocess tier drives pyautogui
    directly, so a run could type and click with nothing on screen saying the
    AI was in control: on 2026-09-13 the audit log holds 2,660 VLM actions and
    gui_app.log holds no "Ribbon indicator shown" line.  A show request also
    re-arms the ribbon's 15 s inactivity timer, so one rides every step.

    Best effort, and only inside Nunba: standalone HARTOS has no ribbon, and a
    refused localhost connect costs seconds on Windows.
    """
    try:
        from core.config_cache import is_bundled, _local_base
        if not is_bundled():
            return
        from core.http_pool import pooled_get
        pooled_get(f"{_local_base()}/indicator/{'show' if show else 'hide'}",
                   timeout=2, params={'text': text} if text else None)
    except Exception as e:
        logger.debug(f"AI-control ribbon {'show' if show else 'hide'} "
                     f"skipped: {e}")


#: Step phases the ribbon speaks, and how.  ``executing`` is the step's intent,
#: which is what the owner needs while it happens.  ``blocked``/``failed`` are
#: the outcomes that CONTRADICT that intent, and the loop keeps running after
#: them, so without this the ribbon would sit there claiming the refused click
#: happened.  ``completed`` is silent: the next step's intent replaces the line
#: a moment later, and a tick per step is churn, not information.  ``stopped``
#: is silent because its caller breaks straight into the run close below, which
#: takes the ribbon down within milliseconds.
_RIBBON_PHASES = {'executing', 'blocked', 'failed'}


def _ribbon_line(phase: str, caption: str, error: str) -> str:
    """What the ribbon says for a step, from the same caption the topic gets."""
    line = str(caption or '').strip()
    if phase == 'executing':
        return line
    detail = str(error or '').strip()
    outcome = 'refused' if phase == 'blocked' else 'failed'
    head = f'{line} — {outcome}' if line else outcome.capitalize()
    return f'{head}: {detail}' if detail else head


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
    """Announce one step phase: ribbon, then the run's task, then fan out.

    THE one way to say what the AI is doing.  Callers never touch a surface
    themselves -- that is what let the ribbon and the companion window drift
    (see _ribbon).  ``phase`` is one of STEP_PHASES.  The run task is created
    IN_PROGRESS on the first call; later calls rewrite its context.  Only an
    outcome phase (anything but ``executing``) is written to disk.  The
    returned dict is the safe client payload.  A failed ledger write returns
    ``None`` and therefore fans out nothing.
    """
    if phase not in STEP_PHASES:
        logger.warning('computer-use: unknown step phase %r ignored', phase)
        return None
    # The ribbon goes first and is never conditional on the durable leg.  It
    # is the owner's live signal that the AI holds this machine's mouse and
    # keyboard, so a ledger failure -- or a caller with no run to record
    # against -- must not leave the screen silent while the pointer moves.
    if phase in _RIBBON_PHASES:
        _ribbon(True, _ribbon_line(phase, caption, error))
    if not user_id or not prompt_id or not run_id:
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

    Takes the ribbon down first, for the same reason record_activity puts it
    up first: the run is over, and a ribbon left claiming the AI has the
    machine is the one failure mode here that the owner cannot ignore.  (It
    would clear on its own after the 15 s inactivity timer, but only after
    15 s of lying.)
    """
    _ribbon(False)
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


class ActivityRun:
    """A computer-use run in progress: the ids its steps are announced with.

    Held so a caller never re-derives them, and so ``finish`` can be called
    unconditionally -- whether this frame opened the run or joined one already
    running, which is the difference ``open_run`` resolves.
    """

    __slots__ = ('run_id', 'user_id', 'prompt_id', 'agent_id',
                 'steering_agent_id', 'owns')

    def __init__(self, *, run_id, user_id, prompt_id, agent_id='',
                 steering_agent_id='', owns=False):
        self.run_id = run_id
        self.user_id = str(user_id or '')
        self.prompt_id = str(prompt_id or '')
        self.agent_id = str(agent_id or '')
        self.steering_agent_id = str(steering_agent_id or '')
        self.owns = bool(owns)

    def step(self, *, iteration: int, action: str, phase: str,
             caption: str = '', error: str = '',
             audit_ref: Optional[Dict[str, Any]] = None):
        """Announce one step of THIS run.  Binds the run's ids to the one
        announcer; it is ``record_activity``, not a second implementation."""
        return record_activity(
            user_id=self.user_id, prompt_id=self.prompt_id,
            run_id=self.run_id, iteration=iteration, action=action,
            phase=phase, agent_id=self.agent_id,
            steering_agent_id=self.steering_agent_id,
            audit_ref=audit_ref, error=error, caption=caption)

    def finish(self, *, exit_reason: str, iteration: int = 0,
               action: str = '', error: str = '', caption: str = ''):
        """Close the run -- but only if this frame opened it.

        A joined run belongs to the frame above, which is still taking steps;
        closing it here would take the ribbon down mid-run and move the task
        to a terminal status the next step could not leave.  That is exactly
        what the shell tool used to do with its bare ribbon hide.
        """
        if not self.owns:
            return None
        try:
            return finish_run(
                user_id=self.user_id, prompt_id=self.prompt_id,
                run_id=self.run_id, exit_reason=exit_reason,
                iteration=iteration, action=action, error=error,
                agent_id=self.agent_id,
                steering_agent_id=self.steering_agent_id, caption=caption)
        finally:
            with contextlib.suppress(Exception):
                from hartos.threadlocal import thread_local_data
                thread_local_data.clear_activity_run()


def open_run(*, user_id: Any, prompt_id: Any, agent_id: str = '',
             steering_agent_id: str = '') -> ActivityRun:
    """Start a computer-use run.  THE one opener.

    Always owns, and always REPLACES whatever run is stamped on this thread.
    That replacement is load-bearing, not tidiness: run_local_agentic_loop
    reaches its close straight-line, with no ``finally`` (the same shape its
    stop-registry pair already has), so a raise anywhere above that line ends
    the run with its stamp still on the thread.  Without the overwrite the
    NEXT run on that thread would join the dead one -- never opening its own
    ledger task, and never lowering the ribbon, because a joined run's close
    is deliberately a no-op.  A caller that means "I am a step in whatever is
    already running" wants ``current_run`` below, which is a different
    question and says so.
    """
    run = ActivityRun(
        run_id=uuid.uuid4().hex[:12], user_id=user_id, prompt_id=prompt_id,
        agent_id=agent_id, steering_agent_id=steering_agent_id, owns=True)
    with contextlib.suppress(Exception):
        from hartos.threadlocal import thread_local_data
        thread_local_data.set_activity_run(
            run.run_id, user_id=run.user_id, prompt_id=run.prompt_id)
    return run


def current_run(*, user_id: Any, prompt_id: Any, agent_id: str = '',
                steering_agent_id: str = '') -> ActivityRun:
    """The run this work belongs to: the live one, or a new one for it alone.

    For a tool that may or may not be executing inside a computer-use run and
    should not have to know which.  The shell tool is the case: it reaches
    hart_intelligence_entry on the VLM loop's OWN thread during a run, and
    from a plain chat turn on a thread with no run at all.  Joining means its
    work is announced as steps of the enclosing run and its close is a no-op,
    so it cannot take the ribbon down or terminate a task the loop is still
    using -- which is exactly what its old bare ribbon hide did.

    Opening is delegated to open_run; there is no second opener here.
    """
    try:
        from hartos.threadlocal import thread_local_data
        joined = thread_local_data.get_activity_run()
    except Exception:
        joined = None
    if joined and joined.get('run_id'):
        return ActivityRun(
            run_id=joined['run_id'],
            user_id=joined.get('user_id') or user_id,
            prompt_id=joined.get('prompt_id') or prompt_id,
            agent_id=agent_id, steering_agent_id=steering_agent_id,
            owns=False)
    return open_run(user_id=user_id, prompt_id=prompt_id,
                    agent_id=agent_id, steering_agent_id=steering_agent_id)
