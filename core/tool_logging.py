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
`hart_intelligence_entry.py` (#114) now DELEGATES here via the
`name=` parameter (a LangChain `Tool` carries a `.name` distinct from
`func.__name__`) and `plain_errors=True` (its agent_executor wants a
plain string, not the JSON envelope) — one tool-log impl, no parallel
path.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
import inspect
import json
import logging
from functools import partial, wraps
from typing import get_type_hints

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
        from core.constants import TOOL_LABELS, generic_tool_label
        from core.peer_link.crossbar_publish import publish_chat_stage
        from threadlocal import thread_local_data

        user_id = thread_local_data.get_user_id() or ''
        request_id = thread_local_data.get_request_id() or ''
        if user_id:
            publish_chat_stage(
                'tool_call',
                user_id=str(user_id),
                request_id=str(request_id),
                text=TOOL_LABELS.get(tool_name, generic_tool_label(tool_name)),
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


def log_tool_execution(func=None, *, name=None, plain_errors=False):
    """Decorator wrapping a tool function with logging + UI emit.

    Handles both sync and async callables.  Apply with `@log_tool_execution`
    or programmatically as `wrapped = log_tool_execution(func)`.

    Args:
        name: override the logged + UI-emitted tool name.  A LangChain ``Tool``
            carries a ``.name`` distinct from ``func.__name__`` (often a generic
            closure name), so the LangChain wrapper passes it for the correct UI
            label.  Defaults to ``func.__name__``.
        plain_errors: on failure return a plain
            ``"Tool 'X' encountered an error: …"`` string instead of the JSON
            error envelope.  LangChain's agent_executor wants a plain string;
            autogen wants the envelope.  Default False (the envelope).

    See module docstring for the full behavior contract.
    """
    if func is None:
        # Called as @log_tool_execution(name=..., plain_errors=...) — bind the
        # options and return the real decorator once the function arrives.
        return partial(log_tool_execution, name=name, plain_errors=plain_errors)

    tool_name = name or func.__name__

    # PEP 563 repair, done here because this decorator is the INNERMOST one on
    # every registered tool -- autogen's register_for_llm sits above it and so
    # only ever sees what we hand back.
    #
    # reuse_recipe.py carries `from __future__ import annotations` (load-bearing
    # there: it stops the module-level
    # `user_agents: Dict[str, Tuple[autogen.AssistantAgent, ...]]` at :269-270
    # and the defs at :895/:3187 from evaluating `autogen.*` at import time and
    # dragging the lazy autogen proxy -- ~7.6s + torch -- onto the boot path).
    # But PEP 563 stringizes EVERY annotation in that module, including the
    # `Annotated[str, "..."]` params of its 48 tools.  @wraps copies those
    # strings onto the wrapper, autogen hands them to pydantic, and pydantic
    # raises:
    #   PydanticUserError: TypeAdapter[ForwardRef("Annotated[str, 'Target
    #   persona/role name to deliver the message to']")] is not fully defined
    # Measured live 2026-08-29 09:31:18: that killed create_agents_for_user(),
    # i.e. every agent REUSE and the whole multi-persona path.  create_recipe.py
    # has no __future__ import, which is exactly why CREATE worked and REUSE did
    # not.
    #
    # include_extras=True is required: without it Annotated[str, "desc"]
    # collapses to plain str and every per-argument description autogen shows
    # the model is silently lost.
    try:
        _resolved_hints = get_type_hints(func, include_extras=True)
    except Exception as _e:  # unresolvable forward ref -> leave the original
        tool_logger.debug(
            f"annotation resolve skipped for {tool_name}: {_e}")
        _resolved_hints = None

    def _on_error(e, _t0=None):
        _took = '' if _t0 is None else f" latency_ms={round((time.perf_counter() - _t0) * 1000, 1)}"
        tool_logger.error(f"TOOL EXECUTION ERROR: {tool_name} - {e}{_took}")
        tool_logger.exception("Exception details:")
        if plain_errors:
            return f"Tool '{tool_name}' encountered an error: {str(e)[:200]}"
        envelope = _error_envelope(tool_name, e)
        tool_logger.info(f"Returning error response: {envelope}")
        return envelope

    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            tool_logger.info(f"TOOL EXECUTION START: {tool_name}")
            tool_logger.info(
                f"Arguments: {args}, Keyword Arguments: {kwargs}")
            _emit_tool_call_stage(tool_name)
            _t0 = time.perf_counter()
            try:
                result = await func(*args, **kwargs)
                if not isinstance(result, str):
                    tool_logger.warning(
                        f"Tool function {tool_name} returned "
                        f"non-string type: {type(result)}")
                    result = str(result)
                tool_logger.info(
                    f"TOOL EXECUTION SUCCESS: {tool_name} "
                    f"latency_ms={round((time.perf_counter() - _t0) * 1000, 1)}")
                tool_logger.info(
                    f"Result: {result[:100]}..."
                    if len(result) > 100 else f"Result: {result}"
                )
                return result
            except Exception as e:
                return _on_error(e, _t0)

        if _resolved_hints:
            async_wrapper.__annotations__ = _resolved_hints
        return async_wrapper

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        tool_logger.info(f"TOOL EXECUTION START: {tool_name}")
        tool_logger.info(
            f"Arguments: {args}, Keyword Arguments: {kwargs}")
        _emit_tool_call_stage(tool_name)
        _t0 = time.perf_counter()
        try:
            result = func(*args, **kwargs)
            # If the sync function accidentally returned a coroutine,
            # drive it to completion.  Behavior preserved from the
            # original create_recipe.py:365-367.
            if asyncio.iscoroutine(result):
                tool_logger.info(
                    f"Detected coroutine return from {tool_name}, "
                    f"running it to completion")
                result = asyncio.get_event_loop().run_until_complete(
                    result)
            if not isinstance(result, str):
                tool_logger.warning(
                    f"Tool function {tool_name} returned "
                    f"non-string type: {type(result)}")
                result = str(result)
            tool_logger.info(
                f"TOOL EXECUTION SUCCESS: {tool_name} "
                f"latency_ms={round((time.perf_counter() - _t0) * 1000, 1)}")
            tool_logger.info(
                f"Result: {result[:100]}..."
                if len(result) > 100 else f"Result: {result}"
            )
            return result
        except Exception as e:
            return _on_error(e, _t0)

    if _resolved_hints:
        sync_wrapper.__annotations__ = _resolved_hints
    return sync_wrapper


@contextmanager
def timed_stage(name: str, logger=None, warn_over_ms: float | None = None,
                **fields):
    """Time an INLINE stage that is not a decoratable function.

    Same concern as ``log_tool_execution``'s clock, same ``latency_ms=``
    key, deliberately in the same module so there is ONE format for "how
    long did this step take" across the codebase.  Use the decorator when
    the step IS a function; use this when it is a few statements inside a
    larger one (the TTS ``_bg`` thread's clean / normalize / synthesize
    stages are the motivating case — see below).

    WHY THIS EXISTS (task #652 follow-up, 2026-08-12): a TTS background
    thread took 33.4s and 46.0s between "thread started" and "Synthesized
    audio", while Piper standalone synthesizes the SAME text in 0.38s
    (RTF 0.045-0.089, measured).  The span contained exactly three steps
    and the only expensive candidate — an LLM-backed text normalization —
    logged NOTHING on success and only DEBUG on failure.  So ~45s of a
    user-visible delay could not be attributed from the logs at all.
    ``log_tool_execution`` already existed as the AOP seam but had no
    clock, so even decorated code could not be attributed either.

    Emits one line per stage.  ``warn_over_ms`` promotes the line to
    WARNING past a threshold, so a stage that becomes slow announces
    itself instead of waiting to be noticed by a human reading a log.

    Never swallows: the exception propagates after being timed and logged,
    because a stage that failed fast and a stage that hung are different
    problems and must not print the same line.
    """
    _log = logger if logger is not None else tool_logger
    _extra = (' ' + ' '.join(f'{k}={v}' for k, v in fields.items())) if fields else ''
    _t0 = time.perf_counter()
    try:
        yield
    except Exception as e:
        _log.warning(
            f"STAGE FAILED: {name} "
            f"latency_ms={round((time.perf_counter() - _t0) * 1000, 1)}"
            f"{_extra} error={type(e).__name__}: {str(e)[:160]}")
        raise
    else:
        _ms = round((time.perf_counter() - _t0) * 1000, 1)
        _line = f"STAGE: {name} latency_ms={_ms}{_extra}"
        if warn_over_ms is not None and _ms > warn_over_ms:
            _log.warning(f"{_line} SLOW (budget_ms={warn_over_ms})")
        else:
            _log.info(_line)


__all__ = ['log_tool_execution', 'timed_stage', 'tool_logger']
