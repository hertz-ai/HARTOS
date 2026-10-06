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


def test_switched_off_the_node_stays_off_the_relay(monkeypatch):
    monkeypatch.setenv('HEVOLVE_PEER_LINK_RELAY', '0')
    assert relay_mod.start_relay_hub('node-x', transport=MemoryRouter().transport()) is None
    assert relay_mod.get_relay_hub() is None
