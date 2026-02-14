"""
Trigger Manager for HevolveBot Integration.

Provides event-based trigger registration and evaluation.
"""

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Pattern, Union


class TriggerType(Enum):
    """Types of triggers that can be registered."""
    MESSAGE_RECEIVED = "message_received"
    USER_JOINED = "user_joined"
    USER_LEFT = "user_left"
    REACTION_ADDED = "reaction_added"
    FILE_SHARED = "file_shared"
    MENTION = "mention"
    KEYWORD = "keyword"
    REGEX = "regex"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
    VISUAL_MATCH = "visual_match"
    SCREEN_MATCH = "screen_match"


class TriggerPriority(Enum):
    """Priority levels for trigger execution."""
    LOW = 1
    NORMAL = 5
    HIGH = 10
    CRITICAL = 100


@dataclass
class TriggerCondition:
    """A condition that must be met for a trigger to fire."""
    field: str
    operator: str  # 'eq', 'ne', 'gt', 'lt', 'gte', 'lte', 'contains', 'startswith', 'endswith', 'matches'
    value: Any

    def evaluate(self, data: Dict[str, Any]) -> bool:
        """
        Evaluate this condition against event data.

        Args:
            data: The event data to check

        Returns:
            True if condition is met
        """
        actual = data.get(self.field)

        if actual is None:
            return False

        if self.operator == "eq":
            return actual == self.value
        elif self.operator == "ne":
            return actual != self.value
        elif self.operator == "gt":
            return actual > self.value
        elif self.operator == "lt":
            return actual < self.value
        elif self.operator == "gte":
            return actual >= self.value
        elif self.operator == "lte":
            return actual <= self.value
        elif self.operator == "contains":
            return self.value in str(actual)
        elif self.operator == "startswith":
            return str(actual).startswith(self.value)
        elif self.operator == "endswith":
            return str(actual).endswith(self.value)
        elif self.operator == "matches":
            return bool(re.match(self.value, str(actual)))
        else:
            return False


@dataclass
class Trigger:
    """A registered trigger."""
    id: str
    name: str
    trigger_type: TriggerType
    callback: Callable[[Dict[str, Any]], Any]
    enabled: bool = True
    priority: TriggerPriority = TriggerPriority.NORMAL
    conditions: List[TriggerCondition] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    pattern: Optional[str] = None
    compiled_pattern: Optional[Pattern] = None
    channel_filter: Optional[List[str]] = None
    user_filter: Optional[List[str]] = None
    cooldown_seconds: int = 0
    last_triggered: Optional[datetime] = None
    trigger_count: int = 0
    max_triggers: Optional[int] = None
    created_at: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TriggerResult:
    """Result of a trigger evaluation."""
    trigger_id: str
    trigger_name: str
    triggered: bool
    callback_result: Any = None
    error: Optional[str] = None
    execution_time_ms: float = 0.0


class TriggerManager:
    """
    Manages event-based triggers.

    Features:
    - Register triggers for various event types
    - Support for keyword and regex matching
    - Conditional trigger evaluation
    - Priority-based execution order
    - Cooldown support
    - Channel and user filtering
    """

    def __init__(self):
        """Initialize the TriggerManager."""
        self._triggers: Dict[str, Trigger] = {}
        self._type_index: Dict[TriggerType, List[str]] = {t: [] for t in TriggerType}

    def register(
        self,
        trigger_type: TriggerType,
        callback: Callable[[Dict[str, Any]], Any],
        name: Optional[str] = None,
        trigger_id: Optional[str] = None,
        priority: TriggerPriority = TriggerPriority.NORMAL,
        conditions: Optional[List[TriggerCondition]] = None,
        keywords: Optional[List[str]] = None,
        pattern: Optional[str] = None,
        channel_filter: Optional[List[str]] = None,
        user_filter: Optional[List[str]] = None,
        cooldown_seconds: int = 0,
        max_triggers: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Trigger:
        """
        Register a new trigger.

        Args:
            trigger_type: The type of event to trigger on
            callback: Function to call when trigger fires
            name: Optional trigger name
            trigger_id: Optional custom ID
            priority: Execution priority
            conditions: Optional list of conditions
            keywords: Keywords to match (for KEYWORD type)
            pattern: Regex pattern (for REGEX type)
            channel_filter: Optional list of channel IDs to match
            user_filter: Optional list of user IDs to match
            cooldown_seconds: Minimum seconds between triggers
            max_triggers: Maximum number of times to trigger
            metadata: Optional metadata

        Returns:
            The created Trigger

        Raises:
            ValueError: If required parameters are missing
        """
        trigger_id = trigger_id or f"trg_{secrets.token_hex(6)}"
        name = name or f"{trigger_type.value} trigger"

        if trigger_id in self._triggers:
            raise ValueError(f"Trigger with ID '{trigger_id}' already exists")

        # Validate type-specific requirements
        if trigger_type == TriggerType.KEYWORD and not keywords:
            raise ValueError("KEYWORD trigger requires keywords list")

        if trigger_type == TriggerType.REGEX and not pattern:
            raise ValueError("REGEX trigger requires pattern")

        # Compile regex pattern if provided
        compiled_pattern = None
        if pattern:
            try:
                compiled_pattern = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern: {e}")

        trigger = Trigger(
            id=trigger_id,
            name=name,
            trigger_type=trigger_type,
            callback=callback,
            priority=priority,
            conditions=conditions or [],
            keywords=keywords or [],
            pattern=pattern,
            compiled_pattern=compiled_pattern,
            channel_filter=channel_filter,
            user_filter=user_filter,
            cooldown_seconds=cooldown_seconds,
            max_triggers=max_triggers,
            metadata=metadata or {}
        )

        self._triggers[trigger_id] = trigger
        self._type_index[trigger_type].append(trigger_id)

        return trigger

    def unregister(self, trigger_id: str) -> bool:
        """
        Unregister a trigger.

        Args:
            trigger_id: The trigger ID to remove

        Returns:
            True if removed, False if not found
        """
        if trigger_id in self._triggers:
            trigger = self._triggers[trigger_id]
            self._type_index[trigger.trigger_type].remove(trigger_id)
            del self._triggers[trigger_id]
            return True
        return False

    def enable(self, trigger_id: str) -> bool:
        """
        Enable a trigger.

        Args:
            trigger_id: The trigger ID

        Returns:
            True if enabled, False if not found
        """
        if trigger_id in self._triggers:
            self._triggers[trigger_id].enabled = True
            return True
        return False

    def disable(self, trigger_id: str) -> bool:
        """
        Disable a trigger.

        Args:
            trigger_id: The trigger ID

        Returns:
            True if disabled, False if not found
        """
        if trigger_id in self._triggers:
            self._triggers[trigger_id].enabled = False
            return True
        return False

    def get_trigger(self, trigger_id: str) -> Optional[Trigger]:
        """
        Get a trigger by ID.

        Args:
            trigger_id: The trigger ID

        Returns:
            The trigger or None
        """
        return self._triggers.get(trigger_id)

    def list_triggers(
        self,
        trigger_type: Optional[TriggerType] = None,
        enabled_only: bool = False
    ) -> List[Trigger]:
        """
        List registered triggers.

        Args:
            trigger_type: Optional filter by type
            enabled_only: Only return enabled triggers

        Returns:
            List of matching triggers
        """
        if trigger_type:
            trigger_ids = self._type_index.get(trigger_type, [])
            triggers = [self._triggers[tid] for tid in trigger_ids]
        else:
            triggers = list(self._triggers.values())

        if enabled_only:
            triggers = [t for t in triggers if t.enabled]

        # Sort by priority (highest first)
        triggers.sort(key=lambda t: t.priority.value, reverse=True)

        return triggers

    def evaluate(
        self,
        event_type: TriggerType,
        event_data: Dict[str, Any],
        stop_on_first: bool = False
    ) -> List[TriggerResult]:
        """
        Evaluate triggers for an event.

        Args:
            event_type: The type of event
            event_data: The event data
            stop_on_first: Stop after first successful trigger

        Returns:
            List of trigger results
        """
        import time

        results = []

        # Get triggers for this event type, sorted by priority
        triggers = self.list_triggers(trigger_type=event_type, enabled_only=True)

        for trigger in triggers:
            # Check if trigger should fire
            should_fire, reason = self._should_fire(trigger, event_data)

            if not should_fire:
                continue

            # Execute the trigger
            start_time = time.time()
            result = TriggerResult(
                trigger_id=trigger.id,
                trigger_name=trigger.name,
                triggered=True
            )

            try:
                result.callback_result = trigger.callback(event_data)
                trigger.last_triggered = datetime.now()
                trigger.trigger_count += 1

                # Check if max triggers reached
                if trigger.max_triggers and trigger.trigger_count >= trigger.max_triggers:
                    trigger.enabled = False

            except Exception as e:
                result.error = str(e)

            result.execution_time_ms = (time.time() - start_time) * 1000
            results.append(result)

            if stop_on_first and result.callback_result is not None:
                break

        return results

    def _should_fire(
        self,
        trigger: Trigger,
        event_data: Dict[str, Any]
    ) -> tuple[bool, str]:
        """
        Check if a trigger should fire for given event data.

        Returns:
            Tuple of (should_fire, reason)
        """
        # Check cooldown
        if trigger.cooldown_seconds > 0 and trigger.last_triggered:
            elapsed = (datetime.now() - trigger.last_triggered).total_seconds()
            if elapsed < trigger.cooldown_seconds:
                return False, "cooldown"

        # Check max triggers
        if trigger.max_triggers and trigger.trigger_count >= trigger.max_triggers:
            return False, "max_triggers_reached"

        # Check channel filter
        if trigger.channel_filter:
            channel = event_data.get("channel_id") or event_data.get("channel")
            if channel not in trigger.channel_filter:
                return False, "channel_filter"

        # Check user filter
        if trigger.user_filter:
            user = event_data.get("user_id") or event_data.get("user")
            if user not in trigger.user_filter:
                return False, "user_filter"

        # Check conditions
        for condition in trigger.conditions:
            if not condition.evaluate(event_data):
                return False, f"condition_{condition.field}"

        # Type-specific checks
        if trigger.trigger_type == TriggerType.KEYWORD:
            message = str(event_data.get("message", "") or event_data.get("text", ""))
            if not any(kw.lower() in message.lower() for kw in trigger.keywords):
                return False, "keyword_not_found"

        elif trigger.trigger_type == TriggerType.REGEX:
            message = str(event_data.get("message", "") or event_data.get("text", ""))
            if trigger.compiled_pattern and not trigger.compiled_pattern.search(message):
                return False, "pattern_not_matched"

        elif trigger.trigger_type == TriggerType.MENTION:
            mentions = event_data.get("mentions", [])
            mentioned_user = event_data.get("mentioned_user")
            if mentioned_user and mentioned_user not in mentions:
                return False, "not_mentioned"

        elif trigger.trigger_type in (TriggerType.VISUAL_MATCH, TriggerType.SCREEN_MATCH):
            description = str(event_data.get("description", ""))
            if trigger.keywords and not any(kw.lower() in description.lower() for kw in trigger.keywords):
                return False, "visual_keyword_not_found"
            if trigger.compiled_pattern and not trigger.compiled_pattern.search(description):
                return False, "visual_pattern_not_matched"

        return True, "ok"

    def evaluate_message(
        self,
        message: str,
        channel_id: Optional[str] = None,
        user_id: Optional[str] = None,
        extra_data: Optional[Dict[str, Any]] = None
    ) -> List[TriggerResult]:
        """
        Convenience method to evaluate a message against all applicable triggers.

        Args:
            message: The message text
            channel_id: Optional channel ID
            user_id: Optional user ID
            extra_data: Optional additional event data

        Returns:
            List of trigger results
        """
        event_data = {
            "message": message,
            "text": message,
            "channel_id": channel_id,
            "user_id": user_id,
            **(extra_data or {})
        }

        results = []

        # Evaluate MESSAGE_RECEIVED triggers
        results.extend(self.evaluate(TriggerType.MESSAGE_RECEIVED, event_data))

        # Evaluate KEYWORD triggers
        results.extend(self.evaluate(TriggerType.KEYWORD, event_data))

        # Evaluate REGEX triggers
        results.extend(self.evaluate(TriggerType.REGEX, event_data))

        return results

    def reset_trigger(self, trigger_id: str) -> bool:
        """
        Reset a trigger's state (count, last_triggered, re-enable).

        Args:
            trigger_id: The trigger ID

        Returns:
            True if reset, False if not found
        """
        if trigger_id in self._triggers:
            trigger = self._triggers[trigger_id]
            trigger.trigger_count = 0
            trigger.last_triggered = None
            trigger.enabled = True
            return True
        return False

    def get_stats(self) -> Dict[str, Any]:
        """
        Get statistics about registered triggers.

        Returns:
            Dictionary with trigger statistics
        """
        total = len(self._triggers)
        enabled = sum(1 for t in self._triggers.values() if t.enabled)
        by_type = {
            t.value: len(ids) for t, ids in self._type_index.items()
        }
        total_triggers = sum(t.trigger_count for t in self._triggers.values())

        return {
            "total_triggers": total,
            "enabled_triggers": enabled,
            "disabled_triggers": total - enabled,
            "by_type": by_type,
            "total_executions": total_triggers
        }
