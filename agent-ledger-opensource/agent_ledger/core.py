"""
Agent Ledger - Smart Task Tracking System for AI Agents

A framework-agnostic task tracking system with persistent memory, designed for
autonomous AI agents. Maintains task state across sessions with support for
hierarchical tasks, dependencies, and dynamic reprioritization.

Features:
- Persistent task memory across agent sessions
- 12 comprehensive task states with full lifecycle management
- Parent-child task relationships with auto-resume
- Dynamic task reprioritization
- Pluggable storage backends (Redis, MongoDB, PostgreSQL, JSON)
- State history and audit trails
- Framework agnostic - works with any agent system
"""

import json
import os
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Any, Callable
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# Optional VLM Integration - No hard dependencies
_vlm_context_getter: Optional[Callable] = None


def enable_vlm_integration(vlm_context_getter: Callable):
    """
    Enable optional VLM (Vision-Language Model) integration.

    This allows dynamic VLM integration without hard dependencies.
    The VLM context getter should return an object with:
    - inject_visual_context_into_ledger_task(context) -> dict
    - get_visual_feedback_for_task(description) -> str

    Args:
        vlm_context_getter: Function that returns VLM context object

    Example:
        from your_vlm_module import get_vlm_context
        enable_vlm_integration(get_vlm_context)
    """
    global _vlm_context_getter
    _vlm_context_getter = vlm_context_getter
    logger.info("VLM agent integration enabled")


def disable_vlm_integration():
    """Disable VLM integration."""
    global _vlm_context_getter
    _vlm_context_getter = None
    logger.info("VLM agent integration disabled")


def is_vlm_enabled() -> bool:
    """Check if VLM integration is enabled."""
    return _vlm_context_getter is not None


class TaskType(str, Enum):
    """Types of tasks in the system"""
    PRE_ASSIGNED = "pre_assigned"  # Tasks from initial prompt/workflow
    AUTONOMOUS = "autonomous"      # Tasks created by agent autonomously
    USER_REQUESTED = "user_requested"  # Tasks from user feedback
    INTERMEDIATE = "intermediate"  # Sub-tasks created during execution


class BlockedReason(str, Enum):
    """Reasons why a task might be blocked"""
    DEPENDENCY = "dependency"                    # Waiting for prerequisite task
    INPUT_REQUIRED = "input_required"            # Needs user input
    APPROVAL_REQUIRED = "approval_required"      # Needs human approval
    RESOURCE_UNAVAILABLE = "resource_unavailable"  # No resources available
    RATE_LIMITED = "rate_limited"                # API rate limit hit
    EXTERNAL_SERVICE = "external_service"        # Waiting for external API/service
    MANUAL_BLOCK = "manual_block"                # Manually blocked by user/system


class FailureReason(str, Enum):
    """Reasons why a task might fail"""
    ERROR = "error"                              # Generic error
    TIMEOUT = "timeout"                          # Time limit exceeded
    VALIDATION_FAILED = "validation_failed"      # Output validation failed
    PERMISSION_DENIED = "permission_denied"      # Insufficient permissions
    RESOURCE_EXHAUSTED = "resource_exhausted"    # Out of memory/disk/quota
    DEPENDENCY_FAILED = "dependency_failed"      # Prerequisite task failed
    MAX_RETRIES_EXCEEDED = "max_retries_exceeded"  # Retry limit reached
    EXTERNAL_SERVICE_ERROR = "external_service_error"  # External API failed


class PendingReason(str, Enum):
    """Reasons/sub-states for pending tasks"""
    READY = "ready"                # Ready to execute immediately
    QUEUED = "queued"              # In execution queue, waiting for slot
    SCHEDULED = "scheduled"        # Scheduled for future execution
    AWAITING_PREREQUISITES = "awaiting_prerequisites"  # Prerequisites not met yet


class TaskStatus(str, Enum):
    """Task completion status with comprehensive lifecycle states (15 states)"""
    # Initial states
    PENDING = "pending"                    # Task created, waiting to start
    DEFERRED = "deferred"                  # Task intentionally postponed before starting

    # Active execution states
    IN_PROGRESS = "in_progress"           # Task actively being executed
    DELEGATED = "delegated"               # Task delegated to another agent

    # Interruption states
    PAUSED = "paused"                      # Task paused by user or system
    USER_STOPPED = "user_stopped"         # User explicitly stopped the task
    BLOCKED = "blocked"                    # Task blocked by dependencies or errors

    # Terminal states (final states)
    COMPLETED = "completed"                # Task successfully completed
    FAILED = "failed"                      # Task failed with errors
    CANCELLED = "cancelled"                # Task cancelled by user
    TERMINATED = "terminated"              # Task killed/terminated forcefully
    SKIPPED = "skipped"                    # Task skipped (not needed anymore)
    NOT_APPLICABLE = "not_applicable"      # Task no longer applicable to goal
    ROLLED_BACK = "rolled_back"            # Task was completed but then undone

    # Resume-related states
    RESUMING = "resuming"                  # Task being resumed from paused/stopped

    @classmethod
    def is_terminal_state(cls, status: 'TaskStatus') -> bool:
        """Check if a state is terminal (cannot transition from normally)"""
        terminal_states = {
            cls.COMPLETED, cls.FAILED, cls.CANCELLED,
            cls.TERMINATED, cls.SKIPPED, cls.NOT_APPLICABLE,
            cls.ROLLED_BACK
        }
        return status in terminal_states

    @classmethod
    def is_active_state(cls, status: 'TaskStatus') -> bool:
        """Check if a state represents active work"""
        active_states = {cls.IN_PROGRESS, cls.RESUMING, cls.DELEGATED}
        return status in active_states

    @classmethod
    def is_paused_state(cls, status: 'TaskStatus') -> bool:
        """Check if a state represents paused work"""
        paused_states = {cls.PAUSED, cls.USER_STOPPED, cls.BLOCKED}
        return status in paused_states

    @classmethod
    def is_initial_state(cls, status: 'TaskStatus') -> bool:
        """Check if a state represents initial/not-started"""
        initial_states = {cls.PENDING, cls.DEFERRED}
        return status in initial_states

    @classmethod
    def is_delegated_state(cls, status: 'TaskStatus') -> bool:
        """Check if task is delegated to another agent"""
        return status == cls.DELEGATED

    @classmethod
    def can_rollback(cls, status: 'TaskStatus') -> bool:
        """Check if a completed task can be rolled back"""
        return status == cls.COMPLETED


class ExecutionMode(str, Enum):
    """How task should be executed"""
    PARALLEL = "parallel"      # Can run concurrently with others
    SEQUENTIAL = "sequential"  # Must wait for prerequisites


class Task:
    """Individual task representation with full lifecycle management."""

    def __init__(
        self,
        task_id: str,
        description: str,
        task_type: TaskType,
        execution_mode: ExecutionMode = ExecutionMode.SEQUENTIAL,
        status: TaskStatus = TaskStatus.PENDING,
        prerequisites: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
        priority: int = 50,
        parent_task_id: Optional[str] = None
    ):
        self.task_id = task_id
        self.description = description
        self.task_type = task_type
        self.execution_mode = execution_mode
        self.status = status
        self.prerequisites = prerequisites or []
        self.context = context or {}
        self.priority = priority  # 0-100, higher = more important
        self.parent_task_id = parent_task_id
        self.created_at = datetime.now().isoformat()
        self.updated_at = self.created_at
        self.completed_at: Optional[str] = None
        self.error_message: Optional[str] = None
        self.result: Optional[Any] = None

        # State management - track history of all state transitions
        self.state_history: List[Dict[str, Any]] = [{
            "status": status,
            "timestamp": self.created_at,
            "reason": "Task created"
        }]

        # Track pause/resume information
        self.paused_at: Optional[str] = None
        self.resumed_at: Optional[str] = None
        self.pause_count: int = 0
        self.stop_reason: Optional[str] = None
        self.termination_reason: Optional[str] = None

        # Reason tracking for sub-states
        self.blocked_reason: Optional[str] = None  # BlockedReason value
        self.failure_reason: Optional[str] = None  # FailureReason value
        self.pending_reason: Optional[str] = None  # PendingReason value

        # Delegation tracking
        self.delegated_to: Optional[str] = None    # Agent ID task is delegated to
        self.delegated_at: Optional[str] = None    # When delegation happened
        self.delegation_type: Optional[str] = None  # "sub_agent", "escalation", "handoff"
        self.delegation_result: Optional[Any] = None  # Result from delegated agent

        # Deferred/Scheduling tracking
        self.deferred_at: Optional[str] = None     # When task was deferred
        self.deferred_until: Optional[str] = None  # Target date to resume
        self.deferred_reason: Optional[str] = None # Why task was deferred
        self.scheduled_at: Optional[str] = None    # Scheduled execution time

        # Retry tracking
        self.retry_count: int = 0                  # Number of retry attempts
        self.max_retries: int = 3                  # Maximum retry attempts
        self.last_retry_at: Optional[str] = None   # Last retry timestamp
        self.retry_errors: List[str] = []          # Errors from each retry

        # Progress tracking
        self.progress_pct: float = 0.0             # 0-100 progress percentage
        self.checkpoints: List[Dict[str, Any]] = []  # Completed checkpoints

        # Rollback tracking
        self.rolled_back_at: Optional[str] = None  # When rollback happened
        self.rollback_reason: Optional[str] = None # Why it was rolled back
        self.original_result: Optional[Any] = None # Result before rollback

        # Nested task management
        self.child_task_ids: List[str] = []  # Direct children of this task
        self.sibling_task_ids: List[str] = []  # Sibling tasks (same parent)
        self.dependent_task_ids: List[str] = []  # Tasks waiting on this one

        # Inter-task communication
        self.messages_to_dependents: List[Dict[str, Any]] = []
        self.received_messages: List[Dict[str, Any]] = []

        # Dependency tracking
        self.blocked_by: List[str] = []  # Task IDs blocking this task

    def to_dict(self) -> Dict[str, Any]:
        """Convert task to dictionary for serialization."""
        return {
            "task_id": self.task_id,
            "description": self.description,
            "task_type": self.task_type,
            "execution_mode": self.execution_mode,
            "status": self.status,
            "prerequisites": self.prerequisites,
            "context": self.context,
            "priority": self.priority,
            "parent_task_id": self.parent_task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error_message": self.error_message,
            "result": self.result,
            "state_history": self.state_history,
            "paused_at": self.paused_at,
            "resumed_at": self.resumed_at,
            "pause_count": self.pause_count,
            "stop_reason": self.stop_reason,
            "termination_reason": self.termination_reason,
            "child_task_ids": self.child_task_ids,
            "sibling_task_ids": self.sibling_task_ids,
            "dependent_task_ids": self.dependent_task_ids,
            "messages_to_dependents": self.messages_to_dependents,
            "received_messages": self.received_messages,
            "blocked_by": self.blocked_by,
            # Reason tracking
            "blocked_reason": self.blocked_reason,
            "failure_reason": self.failure_reason,
            "pending_reason": self.pending_reason,
            # Delegation
            "delegated_to": self.delegated_to,
            "delegated_at": self.delegated_at,
            "delegation_type": self.delegation_type,
            "delegation_result": self.delegation_result,
            # Deferred/Scheduling
            "deferred_at": self.deferred_at,
            "deferred_until": self.deferred_until,
            "deferred_reason": self.deferred_reason,
            "scheduled_at": self.scheduled_at,
            # Retry
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "last_retry_at": self.last_retry_at,
            "retry_errors": self.retry_errors,
            # Progress
            "progress_pct": self.progress_pct,
            "checkpoints": self.checkpoints,
            # Rollback
            "rolled_back_at": self.rolled_back_at,
            "rollback_reason": self.rollback_reason,
            "original_result": self.original_result
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Task':
        """Create task from dictionary."""
        task = cls(
            task_id=data["task_id"],
            description=data["description"],
            task_type=TaskType(data["task_type"]),
            execution_mode=ExecutionMode(data["execution_mode"]),
            status=TaskStatus(data["status"]),
            prerequisites=data.get("prerequisites", []),
            context=data.get("context", {}),
            priority=data.get("priority", 50),
            parent_task_id=data.get("parent_task_id")
        )
        task.created_at = data.get("created_at", task.created_at)
        task.updated_at = data.get("updated_at", task.updated_at)
        task.completed_at = data.get("completed_at")
        task.error_message = data.get("error_message")
        task.result = data.get("result")
        task.state_history = data.get("state_history", task.state_history)
        task.paused_at = data.get("paused_at")
        task.resumed_at = data.get("resumed_at")
        task.pause_count = data.get("pause_count", 0)
        task.stop_reason = data.get("stop_reason")
        task.termination_reason = data.get("termination_reason")
        task.child_task_ids = data.get("child_task_ids", [])
        task.sibling_task_ids = data.get("sibling_task_ids", [])
        task.dependent_task_ids = data.get("dependent_task_ids", [])
        task.messages_to_dependents = data.get("messages_to_dependents", [])
        task.received_messages = data.get("received_messages", [])
        task.blocked_by = data.get("blocked_by", [])
        # Reason tracking
        task.blocked_reason = data.get("blocked_reason")
        task.failure_reason = data.get("failure_reason")
        task.pending_reason = data.get("pending_reason")
        # Delegation
        task.delegated_to = data.get("delegated_to")
        task.delegated_at = data.get("delegated_at")
        task.delegation_type = data.get("delegation_type")
        task.delegation_result = data.get("delegation_result")
        # Deferred/Scheduling
        task.deferred_at = data.get("deferred_at")
        task.deferred_until = data.get("deferred_until")
        task.deferred_reason = data.get("deferred_reason")
        task.scheduled_at = data.get("scheduled_at")
        # Retry
        task.retry_count = data.get("retry_count", 0)
        task.max_retries = data.get("max_retries", 3)
        task.last_retry_at = data.get("last_retry_at")
        task.retry_errors = data.get("retry_errors", [])
        # Progress
        task.progress_pct = data.get("progress_pct", 0.0)
        task.checkpoints = data.get("checkpoints", [])
        # Rollback
        task.rolled_back_at = data.get("rolled_back_at")
        task.rollback_reason = data.get("rollback_reason")
        task.original_result = data.get("original_result")
        return task

    def inject_vlm_context(self):
        """
        Inject VLM agent visual context into this task's context.

        Only works if VLM integration has been enabled via enable_vlm_integration().
        """
        if _vlm_context_getter is not None:
            try:
                vlm = _vlm_context_getter()
                self.context = vlm.inject_visual_context_into_ledger_task(self.context)
                self.updated_at = datetime.now().isoformat()
                logger.info(f"VLM context injected into task {self.task_id}")
            except Exception as e:
                logger.error(f"Failed to inject VLM context: {e}")
                self.context["vlm_error"] = str(e)
        else:
            logger.debug(f"VLM integration not enabled, skipping for task {self.task_id}")

    def get_visual_feedback(self) -> str:
        """
        Get visual feedback from VLM agent for this task.

        Returns:
            Human-readable visual feedback about current screen state
        """
        if _vlm_context_getter is not None:
            try:
                vlm = _vlm_context_getter()
                return vlm.get_visual_feedback_for_task(self.description)
            except Exception as e:
                logger.error(f"Failed to get visual feedback: {e}")
                return f"Visual feedback unavailable: {e}"
        return "VLM integration not enabled"

    # ==================== State Transition Methods ====================

    def _record_state_transition(self, new_status: TaskStatus, reason: str):
        """Internal method to record a state transition in history."""
        self.state_history.append({
            "status": new_status,
            "timestamp": datetime.now().isoformat(),
            "reason": reason,
            "previous_status": self.status
        })
        self.status = new_status
        self.updated_at = datetime.now().isoformat()
        logger.info(f"Task {self.task_id} transitioned to {new_status}: {reason}")

    def _validate_transition(self, new_status: TaskStatus) -> bool:
        """Validate if transition from current state to new state is allowed."""
        # Special case: COMPLETED can transition to ROLLED_BACK
        if self.status == TaskStatus.COMPLETED and new_status == TaskStatus.ROLLED_BACK:
            return True

        if TaskStatus.is_terminal_state(self.status):
            logger.warning(f"Cannot transition from terminal state {self.status} to {new_status}")
            return False

        valid_transitions = {
            TaskStatus.PENDING: {
                TaskStatus.IN_PROGRESS, TaskStatus.PAUSED, TaskStatus.CANCELLED,
                TaskStatus.SKIPPED, TaskStatus.NOT_APPLICABLE, TaskStatus.DEFERRED,
                TaskStatus.DELEGATED
            },
            TaskStatus.DEFERRED: {
                TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED,
                TaskStatus.SKIPPED, TaskStatus.NOT_APPLICABLE
            },
            TaskStatus.IN_PROGRESS: {
                TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PAUSED,
                TaskStatus.USER_STOPPED, TaskStatus.BLOCKED, TaskStatus.TERMINATED,
                TaskStatus.NOT_APPLICABLE, TaskStatus.DELEGATED
            },
            TaskStatus.DELEGATED: {
                TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.IN_PROGRESS,
                TaskStatus.CANCELLED, TaskStatus.BLOCKED
            },
            TaskStatus.PAUSED: {
                TaskStatus.RESUMING, TaskStatus.CANCELLED, TaskStatus.TERMINATED,
                TaskStatus.NOT_APPLICABLE, TaskStatus.SKIPPED, TaskStatus.DEFERRED
            },
            TaskStatus.USER_STOPPED: {
                TaskStatus.RESUMING, TaskStatus.CANCELLED, TaskStatus.TERMINATED,
                TaskStatus.NOT_APPLICABLE, TaskStatus.SKIPPED, TaskStatus.DEFERRED
            },
            TaskStatus.BLOCKED: {
                TaskStatus.PENDING, TaskStatus.RESUMING, TaskStatus.FAILED,
                TaskStatus.CANCELLED, TaskStatus.NOT_APPLICABLE, TaskStatus.DEFERRED
            },
            TaskStatus.RESUMING: {
                TaskStatus.IN_PROGRESS, TaskStatus.PAUSED, TaskStatus.FAILED
            }
        }

        allowed_states = valid_transitions.get(self.status, set())
        if new_status not in allowed_states:
            logger.warning(f"Invalid transition from {self.status} to {new_status}")
            return False

        return True

    def start(self, reason: str = "Task execution started") -> bool:
        """Start task execution (PENDING -> IN_PROGRESS)."""
        if not self._validate_transition(TaskStatus.IN_PROGRESS):
            return False
        self._record_state_transition(TaskStatus.IN_PROGRESS, reason)
        return True

    def complete(self, result: Any = None, reason: str = "Task completed successfully") -> bool:
        """Mark task as completed (IN_PROGRESS -> COMPLETED)."""
        if not self._validate_transition(TaskStatus.COMPLETED):
            return False
        self.result = result
        self.completed_at = datetime.now().isoformat()
        self._record_state_transition(TaskStatus.COMPLETED, reason)
        return True

    def fail(self, error: str, reason: str = "Task failed") -> bool:
        """Mark task as failed (IN_PROGRESS/BLOCKED -> FAILED)."""
        if not self._validate_transition(TaskStatus.FAILED):
            return False
        self.error_message = error
        self.completed_at = datetime.now().isoformat()
        self._record_state_transition(TaskStatus.FAILED, f"{reason}: {error}")
        return True

    def pause(self, reason: str = "Task paused by system") -> bool:
        """Pause task execution (IN_PROGRESS -> PAUSED)."""
        if not self._validate_transition(TaskStatus.PAUSED):
            return False
        self.paused_at = datetime.now().isoformat()
        self.pause_count += 1
        self._record_state_transition(TaskStatus.PAUSED, reason)
        return True

    def user_stop(self, reason: str = "User stopped task") -> bool:
        """User explicitly stopped task (IN_PROGRESS -> USER_STOPPED)."""
        if not self._validate_transition(TaskStatus.USER_STOPPED):
            return False
        self.paused_at = datetime.now().isoformat()
        self.stop_reason = reason
        self._record_state_transition(TaskStatus.USER_STOPPED, reason)
        return True

    def block(self, reason: str = "Task blocked by dependencies") -> bool:
        """Block task due to dependencies (IN_PROGRESS -> BLOCKED)."""
        if not self._validate_transition(TaskStatus.BLOCKED):
            return False
        self.error_message = reason
        self._record_state_transition(TaskStatus.BLOCKED, reason)
        return True

    def resume(self, reason: str = "Task resumed") -> bool:
        """Resume task from paused/stopped state."""
        if not self._validate_transition(TaskStatus.RESUMING):
            return False
        self.resumed_at = datetime.now().isoformat()
        self._record_state_transition(TaskStatus.RESUMING, reason)
        self._record_state_transition(TaskStatus.IN_PROGRESS, "Resumed to active execution")
        return True

    def cancel(self, reason: str = "Task cancelled by user") -> bool:
        """Cancel task."""
        if not self._validate_transition(TaskStatus.CANCELLED):
            return False
        self.completed_at = datetime.now().isoformat()
        self._record_state_transition(TaskStatus.CANCELLED, reason)
        return True

    def terminate(self, reason: str = "Task terminated/killed") -> bool:
        """Forcefully terminate task."""
        if not self._validate_transition(TaskStatus.TERMINATED):
            return False
        self.termination_reason = reason
        self.completed_at = datetime.now().isoformat()
        self._record_state_transition(TaskStatus.TERMINATED, reason)
        return True

    def skip(self, reason: str = "Task skipped") -> bool:
        """Skip task as it's not needed."""
        if not self._validate_transition(TaskStatus.SKIPPED):
            return False
        self.completed_at = datetime.now().isoformat()
        self._record_state_transition(TaskStatus.SKIPPED, reason)
        return True

    def mark_not_applicable(self, reason: str = "Task no longer applicable") -> bool:
        """Mark task as not applicable anymore."""
        if not self._validate_transition(TaskStatus.NOT_APPLICABLE):
            return False
        self.completed_at = datetime.now().isoformat()
        self._record_state_transition(TaskStatus.NOT_APPLICABLE, reason)
        return True

    # ==================== New State Transition Methods ====================

    def defer(self, reason: str = "Task deferred", until: Optional[str] = None) -> bool:
        """
        Defer task for later execution (PENDING/PAUSED/BLOCKED -> DEFERRED).

        Args:
            reason: Why the task is being deferred
            until: Optional target date/time to resume (ISO format)

        Returns:
            True if transition successful
        """
        if not self._validate_transition(TaskStatus.DEFERRED):
            return False
        self.deferred_at = datetime.now().isoformat()
        self.deferred_reason = reason
        self.deferred_until = until
        self._record_state_transition(TaskStatus.DEFERRED, reason)
        return True

    def undefer(self, reason: str = "Task undeferred") -> bool:
        """
        Undefer a deferred task back to pending (DEFERRED -> PENDING).

        Returns:
            True if transition successful
        """
        if self.status != TaskStatus.DEFERRED:
            logger.warning(f"Cannot undefer task not in DEFERRED state: {self.status}")
            return False
        self._record_state_transition(TaskStatus.PENDING, reason)
        return True

    def delegate(
        self,
        to_agent_id: str,
        delegation_type: str = "sub_agent",
        reason: str = "Task delegated"
    ) -> bool:
        """
        Delegate task to another agent (PENDING/IN_PROGRESS -> DELEGATED).

        Args:
            to_agent_id: ID of agent receiving the task
            delegation_type: Type of delegation ("sub_agent", "escalation", "handoff")
            reason: Why the task is being delegated

        Returns:
            True if transition successful
        """
        if not self._validate_transition(TaskStatus.DELEGATED):
            return False
        self.delegated_to = to_agent_id
        self.delegated_at = datetime.now().isoformat()
        self.delegation_type = delegation_type
        self._record_state_transition(TaskStatus.DELEGATED, f"{reason} to {to_agent_id}")
        return True

    def complete_delegation(self, result: Any = None, reason: str = "Delegation completed") -> bool:
        """
        Complete a delegated task with result from delegate (DELEGATED -> COMPLETED).

        Args:
            result: Result returned by the delegated agent
            reason: Completion reason

        Returns:
            True if transition successful
        """
        if self.status != TaskStatus.DELEGATED:
            logger.warning(f"Cannot complete delegation for non-delegated task: {self.status}")
            return False
        self.delegation_result = result
        self.result = result
        self.completed_at = datetime.now().isoformat()
        self._record_state_transition(TaskStatus.COMPLETED, reason)
        return True

    def reclaim_delegation(self, reason: str = "Delegation reclaimed") -> bool:
        """
        Reclaim a delegated task back to in-progress (DELEGATED -> IN_PROGRESS).

        Returns:
            True if transition successful
        """
        if self.status != TaskStatus.DELEGATED:
            logger.warning(f"Cannot reclaim non-delegated task: {self.status}")
            return False
        self._record_state_transition(TaskStatus.IN_PROGRESS, reason)
        return True

    def rollback(self, reason: str = "Task rolled back") -> bool:
        """
        Rollback a completed task (COMPLETED -> ROLLED_BACK).

        Preserves the original result for reference.

        Returns:
            True if transition successful
        """
        if self.status != TaskStatus.COMPLETED:
            logger.warning(f"Cannot rollback non-completed task: {self.status}")
            return False
        self.original_result = self.result
        self.result = None
        self.rolled_back_at = datetime.now().isoformat()
        self.rollback_reason = reason
        self._record_state_transition(TaskStatus.ROLLED_BACK, reason)
        return True

    def update_progress(self, progress_pct: float, checkpoint: Optional[str] = None) -> None:
        """
        Update task progress percentage and optionally add a checkpoint.

        Args:
            progress_pct: Progress percentage (0-100)
            checkpoint: Optional checkpoint description
        """
        self.progress_pct = max(0.0, min(100.0, progress_pct))
        if checkpoint:
            self.checkpoints.append({
                "description": checkpoint,
                "progress": self.progress_pct,
                "timestamp": datetime.now().isoformat()
            })
        self.updated_at = datetime.now().isoformat()
        logger.debug(f"Task {self.task_id} progress: {self.progress_pct}%")

    def record_retry(self, error: str) -> bool:
        """
        Record a retry attempt after failure.

        Args:
            error: Error message from the failed attempt

        Returns:
            True if retry allowed, False if max retries exceeded
        """
        self.retry_count += 1
        self.last_retry_at = datetime.now().isoformat()
        self.retry_errors.append(error)

        if self.retry_count > self.max_retries:
            self.failure_reason = "max_retries_exceeded"
            logger.warning(f"Task {self.task_id} exceeded max retries ({self.max_retries})")
            return False

        logger.info(f"Task {self.task_id} retry {self.retry_count}/{self.max_retries}")
        return True

    def set_blocked_reason(self, reason: str) -> None:
        """Set the specific reason for blocking."""
        self.blocked_reason = reason
        self.updated_at = datetime.now().isoformat()

    def set_failure_reason(self, reason: str) -> None:
        """Set the specific reason for failure."""
        self.failure_reason = reason
        self.updated_at = datetime.now().isoformat()

    def set_pending_reason(self, reason: str) -> None:
        """Set the specific reason/sub-state for pending."""
        self.pending_reason = reason
        self.updated_at = datetime.now().isoformat()

    def schedule(self, scheduled_time: str, reason: str = "Task scheduled") -> bool:
        """
        Schedule a pending task for future execution.

        Args:
            scheduled_time: ISO format datetime for execution
            reason: Why the task is being scheduled

        Returns:
            True if scheduling successful
        """
        if self.status not in (TaskStatus.PENDING, TaskStatus.DEFERRED):
            logger.warning(f"Cannot schedule task in state: {self.status}")
            return False
        self.scheduled_at = scheduled_time
        self.pending_reason = "scheduled"
        self.updated_at = datetime.now().isoformat()
        logger.info(f"Task {self.task_id} scheduled for {scheduled_time}")
        return True

    def is_deferred(self) -> bool:
        """Check if task is deferred."""
        return self.status == TaskStatus.DEFERRED

    def is_delegated(self) -> bool:
        """Check if task is delegated."""
        return self.status == TaskStatus.DELEGATED

    def is_rolled_back(self) -> bool:
        """Check if task was rolled back."""
        return self.status == TaskStatus.ROLLED_BACK

    def get_state_history(self) -> List[Dict[str, Any]]:
        """Get complete history of state transitions."""
        return self.state_history.copy()

    def get_current_state_duration(self) -> float:
        """Get duration (in seconds) task has been in current state."""
        if not self.state_history:
            return 0.0
        last_transition = self.state_history[-1]
        last_time = datetime.fromisoformat(last_transition["timestamp"])
        current_time = datetime.now()
        return (current_time - last_time).total_seconds()

    def is_resumable(self) -> bool:
        """Check if task can be resumed."""
        return TaskStatus.is_paused_state(self.status)

    def is_terminal(self) -> bool:
        """Check if task is in a terminal state."""
        return TaskStatus.is_terminal_state(self.status)

    # ==================== Nested Task & Communication Methods ====================

    def add_child_task(self, child_task_id: str):
        """Add a child task to this parent task."""
        if child_task_id not in self.child_task_ids:
            self.child_task_ids.append(child_task_id)
            self.updated_at = datetime.now().isoformat()

    def add_sibling_task(self, sibling_task_id: str):
        """Add a sibling task (shares same parent)."""
        if sibling_task_id not in self.sibling_task_ids:
            self.sibling_task_ids.append(sibling_task_id)
            self.updated_at = datetime.now().isoformat()

    def add_dependent_task(self, dependent_task_id: str):
        """Register a task that depends on this one."""
        if dependent_task_id not in self.dependent_task_ids:
            self.dependent_task_ids.append(dependent_task_id)
            self.updated_at = datetime.now().isoformat()

    def add_blocking_task(self, blocking_task_id: str):
        """Register a task that is blocking this one."""
        if blocking_task_id not in self.blocked_by:
            self.blocked_by.append(blocking_task_id)
            self.updated_at = datetime.now().isoformat()

    def remove_blocking_task(self, blocking_task_id: str):
        """Remove a blocking task (dependency completed)."""
        if blocking_task_id in self.blocked_by:
            self.blocked_by.remove(blocking_task_id)
            self.updated_at = datetime.now().isoformat()

    def is_blocked(self) -> bool:
        """Check if this task is blocked by any dependencies."""
        return len(self.blocked_by) > 0

    def send_message_to_dependents(self, message: Dict[str, Any]):
        """Send a message to all dependent tasks."""
        message["from_task_id"] = self.task_id
        message["timestamp"] = datetime.now().isoformat()
        self.messages_to_dependents.append(message)
        self.updated_at = datetime.now().isoformat()

    def receive_message(self, message: Dict[str, Any]):
        """Receive a message from a prerequisite task."""
        message["received_at"] = datetime.now().isoformat()
        self.received_messages.append(message)
        self.updated_at = datetime.now().isoformat()

    def get_messages_from_prerequisites(self, message_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get messages received from prerequisite tasks."""
        if message_type:
            return [msg for msg in self.received_messages if msg.get("message_type") == message_type]
        return self.received_messages.copy()

    def get_prerequisite_results(self) -> Dict[str, Any]:
        """Extract results from all prerequisite tasks."""
        results = {}
        for msg in self.received_messages:
            if msg.get("message_type") == "result" and "from_task_id" in msg:
                results[msg["from_task_id"]] = msg.get("data")
        return results

    def has_all_children_completed(self, ledger: 'SmartLedger') -> bool:
        """Check if all child tasks have completed."""
        if not self.child_task_ids:
            return True
        for child_id in self.child_task_ids:
            child = ledger.get_task(child_id)
            if not child or not child.is_terminal():
                return False
        return True

    def has_all_prerequisites_completed(self, ledger: 'SmartLedger') -> bool:
        """Check if all prerequisite tasks have completed."""
        if not self.prerequisites:
            return True
        for prereq_id in self.prerequisites:
            prereq = ledger.get_task(prereq_id)
            if not prereq or prereq.status != TaskStatus.COMPLETED:
                return False
        return True


class SmartLedger:
    """
    Smart Ledger for persistent task tracking throughout agent execution.

    Features:
    - Maintains memory of all tasks until completion
    - Supports task reprioritization
    - Tracks pre-assigned, autonomous, and user-requested tasks
    - Provides context-aware task retrieval
    - Enables elastic and robust task management
    - Pluggable storage backends (Redis, MongoDB, JSON)
    """

    def __init__(self, agent_id: str, session_id: str, ledger_dir: str = "agent_data", backend: Optional[Any] = None):
        """
        Initialize SmartLedger.

        Args:
            agent_id: Unique identifier for the agent
            session_id: Unique identifier for this session
            ledger_dir: Directory for file-based storage (default: "agent_data")
            backend: Optional storage backend (RedisBackend, MongoDBBackend, etc.)
        """
        self.agent_id = agent_id
        self.session_id = session_id
        self.ledger_key = f"ledger_{agent_id}_{session_id}"
        self.ledger_dir = Path(ledger_dir)
        try:
            self.ledger_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Bundled/installed mode: relative path resolves to read-only dir
            self.ledger_dir = Path.home() / 'Documents' / 'Nunba' / 'data' / 'agent_data'
            self.ledger_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_file = self.ledger_dir / f"ledger_{agent_id}_{session_id}.json"
        self.tasks: Dict[str, Task] = {}
        self.task_order: List[str] = []  # Track order of task creation
        self.events: List[Dict[str, Any]] = []

        # Initialize storage backend
        if backend is None:
            from agent_ledger.backends import JSONBackend
            self.backend = JSONBackend(storage_dir=str(self.ledger_dir))
        else:
            self.backend = backend

        # Optional distributed features (activated via enable_pubsub/enable_heartbeat)
        self._pubsub = None
        self._heartbeat = None

        self.load()

    def load(self):
        """Load ledger from backend storage."""
        try:
            data = self.backend.load(self.ledger_key)
            if data:
                self.tasks = {
                    task_id: Task.from_dict(task_data)
                    for task_id, task_data in data.get("tasks", {}).items()
                }
                self.task_order = data.get("task_order", list(self.tasks.keys()))
                logger.info(f"Loaded {len(self.tasks)} tasks from ledger backend")
            else:
                logger.info("No existing ledger found, starting fresh")
        except Exception as e:
            logger.error(f"Failed to load ledger: {e}")
            self.tasks = {}
            self.task_order = []

    def save(self):
        """Save ledger to backend storage."""
        try:
            data = {
                "agent_id": self.agent_id,
                "session_id": self.session_id,
                "last_updated": datetime.now().isoformat(),
                "task_order": self.task_order,
                "tasks": {
                    task_id: task.to_dict()
                    for task_id, task in self.tasks.items()
                }
            }
            self.backend.save(self.ledger_key, data)
            logger.info(f"Saved {len(self.tasks)} tasks to ledger")
        except Exception as e:
            logger.error(f"Failed to save ledger: {e}")

    def enable_pubsub(self, redis_client) -> None:
        """Enable distributed notifications via Redis PUBSUB."""
        from agent_ledger.pubsub import LedgerPubSub
        self._pubsub = LedgerPubSub(redis_client, self.agent_id)
        logger.info(f"PubSub enabled for ledger {self.agent_id}")

    def enable_heartbeat(self, redis_client, host_info: Optional[Dict] = None) -> None:
        """Enable agent liveness tracking via Redis heartbeat."""
        from agent_ledger.heartbeat import AgentHeartbeat
        self._heartbeat = AgentHeartbeat(redis_client, self.agent_id, host_info)
        self._heartbeat.start()
        logger.info(f"Heartbeat enabled for ledger {self.agent_id}")

    def add_task(self, task: Task) -> bool:
        """Add a new task to ledger."""
        if task.task_id in self.tasks:
            logger.warning(f"Task {task.task_id} already exists")
            return False

        self.tasks[task.task_id] = task
        if task.task_id not in self.task_order:
            self.task_order.append(task.task_id)
        self.save()
        logger.info(f"Added task {task.task_id}: {task.description}")
        return True

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        error_message: Optional[str] = None,
        result: Optional[Any] = None
    ):
        """Update task status with automatic dependency management."""
        if task_id not in self.tasks:
            logger.error(f"Task {task_id} not found")
            return False

        task = self.tasks[task_id]
        task.status = status
        task.updated_at = datetime.now().isoformat()

        if status == TaskStatus.COMPLETED:
            task.completed_at = task.updated_at
            task.result = result
            self._handle_task_completion(task)

        if error_message:
            task.error_message = error_message

        self.save()
        logger.info(f"Updated task {task_id} status to {status}")
        return True

    def reprioritize_task(self, task_id: str, new_priority: int):
        """Change task priority (0-100)."""
        if task_id not in self.tasks:
            logger.error(f"Task {task_id} not found")
            return False

        old_priority = self.tasks[task_id].priority
        self.tasks[task_id].priority = max(0, min(100, new_priority))
        self.tasks[task_id].updated_at = datetime.now().isoformat()
        self.save()
        logger.info(f"Reprioritized task {task_id}: {old_priority} -> {new_priority}")
        return True

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get specific task."""
        return self.tasks.get(task_id)

    def get_tasks_by_status(self, status: TaskStatus) -> List[Task]:
        """Get all tasks with specific status."""
        return [task for task in self.tasks.values() if task.status == status]

    def get_tasks_by_type(self, task_type: TaskType) -> List[Task]:
        """Get all tasks of specific type."""
        return [task for task in self.tasks.values() if task.task_type == task_type]

    def get_ready_tasks(self) -> List[Task]:
        """Get tasks ready to execute (all prerequisites met), sorted by priority."""
        ready_tasks = []

        for task in self.tasks.values():
            if task.status != TaskStatus.PENDING:
                continue

            prerequisites_met = all(
                self.tasks.get(prereq_id, Task("", "", TaskType.PRE_ASSIGNED)).status == TaskStatus.COMPLETED
                for prereq_id in task.prerequisites
            )

            if prerequisites_met:
                ready_tasks.append(task)

        ready_tasks.sort(key=lambda t: t.priority, reverse=True)
        return ready_tasks

    def get_next_task(self) -> Optional[Task]:
        """Get the next task to execute (highest priority ready task)."""
        ready_tasks = self.get_ready_tasks()
        return ready_tasks[0] if ready_tasks else None

    def get_parallel_tasks(self) -> List[Task]:
        """Get tasks that can run in parallel."""
        ready_tasks = self.get_ready_tasks()
        return [task for task in ready_tasks if task.execution_mode == ExecutionMode.PARALLEL]

    def get_context_for_task(self, task_id: str) -> Dict[str, Any]:
        """Get relevant context for a task."""
        task = self.get_task(task_id)
        if not task:
            return {}

        context = {
            "current_task": task.to_dict(),
            "parent_info": None,
            "prerequisite_results": {},
            "sibling_tasks": []
        }

        if task.parent_task_id:
            parent = self.get_task(task.parent_task_id)
            if parent:
                context["parent_info"] = parent.to_dict()

        for prereq_id in task.prerequisites:
            prereq = self.get_task(prereq_id)
            if prereq and prereq.status == TaskStatus.COMPLETED:
                context["prerequisite_results"][prereq_id] = {
                    "description": prereq.description,
                    "result": prereq.result
                }

        if task.parent_task_id:
            siblings = [
                t.to_dict() for t in self.tasks.values()
                if t.parent_task_id == task.parent_task_id and t.task_id != task_id
            ]
            context["sibling_tasks"] = siblings

        return context

    def get_task_hierarchy(self) -> Dict[str, Any]:
        """Get hierarchical view of all tasks."""
        root_tasks = [t for t in self.tasks.values() if not t.parent_task_id]

        def build_tree(task: Task) -> Dict[str, Any]:
            children = [
                build_tree(t) for t in self.tasks.values()
                if t.parent_task_id == task.task_id
            ]
            return {
                **task.to_dict(),
                "children": children
            }

        return {
            "root_tasks": [build_tree(task) for task in root_tasks]
        }

    def get_progress_summary(self) -> Dict[str, Any]:
        """Get overall progress summary."""
        total = len(self.tasks)
        if total == 0:
            return {"total": 0, "progress": "0%"}

        status_counts = {}
        for status in TaskStatus:
            status_counts[status.value] = len(self.get_tasks_by_status(status))

        completed = status_counts[TaskStatus.COMPLETED.value]
        in_progress = status_counts[TaskStatus.IN_PROGRESS.value]
        pending = status_counts[TaskStatus.PENDING.value]
        blocked = status_counts[TaskStatus.BLOCKED.value]
        failed = status_counts[TaskStatus.FAILED.value]

        progress_pct = (completed / total) * 100 if total > 0 else 0

        return {
            "total": total,
            "completed": completed,
            "in_progress": in_progress,
            "pending": pending,
            "blocked": blocked,
            "failed": failed,
            "progress": f"{progress_pct:.1f}%",
            "status_breakdown": status_counts
        }

    def clear_completed_tasks(self):
        """Remove completed tasks from ledger."""
        completed_ids = [
            task_id for task_id, task in self.tasks.items()
            if task.status == TaskStatus.COMPLETED
        ]
        for task_id in completed_ids:
            del self.tasks[task_id]
        self.save()
        logger.info(f"Cleared {len(completed_ids)} completed tasks")

    def cancel_task(self, task_id: str, cascade: bool = False):
        """Cancel a task and optionally all dependent tasks."""
        if task_id not in self.tasks:
            logger.error(f"Task {task_id} not found")
            return False

        self.update_task_status(task_id, TaskStatus.CANCELLED)

        if cascade:
            dependent_tasks = [
                t for t in self.tasks.values()
                if task_id in t.prerequisites
            ]
            for task in dependent_tasks:
                self.cancel_task(task.task_id, cascade=True)

        return True

    # ==================== State Management Methods ====================

    def get_paused_tasks(self) -> List[Task]:
        """Get all paused tasks (PAUSED, USER_STOPPED, BLOCKED)."""
        return [
            task for task in self.tasks.values()
            if TaskStatus.is_paused_state(task.status)
        ]

    def get_resumable_tasks(self) -> List[Task]:
        """Get all tasks that can be resumed."""
        return [task for task in self.tasks.values() if task.is_resumable()]

    def get_active_tasks(self) -> List[Task]:
        """Get all tasks that are actively being worked on."""
        return [
            task for task in self.tasks.values()
            if TaskStatus.is_active_state(task.status)
        ]

    def get_terminal_tasks(self) -> List[Task]:
        """Get all tasks in terminal states."""
        return [task for task in self.tasks.values() if task.is_terminal()]

    def pause_task(self, task_id: str, reason: str = "Task paused") -> bool:
        """Pause a specific task."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        success = task.pause(reason)
        if success:
            self.save()
        return success

    def resume_task(self, task_id: str, reason: str = "Task resumed") -> bool:
        """Resume a paused/stopped task."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        success = task.resume(reason)
        if success:
            self.save()
        return success

    def complete_task(self, task_id: str, result: Any = None) -> bool:
        """Mark task as completed with optional result."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        success = task.complete(result)
        if success:
            # Auto-compute result hash for distributed verification
            if result is not None:
                try:
                    from agent_ledger.verification import TaskVerification
                    task.context["result_hash"] = TaskVerification.compute_result_hash(result)
                except Exception:
                    pass  # verification module not available or result not serializable
            self._handle_task_completion(task)
            self.save()
        return success

    def fail_task(self, task_id: str, error: str) -> bool:
        """Mark task as failed with error message."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        success = task.fail(error)
        if success:
            self.save()
        return success

    def user_stop_task(self, task_id: str, reason: str = "User stopped task") -> bool:
        """User explicitly stops a task."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        success = task.user_stop(reason)
        if success:
            self.save()
        return success

    def terminate_task(self, task_id: str, reason: str = "Task terminated") -> bool:
        """Forcefully terminate a task."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        success = task.terminate(reason)
        if success:
            self.save()
        return success

    def skip_task(self, task_id: str, reason: str = "Task skipped") -> bool:
        """Skip a task."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        success = task.skip(reason)
        if success:
            self.save()
        return success

    def mark_task_not_applicable(self, task_id: str, reason: str = "No longer applicable") -> bool:
        """Mark task as no longer applicable."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        success = task.mark_not_applicable(reason)
        if success:
            self.save()
        return success

    def pause_all_active_tasks(self, reason: str = "All tasks paused") -> int:
        """Pause all currently active tasks. Returns count of paused tasks."""
        paused_count = 0
        for task in self.get_active_tasks():
            if task.pause(reason):
                paused_count += 1
        if paused_count > 0:
            self.save()
        return paused_count

    def resume_all_paused_tasks(self, reason: str = "All tasks resumed") -> int:
        """Resume all paused tasks. Returns count of resumed tasks."""
        resumed_count = 0
        for task in self.get_resumable_tasks():
            if task.resume(reason):
                resumed_count += 1
        if resumed_count > 0:
            self.save()
        return resumed_count

    def get_task_state_summary(self) -> Dict[str, int]:
        """Get count of tasks in each state."""
        summary = {}
        for task in self.tasks.values():
            status_key = task.status
            summary[status_key] = summary.get(status_key, 0) + 1
        return summary

    def get_detailed_progress(self) -> Dict[str, Any]:
        """Get detailed progress including all state information."""
        state_summary = self.get_task_state_summary()
        active_count = len(self.get_active_tasks())
        paused_count = len(self.get_paused_tasks())
        terminal_count = len(self.get_terminal_tasks())

        return {
            "total_tasks": len(self.tasks),
            "by_state": state_summary,
            "active": active_count,
            "paused": paused_count,
            "terminal": terminal_count,
            "resumable": len(self.get_resumable_tasks()),
            "pending": state_summary.get(TaskStatus.PENDING, 0),
            "deferred": state_summary.get(TaskStatus.DEFERRED, 0),
            "delegated": state_summary.get(TaskStatus.DELEGATED, 0)
        }

    # ==================== New State Methods (Defer, Delegate, Rollback) ====================

    def defer_task(self, task_id: str, reason: str = "Task deferred", until: Optional[str] = None) -> bool:
        """Defer a task for later execution."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        success = task.defer(reason, until)
        if success:
            self.save()
        return success

    def undefer_task(self, task_id: str, reason: str = "Task undeferred") -> bool:
        """Undefer a deferred task back to pending."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        success = task.undefer(reason)
        if success:
            self.save()
        return success

    def delegate_task(
        self,
        task_id: str,
        to_agent_id: str,
        delegation_type: str = "sub_agent",
        reason: str = "Task delegated"
    ) -> bool:
        """Delegate a task to another agent."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        success = task.delegate(to_agent_id, delegation_type, reason)
        if success:
            self._generate_event("task_delegated", {
                "task_id": task_id,
                "to_agent": to_agent_id,
                "delegation_type": delegation_type
            })
            self.save()
        return success

    def complete_delegation(self, task_id: str, result: Any = None, reason: str = "Delegation completed") -> bool:
        """Complete a delegated task with result from delegate."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        success = task.complete_delegation(result, reason)
        if success:
            self._handle_task_completion(task)
            self.save()
        return success

    def reclaim_delegation(self, task_id: str, reason: str = "Delegation reclaimed") -> bool:
        """Reclaim a delegated task back to in-progress."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        success = task.reclaim_delegation(reason)
        if success:
            self.save()
        return success

    def rollback_task(self, task_id: str, reason: str = "Task rolled back") -> bool:
        """Rollback a completed task."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        success = task.rollback(reason)
        if success:
            self._generate_event("task_rolled_back", {
                "task_id": task_id,
                "reason": reason,
                "original_result": task.original_result
            })
            self.save()
        return success

    def get_deferred_tasks(self) -> List[Task]:
        """Get all deferred tasks."""
        return [task for task in self.tasks.values() if task.status == TaskStatus.DEFERRED]

    def get_delegated_tasks(self) -> List[Task]:
        """Get all delegated tasks."""
        return [task for task in self.tasks.values() if task.status == TaskStatus.DELEGATED]

    def get_tasks_delegated_to(self, agent_id: str) -> List[Task]:
        """Get all tasks delegated to a specific agent."""
        return [
            task for task in self.tasks.values()
            if task.status == TaskStatus.DELEGATED and task.delegated_to == agent_id
        ]

    def get_rolled_back_tasks(self) -> List[Task]:
        """Get all rolled back tasks."""
        return [task for task in self.tasks.values() if task.status == TaskStatus.ROLLED_BACK]

    def get_scheduled_tasks(self) -> List[Task]:
        """Get all tasks with a scheduled execution time."""
        return [
            task for task in self.tasks.values()
            if task.scheduled_at is not None and task.status in (TaskStatus.PENDING, TaskStatus.DEFERRED)
        ]

    def update_task_progress(self, task_id: str, progress_pct: float, checkpoint: Optional[str] = None) -> bool:
        """Update progress for a task."""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return False
        task.update_progress(progress_pct, checkpoint)
        self.save()
        return True

    def defer_all_pending_tasks(self, reason: str = "All tasks deferred") -> int:
        """Defer all pending tasks. Returns count of deferred tasks."""
        deferred_count = 0
        for task in self.tasks.values():
            if task.status == TaskStatus.PENDING:
                if task.defer(reason):
                    deferred_count += 1
        if deferred_count > 0:
            self.save()
        return deferred_count

    def undefer_all_tasks(self, reason: str = "All tasks undeferred") -> int:
        """Undefer all deferred tasks. Returns count of undeferred tasks."""
        undeferred_count = 0
        for task in self.get_deferred_tasks():
            if task.undefer(reason):
                undeferred_count += 1
        if undeferred_count > 0:
            self.save()
        return undeferred_count

    # ==================== Dependency Management ====================

    def _handle_task_completion(self, task: Task):
        """Handle task completion: update dependencies and auto-resume."""
        logger.debug(f"Updating dependency graph for completed task {task.task_id}")

        if task.result is not None:
            result_message = {
                "message_type": "result",
                "data": task.result,
                "from_task_id": task.task_id,
                "status": "completed"
            }
            task.send_message_to_dependents(result_message)

        completion_message = {
            "message_type": "completion",
            "from_task_id": task.task_id,
            "completed_at": task.completed_at
        }
        task.send_message_to_dependents(completion_message)

        auto_resumed = []
        for dependent_id in task.dependent_task_ids:
            dependent = self.get_task(dependent_id)
            if dependent:
                for msg in task.messages_to_dependents:
                    dependent.receive_message(msg)

                dependent.remove_blocking_task(task.task_id)

                if dependent.status == TaskStatus.BLOCKED and not dependent.is_blocked():
                    success = dependent.resume(reason=f"Auto-resume: dependency {task.task_id} completed")
                    if success:
                        auto_resumed.append(dependent_id)
                        logger.info(f"Auto-resumed task {dependent_id}")
                        self._generate_event("task_auto_resumed", {
                            "task_id": dependent_id,
                            "trigger": f"dependency_{task.task_id}_completed",
                            "reason": "All dependencies met"
                        })

        self._generate_event("task_completed", {
            "task_id": task.task_id,
            "result": task.result,
            "auto_resumed_tasks": auto_resumed
        })

        self.save()

    def _generate_event(self, event_type: str, event_data: Dict[str, Any]):
        """Generate an event for observation. Broadcasts via PubSub if enabled."""
        event = {
            "type": event_type,
            "timestamp": datetime.now().isoformat(),
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "data": event_data
        }

        if not hasattr(self, 'events'):
            self.events: List[Dict[str, Any]] = []

        self.events.append(event)
        logger.debug(f"Event: {event_type}: {event_data}")

        if len(self.events) > 100:
            self.events = self.events[-100:]

        # Broadcast to distributed listeners via Redis PubSub
        if hasattr(self, '_pubsub') and self._pubsub:
            try:
                if event_type == "task_completed":
                    task_id = event_data.get("task_id", "")
                    result_hash = None
                    task = self.get_task(task_id)
                    if task and hasattr(task, 'context'):
                        result_hash = task.context.get("result_hash")
                    self._pubsub.publish_task_update(task_id, "IN_PROGRESS", "COMPLETED", result_hash)
                elif event_type == "task_delegated":
                    self._pubsub.publish_delegation(
                        task_id=event_data.get("task_id", ""),
                        from_agent=self.agent_id,
                        to_agent=event_data.get("to_agent", ""),
                        description=event_data.get("reason", ""),
                    )
            except Exception as e:
                logger.debug(f"PubSub broadcast error (non-critical): {e}")

    def get_events(self, event_type: Optional[str] = None, since: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get events from the ledger."""
        if not hasattr(self, 'events'):
            self.events = []
            return []

        events = self.events.copy()

        if event_type:
            events = [e for e in events if e.get("type") == event_type]

        if since:
            events = [e for e in events if e.get("timestamp", "") > since]

        return events

    def clear_events(self):
        """Clear all events."""
        self.events = []
        logger.debug("Cleared event queue")

    def get_tasks_ready_to_resume(self) -> List[Task]:
        """Get all tasks that are BLOCKED but have no blockers."""
        ready_tasks = []
        for task in self.tasks.values():
            if task.status == TaskStatus.BLOCKED and not task.is_blocked():
                ready_tasks.append(task)
        return ready_tasks

    def get_tasks_blocked_by(self, blocking_task_id: str) -> List[Task]:
        """Get all tasks blocked by a specific task."""
        blocked_tasks = []
        for task in self.tasks.values():
            if blocking_task_id in task.blocked_by:
                blocked_tasks.append(task)
        return blocked_tasks

    def get_dependency_status(self, task_id: str) -> Dict[str, Any]:
        """Get comprehensive dependency information for a task."""
        task = self.get_task(task_id)
        if not task:
            return {}

        return {
            "task_id": task_id,
            "status": task.status,
            "is_blocked": task.is_blocked(),
            "blocked_by": task.blocked_by.copy(),
            "blocking_count": len(task.blocked_by),
            "dependents": task.dependent_task_ids.copy(),
            "dependent_count": len(task.dependent_task_ids),
            "all_prerequisites_met": task.has_all_prerequisites_completed(self),
            "ready_to_resume": task.status == TaskStatus.BLOCKED and not task.is_blocked(),
            "messages_received": len(task.received_messages)
        }

    # ==================== Parent-Child Task Creation ====================

    def create_parent_child_task(
        self,
        parent_task_id: str,
        child_description: str,
        child_type: TaskType = TaskType.PRE_ASSIGNED,
        **child_kwargs
    ) -> Optional[Task]:
        """Create a child task under a parent task."""
        parent = self.get_task(parent_task_id)
        if not parent:
            logger.error(f"Parent task {parent_task_id} not found")
            return None

        child_id = f"{parent_task_id}_child_{len(parent.child_task_ids) + 1}"

        child = Task(
            task_id=child_id,
            description=child_description,
            task_type=child_type,
            parent_task_id=parent_task_id,
            status=TaskStatus.PENDING,
            **child_kwargs
        )

        parent.add_child_task(child_id)
        self.tasks[child_id] = child
        self.save()
        logger.info(f"Created child task {child_id} under parent {parent_task_id}")

        return child

    def create_sibling_tasks(
        self,
        parent_task_id: str,
        sibling_descriptions: List[str],
        task_type: TaskType = TaskType.PRE_ASSIGNED
    ) -> List[Task]:
        """Create multiple sibling tasks under a parent."""
        parent = self.get_task(parent_task_id)
        if not parent:
            logger.error(f"Parent task {parent_task_id} not found")
            return []

        siblings = []
        sibling_ids = []

        for i, description in enumerate(sibling_descriptions, 1):
            sibling_id = f"{parent_task_id}_sibling_{i}"

            sibling = Task(
                task_id=sibling_id,
                description=description,
                task_type=task_type,
                parent_task_id=parent_task_id,
                status=TaskStatus.PENDING
            )

            sibling_ids.append(sibling_id)
            siblings.append(sibling)
            self.tasks[sibling_id] = sibling
            parent.add_child_task(sibling_id)

        for sibling in siblings:
            for other_id in sibling_ids:
                if other_id != sibling.task_id:
                    sibling.add_sibling_task(other_id)

        self.save()
        logger.info(f"Created {len(siblings)} sibling tasks under parent {parent_task_id}")

        return siblings

    def create_sequential_tasks(
        self,
        task_descriptions: List[str],
        task_type: TaskType = TaskType.PRE_ASSIGNED,
        parent_task_id: Optional[str] = None
    ) -> List[Task]:
        """Create a sequence of tasks with automatic dependencies."""
        tasks = []
        previous_task_id = None

        for i, description in enumerate(task_descriptions, 1):
            if parent_task_id:
                task_id = f"{parent_task_id}_seq_{i}"
            else:
                task_id = f"seq_task_{len(self.tasks) + 1}"

            task = Task(
                task_id=task_id,
                description=description,
                task_type=task_type,
                parent_task_id=parent_task_id,
                status=TaskStatus.PENDING
            )

            if previous_task_id:
                task.prerequisites.append(previous_task_id)
                task.add_blocking_task(previous_task_id)
                task.status = TaskStatus.BLOCKED

                prev_task = self.get_task(previous_task_id)
                if prev_task:
                    prev_task.add_dependent_task(task_id)

            self.tasks[task_id] = task
            tasks.append(task)

            if parent_task_id:
                parent = self.get_task(parent_task_id)
                if parent:
                    parent.add_child_task(task_id)

            previous_task_id = task_id

        self.save()
        logger.info(f"Created sequential chain of {len(tasks)} tasks")

        return tasks

    def get_task_tree(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get hierarchical tree structure for a task and descendants."""
        task = self.get_task(task_id)
        if not task:
            return None

        tree = {
            "task_id": task.task_id,
            "description": task.description,
            "status": task.status,
            "type": task.task_type,
            "is_blocked": task.is_blocked(),
            "blocked_by": task.blocked_by.copy(),
            "children": []
        }

        for child_id in task.child_task_ids:
            child_tree = self.get_task_tree(child_id)
            if child_tree:
                tree["children"].append(child_tree)

        return tree

    def get_all_descendants(self, task_id: str) -> List[Task]:
        """Get all descendant tasks recursively."""
        task = self.get_task(task_id)
        if not task:
            return []

        descendants = []

        for child_id in task.child_task_ids:
            child = self.get_task(child_id)
            if child:
                descendants.append(child)
                descendants.extend(self.get_all_descendants(child_id))

        return descendants

    def get_task_depth(self, task_id: str) -> int:
        """Get the depth of a task in the hierarchy."""
        task = self.get_task(task_id)
        if not task or not task.parent_task_id:
            return 0
        return 1 + self.get_task_depth(task.parent_task_id)

    def visualize_task_tree(self, task_id: str, indent: int = 0) -> str:
        """Generate ASCII tree visualization of task hierarchy."""
        task = self.get_task(task_id)
        if not task:
            return ""

        prefix = "  " * indent
        status_icon = {
            TaskStatus.PENDING: "[PEND]",
            TaskStatus.IN_PROGRESS: "[RUN ]",
            TaskStatus.COMPLETED: "[DONE]",
            TaskStatus.FAILED: "[FAIL]",
            TaskStatus.BLOCKED: "[BLOK]",
            TaskStatus.PAUSED: "[PAUS]",
            TaskStatus.CANCELLED: "[CANC]"
        }.get(task.status, "[?   ]")

        blocked_indicator = " [BLOCKED]" if task.is_blocked() else ""

        lines = [f"{prefix}{status_icon} {task.task_id}: {task.description} ({task.status}){blocked_indicator}"]

        for child_id in task.child_task_ids:
            lines.append(self.visualize_task_tree(child_id, indent + 1))

        return "\n".join(lines)

    def __repr__(self) -> str:
        summary = self.get_progress_summary()
        return f"SmartLedger({self.agent_id}:{self.session_id}, {summary['total']} tasks, {summary['progress']} complete)"

    # ==================== DYNAMIC TASK MANAGEMENT ====================

    def add_dynamic_task(
        self,
        task_description: str,
        context: Dict[str, Any],
        llm_client: Any = None
    ) -> Optional[Task]:
        """
        Add a dynamically discovered task with LLM auto-classification.

        This is the main entry point for adding runtime-discovered tasks.
        The LLM analyzes the task and determines:
        - Relationship type (child/sibling/sequential/conditional/independent)
        - Execution mode (parallel/sequential)
        - Dependencies and blockers
        - Delegation needs
        - Scheduling/deferral
        - Retry configuration

        Args:
            task_description: Description of the new task
            context: Context dict with:
                - current_action_id: Current action being executed
                - previous_outcome: Outcome of previous action
                - user_message: Latest user message
                - discovered_by: Which agent discovered this task
            llm_client: Optional LLM client for classification

        Returns:
            The created Task object, or None if failed

        Example:
            >>> task = ledger.add_dynamic_task(
            ...     "Validate credit card before processing payment",
            ...     {"current_action_id": 1, "previous_outcome": None, "user_message": "process my order"}
            ... )
        """
        # Get existing tasks summary for classification
        existing_tasks = [
            {
                "task_id": t.task_id,
                "description": t.description,
                "status": t.status.value if hasattr(t.status, 'value') else str(t.status),
                "parent_task_id": t.parent_task_id
            }
            for t in self.tasks.values()
        ]

        # Extract context
        current_action_id = context.get('current_action_id')
        current_action = f"action_{current_action_id}" if current_action_id else None
        previous_outcome = context.get('previous_outcome')
        user_message = context.get('user_message', '')
        discovered_by = context.get('discovered_by', 'assistant')

        # Use provided LLM client or try to get default
        if llm_client is None:
            llm_client = self._get_default_llm_client()

        # Classify task relationship using LLM
        classification = self._classify_task_relationship(
            new_task_description=task_description,
            existing_tasks=existing_tasks,
            current_action=current_action,
            previous_outcome=previous_outcome,
            user_message=user_message,
            llm_client=llm_client
        )

        # Generate unique task ID
        task_id = f"dynamic_{len([t for t in self.tasks if t.startswith('dynamic_')]) + 1}"

        # Determine parent based on relationship
        parent_task_id = None
        if classification['relationship'] == 'child':
            parent_task_id = classification.get('related_to_task_id') or current_action

        # Create the task with full field utilization
        task = Task(
            task_id=task_id,
            description=task_description,
            task_type=TaskType.AUTONOMOUS,
            execution_mode=ExecutionMode.PARALLEL if classification['execution_mode'] == 'parallel' else ExecutionMode.SEQUENTIAL,
            status=TaskStatus.PENDING,
            prerequisites=classification.get('prerequisites', []),
            context={
                "discovered_by": discovered_by,
                "discovery_context": user_message,
                "classification": classification,
                "current_action_at_discovery": current_action
            },
            priority=classification.get('priority', 50),
            parent_task_id=parent_task_id
        )

        # === WIRE ALL FIELDS BASED ON CLASSIFICATION ===

        # 1. Blocked state
        if classification.get('blocked_by'):
            task.blocked_by = classification['blocked_by']
            task.blocked_reason = classification.get('blocked_reason')
            task.status = TaskStatus.BLOCKED

        # 2. Delegation
        delegation = classification.get('delegation', {})
        if delegation.get('should_delegate'):
            task.delegated_to = delegation.get('delegate_to')
            task.delegated_at = datetime.now().isoformat()
            task.delegation_type = delegation.get('delegation_type')
            task.status = TaskStatus.DELEGATED

        # 3. Scheduling/Deferral
        scheduling = classification.get('scheduling', {})
        if scheduling.get('defer'):
            task.deferred_at = datetime.now().isoformat()
            task.deferred_until = scheduling.get('defer_until')
            task.deferred_reason = scheduling.get('defer_reason')
            task.status = TaskStatus.DEFERRED
        if scheduling.get('scheduled_at'):
            task.scheduled_at = scheduling.get('scheduled_at')

        # 4. Retry configuration
        retry_config = classification.get('retry_config', {})
        task.max_retries = retry_config.get('max_retries', 3)

        # 5. Sibling relationships (parallel execution)
        if classification['relationship'] == 'sibling':
            related_id = classification.get('related_to_task_id')
            if related_id and related_id in self.tasks:
                task.sibling_task_ids.append(related_id)
                self.tasks[related_id].sibling_task_ids.append(task_id)

        # 6. Parallel execution hints
        task.sibling_task_ids.extend(classification.get('can_run_parallel_with', []))

        # 7. Conditional execution (outcome-based)
        condition = classification.get('condition', {})
        if condition.get('depends_on_outcome'):
            task.context['depends_on_outcome'] = {
                'task_id': condition['depends_on_outcome'],
                'required_outcome': condition.get('required_outcome', 'success'),
                'description': condition.get('condition_description')
            }
            if condition['depends_on_outcome'] not in task.prerequisites:
                task.prerequisites.append(condition['depends_on_outcome'])

        # 8. Dependent task tracking
        for prereq_id in task.prerequisites:
            if prereq_id in self.tasks:
                if task_id not in self.tasks[prereq_id].dependent_task_ids:
                    self.tasks[prereq_id].dependent_task_ids.append(task_id)

        # 9. Pending reason
        if task.status == TaskStatus.PENDING:
            if task.prerequisites:
                task.pending_reason = "awaiting_prerequisites"
            else:
                task.pending_reason = "ready"

        # Add task to ledger
        try:
            self.tasks[task_id] = task
            self.save()
            logger.info(f"Added dynamic task {task_id}: {task_description} (relationship: {classification['relationship']})")

            # If this is a child task, block the parent
            if parent_task_id and parent_task_id in self.tasks:
                parent = self.tasks[parent_task_id]
                if task_id not in parent.child_task_ids:
                    parent.child_task_ids.append(task_id)
                if parent.status not in [TaskStatus.BLOCKED, TaskStatus.COMPLETED]:
                    self.update_task_status(
                        parent_task_id,
                        TaskStatus.BLOCKED,
                        error_message=f"Waiting for child task: {task_description}"
                    )

            return task

        except Exception as e:
            logger.error(f"Failed to add dynamic task: {e}")
            return None

    def _classify_task_relationship(
        self,
        new_task_description: str,
        existing_tasks: List[Dict],
        current_action: Optional[str],
        previous_outcome: Optional[str],
        user_message: str,
        llm_client: Any
    ) -> Dict:
        """Use LLM to classify a new task's relationship to existing tasks."""
        import json as json_module

        classification_prompt = """You are a task relationship analyzer. Given existing tasks and a new task, determine the relationship.

EXISTING TASKS:
{existing_tasks}

CURRENT CONTEXT:
- Current action being executed: {current_action}
- Previous action outcome: {previous_outcome}
- User's latest message: {user_message}

NEW TASK TO CLASSIFY:
"{new_task_description}"

Analyze and respond with ONLY valid JSON (no other text):
{{
    "relationship": "child|sibling|sequential|conditional|independent",
    "related_to_task_id": "task_id or null",
    "execution_mode": "parallel|sequential",
    "priority": 0-100,
    "prerequisites": ["task_id", ...] or [],
    "blocked_by": ["task_id", ...] or [],
    "blocked_reason": "dependency|input_required|approval_required|resource_unavailable|null",
    "condition": {{
        "depends_on_outcome": "task_id or null",
        "required_outcome": "success|failure|any|null",
        "condition_description": "description or null"
    }},
    "delegation": {{
        "should_delegate": true|false,
        "delegate_to": "agent_name or null",
        "delegation_type": "sub_agent|escalation|handoff|null"
    }},
    "scheduling": {{
        "defer": true|false,
        "defer_until": "ISO datetime or null",
        "defer_reason": "reason or null",
        "scheduled_at": "ISO datetime or null"
    }},
    "retry_config": {{
        "max_retries": 0-5,
        "retry_on_failure": true|false
    }},
    "can_run_parallel_with": ["task_id", ...] or [],
    "reasoning": "Brief explanation of classification"
}}

RELATIONSHIP TYPES:
- child: Subtask of existing task (blocks parent until complete)
- sibling: Can run in parallel with related task
- sequential: Must run after related task completes
- conditional: Only runs based on outcome of another task
- independent: No relationship to existing tasks
"""

        # Format existing tasks for prompt
        tasks_str = "\n".join([
            f"- {t['task_id']}: {t['description']} (status: {t['status']})"
            for t in existing_tasks
        ]) if existing_tasks else "No existing tasks"

        prompt = classification_prompt.format(
            existing_tasks=tasks_str,
            current_action=current_action or "None",
            previous_outcome=previous_outcome or "None",
            user_message=user_message or "None",
            new_task_description=new_task_description
        )

        try:
            response = llm_client.complete(prompt)
            response_text = response.strip()
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]

            classification = json_module.loads(response_text)
            logger.info(f"Task classified: {classification.get('relationship')} - {classification.get('reasoning', '')}")
            return classification

        except Exception as e:
            logger.warning(f"LLM classification failed: {e}, using defaults")
            return {
                "relationship": "independent",
                "related_to_task_id": None,
                "execution_mode": "sequential",
                "priority": 50,
                "prerequisites": [],
                "blocked_by": [],
                "blocked_reason": None,
                "condition": {"depends_on_outcome": None, "required_outcome": None, "condition_description": None},
                "delegation": {"should_delegate": False, "delegate_to": None, "delegation_type": None},
                "scheduling": {"defer": False, "defer_until": None, "defer_reason": None, "scheduled_at": None},
                "retry_config": {"max_retries": 3, "retry_on_failure": True},
                "can_run_parallel_with": [],
                "reasoning": "Default classification due to LLM error"
            }

    def _get_default_llm_client(self) -> Any:
        """Get default LLM client for classification."""
        class SimpleLLMClient:
            def complete(self, prompt: str) -> str:
                import requests
                try:
                    response = requests.post(
                        "http://localhost:8080/v1/chat/completions",
                        json={
                            "model": "Qwen3-VL-4B-Instruct",
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 500,
                            "temperature": 0.1
                        },
                        timeout=30
                    )
                    return response.json()['choices'][0]['message']['content']
                except Exception as e:
                    logger.error(f"LLM call failed: {e}")
                    raise
        return SimpleLLMClient()

    # ==================== TASK ORCHESTRATION ====================

    def get_next_executable_task(self) -> Optional[Task]:
        """
        Get the next task that can be executed based on relationships and outcomes.

        This is the main orchestrator that respects:
        - Hierarchical relationships (children must complete before parent resumes)
        - Prerequisites/dependencies
        - Outcome-based conditions
        - Blocked states
        - Priority ordering

        Returns:
            Next executable Task, or None if nothing can run
        """
        candidates = [
            task for task in self.tasks.values()
            if not TaskStatus.is_terminal_state(task.status)
            and task.status not in [TaskStatus.BLOCKED, TaskStatus.DELEGATED, TaskStatus.DEFERRED]
        ]

        candidates.sort(key=lambda t: t.priority, reverse=True)

        for task in candidates:
            # Check 1: Hierarchical - if task has pending children, skip it
            if task.child_task_ids:
                children_complete = all(
                    self.tasks[cid].status in [
                        TaskStatus.COMPLETED, TaskStatus.SKIPPED, TaskStatus.NOT_APPLICABLE
                    ]
                    for cid in task.child_task_ids
                    if cid in self.tasks
                )
                if not children_complete:
                    continue

            # Check 2: Prerequisites must be complete
            if task.prerequisites:
                prereqs_complete = all(
                    self.tasks[pid].status == TaskStatus.COMPLETED
                    for pid in task.prerequisites
                    if pid in self.tasks
                )
                if not prereqs_complete:
                    continue

            # Check 3: Outcome-based conditions
            if 'depends_on_outcome' in task.context:
                dep = task.context['depends_on_outcome']
                dep_task_id = dep['task_id']
                required_outcome = dep.get('required_outcome', 'success')

                if dep_task_id in self.tasks:
                    dep_task = self.tasks[dep_task_id]
                    actual_outcome = 'success' if dep_task.status == TaskStatus.COMPLETED else 'failure'

                    if required_outcome != 'any' and required_outcome != actual_outcome:
                        continue

            # Check 4: Blocked by other tasks
            if task.blocked_by:
                blockers_resolved = all(
                    self.tasks[bid].status in [
                        TaskStatus.COMPLETED, TaskStatus.SKIPPED, TaskStatus.NOT_APPLICABLE
                    ]
                    for bid in task.blocked_by
                    if bid in self.tasks
                )
                if not blockers_resolved:
                    continue

            return task

        return None

    def get_parallel_executable_tasks(self) -> List[Task]:
        """
        Get all tasks that can be executed in parallel right now.

        Returns tasks that:
        - Are in PENDING state with pending_reason='ready'
        - Have execution_mode=PARALLEL
        - All prerequisites satisfied
        """
        parallel_tasks = []

        for task in self.tasks.values():
            if task.status != TaskStatus.PENDING:
                continue
            if task.pending_reason != 'ready':
                continue
            if task.execution_mode != ExecutionMode.PARALLEL:
                continue

            if task.prerequisites:
                prereqs_complete = all(
                    self.tasks[pid].status == TaskStatus.COMPLETED
                    for pid in task.prerequisites
                    if pid in self.tasks
                )
                if not prereqs_complete:
                    continue

            parallel_tasks.append(task)

        return parallel_tasks

    def complete_task_and_route(
        self,
        task_id: str,
        outcome: str,
        result: Any = None
    ) -> Optional[Task]:
        """
        Complete a task and determine what should run next based on outcome.

        This handles:
        - Marking task complete/failed
        - Unblocking parent tasks
        - Activating conditional tasks based on outcome
        - Sending messages to dependent tasks

        Args:
            task_id: ID of completed task
            outcome: 'success' or 'failure'
            result: Result data from task execution

        Returns:
            Next task to execute, or None
        """
        if task_id not in self.tasks:
            return None

        task = self.tasks[task_id]

        if outcome == 'success':
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now().isoformat()
            task.result = result
            task.progress_pct = 100.0
        else:
            task.status = TaskStatus.FAILED
            task.failure_reason = "execution_failed"
            task.error_message = str(result) if result else "Task failed"

            if task.retry_count < task.max_retries:
                task.retry_count += 1
                task.last_retry_at = datetime.now().isoformat()
                task.retry_errors.append(task.error_message)
                task.status = TaskStatus.PENDING
                task.pending_reason = "queued"
                logger.info(f"Task {task_id} failed, retry {task.retry_count}/{task.max_retries}")

        task.state_history.append({
            "status": task.status.value if hasattr(task.status, 'value') else str(task.status),
            "timestamp": datetime.now().isoformat(),
            "reason": f"Task {outcome}: {result}" if result else f"Task {outcome}"
        })

        # Send messages to dependent tasks
        for dep_id in task.dependent_task_ids:
            if dep_id in self.tasks:
                self.tasks[dep_id].received_messages.append({
                    "from": task_id,
                    "outcome": outcome,
                    "result": result,
                    "timestamp": datetime.now().isoformat()
                })

        # Unblock parent if all children done
        if task.parent_task_id:
            self._check_and_unblock_parent(task_id)

        self.save()
        return self.get_next_executable_task()

    def _check_and_unblock_parent(self, completed_task_id: str) -> bool:
        """Check if all subtasks are complete and unblock parent task if so."""
        if completed_task_id not in self.tasks:
            return False

        completed_task = self.tasks[completed_task_id]
        parent_id = completed_task.parent_task_id

        if not parent_id or parent_id not in self.tasks:
            return False

        parent_task = self.tasks[parent_id]

        children = [t for t in self.tasks.values() if t.parent_task_id == parent_id]

        all_complete = all(
            child.status in [TaskStatus.COMPLETED, TaskStatus.SKIPPED, TaskStatus.NOT_APPLICABLE]
            for child in children
        )

        if all_complete and parent_task.status == TaskStatus.BLOCKED:
            parent_task.status = TaskStatus.IN_PROGRESS
            parent_task._record_state_transition(TaskStatus.IN_PROGRESS, "All subtasks completed")
            logger.info(f"Unblocked parent task {parent_id} - all {len(children)} subtasks complete")
            return True

        return False

    # ==================== SUBTASK MANAGEMENT ====================

    def add_subtasks(
        self,
        parent_action_id: int,
        subtasks: List[Dict]
    ) -> bool:
        """
        Add subtasks from LLM response to the ledger.

        When an LLM identifies that an action requires breakdown into subtasks,
        this creates child tasks and blocks the parent.

        Args:
            parent_action_id: The parent action ID (e.g., 1)
            subtasks: List of subtask dicts from LLM response with format:
                [{"subtask_id": "1.1", "description": "...", "depends_on": []}]

        Returns:
            bool: True if subtasks were added successfully
        """
        parent_task_id = f"action_{parent_action_id}"

        if parent_task_id not in self.tasks:
            logger.warning(f"Parent task {parent_task_id} not found in ledger")
            return False

        try:
            for subtask in subtasks:
                child_task_id = str(subtask.get('subtask_id', f"{parent_action_id}.{len(self.tasks)}"))
                description = subtask.get('description', 'Subtask')
                depends_on = subtask.get('depends_on', [])

                child_task = Task(
                    task_id=child_task_id,
                    description=description,
                    task_type=TaskType.AUTONOMOUS,
                    parent_task_id=parent_task_id
                )

                for dep in depends_on:
                    if str(dep) not in child_task.prerequisites:
                        child_task.prerequisites.append(str(dep))

                self.tasks[child_task_id] = child_task
                logger.info(f"Added subtask {child_task_id}: {description}")

            # Block parent task until children complete
            parent_task = self.tasks[parent_task_id]
            if parent_task.status != TaskStatus.BLOCKED:
                parent_task.status = TaskStatus.BLOCKED
                parent_task._record_state_transition(
                    TaskStatus.BLOCKED,
                    f"Waiting for {len(subtasks)} subtasks to complete"
                )

            self.save()
            return True

        except Exception as e:
            logger.error(f"Error adding subtasks to ledger: {e}")
            return False

    def get_pending_subtasks(self, parent_action_id: int) -> List[Task]:
        """
        Get all pending subtasks for a parent action.

        Args:
            parent_action_id: The parent action ID

        Returns:
            List of pending Task objects
        """
        parent_task_id = f"action_{parent_action_id}"
        return [
            task for task in self.tasks.values()
            if task.parent_task_id == parent_task_id and task.status == TaskStatus.PENDING
        ]

    # ==================== AGENT AWARENESS ====================

    def get_awareness(self) -> Dict[str, Any]:
        """
        Get complete execution awareness for the agent.

        This is the PRIMARY method agents should call to understand:
        1. What tasks have been executed and their outcomes
        2. What tasks are currently executing
        3. What is the next course of action for each executing task

        Returns:
            Comprehensive awareness dict with:
            - executed_tasks: List of completed/failed tasks with outcomes
            - executing_tasks: List of in-progress tasks with next actions
            - pending_tasks: Tasks waiting to be executed
            - blocked_tasks: Tasks blocked and why
            - recommended_action: What the agent should do next
            - hints: Execution hints

        Example:
            >>> awareness = ledger.get_awareness()
            >>> print(awareness['recommended_action'])
            {"action": "start_task", "task_id": "dynamic_1", ...}
        """
        # === 1. EXECUTED TASKS (with outcomes) ===
        executed_tasks = []
        for task in self.tasks.values():
            if task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED,
                              TaskStatus.SKIPPED, TaskStatus.CANCELLED,
                              TaskStatus.TERMINATED, TaskStatus.ROLLED_BACK]:
                executed_tasks.append({
                    "task_id": task.task_id,
                    "description": task.description,
                    "status": task.status.value if hasattr(task.status, 'value') else str(task.status),
                    "outcome": "success" if task.status == TaskStatus.COMPLETED else "failure",
                    "result": task.result,
                    "error": task.error_message,
                    "completed_at": task.completed_at,
                    "duration": self._calculate_duration(task.created_at, task.completed_at),
                    "retry_count": task.retry_count,
                    "was_rolled_back": task.status == TaskStatus.ROLLED_BACK,
                    "rollback_reason": task.rollback_reason
                })

        executed_tasks.sort(key=lambda x: x.get('completed_at') or '', reverse=True)

        # === 2. CURRENTLY EXECUTING TASKS (with next actions) ===
        executing_tasks = []
        for task in self.tasks.values():
            if task.status in [TaskStatus.IN_PROGRESS, TaskStatus.RESUMING]:
                next_action = self._determine_next_action_for_task(task)

                executing_tasks.append({
                    "task_id": task.task_id,
                    "description": task.description,
                    "status": task.status.value if hasattr(task.status, 'value') else str(task.status),
                    "progress_pct": task.progress_pct,
                    "started_at": task.updated_at,
                    "checkpoints_completed": len(task.checkpoints),
                    "has_children": len(task.child_task_ids) > 0,
                    "pending_children": [
                        cid for cid in task.child_task_ids
                        if cid in self.tasks and self.tasks[cid].status not in [
                            TaskStatus.COMPLETED, TaskStatus.SKIPPED
                        ]
                    ],
                    "next_action": next_action,
                    "blockers": task.blocked_by,
                    "waiting_for": self._get_waiting_for(task)
                })

        # === 3. PENDING TASKS ===
        pending_tasks = []
        for task in self.tasks.values():
            if task.status == TaskStatus.PENDING:
                can_execute, reason = self._can_task_execute(task)

                pending_tasks.append({
                    "task_id": task.task_id,
                    "description": task.description,
                    "priority": task.priority,
                    "pending_reason": task.pending_reason,
                    "can_execute_now": can_execute,
                    "waiting_reason": reason if not can_execute else None,
                    "prerequisites": task.prerequisites,
                    "prerequisites_status": self._get_prerequisites_status(task),
                    "execution_mode": task.execution_mode.value if hasattr(task.execution_mode, 'value') else str(task.execution_mode),
                    "parent_task": task.parent_task_id,
                    "is_dynamic": task.task_type == TaskType.AUTONOMOUS
                })

        pending_tasks.sort(key=lambda x: (x['can_execute_now'], x['priority']), reverse=True)

        # === 4. BLOCKED TASKS ===
        blocked_tasks = []
        for task in self.tasks.values():
            if task.status in [TaskStatus.BLOCKED, TaskStatus.DEFERRED, TaskStatus.DELEGATED]:
                blocked_tasks.append({
                    "task_id": task.task_id,
                    "description": task.description,
                    "status": task.status.value if hasattr(task.status, 'value') else str(task.status),
                    "blocked_reason": task.blocked_reason,
                    "blocked_by": task.blocked_by,
                    "deferred_until": task.deferred_until,
                    "deferred_reason": task.deferred_reason,
                    "delegated_to": task.delegated_to,
                    "delegation_type": task.delegation_type,
                    "unblock_condition": self._get_unblock_condition(task)
                })

        # === 5. RECOMMENDED ACTION ===
        recommended_action = self._get_recommended_action(
            executed_tasks, executing_tasks, pending_tasks, blocked_tasks
        )

        # === 6. OVERALL PROGRESS ===
        total = len(self.tasks)
        completed = len([t for t in self.tasks.values() if t.status == TaskStatus.COMPLETED])
        failed = len([t for t in self.tasks.values() if t.status == TaskStatus.FAILED])

        return {
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "timestamp": datetime.now().isoformat(),

            "progress": {
                "total_tasks": total,
                "completed": completed,
                "failed": failed,
                "in_progress": len(executing_tasks),
                "pending": len(pending_tasks),
                "blocked": len(blocked_tasks),
                "completion_pct": round((completed / total) * 100, 1) if total > 0 else 0
            },

            "executed_tasks": executed_tasks,
            "executing_tasks": executing_tasks,
            "pending_tasks": pending_tasks,
            "blocked_tasks": blocked_tasks,

            "recommended_action": recommended_action,

            "next_executable_task": self.get_next_executable_task(),
            "parallel_ready_tasks": [t.task_id for t in self.get_parallel_executable_tasks()],

            "hints": self._generate_execution_hints(executed_tasks, executing_tasks, pending_tasks, blocked_tasks)
        }

    def get_awareness_text(self) -> str:
        """
        Get agent awareness as formatted text for injection into prompts.

        Returns a human-readable string that can be injected into
        agent prompts to give them full context.

        Returns:
            Formatted string with execution context
        """
        awareness = self.get_awareness()

        lines = []
        lines.append("=" * 60)
        lines.append("AGENT EXECUTION AWARENESS")
        lines.append("=" * 60)

        p = awareness['progress']
        lines.append(f"\nPROGRESS: {p['completion_pct']}% complete")
        lines.append(f"  Total: {p['total_tasks']} | Done: {p['completed']} | Failed: {p['failed']} | Running: {p['in_progress']} | Pending: {p['pending']} | Blocked: {p['blocked']}")

        if awareness['executed_tasks']:
            lines.append(f"\nEXECUTED TASKS ({len(awareness['executed_tasks'])}):")
            for t in awareness['executed_tasks'][:5]:
                outcome_icon = "OK" if t['outcome'] == 'success' else "FAIL"
                lines.append(f"  [{outcome_icon}] {t['task_id']}: {t['description'][:50]}")
                if t['result']:
                    lines.append(f"      Result: {str(t['result'])[:100]}")
                if t['error']:
                    lines.append(f"      Error: {t['error'][:100]}")

        if awareness['executing_tasks']:
            lines.append(f"\nCURRENTLY EXECUTING ({len(awareness['executing_tasks'])}):")
            for t in awareness['executing_tasks']:
                lines.append(f"  > {t['task_id']}: {t['description'][:50]}")
                lines.append(f"      Progress: {t['progress_pct']}%")
                lines.append(f"      Next: {t['next_action']['description']}")
                if t['pending_children']:
                    lines.append(f"      Waiting for children: {', '.join(t['pending_children'])}")

        if awareness['pending_tasks']:
            lines.append(f"\nPENDING TASKS ({len(awareness['pending_tasks'])}):")
            for t in awareness['pending_tasks'][:5]:
                ready_icon = "[READY]" if t['can_execute_now'] else "[WAIT]"
                lines.append(f"  {ready_icon} {t['task_id']}: {t['description'][:50]} (priority: {t['priority']})")
                if not t['can_execute_now']:
                    lines.append(f"      Waiting: {t['waiting_reason']}")

        if awareness['blocked_tasks']:
            lines.append(f"\nBLOCKED TASKS ({len(awareness['blocked_tasks'])}):")
            for t in awareness['blocked_tasks']:
                lines.append(f"  [BLOCKED] {t['task_id']}: {t['description'][:50]}")
                lines.append(f"      Unblock: {t['unblock_condition']}")

        rec = awareness['recommended_action']
        lines.append(f"\nRECOMMENDED ACTION: {rec['action'].upper()}")
        lines.append(f"  {rec['description']}")
        if 'reason' in rec:
            lines.append(f"  Reason: {rec['reason']}")

        if awareness['hints']:
            lines.append("\nHINTS:")
            for hint in awareness['hints']:
                lines.append(f"  * {hint}")

        lines.append("\n" + "=" * 60)

        return "\n".join(lines)

    def get_execution_summary(self) -> Dict[str, Any]:
        """
        Get a summary of task execution status.

        Returns:
            Dict with counts and lists of tasks by status
        """
        summary = {
            "total": len(self.tasks),
            "pending": [],
            "in_progress": [],
            "blocked": [],
            "completed": [],
            "failed": [],
            "delegated": [],
            "parallel_ready": [],
            "next_executable": None
        }

        for task in self.tasks.values():
            status_key = task.status.value if hasattr(task.status, 'value') else str(task.status)
            if status_key in summary:
                summary[status_key].append(task.task_id)

        summary["parallel_ready"] = [t.task_id for t in self.get_parallel_executable_tasks()]
        next_task = self.get_next_executable_task()
        summary["next_executable"] = next_task.task_id if next_task else None

        return summary

    # ==================== AWARENESS HELPER METHODS ====================

    def _calculate_duration(self, start: Optional[str], end: Optional[str]) -> Optional[str]:
        """Calculate duration between two ISO timestamps."""
        if not start or not end:
            return None
        try:
            start_dt = datetime.fromisoformat(start)
            end_dt = datetime.fromisoformat(end)
            delta = end_dt - start_dt
            seconds = delta.total_seconds()
            if seconds < 60:
                return f"{seconds:.1f}s"
            elif seconds < 3600:
                return f"{seconds/60:.1f}m"
            else:
                return f"{seconds/3600:.1f}h"
        except:
            return None

    def _determine_next_action_for_task(self, task: Task) -> Dict:
        """Determine what should happen next for an executing task."""
        pending_children = [
            cid for cid in task.child_task_ids
            if cid in self.tasks and self.tasks[cid].status not in [
                TaskStatus.COMPLETED, TaskStatus.SKIPPED, TaskStatus.NOT_APPLICABLE
            ]
        ]

        if pending_children:
            child = self.tasks[pending_children[0]]
            return {
                "action": "wait_for_child",
                "description": f"Complete child task first: {child.description}",
                "child_task_id": pending_children[0],
                "total_pending_children": len(pending_children)
            }

        if task.blocked_reason == "input_required":
            return {
                "action": "await_input",
                "description": "Waiting for user input to continue",
                "blocked_reason": task.blocked_reason
            }

        if task.delegated_to:
            return {
                "action": "await_delegation",
                "description": f"Waiting for {task.delegated_to} to complete delegated work",
                "delegated_to": task.delegated_to,
                "delegation_type": task.delegation_type
            }

        if task.progress_pct < 100:
            return {
                "action": "continue_execution",
                "description": "Continue executing this task",
                "progress": task.progress_pct,
                "next_checkpoint": f"Checkpoint {len(task.checkpoints) + 1}"
            }

        return {
            "action": "complete",
            "description": "Task execution complete - mark as done",
            "progress": 100
        }

    def _can_task_execute(self, task: Task) -> tuple:
        """Check if a task can execute now, return (can_execute, reason)."""
        if task.prerequisites:
            incomplete = [
                pid for pid in task.prerequisites
                if pid in self.tasks and self.tasks[pid].status != TaskStatus.COMPLETED
            ]
            if incomplete:
                return False, f"Waiting for prerequisites: {', '.join(incomplete)}"

        if task.blocked_by:
            unresolved = [
                bid for bid in task.blocked_by
                if bid in self.tasks and self.tasks[bid].status not in [
                    TaskStatus.COMPLETED, TaskStatus.SKIPPED
                ]
            ]
            if unresolved:
                return False, f"Blocked by: {', '.join(unresolved)}"

        if 'depends_on_outcome' in task.context:
            dep = task.context['depends_on_outcome']
            dep_task_id = dep.get('task_id')
            required_outcome = dep.get('required_outcome', 'success')

            if dep_task_id and dep_task_id in self.tasks:
                dep_task = self.tasks[dep_task_id]
                if dep_task.status == TaskStatus.COMPLETED:
                    if required_outcome == 'failure':
                        return False, f"Requires {dep_task_id} to fail, but it succeeded"
                elif dep_task.status == TaskStatus.FAILED:
                    if required_outcome == 'success':
                        return False, f"Requires {dep_task_id} to succeed, but it failed"
                else:
                    return False, f"Waiting for {dep_task_id} outcome"

        if task.parent_task_id:
            parent = self.tasks.get(task.parent_task_id)
            if parent:
                higher_priority_siblings = [
                    self.tasks[cid] for cid in parent.child_task_ids
                    if cid in self.tasks
                    and cid != task.task_id
                    and self.tasks[cid].status == TaskStatus.PENDING
                    and self.tasks[cid].priority > task.priority
                ]
                if higher_priority_siblings:
                    return False, f"Higher priority sibling: {higher_priority_siblings[0].task_id}"

        return True, "Ready to execute"

    def _get_waiting_for(self, task: Task) -> List[str]:
        """Get list of what a task is waiting for."""
        waiting_for = []

        for cid in task.child_task_ids:
            if cid in self.tasks:
                child = self.tasks[cid]
                if child.status not in [TaskStatus.COMPLETED, TaskStatus.SKIPPED]:
                    waiting_for.append(f"child:{cid}")

        if task.delegated_to:
            waiting_for.append(f"delegation:{task.delegated_to}")

        if task.blocked_reason == "input_required":
            waiting_for.append("user_input")

        return waiting_for

    def _get_prerequisites_status(self, task: Task) -> Dict[str, str]:
        """Get status of each prerequisite."""
        status = {}
        for pid in task.prerequisites:
            if pid in self.tasks:
                prereq = self.tasks[pid]
                status[pid] = prereq.status.value if hasattr(prereq.status, 'value') else str(prereq.status)
            else:
                status[pid] = "not_found"
        return status

    def _get_unblock_condition(self, task: Task) -> str:
        """Get human-readable unblock condition for blocked task."""
        if task.status == TaskStatus.DEFERRED:
            if task.deferred_until:
                return f"Wait until {task.deferred_until}"
            return f"Deferred: {task.deferred_reason or 'unknown reason'}"

        if task.status == TaskStatus.DELEGATED:
            return f"Wait for {task.delegated_to} to complete ({task.delegation_type})"

        if task.blocked_by:
            blockers = []
            for bid in task.blocked_by:
                if bid in self.tasks:
                    blocker = self.tasks[bid]
                    blockers.append(f"{bid} ({blocker.status.value if hasattr(blocker.status, 'value') else blocker.status})")
            return f"Complete: {', '.join(blockers)}"

        if task.blocked_reason:
            reason_map = {
                "dependency": "Complete dependent tasks",
                "input_required": "Provide user input",
                "approval_required": "Get approval",
                "resource_unavailable": "Wait for resources",
                "rate_limited": "Wait for rate limit reset",
                "external_service": "Wait for external service"
            }
            return reason_map.get(task.blocked_reason, task.blocked_reason)

        return "Unknown - check task state"

    def _get_recommended_action(
        self,
        executed: List[Dict],
        executing: List[Dict],
        pending: List[Dict],
        blocked: List[Dict]
    ) -> Dict:
        """Generate recommended action for the agent."""
        # Priority 1: Handle any failed tasks that can be retried
        failed_retriable = [
            t for t in executed
            if t['outcome'] == 'failure' and t['retry_count'] < 3
        ]
        if failed_retriable:
            task = failed_retriable[0]
            return {
                "action": "retry_failed",
                "task_id": task['task_id'],
                "description": f"Retry failed task: {task['description']}",
                "reason": f"Failed with error: {task['error']}",
                "retry_count": task['retry_count']
            }

        # Priority 2: Complete tasks that are in progress and ready
        ready_to_complete = [
            t for t in executing
            if t['next_action']['action'] == 'complete'
        ]
        if ready_to_complete:
            task = ready_to_complete[0]
            return {
                "action": "complete_task",
                "task_id": task['task_id'],
                "description": f"Complete: {task['description']}",
                "reason": "Task execution finished, mark as complete"
            }

        # Priority 3: Work on child tasks of executing tasks
        for exec_task in executing:
            if exec_task['pending_children']:
                child_id = exec_task['pending_children'][0]
                if child_id in self.tasks:
                    child = self.tasks[child_id]
                    return {
                        "action": "execute_child",
                        "task_id": child_id,
                        "parent_task_id": exec_task['task_id'],
                        "description": f"Execute child task: {child.description}",
                        "reason": f"Required to complete parent: {exec_task['description']}"
                    }

        # Priority 4: Start highest priority pending task that can execute
        executable_pending = [t for t in pending if t['can_execute_now']]
        if executable_pending:
            task = executable_pending[0]
            return {
                "action": "start_task",
                "task_id": task['task_id'],
                "description": f"Start: {task['description']}",
                "reason": f"Highest priority ready task (priority: {task['priority']})",
                "is_dynamic": task['is_dynamic']
            }

        # Priority 5: Check if parallel tasks can be started
        parallel_ready = [t for t in pending if t['execution_mode'] == 'parallel' and t['can_execute_now']]
        if len(parallel_ready) > 1:
            return {
                "action": "start_parallel",
                "task_ids": [t['task_id'] for t in parallel_ready[:3]],
                "description": f"Start {len(parallel_ready)} tasks in parallel",
                "reason": "Multiple independent tasks can run concurrently"
            }

        # Priority 6: All tasks blocked - provide guidance
        if blocked and not executing and not executable_pending:
            blocker = blocked[0]
            return {
                "action": "resolve_blocker",
                "task_id": blocker['task_id'],
                "description": f"Resolve blocker for: {blocker['description']}",
                "blocker_type": blocker['status'],
                "resolution": blocker.get('unblock_condition', 'Check task state')
            }

        # Priority 7: Everything done
        if not executing and not pending and not blocked:
            success_count = len([t for t in executed if t['outcome'] == 'success'])
            fail_count = len([t for t in executed if t['outcome'] == 'failure'])
            return {
                "action": "all_complete",
                "description": "All tasks completed",
                "summary": f"{success_count} succeeded, {fail_count} failed",
                "reason": "No more tasks to execute"
            }

        # Default: Continue with current execution
        if executing:
            task = executing[0]
            return {
                "action": "continue",
                "task_id": task['task_id'],
                "description": f"Continue: {task['description']}",
                "progress": task['progress_pct'],
                "next_action": task['next_action']
            }

        return {
            "action": "wait",
            "description": "Waiting for blocked tasks to unblock",
            "reason": "No executable tasks available"
        }

    def _generate_execution_hints(
        self,
        executed: List[Dict],
        executing: List[Dict],
        pending: List[Dict],
        blocked: List[Dict]
    ) -> List[str]:
        """Generate helpful hints for the agent."""
        hints = []

        if executed:
            fail_rate = len([t for t in executed if t['outcome'] == 'failure']) / len(executed)
            if fail_rate > 0.3:
                hints.append(f"Warning: {fail_rate*100:.0f}% failure rate - consider reviewing approach")

        for task in executing:
            if task.get('progress_pct', 0) < 20 and len(task.get('checkpoints_completed', [])) == 0:
                hints.append(f"Task {task['task_id']} may be stalled - no progress recorded")

        if len(blocked) > 3:
            hints.append(f"{len(blocked)} tasks blocked - prioritize unblocking")

        parallel_count = len([t for t in pending if t['execution_mode'] == 'parallel'])
        if parallel_count > 2:
            hints.append(f"{parallel_count} tasks can run in parallel - consider concurrent execution")

        for task in pending:
            if task.get('parent_task') and not task['can_execute_now']:
                hints.append(f"Task {task['task_id']} waiting on parent - check parent status")

        return hints


# ==================== Utility Functions ====================

def get_production_backend():
    """
    Get the best available backend for production use.

    Tries Redis first (fastest), falls back to JSON.
    """
    try:
        from agent_ledger.backends import RedisBackend
        backend = RedisBackend(host='localhost', port=6379)
        logger.info("Using Redis backend for ledger (production mode)")
        return backend
    except ImportError:
        logger.warning("Redis not installed. Install with: pip install redis")
        return None
    except Exception as e:
        logger.warning(f"Redis not available: {e}. Using JSON fallback.")
        return None


def create_ledger_from_actions(
    agent_id: str = None,
    session_id: str = None,
    actions: List[Dict[str, Any]] = None,
    backend: Optional[Any] = None,
    # Backwards compatibility with old API
    user_id: int = None,
    prompt_id: int = None
) -> SmartLedger:
    """
    Create a ledger from pre-assigned actions.

    Args:
        agent_id: Agent ID (or use user_id/prompt_id for backwards compatibility)
        session_id: Session ID (or use user_id/prompt_id for backwards compatibility)
        actions: List of action dictionaries
        backend: Optional storage backend
        user_id: (DEPRECATED) Use agent_id/session_id instead
        prompt_id: (DEPRECATED) Use agent_id/session_id instead

    Returns:
        SmartLedger instance with tasks created from actions
    """
    # Handle backwards compatibility with old user_id/prompt_id API
    if user_id is not None and prompt_id is not None:
        agent_id = str(prompt_id)
        session_id = f"{user_id}_{prompt_id}"

    if agent_id is None or session_id is None:
        raise ValueError("Must provide either (agent_id, session_id) or (user_id, prompt_id)")

    if actions is None:
        actions = []

    ledger = SmartLedger(agent_id, session_id, backend=backend)

    for action in actions:
        if isinstance(action, str):
            action = {"description": action, "action": action}

        task_id = f"action_{action.get('action_id', len(ledger.tasks) + 1)}"

        has_prereqs = bool(action.get('prerequisites', []))
        execution_mode = ExecutionMode.SEQUENTIAL if has_prereqs else ExecutionMode.PARALLEL

        task = Task(
            task_id=task_id,
            description=action.get('description', action.get('action', '')),
            task_type=TaskType.PRE_ASSIGNED,
            execution_mode=execution_mode,
            status=TaskStatus.PENDING,
            prerequisites=[f"action_{p}" for p in action.get('prerequisites', [])],
            context={
                "action_id": action.get('action_id'),
                "flow": action.get('flow'),
                "persona": action.get('persona')
            },
            priority=100 - action.get('action_id', 0)
        )

        ledger.add_task(task)

    return ledger
