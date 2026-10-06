"""The PeerLink relay: the rung a NAT'd node answers on (core.peer_link.relay).

Both ends dial out to one WAMP router and meet in each other's inbox topic.
The router is shared with strangers, so a relay link carries the
X25519/AES-GCM session whatever its trust, refuses to run without one, and
after HELLO / HELLO_ACK the router sees only ciphertext.  These run the real
handshake, the real crypto and the real receive loop over an in-memory router
that records every publish, so "what the router saw" is measured, not assumed.
"""
import base64
import json
import os
import sys
import threading
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.peer_link import link as link_mod  # noqa: E402
from core.peer_link import relay as relay_mod  # noqa: E402
from core.peer_link.link import LinkState, PeerLink, TrustLevel  # noqa: E402
from core.peer_link.relay import (  # noqa: E402
    RELAY_ADDRESS_SCHEME, RelayHub, relay_topic,
)


class MemoryRouter:
    """A WAMP router in one process: one subscriber per topic, every publish
    recorded and delivered in order, as a JSON round trip (the wire)."""

    def __init__(self):
        self.subscribers = {}
        self.published = []
        self._lock = threading.Lock()

    def transport(self):
        return MemoryTransport(self)

    def deliver(self, topic, envelope):
        wire = json.loads(json.dumps(envelope))
        with self._lock:
            self.published.append((topic, wire))
            callback = self.subscribers.get(topic)
        if callback is not None:
            callback(wire)

    def frames(self):
        """Every payload the router carried, decoded."""
        return [base64.b64decode(env['b']) for _, env in self.published if 'b' in env]


class MemoryTransport:
    def __init__(self, router):
        self.router = router
        self.joined = threading.Event()

    def start(self, topic, on_message):
        self.router.subscribers[topic] = on_message
        self.joined.set()
        return True

    def publish(self, topic, envelope):
        self.router.deliver(topic, envelope)

    def stop(self):
        self.joined.clear()


@pytest.fixture
def router():
    return MemoryRouter()


@pytest.fixture
def same_user(monkeypatch):
    # Both ends run in this process under one node key, so a SAME_USER proof
    # signed over this id verifies on the far side.
    monkeypatch.setenv('HEVOLVE_USER_ID', 'owner-relay-test')


def _accepting_hub(router, endpoint='node-b', handlers=None):
    """A node on the relay whose inbound HELLOs become real accepted links."""
    hub = RelayHub(endpoint, router.transport())
    accepted = []

    def accept(sock, hello):
        link = PeerLink(peer_id=str(hello.get('node_id') or ''),
                        address=sock.address, trust=TrustLevel.PEER)
        for channel, handler in (handlers or {}).items():
            link.on_message(channel, handler)
        if link.accept(sock, hello):
            accepted.append(link)
            return link
        return None

    hub.on_inbound(accept)
    hub.start()
    return hub, accepted


def _dial(router, hub_b, trust, endpoint='node-a'):
    hub_a = RelayHub(endpoint, router.transport())
    hub_a.start()
    link = PeerLink(peer_id=hub_b.endpoint_id,
                    address=RELAY_ADDRESS_SCHEME + hub_b.endpoint_id, trust=trust)
    with patch.object(relay_mod, 'get_relay_hub', return_value=hub_a):
        ok = link.connect()
    return hub_a, link, ok


def _wait(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_own_nodes_on_the_relay_are_encrypted_though_same_user_trust_is_plaintext(router, same_user):
    hub_b, accepted = _accepting_hub(router)
    _, link, ok = _dial(router, hub_b, TrustLevel.SAME_USER)
    try:
        assert ok and link.is_connected
        assert _wait(lambda: accepted and accepted[0].is_connected)
        far = accepted[0]
        # Same user on both ends -- the trust that is plaintext on a websocket --
        assert link.trust == TrustLevel.SAME_USER and far.trust == TrustLevel.SAME_USER
        # -- and still sealed, because the router is shared with strangers.
        assert link.requires_e2e and far.requires_e2e
        assert link.is_encrypted and far.is_encrypted
    finally:
        link.close()


def test_after_the_handshake_the_router_sees_only_ciphertext(router, same_user):
    got = []
    hub_b, accepted = _accepting_hub(
        router, handlers={'gossip': lambda ch, data, pid: got.append(data)})
    _, link, ok = _dial(router, hub_b, TrustLevel.SAME_USER)
    try:
        assert ok
        link.send('gossip', {'note': 'SECRET-PAYLOAD-7f3a'})
        assert _wait(lambda: got)
        assert got[0] == {'note': 'SECRET-PAYLOAD-7f3a'}

        frames = router.frames()
        # hello, hello_ack: the only frames the router can read
        readable = []
        for raw in frames:
            try:
                readable.append(json.loads(raw.decode('utf-8'))['type'])
            except Exception:
                pass
        assert readable == ['hello', 'hello_ack']
        assert len(frames) >= 3
        assert not any(b'SECRET-PAYLOAD-7f3a' in raw for raw in frames)
    finally:
        link.close()


def test_a_request_crosses_the_relay_and_its_reply_comes_back(router, same_user):
    hub_b, _ = _accepting_hub(
        router, handlers={'dispatch': lambda ch, data, pid: {'echo': data.get('n')}})
    _, link, ok = _dial(router, hub_b, TrustLevel.PEER)
    try:
        assert ok
        reply = link.send('dispatch', {'n': 41}, wait_response=True, timeout=5)
        assert reply == {'echo': 41}
    finally:
        link.close()


def test_a_relay_hello_without_an_x25519_key_is_refused_unanswered(router):
    from security.node_integrity import (
        get_node_identity, get_public_key_hex, sign_json_payload)
    hub_b, accepted = _accepting_hub(router)
    hub_a = RelayHub('node-a', router.transport())
    hub_a.start()
    hello = {
        'type': 'hello',
        'node_id': get_node_identity()['node_id'],
        'ed25519_public': get_public_key_hex(),
        'x25519_public': '',
        'trust_requested': 'peer',
        'protocol_version': 1,
        'capabilities': {},
        'timestamp': time.time(),
    }
    hello['signature'] = sign_json_payload(hello)
    sock = hub_a.dial('node-b')
    sock.send(json.dumps(hello).encode('utf-8'))
    # refused: the accept thread closes the conversation without an ack
    assert _wait(lambda: hub_b.conversation_count() == 0)
    assert accepted == []
    sent_to_a = [env for topic, env in router.published
                 if topic == relay_topic('node-a') and 'b' in env]
    assert sent_to_a == []


def test_a_forged_frame_in_a_live_conversation_is_dropped_and_the_link_stays_up(router, same_user):
    hub_b, accepted = _accepting_hub(
        router, handlers={'dispatch': lambda ch, data, pid: {'ok': data.get('n')}})
    hub_a, link, ok = _dial(router, hub_b, TrustLevel.SAME_USER)
    try:
        assert ok and _wait(lambda: accepted)
        conversation = link._ws.conversation
        # Anyone on the realm can publish into the conversation; without the
        # session key the frame cannot open and is dropped.
        forged = json.dumps({'ch': 'dispatch', 'id': 'x1', 'rq': 1,
                             'd': {'n': 666}}).encode()
        router.deliver(relay_topic('node-b'), {
            'c': conversation, 'r': relay_topic('node-a'),
            'b': base64.b64encode(forged).decode()})
        time.sleep(0.2)
        assert accepted[0].is_connected
        assert link.send('dispatch', {'n': 5}, wait_response=True, timeout=5) == {'ok': 5}
    finally:
        link.close()


def test_only_a_hello_from_another_inbox_opens_a_conversation(router):
    calls = []
    hub = RelayHub('node-b', router.transport())
    hub.on_inbound(lambda sock, hello: calls.append(hello) or None)
    hub.start()
    not_hello = base64.b64encode(json.dumps({'type': 'gossip'}).encode()).decode()
    hello = base64.b64encode(json.dumps({'type': 'hello'}).encode()).decode()
    router.deliver(relay_topic('node-b'), {'c': 'c1', 'r': relay_topic('a'), 'b': not_hello})
    router.deliver(relay_topic('node-b'), {'c': 'c2', 'r': 'com.evil.topic', 'b': hello})
    router.deliver(relay_topic('node-b'), {'c': 'c3', 'r': relay_topic('node-b'), 'b': hello})
    router.deliver(relay_topic('node-b'), {'c': 'c4', 'r': relay_topic('a'), 'b': '!!not-b64'})
    time.sleep(0.1)
    assert calls == []
    router.deliver(relay_topic('node-b'), {'c': 'c5', 'r': relay_topic('a'), 'b': hello})
    assert _wait(lambda: len(calls) == 1)


def test_open_conversations_are_bounded(router):
    release = threading.Event()
    hub = RelayHub('node-b', router.transport())
    hub.on_inbound(lambda sock, hello: release.wait(5) and None)
    hub.start()
    hello = base64.b64encode(json.dumps({'type': 'hello'}).encode()).decode()
    with patch.object(relay_mod, 'MAX_CONVERSATIONS', 2):
        for n in range(3):
            router.deliver(relay_topic('node-b'),
                           {'c': f'c{n}', 'r': relay_topic('a'), 'b': hello})
        assert hub.conversation_count() == 2
    release.set()
    assert _wait(lambda: hub.conversation_count() == 0)


def test_an_x509_wrapped_x25519_key_is_the_same_key():
    raw = bytes(range(32)).hex()
    wrapped = link_mod._X25519_SPKI_PREFIX.hex() + raw
    assert link_mod._x25519_raw(wrapped) == bytes.fromhex(raw)
    assert link_mod._x25519_raw(raw) == bytes.fromhex(raw)

    from security.channel_encryption import get_x25519_public_hex
    peer = get_x25519_public_hex()
    a = PeerLink('p', 'h', TrustLevel.PEER, x25519_public_hex=peer)
    b = PeerLink('p', 'h', TrustLevel.PEER,
                 x25519_public_hex=link_mod._X25519_SPKI_PREFIX.hex() + peer)
    a._derive_session_key()
    b._derive_session_key()
    assert a._session_key is not None and a._session_key == b._session_key


class _PlainWs:
    """A websocket between two of the owner's machines on the LAN."""

    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def recv(self, timeout=None):
        raise TimeoutError('idle')

    def close(self):
        pass


def test_a_websocket_link_between_own_nodes_is_unchanged():
    link = PeerLink('peer-1', '192.168.0.9:6777', TrustLevel.SAME_USER,
                    x25519_public_hex='ab' * 32)
    link._ws = _PlainWs()
    link._state = LinkState.CONNECTED
    link.send('gossip', {'n': 1})
    assert not link.requires_e2e and not link.is_encrypted
    assert json.loads(link._ws.sent[0])['d'] == {'n': 1}


def test_the_relay_rung_is_tried_after_the_direct_address():
    from core.peer_link.link_manager import PeerLinkManager
    manager = PeerLinkManager()
    tried = []

    def upgrade_peer(peer_id, address, trust, x25519_public='', ed25519_public=''):
        tried.append(address)
        return address.startswith(RELAY_ADDRESS_SCHEME)

    class Hub:
        joined = True

    peer = {'node_id': 'peer-nat', 'url': 'http://10.0.0.7:6777'}

    class Gossip:
        @staticmethod
        def get_peer_list():
            return [peer]

    class Nat:
        @staticmethod
        def resolve_peer_address(info):
            return None

    with patch.dict(sys.modules, {
            'integrations.social.peer_discovery': type(sys)('pd')}), \
            patch.object(manager, 'upgrade_peer', side_effect=upgrade_peer), \
            patch.object(relay_mod, 'get_relay_hub', return_value=Hub()), \
            patch('core.peer_link.nat.get_nat_traversal', return_value=Nat()):
        sys.modules['integrations.social.peer_discovery'].gossip = Gossip()
        manager._try_auto_upgrade('peer-nat')
    assert tried == ['10.0.0.7:6777', 'relay://peer-nat']


def test_the_handshake_names_the_inbox_a_phone_reaches_this_node_at(router):
    """A phone that met this node on the LAN keeps the inbox the hello_ack
    names and dials relay://<inbox> from anywhere; off the relay the
    handshake names none."""
    hub = RelayHub('a1b2c3d4e5f60718', router.transport())
    assert hub.start()
    with patch.object(relay_mod, 'get_relay_hub', return_value=hub):
        assert PeerLink._get_local_capabilities()['relay'] == 'a1b2c3d4e5f60718'
    hub.stop()
    with patch.object(relay_mod, 'get_relay_hub', return_value=hub):
        assert 'relay' not in PeerLink._get_local_capabilities()
    with patch.object(relay_mod, 'get_relay_hub', return_value=None):
        assert 'relay' not in PeerLink._get_local_capabilities()


def test_switched_off_the_node_stays_off_the_relay(monkeypatch):
    monkeypatch.setenv('HEVOLVE_PEER_LINK_RELAY', '0')
    assert relay_mod.start_relay_hub('node-x', transport=MemoryRouter().transport()) is None
    assert relay_mod.get_relay_hub() is None


def test_unit_tests_never_join_the_production_router():
    # tests/conftest.py keeps the suite off the relay: a test that runs the
    # real bootstrap (test_desktop_socket_gate) must not open a session to
    # central's router, nor leave a hub behind for the tests after it.
    assert os.environ.get('HEVOLVE_PEER_LINK_RELAY') == '0'
    assert relay_mod.start_relay_hub('node-x') is None
    assert relay_mod.get_relay_hub() is None


class _DroppedTransport:
    """Joined, but the router session drops before the HELLO goes out."""

    def __init__(self):
        self.joined = threading.Event()

    def start(self, topic, on_message):
        self.joined.set()
        return True

    def publish(self, topic, envelope):
        raise ConnectionError('router session dropped')

    def stop(self):
        pass


def test_a_dial_that_fails_mid_handshake_gives_its_conversation_back():
    # A failed dial must not keep a conversation open: past MAX_CONVERSATIONS
    # the hub drops every inbound HELLO, so leaked dials would lock phones out.
    hub = RelayHub('node-a', _DroppedTransport())
    hub.start()
    with patch.object(relay_mod, 'get_relay_hub', return_value=hub):
        for _ in range(3):
            link = PeerLink(peer_id='node-b', address='relay://node-b',
                            trust=TrustLevel.PEER)
            assert link.connect() is False
    assert hub.conversation_count() == 0


def test_the_router_is_met_over_tls_first():
    urls = relay_mod.relay_router_urls()
    assert urls[0].startswith('wss://')
    assert all(u.startswith(('ws://', 'wss://')) for u in urls)


def test_an_operator_router_is_the_only_one_tried(monkeypatch):
    monkeypatch.setenv('HEVOLVE_PEER_LINK_RELAY_URL', 'wss://regional.example:9443/wss')
    assert relay_mod.relay_router_urls() == ['wss://regional.example:9443/wss']


# ── #187: what a stranger on the realm can and cannot do ───────────────────


def test_a_forged_close_cannot_end_a_live_link(router, same_user):
    # Anyone in the realm reads a conversation id off the inbox topic and can
    # publish {'c': id, 'x': 1} to either end.  Once the session key exists
    # the link ends only on the peer's sealed goodbye.
    hub_b, accepted = _accepting_hub(
        router, handlers={'dispatch': lambda ch, data, pid: {'ok': data.get('n')}})
    hub_a, link, ok = _dial(router, hub_b, TrustLevel.SAME_USER)
    try:
        assert ok and _wait(lambda: accepted and accepted[0].is_connected)
        conversation = link._ws.conversation
        router.deliver(relay_topic('node-b'), {'c': conversation, 'r': relay_topic('node-a'), 'x': 1})
        router.deliver(relay_topic('node-a'), {'c': conversation, 'r': relay_topic('node-b'), 'x': 1})
        time.sleep(0.3)
        assert accepted[0].is_connected and link.is_connected
        assert link.send('dispatch', {'n': 6}, wait_response=True, timeout=5) == {'ok': 6}
    finally:
        link.close()


def test_closing_a_relay_link_ends_its_far_side(router, same_user):
    hub_b, accepted = _accepting_hub(router)
    hub_a, link, ok = _dial(router, hub_b, TrustLevel.SAME_USER)
    assert ok and _wait(lambda: accepted and accepted[0].is_connected)
    link.close()
    assert _wait(lambda: not accepted[0].is_connected)
    assert _wait(lambda: hub_b.conversation_count() == 0)
    assert hub_a.conversation_count() == 0


def test_a_refused_hello_frees_the_dialer_at_once(router):
    # Before any key exists the close is all a refused dialer gets: it must
    # still end the dial now, not after the handshake's 10 s wait.
    hub_b = RelayHub('node-b', router.transport())
    hub_b.on_inbound(lambda sock, hello: None)
    hub_b.start()
    started = time.monotonic()
    hub_a, link, ok = _dial(router, hub_b, TrustLevel.PEER)
    assert ok is False
    assert time.monotonic() - started < 3
    assert hub_a.conversation_count() == 0


def test_a_peer_not_on_the_relay_is_not_dialled_there_again_soon():
    from core.peer_link.link_manager import PeerLinkManager
    manager = PeerLinkManager()
    tried = []

    def upgrade_peer(peer_id, address, trust, x25519_public='', ed25519_public=''):
        tried.append(address)
        return False

    class Hub:
        joined = True

    peer = {'node_id': 'peer-nat', 'url': 'http://10.0.0.7:6777'}

    class Gossip:
        @staticmethod
        def get_peer_list():
            return [peer]

    class Nat:
        @staticmethod
        def resolve_peer_address(info):
            return None

    with patch.dict(sys.modules, {
            'integrations.social.peer_discovery': type(sys)('pd')}), \
            patch.object(manager, 'upgrade_peer', side_effect=upgrade_peer), \
            patch.object(relay_mod, 'get_relay_hub', return_value=Hub()), \
            patch('core.peer_link.nat.get_nat_traversal', return_value=Nat()):
        sys.modules['integrations.social.peer_discovery'].gossip = Gossip()
        manager._try_auto_upgrade('peer-nat')
        manager._try_auto_upgrade('peer-nat')
    # The relay rung answered nothing once: the next upgrade tries only the
    # direct address, so a peer off the relay costs the gossip thread no
    # handshake wait on every exchange.
    assert tried == ['10.0.0.7:6777', 'relay://peer-nat', '10.0.0.7:6777']


def test_the_relay_joins_one_router_at_a_time_and_says_which(caplog):
    import logging
    attempts = []
    transport = relay_mod.WampRelayTransport(['wss://down.example/wss', 'ws://up.example/ws'])

    class FakeSession:
        async def subscribe(self, handler, topic):
            pass

        def publish(self, *args):
            pass

        def leave(self):
            pass

    class FakeComponent:
        def __init__(self, transports, realm):
            self.urls = [t['url'] for t in transports]
            attempts.append(self.urls)
            self._join = None

        def on_join(self, fn):
            self._join = fn
            return fn

        def on_leave(self, fn):
            return fn

        async def start(self, loop=None):
            if self.urls[0].startswith('wss://down'):
                raise ConnectionError('refused')
            await self._join(FakeSession(), object())
            transport._stop = True          # one join is the whole run

    autobahn = type(sys)('autobahn')
    asyncio_pkg = type(sys)('autobahn.asyncio')
    component_mod = type(sys)('autobahn.asyncio.component')
    component_mod.Component = FakeComponent
    with patch.dict(sys.modules, {'autobahn': autobahn, 'autobahn.asyncio': asyncio_pkg,
                                  'autobahn.asyncio.component': component_mod}), \
            caplog.at_level(logging.INFO, logger='hevolve.peer_link'):
        try:
            assert transport.start('com.hertzai.hevolve.peerlink.relay.node-x', lambda e: None)
            transport._thread.join(5)
        finally:
            transport._stop = True
    assert attempts == [['wss://down.example/wss'], ['ws://up.example/ws']]
    assert 'PeerLink relay joined ws://up.example/ws' in caplog.text


def _fake_autobahn(attempts, join_on, transport, stop_after):
    """autobahn modules whose Component records each URL tried; a URL in
    join_on joins and then drops at once; the run stops after stop_after
    attempts."""
    class FakeSession:
        async def subscribe(self, handler, topic):
            pass

        def leave(self):
            pass

    class FakeComponent:
        def __init__(self, transports, realm):
            self.urls = [t['url'] for t in transports]
            attempts.append(self.urls)
            self._join = None

        def on_join(self, fn):
            self._join = fn
            return fn

        def on_leave(self, fn):
            return fn

        async def start(self, loop=None):
            if len(attempts) >= stop_after:
                transport._stop = True
            if self.urls[0] not in join_on:
                raise ConnectionError('refused')
            await self._join(FakeSession(), object())   # joined, then the session ends

    autobahn = type(sys)('autobahn')
    asyncio_pkg = type(sys)('autobahn.asyncio')
    component_mod = type(sys)('autobahn.asyncio.component')
    component_mod.Component = FakeComponent
    return {'autobahn': autobahn, 'autobahn.asyncio': asyncio_pkg,
            'autobahn.asyncio.component': component_mod}


def test_a_router_that_took_us_and_dropped_us_is_retried_before_the_plaintext_one():
    """The TLS router joined and then dropped the session: the next try is
    that router again, after the backoff, not an immediate step down to the
    plaintext URL.  Only a router that cannot be reached moves us on."""
    attempts = []
    transport = relay_mod.WampRelayTransport(['wss://tls.example/wss', 'ws://plain.example/ws'])
    with patch.dict(sys.modules, _fake_autobahn(attempts, {'wss://tls.example/wss'}, transport, 3)),             patch('time.sleep'):
        try:
            assert transport.start('com.hertzai.hevolve.peerlink.relay.node-x', lambda e: None)
            transport._thread.join(5)
        finally:
            transport._stop = True
    assert attempts == [['wss://tls.example/wss']] * 3


def test_a_peer_missed_on_the_relay_is_skipped_a_minute_then_longer_and_an_answer_clears_it(monkeypatch):
    """One miss skips the peer's relay rung for 60 s, each further miss in a
    row doubles that (to 600 s at most), and an answer forgets the misses: a
    peer reachable only on the relay is not cut off for ten minutes by one
    transient miss."""
    from core.peer_link import link_manager as lm
    manager = lm.PeerLinkManager()
    clock = [1000.0]
    monkeypatch.setattr(lm, '_now', lambda: clock[0])
    relay_tries, answers = [], {'relay': False}

    def upgrade_peer(peer_id, address, trust, x25519_public='', ed25519_public=''):
        if address.startswith('relay://'):
            relay_tries.append(clock[0])
            return answers['relay']
        return False

    class Hub:
        joined = True

    peer = {'node_id': 'peer-nat', 'url': 'http://10.0.0.7:6777'}

    class Gossip:
        @staticmethod
        def get_peer_list():
            return [peer]

    class Nat:
        @staticmethod
        def resolve_peer_address(info):
            return None

    def at(t, answer=False):
        clock[0] = 1000.0 + t
        answers['relay'] = answer
        manager._try_auto_upgrade('peer-nat')

    with patch.dict(sys.modules, {
            'integrations.social.peer_discovery': type(sys)('pd')}),             patch.object(manager, 'upgrade_peer', side_effect=upgrade_peer),             patch.object(relay_mod, 'get_relay_hub', return_value=Hub()),             patch('core.peer_link.nat.get_nat_traversal', return_value=Nat()):
        sys.modules['integrations.social.peer_discovery'].gossip = Gossip()
        at(0)                       # miss 1: skipped for 60 s
        at(30)
        at(61)                      # miss 2: skipped for 120 s
        at(150)
        at(182, answer=True)        # answers: the misses are forgotten
        at(200)                     # miss 1 again: 60 s, not 240 s
        at(261)
    assert [t - 1000.0 for t in relay_tries] == [0, 61, 182, 200, 261]
