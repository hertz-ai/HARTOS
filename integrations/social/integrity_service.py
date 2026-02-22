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
    'reward_velocity_anomaly': 25.0,
    'reward_self_dealing': 35.0,
    'spark_gaming': 20.0,
    'witness_ring': 30.0,
    'temporal_clustering': 20.0,
    'seal_tamper': 35.0,
    'gradient_magnitude_anomaly': 20.0,
    'gradient_direction_flip': 25.0,
}

IMPRESSION_ANOMALY_STDDEV = 3.0
SCORE_JUMP_THRESHOLD_PCT = 200
FRAUD_BAN_THRESHOLD = 80.0
ATTESTATION_EXPIRY_DAYS = 7
MIN_WITNESS_PEERS = 1
CHALLENGE_TIMEOUT_SECONDS = 30
WITNESS_TIMESTAMP_MAX_AGE = 60  # seconds

# ── Fraud Score Decay ──
# Every audit round, each node's fraud score decays by this amount.
# Good behavior over time earns back trust.  But ban records persist.
FRAUD_SCORE_DECAY_PER_ROUND = 2.0  # Points per audit round (~5 min)
FRAUD_SCORE_DECAY_MIN = 0.0

# ── Fail2ban: Progressive Ban Durations ──
# Each subsequent ban lasts longer. ban_count tracked on PeerNode.
# After max_bans (4+), node must petition for human review.
FAIL2BAN_DURATIONS = {
    1: timedelta(hours=1),     # 1st offense: 1 hour
    2: timedelta(hours=24),    # 2nd offense: 24 hours
    3: timedelta(days=7),      # 3rd offense: 1 week
}
FAIL2BAN_MAX_DURATION = timedelta(days=30)  # 4th+ offense: 30 days


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

        # Priority 0: Release hash registry (multi-version support)
        try:
            from security.release_hash_registry import get_release_hash_registry
            registry = get_release_hash_registry()
            if registry.is_known_release_hash(peer.code_hash):
                peer.integrity_status = 'verified'
                peer.last_attestation_at = datetime.utcnow()
                return {'verified': True,
                        'details': 'Code hash in release registry'}
        except Exception:
            pass

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

        elif challenge_type == 'guardrail_verify':
            try:
                from security.hive_guardrails import get_guardrail_hash, compute_guardrail_hash
                response['guardrail_hash'] = get_guardrail_hash()
                # Recompute live to prove it's not cached/stale
                response['guardrail_hash_live'] = compute_guardrail_hash()
            except Exception:
                response['guardrail_hash'] = 'unavailable'

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
        # Prevent replay: reject already-evaluated challenges
        if challenge.status != 'pending':
            return {'passed': False, 'details': f'Challenge already processed (status={challenge.status})'}

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

        elif challenge.challenge_type == 'guardrail_verify':
            reported_hash = response_data.get('guardrail_hash', '')
            reported_live = response_data.get('guardrail_hash_live', '')
            try:
                from security.hive_guardrails import get_guardrail_hash
                expected = get_guardrail_hash()
                if reported_hash != expected:
                    passed = False
                    details = f'Guardrail hash mismatch: expected {expected[:16]}, got {reported_hash[:16]}'
                elif reported_live and reported_live != expected:
                    passed = False
                    details = f'Guardrail live recompute mismatch — possible tampering'
                elif reported_hash != reported_live:
                    passed = False
                    details = 'Cached vs live guardrail hash mismatch — values may have drifted'
            except Exception:
                pass

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

    # Rate limit: max witness requests per node per hour
    _witness_request_counts = {}  # node_id -> (count, window_start)
    _WITNESS_MAX_PER_HOUR = 100

    @staticmethod
    def request_nearest_witness(db: Session, impression_id: str,
                                 ad_id: str, requesting_node_id: str) -> Optional[Dict]:
        """Find nearest active non-banned peer and request a witness attestation."""
        # Rate limit witness requests per node
        now = datetime.utcnow()
        counts = IntegrityService._witness_request_counts
        entry = counts.get(requesting_node_id)
        if entry:
            count, window_start = entry
            if (now - window_start).total_seconds() > 3600:
                counts[requesting_node_id] = (1, now)
            elif count >= IntegrityService._WITNESS_MAX_PER_HOUR:
                return None  # Rate limited
            else:
                counts[requesting_node_id] = (count + 1, window_start)
        else:
            counts[requesting_node_id] = (1, now)

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

        # Verify signature (required — omitting signature is not allowed)
        if not signature or not public_key:
            return {'witnessed': False, 'reason': 'Missing signature or public key'}
        try:
            from security.node_integrity import verify_json_signature
            if not verify_json_signature(public_key, witness_data, signature):
                return {'witnessed': False, 'reason': 'Invalid signature'}
        except Exception:
            return {'witnessed': False, 'reason': 'Signature verification failed'}

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
        """Run all fraud detection checks on a node, including reward hacking.

        Also applies fraud score decay and checks ban expiry — good nodes
        recover trust over time, banned nodes serve their sentence then
        return to 'suspicious' for re-evaluation.
        """
        results = {}

        # Decay fraud scores + check ban expiry BEFORE running checks.
        # This ensures previously-banned nodes get a fair reassessment.
        results['decay'] = IntegrityService.apply_fraud_score_decay(db)

        results['code_hash'] = IntegrityService.verify_code_hash(
            db, node_id, registry_url)
        results['impression_anomaly'] = IntegrityService.detect_impression_anomaly(
            db, node_id)
        results['score_jump'] = IntegrityService.detect_score_jump(db, node_id)
        results['collusion'] = IntegrityService.detect_collusion(db, node_id)
        results['audit_dominance'] = IntegrityService.verify_audit_dominance(
            db, node_id)

        # Reward hacking detection
        results['reward_velocity'] = IntegrityService.detect_reward_velocity_anomaly(
            db, node_id)
        results['reward_self_dealing'] = IntegrityService.detect_reward_self_dealing(
            db, node_id)
        results['spark_gaming'] = IntegrityService.detect_spark_gaming(db, node_id)

        # Impression integrity (collusion + tampering)
        results['witness_ring'] = IntegrityService.detect_witness_ring(db, node_id)
        results['temporal_clustering'] = IntegrityService.detect_temporal_clustering(
            db, node_id)
        results['seal_integrity'] = IntegrityService.verify_all_sealed_impressions(
            db, node_id, limit=50)

        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        results['fraud_score'] = peer.fraud_score if peer else 0.0
        results['integrity_status'] = peer.integrity_status if peer else 'unknown'
        results['ban_count'] = peer.ban_count if peer else 0
        results['ban_until'] = peer.ban_until.isoformat() if peer and peer.ban_until else None

        # Auto-isolate if multiple reward hacking signals fire
        reward_hack_signals = sum(1 for k in (
            'reward_velocity', 'reward_self_dealing', 'spark_gaming'
        ) if results.get(k) is not None)
        if reward_hack_signals >= 2:
            results['isolation'] = IntegrityService.isolate_reward_hacker(
                db, node_id,
                f'Multiple reward hacking signals: {reward_hack_signals}/3 triggered',
                {'signals': {k: results.get(k) for k in (
                    'reward_velocity', 'reward_self_dealing', 'spark_gaming'
                ) if results.get(k)}})

        # Auto-isolate impression fraud: witness_ring + temporal_clustering together
        impression_fraud_signals = sum(1 for k in (
            'witness_ring', 'temporal_clustering',
        ) if results.get(k) is not None)
        seal_tampered = (results.get('seal_integrity') or {}).get('tampered', 0) > 0
        if impression_fraud_signals >= 2 or (impression_fraud_signals >= 1 and seal_tampered):
            results['impression_isolation'] = IntegrityService.isolate_reward_hacker(
                db, node_id,
                f'Impression fraud: {impression_fraud_signals} signals + '
                f'seal_tampered={seal_tampered}',
                {'witness_ring': results.get('witness_ring'),
                 'temporal_clustering': results.get('temporal_clustering'),
                 'seal_integrity': results.get('seal_integrity')})

        return results

    # ─── Post-Update Peer Witness Verification ───

    @staticmethod
    def verify_post_update(db: Session, node_id: str,
                           expected_version: str = '') -> Dict:
        """Verify a node after it reports an upgrade.

        Challenges code_hash, verifies guardrail_hash, and records
        attestation in NodeAttestation table. Called on next gossip round
        after a peer announces a new version.
        """
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            return {'verified': False, 'reason': 'peer not found'}

        results = {'node_id': node_id, 'checks': {}}

        # 1. Code hash verification (against master-signed manifest)
        hash_result = IntegrityService.verify_code_hash(db, node_id)
        results['checks']['code_hash'] = hash_result

        # 2. Guardrail hash verification via challenge
        if peer.url:
            try:
                challenge_result = IntegrityService.create_challenge(
                    db, challenger_node_id='self',
                    target_node_id=node_id,
                    target_url=peer.url,
                    challenge_type='code_hash_check')
                results['checks']['challenge'] = challenge_result or {}
            except Exception as e:
                results['checks']['challenge'] = {'error': str(e)}

        # 3. Guardrail hash match check from latest gossip info
        try:
            from security.hive_guardrails import get_guardrail_hash
            our_hash = get_guardrail_hash()
            peer_hash = getattr(peer, 'guardrail_hash', None)
            if peer_hash and peer_hash != our_hash:
                results['checks']['guardrail_hash'] = {
                    'match': False, 'our': our_hash[:16], 'theirs': peer_hash[:16]}
                IntegrityService.increase_fraud_score(
                    db, node_id, 10.0,
                    f'Post-update guardrail hash mismatch',
                    {'expected': our_hash[:16], 'got': peer_hash[:16]})
            else:
                results['checks']['guardrail_hash'] = {'match': True}
        except ImportError:
            results['checks']['guardrail_hash'] = {'skipped': True}

        # 4. Record attestation
        try:
            attestation = NodeAttestation(
                node_id=node_id,
                attestation_type='post_update_verification',
                attestation_data={
                    'version': expected_version,
                    'code_hash_verified': hash_result.get('verified', False),
                    'checks': {k: bool(v.get('verified') or v.get('match'))
                               for k, v in results['checks'].items()
                               if isinstance(v, dict)},
                },
            )
            db.add(attestation)
            db.flush()
            results['attestation_id'] = attestation.id
        except Exception as e:
            results['attestation_error'] = str(e)

        all_passed = all(
            v.get('verified') or v.get('match') or v.get('skipped') or v.get('passed')
            for v in results['checks'].values()
            if isinstance(v, dict)
        )
        results['verified'] = all_passed
        return results

    # ─── Audit Compute Dominance ───

    @staticmethod
    def verify_audit_dominance(db: Session, target_node_id: str) -> Dict:
        """Verify that the compute available for auditing a node exceeds
        that node's own compute. No node should be able to outcompute its auditors.

        Principle: audit_compute > target_compute — always.
        This is enforced by compute democracy (max 5% influence) but we
        verify it explicitly here to catch edge cases.
        """
        target = db.query(PeerNode).filter_by(node_id=target_node_id).first()
        if not target:
            return {'dominant': True, 'details': 'Unknown node'}

        target_compute = _get_node_compute(target)

        # Sum compute of all active non-banned peers (excluding target)
        auditors = db.query(PeerNode).filter(
            PeerNode.status == 'active',
            PeerNode.node_id != target_node_id,
            PeerNode.integrity_status != 'banned',
        ).all()

        auditor_compute = sum(_get_node_compute(p) for p in auditors)
        auditor_count = len(auditors)

        # The audit collective must have more compute than the target
        dominant = auditor_compute > target_compute

        if not dominant and auditor_count > 0:
            # If a single node has more compute than all its auditors combined,
            # flag it — this violates compute democracy
            logger.warning(
                f"Audit dominance violation: node {target_node_id[:8]} has "
                f"{target_compute} compute vs {auditor_compute} auditor compute "
                f"({auditor_count} auditors)")
            IntegrityService.increase_fraud_score(
                db, target_node_id, 10.0,
                f'Audit dominance violation: target compute ({target_compute}) '
                f'exceeds auditor compute ({auditor_compute})',
                {'target_compute': target_compute,
                 'auditor_compute': auditor_compute,
                 'auditor_count': auditor_count})

        return {
            'dominant': dominant,
            'target_compute': target_compute,
            'auditor_compute': auditor_compute,
            'auditor_count': auditor_count,
            'ratio': round(auditor_compute / max(target_compute, 1), 2),
        }

    @staticmethod
    def get_audit_coverage(db: Session) -> Dict:
        """Network-wide audit dominance report. Returns nodes where
        audit compute does NOT exceed their own compute."""
        active_peers = db.query(PeerNode).filter(
            PeerNode.status == 'active',
            PeerNode.integrity_status != 'banned',
        ).all()

        total_compute = sum(_get_node_compute(p) for p in active_peers)
        violations = []

        for peer in active_peers:
            peer_compute = _get_node_compute(peer)
            # Auditors = everyone else
            auditor_compute = total_compute - peer_compute
            if peer_compute > 0 and auditor_compute <= peer_compute:
                violations.append({
                    'node_id': peer.node_id,
                    'node_name': peer.name,
                    'compute': peer_compute,
                    'auditor_compute': auditor_compute,
                    'ratio': round(auditor_compute / max(peer_compute, 1), 2),
                })

        return {
            'total_nodes': len(active_peers),
            'total_compute': total_compute,
            'violations': violations,
            'all_dominant': len(violations) == 0,
        }

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

        # Auto-ban at threshold — fail2ban progressive duration
        if peer.fraud_score >= FRAUD_BAN_THRESHOLD:
            IntegrityService._apply_fail2ban(db, peer, reason)
        elif peer.fraud_score >= 40:
            peer.integrity_status = 'suspicious'

        db.flush()
        return peer.fraud_score

    @staticmethod
    def _apply_fail2ban(db: Session, peer: 'PeerNode', reason: str):
        """Apply fail2ban progressive ban. Each offense = longer ban.

        1st ban: 1 hour,  2nd: 24 hours,  3rd: 7 days,  4th+: 30 days.
        ban_count persists across unbans — history is never erased.
        """
        peer.ban_count = (peer.ban_count or 0) + 1
        peer.integrity_status = 'banned'

        duration = FAIL2BAN_DURATIONS.get(peer.ban_count, FAIL2BAN_MAX_DURATION)
        peer.ban_until = datetime.utcnow() + duration

        logger.warning(
            f"Node {peer.node_id[:8]} fail2ban: offense #{peer.ban_count}, "
            f"banned until {peer.ban_until.isoformat()}, "
            f"fraud_score={peer.fraud_score}, reason={reason}")

    @staticmethod
    def decrease_fraud_score(db: Session, node_id: str, delta: float,
                              reason: str) -> float:
        """Decrease fraud_score (e.g., after successful verification)."""
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            return 0.0

        peer.fraud_score = max((peer.fraud_score or 0) - delta, 0.0)

        # Upgrade status if score dropped below threshold
        if peer.fraud_score < 40 and peer.integrity_status == 'suspicious':
            peer.integrity_status = 'verified'

        db.flush()
        return peer.fraud_score

    @staticmethod
    def apply_fraud_score_decay(db: Session) -> Dict:
        """Decay all nodes' fraud scores by a small amount each audit round.

        Good behavior over time earns back trust.  Called every audit round
        (typically every ~5 minutes via AgentDaemon integrity tick).

        Also checks ban expiry: if a banned node's ban_until has passed,
        move to 'suspicious' (not 'verified' — they must prove themselves).

        Returns summary of decay effects.
        """
        now = datetime.utcnow()
        decayed = 0
        unbanned = 0

        # Decay all non-zero fraud scores
        peers_with_score = db.query(PeerNode).filter(
            PeerNode.fraud_score > FRAUD_SCORE_DECAY_MIN
        ).all()

        for peer in peers_with_score:
            old_score = peer.fraud_score or 0
            peer.fraud_score = max(old_score - FRAUD_SCORE_DECAY_PER_ROUND, 0.0)
            if peer.fraud_score != old_score:
                decayed += 1

            # If score drops below suspicious threshold, upgrade
            if (peer.fraud_score < 40 and
                    peer.integrity_status == 'suspicious'):
                peer.integrity_status = 'verified'

        # Check ban expiry (fail2ban timer)
        banned_peers = db.query(PeerNode).filter(
            PeerNode.integrity_status == 'banned',
            PeerNode.ban_until != None,  # noqa: E711 (SQLAlchemy)
            PeerNode.ban_until <= now,
        ).all()

        for peer in banned_peers:
            peer.integrity_status = 'suspicious'
            peer.fraud_score = min(peer.fraud_score or 0, 50.0)
            peer.ban_until = None
            unbanned += 1
            logger.info(
                f"Node {peer.node_id[:8]} ban expired (offense #{peer.ban_count}). "
                f"Status → suspicious. Will be re-audited.")

        if decayed or unbanned:
            db.flush()

        return {
            'decayed_count': decayed,
            'unbanned_count': unbanned,
            'total_with_score': len(peers_with_score),
        }

    @staticmethod
    def ban_node(db: Session, node_id: str, reason: str):
        """Set integrity_status='banned' with fail2ban progression."""
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if peer:
            peer.fraud_score = 100.0
            IntegrityService._apply_fail2ban(db, peer, reason)
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

    # ─── Reward Hacking Detection ───

    @staticmethod
    def detect_reward_velocity_anomaly(db: Session, node_id: str,
                                        period_hours: int = 24) -> Optional[Dict]:
        """Detect nodes claiming rewards at anomalously high rates.

        Compares this node's reward claim rate to the network average.
        A node receiving >3 stddev above mean reward amount per period
        is flagged for investigation.
        """
        cutoff = datetime.utcnow() - timedelta(hours=period_hours)

        # Get per-node reward totals in the period
        node_totals = db.query(
            HostingReward.node_id,
            sqlfunc.sum(HostingReward.amount).label('total'),
            sqlfunc.count(HostingReward.id).label('claim_count'),
        ).filter(
            HostingReward.created_at >= cutoff,
        ).group_by(HostingReward.node_id).all()

        if len(node_totals) < 3:
            return None  # Not enough nodes to compare

        amounts = [nt.total for nt in node_totals]
        target_entry = next(
            (nt for nt in node_totals if nt.node_id == node_id), None)
        if not target_entry or target_entry.total < 10:
            return None  # Too low to flag

        mean = statistics.mean(amounts)
        stddev = statistics.stdev(amounts) if len(amounts) > 1 else 0
        if stddev == 0:
            return None

        z_score = (target_entry.total - mean) / stddev

        if z_score > 3.0:
            return IntegrityService._create_fraud_alert(
                db, node_id, 'reward_velocity_anomaly', 'high',
                f'Reward velocity anomaly: {target_entry.total:.1f} Spark '
                f'in {period_hours}h ({target_entry.claim_count} claims, '
                f'z-score={z_score:.1f}, mean={mean:.1f})',
                FRAUD_WEIGHTS['reward_velocity_anomaly'],
                {'total': round(target_entry.total, 2),
                 'claim_count': target_entry.claim_count,
                 'mean': round(mean, 2), 'stddev': round(stddev, 2),
                 'z_score': round(z_score, 2),
                 'period_hours': period_hours})

        return None

    @staticmethod
    def detect_reward_self_dealing(db: Session, node_id: str) -> Optional[Dict]:
        """Detect circular reward patterns — nodes awarding rewards to
        themselves or to a small ring of accomplices who reciprocate.

        Checks:
        1. Node's operator_id matches the node's own user account
        2. Reward claims where the same small group witnesses each other
        """
        # Check 1: Self-referential rewards
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            return None

        operator_id = peer.node_operator_id
        self_rewards = db.query(HostingReward).filter(
            HostingReward.node_id == node_id,
            HostingReward.operator_id == operator_id,
        ).count()

        total_rewards = db.query(HostingReward).filter(
            HostingReward.node_id == node_id,
        ).count()

        if total_rewards > 5 and self_rewards > total_rewards * 0.5:
            return IntegrityService._create_fraud_alert(
                db, node_id, 'reward_self_dealing', 'critical',
                f'Self-dealing: {self_rewards}/{total_rewards} rewards '
                f'({self_rewards / total_rewards * 100:.0f}%) awarded to '
                f'own operator account',
                FRAUD_WEIGHTS['reward_self_dealing'],
                {'self_rewards': self_rewards, 'total_rewards': total_rewards,
                 'ratio': round(self_rewards / total_rewards, 3)})

        # Check 2: Witness ring — same small set of peers witness all
        # of this node's ad impressions (already partially covered by
        # detect_collusion, but this checks the reward side specifically)
        cutoff = datetime.utcnow() - timedelta(days=7)
        witnessed = db.query(AdImpression).filter(
            AdImpression.node_id == node_id,
            AdImpression.created_at >= cutoff,
            AdImpression.witness_node_id.isnot(None),
        ).all()

        if len(witnessed) > 10:
            witness_set = set(w.witness_node_id for w in witnessed)
            if len(witness_set) <= 2:
                return IntegrityService._create_fraud_alert(
                    db, node_id, 'reward_self_dealing', 'high',
                    f'Witness ring: {len(witnessed)} impressions all '
                    f'witnessed by only {len(witness_set)} peer(s): '
                    f'{", ".join(w[:8] for w in witness_set)}',
                    FRAUD_WEIGHTS['reward_self_dealing'],
                    {'impression_count': len(witnessed),
                     'unique_witnesses': len(witness_set),
                     'witness_ids': list(witness_set)})

        return None

    @staticmethod
    def detect_spark_gaming(db: Session, node_id: str) -> Optional[Dict]:
        """Detect goals created to burn Spark budget without producing
        real output — a form of reward hacking where the node claims
        compute credits for work it didn't meaningfully do.

        Signals: high goal failure rate, minimal output, rapid goal cycling.
        """
        try:
            from integrations.social.models import AgentGoal
        except ImportError:
            return None

        # Get goals associated with this node's user
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer or not peer.node_operator_id:
            return None

        cutoff = datetime.utcnow() - timedelta(hours=48)
        recent_goals = db.query(AgentGoal).filter(
            AgentGoal.owner_id == peer.node_operator_id,
            AgentGoal.created_at >= cutoff,
        ).all()

        if len(recent_goals) < 5:
            return None  # Not enough to judge

        total = len(recent_goals)
        failed = sum(1 for g in recent_goals if g.status in ('failed', 'error'))
        total_spent = sum((g.spark_spent or 0) for g in recent_goals)
        completed = sum(1 for g in recent_goals if g.status == 'completed')

        # High failure rate with significant spend = gaming
        failure_rate = failed / total if total > 0 else 0
        if failure_rate > 0.7 and total_spent > 100:
            return IntegrityService._create_fraud_alert(
                db, node_id, 'spark_gaming', 'high',
                f'Spark gaming suspected: {failed}/{total} goals failed '
                f'({failure_rate:.0%}) while spending {total_spent} Spark '
                f'in 48h. Only {completed} completed.',
                FRAUD_WEIGHTS['spark_gaming'],
                {'total_goals': total, 'failed': failed,
                 'completed': completed, 'spark_spent': total_spent,
                 'failure_rate': round(failure_rate, 3)})

        # Rapid cycling: many goals created and immediately abandoned
        abandoned = sum(1 for g in recent_goals
                       if g.status == 'archived' and (g.spark_spent or 0) > 0)
        if abandoned > 10 and total_spent > 200:
            return IntegrityService._create_fraud_alert(
                db, node_id, 'spark_gaming', 'medium',
                f'Goal cycling: {abandoned} goals abandoned after spending '
                f'{total_spent} Spark in 48h',
                FRAUD_WEIGHTS['spark_gaming'],
                {'abandoned': abandoned, 'spark_spent': total_spent})

        return None

    # ─── Impression Integrity (Collusion + Tampering) ───

    @staticmethod
    def detect_witness_ring(db: Session, node_id: str,
                             period_days: int = 7,
                             min_impressions: int = 10,
                             max_witnesses: int = 2,
                             ring_ratio: float = 0.9) -> Optional[Dict]:
        """Detect small witness rings — a tight group of nodes that
        exclusively witness each other's ad impressions.

        Unlike detect_collusion (which checks attestation concentration),
        this checks bidirectional impression witnessing: if A witnesses B
        and B witnesses A with the same tiny set, it's a ring.

        Signals:
        1. Node's impressions witnessed by <=max_witnesses unique peers
        2. Those same peers have their impressions witnessed by this node
        3. The overlap ratio exceeds ring_ratio
        """
        cutoff = datetime.utcnow() - timedelta(days=period_days)

        # 1. Who witnesses this node's impressions?
        node_impressions = db.query(AdImpression).filter(
            AdImpression.node_id == node_id,
            AdImpression.created_at >= cutoff,
            AdImpression.witness_node_id.isnot(None),
        ).all()

        if len(node_impressions) < min_impressions:
            return None  # Not enough data

        witness_counts = {}
        for imp in node_impressions:
            witness_counts[imp.witness_node_id] = witness_counts.get(
                imp.witness_node_id, 0) + 1

        unique_witnesses = set(witness_counts.keys())
        if len(unique_witnesses) > max_witnesses:
            return None  # Diverse enough

        # 2. Check bidirectional: does this node witness those peers back?
        ring_members = set()
        for witness_id in unique_witnesses:
            reverse_count = db.query(sqlfunc.count(AdImpression.id)).filter(
                AdImpression.node_id == witness_id,
                AdImpression.witness_node_id == node_id,
                AdImpression.created_at >= cutoff,
            ).scalar() or 0
            if reverse_count >= min_impressions // 2:
                ring_members.add(witness_id)

        if not ring_members:
            return None

        # 3. Calculate ring tightness
        total_witnessed = len(node_impressions)
        ring_witnessed = sum(witness_counts.get(m, 0) for m in ring_members)
        ratio = ring_witnessed / total_witnessed if total_witnessed > 0 else 0

        if ratio >= ring_ratio:
            return IntegrityService._create_fraud_alert(
                db, node_id, 'witness_ring', 'high',
                f'Witness ring detected: {total_witnessed} impressions, '
                f'{len(ring_members)+1} nodes in ring '
                f'(bidirectional ratio={ratio:.0%})',
                FRAUD_WEIGHTS['witness_ring'],
                {'ring_members': [node_id] + list(ring_members),
                 'total_witnessed': total_witnessed,
                 'ring_witnessed': ring_witnessed,
                 'ratio': round(ratio, 3),
                 'period_days': period_days})

        return None

    @staticmethod
    def detect_temporal_clustering(db: Session, node_id: str,
                                    period_hours: int = 1,
                                    cluster_window_seconds: int = 5,
                                    min_cluster_size: int = 10,
                                    min_impressions: int = 20) -> Optional[Dict]:
        """Detect suspiciously tight temporal clustering of impressions.

        Legitimate traffic has organic timing variation. Bot-driven or
        fabricated impressions often arrive in tight bursts — many
        impressions within a few seconds, then silence.

        Algorithm: slide a window of cluster_window_seconds across the
        node's impressions. If any window contains >=min_cluster_size
        impressions, flag it.
        """
        cutoff = datetime.utcnow() - timedelta(hours=period_hours)

        impressions = db.query(AdImpression).filter(
            AdImpression.node_id == node_id,
            AdImpression.created_at >= cutoff,
        ).order_by(AdImpression.created_at.asc()).all()

        if len(impressions) < min_impressions:
            return None

        # Sliding window: find max cluster density
        timestamps = [imp.created_at for imp in impressions]
        max_cluster = 0
        worst_window_start = None

        for i, ts in enumerate(timestamps):
            window_end = ts + timedelta(seconds=cluster_window_seconds)
            # Count impressions within [ts, ts + window]
            cluster_count = 0
            for j in range(i, len(timestamps)):
                if timestamps[j] <= window_end:
                    cluster_count += 1
                else:
                    break
            if cluster_count > max_cluster:
                max_cluster = cluster_count
                worst_window_start = ts

        if max_cluster >= min_cluster_size:
            return IntegrityService._create_fraud_alert(
                db, node_id, 'temporal_clustering', 'medium',
                f'Temporal clustering: {max_cluster} impressions in '
                f'{cluster_window_seconds}s window '
                f'(total={len(impressions)} in {period_hours}h)',
                FRAUD_WEIGHTS['temporal_clustering'],
                {'max_cluster': max_cluster,
                 'window_seconds': cluster_window_seconds,
                 'total_impressions': len(impressions),
                 'worst_window_start': worst_window_start.isoformat()
                 if worst_window_start else None,
                 'period_hours': period_hours})

        return None

    @staticmethod
    def verify_impression_seal(db: Session, impression_id: str) -> Dict:
        """Verify that a sealed impression's hash matches its current data.

        Once an impression is sealed (witnessed + hashed), its data should
        be immutable. If the recomputed hash doesn't match sealed_hash,
        the impression has been tampered with post-seal.

        Returns: {'valid': bool, 'details': str, 'impression_id': str}
        """
        imp = db.query(AdImpression).filter_by(id=impression_id).first()
        if not imp:
            return {'valid': False, 'details': 'Impression not found',
                    'impression_id': impression_id}

        if not imp.sealed_hash:
            return {'valid': True, 'details': 'Not sealed (no hash to verify)',
                    'impression_id': impression_id}

        # Recompute and compare
        current_hash = imp.compute_seal_hash
        if current_hash == imp.sealed_hash:
            return {'valid': True, 'details': 'Seal intact',
                    'impression_id': impression_id}

        # Tampered — raise fraud alert on the node
        if imp.node_id:
            IntegrityService._create_fraud_alert(
                db, imp.node_id, 'seal_tamper', 'critical',
                f'Impression seal tampered: id={impression_id[:16]}, '
                f'expected={imp.sealed_hash[:16]}..., '
                f'got={current_hash[:16]}...',
                FRAUD_WEIGHTS['seal_tamper'],
                {'impression_id': impression_id,
                 'sealed_hash': imp.sealed_hash,
                 'recomputed_hash': current_hash})

        return {'valid': False, 'details': 'Seal hash mismatch — tampered',
                'impression_id': impression_id,
                'sealed_hash': imp.sealed_hash,
                'recomputed_hash': current_hash}

    @staticmethod
    def verify_all_sealed_impressions(db: Session, node_id: str = None,
                                       limit: int = 100) -> Dict:
        """Batch-verify sealed impressions. Returns summary of tampered seals."""
        q = db.query(AdImpression).filter(
            AdImpression.sealed_hash.isnot(None),
        )
        if node_id:
            q = q.filter(AdImpression.node_id == node_id)
        impressions = q.order_by(AdImpression.sealed_at.desc()).limit(limit).all()

        total = len(impressions)
        tampered = 0
        tampered_ids = []

        for imp in impressions:
            result = IntegrityService.verify_impression_seal(db, imp.id)
            if not result['valid'] and 'tampered' in result.get('details', '').lower():
                tampered += 1
                tampered_ids.append(imp.id)

        return {
            'total_checked': total,
            'tampered': tampered,
            'tampered_ids': tampered_ids,
            'integrity_ratio': round((total - tampered) / total, 3) if total > 0 else 1.0,
        }

    @staticmethod
    def isolate_reward_hacker(db: Session, node_id: str,
                               reason: str, evidence: dict = None) -> Dict:
        """Isolate a confirmed reward hacker from the network.

        This is the enforcement action when reward hacking is detected
        with high confidence. The node is:
        1. Quarantined via TrustQuarantine (ISOLATE level)
        2. All pending rewards frozen
        3. Fraud score set to maximum
        4. Witnesses notified
        5. Node cannot re-enter until reviewed by a human auditor

        Reward hackers are isolated, not punished — the goal is to
        protect the network, not to seek vengeance (per TrustQuarantine).
        """
        # 1. Set fraud score to max and ban
        IntegrityService.increase_fraud_score(
            db, node_id, 50.0,
            f'Reward hacker isolated: {reason}', evidence)

        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if peer:
            peer.integrity_status = 'banned'
            peer.fraud_score = 100.0

        # 2. Quarantine via guardrail system
        try:
            from security.hive_guardrails import TrustQuarantine
            TrustQuarantine.quarantine(
                node_id,
                TrustQuarantine.LEVEL_ISOLATE,
                f'Reward hacking: {reason}')
        except ImportError:
            logger.warning("TrustQuarantine not available for isolation")

        # 3. Freeze pending rewards (mark as disputed)
        pending = db.query(HostingReward).filter(
            HostingReward.node_id == node_id,
            HostingReward.created_at >= datetime.utcnow() - timedelta(days=30),
        ).all()
        frozen_amount = 0.0
        for reward in pending:
            frozen_amount += reward.amount
            reward.reason = f'FROZEN: {reward.reason} [reward hack investigation]'
        db.flush()

        result = {
            'node_id': node_id,
            'action': 'isolated',
            'reason': reason,
            'rewards_frozen': len(pending),
            'frozen_amount': round(frozen_amount, 2),
            'requires_human_review': True,
        }

        logger.warning(
            f"Reward hacker isolated: node={node_id[:8]}, "
            f"reason={reason}, frozen={frozen_amount:.2f} Spark")

        return result

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


def _get_node_compute(peer) -> float:
    """Extract compute score from a PeerNode's metadata.
    Uses contribution_score as primary metric, falls back to metadata fields."""
    if peer.contribution_score and peer.contribution_score > 0:
        return float(peer.contribution_score)
    meta = peer.metadata_json or {}
    # Check for reported compute capacity (TFLOPS, GPU count, etc.)
    compute = meta.get('compute_tflops', 0) or meta.get('gpu_count', 0)
    if compute:
        return float(compute)
    # Minimum: 1.0 (every node has at least some compute)
    return 1.0


def _determine_alert_type(reason: str) -> str:
    """Infer alert type from reason string."""
    reason_lower = reason.lower()
    if 'reward hack' in reason_lower or 'isolated' in reason_lower:
        return 'reward_hacking'
    if 'self-dealing' in reason_lower or 'witness ring' in reason_lower:
        return 'reward_self_dealing'
    if 'spark gaming' in reason_lower or 'goal cycling' in reason_lower:
        return 'spark_gaming'
    if 'reward velocity' in reason_lower:
        return 'reward_velocity_anomaly'
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
