"""
Input Sanitization & Validation
Prevents SQL LIKE injection, path traversal, XSS, and input abuse.
"""

import re
import os
import html
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger('hevolve_security')


def escape_like(value: str) -> str:
    """
    Escape SQL LIKE wildcards to prevent LIKE injection.
    Users searching for '%' would otherwise match everything.
    """
    return (
        value
        .replace('\\', '\\\\')
        .replace('%', '\\%')
        .replace('_', '\\_')
    )


def sanitize_path(user_input: str, base_dir: str) -> str:
    """
    Validate a file path stays within base_dir.
    Raises ValueError on path traversal attempt.

    Usage:
        safe_path = sanitize_path(f"{prompt_id}.json", "prompts")
    """
    base = Path(base_dir).resolve()
    # Strip any path separators from the input
    cleaned = user_input.replace('..', '').replace('/', '').replace('\\', '')
    target = (base / cleaned).resolve()

    if not str(target).startswith(str(base)):
        logger.warning(f"Path traversal blocked: {user_input!r} escapes {base_dir}")
        raise ValueError(f"Invalid path: {user_input}")

    return str(target)


def sanitize_html(text: str) -> str:
    """
    Escape HTML entities to prevent stored XSS.
    Apply to all user-generated text before JSON serialization.
    """
    if not isinstance(text, str):
        return text
    return html.escape(text, quote=True)


def validate_input(
    value: str,
    max_length: int = 10000,
    min_length: int = 0,
    pattern: Optional[str] = None,
    field_name: str = 'input',
) -> str:
    """
    Validate input string against length and pattern constraints.
    Raises ValueError with descriptive message on failure.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")

    value = value.strip()

    if len(value) < min_length:
        raise ValueError(f"{field_name} must be at least {min_length} characters")

    if len(value) > max_length:
        raise ValueError(f"{field_name} exceeds maximum length of {max_length}")

    if pattern and not re.match(pattern, value):
        raise ValueError(f"{field_name} contains invalid characters")

    return value


def validate_prompt_id(prompt_id) -> str:
    """Validate prompt_id is a safe integer string."""
    pid = str(prompt_id).strip()
    if not re.match(r'^\d+$', pid):
        raise ValueError(f"Invalid prompt_id: must be numeric, got {pid!r}")
    return pid


def validate_user_id(user_id) -> str:
    """Validate user_id is alphanumeric."""
    uid = str(user_id).strip()
    if not re.match(r'^[a-zA-Z0-9_-]+$', uid):
        raise ValueError(f"Invalid user_id: must be alphanumeric, got {uid!r}")
    return uid


def validate_username(username: str) -> str:
    """Validate username format for social platform."""
    return validate_input(
        username,
        max_length=50,
        min_length=2,
        pattern=r'^[a-zA-Z0-9_.@-]+$',
        field_name='username',
    )


def validate_password(password: str) -> str:
    """Validate password meets minimum requirements."""
    return validate_input(
        password,
        max_length=128,
        min_length=8,
        field_name='password',
    )


def validate_search_query(query: str) -> str:
    """Validate and sanitize search query."""
    return validate_input(
        query,
        max_length=200,
        min_length=1,
        field_name='search query',
    )


def validate_post_content(content: str) -> str:
    """Validate post content length."""
    return validate_input(
        content,
        max_length=40000,
        min_length=1,
        field_name='post content',
    )


def validate_comment(content: str) -> str:
    """Validate comment content length."""
    return validate_input(
        content,
        max_length=10000,
        min_length=1,
        field_name='comment',
    )
