"""
Event Bus — Topic-based pub/sub with Crossbar WAMP bridge.

Decouples HART OS subsystems without direct imports. Any module can
emit events, any other can subscribe — config changes, app lifecycle,
theme updates, etc.

Design decisions:
- Topic-based: dot-separated names (e.g., 'config.display.scale')
- Wildcard subscriptions: 'theme.*' matches 'theme.changed', 'theme.preset.applied'
- Sync dispatch by default (callback in emitter's thread)
- Optional async_emit() for non-blocking (uses core/event_loop.py)
- Events are plain dicts — no custom event classes
- Thread-safe via threading.Lock
- WAMP bridge: local events optionally publish to Crossbar; WAMP events
  fire local callbacks. Topic mapping: 'theme.changed' ↔ 'com.hartos.event.theme.changed'

Generalizes patterns from:
- model_bus_service.py (multi-transport routing concept)
- crossbar_server.py (WAMP component lifecycle)
- wamp_bridge.py (Crossbar topic conventions)

Usage:
    bus = EventBus()
    bus.on('config.display.scale', handle_scale_change)
    bus.on('theme.*', handle_any_theme_event)
    bus.emit('config.display.scale', {'old': 1.0, 'new': 1.5})
    bus.off('config.display.scale', handle_scale_change)

    # Optional WAMP bridge (cross-process / cross-device)
    bus.connect_wamp('ws://localhost:8088/ws', 'realm1')
"""

import asyncio
import fnmatch
import json
import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger('hevolve.platform')

# WAMP topic prefix — matches crossbar_server.py / wamp_bridge.py convention
WAMP_TOPIC_PREFIX = 'com.hartos.event'


def _local_to_wamp(topic: str) -> str:
    """Convert local dot-topic to WAMP URI.  theme.changed → com.hartos.event.theme.changed"""
    return f'{WAMP_TOPIC_PREFIX}.{topic}'


def _wamp_to_local(uri: str) -> Optional[str]:
    """Convert WAMP URI to local dot-topic.  com.hartos.event.theme.changed → theme.changed"""
    prefix = WAMP_TOPIC_PREFIX + '.'
    if uri.startswith(prefix):
        return uri[len(prefix):]
    return None


class EventBus:
    """Topic-based pub/sub event bus with optional Crossbar WAMP bridge.

    The decoupling layer for HART OS — subsystems communicate through
    events instead of direct imports.  When a WAMP session is connected,
    every local emit() also publishes to Crossbar, and WAMP subscriptions
    fire local callbacks, enabling cross-process and cross-device events.
    """

    def __init__(self):
        self._listeners: Dict[str, List[Callable]] = {}
        self._wildcard_listeners: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()
        self._emit_count: int = 0
        # WAMP bridge state
        self._wamp_session = None
        self._wamp_connected = False
        self._wamp_loop: Optional[asyncio.AbstractEventLoop] = None
        self._wamp_thread: Optional[threading.Thread] = None
        self._wamp_subscribed_topics: set = set()
        self._bridged_topics: set = set()  # topics currently bridged to WAMP

    def on(self, topic: str, callback: Callable) -> None:
        """Subscribe to a topic.

        Args:
            topic: Event topic. Use '*' for wildcard matching:
                   'theme.*' matches 'theme.changed', 'theme.preset.applied'
                   '*' matches everything.
            callback: Called with (topic, data) when event fires.
        """
        with self._lock:
            if '*' in topic:
                if topic not in self._wildcard_listeners:
                    self._wildcard_listeners[topic] = []
                self._wildcard_listeners[topic].append(callback)
            else:
                if topic not in self._listeners:
                    self._listeners[topic] = []
                self._listeners[topic].append(callback)

    def off(self, topic: str, callback: Callable) -> None:
        """Unsubscribe from a topic.

        Args:
            topic: Same topic string used in on().
            callback: Same callback reference used in on().
        """
        with self._lock:
            target = (self._wildcard_listeners if '*' in topic
                      else self._listeners)
            if topic in target:
                try:
                    target[topic].remove(callback)
                    if not target[topic]:
                        del target[topic]
                except ValueError:
                    pass

    def once(self, topic: str, callback: Callable) -> None:
        """Subscribe to a topic for one event only.

        After the first matching event, the callback is automatically removed.
        """
        def wrapper(t, data):
            self.off(topic, wrapper)
            callback(t, data)
        self.on(topic, wrapper)

    def emit(self, topic: str, data: Any = None, _from_wamp: bool = False) -> int:
        """Emit an event synchronously.

        Args:
            topic: Event topic (e.g., 'config.display.scale').
            data: Event payload (any JSON-serializable value, typically dict).
            _from_wamp: Internal flag — True when event originated from WAMP
                        (prevents echo loop back to Crossbar).

        Returns:
            Number of listeners that were called.
        """
        self._emit_count += 1
        called = 0

        # Exact match listeners
        with self._lock:
            exact = list(self._listeners.get(topic, []))
            wildcards = []
            for pattern, cbs in self._wildcard_listeners.items():
                if fnmatch.fnmatch(topic, pattern):
                    wildcards.extend(cbs)

        for cb in exact:
            try:
                cb(topic, data)
                called += 1
            except Exception as e:
                logger.warning("Event listener error on '%s': %s", topic, e)

        for cb in wildcards:
            try:
                cb(topic, data)
                called += 1
            except Exception as e:
                logger.warning("Wildcard listener error on '%s': %s", topic, e)

        # Bridge to WAMP (skip if event already came from WAMP → no echo)
        if not _from_wamp and self._wamp_connected and self._wamp_session:
            self._publish_to_wamp(topic, data)

        return called

    def emit_async(self, topic: str, data: Any = None) -> None:
        """Emit an event asynchronously (fire-and-forget in a thread).

        Uses a daemon thread so it won't block shutdown.
        """
        t = threading.Thread(target=self.emit, args=(topic, data), daemon=True)
        t.start()

    def has_listeners(self, topic: str) -> bool:
        """Check if a topic has any subscribers (exact or wildcard)."""
        with self._lock:
            if topic in self._listeners and self._listeners[topic]:
                return True
            for pattern in self._wildcard_listeners:
                if fnmatch.fnmatch(topic, pattern):
                    return True
        return False

    def topics(self) -> List[str]:
        """Return all topics with registered listeners."""
        with self._lock:
            exact = list(self._listeners.keys())
            wild = list(self._wildcard_listeners.keys())
        return exact + wild

    def clear(self) -> None:
        """Remove all listeners. For testing."""
        with self._lock:
            self._listeners.clear()
            self._wildcard_listeners.clear()

    # ─── WAMP / Crossbar Bridge ─────────────────────────────

    def connect_wamp(self, url: str = None, realm: str = None) -> bool:
        """Connect EventBus to Crossbar WAMP router.

        Local events are published to WAMP; WAMP events fire local callbacks.
        Uses autobahn (same as crossbar_server.py / wamp_bridge.py).

        Args:
            url:   WebSocket URL (default: CBURL env or ws://localhost:8088/ws)
            realm: WAMP realm  (default: CBREALM env or realm1)

        Returns:
            True if connection initiated (async — may not be connected yet).
        """
        try:
            from autobahn.asyncio.component import Component
        except ImportError:
            logger.warning("autobahn not installed — WAMP bridge unavailable")
            return False

        url = url or os.environ.get('CBURL', 'ws://localhost:8088/ws')
        realm = realm or os.environ.get('CBREALM', 'realm1')

        component = Component(transports=url, realm=realm)
        bus = self  # closure capture

        @component.on_join
        async def on_join(session, details):
            bus._wamp_session = session
            bus._wamp_connected = True
            logger.info("EventBus WAMP bridge connected to %s (realm=%s)", url, realm)

            # Subscribe to the wildcard topic for all HARTOS events
            wamp_wildcard = f'{WAMP_TOPIC_PREFIX}.'
            try:
                await session.subscribe(bus._on_wamp_event, wamp_wildcard,
                                        options={'match': 'prefix'})
                logger.info("EventBus subscribed to WAMP prefix: %s", wamp_wildcard)
            except Exception as e:
                logger.warning("WAMP wildcard subscribe failed: %s", e)

        @component.on_leave
        async def on_leave(session, details):
            bus._wamp_connected = False
            bus._wamp_session = None
            logger.info("EventBus WAMP bridge disconnected")

        # Run WAMP component in a background thread with its own event loop
        # Reconnects with exponential backoff on disconnect/failure
        bus._wamp_stop = False

        def _run():
            import time as _time
            backoff = 1
            max_backoff = 60
            while not bus._wamp_stop:
                loop = asyncio.new_event_loop()
                bus._wamp_loop = loop
                asyncio.set_event_loop(loop)
                connected_at = None
                try:
                    connected_at = _time.time()
                    loop.run_until_complete(component.start(loop=loop))
                except Exception as e:
                    logger.warning("WAMP component exited: %s — reconnecting in %ds", e, backoff)
                finally:
                    bus._wamp_loop = None
                    bus._wamp_connected = False
                    bus._wamp_session = None
                # Reset backoff if connection lived > 60s (was a real session)
                if connected_at and (_time.time() - connected_at) > 60:
                    backoff = 1
                _time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

        self._wamp_thread = threading.Thread(target=_run, daemon=True, name='eventbus-wamp')
        self._wamp_thread.start()
        return True

    def disconnect_wamp(self):
        """Disconnect from Crossbar WAMP router."""
        self._wamp_stop = True
        self._wamp_connected = False
        session = self._wamp_session
        self._wamp_session = None
        if session and self._wamp_loop:
            try:
                asyncio.run_coroutine_threadsafe(session.leave(), self._wamp_loop)
            except Exception:
                pass
        logger.info("EventBus WAMP bridge disconnected")

    def _publish_to_wamp(self, topic: str, data: Any):
        """Publish a local event to WAMP (fire-and-forget)."""
        session = self._wamp_session
        loop = self._wamp_loop
        if not session or not loop:
            return
        wamp_uri = _local_to_wamp(topic)
        # Serialize data to JSON-safe dict for WAMP transport
        try:
            payload = json.loads(json.dumps(data, default=str)) if data is not None else {}
        except (TypeError, ValueError):
            payload = {'value': str(data)}
        try:
            asyncio.run_coroutine_threadsafe(
                session.publish(wamp_uri, payload), loop
            )
        except Exception as e:
            logger.debug("WAMP publish failed for %s: %s", wamp_uri, e)

    async def _on_wamp_event(self, *args, **kwargs):
        """Handle incoming WAMP event → dispatch to local listeners."""
        # autobahn passes positional args; first is payload, details in kwargs
        payload = args[0] if args else kwargs
        details = kwargs.get('details')
        # Extract the WAMP topic from details
        wamp_topic = getattr(details, 'topic', None) if details else None
        if not wamp_topic:
            return
        local_topic = _wamp_to_local(wamp_topic)
        if local_topic:
            # Dispatch locally, but mark _from_wamp to prevent echo
            self.emit(local_topic, payload, _from_wamp=True)

    @property
    def wamp_connected(self) -> bool:
        """Whether the WAMP bridge is currently connected."""
        return self._wamp_connected

    # ─── Properties & Health ──────────────────────────────────

    @property
    def emit_count(self) -> int:
        """Total number of emit() calls since creation."""
        return self._emit_count

    def health(self) -> dict:
        """Health report for ServiceRegistry integration."""
        with self._lock:
            exact_count = sum(len(v) for v in self._listeners.values())
            wild_count = sum(len(v) for v in self._wildcard_listeners.values())
        return {
            'status': 'ok',
            'listeners': exact_count + wild_count,
            'topics': len(self._listeners) + len(self._wildcard_listeners),
            'total_emits': self._emit_count,
            'wamp_connected': self._wamp_connected,
        }


# ─── Module-level helper — safe emit without circular imports ─────

def emit_event(topic: str, data: Any = None, async_: bool = True) -> None:
    """Emit an event on the platform EventBus (if bootstrapped).

    Safe to call from anywhere — no-ops if the platform hasn't been bootstrapped.
    Uses emit_async by default to avoid blocking the caller.

    Args:
        topic: Dot-separated topic (e.g., 'theme.changed', 'resonance.tuned')
        data:  JSON-serializable payload
        async_: If True (default), emit in a background thread
    """
    try:
        from core.platform.registry import get_registry
        registry = get_registry()
        if not registry.has('events'):
            return
        bus = registry.get('events')
        if async_:
            bus.emit_async(topic, data)
        else:
            bus.emit(topic, data)
    except Exception:
        pass  # Never block callers — event emission is best-effort


def broadcast_sse_safe(event_type: str, data: Any, user_id: Any = None) -> bool:
    """Best-effort SSE broadcast to Nunba desktop clients.

    Nunba's ``main.py`` exposes ``broadcast_sse_event`` on ``__main__`` —
    the SSE fan-out is still a distinct transport from the WAMP /
    MessageBus path, so callers that want to reach SSE listeners must
    push there explicitly. This helper encapsulates the
    ``import __main__`` + ``sys.modules.get('main_module')`` fallback
    chain so ``hart_intelligence_entry``, ``integrations.social.realtime``,
    ``model_orchestrator``, etc. don't each reimplement the same 10-line
    try/except block. Until EventBus grows a proper SSE transport
    adapter, this is the canonical single-call entrypoint for pushing a
    payload to SSE subscribers.

    Args:
        event_type: SSE ``event:`` type string (e.g. ``'notification'``,
                    ``'message'``, ``'capability_update'``).
        data:       JSON-serializable payload dict.
        user_id:    Target user for per-user routing. ``None`` broadcasts
                    to every connected SSE client.

    Returns:
        ``True`` if the broadcast function was resolved and invoked,
        ``False`` otherwise. Never raises — SSE delivery is best-effort
        and must not block the caller or mask the primary transport.
    """
    try:
        import sys as _sys
        import __main__ as _main_mod
        broadcast = getattr(_main_mod, 'broadcast_sse_event', None)
        if broadcast is None:
            _mm = _sys.modules.get('main_module')
            if _mm is not None:
                broadcast = getattr(_mm, 'broadcast_sse_event', None)
        if broadcast is None:
            return False
        broadcast(event_type, data, user_id=user_id)
        return True
    except Exception as e:
        logger.debug("SSE broadcast skipped: %s", e)
        return False
