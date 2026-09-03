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
  3. This node's OWN running code hash — same build as me is my build
  4. Runtime discovery — hashes from verified peers (bounded, thread-safe)

Usage at perimeter (READ THE SUPERSESSION NOTE BELOW FIRST -- this is a
TRUST SIGNAL, not an admission gate):

  from security.release_hash_registry import get_release_hash_registry
  registry = get_release_hash_registry()
  master_key_verified = registry.is_known_release_hash(peer_code_hash)
  # peer is admitted either way; the flag records HOW MUCH we trust it

WHAT SUPERSEDED WHAT, AND WHY THE OLD PATHS ARE STILL HERE
===========================================================
Three things changed meaning over time. None of the old code is dead; each was
DEMOTED from a hard gate to an input, and the inputs are load-bearing. Knowing
which is which is the difference between reading this file correctly and
concluding the network cannot federate.

1. REJECT-ON-UNKNOWN-HASH  ->  ADMIT-AND-RECORD
   Was: `if not is_known_release_hash(peer): reject_peer()` (the example this
        docstring used to show).
   Now: the peer is admitted with master_key_verified=False and
        hash_trusted_source='untrusted' (peer_discovery.py, see the long note at
        the admission site).
   Why: an announced code_hash is SELF-REPORTED. The signature proves "this key
        asserted this value", never "this is the code running", so a hostile node
        just claims a known-good hash. The gate never stopped an attacker; it
        only stopped honest nodes on unpublished builds. Measured: it held the
        live network at ZERO federating peers out of 69 registered nodes.
   Retained because: the flag it produces still decides real things --
        canary node selection (upgrade_orchestrator.py:539 filters
        master_key_verified=True), hive revenue credit
        (speculative_dispatcher.py:2063, "only master_key_verified nodes get
        credit"), moat scoring (ip_service.py:420), plus visibility tier and
        fraud_score. Deleting the check would silently zero all of those.
   Strict mode still exists for a locked cluster where every node genuinely runs
        one published build: HEVOLVE_REQUIRE_KNOWN_CODE_HASH=1, guarded by
        has_trust_basis() so it cannot be switched on into a vacuum and partition
        the very cluster it was meant to protect.

2. CODE-HASH-AS-PROVENANCE  ->  CHALLENGE / ATTESTATION
   Was: treating a matching hash as proof of what a peer is running.
   Now: /api/social/integrity/challenge and .../challenge-response
        (integrations/social/discovery.py:758,776; driven from
        integrity_service.py:190) prove the RUNNING code by round-trip.
   Why: see above -- a self-reported value cannot prove itself. A signed list of
        legitimate hashes would not fix that either; it would still be trusting
        the peer's claim about which entry it matches.
   Retained because: the hash is a free first-pass signal that arrives with the
        announce, before any round-trip. It is a cheap prior, not a proof.

3. RELEASE MANIFEST AS THE NODE-SIDE TRUST BASIS  ->  _KNOWN_HASHES
   Was: _load_from_manifest() as the general answer for every deployment.
   Now: BY DESIGN the signed release_manifest.json exists on CENTRAL ONLY. No
        HART OS appliance and no desktop bundle ships one, and none is expected
        to (verified on hardware: full_boot_verification returns
        reason='no_manifest' on a live node). Everyone else trusts peers via
        _KNOWN_HASHES, populated at release time by
        scripts/update_release_hashes.py (see .github/workflows/release-sign.yml).
   Retained because: on central the manifest IS present and IS the authoritative
        basis, and it stays the documented fallback if the registry is
        unavailable. Do NOT "fix" a node's missing manifest by shipping one --
        CI computes code_hash over a repo checkout while a node computes it over
        a /nix/store path, so they cannot match, and the mismatch would fire
        fleet-wide.

Note the structural limit of _KNOWN_HASHES that follows from (3): the table is
baked into each build, and a tree cannot contain its own hash, so a build only
ever learns the hashes of builds that came BEFORE it. Older nodes therefore
cannot recognise newer ones, and a build from an untagged main commit appears in
no table at all. That is precisely why admission no longer depends on it.
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
    '1.0.0': '0fee15efe05f3a0fac3973a89980e9213c78c7e05710ef6ae950c60c7534afad',
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
        # Own-hash layer: computed lazily on first lookup (compute_code_hash
        # is mtime-cached after first boot, but first computation walks every
        # .py — keep it off the constructor).  '' = tried and failed, so the
        # layer stays inert without retry storms.
        self._self_hash: Optional[str] = None
        self._load_from_manifest()

    def _own_hash(self) -> str:
        """This node's running code hash, computed once, '' on failure."""
        if self._self_hash is None:
            try:
                from security.node_integrity import compute_code_hash
                self._self_hash = compute_code_hash() or ''
            except Exception:
                self._self_hash = ''
        return self._self_hash

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
          3. This node's OWN running code hash
          4. Any runtime-discovered hash from a verified peer
        """
        if not code_hash:
            return False

        # 1. Hardcoded GA releases
        if code_hash in _KNOWN_HASHES.values():
            return True

        # 2. Current manifest
        if self._manifest_hash and code_hash == self._manifest_hash:
            return True

        # 3. Same build as me.  Fleet policy (steward, 2026-08-22): every
        #    nightly and every machine in the central network is trustworthy
        #    by design.  A peer whose reported hash equals the hash of the
        #    code I am running IS my build — the strongest provenance signal
        #    this registry has, and the only one that needs no pipeline:
        #    identical nightlies verify each other the moment they boot,
        #    frozen bundles included (whose trees never match a repo-tag
        #    entry), on every platform, with no registry round-trip and no
        #    self-reference lag.  Security is unchanged: code_hash is
        #    self-reported everywhere (see has_trust_basis), so claiming MY
        #    hash is the same spoof as claiming a GA hash — provenance proof
        #    stays with the challenge/attestation endpoints.
        _own = self._own_hash()
        if _own and code_hash == _own:
            return True

        # 4. Runtime-discovered
        with self._lock:
            if code_hash in self._runtime_hashes.values():
                return True

        return False

    def get_known_versions(self) -> Dict[str, str]:
        """Return all known version→hash mappings (for diagnostics)."""
        result = dict(_KNOWN_HASHES)
        if self._manifest_hash:
            result['_current_manifest'] = self._manifest_hash
        _own = self._own_hash()
        if _own:
            result['_self'] = _own
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

    def has_trust_basis(self) -> bool:
        """Is there any authoritative basis for judging a peer's code hash?

        True only for hashes that came from the release pipeline: the
        hardcoded GA table, or a release manifest that passed signature
        verification. Runtime-discovered hashes deliberately do NOT count.
        They are learned from other peers, so counting them would let the
        gate switch itself on from hearsay, and the first hash learned would
        become the standard everyone else is measured against.

        This exists because "is this hash known" has no meaningful answer on
        a node that knows no hashes at all. _KNOWN_HASHES ships empty, no
        workflow populates it, and no signed manifest ships in the desktop
        bundle, so is_known_release_hash() returned False for every peer.
        Under enforcement=hard that rejected all of them, which is what held
        the live network at zero peers: 69 registered nodes, none federating.

        Note also that an announced code_hash is self-reported. It is signed,
        so it is authenticated as "this key asserted this hash", but nothing
        proves it matches the code actually running. A hostile node simply
        claims a known-good hash. So rejecting unknown hashes never stopped
        an attacker; it only stopped honest nodes on new builds. Provenance
        belongs in the trust signals (integrity_status, master_key_verified,
        fraud_score, the challenge/attestation endpoints), not in a gate that
        cannot be enforced meaningfully without a basis.

        The own-hash layer (is_known_release_hash source 3) deliberately does
        NOT count either.  Every node trivially knows its own hash, so
        counting it would arm HEVOLVE_REQUIRE_KNOWN_CODE_HASH on every node
        — turning strict mode into "only my exact build may join" on boxes
        whose operator never published anything.  Strict enforcement stays
        tied to hashes the release pipeline actually vouched for.
        """
        return bool(_KNOWN_HASHES) or bool(self._manifest_hash)

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
