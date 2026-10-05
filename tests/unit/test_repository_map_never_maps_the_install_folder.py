"""The repository-map tool maps the agent's workspace, never the process cwd.

get_repository_map defaulted to '.', the process cwd.  In the installed
desktop that is the install folder (C:\\Program Files (x86)\\HevolveAI\\Nunba),
and the map writes its tags cache under the root it maps: the 14 MB
.aider.tags.cache.v4 measured there on 2026-10-05.  The review of cd99c540d
found this tool still on '.' after the coding run's own workspace fix.  It
now resolves its directory the way execute_coding_task does: the caller's
directory, else HEVOLVE_CODING_WORKDIR, else the agent's goal repo_path,
else the user-data coding workspace (vlm_adapter.resolve_task_workspace).

    python -m pytest tests/unit/test_repository_map_never_maps_the_install_folder.py -q
"""
import asyncio
import os
from unittest.mock import MagicMock

import pytest


def _repository_map(prompt_id='8888'):
    """The REAL get_repository_map closure an agent turn is given."""
    from core.agent_tools import build_core_tool_closures
    ctx = {
        'user_id': '999', 'prompt_id': prompt_id, 'agent_data': {},
        'helper_fun': MagicMock(), 'user_prompt': f'999_{prompt_id}',
        'request_id_list': {f'999_{prompt_id}': 'req1'}, 'recent_file_id': {},
        'scheduler': MagicMock(), 'send_message_to_user1': MagicMock(),
        'retrieve_json': MagicMock(return_value={}),
        'strip_json_values': MagicMock(return_value=''),
        'save_conversation_db': MagicMock(return_value='1'),
    }
    tools = {name: fn for name, _d, fn in build_core_tool_closures(ctx)}
    return tools['get_repository_map']


@pytest.fixture
def mapped(monkeypatch, tmp_path):
    """The directory the map was asked to build in.  Faked at the
    boundaries only: the user-data workspace and the tree-sitter map."""
    from integrations.coding_agent.recipe_bridge import CodingRecipeBridge
    workspace = tmp_path / 'coding_workspace'
    workspace.mkdir()
    monkeypatch.setattr('core.platform_paths.get_coding_workspace_dir',
                        lambda: str(workspace))
    monkeypatch.delenv('HEVOLVE_CODING_WORKDIR', raising=False)
    roots = []
    monkeypatch.setattr(
        CodingRecipeBridge, 'get_repository_map',
        staticmethod(lambda working_dir='.', max_tokens=2048:
                     (roots.append(working_dir), 'the map')[1]))
    return {'roots': roots, 'workspace': str(workspace)}


def test_no_directory_maps_the_workspace_never_the_process_cwd(mapped):
    assert asyncio.run(_repository_map()()) == 'the map'
    assert mapped['roots'] == [mapped['workspace']]
    assert os.path.abspath(mapped['roots'][0]) != os.path.abspath(os.getcwd())


def test_the_configured_coding_directory_comes_next(mapped, monkeypatch, tmp_path):
    monkeypatch.setenv('HEVOLVE_CODING_WORKDIR', str(tmp_path / 'clone'))
    asyncio.run(_repository_map()())
    assert mapped['roots'] == [str(tmp_path / 'clone')]


def test_a_directory_the_agent_names_is_kept(mapped, tmp_path):
    asyncio.run(_repository_map()(working_dir=str(tmp_path / 'repo')))
    assert mapped['roots'] == [str(tmp_path / 'repo')]


def test_the_agents_own_goal_repo_is_found_by_its_prompt_id(mapped, monkeypatch):
    asked = []
    monkeypatch.setattr('integrations.vlm.vlm_adapter.resolve_task_workspace',
                        lambda prompt_id=None, explicit='': (
                            asked.append((prompt_id, explicit)), 'D:/goal-repo')[1])
    asyncio.run(_repository_map(prompt_id='8888')())
    assert asked == [('8888', '')]
    assert mapped['roots'] == ['D:/goal-repo']


# ── review of c49271ba8 (11:39Z): a relative directory still meant the cwd ──

@pytest.fixture
def in_install_folder(tmp_path, monkeypatch):
    """The process cwd is an install folder, as on the frozen desktop."""
    install = tmp_path / 'Program Files (x86)' / 'HevolveAI' / 'Nunba'
    install.mkdir(parents=True)
    monkeypatch.chdir(install)
    return str(install)


def test_a_relative_directory_maps_inside_the_workspace(mapped, in_install_folder):
    asyncio.run(_repository_map()(working_dir='.'))
    asyncio.run(_repository_map()(working_dir='tts'))
    assert mapped['roots'] == [mapped['workspace'],
                               os.path.join(mapped['workspace'], 'tts')]
    assert in_install_folder not in mapped['roots']


def test_a_relative_configured_directory_maps_inside_the_workspace(
        mapped, in_install_folder, monkeypatch):
    monkeypatch.setenv('HEVOLVE_CODING_WORKDIR', '.')
    asyncio.run(_repository_map()())
    assert mapped['roots'] == [mapped['workspace']]


def test_the_mcp_code_tool_does_not_hand_over_its_own_cwd(monkeypatch, in_install_folder):
    """The stdio MCP `code` tool passed os.getcwd() when no directory was
    named, so a coding run worked wherever the MCP server was launched.  It
    now passes what it was given; the orchestrator resolves an empty one
    through resolve_task_workspace like every other coding entry point."""
    import types
    from tests.unit.module_swap import swap_modules

    class _FastMCP:
        def __init__(self, *_a, **_k):
            pass

        def tool(self, *_a, **_k):
            return lambda fn: fn

        def resource(self, *_a, **_k):
            return lambda fn: fn

        def prompt(self, *_a, **_k):
            return lambda fn: fn

    fastmcp = types.ModuleType('mcp.server.fastmcp')
    fastmcp.FastMCP = _FastMCP
    server = types.ModuleType('mcp.server')
    server.fastmcp = fastmcp
    mcp_pkg = types.ModuleType('mcp')
    mcp_pkg.server = server
    calls = []

    class _Orchestrator:
        def execute(self, **kw):
            calls.append(kw)
            return {'success': True}

    import importlib
    import sys
    import integrations.mcp as mcp_package
    had_attr = hasattr(mcp_package, 'mcp_server')
    saved_attr = getattr(mcp_package, 'mcp_server', None)
    try:
        # The real module needs the optional `mcp` SDK; the stub stands in
        # for it, and the module imported against it is evicted on exit.
        with swap_modules({'mcp': mcp_pkg, 'mcp.server': server,
                           'mcp.server.fastmcp': fastmcp,
                           'integrations.mcp.mcp_server': None}):
            sys.modules.pop('integrations.mcp.mcp_server', None)
            mcp_server = importlib.import_module('integrations.mcp.mcp_server')
            monkeypatch.setattr(
                'integrations.coding_agent.orchestrator.get_coding_orchestrator',
                lambda: _Orchestrator())
            mcp_server.code(task='add a test')
            mcp_server.code(task='add a test', working_dir='D:/named/repo')
    finally:
        if had_attr:
            mcp_package.mcp_server = saved_attr
        elif hasattr(mcp_package, 'mcp_server'):
            delattr(mcp_package, 'mcp_server')
    assert [c['working_dir'] for c in calls] == ['', 'D:/named/repo']
