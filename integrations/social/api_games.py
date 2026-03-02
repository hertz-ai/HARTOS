"""
HevolveSocial - Multiplayer Games API Blueprint
12 endpoints for game session lifecycle, matchmaking, and history.
"""
import logging
from flask import Blueprint, request, jsonify, g

from .auth import require_auth
from .rate_limiter import rate_limit
from .models import get_db
from .game_service import GameService

logger = logging.getLogger('hevolve_social')

games_bp = Blueprint('games', __name__, url_prefix='/api/social')


def _ok(data=None, meta=None, status=200):
    r = {'success': True}
    if data is not None:
        r['data'] = data
    if meta is not None:
        r['meta'] = meta
    return jsonify(r), status


def _err(msg, status=400):
    return jsonify({'success': False, 'error': msg}), status


def _get_json():
    return request.get_json(force=True, silent=True) or {}


# ═══════════════════════════════════════════════════════════════
# GAME SESSIONS (12 endpoints)
# ═══════════════════════════════════════════════════════════════

@games_bp.route('/games', methods=['POST'])
@require_auth
@rate_limit(30)
def create_game():
    """Create a new game session. Host auto-joins."""
    data = _get_json()
    game_type = data.get('game_type', 'trivia')
    config = data.get('config', {})
    max_players = data.get('max_players', 4)
    total_rounds = data.get('total_rounds', 5)
    encounter_id = data.get('encounter_id')
    community_id = data.get('community_id')
    challenge_id = data.get('challenge_id')

    db = get_db()
    try:
        session = GameService.create_session(
            db, host_user_id=g.user_id, game_type=game_type,
            config=config, encounter_id=encounter_id,
            community_id=community_id, challenge_id=challenge_id,
            max_players=max_players, total_rounds=total_rounds,
        )
        db.commit()
        return _ok(session, status=201)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        db.rollback()
        logger.exception("Error creating game")
        return _err("Failed to create game", 500)
    finally:
        db.close()


@games_bp.route('/games', methods=['GET'])
@require_auth
def list_games():
    """List open/joinable game sessions."""
    game_type = request.args.get('game_type')
    community_id = request.args.get('community_id')
    limit = min(int(request.args.get('limit', 20)), 50)

    db = get_db()
    try:
        sessions = GameService.find_open_sessions(
            db, g.user_id, game_type=game_type,
            community_id=community_id, limit=limit,
        )
        return _ok(sessions)
    except Exception as e:
        logger.exception("Error listing games")
        return _err("Failed to list games", 500)
    finally:
        db.close()


@games_bp.route('/games/<session_id>', methods=['GET'])
@require_auth
def get_game(session_id):
    """Get game session state."""
    db = get_db()
    try:
        session = GameService.get_session(db, session_id)
        if not session:
            return _err("Game not found", 404)
        return _ok(session)
    except Exception as e:
        logger.exception("Error getting game")
        return _err("Failed to get game", 500)
    finally:
        db.close()


@games_bp.route('/games/<session_id>/join', methods=['POST'])
@require_auth
@rate_limit(30)
def join_game(session_id):
    """Join a waiting game session."""
    db = get_db()
    try:
        session = GameService.join_session(db, session_id, g.user_id)
        db.commit()

        # Notify other players
        try:
            from .realtime import on_notification
            for p in session.get('participants', []):
                if p['user_id'] != g.user_id:
                    on_notification(p['user_id'], {
                        'type': 'game_player_joined',
                        'game_id': session_id,
                        'user_id': g.user_id,
                    })
        except Exception:
            pass

        return _ok(session)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        db.rollback()
        logger.exception("Error joining game")
        return _err("Failed to join game", 500)
    finally:
        db.close()


@games_bp.route('/games/<session_id>/ready', methods=['POST'])
@require_auth
def ready_game(session_id):
    """Mark yourself as ready."""
    db = get_db()
    try:
        session = GameService.set_ready(db, session_id, g.user_id)
        db.commit()
        return _ok(session)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        db.rollback()
        logger.exception("Error marking ready")
        return _err("Failed to mark ready", 500)
    finally:
        db.close()


@games_bp.route('/games/<session_id>/start', methods=['POST'])
@require_auth
def start_game(session_id):
    """Host starts the game."""
    db = get_db()
    try:
        session = GameService.start_session(db, session_id, g.user_id)
        db.commit()

        # Notify all players game started
        try:
            from .realtime import on_notification
            for p in session.get('participants', []):
                on_notification(p['user_id'], {
                    'type': 'game_started',
                    'game_id': session_id,
                    'game_type': session.get('game_type'),
                })
        except Exception:
            pass

        return _ok(session)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        db.rollback()
        logger.exception("Error starting game")
        return _err("Failed to start game", 500)
    finally:
        db.close()


@games_bp.route('/games/<session_id>/move', methods=['POST'])
@require_auth
@rate_limit(60)
def submit_move(session_id):
    """Submit a move in an active game."""
    data = _get_json()
    db = get_db()
    try:
        session = GameService.submit_move(db, session_id, g.user_id, data)
        db.commit()

        # If game completed, notify all players
        if session.get('status') == 'completed':
            try:
                from .realtime import on_notification
                for p in session.get('participants', []):
                    on_notification(p['user_id'], {
                        'type': 'game_completed',
                        'game_id': session_id,
                        'result': p.get('result'),
                        'spark_earned': p.get('spark_earned', 0),
                        'xp_earned': p.get('xp_earned', 0),
                    })
            except Exception:
                pass

        return _ok(session)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        db.rollback()
        logger.exception("Error submitting move")
        return _err("Failed to submit move", 500)
    finally:
        db.close()


@games_bp.route('/games/<session_id>/leave', methods=['POST'])
@require_auth
def leave_game(session_id):
    """Leave a game session."""
    db = get_db()
    try:
        session = GameService.leave_session(db, session_id, g.user_id)
        db.commit()
        return _ok(session)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        db.rollback()
        logger.exception("Error leaving game")
        return _err("Failed to leave game", 500)
    finally:
        db.close()


@games_bp.route('/games/<session_id>/results', methods=['GET'])
@require_auth
def game_results(session_id):
    """Get final game results."""
    db = get_db()
    try:
        session = GameService.get_session(db, session_id)
        if not session:
            return _err("Game not found", 404)
        if session.get('status') != 'completed':
            return _err("Game not yet completed")
        return _ok(session)
    except Exception as e:
        logger.exception("Error getting results")
        return _err("Failed to get results", 500)
    finally:
        db.close()


@games_bp.route('/games/history', methods=['GET'])
@require_auth
def game_history():
    """Get user's game history."""
    limit = min(int(request.args.get('limit', 20)), 50)
    offset = int(request.args.get('offset', 0))

    db = get_db()
    try:
        history = GameService.get_history(db, g.user_id, limit=limit, offset=offset)
        return _ok(history)
    except Exception as e:
        logger.exception("Error getting game history")
        return _err("Failed to get history", 500)
    finally:
        db.close()


@games_bp.route('/games/quick-match', methods=['POST'])
@require_auth
@rate_limit(10)
def quick_match():
    """Auto-matchmake: join an open session or create a new one."""
    data = _get_json()
    game_type = data.get('game_type', 'trivia')

    db = get_db()
    try:
        session = GameService.quick_match(db, g.user_id, game_type=game_type)
        db.commit()
        return _ok(session)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        db.rollback()
        logger.exception("Error in quick match")
        return _err("Failed to quick match", 500)
    finally:
        db.close()


@games_bp.route('/games/from-encounter/<encounter_id>', methods=['POST'])
@require_auth
@rate_limit(10)
def game_from_encounter(encounter_id):
    """Create a game from an existing encounter (play with a bond)."""
    data = _get_json()
    game_type = data.get('game_type', 'trivia')
    config = data.get('config', {})

    db = get_db()
    try:
        session = GameService.create_from_encounter(
            db, encounter_id, g.user_id, game_type, config)
        db.commit()

        # Notify the other person in the encounter
        try:
            from .models import Encounter
            enc = db.query(Encounter).filter_by(id=encounter_id).first()
            if enc:
                other_id = enc.user_b_id if enc.user_a_id == g.user_id else enc.user_a_id
                from .realtime import on_notification
                on_notification(other_id, {
                    'type': 'game_invite',
                    'game_id': session['id'],
                    'game_type': game_type,
                    'from_user_id': g.user_id,
                    'encounter_id': encounter_id,
                })
        except Exception:
            pass

        return _ok(session, status=201)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        db.rollback()
        logger.exception("Error creating game from encounter")
        return _err("Failed to create game", 500)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# COMPUTE LENDING (6 endpoints)
# ═══════════════════════════════════════════════════════════════

@games_bp.route('/compute/opt-in', methods=['POST'])
@require_auth
@rate_limit(5)
def compute_opt_in():
    """Enable idle compute sharing."""
    db = get_db()
    try:
        user = db.query(User).filter_by(id=g.user_id).first()
        if not user:
            return _err("User not found", 404)

        already_opted = user.idle_compute_opt_in
        user.idle_compute_opt_in = True
        db.flush()

        if not already_opted:
            ResonanceService.award_action(db, g.user_id, 'compute_opt_in')

            # Check for first_compute_share achievement
            try:
                from .gamification_service import GamificationService
                GamificationService.check_achievements(db, g.user_id)
            except Exception:
                pass

        db.commit()
        return _ok({'opted_in': True, 'first_time': not already_opted})
    except Exception as e:
        db.rollback()
        logger.exception("Error opting in to compute")
        return _err("Failed to opt in", 500)
    finally:
        db.close()


@games_bp.route('/compute/opt-out', methods=['POST'])
@require_auth
def compute_opt_out():
    """Disable idle compute sharing."""
    db = get_db()
    try:
        user = db.query(User).filter_by(id=g.user_id).first()
        if not user:
            return _err("User not found", 404)
        user.idle_compute_opt_in = False
        db.commit()
        return _ok({'opted_in': False})
    except Exception as e:
        db.rollback()
        logger.exception("Error opting out of compute")
        return _err("Failed to opt out", 500)
    finally:
        db.close()


@games_bp.route('/compute/status', methods=['GET'])
@require_auth
def compute_status():
    """Get compute sharing status."""
    from .models import User, PeerNode
    db = get_db()
    try:
        user = db.query(User).filter_by(id=g.user_id).first()
        if not user:
            return _err("User not found", 404)

        node = db.query(PeerNode).filter_by(node_operator_id=g.user_id).first()
        status = {
            'opted_in': bool(user.idle_compute_opt_in),
            'node_active': bool(node and node.status == 'active') if node else False,
            'contribution_score': node.contribution_score if node else 0,
            'visibility_tier': node.visibility_tier if node else 'standard',
            'gpu_hours_served': node.gpu_hours_served if node else 0,
            'total_inferences': node.total_inferences if node else 0,
            'energy_kwh': node.energy_kwh_contributed if node else 0,
        }
        return _ok(status)
    except Exception as e:
        logger.exception("Error getting compute status")
        return _err("Failed to get status", 500)
    finally:
        db.close()


@games_bp.route('/compute/impact', methods=['GET'])
@require_auth
def compute_impact():
    """Get personal compute impact stats."""
    from .models import PeerNode, HostingReward
    db = get_db()
    try:
        node = db.query(PeerNode).filter_by(node_operator_id=g.user_id).first()
        if not node:
            return _ok({
                'gpu_hours': 0, 'inferences': 0, 'energy_kwh': 0,
                'spark_earned': 0, 'users_helped': 0,
            })

        # Sum Spark earned from hosting rewards
        from sqlalchemy import func
        total_spark = db.query(func.coalesce(func.sum(HostingReward.spark_amount), 0)).filter_by(
            node_operator_id=g.user_id
        ).scalar()

        return _ok({
            'gpu_hours': node.gpu_hours_served or 0,
            'inferences': node.total_inferences or 0,
            'energy_kwh': node.energy_kwh_contributed or 0,
            'spark_earned': total_spark,
            'agent_count': node.agent_count or 0,
            'contribution_score': node.contribution_score or 0,
            'visibility_tier': node.visibility_tier or 'standard',
        })
    except Exception as e:
        logger.exception("Error getting compute impact")
        return _err("Failed to get impact", 500)
    finally:
        db.close()


@games_bp.route('/compute/community-impact', methods=['GET'])
@require_auth
def compute_community_impact():
    """Get aggregate community compute stats."""
    from .models import PeerNode
    from sqlalchemy import func
    db = get_db()
    try:
        stats = db.query(
            func.count(PeerNode.id).label('total_nodes'),
            func.coalesce(func.sum(PeerNode.gpu_hours_served), 0).label('total_gpu_hours'),
            func.coalesce(func.sum(PeerNode.total_inferences), 0).label('total_inferences'),
            func.coalesce(func.sum(PeerNode.energy_kwh_contributed), 0).label('total_energy'),
            func.coalesce(func.sum(PeerNode.agent_count), 0).label('total_agents'),
        ).filter(PeerNode.status == 'active').first()

        return _ok({
            'active_nodes': stats.total_nodes or 0,
            'total_gpu_hours': float(stats.total_gpu_hours or 0),
            'total_inferences': int(stats.total_inferences or 0),
            'total_energy_kwh': float(stats.total_energy or 0),
            'total_agents_hosted': int(stats.total_agents or 0),
        })
    except Exception as e:
        logger.exception("Error getting community impact")
        return _err("Failed to get community impact", 500)
    finally:
        db.close()


@games_bp.route('/compute/health-check', methods=['POST'])
@require_auth
@rate_limit(60)
def compute_health_check():
    """Client heartbeat — updates PeerNode.last_seen."""
    from .models import PeerNode
    db = get_db()
    try:
        node = db.query(PeerNode).filter_by(node_operator_id=g.user_id).first()
        if node:
            node.last_seen = datetime.utcnow()
            node.status = 'active'
            db.commit()
            return _ok({'status': 'active'})
        return _ok({'status': 'no_node'})
    except Exception as e:
        db.rollback()
        logger.exception("Error in compute health check")
        return _err("Failed to update health", 500)
    finally:
        db.close()


# Need datetime for health check
from datetime import datetime
# Need User for compute endpoints
from .models import User
from .resonance_engine import ResonanceService
