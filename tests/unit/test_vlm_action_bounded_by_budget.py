"""The VLM loop's time budget bounds the action in flight, not only the gaps.

MEASURED LIVE 2026-09-27 (installed build Nunba 53927f85 / HARTOS 62649bc1,
daemon goal b18bba6f, prompt 88555124130, task #125), frozen_debug.log:

    15:34:28.921  VLM action: open_file_gui   (path ...\\coding\\feedback_collector.py)
    17:30:43.816  VLM loop hit ETA limit (1800s) at iteration 1
    17:30:43.818  VLM loop finished: 1 actions in 6979.1s (exit_reason=timeout)

and the safety audit JSONL wrote that action's record at 17:30:43 with no
error, i.e. execute_action RETURNED after ~6974 s.  The daemon's thread
(Thread-42) logged nothing in between.  The 1800 s ETA was checked only at
the top of each iteration, so one action that did not return held the
daemon for 1 h 56 min.  On Windows open_file_gui is os.startfile, an
in-process ShellExecute call that no subprocess timeout can reach; `.py` on
that machine is associated with pycharm64.exe.

Review of that fix (2026-09-27, probes vlm_probe.py q1/q2b/q6/q_stop) found
what an abandoned action still did wrong, and these tests pin each:
  F1  a late step wrote into the CLOSED run (ribbon up, task rewritten);
  F2  an action still running at the deadline was called a FAILURE, though
      it may yet succeed, so a retry could repeat it; shell steps, which
      carry their own 30 s cap, were cut off before it;
  F3  abandoned workers piled up, and the same action could be fired again
      at a target whose first attempt was still running;
  F4  Stop pressed during a stuck action was labelled a timeout, and the
      loop waited out the whole budget.

These tests drive the REAL loop with tools that block past the budget and
assert on the wall clock and on the result the caller gets.
"""
import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from integrations.vlm import local_loop  # noqa: E402
# Imported HERE, before any test runs: _run_loop's patch.dict('sys.modules')
# drops every module first imported inside it, after which `from
# integrations.vlm import activity_stream` hands back the stale package
# attribute while the loop re-imports a fresh copy -- so a test patching
# activity_stream would patch a module the loop no longer uses.
from integrations.vlm import activity_stream as act  # noqa: E402
# Same reason, for the other modules these tests patch or share state with:
# measured 2026-09-27, the late-finish test patched a stale subprocess_safe
# and passed with the code it guards disabled.
from core import subprocess_safe  # noqa: E402
from hartos.threadlocal import thread_local_data as tld  # noqa: E402
from integrations.vlm.local_loop import run_local_agentic_loop  # noqa: E402

#: Budget given to the loop in these tests, seconds.
BUDGET_S = 1.0
#: How long the stuck tool would block if nothing bounded it.  Long enough
#: that an unbounded loop is unmistakable on the clock, short enough that a
#: mutant run still finishes.
STUCK_S = 8.0
#: Allowed overrun past the budget: the screenshot/VLM mocks, thread start,
#: and the bookkeeping after the timeout.  Far below STUCK_S - BUDGET_S.
MARGIN_S = 2.0


def _action_json(action, **fields):
    import json
    body = {'Next Action': action, 'Reasoning': 'do it', 'Status': 'IN_PROGRESS'}
    body.update(fields)
    return json.dumps(body)


_OPEN_FILE = _action_json('open_file_gui', path='C:\\x\\feedback_collector.py')
_SHELL = _action_json('shell', command='git push')


def _backend(response, call_api=None):
    b = MagicMock()
    b.route_task.return_value = 'multi_step'
    if call_api is not None:
        b._call_api.side_effect = call_api
    else:
        b._call_api.return_value = response
    b.try_taskbar_pre_check.return_value = None
    b.detect_grounding_bias.return_value = None
    b.retry_with_elimination.return_value = None
    return b


def _run_loop(execute_action, *, budget=BUDGET_S, call_api=None,
              message_extra=None, response=_OPEN_FILE, max_iterations=5):
    lct = MagicMock()
    lct.take_screenshot.return_value = 'base64'
    lct.execute_action.side_effect = execute_action
    lct.VLM_IMG_W = 1280
    lct.VLM_IMG_H = 720
    message = {'instruction_to_vlm_agent': 'Open feedback_collector.py',
               'max_ETA_in_seconds': budget}
    message.update(message_extra or {})
    # resolve_steering_agent_id is a DB lookup; its first import alone costs
    # over a second on a loaded box, which would spend the whole test budget
    # before iteration 1 and leave nothing under test.
    with patch.dict('sys.modules', {'integrations.vlm.local_computer_tool': lct}), \
            patch('integrations.vlm.qwen3vl_backend.get_qwen3vl_backend',
                  return_value=_backend(response, call_api)), \
            patch('integrations.vlm.activity_stream.resolve_steering_agent_id',
                  return_value=''), \
            patch('integrations.vlm.local_loop.time.sleep'):
        t0 = time.monotonic()
        result = run_local_agentic_loop(message, tier='inprocess',
                                        max_iterations=max_iterations)
        elapsed = time.monotonic() - t0
    return result, elapsed, lct


def _pause(seconds):
    """Block for real.  _run_loop patches local_loop.time.sleep, and that IS
    time.sleep, so a test tool that slept with it would not wait at all."""
    threading.Event().wait(seconds)


def _drain(timeout=10.0):
    """Wait until no abandoned action is still running (see F3)."""
    deadline = time.monotonic() + timeout
    while local_loop.abandoned_actions_in_flight() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert local_loop.abandoned_actions_in_flight() == 0, 'a worker never finished'


@pytest.fixture(autouse=True)
def _clean_registry():
    _drain()
    yield
    _drain()


@pytest.fixture
def stuck_tool():
    """An action that blocks like the live os.startfile did, until released."""
    release = threading.Event()
    entered = threading.Event()

    def _execute(action, tier, **_kw):
        entered.set()
        release.wait(STUCK_S)
        return {'output': 'Opened %s' % action.get('path')}

    _execute.entered = entered
    _execute.release = release
    yield _execute
    release.set()
    _drain()


def _last_action(result):
    return [r for r in result['extracted_responses'] if r['type'] == 'action'][-1]


@pytest.mark.usefixtures('computer_control_granted')
class TestTheBudgetBoundsTheActionInFlight:

    def test_a_stuck_action_returns_within_the_budget(self, stuck_tool):
        result, elapsed, _ = _run_loop(stuck_tool)
        assert stuck_tool.entered.is_set(), 'the action never started'
        assert elapsed < BUDGET_S + MARGIN_S, (
            'the loop held the caller %.1fs on a %.1fs budget' % (elapsed, BUDGET_S))

    def test_the_run_is_reported_as_timed_out(self, stuck_tool):
        result, _, _ = _run_loop(stuck_tool)
        assert result['status'] == 'incomplete'
        assert result['exit_reason'] == 'timeout'

    def test_the_caller_is_told_it_ran_out_of_time(self, stuck_tool):
        from integrations.vlm.response_view import outcome_summary
        result, _, _ = _run_loop(stuck_tool)
        assert outcome_summary(result).startswith('Ran out of time')

    def test_the_cause_is_logged(self, stuck_tool, caplog):
        with caplog.at_level('WARNING', logger='hevolve.vlm.local_loop'):
            _run_loop(stuck_tool)
        lines = [r.getMessage() for r in caplog.records
                 if r.name == 'hevolve.vlm.local_loop']
        assert any('open_file_gui' in m and 'budget' in m for m in lines), lines

    def test_a_timeout_after_two_failures_is_still_called_a_timeout(self, stuck_tool):
        """The third failure in a row would otherwise trip the
        3-consecutive-errors exit and report 'action_error', hiding that the
        run was cut off by its budget."""
        calls = []

        def _execute(action, tier, **kw):
            calls.append(1)
            if len(calls) < 3:
                return {'output': '', 'error': 'window not found'}
            return stuck_tool(action, tier, **kw)

        result, elapsed, _ = _run_loop(_execute)
        assert len(calls) == 3
        assert result['exit_reason'] == 'timeout'
        assert elapsed < BUDGET_S + MARGIN_S

    def test_no_budget_left_means_the_action_never_starts(self):
        """The VLM call itself can spend the rest of the budget; the action
        must then not fire on the owner's machine after the deadline.  That
        one did NOT happen, so it is a plain failure."""
        started = threading.Event()

        def _execute(action, tier, **_kw):
            started.set()
            return {'output': 'Opened'}

        def _slow_vlm(_messages):
            _pause(0.6)
            return _OPEN_FILE

        result, _, _ = _run_loop(_execute, budget=0.3, call_api=_slow_vlm)
        assert not started.is_set()
        assert result['exit_reason'] == 'timeout'
        last = _last_action(result)['content']
        assert last['ok'] is False
        assert 'not started' in last['result']


@pytest.mark.usefixtures('computer_control_granted')
class TestAnAbandonedActionIsNotCalledAFailure:
    """F2: nobody knows whether it worked; say exactly that."""

    def test_its_result_is_unknown_not_failed(self, stuck_tool):
        result, _, _ = _run_loop(stuck_tool)
        last = _last_action(result)['content']
        assert last['action'] == 'open_file_gui'
        assert last['ok'] is None
        assert 'still running when the time ran out' in last['result']
        assert 'result unknown' in last['result']
        assert 'FAILED' not in last['result']

    def test_the_model_is_told_to_check_before_repeating_it(self, stuck_tool):
        from integrations.vlm.response_view import observation_text
        result, _, _ = _run_loop(stuck_tool)
        assert 'before repeating it' in observation_text(result)

    def test_a_shell_step_gets_its_own_cap_as_grace(self):
        """A shell step bounds itself (SHELL_COMMAND_TIMEOUT_S), so the loop
        waits that long past its budget and reports the REAL result."""
        def _slow_shell(action, tier, **_kw):
            _pause(1.6)
            return {'output': 'Exit code: 0\npushed', 'status': 'ok'}

        with patch.object(local_loop, 'SHELL_COMMAND_TIMEOUT_S', 2.0):
            result, elapsed, _ = _run_loop(
                _slow_shell, budget=0.6, response=_SHELL, max_iterations=1)
        last = _last_action(result)['content']
        assert last['ok'] is True, last
        assert 'pushed' in last['result']
        assert elapsed < 0.6 + 2.0 + MARGIN_S

    def test_the_grace_is_bounded_too(self):
        release = threading.Event()

        def _stuck_shell(action, tier, **_kw):
            release.wait(STUCK_S)
            return {'output': 'Exit code: 0', 'status': 'ok'}

        try:
            with patch.object(local_loop, 'SHELL_COMMAND_TIMEOUT_S', 0.5):
                result, elapsed, _ = _run_loop(
                    _stuck_shell, budget=0.5, response=_SHELL)
        finally:
            release.set()
        assert elapsed < 0.5 + 0.5 + MARGIN_S
        assert result['exit_reason'] == 'timeout'
        assert _last_action(result)['content']['ok'] is None

    def test_a_gui_action_gets_no_grace(self):
        def _slow_open(action, tier, **_kw):
            _pause(1.6)
            return {'output': 'Opened'}

        with patch.object(local_loop, 'SHELL_COMMAND_TIMEOUT_S', 2.0):
            result, _, _ = _run_loop(_slow_open, budget=0.6, max_iterations=1)
        assert _last_action(result)['content']['ok'] is None


@pytest.mark.usefixtures('computer_control_granted')
class TestAbandonedWorkersDoNotPileUp:
    """F3: one still-running action per target; the count is visible."""

    def test_the_same_target_is_refused_while_its_first_attempt_runs(self, stuck_tool):
        _run_loop(stuck_tool)
        assert local_loop.abandoned_actions_in_flight() == 1

        calls = []

        def _record(action, tier, **_kw):
            calls.append(action.get('path'))
            return {'output': 'Opened'}

        result, _, _ = _run_loop(_record, budget=30, max_iterations=1)
        assert calls == [], 'fired again at a target whose first attempt still runs'
        first = _last_action(result)['content']
        assert first['ok'] is False
        assert 'still running' in first['result']

    def test_a_different_target_still_runs(self, stuck_tool):
        _run_loop(stuck_tool)
        calls = []

        def _record(action, tier, **_kw):
            calls.append(action.get('path'))
            return {'output': 'Opened'}

        _run_loop(_record, budget=30, max_iterations=1,
                  response=_action_json('open_file_gui', path='C:\\x\\other.py'))
        assert calls == ['C:\\x\\other.py']

    def test_the_target_frees_when_the_worker_finishes(self, stuck_tool):
        _run_loop(stuck_tool)
        stuck_tool.release.set()
        _drain()
        calls = []

        def _record(action, tier, **_kw):
            calls.append(action.get('path'))
            return {'output': 'Opened'}

        _run_loop(_record, budget=30, max_iterations=1)
        assert calls == ['C:\\x\\feedback_collector.py']

    def test_the_count_is_logged(self, stuck_tool, caplog):
        with caplog.at_level('WARNING', logger='hevolve.vlm.local_loop'):
            _run_loop(stuck_tool)
        assert any('1 abandoned action(s) still running' in r.getMessage()
                   for r in caplog.records), [r.getMessage() for r in caplog.records]


@pytest.mark.usefixtures('computer_control_granted')
class TestStopEndsTheWait:
    """F4: Stop is Stop, not a timeout, and it does not wait out the budget."""

    def test_stop_during_a_stuck_action(self, stuck_tool):
        marks = {}

        def _press_stop():
            stuck_tool.entered.wait(5)
            _pause(0.3)
            marks['stop'] = time.monotonic()
            local_loop.request_stop('', 'p-stop')

        threading.Thread(target=_press_stop, daemon=True).start()
        result, _, _ = _run_loop(stuck_tool, budget=STUCK_S - 1,
                                 message_extra={'prompt_id': 'p-stop'})
        after_stop = time.monotonic() - marks['stop']
        assert result['exit_reason'] == 'stopped'
        assert after_stop < 1.5, 'the loop kept waiting %.1fs after Stop' % after_stop
        last = _last_action(result)['content']
        assert last['ok'] is None
        assert 'Stop' in last['result']


@pytest.mark.usefixtures('computer_control_granted')
class TestALateStepCannotReopenTheRun:
    """F1: after the deadline the run is closed; the abandoned worker's own
    steps (the shell tool announces itself) must not write into it."""

    def test_nothing_reaches_the_owner_after_the_run_closed(self, tmp_path, monkeypatch):
        from agent_ledger import SmartLedger, TaskStatus
        from agent_ledger.backends import JSONBackend
        from hartos.threadlocal import thread_local_data as tld

        monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
        ledger = SmartLedger('p1', 'u1_p1',
                             backend=JSONBackend(storage_dir=str(tmp_path)))
        events = []
        worker_done, worker_entered = threading.Event(), threading.Event()

        def _shell_like(action, tier, **_kw):
            worker_entered.set()
            # what hart_intelligence_entry._handle_shell_command_tool does
            run = act.current_run(user_id=tld.get_user_id() or 'u1',
                                  prompt_id=tld.get_prompt_id() or '')
            run.step(iteration=1, action='shell', phase='executing',
                     caption='Shell command: git push')
            _pause(3.0)          # its own bounded wait, past budget + grace
            run.step(iteration=1, action='shell', phase='failed',
                     caption='Shell command: git push', error='rc 1')
            run.finish(exit_reason='action_error')
            worker_done.set()
            return {'output': 'Exit code: 1'}

        with patch.object(act, '_ledger_for', return_value=ledger), \
                patch.object(act, '_ribbon',
                             side_effect=lambda show, text=None: events.append(
                                 ('ribbon', show, time.monotonic()))), \
                patch('integrations.social.realtime.on_notification',
                      side_effect=lambda uid, p: events.append(
                          ('fan', p.get('phase'), p.get('run_done'), time.monotonic()))), \
                patch.object(local_loop, 'SHELL_COMMAND_TIMEOUT_S', 0.3):
            result, _, _ = _run_loop(
                _shell_like, budget=2.0, response=_SHELL,
                message_extra={'user_id': 'u1', 'prompt_id': 'p1'})
            returned = time.monotonic()
            # The ledger save before iteration 1 costs real time on a loaded
            # box; the budget leaves room for it, and this says it was left.
            assert worker_entered.is_set(), 'the action never ran: nothing tested'
            assert worker_done.wait(8)

        assert result['exit_reason'] == 'timeout'
        late = [e for e in events if e[-1] > returned]
        assert late == [], 'written into the closed run: %r' % late
        task = [t for t in ledger.tasks.values()
                if t.task_id.startswith('computer_use_')][0]
        assert task.status == TaskStatus.FAILED
        assert task.context['phase'] != 'failed' or task.context['action'] != 'shell'


@pytest.mark.usefixtures('computer_control_granted')
class TestAFastActionIsUnchanged:

    def test_its_result_and_thread_context_reach_the_loop(self):
        """The action runs on a worker now; the shell tool inside it reads
        prompt_id and the activity run from hartos.threadlocal, so both must
        still be the loop's."""
        from hartos.threadlocal import thread_local_data
        seen = {}

        def _execute(action, tier, **_kw):
            seen['prompt_id'] = thread_local_data.get_prompt_id()
            seen['run'] = thread_local_data.get_activity_run()
            return {'output': 'Opened it'}

        # No user_id: record_activity then stops before the ledger, so the
        # test writes nothing to the real task store.
        result, _, _ = _run_loop(
            _execute, budget=30, message_extra={'prompt_id': 'p-77'})
        assert seen['prompt_id'] == 'p-77'
        assert seen['run'] and seen['run']['prompt_id'] == 'p-77'
        first = result['extracted_responses'][0]
        assert first['content']['ok'] is True
        assert first['content']['result'] == 'Opened it'

    def test_an_action_that_raises_is_still_an_iteration_error(self):
        def _execute(action, tier, **_kw):
            raise RuntimeError('pyautogui exploded')

        result, _, _ = _run_loop(_execute, budget=30)
        assert result['exit_reason'] == 'action_error'
        assert result['extracted_responses'][0]['type'] == 'error'
        assert 'pyautogui exploded' in result['extracted_responses'][0]['content']


class TestTheHelperDirectly:
    """_execute_within_budget's own contract (peer review, 2026-09-27)."""

    def test_an_action_that_finishes_just_after_the_deadline_is_done(self):
        """It finished between call_bounded giving up and the loop taking
        the lock: report its real result, and leave its run stamp open."""
        faked = []

        def _late_finish(fn, wait, **_kw):
            faked.append(1)
            fn()                          # it completes...
            return False, None, None      # ...just after the wait gave up

        tld.set_activity_run('r-late', user_id='u', prompt_id='p')
        try:
            with patch.object(subprocess_safe, 'call_bounded', side_effect=_late_finish), \
                    patch.object(act, 'close_run_stamp') as close:
                outcome, result = local_loop._execute_within_budget(
                    lambda a, t, **k: {'output': 'Opened'},
                    {'action': 'open_file_gui', 'path': 'x.py'}, 'inprocess',
                    safety=True, verify=False, remaining_s=1.0, grace_s=0.0)
        finally:
            tld.clear_activity_run()
        assert faked == [1], 'the fake never ran: this tested nothing'
        assert (outcome, result) == ('done', {'output': 'Opened'})
        close.assert_not_called()
        assert local_loop.abandoned_actions_in_flight() == 0

    def test_the_grace_it_applies_is_the_one_it_is_given(self):
        """One grace, computed once by the loop and passed in, so the text
        the model reads can never promise a grace that was not waited."""
        def _slow(action, tier, **_kw):
            _pause(0.9)
            return {'output': 'Opened'}

        outcome, result = local_loop._execute_within_budget(
            _slow, {'action': 'open_file_gui', 'path': 'y.py'}, 'inprocess',
            safety=True, verify=False, remaining_s=0.3, grace_s=2.0)
        assert (outcome, result) == ('done', {'output': 'Opened'})

    def test_an_unknown_result_says_so_in_its_status(self, stuck_tool):
        outcome, result = local_loop._execute_within_budget(
            stuck_tool, {'action': 'open_file_gui', 'path': 'z.py'}, 'inprocess',
            safety=True, verify=False, remaining_s=0.3, grace_s=0.0)
        assert outcome == 'abandoned'
        assert result['status'] == local_loop.ACTION_STATUS_UNKNOWN
        assert 'error' not in result          # not a failure either

    def test_an_abandoned_workers_run_stamp_is_closed(self, stuck_tool):
        """The worker's adopted stamp is marked, so a step it announces
        between the abandonment and the loop's finish_run is refused too
        (record_activity's closed-task check only covers after finish)."""
        from hartos.threadlocal import thread_local_data as tld
        seen = {}

        def _stuck(action, tier, **kw):
            seen['stamp'] = tld.get_activity_run()
            return stuck_tool(action, tier, **kw)

        tld.set_activity_run('r-stamp', user_id='u', prompt_id='p')
        try:
            outcome, _ = local_loop._execute_within_budget(
                _stuck, {'action': 'open_file_gui', 'path': 's.py'}, 'inprocess',
                safety=True, verify=False, remaining_s=0.3, grace_s=0.0)
            assert outcome == 'abandoned'
            assert seen['stamp'].get('closed') is True
            # the loop's own stamp is its own copy, still open
            assert not tld.get_activity_run().get('closed')
        finally:
            tld.clear_activity_run()
