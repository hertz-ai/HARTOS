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
        """Request agent-native iterative evaluation of a thought experiment.

        Creates an AgentGoal with a type-aware iteration recipe. The agent
        loop (autogen group chat) drives hypothesis→execute→score→iterate
        for ALL experiment types — not just software.

        - software:     uses autoresearch tools (code edit → run → metric)
        - traditional:  uses LLM scoring (propose → evaluate → refine)
        - physical_ai:  uses visual context (hypothesis → observe → measure)
        - research:     uses web search (search → synthesize → score)
        """
        from .models import ThoughtExperiment

        experiment = db.query(ThoughtExperiment).filter_by(
            id=experiment_id).first()
        if not experiment:
            return {'success': False, 'reason': 'not_found'}

        experiment.status = 'evaluating'
        db.flush()

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
            from .models import User
            system_user = db.query(User).filter_by(
                username='hevolve_system_agent').first()
            user_id = system_user.id if system_user else 'system'

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
