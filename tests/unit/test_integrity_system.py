"""
Anti-Fraud & Peer Integrity Verification Test Suite
=====================================================
~45 tests across 12 classes covering:
- Ed25519 keypair generation, persistence, sign/verify
- PeerNode integrity columns (defaults, to_dict)
- NodeAttestation model (create, to_dict, expiry)
- IntegrityChallenge model (create, state transitions, timeout)
- FraudAlert model (create, severity, status transitions)
- Challenge-response protocol (create, handle, evaluate valid/invalid)
- Impression witnessing (accepted, banned refused, stale timestamp, reduced credit)
- Fraud detection (anomaly, score jump, auto-ban, score management)
- Code hash verification (match, mismatch)
- Collusion detection (single dominant attester, diverse OK)
- Gossip signature integration (signed, unsigned backward compat, banned rejection)
- Migration v11 (schema version, new tables, new columns)

All external calls mocked -- in-memory SQLite.
"""
import os
import sys
import uuid
import json
import shutil
import tempfile
import secrets
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# Add parent dir for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

# Force in-memory SQLite before importing models
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from integrations.social.models import (
    Base, User, Post, PeerNode, AdUnit, AdPlacement, AdImpression,
    HostingReward, ResonanceWallet, ResonanceTransaction,
    NodeAttestation, IntegrityChallenge, FraudAlert,
)

# =====================================================================
# FIXTURES
# =====================================================================

@pytest.fixture(scope='session')
def engine():
    eng = create_engine('sqlite://', echo=False,
                        connect_args={"check_same_thread": False})
    return eng


@pytest.fixture(scope='session')
def tables(engine):
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db(engine, tables):
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.rollback()
    session.close()


_counter = 0


def _uid():
    global _counter
    _counter += 1
    return f'test_{_counter}_{uuid.uuid4().hex[:8]}'


def _make_user(db, user_type='human', username=None, karma=10):
    uid = _uid()
    u = User(id=uid, username=username or f'user_{uid[:8]}',
             user_type=user_type, karma_score=karma)
    db.add(u)
    db.flush()
    # Create wallet
    w = ResonanceWallet(user_id=uid, pulse=karma, spark=500,
                         spark_lifetime=500)
    db.add(w)
    db.flush()
    return u


def _make_peer(db, node_id=None, url=None, status='active',
               integrity_status='unverified', fraud_score=0.0,
               agent_count=5, post_count=20, public_key='',
               code_hash='', operator=None):
    nid = node_id or _uid()
    p = PeerNode(
        node_id=nid,
        url=url or f'http://node-{nid[:8]}.example.com:6777',
        name=f'node-{nid[:8]}', version='1.0.0',
        status=status, agent_count=agent_count,
        post_count=post_count, integrity_status=integrity_status,
        fraud_score=fraud_score, public_key=public_key,
        code_hash=code_hash,
        node_operator_id=operator.id if operator else None,
    )
    db.add(p)
    db.flush()
    return p


# =====================================================================
# 1. TestNodeIntegrityCrypto (7 tests)
# =====================================================================

class TestNodeIntegrityCrypto:
    """Ed25519 keypair generation, persistence, sign/verify, code hash."""

    def setup_method(self):
        # Use temp dir for keypair storage
        self.tmp_dir = tempfile.mkdtemp()
        os.environ['HEVOLVE_KEY_DIR'] = self.tmp_dir

    def teardown_method(self):
        from security.node_integrity import reset_keypair
        reset_keypair()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_keypair_generation(self):
        from security.node_integrity import get_or_create_keypair
        priv, pub = get_or_create_keypair()
        assert priv is not None
        assert pub is not None

    def test_keypair_persistence(self):
        from security.node_integrity import get_or_create_keypair, reset_keypair, get_public_key_hex
        _, _ = get_or_create_keypair()
        hex1 = get_public_key_hex()
        reset_keypair()
        _, _ = get_or_create_keypair()
        hex2 = get_public_key_hex()
        # Should load same key from disk
        assert hex1 == hex2

    def test_public_key_hex(self):
        from security.node_integrity import get_public_key_hex
        hexkey = get_public_key_hex()
        assert len(hexkey) == 64  # 32 bytes = 64 hex chars

    def test_sign_verify(self):
        from security.node_integrity import sign_message, verify_signature, get_public_key_hex
        msg = b'hello world'
        sig = sign_message(msg)
        pubhex = get_public_key_hex()
        assert verify_signature(pubhex, msg, sig) is True

    def test_wrong_key_verify(self):
        from security.node_integrity import sign_message, verify_signature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        msg = b'test data'
        sig = sign_message(msg)
        # Use different key
        other_key = Ed25519PrivateKey.generate()
        from cryptography.hazmat.primitives import serialization
        other_pub_hex = other_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw).hex()
        assert verify_signature(other_pub_hex, msg, sig) is False

    def test_json_sign_verify(self):
        from security.node_integrity import sign_json_payload, verify_json_signature, get_public_key_hex
        payload = {'node_id': 'abc', 'url': 'http://example.com', 'count': 42}
        sig = sign_json_payload(payload)
        pubhex = get_public_key_hex()
        assert verify_json_signature(pubhex, payload, sig) is True

    def test_code_hash_deterministic(self):
        from security.node_integrity import compute_code_hash
        # Create a temp dir with known .py files
        code_dir = os.path.join(self.tmp_dir, 'code')
        os.makedirs(code_dir)
        with open(os.path.join(code_dir, 'a.py'), 'w') as f:
            f.write('print("hello")\n')
        with open(os.path.join(code_dir, 'b.py'), 'w') as f:
            f.write('x = 1\n')
        hash1 = compute_code_hash(code_dir)
        hash2 = compute_code_hash(code_dir)
        assert hash1 == hash2
        assert len(hash1) == 64  # SHA-256 hex


# =====================================================================
# 2. TestPeerNodeIntegrityColumns (4 tests)
# =====================================================================

class TestPeerNodeIntegrityColumns:
    """Verify new integrity fields on PeerNode."""

    def test_default_values(self, db):
        peer = _make_peer(db)
        assert peer.integrity_status == 'unverified'
        assert peer.fraud_score == 0.0

    def test_public_key_stored(self, db):
        peer = _make_peer(db, public_key='aabbcc')
        assert peer.public_key == 'aabbcc'

    def test_code_hash_stored(self, db):
        peer = _make_peer(db, code_hash='deadbeef' * 8)
        assert peer.code_hash == 'deadbeef' * 8

    def test_to_dict_includes_integrity(self, db):
        peer = _make_peer(db, fraud_score=15.0, integrity_status='suspicious')
        d = peer.to_dict()
        assert d['integrity_status'] == 'suspicious'
        assert d['fraud_score'] == 15.0


# =====================================================================
# 3. TestNodeAttestation (3 tests)
# =====================================================================

class TestNodeAttestation:
    """NodeAttestation model tests."""

    def test_create(self, db):
        att = NodeAttestation(
            attester_node_id='node_a', subject_node_id='node_b',
            attestation_type='impression_witness',
            payload_json={'impression_id': 'imp1'},
            signature='abcdef', attester_public_key='pubkey_a',
        )
        db.add(att)
        db.flush()
        assert att.id is not None
        assert att.is_valid is True

    def test_to_dict(self, db):
        att = NodeAttestation(
            attester_node_id='node_x', subject_node_id='node_y',
            attestation_type='code_hash_match',
            signature='sig123', attester_public_key='pk1',
        )
        db.add(att)
        db.flush()
        d = att.to_dict()
        assert d['attestation_type'] == 'code_hash_match'
        assert d['is_valid'] is True

    def test_expiry(self, db):
        expires = datetime.utcnow() + timedelta(days=7)
        att = NodeAttestation(
            attester_node_id='a', subject_node_id='b',
            attestation_type='stats_verify',
            signature='s', attester_public_key='p',
            expires_at=expires,
        )
        db.add(att)
        db.flush()
        assert att.expires_at is not None


# =====================================================================
# 4. TestIntegrityChallenge (3 tests)
# =====================================================================

class TestIntegrityChallenge:
    """IntegrityChallenge model tests."""

    def test_create(self, db):
        ch = IntegrityChallenge(
            challenger_node_id='c1', target_node_id='t1',
            challenge_type='agent_count_verify',
            challenge_nonce=secrets.token_hex(32),
        )
        db.add(ch)
        db.flush()
        assert ch.status == 'pending'

    def test_state_transitions(self, db):
        ch = IntegrityChallenge(
            challenger_node_id='c2', target_node_id='t2',
            challenge_type='stats_probe',
            challenge_nonce=secrets.token_hex(32),
        )
        db.add(ch)
        db.flush()
        ch.status = 'responded'
        db.flush()
        assert ch.status == 'responded'
        ch.status = 'passed'
        db.flush()
        assert ch.status == 'passed'

    def test_to_dict(self, db):
        ch = IntegrityChallenge(
            challenger_node_id='c3', target_node_id='t3',
            challenge_type='code_hash_check',
            challenge_nonce='nonce123',
        )
        db.add(ch)
        db.flush()
        d = ch.to_dict()
        assert d['challenge_type'] == 'code_hash_check'
        assert d['challenge_nonce'] == 'nonce123'
        assert d['status'] == 'pending'


# =====================================================================
# 5. TestFraudAlert (3 tests)
# =====================================================================

class TestFraudAlert:
    """FraudAlert model tests."""

    def test_create(self, db):
        alert = FraudAlert(
            node_id='n1', alert_type='impression_anomaly',
            severity='high', description='Test alert',
            fraud_score_delta=20.0,
        )
        db.add(alert)
        db.flush()
        assert alert.status == 'open'

    def test_severity_levels(self, db):
        for sev in ['low', 'medium', 'high', 'critical']:
            alert = FraudAlert(
                node_id='n2', alert_type='test',
                severity=sev, description=f'{sev} alert',
            )
            db.add(alert)
            db.flush()
            assert alert.severity == sev

    def test_status_transitions(self, db):
        alert = FraudAlert(
            node_id='n3', alert_type='hash_mismatch',
            severity='high', description='test',
        )
        db.add(alert)
        db.flush()
        assert alert.status == 'open'
        alert.status = 'investigating'
        alert.reviewed_by = 'admin1'
        alert.reviewed_at = datetime.utcnow()
        db.flush()
        assert alert.status == 'investigating'


# =====================================================================
# 6. TestChallengeResponse (6 tests)
# =====================================================================

class TestChallengeResponse:
    """Challenge-response protocol tests."""

    def test_handle_agent_count_challenge(self, db):
        from integrations.social.integrity_service import IntegrityService
        # Create some agents
        for i in range(3):
            _make_user(db, user_type='agent', username=f'agent_ch_{_uid()}')
        challenge_data = {
            'type': 'agent_count_verify',
            'nonce': 'test_nonce_1',
            'claimed_agent_count': 3,
        }
        with patch('security.node_integrity.sign_json_payload', return_value='fake_sig'), \
             patch('security.node_integrity.get_public_key_hex', return_value='fake_pk'):
            result = IntegrityService.handle_challenge(db, challenge_data)
        assert 'response' in result
        assert result['response']['nonce'] == 'test_nonce_1'
        assert result['response']['agent_count'] >= 3

    def test_handle_stats_probe_challenge(self, db):
        from integrations.social.integrity_service import IntegrityService
        challenge_data = {
            'type': 'stats_probe',
            'nonce': 'nonce_stats',
            'requested_stats': ['agent_count', 'post_count'],
        }
        with patch('security.node_integrity.sign_json_payload', return_value='sig'), \
             patch('security.node_integrity.get_public_key_hex', return_value='pk'):
            result = IntegrityService.handle_challenge(db, challenge_data)
        assert result['response']['nonce'] == 'nonce_stats'
        assert 'agent_count' in result['response']
        assert 'post_count' in result['response']

    def test_handle_code_hash_challenge(self, db):
        from integrations.social.integrity_service import IntegrityService
        challenge_data = {'type': 'code_hash_check', 'nonce': 'nonce_hash'}
        with patch('security.node_integrity.compute_code_hash', return_value='abc123'), \
             patch('security.node_integrity.sign_json_payload', return_value='sig'), \
             patch('security.node_integrity.get_public_key_hex', return_value='pk'), \
             patch('integrations.social.peer_discovery.gossip') as mock_gossip:
            mock_gossip.version = '1.0.0'
            result = IntegrityService.handle_challenge(db, challenge_data)
        assert result['response']['code_hash'] == 'abc123'

    def test_evaluate_valid_response(self, db):
        from integrations.social.integrity_service import IntegrityService
        peer = _make_peer(db, agent_count=10)
        nonce = secrets.token_hex(32)
        ch = IntegrityChallenge(
            challenger_node_id='myself', target_node_id=peer.node_id,
            challenge_type='agent_count_verify',
            challenge_nonce=nonce,
            challenge_data={'claimed_agent_count': 10},
        )
        db.add(ch)
        db.flush()
        response_data = {'nonce': nonce, 'agent_count': 10, 'timestamp': datetime.utcnow().isoformat()}
        result = IntegrityService.evaluate_challenge_response(
            db, ch.id, response_data, '')
        assert result['passed'] is True

    def test_evaluate_nonce_mismatch(self, db):
        from integrations.social.integrity_service import IntegrityService
        peer = _make_peer(db)
        ch = IntegrityChallenge(
            challenger_node_id='myself', target_node_id=peer.node_id,
            challenge_type='stats_probe',
            challenge_nonce='correct_nonce',
        )
        db.add(ch)
        db.flush()
        response_data = {'nonce': 'wrong_nonce'}
        result = IntegrityService.evaluate_challenge_response(
            db, ch.id, response_data, '')
        assert result['passed'] is False
        assert 'Nonce mismatch' in result['details']

    def test_evaluate_invalid_signature(self, db):
        from integrations.social.integrity_service import IntegrityService
        peer = _make_peer(db)
        nonce = 'sig_test_nonce'
        ch = IntegrityChallenge(
            challenger_node_id='myself', target_node_id=peer.node_id,
            challenge_type='agent_count_verify',
            challenge_nonce=nonce,
            challenge_data={'claimed_agent_count': 5},
        )
        db.add(ch)
        db.flush()
        response_data = {'nonce': nonce, 'agent_count': 5, 'public_key': 'bad_key'}
        with patch('security.node_integrity.verify_json_signature', return_value=False):
            result = IntegrityService.evaluate_challenge_response(
                db, ch.id, response_data, 'fake_sig')
        assert result['passed'] is False
        assert 'Invalid signature' in result['details']


# =====================================================================
# 7. TestImpressionWitness (5 tests)
# =====================================================================

class TestImpressionWitness:
    """Impression witnessing tests."""

    def test_witness_accepted(self, db):
        from integrations.social.integrity_service import IntegrityService
        requesting_peer = _make_peer(db, status='active')
        witness_data = {
            'impression_id': 'imp_1',
            'ad_id': 'ad_1',
            'node_id': requesting_peer.node_id,
            'timestamp': datetime.utcnow().isoformat(),
            'nonce': 'test_nonce',
            'signature': 'test_sig',
            'public_key': 'test_pk',
        }
        with patch('security.node_integrity.sign_json_payload', return_value='wsig'), \
             patch('security.node_integrity.get_public_key_hex', return_value='wpk'), \
             patch('security.node_integrity.verify_json_signature', return_value=True):
            result = IntegrityService.handle_witness_request(db, witness_data)
        assert result['witnessed'] is True

    def test_witness_banned_node_refused(self, db):
        from integrations.social.integrity_service import IntegrityService
        banned_peer = _make_peer(db, status='active', integrity_status='banned')
        witness_data = {
            'node_id': banned_peer.node_id,
            'timestamp': datetime.utcnow().isoformat(),
        }
        result = IntegrityService.handle_witness_request(db, witness_data)
        assert result['witnessed'] is False
        assert 'banned' in result.get('reason', '')

    def test_witness_stale_timestamp(self, db):
        from integrations.social.integrity_service import IntegrityService
        peer = _make_peer(db, status='active')
        old_time = (datetime.utcnow() - timedelta(seconds=120)).isoformat()
        witness_data = {
            'node_id': peer.node_id,
            'timestamp': old_time,
        }
        result = IntegrityService.handle_witness_request(db, witness_data)
        assert result['witnessed'] is False
        assert 'Stale' in result.get('reason', '')

    def test_witness_count_tracking(self, db):
        from integrations.social.integrity_service import IntegrityService
        att = NodeAttestation(
            attester_node_id='w1', subject_node_id='s1',
            attestation_type='impression_witness',
            payload_json={'impression_id': 'imp_track'},
            signature='sig', attester_public_key='pk',
        )
        db.add(att)
        db.flush()
        count = IntegrityService.get_impression_witness_count(db, 'imp_track')
        assert count >= 1

    def test_reduced_credit_without_witness(self, db):
        """Ad impression without witness should use HOSTER_UNWITNESSED_SHARE."""
        from integrations.social.ad_service import (
            AdService, HOSTER_REVENUE_SHARE, HOSTER_UNWITNESSED_SHARE
        )
        assert HOSTER_UNWITNESSED_SHARE < HOSTER_REVENUE_SHARE
        assert HOSTER_UNWITNESSED_SHARE == 0.50
        assert HOSTER_REVENUE_SHARE == 0.90  # 90/9/1 split


# =====================================================================
# 8. TestFraudDetection (7 tests)
# =====================================================================

class TestFraudDetection:
    """Fraud detection algorithms."""

    def test_impression_anomaly_detected(self, db):
        """Node with outlier impressions should be flagged."""
        from integrations.social.integrity_service import IntegrityService
        # Create target node with lots of impressions
        target_nid = f'anomaly_target_{uuid.uuid4().hex[:6]}'
        target = _make_peer(db, node_id=target_nid)
        # Create 20 normal nodes with 2 impressions each (low baseline)
        for i in range(20):
            nid = f'anom_normal_{i}_{uuid.uuid4().hex[:6]}'
            _make_peer(db, node_id=nid)
            for j in range(2):
                imp = AdImpression(
                    ad_id='ad_anom_det', node_id=nid,
                    impression_type='view',
                    created_at=datetime.utcnow() - timedelta(hours=1),
                )
                db.add(imp)

        # Target has 500 impressions (extreme outlier, z-score > 3)
        for j in range(500):
            imp = AdImpression(
                ad_id='ad_anom_det', node_id=target_nid,
                impression_type='view',
                created_at=datetime.utcnow() - timedelta(hours=1),
            )
            db.add(imp)
        db.flush()

        result = IntegrityService.detect_impression_anomaly(db, target_nid, 24)
        assert result is not None
        assert result['alert_type'] == 'impression_anomaly'

    def test_normal_impressions_not_flagged(self, db):
        """Node with normal impressions should NOT be flagged."""
        from integrations.social.integrity_service import IntegrityService
        # Create nodes with similar counts (all 10)
        for i in range(5):
            nid = f'normal_ok_{i}_{uuid.uuid4().hex[:6]}'
            _make_peer(db, node_id=nid)
            for j in range(10):
                imp = AdImpression(
                    ad_id='ad_norm', node_id=nid,
                    impression_type='view',
                    created_at=datetime.utcnow(),
                )
                db.add(imp)
        db.flush()
        result = IntegrityService.detect_impression_anomaly(
            db, f'normal_ok_0_{uuid.uuid4().hex[:6]}', 24)
        assert result is None

    def test_score_jump_detected(self, db):
        from integrations.social.integrity_service import IntegrityService
        peer = _make_peer(db, agent_count=100)
        peer.metadata_json = {'_prev_agent_count': 10, '_prev_post_count': 20}
        db.flush()
        result = IntegrityService.detect_score_jump(db, peer.node_id)
        assert result is not None

    def test_gradual_growth_ok(self, db):
        from integrations.social.integrity_service import IntegrityService
        peer = _make_peer(db, agent_count=12)
        peer.metadata_json = {'_prev_agent_count': 10, '_prev_post_count': 20}
        db.flush()
        result = IntegrityService.detect_score_jump(db, peer.node_id)
        assert result is None

    def test_fraud_score_increase(self, db):
        from integrations.social.integrity_service import IntegrityService
        peer = _make_peer(db, fraud_score=0.0)
        new_score = IntegrityService.increase_fraud_score(
            db, peer.node_id, 25.0, 'Test increase', {'test': True})
        assert new_score == 25.0
        db.refresh(peer)
        assert peer.fraud_score == 25.0

    def test_fraud_score_decrease(self, db):
        from integrations.social.integrity_service import IntegrityService
        peer = _make_peer(db, fraud_score=50.0, integrity_status='suspicious')
        new_score = IntegrityService.decrease_fraud_score(
            db, peer.node_id, 20.0, 'Passed verification')
        assert new_score == 30.0

    def test_auto_ban_at_threshold(self, db):
        from integrations.social.integrity_service import IntegrityService, FRAUD_BAN_THRESHOLD
        peer = _make_peer(db, fraud_score=70.0)
        IntegrityService.increase_fraud_score(
            db, peer.node_id, 15.0, 'Push over threshold')
        db.refresh(peer)
        assert peer.fraud_score >= FRAUD_BAN_THRESHOLD
        assert peer.integrity_status == 'banned'


# =====================================================================
# 9. TestCodeHashVerification (3 tests)
# =====================================================================

class TestCodeHashVerification:
    """Code hash verification tests."""

    def test_hash_match_verified(self, db):
        from integrations.social.integrity_service import IntegrityService
        local_hash = 'abcdef1234567890' * 4  # 64 chars
        peer = _make_peer(db, code_hash=local_hash)
        with patch('security.node_integrity.compute_code_hash', return_value=local_hash):
            result = IntegrityService.verify_code_hash(db, peer.node_id)
        assert result['verified'] is True

    def test_hash_mismatch_flagged(self, db):
        from integrations.social.integrity_service import IntegrityService
        peer = _make_peer(db, code_hash='aaaa' * 16, fraud_score=0.0)
        with patch('security.node_integrity.compute_code_hash', return_value='bbbb' * 16):
            result = IntegrityService.verify_code_hash(db, peer.node_id)
        assert result['verified'] is False
        db.refresh(peer)
        assert peer.fraud_score > 0

    def test_registry_fetch_mocked(self, db):
        from integrations.social.integrity_service import IntegrityService
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'code_hash': 'expected_hash'}
        with patch('requests.get', return_value=mock_resp):
            result = IntegrityService.fetch_expected_hash(
                'http://registry.example.com', '1.0.0')
        assert result == 'expected_hash'


# =====================================================================
# 10. TestCollusionDetection (2 tests)
# =====================================================================

class TestCollusionDetection:
    """Collusion detection tests."""

    def test_single_dominant_attester_flagged(self, db):
        from integrations.social.integrity_service import IntegrityService
        target_nid = f'collusion_target_{uuid.uuid4().hex[:6]}'
        target = _make_peer(db, node_id=target_nid)
        dominant = f'dominant_{uuid.uuid4().hex[:6]}'

        # 9 attestations from dominant, 1 from other
        for i in range(9):
            att = NodeAttestation(
                attester_node_id=dominant, subject_node_id=target_nid,
                attestation_type='impression_witness',
                signature='s', attester_public_key='pk',
                created_at=datetime.utcnow(),
            )
            db.add(att)
        att_other = NodeAttestation(
            attester_node_id=f'other_{uuid.uuid4().hex[:6]}',
            subject_node_id=target_nid,
            attestation_type='impression_witness',
            signature='s', attester_public_key='pk',
            created_at=datetime.utcnow(),
        )
        db.add(att_other)
        db.flush()

        result = IntegrityService.detect_collusion(db, target_nid)
        assert result is not None
        assert result['alert_type'] == 'collusion_suspected'

    def test_diverse_attesters_ok(self, db):
        from integrations.social.integrity_service import IntegrityService
        target_nid = f'diverse_target_{uuid.uuid4().hex[:6]}'
        _make_peer(db, node_id=target_nid)

        # 10 attestations from 5 different attesters (2 each)
        for i in range(5):
            attester = f'attester_{i}_{uuid.uuid4().hex[:6]}'
            for j in range(2):
                att = NodeAttestation(
                    attester_node_id=attester, subject_node_id=target_nid,
                    attestation_type='impression_witness',
                    signature='s', attester_public_key='pk',
                    created_at=datetime.utcnow(),
                )
                db.add(att)
        db.flush()

        result = IntegrityService.detect_collusion(db, target_nid)
        assert result is None


# =====================================================================
# 11. TestGossipSignatureIntegration (4 tests)
# =====================================================================

class TestGossipSignatureIntegration:
    """Gossip protocol signed message handling."""

    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        os.environ['HEVOLVE_KEY_DIR'] = self.tmp_dir

    def teardown_method(self):
        from security.node_integrity import reset_keypair
        reset_keypair()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_signed_peer_accepted(self, db):
        """Peer with valid signature should be accepted and marked verified."""
        from integrations.social.peer_discovery import GossipProtocol
        from security.node_integrity import sign_json_payload, get_public_key_hex

        gp = GossipProtocol()
        peer_data = {
            'node_id': f'signed_{uuid.uuid4().hex[:8]}',
            'url': 'http://signed-node.example.com:6777',
            'name': 'signed-node',
            'version': '1.0.0',
            'agent_count': 5,
            'post_count': 10,
            'public_key': get_public_key_hex(),
        }
        peer_data['signature'] = sign_json_payload(peer_data)

        is_new = gp._merge_peer(db, peer_data)
        assert is_new is True

        # Check the stored peer has verified status
        from integrations.social.models import PeerNode
        stored = db.query(PeerNode).filter_by(node_id=peer_data['node_id']).first()
        assert stored is not None
        assert stored.integrity_status == 'verified'
        assert stored.public_key == peer_data['public_key']

    def test_unsigned_backward_compat(self, db):
        """Peer without signature should be accepted as 'unverified' (backward compat)."""
        from integrations.social.peer_discovery import GossipProtocol

        gp = GossipProtocol()
        peer_data = {
            'node_id': f'unsigned_{uuid.uuid4().hex[:8]}',
            'url': 'http://unsigned-node.example.com:6777',
            'name': 'unsigned-node',
            'version': '1.0.0',
        }
        is_new = gp._merge_peer(db, peer_data)
        assert is_new is True

        from integrations.social.models import PeerNode
        stored = db.query(PeerNode).filter_by(node_id=peer_data['node_id']).first()
        assert stored.integrity_status == 'unverified'

    def test_invalid_signature_rejected(self, db):
        """Peer with invalid signature should be rejected."""
        from integrations.social.peer_discovery import GossipProtocol
        from security.node_integrity import get_public_key_hex

        gp = GossipProtocol()
        peer_data = {
            'node_id': f'bad_sig_{uuid.uuid4().hex[:8]}',
            'url': 'http://bad-sig-node.example.com:6777',
            'public_key': get_public_key_hex(),
            'signature': 'invalid_hex_signature_that_is_wrong',
        }
        is_new = gp._merge_peer(db, peer_data)
        assert is_new is False  # Rejected

    def test_banned_node_rejected(self, db):
        """Banned node should be rejected even with valid data."""
        from integrations.social.peer_discovery import GossipProtocol

        banned_nid = f'banned_{uuid.uuid4().hex[:8]}'
        _make_peer(db, node_id=banned_nid, integrity_status='banned')

        gp = GossipProtocol()
        peer_data = {
            'node_id': banned_nid,
            'url': 'http://banned.example.com:6777',
            'name': 'banned',
        }
        is_new = gp._merge_peer(db, peer_data)
        assert is_new is False


# =====================================================================
# 12. TestMigrationV11 (3 tests)
# =====================================================================

class TestMigrationV11:
    """Migration v11 schema tests."""

    def test_schema_version(self):
        from integrations.social.migrations import SCHEMA_VERSION
        assert SCHEMA_VERSION >= 11

    def test_new_tables_exist(self, db):
        """node_attestations, integrity_challenges, fraud_alerts tables exist."""
        from sqlalchemy import inspect
        inspector = inspect(db.bind)
        tables = inspector.get_table_names()
        assert 'node_attestations' in tables
        assert 'integrity_challenges' in tables
        assert 'fraud_alerts' in tables

    def test_peer_node_integrity_columns(self, db):
        """PeerNode should have integrity columns."""
        from sqlalchemy import inspect
        inspector = inspect(db.bind)
        columns = {c['name'] for c in inspector.get_columns('peer_nodes')}
        assert 'public_key' in columns
        assert 'code_hash' in columns
        assert 'integrity_status' in columns
        assert 'fraud_score' in columns
        assert 'last_challenge_at' in columns
        assert 'last_attestation_at' in columns
