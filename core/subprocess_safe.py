"""
core.subprocess_safe — bounded external-command execution.

WHY THIS EXISTS
───────────────
`subprocess.run(cmd, capture_output=True, text=True, timeout=N)` is the
canonical way to read a child process' stdout with a time limit.  On
Windows it has a latent failure mode that becomes load-bearing for
nunba's pytest runs and first-boot probes: when the child is killed
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
import subprocess
import sys
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)


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


def run_bounded(
    cmd: Sequence[str],
    timeout: float = 5.0,
    *,
    wait_after_kill: float = 2.0,
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

    Returns:
        BoundedResult with .returncode, .stdout, .stderr, .timed_out.
        On timeout: returncode=-1, timed_out=True, output fields empty.

    Raises:
        FileNotFoundError: cmd[0] not on PATH (caller handles).
        OSError: other Popen spawn failure (caller handles).
    """
    popen_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "text": True,
    }
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        popen_kwargs["startupinfo"] = si
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    # FileNotFoundError / OSError from Popen propagate — callers that
    # already do `except FileNotFoundError: pass` still work unchanged.
    proc = subprocess.Popen(list(cmd), **popen_kwargs)

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


def _safe_kill_and_close(
    proc: "subprocess.Popen[str]",
    cmd_name: str,
    *,
    wait_after_kill: float,
) -> None:
    """Kill proc, close pipes, bounded wait — no exception escapes.

    The explicit close() on stdout/stderr is the load-bearing line:
    without it, Python's _readerthread daemons stay blocked in
    fh.read() after the child dies, and join() wedges.  Closing the
    parent FD causes the read() to return EOF → thread exits cleanly.
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


__all__ = ["BoundedResult", "run_bounded"]
