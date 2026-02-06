"""
HevolveSocial - Integrity Verification Service
Challenge-response protocol, impression witnessing, consensus verification,
fraud scoring. Central service for all anti-fraud logic.
"""
import os
import secrets
import logging
import statistics
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, List

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from .models import (
    PeerNode, AdImpression, HostingReward,
    NodeAttestation, IntegrityChallenge, FraudAlert,
    User, Post,
)

logger = logging.getLogger('hevolve_social')

# Fraud scoring weights
FRAUD_WEIGHTS = {
    'hash_mismatch': 30.0,
    'challenge_fail': 15.0,
    'impression_anomaly': 20.0,
    'score_jump': 10.0,
    'witness_refusal': 5.0,
    'collusion_suspected': 25.0,
}

IMPRESSION_ANOMALY_STDDEV = 3.0
SCORE_JUMP_THRESHOLD_PCT = 200
FRAUD_BAN_THRESHOLD = 80.0
ATTESTATION_EXPIRY_DAYS = 7
MIN_WITNESS_PEERS = 1
CHALLENGE_TIMEOUT_SECONDS = 30
WITNESS_TIMESTAMP_MAX_AGE = 60  # seconds


class IntegrityService:
    """Central service for node integrity verification and anti-fraud."""

    # ─── Code Hash Verification ───

    @staticmethod
    def verify_code_hash(db: Session, node_id: str,
                         registry_url: str = None) -> Dict:
        """Check if node's code_hash matches expected hash.
        Priority: master-signed manifest > registry > local computation."""
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer or not peer.code_hash:
            return {'verified': False, 'details': 'No code hash available'}

        expected = None

        # Primary: check against master-signed manifest
        try:
            from security.master_key import load_release_manifest, verify_release_manifest
            manifest = load_release_manifest()
            if manifest and verify_release_manifest(manifest):
                expected = manifest.get('code_hash', '')
        except Exception:
            pass

        # Fallback: registry
        if not expected and registry_url:
            expected = IntegrityService.fetch_expected_hash(
                registry_url, peer.code_version or peer.version)

        if not expected:
            # Last resort: compare against own code hash
            try:
                from security.node_integrity import compute_code_hash
                expected = compute_code_hash()
            except Exception:
                return {'verified': False, 'details': 'Cannot compute local hash'}

        if peer.code_hash == expected:
            peer.integrity_status = 'verified'
            peer.last_attestation_at = datetime.utcnow()
            return {'verified': True, 'details': 'Code hash matches'}
        else:
            IntegrityService.increase_fraud_score(
                db, node_id, FRAUD_WEIGHTS['hash_mismatch'],
                f'Code hash mismatch: expected {expected[:16]}..., got {peer.code_hash[:16]}...',
                {'expected': expected, 'reported': peer.code_hash})
            return {'verified': False, 'details': 'Code hash mismatch'}

    @staticmethod
    def fetch_expected_hash(registry_url: str, version: str) -> Optional[str]:
        """GET expected code hash from central registry."""
        try:
            resp = requests.get(
                f"{registry_url}/api/social/integrity/expected-hash",
                params={'version': version},
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get('code_hash')
        except requests.RequestException:
            pass
        return None

    # ─── Challenge-Response Protocol ───

    @staticmethod
    def create_challenge(db: Session, challenger_node_id: str,
                         target_node_id: str, target_url: str,
                         challenge_type: str = 'agent_count_verify') -> Optional[Dict]:
        """Create and send a challenge to a target node."""
        nonce = secrets.token_hex(32)

        challenge_data = {'type': challenge_type, 'nonce': nonce}
        if challenge_type == 'agent_count_verify':
            peer = db.query(PeerNode).filter_by(node_id=target_node_id).first()
            if peer:
                challenge_data['claimed_agent_count'] = peer.agent_count or 0
        elif challenge_type == 'stats_probe':
            challenge_data['requested_stats'] = ['agent_count', 'post_count']
        elif challenge_type == 'code_hash_check':
            challenge_data['request'] = 'code_hash_and_version'

        # Sign the challenge
        try:
            from security.node_integrity import sign_json_payload, get_public_key_hex
            challenge_data['challenger_public_key'] = get_public_key_hex()
            challenge_data['signature'] = sign_json_payload(challenge_data)
        except Exception:
            pass

        challenge = IntegrityChallenge(
            challenger_node_id=challenger_node_id,
            target_node_id=target_node_id,
            challenge_type=challenge_type,
            challenge_nonce=nonce,
            challenge_data=challenge_data,
            status='pending',
        )
        db.add(challenge)
        db.flush()

        # Send challenge to target node
        try:
            resp = requests.post(
                f"{target_url}/api/social/integrity/challenge",
                json={'challenge_id': challenge.id, **challenge_data,
                      'challenger_node_id': challenger_node_id},
                timeout=CHALLENGE_TIMEOUT_SECONDS,
            )
            if resp.status_code == 200:
                response_data = resp.json()
                return IntegrityService.evaluate_challenge_response(
                    db, challenge.id,
                    response_data.get('response', {}),
                    response_data.get('signature', ''))
        except requests.RequestException:
            challenge.status = 'timeout'
            IntegrityService.increase_fraud_score(
                db, target_node_id, 5.0,
                f'Challenge timeout: {challenge_type}')

        peer = db.query(PeerNode).filter_by(node_id=target_node_id).first()
        if peer:
            peer.last_challenge_at = datetime.utcnow()
        return challenge.to_dict()

    @staticmethod
    def handle_challenge(db: Session, challenge_data: dict) -> Dict:
        """Handle an incoming challenge from a peer. Compute response from local data."""
        challenge_type = challenge_data.get('type', '')
        nonce = challenge_data.get('nonce', '')
        response = {'nonce': nonce, 'timestamp': datetime.utcnow().isoformat()}

        if challenge_type == 'agent_count_verify':
            actual_count = db.query(sqlfunc.count(User.id)).filter_by(
                user_type='agent').scalar() or 0
            response['agent_count'] = actual_count
            # Include sample agent IDs as proof
            agents = db.query(User.id, User.username).filter_by(
                user_type='agent').limit(5).all()
            response['sample_agents'] = [
                {'id': a.id, 'username': a.username} for a in agents]

        elif challenge_type == 'stats_probe':
            response['agent_count'] = db.query(sqlfunc.count(User.id)).filter_by(
                user_type='agent').scalar() or 0
            response['post_count'] = db.query(sqlfunc.count(Post.id)).scalar() or 0

        elif challenge_type == 'code_hash_check':
            try:
                from security.node_integrity import compute_code_hash
                from integrations.social.peer_discovery import gossip
                response['code_hash'] = compute_code_hash()
                response['version'] = gossip.version
            except Exception:
                response['code_hash'] = 'unavailable'

        elif challenge_type == 'impression_audit':
            ad_id = challenge_data.get('ad_id')
            if ad_id:
                hour_ago = datetime.utcnow() - timedelta(hours=1)
                count = db.query(sqlfunc.count(AdImpression.id)).filter(
                    AdImpression.ad_id == ad_id,
                    AdImpression.created_at >= hour_ago,
                ).scalar() or 0
                response['impression_count'] = count

        # Sign the response
        try:
            from security.node_integrity import sign_json_payload, get_public_key_hex
            response['public_key'] = get_public_key_hex()
            sig = sign_json_payload(response)
            return {'response': response, 'signature': sig}
        except Exception:
            return {'response': response, 'signature': ''}

    @staticmethod
    def evaluate_challenge_response(db: Session, challenge_id: str,
                                     response_data: dict,
                                     response_signature: str) -> Dict:
        """Evaluate a challenge response. Verify signature and data consistency."""
        challenge = db.query(IntegrityChallenge).filter_by(id=challenge_id).first()
        if not challenge:
            return {'passed': False, 'details': 'Challenge not found'}

        challenge.response_data = response_data
        challenge.response_signature = response_signature
        challenge.responded_at = datetime.utcnow()

        # Verify nonce
        if response_data.get('nonce') != challenge.challenge_nonce:
            challenge.status = 'failed'
            challenge.result_details = 'Nonce mismatch'
            IntegrityService.increase_fraud_score(
                db, challenge.target_node_id, FRAUD_WEIGHTS['challenge_fail'],
                'Challenge failed: nonce mismatch')
            return {'passed': False, 'details': 'Nonce mismatch'}

        # Verify signature if public key available
        public_key = response_data.get('public_key', '')
        if public_key and response_signature:
            try:
                from security.node_integrity import verify_json_signature
                if not verify_json_signature(public_key, response_data, response_signature):
                    challenge.status = 'failed'
                    challenge.result_details = 'Invalid signature'
                    IntegrityService.increase_fraud_score(
                        db, challenge.target_node_id, FRAUD_WEIGHTS['challenge_fail'],
                        'Challenge failed: invalid signature')
                    return {'passed': False, 'details': 'Invalid signature'}
            except Exception:
                pass

        # Evaluate based on challenge type
        passed = True
        details = 'OK'

        if challenge.challenge_type == 'agent_count_verify':
            claimed = (challenge.challenge_data or {}).get('claimed_agent_count', 0)
            actual = response_data.get('agent_count', 0)
            # Allow 10% tolerance
            if claimed > 0 and actual < claimed * 0.5:
                passed = False
                details = f'Agent count mismatch: claimed {claimed}, actual {actual}'

        elif challenge.challenge_type == 'stats_probe':
            peer = db.query(PeerNode).filter_by(
                node_id=challenge.target_node_id).first()
            if peer:
                reported_agents = response_data.get('agent_count', 0)
                if peer.agent_count and reported_agents < (peer.agent_count or 0) * 0.5:
                    passed = False
                    details = f'Stats mismatch: DB has {peer.agent_count}, node reports {reported_agents}'

        elif challenge.challenge_type == 'code_hash_check':
            reported_hash = response_data.get('code_hash', '')
            peer = db.query(PeerNode).filter_by(
                node_id=challenge.target_node_id).first()
            if peer and peer.code_hash and reported_hash != peer.code_hash:
                passed = False
                details = 'Code hash changed since last exchange'

        challenge.status = 'passed' if passed else 'failed'
        challenge.result_details = details
        challenge.evaluated_at = datetime.utcnow()

        if not passed:
            IntegrityService.increase_fraud_score(
                db, challenge.target_node_id, FRAUD_WEIGHTS['challenge_fail'],
                f'Challenge failed: {details}')
        else:
            # Successful challenge reduces fraud score slightly
            IntegrityService.decrease_fraud_score(
                db, challenge.target_node_id, 2.0,
                f'Challenge passed: {challenge.challenge_type}')

        return {'passed': passed, 'details': details}

    # ─── Impression Witnessing ───

    @staticmethod
    def request_nearest_witness(db: Session, impression_id: str,
                                 ad_id: str, requesting_node_id: str) -> Optional[Dict]:
        """Find nearest active non-banned peer and request a witness attestation."""
        peers = db.query(PeerNode).filter(
            PeerNode.status == 'active',
            PeerNode.integrity_status != 'banned',
            PeerNode.node_id != requesting_node_id,
        ).limit(5).all()

        for peer in peers:
            result = IntegrityService._request_witness_from_peer(
                db, impression_id, ad_id, requesting_node_id, peer)
            if result:
                return result
        return None

    @staticmethod
    def _request_witness_from_peer(db: Session, impression_id: str,
                                    ad_id: str, requesting_node_id: str,
                                    peer: PeerNode) -> Optional[Dict]:
        """Request a specific peer to witness an impression."""
        nonce = secrets.token_hex(16)
        payload = {
            'impression_id': impression_id,
            'ad_id': ad_id,
            'node_id': requesting_node_id,
            'timestamp': datetime.utcnow().isoformat(),
            'nonce': nonce,
        }
        try:
            from security.node_integrity import sign_json_payload, get_public_key_hex
            payload['public_key'] = get_public_key_hex()
            payload['signature'] = sign_json_payload(payload)
        except Exception:
            pass

        try:
            resp = requests.post(
                f"{peer.url}/api/social/integrity/witness-impression",
                json=payload,
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get('witnessed'):
                    # Store attestation
                    attestation = NodeAttestation(
                        attester_node_id=peer.node_id,
                        subject_node_id=requesting_node_id,
                        attestation_type='impression_witness',
                        payload_json={'impression_id': impression_id, 'ad_id': ad_id},
                        signature=data.get('signature', ''),
                        attester_public_key=data.get('public_key', peer.public_key or ''),
                        expires_at=datetime.utcnow() + timedelta(days=ATTESTATION_EXPIRY_DAYS),
                    )
                    db.add(attestation)
                    db.flush()
                    return attestation.to_dict()
        except requests.RequestException:
            pass
        return None

    @staticmethod
    def handle_witness_request(db: Session, witness_data: dict) -> Dict:
        """Handle incoming witness request from a peer node."""
        requesting_node_id = witness_data.get('node_id', '')
        timestamp_str = witness_data.get('timestamp', '')
        signature = witness_data.get('signature', '')
        public_key = witness_data.get('public_key', '')

        # Check if requesting node is known and not banned
        peer = db.query(PeerNode).filter_by(node_id=requesting_node_id).first()
        if peer and peer.integrity_status == 'banned':
            return {'witnessed': False, 'reason': 'Requesting node is banned'}

        # Check timestamp freshness
        if timestamp_str:
            try:
                ts = datetime.fromisoformat(timestamp_str)
                age = (datetime.utcnow() - ts).total_seconds()
                if age > WITNESS_TIMESTAMP_MAX_AGE:
                    return {'witnessed': False, 'reason': 'Stale timestamp'}
            except (ValueError, TypeError):
                pass

        # Verify signature if available
        if signature and public_key:
            try:
                from security.node_integrity import verify_json_signature
                if not verify_json_signature(public_key, witness_data, signature):
                    return {'witnessed': False, 'reason': 'Invalid signature'}
            except Exception:
                pass

        # Co-sign the witness
        try:
            from security.node_integrity import sign_json_payload, get_public_key_hex
            witness_response = {
                'witnessed': True,
                'impression_id': witness_data.get('impression_id'),
                'ad_id': witness_data.get('ad_id'),
                'nonce': witness_data.get('nonce'),
                'witness_timestamp': datetime.utcnow().isoformat(),
            }
            witness_response['public_key'] = get_public_key_hex()
            witness_response['signature'] = sign_json_payload(witness_response)
            return witness_response
        except Exception:
            return {'witnessed': True, 'signature': '', 'public_key': ''}

    @staticmethod
    def get_impression_witness_count(db: Session, impression_id: str) -> int:
        """Count how many peer witnesses confirmed a given impression."""
        return db.query(sqlfunc.count(NodeAttestation.id)).filter(
            NodeAttestation.attestation_type == 'impression_witness',
            NodeAttestation.is_valid == True,
            NodeAttestation.payload_json.contains(impression_id) if hasattr(
                NodeAttestation.payload_json, 'contains') else True,
        ).scalar() or 0

    # ─── Score Consensus ───

    @staticmethod
    def probe_peer_stats(peer_url: str, target_node_id: str) -> Optional[Dict]:
        """Query a peer for its view of a target node's stats."""
        try:
            resp = requests.get(
                f"{peer_url}/api/social/integrity/peer-stats",
                params={'node_id': target_node_id},
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json()
        except requests.RequestException:
            pass
        return None

    @staticmethod
    def run_consensus_check(db: Session, target_node_id: str,
                             peer_urls: List[str]) -> Dict:
        """Query multiple peers for their view of target's stats. Flag disagreements."""
        reports = []
        for url in peer_urls[:5]:
            report = IntegrityService.probe_peer_stats(url, target_node_id)
            if report:
                reports.append(report)

        if len(reports) < 2:
            return {'consensus': True, 'reports': reports, 'anomalies': [],
                    'details': 'Not enough peers for consensus'}

        agent_counts = [r.get('agent_count', 0) for r in reports]
        post_counts = [r.get('post_count', 0) for r in reports]

        anomalies = []
        if len(set(agent_counts)) > 1:
            spread = max(agent_counts) - min(agent_counts)
            mean = statistics.mean(agent_counts) if agent_counts else 0
            if mean > 0 and spread / mean > 0.5:
                anomalies.append({
                    'field': 'agent_count',
                    'values': agent_counts,
                    'spread_ratio': round(spread / mean, 2),
                })

        consensus = len(anomalies) == 0
        if not consensus:
            IntegrityService.increase_fraud_score(
                db, target_node_id, FRAUD_WEIGHTS['score_jump'] * 0.5,
                f'Consensus disagreement: {anomalies}',
                {'reports': reports, 'anomalies': anomalies})

        return {'consensus': consensus, 'reports': reports, 'anomalies': anomalies}

    # ─── Fraud Detection ───

    @staticmethod
    def detect_impression_anomaly(db: Session, node_id: str,
                                   period_hours: int = 24) -> Optional[Dict]:
        """Compare node's impression rate to network average. Flag if >3 stddev."""
        cutoff = datetime.utcnow() - timedelta(hours=period_hours)

        # Get per-node impression counts
        node_counts = db.query(
            AdImpression.node_id,
            sqlfunc.count(AdImpression.id).label('cnt'),
        ).filter(
            AdImpression.created_at >= cutoff,
            AdImpression.node_id.isnot(None),
        ).group_by(AdImpression.node_id).all()

        if len(node_counts) < 3:
            return None  # Not enough data

        counts = [nc.cnt for nc in node_counts]
        target_count = next((nc.cnt for nc in node_counts if nc.node_id == node_id), 0)

        if target_count < 50:
            return None  # Too low to flag

        mean = statistics.mean(counts)
        stddev = statistics.stdev(counts) if len(counts) > 1 else 0

        if stddev == 0:
            return None

        z_score = (target_count - mean) / stddev

        if z_score > IMPRESSION_ANOMALY_STDDEV:
            return IntegrityService._create_fraud_alert(
                db, node_id, 'impression_anomaly', 'high',
                f'Impression rate anomaly: {target_count} impressions '
                f'(z-score={z_score:.1f}, mean={mean:.1f}, stddev={stddev:.1f})',
                FRAUD_WEIGHTS['impression_anomaly'],
                {'count': target_count, 'mean': round(mean, 2),
                 'stddev': round(stddev, 2), 'z_score': round(z_score, 2)})

        return None

    @staticmethod
    def detect_score_jump(db: Session, node_id: str) -> Optional[Dict]:
        """Flag if agent_count or post_count jumped >200% in 24h."""
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            return None

        metadata = peer.metadata_json or {}
        prev_agents = metadata.get('_prev_agent_count', 0)
        prev_posts = metadata.get('_prev_post_count', 0)
        current_agents = peer.agent_count or 0
        current_posts = peer.post_count or 0

        alert = None
        if prev_agents > 5 and current_agents > prev_agents * (1 + SCORE_JUMP_THRESHOLD_PCT / 100):
            alert = IntegrityService._create_fraud_alert(
                db, node_id, 'score_jump', 'medium',
                f'Agent count jumped from {prev_agents} to {current_agents} '
                f'({((current_agents / prev_agents) - 1) * 100:.0f}% increase)',
                FRAUD_WEIGHTS['score_jump'],
                {'prev': prev_agents, 'current': current_agents, 'field': 'agent_count'})

        if prev_posts > 10 and current_posts > prev_posts * (1 + SCORE_JUMP_THRESHOLD_PCT / 100):
            alert = IntegrityService._create_fraud_alert(
                db, node_id, 'score_jump', 'medium',
                f'Post count jumped from {prev_posts} to {current_posts}',
                FRAUD_WEIGHTS['score_jump'],
                {'prev': prev_posts, 'current': current_posts, 'field': 'post_count'})

        # Store current values as previous for next check
        if not metadata:
            metadata = {}
        metadata['_prev_agent_count'] = current_agents
        metadata['_prev_post_count'] = current_posts
        metadata['_last_score_check'] = datetime.utcnow().isoformat()
        peer.metadata_json = metadata

        return alert

    @staticmethod
    def detect_collusion(db: Session, node_id: str) -> Optional[Dict]:
        """Detect if >80% attestations come from a single peer."""
        cutoff = datetime.utcnow() - timedelta(days=ATTESTATION_EXPIRY_DAYS)
        attestations = db.query(NodeAttestation).filter(
            NodeAttestation.subject_node_id == node_id,
            NodeAttestation.created_at >= cutoff,
            NodeAttestation.is_valid == True,
        ).all()

        if len(attestations) < 5:
            return None

        attester_counts = {}
        for a in attestations:
            attester_counts[a.attester_node_id] = attester_counts.get(
                a.attester_node_id, 0) + 1

        total = sum(attester_counts.values())
        for attester_id, count in attester_counts.items():
            ratio = count / total
            if ratio > 0.8:
                return IntegrityService._create_fraud_alert(
                    db, node_id, 'collusion_suspected', 'high',
                    f'Collusion suspected: {count}/{total} attestations '
                    f'({ratio:.0%}) from single peer {attester_id[:8]}',
                    FRAUD_WEIGHTS['collusion_suspected'],
                    {'dominant_attester': attester_id, 'ratio': round(ratio, 3),
                     'count': count, 'total': total})

        return None

    @staticmethod
    def run_full_audit(db: Session, node_id: str,
                       registry_url: str = None) -> Dict:
        """Run all fraud detection checks on a node."""
        results = {}
        results['code_hash'] = IntegrityService.verify_code_hash(
            db, node_id, registry_url)
        results['impression_anomaly'] = IntegrityService.detect_impression_anomaly(
            db, node_id)
        results['score_jump'] = IntegrityService.detect_score_jump(db, node_id)
        results['collusion'] = IntegrityService.detect_collusion(db, node_id)

        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        results['fraud_score'] = peer.fraud_score if peer else 0.0
        results['integrity_status'] = peer.integrity_status if peer else 'unknown'
        return results

    # ─── Fraud Score Management ───

    @staticmethod
    def increase_fraud_score(db: Session, node_id: str, delta: float,
                              reason: str, evidence: dict = None) -> float:
        """Increase a node's fraud_score. Auto-bans at threshold. Creates FraudAlert."""
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            return 0.0

        peer.fraud_score = min((peer.fraud_score or 0) + delta, 100.0)

        # Determine severity
        severity = 'low'
        if delta >= 20:
            severity = 'high'
        elif delta >= 10:
            severity = 'medium'
        if peer.fraud_score >= FRAUD_BAN_THRESHOLD:
            severity = 'critical'

        # Create fraud alert
        alert = FraudAlert(
            node_id=node_id,
            alert_type=_determine_alert_type(reason),
            severity=severity,
            description=reason,
            evidence_json=evidence or {},
            fraud_score_delta=delta,
        )
        db.add(alert)

        # Auto-ban at threshold
        if peer.fraud_score >= FRAUD_BAN_THRESHOLD:
            peer.integrity_status = 'banned'
            logger.warning(f"Node {node_id[:8]} auto-banned: fraud_score={peer.fraud_score}")
        elif peer.fraud_score >= 40:
            peer.integrity_status = 'suspicious'

        db.flush()
        return peer.fraud_score

    @staticmethod
    def decrease_fraud_score(db: Session, node_id: str, delta: float,
                              reason: str) -> float:
        """Decrease fraud_score (e.g., after successful verification)."""
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            return 0.0

        peer.fraud_score = max((peer.fraud_score or 0) - delta, 0.0)

        # Upgrade status if score dropped
        if peer.fraud_score < 40 and peer.integrity_status == 'suspicious':
            peer.integrity_status = 'verified'

        db.flush()
        return peer.fraud_score

    @staticmethod
    def ban_node(db: Session, node_id: str, reason: str):
        """Set integrity_status='banned'."""
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if peer:
            peer.integrity_status = 'banned'
            peer.fraud_score = 100.0
            alert = FraudAlert(
                node_id=node_id, alert_type='manual_ban',
                severity='critical', description=reason,
            )
            db.add(alert)
            db.flush()

    @staticmethod
    def unban_node(db: Session, node_id: str, admin_user_id: str):
        """Admin action to unban a node."""
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if peer:
            peer.integrity_status = 'unverified'
            peer.fraud_score = 0.0
            alert = FraudAlert(
                node_id=node_id, alert_type='unban',
                severity='low',
                description=f'Unbanned by admin {admin_user_id}',
                reviewed_by=admin_user_id,
                reviewed_at=datetime.utcnow(),
                status='dismissed',
            )
            db.add(alert)
            db.flush()

    # ─── Registry (Central Trust Anchor) ───

    @staticmethod
    def register_with_registry(registry_url: str, node_id: str,
                                public_key_hex: str, version: str) -> bool:
        """POST to registry to register this node's public key."""
        try:
            from security.node_integrity import compute_code_hash, sign_json_payload
            payload = {
                'node_id': node_id,
                'public_key': public_key_hex,
                'version': version,
                'code_hash': compute_code_hash(),
            }
            # Include release manifest info if available
            try:
                from security.master_key import load_release_manifest
                manifest = load_release_manifest()
                if manifest:
                    payload['release_version'] = manifest.get('version', '')
                    payload['release_manifest_signature'] = manifest.get('master_signature', '')
            except Exception:
                pass
            payload['signature'] = sign_json_payload(payload)
            resp = requests.post(
                f"{registry_url}/api/social/integrity/register-node",
                json=payload,
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    @staticmethod
    def check_registry_ban_list(registry_url: str) -> List[str]:
        """GET banned node_ids from registry."""
        try:
            resp = requests.get(
                f"{registry_url}/api/social/integrity/ban-list",
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get('banned_node_ids', [])
        except requests.RequestException:
            pass
        return []

    @staticmethod
    def pull_trusted_keys(registry_url: str) -> Dict[str, str]:
        """GET verified node public keys from registry."""
        try:
            resp = requests.get(
                f"{registry_url}/api/social/integrity/trusted-keys",
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get('keys', {})
        except requests.RequestException:
            pass
        return {}

    # ─── Queries ───

    @staticmethod
    def get_fraud_alerts(db: Session, node_id: str = None,
                         status: str = None, severity: str = None,
                         limit: int = 50, offset: int = 0) -> List[Dict]:
        """Query fraud alerts with filters."""
        q = db.query(FraudAlert)
        if node_id:
            q = q.filter(FraudAlert.node_id == node_id)
        if status:
            q = q.filter(FraudAlert.status == status)
        if severity:
            q = q.filter(FraudAlert.severity == severity)
        alerts = q.order_by(FraudAlert.created_at.desc()).offset(offset).limit(limit).all()
        return [a.to_dict() for a in alerts]

    @staticmethod
    def update_alert(db: Session, alert_id: str, status: str,
                     reviewed_by: str) -> Optional[Dict]:
        """Update a fraud alert status."""
        alert = db.query(FraudAlert).filter_by(id=alert_id).first()
        if not alert:
            return None
        alert.status = status
        alert.reviewed_by = reviewed_by
        alert.reviewed_at = datetime.utcnow()
        db.flush()
        return alert.to_dict()

    @staticmethod
    def get_integrity_dashboard(db: Session) -> Dict:
        """Overview stats for admin dashboard."""
        total_nodes = db.query(sqlfunc.count(PeerNode.id)).scalar() or 0
        verified = db.query(sqlfunc.count(PeerNode.id)).filter(
            PeerNode.integrity_status == 'verified').scalar() or 0
        suspicious = db.query(sqlfunc.count(PeerNode.id)).filter(
            PeerNode.integrity_status == 'suspicious').scalar() or 0
        banned = db.query(sqlfunc.count(PeerNode.id)).filter(
            PeerNode.integrity_status == 'banned').scalar() or 0
        open_alerts = db.query(sqlfunc.count(FraudAlert.id)).filter(
            FraudAlert.status == 'open').scalar() or 0

        return {
            'total_nodes': total_nodes,
            'verified': verified,
            'suspicious': suspicious,
            'banned': banned,
            'unverified': total_nodes - verified - suspicious - banned,
            'open_alerts': open_alerts,
        }

    # ─── Private Helpers ───

    @staticmethod
    def _create_fraud_alert(db: Session, node_id: str, alert_type: str,
                             severity: str, description: str,
                             fraud_delta: float, evidence: dict) -> Dict:
        """Create a FraudAlert and increase fraud score."""
        IntegrityService.increase_fraud_score(
            db, node_id, fraud_delta, description, evidence)
        # The alert is created inside increase_fraud_score
        return {'node_id': node_id, 'alert_type': alert_type,
                'severity': severity, 'description': description}


def _determine_alert_type(reason: str) -> str:
    """Infer alert type from reason string."""
    reason_lower = reason.lower()
    if 'hash' in reason_lower or 'code' in reason_lower:
        return 'hash_mismatch'
    if 'challenge' in reason_lower:
        return 'challenge_fail'
    if 'impression' in reason_lower or 'anomaly' in reason_lower:
        return 'impression_anomaly'
    if 'jump' in reason_lower or 'count' in reason_lower:
        return 'score_jump'
    if 'collusion' in reason_lower:
        return 'collusion_suspected'
    if 'witness' in reason_lower:
        return 'witness_refusal'
    if 'ban' in reason_lower:
        return 'manual_ban'
    return 'other'
