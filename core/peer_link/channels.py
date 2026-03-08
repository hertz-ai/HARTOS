"""
Channel definitions — what data flows on each PeerLink channel.

Data classification determines encryption behavior:
  OPEN:    Peer list exchange, health — no secrets. Encrypted only on PEER/RELAY trust.
  PRIVATE: User prompts, compute results — always encrypted on cross-user links.
  SYSTEM:  Control messages, heartbeat — no secrets.

Within same-user (ANY network — LAN, WAN, regional): all channels are
unencrypted (your own devices, trust based on authenticated user_id match).
Across users: PRIVATE channels are E2E encrypted at the link layer.
"""
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger('hevolve.peer_link')


class DataClass:
    """Data classification — determines privacy requirements."""
    OPEN = 'open'        # Public data (peer lists, health)
    PRIVATE = 'private'  # User data (prompts, responses, compute results)
    SYSTEM = 'system'    # Infrastructure (heartbeat, control, telemetry metadata)


# Channel registry: name -> config
CHANNEL_REGISTRY = {
    'control': {
        'id': 0x00,
        'data_class': DataClass.SYSTEM,
        'priority': 0,    # Highest priority
        'reliable': True,
        'description': 'Handshake, heartbeat, disconnect, capability updates',
    },
    'compute': {
        'id': 0x01,
        'data_class': DataClass.PRIVATE,  # Prompts are private
        'priority': 1,
        'reliable': True,
        'description': 'Inference offload — prompts, results, model status',
    },
    'dispatch': {
        'id': 0x02,
        'data_class': DataClass.PRIVATE,  # Agent tasks are private
        'priority': 1,
        'reliable': True,
        'description': 'Agent task dispatch — goal execution, streaming results',
    },
    'gossip': {
        'id': 0x03,
        'data_class': DataClass.OPEN,     # Peer lists are public
        'priority': 2,
        'reliable': False,  # Loss-tolerant (retry next round)
        'description': 'Peer list exchange, announce, health check',
    },
    'federation': {
        'id': 0x04,
        'data_class': DataClass.OPEN,     # Federated posts are public
        'priority': 3,
        'reliable': True,
        'description': 'Federated post delivery, follow/unfollow',
    },
    'hivemind': {
        'id': 0x05,
        'data_class': DataClass.PRIVATE,  # Thought vectors are private
        'priority': 1,
        'reliable': True,
        'description': 'HiveMind distributed thought — query, fuse, respond',
    },
    'events': {
        'id': 0x06,
        'data_class': DataClass.OPEN,     # Theme changes, config are public
        'priority': 4,
        'reliable': False,
        'description': 'EventBus cross-device — theme, config, lifecycle',
    },
    'ralt': {
        'id': 0x07,
        'data_class': DataClass.OPEN,     # Skill availability is public
        'priority': 3,
        'reliable': True,
        'description': 'RALT skill distribution — availability, ingestion trigger',
    },
    'sensor': {
        'id': 0x08,
        'data_class': DataClass.PRIVATE,  # Camera/screen frames are private
        'priority': 5,   # Lowest priority (bulk data)
        'reliable': False,
        'description': 'Sensor frames — camera, screen, audio for HevolveAI learning',
    },
}

# Reverse lookup: id -> name
CHANNEL_ID_TO_NAME = {v['id']: k for k, v in CHANNEL_REGISTRY.items()}


def get_channel_config(channel: str) -> dict:
    """Get channel config. Returns empty dict for unknown channels."""
    return CHANNEL_REGISTRY.get(channel, {})


def is_private_channel(channel: str) -> bool:
    """Check if channel carries private data (requires E2E for cross-user)."""
    config = CHANNEL_REGISTRY.get(channel, {})
    return config.get('data_class') == DataClass.PRIVATE


class ChannelDispatcher:
    """Routes incoming PeerLink messages to registered handlers.

    Each subsystem registers its handler:
      dispatcher.register('gossip', peer_discovery.handle_exchange)
      dispatcher.register('federation', federation.receive_inbox)

    When a message arrives on a channel, all registered handlers are called.
    """

    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()

    def register(self, channel: str, handler: Callable) -> None:
        """Register a handler for a channel.

        Handler signature: handler(data: dict, sender_peer_id: str) -> Optional[dict]
        If handler returns a dict, it's sent back as response.
        """
        with self._lock:
            if channel not in self._handlers:
                self._handlers[channel] = []
            self._handlers[channel].append(handler)

    def unregister(self, channel: str, handler: Callable) -> None:
        """Remove a handler."""
        with self._lock:
            handlers = self._handlers.get(channel, [])
            if handler in handlers:
                handlers.remove(handler)

    def dispatch(self, channel: str, data: Any,
                 sender_peer_id: str) -> Optional[dict]:
        """Dispatch a message to all handlers for a channel.

        Returns first non-None response (for request-response patterns).
        """
        with self._lock:
            handlers = list(self._handlers.get(channel, []))

        response = None
        for handler in handlers:
            try:
                result = handler(data, sender_peer_id)
                if result is not None and response is None:
                    response = result
            except Exception as e:
                logger.debug(f"Channel {channel} handler error: {e}")

        return response

    def has_handlers(self, channel: str) -> bool:
        with self._lock:
            return bool(self._handlers.get(channel))

    def get_registered_channels(self) -> List[str]:
        with self._lock:
            return [ch for ch, handlers in self._handlers.items() if handlers]


# Module-level singleton
_dispatcher: Optional[ChannelDispatcher] = None
_dispatcher_lock = threading.Lock()


def get_channel_dispatcher() -> ChannelDispatcher:
    global _dispatcher
    if _dispatcher is None:
        with _dispatcher_lock:
            if _dispatcher is None:
                _dispatcher = ChannelDispatcher()
    return _dispatcher
