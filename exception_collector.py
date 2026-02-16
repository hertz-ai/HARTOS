"""
Centralized Exception Collector
================================

Thread-safe singleton that aggregates exceptions from anywhere in the codebase.
Used by SelfHealingDispatcher and ExceptionWatcher to detect recurring patterns
and create automated fix goals.

All recording is fire-and-forget — never raises into the main execution flow.
"""
import time
import uuid
import threading
import traceback
import logging
from collections import defaultdict
from typing import List, Dict, Optional, Callable, Any

logger = logging.getLogger(__name__)


class ExceptionRecord:
    """Single exception occurrence."""

    __slots__ = (
        'id', 'exc_type', 'exc_message', 'module', 'function',
        'lineno', 'traceback_str', 'timestamp', 'user_prompt',
        'action_id', 'context', 'resolved',
    )

    def __init__(self, exc_type: str, exc_message: str, module: str = '',
                 function: str = '', lineno: int = 0, traceback_str: str = '',
                 user_prompt: str = '', action_id: int = 0,
                 context: Optional[Dict] = None):
        self.id = uuid.uuid4().hex[:12]
        self.exc_type = exc_type
        self.exc_message = exc_message
        self.module = module
        self.function = function
        self.lineno = lineno
        self.traceback_str = traceback_str
        self.timestamp = time.time()
        self.user_prompt = user_prompt
        self.action_id = action_id
        self.context = context or {}
        self.resolved = False

    @property
    def pattern_key(self) -> str:
        """Grouping key for deduplication: (type, module, function)."""
        return f"{self.exc_type}::{self.module}::{self.function}"

    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'exc_type': self.exc_type,
            'exc_message': self.exc_message,
            'module': self.module,
            'function': self.function,
            'lineno': self.lineno,
            'traceback_str': self.traceback_str,
            'timestamp': self.timestamp,
            'user_prompt': self.user_prompt,
            'action_id': self.action_id,
            'context': self.context,
            'resolved': self.resolved,
            'pattern_key': self.pattern_key,
        }


class ExceptionCollector:
    """Thread-safe centralized exception aggregation.

    Singleton. Bounded buffer (drops oldest beyond max_buffer).
    Supports subscriber callbacks for real-time notification.
    """

    _instance = None
    _create_lock = threading.Lock()

    def __init__(self):
        self._lock = threading.RLock()
        self._exceptions: List[ExceptionRecord] = []
        self._max_buffer = int(500)
        self._subscribers: List[Callable[[ExceptionRecord], None]] = []

    @classmethod
    def get_instance(cls) -> 'ExceptionCollector':
        if cls._instance is None:
            with cls._create_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset singleton (for testing only)."""
        with cls._create_lock:
            cls._instance = None

    def record(self, exc: BaseException, module: str = '', function: str = '',
               user_prompt: str = '', action_id: int = 0,
               context: Optional[Dict] = None):
        """Record an exception. Called from except blocks.

        Creates ExceptionRecord from the exception + current traceback.
        Appends to buffer (circular — drops oldest beyond max_buffer).
        Notifies all subscribers.
        """
        try:
            tb_str = traceback.format_exc()
            if tb_str == 'NoneType: None\n':
                tb_str = str(exc)

            record = ExceptionRecord(
                exc_type=type(exc).__name__,
                exc_message=str(exc),
                module=module,
                function=function,
                lineno=0,
                traceback_str=tb_str,
                user_prompt=user_prompt,
                action_id=action_id,
                context=context,
            )

            # Extract line number from traceback if available
            tb = getattr(exc, '__traceback__', None)
            if tb is not None:
                while tb.tb_next:
                    tb = tb.tb_next
                record.lineno = tb.tb_lineno

            with self._lock:
                self._exceptions.append(record)
                # Circular buffer: drop oldest
                if len(self._exceptions) > self._max_buffer:
                    self._exceptions = self._exceptions[-self._max_buffer:]

            # Notify subscribers (outside lock)
            for callback in self._subscribers:
                try:
                    callback(record)
                except Exception:
                    pass

        except Exception:
            pass  # never let the collector itself crash

    def subscribe(self, callback: Callable[[ExceptionRecord], None]):
        """Register a callback for real-time exception notification."""
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable):
        """Remove a subscriber."""
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s is not callback]

    def get_unresolved(self, since: float = 0) -> List[ExceptionRecord]:
        """Get exceptions since timestamp that haven't been resolved."""
        with self._lock:
            return [
                r for r in self._exceptions
                if not r.resolved and r.timestamp >= since
            ]

    def get_patterns(self, since: float = 0,
                     min_count: int = 1) -> Dict[str, List[ExceptionRecord]]:
        """Group unresolved exceptions by pattern_key.

        Returns only groups with >= min_count occurrences.
        """
        unresolved = self.get_unresolved(since=since)
        groups: Dict[str, List[ExceptionRecord]] = defaultdict(list)
        for rec in unresolved:
            groups[rec.pattern_key].append(rec)
        return {k: v for k, v in groups.items() if len(v) >= min_count}

    def mark_resolved(self, record_ids: List[str]):
        """Mark exceptions as resolved (fix goal created)."""
        id_set = set(record_ids)
        with self._lock:
            for rec in self._exceptions:
                if rec.id in id_set:
                    rec.resolved = True

    def mark_pattern_resolved(self, pattern_key: str):
        """Mark all exceptions matching a pattern as resolved."""
        with self._lock:
            for rec in self._exceptions:
                if rec.pattern_key == pattern_key:
                    rec.resolved = True

    def get_stats(self) -> Dict[str, Any]:
        """Aggregate stats: count by type, top modules, frequency."""
        with self._lock:
            total = len(self._exceptions)
            unresolved = sum(1 for r in self._exceptions if not r.resolved)
            resolved = total - unresolved

            by_type: Dict[str, int] = defaultdict(int)
            by_module: Dict[str, int] = defaultdict(int)
            for rec in self._exceptions:
                if not rec.resolved:
                    by_type[rec.exc_type] += 1
                    by_module[rec.module] += 1

            top_types = sorted(by_type.items(), key=lambda x: -x[1])[:10]
            top_modules = sorted(by_module.items(), key=lambda x: -x[1])[:10]

            return {
                'total': total,
                'unresolved': unresolved,
                'resolved': resolved,
                'buffer_capacity': self._max_buffer,
                'top_exception_types': top_types,
                'top_modules': top_modules,
            }

    def clear(self):
        """Clear all records (for testing)."""
        with self._lock:
            self._exceptions.clear()


def record_exception(exc: BaseException, module: str = '', function: str = '',
                     user_prompt: str = '', action_id: int = 0, **ctx):
    """Convenience function — fire-and-forget exception recording.

    Safe to call anywhere. Never raises.
    """
    try:
        ExceptionCollector.get_instance().record(
            exc, module=module, function=function,
            user_prompt=user_prompt, action_id=action_id, context=ctx)
    except Exception:
        pass
