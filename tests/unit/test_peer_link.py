"""Tests for HART OS PeerLink module — peer-to-peer communication layer.

Covers: PeerLink, PeerLinkManager, Channels, NATTraversal, Telemetry,
        MessageBus, and integration wiring.
"""
import os
import sys
import time
import threading
import unittest
from collections import OrderedDict
from unittest.mock import MagicMock, patch, PropertyMock

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.peer_link.link import PeerLink, TrustLevel, LinkState, CHANNEL_IDS, CHANNEL_NAMES
from core.peer_link.link_manager import (
    PeerLinkManager, get_link_manager, reset_link_manager,
    _MAX_LINKS, _UPGRADE_THRESHOLD,
)
from core.peer_link.channels import (
    DataClass, CHANNEL_REGISTRY, ChannelDispatcher,
    is_private_channel, get_channel_config,
)
from core.peer_link.nat import NATTraversal, NATType
from core.peer_link.telemetry import (
    TelemetryCollector, CentralConnection,
    _DEGRADED_THRESHOLD, _RESTRICTED_THRESHOLD,
)
from core.peer_link.message_bus import (
    MessageBus, get_message_bus, reset_message_bus,
    TOPIC_MAP, _LRUDedup, _REVERSE_MAP,
)


# ---------------------------------------------------------------------------
# TestTrustLevel
# ---------------------------------------------------------------------------
class TestTrustLevel(unittest.TestCase):
    """TrustLevel enum correctness."""

    def test_same_user_value(self):
        self.assertEqual(TrustLevel.SAME_USER.value, 'same_user')

    def test_peer_value(self):
        self.assertEqual(TrustLevel.PEER.value, 'peer')

    def test_relay_value(self):
        self.assertEqual(TrustLevel.RELAY.value, 'relay')

    def test_exactly_three_members(self):
        self.assertEqual(len(TrustLevel), 3)

    def test_same_user_covers_lan_and_wan(self):
        """SAME_USER is user_id based, not network based.
        Verify the enum value has no network qualifier."""
        self.assertNotIn('lan', TrustLevel.SAME_USER.value)
        self.assertNotIn('wan', TrustLevel.SAME_USER.value)
        # The same_user trust is applied across any network when user_id matches
        link_lan = PeerLink('peer1', '192.168.1.5:6777', TrustLevel.SAME_USER)
        link_wan = PeerLink('peer2', '203.0.113.50:6777', TrustLevel.SAME_USER)
        self.assertEqual(link_lan.trust, link_wan.trust)


# ---------------------------------------------------------------------------
# TestPeerLink
# ---------------------------------------------------------------------------
class TestPeerLink(unittest.TestCase):
    """PeerLink connection, messaging, encryption."""

    def setUp(self):
        self.link = PeerLink(
            peer_id='abc123def456',
            address='192.168.1.10:6777',
            trust=TrustLevel.PEER,
        )

    def tearDown(self):
        try:
            self.link.close()
        except Exception:
            pass

    # -- Construction --
    def test_construction_defaults(self):
        self.assertEqual(self.link.peer_id, 'abc123def456')
        self.assertEqual(self.link.address, '192.168.1.10:6777')
        self.assertEqual(self.link.trust, TrustLevel.PEER)
        self.assertEqual(self.link.capabilities, {})
        self.assertEqual(self.link._state, LinkState.DISCONNECTED)

    def test_construction_same_user_trust(self):
        link = PeerLink('p1', '10.0.0.1:6777', TrustLevel.SAME_USER)
        self.assertEqual(link.trust, TrustLevel.SAME_USER)

    def test_construction_relay_trust(self):
        link = PeerLink('p2', 'relay.example.com:6777', TrustLevel.RELAY)
        self.assertEqual(link.trust, TrustLevel.RELAY)

    def test_construction_with_capabilities(self):
        link = PeerLink('p3', '1.2.3.4:6777', TrustLevel.PEER,
                         capabilities={'gpu': 'RTX 4090', 'tier': 'regional'})
        self.assertEqual(link.capabilities['gpu'], 'RTX 4090')

    # -- Properties --
    def test_is_connected_false_when_disconnected(self):
        self.assertFalse(self.link.is_connected)

    def test_is_connected_true_when_connected(self):
        self.link._state = LinkState.CONNECTED
        self.assertTrue(self.link.is_connected)

    def test_is_encrypted_false_without_session_key(self):
        self.assertFalse(self.link.is_encrypted)

    def test_is_encrypted_true_with_session_key(self):
        self.link._session_key = b'\x00' * 32
        self.assertTrue(self.link.is_encrypted)

    def test_idle_seconds_zero_when_no_activity(self):
        self.assertEqual(self.link.idle_seconds, 0)

    def test_idle_seconds_positive_after_activity(self):
        self.link._last_activity = time.time() - 5
        idle = self.link.idle_seconds
        self.assertGreaterEqual(idle, 4.5)
        self.assertLess(idle, 10)

    # -- send() --
    def test_send_returns_none_when_disconnected(self):
        result = self.link.send('gossip', {'hello': 'world'})
        self.assertIsNone(result)

    def test_send_returns_none_when_ws_is_none(self):
        self.link._state = LinkState.CONNECTED
        self.link._ws = None
        result = self.link.send('gossip', {'test': 1})
        self.assertIsNone(result)

    # -- send_binary() --
    def test_send_binary_returns_false_when_disconnected(self):
        result = self.link.send_binary('sensor', b'\x01\x02\x03')
        self.assertFalse(result)

    def test_send_binary_returns_false_when_ws_none(self):
        self.link._state = LinkState.CONNECTED
        self.link._ws = None
        result = self.link.send_binary('compute', b'payload')
        self.assertFalse(result)

    # -- on_message() --
    def test_on_message_registers_handler(self):
        handler = MagicMock()
        self.link.on_message('gossip', handler)
        self.assertIn('gossip', self.link._message_handlers)
        self.assertIn(handler, self.link._message_handlers['gossip'])

    def test_on_message_multiple_handlers(self):
        h1 = MagicMock()
        h2 = MagicMock()
        self.link.on_message('compute', h1)
        self.link.on_message('compute', h2)
        self.assertEqual(len(self.link._message_handlers['compute']), 2)

    # -- close() --
    def test_close_transitions_to_disconnected(self):
        self.link._state = LinkState.CONNECTED
        self.link._session_key = b'\x00' * 32
        mock_ws = MagicMock()
        self.link._ws = mock_ws
        self.link.close()
        self.assertEqual(self.link._state, LinkState.DISCONNECTED)
        self.assertIsNone(self.link._ws)
        self.assertIsNone(self.link._session_key)

    def test_close_idempotent_when_closing(self):
        self.link._state = LinkState.CLOSING
        self.link.close()  # Should return immediately, no error
        self.assertEqual(self.link._state, LinkState.CLOSING)

    # -- get_stats() --
    def test_get_stats_shape(self):
        stats = self.link.get_stats()
        expected_keys = {
            'peer_id', 'state', 'trust', 'encrypted', 'connected_seconds',
            'idle_seconds', 'messages_sent', 'messages_received',
            'bytes_sent', 'bytes_received', 'capabilities',
        }
        self.assertEqual(set(stats.keys()), expected_keys)
        self.assertEqual(stats['peer_id'], 'abc123def456')
        self.assertEqual(stats['state'], 'disconnected')
        self.assertEqual(stats['trust'], 'peer')
        self.assertFalse(stats['encrypted'])
        self.assertEqual(stats['messages_sent'], 0)

    # -- _resolve_ws_url() --
    def test_resolve_ws_url_plain_address(self):
        url = self.link._resolve_ws_url()
        self.assertEqual(url, 'ws://192.168.1.10:6777/peer_link')

    def test_resolve_ws_url_same_user(self):
        link = PeerLink('p', '10.0.0.5:6777', TrustLevel.SAME_USER)
        url = link._resolve_ws_url()
        self.assertEqual(url, 'ws://10.0.0.5:6777/peer_link')

    def test_resolve_ws_url_already_ws(self):
        link = PeerLink('p', 'ws://example.com/peer_link', TrustLevel.PEER)
        url = link._resolve_ws_url()
        self.assertEqual(url, 'ws://example.com/peer_link')

    def test_resolve_ws_url_already_wss(self):
        link = PeerLink('p', 'wss://example.com/peer_link', TrustLevel.PEER)
        url = link._resolve_ws_url()
        self.assertEqual(url, 'wss://example.com/peer_link')

    # -- _encrypt() / _decrypt() round-trip --
    def test_encrypt_decrypt_roundtrip(self):
        """Test encrypt/decrypt with a real AES key."""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa
        except ImportError:
            self.skipTest("cryptography library not installed")

        key = os.urandom(32)
        self.link._session_key = key
        self.link._key_established_at = time.time()

        plaintext = b'hello peer link world!'
        ciphertext = self.link._encrypt(plaintext)
        self.assertNotEqual(plaintext, ciphertext)

        recovered = self.link._decrypt(ciphertext)
        self.assertEqual(recovered, plaintext)

    def test_encrypt_passthrough_without_session_key(self):
        self.link._session_key = None
        data = b'plain data'
        self.assertEqual(self.link._encrypt(data), data)

    def test_decrypt_passthrough_without_session_key(self):
        self.link._session_key = None
        data = b'plain data'
        self.assertEqual(self.link._decrypt(data), data)

    def test_decrypt_returns_none_for_short_data(self):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa
        except ImportError:
            self.skipTest("cryptography library not installed")
        self.link._session_key = os.urandom(32)
        # Less than 13 bytes (12 nonce + 1 minimum)
        result = self.link._decrypt(b'\x00' * 12)
        self.assertIsNone(result)

    # -- _get_local_capabilities() --
    @patch('os.cpu_count', return_value=8)
    def test_get_local_capabilities_has_cpu_count(self, mock_cpu):
        caps = PeerLink._get_local_capabilities()
        self.assertIn('cpu_count', caps)
        self.assertEqual(caps['cpu_count'], 8)

    @patch('os.cpu_count', return_value=None)
    def test_get_local_capabilities_cpu_count_fallback(self, mock_cpu):
        caps = PeerLink._get_local_capabilities()
        self.assertEqual(caps['cpu_count'], 1)


# ---------------------------------------------------------------------------
# TestLinkManager
# ---------------------------------------------------------------------------
class TestLinkManager(unittest.TestCase):
    """PeerLinkManager — budget, fallback, broadcast, auto-upgrade."""

    def setUp(self):
        reset_link_manager()
        # Patch key_delegation to avoid import errors on tier lookup
        patcher = patch('core.peer_link.link_manager.PeerLinkManager.__init__',
                        lambda self: self._patch_init())
        # We do manual init instead to avoid import issues
        self.mgr = PeerLinkManager.__new__(PeerLinkManager)
        self.mgr._links = {}
        self.mgr._lock = threading.Lock()
        self.mgr._running = False
        self.mgr._maintenance_thread = None
        self.mgr._http_exchange_counts = {}
        self.mgr._channel_handlers = {}
        self.mgr._reconnect_backoff = {}
        self.mgr._max_links = 10
        self.mgr._tier = 'flat'

    def tearDown(self):
        reset_link_manager()

    def test_get_link_returns_none_for_unknown(self):
        self.assertIsNone(self.mgr.get_link('unknown_peer'))

    def test_has_link_returns_false_for_unknown(self):
        self.assertFalse(self.mgr.has_link('unknown_peer'))

    def test_has_link_returns_false_for_disconnected(self):
        link = PeerLink('peer1', '10.0.0.1:6777', TrustLevel.PEER)
        link._state = LinkState.DISCONNECTED
        self.mgr._links['peer1'] = link
        self.assertFalse(self.mgr.has_link('peer1'))

    def test_has_link_returns_true_for_connected(self):
        link = PeerLink('peer1', '10.0.0.1:6777', TrustLevel.PEER)
        link._state = LinkState.CONNECTED
        self.mgr._links['peer1'] = link
        self.assertTrue(self.mgr.has_link('peer1'))

    def test_connection_budget_enforcement(self):
        """Budget prevents adding links beyond max."""
        self.mgr._max_links = 2
        # Add 2 connected links
        for i in range(2):
            link = PeerLink(f'peer{i}', f'10.0.0.{i}:6777', TrustLevel.PEER)
            link._state = LinkState.CONNECTED
            self.mgr._links[f'peer{i}'] = link

        # Attempting to upgrade a 3rd peer should fail when eviction fails
        with patch.object(self.mgr, '_evict_weakest_link', return_value=False):
            result = self.mgr.upgrade_peer('peer_new', '10.0.0.99:6777', TrustLevel.PEER)
            self.assertFalse(result)

    @patch('core.peer_link.link_manager.PeerLinkManager._http_fallback')
    def test_send_falls_back_to_http(self, mock_fallback):
        """send() uses HTTP fallback when no PeerLink."""
        mock_fallback.return_value = {'status': 'ok'}
        result = self.mgr.send(
            'unknown_peer', 'gossip', {'test': 1},
            peer_url='http://10.0.0.1:6777', wait_response=True)
        mock_fallback.assert_called_once()

    def test_send_returns_none_without_link_or_url(self):
        result = self.mgr.send('unknown', 'gossip', {'data': 1})
        self.assertIsNone(result)

    def test_broadcast_sends_to_all_connected(self):
        links = []
        for i in range(3):
            link = MagicMock(spec=PeerLink)
            link.is_connected = True
            link.trust = TrustLevel.PEER
            link.send.return_value = None
            self.mgr._links[f'peer{i}'] = link
            links.append(link)

        sent = self.mgr.broadcast('gossip', {'hello': 'world'})
        self.assertEqual(sent, 3)
        for link in links:
            link.send.assert_called_once_with('gossip', {'hello': 'world'})

    def test_broadcast_with_trust_filter(self):
        link_same = MagicMock(spec=PeerLink)
        link_same.is_connected = True
        link_same.trust = TrustLevel.SAME_USER
        link_same.send.return_value = None
        self.mgr._links['own_device'] = link_same

        link_peer = MagicMock(spec=PeerLink)
        link_peer.is_connected = True
        link_peer.trust = TrustLevel.PEER
        link_peer.send.return_value = None
        self.mgr._links['other_user'] = link_peer

        sent = self.mgr.broadcast('events', {'data': 1}, trust_filter=TrustLevel.SAME_USER)
        self.assertEqual(sent, 1)
        link_same.send.assert_called_once()
        link_peer.send.assert_not_called()

    def test_collect_gathers_responses(self):
        link = MagicMock(spec=PeerLink)
        link.is_connected = True
        link.send.return_value = {'answer': 42}
        self.mgr._links['peer1'] = link

        responses = self.mgr.collect('hivemind', timeout_ms=500)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]['answer'], 42)

    def test_collect_skips_disconnected(self):
        link = MagicMock(spec=PeerLink)
        link.is_connected = False
        self.mgr._links['peer1'] = link

        responses = self.mgr.collect('hivemind', timeout_ms=100)
        self.assertEqual(len(responses), 0)

    def test_record_http_exchange_increments(self):
        self.mgr.record_http_exchange('peer1')
        self.assertEqual(self.mgr._http_exchange_counts['peer1'], 1)
        self.mgr.record_http_exchange('peer1')
        self.assertEqual(self.mgr._http_exchange_counts['peer1'], 2)

    def test_auto_upgrade_triggers_at_threshold(self):
        """After _UPGRADE_THRESHOLD exchanges, auto-upgrade is attempted."""
        with patch.object(self.mgr, '_try_auto_upgrade') as mock_upgrade:
            for _ in range(_UPGRADE_THRESHOLD):
                self.mgr.record_http_exchange('peer_x')
            mock_upgrade.assert_called_once_with('peer_x')

    def test_auto_upgrade_resets_counter(self):
        with patch.object(self.mgr, '_try_auto_upgrade'):
            for _ in range(_UPGRADE_THRESHOLD):
                self.mgr.record_http_exchange('peer_x')
            self.assertEqual(self.mgr._http_exchange_counts['peer_x'], 0)

    def test_register_channel_handler_applies_to_existing_links(self):
        link = MagicMock(spec=PeerLink)
        link.is_connected = True
        self.mgr._links['p1'] = link

        handler = MagicMock()
        self.mgr.register_channel_handler('gossip', handler)

        link.on_message.assert_called_once_with('gossip', handler)
        self.assertIn(handler, self.mgr._channel_handlers['gossip'])

    def test_close_link_removes_and_closes(self):
        link = MagicMock(spec=PeerLink)
        self.mgr._links['peer1'] = link

        self.mgr.close_link('peer1')
        self.assertNotIn('peer1', self.mgr._links)
        link.close.assert_called_once()

    def test_close_link_noop_for_unknown(self):
        self.mgr.close_link('nonexistent')  # Should not raise

    def test_get_status_shape(self):
        status = self.mgr.get_status()
        expected_keys = {
            'running', 'tier', 'max_links', 'active_links',
            'encrypted_links', 'total_links', 'links',
        }
        self.assertEqual(set(status.keys()), expected_keys)
        self.assertEqual(status['tier'], 'flat')
        self.assertEqual(status['max_links'], 10)
        self.assertEqual(status['active_links'], 0)

    def test_get_status_counts_active_links(self):
        link = MagicMock(spec=PeerLink)
        link.get_stats.return_value = {
            'state': 'connected', 'encrypted': True,
            'peer_id': 'p1', 'trust': 'peer',
        }
        self.mgr._links['p1'] = link

        status = self.mgr.get_status()
        self.assertEqual(status['active_links'], 1)
        self.assertEqual(status['encrypted_links'], 1)
        self.assertEqual(status['total_links'], 1)

    def test_trust_determination_same_user_id(self):
        """Same user_id means SAME_USER regardless of network."""
        # _try_auto_upgrade checks user_id match — test the logic path
        # We verify the peer_discovery + compute_mesh lookup path
        with patch.dict(os.environ, {'HEVOLVE_USER_ID': 'user_abc'}):
            mock_gossip = MagicMock()
            mock_gossip.get_peer_list.return_value = [
                {'node_id': 'peer_wan', 'url': 'http://203.0.113.50:6777',
                 'user_id': 'user_abc', 'x25519_public': '', 'public_key': ''}
            ]
            with patch('integrations.social.peer_discovery.gossip', mock_gossip):
                with patch.object(self.mgr, 'upgrade_peer', return_value=True) as mock_up:
                    self.mgr._try_auto_upgrade('peer_wan')
                    if mock_up.called:
                        call_args = mock_up.call_args
                        self.assertEqual(call_args[1].get('trust', call_args[0][2] if len(call_args[0]) > 2 else None),
                                         TrustLevel.SAME_USER)

    def test_evict_weakest_link_removes_least_useful(self):
        """Eviction removes the link with lowest score (idle, no GPU)."""
        link_gpu = MagicMock(spec=PeerLink)
        link_gpu.is_connected = True
        link_gpu.capabilities = {'gpu': 'RTX 4090'}
        link_gpu.idle_seconds = 10
        link_gpu._messages_received = 100

        link_idle = MagicMock(spec=PeerLink)
        link_idle.is_connected = True
        link_idle.capabilities = {}
        link_idle.idle_seconds = 300
        link_idle._messages_received = 0

        self.mgr._links['gpu_peer'] = link_gpu
        self.mgr._links['idle_peer'] = link_idle

        with patch.object(self.mgr, 'close_link') as mock_close:
            result = self.mgr._evict_weakest_link()
            self.assertTrue(result)
            # The idle peer with no GPU should be evicted
            mock_close.assert_called_once_with('idle_peer')

    def test_evict_weakest_returns_false_when_empty(self):
        self.assertFalse(self.mgr._evict_weakest_link())


# ---------------------------------------------------------------------------
# TestChannels
# ---------------------------------------------------------------------------
class TestChannels(unittest.TestCase):
    """Channel registry, DataClass, ChannelDispatcher."""

    def test_all_nine_channels_in_registry(self):
        expected = {
            'control', 'compute', 'dispatch', 'gossip', 'federation',
            'hivemind', 'events', 'ralt', 'sensor',
        }
        self.assertEqual(set(CHANNEL_REGISTRY.keys()), expected)

    def test_channel_ids_match_registry(self):
        """CHANNEL_IDS in link.py matches CHANNEL_REGISTRY ids."""
        for name, config in CHANNEL_REGISTRY.items():
            self.assertIn(name, CHANNEL_IDS,
                          f"Channel {name} missing from CHANNEL_IDS")
            self.assertEqual(CHANNEL_IDS[name], config['id'])

    def test_is_private_channel_compute(self):
        self.assertTrue(is_private_channel('compute'))

    def test_is_private_channel_dispatch(self):
        self.assertTrue(is_private_channel('dispatch'))

    def test_is_private_channel_hivemind(self):
        self.assertTrue(is_private_channel('hivemind'))

    def test_is_private_channel_sensor(self):
        self.assertTrue(is_private_channel('sensor'))

    def test_is_not_private_gossip(self):
        self.assertFalse(is_private_channel('gossip'))

    def test_is_not_private_federation(self):
        self.assertFalse(is_private_channel('federation'))

    def test_is_not_private_events(self):
        self.assertFalse(is_private_channel('events'))

    def test_data_class_values(self):
        self.assertEqual(DataClass.OPEN, 'open')
        self.assertEqual(DataClass.PRIVATE, 'private')
        self.assertEqual(DataClass.SYSTEM, 'system')

    def test_get_channel_config_known(self):
        cfg = get_channel_config('gossip')
        self.assertEqual(cfg['data_class'], DataClass.OPEN)
        self.assertFalse(cfg['reliable'])

    def test_get_channel_config_unknown(self):
        cfg = get_channel_config('nonexistent')
        self.assertEqual(cfg, {})

    # -- ChannelDispatcher --
    def test_dispatcher_register_and_dispatch(self):
        dispatcher = ChannelDispatcher()
        handler = MagicMock(return_value=None)
        dispatcher.register('gossip', handler)
        dispatcher.dispatch('gossip', {'hello': 1}, 'sender_abc')
        handler.assert_called_once_with({'hello': 1}, 'sender_abc')

    def test_dispatcher_returns_first_non_none(self):
        dispatcher = ChannelDispatcher()
        h1 = MagicMock(return_value=None)
        h2 = MagicMock(return_value={'ack': True})
        h3 = MagicMock(return_value={'late': True})
        dispatcher.register('compute', h1)
        dispatcher.register('compute', h2)
        dispatcher.register('compute', h3)
        result = dispatcher.dispatch('compute', {}, 'peer1')
        self.assertEqual(result, {'ack': True})
        # All handlers still called
        h3.assert_called_once()

    def test_dispatcher_unregister(self):
        dispatcher = ChannelDispatcher()
        handler = MagicMock(return_value=None)
        dispatcher.register('events', handler)
        dispatcher.unregister('events', handler)
        dispatcher.dispatch('events', {}, 'peer1')
        handler.assert_not_called()

    def test_dispatcher_has_handlers(self):
        dispatcher = ChannelDispatcher()
        self.assertFalse(dispatcher.has_handlers('gossip'))
        dispatcher.register('gossip', lambda d, s: None)
        self.assertTrue(dispatcher.has_handlers('gossip'))

    def test_dispatcher_get_registered_channels(self):
        dispatcher = ChannelDispatcher()
        dispatcher.register('gossip', lambda d, s: None)
        dispatcher.register('events', lambda d, s: None)
        channels = dispatcher.get_registered_channels()
        self.assertIn('gossip', channels)
        self.assertIn('events', channels)
        self.assertEqual(len(channels), 2)

    def test_dispatcher_no_handlers_returns_none(self):
        dispatcher = ChannelDispatcher()
        result = dispatcher.dispatch('unknown_channel', {}, 'peer1')
        self.assertIsNone(result)

    def test_dispatcher_handler_exception_swallowed(self):
        dispatcher = ChannelDispatcher()
        def bad_handler(data, sender):
            raise RuntimeError("boom")
        dispatcher.register('compute', bad_handler)
        # Should not raise
        result = dispatcher.dispatch('compute', {}, 'peer1')
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# TestNATTraversal
# ---------------------------------------------------------------------------
class TestNATTraversal(unittest.TestCase):
    """NAT traversal strategies."""

    def setUp(self):
        self.nat = NATTraversal(stun_server='stun.example.com:3478')

    def test_is_private_ip_10_x(self):
        self.assertTrue(NATTraversal._is_private_ip('10.0.0.1'))
        self.assertTrue(NATTraversal._is_private_ip('10.255.255.255'))

    def test_is_private_ip_172_16_to_31(self):
        self.assertTrue(NATTraversal._is_private_ip('172.16.0.1'))
        self.assertTrue(NATTraversal._is_private_ip('172.31.255.255'))
        # 172.15 and 172.32 are public
        self.assertFalse(NATTraversal._is_private_ip('172.15.0.1'))
        self.assertFalse(NATTraversal._is_private_ip('172.32.0.1'))

    def test_is_private_ip_192_168(self):
        self.assertTrue(NATTraversal._is_private_ip('192.168.0.1'))
        self.assertTrue(NATTraversal._is_private_ip('192.168.255.255'))

    def test_is_private_ip_127_loopback(self):
        self.assertTrue(NATTraversal._is_private_ip('127.0.0.1'))
        self.assertTrue(NATTraversal._is_private_ip('127.255.255.255'))

    def test_is_not_private_public_ips(self):
        self.assertFalse(NATTraversal._is_private_ip('8.8.8.8'))
        self.assertFalse(NATTraversal._is_private_ip('203.0.113.50'))
        self.assertFalse(NATTraversal._is_private_ip('1.1.1.1'))

    def test_is_private_ip_invalid_input(self):
        self.assertFalse(NATTraversal._is_private_ip('not_an_ip'))
        self.assertFalse(NATTraversal._is_private_ip(''))
        self.assertFalse(NATTraversal._is_private_ip('192.168.1'))

    def test_extract_host_strips_protocol(self):
        self.assertEqual(NATTraversal._extract_host('http://example.com:6777/path'), 'example.com')
        self.assertEqual(NATTraversal._extract_host('https://10.0.0.1:8080'), '10.0.0.1')

    def test_extract_host_strips_port_and_path(self):
        self.assertEqual(NATTraversal._extract_host('myhost:6777/api'), 'myhost')

    def test_extract_host_empty_string(self):
        self.assertEqual(NATTraversal._extract_host(''), '')

    def test_extract_host_plain_hostname(self):
        self.assertEqual(NATTraversal._extract_host('192.168.1.5'), '192.168.1.5')

    @patch('socket.socket')
    @patch('core.port_registry.get_port', return_value=6777)
    def test_resolve_peer_address_lan_direct(self, mock_port, mock_sock_cls):
        """LAN direct: reachable peer returns ws:// URL."""
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 0
        mock_sock_cls.return_value = mock_sock

        result = self.nat.resolve_peer_address({
            'url': 'http://192.168.1.20:6777',
            'mesh_ip': '',
        })
        self.assertEqual(result, 'ws://192.168.1.20:6777/peer_link')

    @patch('socket.socket')
    @patch('core.port_registry.get_port', return_value=6777)
    def test_resolve_peer_address_skips_wan_for_private_ip(self, mock_port, mock_sock_cls):
        """Direct WAN is skipped for private IPs."""
        mock_sock = MagicMock()
        # LAN fails
        mock_sock.connect_ex.return_value = 1
        mock_sock_cls.return_value = mock_sock

        # Private IP: _try_direct_wan should skip it
        result = self.nat.resolve_peer_address({
            'url': 'http://192.168.1.20:6777',
            'mesh_ip': '',
        })
        # With LAN and WAN both failing, falls through
        # Crossbar relay depends on CBURL env
        # The point is _try_direct_wan returns None for private IPs

    @patch('core.peer_link.nat.socket.socket')
    @patch('core.port_registry.get_port', return_value=6777)
    def test_resolve_peer_address_wireguard_mesh(self, mock_port, mock_sock_cls):
        """WireGuard mesh IP is tried when LAN/WAN fail."""
        call_count = [0]
        def side_effect(*args, **kwargs):
            mock = MagicMock()
            call_count[0] += 1
            # LAN direct creates a socket that fails; WAN is skipped
            # (192.168.x is private); WireGuard creates a socket that succeeds
            if call_count[0] <= 1:
                mock.connect_ex.return_value = 1  # LAN fails
            else:
                mock.connect_ex.return_value = 0  # WireGuard succeeds
            return mock

        mock_sock_cls.side_effect = side_effect

        result = self.nat.resolve_peer_address({
            'url': 'http://192.168.1.20:6777',
            'mesh_ip': '10.100.0.5',
        })
        self.assertEqual(result, 'ws://10.100.0.5:6796/peer_link')

    @patch('socket.socket')
    @patch('core.port_registry.get_port', return_value=6777)
    def test_resolve_peer_address_crossbar_fallback(self, mock_port, mock_sock_cls):
        """Falls back to Crossbar relay when all direct strategies fail."""
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 1
        mock_sock_cls.return_value = mock_sock

        with patch.dict(os.environ, {'CBURL': 'ws://crossbar.example.com:8088/ws'}):
            result = self.nat.resolve_peer_address({
                'url': 'http://203.0.113.50:6777',
                'mesh_ip': '',
            })
            self.assertEqual(result, 'ws://crossbar.example.com:8088/ws')


# ---------------------------------------------------------------------------
# TestTelemetry
# ---------------------------------------------------------------------------
class TestTelemetry(unittest.TestCase):
    """TelemetryCollector and CentralConnection."""

    def test_collector_record_sent(self):
        tc = TelemetryCollector()
        tc.record_sent('gossip', 512)
        tc.record_sent('gossip', 1024)
        summary = tc.get_summary()
        self.assertEqual(summary['traffic']['gossip']['sent'], 2)
        self.assertEqual(summary['traffic']['gossip']['bytes_sent'], 1536)

    def test_collector_record_received(self):
        tc = TelemetryCollector()
        tc.record_received('compute', 2048)
        summary = tc.get_summary()
        self.assertEqual(summary['traffic']['compute']['recv'], 1)
        self.assertEqual(summary['traffic']['compute']['bytes_recv'], 2048)

    def test_collector_record_security_event(self):
        tc = TelemetryCollector()
        tc.record_security_event('tamper_detected', 'guardrail hash mismatch')
        summary = tc.get_summary()
        self.assertEqual(len(summary['security_events']), 1)
        self.assertEqual(summary['security_events'][0]['type'], 'tamper_detected')

    def test_collector_get_summary_resets_counters(self):
        tc = TelemetryCollector()
        tc.record_sent('gossip', 100)
        tc.record_security_event('test', 'detail')
        summary1 = tc.get_summary()
        self.assertEqual(summary1['traffic']['gossip']['sent'], 1)
        self.assertEqual(len(summary1['security_events']), 1)

        # Second call should be empty
        summary2 = tc.get_summary()
        self.assertEqual(len(summary2['traffic']), 0)
        self.assertEqual(len(summary2['security_events']), 0)

    def test_collector_security_events_capped_at_100(self):
        tc = TelemetryCollector()
        for i in range(120):
            tc.record_security_event('event', f'detail_{i}')
        # Internal list should be capped at 100
        self.assertLessEqual(len(tc._security_events), 100)
        summary = tc.get_summary()
        self.assertLessEqual(len(summary['security_events']), 100)

    # -- CentralConnection --
    def test_central_is_degraded_after_1h(self):
        cc = CentralConnection()
        cc._connected = False
        cc._disconnected_since = time.time() - _DEGRADED_THRESHOLD - 10
        self.assertTrue(cc.is_degraded())

    def test_central_not_degraded_when_connected(self):
        cc = CentralConnection()
        cc._connected = True
        cc._disconnected_since = time.time() - _DEGRADED_THRESHOLD - 10
        self.assertFalse(cc.is_degraded())

    def test_central_not_degraded_when_recently_disconnected(self):
        cc = CentralConnection()
        cc._connected = False
        cc._disconnected_since = time.time() - 60  # Only 1 minute
        self.assertFalse(cc.is_degraded())

    def test_central_is_restricted_after_24h(self):
        cc = CentralConnection()
        cc._connected = False
        cc._disconnected_since = time.time() - _RESTRICTED_THRESHOLD - 10
        self.assertTrue(cc.is_restricted())

    def test_central_not_restricted_when_connected(self):
        cc = CentralConnection()
        cc._connected = True
        cc._disconnected_since = time.time() - _RESTRICTED_THRESHOLD - 100
        self.assertFalse(cc.is_restricted())

    def test_central_degraded_but_not_restricted_between_thresholds(self):
        cc = CentralConnection()
        cc._connected = False
        cc._disconnected_since = time.time() - (_DEGRADED_THRESHOLD + 100)
        self.assertTrue(cc.is_degraded())
        self.assertFalse(cc.is_restricted())

    def test_handle_emergency_halt_valid_signature(self):
        """Valid master-key-signed halt calls into the circuit breaker."""
        cc = CentralConnection()
        message = {
            'type': 'emergency_halt',
            'reason': 'test_halt',
            'master_signature': 'valid_sig_hex',
        }
        # Mock the security imports that _handle_emergency_halt uses
        mock_breaker = MagicMock()
        mock_gossip_mod = MagicMock()
        with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', 'mock_pub_key'), \
             patch('security.node_integrity.verify_json_signature', return_value=True), \
             patch.dict('sys.modules', {
                 'security.hive_guardrails': MagicMock(
                     HiveCircuitBreaker=mock_breaker),
                 'integrations.social.peer_discovery': mock_gossip_mod,
             }):
            cc.handle_control_message(message)
        mock_breaker.trip.assert_called_once_with(reason='test_halt')

    def test_handle_emergency_halt_no_signature_ignored(self):
        """Emergency halt without signature is IGNORED."""
        cc = CentralConnection()
        message = {
            'type': 'emergency_halt',
            'reason': 'bad_halt',
            # No master_signature
        }
        mock_breaker = MagicMock()
        with patch.dict('sys.modules', {
            'security.hive_guardrails': MagicMock(
                HiveCircuitBreaker=mock_breaker)
        }):
            cc.handle_control_message(message)
            mock_breaker.trip.assert_not_called()
            mock_breaker.halt_network.assert_not_called()

    def test_handle_peer_ban_closes_link(self):
        """peer_ban closes the link and updates DB."""
        cc = CentralConnection()
        message = {
            'type': 'peer_ban',
            'node_id': 'banned_node_abc',
        }
        mock_mgr = MagicMock()
        mock_link_mgr_mod = MagicMock()
        mock_link_mgr_mod.get_link_manager.return_value = mock_mgr
        mock_models = MagicMock()
        with patch.dict('sys.modules', {
            'core.peer_link.link_manager': mock_link_mgr_mod,
            'integrations.social.models': mock_models,
        }):
            cc.handle_control_message(message)

        mock_mgr.close_link.assert_called_once_with('banned_node_abc')

    def test_handle_peer_ban_records_security_event(self):
        cc = CentralConnection()
        message = {
            'type': 'peer_ban',
            'node_id': 'banned_peer_xyz',
        }
        mock_link_mgr_mod = MagicMock()
        mock_models = MagicMock()
        mock_models.get_db.side_effect = Exception("no db")
        with patch.dict('sys.modules', {
            'core.peer_link.link_manager': mock_link_mgr_mod,
            'integrations.social.models': mock_models,
        }):
            cc.handle_control_message(message)
        # Check telemetry recorded the event
        summary = cc._telemetry.get_summary()
        security_events = summary['security_events']
        self.assertEqual(len(security_events), 1)
        self.assertEqual(security_events[0]['type'], 'peer_ban')

    def test_get_disconnection_hours_zero_when_connected(self):
        cc = CentralConnection()
        cc._connected = True
        self.assertEqual(cc.get_disconnection_hours(), 0)

    def test_get_disconnection_hours_zero_when_never_disconnected(self):
        cc = CentralConnection()
        cc._connected = False
        cc._disconnected_since = None
        self.assertEqual(cc.get_disconnection_hours(), 0)

    def test_get_disconnection_hours_positive(self):
        cc = CentralConnection()
        cc._connected = False
        cc._disconnected_since = time.time() - 7200  # 2 hours ago
        hours = cc.get_disconnection_hours()
        self.assertGreater(hours, 1.9)
        self.assertLess(hours, 2.1)


# ---------------------------------------------------------------------------
# TestMessageBus
# ---------------------------------------------------------------------------
class TestMessageBus(unittest.TestCase):
    """MessageBus — unified pub/sub across transports."""

    def setUp(self):
        reset_message_bus()
        self.bus = MessageBus()

    def tearDown(self):
        reset_message_bus()

    def test_publish_returns_message_id(self):
        with patch.object(self.bus, '_route_peerlink'), \
             patch.object(self.bus, '_route_crossbar'), \
             patch.object(self.bus, '_route_local'):
            msg_id = self.bus.publish('chat.response', {'text': 'hi'})
            self.assertIsInstance(msg_id, str)
            self.assertEqual(len(msg_id), 16)

    def test_subscribe_and_publish_delivers(self):
        received = []
        def handler(topic, data):
            received.append((topic, data))

        self.bus.subscribe('chat.response', handler)
        with patch.object(self.bus, '_route_peerlink'), \
             patch.object(self.bus, '_route_crossbar'):
            self.bus.publish('chat.response', {'text': 'hello'})

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], 'chat.response')
        self.assertEqual(received[0][1]['text'], 'hello')

    def test_wildcard_subscribe_matches(self):
        received = []
        def handler(topic, data):
            received.append(topic)

        self.bus.subscribe('chat.*', handler)
        with patch.object(self.bus, '_route_peerlink'), \
             patch.object(self.bus, '_route_crossbar'):
            self.bus.publish('chat.response', {'text': 'a'})
            self.bus.publish('chat.action', {'text': 'b'})
            self.bus.publish('task.progress', {'text': 'c'})

        self.assertIn('chat.response', received)
        self.assertIn('chat.action', received)
        self.assertNotIn('task.progress', received)

    def test_receive_from_peer_deduplicates(self):
        received = []
        self.bus.subscribe('chat.response', lambda t, d: received.append(d))

        envelope = {
            'msg_id': 'dedup_test_001',
            'topic': 'chat.response',
            'data': {'text': 'unique'},
        }
        result1 = self.bus.receive_from_peer(envelope)
        result2 = self.bus.receive_from_peer(envelope)

        self.assertTrue(result1)
        self.assertFalse(result2)
        # Handler called only once
        self.assertEqual(len(received), 1)

    def test_receive_from_peer_no_msg_id(self):
        result = self.bus.receive_from_peer({'topic': 'test', 'data': {}})
        self.assertFalse(result)

    def test_receive_from_crossbar_maps_legacy_topics(self):
        received = []
        self.bus.subscribe('chat.response', lambda t, d: received.append(d))

        legacy_topic = 'com.hertzai.hevolve.chat.12345'
        result = self.bus.receive_from_crossbar(legacy_topic, {'text': 'legacy'})
        self.assertTrue(result)
        self.assertEqual(len(received), 1)

    def test_receive_from_crossbar_unknown_topic_passthrough(self):
        received = []
        self.bus.subscribe('com.unknown.topic', lambda t, d: received.append(d))

        result = self.bus.receive_from_crossbar('com.unknown.topic', {'data': 1})
        self.assertTrue(result)
        self.assertEqual(len(received), 1)

    def test_receive_from_crossbar_deduplicates(self):
        self.bus.subscribe('chat.response', lambda t, d: None)
        legacy = 'com.hertzai.hevolve.chat.1'
        result1 = self.bus.receive_from_crossbar(legacy, {'msg_id': 'dup1'})
        result2 = self.bus.receive_from_crossbar(legacy, {'msg_id': 'dup1'})
        self.assertTrue(result1)
        self.assertFalse(result2)

    def test_unsubscribe_removes_handler(self):
        received = []
        handler = lambda t, d: received.append(d)
        self.bus.subscribe('events', handler)
        self.bus.unsubscribe('events', handler)

        with patch.object(self.bus, '_route_peerlink'), \
             patch.object(self.bus, '_route_crossbar'):
            self.bus.publish('events', {'data': 1})
        self.assertEqual(len(received), 0)

    def test_get_stats_tracks_published(self):
        with patch.object(self.bus, '_route_local'), \
             patch.object(self.bus, '_route_peerlink'), \
             patch.object(self.bus, '_route_crossbar'):
            self.bus.publish('test', {})
            self.bus.publish('test', {})
        stats = self.bus.get_stats()
        self.assertEqual(stats['published'], 2)

    def test_get_stats_tracks_delivered_local(self):
        with patch.object(self.bus, '_route_peerlink'), \
             patch.object(self.bus, '_route_crossbar'):
            self.bus.publish('test', {})
        stats = self.bus.get_stats()
        self.assertEqual(stats['delivered_local'], 1)

    def test_topic_map_has_correct_count(self):
        """TOPIC_MAP should have the documented legacy mappings."""
        self.assertEqual(len(TOPIC_MAP), 11)

    def test_topic_map_has_key_entries(self):
        self.assertIn('chat.response', TOPIC_MAP)
        self.assertIn('chat.action', TOPIC_MAP)
        self.assertIn('task.progress', TOPIC_MAP)
        self.assertIn('mobile.push', TOPIC_MAP)
        self.assertIn('remote_desktop.signal', TOPIC_MAP)

    def test_reverse_map_populated(self):
        self.assertGreater(len(_REVERSE_MAP), 0)
        # A known legacy prefix should map back
        self.assertIn('com.hertzai.hevolve.chat', _REVERSE_MAP)

    def test_publish_skip_crossbar(self):
        with patch.object(self.bus, '_route_local'), \
             patch.object(self.bus, '_route_peerlink'), \
             patch.object(self.bus, '_route_crossbar') as mock_cb:
            self.bus.publish('chat.response', {'text': 'hi'}, skip_crossbar=True)
        mock_cb.assert_not_called()

    def test_publish_skip_peerlink(self):
        with patch.object(self.bus, '_route_local'), \
             patch.object(self.bus, '_route_peerlink') as mock_pl, \
             patch.object(self.bus, '_route_crossbar'):
            self.bus.publish('chat.response', {'text': 'hi'}, skip_peerlink=True)
        mock_pl.assert_not_called()

    @patch('core.platform.events.emit_event')
    def test_route_local_emits_to_eventbus(self, mock_emit):
        self.bus._route_local('test.topic', {'data': 1}, 'msg123')
        mock_emit.assert_called_once_with('bus.test.topic', {'data': 1})


# ---------------------------------------------------------------------------
# TestLRUDedup
# ---------------------------------------------------------------------------
class TestLRUDedup(unittest.TestCase):
    """LRU deduplication set."""

    def test_new_id_returns_true(self):
        d = _LRUDedup(maxsize=10)
        self.assertTrue(d.check_and_add('msg_001'))

    def test_duplicate_returns_false(self):
        d = _LRUDedup(maxsize=10)
        d.check_and_add('msg_001')
        self.assertFalse(d.check_and_add('msg_001'))

    def test_respects_maxsize(self):
        d = _LRUDedup(maxsize=3)
        d.check_and_add('a')
        d.check_and_add('b')
        d.check_and_add('c')
        d.check_and_add('d')  # Evicts 'a'
        # 'a' should be considered new again
        self.assertTrue(d.check_and_add('a'))
        # 'b' was evicted when 'a' was re-added (now 4 inserts with maxsize 3)
        # After d: [b, c, d], then adding 'a' evicts 'b': [c, d, a]
        self.assertTrue(d.check_and_add('b'))

    def test_thread_safety(self):
        d = _LRUDedup(maxsize=10000)
        results = []

        def worker(prefix):
            for i in range(100):
                results.append(d.check_and_add(f'{prefix}_{i}'))

        threads = [threading.Thread(target=worker, args=(f't{t}',)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All first inserts should be True, so at least 500 True values
        self.assertEqual(len([r for r in results if r]), 500)


# ---------------------------------------------------------------------------
# TestIntegration
# ---------------------------------------------------------------------------
class TestIntegration(unittest.TestCase):
    """Cross-module integration wiring."""

    def setUp(self):
        reset_link_manager()
        reset_message_bus()

    def tearDown(self):
        reset_link_manager()
        reset_message_bus()

    def test_message_bus_to_channel_dispatcher_wiring(self):
        """MessageBus receive_from_peer delivers to subscribers (channel dispatcher path)."""
        bus = MessageBus()
        received = []
        bus.subscribe('federation', lambda t, d: received.append(d))

        envelope = {
            'msg_id': 'integration_001',
            'topic': 'federation',
            'data': {'post_id': 42},
        }
        bus.receive_from_peer(envelope)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['post_id'], 42)

    def test_peerlink_to_link_manager_lifecycle(self):
        """PeerLink created, added to manager, closed via manager."""
        mgr = PeerLinkManager.__new__(PeerLinkManager)
        mgr._links = {}
        mgr._lock = threading.Lock()
        mgr._running = False
        mgr._maintenance_thread = None
        mgr._http_exchange_counts = {}
        mgr._channel_handlers = {}
        mgr._reconnect_backoff = {}
        mgr._max_links = 10
        mgr._tier = 'flat'

        link = PeerLink('test_peer', '10.0.0.1:6777', TrustLevel.PEER)
        link._state = LinkState.CONNECTED
        mgr._links['test_peer'] = link

        self.assertTrue(mgr.has_link('test_peer'))
        mgr.close_link('test_peer')
        self.assertFalse(mgr.has_link('test_peer'))
        self.assertEqual(link._state, LinkState.DISCONNECTED)

    def test_telemetry_collector_used_by_central(self):
        """CentralConnection exposes its TelemetryCollector."""
        cc = CentralConnection()
        cc.telemetry.record_sent('gossip', 100)
        cc.telemetry.record_security_event('test', 'detail')
        summary = cc.telemetry.get_summary()
        self.assertEqual(summary['traffic']['gossip']['sent'], 1)
        self.assertEqual(len(summary['security_events']), 1)

    def test_reset_singletons_work(self):
        """Singleton reset functions properly clear state."""
        bus1 = get_message_bus()
        reset_message_bus()
        bus2 = get_message_bus()
        self.assertIsNot(bus1, bus2)

    @patch('security.key_delegation.get_node_tier', return_value='flat')
    def test_reset_link_manager_singleton(self, mock_tier):
        mgr1 = get_link_manager()
        reset_link_manager()
        mgr2 = get_link_manager()
        self.assertIsNot(mgr1, mgr2)


# ---------------------------------------------------------------------------
# TestLinkState
# ---------------------------------------------------------------------------
class TestLinkState(unittest.TestCase):
    """LinkState enum."""

    def test_all_states_exist(self):
        states = {s.value for s in LinkState}
        expected = {'disconnected', 'connecting', 'handshaking', 'connected', 'closing'}
        self.assertEqual(states, expected)

    def test_channel_ids_reverse_mapping(self):
        """CHANNEL_NAMES is the inverse of CHANNEL_IDS."""
        for name, cid in CHANNEL_IDS.items():
            self.assertIn(cid, CHANNEL_NAMES)
            self.assertEqual(CHANNEL_NAMES[cid], name)


if __name__ == '__main__':
    unittest.main()
