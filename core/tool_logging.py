"""Canonical tool-execution logging decorator (#509 unification).

Originally defined inline at `create_recipe.py:329` and applied as
`@log_tool_execution` on ~40 tool functions across
`core/agent_tools.py` and `integrations/channels/agent_tools.py`.  Moved
here so the *autogen registration chokepoint*
(`core.labeled_autogen_function.register_labeled_function`) can reuse it
without dragging the heavy `create_recipe` module in as a dependency.

Behavior preserved verbatim from the original:
  - Sync vs async branching via inspect.iscoroutinefunction(func).
  - Logs entry with args+kwargs.
  - Coerces non-string returns to str (autogen contract).
  - Sync wrapper: if the underlying func accidentally returns a
    coroutine, drives it to completion via get_event_loop().
  - On exception: logs error + traceback, returns a structured JSON
    error envelope as a string ("Tool execution failed: {...}") so the
    LLM sees the failure as a normal tool message.

#509 extension: emits a per-tool UI status
(`publish_chat_stage('tool_call', text=<TOOL_LABELS[name] or fallback>)`)
BEFORE invoking the function, so the chat spinner shows tool-specific
verb text (e.g. "Searching CRM…") instead of staying on generic
"Thinking…".  The emit is best-effort: failures are LOGGED at warning
level (no silent gulp per user directive 2026-05-11), never block the
tool.

Per CLAUDE.md Gate 4 (no parallel paths) this is THE single canonical
home for tool-execution logging + per-tool UI emit.  `create_recipe.py`
re-exports `log_tool_execution` for backward compatibility with the
existing 40+ decorator sites.  LangChain's `_with_tool_logging` in
`hart_intelligence_entry.py` is functionally similar but operates on
LangChain `Tool.func` objects and has a different error-format
contract (LangChain agent_executor wants a plain string, not the
JSON envelope) — left in place pending a future merge.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from functools import wraps

# Module-level tool_logger named "agent_logger" — matches the legacy
# logger create_recipe.py:284 configured with a RotatingFileHandler.
# When that file is imported (which always happens at HARTOS boot)
# the handler is attached; this module just looks up the same logger.
tool_logger = logging.getLogger("agent_logger")

# Local fallback logger for the UI-emit warning channel (kept distinct
# from agent_logger so emit-failure warnings don't drown the per-tool
# success/error stream).
_emit_logger = logging.getLogger(__name__)


def _emit_tool_call_stage(tool_name: str) -> None:
    """Emit the canonical per-tool UI status event.

    Best-effort: catches and LOGS any failure (publish_chat_stage import
    error, transport error, missing thread-local context) so the tool
    invocation is never blocked by an emit issue.  Per user directive
    2026-05-11: exceptions are logged at warning level, never silently
    swallowed.
    """
    try:
        from core.constants import TOOL_LABELS
        from core.peer_link.crossbar_publish import publish_chat_stage
        from threadlocal import thread_local_data

        user_id = thread_local_data.get_user_id() or ''
        request_id = thread_local_data.get_request_id() or ''
        if user_id:
            publish_chat_stage(
                'tool_call',
                user_id=str(user_id),
                request_id=str(request_id),
                text=TOOL_LABELS.get(tool_name, f'Running {tool_name}…'),
            )
        else:
            _emit_logger.debug(
                "[tool_logging] %s invoked with no chat context; "
                "UI emit skipped",
                tool_name,
            )
    except Exception:
        _emit_logger.warning(
            "[tool_logging] UI emit for %s failed",
            tool_name, exc_info=True,
        )


def _error_envelope(func_name: str, exc: BaseException) -> str:
    """Structured JSON error returned to the LLM on tool failure.

    Preserves the legacy contract from create_recipe.py:344-359 so the
    40+ existing tools using `@log_tool_execution` see identical output.
    """
    payload = {
        "status": "error",
        "tool_function": func_name,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "suggestion": "Check logs for detailed traceback information",
    }
    return f"Tool execution failed: {json.dumps(payload)}"


def log_tool_execution(func):
    """Decorator wrapping a tool function with logging + UI emit.

    Handles both sync and async callables.  Apply with `@log_tool_execution`
    or programmatically as `wrapped = log_tool_execution(func)`.

    See module docstring for the full behavior contract.
    """
    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            tool_logger.info(f"TOOL EXECUTION START: {func.__name__}")
            tool_logger.info(
                f"Arguments: {args}, Keyword Arguments: {kwargs}")
            _emit_tool_call_stage(func.__name__)
            try:
                result = await func(*args, **kwargs)
                if not isinstance(result, str):
                    tool_logger.warning(
                        f"Tool function {func.__name__} returned "
                        f"non-string type: {type(result)}")
                    result = str(result)
                tool_logger.info(
                    f"TOOL EXECUTION SUCCESS: {func.__name__}")
                tool_logger.info(
                    f"Result: {result[:100]}..."
                    if len(result) > 100 else f"Result: {result}"
                )
                return result
            except Exception as e:
                tool_logger.error(
                    f"TOOL EXECUTION ERROR: {func.__name__} - {e}")
                tool_logger.exception("Exception details:")
                envelope = _error_envelope(func.__name__, e)
                tool_logger.info(f"Returning error response: {envelope}")
                return envelope

        return async_wrapper

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        tool_logger.info(f"TOOL EXECUTION START: {func.__name__}")
        tool_logger.info(
            f"Arguments: {args}, Keyword Arguments: {kwargs}")
        _emit_tool_call_stage(func.__name__)
        try:
            result = func(*args, **kwargs)
            # If the sync function accidentally returned a coroutine,
            # drive it to completion.  Behavior preserved from the
            # original create_recipe.py:365-367.
            if asyncio.iscoroutine(result):
                tool_logger.info(
                    f"Detected coroutine return from {func.__name__}, "
                    f"running it to completion")
                result = asyncio.get_event_loop().run_until_complete(
                    result)
            if not isinstance(result, str):
                tool_logger.warning(
                    f"Tool function {func.__name__} returned "
                    f"non-string type: {type(result)}")
                result = str(result)
            tool_logger.info(f"TOOL EXECUTION SUCCESS: {func.__name__}")
            tool_logger.info(
                f"Result: {result[:100]}..."
                if len(result) > 100 else f"Result: {result}"
            )
            return result
        except Exception as e:
            tool_logger.error(
                f"TOOL EXECUTION ERROR: {func.__name__} - {e}")
            tool_logger.exception("Exception details:")
            envelope = _error_envelope(func.__name__, e)
            tool_logger.info(f"Returning error response: {envelope}")
            return envelope

    return sync_wrapper


__all__ = ['log_tool_execution', 'tool_logger']
