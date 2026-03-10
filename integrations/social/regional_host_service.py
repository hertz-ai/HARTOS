"""
Regional Host Service - Hybrid Approval for Regional Host Onboarding

Users request regional host status via UI. The system auto-qualifies based on
compute tier (>= STANDARD) and trust score (>= 2.5). A steward then one-click
approves, which triggers certificate issuance, GitHub repo invite, and
registration in the hierarchy.

All methods follow the static service pattern: receive db: Session, return Dict.
"""
import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve_social')

# Minimum requirements for regional host qualification
MIN_COMPUTE_TIER = 'STANDARD'
MIN_TRUST_SCORE = 2.5

# Tier ranking for comparison
_TIER_RANK = {
    'OBSERVER': 0,
    'BASIC': 1,
    'STANDARD': 2,
    'ADVANCED': 3,
    'COMPUTE_HOST': 4,
}


class RegionalHostService:
    """Static service for regional host application + hybrid approval."""

    @staticmethod
    def request_regional_host(
        db, user_id: str, compute_info: dict,
        node_id: str = '', public_key_hex: str = '',
        github_username: str = '',
    ) -> Dict:
        """User clicks 'Request Regional Host' in UI.

        1. Check compute tier via system_requirements → must be >= STANDARD
        2. Check trust score → must be >= 2.5 composite_trust
        3. If qualifies: create RegionalHostRequest(status='pending_steward')
        4. If not: return {qualified: False, reason: ...}
        """
        from .models import RegionalHostRequest

        # Check for existing pending/approved request
        existing = db.query(RegionalHostRequest).filter(
            RegionalHostRequest.user_id == user_id,
            RegionalHostRequest.status.in_([
                'pending', 'pending_steward', 'approved']),
        ).first()
        if existing:
            return {
                'qualified': existing.status == 'approved',
                'request_id': existing.id,
                'status': existing.status,
                'reason': 'Request already exists',
            }

        # Detect compute tier — server-side only (never trust client claims)
        compute_tier = 'UNKNOWN'
        try:
            from security.system_requirements import (
                detect_hardware, classify_tier)
            hw = detect_hardware()
            # NOTE: client compute_info is stored for display but NEVER
            # used to override server-detected hardware for tier classification
            compute_tier = classify_tier(hw)
        except Exception as e:
            logger.debug(f"Compute tier detection failed: {e}")
            # Do NOT fall back to client-provided tier — unknown is safer
            compute_tier = 'UNKNOWN'

        # Check trust score
        trust_score = 0.0
        try:
            from .rating_service import RatingService
            ts = RatingService.get_trust_score(db, user_id)
            if ts:
                trust_score = ts.get('composite_trust', 0.0)
        except Exception:
            pass

        # Evaluate qualification
        reasons = []
        tier_rank = _TIER_RANK.get(compute_tier, -1)
        min_rank = _TIER_RANK.get(MIN_COMPUTE_TIER, 2)
        if tier_rank < min_rank:
            reasons.append(
                f'Compute tier {compute_tier} below minimum '
                f'{MIN_COMPUTE_TIER}')
        if trust_score < MIN_TRUST_SCORE:
            reasons.append(
                f'Trust score {trust_score:.2f} below minimum '
                f'{MIN_TRUST_SCORE}')

        qualified = len(reasons) == 0
        status = 'pending_steward' if qualified else 'rejected'

        request = RegionalHostRequest(
            user_id=user_id,
            node_id=node_id or None,
            public_key_hex=public_key_hex or None,
            compute_tier=compute_tier,
            compute_info_json=json.dumps(compute_info),
            trust_score=trust_score,
            status=status,
            github_username=github_username or None,
            requested_at=datetime.utcnow(),
            rejected_reason='; '.join(reasons) if reasons else None,
        )
        db.add(request)
        db.flush()

        return {
            'qualified': qualified,
            'request_id': request.id,
            'status': status,
            'compute_tier': compute_tier,
            'trust_score': trust_score,
            'reason': '; '.join(reasons) if reasons else 'Auto-qualified',
        }

    @staticmethod
    def approve_request(
        db, request_id: str, steward_node_id: str,
        region_name: str,
    ) -> Dict:
        """Steward one-click approval.

        1. Issue certificate via key_delegation
        2. Trigger GitHub repo invite (coding agent)
        3. Register regional host via hierarchy_service
        """
        from .models import RegionalHostRequest

        request = db.query(RegionalHostRequest).get(request_id)
        if not request:
            return {'approved': False, 'error': 'Request not found'}
        if request.status == 'approved':
            return {'approved': True, 'request_id': request_id,
                    'reason': 'Already approved'}
        if request.status not in ('pending_steward', 'pending'):
            return {'approved': False,
                    'error': f'Cannot approve from status: {request.status}'}

        # Issue certificate — requires valid public key and this node's private key
        certificate = None
        if not request.public_key_hex:
            logger.warning("Cannot issue certificate: no public_key_hex on request")
        else:
            try:
                from security.key_delegation import create_child_certificate
                from security.node_integrity import get_node_identity
                identity = get_node_identity()
                parent_private_key = identity.get('_private_key')
                if not parent_private_key:
                    logger.warning("Cannot issue certificate: no node private key")
                else:
                    cert = create_child_certificate(
                        parent_private_key=parent_private_key,
                        child_public_key_hex=request.public_key_hex,
                        node_id=request.node_id or request.user_id,
                        tier='regional',
                        region_name=region_name,
                    )
                    certificate = cert
                    request.certificate_json = json.dumps(cert)
            except Exception as e:
                logger.warning(f"Certificate issuance failed: {e}")

        # Send GitHub repo invite via coding agent
        invite_sent = False
        if request.github_username:
            try:
                from integrations.agent_engine.private_repo_access import (
                    PrivateRepoAccessService)
                repos = os.environ.get(
                    'HEVOLVE_PRIVATE_REPOS', '').split(',')
                for repo_url in repos:
                    repo_url = repo_url.strip()
                    if repo_url:
                        result = PrivateRepoAccessService.send_github_invite(
                            repo_url, request.github_username,
                            permission='push')
                        if result.get('invited'):
                            invite_sent = True
                request.github_invite_sent = invite_sent
            except Exception as e:
                logger.debug(f"GitHub invite failed: {e}")

        # Register in hierarchy
        try:
            from .hierarchy_service import HierarchyService
            HierarchyService.register_regional_host(
                db,
                node_id=request.node_id or request.user_id,
                region_name=region_name,
                public_key_hex=request.public_key_hex,
            )
        except Exception as e:
            logger.debug(f"Hierarchy registration failed: {e}")

        request.status = 'approved'
        request.region_name = region_name
        request.approved_at = datetime.utcnow()
        request.approved_by = steward_node_id
        db.flush()

        # Push tier_promote fleet command to the node so it auto-reloads as regional
        cmd_pushed = False
        target_node = request.node_id or ''
        if target_node:
            try:
                from .fleet_command import FleetCommandService
                FleetCommandService.push_command(
                    db, target_node, 'tier_promote',
                    params={
                        'new_tier': 'regional',
                        'region_name': region_name,
                        'env_vars': {
                            'HEVOLVE_NODE_TIER': 'regional',
                        },
                        'restart_required': True,
                    },
                    issued_by=steward_node_id,
                )
                cmd_pushed = True
            except Exception as e:
                logger.debug(f"Fleet command push after approval failed: {e}")

        return {
            'approved': True,
            'request_id': request_id,
            'certificate': certificate,
            'invite_sent': invite_sent,
            'region_name': region_name,
            'fleet_command_pushed': cmd_pushed,
        }

    @staticmethod
    def reject_request(
        db, request_id: str, reason: str = '',
    ) -> Dict:
        """Steward rejects a request."""
        from .models import RegionalHostRequest

        request = db.query(RegionalHostRequest).get(request_id)
        if not request:
            return {'rejected': False, 'error': 'Request not found'}

        request.status = 'rejected'
        request.rejected_reason = reason or 'Rejected by steward'
        db.flush()
        return {'rejected': True, 'request_id': request_id}

    @staticmethod
    def revoke_regional_host(
        db, request_id: str,
    ) -> Dict:
        """Revoke certificate + remove GitHub collaborator + downgrade."""
        from .models import RegionalHostRequest

        request = db.query(RegionalHostRequest).get(request_id)
        if not request:
            return {'revoked': False, 'error': 'Request not found'}

        # Revoke GitHub access
        if request.github_username and request.github_invite_sent:
            try:
                from integrations.agent_engine.private_repo_access import (
                    PrivateRepoAccessService)
                repos = os.environ.get(
                    'HEVOLVE_PRIVATE_REPOS', '').split(',')
                for repo_url in repos:
                    repo_url = repo_url.strip()
                    if repo_url:
                        PrivateRepoAccessService.revoke_github_access(
                            repo_url, request.github_username)
            except Exception as e:
                logger.debug(f"GitHub access revocation failed: {e}")

        request.status = 'revoked'
        request.github_invite_sent = False
        request.certificate_json = None
        db.flush()

        # Push tier_demote fleet command so the node auto-reloads as flat
        target_node = request.node_id or ''
        if target_node:
            try:
                from .fleet_command import FleetCommandService
                FleetCommandService.push_command(
                    db, target_node, 'tier_demote',
                    params={
                        'new_tier': 'flat',
                        'reason': 'Regional host certificate revoked',
                        'env_vars': {
                            'HEVOLVE_NODE_TIER': 'flat',
                        },
                        'restart_required': True,
                    },
                )
            except Exception as e:
                logger.debug(f"Fleet command push after revoke failed: {e}")

        return {'revoked': True, 'request_id': request_id}

    @staticmethod
    def list_pending_requests(db) -> List[Dict]:
        """Steward dashboard: list all pending requests."""
        from .models import RegionalHostRequest

        requests = db.query(RegionalHostRequest).filter(
            RegionalHostRequest.status.in_([
                'pending', 'pending_steward']),
        ).order_by(RegionalHostRequest.requested_at.desc()).all()
        return [r.to_dict() for r in requests]

    @staticmethod
    def get_request_status(db, user_id: str) -> Optional[Dict]:
        """User checks their latest request status."""
        from .models import RegionalHostRequest

        request = db.query(RegionalHostRequest).filter_by(
            user_id=user_id,
        ).order_by(RegionalHostRequest.requested_at.desc()).first()
        if not request:
            return None
        return request.to_dict()

    @staticmethod
    def get_region_capacity(db, region_name: str) -> Dict:
        """Get region capacity metrics — current load, max capacity, health."""
        from .models import PeerNode, RegionalHostRequest

        # Find all approved regional hosts in this region
        hosts = db.query(RegionalHostRequest).filter(
            RegionalHostRequest.status == 'approved',
            RegionalHostRequest.region_name == region_name,
        ).all()

        # Find all peer nodes in this region
        nodes = db.query(PeerNode).filter(
            PeerNode.dns_region == region_name,
            PeerNode.status.in_(['active', 'online']),
        ).all()

        total_capacity = 0
        current_load = 0
        compute_cores = 0
        compute_ram_gb = 0
        gpu_count = 0

        for node in nodes:
            max_users = getattr(node, 'max_user_capacity', 50) or 50
            active_users = getattr(node, 'active_user_count', 0) or 0
            total_capacity += max_users
            current_load += active_users
            compute_cores += getattr(node, 'compute_cpu_cores', 0) or 0
            compute_ram_gb += getattr(node, 'compute_ram_gb', 0) or 0
            gpu_count += getattr(node, 'compute_gpu_count', 0) or 0

        utilization = (current_load / total_capacity * 100) if total_capacity > 0 else 0

        return {
            'region_name': region_name,
            'host_count': len(hosts),
            'active_node_count': len(nodes),
            'total_capacity': total_capacity,
            'current_load': current_load,
            'utilization_percent': round(utilization, 1),
            'compute_cores': compute_cores,
            'compute_ram_gb': round(compute_ram_gb, 1),
            'gpu_count': gpu_count,
            'status': (
                'critical' if utilization > 90 else
                'high' if utilization > 75 else
                'healthy' if utilization > 0 else
                'idle'
            ),
            'needs_scaling': utilization > 80,
        }

    @staticmethod
    def get_all_region_capacities(db) -> List[Dict]:
        """Get capacity metrics for ALL regions — used by elastic rebalancer."""
        from .models import RegionalHostRequest

        # Get unique region names
        regions = db.query(RegionalHostRequest.region_name).filter(
            RegionalHostRequest.status == 'approved',
            RegionalHostRequest.region_name.isnot(None),
        ).distinct().all()

        capacities = []
        for (region_name,) in regions:
            if region_name:
                cap = RegionalHostService.get_region_capacity(db, region_name)
                capacities.append(cap)

        return sorted(capacities, key=lambda c: c['utilization_percent'], reverse=True)

    @staticmethod
    def suggest_rebalance(db) -> Dict:
        """Elastic rebalancing: identify overloaded regions and suggest migrations.

        Returns a list of suggested user migrations from overloaded to underloaded regions.
        Does NOT execute — returns suggestions for steward approval or auto-execution.
        """
        capacities = RegionalHostService.get_all_region_capacities(db)
        if len(capacities) < 2:
            return {'suggestions': [], 'reason': 'Need at least 2 regions to rebalance'}

        overloaded = [c for c in capacities if c['utilization_percent'] > 80]
        underloaded = [c for c in capacities if c['utilization_percent'] < 50]

        suggestions = []
        for over in overloaded:
            excess = over['current_load'] - int(over['total_capacity'] * 0.7)
            if excess <= 0:
                continue

            for under in underloaded:
                available = under['total_capacity'] - under['current_load']
                if available <= 0:
                    continue

                migrate_count = min(excess, available)
                suggestions.append({
                    'from_region': over['region_name'],
                    'to_region': under['region_name'],
                    'migrate_count': migrate_count,
                    'from_utilization': over['utilization_percent'],
                    'to_utilization': under['utilization_percent'],
                    'reason': f"{over['region_name']} at {over['utilization_percent']}% → "
                              f"migrate {migrate_count} users to {under['region_name']} "
                              f"({under['utilization_percent']}%)",
                })
                excess -= migrate_count
                if excess <= 0:
                    break

        return {
            'suggestions': suggestions,
            'total_regions': len(capacities),
            'overloaded_count': len(overloaded),
            'underloaded_count': len(underloaded),
            'capacities': capacities,
        }

    @staticmethod
    def check_scaling_needed(db) -> Dict:
        """Check if any region needs horizontal scaling (more hosts).

        Called periodically by a background task or fleet command.
        Returns regions that need more hosts and optionally auto-posts
        recruitment requests.
        """
        capacities = RegionalHostService.get_all_region_capacities(db)
        scaling_needed = []

        for cap in capacities:
            if cap['utilization_percent'] > 80:
                scaling_needed.append({
                    'region_name': cap['region_name'],
                    'utilization_percent': cap['utilization_percent'],
                    'current_hosts': cap['host_count'],
                    'current_load': cap['current_load'],
                    'total_capacity': cap['total_capacity'],
                    'action': 'recruit_hosts' if cap['host_count'] < 3 else 'scale_compute',
                    'recommended_additional_hosts': max(1, (cap['current_load'] - int(cap['total_capacity'] * 0.6)) // 50),
                })
            elif cap['utilization_percent'] < 10 and cap['host_count'] > 1:
                scaling_needed.append({
                    'region_name': cap['region_name'],
                    'utilization_percent': cap['utilization_percent'],
                    'current_hosts': cap['host_count'],
                    'current_load': cap['current_load'],
                    'total_capacity': cap['total_capacity'],
                    'action': 'consolidate',
                    'reason': 'Very low utilization — consider consolidating hosts',
                })

        return {
            'scaling_needed': scaling_needed,
            'total_regions': len(capacities),
        }
