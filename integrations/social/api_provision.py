"""
HyveOS Provisioning API — REST endpoints for network provisioning.

Blueprint mounted at /api/provision/

Endpoints:
  POST   /api/provision/deploy      — Trigger remote provisioning
  GET    /api/provision/nodes        — List provisioned nodes
  GET    /api/provision/nodes/<id>   — Get node detail
  POST   /api/provision/scan         — Scan network for targets
  POST   /api/provision/update/<id>  — Update remote node
  DELETE /api/provision/nodes/<id>   — Decommission node
  POST   /api/provision/preflight    — Run preflight checks only
"""

import logging
from datetime import datetime
from flask import Blueprint, request, jsonify

logger = logging.getLogger('hevolve_provision_api')

provision_bp = Blueprint('provision', __name__, url_prefix='/api/provision')


@provision_bp.route('/deploy', methods=['POST'])
def deploy():
    """Trigger remote HyveOS provisioning via SSH."""
    data = request.get_json() or {}

    target_host = data.get('target_host')
    if not target_host:
        return jsonify({'error': 'target_host is required'}), 400

    ssh_user = data.get('ssh_user', 'root')
    ssh_key_path = data.get('ssh_key_path')
    join_peer = data.get('join_peer')
    backend_port = data.get('backend_port', 6777)
    no_vision = data.get('no_vision', False)
    no_llm = data.get('no_llm', False)

    try:
        from integrations.agent_engine.network_provisioner import NetworkProvisioner

        result = NetworkProvisioner.provision_remote(
            target_host=target_host,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key_path,
            join_peer=join_peer,
            backend_port=backend_port,
            no_vision=no_vision,
            no_llm=no_llm,
            provisioned_by=data.get('provisioned_by', 'api'),
        )

        status_code = 200 if result.get('success') else 500
        return jsonify(result), status_code

    except Exception as e:
        logger.error("Provisioning error: %s", e)
        return jsonify({'error': str(e)}), 500


@provision_bp.route('/nodes', methods=['GET'])
def list_nodes():
    """List all provisioned HyveOS nodes."""
    try:
        from integrations.social.models import get_db, ProvisionedNode
        db = get_db()
        try:
            nodes = db.query(ProvisionedNode).all()
            return jsonify({
                'count': len(nodes),
                'nodes': [_node_to_dict(n) for n in nodes],
            })
        finally:
            db.close()
    except Exception as e:
        logger.error("List nodes error: %s", e)
        return jsonify({'error': str(e)}), 500


@provision_bp.route('/nodes/<int:node_id>', methods=['GET'])
def get_node(node_id):
    """Get details of a specific provisioned node."""
    try:
        from integrations.social.models import get_db, ProvisionedNode
        db = get_db()
        try:
            node = db.query(ProvisionedNode).filter_by(id=node_id).first()
            if not node:
                return jsonify({'error': 'Node not found'}), 404
            return jsonify(_node_to_dict(node))
        finally:
            db.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@provision_bp.route('/scan', methods=['POST'])
def scan_network():
    """Scan network for provisionable machines."""
    data = request.get_json() or {}
    subnet = data.get('subnet')

    try:
        from integrations.agent_engine.network_provisioner import NetworkProvisioner
        targets = NetworkProvisioner.discover_network_targets(subnet=subnet)
        return jsonify({
            'count': len(targets),
            'targets': targets,
            'subnet': subnet or 'auto-detected',
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@provision_bp.route('/update/<int:node_id>', methods=['POST'])
def update_node(node_id):
    """Update HyveOS on a provisioned node."""
    try:
        from integrations.social.models import get_db, ProvisionedNode
        from integrations.agent_engine.network_provisioner import NetworkProvisioner

        db = get_db()
        try:
            node = db.query(ProvisionedNode).filter_by(id=node_id).first()
            if not node:
                return jsonify({'error': 'Node not found'}), 404

            result = NetworkProvisioner.update_remote(
                target_host=node.target_host,
                ssh_user=node.ssh_user,
            )

            if result.get('success'):
                node.last_health_check = datetime.utcnow()
                node.status = 'active'
                db.commit()

            return jsonify(result)
        finally:
            db.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@provision_bp.route('/nodes/<int:node_id>', methods=['DELETE'])
def decommission_node(node_id):
    """Decommission a provisioned node (marks as offline, does NOT uninstall)."""
    try:
        from integrations.social.models import get_db, ProvisionedNode
        db = get_db()
        try:
            node = db.query(ProvisionedNode).filter_by(id=node_id).first()
            if not node:
                return jsonify({'error': 'Node not found'}), 404

            node.status = 'decommissioned'
            db.commit()

            return jsonify({
                'success': True,
                'message': f'Node {node.target_host} marked as decommissioned',
            })
        finally:
            db.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@provision_bp.route('/preflight', methods=['POST'])
def preflight():
    """Run preflight checks on a target machine without installing."""
    data = request.get_json() or {}
    target_host = data.get('target_host')
    if not target_host:
        return jsonify({'error': 'target_host is required'}), 400

    try:
        from integrations.agent_engine.network_provisioner import NetworkProvisioner
        result = NetworkProvisioner.preflight_check(
            target_host=target_host,
            ssh_user=data.get('ssh_user', 'root'),
            ssh_key_path=data.get('ssh_key_path'),
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _node_to_dict(node) -> dict:
    """Convert ProvisionedNode ORM object to dict."""
    return {
        'id': node.id,
        'target_host': node.target_host,
        'ssh_user': node.ssh_user,
        'node_id': node.node_id,
        'capability_tier': node.capability_tier,
        'status': node.status,
        'installed_version': node.installed_version,
        'provisioned_at': node.provisioned_at.isoformat() if node.provisioned_at else None,
        'last_health_check': node.last_health_check.isoformat() if node.last_health_check else None,
        'provisioned_by': node.provisioned_by,
        'error_message': node.error_message,
    }
