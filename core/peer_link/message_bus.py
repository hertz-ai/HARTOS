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

from core.constants import RECIPE_AVAILABLE_TOPIC

logger = logging.getLogger('hevolve.peer_link')


# ─── Multi-hop fleet-command relay (gap #57) ──────────────────────────
#
# A signed ``fleet.command`` (e.g. an OTA firmware_update) published by
# central/regional must reach nodes >1 hop away — a flat node linked only to
# a regional, which is linked to central — WITHOUT direct connectivity or USB.
# Only this ONE topic is relayed; every other topic keeps today's
# deliver-local-only behaviour.  Loops/storms are made structurally impossible
# by three independent guards:
#   1. msg_id LRU dedup (primary) — a node delivers+relays each id exactly once.
#   2. hop_ttl decrement, drop at 0 (caps blast radius even if dedup is evicted).
#   3. relay_path membership — a node that sees its own id already in the path
#      drops (independent of the LRU, which can evict under load).
# A relay only PROPAGATES an already-signature-verified command; it holds no
# signing key and cannot forge or re-sign one.
RELAY_TOPIC = 'fleet.command'
RELAY_DEFAULT_HOP_TTL = 4


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
    # Agent coordination — per-user suffix mirrors chat/action/vision/...
    # convention so WAMP router ACL (#246) can gate this topic too.
    # (#510 follow-up — was a global topic before.)
    'agent.multichat': 'com.hertzai.hevolve.agent.multichat.{user_id}',
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
    # Recipe capability mesh: the PROACTIVE advert layer (peer_reuse
    # announce/consume). Dot-alias so the SAME gossip publish rides the
    # WAMP bus the day a node-local recipe router ships. Value imported
    # from core.constants (never inline a topic literal).
    'recipe.available': RECIPE_AVAILABLE_TOPIC,
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
            # Multi-hop fleet.command relay (gap #57)
            'relay_rebroadcast': 0,
            'relay_dropped_unverified': 0,
            'relay_loop_blocked': 0,
            'relay_ttl_expired': 0,
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

        # Seed multi-hop relay metadata for the one relayed topic so a fresh
        # publish starts the chain at this node (gap #57).  Re-broadcasts from
        # _relay_fleet_command carry their own decremented values via the
        # transport envelope, not through publish(), so this only fires for the
        # ORIGINATING publish.  origin is the issuer; relay_path starts empty.
        if topic == RELAY_TOPIC:
            envelope.setdefault('hop_ttl', RELAY_DEFAULT_HOP_TTL)
            envelope.setdefault('origin', data.get('issued_by', '') if isinstance(data, dict) else '')
            envelope.setdefault('relay_path', [])

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

        # 3. PEERLINK — if connected peers exist.  For the relayed topic, carry
        #    the seeded hop_ttl/origin/relay_path on the outbound envelope so a
        #    downstream node can continue the multi-hop chain (gap #57).
        if not skip_peerlink:
            relay_meta = None
            if topic == RELAY_TOPIC:
                relay_meta = {
                    'hop_ttl': envelope.get('hop_ttl', RELAY_DEFAULT_HOP_TTL),
                    'origin': envelope.get('origin', ''),
                    'relay_path': envelope.get('relay_path', []),
                }
            self._route_peerlink(topic, outbound_data, msg_id, relay_meta)

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

    def receive_from_peer(self, envelope: dict, sender_peer_id: str = '') -> bool:
        """Handle message received via PeerLink.

        Deduplicates and delivers to local subscribers.
        Called by ChannelDispatcher when 'events' channel message arrives.

        ``sender_peer_id`` (optional — the ChannelDispatcher passes the source
        peer's node_id) lets a multi-hop relay exclude the inbound link when it
        re-broadcasts a fleet.command, so the command never echoes back the way
        it came.
        """
        msg_id = envelope.get('msg_id', '')
        if not msg_id:
            return False

        if not self._dedup.check_and_add(msg_id):
            self._stats['deduplicated'] += 1
            return False  # Already delivered via another transport

        topic = envelope.get('topic', '')
        data = envelope.get('data', {})

        # MULTI-HOP RELAY (gap #57): only the one relayed topic, only AFTER the
        # dedup gate above proved this message is NEW.  Deliver locally + (if the
        # signed command verifies and the loop/ttl guards pass) re-broadcast to
        # peers EXCEPT the inbound sender.  Every other topic is unaffected.
        if topic == RELAY_TOPIC:
            self._relay_fleet_command(envelope, exclude_peer=sender_peer_id,
                                      from_crossbar=False)
            return True

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

        # MULTI-HOP RELAY (gap #57): a fleet.command that arrived over the shared
        # WAMP bus is delivered locally and then relayed onward to PeerLink peers
        # ONLY (never re-published to Crossbar — WAMP fan-out already reached
        # every subscriber once; re-publishing there is the storm).  PeerLink is
        # point-to-point and is where multi-hop buys reach into NAT'd sub-trees.
        if new_topic == RELAY_TOPIC:
            envelope = {
                'msg_id': msg_id,
                'topic': new_topic,
                'data': data,
                'hop_ttl': data.get('hop_ttl', RELAY_DEFAULT_HOP_TTL),
                'origin': data.get('origin', '') or data.get('issued_by', ''),
                'relay_path': data.get('relay_path', []),
            }
            self._relay_fleet_command(envelope, exclude_peer='',
                                      from_crossbar=True)
            return True

        self._deliver_to_subscribers(new_topic, data)
        return True

    def bootstrap_peerlink_ingress(self) -> bool:
        """Wire the inbound PeerLink 'events' channel → receive_from_peer.

        THE missing wire that made the multi-hop fleet.command relay (gap #57)
        dead code.  ``_route_peerlink`` SENDS every bus message — including the
        relayed 'fleet.command' — on the PeerLink ``'events'`` channel
        (``mgr.broadcast('events', envelope, …)``).  But nothing on the
        RECEIVING side listened to inbound ``'events'`` frames, so a peer's
        ``PeerLink._receive_loop`` found zero handlers for that channel and
        dropped the frame on the floor.  ``receive_from_peer`` (and therefore
        ``_relay_fleet_command``) was only ever reached over the WAMP/Crossbar
        leg — which does NOT reach NAT'd sub-trees.  The PeerLink leg is exactly
        the one that buys reach into a NAT'd node N hops from central, and its
        inbound handler was absent.

        This registers ONE handler on the link manager's ``'events'`` channel.
        ``PeerLinkManager.register_channel_handler`` applies it to every current
        and future link, and ``PeerLink._receive_loop`` invokes channel handlers
        as ``handler(channel, data, peer_id)`` — so the handler takes that
        3-arg link signature and forwards the received envelope into the EXISTING
        ``receive_from_peer`` ingress (dedup + signature-verify + loop/TTL-safe
        relay all live there; this method adds NO second relay path).  The
        inbound peer is passed as ``sender_peer_id`` so the re-broadcast excludes
        the link the command arrived on (no echo-back).

        Idempotent: registers at most once per bus instance.  Best-effort — a
        missing PeerLink layer (HTTP-only tier, tests) is a silent no-op, never
        an error.  Returns True iff the handler was newly registered.
        """
        if getattr(self, '_peerlink_ingress_wired', False):
            return False

        try:
            from core.peer_link.link_manager import get_link_manager
            mgr = get_link_manager()
        except Exception as e:
            logger.debug("PeerLink ingress not wired (no link manager): %s", e)
            return False

        def _on_events_channel(channel, data, sender_peer_id):
            # link.py dispatches as handler(channel, data, peer_id).  The wire
            # envelope carries msg_id/topic/data (+ hop_ttl/origin/relay_path for
            # the relayed topic); hand it straight to the single ingress.
            try:
                if isinstance(data, dict):
                    self.receive_from_peer(data, sender_peer_id=sender_peer_id or '')
            except Exception as e:
                logger.debug("PeerLink 'events' ingress error: %s", e)
            return None  # fire-and-forget; the relay has no synchronous reply

        try:
            mgr.register_channel_handler('events', _on_events_channel)
        except Exception as e:
            logger.debug("PeerLink ingress registration skipped: %s", e)
            return False

        self._peerlink_ingress_wired = True
        logger.info(
            "PeerLink 'events' ingress wired → receive_from_peer "
            "(multi-hop fleet.command relay reachable over PeerLink)")
        return True

    def get_stats(self) -> dict:
        return dict(self._stats)

    # ─── Multi-hop fleet-command relay (gap #57) ─────────

    def _relay_fleet_command(self, envelope: dict, exclude_peer: str = '',
                             from_crossbar: bool = False) -> bool:
        """Deliver a NEW signed fleet.command locally, then (if it verifies and
        the loop/TTL guards pass) re-broadcast it one hop further to peers.

        Precondition: the caller has ALREADY passed the LRU dedup gate for this
        message_id (so this fires at most once per id per node).  This method
        adds the cryptographic + structural guards on top of dedup:

          * SIGNATURE — FleetCommandService.verify_command_signature must pass.
            A relay NEVER forwards an unverified/forged command; it holds no
            signing key and cannot forge or re-sign one.  An unverified command
            is dropped entirely (NOT delivered, NOT relayed).
          * hop_ttl  — decremented each hop; at <= 0 the command is delivered
            locally but NOT re-broadcast (drop-at-zero blast-radius cap).
          * relay_path — this node's id is appended before re-broadcast; a node
            that already appears in the path drops (loop guard independent of
            the LRU, which can evict under load).

        ``exclude_peer`` is the inbound PeerLink sender (skipped on re-broadcast
        so a command never echoes back its arrival link).  ``from_crossbar``
        re-broadcasts to PeerLink peers only (the shared WAMP bus already
        reached every subscriber once — re-publishing there would be the storm).

        Returns True iff the command was re-broadcast to at least one peer.
        """
        data = envelope.get('data', {}) or {}

        # SIGNATURE GATE — single canonical authority check.  Drop on failure.
        try:
            from integrations.social.fleet_command import FleetCommandService
            if not FleetCommandService.verify_command_signature(data):
                logger.warning(
                    "Fleet relay: dropped unverified fleet.command (origin=%s)",
                    (envelope.get('origin', '') or '?')[:8])
                self._stats['relay_dropped_unverified'] += 1
                return False
        except Exception as e:
            # Fail-closed: an unavailable verifier must NOT let a command relay.
            logger.warning("Fleet relay: verifier unavailable, not relaying: %s", e)
            self._stats['relay_dropped_unverified'] += 1
            return False

        # Deliver locally FIRST — this node's own consumers (ota_push_listener
        # .handle_push etc.) must fire regardless of whether we relay onward.
        self._deliver_to_subscribers(envelope.get('topic', RELAY_TOPIC), data)

        # Loop guard #2 (relay_path membership) — independent of the LRU dedup.
        self_id = self._self_node_id()
        relay_path = list(envelope.get('relay_path', []) or [])
        if self_id and self_id in relay_path:
            self._stats['relay_loop_blocked'] += 1
            return False  # already relayed by us on another transport — drop

        # TTL guard — drop-at-zero blast-radius cap.
        hop_ttl = int(envelope.get('hop_ttl', RELAY_DEFAULT_HOP_TTL))
        if hop_ttl <= 0:
            self._stats['relay_ttl_expired'] += 1
            return False  # delivered locally above, but no further re-broadcast

        # Re-broadcast one hop further: decrement TTL, stamp our id on the path.
        if self_id:
            relay_path.append(self_id)
        relay_meta = {
            'hop_ttl': hop_ttl - 1,
            'origin': envelope.get('origin', ''),
            'relay_path': relay_path,
        }
        # msg_id stays IDENTICAL across hops so every downstream node's LRU
        # dedup collapses duplicates arriving via multiple peers (primary
        # anti-storm mechanism).
        self._route_peerlink(RELAY_TOPIC, data, envelope.get('msg_id', ''),
                             relay_meta=relay_meta, exclude_peer=exclude_peer)
        self._stats['relay_rebroadcast'] += 1
        return True

    def _self_node_id(self) -> str:
        """This node's id — REUSE the fleet bus helper (single source)."""
        try:
            from integrations.social.fleet_command import _get_self_node_id
            return _get_self_node_id()
        except Exception:
            return ''

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

    def _route_peerlink(self, topic: str, data: dict, msg_id: str,
                        relay_meta: dict = None, exclude_peer: str = ''):
        """Send to connected peers via PeerLink 'events' channel.

        ``relay_meta`` (when set, for the relayed fleet.command topic) adds the
        hop_ttl/origin/relay_path fields to the wire envelope so a downstream
        node can continue the multi-hop chain.  ``exclude_peer`` skips one peer
        (the inbound sender) on a re-broadcast so a command never echoes back
        the link it arrived on.
        """
        try:
            from core.peer_link.link_manager import get_link_manager
            mgr = get_link_manager()

            envelope = {
                'msg_id': msg_id,
                'topic': topic,
                'data': data,
            }
            if relay_meta:
                envelope.update(relay_meta)

            # Regular bus messages reach the user's OWN devices only (SAME_USER
            # multi-device sync).  The signed fleet.command relay reaches the
            # whole fleet — multi-hop into NAT'd sub-trees is its entire purpose
            # (and it is signature-gated in _relay_fleet_command).  Without this
            # scope, wiring the inbound 'events' handler would deliver a per-user
            # topic to a non-SAME_USER (fleet) peer.
            trust_filter = None
            if topic != RELAY_TOPIC:
                try:
                    from core.peer_link.link import TrustLevel
                    trust_filter = TrustLevel.SAME_USER
                except Exception:
                    trust_filter = None

            sent = mgr.broadcast('events', envelope, trust_filter=trust_filter,
                                 exclude_peer=exclude_peer)
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
