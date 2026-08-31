"""
Helper functions for creating SmartLedger instances with the recommended pattern.

This module provides convenience functions for creating ledgers using the
application's standard pattern: agent_id=prompt_id, session_id=user_id_prompt_id

Factory functions (convenience wrappers):
- create_ledger_for_user_prompt - Create ledger with standard naming
- create_ledger_with_auto_backend - Create with Redis/JSON auto-selection
- get_ledger_key - Get storage key for a user+prompt combination

Core functionality is now part of SmartLedger class:
- ledger.add_dynamic_task() - Add runtime-discovered tasks with LLM classification
- ledger.get_next_executable_task() - Get next task respecting dependencies
- ledger.get_parallel_executable_tasks() - Get parallel-ready tasks
- ledger.complete_task_and_route() - Complete and route to next
- ledger.get_awareness() - Full execution context for agents
- ledger.get_awareness_text() - Text format for prompts
- ledger.add_subtasks() - Add child tasks
- ledger.get_pending_subtasks() - Get pending children
"""

try:
    from agent_ledger import SmartLedger, Task, TaskType, TaskStatus as LedgerTaskStatus
except ImportError:
    SmartLedger = None
    Task = None
    TaskType = None
    LedgerTaskStatus = None
from typing import Optional, Any, Dict, List
import logging
import os

logger = logging.getLogger(__name__)


def create_ledger_for_user_prompt(
    user_id: int,
    prompt_id: int,
    backend: Optional[Any] = None
) -> SmartLedger:
    """
    Create a SmartLedger for a specific user and prompt using the recommended pattern.

    Pattern:
    - agent_id: prompt_id (identifies the agent/conversation)
    - session_id: user_id_prompt_id (unique session identifier)

    This matches the application's existing pattern of using f'{user_id}_{prompt_id}'
    as the key throughout the codebase.

    Args:
        user_id: User identifier
        prompt_id: Prompt/conversation identifier
        backend: Optional storage backend (Redis, MongoDB, JSON, etc.)

    Returns:
        SmartLedger instance configured for this user+prompt

    Example:
        >>> ledger = create_ledger_for_user_prompt(123, 456)
        >>> print(ledger)
        SmartLedger(456:123_456, 0 tasks, 0.0% complete)

        >>> # With Redis backend
        >>> from agent_ledger.backends import RedisBackend
        >>> redis = RedisBackend(host='localhost', port=6379)
        >>> ledger = create_ledger_for_user_prompt(123, 456, backend=redis)
    """
    return SmartLedger(
        agent_id=f"{prompt_id}",
        session_id=f"{user_id}_{prompt_id}",
        backend=backend
    )


def create_ledger_with_auto_backend(
    user_id: int,
    prompt_id: int,
    prefer_redis: bool = True
) -> SmartLedger:
    """
    Create a ledger with automatic backend selection (Redis → JSON fallback).

    Tries to use Redis for production performance, falls back to JSON if unavailable.

    Args:
        user_id: User identifier
        prompt_id: Prompt/conversation identifier
        prefer_redis: If True, try Redis first (default: True)

    Returns:
        SmartLedger with best available backend

    Example:
        >>> ledger = create_ledger_with_auto_backend(123, 456)
        # Automatically uses Redis if available, JSON otherwise
    """
    from agent_ledger.factory import create_production_ledger

    return create_production_ledger(
        agent_id=f"{prompt_id}",
        session_id=f"{user_id}_{prompt_id}",
        prefer_redis=prefer_redis
    )


def get_ledger_key(user_id: int, prompt_id: int) -> str:
    """
    Get the ledger key for a user+prompt combination.

    Useful for querying backends directly or debugging.

    Args:
        user_id: User identifier
        prompt_id: Prompt/conversation identifier

    Returns:
        Ledger key string in format: "ledger_{prompt_id}_{user_id}_{prompt_id}"

    Example:
        >>> key = get_ledger_key(123, 456)
        >>> print(key)
        'ledger_456_123_456'
    """
    agent_id = f"{prompt_id}"
    session_id = f"{user_id}_{prompt_id}"
    return f"ledger_{agent_id}_{session_id}"


# =============================================================================
# CONVENIENCE WRAPPERS (delegate to SmartLedger methods)
# These exist for backwards compatibility with existing code
# =============================================================================

def add_subtasks_to_ledger(
    user_prompt: str,
    parent_action_id: int,
    subtasks: List[Dict],
    user_ledgers: Dict
) -> bool:
    """
    Add subtasks from LLM response to the agent ledger.

    DEPRECATED: Use ledger.add_subtasks() directly instead.

    Args:
        user_prompt: The user_prompt key (e.g., "123_456")
        parent_action_id: The parent action ID (e.g., 1)
        subtasks: List of subtask dicts from LLM response
        user_ledgers: The global user_ledgers dict

    Returns:
        bool: True if subtasks were added successfully
    """
    if user_prompt not in user_ledgers:
        logger.warning(f"No ledger found for user_prompt: {user_prompt}")
        return False

    ledger = user_ledgers[user_prompt]
    return ledger.add_subtasks(parent_action_id, subtasks)


def check_and_unblock_parent(user_prompt: str, completed_subtask_id: str, user_ledgers: Dict) -> bool:
    """
    Check if all subtasks are complete and unblock parent task if so.

    DEPRECATED: This is now handled automatically by ledger.complete_task_and_route()

    Args:
        user_prompt: The user_prompt key
        completed_subtask_id: The subtask that just completed
        user_ledgers: The global user_ledgers dict

    Returns:
        bool: True if parent was unblocked
    """
    if user_prompt not in user_ledgers:
        return False

    ledger = user_ledgers[user_prompt]
    return ledger._check_and_unblock_parent(completed_subtask_id)


def get_pending_subtasks(user_prompt: str, parent_action_id: int, user_ledgers: Dict) -> List[Task]:
    """
    Get all pending subtasks for a parent action.

    DEPRECATED: Use ledger.get_pending_subtasks() directly instead.

    Args:
        user_prompt: The user_prompt key
        parent_action_id: The parent action ID
        user_ledgers: The global user_ledgers dict

    Returns:
        List of pending Task objects
    """
    if user_prompt not in user_ledgers:
        return []

    ledger = user_ledgers[user_prompt]
    return ledger.get_pending_subtasks(parent_action_id)


def add_dynamic_task(
    user_prompt: str,
    task_description: str,
    user_ledgers: Dict,
    context: Dict,
    llm_client: Any = None
) -> Optional[Task]:
    """
    Add a dynamically discovered task with LLM auto-classification.

    DEPRECATED: Use ledger.add_dynamic_task() directly instead.

    Args:
        user_prompt: The user_prompt key (e.g., "123_456")
        task_description: Description of the new task
        user_ledgers: The global user_ledgers dict
        context: Context dict with current_action_id, previous_outcome, user_message
        llm_client: Optional LLM client for classification

    Returns:
        The created Task object, or None if failed
    """
    if user_prompt not in user_ledgers:
        logger.warning(f"No ledger found for user_prompt: {user_prompt}")
        return None

    ledger = user_ledgers[user_prompt]
    return ledger.add_dynamic_task(task_description, context, llm_client)


def get_next_executable_task(user_prompt: str, user_ledgers: Dict) -> Optional[Task]:
    """
    Get the next task that can be executed based on relationships and outcomes.

    DEPRECATED: Use ledger.get_next_executable_task() directly instead.

    Args:
        user_prompt: The user_prompt key
        user_ledgers: The global user_ledgers dict

    Returns:
        Next executable Task, or None
    """
    if user_prompt not in user_ledgers:
        return None

    ledger = user_ledgers[user_prompt]
    return ledger.get_next_executable_task()


def get_parallel_executable_tasks(user_prompt: str, user_ledgers: Dict) -> List[Task]:
    """
    Get all tasks that can be executed in parallel right now.

    DEPRECATED: Use ledger.get_parallel_executable_tasks() directly instead.

    Args:
        user_prompt: The user_prompt key
        user_ledgers: The global user_ledgers dict

    Returns:
        List of tasks that can run in parallel
    """
    if user_prompt not in user_ledgers:
        return []

    ledger = user_ledgers[user_prompt]
    return ledger.get_parallel_executable_tasks()


def complete_task_and_route(
    user_prompt: str,
    task_id: str,
    outcome: str,
    result: Any,
    user_ledgers: Dict
) -> Optional[Task]:
    """
    Complete a task and determine what should run next based on outcome.

    DEPRECATED: Use ledger.complete_task_and_route() directly instead.

    Args:
        user_prompt: The user_prompt key
        task_id: ID of completed task
        outcome: 'success' or 'failure'
        result: Result data from task execution
        user_ledgers: The global user_ledgers dict

    Returns:
        Next task to execute, or None
    """
    if user_prompt not in user_ledgers:
        return None

    ledger = user_ledgers[user_prompt]
    return ledger.complete_task_and_route(task_id, outcome, result)


def get_task_execution_summary(user_prompt: str, user_ledgers: Dict) -> Dict:
    """
    Get a summary of task execution status for the ledger.

    DEPRECATED: Use ledger.get_execution_summary() directly instead.

    Args:
        user_prompt: The user_prompt key
        user_ledgers: The global user_ledgers dict

    Returns:
        Dict with counts and lists of tasks by status
    """
    if user_prompt not in user_ledgers:
        return {"error": "No ledger found"}

    ledger = user_ledgers[user_prompt]
    return ledger.get_execution_summary()


def get_agent_awareness(user_prompt: str, user_ledgers: Dict) -> Dict:
    """
    Get complete execution awareness for the agent.

    DEPRECATED: Use ledger.get_awareness() directly instead.

    This is the PRIMARY function agents should call to understand:
    1. What tasks have been executed and their outcomes
    2. What tasks are currently executing
    3. What is the next course of action for each executing task

    Args:
        user_prompt: The user_prompt key (e.g., "123_456")
        user_ledgers: The global user_ledgers dict

    Returns:
        Comprehensive awareness dict
    """
    if user_prompt not in user_ledgers:
        return {
            "error": "No ledger found",
            "user_prompt": user_prompt,
            "executed_tasks": [],
            "executing_tasks": [],
            "pending_tasks": [],
            "blocked_tasks": [],
            "recommended_action": "No ledger - initialize first"
        }

    ledger = user_ledgers[user_prompt]
    return ledger.get_awareness()


def get_agent_awareness_text(user_prompt: str, user_ledgers: Dict) -> str:
    """
    Get agent awareness as formatted text for injection into prompts.

    DEPRECATED: Use ledger.get_awareness_text() directly instead.

    Args:
        user_prompt: The user_prompt key
        user_ledgers: The global user_ledgers dict

    Returns:
        Formatted string with execution context
    """
    if user_prompt not in user_ledgers:
        return f"[LEDGER ERROR: No ledger found for {user_prompt}]"

    ledger = user_ledgers[user_prompt]
    return ledger.get_awareness_text()


# =============================================================================
# LLM CLASSIFICATION SUPPORT
# These are kept here as they're LLM-specific and not part of core ledger
# =============================================================================

# Classification prompt for LLM - used by SmartLedger._classify_task_relationship
TASK_CLASSIFICATION_PROMPT = """You are a task relationship analyzer. Given existing tasks and a new task, determine the relationship.

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


def get_default_llm_client():
    """
    Get default LLM client for task classification.

    DEPRECATED: The ledger is a data structure and should not have LLM
    dependencies. Callers should pass their own ``llm_client`` to methods
    that require language-model inference.

    Raises:
        NotImplementedError: Always. Callers must supply their own LLM client.
    """
    raise NotImplementedError(
        "get_default_llm_client() is removed. The ledger is framework-agnostic "
        "and does not bundle a default LLM client. Pass llm_client= to methods "
        "that require language model inference."
    )


# Example usage
if __name__ == "__main__":
    print("Agent Ledger Helper Functions")
    print("=" * 60)

    # Example 1: Basic ledger creation
    print("\n1. Basic ledger creation:")
    ledger1 = create_ledger_for_user_prompt(user_id=123, prompt_id=456)
    print(f"   Created: {ledger1}")
    print(f"   Key: {get_ledger_key(123, 456)}")

    # Example 2: Using SmartLedger methods directly (RECOMMENDED)
    print("\n2. Using SmartLedger methods directly:")
    print("   >>> ledger = create_ledger_for_user_prompt(123, 456)")
    print("   >>> awareness = ledger.get_awareness()  # Get full context")
    print("   >>> next_task = ledger.get_next_executable_task()  # Get next task")
    print("   >>> ledger.complete_task_and_route('task_1', 'success', result)")

    # Example 3: Dynamic task with LLM classification
    print("\n3. Dynamic task with LLM classification:")
    print("   >>> task = ledger.add_dynamic_task(")
    print("   ...     'Validate credit card before payment',")
    print("   ...     {'current_action_id': 1, 'user_message': 'process order'}")
    print("   ... )")

    print("\n" + "=" * 60)
    print("Core functionality moved to SmartLedger class!")
    print("Use ledger.method() instead of helper_function(user_prompt, user_ledgers)")
