"""
Key derivation for HevolveArmor runtime.

Integrates with HARTOS's existing key hierarchy:
  1. Ed25519 node private key (security/node_integrity.py)
  2. HEVOLVE_DATA_KEY env var (security/crypto.py Fernet key)
  3. Tier certificate signing key
  4. Passphrase fallback

The derived key is deterministic for a given node identity — the same
node always derives the same AES key, enabling pre-encrypted bundles
that work on any node with a valid certificate chain.

For OS-level distribution (deploy/distro), the key is derived from the
master public key + tier, making it consistent across all nodes of the
same tier without exposing the master private key.
"""
import os
import hashlib


def derive_runtime_key(
    node_private_key: bytes = None,
    data_key: str = None,
    tier: str = None,
    passphrase: str = None,
) -> bytes:
    """Derive the AES-256 runtime key using the HARTOS key hierarchy.

    Tries sources in priority order:
      1. Ed25519 node private key → deterministic per-node key
      2. HEVOLVE_DATA_KEY → Fernet key used for data-at-rest
      3. Tier-based derivation → same key for all nodes of same tier
      4. Passphrase → manual fallback

    Returns:
        32-byte AES key

    Raises:
        RuntimeError: if no key source is available
    """
    from hevolvearmor._native import (
        armor_derive_key_ed25519,
        armor_derive_key_raw,
        armor_derive_key_passphrase,
    )

    # 1. Node Ed25519 private key
    if node_private_key is None:
        node_private_key = _load_node_private_key()
    if node_private_key is not None:
        return bytes(armor_derive_key_ed25519(node_private_key))

    # 2. HEVOLVE_DATA_KEY
    if data_key is None:
        data_key = os.environ.get('HEVOLVE_DATA_KEY')
    if data_key:
        raw = data_key.encode('utf-8') if isinstance(data_key, str) else data_key
        return bytes(armor_derive_key_raw(raw))

    # 3. Tier-based (deterministic per tier — uses master public key as salt)
    if tier is None:
        tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
    tier_seed = _derive_tier_seed(tier)
    if tier_seed:
        return bytes(armor_derive_key_raw(tier_seed))

    # 4. Passphrase
    if passphrase:
        return bytes(armor_derive_key_passphrase(passphrase))

    raise RuntimeError(
        "HevolveArmor: cannot derive runtime key. "
        "Set HEVOLVE_DATA_KEY, provide a node keypair, or pass a passphrase."
    )


def _load_node_private_key() -> bytes | None:
    """Try to load the node's Ed25519 private key from HARTOS security infra."""
    try:
        from security.node_integrity import get_or_create_keypair
        private_key, _ = get_or_create_keypair()
        # Ed25519PrivateKey → raw 32 bytes
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PrivateFormat, NoEncryption
        )
        raw = private_key.private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption()
        )
        return raw
    except Exception:
        pass

    # Fallback: check key file directly
    for candidate in [
        os.path.join(os.environ.get('HEVOLVE_KEY_DIR', ''), 'node_private_key.pem'),
        os.path.join('agent_data', 'node_private_key.pem'),
        os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data',
                      'node_private_key.pem'),
    ]:
        if os.path.isfile(candidate):
            try:
                from cryptography.hazmat.primitives.serialization import (
                    load_pem_private_key, Encoding, PrivateFormat, NoEncryption
                )
                with open(candidate, 'rb') as f:
                    pem = f.read()
                key = load_pem_private_key(pem, password=None)
                return key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
            except Exception:
                continue
    return None


def _derive_tier_seed(tier: str) -> bytes | None:
    """Derive a deterministic seed from tier + master public key.

    This allows pre-encrypting modules with a tier-specific key that
    any node of that tier can derive independently.
    """
    try:
        from security.master_key import MASTER_PUBLIC_KEY_HEX
        seed = hashlib.sha256(
            f"hevolvearmor-tier-{tier}-{MASTER_PUBLIC_KEY_HEX}".encode()
        ).digest()
        return seed
    except ImportError:
        # Not running inside HARTOS — use tier string alone
        seed = hashlib.sha256(
            f"hevolvearmor-tier-{tier}-standalone".encode()
        ).digest()
        return seed
