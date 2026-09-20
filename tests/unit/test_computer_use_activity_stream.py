"""Computer-use activity: ONE ledger task per run, one disk write per step.

Behavioural: a real SmartLedger on a real JSONBackend in tmp_path; only the
ledger registry lookup, the goal lookup and the realtime fan-out are patched.
"""
from unittest.mock import patch

import pytest

from agent_ledger import SmartLedger, TaskStatus
from agent_ledger.backends import JSONBackend
from integrations.vlm import activity_stream


@pytest.fixture
def ledger(tmp_path):
    return SmartLedger(
        '42', 'guest_42_test',
        backend=JSONBackend(storage_dir=str(tmp_path)),
    )


@pytest.fixture
def wired(ledger):
    with patch.object(activity_stream, '_ledger_for', return_value=ledger), \
         patch.object(activity_stream, 'resolve_steering_agent_id', return_value='goal-42'), \
         patch('integrations.social.realtime.on_notification') as notify:
        yield notify


def _step(phase, iteration=1, **kw):
    return activity_stream.record_activity(
        user_id='guest', prompt_id='42', run_id='run1', iteration=iteration,
        action='left_click', phase=phase, agent_id='42',
        audit_ref={'activity_id': f'run1:{iteration}'},
        caption='Open Settings (left_click)', **kw)


def test_a_run_is_one_task_reduced_through_its_steps(ledger, wired):
    started = _step('executing', 1)
    assert started['task_id'] == 'computer_use_run1'
    task = ledger.get_task(started['task_id'])
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.context['kind'] == 'computer_use'
    assert task.context['audit_ref']['activity_id'] == 'run1:1'
    assert task.context['caption'] == 'Open Settings (left_click)'
    wired.assert_called_once_with('guest', started)

    done1 = _step('completed', 1)
    # A step outcome updates the run, it does not close it: the next step
    # still has to land on this task.
    assert ledger.get_task(done1['task_id']).status == TaskStatus.IN_PROGRESS
    assert ledger.get_task(done1['task_id']).context['phase'] == 'completed'

    started2 = _step('executing', 2)
    assert started2['task_id'] == started['task_id']
    assert started2['msg_id'] != started['msg_id']
    assert ledger.get_task(started2['task_id']).context['sequence'] == 2
    assert [t for t in ledger.tasks if t.startswith('computer_use_')] == ['computer_use_run1']

    closed = activity_stream.finish_run(
        user_id='guest', prompt_id='42', run_id='run1', exit_reason='done',
        iteration=2, steering_agent_id='goal-42')
    assert closed['run_done'] is True
    assert closed['phase'] == 'completed'
    assert ledger.get_task(closed['task_id']).status == TaskStatus.COMPLETED
    assert wired.call_count == 4


@pytest.mark.parametrize('exit_reason, status, phase', [
    ('stopped', TaskStatus.USER_STOPPED, 'stopped'),
    ('action_error', TaskStatus.FAILED, 'failed'),
    ('timeout', TaskStatus.FAILED, 'failed'),
    ('max_iterations', TaskStatus.FAILED, 'failed'),
    ('something_new', TaskStatus.FAILED, 'failed'),
])
def test_finish_run_maps_every_exit_reason_to_a_terminal_status(
        ledger, wired, exit_reason, status, phase):
    _step('executing', 1)
    closed = activity_stream.finish_run(
        user_id='guest', prompt_id='42', run_id='run1', exit_reason=exit_reason)
    task = ledger.get_task(closed['task_id'])
    assert task.status == status
    assert closed['phase'] == phase
    if status == TaskStatus.FAILED:
        assert task.error_message  # never a silent failure


def test_finish_run_without_a_recorded_step_emits_nothing(ledger, wired):
    assert activity_stream.finish_run(
        user_id='guest', prompt_id='42', run_id='never', exit_reason='done') is None
    wired.assert_not_called()


def test_one_disk_write_per_step_not_three(ledger, wired):
    """NFT: the live 2026-09-19 log showed three full-ledger saves per step."""
    saves = []
    real_save = ledger.save

    def counting_save(*a, **k):
        saves.append(1)
        return real_save(*a, **k)

    with patch.object(ledger, 'save', counting_save):
        steps = 5
        for i in range(1, steps + 1):
            _step('executing', i)
            _step('completed', i)
        activity_stream.finish_run(
            user_id='guest', prompt_id='42', run_id='run1', exit_reason='done',
            iteration=steps)
    # creation + one outcome per step + the terminal write
    assert len(saves) == steps + 2
    assert wired.call_count == steps * 2 + 1


def test_a_failed_ledger_write_emits_nothing(ledger, wired):
    with patch.object(ledger, 'add_task', return_value=False):
        assert _step('executing', 1) is None
    wired.assert_not_called()


def test_an_unknown_step_phase_is_refused(ledger, wired):
    assert _step('run_completed', 1) is None
    wired.assert_not_called()
