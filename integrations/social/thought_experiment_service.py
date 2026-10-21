"""
Thought Experiment Service — Full lifecycle for constitutional thought experiments.

PROPOSE → DISCUSS → VOTE → EVALUATE → DECIDE → ARCHIVE

Both humans and agents vote. ConstitutionalFilter gates all content.
Core IP experiments require steward approval. Outcomes feed back to
WorldModelBridge for RL-EF learning.

Service Pattern: static methods, db: Session, db.flush() not db.commit().
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session

from .models import after_commit
from .voting_rules import WORLD_MODEL_MARKER

logger = logging.getLogger('hevolve_social')

# ─── Constants ───

DISCUSS_DURATION_HOURS = 48
VOTING_DURATION_HOURS = 72
EVALUATION_DURATION_HOURS = 24
VALID_STATUSES = ['proposed', 'discussing', 'voting', 'evaluating', 'decided', 'archived']
VALID_INTENT_CATEGORIES = [
    'community', 'environment', 'education', 'health', 'equity', 'technology',
]
VALID_DECISION_TYPES = ['majority', 'weighted', 'consensus', 'expert_panel']
VOTE_RANGE = (-2, 2)  # Strongly oppose to strongly support
# The world model's own voter account (owner decision, 2026-10-19: agents AND
# the world model vote; quorum and the 2/3 rule unchanged). An agent with no
# owner, marked in its settings (voting_rules.WORLD_MODEL_MARKER), so the
# tally counts it as one identity of its own (WORLD_MODEL_IDENTITY) at agent
# weight; the marker is also how a same-named account that is NOT this one
# is told apart, and it is never dispatched work as a persona.
WORLD_MODEL_VOTER = 'hevolveai_world_model'
# How many open experiments the world model scores per daemon tick: each
# costs two llama-server encodes on the slot chat shares.
WORLD_MODEL_VOTES_PER_TICK = 3
# The marker the voter account is made with; an account under its username
# without it is not the world model's (UserService.machine_account_conflict).
WORLD_MODEL_VOTER_SETTINGS = {WORLD_MODEL_MARKER: True, 'dispatchable': False}
# Per-process count of scores that cast nothing (an abstention, a busy or
# unreachable world model), so an experiment that keeps abstaining sinks
# behind fresh ones instead of blocking them for ever.
_WORLD_MODEL_ATTEMPTS: Dict[str, int] = {}
# Per-process: the refusal reason last logged for each open experiment, so a
# refusal is logged when an experiment's reason CHANGES (an alternating
# reason logs each change), whatever attempts came before it (review of
# 7fa1d4aba: logging on the first attempt only never logged a refusal that
# followed a busy reply).
_WORLD_MODEL_REFUSAL_LOGGED: Dict[str, str] = {}


def _feed_decision_to_world_model(**decision) -> None:
    """After decide() commits: hand the outcome to the world model bridge,
    which queues it off this thread.  A missing or failing bridge never
    costs the decision; it is said."""
    try:
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge)
        bridge = get_world_model_bridge()
        if bridge:
            bridge.ingest_decided_experiment(**decision)
    except Exception as e:
        logger.warning("decided experiment %s: outcome not fed to the world "
                       "model (%s)", decision.get('experiment_id'),
                       type(e).__name__)


class ThoughtExperimentService:
    """Manages constitutional thought experiment lifecycle."""

    # ─── Create ───

    @staticmethod
    def create_experiment(db: Session, creator_id: str, title: str,
                          hypothesis: str, expected_outcome: str = '',
                          intent_category: str = 'technology',
                          decision_type: str = 'weighted',
                          is_core_ip: bool = False,
                          parent_experiment_id: str = None) -> Optional[Dict]:
        """Create a new thought experiment with linked Post.

        Gates through ConstitutionalFilter. Sets initial timeline.
        Returns experiment dict or None if blocked.
        """
        # Constitutional filter gate
        try:
            from security.hive_guardrails import ConstitutionalFilter
            check = ConstitutionalFilter.check_prompt(
                f"{title}: {hypothesis}")
            # check_prompt returns (approved: bool, reason: str)
            approved = check[0] if isinstance(check, tuple) else check.get('approved', True)
            reason = check[1] if isinstance(check, tuple) else check.get('reason', '')
            if not approved:
                logger.info(f"Thought experiment blocked by ConstitutionalFilter: {reason}")
                return None
        except ImportError:
            pass

        if intent_category not in VALID_INTENT_CATEGORIES:
            intent_category = 'technology'
        if decision_type not in VALID_DECISION_TYPES:
            decision_type = 'weighted'

        from .models import ThoughtExperiment, Post

        experiment_id = str(uuid.uuid4())
        now = datetime.utcnow()

        # Create linked Post (visible on feed)
        post = Post(
            author_id=creator_id,
            title=title,
            content=f"**Hypothesis:** {hypothesis}\n\n"
                    f"**Expected Outcome:** {expected_outcome}",
            content_type='thought_experiment',
            is_thought_experiment=True,
            hypothesis=hypothesis,
            expected_outcome=expected_outcome,
            intent_category=intent_category,
        )
        db.add(post)
        db.flush()

        # Create experiment
        experiment = ThoughtExperiment(
            id=experiment_id,
            post_id=post.id,
            creator_id=creator_id,
            title=title,
            hypothesis=hypothesis,
            expected_outcome=expected_outcome,
            intent_category=intent_category,
            status='proposed',
            decision_type=decision_type,
            voting_opens_at=now + timedelta(hours=DISCUSS_DURATION_HOURS),
            voting_closes_at=now + timedelta(
                hours=DISCUSS_DURATION_HOURS + VOTING_DURATION_HOURS),
            evaluation_deadline=now + timedelta(
                hours=DISCUSS_DURATION_HOURS + VOTING_DURATION_HOURS
                + EVALUATION_DURATION_HOURS),
            is_core_ip=is_core_ip,
            parent_experiment_id=parent_experiment_id,
        )
        db.add(experiment)
        db.flush()

        # Award spark for proposing
        try:
            from .resonance_engine import ResonanceService
            ResonanceService.award_action(
                db, creator_id, 'experiment_proposed',
                source_id=experiment_id)
        except Exception:
            pass

        return experiment.to_dict()

    # ─── Lifecycle Advance ───

    @staticmethod
    def advance_status(db: Session, experiment_id: str,
                       target_status: str = None) -> Optional[Dict]:
        """Advance experiment to next lifecycle phase.

        Automatic progression: proposed → discussing → voting → evaluating → decided
        """
        from .models import ThoughtExperiment

        experiment = db.query(ThoughtExperiment).filter_by(
            id=experiment_id).first()
        if not experiment:
            return None

        status_order = VALID_STATUSES
        current_idx = status_order.index(experiment.status) if experiment.status in status_order else 0

        if target_status:
            if target_status not in status_order:
                return None
            target_idx = status_order.index(target_status)
            if target_idx <= current_idx:
                return None  # Can't go backwards
            experiment.status = target_status
        else:
            if current_idx < len(status_order) - 1:
                experiment.status = status_order[current_idx + 1]

        db.flush()
        return experiment.to_dict()

    @staticmethod
    def advance_due_experiments(db: Session, limit: int = 25,
                                now: datetime = None) -> Dict:
        """Advance every experiment whose OWN stamped deadline has passed.

        `create_experiment` writes a full schedule onto every row —
        `voting_opens_at` (+DISCUSS_DURATION_HOURS), `voting_closes_at`
        (+VOTING_DURATION_HOURS), `evaluation_deadline`
        (+EVALUATION_DURATION_HOURS).  Nothing ever read them back.  On the
        local instance all 142 experiments carried a schedule and 132 were
        overdue to open voting, some since 2026-04-06; every one still sat at
        `proposed`.  `cast_vote` refuses anything outside
        ('discussing', 'voting'), so no vote was castable, and `auto_evolve`
        gathers only ('voting', 'evaluating'), so no candidate was ever
        visible.  The whole loop was starved by a driver that was never
        written — the policy was already decided and stored.

        This reads that stored schedule and nothing else.  It invents no
        policy: the durations are the module constants, eligibility stays with
        `voting_rules.check_voter_eligibility` inside `cast_vote`, and the
        approval gates stay in `auto_evolve._rank_by_votes`.

        DELIBERATELY STOPS AT `evaluating`.  `decided` is terminal and carries
        a `decision_rationale`; advancing into it on a timer would manufacture
        an outcome for an experiment nobody voted on — which is what the
        super-majority gate exists to prevent.  Deciding stays with the
        explicit API/tool paths.

        `limit` drains a long backlog gradually instead of moving four months
        of rows in one tick.

        Returns a per-transition count.
        """
        from .models import ThoughtExperiment

        now = now or datetime.utcnow()
        moved = {'discussing': 0, 'voting': 0, 'evaluating': 0}

        # (from-status, deadline column, to-status). `proposed -> discussing`
        # has no stamp of its own because the discussion window opens at
        # creation — voting_opens_at is when it CLOSES.
        transitions = (
            ('proposed', None, 'discussing'),
            ('discussing', ThoughtExperiment.voting_opens_at, 'voting'),
            ('voting', ThoughtExperiment.voting_closes_at, 'evaluating'),
        )

        try:
            from integrations.agent_engine.auto_evolve import is_experiment_paused
        except Exception:
            def is_experiment_paused(_):
                return False

        budget = limit
        for from_status, deadline_col, to_status in transitions:
            if budget <= 0:
                break
            query = db.query(ThoughtExperiment).filter(
                ThoughtExperiment.status == from_status)
            if deadline_col is not None:
                query = query.filter(deadline_col <= now)
            for experiment in query.limit(budget).all():
                if is_experiment_paused(experiment.id):
                    continue
                # Route through advance_status — the ONE writer of this
                # column. Setting experiment.status here instead would be a
                # second transition path that skips its ordering guard, and
                # the two would drift the first time that guard changed.
                if ThoughtExperimentService.advance_status(
                        db, experiment.id, to_status):
                    moved[to_status] += 1
                    budget -= 1

        if any(moved.values()):
            db.flush()
            logger.info(
                "Thought-experiment lifecycle: %d -> discussing, %d -> voting, "
                "%d -> evaluating", moved['discussing'], moved['voting'],
                moved['evaluating'])
        return moved

    # ─── Voting ───

    @staticmethod
    def ensure_world_model_voter(db: Session):
        """The world model's voter account, created once through
        UserService.ensure_system_user: an agent owned by no one, marked so
        the tally counts it as one identity of its own (WORLD_MODEL_IDENTITY),
        marked WORLD_MODEL_VOTER_SETTINGS so it is never handed goals as a
        persona.  A same-named account that is not this one (a human who
        took the handle first, any account without the marker) raises
        ValueError rather than lend the world model's votes to it."""
        from .services import UserService
        return UserService.ensure_system_user(
            db, WORLD_MODEL_VOTER, display_name='HevolveAI World Model',
            user_type='agent', settings=WORLD_MODEL_VOTER_SETTINGS)

    @staticmethod
    def cast_world_model_votes(db: Session, bridge=None,
                               limit: int = WORLD_MODEL_VOTES_PER_TICK) -> int:
        """The world model votes on open experiments (discussing / voting)
        it may vote on and has not voted on yet, at most `limit` per call.

        Owner decision, 2026-10-19: agents AND the world model vote. Measured
        on the owner's node, no real experiment had ever received a vote, so
        auto-evolve could select only test fixtures (hevolveai Master 11.462
        G). hevolveai scores each claim (hypothesis -> expected outcome) by
        how surprised its own predictor is, measured without learning it
        (POST /v1/world_model/score, C329-C329g): +1 when the claim surprises
        it more than its typical error, -1 when it does not. That vote is
        cast through cast_vote like any other, with the measured confidence,
        and earns no spark (a machine vote is not a contribution).

        ORDER (review of 9ca2be81a): every read and every score first, every
        write after. A flushed row holds SQLite's one write lock until the
        caller commits, so a score (an HTTP call of seconds) after a write
        held every other writer off (the outage class models.after_commit
        documents; measured 'database is locked' after 3.36 s).
        NO HEAD-OF-LINE BLOCK: a blank expected outcome is filtered in SQL
        first, then a blank outcome or hypothesis by Python's str.strip()
        (TRIM strips only ASCII spaces);
        an experiment whose context refuses agent votes is never scored; and
        candidates are taken least-attempted first, so one that keeps
        abstaining sinks behind fresh ones.
        A world model that cannot answer now (score_claim returns None:
        busy, unreachable, a 5xx, a reply that is not JSON) ends the round at
        its first such reply, so a down world model costs one call per round,
        not one per experiment.  A claim it cannot score (refused by the
        screen, no cloud consent, blank as sent, the route's 422) is an
        abstention like any other: counted, logged when the experiment's
        reason changes (in this process), and the round goes on.
        TIME: the claim is scored at the experiment's created_at read as UTC
        epoch seconds (SQLite's func.now() is UTC and stored naive; on MySQL
        func.now() is the server's local time, so a central node scores at
        an offset until the column is stored as UTC). A busy or unreachable
        world model, or an abstention (vote 0), casts nothing; a later tick
        asks again. Nothing to score never contacts the world model.
        Returns how many votes were cast.
        """
        import math
        from sqlalchemy import func
        from .models import ExperimentVote, ThoughtExperiment, User
        from .services import UserService
        voter = db.query(User).filter(User.username == WORLD_MODEL_VOTER).first()
        conflict = voter and UserService.machine_account_conflict(
            voter, 'agent', WORLD_MODEL_VOTER_SETTINGS)
        if conflict:
            logger.warning("world-model voter username %r belongs to another "
                           "account (%s); the world model does not vote",
                           WORLD_MODEL_VOTER, conflict)
            return 0
        voted = set() if voter is None else {
            row[0] for row in db.query(ExperimentVote.experiment_id)
            .filter(ExperimentVote.voter_id == voter.id)}
        query = (db.query(ThoughtExperiment)
                 .filter(ThoughtExperiment.expected_outcome.isnot(None))
                 .filter(func.length(func.trim(ThoughtExperiment.expected_outcome)) > 0))
        if voted:
            query = query.filter(~ThoughtExperiment.id.in_(voted))
        # The SQL TRIM strips only ASCII spaces; an outcome of tabs, newlines
        # or U+3000 is just as blank (review of 4c4ba922f).
        open_rows = [e for e in ThoughtExperimentService._open_agent_votable(db, query)
                     if (e.expected_outcome or '').strip()]
        live = {e.id for e in open_rows}
        for per_experiment in (_WORLD_MODEL_ATTEMPTS, _WORLD_MODEL_REFUSAL_LOGGED):
            for stale in [k for k in per_experiment if k not in live]:
                per_experiment.pop(stale, None)
        rows = list(open_rows)
        rows.sort(key=lambda e: (_WORLD_MODEL_ATTEMPTS.get(e.id, 0),
                                 e.created_at or datetime.min))
        rows = rows[:limit]
        if not rows:
            return 0
        if bridge is None:
            from integrations.agent_engine.world_model_bridge import (
                get_world_model_bridge)
            bridge = get_world_model_bridge()
            if bridge is None:
                return 0
        scored = []
        for exp in rows:
            created = exp.created_at
            if created is None:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            score = bridge.score_claim(
                context=exp.hypothesis, outcome=exp.expected_outcome,
                timestamp=created.timestamp(), creator_id=str(exp.creator_id))
            conf = score.get('confidence') if isinstance(score, dict) else None
            if (isinstance(score, dict) and score.get('vote') in (1, -1)
                    and isinstance(conf, (int, float)) and math.isfinite(conf)):
                scored.append((exp, score, float(conf)))
                continue
            _WORLD_MODEL_ATTEMPTS[exp.id] = _WORLD_MODEL_ATTEMPTS.get(exp.id, 0) + 1
            if isinstance(score, dict) and score.get('refused'):
                # bounded at the logger, whatever the source of the reason
                reason = str(score.get('reason'))[:300]
                if _WORLD_MODEL_REFUSAL_LOGGED.get(exp.id) != reason:
                    _WORLD_MODEL_REFUSAL_LOGGED[exp.id] = reason
                    # %r: a reason can carry a world model's text (a 422
                    # detail), and a newline in it must not forge a log line
                    logger.info("world model cannot score experiment %s: %r",
                                exp.id, reason)
            if score is None:
                break
        if not scored:
            return 0
        voter = voter or ThoughtExperimentService.ensure_world_model_voter(db)
        cast = 0
        for exp, score, conf in scored:
            reasoning = ('world model: prediction error %s against its typical %s '
                         '(log surprise %s); +1 means running this would teach it'
                         % (score.get('error'), score.get('typical_error'),
                            score.get('log_surprise')))
            out = ThoughtExperimentService.cast_vote(
                db, exp.id, voter.id, int(score['vote']), reasoning=reasoning,
                voter_type='agent', confidence=conf, award=False)
            if isinstance(out, dict) and out.get('error'):
                logger.info("world-model vote on %s refused: %s", exp.id, out.get('error'))
                _WORLD_MODEL_ATTEMPTS[exp.id] = _WORLD_MODEL_ATTEMPTS.get(exp.id, 0) + 1
                continue
            _WORLD_MODEL_ATTEMPTS.pop(exp.id, None)
            cast += 1
        return cast

    @staticmethod
    def cast_vote(db: Session, experiment_id: str, voter_id: str,
                  vote_value: int, reasoning: str = '',
                  suggestion: str = '',
                  voter_type: str = 'human',
                  confidence: float = 1.0,
                  award: bool = True) -> Optional[Dict]:
        """Cast a vote on a thought experiment.

        Both humans and agents can vote. Agent votes include confidence.
        Vote value: -2 (strongly oppose) to +2 (strongly support).
        """
        from .models import ThoughtExperiment, ExperimentVote

        experiment = db.query(ThoughtExperiment).filter_by(
            id=experiment_id).first()
        if not experiment:
            return None

        # Must be in voting status (or discussing — early votes allowed)
        if experiment.status not in ('discussing', 'voting'):
            return {'error': 'experiment_not_in_voting_phase',
                    'current_status': experiment.status}

        # Context-based voter eligibility check
        try:
            from .voting_rules import check_voter_eligibility
            eligibility = check_voter_eligibility(experiment.to_dict(), voter_type)
            if not eligibility['eligible']:
                return {'error': 'voter_not_eligible',
                        'reason': eligibility['reason'],
                        'context': eligibility['context']}
        except ImportError:
            pass

        # Clamp vote value
        vote_value = max(VOTE_RANGE[0], min(VOTE_RANGE[1], vote_value))

        # Clamp confidence: the ONE clamp (a nan or a non-number is no
        # confidence; max(0, min(1, nan)) was 1.0)
        from integrations.agent_engine.world_model_bridge import _unit_confidence
        confidence = _unit_confidence(confidence)
        if voter_type == 'human':
            confidence = 1.0

        # Constitutional check on reasoning
        constitutional_ok = True
        if reasoning:
            try:
                from security.hive_guardrails import ConstitutionalFilter
                check = ConstitutionalFilter.check_prompt(reasoning)
                constitutional_ok = check[0] if isinstance(check, tuple) else check.get('approved', True)
            except ImportError:
                pass

        # Check for existing vote (upsert)
        existing = db.query(ExperimentVote).filter_by(
            experiment_id=experiment_id,
            voter_id=voter_id,
        ).first()

        created = existing is None
        if existing:
            existing.vote_value = vote_value
            existing.reasoning = reasoning
            existing.suggestion = suggestion
            existing.confidence = confidence
            existing.constitutional_check = constitutional_ok
            vote = existing
        else:
            vote = ExperimentVote(
                experiment_id=experiment_id,
                voter_id=voter_id,
                voter_type=voter_type,
                vote_value=vote_value,
                confidence=confidence,
                reasoning=reasoning,
                suggestion=suggestion,
                constitutional_check=constitutional_ok,
            )
            db.add(vote)
            experiment.total_votes = (experiment.total_votes or 0) + 1

        db.flush()

        # Award spark for voting (not for a machine vote: the world model's
        # votes pass award=False; review of 9ca2be81a measured 60 spark for 6),
        # once per voter and experiment: a changed vote earns nothing more
        # (review of 81c34a0ae: 4 re-votes minted 4 awards).
        if award and created:
            try:
                from .resonance_engine import ResonanceService
                ResonanceService.award_action(
                    db, voter_id, 'experiment_voted',
                    source_id=experiment_id)
                if suggestion:
                    ResonanceService.award_action(
                        db, voter_id, 'experiment_suggestion',
                        source_id=experiment_id)
            except Exception:
                pass

        return vote.to_dict()

    # ─── Agent Evaluation ───

    @staticmethod
    def request_agent_evaluation(db: Session, experiment_id: str) -> Dict:
        """Request agent-native iterative evaluation of a thought experiment.

        Creates an AgentGoal with a type-aware iteration recipe. The agent
        loop (autogen group chat) drives hypothesis→execute→score→iterate
        for ALL experiment types — not just software.

        - software:     uses autoresearch tools (code edit → run → metric)
        - traditional:  uses LLM scoring (propose → evaluate → refine)
        - physical_ai:  uses visual context (hypothesis → observe → measure)
        - research:     uses web search (search → synthesize → score)

        GATED HERE, for every caller.  The experiment must be approved by
        the vote -- voting_rules.approval_verdict, the ONE rule: a quorum
        of distinct identities (>= 3, >= 2 FOR), a FOR share of at least
        max(2/3, the context's threshold) with one vote per identity (an
        owner and all their agents are one), and the steward's FOR where
        the context requires one -- and its lifecycle must be able to reach
        'evaluating' (advance_status: never backwards from decided/archived).
        Measured live 2026-09-25 (AE-7): with the gate only in auto-evolve's
        ranking, one authenticated non-admin POSTed /evaluate on an unvoted
        experiment and started an autonomous agent goal alone.  A refusal
        mutates nothing.
        """
        from .models import ThoughtExperiment
        from .voting_rules import approval_verdict

        experiment = db.query(ThoughtExperiment).filter_by(
            id=experiment_id).first()
        if not experiment:
            return {'success': False, 'reason': 'not_found'}

        tally = ThoughtExperimentService.tally_votes(db, experiment_id)
        verdict = approval_verdict(tally)
        if not verdict['approved']:
            return {'success': False, 'reason': 'not_approved',
                    'verdict': verdict,
                    'distinct_voters': tally.get('distinct_voters'),
                    'distinct_supporters': tally.get('distinct_supporters')}

        # advance_status is the ONE writer of the status column; it refuses
        # to move backwards, so a decided or archived experiment stays closed.
        if experiment.status != 'evaluating' and not \
                ThoughtExperimentService.advance_status(
                    db, experiment_id, 'evaluating'):
            return {'success': False, 'reason': 'invalid_status',
                    'current_status': experiment.status}

        # ONE live evaluation goal per experiment.  A paused goal is not
        # terminal, so an auto-evolve cycle holding one ages out after 6 h
        # and the next cycle asks again; this used to create another goal
        # every time (MEASURED live 2026-09-24: each LiveProbe dispatched at
        # 20:09Z and 02:24Z, six paused goals for three experiments).  The
        # live goal is returned instead, so the caller tracks the goal that
        # already exists.  "Ended" is the same set auto_evolve.reconcile
        # treats as terminal, so a retry after a failure still creates one.
        existing = ThoughtExperimentService._live_evaluation_goal(
            db, experiment_id)
        if existing is not None:
            return {
                'success': True,
                'goal_id': existing.id,
                'reused': True,
                'experiment_type': getattr(
                    experiment, 'experiment_type', 'traditional') or 'traditional',
            }

        exp_type = getattr(experiment, 'experiment_type', 'traditional') or 'traditional'
        recipe = ThoughtExperimentService._build_iteration_recipe(
            experiment, exp_type, config={})

        # Map experiment_type to goal_type so the right tools get loaded
        goal_type_map = {
            'software': 'autoresearch',
            'code_evolution': 'code_evolution',
        }
        goal_type = goal_type_map.get(exp_type, 'thought_experiment')

        # Create evaluation goal for agent dispatch
        try:
            from integrations.agent_engine.goal_manager import GoalManager
            from .services import UserService
            # Ensure the system user exists rather than falling back
            # to the literal 'system' sentinel (which fails any FK
            # constraint downstream).  Posts + experiments all
            # surface to feed readers as authored by "Nunba" — one
            # shared system identity so the flywheel's published
            # content has a coherent voice.  Idempotent + collision-
            # guarded — see UserService.ensure_system_user docstring.
            system_user = UserService.ensure_system_user(
                db, 'nunba', display_name='Nunba',
                bio='The Hevolve hive — autonomous orchestrator '
                    'for benchmarks, experiments, and proof.')
            user_id = system_user.id

            goal = GoalManager.create_goal(
                db,
                goal_type=goal_type,
                title=f'Evaluate: {experiment.title}',
                description=recipe['description'],
                config={
                    'experiment_id': experiment_id,
                    'experiment_type': exp_type,
                    'iteration_recipe': recipe,
                    'autonomous': True,
                },
                created_by=str(user_id),
            )
            return {
                'success': True,
                'goal_id': goal.get('goal', {}).get('id') if goal else None,
                'experiment_type': exp_type,
                'iteration_strategy': recipe['strategy'],
            }
        except Exception as e:
            logger.debug(f"Agent evaluation goal creation failed: {e}")
            return {'success': False, 'reason': str(e)}

    #: Goal statuses after which an evaluation is over -- the same set
    #: auto_evolve.reconcile treats as terminal.  Anything else ('active',
    #: 'paused', ...) is still live.
    _EVALUATION_GOAL_ENDED = frozenset({'completed', 'failed', 'archived'})

    @staticmethod
    def _live_evaluation_goal(db: Session, experiment_id: str):
        """The not-yet-ended evaluation goal for this experiment, or None."""
        from .models import AgentGoal
        goals = db.query(AgentGoal).filter(
            AgentGoal.goal_type.in_(
                ('thought_experiment', 'autoresearch', 'code_evolution')),
            ~AgentGoal.status.in_(
                tuple(ThoughtExperimentService._EVALUATION_GOAL_ENDED)),
        ).all()
        for g in goals:
            cfg = g.config_json or {}
            if isinstance(cfg, str):
                try:
                    cfg = json.loads(cfg or '{}')
                except ValueError:
                    continue
            if isinstance(cfg, dict) and cfg.get('experiment_id') == experiment_id:
                return g
        return None

    @staticmethod
    def _build_iteration_recipe(experiment, exp_type: str, config: dict = None) -> Dict:
        """Build a type-aware iteration recipe for the agent loop.

        The recipe tells the agent HOW to iterate — which tools to use,
        what constitutes improvement, and when to stop. The agent's own
        conversation loop (autogen group chat) drives the iteration,
        not a hardcoded Python while loop.
        """
        base_context = (
            f'Hypothesis: {experiment.hypothesis}\n'
            f'Expected outcome: {experiment.expected_outcome}\n'
            f'Intent: {experiment.intent_category}\n'
        )

        if exp_type == 'code_evolution':
            config = config or {}
            repo_path = config.get('repo_path', '')
            repo_name = config.get('repo_name', '')
            target_files = config.get('target_files', [])
            scope = config.get('scope', 'interfaces')
            return {
                'strategy': 'code_evolution',
                'description': (
                    f'CODE EVOLUTION EXPERIMENT\n\n{base_context}\n'
                    f'REPOSITORY: {repo_name or repo_path or "specified in config"}\n'
                    f'SCOPE: {scope} (agents see signatures, not implementations)\n'
                    f'TARGET FILES: {", ".join(target_files) if target_files else "auto-detected"}\n\n'
                    f'WORKFLOW:\n'
                    f'1. Use the coding tools to edit files in the target repo\n'
                    f'2. The shard engine provides interface-only views for privacy\n'
                    f'3. Validate changes pass tests\n'
                    f'4. Use evaluate_thought_experiment to record findings\n\n'
                    f'TOOLS: coding tools, evaluate_thought_experiment\n\n'
                    f'The repo owner\'s node is the trusted node. '
                    f'Changes are applied locally, then go through the upgrade pipeline.'
                ),
                'tools': [
                    'evaluate_thought_experiment',
                ],
                'max_iterations': 30,
                'scoring': 'metric_extraction',
            }
        elif exp_type == 'software':
            return {
                'strategy': 'autoresearch',
                'description': (
                    f'ITERATIVE SOFTWARE EXPERIMENT\n\n{base_context}\n'
                    f'LOOP PATTERN: Use launch_experiment_autoresearch to start '
                    f'the code iteration loop. Monitor with get_experiment_research_status. '
                    f'When complete, use evaluate_thought_experiment to record findings.\n\n'
                    f'TOOLS: launch_experiment_autoresearch, get_experiment_research_status, '
                    f'evaluate_thought_experiment\n\n'
                    f'The autoresearch engine handles: code edit → run → metric → keep/revert.'
                ),
                'tools': [
                    'launch_experiment_autoresearch',
                    'get_experiment_research_status',
                    'evaluate_thought_experiment',
                ],
                'max_iterations': 50,
                'scoring': 'metric_extraction',
            }
        elif exp_type == 'physical_ai':
            return {
                'strategy': 'observe_and_measure',
                'description': (
                    f'ITERATIVE PHYSICAL AI EXPERIMENT\n\n{base_context}\n'
                    f'LOOP PATTERN:\n'
                    f'1. Use iterate_hypothesis to propose a testable physical hypothesis\n'
                    f'2. Observe via visual context tools (camera feed if available)\n'
                    f'3. Use score_hypothesis_result to evaluate observations\n'
                    f'4. Use get_iteration_history to review what worked\n'
                    f'5. Repeat with refined hypothesis until convergence\n'
                    f'6. Use evaluate_thought_experiment to record final findings\n\n'
                    f'TOOLS: iterate_hypothesis, score_hypothesis_result, '
                    f'get_iteration_history, evaluate_thought_experiment\n\n'
                    f'Score each iteration -2 to +2. Stop when 3 consecutive '
                    f'iterations show no improvement.'
                ),
                'tools': [
                    'iterate_hypothesis', 'score_hypothesis_result',
                    'get_iteration_history', 'evaluate_thought_experiment',
                ],
                'max_iterations': 20,
                'scoring': 'llm_rubric',
            }
        else:
            # traditional, research, or any future type
            return {
                'strategy': 'reason_and_refine',
                'description': (
                    f'ITERATIVE THOUGHT EXPERIMENT\n\n{base_context}\n'
                    f'LOOP PATTERN:\n'
                    f'1. Use iterate_hypothesis to propose a refinement or test angle\n'
                    f'2. Research/reason about the hypothesis (use web search, '
                    f'recall_memory, or domain tools as needed)\n'
                    f'3. Use score_hypothesis_result to evaluate quality against rubric\n'
                    f'4. Use get_iteration_history to see what approaches scored well\n'
                    f'5. Repeat with refined hypothesis until convergence or budget\n'
                    f'6. Use evaluate_thought_experiment to record final evaluation\n\n'
                    f'TOOLS: iterate_hypothesis, score_hypothesis_result, '
                    f'get_iteration_history, evaluate_thought_experiment\n\n'
                    f'SCORING RUBRIC:\n'
                    f'- Evidence quality: is the reasoning backed by data/research?\n'
                    f'- Hypothesis clarity: is it specific and testable?\n'
                    f'- Expected impact: how significant would the outcome be?\n'
                    f'- Feasibility: can this realistically be tested/implemented?\n\n'
                    f'Score each iteration -2 to +2. Stop when 3 consecutive '
                    f'iterations show no improvement or after 10 iterations.'
                ),
                'tools': [
                    'iterate_hypothesis', 'score_hypothesis_result',
                    'get_iteration_history', 'evaluate_thought_experiment',
                ],
                'max_iterations': 10,
                'scoring': 'llm_rubric',
            }

    @staticmethod
    def record_agent_evaluation(db: Session, experiment_id: str,
                                 agent_id: str, score: float,
                                 confidence: float, reasoning: str,
                                 evidence: str = '') -> Optional[Dict]:
        """Record an agent's evaluation result."""
        from .models import ThoughtExperiment

        experiment = db.query(ThoughtExperiment).filter_by(
            id=experiment_id).first()
        if not experiment:
            return None

        evaluations = experiment.agent_evaluations_json or []
        evaluations.append({
            'agent_id': agent_id,
            'score': max(-2.0, min(2.0, score)),
            'confidence': max(0.0, min(1.0, confidence)),
            'reasoning': reasoning,
            'evidence': evidence,
            'evaluated_at': datetime.utcnow().isoformat(),
        })
        experiment.agent_evaluations_json = evaluations
        db.flush()

        # Award spark
        try:
            from .resonance_engine import ResonanceService
            ResonanceService.award_action(
                db, agent_id, 'experiment_evaluated',
                source_id=experiment_id)
        except Exception:
            pass

        return experiment.to_dict()

    # ─── Tally & Decision ───

    @staticmethod
    def tally_votes(db: Session, experiment_id: str) -> Dict:
        """Tally all votes for an experiment.

        Uses context-aware weighting from voting_rules when available.
        Fallback: human=1.0, agent=confidence.
        """
        from .models import ThoughtExperiment, ExperimentVote

        experiment = db.query(ThoughtExperiment).filter_by(
            id=experiment_id).first()
        if not experiment:
            return {'error': 'not_found'}

        # Load context-aware voter rules
        context_rules = None
        decision_context = None
        try:
            from .voting_rules import get_voter_rules, classify_decision_context
            exp_dict = experiment.to_dict()
            decision_context = exp_dict.get('decision_context') or \
                classify_decision_context(exp_dict)
            context_rules = get_voter_rules(decision_context)
        except ImportError:
            pass

        votes = db.query(ExperimentVote).filter_by(
            experiment_id=experiment_id).all()

        # Who each vote REALLY belongs to (voting_rules.voter_identity): a
        # registered user; an agent is the person at the root of its owner
        # chain, or this node when no person owns it.  An unregistered
        # voter_id is a string anyone can pass, so it keeps its weight but is
        # no identity for the quorum.
        from .models import User
        from .voting_rules import (
            is_person, is_steward, one_vote_per_identity, quorum_met, recommendation)
        voter_ids = {v.voter_id for v in votes}
        users = {u.id: u for u in
                 (db.query(User).filter(User.id.in_(voter_ids)).all()
                  if voter_ids else [])}
        identity_of, _node = ThoughtExperimentService.voter_identities(
            db, users.values())
        # Whose vote is the steward's: voting_rules.is_steward, never a
        # voter_id string.
        stewards = {uid for uid, u in users.items() if is_steward(u)}

        human_votes = 0
        agent_votes = 0
        suggestions = []
        steward_vote = None
        ballots = []

        for v in votes:
            if v.voter_id in stewards:
                steward_vote = (v.vote_value if steward_vote is None
                                else min(steward_vote, v.vote_value))
            user = users.get(v.voter_id)
            # Person or not from the ACCOUNT (voting_rules.is_person, the one
            # person test), never the stored voter_type, which the vote route
            # used to take from the request body.  Only an unregistered id
            # (in-process callers) falls back to it.  A non-person (an agent, a
            # system account) weighs as an agent and is never a person's OWN
            # vote: a system account shares the node's identity, which is the
            # owner's, and counted as "own" its one vote cancelled the owner's
            # AGAINST (review of 81c34a0ae, measured end to end).
            is_agent = ((not is_person(user)) if user is not None
                        else v.voter_type != 'human')
            if is_agent:
                agent_weight = context_rules['agent_weight'] if context_rules else 1.0
                weight = v.confidence * agent_weight
                agent_votes += 1
            else:
                weight = context_rules['human_weight'] if context_rules else 1.0
                human_votes += 1
            if user is not None:
                identity, own = identity_of[user.id], not is_agent
            else:
                identity, own = ('unregistered', v.voter_id), True
            ballots.append({'identity': identity, 'own': own,
                            'value': v.vote_value, 'weight': weight})

            if v.suggestion:
                suggestions.append({
                    'voter_id': v.voter_id,
                    'voter_type': 'agent' if is_agent else 'human',
                    'suggestion': v.suggestion,
                })

        # ONE vote per identity (voting_rules.one_vote_per_identity): an
        # owner and all their agents weigh as one in the ratio, as they
        # already counted as one in the quorum.
        total_for = 0.0
        total_against = 0.0
        weighted_sum = 0.0
        total_weight = 0.0
        voters = set()
        supporters = set()
        for identity, (value, weight) in one_vote_per_identity(ballots).items():
            weighted_sum += value * weight
            total_weight += weight
            if value > 0:
                total_for += weight
            elif value < 0:
                total_against += weight
            registered = not (isinstance(identity, tuple)
                              and identity[0] == 'unregistered')
            if registered and weight > 0 and value != 0:
                voters.add(identity)
                if value > 0:
                    supporters.add(identity)

        weighted_score = weighted_sum / total_weight if total_weight > 0 else 0.0
        quorate = quorum_met(len(voters), len(supporters))

        tally = {
            'experiment_id': experiment_id,
            'total_votes': len(votes),
            'human_votes': human_votes,
            'agent_votes': agent_votes,
            'total_for': round(total_for, 2),
            'total_against': round(total_against, 2),
            'weighted_score': round(weighted_score, 4),
            'total_weight': round(total_weight, 2),
            'suggestions': suggestions,
            'decision_context': decision_context,
            'distinct_voters': len(voters),
            'distinct_supporters': len(supporters),
            'quorum_met': quorate,
            'steward_vote': steward_vote,
        }
        # From the ONE approval rule (voting_rules.approval_verdict), never
        # a threshold of its own: this used to say 'approve' on the context
        # threshold while the goal writer approved on 2/3 alone.
        tally['decision_recommendation'] = recommendation(tally)
        return tally

    @staticmethod
    def decide(db: Session, experiment_id: str,
               decision_text: str) -> Optional[Dict]:
        """Record final decision for an experiment.

        Transitions to 'decided' status. Feeds outcome to WorldModelBridge.
        Steward-required contexts block decision until the steward has
        answered FOR or AGAINST (an abstain is no answer).
        """
        from .models import ThoughtExperiment
        from .voting_rules import approval_verdict

        experiment = db.query(ThoughtExperiment).filter_by(
            id=experiment_id).first()
        if not experiment:
            return None

        # Steward gate, from the ONE rule (voting_rules.approval_verdict):
        # who the steward is and whether they answered come from the tally
        # (voting_rules.is_steward), never a voter_id string.  A decision may
        # record either outcome, so a steward AGAINST lets it be decided;
        # only no answer (or an abstain) waits.
        tally = ThoughtExperimentService.tally_votes(db, experiment_id)
        if approval_verdict(tally)['steward_missing']:
            return {'error': 'steward_vote_required',
                    'context': tally.get('decision_context'),
                    'message': 'Steward must vote before decision on security contexts'}

        experiment.status = 'decided'
        experiment.decision_outcome = decision_text
        decided_at = datetime.utcnow().isoformat()
        experiment.decision_rationale = {
            'tally': tally,
            'agent_evaluations': experiment.agent_evaluations_json or [],
            'decided_at': decided_at,
        }
        db.flush()

        # Feed the outcome to the world model ONCE THE DECISION IS DURABLE
        # (models.after_commit): never between this flush and the caller's
        # commit, where the bridge call would hold SQLite's one write lock for
        # its whole timeout, and never for a decision that rolls back.  It
        # used to call submit_correction with ONE dict, which raised TypeError
        # on every decision, so no decided experiment ever reached the world
        # model (hevolveai Master 11.462 H).  Only the claim and the decision
        # leave; the tally's voter ids and suggestions stay here.
        claim = experiment.hypothesis or experiment.title or ''
        if experiment.expected_outcome:
            claim = f'{claim}\nExpected outcome: {experiment.expected_outcome}'
        feed = dict(experiment_id=experiment_id,
                    creator_id=str(experiment.creator_id), claim=claim,
                    decision=decision_text, decided_at=decided_at)
        after_commit(db, lambda: _feed_decision_to_world_model(**feed))

        return experiment.to_dict()

    @staticmethod
    def close_experiment(db: Session, experiment_id: str) -> Optional[Dict]:
        """Archive a decided experiment."""
        from .models import ThoughtExperiment

        experiment = db.query(ThoughtExperiment).filter_by(
            id=experiment_id).first()
        if not experiment:
            return None

        experiment.status = 'archived'
        db.flush()
        return experiment.to_dict()

    # ─── Queries ───

    @staticmethod
    def voter_identities(db: Session, users) -> tuple:
        """({user.id: identity}, this node's identity) for these users rows,
        by voting_rules.voter_identity and node_identity: every owner on
        their chains is loaded one query per chain level (a cycle cannot
        loop: an id is loaded once)."""
        from .models import User
        from .voting_rules import node_identity, voter_identity
        users = list(users)
        rows = {u.id: u for u in users}
        frontier = users
        while frontier:
            need = {u.owner_id for u in frontier
                    if u.owner_id is not None and u.owner_id not in rows}
            if not need:
                break
            found = db.query(User).filter(User.id.in_(need)).all()
            for oid in need:
                rows[oid] = None
            for u in found:
                rows[u.id] = u
            frontier = found

        def owner_of(owner_id):
            if owner_id not in rows:
                rows[owner_id] = db.query(User).filter(User.id == owner_id).first()
            return rows[owner_id]
        node = node_identity(owner_of)
        return {u.id: voter_identity(u, owner_of, node) for u in users}, node

    @staticmethod
    def _open_agent_votable(db: Session, query=None) -> List:
        """The ONE selection of experiments an agent may vote on now, shared
        by the world model's round and the coordinator's ballot: open
        ('discussing' or 'voting'; cast_vote refuses the rest), a hypothesis
        that is not blank, and a decision context agents may vote in
        (voting_rules.check_voter_eligibility, the check cast_vote makes).
        `query` narrows it further (a ThoughtExperiment query)."""
        from .models import ThoughtExperiment
        from .voting_rules import check_voter_eligibility
        q = query if query is not None else db.query(ThoughtExperiment)
        q = q.filter(ThoughtExperiment.status.in_(('discussing', 'voting')))
        return [e for e in q.all()
                if (e.hypothesis or '').strip()
                and check_voter_eligibility(e.to_dict(), 'agent').get('eligible')]

    @staticmethod
    def open_for_agent_votes(db: Session, limit: int) -> List[Dict]:
        """The experiments this node's agents may vote on now: open and
        agent-eligible (_open_agent_votable), and not yet voted on by this
        node (owner ruling 2026-10-21: a node's agents are ONE identity, so
        once one of them voted, another node vote changes nothing and only
        repeats a turn).  Fewest votes first (total_votes), then oldest, so
        every open experiment is reached before any is judged twice.
        Read-only.  Each entry is to_dict()."""
        from .models import ExperimentVote, User
        rows = ThoughtExperimentService._open_agent_votable(db)
        if not rows:
            return []
        votes = (db.query(ExperimentVote.experiment_id, ExperimentVote.voter_id)
                 .filter(ExperimentVote.experiment_id.in_([e.id for e in rows])).all())
        voters = {u.id: u for u in (db.query(User).filter(
            User.id.in_({v for _, v in votes})).all() if votes else [])}
        identity_of, node = ThoughtExperimentService.voter_identities(db, voters.values())
        node_voted = {exp_id for exp_id, voter_id in votes
                      if voter_id in voters and identity_of[voter_id] == node}
        rows = [e for e in rows if e.id not in node_voted]
        rows.sort(key=lambda e: (e.total_votes or 0, e.created_at or datetime.min))
        return [e.to_dict() for e in rows[:max(0, int(limit))]]

    @staticmethod
    def get_active_experiments(db: Session, status: str = None,
                                limit: int = 50,
                                with_votes_only: bool = False) -> List[Dict]:
        """List experiments filtered by status, newest first.

        with_votes_only: only experiments with at least one vote.  This is
        the question auto-evolve's VOTE gate asks -- a zero-vote experiment
        can never pass it -- and without it the newest-`limit` window hid
        every voted row once unvoted rows piled up (measured on a live
        node: the three human-voted experiments sat at rank ~693 of 807).
        """
        from .models import ThoughtExperiment, ExperimentVote

        query = db.query(ThoughtExperiment)
        if status:
            query = query.filter_by(status=status)
        else:
            query = query.filter(
                ThoughtExperiment.status != 'archived')
        if with_votes_only:
            query = query.filter(
                db.query(ExperimentVote.id).filter(
                    ExperimentVote.experiment_id == ThoughtExperiment.id,
                ).exists())

        experiments = query.order_by(
            desc(ThoughtExperiment.created_at)
        ).limit(min(limit, 200)).all()

        return [e.to_dict() for e in experiments]

    @staticmethod
    def get_experiment_detail(db: Session, experiment_id: str) -> Optional[Dict]:
        """Get full experiment with votes and timeline."""
        from .models import ThoughtExperiment, ExperimentVote

        experiment = db.query(ThoughtExperiment).filter_by(
            id=experiment_id).first()
        if not experiment:
            return None

        votes = db.query(ExperimentVote).filter_by(
            experiment_id=experiment_id
        ).order_by(ExperimentVote.created_at).all()

        result = experiment.to_dict()
        result['votes'] = [v.to_dict() for v in votes]
        result['tally'] = ThoughtExperimentService.tally_votes(
            db, experiment_id)
        return result

    @staticmethod
    def get_experiment_votes(db: Session, experiment_id: str) -> List[Dict]:
        """Get all votes for an experiment."""
        from .models import ExperimentVote

        votes = db.query(ExperimentVote).filter_by(
            experiment_id=experiment_id
        ).order_by(ExperimentVote.created_at).all()
        return [v.to_dict() for v in votes]

    @staticmethod
    def get_core_ip_experiments(db: Session) -> List[Dict]:
        """List experiments flagged as core IP."""
        from .models import ThoughtExperiment

        experiments = db.query(ThoughtExperiment).filter_by(
            is_core_ip=True
        ).order_by(desc(ThoughtExperiment.created_at)).all()
        return [e.to_dict() for e in experiments]

    @staticmethod
    def get_experiment_timeline(db: Session, experiment_id: str) -> Optional[Dict]:
        """Get lifecycle timeline for an experiment."""
        from .models import ThoughtExperiment

        experiment = db.query(ThoughtExperiment).filter_by(
            id=experiment_id).first()
        if not experiment:
            return None

        now = datetime.utcnow()
        return {
            'experiment_id': experiment_id,
            'status': experiment.status,
            'created_at': experiment.created_at.isoformat() if experiment.created_at else None,
            'voting_opens_at': experiment.voting_opens_at.isoformat() if experiment.voting_opens_at else None,
            'voting_closes_at': experiment.voting_closes_at.isoformat() if experiment.voting_closes_at else None,
            'evaluation_deadline': experiment.evaluation_deadline.isoformat() if experiment.evaluation_deadline else None,
            'is_voting_open': (
                experiment.voting_opens_at and experiment.voting_closes_at
                and experiment.voting_opens_at <= now <= experiment.voting_closes_at
            ),
            'time_until_voting': (
                (experiment.voting_opens_at - now).total_seconds()
                if experiment.voting_opens_at and now < experiment.voting_opens_at
                else 0
            ),
        }
