"""
core.subprocess_safe — bounded external-command execution.

WHY THIS EXISTS
───────────────
`subprocess.run(cmd, capture_output=True, text=True, timeout=N)` is the
canonical way to read a child process' stdout with a time limit.  On
Windows it has a latent failure mode that bites nunba's pytest runs
and first-boot probes: when the child is killed
mid-initialization (e.g. nvidia-smi during driver probe, wmic on a
cold WMI repository, sysctl on a locked macOS kernel), Python's two
`_readerthread` daemons stay blocked in `fh.read()` because
`Popen.kill()` does NOT close stdout/stderr pipes on the parent side.
`subprocess.run`'s timeout handler then calls `communicate()` to drain
them, which joins those orphaned readers → the entire call wedges for
minutes (observed: 27 min wmic hang 2026-04-15; 5+ min nvidia-smi hang
during tests/journey/ setup).

CLAUDE.md Gate 7 already bans `os.popen` and `subprocess.run` without
a timeout; this module closes the adjacent hole where the timeout
fires but the reader-thread cleanup still hangs.

THE FIX
───────
Drive Popen directly.  On TimeoutExpired, kill() then **explicitly
close** the parent-side pipe handles so any still-running reader
thread unblocks and exits; finally `wait()` briefly to reap.

Always returns a `BoundedResult` — never raises TimeoutExpired.
`FileNotFoundError` propagates (caller decides "tool missing" vs
"tool failed"), matching the semantics of the subprocess.run calls
this replaces.

WHO CALLS IT
────────────
- integrations/service_tools/vram_manager.py (nvidia-smi, rocm-smi)
- security/system_requirements.py (_detect_camera_hw vcgencmd,
  _detect_ram_gb sysctl fallback)

For new callers: use `run_bounded()` from this module for any
external-tool probe where the child can block on init.  Do NOT add
fresh `subprocess.run(..., capture_output=True, text=True, timeout=N)`
sites — they reintroduce the reader-thread orphan.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from typing import Optional, Sequence

logger = logging.getLogger(__name__)


def hidden_popen_kwargs() -> dict:
    """Deprecated alias for `no_window_kwargs()`.  Kept for its 34 callers.

    Until 2026-08-29 this carried its own copy of the flag-building body.  The
    two returned identical values -- same keys, creationflags 134217728,
    startupinfo dwFlags=1 wShowWindow=0 -- so this is a name, not a second
    behaviour.  It now delegates, leaving one implementation of the concept.
    New callers should use no_window_kwargs(), which run_bounded() also uses.
    """
    return no_window_kwargs()


class BoundedResult:
    """Minimal CompletedProcess-shaped result.

    Exposes `returncode`, `stdout`, `stderr` (both str), and
    `timed_out` (True when the child was killed by the watchdog).
    """
    __slots__ = ("returncode", "stdout", "stderr", "timed_out")

    def __init__(
        self,
        returncode: int,
        stdout: str,
        stderr: str,
        timed_out: bool = False,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


def no_window_kwargs() -> dict:
    """Popen/run kwargs that stop Windows painting a console window.

    THE ONE SOURCE for these flags in HARTOS.  `run_bounded` uses it, and so
    must every direct `subprocess.run/Popen` that can execute on Windows.

    Returns ``{}`` off win32, so `**no_window_kwargs()` is safe to splat at any
    call site on any platform — callers never need a `sys.platform` branch.

    WHY IT MATTERS HERE.  HARTOS is imported IN-PROCESS by Nunba.exe, which is
    a GUI-subsystem binary owning no console.  Spawning a console-subsystem
    child from such a parent without CREATE_NO_WINDOW makes Windows allocate a
    fresh, VISIBLE console for the child's lifetime — the "brief cmd windows"
    users report.  Measured 2026-08-13: visible top-level ConsoleWindowClass
    windows go 1 -> 2 without the flag and stay at 1 with it.

    conhost.exe appearing in the process tree proves NOTHING either way: a
    CREATE_NO_WINDOW child still gets a conhost, just a headless one.  Judge by
    window visibility, not by conhost's existence.
    """
    if sys.platform != "win32":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}


def run_bounded(
    cmd: Sequence[str],
    timeout: float = 5.0,
    *,
    wait_after_kill: float = 2.0,
    **popen_kwargs,
) -> BoundedResult:
    """Run `cmd` with a hard timeout and reader-thread-safe cleanup.

    Unlike ``subprocess.run(..., capture_output=True, text=True,
    timeout=N)``, this helper explicitly closes the parent-side stdout
    / stderr pipes after killing a timed-out child.  That releases the
    OS handles the `_readerthread` daemons are blocked on, so they
    unblock and exit instead of wedging the caller forever.

    Args:
        cmd: argv list — never a shell string.
        timeout: seconds to wait for the child's natural exit.
        wait_after_kill: seconds to wait for proc cleanup after kill()
            before giving up and letting the OS reap a zombie.
        **popen_kwargs: extra Popen kwargs (``cwd``, ``env``, …) merged
            over the defaults. Do NOT pass ``capture_output`` — that is a
            ``subprocess.run`` argument and Popen rejects it; stdout and
            stderr are already piped here.

    Returns:
        BoundedResult with .returncode, .stdout, .stderr, .timed_out.
        On timeout: returncode=-1, timed_out=True, output fields empty.

    Raises:
        FileNotFoundError: cmd[0] not on PATH (caller handles).
        OSError: other Popen spawn failure (caller handles).
    """
    popen_kwargs_base = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "text": True,
    }
    popen_kwargs_base.update(no_window_kwargs())

    # Caller overrides win over the defaults (cwd/env/stdin), but they
    # cannot silently drop the piping this function's contract depends on.
    popen_kwargs_base.update(popen_kwargs)

    # FileNotFoundError / OSError from Popen propagate — callers that
    # already do `except FileNotFoundError: pass` still work unchanged.
    proc = subprocess.Popen(list(cmd), **popen_kwargs_base)

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return BoundedResult(
            returncode=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=False,
        )
    except subprocess.TimeoutExpired:
        _safe_kill_and_close(proc, cmd[0] if cmd else "<unknown>",
                             wait_after_kill=wait_after_kill)
        return BoundedResult(
            returncode=-1, stdout="", stderr="", timed_out=True,
        )


# Where a NixOS node keeps the tools a login shell can see. A systemd unit's
# PATH is built from its own declared dependencies and contains NEITHER of
# these, which is the whole problem below.
_SYSTEM_TOOL_DIRS = ("/run/current-system/sw/bin", "/run/wrappers/bin")


def system_search_path(base: Optional[str] = None) -> str:
    """``base`` PATH with the node's system tool dirs APPENDED if they exist.

    THE ONE DEFINITION of "where the system tools are". A systemd unit does not
    inherit a login shell's PATH, so a tool that is installed and working is
    indistinguishable, from inside a service, from one that was never there.
    Audited on the box 2026-09-07: 33 of the 77 binaries the shell layer shells
    were installed and invisible to hart-liquid-ui, and ALL 29 other hart-*
    units had the same blindness.

    Appended, never prepended, so a caller's own PATH always wins and a pinned
    or shimmed tool keeps its precedence. Only existing dirs are added, so this
    is a no-op on a dev host or in a container, and returns ``base`` unchanged
    there.
    """
    path = base if base is not None else os.environ.get("PATH", "")
    entries = [p for p in path.split(os.pathsep) if p]
    for d in _SYSTEM_TOOL_DIRS:
        if d not in entries and os.path.isdir(d):
            entries.append(d)
    return os.pathsep.join(entries)


_missing_tools_seen: set = set()
_missing_tools_lock = threading.Lock()


def _warn_missing_tool_once(name: str) -> None:
    """Warn the FIRST time a binary is found missing, then stay quiet.

    See the FileNotFoundError branch of ``run_probe`` for why this exists. The
    wording deliberately names the ambiguity rather than asserting the tool is
    absent, because this function cannot tell the two cases apart and guessing
    wrong is what sent an operator off to debug a working Flatpak install.
    """
    with _missing_tools_lock:
        if name in _missing_tools_seen:
            return
        _missing_tools_seen.add(name)
    logger.warning(
        "run_probe: %r is not on this process's PATH, so every feature that "
        "shells it degrades silently from here on. If it IS installed on this "
        "machine, the gap is this process's PATH, not a missing system tool.",
        name)


def run_probe(
    cmd: Sequence[str],
    timeout: float = 10.0,
    **popen_kwargs,
) -> Optional[BoundedResult]:
    """Probe an external tool; ``None`` means "no answer available".

    THE CANONICAL SHELL-API PROBE. `integrations/agent_engine/
    shell_system_apis.py` and `shell_desktop_apis.py` each carried a
    byte-equivalent private `_run()` — 139 call sites across the two —
    and both were the exact `subprocess.run(capture_output=True,
    text=True, timeout=N)` shape this module's docstring tells new
    callers not to write. So the OS's two biggest hardware-probing
    surfaces (the ones running lspci / nmcli / bluetoothctl / upower on
    a booted node) were the most exposed to the reader-thread orphan
    hang, which on a desktop shell shows up as a frozen panel.

    Consolidated here rather than into a third `shell_common.py`,
    because a bounded-subprocess helper already had a canonical home.

    Semantics are preserved EXACTLY as the two `_run`s had them, since
    the call sites were left untouched:
      * tool missing (FileNotFoundError)  → None
      * tool exceeded `timeout`           → None
      * otherwise → a result exposing .returncode / .stdout / .stderr

    The single behavioural CHANGE is the fix itself: a timed-out child
    now gets its parent-side pipes closed, so the reader threads unblock
    instead of wedging the request. `stdin` is also DEVNULL, so a tool
    that unexpectedly reads stdin returns EOF rather than hanging
    forever — both strictly reduce ways the shell can freeze.

    Other OSErrors (PermissionError on a non-executable, ENOEXEC) still
    propagate, exactly as before — they are real faults, not a missing
    optional tool, and swallowing them here would hide a broken install.
    """
    # Look for the tool where a login shell would. Every caller of this probe
    # runs inside a systemd unit whose PATH lists only its own dependencies, so
    # without this a working system tool reads as absent. A caller that passes
    # its own env keeps it, augmented the same way; nothing is ever shadowed,
    # because the additions go on the END.
    _env = popen_kwargs.get("env")
    _base = _env.get("PATH") if _env is not None else None
    _search = system_search_path(_base)
    if _search:
        popen_kwargs["env"] = dict(_env if _env is not None else os.environ,
                                   PATH=_search)

    try:
        result = run_bounded(cmd, timeout=timeout, **popen_kwargs)
    except FileNotFoundError:
        # Expected in general: optional tooling absent on this build (no lspci
        # in a container, no nmcli on a headless server). PER CALL this stays
        # debug, because callers degrade by design and this is a hot path.
        #
        # ONCE per binary it is a warning, because from here "not installed"
        # and "installed, but not on THIS process's PATH" are the same
        # exception, and the second is a real defect that hid behind this line
        # four separate times: flatpak (2026-08-12), six more capabilities
        # (2026-08-26), gtk-launch (2026-09-01), and every nix binary
        # (2026-09-07, when a sweep found 33 of the 77 tools the shell shells
        # were installed on the box and invisible to the service). Each one
        # degraded silently, so the OS reported its own working features as
        # unavailable. One line per binary per process costs nothing on the hot
        # path and makes the whole class findable in a journal.
        name = cmd[0] if cmd else "<empty>"
        logger.debug("run_probe: %s not present on PATH", name)
        _warn_missing_tool_once(name)
        return None
    if result.timed_out:
        # run_bounded already logged a warning with the command name.
        return None
    return result


def _safe_kill_and_close(
    proc: "subprocess.Popen[str]",
    cmd_name: str,
    *,
    wait_after_kill: float,
) -> None:
    """Kill proc, close pipes, bounded wait — no exception escapes.

    Without the explicit close() on stdout/stderr, Python's
    _readerthread daemons stay blocked in fh.read() after the child
    dies, and join() wedges.  Closing the parent FD causes the read()
    to return EOF → thread exits cleanly.
    """
    logger.warning(
        "subprocess %s exceeded timeout; killing + closing pipes "
        "to unblock reader threads", cmd_name,
    )
    try:
        proc.kill()
    except Exception:
        pass
    for fh in (proc.stdout, proc.stderr):
        try:
            if fh is not None and not fh.closed:
                fh.close()
        except Exception:
            pass
    try:
        proc.wait(timeout=wait_after_kill)
    except subprocess.TimeoutExpired:
        logger.warning(
            "subprocess %s did not exit within %.1fs after kill; "
            "leaving as zombie (OS will reap)",
            cmd_name, wait_after_kill,
        )
    except Exception:
        pass


__all__ = ["BoundedResult", "run_bounded", "run_probe",
           "no_window_kwargs", "hidden_popen_kwargs"]
