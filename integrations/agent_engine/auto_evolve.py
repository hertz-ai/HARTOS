"""
Auto Evolve Orchestrator — Democratic thought experiment → autonomous iteration.

Single entry point for the full evolution loop:
1. GATHER  — collect eligible thought experiments
2. FILTER  — constitutional gate (ConstitutionalFilter)
3. VOTE    — tally democratic votes (human + agent, weighted)
4. SELECT  — top-N experiments by approval score
5. DISPATCH — route each winner to its type-aware iteration loop
6. TRACK   — monitor progress, feed results back to evolution stack

Triggered by:
- Admin "Auto Evolve" button
- Agent tool `start_auto_evolve`
- Scheduled cron (optional)

All iteration is agent-native — the AutoEvolveOrchestrator doesn't run
experiments itself. It selects which experiments DESERVE to run, then
dispatches them through the existing agent goal system.
"""
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve.auto_evolve')


@dataclass
class EvolveSession:
    """Tracks one auto-evolve cycle."""
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = 'pending'  # pending | selecting | dispatching | running | completed | failed
    started_at: float = 0.0
    candidates: int = 0
    filtered: int = 0
    selected: int = 0
    dispatched: int = 0
    completed: int = 0
    failed: int = 0
    experiments: List[Dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            'session_id': self.session_id,
            'status': self.status,
            'elapsed_s': round(time.time() - self.started_at, 1) if self.started_at else 0,
            'candidates': self.candidates,
            'filtered': self.filtered,
            'selected': self.selected,
            'dispatched': self.dispatched,
            'completed': self.completed,
            'failed': self.failed,
            'experiments': self.experiments,
            'errors': self.errors,
        }


class AutoEvolveOrchestrator:
    """Democratic selection + autonomous iteration dispatch.

    The orchestrator doesn't run experiments — it selects which ones
    to run based on constitutional + democratic criteria, then dispatches
    them through the agent goal system for type-aware iteration.
    """

    def __init__(self):
        self._active_session: Optional[EvolveSession] = None
        self._lock = threading.Lock()

    def start(self, max_experiments: int = 5,
              min_approval_score: float = 0.3,
              statuses: List[str] = None,
              user_id: str = 'system') -> Dict:
        """Start an auto-evolve cycle.

        Args:
            max_experiments: Max experiments to dispatch in this cycle
            min_approval_score: Minimum weighted approval score to qualify
            statuses: Which experiment statuses to consider
                      (default: ['voting', 'evaluating'])
            user_id: Who triggered the evolve cycle

        Returns:
            Session info dict
        """
        with self._lock:
            if self._active_session and self._active_session.status == 'running':
                return {
                    'success': False,
                    'reason': 'Auto-evolve cycle already running',
                    'session': self._active_session.to_dict(),
                }

        session = EvolveSession()
        session.started_at = time.time()
        session.status = 'selecting'

        with self._lock:
            self._active_session = session

        # Run in background thread
        def _run():
            try:
                self._execute_cycle(session, max_experiments,
                                    min_approval_score,
                                    statuses or ['voting', 'evaluating'],
                                    user_id)
            except Exception as e:
                session.status = 'failed'
                session.errors.append(str(e))
                logger.exception(f"[{session.session_id}] Auto-evolve failed: {e}")

        t = threading.Thread(target=_run, daemon=True,
                             name=f'auto-evolve-{session.session_id}')
        t.start()

        return {
            'success': True,
            'session_id': session.session_id,
            'status': 'selecting',
        }

    def get_status(self) -> Dict:
        """Get current auto-evolve session status."""
        with self._lock:
            if self._active_session:
                return self._active_session.to_dict()
        return {'status': 'idle', 'message': 'No active auto-evolve session'}

    def _execute_cycle(self, session: EvolveSession,
                       max_experiments: int,
                       min_approval_score: float,
                       statuses: List[str],
                       user_id: str):
        """Execute the full auto-evolve cycle."""

        # Phase 1: GATHER candidates
        candidates = self._gather_candidates(session, statuses)
        session.candidates = len(candidates)
        if not candidates:
            session.status = 'completed'
            session.errors.append('No eligible experiments found')
            self._emit_event('auto_evolve.no_candidates', session.to_dict())
            return

        # Phase 2: FILTER through constitutional gate
        approved = self._constitutional_filter(session, candidates)
        session.filtered = len(approved)

        # Phase 3: VOTE tally + rank
        ranked = self._rank_by_votes(session, approved, min_approval_score)
        session.selected = len(ranked)

        if not ranked:
            session.status = 'completed'
            session.errors.append(
                f'No experiments met approval threshold ({min_approval_score})')
            self._emit_event('auto_evolve.none_approved', session.to_dict())
            return

        # Phase 4: SELECT top-N
        winners = ranked[:max_experiments]

        # Phase 5: DISPATCH to type-aware iteration
        session.status = 'dispatching'
        self._emit_event('auto_evolve.dispatching', {
            'count': len(winners),
            'experiments': [w['id'] for w in winners],
        })

        for exp in winners:
            try:
                goal_result = self._dispatch_experiment(session, exp, user_id)
                session.dispatched += 1
                session.experiments.append({
                    'id': exp['id'],
                    'title': exp['title'],
                    'type': exp.get('experiment_type', 'traditional'),
                    'approval_score': exp.get('_approval_score', 0),
                    'goal_id': goal_result.get('goal_id'),
                    'status': 'dispatched',
                })
            except Exception as e:
                session.failed += 1
                session.errors.append(f"Dispatch {exp['id']}: {e}")
                logger.warning(f"[{session.session_id}] Failed to dispatch "
                               f"{exp['id']}: {e}")

        session.status = 'running' if session.dispatched > 0 else 'failed'
        self._emit_event('auto_evolve.started', session.to_dict())

        logger.info(f"[{session.session_id}] Auto-evolve dispatched "
                     f"{session.dispatched}/{len(winners)} experiments")

    def _gather_candidates(self, session: EvolveSession,
                           statuses: List[str]) -> List[Dict]:
        """Gather eligible thought experiments from DB."""
        try:
            from integrations.social.models import get_db
            from integrations.social.thought_experiment_service import (
                ThoughtExperimentService)

            db = get_db()
            try:
                all_experiments = []
                for status in statuses:
                    exps = ThoughtExperimentService.get_active_experiments(
                        db, status=status, limit=50)
                    all_experiments.extend(exps)
                return all_experiments
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"[{session.session_id}] Gather failed: {e}")
            return []

    def _constitutional_filter(self, session: EvolveSession,
                                candidates: List[Dict]) -> List[Dict]:
        """Filter candidates through ConstitutionalFilter."""
        approved = []
        for exp in candidates:
            try:
                from security.hive_guardrails import ConstitutionalFilter
                text = f"{exp.get('title', '')}: {exp.get('hypothesis', '')}"
                check = ConstitutionalFilter.check_prompt(text)
                ok = check[0] if isinstance(check, tuple) else check.get('approved', True)
                if ok:
                    approved.append(exp)
                else:
                    logger.debug(f"[{session.session_id}] Filtered out: {exp.get('id')}")
            except ImportError:
                # No filter available — pass through
                approved.append(exp)
        return approved

    def _rank_by_votes(self, session: EvolveSession,
                       candidates: List[Dict],
                       min_score: float) -> List[Dict]:
        """Tally votes and rank by approval score."""
        scored = []
        try:
            from integrations.social.models import get_db
            from integrations.social.thought_experiment_service import (
                ThoughtExperimentService)

            db = get_db()
            try:
                for exp in candidates:
                    tally = ThoughtExperimentService.tally_votes(
                        db, exp['id'])
                    score = tally.get('weighted_score', 0)
                    exp['_approval_score'] = score
                    exp['_tally'] = tally
                    if score >= min_score:
                        scored.append(exp)
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"[{session.session_id}] Vote tally failed: {e}")
            return candidates  # Fall through unranked

        # Sort by approval score descending
        scored.sort(key=lambda e: e.get('_approval_score', 0), reverse=True)
        return scored

    def _dispatch_experiment(self, session: EvolveSession,
                             exp: Dict, user_id: str) -> Dict:
        """Dispatch a winning experiment to its type-aware iteration loop.

        Uses ThoughtExperimentService.request_agent_evaluation() which
        creates an agent goal with the type-aware iteration recipe.
        """
        from integrations.social.models import get_db
        from integrations.social.thought_experiment_service import (
            ThoughtExperimentService)

        db = get_db()
        try:
            result = ThoughtExperimentService.request_agent_evaluation(
                db, exp['id'])
            if result.get('success'):
                db.commit()
            return result
        finally:
            db.close()

    def _emit_event(self, topic: str, data: Dict):
        """Emit progress event via EventBus."""
        try:
            from core.platform.events import emit_event
            emit_event(topic, data)
        except Exception:
            pass


# ── Singleton ────────────────────────────────────────────────

_orchestrator: Optional[AutoEvolveOrchestrator] = None
_lock = threading.Lock()


def get_auto_evolve_orchestrator() -> AutoEvolveOrchestrator:
    """Get or create the singleton AutoEvolveOrchestrator."""
    global _orchestrator
    if _orchestrator is None:
        with _lock:
            if _orchestrator is None:
                _orchestrator = AutoEvolveOrchestrator()
    return _orchestrator


# ── Owner Pause/Resume ───────────────────────────────────────

# Paused experiment IDs — owner can pause their experiment's iteration
_paused_experiments: Dict[str, str] = {}  # experiment_id → paused_by_user_id
_pause_lock = threading.Lock()


def pause_experiment_evolution(experiment_id: str, user_id: str) -> Dict:
    """Pause a running experiment's iteration (owner only).

    The experiment stays in 'evaluating' status but the agent goal
    is signalled to stop iterating.
    """
    # Verify ownership
    try:
        from integrations.social.models import get_db
        from integrations.social.thought_experiment_service import ThoughtExperimentService
        db = get_db()
        try:
            detail = ThoughtExperimentService.get_experiment_detail(
                db, experiment_id)
            if not detail:
                return {'success': False, 'reason': 'not_found'}
            if detail.get('creator_id') != user_id:
                return {'success': False, 'reason': 'not_owner',
                        'message': 'Only the experiment creator can pause it'}
        finally:
            db.close()
    except Exception as e:
        return {'success': False, 'reason': str(e)}

    with _pause_lock:
        _paused_experiments[experiment_id] = user_id

    logger.info(f"Experiment {experiment_id} paused by {user_id}")
    return {'success': True, 'experiment_id': experiment_id, 'status': 'paused'}


def resume_experiment_evolution(experiment_id: str, user_id: str) -> Dict:
    """Resume a paused experiment's iteration (owner only)."""
    with _pause_lock:
        paused_by = _paused_experiments.get(experiment_id)
        if not paused_by:
            return {'success': False, 'reason': 'not_paused'}
        if paused_by != user_id:
            return {'success': False, 'reason': 'not_owner',
                    'message': 'Only the user who paused can resume'}
        del _paused_experiments[experiment_id]

    logger.info(f"Experiment {experiment_id} resumed by {user_id}")
    return {'success': True, 'experiment_id': experiment_id, 'status': 'resumed'}


def is_experiment_paused(experiment_id: str) -> bool:
    """Check if an experiment's evolution is paused."""
    with _pause_lock:
        return experiment_id in _paused_experiments


def get_paused_experiments() -> List[str]:
    """Get list of paused experiment IDs."""
    with _pause_lock:
        return list(_paused_experiments.keys())


# ── Agent Tool Functions ─────────────────────────────────────

def start_auto_evolve(max_experiments: int = 5,
                      min_approval_score: float = 0.3,
                      user_id: str = 'system') -> str:
    """Start an auto-evolve cycle: democratically select thought experiments
    and dispatch them to autonomous iteration loops.

    The orchestrator:
    1. Gathers eligible thought experiments (voting/evaluating phase)
    2. Filters through ConstitutionalFilter
    3. Tallies democratic votes (human + agent, weighted)
    4. Selects top-N by approval score
    5. Dispatches each to its type-aware iteration loop

    Software experiments → autoresearch (edit→run→metric→iterate)
    Traditional experiments → reason_and_refine (hypothesize→score→refine)
    Physical AI experiments → observe_and_measure

    Args:
        max_experiments: Max experiments to dispatch (default 5)
        min_approval_score: Minimum weighted vote score to qualify (default 0.3)
        user_id: Who triggered the cycle

    Returns:
        JSON with session_id and status
    """
    orch = get_auto_evolve_orchestrator()
    result = orch.start(
        max_experiments=int(max_experiments),
        min_approval_score=float(min_approval_score),
        user_id=user_id,
    )
    return json.dumps(result)


def get_auto_evolve_status() -> str:
    """Get the status of the current auto-evolve cycle.

    Returns progress including: candidates gathered, filtered, selected,
    dispatched, and per-experiment status.
    """
    orch = get_auto_evolve_orchestrator()
    return json.dumps(orch.get_status())


def pause_evolve_experiment(experiment_id: str, user_id: str) -> str:
    """Pause a running thought experiment's evolution loop.

    Only the experiment creator (owner) can pause their experiment.
    The experiment stays in 'evaluating' status but iteration stops.

    Args:
        experiment_id: The ThoughtExperiment ID to pause
        user_id: ID of the user requesting pause (must be creator)

    Returns:
        JSON with success status
    """
    result = pause_experiment_evolution(experiment_id, user_id)
    return json.dumps(result)


def resume_evolve_experiment(experiment_id: str, user_id: str) -> str:
    """Resume a paused thought experiment's evolution loop.

    Only the user who paused it can resume.

    Args:
        experiment_id: The ThoughtExperiment ID to resume
        user_id: ID of the user requesting resume (must be pauser)

    Returns:
        JSON with success status
    """
    result = resume_experiment_evolution(experiment_id, user_id)
    return json.dumps(result)


# Tool registration for ServiceToolRegistry
AUTO_EVOLVE_TOOLS = [
    {
        'name': 'start_auto_evolve',
        'func': start_auto_evolve,
        'description': (
            'Start democratic auto-evolve cycle: gather thought experiments, '
            'constitutional filter, vote tally, dispatch winners to '
            'autonomous iteration loops.'
        ),
        'tags': ['auto_evolve', 'thought_experiment'],
    },
    {
        'name': 'get_auto_evolve_status',
        'func': get_auto_evolve_status,
        'description': 'Get progress of the current auto-evolve cycle.',
        'tags': ['auto_evolve'],
    },
    {
        'name': 'pause_evolve_experiment',
        'func': pause_evolve_experiment,
        'description': (
            'Pause a running thought experiment evolution (owner only). '
            'Stops iteration but keeps evaluating status.'
        ),
        'tags': ['auto_evolve', 'thought_experiment'],
    },
    {
        'name': 'resume_evolve_experiment',
        'func': resume_evolve_experiment,
        'description': 'Resume a paused thought experiment evolution (owner only).',
        'tags': ['auto_evolve', 'thought_experiment'],
    },
]
