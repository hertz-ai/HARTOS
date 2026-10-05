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
