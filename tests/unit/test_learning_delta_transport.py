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
import logging
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

    def _run_broadcast(self, link, status_code=200, agg=None,
                       attestation=None):
        """Drive broadcast_delta against ONE active peer 'peerB', with a
        PeerLink manager whose get_link returns `link` (or None). Returns the
        pooled_post mock so the caller can assert HTTP was / was not used.
        `status_code` is what the receiver answers an HTTP POST with.
        `attestation` is what get_attestation_for_federation returns, or an
        exception instance for it to raise (default: an invalid one)."""
        if attestation is None:
            attestation = {'valid': False}
        att_patch = (dict(side_effect=attestation)
                     if isinstance(attestation, BaseException)
                     else dict(return_value=attestation))
        agg = agg or fa.FederatedAggregator()
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
        pooled_post.return_value.status_code = status_code
        pooled_post.return_value.text = 'unverified build' if status_code == 403 else '{}'

        with patch.object(fa, '_sign_delta'), \
             patch('security.edge_privacy.get_scope_guard', return_value=guard), \
             patch('security.origin_attestation.get_attestation_for_federation',
                   **att_patch), \
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


# ── 4. The receiver's answer is read, not discarded ────────────────────────

class TestTheReceiversAnswerCounts:
    """Measured 2026-09-23 on the owner's Lenovo (nightly df5536d): a node
    whose origin attestation failed kept 'delivering' deltas, and nothing
    anywhere recorded that central refused them. _deliver_one discarded
    pooled_post's response and returned success for ANY answer, so a
    403 'unverified build' counted as delivered, fed record_success, and left
    no log line. The answer decides the outcome now, and a change in it is
    logged once, so a rejection (and its recovery) is visible."""

    URL = 'http://192.168.0.83:6777'

    def _agg(self):
        agg = fa.FederatedAggregator()
        agg._peer_backoff = MagicMock()
        agg._peer_backoff.is_backed_off.return_value = False
        return agg

    def test_a_refused_delta_is_a_failed_delivery(self):
        agg = self._agg()
        TestBroadcastTransportSelection()._run_broadcast(None, status_code=403, agg=agg)
        agg._peer_backoff.record_failure.assert_called_once_with(self.URL)
        agg._peer_backoff.record_success.assert_not_called()

    def test_an_accepted_delta_is_a_success(self):
        agg = self._agg()
        TestBroadcastTransportSelection()._run_broadcast(None, status_code=200, agg=agg)
        agg._peer_backoff.record_success.assert_called_once_with(self.URL)
        agg._peer_backoff.record_failure.assert_not_called()

    def test_the_refusal_is_logged_with_its_status_once(self, caplog):
        import logging
        agg = self._agg()
        run = TestBroadcastTransportSelection()._run_broadcast
        with caplog.at_level(logging.WARNING):
            run(None, status_code=403, agg=agg)
            run(None, status_code=403, agg=agg)     # same answer: no repeat
        hits = [r for r in caplog.records
                if '403' in r.getMessage() and self.URL in r.getMessage()]
        assert len(hits) == 1, [r.getMessage() for r in caplog.records]

    def test_recovery_is_logged_when_the_answer_turns_good(self, caplog):
        import logging
        agg = self._agg()
        run = TestBroadcastTransportSelection()._run_broadcast
        run(None, status_code=403, agg=agg)
        with caplog.at_level(logging.INFO):
            run(None, status_code=200, agg=agg)
        assert any('accepted' in r.getMessage() and self.URL in r.getMessage()
                   for r in caplog.records), [r.getMessage() for r in caplog.records]


# ── 5. Every round says what it delivered ──────────────────────────────────

class TestTheRoundIsSummarised:
    """A node's own log could not say whether it delivered ANYTHING.
    'Federation: epoch=N' is printed only when RECEIVED deltas are
    aggregated, and a successful delivery logged nothing, so on 2026-09-26 a
    14 h session showed zero epochs and no way to tell whether its deltas
    ever left.  Each round now summarises its targets and outcomes at INFO
    when the outcome changes (and as a heartbeat), DEBUG otherwise, so a
    once-a-minute tick cannot flood the log."""

    PREFIX = 'Federation delta round:'

    def _summaries(self, caplog):
        return [r for r in caplog.records
                if r.getMessage().startswith(self.PREFIX)
                and r.levelno == logging.INFO]

    def _agg(self):
        return TestTheReceiversAnswerCounts()._agg()

    def test_the_first_round_is_summarised_with_what_it_delivered(self, caplog):
        agg = self._agg()
        with caplog.at_level(logging.INFO):
            TestBroadcastTransportSelection()._run_broadcast(None, 200, agg=agg)
        lines = [r.getMessage() for r in self._summaries(caplog)]
        assert len(lines) == 1, lines
        assert 'delivered 1' in lines[0] and 'refused 0' in lines[0], lines

    def test_an_unchanged_round_is_not_repeated_at_info(self, caplog):
        agg = self._agg()
        run = TestBroadcastTransportSelection()._run_broadcast
        with caplog.at_level(logging.INFO):
            run(None, 200, agg=agg)
            run(None, 200, agg=agg)
        assert len(self._summaries(caplog)) == 1

    def test_a_changed_outcome_is_summarised_again(self, caplog):
        agg = self._agg()
        run = TestBroadcastTransportSelection()._run_broadcast
        with caplog.at_level(logging.INFO):
            run(None, 200, agg=agg)
            run(None, 403, agg=agg)
        lines = [r.getMessage() for r in self._summaries(caplog)]
        assert len(lines) == 2, lines
        assert 'refused 1' in lines[1], lines

    def test_a_steady_outcome_still_heartbeats(self, caplog):
        agg = self._agg()
        agg._ROUND_SUMMARY_HEARTBEAT = 2
        run = TestBroadcastTransportSelection()._run_broadcast
        with caplog.at_level(logging.INFO):
            for _ in range(3):
                run(None, 200, agg=agg)
        assert len(self._summaries(caplog)) == 2

    def test_a_peerlink_delivery_counts_as_delivered(self, caplog):
        link = MagicMock()
        link.is_connected = True
        with caplog.at_level(logging.INFO):
            TestBroadcastTransportSelection()._run_broadcast(link, agg=self._agg())
        line = self._summaries(caplog)[0].getMessage()
        assert 'delivered 1 (peerlink 1)' in line, line

    def test_a_failing_attestation_is_a_warning_not_a_silent_skip(self, caplog):
        """RED before: `except Exception: pass` sent the delta unattested,
        which central refuses as 'unverified build', with no trace here."""
        with caplog.at_level(logging.WARNING):
            TestBroadcastTransportSelection()._run_broadcast(
                None, agg=self._agg(), attestation=RuntimeError('LICENSE gone'))
        assert any('attestation' in r.getMessage().lower()
                   and r.levelno >= logging.WARNING for r in caplog.records), \
            [r.getMessage() for r in caplog.records]

    def test_a_broadcast_that_cannot_reach_the_db_is_a_warning(self, caplog):
        """RED before: logged at DEBUG, invisible in every shipped log."""
        agg = self._agg()
        guard = MagicMock()
        guard.check_egress.return_value = (True, 'ok')
        with patch.object(fa, '_sign_delta'), \
             patch('security.edge_privacy.get_scope_guard', return_value=guard), \
             patch('security.origin_attestation.get_attestation_for_federation',
                   return_value={'valid': False}), \
             patch('integrations.social.models.get_db',
                   side_effect=RuntimeError('db locked')), \
             caplog.at_level(logging.WARNING):
            agg.broadcast_delta({'version': 1, 'node_id': 'selfNode'})
        assert any('Federation broadcast error' in r.getMessage()
                   and r.levelno >= logging.WARNING for r in caplog.records), \
            [r.getMessage() for r in caplog.records]
