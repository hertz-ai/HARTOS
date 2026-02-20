"""
Content Generation Task API Blueprint

GET  /api/social/content-gen/games           — All games with content gen tasks
GET  /api/social/content-gen/games/<game_id> — Single game progress + 24h delta
GET  /api/social/content-gen/stuck           — Games with stalled content gen
POST /api/social/content-gen/retry           — Retry stuck task(s)
GET  /api/social/content-gen/services        — Media service health
POST /api/social/content-gen/register        — Register a game for content gen tracking
"""
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger('hevolve_social')

content_gen_bp = Blueprint('content_gen', __name__)


@content_gen_bp.route('/api/social/content-gen/games', methods=['GET'])
def list_games():
    """All games with content gen task breakdown."""
    from integrations.social.models import get_db
    from .content_gen_tracker import ContentGenTracker

    db = get_db()
    try:
        games = ContentGenTracker.get_all_game_tasks(db)
        return jsonify({'success': True, 'data': games}), 200
    except Exception as e:
        logger.error(f"Content gen list error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@content_gen_bp.route('/api/social/content-gen/games/<game_id>', methods=['GET'])
def get_game(game_id):
    """Single game progress with per-task breakdown and 24h delta."""
    from integrations.social.models import get_db
    from .content_gen_tracker import ContentGenTracker

    db = get_db()
    try:
        progress = ContentGenTracker.get_game_progress(db, game_id)
        if not progress:
            return jsonify({
                'success': False,
                'error': f'No content generation found for game {game_id}',
            }), 404
        return jsonify({'success': True, 'data': progress}), 200
    except Exception as e:
        logger.error(f"Content gen game error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@content_gen_bp.route('/api/social/content-gen/stuck', methods=['GET'])
def get_stuck():
    """Games where content gen has stalled (0% delta for 24h+)."""
    from integrations.social.models import get_db
    from .content_gen_tracker import ContentGenTracker

    threshold = request.args.get('threshold_hours', 24, type=int)

    db = get_db()
    try:
        stuck = ContentGenTracker.get_stuck_games(db, stall_threshold_hours=threshold)
        return jsonify({'success': True, 'data': stuck}), 200
    except Exception as e:
        logger.error(f"Content gen stuck error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@content_gen_bp.route('/api/social/content-gen/retry', methods=['POST'])
def retry_task():
    """Retry stuck content gen task(s) for a game.

    Body: {game_id: str, task_type?: str}
    If task_type omitted, retries all stuck tasks.
    """
    from integrations.social.models import get_db
    from .content_gen_tracker import ContentGenTracker

    body = request.get_json(silent=True) or {}
    game_id = body.get('game_id')
    if not game_id:
        return jsonify({'success': False, 'error': 'game_id required'}), 400

    task_type = body.get('task_type')

    db = get_db()
    try:
        if task_type:
            ContentGenTracker.update_task_job(
                db, game_id, task_type,
                status='retrying', error=None)
            db.commit()
            result = {
                'action_taken': f'retry_{task_type}',
                'success': True,
                'detail': f'Retrying {task_type} for game {game_id}',
            }
        else:
            result = ContentGenTracker.attempt_unblock(db, game_id)
            db.commit()
        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        db.rollback()
        logger.error(f"Content gen retry error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@content_gen_bp.route('/api/social/content-gen/services', methods=['GET'])
def services_health():
    """Health of all media generation services."""
    from .content_gen_tracker import ContentGenTracker

    try:
        health = ContentGenTracker.get_services_health()
        return jsonify({
            'success': True,
            'data': {
                'services': {name: 'running' if ok else 'offline'
                             for name, ok in health.items()},
                'all_healthy': all(health.values()),
            },
        }), 200
    except Exception as e:
        logger.error(f"Content gen services error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@content_gen_bp.route('/api/social/content-gen/register', methods=['POST'])
def register_game():
    """Register a game for content generation tracking.

    Body: {game_id: str, game_config: dict}
    Creates an AgentGoal with goal_type='content_gen' if one doesn't exist.
    """
    from integrations.social.models import get_db
    from .content_gen_tracker import ContentGenTracker

    body = request.get_json(silent=True) or {}
    game_id = body.get('game_id')
    game_config = body.get('game_config', {})

    if not game_id:
        return jsonify({'success': False, 'error': 'game_id required'}), 400

    db = get_db()
    try:
        goal = ContentGenTracker.get_or_create_game_goal(db, game_id, game_config)
        if goal:
            db.commit()
            return jsonify({'success': True, 'data': goal}), 200
        return jsonify({'success': False, 'error': 'Failed to create goal'}), 500
    except Exception as e:
        db.rollback()
        logger.error(f"Content gen register error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()
