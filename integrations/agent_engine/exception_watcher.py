"""
Exception Watcher — Idle Agent Monitor
========================================

Assigns idle (non-participating) agents to watch for exceptions.
When exceptions arrive, watchers classify severity and trigger
the SelfHealingDispatcher for critical/high patterns.

This is the "non-participating coding agent actively watches" feature.
Runs inside AgentDaemon._tick() on the existing periodic schedule.
"""
import time
import logging
import threading
from typing import Dict, List, Optional
from sqlalchemy.orm import Session

logger = logging.getLogger('hevolve_social')

# Severity classification thresholds
CRITICAL_TYPES = frozenset({
    'SystemExit', 'MemoryError', 'RecursionError',
    'DatabaseError', 'OperationalError', 'IntegrityError',
})
HIGH_TYPES = frozenset({
    'KeyError', 'AttributeError', 'TypeError', 'ValueError',
    'IndexError', 'ImportError', 'FileNotFoundError',
    'ConnectionError', 'TimeoutError', 'PermissionError',
})


class ExceptionWatcher:
    """Assigns idle agents as exception watchers.

    When more idle agents exist than active goals, excess agents
    are assigned as watchers. They monitor the ExceptionCollector
    and trigger SelfHealingDispatcher for severe patterns.
    """

    _instance = None
    _create_lock = threading.Lock()

    def __init__(self):
        self._watchers: Dict[str, Dict] = {}  # user_id → watcher info
        self._lock = threading.RLock()
        self._last_process = 0.0

    @classmethod
    def get_instance(cls) -> 'ExceptionWatcher':
        if cls._instance is None:
            with cls._create_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset singleton (for testing)."""
        with cls._create_lock:
            cls._instance = None

    def assign_watcher(self, user_id: str, username: str):
        """Assign an idle agent as an exception watcher."""
        with self._lock:
            if user_id in self._watchers:
                return  # already watching
            self._watchers[user_id] = {
                'user_id': user_id,
                'username': username,
                'assigned_at': time.time(),
                'exceptions_reported': 0,
                'critical_reported': 0,
                'high_reported': 0,
            }
        logger.info(f"Exception watcher assigned: {username} ({user_id})")

    def release_watcher(self, user_id: str):
        """Release a watcher (e.g. when agent gets a real task)."""
        with self._lock:
            removed = self._watchers.pop(user_id, None)
        if removed:
            logger.info(f"Exception watcher released: {removed['username']} "
                        f"(reported {removed['exceptions_reported']} exceptions)")

    def release_all(self):
        """Release all watchers."""
        with self._lock:
            self._watchers.clear()

    def has_watchers(self) -> bool:
        """Check if any watchers are assigned."""
        with self._lock:
            return len(self._watchers) > 0

    def get_watcher_count(self) -> int:
        """Get number of active watchers."""
        with self._lock:
            return len(self._watchers)

    def process_exceptions(self, db: Session) -> int:
        """Process exceptions through watchers.

        Called from AgentDaemon._tick().

        1. Get recent unresolved exceptions from ExceptionCollector
        2. Classify severity (critical/high/low)
        3. For critical/high: trigger SelfHealingDispatcher immediately
           (bypasses the normal min_occurrences threshold for critical)
        4. Update watcher stats

        Returns number of exceptions processed.
        """
        if not self.has_watchers():
            return 0

        try:
            from exception_collector import ExceptionCollector
            collector = ExceptionCollector.get_instance()
        except ImportError:
            return 0

        # Get exceptions since last process
        unresolved = collector.get_unresolved(since=self._last_process)
        self._last_process = time.time()

        if not unresolved:
            return 0

        # Classify severity
        critical = []
        high = []

        for rec in unresolved:
            severity = self._classify_severity(rec)
            if severity == 'critical':
                critical.append(rec)
            elif severity == 'high':
                high.append(rec)

        processed = 0

        # For critical exceptions, trigger fix with lower threshold (1 occurrence)
        if critical:
            try:
                from .self_healing_dispatcher import SelfHealingDispatcher
                dispatcher = SelfHealingDispatcher.get_instance()
                # Temporarily lower threshold for critical
                original_min = dispatcher._min_occurrences
                dispatcher._min_occurrences = 1
                dispatcher._last_check = 0  # force check
                fix_count = dispatcher.check_and_dispatch(db)
                dispatcher._min_occurrences = original_min
                if fix_count > 0:
                    logger.info(f"Watcher triggered {fix_count} critical fix goal(s)")
                processed += len(critical)
            except Exception as e:
                logger.debug(f"Watcher critical dispatch failed: {e}")

        # For high severity, use normal threshold
        if high:
            try:
                from .self_healing_dispatcher import SelfHealingDispatcher
                dispatcher = SelfHealingDispatcher.get_instance()
                dispatcher._last_check = 0  # force check
                fix_count = dispatcher.check_and_dispatch(db)
                if fix_count > 0:
                    logger.info(f"Watcher triggered {fix_count} high-severity fix goal(s)")
                processed += len(high)
            except Exception as e:
                logger.debug(f"Watcher high dispatch failed: {e}")

        # Update watcher stats
        with self._lock:
            for watcher in self._watchers.values():
                watcher['exceptions_reported'] += len(unresolved)
                watcher['critical_reported'] += len(critical)
                watcher['high_reported'] += len(high)

        return processed

    def _classify_severity(self, record) -> str:
        """Classify exception severity.

        Returns: 'critical', 'high', or 'low'
        """
        exc_type = record.exc_type

        if exc_type in CRITICAL_TYPES:
            return 'critical'
        if exc_type in HIGH_TYPES:
            return 'high'

        # Check message for severity hints
        msg_lower = record.exc_message.lower()
        if any(word in msg_lower for word in ('corrupt', 'fatal', 'crash', 'data loss')):
            return 'critical'
        if any(word in msg_lower for word in ('failed', 'missing', 'not found', 'denied')):
            return 'high'

        return 'low'

    def get_watcher_stats(self) -> Dict:
        """Get stats about active watchers and their reports."""
        with self._lock:
            watchers = list(self._watchers.values())
        total_reported = sum(w['exceptions_reported'] for w in watchers)
        total_critical = sum(w['critical_reported'] for w in watchers)

        return {
            'active_watchers': len(watchers),
            'total_exceptions_reported': total_reported,
            'total_critical_reported': total_critical,
            'watchers': [
                {
                    'user_id': w['user_id'],
                    'username': w['username'],
                    'watching_since': w['assigned_at'],
                    'exceptions_reported': w['exceptions_reported'],
                }
                for w in watchers
            ],
        }
