"""
Self-Healing Dispatcher
========================

Periodically reviews collected exceptions and creates coding fix goals.
Runs inside AgentDaemon._tick() on the existing periodic schedule.

Pattern: group exceptions by (type, module, function) → create goals for
patterns with >= min_occurrences, deduplicating against active goals.
"""
import time
import logging
import threading
from typing import Dict, Optional
from sqlalchemy.orm import Session

logger = logging.getLogger('hevolve_social')


class SelfHealingDispatcher:
    """Creates coding fix goals from recurring exception patterns."""

    _instance = None
    _create_lock = threading.Lock()

    def __init__(self):
        self._last_check = 0.0
        self._check_interval = int(300)  # 5 minutes
        self._min_occurrences = int(3)   # require 3+ of same type before creating goal
        self._lock = threading.RLock()

    @classmethod
    def get_instance(cls) -> 'SelfHealingDispatcher':
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

    def check_and_dispatch(self, db: Session) -> int:
        """Check for recurring exception patterns and create fix goals.

        Returns number of fix goals created.
        """
        now = time.time()
        if now - self._last_check < self._check_interval:
            return 0

        with self._lock:
            self._last_check = now

        try:
            from exception_collector import ExceptionCollector
            collector = ExceptionCollector.get_instance()
        except ImportError:
            return 0

        # Get patterns with >= min_occurrences
        patterns = collector.get_patterns(
            since=now - 3600,  # look back 1 hour
            min_count=self._min_occurrences,
        )

        if not patterns:
            return 0

        goals_created = 0
        for pattern_key, records in patterns.items():
            if self._is_already_being_fixed(db, pattern_key):
                continue

            goal_result = self._create_fix_goal(db, pattern_key, records)
            if goal_result and goal_result.get('success'):
                collector.mark_pattern_resolved(pattern_key)
                goals_created += 1
                logger.info(f"Self-heal goal created for pattern: {pattern_key}")

        return goals_created

    def _create_fix_goal(self, db: Session, pattern_key: str,
                         records: list) -> Optional[Dict]:
        """Create a coding goal from an exception pattern."""
        try:
            from .goal_manager import GoalManager
        except ImportError:
            return None

        sample = records[0]
        parts = pattern_key.split('::')
        exc_type = parts[0] if len(parts) > 0 else 'Unknown'
        module = parts[1] if len(parts) > 1 else 'unknown'
        function = parts[2] if len(parts) > 2 else 'unknown'

        title = f"Fix {exc_type} in {module}.{function}"
        if len(title) > 200:
            title = title[:197] + '...'

        # Collect unique error messages for context
        unique_messages = list(dict.fromkeys(r.exc_message for r in records[:5]))
        sample_traceback = records[-1].traceback_str[:2000]

        description = (
            f"Recurring exception detected ({len(records)} occurrences in last hour).\n\n"
            f"Exception: {exc_type}\n"
            f"Module: {module}\n"
            f"Function: {function}\n"
            f"Messages: {'; '.join(unique_messages)}\n\n"
            f"Sample traceback:\n{sample_traceback}\n\n"
            f"Fix the root cause. Do not just add try/except — understand why "
            f"the exception occurs and fix the underlying issue."
        )

        config = {
            'mode': 'self_heal',
            'pattern_key': pattern_key,
            'source_module': module,
            'source_function': function,
            'exc_type': exc_type,
            'occurrence_count': len(records),
            'sample_traceback': sample_traceback,
        }

        return GoalManager.create_goal(
            db,
            goal_type='self_heal',
            title=title,
            description=description,
            config=config,
            spark_budget=100,
            created_by='self_healing_dispatcher',
        )

    def _is_already_being_fixed(self, db: Session, pattern_key: str) -> bool:
        """Check if an active goal already targets this exception pattern."""
        try:
            from integrations.social.models import AgentGoal
        except ImportError:
            return False

        active_goals = db.query(AgentGoal).filter(
            AgentGoal.status == 'active',
            AgentGoal.goal_type == 'self_heal',
        ).all()

        for goal in active_goals:
            config = goal.config_json or {}
            if config.get('pattern_key') == pattern_key:
                return True

        return False
