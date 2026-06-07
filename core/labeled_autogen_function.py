"""Mandatory-UI-label autogen registration chokepoint (#508/#509).

`autogen.register_function(func, caller=..., executor=..., ...)` stores
the raw Python callable in the executor's `function_map`.  When autogen
later invokes the tool, it bypasses LangChain's `_with_tool_logging`
wrapper, so per-tool UI status events (`publish_chat_stage('tool_call',
…)`) never fire and the chat spinner stays on the generic "Thinking…"
verb during autogen turns.

This module closes that gap:

  1. Requires a `ui_label` kwarg at registration time (TypeError if
     omitted, ValueError if empty) — compile-time enforcement replaces
     an AST drift-guard.

  2. Registers the label into the canonical `TOOL_LABELS` dict via
     `core.constants.register_tool_label`.

  3. Wraps the function with the canonical
     `core.tool_logging.log_tool_execution` decorator, which:
       - Branches sync vs async via `inspect.iscoroutinefunction(func)`.
       - Emits the per-tool UI status BEFORE invocation.
       - Logs entry, args, success result, errors (with traceback).
       - Coerces non-string returns to str.
       - Returns a structured JSON error envelope on exception (autogen
         friendly — the LLM sees the failure as a normal tool output).

  4. Passes the wrapped function to `autogen.register_function` with
     identical `caller`/`executor`/`name`/`description` kwargs.

Per CLAUDE.md Gate 4 (no parallel paths) this is THIN by design:
all the wrap-and-log logic lives in `core.tool_logging`, shared with
the ~65 decorator sites in `create_recipe.py`, `reuse_recipe.py`,
`core/agent_tools.py`, and `integrations/channels/agent_tools.py`.
"""
from __future__ import annotations

from typing import Any, Callable

from autogen import register_function as _autogen_register_function

from core.constants import register_tool_label
from core.tool_logging import log_tool_execution


def register_labeled_function(
    func: Callable[..., Any],
    *,
    caller: Any,
    executor: Any,
    description: str | None = None,
    ui_label: str,
    name: str | None = None,
) -> Any:
    """Register `func` with autogen, wrapping it via the canonical
    `core.tool_logging.log_tool_execution` decorator so the UI spinner
    shows tool-specific status text and the tool gets full structured
    logging.

    Args:
        func: The autogen tool function (sync or async).
        caller: autogen agent that asks for the tool to be invoked.
        executor: autogen agent that runs the function (its function_map
            holds the wrapped func).
        description: LLM-facing description; defaults to func.__doc__.
        ui_label: REQUIRED user-facing spinner text shown when this tool
            fires.  Same enforcement as `core.labeled_tool.labeled_tool`.
        name: Optional explicit tool name (defaults to func.__name__).

    Raises:
        TypeError: if `ui_label` is omitted (Python kwarg enforcement).
        ValueError: if `ui_label` is empty or non-string.

    Returns:
        autogen.register_function's return value.
    """
    if not isinstance(ui_label, str) or not ui_label.strip():
        raise ValueError(
            f"register_labeled_function({func.__name__!r}): ui_label must "
            f"be a non-empty string; pass generic_autogen_label({func.__name__!r}) "
            f"to opt into the 'Running …' fallback"
        )

    tool_name = name or func.__name__
    register_tool_label(tool_name, ui_label)

    # Canonical chokepoint — preserves the sync/async branch, structured
    # error envelope, str-coercion, and publish_chat_stage emit.  All
    # behavior lives in one place (CLAUDE.md Gate 4: no parallel paths).
    wrapped = log_tool_execution(func)

    return _autogen_register_function(
        wrapped,
        caller=caller,
        executor=executor,
        name=tool_name,
        description=description or func.__doc__,
    )


def generic_autogen_label(name: str) -> str:
    """Explicit opt-in to the 'Running {name}…' fallback for autogen
    tools whose name is already self-descriptive.  Use sparingly — a
    real verb phrase is more polished.  Template single-sourced in
    core.constants.generic_tool_label (#116)."""
    from core.constants import generic_tool_label
    return generic_tool_label(name)
