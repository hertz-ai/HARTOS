"""
Tests for bandwidth-configurable gossip (Phase 3 — Embedded/Robot Support).

Tests bandwidth profiles, payload compaction, tier auto-selection,
and serialization helpers.
"""
import json
import os
import sys
import time
from unittest.mock import patch, MagicMock

import pytest


class TestBandwidthProfiles:
    """Verify bandwidth profile constants and structure."""

    def test_three_profiles_exist(self):
        from integrations.social.peer_discovery import BANDWIDTH_PROFILES
        assert 'full' in BANDWIDTH_PROFILES
        assert 'constrained' in BANDWIDTH_PROFILES
        assert 'minimal' in BANDWIDTH_PROFILES

    def test_full_profile_values(self):
        from integrations.social.peer_discovery import BANDWIDTH_PROFILES
        full = BANDWIDTH_PROFILES['full']
        assert full['gossip_interval'] == 60
        assert full['health_interval'] == 120
        assert full['gossip_fanout'] == 3
        assert full['payload_mode'] == 'json'

    def test_constrained_profile_values(self):
        from integrations.social.peer_discovery import BANDWIDTH_PROFILES
        c = BANDWIDTH_PROFILES['constrained']
        assert c['gossip_interval'] == 300
        assert c['health_interval'] == 600
        assert c['gossip_fanout'] == 2
        assert c['payload_mode'] == 'json_compact'

    def test_minimal_profile_values(self):
        from integrations.social.peer_discovery import BANDWIDTH_PROFILES
        m = BANDWIDTH_PROFILES['minimal']
        assert m['gossip_interval'] == 900
        assert m['health_interval'] == 1800
        assert m['gossip_fanout'] == 1
        assert m['payload_mode'] == 'msgpack'

    def test_profiles_have_required_keys(self):
        from integrations.social.peer_discovery import BANDWIDTH_PROFILES
        required = {'gossip_interval', 'health_interval', 'gossip_fanout',
                    'payload_mode', 'stale_threshold', 'dead_threshold'}
        for name, profile in BANDWIDTH_PROFILES.items():
            missing = required - set(profile.keys())
            assert not missing, f"Profile '{name}' missing keys: {missing}"

    def test_intervals_decrease_with_tier(self):
        """Minimal > constrained > full for all intervals."""
        from integrations.social.peer_discovery import BANDWIDTH_PROFILES
        f = BANDWIDTH_PROFILES['full']
        c = BANDWIDTH_PROFILES['constrained']
        m = BANDWIDTH_PROFILES['minimal']
        assert m['gossip_interval'] > c['gossip_interval'] > f['gossip_interval']
        assert m['health_interval'] > c['health_interval'] > f['health_interval']

    def test_fanout_decreases_with_tier(self):
        from integrations.social.peer_discovery import BANDWIDTH_PROFILES
        f = BANDWIDTH_PROFILES['full']
        c = BANDWIDTH_PROFILES['constrained']
        m = BANDWIDTH_PROFILES['minimal']
        assert f['gossip_fanout'] > c['gossip_fanout'] > m['gossip_fanout']


class TestTierBandwidthMapping:
    """Verify tier → bandwidth profile auto-selection."""

    def test_embedded_gets_minimal(self):
        from integrations.social.peer_discovery import _TIER_BANDWIDTH_MAP
        assert _TIER_BANDWIDTH_MAP['embedded'] == 'minimal'

    def test_observer_gets_constrained(self):
        from integrations.social.peer_discovery import _TIER_BANDWIDTH_MAP
        assert _TIER_BANDWIDTH_MAP['observer'] == 'constrained'

    def test_lite_gets_constrained(self):
        from integrations.social.peer_discovery import _TIER_BANDWIDTH_MAP
        assert _TIER_BANDWIDTH_MAP['lite'] == 'constrained'

    def test_standard_gets_full(self):
        from integrations.social.peer_discovery import _TIER_BANDWIDTH_MAP
        assert _TIER_BANDWIDTH_MAP['standard'] == 'full'

    def test_compute_host_gets_full(self):
        from integrations.social.peer_discovery import _TIER_BANDWIDTH_MAP
        assert _TIER_BANDWIDTH_MAP['compute_host'] == 'full'

    def test_flat_gets_full(self):
        from integrations.social.peer_discovery import _TIER_BANDWIDTH_MAP
        assert _TIER_BANDWIDTH_MAP['flat'] == 'full'


class TestGossipProtocolBandwidth:
    """Verify GossipProtocol respects bandwidth profiles."""

    def test_env_override_bandwidth(self):
        """HEVOLVE_GOSSIP_BANDWIDTH overrides tier auto-selection."""
        from integrations.social.peer_discovery import GossipProtocol
        with patch.dict(os.environ, {'HEVOLVE_GOSSIP_BANDWIDTH': 'minimal'}):
            gp = GossipProtocol()
            assert gp.bandwidth_profile == 'minimal'
            assert gp.gossip_interval == 900
            assert gp.health_interval == 1800
            assert gp.gossip_fanout == 1
            assert gp.payload_mode == 'msgpack'

    def test_env_override_constrained(self):
        from integrations.social.peer_discovery import GossipProtocol
        with patch.dict(os.environ, {'HEVOLVE_GOSSIP_BANDWIDTH': 'constrained'}):
            gp = GossipProtocol()
            assert gp.bandwidth_profile == 'constrained'
            assert gp.gossip_interval == 300
            assert gp.gossip_fanout == 2

    def test_env_override_full(self):
        from integrations.social.peer_discovery import GossipProtocol
        with patch.dict(os.environ, {'HEVOLVE_GOSSIP_BANDWIDTH': 'full'}):
            gp = GossipProtocol()
            assert gp.bandwidth_profile == 'full'
            assert gp.gossip_interval == 60
            assert gp.gossip_fanout == 3

    def test_individual_env_overrides_profile(self):
        """Individual env vars override profile defaults."""
        from integrations.social.peer_discovery import GossipProtocol
        with patch.dict(os.environ, {
            'HEVOLVE_GOSSIP_BANDWIDTH': 'minimal',
            'HEVOLVE_GOSSIP_INTERVAL': '120',  # Override minimal's 900
            'HEVOLVE_GOSSIP_FANOUT': '5',      # Override minimal's 1
        }):
            gp = GossipProtocol()
            assert gp.bandwidth_profile == 'minimal'
            assert gp.gossip_interval == 120  # Individual override
            assert gp.gossip_fanout == 5      # Individual override
            # But health stays at profile default
            assert gp.health_interval == 1800

    def test_unknown_profile_falls_back_to_full(self):
        from integrations.social.peer_discovery import GossipProtocol
        with patch.dict(os.environ, {'HEVOLVE_GOSSIP_BANDWIDTH': 'imaginary'}):
            gp = GossipProtocol()
            assert gp.gossip_interval == 60  # Full profile default


class TestCompactPayload:
    """Verify compact payload mode strips non-essential fields."""

    def test_compact_fields_defined(self):
        from integrations.social.peer_discovery import _COMPACT_FIELDS
        assert 'node_id' in _COMPACT_FIELDS
        assert 'url' in _COMPACT_FIELDS
        assert 'public_key' in _COMPACT_FIELDS
        assert 'guardrail_hash' in _COMPACT_FIELDS
        assert 'signature' in _COMPACT_FIELDS
        # Non-essential fields should NOT be in compact set
        assert 'name' not in _COMPACT_FIELDS
        assert 'version' not in _COMPACT_FIELDS
        assert 'agent_count' not in _COMPACT_FIELDS
        assert 'post_count' not in _COMPACT_FIELDS
        assert 'hardware_summary' not in _COMPACT_FIELDS

    def test_gossip_self_info_full(self):
        """Full mode returns complete self_info."""
        from integrations.social.peer_discovery import GossipProtocol
        with patch.dict(os.environ, {'HEVOLVE_GOSSIP_BANDWIDTH': 'full'}):
            gp = GossipProtocol()
            info = gp._gossip_self_info()
            # Full includes name, version, etc.
            assert 'name' in info
            assert 'version' in info
            assert 'node_id' in info

    def test_gossip_self_info_compact(self):
        """Compact mode strips non-essential fields."""
        from integrations.social.peer_discovery import GossipProtocol, _COMPACT_FIELDS
        with patch.dict(os.environ, {'HEVOLVE_GOSSIP_BANDWIDTH': 'constrained'}):
            gp = GossipProtocol()
            info = gp._gossip_self_info()
            # Only compact fields should be present
            for key in info:
                assert key in _COMPACT_FIELDS, \
                    f"Compact payload has non-compact key: {key}"
            # Must have node_id and url
            assert 'node_id' in info
            assert 'url' in info

    def test_compact_smaller_than_full(self):
        """Compact payload is smaller than full payload."""
        from integrations.social.peer_discovery import GossipProtocol
        with patch.dict(os.environ, {'HEVOLVE_GOSSIP_BANDWIDTH': 'full'}):
            gp_full = GossipProtocol()
            full_info = gp_full._gossip_self_info()
        with patch.dict(os.environ, {'HEVOLVE_GOSSIP_BANDWIDTH': 'constrained'}):
            gp_compact = GossipProtocol()
            compact_info = gp_compact._gossip_self_info()

        full_size = len(json.dumps(full_info))
        compact_size = len(json.dumps(compact_info))
        assert compact_size < full_size


class TestSerialization:
    """Verify payload serialization helpers."""

    def test_serialize_json_fallback(self):
        """Without msgpack, falls back to JSON."""
        from integrations.social.peer_discovery import GossipProtocol
        data = {'node_id': 'test123', 'url': 'http://localhost:6777'}
        # Force JSON fallback by hiding msgpack
        with patch.dict(sys.modules, {'msgpack': None}):
            raw = GossipProtocol._serialize_payload(data)
        # Should be valid JSON
        result = json.loads(raw.decode('utf-8'))
        assert result['node_id'] == 'test123'

    def test_deserialize_json_fallback(self):
        from integrations.social.peer_discovery import GossipProtocol
        data = {'node_id': 'test123', 'url': 'http://localhost:6777'}
        raw = json.dumps(data).encode('utf-8')
        result = GossipProtocol._deserialize_payload(raw)
        assert result['node_id'] == 'test123'

    def test_serialize_deserialize_roundtrip(self):
        from integrations.social.peer_discovery import GossipProtocol
        data = {
            'node_id': 'abc',
            'url': 'http://localhost:6777',
            'peers': [{'node_id': 'x', 'url': 'http://peer1'}],
        }
        raw = GossipProtocol._serialize_payload(data)
        result = GossipProtocol._deserialize_payload(raw)
        assert result == data

    def test_msgpack_if_available(self):
        """If msgpack is installed, serialization uses it."""
        try:
            import msgpack
            has_msgpack = True
        except ImportError:
            has_msgpack = False

        if has_msgpack:
            from integrations.social.peer_discovery import GossipProtocol
            data = {'key': 'value'}
            raw = GossipProtocol._serialize_payload(data)
            # msgpack produces binary, not JSON text
            try:
                json.loads(raw)
                is_json = True
            except (json.JSONDecodeError, UnicodeDecodeError):
                is_json = False
            assert not is_json, "Expected msgpack binary, got JSON"
        else:
            pytest.skip("msgpack not installed")


class TestGossipPeerListCompact:
    """Verify _gossip_peer_list() respects payload mode."""

    def test_full_peer_list(self):
        """Full mode returns peers with all fields."""
        from integrations.social.peer_discovery import GossipProtocol
        with patch.dict(os.environ, {'HEVOLVE_GOSSIP_BANDWIDTH': 'full'}):
            gp = GossipProtocol()
            # Mock get_peer_list to return test data
            test_peers = [
                {'node_id': 'a', 'url': 'http://a', 'name': 'Node A',
                 'version': '1.0', 'agent_count': 5},
            ]
            with patch.object(gp, 'get_peer_list', return_value=test_peers):
                result = gp._gossip_peer_list()
                assert result[0].get('name') == 'Node A'
                assert result[0].get('agent_count') == 5

    def test_compact_peer_list(self):
        """Compact mode strips non-essential fields from peer list."""
        from integrations.social.peer_discovery import GossipProtocol, _COMPACT_FIELDS
        with patch.dict(os.environ, {'HEVOLVE_GOSSIP_BANDWIDTH': 'constrained'}):
            gp = GossipProtocol()
            test_peers = [
                {'node_id': 'a', 'url': 'http://a', 'name': 'Node A',
                 'version': '1.0', 'agent_count': 5, 'post_count': 10},
            ]
            with patch.object(gp, 'get_peer_list', return_value=test_peers):
                result = gp._gossip_peer_list()
                for key in result[0]:
                    assert key in _COMPACT_FIELDS
                assert 'name' not in result[0]
                assert 'agent_count' not in result[0]


class TestDeadThresholds:
    """Verify stale/dead thresholds scale with bandwidth profile."""

    def test_full_thresholds(self):
        from integrations.social.peer_discovery import BANDWIDTH_PROFILES
        assert BANDWIDTH_PROFILES['full']['stale_threshold'] == 300
        assert BANDWIDTH_PROFILES['full']['dead_threshold'] == 900

    def test_constrained_thresholds(self):
        from integrations.social.peer_discovery import BANDWIDTH_PROFILES
        assert BANDWIDTH_PROFILES['constrained']['stale_threshold'] == 900
        assert BANDWIDTH_PROFILES['constrained']['dead_threshold'] == 2700

    def test_minimal_thresholds(self):
        from integrations.social.peer_discovery import BANDWIDTH_PROFILES
        assert BANDWIDTH_PROFILES['minimal']['stale_threshold'] == 2700
        assert BANDWIDTH_PROFILES['minimal']['dead_threshold'] == 7200

    def test_thresholds_scale_with_interval(self):
        """Dead threshold should be at least 3x gossip interval."""
        from integrations.social.peer_discovery import BANDWIDTH_PROFILES
        for name, profile in BANDWIDTH_PROFILES.items():
            assert profile['dead_threshold'] >= 3 * profile['gossip_interval'], \
                f"Profile '{name}': dead_threshold too low relative to gossip_interval"
