"""MCP tool bodies have ONE source: integrations.mcp._tool_impls (#98c).

The stdio server (mcp_server.py, FastMCP) and the HTTP bridge (mcp_http_bridge.py)
used to carry byte-copied tool bodies that had already drifted. They now both
register the SAME impl functions, keeping only their own registration mechanism.

Behavioural guards (drive the REAL modules):
  * single-source identity: the HTTP bridge registers the exact impls.* objects;
  * provenance preserved: `remember` tags source_agent per transport
    ('claude_orchestrator' stdio vs 'mcp_bridge' HTTP) while sharing the body —
    verified through the REAL FastMCP dispatch (mcp.call_tool) and the REAL HTTP
    wrapper, with the memory graph mocked;
  * richer shape kept: list_agents emits the canonical fields (model_type, etc).

If either transport re-inlines a body, the fn-identity check fails; if a wrapper
stops threading source_agent, the provenance check fails. No grep tests.
"""
from __future__ import annotations

import os
import sys
import json
import asyncio
from unittest.mock import patch, MagicMock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.mcp import _tool_impls as impls


def test_http_bridge_registers_shared_impls():
    from integrations.mcp import mcp_http_bridge as b
    b._load_tools()
    by_name = {t['name']: t for t in b._local_tools}
    # Read-only shared tools must BE the canonical impl objects (no copy).
    assert by_name['list_agents']['fn'] is impls.list_agents
    assert by_name['list_goals']['fn'] is impls.list_goals
    assert by_name['agent_status']['fn'] is impls.agent_status
    assert by_name['list_recipes']['fn'] is impls.list_recipes
    assert by_name['system_health']['fn'] is impls.system_health
    assert by_name['social_query']['fn'] is impls.social_query
    assert by_name['recall']['fn'] is impls.recall


def test_remember_provenance_http_is_mcp_bridge():
    from integrations.mcp import mcp_http_bridge as b
    captured = {}
    fake_mg = MagicMock()
    fake_mg.register.side_effect = lambda content, metadata: captured.update(metadata) or 'mem1'
    with patch.object(impls, '_get_memory_graph', return_value=fake_mg):
        out = json.loads(b._tool_remember('hello'))
    assert out == {'stored': True, 'memory_id': 'mem1'}
    assert captured['source_agent'] == 'mcp_bridge'
    assert captured['memory_type'] == 'decision'


def test_remember_provenance_stdio_is_claude_orchestrator():
    """Exercise the REAL FastMCP dispatch path for the stdio 'remember' tool."""
    from integrations.mcp import mcp_server as s
    captured = {}
    fake_mg = MagicMock()
    fake_mg.register.side_effect = lambda content, metadata: captured.update(metadata) or 'mem2'
    with patch.object(impls, '_get_memory_graph', return_value=fake_mg):
        asyncio.get_event_loop().run_until_complete(
            s.mcp.call_tool('remember', {'content': 'hi', 'memory_type': 'fact'}))
    assert captured['source_agent'] == 'claude_orchestrator'
    assert captured['memory_type'] == 'fact'


def test_list_agents_shape_is_canonical():
    """The shared impl emits the richer fields (model_type on every agent)."""
    fake_agent = MagicMock()
    fake_agent.agent_id = 'a1'
    fake_agent.name = 'Coder'
    fake_agent.category = MagicMock()
    fake_agent.category.name = 'software_dev'
    fake_agent.description = 'writes code'
    fake_agent.model_type = 'llm'
    fake_reg = MagicMock()
    fake_reg.agents = {'a1': fake_agent}
    with patch.object(impls, '_get_registry', return_value=fake_reg):
        out = json.loads(impls.list_agents())
    assert set(out.keys()) == {'expert_agents', 'dynamic_agents', 'agents', 'dynamic'}
    assert out['agents'][0]['model_type'] == 'llm'
    assert out['agents'][0]['agent_id'] == 'a1'


def test_unknown_category_lists_valid_options():
    """Canonical list_agents includes the valid-category hint on a bad category
    (a detail the drifted HTTP copy had dropped)."""
    fake_reg = MagicMock()
    fake_reg.agents = {}
    with patch.object(impls, '_get_registry', return_value=fake_reg):
        out = json.loads(impls.list_agents(category='nonsense'))
    assert 'error' in out
    assert 'Valid:' in out['error']
