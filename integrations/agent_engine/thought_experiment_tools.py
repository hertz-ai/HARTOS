"""
Thought Experiment Agent Tools — AutoGen tools for thought experiment coordination.

6 tools for creating, voting, evaluating, and managing thought experiments.
Tier 2 tools (agent_engine context). Same pattern as learning_tools.py.
"""
import json
import logging

logger = logging.getLogger('hevolve_social')


def create_thought_experiment(creator_id: str, title: str,
                               hypothesis: str,
                               expected_outcome: str = '',
                               intent_category: str = 'technology',
                               is_core_ip: bool = False) -> str:
    """Create a new constitutional thought experiment."""
    try:
        from integrations.social.models import get_db
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        db = get_db()
        try:
            result = ThoughtExperimentService.create_experiment(
                db, creator_id, title, hypothesis,
                expected_outcome=expected_outcome,
                intent_category=intent_category,
                is_core_ip=is_core_ip)
            if result:
                db.commit()
                return json.dumps({'success': True, 'experiment': result})
            else:
                return json.dumps({
                    'success': False,
                    'reason': 'Blocked by ConstitutionalFilter or invalid input',
                })
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


def cast_experiment_vote(experiment_id: str, voter_id: str,
                          vote_value: int = 0,
                          reasoning: str = '',
                          suggestion: str = '',
                          voter_type: str = 'agent',
                          confidence: float = 0.8) -> str:
    """Cast a vote on a thought experiment (as agent or human)."""
    try:
        from integrations.social.models import get_db
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        db = get_db()
        try:
            result = ThoughtExperimentService.cast_vote(
                db, experiment_id, voter_id,
                vote_value=int(vote_value),
                reasoning=reasoning,
                suggestion=suggestion,
                voter_type=voter_type,
                confidence=float(confidence))
            if result:
                db.commit()
                return json.dumps({'success': True, 'vote': result})
            else:
                return json.dumps({
                    'success': False,
                    'reason': 'Experiment not found or not in voting phase',
                })
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


def evaluate_thought_experiment(experiment_id: str, agent_id: str,
                                  score: float = 0.0,
                                  confidence: float = 0.8,
                                  reasoning: str = '',
                                  evidence: str = '') -> str:
    """Record an agent evaluation for a thought experiment."""
    try:
        from integrations.social.models import get_db
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        db = get_db()
        try:
            result = ThoughtExperimentService.record_agent_evaluation(
                db, experiment_id, agent_id,
                score=float(score),
                confidence=float(confidence),
                reasoning=reasoning,
                evidence=evidence)
            if result:
                db.commit()
                return json.dumps({'success': True, 'experiment': result})
            else:
                return json.dumps({'success': False, 'reason': 'not_found'})
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


def get_experiment_status(experiment_id: str = '',
                           status_filter: str = '') -> str:
    """Get experiment detail or list experiments by status."""
    try:
        from integrations.social.models import get_db
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        db = get_db()
        try:
            if experiment_id:
                result = ThoughtExperimentService.get_experiment_detail(
                    db, experiment_id)
                return json.dumps({'success': True, 'experiment': result})
            else:
                results = ThoughtExperimentService.get_active_experiments(
                    db, status=status_filter or None)
                return json.dumps({
                    'success': True,
                    'experiments': results,
                    'count': len(results),
                })
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


def tally_experiment_votes(experiment_id: str) -> str:
    """Get the current vote tally for an experiment."""
    try:
        from integrations.social.models import get_db
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        db = get_db()
        try:
            tally = ThoughtExperimentService.tally_votes(db, experiment_id)
            return json.dumps({'success': True, 'tally': tally})
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


def advance_experiment(experiment_id: str,
                        target_status: str = '') -> str:
    """Advance experiment to next lifecycle phase or specific status."""
    try:
        from integrations.social.models import get_db
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        db = get_db()
        try:
            result = ThoughtExperimentService.advance_status(
                db, experiment_id,
                target_status=target_status or None)
            if result:
                db.commit()
                return json.dumps({'success': True, 'experiment': result})
            else:
                return json.dumps({
                    'success': False,
                    'reason': 'Cannot advance (invalid status or not found)',
                })
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


# ─── Tool Registration ───

THOUGHT_EXPERIMENT_TOOLS = [
    {
        'name': 'create_thought_experiment',
        'func': create_thought_experiment,
        'description': 'Create a new constitutional thought experiment',
        'tags': ['thought_experiment'],
    },
    {
        'name': 'cast_experiment_vote',
        'func': cast_experiment_vote,
        'description': 'Cast a vote on a thought experiment',
        'tags': ['thought_experiment'],
    },
    {
        'name': 'evaluate_thought_experiment',
        'func': evaluate_thought_experiment,
        'description': 'Record an agent evaluation for a thought experiment',
        'tags': ['thought_experiment'],
    },
    {
        'name': 'get_experiment_status',
        'func': get_experiment_status,
        'description': 'Get experiment detail or list experiments by status',
        'tags': ['thought_experiment'],
    },
    {
        'name': 'tally_experiment_votes',
        'func': tally_experiment_votes,
        'description': 'Get the current vote tally for an experiment',
        'tags': ['thought_experiment'],
    },
    {
        'name': 'advance_experiment',
        'func': advance_experiment,
        'description': 'Advance experiment to next lifecycle phase',
        'tags': ['thought_experiment'],
    },
]
