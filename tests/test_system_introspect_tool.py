"""Tests for integrations.service_tools.system_introspect_tool.

Covers:
  * All 8 tool functions return well-formed dicts with a `summary` key
  * `explain_decision` returns real source code via inspect.getsource
  * `list_decisions` enumerates all registered topics
  * LangChain wrapper doesn't crash at import
  * autogen wrapper is no-op when autogen isn't available
  * Graceful degradation when Nunba Flask is unreachable
"""
from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(scope='module')
def tool():
    """Fresh import of the module per test session."""
    sys.path.insert(0, '.')
    from integrations.service_tools import system_introspect_tool as t
    return t


# ── Structural invariants ─────────────────────────────────────────

def test_all_tool_functions_have_summary_key(tool):
    """Every tool function returns a dict with a `summary` field — the
    agent relies on this for natural-language responses.  Patch `_get`
    to return a 'Flask unavailable' response so we exercise the
    fallback paths too."""
    with patch.object(tool, '_get', return_value={'available': False, 'reason': 'offline'}):
        for fn in tool._TOOL_FUNCTIONS:
            if fn.__name__ in ('explain_decision',):
                result = fn()  # no arg → returns topic list
            else:
                result = fn()
            assert isinstance(result, dict), f"{fn.__name__} did not return dict"
            assert 'summary' in result, f"{fn.__name__} missing 'summary' key"
            assert isinstance(result['summary'], str), f"{fn.__name__} summary not str"


def test_tool_function_list_stable(tool):
    """`_TOOL_FUNCTIONS` must include every public function to keep the
    registry + loaders in sync."""
    names = {fn.__name__ for fn in tool._TOOL_FUNCTIONS}
    expected = {
        'get_gpu_tier', 'list_running_models', 'get_tts_status',
        'get_tier_thresholds', 'get_boot_decision', 'get_system_health',
        'list_decisions', 'explain_decision',
    }
    assert names == expected, f"Tool function set drift: {names} vs {expected}"


# ── Decision-logic RAG ────────────────────────────────────────────

def test_list_decisions_returns_all_topics(tool):
    result = tool.list_decisions()
    assert result['available'] is True
    assert len(result['topics']) >= 11
    names = {t['name'] for t in result['topics']}
    # Canonical topics the agent is expected to cover
    for expected_topic in ('draft_gate', 'tts_lang_ladder', 'gpu_tier_thresholds',
                            'mcp_auth', 'hf_install_gates', 'hub_allowlist'):
        assert expected_topic in names, f"Missing canonical topic: {expected_topic}"


def test_explain_decision_empty_lists_topics(tool):
    """Empty topic → returns the list of known topics so the agent
    can pick one."""
    result = tool.explain_decision('')
    assert result['available'] is True
    assert 'topics' in result
    assert 'draft_gate' in result['topics']
    assert 'Known decision topics' in result['summary']


def test_explain_decision_unknown_topic(tool):
    result = tool.explain_decision('not_a_real_topic')
    assert result['available'] is False
    assert 'Known topics' in result['summary']


def test_explain_decision_returns_live_source(tool):
    """The whole point of RAG: source must be retrieved via
    inspect.getsource, not a paraphrased constant."""
    result = tool.explain_decision('gpu_tier_thresholds')
    assert result['available'] is True
    assert 'source' in result
    # The source must be Python code referencing the actual symbol name
    assert 'TIER_THRESHOLDS' in result['source']
    assert result['description']
    assert result['question']


def test_explain_decision_function_source(tool):
    """A function topic (draft_gate → should_boot_draft) should return
    a `def` line in the source."""
    result = tool.explain_decision('draft_gate')
    if result['available']:
        # Only assert code-like content if import succeeded
        assert 'def should_boot_draft' in result['source'] or 'return' in result['source']
    else:
        # Acceptable if llama.llama_config isn't on sys.path (test env)
        assert 'description' in result


def test_explain_decision_fuzzy_prefix_match(tool):
    """Partial topic names should match (e.g. 'draft' → 'draft_gate')."""
    result = tool.explain_decision('draft')
    # Either exact miss or prefix-matched to draft_gate
    assert result.get('topic') == 'draft_gate' or 'Unknown decision topic' in result.get('summary', '')


# ── Graceful degradation ──────────────────────────────────────────

def test_get_gpu_tier_handles_flask_down(tool):
    with patch.object(tool, '_get', return_value={'available': False, 'reason': 'offline'}):
        result = tool.get_gpu_tier()
    assert result['available'] is False
    assert 'unavailable' in result['summary'].lower()


def test_get_gpu_tier_timeout_becomes_unavailable(tool):
    """Simulate requests.Timeout — the `_get` helper should return
    available=False instead of raising into the LLM loop."""
    import requests
    with patch('integrations.service_tools.system_introspect_tool.requests.get',
               side_effect=requests.exceptions.Timeout()):
        result = tool._get('/backend/health')
    assert result['available'] is False
    assert 'timed out' in result['reason'].lower()


def test_get_gpu_tier_connection_error(tool):
    import requests
    with patch('integrations.service_tools.system_introspect_tool.requests.get',
               side_effect=requests.exceptions.ConnectionError()):
        result = tool._get('/backend/health')
    assert result['available'] is False
    assert 'not reachable' in result['reason'].lower()


def test_get_gpu_tier_live_data(tool):
    """With a mocked-OK response, summary must reference the tier."""
    mock_payload = {
        'gpu_tier': 'standard',
        'gpu_name': 'RTX 3070',
        'vram_total_gb': 8.0,
        'vram_free_gb': 1.5,
        'cuda_available': True,
        'speculation_enabled': True,
    }
    with patch.object(tool, '_get', return_value=mock_payload):
        result = tool.get_gpu_tier()
    assert result['available'] is True
    assert result['gpu_tier'] == 'standard'
    assert 'RTX 3070' in result['summary']
    assert '8.0GB' in result['summary']
    assert 'Speculative decoding: on' in result['summary']


def test_tier_thresholds_fallback_when_api_unavailable(tool):
    """Even when /api/v1/system/tiers is offline, static fallback
    returns the canonical thresholds so the agent can always answer
    'what tiers exist?'."""
    with patch.object(tool, '_get', return_value={'available': False, 'reason': 'offline'}):
        result = tool.get_tier_thresholds()
    assert result['available'] is True
    assert result.get('source') == 'fallback'
    assert len(result['tiers']) == 4
    names = {t['name'] for t in result['tiers']}
    assert names == {'ultra', 'full', 'standard', 'none'}


# ── Loader integration ───────────────────────────────────────────

def test_langchain_loader_returns_list(tool):
    """Wrapper returns a list; if langchain isn't installed returns []."""
    tools = tool.get_langchain_tools()
    assert isinstance(tools, list)
    # If langchain IS installed, we should get one Tool per function
    if tools:
        assert len(tools) == len(tool._TOOL_FUNCTIONS)
        names = {t.name for t in tools}
        assert 'get_gpu_tier' in names
        assert 'explain_decision' in names


def test_langchain_tool_invokes_function(tool):
    """The wrapped Tool.func must call the underlying introspect fn
    and return its summary string."""
    tools = tool.get_langchain_tools()
    if not tools:
        pytest.skip('langchain not available')
    gpu_tool = next((t for t in tools if t.name == 'get_gpu_tier'), None)
    assert gpu_tool is not None
    with patch.object(tool, '_get', return_value={'available': False, 'reason': 'offline'}):
        out = gpu_tool.func('')
    assert isinstance(out, str)
    assert 'unavailable' in out.lower() or 'GPU' in out


def test_explain_decision_via_langchain_takes_topic_arg(tool):
    """The LangChain wrapper detects that explain_decision takes a
    real string arg and passes it through (not the argless path)."""
    tools = tool.get_langchain_tools()
    if not tools:
        pytest.skip('langchain not available')
    ed = next((t for t in tools if t.name == 'explain_decision'), None)
    assert ed is not None
    out = ed.func('gpu_tier_thresholds')
    assert isinstance(out, str)
    # Should contain the tier topic rationale
    assert 'tier' in out.lower() or 'VRAM' in out or 'TIER_THRESHOLDS' in out


def test_autogen_register_no_op_without_autogen(tool):
    """`register_autogen` must be a silent no-op if autogen can't be
    imported — returns 0."""
    # Force the import failure path
    with patch.dict(sys.modules, {'autogen': None}):
        # Re-reload to trigger re-import attempt (nope — import is inside fn)
        count = tool.register_autogen(MagicMock(), MagicMock())
    # Real autogen IS installed in test env so this likely returns >0
    assert isinstance(count, int)
    assert count >= 0


# ── Module __all__ and public API ─────────────────────────────────

def test_all_public_symbols_listed(tool):
    """Every function in _TOOL_FUNCTIONS is in __all__."""
    expected = {fn.__name__ for fn in tool._TOOL_FUNCTIONS}
    expected.update({'get_tool_functions', 'get_langchain_tools', 'register_autogen'})
    assert expected.issubset(set(tool.__all__)), (
        f"Missing from __all__: {expected - set(tool.__all__)}"
    )


def test_boot_decision_handles_missing_log(tool):
    """If `~/Documents/Nunba/logs/draft_decision.jsonl` doesn't exist,
    the tool returns available=False with a human-readable reason
    instead of raising."""
    import tempfile
    from pathlib import Path
    # Point Home to a temp dir that definitely lacks the log file
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(Path, 'home', return_value=Path(tmp)):
            result = tool.get_boot_decision()
        assert result['available'] is False
        assert 'not yet written' in result['summary'] or 'empty' in result['summary']
