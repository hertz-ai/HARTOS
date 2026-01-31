"""
Multi-Channel Messaging Integration

Provides unified interface for messaging across multiple platforms:
- Telegram
- Discord
- Slack
- WhatsApp (future)

Each adapter implements the ChannelAdapter interface for consistent behavior.
"""

from .base import ChannelAdapter, ChannelStatus, Message, MessageType
from .registry import ChannelRegistry
from .security import (
    PairingManager,
    PairingMiddleware,
    PairingCode,
    PairedSession,
    PairingStatus,
    get_pairing_manager,
)
from .session_manager import (
    ChannelSession,
    ChannelSessionManager,
    SessionIsolationMiddleware,
    ConversationMessage,
    get_session_manager,
)

__all__ = [
    "ChannelAdapter",
    "ChannelStatus",
    "Message",
    "MessageType",
    "ChannelRegistry",
    "PairingManager",
    "PairingMiddleware",
    "PairingCode",
    "PairedSession",
    "PairingStatus",
    "get_pairing_manager",
    "ChannelSession",
    "ChannelSessionManager",
    "SessionIsolationMiddleware",
    "ConversationMessage",
    "get_session_manager",
]
