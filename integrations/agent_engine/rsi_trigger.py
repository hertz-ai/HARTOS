"""Realtime RSI trigger — usage-stat-driven autoresearch enqueue.

Closes the cadence axis of the recursive self-improvement loop.  The
baseline autoresearch pipeline only fires on a 6-hour cron + admin
button, which misses the realtime feedback that the AutoEconomy /
Hive principle demands: agents should iterate on the live signal
they just received, not on a nightly digest.

Responsibilities — one per class (SRP):
    * UsageSignalTracker — rolling-window counter with cooldown; pure,
      thread-safe, no I/O.  Independently testable.
    * RealtimeRSITrigger — glues the tracker to the EventBus and to a
      caller-injected enqueue callback.  Fires enqueue on its own daemon
      thread so the EventBus dispatch is never blocked.

Feature flag: ``HEVOLVE_RSI_REALTIME=1`` (off by default).  The tracker
always runs because dashboards read its snapshot; the auto-enqueue leg
only wakes when the flag is set.  The promoted candidate still passes
through RSI-1 (ConstitutionalFilter) + RSI-2 (AgentBaselineService
delta) inside ``autoevolve_code_tools.commit_improvement`` — this
module only opens the TRIGGER, not the SAFETY gate.
"""
import logging
import os
import threading
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve.rsi_trigger')


# ── Defaults (tune via kwargs, not by editing) ─────────────────
_DEFAULT_WINDOW_S = 300        # 5 min rolling window
_DEFAULT_THRESHOLD = 3         # N signals in window → trigger
_DEFAULT_COOLDOWN_S = 1800     # 30 min per-key cooldown post-trigger
_MAX_KEYS = 500                # unbounded-dict cap

_DEFAULT_TOPICS: Tuple[str, ...] = (
    'tool.error',
    'tool.failed',
    'goal.failed',
    'agent.failure',
    'benchmark.regression',
)


def _flag_enabled() -> bool:
    return os.environ.get('HEVOLVE_RSI_REALTIME', '').lower() in (
        '1', 'true', 'yes', 'on'
    )


# ── Tracker (pure logic, no I/O) ───────────────────────────────

class UsageSignalTracker:
    """Thread-safe rolling-window failure counter with cooldowns.

    One deque of timestamps per ``(signal_type, key)``.  Timestamps
    older than ``window_s`` are pruned lazily on record/query.  The
    cooldown suppresses re-triggering the same ``(signal, key)`` until
    ``cooldown_s`` has elapsed since ``mark_triggered``.
    """

    def __init__(self, window_s: int = _DEFAULT_WINDOW_S,
                 threshold: int = _DEFAULT_THRESHOLD,
                 cooldown_s: int = _DEFAULT_COOLDOWN_S,
                 max_keys: int = _MAX_KEYS,
                 now_fn: Callable[[], float] = time.time):
        self._window_s = max(1, int(window_s))
        self._threshold = max(1, int(threshold))
        self._cooldown_s = max(0, int(cooldown_s))
        self._max_keys = max(1, int(max_keys))
        self._now = now_fn
        self._lock = threading.Lock()
        self._events: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)
        self._last_trigger: Dict[Tuple[str, str], float] = {}

    def record(self, signal_type: str, key: str,
               ts: Optional[float] = None) -> None:
        if not signal_type or not key:
            return
        t = self._now() if ts is None else float(ts)
        k = (str(signal_type), str(key))
        with self._lock:
            dq = self._events[k]
            dq.append(t)
            self._prune_locked(dq, t)
            self._enforce_cap_locked()

    def should_trigger(self, signal_type: str, key: str) -> bool:
        """True iff threshold crossed AND cooldown has elapsed."""
        t = self._now()
        k = (str(signal_type), str(key))
        with self._lock:
            dq = self._events.get(k)
            if not dq:
                return False
            self._prune_locked(dq, t)
            if len(dq) < self._threshold:
                return False
            last = self._last_trigger.get(k, 0.0)
            return (t - last) >= self._cooldown_s

    def mark_triggered(self, signal_type: str, key: str) -> None:
        with self._lock:
            self._last_trigger[(str(signal_type), str(key))] = self._now()

    def get_snapshot(self) -> Dict[str, Dict]:
        """Non-blocking-friendly view for dashboards."""
        t = self._now()
        with self._lock:
            out: Dict[str, Dict] = {}
            for k, dq in list(self._events.items()):
                self._prune_locked(dq, t)
                out[f'{k[0]}:{k[1]}'] = {
                    'count_in_window': len(dq),
                    'last_ts': dq[-1] if dq else None,
                    'last_triggered': self._last_trigger.get(k),
                }
            return out

    # ── internal (lock-held) ──
    def _prune_locked(self, dq: Deque[float], t: float) -> None:
        cutoff = t - self._window_s
        while dq and dq[0] < cutoff:
            dq.popleft()

    def _enforce_cap_locked(self) -> None:
        if len(self._events) <= self._max_keys:
            return
        empty_keys = [k for k, dq in self._events.items() if not dq]
        for k in empty_keys[: len(self._events) - self._max_keys]:
            self._events.pop(k, None)
            self._last_trigger.pop(k, None)
        if len(self._events) <= self._max_keys:
            return
        ranked = sorted(
            self._events.items(),
            key=lambda kv: (kv[1][-1] if kv[1] else 0.0),
        )
        for k, _ in ranked[: len(self._events) - self._max_keys]:
            self._events.pop(k, None)
            self._last_trigger.pop(k, None)


# ── Glue class (I/O, threading, EventBus) ──────────────────────

class RealtimeRSITrigger:
    """Binds a UsageSignalTracker to the platform EventBus.

    ``enqueue_fn`` is dependency-injected so unit tests don't need the
    autoresearch infrastructure and so callers can route enqueues to a
    different backend (e.g. a goal queue, a distributed task).
    """

    def __init__(self, tracker: Optional[UsageSignalTracker] = None,
                 enqueue_fn: Optional[Callable[[str, str], None]] = None):
        self._tracker = tracker or UsageSignalTracker()
        self._enqueue = enqueue_fn or _default_enqueue_autoresearch
        self._bound = False

    @property
    def tracker(self) -> UsageSignalTracker:
        return self._tracker

    def on_signal(self, signal_type: str, data) -> None:
        """EventBus callback — expects ``data`` dict with ``key`` / ``tool``
        / ``goal_id`` field.  Records the signal unconditionally (for
        dashboards) and only fires the enqueue leg when the feature
        flag is set AND the threshold is crossed AND cooldown elapsed.
        """
        key = _extract_key(data)
        if not key:
            return
        self._tracker.record(signal_type, key)
        if not _flag_enabled():
            return
        if not self._tracker.should_trigger(signal_type, key):
            return
        self._tracker.mark_triggered(signal_type, key)
        threading.Thread(
            target=self._safe_enqueue,
            args=(signal_type, key),
            daemon=True,
            name=f'rsi-enqueue-{key[:20]}',
        ).start()

    def bind_to_bus(self, topics: Optional[List[str]] = None) -> bool:
        """Subscribe to EventBus topics.  Idempotent.  Returns True
        iff the bus was available and subscriptions were added."""
        if self._bound:
            return True
        topics = list(topics) if topics is not None else list(_DEFAULT_TOPICS)
        bus = _get_event_bus()
        if bus is None:
            return False
        for topic in topics:
            try:
                bus.on(topic, lambda data, _t=topic: self.on_signal(_t, data))
            except Exception as e:
                logger.debug('rsi_trigger: failed to bind %s (%s)', topic, e)
        self._bound = True
        logger.info('rsi_trigger: bound to %d EventBus topics', len(topics))
        return True

    # ── internal ──
    def _safe_enqueue(self, signal_type: str, key: str) -> None:
        try:
            self._enqueue(signal_type, key)
            logger.info('rsi_trigger: enqueued autoresearch for %s:%s',
                        signal_type, key)
        except Exception as e:
            logger.warning(
                'rsi_trigger: enqueue failed for %s:%s (%s: %s)',
                signal_type, key, type(e).__name__, e,
            )


# ── Helpers ────────────────────────────────────────────────────

def _extract_key(data) -> str:
    if isinstance(data, dict):
        for field in ('key', 'tool', 'tool_name', 'goal_id', 'benchmark'):
            v = data.get(field)
            if v:
                return str(v)
    elif isinstance(data, str):
        return data
    return ''


def _get_event_bus():
    """Return the platform EventBus if registered, else None."""
    try:
        from core.platform.registry import get_registry
        reg = get_registry()
        if not reg.has('events'):
            return None
        return reg.get('events')
    except Exception as e:
        logger.debug('rsi_trigger: registry unavailable (%s)', e)
        return None


def _default_enqueue_autoresearch(signal_type: str, key: str) -> None:
    """Default enqueue: log intent only.

    A real enqueue needs a ``(repo_path, target_file, run_command)``
    context that can't be derived from every signal payload — the
    orchestrator supplies that context by injecting its own
    ``enqueue_fn`` at construction.  Without injection, the trigger
    arm still records and fires, but the enqueue leg is dark.  This
    is deliberate: we open the trigger first, wire the real enqueue
    once the context-resolution code lands, so a bug in enqueue
    composition never silently bypasses the safety gates.
    """
    logger.info(
        'rsi_trigger: threshold crossed for %s:%s — enqueue intent '
        '(inject enqueue_fn to dispatch)',
        signal_type, key,
    )


# ── Module singleton ───────────────────────────────────────────

_trigger: Optional[RealtimeRSITrigger] = None
_trigger_lock = threading.Lock()


def get_rsi_trigger() -> RealtimeRSITrigger:
    """Return the process-wide RealtimeRSITrigger."""
    global _trigger
    if _trigger is None:
        with _trigger_lock:
            if _trigger is None:
                _trigger = RealtimeRSITrigger()
    return _trigger


def reset_for_tests() -> None:
    """Drop the singleton — for unit tests only."""
    global _trigger
    with _trigger_lock:
        _trigger = None
