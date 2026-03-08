# Nested Task Management System - Complete Documentation

**Date**: 2025-10-25
**Status**: ✅ Production Ready
**Architecture**: Deterministic Ledger + Event-Driven Agent

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture Principles](#architecture-principles)
3. [Implementation Summary](#implementation-summary)
4. [Quick Reference](#quick-reference)
5. [Complete API Documentation](#complete-api-documentation)
6. [Usage Examples](#usage-examples)
7. [Agent Integration Patterns](#agent-integration-patterns)

---

## Overview

The Smart Ledger now includes **nested task management** with parent-child relationships, sibling tasks, sequential chains, dependency tracking, inter-task communication, and deterministic auto-resume.

### Key Architectural Decision

**Ledger**: Deterministic automation (no LLM intelligence)
**Agent**: All decision-making intelligence
**Events**: Notification nudges from ledger to agent

The ledger is a **deterministic, eventually-consistent state machine** that:
- ✅ Maintains task relationships and dependency graphs
- ✅ Auto-resumes tasks when dependencies complete (deterministic rule)
- ✅ Generates event nudges for agent observation
- ❌ Makes NO intelligent decisions (no prioritization, resource allocation, or context-based choices)

The agent observes events and makes ALL intelligent decisions about task execution.

---

## Architecture Principles

### Ledger: Deterministic Automation (No LLM Intelligence)

✅ **ALLOWED** (Deterministic Rules):
- Auto-resume when `blocked_by` becomes empty
- Update dependency graphs when tasks complete
- Deliver messages between dependent tasks
- Generate events for agent observation
- Maintain state consistency
- Apply pure state machine logic

❌ **NOT ALLOWED** (LLM Intelligence):
- Deciding which task to work on next
- Choosing execution order based on context
- Resource allocation decisions
- Error handling strategies
- Priority-based scheduling
- Any context-aware decision-making

### Agent: All Intelligence

The agent makes ALL decisions:
- Which tasks to start
- When to resume blocked tasks
- Execution order and priority
- Resource management
- Error handling and retries
- Context interpretation
- Strategy and planning

### Events: Nudges/Notifications

Events are **notifications** from ledger to agent:
- Not commands, just information
- Agent observes and decides how to react
- Enables event-driven architecture without polling

---

## Implementation Summary

### What Was Implemented

#### 1. Task Relationships (Lines 141-151 in Task class)

**Parent-Child Tasks**: Hierarchical relationships
```python
# Fields added to Task class
self.child_task_ids: List[str] = []          # Direct children
self.sibling_task_ids: List[str] = []        # Siblings (same parent)
self.dependent_task_ids: List[str] = []      # Tasks waiting on this
self.parent_task_id: Optional[str] = None    # Parent reference
```

**Sibling Tasks**: Parallel execution groups
**Sequential Tasks**: Chains with automatic dependencies

#### 2. Dependency Tracking (Lines 150-151)

```python
self.blocked_by: List[str] = []  # Tasks blocking this one
```

When creating sequential tasks:
- `task2.prerequisites = [task1.task_id]`
- `task2.blocked_by = [task1.task_id]`
- `task2.status = TaskStatus.BLOCKED`
- `task1.dependent_task_ids = [task2.task_id]`

#### 3. Inter-Task Communication (Lines 146-148)

```python
self.messages_to_dependents: List[Dict[str, Any]] = []
self.received_messages: List[Dict[str, Any]] = []
```

Messages automatically delivered when task completes.

#### 4. Deterministic Auto-Resume (Lines 1088-1164)

```python
def _handle_task_completion(self, task: Task):
    """
    DETERMINISTIC AUTOMATION: Update dependency tracking and auto-resume.

    Rule: if (status == BLOCKED) AND (blocked_by == []) then resume()
    """
    for dependent_id in task.dependent_task_ids:
        dependent = self.get_task(dependent_id)

        # Remove this task from blockers (deterministic update)
        dependent.remove_blocking_task(task.task_id)

        # DETERMINISTIC RULE: blocked but no blockers = resume
        if dependent.status == TaskStatus.BLOCKED and not dependent.is_blocked():
            dependent.resume(reason=f"Auto-resume: dependency {task.task_id} completed")

            # Generate event nudge for agent
            self._generate_event("task_auto_resumed", {
                "task_id": dependent_id,
                "trigger": f"dependency_{task.task_id}_completed"
            })
```

**This is deterministic** because:
- Pure state machine logic: `BLOCKED + empty blocked_by → IN_PROGRESS`
- No context evaluation, priority checking, or resource decisions
- Just mechanical state transition

#### 5. Event System (Lines 1166-1228)

```python
def _generate_event(self, event_type: str, event_data: Dict[str, Any]):
    """Generate event nudge for agent."""
    event = {
        "type": event_type,
        "timestamp": datetime.now().isoformat(),
        "data": event_data
    }
    self.events.append(event)

def get_events(self, event_type=None, since=None):
    """Agent queries events."""
    # Filter and return events

def clear_events(self):
    """Agent clears processed events."""
```

**Event Types**:
- `task_completed` - Task finished
- `task_auto_resumed` - Task auto-resumed by ledger

#### 6. Task Creation Methods (Lines 1259-1363)

```python
ledger.create_parent_child_task(parent_id, description)
ledger.create_sibling_tasks(parent_id, descriptions)
ledger.create_sequential_tasks(descriptions)
```

#### 7. Query Methods (Lines 1230-1257, 1365-1472)

**Dependency Queries**:
- `get_tasks_ready_to_resume()` - Find unblocked tasks
- `get_tasks_blocked_by(task_id)` - Find dependents
- `get_dependency_status(task_id)` - Comprehensive status

**Hierarchy Queries**:
- `get_task_tree(task_id)` - Tree structure
- `get_all_descendants(task_id)` - Recursive children
- `get_task_depth(task_id)` - Depth in hierarchy
- `visualize_task_tree(task_id)` - ASCII visualization

#### 8. Task Methods (Lines 528-656)

**Relationship Management**:
```python
task.add_child_task(child_id)
task.add_sibling_task(sibling_id)
task.add_dependent_task(dependent_id)
```

**Blocking**:
```python
task.add_blocking_task(blocker_id)
task.remove_blocking_task(blocker_id)
task.is_blocked()  # Returns: bool
```

**Communication**:
```python
task.send_message_to_dependents(message)
task.receive_message(message)
task.get_messages_from_prerequisites(message_type=None)
task.get_prerequisite_results()
```

**Dependency Checking**:
```python
task.has_all_children_completed(ledger)
task.has_all_prerequisites_completed(ledger)
```

### Files Modified

**task_ledger.py**:
- Added nested task fields to Task.__init__ (lines 141-151)
- Updated Task.to_dict() and from_dict() (lines 178-186, 214-222)
- Added 15 Task methods for nested management (lines 528-656)
- Added events list to SmartLedger.__init__ (line 677)
- Added deterministic auto-resume (lines 1088-1164)
- Added event generation and queries (lines 1166-1228)
- Added 3 task creation methods (lines 1259-1363)
- Added 7 query methods (lines 1230-1257, 1365-1472)

**Total**: ~400 lines of new functionality

---

## Quick Reference

### Creating Nested Tasks

#### Parent-Child
```python
child = ledger.create_parent_child_task(
    parent_task_id="parent_1",
    child_description="Subtask 1"
)
```

#### Siblings (Parallel)
```python
siblings = ledger.create_sibling_tasks(
    parent_task_id="parent_1",
    sibling_descriptions=["Task A", "Task B", "Task C"]
)
```

#### Sequential (Auto-Dependencies)
```python
tasks = ledger.create_sequential_tasks([
    "Step 1: Initialize",
    "Step 2: Process",
    "Step 3: Finalize"
])
# task2 automatically blocked by task1
# task3 automatically blocked by task2
```

### Deterministic Auto-Resume

```python
# When task completes:
ledger.update_task_status(task1.task_id, TaskStatus.COMPLETED, result=data)

# Ledger automatically:
# 1. Updates blocked_by lists of dependents
# 2. If blocked_by becomes empty → auto-resume
# 3. Generates event nudge for agent
```

### Events (Nudges to Agent)

```python
# Agent observes events
events = ledger.get_events(since=last_check_time)

for event in events:
    if event["type"] == "task_completed":
        agent.handle_completion(event)
    elif event["type"] == "task_auto_resumed":
        agent.consider_task(event["data"]["task_id"])

# Clear after processing
ledger.clear_events()
```

### Inter-Task Communication

```python
# When task completes, ledger sends result to dependents
ledger.update_task_status(task1.task_id, COMPLETED, result={"data": 100})

# Dependent receives messages
messages = task2.get_messages_from_prerequisites()
results = task2.get_prerequisite_results()
# Returns: {"task1": {"data": 100}}
```

### Query Methods

#### Dependency Queries
```python
# Get tasks ready to resume
ready = ledger.get_tasks_ready_to_resume()

# Get dependency status
status = ledger.get_dependency_status("task_id")
# {
#     "is_blocked": bool,
#     "blocked_by": [task_ids],
#     "dependents": [task_ids],
#     "ready_to_resume": bool,
#     "messages_received": count
# }

# Get tasks blocked by specific task
blocked = ledger.get_tasks_blocked_by("task1")
```

#### Hierarchy Queries
```python
# Get tree structure
tree = ledger.get_task_tree("root")

# Get all descendants (recursive)
descendants = ledger.get_all_descendants("parent")

# Get depth in hierarchy
depth = ledger.get_task_depth("child")  # 0=root, 1=child, 2=grandchild

# Visualize (for debugging)
print(ledger.visualize_task_tree("root"))
# ▶ root: Main task (in_progress)
#   ✓ child1: Subtask 1 (completed)
#     ✓ grandchild1: Detail (completed)
#   🚫 child2: Subtask 2 (blocked) [BLOCKED]
```

### Task Relationship Fields

```python
task.parent_task_id           # Parent task ID
task.child_task_ids           # List[str] of children
task.sibling_task_ids         # List[str] of siblings
task.dependent_task_ids       # List[str] of tasks waiting on this
task.blocked_by               # List[str] of tasks blocking this
task.prerequisites            # List[str] of prerequisite IDs
```

---

## Complete API Documentation

### SmartLedger Methods

#### Task Creation

##### `create_parent_child_task(parent_task_id, child_description, child_type=PRE_ASSIGNED, **kwargs) -> Task`
Creates a child task under a parent.

**Args**:
- `parent_task_id`: ID of parent task
- `child_description`: Description for child
- `child_type`: Task type (default: PRE_ASSIGNED)
- `**kwargs`: Additional Task creation arguments

**Returns**: Created child Task or None if parent not found

**Example**:
```python
child = ledger.create_parent_child_task(
    "parent_1",
    "Process subset of data",
    priority=5
)
```

##### `create_sibling_tasks(parent_task_id, sibling_descriptions, task_type=PRE_ASSIGNED) -> List[Task]`
Creates multiple sibling tasks (parallel execution).

**Args**:
- `parent_task_id`: ID of parent task
- `sibling_descriptions`: List of descriptions
- `task_type`: Type for all siblings

**Returns**: List of created sibling Tasks

**Example**:
```python
siblings = ledger.create_sibling_tasks(
    "parent_1",
    ["Build frontend", "Build backend", "Write tests"]
)
```

##### `create_sequential_tasks(task_descriptions, task_type=PRE_ASSIGNED, parent_task_id=None) -> List[Task]`
Creates sequential task chain with automatic dependencies.

**Args**:
- `task_descriptions`: List of descriptions in execution order
- `task_type`: Type for all tasks
- `parent_task_id`: Optional parent

**Returns**: List of created Tasks

**Behavior**:
- First task: PENDING
- Rest: BLOCKED by previous task
- Dependencies automatically set up

**Example**:
```python
tasks = ledger.create_sequential_tasks([
    "Initialize database",
    "Load data",
    "Process records",
    "Generate report"
])
```

#### Dependency Queries

##### `get_tasks_ready_to_resume() -> List[Task]`
Get tasks that are BLOCKED but have no blockers (dependencies met).

**Returns**: List of Tasks that could be resumed

**Note**: Informational only - agent decides whether to resume

##### `get_tasks_blocked_by(blocking_task_id) -> List[Task]`
Get all tasks blocked by a specific task.

**Args**: `blocking_task_id` - ID of blocking task

**Returns**: List of Tasks waiting on this task

##### `get_dependency_status(task_id) -> Dict[str, Any]`
Get comprehensive dependency information.

**Args**: `task_id` - Task ID to check

**Returns**: Dictionary with:
```python
{
    "task_id": str,
    "status": TaskStatus,
    "is_blocked": bool,
    "blocked_by": List[str],
    "blocking_count": int,
    "dependents": List[str],
    "dependent_count": int,
    "all_prerequisites_met": bool,
    "ready_to_resume": bool,
    "messages_received": int
}
```

#### Hierarchy Queries

##### `get_task_tree(task_id) -> Dict[str, Any]`
Get hierarchical tree structure for task and descendants.

**Args**: `task_id` - Root task ID

**Returns**: Dictionary representing tree:
```python
{
    "task_id": str,
    "description": str,
    "status": TaskStatus,
    "type": TaskType,
    "is_blocked": bool,
    "blocked_by": List[str],
    "children": [
        {nested child trees...}
    ]
}
```

##### `get_all_descendants(task_id) -> List[Task]`
Get all descendant tasks recursively.

**Args**: `task_id` - Parent task ID

**Returns**: List of all descendants (children, grandchildren, etc.)

##### `get_task_depth(task_id) -> int`
Get depth of task in hierarchy.

**Args**: `task_id` - Task ID

**Returns**: Depth level (0 = root, 1 = child, 2 = grandchild, etc.)

##### `visualize_task_tree(task_id, indent=0) -> str`
Generate ASCII tree visualization.

**Args**:
- `task_id` - Root task ID
- `indent` - Internal use only

**Returns**: String representation with status icons

**Example output**:
```
▶ root_task: Main task (in_progress)
  ✓ child1: Subtask 1 (completed)
    ✓ grandchild1: Detail 1 (completed)
  🚫 child2: Subtask 2 (blocked) [BLOCKED]
```

#### Event Methods

##### `get_events(event_type=None, since=None) -> List[Dict[str, Any]]`
Get events from ledger (agent observation).

**Args**:
- `event_type` - Filter by type (optional)
- `since` - ISO timestamp, only events after this (optional)

**Returns**: List of events

**Example**:
```python
# Get all events
events = ledger.get_events()

# Get only completions
completed = ledger.get_events(event_type="task_completed")

# Get events since last check
new_events = ledger.get_events(since="2025-10-25T10:30:00")
```

##### `clear_events()`
Clear all events (after agent processes them).

**Example**:
```python
events = ledger.get_events()
agent.process(events)
ledger.clear_events()
```

### Task Methods

#### Relationship Management

##### `add_child_task(child_task_id)`
Register a child task.

##### `add_sibling_task(sibling_task_id)`
Register a sibling task.

##### `add_dependent_task(dependent_task_id)`
Register a task that depends on this one.

##### `add_blocking_task(blocking_task_id)`
Register a task that is blocking this one.

##### `remove_blocking_task(blocking_task_id)`
Remove a blocking task (dependency completed).

##### `is_blocked() -> bool`
Check if task is blocked by any dependencies.

**Returns**: `True` if `len(blocked_by) > 0`

#### Communication

##### `send_message_to_dependents(message)`
Send message to all dependent tasks.

**Args**: `message` - Dictionary with:
```python
{
    "from_task_id": str,      # Auto-added
    "message_type": str,       # "result", "state_change", etc.
    "data": Any,               # Message payload
    "timestamp": str           # Auto-added
}
```

##### `receive_message(message)`
Receive a message from prerequisite task.

##### `get_messages_from_prerequisites(message_type=None) -> List[Dict]`
Get messages received from prerequisites.

**Args**: `message_type` - Filter by type (optional)

**Returns**: List of messages

##### `get_prerequisite_results() -> Dict[str, Any]`
Extract results from prerequisite tasks.

**Returns**: Dict mapping `prerequisite_task_id -> result`

#### Dependency Checking

##### `has_all_children_completed(ledger) -> bool`
Check if all child tasks completed.

##### `has_all_prerequisites_completed(ledger) -> bool`
Check if all prerequisite tasks completed.

---

## Usage Examples

### Example 1: Simple Sequential Workflow

```python
from task_ledger import SmartLedger, TaskStatus

ledger = SmartLedger(user_id=123, prompt_id=456)

# Create sequential tasks
tasks = ledger.create_sequential_tasks([
    "Initialize database",
    "Load data",
    "Process records",
    "Generate report"
])

# Start first task
tasks[0].start("Agent: begin workflow")

# Simulate completion
ledger.update_task_status(
    tasks[0].task_id,
    TaskStatus.COMPLETED,
    result={"status": "initialized"}
)

# Check events
events = ledger.get_events()
# [{"type": "task_auto_resumed", "data": {"task_id": "seq_task_2"}}]

# Second task auto-resumed by ledger
assert tasks[1].status == TaskStatus.IN_PROGRESS
```

### Example 2: Parallel Task Execution

```python
# Create parent task
parent = Task(
    task_id="build_app",
    description="Build Application",
    task_type=TaskType.PRE_ASSIGNED,
    status=TaskStatus.IN_PROGRESS
)
ledger.tasks["build_app"] = parent
ledger.task_order.append("build_app")

# Create parallel sibling tasks
siblings = ledger.create_sibling_tasks(
    "build_app",
    ["Build frontend", "Build backend", "Write tests"]
)

# Agent executes in parallel
for task in siblings:
    task.start(f"Agent: parallel execution")
    # Execute in separate threads/processes

# Check when all complete
if parent.has_all_children_completed(ledger):
    ledger.update_task_status("build_app", TaskStatus.COMPLETED)
```

### Example 3: Hierarchical Workflow

```python
# Create root task
root = Task(task_id="project", description="Complete Project", ...)
ledger.tasks["project"] = root
ledger.task_order.append("project")

# Create phase tasks
phase1 = ledger.create_parent_child_task("project", "Phase 1: Setup")
phase2 = ledger.create_parent_child_task("project", "Phase 2: Development")
phase3 = ledger.create_parent_child_task("project", "Phase 3: Testing")

# Create subtasks under each phase
phase1_steps = ledger.create_sequential_tasks(
    ["Install deps", "Configure", "Init DB"],
    parent_task_id=phase1.task_id
)

# Block phase 2 on phase 1
phase2.add_blocking_task(phase1.task_id)
phase1.add_dependent_task(phase2.task_id)

# Execute phase 1...
# When complete, phase 2 auto-resumes

# Visualize progress
print(ledger.visualize_task_tree("project"))
```

### Example 4: Inter-Task Communication

```python
# Create tasks with data dependencies
tasks = ledger.create_sequential_tasks([
    "Fetch user data",
    "Process user data",
    "Generate report"
])

# Complete first task with result
ledger.update_task_status(
    tasks[0].task_id,
    TaskStatus.COMPLETED,
    result={"users": [{"id": 1, "name": "Alice"}]}
)

# Second task auto-resumes and receives result
assert tasks[1].status == TaskStatus.IN_PROGRESS

# Get prerequisite results
results = tasks[1].get_prerequisite_results()
users = results[tasks[0].task_id]["users"]
# Use users data in processing...
```

---

## Agent Integration Patterns

### Pattern 1: Event-Driven Sequential Execution

```python
from task_ledger import SmartLedger, TaskStatus
from datetime import datetime

class EventDrivenAgent:
    def __init__(self, user_id: int, prompt_id: int):
        self.ledger = SmartLedger(user_id, prompt_id)
        self.last_event_check = datetime.now().isoformat()

    def run_workflow(self, steps: List[str]):
        """Execute sequential workflow with event-driven architecture."""
        # Create tasks (ledger handles blocking)
        tasks = self.ledger.create_sequential_tasks(steps)

        # Start first task
        tasks[0].start("Agent: beginning workflow")
        self.execute_task(tasks[0])

        # Event-driven loop
        while True:
            # Check for new events
            events = self.ledger.get_events(since=self.last_event_check)
            self.last_event_check = datetime.now().isoformat()

            if not events:
                break  # No new events, all done

            for event in events:
                self.handle_event(event)

        print("✓ Workflow complete!")

    def handle_event(self, event: dict):
        """React to ledger events."""
        if event["type"] == "task_completed":
            auto_resumed = event["data"].get("auto_resumed_tasks", [])

            for task_id in auto_resumed:
                task = self.ledger.get_task(task_id)
                results = task.get_prerequisite_results()
                self.execute_task(task, prerequisite_results=results)

        elif event["type"] == "task_auto_resumed":
            task_id = event["data"]["task_id"]
            print(f"→ Ledger auto-resumed: {task_id}")

    def execute_task(self, task, prerequisite_results=None):
        """Execute task with context from prerequisites."""
        print(f"▶ Executing: {task.description}")

        # Use prerequisite results if available
        context = prerequisite_results or {}

        # Simulate work
        result = {"status": "success", "task_id": task.task_id}

        # Complete task (ledger auto-resumes next)
        self.ledger.update_task_status(
            task.task_id,
            TaskStatus.COMPLETED,
            result=result
        )

# Usage
agent = EventDrivenAgent(user_id=999, prompt_id=9001)
agent.run_workflow([
    "Initialize",
    "Process data",
    "Generate report",
    "Send notification"
])
```

### Pattern 2: Parallel Task Management

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

class ParallelAgent:
    def __init__(self, user_id: int, prompt_id: int):
        self.ledger = SmartLedger(user_id, prompt_id)
        self.executor = ThreadPoolExecutor(max_workers=4)

    def execute_parallel_workflow(self, parent_id: str):
        """Execute all children of a parent in parallel."""
        parent = self.ledger.get_task(parent_id)
        children = [
            self.ledger.get_task(cid)
            for cid in parent.child_task_ids
        ]

        # Start all children
        futures = []
        for child in children:
            child.start(f"Agent: parallel execution")
            future = self.executor.submit(self.execute_task, child)
            futures.append(future)

        # Wait for all to complete
        for future in futures:
            future.result()

        # Check if parent should be marked complete
        if parent.has_all_children_completed(self.ledger):
            self.ledger.update_task_status(
                parent_id,
                TaskStatus.COMPLETED,
                result={"all_children": "completed"}
            )

    def execute_task(self, task):
        """Execute single task."""
        # Do work...
        result = {"task_id": task.task_id, "status": "done"}

        self.ledger.update_task_status(
            task.task_id,
            TaskStatus.COMPLETED,
            result=result
        )
```

### Pattern 3: Smart Dependency Resolution

```python
class IntelligentAgent:
    def __init__(self, user_id: int, prompt_id: int):
        self.ledger = SmartLedger(user_id, prompt_id)
        self.priority_map = {}

    def smart_executor(self):
        """
        Agent continuously checks for work and makes intelligent decisions.
        """
        while True:
            # Get ready tasks
            ready = self.ledger.get_tasks_ready_to_resume()
            pending = self.ledger.get_tasks_by_status(TaskStatus.PENDING)

            available = ready + pending
            if not available:
                break

            # Agent applies intelligence to prioritize
            task = self.select_highest_priority(available)

            # Get dependency context
            dep_status = self.ledger.get_dependency_status(task.task_id)
            prereq_results = task.get_prerequisite_results()

            # Execute with full context
            self.execute_with_context(task, dep_status, prereq_results)

    def select_highest_priority(self, tasks: List[Task]) -> Task:
        """Agent decides which task to work on (intelligence)."""
        # Could consider:
        # - Task priority
        # - Resource availability
        # - Expected duration
        # - Business value
        # - User waiting
        return max(tasks, key=lambda t: self.priority_map.get(t.task_id, 0))

    def execute_with_context(self, task, dep_status, prereq_results):
        """Execute task with full dependency context."""
        # Agent uses context to execute intelligently
        # ...
        pass
```

### Pattern 4: Complex Multi-Level Workflow

```python
class MultiLevelWorkflowAgent:
    def __init__(self, user_id: int, prompt_id: int):
        self.ledger = SmartLedger(user_id, prompt_id)

    def create_complex_workflow(self):
        """
        Create complex nested workflow:

        Root: Build Application
        ├── Phase 1: Setup (sequential)
        │   ├── Install dependencies
        │   ├── Configure environment
        │   └── Initialize database
        ├── Phase 2: Development (parallel)
        │   ├── Build frontend
        │   ├── Build backend
        │   └── Write tests
        └── Phase 3: Deploy (sequential)
            ├── Run tests
            ├── Build production
            └── Deploy to server
        """
        # Create root
        root = Task(
            task_id="build_app",
            description="Build Application",
            task_type=TaskType.PRE_ASSIGNED,
            status=TaskStatus.IN_PROGRESS
        )
        self.ledger.tasks["build_app"] = root
        self.ledger.task_order.append("build_app")

        # Phase 1: Sequential setup
        phase1 = self.ledger.create_sequential_tasks(
            ["Install dependencies", "Configure environment", "Initialize database"],
            parent_task_id="build_app"
        )

        # Phase 2: Parallel development (blocked on Phase 1)
        phase2_parent = Task(
            task_id="phase2",
            description="Development Phase",
            task_type=TaskType.PRE_ASSIGNED,
            status=TaskStatus.BLOCKED,
            parent_task_id="build_app"
        )
        phase2_parent.add_blocking_task(phase1[-1].task_id)
        phase1[-1].add_dependent_task("phase2")

        self.ledger.tasks["phase2"] = phase2_parent
        self.ledger.task_order.append("phase2")
        root.add_child_task("phase2")

        phase2_tasks = self.ledger.create_sibling_tasks(
            "phase2",
            ["Build frontend", "Build backend", "Write tests"]
        )

        # Phase 3: Sequential deploy (blocked on Phase 2)
        phase3_parent = Task(
            task_id="phase3",
            description="Deployment Phase",
            task_type=TaskType.PRE_ASSIGNED,
            status=TaskStatus.BLOCKED,
            parent_task_id="build_app"
        )
        phase3_parent.add_blocking_task("phase2")
        phase2_parent.add_dependent_task("phase3")

        self.ledger.tasks["phase3"] = phase3_parent
        self.ledger.task_order.append("phase3")
        root.add_child_task("phase3")

        phase3_tasks = self.ledger.create_sequential_tasks(
            ["Run tests", "Build production", "Deploy to server"],
            parent_task_id="phase3"
        )

        return root, phase1, phase2_tasks, phase3_tasks

    def execute_workflow(self):
        """Execute complex multi-level workflow."""
        root, phase1, phase2, phase3 = self.create_complex_workflow()

        print("Starting complex workflow...\n")

        # Execute Phase 1 (sequential)
        for task in phase1:
            if task.status == TaskStatus.PENDING:
                task.start("Agent: Phase 1")
            self.execute_task(task)

        # Check events
        events = self.ledger.get_events(event_type="task_auto_resumed")

        # Phase 2 parent should auto-resume
        phase2_parent = self.ledger.get_task("phase2")
        if not phase2_parent.is_blocked():
            phase2_parent.resume("Agent: Phase 1 complete")

            # Execute Phase 2 (parallel)
            self.execute_parallel(phase2)

            # Mark phase 2 parent complete
            if phase2_parent.has_all_children_completed(self.ledger):
                self.ledger.update_task_status("phase2", TaskStatus.COMPLETED)

        # Phase 3 auto-resumes
        events = self.ledger.get_events(event_type="task_auto_resumed")

        phase3_parent = self.ledger.get_task("phase3")
        if not phase3_parent.is_blocked():
            phase3_parent.resume("Agent: Phase 2 complete")

            # Execute Phase 3 (sequential)
            for task in phase3:
                if task.status == TaskStatus.IN_PROGRESS:
                    self.execute_task(task)

        # Show final tree
        print("\nFinal workflow tree:")
        print(self.ledger.visualize_task_tree("build_app"))

    def execute_task(self, task):
        """Execute single task."""
        print(f"▶ {task.description}")
        result = {"status": "success"}
        self.ledger.update_task_status(task.task_id, TaskStatus.COMPLETED, result=result)

    def execute_parallel(self, tasks):
        """Execute tasks in parallel (simulated)."""
        for task in tasks:
            task.start("Agent: parallel")
            self.execute_task(task)
```

---

## Comparison: Before vs. After

### Before (Potential Issue)
```python
# BAD: Ledger making intelligent decisions
if dependent.priority > 5 and resources_available():
    dependent.resume()  # ❌ LLM-style intelligence in ledger
```

### After (Current Implementation)
```python
# GOOD: Ledger applies deterministic rule
if dependent.status == BLOCKED and not dependent.is_blocked():
    dependent.resume()  # ✅ Pure state transition (deterministic)
    ledger._generate_event("task_auto_resumed", {...})  # ✅ Notify agent

# Agent observes event and applies intelligence
events = ledger.get_events()
for event in events:
    if event["type"] == "task_auto_resumed":
        task = ledger.get_task(event["data"]["task_id"])

        # ✅ Agent applies intelligence
        if agent.should_work_on(task):  # Priority, resources, context
            agent.execute(task)
```

---

## Summary

### Implementation Complete ✅

**Ledger Capabilities (Deterministic)**:
- ✅ Nested task relationships (parent-child, siblings, sequential)
- ✅ Dependency tracking and graph updates
- ✅ Deterministic auto-resume when `blocked_by` becomes empty
- ✅ Inter-task message delivery
- ✅ Event generation for agent notification
- ✅ Query methods for dependency status and hierarchy
- ✅ Tree visualization

**Agent Capabilities (Intelligence)**:
- ✅ Event observation and reaction
- ✅ Task selection and prioritization
- ✅ Execution strategy
- ✅ Resource management
- ✅ Context-aware execution
- ✅ Error handling

**Key Architectural Principles**:
- **Ledger** = Eventually consistent state machine (deterministic)
- **Agent** = All decision-making intelligence (LLM-powered)
- **Events** = Notification nudges from ledger to agent

**Benefits**:
1. Clear separation of concerns
2. No intelligence overlap
3. Event-driven (efficient, no polling)
4. Eventually consistent
5. Testable and predictable
6. Agent has complete visibility

---

**Date**: 2025-10-25
**Status**: ✅ Production Ready
**Tests**: Pending update for event-driven model
**Documentation**: Complete
