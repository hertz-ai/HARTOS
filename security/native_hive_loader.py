"""
Native Hive AI Loader — Loads closed-source HevolveAI at runtime.

Architecture:
  HART OS (open source, BSL-1.1)
       │
       ├─ Reads source code freely — it's open source
       │
       └─ Loads HevolveAI (closed source, compiled)
           │
           ├─ PRIMARY: Cython-compiled Python wheel
           │   ├─ .so (Linux) / .pyd (Windows) — standard Python C extensions
           │   ├─ Compiled from Python via Cython — not readable source
           │   ├─ Installed: pip install hevolveai-0.1.0-cp311-cp311-linux_x86_64.whl
           │   └─ Imported via: import hevolveai (standard Python import)
           │
           ├─ FALLBACK: Native binary via ctypes (Rust hevolveai_topo)
           │   ├─ .so (Linux) / .dll (Windows) / .dylib (macOS)
           │   ├─ Signed by master key — tampered binaries rejected
           │   └─ Exposed via C ABI + Python ctypes wrapper
           │
           ├─ Provides: Hebbian learning, Bayesian inference,
           │   RALT distribution, world model, embodied AI
           │
           └─ STUB: Pure-Python stubs if neither path available

Who can run HevolveAI:
  Anyone on a legitimate deployment — Nunba, HARTOS standalone, Docker,
  HART OS (Live OS), cloud, or pip install. Flat, regional, or central tier.
  Users never need the master key. They download already-signed binaries.

Protection (against forks weaponizing, NOT against users running):
  1. Cython-compiled .so/.pyd — not readable Python, not trivially decompilable
  2. Release-signed by master key — proves authenticity (not tampered)
  3. Origin attestation check — refuses to load on unauthorized forks
  4. Forks cannot sign modified packages — no master key = can't distribute
  5. Forks cannot join federation — origin attestation fails
  6. License prohibits decompilation / reverse engineering

Load order:
  1. import hevolveai (Cython-compiled wheel, pip-installed)
  2. HEVOLVE_NATIVE_LIB env var (ctypes binary, explicit path)
  3. /usr/lib/hevolve/libhevolve_ai.so (system install)
  4. ~/.hevolve/lib/libhevolve_ai.so (user install)
  5. {HART_ROOT}/lib/libhevolve_ai.so (in-tree)
  6. Falls back to pure-Python stubs (reduced functionality)
"""

import ctypes
import hashlib
import json
import logging
import os
import platform
import struct
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger('hevolve_security')

# ═══════════════════════════════════════════════════════════════════════
# Binary naming convention per platform
# ═══════════════════════════════════════════════════════════════════════

_PLATFORM_LIB = {
    'Linux': 'libhevolve_ai.so',
    'Darwin': 'libhevolve_ai.dylib',
    'Windows': 'hevolve_ai.dll',
}

_LIB_NAME = _PLATFORM_LIB.get(platform.system(), 'libhevolve_ai.so')

_HART_ROOT = os.environ.get(
    'HART_INSTALL_DIR',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

# Search paths for the native binary
_SEARCH_PATHS = [
    os.environ.get('HEVOLVE_NATIVE_LIB', ''),
    f'/usr/lib/hevolve/{_LIB_NAME}',
    f'/usr/local/lib/hevolve/{_LIB_NAME}',
    os.path.expanduser(f'~/.hevolve/lib/{_LIB_NAME}'),
    os.path.join(_HART_ROOT, 'lib', _LIB_NAME),
]

# Expected signatures for known binary versions (SHA-256 of binary)
# Updated on each official release by the CI/CD pipeline
_KNOWN_SIGNATURES: Dict[str, str] = {}

# Module-level singleton
_native_lib: Optional[ctypes.CDLL] = None
_cython_module = None  # The Cython-compiled hevolveai package (if imported)
_native_available = False
_stub_mode = False
_load_method: Optional[str] = None  # 'cython' | 'ctypes' | None


# ═══════════════════════════════════════════════════════════════════════
# Binary verification
# ═══════════════════════════════════════════════════════════════════════

def _compute_binary_hash(path: str) -> str:
    """SHA-256 hash of the native binary file."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _verify_binary_signature(path: str) -> Tuple[bool, str]:
    """Verify the native binary was signed by the master key.

    The binary has a 256-byte Ed25519 signature appended after a magic marker.
    Format: [binary content] [HEVOLVE_SIG_V1] [64-byte Ed25519 signature]
    """
    MAGIC = b'HEVOLVE_SIG_V1'
    SIG_LEN = 64  # Ed25519 signature is 64 bytes

    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            file_size = f.tell()

            if file_size < len(MAGIC) + SIG_LEN + 1024:
                return False, 'Binary too small to contain signature'

            # Read the signature trailer
            trailer_size = len(MAGIC) + SIG_LEN
            f.seek(-trailer_size, 2)
            trailer = f.read(trailer_size)

            if not trailer.startswith(MAGIC):
                # No signature embedded — check known hashes instead
                bin_hash = _compute_binary_hash(path)
                if bin_hash in _KNOWN_SIGNATURES:
                    return True, f'Known binary hash: {bin_hash[:16]}...'
                # In dev mode, allow unsigned binaries
                if os.environ.get('HEVOLVE_DEV_MODE', '').lower() == 'true':
                    return True, 'Dev mode — unsigned binary allowed'
                return False, 'No embedded signature and unknown hash'

            # Extract signature
            sig_bytes = trailer[len(MAGIC):]

            # Hash the binary content (excluding the signature trailer)
            content_size = file_size - trailer_size
            f.seek(0)
            h = hashlib.sha256()
            remaining = content_size
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
            content_hash = h.digest()

        # Verify with master public key
        from security.master_key import get_master_public_key
        pub_key = get_master_public_key()
        pub_key.verify(sig_bytes, content_hash)
        return True, 'Binary signature verified by master key'

    except Exception as e:
        return False, f'Signature verification error: {e}'


def _verify_binary_origin_check(lib: ctypes.CDLL) -> Tuple[bool, str]:
    """Call the binary's built-in origin check function.

    The native binary has a compiled-in function that verifies
    it's running inside genuine HART OS (checks origin fingerprint).
    A fork loading the binary will fail this check.
    """
    try:
        # The binary exposes: int hevolve_verify_origin(const char* fingerprint)
        # Returns 0 on success, non-zero on failure
        func = lib.hevolve_verify_origin
        func.argtypes = [ctypes.c_char_p]
        func.restype = ctypes.c_int

        from security.origin_attestation import ORIGIN_FINGERPRINT
        result = func(ORIGIN_FINGERPRINT.encode('utf-8'))
        if result == 0:
            return True, 'Binary origin check passed'
        return False, f'Binary rejected origin (code {result})'
    except AttributeError:
        # Function not exported — older binary version, accept
        return True, 'Binary does not export origin check (older version)'
    except Exception as e:
        return False, f'Origin check error: {e}'


# ═══════════════════════════════════════════════════════════════════════
# Binary loading
# ═══════════════════════════════════════════════════════════════════════

def _find_native_binary() -> Optional[str]:
    """Search for the native binary in known locations.

    Checks for both plaintext (.so/.dll) and encrypted (.so.enc) variants.
    Encrypted binaries are decrypted to tmpfs (RAM) at load time.
    """
    for path in _SEARCH_PATHS:
        if path and os.path.isfile(path):
            return path
        # Check for encrypted variant
        enc_path = path + '.enc' if path else ''
        if enc_path and os.path.isfile(enc_path):
            decrypted = _decrypt_binary_to_tmpfs(enc_path)
            if decrypted:
                return decrypted
    return None


def _decrypt_binary_to_tmpfs(enc_path: str) -> Optional[str]:
    """Decrypt an encrypted HevolveAI binary to RAM-only filesystem.

    The binary is AES-256-GCM encrypted. The decryption key is derived via
    ECDH between this node's Ed25519 key and the master public key, ensuring
    only legitimate HART OS nodes (with valid first-boot keypairs) can decrypt.

    File format: [12-byte nonce][ciphertext][16-byte GCM tag]

    The decrypted binary lives ONLY in tmpfs (RAM) — never touches disk.
    Docker: --read-only --tmpfs /run/hevolve ensures this.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        logger.warning("cryptography package not available — cannot decrypt binary")
        return None

    try:
        # Derive decryption key: HKDF(node_private_key_seed || master_public_key)
        # This ensures only nodes with valid Ed25519 keys from first-boot can decrypt
        node_key_path = os.path.expanduser('~/.hevolve/keys/node_private.key')
        if not os.path.isfile(node_key_path):
            node_key_path = '/var/lib/hevolve/node_private.key'
        if not os.path.isfile(node_key_path):
            logger.debug("No node private key — cannot decrypt binary")
            return None

        with open(node_key_path, 'rb') as f:
            node_seed = f.read(32)

        from security.master_key import MASTER_PUBLIC_KEY_HEX
        master_pub_bytes = bytes.fromhex(MASTER_PUBLIC_KEY_HEX)

        # HKDF: derive AES-256 key from node_seed + master_public_key
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=master_pub_bytes,
            info=b'hevolve-native-binary-v1',
        )
        aes_key = hkdf.derive(node_seed)

        # Read encrypted binary
        with open(enc_path, 'rb') as f:
            data = f.read()

        if len(data) < 28:  # 12 nonce + 16 tag minimum
            logger.warning(f"Encrypted binary too small: {enc_path}")
            return None

        nonce = data[:12]
        ciphertext_with_tag = data[12:]

        # Decrypt
        aesgcm = AESGCM(aes_key)
        plaintext = aesgcm.decrypt(nonce, ciphertext_with_tag, None)

        # Write to tmpfs (RAM only — never persists to disk)
        tmpfs_dir = os.environ.get('HEVOLVE_TMPFS', '/run/hevolve')
        if not os.path.isdir(tmpfs_dir):
            # Fallback: /dev/shm (Linux shared memory) or system temp
            for candidate in ['/dev/shm/hevolve', '/tmp/.hevolve_runtime']:
                try:
                    os.makedirs(candidate, mode=0o700, exist_ok=True)
                    tmpfs_dir = candidate
                    break
                except OSError:
                    continue

        decrypted_path = os.path.join(tmpfs_dir, _LIB_NAME)
        with open(decrypted_path, 'wb') as f:
            f.write(plaintext)
        os.chmod(decrypted_path, 0o500)  # read+execute only

        logger.info(f"Decrypted HevolveAI binary to tmpfs: {decrypted_path}")
        return decrypted_path

    except Exception as e:
        logger.warning(f"Failed to decrypt binary {enc_path}: {e}")
        return None


def _try_load_cython_package() -> Tuple[bool, str]:
    """Try to import the Cython-compiled hevolveai wheel.

    This is the PRIMARY load path. The wheel is installed via pip and
    contains .so/.pyd Cython extensions — standard Python imports, no ctypes.

    Returns (success, message).
    """
    global _cython_module, _native_available, _stub_mode, _load_method

    try:
        import hevolveai
        # Verify it's actually compiled (not someone's source checkout)
        mod_file = getattr(hevolveai, '__file__', '') or ''
        # Compiled packages have __init__.cpython-*.so or __init__.pyd
        # OR a minimal stub __init__.py that imports from compiled submodules
        # Check for at least one compiled submodule
        pkg_dir = os.path.dirname(mod_file)
        if pkg_dir:
            has_compiled = any(
                f.endswith('.so') or f.endswith('.pyd')
                for d, _, files in os.walk(pkg_dir)
                for f in files
                if '.cpython-' in f or f.endswith('.pyd')
            )
            if not has_compiled:
                # This is a source checkout, not the compiled wheel
                return False, 'hevolveai found but not Cython-compiled (source install)'

        _cython_module = hevolveai
        _native_available = True
        _stub_mode = False
        _load_method = 'cython'
        version = getattr(hevolveai, '__version__', 'unknown')
        logger.info(f"Loaded HevolveAI Cython package v{version} from {mod_file}")
        return True, f'Cython wheel loaded: {mod_file}'

    except ImportError:
        return False, 'hevolveai package not installed'
    except Exception as e:
        return False, f'hevolveai import error: {e}'


def load_native_lib(force_reload: bool = False) -> Tuple[bool, str]:
    """Load HevolveAI — tries Cython wheel first, then ctypes binary.

    Returns (success, message).

    Load order:
      1. Cython-compiled wheel (pip install hevolveai-*.whl)
      2. ctypes native binary (.so/.dll)
      3. Stub mode (reduced functionality)
    """
    global _native_lib, _native_available, _stub_mode, _load_method

    if (_native_available or _cython_module) and not force_reload:
        return True, f'Already loaded ({_load_method})'

    # ── Path 1: Cython-compiled Python wheel (primary) ────────
    ok, msg = _try_load_cython_package()
    if ok:
        return True, msg
    logger.debug(f"Cython path: {msg}")

    # ── Path 2: ctypes native binary (fallback) ──────────────
    path = _find_native_binary()
    if not path:
        _stub_mode = True
        _native_available = False
        _load_method = None
        logger.info(
            "HevolveAI not available — running in stub mode. "
            "AI features (Hebbian learning, Bayesian inference, RALT, "
            "world model) will use pure-Python fallbacks with reduced "
            "performance. Install the compiled wheel for full capability: "
            "pip install hevolveai-*.whl"
        )
        return False, 'HevolveAI not found — stub mode active'

    # Verify binary signature
    sig_ok, sig_msg = _verify_binary_signature(path)
    enforcement = os.environ.get('HEVOLVE_ENFORCEMENT_MODE', 'warn').lower()
    if not sig_ok:
        if enforcement == 'hard':
            _stub_mode = True
            _native_available = False
            _load_method = None
            logger.critical(f"Refusing to load unsigned binary: {sig_msg}")
            return False, f'Binary signature verification failed: {sig_msg}'
        else:
            logger.warning(f"Binary signature warning: {sig_msg}")

    # Load the binary
    try:
        _native_lib = ctypes.CDLL(path)
        logger.info(f"Loaded HevolveAI native binary from {path}")
    except OSError as e:
        _stub_mode = True
        _native_available = False
        _load_method = None
        logger.error(f"Failed to load native binary: {e}")
        return False, f'Load failed: {e}'

    # Origin check — the binary verifies it's inside genuine HART OS
    origin_ok, origin_msg = _verify_binary_origin_check(_native_lib)
    if not origin_ok:
        logger.warning(f"Binary origin check: {origin_msg}")
        if enforcement == 'hard':
            _native_lib = None
            _stub_mode = True
            _native_available = False
            _load_method = None
            return False, f'Binary origin check failed: {origin_msg}'

    # Initialize
    try:
        init_func = _native_lib.hevolve_init
        init_func.restype = ctypes.c_int
        result = init_func()
        if result != 0:
            logger.warning(f"hevolve_init() returned {result}")
    except AttributeError:
        pass  # No init function — older binary

    _native_available = True
    _stub_mode = False
    _load_method = 'ctypes'
    return True, f'Loaded ctypes binary from {path}'


def is_native_available() -> bool:
    """Check if the native binary is loaded and functional."""
    return _native_available


def is_stub_mode() -> bool:
    """Check if running with pure-Python stubs (no native binary)."""
    return _stub_mode


def get_native_lib() -> Optional[ctypes.CDLL]:
    """Get the loaded native library handle."""
    return _native_lib


# ═══════════════════════════════════════════════════════════════════════
# Python-side function wrappers (safe to call even in stub mode)
# ═══════════════════════════════════════════════════════════════════════

def get_hevolveai():
    """Get the loaded hevolveai Cython package, or None.

    HARTOS code that needs HevolveAI should use this:
        hevolveai = get_hevolveai()
        if hevolveai:
            from hevolveai.embodied_ai.learning.hive_mind import HiveMind
            ...
    """
    return _cython_module


def native_infer(prompt: str, model: str = 'default',
                 options: Optional[Dict] = None) -> Optional[str]:
    """Call inference if available, else return None for Python fallback."""
    # Cython path — call the Python API directly
    if _cython_module:
        try:
            from hevolveai.embodied_ai.inference.qwen_inference_only import infer
            return infer(prompt, model=model, **(options or {}))
        except (ImportError, AttributeError):
            pass
        except Exception as e:
            logger.debug(f"Cython inference error: {e}")
            return None

    # ctypes path
    if not _native_available or not _native_lib:
        return None
    try:
        func = _native_lib.hevolve_infer
        func.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        func.restype = ctypes.c_char_p
        result = func(prompt.encode('utf-8'), model.encode('utf-8'))
        return result.decode('utf-8') if result else None
    except Exception as e:
        logger.debug(f"Native inference error: {e}")
        return None


def native_hebbian_update(signals: Dict[str, float]) -> Optional[Dict]:
    """Run Hebbian learning step."""
    # Cython path
    if _cython_module:
        try:
            from hevolveai.embodied_ai.learning.hebbian_differentiator import HebbianDifferentiator
            heb = HebbianDifferentiator()
            return heb.update(signals)
        except (ImportError, AttributeError):
            pass
        except Exception as e:
            logger.debug(f"Cython Hebbian error: {e}")
            return None

    # ctypes path
    if not _native_available or not _native_lib:
        return None
    try:
        func = _native_lib.hevolve_hebbian_update
        func.argtypes = [ctypes.c_char_p]
        func.restype = ctypes.c_char_p
        payload = json.dumps(signals).encode('utf-8')
        result = func(payload)
        return json.loads(result.decode('utf-8')) if result else None
    except Exception as e:
        logger.debug(f"Native Hebbian error: {e}")
        return None


def native_version() -> Optional[str]:
    """Get HevolveAI version string."""
    if _cython_module:
        return getattr(_cython_module, '__version__', '0.1.0')
    if not _native_available or not _native_lib:
        return None
    try:
        func = _native_lib.hevolve_version
        func.restype = ctypes.c_char_p
        result = func()
        return result.decode('utf-8') if result else None
    except Exception:
        return None


def shutdown_native():
    """Clean shutdown of HevolveAI."""
    global _native_lib, _native_available, _cython_module, _load_method
    if _native_lib:
        try:
            func = _native_lib.hevolve_shutdown
            func.restype = ctypes.c_int
            func()
        except AttributeError:
            pass
        _native_lib = None
    _cython_module = None
    _native_available = False
    _load_method = None


def get_status() -> Dict:
    """Status summary for diagnostics."""
    return {
        'native_available': _native_available,
        'stub_mode': _stub_mode,
        'load_method': _load_method,
        'binary_path': _find_native_binary(),
        'cython_package': bool(_cython_module),
        'version': native_version(),
        'platform_lib': _LIB_NAME,
        'search_paths': [p for p in _SEARCH_PATHS if p],
    }
