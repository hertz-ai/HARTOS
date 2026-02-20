"""
Tests for the Continual Learner Gate — CCT issuance, validation, tier computation,
access matrix, learning tools, API endpoints, WorldModelBridge gating, and rewards.

The learner is the incentive. Intelligence is earned through contribution.
"""
import base64
import json
import os
import time
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest

# Ensure in-memory DB
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from integrations.agent_engine.continual_learner_gate import (
    ContinualLearnerGateService,
    LEARNING_TIER_THRESHOLDS,
    LEARNING_ACCESS_MATRIX,
    MINIMUM_CAPABILITY_TIER,
    CAPABILITY_TIER_ORDER,
    CCT_VALIDITY_HOURS,
    CCT_CLOCK_SKEW_SECONDS,
    register_trusted_issuer,
    get_trusted_issuers,
    _trusted_issuers,
)


# ─── Fixtures ───

@pytest.fixture(scope='session')
def engine():
    from sqlalchemy import create_engine
    from integrations.social.models import Base
    eng = create_engine('sqlite://', echo=False)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db(engine):
    from sqlalchemy.orm import Session
    session = Session(bind=engine)
    yield session
    session.rollback()
    session.close()


def _make_peer(db, node_id='test_node_1', contribution_score=0.0,
               integrity_status='unverified', capability_tier='observer',
               status='active', node_operator=None):
    """Create a PeerNode in the test DB."""
    from integrations.social.models import PeerNode
    peer = PeerNode(
        node_id=node_id,
        url=f'http://{node_id}:6777',
        status=status,
        contribution_score=contribution_score,
        integrity_status=integrity_status,
        capability_tier=capability_tier,
        node_operator=node_operator,
    )
    db.add(peer)
    db.flush()
    return peer


def _make_user(db, user_id='user_1'):
    """Create a User in the test DB."""
    from integrations.social.models import User
    user = User(id=user_id, username=f'user_{user_id}')
    db.add(user)
    db.flush()
    return user


# ─── Test: Tier Computation ───

class TestTierComputation:
    def test_node_not_found(self, db):
        result = ContinualLearnerGateService.compute_learning_tier(
            db, 'nonexistent')
        assert result['tier'] == 'none'
        assert result['reason'] == 'node_not_found'

    def test_unverified_node_gets_none(self, db):
        _make_peer(db, 'tv1', 500, 'unverified', 'compute_host')
        result = ContinualLearnerGateService.compute_learning_tier(db, 'tv1')
        assert result['tier'] == 'none'
        assert 'integrity_unverified' in result['reason']

    def test_banned_node_gets_none(self, db):
        from integrations.social.models import PeerNode
        peer = _make_peer(db, 'tv2', 500, 'verified', 'compute_host')
        peer.ban_until = datetime.utcnow() + timedelta(hours=1)
        db.flush()
        result = ContinualLearnerGateService.compute_learning_tier(db, 'tv2')
        assert result['tier'] == 'none'
        assert result['reason'] == 'banned'

    def test_basic_tier(self, db):
        _make_peer(db, 'tv3', 60, 'verified', 'standard')
        result = ContinualLearnerGateService.compute_learning_tier(db, 'tv3')
        assert result['tier'] == 'basic'
        assert result['eligible']
        assert 'temporal_coherence' in result['capabilities']

    def test_full_tier(self, db):
        _make_peer(db, 'tv4', 250, 'verified', 'full')
        result = ContinualLearnerGateService.compute_learning_tier(db, 'tv4')
        assert result['tier'] == 'full'
        assert 'manifold_credit' in result['capabilities']
        assert 'meta_learning' in result['capabilities']

    def test_host_tier(self, db):
        _make_peer(db, 'tv5', 600, 'verified', 'compute_host')
        result = ContinualLearnerGateService.compute_learning_tier(db, 'tv5')
        assert result['tier'] == 'host'
        assert 'reality_grounded' in result['capabilities']
        assert 'hivemind_query' in result['capabilities']
        assert 'skill_distribution' in result['capabilities']

    def test_high_score_low_capability_limits_tier(self, db):
        """Score is high but capability_tier is too low — caps the tier."""
        _make_peer(db, 'tv6', 600, 'verified', 'standard')
        result = ContinualLearnerGateService.compute_learning_tier(db, 'tv6')
        # standard can only be basic (minimum capability for full is 'full')
        assert result['tier'] == 'basic'

    def test_below_threshold_gets_none(self, db):
        _make_peer(db, 'tv7', 10, 'verified', 'standard')
        result = ContinualLearnerGateService.compute_learning_tier(db, 'tv7')
        assert result['tier'] == 'none'
        assert not result['eligible']


# ─── Test: CCT Creation ───

class TestCCTCreation:
    @patch('security.node_integrity.sign_json_payload',
           return_value='a' * 128)
    @patch('security.node_integrity.get_public_key_hex',
           return_value='b' * 64)
    @patch('security.node_integrity.get_node_identity',
           return_value={'node_id': 'issuer_1'})
    def test_issue_valid_cct(self, mock_identity, mock_pub, mock_sign, db):
        _make_peer(db, 'cc1', 100, 'verified', 'standard')
        result = ContinualLearnerGateService.issue_cct(db, 'cc1')
        assert result is not None
        assert result['tier'] == 'basic'
        assert 'temporal_coherence' in result['capabilities']
        assert '.' in result['cct']
        assert result['expires_at'].endswith('Z')

    def test_issue_denied_for_ineligible(self, db):
        _make_peer(db, 'cc2', 5, 'unverified', 'observer')
        result = ContinualLearnerGateService.issue_cct(db, 'cc2')
        assert result is None

    @patch('security.node_integrity.sign_json_payload',
           return_value='c' * 128)
    @patch('security.node_integrity.get_public_key_hex',
           return_value='d' * 64)
    @patch('security.node_integrity.get_node_identity',
           return_value={'node_id': 'issuer_2'})
    def test_issue_creates_attestation(self, mock_id, mock_pub, mock_sign, db):
        _make_peer(db, 'cc3', 600, 'verified', 'compute_host')
        result = ContinualLearnerGateService.issue_cct(db, 'cc3')
        assert result is not None

        from integrations.social.models import NodeAttestation
        att = db.query(NodeAttestation).filter_by(
            subject_node_id='cc3',
            attestation_type='cct_issued',
        ).first()
        assert att is not None
        assert att.is_valid is True
        assert att.payload_json['tier'] == 'host'

    @patch('security.node_integrity.sign_json_payload',
           return_value='e' * 128)
    @patch('security.node_integrity.get_public_key_hex',
           return_value='f' * 64)
    @patch('security.node_integrity.get_node_identity',
           return_value={'node_id': 'issuer_3'})
    def test_issue_correct_tier_host(self, mock_id, mock_pub, mock_sign, db):
        _make_peer(db, 'cc4', 500, 'verified', 'compute_host')
        result = ContinualLearnerGateService.issue_cct(db, 'cc4')
        assert result['tier'] == 'host'
        assert len(result['capabilities']) == 6  # all capabilities


# ─── Test: CCT Validation ───

class TestCCTValidation:
    def _make_cct(self, sub='node_1', tier='basic', exp_offset=3600,
                  iss_pub='ab' * 32, nonce='test123'):
        """Build a test CCT string (payload_b64.signature_hex)."""
        payload = {
            'sub': sub,
            'pub': 'standard',
            'tier': tier,
            'cs': 100.0,
            'ist': 'verified',
            'iat': int(time.time()),
            'exp': int(time.time()) + exp_offset,
            'iss': iss_pub,
            'nonce': nonce,
        }
        payload_json = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode()
        sig_hex = 'ab' * 64  # Dummy signature
        return f"{payload_b64}.{sig_hex}", payload

    def test_malformed_token(self):
        result = ContinualLearnerGateService.validate_cct('not-a-token')
        assert not result['valid']
        assert result['reason'] == 'malformed_token'

    def test_decode_error(self):
        result = ContinualLearnerGateService.validate_cct('bad_base64.abc')
        assert not result['valid']
        assert result['reason'] == 'decode_error'

    @patch('security.node_integrity.get_public_key_hex',
           return_value='ab' * 32)
    @patch('security.node_integrity.verify_json_signature',
           return_value=True)
    def test_valid_cct(self, mock_verify, mock_pub):
        cct, payload = self._make_cct()
        result = ContinualLearnerGateService.validate_cct(cct)
        assert result['valid']
        assert result['tier'] == 'basic'
        assert 'temporal_coherence' in result['capabilities']
        assert result['expires_in'] > 0

    @patch('security.node_integrity.get_public_key_hex',
           return_value='ab' * 32)
    @patch('security.node_integrity.verify_json_signature',
           return_value=True)
    def test_expired_cct(self, mock_verify, mock_pub):
        cct, _ = self._make_cct(exp_offset=-(CCT_CLOCK_SKEW_SECONDS + 100))
        result = ContinualLearnerGateService.validate_cct(cct)
        assert not result['valid']
        assert result['reason'] == 'expired'

    @patch('security.node_integrity.get_public_key_hex',
           return_value='ab' * 32)
    @patch('security.node_integrity.verify_json_signature',
           return_value=True)
    def test_node_mismatch(self, mock_verify, mock_pub):
        cct, _ = self._make_cct(sub='node_A')
        result = ContinualLearnerGateService.validate_cct(
            cct, expected_node_id='node_B')
        assert not result['valid']
        assert result['reason'] == 'node_mismatch'

    @patch('security.node_integrity.get_public_key_hex',
           return_value='ab' * 32)
    @patch('security.node_integrity.verify_json_signature',
           return_value=False)
    def test_invalid_signature(self, mock_verify, mock_pub):
        cct, _ = self._make_cct()
        result = ContinualLearnerGateService.validate_cct(cct)
        assert not result['valid']
        assert result['reason'] == 'invalid_signature'

    def test_untrusted_issuer(self):
        """CCT from unknown issuer AND not self-issued = rejected."""
        iss = 'ff' * 32
        cct, _ = self._make_cct(iss_pub=iss)
        with patch('security.node_integrity.get_public_key_hex',
                   return_value='00' * 32):
            result = ContinualLearnerGateService.validate_cct(cct)
        assert not result['valid']
        assert result['reason'] == 'untrusted_issuer'

    @patch('security.node_integrity.get_public_key_hex',
           return_value='ab' * 32)
    @patch('security.node_integrity.verify_json_signature',
           return_value=True)
    def test_clock_skew_tolerance(self, mock_verify, mock_pub):
        """CCT expired by 200s but within 300s skew — still valid."""
        cct, _ = self._make_cct(exp_offset=-200)
        result = ContinualLearnerGateService.validate_cct(cct)
        assert result['valid']


# ─── Test: CCT Capability Check ───

class TestAccessMatrix:
    @patch('security.node_integrity.get_public_key_hex',
           return_value='ab' * 32)
    @patch('security.node_integrity.verify_json_signature',
           return_value=True)
    def test_basic_has_temporal_coherence(self, mock_v, mock_p):
        cct, _ = TestCCTValidation()._make_cct(tier='basic')
        assert ContinualLearnerGateService.check_cct_capability(
            cct, 'temporal_coherence')

    @patch('security.node_integrity.get_public_key_hex',
           return_value='ab' * 32)
    @patch('security.node_integrity.verify_json_signature',
           return_value=True)
    def test_basic_lacks_hivemind(self, mock_v, mock_p):
        cct, _ = TestCCTValidation()._make_cct(tier='basic')
        assert not ContinualLearnerGateService.check_cct_capability(
            cct, 'hivemind_query')

    @patch('security.node_integrity.get_public_key_hex',
           return_value='ab' * 32)
    @patch('security.node_integrity.verify_json_signature',
           return_value=True)
    def test_host_has_all_capabilities(self, mock_v, mock_p):
        cct, _ = TestCCTValidation()._make_cct(tier='host')
        for cap in LEARNING_ACCESS_MATRIX['host']:
            assert ContinualLearnerGateService.check_cct_capability(cct, cap), \
                f"host tier should have {cap}"

    def test_invalid_cct_denies_all(self):
        assert not ContinualLearnerGateService.check_cct_capability(
            'invalid', 'temporal_coherence')


# ─── Test: CCT Renewal ───

class TestCCTRenewal:
    @patch('security.node_integrity.sign_json_payload',
           return_value='r' * 128)
    @patch('security.node_integrity.get_public_key_hex',
           return_value='ab' * 32)
    @patch('security.node_integrity.get_node_identity',
           return_value={'node_id': 'renew_issuer'})
    def test_renew_eligible(self, mock_id, mock_pub, mock_sign, db):
        _make_peer(db, 'rn1', 300, 'verified', 'full')
        result = ContinualLearnerGateService.renew_cct(db, 'rn1')
        assert result is not None
        assert result['tier'] == 'full'

    def test_renew_ineligible(self, db):
        _make_peer(db, 'rn2', 5, 'unverified', 'observer')
        result = ContinualLearnerGateService.renew_cct(db, 'rn2')
        assert result is None

    @patch('security.node_integrity.sign_json_payload',
           return_value='s' * 128)
    @patch('security.node_integrity.verify_json_signature',
           return_value=True)
    @patch('security.node_integrity.get_public_key_hex',
           return_value='ab' * 32)
    @patch('security.node_integrity.get_node_identity',
           return_value={'node_id': 'renew_issuer'})
    def test_renew_with_expired_old_cct(self, mock_id, mock_pub, mock_verify,
                                        mock_sign, db):
        """Expired old CCT is acceptable for renewal (grace period)."""
        _make_peer(db, 'rn3', 100, 'verified', 'standard')
        old_cct = TestCCTValidation()._make_cct(
            sub='rn3', exp_offset=-7200)[0]
        result = ContinualLearnerGateService.renew_cct(db, 'rn3', old_cct)
        assert result is not None


# ─── Test: CCT Revocation ───

class TestCCTRevocation:
    @patch('security.node_integrity.sign_json_payload',
           return_value='v' * 128)
    @patch('security.node_integrity.get_public_key_hex',
           return_value='ab' * 32)
    @patch('security.node_integrity.get_node_identity',
           return_value={'node_id': 'rev_issuer'})
    def test_revoke_invalidates_attestations(self, mock_id, mock_pub,
                                              mock_sign, db):
        _make_peer(db, 'rv1', 200, 'verified', 'full')
        ContinualLearnerGateService.issue_cct(db, 'rv1')

        result = ContinualLearnerGateService.revoke_cct(
            db, 'rv1', 'fraud_detected')
        assert result['success']
        assert result['revoked_count'] >= 1

        from integrations.social.models import NodeAttestation
        atts = db.query(NodeAttestation).filter_by(
            subject_node_id='rv1', attestation_type='cct_issued').all()
        for att in atts:
            assert att.is_valid is False


# ─── Test: Tier Stats ───

class TestTierStats:
    def test_stats_empty(self, db):
        stats = ContinualLearnerGateService.get_learning_tier_stats(db)
        assert 'tiers' in stats
        assert stats['total_nodes'] >= 0

    def test_stats_counts_tiers(self, db):
        _make_peer(db, 'st1', 60, 'verified', 'standard')
        _make_peer(db, 'st2', 250, 'verified', 'full')
        _make_peer(db, 'st3', 5, 'unverified', 'observer')
        stats = ContinualLearnerGateService.get_learning_tier_stats(db)
        assert stats['eligible_nodes'] >= 2


# ─── Test: Compute Contribution Verification ───

class TestComputeContribution:
    @patch('security.node_integrity.sign_json_payload',
           return_value='b' * 128)
    @patch('security.node_integrity.get_public_key_hex',
           return_value='ab' * 32)
    @patch('security.node_integrity.get_node_identity',
           return_value={'node_id': 'bench_issuer'})
    def test_verify_valid_benchmark(self, mock_id, mock_pub, mock_sign, db):
        _make_peer(db, 'bm1', 100, 'verified', 'standard')
        result = ContinualLearnerGateService.verify_compute_contribution(
            db, 'bm1', {
                'benchmark_type': 'credit_assignment',
                'score': 42.5,
                'duration_ms': 150.0,
            })
        assert result['verified']
        assert result['score'] == 42.5

    @patch('security.node_integrity.sign_json_payload',
           return_value='b' * 128)
    @patch('security.node_integrity.get_public_key_hex',
           return_value='ab' * 32)
    @patch('security.node_integrity.get_node_identity',
           return_value={'node_id': 'bench_issuer'})
    def test_verify_invalid_score(self, mock_id, mock_pub, mock_sign, db):
        _make_peer(db, 'bm2', 100, 'verified', 'standard')
        result = ContinualLearnerGateService.verify_compute_contribution(
            db, 'bm2', {'score': 0, 'duration_ms': 100})
        assert not result['verified']
        assert result['reason'] == 'invalid_benchmark'


# ─── Test: Learning Tools ───

class TestLearningTools:
    def test_check_learning_health_returns_json(self):
        with patch('integrations.social.models.get_db') as mock_db:
            mock_session = MagicMock()
            mock_db.return_value = mock_session
            mock_session.query.return_value.filter.return_value.all.return_value = []

            from integrations.agent_engine.learning_tools import check_learning_health
            result = json.loads(check_learning_health())
            # Should return learning_health or error
            assert 'learning_health' in result or 'error' in result

    def test_get_learning_tier_stats_returns_json(self):
        with patch('integrations.social.models.get_db') as mock_db:
            mock_session = MagicMock()
            mock_db.return_value = mock_session
            mock_session.query.return_value.filter.return_value.all.return_value = []

            from integrations.agent_engine.learning_tools import get_learning_tier_stats
            result = json.loads(get_learning_tier_stats())
            assert isinstance(result, dict)

    def test_get_node_learning_status_returns_json(self):
        with patch('integrations.social.models.get_db') as mock_db:
            mock_session = MagicMock()
            mock_db.return_value = mock_session
            mock_session.query.return_value.filter_by.return_value.first.return_value = None

            from integrations.agent_engine.learning_tools import get_node_learning_status
            result = json.loads(get_node_learning_status('test_node'))
            assert isinstance(result, dict)

    def test_tools_list_has_six_entries(self):
        from integrations.agent_engine.learning_tools import LEARNING_TOOLS
        assert len(LEARNING_TOOLS) == 6
        for tool in LEARNING_TOOLS:
            assert 'name' in tool
            assert 'func' in tool
            assert 'learning' in tool['tags']


# ─── Test: Learning API ───

class TestLearningAPI:
    @pytest.fixture
    def client(self):
        from flask import Flask
        from integrations.agent_engine.api_learning import learning_bp
        app = Flask(__name__)
        app.register_blueprint(learning_bp)
        app.config['TESTING'] = True
        with app.test_client() as c:
            yield c

    def test_cct_request_missing_node_id(self, client):
        resp = client.post('/api/learning/cct/request',
                          json={})
        assert resp.status_code == 400
        assert b'node_id required' in resp.data

    def test_cct_request_invalid_signature(self, client):
        resp = client.post('/api/learning/cct/request',
                          json={'node_id': 'x', 'signature': 'bad',
                                'public_key': 'bad'})
        assert resp.status_code == 403

    def test_cct_verify_endpoint(self, client):
        resp = client.post('/api/learning/cct/verify',
                          json={'cct': 'invalid_token'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['data']['valid'] is False

    def test_cct_verify_missing_cct(self, client):
        resp = client.post('/api/learning/cct/verify', json={})
        assert resp.status_code == 400

    def test_cct_status_missing_node_id(self, client):
        resp = client.get('/api/learning/cct/status')
        assert resp.status_code == 400

    @patch('integrations.social.models.get_db')
    @patch('integrations.agent_engine.continual_learner_gate.ContinualLearnerGateService')
    def test_tier_stats_endpoint(self, mock_svc, mock_db, client):
        mock_session = MagicMock()
        mock_db.return_value = mock_session
        mock_svc.get_learning_tier_stats.return_value = {
            'tiers': {'none': 5, 'basic': 2, 'full': 1, 'host': 0},
            'total_nodes': 8,
        }
        resp = client.get('/api/learning/tiers')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success']
        assert data['data']['total_nodes'] == 8

    def test_benchmark_missing_node_id(self, client):
        resp = client.post('/api/learning/benchmark', json={})
        assert resp.status_code == 400


# ─── Test: WorldModelBridge Gating ───

class TestWorldModelBridgeGating:
    def test_distribute_blocked_without_cct(self):
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()
        # No CCT file → _check_cct_access returns False
        with patch.object(bridge, '_check_cct_access', return_value=False):
            result = bridge.distribute_skill_packet(
                {'description': 'test skill'}, 'node_1')
        assert not result['success']
        assert result['reason'] == 'no_cct_skill_distribution'

    def test_hivemind_degrades_without_cct(self):
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()
        with patch.object(bridge, '_check_cct_access', return_value=False):
            result = bridge.query_hivemind('test query')
        assert result is None  # No cached thought either

    def test_hivemind_returns_cached_without_cct(self):
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()
        bridge._federation_aggregated['last_thought'] = 'cached wisdom'
        with patch.object(bridge, '_check_cct_access', return_value=False):
            result = bridge.query_hivemind('test query')
        assert result is not None
        assert result['source'] == 'cached'
        assert result['cct_gated'] is True

    def test_check_cct_access_returns_false_no_file(self):
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()
        # No CCT file exists
        assert not bridge._check_cct_access('temporal_coherence')


# ─── Test: Reward Integration ───

class TestRewardIntegration:
    def test_learning_contribution_in_award_table(self):
        from integrations.social.resonance_engine import AWARD_TABLE
        assert 'learning_contribution' in AWARD_TABLE
        assert AWARD_TABLE['learning_contribution']['spark'] == 25
        assert AWARD_TABLE['learning_contribution']['xp'] == 40

    def test_learning_skill_shared_in_award_table(self):
        from integrations.social.resonance_engine import AWARD_TABLE
        assert 'learning_skill_shared' in AWARD_TABLE
        assert AWARD_TABLE['learning_skill_shared']['spark'] == 15

    def test_learning_credit_assigned_in_award_table(self):
        from integrations.social.resonance_engine import AWARD_TABLE
        assert 'learning_credit_assigned' in AWARD_TABLE
        assert AWARD_TABLE['learning_credit_assigned']['spark'] == 5


# ─── Test: Goal Registration ───

class TestGoalRegistration:
    def test_learning_type_registered(self):
        from integrations.agent_engine.goal_manager import (
            get_prompt_builder, get_tool_tags)
        builder = get_prompt_builder('learning')
        assert builder is not None
        tags = get_tool_tags('learning')
        assert 'learning' in tags

    def test_prompt_builder_returns_valid_prompt(self):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('learning')
        prompt = builder({
            'title': 'Test Learning Goal',
            'description': 'Monitor compute contributions',
            'config': {'mode': 'monitor'},
        })
        assert 'CONTINUAL LEARNING COORDINATOR' in prompt
        assert 'Intelligence is earned' in prompt
        assert '90%' in prompt


# ─── Test: Trusted Issuers ───

class TestTrustedIssuers:
    def test_register_and_get(self):
        old = dict(_trusted_issuers)
        try:
            register_trusted_issuer('test_pub_key', 'test_node', 'central')
            issuers = get_trusted_issuers()
            assert 'test_pub_key' in issuers
            assert issuers['test_pub_key']['node_id'] == 'test_node'
        finally:
            _trusted_issuers.clear()
            _trusted_issuers.update(old)


# ─── Test: CCT File Management ───

class TestCCTFileManagement:
    def test_save_and_load(self, tmp_path):
        path = str(tmp_path / 'cct.json')
        ContinualLearnerGateService.save_cct_to_file('test_cct_value', path)
        loaded = ContinualLearnerGateService.load_cct_from_file(path)
        assert loaded == 'test_cct_value'

    def test_load_nonexistent(self, tmp_path):
        path = str(tmp_path / 'nonexistent.json')
        loaded = ContinualLearnerGateService.load_cct_from_file(path)
        assert loaded is None


# ─── Test: Bootstrap Goal Exists ───

class TestBootstrapGoal:
    def test_learning_coordinator_in_seed_goals(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_learning_coordinator' in slugs

    def test_learning_coordinator_goal_config(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        goal = next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_learning_coordinator')
        assert goal['goal_type'] == 'learning'
        assert goal['config']['continuous'] is True
        assert goal['spark_budget'] == 200
