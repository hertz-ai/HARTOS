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
it.  Only that first request is sent until LiveKit has answered it: a
WebSocket upgrade (101 with Upgrade: websocket) opens the WebSocket and what
the phone sent after the request follows; any other answer (no Upgrade
header, a bad token -- LiveKit keeps such a connection open for another
request -- or a 101 to another protocol) reaches the phone, its body in full
when it names a Content-Length (LiveKit's do), and the tunnel closes
('not_upgraded'), so no second request is ever sent.  A tunnel whose link is
gone (dropped, pruned idle, replaced by the phone's redial) closes, at the
latest when the phone next opens one or within _WRITER_POLL_S; one whose
LiveKit stops reading closes after _IO_TIMEOUT_S ('livekit_stalled'), and
one whose first request LiveKit leaves unanswered after _ANSWER_TIMEOUT_S
('no_answer').

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
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

from core.auth_local import _is_loopback
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
#: The longest one read or write on LiveKit's socket may block: a write
#: that cannot finish in it means LiveKit stopped reading ('livekit_stalled'),
#: and an idle read wakes this often to see whether the link is still there.
_IO_TIMEOUT_S = 5
#: How long LiveKit has to answer the tunnel's first request ('no_answer').
_ANSWER_TIMEOUT_S = 10
#: How often the writer, idle, looks whether its link is still there.
_WRITER_POLL_S = 1.0
#: The first request on a tunnel: a GET of LiveKit's signal path.
_SIGNAL_REQUEST = re.compile(rb'GET /rtc(?:[/?][^ ]*)? HTTP/1\.[01]\r?')
#: How much of the first request line is read before it must have ended.
_FIRST_LINE_MAX = 8 * 1024
#: How much of the first request, and of LiveKit's answer to it, is read
#: before its headers must have ended.
_HEAD_MAX = 16 * 1024
#: Where an HTTP head ends.
_HEAD_END = re.compile(rb'\r?\n\r?\n')
_STATUS = re.compile(rb'HTTP/1\.[01] (\d{3})')
#: The header that makes a 101 a WebSocket upgrade (and not h2c or another
#: protocol, whose bytes would then pass raw to the loopback port).
_WEBSOCKET_UPGRADE = re.compile(rb'(?im)^upgrade:[ \t]*websocket[ \t]*\r?$')
_CONTENT_LENGTH = re.compile(rb'(?im)^content-length:[ \t]*(\d+)[ \t]*\r?$')

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
    url = get_livekit_url()
    parts = urlsplit(url)
    host = parts.hostname or ''
    if not _is_loopback(host):
        return None
    try:
        port = parts.port or (443 if parts.scheme in ('wss', 'https') else 80)
    except ValueError:
        logger.warning("LiveKit URL %r names no usable port (LIVEKIT_PORT?); "
                       "no tunnel to it", url)
        return None
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
        # The phone's bytes and LiveKit's answer meet here (the link's thread
        # pushes, the reader judges), so what is sent stays in order.
        self._gate = threading.Lock()
        # The phone's bytes until its first request's head is whole and that
        # request is LiveKit signalling (None once it was sent).
        self._head: Optional[bytes] = b''
        # The phone's bytes after that request, held until LiveKit upgrades
        # (None once it has: they were sent, and the rest passes through).
        self._held: Optional[bytes] = b''
        # LiveKit's bytes until the head of its answer is whole (None once
        # judged), and a refusal's body still to reach the phone.
        self._answer: Optional[bytes] = b''
        self._body_left = 0
        # When the first request went to LiveKit (monotonic), for its answer's
        # deadline; 0 until it has.
        self._asked_at = 0.0

    def start(self) -> None:
        threading.Thread(target=self._read, daemon=True,
                         name=f'livekit-tunnel-r-{self.tid[:8]}').start()
        threading.Thread(target=self._write, daemon=True,
                         name=f'livekit-tunnel-w-{self.tid[:8]}').start()

    def push(self, data: bytes) -> None:
        """The phone's bytes: its first request once its head is whole and it
        is LiveKit signalling, then nothing more until LiveKit upgrades."""
        fault = None
        with self._gate:
            if self._head is not None:
                self._head += data
                data = b''
                if b'\n' not in self._head:
                    if len(self._head) > _FIRST_LINE_MAX:
                        fault = 'not_signalling'
                elif not _SIGNAL_REQUEST.fullmatch(self._head.split(b'\n', 1)[0]):
                    fault = 'not_signalling'
                else:
                    end = _HEAD_END.search(self._head)
                    if end is not None:
                        data, self._held = self._head[:end.end()], self._head[end.end():]
                        self._head = None
                        self._asked_at = time.monotonic()
                    elif len(self._head) > _HEAD_MAX:
                        fault = 'not_signalling'
            elif self._held is not None:
                self._held += data
                data = b''
                if len(self._held) > MAX_CHUNK:
                    fault = 'overrun'
            if data:
                fault = self._send_to_livekit(data)
        if fault == 'not_signalling':
            self._not_signalling()
        elif fault:
            self.close(fault, tell_phone=True)

    def _send_to_livekit(self, data: bytes) -> Optional[str]:
        """Queue bytes for the writer; 'overrun' when it is too far behind."""
        try:
            self._out.put_nowait(data)
        except queue.Full:
            logger.warning("LiveKit tunnel %s: %d chunks unwritten; closing it",
                           self.tid[:8], _WRITE_QUEUE)
            return 'overrun'
        return None

    def _judge(self, data: bytes) -> Optional[str]:
        """LiveKit's bytes, as they reach the phone: why to close now, if so.
        A WebSocket upgrade (101, Upgrade: websocket) sends the phone's held
        bytes on and lets the rest through; any other answer closes the
        tunnel once it reached the phone -- its body in full when it names a
        Content-Length, else once its head has."""
        with self._gate:
            if self._answer is None:
                if self._held is None:
                    return None               # upgraded: the WebSocket
                self._body_left -= len(data)
                return 'not_upgraded' if self._body_left <= 0 else None
            self._answer += data
            end = _HEAD_END.search(self._answer)
            if end is None:
                return 'not_upgraded' if len(self._answer) > _HEAD_MAX else None
            head, rest = self._answer[:end.start()], self._answer[end.end():]
            self._answer = None
            status = _STATUS.match(head)
            if status and status.group(1) == b'101' and _WEBSOCKET_UPGRADE.search(head):
                held, self._held = self._held, None
                return self._send_to_livekit(held) if held else None
            length = _CONTENT_LENGTH.search(head)
            self._body_left = (int(length.group(1)) if length else 0) - len(rest)
            logger.info("LiveKit answered tunnel %s's first request %s, not an "
                        "upgrade; closing it once the phone has the answer",
                        self.tid[:8], status.group(1).decode() if status else 'unreadably')
            return 'not_upgraded' if self._body_left <= 0 else None

    def _not_signalling(self) -> None:
        line = (self._head or b'').split(b'\n', 1)[0][:80]
        logger.warning("LiveKit tunnel %s from %s refused: its first request is not "
                       "LiveKit signalling (%r)", self.tid[:8], self.link.peer_id[:12], line)
        self.close('not_signalling', tell_phone=True)

    def _overdue(self) -> bool:
        """LiveKit has not answered the first request in _ANSWER_TIMEOUT_S."""
        with self._gate:
            waiting = self._answer is not None and self._asked_at
        return bool(waiting) and time.monotonic() - self._asked_at > _ANSWER_TIMEOUT_S

    def _read(self) -> None:
        reason = 'closed'
        while not self._closed.is_set():
            try:
                data = self.sock.recv(MAX_CHUNK)
            except TimeoutError:
                # Idle (the socket's I/O timeout): still wanted?
                if not self.link.is_connected:
                    reason = 'link gone'
                    break
                if self._overdue():
                    reason = 'no_answer'
                    break
                continue
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
            judged = self._judge(data)
            if judged:
                reason = judged
                break
        self.close(reason, tell_phone=True)

    def _write(self) -> None:
        while not self._closed.is_set():
            try:
                data = self._out.get(timeout=_WRITER_POLL_S)
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
            except TimeoutError:
                # LiveKit took nothing for the socket's whole I/O timeout.
                logger.warning("LiveKit tunnel %s: LiveKit stopped reading; closing it",
                               self.tid[:8])
                self.close('livekit_stalled', tell_phone=True)
                break
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
    # A phone that redialled holds this peer id on a new link: the old link's
    # tunnels are no one's now, so they end here rather than hold its slots
    # (and its tunnel ids) until their own threads notice.
    with _lock:
        stale = [t for (pid, _), t in _tunnels.items()
                 if pid == link.peer_id and t is not None
                 and (t.link is not link or not t.link.is_connected)]
    for t in stale:
        t.close('link replaced', tell_phone=False)
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
            sock.settimeout(_IO_TIMEOUT_S)
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
        return get_link_manager().get_device_link(peer_id)
    except Exception as e:
        logger.warning("LiveKit tunnel frame from %s unanswered: link lookup failed: %s",
                       peer_id, e)
        return None


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
    in this node's handshake (device_requests).  False, offering nothing,
    when LiveKit is not served on this node's loopback (signal_address: a
    managed SFU elsewhere, an unreadable port) -- a phone would only have
    every open refused -- or when PeerLink is not available."""
    if signal_address() is None:
        logger.info("LiveKit tunnel not offered: LiveKit is not served on this "
                    "node's loopback")
        return False
    try:
        from core.peer_link.link_manager import get_link_manager
    except Exception as e:
        logger.warning("LiveKit tunnel not installed: %s", e)
        return False
    get_link_manager().register_channel_handler(CHANNEL, handle_tunnel_frame,
                                                answers=(TUNNEL_OPEN,))
    return True
