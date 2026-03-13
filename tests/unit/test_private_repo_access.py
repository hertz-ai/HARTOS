"""Tests for PrivateRepoAccessService - GitHub invite/revoke + access control."""
import os
import sys

import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from integrations.agent_engine.private_repo_access import (
    PrivateRepoAccessService,
    _extract_owner_repo,
)


@pytest.fixture(autouse=True)
def reset_module_globals(monkeypatch):
    """Reset module-level caches."""
    import integrations.agent_engine.private_repo_access as mod
    mod._PRIVATE_REPOS = None
    mod._GITHUB_TOKEN = None
    yield


class TestIsPrivateRepo:
    """Test private repo detection."""

    def test_matches_env_list(self, monkeypatch):
        monkeypatch.setenv('HEVOLVE_PRIVATE_REPOS',
                           'hevolve-ai/hevolveai,hevolve-ai/secret-core')
        result = PrivateRepoAccessService.is_private_repo(
            'https://github.com/hevolve-ai/hevolveai')
        assert result is True

    def test_no_match(self, monkeypatch):
        monkeypatch.setenv('HEVOLVE_PRIVATE_REPOS', 'hevolve-ai/hevolveai')
        result = PrivateRepoAccessService.is_private_repo(
            'https://github.com/hevolve-ai/public-repo')
        assert result is False

    def test_empty_env(self, monkeypatch):
        monkeypatch.setenv('HEVOLVE_PRIVATE_REPOS', '')
        result = PrivateRepoAccessService.is_private_repo(
            'https://github.com/anything/repo')
        assert result is False

    def test_handles_git_suffix(self, monkeypatch):
        monkeypatch.setenv('HEVOLVE_PRIVATE_REPOS', 'hevolve-ai/hevolveai')
        result = PrivateRepoAccessService.is_private_repo(
            'https://github.com/hevolve-ai/hevolveai.git')
        assert result is True


class TestVerifyAccess:
    """Test certificate-based access control."""

    def test_non_private_repo_always_allowed(self, monkeypatch):
        monkeypatch.setenv('HEVOLVE_PRIVATE_REPOS', 'hevolve-ai/hevolveai')
        result = PrivateRepoAccessService.verify_access(
            None, 'https://github.com/hevolve-ai/public')
        assert result['allowed'] is True

    def test_no_certificate_denied(self, monkeypatch):
        monkeypatch.setenv('HEVOLVE_PRIVATE_REPOS', 'hevolve-ai/hevolveai')
        result = PrivateRepoAccessService.verify_access(
            None, 'https://github.com/hevolve-ai/hevolveai')
        assert result['allowed'] is False

    def test_central_full_access(self, monkeypatch):
        monkeypatch.setenv('HEVOLVE_PRIVATE_REPOS', 'hevolve-ai/hevolveai')
        cert = {'tier': 'central', 'node_id': 'central_1'}
        result = PrivateRepoAccessService.verify_access(
            cert, 'https://github.com/hevolve-ai/hevolveai')
        assert result['allowed'] is True
        assert result['access_level'] == 'full'

    @patch('security.key_delegation.verify_certificate_chain')
    def test_regional_push_access(self, mock_verify, monkeypatch):
        monkeypatch.setenv('HEVOLVE_PRIVATE_REPOS', 'hevolve-ai/hevolveai')
        mock_verify.return_value = True
        cert = {'tier': 'regional', 'node_id': 'regional_1'}

        result = PrivateRepoAccessService.verify_access(
            cert, 'https://github.com/hevolve-ai/hevolveai', 'push')
        assert result['allowed'] is True
        assert result['access_level'] == 'push'

    @patch('security.key_delegation.verify_certificate_chain')
    def test_regional_invalid_cert_denied(self, mock_verify, monkeypatch):
        monkeypatch.setenv('HEVOLVE_PRIVATE_REPOS', 'hevolve-ai/hevolveai')
        mock_verify.return_value = False
        cert = {'tier': 'regional', 'node_id': 'bad_node'}

        result = PrivateRepoAccessService.verify_access(
            cert, 'https://github.com/hevolve-ai/hevolveai')
        assert result['allowed'] is False

    def test_local_denied(self, monkeypatch):
        monkeypatch.setenv('HEVOLVE_PRIVATE_REPOS', 'hevolve-ai/hevolveai')
        cert = {'tier': 'local', 'node_id': 'local_1'}
        result = PrivateRepoAccessService.verify_access(
            cert, 'https://github.com/hevolve-ai/hevolveai')
        assert result['allowed'] is False


class TestSendGitHubInvite:
    """Test GitHub invite via gh CLI and HTTP."""

    @patch('subprocess.run')
    def test_invite_via_gh_cli(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='')
        result = PrivateRepoAccessService.send_github_invite(
            'hevolve-ai/hevolveai', 'testuser', 'push')

        assert result['invited'] is True
        assert result['method'] == 'gh_cli'
        mock_run.assert_called_once()

    @patch('subprocess.run', side_effect=FileNotFoundError)
    @patch('requests.put')
    def test_invite_fallback_to_http(self, mock_put, mock_run, monkeypatch):
        monkeypatch.setenv('HEVOLVE_GITHUB_TOKEN', 'fake_token')
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_put.return_value = mock_resp

        result = PrivateRepoAccessService.send_github_invite(
            'https://github.com/hevolve-ai/hevolveai', 'testuser', 'push')

        assert result['invited'] is True
        assert result['method'] == 'http_api'

    def test_invite_no_username(self):
        result = PrivateRepoAccessService.send_github_invite(
            'hevolve-ai/hevolveai', '', 'push')
        assert result['invited'] is False


class TestRevokeGitHubAccess:
    """Test GitHub revoke."""

    @patch('subprocess.run')
    def test_revoke_via_gh_cli(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        result = PrivateRepoAccessService.revoke_github_access(
            'hevolve-ai/hevolveai', 'testuser')
        assert result['revoked'] is True

    def test_revoke_no_username(self):
        result = PrivateRepoAccessService.revoke_github_access(
            'hevolve-ai/hevolveai', '')
        assert result['revoked'] is False


class TestSplitRepoTask:
    """Test task splitting for private repo delegation."""

    def test_splits_by_file(self):
        subtasks = PrivateRepoAccessService.split_repo_task(
            'Fix bug in auth',
            'hevolve-ai/hevolveai',
            target_files=['auth.py', 'tests/test_auth.py'],
        )
        assert len(subtasks) == 2
        assert subtasks[0]['file_path'] == 'auth.py'
        assert 'Fix bug in auth' in subtasks[0]['description']

    def test_single_subtask_when_no_files(self):
        subtasks = PrivateRepoAccessService.split_repo_task(
            'Refactor module', 'hevolve-ai/hevolveai')
        assert len(subtasks) == 1


class TestExtractOwnerRepo:
    """Test _extract_owner_repo helper."""

    def test_full_url(self):
        assert _extract_owner_repo(
            'https://github.com/hevolve-ai/hevolveai') == (
                'hevolve-ai', 'hevolveai')

    def test_url_with_git_suffix(self):
        assert _extract_owner_repo(
            'https://github.com/hevolve-ai/hevolveai.git') == (
                'hevolve-ai', 'hevolveai')

    def test_owner_repo_format(self):
        assert _extract_owner_repo('hevolve-ai/hevolveai') == (
            'hevolve-ai', 'hevolveai')

    def test_empty_returns_none(self):
        assert _extract_owner_repo('') is None

    def test_invalid_returns_none(self):
        assert _extract_owner_repo('just-a-name') is None
