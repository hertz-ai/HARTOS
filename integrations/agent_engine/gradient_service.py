"""
Gradient Service — Distributed embedding synchronization service.

Service Pattern: static methods, db: Session, db.flush() not db.commit().

Submit embedding deltas, aggregate across peers, request witnesses,
track convergence. Integrates with CCT (embedding_sync capability),
IntegrityService (fraud detection), and FederatedAggregator (gossip).

Phase 1: Embedding delta sync (compressed, <100KB, trimmed mean).
Phase 2: LoRA gradient sync (stubs in federated_gradient_protocol.py).
"""
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve_social')

# ─── Constants ───

GRADIENT_ROUND_DURATION_SECONDS = 300  # 5 min per aggregation round
MAX_DELTAS_PER_ROUND = 200            # Max deltas stored per round
WITNESS_REQUIREMENT = 2                # Min witnesses for embedding delta
CONVERGENCE_WINDOW = 50                # Rounds to track for convergence trend


class GradientSyncService:
    """Manages distributed embedding delta submission, aggregation, and witnessing."""

    # ─── Delta Submission ───

    @staticmethod
    def submit_embedding_delta(db, node_id: str, delta: Dict,
                                cct_string: str = None) -> Dict:
        """Submit a compressed embedding delta from a node.

        Validates: CCT (embedding_sync capability), delta format, magnitude,
        direction. Stores as NodeAttestation and feeds to FederatedAggregator.

        Returns: {'accepted': bool, 'reason': str, 'attestation_id': str}
        """
        # 1. Validate CCT has embedding_sync capability
        if cct_string:
            try:
                from .continual_learner_gate import ContinualLearnerGateService
                if not ContinualLearnerGateService.check_cct_capability(
                        cct_string, 'embedding_sync', node_id):
                    return {'accepted': False, 'reason': 'cct_no_embedding_sync'}
            except Exception:
                pass  # If CCT check unavailable, allow (graceful degrade)
        else:
            # Check tier directly from DB
            try:
                from .continual_learner_gate import (
                    ContinualLearnerGateService, LEARNING_ACCESS_MATRIX)
                tier_info = ContinualLearnerGateService.compute_learning_tier(
                    db, node_id)
                tier = tier_info.get('tier', 'none')
                if 'embedding_sync' not in LEARNING_ACCESS_MATRIX.get(tier, []):
                    return {'accepted': False, 'reason': 'tier_insufficient',
                            'current_tier': tier, 'required': 'full'}
            except Exception:
                pass

        # 2. Validate delta format
        try:
            from .embedding_delta import validate_delta
            valid, reason = validate_delta(delta)
            if not valid:
                return {'accepted': False, 'reason': f'invalid_delta: {reason}'}
        except ImportError:
            return {'accepted': False, 'reason': 'embedding_delta_module_unavailable'}

        # 3. Magnitude anomaly check
        anomaly_detected = False
        try:
            from .embedding_delta import detect_magnitude_anomaly
            magnitude = delta.get('magnitude', 0.0)
            peer_magnitudes = GradientSyncService._get_peer_magnitudes(db)
            if peer_magnitudes:
                anomaly_detected = detect_magnitude_anomaly(
                    magnitude, peer_magnitudes)
        except Exception:
            pass

        if anomaly_detected:
            # Record fraud signal but still accept (IntegrityService handles banning)
            try:
                GradientSyncService._record_gradient_fraud(
                    db, node_id, 'gradient_magnitude_anomaly',
                    {'magnitude': delta.get('magnitude', 0)})
            except Exception:
                pass
            return {'accepted': False, 'reason': 'magnitude_anomaly'}

        # 4. Direction flip check (vs previous delta from this node)
        direction_flipped = False
        try:
            from .embedding_delta import detect_direction_flip, decompress_delta
            previous = GradientSyncService._get_previous_delta(db, node_id)
            if previous:
                current_vals = decompress_delta(delta)
                prev_vals = decompress_delta(previous)
                direction_flipped = detect_direction_flip(
                    current_vals, prev_vals)
        except Exception:
            pass

        if direction_flipped:
            try:
                GradientSyncService._record_gradient_fraud(
                    db, node_id, 'gradient_direction_flip',
                    {'delta_dimension': delta.get('dimension', 0)})
            except Exception:
                pass
            return {'accepted': False, 'reason': 'direction_flip'}

        # 5. Store as NodeAttestation
        attestation_id = None
        try:
            from integrations.social.models import NodeAttestation
            from security.node_integrity import (
                get_public_key_hex, sign_json_payload, get_node_identity)

            identity = get_node_identity()
            evidence = {
                'delta_method': delta.get('method', 'unknown'),
                'delta_dimension': delta.get('dimension', 0),
                'delta_k': delta.get('k', 0),
                'magnitude': delta.get('magnitude', 0),
                'submitted_at': datetime.utcnow().isoformat(),
            }
            sig = sign_json_payload(evidence)

            attestation = NodeAttestation(
                attester_node_id=identity.get('node_id', 'self'),
                subject_node_id=node_id,
                attestation_type='embedding_delta',
                payload_json={
                    'evidence': evidence,
                    'delta': delta,  # Store compressed delta for replay
                },
                signature=sig[:256],
                attester_public_key=get_public_key_hex(),
                is_valid=True,
                expires_at=datetime.utcnow() + timedelta(hours=1),
            )
            db.add(attestation)
            db.flush()
            attestation_id = attestation.id
        except ImportError:
            logger.debug("Cannot store embedding delta attestation: imports unavailable")
        except Exception as e:
            logger.debug(f"Embedding delta attestation failed: {e}")

        # 6. Feed to FederatedAggregator
        try:
            from .federated_aggregator import get_federated_aggregator
            aggregator = get_federated_aggregator()
            aggregator.receive_embedding_delta(node_id, delta)
        except Exception as e:
            logger.debug(f"Aggregator feed failed: {e}")

        return {
            'accepted': True,
            'attestation_id': attestation_id,
            'reason': 'ok',
        }

    # ─── Aggregation Status ───

    @staticmethod
    def get_convergence_status(db) -> Dict:
        """Get current embedding sync convergence status.

        Returns: {'epoch': int, 'peer_count': int, 'convergence_score': float,
                  'deltas_this_round': int, 'round_duration': int}
        """
        try:
            from .federated_aggregator import get_federated_aggregator
            aggregator = get_federated_aggregator()
            stats = aggregator.get_stats()

            # Count embedding deltas in current round
            delta_count = 0
            try:
                from integrations.social.models import NodeAttestation
                cutoff = datetime.utcnow() - timedelta(
                    seconds=GRADIENT_ROUND_DURATION_SECONDS)
                delta_count = db.query(NodeAttestation).filter(
                    NodeAttestation.attestation_type == 'embedding_delta',
                    NodeAttestation.is_valid == True,
                    NodeAttestation.created_at >= cutoff,
                ).count()
            except Exception:
                pass

            # Embedding-specific stats from aggregator
            embedding_stats = {}
            try:
                embedding_stats = aggregator.get_embedding_stats()
            except AttributeError:
                pass  # Aggregator doesn't have embedding channel yet

            return {
                'epoch': stats.get('epoch', 0),
                'peer_count': stats.get('peer_count', 0),
                'convergence_score': stats.get('convergence', 0.0),
                'deltas_this_round': delta_count,
                'round_duration_seconds': GRADIENT_ROUND_DURATION_SECONDS,
                'embedding_sync': embedding_stats,
            }
        except Exception as e:
            return {'epoch': 0, 'peer_count': 0, 'convergence_score': 0.0,
                    'error': str(e)}

    # ─── Witness Request ───

    @staticmethod
    def request_embedding_witnesses(db, delta: Dict,
                                     node_id: str) -> Dict:
        """Request peer witnesses for an embedding delta.

        Uses IntegrityService witness pattern: need WITNESS_REQUIREMENT+ peers
        to validate. Returns witness request status.
        """
        try:
            from integrations.social.models import PeerNode

            # Find eligible witness peers (active, verified, different node)
            witnesses = db.query(PeerNode).filter(
                PeerNode.status == 'active',
                PeerNode.integrity_status == 'verified',
                PeerNode.node_id != node_id,
            ).limit(WITNESS_REQUIREMENT * 2).all()

            if len(witnesses) < WITNESS_REQUIREMENT:
                return {
                    'witnessed': False,
                    'reason': 'insufficient_peers',
                    'available': len(witnesses),
                    'required': WITNESS_REQUIREMENT,
                }

            # Request witnesses via gossip
            witness_ids = []
            for peer in witnesses[:WITNESS_REQUIREMENT]:
                try:
                    from core.http_pool import pooled_post
                    url = f"{peer.url.rstrip('/')}/api/social/peers/embedding-delta"
                    witness_payload = {
                        'action': 'witness_request',
                        'delta': delta,
                        'submitter_node_id': node_id,
                        'request_id': uuid.uuid4().hex[:12],
                    }
                    resp = pooled_post(url, json=witness_payload, timeout=5)
                    if resp.status_code == 200:
                        witness_ids.append(peer.node_id)
                except Exception:
                    pass

            return {
                'witnessed': len(witness_ids) >= WITNESS_REQUIREMENT,
                'witness_count': len(witness_ids),
                'witness_ids': witness_ids,
                'required': WITNESS_REQUIREMENT,
            }
        except Exception as e:
            return {'witnessed': False, 'reason': str(e)}

    # ─── Internal Helpers ───

    @staticmethod
    def _get_peer_magnitudes(db) -> List[float]:
        """Get recent embedding delta magnitudes from all peers."""
        try:
            from integrations.social.models import NodeAttestation
            cutoff = datetime.utcnow() - timedelta(
                seconds=GRADIENT_ROUND_DURATION_SECONDS)
            attestations = db.query(NodeAttestation).filter(
                NodeAttestation.attestation_type == 'embedding_delta',
                NodeAttestation.is_valid == True,
                NodeAttestation.created_at >= cutoff,
            ).all()

            magnitudes = []
            for att in attestations:
                payload = att.payload_json or {}
                evidence = payload.get('evidence', payload)
                mag = evidence.get('magnitude', 0.0)
                if isinstance(mag, (int, float)) and mag > 0:
                    magnitudes.append(mag)
            return magnitudes
        except Exception:
            return []

    @staticmethod
    def _get_previous_delta(db, node_id: str) -> Optional[Dict]:
        """Get the most recent embedding delta from this node."""
        try:
            from integrations.social.models import NodeAttestation
            from sqlalchemy import desc
            att = db.query(NodeAttestation).filter_by(
                subject_node_id=node_id,
                attestation_type='embedding_delta',
                is_valid=True,
            ).order_by(desc(NodeAttestation.created_at)).first()

            if att and att.payload_json:
                return att.payload_json.get('delta')
        except Exception:
            pass
        return None

    @staticmethod
    def _record_gradient_fraud(db, node_id: str, signal_type: str,
                                details: dict):
        """Record a gradient fraud signal via IntegrityService."""
        try:
            from integrations.social.models import FraudAlert
            alert = FraudAlert(
                node_id=node_id,
                alert_type=signal_type,
                severity='medium',
                details_json=details,
            )
            db.add(alert)
            db.flush()
            logger.warning(f"Gradient fraud signal: {signal_type} "
                          f"for node {node_id}")
        except Exception as e:
            logger.debug(f"Fraud signal record failed: {e}")
