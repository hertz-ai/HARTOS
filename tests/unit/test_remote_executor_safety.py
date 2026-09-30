"""Safety contract at the remote computer-use transport boundary."""

from unittest.mock import patch

from integrations.coding_agent.remote_executor import RemoteDesktopExecutor


def test_remote_shutdown_is_refused_even_when_force_is_requested():
    executor = RemoteDesktopExecutor('http://example.invalid')
    with patch('integrations.coding_agent.remote_executor.pooled_post') as post:
        result = executor.execute('shutdown.exe /s /t 0', force=True)
    assert result['success'] is False
    assert 'destructive_computer_operation' in result['error']
    post.assert_not_called()


def test_remote_safe_command_reaches_existing_transport():
    executor = RemoteDesktopExecutor('http://example.invalid')
    response = type('Response', (), {
        'status_code': 200,
        'json': lambda self: {'returncode': 0, 'output': 'ok'},
    })()
    with patch('integrations.coding_agent.remote_executor.pooled_post', return_value=response) as post:
        result = executor.execute('dir')
    assert result == {'success': True, 'output': 'ok', 'returncode': 0}
    post.assert_called_once()
