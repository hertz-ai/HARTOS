"""
Automation Module for HevolveBot Integration.

Provides webhook management, cron scheduling, event triggers,
workflow execution, and scheduled message delivery.
"""

from .webhooks import (
    WebhookManager,
    WebhookConfig,
    WebhookDelivery,
    WebhookStatus,
)

from .cron import (
    CronManager,
    CronJob,
    CronExpression,
    JobStatus,
    IntervalUnit,
)

from .triggers import (
    TriggerManager,
    Trigger,
    TriggerType,
    TriggerPriority,
    TriggerCondition,
    TriggerResult,
)

from .workflows import (
    WorkflowEngine,
    Workflow,
    WorkflowStep,
    WorkflowExecution,
    WorkflowStatus,
    StepType,
)

from .scheduled_messages import (
    ScheduledMessageManager,
    ScheduledMessage,
    MessageDeliveryResult,
    MessageStatus,
    RecurrenceType,
)


__all__ = [
    # Webhooks
    "WebhookManager",
    "WebhookConfig",
    "WebhookDelivery",
    "WebhookStatus",
    # Cron
    "CronManager",
    "CronJob",
    "CronExpression",
    "JobStatus",
    "IntervalUnit",
    # Triggers
    "TriggerManager",
    "Trigger",
    "TriggerType",
    "TriggerPriority",
    "TriggerCondition",
    "TriggerResult",
    # Workflows
    "WorkflowEngine",
    "Workflow",
    "WorkflowStep",
    "WorkflowExecution",
    "WorkflowStatus",
    "StepType",
    # Scheduled Messages
    "ScheduledMessageManager",
    "ScheduledMessage",
    "MessageDeliveryResult",
    "MessageStatus",
    "RecurrenceType",
]
