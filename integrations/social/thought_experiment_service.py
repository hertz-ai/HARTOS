"""
Thought Experiment Service — Full lifecycle for constitutional thought experiments.

PROPOSE → DISCUSS → VOTE → EVALUATE → DECIDE → ARCHIVE

Both humans and agents vote. ConstitutionalFilter gates all content.
Core IP experiments require steward approval. Outcomes feed back to
WorldModelBridge for RL-EF learning.

Service Pattern: static methods, db: Session, db.flush() not db.commit().
"""
import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session

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

    # ─── Voting ───

    @staticmethod
    def cast_vote(db: Session, experiment_id: str, voter_id: str,
                  vote_value: int, reasoning: str = '',
                  suggestion: str = '',
                  voter_type: str = 'human',
                  confidence: float = 1.0) -> Optional[Dict]:
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

        # Clamp confidence
        confidence = max(0.0, min(1.0, confidence))
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

        # Award spark for voting
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
        """Request parallel agent evaluation of a thought experiment.

        Creates AgentGoal for multi-agent evaluation dispatch.
        """
        from .models import ThoughtExperiment

        experiment = db.query(ThoughtExperiment).filter_by(
            id=experiment_id).first()
        if not experiment:
            return {'success': False, 'reason': 'not_found'}

        experiment.status = 'evaluating'
        db.flush()

        # Create evaluation goal for agent dispatch
        try:
            from integrations.agent_engine.goal_manager import GoalManager
            from .models import User
            system_user = db.query(User).filter_by(
                username='hevolve_system_agent').first()
            user_id = system_user.id if system_user else 'system'

            goal = GoalManager.create_goal(
                db, user_id=user_id,
                goal_type='thought_experiment',
                title=f'Evaluate: {experiment.title}',
                description=(
                    f'Evaluate thought experiment: {experiment.hypothesis}\n'
                    f'Expected outcome: {experiment.expected_outcome}\n'
                    f'Intent: {experiment.intent_category}\n'
                    f'Provide: score (-2 to +2), confidence (0-1), '
                    f'reasoning, and evidence.'
                ),
                config_json={'experiment_id': experiment_id},
            )
            return {'success': True, 'goal_id': goal.get('id') if goal else None}
        except Exception as e:
            logger.debug(f"Agent evaluation goal creation failed: {e}")
            return {'success': False, 'reason': str(e)}

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

        total_for = 0.0
        total_against = 0.0
        weighted_sum = 0.0
        total_weight = 0.0
        human_votes = 0
        agent_votes = 0
        suggestions = []

        for v in votes:
            if v.voter_type == 'human':
                human_weight = context_rules['human_weight'] if context_rules else 1.0
                weight = human_weight
                human_votes += 1
            else:
                agent_weight = context_rules['agent_weight'] if context_rules else 1.0
                weight = v.confidence * agent_weight
                agent_votes += 1

            weighted_sum += v.vote_value * weight
            total_weight += weight

            if v.vote_value > 0:
                total_for += weight
            elif v.vote_value < 0:
                total_against += weight

            if v.suggestion:
                suggestions.append({
                    'voter_id': v.voter_id,
                    'voter_type': v.voter_type,
                    'suggestion': v.suggestion,
                })

        weighted_score = weighted_sum / total_weight if total_weight > 0 else 0.0
        threshold = context_rules['approval_threshold'] if context_rules else 0.5

        return {
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
            'approval_threshold': threshold,
            'decision_recommendation': (
                'approve' if weighted_score > threshold
                else 'reject' if weighted_score < -threshold
                else 'inconclusive'
            ),
        }

    @staticmethod
    def decide(db: Session, experiment_id: str,
               decision_text: str) -> Optional[Dict]:
        """Record final decision for an experiment.

        Transitions to 'decided' status. Feeds outcome to WorldModelBridge.
        Steward-required contexts block decision until steward has voted.
        """
        from .models import ThoughtExperiment, ExperimentVote

        experiment = db.query(ThoughtExperiment).filter_by(
            id=experiment_id).first()
        if not experiment:
            return None

        # Steward gate: certain contexts require steward vote before decision
        try:
            from .voting_rules import get_voter_rules, classify_decision_context
            exp_dict = experiment.to_dict()
            context = exp_dict.get('decision_context') or \
                classify_decision_context(exp_dict)
            rules = get_voter_rules(context)
            if rules.get('steward_required'):
                steward_voted = db.query(ExperimentVote).filter_by(
                    experiment_id=experiment_id,
                    voter_id='steward',
                ).first()
                if not steward_voted:
                    return {'error': 'steward_vote_required',
                            'context': context,
                            'message': 'Steward must vote before decision on security contexts'}
        except ImportError:
            pass

        tally = ThoughtExperimentService.tally_votes(db, experiment_id)
        experiment.status = 'decided'
        experiment.decision_outcome = decision_text
        experiment.decision_rationale = {
            'tally': tally,
            'agent_evaluations': experiment.agent_evaluations_json or [],
            'decided_at': datetime.utcnow().isoformat(),
        }
        db.flush()

        # Feed to WorldModelBridge (RL-EF)
        try:
            from integrations.agent_engine.world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()
            if bridge:
                bridge.submit_correction({
                    'type': 'thought_experiment_outcome',
                    'experiment_id': experiment_id,
                    'hypothesis': experiment.hypothesis,
                    'outcome': decision_text,
                    'tally': tally,
                })
        except Exception:
            pass

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
    def get_active_experiments(db: Session, status: str = None,
                                limit: int = 50) -> List[Dict]:
        """List experiments filtered by status."""
        from .models import ThoughtExperiment

        query = db.query(ThoughtExperiment)
        if status:
            query = query.filter_by(status=status)
        else:
            query = query.filter(
                ThoughtExperiment.status != 'archived')

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
