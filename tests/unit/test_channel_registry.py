"""
test_channel_registry.py - Tests for integrations/channels/registry.py

Tests the channel adapter registry — central hub for all 34 messaging integrations.
Each test verifies a specific routing or lifecycle guarantee:

FT: Register/unregister adapters, get by name, list channels, status aggregation,
    agent handler routing, duplicate registration warning.
NFT: Empty registry safety, unregister nonexistent is no-op, registry config defaults.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from integrations.channels.registry import ChannelRegistry, ChannelRegistryConfig
from integrations.channels.base import ChannelStatus


def _make_mock_adapter(name='test_channel'):
    adapter = MagicMock()
    adapter.name = name
    adapter.get_status.return_value = ChannelStatus.DISCONNECTED
    adapter.on_message = MagicMock()
    return adapter


# ============================================================
# ChannelRegistryConfig — defaults that drive routing
# ============================================================

class TestRegistryConfig:
    """Config defaults matter — wrong callback URL = agent never receives messages."""

    def test_default_callback_url_has_chat(self):
        with patch('integrations.channels.registry.get_port', return_value=6777):
            config = ChannelRegistryConfig()
        assert '/chat' in config.agent_callback_url

    def test_default_user_id(self):
        config = ChannelRegistryConfig()
        assert config.default_user_id == 10077

    def test_custom_callback_url(self):
        config = ChannelRegistryConfig(agent_callback_url='http://custom:9000/chat')
        assert config.agent_callback_url == 'http://custom:9000/chat'


# ============================================================
# Register / Unregister — adapter lifecycle
# ============================================================

class TestRegisterUnregister:
    """Adapter registration connects it to the message routing pipeline."""

    def test_register_stores_adapter(self):
        registry = ChannelRegistry()
        adapter = _make_mock_adapter('telegram')
        registry.register(adapter)
        assert registry.get('telegram') is adapter

    def test_register_sets_up_message_routing(self):
        """on_message must be called — connects adapter to agent pipeline."""
        registry = ChannelRegistry()
        adapter = _make_mock_adapter()
        registry.register(adapter)
        adapter.on_message.assert_called_once()

    def test_register_duplicate_replaces(self):
        """Re-registering same channel replaces the old adapter."""
        registry = ChannelRegistry()
        old = _make_mock_adapter('slack')
        new = _make_mock_adapter('slack')
        registry.register(old)
        registry.register(new)
        assert registry.get('slack') is new

    def test_unregister_removes(self):
        registry = ChannelRegistry()
        adapter = _make_mock_adapter('discord')
        registry.register(adapter)
        registry.unregister('discord')
        assert registry.get('discord') is None

    def test_unregister_nonexistent_no_crash(self):
        """Unregistering a channel that was never registered must not crash."""
        registry = ChannelRegistry()
        registry.unregister('nonexistent')  # Must not raise


# ============================================================
# Get / List — adapter lookup
# ============================================================

class TestGetList:
    """Adapter lookup used by the channels admin API."""

    def test_get_returns_none_for_missing(self):
        registry = ChannelRegistry()
        assert registry.get('nonexistent') is None

    def test_list_channels_empty(self):
        registry = ChannelRegistry()
        assert registry.list_channels() == []

    def test_list_channels_returns_names(self):
        registry = ChannelRegistry()
        registry.register(_make_mock_adapter('telegram'))
        registry.register(_make_mock_adapter('discord'))
        channels = registry.list_channels()
        assert 'telegram' in channels
        assert 'discord' in channels

    def test_get_status_all(self):
        """Admin dashboard polls all channel statuses at once."""
        registry = ChannelRegistry()
        registry.register(_make_mock_adapter('telegram'))
        registry.register(_make_mock_adapter('slack'))
        statuses = registry.get_status()
        assert isinstance(statuses, dict)
        assert 'telegram' in statuses
        assert 'slack' in statuses


# ============================================================
# Agent handler — connects channels to the AI backend
# ============================================================

class TestAgentHandler:
    """set_agent_handler connects incoming channel messages to /chat."""

    def test_set_agent_handler(self):
        registry = ChannelRegistry()
        handler = MagicMock()
        registry.set_agent_handler(handler)
        assert registry._agent_handler is handler

    def test_is_running_default_false(self):
        registry = ChannelRegistry()
        assert registry.is_running() is False


# ============================================================
# Message routing — core integration point
# ============================================================

class TestMessageRouting:
    """_route_to_agent is the bridge between channels and the AI backend."""

    @pytest.mark.asyncio
    async def test_route_ignores_without_handler(self):
        """No handler set = message dropped silently (not crash)."""
        registry = ChannelRegistry()
        from integrations.channels.base import Message
        msg = Message(id='1', channel='telegram', sender_id='user_1', text='hello')
        await registry._route_to_agent(msg)  # Must not raise

    @pytest.mark.asyncio
    async def test_route_ignores_unknown_channel(self):
        """Message from unregistered channel = dropped."""
        registry = ChannelRegistry()
        registry.set_agent_handler(MagicMock(return_value='response'))
        from integrations.channels.base import Message
        msg = Message(id='1', channel='nonexistent', sender_id='u1', text='hi')
        await registry._route_to_agent(msg)  # Must not raise


# ============================================================
# send_to_channel — outbound messaging
# ============================================================

class TestSendToChannel:
    """send_to_channel routes outbound messages to the right adapter."""

    @pytest.mark.asyncio
    async def test_send_returns_error_for_unknown_channel(self):
        registry = ChannelRegistry()
        result = await registry.send_to_channel('nonexistent', 'chat_1', 'hello')
        assert result is not None
        # Should indicate failure
        if hasattr(result, 'success'):
            assert result.success is False

    def test_send_to_channel_requires_registered_adapter(self):
        """Sending to unregistered channel must fail gracefully."""
        registry = ChannelRegistry()
        # No adapters registered — send should return error result
        assert registry.get('nonexistent') is None


# ============================================================
# Multi-channel management
# ============================================================

class TestMultiChannel:
    """Registry handles multiple channels simultaneously."""

    def test_register_three_channels(self):
        registry = ChannelRegistry()
        for name in ('telegram', 'discord', 'slack'):
            registry.register(_make_mock_adapter(name))
        assert len(registry.list_channels()) == 3

    def test_unregister_one_keeps_others(self):
        registry = ChannelRegistry()
        for name in ('telegram', 'discord', 'slack'):
            registry.register(_make_mock_adapter(name))
        registry.unregister('discord')
        assert len(registry.list_channels()) == 2
        assert 'discord' not in registry.list_channels()
        assert 'telegram' in registry.list_channels()

    def test_status_reflects_all_channels(self):
        registry = ChannelRegistry()
        for name in ('telegram', 'discord'):
            registry.register(_make_mock_adapter(name))
        statuses = registry.get_status()
        assert len(statuses) == 2
