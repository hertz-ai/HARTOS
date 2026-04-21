"""
Node Watchdog - Frozen Thread Auto-Detection and Restart

Monitors all background daemon threads via heartbeat protocol.
Detects frozen/crashed threads and auto-restarts them.

Each daemon calls watchdog.heartbeat('name') every loop iteration.
If a heartbeat is older than 2× the expected interval, the thread
is considered frozen and gets auto-restarted.

After 5 consecutive restart failures, the thread is marked 'dead'.
"""
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

logger = logging.getLogger('hevolve_security')

MAX_CONSECUTIVE_FAILURES = 5
# Grace period after registration/restart before monitoring begins.
# Prevents false FROZEN alerts during startup when daemons are still
# initialising and haven't entered their heartbeat loop yet.
STARTUP_GRACE_SECONDS = 60
# LLM calls can take 30-120s on local models. When a thread is marked
# in_llm_call, the frozen threshold is multiplied by this factor instead
# of the normal frozen_multiplier. Prevents restart cascade.
# Autonomous gather_info + recipe() on local LLM routinely takes 5-10 min.
# 300s was too tight — caused watchdog restart loops where each restart
# re-dispatched the same goal, producing duplicate messages.
LLM_CALL_TIMEOUT_SECONDS = int(os.environ.get('HEVOLVE_LLM_CALL_TIMEOUT', '900'))
# Fleet-wide restart budget: hard ceiling on cross-thread restart storms.
# The per-thread budget (3 restarts / 5min → mark dead) still applies first;
# this is a second-line escalation when many different threads are flapping
# in parallel (e.g. cascade failure from a shared resource exhaustion).
# When the ceiling is breached the whole watchdog halts further restarts
# until manual intervention. Prior to this guard an infinite restart loop
# was possible when N threads rotated through fresh per-thread budgets.
FLEET_RESTART_WINDOW_SECONDS = int(
    os.environ.get('HEVOLVE_WATCHDOG_FLEET_WINDOW_S', '60'))
FLEET_RESTART_MAX = int(os.environ.get('HEVOLVE_WATCHDOG_FLEET_MAX', '10'))


@dataclass
class ThreadInfo:
    """Tracked state for a single monitored daemon thread."""
    name: str
    expected_interval: float
    restart_fn: Callable
    stop_fn: Optional[Callable] = None
    last_heartbeat: float = field(default_factory=time.time)
    status: str = 'healthy'  # healthy | frozen | restarting | dead | in_llm_call
    restart_count: int = 0
    last_restart_at: Optional[float] = None
    consecutive_failures: int = 0
    # Track recent restart times to detect rapid-restart loops
    recent_restart_times: list = field(default_factory=list)
    # LLM call awareness: when True, the thread is blocked on a legitimate
    # LLM inference call. Watchdog extends the threshold by LLM_CALL_MULTIPLIER
    # instead of restarting.
    in_llm_call: bool = False
    llm_call_started_at: Optional[float] = None


class NodeWatchdog:
    """Monitors background daemon threads via heartbeat protocol.

    Usage:
        watchdog = NodeWatchdog()
        watchdog.register('gossip', expected_interval=60,
                          restart_fn=gossip.start, stop_fn=gossip.stop)
        watchdog.start()

        # In daemon loops:
        watchdog.heartbeat('gossip')
    """

    def __init__(self, check_interval: int = None, frozen_multiplier: float = 10.0):
        import os
        self._check_interval = check_interval or int(
            os.environ.get('HEVOLVE_WATCHDOG_INTERVAL', '30'))
        self._frozen_multiplier = frozen_multiplier
        self._threads: Dict[str, ThreadInfo] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._restart_log: List[Dict] = []
        self._started_at: Optional[float] = None
        # Fleet-wide restart budget — timestamps (monotonic seconds) of every
        # restart across every thread. Rolling window pruned in _restart_thread.
        # Breach → self._fleet_halted = True; no further restarts until cleared.
        self._fleet_restart_times: List[float] = []
        self._fleet_halted: bool = False

    def register(self, name: str, expected_interval: float,
                 restart_fn: Callable, stop_fn: Callable = None) -> None:
        """Register a daemon thread to be monitored.

        A grace period delays monitoring so startup initialisation doesn't
        trigger false FROZEN alerts before the daemon enters its loop.
        """
        with self._lock:
            self._threads[name] = ThreadInfo(
                name=name,
                expected_interval=expected_interval,
                restart_fn=restart_fn,
                stop_fn=stop_fn,
                # Pretend the last heartbeat is in the future so the grace
                # period must elapse before we consider the thread frozen.
                last_heartbeat=time.time() + STARTUP_GRACE_SECONDS,
            )
        logger.info(f"Watchdog: registered thread '{name}' "
                    f"(interval={expected_interval}s)")

    def unregister(self, name: str) -> None:
        """Remove a thread from monitoring."""
        with self._lock:
            self._threads.pop(name, None)

    def heartbeat(self, name: str) -> None:
        """Called by daemon threads each cycle to signal they are alive."""
        with self._lock:
            info = self._threads.get(name)
            if info:
                info.last_heartbeat = time.time()

    def is_registered(self, name: str) -> bool:
        """Check if a thread name is registered."""
        with self._lock:
            return name in self._threads

    def registered_names(self) -> list:
        """Return list of all registered thread names."""
        with self._lock:
            return list(self._threads.keys())

    def mark_in_llm_call(self, name: str) -> None:
        """Mark a thread as blocked on a legitimate LLM inference call.

        The watchdog will use LLM_CALL_TIMEOUT_SECONDS instead of the
        normal frozen threshold, preventing false restarts during long
        inference calls.
        """
        with self._lock:
            info = self._threads.get(name)
            if info:
                info.in_llm_call = True
                info.llm_call_started_at = time.time()
                info.last_heartbeat = time.time()  # refresh heartbeat

    def clear_llm_call(self, name: str) -> None:
        """Clear the LLM call marker after inference completes."""
        with self._lock:
            info = self._threads.get(name)
            if info:
                info.in_llm_call = False
                info.llm_call_started_at = None

    def sleep_with_heartbeat(
        self,
        name: str,
        total_seconds: float,
        chunk_seconds: float = 10.0,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Sleep for ``total_seconds`` while keeping the daemon's heartbeat fresh.

        Background daemons freeze the watchdog when a single ``time.sleep``
        or ``time.sleep + blocking call`` exceeds ``expected_interval *
        frozen_multiplier`` (default 300s for a 30s interval). The
        exponential-backoff path in agent_daemon / coding_daemon could
        issue ``time.sleep(480)`` on its 4th consecutive failure, which
        aged the heartbeat past the 300s threshold and triggered a
        restart CASCADE — the exact symptom in 2026-04-11 langchain.log
        (9 restarts of auto_discovery, agent_daemon, coding_daemon in a
        single session).

        This helper breaks the sleep into short chunks (default 10s each)
        and calls :meth:`heartbeat` after every chunk, so:

          - A 30s sleep produces 3 heartbeats.
          - A 480s backoff produces 48 heartbeats, none aged past 10s.
          - The watchdog sees the thread as healthy throughout the sleep.

        If ``stop_check`` is provided, the sleep exits early when it
        returns True — this lets callers shut down cleanly without
        waiting for the full duration.

        The helper is a no-op when ``name`` isn't registered (during
        tests or when the watchdog isn't running) so daemons can use it
        unconditionally without guarding on ``get_watchdog() is not None``.

        Args:
            name: Thread name registered via :meth:`register`.
            total_seconds: Total wall-clock time to sleep.
            chunk_seconds: Size of each sleep chunk. Keep this below
                ``expected_interval`` so the heartbeat stays fresh.
                10s is a good default for the standard 30s daemon
                interval (3× headroom against the 300s frozen threshold).
            stop_check: Optional callable that returns True to exit
                the sleep early. Called between chunks.
        """
        if total_seconds <= 0:
            self.heartbeat(name)
            return
        end = time.monotonic() + total_seconds
        # Guarantee at least one heartbeat at the start so callers that
        # were blocked before calling sleep_with_heartbeat reset their age.
        self.heartbeat(name)
        while True:
            if stop_check is not None and stop_check():
                return
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            chunk = min(chunk_seconds, remaining)
            time.sleep(chunk)
            self.heartbeat(name)

    def start(self) -> None:
        """Start the watchdog background thread. Call LAST after all daemons."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._started_at = time.time()
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        logger.info(f"NodeWatchdog started (interval={self._check_interval}s, "
                    f"multiplier={self._frozen_multiplier}x)")

    def stop(self) -> None:
        """Stop the watchdog."""
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    def get_health(self) -> Dict:
        """Return health status of all monitored threads."""
        now = time.time()
        threads = {}
        with self._lock:
            for name, info in self._threads.items():
                age = now - info.last_heartbeat
                threads[name] = {
                    'status': info.status,
                    'last_heartbeat_age_s': round(age, 1),
                    'last_heartbeat_iso': datetime.fromtimestamp(
                        info.last_heartbeat, tz=timezone.utc).isoformat(),
                    'expected_interval': info.expected_interval,
                    'restart_count': info.restart_count,
                    'consecutive_failures': info.consecutive_failures,
                }
                if info.last_restart_at:
                    threads[name]['last_restart_iso'] = datetime.fromtimestamp(
                        info.last_restart_at, tz=timezone.utc).isoformat()

        uptime = round(now - self._started_at, 1) if self._started_at else 0
        with self._lock:
            fleet_recent = len(self._fleet_restart_times)
            fleet_halted = self._fleet_halted
        return {
            'watchdog': 'healthy' if self._running else 'stopped',
            'uptime_seconds': uptime,
            'threads': threads,
            'restart_log': list(self._restart_log[-20:]),  # last 20 events
            'fleet_restarts_in_window': fleet_recent,
            'fleet_restart_window_s': FLEET_RESTART_WINDOW_SECONDS,
            'fleet_restart_ceiling': FLEET_RESTART_MAX,
            'fleet_halted': fleet_halted,
        }

    def clear_fleet_halt(self) -> None:
        """Clear the fleet-wide restart halt flag (manual recovery).

        Call after diagnosing + fixing the cascade failure that triggered
        the halt. Does NOT revive threads marked 'dead' — those still
        require a process restart.
        """
        with self._lock:
            self._fleet_halted = False
            self._fleet_restart_times.clear()
        logger.critical("Watchdog: fleet halt cleared — restarts re-enabled")

    def _check_loop(self) -> None:
        """Background loop: check heartbeats, restart frozen threads."""
        while self._running:
            time.sleep(self._check_interval)
            if not self._running:
                break
            self._check_all()

    def _check_all(self) -> None:
        """Single check pass over all threads."""
        now = time.time()
        to_restart = []
        with self._lock:
            for name, info in self._threads.items():
                if info.status == 'dead':
                    continue
                age = now - info.last_heartbeat
                # Negative age means we're still in the grace period
                if age < 0:
                    continue
                # Use extended timeout when thread is in a legitimate LLM call
                if info.in_llm_call:
                    threshold = LLM_CALL_TIMEOUT_SECONDS
                else:
                    threshold = info.expected_interval * self._frozen_multiplier
                if age > threshold and info.status in ('healthy', 'frozen', 'in_llm_call'):
                    # Detect rapid-restart loop: 3+ restarts in last 5 minutes
                    # means the thread keeps dying — stop restarting it
                    recent = [t for t in info.recent_restart_times
                              if now - t < 300]
                    if len(recent) >= 3:
                        info.status = 'dead'
                        logger.warning(
                            f"Watchdog: thread '{name}' stuck in restart loop "
                            f"({len(recent)} restarts in 5min) — marking dormant. "
                            f"Will not restart again until app is restarted.")
                        continue
                    logger.critical(
                        f"Watchdog: thread '{name}' FROZEN - no heartbeat "
                        f"for {age:.0f}s (threshold: {threshold:.0f}s)")
                    info.status = 'frozen'
                    to_restart.append(name)

        # Best-effort thread-stack dump BEFORE restart — capture the
        # live frame state of the frozen thread so we can diagnose
        # WHY it stalled.  Uses the unified `core.diag.dump_all_thread_stacks`
        # canonical helper (refactor: replaces a 30-line module-lookup +
        # inline-fallback that silently dropped dumps in some bundle layouts).
        # The dump goes to BOTH the logger AND startup_trace.log
        # (flushes immediately, survives GIL-held hangs).
        if to_restart:
            try:
                _dumper = None
                # Preferred: direct import from Nunba's core package (works in
                # dev + most frozen layouts because Nunba's `core/` is on the
                # default sys.path).
                try:
                    from core.diag import dump_all_thread_stacks as _dumper
                except Exception:
                    pass
                # Fallback: builtin published by core.diag at import time
                # (`builtins._nunba_dump_threads`).  Guarantees the dumper is
                # reachable from any frozen-bundle topology where direct
                # import paths break.
                if _dumper is None:
                    import builtins as _b
                    _dumper = getattr(_b, '_nunba_dump_threads', None)
                if _dumper:
                    _dumper(
                        f"NodeWatchdog FROZEN restart: {','.join(to_restart)}",
                    )
                else:
                    # Last-resort fallback if even the builtin is missing
                    # (e.g., HARTOS standalone, no Nunba in process).  Keep
                    # this minimal — the unified path is the supported one.
                    import sys as _sys
                    import traceback as _tb
                    logger.error(
                        "NodeWatchdog: dumping thread stacks before "
                        "restart (core.diag unavailable):",
                    )
                    for _tid, _frame in _sys._current_frames().items():
                        try:
                            _stack = ''.join(_tb.format_stack(_frame))
                            logger.error(
                                f"  Thread id={_tid}:\n{_stack}",
                            )
                        except Exception:
                            pass
            except Exception as _de:
                logger.debug(f"Thread-stack dump failed: {_de}")

        # Restart outside the lock to avoid deadlocks
        for name in to_restart:
            self._restart_thread(name)

    def _restart_thread(self, name: str) -> bool:
        """Stop and restart a frozen thread. Returns True on success."""
        with self._lock:
            info = self._threads.get(name)
            if not info:
                return False
            # Fleet-wide restart budget: hard ceiling across all threads.
            # Prune outside the window, then check against the ceiling.
            now_mono = time.monotonic()
            cutoff = now_mono - FLEET_RESTART_WINDOW_SECONDS
            self._fleet_restart_times = [
                t for t in self._fleet_restart_times if t > cutoff]
            if self._fleet_halted:
                # Already escalated — don't restart anything until manual clear
                return False
            if len(self._fleet_restart_times) >= FLEET_RESTART_MAX:
                self._fleet_halted = True
                info.status = 'dead'
                logger.critical(
                    f"Watchdog: FLEET RESTART BUDGET EXCEEDED — "
                    f"{len(self._fleet_restart_times)} restarts in "
                    f"{FLEET_RESTART_WINDOW_SECONDS}s (ceiling="
                    f"{FLEET_RESTART_MAX}). Halting all further restarts; "
                    f"thread '{name}' marked DEAD. Investigate cascade "
                    f"failure before restarting the process.")
                return False
            if info.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                info.status = 'dead'
                logger.critical(
                    f"Watchdog: thread '{name}' marked DEAD after "
                    f"{MAX_CONSECUTIVE_FAILURES} consecutive restart failures")
                return False
            # Record the restart attempt in the fleet window before dispatch
            self._fleet_restart_times.append(now_mono)
            info.status = 'restarting'
            stop_fn = info.stop_fn
            restart_fn = info.restart_fn

        # Stop the frozen thread
        if stop_fn:
            try:
                stop_fn()
            except Exception as e:
                logger.warning(f"Watchdog: error stopping '{name}': {e}")

        # Restart it
        try:
            restart_fn()
            with self._lock:
                info = self._threads.get(name)
                if info:
                    info.status = 'healthy'
                    # Give the restarted thread a grace period before
                    # monitoring resumes (same as initial registration)
                    info.last_heartbeat = time.time() + STARTUP_GRACE_SECONDS
                    info.restart_count += 1
                    info.last_restart_at = time.time()
                    info.consecutive_failures = 0
                    info.recent_restart_times.append(time.time())
                    # Prune entries older than 5 minutes
                    cutoff = time.time() - 300
                    info.recent_restart_times = [
                        t for t in info.recent_restart_times if t > cutoff]
                    self._restart_log.append({
                        'name': name,
                        'time': datetime.now(timezone.utc).isoformat(),
                        'restart_count': info.restart_count,
                    })
                    # Cap at 100 entries to prevent memory leak on long-running
                    # instances with flapping daemons (SRE audit finding).
                    if len(self._restart_log) > 100:
                        self._restart_log = self._restart_log[-100:]
            _count = info.restart_count if info else '?'
            logger.critical(
                f"Watchdog: thread '{name}' RESTARTED successfully "
                f"(total restarts: {_count})")
            return True
        except Exception as e:
            with self._lock:
                info = self._threads.get(name)
                if info:
                    info.consecutive_failures += 1
                    if info.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        info.status = 'dead'
                    else:
                        info.status = 'frozen'
            logger.critical(f"Watchdog: FAILED to restart '{name}': {e}")
            return False


# ─── Module singleton ───

_watchdog: Optional[NodeWatchdog] = None


def start_watchdog(check_interval: int = None) -> NodeWatchdog:
    """Create and return the global watchdog instance.

    Idempotent: if a watchdog already exists and is running, return it.
    Without this guard, a second call (e.g. init_social called from both
    hart_intelligence_entry AND Nunba main.py) replaces the global
    singleton.  The first watchdog's check-loop thread keeps running
    with stale ThreadInfo entries that never receive heartbeats,
    causing it to false-FROZEN every daemon every 300 s — the exact
    6-minute restart cascade seen since 2026-04-11.
    """
    global _watchdog
    if _watchdog is not None:
        return _watchdog
    _watchdog = NodeWatchdog(check_interval=check_interval)
    return _watchdog


def get_watchdog() -> Optional[NodeWatchdog]:
    """Get the current watchdog instance (or None)."""
    return _watchdog
