"""A node that cannot run a coding tool is not offered it.

Live on central, 2026-11-21 02:37Z, goal 4f294fa3 (the rice buyer sales
agent): action 2 was planned as "execute_coding_task: read
trade/leads/rice_importers.csv and trade/leads/hartos_import_rice_importers.json
and report row counts and emails".  The tool answered "No coding tools
installed. Install one: kilocode, claude_code, opencode, pi, hermes,
aider_native." (integrations/coding_agent/orchestrator.py, _execute_local).
The agent then read both files with get_user_uploaded_file, but the step
names execute_coding_task, a tool the agents could serve
(hartos.lifecycle_hooks.action_named_tools reads _function_map, the schema
and _hart_core_tools), so the step stayed open and the goal paused
needs_help after 3 tries.

The rule pinned here (rework of f8f436ab8 after its review):
  * execute_coding_task is offered where the orchestrator can run it
    (CodingAgentOrchestrator.can_run): a coding tool here, or, below the
    coding tier, the hive offload when a code-trusted peer exists.  A
    STANDARD+ node with no tool: not offered.  A LITE or embedded node with
    no tool and no trusted peer (central as measured live, tier read
    'embedded'): not offered.  The same with a trusted peer: offered, and
    the call offloads.
  * get_repository_map is offered where aider_native, which draws the map,
    is installed.
  * create_code_shard, get_coding_benchmarks and validate_json_response
    need no coding tool and stay everywhere.
  * a node with a coding tool builds exactly today's list;
  * the check never runs on the thread that builds the tools (the chat
    path): it runs at boot and in the background;
  * a call to a withheld tool is refused with what to do instead.
"""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from integrations.coding_agent import orchestrator as orch
from integrations.coding_agent import tool_backends as tb
from security import system_requirements as sr

GATED = {'execute_coding_task', 'get_repository_map'}
KEPT = {'create_code_shard', 'get_coding_benchmarks', 'validate_json_response',
        'get_user_uploaded_file'}

LIVE_STEP = ('execute_coding_task: read trade/leads/rice_importers.csv and '
             'trade/leads/hartos_import_rice_importers.json and report row '
             'counts and emails')


def _ctx():
    return {
        'user_id': '999', 'prompt_id': '8888', 'agent_data': {},
        'helper_fun': MagicMock(), 'user_prompt': '999_8888',
        'request_id_list': {'999_8888': 'req1'}, 'recent_file_id': {},
        'scheduler': MagicMock(), 'send_message_to_user1': MagicMock(),
        'retrieve_json': MagicMock(return_value={}),
        'strip_json_values': MagicMock(return_value=''),
        'save_conversation_db': MagicMock(return_value='1'),
    }


def _build():
    from core.agent_tools import build_core_tool_closures
    return build_core_tool_closures(_ctx())


#: A peer the hive paths may send a coding task to (_is_code_trusted).
TRUSTED_PEER = {'node_id': 'peer-1', 'trust_level': 'SAME_USER',
                'url': 'http://10.0.0.9:6777', 'x25519_public_hex': 'ab' * 32}


@pytest.fixture
def node(monkeypatch):
    """Set this node's tier, installed coding tools and compute-mesh peers
    (the boundaries the orchestrator reads), with no check kept yet.
    ``node(tier, backends, peers)`` then runs the check the way boot does,
    and returns the thread ids get_available_backends and the mesh's peer
    listing were called on."""
    from integrations.agent_engine import compute_mesh_service as cms
    # Someone can allow a command here (Shell_Command's 'runs_commands');
    # test_a_node_with_no_owner_is_not_offered_the_command_tool removes it.
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-of-this-node')
    monkeypatch.setattr(orch, '_coding_caps',
                        {'value': None, 'at': None, 'thread': None},
                        raising=False)
    calls = []

    def _set(tier, backends, peers=(), check=True):
        monkeypatch.setattr(sr, 'get_tier', lambda: tier)

        def fake():
            calls.append(threading.get_ident())
            return dict(backends)
        monkeypatch.setattr(tb, 'get_available_backends', fake)

        class Mesh:
            def get_available_peers(self):
                calls.append(threading.get_ident())
                return [dict(p) for p in peers]

            def score(self, peer):
                return 0.0
        monkeypatch.setattr(cms, 'get_compute_mesh', lambda: Mesh())
        if check:
            orch.refresh_coding_capabilities().join(5)
        return calls
    return _set


def test_central_with_no_coding_tool_is_not_offered_the_two(node):
    node(sr.NodeTierLevel.STANDARD, {})
    names = {n for n, _d, _f in _build()}
    assert not GATED & names, names
    assert KEPT <= names


@pytest.mark.parametrize('tier', [sr.NodeTierLevel.LITE,
                                  sr.NodeTierLevel.EMBEDDED])
def test_below_the_tier_with_no_tool_and_no_peer_it_is_dropped(node, tier):
    """Live on central 2026-11-21 (tier read 'embedded', no coding tool, no
    trusted peer): the offload had nowhere to go, the call fell back and
    answered "No coding tools installed"."""
    node(tier, {}, peers=[{'node_id': 'stranger', 'url': 'http://x'}])
    names = {n for n, _d, _f in _build()}
    assert 'execute_coding_task' not in names
    assert KEPT <= names


def test_below_the_tier_with_a_local_tool_and_no_peer_it_is_kept(node):
    node(sr.NodeTierLevel.LITE, {'hermes': object()})
    assert 'execute_coding_task' in {n for n, _d, _f in _build()}


def test_a_lite_node_with_no_tool_keeps_the_task_tool_and_offloads_it(node, monkeypatch):
    """On the parent of f8f436ab8 a LITE node with no coding tool sent the
    task to a trusted peer's /coding/execute and got success back."""
    node(sr.NodeTierLevel.LITE, {}, peers=[TRUSTED_PEER])
    tools = {n: f for n, _d, f in _build()}
    assert 'execute_coding_task' in tools
    assert 'get_repository_map' not in tools      # no aider here
    o = orch.CodingAgentOrchestrator()
    monkeypatch.setattr(o, '_classify_scope', lambda *a: 'trusted_peer')
    monkeypatch.setattr(o, '_distribute_to_hive', lambda *a, **k: {
        'success': True, 'tool': 'distributed', 'output': 'done by a peer'})
    monkeypatch.setattr(orch, 'get_coding_orchestrator', lambda: o)
    out = json.loads(asyncio.run(tools['execute_coding_task']('fix t.py')))
    assert out['success'] is True and out['tool'] == 'distributed'


def test_a_desktop_with_a_coding_tool_keeps_todays_list(node):
    node(sr.NodeTierLevel.FULL, {'hermes': object(), 'aider_native': object()},
         check=False)
    before_any_check = [n for n, _d, _f in _build()]     # cold: as today
    orch._coding_caps['thread'].join(5)                  # the check it started
    with_tool = [n for n, _d, _f in _build()]
    assert with_tool == before_any_check
    assert GATED <= set(with_tool)
    node(sr.NodeTierLevel.FULL, {})
    without = [n for n, _d, _f in _build()]
    assert [n for n in with_tool if n not in GATED] == without


def test_the_live_step_is_not_held_for_a_tool_the_node_cannot_run(node):
    """The step checker's servable set is what the agents hold; built on
    central, it no longer holds execute_coding_task, so the live step reads
    as naming no tool and the get_user_uploaded_file work can complete it."""
    from hartos.lifecycle_hooks import action_named_tools

    def seat():
        return SimpleNamespace(_function_map={}, llm_config={'tools': []},
                               _hart_core_tools=_build())

    node(sr.NodeTierLevel.STANDARD, {})
    assert action_named_tools([seat()], LIVE_STEP)[1] == []
    node(sr.NodeTierLevel.STANDARD, {'hermes': object()})
    assert 'execute_coding_task' in action_named_tools([seat()], LIVE_STEP)[1]


def test_a_tool_build_never_checks_on_its_own_thread(node):
    """The check builds every backend (415 ms first, 31-52 ms after,
    measured); a build reads the kept answer and an expired one is renewed
    in the background."""
    me = threading.get_ident()
    # Below the tier, so the check reads the mesh's peers too.
    calls = node(sr.NodeTierLevel.EMBEDDED, {}, check=False)
    names = {n for n, _d, _f in _build()}           # cold: no answer yet
    assert GATED <= names                            # offered, as before
    orch._coding_caps['thread'].join(5)              # the boot-style check
    assert calls and me not in calls
    calls.clear()
    orch._coding_caps['at'] -= orch.CODING_CAPABILITIES_TTL_S + 1   # expired
    names = {n for n, _d, _f in _build()}
    assert not GATED & names                         # the kept answer serves
    orch._coding_caps['thread'].join(5)
    assert calls and me not in calls                 # renewed off this thread


def test_a_call_to_a_withheld_coding_tool_says_what_to_do(node):
    from hartos.helper import executor_refusal
    node(sr.NodeTierLevel.STANDARD, {})
    reply = executor_refusal('execute_coding_task',
                             {'get_user_uploaded_file': None,
                              'send_message_to_user': None})
    assert reply.startswith('Error:')
    assert 'not installed on this computer' in reply
    assert 'get_user_uploaded_file' in reply and 'full path' in reply
    # A node that can run it but did not attach it keeps the plain refusal.
    node(sr.NodeTierLevel.STANDARD, {'hermes': object()})
    plain = executor_refusal('execute_coding_task',
                             {'get_user_uploaded_file': None})
    assert 'not installed on this computer' not in plain


def test_a_node_with_no_owner_is_not_offered_the_command_tool(node, monkeypatch):
    """Shell_Command runs a command only once the owner allows it
    (integrations.vlm.safety.computer_control_block), and with no owner
    identity it refuses every one: not offered, and a call to it is told
    to ask the owner instead (core.agent_tool_menu.CODING_TOOL_NEEDS)."""
    from hartos.helper import executor_refusal
    node(sr.NodeTierLevel.FULL, {'hermes': object(), 'aider_native': object()})
    assert 'Shell_Command' in {n for n, _d, _f in _build()}
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')
    node(sr.NodeTierLevel.FULL, {'hermes': object(), 'aider_native': object()})
    names = {n for n, _d, _f in _build()}
    assert 'Shell_Command' not in names and GATED <= names, names
    reply = executor_refusal('Shell_Command', {'ask_person': None,
                                               'get_user_uploaded_file': None})
    assert reply.startswith('Error:')
    assert 'nobody on this computer can allow' in reply, reply
    assert 'not installed' not in reply, reply
    assert 'ask_person' in reply and 'owner' in reply, reply
