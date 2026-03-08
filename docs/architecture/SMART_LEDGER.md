# Smart Ledger System - Technical Documentation

## Overview

The Smart Ledger is a plug-and-play, framework-agnostic task tracking and memory system designed specifically for autonomous AI agents. It solves the critical problem of **task memory persistence** across agent sessions, restarts, crashes, and context resets.

## Problem Statement

Autonomous AI agents face several critical challenges:

1. **Memory Loss**: After crashes, restarts, or context resets (like Claude Code compacting), agents lose track of:
   - What tasks they were working on
   - What's been completed
   - What's pending
   - The current context and progress

2. **Lost in Complexity**: When working on complex projects with nested task hierarchies:
   - Hard to know where you are in the overall structure
   - Difficult to navigate back to incomplete tasks
   - No breadcrumb trail showing the path

3. **Duplicate Work**: Without persistent memory:
   - Tasks get repeated
   - Multiple report files get created
   - Token usage explodes reading duplicate documentation

4. **No Reprioritization**: User priorities change, but:
   - No way to dynamically adjust task order
   - Pre-assigned tasks can't adapt to new information
   - Agent can't handle evolving requirements

## Solution: Agent Ledger

The Smart Ledger provides:

- **Persistent Memory**: All task state saved to disk automatically
- **Hierarchical Navigation**: Tree view with breadcrumbs showing exactly where you are
- **Dynamic Reprioritization**: Change task priorities on-the-fly
- **Document Evolution**: Single evolving documents instead of duplicates
- **Context-Aware Retrieval**: Get relevant context for any task automatically
- **Framework Agnostic**: Works with any agent system (LangChain, AutoGen, CrewAI, custom)

## Architecture

### Core Components

```
agent_ledger/
├── __init__.py                    # Public API exports
├── core.py                        # Core ledger implementation
├── claude_code_integration.py     # Claude Code-specific integration
├── README.md                      # User documentation
├── CLAUDE_CODE_USAGE.md          # Claude Code guide
├── setup.py                       # Package setup
├── LICENSE                        # MIT license
└── examples/
    └── claude_code_example.py     # Runnable example
```

### Data Model

#### Task
Represents a single unit of work with:
- `task_id`: Unique identifier
- `description`: What needs to be done
- `task_type`: Pre-assigned, autonomous, user-requested, or intermediate
- `status`: Pending, in_progress, completed, blocked, failed, cancelled
- `execution_mode`: Parallel or sequential
- `prerequisites`: List of task IDs that must complete first
- `priority`: 0-100 (higher = more urgent)
- `parent_task_id`: For hierarchical relationships
- `context`: Additional metadata
- `result`: Output when completed
- `timestamps`: Created, updated, completed

#### SmartLedger
Main controller managing:
- Dictionary of all tasks
- Disk persistence (JSON files)
- Task lifecycle operations
- Context retrieval
- Progress tracking

#### ClaudeCodeLedger (Specialized)
Extension for Claude Code providing:
- Todo-style interface
- Document evolution tracking
- Breadcrumb navigation
- Context restoration after compacting

### Storage Format

Ledgers are stored as JSON files in `.agent_ledger/`:

```json
{
  "metadata": {
    "agent_id": "my_agent",
    "session_id": "session_1",
    "created_at": "2025-10-24T10:00:00",
    "last_saved": "2025-10-24T10:15:00",
    "task_count": 10
  },
  "tasks": {
    "task_1": {
      "task_id": "task_1",
      "description": "Implement user authentication",
      "task_type": "pre_assigned",
      "status": "completed",
      "execution_mode": "sequential",
      "prerequisites": [],
      "priority": 90,
      "parent_task_id": null,
      "context": {"flow": "auth_flow"},
      "result": {"auth_endpoint": "/api/login"},
      "created_at": "2025-10-24T10:00:00",
      "updated_at": "2025-10-24T10:10:00",
      "completed_at": "2025-10-24T10:10:00"
    }
  }
}
```

## Integration Patterns

### Pattern 1: LangChain Integration

```python
from langchain.agents import AgentExecutor
from agent_ledger import SmartLedger, Task, TaskStatus, TaskType

class LedgerAwareAgent:
    def __init__(self, executor: AgentExecutor, project_name: str):
        self.executor = executor
        self.ledger = SmartLedger(agent_id=project_name, session_id="main")

    def execute_workflow(self, tasks: List[Dict]):
        # Initialize ledger with tasks
        for task_def in tasks:
            task = Task(
                task_id=task_def["id"],
                description=task_def["description"],
                task_type=TaskType.PRE_ASSIGNED,
                priority=task_def.get("priority", 50)
            )
            self.ledger.add_task(task)

        # Execute tasks in priority order
        while True:
            ready_tasks = self.ledger.get_ready_tasks()
            if not ready_tasks:
                break

            task = ready_tasks[0]
            self.ledger.update_task_status(task.task_id, TaskStatus.IN_PROGRESS)

            try:
                result = self.executor.run(task.description)
                self.ledger.update_task_status(
                    task.task_id,
                    TaskStatus.COMPLETED,
                    result=result
                )
            except Exception as e:
                self.ledger.update_task_status(
                    task.task_id,
                    TaskStatus.FAILED,
                    error_message=str(e)
                )

        return self.ledger.get_progress_summary()
```

### Pattern 2: AutoGen Integration

```python
from autogen import AssistantAgent, UserProxyAgent
from agent_ledger import SmartLedger, Task, TaskType, TaskStatus

# Initialize ledger
ledger = SmartLedger(agent_id="autogen_workflow", session_id="run_1")

# Load tasks from workflow config
for action in workflow_config["actions"]:
    task = Task(
        task_id=f"action_{action['id']}",
        description=action['description'],
        task_type=TaskType.PRE_ASSIGNED,
        prerequisites=[f"action_{p}" for p in action.get('prerequisites', [])],
        priority=action.get('priority', 50)
    )
    ledger.add_task(task)

# Hook into agent callbacks
def on_action_start(action_id):
    ledger.update_task_status(action_id, TaskStatus.IN_PROGRESS)

def on_action_complete(action_id, result):
    ledger.update_task_status(action_id, TaskStatus.COMPLETED, result=result)

def on_action_error(action_id, error):
    ledger.update_task_status(action_id, TaskStatus.FAILED, error_message=str(error))

# Get next action to execute
ready_tasks = ledger.get_ready_tasks()
if ready_tasks:
    next_task = ready_tasks[0]
    context = ledger.get_context_for_task(next_task.task_id)
    # Use context to inform agent execution
```

### Pattern 3: Claude Code Integration

```python
from agent_ledger.claude_code_integration import ClaudeCodeLedger, restore_session

# At start of session
ledger = ClaudeCodeLedger.from_session("my_project")

# Add todos
ledger.add_todo("Implement feature X", priority=90)
ledger.add_todo("Write tests", priority=80, prerequisites=["Implement feature X"])

# Work on tasks
ledger.start_todo("Implement feature X")

# ... context gets large, needs compacting ...

# After compacting - restore everything
ledger = restore_session("my_project")
ledger.get_context_summary()  # Shows where you were
```

## Key Features Explained

### 1. Persistent Memory

All state automatically saved to disk:
- Tasks survive crashes, restarts, process kills
- Can restore state days/weeks/months later
- Version controlled (commit `.agent_ledger/` to git)
- Human-readable JSON format

### 2. Hierarchical Tasks

Support parent-child relationships:

```python
# Parent task
ledger.add_task(Task(
    task_id="implement_auth",
    description="Implement authentication",
    task_type=TaskType.PRE_ASSIGNED,
    priority=90
))

# Child tasks
ledger.add_task(Task(
    task_id="create_login",
    description="Create login endpoint",
    task_type=TaskType.AUTONOMOUS,
    parent_task_id="implement_auth",
    priority=90
))

ledger.add_task(Task(
    task_id="add_jwt",
    description="Add JWT generation",
    task_type=TaskType.AUTONOMOUS,
    parent_task_id="implement_auth",
    priority=85
))

# Navigate hierarchy
hierarchy = ledger.get_task_hierarchy()
# Returns tree structure with parent-child relationships
```

### 3. Dynamic Reprioritization

Change priorities based on evolving requirements:

```python
# Initial priorities
ledger.add_task(Task(task_id="feature_a", description="...", priority=80))
ledger.add_task(Task(task_id="feature_b", description="...", priority=70))
ledger.add_task(Task(task_id="bug_fix", description="...", priority=50))

# User reports critical bug
ledger.reprioritize_task("bug_fix", new_priority=100)

# Now bug_fix is first in ready tasks
ready = ledger.get_ready_tasks()  # Returns [bug_fix, feature_a, feature_b]
```

### 4. Context-Aware Retrieval

Get relevant context for any task:

```python
context = ledger.get_context_for_task("process_data")
# Returns:
# {
#     "current_task": {task details},
#     "parent_info": {parent task details if exists},
#     "prerequisite_results": {
#         "fetch_data": {
#             "result": {"records": 1500},
#             "completed_at": "2025-10-24T10:00:00"
#         }
#     },
#     "sibling_tasks": [{sibling 1}, {sibling 2}]
# }
```

This context helps agents make informed decisions based on:
- Results from prerequisite tasks
- Parent task objectives
- Progress of related tasks

### 5. Parallel Execution Support

Tag tasks for concurrent execution:

```python
# Independent tasks that can run in parallel
task_a = Task(
    task_id="fetch_users",
    description="Fetch user data",
    execution_mode=ExecutionMode.PARALLEL,
    priority=80
)

task_b = Task(
    task_id="fetch_products",
    description="Fetch product data",
    execution_mode=ExecutionMode.PARALLEL,
    priority=80
)

task_c = Task(
    task_id="merge_data",
    description="Merge user and product data",
    execution_mode=ExecutionMode.SEQUENTIAL,
    prerequisites=["fetch_users", "fetch_products"],
    priority=75
)

ledger.add_task(task_a)
ledger.add_task(task_b)
ledger.add_task(task_c)

# Get tasks that can run concurrently
parallel_tasks = ledger.get_parallel_tasks()  # Returns [task_a, task_b]

# After both complete, task_c becomes ready
```

### 6. Document Evolution (Claude Code)

Maintain single evolving documents:

```python
from agent_ledger.claude_code_integration import ClaudeCodeLedger

ledger = ClaudeCodeLedger.from_session("project")

# Day 1
ledger.update_document("STATUS.md", "Week 1", "Completed auth implementation")

# Day 7
ledger.update_document("STATUS.md", "Week 2", "Added API endpoints", append=True)

# Day 14
ledger.update_document("STATUS.md", "Week 3", "Deployed to staging", append=True)

# Write single consolidated file
ledger.write_document_to_file("STATUS.md")

# Result: One file with version history, not multiple files
```

## Performance Considerations

### Storage Efficiency

- **JSON Format**: Human-readable but efficient
- **Incremental Saves**: Only modified data written
- **File Size**: Typical ledger with 100 tasks = ~50KB

### Memory Usage

- **In-Memory Dict**: All tasks loaded on initialization
- **Typical Size**: 100 tasks = ~500KB RAM
- **For Large Projects**: Consider periodic cleanup of completed tasks

### Disk I/O

- **Automatic Saves**: Every state change saves to disk
- **Optimization**: Uses Python's atomic write operations
- **Failure Recovery**: Previous state retained on save failure

## Best Practices

### 1. Use Descriptive Task IDs

```python
# GOOD: Descriptive
task_id = "implement_user_authentication"

# BAD: Generic
task_id = "task_1"
```

### 2. Set Appropriate Priorities

```python
# 90-100: Critical/Urgent (bugs, blockers)
# 70-89: High priority (main features)
# 50-69: Medium priority (nice-to-haves)
# 30-49: Low priority (optimizations)
# 0-29: Nice-to-have (future work)
```

### 3. Use Prerequisites for Dependencies

```python
# Define clear dependency chains
ledger.add_task(Task(task_id="design_db", ...))
ledger.add_task(Task(
    task_id="implement_models",
    prerequisites=["design_db"],  # Can't start until design done
    ...
))
```

### 4. Tag Parallel Tasks Correctly

```python
# Independent tasks = PARALLEL
ledger.add_task(Task(
    task_id="fetch_users",
    execution_mode=ExecutionMode.PARALLEL
))

# Dependent tasks = SEQUENTIAL
ledger.add_task(Task(
    task_id="process_users",
    execution_mode=ExecutionMode.SEQUENTIAL,
    prerequisites=["fetch_users"]
))
```

### 5. Use Context Results

```python
# Store useful results
ledger.update_task_status(
    "fetch_data",
    TaskStatus.COMPLETED,
    result={"records": 1500, "api_url": "https://..."}
)

# Retrieve for next task
context = ledger.get_context_for_task("process_data")
records = context["prerequisite_results"]["fetch_data"]["result"]["records"]
```

## Troubleshooting

### Issue: Tasks Not Showing as Ready

**Cause**: Prerequisites not completed

**Solution**:
```python
task = ledger.get_task("my_task")
print(f"Prerequisites: {task.prerequisites}")

for prereq_id in task.prerequisites:
    prereq = ledger.get_task(prereq_id)
    print(f"{prereq_id}: {prereq.status if prereq else 'NOT FOUND'}")
```

### Issue: Ledger File Not Found

**Cause**: Wrong agent_id or session_id

**Solution**:
```python
# List all ledger files
import os
from pathlib import Path

ledger_dir = Path(".agent_ledger")
if ledger_dir.exists():
    for file in ledger_dir.glob("ledger_*.json"):
        print(file.name)
```

### Issue: Out of Memory with Large Projects

**Cause**: Too many completed tasks in memory

**Solution**:
```python
# Periodically clean completed tasks
ledger.clear_completed_tasks()

# Or export and archive
ledger.export_json("archive_2025_10.json")
ledger.clear_completed_tasks()
```

## Future Enhancements

Planned features:

1. **Async Support**: Concurrent operations with asyncio
2. **Time Tracking**: Automatic task duration tracking
3. **Retry Logic**: Built-in retry for failed tasks
4. **Visualization**: Task dependency graphs
5. **REST API**: Remote ledger access
6. **Database Backends**: PostgreSQL, Redis options
7. **Task Templates**: Common patterns as templates

## Contributing

Agent Ledger is designed to be community-driven. Contributions welcome!

## License

MIT License - Free for commercial and personal use.

---

**Agent Ledger** - The working memory every agent needs.
