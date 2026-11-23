"""
core.child_lifecycle: children that die with this process, through ONE job.

Nunba quits from the tray with os._exit(0).  On Windows that left every
llama-server and GPU worker running (2026-10-01: a 2.2 GB llama-server at
full CPU after quit, reused by the next launch).  These tests reproduce the
quit with a real parent process that spawns children and calls os._exit(0).

The process has exactly one Job Object: the resource governor's
(core/resource_governor.ResourceEnforcer).  bind_to_parent assigns a child to
it, creating it first if the governor has not started yet.  Review of #141: a
second job beside the governor's made the outcome depend on start order
(Nunba starts the llama-server thread before the governor).  So both orders
are tested, and so is "no second job".
"""
import ast
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

_CHILD = ("subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],"
          " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
          " stderr=subprocess.DEVNULL)")   # no inherited pipes: an orphan would block run()

_PRELUDE = textwrap.dedent(f"""
    import os, subprocess, sys
    sys.path.insert(0, {str(HARTOS_ROOT)!r})
    from core.child_lifecycle import bind_to_parent
    from core.resource_governor import get_enforcer
    enforcer = get_enforcer()
""")


def _run(body: str) -> list:
    """Run `body` in a fresh parent process; it prints one line of words and
    leaves with os._exit(0), the tray quit: no atexit, no cleanup."""
    script = _PRELUDE + textwrap.dedent(body) + "\nos._exit(0)\n"
    out = subprocess.run([sys.executable, '-c', script],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip(), out.stderr
    return out.stdout.split()


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


def _kill(pid: int) -> None:
    try:
        psutil.Process(pid).kill()
    except psutil.NoSuchProcess:
        pass


@windows_only
def test_bound_child_dies_when_the_parent_os_exits_governor_not_started():
    """llama-server's thread binds BEFORE the governor starts: bind_to_parent
    creates the one job itself."""
    pid, bound = _run(f"""
        child = {_CHILD}
        print(child.pid, bind_to_parent(child), flush=True)
    """)
    assert bound == 'True'
    assert _gone_within(int(pid), 10), 'child outlived its parent'


@windows_only
def test_bound_child_dies_when_the_governor_job_exists_first():
    """The governor-first order: the parent is already in the job; the child
    starts outside it (silent breakaway) and is then bound."""
    pid, bound = _run(f"""
        enforcer._ensure_job_windows()
        child = {_CHILD}
        print(child.pid, bind_to_parent(child), flush=True)
    """)
    assert bound == 'True'
    assert _gone_within(int(pid), 10), 'child outlived its parent'


@windows_only
def test_a_child_bound_before_the_governor_starts_is_still_killed_and_no_second_job_appears():
    """The child-first order, the one a second job broke: child1 is bound,
    THEN the governor starts and runs its memory step, THEN child2 is bound.  The
    governor reuses the job bind_to_parent created, so both die."""
    pid1, pid2, same_job, bound2 = _run(f"""
        child1 = {_CHILD}
        assert bind_to_parent(child1)
        job_before = enforcer._job_handle
        enforcer._ensure_job_windows()              # what enforce() does
        enforcer._enforce_ram_windows(4 << 30)      # and update_caps()
        child2 = {_CHILD}
        print(child1.pid, child2.pid, job_before == enforcer._job_handle,
              bind_to_parent(child2), flush=True)
    """)
    assert same_job == 'True', 'the governor made a second job'
    assert bound2 == 'True'
    assert _gone_within(int(pid1), 10), 'the child bound first outlived its parent'
    assert _gone_within(int(pid2), 10), 'the child bound after the governor outlived its parent'


@windows_only
def test_the_governors_memory_step_keeps_kill_on_close_and_caps_nothing():
    """_enforce_ram_windows runs on every update_caps().  The job's limit flags
    are replaced on each SetInformationJobObject, so it must restate
    kill-on-close and breakaway; and it must not put a job-wide memory limit on
    the one job, which holds this process and every bound child (review of
    #141, 20 Nov: a bound child got MemoryError under the cap)."""
    flags = _run("""
        import ctypes
        enforcer._ensure_job_windows()
        enforcer._enforce_ram_windows(4 << 30)

        class _Basic(ctypes.Structure):
            _fields_ = [('a', ctypes.c_longlong), ('b', ctypes.c_longlong),
                        ('LimitFlags', ctypes.c_ulong), ('c', ctypes.c_size_t),
                        ('d', ctypes.c_size_t), ('e', ctypes.c_ulong),
                        ('f', ctypes.c_size_t), ('g', ctypes.c_ulong),
                        ('h', ctypes.c_ulong)]

        class _Ext(ctypes.Structure):
            _fields_ = [('Basic', _Basic), ('io', ctypes.c_ulonglong * 6),
                        ('ProcessMemoryLimit', ctypes.c_size_t),
                        ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t),
                        ('PeakJobMemoryUsed', ctypes.c_size_t)]

        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.QueryInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.c_void_p]
        info = _Ext()
        assert k.QueryInformationJobObject(
            enforcer._job_handle, 9, ctypes.byref(info), ctypes.sizeof(info), None)
        print(info.Basic.LimitFlags, info.JobMemoryLimit, flush=True)
    """)
    limit_flags, job_memory = int(flags[0]), int(flags[1])
    assert limit_flags & 0x2000, 'KILL_ON_JOB_CLOSE was dropped by the memory step'
    assert limit_flags & 0x1000, 'SILENT_BREAKAWAY_OK was dropped by the memory step'
    assert limit_flags & 0x0800, 'BREAKAWAY_OK was dropped by the memory step'
    assert not limit_flags & 0x200, 'the one job carries a job-wide memory limit'
    assert job_memory == 0


@windows_only
def test_a_bound_child_and_the_parent_can_allocate_past_the_active_cap():
    """Review of #141 (20 Nov), the test asked for: a bound child allocates
    past the ACTIVE cap.  The real update_caps(MODE_ACTIVE) runs with total
    RAM read as 4 GB, so the cap it computes is max(0.5, 4 x 0.6 - 2) = 0.5 GB.
    The child (bound, then told to go) and the parent each commit 1 GB.
    Main never applied the cap (its job held no process), and it must not
    start biting here.  Measured with the cap on the one job (23 Nov, this
    test run inside a harness that already puts the interpreter in a job):
    the parent got MemoryError and the child allocated.  The review measured
    MemoryError in the bound child as well (1 GiB cap, 1.5 GiB child)."""
    out = _run("""
        from core.resource_governor import MODE_ACTIVE
        enforcer._get_total_ram_gb = lambda: 4.0
        enforcer._enforce_gpu = lambda fraction: None
        enforcer._ensure_job_windows()
        enforcer._enforced = True
        enforcer.update_caps(MODE_ACTIVE)
        child = subprocess.Popen(
            [sys.executable, '-c',
             "import sys; sys.stdin.readline(); b = bytearray(1 << 30); "
             "print('child-allocated', flush=True)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        bound = bind_to_parent(child)
        said, _ = child.communicate('go\\n', timeout=60)
        try:
            mine = bytearray(1 << 30)
            parent = 'parent-allocated'
        except MemoryError:
            parent = 'parent-MemoryError'
        print(bound, said.strip() or 'child-failed', parent, flush=True)
    """)
    assert out[0] == 'True', out
    assert out[1] == 'child-allocated', 'the bound child could not allocate past the ACTIVE cap'
    assert out[2] == 'parent-allocated', 'this process could not allocate past the ACTIVE cap'


@windows_only
def test_only_the_bound_child_dies_other_children_of_the_parent_survive_quit():
    """Review of #141: the job Nunba itself joins has kill-on-close, so with no
    breakaway flag EVERY child (an editor the agent opened, the setup wizard, a
    browser) was killed on quit, not only the workers.  Measured on Windows 11:
    SILENT_BREAKAWAY_OK keeps un-assigned children out of the job, so only the
    child bind_to_parent assigns dies."""
    other, bound = _run(f"""
        enforcer._ensure_job_windows()       # the parent is in the job
        other = {_CHILD}                     # an ordinary child, never bound
        child = {_CHILD}
        assert bind_to_parent(child)
        print(other.pid, child.pid, flush=True)
    """)
    try:
        assert _gone_within(int(bound), 10), 'the bound child outlived its parent'
        assert not _gone_within(int(other), 2), \
            'an ordinary child was killed with the parent (no breakaway on the job)'
    finally:
        _kill(int(other))


@windows_only
def test_a_child_can_ask_to_break_away_from_the_job():
    """hevolveai_supervisor spawns with CREATE_BREAKAWAY_FROM_JOB.  In a job
    without BREAKAWAY_OK that CreateProcess fails with WinError 5 (measured),
    and the supervisor retries forever."""
    out = _run("""
        enforcer._ensure_job_windows()
        try:
            c = subprocess.Popen([sys.executable, '-c', 'pass'],
                                 creationflags=0x01000000)   # CREATE_BREAKAWAY_FROM_JOB
            c.wait()
            print('ok', flush=True)
        except OSError as exc:
            print('failed', exc.winerror, flush=True)
    """)
    assert out == ['ok'], out


@windows_only
def test_control_an_unbound_child_outlives_the_parent():
    # Proves the tests above can see an orphan: without binding the child stays.
    pid, = _run(f"""
        child = {_CHILD}
        print(child.pid, flush=True)
    """)
    try:
        assert not _gone_within(int(pid), 2)
    finally:
        _kill(int(pid))


def test_off_windows_or_without_a_handle_binding_is_a_no_op(monkeypatch):
    class _NoHandle:
        _handle = None
    assert child_lifecycle.bind_to_parent(_NoHandle()) is False
    monkeypatch.setattr(child_lifecycle.sys, 'platform', 'linux')
    assert child_lifecycle.bind_to_parent(object()) is False


def test_there_is_one_job_bind_to_parent_does_not_create_its_own():
    """The parallel-path guard: bind_to_parent goes through the resource
    governor's enforcer; child_lifecycle keeps no job of its own for it."""
    src = Path(child_lifecycle.__file__).read_text(encoding='utf-8')
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == 'bind_to_parent')
    body = ast.get_source_segment(src, fn)
    assert 'get_enforcer' in body
    assert 'JobHandle' not in body
    assert 'CreateJobObject' not in body
    assert '_LIFECYCLE_JOB' not in src


@pytest.mark.parametrize('relpath, enclosing', [
    ('integrations/service_tools/gpu_worker.py', '_spawn'),
    ('integrations/service_tools/model_lifecycle.py', '_launch_llama_server_direct'),
])
def test_every_spawn_site_still_binds_its_child(relpath, enclosing):
    """Deleting the bind_to_parent call leaves the spawn working and the child
    orphaned on quit, so nothing else fails: pin the call (review of #141)."""
    src = (HARTOS_ROOT / relpath).read_text(encoding='utf-8')
    tree = ast.parse(src)
    scope = tree
    if enclosing:
        scope = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == enclosing)
    calls = [n for n in ast.walk(scope)
             if isinstance(n, ast.Call)
             and getattr(n.func, 'id', getattr(n.func, 'attr', None)) == 'bind_to_parent']
    assert calls, f'{relpath} no longer binds the child it spawns'
