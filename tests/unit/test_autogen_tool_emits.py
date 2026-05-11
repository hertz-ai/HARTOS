"""Drift-guard for #509 — autogen-invoked tools emit UI status events.

When `autogen.register_function` registers a tool function, autogen's
executor invokes the raw Python function — it does NOT traverse the
LangChain `_with_tool_logging` wrapper.  Before #509 this left a hole:
helper→assistant autogen handoffs would call tools like
`create_prospect` or `view_journey_pipeline` and the chat UI got NO
per-tool status; the spinner stayed on the generic "Thinking…" verb.

The fix routes every autogen registration through
`core.labeled_autogen_function.register_labeled_function`, which:

  1. Requires a `ui_label` kwarg (TypeError if missing — Python kwarg
     enforcement replaces an AST drift-guard).
  2. Registers the label into the canonical TOOL_LABELS dict via
     `register_tool_label`.
  3. Wraps the function with the CANONICAL
     `core.tool_logging.log_tool_execution` decorator (CLAUDE.md
     Gate 4 — single chokepoint, no parallel wrappers).  That
     decorator emits `publish_chat_stage('tool_call', text=…)` BEFORE
     invocation, logs entry/args/result/error, coerces non-string
     returns to str, returns a JSON error envelope on exception.

Tests below pin those contracts so the next refactor can't silently
strip them.
"""
from __future__ import annotations

import ast
import io
import json
import logging
import os
from unittest.mock import MagicMock, patch

import pytest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


# ─── T1: Factory enforces ui_label kwarg ────────────────────────────

def test_missing_ui_label_raises_type_error():
    """Compile-time check: Python's required-kwarg semantics catch any
    call site that forgets to supply ui_label."""
    from core.labeled_autogen_function import register_labeled_function
    with pytest.raises(TypeError):
        register_labeled_function(
            lambda: None, caller=MagicMock(), executor=MagicMock()
        )


@pytest.mark.parametrize('bad', ['', '   ', '\t', 123, None])
def test_empty_or_non_string_ui_label_raises_value_error(bad):
    from core.labeled_autogen_function import register_labeled_function
    with pytest.raises(ValueError):
        register_labeled_function(
            lambda: None,
            caller=MagicMock(),
            executor=MagicMock(),
            ui_label=bad,
        )


# ─── T2: Factory registers label into TOOL_LABELS ───────────────────

def test_factory_registers_label_into_tool_labels():
    """Every labeled construction populates TOOL_LABELS so the
    UI-status pipeline picks the spinner text up at runtime."""
    from core.labeled_autogen_function import register_labeled_function
    from core.constants import TOOL_LABELS

    def _stub_fn(): return 'ok'
    _stub_fn.__name__ = 'unique_autogen_smoke_t2'

    with patch(
        'core.labeled_autogen_function._autogen_register_function'
    ) as mocked:
        mocked.return_value = 'sentinel'
        register_labeled_function(
            _stub_fn,
            caller=MagicMock(),
            executor=MagicMock(),
            description='smoke',
            ui_label='Smoke testing autogen tool…',
        )
        assert mocked.called

    assert TOOL_LABELS.get('unique_autogen_smoke_t2') == \
        'Smoke testing autogen tool…'


# ─── T3: Wrapped function emits publish_chat_stage('tool_call', …) ──
#
# The wrapper now routes through core.tool_logging.log_tool_execution,
# which calls core.tool_logging._emit_tool_call_stage(func.__name__).
# That helper imports publish_chat_stage from core.peer_link.crossbar_publish
# lazily — so patches must target the source module.

def test_wrapped_function_emits_tool_call_when_user_context_set():
    """When the chat hot path has populated thread_local_data with a
    user_id, invoking the wrapped function MUST call
    publish_chat_stage('tool_call', user_id=…, request_id=…, text=…)."""
    from core.labeled_autogen_function import register_labeled_function
    from threadlocal import thread_local_data

    def _stub_fn(arg):
        return f'echo:{arg}'
    _stub_fn.__name__ = 'unique_autogen_smoke_t3'

    with patch(
        'core.labeled_autogen_function._autogen_register_function'
    ) as mocked:
        register_labeled_function(
            _stub_fn,
            caller=MagicMock(),
            executor=MagicMock(),
            description='smoke',
            ui_label='Smoke T3…',
        )
        wrapped = mocked.call_args[0][0]

    thread_local_data.set_user_id('u-t3')
    thread_local_data.set_request_id('r-t3')

    with patch(
        'core.peer_link.crossbar_publish.publish_chat_stage'
    ) as pub:
        result = wrapped('hello')
        assert result == 'echo:hello'
        assert pub.called, "publish_chat_stage was not called"
        kwargs = pub.call_args.kwargs
        assert kwargs['user_id'] == 'u-t3'
        assert kwargs['request_id'] == 'r-t3'
        assert kwargs['text'] == 'Smoke T3…'
        assert pub.call_args.args[0] == 'tool_call'


def test_wrapped_function_skips_emit_when_no_user_context(caplog):
    """When thread_local_data has no user_id (daemon-loop / test
    context), the wrapped function logs a debug line + invokes the
    real func — no exception, no emit."""
    from core.labeled_autogen_function import register_labeled_function
    from threadlocal import thread_local_data

    def _stub_fn():
        return 'noctx'
    _stub_fn.__name__ = 'unique_autogen_smoke_t3b'

    with patch(
        'core.labeled_autogen_function._autogen_register_function'
    ) as mocked:
        register_labeled_function(
            _stub_fn,
            caller=MagicMock(),
            executor=MagicMock(),
            description='smoke',
            ui_label='Smoke T3b…',
        )
        wrapped = mocked.call_args[0][0]

    thread_local_data.set_user_id(None)

    with patch(
        'core.peer_link.crossbar_publish.publish_chat_stage'
    ) as pub:
        with caplog.at_level(logging.DEBUG, logger='core.tool_logging'):
            result = wrapped()
            assert result == 'noctx'
            assert not pub.called, \
                "publish_chat_stage must not fire without user context"

    assert any(
        'no chat context' in r.message for r in caplog.records
    ), "expected debug log when context absent"


# ─── T4: Tool exceptions are LOGGED and returned as JSON envelope ───
#
# Contract change in unification: log_tool_execution returns a JSON
# error envelope STRING instead of re-raising — autogen's executor
# treats the string as a normal tool output that the LLM can read.

def test_tool_exception_is_logged_and_returns_json_envelope(caplog):
    """Tool exceptions must (a) appear in logs at ERROR level
    (b) be returned to autogen as a 'Tool execution failed: {json}'
    string envelope (NOT raised), per the canonical log_tool_execution
    contract.  Re-raising would have broken autogen's chat loop."""
    from core.labeled_autogen_function import register_labeled_function

    def _bad_fn():
        raise RuntimeError('intentional T4 failure')
    _bad_fn.__name__ = 'unique_autogen_smoke_t4'

    with patch(
        'core.labeled_autogen_function._autogen_register_function'
    ) as mocked:
        register_labeled_function(
            _bad_fn,
            caller=MagicMock(),
            executor=MagicMock(),
            description='smoke',
            ui_label='Smoke T4…',
        )
        wrapped = mocked.call_args[0][0]

    with caplog.at_level(logging.ERROR, logger='agent_logger'):
        result = wrapped()

    assert isinstance(result, str)
    assert result.startswith('Tool execution failed: ')
    payload = json.loads(result[len('Tool execution failed: '):])
    assert payload['status'] == 'error'
    assert payload['tool_function'] == 'unique_autogen_smoke_t4'
    assert payload['error_type'] == 'RuntimeError'
    assert 'intentional T4 failure' in payload['error_message']

    assert any('TOOL EXECUTION ERROR' in r.message
               for r in caplog.records), \
        "ERROR log line for tool exception was not emitted"


def test_emit_failure_does_not_block_tool_and_is_logged(caplog):
    """If publish_chat_stage itself raises, the tool MUST still run AND
    the failure MUST appear in logs at warning level."""
    from core.labeled_autogen_function import register_labeled_function
    from threadlocal import thread_local_data

    def _good_fn():
        return 'still_runs'
    _good_fn.__name__ = 'unique_autogen_smoke_t4b'

    with patch(
        'core.labeled_autogen_function._autogen_register_function'
    ) as mocked:
        register_labeled_function(
            _good_fn,
            caller=MagicMock(),
            executor=MagicMock(),
            description='smoke',
            ui_label='Smoke T4b…',
        )
        wrapped = mocked.call_args[0][0]

    thread_local_data.set_user_id('u-t4b')
    thread_local_data.set_request_id('r-t4b')

    with patch(
        'core.peer_link.crossbar_publish.publish_chat_stage',
        side_effect=RuntimeError('emit boom'),
    ):
        with caplog.at_level(logging.WARNING,
                             logger='core.tool_logging'):
            result = wrapped()
            assert result == 'still_runs'

    assert any('UI emit for' in r.message for r in caplog.records), \
        "Emit-failure warning log was not emitted"


# ─── T5: async tool wrapper preserves coroutine-ness ────────────────

def test_async_tool_wrapper_is_coroutine_function():
    """helper.py's patched ConversableAgent.execute_function checks
    `inspect.iscoroutinefunction(func)` and either awaits the call or
    runs it synchronously.  The canonical log_tool_execution decorator
    branches at the sync/async level so async tools produce an async
    wrapper — drift would cause autogen to call wrapper() without
    await and get a coroutine object instead of the result."""
    import asyncio
    import inspect as ins
    from core.labeled_autogen_function import register_labeled_function

    async def async_tool(x):
        return f'got:{x}'

    def sync_tool(x):
        return f'sync:{x}'

    with patch(
        'core.labeled_autogen_function._autogen_register_function'
    ) as mocked:
        register_labeled_function(
            async_tool, caller=MagicMock(), executor=MagicMock(),
            description='d', ui_label='A…',
        )
        wrapped_async = mocked.call_args[0][0]

    assert ins.iscoroutinefunction(wrapped_async), (
        "async tool's wrapper must itself be a coroutine function so "
        "helper.py:2592 awaits it"
    )
    assert asyncio.run(wrapped_async('x')) == 'got:x'

    with patch(
        'core.labeled_autogen_function._autogen_register_function'
    ) as mocked:
        register_labeled_function(
            sync_tool, caller=MagicMock(), executor=MagicMock(),
            description='d', ui_label='S…',
        )
        wrapped_sync = mocked.call_args[0][0]

    assert not ins.iscoroutinefunction(wrapped_sync), (
        "sync tool's wrapper must be a plain function (not coroutine)"
    )
    assert wrapped_sync('x') == 'sync:x'


# ─── T6: existing call sites use the labeled factory ────────────────

def _imports_register_labeled_function(path: str) -> bool:
    src = io.open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or '').endswith('labeled_autogen_function'):
                for alias in node.names:
                    if alias.name == 'register_labeled_function':
                        return True
    return False


def _calls_raw_autogen_register_function(path: str) -> bool:
    src = io.open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == 'register_function':
            return True
        if isinstance(fn, ast.Attribute) and fn.attr == 'register_function':
            return True
    return False


@pytest.mark.parametrize('relpath', [
    'integrations/agent_engine/journey_engine.py',
    'integrations/agent_engine/outreach_crm_tools.py',
    'integrations/service_tools/system_introspect_tool.py',
])
def test_call_site_uses_labeled_factory(relpath):
    """Drift-guard: every known autogen-tool registration call site must
    import the labeled factory (proves the migration happened)."""
    abs_path = os.path.join(REPO_ROOT, relpath)
    assert _imports_register_labeled_function(abs_path), (
        f"{relpath} does not import register_labeled_function — "
        f"new autogen tools at this site will not emit UI status events"
    )


@pytest.mark.parametrize('relpath', [
    'integrations/agent_engine/journey_engine.py',
    'integrations/agent_engine/outreach_crm_tools.py',
    'integrations/service_tools/system_introspect_tool.py',
])
def test_call_site_no_raw_register_function_call(relpath):
    """Drift-guard: ensure no bare `register_function(...)` survived the
    migration."""
    abs_path = os.path.join(REPO_ROOT, relpath)
    assert not _calls_raw_autogen_register_function(abs_path), (
        f"{relpath} still calls register_function(...) directly — "
        f"route it through register_labeled_function so the tool emits UI status"
    )


# ─── T7: log_tool_execution lives in core.tool_logging (single home) ─

def test_log_tool_execution_canonical_home():
    """The canonical decorator now lives in core.tool_logging.  create_recipe
    re-imports it for the 40+ legacy decorator sites; reuse_recipe imports
    it at module scope; core.labeled_autogen_function uses it as the
    autogen registration chokepoint.  Drift-guard: ensure all four files
    resolve the same callable."""
    from core.tool_logging import log_tool_execution as canonical
    from create_recipe import log_tool_execution as via_create
    from reuse_recipe import log_tool_execution as via_reuse

    assert via_create is canonical, (
        "create_recipe.log_tool_execution must re-export the canonical "
        "core.tool_logging.log_tool_execution"
    )
    assert via_reuse is canonical, (
        "reuse_recipe.log_tool_execution must be the canonical "
        "core.tool_logging.log_tool_execution"
    )


# ─── T8: reuse_recipe inner tools wear @log_tool_execution ──────────

def test_reuse_recipe_inner_tools_have_log_tool_execution():
    """Every `@assistant.register_for_execution()`-decorated function
    in reuse_recipe.py must ALSO have `@log_tool_execution` in its
    decorator stack so autogen-side tools get the same logging + UI
    emit contract as the LangChain side."""
    path = os.path.join(REPO_ROOT, 'reuse_recipe.py')
    src = io.open(path, encoding='utf-8').read()
    tree = ast.parse(src)

    def deco_names(node):
        names = []
        for d in node.decorator_list:
            if isinstance(d, ast.Call):
                f = d.func
                if isinstance(f, ast.Attribute):
                    names.append(f.attr)
                elif isinstance(f, ast.Name):
                    names.append(f.id)
            elif isinstance(d, ast.Name):
                names.append(d.id)
            elif isinstance(d, ast.Attribute):
                names.append(d.attr)
        return names

    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decos = deco_names(node)
        wants = any('register_for_execution' in n or 'register_for_llm' in n
                    for n in decos)
        if wants and 'log_tool_execution' not in decos:
            missing.append(f'{node.name}:{node.lineno}')

    assert not missing, (
        f"reuse_recipe.py inner tools missing @log_tool_execution: "
        f"{missing} — add the decorator so the tool emits UI status + "
        f"gets structured logging"
    )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
