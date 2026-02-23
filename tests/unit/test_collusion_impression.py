"""
Impression Integrity & Collusion Detection Test Suite
======================================================
Tests for:
- detect_witness_ring: bidirectional witness ring detection
- detect_temporal_clustering: burst/bot impression detection
- verify_impression_seal: tamper detection on sealed impressions
- verify_all_sealed_impressions: batch seal verification
- run_full_audit integration with new checks
- FRAUD_WEIGHTS new entries
- Migration v28 (impression seal columns)

All external calls mocked -- in-memory SQLite.
"""
import os
import sys
import uuid
import hashlib
import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from integrations.social.models import (
    Base, User, PeerNode, AdUnit, AdImpression,
    HostingReward, NodeAttestation, FraudAlert,
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


@pytest.fixture
def db(engine, tables):
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.rollback()
    session.close()


def _uid():
    return str(uuid.uuid4())


def _make_peer(db, node_id=None, status='active', fraud_score=0.0):
    nid = node_id or _uid()
    peer = PeerNode(
        node_id=nid, name=f'node-{nid[:8]}', url='http://localhost:9000',
        status=status, integrity_status='verified',
        fraud_score=fraud_score,
    )
    db.add(peer)
    db.flush()
    return peer


def _make_impression(db, ad_id, node_id, witness_node_id=None,
                     witness_signature=None, sealed_hash=None,
                     sealed_at=None, created_at=None):
    imp = AdImpression(
        ad_id=ad_id, node_id=node_id, impression_type='view',
        witness_node_id=witness_node_id,
        witness_signature=witness_signature,
        sealed_hash=sealed_hash,
        sealed_at=sealed_at,
        created_at=created_at or datetime.utcnow(),
    )
    db.add(imp)
    db.flush()
    return imp


def _make_ad(db, advertiser_id=None):
    ad = AdUnit(
        advertiser_id=advertiser_id or _uid(),
        title='Test Ad', ad_type='banner', status='active',
        budget_spark=10000, click_url='https://example.com',
    )
    db.add(ad)
    db.flush()
    return ad


# =====================================================================
# FRAUD_WEIGHTS
# =====================================================================

class TestFraudWeights:
    """Verify new FRAUD_WEIGHTS entries exist."""

    def test_witness_ring_weight(self):
        from integrations.social.integrity_service import FRAUD_WEIGHTS
        assert 'witness_ring' in FRAUD_WEIGHTS
        assert FRAUD_WEIGHTS['witness_ring'] == 30.0

    def test_temporal_clustering_weight(self):
        from integrations.social.integrity_service import FRAUD_WEIGHTS
        assert 'temporal_clustering' in FRAUD_WEIGHTS
        assert FRAUD_WEIGHTS['temporal_clustering'] == 20.0

    def test_seal_tamper_weight(self):
        from integrations.social.integrity_service import FRAUD_WEIGHTS
        assert 'seal_tamper' in FRAUD_WEIGHTS
        assert FRAUD_WEIGHTS['seal_tamper'] == 35.0


# =====================================================================
# detect_witness_ring
# =====================================================================

class TestDetectWitnessRing:
    """Test bidirectional witness ring detection."""

    def test_no_impressions_returns_none(self, db):
        from integrations.social.integrity_service import IntegrityService
        peer = _make_peer(db)
        result = IntegrityService.detect_witness_ring(db, peer.node_id)
        assert result is None

    def test_below_min_impressions_returns_none(self, db):
        from integrations.social.integrity_service import IntegrityService
        ad = _make_ad(db)
        peer = _make_peer(db)
        witness = _make_peer(db)
        # Only 5 impressions (below default min_impressions=10)
        for _ in range(5):
            _make_impression(db, ad.id, peer.node_id,
                           witness_node_id=witness.node_id)
        result = IntegrityService.detect_witness_ring(
            db, peer.node_id, min_impressions=10)
        assert result is None

    def test_diverse_witnesses_returns_none(self, db):
        """Many unique witnesses = no ring."""
        from integrations.social.integrity_service import IntegrityService
        ad = _make_ad(db)
        peer = _make_peer(db)
        # 15 impressions, each with a different witness
        for _ in range(15):
            w = _make_peer(db)
            _make_impression(db, ad.id, peer.node_id,
                           witness_node_id=w.node_id)
        result = IntegrityService.detect_witness_ring(
            db, peer.node_id, min_impressions=10)
        assert result is None

    def test_ring_detected_bidirectional(self, db):
        """A→B witnesses and B→A witnesses with tight set → ring."""
        from integrations.social.integrity_service import IntegrityService
        ad = _make_ad(db)
        node_a = _make_peer(db)
        node_b = _make_peer(db)

        now = datetime.utcnow()
        # A's impressions witnessed by B (15 times)
        for i in range(15):
            _make_impression(db, ad.id, node_a.node_id,
                           witness_node_id=node_b.node_id,
                           created_at=now - timedelta(hours=i))
        # B's impressions witnessed by A (10 times) - bidirectional
        for i in range(10):
            _make_impression(db, ad.id, node_b.node_id,
                           witness_node_id=node_a.node_id,
                           created_at=now - timedelta(hours=i))
        db.flush()

        result = IntegrityService.detect_witness_ring(
            db, node_a.node_id, min_impressions=10, max_witnesses=2)
        assert result is not None
        assert result['alert_type'] == 'witness_ring'

    def test_one_way_only_no_ring(self, db):
        """A→B witnessed but B not witnessed by A → no bidirectional ring."""
        from integrations.social.integrity_service import IntegrityService
        ad = _make_ad(db)
        node_a = _make_peer(db)
        node_b = _make_peer(db)

        # Only A→B, no B→A
        for _ in range(15):
            _make_impression(db, ad.id, node_a.node_id,
                           witness_node_id=node_b.node_id)
        db.flush()

        # B has no impressions witnessed by A, so reverse check fails
        result = IntegrityService.detect_witness_ring(
            db, node_a.node_id, min_impressions=10, max_witnesses=2)
        # Should be None because no bidirectional ring
        assert result is None


# =====================================================================
# detect_temporal_clustering
# =====================================================================

class TestDetectTemporalClustering:
    """Test burst/bot impression detection."""

    def test_no_impressions_returns_none(self, db):
        from integrations.social.integrity_service import IntegrityService
        peer = _make_peer(db)
        result = IntegrityService.detect_temporal_clustering(db, peer.node_id)
        assert result is None

    def test_below_min_impressions_returns_none(self, db):
        from integrations.social.integrity_service import IntegrityService
        ad = _make_ad(db)
        peer = _make_peer(db)
        for _ in range(5):
            _make_impression(db, ad.id, peer.node_id)
        result = IntegrityService.detect_temporal_clustering(
            db, peer.node_id, min_impressions=20)
        assert result is None

    def test_organic_traffic_no_alert(self, db):
        """Spread-out impressions should not trigger."""
        from integrations.social.integrity_service import IntegrityService
        ad = _make_ad(db)
        peer = _make_peer(db)
        now = datetime.utcnow()
        # 25 impressions spaced 2 minutes apart
        for i in range(25):
            _make_impression(db, ad.id, peer.node_id,
                           created_at=now - timedelta(minutes=i * 2))
        result = IntegrityService.detect_temporal_clustering(
            db, peer.node_id, period_hours=2, min_impressions=20,
            cluster_window_seconds=5, min_cluster_size=10)
        assert result is None

    def test_burst_detected(self, db):
        """15 impressions in 3 seconds → temporal clustering alert."""
        from integrations.social.integrity_service import IntegrityService
        ad = _make_ad(db)
        peer = _make_peer(db)
        now = datetime.utcnow()
        # 15 impressions in a 3-second burst
        for i in range(15):
            _make_impression(db, ad.id, peer.node_id,
                           created_at=now - timedelta(milliseconds=i * 200))
        # Plus some organic impressions to meet min_impressions
        for i in range(10):
            _make_impression(db, ad.id, peer.node_id,
                           created_at=now - timedelta(minutes=i + 5))
        db.flush()

        result = IntegrityService.detect_temporal_clustering(
            db, peer.node_id, period_hours=1, min_impressions=20,
            cluster_window_seconds=5, min_cluster_size=10)
        assert result is not None
        assert result['alert_type'] == 'temporal_clustering'


# =====================================================================
# verify_impression_seal
# =====================================================================

class TestVerifyImpressionSeal:
    """Test tamper detection on sealed impressions."""

    def test_impression_not_found(self, db):
        from integrations.social.integrity_service import IntegrityService
        result = IntegrityService.verify_impression_seal(db, 'nonexistent')
        assert result['valid'] is False
        assert 'not found' in result['details'].lower()

    def test_unsealed_impression_valid(self, db):
        """Unsealed impressions pass (nothing to verify)."""
        from integrations.social.integrity_service import IntegrityService
        ad = _make_ad(db)
        peer = _make_peer(db)
        imp = _make_impression(db, ad.id, peer.node_id)
        result = IntegrityService.verify_impression_seal(db, imp.id)
        assert result['valid'] is True
        assert 'not sealed' in result['details'].lower()

    def test_intact_seal_valid(self, db):
        """Properly sealed impression passes verification."""
        from integrations.social.integrity_service import IntegrityService
        ad = _make_ad(db)
        peer = _make_peer(db)
        witness = _make_peer(db)
        imp = _make_impression(db, ad.id, peer.node_id,
                              witness_node_id=witness.node_id,
                              witness_signature='sig123')
        # Seal it
        imp.sealed_hash = imp.compute_seal_hash
        imp.sealed_at = datetime.utcnow()
        db.flush()

        result = IntegrityService.verify_impression_seal(db, imp.id)
        assert result['valid'] is True
        assert 'intact' in result['details'].lower()

    def test_tampered_seal_detected(self, db):
        """Modifying data after sealing should fail verification."""
        from integrations.social.integrity_service import IntegrityService
        ad = _make_ad(db)
        peer = _make_peer(db)
        witness = _make_peer(db)
        imp = _make_impression(db, ad.id, peer.node_id,
                              witness_node_id=witness.node_id,
                              witness_signature='sig456')
        # Seal it
        imp.sealed_hash = imp.compute_seal_hash
        imp.sealed_at = datetime.utcnow()
        db.flush()

        # Tamper: change the impression type after sealing
        imp.impression_type = 'click'
        db.flush()

        result = IntegrityService.verify_impression_seal(db, imp.id)
        assert result['valid'] is False
        assert 'tampered' in result['details'].lower()

    def test_tampered_seal_creates_fraud_alert(self, db):
        """Tampered seal should increase fraud score on the node."""
        from integrations.social.integrity_service import IntegrityService
        ad = _make_ad(db)
        peer = _make_peer(db)
        witness = _make_peer(db)
        imp = _make_impression(db, ad.id, peer.node_id,
                              witness_node_id=witness.node_id,
                              witness_signature='sig789')
        imp.sealed_hash = imp.compute_seal_hash
        imp.sealed_at = datetime.utcnow()
        db.flush()

        old_score = peer.fraud_score or 0

        # Tamper
        imp.impression_type = 'click'
        db.flush()

        IntegrityService.verify_impression_seal(db, imp.id)
        db.refresh(peer)
        assert (peer.fraud_score or 0) > old_score


# =====================================================================
# verify_all_sealed_impressions
# =====================================================================

class TestVerifyAllSealedImpressions:
    """Test batch seal verification."""

    def test_empty_returns_zero(self, db):
        from integrations.social.integrity_service import IntegrityService
        peer = _make_peer(db)
        result = IntegrityService.verify_all_sealed_impressions(
            db, peer.node_id)
        assert result['total_checked'] == 0
        assert result['tampered'] == 0
        assert result['integrity_ratio'] == 1.0

    def test_all_intact(self, db):
        from integrations.social.integrity_service import IntegrityService
        ad = _make_ad(db)
        peer = _make_peer(db)
        witness = _make_peer(db)
        for _ in range(5):
            imp = _make_impression(db, ad.id, peer.node_id,
                                  witness_node_id=witness.node_id)
            imp.sealed_hash = imp.compute_seal_hash
            imp.sealed_at = datetime.utcnow()
        db.flush()

        result = IntegrityService.verify_all_sealed_impressions(
            db, peer.node_id)
        assert result['total_checked'] == 5
        assert result['tampered'] == 0
        assert result['integrity_ratio'] == 1.0

    def test_some_tampered(self, db):
        from integrations.social.integrity_service import IntegrityService
        ad = _make_ad(db)
        peer = _make_peer(db)
        witness = _make_peer(db)

        intact = []
        tampered = []
        for i in range(4):
            imp = _make_impression(db, ad.id, peer.node_id,
                                  witness_node_id=witness.node_id)
            imp.sealed_hash = imp.compute_seal_hash
            imp.sealed_at = datetime.utcnow()
            if i < 2:
                intact.append(imp)
            else:
                tampered.append(imp)
        db.flush()

        # Tamper with 2 of them
        for imp in tampered:
            imp.impression_type = 'click'
        db.flush()

        result = IntegrityService.verify_all_sealed_impressions(
            db, peer.node_id, limit=10)
        assert result['total_checked'] == 4
        assert result['tampered'] == 2
        assert result['integrity_ratio'] == 0.5


# =====================================================================
# compute_seal_hash property
# =====================================================================

class TestComputeSealHash:
    """Test the AdImpression.compute_seal_hash property."""

    def test_deterministic(self, db):
        """Same data → same hash."""
        ad = _make_ad(db)
        peer = _make_peer(db)
        imp = _make_impression(db, ad.id, peer.node_id,
                              witness_node_id='w1')
        h1 = imp.compute_seal_hash
        h2 = imp.compute_seal_hash
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_changes_with_data(self, db):
        """Different witness → different hash."""
        ad = _make_ad(db)
        peer = _make_peer(db)
        imp = _make_impression(db, ad.id, peer.node_id,
                              witness_node_id='w1')
        h1 = imp.compute_seal_hash
        imp.witness_node_id = 'w2'
        h2 = imp.compute_seal_hash
        assert h1 != h2


# =====================================================================
# AdImpression to_dict includes new columns
# =====================================================================

class TestImpressionToDict:
    """Verify to_dict includes seal columns."""

    def test_to_dict_has_seal_fields(self, db):
        ad = _make_ad(db)
        peer = _make_peer(db)
        imp = _make_impression(db, ad.id, peer.node_id,
                              witness_node_id='w1',
                              witness_signature='sig',
                              sealed_hash='abc123',
                              sealed_at=datetime.utcnow())
        d = imp.to_dict()
        assert 'witness_node_id' in d
        assert 'witness_signature' in d
        assert 'sealed_hash' in d
        assert 'sealed_at' in d
        assert d['witness_node_id'] == 'w1'
        assert d['witness_signature'] == 'sig'
        assert d['sealed_hash'] == 'abc123'


# =====================================================================
# run_full_audit integration
# =====================================================================

class TestRunFullAuditIntegration:
    """Verify run_full_audit includes new impression checks."""

    @patch('integrations.social.integrity_service.IntegrityService.verify_code_hash',
           return_value={'verified': True})
    @patch('integrations.social.integrity_service.IntegrityService.verify_audit_dominance',
           return_value={'dominant': True})
    def test_full_audit_includes_witness_ring(self, mock_dom, mock_hash, db):
        from integrations.social.integrity_service import IntegrityService
        peer = _make_peer(db)
        results = IntegrityService.run_full_audit(db, peer.node_id)
        assert 'witness_ring' in results
        assert 'temporal_clustering' in results
        assert 'seal_integrity' in results

    @patch('integrations.social.integrity_service.IntegrityService.verify_code_hash',
           return_value={'verified': True})
    @patch('integrations.social.integrity_service.IntegrityService.verify_audit_dominance',
           return_value={'dominant': True})
    def test_seal_integrity_in_audit_result(self, mock_dom, mock_hash, db):
        from integrations.social.integrity_service import IntegrityService
        peer = _make_peer(db)
        results = IntegrityService.run_full_audit(db, peer.node_id)
        seal = results['seal_integrity']
        assert 'total_checked' in seal
        assert 'tampered' in seal


# =====================================================================
# Migration v28
# =====================================================================

class TestMigrationV28:
    """Verify SCHEMA_VERSION bumped to 28."""

    def test_schema_version_28(self):
        from integrations.social.migrations import SCHEMA_VERSION
        assert SCHEMA_VERSION >= 28
