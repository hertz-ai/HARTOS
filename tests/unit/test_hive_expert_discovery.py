"""Unit tests for ``integrations.agent_engine.hive_expert_discovery``.

Coverage matrix:

  Trust gate
    - env-var allowlist: hit, miss, unset, whitespace, empty entries
    - attestation: ImportError fallback (current production state),
      future-proof when symbol exists

  Announce parsing
    - missing peer_id / endpoint → rejected
    - empty / non-list / non-dict models → rejected gracefully
    - non-expert tier filtered out
    - baseline below floor filtered out
    - non-numeric baseline filtered out
    - well-formed payload → registers backend, returns count

  Diff logic on re-announce
    - overlapping IDs → no churn (no drop+re-register)
    - subset of previous → drops the missing ones
    - empty models → drops everything, peer entry stays in _peer_models
      (so subsequent revoke still finds the peer)

  Health loop
    - 1 ping failure → fail_count=1, not dropped
    - N fail-budget failures → dropped via _drop_peer
    - successful ping after failure → fail_count resets to 0
    - _stop event terminates loop within one tick

  Lifecycle
    - attach_to_event_bus idempotent
    - attach_to_event_bus no-op when bus not bootstrapped
    - shutdown unsubscribes via bus.off (regression test for the
      reviewer-found leak)
    - shutdown idempotent
    - _drop_peer on unknown peer is safe + returns 0
"""
from __future__ import annotations

import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def fresh_registry():
    from integrations.agent_engine.model_registry import ModelRegistry
    return ModelRegistry()


@pytest.fixture
def discovery(fresh_registry):
    """Isolated HiveExpertDiscovery bound to a fresh registry.

    Health loop is NOT started (no attach_to_event_bus call).  Tests
    that need the loop construct their own.
    """
    from integrations.agent_engine.hive_expert_discovery import (
        HiveExpertDiscovery,
    )
    d = HiveExpertDiscovery(registry=fresh_registry)
    yield d
    d.shutdown()


def _announce(peer_id='node-a', endpoint='https://node-a.example.com',
              models=None, **extra):
    """Build a well-formed announce payload + apply overrides."""
    if models is None:
        models = [{
            'model_id': 'qwen-27b',
            'display_name': 'Qwen 27B',
            'tier': 'expert',
            'verified_baseline': 0.85,
        }]
    payload = {
        'peer_id': peer_id,
        'endpoint': endpoint,
        'auth_token': 'token-xyz',
        'trust_signature': 'sig',
        'models': models,
    }
    payload.update(extra)
    return payload


def _mock_ping(latency_ms=42.0):
    """Force the reachability probe to return a specific latency."""
    return patch(
        'integrations.agent_engine.hive_expert_discovery.'
        'HiveExpertDiscovery._ping_latency',
        return_value=latency_ms,
    )


def _allowlist(*peer_ids):
    """Set ``HEVOLVE_HIVE_TRUSTED_PEERS`` for the duration of the test."""
    return patch.dict(
        os.environ,
        {'HEVOLVE_HIVE_TRUSTED_PEERS': ','.join(peer_ids)},
    )


# ─────────────────────────────────────────────────────────────────────
# Trust gate
# ─────────────────────────────────────────────────────────────────────


class TestTrustGate:

    def test_allowlist_hit(self, discovery):
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        with _allowlist('node-a', 'node-b'):
            assert HiveExpertDiscovery._verify_peer_trust(
                {'peer_id': 'node-a'}) is True

    def test_allowlist_miss(self, discovery):
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        with _allowlist('node-a'):
            assert HiveExpertDiscovery._verify_peer_trust(
                {'peer_id': 'node-z'}) is False

    def test_unset_env_denies(self, discovery, monkeypatch):
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        monkeypatch.delenv('HEVOLVE_HIVE_TRUSTED_PEERS', raising=False)
        assert HiveExpertDiscovery._verify_peer_trust(
            {'peer_id': 'node-a'}) is False

    def test_empty_peer_id_denied(self, discovery):
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        with _allowlist('node-a'):
            assert HiveExpertDiscovery._verify_peer_trust(
                {'peer_id': ''}) is False
            assert HiveExpertDiscovery._verify_peer_trust(
                {'peer_id': None}) is False
            assert HiveExpertDiscovery._verify_peer_trust({}) is False

    def test_allowlist_whitespace_tolerated(self, discovery):
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        with patch.dict(os.environ, {
            'HEVOLVE_HIVE_TRUSTED_PEERS': '  node-a , node-b ,, ,  ',
        }):
            assert HiveExpertDiscovery._verify_peer_trust(
                {'peer_id': 'node-a'}) is True
            assert HiveExpertDiscovery._verify_peer_trust(
                {'peer_id': 'node-b'}) is True
            # The empty-token entries between commas must NOT match an
            # empty peer_id
            assert HiveExpertDiscovery._verify_peer_trust(
                {'peer_id': ''}) is False

    def test_attestation_path_when_symbol_lands(
            self, discovery, monkeypatch):
        """When ``verify_peer_attestation`` becomes importable, the
        env-var fallback is bypassed and attestation is the source of
        truth.  Simulates a future state — proves the ImportError
        branch will retire cleanly."""
        import sys as _sys
        fake_module = MagicMock()
        fake_module.verify_peer_attestation = MagicMock(return_value=True)
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        # Inject ``verify_peer_attestation`` into the existing
        # ``security.key_delegation`` module (it ships, just doesn't
        # export the symbol yet).
        kd_module = _sys.modules.get('security.key_delegation')
        if kd_module is not None:
            monkeypatch.setattr(
                kd_module, 'verify_peer_attestation',
                fake_module.verify_peer_attestation,
                raising=False,
            )
        else:
            monkeypatch.setitem(_sys.modules,
                                'security.key_delegation', fake_module)
        ok = HiveExpertDiscovery._verify_peer_trust(
            {'peer_id': 'node-a', 'trust_signature': 'sig'})
        assert ok is True
        fake_module.verify_peer_attestation.assert_called_once()

    def test_attestation_exception_denies(
            self, discovery, monkeypatch):
        """If attestation raises a non-ImportError exception, the gate
        must DENY (never accept by accident)."""
        import sys as _sys
        kd_module = _sys.modules.get('security.key_delegation')

        def _boom(*a, **kw):
            raise RuntimeError('attestation backend down')
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        if kd_module is not None:
            monkeypatch.setattr(
                kd_module, 'verify_peer_attestation', _boom,
                raising=False,
            )
        else:
            fake = MagicMock()
            fake.verify_peer_attestation = _boom
            monkeypatch.setitem(
                _sys.modules, 'security.key_delegation', fake)
        assert HiveExpertDiscovery._verify_peer_trust(
            {'peer_id': 'node-a'}) is False


# ─────────────────────────────────────────────────────────────────────
# Announce payload parsing
# ─────────────────────────────────────────────────────────────────────


class TestAnnounceParsing:

    def test_missing_peer_id(self, discovery):
        with _allowlist('node-a'), _mock_ping():
            assert discovery.on_peer_announce(
                _announce(peer_id='')) == 0

    def test_missing_endpoint(self, discovery):
        with _allowlist('node-a'), _mock_ping():
            assert discovery.on_peer_announce(
                _announce(endpoint='')) == 0

    def test_trust_denied(self, discovery):
        with _allowlist('node-b'), _mock_ping():  # node-b allowed, not node-a
            assert discovery.on_peer_announce(_announce()) == 0

    def test_unreachable_first_probe(self, discovery):
        """Peer fails the reachability probe → no registration; the
        peer's next announce gets another shot."""
        with _allowlist('node-a'):
            with patch(
                'integrations.agent_engine.hive_expert_discovery.'
                'HiveExpertDiscovery._ping_latency',
                return_value=None,
            ):
                assert discovery.on_peer_announce(_announce()) == 0
        assert len(discovery._registry._models) == 0

    def test_models_not_list(self, discovery):
        with _allowlist('node-a'), _mock_ping():
            assert discovery.on_peer_announce(
                _announce(models='not-a-list')) == 0

    def test_model_not_dict(self, discovery):
        with _allowlist('node-a'), _mock_ping():
            assert discovery.on_peer_announce(
                _announce(models=['just a string'])) == 0

    def test_non_expert_tier_filtered_out(self, discovery):
        """Only ``tier=expert`` models register.  Mixed payloads keep
        the expert entries and silently drop the others."""
        with _allowlist('node-a'), _mock_ping():
            count = discovery.on_peer_announce(_announce(models=[
                {'model_id': 'qwen-27b', 'tier': 'expert',
                 'verified_baseline': 0.8, 'display_name': 'Q'},
                {'model_id': 'qwen-4b', 'tier': 'fast',
                 'verified_baseline': 0.6, 'display_name': 'Q4'},
            ]))
        assert count == 1
        assert 'hive-node-a-qwen-27b' in discovery._registry._models
        assert 'hive-node-a-qwen-4b' not in discovery._registry._models

    def test_below_baseline_floor_filtered(self, discovery):
        """``_MIN_VERIFIED_BASELINE=0.5`` floor — anything advertised
        below random-chance reasoning quality must not register."""
        with _allowlist('node-a'), _mock_ping():
            count = discovery.on_peer_announce(_announce(models=[
                {'model_id': 'untrusted', 'tier': 'expert',
                 'verified_baseline': 0.49, 'display_name': 'X'},
            ]))
        assert count == 0
        assert len(discovery._registry._models) == 0

    def test_non_numeric_baseline_silently_dropped(self, discovery):
        with _allowlist('node-a'), _mock_ping():
            count = discovery.on_peer_announce(_announce(models=[
                {'model_id': 'broken', 'tier': 'expert',
                 'verified_baseline': 'unparseable', 'display_name': 'X'},
            ]))
        assert count == 0

    def test_missing_model_id_filtered(self, discovery):
        with _allowlist('node-a'), _mock_ping():
            count = discovery.on_peer_announce(_announce(models=[
                {'tier': 'expert',
                 'verified_baseline': 0.8, 'display_name': 'X'},
            ]))
        assert count == 0

    def test_well_formed_payload_registers(self, discovery):
        with _allowlist('node-a'), _mock_ping(latency_ms=120):
            count = discovery.on_peer_announce(_announce())
        assert count == 1
        backend = discovery._registry.get_model('hive-node-a-qwen-27b')
        assert backend is not None
        assert backend.is_local is False
        assert backend.tier.value == 'expert'
        assert backend.avg_latency_ms == 120
        assert backend.accuracy_score == 0.85
        assert backend.config_list_entry['base_url'] == (
            'https://node-a.example.com/v1')

    def test_specialty_propagated(self, discovery):
        with _allowlist('node-a'), _mock_ping():
            discovery.on_peer_announce(_announce(models=[
                {'model_id': 'coder', 'tier': 'expert',
                 'verified_baseline': 0.9, 'display_name': 'C',
                 'specialty': ['coding', 'reasoning']},
            ]))
        backend = discovery._registry.get_model('hive-node-a-coder')
        assert backend.config_list_entry['specialty'] == [
            'coding', 'reasoning']


# ─────────────────────────────────────────────────────────────────────
# Diff logic on re-announce
# ─────────────────────────────────────────────────────────────────────


class TestReannounceDiff:

    def test_overlapping_ids_no_churn(self, discovery):
        """Same peer re-announces the same models → register overwrites
        idempotently, no drop+re-register sequence."""
        with _allowlist('node-a'), _mock_ping():
            discovery.on_peer_announce(_announce())
            with patch.object(discovery._registry,
                              'unregister') as unreg:
                discovery.on_peer_announce(_announce())
        unreg.assert_not_called()

    def test_subset_drops_missing(self, discovery):
        """Peer reduces its model set → the missing ones get
        unregistered, surviving ones stay."""
        with _allowlist('node-a'), _mock_ping():
            discovery.on_peer_announce(_announce(models=[
                {'model_id': 'qwen-27b', 'tier': 'expert',
                 'verified_baseline': 0.8, 'display_name': 'Q'},
                {'model_id': 'mistral-22b', 'tier': 'expert',
                 'verified_baseline': 0.75, 'display_name': 'M'},
            ]))
            assert 'hive-node-a-mistral-22b' in discovery._registry._models
            discovery.on_peer_announce(_announce(models=[
                {'model_id': 'qwen-27b', 'tier': 'expert',
                 'verified_baseline': 0.8, 'display_name': 'Q'},
            ]))
        assert 'hive-node-a-qwen-27b' in discovery._registry._models
        assert 'hive-node-a-mistral-22b' not in (
            discovery._registry._models)

    def test_empty_announce_drops_all(self, discovery):
        """Peer announces zero expert models → all its backends drop.
        The peer entry survives in _peer_models with an empty set, so
        a later revoke is still well-formed."""
        with _allowlist('node-a'), _mock_ping():
            discovery.on_peer_announce(_announce())
            assert 'hive-node-a-qwen-27b' in discovery._registry._models
            discovery.on_peer_announce(_announce(models=[]))
        assert 'hive-node-a-qwen-27b' not in discovery._registry._models
        assert discovery._peer_models.get('node-a') == set()


# ─────────────────────────────────────────────────────────────────────
# Revoke handling
# ─────────────────────────────────────────────────────────────────────


class TestRevoke:

    def test_revoke_drops_all_peer_backends(self, discovery):
        with _allowlist('node-a'), _mock_ping():
            discovery.on_peer_announce(_announce())
        # Simulate the revoke event-callback shape (topic, data)
        discovery._on_revoke_event(
            'peer.capability.revoke', {'peer_id': 'node-a'})
        assert discovery._peer_models.get('node-a') is None
        assert 'hive-node-a-qwen-27b' not in (
            discovery._registry._models)

    def test_revoke_unknown_peer_safe(self, discovery):
        # No registration, no exception
        discovery._on_revoke_event(
            'peer.capability.revoke', {'peer_id': 'never-registered'})
        assert len(discovery._registry._models) == 0

    def test_revoke_malformed_payload_safe(self, discovery):
        discovery._on_revoke_event(
            'peer.capability.revoke', 'not-a-dict')
        discovery._on_revoke_event(
            'peer.capability.revoke', {})
        # No exception, no state mutation


# ─────────────────────────────────────────────────────────────────────
# Health-check loop
# ─────────────────────────────────────────────────────────────────────


class TestHealthCheck:

    def test_single_failure_does_not_drop(self, discovery):
        with _allowlist('node-a'), _mock_ping():
            discovery.on_peer_announce(_announce())
        with patch(
            'integrations.agent_engine.hive_expert_discovery.'
            'HiveExpertDiscovery._ping_latency',
            return_value=None,
        ):
            discovery._check_one_peer('node-a')
        assert discovery._fail_count['node-a'] == 1
        assert 'hive-node-a-qwen-27b' in discovery._registry._models

    def test_drop_after_fail_budget(self, discovery):
        from integrations.agent_engine import hive_expert_discovery as hed
        with _allowlist('node-a'), _mock_ping():
            discovery.on_peer_announce(_announce())
        with patch(
            'integrations.agent_engine.hive_expert_discovery.'
            'HiveExpertDiscovery._ping_latency',
            return_value=None,
        ):
            for _ in range(hed._HEALTH_CHECK_FAIL_BUDGET):
                discovery._check_one_peer('node-a')
        assert 'hive-node-a-qwen-27b' not in discovery._registry._models
        assert discovery._peer_models.get('node-a') is None

    def test_success_resets_fail_count(self, discovery):
        with _allowlist('node-a'), _mock_ping():
            discovery.on_peer_announce(_announce())
        # First two pings fail
        with patch(
            'integrations.agent_engine.hive_expert_discovery.'
            'HiveExpertDiscovery._ping_latency',
            return_value=None,
        ):
            discovery._check_one_peer('node-a')
            discovery._check_one_peer('node-a')
        assert discovery._fail_count['node-a'] == 2
        # Successful ping → counter resets
        with _mock_ping(latency_ms=50):
            discovery._check_one_peer('node-a')
        assert discovery._fail_count['node-a'] == 0

    def test_latency_update_propagates_to_backend(self, discovery):
        """Health-check ping latency feeds ModelRegistry.record_latency
        so the dispatcher's picker reflects live network conditions
        instead of the stale announce-time snapshot."""
        with _allowlist('node-a'), _mock_ping(latency_ms=100):
            discovery.on_peer_announce(_announce())
        # Backend has latency=100 from announce (constructor-set hint).
        backend = discovery._registry.get_model('hive-node-a-qwen-27b')
        assert backend.avg_latency_ms == 100
        # Health ping at 200ms — record_latency stamps the FIRST live
        # sample.  The announce-time value was a hint, not a sample, so
        # the running mean is just {200}.
        with _mock_ping(latency_ms=200):
            discovery._check_one_peer('node-a')
        assert backend.avg_latency_ms == 200
        # A second ping at 100ms — running mean over {200, 100} = 150.
        with _mock_ping(latency_ms=100):
            discovery._check_one_peer('node-a')
        assert backend.avg_latency_ms == 150

    def test_health_check_loop_terminates_on_stop(self, fresh_registry):
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        # Use a tiny interval so the test doesn't wait 60s
        import integrations.agent_engine.hive_expert_discovery as hed
        original_interval = hed._HEALTH_CHECK_INTERVAL_S
        hed._HEALTH_CHECK_INTERVAL_S = 0.05
        try:
            d = HiveExpertDiscovery(registry=fresh_registry)
            thread = threading.Thread(
                target=d._health_check_loop, daemon=True)
            thread.start()
            # Loop is running; signal stop
            d._stop.set()
            thread.join(timeout=1.0)
            assert not thread.is_alive(), 'loop did not honor _stop'
        finally:
            hed._HEALTH_CHECK_INTERVAL_S = original_interval


# ─────────────────────────────────────────────────────────────────────
# Lifecycle: attach + shutdown
# ─────────────────────────────────────────────────────────────────────


class TestLifecycle:

    def test_attach_idempotent(self, fresh_registry):
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        d = HiveExpertDiscovery(registry=fresh_registry)
        # Mock EventBus + ServiceRegistry to be bootstrapped
        fake_bus = MagicMock()
        fake_reg = MagicMock()
        fake_reg.has.return_value = True
        fake_reg.get.return_value = fake_bus
        with patch(
            'core.platform.registry.get_registry',
            return_value=fake_reg,
        ):
            assert d.attach_to_event_bus() is True
            # Second call → no-op
            assert d.attach_to_event_bus() is False
        # bus.on called exactly twice (announce + revoke), not four times
        assert fake_bus.on.call_count == 2
        d.shutdown()

    def test_attach_noop_when_bus_not_bootstrapped(self, fresh_registry):
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        d = HiveExpertDiscovery(registry=fresh_registry)
        fake_reg = MagicMock()
        fake_reg.has.return_value = False
        with patch(
            'core.platform.registry.get_registry',
            return_value=fake_reg,
        ):
            assert d.attach_to_event_bus() is False
        assert d._subscribed is False
        d.shutdown()

    def test_shutdown_unsubscribes(self, fresh_registry):
        """Regression test for the reviewer-found leak: shutdown must
        call bus.off so the old callbacks don't outlive the
        instance."""
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        d = HiveExpertDiscovery(registry=fresh_registry)
        fake_bus = MagicMock()
        fake_reg = MagicMock()
        fake_reg.has.return_value = True
        fake_reg.get.return_value = fake_bus
        with patch(
            'core.platform.registry.get_registry',
            return_value=fake_reg,
        ):
            d.attach_to_event_bus()
            d.shutdown()
        # off() called for both topics
        topics = [call.args[0] for call in fake_bus.off.call_args_list]
        assert 'peer.capability.announce' in topics
        assert 'peer.capability.revoke' in topics
        assert d._subscribed is False

    def test_shutdown_idempotent(self, fresh_registry):
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        d = HiveExpertDiscovery(registry=fresh_registry)
        # Never attached — shutdown still safe + repeatable
        d.shutdown()
        d.shutdown()
        d.shutdown()

    def test_drop_unknown_peer_safe(self, discovery):
        assert discovery._drop_peer('never-existed', reason='test') == 0

    def test_singleton_returns_same_instance(self):
        from integrations.agent_engine.hive_expert_discovery import (
            get_hive_expert_discovery,
        )
        a = get_hive_expert_discovery()
        b = get_hive_expert_discovery()
        assert a is b


# ─────────────────────────────────────────────────────────────────────
# Static helpers
# ─────────────────────────────────────────────────────────────────────


class TestStaticHelpers:

    def test_backend_id_format(self):
        from integrations.agent_engine.hive_expert_discovery import (
            _backend_id_for,
        )
        assert _backend_id_for('node-a', 'qwen-27b') == (
            'hive-node-a-qwen-27b')

    def test_ping_latency_treats_503_loading_as_alive(self):
        """Mirror of llama-server's contract (commit 3f9be3be) — HTTP
        503 with 'Loading' body means the model is still warming and
        the peer should be considered reachable."""
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        fake_resp = MagicMock()
        fake_resp.status_code = 503
        fake_resp.text = 'Loading model, please wait...'
        with patch('requests.get', return_value=fake_resp):
            latency = HiveExpertDiscovery._ping_latency(
                'https://node-a.example.com', 'tok')
        assert latency is not None
        assert latency >= 0

    def test_ping_latency_503_other_body_returns_none(self):
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        fake_resp = MagicMock()
        fake_resp.status_code = 503
        fake_resp.text = 'service unavailable'
        with patch('requests.get', return_value=fake_resp):
            latency = HiveExpertDiscovery._ping_latency(
                'https://node-a.example.com', 'tok')
        assert latency is None

    def test_ping_latency_empty_endpoint_returns_none(self):
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        assert HiveExpertDiscovery._ping_latency('', 'tok') is None

    def test_ping_latency_network_exception_returns_none(self):
        from integrations.agent_engine.hive_expert_discovery import (
            HiveExpertDiscovery,
        )
        import requests
        with patch(
            'requests.get',
            side_effect=requests.ConnectionError('refused'),
        ):
            assert HiveExpertDiscovery._ping_latency(
                'https://x.example.com', 'tok') is None
