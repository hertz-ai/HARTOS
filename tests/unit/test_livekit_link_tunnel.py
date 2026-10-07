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
    It records every connection, every byte it read before an upgrade, every
    request line it was sent, and every connection's end.  A refusal's head
    and body go out as two writes, as a slow answer would arrive, and an
    upgrade answer in ``answer_piece``-byte writes when that is set; clearing
    ``answering`` holds every answer until it is set again, and clearing
    ``reading`` stops it reading an upgraded connection (a LiveKit that
    stalled)."""

    def __init__(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self.accepted = queue.Queue()
        self.read = queue.Queue()
        self.requests = queue.Queue()
        self.ended = queue.Queue()
        self.answering = threading.Event()
        self.answering.set()
        self.reading = threading.Event()
        self.reading.set()
        self.upgrade_answer = UPGRADED
        self.answer_piece = 0
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
            self.read.put(data)
            buf += data
            while b'\r\n\r\n' in buf and not upgraded:
                head, buf = buf.split(b'\r\n\r\n', 1)
                self.requests.put(head.split(b'\r\n', 1)[0])
                upgraded = b'\r\nupgrade: websocket' in head.lower()
                self.answering.wait(WAIT_S)
                if upgraded:
                    answer = self.upgrade_answer
                    step = self.answer_piece or len(answer)
                    for at in range(0, len(answer), step):
                        conn.sendall(answer[at:at + step])
                        if step < len(answer):
                            time.sleep(0.02)
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

    def bytes_read(self):
        """Every byte read so far before an upgrade, on every connection."""
        got = b''
        while not self.read.empty():
            got += self.read.get()
        return got

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


def _link(kind='device', peer_id=None):
    """A live link of ``kind`` with its receive loop running over a LinkSocket."""
    link = PeerLink(peer_id or f'{kind}-1', 'relay:conv-1', TrustLevel.SAME_USER)
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


def test_a_101_that_is_not_a_websocket_upgrade_releases_nothing(signal_port):
    """Only a WebSocket upgrade opens the tunnel: a 101 to another protocol
    (h2c) would let the held bytes through raw to the loopback port, so it
    ends the tunnel like any other answer and they are never sent."""
    h2c = b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: h2c\r\n\r\n'
    signal_port.upgrade_answer = h2c
    link, ws = _link()
    _open(ws)
    conn = signal_port.accepted.get(timeout=WAIT_S)
    _send(ws, UPGRADE_REQUEST + b'held bytes')
    got, close = _received_until_close(ws)
    assert got == h2c
    assert close == {'type': 'tunnel_close', 'id': 't1', 'reason': 'not_upgraded'}
    assert signal_port.ended.get(timeout=WAIT_S) is conn


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


@pytest.mark.parametrize('url,address', [
    ('ws://localhost:7880', ('127.0.0.1', 7880)),
    ('ws://127.0.0.1:7881', ('127.0.0.1', 7881)),
    ('ws://[::1]:7882', ('::1', 7882)),
    ('ws://192.168.0.5:7880', None),
    ('wss://sfu.example.org', None),
])
def test_the_signal_address_is_this_desktops_loopback_only(monkeypatch, url, address):
    """Loopback is judged by core.auth_local's one test, so IPv6 loopback
    counts and any LAN or remote host does not."""
    from integrations.social import livekit_link
    monkeypatch.setenv('LIVEKIT_URL', url)
    assert livekit_link.signal_address() == address


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
    link, ws = _link(peer_id=old.peer_id)
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


def test_one_phones_redial_leaves_another_phones_tunnels_open(signal_port):
    """The redial sweep is one phone's: a second phone's tunnel on its own
    link still carries its bytes after the first phone redials."""
    a, a_ws = _link(peer_id='device-a')
    b, b_ws = _link(peer_id='device-b')
    _open(b_ws, 'b1')
    b_conn = signal_port.accepted.get(timeout=WAIT_S)
    _open(a_ws, 'a1')
    signal_port.accepted.get(timeout=WAIT_S)
    a2, a2_ws = _replace_link(a)
    assert a2.peer_id == 'device-a'
    assert _open(a2_ws, 'a1') == {'type': 'tunnel_opened', 'id': 'a1'}
    assert signal_port.ended.get(timeout=WAIT_S) is not b_conn
    _send(b_ws, UPGRADE_REQUEST, 'b1')
    assert _received(b_ws, len(UPGRADED), 'b1') == UPGRADED


def _deadlines(monkeypatch, request_s, answer_s=10):
    from integrations.social import livekit_link
    monkeypatch.setattr(livekit_link, '_IO_TIMEOUT_S', 0.2)
    monkeypatch.setattr(livekit_link, '_REQUEST_TIMEOUT_S', request_s)
    monkeypatch.setattr(livekit_link, '_ANSWER_TIMEOUT_S', answer_s)


def test_tunnels_whose_phone_never_sends_a_request_end_and_free_its_slots(
        monkeypatch, signal_port):
    """Opened and then silent: the tunnels do not hold the phone's slots
    until its link goes; they end 'no_request' and the phone opens again."""
    from integrations.social import livekit_link
    _deadlines(monkeypatch, request_s=0.5)
    link, ws = _link()
    tids = [f't{n}' for n in range(livekit_link.MAX_TUNNELS_PER_LINK)]
    for tid in tids:
        assert _open(ws, tid) == {'type': 'tunnel_opened', 'id': tid}
    conns = {signal_port.accepted.get(timeout=WAIT_S) for _ in tids}
    closes = [ws.next_out('tunnel_close')['d'] for _ in tids]
    assert sorted(closes, key=lambda c: c['id']) == [
        {'type': 'tunnel_close', 'id': tid, 'reason': 'no_request'} for tid in tids]
    assert {signal_port.ended.get(timeout=WAIT_S) for _ in tids} == conns
    assert _open(ws, 'again') == {'type': 'tunnel_opened', 'id': 'again'}


def test_a_phone_has_the_whole_request_deadline_to_send_its_request(monkeypatch, signal_port):
    """The deadline runs from the open: idle reads before it pass, and a
    request sent within it opens the WebSocket as usual."""
    _deadlines(monkeypatch, request_s=5)
    link, ws = _link()
    _open(ws)
    signal_port.accepted.get(timeout=WAIT_S)
    time.sleep(1.0)                        # five idle reads, well inside the deadline
    assert ws.out.empty(), 'the tunnel closed before its request deadline'
    _send(ws, UPGRADE_REQUEST)
    assert _received(ws, len(UPGRADED)) == UPGRADED


def test_a_request_sent_a_byte_at_a_time_still_ends_at_the_deadline(monkeypatch, signal_port):
    """The deadline runs from the open, not from the phone's last byte: a
    phone that trickles a request it never finishes keeps no slot."""
    _deadlines(monkeypatch, request_s=0.6)
    link, ws = _link()
    _open(ws)
    conn = signal_port.accepted.get(timeout=WAIT_S)
    _send(ws, b'GET /rtc')
    ended_while_sending = False
    for _ in range(12):                    # a byte every 0.25 s for 3 s
        time.sleep(0.25)
        if not ws.out.empty():
            ended_while_sending = True
            break
        _send(ws, b'x')
    assert ended_while_sending, 'a phone trickling bytes kept its slot past the deadline'
    close = ws.next_out('tunnel_close')['d']
    assert close == {'type': 'tunnel_close', 'id': 't1', 'reason': 'no_request'}
    assert signal_port.ended.get(timeout=WAIT_S) is conn


def test_a_whole_request_waits_out_livekits_answer_deadline_not_the_requests(
        monkeypatch, signal_port):
    """Once the request is whole, only LiveKit's answer deadline applies."""
    _deadlines(monkeypatch, request_s=0.5, answer_s=5)
    link, ws = _link()
    _open(ws)
    signal_port.accepted.get(timeout=WAIT_S)
    signal_port.answering.clear()
    try:
        _send(ws, UPGRADE_REQUEST)
        time.sleep(1.2)                    # past the request's deadline, inside the answer's
        assert ws.out.empty(), 'the tunnel ended while LiveKit still had time to answer'
    finally:
        signal_port.answering.set()
    assert _received(ws, len(UPGRADED)) == UPGRADED


def test_a_call_outlives_the_request_deadline(monkeypatch, signal_port):
    """The request deadline is the first request's, never the call's: an
    upgraded tunnel idle past it stays open and carries the next frame."""
    _deadlines(monkeypatch, request_s=0.5)
    link, ws = _link()
    _open(ws)
    signal_port.accepted.get(timeout=WAIT_S)
    _send(ws, UPGRADE_REQUEST)
    assert _received(ws, len(UPGRADED)) == UPGRADED
    time.sleep(1.2)                        # idle reads well past the request deadline
    assert ws.out.empty(), 'a call was closed by the request deadline'
    _send(ws, b'frame')
    assert _received(ws, 5) == b'frame'


def test_a_livekit_that_stops_reading_ends_the_tunnel(monkeypatch, signal_port):
    """LiveKit upgraded, then stopped reading: the tunnel's writer gives up
    after the I/O timeout instead of blocking for ever, the tunnel closes and
    the phone is told -- with the write queue far from full, so it is not
    the queue's overrun that ends it."""
    from integrations.social import livekit_link
    monkeypatch.setattr(livekit_link, '_IO_TIMEOUT_S', 0.5)
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
    monkeypatch.setattr(livekit_link, '_IO_TIMEOUT_S', 0.2)
    monkeypatch.setattr(livekit_link, '_ANSWER_TIMEOUT_S', 0.5)
    link, ws = _link()
    _open(ws)
    conn = signal_port.accepted.get(timeout=WAIT_S)
    signal_port.answering.clear()
    _send(ws, UPGRADE_REQUEST)
    close = ws.next_out('tunnel_close')['d']
    signal_port.answering.set()
    assert close == {'type': 'tunnel_close', 'id': 't1', 'reason': 'no_answer'}
    assert signal_port.ended.get(timeout=WAIT_S) is conn


# ── the limits: each one ends the tunnel, and says why ────────────────────


def _close_after(ws, *datas, tid='t1'):
    for data in datas:
        _send(ws, data, tid)
    return ws.next_out('tunnel_close')['d']


def test_a_chunk_over_max_chunk_ends_the_tunnel(signal_port):
    from integrations.social import livekit_link
    link, ws = _link()
    _open(ws)
    close = _close_after(ws, b'x' * (livekit_link.MAX_CHUNK + 1))
    assert close == {'type': 'tunnel_close', 'id': 't1', 'reason': 'chunk too large'}


def test_data_that_is_not_base64_ends_the_tunnel(signal_port):
    link, ws = _link()
    _open(ws)
    # Strict base64 only: a lenient decoder would drop the '!' and read
    # 'hello' -- bytes the phone never encoded.
    ws.frame({'type': 'tunnel_data', 'id': 't1', 'b64': 'aGVsbG8=!!!!'})
    close = ws.next_out('tunnel_close')['d']
    assert close == {'type': 'tunnel_close', 'id': 't1', 'reason': 'bad data'}


@pytest.mark.parametrize('tid', ['', 'x' * 65])
def test_a_tunnel_id_outside_one_to_64_chars_is_refused(signal_port, tid):
    link, ws = _link()
    reply = _open(ws, tid)
    assert reply['type'] == 'tunnel_refused' and reply['reason'] == 'bad_id'
    assert signal_port.accepted.empty()


def test_a_first_line_with_no_end_ends_the_tunnel(signal_port):
    """Sent as a head that never ends arrives, in pieces each under the cap:
    the cap is on what has built up, and none of it reaches LiveKit."""
    from integrations.social import livekit_link
    link, ws = _link()
    _open(ws)
    conn = signal_port.accepted.get(timeout=WAIT_S)
    piece = b'x' * 1024
    close = _close_after(ws, b'GET /rtc',
                         *[piece] * (livekit_link._FIRST_LINE_MAX // len(piece)))
    assert close['reason'] == 'not_signalling'
    assert signal_port.ended.get(timeout=WAIT_S) is conn
    assert signal_port.bytes_read() == b''


def test_a_first_request_whose_headers_never_end_ends_the_tunnel(signal_port):
    from integrations.social import livekit_link
    link, ws = _link()
    _open(ws)
    conn = signal_port.accepted.get(timeout=WAIT_S)
    header = b'X-Pad: ' + b'a' * 1000 + b'\r\n'
    close = _close_after(ws, b'GET /rtc HTTP/1.1\r\n',
                         *[header] * (livekit_link._HEAD_MAX // len(header) + 1))
    assert close['reason'] == 'not_signalling'
    assert signal_port.ended.get(timeout=WAIT_S) is conn
    assert signal_port.bytes_read() == b''


def test_bytes_held_past_max_chunk_before_the_answer_end_the_tunnel(signal_port):
    """A phone sends far more before LiveKit has answered than a WebSocket
    client ever would: the held bytes are capped."""
    from integrations.social import livekit_link
    link, ws = _link()
    _open(ws)
    signal_port.answering.clear()
    try:
        close = _close_after(ws, UPGRADE_REQUEST, b'h' * livekit_link.MAX_CHUNK, b'h')
    finally:
        signal_port.answering.set()
    assert close['reason'] == 'overrun'


def test_an_answer_whose_head_never_ends_ends_the_tunnel(signal_port):
    """LiveKit's answer arrives in 4 KiB pieces, each under the cap."""
    from integrations.social import livekit_link
    signal_port.upgrade_answer = (b'HTTP/1.1 101 Switching Protocols\r\nX-Pad: '
                                  + b'a' * (livekit_link._HEAD_MAX + 1))
    signal_port.answer_piece = 4096
    link, ws = _link()
    _open(ws)
    close = _close_after(ws, UPGRADE_REQUEST)
    assert close['reason'] == 'not_upgraded'


def test_a_full_write_queue_ends_the_tunnel(monkeypatch, signal_port):
    """The phone sends faster than this desktop writes to LiveKit: once the
    write queue is full the tunnel closes ('overrun') rather than hold the
    link's receive loop.  A burst outruns the writer thread on its own (the
    queue fills within its first chunks whether or not LiveKit reads);
    LiveKit is stalled here as well, so nothing can drain it."""
    from integrations.social import livekit_link
    monkeypatch.setattr(livekit_link, '_WRITE_QUEUE', 8)
    # Longer than the burst takes, so the stalled write cannot end the
    # tunnel first ('livekit_stalled').
    monkeypatch.setattr(livekit_link, '_IO_TIMEOUT_S', 30)
    link, ws = _link()
    _open(ws)
    _send(ws, UPGRADE_REQUEST)
    assert _received(ws, len(UPGRADED)) == UPGRADED
    signal_port.reading.clear()
    try:
        for _ in range(200):
            _send(ws, b'x' * livekit_link.MAX_CHUNK)
        close = ws.next_out('tunnel_close')['d']
    finally:
        signal_port.reading.set()
    assert close == {'type': 'tunnel_close', 'id': 't1', 'reason': 'overrun'}


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
