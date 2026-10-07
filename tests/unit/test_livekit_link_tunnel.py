"""A phone's LiveKit signalling over its device link (integrations.social.
livekit_link): the phone's native LiveKit client opens its signal WebSocket
through a socket that is a tunnel over the link, and this desktop joins the
tunnel to its own LiveKit signal port -- the loopback port the supervisor
serves, never one the phone names.

Driven through the link's real receive loop (channel policy, request
threading, replies) with a real TCP server standing in for livekit-server's
signal port.
"""
import base64
import json
import os
import queue
import socket
import sys
import threading
import time

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.peer_link.link import LinkState, PeerLink, TrustLevel  # noqa: E402
from core.peer_link.link_manager import get_link_manager, reset_link_manager  # noqa: E402

WAIT_S = 5


#: What the signal port answers a request that is not a WebSocket upgrade (a
#: GET /rtc with no Upgrade header, a bad token): an HTTP error with a body,
#: the connection kept alive for the next request, as Go's HTTP server does.
REFUSAL_HEAD = b'HTTP/1.1 401 Unauthorized\r\nContent-Length: 12\r\n\r\n'
REFUSAL = REFUSAL_HEAD + b'unauthorized'
UPGRADED = b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n'
UPGRADE_REQUEST = (b'GET /rtc?access_token=t HTTP/1.1\r\nHost: localhost\r\n'
                   b'Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n')


class SignalPort:
    """A TCP server on loopback standing in for livekit-server's signal port.
    Like LiveKit's, it answers each HTTP request on a connection in turn: an
    upgrade with 101, after which it echoes what it reads (the WebSocket),
    anything else with REFUSAL, keeping the connection for the next request.
    It records every connection, every request line it was sent, and every
    connection's end.  A refusal's head and body go out as two writes, as a
    slow answer would arrive; clearing ``answering`` holds every answer until
    it is set again, and clearing ``reading`` stops it reading an upgraded
    connection (a LiveKit that stalled)."""

    def __init__(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self.accepted = queue.Queue()
        self.requests = queue.Queue()
        self.ended = queue.Queue()
        self.answering = threading.Event()
        self.answering.set()
        self.reading = threading.Event()
        self.reading.set()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.accepted.put(conn)
            threading.Thread(target=self._http, args=(conn,), daemon=True).start()

    def _http(self, conn):
        buf, upgraded = b'', False
        while True:
            if upgraded:
                self.reading.wait(WAIT_S * 4)
            try:
                data = conn.recv(65536)
            except OSError:
                data = b''
            if not data:
                self.ended.put(conn)
                return
            if upgraded:
                conn.sendall(data)
                continue
            buf += data
            while b'\r\n\r\n' in buf and not upgraded:
                head, buf = buf.split(b'\r\n\r\n', 1)
                self.requests.put(head.split(b'\r\n', 1)[0])
                upgraded = b'\r\nupgrade: websocket' in head.lower()
                self.answering.wait(WAIT_S)
                if upgraded:
                    conn.sendall(UPGRADED)
                else:
                    conn.sendall(REFUSAL_HEAD)
                    time.sleep(0.1)
                    conn.sendall(REFUSAL[len(REFUSAL_HEAD):])
            if upgraded and buf:
                conn.sendall(buf)
                buf = b''

    def request_lines(self):
        lines = []
        while not self.requests.empty():
            lines.append(self.requests.get())
        return lines

    def close(self):
        self.server.close()


class LinkSocket:
    """The socket a link reads and writes: frames in from a queue, frames out
    recorded (the desktop's tunnel threads write from their own threads)."""

    def __init__(self):
        self.incoming = queue.Queue()
        self.out = queue.Queue()

    def recv(self, timeout=None):
        try:
            item = self.incoming.get(timeout=0.05)
        except queue.Empty:
            raise TimeoutError('idle')
        if isinstance(item, BaseException):
            raise item
        return item

    def send(self, data):
        self.out.put(json.loads(data.decode('utf-8') if isinstance(data, bytes) else data))

    def close(self):
        pass

    def frame(self, d, rq=False, msg_id=None):
        env = {'ch': 'tunnel', 'id': msg_id or os.urandom(6).hex(), 'd': d}
        if rq:
            env['rq'] = 1
        self.incoming.put(json.dumps(env))

    def next_out(self, kind=None):
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            try:
                f = self.out.get(timeout=0.1)
            except queue.Empty:
                continue
            if kind is None or (f.get('d') or {}).get('type') == kind:
                return f
        raise AssertionError(f'no {kind or "frame"} from the desktop in {WAIT_S}s')


@pytest.fixture
def signal_port(monkeypatch):
    port = SignalPort()
    monkeypatch.delenv('LIVEKIT_URL', raising=False)
    monkeypatch.setenv('LIVEKIT_PORT', str(port.port))
    monkeypatch.setenv('LIVEKIT_AUTOSTART', '1')
    yield port
    port.close()


@pytest.fixture(autouse=True)
def _manager(monkeypatch):
    reset_link_manager()
    # This node serves LiveKit on its loopback, as a flat desktop does.
    monkeypatch.setenv('LIVEKIT_AUTOSTART', '1')
    monkeypatch.delenv('LIVEKIT_URL', raising=False)
    monkeypatch.delenv('LIVEKIT_PORT', raising=False)
    from integrations.social import livekit_link
    livekit_link.install()
    yield
    livekit_link.close_all()
    reset_link_manager()


def _link(kind='device'):
    """A live link of ``kind`` with its receive loop running over a LinkSocket."""
    link = PeerLink(f'{kind}-1', 'relay:conv-1', TrustLevel.SAME_USER)
    link.kind = kind
    link.user_id = '40021' if kind == 'device' else ''
    ws = LinkSocket()
    link._ws = ws
    link._state = LinkState.CONNECTED
    mgr = get_link_manager()
    mgr._apply_channel_handlers(link)
    mgr._links[link.peer_id] = link
    threading.Thread(target=link._receive_loop, daemon=True).start()
    return link, ws


def _open(ws, tid='t1'):
    ws.frame({'type': 'tunnel_open', 'id': tid}, rq=True, msg_id=f'open-{tid}')
    return ws.next_out()['d']


def _send(ws, data, tid='t1'):
    ws.frame({'type': 'tunnel_data', 'id': tid, 'b64': base64.b64encode(data).decode()})


def _received_until_close(ws, tid='t1'):
    """The bytes the desktop sent the phone on ``tid``, and its close."""
    got = b''
    while True:
        d = ws.next_out()['d']
        if d.get('id') != tid:
            continue
        if d['type'] == 'tunnel_close':
            return got, d
        got += base64.b64decode(d['b64'])


def _received(ws, n, tid='t1'):
    got = b''
    while len(got) < n:
        d = ws.next_out('tunnel_data')['d']
        if d['id'] == tid:
            got += base64.b64decode(d['b64'])
    return got


def test_a_phones_bytes_reach_this_desktops_signal_port_and_back(signal_port):
    link, ws = _link()
    assert _open(ws) == {'type': 'tunnel_opened', 'id': 't1'}
    signal_port.accepted.get(timeout=WAIT_S)
    _send(ws, UPGRADE_REQUEST)
    assert _received(ws, len(UPGRADED)) == UPGRADED
    _send(ws, b'websocket frame')
    assert _received(ws, len(b'websocket frame')) == b'websocket frame'


def test_bytes_sent_behind_the_upgrade_request_reach_livekit_once_it_upgrades(signal_port):
    """A phone's bytes that follow its upgrade request in the same chunk are
    held until LiveKit has answered 101, then sent, in order."""
    link, ws = _link()
    _open(ws)
    signal_port.accepted.get(timeout=WAIT_S)
    _send(ws, UPGRADE_REQUEST + b'first frame')
    assert _received(ws, len(UPGRADED) + len(b'first frame')) == UPGRADED + b'first frame'
    _send(ws, b'next')
    assert _received(ws, 4) == b'next'
    assert signal_port.request_lines() == [UPGRADE_REQUEST.split(b'\r\n', 1)[0]]


def test_a_signal_request_livekit_refuses_ends_the_tunnel_before_another_request(signal_port):
    """LiveKit keeps an HTTP connection open after an answer that is not an
    upgrade (no Upgrade header, a bad token).  The phone gets that answer in
    full and the tunnel closes: a second request on it -- LiveKit's server
    API -- never reaches LiveKit."""
    link, ws = _link()
    _open(ws)
    conn = signal_port.accepted.get(timeout=WAIT_S)
    _send(ws, b'GET /rtc HTTP/1.1\r\nHost: localhost\r\n\r\n')
    got, close = _received_until_close(ws)
    assert got == REFUSAL
    assert close == {'type': 'tunnel_close', 'id': 't1', 'reason': 'not_upgraded'}
    _send(ws, b'POST /twirp/livekit.RoomService/ListRooms HTTP/1.1\r\n\r\n')
    assert signal_port.ended.get(timeout=WAIT_S) is conn
    assert signal_port.request_lines() == [b'GET /rtc HTTP/1.1']


def test_a_request_sent_before_livekit_answers_waits_for_the_answer(signal_port):
    """A phone's second request, sent on its own before LiveKit has answered
    the first, is held; LiveKit refusing the first, it is never sent."""
    link, ws = _link()
    _open(ws)
    conn = signal_port.accepted.get(timeout=WAIT_S)
    signal_port.answering.clear()
    _send(ws, b'GET /rtc HTTP/1.1\r\nHost: localhost\r\n\r\n')
    assert signal_port.requests.get(timeout=WAIT_S) == b'GET /rtc HTTP/1.1'
    _send(ws, b'POST /twirp/livekit.RoomService/ListRooms HTTP/1.1\r\n\r\n')
    time.sleep(0.3)
    signal_port.answering.set()
    got, close = _received_until_close(ws)
    assert got == REFUSAL and close['reason'] == 'not_upgraded'
    assert signal_port.ended.get(timeout=WAIT_S) is conn
    assert signal_port.request_lines() == []


def test_a_request_pipelined_behind_the_signal_request_never_reaches_livekit(signal_port):
    """A second request sent in the same chunk as the first waits for
    LiveKit's answer to the first; refused, it is never sent."""
    link, ws = _link()
    _open(ws)
    conn = signal_port.accepted.get(timeout=WAIT_S)
    _send(ws, b'GET /rtc/validate HTTP/1.1\r\nHost: localhost\r\n\r\n'
              b'POST /twirp/livekit.RoomService/DeleteRoom HTTP/1.1\r\n\r\n')
    got, close = _received_until_close(ws)
    assert got == REFUSAL and close['reason'] == 'not_upgraded'
    assert signal_port.ended.get(timeout=WAIT_S) is conn
    assert signal_port.request_lines() == [b'GET /rtc/validate HTTP/1.1']


def test_a_tunnel_that_is_not_livekit_signalling_is_closed_unsent(signal_port):
    """The tunnel carries LiveKit's signal WebSocket and nothing else: a
    first request for LiveKit's server API never reaches LiveKit, and the
    phone is told why."""
    link, ws = _link()
    _open(ws)
    conn = signal_port.accepted.get(timeout=WAIT_S)
    ws.frame({'type': 'tunnel_data', 'id': 't1', 'b64': base64.b64encode(
        b'POST /twirp/livekit.RoomService/ListRooms HTTP/1.1\r\n\r\n').decode()})
    close = ws.next_out('tunnel_close')['d']
    assert close == {'type': 'tunnel_close', 'id': 't1', 'reason': 'not_signalling'}
    assert signal_port.ended.get(timeout=WAIT_S) is conn
    assert ws.out.empty(), 'LiveKit answered bytes it should never have been sent'


def test_a_dropped_link_closes_its_tunnels(signal_port):
    """A phone that vanishes (its link dropped or pruned) leaves no socket
    open on LiveKit."""
    link, ws = _link()
    _open(ws)
    conn = signal_port.accepted.get(timeout=WAIT_S)
    link._state = LinkState.DISCONNECTED
    assert signal_port.ended.get(timeout=WAIT_S) is conn


def test_the_phone_closing_its_end_closes_the_desktops_socket(signal_port):
    link, ws = _link()
    _open(ws)
    conn = signal_port.accepted.get(timeout=WAIT_S)
    ws.frame({'type': 'tunnel_close', 'id': 't1'})
    assert signal_port.ended.get(timeout=WAIT_S) is conn


def test_livekit_ending_the_socket_tells_the_phone(signal_port):
    link, ws = _link()
    _open(ws)
    conn = signal_port.accepted.get(timeout=WAIT_S)
    conn.shutdown(socket.SHUT_RDWR)
    conn.close()
    assert ws.next_out('tunnel_close')['d']['id'] == 't1'


def test_no_tunnel_when_livekit_is_not_served_on_this_desktop(monkeypatch, signal_port):
    """A LIVEKIT_URL that points away (a managed or another node's SFU) is
    not this desktop's to open: refused, named."""
    monkeypatch.setenv('LIVEKIT_URL', 'wss://sfu.example.org')
    link, ws = _link()
    reply = _open(ws)
    assert reply['type'] == 'tunnel_refused' and reply['reason'] == 'unavailable'
    assert signal_port.accepted.empty()


@pytest.mark.parametrize('env', [{'LIVEKIT_URL': 'wss://sfu.example.org'},
                                 {'LIVEKIT_PORT': 'abc'}])
def test_no_tunnel_is_offered_where_livekit_is_not_on_this_loopback(monkeypatch, env):
    """A node whose LiveKit is elsewhere (a managed SFU) or unreadable (a bad
    LIVEKIT_PORT) names no tunnel_open in its handshake: a phone would only
    have every open refused there."""
    from integrations.social import livekit_link
    reset_link_manager()
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert livekit_link.install() is False
    assert 'tunnel_open' not in get_link_manager().device_requests_answered()


def test_a_bad_livekit_port_is_answered_not_left_hanging(monkeypatch, signal_port):
    """LIVEKIT_PORT unreadable after boot: the phone's open is refused with a
    reason, not left waiting on a handler that raised."""
    monkeypatch.setenv('LIVEKIT_PORT', 'abc')
    link, ws = _link()
    reply = _open(ws)
    assert reply == {'type': 'tunnel_refused', 'id': 't1', 'reason': 'unavailable'}
    assert signal_port.accepted.empty()


def test_a_node_cannot_open_a_tunnel(signal_port):
    """Another node is not a person's phone: its open is not answered and no
    socket is opened for it."""
    link, ws = _link(kind='node')
    ws.frame({'type': 'tunnel_open', 'id': 't1'}, rq=True, msg_id='open-node')
    time.sleep(0.5)
    assert ws.out.empty()
    assert signal_port.accepted.empty()


def test_a_phone_holds_at_most_two_tunnels(signal_port):
    link, ws = _link()
    assert _open(ws, 'a')['type'] == 'tunnel_opened'
    assert _open(ws, 'b')['type'] == 'tunnel_opened'
    reply = _open(ws, 'c')
    assert reply['type'] == 'tunnel_refused' and reply['reason'] == 'busy'


def _replace_link(old):
    """The phone redials: a fresh link under the same peer id replaces the
    old one in the manager, and the old one is closed (accept_inbound)."""
    link, ws = _link()
    old._state = LinkState.DISCONNECTED
    return link, ws


def test_a_phone_that_redials_opens_at_once_and_its_old_tunnels_end(signal_port):
    """After a redial the old link's tunnels are not the phone's any more:
    the new link's opens are not refused 'busy' behind them, a tunnel id is
    reusable, and the old sockets on LiveKit close."""
    old, old_ws = _link()
    _open(old_ws, 'a')
    _open(old_ws, 'b')
    first = {signal_port.accepted.get(timeout=WAIT_S), signal_port.accepted.get(timeout=WAIT_S)}
    link, ws = _replace_link(old)
    started = time.monotonic()
    assert _open(ws, 'a') == {'type': 'tunnel_opened', 'id': 'a'}
    assert _open(ws, 'c') == {'type': 'tunnel_opened', 'id': 'c'}
    assert time.monotonic() - started < 0.9
    ended = {signal_port.ended.get(timeout=WAIT_S), signal_port.ended.get(timeout=WAIT_S)}
    assert ended == first


def test_a_livekit_that_stops_reading_ends_the_tunnel(monkeypatch, signal_port):
    """LiveKit upgraded, then stopped reading: the tunnel's writer gives up
    after the I/O timeout instead of blocking for ever, the tunnel closes and
    the phone is told -- with the write queue far from full, so it is not
    the queue's overrun that ends it."""
    from integrations.social import livekit_link
    monkeypatch.setattr(livekit_link, '_IO_TIMEOUT_S', 0.5, raising=False)
    link, ws = _link()
    _open(ws)
    conn = signal_port.accepted.get(timeout=WAIT_S)
    _send(ws, UPGRADE_REQUEST)
    assert _received(ws, len(UPGRADED)) == UPGRADED
    signal_port.reading.clear()
    chunk = b'x' * livekit_link.MAX_CHUNK
    for _ in range(160):                   # 10 MiB, 160 of the queue's 256 chunks
        _send(ws, chunk)
    try:
        close = ws.next_out('tunnel_close')['d']
    finally:
        signal_port.reading.set()
    assert close == {'type': 'tunnel_close', 'id': 't1', 'reason': 'livekit_stalled'}
    assert signal_port.ended.get(timeout=WAIT_S) is conn


def test_a_first_request_livekit_never_answers_ends_the_tunnel(monkeypatch, signal_port):
    from integrations.social import livekit_link
    monkeypatch.setattr(livekit_link, '_IO_TIMEOUT_S', 0.2, raising=False)
    monkeypatch.setattr(livekit_link, '_ANSWER_TIMEOUT_S', 0.5, raising=False)
    link, ws = _link()
    _open(ws)
    conn = signal_port.accepted.get(timeout=WAIT_S)
    signal_port.answering.clear()
    _send(ws, UPGRADE_REQUEST)
    close = ws.next_out('tunnel_close')['d']
    signal_port.answering.set()
    assert close == {'type': 'tunnel_close', 'id': 't1', 'reason': 'no_answer'}
    assert signal_port.ended.get(timeout=WAIT_S) is conn


def test_the_handshake_names_tunnels_as_answered():
    """A phone opens a tunnel only to a desktop whose hello_ack says it
    answers one (link._get_local_capabilities device_requests)."""
    assert 'tunnel_open' in PeerLink._get_local_capabilities()['device_requests']


@pytest.mark.parametrize('hosts_sfu', [True, False])
def test_boot_installs_the_tunnel_only_where_the_sfu_runs(monkeypatch, hosts_sfu):
    """The boot step that starts the SFU installs the tunnel; a node that
    hosts none (central, LIVEKIT_DISABLE) names no tunnel to a phone."""
    import hartos.hartos_bootstrap as hb
    from integrations.social import livekit_supervisor
    reset_link_manager()
    monkeypatch.setattr(livekit_supervisor, 'start_supervisor',
                        lambda: {'should_run': hosts_sfu, 'mode': 'flat'})
    hb._init_livekit_supervisor({})
    answered = 'tunnel_open' in get_link_manager().device_requests_answered()
    assert answered is hosts_sfu
