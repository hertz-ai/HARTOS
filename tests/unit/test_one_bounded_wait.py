"""ONE bounded wait for a blocking in-process call: core.subprocess_safe.call_bounded.

Review F7 (2026-09-27): call_bounded's docstring said "THE ONE bounded wait"
while four more copies of the shape lived elsewhere, and two of them did not
bound anything.  `with ThreadPoolExecutor(max_workers=1) as pool:
pool.submit(f).result(timeout=T)` raises at T, but leaving the `with` calls
shutdown(wait=True), which joins the stuck worker: probe vlm_pool.py measured
3.02 s on a 0.5 s timeout.  web_crawler._run_async and
hart_intelligence_entry._handle_agentic_router_tool were that shape.
dashboard_service and system_requirements avoided the join by hand
(shutdown(wait=False)), but an executor's worker is not a daemon thread, so
a wedged one still holds the interpreter open at exit.

This file has two halves:
  * behaviour: each former copy now returns on time with a call that never
    does;
  * the guard: no function outside core/subprocess_safe.py may build either
    shape again (thread + Event.wait, or a one-worker executor +
    result(timeout)).  The detector is shown to FIRE on each shape first,
    so a guard that matches nothing cannot pass by accident.
"""
import ast
import asyncio
import os
import threading
import time
from unittest.mock import patch

import pytest

#: Allowed overrun past a bound, on a box other sessions load heavily
#: (measured 2026-09-27: a 0.5 s bound took 2.9 s to release under load).
#: Every blocker below lasts 10 s, so this still tells bounded from not.
SLACK_S = 4.0

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── the detector ──────────────────────────────────────────────────────────

_SKIP_DIRS = {'venv', 'venv311', '.venv', 'node_modules', 'python-embed',
              '__pycache__', 'build', 'dist', 'site-packages', 'tests',
              'agent-ledger-opensource'}

#: The one home of the shape, and the function in it.
_CANONICAL = ('core/subprocess_safe.py', 'call_bounded')


def _name(call):
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ''


def _own_calls(fn):
    """Calls in fn's own body; nested defs are judged on their own."""
    out, stack = [], list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _has_timeout(call):
    return bool(call.args) or any(k.arg == 'timeout' for k in call.keywords)


def _is_one_worker_pool(call):
    if _name(call) != 'ThreadPoolExecutor':
        return False
    for k in call.keywords:
        if k.arg == 'max_workers':
            return isinstance(k.value, ast.Constant) and k.value.value == 1
    return bool(call.args) and isinstance(call.args[0], ast.Constant) \
        and call.args[0].value == 1


def bounded_wait_shapes(source):
    """[(function name, shape)] for every hand-rolled bounded wait in source."""
    found = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = _own_calls(fn)
        names = {_name(c) for c in calls}
        if {'Thread', 'Event'} <= names and any(
                _name(c) == 'wait' and _has_timeout(c) for c in calls):
            found.append((fn.name, 'thread + Event.wait(timeout)'))
        if any(_is_one_worker_pool(c) for c in calls) and any(
                _name(c) == 'result' and _has_timeout(c) for c in calls):
            found.append((fn.name, 'one-worker executor + result(timeout)'))
    return found


def _repo_sources():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.startswith('.')]
        for fname in filenames:
            if fname.endswith('.py'):
                path = os.path.join(dirpath, fname)
                yield os.path.relpath(path, ROOT).replace(os.sep, '/'), path


# ── the detector fires on each shape (so the guard below can fail) ────────

_THREAD_EVENT = '''
import threading
def bounded(fn, wait):
    done = threading.Event()
    def _w():
        fn(); done.set()
    threading.Thread(target=_w, daemon=True).start()
    return done.wait(wait)
'''

_POOL_WITH = '''
import concurrent.futures
def bounded(coro):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(coro).result(timeout=120)
'''

_POOL_MANUAL = '''
import concurrent.futures as _cf
def bounded():
    _ex = _cf.ThreadPoolExecutor(1)
    return _ex.submit(lambda: 1).result(timeout=5)
'''

_NOT_A_BOUNDED_WAIT = '''
import threading, concurrent.futures
def shutdown(t, stop):
    stop.set()
    t.join(timeout=5)
def fan_out(items):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        return [f.result(timeout=5) for f in [pool.submit(i) for i in items]]
'''


@pytest.mark.parametrize('src, shape', [
    (_THREAD_EVENT, 'thread + Event.wait(timeout)'),
    (_POOL_WITH, 'one-worker executor + result(timeout)'),
    (_POOL_MANUAL, 'one-worker executor + result(timeout)'),
])
def test_the_detector_fires_on_each_shape(src, shape):
    assert bounded_wait_shapes(src) == [('bounded', shape)]


def test_the_detector_leaves_other_thread_code_alone():
    assert bounded_wait_shapes(_NOT_A_BOUNDED_WAIT) == []


def test_the_canonical_helper_is_found_where_it_lives():
    path = os.path.join(ROOT, *_CANONICAL[0].split('/'))
    assert (_CANONICAL[1], 'thread + Event.wait(timeout)') in bounded_wait_shapes(
        open(path, encoding='utf-8').read())


def test_source_guard_no_second_bounded_wait():
    """Use core.subprocess_safe.call_bounded; do not hand-roll another."""
    strays = []
    for rel, path in _repo_sources():
        try:
            source = open(path, encoding='utf-8', errors='replace').read()
            shapes = bounded_wait_shapes(source)
        except SyntaxError:
            continue
        for fn, shape in shapes:
            if (rel, fn) != _CANONICAL:
                strays.append(f'{rel}::{fn} [{shape}]')
    assert strays == [], (
        'hand-rolled bounded wait(s); use core.subprocess_safe.call_bounded:\n  '
        + '\n  '.join(strays))


# ── behaviour: each former copy returns on time ───────────────────────────

def test_web_crawler_async_bridge_is_bounded_inside_a_running_loop():
    from integrations import web_crawler

    async def _never():
        await asyncio.sleep(10)

    async def _caller():
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            web_crawler._run_async(_never(), timeout=0.5)
        return time.monotonic() - t0

    assert asyncio.run(_caller()) < 0.5 + SLACK_S


def test_web_crawler_async_bridge_returns_the_result():
    from integrations import web_crawler

    async def _value():
        return 42

    async def _caller():
        return web_crawler._run_async(_value())

    assert asyncio.run(_caller()) == 42


def test_web_crawler_async_bridge_passes_an_error_through():
    from integrations import web_crawler

    async def _boom():
        raise ValueError('page gone')

    async def _caller():
        with pytest.raises(ValueError, match='page gone'):
            web_crawler._run_async(_boom())

    asyncio.run(_caller())


def test_agentic_plan_is_bounded():
    from integrations import agentic_router
    release = threading.Event()
    with patch.object(agentic_router, 'build_agentic_plan',
                      side_effect=lambda *a, **k: release.wait(10)):
        t0 = time.monotonic()
        plan = agentic_router.build_agentic_plan_bounded('x', None, timeout_s=0.3)
        elapsed = time.monotonic() - t0
    release.set()
    assert plan is None
    assert elapsed < 0.3 + SLACK_S


def test_agentic_plan_bounded_returns_the_plan_and_raises_its_error():
    from integrations import agentic_router
    with patch.object(agentic_router, 'build_agentic_plan', return_value={'steps': []}):
        assert agentic_router.build_agentic_plan_bounded('x', None) == {'steps': []}
    with patch.object(agentic_router, 'build_agentic_plan',
                      side_effect=RuntimeError('llm down')):
        with pytest.raises(RuntimeError, match='llm down'):
            agentic_router.build_agentic_plan_bounded('x', None)


def test_dashboard_world_model_is_bounded():
    from integrations.social.dashboard_service import DashboardService
    release = threading.Event()
    with patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
               side_effect=lambda: release.wait(10)):
        t0 = time.monotonic()
        status = DashboardService._world_model_status(timeout_s=0.3)
        elapsed = time.monotonic() - t0
    release.set()
    assert status == {'healthy': False, 'error': 'cold_or_unreachable'}
    assert elapsed < 0.3 + SLACK_S


def test_dashboard_world_model_reports_a_live_bridge():
    from integrations.social.dashboard_service import DashboardService

    class _Bridge:
        def check_health(self):
            return {'healthy': True}

        def get_learning_stats(self):
            return {'learning': {'n': 1}, 'hivemind': {}, 'bridge': {}}

    with patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
               return_value=_Bridge()):
        status = DashboardService._world_model_status()
    assert status['healthy'] is True
    assert status['learning_stats'] == {'n': 1}


def test_cpu_model_probe_is_bounded():
    from security import system_requirements
    release = threading.Event()
    with patch.object(system_requirements.platform, 'processor',
                      side_effect=lambda: release.wait(10)):
        t0 = time.monotonic()
        model = system_requirements._detect_cpu_model(timeout_s=0.3)
        elapsed = time.monotonic() - t0
    release.set()
    assert model == ''
    assert elapsed < 0.3 + SLACK_S


def test_cpu_model_probe_returns_the_model():
    from security import system_requirements
    with patch.object(system_requirements.platform, 'processor',
                      return_value='Intel64 Family 6'):
        assert system_requirements._detect_cpu_model() == 'Intel64 Family 6'


def test_the_agentic_plan_runs_as_the_request_that_asked():
    """Review of 924b8e9dc (minor): the plan's worker made LLM calls with an
    empty thread-local -- no user, no prompt, no request id -- and kept
    doing so after the turn stopped waiting.  It carries the caller's."""
    from integrations import agentic_router
    from hartos.threadlocal import thread_local_data as tld
    seen = {}

    def _plan(*_a, **_k):
        seen['user'] = tld.get_user_id()
        seen['prompt'] = tld.get_prompt_id()
        seen['request'] = tld.get_request_id()
        return {'steps': []}

    tld.set_user_id('u-plan')
    tld.set_prompt_id('p-plan')
    tld.set_request_id('r-plan')
    try:
        with patch.object(agentic_router, 'build_agentic_plan', side_effect=_plan):
            agentic_router.build_agentic_plan_bounded('x', None)
    finally:
        tld.set_user_id(None)
        tld.set_prompt_id(None)
        tld.set_request_id(None)
    assert seen == {'user': 'u-plan', 'prompt': 'p-plan', 'request': 'r-plan'}
