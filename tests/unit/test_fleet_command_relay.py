"""
Behavioural tests for the multi-hop, loop-safe fleet.command relay
(core.peer_link.message_bus — gap #57).

These import the REAL MessageBus, mock the BOUNDARIES (the signature authority
check + the PeerLink broadcast + this node's id), drive the REAL ingress methods
(receive_from_peer / receive_from_crossbar), and assert observable effects:

  - a NEW, signature-VERIFIED fleet.command is delivered locally AND re-broadcast
    one hop further with hop_ttl decremented and this node appended to relay_path
  - a duplicate message_id (already seen) is dropped (LRU dedup) — no re-broadcast
  - hop_ttl <= 0 delivers locally but does NOT re-broadcast (drop-at-zero)
  - a node already in relay_path drops (loop guard independent of the LRU)
  - an UNVERIFIED/forged command is dropped entirely (NOT delivered, NOT relayed)
  - the inbound sender peer is excluded from the re-broadcast (no echo-back)
  - a crossbar-origin command re-broadcasts to PeerLink ONLY (no WAMP re-publish)
  - non-relayed topics keep today's deliver-local-only behaviour (zero change)

No grep/source-shape assertions — every test calls the real code and observes
the mock call args / delivered payloads / return values.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from core.peer_link.message_bus import (  # noqa: E402
    MessageBus, RELAY_TOPIC, RELAY_DEFAULT_HOP_TTL,
)


SELF = 'thisnode00000000'


def _signed_cmd(**over):
    """A FleetCommand-shaped payload as it rides 'fleet.command'."""
    cmd = {
        'cmd_type': 'firmware_update',
        'issued_by': 'central00central0',
        'signature': 'deadbeef',
        'target_node_id': '',
        'params': {'update_url': 'github:hertz-ai/HARTOS/abc',
                   'release_hash': 'abc', 'channel': 'stable'},
    }
    cmd.update(over)
    return cmd


def _envelope(msg_id='m1', hop_ttl=RELAY_DEFAULT_HOP_TTL, origin='central00central0',
              relay_path=None, data=None):
    return {
        'msg_id': msg_id,
        'topic': RELAY_TOPIC,
        'data': data if data is not None else _signed_cmd(),
        'hop_ttl': hop_ttl,
        'origin': origin,
        'relay_path': relay_path if relay_path is not None else [],
    }


class _RelayCase(unittest.TestCase):
    """Shared harness: fresh bus, verifier forced True, self id pinned, and the
    PeerLink leg + local delivery captured as mocks."""

    def setUp(self):
        self.bus = MessageBus()

        # Capture re-broadcasts (the PeerLink leg) without a real link manager.
        self._route = patch.object(self.bus, '_route_peerlink')
        self.m_route = self._route.start()

        # Capture local deliveries.
        self._deliver = patch.object(self.bus, '_deliver_to_subscribers')
        self.m_deliver = self._deliver.start()

        # Pin this node's id (drives relay_path stamping + loop guard).
        self._selfid = patch.object(self.bus, '_self_node_id', return_value=SELF)
        self._selfid.start()

        # Authority gate — default verified; individual tests flip it.
        self._verify = patch(
            'integrations.social.fleet_command.FleetCommandService.verify_command_signature',
            return_value=True)
        self.m_verify = self._verify.start()

    def tearDown(self):
        for p in (self._route, self._deliver, self._selfid, self._verify):
            p.stop()


class TestRelayRebroadcastsNew(_RelayCase):

    def test_new_verified_command_delivers_local_and_rebroadcasts(self):
        ok = self.bus.receive_from_peer(_envelope(msg_id='new1'),
                                        sender_peer_id='peerA')
        self.assertTrue(ok)

        # Delivered locally (this node's ota_push_listener consumer must fire).
        self.m_deliver.assert_called_once()
        self.assertEqual(self.m_deliver.call_args[0][0], RELAY_TOPIC)

        # Re-broadcast exactly once, one hop further.
        self.m_route.assert_called_once()
        kwargs = self.m_route.call_args.kwargs
        meta = kwargs['relay_meta']
        # hop_ttl decremented by 1.
        self.assertEqual(meta['hop_ttl'], RELAY_DEFAULT_HOP_TTL - 1)
        # This node appended to the relay_path.
        self.assertEqual(meta['relay_path'], [SELF])
        # origin carried verbatim (true source preserved for audit).
        self.assertEqual(meta['origin'], 'central00central0')

    def test_msg_id_is_identical_across_the_hop(self):
        """The id must NOT change across hops — that is what lets downstream LRU
        dedup collapse duplicates arriving via multiple peers (anti-storm)."""
        self.bus.receive_from_peer(_envelope(msg_id='stable-id'), sender_peer_id='peerA')
        # _route_peerlink(topic, data, msg_id, relay_meta=, exclude_peer=)
        positional = self.m_route.call_args[0]
        self.assertEqual(positional[2], 'stable-id')

    def test_relay_counter_increments(self):
        self.bus.receive_from_peer(_envelope(msg_id='cnt1'), sender_peer_id='peerA')
        self.assertEqual(self.bus.get_stats()['relay_rebroadcast'], 1)


class TestRelayExcludesSender(_RelayCase):

    def test_inbound_sender_is_excluded_from_rebroadcast(self):
        """A command must never echo back the link it arrived on."""
        self.bus.receive_from_peer(_envelope(msg_id='excl1'), sender_peer_id='peerSENDER')
        self.assertEqual(self.m_route.call_args.kwargs['exclude_peer'], 'peerSENDER')


class TestRelayDropsDuplicate(_RelayCase):

    def test_second_arrival_of_same_id_is_dropped(self):
        env = _envelope(msg_id='dup-1')
        first = self.bus.receive_from_peer(env, sender_peer_id='peerA')
        # A second arrival (e.g. via a different peer) with the SAME id.
        second = self.bus.receive_from_peer(_envelope(msg_id='dup-1'), sender_peer_id='peerB')

        self.assertTrue(first)
        self.assertFalse(second)  # dedup gate rejects the duplicate
        # Only the FIRST arrival delivered + relayed; the duplicate did neither.
        self.assertEqual(self.m_route.call_count, 1)
        self.assertEqual(self.m_deliver.call_count, 1)
        self.assertEqual(self.bus.get_stats()['deduplicated'], 1)


class TestRelayDropsTtlZero(_RelayCase):

    def test_ttl_zero_delivers_local_but_no_rebroadcast(self):
        ok = self.bus.receive_from_peer(_envelope(msg_id='ttl0', hop_ttl=0),
                                        sender_peer_id='peerA')
        self.assertTrue(ok)
        # Delivered locally (still applies on THIS node)...
        self.m_deliver.assert_called_once()
        # ...but NOT re-broadcast (drop-at-zero blast-radius cap).
        self.m_route.assert_not_called()
        self.assertEqual(self.bus.get_stats()['relay_ttl_expired'], 1)

    def test_ttl_one_rebroadcasts_with_zero(self):
        """ttl=1 still relays once, decremented to 0 (next hop won't re-broadcast)."""
        self.bus.receive_from_peer(_envelope(msg_id='ttl1', hop_ttl=1), sender_peer_id='peerA')
        self.m_route.assert_called_once()
        self.assertEqual(self.m_route.call_args.kwargs['relay_meta']['hop_ttl'], 0)


class TestRelayLoopGuard(_RelayCase):

    def test_self_already_in_relay_path_drops_rebroadcast(self):
        """relay_path membership is an independent loop guard (the LRU can evict
        under load; this one cannot be evicted)."""
        env = _envelope(msg_id='loop1', relay_path=[SELF, 'other'])
        ok = self.bus.receive_from_peer(env, sender_peer_id='peerA')
        self.assertTrue(ok)
        # Verified + delivered locally, but NOT re-broadcast (we're in the path).
        self.m_deliver.assert_called_once()
        self.m_route.assert_not_called()
        self.assertEqual(self.bus.get_stats()['relay_loop_blocked'], 1)


class TestRelayDropsUnverified(_RelayCase):

    def test_forged_command_is_neither_delivered_nor_relayed(self):
        self.m_verify.return_value = False  # forged / unauthorized signature
        ok = self.bus.receive_from_peer(_envelope(msg_id='forged1'), sender_peer_id='peerA')
        # receive_from_peer still returns True (message consumed), but the relay
        # helper dropped it: no local delivery, no re-broadcast.
        self.assertTrue(ok)
        self.m_deliver.assert_not_called()
        self.m_route.assert_not_called()
        self.assertEqual(self.bus.get_stats()['relay_dropped_unverified'], 1)

    def test_verifier_exception_fails_closed(self):
        """If the verifier is unavailable, a command must NOT relay (fail-closed)."""
        self.m_verify.side_effect = Exception('verifier down')
        self.bus.receive_from_peer(_envelope(msg_id='exc1'), sender_peer_id='peerA')
        self.m_deliver.assert_not_called()
        self.m_route.assert_not_called()
        self.assertEqual(self.bus.get_stats()['relay_dropped_unverified'], 1)


class TestCrossbarRelayPeerLinkOnly(_RelayCase):

    def test_crossbar_origin_relays_to_peerlink_only(self):
        """A fleet.command arriving over WAMP is delivered + relayed onward to
        PeerLink peers — but NEVER re-published to Crossbar (that would storm
        the shared bus, which already reached every subscriber once)."""
        data = _signed_cmd()
        data['msg_id'] = 'cb1'
        data['hop_ttl'] = RELAY_DEFAULT_HOP_TTL
        data['relay_path'] = []
        with patch.object(self.bus, '_route_crossbar') as m_cross:
            ok = self.bus.receive_from_crossbar('com.hertzai.hevolve.fleet', data)
        self.assertTrue(ok)
        # Delivered locally + relayed to PeerLink...
        self.m_deliver.assert_called_once()
        self.m_route.assert_called_once()
        # ...and NOT re-published to Crossbar.
        m_cross.assert_not_called()


class TestNonRelayedTopicsUnchanged(_RelayCase):

    def test_chat_response_is_delivered_local_only_no_relay(self):
        """Every non-fleet.command topic keeps the deliver-local-only behaviour
        — the relay path must not touch chat/task/etc."""
        env = {'msg_id': 'chat1', 'topic': 'chat.response', 'data': {'text': 'hi'}}
        ok = self.bus.receive_from_peer(env, sender_peer_id='peerA')
        self.assertTrue(ok)
        self.m_deliver.assert_called_once()
        self.m_deliver.assert_called_with('chat.response', {'text': 'hi'})
        # No relay, no signature check for non-relayed topics.
        self.m_route.assert_not_called()
        self.m_verify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
