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

#: Why an engine-outage goal is archived; also what the log says.
_ENGINE_OUTAGE_REASON = (
    'the LLM engine failed a generation (5xx, or a dropped or timed-out '
    'connection); the engine is the LLM watchdog\'s '
    '(service_tools.model_lifecycle), and no source edit restarts it')


def _engine_failed_a_generation(module: str, function: str) -> bool:
    """True for the pattern agent_lightning's wrapper reports when the SERVING
    ENGINE failed a generation after its re-samples:
    report_subsystem_failure('llm', <agent>, exc, 'generate_reply'), which it
    files only for _is_recoverable_generation_failure (an engine 5xx, or a
    dropped or timed-out connection).

    That is the engine's availability.  The LLM watchdog owns it
    (integrations/service_tools/model_lifecycle.py, [LLM-WATCHDOG]), and a
    coding goal cannot restart llama-server, so none is made.  Measured
    2026-10-05 on the owner's desktop: 120 of the 214 self_heal goals ever
    created had this signature, 12 still active; the reporter names the
    agent, so one outage made one goal per agent that hit it, and the coding
    agent spent its turns searching for a source file named after an agent.
    """
    return str(module or '').startswith('llm.') and function == 'generate_reply'


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

        # Housekeeping, so its failure never stops a new goal (review of
        # bef0e1e44): a goal writer that raised made this whole check raise,
        # and both callers log that only at DEBUG.  In a savepoint, so a
        # failure undoes only the sweep's own writes (review of c0c3a5d76):
        # an UPDATE the database refused ('database is locked') failed the
        # flush and left the session needing a rollback, and the next query
        # raised PendingRollbackError.  The sweep is idempotent, so the next
        # check archives what this one could not.
        try:
            with db.begin_nested():
                self._archive_engine_outage_goals(db)
        except Exception:
            logger.warning("Self-heal: the engine-outage archive sweep failed; "
                           "new fix goals are still made", exc_info=True)

        try:
            from hartos.exception_collector import ExceptionCollector
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
            parts = pattern_key.split('::')
            if len(parts) > 2 and _engine_failed_a_generation(parts[1], parts[2]):
                # Handled, not lost: marked so it is not re-read every check.
                collector.mark_pattern_resolved(pattern_key)
                logger.info("Self-heal: no coding goal for %s: %s",
                            pattern_key, _ENGINE_OUTAGE_REASON)
                continue
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

        # Surface the subsystem + identifier that report_subsystem_failure
        # stamped into the record context, so the self-heal goal can route
        # to the right repair tool from the capability matrix.  Without
        # this the goal carried only the 'module' string ('tts.indic_parler')
        # and the "agent picks the right repair tool" promise in the
        # report_subsystem_failure / channel-base / tts-engine docstrings
        # was unwired (self-review H1, 2026-05-29).  Module key is
        # '{subsystem}.{identifier}' so we can also derive them from the
        # split when the context fields are absent (older records).
        _ctx = getattr(sample, 'context', None) or {}
        subsystem = _ctx.get('subsystem')
        identifier = _ctx.get('identifier')
        if not subsystem and '.' in module:
            subsystem, _, identifier = module.partition('.')

        title = f"Fix {exc_type} in {module}.{function}"
        if len(title) > 200:
            title = title[:197] + '...'

        # Collect unique error messages for context
        unique_messages = list(dict.fromkeys(r.exc_message for r in records[:5]))
        sample_traceback = records[-1].traceback_str[:2000]

        _subsystem_line = (
            f"Subsystem: {subsystem} (identifier: {identifier})\n"
            if subsystem else ""
        )
        description = (
            f"Recurring exception detected ({len(records)} occurrences in last hour).\n\n"
            f"Exception: {exc_type}\n"
            f"Module: {module}\n"
            f"Function: {function}\n"
            f"{_subsystem_line}"
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
            # Capability-matrix routing inputs (self-review H1).  The
            # repair agent reads config['subsystem'] to pick the right
            # tool: 'tts'/'vlm'/'llm' → reinstall/venv-repair,
            # 'channels' → reconnect/credential-refresh, 'daemon' →
            # supervisor restart, 'tool' → re-register.  None when the
            # exception came from a generic record_exception (not via
            # report_subsystem_failure).
            'subsystem': subsystem,
            'identifier': identifier,
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

    def _archive_engine_outage_goals(self, db: Session) -> int:
        """Archive self_heal goals made from an engine outage before
        _engine_failed_a_generation kept them from being made.  Through
        GoalManager, the one writer of goal status; idempotent, so it is
        simply asked every check.  Returns how many were archived."""
        try:
            from integrations.social.models import AgentGoal
            from .goal_manager import GoalManager
        except ImportError:
            return 0
        archived = 0
        for goal in db.query(AgentGoal).filter(
                AgentGoal.goal_type == 'self_heal',
                AgentGoal.status.in_(('active', 'paused'))).all():
            config = dict(goal.config_json or {})
            if not _engine_failed_a_generation(config.get('source_module'),
                                               config.get('source_function')):
                continue
            config['archived_reason'] = _ENGINE_OUTAGE_REASON
            GoalManager.update_goal(db, goal.id, config_json=config)
            GoalManager.update_goal_status(db, goal.id, 'archived')
            archived += 1
        if archived:
            logger.info("Self-heal: archived %d goal(s): %s",
                        archived, _ENGINE_OUTAGE_REASON)
        return archived

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
