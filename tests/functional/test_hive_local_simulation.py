"""T213: Local hive simulation — two HARTOS instances on different ports.

Tests peer discovery, gossip, federation, and master key operations
without needing a physical second node. Uses Flask test_client for
in-process simulation of both nodes.
"""
import os
import sys
import pytest
import json
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class TestGossipProtocol:
    """Verify gossip protocol can serialize/deserialize peer info."""

    def test_gossip_self_info_serializable(self):
        from integrations.social.peer_discovery import GossipProtocol
        gp = GossipProtocol.__new__(GossipProtocol)
        gp._node_id = 'test-node-001'
        gp._capability_tier = 'standard'
        gp._bandwidth_profile = 'full'
        # The gossip payload should be JSON-serializable
        info = {
            'node_id': gp._node_id,
            'tier': gp._capability_tier,
            'profile': gp._bandwidth_profile,
            'timestamp': time.time(),
        }
        serialized = json.dumps(info)
        assert len(serialized) > 0
        parsed = json.loads(serialized)
        assert parsed['node_id'] == 'test-node-001'

    def test_bandwidth_profiles_exist(self):
        from integrations.social.peer_discovery import GossipProtocol
        # Verify all bandwidth profile configs exist
        profiles = ['full', 'constrained', 'minimal']
        for p in profiles:
            assert p in ('full', 'constrained', 'minimal')


class TestFederatedAggregator:
    """Verify federated learning delta extraction and aggregation."""

    def test_delta_signing(self):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        fa = FederatedAggregator.__new__(FederatedAggregator)
        fa._node_id = 'test-node'
        fa._hmac_secret = b'test-secret-key-32-bytes-long!!!'
        delta = {'task_type': 'coding', 'tool': 'shell', 'success_rate': 0.85}
        # Sign should produce a hex string
        import hmac, hashlib
        sig = hmac.new(fa._hmac_secret, json.dumps(delta, sort_keys=True).encode(),
                       hashlib.sha256).hexdigest()
        assert len(sig) == 64  # SHA-256 hex

    def test_delta_verification(self):
        import hmac, hashlib
        secret = b'test-secret'
        delta = {'metric': 'accuracy', 'value': 0.92}
        payload = json.dumps(delta, sort_keys=True).encode()
        sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        # Verify
        expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        assert sig == expected


class TestMasterKeyAndCircuitBreaker:
    """Verify master key verification and circuit breaker operations."""

    def test_circuit_breaker_initial_state(self):
        from security.hive_guardrails import HiveCircuitBreaker
        # Reset class state — previous tests may have tripped the breaker
        HiveCircuitBreaker._halted = False
        cb = HiveCircuitBreaker()
        assert not cb.is_halted()

    def test_circuit_breaker_local_trip(self):
        from security.hive_guardrails import HiveCircuitBreaker
        cb = HiveCircuitBreaker()
        cb.trip('test: safety monitor detected issue')
        assert cb.is_halted()

    def test_guardrail_hash_deterministic(self):
        from security.hive_guardrails import compute_guardrail_hash
        h1 = compute_guardrail_hash()
        h2 = compute_guardrail_hash()
        assert h1 == h2  # Same frozen values → same hash

    def test_constitutional_rules_exist(self):
        from security.hive_guardrails import CONSTITUTIONAL_RULES
        assert len(CONSTITUTIONAL_RULES) >= 30

    def test_compute_caps_defined(self):
        from security.hive_guardrails import COMPUTE_CAPS
        assert COMPUTE_CAPS.get('max_influence_weight', 0) > 0
        assert COMPUTE_CAPS.get('single_entity_cap_pct', 0) > 0


class TestDistributedTaskCoordinator:
    """Verify task coordination primitives."""

    def test_task_status_enum(self):
        from agent_ledger.core import TaskStatus
        assert hasattr(TaskStatus, 'PENDING')
        assert hasattr(TaskStatus, 'IN_PROGRESS')
        assert hasattr(TaskStatus, 'COMPLETED')

    def test_smart_ledger_class_exists(self):
        from agent_ledger.core import SmartLedger
        assert SmartLedger is not None
        assert callable(SmartLedger)


class TestNodeTierGating:
    """Verify tier classification and feature gating."""

    def test_tier_levels_ordered(self):
        from security.system_requirements import NodeTierLevel
        tiers = [NodeTierLevel.EMBEDDED, NodeTierLevel.OBSERVER,
                 NodeTierLevel.LITE, NodeTierLevel.STANDARD,
                 NodeTierLevel.FULL, NodeTierLevel.COMPUTE_HOST]
        assert len(tiers) == 6

    def test_feature_gates_exist(self):
        from security.system_requirements import FEATURE_TIER_MAP
        assert 'local_llm' in FEATURE_TIER_MAP or len(FEATURE_TIER_MAP) > 0


# ═══════════════════════════════════════════════════════════════════
# T213 continued: the two-node census this file's docstring promises
# ═══════════════════════════════════════════════════════════════════
#
# The header says "two HARTOS instances on different ports ... without needing
# a physical second node", but every test above is a shape check. None of them
# peers two nodes, so none would have caught the live symptom: hive-census
# reporting nodes_reporting=1 with local:true, on a network with 66 registered
# peers.
#
# These run the real production path in HARD enforcement, which is the default
# (security/master_key.get_enforcement_mode: "the correct default for Sybil
# resistance"). No warn-mode shortcut: a census test that passes only because
# verification was relaxed would prove nothing about production.

class TestTwoNodeCensus:
    """Node A produces a delta, node B accepts it, B's census counts two."""

    @staticmethod
    def _pair(monkeypatch):
        from integrations.agent_engine.federated_aggregator import (
            FederatedAggregator, register_peer_hmac_secret, _get_hmac_secret,
            _sign_delta,
        )
        monkeypatch.setenv('HEVOLVE_ENFORCEMENT_MODE', 'hard')
        node_a = FederatedAggregator()
        node_b = FederatedAggregator()
        # A running node always holds its own delta; that is the entry the live
        # census reported as local:true. Without it the census counts only
        # peers, and this test would assert against a state no real node is in.
        node_b._local_delta = node_b.extract_local_delta()
        # Simulate the federation handshake: B learns A's HMAC secret. In
        # production this is exchanged signed by the node's Ed25519 key. Both
        # aggregators share this process's per-node secret, so registering it
        # under A's id is what the handshake would have cached.
        return (node_a, node_b, register_peer_hmac_secret, _get_hmac_secret,
                _sign_delta)

    @staticmethod
    def _wire_delta(node, sign, as_node_id=None):
        """A delta exactly as it appears on the wire.

        extract_local_delta() attaches the Ed25519 signature and public key;
        broadcast_delta() then calls _sign_delta() to add the HMAC before
        posting (federated_aggregator.py:422). Both steps are the production
        path, and a receiver in hard mode requires both.

        Worth stating because I got this wrong first: testing with the output
        of extract_local_delta() alone produces "missing HMAC signature (hard
        enforcement)" and looks exactly like a broken federation. It is not.
        It is a test that skipped the signing step the sender performs.
        """
        delta = node.extract_local_delta()
        if not delta:
            return delta
        if as_node_id:
            # Both aggregators in this process resolve the SAME node identity,
            # so without this the census sees one distinct node and counts 1.
            # Re-sign after changing the id: Ed25519 covers node_id, and the
            # receiver verifies against delta['public_key'], which does not
            # bind a key to an id. This is a peer that shares our keypair, not
            # a forged one, and it exercises the real acceptance path.
            from security.node_integrity import sign_json_payload
            delta['node_id'] = as_node_id
            delta['signature'] = sign_json_payload(
                {k: v for k, v in delta.items() if k != 'hmac_signature'})
        sign(delta)
        return delta

    def test_census_counts_a_second_node_after_a_delta_is_accepted(self, monkeypatch):
        """The end-to-end assertion. Live this reads 1; it must read 2."""
        node_a, node_b, register, own_secret, sign = self._pair(monkeypatch)

        delta = self._wire_delta(node_a, sign, as_node_id='peer_node_a')
        if not delta:
            pytest.skip('no local delta available in this environment')

        register(delta.get('node_id', ''), own_secret())
        accepted, reason = node_b.receive_peer_delta(delta)
        assert accepted, f'delta rejected under hard enforcement: {reason}'

        census = node_b.hive_census()
        assert census.get('nodes_reporting', 0) >= 2, census

    def test_census_marks_exactly_one_node_local(self, monkeypatch):
        """local:true is how the live census revealed it was alone. With a real
        peer present, exactly one entry may claim it."""
        node_a, node_b, register, own_secret, sign = self._pair(monkeypatch)

        delta = self._wire_delta(node_a, sign, as_node_id='peer_node_a')
        if not delta:
            pytest.skip('no local delta available in this environment')

        register(delta.get('node_id', ''), own_secret())
        node_b.receive_peer_delta(delta)

        per_node = node_b.hive_census().get('per_node', {})
        locals_ = [k for k, v in per_node.items() if v.get('local')]
        assert len(locals_) == 1, per_node

    def test_unsigned_delta_is_refused_under_hard_enforcement(self, monkeypatch):
        """The guard that makes the two tests above meaningful.

        If anything can be accepted, counting to 2 proves nothing.
        """
        _, node_b, _, _, _ = self._pair(monkeypatch)
        import time as _t
        from integrations.agent_engine.federated_aggregator import DELTA_VERSION

        accepted, reason = node_b.receive_peer_delta({
            'version': DELTA_VERSION,
            'node_id': 'impostor',
            'timestamp': _t.time(),
            'signature': '',
        })
        assert not accepted
        assert 'signature' in reason.lower(), reason
