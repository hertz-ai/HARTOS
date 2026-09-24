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

_PRIVATE_KEY_FILE = 'node_private_key.pem'
_PUBLIC_KEY_FILE = 'node_public_key.pem'


def _resolve_key_dir():
    explicit = os.environ.get('HEVOLVE_KEY_DIR')
    if explicit:
        return explicit
    db_path = os.environ.get('HEVOLVE_DB_PATH', '')
    if db_path and db_path != ':memory:' and os.path.isabs(db_path):
        return os.path.dirname(db_path)
    # The last resort used to be a RELATIVE 'agent_data': the same machine
    # minted a DIFFERENT Ed25519 identity per working directory, and under
    # a read-only install root (Program Files) the key write failed
    # outright (#632).  Anchor to the stable user data dir instead, and
    # adopt a legacy CWD-relative keypair once so no node changes identity.
    try:
        from core.platform_paths import get_identity_data_dir
        root = get_identity_data_dir()
    except Exception:
        return 'agent_data'  # bare checkout without core: old behaviour
    # A keypair that already exists in data/ wins over agent_data/. data/ is
    # where a Nunba process keeps its keys (dirname of its HEVOLVE_DB_PATH), so
    # a helper process without that env used to resolve agent_data/ and sign as
    # a DIFFERENT node on the same machine (#140: the owner's desktop held
    # fee9f14a in data/, the key central knows, and 25cedaa4 in agent_data/).
    # Only an existing keypair moves the choice; nothing is minted in data/.
    # channel_encryption's x25519 keys follow the same dir, on purpose.
    data_dir = os.path.join(root, 'data')
    if os.path.isfile(os.path.join(data_dir, _PRIVATE_KEY_FILE)) and \
            os.path.isfile(os.path.join(data_dir, _PUBLIC_KEY_FILE)):
        return data_dir
    stable = os.path.join(root, 'agent_data')
    legacy_priv = os.path.join('agent_data', _PRIVATE_KEY_FILE)
    stable_priv = os.path.join(stable, _PRIVATE_KEY_FILE)
    if os.path.isfile(legacy_priv) and not os.path.isfile(stable_priv):
        try:
            os.makedirs(stable, exist_ok=True)
            shutil.copy2(legacy_priv, stable_priv)
            legacy_pub = os.path.join('agent_data', _PUBLIC_KEY_FILE)
            if os.path.isfile(legacy_pub):
                shutil.copy2(legacy_pub,
                             os.path.join(stable, _PUBLIC_KEY_FILE))
            logger.warning('Adopted legacy CWD-relative node keypair '
                           '%s -> %s (#632)',
                           os.path.abspath(legacy_priv), stable_priv)
        except OSError as e:
            logger.warning('Legacy keypair adoption failed (%s); '
                           'using stable dir %s', e, stable)
    return stable


_KEY_DIR = _resolve_key_dir()

# Set only by switch_key_dir: this process recovered its identity's key from
# another local dir (#140 B3 A1), so every key user must follow it there.
_key_dir_override = None


def resolve_key_dir():
    """Canonical resolver for "where does this node's key material live".

    channel_encryption (X25519 persists alongside Ed25519) and
    key_delegation (node_certificate.json) import THIS -- their own copies
    had already drifted (key_delegation missed the HEVOLVE_DB_PATH branch).
    """
    return _key_dir_override or _resolve_key_dir()
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


_IDENTITY_FILE = 'node_identity.json'

# How this process got its node_id, for whoever must confirm or question it:
#   'recorded'           node_identity.json in the key dir, bound to this key
#   'minted'             no identity existed; a new one was recorded for this key
#   'legacy_provisional' the old node_id.json, not yet confirmed to belong to
#                        this key; used in memory, nothing written (#140 B1)
identity_state = None


def _write_identity_once(path, record):
    """Create path with record unless it already exists; return what is on disk.

    Two processes can boot together (Nunba and a helper) and both mint. The
    record is written to a temp file and hard-linked into place, which fails if
    another process got there first; either way the caller uses the file on
    disk, so both processes end up with ONE id instead of forking it.
    """
    import json
    import uuid as _uuid
    tmp = f'{path}.{os.getpid()}.{_uuid.uuid4().hex}.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(record, fh)
    try:
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        except OSError:
            # No hard links on this filesystem: atomic replace, then re-read.
            if not os.path.exists(path):
                os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def load_or_create_node_identity(legacy_node_id_path=None) -> str:
    """This node's id, stored next to the key that proves it.

    The id used to live in <data root>/node_id.json while the key came from
    resolve_key_dir(), so one machine could hold one id and two keys (the
    owner's desktop: data/ and agent_data/), and whichever process wrote last
    paired the id with its key. node_identity.json now lives IN the key dir:
    two key dirs are two identities, never one id with two keys.

    - Record present and bound to this key: use it.
    - Record bound to a different key (the key under it was replaced): the old
      id cannot be proven by this key. Keep the old record as
      node_identity.superseded.<ts>.json (restoring the old key recovers it)
      and mint a new id for this key.
    - No record, but a legacy node_id.json: nothing says which key it belongs
      to, so it is NOT adopted here. Use it in memory for this boot and write
      nothing; it is persisted only once a peer confirms it holds THIS key for
      that id, and a mismatch goes to the owner, never to a silent re-mint.
    - Nothing at all: mint and record.
    """
    import json
    import uuid as _uuid
    from datetime import datetime as _dt
    global identity_state

    pub_hex = get_public_key_hex()
    key_dir = Path(_KEY_DIR)
    rec_path = key_dir / _IDENTITY_FILE
    fresh = {'node_id': str(_uuid.uuid4()), 'public_key_hex': pub_hex,
             'created_at': _dt.utcnow().isoformat()}

    if rec_path.exists():
        try:
            rec = json.loads(rec_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            rec = None
        if rec and rec.get('node_id') and rec.get('public_key_hex') == pub_hex:
            identity_state = 'recorded'
            return rec['node_id']
        stamp = f"{_dt.utcnow().strftime('%Y%m%dT%H%M%S')}.{os.getpid()}"
        superseded = key_dir / f'node_identity.superseded.{stamp}.json'
        try:
            # Another process on this key may have just replaced it.
            now = json.loads(rec_path.read_text(encoding='utf-8'))
            if now.get('node_id') and now.get('public_key_hex') == pub_hex:
                identity_state = 'recorded'
                return now['node_id']
        except (OSError, ValueError):
            pass
        logger.warning(
            'node identity record %s does not match the key in %s '
            '(record key %s, this key %s); keeping it as %s and taking a new '
            'identity for this key', rec_path, key_dir,
            str((rec or {}).get('public_key_hex', 'unreadable'))[:16],
            pub_hex[:16], superseded.name)
        try:
            os.replace(rec_path, superseded)
        except FileNotFoundError:
            pass  # another process moved it first; _write_identity_once settles it
        rec = _write_identity_once(str(rec_path), fresh)
        identity_state = 'minted'
        return rec['node_id']

    if legacy_node_id_path and os.path.exists(legacy_node_id_path):
        try:
            with open(legacy_node_id_path, 'r', encoding='utf-8') as fh:
                legacy_id = json.load(fh).get('node_id', '')
        except (OSError, ValueError):
            legacy_id = ''
        if legacy_id:
            identity_state = 'legacy_provisional'
            return legacy_id

    rec = _write_identity_once(str(rec_path), fresh)
    identity_state = 'minted' if rec['node_id'] == fresh['node_id'] else 'recorded'
    return rec['node_id']


def _public_hex_in(key_dir) -> str:
    """Raw hex of the Ed25519 public key in key_dir, or '' if it has no pair."""
    pub = os.path.join(key_dir, _PUBLIC_KEY_FILE)
    if not (os.path.isfile(pub)
            and os.path.isfile(os.path.join(key_dir, _PRIVATE_KEY_FILE))):
        return ''
    try:
        key = serialization.load_pem_public_key(Path(pub).read_bytes())
        return key.public_bytes(encoding=serialization.Encoding.Raw,
                                format=serialization.PublicFormat.Raw).hex()
    except Exception:
        return ''


def local_key_dir_holding(fingerprint: str):
    """The local key dir whose public key starts with fingerprint, or None.

    Candidates are every place this machine has kept node keys: the DB_PATH
    dir, <data root>/data, <data root>/agent_data, and the legacy
    CWD-relative agent_data (#632).  An explicit HEVOLVE_KEY_DIR is an
    operator's choice and is never switched away from, so nothing is
    returned then.
    """
    fp = (fingerprint or '').strip().lower()
    if len(fp) < 16 or os.environ.get('HEVOLVE_KEY_DIR'):
        return None
    candidates = []
    db_path = os.environ.get('HEVOLVE_DB_PATH', '')
    if db_path and db_path != ':memory:' and os.path.isabs(db_path):
        candidates.append(os.path.dirname(db_path))
    try:
        from core.platform_paths import get_identity_data_dir
        root = get_identity_data_dir()
        candidates += [os.path.join(root, 'data'), os.path.join(root, 'agent_data')]
    except Exception:
        pass
    candidates.append(os.path.abspath('agent_data'))
    seen = set()
    for d in candidates:
        d = os.path.abspath(d)
        if d in seen:
            continue
        seen.add(d)
        if _public_hex_in(d).startswith(fp):
            return d
    return None


def switch_key_dir(key_dir: str) -> None:
    """Point this process at the keys in key_dir, everywhere at once.

    The ONE owner of the in-process switch (#140 B3 A1): signing
    (get_or_create_keypair), X25519 (channel_encryption, which resolves through
    resolve_key_dir) and the node certificate path (key_delegation) all follow,
    so a node never signs with one key while encrypting with another.  Not
    persisted: the resolver's data/-first order already picks the right dir at
    boot, and this only covers a node that booted on the wrong one.
    """
    import sys as _sys
    global _KEY_DIR, _key_dir_override, _private_key, _public_key, identity_state
    _KEY_DIR = _key_dir_override = str(key_dir)
    _private_key = _public_key = None
    identity_state = None
    ce = _sys.modules.get('security.channel_encryption')
    if ce is not None:
        ce.reset_keypair_cache()
    kd = _sys.modules.get('security.key_delegation')
    if kd is not None:
        kd._DEFAULT_CERT_PATH = os.path.join(_KEY_DIR, kd._CERT_FILE)


def confirm_node_identity(node_id: str) -> str:
    """Record a provisional legacy id once a configured seed accepted it.

    A seed accepts a direct announce only when it holds THIS key for the id
    (or held no key and binds this one), so its "accepted" is the evidence
    the legacy node_id.json never carried.  First writer wins, as at boot;
    returns the id now on record.
    """
    from datetime import datetime as _dt
    global identity_state
    rec = _write_identity_once(
        str(Path(_KEY_DIR) / _IDENTITY_FILE),
        {'node_id': node_id, 'public_key_hex': get_public_key_hex(),
         'created_at': _dt.utcnow().isoformat(), 'confirmed_legacy': True})
    identity_state = 'recorded'
    if rec.get('node_id') != node_id:
        logger.warning('node identity record already names %s; keeping it '
                       'over the confirmed legacy id %s',
                       rec.get('node_id', '')[:8], node_id[:8])
    return rec['node_id']


def take_new_node_identity(reason: str) -> str:
    """Mint and record a new id for this key, keeping any old record aside.

    Called only on an AUTHENTICATED key conflict from a configured seed, after
    no key on this machine matched the one the seed holds (#140 B3, A2): this
    key cannot prove the old id, so the node takes a new one.
    """
    import uuid as _uuid
    from datetime import datetime as _dt
    global identity_state
    key_dir = Path(_KEY_DIR)
    rec_path = key_dir / _IDENTITY_FILE
    if rec_path.exists():
        stamp = f"{_dt.utcnow().strftime('%Y%m%dT%H%M%S')}.{os.getpid()}"
        try:
            os.replace(rec_path, key_dir / f'node_identity.superseded.{stamp}.json')
        except FileNotFoundError:
            pass
    rec = _write_identity_once(str(rec_path), {
        'node_id': str(_uuid.uuid4()), 'public_key_hex': get_public_key_hex(),
        'created_at': _dt.utcnow().isoformat()})
    identity_state = 'minted'
    logger.warning('node took a new identity %s (%s)', rec['node_id'][:8], reason)
    return rec['node_id']


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


def compute_code_hash(code_root: str = None, force_walk: bool = False) -> str:
    """Compute SHA-256 manifest hash of all .py files in the project.

    Deterministic across identical deployments.

    Performance modes for embedded/resource-constrained devices:
        HEVOLVE_CODE_HASH_PRECOMPUTED: Skip computation entirely (ROM/SD card).
            Set at build time from a known-good hash.
        File cache (agent_data/code_hash_cache.json): Reuse cached hash if
            no .py file has a newer mtime than the cache timestamp.

    force_walk: bypass BOTH shortcut tiers and hash the actual bytes.
        Exists for the runtime integrity monitor, whose entire job is
        detecting that the bytes changed: the Nunba bundle sets
        HEVOLVE_CODE_HASH_PRECOMPUTED to sha256(exe_path|exe_mtime) as a
        cheap stable identity (Nunba app.py:230), so without this flag the
        monitor's periodic re-check compared that constant against itself
        every cycle — an edited .py could never move either side of the
        comparison.  The mtime cache is skipped for the same reason: an
        attacker who back-dates a file's mtime keeps the cache warm.  A
        forced walk is also a pure read — it does not refresh the cache.
    """
    root = Path(code_root or _CODE_ROOT)

    if not force_walk:
        # Tier 1: Precomputed hash (ROM/read-only deployments)
        precomputed = os.environ.get('HEVOLVE_CODE_HASH_PRECOMPUTED', '')
        if precomputed:
            logger.debug(f"Code hash: using precomputed {precomputed[:16]}...")
            return precomputed

        # Tier 2: File-based cache (skip recompute if .py files unchanged)
        cached = _load_code_hash_cache(root)
        if cached:
            return cached

    # Tier 3: Full computation
    py_files = sorted(_collect_py_files(root, root))
    file_manifest = {rel_path: _hash_file(file_path)
                     for rel_path, file_path in py_files}
    result = manifest_to_code_hash(file_manifest)

    if not force_walk:
        # Save to cache for next boot
        _save_code_hash_cache(root, result)

    return result


def manifest_to_code_hash(file_manifest: Dict[str, str]) -> str:
    """Fold a {relative_path: sha256} file manifest into THE code hash.

    Single source of truth for the fold format ("rel:hash" lines, sorted,
    sha256).  compute_code_hash's full walk goes through here, and so does
    the runtime monitor's boot-baseline mode, which derives its expected
    hash from the boot snapshot it already takes — one walk at boot, not
    two.  If the fold ever drifted between those two call sites, every
    baseline-mode node would false-positive as tampered on its first full
    verify.
    """
    manifest = '\n'.join(f"{rel}:{file_manifest[rel]}"
                         for rel in sorted(file_manifest))
    return hashlib.sha256(manifest.encode('utf-8')).hexdigest()


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
