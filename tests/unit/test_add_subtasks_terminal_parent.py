"""A terminal parent is never asked to move, and never rewrites the ledger for it.

SmartLedger.add_subtasks blocks the parent once its children exist, guarded by
`if parent_task.status != TaskStatus.BLOCKED`.  That guard asks one question,
"already blocked?", and never asks whether the parent can be moved at all.
FAILED is terminal, so a failed parent sails through, `transition_to` refuses
it, `_validate_transition` logs "Cannot transition from terminal state", and
the call still writes the whole ledger.

Measured on the installed build 2026-09-21: the refusal fires ~30x/minute from
one loop, each one paired with a full save within ~47 ms (median).  The pairing
and the caller were established by the peer session from the log line unique to
this method, "Added subtask {id}: {description}", which precedes every refusal
by 1 ms.

These tests pin only the guard.  They deliberately do NOT pin the return value
or the unconditional save: `add_subtasks` returns True unconditionally today and
three production callers read that value, so changing it is a contract change
that needs its own caller audit.  Skipping a transition that was already being
refused cannot change state -- the state was unchanged either way -- so this is
behaviour-preserving by construction.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..',
                                'agent-ledger-opensource'))
import pytest
from agent_ledger.core import SmartLedger, Task, TaskStatus, TaskType


def _ledger_with_parent(tmp_path, parent_status):
    led = SmartLedger(agent_id='u', session_id='p',
                      ledger_dir=str(tmp_path))
    parent = Task(task_id='action_1', description='parent',
                  task_type=TaskType.AUTONOMOUS)
    parent.status = parent_status
    led.tasks['action_1'] = parent
    return led, parent


SUBS = [{'subtask_id': '1.1', 'description': 'first'},
        {'subtask_id': '1.2', 'description': 'second'}]


def test_a_failed_parent_is_not_asked_to_move(tmp_path, caplog):
    led, parent = _ledger_with_parent(tmp_path, TaskStatus.FAILED)
    with caplog.at_level('WARNING'):
        led.add_subtasks(1, SUBS)
    assert parent.status == TaskStatus.FAILED, 'a terminal parent must not move'
    assert not any('Cannot transition from terminal state' in r.message
                   for r in caplog.records), (
        'the guard let a terminal parent reach transition_to; the refusal it '
        'logs is the ~30/min log line measured on the installed build')


@pytest.mark.parametrize('terminal', [
    TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.COMPLETED,
])
def test_no_terminal_parent_reaches_the_transition(tmp_path, caplog, terminal):
    """Not just FAILED: the guard must ask the canonical predicate."""
    led, parent = _ledger_with_parent(tmp_path, terminal)
    with caplog.at_level('WARNING'):
        led.add_subtasks(1, SUBS)
    assert parent.status == terminal
    assert not any('Cannot transition from terminal state' in r.message
                   for r in caplog.records)


def test_the_children_are_still_created_for_a_terminal_parent(tmp_path):
    """Skipping the parent move must not skip the subtasks themselves."""
    led, _ = _ledger_with_parent(tmp_path, TaskStatus.FAILED)
    led.add_subtasks(1, SUBS)
    assert '1.1' in led.tasks and '1.2' in led.tasks


def test_a_live_parent_is_still_blocked(tmp_path):
    """The path this guard exists to serve must keep working."""
    led, parent = _ledger_with_parent(tmp_path, TaskStatus.IN_PROGRESS)
    led.add_subtasks(1, SUBS)
    assert parent.status == TaskStatus.BLOCKED


class TestTheGuardAsksWhetherTheParentCanBeBlocked:
    """Not "is it terminal" -- that was a narrower question wearing the
    right title.

    A reviewer measured five NON-terminal statuses that sailed past the
    terminal check, reached transition_to and were refused there: PENDING,
    DEFERRED, PAUSED, USER_STOPPED, RESUMING.  Same refusal, same
    unconditional save, just under a different log line -- so it did not
    surface when grepping for the one the original fix measured.

    PENDING is not hypothetical: both create_recipe call sites run
    safe_set_state(..., ActionState.PENDING, ...) immediately after
    add_subtasks, so a repeat requires_breakdown on the same action meets a
    PENDING parent.  Only IN_PROGRESS and DELEGATED can actually be blocked.
    """

    @pytest.mark.parametrize('status_name', [
        'PENDING', 'DEFERRED', 'PAUSED', 'USER_STOPPED', 'RESUMING',
    ])
    def test_a_parent_that_cannot_be_blocked_is_left_alone(
            self, tmp_path, caplog, status_name):
        status = TaskStatus[status_name]
        led, parent = _ledger_with_parent(tmp_path, status)
        before = len(parent.state_history)

        with caplog.at_level('WARNING'):
            led.add_subtasks(1, SUBS)

        assert parent.status == status, 'the parent must be left as it was'
        assert len(parent.state_history) == before, (
            'no transition may be recorded for a refusal')
        assert not any('Invalid transition' in r.message
                       for r in caplog.records), (
            'the guard let a parent that cannot be blocked reach '
            'transition_to; that refusal is the log line the narrower '
            'terminal-only check never covered')

    @pytest.mark.parametrize('status_name', ['IN_PROGRESS', 'DELEGATED'])
    def test_a_parent_that_can_be_blocked_still_is(self, tmp_path, status_name):
        led, parent = _ledger_with_parent(tmp_path, TaskStatus[status_name])
        led.add_subtasks(1, SUBS)
        assert parent.status == TaskStatus.BLOCKED, (
            'the guard must not cost the two statuses it exists to serve')

    def test_an_already_blocked_parent_is_not_transitioned_again(self, tmp_path):
        led, parent = _ledger_with_parent(tmp_path, TaskStatus.BLOCKED)
        before = len(parent.state_history)
        led.add_subtasks(1, SUBS)
        assert parent.status == TaskStatus.BLOCKED
        assert len(parent.state_history) == before

    def test_the_children_are_added_whatever_the_parent_is(self, tmp_path):
        """The subtasks are real work and must land regardless of whether
        the parent could be blocked -- that is why add_subtasks returns
        True in every case."""
        led, parent = _ledger_with_parent(tmp_path, TaskStatus.PENDING)
        assert led.add_subtasks(1, SUBS) is True
        # get_pending_subtasks takes the NUMERIC action id and builds
        # the 'action_N' key itself -- passing the built key looks for
        # 'action_action_1' and silently returns [].
        assert len(led.get_pending_subtasks(1)) == 2
