"""
test_release_hash_registry.py - Tests for security/release_hash_registry.py

FT: known-hash lookup across the three sources (GA table, release manifest,
    runtime discovery), and whether the node has any authoritative basis for
    judging a peer's code hash at all.
NFT: the bootstrap property. A node that knows no hashes must not be able to
     enforce a hash gate, because the answer to "is this known" is vacuous and
     enforcing it partitions the network.
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from security import release_hash_registry as rhr  # noqa: E402


@pytest.fixture
def registry():
    """A registry with no manifest, matching a stock desktop install."""
    r = rhr.ReleaseHashRegistry()
    r._manifest_hash = None
    r._runtime_hashes.clear()
    return r


class TestTrustBasis:
    """has_trust_basis decides whether the code-hash gate can be enforced."""

    def test_bare_node_has_no_basis(self, registry):
        """Ships empty: no GA table entries, no manifest.

        This is the shipping state. _KNOWN_HASHES is an empty dict, nothing
        populates it, and no signed manifest is bundled. Enforcing a hash gate
        here rejected every peer on the network.
        """
        assert not rhr._KNOWN_HASHES, (
            'if GA hashes are now published, this test documents the old '
            'state and the bootstrap path below needs rechecking')
        assert registry.has_trust_basis() is False

    def test_manifest_creates_a_basis(self, registry):
        registry._manifest_hash = 'a' * 64
        assert registry.has_trust_basis() is True

    def test_ga_table_creates_a_basis(self, registry, monkeypatch):
        monkeypatch.setattr(rhr, '_KNOWN_HASHES', {'1.2.3': 'b' * 64})
        assert registry.has_trust_basis() is True

    def test_runtime_hashes_do_not_create_a_basis(self, registry):
        """Learned-from-peers hashes must not switch the gate on.

        Otherwise the first hash a node happens to learn becomes the standard
        every later peer is judged against, and the gate bootstraps itself
        from hearsay rather than from the release pipeline.
        """
        registry.add_runtime_hash('9.9.9', 'c' * 64)
        assert registry.is_known_release_hash('c' * 64) is True
        assert registry.has_trust_basis() is False


class TestKnownHashLookup:

    def test_unknown_hash_is_not_known(self, registry):
        assert registry.is_known_release_hash('d' * 64) is False

    def test_empty_hash_is_not_known(self, registry):
        assert registry.is_known_release_hash('') is False

    def test_manifest_hash_is_known(self, registry):
        registry._manifest_hash = 'e' * 64
        assert registry.is_known_release_hash('e' * 64) is True

    def test_runtime_hash_is_known(self, registry):
        registry.add_runtime_hash('1.0.1', 'f' * 64)
        assert registry.is_known_release_hash('f' * 64) is True

    def test_runtime_hashes_are_bounded(self, registry):
        for i in range(rhr._MAX_RUNTIME_HASHES + 10):
            registry.add_runtime_hash(f'v{i}', f'{i:064d}')
        assert len(registry._runtime_hashes) <= rhr._MAX_RUNTIME_HASHES
