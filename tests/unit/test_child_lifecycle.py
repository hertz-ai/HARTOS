"""
core.child_lifecycle: children that die with this process.

Nunba quits from the tray with os._exit(0).  On Windows that left every
llama-server and GPU worker running (2026-10-01: a 2.2 GB llama-server at
full CPU after quit, reused by the next launch).  These tests reproduce the
quit with a real parent process that spawns a child and calls os._exit(0).
"""
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

HARTOS_ROOT = Path(__file__).resolve().parents[2]
if str(HARTOS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARTOS_ROOT))

from core import child_lifecycle  # noqa: E402

psutil = pytest.importorskip('psutil')

windows_only = pytest.mark.skipif(sys.platform != 'win32', reason='Job Objects are Windows-only')


# Puts the parent in a job like core/resource_governor.py's: no
# BREAKAWAY_OK, no kill-on-close.  Nunba runs inside that job; its children
# inherit it and must still bind (the lifecycle job nests under it).
_GOVERNOR_LIKE_JOB = textwrap.dedent("""
    import ctypes
    from core.child_lifecycle import JobHandle
    _gov = JobHandle(owner='test-governor')
    _gov._kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _k = _gov._kernel32
    _k.CreateJobObjectW.restype = ctypes.c_void_p
    _k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _k.GetCurrentProcess.restype = ctypes.c_void_p
    _gov.handle = _k.CreateJobObjectW(None, None)
    assert _k.AssignProcessToJobObject(_gov.handle, _k.GetCurrentProcess())
""")


def _parent_script(bind: bool, in_governor_job: bool = False) -> str:
    governor = _GOVERNOR_LIKE_JOB if in_governor_job else ''
    return textwrap.dedent(f"""
        import os, subprocess, sys
        sys.path.insert(0, {str(HARTOS_ROOT)!r})
        from core.child_lifecycle import bind_to_parent
""") + governor + textwrap.dedent(f"""
        # No inherited pipes: an orphan holding our stdout would block run().
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        bound = bind_to_parent(child) if {bind!r} else False
        print(child.pid, bound, flush=True)
        os._exit(0)   # the tray quit: no atexit, no cleanup
    """)


def _run_parent(bind: bool, in_governor_job: bool = False):
    out = subprocess.run([sys.executable, '-c', _parent_script(bind, in_governor_job)],
                         capture_output=True, text=True, timeout=60)
    pid, bound = out.stdout.split()
    return int(pid), bound == 'True'


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.2)
    return False


@windows_only
def test_bound_child_dies_when_the_parent_os_exits():
    pid, bound = _run_parent(bind=True)
    assert bound
    assert _gone_within(pid, 10), 'child outlived its parent'


@windows_only
def test_bound_child_dies_even_when_the_parent_is_in_the_governor_job():
    # The child inherits the governor's job (no breakaway); the lifecycle job
    # nests under it, so the governor keeps counting the child's memory.
    pid, bound = _run_parent(bind=True, in_governor_job=True)
    assert bound
    assert _gone_within(pid, 10), 'child outlived its parent'


@windows_only
def test_control_an_unbound_child_outlives_the_parent():
    # Proves the test above can see an orphan: without binding the child stays.
    pid, bound = _run_parent(bind=False)
    try:
        assert not bound
        assert not _gone_within(pid, 2)
    finally:
        try:
            psutil.Process(pid).kill()
        except psutil.NoSuchProcess:
            pass


def test_off_windows_or_without_a_handle_binding_is_a_no_op(monkeypatch):
    class _NoHandle:
        _handle = None
    assert child_lifecycle.bind_to_parent(_NoHandle()) is False
    monkeypatch.setattr(child_lifecycle.sys, 'platform', 'linux')
    assert child_lifecycle.bind_to_parent(object()) is False
