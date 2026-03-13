"""
Compute Borrowing Service — request, offer, and settle compute across peers.

Builds on peer_discovery.py gossip protocol and hosting_reward_service.py
contribution scoring. Peers with idle compute advertise capacity;
nodes under pressure can borrow and pay via Spark settlement.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# In-memory tracking (transient / short-lived — not persisted)
_compute_offers: Dict[str, Dict] = {}   # node_id → {resources, timestamp}
_compute_requests: Dict[str, Dict] = {}  # request_id → {node_id, task_type, ...}
# _compute_debts removed — now persisted via ComputeEscrow table


class ComputeBorrowingService:
    """Cross-node compute sharing via gossip protocol."""

    @staticmethod
    def offer_compute(db, node_id: str, available_resources: Dict) -> Dict:
        """Advertise idle compute capacity to the network.

        Args:
            node_id: Offering node
            available_resources: {cpu_pct_free, ram_gb_free, gpu_free_gb}

        Returns: Offer confirmation
        """
        offer = {
            'node_id': node_id,
            'resources': available_resources,
            'timestamp': datetime.utcnow().isoformat(),
            'status': 'available',
        }
        _compute_offers[node_id] = offer

        # Broadcast via gossip
        try:
            from integrations.social.peer_discovery import get_peer_discovery
            pd = get_peer_discovery()
            pd.gossip_broadcast({
                'type': 'compute_offer',
                'payload': offer,
            })
        except Exception as e:
            logger.debug(f"Gossip broadcast failed: {e}")

        return {'success': True, 'offer': offer}

    @staticmethod
    def request_compute(db, node_id: str, task_type: str,
                        min_resources: Dict) -> Dict:
        """Request compute from the network.

        Args:
            node_id: Requesting node
            task_type: 'inference' | 'training' | 'federation'
            min_resources: {min_cpu_pct, min_ram_gb, min_gpu_gb}

        Returns: Matched offer or no_match
        """
        # Find a matching offer
        matched = None
        for offer_id, offer in _compute_offers.items():
            if offer['status'] != 'available':
                continue
            res = offer['resources']
            if (res.get('cpu_pct_free', 0) >= min_resources.get('min_cpu_pct', 0) and
                    res.get('ram_gb_free', 0) >= min_resources.get('min_ram_gb', 0)):
                matched = offer
                break

        if not matched:
            # Broadcast request via gossip for deferred matching
            try:
                from integrations.social.peer_discovery import get_peer_discovery
                pd = get_peer_discovery()
                pd.gossip_broadcast({
                    'type': 'compute_request',
                    'payload': {
                        'requester': node_id,
                        'task_type': task_type,
                        'min_resources': min_resources,
                    },
                })
            except Exception:
                pass
            return {'matched': False, 'reason': 'no_available_offers'}

        # Reserve the offer
        matched['status'] = 'reserved'
        request_id = f"{node_id}_{matched['node_id']}_{task_type}"
        _compute_requests[request_id] = {
            'requester': node_id,
            'provider': matched['node_id'],
            'task_type': task_type,
            'resources': matched['resources'],
            'started_at': datetime.utcnow().isoformat(),
        }

        # Persist escrow record (fail-open: match succeeds even if DB write fails)
        # Estimate cost: 1 Spark per GPU-GB offered per hour (minimum 1)
        estimated_spark = max(1, int(matched.get('resources', {}).get('gpu_gb', 1)))
        try:
            from integrations.social.models import ComputeEscrow
            escrow = ComputeEscrow(
                debtor_node_id=node_id,
                creditor_node_id=matched['node_id'],
                request_id=request_id,
                task_type=task_type,
                spark_amount=estimated_spark,
                status='pending',
                created_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(hours=24),
            )
            db.add(escrow)
            db.commit()
        except Exception as e:
            logger.warning(f"ComputeEscrow write failed (fail-open): {e}")
            try:
                db.rollback()
            except Exception:
                pass

        return {
            'matched': True,
            'request_id': request_id,
            'provider': matched['node_id'],
            'resources': matched['resources'],
        }

    @staticmethod
    def settle_compute_debt(db, debtor_node_id: str,
                            creditor_node_id: str,
                            spark_amount: float) -> Dict:
        """Pay a peer for borrowed compute cycles via Spark transfer.

        Records settlement and awards Spark to the provider.
        """
        try:
            from integrations.social.models import PeerNode, ComputeEscrow
            provider = db.query(PeerNode).filter_by(
                node_id=creditor_node_id).first()
            if not provider or not provider.node_operator_id:
                return {'error': 'provider_not_found'}

            # Award Spark to provider
            from integrations.social.resonance_engine import ResonanceService
            ResonanceService.award_spark(
                db, str(provider.node_operator_id), int(spark_amount),
                'compute_borrowing_settlement', debtor_node_id,
                f'Compute settlement: {spark_amount} Spark from {debtor_node_id}')

            # Settle escrow record in DB
            remaining_debt = 0
            try:
                escrow = db.query(ComputeEscrow).filter(
                    ComputeEscrow.debtor_node_id == debtor_node_id,
                    ComputeEscrow.creditor_node_id == creditor_node_id,
                    ComputeEscrow.status == 'pending',
                ).first()
                if escrow:
                    escrow.spark_amount = int(spark_amount)
                    escrow.status = 'settled'
                    escrow.settled_at = datetime.utcnow()
                    db.commit()

                # Calculate remaining debt from any other pending escrows
                from sqlalchemy import func
                pending_sum = db.query(func.coalesce(
                    func.sum(ComputeEscrow.spark_amount), 0
                )).filter(
                    ComputeEscrow.debtor_node_id == debtor_node_id,
                    ComputeEscrow.status == 'pending',
                ).scalar()
                remaining_debt = float(pending_sum)
            except Exception as e:
                logger.warning(f"ComputeEscrow settle failed (fail-open): {e}")
                try:
                    db.rollback()
                except Exception:
                    pass

            logger.info(
                f"Compute settlement: {debtor_node_id} → {creditor_node_id} "
                f"({spark_amount} Spark)")

            return {
                'settled': True,
                'debtor': debtor_node_id,
                'creditor': creditor_node_id,
                'amount': spark_amount,
                'remaining_debt': remaining_debt,
            }
        except Exception as e:
            return {'error': str(e)}

    @staticmethod
    def get_status(db=None) -> Dict:
        """Current compute borrowing status."""
        total_debt = 0.0
        if db is not None:
            try:
                from integrations.social.models import ComputeEscrow
                from sqlalchemy import func
                pending_sum = db.query(func.coalesce(
                    func.sum(ComputeEscrow.spark_amount), 0
                )).filter(
                    ComputeEscrow.status == 'pending',
                ).scalar()
                total_debt = round(float(pending_sum), 2)
            except Exception as e:
                logger.debug(f"ComputeEscrow query failed (fail-open): {e}")
                total_debt = 0.0

        return {
            'active_offers': len([o for o in _compute_offers.values()
                                  if o['status'] == 'available']),
            'reserved_offers': len([o for o in _compute_offers.values()
                                    if o['status'] == 'reserved']),
            'active_requests': len(_compute_requests),
            'total_debt_spark': total_debt,
        }
