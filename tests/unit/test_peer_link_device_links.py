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


def _hello(phone, token=None, node_id=None, trust='same_user', x25519_hex=None, sealed=None):
    """The HELLO the phone sends (PeerLinkHandshake.buildHelloMessage): every
    value a string or an integer, signed over canonical_payload."""
    hello = {
        'type': 'hello',
        'node_id': node_id or phone.public_hex[:16],
        'ed25519_public': phone.public_hex,
        'x25519_public': x25519_hex or 'cd' * 32,
        'trust_requested': trust,
        'protocol_version': 1,
        'timestamp': int(time.time()),
    }
    if token:
        hello['device_token'] = token
    if sealed:
        hello['device_token_sealed'] = sealed
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


def _accept(hello, address='192.168.0.164:41234', ws=None):
    mgr = get_link_manager()
    with patch.object(PeerLink, 'accept', _accept_without_thread):
        return mgr.accept_inbound(str(hello.get('node_id', '')), address,
                                  ws if ws is not None else FakeWs(), hello)


def _refusal(ws):
    """The one frame a refused device HELLO is answered with, its signature
    checked against the key it names (the phone checks it against the key it
    remembers)."""
    from security.node_integrity import verify_json_signature
    assert len(ws.sent) == 1, ws.sent
    frame = json.loads(ws.sent[0])
    signature = frame.pop('signature')
    assert verify_json_signature(frame['ed25519_public'], frame, signature)
    return frame


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
    """Refused, and told only that this desktop cannot admit devices now --
    not which of its own parts failed."""
    _allow(phone)
    ws = FakeWs()
    assert _accept(_hello(phone, phone.token()), ws=ws) is None
    assert _refusal(ws)['reason'] == 'unavailable'


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


def test_get_device_link_names_only_a_live_device_link_with_a_user(phone):
    """The one lookup the handlers that answer only a phone share (the mobile
    adapter, the LiveKit tunnel): a live device link of a user -- never a
    node, a device link with no user, or one that dropped."""
    mgr = get_link_manager()
    node = PeerLink('node-7', '10.0.0.7:6777', TrustLevel.PEER)
    node._state = LinkState.CONNECTED
    node.user_id = OWNER        # a node is never a phone, whatever it carries
    mgr._links['node-7'] = node
    device = _device_link(phone)
    assert mgr.get_device_link(device.peer_id) is device
    assert mgr.get_device_link('node-7') is None
    assert mgr.get_device_link('nobody') is None
    device.user_id = ''
    assert mgr.get_device_link(device.peer_id) is None
    device.user_id = '40021'
    device._state = LinkState.DISCONNECTED
    assert mgr.get_device_link(device.peer_id) is None


def test_the_channel_policy_opens_only_control_and_events():
    assert device_may_send('control') and device_may_receive('control')
    assert device_may_receive('events') and not device_may_send('events')
    for closed in ('compute', 'dispatch', 'gossip', 'federation', 'hivemind',
                   'ralt', 'sensor', 'messages', 'learning', 'never-heard-of'):
        assert not device_may_send(closed), closed
        assert not device_may_receive(closed), closed


def test_a_phone_carries_only_its_tunnel_frames_on_tunnel(phone):
    """'tunnel' (a phone's LiveKit signalling, integrations.social.
    livekit_link): the phone opens a tunnel by asking, then streams its
    bytes and its close unasked; any other frame type, or an open nobody
    asked for, is dropped before a handler sees it."""
    assert device_may_receive('tunnel')
    link = _device_link(phone)
    seen = []
    link.on_message('tunnel', lambda channel, data, pid: seen.append(data.get('type')))
    frames = [
        {'ch': 'tunnel', 'id': 'f1', 'd': {'type': 'tunnel_data', 'id': 'a', 'b64': ''}},
        {'ch': 'tunnel', 'id': 'f2', 'd': {'type': 'tunnel_close', 'id': 'a'}},
        {'ch': 'tunnel', 'id': 'f3', 'd': {'type': 'tunnel_open', 'id': 'b'}},
        {'ch': 'tunnel', 'id': 'f4', 'd': {'type': 'shell', 'id': 'c'}},
        {'ch': 'tunnel', 'id': 'f5', 'rq': 1, 'd': {'type': 'tunnel_open', 'id': 'd'}},
    ]
    _frames_out(link, frames)
    deadline = time.monotonic() + 5
    while 'tunnel_open' not in seen and time.monotonic() < deadline:
        time.sleep(0.02)
    assert seen == ['tunnel_data', 'tunnel_close', 'tunnel_open']


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


def test_a_phone_asking_again_over_the_relay_spends_no_relay_budget(monkeypatch, phone):
    """#187 R5: relay HELLOs have no network address, so they share one ask
    budget ('relay').  A phone already asked about re-dials on every backoff;
    those repeats find the ask on file and must not use up the budget the
    next phone's first ask needs."""
    from integrations.social import discovery
    monkeypatch.setattr(discovery, '_ANNOUNCE_RATE', {})
    monkeypatch.setattr(discovery, '_RATE_LIMIT', 2)
    _install_real_verifier()
    for n in range(5):
        assert _accept(_hello(phone, phone.token()), address=f'relay:conv-{n}') is None
    second = Phone(user_id='40022', username='Second')
    assert _accept(_hello(second, second.token()), address='relay:conv-9') is None
    with db_session() as db:
        scopes = {r.scope for r in db.query(UserConsent).filter_by(
            user_id=OWNER, consent_type='device_access')}
    assert scopes == {device_scope(phone.public_hex), device_scope(second.public_hex)}


def test_a_phone_re_dialling_re_shows_its_card_at_most_every_30_seconds(monkeypatch, phone):
    """A phone the owner has not answered re-dials on every backoff.  Its
    repeats spend no shared budget (above), and they cannot flood the owner
    either: the one card is re-shown at most every _REASK_EVERY_SECONDS."""
    import hartos.hartos_bootstrap as hb
    from integrations.social import consent_service, discovery
    monkeypatch.setattr(discovery, '_ANNOUNCE_RATE', {})
    monkeypatch.setattr(hb, '_reask_at', {})
    clock = [500.0]
    monkeypatch.setattr(hb, '_reask_clock', lambda: clock[0])
    shown = []
    real_emit = consent_service._emit

    def emit(topic, data, *args, **kwargs):
        if topic == 'consent.request':
            shown.append(clock[0])
        return real_emit(topic, data, *args, **kwargs)
    monkeypatch.setattr(consent_service, '_emit', emit)
    _install_real_verifier()
    for n in range(5):                       # the first ask, then four re-dials in 4 s
        clock[0] = 500.0 + n
        assert _accept(_hello(phone, phone.token()), address=f'relay:conv-{n}') is None
    clock[0] = 540.0                         # past the window: shown once more
    assert _accept(_hello(phone, phone.token()), address='relay:conv-9') is None
    assert shown == [500.0, 501.0, 540.0]


def _frames_out(link, frames_in):
    """Run the link's real receive loop over ``frames_in``; the frames it
    sent back, as JSON."""
    frames = [json.dumps(f) for f in frames_in]
    sent = []

    class Ws:
        def recv(self, timeout=None):
            if frames:
                return frames.pop(0)
            raise ConnectionResetError('done')

        def send(self, data):
            sent.append(json.loads(data.decode('utf-8') if isinstance(data, bytes) else data))

    link._ws = Ws()
    link._receive_loop()
    return sent


def test_a_phones_heartbeat_is_answered_and_the_ack_says_so(phone):
    """On the relay a phone's socket is to central, alive whether or not this
    desktop still is: this desktop's answer to the phone's heartbeat is how
    the phone knows it is there (PeerLinkClient ends a link that stays
    silent).  The hello_ack names it, so a phone waits for an answer only
    from a desktop that sends one."""
    link = _device_link(phone)
    out = _frames_out(link, [{'ch': 'control', 'id': 'hb-1', 'd': {'type': 'heartbeat'}}])
    assert [f['d'] for f in out if f.get('ch') == 'control'] == [
        {'type': 'heartbeat', 'reply': True}]
    assert PeerLink._get_local_capabilities()['heartbeat_reply'] is True


def test_a_heartbeat_reply_is_never_answered(phone):
    """A device that echoes what it hears cannot start a ping-pong."""
    link = _device_link(phone)
    out = _frames_out(link, [{'ch': 'control', 'id': 'hb-2',
                              'd': {'type': 'heartbeat', 'reply': True}}])
    assert out == []


def test_a_nodes_heartbeat_is_not_answered():
    node = PeerLink('node-7', '10.0.0.7:6777', TrustLevel.PEER)
    node._state = LinkState.CONNECTED
    assert _frames_out(node, [{'ch': 'control', 'id': 'hb-1', 'd': {'type': 'heartbeat'}}]) == []


def _sealed_to(desktop_x25519_hex, token):
    """A phone's device token sealed the way PeerLinkHandshake seals it: the
    phone's X25519 key against the desktop's, the session key both ends
    derive (link.session_key_from), AES-256-GCM, nonce first, base64.
    Returns (the phone's X25519 public hex, the sealed token)."""
    import base64
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives import serialization as ser
    from core.peer_link.link import session_key_from
    ours = X25519PrivateKey.generate()
    key = session_key_from(ours, desktop_x25519_hex)
    nonce = os.urandom(12)
    sealed = base64.b64encode(nonce + AESGCM(key).encrypt(nonce, token.encode(), None)).decode()
    pub = ours.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex()
    return pub, sealed


def test_a_phone_whose_token_is_sealed_to_this_desktop_opens_its_device_link(phone):
    """On the relay the HELLO can cross a plaintext leg (the desktop's router
    session falls back to ws:// when the TLS one is unreachable): a phone
    that met this desktop on its LAN seals its token to the desktop's key,
    and the desktop opens it -- the same device link as a token in clear."""
    from security.channel_encryption import get_x25519_public_hex
    _allow(phone)
    _install_real_verifier()
    pub, sealed = _sealed_to(get_x25519_public_hex(), phone.token())
    link = _accept(_hello(phone, x25519_hex=pub, sealed=sealed))
    assert link is not None and link.is_connected
    assert link.kind == 'device'
    assert link.user_id == '40021'
    assert link.trust == TrustLevel.SAME_USER
    assert link.peer_id == device_fingerprint(phone.public_hex)
    assert get_link_manager().get_link(link.peer_id) is link
    assert PeerLink._get_local_capabilities()['sealed_hello'] is True


def test_a_sealed_token_that_does_not_open_here_is_refused(phone):
    """Sealed to another desktop's key: no device link, and no node link
    either -- it is not taken for a node's HELLO."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization as ser
    _allow(phone)
    _install_real_verifier()
    elsewhere = X25519PrivateKey.generate().public_key().public_bytes(
        ser.Encoding.Raw, ser.PublicFormat.Raw).hex()
    pub, sealed = _sealed_to(elsewhere, phone.token())
    asked_as_node = []

    def node_would_admit(self, hello_data, peer_ed25519):
        asked_as_node.append(peer_ed25519)
        return True
    with patch.object(PeerLink, '_decide_node_trust', node_would_admit):
        assert _accept(_hello(phone, x25519_hex=pub, sealed=sealed)) is None
    assert asked_as_node == [], "a seal that does not open fell through to the node path"
    assert get_link_manager()._links == {}


def test_a_seal_that_does_not_open_is_answered_with_this_desktops_key(phone):
    """A phone that sealed to a key this desktop no longer holds (its key file
    was replaced) is told so, under this desktop's signature, with the key to
    seal to: it re-seals at once instead of re-dialling into silence."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization as ser
    from security.channel_encryption import get_x25519_public_hex
    from security.node_integrity import get_public_key_hex
    _allow(phone)
    _install_real_verifier()
    stale = X25519PrivateKey.generate().public_key().public_bytes(
        ser.Encoding.Raw, ser.PublicFormat.Raw).hex()
    pub, sealed = _sealed_to(stale, phone.token())
    hello = _hello(phone, x25519_hex=pub, sealed=sealed)
    ws = FakeWs()
    assert _accept(hello, ws=ws) is None
    refusal = _refusal(ws)
    assert refusal['type'] == 'hello_refused'
    assert refusal['reason'] == 'seal_not_opened'
    assert refusal['x25519_public'] == get_x25519_public_hex()
    assert refusal['ed25519_public'] == get_public_key_hex()
    # Bound to the HELLO it answers: one replayed into another dial is not
    # that dial's answer, and the phone ignores it.
    assert refusal['answers'] == hello['signature']


def test_a_phone_the_owner_has_not_allowed_hears_why(phone):
    """On the relay the phone has no HTTP call to the desktop to learn its
    state from: the refused HELLO itself says it is waiting for the owner."""
    _install_real_verifier()
    ws = FakeWs()
    assert _accept(_hello(phone, phone.token()), ws=ws) is None
    assert _refusal(ws)['reason'] == 'pending'


def test_a_refused_node_hello_is_not_answered(phone, monkeypatch):
    """The refusal is a phone's: a node's HELLO refused under hard
    enforcement closes as before, unanswered."""
    monkeypatch.setattr(link_mod, '_enforcement_mode', lambda: 'hard')
    hello = _hello(phone, trust='peer')
    hello.pop('signature')
    ws = FakeWs()
    assert _accept(hello, ws=ws) is None
    assert ws.sent == []


def test_a_sealed_phone_is_bounded_by_its_owners_grant_not_the_node_budget(phone):
    """A phone is a device whether its token is clear or sealed: the node
    connection budget never turns it away."""
    from security.channel_encryption import get_x25519_public_hex
    _allow(phone)
    _install_real_verifier()
    get_link_manager()._max_links = 0
    pub, sealed = _sealed_to(get_x25519_public_hex(), phone.token())
    link = _accept(_hello(phone, x25519_hex=pub, sealed=sealed))
    assert link is not None and link.kind == 'device'
