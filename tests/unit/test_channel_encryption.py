"""
Tests for E2E Channel Encryption — security/channel_encryption.py

Covers:
- X25519 keypair generation and persistence
- Encrypt / decrypt roundtrip (bytes and JSON)
- Forward secrecy (different ciphertexts for same plaintext)
- Tamper detection (modified ciphertext/nonce rejected)
- Envelope format and protocol versioning
- Large payload handling
- Cross-peer key mismatch rejection
"""

import json
import os
import tempfile
import pytest


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _isolate_keys(tmp_path):
    """Use temp dir for key storage so tests don't pollute agent_data."""
    os.environ['HEVOLVE_KEY_DIR'] = str(tmp_path)
    # Reset cached keypair between tests
    from security.channel_encryption import reset_keypair_cache
    reset_keypair_cache()
    yield
    reset_keypair_cache()
    os.environ.pop('HEVOLVE_KEY_DIR', None)


# ═══════════════════════════════════════════════════════════════
# Keypair Management Tests
# ═══════════════════════════════════════════════════════════════

class TestX25519Keypair:
    """Test X25519 keypair generation and persistence."""

    def test_keypair_generation(self, tmp_path):
        from security.channel_encryption import get_x25519_keypair
        private, public = get_x25519_keypair()
        assert private is not None
        assert public is not None
        assert len(public) == 32  # X25519 public key is 32 bytes

    def test_keypair_persisted(self, tmp_path):
        from security.channel_encryption import get_x25519_keypair
        get_x25519_keypair()
        priv_path = os.path.join(str(tmp_path), 'node_x25519_private.key')
        pub_path = os.path.join(str(tmp_path), 'node_x25519_public.key')
        assert os.path.isfile(priv_path)
        assert os.path.isfile(pub_path)

    def test_keypair_reload(self, tmp_path):
        """Second call loads from disk, returns same key."""
        from security.channel_encryption import get_x25519_keypair, reset_keypair_cache
        _, pub1 = get_x25519_keypair()
        reset_keypair_cache()
        _, pub2 = get_x25519_keypair()
        assert pub1 == pub2

    def test_public_hex_format(self):
        from security.channel_encryption import get_x25519_public_hex
        hex_key = get_x25519_public_hex()
        assert len(hex_key) == 64  # 32 bytes = 64 hex chars
        # Should be valid hex
        int(hex_key, 16)

    def test_reset_keypair_cache(self):
        from security.channel_encryption import (
            get_x25519_keypair, reset_keypair_cache,
            _x25519_private, _x25519_public_bytes,
        )
        get_x25519_keypair()
        reset_keypair_cache()
        import security.channel_encryption as ce
        assert ce._x25519_private is None
        assert ce._x25519_public_bytes is None


# ═══════════════════════════════════════════════════════════════
# Encrypt / Decrypt Roundtrip Tests
# ═══════════════════════════════════════════════════════════════

class TestEncryptDecrypt:
    """Test encrypt → decrypt roundtrip."""

    def test_roundtrip_bytes(self):
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer, decrypt_from_peer,
        )
        _, pub = get_x25519_keypair()
        plaintext = b'hello hart network'
        envelope = encrypt_for_peer(plaintext, pub.hex())
        result = decrypt_from_peer(envelope)
        assert result == plaintext

    def test_roundtrip_json(self):
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_json_for_peer, decrypt_json_from_peer,
        )
        _, pub = get_x25519_keypair()
        payload = {'goal_id': 'g1', 'tasks': [{'id': 't1', 'desc': 'test'}]}
        envelope = encrypt_json_for_peer(payload, pub.hex())
        result = decrypt_json_from_peer(envelope)
        assert result == payload

    def test_roundtrip_empty_bytes(self):
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer, decrypt_from_peer,
        )
        _, pub = get_x25519_keypair()
        envelope = encrypt_for_peer(b'', pub.hex())
        result = decrypt_from_peer(envelope)
        assert result == b''

    def test_roundtrip_unicode(self):
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_json_for_peer, decrypt_json_from_peer,
        )
        _, pub = get_x25519_keypair()
        payload = {'message': 'Hello \u2764\ufe0f from HART!'}
        envelope = encrypt_json_for_peer(payload, pub.hex())
        result = decrypt_json_from_peer(envelope)
        assert result == payload

    def test_large_payload(self):
        """1MB payload encrypts/decrypts correctly."""
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer, decrypt_from_peer,
        )
        _, pub = get_x25519_keypair()
        plaintext = os.urandom(1024 * 1024)  # 1MB
        envelope = encrypt_for_peer(plaintext, pub.hex())
        result = decrypt_from_peer(envelope)
        assert result == plaintext


# ═══════════════════════════════════════════════════════════════
# Security Properties Tests
# ═══════════════════════════════════════════════════════════════

class TestSecurityProperties:
    """Test forward secrecy, tamper detection, key mismatch."""

    def test_forward_secrecy(self):
        """Two encryptions of same plaintext produce different ciphertexts."""
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer,
        )
        _, pub = get_x25519_keypair()
        plaintext = b'same data twice'
        env1 = encrypt_for_peer(plaintext, pub.hex())
        env2 = encrypt_for_peer(plaintext, pub.hex())
        # Ephemeral keys should differ
        assert env1['eph'] != env2['eph']
        # Nonces should differ
        assert env1['nonce'] != env2['nonce']
        # Ciphertexts should differ
        assert env1['ct'] != env2['ct']

    def test_wrong_key_fails(self):
        """Decrypting with wrong key returns None."""
        from security.channel_encryption import (
            encrypt_for_peer, decrypt_from_peer, reset_keypair_cache,
        )
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives import serialization

        # Generate a different keypair (not our node's key)
        other_key = X25519PrivateKey.generate()
        other_pub = other_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

        # Encrypt for the "other" key
        envelope = encrypt_for_peer(b'secret', other_pub.hex())

        # Try to decrypt with our node's key (which is different)
        result = decrypt_from_peer(envelope)
        assert result is None

    def test_tampered_ciphertext_fails(self):
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer, decrypt_from_peer,
        )
        _, pub = get_x25519_keypair()
        envelope = encrypt_for_peer(b'sensitive data', pub.hex())
        # Tamper with ciphertext
        ct_bytes = bytes.fromhex(envelope['ct'])
        tampered = bytes([ct_bytes[0] ^ 0xff]) + ct_bytes[1:]
        envelope['ct'] = tampered.hex()
        result = decrypt_from_peer(envelope)
        assert result is None

    def test_tampered_nonce_fails(self):
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer, decrypt_from_peer,
        )
        _, pub = get_x25519_keypair()
        envelope = encrypt_for_peer(b'sensitive data', pub.hex())
        # Tamper with nonce
        nonce_bytes = bytes.fromhex(envelope['nonce'])
        tampered = bytes([nonce_bytes[0] ^ 0xff]) + nonce_bytes[1:]
        envelope['nonce'] = tampered.hex()
        result = decrypt_from_peer(envelope)
        assert result is None

    def test_tampered_ephemeral_key_fails(self):
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer, decrypt_from_peer,
        )
        _, pub = get_x25519_keypair()
        envelope = encrypt_for_peer(b'sensitive data', pub.hex())
        # Tamper with ephemeral public key
        eph_bytes = bytes.fromhex(envelope['eph'])
        tampered = bytes([eph_bytes[0] ^ 0xff]) + eph_bytes[1:]
        envelope['eph'] = tampered.hex()
        result = decrypt_from_peer(envelope)
        assert result is None


# ═══════════════════════════════════════════════════════════════
# Envelope Format Tests
# ═══════════════════════════════════════════════════════════════

class TestEnvelopeFormat:
    """Test envelope structure and validation."""

    def test_envelope_has_required_keys(self):
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer,
        )
        _, pub = get_x25519_keypair()
        envelope = encrypt_for_peer(b'test', pub.hex())
        assert 'eph' in envelope
        assert 'nonce' in envelope
        assert 'ct' in envelope
        assert 'v' in envelope

    def test_protocol_version(self):
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer,
        )
        _, pub = get_x25519_keypair()
        envelope = encrypt_for_peer(b'test', pub.hex())
        assert envelope['v'] == 1

    def test_is_encrypted_envelope_positive(self):
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer, is_encrypted_envelope,
        )
        _, pub = get_x25519_keypair()
        envelope = encrypt_for_peer(b'test', pub.hex())
        assert is_encrypted_envelope(envelope) is True

    def test_is_encrypted_envelope_negative(self):
        from security.channel_encryption import is_encrypted_envelope
        assert is_encrypted_envelope({}) is False
        assert is_encrypted_envelope({'eph': 'x', 'nonce': 'y'}) is False
        assert is_encrypted_envelope({'eph': 'x', 'nonce': 'y', 'ct': 'z', 'v': 2}) is False
        assert is_encrypted_envelope('not a dict') is False

    def test_envelope_hex_encoding(self):
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer,
        )
        _, pub = get_x25519_keypair()
        envelope = encrypt_for_peer(b'test', pub.hex())
        # All hex fields should be valid hex
        bytes.fromhex(envelope['eph'])
        bytes.fromhex(envelope['nonce'])
        bytes.fromhex(envelope['ct'])

    def test_nonce_is_12_bytes(self):
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer,
        )
        _, pub = get_x25519_keypair()
        envelope = encrypt_for_peer(b'test', pub.hex())
        assert len(bytes.fromhex(envelope['nonce'])) == 12

    def test_ephemeral_key_is_32_bytes(self):
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer,
        )
        _, pub = get_x25519_keypair()
        envelope = encrypt_for_peer(b'test', pub.hex())
        assert len(bytes.fromhex(envelope['eph'])) == 32

    def test_envelope_json_serializable(self):
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer,
        )
        _, pub = get_x25519_keypair()
        envelope = encrypt_for_peer(b'test data', pub.hex())
        # Must be JSON-serializable for HTTP transport
        serialized = json.dumps(envelope)
        deserialized = json.loads(serialized)
        assert deserialized == envelope


# ═══════════════════════════════════════════════════════════════
# Cross-Peer Tests
# ═══════════════════════════════════════════════════════════════

class TestCrossPeer:
    """Simulate two different nodes communicating."""

    def test_two_node_communication(self, tmp_path):
        """Node A encrypts for Node B, Node B decrypts successfully."""
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        from security.channel_encryption import (
            encrypt_for_peer, _derive_aes_key,
        )
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes

        # Node B's keypair
        b_private = X25519PrivateKey.generate()
        b_public = b_private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

        # Node A encrypts for Node B
        plaintext = b'distributed task payload'
        envelope = encrypt_for_peer(plaintext, b_public.hex())

        # Node B decrypts manually (simulating different node)
        eph_pub = X25519PublicKey.from_public_bytes(bytes.fromhex(envelope['eph']))
        nonce = bytes.fromhex(envelope['nonce'])
        ciphertext = bytes.fromhex(envelope['ct'])

        shared_secret = b_private.exchange(eph_pub)
        aes_key = HKDF(
            algorithm=hashes.SHA256(), length=32,
            salt=nonce, info=b'hart-e2e-v1',
        ).derive(shared_secret)

        result = AESGCM(aes_key).decrypt(nonce, ciphertext, None)
        assert result == plaintext
