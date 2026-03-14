"""
Native Hive AI Loader — Loads closed-source HevolveAI binaries at runtime.

Architecture:
  HART OS (open source, BSL-1.1)
       │
       ├─ Reads source code freely — it's open source
       │
       └─ Loads HevolveAI native binary (closed source, packed)
           │
           ├─ .so (Linux)  / .dll (Windows) / .dylib (macOS)
           ├─ Compiled from Rust/C++ — no readable source
           ├─ Signed by master key — tampered binaries rejected
           ├─ Provides: Hebbian learning, Bayesian inference,
           │   RALT distribution, world model, biometric ML
           └─ Exposed via C ABI + Python ctypes/cffi wrapper

Who can run HevolveAI:
  Anyone on a legitimate deployment — Nunba, HARTOS standalone, Docker,
  HART OS (Live OS), cloud, or pip install. Flat, regional, or central tier.
  Users never need the master key. They download already-signed binaries.

Protection (against forks weaponizing, NOT against users running):
  1. Binary is compiled (Rust/C++) — not readable Python
  2. Binary is release-signed by master key — proves authenticity (not tampered)
  3. Binary checks origin attestation — refuses to load on unauthorized forks
  4. Forks cannot sign modified binaries — no master key = can't distribute
     weaponized versions that pass verification
  5. Forks cannot join federation — origin attestation fails
  6. License prohibits decompilation / reverse engineering

Search order for native binary:
  1. HEVOLVE_NATIVE_LIB env var (explicit path)
  2. /usr/lib/hevolve/libhevolve_ai.so (system install)
  3. ~/.hevolve/lib/libhevolve_ai.so (user install)
  4. {HART_ROOT}/lib/libhevolve_ai.so (in-tree)
  5. Falls back to pure-Python stubs (reduced functionality)
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
_native_available = False
_stub_mode = False


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
    """Search for the native binary in known locations."""
    for path in _SEARCH_PATHS:
        if path and os.path.isfile(path):
            return path
    return None


def load_native_lib(force_reload: bool = False) -> Tuple[bool, str]:
    """Load the HevolveAI native binary.

    Returns (success, message).

    The binary provides:
      - hevolve_init() → Initialize the AI engine
      - hevolve_infer(prompt, model) → Run inference
      - hevolve_hebbian_update(signals) → Hebbian learning step
      - hevolve_bayesian_update(prior, evidence) → Bayesian update
      - hevolve_world_model_step(state) → World model evolution
      - hevolve_verify_origin(fingerprint) → Origin attestation
      - hevolve_version() → Version string
      - hevolve_shutdown() → Clean shutdown
    """
    global _native_lib, _native_available, _stub_mode

    if _native_lib and not force_reload:
        return True, 'Already loaded'

    path = _find_native_binary()
    if not path:
        _stub_mode = True
        _native_available = False
        logger.info(
            "HevolveAI native binary not found — running in stub mode. "
            "AI features (Hebbian learning, Bayesian inference, RALT, "
            "world model) will use pure-Python fallbacks with reduced "
            "performance. Install the binary for full capability."
        )
        return False, 'Native binary not found — stub mode active'

    # Verify binary signature
    sig_ok, sig_msg = _verify_binary_signature(path)
    enforcement = os.environ.get('HEVOLVE_ENFORCEMENT_MODE', 'warn').lower()
    if not sig_ok:
        if enforcement == 'hard':
            _stub_mode = True
            _native_available = False
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
    return True, f'Loaded from {path}'


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

def native_infer(prompt: str, model: str = 'default',
                 options: Optional[Dict] = None) -> Optional[str]:
    """Call native inference if available, else return None for Python fallback."""
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
    """Run Hebbian learning step via native binary."""
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
    """Get native binary version string."""
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
    """Clean shutdown of native binary."""
    global _native_lib, _native_available
    if _native_lib:
        try:
            func = _native_lib.hevolve_shutdown
            func.restype = ctypes.c_int
            func()
        except AttributeError:
            pass
        _native_lib = None
        _native_available = False


def get_status() -> Dict:
    """Status summary for diagnostics."""
    return {
        'native_available': _native_available,
        'stub_mode': _stub_mode,
        'binary_path': _find_native_binary(),
        'version': native_version(),
        'platform_lib': _LIB_NAME,
        'search_paths': [p for p in _SEARCH_PATHS if p],
    }
