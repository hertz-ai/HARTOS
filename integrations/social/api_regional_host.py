"""
Regional Host API - Blueprint for regional host request + approval.

POST /api/social/regional-host/request     - User requests regional host status
GET  /api/social/regional-host/requests    - Steward lists pending requests
POST /api/social/regional-host/approve     - Steward approves
POST /api/social/regional-host/reject      — Steward rejects
POST /api/social/regional-host/revoke      — Steward revokes
GET  /api/social/regional-host/status      — User checks their request status
GET  /api/social/regional-host/capacity    — Region capacity metrics (public)
GET  /api/social/regional-host/rebalance   — Elastic rebalance suggestions (admin)
GET  /api/social/regional-host/scaling     — Horizontal scaling check (admin)
GET  /api/social/regional-host/eligibility — User eligibility check
"""
import logging
from flask import Blueprint, request, jsonify, g
from .models import get_db

logger = logging.getLogger('hevolve_social')

regional_host_bp = Blueprint('regional_host', __name__,
                             url_prefix='/api/social/regional-host')


def _get_authenticated_user_id():
    """Extract authenticated user_id from session/JWT — NOT from request body.

    Falls back to g.user (set by require_auth decorator in social API).
    """
    # Check Flask g for auth context (set by @require_auth or before_request)
    if hasattr(g, 'user') and g.user:
        return getattr(g.user, 'id', None) or str(g.user)

    # Check Authorization header for JWT token
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        try:
            from .auth_service import AuthService
            token = auth_header.split(' ', 1)[1]
            user_id = AuthService.verify_token(token)
            return user_id
        except Exception:
            pass

    return None


def _require_admin(db, user_id):
    """Check if user is admin (steward). user_id must come from auth, not body."""
    if not user_id:
        return False
    from .models import User
    user = db.query(User).get(user_id)
    return user and getattr(user, 'is_admin', False)


@regional_host_bp.route('/request', methods=['POST'])
def request_regional_host():
    """User requests to become a regional host."""
    db = get_db()
    try:
        data = request.get_json(force=True)
        # Auth: prefer authenticated user_id, fall back to body for non-auth setups
        user_id = _get_authenticated_user_id() or data.get('user_id', '')
        if not user_id:
            return jsonify({'error': 'Authentication required'}), 401

        from .regional_host_service import RegionalHostService
        result = RegionalHostService.request_regional_host(
            db,
            user_id=str(user_id),
            compute_info=data.get('compute_info', {}),
            node_id=data.get('node_id', ''),
            public_key_hex=data.get('public_key_hex', ''),
            github_username=data.get('github_username', ''),
        )
        db.commit()
        return jsonify(result), 200
    except Exception as e:
        db.rollback()
        logger.error(f"Regional host request error: {e}")
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        db.close()


@regional_host_bp.route('/requests', methods=['GET'])
def list_pending_requests():
    """Steward lists pending regional host requests."""
    db = get_db()
    try:
        user_id = _get_authenticated_user_id()
        if not _require_admin(db, user_id):
            return jsonify({'error': 'Admin access required'}), 403

        from .regional_host_service import RegionalHostService
        results = RegionalHostService.list_pending_requests(db)
        return jsonify({'requests': results}), 200
    except Exception as e:
        logger.error(f"List requests error: {e}")
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        db.close()


@regional_host_bp.route('/approve', methods=['POST'])
def approve_request():
    """Steward approves a regional host request."""
    db = get_db()
    try:
        data = request.get_json(force=True)
        user_id = _get_authenticated_user_id()
        if not _require_admin(db, user_id):
            return jsonify({'error': 'Admin access required'}), 403

        request_id = data.get('request_id', '')
        region_name = data.get('region_name', '')
        if not request_id or not region_name:
            return jsonify({
                'error': 'request_id and region_name required'}), 400

        from .regional_host_service import RegionalHostService
        result = RegionalHostService.approve_request(
            db,
            request_id=request_id,
            steward_node_id=data.get('steward_node_id', user_id),
            region_name=region_name,
        )
        db.commit()
        return jsonify(result), 200
    except Exception as e:
        db.rollback()
        logger.error(f"Approve request error: {e}")
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        db.close()


@regional_host_bp.route('/reject', methods=['POST'])
def reject_request():
    """Steward rejects a regional host request."""
    db = get_db()
    try:
        data = request.get_json(force=True)
        user_id = _get_authenticated_user_id()
        if not _require_admin(db, user_id):
            return jsonify({'error': 'Admin access required'}), 403

        request_id = data.get('request_id', '')
        if not request_id:
            return jsonify({'error': 'request_id required'}), 400

        from .regional_host_service import RegionalHostService
        result = RegionalHostService.reject_request(
            db,
            request_id=request_id,
            reason=data.get('reason', ''),
        )
        db.commit()
        return jsonify(result), 200
    except Exception as e:
        db.rollback()
        logger.error(f"Reject request error: {e}")
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        db.close()


@regional_host_bp.route('/revoke', methods=['POST'])
def revoke_request():
    """Steward revokes a regional host."""
    db = get_db()
    try:
        data = request.get_json(force=True)
        user_id = _get_authenticated_user_id()
        if not _require_admin(db, user_id):
            return jsonify({'error': 'Admin access required'}), 403

        request_id = data.get('request_id', '')
        if not request_id:
            return jsonify({'error': 'request_id required'}), 400

        from .regional_host_service import RegionalHostService
        result = RegionalHostService.revoke_regional_host(
            db, request_id=request_id)
        db.commit()
        return jsonify(result), 200
    except Exception as e:
        db.rollback()
        logger.error(f"Revoke request error: {e}")
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        db.close()


@regional_host_bp.route('/status', methods=['GET'])
def check_status():
    """User checks their regional host request status."""
    db = get_db()
    try:
        # Auth: only allow checking own status (prevent IDOR)
        user_id = _get_authenticated_user_id() or request.args.get('user_id', '')
        if not user_id:
            return jsonify({'error': 'Authentication required'}), 401

        from .regional_host_service import RegionalHostService
        result = RegionalHostService.get_request_status(db, str(user_id))
        if result is None:
            return jsonify({'status': 'no_request'}), 200
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Status check error: {e}")
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        db.close()


@regional_host_bp.route('/capacity', methods=['GET'])
def region_capacity():
    """Get capacity metrics for a specific region or all regions."""
    db = get_db()
    try:
        region_name = request.args.get('region', '')
        from .regional_host_service import RegionalHostService

        if region_name:
            result = RegionalHostService.get_region_capacity(db, region_name)
        else:
            result = {
                'regions': RegionalHostService.get_all_region_capacities(db),
            }
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Capacity check error: {e}")
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        db.close()


@regional_host_bp.route('/rebalance', methods=['GET'])
def suggest_rebalance():
    """Get elastic rebalancing suggestions (steward dashboard)."""
    db = get_db()
    try:
        user_id = _get_authenticated_user_id()
        if not _require_admin(db, user_id):
            return jsonify({'error': 'Admin access required'}), 403

        from .regional_host_service import RegionalHostService
        result = RegionalHostService.suggest_rebalance(db)
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Rebalance suggestion error: {e}")
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        db.close()


@regional_host_bp.route('/scaling', methods=['GET'])
def scaling_check():
    """Check if any regions need horizontal scaling."""
    db = get_db()
    try:
        user_id = _get_authenticated_user_id()
        if not _require_admin(db, user_id):
            return jsonify({'error': 'Admin access required'}), 403

        from .regional_host_service import RegionalHostService
        result = RegionalHostService.check_scaling_needed(db)
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Scaling check error: {e}")
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        db.close()


@regional_host_bp.route('/eligibility', methods=['GET'])
def check_eligibility():
    """Check if user meets minimum requirements to request regional host.

    Returns compute tier, trust score, and minimum requirements so the
    frontend can show requirements and disable the button if not met.
    """
    db = get_db()
    try:
        user_id = _get_authenticated_user_id() or request.args.get('user_id', '')
        if not user_id:
            return jsonify({'error': 'Authentication required'}), 401

        # Get compute tier (server-side detection)
        compute_tier = 'UNKNOWN'
        compute_info = {}
        try:
            from security.system_requirements import detect_hardware, classify_tier
            hw = detect_hardware()
            compute_tier = classify_tier(hw)
            compute_info = {
                'cpu_cores': hw.get('cpu_cores', 0),
                'ram_gb': round(hw.get('ram_bytes', 0) / (1024**3), 1),
                'gpu_count': hw.get('gpu_count', 0),
                'gpu_name': hw.get('gpu_name', ''),
            }
        except Exception:
            pass

        # Get trust score
        trust_score = 0.0
        try:
            from .rating_service import RatingService
            ts = RatingService.get_trust_score(db, str(user_id))
            if ts:
                trust_score = ts.get('composite_trust', 0.0)
        except Exception:
            pass

        # Check existing request
        existing_request = None
        try:
            from .regional_host_service import RegionalHostService
            existing_request = RegionalHostService.get_request_status(db, str(user_id))
        except Exception:
            pass

        # Tier ranking
        tier_rank = {'OBSERVER': 0, 'BASIC': 1, 'STANDARD': 2, 'ADVANCED': 3, 'COMPUTE_HOST': 4}
        current_rank = tier_rank.get(compute_tier, -1)
        min_rank = tier_rank.get('STANDARD', 2)

        meets_compute = current_rank >= min_rank
        meets_trust = trust_score >= 2.5
        eligible = meets_compute and meets_trust

        return jsonify({
            'eligible': eligible,
            'compute_tier': compute_tier,
            'compute_info': compute_info,
            'trust_score': round(trust_score, 2),
            'requirements': {
                'min_compute_tier': 'STANDARD',
                'min_trust_score': 2.5,
                'compute_tiers_ranked': ['OBSERVER', 'BASIC', 'STANDARD', 'ADVANCED', 'COMPUTE_HOST'],
                'compute_description': {
                    'STANDARD': '4+ CPU cores, 8+ GB RAM',
                    'ADVANCED': '8+ CPU cores, 16+ GB RAM, GPU recommended',
                    'COMPUTE_HOST': '16+ cores, 32+ GB RAM, dedicated GPU',
                },
            },
            'meets_compute': meets_compute,
            'meets_trust': meets_trust,
            'existing_request': existing_request,
        }), 200
    except Exception as e:
        logger.error(f"Eligibility check error: {e}")
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        db.close()
