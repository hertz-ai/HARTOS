"""
HevolveSocial - 3-Tier Hierarchy Service

Manages registration of regional/local nodes, region assignment,
tier-aware gossip targets, and capacity reporting.

Tiers: central -> regional -> local (flat = backward-compatible)
"""
import logging
from datetime import datetime
from typing import Optional, Dict, List

from sqlalchemy.orm import Session
from sqlalchemy import func

logger = logging.getLogger('hevolve_social')


class HierarchyService:
    """Static-only service for 3-tier hierarchy operations."""

    # ─── Registration (central-only) ───

    @staticmethod
    def register_regional_host(
        db: Session,
        node_id: str,
        public_key_hex: str,
        region_name: str,
        compute_info: dict,
        certificate: dict,
    ) -> Dict:
        """Register a regional host. Called on central node.

        Verifies certificate chain, creates PeerNode + Region entries.
        """
        from .models import PeerNode, Region

        # Verify certificate
        from security.key_delegation import verify_certificate_chain
        chain_result = verify_certificate_chain(certificate)
        if not chain_result['valid']:
            return {'registered': False,
                    'error': f'Invalid certificate: {chain_result["details"]}'}

        if certificate.get('tier') != 'regional':
            return {'registered': False,
                    'error': 'Certificate tier must be regional'}

        # Upsert PeerNode
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            peer = PeerNode(
                node_id=node_id,
                url=compute_info.get('url', ''),
                name=compute_info.get('name', f'regional-{node_id[:8]}'),
                version=compute_info.get('version', ''),
                status='active',
            )
            db.add(peer)

        peer.tier = 'regional'
        peer.public_key = public_key_hex
        peer.certificate_json = certificate
        peer.certificate_verified = True
        peer.compute_cpu_cores = compute_info.get('cpu_cores')
        peer.compute_ram_gb = compute_info.get('ram_gb')
        peer.compute_gpu_count = compute_info.get('gpu_count')
        peer.max_user_capacity = compute_info.get('max_users', 0)
        peer.dns_region = compute_info.get('dns_region', '')
        db.flush()

        # Upsert Region
        region = db.query(Region).filter_by(name=region_name).first()
        if not region:
            region = Region(
                name=region_name,
                display_name=region_name.replace('-', ' ').title(),
                region_type='geographic',
            )
            db.add(region)
            db.flush()

        region.host_node_id = node_id
        region.capacity_cpu = compute_info.get('cpu_cores')
        region.capacity_ram_gb = compute_info.get('ram_gb')
        region.capacity_gpu = compute_info.get('gpu_count')
        region.central_approved = True
        region.is_accepting_nodes = True
        region.global_server_url = compute_info.get('url', '')
        db.flush()

        logger.info(f"Regional host registered: {node_id[:8]} in {region_name}")
        return {
            'registered': True,
            'node_id': node_id,
            'region_id': region.id,
            'region_name': region_name,
        }

    @staticmethod
    def register_local_node(
        db: Session,
        node_id: str,
        public_key_hex: str,
        compute_info: dict,
        geo_info: dict = None,
    ) -> Dict:
        """Register a local node and auto-assign to a region.

        Called on central. Returns region assignment.
        """
        from .models import PeerNode

        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            peer = PeerNode(
                node_id=node_id,
                url=compute_info.get('url', ''),
                name=compute_info.get('name', f'local-{node_id[:8]}'),
                version=compute_info.get('version', ''),
                status='active',
            )
            db.add(peer)

        peer.tier = 'local'
        peer.public_key = public_key_hex
        peer.compute_cpu_cores = compute_info.get('cpu_cores')
        peer.compute_ram_gb = compute_info.get('ram_gb')
        peer.compute_gpu_count = compute_info.get('gpu_count')
        peer.dns_region = (geo_info or {}).get('dns_region', '')
        db.flush()

        # Auto-assign to best region
        assignment = HierarchyService.assign_to_region(
            db, node_id, compute_info, geo_info or {})

        return {
            'registered': True,
            'node_id': node_id,
            'assignment': assignment,
        }

    # ─── Region Assignment (central-only) ───

    @staticmethod
    def assign_to_region(
        db: Session,
        local_node_id: str,
        compute_info: dict,
        geo_info: dict,
    ) -> Dict:
        """Auto-assign a local node to the best regional host.

        Scoring: compute_headroom*0.4 + user_headroom*0.3 + geo_proximity*0.2 + dns_match*0.1
        """
        from .models import PeerNode, Region, RegionAssignment

        # Find all accepting regional hosts
        regionals = db.query(PeerNode).filter(
            PeerNode.tier == 'regional',
            PeerNode.status == 'active',
        ).all()

        if not regionals:
            return {'assigned': False, 'error': 'No regional hosts available'}

        # Get regions for accepting check
        best_score = -1
        best_regional = None
        best_region = None
        node_dns = geo_info.get('dns_region', '') or ''

        for regional in regionals:
            region = db.query(Region).filter_by(
                host_node_id=regional.node_id).first()
            if region and not region.is_accepting_nodes:
                continue

            score = 0.0

            # Compute headroom (0.4 weight)
            if regional.max_user_capacity and regional.max_user_capacity > 0:
                used_pct = (regional.active_user_count or 0) / regional.max_user_capacity
                score += (1.0 - min(used_pct, 1.0)) * 0.4

            # User headroom (0.3 weight)
            if regional.max_user_capacity and regional.max_user_capacity > 0:
                remaining = regional.max_user_capacity - (regional.active_user_count or 0)
                headroom = min(remaining / max(regional.max_user_capacity, 1), 1.0)
                score += headroom * 0.3

            # DNS match (0.1 weight)
            if node_dns and regional.dns_region and node_dns == regional.dns_region:
                score += 0.1

            # Geo proximity (0.2 weight) — simple: same dns_region prefix
            if node_dns and regional.dns_region:
                # Compare first segment (e.g., 'us' from 'us-east-1')
                node_prefix = node_dns.split('-')[0] if '-' in node_dns else node_dns
                reg_prefix = regional.dns_region.split('-')[0] if '-' in regional.dns_region else regional.dns_region
                if node_prefix == reg_prefix:
                    score += 0.2

            if score > best_score:
                best_score = score
                best_regional = regional
                best_region = region

        if not best_regional:
            return {'assigned': False, 'error': 'No suitable regional host found'}

        # Create assignment
        assignment = RegionAssignment(
            local_node_id=local_node_id,
            regional_node_id=best_regional.node_id,
            region_id=best_region.id if best_region else None,
            assigned_by='central_auto',
            status='active',
            approved_at=datetime.utcnow(),
            approved_by_central=True,
            compute_snapshot=compute_info,
        )
        db.add(assignment)

        # Update peer's assignment
        peer = db.query(PeerNode).filter_by(node_id=local_node_id).first()
        if peer:
            peer.region_assignment_id = assignment.id
            peer.parent_node_id = best_regional.node_id

        # Atomic increment — prevents race when multiple nodes assigned concurrently
        db.query(PeerNode).filter_by(node_id=best_regional.node_id).update(
            {PeerNode.active_user_count: func.coalesce(PeerNode.active_user_count, 0) + 1}
        )
        db.flush()

        return {
            'assigned': True,
            'regional_node_id': best_regional.node_id,
            'regional_url': best_regional.url,
            'region_id': best_region.id if best_region else None,
            'region_name': best_region.name if best_region else None,
            'assignment_id': assignment.id,
        }

    @staticmethod
    def switch_region(
        db: Session,
        local_node_id: str,
        new_region_id: str,
        requester: str,
    ) -> Dict:
        """Switch a local node to a different region."""
        from .models import PeerNode, Region, RegionAssignment

        new_region = db.query(Region).filter_by(id=new_region_id).first()
        if not new_region:
            return {'switched': False, 'error': 'Region not found'}

        if not new_region.is_accepting_nodes:
            return {'switched': False, 'error': 'Region not accepting nodes'}

        new_regional = db.query(PeerNode).filter_by(
            node_id=new_region.host_node_id).first()
        if not new_regional:
            return {'switched': False, 'error': 'Regional host not found'}

        # Revoke old assignment
        old = db.query(RegionAssignment).filter_by(
            local_node_id=local_node_id, status='active').first()
        if old:
            old.status = 'revoked'
            # Decrement old regional user count
            old_regional = db.query(PeerNode).filter_by(
                node_id=old.regional_node_id).first()
            if old_regional and (old_regional.active_user_count or 0) > 0:
                old_regional.active_user_count -= 1

        # Create new assignment
        assignment = RegionAssignment(
            local_node_id=local_node_id,
            regional_node_id=new_regional.node_id,
            region_id=new_region_id,
            assigned_by=requester,
            status='active',
            approved_at=datetime.utcnow(),
            approved_by_central=True,
        )
        db.add(assignment)

        # Update peer
        peer = db.query(PeerNode).filter_by(node_id=local_node_id).first()
        if peer:
            peer.region_assignment_id = assignment.id
            peer.parent_node_id = new_regional.node_id

        new_regional.active_user_count = (new_regional.active_user_count or 0) + 1
        db.flush()

        return {
            'switched': True,
            'regional_node_id': new_regional.node_id,
            'regional_url': new_regional.url,
            'region_id': new_region_id,
            'assignment_id': assignment.id,
        }

    # ─── Tier-Aware Gossip Targets ───

    @staticmethod
    def get_gossip_targets(
        db: Session,
        node_id: str,
        tier: str,
    ) -> List[Dict]:
        """Return appropriate gossip targets based on node tier.

        - central: only regional peers
        - regional: central + own local nodes
        - local: assigned regional only
        - flat: all peers (backward compat)
        """
        from .models import PeerNode, RegionAssignment

        if tier == 'flat':
            peers = db.query(PeerNode).filter(
                PeerNode.status != 'dead',
                PeerNode.node_id != node_id,
            ).all()
            return [p.to_dict() for p in peers]

        if tier == 'central':
            peers = db.query(PeerNode).filter(
                PeerNode.tier == 'regional',
                PeerNode.status == 'active',
            ).all()
            return [p.to_dict() for p in peers]

        if tier == 'regional':
            targets = []
            # Central nodes
            centrals = db.query(PeerNode).filter(
                PeerNode.tier == 'central',
                PeerNode.status == 'active',
            ).all()
            targets.extend([p.to_dict() for p in centrals])

            # Own local nodes
            locals_ = db.query(PeerNode).filter(
                PeerNode.parent_node_id == node_id,
                PeerNode.tier == 'local',
                PeerNode.status == 'active',
            ).all()
            targets.extend([p.to_dict() for p in locals_])
            return targets

        if tier == 'local':
            # Find assigned regional
            assignment = db.query(RegionAssignment).filter_by(
                local_node_id=node_id, status='active').first()
            if assignment:
                regional = db.query(PeerNode).filter_by(
                    node_id=assignment.regional_node_id).first()
                if regional:
                    return [regional.to_dict()]
            return []

        return []

    # ─── Health & Capacity ───

    @staticmethod
    def report_node_capacity(
        db: Session,
        node_id: str,
        compute_info: dict,
    ) -> Dict:
        """Update a node's compute capacity info."""
        from .models import PeerNode

        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            return {'updated': False, 'error': 'Node not found'}

        peer.compute_cpu_cores = compute_info.get('cpu_cores', peer.compute_cpu_cores)
        peer.compute_ram_gb = compute_info.get('ram_gb', peer.compute_ram_gb)
        peer.compute_gpu_count = compute_info.get('gpu_count', peer.compute_gpu_count)
        peer.active_user_count = compute_info.get('active_users', peer.active_user_count)
        peer.max_user_capacity = compute_info.get('max_users', peer.max_user_capacity)
        db.flush()

        return {'updated': True, 'node_id': node_id}

    @staticmethod
    def get_region_health(db: Session, region_id: str) -> Optional[Dict]:
        """Get health/load info for a region."""
        from .models import Region, PeerNode, RegionAssignment

        region = db.query(Region).filter_by(id=region_id).first()
        if not region:
            return None

        # Get host node info
        host = None
        if region.host_node_id:
            host = db.query(PeerNode).filter_by(
                node_id=region.host_node_id).first()

        # Count assigned local nodes
        local_count = db.query(RegionAssignment).filter_by(
            region_id=region_id, status='active').count()

        return {
            'region': region.to_dict(),
            'host_status': host.status if host else 'unknown',
            'host_url': host.url if host else None,
            'local_node_count': local_count,
            'is_accepting': region.is_accepting_nodes,
            'capacity_cpu': region.capacity_cpu,
            'capacity_ram_gb': region.capacity_ram_gb,
            'current_load_pct': region.current_load_pct,
        }
