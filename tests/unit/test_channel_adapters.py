"""
Parametrized tests for ALL channel adapters.

Verifies the ChannelAdapter contract across every adapter:
- Importable (module and class exist)
- Has required interface methods (name, connect, disconnect, send_message, etc.)
- Constructor works with mock config (no real connections)
- Initial status is DISCONNECTED
- on_message() registers handlers

Adapters already covered by dedicated test files:
  discord, telegram, signal, web, google_chat, imessage,
  mattermost, nextcloud, serial, gpio, wamp_iot, ros_bridge

This file covers the REMAINING untested adapters:
  slack, whatsapp + 20 extension adapters
AND runs contract tests across ALL adapters for uniform coverage.

Uses unittest.mock — no real connections. --noconftest compatible.
"""

import asyncio
import importlib
import inspect
import os
import sys
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.base import (
    ChannelAdapter,
    ChannelConfig,
    ChannelStatus,
)


def _run(coro):
    """Run async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ─── Adapter registry: (module_path, class_name, mock_deps, config_factory) ───
# mock_deps: dict of sys.modules patches needed so the adapter can import
# config_factory: callable returning a config suitable for the adapter's __init__

def _default_config():
    return ChannelConfig(token="test_token")


def _webhook_config():
    return ChannelConfig(webhook_url="https://example.com/webhook", token="test_token")


def _extra_config(**extra):
    def _factory():
        return ChannelConfig(token="test_token", extra=extra)
    return _factory


# Mock modules needed for various adapters
_AIOHTTP_MOCKS = {
    'aiohttp': MagicMock(),
    'aiohttp.web': MagicMock(),
}

_SLACK_MOCKS = {
    'slack_bolt': MagicMock(),
    'slack_bolt.async_app': MagicMock(),
    'slack_bolt.adapter': MagicMock(),
    'slack_bolt.adapter.socket_mode': MagicMock(),
    'slack_bolt.adapter.socket_mode.async_handler': MagicMock(),
    'slack_sdk': MagicMock(),
    'slack_sdk.web': MagicMock(),
    'slack_sdk.web.async_client': MagicMock(),
    'slack_sdk.errors': MagicMock(),
}

_DISCORD_MOCKS = {
    'discord': MagicMock(Intents=MagicMock(default=MagicMock(return_value=MagicMock()))),
    'discord.ext': MagicMock(),
    'discord.ext.commands': MagicMock(Bot=MagicMock(return_value=MagicMock())),
    'discord.ui': MagicMock(),
}

_TELEGRAM_MOCKS = {
    'telegram': MagicMock(),
    'telegram.ext': MagicMock(),
    'telegram.constants': MagicMock(),
    'telegram.error': MagicMock(),
}

_MATRIX_MOCKS = {
    'nio': MagicMock(),
    'nio.store': MagicMock(),
}

_TEAMS_MOCKS = {
    'botbuilder': MagicMock(),
    'botbuilder.core': MagicMock(),
    'botbuilder.schema': MagicMock(),
    'botbuilder.core.teams': MagicMock(),
}

_LINE_MOCKS = {
    'linebot': MagicMock(),
    'linebot.models': MagicMock(),
    'linebot.exceptions': MagicMock(),
}

_WEBSOCKETS_MOCKS = {
    'websockets': MagicMock(
        connect=AsyncMock(return_value=MagicMock()),
        exceptions=MagicMock(ConnectionClosed=Exception),
    ),
    'websockets.exceptions': MagicMock(ConnectionClosed=Exception),
}

_TWILIO_MOCKS = {
    'twilio': MagicMock(),
    'twilio.rest': MagicMock(),
    'twilio.twiml': MagicMock(),
    'twilio.twiml.voice_response': MagicMock(),
}


# ─── Top-level adapters ───

ADAPTERS = [
    # (id_label, module_path, class_name, mock_deps, config_factory)

    # Top-level adapters
    ("slack",
     "integrations.channels.slack_adapter", "SlackAdapter",
     {**_SLACK_MOCKS, **_AIOHTTP_MOCKS},
     _extra_config(app_token="xapp-test")),

    ("whatsapp",
     "integrations.channels.whatsapp_adapter", "WhatsAppAdapter",
     _AIOHTTP_MOCKS,
     lambda: ChannelConfig(webhook_url="http://localhost:3000", extra={"phone_number": "+1234567890"})),

    # Extension adapters
    ("matrix",
     "integrations.channels.extensions.matrix_adapter", "MatrixAdapter",
     {**_MATRIX_MOCKS, **_AIOHTTP_MOCKS},
     _default_config),

    ("teams",
     "integrations.channels.extensions.teams_adapter", "TeamsAdapter",
     {**_TEAMS_MOCKS, **_AIOHTTP_MOCKS},
     _default_config),

    ("line",
     "integrations.channels.extensions.line_adapter", "LINEAdapter",
     {**_LINE_MOCKS, **_AIOHTTP_MOCKS},
     _default_config),

    ("twitch",
     "integrations.channels.extensions.twitch_adapter", "TwitchAdapter",
     {**_AIOHTTP_MOCKS, **_WEBSOCKETS_MOCKS},
     _default_config),

    ("zalo",
     "integrations.channels.extensions.zalo_adapter", "ZaloAdapter",
     _AIOHTTP_MOCKS,
     _default_config),

    ("nostr",
     "integrations.channels.extensions.nostr_adapter", "NostrAdapter",
     {**_AIOHTTP_MOCKS, **_WEBSOCKETS_MOCKS},
     _default_config),

    ("bluebubbles",
     "integrations.channels.extensions.bluebubbles_adapter", "BlueBubblesAdapter",
     {**_AIOHTTP_MOCKS, 'socketio': MagicMock()},
     _default_config),

    ("voice",
     "integrations.channels.extensions.voice_adapter", "VoiceAdapter",
     {**_AIOHTTP_MOCKS, **_TWILIO_MOCKS},
     _default_config),

    ("rocketchat",
     "integrations.channels.extensions.rocketchat_adapter", "RocketChatAdapter",
     {**_AIOHTTP_MOCKS, **_WEBSOCKETS_MOCKS},
     _default_config),

    ("wechat",
     "integrations.channels.extensions.wechat_adapter", "WeChatAdapter",
     _AIOHTTP_MOCKS,
     _default_config),

    ("viber",
     "integrations.channels.extensions.viber_adapter", "ViberAdapter",
     _AIOHTTP_MOCKS,
     _default_config),

    ("messenger",
     "integrations.channels.extensions.messenger_adapter", "MessengerAdapter",
     _AIOHTTP_MOCKS,
     _default_config),

    ("instagram",
     "integrations.channels.extensions.instagram_adapter", "InstagramAdapter",
     _AIOHTTP_MOCKS,
     _default_config),

    ("twitter",
     "integrations.channels.extensions.twitter_adapter", "TwitterAdapter",
     _AIOHTTP_MOCKS,
     _default_config),

    ("email",
     "integrations.channels.extensions.email_adapter", "EmailAdapter",
     _AIOHTTP_MOCKS,
     _default_config),

    ("tlon",
     "integrations.channels.extensions.tlon_adapter", "TlonAdapter",
     {**_AIOHTTP_MOCKS, **_WEBSOCKETS_MOCKS},
     _default_config),

    ("openprose",
     "integrations.channels.extensions.openprose_adapter", "OpenProseAdapter",
     {**_AIOHTTP_MOCKS, **_WEBSOCKETS_MOCKS},
     _default_config),

    ("telegram_user",
     "integrations.channels.extensions.telegram_user_adapter", "TelegramUserAdapter",
     {**_TELEGRAM_MOCKS, **_AIOHTTP_MOCKS,
      'telethon': MagicMock(), 'telethon.sessions': MagicMock(),
      'telethon.tl': MagicMock(), 'telethon.tl.types': MagicMock(),
      'telethon.events': MagicMock()},
     _default_config),

    ("discord_user",
     "integrations.channels.extensions.discord_user_adapter", "DiscordUserAdapter",
     {**_DISCORD_MOCKS, **_AIOHTTP_MOCKS},
     _default_config),

    ("zalo_user",
     "integrations.channels.extensions.zalo_user_adapter", "ZaloUserAdapter",
     _AIOHTTP_MOCKS,
     _default_config),
]


def _get_adapter_ids():
    return [a[0] for a in ADAPTERS]


def _import_adapter(module_path, class_name, mock_deps):
    """Import adapter class with mocked dependencies."""
    # Invalidate cached module to ensure fresh import with mocks
    for key in list(sys.modules.keys()):
        if key.startswith(module_path):
            del sys.modules[key]

    with patch.dict('sys.modules', mock_deps):
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
    return cls


def _try_import_adapter(module_path, class_name, mock_deps):
    """Try to import an adapter, return (cls, error_msg)."""
    try:
        cls = _import_adapter(module_path, class_name, mock_deps)
        return cls, None
    except Exception as e:
        return None, f"Failed to import {module_path}.{class_name}: {e}"


def _construct_adapter(cls, config, mock_deps, module_path):
    """Construct an adapter, falling back to custom Config if needed.

    Returns adapter instance or raises on failure.
    """
    with patch.dict('sys.modules', mock_deps):
        try:
            return cls(config)
        except (TypeError, AttributeError, ImportError):
            # Try with adapter's custom Config subclass
            module = importlib.import_module(module_path)
            config_cls = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (isinstance(attr, type) and
                    issubclass(attr, ChannelConfig) and
                    attr is not ChannelConfig):
                    config_cls = attr
                    break
            if config_cls:
                return cls(config_cls())
            raise


# ═══════════════════════════════════════════════════════════════
# Contract Tests — parametrized across ALL untested adapters
# ═══════════════════════════════════════════════════════════════

class TestAdapterImportable:
    """Test that every adapter module and class is importable."""

    @pytest.mark.parametrize(
        "adapter_id, module_path, class_name, mock_deps, config_factory",
        ADAPTERS,
        ids=_get_adapter_ids(),
    )
    def test_importable(self, adapter_id, module_path, class_name, mock_deps, config_factory):
        """Adapter module is importable and class exists."""
        cls, error = _try_import_adapter(module_path, class_name, mock_deps)
        assert cls is not None, error


class TestAdapterIsChannelAdapter:
    """Test that every adapter is a subclass of ChannelAdapter."""

    @pytest.mark.parametrize(
        "adapter_id, module_path, class_name, mock_deps, config_factory",
        ADAPTERS,
        ids=_get_adapter_ids(),
    )
    def test_is_channel_adapter(self, adapter_id, module_path, class_name, mock_deps, config_factory):
        """Adapter class extends ChannelAdapter."""
        cls, error = _try_import_adapter(module_path, class_name, mock_deps)
        if cls is None:
            pytest.skip(error)
        assert issubclass(cls, ChannelAdapter), (
            f"{class_name} does not extend ChannelAdapter"
        )


class TestAdapterHasInterface:
    """Test that every adapter has required interface methods."""

    REQUIRED_METHODS = [
        'connect', 'disconnect', 'send_message',
        'edit_message', 'delete_message', 'send_typing',
        'get_chat_info', 'on_message',
    ]

    @pytest.mark.parametrize(
        "adapter_id, module_path, class_name, mock_deps, config_factory",
        ADAPTERS,
        ids=_get_adapter_ids(),
    )
    def test_has_required_methods(self, adapter_id, module_path, class_name, mock_deps, config_factory):
        """Adapter has all ChannelAdapter abstract methods."""
        cls, error = _try_import_adapter(module_path, class_name, mock_deps)
        if cls is None:
            pytest.skip(error)

        for method_name in self.REQUIRED_METHODS:
            assert hasattr(cls, method_name), (
                f"{class_name} missing method: {method_name}"
            )

    @pytest.mark.parametrize(
        "adapter_id, module_path, class_name, mock_deps, config_factory",
        ADAPTERS,
        ids=_get_adapter_ids(),
    )
    def test_has_name_property(self, adapter_id, module_path, class_name, mock_deps, config_factory):
        """Adapter has 'name' property."""
        cls, error = _try_import_adapter(module_path, class_name, mock_deps)
        if cls is None:
            pytest.skip(error)

        # Check 'name' is either a property or attribute
        assert hasattr(cls, 'name'), f"{class_name} missing 'name' property"


class TestAdapterConstructor:
    """Test that every adapter constructor works with mock config."""

    @pytest.mark.parametrize(
        "adapter_id, module_path, class_name, mock_deps, config_factory",
        ADAPTERS,
        ids=_get_adapter_ids(),
    )
    def test_constructor_doesnt_crash(self, adapter_id, module_path, class_name, mock_deps, config_factory):
        """Adapter constructor accepts config without crashing."""
        cls, error = _try_import_adapter(module_path, class_name, mock_deps)
        if cls is None:
            pytest.skip(error)

        config = config_factory()
        adapter = _construct_adapter(cls, config, mock_deps, module_path)
        assert adapter is not None


class TestAdapterInitialStatus:
    """Test that every adapter starts in DISCONNECTED status."""

    @pytest.mark.parametrize(
        "adapter_id, module_path, class_name, mock_deps, config_factory",
        ADAPTERS,
        ids=_get_adapter_ids(),
    )
    def test_initial_status_disconnected(self, adapter_id, module_path, class_name, mock_deps, config_factory):
        """Adapter starts in DISCONNECTED status."""
        cls, error = _try_import_adapter(module_path, class_name, mock_deps)
        if cls is None:
            pytest.skip(error)

        config = config_factory()
        adapter = _construct_adapter(cls, config, mock_deps, module_path)
        assert adapter.status == ChannelStatus.DISCONNECTED


class TestAdapterName:
    """Test that every adapter has a non-empty name string."""

    @pytest.mark.parametrize(
        "adapter_id, module_path, class_name, mock_deps, config_factory",
        ADAPTERS,
        ids=_get_adapter_ids(),
    )
    def test_name_is_nonempty_string(self, adapter_id, module_path, class_name, mock_deps, config_factory):
        """Adapter name property returns a non-empty string."""
        cls, error = _try_import_adapter(module_path, class_name, mock_deps)
        if cls is None:
            pytest.skip(error)

        config = config_factory()
        adapter = _construct_adapter(cls, config, mock_deps, module_path)
        assert isinstance(adapter.name, str), f"{class_name}.name is not a string"
        assert len(adapter.name) > 0, f"{class_name}.name is empty"


class TestAdapterOnMessage:
    """Test that on_message() registers handlers correctly."""

    @pytest.mark.parametrize(
        "adapter_id, module_path, class_name, mock_deps, config_factory",
        ADAPTERS,
        ids=_get_adapter_ids(),
    )
    def test_on_message_registers_handler(self, adapter_id, module_path, class_name, mock_deps, config_factory):
        """on_message() adds handler to _message_handlers list."""
        cls, error = _try_import_adapter(module_path, class_name, mock_deps)
        if cls is None:
            pytest.skip(error)

        config = config_factory()
        adapter = _construct_adapter(cls, config, mock_deps, module_path)

        async def dummy_handler(msg):
            pass

        adapter.on_message(dummy_handler)
        assert dummy_handler in adapter._message_handlers, (
            f"{class_name}.on_message() did not register handler"
        )


# ═══════════════════════════════════════════════════════════════
# Specific adapter tests for previously untested adapters
# ═══════════════════════════════════════════════════════════════

class TestSlackAdapterSpecific:
    """Specific tests for SlackAdapter."""

    def test_slack_requires_sdk(self):
        """SlackAdapter raises ImportError without slack-bolt."""
        with patch.dict('sys.modules', {'slack_bolt': None, 'slack_bolt.async_app': None}):
            # Clear cached module
            for key in list(sys.modules.keys()):
                if 'slack_adapter' in key:
                    del sys.modules[key]

            try:
                from integrations.channels.slack_adapter import SlackAdapter
                config = ChannelConfig(token="test", extra={"app_token": "xapp-test"})
                with pytest.raises(ImportError):
                    SlackAdapter(config)
            except ImportError:
                # Module-level import fails — that's also acceptable
                pass

    def test_slack_name(self):
        """SlackAdapter.name returns 'slack'."""
        cls, error = _try_import_adapter(
            "integrations.channels.slack_adapter", "SlackAdapter",
            {**_SLACK_MOCKS, **_AIOHTTP_MOCKS}
        )
        if cls is None:
            pytest.skip(error)
        with patch.dict('sys.modules', {**_SLACK_MOCKS, **_AIOHTTP_MOCKS}):
            config = ChannelConfig(token="xoxb-test", extra={"app_token": "xapp-test"})
            adapter = cls(config)
            assert adapter.name == "slack"


class TestWhatsAppAdapterSpecific:
    """Specific tests for WhatsAppAdapter."""

    def test_whatsapp_name(self):
        """WhatsAppAdapter.name returns 'whatsapp'."""
        cls, error = _try_import_adapter(
            "integrations.channels.whatsapp_adapter", "WhatsAppAdapter",
            _AIOHTTP_MOCKS
        )
        if cls is None:
            pytest.skip(error)
        with patch.dict('sys.modules', _AIOHTTP_MOCKS):
            config = ChannelConfig(
                webhook_url="http://localhost:3000",
                extra={"phone_number": "+1234567890"}
            )
            adapter = cls(config)
            assert adapter.name == "whatsapp"

    def test_whatsapp_stores_phone_number(self):
        """WhatsAppAdapter stores phone number from config."""
        cls, error = _try_import_adapter(
            "integrations.channels.whatsapp_adapter", "WhatsAppAdapter",
            _AIOHTTP_MOCKS
        )
        if cls is None:
            pytest.skip(error)
        with patch.dict('sys.modules', _AIOHTTP_MOCKS):
            config = ChannelConfig(
                webhook_url="http://localhost:3000",
                extra={"phone_number": "+9876543210"}
            )
            adapter = cls(config)
            assert adapter._phone_number == "+9876543210"

    def test_whatsapp_default_base_url(self):
        """WhatsAppAdapter uses webhook_url as base URL."""
        cls, error = _try_import_adapter(
            "integrations.channels.whatsapp_adapter", "WhatsAppAdapter",
            _AIOHTTP_MOCKS
        )
        if cls is None:
            pytest.skip(error)
        with patch.dict('sys.modules', _AIOHTTP_MOCKS):
            config = ChannelConfig(
                webhook_url="http://custom:5000",
                extra={"phone_number": "+1234567890"}
            )
            adapter = cls(config)
            assert adapter._base_url == "http://custom:5000"


# ═══════════════════════════════════════════════════════════════
# Registry integration — all adapters can be registered
# ═══════════════════════════════════════════════════════════════

class TestRegistryIntegration:
    """Test that adapters work with ChannelRegistry."""

    def test_registry_accepts_mock_adapter(self):
        """Registry.register() accepts any ChannelAdapter."""
        from integrations.channels.registry import ChannelRegistry

        registry = ChannelRegistry()

        mock_adapter = MagicMock(spec=ChannelAdapter)
        mock_adapter.name = "test_adapter"
        mock_adapter.on_message = MagicMock()
        mock_adapter.get_status.return_value = ChannelStatus.DISCONNECTED

        registry.register(mock_adapter)
        assert "test_adapter" in registry.list_channels()

    def test_registry_handles_multiple_adapters(self):
        """Registry handles many adapters without conflict."""
        from integrations.channels.registry import ChannelRegistry

        registry = ChannelRegistry()

        for i in range(10):
            mock_adapter = MagicMock(spec=ChannelAdapter)
            mock_adapter.name = f"adapter_{i}"
            mock_adapter.on_message = MagicMock()
            mock_adapter.get_status.return_value = ChannelStatus.DISCONNECTED
            registry.register(mock_adapter)

        assert len(registry.list_channels()) == 10


# ═══════════════════════════════════════════════════════════════
# Base class contract — ChannelAdapter ABC
# ═══════════════════════════════════════════════════════════════

class TestBaseChannelAdapterContract:
    """Verify ChannelAdapter ABC defines the expected interface."""

    def test_is_abstract(self):
        """ChannelAdapter cannot be instantiated directly."""
        with pytest.raises(TypeError):
            ChannelAdapter(ChannelConfig())

    def test_abstract_methods(self):
        """ChannelAdapter defines expected abstract methods."""
        abstract = set()
        for name, method in inspect.getmembers(ChannelAdapter):
            if getattr(method, '__isabstractmethod__', False):
                abstract.add(name)

        expected = {
            'name', 'connect', 'disconnect', 'send_message',
            'edit_message', 'delete_message', 'send_typing',
            'get_chat_info',
        }
        assert expected.issubset(abstract), (
            f"Missing abstract methods: {expected - abstract}"
        )

    def test_concrete_methods(self):
        """ChannelAdapter provides concrete helper methods."""
        assert hasattr(ChannelAdapter, 'on_message')
        assert hasattr(ChannelAdapter, 'get_status')
        assert hasattr(ChannelAdapter, 'start')
        assert hasattr(ChannelAdapter, 'stop')
        assert hasattr(ChannelAdapter, 'is_running')
        assert hasattr(ChannelAdapter, '_dispatch_message')


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--noconftest"])
