"""
Node Integrity: Ed25519 keypair management, code hashing, and signature operations.
Provides cryptographic identity for peer verification in the HevolveSocial network.
"""
import os
import json
import hashlib
import logging
import shutil
from pathlib import Path
from typing import Optional, Tuple, Dict

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger('hevolve_security')

def _resolve_key_dir():
    explicit = os.environ.get('HEVOLVE_KEY_DIR')
    if explicit:
        return explicit
    db_path = os.environ.get('HEVOLVE_DB_PATH', '')
    if db_path and db_path != ':memory:' and os.path.isabs(db_path):
        return os.path.dirname(db_path)
    return 'agent_data'

_KEY_DIR = _resolve_key_dir()
_PRIVATE_KEY_FILE = 'node_private_key.pem'
_PUBLIC_KEY_FILE = 'node_public_key.pem'
_CODE_ROOT = os.environ.get('HEVOLVE_CODE_ROOT', os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))

# Module-level cache
_private_key: Optional[Ed25519PrivateKey] = None
_public_key: Optional[Ed25519PublicKey] = None

# Directories excluded from code hash computation.
#
# Defense-in-depth (2026-04-19): when compute_code_hash is called in a
# cx_Freeze bundle and HEVOLVE_CODE_HASH_PRECOMPUTED is NOT set (e.g.,
# env var missing because app.py's setup block raised), the fallback
# walk runs against the install root.  In a cx_Freeze layout that root
# contains `python-embed/` (stdlib + site-packages, 10k+ .py files),
# `lib/` (bundled .pyc modules), `lib_src/` (pycparser + cryptography
# source copies), `build/` (intermediate artifacts), `landing-page/`
# (React build output), `node_modules/` (already excluded).  Without
# these in the exclude set, a single code-hash walk on cold cache
# takes 2-5 minutes per caller, and 5+ peer-discovery threads running
# in parallel stalled boot for 10+ minutes in startup_trace.log from
# 2026-04-19T17:00:29.  The exclude-set expansion keeps that walk
# bounded to Nunba/HARTOS source only.
_EXCLUDE_DIRS = {
    '__pycache__', 'venv310', 'venv', '.venv', '.git', '.idea',
    'agent_data', 'tests', 'node_modules', 'hevolve_backend.egg-info',
    'autogen-0.2.37', '.pycharm_plugin',
    # cx_Freeze bundle dirs (Nunba desktop install) — defense-in-depth
    # in case HEVOLVE_CODE_HASH_PRECOMPUTED is not set by the host app.
    'python-embed', 'lib', 'lib_src', 'build', 'landing-page',
    'Output', 'dist', '.pytest_cache', '.ruff_cache', '.mypy_cache',
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
            raw = priv_path.read_bytes()
            # Decrypt at rest — auto-detects encrypted vs plaintext PEM
            try:
                from security.crypto import decrypt_data
                raw = decrypt_data(raw)
            except ImportError:
                # NEVER silent: without security.crypto the key bytes are used
                # EXACTLY as they sit on disk. If they are plaintext PEM this is
                # the intended path and load_pem_private_key succeeds; if they are
                # encrypted-at-rest, the load below fails with a confusing PEM
                # error and the real cause (the crypto module is missing) is
                # nowhere in the log. Name it once, here.
                logger.warning(
                    "node_integrity: security.crypto unavailable — the node private "
                    "key is being read WITHOUT decrypt-at-rest. Fine for a plaintext "
                    "PEM; if the key is encrypted, the PEM load below will fail and "
                    "THIS is why.", exc_info=True)
            _private_key = serialization.load_pem_private_key(raw, password=None)
            _public_key = _private_key.public_key()
            logger.info(f"Node keypair loaded from {key_dir}")
            return _private_key, _public_key
        except Exception as e:
            # An EXISTING private key that fails to load/decrypt is almost always
            # a TRANSIENT problem (HEVOLVE_DATA_KEY missing/wrong, a partial write,
            # a bad read) — NOT a signal to mint a fresh identity. Silently
            # regenerating here would (a) rotate the node's peer trust anchor and
            # (b) overwrite the on-disk key below, destroying identity material
            # that was very likely recoverable. Fail loudly and leave the file
            # untouched so a steward can restore the key or supply the right data
            # key, rather than discovering the node became a stranger to its peers.
            logger.error(
                f"Existing node private key at {priv_path} failed to load: {e}. "
                f"Refusing to regenerate (that would rotate/destroy the node identity)."
            )
            raise RuntimeError(
                f"Node private key exists but could not be loaded: {e}. Refusing to "
                f"overwrite the existing identity — check HEVOLVE_DATA_KEY or restore "
                f"{priv_path}."
            ) from e

    # Generate new keypair
    _private_key = Ed25519PrivateKey.generate()
    _public_key = _private_key.public_key()

    # Persist to disk — encrypted at rest when HEVOLVE_DATA_KEY is set
    priv_pem = _private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = _public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    try:
        from security.crypto import encrypt_data
        priv_path.write_bytes(encrypt_data(priv_pem))
    except ImportError:
        priv_path.write_bytes(priv_pem)
    pub_path.write_bytes(pub_pem)  # Public key stays plaintext
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


def canonical_payload(payload: dict, exclude=('signature',)) -> bytes:
    """The ONE canonical serialization for every Ed25519 sign/verify in HART OS:
    drop the signature field(s), then ``json.dumps`` with sorted keys and no
    whitespace. Every signer AND verifier must emit byte-identical bytes here or
    signatures silently fail network-wide — so this is the single source of truth
    that master_key / key_delegation / origin_attestation / pre_trust_contract and
    this module route through, instead of each re-implementing
    ``json.dumps(..., sort_keys=True, separators=(',',':'))`` inline (any drift =
    network-wide verification failure).

    ``exclude`` (the signature key-name(s) to strip) differs per payload type
    ('signature' / 'sig' / 'node_sig' / ...), so it stays a parameter — the
    SERIALIZATION is what must never drift, not the exclude-set.
    """
    ex = (exclude,) if isinstance(exclude, str) else tuple(exclude)
    clean = {k: v for k, v in payload.items() if k not in ex}
    return json.dumps(clean, sort_keys=True, separators=(',', ':')).encode('utf-8')


def sign_json_payload(payload: dict) -> str:
    """Canonicalize dict (sorted JSON, no spaces), sign it, return hex signature.
    The payload dict should NOT contain the 'signature' key itself."""
    sig = sign_message(canonical_payload(payload, exclude=('signature',)))
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


def sign_message_hex(message: str) -> str:
    """Hex detached signature over a plain UTF-8 string (not a JSON payload).

    Companion to sign_json_payload for the one case that signs a bare string
    rather than a dict: PeerLink's SAME_USER proof, where the signed value is
    the user_id itself.
    """
    return sign_message(message.encode('utf-8')).hex()


def verify_message_signature(public_key_hex: str, message: str,
                             signature_hex: str) -> bool:
    """Verify a detached Ed25519 signature over a plain UTF-8 string.

    The verifier `PeerLink._verify_same_user_proof` has always imported and
    never found: the symbol did not exist anywhere in the repo, so that import
    raised ImportError, the gate failed closed, and SAME_USER could not be
    granted to any peer on any node.  That in turn left every link at PEER, and
    `message_bus._route_peerlink` filters its per-user fan-out on SAME_USER —
    so multi-device sync, and the skill broadcast riding it, reached nobody.

    Argument order matches the call the gate makes and the tests pin:
    (peer public key, the message we expect them to have signed, signature).
    """
    try:
        return verify_signature(public_key_hex, message.encode('utf-8'),
                                bytes.fromhex(signature_hex))
    except (ValueError, Exception):
        return False


def verify_json_signature(public_key_hex: str, payload: dict,
                          signature_hex: str) -> bool:
    """Verify signature on a JSON payload. Strips 'signature' key before verification."""
    try:
        sig = bytes.fromhex(signature_hex)
        return verify_signature(public_key_hex,
                                canonical_payload(payload, exclude=('signature',)), sig)
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
        # Read-only FS - silently skip
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
    except (PermissionError, OSError):
        # NEVER silent: an unreadable directory means its .py files are silently
        # ABSENT from the code hash. The hash still computes and still compares —
        # over a SUBSET of the tree — so integrity verification quietly covers less
        # than it claims. Keep walking (one bad dir must not abort the scan), but
        # make the reduced coverage visible.
        logger.warning(
            "node_integrity: cannot read %s — its .py files are EXCLUDED from the "
            "code hash, so integrity verification covers less of the tree than it "
            "appears to", directory, exc_info=True)


def _hash_file(file_path: Path) -> str:
    """Compute SHA-256 hash of a single file."""
    h = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                h.update(chunk)
    except (IOError, OSError):
        # NEVER silent: an unreadable file yields the hash of EMPTY input, which is
        # a real-looking sha256 that says nothing about the file. Two different
        # unreadable files hash identically, and a file that becomes unreadable
        # looks like a file that changed. Return it (callers expect a string) but
        # do not let it pass unnoticed.
        logger.warning(
            "node_integrity: cannot read %s — hashing it as EMPTY, so this entry "
            "does not reflect the file's real contents", file_path, exc_info=True)
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


def purge_pycache(code_root: str = None) -> int:
    """Delete all __pycache__ directories and prevent bytecode regeneration.

    Called at boot before the integrity manifest snapshot is taken.
    Blocks bytecode injection attacks where malicious .pyc files
    could be loaded by Python instead of the verified .py sources.

    Returns count of __pycache__ directories removed.
    """
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    root = Path(code_root or _CODE_ROOT)
    count = 0
    try:
        for pycache_dir in root.rglob('__pycache__'):
            if pycache_dir.is_dir():
                shutil.rmtree(pycache_dir, ignore_errors=True)
                count += 1
        if count:
            logger.info(f"Boot integrity: purged {count} __pycache__ directories")
    except (PermissionError, OSError) as e:
        logger.warning(f"Boot integrity: pycache purge partial - {e}")
    return count
