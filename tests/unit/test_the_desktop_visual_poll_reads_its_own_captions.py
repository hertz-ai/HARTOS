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
    import logging
    import requests
    rr, got, state = poll
    state['rows'] = _rows(3, minutes_ago=1)
    state['post_raises'] = requests.exceptions.ReadTimeout('read timed out')
    with caplog.at_level(logging.INFO, logger='hartos.reuse_recipe'):
        assert rr.call_visual_task('engage', UID, PID) == 'done'
    assert len(got['posts']) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


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
