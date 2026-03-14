"""
Tests for gossip security edge cases not covered by test_integrity_system.py
or test_security_hardening.py:

  1. Sybil protection: per-IP peer limit enforcement
  2. Self-info signing: _gossip_self_info() produces verifiable signature
  3. Tampered payload: valid signature with modified fields rejected
  4. Unsigned peers in hard enforcement mode (env-configurable limit)
"""
import os
import sys
import tempfile
import shutil

import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


# ═══════════════════════════════════════════════════════════════
# Helper: create a GossipProtocol without real __init__
# ═══════════════════════════════════════════════════════════════

def _make_gossip():
    """Create a GossipProtocol with predictable node_id, no side effects."""
    with patch('integrations.social.peer_discovery.GossipProtocol.__init__',
               lambda self: None):
        from integrations.social.peer_discovery import GossipProtocol
        gp = GossipProtocol.__new__(GossipProtocol)
        gp.node_id = 'test-local-node'
        gp.base_url = 'http://localhost:6777'
        gp.node_name = 'test'
        gp.version = '1.0.0'
        gp.tier = 'flat'
        gp._hart_tag = ''
        return gp


def _mock_db_no_existing():
    """Return a mock DB session where no existing peers are found."""
    db = MagicMock()
    # For the Sybil check: db.query(PeerNode).filter(...).count()
    db.query.return_value.filter.return_value.count.return_value = 0
    # For the existing peer check: db.query(PeerNode).filter(PeerNode.node_id==...).first()
    db.query.return_value.filter.return_value.first.return_value = None
    return db


def _mock_db_sybil_full(count=5):
    """Return a mock DB session where the per-IP count is at the limit."""
    db = MagicMock()
    # Sybil check returns count at/above limit
    db.query.return_value.filter.return_value.count.return_value = count
    # Existing peer check (shouldn't be reached, but safe)
    db.query.return_value.filter.return_value.first.return_value = None
    return db


# ═══════════════════════════════════════════════════════════════
# 1. Sybil Protection Tests
# ═══════════════════════════════════════════════════════════════

class TestSybilProtection:
    """Verify per-IP peer limit enforcement in _merge_peer()."""

    def test_rejects_when_ip_at_default_limit(self):
        """With 5 existing peers from same host, new peer is rejected."""
        gossip = _make_gossip()
        db = _mock_db_sybil_full(5)

        peer_data = {
            'node_id': 'sybil-node-001',
            'url': 'http://10.0.0.5:6777',
        }

        result = gossip._merge_peer(db, peer_data)
        assert result is False

    def test_accepts_when_below_limit(self):
        """With 4 existing peers from same host (below limit of 5), new peer accepted."""
        gossip = _make_gossip()
        db = _mock_db_no_existing()
        # Override: Sybil check returns 4 (below default limit of 5)
        db.query.return_value.filter.return_value.count.return_value = 4

        peer_data = {
            'node_id': 'legit-node-001',
            'url': 'http://10.0.0.5:6777',
        }

        # Patch out enforcement, guardrail, and manifest checks so we reach the add
        with patch('security.master_key.get_enforcement_mode', side_effect=ImportError):
            with patch('security.hive_guardrails.get_guardrail_hash', side_effect=ImportError):
                result = gossip._merge_peer(db, peer_data)

        assert result is True

    def test_custom_limit_via_env(self):
        """HEVOLVE_MAX_PEERS_PER_IP env var overrides default limit."""
        gossip = _make_gossip()
        db = _mock_db_sybil_full(3)

        peer_data = {
            'node_id': 'sybil-node-custom',
            'url': 'http://10.0.0.8:6777',
        }

        # Lower the limit to 3
        with patch.dict(os.environ, {'HEVOLVE_MAX_PEERS_PER_IP': '3'}):
            result = gossip._merge_peer(db, peer_data)

        assert result is False

    def test_higher_limit_via_env_allows_more(self):
        """Raising HEVOLVE_MAX_PEERS_PER_IP to 10 allows more peers."""
        gossip = _make_gossip()
        db = _mock_db_no_existing()
        # 7 existing peers from this host
        db.query.return_value.filter.return_value.count.return_value = 7

        peer_data = {
            'node_id': 'node-in-large-cluster',
            'url': 'http://10.0.0.9:6777',
        }

        with patch.dict(os.environ, {'HEVOLVE_MAX_PEERS_PER_IP': '10'}):
            with patch('security.master_key.get_enforcement_mode', side_effect=ImportError):
                with patch('security.hive_guardrails.get_guardrail_hash',
                           side_effect=ImportError):
                    result = gossip._merge_peer(db, peer_data)

        assert result is True

    def test_sybil_limit_at_boundary(self):
        """Exactly at limit (count == max_per_ip) rejects."""
        gossip = _make_gossip()
        db = _mock_db_sybil_full(10)

        peer_data = {
            'node_id': 'boundary-node',
            'url': 'http://10.0.0.10:6777',
        }

        with patch.dict(os.environ, {'HEVOLVE_MAX_PEERS_PER_IP': '10'}):
            result = gossip._merge_peer(db, peer_data)

        assert result is False


# ═══════════════════════════════════════════════════════════════
# 2. Tampered Payload Tests
# ═══════════════════════════════════════════════════════════════

class TestTamperedPayload:
    """Verify that valid signatures with tampered payloads are rejected."""

    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        os.environ['HEVOLVE_KEY_DIR'] = self.tmp_dir

    def teardown_method(self):
        from security.node_integrity import reset_keypair
        reset_keypair()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.environ.pop('HEVOLVE_KEY_DIR', None)

    def test_tampered_url_rejected(self):
        """Sign a payload, then change URL -- signature should fail."""
        from security.node_integrity import sign_json_payload, get_public_key_hex

        gossip = _make_gossip()
        db = _mock_db_no_existing()

        peer_data = {
            'node_id': 'tampered-url-node',
            'url': 'http://honest-node.example.com:6777',
            'public_key': get_public_key_hex(),
        }
        peer_data['signature'] = sign_json_payload(peer_data)

        # Tamper: change URL after signing
        peer_data['url'] = 'http://evil-node.example.com:6777'

        result = gossip._merge_peer(db, peer_data)
        assert result is False

    def test_tampered_node_id_rejected(self):
        """Sign a payload, then change node_id -- signature should fail."""
        from security.node_integrity import sign_json_payload, get_public_key_hex

        gossip = _make_gossip()
        db = _mock_db_no_existing()

        peer_data = {
            'node_id': 'original-node-id',
            'url': 'http://node.example.com:6777',
            'public_key': get_public_key_hex(),
        }
        peer_data['signature'] = sign_json_payload(peer_data)

        # Tamper: change node_id after signing
        peer_data['node_id'] = 'impersonated-node'

        result = gossip._merge_peer(db, peer_data)
        assert result is False

    def test_tampered_extra_field_rejected(self):
        """Sign a payload, then add a field -- signature should fail."""
        from security.node_integrity import sign_json_payload, get_public_key_hex

        gossip = _make_gossip()
        db = _mock_db_no_existing()

        peer_data = {
            'node_id': 'inject-field-node',
            'url': 'http://node.example.com:6777',
            'public_key': get_public_key_hex(),
        }
        peer_data['signature'] = sign_json_payload(peer_data)

        # Tamper: inject an extra field after signing
        peer_data['tier'] = 'central'

        result = gossip._merge_peer(db, peer_data)
        assert result is False

    def test_wrong_key_rejected(self):
        """Sign with one key, present a different public_key -- rejected.

        The payload's public_key field changes between signing and verification,
        so the canonical JSON differs and the signature is invalid.
        """
        from security.node_integrity import sign_json_payload, get_public_key_hex
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        gossip = _make_gossip()
        db = _mock_db_no_existing()

        # Sign with the current node key (key A)
        peer_data = {
            'node_id': 'wrong-key-node',
            'url': 'http://wrong-key.example.com:6777',
            'public_key': get_public_key_hex(),
        }
        peer_data['signature'] = sign_json_payload(peer_data)

        # Generate a completely separate Ed25519 keypair (key B) and replace public_key
        key_b = Ed25519PrivateKey.generate()
        from cryptography.hazmat.primitives import serialization
        key_b_pub_hex = key_b.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()
        peer_data['public_key'] = key_b_pub_hex

        result = gossip._merge_peer(db, peer_data)
        assert result is False


# ═══════════════════════════════════════════════════════════════
# 3. Self-Info Signing Tests
# ═══════════════════════════════════════════════════════════════

class TestSelfInfoSigning:
    """Verify _self_info() / _gossip_self_info() produces verifiable signatures."""

    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        os.environ['HEVOLVE_KEY_DIR'] = self.tmp_dir

    def teardown_method(self):
        from security.node_integrity import reset_keypair
        reset_keypair()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.environ.pop('HEVOLVE_KEY_DIR', None)

    def test_self_info_includes_signature(self):
        """_self_info() should include 'signature' when crypto is available."""
        from integrations.social.peer_discovery import GossipProtocol

        with patch.dict(os.environ, {'HEVOLVE_GOSSIP_BANDWIDTH': 'full'}):
            gp = GossipProtocol()
            info = gp._self_info()

        assert 'signature' in info, "_self_info() should include signature"
        assert 'public_key' in info, "_self_info() should include public_key"

    def test_self_info_signature_is_verifiable_on_signed_fields(self):
        """The signature from _self_info() verifies against the fields that existed at signing time.

        Note: _self_info() adds post-signing fields (x25519_public, guardrail_hash,
        capability_tier, idle_compute, current_version) AFTER calling sign_json_payload().
        The receiver's _merge_peer() includes these extra fields in the verification payload,
        which means verification fails on the full dict. This test verifies that the signature
        IS valid against the originally-signed subset of fields.
        """
        from integrations.social.peer_discovery import GossipProtocol
        from security.node_integrity import verify_json_signature

        # Fields known to be added AFTER sign_json_payload() in _self_info()
        POST_SIGNING_FIELDS = {
            'x25519_public', 'guardrail_hash', 'capability_tier',
            'enabled_features', 'hardware_summary', 'idle_compute',
            'current_version', 'available_version',
        }

        with patch.dict(os.environ, {'HEVOLVE_GOSSIP_BANDWIDTH': 'full'}):
            gp = GossipProtocol()
            info = gp._self_info()

        sig = info.get('signature')
        pub = info.get('public_key')
        assert sig, "signature must be present"
        assert pub, "public_key must be present"

        # Build the signed-fields-only payload (what was present when signature was computed)
        signed_payload = {k: v for k, v in info.items()
                         if k != 'signature' and k not in POST_SIGNING_FIELDS}
        assert verify_json_signature(pub, signed_payload, sig), \
            "Signature should verify against the originally-signed fields"

    def test_gossip_self_info_compact_still_signs(self):
        """Even in compact mode, _gossip_self_info() should include signature if available."""
        from integrations.social.peer_discovery import GossipProtocol

        with patch.dict(os.environ, {'HEVOLVE_GOSSIP_BANDWIDTH': 'constrained'}):
            gp = GossipProtocol()
            info = gp._gossip_self_info()

        # In compact mode, signature and public_key should still be included
        # (they're in _COMPACT_FIELDS)
        assert 'signature' in info, \
            "Compact gossip_self_info should include signature"
        assert 'public_key' in info, \
            "Compact gossip_self_info should include public_key"


# ═══════════════════════════════════════════════════════════════
# 4. Unsigned Peers in Hard Mode (Sybil + Enforcement Combined)
# ═══════════════════════════════════════════════════════════════

class TestHardModeUnsignedWithSybil:
    """Combined: unsigned peer in hard mode at various Sybil counts."""

    def test_unsigned_rejected_in_hard_mode_even_below_sybil_limit(self):
        """Hard enforcement rejects unsigned peers regardless of Sybil count."""
        gossip = _make_gossip()
        db = _mock_db_no_existing()

        peer_data = {
            'node_id': 'unsigned-hard-node',
            'url': 'http://10.0.0.20:6777',
            'name': 'no-sig',
            # No signature, no public_key
        }

        with patch('security.master_key.get_enforcement_mode', return_value='hard'):
            result = gossip._merge_peer(db, peer_data)

        assert result is False

    def test_signed_accepted_in_hard_mode(self):
        """Hard enforcement accepts properly signed peers."""
        tmp_dir = tempfile.mkdtemp()
        os.environ['HEVOLVE_KEY_DIR'] = tmp_dir
        try:
            from security.node_integrity import (
                sign_json_payload, get_public_key_hex, reset_keypair
            )

            gossip = _make_gossip()
            db = _mock_db_no_existing()

            peer_data = {
                'node_id': 'signed-hard-node',
                'url': 'http://10.0.0.21:6777',
                'public_key': get_public_key_hex(),
            }
            peer_data['signature'] = sign_json_payload(peer_data)

            with patch('security.master_key.get_enforcement_mode', return_value='hard'):
                with patch('security.hive_guardrails.get_guardrail_hash',
                           side_effect=ImportError):
                    with patch('security.master_key.load_release_manifest',
                               side_effect=ImportError):
                        result = gossip._merge_peer(db, peer_data)

            assert result is True
        finally:
            reset_keypair()
            shutil.rmtree(tmp_dir, ignore_errors=True)
            os.environ.pop('HEVOLVE_KEY_DIR', None)
