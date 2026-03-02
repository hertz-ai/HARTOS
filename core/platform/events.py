"""
Event Bus — Topic-based pub/sub for decoupled communication.

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
- Transport-agnostic: future phases can add WAMP/Redis/D-Bus without API change

Generalizes patterns from:
- model_bus_service.py (multi-transport routing concept)
- core/event_loop.py (async dispatch)

Usage:
    bus = EventBus()
    bus.on('config.display.scale', handle_scale_change)
    bus.on('theme.*', handle_any_theme_event)
    bus.emit('config.display.scale', {'old': 1.0, 'new': 1.5})
    bus.off('config.display.scale', handle_scale_change)
"""

import fnmatch
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger('hevolve.platform')


class EventBus:
    """Topic-based pub/sub event bus.

    The decoupling layer for HART OS — subsystems communicate through
    events instead of direct imports.
    """

    def __init__(self):
        self._listeners: Dict[str, List[Callable]] = {}
        self._wildcard_listeners: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()
        self._emit_count: int = 0

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

    def emit(self, topic: str, data: Any = None) -> int:
        """Emit an event synchronously.

        Args:
            topic: Event topic (e.g., 'config.display.scale').
            data: Event payload (any JSON-serializable value, typically dict).

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
        }
