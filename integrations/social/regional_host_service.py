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

        return {
            'approved': True,
            'request_id': request_id,
            'certificate': certificate,
            'invite_sent': invite_sent,
            'region_name': region_name,
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
