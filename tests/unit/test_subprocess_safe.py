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
import logging
import os
import subprocess
import sys
import time
from unittest.mock import patch

import core.subprocess_safe as subprocess_safe

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

    def test_posix_branch_returns_empty(self, monkeypatch):
        """The non-Windows branch, exercised ON Windows.

        Whichever box runs this suite, one of the two branches is dead code
        to it — so the OTHER one is never covered and a mistake there ships
        unseen to the platform that does run it. HART targets NixOS while
        this repo is developed on Windows, making that the normal case, not
        an edge one. Patching the platform check covers both directions from
        either host.
        """
        monkeypatch.setattr(sys, "platform", "linux")
        assert hidden_popen_kwargs() == {}

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="subprocess.STARTUPINFO and subprocess.CREATE_NO_WINDOW exist "
               "only inside CPython's `if _mswindows:` block, so this raises "
               "AttributeError INSIDE hidden_popen_kwargs() on Linux/macOS "
               "before any assert runs. Monkeypatching sys.platform selects "
               "the branch but cannot conjure the Win32 API objects that "
               "branch constructs.")
    def test_windows_branch_sets_no_window_flags(self, monkeypatch):
        """Windows-only by necessity — see the skipif.

        The POSIX half above stays portable and IS covered from either host,
        which keeps most of the cross-platform win. Making this one honestly
        host-dependent is better than it being falsely portable: a test added
        to stop platform mistakes shipping unseen would otherwise have been
        the thing that broke the Linux runner, which is where CI runs and what
        HART actually targets.
        """
        monkeypatch.setattr(sys, "platform", "win32")
        kw = hidden_popen_kwargs()
        assert kw.get("creationflags") == subprocess.CREATE_NO_WINDOW
        assert kw["startupinfo"].wShowWindow == 0


class TestKillCleanupIsUnkillable:
    """`_safe_kill_and_close` must never raise — it runs on the timeout path.

    If cleanup itself threw, a timed-out probe would surface as an exception
    from run_probe instead of the documented None, and every caller's
    "tool missing or hung" branch would be bypassed.
    """

    def test_zombie_child_does_not_raise(self, monkeypatch):
        """Child ignores kill() and never reaps: bounded, logged, no raise."""
        class _Stubborn:
            returncode = None
            stdout = stderr = None

            def kill(self):
                pass

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("cmd", timeout or 0)

            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired("cmd", timeout or 0)

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Stubborn())
        # Must still honour the contract: None, not an escaping exception.
        assert run_probe(["anything"], timeout=0.01) is None

    def test_kill_that_raises_is_swallowed(self, monkeypatch):
        """A kill() that itself throws (already-reaped race) must not escape."""
        class _Nasty:
            returncode = None
            stdout = stderr = None

            def kill(self):
                raise OSError("no such process")

            def wait(self, timeout=None):
                return 0

            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired("cmd", timeout or 0)

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Nasty())
        assert run_probe(["anything"], timeout=0.01) is None


class TestShellApiProbeContract:
    """The CONTRACT both shell API modules' `_run` must honour.

    These asserted object IDENTITY (`shell_system_apis._run is run_probe`)
    while the two modules were aliased to this module's probe. That alias is
    reverted for now — it broke TestShellWiFi/TestShellVPN, which mock
    `subprocess` wholesale in the module namespace and therefore stopped
    intercepting once the real work moved into core.subprocess_safe's
    namespace (see the note at shell_system_apis._run).

    So the identity assertion would now fail for a REVERT, not for the
    regression it was written to catch. Rewritten to assert the behavioural
    contract instead, which holds for the duplicate today and for the
    consolidated probe tomorrow — and which is what the 139 call sites
    actually depend on. Consolidation tracked in task #26.
    """

    def test_both_modules_resolve_to_the_one_implementation(self):
        """DRY guard, re-armed once the consolidation re-landed.

        The contract tests below hold for a duplicate too, so they cannot
        detect re-duplication — only identity can. This failed for the
        REVERT earlier today, which is why it was temporarily replaced
        rather than kept; with the alias restored it is the assertion that
        stops a third copy of `_run` appearing.
        """
        from integrations.agent_engine import shell_desktop_apis, shell_system_apis
        assert shell_system_apis._run is run_probe
        assert shell_desktop_apis._run is run_probe

    def _runners(self):
        from integrations.agent_engine import shell_desktop_apis, shell_system_apis
        return [("shell_system_apis", shell_system_apis._run),
                ("shell_desktop_apis", shell_desktop_apis._run)]

    def test_missing_tool_returns_none(self):
        for name, fn in self._runners():
            assert fn(["hart-not-a-real-binary-9f3a"]) is None, \
                f"{name}._run must degrade to None when the tool is absent"

    def test_success_exposes_completed_process_shape(self):
        for name, fn in self._runners():
            r = fn(_py("print('ok')"), timeout=30)
            assert r is not None, f"{name}._run lost a successful result"
            assert r.returncode == 0 and "ok" in r.stdout, \
                f"{name}._run must expose returncode/stdout"

    def test_nonzero_exit_is_not_collapsed_to_none(self):
        """A tool that ran and failed must stay distinguishable from an
        absent one — the whole point of the None sentinel."""
        for name, fn in self._runners():
            r = fn(_py("import sys; sys.exit(4)"), timeout=30)
            assert r is not None and r.returncode == 4, \
                f"{name}._run collapsed a real failure into 'tool missing'"


class TestKillReachesDescendants:
    r"""The gap that let D36 through: a kill whose pipes never reach EOF.

    Every existing boundedness test here kills a child whose stdout pipe THEN
    reaches EOF, so `_safe_kill_and_close`'s `fh.close()` is uncontended and
    returns instantly. That is the easy half of the problem, and passing it
    told us nothing about the hard half.

    THE HARD HALF, measured live on 2026-09-09 (agent 33323830039). A reuse
    turn sat in this exact frame for 85+ seconds across three thread dumps:

        core/subprocess_safe.py:174, in run_bounded
            _safe_kill_and_close(proc, ...)
        core/subprocess_safe.py:256, in _safe_kill_and_close
            fh.close()

    while the SAME dump held two live reader threads:

        Thread-373 / Thread-374 (_readerthread)
            File "C:\Python312\Lib\subprocess.py", line 1599, in _readerthread
                buffer.append(fh.read())

    `fh.read()` holds the file object's lock for its whole duration and
    `fh.close()` must acquire that same lock, so the parent blocks for exactly
    as long as the reader stays in read().  The module docstring's premise —
    "Closing the parent FD causes the read() to return EOF -> thread exits
    cleanly" — does not hold once a reader is ALREADY inside read().

    This test builds that state deliberately: the direct child spawns a
    GRANDCHILD that inherits the same stdout pipe, then the child is killed.
    kill() reaches only the direct child, the grandchild keeps the write end
    open, so no EOF ever arrives and the reader never returns.

    It asserts WALL CLOCK from a watchdog thread rather than calling
    run_bounded inline, because the pre-fix failure mode is an unbounded
    block: called inline it would hang the whole suite instead of failing it.
    """

    # Child spawns a grandchild that inherits stdout (so it holds the pipe's
    # write end), then sleeps.  Killing the child cannot close that handle.
    _HOLDS_PIPE_AFTER_DEATH = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "time.sleep(60)"
    )

    def test_surviving_grandchild_does_not_wedge_the_caller(self):
        import threading

        box = {}

        def _run():
            t0 = time.monotonic()
            try:
                box["result"] = run_bounded(
                    _py(self._HOLDS_PIPE_AFTER_DEATH), timeout=2.0)
            except Exception as exc:            # pragma: no cover - diagnostic
                box["error"] = exc
            box["secs"] = time.monotonic() - t0

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        # timeout 2s + wait_after_kill 2s + tree-kill + generous slack.
        worker.join(25.0)

        assert not worker.is_alive(), (
            "run_bounded did not return within 25s for a child whose pipe "
            "never reaches EOF — it is blocked in _safe_kill_and_close's "
            "fh.close(), waiting on the lock a live _readerthread holds "
            "inside fh.read(). This is D36, measured live 2026-09-09."
        )
        assert "error" not in box, f"run_bounded raised: {box.get('error')!r}"
        result = box["result"]
        assert result.timed_out is True
        assert result.returncode == -1
        assert box["secs"] < 25.0, (
            f"bounded call took {box['secs']:.1f}s for a 2s budget"
        )

    def test_the_scenario_actually_outlives_the_direct_child(self):
        """Anti-vacuity: prove the fixture really does keep the pipe open.

        If the grandchild died with its parent, the test above would pass
        against the BROKEN code and verify nothing. So assert the shape
        directly: kill the child, and confirm its stdout pipe still has not
        reached EOF a moment later.
        """
        proc = subprocess.Popen(
            _py(self._HOLDS_PIPE_AFTER_DEATH),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, text=True,
        )
        try:
            time.sleep(1.5)                     # let the grandchild spawn
            proc.kill()
            proc.wait(timeout=10)
            # The direct child is gone...
            assert proc.returncode is not None
            # ...but a read must NOT return promptly with EOF, because the
            # grandchild still owns the write end. Probe it off-thread.
            import threading
            got = {}

            def _read():
                try:
                    got["data"] = proc.stdout.read()
                except Exception as exc:        # pragma: no cover
                    got["error"] = exc

            r = threading.Thread(target=_read, daemon=True)
            r.start()
            r.join(4.0)
            assert r.is_alive(), (
                "the grandchild did NOT keep the pipe open, so this fixture "
                "cannot reproduce D36 and the test above would be vacuous"
            )
        finally:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10)


# MERGE NOTE 2026-09-11: two lanes each appended a test class here. They
# guard different invariants of the same module -- one that kill() must be
# bounded even when a grandchild holds the pipe open (D36), one that a
# missing tool is reported rather than silently degraded -- so both are
# kept. Neither was dropped to make the merge tidy.

class TestAMissingToolIsSaidOutLoudOnce:
    """A silent degrade is how the same defect survived four rounds.

    run_probe cannot tell "not installed" from "installed, but not on THIS
    process's PATH", and the second is a real bug: flatpak (2026-08-12), six
    more capabilities (2026-08-26), gtk-launch (2026-09-01), and every nix
    binary (2026-09-07, when a sweep found 33 of the 77 tools the shell shells
    were installed on the box and invisible to the service). All of it hid
    behind a debug line while callers degraded by design.

    Per call it stays debug (139 call sites, hot path). Once per binary it is a
    warning, which is what makes the class findable in a journal.
    """

    def setup_method(self):
        subprocess_safe._missing_tools_seen.clear()

    def test_the_first_miss_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger=subprocess_safe.__name__):
            with patch.object(subprocess_safe, 'run_bounded',
                              side_effect=FileNotFoundError()):
                assert subprocess_safe.run_probe(['definitely-not-a-tool']) is None
        assert any('definitely-not-a-tool' in r.message for r in caplog.records)

    def test_the_warning_names_the_ambiguity_not_a_conclusion(self, caplog):
        """It must not assert the tool is absent. Saying so is what sent an
        operator to debug a Flatpak install that was working fine."""
        with caplog.at_level(logging.WARNING, logger=subprocess_safe.__name__):
            with patch.object(subprocess_safe, 'run_bounded',
                              side_effect=FileNotFoundError()):
                subprocess_safe.run_probe(['some-tool'])
        # getMessage() formats once; r.message is ALREADY formatted, so
        # applying r.args to it again raises TypeError.
        text = ' '.join(r.getMessage() for r in caplog.records)
        assert 'PATH' in text

    def test_it_stays_quiet_after_the_first(self, caplog):
        """139 call sites on a hot path: one line per binary per process, not
        one per call."""
        with patch.object(subprocess_safe, 'run_bounded',
                          side_effect=FileNotFoundError()):
            with caplog.at_level(logging.WARNING, logger=subprocess_safe.__name__):
                for _ in range(25):
                    subprocess_safe.run_probe(['repeated-tool'])
        hits = [r for r in caplog.records if 'repeated-tool' in r.message]
        assert len(hits) == 1, 'expected exactly one warning, got %d' % len(hits)

    def test_each_distinct_binary_gets_its_own_line(self, caplog):
        with patch.object(subprocess_safe, 'run_bounded',
                          side_effect=FileNotFoundError()):
            with caplog.at_level(logging.WARNING, logger=subprocess_safe.__name__):
                subprocess_safe.run_probe(['tool-a'])
                subprocess_safe.run_probe(['tool-b'])
        msgs = ' '.join(r.message for r in caplog.records)
        assert 'tool-a' in msgs and 'tool-b' in msgs

    def test_an_empty_argv_does_not_explode(self, caplog):
        with patch.object(subprocess_safe, 'run_bounded',
                          side_effect=FileNotFoundError()):
            assert subprocess_safe.run_probe([]) is None
class TestTheProbeLooksWhereALoginShellWould:
    """A unit's PATH lists only its own dependencies, so a working system tool
    reads as absent from inside a service. Audited on the box 2026-09-07: 33 of
    the 77 binaries the shell layer shells were installed and invisible to
    hart-liquid-ui, and all 29 OTHER hart-* units had the same blindness. One
    shared helper is the fix, not 29 nix modules.
    """

    def test_existing_system_dirs_are_appended(self):
        with patch.object(subprocess_safe.os.path, 'isdir', return_value=True):
            out = subprocess_safe.system_search_path('/only/this')
        parts = out.split(os.pathsep)
        assert parts[0] == '/only/this', 'the caller PATH must stay first'
        assert '/run/current-system/sw/bin' in parts
        assert '/run/wrappers/bin' in parts

    def test_it_appends_never_prepends(self):
        """A pinned or shimmed tool keeps its precedence. The rustdesk guard in
        hart-liquid-ui.nix depends on being first on PATH."""
        with patch.object(subprocess_safe.os.path, 'isdir', return_value=True):
            out = subprocess_safe.system_search_path('/a')
        assert out.startswith('/a' + os.pathsep)

    def test_absent_dirs_are_not_invented(self):
        """On a dev host or in a container these do not exist, and claiming
        them would put a lie in PATH."""
        with patch.object(subprocess_safe.os.path, 'isdir', return_value=False):
            assert subprocess_safe.system_search_path('/a:/b') == '/a:/b'

    def test_no_duplicates_when_already_present(self):
        with patch.object(subprocess_safe.os.path, 'isdir', return_value=True):
            out = subprocess_safe.system_search_path('/run/current-system/sw/bin')
        assert out.split(os.pathsep).count('/run/current-system/sw/bin') == 1

    def test_run_probe_passes_the_augmented_path_down(self):
        captured = {}

        def fake(cmd, timeout=5.0, **kw):
            captured.update(kw)
            return subprocess_safe.BoundedResult(0, '', '', False)

        with patch.object(subprocess_safe.os.path, 'isdir', return_value=True):
            with patch.object(subprocess_safe, 'run_bounded', side_effect=fake):
                subprocess_safe.run_probe(['anything'])
        assert '/run/current-system/sw/bin' in captured['env']['PATH']

    def test_a_callers_own_env_is_kept_and_augmented(self):
        """Passing env must not lose the caller's variables, and must not lose
        the augmentation either."""
        captured = {}

        def fake(cmd, timeout=5.0, **kw):
            captured.update(kw)
            return subprocess_safe.BoundedResult(0, '', '', False)

        with patch.object(subprocess_safe.os.path, 'isdir', return_value=True):
            with patch.object(subprocess_safe, 'run_bounded', side_effect=fake):
                subprocess_safe.run_probe(
                    ['anything'], env={'PATH': '/mine', 'MARKER': 'kept'})
        env = captured['env']
        assert env['MARKER'] == 'kept'
        assert env['PATH'].startswith('/mine')
        assert '/run/current-system/sw/bin' in env['PATH']
