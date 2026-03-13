"""
Event Client — Thin wrapper for EventBus pub/sub.

Usage:
    from hart_sdk import events

    events.emit('task.completed', {'result': 'ok'})
    events.on('theme.changed', handle_theme)
    events.off('theme.changed', handle_theme)
    events.once('app.ready', on_ready)
"""

from typing import Any, Callable, Dict, Optional


class EventClient:
    """Singleton event client for HART OS EventBus."""

    def _get_bus(self):
        """Get the EventBus from ServiceRegistry."""
        try:
            from core.platform.registry import get_registry
            registry = get_registry()
            if registry.has('events'):
                return registry.get('events')
        except ImportError:
            pass
        return None

    def emit(self, topic: str, data: Optional[Dict[str, Any]] = None) -> bool:
        """Emit an event.

        Returns True if emitted, False if EventBus unavailable.
        """
        try:
            from core.platform.events import emit_event
            emit_event(topic, data or {})
            return True
        except Exception:
            return False

    def on(self, topic: str, callback: Callable) -> bool:
        """Subscribe to a topic.

        Returns True if subscribed, False if EventBus unavailable.
        """
        bus = self._get_bus()
        if bus is None:
            return False
        bus.on(topic, callback)
        return True

    def off(self, topic: str, callback: Callable) -> bool:
        """Unsubscribe from a topic.

        Returns True if unsubscribed, False if EventBus unavailable.
        """
        bus = self._get_bus()
        if bus is None:
            return False
        bus.off(topic, callback)
        return True

    def once(self, topic: str, callback: Callable) -> bool:
        """Subscribe for a single event.

        Returns True if subscribed, False if EventBus unavailable.
        """
        bus = self._get_bus()
        if bus is None:
            return False
        bus.once(topic, callback)
        return True


# Singleton
events = EventClient()
