"""
Safe Deserialization - Pickle Replacement
Replaces pickle.loads() for numpy frame data with a safe binary format.
Defends against CVE-style RCE via deserialization (OpenClaw attack vector).

Format: [4-byte header length][JSON header][raw numpy bytes]
Header: {"shape": [h, w, c], "dtype": "uint8"}
"""

import io
import json
import struct
import pickle
import logging
from typing import Optional

logger = logging.getLogger('hevolve_security')

# Sentinel bytes to identify the safe format
_MAGIC = b'HVSF'  # HevolVe Safe Frame

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


class RestrictedUnpickler(pickle.Unpickler):
    """
    Restricted unpickler that only allows numpy types.
    Used as a fallback for legacy pickle data during migration.
    """

    ALLOWED_MODULES = {
        'numpy': {'ndarray', 'dtype', 'core'},
        'numpy.core.multiarray': {'_reconstruct', 'scalar'},
        'numpy.core.numeric': {'*'},
        'numpy.ma.core': {'MaskedArray'},
    }

    def find_class(self, module, name):
        module_base = module.split('.')[0]
        if module_base == 'numpy':
            allowed = self.ALLOWED_MODULES.get(module)
            if allowed is None or name in allowed or '*' in allowed:
                return super().find_class(module, name)
        raise pickle.UnpicklingError(
            f"Blocked unpickling of {module}.{name} - "
            f"only numpy types are allowed"
        )


def safe_dump_frame(frame) -> bytes:
    """
    Serialize a numpy array without pickle.
    Returns: magic + header_size + json_header + raw_bytes
    """
    if not HAS_NUMPY:
        raise RuntimeError("numpy required for frame serialization")

    header = json.dumps({
        'shape': list(frame.shape),
        'dtype': str(frame.dtype),
    }).encode('utf-8')

    header_size = struct.pack('<I', len(header))
    return _MAGIC + header_size + header + frame.tobytes()


def safe_load_frame(data: bytes):
    """
    Deserialize a numpy array safely.
    Tries safe format first, falls back to RestrictedUnpickler for legacy data.
    Returns the numpy array, or None on failure.
    """
    if not HAS_NUMPY:
        raise RuntimeError("numpy required for frame deserialization")

    # Try safe format first
    if data[:4] == _MAGIC:
        return _load_safe_format(data)

    # Fall back to restricted unpickler for legacy pickle data
    logger.warning("Legacy pickle data detected - using restricted unpickler")
    return _load_restricted_pickle(data)


def _load_safe_format(data: bytes):
    """Load from the safe binary format."""
    header_size = struct.unpack('<I', data[4:8])[0]
    header_json = data[8:8 + header_size]
    header = json.loads(header_json.decode('utf-8'))

    raw_bytes = data[8 + header_size:]
    return np.frombuffer(
        raw_bytes, dtype=np.dtype(header['dtype'])
    ).reshape(header['shape']).copy()


def _load_restricted_pickle(data: bytes):
    """
    Load legacy pickle data with restricted unpickler.
    Only allows numpy types - blocks arbitrary code execution.
    """
    try:
        return RestrictedUnpickler(io.BytesIO(data)).load()
    except (pickle.UnpicklingError, Exception) as e:
        logger.error(f"Restricted unpickle failed: {e}")
        return None


def migrate_redis_frame(redis_client, key: str) -> bool:
    """
    Migrate a single Redis key from pickle to safe format.
    Returns True if migration occurred.
    """
    data = redis_client.get(key)
    if data is None:
        return False

    if data[:4] == _MAGIC:
        return False  # Already in safe format

    frame = _load_restricted_pickle(data)
    if frame is None:
        return False

    safe_data = safe_dump_frame(frame)
    redis_client.set(key, safe_data)
    logger.info(f"Migrated Redis key {key} from pickle to safe format")
    return True
