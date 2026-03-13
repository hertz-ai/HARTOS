"""
End-to-End Regression Test Suite for HevolveBot Channel Integration

Run with: python -m pytest integrations/channels/tests/test_e2e_regression.py -v

This comprehensive test suite verifies:
1. All module imports work correctly
2. Core APIs function correctly
3. Queue pipeline works end-to-end
4. Commands system works correctly
5. Response system works correctly
6. Automation system works correctly
7. Identity system works correctly
8. Memory system works correctly
9. Gateway protocol works correctly
10. Admin API works correctly
"""

import pytest
import sys
from datetime import datetime
from typing import Dict, Any, List


# ============================================================
# HELPER: Check if module/class exists
# ============================================================

def module_available(module_name: str) -> bool:
    """Check if a module can be imported."""
    try:
        __import__(module_name)
        return True
    except ImportError:
        return False


def class_in_module(module_name: str, class_name: str) -> bool:
    """Check if a class exists in a module."""
    try:
        mod = __import__(module_name, fromlist=[class_name])
        return hasattr(mod, class_name)
    except ImportError:
        return False


# ============================================================
# SECTION 1: MODULE IMPORT TESTS
# ============================================================

class TestModuleImports:
    """Test all module imports work correctly."""

    def test_base_module(self):
        """Test base module imports."""
        from integrations.channels.base import (
            ChannelAdapter,
            ChannelConfig,
            Message,
            MessageType,
            ChannelStatus,
            SendResult,
            MediaAttachment,
            ChannelConnectionError,
            ChannelSendError,
            ChannelRateLimitError,
        )
        assert ChannelAdapter is not None
        assert ChannelConfig is not None
        assert Message is not None

    def test_registry_module(self):
        """Test registry module imports."""
        from integrations.channels.registry import ChannelRegistry
        assert ChannelRegistry is not None

    def test_security_module(self):
        """Test security module imports."""
        from integrations.channels.security import (
            PairingManager,
            PairingCode,
            PairingStatus,
        )
        assert PairingManager is not None

    def test_session_manager_module(self):
        """Test session manager module imports."""
        from integrations.channels.session_manager import (
            ChannelSessionManager,
            ChannelSession,
        )
        assert ChannelSessionManager is not None
        assert ChannelSession is not None

    def test_flask_integration_module(self):
        """Test Flask integration module imports."""
        from integrations.channels.flask_integration import (
            FlaskChannelIntegration,
            init_channels,
        )
        assert FlaskChannelIntegration is not None
        assert init_channels is not None


class TestQueueModuleImports:
    """Test queue module imports."""

    def test_message_queue(self):
        from integrations.channels.queue.message_queue import MessageQueue
        assert MessageQueue is not None

    def test_rate_limit(self):
        from integrations.channels.queue.rate_limit import RateLimiter, RateLimitConfig
        assert RateLimiter is not None
        assert RateLimitConfig is not None

    def test_retry(self):
        from integrations.channels.queue.retry import RetryHandler, RetryConfig
        assert RetryHandler is not None
        assert RetryConfig is not None

    def test_concurrency(self):
        from integrations.channels.queue.concurrency import (
            ConcurrencyController,
            ConcurrencyLimits,
        )
        assert ConcurrencyController is not None
        assert ConcurrencyLimits is not None

    def test_debounce(self):
        from integrations.channels.queue.debounce import InboundDebouncer
        assert InboundDebouncer is not None

    def test_dedupe(self):
        from integrations.channels.queue.dedupe import MessageDeduplicator
        assert MessageDeduplicator is not None

    def test_batching(self):
        from integrations.channels.queue.batching import MessageBatcher
        assert MessageBatcher is not None

    def test_pipeline(self):
        from integrations.channels.queue.pipeline import MessagePipeline
        assert MessagePipeline is not None


class TestCommandsModuleImports:
    """Test commands module imports."""

    def test_registry(self):
        from integrations.channels.commands.registry import (
            CommandRegistry,
            CommandDefinition,
        )
        assert CommandRegistry is not None
        assert CommandDefinition is not None

    def test_detection(self):
        from integrations.channels.commands.detection import CommandDetector
        assert CommandDetector is not None

    def test_arguments(self):
        from integrations.channels.commands.arguments import ArgumentParser
        assert ArgumentParser is not None

    def test_mention_gating(self):
        from integrations.channels.commands.mention_gating import MentionGate
        assert MentionGate is not None

    def test_builtin(self):
        from integrations.channels.commands.builtin import BuiltinCommands
        assert BuiltinCommands is not None


class TestResponseModuleImports:
    """Test response module imports."""

    def test_typing(self):
        from integrations.channels.response.typing import TypingManager
        assert TypingManager is not None

    def test_reactions(self):
        from integrations.channels.response.reactions import AckManager
        assert AckManager is not None

    def test_templates(self):
        from integrations.channels.response.templates import TemplateEngine
        assert TemplateEngine is not None

    def test_streaming(self):
        from integrations.channels.response.streaming import StreamingResponse
        assert StreamingResponse is not None


class TestMediaModuleImports:
    """Test media module imports."""

    def test_vision(self):
        from integrations.channels.media.vision import VisionProcessor
        assert VisionProcessor is not None

    def test_audio(self):
        from integrations.channels.media.audio import AudioProcessor
        assert AudioProcessor is not None

    def test_tts(self):
        from integrations.channels.media.tts import TTSEngine
        assert TTSEngine is not None

    def test_image_gen(self):
        from integrations.channels.media.image_gen import ImageGenerator
        assert ImageGenerator is not None

    def test_links(self):
        from integrations.channels.media.links import LinkProcessor
        assert LinkProcessor is not None

    def test_files(self):
        from integrations.channels.media.files import FileManager
        assert FileManager is not None

    def test_limits(self):
        from integrations.channels.media.limits import MediaLimits
        assert MediaLimits is not None


class TestAutomationModuleImports:
    """Test automation module imports."""

    def test_webhooks(self):
        from integrations.channels.automation.webhooks import WebhookManager
        assert WebhookManager is not None

    def test_cron(self):
        from integrations.channels.automation.cron import CronManager
        assert CronManager is not None

    def test_triggers(self):
        from integrations.channels.automation.triggers import TriggerManager
        assert TriggerManager is not None

    def test_workflows(self):
        from integrations.channels.automation.workflows import WorkflowEngine
        assert WorkflowEngine is not None

    def test_scheduled_messages(self):
        from integrations.channels.automation.scheduled_messages import (
            ScheduledMessageManager,
        )
        assert ScheduledMessageManager is not None


class TestIdentityModuleImports:
    """Test identity module imports."""

    def test_agent_identity(self):
        from integrations.channels.identity.agent_identity import AgentIdentity
        assert AgentIdentity is not None

    def test_avatars(self):
        from integrations.channels.identity.avatars import AvatarManager
        assert AvatarManager is not None

    def test_sender_mapping(self):
        from integrations.channels.identity.sender_mapping import SenderIdentityMapper
        assert SenderIdentityMapper is not None

    def test_preferences(self):
        from integrations.channels.identity.preferences import UserPreferences
        assert UserPreferences is not None


class TestMemoryModuleImports:
    """Test memory module imports."""

    def test_memory_store(self):
        from integrations.channels.memory.memory_store import MemoryStore
        assert MemoryStore is not None

    def test_file_tracker(self):
        from integrations.channels.memory.file_tracker import FileTracker
        assert FileTracker is not None

    def test_embeddings(self):
        from integrations.channels.memory.embeddings import EmbeddingCache, EmbeddingConfig
        assert EmbeddingCache is not None
        assert EmbeddingConfig is not None

    def test_search(self):
        from integrations.channels.memory.search import MemorySearch
        assert MemorySearch is not None


class TestGatewayModuleImports:
    """Test gateway module imports."""

    def test_protocol(self):
        from integrations.channels.gateway.protocol import GatewayProtocol
        assert GatewayProtocol is not None


class TestAdminModuleImports:
    """Test admin module imports."""

    def test_admin_api(self):
        from integrations.channels.admin.api import AdminAPI, admin_bp
        assert AdminAPI is not None
        assert admin_bp is not None


# ============================================================
# SECTION 2: QUEUE PIPELINE TESTS
# ============================================================

class TestQueuePipeline:
    """Test queue pipeline functionality."""

    def test_rate_limiter_config(self):
        """Test rate limiter configuration."""
        from integrations.channels.queue.rate_limit import RateLimitConfig

        config = RateLimitConfig(
            requests_per_minute=60,
            requests_per_hour=1000,
            burst_limit=10,
        )
        assert config.requests_per_minute == 60
        assert config.requests_per_hour == 1000
        assert config.burst_limit == 10

    def test_retry_config(self):
        """Test retry configuration."""
        from integrations.channels.queue.retry import RetryConfig

        config = RetryConfig(
            max_retries=3,
            initial_delay_ms=1000,
            max_delay_ms=30000,
        )
        assert config.max_retries == 3
        assert config.initial_delay_ms == 1000
        assert config.max_delay_ms == 30000

    def test_concurrency_limits(self):
        """Test concurrency limits configuration."""
        from integrations.channels.queue.concurrency import ConcurrencyLimits

        limits = ConcurrencyLimits(
            max_global=100,
            max_per_channel=20,
            max_per_chat=2,
            max_per_user=4,
        )
        assert limits.max_global == 100
        assert limits.max_per_channel == 20
        assert limits.max_per_chat == 2
        assert limits.max_per_user == 4

    def test_message_deduplicator(self):
        """Test message deduplicator."""
        from integrations.channels.queue.dedupe import MessageDeduplicator, DedupeConfig

        config = DedupeConfig()
        deduper = MessageDeduplicator(config=config)
        assert deduper is not None

    def test_debouncer(self):
        """Test debouncer."""
        from integrations.channels.queue.debounce import InboundDebouncer, DebounceConfig

        config = DebounceConfig()
        debouncer = InboundDebouncer(config=config)
        assert debouncer is not None


# ============================================================
# SECTION 3: COMMANDS SYSTEM TESTS
# ============================================================

class TestCommandsSystem:
    """Test commands system functionality."""

    def test_command_definition(self):
        """Test command definition uses 'key' field."""
        from integrations.channels.commands.registry import CommandDefinition

        cmd = CommandDefinition(
            key="help",
            description="Show help message",
            aliases=["h", "?"],
        )
        assert cmd.key == "help"
        assert cmd.description == "Show help message"
        assert "h" in cmd.aliases

    def test_command_registry(self):
        """Test command registry."""
        from integrations.channels.commands.registry import (
            CommandRegistry,
            CommandDefinition,
        )

        registry = CommandRegistry()
        cmd = CommandDefinition(key="test", description="Test command")
        registry.register(cmd)
        assert registry.get("test") is not None

    def test_command_detector(self):
        """Test command detector."""
        from integrations.channels.commands.detection import CommandDetector

        detector = CommandDetector()
        assert detector is not None

    def test_argument_parser(self):
        """Test argument parser."""
        from integrations.channels.commands.arguments import ArgumentParser

        parser = ArgumentParser()
        assert parser is not None


# ============================================================
# SECTION 4: RESPONSE SYSTEM TESTS
# ============================================================

class TestResponseSystem:
    """Test response system functionality."""

    def test_template_engine(self):
        """Test template engine uses register_template method."""
        from integrations.channels.response.templates import TemplateEngine

        engine = TemplateEngine()
        # Check if register_template method exists
        assert hasattr(engine, 'register_template')

    def test_typing_manager(self):
        """Test typing manager."""
        from integrations.channels.response.typing import TypingManager

        manager = TypingManager()
        assert manager is not None

    def test_ack_manager(self):
        """Test acknowledgment manager."""
        from integrations.channels.response.reactions import AckManager

        manager = AckManager()
        assert manager is not None


# ============================================================
# SECTION 5: IDENTITY SYSTEM TESTS
# ============================================================

class TestIdentitySystem:
    """Test identity system functionality."""

    def test_agent_identity_structure(self):
        """Test AgentIdentity uses 'name' field and personality as Dict."""
        from integrations.channels.identity.agent_identity import AgentIdentity

        identity = AgentIdentity(
            name="TestBot",
            description="A test bot",
            personality={"tone": "friendly", "style": "casual"},
        )
        assert identity.name == "TestBot"
        assert identity.description == "A test bot"
        assert isinstance(identity.personality, dict)
        assert identity.personality["tone"] == "friendly"

    def test_sender_mapper(self):
        """Test sender mapper."""
        from integrations.channels.identity.sender_mapping import SenderIdentityMapper

        mapper = SenderIdentityMapper()
        assert mapper is not None


# ============================================================
# SECTION 6: AUTOMATION SYSTEM TESTS
# ============================================================

class TestAutomationSystem:
    """Test automation system functionality."""

    def test_trigger_manager(self):
        """Test trigger manager."""
        from integrations.channels.automation.triggers import TriggerManager

        manager = TriggerManager()
        assert manager is not None
        # Check register method exists
        assert hasattr(manager, 'register')

    def test_webhook_manager(self):
        """Test webhook manager."""
        from integrations.channels.automation.webhooks import WebhookManager

        manager = WebhookManager()
        assert manager is not None

    def test_cron_manager(self):
        """Test cron manager."""
        from integrations.channels.automation.cron import CronManager

        manager = CronManager()
        assert manager is not None


# ============================================================
# SECTION 7: MEMORY SYSTEM TESTS
# ============================================================

class TestMemorySystem:
    """Test memory system functionality."""

    def test_memory_store(self):
        """Test memory store."""
        from integrations.channels.memory.memory_store import MemoryStore

        store = MemoryStore()
        assert store is not None

    def test_file_tracker(self):
        """Test file tracker."""
        from integrations.channels.memory.file_tracker import FileTracker

        tracker = FileTracker()
        assert tracker is not None


# ============================================================
# SECTION 8: GATEWAY PROTOCOL TESTS
# ============================================================

class TestGatewayProtocol:
    """Test gateway protocol functionality."""

    def test_protocol_init(self):
        """Test gateway protocol initialization."""
        from integrations.channels.gateway.protocol import GatewayProtocol

        protocol = GatewayProtocol()
        assert protocol is not None

    def test_protocol_register_method(self):
        """Test registering a method."""
        from integrations.channels.gateway.protocol import GatewayProtocol

        protocol = GatewayProtocol()

        def test_handler(params):
            return {"result": "ok"}

        protocol.register_method("test.method", test_handler)
        methods = protocol.get_methods()
        assert "test.method" in methods

    @pytest.mark.asyncio
    async def test_protocol_handle_request(self):
        """Test handling a request."""
        from integrations.channels.gateway.protocol import GatewayProtocol

        protocol = GatewayProtocol()

        def echo_handler(params):
            return params

        protocol.register_method("echo", echo_handler)

        request = '{"jsonrpc": "2.0", "method": "echo", "params": {"msg": "hello"}, "id": 1}'
        response = await protocol.handle_request(request)

        assert response is not None


# ============================================================
# SECTION 9: ADMIN API TESTS
# ============================================================

class TestAdminAPI:
    """Test admin API functionality."""

    def test_admin_api_exists(self):
        """Test AdminAPI class exists."""
        from integrations.channels.admin.api import AdminAPI
        assert AdminAPI is not None

    def test_admin_blueprint_exists(self):
        """Test admin blueprint exists."""
        from integrations.channels.admin.api import admin_bp
        assert admin_bp is not None


# ============================================================
# SECTION 10: FLASK INTEGRATION TESTS
# ============================================================

class TestFlaskIntegration:
    """Test Flask integration functionality."""

    def test_flask_channel_integration(self):
        """Test FlaskChannelIntegration can be instantiated."""
        from integrations.channels.flask_integration import FlaskChannelIntegration

        integration = FlaskChannelIntegration()
        assert integration is not None

    def test_init_channels_function(self):
        """Test init_channels function exists and is callable."""
        from integrations.channels.flask_integration import init_channels

        assert callable(init_channels)


# ============================================================
# SECTION 11: ADAPTER IMPORT TESTS
# ============================================================

class TestAdapterImports:
    """Test that adapters can be imported."""

    def test_telegram_adapter_imports(self):
        """Test Telegram adapter can be imported."""
        from integrations.channels.telegram_adapter import TelegramAdapter
        assert TelegramAdapter is not None

    def test_discord_adapter_imports(self):
        """Test Discord adapter can be imported."""
        from integrations.channels.discord_adapter import DiscordAdapter
        assert DiscordAdapter is not None

    def test_slack_adapter_imports(self):
        """Test Slack adapter can be imported."""
        from integrations.channels.slack_adapter import SlackAdapter
        assert SlackAdapter is not None

    def test_whatsapp_adapter_imports(self):
        """Test WhatsApp adapter can be imported."""
        from integrations.channels.whatsapp_adapter import WhatsAppAdapter
        assert WhatsAppAdapter is not None

    def test_web_adapter_imports(self):
        """Test Web adapter can be imported."""
        from integrations.channels.web_adapter import WebAdapter
        assert WebAdapter is not None

    def test_signal_adapter_imports(self):
        """Test Signal adapter can be imported."""
        from integrations.channels.signal_adapter import SignalAdapter
        assert SignalAdapter is not None

    def test_imessage_adapter_imports(self):
        """Test iMessage adapter can be imported."""
        from integrations.channels.imessage_adapter import IMessageAdapter
        assert IMessageAdapter is not None

    def test_google_chat_adapter_imports(self):
        """Test Google Chat adapter can be imported."""
        from integrations.channels.google_chat_adapter import GoogleChatAdapter
        assert GoogleChatAdapter is not None


# ============================================================
# SECTION 12: DATACLASS FIELD VERIFICATION TESTS
# ============================================================

class TestDataclassFields:
    """Test that dataclasses have correct field names."""

    def test_rate_limit_config_fields(self):
        """Test RateLimitConfig has correct fields."""
        from integrations.channels.queue.rate_limit import RateLimitConfig
        import dataclasses

        fields = {f.name for f in dataclasses.fields(RateLimitConfig)}
        assert "requests_per_minute" in fields
        assert "requests_per_hour" in fields
        assert "burst_limit" in fields

    def test_retry_config_fields(self):
        """Test RetryConfig has correct fields."""
        from integrations.channels.queue.retry import RetryConfig
        import dataclasses

        fields = {f.name for f in dataclasses.fields(RetryConfig)}
        assert "max_retries" in fields
        assert "initial_delay_ms" in fields
        assert "max_delay_ms" in fields

    def test_concurrency_limits_fields(self):
        """Test ConcurrencyLimits has correct fields."""
        from integrations.channels.queue.concurrency import ConcurrencyLimits
        import dataclasses

        fields = {f.name for f in dataclasses.fields(ConcurrencyLimits)}
        assert "max_global" in fields
        assert "max_per_channel" in fields
        assert "max_per_chat" in fields
        assert "max_per_user" in fields

    def test_command_definition_fields(self):
        """Test CommandDefinition uses 'key' field."""
        from integrations.channels.commands.registry import CommandDefinition
        import dataclasses

        fields = {f.name for f in dataclasses.fields(CommandDefinition)}
        assert "key" in fields
        assert "description" in fields
        assert "aliases" in fields

    def test_agent_identity_fields(self):
        """Test AgentIdentity has correct fields."""
        from integrations.channels.identity.agent_identity import AgentIdentity
        import dataclasses

        fields = {f.name for f in dataclasses.fields(AgentIdentity)}
        assert "name" in fields
        assert "description" in fields
        assert "personality" in fields


# ============================================================
# SECTION 13: SIMPLEMEM INTEGRATION TESTS
# ============================================================

class TestSimpleMemIntegration:
    """Test SimpleMem memory integration."""

    def test_simplemem_config_importable(self):
        """Test SimpleMemConfig can be imported regardless of simplemem package."""
        from integrations.channels.memory.simplemem_store import SimpleMemConfig
        config = SimpleMemConfig()
        assert config.model == "gpt-4.1-mini"
        assert config.embedding_model == "Qwen/Qwen3-Embedding-0.6B"
        assert config.window_size == 40
        assert config.db_path == "./simplemem_db"
        assert config.enabled is True

    def test_simplemem_config_from_env(self):
        """Test SimpleMemConfig.from_env() works."""
        from integrations.channels.memory.simplemem_store import SimpleMemConfig
        config = SimpleMemConfig.from_env()
        assert isinstance(config.enabled, bool)
        assert isinstance(config.model, str)
        assert isinstance(config.window_size, int)
        assert isinstance(config.parallel_workers, int)

    def test_simplemem_has_simplemem_flag(self):
        """Test HAS_SIMPLEMEM flag is accessible."""
        from integrations.channels.memory import HAS_SIMPLEMEM
        assert isinstance(HAS_SIMPLEMEM, bool)

    def test_simplemem_config_fields(self):
        """Test SimpleMemConfig has all expected fields."""
        import dataclasses
        from integrations.channels.memory.simplemem_store import SimpleMemConfig
        fields = {f.name for f in dataclasses.fields(SimpleMemConfig)}
        expected = {
            "enabled", "api_key", "base_url", "model", "embedding_model",
            "db_path", "window_size", "overlap_size", "parallel_workers",
            "retrieval_workers", "auto_finalize_interval",
        }
        assert expected.issubset(fields), f"Missing fields: {expected - fields}"

    def test_simplemem_store_requires_package(self):
        """Test SimpleMemStore raises ImportError when simplemem not installed."""
        from integrations.channels.memory.simplemem_store import (
            SimpleMemStore, SimpleMemConfig, HAS_SIMPLEMEM
        )
        if not HAS_SIMPLEMEM:
            with pytest.raises(ImportError, match="simplemem is required"):
                SimpleMemStore(SimpleMemConfig())

    def test_simplemem_memory_source_interface(self):
        """Test SimpleMemStore implements MemorySource interface."""
        from integrations.channels.memory.search import MemorySource
        from integrations.channels.memory.simplemem_store import SimpleMemStore
        assert issubclass(SimpleMemStore, MemorySource)

    def test_memory_search_simplemem_parameter(self):
        """Test MemorySearch accepts enable_simplemem parameter."""
        from integrations.channels.memory.search import MemorySearch
        ms = MemorySearch(enable_simplemem=False)
        assert "simplemem" not in ms.get_sources()

    def test_simplemem_exported_from_memory_module(self):
        """Test SimpleMem classes are listed in __all__."""
        import integrations.channels.memory as mem
        assert "SimpleMemStore" in mem.__all__
        assert "SimpleMemConfig" in mem.__all__
        assert "HAS_SIMPLEMEM" in mem.__all__


# ============================================================
# RUN ALL TESTS
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
