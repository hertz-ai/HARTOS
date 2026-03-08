# SmartLedger

The SmartLedger is HART OS's persistent task tracking system. It provides cross-session recovery, audit trails, and dependency-aware task scheduling.

## Storage

Ledgers are persisted as JSON files:

```
agent_data/ledger_{user_id}_{prompt_id}.json
```

Each ledger tracks all tasks for a specific user+prompt combination, including their status, dependencies, and execution history.

## Creating a Ledger

Use the helper functions in `helper_ledger.py`:

```python
from helper_ledger import create_ledger_for_user_prompt

ledger = create_ledger_for_user_prompt(user_id=123, prompt_id=456)
# SmartLedger(456:123_456, 0 tasks, 0.0% complete)
```

### Naming Pattern

- `agent_id`: prompt_id (identifies the agent/conversation)
- `session_id`: `{user_id}_{prompt_id}` (unique session identifier)

### Backend Options

- **JSON (default):** File-based persistence in `agent_data/`
- **Redis:** For distributed deployments
- **MongoDB:** For large-scale deployments

```python
from agent_ledger.backends import RedisBackend
redis = RedisBackend(host='localhost', port=6379)
ledger = create_ledger_for_user_prompt(123, 456, backend=redis)
```

## Core Operations

### Adding Tasks

```python
ledger.add_dynamic_task(
    task_id="action_1",
    description="Research renewable energy",
    task_type=TaskType.SEQUENTIAL,
    dependencies=[]
)
```

### Task Scheduling

```python
# Get next task respecting dependency order
next_task = ledger.get_next_executable_task()

# Get all tasks that can run in parallel
parallel = ledger.get_parallel_executable_tasks()
```

### Completing Tasks

```python
ledger.complete_task_and_route(
    task_id="action_1",
    result="Research complete"
)
```

### Subtasks

```python
ledger.add_subtasks("action_1", [
    Task(id="action_1_sub1", description="Find sources"),
    Task(id="action_1_sub2", description="Summarize findings"),
])

pending = ledger.get_pending_subtasks("action_1")
```

## Task Status

| Status | Description |
|--------|-------------|
| `PENDING` | Not yet started |
| `IN_PROGRESS` | Currently executing |
| `VALIDATING` | Status verification in progress |
| `COMPLETED` | Successfully finished |
| `FAILED` | Execution error |
| `BLOCKED` | Waiting on dependencies or fallback |

## Awareness Context

Agents can query the ledger for full execution context:

```python
# Structured context for programmatic use
context = ledger.get_awareness()

# Text format for LLM prompts
context_text = ledger.get_awareness_text()
```

## Auto-Sync with ActionState

When a ledger is registered for a session, ActionState changes auto-sync:

```python
from lifecycle_hooks import register_ledger_for_session
register_ledger_for_session(f"{user_id}_{prompt_id}", ledger)
```

This maps ActionState values to TaskStatus (see [../developer/state-machine.md](../developer/state-machine.md)).

## Cross-Session Recovery

Because ledgers are persisted to disk, a crashed or restarted server can resume:

1. Load ledger from `agent_data/ledger_{user_id}_{prompt_id}.json`
2. Find tasks with status IN_PROGRESS or PENDING
3. Resume execution from where it left off

## See Also

- [task-delegation.md](task-delegation.md) -- How tasks are created
- [nested-tasks.md](nested-tasks.md) -- Nested task decomposition
- [../developer/state-machine.md](../developer/state-machine.md) -- ActionState details
