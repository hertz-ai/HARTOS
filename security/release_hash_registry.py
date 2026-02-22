"""
Release Hash Registry — Multi-version code hash allowlist.

Maintains a set of known-good code hashes from GA releases so the
perimeter can accept peers running any valid version (not just the
current one).  Critical during rolling upgrades where the network
has a mix of old and new nodes.

Populated by:
  1. _KNOWN_HASHES dict — hardcoded by CI/CD at release time
     (scripts/update_release_hashes.py writes this dict)
  2. Current release manifest — always trusted
  3. Runtime discovery — hashes from verified peers (bounded, thread-safe)

Usage at perimeter:
  from security.release_hash_registry import ReleaseHashRegistry
  registry = ReleaseHashRegistry()
  if not registry.is_known_release_hash(peer_code_hash):
      reject_peer()
"""
import logging
import os
import threading
from collections import OrderedDict
from typing import Dict, Optional

logger = logging.getLogger('hevolve_security')

# ── CI/CD-populated GA release hashes ────────────────────────────
# Format: {'version_string': 'sha256_code_hash'}
# Updated automatically by scripts/update_release_hashes.py before
# each release signing.  Do NOT edit manually.
_KNOWN_HASHES: Dict[str, str] = {
    # CI/CD will append entries here, e.g.:
    # '1.0.0': 'abc123...',
    # '1.1.0': 'def456...',
}

# Maximum runtime-discovered hashes to keep (prevents unbounded growth)
_MAX_RUNTIME_HASHES = 50


class ReleaseHashRegistry:
    """Thread-safe registry of known-good code hashes.

    Combines:
      - Hardcoded GA release hashes (_KNOWN_HASHES)
      - Current release manifest's code_hash
      - Runtime-discovered hashes from verified peers
    """

    def __init__(self):
        self._lock = threading.Lock()
        # Runtime hashes: bounded OrderedDict (FIFO eviction)
        self._runtime_hashes: OrderedDict = OrderedDict()
        self._manifest_hash: Optional[str] = None
        self._load_from_manifest()

    def _load_from_manifest(self) -> None:
        """Load the current release manifest's code_hash as always-trusted."""
        try:
            from security.master_key import (
                load_release_manifest, verify_release_manifest,
            )
            manifest = load_release_manifest()
            if manifest and verify_release_manifest(manifest):
                self._manifest_hash = manifest.get('code_hash', '')
        except Exception:
            pass

    def is_known_release_hash(self, code_hash: str) -> bool:
        """Check if a code hash belongs to any known GA release.

        Returns True if the hash matches:
          1. Any hardcoded GA release hash
          2. The current release manifest's hash
          3. Any runtime-discovered hash from a verified peer
        """
        if not code_hash:
            return False

        # 1. Hardcoded GA releases
        if code_hash in _KNOWN_HASHES.values():
            return True

        # 2. Current manifest
        if self._manifest_hash and code_hash == self._manifest_hash:
            return True

        # 3. Runtime-discovered
        with self._lock:
            if code_hash in self._runtime_hashes.values():
                return True

        return False

    def get_known_versions(self) -> Dict[str, str]:
        """Return all known version→hash mappings (for diagnostics)."""
        result = dict(_KNOWN_HASHES)
        if self._manifest_hash:
            result['_current_manifest'] = self._manifest_hash
        with self._lock:
            result.update(self._runtime_hashes)
        return result

    def add_runtime_hash(self, version: str, code_hash: str) -> None:
        """Add a hash discovered from a verified peer at runtime.

        Thread-safe.  Bounded to _MAX_RUNTIME_HASHES entries (FIFO eviction).
        Only call this for hashes from peers that passed full verification
        (signature + master_key_verified).
        """
        if not version or not code_hash:
            return
        with self._lock:
            self._runtime_hashes[version] = code_hash
            # FIFO eviction if over limit
            while len(self._runtime_hashes) > _MAX_RUNTIME_HASHES:
                self._runtime_hashes.popitem(last=False)

    def hash_count(self) -> int:
        """Total number of known hashes (for diagnostics)."""
        count = len(_KNOWN_HASHES)
        if self._manifest_hash:
            count += 1
        with self._lock:
            count += len(self._runtime_hashes)
        return count


# ── Module-level singleton ────────────────────────────────────────
_registry = None
_registry_lock = threading.Lock()


def get_release_hash_registry() -> ReleaseHashRegistry:
    """Get or create the singleton ReleaseHashRegistry."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ReleaseHashRegistry()
    return _registry
