"""ProcessSupervisor — the shared spawn/stream/backoff-restart loop (#110).

Three supervisors (livekit_supervisor, whatsapp_supervisor, hevolveai_supervisor)
each pasted the SAME ``_run`` skeleton:

    backoff = 1.0
    while not stop_event.is_set():
        spawn the child (Popen), stream its stdout to the logger, wait for it to
        exit, then sleep `backoff` (exp, capped 60s) and respawn.

…differing only in (a) the command/env they build, (b) how they format a stdout
line, and (c) what they do when the child exits. hevolveai additionally needs the
#59 circuit breakers (disable after N unhealthy exits), Windows Job-Object binding,
governor registration, and a backoff-reset after healthy uptime.

This base owns the loop + the lifecycle boilerplate (thread, stop, terminate→kill);
subclasses supply ONLY the differences via the hooks below. No subprocess is
spawned at import; the base is unit-testable by mocking the hooks (see
tests/unit/test_process_supervisor.py).
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class ProcessSupervisor:
    """Base class: subclass + implement ``_build_popen``; override the optional
    hooks for per-supervisor behaviour. One instance per supervised child."""

    # ── tunables (override as class attrs) ──
    name: str = 'process'
    backoff_cap: float = 60.0
    # Seconds of uptime after which the backoff counter resets to 1s (so a
    # transient crash after a long healthy run respawns promptly instead of
    # waiting the 60s cap accumulated during an earlier storm). None = never
    # reset (preserves livekit/whatsapp's original behaviour).
    reset_backoff_after: Optional[float] = None
    # One-shot extra wait a subclass may request from _on_child_exit: the
    # NEXT restart wait becomes max(normal backoff, extra_wait_once), then
    # the field auto-clears. This is how a circuit breaker COOLS DOWN AND
    # RE-ARMS instead of disabling forever (return True remains the
    # terminal option). 0.0 = no effect (default for all supervisors).
    extra_wait_once: float = 0.0
    # Exception types from the spawn that should DISABLE the loop (stop retrying)
    # rather than back off and respawn — e.g. whatsapp's FileNotFoundError when
    # node is missing. Empty = every spawn error is treated as transient.
    fatal_spawn_errors: Tuple[type, ...] = ()

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.last_error: Optional[str] = None
        self.last_started: Optional[float] = None
        self.restart_count = 0

    # ── hooks (subclasses override) ─────────────────────────────────────
    def _build_popen(self) -> Tuple[list, Dict[str, Any]]:
        """Return ``(cmd, popen_kwargs)``. popen_kwargs SHOULD set stdout=PIPE +
        stderr=STDOUT for the base to stream output; include cwd/env/text/etc.
        and the canonical core.subprocess_safe.hidden_popen_kwargs()."""
        raise NotImplementedError

    def _on_started(self, proc: subprocess.Popen) -> None:
        """Called immediately after a successful Popen (before output stream) —
        e.g. bind a Windows Job Object, register with the resource governor."""

    def _format_stdout_line(self, line: str) -> None:
        """Emit one decoded, stripped, non-empty stdout line. Default: log it
        under the supervisor name. Override to parse JSON events etc."""
        logger.info('%s: %s', self.name, line)

    def _on_child_exit(self, rc: Optional[int], uptime: float) -> bool:
        """Called after the child exits (not on stop). Return True to DISABLE the
        supervisor (stop the loop) — e.g. a circuit breaker tripped. Default:
        keep restarting (False)."""
        return False

    # ── lifecycle ───────────────────────────────────────────────────────
    def _spawn_thread(self) -> None:
        """Start the supervise thread. Subclass start() calls this after its own
        pre-spawn provisioning/gating."""
        self.thread = threading.Thread(
            target=self._run, daemon=True, name=f'{self.name}-supervisor')
        self.thread.start()

    def is_running(self) -> bool:
        return (self.proc is not None and self.proc.poll() is None
                and self.thread is not None and self.thread.is_alive())

    def stop(self) -> None:
        """Signal the loop to exit and terminate (then kill) the child."""
        self.stop_event.set()
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                except OSError:
                    pass

    def _run(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                cmd, popen_kw = self._build_popen()
                logger.info('%s_supervisor: spawning %s', self.name,
                            ' '.join(str(c) for c in cmd))
                with self.lock:
                    self.proc = subprocess.Popen(cmd, **popen_kw)
                    self.last_started = time.time()
                self._on_started(self.proc)

                if self.proc.stdout is not None:
                    for raw in self.proc.stdout:
                        if self.stop_event.is_set():
                            break
                        line = (raw if isinstance(raw, str)
                                else raw.decode('utf-8', errors='replace')).rstrip()
                        if line:
                            self._format_stdout_line(line)

                rc = self.proc.wait()
                if self.stop_event.is_set():
                    return
                uptime = (time.time() - self.last_started
                          if self.last_started is not None else 0.0)
                self.last_error = f'{self.name} exited rc={rc}'
                logger.warning('%s_supervisor: %s', self.name, self.last_error)

                if (self.reset_backoff_after is not None
                        and uptime > self.reset_backoff_after):
                    backoff = 1.0
                if self._on_child_exit(rc, uptime):
                    return  # subclass tripped a circuit breaker — disable
            except self.fatal_spawn_errors as e:  # type: ignore[misc]
                self.last_error = f'{self.name} fatal spawn error: {e}'
                logger.error('%s_supervisor: %s', self.name, self.last_error)
                return
            except Exception as e:  # noqa: BLE001 — supervisor catches all
                self.last_error = f'spawn failed: {e}'
                logger.error('%s_supervisor: %s', self.name, self.last_error,
                             exc_info=True)

            self.restart_count += 1
            wait = min(backoff, self.backoff_cap)
            backoff = min(backoff * 2.0, self.backoff_cap)
            # Breaker cooldown: honour a one-shot extended wait requested by
            # _on_child_exit (cool-down-and-rearm; see extra_wait_once).
            extra = float(getattr(self, 'extra_wait_once', 0.0) or 0.0)
            if extra > 0.0:
                self.extra_wait_once = 0.0
                wait = max(wait, extra)
            if self.stop_event.wait(wait):
                return
