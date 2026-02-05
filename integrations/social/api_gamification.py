"""
HevolveSocial - Gamification API Blueprint
~76 endpoints for Resonance, Achievements, Challenges, Seasons, Regions,
Encounters, Agent Evolution, Ratings, Distribution, Onboarding, Campaigns.
"""
import logging
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, g

from sqlalchemy.orm import Session as SASession
from .auth import require_auth, optional_auth, require_admin
from .rate_limiter import rate_limit
from .models import (
    get_db, User, Post, ResonanceWallet, ResonanceTransaction,
    Achievement, UserAchievement, Season, Challenge, UserChallenge,
    Region, RegionMembership, Encounter, Rating, TrustScore,
    AgentEvolution, AgentCollaboration, Referral, ReferralCode,
    Boost, OnboardingProgress, Campaign, CampaignAction,
    AdUnit, AdPlacement, AdImpression, PeerNode, HostingReward,
)
from .resonance_engine import ResonanceService

logger = logging.getLogger('hevolve_social')

gamification_bp = Blueprint('gamification', __name__, url_prefix='/api/social')


def _ok(data=None, meta=None, status=200):
    r = {'success': True}
    if data is not None:
        r['data'] = data
    if meta is not None:
        r['meta'] = meta
    return jsonify(r), status


def _err(msg, status=400):
    return jsonify({'success': False, 'error': msg}), status


def _paginate(total, limit, offset):
    return {'total': total, 'limit': limit, 'offset': offset,
            'has_more': offset + limit < total}


def _get_json():
    return request.get_json(force=True, silent=True) or {}


# ═══════════════════════════════════════════════════════════════
# RESONANCE (10 endpoints)
# ═══════════════════════════════════════════════════════════════

@gamification_bp.route('/resonance/wallet', methods=['GET'])
@require_auth
def resonance_wallet_self():
    db = get_db()
    try:
        wallet = ResonanceService.get_wallet(db, g.user_id)
        if not wallet:
            wallet = ResonanceService.get_or_create_wallet(db, g.user_id).to_dict()
            db.commit()
        return _ok(wallet)
    finally:
        db.close()


@gamification_bp.route('/resonance/wallet/<user_id>', methods=['GET'])
@optional_auth
def resonance_wallet_user(user_id):
    db = get_db()
    try:
        wallet = ResonanceService.get_wallet(db, user_id)
        if not wallet:
            return _err('Wallet not found', 404)
        return _ok(wallet)
    finally:
        db.close()


@gamification_bp.route('/resonance/transactions', methods=['GET'])
@require_auth
def resonance_transactions():
    db = get_db()
    try:
        currency = request.args.get('currency')
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))
        txns = ResonanceService.get_transactions(db, g.user_id, currency, limit, offset)
        return _ok(txns)
    finally:
        db.close()


@gamification_bp.route('/resonance/leaderboard', methods=['GET'])
@optional_auth
def resonance_leaderboard():
    db = get_db()
    try:
        currency = request.args.get('currency', 'pulse')
        region_id = request.args.get('region')
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))
        board = ResonanceService.get_leaderboard(db, currency, limit, offset, region_id)
        return _ok(board)
    finally:
        db.close()


@gamification_bp.route('/resonance/boost', methods=['POST'])
@require_auth
def resonance_boost():
    db = get_db()
    try:
        data = _get_json()
        target_type = data.get('target_type')
        target_id = data.get('target_id')
        spark_amount = int(data.get('spark_amount', 10))
        if not target_type or not target_id:
            return _err('target_type and target_id required')
        if spark_amount < 1:
            return _err('spark_amount must be positive')

        ok, remaining = ResonanceService.spend_spark(
            db, g.user_id, spark_amount, 'boost', target_id,
            f'Boost {target_type} {target_id}')
        if not ok:
            return _err(f'Insufficient Spark (have {remaining})')

        multiplier = min(1.0 + spark_amount * 0.01, 2.0)
        hours = spark_amount
        boost = Boost(
            user_id=g.user_id, target_type=target_type, target_id=target_id,
            spark_spent=spark_amount, boost_multiplier=multiplier,
            expires_at=datetime.utcnow() + timedelta(hours=hours),
        )
        db.add(boost)

        if target_type == 'post':
            post = db.query(Post).filter_by(id=target_id).first()
            if post:
                post.boost_score = (post.boost_score or 0) + multiplier

        db.commit()
        return _ok(boost.to_dict())
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/resonance/boosts/<target_type>/<target_id>', methods=['GET'])
@optional_auth
def resonance_boosts_for_target(target_type, target_id):
    db = get_db()
    try:
        boosts = db.query(Boost).filter_by(
            target_type=target_type, target_id=target_id
        ).filter(Boost.expires_at > datetime.utcnow()).all()
        return _ok([b.to_dict() for b in boosts])
    finally:
        db.close()


@gamification_bp.route('/resonance/level-info', methods=['GET'])
@require_auth
def resonance_level_info():
    db = get_db()
    try:
        wallet = ResonanceService.get_or_create_wallet(db, g.user_id)
        from .resonance_engine import xp_for_level, title_for_level, LEVEL_TITLES
        info = {
            'level': wallet.level, 'title': wallet.level_title,
            'xp': wallet.xp, 'xp_next': wallet.xp_next_level,
            'progress_pct': round(wallet.xp / max(wallet.xp_next_level, 1) * 100, 1),
            'all_titles': {str(k): v for k, v in LEVEL_TITLES.items()},
        }
        db.commit()
        return _ok(info)
    finally:
        db.close()


@gamification_bp.route('/resonance/streak', methods=['GET'])
@require_auth
def resonance_streak():
    db = get_db()
    try:
        wallet = ResonanceService.get_or_create_wallet(db, g.user_id)
        db.commit()
        return _ok({
            'streak_days': wallet.streak_days,
            'streak_best': wallet.streak_best,
            'last_active_date': wallet.last_active_date,
        })
    finally:
        db.close()


@gamification_bp.route('/resonance/daily-checkin', methods=['POST'])
@require_auth
def resonance_daily_checkin():
    db = get_db()
    try:
        result = ResonanceService.process_streak(db, g.user_id)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/resonance/breakdown/<user_id>', methods=['GET'])
@optional_auth
def resonance_breakdown(user_id):
    db = get_db()
    try:
        breakdown = ResonanceService.get_breakdown(db, user_id)
        if not breakdown:
            return _err('User not found', 404)
        return _ok(breakdown)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# ACHIEVEMENTS (5 endpoints)
# ═══════════════════════════════════════════════════════════════

@gamification_bp.route('/achievements', methods=['GET'])
@optional_auth
def list_achievements():
    from .gamification_service import GamificationService
    db = get_db()
    try:
        achievements = GamificationService.get_all_achievements(db)
        return _ok(achievements)
    finally:
        db.close()


@gamification_bp.route('/achievements/<user_id>', methods=['GET'])
@optional_auth
def user_achievements(user_id):
    from .gamification_service import GamificationService
    db = get_db()
    try:
        achievements = GamificationService.get_user_achievements(db, user_id)
        return _ok(achievements)
    finally:
        db.close()


@gamification_bp.route('/achievements/<achievement_id>/showcase', methods=['POST'])
@require_auth
def toggle_showcase(achievement_id):
    from .gamification_service import GamificationService
    db = get_db()
    try:
        data = _get_json()
        result = GamificationService.toggle_showcase(db, g.user_id, achievement_id)
        if result is None:
            return _err("Achievement not found or not unlocked", 404)
        db.commit()
        return _ok({'is_showcased': result})
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# CHALLENGES (5 endpoints)
# ═══════════════════════════════════════════════════════════════

@gamification_bp.route('/challenges', methods=['GET'])
@optional_auth
def list_challenges():
    from .gamification_service import GamificationService
    db = get_db()
    try:
        user_id = g.user_id if hasattr(g, 'user_id') and g.user_id else None
        challenges = GamificationService.get_active_challenges(db, user_id)
        return _ok(challenges)
    finally:
        db.close()


@gamification_bp.route('/challenges/<challenge_id>', methods=['GET'])
@optional_auth
def get_challenge(challenge_id):
    from .gamification_service import GamificationService
    db = get_db()
    try:
        user_id = g.user_id if hasattr(g, 'user_id') and g.user_id else None
        challenge = GamificationService.get_challenge(db, challenge_id, user_id)
        if not challenge:
            return _err("Challenge not found", 404)
        return _ok(challenge)
    finally:
        db.close()


@gamification_bp.route('/challenges/<challenge_id>/progress', methods=['POST'])
@require_auth
def update_challenge_progress(challenge_id):
    from .gamification_service import GamificationService
    db = get_db()
    try:
        data = _get_json()
        increment = data.get('increment', 1)
        result = GamificationService.update_challenge_progress(db, g.user_id, challenge_id, increment)
        if not result:
            return _err("Challenge not found", 404)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/challenges/<challenge_id>/claim', methods=['POST'])
@require_auth
def claim_challenge_reward(challenge_id):
    from .gamification_service import GamificationService
    db = get_db()
    try:
        result = GamificationService.claim_challenge_reward(db, g.user_id, challenge_id)
        if not result:
            return _err("Challenge not completed or not found", 404)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# SEASONS (4 endpoints)
# ═══════════════════════════════════════════════════════════════

@gamification_bp.route('/seasons/current', methods=['GET'])
@optional_auth
def current_season():
    from .gamification_service import GamificationService
    db = get_db()
    try:
        season = GamificationService.get_current_season(db)
        return _ok(season)
    finally:
        db.close()


@gamification_bp.route('/seasons/<season_id>/leaderboard', methods=['GET'])
@optional_auth
def season_leaderboard(season_id):
    from .gamification_service import GamificationService
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))
        result = GamificationService.get_season_leaderboard(db, season_id, limit, offset)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/seasons/<season_id>/achievements', methods=['GET'])
@optional_auth
def season_achievements(season_id):
    from .gamification_service import GamificationService
    db = get_db()
    try:
        result = GamificationService.get_season_achievements(db, season_id)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/collectibles/<user_id>', methods=['GET'])
@optional_auth
def user_collectibles(user_id):
    db = get_db()
    try:
        showcased = db.query(UserAchievement).filter_by(
            user_id=user_id, is_showcased=True).all()
        wallet = ResonanceService.get_wallet(db, user_id)
        return _ok({
            'showcased_achievements': [ua.to_dict() for ua in showcased],
            'level': wallet.get('level', 1) if wallet else 1,
            'level_title': wallet.get('level_title', 'Newcomer') if wallet else 'Newcomer',
        })
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# REGIONS & GOVERNANCE (14 endpoints)
# ═══════════════════════════════════════════════════════════════

@gamification_bp.route('/regions', methods=['GET'])
@optional_auth
def list_regions():
    from .region_service import RegionService
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))
        regions = RegionService.list_regions(db, limit=limit, offset=offset)
        return _ok(regions)
    finally:
        db.close()


@gamification_bp.route('/regions', methods=['POST'])
@require_auth
def create_region():
    from .region_service import RegionService
    db = get_db()
    try:
        data = _get_json()
        result = RegionService.create_region(db, g.user_id, data)
        db.commit()
        return _ok(result, status=201)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/regions/<region_id>', methods=['GET'])
@optional_auth
def get_region(region_id):
    from .region_service import RegionService
    db = get_db()
    try:
        region = RegionService.get_region(db, region_id)
        if not region:
            return _err('Region not found', 404)
        return _ok(region)
    finally:
        db.close()


@gamification_bp.route('/regions/<region_id>', methods=['PATCH'])
@require_auth
def update_region(region_id):
    db = get_db()
    try:
        mem = db.query(RegionMembership).filter_by(
            user_id=g.user_id, region_id=region_id).first()
        if not mem or mem.role not in ('admin', 'steward'):
            return _err('Insufficient privileges', 403)
        region = db.query(Region).filter_by(id=region_id).first()
        if not region:
            return _err('Region not found', 404)
        data = _get_json()
        for field in ('display_name', 'description', 'global_server_url', 'settings_json'):
            if field in data:
                setattr(region, field, data[field])
        db.commit()
        return _ok(region.to_dict())
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/regions/<region_id>/join', methods=['POST'])
@require_auth
def join_region(region_id):
    from .region_service import RegionService
    db = get_db()
    try:
        result = RegionService.join_region(db, g.user_id, region_id)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/regions/<region_id>/leave', methods=['DELETE'])
@require_auth
def leave_region(region_id):
    from .region_service import RegionService
    db = get_db()
    try:
        result = RegionService.leave_region(db, g.user_id, region_id)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/regions/<region_id>/members', methods=['GET'])
@optional_auth
def region_members(region_id):
    from .region_service import RegionService
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))
        result = RegionService.get_members(db, region_id, limit=limit, offset=offset)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/regions/<region_id>/feed', methods=['GET'])
@optional_auth
def region_feed(region_id):
    from .region_service import RegionService
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 20)), 50)
        offset = int(request.args.get('offset', 0))
        result = RegionService.get_regional_feed(db, region_id, limit=limit, offset=offset)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/regions/<region_id>/leaderboard', methods=['GET'])
@optional_auth
def region_leaderboard(region_id):
    from .region_service import RegionService
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))
        result = RegionService.get_regional_leaderboard(db, region_id, limit=limit, offset=offset)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/regions/<region_id>/governance', methods=['GET'])
@optional_auth
def region_governance(region_id):
    from .region_service import RegionService
    db = get_db()
    try:
        result = RegionService.get_governance_info(db, region_id)
        if not result:
            return _err('Region not found', 404)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/regions/<region_id>/promote', methods=['POST'])
@require_auth
def promote_member(region_id):
    from .region_service import RegionService
    db = get_db()
    try:
        data = _get_json()
        target_user_id = data.get('user_id')
        new_role = data.get('role')
        result = RegionService.promote_member(db, g.user_id, region_id, target_user_id, new_role)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/regions/<region_id>/demote', methods=['POST'])
@require_auth
def demote_member(region_id):
    from .region_service import RegionService
    db = get_db()
    try:
        data = _get_json()
        target_user_id = data.get('user_id')
        result = RegionService.demote_member(db, g.user_id, region_id, target_user_id)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/regions/nearby', methods=['GET'])
@optional_auth
def nearby_regions():
    from .region_service import RegionService
    db = get_db()
    try:
        lat = float(request.args.get('lat', 0))
        lon = float(request.args.get('lon', 0))
        radius = float(request.args.get('radius', 100))
        result = RegionService.nearby_regions(db, lat, lon, radius)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/regions/<region_id>/sync', methods=['POST'])
@require_auth
def sync_region(region_id):
    db = get_db()
    try:
        mem = db.query(RegionMembership).filter_by(
            user_id=g.user_id, region_id=region_id).first()
        if not mem or mem.role not in ('admin', 'steward'):
            return _err('Insufficient privileges', 403)
        region = db.query(Region).filter_by(id=region_id).first()
        if not region or not region.global_server_url:
            return _err('No global server configured')
        # Sync with global server (placeholder — actual federation call)
        return _ok({'synced': True, 'global_server': region.global_server_url})
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# ENCOUNTERS (6 endpoints)
# ═══════════════════════════════════════════════════════════════

@gamification_bp.route('/encounters', methods=['GET'])
@require_auth
def list_encounters():
    from .encounter_service import EncounterService
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 20)), 50)
        encounters = EncounterService.get_encounters(db, g.user_id, limit=limit)
        return _ok(encounters)
    finally:
        db.close()


@gamification_bp.route('/encounters/<user_id>', methods=['GET'])
@require_auth
def shared_encounters(user_id):
    from .encounter_service import EncounterService
    db = get_db()
    try:
        encounters = EncounterService.get_encounters_with(db, g.user_id, user_id)
        return _ok(encounters)
    finally:
        db.close()


@gamification_bp.route('/encounters/<encounter_id>/acknowledge', methods=['POST'])
@require_auth
def acknowledge_encounter(encounter_id):
    from .encounter_service import EncounterService
    db = get_db()
    try:
        result = EncounterService.acknowledge_encounter(db, encounter_id, g.user_id)
        if not result:
            return _err('Encounter not found', 404)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/encounters/suggestions', methods=['GET'])
@require_auth
def encounter_suggestions():
    from .encounter_service import EncounterService
    db = get_db()
    try:
        suggestions = EncounterService.get_suggestions(db, g.user_id)
        return _ok(suggestions)
    finally:
        db.close()


@gamification_bp.route('/encounters/bonds', methods=['GET'])
@require_auth
def encounter_bonds():
    from .encounter_service import EncounterService
    db = get_db()
    try:
        bonds = EncounterService.get_bonds(db, g.user_id)
        return _ok(bonds)
    finally:
        db.close()


@gamification_bp.route('/encounters/nearby', methods=['GET'])
@require_auth
def encounters_nearby():
    from .encounter_service import EncounterService
    db = get_db()
    try:
        result = EncounterService.get_nearby_active(db, g.user_id)
        return _ok(result)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# AGENT EVOLUTION (8 endpoints)
# ═══════════════════════════════════════════════════════════════

@gamification_bp.route('/agents/<agent_id>/evolution', methods=['GET'])
@optional_auth
def agent_evolution(agent_id):
    from .agent_evolution_service import AgentEvolutionService
    db = get_db()
    try:
        result = AgentEvolutionService.get_evolution(db, agent_id)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/agents/<agent_id>/specialize', methods=['POST'])
@require_auth
def agent_specialize(agent_id):
    from .agent_evolution_service import AgentEvolutionService
    db = get_db()
    try:
        data = _get_json()
        path = data.get('path')
        result = AgentEvolutionService.specialize(db, agent_id, path)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/agents/leaderboard', methods=['GET'])
@optional_auth
def agent_leaderboard():
    from .agent_evolution_service import AgentEvolutionService
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        result = AgentEvolutionService.get_agent_leaderboard(db, limit=limit)
        return _ok(result)
    finally:
        db.close()


SPECIALIZATION_TREES = {
    'analyst': {'base': 'Analyst', 'advanced': 'Oracle',
                'description': 'Data analysis, pattern recognition, insights'},
    'creator': {'base': 'Creator', 'advanced': 'Visionary',
                'description': 'Content generation, creative problem solving'},
    'executor': {'base': 'Executor', 'advanced': 'Automaton',
                 'description': 'Task execution, workflow automation'},
    'communicator': {'base': 'Communicator', 'advanced': 'Ambassador',
                     'description': 'Inter-agent communication, negotiation'},
}


@gamification_bp.route('/agents/specialization-trees', methods=['GET'])
@optional_auth
def specialization_trees():
    return _ok(SPECIALIZATION_TREES)


@gamification_bp.route('/agents/<agent_id>/collaborations', methods=['GET'])
@optional_auth
def agent_collaborations(agent_id):
    from .agent_evolution_service import AgentEvolutionService
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        result = AgentEvolutionService.get_collaborations(db, agent_id, limit=limit)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/agents/<agent_id>/collaborate', methods=['POST'])
@require_auth
def record_collaboration(agent_id):
    from .agent_evolution_service import AgentEvolutionService
    db = get_db()
    try:
        data = _get_json()
        other_agent_id = data.get('other_agent_id')
        collab_type = data.get('type', 'co_task')
        quality = float(data.get('quality_score', 0.5))
        task_id = data.get('task_id')
        result = AgentEvolutionService.record_collaboration(
            db, agent_id, other_agent_id, collab_type, quality, task_id)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/agents/showcase', methods=['GET'])
@optional_auth
def agent_showcase():
    from .agent_evolution_service import AgentEvolutionService
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 20)), 50)
        result = AgentEvolutionService.get_showcase(db, limit=limit)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/agents/<agent_id>/evolution-history', methods=['GET'])
@optional_auth
def agent_evolution_history(agent_id):
    db = get_db()
    try:
        # XP timeline from transactions
        txns = db.query(ResonanceTransaction).filter_by(
            user_id=agent_id, currency='xp'
        ).order_by(ResonanceTransaction.created_at).all()
        return _ok([t.to_dict() for t in txns])
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# RATINGS & TRUST (6 endpoints)
# ═══════════════════════════════════════════════════════════════

@gamification_bp.route('/ratings', methods=['POST'])
@require_auth
def submit_rating():
    from .rating_service import RatingService
    db = get_db()
    try:
        data = _get_json()
        result = RatingService.submit_rating(db, g.user_id, data)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


def _recalculate_trust(db: SASession, user_id: str):
    """Recalculate composite trust score for a user."""
    from sqlalchemy import func as sqlfunc
    avgs = db.query(
        Rating.dimension, sqlfunc.avg(Rating.score), sqlfunc.count(Rating.id)
    ).filter_by(rated_id=user_id).group_by(Rating.dimension).all()

    ts = db.query(TrustScore).filter_by(user_id=user_id).first()
    if not ts:
        ts = TrustScore(user_id=user_id)
        db.add(ts)

    total = 0
    for dim, avg_score, count in avgs:
        setattr(ts, f'avg_{dim}', float(avg_score))
        total += count

    ts.total_ratings_received = total
    # Weighted composite: skill 0.25, usefulness 0.30, reliability 0.30, creativity 0.15
    ts.composite_trust = (
        ts.avg_skill * 0.25 + ts.avg_usefulness * 0.30 +
        ts.avg_reliability * 0.30 + ts.avg_creativity * 0.15
    )


@gamification_bp.route('/ratings/<user_id>', methods=['GET'])
@optional_auth
def get_trust_scores(user_id):
    from .rating_service import RatingService
    db = get_db()
    try:
        result = RatingService.get_aggregated(db, user_id)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/ratings/<user_id>/received', methods=['GET'])
@optional_auth
def ratings_received(user_id):
    from .rating_service import RatingService
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))
        result = RatingService.get_ratings_received(db, user_id, limit=limit, offset=offset)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/ratings/<user_id>/given', methods=['GET'])
@require_auth
def ratings_given(user_id):
    from .rating_service import RatingService
    db = get_db()
    try:
        if user_id != g.user_id:
            return _err('Can only view your own given ratings', 403)
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))
        result = RatingService.get_ratings_given(db, user_id, limit=limit, offset=offset)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/ratings/context/<context_type>/<context_id>', methods=['GET'])
@optional_auth
def ratings_for_context(context_type, context_id):
    db = get_db()
    try:
        ratings = db.query(Rating).filter_by(
            context_type=context_type, context_id=context_id).all()
        return _ok([r.to_dict() for r in ratings])
    finally:
        db.close()


@gamification_bp.route('/trust/<user_id>', methods=['GET'])
@optional_auth
def trust_card(user_id):
    from .rating_service import RatingService
    db = get_db()
    try:
        result = RatingService.get_trust_score(db, user_id)
        if not result:
            return _err('User not found', 404)
        return _ok(result)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# DISTRIBUTION (8 endpoints)
# ═══════════════════════════════════════════════════════════════

@gamification_bp.route('/referral/code', methods=['GET'])
@require_auth
def get_referral_code():
    from .distribution_service import DistributionService
    db = get_db()
    try:
        result = DistributionService.get_or_create_referral_code(db, g.user_id)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/referral/use', methods=['POST'])
@require_auth
def use_referral_code():
    from .distribution_service import DistributionService
    db = get_db()
    try:
        data = _get_json()
        code = data.get('code', '')
        result = DistributionService.use_referral_code(db, g.user_id, code)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/referral/stats', methods=['GET'])
@require_auth
def referral_stats():
    from .distribution_service import DistributionService
    db = get_db()
    try:
        result = DistributionService.get_referral_stats(db, g.user_id)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/marketplace/recipes', methods=['GET'])
@optional_auth
def marketplace_recipes():
    db = get_db()
    try:
        from .models import RecipeShare
        limit = min(int(request.args.get('limit', 20)), 50)
        recipes = db.query(RecipeShare).order_by(
            RecipeShare.fork_count.desc()).limit(limit).all()
        return _ok([r.to_dict() for r in recipes])
    finally:
        db.close()


@gamification_bp.route('/marketplace/agents', methods=['GET'])
@optional_auth
def marketplace_agents():
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 20)), 50)
        agents = db.query(User).filter_by(
            user_type='agent', is_banned=False
        ).order_by(User.karma_score.desc()).limit(limit).all()
        return _ok([a.to_dict() for a in agents])
    finally:
        db.close()


@gamification_bp.route('/share/generate-link', methods=['POST'])
@require_auth
def generate_share_link():
    db = get_db()
    try:
        data = _get_json()
        target_type = data.get('target_type')
        target_id = data.get('target_id')
        # Get user's referral code
        user = db.query(User).filter_by(id=g.user_id).first()
        ref_code = user.referral_code if user else ''
        base_url = data.get('base_url', 'https://hevolve.ai')
        link = f"{base_url}/{target_type}/{target_id}?ref={ref_code}"
        return _ok({'link': link, 'referral_code': ref_code})
    finally:
        db.close()


@gamification_bp.route('/federation/contribution', methods=['GET'])
@optional_auth
def federation_contribution():
    db = get_db()
    try:
        from .models import PeerNode
        nodes = db.query(PeerNode).filter_by(
            status='active'
        ).order_by(PeerNode.contribution_score.desc()).limit(50).all()
        return _ok([n.to_dict() for n in nodes])
    finally:
        db.close()


@gamification_bp.route('/growth/stats', methods=['GET'])
@require_admin
def growth_stats():
    db = get_db()
    try:
        total_users = db.query(User).count()
        total_agents = db.query(User).filter_by(user_type='agent').count()
        total_wallets = db.query(ResonanceWallet).count()
        total_regions = db.query(Region).count()
        total_campaigns = db.query(Campaign).count()
        return _ok({
            'total_users': total_users,
            'total_agents': total_agents,
            'total_wallets': total_wallets,
            'total_regions': total_regions,
            'total_campaigns': total_campaigns,
        })
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# CAMPAIGNS — "Make Me Viral" (8 endpoints)
# ═══════════════════════════════════════════════════════════════

@gamification_bp.route('/campaigns', methods=['POST'])
@require_auth
def create_campaign():
    from .campaign_service import CampaignService
    db = get_db()
    try:
        data = _get_json()
        result = CampaignService.create_campaign(db, g.user_id, data)
        db.commit()
        return _ok(result, status=201)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/campaigns', methods=['GET'])
@require_auth
def list_campaigns():
    from .campaign_service import CampaignService
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))
        result = CampaignService.list_campaigns(db, owner_id=g.user_id, limit=limit, offset=offset)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/campaigns/<campaign_id>', methods=['GET'])
@require_auth
def get_campaign(campaign_id):
    from .campaign_service import CampaignService
    db = get_db()
    try:
        result = CampaignService.get_campaign(db, campaign_id)
        if not result:
            return _err('Campaign not found', 404)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/campaigns/<campaign_id>', methods=['PATCH'])
@require_auth
def update_campaign(campaign_id):
    from .campaign_service import CampaignService
    db = get_db()
    try:
        data = _get_json()
        result = CampaignService.update_campaign(db, campaign_id, g.user_id, data)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/campaigns/<campaign_id>/generate-strategy', methods=['POST'])
@require_auth
def generate_campaign_strategy(campaign_id):
    from .campaign_service import CampaignService
    db = get_db()
    try:
        result = CampaignService.generate_strategy(db, campaign_id, g.user_id)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/campaigns/<campaign_id>/execute-step', methods=['POST'])
@require_auth
def execute_campaign_step(campaign_id):
    from .campaign_service import CampaignService
    db = get_db()
    try:
        result = CampaignService.execute_campaign_step(db, campaign_id, g.user_id)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/campaigns/leaderboard', methods=['GET'])
@optional_auth
def campaign_leaderboard():
    from .campaign_service import CampaignService
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 20)), 50)
        offset = int(request.args.get('offset', 0))
        result = CampaignService.get_leaderboard(db, limit=limit, offset=offset)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/campaigns/<campaign_id>', methods=['DELETE'])
@require_auth
def delete_campaign(campaign_id):
    from .campaign_service import CampaignService
    db = get_db()
    try:
        result = CampaignService.delete_campaign(db, campaign_id, g.user_id)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# ONBOARDING (4 endpoints)
# ═══════════════════════════════════════════════════════════════

ONBOARDING_STEPS = [
    'welcome', 'pick_interests', 'claim_handle', 'follow_friends',
    'first_interaction', 'join_community', 'create_something',
]


@gamification_bp.route('/onboarding/progress', methods=['GET'])
@require_auth
def onboarding_progress():
    from .onboarding_service import OnboardingService
    db = get_db()
    try:
        result = OnboardingService.get_progress(db, g.user_id)
        db.commit()
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/onboarding/complete-step', methods=['POST'])
@require_auth
def complete_onboarding_step():
    from .onboarding_service import OnboardingService
    db = get_db()
    try:
        data = _get_json()
        step = data.get('step')
        result = OnboardingService.complete_step(db, g.user_id, step)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/onboarding/dismiss', methods=['POST'])
@require_auth
def dismiss_onboarding():
    from .onboarding_service import OnboardingService
    db = get_db()
    try:
        result = OnboardingService.dismiss(db, g.user_id)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/onboarding/suggestion', methods=['GET'])
@require_auth
def onboarding_suggestion():
    from .onboarding_service import OnboardingService
    db = get_db()
    try:
        result = OnboardingService.get_suggestion(db, g.user_id)
        return _ok(result)
    finally:
        db.close()


# ── Proximity & Missed Connections ──────────────────────────────

@gamification_bp.route('/encounters/location-ping', methods=['POST'])
@require_auth
def location_ping():
    from .proximity_service import ProximityService
    data = request.get_json(force=True, silent=True) or {}
    lat = data.get('lat')
    lon = data.get('lon')
    accuracy = data.get('accuracy', 0)
    if lat is None or lon is None:
        return _err("lat and lon required", 400)
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return _err("Invalid coordinates", 400)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return _err("Coordinates out of range", 400)
    db = get_db()
    try:
        result = ProximityService.update_location(db, g.user_id, lat, lon, accuracy)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/encounters/nearby-now', methods=['GET'])
@require_auth
def nearby_now():
    from .proximity_service import ProximityService
    db = get_db()
    try:
        count = ProximityService.get_nearby_count(db, g.user_id)
        return _ok({'nearby_count': count})
    finally:
        db.close()


@gamification_bp.route('/encounters/proximity-matches', methods=['GET'])
@require_auth
def proximity_matches():
    from .proximity_service import ProximityService
    db = get_db()
    try:
        status = request.args.get('status')
        matches = ProximityService.get_matches(db, g.user_id, status=status)
        return _ok(matches)
    finally:
        db.close()


@gamification_bp.route('/encounters/proximity/<match_id>/reveal', methods=['POST'])
@require_auth
def proximity_reveal(match_id):
    from .proximity_service import ProximityService
    db = get_db()
    try:
        result = ProximityService.reveal_self(db, match_id, g.user_id)
        db.commit()
        return _ok(result)
    except ValueError as e:
        db.rollback()
        return _err(str(e), 400)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/encounters/location-settings', methods=['GET', 'PATCH'])
@require_auth
def location_settings():
    from .proximity_service import ProximityService
    db = get_db()
    try:
        if request.method == 'GET':
            result = ProximityService.get_location_settings(db, g.user_id)
            return _ok(result)
        else:
            data = request.get_json(force=True, silent=True) or {}
            enabled = data.get('location_sharing_enabled', False)
            result = ProximityService.update_location_settings(db, g.user_id, enabled)
            db.commit()
            return _ok(result)
    except ValueError as e:
        db.rollback()
        return _err(str(e), 400)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/encounters/missed-connections', methods=['GET', 'POST'])
@require_auth
def missed_connections():
    from .proximity_service import ProximityService
    db = get_db()
    try:
        if request.method == 'POST':
            data = request.get_json(force=True, silent=True) or {}
            result = ProximityService.create_missed_connection(
                db, g.user_id,
                lat=float(data.get('lat', 0)),
                lon=float(data.get('lon', 0)),
                location_name=data.get('location_name', ''),
                description=data.get('description', ''),
                was_at_iso=data.get('was_at', ''),
            )
            db.commit()
            return _ok(result), 201
        else:
            lat = request.args.get('lat', type=float)
            lon = request.args.get('lon', type=float)
            radius = request.args.get('radius', 1000, type=float)
            limit = request.args.get('limit', 20, type=int)
            offset = request.args.get('offset', 0, type=int)
            sort = request.args.get('sort', 'recent')
            if lat is None or lon is None:
                return _err("lat and lon required for search", 400)
            result = ProximityService.search_missed_connections(
                db, lat, lon, radius, limit=min(limit, 50), offset=offset,
                exclude_user_id=None, sort=sort)
            return _ok(result['data'], meta=result['meta'])
    except ValueError as e:
        db.rollback()
        return _err(str(e), 400)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/encounters/missed-connections/mine', methods=['GET'])
@require_auth
def my_missed_connections():
    from .proximity_service import ProximityService
    db = get_db()
    try:
        limit = request.args.get('limit', 20, type=int)
        offset = request.args.get('offset', 0, type=int)
        result = ProximityService.get_my_missed_connections(db, g.user_id, limit=min(limit, 50), offset=offset)
        return _ok(result['data'], meta=result['meta'])
    finally:
        db.close()


@gamification_bp.route('/encounters/missed-connections/<missed_id>', methods=['GET', 'DELETE'])
@require_auth
def missed_connection_detail(missed_id):
    from .proximity_service import ProximityService
    db = get_db()
    try:
        if request.method == 'DELETE':
            result = ProximityService.delete_missed_connection(db, missed_id, g.user_id)
            db.commit()
            return _ok(result)
        else:
            result = ProximityService.get_missed_with_responses(db, missed_id, viewer_id=g.user_id)
            return _ok(result)
    except ValueError as e:
        db.rollback()
        return _err(str(e), 400)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/encounters/missed-connections/<missed_id>/respond', methods=['POST'])
@require_auth
def respond_missed_connection(missed_id):
    from .proximity_service import ProximityService
    data = request.get_json(force=True, silent=True) or {}
    db = get_db()
    try:
        result = ProximityService.respond_to_missed(db, missed_id, g.user_id, data.get('message', ''))
        db.commit()
        return _ok(result), 201
    except ValueError as e:
        db.rollback()
        return _err(str(e), 400)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/encounters/missed-connections/<missed_id>/accept/<response_id>', methods=['POST'])
@require_auth
def accept_missed_response(missed_id, response_id):
    from .proximity_service import ProximityService
    db = get_db()
    try:
        result = ProximityService.accept_missed_response(db, missed_id, response_id, g.user_id)
        db.commit()
        return _ok(result)
    except ValueError as e:
        db.rollback()
        return _err(str(e), 400)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/encounters/missed-connections/suggest-locations', methods=['GET'])
@require_auth
def suggest_locations():
    from .proximity_service import ProximityService
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    if lat is None or lon is None:
        return _err("lat and lon required", 400)
    db = get_db()
    try:
        result = ProximityService.auto_suggest_locations(db, lat, lon)
        return _ok(result)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# ADS (7 endpoints)
# ═══════════════════════════════════════════════════════════════

@gamification_bp.route('/ads', methods=['POST'])
@require_auth
def create_ad():
    from .ad_service import AdService
    db = get_db()
    try:
        data = _get_json()
        result = AdService.create_ad(
            db, g.user_id,
            title=data.get('title', ''),
            click_url=data.get('click_url', ''),
            content=data.get('content', ''),
            image_url=data.get('image_url', ''),
            ad_type=data.get('ad_type', 'banner'),
            targeting=data.get('targeting'),
            budget_spark=int(data.get('budget_spark', 100)),
            cost_per_impression=float(data.get('cost_per_impression', 0.1)),
            cost_per_click=float(data.get('cost_per_click', 1.0)),
            starts_at=data.get('starts_at'),
            ends_at=data.get('ends_at'),
        )
        if 'error' in result:
            return _err(result['error'])
        db.commit()
        return _ok(result, status=201)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/ads/serve', methods=['GET'])
@optional_auth
def serve_ad():
    from .ad_service import AdService
    db = get_db()
    try:
        user_id = g.user_id if hasattr(g, 'user_id') and g.user_id else None
        region_id = request.args.get('region')
        placement = request.args.get('placement', 'feed_top')
        node_id = request.args.get('node_id')
        result = AdService.serve_ad(db, user_id, region_id, placement, node_id)
        if not result:
            return _ok(None)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/ads/<ad_id>/impression', methods=['POST'])
@optional_auth
def record_ad_impression(ad_id):
    from .ad_service import AdService
    import hashlib
    db = get_db()
    try:
        data = _get_json()
        user_id = g.user_id if hasattr(g, 'user_id') and g.user_id else data.get('user_id')
        node_id = data.get('node_id')
        region_id = data.get('region_id')
        placement_id = data.get('placement_id')
        ip_raw = request.remote_addr or ''
        ip_hash = hashlib.sha256(ip_raw.encode()).hexdigest()[:16]
        result = AdService.record_impression(
            db, ad_id, user_id, node_id, region_id, placement_id, ip_hash)
        if not result:
            return _err('Ad not found', 404)
        if 'error' in result:
            return _err(result['error'], 429)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/ads/<ad_id>/click', methods=['POST'])
@optional_auth
def record_ad_click(ad_id):
    from .ad_service import AdService
    import hashlib
    db = get_db()
    try:
        data = _get_json()
        user_id = g.user_id if hasattr(g, 'user_id') and g.user_id else data.get('user_id')
        node_id = data.get('node_id')
        ip_raw = request.remote_addr or ''
        ip_hash = hashlib.sha256(ip_raw.encode()).hexdigest()[:16]
        result = AdService.record_click(db, ad_id, user_id, node_id, ip_hash)
        if not result:
            return _err('Ad not found', 404)
        if 'error' in result:
            return _err(result['error'], 429)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@gamification_bp.route('/ads/mine', methods=['GET'])
@require_auth
def list_my_ads():
    from .ad_service import AdService
    db = get_db()
    try:
        status = request.args.get('status')
        limit = min(int(request.args.get('limit', 25)), 100)
        offset = int(request.args.get('offset', 0))
        ads = AdService.list_my_ads(db, g.user_id, status, limit, offset)
        return _ok(ads)
    finally:
        db.close()


@gamification_bp.route('/ads/<ad_id>/analytics', methods=['GET'])
@require_auth
def ad_analytics(ad_id):
    from .ad_service import AdService
    db = get_db()
    try:
        result = AdService.get_analytics(db, ad_id, g.user_id)
        if not result:
            return _err('Ad not found', 404)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/ads/<ad_id>', methods=['DELETE'])
@require_auth
def delete_ad(ad_id):
    from .ad_service import AdService
    db = get_db()
    try:
        result = AdService.delete_ad(db, ad_id, g.user_id)
        if not result:
            return _err('Ad not found', 404)
        db.commit()
        return _ok(result)
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# HOSTING REWARDS (3 endpoints)
# ═══════════════════════════════════════════════════════════════

@gamification_bp.route('/hosting/rewards', methods=['GET'])
@require_auth
def hosting_rewards():
    from .hosting_reward_service import HostingRewardService
    db = get_db()
    try:
        node_id = request.args.get('node_id')
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))
        rewards = HostingRewardService.get_rewards(
            db, node_id=node_id, operator_id=g.user_id, limit=limit, offset=offset)
        summary = None
        if node_id:
            summary = HostingRewardService.get_reward_summary(db, node_id)
        return _ok({'rewards': rewards, 'summary': summary})
    finally:
        db.close()


@gamification_bp.route('/hosting/leaderboard', methods=['GET'])
@optional_auth
def hosting_leaderboard():
    from .hosting_reward_service import HostingRewardService
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        offset = int(request.args.get('offset', 0))
        result = HostingRewardService.get_leaderboard(db, limit, offset)
        return _ok(result)
    finally:
        db.close()


@gamification_bp.route('/hosting/compute-rewards', methods=['POST'])
@require_admin
def compute_hosting_rewards():
    from .hosting_reward_service import HostingRewardService
    db = get_db()
    try:
        period_days = int(request.args.get('period_days', 7))
        scores = HostingRewardService.compute_all_scores(db, period_days)
        # Distribute uptime bonuses and check milestones for each node
        bonuses = []
        milestones = []
        for s in scores:
            bonus = HostingRewardService.distribute_uptime_bonus(db, s['node_id'])
            if bonus:
                bonuses.append(bonus)
            milestone = HostingRewardService.check_milestones(db, s['node_id'])
            if milestone:
                milestones.append(milestone)
        db.commit()
        return _ok({
            'scores_computed': len(scores),
            'uptime_bonuses': len(bonuses),
            'milestones_awarded': len(milestones),
            'details': scores,
        })
    except Exception as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()
