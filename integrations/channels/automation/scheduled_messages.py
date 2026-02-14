"""
Scheduled Message Manager for HevolveBot Integration.

Provides scheduling and management of delayed messages.
"""

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Callable
import threading


class MessageStatus(Enum):
    """Status of a scheduled message."""
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RecurrenceType(Enum):
    """Types of message recurrence."""
    NONE = "none"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    CUSTOM = "custom"


@dataclass
class ScheduledMessage:
    """A scheduled message."""
    id: str
    channel_id: str
    content: str
    scheduled_time: datetime
    status: MessageStatus = MessageStatus.PENDING
    sender_id: Optional[str] = None
    thread_id: Optional[str] = None
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    recurrence: RecurrenceType = RecurrenceType.NONE
    recurrence_interval: Optional[int] = None
    recurrence_end: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)
    sent_at: Optional[datetime] = None
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3


@dataclass
class MessageDeliveryResult:
    """Result of a message delivery attempt."""
    message_id: str
    success: bool
    delivered_at: Optional[datetime] = None
    error: Optional[str] = None
    response: Optional[Dict[str, Any]] = None


class ScheduledMessageManager:
    """
    Manages scheduled messages.

    Features:
    - Schedule messages for future delivery
    - Support for recurring messages
    - Cancel pending messages
    - Track delivery status
    - Retry failed deliveries
    """

    def __init__(self, delivery_handler: Optional[Callable[[ScheduledMessage], bool]] = None):
        """
        Initialize the ScheduledMessageManager.

        Args:
            delivery_handler: Optional function to actually deliver messages
        """
        self._messages: Dict[str, ScheduledMessage] = {}
        self._lock = threading.Lock()
        self._delivery_handler = delivery_handler
        self._delivery_history: List[MessageDeliveryResult] = []

    def schedule(
        self,
        channel_id: str,
        content: str,
        scheduled_time: datetime,
        message_id: Optional[str] = None,
        sender_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        recurrence: RecurrenceType = RecurrenceType.NONE,
        recurrence_interval: Optional[int] = None,
        recurrence_end: Optional[datetime] = None
    ) -> ScheduledMessage:
        """
        Schedule a message for future delivery.

        Args:
            channel_id: The channel to send to
            content: The message content
            scheduled_time: When to send the message
            message_id: Optional custom message ID
            sender_id: Optional sender ID
            thread_id: Optional thread ID for replies
            attachments: Optional list of attachments
            metadata: Optional metadata
            recurrence: Recurrence type
            recurrence_interval: Days between recurrences
            recurrence_end: When to stop recurring

        Returns:
            The scheduled message

        Raises:
            ValueError: If scheduled_time is in the past
        """
        if scheduled_time < datetime.now():
            raise ValueError("Cannot schedule message in the past")

        message_id = message_id or f"msg_{secrets.token_hex(8)}"

        if message_id in self._messages:
            raise ValueError(f"Message with ID '{message_id}' already exists")

        message = ScheduledMessage(
            id=message_id,
            channel_id=channel_id,
            content=content,
            scheduled_time=scheduled_time,
            sender_id=sender_id,
            thread_id=thread_id,
            attachments=attachments or [],
            metadata=metadata or {},
            recurrence=recurrence,
            recurrence_interval=recurrence_interval,
            recurrence_end=recurrence_end
        )

        with self._lock:
            self._messages[message_id] = message

        return message

    def schedule_relative(
        self,
        channel_id: str,
        content: str,
        delay_seconds: int = 0,
        delay_minutes: int = 0,
        delay_hours: int = 0,
        delay_days: int = 0,
        **kwargs
    ) -> ScheduledMessage:
        """
        Schedule a message with a relative delay.

        Args:
            channel_id: The channel to send to
            content: The message content
            delay_seconds: Seconds from now
            delay_minutes: Minutes from now
            delay_hours: Hours from now
            delay_days: Days from now
            **kwargs: Additional arguments for schedule()

        Returns:
            The scheduled message
        """
        delay = timedelta(
            seconds=delay_seconds,
            minutes=delay_minutes,
            hours=delay_hours,
            days=delay_days
        )

        scheduled_time = datetime.now() + delay
        return self.schedule(channel_id, content, scheduled_time, **kwargs)

    def cancel(self, message_id: str) -> bool:
        """
        Cancel a scheduled message.

        Args:
            message_id: The message ID to cancel

        Returns:
            True if cancelled, False if not found or already sent
        """
        with self._lock:
            if message_id in self._messages:
                message = self._messages[message_id]
                if message.status == MessageStatus.PENDING:
                    message.status = MessageStatus.CANCELLED
                    return True
        return False

    def reschedule(
        self,
        message_id: str,
        new_time: datetime
    ) -> Optional[ScheduledMessage]:
        """
        Reschedule a pending message.

        Args:
            message_id: The message ID
            new_time: The new scheduled time

        Returns:
            The updated message or None if not found/not pending
        """
        if new_time < datetime.now():
            raise ValueError("Cannot reschedule to the past")

        with self._lock:
            if message_id in self._messages:
                message = self._messages[message_id]
                if message.status == MessageStatus.PENDING:
                    message.scheduled_time = new_time
                    return message
        return None

    def update_content(
        self,
        message_id: str,
        content: str
    ) -> Optional[ScheduledMessage]:
        """
        Update the content of a pending message.

        Args:
            message_id: The message ID
            content: The new content

        Returns:
            The updated message or None if not found/not pending
        """
        with self._lock:
            if message_id in self._messages:
                message = self._messages[message_id]
                if message.status == MessageStatus.PENDING:
                    message.content = content
                    return message
        return None

    def get_message(self, message_id: str) -> Optional[ScheduledMessage]:
        """
        Get a scheduled message by ID.

        Args:
            message_id: The message ID

        Returns:
            The message or None
        """
        return self._messages.get(message_id)

    def list_pending(
        self,
        channel_id: Optional[str] = None,
        before: Optional[datetime] = None,
        after: Optional[datetime] = None
    ) -> List[ScheduledMessage]:
        """
        List pending scheduled messages.

        Args:
            channel_id: Optional filter by channel
            before: Optional filter by scheduled time (before)
            after: Optional filter by scheduled time (after)

        Returns:
            List of pending messages
        """
        with self._lock:
            messages = [
                m for m in self._messages.values()
                if m.status == MessageStatus.PENDING
            ]

        if channel_id:
            messages = [m for m in messages if m.channel_id == channel_id]

        if before:
            messages = [m for m in messages if m.scheduled_time < before]

        if after:
            messages = [m for m in messages if m.scheduled_time > after]

        # Sort by scheduled time
        messages.sort(key=lambda m: m.scheduled_time)

        return messages

    def list_all(
        self,
        status: Optional[MessageStatus] = None,
        channel_id: Optional[str] = None,
        limit: int = 100
    ) -> List[ScheduledMessage]:
        """
        List all scheduled messages.

        Args:
            status: Optional filter by status
            channel_id: Optional filter by channel
            limit: Maximum number to return

        Returns:
            List of messages
        """
        with self._lock:
            messages = list(self._messages.values())

        if status:
            messages = [m for m in messages if m.status == status]

        if channel_id:
            messages = [m for m in messages if m.channel_id == channel_id]

        # Sort by scheduled time (most recent first)
        messages.sort(key=lambda m: m.scheduled_time, reverse=True)

        return messages[:limit]

    def get_due_messages(self) -> List[ScheduledMessage]:
        """
        Get all messages that are due for delivery.

        Returns:
            List of due messages
        """
        now = datetime.now()

        with self._lock:
            due = [
                m for m in self._messages.values()
                if m.status == MessageStatus.PENDING and m.scheduled_time <= now
            ]

        # Sort by scheduled time (oldest first)
        due.sort(key=lambda m: m.scheduled_time)

        return due

    def deliver_due_messages(self) -> List[MessageDeliveryResult]:
        """
        Deliver all due messages.

        Returns:
            List of delivery results
        """
        due_messages = self.get_due_messages()
        results = []

        for message in due_messages:
            result = self._deliver_message(message)
            results.append(result)
            self._delivery_history.append(result)

        return results

    def _deliver_message(self, message: ScheduledMessage) -> MessageDeliveryResult:
        """
        Deliver a single message.

        Args:
            message: The message to deliver

        Returns:
            Delivery result
        """
        result = MessageDeliveryResult(
            message_id=message.id,
            success=False
        )

        try:
            if self._delivery_handler:
                success = self._delivery_handler(message)
            else:
                # Simulate successful delivery
                success = True

            if success:
                message.status = MessageStatus.SENT
                message.sent_at = datetime.now()
                result.success = True
                result.delivered_at = message.sent_at

                # Handle recurrence
                if message.recurrence != RecurrenceType.NONE:
                    self._schedule_next_recurrence(message)
            else:
                self._handle_delivery_failure(message, "Delivery failed")
                result.error = "Delivery failed"

        except Exception as e:
            self._handle_delivery_failure(message, str(e))
            result.error = str(e)

        return result

    def _handle_delivery_failure(self, message: ScheduledMessage, error: str) -> None:
        """Handle a delivery failure."""
        message.retry_count += 1
        message.error = error

        if message.retry_count >= message.max_retries:
            message.status = MessageStatus.FAILED
        else:
            # Reschedule for retry (exponential backoff)
            delay = timedelta(minutes=2 ** message.retry_count)
            message.scheduled_time = datetime.now() + delay

    def _schedule_next_recurrence(self, message: ScheduledMessage) -> None:
        """Schedule the next occurrence of a recurring message."""
        if message.recurrence == RecurrenceType.NONE:
            return

        # Calculate next time
        if message.recurrence == RecurrenceType.DAILY:
            next_time = message.scheduled_time + timedelta(days=1)
        elif message.recurrence == RecurrenceType.WEEKLY:
            next_time = message.scheduled_time + timedelta(weeks=1)
        elif message.recurrence == RecurrenceType.MONTHLY:
            # Approximate month as 30 days
            next_time = message.scheduled_time + timedelta(days=30)
        elif message.recurrence == RecurrenceType.CUSTOM and message.recurrence_interval:
            next_time = message.scheduled_time + timedelta(days=message.recurrence_interval)
        else:
            return

        # Check if we're past the end date
        if message.recurrence_end and next_time > message.recurrence_end:
            return

        # Schedule the new message
        new_id = f"{message.id}_next_{secrets.token_hex(4)}"

        new_message = ScheduledMessage(
            id=new_id,
            channel_id=message.channel_id,
            content=message.content,
            scheduled_time=next_time,
            sender_id=message.sender_id,
            thread_id=message.thread_id,
            attachments=message.attachments.copy(),
            metadata=message.metadata.copy(),
            recurrence=message.recurrence,
            recurrence_interval=message.recurrence_interval,
            recurrence_end=message.recurrence_end
        )

        with self._lock:
            self._messages[new_id] = new_message

    def retry_failed(self, message_id: str) -> Optional[ScheduledMessage]:
        """
        Retry a failed message.

        Args:
            message_id: The message ID

        Returns:
            The message if reset for retry, None otherwise
        """
        with self._lock:
            if message_id in self._messages:
                message = self._messages[message_id]
                if message.status == MessageStatus.FAILED:
                    message.status = MessageStatus.PENDING
                    message.retry_count = 0
                    message.error = None
                    message.scheduled_time = datetime.now() + timedelta(seconds=10)
                    return message
        return None

    def delete(self, message_id: str) -> bool:
        """
        Delete a scheduled message.

        Args:
            message_id: The message ID

        Returns:
            True if deleted, False if not found
        """
        with self._lock:
            if message_id in self._messages:
                del self._messages[message_id]
                return True
        return False

    def clear_sent(self) -> int:
        """
        Clear all sent messages from the manager.

        Returns:
            Number of messages cleared
        """
        with self._lock:
            sent_ids = [
                m.id for m in self._messages.values()
                if m.status == MessageStatus.SENT
            ]
            for msg_id in sent_ids:
                del self._messages[msg_id]
            return len(sent_ids)

    def get_delivery_history(
        self,
        message_id: Optional[str] = None,
        limit: int = 100
    ) -> List[MessageDeliveryResult]:
        """
        Get delivery history.

        Args:
            message_id: Optional filter by message ID
            limit: Maximum number of records

        Returns:
            List of delivery results
        """
        history = self._delivery_history.copy()

        if message_id:
            history = [h for h in history if h.message_id == message_id]

        return history[-limit:]

    def get_stats(self) -> Dict[str, Any]:
        """
        Get statistics about scheduled messages.

        Returns:
            Dictionary with message statistics
        """
        with self._lock:
            messages = list(self._messages.values())

        pending = sum(1 for m in messages if m.status == MessageStatus.PENDING)
        sent = sum(1 for m in messages if m.status == MessageStatus.SENT)
        failed = sum(1 for m in messages if m.status == MessageStatus.FAILED)
        cancelled = sum(1 for m in messages if m.status == MessageStatus.CANCELLED)

        return {
            "total": len(messages),
            "pending": pending,
            "sent": sent,
            "failed": failed,
            "cancelled": cancelled,
            "deliveries": len(self._delivery_history)
        }
