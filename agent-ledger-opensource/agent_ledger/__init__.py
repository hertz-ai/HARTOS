"""
Agent Ledger - A Framework-Agnostic Task Tracking System for AI Agents

A standalone, production-ready task tracking system with persistent memory,
rich state management, and minimal integration footprint. Perfect for autonomous
AI agents that need reliable task memory across sessions.

Features:
- Persistent Memory - Tasks survive restarts, crashes, and interruptions
- 15 Task States - Comprehensive lifecycle management (including DEFERRED, DELEGATED, ROLLED_BACK)
- Parent-Child Tasks - Hierarchical task relationships with auto-resume
- Dynamic Reprioritization - Change priorities on-the-fly
- Multiple Backends - Redis, JSON, MongoDB, PostgreSQL, or in-memory
- State History - Complete audit trail of all state transitions
- Reason Tracking - Sub-states via BlockedReason, FailureReason, PendingReason
- Progress & Checkpoints - Track partial completion
- Delegation Support - Hand off tasks to other agents
- Framework Agnostic - Works with AutoGen, LangChain, CrewAI, or custom agents
- Zero Core Dependencies - Pure Python 3.7+ (optional backends need their packages)

Quick Start:
    from agent_ledger import SmartLedger, Task, TaskType, TaskStatus

    # Create ledger
    ledger = SmartLedger(agent_id="my_agent", session_id="session_1")

    # Add task
    task = Task("task_1", "Process data", TaskType.PRE_ASSIGNED)
    ledger.add_task(task)

    # Track execution
    ledger.update_task_status("task_1", TaskStatus.IN_PROGRESS)
    ledger.complete_task("task_1", result={"processed": 100})

License: MIT
"""

from .core import (
    SmartLedger,
    Task,
    TaskType,
    TaskStatus,
    ExecutionMode,
    # Reason enums for sub-states
    BlockedReason,
    FailureReason,
    PendingReason,
    # Utilities
    get_production_backend,
    create_ledger_from_actions,
    enable_vlm_integration,
    disable_vlm_integration,
    is_vlm_enabled,
)

from .graph import (
    TaskGraph,
    TaskStateMachine,
    analyze_ledger,
)

from .backends import (
    StorageBackend,
    InMemoryBackend,
    RedisBackend,
    JSONBackend,
    MongoDBBackend,
    PostgreSQLBackend,
)

from .factory import (
    create_production_ledger,
    create_ledger_from_environment,
    get_or_create_ledger,
    migrate_ledger_to_redis,
    clear_ledger_cache,
)

# Distributed features (optional — require Redis at runtime, not import time)
from .verification import TaskVerification, TaskBaseline
from .pubsub import LedgerPubSub
from .heartbeat import AgentHeartbeat
from .distributed import DistributedTaskLock

__version__ = "1.2.0"
__author__ = "Agent Ledger Contributors"
__license__ = "MIT"

__all__ = [
    # Core classes
    "SmartLedger",
    "Task",
    "TaskType",
    "TaskStatus",
    "ExecutionMode",

    # Reason enums (sub-states)
    "BlockedReason",
    "FailureReason",
    "PendingReason",

    # Core utilities
    "get_production_backend",
    "create_ledger_from_actions",
    "enable_vlm_integration",
    "disable_vlm_integration",
    "is_vlm_enabled",

    # Graph analysis
    "TaskGraph",
    "TaskStateMachine",
    "analyze_ledger",

    # Backends
    "StorageBackend",
    "InMemoryBackend",
    "RedisBackend",
    "JSONBackend",
    "MongoDBBackend",
    "PostgreSQLBackend",

    # Factory functions
    "create_production_ledger",
    "create_ledger_from_environment",
    "get_or_create_ledger",
    "migrate_ledger_to_redis",
    "clear_ledger_cache",

    # Distributed features (optional — require Redis)
    "TaskVerification",
    "TaskBaseline",
    "LedgerPubSub",
    "AgentHeartbeat",
    "DistributedTaskLock",
]
