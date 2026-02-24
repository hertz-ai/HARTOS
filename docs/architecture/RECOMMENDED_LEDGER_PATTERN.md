# Recommended Ledger Pattern for This Application

**Date:** 2025-11-09
**Decision:** Use `prompt_id` as `agent_id` and `user_id_prompt_id` as `session_id`

---

## The Pattern

```python
from agent_ledger import SmartLedger

ledger = SmartLedger(
    agent_id=f"{prompt_id}",
    session_id=f"{user_id}_{prompt_id}"
)
```

---

## Why This Makes Sense

### agent_id = prompt_id
- **Prompt ID** identifies the specific agent/conversation instance
- Each prompt creates a distinct agent workflow
- Natural identifier for "which agent is running"

### session_id = user_id_prompt_id
- **Combines user and prompt** for unique session identification
- Matches existing application pattern: `f'{user_id}_{prompt_id}'`
- Allows tracking which user is running which prompt
- Enables multi-user scenarios where different users might use the same prompt

---

## Benefits

1. **Consistency with Existing Code**
   - Your app already uses `f'{user_id}_{prompt_id}'` as the key pattern
   - Minimal cognitive overhead for developers

2. **Semantic Clarity**
   - `agent_id`: Which conversation/prompt (what the agent is doing)
   - `session_id`: Unique session per user+prompt combination (who + what)

3. **Querying Flexibility**
   ```python
   # Find all sessions for a specific agent/prompt
   sessions = ledger_backend.list_keys(f"ledger_{prompt_id}_*")

   # Find all sessions for a specific user
   sessions = ledger_backend.list_keys(f"ledger_*_{user_id}_*")

   # Find specific user+prompt session
   session = ledger_backend.load(f"ledger_{prompt_id}_{user_id}_{prompt_id}")
   ```

---

## Helper Function

Create a helper function in your application code:

```python
# helper.py or wherever appropriate

from agent_ledger import SmartLedger
from typing import Optional, Any

def create_ledger_for_user_prompt(
    user_id: int,
    prompt_id: int,
    backend: Optional[Any] = None
) -> SmartLedger:
    """
    Create a SmartLedger for a specific user and prompt.

    Uses the recommended pattern:
    - agent_id: prompt_id (identifies the agent/conversation)
    - session_id: user_id_prompt_id (unique session identifier)

    Args:
        user_id: User identifier
        prompt_id: Prompt/conversation identifier
        backend: Optional storage backend (Redis, MongoDB, etc.)

    Returns:
        SmartLedger instance configured for this user+prompt

    Example:
        ledger = create_ledger_for_user_prompt(123, 456)
        # Creates: SmartLedger(agent_id="456", session_id="123_456")
    """
    return SmartLedger(
        agent_id=f"{prompt_id}",
        session_id=f"{user_id}_{prompt_id}",
        backend=backend
    )
```

---

## Usage Examples

### In create_recipe.py
```python
from helper import create_ledger_for_user_prompt

def execute_python_file(task_description: str, user_id: int, prompt_id: int, action_entry_point: int = 0):
    # Create ledger
    ledger = create_ledger_for_user_prompt(user_id, prompt_id)

    # Use ledger
    ledger.add_task(Task('task1', 'Process data', TaskType.PRE_ASSIGNED))
    # ...
```

### In reuse_recipe.py
```python
from helper import create_ledger_for_user_prompt

def reuse_recipe(user_id: int, prompt_id: int):
    # Create ledger
    ledger = create_ledger_for_user_prompt(user_id, prompt_id)

    # Integrate with Action class
    action.ledger = ledger
    # ...
```

### With Production Backend
```python
from agent_ledger.backends import RedisBackend
from helper import create_ledger_for_user_prompt

# Use Redis for production
redis_backend = RedisBackend(host='localhost', port=6379)
ledger = create_ledger_for_user_prompt(
    user_id=123,
    prompt_id=456,
    backend=redis_backend
)
```

---

## File Storage Pattern

With this pattern, ledger files are stored as:

```
agent_data/
├── ledger_456_123_456.json     # prompt_id=456, session=123_456
├── ledger_456_789_456.json     # prompt_id=456, session=789_456 (different user)
├── ledger_789_123_789.json     # prompt_id=789, session=123_789 (different prompt)
└── ...
```

**Key structure:** `ledger_{agent_id}_{session_id}.json`
- `agent_id` = prompt_id
- `session_id` = user_id_prompt_id

---

## Migration from Old Pattern

If you have existing code using the old API:

**Before:**
```python
ledger = SmartLedger(user_id=123, prompt_id=456)
```

**After:**
```python
ledger = SmartLedger(
    agent_id=f"{prompt_id}",
    session_id=f"{user_id}_{prompt_id}"
)

# Or use the helper
ledger = create_ledger_for_user_prompt(user_id=123, prompt_id=456)
```

---

## Summary

**Recommended Pattern:**
```python
SmartLedger(
    agent_id=f"{prompt_id}",           # What agent/conversation
    session_id=f"{user_id}_{prompt_id}" # Who + what (unique session)
)
```

This pattern:
- ✅ Matches your existing application design
- ✅ Provides semantic clarity
- ✅ Enables flexible querying
- ✅ Supports multi-user scenarios
- ✅ Maintains consistency with `user_id_{prompt_id}` pattern throughout your codebase
