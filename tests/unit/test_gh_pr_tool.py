"""Unit tests for integrations/service_tools/gh_pr_tool.py

Coverage strategy:
  * Validator unit tests           — pure functions, no I/O
  * Public entrypoint negative path — invalid input shape, no network
  * Auth gating                     — env-var dance, no network
  * Subprocess sequencing           — mocked subprocess.run, no real ``gh``

We DO NOT exercise a live GitHub PR creation in CI — that would need a
real test repo + GH_TOKEN.  The mocked tests validate the call-graph
(clone → branch → write → add → commit → push → pr create) and the
error-classification (which reason_code fires on which failure).
"""
import json
import os
import subprocess
from unittest import mock

import pytest

from integrations.service_tools import gh_pr_tool
from integrations.service_tools.gh_pr_tool import (
    GhPrTool,
    _validate_branch,
    _validate_relpath,
    _validate_repo,
    gh_pr_open,
)


# ── validators ────────────────────────────────────────────────────

class TestValidators:
    @pytest.mark.parametrize('repo', [
        'owner/repo',
        'hertz-ai/Hevolve-Landing',
        'a/b',
        'with-hyphen/with.dot',
        'A1/foo_bar',
    ])
    def test_repo_valid(self, repo):
        assert _validate_repo(repo) is None

    @pytest.mark.parametrize('repo', [
        '',                  # empty
        'no-slash',          # missing /
        '/repo',             # missing owner
        'owner/',            # missing repo
        'owner//repo',       # double slash
        'owner/repo/extra',  # too many segments
        'bad name/repo',     # space in owner
        '-leadhyph/repo',    # owner cannot start with hyphen
        None,                # not str
        123,                 # not str
    ])
    def test_repo_invalid(self, repo):
        assert _validate_repo(repo) is not None

    @pytest.mark.parametrize('branch', [
        'main',
        'feature/foo',
        'auto/seo-blog-2026-05-19',
        'release_1.2.3',
    ])
    def test_branch_valid(self, branch):
        assert _validate_branch(branch) is None

    @pytest.mark.parametrize('branch', [
        '',                # empty (required)
        '/leading-slash',
        'trailing-slash/',
        'double//slash',
        'has..dotdot',
        'with space',
        'with\nnewline',
    ])
    def test_branch_invalid(self, branch):
        assert _validate_branch(branch) is not None

    def test_branch_allow_empty(self):
        assert _validate_branch('', allow_empty=True) is None

    @pytest.mark.parametrize('path', [
        'src/pages/Blogs/foo.md',
        'public/sitemap.xml',
        'README.md',
        'a/b/c/d.txt',
    ])
    def test_relpath_valid(self, path):
        assert _validate_relpath(path) is None

    @pytest.mark.parametrize('path', [
        '',                          # empty
        '/abs/path',                 # leading slash
        '../etc/passwd',             # traversal
        'a/../b',                    # traversal mid-path
        'C:/Windows/System32',       # Windows absolute
        'd:\\foo',                   # Windows drive letter (backslash)
    ])
    def test_relpath_invalid(self, path):
        assert _validate_relpath(path) is not None


# ── public entrypoint: input-shape errors (no network) ────────────

def _ok_files():
    return [{'path': 'src/pages/Blogs/test.md', 'content': '# hi'}]


def _ok_params(**overrides):
    base = {
        'repo': 'owner/repo',
        'head_branch': 'feat/test',
        'title': 'feat: test PR',
        'body': 'body',
        'files': _ok_files(),
    }
    base.update(overrides)
    return base


class TestInputValidation:
    def test_bad_json_string(self):
        r = json.loads(gh_pr_open('not valid json'))
        assert r['ok'] is False
        assert r['reason_code'] == 'bad_input'

    def test_missing_repo(self):
        params = _ok_params()
        del params['repo']
        r = json.loads(gh_pr_open(json.dumps(params)))
        assert r['reason_code'] == 'bad_input'

    def test_invalid_repo(self):
        r = json.loads(gh_pr_open(json.dumps(_ok_params(repo='bad name'))))
        assert r['reason_code'] == 'bad_input'
        assert 'invalid repo' in r['error']

    def test_invalid_branch(self):
        r = json.loads(gh_pr_open(json.dumps(_ok_params(head_branch='bad..branch'))))
        assert r['reason_code'] == 'bad_input'

    def test_missing_title(self):
        r = json.loads(gh_pr_open(json.dumps(_ok_params(title=''))))
        assert r['reason_code'] == 'bad_input'

    def test_title_too_long(self):
        r = json.loads(gh_pr_open(json.dumps(_ok_params(title='x' * 300))))
        assert r['reason_code'] == 'bad_input'
        assert 'too long' in r['error']

    def test_body_too_large(self):
        r = json.loads(gh_pr_open(json.dumps(_ok_params(body='x' * (100 * 1024)))))
        assert r['reason_code'] == 'bad_input'
        assert 'too large' in r['error']

    def test_no_files(self):
        r = json.loads(gh_pr_open(json.dumps(_ok_params(files=[]))))
        assert r['reason_code'] == 'bad_input'

    def test_too_many_files(self):
        files = [{'path': f'a{i}.md', 'content': 'x'} for i in range(60)]
        r = json.loads(gh_pr_open(json.dumps(_ok_params(files=files))))
        assert r['reason_code'] == 'bad_input'
        assert 'too many' in r['error']

    def test_file_path_traversal(self):
        files = [{'path': '../etc/passwd', 'content': 'x'}]
        r = json.loads(gh_pr_open(json.dumps(_ok_params(files=files))))
        assert r['reason_code'] == 'bad_input'

    def test_file_content_not_str(self):
        files = [{'path': 'a.md', 'content': 123}]
        r = json.loads(gh_pr_open(json.dumps(_ok_params(files=files))))
        assert r['reason_code'] == 'bad_input'

    def test_bad_label(self):
        r = json.loads(gh_pr_open(
            json.dumps(_ok_params(labels=['valid', 'has\nnewline']))))
        assert r['reason_code'] == 'bad_input'


# ── auth + binary availability ─────────────────────────────────────

class TestAuthGate:
    def test_no_token_no_session(self, monkeypatch):
        for k in ('HEVOLVE_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN'):
            monkeypatch.delenv(k, raising=False)
        # Force gh to be "available" so we hit the auth gate, not the
        # gh_not_installed gate.
        monkeypatch.setattr(gh_pr_tool, '_gh_binary', lambda: '/usr/bin/gh')
        r = json.loads(gh_pr_open(json.dumps(_ok_params())))
        assert r['ok'] is False
        assert r['reason_code'] == 'no_token'

    def test_gh_binary_missing(self, monkeypatch):
        monkeypatch.setenv('HEVOLVE_GITHUB_TOKEN', 'fake')
        monkeypatch.setattr(gh_pr_tool, '_gh_binary', lambda: None)
        r = json.loads(gh_pr_open(json.dumps(_ok_params())))
        assert r['ok'] is False
        assert r['reason_code'] == 'gh_not_installed'


# ── subprocess sequence: mocked happy + failure paths ─────────────

class _MockProc:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def _mock_env(monkeypatch, tmp_path):
    monkeypatch.setenv('HEVOLVE_GITHUB_TOKEN', 'fake-token')
    monkeypatch.setattr(gh_pr_tool, '_gh_binary', lambda: '/usr/bin/gh')
    monkeypatch.setattr(gh_pr_tool, '_workdir_root', lambda: tmp_path)
    return tmp_path


class TestSubprocessSequence:
    """Mocked subprocess calls — verifies call order + reason_code map."""

    def test_clone_failure(self, _mock_env, monkeypatch):
        def fake_run(args, **kw):
            return _MockProc(returncode=128,
                             stderr='Repository not found')
        monkeypatch.setattr(subprocess, 'run', fake_run)
        r = json.loads(gh_pr_open(json.dumps(_ok_params())))
        assert r['reason_code'] == 'clone_failed'
        assert 'Repository not found' in r['error']

    def test_happy_path_returns_pr_url(self, _mock_env, monkeypatch):
        # Pre-create the workdir so the clone step's mkdir(exist_ok=False)
        # collision branch isn't hit during the happy path.
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            # All subprocess calls succeed; gh pr create prints URL
            if args[0].endswith('gh') and 'pr' in args and 'create' in args:
                return _MockProc(
                    returncode=0,
                    stdout='https://github.com/owner/repo/pull/42\n',
                )
            return _MockProc(returncode=0, stdout='')

        monkeypatch.setattr(subprocess, 'run', fake_run)
        # Pre-write a fake "cloned" workdir so write/add succeed
        # (clone is mocked so doesn't actually populate it; we mkdir via
        # the tool's own mkdir(exist_ok=False); fine since tmp_path is fresh).

        r = json.loads(gh_pr_open(json.dumps(_ok_params())))
        assert r['ok'] is True, r
        assert r['pr_url'] == 'https://github.com/owner/repo/pull/42'
        assert r['pr_number'] == 42
        assert r['repo'] == 'owner/repo'
        # Verify the canonical sequence was issued:
        cmds = [' '.join(c) for c in calls]
        assert any('repo clone' in c for c in cmds), 'gh repo clone missing'
        assert any('checkout -b' in c for c in cmds), 'git checkout -b missing'
        assert any('add --all' in c for c in cmds), 'git add missing'
        assert any('commit -m' in c for c in cmds), 'git commit missing'
        assert any('push --set-upstream' in c for c in cmds), 'git push missing'
        assert any('pr create' in c for c in cmds), 'gh pr create missing'

    def test_no_changes_classified(self, _mock_env, monkeypatch):
        def fake_run(args, **kw):
            if 'commit' in args:
                return _MockProc(
                    returncode=1,
                    stdout='nothing to commit, working tree clean',
                )
            return _MockProc(returncode=0, stdout='')
        monkeypatch.setattr(subprocess, 'run', fake_run)
        r = json.loads(gh_pr_open(json.dumps(_ok_params())))
        assert r['reason_code'] == 'no_changes'

    def test_push_failure(self, _mock_env, monkeypatch):
        def fake_run(args, **kw):
            if 'push' in args:
                return _MockProc(returncode=128, stderr='auth failed')
            return _MockProc(returncode=0, stdout='')
        monkeypatch.setattr(subprocess, 'run', fake_run)
        r = json.loads(gh_pr_open(json.dumps(_ok_params())))
        assert r['reason_code'] == 'push_failed'

    def test_timeout_classified(self, _mock_env, monkeypatch):
        def fake_run(args, **kw):
            raise subprocess.TimeoutExpired(cmd=args, timeout=60)
        monkeypatch.setattr(subprocess, 'run', fake_run)
        r = json.loads(gh_pr_open(json.dumps(_ok_params())))
        assert r['reason_code'] == 'timeout'


# ── registration shape ────────────────────────────────────────────

class TestRegistration:
    def test_create_tool_info_shape(self):
        info = GhPrTool.create_tool_info()
        assert info.name == 'gh_pr_open'
        assert info.base_url == 'native://in-process'
        assert info.health_endpoint is None
        assert 'gh_pr_open' in info.endpoints
        ep = info.endpoints['gh_pr_open']
        # Mirror Crawl4AITool's contract — these keys MUST be present
        # for ServiceToolRegistry + autogen tool-schema synthesis to work.
        for key in ('path', 'method', 'description',
                    'params_schema', 'native_handler'):
            assert key in ep, f'missing endpoint key: {key}'
        # native_handler must be the real function (registry calls it)
        assert ep['native_handler'] is gh_pr_open
        # params_schema is flat per-param dict (not JSON-Schema wrapper)
        schema = ep['params_schema']
        for required_param in ('repo', 'head_branch', 'title',
                               'body', 'files'):
            assert required_param in schema
            assert 'type' in schema[required_param]

    def test_register_returns_bool(self):
        # We don't care what the registry does internally — just that
        # the helper returns a bool and doesn't raise.  Idempotent in
        # the real registry; here we just check no exception.
        result = GhPrTool.register()
        assert isinstance(result, bool)

    def test_importable_from_service_tools_package(self):
        """P1 wiring: the tool must be exported by the package __init__
        (same as Crawl4AITool) so create_recipe/reuse_recipe can register
        it — otherwise the SEO publishing path stays dormant."""
        import integrations.service_tools as pkg
        assert pkg.GhPrTool is GhPrTool
        assert 'GhPrTool' in pkg.__all__

    def test_registered_in_global_service_tool_registry(self):
        """After register(), the tool is discoverable by name in the
        global registry (the surface create_recipe/reuse_recipe expose
        to the agents)."""
        from integrations.service_tools import service_tool_registry
        GhPrTool.register()
        assert GhPrTool.NAME in service_tool_registry._tools
