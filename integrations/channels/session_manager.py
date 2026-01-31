"""
Channel Session Manager

Provides isolated session management for multi-channel messaging.
Each channel/user combination gets its own conversation context,
preventing cross-channel data leakage.

Features:
- Per-channel conversation history
- Session state isolation
- Conversation context management
- Session timeout/cleanup
- Memory limits
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ConversationMessage:
    """A single message in a conversation."""
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ConversationMessage:
        """Create from dictionary."""
        return cls(
            role=data["role"],
            content=data["content"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            metadata=data.get("metadata", {}),
        )


@dataclass
class ChannelSession:
    """
    Represents an isolated session for a channel/user combination.

    Contains conversation history, state, and metadata for a single
    user's interaction through a specific channel.
    """
    channel: str
    sender_id: str
    user_id: Optional[int] = None
    prompt_id: Optional[int] = None
    created_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)
    messages: List[ConversationMessage] = field(default_factory=list)
    state: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Limits
    max_messages: int = 100

    @property
    def session_key(self) -> Tuple[str, str]:
        """Get unique session identifier."""
        return (self.channel, self.sender_id)

    @property
    def message_count(self) -> int:
        """Get number of messages in history."""
        return len(self.messages)

    @property
    def context_window(self) -> List[Dict[str, str]]:
        """Get messages formatted for LLM context."""
        return [
            {"role": msg.role, "content": msg.content}
            for msg in self.messages
        ]

    def add_message(self, role: str, content: str, metadata: Optional[Dict] = None) -> None:
        """
        Add a message to the conversation history.

        Args:
            role: "user" or "assistant"
            content: Message content
            metadata: Optional message metadata
        """
        msg = ConversationMessage(
            role=role,
            content=content,
            metadata=metadata or {},
        )
        self.messages.append(msg)
        self.last_active = datetime.now()

        # Trim if over limit
        if len(self.messages) > self.max_messages:
            self.messages = self.messages[-self.max_messages:]

    def add_user_message(self, content: str, metadata: Optional[Dict] = None) -> None:
        """Add a user message."""
        self.add_message("user", content, metadata)

    def add_assistant_message(self, content: str, metadata: Optional[Dict] = None) -> None:
        """Add an assistant message."""
        self.add_message("assistant", content, metadata)

    def get_state(self, key: str, default: Any = None) -> Any:
        """Get a state value."""
        return self.state.get(key, default)

    def set_state(self, key: str, value: Any) -> None:
        """Set a state value."""
        self.state[key] = value
        self.last_active = datetime.now()

    def clear_state(self) -> None:
        """Clear all state."""
        self.state = {}

    def clear_history(self) -> None:
        """Clear conversation history."""
        self.messages = []

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "channel": self.channel,
            "sender_id": self.sender_id,
            "user_id": self.user_id,
            "prompt_id": self.prompt_id,
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
            "messages": [m.to_dict() for m in self.messages],
            "state": self.state,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ChannelSession:
        """Deserialize from dictionary."""
        session = cls(
            channel=data["channel"],
            sender_id=data["sender_id"],
            user_id=data.get("user_id"),
            prompt_id=data.get("prompt_id"),
            created_at=datetime.fromisoformat(data["created_at"]),
            last_active=datetime.fromisoformat(data["last_active"]),
            state=data.get("state", {}),
            metadata=data.get("metadata", {}),
        )
        session.messages = [
            ConversationMessage.from_dict(m) for m in data.get("messages", [])
        ]
        return session


class LRUSessionCache(OrderedDict):
    """LRU cache for sessions."""

    def __init__(self, maxsize: int = 1000):
        super().__init__()
        self.maxsize = maxsize
        self._lock = threading.Lock()

    def get(self, key: Tuple[str, str], default=None) -> Optional[ChannelSession]:
        """Get a session, moving it to end (most recently used)."""
        with self._lock:
            if key in self:
                self.move_to_end(key)
                return self[key]
            return default

    def put(self, key: Tuple[str, str], value: ChannelSession) -> None:
        """Put a session, evicting oldest if at capacity."""
        with self._lock:
            if key in self:
                self.move_to_end(key)
            else:
                if len(self) >= self.maxsize:
                    # Evict oldest
                    oldest_key = next(iter(self))
                    del self[oldest_key]
            self[key] = value


class ChannelSessionManager:
    """
    Manages isolated sessions for multi-channel messaging.

    Provides session isolation, ensuring that each channel/user
    combination has its own conversation context.

    Usage:
        manager = ChannelSessionManager()

        # Get or create session
        session = manager.get_session("telegram", "user123")

        # Add messages
        session.add_user_message("Hello!")
        session.add_assistant_message("Hi there!")

        # Get context for LLM
        context = session.context_window

        # Store session state
        session.set_state("language", "en")
    """

    def __init__(
        self,
        storage_path: Optional[str] = None,
        max_sessions: int = 1000,
        session_timeout_hours: int = 24,
        auto_persist: bool = True,
    ):
        self.storage_path = storage_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "agent_data",
            "channel_sessions.json"
        )
        self.session_timeout = timedelta(hours=session_timeout_hours)
        self.auto_persist = auto_persist

        # In-memory LRU cache
        self._sessions = LRUSessionCache(maxsize=max_sessions)
        self._lock = threading.Lock()

        # Load persisted sessions
        self._load_sessions()

    def get_session(
        self,
        channel: str,
        sender_id: str,
        user_id: Optional[int] = None,
        prompt_id: Optional[int] = None,
        create: bool = True,
    ) -> Optional[ChannelSession]:
        """
        Get or create a session for a channel/user.

        Args:
            channel: Channel name (e.g., "telegram", "discord")
            sender_id: Unique sender identifier from the channel
            user_id: Agent user ID (if known from pairing)
            prompt_id: Agent prompt ID (if known from pairing)
            create: Whether to create if not exists

        Returns:
            ChannelSession or None if not found and create=False
        """
        key = (channel, sender_id)

        # Try cache first
        session = self._sessions.get(key)
        if session:
            # Check timeout
            if datetime.now() - session.last_active > self.session_timeout:
                # Session expired, remove it
                with self._lock:
                    if key in self._sessions:
                        del self._sessions[key]
                session = None

        # Create if not found and allowed
        if not session and create:
            session = ChannelSession(
                channel=channel,
                sender_id=sender_id,
                user_id=user_id,
                prompt_id=prompt_id,
            )
            self._sessions.put(key, session)

            if self.auto_persist:
                self._save_sessions()

        # Update user/prompt ID if provided
        if session and (user_id is not None or prompt_id is not None):
            if user_id is not None:
                session.user_id = user_id
            if prompt_id is not None:
                session.prompt_id = prompt_id

        return session

    def has_session(self, channel: str, sender_id: str) -> bool:
        """Check if a session exists."""
        return self.get_session(channel, sender_id, create=False) is not None

    def delete_session(self, channel: str, sender_id: str) -> bool:
        """
        Delete a session.

        Returns:
            True if deleted, False if not found
        """
        key = (channel, sender_id)
        with self._lock:
            if key in self._sessions:
                del self._sessions[key]
                if self.auto_persist:
                    self._save_sessions()
                return True
        return False

    def clear_channel_sessions(self, channel: str) -> int:
        """
        Delete all sessions for a channel.

        Returns:
            Number of sessions deleted
        """
        to_delete = [
            key for key in self._sessions.keys()
            if key[0] == channel
        ]

        with self._lock:
            for key in to_delete:
                del self._sessions[key]

        if to_delete and self.auto_persist:
            self._save_sessions()

        return len(to_delete)

    def clear_user_sessions(self, user_id: int) -> int:
        """
        Delete all sessions for an agent user.

        Returns:
            Number of sessions deleted
        """
        to_delete = [
            key for key, session in self._sessions.items()
            if session.user_id == user_id
        ]

        with self._lock:
            for key in to_delete:
                del self._sessions[key]

        if to_delete and self.auto_persist:
            self._save_sessions()

        return len(to_delete)

    def list_sessions(
        self,
        channel: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> List[ChannelSession]:
        """
        List sessions with optional filtering.

        Args:
            channel: Filter by channel
            user_id: Filter by user ID

        Returns:
            List of matching sessions
        """
        sessions = list(self._sessions.values())

        if channel:
            sessions = [s for s in sessions if s.channel == channel]
        if user_id is not None:
            sessions = [s for s in sessions if s.user_id == user_id]

        return sessions

    def get_session_count(self, channel: Optional[str] = None) -> int:
        """Get number of active sessions."""
        if channel:
            return len([s for s in self._sessions.values() if s.channel == channel])
        return len(self._sessions)

    def cleanup_expired(self) -> int:
        """
        Remove expired sessions.

        Returns:
            Number of sessions removed
        """
        now = datetime.now()
        to_delete = [
            key for key, session in self._sessions.items()
            if now - session.last_active > self.session_timeout
        ]

        with self._lock:
            for key in to_delete:
                del self._sessions[key]

        if to_delete and self.auto_persist:
            self._save_sessions()

        return len(to_delete)

    def persist(self) -> None:
        """Manually persist sessions to storage."""
        self._save_sessions()

    def _save_sessions(self) -> None:
        """Save sessions to storage."""
        try:
            data = {
                "sessions": [
                    session.to_dict()
                    for session in self._sessions.values()
                ],
                "saved_at": datetime.now().isoformat(),
            }

            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            with open(self.storage_path, 'w') as f:
                json.dump(data, f, indent=2)

            logger.debug(f"Saved {len(data['sessions'])} sessions")

        except Exception as e:
            logger.error(f"Failed to save sessions: {e}")

    def _load_sessions(self) -> None:
        """Load sessions from storage."""
        try:
            if os.path.exists(self.storage_path):
                with open(self.storage_path, 'r') as f:
                    data = json.load(f)

                loaded = 0
                for session_data in data.get("sessions", []):
                    try:
                        session = ChannelSession.from_dict(session_data)

                        # Skip expired sessions
                        if datetime.now() - session.last_active > self.session_timeout:
                            continue

                        self._sessions.put(session.session_key, session)
                        loaded += 1
                    except Exception as e:
                        logger.warning(f"Failed to load session: {e}")

                logger.info(f"Loaded {loaded} sessions")

        except Exception as e:
            logger.error(f"Failed to load sessions: {e}")


class SessionIsolationMiddleware:
    """
    Middleware that provides session isolation for message handling.

    Usage:
        middleware = SessionIsolationMiddleware(session_manager)

        # In message handler:
        session = middleware.get_session_for_message(message)
        session.add_user_message(message.text)

        # Process with LLM using session.context_window

        session.add_assistant_message(response)
    """

    def __init__(
        self,
        session_manager: ChannelSessionManager,
        pairing_manager: Optional[Any] = None,  # PairingManager
    ):
        self.session_manager = session_manager
        self.pairing_manager = pairing_manager

    def get_session_for_message(self, message: Any) -> ChannelSession:
        """
        Get session for a message, with pairing integration if available.

        Args:
            message: Message object with channel, sender_id attributes

        Returns:
            ChannelSession for this message's sender
        """
        channel = message.channel
        sender_id = message.sender_id

        # Get user mapping from pairing if available
        user_id = None
        prompt_id = None
        if self.pairing_manager:
            mapping = self.pairing_manager.get_user_mapping(channel, sender_id)
            if mapping:
                user_id, prompt_id = mapping

        return self.session_manager.get_session(
            channel=channel,
            sender_id=sender_id,
            user_id=user_id,
            prompt_id=prompt_id,
        )


# Singleton instance
_session_manager: Optional[ChannelSessionManager] = None


def get_session_manager() -> ChannelSessionManager:
    """Get or create the global session manager."""
    global _session_manager
    if _session_manager is None:
        _session_manager = ChannelSessionManager()
    return _session_manager
