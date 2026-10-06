"""
PeerLink relay -- the meeting point every PeerLink endpoint can reach from
behind any NAT.

nat.py's ladder (LAN direct, STUN direct WAN, WireGuard, relay) promises "two
peers connected regardless of network topology", and a phone's own HELLO
already proves who it is.  What was missing was any rung a NAT'd desktop can
answer on: link_manager said it plainly ("PeerLink has no WAMP relay mode"),
nat.py's relay rung was a placeholder, and a phone off the home Wi-Fi never
reached its desktop at all.

Both ends dial OUT to the same WAMP router (central's, the router the phone
already holds a session on), and an outbound connection crosses any NAT.  Each
endpoint subscribes to one inbox topic, ``RELAY_TOPIC_PREFIX.<endpoint id>``
(a node's id is its node_id); a link is one conversation id inside it.  The
socket below has the duck type server.ASGIWebSocketAdapter gives a link
(send / recv(timeout) / close), so the handshake, device admission, channels
and request/reply in link.py run unchanged, and an inbound relay HELLO goes
through link_manager.accept_inbound exactly like a websocket one.

Anyone may join the router's realm and read a topic, so a relay socket carries
``requires_e2e``: link.py derives the X25519/AES-256-GCM session key for it
whatever its trust, refuses the link without one, and every frame after the
HELLO / HELLO_ACK pair is ciphertext.  The router sees who talks to whom and
how much, never what.

The relay is the rendezvous, not the destination: once a link is live the
ends can move to a direct path (nat.py's direct rungs); until then, and when
no direct path forms, the relay carries the frames.

HEVOLVE_PEER_LINK_RELAY=0 keeps this node off the relay (no outbound session);
HEVOLVE_PEER_LINK_RELAY_URL points it at another router (a regional host).
"""
import asyncio
import base64
import binascii
import json
import logging
import os
import queue
import threading
import uuid
from typing import Callable, Dict, Optional

logger = logging.getLogger('hevolve.peer_link')

#: Every endpoint's inbox is this prefix plus its id.
RELAY_TOPIC_PREFIX = 'com.hertzai.hevolve.peerlink.relay'

#: A PeerLink address that names an endpoint on the relay, not a host.
RELAY_ADDRESS_SCHEME = 'relay://'

#: The largest frame a relay socket carries (a frame is one PeerLink message;
#: measured 2026-10-06: central's router delivered 4 MB publishes in 1-2.5 s).
MAX_FRAME_BYTES = 8 * 1024 * 1024

#: Conversations an endpoint holds at once, so a stream of HELLOs on a public
#: topic cannot grow it without bound.  A HELLO past this is dropped.
MAX_CONVERSATIONS = 64

#: Frames queued for one link before the oldest are refused.
_INBOX_FRAMES = 1024

_CLOSED = object()


def relay_topic(endpoint_id: str) -> str:
    """The inbox topic of an endpoint (a node's node_id, a phone's key id)."""
    return f'{RELAY_TOPIC_PREFIX}.{endpoint_id}'


def relay_enabled() -> bool:
    """On unless an operator closed it, like the inbound server's switch."""
    return os.environ.get('HEVOLVE_PEER_LINK_RELAY', '1').strip().lower() not in (
        '0', 'false', 'no', 'off')


def relay_router_urls() -> list:
    """The router both ends meet on, in the order to try: central's TLS
    endpoint first (the one the phone joins), its plaintext port after, both
    composed in core.wamp_url.  HEVOLVE_PEER_LINK_RELAY_URL names the only
    one to use instead.  Not WAMP_URL/CBURL: a local-only node points those at
    its own embedded router, which no phone off its Wi-Fi can reach.

    TLS first because the HELLO / HELLO_ACK pair travels before the session
    key exists: over plaintext, anyone on the path between the router and
    this node reads a phone's device-token claims and a SAME_USER proof.
    Frames after the handshake are sealed either way."""
    explicit = os.environ.get('HEVOLVE_PEER_LINK_RELAY_URL', '').strip()
    if explicit:
        return [explicit]
    from core.wamp_url import DEFAULT_ROUTER_URL, DEFAULT_SECURE_ROUTER_URL
    return [DEFAULT_SECURE_ROUTER_URL, DEFAULT_ROUTER_URL]


class RelaySocket:
    """One link's conversation on the relay, as the blocking socket link.py
    speaks (send / recv(timeout) / close)."""

    #: link.py reads this: a relay link is end-to-end encrypted or not at all.
    requires_e2e = True

    def __init__(self, hub: 'RelayHub', conversation: str, peer_topic: str):
        self._hub = hub
        self.conversation = conversation
        self.peer_topic = peer_topic
        self._inbox: 'queue.Queue' = queue.Queue(maxsize=_INBOX_FRAMES)
        self._closed = False

    @property
    def address(self) -> str:
        # host:port shaped, so a caller that rate-limits by the part before
        # the last ':' groups every relay ask under one 'relay' host.
        return f'relay:{self.conversation}'

    def send(self, data) -> None:
        if self._closed:
            raise ConnectionError('relay conversation is closed')
        raw = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) \
            else str(data).encode('utf-8')
        if len(raw) > MAX_FRAME_BYTES:
            raise ValueError(f'relay frame of {len(raw)} bytes exceeds {MAX_FRAME_BYTES}')
        self._hub.publish(self.peer_topic, {
            'c': self.conversation,
            'r': self._hub.inbox_topic,
            'b': base64.b64encode(raw).decode('ascii'),
        })

    def recv(self, timeout: float = 30.0):
        try:
            item = self._inbox.get(timeout=timeout)
        except queue.Empty:
            return None          # idle, not dead -- link.py keeps waiting
        if item is _CLOSED:
            raise ConnectionError('relay conversation closed by peer')
        return item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._hub.publish(self.peer_topic, {
                'c': self.conversation, 'r': self._hub.inbox_topic, 'x': 1})
        except Exception as e:
            logger.debug("relay close not sent for %s: %s", self.conversation[:8], e)
        self._hub._forget(self.conversation)
        self._inbox.put(_CLOSED)

    # --- driven by the hub ---------------------------------------------------

    def _deliver(self, raw: bytes) -> None:
        try:
            self._inbox.put_nowait(raw)
        except queue.Full:
            logger.warning("relay conversation %s: %d frames unread, dropping one",
                           self.conversation[:8], _INBOX_FRAMES)

    def _remote_closed(self) -> None:
        self._closed = True
        self._hub._forget(self.conversation)
        self._inbox.put(_CLOSED)


class WampRelayTransport:
    """The hub's one session on the router: subscribes to the inbox, publishes
    to other inboxes, and rejoins with backoff when the router drops it (the
    same shape as core.platform.events.EventBus.connect_wamp)."""

    def __init__(self, urls, realm: str = 'realm1'):
        # Tried in order on each join (autobahn moves to the next transport
        # when one cannot connect).
        self.urls = [urls] if isinstance(urls, str) else list(urls)
        self.realm = realm
        self._session = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = False
        self.joined = threading.Event()

    def start(self, topic: str, on_message: Callable[[dict], None]) -> bool:
        try:
            from autobahn.asyncio.component import Component
        except ImportError:
            logger.warning("PeerLink relay unavailable: autobahn is not installed")
            return False

        transport = self

        def _run():
            import time as _time
            backoff = 1
            while not transport._stop:
                component = Component(transports=[{'url': url, 'max_retries': 0}
                                                  for url in transport.urls],
                                      realm=transport.realm)

                @component.on_join
                async def _joined(session, details):
                    def _handler(envelope=None, *args, **kwargs):
                        try:
                            on_message(envelope)
                        except Exception as e:
                            logger.debug("relay frame not handled: %s", e)
                    await session.subscribe(_handler, topic)
                    transport._session = session
                    transport.joined.set()
                    logger.info("PeerLink relay joined %s as %s",
                                getattr(getattr(details, 'transport', None), 'url', '')
                                or transport.urls, topic)

                @component.on_leave
                async def _left(session, details):
                    transport._session = None
                    transport.joined.clear()
                    logger.info("PeerLink relay left the router")

                loop = asyncio.new_event_loop()
                transport._loop = loop
                asyncio.set_event_loop(loop)
                started = _time.monotonic()
                try:
                    loop.run_until_complete(component.start(loop=loop))
                except Exception as e:
                    logger.warning("PeerLink relay session ended (%s); rejoining in %ds",
                                   e, backoff)
                finally:
                    transport._session = None
                    transport.joined.clear()
                    transport._loop = None
                    try:
                        loop.close()
                    except Exception:
                        pass
                if transport._stop:
                    break
                # a session that lived a while resets the backoff
                backoff = 1 if _time.monotonic() - started > 60 else min(backoff * 2, 60)
                _time.sleep(backoff)

        self._thread = threading.Thread(target=_run, daemon=True, name='peerlink-relay')
        self._thread.start()
        return True

    def publish(self, topic: str, envelope: dict) -> None:
        session, loop = self._session, self._loop
        if session is None or loop is None:
            raise ConnectionError('PeerLink relay is not joined')
        loop.call_soon_threadsafe(session.publish, topic, envelope)

    def stop(self) -> None:
        self._stop = True
        session, loop = self._session, self._loop
        if session is not None and loop is not None:
            try:
                loop.call_soon_threadsafe(session.leave)
            except Exception:
                pass


class RelayHub:
    """One endpoint on the relay: its inbox, its conversations, and the door
    an inbound HELLO walks through."""

    def __init__(self, endpoint_id: str, transport):
        if not endpoint_id:
            raise ValueError('a relay endpoint needs an id')
        self.endpoint_id = endpoint_id
        self.inbox_topic = relay_topic(endpoint_id)
        self._transport = transport
        self._sockets: Dict[str, RelaySocket] = {}
        self._lock = threading.Lock()
        self._accept: Optional[Callable[[RelaySocket, dict], object]] = None

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> bool:
        return self._transport.start(self.inbox_topic, self._on_message)

    def stop(self) -> None:
        with self._lock:
            sockets = list(self._sockets.values())
        for sock in sockets:
            sock.close()
        self._transport.stop()

    @property
    def joined(self) -> bool:
        return self._transport.joined.is_set()

    def wait_joined(self, timeout: float) -> bool:
        return self._transport.joined.wait(timeout)

    def on_inbound(self, accept: Callable[[RelaySocket, dict], object]) -> None:
        """``accept(sock, hello)`` adopts an inbound link and returns it, or a
        falsy value to refuse (the conversation is then closed)."""
        self._accept = accept

    # --- conversations -------------------------------------------------------

    def publish(self, topic: str, envelope: dict) -> None:
        self._transport.publish(topic, envelope)

    def dial(self, endpoint_id: str) -> RelaySocket:
        """Open a conversation with another endpoint's inbox."""
        if not endpoint_id or endpoint_id == self.endpoint_id:
            raise ValueError('a relay dial needs another endpoint')
        sock = RelaySocket(self, uuid.uuid4().hex, relay_topic(endpoint_id))
        with self._lock:
            self._sockets[sock.conversation] = sock
        return sock

    def _forget(self, conversation: str) -> None:
        with self._lock:
            self._sockets.pop(conversation, None)

    def conversation_count(self) -> int:
        with self._lock:
            return len(self._sockets)

    def _on_message(self, envelope) -> None:
        """A frame on our inbox.  Runs on the transport's thread: never blocks."""
        if not isinstance(envelope, dict):
            return
        conversation = envelope.get('c')
        if not isinstance(conversation, str) or not 0 < len(conversation) <= 64:
            return
        with self._lock:
            sock = self._sockets.get(conversation)
        if envelope.get('x'):
            if sock is not None:
                sock._remote_closed()
            return
        payload = envelope.get('b')
        if not isinstance(payload, str) or len(payload) > (MAX_FRAME_BYTES * 4) // 3 + 4:
            return
        try:
            raw = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            return
        if sock is not None:
            sock._deliver(raw)
            return
        self._open_inbound(conversation, envelope.get('r'), raw)

    def _open_inbound(self, conversation: str, reply_topic, raw: bytes) -> None:
        """Only a HELLO opens a conversation, from an inbox that is not ours."""
        if self._accept is None:
            return
        if (not isinstance(reply_topic, str)
                or not reply_topic.startswith(RELAY_TOPIC_PREFIX + '.')
                or reply_topic == self.inbox_topic):
            return
        try:
            hello = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(hello, dict) or hello.get('type') != 'hello':
            return
        with self._lock:
            if len(self._sockets) >= MAX_CONVERSATIONS:
                logger.warning("PeerLink relay: %d conversations open, HELLO dropped",
                               MAX_CONVERSATIONS)
                return
            sock = RelaySocket(self, conversation, reply_topic)
            self._sockets[conversation] = sock
        threading.Thread(target=self._run_accept, args=(sock, hello),
                         daemon=True, name='peerlink-relay-accept').start()

    def _run_accept(self, sock: RelaySocket, hello: dict) -> None:
        try:
            link = self._accept(sock, hello)
        except Exception as e:
            logger.warning("PeerLink relay HELLO not accepted: %s", e)
            link = None
        if not link:
            sock.close()


_hub: Optional[RelayHub] = None
_hub_lock = threading.Lock()


def get_relay_hub() -> Optional[RelayHub]:
    """This node's hub, or None when it is not on the relay."""
    return _hub


def start_relay_hub(endpoint_id: str = '', transport=None) -> Optional[RelayHub]:
    """Put this node on the relay: its inbox is its node_id, and every inbound
    HELLO goes through link_manager.accept_inbound like a websocket one.
    Idempotent; None when the relay is switched off or has no session."""
    global _hub
    if not relay_enabled():
        logger.info("PeerLink relay DISABLED (HEVOLVE_PEER_LINK_RELAY=0)")
        return None
    with _hub_lock:
        if _hub is not None:
            return _hub
        if not endpoint_id:
            from security.node_integrity import get_node_identity
            endpoint_id = get_node_identity().get('node_id', '')
        hub = RelayHub(endpoint_id, transport or WampRelayTransport(relay_router_urls()))

        def _accept(sock: RelaySocket, hello: dict):
            peer_id = str(hello.get('node_id') or '')
            if not peer_id:
                return None
            from .link_manager import get_link_manager
            return get_link_manager().accept_inbound(peer_id, sock.address, sock, hello)

        hub.on_inbound(_accept)
        if not hub.start():
            return None
        _hub = hub
    return hub


def stop_relay_hub() -> None:
    global _hub
    with _hub_lock:
        hub, _hub = _hub, None
    if hub is not None:
        hub.stop()
