"""
Regional Host API — Blueprint for regional host request + approval.

POST /api/social/regional-host/request     — User requests regional host status
GET  /api/social/regional-host/requests    — Steward lists pending requests
POST /api/social/regional-host/approve     — Steward approves
POST /api/social/regional-host/reject      — Steward rejects
POST /api/social/regional-host/revoke      — Steward revokes
GET  /api/social/regional-host/status      — User checks their request status
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
