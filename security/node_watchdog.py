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
LLM_CALL_TIMEOUT_SECONDS = int(os.environ.get('HEVOLVE_LLM_CALL_TIMEOUT', '300'))


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
        return {
            'watchdog': 'healthy' if self._running else 'stopped',
            'uptime_seconds': uptime,
            'threads': threads,
            'restart_log': list(self._restart_log[-20:]),  # last 20 events
        }

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

        # Restart outside the lock to avoid deadlocks
        for name in to_restart:
            self._restart_thread(name)

    def _restart_thread(self, name: str) -> bool:
        """Stop and restart a frozen thread. Returns True on success."""
        with self._lock:
            info = self._threads.get(name)
            if not info:
                return False
            if info.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                info.status = 'dead'
                logger.critical(
                    f"Watchdog: thread '{name}' marked DEAD after "
                    f"{MAX_CONSECUTIVE_FAILURES} consecutive restart failures")
                return False
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
            logger.critical(
                f"Watchdog: thread '{name}' RESTARTED successfully "
                f"(total restarts: {info.restart_count})")
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
    """Create and return the global watchdog instance."""
    global _watchdog
    _watchdog = NodeWatchdog(check_interval=check_interval)
    return _watchdog


def get_watchdog() -> Optional[NodeWatchdog]:
    """Get the current watchdog instance (or None)."""
    return _watchdog
