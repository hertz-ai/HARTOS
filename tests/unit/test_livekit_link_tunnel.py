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


class SignalPort:
    """A TCP server on loopback standing in for livekit-server's signal port:
    it echoes what it reads, and records every connection and its end."""

    def __init__(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self.accepted = queue.Queue()
        self.ended = queue.Queue()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.accepted.put(conn)
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    def _echo(self, conn):
        while True:
            try:
                data = conn.recv(65536)
            except OSError:
                data = b''
            if not data:
                self.ended.put(conn)
                return
            conn.sendall(data)

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
def _manager():
    reset_link_manager()
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


def test_a_phones_bytes_reach_this_desktops_signal_port_and_back(signal_port):
    link, ws = _link()
    assert _open(ws) == {'type': 'tunnel_opened', 'id': 't1'}
    signal_port.accepted.get(timeout=WAIT_S)
    ws.frame({'type': 'tunnel_data', 'id': 't1',
              'b64': base64.b64encode(b'GET /rtc HTTP/1.1\r\n\r\n').decode()})
    back = ws.next_out('tunnel_data')['d']
    assert back['id'] == 't1'
    assert base64.b64decode(back['b64']) == b'GET /rtc HTTP/1.1\r\n\r\n'


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
