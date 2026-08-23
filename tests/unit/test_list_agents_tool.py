"""P3 (#688) — the chat agent must be able to enumerate agents.

Live 2026-08-23 15:02 (installed build): asked "list the agents you have
available", the model answered "I am Qwen3.5... I do not have separate
agents" — a fabricated denial, because the enumeration implementation
(integrations/mcp/_tool_impls.list_agents — 4 expert + 2,633 dynamic
recipes) is wired only to the MCP bridge, and NO agent-listing tool is
registered in the chat-facing registry.

    python -m pytest tests/unit/test_list_agents_tool.py --noconftest -q
"""
import json
import re
from pathlib import Path

_HIE_SRC = (Path(__file__).resolve().parents[2] /
            'hart_intelligence_entry.py').read_text(encoding='utf-8')


def test_impl_enumerates_real_agents():
    """The canonical impl works standalone (shared with the MCP bridge)."""
    from integrations.mcp._tool_impls import list_agents
    data = json.loads(list_agents())
    assert data.get('expert_agents', 0) >= 1
    assert isinstance(data.get('agents'), list) and data['agents']


def test_langchain_registry_carries_list_agents_tool():
    """The fix: a List_Agents labeled_tool wired to the SAME canonical
    impl (no parallel enumeration) must exist in the chat registry."""
    m = re.search(r'labeled_tool\(\s*name="List_Agents".*?\)', _HIE_SRC, re.DOTALL)
    assert m, (
        "no List_Agents tool registered — the chat agent fabricates "
        "'I have no separate agents' while 2,637 sit in the registry")


def test_wrapper_delegates_to_canonical_impl():
    """The wrapper must import the mcp impl, not re-implement it."""
    m = re.search(r'def _parse_list_agents.*?(?=\ndef |\nclass )', _HIE_SRC, re.DOTALL)
    assert m, "_parse_list_agents wrapper missing"
    assert '_tool_impls import list_agents' in m.group(0), (
        "wrapper must delegate to integrations.mcp._tool_impls.list_agents "
        "— a second enumeration would be a parallel path")
