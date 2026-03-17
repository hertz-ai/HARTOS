"""
HevolveSocial - Chat Dispatch

Thin wrapper around the unified dispatch.dispatch_goal().
Kept for backwards compatibility (coding_daemon, tests).
All 3-tier logic (in-process → HTTP → fallback) lives in dispatch.py.
"""
from typing import Optional


def dispatch_to_chat(prompt: str, user_id: str, goal_id: str,
                     goal_type: str = 'coding') -> Optional[str]:
    """Send a coding goal prompt through the unified 3-tier dispatch.

    Delegates to dispatch.dispatch_goal() which handles:
      Tier 1: Direct in-process call (no HTTP, no ports)
      Tier 2: HTTP proxy to backend port
      Tier 3: llama.cpp fallback

    Args:
        prompt: The goal prompt text
        user_id: Agent user_id to dispatch as
        goal_id: The goal identifier
        goal_type: Goal type (default 'coding')

    Returns:
        Response text or None on failure
    """
    from integrations.agent_engine.dispatch import dispatch_goal
    return dispatch_goal(prompt, user_id, goal_id, goal_type=goal_type)
