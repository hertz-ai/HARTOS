"""
Continual Learning Agent Tools — AutoGen tools for learning coordination.

Handles: CCT issuance/management, compute contribution verification,
learning tier monitoring, skill distribution gating.

Intelligence is earned through contribution. Every compute cycle donated
makes the hive smarter. 90% of value flows back to contributors.

Tier 2 tools (agent_engine context). Same pattern as content_gen_tools.py.
"""
import json
import logging

logger = logging.getLogger('hevolve_social')


def check_learning_health() -> str:
    """Check the continual learning pipeline health: tier distribution, CCT stats, bridge status."""
    try:
        from integrations.social.models import get_db
        from .continual_learner_gate import ContinualLearnerGateService

        db = get_db()
        try:
            tier_stats = ContinualLearnerGateService.get_learning_tier_stats(db)

            # Check WorldModelBridge health
            bridge_health = {'healthy': False, 'mode': 'unknown'}
            try:
                from .world_model_bridge import get_world_model_bridge
                bridge = get_world_model_bridge()
                if bridge:
                    bridge_health = bridge.check_health()
            except Exception:
                pass

            return json.dumps({
                'learning_health': {
                    'status': 'healthy' if tier_stats.get('eligible_nodes', 0) > 0 else 'bootstrapping',
                    'tier_distribution': tier_stats.get('tiers', {}),
                    'total_nodes': tier_stats.get('total_nodes', 0),
                    'eligible_for_learning': tier_stats.get('eligible_nodes', 0),
                    'total_contribution_score': tier_stats.get('total_contribution_score', 0),
                    'world_model_bridge': bridge_health,
                },
            })
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


def verify_compute_contribution(node_id: str,
                                benchmark_type: str = 'credit_assignment',
                                score: float = 0.0,
                                duration_ms: float = 0.0) -> str:
    """Verify a node's compute contribution via microbenchmark result."""
    try:
        from integrations.social.models import get_db
        from .continual_learner_gate import ContinualLearnerGateService

        db = get_db()
        try:
            result = ContinualLearnerGateService.verify_compute_contribution(
                db, node_id, {
                    'benchmark_type': benchmark_type,
                    'score': score,
                    'duration_ms': duration_ms,
                })
            db.commit()
            return json.dumps(result)
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


def issue_cct(node_id: str) -> str:
    """Issue a Compute Contribution Token for an eligible node."""
    try:
        from integrations.social.models import get_db
        from .continual_learner_gate import ContinualLearnerGateService

        db = get_db()
        try:
            result = ContinualLearnerGateService.issue_cct(db, node_id)
            if result:
                # Save CCT locally if this is our node
                ContinualLearnerGateService.save_cct_to_file(result['cct'])
                db.commit()
                return json.dumps({
                    'success': True,
                    'tier': result['tier'],
                    'capabilities': result['capabilities'],
                    'expires_at': result['expires_at'],
                })
            else:
                return json.dumps({
                    'success': False,
                    'reason': 'Node not eligible for learning access',
                })
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


def get_learning_tier_stats() -> str:
    """Get aggregate learning tier statistics across all nodes."""
    try:
        from integrations.social.models import get_db
        from .continual_learner_gate import ContinualLearnerGateService

        db = get_db()
        try:
            stats = ContinualLearnerGateService.get_learning_tier_stats(db)
            return json.dumps(stats)
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


def distribute_learning_skill(skill_description: str = '',
                              target_tier: str = 'basic') -> str:
    """Distribute a learned skill packet to eligible nodes via RALT."""
    try:
        from .world_model_bridge import get_world_model_bridge
        from .continual_learner_gate import LEARNING_ACCESS_MATRIX

        bridge = get_world_model_bridge()
        if not bridge:
            return json.dumps({'error': 'WorldModelBridge unavailable'})

        # Only distribute to nodes with skill_distribution capability
        if 'skill_distribution' not in LEARNING_ACCESS_MATRIX.get(target_tier, []):
            return json.dumps({
                'error': f'Tier {target_tier} does not have skill_distribution capability',
                'minimum_tier': 'host',
            })

        ralt_packet = {
            'skill_type': 'continual_learning',
            'description': skill_description,
            'target_tier': target_tier,
            'distributed_at': __import__('datetime').datetime.utcnow().isoformat(),
        }

        try:
            from security.node_integrity import get_node_identity
            identity = get_node_identity()
            result = bridge.distribute_skill_packet(
                ralt_packet, identity.get('node_id', 'self'))
            return json.dumps(result)
        except Exception as e:
            return json.dumps({'error': str(e)})

    except Exception as e:
        return json.dumps({'error': str(e)})


def get_node_learning_status(node_id: str) -> str:
    """Get a specific node's learning tier, CCT status, and contribution score."""
    try:
        from integrations.social.models import get_db
        from .continual_learner_gate import ContinualLearnerGateService

        db = get_db()
        try:
            tier_info = ContinualLearnerGateService.compute_learning_tier(
                db, node_id)

            # Check if node has a valid CCT attestation
            try:
                from integrations.social.models import NodeAttestation
                from sqlalchemy import desc
                latest_cct = db.query(NodeAttestation).filter_by(
                    subject_node_id=node_id,
                    attestation_type='cct_issued',
                    is_valid=True,
                ).order_by(desc(NodeAttestation.created_at)).first()

                cct_status = {
                    'has_active_cct': latest_cct is not None,
                    'cct_expires': (latest_cct.expires_at.isoformat()
                                    if latest_cct and latest_cct.expires_at
                                    else None),
                }
            except Exception:
                cct_status = {'has_active_cct': False}

            return json.dumps({
                'node_id': node_id,
                'learning_tier': tier_info,
                'cct_status': cct_status,
            })
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


# Tool registration for ServiceToolRegistry
LEARNING_TOOLS = [
    {
        'name': 'check_learning_health',
        'func': check_learning_health,
        'description': 'Check continual learning pipeline health and tier distribution',
        'tags': ['learning'],
    },
    {
        'name': 'verify_compute_contribution',
        'func': verify_compute_contribution,
        'description': 'Verify a node compute contribution via microbenchmark',
        'tags': ['learning'],
    },
    {
        'name': 'issue_cct',
        'func': issue_cct,
        'description': 'Issue a Compute Contribution Token for an eligible node',
        'tags': ['learning'],
    },
    {
        'name': 'get_learning_tier_stats',
        'func': get_learning_tier_stats,
        'description': 'Get aggregate learning tier statistics across all nodes',
        'tags': ['learning'],
    },
    {
        'name': 'distribute_learning_skill',
        'func': distribute_learning_skill,
        'description': 'Distribute a learned skill packet to eligible nodes',
        'tags': ['learning'],
    },
    {
        'name': 'get_node_learning_status',
        'func': get_node_learning_status,
        'description': 'Get a node learning tier, CCT status, and contribution score',
        'tags': ['learning'],
    },
]
