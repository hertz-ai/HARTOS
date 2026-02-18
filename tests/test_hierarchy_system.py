"""
3-Tier Hierarchy System Test Suite
====================================
Tests covering:
- Key delegation: certificate creation, verification, chain validation, expiry, hybrid fallback
- Tier authorization: central requires master key, regional requires cert, local/flat always OK
- PeerNode hierarchy columns, Region hierarchy columns
- RegionAssignment and SyncQueue models
- HierarchyService: registration, auto-assignment scoring, region switch, gossip targets
- Tier-aware gossip in peer_discovery
- SyncEngine: queue/drain, retry logic, batch receive
- Migration v13
- API endpoints: central-only gating, tier-info

All external calls mocked -- in-memory SQLite.
"""
import os
import sys
import uuid
import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

# Add parent dir for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Force in-memory SQLite before importing models
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from integrations.social.models import (
    Base, PeerNode, Region, RegionAssignment, SyncQueue,
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


@pytest.fixture
def master_keypair():
    """Generate a fresh Ed25519 keypair for testing."""
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        'private_key': priv,
        'public_key': pub,
        'private_hex': priv_bytes.hex(),
        'public_hex': pub_bytes.hex(),
    }


@pytest.fixture
def child_keypair():
    """Generate a child Ed25519 keypair for testing."""
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        'private_key': priv,
        'public_key': pub,
        'public_hex': pub_bytes.hex(),
    }


# =====================================================================
# TEST CLASS 1: Key Delegation - Certificate Creation & Verification
# =====================================================================

class TestKeyDelegation:
    """Certificate chain creation and verification."""

    def test_create_child_certificate(self, master_keypair, child_keypair):
        from security.key_delegation import create_child_certificate
        cert = create_child_certificate(
            parent_private_key=master_keypair['private_key'],
            child_public_key_hex=child_keypair['public_hex'],
            node_id='test-regional-1',
            tier='regional',
            region_name='us-east-1',
        )
        assert cert['node_id'] == 'test-regional-1'
        assert cert['public_key'] == child_keypair['public_hex']
        assert cert['tier'] == 'regional'
        assert cert['region_name'] == 'us-east-1'
        assert cert['parent_public_key'] == master_keypair['public_hex']
        assert 'parent_signature' in cert
        assert 'issued_at' in cert
        assert 'expires_at' in cert

    def test_verify_certificate_signature_valid(self, master_keypair, child_keypair):
        from security.key_delegation import create_child_certificate, verify_certificate_signature
        cert = create_child_certificate(
            parent_private_key=master_keypair['private_key'],
            child_public_key_hex=child_keypair['public_hex'],
            node_id='test-node',
            tier='regional',
            region_name='us-east-1',
        )
        assert verify_certificate_signature(cert) is True

    def test_verify_certificate_signature_tampered(self, master_keypair, child_keypair):
        from security.key_delegation import create_child_certificate, verify_certificate_signature
        cert = create_child_certificate(
            parent_private_key=master_keypair['private_key'],
            child_public_key_hex=child_keypair['public_hex'],
            node_id='test-node',
            tier='regional',
            region_name='us-east-1',
        )
        cert['region_name'] = 'tampered-region'
        assert verify_certificate_signature(cert) is False

    def test_verify_certificate_chain_valid(self, master_keypair, child_keypair):
        from security.key_delegation import create_child_certificate, verify_certificate_chain
        cert = create_child_certificate(
            parent_private_key=master_keypair['private_key'],
            child_public_key_hex=child_keypair['public_hex'],
            node_id='test-node',
            tier='regional',
            region_name='us-east-1',
        )
        with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
            result = verify_certificate_chain(cert)
        assert result['valid'] is True
        assert result['path'] == 'chain'
        assert 'master key' in result['details']

    def test_verify_certificate_chain_wrong_master(self, master_keypair, child_keypair):
        """Certificate signed by a key that is NOT the master key."""
        from security.key_delegation import create_child_certificate, verify_certificate_chain
        # Sign with the master key, but verify against a different master
        cert = create_child_certificate(
            parent_private_key=master_keypair['private_key'],
            child_public_key_hex=child_keypair['public_hex'],
            node_id='test-node',
            tier='regional',
            region_name='us-east-1',
        )
        fake_master = Ed25519PrivateKey.generate().public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()
        with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', fake_master):
            result = verify_certificate_chain(cert)
        assert result['valid'] is False
        assert result['path'] == 'none'

    def test_certificate_expiry(self, master_keypair, child_keypair):
        from security.key_delegation import create_child_certificate, verify_certificate_chain
        cert = create_child_certificate(
            parent_private_key=master_keypair['private_key'],
            child_public_key_hex=child_keypair['public_hex'],
            node_id='test-node',
            tier='regional',
            region_name='us-east-1',
            validity_days=0,  # Expires immediately
        )
        # Set expires_at to the past
        cert_copy = dict(cert)
        del cert_copy['parent_signature']
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        cert_copy['expires_at'] = past
        # Re-sign
        canonical = json.dumps(cert_copy, sort_keys=True, separators=(',', ':'))
        sig = master_keypair['private_key'].sign(canonical.encode('utf-8'))
        cert_copy['parent_signature'] = sig.hex()

        with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
            result = verify_certificate_chain(cert_copy)
        assert result['valid'] is False
        assert 'expired' in result['details'].lower()

    def test_hybrid_registry_fallback(self, master_keypair, child_keypair):
        """Certificate chain fails, but registry lookup succeeds."""
        from security.key_delegation import verify_certificate_chain
        cert = {
            'node_id': 'test-node',
            'public_key': child_keypair['public_hex'],
            'tier': 'regional',
            'parent_public_key': '',
            'parent_signature': '',
        }
        trusted_keys = {'test-node': child_keypair['public_hex']}
        result = verify_certificate_chain(cert, trusted_keys=trusted_keys)
        assert result['valid'] is True
        assert result['path'] == 'registry'

    def test_custom_capabilities(self, master_keypair, child_keypair):
        from security.key_delegation import create_child_certificate
        cert = create_child_certificate(
            parent_private_key=master_keypair['private_key'],
            child_public_key_hex=child_keypair['public_hex'],
            node_id='test-node',
            tier='regional',
            region_name='us-east-1',
            capabilities=['registry', 'gossip_hub', 'agent_host', 'sync_hub'],
        )
        assert 'sync_hub' in cert['capabilities']
        assert len(cert['capabilities']) == 4


# =====================================================================
# TEST CLASS 2: Tier Authorization
# =====================================================================

class TestTierAuthorization:
    """Verify that only properly credentialed nodes can claim central/regional."""

    def test_flat_always_authorized(self):
        from security.key_delegation import verify_tier_authorization
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'flat'}):
            result = verify_tier_authorization()
        assert result['authorized'] is True
        assert result['tier'] == 'flat'

    def test_local_always_authorized(self):
        from security.key_delegation import verify_tier_authorization
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'local'}):
            result = verify_tier_authorization()
        assert result['authorized'] is True
        assert result['tier'] == 'local'

    def test_central_without_key_rejected(self):
        from security.key_delegation import verify_tier_authorization
        env = {'HEVOLVE_NODE_TIER': 'central'}
        if 'HEVOLVE_MASTER_PRIVATE_KEY' in os.environ:
            env['HEVOLVE_MASTER_PRIVATE_KEY'] = ''
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop('HEVOLVE_MASTER_PRIVATE_KEY', None)
            result = verify_tier_authorization()
        assert result['authorized'] is False
        assert 'HEVOLVE_MASTER_PRIVATE_KEY' in result['details']

    def test_central_with_valid_key(self, master_keypair):
        from security.key_delegation import verify_tier_authorization
        with patch.dict(os.environ, {
            'HEVOLVE_NODE_TIER': 'central',
            'HEVOLVE_MASTER_PRIVATE_KEY': master_keypair['private_hex'],
        }):
            with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
                result = verify_tier_authorization()
        assert result['authorized'] is True
        assert result['tier'] == 'central'

    def test_central_with_wrong_key(self):
        """Private key doesn't match hardcoded public key."""
        from security.key_delegation import verify_tier_authorization
        wrong_key = Ed25519PrivateKey.generate()
        wrong_hex = wrong_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ).hex()
        with patch.dict(os.environ, {
            'HEVOLVE_NODE_TIER': 'central',
            'HEVOLVE_MASTER_PRIVATE_KEY': wrong_hex,
        }):
            result = verify_tier_authorization()
        assert result['authorized'] is False
        assert 'does not match' in result['details']

    def test_regional_without_cert_rejected(self):
        from security.key_delegation import verify_tier_authorization
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'regional'}):
            with patch('security.key_delegation.load_node_certificate', return_value=None):
                result = verify_tier_authorization()
        assert result['authorized'] is False
        assert 'certificate' in result['details'].lower()

    def test_regional_with_valid_cert(self, master_keypair, child_keypair):
        from security.key_delegation import (
            create_child_certificate, verify_tier_authorization,
        )
        cert = create_child_certificate(
            parent_private_key=master_keypair['private_key'],
            child_public_key_hex=child_keypair['public_hex'],
            node_id='test-regional',
            tier='regional',
            region_name='us-east-1',
        )
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'regional'}):
            with patch('security.key_delegation.load_node_certificate', return_value=cert):
                with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
                    result = verify_tier_authorization()
        assert result['authorized'] is True


# =====================================================================
# TEST CLASS 3: Master Key sign_child_certificate
# =====================================================================

class TestMasterKeySignChild:
    """Test sign_child_certificate in master_key.py."""

    def test_sign_child_certificate_no_key(self):
        from security.master_key import sign_child_certificate
        env = dict(os.environ)
        env.pop('HEVOLVE_MASTER_PRIVATE_KEY', None)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match='HEVOLVE_MASTER_PRIVATE_KEY'):
                sign_child_certificate({'test': 'payload'})

    def test_sign_child_certificate_roundtrip(self, master_keypair):
        from security.master_key import sign_child_certificate
        payload = {'node_id': 'test', 'tier': 'regional'}
        with patch.dict(os.environ, {
            'HEVOLVE_MASTER_PRIVATE_KEY': master_keypair['private_hex'],
        }):
            sig_hex = sign_child_certificate(payload)
        # Verify signature manually
        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        master_keypair['public_key'].verify(
            bytes.fromhex(sig_hex),
            canonical.encode('utf-8'),
        )


# =====================================================================
# TEST CLASS 4: Node Identity
# =====================================================================

class TestNodeIdentity:

    def test_get_node_identity(self):
        from security.node_integrity import get_node_identity, reset_keypair
        reset_keypair()
        with patch('security.key_delegation.get_node_tier', return_value='flat'):
            with patch('security.key_delegation.load_node_certificate', return_value=None):
                identity = get_node_identity()
        assert 'node_id' in identity
        assert 'public_key' in identity
        assert identity['tier'] == 'flat'
        assert identity['certificate'] is None
        assert 'code_hash' in identity


# =====================================================================
# TEST CLASS 5: PeerNode Hierarchy Columns
# =====================================================================

class TestPeerNodeTierColumns:
    """Test new hierarchy columns on PeerNode model."""

    def test_peer_node_tier_default(self, db):
        peer = PeerNode(
            node_id=f'tier-test-{uuid.uuid4().hex[:8]}',
            url='http://localhost:9999',
        )
        db.add(peer)
        db.flush()
        # SQLAlchemy Column default is 'flat'
        assert peer.tier == 'flat' or peer.tier is None  # None before commit in some backends

    def test_peer_node_hierarchy_fields(self, db):
        peer = PeerNode(
            node_id=f'hier-{uuid.uuid4().hex[:8]}',
            url='http://regional.test:8000',
            tier='regional',
            parent_node_id='central-node-123',
            certificate_json={'tier': 'regional', 'region_name': 'us-east-1'},
            certificate_verified=True,
            compute_cpu_cores=8,
            compute_ram_gb=32.0,
            compute_gpu_count=2,
            active_user_count=50,
            max_user_capacity=200,
            dns_region='us-east-1',
        )
        db.add(peer)
        db.flush()
        assert peer.tier == 'regional'
        assert peer.parent_node_id == 'central-node-123'
        assert peer.certificate_verified is True
        assert peer.compute_cpu_cores == 8
        assert peer.compute_ram_gb == 32.0
        assert peer.dns_region == 'us-east-1'

    def test_peer_node_to_dict_includes_hierarchy(self, db):
        peer = PeerNode(
            node_id=f'dict-{uuid.uuid4().hex[:8]}',
            url='http://test:8000',
            tier='local',
            dns_region='eu-west-1',
        )
        db.add(peer)
        db.flush()
        d = peer.to_dict()
        assert 'tier' in d
        assert 'dns_region' in d
        assert 'certificate_verified' in d
        assert 'compute_cpu_cores' in d

    def test_region_hierarchy_fields(self, db):
        region = Region(
            name=f'test-region-{uuid.uuid4().hex[:6]}',
            host_node_id='regional-host-1',
            capacity_cpu=16,
            capacity_ram_gb=64.0,
            capacity_gpu=4,
            current_load_pct=35.0,
            is_accepting_nodes=True,
            central_approved=True,
        )
        db.add(region)
        db.flush()
        d = region.to_dict()
        assert d['host_node_id'] == 'regional-host-1'
        assert d['capacity_cpu'] == 16
        assert d['is_accepting_nodes'] is True
        assert d['central_approved'] is True

    def test_region_assignment_model(self, db):
        assignment = RegionAssignment(
            local_node_id='local-123',
            regional_node_id='regional-456',
            assigned_by='central_auto',
            status='active',
            approved_by_central=True,
            compute_snapshot={'cpu_cores': 4, 'ram_gb': 8},
        )
        db.add(assignment)
        db.flush()
        d = assignment.to_dict()
        assert d['local_node_id'] == 'local-123'
        assert d['regional_node_id'] == 'regional-456'
        assert d['status'] == 'active'
        assert d['approved_by_central'] is True

    def test_sync_queue_model(self, db):
        item = SyncQueue(
            node_id='node-abc',
            target_tier='central',
            operation_type='sync_post',
            payload_json={'post_id': '123', 'title': 'Test'},
            status='queued',
        )
        db.add(item)
        db.flush()
        d = item.to_dict()
        assert d['node_id'] == 'node-abc'
        assert d['target_tier'] == 'central'
        assert d['operation_type'] == 'sync_post'
        assert d['status'] == 'queued'


# =====================================================================
# TEST CLASS 6: Hierarchy Service
# =====================================================================

class TestHierarchyService:
    """Test registration, auto-assignment, region switch, gossip targets."""

    def test_register_regional_host(self, db, master_keypair, child_keypair):
        from integrations.social.hierarchy_service import HierarchyService
        from security.key_delegation import create_child_certificate

        cert = create_child_certificate(
            parent_private_key=master_keypair['private_key'],
            child_public_key_hex=child_keypair['public_hex'],
            node_id='regional-host-1',
            tier='regional',
            region_name='us-east-1',
        )
        with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
            result = HierarchyService.register_regional_host(
                db,
                node_id='regional-host-1',
                public_key_hex=child_keypair['public_hex'],
                region_name='us-east-1',
                compute_info={'url': 'http://us-east.test:8000', 'cpu_cores': 16, 'ram_gb': 64},
                certificate=cert,
            )
        assert result['registered'] is True
        assert result['region_name'] == 'us-east-1'

        # Verify PeerNode was created
        peer = db.query(PeerNode).filter_by(node_id='regional-host-1').first()
        assert peer is not None
        assert peer.tier == 'regional'
        assert peer.certificate_verified is True

    def test_register_regional_host_invalid_cert(self, db, child_keypair):
        from integrations.social.hierarchy_service import HierarchyService
        fake_cert = {
            'node_id': 'fake-regional',
            'public_key': child_keypair['public_hex'],
            'tier': 'regional',
            'parent_public_key': '',
            'parent_signature': '',
        }
        result = HierarchyService.register_regional_host(
            db,
            node_id='fake-regional',
            public_key_hex=child_keypair['public_hex'],
            region_name='us-west-1',
            compute_info={},
            certificate=fake_cert,
        )
        assert result['registered'] is False

    def test_register_local_node(self, db, master_keypair, child_keypair):
        """Register a local node - requires at least one regional host to exist."""
        from integrations.social.hierarchy_service import HierarchyService
        from security.key_delegation import create_child_certificate

        # First ensure a regional host exists
        reg_key = Ed25519PrivateKey.generate()
        reg_pub = reg_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()
        cert = create_child_certificate(
            parent_private_key=master_keypair['private_key'],
            child_public_key_hex=reg_pub,
            node_id='regional-for-local',
            tier='regional',
            region_name='us-east-2',
        )
        with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
            HierarchyService.register_regional_host(
                db, 'regional-for-local', reg_pub, 'us-east-2',
                {'url': 'http://us-east-2.test:8000', 'cpu_cores': 8, 'ram_gb': 32,
                 'max_users': 100, 'dns_region': 'us-east'},
                cert,
            )

        # Now register local node
        local_key = Ed25519PrivateKey.generate()
        local_pub = local_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()
        result = HierarchyService.register_local_node(
            db, 'local-node-1', local_pub,
            compute_info={'cpu_cores': 4, 'ram_gb': 16},
            geo_info={'dns_region': 'us-east'},
        )
        assert result['registered'] is True
        assert 'assignment' in result
        assert result['assignment'].get('assigned') is True

    def test_auto_assignment_scoring(self, db):
        """Verify assignment prefers nodes with more headroom."""
        from integrations.social.hierarchy_service import HierarchyService

        # Create two regionals with different capacities
        for i, (users, max_cap) in enumerate([(10, 100), (90, 100)]):
            nid = f'scoring-regional-{i}'
            existing = db.query(PeerNode).filter_by(node_id=nid).first()
            if not existing:
                peer = PeerNode(
                    node_id=nid,
                    url=f'http://scoring-{i}.test:8000',
                    tier='regional',
                    status='active',
                    active_user_count=users,
                    max_user_capacity=max_cap,
                    dns_region='us-east',
                )
                db.add(peer)
                region = Region(
                    name=f'scoring-region-{i}',
                    host_node_id=nid,
                    is_accepting_nodes=True,
                )
                db.add(region)
        db.flush()

        result = HierarchyService.assign_to_region(
            db, 'local-scoring-test',
            {'cpu_cores': 4},
            {'dns_region': 'us-east'},
        )
        assert result['assigned'] is True
        # Should pick the one with more headroom (10/100 vs 90/100)
        assert result['regional_node_id'] == 'scoring-regional-0'

    def test_switch_region(self, db):
        from integrations.social.hierarchy_service import HierarchyService

        # Setup: two regions
        nid_old = f'switch-old-{uuid.uuid4().hex[:6]}'
        nid_new = f'switch-new-{uuid.uuid4().hex[:6]}'
        for nid in [nid_old, nid_new]:
            peer = PeerNode(
                node_id=nid, url=f'http://{nid}.test:8000',
                tier='regional', status='active',
                active_user_count=5, max_user_capacity=100,
            )
            db.add(peer)
        db.flush()

        region_old = Region(name=f'region-old-{uuid.uuid4().hex[:4]}', host_node_id=nid_old, is_accepting_nodes=True)
        region_new = Region(name=f'region-new-{uuid.uuid4().hex[:4]}', host_node_id=nid_new, is_accepting_nodes=True)
        db.add_all([region_old, region_new])
        db.flush()

        # Create assignment
        local_nid = f'switch-local-{uuid.uuid4().hex[:6]}'
        assignment = RegionAssignment(
            local_node_id=local_nid,
            regional_node_id=nid_old,
            region_id=region_old.id,
            status='active',
        )
        db.add(assignment)
        local_peer = PeerNode(
            node_id=local_nid, url='http://local.test:8000',
            tier='local', status='active',
        )
        db.add(local_peer)
        db.flush()

        # Switch
        result = HierarchyService.switch_region(
            db, local_nid, region_new.id, 'user_choice')
        assert result['switched'] is True
        assert result['regional_node_id'] == nid_new

        # Old assignment should be revoked
        old_assignment = db.query(RegionAssignment).filter_by(id=assignment.id).first()
        assert old_assignment.status == 'revoked'

    def test_gossip_targets_flat(self, db):
        from integrations.social.hierarchy_service import HierarchyService

        nid = f'flat-peer-{uuid.uuid4().hex[:6]}'
        peer = PeerNode(node_id=nid, url='http://flat.test:8000', status='active')
        db.add(peer)
        db.flush()

        targets = HierarchyService.get_gossip_targets(db, 'my-node', 'flat')
        node_ids = [t['node_id'] for t in targets]
        assert nid in node_ids

    def test_gossip_targets_central(self, db):
        from integrations.social.hierarchy_service import HierarchyService

        # Create a regional and a local - central should only see regional
        reg_nid = f'c-reg-{uuid.uuid4().hex[:6]}'
        loc_nid = f'c-loc-{uuid.uuid4().hex[:6]}'
        db.add(PeerNode(node_id=reg_nid, url='http://reg.test:8000', tier='regional', status='active'))
        db.add(PeerNode(node_id=loc_nid, url='http://loc.test:8000', tier='local', status='active'))
        db.flush()

        targets = HierarchyService.get_gossip_targets(db, 'central-node', 'central')
        node_ids = [t['node_id'] for t in targets]
        assert reg_nid in node_ids
        assert loc_nid not in node_ids

    def test_gossip_targets_regional(self, db):
        from integrations.social.hierarchy_service import HierarchyService

        reg_nid = f'my-reg-{uuid.uuid4().hex[:6]}'
        central_nid = f'r-central-{uuid.uuid4().hex[:6]}'
        local_nid = f'r-local-{uuid.uuid4().hex[:6]}'
        db.add(PeerNode(node_id=central_nid, url='http://central.test:8000', tier='central', status='active'))
        db.add(PeerNode(node_id=local_nid, url='http://local.test:8000', tier='local', status='active',
                        parent_node_id=reg_nid))
        db.flush()

        targets = HierarchyService.get_gossip_targets(db, reg_nid, 'regional')
        node_ids = [t['node_id'] for t in targets]
        assert central_nid in node_ids
        assert local_nid in node_ids

    def test_gossip_targets_local(self, db):
        from integrations.social.hierarchy_service import HierarchyService

        local_nid = f'l-node-{uuid.uuid4().hex[:6]}'
        reg_nid = f'l-reg-{uuid.uuid4().hex[:6]}'
        db.add(PeerNode(node_id=reg_nid, url='http://reg.test:8000', tier='regional', status='active'))
        assignment = RegionAssignment(
            local_node_id=local_nid,
            regional_node_id=reg_nid,
            status='active',
        )
        db.add(assignment)
        db.flush()

        targets = HierarchyService.get_gossip_targets(db, local_nid, 'local')
        assert len(targets) == 1
        assert targets[0]['node_id'] == reg_nid

    def test_report_node_capacity(self, db):
        from integrations.social.hierarchy_service import HierarchyService

        nid = f'cap-{uuid.uuid4().hex[:6]}'
        db.add(PeerNode(node_id=nid, url='http://test:8000', tier='regional', status='active'))
        db.flush()

        result = HierarchyService.report_node_capacity(
            db, nid, {'cpu_cores': 32, 'ram_gb': 128, 'gpu_count': 8})
        assert result['updated'] is True

        peer = db.query(PeerNode).filter_by(node_id=nid).first()
        assert peer.compute_cpu_cores == 32
        assert peer.compute_ram_gb == 128

    def test_get_region_health(self, db):
        from integrations.social.hierarchy_service import HierarchyService

        nid = f'health-reg-{uuid.uuid4().hex[:6]}'
        db.add(PeerNode(node_id=nid, url='http://health.test:8000', tier='regional', status='active'))
        region = Region(name=f'health-region-{uuid.uuid4().hex[:4]}', host_node_id=nid, is_accepting_nodes=True)
        db.add(region)
        db.flush()

        health = HierarchyService.get_region_health(db, region.id)
        assert health is not None
        assert health['host_status'] == 'active'
        assert health['is_accepting'] is True


# =====================================================================
# TEST CLASS 7: Tier-Aware Gossip
# =====================================================================

class TestTierAwareGossip:
    """Test that peer_discovery uses tier for gossip scoping."""

    def test_gossip_default_tier_is_flat(self):
        from integrations.social.peer_discovery import GossipProtocol
        with patch.dict(os.environ, {}, clear=False):
            g = GossipProtocol()
        assert g.tier == 'flat'

    def test_gossip_reads_tier_from_env(self):
        from integrations.social.peer_discovery import GossipProtocol
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'regional'}):
            g = GossipProtocol()
        assert g.tier == 'regional'

    def test_self_info_includes_tier(self):
        from integrations.social.peer_discovery import GossipProtocol
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'central'}):
            g = GossipProtocol()
        info = g._self_info()
        assert info['tier'] == 'central'

    def test_merge_peer_stores_tier(self, db):
        """When merging a peer with tier info and certificate, it should be stored."""
        from integrations.social.peer_discovery import GossipProtocol
        g = GossipProtocol()
        peer_data = {
            'node_id': f'tier-merge-{uuid.uuid4().hex[:8]}',
            'url': 'http://tier-test.test:8000',
            'name': 'tier-test',
            'version': '1.0.0',
            'tier': 'regional',
            'certificate': {
                'node_id': f'tier-merge-test',
                'tier': 'regional',
                'parent_public_key': 'abc123',
                'signature': 'sig123',
                'expires_at': (datetime.utcnow().replace(year=datetime.utcnow().year + 1)).isoformat(),
            },
        }
        with patch('security.key_delegation.verify_certificate_chain', return_value={'valid': True}), \
             patch('security.master_key.get_enforcement_mode', return_value='hard'):
            is_new = g._merge_peer(db, peer_data)
        assert is_new is True
        stored = db.query(PeerNode).filter_by(node_id=peer_data['node_id']).first()
        assert stored.tier == 'regional'

    def test_merge_peer_rejects_invalid_cert_hard(self, db):
        """In hard enforcement, reject peers with invalid certificates claiming regional."""
        from integrations.social.peer_discovery import GossipProtocol
        g = GossipProtocol()
        peer_data = {
            'node_id': f'bad-cert-{uuid.uuid4().hex[:8]}',
            'url': 'http://bad-cert.test:8000',
            'name': 'bad-cert',
            'version': '1.0.0',
            'tier': 'regional',
            'certificate': {
                'node_id': 'bad-cert',
                'tier': 'regional',
                'parent_public_key': '',
                'parent_signature': 'deadbeef',
            },
        }
        with patch('security.master_key.get_enforcement_mode', return_value='hard'):
            is_new = g._merge_peer(db, peer_data)
        assert is_new is False  # Rejected


# =====================================================================
# TEST CLASS 8: Sync Engine
# =====================================================================

class TestSyncEngine:
    """Test sync queue/drain, retry logic, batch receive."""

    def test_queue_operation(self, db):
        from integrations.social.sync_engine import SyncEngine
        with patch('security.node_integrity.get_public_key_hex', return_value='abcd1234abcd1234'):
            item_id = SyncEngine.queue(
                db, 'central', 'sync_post',
                {'post_id': '123', 'title': 'Test Post'})
        assert item_id is not None
        item = db.query(SyncQueue).filter_by(id=item_id).first()
        assert item.status == 'queued'
        assert item.target_tier == 'central'
        assert item.operation_type == 'sync_post'

    def test_drain_queue_success(self, db):
        from integrations.social.sync_engine import SyncEngine

        # Queue items
        nid = 'drain-test-node'
        for i in range(3):
            item = SyncQueue(
                node_id=nid, target_tier='central',
                operation_type='sync_post',
                payload_json={'post_id': str(i)},
                status='queued',
            )
            db.add(item)
        db.flush()

        # Get item IDs
        items = db.query(SyncQueue).filter_by(node_id=nid, status='queued').all()
        item_ids = [it.id for it in items]

        # Mock successful HTTP response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'processed': item_ids}

        with patch('requests.post', return_value=mock_resp):
            result = SyncEngine.drain_queue(db, nid, 'http://central.test:8000')

        assert result['sent'] == 3
        assert result['failed'] == 0

    def test_drain_queue_failure_increments_retry(self, db):
        from integrations.social.sync_engine import SyncEngine

        nid = 'retry-test-node'
        item = SyncQueue(
            node_id=nid, target_tier='central',
            operation_type='update_stats',
            payload_json={'stats': 'data'},
            status='queued',
            retry_count=0,
        )
        db.add(item)
        db.flush()

        import requests as req_module
        with patch('integrations.social.sync_engine.requests.post',
                   side_effect=req_module.RequestException('Connection refused')):
            result = SyncEngine.drain_queue(db, nid, 'http://unreachable:8000')

        assert result['failed'] == 1
        refreshed = db.query(SyncQueue).filter_by(id=item.id).first()
        assert refreshed.retry_count == 1

    def test_receive_sync_batch(self, db):
        from integrations.social.sync_engine import SyncEngine
        items = [
            {'id': 'item-1', 'operation_type': 'sync_post', 'payload': {'title': 'Test'}},
            {'id': 'item-2', 'operation_type': 'register_agent', 'payload': {'name': 'Agent'}},
            {'id': 'item-3', 'operation_type': 'update_stats', 'payload': {'count': 5}},
        ]
        result = SyncEngine.receive_sync_batch(db, items)
        assert len(result['processed']) == 3
        assert len(result['errors']) == 0

    def test_is_connected_to_success(self):
        from integrations.social.sync_engine import SyncEngine
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch('requests.get', return_value=mock_resp):
            assert SyncEngine.is_connected_to('http://central.test:8000') is True

    def test_is_connected_to_failure(self):
        from integrations.social.sync_engine import SyncEngine
        import requests as req_module
        with patch('integrations.social.sync_engine.requests.get',
                   side_effect=req_module.RequestException('timeout')):
            assert SyncEngine.is_connected_to('http://unreachable:8000') is False

    def test_get_queue_stats(self, db):
        from integrations.social.sync_engine import SyncEngine

        nid = f'stats-{uuid.uuid4().hex[:6]}'
        for status in ['queued', 'queued', 'completed', 'failed']:
            item = SyncQueue(
                node_id=nid, target_tier='central',
                operation_type='sync_post',
                payload_json={}, status=status,
            )
            db.add(item)
        db.flush()

        stats = SyncEngine.get_queue_stats(db, nid)
        assert stats['queued'] == 2
        assert stats['completed'] == 1
        assert stats['failed'] == 1
        assert stats['total_pending'] == 2


# =====================================================================
# TEST CLASS 9: Migration v13
# =====================================================================

class TestMigrationV13:

    def test_schema_version_is_at_least_13(self):
        from integrations.social.migrations import SCHEMA_VERSION
        assert SCHEMA_VERSION >= 13

    def test_new_tables_exist(self, engine, tables):
        from sqlalchemy import inspect
        inspector = inspect(engine)
        table_names = inspector.get_table_names()
        assert 'region_assignments' in table_names
        assert 'sync_queue' in table_names

    def test_peer_node_has_tier_column(self, engine, tables):
        from sqlalchemy import inspect
        inspector = inspect(engine)
        columns = [c['name'] for c in inspector.get_columns('peer_nodes')]
        assert 'tier' in columns
        assert 'parent_node_id' in columns
        assert 'certificate_json' in columns
        assert 'certificate_verified' in columns
        assert 'compute_cpu_cores' in columns
        assert 'dns_region' in columns

    def test_region_has_hierarchy_columns(self, engine, tables):
        from sqlalchemy import inspect
        inspector = inspect(engine)
        columns = [c['name'] for c in inspector.get_columns('regions')]
        assert 'host_node_id' in columns
        assert 'capacity_cpu' in columns
        assert 'is_accepting_nodes' in columns
        assert 'central_approved' in columns

    def test_region_assignment_columns(self, engine, tables):
        from sqlalchemy import inspect
        inspector = inspect(engine)
        columns = [c['name'] for c in inspector.get_columns('region_assignments')]
        assert 'local_node_id' in columns
        assert 'regional_node_id' in columns
        assert 'assigned_by' in columns
        assert 'approved_by_central' in columns


# =====================================================================
# TEST CLASS 10: Certificate Save/Load
# =====================================================================

class TestCertificatePersistence:

    def test_save_and_load_certificate(self, tmp_path, master_keypair, child_keypair):
        from security.key_delegation import (
            create_child_certificate, save_node_certificate, load_node_certificate,
        )
        cert = create_child_certificate(
            parent_private_key=master_keypair['private_key'],
            child_public_key_hex=child_keypair['public_hex'],
            node_id='persist-test',
            tier='regional',
            region_name='us-west-2',
        )
        cert_path = str(tmp_path / 'node_certificate.json')
        save_node_certificate(cert, cert_path)
        loaded = load_node_certificate(cert_path)
        assert loaded is not None
        assert loaded['node_id'] == 'persist-test'
        assert loaded['parent_signature'] == cert['parent_signature']

    def test_load_nonexistent_returns_none(self, tmp_path):
        from security.key_delegation import load_node_certificate
        result = load_node_certificate(str(tmp_path / 'nonexistent.json'))
        assert result is None


# =====================================================================
# TEST CLASS 11: get_node_tier
# =====================================================================

class TestGetNodeTier:

    def test_default_is_flat(self):
        from security.key_delegation import get_node_tier
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HEVOLVE_NODE_TIER', None)
            assert get_node_tier() == 'flat'

    def test_central(self):
        from security.key_delegation import get_node_tier
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'central'}):
            assert get_node_tier() == 'central'

    def test_regional(self):
        from security.key_delegation import get_node_tier
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'regional'}):
            assert get_node_tier() == 'regional'

    def test_local(self):
        from security.key_delegation import get_node_tier
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'local'}):
            assert get_node_tier() == 'local'

    def test_invalid_defaults_to_flat(self):
        from security.key_delegation import get_node_tier
        with patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'invalid'}):
            assert get_node_tier() == 'flat'
