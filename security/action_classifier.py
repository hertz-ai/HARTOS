"""
Action Classifier — Destructive Action Detection + Preview Gate

Classifies action text as 'safe', 'destructive', or 'unknown'.
Used by lifecycle_hooks.py to gate destructive operations behind
an opt-in preview approval flow.

Usage:
    from security.action_classifier import classify_action, should_preview

    cls = classify_action("DELETE FROM users WHERE id=5")  # 'destructive'
    if should_preview(action_text, preview_enabled=True):
        # Route to PREVIEW_PENDING state
"""

import re
import logging
from typing import Literal

logger = logging.getLogger('hevolve_security')

ActionClass = Literal['safe', 'destructive', 'unknown']

# Patterns indicating destructive operations
DESTRUCTIVE_PATTERNS = [
    re.compile(r'\b(delete|remove|drop|truncate|destroy|overwrite|erase|purge|wipe)\b', re.I),
    re.compile(r'\brm\s+(-[rf]+\s+)?/', re.I),
    re.compile(r'\bDELETE\s+FROM\b', re.I),
    re.compile(r'\bDROP\s+(TABLE|DATABASE|INDEX|SCHEMA)\b', re.I),
    re.compile(r'\bTRUNCATE\s+(TABLE)?\b', re.I),
    re.compile(r'\bformat\s+[a-zA-Z]:', re.I),
    re.compile(r'\bmkfs\b', re.I),
    re.compile(r'\bdd\s+if=', re.I),
    re.compile(r'\bgit\s+(push\s+--force|reset\s+--hard|clean\s+-fd)', re.I),
    re.compile(r'\bkill\s+-9\b', re.I),
    re.compile(r'\bshutdown\b', re.I),
    re.compile(r'\breboot\b', re.I),
]

# Patterns that are clearly read-only / safe
SAFE_PATTERNS = [
    re.compile(r'\b(read|get|list|show|describe|explain|search|query|fetch|view|check|status)\b', re.I),
    re.compile(r'\bSELECT\b(?!.*\bINTO\b)', re.I),
    re.compile(r'\bcat\s', re.I),
    re.compile(r'\bls\b', re.I),
    re.compile(r'\bgit\s+(status|log|diff|show|branch)\b', re.I),
]


def classify_action(action_text: str) -> ActionClass:
    """
    Classify an action as safe, destructive, or unknown.

    Destructive takes priority: if both safe and destructive patterns match,
    the action is classified as destructive (fail-safe).
    """
    if not action_text or not action_text.strip():
        return 'unknown'

    is_destructive = any(p.search(action_text) for p in DESTRUCTIVE_PATTERNS)
    is_safe = any(p.search(action_text) for p in SAFE_PATTERNS)

    if is_destructive:
        logger.info(f"Action classified as DESTRUCTIVE: {action_text[:80]}")
        return 'destructive'

    if is_safe:
        return 'safe'

    return 'unknown'


def should_preview(action_text: str, preview_enabled: bool = False) -> bool:
    """
    Determine if an action should go through the preview approval flow.

    Preview is opt-in. When enabled, destructive and unknown actions
    require user approval before execution.

    Args:
        action_text: The action to classify
        preview_enabled: Whether the user/agent has opted into preview mode

    Returns:
        True if the action should be routed to PREVIEW_PENDING
    """
    if not preview_enabled:
        return False

    classification = classify_action(action_text)
    return classification in ('destructive', 'unknown')
