"""
MessageBus — unified publish/subscribe across all transports.

Every publish routes to ALL available transports simultaneously:
  1. LOCAL EventBus — always available, same-process delivery
  2. SSE          — same-machine cross-process (Flask → frontend), always available
  3. PEERLINK     — encrypted direct links to peers (when connected)
  4. CROSSBAR     — central telemetry + legacy mobile push (when internet available)

Works at every level:
  Single device offline     → LOCAL + SSE
  Multi-device LAN          → LOCAL + SSE + PEERLINK (plain, same-user)
  Multi-device WAN          → LOCAL + SSE + PEERLINK (encrypted) + CROSSBAR
  Full hive                 → LOCAL + SSE + PEERLINK (encrypted) + CROSSBAR

The SSE leg fan-outs to the same-machine frontend via the Flask SSE
broker (see ``core.platform.events.broadcast_sse_safe``). It is the
only delivery path that does NOT depend on Crossbar / WAMP being up
— so any chat upgrade, dashboard invalidate, or capability event
remains visible to the local UI even when the WAMP router refuses
connection. Without the SSE leg every caller would have to bolt on
``broadcast_sse_safe`` by hand and inevitably forget it (the
``_deliver_expert_to_user_async`` regression is exactly that
shape — expert reply published to WAMP only, lost when Crossbar
was down).

Dedup: message_id LRU set prevents double delivery when message
arrives via multiple transports.

Topic mapping:
  New topics use dot-notation: 'chat.response', 'task.progress'
  Legacy Crossbar topics: 'com.hertzai.hevolve.chat.{user_id}'
  Mapping is bidirectional for backward compatibility.
"""
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger('hevolve.peer_link')


# Legacy topic mapping: new → old Crossbar topic template
# {user_id} is substituted at publish time from data dict
TOPIC_MAP = {
    # Per-user chat topics (frontend crossbarWorker.js subscribes to these)
    'chat.response': 'com.hertzai.hevolve.chat.{user_id}',
    'chat.action': 'com.hertzai.hevolve.action.{user_id}',
    'chat.general': 'com.hertzai.hevolve.{user_id}',
    'chat.analogy': 'com.hertzai.hevolve.analogy.{user_id}',
    'chat.social': 'com.hertzai.hevolve.social.{user_id}',
    'chat.pupit': 'com.hertzai.pupit.{user_id}',
    # Book parsing (percentage progress → frontend progress bar)
    'book.parsing': 'com.hertzai.bookparsing.{user_id}',
    # Task lifecycle (server-side tracking)
    'task.progress': 'com.hertzai.longrunning.log',
    'task.confirmation': 'com.hertzai.hevolve.confirmation',
    'task.exception': 'com.hertzai.hevolve.exception',
    'task.timeout': 'com.hertzai.hevolve.timeout',
    'task.intermediate': 'com.hertzai.hevolve.intermediate',
    'task.error': 'com.hertzai.hevolve.error',
    'task.actions': 'com.hertzai.hevolve.actions',
    'task.probe': 'com.hertzai.hevolve.probe',
    # Mobile / push
    'mobile.push': 'com.hertzai.hevolve.pupitpublish',
    # Agent coordination
    'agent.multichat': 'com.hertzai.hevolve.agent.multichat',
    # Game sessions
    'game.session': 'com.hertzai.hevolve.game.{session_id}',
    # Community
    'community.message': 'com.hertzai.hevolve.community.{community_id}',
    'community.feed': 'com.hertzai.community.feed',
    # Fleet commands (RN subscribes for TTS, agent consent, game dispatch)
    'fleet.command': 'com.hertzai.hevolve.fleet.{device_id}',
    # Fleet commands targeting all devices for a user (fan-out when device_id unknown)
    'fleet.command.user': 'com.hertzai.hevolve.fleet.user.{user_id}',
    # Mock interview (RN only)
    'mock_interview': 'com.hertzai.mock_interview.{user_id}',
    # Telemetry (node → central only, metadata, never content)
    'telemetry.node': 'com.hartos.telemetry.{node_id}',
    # Compute routing status (client shows real-time routing info)
    'compute.routing': 'com.hertzai.hevolve.compute.routing.{user_id}',
    # Compute relay — phone→HARTOS request + HARTOS→phone response (NAT traversal)
    'compute.request': 'com.hertzai.hevolve.compute.request.{user_id}',
    'compute.response': 'com.hertzai.hevolve.compute.response.{user_id}',
    # Remote desktop
    'remote_desktop.signal': 'com.hartos.remote_desktop.signal.{device_id}',
}

# Reverse lookup: legacy topic prefix → new topic
# Sorted by prefix length (longest first) so 'com.hertzai.hevolve.chat'
# matches before the shorter 'com.hertzai.hevolve' (chat.general).
_REVERSE_MAP_UNSORTED = {}
for new_topic, legacy_template in TOPIC_MAP.items():
    prefix = legacy_template.split('.{')[0] if '.{' in legacy_template else legacy_template
    _REVERSE_MAP_UNSORTED[prefix] = new_topic
_REVERSE_MAP = dict(sorted(_REVERSE_MAP_UNSORTED.items(), key=lambda x: -len(x[0])))


def resolve_legacy_topic(legacy_topic: str):
    """Map a legacy Crossbar topic to a MessageBus topic + extract suffix.

    This is the SINGLE source of truth for legacy→bus topic resolution.
    Consumers: hart_intelligence.publish_async(), receive_from_crossbar().

    Returns:
        (bus_topic, suffix) where suffix is typically user_id.
        (None, '') if no mapping found.
    """
    for prefix, bus_topic in _REVERSE_MAP.items():
        if legacy_topic == prefix:
            return bus_topic, ''
        if legacy_topic.startswith(prefix + '.'):
            suffix = legacy_topic[len(prefix) + 1:]
            return bus_topic, suffix
    return None, ''


def chat_topic_for(user_id: str) -> str:
    """Return the legacy WAMP chat topic for a given user.

    Single source of truth for the per-user chat-bubble topic.
    Replaces the inline ``f'com.hertzai.hevolve.chat.{user_id}'``
    pattern that was duplicated at every publish call site.

    Output is byte-identical to the inline f-string callers were
    producing — every subscriber (Android RN, Web SPA, Nunba
    adapter) sees zero wire change.  This is purely a refactor
    seam so the legacy topic name lives in one place the day we
    eventually retire it.
    """
    return f'com.hertzai.hevolve.chat.{user_id}'


class _LRUDedup:
    """LRU set for message deduplication. O(1) check and insert."""

    def __init__(self, maxsize: int = 10000):
        self._cache: OrderedDict = OrderedDict()
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def check_and_add(self, msg_id: str) -> bool:
        """Returns True if msg_id is new (not a duplicate)."""
        with self._lock:
            if msg_id in self._cache:
                return False  # Duplicate
            self._cache[msg_id] = True
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)
            return True  # New


class MessageBus:
    """Unified pub/sub across LOCAL + PEERLINK + CROSSBAR.

    Usage:
        bus = get_message_bus()
        bus.subscribe('chat.response', handler)
        bus.publish('chat.response', {'user_id': '123', 'text': 'Hello'})
    """

    def __init__(self):
        self._subscriptions: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()
        self._dedup = _LRUDedup(maxsize=10000)
        self._http_transport: Optional[Callable] = None  # injected Crossbar HTTP fallback
        self._stats = {
            'published': 0,
            'delivered_local': 0,
            'delivered_sse': 0,
            'delivered_peerlink': 0,
            'delivered_crossbar': 0,
            'deduplicated': 0,
        }

    def set_http_transport(self, transport_fn: Callable) -> None:
        """Inject HTTP Crossbar transport (avoids layering violation).

        Called by hart_intelligence at startup to provide the HTTP publish
        fallback without MessageBus importing from hart_intelligence.

        Args:
            transport_fn: callable(topic: str, payload: str) -> None
        """
        self._http_transport = transport_fn

    def publish(self, topic: str, data: dict = None,
                user_id: str = '', device_id: str = '',
                skip_crossbar: bool = False,
                skip_peerlink: bool = False,
                skip_sse: bool = False) -> str:
        """Publish a message to all available transports.

        Fan-out order (each leg is independent — failure in one never
        blocks the others):

          1. LOCAL    — same-process subscribers + EventBus       (always)
          2. SSE      — same-machine Flask → frontend, no network (always)
          3. PEERLINK — P2P device-to-device (BLE / local Wi-Fi)  (best-effort)
          4. CROSSBAR — WAMP / cross-network                       (best-effort)

        SSE is treated like LOCAL trust-wise (same-machine, loopback
        only, MCP-token gated) so payloads pass through unredacted.
        Outbound legs (PEERLINK + CROSSBAR) get the DLP scrub.

        SUBTLE — Nunba's adapter does NOT see direct ``bus.publish``
        calls.  Nunba's ``routes/hartos_backend_adapter.py``
        monkey-patches ``hart_intelligence.publish_async``, not this
        method.  Callers that go directly through the bus
        (``bus.publish(...)``) bypass that monkey-patch — Nunba's
        per-request thinking-trace buffer never sees those messages.
        For chat-bubble publishes, prefer
        ``hart_intelligence.publish_async(chat_topic_for(user_id),
        json.dumps(payload))`` so the interceptor still fires.
        Migration to a bus subscriber on ``chat.response`` is the
        right long-term shape (tracked in
        ``memory/project_publish_aop_migration.md``).

        Args:
            topic: Dot-notation topic (e.g., 'chat.response')
            data: Message payload (JSON-serializable dict)
            user_id: For per-user topic routing (substituted into legacy topics)
            device_id: For per-device topic routing
            skip_crossbar: Don't publish to Crossbar (for local-only events)
            skip_peerlink: Don't publish to PeerLink (for same-process events)
            skip_sse: Don't publish to the same-machine SSE broker
                (for events that should NEVER reach the local UI, e.g.
                pure server-to-server telemetry).  Default False so
                every existing caller automatically gains the SSE leg.

        Returns:
            Message ID (for dedup/tracking)
        """
        data = data or {}
        msg_id = uuid.uuid4().hex[:16]

        # Add metadata
        envelope = {
            'msg_id': msg_id,
            'topic': topic,
            'data': data,
            'timestamp': time.time(),
        }
        if user_id:
            envelope['user_id'] = user_id
            data.setdefault('user_id', user_id)

        self._stats['published'] += 1

        # 1. LOCAL — always deliver (unredacted, same process)
        self._route_local(topic, data, msg_id)

        # 2. SSE — same-machine cross-process, unredacted (same trust as LOCAL —
        #    loopback only, MCP-token gated, same user).  Always attempted; the
        #    helper no-ops gracefully if the SSE broker hasn't been set up yet
        #    (e.g. tests, or pre-Flask boot).  This is the leg that keeps the
        #    local UI working when Crossbar / WAMP is down.
        if not skip_sse:
            self._route_sse(topic, data, user_id, msg_id)

        # Redact secrets before outbound transmission (PeerLink + Crossbar)
        outbound_data = data
        if not skip_peerlink or not skip_crossbar:
            try:
                from security.dlp_engine import redact_pii
                import json
                raw = json.dumps(data)
                redacted = redact_pii(raw)
                if redacted != raw:
                    outbound_data = json.loads(redacted)
            except (ImportError, Exception):
                pass  # DLP not available — proceed unredacted

        # 3. PEERLINK — if connected peers exist
        if not skip_peerlink:
            self._route_peerlink(topic, outbound_data, msg_id)

        # 4. CROSSBAR — if internet available (and not skipped)
        if not skip_crossbar:
            self._route_crossbar(topic, outbound_data, user_id, device_id, msg_id)

        return msg_id

    def subscribe(self, topic: str, handler: Callable) -> None:
        """Subscribe to a topic.

        Handler signature: handler(topic: str, data: dict)
        Supports wildcard: 'chat.*' matches 'chat.response', 'chat.action'
        """
        with self._lock:
            if topic not in self._subscriptions:
                self._subscriptions[topic] = []
            self._subscriptions[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Callable) -> None:
        with self._lock:
            handlers = self._subscriptions.get(topic, [])
            if handler in handlers:
                handlers.remove(handler)

    def receive_from_peer(self, envelope: dict) -> bool:
        """Handle message received via PeerLink.

        Deduplicates and delivers to local subscribers.
        Called by ChannelDispatcher when 'events' channel message arrives.
        """
        msg_id = envelope.get('msg_id', '')
        if not msg_id:
            return False

        if not self._dedup.check_and_add(msg_id):
            self._stats['deduplicated'] += 1
            return False  # Already delivered via another transport

        topic = envelope.get('topic', '')
        data = envelope.get('data', {})

        self._deliver_to_subscribers(topic, data)
        return True

    def receive_from_crossbar(self, legacy_topic: str, message: Any) -> bool:
        """Handle message received via Crossbar (legacy path).

        Maps legacy topic to new topic and delivers.
        """
        # Find matching new topic
        new_topic = None
        for prefix, topic in _REVERSE_MAP.items():
            if legacy_topic.startswith(prefix):
                new_topic = topic
                break

        if not new_topic:
            new_topic = legacy_topic  # Pass through unknown topics

        data = message if isinstance(message, dict) else {'raw': str(message)}

        msg_id = data.get('msg_id', '') or uuid.uuid4().hex[:16]
        if not self._dedup.check_and_add(msg_id):
            self._stats['deduplicated'] += 1
            return False

        self._deliver_to_subscribers(new_topic, data)
        return True

    def get_stats(self) -> dict:
        return dict(self._stats)

    # ─── Internal routing ────────────────────────────────

    def _route_local(self, topic: str, data: dict, msg_id: str):
        """Deliver to local EventBus + direct subscribers."""
        # Mark as seen for dedup
        self._dedup.check_and_add(msg_id)

        # Direct subscribers
        self._deliver_to_subscribers(topic, data)

        # Also emit to EventBus (for cross-subsystem communication)
        try:
            from core.platform.events import emit_event
            emit_event(f'bus.{topic}', data)
        except Exception:
            pass

        self._stats['delivered_local'] += 1

    def _route_sse(self, topic: str, data: dict, user_id: str, msg_id: str):
        """Push to the same-machine SSE broker (Flask → frontend).

        Uses ``core.platform.events.broadcast_sse_safe`` which encapsulates
        the ``import __main__`` + ``sys.modules['main_module']`` fallback
        chain (Nunba's SSE registry lives on ``__main__``).  The helper
        returns ``False`` if the SSE broker hasn't been wired up yet —
        we increment the stat only on confirmed delivery so test runs
        and pre-Flask boot don't inflate the counter.

        Event type is the bus topic itself (1:1 mapping).  Frontends
        listening for legacy event names (``'notification'``,
        ``'message'``, ``'capability_update'``, etc.) will keep working
        because those callers continue to invoke ``broadcast_sse_safe``
        directly with their legacy event_type — this bus leg is purely
        additive for callers that didn't have an SSE leg before.

        Best-effort — never raises.  An SSE failure must not block the
        outbound legs (PEERLINK / CROSSBAR) or the LOCAL deliveries that
        already happened.
        """
        try:
            from core.platform.events import broadcast_sse_safe
        except Exception:
            return  # core.platform.events not importable — fail-closed silent
        try:
            delivered = broadcast_sse_safe(
                topic, data, user_id=(user_id or None))
        except Exception as e:
            logger.debug(f"SSE route failed for {topic}: {e}")
            return
        if delivered:
            self._stats['delivered_sse'] += 1

    def _route_peerlink(self, topic: str, data: dict, msg_id: str):
        """Send to connected peers via PeerLink 'events' channel."""
        try:
            from core.peer_link.link_manager import get_link_manager
            mgr = get_link_manager()

            envelope = {
                'msg_id': msg_id,
                'topic': topic,
                'data': data,
            }

            sent = mgr.broadcast('events', envelope)
            if sent > 0:
                self._stats['delivered_peerlink'] += sent
        except Exception:
            pass  # No PeerLink available — that's fine

    def _route_crossbar(self, topic: str, data: dict,
                        user_id: str, device_id: str, msg_id: str):
        """Publish to Crossbar for legacy mobile app + central telemetry."""
        legacy_topic = TOPIC_MAP.get(topic)
        if not legacy_topic:
            return  # No legacy mapping — skip Crossbar

        # Substitute template variables from data dict
        import re as _re
        placeholders = _re.findall(r'\{(\w+)\}', legacy_topic)
        for key in placeholders:
            val = ''
            if key == 'user_id':
                val = user_id or data.get('user_id', '')
            elif key == 'device_id':
                val = device_id or data.get('device_id', '')
            else:
                val = data.get(key, '')
            if not val:
                return  # Can't route without required variable
            legacy_topic = legacy_topic.replace(f'{{{key}}}', str(val))

        # Add msg_id for dedup
        if isinstance(data, dict):
            data = dict(data)
            data['msg_id'] = msg_id

        payload = json.dumps(data, separators=(',', ':')) if isinstance(data, dict) else str(data)

        # Try native WAMP session first (crossbar_server is optional)
        try:
            from crossbar_server import wamp_session
            if wamp_session:
                import asyncio
                asyncio.ensure_future(wamp_session.publish(legacy_topic, payload))
                self._stats['delivered_crossbar'] += 1
                return
        except (ImportError, RuntimeError):
            pass

        # HTTP bridge fallback (injected by hart_intelligence at startup)
        if self._http_transport:
            try:
                self._http_transport(legacy_topic, payload)
                self._stats['delivered_crossbar'] += 1
            except Exception:
                pass  # No Crossbar available — offline mode

    def _deliver_to_subscribers(self, topic: str, data: dict):
        """Deliver to matching subscribers (exact + wildcard)."""
        with self._lock:
            # Exact match
            handlers = list(self._subscriptions.get(topic, []))

            # Wildcard match (e.g., 'chat.*' matches 'chat.response')
            for pattern, pattern_handlers in self._subscriptions.items():
                if '*' in pattern:
                    import fnmatch
                    if fnmatch.fnmatch(topic, pattern):
                        handlers.extend(pattern_handlers)

        for handler in handlers:
            try:
                handler(topic, data)
            except Exception as e:
                logger.debug(f"MessageBus subscriber error on {topic}: {e}")


# ─── Singleton ────────────────────────────────────────

_bus: Optional[MessageBus] = None
_bus_lock = threading.Lock()


def get_message_bus() -> MessageBus:
    """Get or create the singleton MessageBus."""
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = MessageBus()
    return _bus


def reset_message_bus():
    """Reset singleton (testing only)."""
    global _bus
    _bus = None
