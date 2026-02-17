"""
Node Integrity: Ed25519 keypair management, code hashing, and signature operations.
Provides cryptographic identity for peer verification in the HevolveSocial network.
"""
import os
import json
import hashlib
import logging
from pathlib import Path
from typing import Optional, Tuple, Dict

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger('hevolve_security')

_KEY_DIR = os.environ.get('HEVOLVE_KEY_DIR', 'agent_data')
_PRIVATE_KEY_FILE = 'node_private_key.pem'
_PUBLIC_KEY_FILE = 'node_public_key.pem'
_CODE_ROOT = os.environ.get('HEVOLVE_CODE_ROOT', os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))

# Module-level cache
_private_key: Optional[Ed25519PrivateKey] = None
_public_key: Optional[Ed25519PublicKey] = None

# Directories excluded from code hash computation
_EXCLUDE_DIRS = {
    '__pycache__', 'venv310', 'venv', '.venv', '.git', '.idea',
    'agent_data', 'tests', 'node_modules', 'hevolve_backend.egg-info',
    'autogen-0.2.37', '.pycharm_plugin',
}


def get_or_create_keypair() -> Tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Load existing keypair from disk or generate a new one on first start."""
    global _private_key, _public_key
    if _private_key and _public_key:
        return _private_key, _public_key

    key_dir = Path(_KEY_DIR)
    key_dir.mkdir(parents=True, exist_ok=True)
    priv_path = key_dir / _PRIVATE_KEY_FILE
    pub_path = key_dir / _PUBLIC_KEY_FILE

    if priv_path.exists() and pub_path.exists():
        try:
            priv_pem = priv_path.read_bytes()
            _private_key = serialization.load_pem_private_key(priv_pem, password=None)
            _public_key = _private_key.public_key()
            logger.info(f"Node keypair loaded from {key_dir}")
            return _private_key, _public_key
        except Exception as e:
            logger.warning(f"Failed to load keypair, regenerating: {e}")

    # Generate new keypair
    _private_key = Ed25519PrivateKey.generate()
    _public_key = _private_key.public_key()

    # Persist to disk
    priv_pem = _private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = _public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path.write_bytes(priv_pem)
    pub_path.write_bytes(pub_pem)
    logger.info(f"Node keypair generated and saved to {key_dir}")
    return _private_key, _public_key


def get_public_key_bytes() -> bytes:
    """Return raw 32-byte public key."""
    _, pub = get_or_create_keypair()
    return pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def get_public_key_hex() -> str:
    """Return hex-encoded public key string for JSON payloads."""
    return get_public_key_bytes().hex()


def sign_message(message: bytes) -> bytes:
    """Sign arbitrary bytes with node's private key."""
    priv, _ = get_or_create_keypair()
    return priv.sign(message)


def sign_json_payload(payload: dict) -> str:
    """Canonicalize dict (sorted JSON, no spaces), sign it, return hex signature.
    The payload dict should NOT contain the 'signature' key itself."""
    clean = {k: v for k, v in payload.items() if k != 'signature'}
    canonical = json.dumps(clean, sort_keys=True, separators=(',', ':'))
    sig = sign_message(canonical.encode('utf-8'))
    return sig.hex()


def verify_signature(public_key_hex: str, message: bytes, signature: bytes) -> bool:
    """Verify a signature from a peer node."""
    try:
        raw_key = bytes.fromhex(public_key_hex)
        pub = Ed25519PublicKey.from_public_bytes(raw_key)
        pub.verify(signature, message)
        return True
    except (InvalidSignature, ValueError, Exception):
        return False


def verify_json_signature(public_key_hex: str, payload: dict,
                          signature_hex: str) -> bool:
    """Verify signature on a JSON payload. Strips 'signature' key before verification."""
    try:
        clean = {k: v for k, v in payload.items() if k != 'signature'}
        canonical = json.dumps(clean, sort_keys=True, separators=(',', ':'))
        sig = bytes.fromhex(signature_hex)
        return verify_signature(public_key_hex, canonical.encode('utf-8'), sig)
    except (ValueError, Exception):
        return False


def compute_code_hash(code_root: str = None) -> str:
    """Compute SHA-256 manifest hash of all .py files in the project.

    Deterministic across identical deployments.

    Performance modes for embedded/resource-constrained devices:
        HEVOLVE_CODE_HASH_PRECOMPUTED: Skip computation entirely (ROM/SD card).
            Set at build time from a known-good hash.
        File cache (agent_data/code_hash_cache.json): Reuse cached hash if
            no .py file has a newer mtime than the cache timestamp.
    """
    # Tier 1: Precomputed hash (ROM/read-only deployments)
    precomputed = os.environ.get('HEVOLVE_CODE_HASH_PRECOMPUTED', '')
    if precomputed:
        logger.debug(f"Code hash: using precomputed {precomputed[:16]}...")
        return precomputed

    root = Path(code_root or _CODE_ROOT)

    # Tier 2: File-based cache (skip recompute if .py files unchanged)
    cached = _load_code_hash_cache(root)
    if cached:
        return cached

    # Tier 3: Full computation
    manifest_lines = []
    py_files = sorted(_collect_py_files(root, root))
    for rel_path, file_path in py_files:
        file_hash = _hash_file(file_path)
        manifest_lines.append(f"{rel_path}:{file_hash}")

    manifest = '\n'.join(manifest_lines)
    result = hashlib.sha256(manifest.encode('utf-8')).hexdigest()

    # Save to cache for next boot
    _save_code_hash_cache(root, result)

    return result


def _load_code_hash_cache(root: Path) -> Optional[str]:
    """Load cached code hash if no .py file has changed since cache was written."""
    cache_path = root / 'agent_data' / 'code_hash_cache.json'
    try:
        if not cache_path.exists():
            return None
        with open(cache_path, 'r') as f:
            cache = json.load(f)
        cached_hash = cache.get('code_hash', '')
        cached_at = cache.get('cached_at', 0)
        if not cached_hash or not cached_at:
            return None

        # Check if any .py file is newer than the cache
        for _, file_path in _collect_py_files(root, root):
            try:
                if file_path.stat().st_mtime > cached_at:
                    logger.debug("Code hash cache stale: .py file modified")
                    return None
            except OSError:
                continue

        logger.debug(f"Code hash: using cache {cached_hash[:16]}...")
        return cached_hash
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def _save_code_hash_cache(root: Path, code_hash: str):
    """Save code hash to file cache for faster subsequent boots."""
    import time
    cache_path = root / 'agent_data' / 'code_hash_cache.json'
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump({'code_hash': code_hash, 'cached_at': time.time()}, f)
    except (OSError, IOError) as e:
        # Read-only FS — silently skip
        logger.debug(f"Code hash cache write skipped: {e}")


def compute_file_manifest(code_root: str = None) -> Dict[str, str]:
    """Return {relative_path: sha256_hex} for all tracked source files."""
    root = Path(code_root or _CODE_ROOT)
    result = {}
    for rel_path, file_path in sorted(_collect_py_files(root, root)):
        result[rel_path] = _hash_file(file_path)
    return result


def _collect_py_files(directory: Path, root: Path):
    """Walk directory recursively, yield (relative_path, absolute_path) for .py files."""
    try:
        for entry in sorted(directory.iterdir()):
            if entry.is_dir():
                if entry.name in _EXCLUDE_DIRS:
                    continue
                yield from _collect_py_files(entry, root)
            elif entry.is_file() and entry.suffix == '.py':
                rel = str(entry.relative_to(root)).replace('\\', '/')
                yield (rel, entry)
    except PermissionError:
        pass


def _hash_file(file_path: Path) -> str:
    """Compute SHA-256 hash of a single file."""
    h = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                h.update(chunk)
    except (IOError, OSError):
        pass
    return h.hexdigest()


def get_node_identity(code_root: str = None) -> dict:
    """Return consolidated node identity info.

    Returns dict with node_id (public key hex), public_key, tier, certificate,
    and code_hash. Consolidates identity info for gossip and registration.
    """
    from security.key_delegation import get_node_tier, load_node_certificate

    pub_hex = get_public_key_hex()
    cert = load_node_certificate()
    code_hash = compute_code_hash(code_root)

    return {
        'node_id': pub_hex[:16],
        'public_key': pub_hex,
        'tier': get_node_tier(),
        'certificate': cert,
        'code_hash': code_hash,
    }


def reset_keypair():
    """Reset cached keypair (for testing)."""
    global _private_key, _public_key
    _private_key = None
    _public_key = None
