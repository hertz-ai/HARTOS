"""
Tests for Message Queue System

Tests queue policies, drop policies, deduplication,
and queue management.
"""

import pytest
import os
import sys
import time
from datetime import datetime, timedelta
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.queue.message_queue import (
    QueuePolicy,
    DropPolicy,
    DedupeMode,
    QueuedMessage,
    QueueConfig,
    QueueStats,
    MessageQueue,
    QueueManager,
    get_queue_manager,
)


class TestQueuedMessage:
    """Tests for QueuedMessage."""

    def test_message_creation(self):
        """Test basic message creation."""
        msg = QueuedMessage(
            message_id="msg123",
            channel="telegram",
            chat_id="chat456",
            sender_id="user789",
            content="Hello world",
        )

        assert msg.message_id == "msg123"
        assert msg.channel == "telegram"
        assert msg.chat_id == "chat456"
        assert msg.sender_id == "user789"
        assert msg.content == "Hello world"
        assert msg.priority == 0
        assert msg.enqueued_at is not None

    def test_content_hash_generated(self):
        """Test that content hash is auto-generated."""
        msg = QueuedMessage(
            message_id="msg1",
            channel="test",
            chat_id="chat1",
            sender_id="user1",
            content="Test content",
        )

        assert msg.content_hash != ""
        assert len(msg.content_hash) == 16

    def test_same_content_same_hash(self):
        """Test that same content produces same hash."""
        msg1 = QueuedMessage(
            message_id="msg1",
            channel="test",
            chat_id="chat1",
            sender_id="user1",
            content="Same content",
        )
        msg2 = QueuedMessage(
            message_id="msg2",
            channel="test",
            chat_id="chat1",
            sender_id="user1",
            content="Same content",
        )

        assert msg1.content_hash == msg2.content_hash

    def test_different_content_different_hash(self):
        """Test that different content produces different hash."""
        msg1 = QueuedMessage(
            message_id="msg1",
            channel="test",
            chat_id="chat1",
            sender_id="user1",
            content="Content A",
        )
        msg2 = QueuedMessage(
            message_id="msg2",
            channel="test",
            chat_id="chat1",
            sender_id="user1",
            content="Content B",
        )

        assert msg1.content_hash != msg2.content_hash


class TestQueueConfig:
    """Tests for QueueConfig."""

    def test_default_config(self):
        """Test default configuration values."""
        config = QueueConfig()

        assert config.policy == QueuePolicy.BACKLOG
        assert config.drop_policy == DropPolicy.SUMMARIZE
        assert config.dedupe_mode == DedupeMode.MESSAGE_ID
        assert config.max_size == 20
        assert config.max_age_seconds == 300
        assert config.debounce_ms == 1000

    def test_custom_config(self):
        """Test custom configuration."""
        config = QueueConfig(
            policy=QueuePolicy.PRIORITY,
            drop_policy=DropPolicy.OLD,
            max_size=50,
        )

        assert config.policy == QueuePolicy.PRIORITY
        assert config.drop_policy == DropPolicy.OLD
        assert config.max_size == 50


class TestMessageQueueBacklogPolicy:
    """Tests for MessageQueue with BACKLOG policy."""

    @pytest.fixture
    def queue(self):
        """Create a queue with BACKLOG policy."""
        config = QueueConfig(
            policy=QueuePolicy.BACKLOG,
            max_size=5,
            dedupe_mode=DedupeMode.NONE,
        )
        return MessageQueue(config)

    def test_enqueue_dequeue_basic(self, queue):
        """Test basic enqueue and dequeue."""
        msg = QueuedMessage(
            message_id="msg1",
            channel="test",
            chat_id="chat1",
            sender_id="user1",
            content="Hello",
        )

        assert queue.enqueue(msg) is True
        assert queue.size == 1

        result = queue.dequeue()
        assert result is not None
        assert result.message_id == "msg1"
        assert queue.size == 0

    def test_fifo_ordering(self, queue):
        """Test FIFO ordering in backlog mode."""
        for i in range(3):
            msg = QueuedMessage(
                message_id=f"msg{i}",
                channel="test",
                chat_id="chat1",
                sender_id="user1",
                content=f"Message {i}",
            )
            queue.enqueue(msg)

        for i in range(3):
            result = queue.dequeue()
            assert result.message_id == f"msg{i}"

    def test_peek_does_not_remove(self, queue):
        """Test that peek doesn't remove the message."""
        msg = QueuedMessage(
            message_id="msg1",
            channel="test",
            chat_id="chat1",
            sender_id="user1",
            content="Hello",
        )
        queue.enqueue(msg)

        peeked = queue.peek()
        assert peeked is not None
        assert queue.size == 1

        dequeued = queue.dequeue()
        assert dequeued.message_id == peeked.message_id

    def test_dequeue_empty_returns_none(self, queue):
        """Test dequeue on empty queue."""
        result = queue.dequeue()
        assert result is None

    def test_is_empty_and_is_full(self, queue):
        """Test is_empty and is_full properties."""
        assert queue.is_empty is True
        assert queue.is_full is False

        for i in range(5):
            msg = QueuedMessage(
                message_id=f"msg{i}",
                channel="test",
                chat_id="chat1",
                sender_id="user1",
                content=f"Message {i}",
            )
            queue.enqueue(msg)

        assert queue.is_empty is False
        assert queue.is_full is True


class TestMessageQueueDropPolicy:
    """Tests for MessageQueue with DROP policy."""

    def test_drop_policy_rejects_when_full(self):
        """Test DROP policy rejects new messages when full."""
        config = QueueConfig(
            policy=QueuePolicy.DROP,
            max_size=2,
            dedupe_mode=DedupeMode.NONE,
        )
        queue = MessageQueue(config)

        msg1 = QueuedMessage(message_id="msg1", channel="test", chat_id="c", sender_id="u", content="1")
        msg2 = QueuedMessage(message_id="msg2", channel="test", chat_id="c", sender_id="u", content="2")
        msg3 = QueuedMessage(message_id="msg3", channel="test", chat_id="c", sender_id="u", content="3")

        assert queue.enqueue(msg1) is True
        assert queue.enqueue(msg2) is True
        assert queue.enqueue(msg3) is False  # Dropped

        assert queue.size == 2
        stats = queue.get_stats()
        assert stats.total_dropped == 1


class TestMessageQueueLatestPolicy:
    """Tests for MessageQueue with LATEST policy."""

    def test_latest_policy_keeps_only_newest(self):
        """Test LATEST policy keeps only the most recent message."""
        config = QueueConfig(
            policy=QueuePolicy.LATEST,
            max_size=10,
            dedupe_mode=DedupeMode.NONE,
        )
        queue = MessageQueue(config)

        for i in range(5):
            msg = QueuedMessage(
                message_id=f"msg{i}",
                channel="test",
                chat_id="chat1",
                sender_id="user1",
                content=f"Message {i}",
            )
            queue.enqueue(msg)

        assert queue.size == 1
        result = queue.dequeue()
        assert result.message_id == "msg4"  # Last one


class TestMessageQueuePriorityPolicy:
    """Tests for MessageQueue with PRIORITY policy."""

    def test_priority_ordering(self):
        """Test PRIORITY policy orders by priority."""
        config = QueueConfig(
            policy=QueuePolicy.PRIORITY,
            max_size=10,
            dedupe_mode=DedupeMode.NONE,
        )
        queue = MessageQueue(config)

        # Add messages with different priorities
        priorities = [3, 1, 5, 2, 4]
        for i, priority in enumerate(priorities):
            msg = QueuedMessage(
                message_id=f"msg{i}",
                channel="test",
                chat_id="chat1",
                sender_id="user1",
                content=f"Message {i}",
                priority=priority,
            )
            queue.enqueue(msg)

        # Should come out in priority order (highest first)
        expected_priorities = [5, 4, 3, 2, 1]
        for expected_priority in expected_priorities:
            result = queue.dequeue()
            assert result.priority == expected_priority

    def test_priority_override_on_enqueue(self):
        """Test priority can be overridden on enqueue."""
        config = QueueConfig(policy=QueuePolicy.PRIORITY, dedupe_mode=DedupeMode.NONE)
        queue = MessageQueue(config)

        msg = QueuedMessage(
            message_id="msg1",
            channel="test",
            chat_id="chat1",
            sender_id="user1",
            content="Test",
            priority=1,
        )
        queue.enqueue(msg, priority=10)

        result = queue.dequeue()
        assert result.priority == 10


class TestDropPolicies:
    """Tests for drop policies when queue is at capacity."""

    def test_drop_old_policy(self):
        """Test DROP_OLD removes oldest messages."""
        config = QueueConfig(
            policy=QueuePolicy.BACKLOG,
            drop_policy=DropPolicy.OLD,
            max_size=2,
            dedupe_mode=DedupeMode.NONE,
        )
        queue = MessageQueue(config)

        msg1 = QueuedMessage(message_id="msg1", channel="test", chat_id="c", sender_id="u", content="First")
        msg2 = QueuedMessage(message_id="msg2", channel="test", chat_id="c", sender_id="u", content="Second")
        msg3 = QueuedMessage(message_id="msg3", channel="test", chat_id="c", sender_id="u", content="Third")

        queue.enqueue(msg1)
        queue.enqueue(msg2)
        queue.enqueue(msg3)

        assert queue.size == 2
        result1 = queue.dequeue()
        assert result1.message_id == "msg2"  # First was dropped

    def test_drop_new_policy(self):
        """Test DROP_NEW rejects new messages."""
        config = QueueConfig(
            policy=QueuePolicy.BACKLOG,
            drop_policy=DropPolicy.NEW,
            max_size=2,
            dedupe_mode=DedupeMode.NONE,
        )
        queue = MessageQueue(config)

        msg1 = QueuedMessage(message_id="msg1", channel="test", chat_id="c", sender_id="u", content="First")
        msg2 = QueuedMessage(message_id="msg2", channel="test", chat_id="c", sender_id="u", content="Second")
        msg3 = QueuedMessage(message_id="msg3", channel="test", chat_id="c", sender_id="u", content="Third")

        assert queue.enqueue(msg1) is True
        assert queue.enqueue(msg2) is True
        assert queue.enqueue(msg3) is False  # Rejected

        assert queue.size == 2
        result1 = queue.dequeue()
        assert result1.message_id == "msg1"  # Original messages preserved

    def test_summarize_policy(self):
        """Test SUMMARIZE keeps dropped message summaries."""
        config = QueueConfig(
            policy=QueuePolicy.BACKLOG,
            drop_policy=DropPolicy.SUMMARIZE,
            max_size=2,
            dedupe_mode=DedupeMode.NONE,
        )
        queue = MessageQueue(config)

        msg1 = QueuedMessage(message_id="msg1", channel="test", chat_id="c", sender_id="u", content="First message")
        msg2 = QueuedMessage(message_id="msg2", channel="test", chat_id="c", sender_id="u", content="Second message")
        msg3 = QueuedMessage(message_id="msg3", channel="test", chat_id="c", sender_id="u", content="Third message")

        queue.enqueue(msg1)
        queue.enqueue(msg2)
        queue.enqueue(msg3)

        stats = queue.get_stats()
        assert stats.total_dropped == 1
        assert len(stats.dropped_summaries) == 1
        assert "First message" in stats.dropped_summaries[0]

    def test_get_dropped_summary(self):
        """Test get_dropped_summary returns and clears summaries."""
        config = QueueConfig(
            policy=QueuePolicy.BACKLOG,
            drop_policy=DropPolicy.SUMMARIZE,
            max_size=2,
            dedupe_mode=DedupeMode.NONE,
        )
        queue = MessageQueue(config)

        for i in range(4):
            msg = QueuedMessage(
                message_id=f"msg{i}",
                channel="test",
                chat_id="c",
                sender_id="u",
                content=f"Message {i}",
            )
            queue.enqueue(msg)

        summary = queue.get_dropped_summary()
        assert summary is not None
        assert "Queue overflow" in summary
        assert "Dropped 2 messages" in summary

        # Second call should return None (cleared)
        summary2 = queue.get_dropped_summary()
        assert summary2 is None


class TestDeduplication:
    """Tests for message deduplication."""

    def test_dedupe_by_message_id(self):
        """Test deduplication by message ID."""
        config = QueueConfig(
            dedupe_mode=DedupeMode.MESSAGE_ID,
            max_size=10,
        )
        queue = MessageQueue(config)

        msg1 = QueuedMessage(message_id="same_id", channel="test", chat_id="c", sender_id="u", content="First")
        msg2 = QueuedMessage(message_id="same_id", channel="test", chat_id="c", sender_id="u", content="Second")

        assert queue.enqueue(msg1) is True
        assert queue.enqueue(msg2) is False  # Duplicate

        stats = queue.get_stats()
        assert stats.total_deduplicated == 1

    def test_dedupe_by_content(self):
        """Test deduplication by content hash."""
        config = QueueConfig(
            dedupe_mode=DedupeMode.CONTENT,
            max_size=10,
        )
        queue = MessageQueue(config)

        msg1 = QueuedMessage(message_id="id1", channel="test", chat_id="c", sender_id="u", content="Same content")
        msg2 = QueuedMessage(message_id="id2", channel="test", chat_id="c", sender_id="u", content="Same content")

        assert queue.enqueue(msg1) is True
        assert queue.enqueue(msg2) is False  # Duplicate content

    def test_dedupe_combined(self):
        """Test combined deduplication mode."""
        config = QueueConfig(
            dedupe_mode=DedupeMode.COMBINED,
            max_size=10,
        )
        queue = MessageQueue(config)

        # Same message ID
        msg1 = QueuedMessage(message_id="same_id", channel="test", chat_id="c", sender_id="u", content="Content 1")
        msg2 = QueuedMessage(message_id="same_id", channel="test", chat_id="c", sender_id="u", content="Content 2")

        assert queue.enqueue(msg1) is True
        assert queue.enqueue(msg2) is False  # Same ID

        # Same content
        msg3 = QueuedMessage(message_id="id3", channel="test", chat_id="c", sender_id="u", content="Content 1")

        assert queue.enqueue(msg3) is False  # Same content as msg1

    def test_dedupe_none(self):
        """Test no deduplication."""
        config = QueueConfig(
            dedupe_mode=DedupeMode.NONE,
            max_size=10,
        )
        queue = MessageQueue(config)

        msg1 = QueuedMessage(message_id="same_id", channel="test", chat_id="c", sender_id="u", content="Same content")
        msg2 = QueuedMessage(message_id="same_id", channel="test", chat_id="c", sender_id="u", content="Same content")

        assert queue.enqueue(msg1) is True
        assert queue.enqueue(msg2) is True  # No deduplication

        assert queue.size == 2


class TestMessageExpiration:
    """Tests for message expiration."""

    def test_expired_messages_removed(self):
        """Test that expired messages are removed on dequeue."""
        config = QueueConfig(
            max_size=10,
            max_age_seconds=1,
            dedupe_mode=DedupeMode.NONE,
        )
        queue = MessageQueue(config)

        msg = QueuedMessage(
            message_id="msg1",
            channel="test",
            chat_id="c",
            sender_id="u",
            content="Test",
        )
        queue.enqueue(msg)
        assert queue.size == 1

        # Manually expire the message
        queue._items[0].enqueued_at = datetime.now() - timedelta(seconds=2)

        # Dequeue should return None (message expired)
        result = queue.dequeue()
        assert result is None

    def test_fresh_messages_not_expired(self):
        """Test that fresh messages are not expired."""
        config = QueueConfig(
            max_size=10,
            max_age_seconds=60,
            dedupe_mode=DedupeMode.NONE,
        )
        queue = MessageQueue(config)

        msg = QueuedMessage(
            message_id="msg1",
            channel="test",
            chat_id="c",
            sender_id="u",
            content="Test",
        )
        queue.enqueue(msg)

        result = queue.dequeue()
        assert result is not None
        assert result.message_id == "msg1"


class TestQueueClear:
    """Tests for queue clear functionality."""

    def test_clear_removes_all(self):
        """Test clear removes all messages."""
        config = QueueConfig(max_size=10, dedupe_mode=DedupeMode.NONE)
        queue = MessageQueue(config)

        for i in range(5):
            msg = QueuedMessage(
                message_id=f"msg{i}",
                channel="test",
                chat_id="c",
                sender_id="u",
                content=f"Message {i}",
            )
            queue.enqueue(msg)

        assert queue.size == 5
        cleared = queue.clear()
        assert cleared == 5
        assert queue.size == 0


class TestQueueStats:
    """Tests for queue statistics."""

    def test_stats_tracking(self):
        """Test statistics are tracked correctly."""
        config = QueueConfig(max_size=2, dedupe_mode=DedupeMode.MESSAGE_ID)
        queue = MessageQueue(config)

        # Enqueue
        msg1 = QueuedMessage(message_id="msg1", channel="test", chat_id="c", sender_id="u", content="1")
        msg2 = QueuedMessage(message_id="msg2", channel="test", chat_id="c", sender_id="u", content="2")
        msg3 = QueuedMessage(message_id="msg3", channel="test", chat_id="c", sender_id="u", content="3")
        msg4 = QueuedMessage(message_id="msg1", channel="test", chat_id="c", sender_id="u", content="dup")  # Duplicate

        queue.enqueue(msg1)
        queue.enqueue(msg2)
        queue.enqueue(msg3)  # Causes drop
        queue.enqueue(msg4)  # Duplicate

        queue.dequeue()

        stats = queue.get_stats()
        assert stats.total_enqueued == 3
        assert stats.total_dequeued == 1
        assert stats.total_dropped == 1
        assert stats.total_deduplicated == 1
        assert stats.current_size == 1

    def test_stats_to_dict(self):
        """Test stats serialization."""
        stats = QueueStats(
            total_enqueued=10,
            total_dequeued=5,
            total_dropped=2,
            current_size=3,
        )

        data = stats.to_dict()
        assert data["total_enqueued"] == 10
        assert data["total_dequeued"] == 5
        assert data["total_dropped"] == 2
        assert data["current_size"] == 3


class TestCollectMode:
    """Tests for COLLECT mode."""

    def test_collect_multiple_messages(self):
        """Test collecting multiple messages."""
        config = QueueConfig(
            policy=QueuePolicy.COLLECT,
            max_size=10,
            collect_batch_size=3,
            dedupe_mode=DedupeMode.NONE,
        )
        queue = MessageQueue(config)

        for i in range(5):
            msg = QueuedMessage(
                message_id=f"msg{i}",
                channel="test",
                chat_id="c",
                sender_id="u",
                content=f"Message {i}",
            )
            queue.enqueue(msg)

        collected = queue.collect()
        assert len(collected) == 3
        assert queue.size == 2

    def test_collect_with_custom_limit(self):
        """Test collect with custom limit."""
        config = QueueConfig(
            policy=QueuePolicy.COLLECT,
            max_size=10,
            dedupe_mode=DedupeMode.NONE,
        )
        queue = MessageQueue(config)

        for i in range(5):
            msg = QueuedMessage(
                message_id=f"msg{i}",
                channel="test",
                chat_id="c",
                sender_id="u",
                content=f"Message {i}",
            )
            queue.enqueue(msg)

        collected = queue.collect(max_items=2)
        assert len(collected) == 2


class TestDebounce:
    """Tests for debounce functionality."""

    def test_should_debounce(self):
        """Test debounce detection."""
        config = QueueConfig(debounce_ms=100, dedupe_mode=DedupeMode.NONE)
        queue = MessageQueue(config)

        assert queue.should_debounce() is False

        msg = QueuedMessage(message_id="msg1", channel="test", chat_id="c", sender_id="u", content="Test")
        queue.enqueue(msg)

        assert queue.should_debounce() is True

    def test_time_until_debounce_complete(self):
        """Test getting time until debounce completes."""
        config = QueueConfig(debounce_ms=100, dedupe_mode=DedupeMode.NONE)
        queue = MessageQueue(config)

        msg = QueuedMessage(message_id="msg1", channel="test", chat_id="c", sender_id="u", content="Test")
        queue.enqueue(msg)

        remaining = queue.time_until_debounce_complete()
        assert remaining > 0
        assert remaining <= 100


class TestQueueManager:
    """Tests for QueueManager."""

    @pytest.fixture
    def manager(self):
        """Create a queue manager."""
        return QueueManager()

    def test_get_queue_creates_new(self, manager):
        """Test get_queue creates new queue."""
        queue = manager.get_queue("telegram", "chat123")
        assert queue is not None
        assert manager.has_queue("telegram", "chat123")

    def test_get_queue_returns_existing(self, manager):
        """Test get_queue returns existing queue."""
        queue1 = manager.get_queue("telegram", "chat123")
        queue2 = manager.get_queue("telegram", "chat123")
        assert queue1 is queue2

    def test_get_queue_create_false(self, manager):
        """Test get_queue with create=False."""
        queue = manager.get_queue("telegram", "chat123", create=False)
        assert queue is None

    def test_different_chats_different_queues(self, manager):
        """Test different chats get different queues."""
        queue1 = manager.get_queue("telegram", "chat1")
        queue2 = manager.get_queue("telegram", "chat2")
        assert queue1 is not queue2

    def test_different_channels_different_queues(self, manager):
        """Test different channels get different queues."""
        queue1 = manager.get_queue("telegram", "chat1")
        queue2 = manager.get_queue("discord", "chat1")
        assert queue1 is not queue2

    def test_delete_queue(self, manager):
        """Test deleting a queue."""
        manager.get_queue("telegram", "chat123")
        assert manager.has_queue("telegram", "chat123")

        result = manager.delete_queue("telegram", "chat123")
        assert result is True
        assert not manager.has_queue("telegram", "chat123")

    def test_list_queues(self, manager):
        """Test listing queues."""
        manager.get_queue("telegram", "chat1")
        manager.get_queue("telegram", "chat2")
        manager.get_queue("discord", "chat1")

        all_queues = manager.list_queues()
        assert len(all_queues) == 3

        telegram_queues = manager.list_queues(channel="telegram")
        assert len(telegram_queues) == 2

    def test_get_total_size(self, manager):
        """Test getting total message count."""
        queue1 = manager.get_queue("telegram", "chat1")
        queue2 = manager.get_queue("telegram", "chat2")

        msg1 = QueuedMessage(message_id="m1", channel="t", chat_id="c1", sender_id="u", content="1")
        msg2 = QueuedMessage(message_id="m2", channel="t", chat_id="c2", sender_id="u", content="2")
        msg3 = QueuedMessage(message_id="m3", channel="t", chat_id="c2", sender_id="u", content="3")

        queue1.enqueue(msg1)
        queue2.enqueue(msg2)
        queue2.enqueue(msg3)

        assert manager.get_total_size() == 3

    def test_process_all(self, manager):
        """Test processing all queues."""
        queue1 = manager.get_queue("telegram", "chat1")
        queue2 = manager.get_queue("discord", "chat1")

        queue1.enqueue(QueuedMessage(message_id="m1", channel="t", chat_id="c1", sender_id="u", content="1"))
        queue2.enqueue(QueuedMessage(message_id="m2", channel="d", chat_id="c1", sender_id="u", content="2"))

        processed = []
        count = manager.process_all(lambda msg: processed.append(msg.message_id))

        assert count == 2
        assert set(processed) == {"m1", "m2"}

    def test_channel_config(self, manager):
        """Test setting channel-specific config."""
        custom_config = QueueConfig(
            policy=QueuePolicy.LATEST,
            max_size=5,
        )
        manager.set_channel_config("telegram", custom_config)

        queue = manager.get_queue("telegram", "chat1")
        assert queue.config.policy == QueuePolicy.LATEST
        assert queue.config.max_size == 5

    def test_get_stats(self, manager):
        """Test getting aggregated stats."""
        queue1 = manager.get_queue("telegram", "chat1")
        queue2 = manager.get_queue("discord", "chat1")

        queue1.enqueue(QueuedMessage(message_id="m1", channel="t", chat_id="c1", sender_id="u", content="1"))
        queue2.enqueue(QueuedMessage(message_id="m2", channel="d", chat_id="c1", sender_id="u", content="2"))

        stats = manager.get_stats()
        assert stats["queue_count"] == 2
        assert stats["total_messages"] == 2
        assert stats["total_enqueued"] == 2


class TestGlobalQueueManager:
    """Tests for global queue manager singleton."""

    def test_singleton_pattern(self):
        """Test singleton returns same instance."""
        import integrations.channels.queue.message_queue as mq_module
        mq_module._queue_manager = None

        manager1 = get_queue_manager()
        manager2 = get_queue_manager()

        assert manager1 is manager2


class TestConcurrency:
    """Tests for thread safety."""

    def test_concurrent_enqueue(self):
        """Test concurrent enqueue operations."""
        import threading

        config = QueueConfig(max_size=100, dedupe_mode=DedupeMode.NONE)
        queue = MessageQueue(config)

        def enqueue_messages(start_id):
            for i in range(10):
                msg = QueuedMessage(
                    message_id=f"msg{start_id}_{i}",
                    channel="test",
                    chat_id="c",
                    sender_id="u",
                    content=f"Message {start_id}_{i}",
                )
                queue.enqueue(msg)

        threads = [
            threading.Thread(target=enqueue_messages, args=(i,))
            for i in range(5)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert queue.size == 50


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
