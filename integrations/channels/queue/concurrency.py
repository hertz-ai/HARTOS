"""
Concurrency Control System

Limits concurrent message processing to prevent overload.
Ported from HevolveBot's src/config/agent-limits.ts.

Features:
- Per-user limits
- Per-channel limits
- Per-chat limits
- Global limits
- Queue when limited option
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ConcurrencyLimits:
    """Configuration for concurrency limits."""
    max_per_user: int = 4
    max_per_channel: int = 20
    max_per_chat: int = 2
    max_global: int = 100
    queue_when_limited: bool = True
    timeout_seconds: int = 300


@dataclass
class ConcurrencyStats:
    """Statistics for concurrency controller."""
    current_global: int = 0
    current_by_channel: Dict[str, int] = field(default_factory=dict)
    current_by_chat: Dict[str, int] = field(default_factory=dict)
    current_by_user: Dict[str, int] = field(default_factory=dict)
    total_acquired: int = 0
    total_rejected: int = 0
    total_queued: int = 0
    total_released: int = 0


@dataclass
class ConcurrencySlot:
    """Represents an acquired concurrency slot."""
    slot_id: str
    channel: str
    chat_id: str
    user_id: str
    acquired_at: datetime = field(default_factory=datetime.now)


class ConcurrencyController:
    """
    Controls concurrent message processing.

    Ensures system doesn't get overwhelmed by limiting how many
    messages can be processed simultaneously.

    Usage:
        limits = ConcurrencyLimits(max_per_user=4, max_global=100)
        controller = ConcurrencyController(limits)

        # Try to acquire a slot
        if await controller.acquire("telegram", "chat123", "user456"):
            try:
                # Process message
                pass
            finally:
                controller.release("telegram", "chat123", "user456")
    """

    def __init__(self, limits: ConcurrencyLimits):
        self.limits = limits
        self._slots: Dict[str, ConcurrencySlot] = {}
        self._by_channel: Dict[str, Set[str]] = {}
        self._by_chat: Dict[str, Set[str]] = {}
        self._by_user: Dict[str, Set[str]] = {}
        self._lock = threading.Lock()
        self._stats = ConcurrencyStats()
        self._slot_counter = 0
        self._waiters: Dict[str, asyncio.Event] = {}

    def _make_slot_id(self, channel: str, chat_id: str, user_id: str) -> str:
        """Generate unique slot ID."""
        self._slot_counter += 1
        return f"{channel}:{chat_id}:{user_id}:{self._slot_counter}"

    def _get_chat_key(self, channel: str, chat_id: str) -> str:
        """Get unique key for a chat."""
        return f"{channel}:{chat_id}"

    def _is_available_unlocked(
        self,
        channel: str,
        chat_id: str,
        user_id: str,
    ) -> bool:
        """
        Check if a slot is available (internal, no lock).

        Must be called with self._lock already held.
        """
        # Check global limit
        if len(self._slots) >= self.limits.max_global:
            return False

        # Check per-channel limit
        channel_slots = self._by_channel.get(channel, set())
        if len(channel_slots) >= self.limits.max_per_channel:
            return False

        # Check per-chat limit
        chat_key = self._get_chat_key(channel, chat_id)
        chat_slots = self._by_chat.get(chat_key, set())
        if len(chat_slots) >= self.limits.max_per_chat:
            return False

        # Check per-user limit
        user_slots = self._by_user.get(user_id, set())
        if len(user_slots) >= self.limits.max_per_user:
            return False

        return True

    def is_available(
        self,
        channel: str,
        chat_id: str,
        user_id: str,
    ) -> bool:
        """
        Check if a slot is available without acquiring.

        Args:
            channel: Channel name
            chat_id: Chat identifier
            user_id: User identifier

        Returns:
            True if slot would be available
        """
        with self._lock:
            return self._is_available_unlocked(channel, chat_id, user_id)

    def acquire_sync(
        self,
        channel: str,
        chat_id: str,
        user_id: str,
    ) -> Optional[str]:
        """
        Synchronously try to acquire a concurrency slot.

        Args:
            channel: Channel name
            chat_id: Chat identifier
            user_id: User identifier

        Returns:
            Slot ID if acquired, None if not available
        """
        with self._lock:
            if not self._is_available_unlocked(channel, chat_id, user_id):
                self._stats.total_rejected += 1
                return None

            # Acquire slot
            slot_id = self._make_slot_id(channel, chat_id, user_id)
            slot = ConcurrencySlot(
                slot_id=slot_id,
                channel=channel,
                chat_id=chat_id,
                user_id=user_id,
            )

            self._slots[slot_id] = slot

            # Update indexes
            if channel not in self._by_channel:
                self._by_channel[channel] = set()
            self._by_channel[channel].add(slot_id)

            chat_key = self._get_chat_key(channel, chat_id)
            if chat_key not in self._by_chat:
                self._by_chat[chat_key] = set()
            self._by_chat[chat_key].add(slot_id)

            if user_id not in self._by_user:
                self._by_user[user_id] = set()
            self._by_user[user_id].add(slot_id)

            self._stats.total_acquired += 1
            return slot_id

    async def acquire(
        self,
        channel: str,
        chat_id: str,
        user_id: str,
        wait: bool = False,
        timeout: Optional[float] = None,
    ) -> Optional[str]:
        """
        Try to acquire a concurrency slot.

        Args:
            channel: Channel name
            chat_id: Chat identifier
            user_id: User identifier
            wait: Whether to wait for a slot if not available
            timeout: Maximum wait time in seconds

        Returns:
            Slot ID if acquired, None if not available or timed out
        """
        # Try immediate acquire
        slot_id = self.acquire_sync(channel, chat_id, user_id)
        if slot_id:
            return slot_id

        if not wait:
            return None

        # Wait for availability
        wait_key = self._get_chat_key(channel, chat_id)
        event = asyncio.Event()

        with self._lock:
            self._waiters[wait_key] = event
            self._stats.total_queued += 1

        try:
            wait_timeout = timeout or self.limits.timeout_seconds
            await asyncio.wait_for(event.wait(), timeout=wait_timeout)
            return self.acquire_sync(channel, chat_id, user_id)
        except asyncio.TimeoutError:
            return None
        finally:
            with self._lock:
                self._waiters.pop(wait_key, None)

    def release(
        self,
        slot_id: Optional[str] = None,
        channel: Optional[str] = None,
        chat_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> bool:
        """
        Release a concurrency slot.

        Can release by slot_id or by channel/chat_id/user_id combination.

        Args:
            slot_id: Slot ID to release
            channel: Channel name
            chat_id: Chat identifier
            user_id: User identifier

        Returns:
            True if released, False if not found
        """
        with self._lock:
            # Find slot to release
            if slot_id and slot_id in self._slots:
                slot = self._slots.pop(slot_id)
            elif channel and chat_id and user_id:
                # Find slot by attributes
                for sid, s in list(self._slots.items()):
                    if s.channel == channel and s.chat_id == chat_id and s.user_id == user_id:
                        slot = self._slots.pop(sid)
                        slot_id = sid
                        break
                else:
                    return False
            else:
                return False

            # Update indexes
            if slot.channel in self._by_channel:
                self._by_channel[slot.channel].discard(slot_id)
                if not self._by_channel[slot.channel]:
                    del self._by_channel[slot.channel]

            chat_key = self._get_chat_key(slot.channel, slot.chat_id)
            if chat_key in self._by_chat:
                self._by_chat[chat_key].discard(slot_id)
                if not self._by_chat[chat_key]:
                    del self._by_chat[chat_key]

            if slot.user_id in self._by_user:
                self._by_user[slot.user_id].discard(slot_id)
                if not self._by_user[slot.user_id]:
                    del self._by_user[slot.user_id]

            self._stats.total_released += 1

            # Notify waiters
            if chat_key in self._waiters:
                self._waiters[chat_key].set()

            return True

    def release_all_for_user(self, user_id: str) -> int:
        """Release all slots for a user."""
        with self._lock:
            slot_ids = list(self._by_user.get(user_id, set()))

        released = 0
        for slot_id in slot_ids:
            if self.release(slot_id=slot_id):
                released += 1
        return released

    def release_all_for_channel(self, channel: str) -> int:
        """Release all slots for a channel."""
        with self._lock:
            slot_ids = list(self._by_channel.get(channel, set()))

        released = 0
        for slot_id in slot_ids:
            if self.release(slot_id=slot_id):
                released += 1
        return released

    def release_all_for_chat(self, channel: str, chat_id: str) -> int:
        """Release all slots for a specific chat."""
        chat_key = self._get_chat_key(channel, chat_id)
        with self._lock:
            slot_ids = list(self._by_chat.get(chat_key, set()))

        released = 0
        for slot_id in slot_ids:
            if self.release(slot_id=slot_id):
                released += 1
        return released

    def get_usage(self) -> ConcurrencyStats:
        """Get current concurrency usage statistics."""
        with self._lock:
            stats = ConcurrencyStats(
                current_global=len(self._slots),
                current_by_channel={k: len(v) for k, v in self._by_channel.items()},
                current_by_chat={k: len(v) for k, v in self._by_chat.items()},
                current_by_user={k: len(v) for k, v in self._by_user.items()},
                total_acquired=self._stats.total_acquired,
                total_rejected=self._stats.total_rejected,
                total_queued=self._stats.total_queued,
                total_released=self._stats.total_released,
            )
            return stats

    def get_slot_count(
        self,
        channel: Optional[str] = None,
        chat_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> int:
        """Get current slot count with optional filtering."""
        with self._lock:
            if user_id:
                return len(self._by_user.get(user_id, set()))
            if channel and chat_id:
                chat_key = self._get_chat_key(channel, chat_id)
                return len(self._by_chat.get(chat_key, set()))
            if channel:
                return len(self._by_channel.get(channel, set()))
            return len(self._slots)

    def cleanup_stale(self, max_age_seconds: Optional[int] = None) -> int:
        """
        Remove stale slots that have been held too long.

        Args:
            max_age_seconds: Maximum slot age (defaults to timeout_seconds)

        Returns:
            Number of slots released
        """
        max_age = max_age_seconds or self.limits.timeout_seconds
        cutoff = datetime.now()

        with self._lock:
            stale_ids = []
            for slot_id, slot in self._slots.items():
                age = (cutoff - slot.acquired_at).total_seconds()
                if age > max_age:
                    stale_ids.append(slot_id)

        released = 0
        for slot_id in stale_ids:
            if self.release(slot_id=slot_id):
                released += 1

        return released

    def clear(self) -> int:
        """Clear all slots."""
        with self._lock:
            count = len(self._slots)
            self._slots.clear()
            self._by_channel.clear()
            self._by_chat.clear()
            self._by_user.clear()
            return count
