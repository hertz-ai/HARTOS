"""Only the transition and the durable write may fail a state projection.

set_action_state RAISES when _auto_sync_to_ledger returns False, so anything
that can make that function return False can block an action's lifecycle.
Before this guard the same blanket `except Exception` covered the persistence
AND the bookkeeping that follows it (heartbeat, SLA, record_spend,
release), so one unparseable ``started_at`` -- a stale or hand-edited value
reaching datetime.fromisoformat -- raised StateTransitionError on the
action's TERMINAL transition: the work was done and already persisted, and
the agent still could not record that it finished.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

import pytest

from agent_ledger import SmartLedger, Task, TaskStatus, TaskType
from agent_ledger.backends import JSONBackend
from hartos import lifecycle_hooks as lh


@pytest.fixture
def wired(tmp_path):
    """A registered ledger holding one owned, in-progress action."""
    user_prompt = 'u_sync_verdict'
    ledger = SmartLedger(agent_id='7', session_id=user_prompt,
                         backend=JSONBackend(storage_dir=str(tmp_path)))
    task = Task(task_id='action_1', description='do the thing',
                task_type=TaskType.INTERMEDIATE)
    ledger.add_task(task)
    ledger.update_task_status('action_1', TaskStatus.IN_PROGRESS)
    task.claim(node_id='node-a', user_id='7')
    lh.register_ledger_for_session(user_prompt, ledger)
    lh.action_states.pop(user_prompt, None)
    yield user_prompt, ledger, task
    lh._ledger_registry.pop(user_prompt, None)
    lh.action_states.pop(user_prompt, None)


def test_an_unparseable_started_at_does_not_block_the_terminal_transition(wired):
    user_prompt, ledger, task = wired
    task.started_at = 'not-a-timestamp'          # stale/hand-edited value

    assert lh._auto_sync_to_ledger(user_prompt, 1, lh.ActionState.COMPLETED) is True
    # The durable authority took the completion...
    assert ledger.get_task('action_1').status == TaskStatus.COMPLETED
    # ...and the bookkeeping still ran: ownership was released.
    assert ledger.get_task('action_1').is_owned is False


def _drive_to_completion(user_prompt):
    """The action's own legal path: the FSM requires verification first."""
    lh.set_action_state(user_prompt, 1, lh.ActionState.IN_PROGRESS, 'start')
    lh.set_action_state(user_prompt, 1,
                        lh.ActionState.STATUS_VERIFICATION_REQUESTED, 'verify')
    lh.set_action_state(user_prompt, 1, lh.ActionState.COMPLETED, 'done')


def test_set_action_state_does_not_raise_on_a_bookkeeping_failure(wired):
    user_prompt, ledger, task = wired
    task.started_at = 'not-a-timestamp'

    # Would previously raise StateTransitionError("Ledger persistence failed")
    # on the terminal hop, because record_spend's fromisoformat shared the
    # persistence try block.
    _drive_to_completion(user_prompt)
    assert lh.get_action_state(user_prompt, 1) == lh.ActionState.COMPLETED
    assert ledger.get_task('action_1').status == TaskStatus.COMPLETED


def test_a_real_persistence_failure_still_fails_the_projection(wired):
    """The narrowing must not swallow the case it was built to surface."""
    user_prompt, ledger, _task = wired
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ledger, 'save', lambda *a, **k: False)
        assert lh._auto_sync_to_ledger(
            user_prompt, 1, lh.ActionState.COMPLETED) is False
        with pytest.raises(lh.StateTransitionError) as caught:
            _drive_to_completion(user_prompt)
    # Specifically the persistence verdict, not the FSM's own validation.
    assert 'Ledger persistence failed' in str(caught.value)
