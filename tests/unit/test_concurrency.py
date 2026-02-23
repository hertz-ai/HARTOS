"""
Tests for Concurrency Control System
"""

import pytest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.queue.concurrency import (
    ConcurrencyLimits,
    ConcurrencyStats,
    ConcurrencyController,
)


class TestConcurrencyLimits:
    """Tests for ConcurrencyLimits."""

    def test_default_limits(self):
        limits = ConcurrencyLimits()
        assert limits.max_per_user == 4
        assert limits.max_per_channel == 20
        assert limits.max_per_chat == 2
        assert limits.max_global == 100

    def test_custom_limits(self):
        limits = ConcurrencyLimits(max_per_user=10, max_global=50)
        assert limits.max_per_user == 10
        assert limits.max_global == 50


class TestConcurrencyController:
    """Tests for ConcurrencyController."""

    @pytest.fixture
    def controller(self):
        limits = ConcurrencyLimits(
            max_per_user=2,
            max_per_channel=5,
            max_per_chat=2,
            max_global=10,
        )
        return ConcurrencyController(limits)

    def test_acquire_sync(self, controller):
        slot_id = controller.acquire_sync("telegram", "chat1", "user1")
        assert slot_id is not None
        assert controller.get_slot_count() == 1

    def test_release_by_slot_id(self, controller):
        slot_id = controller.acquire_sync("telegram", "chat1", "user1")
        released = controller.release(slot_id=slot_id)
        assert released is True
        assert controller.get_slot_count() == 0

    def test_release_by_attributes(self, controller):
        controller.acquire_sync("telegram", "chat1", "user1")
        released = controller.release(channel="telegram", chat_id="chat1", user_id="user1")
        assert released is True
        assert controller.get_slot_count() == 0

    def test_per_user_limit(self, controller):
        controller.acquire_sync("telegram", "chat1", "user1")
        controller.acquire_sync("telegram", "chat2", "user1")
        # Third should fail (max_per_user=2)
        slot = controller.acquire_sync("telegram", "chat3", "user1")
        assert slot is None

    def test_per_chat_limit(self, controller):
        controller.acquire_sync("telegram", "chat1", "user1")
        controller.acquire_sync("telegram", "chat1", "user2")
        # Third should fail (max_per_chat=2)
        slot = controller.acquire_sync("telegram", "chat1", "user3")
        assert slot is None

    def test_per_channel_limit(self, controller):
        for i in range(5):
            controller.acquire_sync("telegram", f"chat{i}", f"user{i}")
        # Sixth should fail (max_per_channel=5)
        slot = controller.acquire_sync("telegram", "chat5", "user5")
        assert slot is None

    def test_global_limit(self, controller):
        for i in range(10):
            controller.acquire_sync(f"channel{i}", f"chat{i}", f"user{i}")
        # Eleventh should fail (max_global=10)
        slot = controller.acquire_sync("extra", "extra", "extra")
        assert slot is None

    def test_is_available(self, controller):
        assert controller.is_available("telegram", "chat1", "user1") is True
        controller.acquire_sync("telegram", "chat1", "user1")
        controller.acquire_sync("telegram", "chat1", "user2")
        # Chat is full
        assert controller.is_available("telegram", "chat1", "user3") is False

    def test_release_all_for_user(self, controller):
        controller.acquire_sync("telegram", "chat1", "user1")
        controller.acquire_sync("discord", "chat2", "user1")
        released = controller.release_all_for_user("user1")
        assert released == 2
        assert controller.get_slot_count() == 0

    def test_release_all_for_channel(self, controller):
        controller.acquire_sync("telegram", "chat1", "user1")
        controller.acquire_sync("telegram", "chat2", "user2")
        controller.acquire_sync("discord", "chat1", "user3")
        released = controller.release_all_for_channel("telegram")
        assert released == 2
        assert controller.get_slot_count() == 1

    def test_get_usage(self, controller):
        controller.acquire_sync("telegram", "chat1", "user1")
        usage = controller.get_usage()
        assert usage.current_global == 1
        assert usage.current_by_channel["telegram"] == 1
        assert usage.total_acquired == 1

    def test_clear(self, controller):
        controller.acquire_sync("telegram", "chat1", "user1")
        controller.acquire_sync("discord", "chat2", "user2")
        cleared = controller.clear()
        assert cleared == 2
        assert controller.get_slot_count() == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
