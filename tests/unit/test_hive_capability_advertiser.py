"""Unit tests for ``integrations.agent_engine.hive_capability_advertiser``.

Coverage matrix:

  Opt-in semantics
    - HEVOLVE_HIVE_ADVERTISE default OFF
    - truthy / falsy value handling
    - attach() refuses without endpoint / without opt-in
    - attach() idempotent

  Identity resolution
    - HEVOLVE_NODE_ID explicit, 'local' sentinel, unset
    - HEVOLVE_HIVE_PUBLIC_ENDPOINT trim / unset
    - HEVOLVE_HIVE_AUTH_TOKEN trim / unset
    - HEVOLVE_HIVE_ADVERTISE_TIER recognised values, fallback

  Model enumeration
    - Filters out non-local models (no re-advertising others' work)
    - Filters by tier floor
    - Filters by accuracy floor
    - All advertised models stamped tier='expert' (consumer registers
      that tier exclusively)
    - Specialty propagates through

  Payload assembly
    - Returns None when no qualified models
    - Returns None when endpoint missing
    - Well-formed payload structure

  Lifecycle
    - shutdown() idempotent
    - shutdown() emits revoke
    - shutdown() before attach is safe (no revoke)

  Self-echo guard (consumer side regression test)
    - Self-announce → 0 backends registered
    - Different peer_id → registers normally
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def fresh_registry():
    """Registry seeded with one local EXPERT and one local FAST model
    so tier-floor tests have signal to filter."""
    from integrations.agent_engine.model_registry import (
        ModelRegistry, ModelBackend, ModelTier,
    )
    reg = ModelRegistry()
    reg.register(ModelBackend(
        model_id='qwen-27b-local',
        display_name='Qwen 27B Local',
        tier=ModelTier.EXPERT,
        config_list_entry={
            'model': 'qwen-27b', 'api_key': 'k',
            'specialty': ['coding', 'reasoning'],
        },
        accuracy_score=0.85, is_local=True,
    ))
    reg.register(ModelBackend(
        model_id='qwen-4b-local',
        display_name='Qwen 4B Local',
        tier=ModelTier.FAST,
        config_list_entry={'model': 'qwen-4b'},
        accuracy_score=0.6, is_local=True,
    ))
    reg.register(ModelBackend(
        model_id='qwen-0.8b-draft',
        display_name='Draft',
        tier=ModelTier.DRAFT,
        config_list_entry={'model': 'qwen-0.8b'},
        accuracy_score=0.45, is_local=True,
    ))
    # A remote/hive-served model that should NEVER be re-advertised
    reg.register(ModelBackend(
        model_id='hive-someone-else-qwen-27b',
        display_name='Hive (other peer)',
        tier=ModelTier.EXPERT,
        config_list_entry={
            'model': 'qwen-27b', 'api_key': 'tok',
            'base_url': 'https://other-peer.example.com/v1',
        },
        accuracy_score=0.88, is_local=False,
    ))
    return reg


@pytest.fixture
def advertiser(fresh_registry):
    from integrations.agent_engine.hive_capability_advertiser import (
        HiveCapabilityAdvertiser,
    )
    a = HiveCapabilityAdvertiser(registry=fresh_registry)
    yield a
    a.shutdown()


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Each test starts from a clean env so HEVOLVE_NODE_ID etc. don't
    leak between tests."""
    for var in [
        'HEVOLVE_HIVE_ADVERTISE',
        'HEVOLVE_HIVE_PUBLIC_ENDPOINT',
        'HEVOLVE_HIVE_AUTH_TOKEN',
        'HEVOLVE_HIVE_ADVERTISE_TIER',
        'HEVOLVE_NODE_ID',
        'HEVOLVE_HIVE_TRUSTED_PEERS',
    ]:
        monkeypatch.delenv(var, raising=False)


# ─────────────────────────────────────────────────────────────────────
# Opt-in predicate
# ─────────────────────────────────────────────────────────────────────


class TestOptInPredicate:

    def test_default_off(self):
        from integrations.agent_engine.hive_capability_advertiser import (
            _enabled,
        )
        assert _enabled() is False

    @pytest.mark.parametrize('truthy', ['1', 'true', 'True', 'YES', 'on'])
    def test_truthy_enables(self, monkeypatch, truthy):
        from integrations.agent_engine.hive_capability_advertiser import (
            _enabled,
        )
        monkeypatch.setenv('HEVOLVE_HIVE_ADVERTISE', truthy)
        assert _enabled() is True

    @pytest.mark.parametrize(
        'falsy', ['', '0', 'false', 'no', 'off', 'maybe'])
    def test_falsy_disables(self, monkeypatch, falsy):
        from integrations.agent_engine.hive_capability_advertiser import (
            _enabled,
        )
        monkeypatch.setenv('HEVOLVE_HIVE_ADVERTISE', falsy)
        assert _enabled() is False


# ─────────────────────────────────────────────────────────────────────
# Identity helpers
# ─────────────────────────────────────────────────────────────────────


class TestIdentityHelpers:

    def test_peer_id_explicit(self, monkeypatch):
        from integrations.agent_engine.hive_capability_advertiser import (
            _local_peer_id,
        )
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'node-prod-7')
        assert _local_peer_id() == 'node-prod-7'

    def test_peer_id_local_sentinel(self, monkeypatch):
        """The 'local' sentinel is the default in hart_intelligence_entry —
        treat it as "unset" so the advertiser uses a generated UUID
        instead of advertising under the literal string 'local'."""
        from integrations.agent_engine.hive_capability_advertiser import (
            _local_peer_id,
        )
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'local')
        assert _local_peer_id() == ''
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'LOCAL')
        assert _local_peer_id() == ''

    def test_peer_id_unset(self):
        from integrations.agent_engine.hive_capability_advertiser import (
            _local_peer_id,
        )
        assert _local_peer_id() == ''

    def test_endpoint_trims(self, monkeypatch):
        from integrations.agent_engine.hive_capability_advertiser import (
            _local_endpoint,
        )
        monkeypatch.setenv(
            'HEVOLVE_HIVE_PUBLIC_ENDPOINT',
            '  https://node.example.com/  ')
        assert _local_endpoint() == 'https://node.example.com'

    def test_endpoint_unset(self):
        from integrations.agent_engine.hive_capability_advertiser import (
            _local_endpoint,
        )
        assert _local_endpoint() == ''

    def test_auth_token_trim(self, monkeypatch):
        from integrations.agent_engine.hive_capability_advertiser import (
            _local_auth_token,
        )
        monkeypatch.setenv('HEVOLVE_HIVE_AUTH_TOKEN', '  tok-xyz  ')
        assert _local_auth_token() == 'tok-xyz'

    @pytest.mark.parametrize('tier_value,expected_name', [
        ('expert', 'EXPERT'),
        ('balanced', 'BALANCED'),
        ('fast', 'FAST'),
        ('draft', 'DRAFT'),
        ('EXPERT', 'EXPERT'),  # case-insensitive
    ])
    def test_tier_floor_recognised(
            self, monkeypatch, tier_value, expected_name):
        from integrations.agent_engine.hive_capability_advertiser import (
            _resolve_tier_floor,
        )
        from integrations.agent_engine.model_registry import ModelTier
        monkeypatch.setenv('HEVOLVE_HIVE_ADVERTISE_TIER', tier_value)
        assert _resolve_tier_floor() is getattr(ModelTier, expected_name)

    def test_tier_floor_invalid_falls_back_to_expert(self, monkeypatch):
        from integrations.agent_engine.hive_capability_advertiser import (
            _resolve_tier_floor,
        )
        from integrations.agent_engine.model_registry import ModelTier
        monkeypatch.setenv('HEVOLVE_HIVE_ADVERTISE_TIER', 'unknown')
        assert _resolve_tier_floor() is ModelTier.EXPERT

    def test_tier_floor_unset(self):
        from integrations.agent_engine.hive_capability_advertiser import (
            _resolve_tier_floor,
        )
        from integrations.agent_engine.model_registry import ModelTier
        assert _resolve_tier_floor() is ModelTier.EXPERT


# ─────────────────────────────────────────────────────────────────────
# attach() refusal semantics
# ─────────────────────────────────────────────────────────────────────


class TestAttachRefusal:

    def test_refuses_without_optin(self, advertiser):
        """Default: refuses to start the loop."""
        assert advertiser.attach() is False
        assert advertiser._attached is False

    def test_refuses_without_endpoint(self, advertiser, monkeypatch):
        monkeypatch.setenv('HEVOLVE_HIVE_ADVERTISE', '1')
        assert advertiser.attach() is False
        assert advertiser._attached is False

    def test_attach_idempotent(self, advertiser, monkeypatch):
        monkeypatch.setenv('HEVOLVE_HIVE_ADVERTISE', '1')
        monkeypatch.setenv(
            'HEVOLVE_HIVE_PUBLIC_ENDPOINT', 'https://x.example.com')
        # Mock the worker pool so we don't actually start a thread
        with patch.object(advertiser._pool, 'submit') as submit:
            assert advertiser.attach() is True
            # Second call is no-op
            assert advertiser.attach() is False
        # Pool submitted the loop exactly once
        assert submit.call_count == 1


# ─────────────────────────────────────────────────────────────────────
# _peer_id resolution
# ─────────────────────────────────────────────────────────────────────


class TestPeerIdResolution:

    def test_explicit_node_id_used(self, advertiser, monkeypatch):
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'node-prod-7')
        assert advertiser._peer_id() == 'node-prod-7'

    def test_fallback_uuid_stable_across_calls(self, advertiser):
        """When HEVOLVE_NODE_ID isn't set, a per-process UUID is
        generated lazily — must be stable across multiple calls."""
        first = advertiser._peer_id()
        second = advertiser._peer_id()
        assert first == second
        assert first.startswith('auto-')

    def test_fallback_uuid_unique_per_instance(self, fresh_registry):
        from integrations.agent_engine.hive_capability_advertiser import (
            HiveCapabilityAdvertiser,
        )
        a = HiveCapabilityAdvertiser(registry=fresh_registry)
        b = HiveCapabilityAdvertiser(registry=fresh_registry)
        # Different instances → different fallback IDs
        assert a._peer_id() != b._peer_id()
        a.shutdown()
        b.shutdown()


# ─────────────────────────────────────────────────────────────────────
# Model enumeration
# ─────────────────────────────────────────────────────────────────────


class TestModelEnumeration:

    def test_only_local_models(self, advertiser):
        """The remote 'hive-someone-else-qwen-27b' must NOT appear in
        the advertised payload — we don't re-broadcast other peers'
        capabilities."""
        models = advertiser._enumerate_models()
        ids = {m['model_id'] for m in models}
        assert 'hive-someone-else-qwen-27b' not in ids

    def test_default_tier_floor_is_expert(self, advertiser):
        """With unset HEVOLVE_HIVE_ADVERTISE_TIER, only EXPERT-tier
        models surface — the 4B FAST and 0.8B DRAFT stay home."""
        models = advertiser._enumerate_models()
        ids = {m['model_id'] for m in models}
        assert ids == {'qwen-27b-local'}

    def test_lowered_tier_floor_admits_fast(
            self, advertiser, monkeypatch):
        monkeypatch.setenv('HEVOLVE_HIVE_ADVERTISE_TIER', 'fast')
        models = advertiser._enumerate_models()
        ids = {m['model_id'] for m in models}
        assert ids == {'qwen-27b-local', 'qwen-4b-local'}

    def test_lowered_tier_admits_balanced_not_fast(
            self, advertiser, monkeypatch, fresh_registry):
        from integrations.agent_engine.model_registry import (
            ModelBackend, ModelTier,
        )
        fresh_registry.register(ModelBackend(
            model_id='qwen-11b-balanced', display_name='Balanced',
            tier=ModelTier.BALANCED,
            config_list_entry={'model': 'qwen-11b'},
            accuracy_score=0.7, is_local=True,
        ))
        monkeypatch.setenv('HEVOLVE_HIVE_ADVERTISE_TIER', 'balanced')
        models = advertiser._enumerate_models()
        ids = {m['model_id'] for m in models}
        assert 'qwen-11b-balanced' in ids
        assert 'qwen-27b-local' in ids
        assert 'qwen-4b-local' not in ids

    def test_below_accuracy_floor_dropped(
            self, advertiser, fresh_registry, monkeypatch):
        from integrations.agent_engine.model_registry import (
            ModelBackend, ModelTier,
        )
        fresh_registry.register(ModelBackend(
            model_id='garbage-expert', display_name='Bad',
            tier=ModelTier.EXPERT,
            config_list_entry={'model': 'garbage'},
            accuracy_score=0.3, is_local=True,  # below floor (0.5)
        ))
        models = advertiser._enumerate_models()
        ids = {m['model_id'] for m in models}
        assert 'garbage-expert' not in ids

    def test_all_advertised_stamped_tier_expert(self, advertiser, monkeypatch):
        """Consumer side registers ``tier='expert'`` entries
        exclusively, so the producer normalises whatever local tier
        the model has into 'expert' for the wire format."""
        monkeypatch.setenv('HEVOLVE_HIVE_ADVERTISE_TIER', 'fast')
        models = advertiser._enumerate_models()
        assert len(models) == 2
        for m in models:
            assert m['tier'] == 'expert'

    def test_specialty_propagates(self, advertiser):
        models = advertiser._enumerate_models()
        qwen = next(m for m in models
                    if m['model_id'] == 'qwen-27b-local')
        assert qwen['specialty'] == ['coding', 'reasoning']


# ─────────────────────────────────────────────────────────────────────
# Payload assembly
# ─────────────────────────────────────────────────────────────────────


class TestBuildPayload:

    def test_returns_none_no_endpoint(self, advertiser, monkeypatch):
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'node-x')
        # No endpoint set
        assert advertiser._build_payload() is None

    def test_returns_none_no_qualified_models(
            self, monkeypatch):
        from integrations.agent_engine.model_registry import ModelRegistry
        from integrations.agent_engine.hive_capability_advertiser import (
            HiveCapabilityAdvertiser,
        )
        empty_reg = ModelRegistry()
        adv = HiveCapabilityAdvertiser(registry=empty_reg)
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'node-x')
        monkeypatch.setenv(
            'HEVOLVE_HIVE_PUBLIC_ENDPOINT', 'https://x.example.com')
        assert adv._build_payload() is None
        adv.shutdown()

    def test_well_formed_payload(self, advertiser, monkeypatch):
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'node-x')
        monkeypatch.setenv(
            'HEVOLVE_HIVE_PUBLIC_ENDPOINT', 'https://node-x.example.com')
        monkeypatch.setenv('HEVOLVE_HIVE_AUTH_TOKEN', 'tok-xyz')

        before = time.time()
        payload = advertiser._build_payload()
        after = time.time()

        assert payload['peer_id'] == 'node-x'
        assert payload['endpoint'] == 'https://node-x.example.com'
        assert payload['auth_token'] == 'tok-xyz'
        assert payload['trust_signature'] == ''
        assert isinstance(payload['models'], list)
        assert len(payload['models']) == 1
        assert before <= payload['announced_at'] <= after


# ─────────────────────────────────────────────────────────────────────
# Emit + lifecycle
# ─────────────────────────────────────────────────────────────────────


class TestEmitAndLifecycle:

    def test_emit_announce_uses_eventbus(
            self, advertiser, monkeypatch):
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'node-x')
        monkeypatch.setenv(
            'HEVOLVE_HIVE_PUBLIC_ENDPOINT', 'https://x.example.com')
        fake_bus = MagicMock()
        fake_reg = MagicMock()
        fake_reg.has.return_value = True
        fake_reg.get.return_value = fake_bus
        with patch(
            'core.platform.registry.get_registry',
            return_value=fake_reg,
        ):
            assert advertiser._emit_announce() is True
        fake_bus.emit.assert_called_once()
        topic, payload = fake_bus.emit.call_args.args
        assert topic == 'peer.capability.announce'
        assert payload['peer_id'] == 'node-x'

    def test_emit_revoke_uses_eventbus(
            self, advertiser, monkeypatch):
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'node-x')
        fake_bus = MagicMock()
        fake_reg = MagicMock()
        fake_reg.has.return_value = True
        fake_reg.get.return_value = fake_bus
        with patch(
            'core.platform.registry.get_registry',
            return_value=fake_reg,
        ):
            assert advertiser._emit_revoke() is True
        fake_bus.emit.assert_called_once()
        topic, payload = fake_bus.emit.call_args.args
        assert topic == 'peer.capability.revoke'
        assert payload['peer_id'] == 'node-x'
        assert 'revoked_at' in payload

    def test_emit_noop_when_bus_not_bootstrapped(self, advertiser):
        fake_reg = MagicMock()
        fake_reg.has.return_value = False
        with patch(
            'core.platform.registry.get_registry',
            return_value=fake_reg,
        ):
            assert advertiser._emit_announce() is False

    def test_emit_swallows_exception(
            self, advertiser, monkeypatch):
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'node-x')
        monkeypatch.setenv(
            'HEVOLVE_HIVE_PUBLIC_ENDPOINT', 'https://x.example.com')
        fake_bus = MagicMock()
        fake_bus.emit.side_effect = RuntimeError('boom')
        fake_reg = MagicMock()
        fake_reg.has.return_value = True
        fake_reg.get.return_value = fake_bus
        with patch(
            'core.platform.registry.get_registry',
            return_value=fake_reg,
        ):
            assert advertiser._emit_announce() is False

    def test_shutdown_before_attach_safe(self, advertiser):
        """No revoke emit when never attached — calling shutdown on a
        cold instance must NOT raise + must NOT emit anything."""
        with patch.object(advertiser, '_emit_revoke') as revoke:
            advertiser.shutdown()
        revoke.assert_not_called()

    def test_shutdown_after_attach_emits_revoke(
            self, advertiser, monkeypatch):
        monkeypatch.setenv('HEVOLVE_HIVE_ADVERTISE', '1')
        monkeypatch.setenv(
            'HEVOLVE_HIVE_PUBLIC_ENDPOINT', 'https://x.example.com')
        # Block the worker submit so the test doesn't race the loop
        with patch.object(advertiser._pool, 'submit'):
            advertiser.attach()
        with patch.object(advertiser, '_emit_revoke') as revoke:
            advertiser.shutdown()
        revoke.assert_called_once()

    def test_shutdown_idempotent(
            self, advertiser, monkeypatch):
        monkeypatch.setenv('HEVOLVE_HIVE_ADVERTISE', '1')
        monkeypatch.setenv(
            'HEVOLVE_HIVE_PUBLIC_ENDPOINT', 'https://x.example.com')
        with patch.object(advertiser._pool, 'submit'):
            advertiser.attach()
        # Second shutdown is a no-op (no double-revoke)
        with patch.object(advertiser, '_emit_revoke') as revoke:
            advertiser.shutdown()
            advertiser.shutdown()
        assert revoke.call_count == 1


# ─────────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────────


class TestSingleton:

    def test_singleton_returns_same_instance(self):
        from integrations.agent_engine.hive_capability_advertiser import (
            get_hive_capability_advertiser,
        )
        a = get_hive_capability_advertiser()
        b = get_hive_capability_advertiser()
        assert a is b


# ─────────────────────────────────────────────────────────────────────
# Consumer-side self-echo guard (regression test)
# ─────────────────────────────────────────────────────────────────────


class TestSelfEchoGuard:
    """The consumer's HiveExpertDiscovery.on_peer_announce checks
    peer_id against HEVOLVE_NODE_ID and short-circuits.  Without this,
    the producer's own announce (which EventBus fans out locally
    before crossbar relays it) would feed back into discovery and
    register the local models as is_local=False hive backends —
    duplicates of entries already present as is_local=True."""

    def test_self_announce_skipped(self, monkeypatch):
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        from integrations.agent_engine.model_registry import ModelRegistry
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'node-x')
        monkeypatch.setenv('HEVOLVE_HIVE_TRUSTED_PEERS', 'node-x')
        disc = HiveExpertDiscovery(registry=ModelRegistry())
        try:
            count = disc.on_peer_announce({
                'peer_id': 'node-x',  # matches HEVOLVE_NODE_ID
                'endpoint': 'https://node-x.example.com',
                'auth_token': 'tok',
                'trust_signature': '',
                'models': [{
                    'model_id': 'qwen-27b', 'tier': 'expert',
                    'verified_baseline': 0.8, 'display_name': 'Q',
                }],
            })
            assert count == 0
            assert len(disc._registry._models) == 0
        finally:
            disc.shutdown()

    def test_local_sentinel_does_not_self_block(self, monkeypatch):
        """When HEVOLVE_NODE_ID='local' (the default in
        hart_intelligence_entry), the self-echo guard must NOT match
        any incoming announce — 'local' is the fallback, not a real
        peer_id.  An advertiser using 'local' would have already
        generated a UUID instead, so the announce's peer_id is the
        UUID and the consumer's literal 'local' should not collide."""
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        from integrations.agent_engine.model_registry import ModelRegistry
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'local')
        monkeypatch.setenv(
            'HEVOLVE_HIVE_TRUSTED_PEERS', 'auto-abc123')
        disc = HiveExpertDiscovery(registry=ModelRegistry())
        try:
            with patch(
                'integrations.agent_engine.hive_expert_discovery.'
                'HiveExpertDiscovery._ping_latency',
                return_value=42.0,
            ):
                count = disc.on_peer_announce({
                    'peer_id': 'auto-abc123',
                    'endpoint': 'https://other.example.com',
                    'auth_token': 'tok',
                    'trust_signature': '',
                    'models': [{
                        'model_id': 'qwen-27b', 'tier': 'expert',
                        'verified_baseline': 0.8, 'display_name': 'Q',
                    }],
                })
            assert count == 1
        finally:
            disc.shutdown()

    def test_different_peer_registers(self, monkeypatch):
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        from integrations.agent_engine.model_registry import ModelRegistry
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'node-x')
        monkeypatch.setenv('HEVOLVE_HIVE_TRUSTED_PEERS', 'node-y')
        disc = HiveExpertDiscovery(registry=ModelRegistry())
        try:
            with patch(
                'integrations.agent_engine.hive_expert_discovery.'
                'HiveExpertDiscovery._ping_latency',
                return_value=42.0,
            ):
                count = disc.on_peer_announce({
                    'peer_id': 'node-y',  # different from HEVOLVE_NODE_ID
                    'endpoint': 'https://node-y.example.com',
                    'auth_token': 'tok',
                    'trust_signature': '',
                    'models': [{
                        'model_id': 'qwen-27b', 'tier': 'expert',
                        'verified_baseline': 0.8, 'display_name': 'Q',
                    }],
                })
            assert count == 1
        finally:
            disc.shutdown()
