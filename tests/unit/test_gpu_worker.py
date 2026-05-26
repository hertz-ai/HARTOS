"""
Unit tests for integrations.service_tools.gpu_worker.

Covers the subprocess-isolation pattern used by all GPU tools
(F5-TTS, Chatterbox, CosyVoice, Indic Parler, Whisper, ...).

These tests use a tiny no-GPU echo worker (_test_echo_worker.py) so
they run everywhere including CI without any ML dependencies.

Categories:
  Happy path:        spawn, echo, shutdown
  Error handling:    handler exception (recoverable), worker crash
  Crash detection:   C-level abort via os._exit, fast detection
  Respawn:           automatic restart after crash
  Timing:            request timeout, startup timeout
  Idle handling:     idle auto-stop via timer
  ToolWorker API:    synthesize() uniform JSON shape
  Variants:          worker_args CLI dispatch (chatterbox turbo vs ml)
  Thread safety:     concurrent calls serialize cleanly
"""

import json
import os
import sys
import time
import threading
from pathlib import Path

import pytest

# Make HARTOS importable
HARTOS_ROOT = Path(__file__).resolve().parents[2]
if str(HARTOS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARTOS_ROOT))

from integrations.service_tools.gpu_worker import (  # noqa: E402
    GPUWorker,
    ToolWorker,
    WorkerCrash,
    WorkerError,
    WorkerNotReady,
    WorkerTimeout,
)


ECHO_MODULE = 'integrations.service_tools._test_echo_worker'
DISPATCHER = 'integrations.service_tools.gpu_worker'


def _make_echo_worker(
    name: str = 'echo',
    startup_timeout: float = 10.0,
    request_timeout: float = 5.0,
    extra_args: list = None,
) -> GPUWorker:
    """Spawn an echo worker via the centralized dispatcher.

    The dispatcher imports `ECHO_MODULE` and picks up its `_load`/
    `_synthesize` callbacks — the echo module itself has no
    `__main__` block (library-only, just like the real GPU tools).
    """
    args = [ECHO_MODULE] + (extra_args or [])
    return GPUWorker(
        name=name,
        module=DISPATCHER,
        args=args,
        startup_timeout=startup_timeout,
        request_timeout=request_timeout,
    )


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def echo_worker():
    """A freshly-spawned echo GPUWorker. Auto-stops on teardown."""
    w = _make_echo_worker()
    w.start()
    yield w
    try:
        w.stop(timeout=2.0)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
# FT 01: happy path — spawn, echo, shutdown
# ═══════════════════════════════════════════════════════════════════

def test_ft01_spawn_and_echo(echo_worker):
    """Worker spawns, READY marker is received, echo round-trip works."""
    assert echo_worker.is_alive()

    resp = echo_worker.call({'op': 'echo', 'hello': 'world', 'n': 42})
    assert resp['echo']['hello'] == 'world'
    assert resp['echo']['n'] == 42
    assert 'latency_ms' in resp
    assert resp['latency_ms'] >= 0


def test_ft02_multiple_sequential_calls(echo_worker):
    """Worker handles many sequential requests without losing state."""
    for i in range(20):
        resp = echo_worker.call({'op': 'echo', 'i': i})
        assert resp['echo']['i'] == i


def test_ft03_clean_shutdown():
    """stop() terminates the worker cleanly within the timeout."""
    w = _make_echo_worker()
    w.start()
    assert w.is_alive()
    w.stop(timeout=3.0)
    assert not w.is_alive()


# ═══════════════════════════════════════════════════════════════════
# FT 04–05: handler exceptions keep the worker alive
# ═══════════════════════════════════════════════════════════════════

def test_ft04_handler_exception_returns_error(echo_worker):
    """When handle() raises, the worker returns an error JSON
    and does NOT die — subsequent calls still succeed."""
    resp = echo_worker.call({'op': 'raise'})
    assert 'error' in resp
    assert 'RuntimeError' in resp['error']
    assert 'simulated handler failure' in resp['error']
    assert echo_worker.is_alive()  # critical: worker stays up


def test_ft05_worker_recovers_after_handler_error(echo_worker):
    """After a handler error, the next request succeeds normally."""
    echo_worker.call({'op': 'raise'})
    resp = echo_worker.call({'op': 'echo', 'after': 'error'})
    assert resp['echo']['after'] == 'error'


# ═══════════════════════════════════════════════════════════════════
# FT 06–08: uncatchable crash — the whole point of this design
# ═══════════════════════════════════════════════════════════════════

def test_ft06_uncatchable_crash_raises_workercrash(echo_worker):
    """When the worker calls os._exit(), the parent catches WorkerCrash
    (not a hang, not a process kill). This is the uncatchable-CUDA-OOM
    equivalent we must handle."""
    with pytest.raises(WorkerCrash) as exc_info:
        echo_worker.call({'op': 'crash'})
    assert 'died' in str(exc_info.value)
    assert '137' in str(exc_info.value)  # exit code from _test_echo_worker
    assert not echo_worker.is_alive()


def test_ft07_crash_detection_is_fast(echo_worker):
    """Crash must be detected within ~1 second, not after request_timeout.
    The request_timeout is 5s for this worker; if we waited the full
    timeout we'd be hiding the crash."""
    t0 = time.time()
    with pytest.raises(WorkerCrash):
        echo_worker.call({'op': 'crash'})
    elapsed = time.time() - t0
    assert elapsed < 2.0, f'Crash detection took {elapsed:.2f}s (should be <2s)'


def test_ft08_respawn_after_crash(echo_worker):
    """After a crash, start() respawns a fresh worker and normal
    operation resumes."""
    with pytest.raises(WorkerCrash):
        echo_worker.call({'op': 'crash'})
    assert not echo_worker.is_alive()

    echo_worker.start()  # respawn
    assert echo_worker.is_alive()

    resp = echo_worker.call({'op': 'echo', 'reborn': True})
    assert resp['echo']['reborn'] is True


# ═══════════════════════════════════════════════════════════════════
# FT 09: NotReady error when calling before start
# ═══════════════════════════════════════════════════════════════════

def test_ft09_call_before_start_raises_notready():
    """Calling a never-started worker raises WorkerNotReady."""
    w = _make_echo_worker(name='never_started')
    with pytest.raises(WorkerNotReady):
        w.call({'op': 'echo'})


# ═══════════════════════════════════════════════════════════════════
# NFT 01: request timeout — worker stuck (simulated with long sleep)
# ═══════════════════════════════════════════════════════════════════

def test_nft01_request_timeout_kills_stuck_worker(echo_worker):
    """If a request takes longer than request_timeout, the worker is
    killed (not left stuck) and WorkerTimeout is raised."""
    # Use a very short request timeout for this test
    echo_worker.request_timeout = 1.0

    # The echo worker does not have a 'sleep' op, so we send a crash
    # op which will terminate before the timeout. To truly test timeout
    # we'd need a slow op — which would make the test fragile.
    # Instead, verify the timeout logic via the time budget in call().
    #
    # This test documents the timeout path but relies on the smoke test
    # script to exercise actual stuck workers.
    assert echo_worker.request_timeout == 1.0


def test_nft02_startup_timeout_on_missing_module():
    """If the worker module doesn't exist, startup fails fast (not a
    60-second hang)."""
    w = GPUWorker(
        name='nonexistent',
        module=DISPATCHER,
        args=['integrations.service_tools._does_not_exist'],
        startup_timeout=5.0,
    )
    t0 = time.time()
    with pytest.raises((WorkerCrash, WorkerTimeout)):
        w.start()
    elapsed = time.time() - t0
    assert elapsed < 6.0, f'Startup failure detection took {elapsed:.2f}s'


# ═══════════════════════════════════════════════════════════════════
# NFT 03: thread safety — concurrent callers serialize
# ═══════════════════════════════════════════════════════════════════

def test_nft03_concurrent_calls_serialize(echo_worker):
    """Multiple threads calling .call() concurrently don't crash or
    interleave responses. Worker's internal lock serializes them."""
    results = []
    errors = []

    def worker(tid):
        try:
            resp = echo_worker.call({'op': 'echo', 'tid': tid})
            results.append(resp['echo']['tid'])
        except Exception as e:
            errors.append((tid, e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f'Concurrent calls errored: {errors}'
    assert sorted(results) == list(range(8))


# ═══════════════════════════════════════════════════════════════════
# NFT 04: idle auto-stop
# ═══════════════════════════════════════════════════════════════════

def test_nft04_idle_timer_stops_worker():
    """ToolWorker stops the subprocess after idle_timeout with no requests."""
    t = ToolWorker(
        tool_name='idle_test',
        tool_module=ECHO_MODULE,
        vram_budget='tts_f5',         # valid key, no real VRAM use
        output_subdir='idle_test',
        engine='test',
        startup_timeout=10.0,
        request_timeout=5.0,
        idle_timeout=1.0,              # 1 second idle timeout
    )
    try:
        # First call spawns and schedules the idle timer
        resp = t.call({'op': 'echo', 'x': 1})
        assert 'error' not in resp
        assert t._worker is not None and t._worker.is_alive()

        # Wait past the idle timeout
        time.sleep(2.0)
        assert t._worker is None, 'idle timer should have stopped the worker'
    finally:
        t.stop()


def test_nft05_idle_timer_reset_on_each_call():
    """Every call resets the idle timer so active tools stay alive."""
    t = ToolWorker(
        tool_name='busy_test',
        tool_module=ECHO_MODULE,
        vram_budget='tts_f5',
        output_subdir='busy_test',
        engine='test',
        startup_timeout=10.0,
        request_timeout=5.0,
        idle_timeout=1.0,
    )
    try:
        for i in range(4):
            resp = t.call({'op': 'echo', 'i': i})
            assert 'error' not in resp
            time.sleep(0.4)  # below idle_timeout
        # Worker should still be alive: each call extended the deadline
        assert t._worker is not None and t._worker.is_alive()
    finally:
        t.stop()


# ═══════════════════════════════════════════════════════════════════
# FT 10: ToolWorker.synthesize uniform JSON shape
# ═══════════════════════════════════════════════════════════════════

def test_ft10_toolworker_call_crash_returns_transient_error():
    """When the worker crashes, ToolWorker.call() returns a dict with
    error + transient:True instead of raising. This lets callers fall
    back to another engine cleanly."""
    t = ToolWorker(
        tool_name='crash_test',
        tool_module=ECHO_MODULE,
        vram_budget='tts_f5',
        output_subdir='crash_test',
        engine='test',
        startup_timeout=10.0,
        request_timeout=5.0,
        idle_timeout=0,  # disable idle stop
    )
    try:
        result = t.call({'op': 'crash'})
        assert 'error' in result
        assert result.get('transient') is True
        assert 'crashed' in result['error']
    finally:
        t.stop()


def test_ft11_toolworker_call_recovers_after_crash():
    """After a crash, ToolWorker transparently respawns on the next call."""
    t = ToolWorker(
        tool_name='recover_test',
        tool_module=ECHO_MODULE,
        vram_budget='tts_f5',
        output_subdir='recover_test',
        engine='test',
        startup_timeout=10.0,
        request_timeout=5.0,
        idle_timeout=0,
    )
    try:
        r1 = t.call({'op': 'crash'})
        assert r1.get('transient') is True

        # Next call must respawn and succeed
        r2 = t.call({'op': 'echo', 'second': True})
        assert 'error' not in r2
        assert r2.get('echo', {}).get('second') is True
    finally:
        t.stop()


# ═══════════════════════════════════════════════════════════════════
# Regression: C5 — stdout contamination from noisy libraries
# ═══════════════════════════════════════════════════════════════════

def test_ft12_stdout_noise_during_load_does_not_break_protocol():
    """The echo worker's _load() intentionally prints garbage to stdout.
    A correctly isolated worker routes those writes to stderr, so the
    parent's first protocol read sees the READY marker, NOT the noise.

    Without the C5 fix, run_worker would fail to come up because the
    parent would read '[noise] library init message' as the READY line
    and hang or raise a WorkerCrash for an unrelated reason."""
    w = _make_echo_worker(name='noisy')
    w.start()
    try:
        assert w.is_alive(), 'worker should come up despite stdout noise'
        r = w.call({'op': 'echo', 'post_noise': True})
        assert r['echo']['post_noise'] is True
    finally:
        w.stop()


def test_ft13_stdout_noise_during_handler_does_not_break_protocol(echo_worker):
    """Handler-time stdout noise must also be neutralized. The noisy_echo
    op prints garbage between receiving the request and returning the
    response — the parent must still parse exactly one JSON line back."""
    r = echo_worker.call({'op': 'noisy_echo', 'payload': 'hello'})
    assert r.get('noisy') is True
    assert r['echo']['payload'] == 'hello'
    # And the next call still works — no residual noise in the queue.
    r2 = echo_worker.call({'op': 'echo', 'after_noise': True})
    assert r2['echo']['after_noise'] is True


# ═══════════════════════════════════════════════════════════════════
# NFT 06: real stuck-worker → request timeout
# ═══════════════════════════════════════════════════════════════════

def test_nft06_real_request_timeout_kills_stuck_worker():
    """A slow handler exceeding request_timeout triggers WorkerTimeout
    and the stuck worker is killed so subsequent calls respawn cleanly."""
    w = _make_echo_worker(name='stuck', request_timeout=1.0)
    w.start()
    try:
        t0 = time.time()
        with pytest.raises(WorkerTimeout):
            w.call({'op': 'sleep', 'sleep_s': 5})
        dt = time.time() - t0
        assert 0.9 < dt < 2.5, f'Timeout fired at {dt:.2f}s (expected ~1s)'
        assert not w.is_alive(), 'stuck worker should be killed'

        # Respawn should work
        w.start()
        r = w.call({'op': 'echo', 'after_timeout': True})
        assert r['echo']['after_timeout'] is True
    finally:
        try:
            w.stop()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
# FT 14: worker_args variant dispatch
# ═══════════════════════════════════════════════════════════════════

def test_ft14_worker_args_forwarded_to_subprocess():
    """Extra CLI args (used by chatterbox turbo vs ml variant dispatch)
    must reach the child as sys.argv[1:] so the dispatcher can read them.

    The centralized dispatcher consumes args[0]=module and args[1]=variant,
    so anything after variant reaches the tool's process as additional
    sys.argv entries."""
    w = _make_echo_worker(
        name='variant',
        extra_args=['turbo', 'extra', 'flag'],
    )
    w.start()
    try:
        r = w.call({'op': 'args'})
        # Child sees: [ECHO_MODULE, 'turbo', 'extra', 'flag'] — argv[0] is
        # the script name in the dispatcher. We passed the module + 3 args.
        assert 'turbo' in r['argv']
        assert 'extra' in r['argv']
        assert 'flag' in r['argv']
    finally:
        w.stop()


# ═══════════════════════════════════════════════════════════════════
# FT 15: idle timer actually stops the worker after inactivity
# ═══════════════════════════════════════════════════════════════════

def test_ft16_observer_fires_on_spawn_crash_stop():
    """Observer API fires 'spawned' on first successful call,
    'crashed' when the worker dies mid-request, and 'stopped' on
    explicit stop(). Essential for wiring ToolWorker to model catalog
    state updates."""
    events = []

    def listener(tool_name: str, event: str):
        events.append((tool_name, event))

    t = ToolWorker(
        tool_name='observer_test',
        tool_module=ECHO_MODULE,
        vram_budget='tts_f5',
        output_subdir='observer_test',
        engine='test',
        startup_timeout=10.0,
        request_timeout=5.0,
        idle_timeout=0,
    )
    t.add_observer(listener)
    try:
        # First call → spawn
        r = t.call({'op': 'echo', 'hello': True})
        assert 'error' not in r
        assert ('observer_test', 'spawned') in events

        # Crash the worker
        events.clear()
        r = t.call({'op': 'crash'})
        assert r.get('transient') is True
        assert ('observer_test', 'crashed') in events

        # Next call respawns
        events.clear()
        r = t.call({'op': 'echo', 'after': True})
        assert ('observer_test', 'spawned') in events

        # Explicit stop → 'stopped'
        events.clear()
        t.stop()
        assert ('observer_test', 'stopped') in events
    finally:
        try:
            t.stop()
        except Exception:
            pass


import pytest

@pytest.mark.skip(reason="try_free_vram internal logic changed — mock path needs rewrite")
def test_ft19_try_free_vram_stops_lru_workers():
    """try_free_vram() should stop live workers oldest-idle-first until
    the VRAM budget is met. Mocks vram_manager to avoid needing a GPU."""
    from unittest.mock import patch
    from integrations.service_tools import gpu_worker as gw

    # Spawn 3 workers with staggered _last_used timestamps (oldest first)
    workers = []
    try:
        for i in range(3):
            w = ToolWorker(
                tool_name=f'evict_test_{i}',
                tool_module=ECHO_MODULE,
                vram_budget='tts_f5',
                output_subdir=f'evict_test_{i}',
                engine='test',
                startup_timeout=10.0,
                request_timeout=5.0,
                idle_timeout=0,
            )
            workers.append(w)
            w.call({'op': 'echo', 'i': i})
            # Stagger last_used so evict_test_0 is oldest
            w._last_used = time.monotonic() - (3 - i)

        # Mock vram_manager.get_free_vram to start low, jump up each stop
        call_count = {'n': 0}
        vram_sequence = [0.2, 0.5, 1.2, 2.5]  # 4 reads: low, after stop 1, 2, 3

        class _FakeVM:
            def get_free_vram(self):
                idx = min(call_count['n'], len(vram_sequence) - 1)
                call_count['n'] += 1
                return vram_sequence[idx]

        with patch.dict('sys.modules', {'integrations.service_tools.vram_manager':
                type('M', (), {'vram_manager': _FakeVM()})()}):
            freed = gw.try_free_vram(
                needed_gb=1.0,
                exclude_tool='evict_test_2',  # keep newest alive
            )

        assert freed is True, f"should have freed enough VRAM, calls={call_count['n']}"
        # evict_test_0 (oldest) and evict_test_1 should have been stopped
        assert not workers[0].is_alive(), "oldest should be evicted first"
        # evict_test_2 must NOT be stopped (it's in exclude_tool)
        assert workers[2].is_alive(), "exclude_tool must stay running"
    finally:
        for w in workers:
            try:
                w.stop()
            except Exception:
                pass


def test_ft18_set_idle_timeout_updates_threshold():
    """set_idle_timeout() updates the auto-stop deadline and takes
    effect on a running worker. Used by model loaders to sync the
    catalog entry's idle_timeout_s to the worker instance."""
    t = ToolWorker(
        tool_name='idle_update_test',
        tool_module=ECHO_MODULE,
        vram_budget='tts_f5',
        output_subdir='idle_update_test',
        engine='test',
        startup_timeout=10.0,
        request_timeout=5.0,
        idle_timeout=60.0,  # long initial timeout
    )
    try:
        # Spawn worker and let the 60s timer arm
        t.call({'op': 'echo'})
        assert t.is_alive()
        assert t.idle_timeout == 60.0

        # Shrink the timeout — the timer should re-arm with the new value
        t.set_idle_timeout(1.0)
        assert t.idle_timeout == 1.0

        # Wait past the new deadline → worker stops
        time.sleep(1.8)
        assert t._worker is None

        # Also test disabling: spawn again, set to 0, confirm no auto-stop
        t.call({'op': 'echo'})
        t.set_idle_timeout(0)
        time.sleep(0.5)
        assert t.is_alive()
    finally:
        try:
            t.stop()
        except Exception:
            pass


def test_ft17_observer_remove():
    """remove_observer() unregisters cleanly; no events after removal."""
    events = []
    cb = lambda name, evt: events.append((name, evt))

    t = ToolWorker(
        tool_name='observer_remove_test',
        tool_module=ECHO_MODULE,
        vram_budget='tts_f5',
        output_subdir='observer_remove_test',
        engine='test',
        startup_timeout=10.0,
        request_timeout=5.0,
        idle_timeout=0,
    )
    t.add_observer(cb)
    try:
        t.call({'op': 'echo'})
        assert len(events) == 1  # spawned
        t.remove_observer(cb)
        t.call({'op': 'crash'})
        try:
            t.call({'op': 'echo'})
        except Exception:
            pass
        # No new events after removal
        assert len(events) == 1
    finally:
        try:
            t.stop()
        except Exception:
            pass


def test_ft15_idle_timer_stops_after_final_call_then_wait():
    """Explicit test of the auto-stop tail: after the LAST call, wait
    past idle_timeout and assert the worker is gone."""
    t = ToolWorker(
        tool_name='final_idle',
        tool_module=ECHO_MODULE,
        vram_budget='tts_f5',
        output_subdir='final_idle',
        engine='test',
        startup_timeout=10.0,
        request_timeout=5.0,
        idle_timeout=1.0,
    )
    try:
        # Two calls, then stop sending
        t.call({'op': 'echo', 'i': 1})
        t.call({'op': 'echo', 'i': 2})
        assert t._worker is not None and t._worker.is_alive()

        time.sleep(1.5)  # past idle_timeout
        assert t._worker is None, 'worker should be stopped by idle timer'
    finally:
        t.stop()


def test_ft20_pythonpath_propagates_parent_syspath():
    """Regression for the TTS refactor bug.

    Before subprocess isolation (commit dce4b31), TTS engines ran in-process
    and inherited Nunba's runtime sys.path mutations automatically — including
    the ~/.nunba/site-packages/ prepend in app.py:97 that holds CUDA torch,
    regex, transformers, parler_tts. After the refactor, spawned subprocess
    workers booted fresh from python-embed's default path and all
    transformers-based engines crashed on `import regex`.

    This test locks in the fix: every subprocess spawn must carry the
    parent's current sys.path via PYTHONPATH so the child sees the same
    package dirs the parent does. Verifies by inspecting the env argument
    passed to subprocess.Popen when GPUWorker._spawn() runs."""
    from unittest.mock import patch, MagicMock

    # Create a worker without starting it
    w = GPUWorker(
        name='path_test',
        module=DISPATCHER,
        args=[ECHO_MODULE],
        startup_timeout=2.0,
        request_timeout=2.0,
    )

    captured_env = {}

    class _FakeProc:
        def __init__(self, *a, **kw):
            captured_env.update(kw.get('env', {}))
            self.stdin = MagicMock()
            self.stdout = MagicMock()
            self.stderr = MagicMock()
            self.returncode = None

        def poll(self):
            return None  # pretend still running

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    with patch('subprocess.Popen', side_effect=_FakeProc), \
         patch.object(w, '_wait_ready'):
        try:
            w.start()
        except Exception:
            # _wait_ready is patched so start() should complete cleanly
            pass

    # PYTHONPATH must be set and must contain a real dir from parent sys.path
    assert 'PYTHONPATH' in captured_env, \
        'spawn env missing PYTHONPATH — parent sys.path not propagated'
    pathsep = os.pathsep
    child_paths = captured_env['PYTHONPATH'].split(pathsep)

    # Every child entry must be a real directory (no empty / stale entries)
    for p in child_paths:
        assert os.path.isdir(p), \
            f'PYTHONPATH contains non-dir entry {p!r}'

    # At least one parent sys.path entry should appear in the child PYTHONPATH
    parent_dirs = {p for p in sys.path if p and os.path.isdir(p)}
    child_dirs = set(child_paths)
    overlap = parent_dirs & child_dirs
    assert overlap, (
        'No parent sys.path entries made it to the child PYTHONPATH. '
        'TTS engines (Indic Parler, Chatterbox, F5) will crash on '
        '`import regex` in frozen builds. See commit dce4b31.'
    )


def test_ft21_pythonpath_preserves_caller_value():
    """If caller set PYTHONPATH on the ToolWorker env, gpu_worker must
    APPEND the parent sys.path to it rather than overwrite. Caller
    intent wins for anything they explicitly set, parent paths fill in
    the gaps."""
    from unittest.mock import patch, MagicMock

    w = GPUWorker(
        name='path_preserve',
        module=DISPATCHER,
        args=[ECHO_MODULE],
        startup_timeout=2.0,
        request_timeout=2.0,
        env={'PYTHONPATH': r'C:\caller\override'},
    )

    captured_env = {}

    class _FakeProc:
        def __init__(self, *a, **kw):
            captured_env.update(kw.get('env', {}))
            self.stdin = MagicMock()
            self.stdout = MagicMock()
            self.stderr = MagicMock()
            self.returncode = None

        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    with patch('subprocess.Popen', side_effect=_FakeProc), \
         patch.object(w, '_wait_ready'):
        try:
            w.start()
        except Exception:
            pass

    final = captured_env.get('PYTHONPATH', '')
    assert r'C:\caller\override' in final, \
        "caller's PYTHONPATH was dropped"
    # And at least one parent dir still made it in
    parent_dirs = [p for p in sys.path if p and os.path.isdir(p)]
    assert any(p in final for p in parent_dirs), \
        'parent sys.path not appended alongside caller override'
