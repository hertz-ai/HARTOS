"""Tests for RegionalHostService - hybrid approval flow."""
import json
import os
import sys

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

# Use in-memory SQLite for tests
os.environ['SOCIAL_DB_PATH'] = ':memory:'

from integrations.social.models import Base, get_engine, get_db, RegionalHostRequest
from integrations.social.regional_host_service import RegionalHostService


@pytest.fixture(scope='module')
def engine():
    """Create all tables once per module."""
    eng = get_engine()
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db(engine):
    """Function-scoped DB session with rollback."""
    session = get_db()
    yield session
    session.rollback()
    session.close()


class TestRequestRegionalHost:
    """Test request_regional_host()."""

    @patch('integrations.social.rating_service.RatingService.get_trust_score')
    @patch('security.system_requirements.detect_hardware')
    @patch('security.system_requirements.classify_tier')
    def test_qualifies_with_good_compute_and_trust(
        self, mock_tier, mock_hw, mock_trust, db,
    ):
        mock_hw.return_value = {'cpu_cores': 8, 'ram_gb': 32}
        mock_tier.return_value = 'COMPUTE_HOST'
        mock_trust.return_value = {'composite_trust': 4.0}

        result = RegionalHostService.request_regional_host(
            db,
            user_id='user_good',
            compute_info={'cpu_cores': 8, 'ram_gb': 32},
            github_username='gooduser',
        )

        assert result['qualified'] is True
        assert result['status'] == 'pending_steward'
        assert result['request_id']

    @patch('integrations.social.rating_service.RatingService.get_trust_score')
    @patch('security.system_requirements.detect_hardware')
    @patch('security.system_requirements.classify_tier')
    def test_rejected_low_compute(
        self, mock_tier, mock_hw, mock_trust, db,
    ):
        mock_hw.return_value = {'cpu_cores': 1, 'ram_gb': 2}
        mock_tier.return_value = 'OBSERVER'
        mock_trust.return_value = {'composite_trust': 4.0}

        result = RegionalHostService.request_regional_host(
            db,
            user_id='user_low_compute',
            compute_info={'cpu_cores': 1, 'ram_gb': 2},
        )

        assert result['qualified'] is False
        assert result['status'] == 'rejected'
        assert 'Compute tier' in result['reason']

    @patch('integrations.social.rating_service.RatingService.get_trust_score')
    @patch('security.system_requirements.detect_hardware')
    @patch('security.system_requirements.classify_tier')
    def test_rejected_low_trust(
        self, mock_tier, mock_hw, mock_trust, db,
    ):
        mock_hw.return_value = {'cpu_cores': 8, 'ram_gb': 32}
        mock_tier.return_value = 'STANDARD'
        mock_trust.return_value = {'composite_trust': 1.0}

        result = RegionalHostService.request_regional_host(
            db,
            user_id='user_low_trust',
            compute_info={'cpu_cores': 8, 'ram_gb': 32},
        )

        assert result['qualified'] is False
        assert 'Trust score' in result['reason']

    @patch('integrations.social.rating_service.RatingService.get_trust_score')
    @patch('security.system_requirements.detect_hardware')
    @patch('security.system_requirements.classify_tier')
    def test_duplicate_request_returns_existing(
        self, mock_tier, mock_hw, mock_trust, db,
    ):
        mock_hw.return_value = {'cpu_cores': 8, 'ram_gb': 32}
        mock_tier.return_value = 'STANDARD'
        mock_trust.return_value = {'composite_trust': 3.0}

        r1 = RegionalHostService.request_regional_host(
            db, user_id='user_dup', compute_info={})
        r2 = RegionalHostService.request_regional_host(
            db, user_id='user_dup', compute_info={})

        assert r1['request_id'] == r2['request_id']
        assert r2['reason'] == 'Request already exists'


class TestApproveRequest:
    """Test approve_request()."""

    def _create_pending_request(self, db, user_id='user_approve'):
        """Helper to create a pending_steward request."""
        req = RegionalHostRequest(
            user_id=user_id,
            node_id='node_123',
            public_key_hex='ab' * 16,
            compute_tier='STANDARD',
            trust_score=3.5,
            status='pending_steward',
            github_username='testghuser',
        )
        db.add(req)
        db.flush()
        return req.id

    @patch('integrations.agent_engine.private_repo_access.'
           'PrivateRepoAccessService.send_github_invite')
    @patch('security.key_delegation.create_child_certificate')
    @patch('security.node_integrity.get_node_identity')
    @patch('integrations.social.hierarchy_service.HierarchyService.'
           'register_regional_host')
    def test_approve_issues_cert_and_invites(
        self, mock_hierarchy, mock_identity, mock_cert, mock_invite, db,
    ):
        request_id = self._create_pending_request(db)
        # Provide a fake Ed25519 private key for certificate signing
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        fake_key = Ed25519PrivateKey.generate()
        mock_identity.return_value = {
            'node_id': 'test-central',
            '_private_key': fake_key,
        }
        mock_cert.return_value = {'tier': 'regional', 'region': 'us-east'}
        mock_invite.return_value = {'invited': True}

        result = RegionalHostService.approve_request(
            db,
            request_id=request_id,
            steward_node_id='steward_1',
            region_name='us-east',
        )

        assert result['approved'] is True
        assert result['region_name'] == 'us-east'
        mock_cert.assert_called_once()

    def test_approve_nonexistent_request(self, db):
        result = RegionalHostService.approve_request(
            db, request_id='fake', steward_node_id='s', region_name='r')
        assert result['approved'] is False
        assert 'not found' in result['error']

    def test_cannot_approve_rejected(self, db):
        req = RegionalHostRequest(
            user_id='user_rej',
            status='rejected',
            trust_score=0,
        )
        db.add(req)
        db.flush()

        result = RegionalHostService.approve_request(
            db, request_id=req.id, steward_node_id='s', region_name='r')
        assert result['approved'] is False
        assert 'Cannot approve' in result['error']


class TestRejectAndRevoke:
    """Test reject and revoke flows."""

    def test_reject_request(self, db):
        req = RegionalHostRequest(
            user_id='user_reject',
            status='pending_steward',
            trust_score=3.0,
        )
        db.add(req)
        db.flush()

        result = RegionalHostService.reject_request(
            db, request_id=req.id, reason='Insufficient capacity')
        assert result['rejected'] is True

        # Verify status changed
        refreshed = db.query(RegionalHostRequest).get(req.id)
        assert refreshed.status == 'rejected'
        assert 'Insufficient' in refreshed.rejected_reason

    @patch('integrations.agent_engine.private_repo_access.'
           'PrivateRepoAccessService.revoke_github_access')
    def test_revoke_regional_host(self, mock_revoke, db):
        req = RegionalHostRequest(
            user_id='user_revoke',
            status='approved',
            trust_score=3.0,
            github_username='testuser',
            github_invite_sent=True,
        )
        db.add(req)
        db.flush()

        result = RegionalHostService.revoke_regional_host(
            db, request_id=req.id)
        assert result['revoked'] is True

        refreshed = db.query(RegionalHostRequest).get(req.id)
        assert refreshed.status == 'revoked'
        assert refreshed.github_invite_sent is False


class TestListAndStatus:
    """Test listing and status endpoints."""

    def test_list_pending_requests(self, db):
        for i in range(3):
            db.add(RegionalHostRequest(
                user_id=f'user_list_{i}',
                status='pending_steward' if i < 2 else 'approved',
                trust_score=3.0,
            ))
        db.flush()

        pending = RegionalHostService.list_pending_requests(db)
        assert len(pending) >= 2
        for p in pending:
            assert p['status'] in ('pending', 'pending_steward')

    def test_get_request_status(self, db):
        db.add(RegionalHostRequest(
            user_id='user_status',
            status='pending_steward',
            compute_tier='STANDARD',
            trust_score=3.0,
        ))
        db.flush()

        result = RegionalHostService.get_request_status(db, 'user_status')
        assert result is not None
        assert result['status'] == 'pending_steward'

    def test_get_request_status_no_request(self, db):
        result = RegionalHostService.get_request_status(db, 'nonexistent')
        assert result is None
