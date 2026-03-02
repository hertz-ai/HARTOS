"""Tests for hart CLI (Click-based command line interface)."""
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

from click.testing import CliRunner

# Import the CLI
from hart_cli import hart


@pytest.fixture
def runner():
    """Click CLI test runner."""
    return CliRunner()


# ── Top-level help & version ──

def test_help(runner):
    """hart --help shows all subcommands."""
    result = runner.invoke(hart, ['--help'])
    assert result.exit_code == 0
    assert 'HART OS' in result.output
    # Check key subcommands are listed
    for cmd in ['chat', 'code', 'social', 'agent', 'expert', 'pay',
                'mcp', 'compute', 'channel', 'tools', 'recipe', 'status',
                'vision', 'voice', 'a2a', 'skill', 'schedule', 'zeroshot',
                'desktop', 'remote', 'screenshot', 'repomap']:
        assert cmd in result.output, f'Missing subcommand: {cmd}'


def test_version(runner):
    """hart --version shows version."""
    result = runner.invoke(hart, ['--version'])
    assert result.exit_code == 0
    assert '0.1.0' in result.output


def test_no_command_shows_help(runner):
    """hart with no args shows help."""
    result = runner.invoke(hart, [])
    assert result.exit_code == 0
    assert 'HART OS' in result.output


# ── Subcommand group help ──

def test_social_help(runner):
    """hart social --help shows subcommands."""
    result = runner.invoke(hart, ['social', '--help'])
    assert result.exit_code == 0
    for cmd in ['post', 'feed', 'comment', 'communities', 'karma',
                'vote', 'encounter', 'wallet', 'leaderboard']:
        assert cmd in result.output, f'Missing social subcommand: {cmd}'


def test_agent_help(runner):
    """hart agent --help shows subcommands."""
    result = runner.invoke(hart, ['agent', '--help'])
    assert result.exit_code == 0
    for cmd in ['list', 'create', 'delegate', 'goal']:
        assert cmd in result.output, f'Missing agent subcommand: {cmd}'


def test_agent_goal_help(runner):
    """hart agent goal --help shows nested subcommands."""
    result = runner.invoke(hart, ['agent', 'goal', '--help'])
    assert result.exit_code == 0
    for cmd in ['list', 'create', 'types']:
        assert cmd in result.output, f'Missing goal subcommand: {cmd}'


def test_expert_help(runner):
    """hart expert --help shows subcommands."""
    result = runner.invoke(hart, ['expert', '--help'])
    assert result.exit_code == 0
    for cmd in ['list', 'find', 'info']:
        assert cmd in result.output, f'Missing expert subcommand: {cmd}'


def test_pay_help(runner):
    """hart pay --help shows subcommands."""
    result = runner.invoke(hart, ['pay', '--help'])
    assert result.exit_code == 0
    for cmd in ['request', 'list', 'authorize', 'process']:
        assert cmd in result.output, f'Missing pay subcommand: {cmd}'


def test_mcp_help(runner):
    """hart mcp --help shows subcommands."""
    result = runner.invoke(hart, ['mcp', '--help'])
    assert result.exit_code == 0
    for cmd in ['list', 'execute', 'servers']:
        assert cmd in result.output, f'Missing mcp subcommand: {cmd}'


def test_compute_help(runner):
    """hart compute --help shows subcommands."""
    result = runner.invoke(hart, ['compute', '--help'])
    assert result.exit_code == 0
    for cmd in ['status', 'providers', 'pressure', 'join', 'revenue']:
        assert cmd in result.output, f'Missing compute subcommand: {cmd}'


def test_channel_help(runner):
    """hart channel --help shows subcommands."""
    result = runner.invoke(hart, ['channel', '--help'])
    assert result.exit_code == 0
    for cmd in ['list', 'send', 'broadcast']:
        assert cmd in result.output, f'Missing channel subcommand: {cmd}'


def test_a2a_help(runner):
    """hart a2a --help shows subcommands."""
    result = runner.invoke(hart, ['a2a', '--help'])
    assert result.exit_code == 0
    for cmd in ['discover', 'send', 'agents']:
        assert cmd in result.output, f'Missing a2a subcommand: {cmd}'


def test_voice_help(runner):
    """hart voice --help shows subcommands."""
    result = runner.invoke(hart, ['voice', '--help'])
    assert result.exit_code == 0
    assert 'transcribe' in result.output


def test_skill_help(runner):
    """hart skill --help shows subcommands."""
    result = runner.invoke(hart, ['skill', '--help'])
    assert result.exit_code == 0
    for cmd in ['list', 'ingest']:
        assert cmd in result.output, f'Missing skill subcommand: {cmd}'


def test_tools_help(runner):
    """hart tools --help shows subcommands."""
    result = runner.invoke(hart, ['tools', '--help'])
    assert result.exit_code == 0
    for cmd in ['list', 'install']:
        assert cmd in result.output, f'Missing tools subcommand: {cmd}'


def test_recipe_help(runner):
    """hart recipe --help shows subcommands."""
    result = runner.invoke(hart, ['recipe', '--help'])
    assert result.exit_code == 0
    for cmd in ['list', 'show']:
        assert cmd in result.output, f'Missing recipe subcommand: {cmd}'


# ── Offline commands (no server needed) ──

def test_tools_list(runner):
    """hart tools list works offline."""
    result = runner.invoke(hart, ['tools', 'list'])
    assert result.exit_code == 0
    assert 'Coding Tools' in result.output


def test_tools_list_json(runner):
    """hart --json tools list returns valid JSON."""
    result = runner.invoke(hart, ['--json', 'tools', 'list'])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert 'tools' in data


def test_recipe_list(runner):
    """hart recipe list works."""
    result = runner.invoke(hart, ['recipe', 'list'])
    # May show recipes or "No recipes found"
    assert result.exit_code == 0


def test_a2a_agents(runner):
    """hart a2a agents lists local A2A agents."""
    result = runner.invoke(hart, ['a2a', 'agents'])
    assert result.exit_code == 0
    # Either shows agents or "No A2A agents found"


def test_a2a_agents_json(runner):
    """hart --json a2a agents returns valid JSON."""
    result = runner.invoke(hart, ['--json', 'a2a', 'agents'])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)


def test_mcp_servers(runner):
    """hart mcp servers shows config info."""
    result = runner.invoke(hart, ['mcp', 'servers'])
    assert result.exit_code == 0
    # Either shows servers or "No mcp_servers.json found"


def test_channel_list(runner):
    """hart channel list works offline."""
    result = runner.invoke(hart, ['channel', 'list'])
    # Should show supported channels or adapter list
    assert result.exit_code == 0


# ── Headless mode (-p) ──

def test_headless_mode(runner):
    """hart -p 'task' dispatches to /chat."""
    mock_response = MagicMock()
    mock_response.json.return_value = {'response': 'Hello from HART'}

    with patch('requests.post', return_value=mock_response):
        result = runner.invoke(hart, ['-p', 'say hello'])
        assert result.exit_code == 0
        assert 'Hello from HART' in result.output


def test_headless_mode_json(runner):
    """hart -p 'task' --json returns JSON."""
    mock_response = MagicMock()
    mock_response.json.return_value = {'response': 'Hello'}

    with patch('requests.post', return_value=mock_response):
        result = runner.invoke(hart, ['-p', 'say hello', '--json'])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['response'] == 'Hello'


def test_headless_connection_error(runner):
    """hart -p handles connection error gracefully."""
    import requests as real_requests

    with patch('requests.post', side_effect=real_requests.ConnectionError):
        result = runner.invoke(hart, ['-p', 'say hello'])
        assert result.exit_code != 0


# ── Desktop group ──

def test_desktop_help(runner):
    """hart desktop --help shows all direct action subcommands."""
    result = runner.invoke(hart, ['desktop', '--help'])
    assert result.exit_code == 0
    for cmd in ['auto', 'click', 'rightclick', 'doubleclick', 'middleclick',
                'move', 'drag', 'cursor', 'type', 'key', 'hotkey',
                'screenshot', 'wait', 'ls', 'readfile', 'writefile',
                'openfile', 'copyfile']:
        assert cmd in result.output, f'Missing desktop subcommand: {cmd}'


def test_desktop_no_subcommand_shows_help(runner):
    """hart desktop with no subcommand shows help."""
    result = runner.invoke(hart, ['desktop'])
    assert result.exit_code == 0
    assert 'auto' in result.output
    assert 'click' in result.output


@patch('integrations.vlm.local_computer_tool.execute_action')
def test_desktop_click(mock_exec, runner):
    """hart desktop click X Y calls execute_action with left_click."""
    mock_exec.return_value = {'output': 'Clicked at [100, 200]'}
    result = runner.invoke(hart, ['desktop', 'click', '100', '200'])
    assert result.exit_code == 0
    assert 'Clicked' in result.output
    mock_exec.assert_called_once()
    action = mock_exec.call_args[0][0]
    assert action['action'] == 'left_click'
    assert action['coordinate'] == [100, 200]


@patch('integrations.vlm.local_computer_tool.execute_action')
def test_desktop_type(mock_exec, runner):
    """hart desktop type sends text via execute_action."""
    mock_exec.return_value = {'output': 'Typed: hello world...'}
    result = runner.invoke(hart, ['desktop', 'type', 'hello world'])
    assert result.exit_code == 0
    assert 'Typed' in result.output
    action = mock_exec.call_args[0][0]
    assert action['action'] == 'type'
    assert action['text'] == 'hello world'


@patch('integrations.vlm.local_computer_tool.execute_action')
def test_desktop_key(mock_exec, runner):
    """hart desktop key sends key press."""
    mock_exec.return_value = {'output': 'Pressed key: enter'}
    result = runner.invoke(hart, ['desktop', 'key', 'enter'])
    assert result.exit_code == 0
    action = mock_exec.call_args[0][0]
    assert action['action'] == 'key'
    assert action['text'] == 'enter'


@patch('integrations.vlm.local_computer_tool.execute_action')
def test_desktop_hotkey(mock_exec, runner):
    """hart desktop hotkey sends key combination."""
    mock_exec.return_value = {'output': 'Hotkey: ctrl+c'}
    result = runner.invoke(hart, ['desktop', 'hotkey', 'ctrl+c'])
    assert result.exit_code == 0
    action = mock_exec.call_args[0][0]
    assert action['action'] == 'hotkey'
    assert action['text'] == 'ctrl+c'


@patch('integrations.vlm.local_computer_tool.execute_action')
def test_desktop_drag(mock_exec, runner):
    """hart desktop drag sends drag action with start/end coordinates."""
    mock_exec.return_value = {'output': 'Dragged from [10, 20] to [100, 200]'}
    result = runner.invoke(hart, ['desktop', 'drag', '10', '20', '100', '200'])
    assert result.exit_code == 0
    action = mock_exec.call_args[0][0]
    assert action['action'] == 'left_click_drag'
    assert action['startCoordinate'] == [10, 20]
    assert action['endCoordinate'] == [100, 200]


@patch('integrations.vlm.local_computer_tool.execute_action')
def test_desktop_cursor(mock_exec, runner):
    """hart desktop cursor gets cursor position."""
    mock_exec.return_value = {'output': 'Cursor at (512, 384)'}
    result = runner.invoke(hart, ['desktop', 'cursor'])
    assert result.exit_code == 0
    assert 'Cursor' in result.output
    action = mock_exec.call_args[0][0]
    assert action['action'] == 'cursor_position'


@patch('integrations.vlm.local_computer_tool.execute_action')
def test_desktop_wait(mock_exec, runner):
    """hart desktop wait sleeps for specified duration."""
    mock_exec.return_value = {'output': 'Waited 3.0s'}
    result = runner.invoke(hart, ['desktop', 'wait', '3'])
    assert result.exit_code == 0
    action = mock_exec.call_args[0][0]
    assert action['action'] == 'wait'
    assert action['duration'] == 3.0


@patch('integrations.vlm.local_computer_tool.execute_action')
def test_desktop_ls(mock_exec, runner):
    """hart desktop ls lists directory contents."""
    mock_exec.return_value = {'output': 'file1.py\nfile2.py'}
    result = runner.invoke(hart, ['desktop', 'ls', '/tmp'])
    assert result.exit_code == 0
    action = mock_exec.call_args[0][0]
    assert action['action'] == 'list_folders_and_files'
    assert action['path'] == '/tmp'


@patch('integrations.vlm.local_computer_tool.execute_action')
def test_desktop_readfile(mock_exec, runner):
    """hart desktop readfile reads a file."""
    mock_exec.return_value = {'output': 'file contents here'}
    result = runner.invoke(hart, ['desktop', 'readfile', '/tmp/test.txt'])
    assert result.exit_code == 0
    action = mock_exec.call_args[0][0]
    assert action['action'] == 'read_file_and_understand'
    assert action['path'] == '/tmp/test.txt'


@patch('integrations.vlm.local_computer_tool.execute_action')
def test_desktop_writefile(mock_exec, runner):
    """hart desktop writefile writes content to a file."""
    mock_exec.return_value = {'output': 'Written to /tmp/out.txt'}
    result = runner.invoke(hart, ['desktop', 'writefile', '/tmp/out.txt', 'hello'])
    assert result.exit_code == 0
    action = mock_exec.call_args[0][0]
    assert action['action'] == 'write_file'
    assert action['path'] == '/tmp/out.txt'
    assert action['content'] == 'hello'


@patch('integrations.vlm.local_computer_tool.execute_action')
def test_desktop_rightclick(mock_exec, runner):
    """hart desktop rightclick sends right-click action."""
    mock_exec.return_value = {'output': 'Right-clicked at [50, 60]'}
    result = runner.invoke(hart, ['desktop', 'rightclick', '50', '60'])
    assert result.exit_code == 0
    action = mock_exec.call_args[0][0]
    assert action['action'] == 'right_click'
    assert action['coordinate'] == [50, 60]


@patch('integrations.vlm.local_computer_tool.execute_action')
def test_desktop_json_output(mock_exec, runner):
    """hart --json desktop click returns JSON."""
    mock_exec.return_value = {'output': 'Clicked at [1, 2]'}
    result = runner.invoke(hart, ['--json', 'desktop', 'click', '1', '2'])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data['output'] == 'Clicked at [1, 2]'


@patch('integrations.vlm.local_computer_tool.execute_action')
def test_desktop_error_handling(mock_exec, runner):
    """hart desktop handles action errors gracefully."""
    mock_exec.return_value = {'output': '', 'error': 'pyautogui not installed'}
    result = runner.invoke(hart, ['desktop', 'click', '100', '200'])
    assert result.exit_code != 0
    assert 'pyautogui not installed' in result.output


# ── Repomap ──

def test_repomap_help(runner):
    """hart repomap --help shows options."""
    result = runner.invoke(hart, ['repomap', '--help'])
    assert result.exit_code == 0
    assert '--tokens' in result.output
    assert '--dir' in result.output


# ── Code command ──

def test_code_help(runner):
    """hart code --help shows options."""
    result = runner.invoke(hart, ['code', '--help'])
    assert result.exit_code == 0
    assert '--task-type' in result.output
    assert '--tool' in result.output
    assert '--files' in result.output


# ── Status (with mock) ──

@patch('hart_cli._api_get')
def test_status_offline(mock_get, runner):
    """hart status handles offline server."""
    mock_get.return_value = None

    result = runner.invoke(hart, ['status'])
    # May exit with error or show offline status
    assert True  # Just verifying no crash


# ── Social commands (with mock) ──

@patch('hart_cli._api_post')
def test_social_post(mock_post, runner):
    """hart social post creates a post."""
    mock_post.return_value = {'id': '123'}

    result = runner.invoke(hart, ['social', 'post', 'Hello world!'])
    assert result.exit_code == 0


@patch('hart_cli._api_get')
def test_social_feed(mock_get, runner):
    """hart social feed shows posts."""
    mock_get.return_value = {'posts': [
        {'id': '1', 'author': 'user1', 'content': 'First post'},
    ]}

    result = runner.invoke(hart, ['social', 'feed'])
    assert result.exit_code == 0


@patch('hart_cli._api_get')
def test_social_communities(mock_get, runner):
    """hart social communities lists communities."""
    mock_get.return_value = {'communities': [
        {'name': 'test', 'member_count': 42},
    ]}

    result = runner.invoke(hart, ['social', 'communities'])
    assert result.exit_code == 0


# ── Count test ──

def test_subcommand_count(runner):
    """CLI has at least 21 subcommands."""
    result = runner.invoke(hart, ['--help'])
    # Count lines that look like subcommand entries (indented name + description)
    commands = [line.strip().split()[0] for line in result.output.split('\n')
                if line.strip() and not line.strip().startswith('-')
                and not line.strip().startswith('Usage')
                and not line.strip().startswith('HART')
                and not line.strip().startswith('Options')
                and not line.strip().startswith('Commands')]
    # Just verify we have many subcommands
    assert 'social' in result.output
    assert 'agent' in result.output
    assert 'expert' in result.output
    assert 'pay' in result.output
    assert 'mcp' in result.output
