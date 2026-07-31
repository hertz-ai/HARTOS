"""core.subprocess_safe — the canonical bounded external-command probe.

WHY THIS FILE EXISTS
────────────────────
`core/subprocess_safe.py` shipped with ZERO tests despite being the module
that exists specifically to stop the OS from hanging (its docstring cites a
27-minute wmic wedge and a 5-minute nvidia-smi wedge). It is now also the
single home of `run_probe()`, which replaced a byte-equivalent private
`_run()` in BOTH `shell_system_apis.py` and `shell_desktop_apis.py` — 139
call sites, every hardware probe the desktop shell makes.

These tests are behavioural: they spawn REAL child processes through the
REAL Popen path, so the timeout + pipe-close fix is genuinely exercised
rather than asserted about. `sys.executable` keeps them portable across the
Windows dev box and the NixOS target.

The timeout test is also the latency guard: the whole point of a bounded
probe is that a wedged tool cannot outlive its deadline, so the test asserts
WALL-CLOCK, not just the return value. A version of this module that
regressed to plain `subprocess.run` would still return None here — it would
just take minutes to do it, and only the clock catches that.
"""
import os
import subprocess
import sys
import time

import pytest

from core.subprocess_safe import (
    BoundedResult,
    hidden_popen_kwargs,
    run_bounded,
    run_probe,
)


def _py(code):
    """argv running `code` in this interpreter — portable child process."""
    return [sys.executable, "-c", code]


class TestRunProbeSemantics:
    """The exact contract inherited from the two `_run` copies it replaced.

    Preserving these is what made it safe to swap 139 call sites without
    touching any of them.
    """

    def test_success_returns_result_with_stdout(self):
        r = run_probe(_py("print('hart-probe-ok')"), timeout=30)
        assert r is not None
        assert r.returncode == 0
        assert "hart-probe-ok" in r.stdout

    def test_missing_tool_returns_none(self):
        """Tool absent is a DEGRADE, not an error — callers branch on None.

        This is the single most common real case: no lspci in a container,
        no nmcli on a headless server.
        """
        assert run_probe(["hart-definitely-not-a-real-binary-9f3a"]) is None

    def test_nonzero_exit_still_returns_result(self):
        """A tool that RAN and failed is not the same as a missing tool.

        Callers check `r.returncode`; collapsing this to None would make a
        failing command indistinguishable from an uninstalled one.
        """
        r = run_probe(_py("import sys; sys.exit(3)"), timeout=30)
        assert r is not None
        assert r.returncode == 3

    def test_stderr_is_captured_separately(self):
        r = run_probe(_py("import sys; sys.stderr.write('warn-line')"), timeout=30)
        assert r is not None
        assert "warn-line" in r.stderr
        assert "warn-line" not in r.stdout


class TestRunProbeBoundedness:
    """The hang fix itself — a wedged child must not outlive its deadline."""

    def test_timeout_returns_none(self):
        assert run_probe(_py("import time; time.sleep(60)"), timeout=1) is None

    def test_timeout_is_enforced_in_wall_clock(self):
        """LATENCY GUARD: a 60s child under a 1s deadline must release the
        caller in ~1s, not 60.

        This is the assertion that actually detects a regression to plain
        `subprocess.run(...)`: that version ALSO returns None, but only
        after its timeout handler joins the orphaned reader threads. On a
        booted node this difference is a frozen shell panel.

        Budget: deadline + kill/reap slack. Generous enough for a loaded
        CI runner, tight enough that a minutes-long wedge fails loudly.
        """
        start = time.monotonic()
        result = run_probe(_py("import time; time.sleep(60)"), timeout=1)
        elapsed = time.monotonic() - start
        assert result is None
        assert elapsed < 15, (
            f"bounded probe took {elapsed:.1f}s for a 1s deadline — the "
            f"timeout did not release the caller promptly"
        )

    def test_child_writing_forever_still_bounded(self):
        """The reader-thread orphan case, directly.

        A child that keeps the pipe hot is what wedges `subprocess.run`'s
        cleanup: kill() does not close the parent-side handles, so the
        reader threads stay blocked in read() and join() never returns.
        """
        start = time.monotonic()
        result = run_probe(
            _py("import sys\nwhile True: sys.stdout.write('x' * 4096)"),
            timeout=1,
        )
        elapsed = time.monotonic() - start
        assert result is None
        assert elapsed < 15, (
            f"noisy child took {elapsed:.1f}s to bound — reader threads "
            f"were likely orphaned"
        )

    def test_child_reading_stdin_does_not_hang(self):
        """stdin is DEVNULL, so a tool that unexpectedly prompts gets EOF.

        With inherited stdin (the old `_run` behaviour) such a child blocks
        until the deadline; with DEVNULL it exits immediately. Asserting
        the FAST path proves DEVNULL is actually wired.
        """
        start = time.monotonic()
        r = run_probe(
            _py("import sys; sys.stdin.read(); print('got-eof')"), timeout=30
        )
        elapsed = time.monotonic() - start
        assert r is not None and "got-eof" in r.stdout
        assert elapsed < 15, "stdin was not DEVNULL — child waited for input"


class TestRunProbeErrorPropagation:
    """Real faults must NOT be swallowed into the None degrade path."""

    def test_permission_error_propagates(self, monkeypatch):
        """A non-executable target is a broken install, not a missing tool.

        Silently returning None here would hide it forever — the caller
        would render "feature unavailable" for a file that is right there.
        """
        def _boom(*a, **kw):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(subprocess, "Popen", _boom)
        with pytest.raises(PermissionError):
            run_probe(["anything"])

    def test_file_not_found_is_the_only_swallowed_oserror(self, monkeypatch):
        def _boom(*a, **kw):
            raise FileNotFoundError(2, "No such file")

        monkeypatch.setattr(subprocess, "Popen", _boom)
        assert run_probe(["anything"]) is None


class TestRunBoundedKwargs:
    """`**popen_kwargs` — added so the shell APIs' `**kw` forward survived."""

    def test_cwd_is_honoured(self, tmp_path):
        r = run_bounded(_py("import os; print(os.getcwd())"), timeout=30,
                        cwd=str(tmp_path))
        assert r.returncode == 0
        assert os.path.realpath(r.stdout.strip()) == os.path.realpath(str(tmp_path))

    def test_env_is_honoured(self):
        env = dict(os.environ, HART_PROBE_MARKER="canary-77")
        r = run_bounded(
            _py("import os; print(os.environ.get('HART_PROBE_MARKER'))"),
            timeout=30, env=env,
        )
        assert "canary-77" in r.stdout

    def test_defaults_still_applied_when_kwargs_given(self):
        """Caller overrides must not silently drop the piping the contract
        depends on — stdout is still captured when cwd is passed."""
        r = run_bounded(_py("print('still-piped')"), timeout=30, cwd=os.getcwd())
        assert "still-piped" in r.stdout

    def test_returns_bounded_result_shape(self):
        r = run_bounded(_py("print('x')"), timeout=30)
        assert isinstance(r, BoundedResult)
        assert r.timed_out is False

    def test_timed_out_flag_set(self):
        r = run_bounded(_py("import time; time.sleep(60)"), timeout=1)
        assert r.timed_out is True
        assert r.returncode == -1


class TestNoWindowFlagsOnWindows:
    """The frozen Nunba GUI must not flicker a console per probe."""

    def test_hidden_kwargs_are_platform_correct(self):
        kw = hidden_popen_kwargs()
        if sys.platform == "win32":
            assert "startupinfo" in kw and "creationflags" in kw
        else:
            assert kw == {}


class TestShellApiConsolidation:
    """DRY: both shell API modules resolve to the ONE implementation.

    This is an identity assertion on the imported objects, not a source
    grep — it fails if anyone reintroduces a local `_run`, because the
    module attribute would stop being `run_probe`.
    """

    def test_system_apis_uses_canonical_probe(self):
        from integrations.agent_engine import shell_system_apis
        assert shell_system_apis._run is run_probe

    def test_desktop_apis_uses_canonical_probe(self):
        from integrations.agent_engine import shell_desktop_apis
        assert shell_desktop_apis._run is run_probe

    def test_both_modules_share_one_implementation(self):
        from integrations.agent_engine import shell_desktop_apis, shell_system_apis
        assert shell_system_apis._run is shell_desktop_apis._run
