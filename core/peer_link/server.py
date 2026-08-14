"""
PeerLink server — the inbound half of core/peer_link/link.py.

`PeerLink.accept()` (link.py) describes itself as "called by link_manager's WS
server".  There was no such server.  Every node could DIAL `/peer_link` — the
dialer builds that URL in `link.py::_resolve_ws_url`, `nat.py` returns it from
all three direct rungs, and docs/architecture/peer_link.md documents it as
`ws://192.168.1.x:6777/peer_link` — and no node could ANSWER, so
`PeerLink.connect()` failed at the TCP layer every time and
`PeerLinkManager._links` stayed empty on every machine in the fleet.

Everything downstream reads that one dict, so everything downstream reported
zero, permanently:

  * skill broadcast     — `PeerLinkManager.broadcast()` iterates `_links`
  * distributed coder   — `claude_hive_session._publish_via_peer_link`
  * shard fan-out       — `PeerLinkManager.collect()` for HiveMind fusion

None of those were separately broken.  They were one missing listener.

Why it lives on the existing server and not a new one
-----------------------------------------------------
Hypercorn is already the production ASGI server (`hart_intelligence_entry.
_serve_app`) and already speaks WebSocket.  The Flask app rides on it through
`AsyncioWSGIMiddleware`, which is WSGI and therefore cannot see a websocket
scope at all — that is the whole reason `/peer_link` 404s today.

So `peer_link_asgi()` wraps that middleware rather than replacing it: websocket
scopes for `/peer_link` are served here, and EVERY other scope is passed through
untouched, so the HTTP surface stays bit-for-bit what it was.  No new port, no
second web framework, no parallel transport — peers already dial the backend
port, and now something is listening on it.

On the Waitress fallback path (pure WSGI, no websocket support) this is simply
not mounted, exactly as before.

Set HEVOLVE_PEER_LINK_SERVER=0 to keep the listener closed on a node that
should dial out but never accept.
"""
import asyncio
import json
import logging
import os
import queue
from typing import Any, Optional

logger = logging.getLogger('hevolve.peer_link')

# The path the dialer asks for. Single source of truth for the server half;
# the client half builds the same string in link.py::_resolve_ws_url.
PEER_LINK_PATH = '/peer_link'

# A socket that connects and then says nothing must not hold a slot.
_HELLO_TIMEOUT = 15.0

# Matches the outbound side's patience (link.py dials with open_timeout=10).
_SEND_TIMEOUT = 30.0

# Pushed into the inbox to wake a blocked reader when the peer goes away.
_CLOSED = object()


class ASGIWebSocketAdapter:
    """Gives an ASGI websocket the blocking interface PeerLink already speaks.

    PeerLink is thread-based: `accept()` verifies and acks inline, then runs
    `_receive_loop` on its own thread, which calls `_ws_send` / `_ws_recv`
    (link.py).  Those expect `.send(data)` and `.recv(timeout=...)` — the shape
    of the `websockets` sync client used for outbound links.  ASGI offers
    coroutines on an event loop instead, so this carries work across the
    boundary: sends are scheduled onto the loop, receives come off a queue the
    server's pump fills.

    It is deliberately the same duck type as the outbound client, so link.py
    needs no branch for which side opened the socket.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, asgi_send,
                 inbox: 'queue.Queue') -> None:
        self._loop = loop
        self._asgi_send = asgi_send
        self._inbox = inbox
        self._closed = False
        self._disconnected = False

    # --- The interface link.py calls ------------------------------------

    def send(self, data: Any) -> None:
        """Blocking send, called from PeerLink's thread."""
        if self._closed:
            raise ConnectionError('PeerLink websocket is closed')
        if isinstance(data, (bytes, bytearray, memoryview)):
            message = {'type': 'websocket.send', 'bytes': bytes(data)}
        else:
            message = {'type': 'websocket.send', 'text': str(data)}
        future = asyncio.run_coroutine_threadsafe(
            self._asgi_send(message), self._loop)
        future.result(timeout=_SEND_TIMEOUT)

    def recv(self, timeout: float = 30.0) -> Optional[bytes]:
        """Blocking receive, called from PeerLink's receive thread."""
        try:
            item = self._inbox.get(timeout=timeout)
        except queue.Empty:
            # Idle, not dead. link.py's _ws_recv turns this into None and
            # _receive_loop keeps waiting — same as a client-side read timeout.
            return None
        if item is _CLOSED:
            raise ConnectionError('PeerLink websocket closed by peer')
        return item

    def close(self) -> None:
        """Close from our side (PeerLink.close -> _ws.close()).

        Fire-and-forget: this can be called from the maintenance thread or,
        via close_link, from a thread that the event loop is waiting on, and
        blocking for the result in the latter case would deadlock.
        """
        self._closed = True
        if self._disconnected:
            # The peer already went away; another ASGI send would be illegal.
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._asgi_send({'type': 'websocket.close'}), self._loop)
        except Exception as exc:  # loop already closing
            logger.debug("PeerLink close send skipped: %s", exc)

    # --- Teardown, driven by the server ---------------------------------

    def mark_disconnected(self) -> None:
        """The peer is gone: no further ASGI message may be sent."""
        self._disconnected = True
        self._closed = True

    def wake_readers(self) -> None:
        """Unblock anyone sitting in recv()."""
        self._inbox.put(_CLOSED)


async def _read_hello(receive) -> Optional[dict]:
    """Read the peer's opening `hello` frame, or None if it never came."""
    try:
        message = await asyncio.wait_for(receive(), _HELLO_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("PeerLink inbound sent no hello within %ss", _HELLO_TIMEOUT)
        return None

    if message.get('type') != 'websocket.receive':
        return None

    raw = message.get('bytes')
    if raw is None:
        text = message.get('text')
        raw = text.encode('utf-8') if text is not None else None
    if raw is None:
        return None

    try:
        hello = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("PeerLink inbound hello was not JSON: %s", exc)
        return None

    if not isinstance(hello, dict) or hello.get('type') != 'hello':
        return None
    return hello


async def _handle_peer_link(scope, receive, send) -> None:
    """Serve one inbound PeerLink connection for its whole lifetime."""
    message = await receive()
    if message.get('type') != 'websocket.connect':
        return
    await send({'type': 'websocket.accept'})

    client = scope.get('client') or ()
    peer_host = f'{client[0]}:{client[1]}' if len(client) >= 2 else ''

    loop = asyncio.get_running_loop()
    inbox: queue.Queue = queue.Queue()
    adapter = ASGIWebSocketAdapter(loop, send, inbox)

    hello = await _read_hello(receive)
    if hello is None:
        await send({'type': 'websocket.close'})
        return

    peer_id = str(hello.get('node_id') or '')
    if not peer_id:
        logger.warning("PeerLink inbound from %s sent no node_id — refused",
                       peer_host or '?')
        await send({'type': 'websocket.close'})
        return

    from .link_manager import get_link_manager
    manager = get_link_manager()

    # accept() blocks: it verifies the signature, sends the ack and starts the
    # receive thread. Run it off the event loop so the pump below can start
    # feeding that thread as soon as it exists.
    link = await loop.run_in_executor(
        None, manager.accept_inbound, peer_id, peer_host, adapter, hello)

    if link is None:
        logger.info("PeerLink inbound REFUSED for %s from %s "
                    "(handshake failed or budget full)",
                    peer_id[:8], peer_host or '?')
        await send({'type': 'websocket.close'})
        return

    logger.info("PeerLink inbound ACCEPTED from %s (%s) trust=%s encrypted=%s",
                peer_id[:8], peer_host or '?', link.trust.value,
                link.is_encrypted)

    try:
        while True:
            message = await receive()
            mtype = message.get('type')
            if mtype == 'websocket.disconnect':
                break
            if mtype != 'websocket.receive':
                continue
            payload = message.get('bytes')
            if payload is None:
                payload = message.get('text')
            if payload is None:
                continue
            inbox.put(payload)
    finally:
        # Order matters. mark_disconnected() first, so the close_link below
        # cannot try to push a `bye` through a socket the peer already dropped
        # (that send would block the executor waiting on this very loop).
        # close_link() next, which clears link._ws — the condition
        # _receive_loop tests, so the thread exits instead of spinning.
        # wake_readers() last, to lift that thread out of its blocking get().
        adapter.mark_disconnected()
        try:
            await loop.run_in_executor(None, manager.close_link, peer_id)
        except Exception as exc:
            logger.debug("PeerLink close_link for %s failed: %s",
                         peer_id[:8], exc)
        adapter.wake_readers()
        logger.info("PeerLink inbound CLOSED for %s (%s)",
                    peer_id[:8], peer_host or '?')


def peer_link_enabled() -> bool:
    """Accept inbound links unless an operator turned the listener off.

    Defaults on, matching the dial-out side: `_try_auto_upgrade` already
    connects outward with no flag, and a node that dials but refuses to answer
    can only ever form links with nodes that happen to dial first.
    """
    return os.environ.get('HEVOLVE_PEER_LINK_SERVER', '1').strip().lower() not in (
        '0', 'false', 'no', 'off')


def peer_link_asgi(next_app):
    """Wrap an ASGI app so `/peer_link` websockets reach PeerLink.

    Every other scope — all HTTP, and any other websocket path — is handed to
    `next_app` unchanged.
    """
    if not peer_link_enabled():
        logger.info("PeerLink inbound server DISABLED "
                    "(HEVOLVE_PEER_LINK_SERVER=0)")
        return next_app

    async def app(scope, receive, send):
        if (scope.get('type') == 'websocket'
                and scope.get('path') == PEER_LINK_PATH):
            await _handle_peer_link(scope, receive, send)
            return
        await next_app(scope, receive, send)

    return app
