"""
Integration Tests for Multi-Channel Messaging System

Comprehensive test suite that verifies:
1. All components work together
2. End-to-end message flow
3. Security and session integration
4. Flask API compatibility
5. Regression tests for existing functionality
"""

import pytest
import os
import sys
import json
import tempfile
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.base import (
    ChannelConfig,
    ChannelStatus,
    Message,
    MessageType,
    MediaAttachment,
    SendResult,
)
from integrations.channels.registry import ChannelRegistry, ChannelRegistryConfig
from integrations.channels.security import (
    PairingManager,
    PairingMiddleware,
    PairingStatus,
)
from integrations.channels.session_manager import (
    ChannelSessionManager,
    SessionIsolationMiddleware,
    ChannelSession,
)
from integrations.channels.flask_integration import FlaskChannelIntegration


class TestEndToEndMessageFlow:
    """
    Tests complete message flow from channel to agent and back.
    """

    @pytest.fixture
    def temp_storage(self, tmp_path):
        """Create temp storage paths."""
        return {
            "pairing": str(tmp_path / "pairing.json"),
            "sessions": str(tmp_path / "sessions.json"),
        }

    @pytest.fixture
    def pairing_manager(self, temp_storage):
        """Create pairing manager."""
        return PairingManager(storage_path=temp_storage["pairing"])

    @pytest.fixture
    def session_manager(self, temp_storage):
        """Create session manager."""
        return ChannelSessionManager(storage_path=temp_storage["sessions"])

    def test_new_user_pairing_flow(self, pairing_manager):
        """Test new user pairing from code generation to verification."""
        # Generate pairing code
        code = pairing_manager.generate_pairing_code(user_id=100, prompt_id=200)
        assert code is not None

        # User enters code
        session = pairing_manager.verify_pairing(
            channel="telegram",
            sender_id="user123",
            code=code,
        )

        assert session is not None
        assert session.user_id == 100
        assert session.prompt_id == 200
        assert pairing_manager.is_paired("telegram", "user123")

    def test_paired_user_message_flow(self, pairing_manager, session_manager):
        """Test message flow for a paired user."""
        # Pair user
        code = pairing_manager.generate_pairing_code(user_id=100, prompt_id=200)
        pairing_manager.verify_pairing("telegram", "user123", code)

        # Create middleware
        middleware = SessionIsolationMiddleware(session_manager, pairing_manager)

        # Simulate incoming message
        message = Message(
            id="msg1",
            channel="telegram",
            sender_id="user123",
            sender_name="Test User",
            chat_id="chat1",
            text="Hello!",
        )

        # Get session through middleware
        session = middleware.get_session_for_message(message)

        # Session should have user mapping from pairing
        assert session.user_id == 100
        assert session.prompt_id == 200

        # Add messages to session
        session.add_user_message(message.text)
        session.add_assistant_message("Hi there! How can I help?")

        # Verify context window
        context = session.context_window
        assert len(context) == 2
        assert context[0]["role"] == "user"
        assert context[1]["role"] == "assistant"

    def test_unpaired_user_pairing_flow(self, pairing_manager):
        """Test middleware handling unpaired user."""
        middleware = PairingMiddleware(pairing_manager)

        # Check unpaired user
        result = middleware.check_pairing("telegram", "newuser", "Hello!")

        assert not result.is_paired
        assert result.instructions is not None

        # Generate and send pairing code
        code = pairing_manager.generate_pairing_code(user_id=100, prompt_id=200)

        # User enters code
        result = middleware.check_pairing("telegram", "newuser", code)

        assert result.is_paired
        assert result.user_id == 100

    def test_session_isolation_between_channels(self, session_manager):
        """Test that sessions are isolated between channels."""
        # Same sender_id on different channels
        session_tg = session_manager.get_session("telegram", "user123")
        session_dc = session_manager.get_session("discord", "user123")

        session_tg.add_user_message("Message on Telegram")
        session_dc.add_user_message("Message on Discord")

        # Each should have only its own message
        assert session_tg.message_count == 1
        assert session_dc.message_count == 1
        assert "Telegram" in session_tg.messages[0].content
        assert "Discord" in session_dc.messages[0].content


class TestMultiChannelRegistry:
    """Tests for channel registry with multiple adapters."""

    def test_multiple_adapters_registration(self):
        """Test registering multiple adapters."""
        registry = ChannelRegistry()

        # Create mock adapters
        telegram_mock = Mock()
        telegram_mock.name = "telegram"
        telegram_mock.on_message = Mock()
        telegram_mock.get_status.return_value = ChannelStatus.CONNECTED

        discord_mock = Mock()
        discord_mock.name = "discord"
        discord_mock.on_message = Mock()
        discord_mock.get_status.return_value = ChannelStatus.CONNECTED

        # Register both
        registry.register(telegram_mock)
        registry.register(discord_mock)

        # Verify both are registered
        channels = registry.list_channels()
        assert "telegram" in channels
        assert "discord" in channels

    def test_message_routing_to_correct_adapter(self):
        """Test messages are routed to correct adapter."""
        registry = ChannelRegistry()

        telegram_handler = Mock()
        discord_handler = Mock()

        telegram_mock = Mock()
        telegram_mock.name = "telegram"
        telegram_mock.on_message = Mock(side_effect=lambda h: telegram_handler)

        discord_mock = Mock()
        discord_mock.name = "discord"
        discord_mock.on_message = Mock(side_effect=lambda h: discord_handler)

        registry.register(telegram_mock)
        registry.register(discord_mock)

        # Messages should be handler-specific
        assert registry.get("telegram") == telegram_mock
        assert registry.get("discord") == discord_mock


class TestFlaskIntegrationComprehensive:
    """Comprehensive tests for Flask integration."""

    @patch('integrations.channels.flask_integration.pooled_post')
    def test_message_routing_with_context(self, mock_post):
        """Test that messages include full context when routed to agent."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"response": "Hello!"}

        integration = FlaskChannelIntegration(
            agent_api_url="http://localhost:6777/chat",
            default_user_id=100,
            default_prompt_id=200,
        )

        message = Message(
            id="msg123",
            channel="telegram",
            sender_id="user456",
            sender_name="Test User",
            chat_id="chat789",
            text="Test message",
            is_group=False,
        )

        response = integration._handle_message(message)

        # Verify request was made with correct context
        call_args = mock_post.call_args
        payload = call_args[1]["json"]

        assert payload["user_id"] == 100
        assert payload["prompt_id"] == 200
        assert payload["prompt"] == "Test message"
        assert payload["channel_context"]["channel"] == "telegram"
        assert payload["channel_context"]["sender_id"] == "user456"
        assert payload["channel_context"]["chat_id"] == "chat789"

    @patch('integrations.channels.flask_integration.pooled_post')
    def test_user_session_mapping_used(self, mock_post):
        """Test that user session mappings are used."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"response": "Hello!"}

        integration = FlaskChannelIntegration()
        integration.set_user_session("telegram", "user123", 999, 888)

        message = Message(
            id="msg1",
            channel="telegram",
            sender_id="user123",
            chat_id="chat1",
            text="Hello",
        )

        integration._handle_message(message)

        # Verify mapped user_id and prompt_id were used
        payload = mock_post.call_args[1]["json"]
        assert payload["user_id"] == 999
        assert payload["prompt_id"] == 888


class TestSecurityIntegration:
    """Tests for security integration."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create pairing manager."""
        return PairingManager(storage_path=str(tmp_path / "pairing.json"))

    def test_pairing_code_format_secure(self, manager):
        """Test that pairing codes have secure format."""
        code = manager.generate_pairing_code(user_id=100, prompt_id=200)

        # Code should have format XXXXXX-YYYY
        parts = code.split("-")
        assert len(parts) == 2
        assert len(parts[0]) == 6  # Main code
        assert len(parts[1]) == 4  # Signature

    def test_pairing_code_uniqueness(self, manager):
        """Test that each code is unique."""
        codes = set()
        for _ in range(50):
            code = manager.generate_pairing_code(user_id=100, prompt_id=200)
            codes.add(code)

        assert len(codes) == 50

    def test_pairing_code_case_insensitive(self, manager):
        """Test that code verification is case insensitive."""
        code = manager.generate_pairing_code(user_id=100, prompt_id=200)

        # Should work with lowercase
        session = manager.verify_pairing(
            "telegram", "user123", code.lower()
        )
        assert session is not None


class TestSessionManagementIntegration:
    """Tests for session management integration."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create session manager."""
        return ChannelSessionManager(storage_path=str(tmp_path / "sessions.json"))

    def test_conversation_continuity(self, manager):
        """Test that conversations maintain continuity."""
        session = manager.get_session("telegram", "user123")

        # Simulate multi-turn conversation
        session.add_user_message("Hello")
        session.add_assistant_message("Hi! How can I help?")
        session.add_user_message("Tell me about Python")
        session.add_assistant_message("Python is a programming language...")

        context = session.context_window
        assert len(context) == 4

        # Verify order is maintained
        assert context[0]["content"] == "Hello"
        assert context[3]["content"] == "Python is a programming language..."

    def test_state_persistence_across_messages(self, manager):
        """Test that state persists across messages."""
        session = manager.get_session("telegram", "user123")
        session.set_state("mode", "assistant")
        session.set_state("language", "python")

        # Later in conversation
        assert session.get_state("mode") == "assistant"
        assert session.get_state("language") == "python"


class TestRegressionExistingFunctionality:
    """
    Regression tests to ensure new channel system doesn't break existing code.
    """

    def test_helper_module_available(self):
        """Test helper module is still available."""
        try:
            from helper import retrieve_json, topological_sort
            assert retrieve_json is not None
            assert topological_sort is not None
        except ImportError:
            pytest.skip("Helper module has missing dependencies")

    def test_lifecycle_hooks_available(self):
        """Test lifecycle hooks are available."""
        try:
            from lifecycle_hooks import ActionState
            assert hasattr(ActionState, 'ASSIGNED')
            assert hasattr(ActionState, 'IN_PROGRESS')
            assert hasattr(ActionState, 'COMPLETED')
        except ImportError:
            pytest.skip("Lifecycle hooks have missing dependencies")

    def test_flask_app_importable(self):
        """Test Flask app can be imported."""
        try:
            import hart_intelligence_entry
            assert hasattr(hart_intelligence_entry, 'app')
        except ImportError:
            pytest.skip("Flask app has missing dependencies")

    def test_config_json_accessible(self):
        """Test config.json is accessible."""
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "config.json"
        )

        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                config = json.load(f)
            assert isinstance(config, dict)

    def test_all_channel_imports(self):
        """Test all channel imports work."""
        from integrations.channels import (
            # Base
            ChannelAdapter,
            ChannelStatus,
            Message,
            MessageType,
            ChannelRegistry,
            # Security
            PairingManager,
            PairingMiddleware,
            PairingCode,
            PairedSession,
            PairingStatus,
            get_pairing_manager,
            # Sessions
            ChannelSession,
            ChannelSessionManager,
            SessionIsolationMiddleware,
            ConversationMessage,
            get_session_manager,
        )

        assert all([
            ChannelAdapter,
            ChannelStatus,
            Message,
            MessageType,
            ChannelRegistry,
            PairingManager,
            PairingMiddleware,
            PairingCode,
            PairedSession,
            PairingStatus,
            get_pairing_manager,
            ChannelSession,
            ChannelSessionManager,
            SessionIsolationMiddleware,
            ConversationMessage,
            get_session_manager,
        ])


class TestDataIntegrity:
    """Tests for data integrity across components."""

    @pytest.fixture
    def temp_storage(self, tmp_path):
        """Create temp storage."""
        return {
            "pairing": str(tmp_path / "pairing.json"),
            "sessions": str(tmp_path / "sessions.json"),
        }

    def test_persistence_survives_restart(self, temp_storage):
        """Test that data persists across component restarts."""
        # Create and populate managers
        pm1 = PairingManager(storage_path=temp_storage["pairing"])
        sm1 = ChannelSessionManager(storage_path=temp_storage["sessions"])

        code = pm1.generate_pairing_code(user_id=100, prompt_id=200)
        pm1.verify_pairing("telegram", "user123", code)

        session = sm1.get_session("telegram", "user123")
        session.add_user_message("Hello!")
        sm1.persist()

        # Create new instances (simulates restart)
        pm2 = PairingManager(storage_path=temp_storage["pairing"])
        sm2 = ChannelSessionManager(storage_path=temp_storage["sessions"])

        # Data should be preserved
        assert pm2.is_paired("telegram", "user123")
        restored_session = sm2.get_session("telegram", "user123", create=False)
        assert restored_session is not None
        assert restored_session.message_count == 1

    def test_no_cross_contamination(self, temp_storage):
        """Test that data doesn't leak between users/channels."""
        pm = PairingManager(storage_path=temp_storage["pairing"])
        sm = ChannelSessionManager(storage_path=temp_storage["sessions"])

        # Set up two users
        code1 = pm.generate_pairing_code(user_id=100, prompt_id=200)
        code2 = pm.generate_pairing_code(user_id=200, prompt_id=300)

        pm.verify_pairing("telegram", "user1", code1)
        pm.verify_pairing("telegram", "user2", code2)

        session1 = sm.get_session("telegram", "user1", user_id=100, prompt_id=200)
        session2 = sm.get_session("telegram", "user2", user_id=200, prompt_id=300)

        session1.add_user_message("Secret message for user1")
        session1.set_state("secret", "user1_data")

        session2.add_user_message("Secret message for user2")
        session2.set_state("secret", "user2_data")

        # Verify isolation
        assert session1.messages[0].content != session2.messages[0].content
        assert session1.get_state("secret") != session2.get_state("secret")
        assert len(session1.messages) == 1
        assert len(session2.messages) == 1


class TestErrorHandling:
    """Tests for error handling."""

    def test_invalid_pairing_code_handled(self, tmp_path):
        """Test handling of invalid pairing codes."""
        pm = PairingManager(storage_path=str(tmp_path / "pairing.json"))

        result = pm.verify_pairing("telegram", "user123", "INVALID-CODE")
        assert result is None

    def test_missing_session_handled(self, tmp_path):
        """Test handling of missing sessions."""
        sm = ChannelSessionManager(storage_path=str(tmp_path / "sessions.json"))

        session = sm.get_session("telegram", "user123", create=False)
        assert session is None

    @patch('integrations.channels.flask_integration.pooled_post')
    def test_api_error_handled(self, mock_post):
        """Test handling of API errors."""
        mock_post.return_value.status_code = 500
        mock_post.return_value.text = "Internal Server Error"

        integration = FlaskChannelIntegration()

        message = Message(
            id="msg1",
            channel="telegram",
            sender_id="user123",
            chat_id="chat1",
            text="Hello",
        )

        response = integration._handle_message(message)
        assert "error" in response.lower()


class TestPerformance:
    """Performance-related tests."""

    def test_large_conversation_handling(self, tmp_path):
        """Test handling of large conversations."""
        sm = ChannelSessionManager(storage_path=str(tmp_path / "sessions.json"))
        session = sm.get_session("telegram", "user123")
        session.max_messages = 100

        # Add many messages
        for i in range(200):
            session.add_user_message(f"Message {i}")

        # Should be trimmed to max
        assert session.message_count == 100

        # Most recent messages should be kept
        assert session.messages[0].content == "Message 100"
        assert session.messages[-1].content == "Message 199"

    def test_many_sessions_lru(self, tmp_path):
        """Test LRU behavior with many sessions."""
        sm = ChannelSessionManager(
            storage_path=str(tmp_path / "sessions.json"),
            max_sessions=10,
        )

        # Create more sessions than max
        for i in range(20):
            sm.get_session("telegram", f"user{i}")

        # Should have max sessions
        assert sm.get_session_count() <= 10


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
