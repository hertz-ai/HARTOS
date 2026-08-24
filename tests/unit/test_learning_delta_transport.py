"""PeerLink transport for federation learning deltas (channel 0x0A 'learning').

The delta reaches a peer's aggregator by exactly one of two AUTHENTICATED
transports, both landing on the SAME receiver (receive_peer_delta + its
genuine-build gate):

  * PeerLink 'learning' channel — when a live authenticated link exists
    (NAT-traversing, the only way two peers behind separate NATs federate)
  * HTTP /api/social/peers/federation-delta — fallback / seeds

These tests prove, without a live stack:
  1. the inbound handler uses the 3-arg on-link signature and delegates to
     receive_peer_delta (NOT the 2-arg dispatcher shape — that is the latent
     hivemind bug this transport deliberately avoids);
  2. bootstrap registers the handler via register_channel_handler exactly once;
  3. broadcast_delta prefers PeerLink when a link is live, and falls back to
     the authenticated HTTP endpoint when there is no link (or the send drops).
"""
import time
from unittest.mock import patch, MagicMock

import integrations.agent_engine.federated_aggregator as fa


# ── 1. Inbound handler ─────────────────────────────────────────────────────

class TestLearningDeltaHandler:

    def test_handler_delegates_to_receive_peer_delta(self):
        """3-arg (channel, data, peer_id) → receive_peer_delta(data)."""
        delta = {'version': 1, 'node_id': 'peerB'}
        agg = MagicMock()
        agg.receive_peer_delta.return_value = (True, 'ok')
        with patch.object(fa, 'get_federated_aggregator', return_value=agg):
            out = fa.handle_learning_delta('learning', delta, 'peerBxxxx')
        agg.receive_peer_delta.assert_called_once_with(delta)
        assert out == {'accepted': True, 'reason': 'ok'}

    def test_handler_rejects_non_dict_without_calling_receiver(self):
        """A malformed frame must never reach the receiver."""
        agg = MagicMock()
        with patch.object(fa, 'get_federated_aggregator', return_value=agg):
            assert fa.handle_learning_delta('learning', 'not-a-dict', 'p') is None
            assert fa.handle_learning_delta('learning', None, 'p') is None
        agg.receive_peer_delta.assert_not_called()

    def test_handler_swallows_receiver_error(self):
        """A receiver exception is logged, not propagated onto the link loop."""
        agg = MagicMock()
        agg.receive_peer_delta.side_effect = RuntimeError('boom')
        with patch.object(fa, 'get_federated_aggregator', return_value=agg):
            assert fa.handle_learning_delta('learning', {'v': 1}, 'p') is None

    def test_rejected_delta_still_returns_structured_result(self):
        agg = MagicMock()
        agg.receive_peer_delta.return_value = (False, 'unverified build')
        with patch.object(fa, 'get_federated_aggregator', return_value=agg):
            out = fa.handle_learning_delta('learning', {'v': 1}, 'p')
        assert out == {'accepted': False, 'reason': 'unverified build'}


# ── 2. Bootstrap wiring ────────────────────────────────────────────────────

class TestLearningDeltaBootstrap:

    def _reset(self):
        fa._learning_ingress_wired = False

    def test_bootstrap_registers_once_and_is_idempotent(self):
        self._reset()
        mgr = MagicMock()
        with patch('core.peer_link.link_manager.get_link_manager',
                   return_value=mgr):
            first = fa.bootstrap_learning_delta_ingress()
            second = fa.bootstrap_learning_delta_ingress()
        assert first is True
        assert second is False
        # Registered on the REAL inbound path (register_channel_handler →
        # link.on_message → _message_handlers), exactly once, with the handler.
        mgr.register_channel_handler.assert_called_once_with(
            'learning', fa.handle_learning_delta)

    def test_bootstrap_returns_false_when_peerlink_absent(self):
        self._reset()
        with patch('core.peer_link.link_manager.get_link_manager',
                   side_effect=Exception('no peer_link')):
            assert fa.bootstrap_learning_delta_ingress() is False
        assert fa._learning_ingress_wired is False


# ── 3. Outbound: PeerLink-first, HTTP fallback ─────────────────────────────

class TestBroadcastTransportSelection:
    """broadcast_delta delivers each sampled peer PeerLink-first."""

    def _run_broadcast(self, link):
        """Drive broadcast_delta against ONE active peer 'peerB', with a
        PeerLink manager whose get_link returns `link` (or None). Returns the
        pooled_post mock so the caller can assert HTTP was / was not used."""
        agg = fa.FederatedAggregator()
        delta = {'version': 1, 'node_id': 'selfNode', 'timestamp': time.time()}

        peer = MagicMock()
        peer.node_id = 'peerB'
        peer.url = 'http://192.168.0.83:6777'
        sess = MagicMock()
        sess.query.return_value.filter_by.return_value.all.return_value = [peer]

        guard = MagicMock()
        guard.check_egress.return_value = (True, 'ok')

        mgr = MagicMock()
        mgr.get_link.return_value = link

        fake_gossip = MagicMock()
        fake_gossip.seed_peers = []          # isolate: no seed HTTP noise
        fake_gossip.gossip_fanout = 3

        pooled_post = MagicMock()

        with patch.object(fa, '_sign_delta'), \
             patch('security.edge_privacy.get_scope_guard', return_value=guard), \
             patch('security.origin_attestation.get_attestation_for_federation',
                   return_value={'valid': False}), \
             patch('integrations.social.models.get_db', return_value=sess), \
             patch('integrations.social.peer_discovery.gossip', fake_gossip), \
             patch('core.http_pool.pooled_post', pooled_post), \
             patch('core.peer_link.link_manager.get_link_manager',
                   return_value=mgr):
            agg.broadcast_delta(delta)

        return mgr, pooled_post, delta

    def test_prefers_peerlink_when_link_is_live(self):
        link = MagicMock()
        link.is_connected = True
        mgr, pooled_post, delta = self._run_broadcast(link)

        mgr.get_link.assert_called_once_with('peerB')
        link.send.assert_called_once_with('learning', delta)
        # PeerLink carried it — no HTTP POST to that peer.
        assert pooled_post.call_count == 0

    def test_falls_back_to_http_when_no_link(self):
        mgr, pooled_post, delta = self._run_broadcast(None)

        mgr.get_link.assert_called_once_with('peerB')
        assert pooled_post.call_count == 1
        url = pooled_post.call_args[0][0]
        assert url == 'http://192.168.0.83:6777/api/social/peers/federation-delta'

    def test_falls_back_to_http_when_send_drops_link(self):
        """send() on a link that dies mid-send trips _handle_disconnect; the
        post-send is_connected=False must trigger the HTTP fallback."""
        link = MagicMock()
        link.is_connected = False   # link dropped during/after send
        mgr, pooled_post, delta = self._run_broadcast(link)

        link.send.assert_called_once_with('learning', delta)
        assert pooled_post.call_count == 1
