"""Computer-use activity projected from the existing SmartLedger.

The ledger is durable state.  ``on_notification`` is the already subscribed
per-user projection path for desktop, web, Android and PeerLink clients.  This
module deliberately owns neither a socket nor a second event store.
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


def safe_action_summary(action: str) -> str:
    """Human-readable action label without prompts, coordinates, or content."""
    return _ACTION_LABELS.get(str(action or '').lower(), 'Working on the computer')


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


def record_activity(*, user_id: Any, prompt_id: Any, run_id: str,
                    iteration: int, action: str, phase: str,
                    agent_id: str = '', steering_agent_id: str = '',
                    audit_ref: Optional[Dict[str, Any]] = None,
                    error: str = '', caption: str = '') -> Optional[Dict[str, Any]]:
    """Persist one computer-use action state, then fan it out once.

    ``phase`` is intentionally small: ``executing``, ``completed``,
    ``blocked`` or ``failed``.  The returned dict is the safe client payload.
    A failed ledger write returns ``None`` and therefore emits nothing.
    """
    if not user_id or not prompt_id or not run_id:
        return None
    user_id, prompt_id = str(user_id), str(prompt_id)
    action = str(action or '')
    task_id = f'computer_use_{run_id}_{int(iteration)}'
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
            if not ledger.add_task(task):
                return None
        else:
            if not ledger.update_task_context(task_id, context):
                return None

        target = {
            'completed': TaskStatus.COMPLETED,
            'blocked': TaskStatus.BLOCKED,
            'failed': TaskStatus.FAILED,
        }.get(phase)
        if target is not None and task.status != target:
            if not ledger.update_task_status(task_id, target, error_message=error or None,
                                             result={'phase': phase}):
                return None

    except Exception:
        logger.exception('computer-use ledger write failed for prompt=%s', prompt_id)
        return None

    payload = {
        'type': 'computer_use.update',
        # Each phase is a distinct transport message; clients reduce by task_id.
        'msg_id': f'computer-use:{task_id}:{phase}',
        'user_id': user_id,
        # This is a database goal id, not an assumed prompt-id alias.  It is
        # the only id accepted by the existing GroupChat injection endpoint.
        'agent_id': steering_agent_id,
        'executor_id': str(agent_id or ''),
        'prompt_id': prompt_id,
        'task_id': task_id,
        'run_id': run_id,
        'sequence': int(iteration),
        'action': action,
        'summary': summary,
        'caption': str(caption or '')[:160],
        'phase': phase,
        'error': error[:160] if error else '',
    }
    try:
        from integrations.social.realtime import on_notification
        on_notification(user_id, payload)
    except Exception:
        # Persistence succeeded; the Task Ledger remains authoritative and a
        # later refresh recovers the state.  Never roll the task back because
        # a best-effort fanout leg was unavailable.
        logger.exception('computer-use projection failed for task=%s', task_id)
    return payload
