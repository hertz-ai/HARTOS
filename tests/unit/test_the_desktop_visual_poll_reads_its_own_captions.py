"""On a desktop the camera poll reads the desktop's own captions, and a
session start never piles up another scheduled job.

MEASURED on the owner's desktop, 2026-11-16 (Nunba build 75): the caption
server writes the phone camera's captions into nunba_db.sqlite `actions` as
'Video Reasoning' (500 rows, the newest that day), and the reads behind the
camera engagement path are local in a bundled app (get_visual_context_api ->
/get_visual_bymins, get_frame -> the FrameStore).  Yet server.log said
"Skipping 2s visual-poll job - ACTION_API not configured" at every session
start, and call_visual_task returned None for every flow's visual task:
reuse_recipe read config.get('ACTION_API', '') raw, '' on every desktop,
instead of the one resolver core.config_cache.get_action_api(), which on a
desktop is this node's own /create_action (it answers GET ?user_id=).

Turning the poll on exposed a second defect: create_schedule named every job
job_<second>, so each session start added one more job that nothing removed,
and two jobs added in the same second collided (ConflictingIdError) and the
except skipped the rest.

Owner, 2026-11-16: "no new code is needed in framework to make this happen
... if there is a defect we need to fix it".

    python -m pytest tests/unit/test_the_desktop_visual_poll_reads_its_own_captions.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

# ACTION_API is read when the module loads, so each case loads it in a fresh
# process with the config it names; the vault is never read (the cached config
# is set first).
_PROBE = textwrap.dedent('''
    import json, os, sys
    sys.path.insert(0, {root!r})
    import core.config_cache as cc
    cc._config = json.loads({config!r})
    from hartos import reuse_recipe as rr
    print('ACTION_API=' + json.dumps(rr.ACTION_API))
''')


def _action_api(config, bundled):
    env = {k: v for k, v in os.environ.items() if k not in ('NUNBA_BUNDLED', 'NUNBA_PORT')}
    env['HEVOLVE_DB_PATH'] = ':memory:'
    env['NUNBA_PORT'] = '5000'
    if bundled:
        env['NUNBA_BUNDLED'] = '1'
    out = subprocess.run([sys.executable, '-c', _PROBE.format(root=ROOT, config=json.dumps(config))],
                         capture_output=True, text=True, env=env, timeout=300, cwd=ROOT)
    for line in out.stdout.splitlines():
        if line.startswith('ACTION_API='):
            return json.loads(line[len('ACTION_API='):])
    raise AssertionError(f'no ACTION_API line; stderr tail: {out.stderr[-800:]}')


def test_a_desktop_with_nothing_configured_polls_its_own_captions():
    assert _action_api({}, bundled=True) == 'http://localhost:5000/create_action'


def test_a_configured_action_api_still_wins_on_a_desktop():
    assert _action_api({'ACTION_API': 'http://cloud.example/x'}, bundled=True) == 'http://cloud.example/x'


def test_off_the_desktop_a_node_without_the_key_keeps_the_poll_off():
    assert _action_api({}, bundled=False) == ''


# ── create_schedule: one job per person, agent and task ──

class _Scheduler:
    def __init__(self):
        self.jobs = {}
        self.calls = []

    def add_job(self, func, trigger=None, id=None, replace_existing=False, args=None, **_k):
        self.calls.append((id, replace_existing))
        if id in self.jobs and not replace_existing:
            raise RuntimeError(f'ConflictingIdError: {id}')
        self.jobs[id] = (func, args)


@pytest.fixture
def rr(monkeypatch, tmp_path):
    from hartos import reuse_recipe as rr
    recipe = tmp_path / 'r.json'
    recipe.write_text(json.dumps({
        'actions': [],
        'scheduled_tasks': [
            {'persona': 'Child', 'cron_expression': '0 9 * * *', 'job_description': 'a',
             'action_entry_point': 'x'},
            {'persona': 'Child', 'cron_expression': '0 10 * * *', 'job_description': 'b',
             'action_entry_point': 'y'}],
        'visual_scheduled_tasks': [
            {'persona': 'Child', 'cron_expression': '* * * * *', 'job_description': 'engage'}],
    }), encoding='utf-8')
    sched = _Scheduler()
    monkeypatch.setattr(rr, 'scheduler', sched)
    monkeypatch.setattr(rr, 'ACTION_API', 'http://localhost:5000/create_action')
    monkeypatch.setattr(rr, 'get_flow_number', lambda *_a, **_k: (0, 'Child'))
    monkeypatch.setattr(rr.helper_fun, 'safe_prompt_path', lambda *_a, **_k: str(recipe))
    rr._sched = sched
    return rr


def test_a_second_session_start_replaces_its_jobs_instead_of_adding_more(rr):
    rr.create_schedule(1794540972213, 'u-child')
    first = set(rr._sched.jobs)
    rr.create_schedule(1794540972213, 'u-child')
    assert set(rr._sched.jobs) == first
    assert len([j for j in first if j.startswith('visual_poll_')]) == 1
    assert all(replace for _id, replace in rr._sched.calls)


def test_two_tasks_added_in_the_same_second_are_both_scheduled(rr):
    rr.create_schedule(1794540972213, 'u-child')
    assert len([j for j in rr._sched.jobs if j.startswith('sched_')]) == 2
    assert len([j for j in rr._sched.jobs if j.startswith('visual_task_')]) == 1


def test_another_person_or_agent_gets_its_own_jobs(rr):
    rr.create_schedule(1794540972213, 'u-child')
    rr.create_schedule(1794540972213, 'u-other')
    rr.create_schedule(1794818852965, 'u-child')
    assert len([j for j in rr._sched.jobs if j.startswith('visual_poll_')]) == 3


# -- the poll once it runs on every desktop (review of 25758b0f7) ------------
#
# rv_hartos_2575_8ddf measured on the commit: 500 stored captions older than
# five minutes gave 502 INFO lines per 2 s poll, and the /visual_agent POST
# gave up after its 15 s read while the turn ran on, so the next tick posted a
# second visual turn.

UID, PID = 'u-child', 1794540972213


class _Resp:
    def __init__(self, rows):
        self.status_code, self._rows = 200, rows

    def json(self):
        return self._rows


def _rows(n, minutes_ago):
    from datetime import datetime, timedelta
    stamp = (datetime.utcnow() - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%dT%H:%M:%S')
    return [{'zeroshot_label': 'Video Reasoning', 'action': f'a child plays {i}',
             'action_id': i, 'created_date': stamp} for i in range(n)]


@pytest.fixture
def poll(monkeypatch):
    from hartos import reuse_recipe as rr
    got = {'gets': 0, 'posts': []}
    state = {'rows': []}

    def _get(method, url, **_k):
        got['gets'] += 1
        return _Resp(state['rows'])

    def _post_once(url, **k):
        got['posts'].append(k.get('timeout'))
        if state.get('server_begins_the_turn'):
            # What /visual_agent's visual_based_execution does on entry.
            import time
            rr._visual_turn_running[(UID, str(PID))] = time.monotonic()
        if state.get('post_raises'):
            raise state['post_raises']
        return _Resp([])

    monkeypatch.setattr(rr, 'ACTION_API', 'http://localhost:5000/create_action')
    monkeypatch.setattr(rr, 'pooled_request', _get)
    monkeypatch.setattr(rr, 'pooled_post', _post_once)
    monkeypatch.setattr(rr, '_visual_poll_paused_until', {})
    monkeypatch.setattr(rr, '_visual_turn_running', {})
    return rr, got, state


def _info_lines(caplog):
    import logging
    return [r for r in caplog.records
            if r.levelno >= logging.INFO and r.name == 'hartos.reuse_recipe']


def test_old_captions_log_nothing_per_caption_and_post_nothing(poll, caplog):
    import logging
    rr, got, state = poll
    state['rows'] = _rows(500, minutes_ago=30)
    with caplog.at_level(logging.DEBUG, logger='hartos.reuse_recipe'):
        assert rr.call_visual_task('engage', UID, PID) is None
    assert got['posts'] == []
    assert _info_lines(caplog) == [], [r.getMessage() for r in _info_lines(caplog)]


def test_recent_captions_start_one_visual_turn_with_one_line(poll, caplog):
    import logging
    rr, got, state = poll
    state['rows'] = _rows(120, minutes_ago=1)
    with caplog.at_level(logging.INFO, logger='hartos.reuse_recipe'):
        assert rr.call_visual_task('engage', UID, PID) == 'done'
    assert len(got['posts']) == 1
    assert len(_info_lines(caplog)) <= 2      # the dispatch line and the response


def test_a_running_visual_turn_keeps_the_poll_from_starting_another(poll):
    import time
    rr, got, state = poll
    state['rows'] = _rows(3, minutes_ago=1)
    rr._visual_turn_running[(UID, str(PID))] = time.monotonic()
    for _tick in range(5):
        assert rr.call_visual_task('engage', UID, PID) is None
    assert got['posts'] == [] and got['gets'] == 0
    rr._visual_turn_running.clear()
    assert rr.call_visual_task('engage', UID, PID) == 'done'
    assert len(got['posts']) == 1


def test_a_mark_older_than_the_turn_budget_no_longer_holds_the_poll(poll):
    import time
    rr, got, state = poll
    state['rows'] = _rows(3, minutes_ago=1)
    rr._visual_turn_running[(UID, str(PID))] = time.monotonic() - rr._VISUAL_TURN_MAX_S - 1
    assert rr.call_visual_task('engage', UID, PID) == 'done'


def test_a_post_that_stops_waiting_is_a_started_turn_not_an_error(poll, caplog):
    """The server began the turn (it marked itself running) and the POST
    stopped waiting for it: a turn still running, not a failure."""
    import logging
    import time
    import requests
    rr, got, state = poll
    state['rows'] = _rows(3, minutes_ago=1)
    state['post_raises'] = requests.exceptions.ReadTimeout('read timed out')
    state['server_begins_the_turn'] = True
    with caplog.at_level(logging.INFO, logger='hartos.reuse_recipe'):
        assert rr.call_visual_task('engage', UID, PID) == 'done'
    assert len(got['posts']) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_post_that_times_out_with_no_turn_running_is_an_error(poll, caplog):
    """Review of 06f4aaea9, S1a: a server that never began the turn times
    out too; that is not 'started'."""
    import logging
    import requests
    rr, got, state = poll
    state['rows'] = _rows(3, minutes_ago=1)
    state['post_raises'] = requests.exceptions.ReadTimeout('read timed out')
    with caplog.at_level(logging.INFO, logger='hartos.reuse_recipe'):
        assert rr.call_visual_task('engage', UID, PID) == 'error'
    assert len(got['posts']) == 1
    assert [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not [r for r in caplog.records if 'Visual task started' in r.getMessage()]


def test_the_visual_turn_marks_itself_running_and_always_clears(monkeypatch):
    from hartos import reuse_recipe as rr
    monkeypatch.setattr(rr, '_visual_turn_running', {})
    seen = []

    def _turn(task, uid, pid):
        seen.append(dict(rr._visual_turn_running))
        if task == 'boom':
            raise RuntimeError('turn failed')
        return 'done'

    monkeypatch.setattr(rr, '_run_visual_turn', _turn)
    assert rr.visual_based_execution('engage', UID, PID) == 'done'
    assert (UID, str(PID)) in seen[0] and rr._visual_turn_running == {}
    with pytest.raises(RuntimeError):
        rr.visual_based_execution('boom', UID, PID)
    assert rr._visual_turn_running == {}


@pytest.fixture
def blocking_turn(monkeypatch):
    """visual_based_execution over a _run_visual_turn that holds until
    released, counting how many turns ran."""
    import threading
    from hartos import reuse_recipe as rr
    monkeypatch.setattr(rr, '_visual_turn_running', {})
    state = {'runs': 0, 'inside': threading.Event(), 'release': threading.Event(),
             'each': [threading.Event() for _ in range(4)]}

    def _turn(task, uid, pid):
        mine = state['each'][state['runs']]
        state['runs'] += 1
        state['inside'].set()
        for _ in range(200):   # ends on the shared release or its own
            if state['release'].is_set() or mine.wait(0.05):
                break
        return 'done'

    monkeypatch.setattr(rr, '_run_visual_turn', _turn)
    yield rr, state
    state['release'].set()


def test_a_second_visual_turn_while_one_runs_is_not_started(blocking_turn):
    """Review of 06f4aaea9, S1b: the visual_poll_ and visual_task_ jobs of
    one person and agent both reach /visual_agent; the second turn does not
    start, and its return leaves the running turn's mark in place."""
    import threading
    rr, state = blocking_turn
    first = threading.Thread(target=rr.visual_based_execution, args=('engage', UID, PID), daemon=True)
    first.start()
    assert state['inside'].wait(10)
    reply = rr.visual_based_execution('engage', UID, PID)
    assert state['runs'] == 1, 'a second visual turn ran beside the first'
    assert 'already running' in reply
    assert (UID, str(PID)) in rr._visual_turn_running, \
        "the refused turn cleared the running turn's mark"
    state['release'].set()
    first.join(10)
    assert rr._visual_turn_running == {}


def test_two_visual_turns_arriving_together_run_once(blocking_turn, monkeypatch):
    """The check and the mark are one step: two turns that both look before
    either marks (held here at the look) still start only one."""
    import threading
    rr, state = blocking_turn
    real_check = rr._visual_turn_is_running
    both_looked = threading.Barrier(2)

    def _look(uid, pid):
        seen = real_check(uid, pid)
        try:
            both_looked.wait(0.5)
        except threading.BrokenBarrierError:
            pass
        return seen

    monkeypatch.setattr(rr, '_visual_turn_is_running', _look)
    threads = [threading.Thread(target=rr.visual_based_execution, args=('engage', UID, PID), daemon=True)
               for _ in range(2)]
    for t in threads:
        t.start()
    assert state['inside'].wait(10)
    for t in threads:
        if t.is_alive() and state['runs'] == 1:
            t.join(2)
    assert state['runs'] == 1, 'both turns ran'
    state['release'].set()
    for t in threads:
        t.join(10)


def test_a_turn_that_ends_late_leaves_the_newer_turns_mark(blocking_turn):
    """A mark older than the turn budget no longer holds (a turn that never
    ended must not stop the poll for good), so a newer turn starts beside
    it; when the old turn finally ends, it clears its own mark only, not
    the newer turn's (review of 06f4aaea9, S1b)."""
    import threading
    import time
    rr, state = blocking_turn
    key = (UID, str(PID))
    old = threading.Thread(target=rr.visual_based_execution, args=('engage', UID, PID), daemon=True)
    old.start()
    assert state['inside'].wait(10)
    rr._visual_turn_running[key] = time.monotonic() - rr._VISUAL_TURN_MAX_S - 1
    new = threading.Thread(target=rr.visual_based_execution, args=('engage', UID, PID), daemon=True)
    new.start()
    for _ in range(100):
        if state['runs'] == 2:
            break
        time.sleep(0.02)
    assert state['runs'] == 2
    state['each'][0].set()
    old.join(10)
    assert key in rr._visual_turn_running, "the old turn cleared the newer turn's mark"
    assert rr._visual_turn_is_running(UID, PID)
    state['each'][1].set()
    new.join(10)
    assert rr._visual_turn_running == {}
