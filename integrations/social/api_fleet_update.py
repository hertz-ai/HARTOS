"""
Fleet OTA Update Approval Endpoint.

Regional hosts can implement approval logic to gate which versions
roll out to fleet nodes. Standalone nodes always auto-approve.
"""
from flask import Blueprint, request, jsonify

fleet_update_bp = Blueprint('fleet_update', __name__)


@fleet_update_bp.route('/api/social/fleet/update-approved', methods=['GET'])
def check_update_approved():
    """Check if a version is approved for fleet rollout.

    Query params:
        v: version string to check (e.g., '1.2.3')

    Returns:
        {approved: bool, version: str}

    Regional hosts can override this with approval lists, staged rollout
    percentages, or canary checks. Default: approve all versions.
    """
    version = request.args.get('v', '')
    # For now: approve all versions for standalone nodes
    # Regional hosts can implement approval logic later
    return jsonify({'approved': True, 'version': version})
