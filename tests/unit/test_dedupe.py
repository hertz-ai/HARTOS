"""
Tests for Message Deduplication System

Tests deduplication modes, TTL expiration,
and statistics tracking.
"""

import pytest
import os
import sys
import time
from datetime import datetime, timedelta
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.queue.dedupe import (
    DedupeMode,
    DedupeConfig,
    DedupeStats,
    DedupeEntry,
    MessageDeduplicator,
    SimpleDeduplicator,
)


@dataclass
class MockMessage:
    """Mock message for testing."""
    id: str
    content: str
    chat_id: str = "chat1"


class TestDedupeConfig:
    """Tests for DedupeConfig."""

    def test_default_config(self):
        """Test default configuration."""
        config = DedupeConfig()
        assert config.mode == DedupeMode.COMBINED
        assert config.ttl_seconds == 300
        assert config.max_entries == 10000

    def test_custom_config(self):
        """Test custom configuration."""
        config = DedupeConfig(
            mode=DedupeMode.CONTENT_HASH,
            ttl_seconds=60,
            max_entries=1000,
        )
        assert config.mode == DedupeMode.CONTENT_HASH
        assert config.ttl_seconds == 60
        assert config.max_entries == 1000


class TestDedupeStats:
    """Tests for DedupeStats."""

    def test_default_stats(self):
        """Test default statistics."""
        stats = DedupeStats()
        assert stats.total_checked == 0
        assert stats.total_duplicates == 0
        assert stats.total_unique == 0
        assert stats.duplicate_rate == 0.0

    def test_duplicate_rate(self):
        """Test duplicate rate calculation."""
        stats = DedupeStats(
            total_checked=100,
            total_duplicates=25,
        )
        assert stats.duplicate_rate == 25.0


class TestMessageDeduplicatorByID:
    """Tests for deduplication by message ID."""

    @pytest.fixture
    def deduper(self):
        """Create deduplicator with ID mode."""
        config = DedupeConfig(mode=DedupeMode.MESSAGE_ID, ttl_seconds=300)
        return MessageDeduplicator(config)

    def test_first_message_not_duplicate(self, deduper):
        """Test first message is not a duplicate."""
        msg = MockMessage(id="msg1", content="Hello")
        assert deduper.is_duplicate(msg, id_func=lambda m: m.id) is False

    def test_same_id_is_duplicate(self, deduper):
        """Test same ID is detected as duplicate."""
        msg1 = MockMessage(id="msg1", content="Hello")
        msg2 = MockMessage(id="msg1", content="Different content")

        deduper.mark_seen(msg1, id_func=lambda m: m.id)
        assert deduper.is_duplicate(msg2, id_func=lambda m: m.id) is True

    def test_different_id_not_duplicate(self, deduper):
        """Test different ID is not a duplicate."""
        msg1 = MockMessage(id="msg1", content="Hello")
        msg2 = MockMessage(id="msg2", content="Hello")  # Same content, diff ID

        deduper.mark_seen(msg1, id_func=lambda m: m.id)
        assert deduper.is_duplicate(msg2, id_func=lambda m: m.id) is False

    def test_check_and_mark(self, deduper):
        """Test check_and_mark operation."""
        msg = MockMessage(id="msg1", content="Hello")

        # First call: not duplicate, marks as seen
        result1 = deduper.check_and_mark(msg, id_func=lambda m: m.id)
        assert result1 is False

        # Second call: is duplicate
        result2 = deduper.check_and_mark(msg, id_func=lambda m: m.id)
        assert result2 is True


class TestMessageDeduplicatorByContent:
    """Tests for deduplication by content hash."""

    @pytest.fixture
    def deduper(self):
        """Create deduplicator with content mode."""
        config = DedupeConfig(mode=DedupeMode.CONTENT_HASH, ttl_seconds=300)
        return MessageDeduplicator(config)

    def test_first_message_not_duplicate(self, deduper):
        """Test first message is not a duplicate."""
        msg = MockMessage(id="msg1", content="Hello world")
        assert deduper.is_duplicate(msg, content_func=lambda m: m.content) is False

    def test_same_content_is_duplicate(self, deduper):
        """Test same content is detected as duplicate."""
        msg1 = MockMessage(id="msg1", content="Hello world")
        msg2 = MockMessage(id="msg2", content="Hello world")  # Diff ID, same content

        deduper.mark_seen(msg1, content_func=lambda m: m.content)
        assert deduper.is_duplicate(msg2, content_func=lambda m: m.content) is True

    def test_different_content_not_duplicate(self, deduper):
        """Test different content is not a duplicate."""
        msg1 = MockMessage(id="msg1", content="Hello world")
        msg2 = MockMessage(id="msg1", content="Goodbye world")  # Same ID, diff content

        deduper.mark_seen(msg1, content_func=lambda m: m.content)
        assert deduper.is_duplicate(msg2, content_func=lambda m: m.content) is False

    def test_whitespace_normalization(self, deduper):
        """Test whitespace is normalized for content comparison."""
        msg1 = MockMessage(id="msg1", content="Hello   world")  # Extra spaces
        msg2 = MockMessage(id="msg2", content="Hello world")    # Normal spaces

        deduper.mark_seen(msg1, content_func=lambda m: m.content)
        assert deduper.is_duplicate(msg2, content_func=lambda m: m.content) is True


class TestMessageDeduplicatorCombined:
    """Tests for combined deduplication mode."""

    @pytest.fixture
    def deduper(self):
        """Create deduplicator with combined mode."""
        config = DedupeConfig(mode=DedupeMode.COMBINED, ttl_seconds=300)
        return MessageDeduplicator(config)

    def test_same_id_is_duplicate(self, deduper):
        """Test same ID triggers duplicate."""
        msg1 = MockMessage(id="msg1", content="Hello")
        msg2 = MockMessage(id="msg1", content="Different")

        deduper.mark_seen(
            msg1,
            id_func=lambda m: m.id,
            content_func=lambda m: m.content
        )
        assert deduper.is_duplicate(
            msg2,
            id_func=lambda m: m.id,
            content_func=lambda m: m.content
        ) is True

    def test_same_content_is_duplicate(self, deduper):
        """Test same content triggers duplicate."""
        msg1 = MockMessage(id="msg1", content="Hello")
        msg2 = MockMessage(id="msg2", content="Hello")

        deduper.mark_seen(
            msg1,
            id_func=lambda m: m.id,
            content_func=lambda m: m.content
        )
        assert deduper.is_duplicate(
            msg2,
            id_func=lambda m: m.id,
            content_func=lambda m: m.content
        ) is True

    def test_both_different_not_duplicate(self, deduper):
        """Test different ID and content is not duplicate."""
        msg1 = MockMessage(id="msg1", content="Hello")
        msg2 = MockMessage(id="msg2", content="World")

        deduper.mark_seen(
            msg1,
            id_func=lambda m: m.id,
            content_func=lambda m: m.content
        )
        assert deduper.is_duplicate(
            msg2,
            id_func=lambda m: m.id,
            content_func=lambda m: m.content
        ) is False


class TestMessageDeduplicatorNone:
    """Tests for no deduplication mode."""

    def test_no_dedup_allows_all(self):
        """Test NONE mode allows all messages."""
        config = DedupeConfig(mode=DedupeMode.NONE)
        deduper = MessageDeduplicator(config)

        msg = MockMessage(id="msg1", content="Hello")

        # Mark and check - should never be duplicate
        deduper.mark_seen(msg, id_func=lambda m: m.id)
        assert deduper.is_duplicate(msg, id_func=lambda m: m.id) is False


class TestMessageDeduplicatorTTL:
    """Tests for TTL expiration."""

    def test_expired_entry_not_duplicate(self):
        """Test expired entries are not considered duplicates."""
        config = DedupeConfig(mode=DedupeMode.MESSAGE_ID, ttl_seconds=1)
        deduper = MessageDeduplicator(config)

        msg = MockMessage(id="msg1", content="Hello")
        deduper.mark_seen(msg, id_func=lambda m: m.id)

        # Manually expire
        hash_val = list(deduper._entries.keys())[0]
        deduper._entries[hash_val].last_seen = datetime.now() - timedelta(seconds=2)

        # Should not be duplicate (expired)
        assert deduper.is_duplicate(msg, id_func=lambda m: m.id) is False

    def test_cleanup_expired(self):
        """Test cleanup_expired removes old entries."""
        config = DedupeConfig(mode=DedupeMode.MESSAGE_ID, ttl_seconds=1)
        deduper = MessageDeduplicator(config)

        msg = MockMessage(id="msg1", content="Hello")
        deduper.mark_seen(msg, id_func=lambda m: m.id)

        assert deduper.get_entry_count() == 1

        # Manually expire
        hash_val = list(deduper._entries.keys())[0]
        deduper._entries[hash_val].last_seen = datetime.now() - timedelta(seconds=2)

        removed = deduper.cleanup_expired()
        assert removed == 1
        assert deduper.get_entry_count() == 0


class TestMessageDeduplicatorMaxEntries:
    """Tests for max entries enforcement."""

    def test_max_entries_eviction(self):
        """Test oldest entries are evicted at max."""
        config = DedupeConfig(
            mode=DedupeMode.MESSAGE_ID,
            max_entries=3,
            ttl_seconds=300,
        )
        deduper = MessageDeduplicator(config)

        # Add 4 messages
        for i in range(4):
            msg = MockMessage(id=f"msg{i}", content=f"Content {i}")
            deduper.mark_seen(msg, id_func=lambda m: m.id)

        # Should only have 3 entries
        assert deduper.get_entry_count() == 3

        # First message should have been evicted
        msg0 = MockMessage(id="msg0", content="Content 0")
        assert deduper.is_duplicate(msg0, id_func=lambda m: m.id) is False

        # Last 3 should still be there
        for i in range(1, 4):
            msg = MockMessage(id=f"msg{i}", content=f"Content {i}")
            assert deduper.is_duplicate(msg, id_func=lambda m: m.id) is True


class TestMessageDeduplicatorStats:
    """Tests for statistics tracking."""

    def test_stats_tracking(self):
        """Test statistics are tracked correctly."""
        config = DedupeConfig(mode=DedupeMode.MESSAGE_ID)
        deduper = MessageDeduplicator(config)

        # Check 5 messages, 2 are duplicates
        msg1 = MockMessage(id="msg1", content="A")
        msg2 = MockMessage(id="msg2", content="B")
        msg3 = MockMessage(id="msg1", content="C")  # Dup of msg1

        deduper.mark_seen(msg1, id_func=lambda m: m.id)
        deduper.is_duplicate(msg1, id_func=lambda m: m.id)  # Dup
        deduper.mark_seen(msg2, id_func=lambda m: m.id)
        deduper.is_duplicate(msg2, id_func=lambda m: m.id)  # Dup
        deduper.is_duplicate(msg3, id_func=lambda m: m.id)  # Dup (same as msg1)

        stats = deduper.get_stats()
        assert stats.total_checked == 3
        assert stats.total_duplicates == 3
        assert stats.current_entries == 2


class TestMessageDeduplicatorClear:
    """Tests for clearing entries."""

    def test_clear(self):
        """Test clearing all entries."""
        config = DedupeConfig(mode=DedupeMode.MESSAGE_ID)
        deduper = MessageDeduplicator(config)

        for i in range(5):
            msg = MockMessage(id=f"msg{i}", content=f"Content {i}")
            deduper.mark_seen(msg, id_func=lambda m: m.id)

        assert deduper.get_entry_count() == 5

        cleared = deduper.clear()
        assert cleared == 5
        assert deduper.get_entry_count() == 0


class TestMessageDeduplicatorDirectValues:
    """Tests for direct value parameters."""

    def test_direct_message_id(self):
        """Test using direct message_id parameter."""
        config = DedupeConfig(mode=DedupeMode.MESSAGE_ID)
        deduper = MessageDeduplicator(config)

        msg = MockMessage(id="ignored", content="Hello")

        deduper.mark_seen(msg, message_id="custom_id")
        assert deduper.is_duplicate(msg, message_id="custom_id") is True
        assert deduper.is_duplicate(msg, message_id="other_id") is False

    def test_direct_content(self):
        """Test using direct content parameter."""
        config = DedupeConfig(mode=DedupeMode.CONTENT_HASH)
        deduper = MessageDeduplicator(config)

        msg = MockMessage(id="msg1", content="ignored")

        deduper.mark_seen(msg, content="custom content")
        assert deduper.is_duplicate(msg, content="custom content") is True
        assert deduper.is_duplicate(msg, content="other content") is False


class TestSimpleDeduplicator:
    """Tests for SimpleDeduplicator."""

    @pytest.fixture
    def deduper(self):
        """Create simple deduplicator."""
        return SimpleDeduplicator(ttl_seconds=300)

    def test_first_key_not_duplicate(self, deduper):
        """Test first key is not a duplicate."""
        assert deduper.is_duplicate("key1") is False

    def test_same_key_is_duplicate(self, deduper):
        """Test same key is a duplicate."""
        deduper.mark_seen("key1")
        assert deduper.is_duplicate("key1") is True

    def test_different_key_not_duplicate(self, deduper):
        """Test different key is not duplicate."""
        deduper.mark_seen("key1")
        assert deduper.is_duplicate("key2") is False

    def test_check_and_mark(self, deduper):
        """Test check_and_mark operation."""
        result1 = deduper.check_and_mark("key1")
        assert result1 is False  # First time, not duplicate

        result2 = deduper.check_and_mark("key1")
        assert result2 is True  # Second time, is duplicate

    def test_ttl_expiration(self):
        """Test TTL expiration."""
        deduper = SimpleDeduplicator(ttl_seconds=1)
        deduper.mark_seen("key1")

        # Manually expire
        deduper._seen["key1"] = datetime.now() - timedelta(seconds=2)

        assert deduper.is_duplicate("key1") is False

    def test_max_entries(self):
        """Test max entries enforcement."""
        deduper = SimpleDeduplicator(max_entries=3)

        for i in range(4):
            deduper.mark_seen(f"key{i}")

        assert deduper.get_count() == 3
        assert deduper.is_duplicate("key0") is False  # Evicted
        assert deduper.is_duplicate("key3") is True   # Still there

    def test_clear(self, deduper):
        """Test clearing entries."""
        for i in range(5):
            deduper.mark_seen(f"key{i}")

        cleared = deduper.clear()
        assert cleared == 5
        assert deduper.get_count() == 0


class TestHighVolume:
    """Tests for high-volume scenarios."""

    def test_high_volume_dedup(self):
        """Test deduplication under high volume."""
        config = DedupeConfig(mode=DedupeMode.MESSAGE_ID, max_entries=1000)
        deduper = MessageDeduplicator(config)

        # Add 500 unique messages
        for i in range(500):
            msg = MockMessage(id=f"msg{i}", content=f"Content {i}")
            deduper.mark_seen(msg, id_func=lambda m: m.id)

        # Check all are detected as duplicates
        duplicates = 0
        for i in range(500):
            msg = MockMessage(id=f"msg{i}", content=f"Content {i}")
            if deduper.is_duplicate(msg, id_func=lambda m: m.id):
                duplicates += 1

        assert duplicates == 500


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
