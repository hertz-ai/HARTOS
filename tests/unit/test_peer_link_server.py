"""
The inbound half of PeerLink — the half that did not exist.

`PeerLink.accept()` said it was "called by link_manager's WS server" and no such
server was ever written, so `PeerLinkManager._links` was empty on every node in
the fleet and every reader of it reported zero forever:

  * `hive_benchmark_prover._discover_nodes` reads get_status()['links'] and
    filters on state == 'connected'  -> shard fan-out ran single-node
  * `message_bus._route_peerlink` calls broadcast()                -> 0 sent
  * `claude_hive_session._register_peer_link` calls broadcast()    -> 0 sent

tests/standalone/peer_link_proof.py proves the whole path against a real
Hypercorn node. These pin the parts that could silently rot back:

  1. the wrapper is transparent — HTTP is untouched, which is the entire
     no-regression claim for mounting this on the production server
  2. trust is never taken from the wire
  3. the adapter's idle timeout means "idle", not "dead"
  4. the shared admit path cannot deadlock when the budget fills
"""
import asyncio
import queue
import unittest
from unittest.mock import MagicMock, patch

from core.peer_link.link import LinkState, PeerLink, TrustLevel
from core.peer_link.link_manager import get_link_manager, reset_link_manager
from core.peer_link.server import (
    ASGIWebSocketAdapter, PEER_LINK_PATH, peer_link_asgi, peer_link_enabled,
)


class TestWrapperIsTransparent(unittest.TestCase):
    """Every scope that is not a /peer_link websocket must pass through.

    This is the claim that lets peer_link_asgi wrap the production server:
    the HTTP surface is bit-for-bit what it was.
    """

    def setUp(self):
        self.seen = []

        async def next_app(scope, receive, send):
            self.seen.append(scope)

        self.next_app = next_app
        self.app = peer_link_asgi(next_app)

    def _run(self, scope):
        asyncio.run(self.app(scope, MagicMock(), MagicMock()))

    def test_http_scope_passes_through(self):
        self._run({'type': 'http', 'path': '/api/social/peers'})
        self.assertEqual(len(self.seen), 1)

    def test_http_scope_on_the_peer_link_path_passes_through(self):
        # Only the websocket upgrade is ours. A plain GET /peer_link stays
        # whatever Flask already made it (a 404).
        self._run({'type': 'http', 'path': PEER_LINK_PATH})
        self.assertEqual(len(self.seen), 1)

    def test_websocket_on_another_path_passes_through(self):
        self._run({'type': 'websocket', 'path': '/some/other/ws'})
        self.assertEqual(len(self.seen), 1)

    def test_lifespan_scope_passes_through(self):
        self._run({'type': 'lifespan'})
        self.assertEqual(len(self.seen), 1)

    def test_peer_link_websocket_is_intercepted(self):
        with patch('core.peer_link.server._handle_peer_link') as handler:
            async def _noop(*a, **kw):
                return None
            handler.side_effect = _noop
            self._run({'type': 'websocket', 'path': PEER_LINK_PATH})
        self.assertEqual(self.seen, [])

    def test_kill_switch_returns_the_app_unwrapped(self):
        with patch.dict('os.environ', {'HEVOLVE_PEER_LINK_SERVER': '0'}):
            self.assertFalse(peer_link_enabled())
            self.assertIs(peer_link_asgi(self.next_app), self.next_app)

    def test_enabled_by_default(self):
        with patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('HEVOLVE_PEER_LINK_SERVER', None)
            self.assertTrue(peer_link_enabled())


class TestAdapter(unittest.TestCase):
    """The adapter must be the same duck type as the outbound ws client."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.inbox = queue.Queue()

        async def _send(msg):
            self.sent.append(msg)

        self.sent = []
        self.adapter = ASGIWebSocketAdapter(self.loop, _send, self.inbox)

    def tearDown(self):
        self.loop.close()

    def test_recv_returns_none_on_idle_timeout(self):
        # link.py's _receive_loop treats None as "nothing yet, keep waiting".
        # Raising here instead would tear down a perfectly healthy link every
        # time a peer stayed quiet for the timeout.
        self.assertIsNone(self.adapter.recv(timeout=0.01))

    def test_recv_returns_the_payload(self):
        self.inbox.put(b'{"ch":"gossip"}')
        self.assertEqual(self.adapter.recv(timeout=1), b'{"ch":"gossip"}')

    def test_recv_raises_once_the_peer_is_gone(self):
        self.adapter.wake_readers()
        with self.assertRaises(ConnectionError):
            self.adapter.recv(timeout=1)

    def test_send_raises_after_close(self):
        self.adapter.mark_disconnected()
        with self.assertRaises(ConnectionError):
            self.adapter.send(b'x')

    def test_close_after_disconnect_sends_nothing(self):
        # An ASGI websocket.close after websocket.disconnect is illegal, and
        # scheduling one onto a loop the server may already be tearing down
        # would block PeerLink.close().
        self.adapter.mark_disconnected()
        self.adapter.close()
        self.assertEqual(self.sent, [])


class TestAcceptInbound(unittest.TestCase):

    def setUp(self):
        reset_link_manager()
        self.mgr = get_link_manager()
        self.mgr._links = {}
        self.mgr._max_links = 10

    def tearDown(self):
        reset_link_manager()

    def test_trust_is_never_taken_from_the_wire(self):
        """A peer claiming same_user must not be constructed as SAME_USER.

        The trust ratchet refuses downgrades, so constructing at SAME_USER
        would let an unproven claim stick. _complete_handshake ratchets UP
        only on a valid user_id_proof.
        """
        captured = {}

        def fake_accept(self_link, ws, hello):
            captured['trust'] = self_link.trust
            return False

        with patch.object(PeerLink, 'accept', fake_accept):
            self.mgr.accept_inbound(
                'peerA', '10.0.0.5:6777', MagicMock(),
                {'type': 'hello', 'node_id': 'peerA',
                 'trust_requested': 'same_user'})

        self.assertEqual(captured['trust'], TrustLevel.PEER)

    def test_failed_handshake_registers_nothing(self):
        with patch.object(PeerLink, 'accept', return_value=False):
            result = self.mgr.accept_inbound(
                'peerA', '10.0.0.5:6777', MagicMock(), {'type': 'hello'})
        self.assertIsNone(result)
        self.assertEqual(self.mgr._links, {})

    def test_successful_handshake_registers_the_link(self):
        def fake_accept(self_link, ws, hello):
            self_link._state = LinkState.CONNECTED
            return True

        with patch.object(PeerLink, 'accept', fake_accept):
            link = self.mgr.accept_inbound(
                'peerA', '10.0.0.5:6777', MagicMock(), {'type': 'hello'})

        self.assertIsNotNone(link)
        # The dict every downstream reader consumes.
        self.assertIn('peerA', self.mgr._links)
        self.assertEqual(self.mgr.get_status()['active_links'], 1)

    def test_channel_handlers_are_attached_before_accept(self):
        """accept() starts the receive thread, so a handler registered after
        it would miss whatever arrived first."""
        seen = []
        self.mgr.register_channel_handler('gossip', lambda *a: None)

        def fake_accept(self_link, ws, hello):
            seen.append(list(self_link._message_handlers.keys()))
            self_link._state = LinkState.CONNECTED
            return True

        with patch.object(PeerLink, 'accept', fake_accept):
            self.mgr.accept_inbound('peerA', '10.0.0.5:6777', MagicMock(),
                                    {'type': 'hello'})

        self.assertEqual(seen, [['gossip']])

    def test_existing_live_link_is_kept(self):
        existing = PeerLink('peerA', '10.0.0.5:6777', TrustLevel.PEER)
        existing._state = LinkState.CONNECTED
        self.mgr._links['peerA'] = existing

        with patch.object(PeerLink, 'accept') as accept:
            result = self.mgr.accept_inbound(
                'peerA', '10.0.0.5:6777', MagicMock(), {'type': 'hello'})

        accept.assert_not_called()
        self.assertIs(result, existing)


class TestAdmitDoesNotDeadlock(unittest.TestCase):
    """_evict_weakest_link takes the manager lock, and so does the close_link
    it calls. The budget check used to run while HOLDING that lock, so the
    first time a node filled its budget it would hang forever on a
    non-reentrant threading.Lock. Unreachable while _links stayed empty;
    reachable now that links form."""

    def setUp(self):
        reset_link_manager()
        self.mgr = get_link_manager()
        self.mgr._links = {}
        self.mgr._max_links = 1

    def tearDown(self):
        reset_link_manager()

    def test_over_budget_admit_returns_instead_of_hanging(self):
        for i in range(2):
            link = PeerLink(f'peer{i}', f'10.0.0.{i}:6777', TrustLevel.PEER)
            link._state = LinkState.CONNECTED
            link._ws = MagicMock()
            self.mgr._links[f'peer{i}'] = link

        done = []

        def run():
            done.append(self.mgr._admit('peer_new'))

        import threading
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=10)

        self.assertFalse(t.is_alive(), '_admit deadlocked on the manager lock')
        self.assertEqual(len(done), 1)

    def test_duplicate_check_precedes_eviction(self):
        """An already-linked peer must not cost another peer its slot."""
        existing = PeerLink('peerA', '10.0.0.5:6777', TrustLevel.PEER)
        existing._state = LinkState.CONNECTED
        self.mgr._links['peerA'] = existing

        with patch.object(self.mgr, '_evict_weakest_link') as evict:
            self.assertTrue(self.mgr._admit('peerA'))
        evict.assert_not_called()


if __name__ == '__main__':
    unittest.main()
