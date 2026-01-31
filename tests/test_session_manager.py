"""
Tests for Channel Session Manager

Tests session isolation, conversation history,
state management, and persistence.
"""

import pytest
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.channels.session_manager import (
    ConversationMessage,
    ChannelSession,
    LRUSessionCache,
    ChannelSessionManager,
    SessionIsolationMiddleware,
    get_session_manager,
)


class TestConversationMessage:
    """Tests for ConversationMessage."""

    def test_message_creation(self):
        """Test message creation."""
        msg = ConversationMessage(
            role="user",
            content="Hello!",
        )

        assert msg.role == "user"
        assert msg.content == "Hello!"
        assert msg.timestamp is not None

    def test_message_serialization(self):
        """Test serialization and deserialization."""
        original = ConversationMessage(
            role="assistant",
            content="Hi there!",
            metadata={"tokens": 10},
        )

        data = original.to_dict()
        restored = ConversationMessage.from_dict(data)

        assert restored.role == original.role
        assert restored.content == original.content
        assert restored.metadata == {"tokens": 10}


class TestChannelSession:
    """Tests for ChannelSession."""

    def test_session_creation(self):
        """Test session creation."""
        session = ChannelSession(
            channel="telegram",
            sender_id="user123",
            user_id=100,
            prompt_id=200,
        )

        assert session.channel == "telegram"
        assert session.sender_id == "user123"
        assert session.user_id == 100
        assert session.session_key == ("telegram", "user123")

    def test_add_messages(self):
        """Test adding messages."""
        session = ChannelSession(channel="telegram", sender_id="user123")

        session.add_user_message("Hello!")
        session.add_assistant_message("Hi there!")

        assert session.message_count == 2
        assert session.messages[0].role == "user"
        assert session.messages[1].role == "assistant"

    def test_context_window(self):
        """Test context window format."""
        session = ChannelSession(channel="telegram", sender_id="user123")
        session.add_user_message("Hello!")
        session.add_assistant_message("Hi!")

        context = session.context_window

        assert len(context) == 2
        assert context[0] == {"role": "user", "content": "Hello!"}
        assert context[1] == {"role": "assistant", "content": "Hi!"}

    def test_message_limit(self):
        """Test message limit enforcement."""
        session = ChannelSession(channel="telegram", sender_id="user123")
        session.max_messages = 5

        # Add more than limit
        for i in range(10):
            session.add_user_message(f"Message {i}")

        assert session.message_count == 5
        # Should keep most recent
        assert session.messages[0].content == "Message 5"
        assert session.messages[-1].content == "Message 9"

    def test_state_management(self):
        """Test state get/set."""
        session = ChannelSession(channel="telegram", sender_id="user123")

        session.set_state("language", "en")
        session.set_state("theme", "dark")

        assert session.get_state("language") == "en"
        assert session.get_state("theme") == "dark"
        assert session.get_state("missing") is None
        assert session.get_state("missing", "default") == "default"

    def test_clear_state(self):
        """Test clearing state."""
        session = ChannelSession(channel="telegram", sender_id="user123")
        session.set_state("key", "value")
        session.clear_state()

        assert session.get_state("key") is None

    def test_clear_history(self):
        """Test clearing conversation history."""
        session = ChannelSession(channel="telegram", sender_id="user123")
        session.add_user_message("Hello!")
        session.clear_history()

        assert session.message_count == 0

    def test_serialization(self):
        """Test full session serialization."""
        original = ChannelSession(
            channel="discord",
            sender_id="user456",
            user_id=300,
            prompt_id=400,
        )
        original.add_user_message("Hello!")
        original.add_assistant_message("Hi!")
        original.set_state("language", "es")

        data = original.to_dict()
        restored = ChannelSession.from_dict(data)

        assert restored.channel == original.channel
        assert restored.sender_id == original.sender_id
        assert restored.user_id == original.user_id
        assert restored.message_count == 2
        assert restored.get_state("language") == "es"


class TestLRUSessionCache:
    """Tests for LRU cache."""

    def test_basic_operations(self):
        """Test basic get/put operations."""
        cache = LRUSessionCache(maxsize=3)

        session = ChannelSession(channel="telegram", sender_id="user1")
        cache.put(("telegram", "user1"), session)

        retrieved = cache.get(("telegram", "user1"))
        assert retrieved is session

    def test_lru_eviction(self):
        """Test LRU eviction when at capacity."""
        cache = LRUSessionCache(maxsize=2)

        s1 = ChannelSession(channel="telegram", sender_id="user1")
        s2 = ChannelSession(channel="telegram", sender_id="user2")
        s3 = ChannelSession(channel="telegram", sender_id="user3")

        cache.put(("telegram", "user1"), s1)
        cache.put(("telegram", "user2"), s2)
        cache.put(("telegram", "user3"), s3)  # Should evict user1

        assert cache.get(("telegram", "user1")) is None
        assert cache.get(("telegram", "user2")) is s2
        assert cache.get(("telegram", "user3")) is s3

    def test_access_updates_lru(self):
        """Test that accessing updates LRU order."""
        cache = LRUSessionCache(maxsize=2)

        s1 = ChannelSession(channel="telegram", sender_id="user1")
        s2 = ChannelSession(channel="telegram", sender_id="user2")
        s3 = ChannelSession(channel="telegram", sender_id="user3")

        cache.put(("telegram", "user1"), s1)
        cache.put(("telegram", "user2"), s2)

        # Access user1, making user2 the oldest
        cache.get(("telegram", "user1"))

        # Add user3, should evict user2
        cache.put(("telegram", "user3"), s3)

        assert cache.get(("telegram", "user1")) is s1  # Still there
        assert cache.get(("telegram", "user2")) is None  # Evicted
        assert cache.get(("telegram", "user3")) is s3


class TestChannelSessionManager:
    """Tests for ChannelSessionManager."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create a session manager with temp storage."""
        storage_path = tmp_path / "sessions.json"
        return ChannelSessionManager(
            storage_path=str(storage_path),
            max_sessions=100,
            session_timeout_hours=24,
        )

    def test_get_or_create_session(self, manager):
        """Test getting or creating a session."""
        session = manager.get_session("telegram", "user123")

        assert session is not None
        assert session.channel == "telegram"
        assert session.sender_id == "user123"

    def test_get_existing_session(self, manager):
        """Test getting an existing session."""
        session1 = manager.get_session("telegram", "user123")
        session1.add_user_message("Hello!")

        session2 = manager.get_session("telegram", "user123")

        assert session1 is session2
        assert session2.message_count == 1

    def test_session_isolation(self, manager):
        """Test that different sessions are isolated."""
        session1 = manager.get_session("telegram", "user1")
        session2 = manager.get_session("telegram", "user2")
        session3 = manager.get_session("discord", "user1")

        session1.add_user_message("Telegram user1")
        session2.add_user_message("Telegram user2")
        session3.add_user_message("Discord user1")

        # Each session should have only its own message
        assert session1.message_count == 1
        assert session2.message_count == 1
        assert session3.message_count == 1

        assert session1.messages[0].content == "Telegram user1"
        assert session2.messages[0].content == "Telegram user2"
        assert session3.messages[0].content == "Discord user1"

    def test_create_false(self, manager):
        """Test create=False returns None for missing session."""
        session = manager.get_session("telegram", "user123", create=False)
        assert session is None

    def test_has_session(self, manager):
        """Test checking session existence."""
        assert not manager.has_session("telegram", "user123")

        manager.get_session("telegram", "user123")

        assert manager.has_session("telegram", "user123")

    def test_delete_session(self, manager):
        """Test deleting a session."""
        manager.get_session("telegram", "user123")
        assert manager.has_session("telegram", "user123")

        result = manager.delete_session("telegram", "user123")

        assert result is True
        assert not manager.has_session("telegram", "user123")

    def test_clear_channel_sessions(self, manager):
        """Test clearing all sessions for a channel."""
        manager.get_session("telegram", "user1")
        manager.get_session("telegram", "user2")
        manager.get_session("discord", "user1")

        count = manager.clear_channel_sessions("telegram")

        assert count == 2
        assert not manager.has_session("telegram", "user1")
        assert not manager.has_session("telegram", "user2")
        assert manager.has_session("discord", "user1")

    def test_clear_user_sessions(self, manager):
        """Test clearing all sessions for a user."""
        session1 = manager.get_session("telegram", "user1", user_id=100)
        session2 = manager.get_session("discord", "user1", user_id=100)
        session3 = manager.get_session("telegram", "user2", user_id=200)

        count = manager.clear_user_sessions(100)

        assert count == 2
        assert not manager.has_session("telegram", "user1")
        assert not manager.has_session("discord", "user1")
        assert manager.has_session("telegram", "user2")

    def test_list_sessions(self, manager):
        """Test listing sessions with filters."""
        manager.get_session("telegram", "user1", user_id=100)
        manager.get_session("telegram", "user2", user_id=100)
        manager.get_session("discord", "user1", user_id=200)

        # All sessions
        all_sessions = manager.list_sessions()
        assert len(all_sessions) == 3

        # By channel
        telegram_sessions = manager.list_sessions(channel="telegram")
        assert len(telegram_sessions) == 2

        # By user
        user_sessions = manager.list_sessions(user_id=100)
        assert len(user_sessions) == 2

    def test_session_count(self, manager):
        """Test getting session count."""
        manager.get_session("telegram", "user1")
        manager.get_session("telegram", "user2")
        manager.get_session("discord", "user1")

        assert manager.get_session_count() == 3
        assert manager.get_session_count("telegram") == 2
        assert manager.get_session_count("discord") == 1

    def test_persistence(self, tmp_path):
        """Test session persistence."""
        storage_path = tmp_path / "sessions.json"

        # Create manager and add session with data
        manager1 = ChannelSessionManager(storage_path=str(storage_path))
        session = manager1.get_session("telegram", "user123", user_id=100)
        session.add_user_message("Hello!")
        session.set_state("language", "en")
        manager1.persist()

        # Create new manager instance
        manager2 = ChannelSessionManager(storage_path=str(storage_path))
        restored = manager2.get_session("telegram", "user123", create=False)

        assert restored is not None
        assert restored.user_id == 100
        assert restored.message_count == 1
        assert restored.messages[0].content == "Hello!"
        assert restored.get_state("language") == "en"

    def test_session_timeout(self, tmp_path):
        """Test that expired sessions are not returned."""
        storage_path = tmp_path / "sessions.json"
        manager = ChannelSessionManager(
            storage_path=str(storage_path),
            session_timeout_hours=1,  # 1 hour timeout
        )

        session = manager.get_session("telegram", "user123")
        # Manually expire the session
        session.last_active = datetime.now() - timedelta(hours=2)

        # Should get a new session, not the expired one
        new_session = manager.get_session("telegram", "user123")
        assert new_session.message_count == 0  # New session, no messages

    def test_cleanup_expired(self, tmp_path):
        """Test cleanup of expired sessions."""
        storage_path = tmp_path / "sessions.json"
        manager = ChannelSessionManager(
            storage_path=str(storage_path),
            session_timeout_hours=1,
        )

        manager.get_session("telegram", "user1")
        session2 = manager.get_session("telegram", "user2")
        # Expire session2
        session2.last_active = datetime.now() - timedelta(hours=2)

        count = manager.cleanup_expired()

        assert count == 1
        assert manager.has_session("telegram", "user1")
        # user2's session was expired and cleaned up


class TestSessionIsolationMiddleware:
    """Tests for SessionIsolationMiddleware."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create session manager."""
        storage_path = tmp_path / "sessions.json"
        return ChannelSessionManager(storage_path=str(storage_path))

    @pytest.fixture
    def middleware(self, manager):
        """Create middleware."""
        return SessionIsolationMiddleware(manager)

    def test_get_session_for_message(self, middleware):
        """Test getting session for a message."""
        message = Mock()
        message.channel = "telegram"
        message.sender_id = "user123"

        session = middleware.get_session_for_message(message)

        assert session is not None
        assert session.channel == "telegram"
        assert session.sender_id == "user123"

    def test_with_pairing_manager(self, manager):
        """Test middleware with pairing integration."""
        # Mock pairing manager
        pairing_manager = Mock()
        pairing_manager.get_user_mapping.return_value = (100, 200)

        middleware = SessionIsolationMiddleware(manager, pairing_manager)

        message = Mock()
        message.channel = "telegram"
        message.sender_id = "user123"

        session = middleware.get_session_for_message(message)

        assert session.user_id == 100
        assert session.prompt_id == 200


class TestGlobalSessionManager:
    """Tests for global session manager."""

    def test_get_session_manager_singleton(self):
        """Test singleton pattern."""
        import integrations.channels.session_manager as sm_module
        sm_module._session_manager = None

        manager1 = get_session_manager()
        manager2 = get_session_manager()

        assert manager1 is manager2


class TestRegressionChannels:
    """Regression tests."""

    def test_channel_imports_work(self):
        """Test that channel imports still work."""
        from integrations.channels import (
            ChannelAdapter,
            ChannelStatus,
            Message,
            ChannelRegistry,
            PairingManager,
        )

        assert ChannelAdapter is not None
        assert PairingManager is not None

    def test_security_imports_work(self):
        """Test security imports still work."""
        from integrations.channels.security import (
            PairingManager,
            PairingMiddleware,
        )

        assert PairingManager is not None
        assert PairingMiddleware is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
