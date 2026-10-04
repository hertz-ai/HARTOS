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
def wired(ledger, monkeypatch):
    # The desktop owner is its own recipient (see the owner tests below);
    # these tests count the run user's messages only.
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
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


def test_the_desktop_owner_sees_a_run_started_by_another_user(
        ledger, wired, monkeypatch):
    """Live 2026-09-27 15:22-15:34: eleven runs of agent 88555124130 drove
    this desktop for user c23d388c, every step reached SSE as targeted=0, and
    the floating window -- signed in as the owner, 10202 -- showed nothing
    while the ribbon showed every step."""
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
    started = _step('executing', 1)
    closed = activity_stream.finish_run(
        user_id='guest', prompt_id='42', run_id='run1', exit_reason='done',
        iteration=1)
    recipients = [c.args[0] for c in wired.call_args_list]
    assert recipients == ['guest', 'owner-1', 'guest', 'owner-1']
    # The owner is told the same step under the same msg_id, so a client
    # subscribed as both dedupes it.
    for run_leg, owner_leg in ((0, 1), (2, 3)):
        run_msg = wired.call_args_list[run_leg].args[1]
        owner_msg = wired.call_args_list[owner_leg].args[1]
        assert owner_msg['msg_id'] == run_msg['msg_id']
        assert owner_msg['summary'] == run_msg['summary']
        assert owner_msg['phase'] == run_msg['phase']
        assert owner_msg['run_done'] == run_msg['run_done']
    assert wired.call_args_list[0].args[1] == started
    assert wired.call_args_list[2].args[1] == closed


def test_the_owners_copy_of_another_users_run_cannot_steer_it(
        ledger, wired, monkeypatch):
    """Review of de3f89364 (CRITICAL): the owner's copy carried the guest's
    goal id, Nunba's liveRunOf made it the live run, and the OWNER's typed
    chat went to /dashboard/agents/<guest goal>/inject.  The owner's copy is
    disclosure only: no goal id, and it says so."""
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
    started = _step('executing', 1, steering_agent_id='goal-42')
    activity_stream.finish_run(
        user_id='guest', prompt_id='42', run_id='run1', exit_reason='done',
        iteration=1, steering_agent_id='goal-42')
    by_recipient = {}
    for c in wired.call_args_list:
        by_recipient.setdefault(c.args[0], []).append(c.args[1])
    for msg in by_recipient['owner-1']:
        assert msg['agent_id'] == ''
        assert msg['disclosure_only'] is True
    for msg in by_recipient['guest']:
        assert msg['agent_id'] == 'goal-42'
        assert msg['disclosure_only'] is False
    # What record_activity returns is the run user's own, routable message.
    assert started['agent_id'] == 'goal-42'
    assert started['disclosure_only'] is False


def test_the_owner_is_not_told_twice_about_their_own_run(
        ledger, wired, monkeypatch):
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'guest')
    _step('executing', 1, steering_agent_id='goal-42')
    assert [c.args[0] for c in wired.call_args_list] == ['guest']
    # Their own run: the one message they get can steer it.
    msg = wired.call_args_list[0].args[1]
    assert msg['agent_id'] == 'goal-42'
    assert msg['disclosure_only'] is False


# ── A closed run takes no more steps (review F1, 2026-09-27) ─────────────
# An action abandoned at the loop's time budget keeps running on its worker,
# and the shell tool inside it announces its own steps.  Probe vlm_probe.py
# q2b: 2 s after the loop returned, that late step raised the ribbon again,
# rewrote the finished task's context to phase=failed and fanned out
# run_done=False, so the companion showed the run live again.

@pytest.mark.parametrize('exit_reason', ['done', 'timeout', 'stopped'])
def test_a_step_after_the_run_closed_is_ignored(ledger, wired, exit_reason):
    _step('executing', 1)
    activity_stream.finish_run(
        user_id='guest', prompt_id='42', run_id='run1', exit_reason=exit_reason)
    task = ledger.get_task('computer_use_run1')
    status, context = task.status, dict(task.context)
    wired.reset_mock()
    with patch.object(activity_stream, '_ribbon') as ribbon:
        assert _step('failed', 2, error='late') is None
    ribbon.assert_not_called()
    wired.assert_not_called()
    task = ledger.get_task('computer_use_run1')
    assert task.status == status
    assert task.context == context


def test_a_step_of_an_open_run_still_raises_the_ribbon(ledger, wired):
    _step('executing', 1)
    with patch.object(activity_stream, '_ribbon') as ribbon:
        assert _step('failed', 1, error='rc 1') is not None
    ribbon.assert_called_once()


def test_a_joined_run_whose_stamp_is_closed_takes_no_steps(ledger, wired):
    """The loop marks the stamp its abandoned worker adopted; a step of a
    run joined BEFORE that mark is refused too, not only after finish_run."""
    from hartos.threadlocal import thread_local_data as tld
    tld.set_activity_run('run1', user_id='guest', prompt_id='42')
    try:
        joined = activity_stream.current_run(user_id='guest', prompt_id='42')
        assert joined.step(iteration=1, action='shell', phase='executing') is not None
        activity_stream.close_run_stamp(tld.get_activity_run())
        wired.reset_mock()
        with patch.object(activity_stream, '_ribbon') as ribbon:
            assert joined.step(iteration=1, action='shell', phase='failed') is None
            again = activity_stream.current_run(user_id='guest', prompt_id='42')
            assert again.step(iteration=2, action='shell', phase='executing') is None
            assert again.finish(exit_reason='action_error') is None
        ribbon.assert_not_called()
        wired.assert_not_called()
        assert ledger.get_task('computer_use_run1').status == TaskStatus.IN_PROGRESS
    finally:
        tld.clear_activity_run()


def test_the_run_close_never_shares_a_msg_id_with_the_last_step(ledger, wired):
    """Live 2026-10-04 17:02:12, task computer_use_443f86f06cd9: the loop's
    third consecutive failed step and the close finish_run sent for exit
    'action_error' both carried msg_id computer-use:<task>:4:failed.  The
    page dedupes transport messages by msg_id (Nunba realtimeService
    _isDuplicate, 10 s window), so the close -- the ONLY message with
    run_done=True -- was dropped, and the floating window kept ': step
    failed' on screen for hours (47 such exits that day).  Clients reduce by
    task_id, so the two ids only have to DIFFER; they must differ for every
    exit reason whose close phase equals the last step's phase."""
    for exit_reason, last_phase in (('action_error', 'failed'),
                                    ('done', 'completed'),
                                    ('stopped', 'stopped')):
        run = f'run-{exit_reason}'
        last = activity_stream.record_activity(
            user_id='guest', prompt_id='42', run_id=run, iteration=4,
            action='shell', phase=last_phase, agent_id='42')
        closed = activity_stream.finish_run(
            user_id='guest', prompt_id='42', run_id=run,
            exit_reason=exit_reason, iteration=4)
        assert closed['task_id'] == last['task_id']
        assert (last['run_done'], closed['run_done']) == (False, True)
        assert closed['msg_id'] != last['msg_id'], (
            f'{exit_reason}: the close reuses the last step msg_id '
            f'{last["msg_id"]!r}; a client that dedupes by msg_id never '
            f'learns the run ended')
        sent = [c.args[1]['msg_id'] for c in wired.call_args_list
                if c.args[1]['run_id'] == run]
        assert sent == [last['msg_id'], closed['msg_id']]
