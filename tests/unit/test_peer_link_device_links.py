"""A person's phone on a PeerLink socket: a DEVICE link, not a node (#111).

A phone's HELLO carries the same device_token its HTTP calls bear.  The
node verifies it with the verifier the API gate uses, injected at boot
(hartos_bootstrap._install_device_verifier -> PeerLinkManager.
set_device_verifier), so core never imports integrations.  Only a token the
owner has allowed, for the key that signed the HELLO, opens the socket; it
opens as SAME_USER trust with kind 'device' and the token's user.  A device
link is bounded three ways: channels.py's device policy says what it may
send (control) and receive (control, events); link_manager delivers to it
only its own user's envelopes; and it is never a node -- excluded from
collect(), from the connection budget, from eviction and from reconnects.
The device's identity is its key's fingerprint, never the node_id it claims.
"""
import json
import os
import sys
import time
from unittest.mock import patch

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.peer_link import link as link_mod  # noqa: E402
from core.peer_link.channels import device_may_receive, device_may_send  # noqa: E402
from core.peer_link.link import LinkState, PeerLink, TrustLevel  # noqa: E402
from core.peer_link.link_manager import get_link_manager, reset_link_manager  # noqa: E402
from integrations.social.consent_service import (  # noqa: E402
    ConsentService, device_fingerprint, device_scope,
)
from integrations.social.models import Base, UserConsent, db_session, get_engine  # noqa: E402
from security.node_integrity import canonical_payload  # noqa: E402
from tests.unit.test_device_access_gate import Phone  # noqa: E402

OWNER = 'owner-1'


class FakeWs:
    """The duck type server.py's ASGIWebSocketAdapter presents to a link."""

    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(data)

    def recv(self, timeout=None):
        raise TimeoutError('idle')

    def close(self):
        self.closed = True


def _hello(phone, token=None, node_id=None, trust='same_user'):
    """The HELLO the phone sends (PeerLinkHandshake.buildHelloMessage): every
    value a string or an integer, signed over canonical_payload."""
    hello = {
        'type': 'hello',
        'node_id': node_id or phone.public_hex[:16],
        'ed25519_public': phone.public_hex,
        'x25519_public': 'cd' * 32,
        'trust_requested': trust,
        'protocol_version': 1,
        'timestamp': int(time.time()),
    }
    if token:
        hello['device_token'] = token
    hello['signature'] = phone.key.sign(
        canonical_payload(hello, exclude=('signature',))).hex()
    return hello


def _accept_without_thread(self_link, ws, hello):
    """link.accept() minus the receive thread: the real handshake, the real
    state, no socket to read from."""
    self_link._ws = ws
    if not self_link._complete_handshake(dict(hello)):
        self_link._state = LinkState.DISCONNECTED
        return False
    self_link._state = LinkState.CONNECTED
    return True


@pytest.fixture(autouse=True)
def _desktop(monkeypatch):
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    engine = get_engine()
    Base.metadata.create_all(engine)
    reset_link_manager()
    mgr = get_link_manager()
    mgr._links = {}
    mgr._max_links = 10
    link_mod.set_device_verifier(None)
    yield
    link_mod.set_device_verifier(None)
    reset_link_manager()
    Base.metadata.drop_all(engine)


@pytest.fixture
def phone():
    return Phone(user_id='40021', username='Sathish')


def _allow(phone):
    with db_session() as db:
        ConsentService.grant_consent(db, OWNER, 'device_access',
                                     scope=device_scope(phone.public_hex))


def _install_real_verifier():
    from hartos.hartos_bootstrap import _install_device_verifier
    _install_device_verifier()


def _accept(hello, address='192.168.0.164:41234'):
    mgr = get_link_manager()
    with patch.object(PeerLink, 'accept', _accept_without_thread):
        return mgr.accept_inbound(str(hello.get('node_id', '')), address,
                                  FakeWs(), hello)


# ── the accept path ────────────────────────────────────────────────────────


def test_an_allowed_phone_opens_a_device_link_of_its_user(phone):
    _allow(phone)
    _install_real_verifier()
    link = _accept(_hello(phone, phone.token()))
    assert link is not None and link.is_connected
    assert link.kind == 'device'
    assert link.user_id == '40021'
    assert link.trust == TrustLevel.SAME_USER
    assert link.peer_id == device_fingerprint(phone.public_hex)
    assert get_link_manager().get_link(link.peer_id) is link
    ack = json.loads(link._ws.sent[-1])
    assert ack['type'] == 'hello_ack'


def test_a_phone_the_owner_has_not_allowed_files_the_canonical_ask(phone):
    """PeerLink must produce the same owner-visible ask as the HTTP gate."""
    _install_real_verifier()
    assert _accept(_hello(phone, phone.token())) is None
    assert get_link_manager()._links == {}
    with db_session() as db:
        ask = db.query(UserConsent).filter_by(
            user_id=OWNER, consent_type='device_access',
            scope=device_scope(phone.public_hex), agent_id=None).one()
        assert ask.granted is False
        assert ask.label == 'Sathish'


def test_a_denied_phone_is_closed(phone):
    with db_session() as db:
        ConsentService.request_consent(db, OWNER, 'device_access',
                                       scope=device_scope(phone.public_hex))
        ConsentService.revoke_consent(db, OWNER, 'device_access',
                                      scope=device_scope(phone.public_hex))
    _install_real_verifier()
    assert _accept(_hello(phone, phone.token())) is None


def test_no_verifier_installed_refuses_every_device_hello(phone):
    _allow(phone)
    assert _accept(_hello(phone, phone.token())) is None


def test_a_token_for_another_key_than_the_sockets_is_closed(phone):
    """(c) the signature bound the HELLO to the socket's key; the token must
    name that same key or token and socket are two identities."""
    other = Phone(user_id='40021', username='Sathish')
    _allow(other)
    _install_real_verifier()
    assert _accept(_hello(phone, other.token())) is None


def test_a_device_never_wears_a_nodes_id(phone):
    """(f) a device claiming an existing node's node_id gets its own
    fingerprint identity and does not return, or replace, the node's link."""
    mgr = get_link_manager()
    node = PeerLink('node-7', '10.0.0.7:6777', TrustLevel.PEER)
    node._state = LinkState.CONNECTED
    mgr._links['node-7'] = node
    _allow(phone)
    _install_real_verifier()
    link = _accept(_hello(phone, phone.token(), node_id='node-7'))
    assert link is not None and link is not node
    assert link.peer_id == device_fingerprint(phone.public_hex)
    assert mgr.get_link('node-7') is node


def test_a_node_hello_is_untouched(phone):
    """No device_token: the peer path of before, PEER trust, kind node,
    counted and deduped by its node_id."""
    _install_real_verifier()
    link = _accept(_hello(phone, trust='peer'))
    assert link is not None and link.kind == 'node' and link.user_id == ''
    assert link.trust == TrustLevel.PEER
    assert link.peer_id == phone.public_hex[:16]


def test_a_redial_replaces_the_stale_device_link(phone):
    _allow(phone)
    _install_real_verifier()
    first = _accept(_hello(phone, phone.token()))
    second = _accept(_hello(phone, phone.token()), address='192.168.0.164:41999')
    assert second is not first
    assert get_link_manager().get_link(second.peer_id) is second
    assert first.state != LinkState.CONNECTED


# ── what a device may send and receive ──────────────────────────────────────


def _device_link(phone):
    _allow(phone)
    _install_real_verifier()
    return _accept(_hello(phone, phone.token()))


def test_the_channel_policy_opens_only_control_and_events():
    assert device_may_send('control') and device_may_receive('control')
    assert device_may_receive('events') and not device_may_send('events')
    for closed in ('compute', 'dispatch', 'gossip', 'federation', 'hivemind',
                   'ralt', 'sensor', 'messages', 'learning', 'never-heard-of'):
        assert not device_may_send(closed), closed
        assert not device_may_receive(closed), closed


def test_a_device_sending_on_a_closed_channel_reaches_no_handler(phone, caplog):
    """(a) drive the real receive loop with frames on hivemind, learning and
    events: nothing dispatched, one warning per channel."""
    link = _device_link(phone)
    seen = []
    for ch in ('hivemind', 'learning', 'events', 'control'):
        link.on_message(ch, lambda channel, data, pid: seen.append(channel))
    frames = [json.dumps({'ch': ch, 'id': f'm-{i}', 'd': {'type': 'x'}})
              for i, ch in enumerate(('hivemind', 'learning', 'events', 'hivemind', 'control'))]

    class Ws:
        def recv(self, timeout=None):
            if frames:
                return frames.pop(0)
            raise ConnectionResetError('done')

        def send(self, data):
            pass

    link._ws = Ws()
    with caplog.at_level('WARNING'):
        link._receive_loop()
    assert seen == ['control']
    dropped = [r for r in caplog.records if 'which devices may not' in r.getMessage()]
    assert sorted(r.getMessage().split("'")[1] for r in dropped) == ['events', 'hivemind', 'learning']


def test_broadcast_delivers_a_device_only_its_users_events(phone):
    """(b) + the per-user rule, through the real broadcast()."""
    link = _device_link(phone)
    sent = []
    link.send = lambda channel, data, **kw: sent.append((channel, data)) or True
    mgr = get_link_manager()
    own = {'msg_id': '1', 'topic': 'chat.pupit', 'data': {'user_id': '40021', 'action': 'TTS'}}
    other = {'msg_id': '2', 'topic': 'chat.pupit', 'data': {'user_id': '40099', 'action': 'TTS'}}
    nobody = {'msg_id': '3', 'topic': 'fleet.command', 'data': {'command': 'reboot'}}
    assert mgr.broadcast('events', own, trust_filter=TrustLevel.SAME_USER) == 1
    assert mgr.broadcast('events', other, trust_filter=TrustLevel.SAME_USER) == 0
    assert mgr.broadcast('events', nobody, trust_filter=TrustLevel.SAME_USER) == 0
    assert mgr.broadcast('dispatch', own, trust_filter=TrustLevel.SAME_USER) == 0
    assert mgr.broadcast('hivemind', own) == 0
    assert sent == [('events', own)]


def test_collect_never_asks_a_device(phone):
    link = _device_link(phone)
    asked = []
    link.send = lambda channel, data, **kw: asked.append(channel) or {'answer': 1}
    assert get_link_manager().collect('hivemind', timeout_ms=10) == []
    assert asked == []


# ── never a node: budget, eviction, reconnects ─────────────────────────────


def test_a_device_is_outside_the_budget_and_never_evicted(phone):
    """(e) with the budget full of nodes plus a device, admitting one more
    node evicts a node, never the phone; the device did not count."""
    mgr = get_link_manager()
    mgr._max_links = 2
    device = _device_link(phone)
    for i in range(2):
        n = PeerLink(f'node-{i}', f'10.0.0.{i}:6777', TrustLevel.PEER)
        n._state = LinkState.CONNECTED
        n._connected_at = time.monotonic()
        mgr._links[n.peer_id] = n
    assert mgr._admit('node-9') is None  # made room, the device still there
    assert device.is_connected
    assert mgr.get_link(device.peer_id) is device
    assert sum(1 for lk in mgr._links.values() if lk.kind == 'node' and lk.is_connected) == 1


def test_a_dropped_device_link_is_not_redialled(phone):
    """Its address is the phone's ephemeral host:port; the phone re-dials."""
    link = _device_link(phone)
    link._state = LinkState.DISCONNECTED
    dialled = []
    with patch.object(PeerLink, 'connect', lambda self: dialled.append(self.peer_id) or False):
        get_link_manager()._attempt_reconnects()
    assert dialled == []


# ── teardown by identity (hartos-3e review, blocking 1) ───────────────────


def _serve(hello, mgr_accept=_accept_without_thread):
    """Run server._handle_peer_link for one socket: connect, the HELLO, then
    the peer hangs up.  Returns what the server sent."""
    import asyncio
    from core.peer_link import server as server_mod
    frames = [
        {'type': 'websocket.connect'},
        {'type': 'websocket.receive', 'text': json.dumps(hello)},
        {'type': 'websocket.disconnect'},
    ]
    sent = []

    async def receive():
        return frames.pop(0)

    async def send(message):
        sent.append(message)

    scope = {'type': 'websocket', 'path': '/peer_link', 'client': ('192.168.0.164', 41234)}
    with patch.object(PeerLink, 'accept', mgr_accept):
        asyncio.run(server_mod._handle_peer_link(scope, receive, send))
    return sent


def test_a_phones_hangup_never_closes_the_node_it_named(phone):
    """(1a) the socket tears down the link it accepted -- the phone's, under
    its fingerprint -- not the node whose node_id the HELLO claimed."""
    mgr = get_link_manager()
    node = PeerLink('node-7', '10.0.0.7:6777', TrustLevel.PEER)
    node._state = LinkState.CONNECTED
    mgr._links['node-7'] = node
    _allow(phone)
    _install_real_verifier()
    _serve(_hello(phone, phone.token(), node_id='node-7'))
    assert mgr.get_link('node-7') is node and node.is_connected
    assert mgr.get_link(device_fingerprint(phone.public_hex)) is None


def test_a_phones_hangup_removes_its_own_link(phone):
    """(1b) the device link leaves the registry when its socket closes; it
    is not left behind for a re-dial to replace."""
    _allow(phone)
    _install_real_verifier()
    _serve(_hello(phone, phone.token()))
    assert get_link_manager().get_link(device_fingerprint(phone.public_hex)) is None


def test_the_old_sockets_teardown_spares_the_redialled_link(phone):
    """(6) ordering: the new socket registers its link under the same
    fingerprint BEFORE the old socket's teardown runs; the old teardown
    closes its own link object and leaves the new one in place."""
    mgr = get_link_manager()
    _allow(phone)
    _install_real_verifier()
    first = _accept(_hello(phone, phone.token()))
    second = _accept(_hello(phone, phone.token()), address='192.168.0.164:41999')
    # the first socket's finally block, as server.py now runs it
    mgr.close_link(first.peer_id, first)
    assert mgr.get_link(second.peer_id) is second and second.is_connected
    assert first.state != LinkState.CONNECTED


def test_a_nodes_second_socket_teardown_spares_the_live_link():
    """Pre-existing shape, same fix: a second socket from a live node_id is
    handed the EXISTING link (_admit True); when that socket closes it must
    not take the healthy link down.  close_link(id, link) with the link the
    socket actually holds -- the existing one -- still closes it, which is
    the server's contract for the link it was handed; what the fix rules out
    is closing a DIFFERENT link under that id."""
    mgr = get_link_manager()
    live = PeerLink('node-7', '10.0.0.7:6777', TrustLevel.PEER)
    live._state = LinkState.CONNECTED
    mgr._links['node-7'] = live
    other = PeerLink('node-7', '10.0.0.7:6778', TrustLevel.PEER)
    other._state = LinkState.CONNECTED
    mgr.close_link('node-7', other)   # a stranger's teardown by that id
    assert mgr.get_link('node-7') is live and live.is_connected
    assert other.state != LinkState.CONNECTED


# ── never a peer agent (blocking 2) ─────────────────────────────────────────


def test_a_device_link_is_not_enrolled_in_hivemind(phone):
    from integrations.agent_engine import world_model_bridge as wmb
    enrolled = []

    class Bridge:
        def register_peer_agent(self, peer_id):
            enrolled.append(peer_id)

    with patch.object(wmb, 'get_world_model_bridge', lambda: Bridge()):
        _device_link(phone)
        node = PeerLink('node-3', '10.0.0.3:6777', TrustLevel.PEER)
        node._state = LinkState.CONNECTED
        get_link_manager()._register_connected_link('node-3', node)
    assert enrolled == ['node-3']
