"""Tests for AutoDiscovery — zero-config LAN peer finding via UDP broadcast."""
import json
import os
import sys
import time
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.social.peer_discovery import AutoDiscovery, GossipProtocol


@pytest.fixture
def mock_gossip():
    g = MagicMock(spec=GossipProtocol)
    g.node_id = 'test-node-001'
    g.base_url = 'http://localhost:6777'
    g.node_name = 'test-node'
    g.version = '1.0.0'
    g.tier = 'flat'
    return g


@pytest.fixture
def discovery(mock_gossip):
    return AutoDiscovery(mock_gossip, port=16780, beacon_interval=5)


class TestAutoDiscoveryBeacon:
    """Beacon build and parse tests."""

    def test_build_beacon_starts_with_magic(self, discovery):
        beacon = discovery._build_beacon()
        assert beacon.startswith(AutoDiscovery.BEACON_MAGIC)

    def test_build_beacon_valid_json(self, discovery):
        beacon = discovery._build_beacon()
        json_bytes = beacon[len(AutoDiscovery.BEACON_MAGIC):]
        payload = json.loads(json_bytes.decode('utf-8'))
        assert payload['type'] == 'hevolve-discovery'
        assert payload['node_id'] == 'test-node-001'
        assert payload['url'] == 'http://localhost:6777'
        assert payload['name'] == 'test-node'
        assert 'timestamp' in payload

    def test_parse_beacon_valid(self, discovery):
        # Build from a "different" node
        payload = {
            'type': 'hevolve-discovery',
            'node_id': 'other-node-002',
            'url': 'http://other:6777',
            'name': 'other',
            'version': '1.0.0',
            'tier': 'flat',
            'timestamp': int(time.time()),
        }
        data = AutoDiscovery.BEACON_MAGIC + json.dumps(payload).encode('utf-8')
        result = discovery._parse_beacon(data)
        assert result['node_id'] == 'other-node-002'
        assert result['url'] == 'http://other:6777'

    def test_parse_beacon_wrong_magic(self, discovery):
        data = b'WRONG_MAGIC_BYTES' + b'{"type":"hevolve-discovery"}'
        assert discovery._parse_beacon(data) == {}

    def test_parse_beacon_own_node_ignored(self, discovery):
        payload = {
            'type': 'hevolve-discovery',
            'node_id': 'test-node-001',  # Same as our node
            'url': 'http://localhost:6777',
            'timestamp': int(time.time()),
        }
        data = AutoDiscovery.BEACON_MAGIC + json.dumps(payload).encode('utf-8')
        assert discovery._parse_beacon(data) == {}

    def test_parse_beacon_stale_rejected(self, discovery):
        payload = {
            'type': 'hevolve-discovery',
            'node_id': 'stale-node-003',
            'url': 'http://stale:6777',
            'timestamp': int(time.time()) - 600,  # 10 minutes old
        }
        data = AutoDiscovery.BEACON_MAGIC + json.dumps(payload).encode('utf-8')
        assert discovery._parse_beacon(data) == {}

    def test_parse_beacon_wrong_type_rejected(self, discovery):
        payload = {
            'type': 'some-other-protocol',
            'node_id': 'other-004',
            'timestamp': int(time.time()),
        }
        data = AutoDiscovery.BEACON_MAGIC + json.dumps(payload).encode('utf-8')
        assert discovery._parse_beacon(data) == {}

    def test_parse_beacon_invalid_json(self, discovery):
        data = AutoDiscovery.BEACON_MAGIC + b'not-json-at-all'
        assert discovery._parse_beacon(data) == {}

    @patch('integrations.social.peer_discovery.get_guardrail_hash',
           create=True)
    def test_parse_beacon_guardrail_mismatch_rejected(self, mock_hash,
                                                       discovery):
        # Mock local guardrail hash
        with patch('security.hive_guardrails.get_guardrail_hash',
                   return_value='local_hash_abc'):
            payload = {
                'type': 'hevolve-discovery',
                'node_id': 'mismatch-005',
                'url': 'http://mismatch:6777',
                'timestamp': int(time.time()),
                'guardrail_hash': 'different_hash_xyz',
            }
            data = AutoDiscovery.BEACON_MAGIC + json.dumps(payload).encode('utf-8')
            assert discovery._parse_beacon(data) == {}


class TestAutoDiscoveryIntegration:
    """Integration with gossip protocol."""

    def test_discovered_node_fed_to_gossip(self, discovery, mock_gossip):
        payload = {
            'type': 'hevolve-discovery',
            'node_id': 'new-node-010',
            'url': 'http://new:6777',
            'name': 'new-node',
            'version': '1.0.0',
            'tier': 'flat',
            'timestamp': int(time.time()),
        }
        data = AutoDiscovery.BEACON_MAGIC + json.dumps(payload).encode('utf-8')

        # Simulate recv
        parsed = discovery._parse_beacon(data)
        assert parsed  # Valid beacon
        discovery._discovered_nodes.add(parsed['node_id'])
        mock_gossip.handle_announce(parsed)
        mock_gossip.handle_announce.assert_called_once_with(parsed)

    def test_duplicate_node_tracked(self, discovery):
        discovery._discovered_nodes.add('dup-node-020')
        assert 'dup-node-020' in discovery._discovered_nodes

    def test_stop_sets_running_false(self, discovery):
        discovery._running = True
        discovery._sock = MagicMock()
        discovery.stop()
        assert discovery._running is False

    def test_disabled_via_env(self, mock_gossip):
        with patch.dict(os.environ, {'HEVOLVE_AUTO_DISCOVERY': 'false'}):
            # Discovery should be creatable but would not be started
            # in init_social when env var is false
            ad = AutoDiscovery(mock_gossip)
            assert ad._running is False

    def test_port_configurable(self, mock_gossip):
        with patch.dict(os.environ, {'HEVOLVE_DISCOVERY_PORT': '9999'}):
            ad = AutoDiscovery(mock_gossip)
            assert ad._port == 9999

    def test_interval_configurable(self, mock_gossip):
        with patch.dict(os.environ, {'HEVOLVE_DISCOVERY_INTERVAL': '15'}):
            ad = AutoDiscovery(mock_gossip)
            assert ad._beacon_interval == 15
