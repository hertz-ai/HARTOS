"""
Tests for Inbound Debouncing System

Tests debounce windows, batching, flush behavior,
and channel-specific configurations.
"""

import pytest
import asyncio

# Configure pytest-asyncio
pytestmark = pytest.mark.asyncio(loop_scope="function")
import os
import sys
import time
from unittest.mock import Mock, AsyncMock
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.channels.queue.debounce import (
    DebounceConfig,
    DebounceStats,
    DebounceBuffer,
    InboundDebouncer,
    SyncDebouncer,
)


@dataclass
class TestMessage:
    """Test message for debouncing."""
    id: str
    chat_id: str
    content: str


class TestDebounceConfig:
    """Tests for DebounceConfig."""

    def test_default_config(self):
        """Test default configuration."""
        config = DebounceConfig()
        assert config.window_ms == 1000
        assert config.max_messages == 10
        assert config.channel_overrides == {}

    def test_custom_config(self):
        """Test custom configuration."""
        config = DebounceConfig(
            window_ms=500,
            max_messages=5,
            channel_overrides={"telegram": 2000},
        )
        assert config.window_ms == 500
        assert config.max_messages == 5
        assert config.channel_overrides["telegram"] == 2000


class TestDebounceBuffer:
    """Tests for DebounceBuffer."""

    def test_buffer_creation(self):
        """Test buffer creation."""
        buffer = DebounceBuffer()
        assert buffer.items == []
        assert buffer.timer_task is None

    def test_add_items(self):
        """Test adding items to buffer."""
        buffer = DebounceBuffer()
        buffer.add("item1")
        buffer.add("item2")
        assert len(buffer.items) == 2
        assert buffer.items == ["item1", "item2"]

    def test_clear_returns_items(self):
        """Test clear returns and removes items."""
        buffer = DebounceBuffer()
        buffer.add("item1")
        buffer.add("item2")

        items = buffer.clear()
        assert items == ["item1", "item2"]
        assert buffer.items == []


class TestInboundDebouncer:
    """Tests for InboundDebouncer (async)."""

    @pytest.fixture
    def config(self):
        """Create test config."""
        return DebounceConfig(window_ms=100, max_messages=5)

    @pytest.fixture
    def debouncer(self, config):
        """Create debouncer."""
        return InboundDebouncer(config)

    @pytest.mark.asyncio
    async def test_single_message_no_debounce(self):
        """Test single message with debounce disabled."""
        config = DebounceConfig(window_ms=0)
        debouncer = InboundDebouncer(config)

        msg = TestMessage(id="1", chat_id="chat1", content="Hello")
        result = await debouncer.debounce(msg, key="chat1")

        assert result is not None
        assert len(result) == 1
        assert result[0] == msg

    @pytest.mark.asyncio
    async def test_message_buffered_with_debounce(self, debouncer):
        """Test message is buffered when debounce enabled."""
        msg = TestMessage(id="1", chat_id="chat1", content="Hello")
        result = await debouncer.debounce(msg, key="chat1")

        # Should be buffered, not returned
        assert result is None
        assert debouncer.get_pending_count() == 1

    @pytest.mark.asyncio
    async def test_multiple_messages_collected(self, debouncer):
        """Test multiple messages are collected in buffer."""
        for i in range(3):
            msg = TestMessage(id=str(i), chat_id="chat1", content=f"Message {i}")
            await debouncer.debounce(msg, key="chat1")

        assert debouncer.get_pending_count() == 3

    @pytest.mark.asyncio
    async def test_max_messages_triggers_flush(self, debouncer):
        """Test max_messages limit triggers immediate flush."""
        results = []
        for i in range(5):
            msg = TestMessage(id=str(i), chat_id="chat1", content=f"Message {i}")
            result = await debouncer.debounce(msg, key="chat1")
            if result:
                results.extend(result)

        # Should have flushed at max_messages
        assert len(results) == 5
        assert debouncer.get_pending_count() == 0

    @pytest.mark.asyncio
    async def test_window_expiration_flush(self):
        """Test window expiration triggers flush."""
        config = DebounceConfig(window_ms=50, max_messages=100)
        flushed_items = []

        async def on_flush(items):
            flushed_items.extend(items)

        debouncer = InboundDebouncer(config, on_flush=on_flush)

        msg = TestMessage(id="1", chat_id="chat1", content="Hello")
        await debouncer.debounce(msg, key="chat1")

        assert debouncer.get_pending_count() == 1

        # Wait for debounce window to expire
        await asyncio.sleep(0.1)

        assert len(flushed_items) == 1
        assert debouncer.get_pending_count() == 0

    @pytest.mark.asyncio
    async def test_manual_flush(self, debouncer):
        """Test manual flush."""
        for i in range(3):
            msg = TestMessage(id=str(i), chat_id="chat1", content=f"Message {i}")
            await debouncer.debounce(msg, key="chat1")

        items = debouncer.flush("chat1")

        assert len(items) == 3
        assert debouncer.get_pending_count() == 0

    @pytest.mark.asyncio
    async def test_flush_all(self, debouncer):
        """Test flush all buffers."""
        # Add to multiple keys
        await debouncer.debounce(
            TestMessage(id="1", chat_id="chat1", content="Hello"),
            key="chat1"
        )
        await debouncer.debounce(
            TestMessage(id="2", chat_id="chat2", content="World"),
            key="chat2"
        )

        result = debouncer.flush_all()

        assert len(result) == 2
        assert "chat1" in result
        assert "chat2" in result
        assert debouncer.get_pending_count() == 0

    @pytest.mark.asyncio
    async def test_channel_specific_debounce(self):
        """Test channel-specific debounce settings."""
        config = DebounceConfig(
            window_ms=1000,
            channel_overrides={"fast_channel": 0},
        )
        debouncer = InboundDebouncer(config)

        # Fast channel should not debounce
        msg = TestMessage(id="1", chat_id="chat1", content="Hello")
        result = await debouncer.debounce(msg, key="chat1", channel="fast_channel")
        assert result is not None

        # Other channels should debounce
        msg2 = TestMessage(id="2", chat_id="chat2", content="World")
        result2 = await debouncer.debounce(msg2, key="chat2", channel="slow_channel")
        assert result2 is None

    @pytest.mark.asyncio
    async def test_key_from_function(self, debouncer):
        """Test key extraction from function."""
        msg = TestMessage(id="1", chat_id="chat123", content="Hello")
        await debouncer.debounce(
            msg,
            key_func=lambda m: m.chat_id
        )

        assert debouncer.get_pending_count() == 1
        keys = debouncer.get_pending_keys()
        assert "chat123" in keys

    @pytest.mark.asyncio
    async def test_should_debounce_false(self, debouncer):
        """Test should_debounce=False bypasses debouncing."""
        msg = TestMessage(id="1", chat_id="chat1", content="Hello")
        result = await debouncer.debounce(msg, key="chat1", should_debounce=False)

        assert result is not None
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_flush_callback(self):
        """Test on_flush callback is called."""
        config = DebounceConfig(window_ms=100, max_messages=2)
        flushed = []

        debouncer = InboundDebouncer(
            config,
            on_flush=lambda items: flushed.extend(items)
        )

        # Trigger max_messages flush
        await debouncer.debounce(TestMessage("1", "c", "a"), key="c")
        await debouncer.debounce(TestMessage("2", "c", "b"), key="c")

        assert len(flushed) == 2

    @pytest.mark.asyncio
    async def test_async_flush_callback(self):
        """Test async on_flush callback."""
        config = DebounceConfig(window_ms=100, max_messages=2)
        flushed = []

        async def async_flush(items):
            await asyncio.sleep(0.01)
            flushed.extend(items)

        debouncer = InboundDebouncer(config, on_flush=async_flush)

        await debouncer.debounce(TestMessage("1", "c", "a"), key="c")
        await debouncer.debounce(TestMessage("2", "c", "b"), key="c")

        assert len(flushed) == 2

    @pytest.mark.asyncio
    async def test_error_callback(self):
        """Test on_error callback on flush failure."""
        config = DebounceConfig(window_ms=100, max_messages=2)
        errors = []

        def bad_flush(items):
            raise ValueError("Flush failed")

        def on_error(err, items):
            errors.append((err, items))

        debouncer = InboundDebouncer(config, on_flush=bad_flush, on_error=on_error)

        await debouncer.debounce(TestMessage("1", "c", "a"), key="c")
        await debouncer.debounce(TestMessage("2", "c", "b"), key="c")

        assert len(errors) == 1
        assert isinstance(errors[0][0], ValueError)

    @pytest.mark.asyncio
    async def test_stats_tracking(self, debouncer):
        """Test statistics are tracked."""
        for i in range(3):
            await debouncer.debounce(
                TestMessage(str(i), "c", f"msg{i}"),
                key="c"
            )

        stats = debouncer.get_stats()
        assert stats.total_received == 3
        assert stats.current_pending == 3

        debouncer.flush("c")

        stats = debouncer.get_stats()
        assert stats.total_flushed == 3
        assert stats.total_batches == 1
        assert stats.current_pending == 0

    @pytest.mark.asyncio
    async def test_clear_buffers(self, debouncer):
        """Test clearing all buffers without flushing."""
        for i in range(3):
            await debouncer.debounce(
                TestMessage(str(i), "c", f"msg{i}"),
                key="c"
            )

        cleared = debouncer.clear()
        assert cleared == 3
        assert debouncer.get_pending_count() == 0

    @pytest.mark.asyncio
    async def test_concurrent_keys(self, debouncer):
        """Test concurrent debouncing to different keys."""
        for i in range(3):
            await debouncer.debounce(
                TestMessage(f"a{i}", "chat_a", f"A{i}"),
                key="chat_a"
            )
            await debouncer.debounce(
                TestMessage(f"b{i}", "chat_b", f"B{i}"),
                key="chat_b"
            )

        assert debouncer.get_pending_count() == 6
        assert len(debouncer.get_pending_keys()) == 2


class TestSyncDebouncer:
    """Tests for SyncDebouncer (synchronous)."""

    @pytest.fixture
    def config(self):
        """Create test config."""
        return DebounceConfig(window_ms=100, max_messages=5)

    @pytest.fixture
    def debouncer(self, config):
        """Create sync debouncer."""
        return SyncDebouncer(config)

    def test_single_message_no_debounce(self):
        """Test single message without debounce."""
        config = DebounceConfig(window_ms=0)
        debouncer = SyncDebouncer(config)

        msg = TestMessage(id="1", chat_id="chat1", content="Hello")
        result = debouncer.debounce(msg, key="chat1")

        assert result is not None
        assert len(result) == 1

    def test_message_buffered(self, debouncer):
        """Test message is buffered."""
        msg = TestMessage(id="1", chat_id="chat1", content="Hello")
        result = debouncer.debounce(msg, key="chat1")

        assert result is None
        assert debouncer.get_pending_count() == 1

    def test_max_messages_flush(self, debouncer):
        """Test max messages triggers flush."""
        results = []
        for i in range(5):
            msg = TestMessage(id=str(i), chat_id="chat1", content=f"Msg {i}")
            result = debouncer.debounce(msg, key="chat1")
            if result:
                results.extend(result)

        assert len(results) == 5

    def test_manual_flush(self, debouncer):
        """Test manual flush."""
        for i in range(3):
            debouncer.debounce(
                TestMessage(str(i), "c", f"msg{i}"),
                key="c"
            )

        items = debouncer.flush("c")
        assert len(items) == 3
        assert debouncer.get_pending_count() == 0

    def test_flush_all(self, debouncer):
        """Test flush all."""
        debouncer.debounce(TestMessage("1", "a", "x"), key="a")
        debouncer.debounce(TestMessage("2", "b", "y"), key="b")

        result = debouncer.flush_all()
        assert len(result) == 2

    def test_timer_flush(self):
        """Test timer-based flush."""
        config = DebounceConfig(window_ms=50, max_messages=100)
        flushed = []

        debouncer = SyncDebouncer(config, on_flush=lambda items: flushed.extend(items))

        debouncer.debounce(TestMessage("1", "c", "x"), key="c")

        # Wait for timer
        time.sleep(0.1)

        assert len(flushed) == 1

    def test_channel_override(self):
        """Test channel-specific settings."""
        config = DebounceConfig(
            window_ms=1000,
            channel_overrides={"fast": 0},
        )
        debouncer = SyncDebouncer(config)

        # Fast channel - no debounce
        result = debouncer.debounce(
            TestMessage("1", "c", "x"),
            key="c",
            channel="fast"
        )
        assert result is not None

        # Normal channel - debounced
        result2 = debouncer.debounce(
            TestMessage("2", "c", "y"),
            key="c2",
            channel="slow"
        )
        assert result2 is None

    def test_stats(self, debouncer):
        """Test statistics."""
        for i in range(3):
            debouncer.debounce(TestMessage(str(i), "c", f"m{i}"), key="c")

        stats = debouncer.get_stats()
        assert stats.total_received == 3
        assert stats.current_pending == 3

        debouncer.flush("c")

        stats = debouncer.get_stats()
        assert stats.total_flushed == 3
        assert stats.total_batches == 1


class TestDebounceStats:
    """Tests for DebounceStats."""

    def test_default_stats(self):
        """Test default statistics values."""
        stats = DebounceStats()
        assert stats.total_received == 0
        assert stats.total_flushed == 0
        assert stats.total_batches == 0
        assert stats.current_pending == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
