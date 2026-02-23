"""
Tests for Built-in Commands

Tests all 20+ built-in commands including:
- User commands (help, start, stop, status, pair, unpair, clear, history, model, language, timezone, feedback)
- Group commands (mention, quiet, resume)
- Admin commands (broadcast, stats, users, ban, unban, config, reload, debug)
"""

import asyncio
import pytest
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from typing import Dict, Any, List

# Import the modules under test
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.commands.builtin import (
    BuiltinCommands,
    CommandContext,
    CommandResult,
    get_builtin_commands,
    reset_builtin_commands,
    register_builtin_commands,
)
from integrations.channels.commands.registry import (
    CommandRegistry,
    CommandDefinition,
    CommandCategory,
    reset_command_registry,
)
from integrations.channels.commands.mention_gating import (
    MentionGate,
    MentionMode,
    reset_mention_gate,
)


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest.fixture(autouse=True)
def reset_globals():
    """Reset global state before each test."""
    reset_builtin_commands()
    reset_command_registry()
    reset_mention_gate()
    yield
    reset_builtin_commands()
    reset_command_registry()
    reset_mention_gate()


@pytest.fixture
def registry():
    """Create a fresh command registry."""
    return CommandRegistry()


@pytest.fixture
def mock_session_manager():
    """Create a mock session manager."""
    manager = Mock()
    manager.get_session_count.return_value = 10

    # Mock session
    mock_session = Mock()
    mock_session.messages = [
        Mock(role="user", content="Hello"),
        Mock(role="assistant", content="Hi there!"),
        Mock(role="user", content="How are you?"),
    ]
    mock_session.message_count = 3
    mock_session.user_id = 123
    mock_session.clear_history = Mock()

    manager.get_session.return_value = mock_session
    manager.list_sessions.return_value = [mock_session]

    return manager


@pytest.fixture
def mock_pairing_manager():
    """Create a mock pairing manager."""
    manager = Mock()

    # Mock successful pairing
    mock_session = Mock()
    mock_session.user_id = 123
    mock_session.prompt_id = 456

    manager.verify_pairing.return_value = mock_session
    manager.unpair.return_value = True

    return manager


@pytest.fixture
def mention_gate():
    """Create a mention gate."""
    return MentionGate()


@pytest.fixture
def builtin(registry, mock_session_manager, mock_pairing_manager, mention_gate):
    """Create a BuiltinCommands instance with mocks."""
    return BuiltinCommands(
        registry=registry,
        session_manager=mock_session_manager,
        pairing_manager=mock_pairing_manager,
        mention_gate=mention_gate,
        admin_ids={"admin123"},
        bot_name="TestBot",
        bot_version="1.0.0-test",
    )


@pytest.fixture
def user_context():
    """Create a basic user context."""
    return CommandContext(
        channel="telegram",
        chat_id="chat123",
        sender_id="user456",
        sender_name="Test User",
        is_admin=False,
        is_group=False,
    )


@pytest.fixture
def admin_context():
    """Create an admin context."""
    return CommandContext(
        channel="telegram",
        chat_id="chat123",
        sender_id="admin123",
        sender_name="Admin User",
        is_admin=True,
        is_group=False,
    )


@pytest.fixture
def group_context():
    """Create a group context."""
    return CommandContext(
        channel="telegram",
        chat_id="group789",
        sender_id="user456",
        sender_name="Test User",
        is_admin=False,
        is_group=True,
    )


# ==============================================================================
# Test Command Registration
# ==============================================================================

class TestCommandRegistration:
    """Tests for command registration."""

    def test_register_all_commands(self, builtin):
        """Test that all commands are registered."""
        builtin.register_all()

        # Should have 20+ commands
        assert len(builtin.registry) >= 20

    def test_register_user_commands(self, builtin):
        """Test user commands are registered."""
        builtin.register_all()

        user_commands = [
            "help", "start", "stop", "status",
            "pair", "unpair", "clear", "history",
            "model", "language", "timezone", "feedback"
        ]

        for cmd in user_commands:
            assert builtin.registry.has_command(cmd), f"Command {cmd} not registered"

    def test_register_group_commands(self, builtin):
        """Test group commands are registered."""
        builtin.register_all()

        group_commands = ["mention", "quiet", "resume"]

        for cmd in group_commands:
            assert builtin.registry.has_command(cmd), f"Command {cmd} not registered"

    def test_register_admin_commands(self, builtin):
        """Test admin commands are registered."""
        builtin.register_all()

        admin_commands = [
            "broadcast", "stats", "users",
            "ban", "unban", "config", "reload", "debug"
        ]

        for cmd in admin_commands:
            assert builtin.registry.has_command(cmd), f"Command {cmd} not registered"

    def test_command_aliases(self, builtin):
        """Test that command aliases work."""
        builtin.register_all()

        # Test some aliases
        assert builtin.registry.get_by_alias("/help") is not None
        assert builtin.registry.get_by_alias("/h") is not None
        assert builtin.registry.get_by_alias("/?") is not None

        assert builtin.registry.get_by_alias("/language") is not None
        assert builtin.registry.get_by_alias("/lang") is not None


# ==============================================================================
# Test User Commands
# ==============================================================================

class TestHelpCommand:
    """Tests for /help command."""

    @pytest.mark.asyncio
    async def test_help_list_all(self, builtin, user_context):
        """Test /help lists all commands."""
        builtin.register_all()

        result = await builtin.execute("help", user_context)

        assert result.success
        assert "Commands" in result.message
        assert "/help" in result.message or "help" in result.message

    @pytest.mark.asyncio
    async def test_help_specific_command(self, builtin, user_context):
        """Test /help <command> shows specific help."""
        builtin.register_all()

        user_context.raw_args = "start"
        result = await builtin.execute("help", user_context)

        assert result.success
        assert "start" in result.message.lower()

    @pytest.mark.asyncio
    async def test_help_unknown_command(self, builtin, user_context):
        """Test /help for unknown command."""
        builtin.register_all()

        user_context.raw_args = "nonexistent"
        result = await builtin.execute("help", user_context)

        assert not result.success
        assert "Unknown" in result.error


class TestStartStopCommands:
    """Tests for /start and /stop commands."""

    @pytest.mark.asyncio
    async def test_start_command(self, builtin, user_context):
        """Test /start sends welcome message."""
        builtin.register_all()

        result = await builtin.execute("start", user_context)

        assert result.success
        assert "Hello" in result.message or "Welcome" in result.message

    @pytest.mark.asyncio
    async def test_stop_command(self, builtin, user_context):
        """Test /stop stops receiving messages."""
        builtin.register_all()

        result = await builtin.execute("stop", user_context)

        assert result.success
        assert "stopped" in result.message.lower()

        # Check user is marked as stopped
        assert builtin._is_stopped(user_context.channel, user_context.sender_id)

    @pytest.mark.asyncio
    async def test_start_after_stop(self, builtin, user_context):
        """Test /start after /stop resumes."""
        builtin.register_all()

        # Stop first
        await builtin.execute("stop", user_context)
        assert builtin._is_stopped(user_context.channel, user_context.sender_id)

        # Start again
        result = await builtin.execute("start", user_context)
        assert result.success
        assert not builtin._is_stopped(user_context.channel, user_context.sender_id)


class TestStatusCommand:
    """Tests for /status command."""

    @pytest.mark.asyncio
    async def test_status_command(self, builtin, user_context):
        """Test /status shows status."""
        builtin.register_all()

        result = await builtin.execute("status", user_context)

        assert result.success
        assert "Status" in result.message
        assert builtin.bot_version in result.message

    @pytest.mark.asyncio
    async def test_status_shows_channel(self, builtin, user_context):
        """Test /status shows current channel."""
        builtin.register_all()

        result = await builtin.execute("status", user_context)

        assert result.success
        assert user_context.channel in result.message


class TestPairingCommands:
    """Tests for /pair and /unpair commands."""

    @pytest.mark.asyncio
    async def test_pair_success(self, builtin, user_context):
        """Test successful pairing."""
        builtin.register_all()

        user_context.raw_args = "ABC123-XYZ1"
        result = await builtin.execute("pair", user_context)

        assert result.success
        assert "successful" in result.message.lower()

    @pytest.mark.asyncio
    async def test_pair_no_code(self, builtin, user_context):
        """Test pairing without code."""
        builtin.register_all()

        user_context.raw_args = None
        result = await builtin.execute("pair", user_context)

        assert not result.success

    @pytest.mark.asyncio
    async def test_pair_invalid_code(self, builtin, mock_pairing_manager, user_context):
        """Test pairing with invalid code."""
        builtin.register_all()
        mock_pairing_manager.verify_pairing.return_value = None

        user_context.raw_args = "INVALID"
        result = await builtin.execute("pair", user_context)

        assert not result.success
        assert "invalid" in result.error.lower() or "expired" in result.error.lower()

    @pytest.mark.asyncio
    async def test_unpair_success(self, builtin, user_context):
        """Test successful unpairing."""
        builtin.register_all()

        user_context.user_id = 123
        user_context.prompt_id = 456
        result = await builtin.execute("unpair", user_context)

        assert result.success
        assert "unpaired" in result.message.lower()

    @pytest.mark.asyncio
    async def test_unpair_not_paired(self, builtin, user_context):
        """Test unpairing when not paired."""
        builtin.register_all()

        user_context.user_id = None
        user_context.prompt_id = None
        result = await builtin.execute("unpair", user_context)

        assert not result.success


class TestHistoryCommands:
    """Tests for /clear and /history commands."""

    @pytest.mark.asyncio
    async def test_clear_history(self, builtin, mock_session_manager, user_context):
        """Test /clear clears history."""
        builtin.register_all()

        result = await builtin.execute("clear", user_context)

        assert result.success
        assert "cleared" in result.message.lower()

    @pytest.mark.asyncio
    async def test_history_default(self, builtin, mock_session_manager, user_context):
        """Test /history shows last messages."""
        builtin.register_all()

        # No args - use default count
        result = await builtin.execute("history", user_context)

        assert result.success
        assert "messages" in result.message.lower()

    @pytest.mark.asyncio
    async def test_history_with_count(self, builtin, mock_session_manager, user_context):
        """Test /history <n> with specific count."""
        builtin.register_all()

        user_context.raw_args = "5"
        result = await builtin.execute("history", user_context)

        assert result.success


class TestPreferenceCommands:
    """Tests for /model, /language, /timezone commands."""

    @pytest.mark.asyncio
    async def test_model_show(self, builtin, user_context):
        """Test /model shows current model."""
        builtin.register_all()

        # No args - show current
        result = await builtin.execute("model", user_context)

        assert result.success
        assert "model" in result.message.lower()

    @pytest.mark.asyncio
    async def test_model_set(self, builtin, user_context):
        """Test /model <name> sets model."""
        builtin.register_all()

        user_context.raw_args = "gpt-4"
        result = await builtin.execute("model", user_context)

        assert result.success
        assert "gpt-4" in result.message

        # Verify stored
        assert builtin.get_user_model(user_context.channel, user_context.sender_id) == "gpt-4"

    @pytest.mark.asyncio
    async def test_language_show(self, builtin, user_context):
        """Test /language shows current language."""
        builtin.register_all()

        # No args - show current
        result = await builtin.execute("language", user_context)

        assert result.success

    @pytest.mark.asyncio
    async def test_language_set(self, builtin, user_context):
        """Test /language <lang> sets language."""
        builtin.register_all()

        user_context.raw_args = "es"
        result = await builtin.execute("language", user_context)

        assert result.success
        assert "es" in result.message

    @pytest.mark.asyncio
    async def test_timezone_show(self, builtin, user_context):
        """Test /timezone shows current timezone."""
        builtin.register_all()

        # No args - show current
        result = await builtin.execute("timezone", user_context)

        assert result.success

    @pytest.mark.asyncio
    async def test_timezone_set(self, builtin, user_context):
        """Test /timezone <tz> sets timezone."""
        builtin.register_all()

        user_context.raw_args = "America/New_York"
        result = await builtin.execute("timezone", user_context)

        assert result.success
        assert "America/New_York" in result.message


class TestFeedbackCommand:
    """Tests for /feedback command."""

    @pytest.mark.asyncio
    async def test_feedback_success(self, builtin, user_context):
        """Test /feedback submits feedback."""
        builtin.register_all()

        user_context.raw_args = "This is great feedback!"
        result = await builtin.execute("feedback", user_context)

        assert result.success
        assert "thank" in result.message.lower()

    @pytest.mark.asyncio
    async def test_feedback_no_text(self, builtin, user_context):
        """Test /feedback without text."""
        builtin.register_all()

        user_context.raw_args = None
        result = await builtin.execute("feedback", user_context)

        assert not result.success

    @pytest.mark.asyncio
    async def test_feedback_handler_called(self, builtin, user_context):
        """Test feedback handler is called."""
        builtin.register_all()

        handler = Mock()
        builtin.set_feedback_handler(handler)

        user_context.raw_args = "Test feedback that is long enough"
        await builtin.execute("feedback", user_context)

        handler.assert_called_once()


# ==============================================================================
# Test Group Commands
# ==============================================================================

class TestMentionCommand:
    """Tests for /mention command."""

    @pytest.mark.asyncio
    async def test_mention_on(self, builtin, group_context):
        """Test /mention on sets always mode."""
        builtin.register_all()

        group_context.raw_args = "on"
        result = await builtin.execute("mention", group_context)

        assert result.success
        assert builtin.mention_gate.get_mode(group_context.chat_id) == MentionMode.ALWAYS

    @pytest.mark.asyncio
    async def test_mention_off(self, builtin, group_context):
        """Test /mention off sets mention mode."""
        builtin.register_all()

        group_context.raw_args = "off"
        result = await builtin.execute("mention", group_context)

        assert result.success
        assert builtin.mention_gate.get_mode(group_context.chat_id) == MentionMode.MENTION

    @pytest.mark.asyncio
    async def test_mention_reply(self, builtin, group_context):
        """Test /mention reply sets commands only mode."""
        builtin.register_all()

        group_context.raw_args = "reply"
        result = await builtin.execute("mention", group_context)

        assert result.success
        assert builtin.mention_gate.get_mode(group_context.chat_id) == MentionMode.COMMANDS_ONLY

    @pytest.mark.asyncio
    async def test_mention_not_in_group(self, builtin, user_context):
        """Test /mention fails outside group."""
        builtin.register_all()

        user_context.raw_args = "on"
        result = await builtin.execute("mention", user_context)

        assert not result.success
        assert "group" in result.error.lower()


class TestQuietResumeCommands:
    """Tests for /quiet and /resume commands."""

    @pytest.mark.asyncio
    async def test_quiet_command(self, builtin, group_context):
        """Test /quiet silences bot in group."""
        builtin.register_all()

        result = await builtin.execute("quiet", group_context)

        assert result.success
        assert builtin.mention_gate.get_mode(group_context.chat_id) == MentionMode.QUIET

    @pytest.mark.asyncio
    async def test_resume_command(self, builtin, group_context):
        """Test /resume resumes bot in group."""
        builtin.register_all()

        # First quiet
        await builtin.execute("quiet", group_context)

        # Then resume
        result = await builtin.execute("resume", group_context)

        assert result.success
        assert builtin.mention_gate.get_mode(group_context.chat_id) != MentionMode.QUIET

    @pytest.mark.asyncio
    async def test_quiet_not_in_group(self, builtin, user_context):
        """Test /quiet fails outside group."""
        builtin.register_all()

        result = await builtin.execute("quiet", user_context)

        assert not result.success

    @pytest.mark.asyncio
    async def test_resume_not_in_group(self, builtin, user_context):
        """Test /resume fails outside group."""
        builtin.register_all()

        result = await builtin.execute("resume", user_context)

        assert not result.success


# ==============================================================================
# Test Admin Commands
# ==============================================================================

class TestAdminAccessControl:
    """Tests for admin command access control."""

    @pytest.mark.asyncio
    async def test_admin_command_blocked_for_user(self, builtin, user_context):
        """Test admin commands blocked for regular users."""
        builtin.register_all()

        result = await builtin.execute("stats", user_context)

        assert not result.success
        assert "admin" in result.error.lower()

    @pytest.mark.asyncio
    async def test_admin_command_allowed_for_admin(self, builtin, admin_context):
        """Test admin commands work for admins."""
        builtin.register_all()

        result = await builtin.execute("stats", admin_context)

        assert result.success


class TestBroadcastCommand:
    """Tests for /broadcast command."""

    @pytest.mark.asyncio
    async def test_broadcast_success(self, builtin, admin_context):
        """Test /broadcast sends to all users."""
        builtin.register_all()

        handler = AsyncMock(return_value=10)
        builtin.set_broadcast_handler(handler)

        admin_context.raw_args = "Hello everyone!"
        result = await builtin.execute("broadcast", admin_context)

        assert result.success
        assert "10" in result.message

    @pytest.mark.asyncio
    async def test_broadcast_no_message(self, builtin, admin_context):
        """Test /broadcast without message."""
        builtin.register_all()

        admin_context.raw_args = None
        result = await builtin.execute("broadcast", admin_context)

        assert not result.success


class TestStatsCommand:
    """Tests for /stats command."""

    @pytest.mark.asyncio
    async def test_stats_command(self, builtin, admin_context):
        """Test /stats shows statistics."""
        builtin.register_all()

        result = await builtin.execute("stats", admin_context)

        assert result.success
        assert "Statistics" in result.message


class TestUsersCommand:
    """Tests for /users command."""

    @pytest.mark.asyncio
    async def test_users_command(self, builtin, admin_context):
        """Test /users lists active users."""
        builtin.register_all()

        result = await builtin.execute("users", admin_context)

        assert result.success
        assert "Active" in result.message or "users" in result.message.lower()


class TestBanUnbanCommands:
    """Tests for /ban and /unban commands."""

    @pytest.mark.asyncio
    async def test_ban_user(self, builtin, admin_context):
        """Test /ban blocks a user."""
        builtin.register_all()

        admin_context.raw_args = "baduser123"
        result = await builtin.execute("ban", admin_context)

        assert result.success
        assert builtin._is_banned("baduser123")

    @pytest.mark.asyncio
    async def test_unban_user(self, builtin, admin_context):
        """Test /unban unblocks a user."""
        builtin.register_all()

        # First ban
        admin_context.raw_args = "baduser123"
        await builtin.execute("ban", admin_context)

        # Then unban
        result = await builtin.execute("unban", admin_context)

        assert result.success
        assert not builtin._is_banned("baduser123")

    @pytest.mark.asyncio
    async def test_ban_no_user(self, builtin, admin_context):
        """Test /ban without user."""
        builtin.register_all()

        admin_context.raw_args = None
        result = await builtin.execute("ban", admin_context)

        assert not result.success

    @pytest.mark.asyncio
    async def test_banned_user_blocked(self, builtin, user_context):
        """Test banned user is blocked from commands."""
        builtin.register_all()

        # Ban the user
        builtin._banned_users.add(user_context.sender_id)

        result = await builtin.execute("help", user_context)

        assert not result.success
        assert "blocked" in result.error.lower()


class TestConfigCommand:
    """Tests for /config command."""

    @pytest.mark.asyncio
    async def test_config_get(self, builtin, admin_context):
        """Test /config get retrieves value."""
        builtin.register_all()
        builtin.config_store["test_key"] = "test_value"

        admin_context.raw_args = "get test_key"
        result = await builtin.execute("config", admin_context)

        assert result.success
        assert "test_value" in result.message

    @pytest.mark.asyncio
    async def test_config_set(self, builtin, admin_context):
        """Test /config set stores value."""
        builtin.register_all()

        admin_context.raw_args = "set new_key new_value"
        result = await builtin.execute("config", admin_context)

        assert result.success
        assert builtin.config_store["new_key"] == "new_value"

    @pytest.mark.asyncio
    async def test_config_get_missing(self, builtin, admin_context):
        """Test /config get for missing key."""
        builtin.register_all()

        admin_context.raw_args = "get nonexistent"
        result = await builtin.execute("config", admin_context)

        assert not result.success


class TestReloadCommand:
    """Tests for /reload command."""

    @pytest.mark.asyncio
    async def test_reload_command(self, builtin, admin_context):
        """Test /reload triggers reload."""
        builtin.register_all()

        result = await builtin.execute("reload", admin_context)

        assert result.success
        assert "reload" in result.message.lower()


class TestDebugCommand:
    """Tests for /debug command."""

    @pytest.mark.asyncio
    async def test_debug_on(self, builtin, admin_context):
        """Test /debug on enables debug mode."""
        builtin.register_all()

        admin_context.raw_args = "on"
        result = await builtin.execute("debug", admin_context)

        assert result.success
        assert builtin._debug_mode is True

    @pytest.mark.asyncio
    async def test_debug_off(self, builtin, admin_context):
        """Test /debug off disables debug mode."""
        builtin.register_all()
        builtin._debug_mode = True

        admin_context.raw_args = "off"
        result = await builtin.execute("debug", admin_context)

        assert result.success
        assert builtin._debug_mode is False


# ==============================================================================
# Test CommandResult Helper Methods
# ==============================================================================

class TestCommandResult:
    """Tests for CommandResult helper methods."""

    def test_ok_result(self):
        """Test CommandResult.ok creates success."""
        result = CommandResult.ok("Success message", data="value")

        assert result.success
        assert result.message == "Success message"
        assert result.data["data"] == "value"

    def test_fail_result(self):
        """Test CommandResult.fail creates failure."""
        result = CommandResult.fail("Error message")

        assert not result.success
        assert result.error == "Error message"

    def test_silent_ok(self):
        """Test CommandResult.silent_ok creates silent success."""
        result = CommandResult.silent_ok(data="value")

        assert result.success
        assert result.silent


# ==============================================================================
# Test Global Functions
# ==============================================================================

class TestGlobalFunctions:
    """Tests for global helper functions."""

    def test_get_builtin_commands(self):
        """Test get_builtin_commands returns singleton."""
        cmd1 = get_builtin_commands()
        cmd2 = get_builtin_commands()

        assert cmd1 is cmd2

    def test_reset_builtin_commands(self):
        """Test reset_builtin_commands resets singleton."""
        cmd1 = get_builtin_commands()
        reset_builtin_commands()
        cmd2 = get_builtin_commands()

        assert cmd1 is not cmd2

    def test_register_builtin_commands(self):
        """Test register_builtin_commands registers all."""
        builtin = register_builtin_commands(bot_name="TestBot")

        assert len(builtin.registry) >= 20
        assert builtin.bot_name == "TestBot"


# ==============================================================================
# Test Edge Cases
# ==============================================================================

class TestEdgeCases:
    """Tests for edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_unknown_command(self, builtin, user_context):
        """Test executing unknown command."""
        builtin.register_all()

        result = await builtin.execute("nonexistent", user_context)

        assert not result.success
        assert "Unknown" in result.error

    @pytest.mark.asyncio
    async def test_disabled_command(self, builtin, user_context):
        """Test executing disabled command."""
        builtin.register_all()
        builtin.registry.disable_command("help")

        result = await builtin.execute("help", user_context)

        assert not result.success
        assert "disabled" in result.error.lower()

    @pytest.mark.asyncio
    async def test_stopped_user_silent(self, builtin, user_context):
        """Test stopped user gets silent response."""
        builtin.register_all()

        # Stop the user
        await builtin.execute("stop", user_context)

        # Other commands should be silent
        result = await builtin.execute("help", user_context)

        assert result.success
        assert result.silent

    @pytest.mark.asyncio
    async def test_pairing_not_configured(self, builtin, user_context):
        """Test pairing when not configured."""
        builtin.pairing_manager = None
        builtin.register_all()

        user_context.raw_args = "ABC123"
        result = await builtin.execute("pair", user_context)

        assert not result.success
        assert "not configured" in result.error.lower()


# ==============================================================================
# Test Integration
# ==============================================================================

class TestIntegration:
    """Integration tests for built-in commands."""

    @pytest.mark.asyncio
    async def test_full_user_flow(self, builtin, user_context):
        """Test complete user interaction flow."""
        builtin.register_all()

        # Start
        result = await builtin.execute("start", user_context)
        assert result.success

        # Get help
        user_context.raw_args = None
        result = await builtin.execute("help", user_context)
        assert result.success

        # Set preferences
        user_context.raw_args = "gpt-4"
        result = await builtin.execute("model", user_context)
        assert result.success

        user_context.raw_args = "en"
        result = await builtin.execute("language", user_context)
        assert result.success

        # Check status
        user_context.raw_args = None
        result = await builtin.execute("status", user_context)
        assert result.success
        assert "gpt-4" in result.message

        # Send feedback
        user_context.raw_args = "Great bot! I love it!"
        result = await builtin.execute("feedback", user_context)
        assert result.success

        # Stop
        result = await builtin.execute("stop", user_context)
        assert result.success

    @pytest.mark.asyncio
    async def test_admin_management_flow(self, builtin, admin_context):
        """Test admin management flow."""
        builtin.register_all()

        # Check stats
        result = await builtin.execute("stats", admin_context)
        assert result.success

        # Set config
        admin_context.raw_args = "set test 123"
        result = await builtin.execute("config", admin_context)
        assert result.success

        # Get config
        admin_context.raw_args = "get test"
        result = await builtin.execute("config", admin_context)
        assert result.success
        assert "123" in result.message

        # Ban a user
        admin_context.raw_args = "spammer"
        result = await builtin.execute("ban", admin_context)
        assert result.success

        # Enable debug
        admin_context.raw_args = "on"
        result = await builtin.execute("debug", admin_context)
        assert result.success

        # Reload
        admin_context.raw_args = None
        result = await builtin.execute("reload", admin_context)
        assert result.success


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
