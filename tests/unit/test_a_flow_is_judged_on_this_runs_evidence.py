"""A flow's goal is judged on what THIS run recorded, never an earlier run's.

Live 2026-11-23 10:18-10:20 IST, speech agent 10202_343 on the desktop, from
the tablet: the child typed "Haan" after the name call, the flow finished, and
at the next run's start the judge said flow 0 was NOT met, quoting "person
answered at step 1: Hey, do you feel me now", an answer from 2026-11-13.  So
flow 0 ran again and ended in praise every time ("i keep seeing super arjun").

Why: _reuse_flow_evidence promised "what this run of flow recorded" but read
every answer kept on the ledger task's context (record_user_answer keeps them
across runs, by design: the window scans and reply checks read them so) and
the task's result, which a re-run cannot overwrite (COMPLETED is terminal), so
step 1 carried 11-13's answer twice, once as an answer and once as its result.

The run window (agreed by hartos-94 and hartos-e1 before any code): the
session's Action keeps ``run_started_at`` (UTC), set when the Action is built
and again when _reset_finished_pointer rewinds a finished run to action 1.
_reuse_flow_evidence, and only it, keeps an answer stamped at or after that
moment, and a step's result only when the task is COMPLETED and completed it
at or after that moment.  Every other reader stays cross-run.

The real reuse_recipe / lifecycle_hooks helpers; the ledger is a stand-in.

    python -m pytest tests/unit/test_a_flow_is_judged_on_this_runs_evidence.py -q --noconftest
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

KEY = 'u-child_77'
OLD_ANSWER = 'Hey, do you feel me now'           # 23 chars, 2026-11-13
OLD_LONG = 'x' * 374                              # 374 chars, 2026-11-13
TODAY = datetime(2026, 11, 23, 4, 48, 6, tzinfo=timezone.utc)
RUN_START = datetime(2026, 11, 23, 4, 45, 0, tzinfo=timezone.utc)


def _utc(dt):
    return dt.isoformat()


def _local_naive(dt):
    """How agent_ledger stamps completed_at: datetime.now().isoformat(), this
    machine's local time with no zone."""
    return dt.astimezone().replace(tzinfo=None).isoformat()


def _entry(answer, when):
    return {'answer': answer, 'timestamp': _utc(when), 'flow': 0}


def _task(entries, status='completed', result=None, completed_at=None, description=''):
    return SimpleNamespace(context={'user_input_evidence': list(entries)}, status=status,
                           result=result, completed_at=completed_at, description=description)


@pytest.fixture
def rr(monkeypatch, tmp_path):
    import core.platform_paths as platform_paths
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    from hartos import reuse_recipe as rr
    monkeypatch.setattr(rr, 'user_tasks', {}, raising=False)
    monkeypatch.setattr(rr, '_reuse_action_flow', lambda _up, _aid: 0)
    with Flask('flow-evidence-test').app_context():
        yield rr


def _ledger(monkeypatch, rr, tasks):
    import hartos.lifecycle_hooks as lh
    ledger = SimpleNamespace(tasks=tasks)
    monkeypatch.setattr(rr, '_reuse_session_ledger', lambda _up: ledger)
    monkeypatch.setitem(lh._ledger_registry, KEY, ledger)
    return ledger


def _session(rr, run_started_at):
    action = rr.Action([{'action_id': 1}, {'action_id': 2}, {'action_id': 3}])
    action.run_started_at = run_started_at
    rr.user_tasks[KEY] = action
    return action


def _the_live_ledger(monkeypatch, rr):
    """The tablet's ledger shape, 2026-11-23 (measured by hartos-94: times,
    keys and lengths only)."""
    nov13 = datetime(2026, 11, 13, 9, 49, tzinfo=timezone.utc)
    return _ledger(monkeypatch, rr, {
        'action_1': _task([_entry(OLD_ANSWER, nov13),
                           _entry(OLD_LONG, nov13 + timedelta(minutes=43)),
                           _entry('Haan', TODAY)],
                          result=OLD_ANSWER,
                          completed_at=_local_naive(datetime(2026, 11, 13, 9, 49, 25, tzinfo=timezone.utc)),
                          description='call Arjun by name'),
        'action_2': _task([], result='praised on 11-13',
                          completed_at=_local_naive(datetime(2026, 11, 13, 10, 32, tzinfo=timezone.utc)),
                          description='if he responds, praise'),
        'action_3': _task([], result='Saved at name_response',
                          completed_at=_local_naive(TODAY + timedelta(seconds=23)),
                          description='save the response'),
    })


def test_the_judge_gets_only_this_runs_answer_and_result(rr, monkeypatch):
    _the_live_ledger(monkeypatch, rr)
    _session(rr, RUN_START)
    observed, _steps = rr._reuse_flow_evidence(KEY, 0)
    assert observed.splitlines() == ['person answered at step 1: Haan',
                                     'step 3 result: Saved at name_response'], observed
    assert 'feel me now' not in observed


def test_a_cold_build_leaves_out_everything_before_it(rr, monkeypatch):
    _the_live_ledger(monkeypatch, rr)
    # A real build: Action.__init__ stamps the run now, after every entry.
    rr.user_tasks[KEY] = rr.Action([{'action_id': 1}, {'action_id': 2}, {'action_id': 3}])
    assert isinstance(rr.user_tasks[KEY].run_started_at, datetime)
    assert rr.user_tasks[KEY].run_started_at.tzinfo is not None
    observed, _steps = rr._reuse_flow_evidence(KEY, 0)
    assert observed == '', observed


def test_a_resumed_run_keeps_its_own_earlier_answers(rr, monkeypatch):
    """No rewind mid-run: a turn that continues the run keeps its stamp, so
    the answers given on its earlier turns are still this run's."""
    import hartos.lifecycle_hooks as lh
    first, second = RUN_START + timedelta(minutes=1), RUN_START + timedelta(minutes=5)
    _ledger(monkeypatch, rr, {
        'action_1': _task([_entry('Haan', first)], status='in_progress'),
        'action_2': _task([_entry('sa', second)], status='in_progress'),
    })
    action = _session(rr, RUN_START)
    action.current_action = 2                    # mid-run, not past the end
    assert lh._reset_finished_pointer(KEY, rr.user_tasks) is False
    assert action.run_started_at == RUN_START
    observed, _steps = rr._reuse_flow_evidence(KEY, 0)
    assert observed.splitlines() == ['person answered at step 1: Haan',
                                     'person answered at step 2: sa'], observed


def test_a_rewind_after_a_finished_run_leaves_the_previous_runs_answers_out(rr, monkeypatch):
    import hartos.lifecycle_hooks as lh
    _ledger(monkeypatch, rr, {
        'action_1': _task([_entry('Haan', RUN_START + timedelta(minutes=1))]),
    })
    action = _session(rr, RUN_START)
    action.current_action = 4                    # ran off the end of 3 actions
    assert lh._reset_finished_pointer(KEY, rr.user_tasks) is True
    assert action.current_action == 1
    assert action.run_started_at > RUN_START + timedelta(minutes=1)
    observed, _steps = rr._reuse_flow_evidence(KEY, 0)
    assert 'Haan' not in observed, observed


def test_the_other_readers_still_read_every_run(rr, monkeypatch):
    """Only the judge's evidence is windowed: the reply checks and window
    scans read the whole record, as before."""
    import hartos.lifecycle_hooks as lh
    _the_live_ledger(monkeypatch, rr)
    _session(rr, RUN_START)
    assert lh.recorded_answers(KEY, 1, 0) == [OLD_ANSWER, OLD_LONG, 'Haan']
    assert len(lh._recorded_answer_entries(KEY, 1, 0)) == 3
    # _requests_that_answered reads the same three entries (no request kept
    # on any of them, so no request is named).
    assert lh._requests_that_answered(KEY, 1) == set()


def test_a_failed_step_with_a_fresh_time_does_not_bring_back_a_stale_result(rr, monkeypatch):
    """fail()/cancel() stamp completed_at without touching result: a stale
    result can sit under today's time on a task that did not complete."""
    _ledger(monkeypatch, rr, {
        'action_1': _task([], status='failed', result='an answer from 11-13',
                          completed_at=_local_naive(TODAY)),
    })
    _session(rr, RUN_START)
    observed, _steps = rr._reuse_flow_evidence(KEY, 0)
    assert 'from 11-13' not in observed, observed


def test_an_answer_without_a_readable_time_is_left_out(rr, monkeypatch):
    _ledger(monkeypatch, rr, {
        'action_1': _task([{'answer': 'no time', 'flow': 0},
                           {'answer': 'bad time', 'flow': 0, 'timestamp': 'yesterday'},
                           _entry('Haan', TODAY)]),
    })
    _session(rr, RUN_START)
    observed, _steps = rr._reuse_flow_evidence(KEY, 0)
    assert observed.splitlines() == ['person answered at step 1: Haan'], observed


def test_times_compare_as_times_not_as_text(rr, monkeypatch):
    """A result completed at 10:18 IST on the run's day is after a run that
    started at 04:45 UTC (10:15 IST), though '2026-11-23T10:18' would sort
    after '2026-11-23T04:45' as text for the wrong reason, and an answer
    stamped '...+05:30' must be read in its own zone."""
    ist = timezone(timedelta(hours=5, minutes=30))
    _ledger(monkeypatch, rr, {
        'action_1': _task([{'answer': 'before, in IST', 'flow': 0,
                            'timestamp': datetime(2026, 11, 23, 10, 10, tzinfo=ist).isoformat()},
                           {'answer': 'after, in IST', 'flow': 0,
                            'timestamp': datetime(2026, 11, 23, 10, 20, tzinfo=ist).isoformat()}],
                          status='in_progress'),
    })
    _session(rr, RUN_START)                       # 04:45 UTC = 10:15 IST
    observed, _steps = rr._reuse_flow_evidence(KEY, 0)
    assert observed.splitlines() == ['person answered at step 1: after, in IST'], observed


def test_with_no_session_the_evidence_reads_as_before(rr, monkeypatch):
    """No Action for this session (none built in this process): nothing marks
    a run, so the reader keeps its earlier behaviour rather than guessing."""
    import hartos.lifecycle_hooks as lh
    _ledger(monkeypatch, rr, {
        'action_1': SimpleNamespace(description='call Arjun by name', result='sent: Arjun!'),
    })
    monkeypatch.setattr(lh, 'recorded_answers', lambda _up, aid, flow: ['he looked'])
    observed, _steps = rr._reuse_flow_evidence(KEY, 0)
    assert observed.splitlines() == ['person answered at step 1: he looked',
                                     'step 1 result: sent: Arjun!'], observed
