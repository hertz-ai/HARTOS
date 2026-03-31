"""
Master Key & Deployment Control Test Suite
============================================
Tests covering:
- Master keypair generation and signing
- Manifest creation, loading, and verification
- Boot verification (all enforcement modes)
- Runtime tamper detection
- Gossip peer rejection based on code hash mismatch
- Registry node rejection in soft/hard mode
- PeerNode master_key_verified column
- Migration v12

All external calls mocked -- in-memory SQLite.
"""
import os
import sys
import uuid
import json
import tempfile
import shutil
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from pathlib import Path

# Add parent dir for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

# Force in-memory SQLite before importing models
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from integrations.social.models import Base, PeerNode


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
def tmp_code_root(tmp_path):
    """Create a temp directory with a .py file for code hashing."""
    (tmp_path / 'test_module.py').write_text('print("hello")\n')
    return str(tmp_path)


def _sign_manifest(manifest: dict, private_key: Ed25519PrivateKey) -> str:
    """Sign a manifest dict, return hex signature."""
    clean = {k: v for k, v in manifest.items() if k != 'master_signature'}
    canonical = json.dumps(clean, sort_keys=True, separators=(',', ':'))
    sig = private_key.sign(canonical.encode('utf-8'))
    return sig.hex()


def _create_signed_manifest(keypair: dict, code_hash: str = 'abc123') -> dict:
    """Create a complete signed manifest for testing."""
    manifest = {
        'version': 'v1.0.0-test',
        'git_sha': 'testsha',
        'code_hash': code_hash,
        'file_manifest_hash': 'manifest_hash_test',
        'built_at': '2026-02-06T00:00:00Z',
        'master_public_key': keypair['public_hex'],
    }
    manifest['master_signature'] = _sign_manifest(manifest, keypair['private_key'])
    return manifest


_counter = 0


def _uid():
    global _counter
    _counter += 1
    return f'mk_test_{_counter}_{uuid.uuid4().hex[:8]}'


# =====================================================================
# TEST: Master Key Module
# =====================================================================

class TestMasterKeyGeneration:
    """Test keypair generation script logic."""

    def test_generate_keypair_produces_valid_keys(self, master_keypair):
        """Generate a keypair and verify it can sign/verify."""
        msg = b'test message'
        sig = master_keypair['private_key'].sign(msg)
        # Should not raise
        master_keypair['public_key'].verify(sig, msg)

    def test_keypair_hex_lengths(self, master_keypair):
        assert len(master_keypair['private_hex']) == 64
        assert len(master_keypair['public_hex']) == 64


class TestMasterSignatureVerification:
    """Test verify_master_signature function."""

    def test_valid_signature(self, master_keypair):
        from security.master_key import verify_master_signature
        payload = {'version': 'v1.0.0', 'code_hash': 'abc123'}
        sig = _sign_manifest(payload, master_keypair['private_key'])
        with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
            assert verify_master_signature(payload, sig) is True

    def test_invalid_signature(self, master_keypair):
        from security.master_key import verify_master_signature
        payload = {'version': 'v1.0.0', 'code_hash': 'abc123'}
        with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
            assert verify_master_signature(payload, 'bad' * 32) is False

    def test_tampered_payload(self, master_keypair):
        from security.master_key import verify_master_signature
        payload = {'version': 'v1.0.0', 'code_hash': 'abc123'}
        sig = _sign_manifest(payload, master_keypair['private_key'])
        payload['code_hash'] = 'tampered'
        with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
            assert verify_master_signature(payload, sig) is False

    def test_master_signature_field_excluded(self, master_keypair):
        """master_signature field should be stripped before verification."""
        from security.master_key import verify_master_signature
        payload = {'version': 'v1.0.0', 'code_hash': 'abc123'}
        sig = _sign_manifest(payload, master_keypair['private_key'])
        payload['master_signature'] = sig  # Add the sig into payload
        with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
            assert verify_master_signature(payload, sig) is True


class TestReleaseManifest:
    """Test manifest loading and verification."""

    def test_load_manifest_file(self, master_keypair, tmp_path):
        manifest = _create_signed_manifest(master_keypair)
        manifest_path = tmp_path / 'release_manifest.json'
        manifest_path.write_text(json.dumps(manifest))

        from security.master_key import load_release_manifest
        loaded = load_release_manifest(str(tmp_path))
        assert loaded is not None
        assert loaded['version'] == 'v1.0.0-test'

    def test_load_manifest_missing(self, tmp_path):
        from security.master_key import load_release_manifest
        result = load_release_manifest(str(tmp_path))
        assert result is None

    def test_load_manifest_invalid_json(self, tmp_path):
        (tmp_path / 'release_manifest.json').write_text('not json{{{')
        from security.master_key import load_release_manifest
        result = load_release_manifest(str(tmp_path))
        assert result is None

    def test_verify_valid_manifest(self, master_keypair):
        from security.master_key import verify_release_manifest
        manifest = _create_signed_manifest(master_keypair)
        with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
            assert verify_release_manifest(manifest) is True

    def test_verify_unsigned_manifest(self):
        from security.master_key import verify_release_manifest
        manifest = {'version': 'v1.0.0', 'code_hash': 'abc'}
        assert verify_release_manifest(manifest) is False

    def test_verify_manifest_wrong_key(self, master_keypair):
        """Manifest signed with a different key should fail."""
        from security.master_key import verify_release_manifest
        other_kp = Ed25519PrivateKey.generate()
        manifest = {'version': 'v1.0.0', 'code_hash': 'abc'}
        sig = other_kp.sign(
            json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
        manifest['master_signature'] = sig.hex()
        with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
            assert verify_release_manifest(manifest) is False


class TestCodeHashComparison:
    """Test verify_local_code_matches_manifest."""

    def test_matching_hash(self, master_keypair, tmp_code_root):
        from security.node_integrity import compute_code_hash
        real_hash = compute_code_hash(tmp_code_root)
        manifest = _create_signed_manifest(master_keypair, code_hash=real_hash)

        from security.master_key import verify_local_code_matches_manifest
        result = verify_local_code_matches_manifest(manifest, tmp_code_root)
        assert result['verified'] is True

    def test_mismatched_hash(self, master_keypair, tmp_code_root):
        manifest = _create_signed_manifest(master_keypair, code_hash='wrong_hash')

        from security.master_key import verify_local_code_matches_manifest
        result = verify_local_code_matches_manifest(manifest, tmp_code_root)
        assert result['verified'] is False
        assert 'mismatch' in result['details'].lower()


class TestEnforcementModes:
    """Test is_dev_mode and get_enforcement_mode."""

    def test_dev_mode_true(self):
        from security.master_key import is_dev_mode
        with patch.dict(os.environ, {'HEVOLVE_DEV_MODE': 'true'}):
            assert is_dev_mode() is True

    def test_dev_mode_false(self):
        from security.master_key import is_dev_mode
        with patch.dict(os.environ, {'HEVOLVE_DEV_MODE': 'false'}):
            assert is_dev_mode() is False

    def test_dev_mode_unset(self):
        from security.master_key import is_dev_mode
        env = os.environ.copy()
        env.pop('HEVOLVE_DEV_MODE', None)
        with patch.dict(os.environ, env, clear=True):
            assert is_dev_mode() is False

    def test_enforcement_hard(self):
        from security.master_key import get_enforcement_mode
        with patch.dict(os.environ, {'HEVOLVE_ENFORCEMENT_MODE': 'hard'}):
            assert get_enforcement_mode() == 'hard'

    def test_enforcement_soft(self):
        from security.master_key import get_enforcement_mode
        with patch.dict(os.environ, {'HEVOLVE_ENFORCEMENT_MODE': 'soft'}):
            assert get_enforcement_mode() == 'soft'

    def test_enforcement_invalid_defaults_hard(self):
        from security.master_key import get_enforcement_mode
        with patch.dict(os.environ, {'HEVOLVE_ENFORCEMENT_MODE': 'invalid'}):
            assert get_enforcement_mode() == 'hard'


class TestBootVerification:
    """Test full_boot_verification."""

    def test_dev_mode_bypass(self):
        from security.master_key import full_boot_verification
        with patch.dict(os.environ, {'HEVOLVE_DEV_MODE': 'true'}):
            result = full_boot_verification()
            assert result['passed'] is True
            assert 'dev mode' in result['details'].lower() or 'Dev mode' in result['details']

    def test_enforcement_off(self):
        from security.master_key import full_boot_verification
        with patch.dict(os.environ, {'HEVOLVE_DEV_MODE': 'false',
                                      'HEVOLVE_ENFORCEMENT_MODE': 'off'}):
            result = full_boot_verification()
            assert result['passed'] is True

    def test_no_manifest_fails(self, tmp_path):
        from security.master_key import full_boot_verification
        with patch.dict(os.environ, {'HEVOLVE_DEV_MODE': 'false',
                                      'HEVOLVE_ENFORCEMENT_MODE': 'hard'}):
            result = full_boot_verification(str(tmp_path))
            assert result['passed'] is False
            assert 'manifest' in result['details'].lower()

    def test_valid_manifest_passes(self, master_keypair, tmp_code_root):
        from security.master_key import full_boot_verification
        from security.node_integrity import compute_code_hash
        real_hash = compute_code_hash(tmp_code_root)
        manifest = _create_signed_manifest(master_keypair, code_hash=real_hash)
        manifest_path = Path(tmp_code_root) / 'release_manifest.json'
        manifest_path.write_text(json.dumps(manifest))

        mock_origin = {'verified': True, 'details': 'test mode'}
        with patch.dict(os.environ, {'HEVOLVE_DEV_MODE': 'false',
                                      'HEVOLVE_ENFORCEMENT_MODE': 'hard'}):
            with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
                with patch('security.origin_attestation.verify_origin',
                           return_value=mock_origin):
                    result = full_boot_verification(tmp_code_root)
                    assert result['passed'] is True

    def test_tampered_code_fails(self, master_keypair, tmp_code_root):
        """If code is modified after signing, verification should fail."""
        from security.master_key import full_boot_verification
        manifest = _create_signed_manifest(master_keypair, code_hash='original_hash')
        manifest_path = Path(tmp_code_root) / 'release_manifest.json'
        manifest_path.write_text(json.dumps(manifest))

        with patch.dict(os.environ, {'HEVOLVE_DEV_MODE': 'false',
                                      'HEVOLVE_ENFORCEMENT_MODE': 'hard'}):
            with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
                result = full_boot_verification(tmp_code_root)
                assert result['passed'] is False


# =====================================================================
# TEST: Runtime Integrity Monitor
# =====================================================================

class TestRuntimeMonitor:
    """Test RuntimeIntegrityMonitor."""

    def test_monitor_starts_healthy(self, tmp_code_root):
        from security.node_integrity import compute_code_hash
        real_hash = compute_code_hash(tmp_code_root)
        manifest = {'code_hash': real_hash, 'version': 'v1.0.0'}

        from security.runtime_monitor import RuntimeIntegrityMonitor
        monitor = RuntimeIntegrityMonitor(manifest, check_interval=1,
                                          code_root=tmp_code_root)
        assert monitor.is_healthy is True

    def test_monitor_detects_tamper(self, tmp_code_root):
        """Monitor should detect when code hash changes."""
        manifest = {'code_hash': 'expected_hash', 'version': 'v1.0.0'}

        from security.runtime_monitor import RuntimeIntegrityMonitor
        monitor = RuntimeIntegrityMonitor(manifest, check_interval=1,
                                          code_root=tmp_code_root)
        # Simulate tamper by having a mismatch between expected and actual
        assert monitor._expected_hash == 'expected_hash'
        # The actual compute will differ from 'expected_hash' since we wrote a real file
        monitor._check_loop_once_for_test()
        assert monitor.is_healthy is False

    def test_is_code_healthy_without_monitor(self):
        from security.runtime_monitor import is_code_healthy
        # When no monitor is set, default to True
        import security.runtime_monitor as rm
        old_monitor = rm._monitor
        rm._monitor = None
        try:
            assert is_code_healthy() is True
        finally:
            rm._monitor = old_monitor


# =====================================================================
# TEST: Gossip Peer Rejection
# =====================================================================

class TestGossipMasterKeyVerification:
    """Test that gossip rejects peers with mismatched code hash in hard mode."""

    def test_peer_accepted_matching_hash(self, db, master_keypair, tmp_code_root):
        """Peers with matching code hash should be accepted."""
        from security.node_integrity import compute_code_hash
        real_hash = compute_code_hash(tmp_code_root)
        manifest = _create_signed_manifest(master_keypair, code_hash=real_hash)

        from integrations.social.peer_discovery import GossipProtocol
        g = GossipProtocol()

        peer_data = {
            'node_id': _uid(),
            'url': 'http://peer1:6777',
            'name': 'test-peer',
            'code_hash': real_hash,
            'signature': 'test_sig',
            'public_key': 'test_pk',
        }

        with patch('security.master_key.load_release_manifest', return_value=manifest):
            with patch('security.master_key.get_enforcement_mode', return_value='hard'):
                with patch('security.master_key.MASTER_PUBLIC_KEY_HEX',
                          master_keypair['public_hex']):
                    with patch('security.node_integrity.verify_json_signature', return_value=True):
                        is_new = g._merge_peer(db, peer_data)
                        assert is_new is True

    def test_peer_rejected_mismatched_hash_hard(self, db, master_keypair):
        """Peers with mismatched code hash should be rejected in hard mode."""
        manifest = _create_signed_manifest(master_keypair, code_hash='official_hash')

        from integrations.social.peer_discovery import GossipProtocol
        g = GossipProtocol()

        peer_data = {
            'node_id': _uid(),
            'url': 'http://peer2:6777',
            'name': 'bad-peer',
            'code_hash': 'tampered_hash',
        }

        with patch('security.master_key.load_release_manifest', return_value=manifest):
            with patch('security.master_key.get_enforcement_mode', return_value='hard'):
                is_new = g._merge_peer(db, peer_data)
                assert is_new is False

    def test_peer_allowed_mismatched_hash_warn(self, db, master_keypair):
        """Peers with mismatched code hash should be allowed in warn mode."""
        manifest = _create_signed_manifest(master_keypair, code_hash='official_hash')

        from integrations.social.peer_discovery import GossipProtocol
        g = GossipProtocol()

        peer_data = {
            'node_id': _uid(),
            'url': 'http://peer3:6777',
            'name': 'warn-peer',
            'code_hash': 'different_hash',
        }

        with patch('security.master_key.load_release_manifest', return_value=manifest):
            with patch('security.master_key.get_enforcement_mode', return_value='warn'):
                is_new = g._merge_peer(db, peer_data)
                assert is_new is True


# =====================================================================
# TEST: PeerNode Model
# =====================================================================

class TestPeerNodeMasterKeyFields:
    """Test new PeerNode columns for master key verification."""

    def test_default_master_key_verified(self, db):
        peer = PeerNode(
            node_id=_uid(),
            url='http://test:6777',
            name='test',
            version='1.0.0',
        )
        db.add(peer)
        db.flush()
        assert peer.master_key_verified is False or peer.master_key_verified == 0
        assert peer.release_version is None

    def test_master_key_verified_set(self, db):
        peer = PeerNode(
            node_id=_uid(),
            url='http://test2:6777',
            name='test2',
            version='1.0.0',
            master_key_verified=True,
            release_version='v1.0.0',
        )
        db.add(peer)
        db.flush()
        assert peer.master_key_verified is True or peer.master_key_verified == 1
        assert peer.release_version == 'v1.0.0'

    def test_to_dict_includes_fields(self, db):
        peer = PeerNode(
            node_id=_uid(),
            url='http://test3:6777',
            name='test3',
            version='1.0.0',
            master_key_verified=True,
            release_version='v2.0.0',
        )
        db.add(peer)
        db.flush()
        d = peer.to_dict()
        assert 'master_key_verified' in d
        assert 'release_version' in d
        assert d['release_version'] == 'v2.0.0'


# =====================================================================
# TEST: Integrity Service Master Key Integration
# =====================================================================

class TestIntegrityServiceMasterKey:
    """Test that IntegrityService.verify_code_hash uses master manifest first."""

    def test_verify_code_hash_uses_manifest_first(self, db, master_keypair):
        """verify_code_hash should check against master manifest before registry."""
        node_id = _uid()
        peer = PeerNode(
            node_id=node_id,
            url='http://test4:6777',
            name='test4',
            version='1.0.0',
            code_hash='matching_hash',
        )
        db.add(peer)
        db.flush()

        manifest = _create_signed_manifest(master_keypair, code_hash='matching_hash')

        from integrations.social.integrity_service import IntegrityService
        with patch('security.master_key.load_release_manifest', return_value=manifest):
            with patch('security.master_key.verify_release_manifest', return_value=True):
                result = IntegrityService.verify_code_hash(db, node_id)
                assert result['verified'] is True

    def test_verify_code_hash_mismatch_via_manifest(self, db, master_keypair):
        """Code hash mismatch against master manifest should increase fraud score."""
        node_id = _uid()
        peer = PeerNode(
            node_id=node_id,
            url='http://test5:6777',
            name='test5',
            version='1.0.0',
            code_hash='tampered_hash',
        )
        db.add(peer)
        db.flush()

        manifest = _create_signed_manifest(master_keypair, code_hash='official_hash')

        from integrations.social.integrity_service import IntegrityService
        with patch('security.master_key.load_release_manifest', return_value=manifest):
            with patch('security.master_key.verify_release_manifest', return_value=True):
                result = IntegrityService.verify_code_hash(db, node_id)
                assert result['verified'] is False
                # Fraud score should have increased
                db.refresh(peer)
                assert peer.fraud_score > 0


# =====================================================================
# TEST: Migration v12
# =====================================================================

class TestMigrationV12:
    """Test schema migration to v12."""

    @pytest.mark.skipif(
        tuple(int(x) for x in __import__('sqlite3').sqlite_version.split('.')) < (3, 35, 0),
        reason='SQLite < 3.35 does not support RETURNING clause')
    def test_migration_adds_columns(self):
        """Verify that v12 migration adds master_key_verified and release_version."""
        from integrations.social.migrations import run_migrations, get_schema_version
        from integrations.social.models import get_engine
        engine = get_engine()

        # Run migrations (should be idempotent)
        run_migrations()

        version = get_schema_version(engine)
        assert version >= 12

        # Verify columns exist
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA table_info(peer_nodes)"))
            columns = {row[1] for row in result.fetchall()}
            assert 'master_key_verified' in columns
            assert 'release_version' in columns


# =====================================================================
# TEST: Sign Release Script
# =====================================================================

class TestSignReleaseScript:
    """Test the sign_release.py script logic."""

    def test_sign_and_verify_roundtrip(self, master_keypair, tmp_path):
        """Sign a manifest and verify it with the public key."""
        manifest = {
            'version': 'v1.0.0',
            'git_sha': 'abc123',
            'code_hash': 'hash_value',
            'file_manifest_hash': 'mhash',
            'built_at': '2026-02-06T00:00:00Z',
            'master_public_key': master_keypair['public_hex'],
        }
        canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
        sig = master_keypair['private_key'].sign(canonical.encode('utf-8'))
        manifest['master_signature'] = sig.hex()

        # Write to file
        manifest_path = tmp_path / 'release_manifest.json'
        manifest_path.write_text(json.dumps(manifest))

        # Verify
        from security.master_key import load_release_manifest, verify_release_manifest
        loaded = load_release_manifest(str(tmp_path))
        assert loaded is not None
        with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', master_keypair['public_hex']):
            assert verify_release_manifest(loaded) is True


# =====================================================================
# TEST: Self Info Release Data
# =====================================================================

class TestSelfInfoReleaseData:
    """Test that _self_info includes release manifest data."""

    def test_self_info_includes_release_fields(self, master_keypair):
        """_self_info should include release_version and release_manifest_signature."""
        manifest = _create_signed_manifest(master_keypair)

        from integrations.social.peer_discovery import GossipProtocol
        g = GossipProtocol()

        with patch('security.master_key.load_release_manifest', return_value=manifest):
            with patch('security.node_integrity.get_public_key_hex', return_value='aabbcc'):
                with patch('security.node_integrity.compute_code_hash', return_value='hash'):
                    with patch('security.node_integrity.sign_json_payload', return_value='sig'):
                        info = g._self_info()
                        assert info.get('release_version') == 'v1.0.0-test'
                        assert info.get('release_manifest_signature') == manifest['master_signature']
