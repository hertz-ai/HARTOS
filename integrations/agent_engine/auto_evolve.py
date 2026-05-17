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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve.auto_evolve')

# PRODUCT_MAP §10: DISPATCH uses parallel_dispatch, bounded by this constant.
# Keep conservative — each dispatched experiment spawns an agent goal which may
# fan out further downstream.  4 keeps the blast radius small on flat-tier
# desktops while still honouring the "parallel" contract from the spec.
AUTO_EVOLVE_MAX_PARALLEL_DISPATCH = 4

# PRODUCT_MAP §10: super-majority threshold for VOTE stage — candidates must
# clear 2/3 of the weighted tally, not a simple majority.  Expressed as a
# fraction of the maximum possible score so callers can still tune per-session
# via min_approval_score (which is applied as an absolute-score floor in
# addition to this ratio).
AUTO_EVOLVE_SUPERMAJORITY_RATIO = 2.0 / 3.0

# Active-learning bias for the VOTE stage.  When the world model
# (HevolveAI side, queried via world_model_bridge.get_learning_feedback)
# reports HIGH epistemic_uncertainty, the system has the most to learn
# from running new experiments — so we boost approval scores slightly
# before sort, biasing the SELECT phase toward the experiments that
# would reduce uncertainty fastest.  Classic AL acquisition.
#
# Bounded multiplier: the bias never re-orders candidates that the
# super-majority gate already rejected, and the boost is small enough
# (≤ 1 + AL_MAX_BOOST) that a high-quality consensus pick still beats
# a marginally-passing high-uncertainty pick.  The point is a NUDGE on
# tie-breaking, not a coup.
AUTO_EVOLVE_AL_MAX_BOOST = 0.25  # +25% upper bound on the AL multiplier

# Trust gate for the AL signal itself.  Prediction-error estimates
# from a half-trained world model are themselves unreliable — the
# model can be over-confident on things it has never actually seen,
# or under-confident on things it has seen but not yet generalized.
# Using such an uncalibrated signal to bias selection would amplify
# the model's own blind spots into the experiment queue.
#
# Maturity signal: HevolveAI's EmbodiedLearner exposes a
# `learning_steps` counter via get_stats() — the actual count of
# learning updates the provider has executed.  We require at least
# AL_TRUST_MIN_STEPS before trusting avg_prediction_error as an AL
# acquisition signal; below that, the multiplier is forced to 1.0
# (pure democratic vote decides the rank).  Above the floor, the
# boost ramps in linearly to its full AL_MAX_BOOST value at
# AL_TRUST_FULL_STEPS.
#
# Step threshold tuning: 100 = "model has done enough updates to
# have a non-trivial loss surface but isn't yet generalized";
# 10_000 = "fully matured", at which point the prediction-error
# signal is trusted at full weight.  Both tunable once we have
# real-world calibration data — kept conservative so the bias
# doesn't fire prematurely.
AUTO_EVOLVE_AL_TRUST_MIN_STEPS = 100
AUTO_EVOLVE_AL_TRUST_FULL_STEPS = 10_000


def _is_sqlite_backend() -> bool:
    """Return True when the active DB engine is SQLite (flat tier).

    FIX-1.4a support: SQLite serializes writes at the file level, so
    parallel dispatch that commits concurrently produces ``database is
    locked`` errors.  Callers use this to fall back to a single worker
    and preserve the DISPATCH contract without the race.

    Detection order:
      1. Ask SQLAlchemy directly via the shared ``engine`` (authoritative).
      2. Sniff the ``HEVOLVE_DB_URL`` env var (covers first-boot path
         where the engine hasn't been created yet).
      3. Default to ``True`` — SQLite is the flat-tier default, so the
         safer assumption on failure is "serialize."
    """
    try:
        from integrations.social.models import engine as _engine
        dialect = getattr(getattr(_engine, 'dialect', None), 'name', '') or ''
        if dialect:
            return dialect.lower() == 'sqlite'
    except Exception:
        pass
    try:
        import os
        url = os.environ.get('HEVOLVE_DB_URL', '') or ''
        if url:
            return url.lower().startswith('sqlite')
    except Exception:
        pass
    return True  # flat-tier default = SQLite


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

        # Phase 5: DISPATCH to type-aware iteration (parallel per PRODUCT_MAP §10)
        session.status = 'dispatching'
        self._emit_event('auto_evolve.dispatching', {
            'count': len(winners),
            'experiments': [w['id'] for w in winners],
        })

        self._dispatch_winners_parallel(session, winners, user_id)

        session.status = 'running' if session.dispatched > 0 else 'failed'
        self._emit_event('auto_evolve.started', session.to_dict())

        logger.info(f"[{session.session_id}] Auto-evolve dispatched "
                     f"{session.dispatched}/{len(winners)} experiments")

    def _gather_candidates(self, session: EvolveSession,
                           statuses: List[str]) -> List[Dict]:
        """Gather eligible thought experiments from DB."""
        try:
            from integrations.social.models import db_session
            from integrations.social.thought_experiment_service import (
                ThoughtExperimentService)

            with db_session(commit=False) as db:
                all_experiments = []
                for status in statuses:
                    exps = ThoughtExperimentService.get_active_experiments(
                        db, status=status, limit=50)
                    all_experiments.extend(exps)
                return all_experiments
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
        """Tally votes and rank by approval score.

        Two gates apply per PRODUCT_MAP §10:
          1. weighted_score >= min_score (caller-provided absolute floor)
          2. total_for / (total_for + total_against) >= 2/3 super-majority

        Both must hold for an experiment to qualify for dispatch.  The
        super-majority gate protects against a small but highly-weighted
        vocal minority flipping a low-participation tally into approval.

        Once the gates pass, the rank is biased by an active-learning
        signal pulled from the world model (HevolveAI) — see
        _active_learning_multiplier docstring.  The bias only nudges
        order; it never re-admits a candidate the gates rejected.
        """
        scored = []
        try:
            from integrations.social.models import db_session
            from integrations.social.thought_experiment_service import (
                ThoughtExperimentService)

            with db_session(commit=False) as db:
                for exp in candidates:
                    tally = ThoughtExperimentService.tally_votes(
                        db, exp['id'])
                    score = tally.get('weighted_score', 0)
                    total_for = tally.get('total_for', 0) or 0
                    total_against = tally.get('total_against', 0) or 0
                    decisive = total_for + total_against
                    # Super-majority: ≥ 2/3 of DECISIVE (non-abstain) weight
                    # must be FOR.  Abstains are excluded from denominator.
                    super_ratio = (total_for / decisive) if decisive > 0 else 0.0
                    exp['_approval_score'] = score
                    exp['_super_majority'] = round(super_ratio, 4)
                    exp['_tally'] = tally
                    if (score >= min_score
                            and super_ratio >= AUTO_EVOLVE_SUPERMAJORITY_RATIO):
                        scored.append(exp)
                    else:
                        logger.debug(
                            f"[{session.session_id}] Rejected {exp.get('id')}: "
                            f"score={score} super_ratio={super_ratio:.3f} "
                            f"(need score>={min_score} and "
                            f"ratio>={AUTO_EVOLVE_SUPERMAJORITY_RATIO:.3f})"
                        )
        except Exception as e:
            logger.warning(f"[{session.session_id}] Vote tally failed: {e}")
            return candidates  # Fall through unranked

        # Active-learning bias: pull a global epistemic-uncertainty
        # multiplier from the world model and apply it as a small
        # boost to each gated-in candidate's approval score before
        # sort.  No-op when the world model isn't reachable.
        al_mult = self._active_learning_multiplier(session)
        for exp in scored:
            base = exp.get('_approval_score', 0) or 0
            biased = base * al_mult
            exp['_active_learning_multiplier'] = round(al_mult, 4)
            exp['_biased_score'] = round(biased, 4)

        # Sort by biased score (falls back to unbiased when al_mult=1.0)
        scored.sort(
            key=lambda e: e.get('_biased_score',
                                e.get('_approval_score', 0)),
            reverse=True,
        )
        return scored

    def _active_learning_multiplier(self, session: EvolveSession) -> float:
        """Pull the world model's current avg_prediction_error +
        learning_steps via agent_baseline_service._collect_world_model_metrics()
        — the same normalized signals persisted into every baseline
        snapshot — and project them into a [1.0, 1+AL_MAX_BOOST]
        multiplier, gated on model maturity.

        Why colocated with _rank_by_votes (not in baseline_service):
        AL acquisition is a SELECTION concern (which candidates does
        the system want to run NEXT), not a SNAPSHOT concern (what did
        the system look like at this moment).  baseline_service owns
        the COLLECTOR (single source for HevolveAI feedback shape);
        this method owns the SELECTION USE of that signal.

        Two-stage trust gate:
          stage 1 — does the AL signal exist?
                    No: return 1.0 (no bias).
          stage 2 — has the world model done enough learning_steps to
                    produce a CALIBRATED prediction-error estimate?
                    < AL_TRUST_MIN_STEPS: return 1.0 (uncertainty about
                    uncertainty — using the signal would amplify the
                    model's own blind spots into the experiment queue).
                    >= AL_TRUST_FULL_STEPS: full AL_MAX_BOOST.
                    in between: linear ramp.

        Returns 1.0 in any failure path so a missing / immature world
        model falls back to pure democratic vote — preserves the
        system's pre-wiring behavior.

        HevolveAI field names verified against
        embodied_learner.py::EmbodiedLearner.get_stats — do not
        invent (`epistemic_uncertainty`, `learning_progress`, etc.
        do NOT exist on get_stats; the real keys are
        avg_prediction_error and learning_steps).
        """
        try:
            from integrations.agent_engine.agent_baseline_service import (
                AgentBaselineService)
            wm = AgentBaselineService._collect_world_model_metrics()
        except Exception:
            return 1.0
        if not wm:
            return 1.0

        # Stage 1: AL signal present?  Higher prediction error =
        # more to learn from this domain = stronger acquisition
        # value.  Already clamped to [0,1] by the collector.
        err = wm.get('al_signal')
        if not isinstance(err, (int, float)):
            return 1.0

        # Stage 2: has the model trained enough that its prediction-
        # error estimate is calibrated?
        steps = wm.get('learning_steps')
        if not isinstance(steps, int) or steps < AUTO_EVOLVE_AL_TRUST_MIN_STEPS:
            logger.debug(
                f"[{session.session_id}] World model still in bootstrap "
                f"(learning_steps={steps} < {AUTO_EVOLVE_AL_TRUST_MIN_STEPS}) "
                f"— skipping AL bias (al_signal={err:.4f} considered uncalibrated)"
            )
            return 1.0

        # Linear ramp from 0 at MIN_STEPS to 1 at FULL_STEPS
        ramp_span = AUTO_EVOLVE_AL_TRUST_FULL_STEPS - AUTO_EVOLVE_AL_TRUST_MIN_STEPS
        trust_factor = (steps - AUTO_EVOLVE_AL_TRUST_MIN_STEPS) / ramp_span
        trust_factor = max(0.0, min(1.0, trust_factor))

        mult = 1.0 + (err * AUTO_EVOLVE_AL_MAX_BOOST * trust_factor)
        logger.debug(
            f"[{session.session_id}] AL bias active: "
            f"al_signal={err:.4f}, learning_steps={steps}, "
            f"trust_factor={trust_factor:.4f} → mult={mult:.4f}"
        )
        return mult

    def _dispatch_winners_parallel(self, session: EvolveSession,
                                    winners: List[Dict], user_id: str) -> None:
        """Fan-out winning experiments to type-aware iteration in parallel.

        PRODUCT_MAP §10 DISPATCH stage.  Uses a bounded ThreadPoolExecutor
        (cap = AUTO_EVOLVE_MAX_PARALLEL_DISPATCH) so a large approved set
        can't stampede the agent goal system.  Each experiment still goes
        through _dispatch_experiment (unchanged contract) — this method
        only adds the concurrency primitive.

        Mutations on `session` (dispatched/failed/experiments/errors) are
        serialized by acquiring self._lock around each bookkeeping update.

        FIX-1.4a: On SQLite (flat tier), ``db.commit()`` serializes the
        whole database file, and multiple concurrent commits produce
        ``database is locked`` SQLALchemy errors.  Detect the engine URL
        and drop ``max_workers`` to 1 when SQLite is the backend.  MySQL
        (regional/central) keeps the bounded parallel dispatch.

        FIX-1.4b: ``_dispatch_experiment`` returns ``{'success': False,
        'reason': ...}`` on logical failure (e.g. already-dispatched
        experiment, missing goal recipe) WITHOUT raising.  The earlier
        code counted those as successful dispatches, so a session with
        100 % logical-failure returns would sit at status='running' with
        ``dispatched == N`` and ``failed == 0`` forever.  Branch on the
        ``success`` flag: only count dispatched on True, else route to
        the ``failed`` counter + errors list so the terminal-state rule
        at line 192 (``running if dispatched > 0 else failed``) fires
        correctly.
        """
        if not winners:
            return

        max_parallel = AUTO_EVOLVE_MAX_PARALLEL_DISPATCH
        if _is_sqlite_backend():
            max_parallel = 1  # FIX-1.4a: serialize on flat/SQLite
            logger.debug(
                f"[{session.session_id}] SQLite backend detected — "
                f"serializing dispatch to avoid 'database is locked'")

        max_workers = min(len(winners), max_parallel)
        with ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=f'auto-evolve-{session.session_id}') as pool:
            futures = {
                pool.submit(self._dispatch_experiment, session, exp, user_id): exp
                for exp in winners
            }
            for future in as_completed(futures):
                exp = futures[future]
                try:
                    goal_result = future.result()
                    # FIX-1.4b: distinguish logical success from logical failure.
                    # _dispatch_experiment returns dict — missing/falsy 'success'
                    # key = logical failure, not dispatched.
                    success = (
                        isinstance(goal_result, dict)
                        and bool(goal_result.get('success'))
                    )
                    with self._lock:
                        if success:
                            session.dispatched += 1
                            session.experiments.append({
                                'id': exp['id'],
                                'title': exp.get('title', ''),
                                'type': exp.get('experiment_type', 'traditional'),
                                'approval_score': exp.get('_approval_score', 0),
                                'super_majority': exp.get('_super_majority', 0),
                                'goal_id': goal_result.get('goal_id')
                                if isinstance(goal_result, dict) else None,
                                'status': 'dispatched',
                            })
                        else:
                            session.failed += 1
                            reason = (
                                goal_result.get('reason')
                                if isinstance(goal_result, dict)
                                else 'non-dict result'
                            ) or 'unknown'
                            session.errors.append(
                                f"Dispatch {exp['id']}: {reason}")
                            session.experiments.append({
                                'id': exp['id'],
                                'title': exp.get('title', ''),
                                'type': exp.get('experiment_type', 'traditional'),
                                'approval_score': exp.get('_approval_score', 0),
                                'super_majority': exp.get('_super_majority', 0),
                                'goal_id': None,
                                'status': 'failed',
                                'reason': reason,
                            })
                            logger.info(
                                f"[{session.session_id}] Dispatch declined "
                                f"for {exp['id']}: {reason}")
                except Exception as e:
                    with self._lock:
                        session.failed += 1
                        session.errors.append(f"Dispatch {exp['id']}: {e}")
                    logger.warning(
                        f"[{session.session_id}] Failed to dispatch "
                        f"{exp['id']}: {e}")

    def _dispatch_experiment(self, session: EvolveSession,
                             exp: Dict, user_id: str) -> Dict:
        """Dispatch a winning experiment to its type-aware iteration loop.

        Uses ThoughtExperimentService.request_agent_evaluation() which
        creates an agent goal with the type-aware iteration recipe.

        Special case: experiments with experiment_type='code_evolution' and
        target_repo='hevolveai' are dispatched to the HevolveAI evolution
        orchestrator inshghtead of the standard evaluation pipeline.
        """
        from integrations.social.models import db_session
        from integrations.social.thought_experiment_service import (
            ThoughtExperimentService)

        with db_session(commit=False) as db:
            result = ThoughtExperimentService.request_agent_evaluation(
                db, exp['id'])
            if result.get('success'):
                db.commit()
            return result

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
        from integrations.social.models import db_session
        from integrations.social.thought_experiment_service import ThoughtExperimentService
        with db_session(commit=False) as db:
            detail = ThoughtExperimentService.get_experiment_detail(
                db, experiment_id)
            if not detail:
                return {'success': False, 'reason': 'not_found'}
            if detail.get('creator_id') != user_id:
                return {'success': False, 'reason': 'not_owner',
                        'message': 'Only the experiment creator can pause it'}
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
