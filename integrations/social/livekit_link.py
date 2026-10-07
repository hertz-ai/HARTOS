"""A phone's LiveKit signalling, over its device link to this desktop.

Owner 10-07: the phone's native LiveKit client talks to this desktop's own
livekit-server -- signalling over the PeerLink link the phone already holds
(the LAN, else the relay), media peer to peer over ICE, no TURN.  The LiveKit
Android SDK opens its signal WebSocket through the OkHttp client it is given
(LiveKitOverrides.okHttpClient; its WebModule.websocketFactory returns that
client), so the phone gives it one whose sockets are tunnels over the link,
and the WebSocket's bytes arrive here.  This module joins each tunnel to this
desktop's signal port: the loopback port the supervisor serves LiveKit on
(livekit_supervisor.get_livekit_url), never a host or port the phone names.
LiveKit checks the room token on that socket as on any other.

On the 'tunnel' channel (core.peer_link.channels), only from a person's own
phone (a device link):

  tunnel_open  {id}       a request; answered tunnel_opened {id}, or
                          tunnel_refused {id, reason}: 'unavailable' (LiveKit
                          is not served on this desktop's loopback),
                          'unreachable' (its port did not answer), 'busy'
                          (MAX_TUNNELS_PER_LINK open), 'bad_id'
  tunnel_data  {id, b64}  bytes, either way
  tunnel_close {id}       either way; the other end closes its side (from
                          here with a reason)

The tunnel carries LiveKit signalling and nothing else: its first request
must be a GET of LiveKit's signal path (/rtc -- the WebSocket, and the
SDK's /rtc/validate), or the tunnel is closed ('not_signalling') before a
byte reaches LiveKit -- so a phone cannot speak LiveKit's server API through
it.  A tunnel whose link is gone (dropped, pruned idle, replaced by the
phone's redial) closes within a second.

Relay links are end-to-end encrypted (relay.py), so the relay reads none of
it.  Media does not ride the tunnel: LiveKit's ICE candidates name this
desktop's own addresses.
"""
import base64
import binascii
import logging
import queue
import re
import socket
import threading
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

from core.peer_link.channels import TUNNEL_CLOSE, TUNNEL_DATA, TUNNEL_OPEN

logger = logging.getLogger('hevolve_social')

CHANNEL = 'tunnel'
TUNNEL_OPENED = 'tunnel_opened'
TUNNEL_REFUSED = 'tunnel_refused'

#: Tunnels one phone holds at once: a call's signal socket and one spare for
#: the SDK's reconnect, which opens the new socket before the old one closes.
MAX_TUNNELS_PER_LINK = 2
#: The most bytes one tunnel_data frame carries.
MAX_CHUNK = 64 * 1024
#: Chunks waiting to be written to LiveKit before the tunnel is given up.
_WRITE_QUEUE = 256
_CONNECT_TIMEOUT_S = 5
_LOOPBACK = ('127.0.0.1', '::1', 'localhost')
#: The first request on a tunnel: a GET of LiveKit's signal path.
_SIGNAL_REQUEST = re.compile(rb'GET /rtc(?:[/?][^ ]*)? HTTP/1\.[01]\r?')
#: How much of the first request line is read before it must have ended.
_FIRST_LINE_MAX = 8 * 1024

#: (peer_id, tunnel id) -> its tunnel; None while its socket is connecting.
_tunnels: Dict[Tuple[str, str], Optional['_Tunnel']] = {}
_lock = threading.Lock()


def signal_address() -> Optional[Tuple[str, int]]:
    """This desktop's LiveKit signal port, or None when LiveKit is not served
    here on loopback (a node that hosts no SFU, or a LIVEKIT_URL naming
    another host)."""
    from .livekit_supervisor import get_livekit_url, supervisor_should_run
    if not supervisor_should_run():
        return None
    parts = urlsplit(get_livekit_url())
    host = parts.hostname or ''
    if host not in _LOOPBACK:
        return None
    port = parts.port or (443 if parts.scheme in ('wss', 'https') else 80)
    return ('127.0.0.1' if host == 'localhost' else host, port)


class _Tunnel:
    """One tunnel: a socket to LiveKit's signal port and a link to a phone.
    A reader thread sends what LiveKit writes; a writer thread writes what
    the phone sends, so the link's receive loop never blocks on the socket."""

    def __init__(self, link, tid: str, sock: socket.socket):
        self.link = link
        self.tid = tid
        self.sock = sock
        self._out: 'queue.Queue' = queue.Queue(maxsize=_WRITE_QUEUE)
        self._closed = threading.Event()
        # The phone's bytes until its first request line is whole and seen to
        # be LiveKit signalling (None once it was).
        self._head: Optional[bytes] = b''

    def start(self) -> None:
        threading.Thread(target=self._read, daemon=True,
                         name=f'livekit-tunnel-r-{self.tid[:8]}').start()
        threading.Thread(target=self._write, daemon=True,
                         name=f'livekit-tunnel-w-{self.tid[:8]}').start()

    def push(self, data: bytes) -> None:
        if self._head is not None:
            self._head += data
            if b'\n' not in self._head:
                if len(self._head) > _FIRST_LINE_MAX:
                    self._not_signalling()
                return
            if not _SIGNAL_REQUEST.fullmatch(self._head.split(b'\n', 1)[0]):
                self._not_signalling()
                return
            data, self._head = self._head, None
        try:
            self._out.put_nowait(data)
        except queue.Full:
            logger.warning("LiveKit tunnel %s: %d chunks unwritten; closing it",
                           self.tid[:8], _WRITE_QUEUE)
            self.close('overrun', tell_phone=True)

    def _not_signalling(self) -> None:
        line = (self._head or b'').split(b'\n', 1)[0][:80]
        logger.warning("LiveKit tunnel %s from %s refused: its first request is not "
                       "LiveKit signalling (%r)", self.tid[:8], self.link.peer_id[:12], line)
        self.close('not_signalling', tell_phone=True)

    def _read(self) -> None:
        reason = 'closed'
        while not self._closed.is_set():
            try:
                data = self.sock.recv(MAX_CHUNK)
            except OSError as e:
                reason = f'error: {e}'
                break
            if not data:
                break
            if not self.link.is_connected:
                reason = 'link gone'
                break
            self.link.send(CHANNEL, {'type': TUNNEL_DATA, 'id': self.tid,
                                     'b64': base64.b64encode(data).decode('ascii')})
        self.close(reason, tell_phone=True)

    def _write(self) -> None:
        while not self._closed.is_set():
            try:
                data = self._out.get(timeout=1.0)
            except queue.Empty:
                # The link this tunnel rode is gone: no one is there to read.
                if not self.link.is_connected:
                    self.close('link gone', tell_phone=False)
                    break
                continue
            if data is None:
                break
            try:
                self.sock.sendall(data)
            except OSError as e:
                self.close(f'error: {e}', tell_phone=True)
                break

    def close(self, reason: str, tell_phone: bool) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with _lock:
            if _tunnels.get((self.link.peer_id, self.tid)) is self:
                _tunnels.pop((self.link.peer_id, self.tid), None)
        try:
            self._out.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        if tell_phone and self.link.is_connected:
            self.link.send(CHANNEL, {'type': TUNNEL_CLOSE, 'id': self.tid, 'reason': reason})
        logger.info("LiveKit tunnel %s for %s closed (%s)", self.tid[:8],
                    self.link.peer_id[:12], reason)


def _refused(tid, reason: str) -> dict:
    return {'type': TUNNEL_REFUSED, 'id': tid, 'reason': reason}


def _open(link, tid: str) -> dict:
    key = (link.peer_id, tid)
    with _lock:
        # Opens arrive as requests, each on its own thread: the slot is
        # taken (None until the socket is up) before the connect.
        if key in _tunnels:
            return _refused(tid, 'bad_id')
        if sum(1 for pid, _ in _tunnels if pid == link.peer_id) >= MAX_TUNNELS_PER_LINK:
            return _refused(tid, 'busy')
        _tunnels[key] = None
    tunnel = None
    try:
        address = signal_address()
        if address is None:
            return _refused(tid, 'unavailable')
        try:
            sock = socket.create_connection(address, timeout=_CONNECT_TIMEOUT_S)
            sock.settimeout(None)
        except OSError as e:
            logger.warning("LiveKit tunnel for %s: signal port %s:%d did not answer: %s",
                           link.peer_id[:12], address[0], address[1], e)
            return _refused(tid, 'unreachable')
        tunnel = _Tunnel(link, tid, sock)
    finally:
        with _lock:
            if tunnel is None:
                _tunnels.pop(key, None)
            else:
                _tunnels[key] = tunnel
    tunnel.start()
    logger.info("LiveKit tunnel %s opened for %s", tid[:8], link.peer_id[:12])
    return {'type': TUNNEL_OPENED, 'id': tid}


def _device_link(peer_id: str):
    try:
        from core.peer_link.link_manager import get_link_manager
        link = get_link_manager().get_link(peer_id)
    except Exception as e:
        logger.warning("LiveKit tunnel frame from %s unanswered: link lookup failed: %s",
                       peer_id, e)
        return None
    if link is None or link.kind != 'device' or not link.user_id:
        return None
    return link


def handle_tunnel_frame(channel: str, data, peer_id: str) -> Optional[dict]:
    """The 'tunnel' handler.  A frame from anything but a person's own phone,
    or with no usable id, is not answered."""
    if not isinstance(data, dict):
        return None
    link = _device_link(peer_id)
    if link is None:
        return None
    tid = data.get('id')
    kind = data.get('type')
    if not isinstance(tid, str) or not 0 < len(tid) <= 64:
        return _refused(tid if isinstance(tid, str) else '', 'bad_id') \
            if kind == TUNNEL_OPEN else None
    if kind == TUNNEL_OPEN:
        return _open(link, tid)
    with _lock:
        tunnel = _tunnels.get((link.peer_id, tid))
    if tunnel is None:
        return None
    if kind == TUNNEL_DATA:
        try:
            raw = base64.b64decode(data.get('b64') or '', validate=True)
        except (binascii.Error, ValueError):
            tunnel.close('bad data', tell_phone=True)
            return None
        if len(raw) > MAX_CHUNK:
            tunnel.close('chunk too large', tell_phone=True)
            return None
        tunnel.push(raw)
    elif kind == TUNNEL_CLOSE:
        tunnel.close('phone closed it', tell_phone=False)
    return None


def close_all() -> None:
    """Close every tunnel (shutdown, tests)."""
    with _lock:
        tunnels = [t for t in _tunnels.values() if t is not None]
    for t in tunnels:
        t.close('shutdown', tell_phone=False)


def install() -> bool:
    """Answer a person's own phones' tunnels on the 'tunnel' channel, named
    in this node's handshake (device_requests).  False when PeerLink is not
    available."""
    try:
        from core.peer_link.link_manager import get_link_manager
    except Exception as e:
        logger.warning("LiveKit tunnel not installed: %s", e)
        return False
    get_link_manager().register_channel_handler(CHANNEL, handle_tunnel_frame,
                                                answers=(TUNNEL_OPEN,))
    return True
