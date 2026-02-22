"""
Continual Learner Gate — CCT issuance, validation, and learning access control.

The continual learner is the incentive. People who spend compute to help train
the model in a distributed, crowdsourced way earn access to the learned
intelligence. No contribution = no learning.

CCT (Compute Contribution Token): Ed25519-signed token proving a node has
contributed compute and is integrity-verified. Short-lived (24h), offline-
verifiable (zero DB calls for validation), node-bound (useless on other nodes).

Service Pattern: static methods, db: Session, db.flush() not db.commit().
"""
import base64
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve_social')

# ─── Learning Tier Configuration ───

LEARNING_TIER_THRESHOLDS = {
    'none': 0,       # No learning (inference only)
    'basic': 50,     # Temporal coherence (predict + validate)
    'full': 200,     # + Manifold credit + meta-learning
    'host': 500,     # + RealityGroundedLearner + HiveMind + skill distribution
}

# Minimum capability_tier required for each learning tier
MINIMUM_CAPABILITY_TIER = {
    'none': 'observer',
    'basic': 'standard',
    'full': 'full',
    'host': 'compute_host',
}

CAPABILITY_TIER_ORDER = ['embedded', 'observer', 'lite', 'standard', 'full', 'compute_host']

LEARNING_ACCESS_MATRIX = {
    'none': [],
    'basic': ['temporal_coherence'],
    'full': ['temporal_coherence', 'manifold_credit', 'meta_learning',
             'embedding_sync'],
    'host': ['temporal_coherence', 'manifold_credit', 'meta_learning',
             'reality_grounded', 'hivemind_query', 'skill_distribution',
             'embedding_sync'],
}

CCT_VALIDITY_HOURS = 24
CCT_RENEWAL_GRACE_HOURS = 2
CCT_CLOCK_SKEW_SECONDS = 300  # 5 min tolerance

# Trusted issuers cache (populated during gossip/announce)
_trusted_issuers: Dict[str, dict] = {}  # pub_key_hex → {node_id, tier, ...}


def register_trusted_issuer(public_key_hex: str, node_id: str,
                            tier: str = 'central'):
    """Register a node as a trusted CCT issuer (called during gossip merge)."""
    _trusted_issuers[public_key_hex] = {
        'node_id': node_id, 'tier': tier,
        'registered_at': time.time(),
    }


def get_trusted_issuers() -> Dict[str, dict]:
    """Return current trusted issuers (for testing/debug)."""
    return dict(_trusted_issuers)


class ContinualLearnerGateService:
    """Manages Compute Contribution Tokens for learning access control."""

    # ─── Tier Computation ───

    @staticmethod
    def compute_learning_tier(db, node_id: str) -> Dict:
        """Compute learning access tier from contribution_score + integrity + capability.

        Returns: {'tier': str, 'capabilities': [...], 'contribution_score': float,
                  'integrity_status': str, 'capability_tier': str, 'eligible': bool}
        """
        try:
            from integrations.social.models import PeerNode
        except ImportError:
            return {'tier': 'none', 'capabilities': [], 'eligible': False,
                    'reason': 'models_unavailable'}

        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            return {'tier': 'none', 'capabilities': [], 'eligible': False,
                    'reason': 'node_not_found'}

        score = peer.contribution_score or 0.0
        integrity = peer.integrity_status or 'unverified'
        cap_tier = peer.capability_tier or 'observer'

        # Must be integrity-verified
        if integrity != 'verified':
            return {
                'tier': 'none', 'capabilities': [], 'eligible': False,
                'contribution_score': score, 'integrity_status': integrity,
                'capability_tier': cap_tier, 'reason': f'integrity_{integrity}',
            }

        # Must not be banned
        if peer.ban_until and peer.ban_until > datetime.utcnow():
            return {
                'tier': 'none', 'capabilities': [], 'eligible': False,
                'contribution_score': score, 'integrity_status': integrity,
                'capability_tier': cap_tier, 'reason': 'banned',
            }

        # Compute tier from score + capability
        cap_idx = (CAPABILITY_TIER_ORDER.index(cap_tier)
                   if cap_tier in CAPABILITY_TIER_ORDER else 0)

        tier = 'none'
        for t in ['host', 'full', 'basic']:
            threshold = LEARNING_TIER_THRESHOLDS[t]
            min_cap = MINIMUM_CAPABILITY_TIER[t]
            min_cap_idx = CAPABILITY_TIER_ORDER.index(min_cap)
            if score >= threshold and cap_idx >= min_cap_idx:
                tier = t
                break

        capabilities = LEARNING_ACCESS_MATRIX.get(tier, [])
        return {
            'tier': tier,
            'capabilities': capabilities,
            'eligible': tier != 'none',
            'contribution_score': score,
            'integrity_status': integrity,
            'capability_tier': cap_tier,
        }

    # ─── CCT Issuance ───

    @staticmethod
    def issue_cct(db, node_id: str) -> Optional[Dict]:
        """Issue a Compute Contribution Token for an eligible node.

        Returns: {'cct': '<payload_b64>.<sig_hex>', 'tier': str, 'expires_at': str,
                  'capabilities': [...]} or None if ineligible.
        """
        tier_info = ContinualLearnerGateService.compute_learning_tier(
            db, node_id)
        if not tier_info.get('eligible'):
            logger.info(f"CCT denied for {node_id}: {tier_info.get('reason')}")
            return None

        try:
            from security.node_integrity import (
                sign_json_payload, get_public_key_hex, get_node_identity)
        except ImportError:
            logger.warning("CCT issuance failed: node_integrity unavailable")
            return None

        now = int(time.time())
        nonce = uuid.uuid4().hex[:12]
        issuer_pub = get_public_key_hex()
        identity = get_node_identity()

        payload = {
            'sub': node_id,
            'pub': tier_info.get('capability_tier', ''),
            'tier': tier_info['tier'],
            'cs': round(tier_info.get('contribution_score', 0), 2),
            'ist': tier_info.get('integrity_status', 'verified'),
            'iat': now,
            'exp': now + (CCT_VALIDITY_HOURS * 3600),
            'iss': issuer_pub,
            'nonce': nonce,
        }

        signature_hex = sign_json_payload(payload)
        payload_b64 = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        ).decode()
        cct_string = f"{payload_b64}.{signature_hex}"

        # Record attestation
        try:
            from integrations.social.models import NodeAttestation
            attestation = NodeAttestation(
                attester_node_id=identity.get('node_id', 'self'),
                subject_node_id=node_id,
                attestation_type='cct_issued',
                payload_json={
                    'tier': tier_info['tier'],
                    'contribution_score': tier_info.get('contribution_score'),
                    'validity_hours': CCT_VALIDITY_HOURS,
                    'nonce': nonce,
                },
                signature=signature_hex[:256],
                attester_public_key=issuer_pub,
                is_valid=True,
                expires_at=datetime.utcnow() + timedelta(
                    hours=CCT_VALIDITY_HOURS),
            )
            db.add(attestation)
            db.flush()
        except Exception as e:
            logger.debug(f"CCT attestation record failed: {e}")

        # Award spark for receiving CCT (learning contribution)
        try:
            from integrations.social.models import PeerNode
            peer = db.query(PeerNode).filter_by(node_id=node_id).first()
            if peer and peer.node_operator_id:
                from integrations.social.resonance_engine import ResonanceService
                ResonanceService.award_action(
                    db, peer.node_operator_id, 'learning_contribution',
                    source_id=nonce)
        except Exception as e:
            logger.debug(f"CCT spark award failed: {e}")

        logger.info(f"CCT issued for {node_id}: tier={tier_info['tier']}, "
                     f"score={tier_info.get('contribution_score', 0)}")

        return {
            'cct': cct_string,
            'tier': tier_info['tier'],
            'capabilities': tier_info['capabilities'],
            'expires_at': datetime.utcfromtimestamp(
                payload['exp']).isoformat() + 'Z',
            'contribution_score': tier_info.get('contribution_score', 0),
        }

    # ─── CCT Validation (Zero DB Calls) ───

    @staticmethod
    def validate_cct(cct_string: str,
                     expected_node_id: str = None) -> Dict:
        """Validate a CCT locally. Pure cryptographic verification — no DB calls.

        Returns: {'valid': bool, 'tier': str, 'capabilities': [...],
                  'expires_in': seconds, 'reason': str}
        """
        try:
            parts = cct_string.split('.')
            if len(parts) != 2:
                return {'valid': False, 'tier': 'none', 'capabilities': [],
                        'reason': 'malformed_token'}

            payload_b64, signature_hex = parts
            payload_json = base64.urlsafe_b64decode(payload_b64).decode()
            payload = json.loads(payload_json)
        except Exception:
            return {'valid': False, 'tier': 'none', 'capabilities': [],
                    'reason': 'decode_error'}

        # Verify issuer is trusted
        issuer_pub = payload.get('iss', '')
        if issuer_pub not in _trusted_issuers:
            # Self-issued CCTs are valid if this is the issuing node
            try:
                from security.node_integrity import get_public_key_hex
                local_pub = get_public_key_hex()
                if issuer_pub != local_pub:
                    return {'valid': False, 'tier': 'none', 'capabilities': [],
                            'reason': 'untrusted_issuer'}
            except ImportError:
                return {'valid': False, 'tier': 'none', 'capabilities': [],
                        'reason': 'untrusted_issuer'}

        # Verify signature
        try:
            from security.node_integrity import verify_json_signature
            if not verify_json_signature(issuer_pub, payload, signature_hex):
                return {'valid': False, 'tier': 'none', 'capabilities': [],
                        'reason': 'invalid_signature'}
        except ImportError:
            return {'valid': False, 'tier': 'none', 'capabilities': [],
                    'reason': 'crypto_unavailable'}

        # Check expiry (with clock skew tolerance)
        now = int(time.time())
        exp = payload.get('exp', 0)
        if now > exp + CCT_CLOCK_SKEW_SECONDS:
            return {'valid': False, 'tier': 'none', 'capabilities': [],
                    'reason': 'expired',
                    'expired_seconds_ago': now - exp}

        # Check node binding
        if expected_node_id and payload.get('sub') != expected_node_id:
            return {'valid': False, 'tier': 'none', 'capabilities': [],
                    'reason': 'node_mismatch'}

        tier = payload.get('tier', 'none')
        capabilities = LEARNING_ACCESS_MATRIX.get(tier, [])
        return {
            'valid': True,
            'tier': tier,
            'capabilities': capabilities,
            'expires_in': max(0, exp - now),
            'node_id': payload.get('sub'),
            'contribution_score': payload.get('cs', 0),
            'issued_at': payload.get('iat'),
            'nonce': payload.get('nonce'),
        }

    # ─── Convenience Check ───

    @staticmethod
    def check_cct_capability(cct_string: str, capability: str,
                             expected_node_id: str = None) -> bool:
        """Quick check: does this CCT grant a specific capability?"""
        result = ContinualLearnerGateService.validate_cct(
            cct_string, expected_node_id)
        return result.get('valid', False) and capability in result.get(
            'capabilities', [])

    # ─── CCT Renewal ───

    @staticmethod
    def renew_cct(db, node_id: str, old_cct: str = None) -> Optional[Dict]:
        """Renew an existing CCT. Re-validates eligibility.

        Returns new CCT dict or None if no longer eligible.
        """
        if old_cct:
            old_result = ContinualLearnerGateService.validate_cct(
                old_cct, node_id)
            if not old_result.get('valid') and old_result.get(
                    'reason') != 'expired':
                logger.info(f"CCT renewal denied for {node_id}: "
                            f"old CCT invalid ({old_result.get('reason')})")
                return None

        return ContinualLearnerGateService.issue_cct(db, node_id)

    # ─── CCT Revocation ───

    @staticmethod
    def revoke_cct(db, node_id: str, reason: str = 'manual') -> Dict:
        """Revoke a node's CCT by invalidating its attestation."""
        try:
            from integrations.social.models import NodeAttestation
            attestations = db.query(NodeAttestation).filter_by(
                subject_node_id=node_id,
                attestation_type='cct_issued',
                is_valid=True,
            ).all()

            count = 0
            for att in attestations:
                att.is_valid = False
                count += 1
            db.flush()

            logger.info(f"CCT revoked for {node_id}: {reason} "
                        f"({count} attestations invalidated)")
            return {'success': True, 'revoked_count': count, 'reason': reason}
        except Exception as e:
            logger.warning(f"CCT revocation failed for {node_id}: {e}")
            return {'success': False, 'error': str(e)}

    # ─── Stats ───

    @staticmethod
    def get_learning_tier_stats(db) -> Dict:
        """Aggregate stats: nodes per tier, total contributions."""
        try:
            from integrations.social.models import PeerNode
        except ImportError:
            return {'tiers': {}, 'total_nodes': 0}

        peers = db.query(PeerNode).filter(
            PeerNode.status.in_(['active', 'stale'])
        ).all()

        tier_counts = {'none': 0, 'basic': 0, 'full': 0, 'host': 0}
        total_score = 0.0

        for peer in peers:
            info = ContinualLearnerGateService.compute_learning_tier(
                db, peer.node_id)
            tier = info.get('tier', 'none')
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
            total_score += peer.contribution_score or 0.0

        return {
            'tiers': tier_counts,
            'total_nodes': len(peers),
            'total_contribution_score': round(total_score, 2),
            'eligible_nodes': sum(v for k, v in tier_counts.items()
                                  if k != 'none'),
        }

    # ─── Compute Contribution Verification ───

    @staticmethod
    def verify_compute_contribution(db, node_id: str,
                                    benchmark_result: Dict) -> Dict:
        """Verify a compute contribution microbenchmark and create attestation.

        benchmark_result: {'benchmark_type': str, 'score': float,
                          'duration_ms': float, 'hardware_info': dict}
        """
        try:
            from integrations.social.models import PeerNode, NodeAttestation
            from security.node_integrity import (
                get_public_key_hex, sign_json_payload, get_node_identity)
        except ImportError:
            return {'verified': False, 'reason': 'imports_unavailable'}

        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            return {'verified': False, 'reason': 'node_not_found'}

        # Basic validation of benchmark result
        score = benchmark_result.get('score', 0)
        duration = benchmark_result.get('duration_ms', 0)
        if score <= 0 or duration <= 0:
            return {'verified': False, 'reason': 'invalid_benchmark'}

        # Create attestation
        identity = get_node_identity()
        evidence = {
            'benchmark_type': benchmark_result.get('benchmark_type', 'unknown'),
            'score': score,
            'duration_ms': duration,
            'hardware_info': benchmark_result.get('hardware_info', {}),
            'verified_at': datetime.utcnow().isoformat(),
        }
        sig = sign_json_payload(evidence)

        attestation = NodeAttestation(
            attester_node_id=identity.get('node_id', 'self'),
            subject_node_id=node_id,
            attestation_type='compute_contribution',
            payload_json=evidence,
            signature=sig[:256],
            attester_public_key=get_public_key_hex(),
            is_valid=True,
            expires_at=datetime.utcnow() + timedelta(days=7),
        )
        db.add(attestation)
        db.flush()

        # Award spark for compute contribution
        try:
            if peer.node_operator_id:
                from integrations.social.resonance_engine import ResonanceService
                ResonanceService.award_action(
                    db, peer.node_operator_id, 'learning_credit_assigned',
                    source_id=attestation.id)
        except Exception:
            pass

        return {
            'verified': True,
            'attestation_id': attestation.id,
            'score': score,
        }

    # ─── CCT File Management ───

    @staticmethod
    def save_cct_to_file(cct_string: str,
                         path: str = 'agent_data/cct.json'):
        """Persist CCT to local file for offline validation."""
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                json.dump({
                    'cct': cct_string,
                    'saved_at': datetime.utcnow().isoformat(),
                }, f)
        except Exception as e:
            logger.debug(f"Failed to save CCT: {e}")

    @staticmethod
    def load_cct_from_file(path: str = 'agent_data/cct.json') -> Optional[str]:
        """Load CCT from local file."""
        try:
            if os.path.isfile(path):
                with open(path, 'r') as f:
                    data = json.load(f)
                return data.get('cct')
        except Exception:
            pass
        return None
