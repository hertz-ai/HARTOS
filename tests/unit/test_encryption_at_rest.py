"""
Tests for Encryption at Rest — security/crypto.py wiring across subsystems.

Covers:
- crypto.py Fernet encrypt/decrypt roundtrip
- encrypt_json_file / decrypt_json_file with auto-detect
- Resonance profiles encrypted on save, decrypted on load
- Instruction queue encrypted on save, decrypted on load
- Node Ed25519 private key encrypted at rest
- X25519 private key encrypted at rest
- Seamless migration: plaintext files read correctly, encrypted on next write
- No encryption when HEVOLVE_DATA_KEY is absent (graceful fallback)
"""

import json
import os
import tempfile
import pytest

from cryptography.fernet import Fernet


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def data_key():
    """Generate a valid Fernet key for testing."""
    return Fernet.generate_key().decode()


@pytest.fixture
def encrypted_env(data_key, tmp_path):
    """Set HEVOLVE_DATA_KEY and isolate key/data dirs."""
    os.environ['HEVOLVE_DATA_KEY'] = data_key
    os.environ['HEVOLVE_KEY_DIR'] = str(tmp_path)
    yield data_key
    os.environ.pop('HEVOLVE_DATA_KEY', None)
    os.environ.pop('HEVOLVE_KEY_DIR', None)


@pytest.fixture
def no_encryption_env(tmp_path):
    """Ensure no data key is set."""
    os.environ.pop('HEVOLVE_DATA_KEY', None)
    os.environ['HEVOLVE_KEY_DIR'] = str(tmp_path)
    yield
    os.environ.pop('HEVOLVE_KEY_DIR', None)


# ═══════════════════════════════════════════════════════════════
# crypto.py Core Tests
# ═══════════════════════════════════════════════════════════════

class TestCryptoCore:
    """Test security/crypto.py primitives."""

    def test_encrypt_decrypt_roundtrip(self, encrypted_env):
        from security.crypto import encrypt_data, decrypt_data
        plaintext = b'hello encryption at rest'
        encrypted = encrypt_data(plaintext)
        assert encrypted != plaintext
        assert encrypted.startswith(b'gAAAAA')
        decrypted = decrypt_data(encrypted)
        assert decrypted == plaintext

    def test_encrypt_string_roundtrip(self, encrypted_env):
        from security.crypto import encrypt_data, decrypt_data
        plaintext = 'string input'
        encrypted = encrypt_data(plaintext)
        decrypted = decrypt_data(encrypted)
        assert decrypted == plaintext.encode()

    def test_no_key_returns_plaintext(self, no_encryption_env):
        from security.crypto import encrypt_data, decrypt_data
        plaintext = b'not encrypted'
        result = encrypt_data(plaintext)
        assert result == plaintext
        decrypted = decrypt_data(result)
        assert decrypted == plaintext

    def test_auto_detect_plaintext(self, encrypted_env):
        """decrypt_data returns plaintext bytes as-is when not Fernet-prefixed."""
        from security.crypto import decrypt_data
        plaintext = b'just plain text, no Fernet prefix'
        result = decrypt_data(plaintext)
        assert result == plaintext

    def test_generate_data_key(self):
        from security.crypto import generate_data_key
        key = generate_data_key()
        assert isinstance(key, str)
        # Valid Fernet key
        Fernet(key.encode())

    def test_encrypt_json_file_roundtrip(self, encrypted_env, tmp_path):
        from security.crypto import encrypt_json_file, decrypt_json_file
        data = {'user_id': '123', 'tuning': {'warmth': 0.8}}
        path = str(tmp_path / 'test.json')
        encrypt_json_file(path, data)

        # File should contain encrypted bytes (not plaintext JSON)
        with open(path, 'rb') as f:
            raw = f.read()
        assert raw.startswith(b'gAAAAA')

        # Decrypt should return original dict
        result = decrypt_json_file(path)
        assert result == data

    def test_decrypt_json_file_reads_plaintext(self, encrypted_env, tmp_path):
        """Plaintext JSON files are read without issue (migration path)."""
        from security.crypto import decrypt_json_file
        data = {'legacy': True, 'value': 42}
        path = str(tmp_path / 'plain.json')
        with open(path, 'w') as f:
            json.dump(data, f)

        result = decrypt_json_file(path)
        assert result == data

    def test_decrypt_json_file_nonexistent(self, encrypted_env, tmp_path):
        from security.crypto import decrypt_json_file
        result = decrypt_json_file(str(tmp_path / 'nope.json'))
        assert result is None

    def test_encrypt_json_file_no_key_writes_plaintext(self, no_encryption_env, tmp_path):
        from security.crypto import encrypt_json_file, decrypt_json_file
        data = {'plain': True}
        path = str(tmp_path / 'test.json')
        encrypt_json_file(path, data)

        # Should be readable as plain JSON
        with open(path, 'r') as f:
            result = json.load(f)
        assert result == data


# ═══════════════════════════════════════════════════════════════
# Resonance Profile Encryption Tests
# ═══════════════════════════════════════════════════════════════

class TestResonanceProfileEncryption:
    """Test resonance profile encrypted at rest."""

    def test_save_encrypted_load_decrypted(self, encrypted_env, tmp_path):
        from core.resonance_profile import (
            UserResonanceProfile, save_resonance_profile,
            load_resonance_profile,
        )
        profile = UserResonanceProfile(user_id='enc_user')
        profile.set_tuning('warmth_score', 0.9)
        profile.set_tuning('humor_receptivity', 0.7)

        save_resonance_profile(profile, base_dir=str(tmp_path))

        # File should be encrypted
        path = os.path.join(str(tmp_path), 'enc_user_resonance.json')
        with open(path, 'rb') as f:
            raw = f.read()
        assert raw.startswith(b'gAAAAA')

        # Load should decrypt and return correct profile
        loaded = load_resonance_profile('enc_user', base_dir=str(tmp_path))
        assert loaded is not None
        assert loaded.user_id == 'enc_user'
        assert loaded.get_tuning('warmth_score') == 0.9
        assert loaded.get_tuning('humor_receptivity') == 0.7

    def test_save_plaintext_when_no_key(self, no_encryption_env, tmp_path):
        from core.resonance_profile import (
            UserResonanceProfile, save_resonance_profile,
            load_resonance_profile,
        )
        profile = UserResonanceProfile(user_id='plain_user')
        save_resonance_profile(profile, base_dir=str(tmp_path))

        # File should be readable plaintext JSON
        path = os.path.join(str(tmp_path), 'plain_user_resonance.json')
        with open(path, 'r') as f:
            data = json.load(f)
        assert data['user_id'] == 'plain_user'

        loaded = load_resonance_profile('plain_user', base_dir=str(tmp_path))
        assert loaded is not None
        assert loaded.user_id == 'plain_user'

    def test_load_plaintext_with_key_set(self, encrypted_env, tmp_path):
        """Seamless migration: plaintext file read even when key is set."""
        from core.resonance_profile import (
            UserResonanceProfile, load_resonance_profile,
        )
        # Write plaintext manually (simulating pre-encryption data)
        profile = UserResonanceProfile(user_id='migrate_user')
        path = os.path.join(str(tmp_path), 'migrate_user_resonance.json')
        with open(path, 'w') as f:
            json.dump(profile.to_dict(), f)

        loaded = load_resonance_profile('migrate_user', base_dir=str(tmp_path))
        assert loaded is not None
        assert loaded.user_id == 'migrate_user'

    def test_biometric_embeddings_encrypted(self, encrypted_env, tmp_path):
        """Biometric data (face/voice embeddings) is encrypted at rest."""
        from core.resonance_profile import (
            UserResonanceProfile, save_resonance_profile,
            load_resonance_profile,
        )
        profile = UserResonanceProfile(user_id='bio_user')
        profile.face_embedding = [0.1, 0.2, 0.3, 0.4]
        profile.voice_embedding = [0.5, 0.6, 0.7, 0.8]
        profile.face_enrollment_count = 3
        profile.voice_enrollment_count = 2

        save_resonance_profile(profile, base_dir=str(tmp_path))

        # Verify encrypted on disk
        path = os.path.join(str(tmp_path), 'bio_user_resonance.json')
        with open(path, 'rb') as f:
            raw = f.read()
        assert b'face_embedding' not in raw  # Not readable in ciphertext

        # Verify decrypted correctly
        loaded = load_resonance_profile('bio_user', base_dir=str(tmp_path))
        assert loaded.face_embedding == [0.1, 0.2, 0.3, 0.4]
        assert loaded.voice_embedding == [0.5, 0.6, 0.7, 0.8]
        assert loaded.face_enrollment_count == 3

    def test_nonexistent_profile_returns_none(self, encrypted_env, tmp_path):
        from core.resonance_profile import load_resonance_profile
        result = load_resonance_profile('ghost_user', base_dir=str(tmp_path))
        assert result is None


# ═══════════════════════════════════════════════════════════════
# Instruction Queue Encryption Tests
# ═══════════════════════════════════════════════════════════════

class TestInstructionQueueEncryption:
    """Test instruction queue encrypted at rest."""

    def test_save_encrypted_load_decrypted(self, encrypted_env, tmp_path, monkeypatch):
        import integrations.agent_engine.instruction_queue as iq_mod
        monkeypatch.setattr(iq_mod, '_QUEUE_DIR', str(tmp_path))

        queue = iq_mod.InstructionQueue('enc_test_user')
        queue.enqueue('Do something sensitive')
        queue.enqueue('Process payment data')

        # File should be encrypted
        path = os.path.join(str(tmp_path), 'enc_test_user_queue.json')
        assert os.path.exists(path)
        with open(path, 'rb') as f:
            raw = f.read()
        assert raw.startswith(b'gAAAAA')

        # Reload queue — should decrypt and restore instructions
        queue2 = iq_mod.InstructionQueue('enc_test_user')
        pending = queue2.get_pending()
        assert len(pending) == 2
        texts = [p.text for p in pending]
        assert 'Do something sensitive' in texts
        assert 'Process payment data' in texts

    def test_save_plaintext_when_no_key(self, no_encryption_env, tmp_path, monkeypatch):
        import integrations.agent_engine.instruction_queue as iq_mod
        monkeypatch.setattr(iq_mod, '_QUEUE_DIR', str(tmp_path))

        queue = iq_mod.InstructionQueue('plain_test_user')
        queue.enqueue('Unencrypted instruction')

        path = os.path.join(str(tmp_path), 'plain_test_user_queue.json')
        with open(path, 'r') as f:
            data = json.load(f)
        assert data['user_id'] == 'plain_test_user'
        assert len(data['instructions']) == 1

    def test_load_plaintext_with_key_set(self, encrypted_env, tmp_path, monkeypatch):
        """Migration: load plaintext queue when encryption key is now set."""
        import integrations.agent_engine.instruction_queue as iq_mod
        monkeypatch.setattr(iq_mod, '_QUEUE_DIR', str(tmp_path))

        # Write plaintext manually
        path = os.path.join(str(tmp_path), 'legacy_user_queue.json')
        data = {
            'user_id': 'legacy_user',
            'updated_at': '2026-01-01T00:00:00',
            'instructions': [{
                'id': 'abc123', 'user_id': 'legacy_user',
                'text': 'old instruction', 'status': 'queued',
                'created_at': '', 'updated_at': '', 'priority': 5,
                'tags': [], 'context': {}, 'related_goal_id': None,
                'batch_id': None, 'result': None, 'error': None,
            }],
        }
        with open(path, 'w') as f:
            json.dump(data, f)

        queue = iq_mod.InstructionQueue('legacy_user')
        pending = queue.get_pending()
        assert len(pending) == 1
        assert pending[0].text == 'old instruction'

    def test_instruction_text_not_visible_in_encrypted_file(
            self, encrypted_env, tmp_path, monkeypatch):
        """Verify instruction text is not visible in encrypted file bytes."""
        import integrations.agent_engine.instruction_queue as iq_mod
        monkeypatch.setattr(iq_mod, '_QUEUE_DIR', str(tmp_path))

        queue = iq_mod.InstructionQueue('secret_user')
        queue.enqueue('My secret API key is sk-12345')

        path = os.path.join(str(tmp_path), 'secret_user_queue.json')
        with open(path, 'rb') as f:
            raw = f.read()
        assert b'secret API key' not in raw
        assert b'sk-12345' not in raw


# ═══════════════════════════════════════════════════════════════
# Node Identity Key Encryption Tests
# ═══════════════════════════════════════════════════════════════

class TestNodeKeyEncryption:
    """Test Ed25519 node private key encrypted at rest."""

    def test_ed25519_key_encrypted_on_disk(self, encrypted_env, tmp_path, monkeypatch):
        import security.node_integrity as ni
        monkeypatch.setattr(ni, '_KEY_DIR', str(tmp_path))
        ni._private_key = None
        ni._public_key = None

        priv, pub = ni.get_or_create_keypair()
        assert priv is not None
        assert pub is not None

        # Private key file should be encrypted
        priv_path = os.path.join(str(tmp_path), 'node_private_key.pem')
        with open(priv_path, 'rb') as f:
            raw = f.read()
        assert raw.startswith(b'gAAAAA')
        assert b'PRIVATE KEY' not in raw

        # Public key stays plaintext (it's public!)
        pub_path = os.path.join(str(tmp_path), 'node_public_key.pem')
        with open(pub_path, 'rb') as f:
            pub_raw = f.read()
        assert b'PUBLIC KEY' in pub_raw

    def test_ed25519_key_reload_after_encryption(self, encrypted_env, tmp_path, monkeypatch):
        import security.node_integrity as ni
        monkeypatch.setattr(ni, '_KEY_DIR', str(tmp_path))
        ni._private_key = None
        ni._public_key = None

        _, pub1 = ni.get_or_create_keypair()
        pub1_hex = pub1.public_bytes(
            encoding=ni.serialization.Encoding.Raw,
            format=ni.serialization.PublicFormat.Raw,
        ).hex()

        # Clear cache and reload from encrypted file
        ni._private_key = None
        ni._public_key = None
        _, pub2 = ni.get_or_create_keypair()
        pub2_hex = pub2.public_bytes(
            encoding=ni.serialization.Encoding.Raw,
            format=ni.serialization.PublicFormat.Raw,
        ).hex()

        assert pub1_hex == pub2_hex

    def test_ed25519_key_plaintext_when_no_key(self, no_encryption_env, tmp_path, monkeypatch):
        import security.node_integrity as ni
        monkeypatch.setattr(ni, '_KEY_DIR', str(tmp_path))
        ni._private_key = None
        ni._public_key = None

        ni.get_or_create_keypair()
        priv_path = os.path.join(str(tmp_path), 'node_private_key.pem')
        with open(priv_path, 'rb') as f:
            raw = f.read()
        assert b'PRIVATE KEY' in raw  # Readable PEM

    def test_ed25519_plaintext_migration(self, encrypted_env, tmp_path, monkeypatch):
        """Load existing plaintext PEM key when encryption is now enabled."""
        import security.node_integrity as ni
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization

        monkeypatch.setattr(ni, '_KEY_DIR', str(tmp_path))
        ni._private_key = None
        ni._public_key = None

        # Write a plaintext PEM key (pre-encryption)
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        (tmp_path / 'node_private_key.pem').write_bytes(pem)
        (tmp_path / 'node_public_key.pem').write_bytes(pub_pem)

        expected_pub = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()

        _, loaded_pub = ni.get_or_create_keypair()
        loaded_hex = loaded_pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()

        assert loaded_hex == expected_pub


# ═══════════════════════════════════════════════════════════════
# X25519 Key Encryption Tests
# ═══════════════════════════════════════════════════════════════

class TestX25519KeyEncryption:
    """Test X25519 key encrypted at rest."""

    def test_x25519_key_encrypted_on_disk(self, encrypted_env, tmp_path):
        from security.channel_encryption import (
            get_x25519_keypair, reset_keypair_cache,
        )
        reset_keypair_cache()
        private, public = get_x25519_keypair()

        priv_path = os.path.join(str(tmp_path), 'node_x25519_private.key')
        with open(priv_path, 'rb') as f:
            raw = f.read()
        # Should be Fernet-encrypted (not raw 32 bytes)
        assert raw.startswith(b'gAAAAA')
        assert len(raw) > 32  # Fernet ciphertext is longer than 32 bytes

        # Public key stays plaintext
        pub_path = os.path.join(str(tmp_path), 'node_x25519_public.key')
        with open(pub_path, 'rb') as f:
            pub_raw = f.read()
        assert len(pub_raw) == 32  # Raw 32-byte X25519 public key

        reset_keypair_cache()

    def test_x25519_key_reload_after_encryption(self, encrypted_env, tmp_path):
        from security.channel_encryption import (
            get_x25519_keypair, reset_keypair_cache,
        )
        reset_keypair_cache()
        _, pub1 = get_x25519_keypair()

        reset_keypair_cache()
        _, pub2 = get_x25519_keypair()

        assert pub1 == pub2
        reset_keypair_cache()

    def test_x25519_plaintext_when_no_key(self, no_encryption_env, tmp_path):
        from security.channel_encryption import (
            get_x25519_keypair, reset_keypair_cache,
        )
        reset_keypair_cache()
        get_x25519_keypair()

        priv_path = os.path.join(str(tmp_path), 'node_x25519_private.key')
        with open(priv_path, 'rb') as f:
            raw = f.read()
        assert len(raw) == 32  # Raw unencrypted key bytes
        reset_keypair_cache()

    def test_x25519_plaintext_migration(self, encrypted_env, tmp_path):
        """Load existing plaintext key file when encryption is now enabled."""
        from security.channel_encryption import (
            get_x25519_keypair, reset_keypair_cache,
        )
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives import serialization

        # Write raw 32-byte key (pre-encryption format)
        key = X25519PrivateKey.generate()
        raw_bytes = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        expected_pub = key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        (tmp_path / 'node_x25519_private.key').write_bytes(raw_bytes)

        reset_keypair_cache()
        _, loaded_pub = get_x25519_keypair()
        assert loaded_pub == expected_pub
        reset_keypair_cache()

    def test_encrypted_key_still_works_for_e2e(self, encrypted_env, tmp_path):
        """Full E2E encrypt/decrypt still works when key is encrypted at rest."""
        from security.channel_encryption import (
            get_x25519_keypair, encrypt_for_peer, decrypt_from_peer,
            reset_keypair_cache,
        )
        reset_keypair_cache()
        _, pub = get_x25519_keypair()
        plaintext = b'message via encrypted-at-rest key'
        envelope = encrypt_for_peer(plaintext, pub.hex())
        result = decrypt_from_peer(envelope)
        assert result == plaintext
        reset_keypair_cache()


# ═══════════════════════════════════════════════════════════════
# A2ACrypto Session Tests
# ═══════════════════════════════════════════════════════════════

class TestA2ACrypto:
    """Test A2A session crypto (independent of at-rest key)."""

    def test_session_roundtrip(self):
        from security.crypto import A2ACrypto
        session = A2ACrypto()
        plaintext = 'agent task payload'
        encrypted = session.encrypt_message(plaintext)
        decrypted = session.decrypt_message(encrypted)
        assert decrypted == plaintext

    def test_session_payload_roundtrip(self):
        from security.crypto import A2ACrypto
        session = A2ACrypto()
        payload = {'action': 'execute', 'args': [1, 2, 3]}
        encrypted = session.encrypt_payload(payload)
        decrypted = session.decrypt_payload(encrypted)
        assert decrypted == payload

    def test_session_key_sharing(self):
        """Two A2ACrypto instances with same key can communicate."""
        from security.crypto import A2ACrypto
        session_a = A2ACrypto()
        session_b = A2ACrypto(session_key=session_a.session_key)
        encrypted = session_a.encrypt_message('shared secret')
        decrypted = session_b.decrypt_message(encrypted)
        assert decrypted == 'shared secret'

    def test_wrong_session_key_fails(self):
        from security.crypto import A2ACrypto
        session_a = A2ACrypto()
        session_b = A2ACrypto()  # Different key
        encrypted = session_a.encrypt_message('secret')
        with pytest.raises(ValueError, match='Decryption failed'):
            session_b.decrypt_message(encrypted)


# ═══════════════════════════════════════════════════════════════
# Cross-Module Integration Tests
# ═══════════════════════════════════════════════════════════════

class TestEncryptionAtRestIntegration:
    """Cross-module encryption at rest integration tests."""

    def test_encrypted_resonance_not_readable_as_text(
            self, encrypted_env, tmp_path):
        """Encrypted resonance file cannot be parsed as JSON without key."""
        from core.resonance_profile import (
            UserResonanceProfile, save_resonance_profile,
        )
        profile = UserResonanceProfile(user_id='secret_user')
        profile.set_tuning('warmth_score', 0.95)
        save_resonance_profile(profile, base_dir=str(tmp_path))

        path = os.path.join(str(tmp_path), 'secret_user_resonance.json')
        with open(path, 'rb') as f:
            raw = f.read()
        # Should NOT be parseable as JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw.decode('utf-8', errors='replace'))

    def test_different_keys_cannot_decrypt(self, tmp_path):
        """Data encrypted with one key cannot be decrypted with another."""
        from security.crypto import encrypt_json_file, decrypt_json_file

        # Encrypt with key 1
        key1 = Fernet.generate_key().decode()
        os.environ['HEVOLVE_DATA_KEY'] = key1
        path = str(tmp_path / 'secret.json')
        encrypt_json_file(path, {'secret': 'value'})

        # Try to decrypt with key 2
        key2 = Fernet.generate_key().decode()
        os.environ['HEVOLVE_DATA_KEY'] = key2
        result = decrypt_json_file(path)
        # Should fail gracefully (returns None or raw)
        # decrypt_json_file falls back to plain text read, which will also fail
        # because the file is binary Fernet data
        assert result is None or result != {'secret': 'value'}

        os.environ.pop('HEVOLVE_DATA_KEY', None)

    def test_hive_data_unaffected_by_encryption(self, encrypted_env, tmp_path):
        """Encryption at rest is boundary-only — in-memory data stays plaintext.

        This verifies the user's requirement: encryption should NOT affect
        learnability, inference, training, agent reasoning, or any hive
        functionalities.
        """
        from core.resonance_profile import (
            UserResonanceProfile, save_resonance_profile,
            load_resonance_profile,
        )
        # Create profile with tuning data (used for hive federation)
        profile = UserResonanceProfile(user_id='hive_user')
        profile.set_tuning('formality_score', 0.3)
        profile.set_tuning('technical_depth', 0.9)
        profile.total_interactions = 42
        profile.resonance_confidence = 0.85
        profile.topic_preferences = {'coding': 0.9, 'music': 0.4}

        # Save (encrypted on disk)
        save_resonance_profile(profile, base_dir=str(tmp_path))

        # Load (decrypted to plaintext in memory)
        loaded = load_resonance_profile('hive_user', base_dir=str(tmp_path))

        # All tuning data is available in memory — hive can use it
        assert loaded.get_tuning('formality_score') == 0.3
        assert loaded.get_tuning('technical_depth') == 0.9
        assert loaded.total_interactions == 42
        assert loaded.resonance_confidence == 0.85
        assert loaded.topic_preferences == {'coding': 0.9, 'music': 0.4}

        # to_dict() works (used for federation delta export)
        delta = loaded.to_dict()
        assert delta['tuning']['formality_score'] == 0.3
        assert delta['topic_preferences']['coding'] == 0.9
