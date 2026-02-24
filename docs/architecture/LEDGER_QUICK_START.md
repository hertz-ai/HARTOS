# Agent Ledger - Quick Start Guide

**For This Application:** Using the recommended pattern

---

## 🚀 Quick Start (3 Lines)

```python
from helper_ledger import create_ledger_for_user_prompt

# Create ledger for user+prompt
ledger = create_ledger_for_user_prompt(user_id=123, prompt_id=456)
```

That's it! The helper function uses the recommended pattern automatically:
- `agent_id` = `"{prompt_id}"` (e.g., "456")
- `session_id` = `"{user_id}_{prompt_id}"` (e.g., "123_456")

---

## 📖 Common Operations

### Add a Task
```python
from agent_ledger import Task, TaskType

task = Task(
    task_id='task_1',
    description='Process customer data',
    task_type=TaskType.PRE_ASSIGNED
)
ledger.add_task(task)
```

### Update Task Status
```python
from agent_ledger import TaskStatus

ledger.update_task_status('task_1', TaskStatus.IN_PROGRESS)
ledger.update_task_status('task_1', TaskStatus.COMPLETED, result={'count': 100})
```

### Get Progress
```python
summary = ledger.get_progress_summary()
# Returns: {'total': 5, 'completed': 2, 'in_progress': 1, 'pending': 2, 'progress': '40.0%'}
```

### Integrate with Action Class
```python
# In your existing code (create_recipe.py, reuse_recipe.py)
from helper_ledger import create_ledger_for_user_prompt

# Create action as usual
action = Action(...)

# Attach ledger
action.ledger = create_ledger_for_user_prompt(user_id, prompt_id)

# Now action has persistent task memory!
```

---

## ⚡ With Production Backend (Redis)

```python
from helper_ledger import create_ledger_with_auto_backend

# Automatically uses Redis if available, falls back to JSON
ledger = create_ledger_with_auto_backend(user_id=123, prompt_id=456)
```

Or specify Redis explicitly:

```python
from agent_ledger.backends import RedisBackend
from helper_ledger import create_ledger_for_user_prompt

redis = RedisBackend(host='localhost', port=6379)
ledger = create_ledger_for_user_prompt(123, 456, backend=redis)
```

**Performance:** Redis is 10-50x faster than JSON files!

---

## 🔧 Helper Functions Reference

### `create_ledger_for_user_prompt(user_id, prompt_id, backend=None)`
Create ledger using recommended pattern.

**Example:**
```python
ledger = create_ledger_for_user_prompt(123, 456)
# Creates: SmartLedger(agent_id="456", session_id="123_456")
```

### `create_ledger_with_auto_backend(user_id, prompt_id, prefer_redis=True)`
Create ledger with automatic backend selection.

**Example:**
```python
ledger = create_ledger_with_auto_backend(123, 456)
# Uses Redis if available, otherwise JSON
```

### `get_ledger_key(user_id, prompt_id)`
Get the storage key for a user+prompt.

**Example:**
```python
key = get_ledger_key(123, 456)
# Returns: "ledger_456_123_456"
```

---

## 📁 File Organization

Ledger files are stored in `agent_data/`:

```
agent_data/
├── ledger_456_123_456.json     # prompt_id=456, user_id=123
├── ledger_456_789_456.json     # prompt_id=456, user_id=789 (different user)
├── ledger_789_123_789.json     # prompt_id=789, user_id=123 (different prompt)
└── ...
```

Pattern: `ledger_{agent_id}_{session_id}.json`

---

## 🎯 Real-World Example

```python
from helper_ledger import create_ledger_for_user_prompt
from agent_ledger import Task, TaskType, TaskStatus

def execute_python_file(task_description: str, user_id: int, prompt_id: int, action_entry_point: int = 0):
    # Create ledger
    ledger = create_ledger_for_user_prompt(user_id, prompt_id)

    # Create tasks from actions
    for i, action in enumerate(actions):
        task = Task(
            task_id=f'action_{i}',
            description=action['description'],
            task_type=TaskType.PRE_ASSIGNED
        )
        ledger.add_task(task)

    # Execute actions
    for task_id in ledger.get_ready_tasks():
        ledger.update_task_status(task_id, TaskStatus.IN_PROGRESS)

        try:
            result = execute_action(task_id)
            ledger.update_task_status(task_id, TaskStatus.COMPLETED, result=result)
        except Exception as e:
            ledger.update_task_status(task_id, TaskStatus.FAILED, error=str(e))

    # Check progress
    progress = ledger.get_progress_summary()
    print(f"Completed {progress['completed']}/{progress['total']} tasks ({progress['progress']})")
```

---

## 🔍 Querying Pattern

### Find all sessions for a specific prompt
```python
from pathlib import Path

agent_data = Path("agent_data")
prompt_456_sessions = list(agent_data.glob("ledger_456_*.json"))
# Returns all sessions for prompt_id=456
```

### Find all sessions for a specific user
```python
user_123_sessions = [f for f in agent_data.glob("ledger_*_123_*.json")]
# Returns all sessions where user_id=123
```

---

## 💡 Migration from Old Code

**Old code:**
```python
ledger = SmartLedger(user_id=123, prompt_id=456)
```

**New code:**
```python
from helper_ledger import create_ledger_for_user_prompt

ledger = create_ledger_for_user_prompt(user_id=123, prompt_id=456)
```

The helper function handles the mapping automatically!

---

## 📚 Full Documentation

For complete API reference, see:
- `agent_ledger/README.md` - Full library documentation
- `RECOMMENDED_LEDGER_PATTERN.md` - Pattern explanation
- `API_CHANGE_SUMMARY.md` - API migration guide

---

## ✅ Summary

**Recommended Pattern:**
```python
ledger = create_ledger_for_user_prompt(user_id, prompt_id)
```

**What it does:**
- `agent_id` = `f"{prompt_id}"` (identifies the agent/conversation)
- `session_id` = `f"{user_id}_{prompt_id}"` (unique session per user+prompt)

**Benefits:**
- ✅ Matches existing application pattern
- ✅ Simple, one-line creation
- ✅ Consistent across codebase
- ✅ Supports multi-user scenarios
- ✅ Easy to query and debug

---

**Ready to use!** Just import `helper_ledger` and start tracking tasks.
