"""
E2E Encrypted Channels — Noise-like protocol for inter-node communication.

When nodes exchange tasks, sync data, or gossip, payloads are encrypted
so that neither network observers nor the compute-hosting node can read
the task owner's data.

Uses: X25519 key exchange + HKDF + AES-256-GCM (from ``cryptography`` library,
already a dependency via node_integrity.py).

Flow:
  1. Node A wants to send encrypted data to Node B
  2. A generates ephemeral X25519 keypair
  3. A derives shared secret: ECDH(ephemeral_private, B_x25519_public)
  4. A derives AES key: HKDF(shared_secret, salt=nonce, info=b'hart-e2e-v1')
  5. A encrypts payload with AES-256-GCM(key, nonce, plaintext)
  6. A sends: {eph, nonce, ct, v}  (all hex-encoded)
  7. B derives same shared secret: ECDH(B_x25519_private, ephemeral_public)
  8. B derives same AES key, decrypts payload

Forward secrecy: ephemeral key discarded after encryption. Compromise of
node's long-term key cannot decrypt past sessions.
"""

import json
import logging
import os
from typing import Dict, Optional, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger('hevolve_security')

# Protocol identifier embedded in HKDF info — bump on breaking changes
_PROTOCOL_INFO = b'hart-e2e-v1'
_PROTOCOL_VERSION = 1

# ── X25519 Keypair Management ──────────────────────────────────────

_x25519_private: Optional[X25519PrivateKey] = None
_x25519_public_bytes: Optional[bytes] = None


def _resolve_key_dir() -> str:
    explicit = os.environ.get('HEVOLVE_KEY_DIR')
    if explicit:
        return explicit
    db_path = os.environ.get('HEVOLVE_DB_PATH', '')
    if db_path and db_path != ':memory:' and os.path.isabs(db_path):
        return os.path.dirname(db_path)
    return 'agent_data'


def get_x25519_keypair() -> Tuple[X25519PrivateKey, bytes]:
    """Get or create X25519 keypair for ECDH key exchange.

    The keypair is persisted alongside the Ed25519 identity keys.
    Separate from Ed25519 because Ed25519 (twisted Edwards) and
    X25519 (Montgomery) operate on different curves.
    """
    global _x25519_private, _x25519_public_bytes
    if _x25519_private is not None:
        return _x25519_private, _x25519_public_bytes

    key_dir = _resolve_key_dir()
    x_priv_path = os.path.join(key_dir, 'node_x25519_private.key')
    x_pub_path = os.path.join(key_dir, 'node_x25519_public.key')

    if os.path.isfile(x_priv_path):
        try:
            with open(x_priv_path, 'rb') as f:
                raw = f.read()
            # Decrypt at rest — auto-detects encrypted vs plaintext
            try:
                from security.crypto import decrypt_data
                raw = decrypt_data(raw)
            except ImportError:
                pass
            _x25519_private = X25519PrivateKey.from_private_bytes(raw)
            logger.info("X25519 keypair loaded from %s", key_dir)
        except Exception as e:
            logger.warning("Failed to load X25519 key, regenerating: %s", e)
            _x25519_private = None

    if _x25519_private is None:
        _x25519_private = X25519PrivateKey.generate()
        os.makedirs(key_dir, exist_ok=True)
        raw = _x25519_private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        # Encrypt at rest when HEVOLVE_DATA_KEY is configured
        try:
            from security.crypto import encrypt_data
            with open(x_priv_path, 'wb') as f:
                f.write(encrypt_data(raw))
        except ImportError:
            with open(x_priv_path, 'wb') as f:
                f.write(raw)
        logger.info("X25519 keypair generated and saved to %s", key_dir)

    _x25519_public_bytes = _x25519_private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    # Persist public key for external use (beacon inclusion)
    if not os.path.isfile(x_pub_path):
        with open(x_pub_path, 'wb') as f:
            f.write(_x25519_public_bytes)

    return _x25519_private, _x25519_public_bytes


def get_x25519_public_hex() -> str:
    """Return hex-encoded X25519 public key for inclusion in gossip beacons."""
    _, pub = get_x25519_keypair()
    return pub.hex()


def reset_keypair_cache():
    """Reset the cached keypair (for testing)."""
    global _x25519_private, _x25519_public_bytes
    _x25519_private = None
    _x25519_public_bytes = None


# ── Encrypt / Decrypt ──────────────────────────────────────────────

def _derive_aes_key(shared_secret: bytes, nonce: bytes) -> bytes:
    """Derive a 256-bit AES key from ECDH shared secret via HKDF."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=nonce,
        info=_PROTOCOL_INFO,
    ).derive(shared_secret)


def encrypt_for_peer(plaintext: bytes,
                     peer_x25519_public_hex: str) -> Dict[str, str]:
    """Encrypt data for a specific peer using ephemeral ECDH + AES-256-GCM.

    Returns envelope dict with hex-encoded fields:
      - eph: ephemeral X25519 public key (32 bytes)
      - nonce: random 12-byte nonce
      - ct: ciphertext + GCM tag
      - v: protocol version

    Forward secrecy: ephemeral key is discarded after this call.
    """
    peer_pub = X25519PublicKey.from_public_bytes(
        bytes.fromhex(peer_x25519_public_hex))

    # Ephemeral keypair — used once and discarded
    ephemeral = X25519PrivateKey.generate()
    shared_secret = ephemeral.exchange(peer_pub)

    nonce = os.urandom(12)
    aes_key = _derive_aes_key(shared_secret, nonce)

    ciphertext = AESGCM(aes_key).encrypt(nonce, plaintext, None)

    eph_pub = ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )

    return {
        'eph': eph_pub.hex(),
        'nonce': nonce.hex(),
        'ct': ciphertext.hex(),
        'v': _PROTOCOL_VERSION,
    }


def decrypt_from_peer(envelope: Dict[str, str]) -> Optional[bytes]:
    """Decrypt an envelope sent by a peer.

    Uses our long-term X25519 private key + the sender's ephemeral public key
    to reconstruct the shared secret and decrypt.

    Returns plaintext bytes, or None if decryption fails.
    """
    try:
        eph_pub = X25519PublicKey.from_public_bytes(
            bytes.fromhex(envelope['eph']))
        nonce = bytes.fromhex(envelope['nonce'])
        ciphertext = bytes.fromhex(envelope['ct'])

        our_private, _ = get_x25519_keypair()
        shared_secret = our_private.exchange(eph_pub)

        aes_key = _derive_aes_key(shared_secret, nonce)

        return AESGCM(aes_key).decrypt(nonce, ciphertext, None)
    except Exception as e:
        logger.warning("E2E decrypt failed: %s", e)
        return None


# ── JSON Convenience Wrappers ──────────────────────────────────────

def encrypt_json_for_peer(payload: dict,
                          peer_x25519_public_hex: str) -> Dict[str, str]:
    """Encrypt a JSON-serializable dict for a peer."""
    plaintext = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    return encrypt_for_peer(plaintext, peer_x25519_public_hex)


def decrypt_json_from_peer(envelope: Dict[str, str]) -> Optional[dict]:
    """Decrypt an envelope and parse as JSON."""
    plaintext = decrypt_from_peer(envelope)
    if plaintext is None:
        return None
    try:
        return json.loads(plaintext)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("E2E JSON decode failed: %s", e)
        return None


# ── Utility: Check if payload is an encrypted envelope ─────────────

def is_encrypted_envelope(data: dict) -> bool:
    """Check if a dict looks like an E2E encrypted envelope."""
    return (isinstance(data, dict)
            and 'eph' in data
            and 'nonce' in data
            and 'ct' in data
            and data.get('v') == _PROTOCOL_VERSION)
