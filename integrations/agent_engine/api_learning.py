"""
Continual Learning API Blueprint — CCT management and learning tier endpoints.

POST /api/learning/cct/request     — Node requests initial CCT
POST /api/learning/cct/renew       — Renew expiring CCT
GET  /api/learning/cct/status      — Check current CCT status
POST /api/learning/cct/verify      — Verify any CCT (public)
GET  /api/learning/tiers           — Tier distribution stats
GET  /api/learning/contributions   — Contribution leaderboard
POST /api/learning/benchmark       — Submit compute benchmark
"""
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger('hevolve_social')

learning_bp = Blueprint('learning', __name__)


def _verify_node_signature(body: dict) -> bool:
    """Verify Ed25519 signature on request body (same as gossip announce)."""
    try:
        node_id = body.get('node_id', '')
        signature = body.get('signature', '')
        public_key = body.get('public_key', '')
        if not all([node_id, signature, public_key]):
            return False
        from security.node_integrity import verify_json_signature
        payload = {k: v for k, v in body.items() if k != 'signature'}
        return verify_json_signature(public_key, payload, signature)
    except Exception:
        return False


@learning_bp.route('/api/learning/cct/request', methods=['POST'])
def request_cct():
    """Node requests a new Compute Contribution Token."""
    from integrations.social.models import get_db
    from .continual_learner_gate import ContinualLearnerGateService

    body = request.get_json(silent=True) or {}
    node_id = body.get('node_id')
    if not node_id:
        return jsonify({'success': False, 'error': 'node_id required'}), 400

    if not _verify_node_signature(body):
        return jsonify({'success': False, 'error': 'invalid_signature'}), 403

    db = get_db()
    try:
        result = ContinualLearnerGateService.issue_cct(db, node_id)
        if result:
            db.commit()
            return jsonify({'success': True, 'data': result}), 200
        else:
            tier_info = ContinualLearnerGateService.compute_learning_tier(
                db, node_id)
            return jsonify({
                'success': False,
                'error': 'Not eligible for learning access',
                'tier_info': tier_info,
            }), 403
    except Exception as e:
        db.rollback()
        logger.error(f"CCT request error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@learning_bp.route('/api/learning/cct/renew', methods=['POST'])
def renew_cct():
    """Renew an existing CCT. Re-validates eligibility."""
    from integrations.social.models import get_db
    from .continual_learner_gate import ContinualLearnerGateService

    body = request.get_json(silent=True) or {}
    node_id = body.get('node_id')
    old_cct = body.get('cct')
    if not node_id:
        return jsonify({'success': False, 'error': 'node_id required'}), 400

    if not _verify_node_signature(body):
        return jsonify({'success': False, 'error': 'invalid_signature'}), 403

    db = get_db()
    try:
        result = ContinualLearnerGateService.renew_cct(db, node_id, old_cct)
        if result:
            db.commit()
            return jsonify({'success': True, 'data': result}), 200
        else:
            return jsonify({
                'success': False,
                'error': 'No longer eligible for learning access',
            }), 403
    except Exception as e:
        db.rollback()
        logger.error(f"CCT renewal error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@learning_bp.route('/api/learning/cct/status', methods=['GET'])
def cct_status():
    """Check current node's CCT status."""
    from integrations.social.models import get_db
    from .continual_learner_gate import ContinualLearnerGateService

    node_id = request.args.get('node_id')
    if not node_id:
        return jsonify({'success': False, 'error': 'node_id required'}), 400

    db = get_db()
    try:
        tier_info = ContinualLearnerGateService.compute_learning_tier(
            db, node_id)

        # Check latest CCT attestation
        cct_info = {'has_active_cct': False}
        try:
            from integrations.social.models import NodeAttestation
            from sqlalchemy import desc
            latest = db.query(NodeAttestation).filter_by(
                subject_node_id=node_id,
                attestation_type='cct_issued',
                is_valid=True,
            ).order_by(desc(NodeAttestation.created_at)).first()
            if latest:
                cct_info = {
                    'has_active_cct': True,
                    'issued_at': (latest.created_at.isoformat()
                                  if latest.created_at else None),
                    'expires_at': (latest.expires_at.isoformat()
                                   if latest.expires_at else None),
                    'tier': (latest.payload_json or {}).get('tier', 'unknown'),
                }
        except Exception:
            pass

        return jsonify({
            'success': True,
            'data': {
                'tier_info': tier_info,
                'cct': cct_info,
            },
        }), 200
    except Exception as e:
        logger.error(f"CCT status error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@learning_bp.route('/api/learning/cct/verify', methods=['POST'])
def verify_cct():
    """Verify any CCT (public endpoint — no auth required)."""
    from .continual_learner_gate import ContinualLearnerGateService

    body = request.get_json(silent=True) or {}
    cct = body.get('cct')
    expected_node = body.get('node_id')
    if not cct:
        return jsonify({'success': False, 'error': 'cct required'}), 400

    result = ContinualLearnerGateService.validate_cct(cct, expected_node)
    return jsonify({'success': True, 'data': result}), 200


@learning_bp.route('/api/learning/tiers', methods=['GET'])
def tier_stats():
    """Get learning tier distribution across all nodes."""
    from integrations.social.models import get_db
    from .continual_learner_gate import ContinualLearnerGateService

    db = get_db()
    try:
        stats = ContinualLearnerGateService.get_learning_tier_stats(db)
        return jsonify({'success': True, 'data': stats}), 200
    except Exception as e:
        logger.error(f"Tier stats error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@learning_bp.route('/api/learning/contributions', methods=['GET'])
def contribution_leaderboard():
    """Top compute contributors by contribution_score."""
    from integrations.social.models import get_db

    limit = request.args.get('limit', 50, type=int)
    db = get_db()
    try:
        from integrations.social.models import PeerNode
        from sqlalchemy import desc
        peers = db.query(PeerNode).filter(
            PeerNode.status.in_(['active', 'stale']),
            PeerNode.contribution_score > 0,
        ).order_by(
            desc(PeerNode.contribution_score)
        ).limit(min(limit, 200)).all()

        leaderboard = []
        for i, peer in enumerate(peers, 1):
            leaderboard.append({
                'rank': i,
                'node_id': peer.node_id,
                'contribution_score': round(peer.contribution_score or 0, 2),
                'capability_tier': peer.capability_tier,
                'integrity_status': peer.integrity_status,
                'visibility_tier': peer.visibility_tier,
            })

        return jsonify({'success': True, 'data': leaderboard}), 200
    except Exception as e:
        logger.error(f"Contribution leaderboard error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@learning_bp.route('/api/learning/benchmark', methods=['POST'])
def submit_benchmark():
    """Submit a compute benchmark result for verification."""
    from integrations.social.models import get_db
    from .continual_learner_gate import ContinualLearnerGateService

    body = request.get_json(silent=True) or {}
    node_id = body.get('node_id')
    if not node_id:
        return jsonify({'success': False, 'error': 'node_id required'}), 400

    if not _verify_node_signature(body):
        return jsonify({'success': False, 'error': 'invalid_signature'}), 403

    benchmark_result = {
        'benchmark_type': body.get('benchmark_type', 'unknown'),
        'score': body.get('score', 0),
        'duration_ms': body.get('duration_ms', 0),
        'hardware_info': body.get('hardware_info', {}),
    }

    db = get_db()
    try:
        result = ContinualLearnerGateService.verify_compute_contribution(
            db, node_id, benchmark_result)
        db.commit()
        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        db.rollback()
        logger.error(f"Benchmark submit error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()
