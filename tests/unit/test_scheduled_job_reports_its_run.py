"""A scheduled REUSE job's status is the status of the run it started.

MEASURED (Nunba log sweep, defect 24, lines from 2026-09-24..26; they have
since rotated off this box, so the numbers below are the sweep's, quoted):

  * create_scheduled_jobs registered cron '* * * * *' for "Monitor financial
    health data and trigger paper trading funding ..." (user c23d388c, prompt
    23841403018; the registration line is still in agent_system.log.1 at
    2026-09-24 18:26:46).  Every minute APScheduler ran execute_python_file,
    which self-POSTs /time_agent on the local backend.
  * The POST passed no timeout, so it inherited core.http_pool.DEFAULT_TIMEOUT
    = (3, 15).  The route runs a whole time-agent group chat (several local
    completions) before it answers.  21 of 186 runs logged `Job
    "execute_python_file ..." raised an exception` (ReadTimeout) while the
    server went on to finish the chat: completions at 13:31:03, :13 and :20
    after the client gave up at 13:31:15,344.
  * The one run that really crashed -- `14:27:05,431 hart_intelligence_entry
    ERROR Exception on /time_agent [POST]`, openai.APIConnectionError -- was
    logged 33 ms later as `Job "execute_python_file ..." executed
    successfully`: the job returned 'done' without reading the response.

So the job status was inverted both ways.  These tests run the REAL
hartos.reuse_recipe.execute_python_file inside a REAL APScheduler
BackgroundScheduler against a REAL HTTP server serving the REAL /time_agent
route, lifted from hart_intelligence_entry.py with ast (no interpreter on the
dev box imports that module; see test_gather_turn_mirrors_user_side.py).  Only
the boundary is stubbed: time_based_execution, the autogen group chat that
needs a model.  HARTOS_ENTRY_SOURCE points the lift at another copy of the
file.

    python -m pytest tests/unit/test_scheduled_job_reports_its_run.py -q
"""
import ast
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

flask = pytest.importorskip('flask')
# hartos.reuse_recipe type-annotates caches with autogen classes at import.
pytest.importorskip('autogen', reason='autogen not installed')

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED  # noqa: E402
from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from core import http_pool  # noqa: E402

_SRC = os.environ.get('HARTOS_ENTRY_SOURCE') or str(
    Path(__file__).resolve().parents[2] / 'hart_intelligence_entry.py')

TASK = 'Monitor financial health data and trigger paper trading funding'
USER_ID = 'c23d388c-07a0-4a79-816d-5b95642683c0'
PROMPT_ID = '23841403018'


def _lift(names):
    """Source of the named top-level defs, decorators included, in file order."""
    src = open(_SRC, encoding='utf-8').read()
    lines = src.splitlines(keepends=True)
    out = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            out.append(''.join(lines[start - 1:node.end_lineno]))
    assert len(out) == len(names), (
        f'lifted {len(out)} of {sorted(names)} from {_SRC} -- re-point this '
        f'test rather than deleting it')
    return '\n\n'.join(out)


def _time_agent_app(run):
    """A Flask app serving the real /time_agent route; `run` stands in for the
    group chat (time_based_execution)."""
    app = flask.Flask(__name__)
    ns = {
        'app': app, 'request': flask.request, 'jsonify': flask.jsonify,
        'time_based_execution': run, 'time_execution': run,
    }
    exec(compile(_lift({'time_agent'}), _SRC, 'exec'), ns)
    return app


class _Server:
    """The route on a real socket, the way the scheduler reaches it."""

    def __init__(self, app):
        self._srv = make_server('127.0.0.1', 0, app, threaded=True)
        self.base_url = f'http://127.0.0.1:{self._srv.server_port}'
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._srv.shutdown()
        self._t.join(timeout=10)


def _run_scheduled_job(base_url, wait_s, task=TASK):
    """Run execute_python_file once as an APScheduler job; return its event."""
    from hartos import reuse_recipe as rr
    sched = BackgroundScheduler()
    fired = threading.Event()
    seen = {}

    def _on_job(event):
        seen['event'] = event
        fired.set()

    sched.add_listener(_on_job, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    sched.start()
    try:
        with patch.object(rr, 'get_local_backend_url', return_value=base_url):
            sched.add_job(rr.execute_python_file, 'date',
                          run_date=datetime.now(), misfire_grace_time=60,
                          args=[task, USER_ID, PROMPT_ID, 0])
            assert fired.wait(wait_s), f'the job never finished in {wait_s}s'
    finally:
        sched.shutdown(wait=True)
    return seen['event']


def _crash(*_a, **_k):
    # What 14:27:05 carried: openai.APIConnectionError's message.
    raise RuntimeError('Connection error.')


def test_a_run_that_crashed_is_a_failed_job():
    """14:27:05 inverted: the route 500'd and the job said 'executed
    successfully'."""
    with _Server(_time_agent_app(_crash)) as srv:
        event = _run_scheduled_job(srv.base_url, wait_s=60)
    assert event.code == EVENT_JOB_ERROR, (
        f'a run that crashed was reported as a successful job '
        f'(retval={getattr(event, "retval", None)!r})')
    assert 'HTTP 500' in str(event.exception), repr(event.exception)


def test_a_run_the_route_refused_is_a_failed_job_that_says_why():
    """A job whose run never started (the route answers 404 for a blank task)
    fails, and the failure carries the route's own error text."""
    ran = []
    with _Server(_time_agent_app(lambda *a: ran.append(a) or 'done')) as srv:
        event = _run_scheduled_job(srv.base_url, wait_s=60, task='')
    assert ran == [], 'the route should have refused before running anything'
    assert event.code == EVENT_JOB_ERROR, (
        f'a refused run was reported as a successful job '
        f'(retval={getattr(event, "retval", None)!r})')
    assert 'HTTP 404' in str(event.exception), repr(event.exception)
    assert 'task_description' in str(event.exception), repr(event.exception)


def test_a_run_that_finished_is_a_successful_job():
    """Control: the ordinary outcome stays a success."""
    calls = []

    def _ok(task, uid, pid, entry):
        calls.append((task, uid, pid, entry))
        return 'done'

    with _Server(_time_agent_app(_ok)) as srv:
        event = _run_scheduled_job(srv.base_url, wait_s=60)
    assert event.code == EVENT_JOB_EXECUTED, repr(event.exception)
    assert event.retval == 'done'
    # The route really ran the job's task, with the ids the job carried.
    assert calls == [(TASK, USER_ID, int(PROMPT_ID), 0)]


def test_a_run_longer_than_the_default_read_timeout_still_reports_its_outcome():
    """13:31 inverted: the run went on and finished, the job gave up at the
    15 s DEFAULT_TIMEOUT read and was logged as failed.  A run that outlasts
    that read and then succeeds is a successful job."""
    slow_s = http_pool.DEFAULT_TIMEOUT[1] + 1.5

    def _slow(*_a, **_k):
        time.sleep(slow_s)
        return 'done'

    with _Server(_time_agent_app(_slow)) as srv:
        event = _run_scheduled_job(srv.base_url, wait_s=slow_s + 60)
    assert event.code == EVENT_JOB_EXECUTED, (
        f'a run that finished after {slow_s}s was reported as a failed job: '
        f'{event.exception!r}')
