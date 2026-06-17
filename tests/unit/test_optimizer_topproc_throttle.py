"""#151: ComputeOptimizer's per-process walk must not GIL-starve the server.

The top-process scan (``psutil.process_iter`` + ``memory_percent`` per PID) is
expensive and GIL-bound on Windows.  It is gated to fire only under a
CPU/RAM/swap/disk breach — i.e. exactly when the box is already loaded — and
WITHOUT throttling it re-walked every MONITOR_INTERVAL, holding the GIL and
hanging the HTTP/SSE event loop (py-spy caught ``compute_optimizer_monitor``
active+gil while every dynamic route timed out — live PID 25248, 2026-06-17;
that's why the desktop UI couldn't mount "Taking longer than expected").

These tests drive the real ``_collect_top_processes`` with psutil mocked at the
boundary and assert it stays responsive in ANY state: a breach walks once; a
SECOND breach within the cooldown does NOT re-walk (reuses the prior result);
no breach never walks; the cooldown elapsing re-walks; and the walk yields the
GIL (``time.sleep(0)``) periodically.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import patch  # noqa: E402

import core.compute_optimizer as co  # noqa: E402


def _fake_psutil(counter, n=130):
    """Fake psutil whose process_iter counts how many full walks happened."""
    class _P:
        def __init__(self, pid):
            self.info = {'pid': pid, 'name': 'p%d' % pid,
                         'cpu_percent': 5.0, 'memory_percent': 1.0}

    def process_iter(attrs=None):
        counter['walks'] += 1
        return [_P(i) for i in range(n)]

    return types.SimpleNamespace(process_iter=process_iter)


def _snap(cpu=99.0):
    s = co.SystemSnapshot(timestamp=0.0, platform_name='Windows')
    s.cpu_percent = cpu  # > CPU_HIGH_THRESHOLD(80) => breach
    return s


def test_no_breach_never_walks():
    """Below every threshold: the expensive walk must not run at all."""
    counter = {'walks': 0}
    mgr = co.ComputeOptimizer()
    with patch.object(co, '_try_import_psutil', return_value=_fake_psutil(counter)):
        mgr._collect_top_processes(_snap(cpu=10.0))
    assert counter['walks'] == 0


def test_breach_walks_once_and_populates():
    counter = {'walks': 0}
    mgr = co.ComputeOptimizer()
    s = _snap(cpu=99.0)
    with patch.object(co, '_try_import_psutil', return_value=_fake_psutil(counter)):
        mgr._collect_top_processes(s)
    assert counter['walks'] == 1
    assert s.top_processes and s.top_processes[0]['cpu_percent'] == 5.0


def test_sustained_breach_reuses_within_cooldown():
    """The core fix: a sustained breach must NOT re-walk every tick."""
    counter = {'walks': 0}
    mgr = co.ComputeOptimizer()
    with patch.object(co, '_try_import_psutil', return_value=_fake_psutil(counter)):
        s1 = _snap()
        mgr._collect_top_processes(s1)
        mgr._last_snapshot = s1                      # simulate _store_snapshot
        s2 = _snap()
        mgr._collect_top_processes(s2)               # within cooldown
    assert counter['walks'] == 1                     # NOT re-walked
    assert s2.top_processes == s1.top_processes      # reused prior result


def test_cooldown_elapsed_walks_again():
    counter = {'walks': 0}
    mgr = co.ComputeOptimizer()
    with patch.object(co, '_try_import_psutil', return_value=_fake_psutil(counter)):
        s1 = _snap()
        mgr._collect_top_processes(s1)
        mgr._last_snapshot = s1
        mgr._last_topproc_ts -= (co.TOPPROC_SCAN_COOLDOWN + 1)  # cooldown elapsed
        s2 = _snap()
        mgr._collect_top_processes(s2)
    assert counter['walks'] == 2


def test_walk_yields_the_gil():
    """A single walk must release the GIL periodically so the server runs."""
    counter = {'walks': 0}
    mgr = co.ComputeOptimizer()
    with patch.object(co, '_try_import_psutil', return_value=_fake_psutil(counter, n=130)), \
            patch.object(co.time, 'sleep') as msleep:
        mgr._collect_top_processes(_snap())
    # 130 PIDs, yield every TOPPROC_YIELD_EVERY(50) -> several sleep(0) calls
    assert msleep.call_count >= 2
    assert all(c.args == (0,) for c in msleep.call_args_list)


# ─── iter_processes: the canonical GIL-safe walker (shared with the shell
#     task-manager routes) ───

def test_iter_processes_walks_all_and_yields_gil():
    counter = {'walks': 0}
    with patch.object(co, '_try_import_psutil', return_value=_fake_psutil(counter, n=130)), \
            patch.object(co.time, 'sleep') as msleep:
        procs = list(co.iter_processes(['pid', 'name'], yield_every=50))
    assert counter['walks'] == 1
    assert len(procs) == 130
    assert msleep.call_count >= 2                       # GIL released mid-walk
    assert all(c.args == (0,) for c in msleep.call_args_list)


def test_iter_processes_empty_when_psutil_missing():
    """No psutil -> yields nothing (never raises) so callers degrade cleanly."""
    with patch.object(co, '_try_import_psutil', return_value=None):
        assert list(co.iter_processes(['pid'])) == []
