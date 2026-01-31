# Agent Ledger

**A Framework-Agnostic Task Tracking System for AI Agents**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)
[![PyPI version](https://badge.fury.io/py/agent-ledger.svg)](https://badge.fury.io/py/agent-ledger)

Agent Ledger is a production-ready, standalone task tracking system with persistent memory designed for autonomous AI agents. It provides reliable task memory across sessions with zero core dependencies.

---

## Features

- **Persistent Memory** - Tasks survive restarts, crashes, and interruptions
- **12 Task States** - Comprehensive lifecycle (PENDING, IN_PROGRESS, BLOCKED, COMPLETED, etc.)
- **Parent-Child Tasks** - Hierarchical task relationships with auto-resume
- **Dynamic Reprioritization** - Change priorities on-the-fly
- **Multiple Backends** - Redis, JSON, MongoDB, PostgreSQL, or in-memory
- **State History** - Complete audit trail of all state transitions
- **Framework Agnostic** - Works with AutoGen, LangChain, CrewAI, or custom agents
- **Zero Core Dependencies** - Pure Python 3.7+ (optional backends need their packages)

---

## Installation

```bash
# From PyPI
pip install agent-ledger

# With Redis support (recommended for production)
pip install agent-ledger[redis]

# With all backends
pip install agent-ledger[all]

# From source
git clone https://github.com/yourusername/agent-ledger.git
cd agent-ledger
pip install -e .
```

---

## Quick Start

```python
from agent_ledger import SmartLedger, Task, TaskType, TaskStatus

# 1. Create ledger
ledger = SmartLedger(agent_id="my_agent", session_id="session_1")

# 2. Add task
task = Task("task_1", "Process customer data", TaskType.PRE_ASSIGNED)
ledger.add_task(task)

# 3. Track execution
ledger.update_task_status("task_1", TaskStatus.IN_PROGRESS)
ledger.complete_task("task_1", result={"customers": 1500})
```

---

## Examples

### Multi-Step Workflow with Dependencies

```python
from agent_ledger import SmartLedger, Task, TaskType, TaskStatus

ledger = SmartLedger(agent_id="data_pipeline", session_id="run_001")

# Define workflow with dependencies
tasks = [
    Task("extract", "Extract data from API", TaskType.PRE_ASSIGNED, priority=100),
    Task("transform", "Transform data", TaskType.PRE_ASSIGNED,
         prerequisites=["extract"], priority=90),
    Task("load", "Load to warehouse", TaskType.PRE_ASSIGNED,
         prerequisites=["transform"], priority=80)
]

for task in tasks:
    ledger.add_task(task)

# Execute workflow
while True:
    next_task = ledger.get_next_task()
    if not next_task:
        break

    print(f"Working on: {next_task.description}")
    ledger.update_task_status(next_task.task_id, TaskStatus.IN_PROGRESS)

    # ... do work ...

    ledger.complete_task(next_task.task_id, result={"success": True})

print(f"Progress: {ledger.get_progress_summary()['progress']}")
```

### Parent-Child Tasks with Auto-Resume

```python
from agent_ledger import SmartLedger, Task, TaskType, TaskStatus

ledger = SmartLedger(agent_id="deploy_agent", session_id="deploy_001")

# Create parent task
parent = Task("deploy_app", "Deploy application", TaskType.PRE_ASSIGNED)
ledger.add_task(parent)
ledger.update_task_status("deploy_app", TaskStatus.IN_PROGRESS)

# Create child task - parent automatically blocks
child = ledger.create_parent_child_task(
    parent_task_id="deploy_app",
    child_description="Run tests before deployment",
    child_type=TaskType.AUTONOMOUS
)

print(f"Parent status: {ledger.get_task('deploy_app').status}")
# Output: TaskStatus.BLOCKED (if dependency tracking is set up)

# Complete child - parent can be resumed
ledger.complete_task(child.task_id, result={"tests_passed": True})
```

### Using Different Storage Backends

```python
from agent_ledger import SmartLedger, InMemoryBackend, RedisBackend, JSONBackend

# In-memory (testing)
backend = InMemoryBackend()
ledger = SmartLedger("agent", "session", backend=backend)

# JSON files (development)
backend = JSONBackend(storage_dir="./task_data")
ledger = SmartLedger("agent", "session", backend=backend)

# Redis (production - 10-50x faster!)
backend = RedisBackend(host="localhost", port=6379)
ledger = SmartLedger("agent", "session", backend=backend)
```

### AutoGen Integration

```python
from agent_ledger import SmartLedger, Task, TaskType, TaskStatus
import autogen

ledger = SmartLedger(agent_id="autogen_agent", session_id="chat_001")

# Create tasks from workflow
for action in workflow_actions:
    task = Task(
        task_id=f"action_{action['id']}",
        description=action["description"],
        task_type=TaskType.PRE_ASSIGNED,
        priority=action.get("priority", 50)
    )
    ledger.add_task(task)

# Agent execution loop
while True:
    task = ledger.get_next_task()
    if not task:
        break

    ledger.update_task_status(task.task_id, TaskStatus.IN_PROGRESS)

    try:
        result = agent.generate_reply(messages=[{
            "role": "user",
            "content": task.description
        }])
        ledger.complete_task(task.task_id, result=result)
    except Exception as e:
        ledger.fail_task(task.task_id, error=str(e))
```

---

## API Reference

### SmartLedger

```python
ledger = SmartLedger(
    agent_id: str,           # Unique agent identifier
    session_id: str,         # Unique session identifier
    ledger_dir: str = "agent_data",  # Storage directory
    backend: Optional[Backend] = None  # Storage backend
)

# Task management
ledger.add_task(task: Task) -> bool
ledger.get_task(task_id: str) -> Optional[Task]
ledger.get_next_task() -> Optional[Task]
ledger.get_ready_tasks() -> List[Task]

# Status updates
ledger.update_task_status(task_id, status, error_message=None, result=None) -> bool
ledger.complete_task(task_id, result=None) -> bool
ledger.fail_task(task_id, error) -> bool
ledger.pause_task(task_id, reason) -> bool
ledger.resume_task(task_id, reason) -> bool

# Reprioritization
ledger.reprioritize_task(task_id, new_priority) -> bool

# Queries
ledger.get_tasks_by_status(status) -> List[Task]
ledger.get_progress_summary() -> Dict
ledger.get_task_hierarchy() -> Dict
```

### Task

```python
task = Task(
    task_id: str,                      # Unique identifier
    description: str,                  # Human-readable description
    task_type: TaskType,               # PRE_ASSIGNED, AUTONOMOUS, etc.
    execution_mode: ExecutionMode = ExecutionMode.SEQUENTIAL,
    status: TaskStatus = TaskStatus.PENDING,
    prerequisites: Optional[List[str]] = None,
    context: Optional[Dict] = None,
    priority: int = 50,                # 0-100, higher = more important
    parent_task_id: Optional[str] = None
)

# State transitions
task.start(reason) -> bool
task.complete(result, reason) -> bool
task.fail(error, reason) -> bool
task.pause(reason) -> bool
task.resume(reason) -> bool
task.cancel(reason) -> bool
task.skip(reason) -> bool

# Queries
task.is_terminal() -> bool
task.is_resumable() -> bool
task.get_state_history() -> List[Dict]
```

### Enums

```python
class TaskType(Enum):
    PRE_ASSIGNED     # From initial workflow
    AUTONOMOUS       # Created by agent
    USER_REQUESTED   # From user feedback
    INTERMEDIATE     # Sub-tasks

class TaskStatus(Enum):
    PENDING          # Not started
    IN_PROGRESS      # Currently executing
    PAUSED           # Paused by system
    USER_STOPPED     # User stopped
    BLOCKED          # Blocked by dependencies
    COMPLETED        # Successfully finished
    FAILED           # Failed with error
    CANCELLED        # Cancelled by user
    TERMINATED       # Forcefully killed
    SKIPPED          # Not needed
    NOT_APPLICABLE   # No longer relevant
    RESUMING         # Being resumed

class ExecutionMode(Enum):
    PARALLEL         # Can run concurrently
    SEQUENTIAL       # Must wait for prerequisites
```

---

## Storage Backends

| Backend | Use Case | Performance | Requirements |
|---------|----------|-------------|--------------|
| InMemoryBackend | Testing | Fastest | None |
| JSONBackend | Development | 1-5ms | None |
| RedisBackend | Production | 0.1-0.5ms | `pip install redis` |
| MongoDBBackend | Large Scale | 1-3ms | `pip install pymongo` |
| PostgreSQLBackend | Enterprise | 0.5-2ms | `pip install psycopg2-binary` |

### Production Setup

```python
from agent_ledger import create_production_ledger

# Automatically uses fastest available backend
ledger = create_production_ledger(
    agent_id="my_agent",
    session_id="session_1"
)
```

---

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

### Development Setup

```bash
git clone https://github.com/yourusername/agent-ledger.git
cd agent-ledger
pip install -e ".[dev]"
pytest
```

---

## License

MIT License - See [LICENSE](LICENSE) for details.

Free for commercial and personal use.

---

## Acknowledgments

Built for the autonomous agent community. Designed to be the persistent memory every agent needs.
