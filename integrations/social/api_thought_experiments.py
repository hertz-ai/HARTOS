"""
Thought Experiment API Blueprint — Constitutional thought experiment endpoints.

POST /api/social/experiments              — Create new experiment
GET  /api/social/experiments              — List experiments (filter by status)
GET  /api/social/experiments/<id>         — Get experiment detail
POST /api/social/experiments/<id>/vote    — Cast vote
POST /api/social/experiments/<id>/advance — Advance lifecycle
POST /api/social/experiments/<id>/evaluate — Trigger agent evaluation
POST /api/social/experiments/<id>/decide  — Record decision
GET  /api/social/experiments/<id>/votes   — Get all votes
GET  /api/social/experiments/<id>/timeline — Get lifecycle timeline
GET  /api/social/experiments/core-ip      — List core IP experiments
"""
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger('hevolve_social')

thought_experiments_bp = Blueprint('thought_experiments', __name__)


@thought_experiments_bp.route('/api/social/experiments', methods=['POST'])
def create_experiment():
    """Create a new thought experiment."""
    from .models import get_db
    from .thought_experiment_service import ThoughtExperimentService

    body = request.get_json(silent=True) or {}
    creator_id = body.get('creator_id')
    title = body.get('title', '')
    hypothesis = body.get('hypothesis', '')

    if not creator_id or not title or not hypothesis:
        return jsonify({
            'success': False,
            'error': 'creator_id, title, and hypothesis required',
        }), 400

    db = get_db()
    try:
        result = ThoughtExperimentService.create_experiment(
            db, creator_id, title, hypothesis,
            expected_outcome=body.get('expected_outcome', ''),
            intent_category=body.get('intent_category', 'technology'),
            decision_type=body.get('decision_type', 'weighted'),
            is_core_ip=body.get('is_core_ip', False),
            parent_experiment_id=body.get('parent_experiment_id'),
        )
        if result:
            db.commit()
            return jsonify({'success': True, 'data': result}), 201
        else:
            return jsonify({
                'success': False,
                'error': 'Experiment creation failed (ConstitutionalFilter may have blocked it)',
            }), 403
    except Exception as e:
        db.rollback()
        logger.error(f"Create experiment error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@thought_experiments_bp.route('/api/social/experiments', methods=['GET'])
def list_experiments():
    """List experiments filtered by status."""
    from .models import get_db
    from .thought_experiment_service import ThoughtExperimentService

    status = request.args.get('status')
    limit = request.args.get('limit', 50, type=int)

    db = get_db()
    try:
        experiments = ThoughtExperimentService.get_active_experiments(
            db, status=status, limit=limit)
        return jsonify({'success': True, 'data': experiments}), 200
    except Exception as e:
        logger.error(f"List experiments error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@thought_experiments_bp.route('/api/social/experiments/core-ip', methods=['GET'])
def core_ip_experiments():
    """List experiments flagged as core IP."""
    from .models import get_db
    from .thought_experiment_service import ThoughtExperimentService

    db = get_db()
    try:
        experiments = ThoughtExperimentService.get_core_ip_experiments(db)
        return jsonify({'success': True, 'data': experiments}), 200
    except Exception as e:
        logger.error(f"Core IP experiments error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@thought_experiments_bp.route('/api/social/experiments/<experiment_id>', methods=['GET'])
def get_experiment(experiment_id):
    """Get experiment detail with votes and timeline."""
    from .models import get_db
    from .thought_experiment_service import ThoughtExperimentService

    db = get_db()
    try:
        result = ThoughtExperimentService.get_experiment_detail(
            db, experiment_id)
        if result:
            return jsonify({'success': True, 'data': result}), 200
        return jsonify({'success': False, 'error': 'not_found'}), 404
    except Exception as e:
        logger.error(f"Get experiment error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@thought_experiments_bp.route('/api/social/experiments/<experiment_id>/vote',
                               methods=['POST'])
def vote_experiment(experiment_id):
    """Cast a vote on a thought experiment."""
    from .models import get_db
    from .thought_experiment_service import ThoughtExperimentService

    body = request.get_json(silent=True) or {}
    voter_id = body.get('voter_id')
    if not voter_id:
        return jsonify({'success': False, 'error': 'voter_id required'}), 400

    db = get_db()
    try:
        result = ThoughtExperimentService.cast_vote(
            db, experiment_id, voter_id,
            vote_value=body.get('vote_value', 0),
            reasoning=body.get('reasoning', ''),
            suggestion=body.get('suggestion', ''),
            voter_type=body.get('voter_type', 'human'),
            confidence=body.get('confidence', 1.0),
        )
        if result:
            db.commit()
            return jsonify({'success': True, 'data': result}), 200
        return jsonify({'success': False, 'error': 'not_found'}), 404
    except Exception as e:
        db.rollback()
        logger.error(f"Vote error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@thought_experiments_bp.route('/api/social/experiments/<experiment_id>/advance',
                               methods=['POST'])
def advance_experiment(experiment_id):
    """Advance experiment to next lifecycle phase."""
    from .models import get_db
    from .thought_experiment_service import ThoughtExperimentService

    body = request.get_json(silent=True) or {}
    target_status = body.get('target_status')

    db = get_db()
    try:
        result = ThoughtExperimentService.advance_status(
            db, experiment_id, target_status=target_status)
        if result:
            db.commit()
            return jsonify({'success': True, 'data': result}), 200
        return jsonify({'success': False, 'error': 'cannot_advance'}), 400
    except Exception as e:
        db.rollback()
        logger.error(f"Advance error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@thought_experiments_bp.route('/api/social/experiments/<experiment_id>/evaluate',
                               methods=['POST'])
def evaluate_experiment(experiment_id):
    """Trigger agent evaluation for an experiment."""
    from .models import get_db
    from .thought_experiment_service import ThoughtExperimentService

    db = get_db()
    try:
        result = ThoughtExperimentService.request_agent_evaluation(
            db, experiment_id)
        if result.get('success'):
            db.commit()
        return jsonify(result), 200 if result.get('success') else 400
    except Exception as e:
        db.rollback()
        logger.error(f"Evaluate error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@thought_experiments_bp.route('/api/social/experiments/<experiment_id>/decide',
                               methods=['POST'])
def decide_experiment(experiment_id):
    """Record final decision for an experiment."""
    from .models import get_db
    from .thought_experiment_service import ThoughtExperimentService

    body = request.get_json(silent=True) or {}
    decision_text = body.get('decision', '')
    if not decision_text:
        return jsonify({'success': False, 'error': 'decision required'}), 400

    db = get_db()
    try:
        result = ThoughtExperimentService.decide(
            db, experiment_id, decision_text)
        if result:
            db.commit()
            return jsonify({'success': True, 'data': result}), 200
        return jsonify({'success': False, 'error': 'not_found'}), 404
    except Exception as e:
        db.rollback()
        logger.error(f"Decide error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@thought_experiments_bp.route('/api/social/experiments/<experiment_id>/votes',
                               methods=['GET'])
def experiment_votes(experiment_id):
    """Get all votes for an experiment."""
    from .models import get_db
    from .thought_experiment_service import ThoughtExperimentService

    db = get_db()
    try:
        votes = ThoughtExperimentService.get_experiment_votes(
            db, experiment_id)
        return jsonify({'success': True, 'data': votes}), 200
    except Exception as e:
        logger.error(f"Votes error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@thought_experiments_bp.route('/api/social/experiments/<experiment_id>/timeline',
                               methods=['GET'])
def experiment_timeline(experiment_id):
    """Get lifecycle timeline for an experiment."""
    from .models import get_db
    from .thought_experiment_service import ThoughtExperimentService

    db = get_db()
    try:
        timeline = ThoughtExperimentService.get_experiment_timeline(
            db, experiment_id)
        if timeline:
            return jsonify({'success': True, 'data': timeline}), 200
        return jsonify({'success': False, 'error': 'not_found'}), 404
    except Exception as e:
        logger.error(f"Timeline error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()
