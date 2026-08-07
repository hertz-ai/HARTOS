"""A leaked non-daemon thread must not hang the run — it must NAME itself (#37).

WHY THIS EXISTS
───────────────
The python gate had never produced a full verdict. Not because the suite was
big — measured on CI run 30885611303, shard 0 ran 1519 tests in 117.86s and
printed its summary at 06:58:23. It then emitted NOTHING for 1h55m, until the
120-minute job cap killed it and GitHub logged it as "cancelled". Five of eight
shards did exactly that.

The cause is Python's own shutdown: threading._shutdown() joins every
non-daemon thread before the interpreter exits. One thread that never returns
means the process never exits. `--timeout` is per-TEST and cannot see it,
because it happens after the last test finishes.

tests/conftest.py's pytest_sessionfinish hook detects that condition, prints
the thread names and where each is blocked, and then os._exit()s with pytest's
own status so the verdict is preserved.

WHAT THESE TESTS PIN
────────────────────
These run pytest as a SUBPROCESS against a generated throwaway test, because
the behaviour under test IS process exit — it cannot be observed from inside
the process that is supposed to die.

    1. a leaked non-daemon thread does not hang the run
    2. the report NAMES the thread, so the next person does not need a CI round
    3. the exit STATUS still reflects pass/fail — the guarantee must not
       manufacture a green
    4. a clean run is untouched: no hard exit, no diagnostics
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A test that leaks a non-daemon thread parked forever on an Event.
LEAKY = '''
import threading
_ev = threading.Event()
def test_leaks_a_non_daemon_thread():
    t = threading.Thread(target=_ev.wait, name="hart-leaked-probe")
    t.daemon = False
    t.start()
    assert True
'''

CLEAN = '''
def test_clean():
    assert True
'''

FAILING_LEAKY = '''
import threading
_ev = threading.Event()
def test_leaks_and_fails():
    t = threading.Thread(target=_ev.wait, name="hart-leaked-probe")
    t.daemon = False
    t.start()
    assert False, "deliberate"
'''


def _scratch():
    """A scratch dir UNDER tests/, because that is where conftest.py lives.

    The generated probe must inherit tests/conftest.py — a conftest applies to
    its own directory tree and nothing else. The first version of this test put
    the probe in the system temp dir, so the hook under test was never loaded
    and all three leak cases simply timed out at 120s: the test "failed" while
    proving nothing about the fix.

    NOT under tests/unit or tests/functional, so the sharded gate (which globs
    exactly those two) never collects the probe as a real test.

    Never rmtree'd: shutil.rmtree raises AttributeError on the orphaned dev
    venv (`os._walk_symlinks_as_files`, see
    memory/reference_broken_venv_test_invocation_2026-07-28). The single file
    is removed by name instead.
    """
    d = os.path.join(REPO, "tests", "_exit_guarantee_probe")
    os.makedirs(d, exist_ok=True)
    return d


def _run(body, timeout=120):
    """Run pytest as a subprocess on a generated test file."""
    f = pathlib.Path(_scratch()) / "test_generated_leak.py"
    f.write_text(textwrap.dedent(body), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=REPO)
    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", str(f), "-q", "-p", "no:randomly",
             "--timeout=30", "-p", "no:cacheprovider"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout)
    finally:
        try:
            f.unlink()
        except OSError:
            pass


class TestTheRunAlwaysTerminates:

    def test_a_leaked_non_daemon_thread_does_not_hang(self):
        """The whole point: this used to burn two hours and report nothing."""
        try:
            r = _run(LEAKY, timeout=120)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "pytest did not exit with a non-daemon thread alive — the "
                "exit guarantee in tests/conftest.py is not working, and CI "
                "will go back to burning the full job cap silently")
        assert r.returncode == 0, (
            f"the leaked-thread run should still report its PASS.\n"
            f"stdout:\n{r.stdout[-1500:]}\nstderr:\n{r.stderr[-1500:]}")

    def test_the_report_names_the_leaking_thread(self):
        """A hang with no attribution costs a CI round to learn nothing."""
        r = _run(LEAKY, timeout=120)
        combined = r.stdout + r.stderr
        assert "hart-leaked-probe" in combined, (
            "the thread's NAME must appear, otherwise the next person has to "
            f"bisect a 90-file shard by hand.\n{combined[-2000:]}")
        assert "NON-DAEMON THREADS STILL ALIVE" in combined

    def test_the_guarantee_does_not_manufacture_a_green(self):
        """Forcing exit must preserve the verdict, not invent one."""
        r = _run(FAILING_LEAKY, timeout=120)
        assert r.returncode != 0, (
            "a FAILING test that also leaked a thread exited 0 — the exit "
            "guarantee is masking failures, which is far worse than the hang "
            f"it replaces.\n{(r.stdout + r.stderr)[-1500:]}")

    def test_a_clean_run_is_left_completely_alone(self):
        """No leak -> normal shutdown, so atexit/coverage still run."""
        r = _run(CLEAN, timeout=120)
        combined = r.stdout + r.stderr
        assert r.returncode == 0, combined[-1500:]
        assert "NON-DAEMON THREADS STILL ALIVE" not in combined, (
            "the diagnostic fired on a clean run — it must be silent unless "
            "something is actually leaking")
