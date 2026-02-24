# Nested Task System

The nested task system enables complex goals to be decomposed into sub-tasks and distributed across peer nodes for parallel execution.

## Overview

```
User Goal
├── Task A (node-1)
│   ├── Subtask A.1
│   └── Subtask A.2
├── Task B (node-2)
│   ├── Subtask B.1
│   └── Subtask B.2
└── Task C (node-1)
    └── depends on Task A, Task B
```

## Coordinator Flow

### Step 1: Submit Goal

The coordinator receives a goal and initiates decomposition:

```python
from integrations.agent_engine.dispatch import submit_distributed_goal

result = submit_distributed_goal(
    prompt="Build a comprehensive market analysis report",
    goal_id="goal_123",
    goal_type="research",
    user_id="user_456"
)
```

### Step 2: Decompose Tasks

The goal is broken into sub-tasks with dependency relationships:

```python
from integrations.agent_engine.parallel_dispatch import decompose_goal_to_ledger

tasks, ledger = decompose_goal_to_ledger(
    prompt=prompt,
    goal_id=goal_id,
    goal_type=goal_type,
    user_id=user_id
)
```

Each task has:

- `task_id`: Unique identifier
- `description`: What the task does
- `task_type`: SEQUENTIAL or PARALLEL
- `dependencies`: List of task IDs that must complete first

### Step 3: Distributed Dispatch

The coordinator submits the decomposed goal:

```python
distributed_goal_id = coordinator.submit_goal(
    goal_id,
    decomposed_tasks=tasks,
)
```

Tasks are assigned to peer nodes based on:

- Available GPU/CPU resources
- Model availability (local models for hive tasks)
- Current load (VRAM pressure, active tasks)
- Compute policy (local_preferred, cloud_preferred)

### Step 4: Parallel Execution

Independent tasks execute simultaneously across nodes:

```python
# On each node, get tasks ready for execution
parallel_tasks = ledger.get_parallel_executable_tasks()

for task in parallel_tasks:
    # Execute via CREATE or REUSE pipeline
    result = execute_task(task)
    ledger.complete_task_and_route(task.id, result)
```

### Step 5: Dependency Resolution

Tasks with dependencies wait until prerequisites complete:

```python
# This returns None if dependencies are not yet met
next_task = ledger.get_next_executable_task()
```

The SmartLedger tracks dependency satisfaction automatically.

### Step 6: Subtask Creation

Tasks can spawn subtasks at runtime (dynamic decomposition):

```python
ledger.add_subtasks("task_A", [
    Task(id="task_A_sub1", description="Gather data sources"),
    Task(id="task_A_sub2", description="Clean and normalize data"),
])

# Check for pending subtasks
pending = ledger.get_pending_subtasks("task_A")
```

Parent tasks do not complete until all subtasks finish.

## SmartLedger Integration

The SmartLedger provides:

- **Persistence:** Tasks survive node restarts
- **Dependency tracking:** Automatic topological ordering
- **Status awareness:** `get_awareness()` returns full execution context
- **Cross-session recovery:** Resume from where execution stopped

## Task Types

| Type | Behavior |
|------|----------|
| `SEQUENTIAL` | Must wait for previous task to complete |
| `PARALLEL` | Can execute simultaneously with other parallel tasks |

## Error Handling

When a task fails:

1. ActionState transitions to ERROR
2. SmartLedger task status becomes FAILED
3. If `autonomous: true`: StatusVerifier generates a fallback strategy
4. If `autonomous: false`: FALLBACK_REQUESTED sent to user
5. Dependent tasks remain BLOCKED until the failed task resolves

## See Also

- [task-delegation.md](task-delegation.md) -- Task decomposition flow
- [smart-ledger.md](smart-ledger.md) -- SmartLedger persistence
- [../developer/state-machine.md](../developer/state-machine.md) -- ActionState machine
