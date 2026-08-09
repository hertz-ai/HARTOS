"""
Behavioural tests for integrations/coding_agent/remote_executor.py.

RemoteDesktopExecutor bridges CLI commands to a remote Nunba /execute and
/screenshot endpoint.  execute() runs a security pre-check (destructive-command
classification + DLP/PII scan) BEFORE POSTing the command to the remote host.

The boundary we mock:
  - core.http_pool.pooled_post / pooled_get  (the network) — re-exported into
    the module namespace, so we patch them on the module under test.
  - security.action_classifier / security.dlp_engine, only where a test needs
    to force a specific classification or simulate a degraded (ImportError)
    security stack.  Otherwise the REAL classifier + DLP engine run.

Security focus (why this file exists): the destructive-command / DLP gate must
FAIL SAFE.  If a security module cannot be imported, the command must NOT be
silently forwarded to the remote host — it must be refused (unless the caller
explicitly passes force=True).
"""

import sys
import types
import base64

import pytest

try:
    import requests
    from integrations.coding_agent import remote_executor as rex
    from integrations.coding_agent.remote_executor import RemoteDesktopExecutor
except Exception as e:  # pragma: no cover - environment guard
    pytest.skip(
        f"remote_executor / requests not importable in this env: {e}",
        allow_module_level=True,
    )

from unittest import mock


# ── Boundary helpers ──────────────────────────────────────────────────────────

class _FakeResp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code=200, json_data=None, text='',
                 headers=None, content=b''):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.headers = headers or {}
        self.content = content

    def json(self):
        return self._json


def _exec(url='http://remote-host:6777'):
    return RemoteDesktopExecutor(url)


# ── execute(): destructive-command gate ───────────────────────────────────────

class TestExecuteDestructiveGate:

    def test_destructive_command_blocked_not_dispatched(self):
        """A destructive command is blocked and NEVER POSTed to the remote."""
        with mock.patch.object(rex, 'pooled_post') as post:
            result = _exec().execute('rm -rf /')
        assert result['success'] is False
        assert 'Destructive' in result['error']
        post.assert_not_called()

    def test_safe_command_is_dispatched(self):
        """A read-only command passes the gate and is dispatched."""
        resp = _FakeResp(200, {'returncode': 0, 'output': 'a\nb\n'})
        with mock.patch.object(rex, 'pooled_post', return_value=resp) as post:
            result = _exec().execute('ls -la')
        assert result['success'] is True
        assert result['output'] == 'a\nb\n'
        assert result['returncode'] == 0
        post.assert_called_once()

    def test_force_bypasses_destructive_gate(self):
        """force=True is the ONLY bypass: even 'rm -rf /' is dispatched."""
        resp = _FakeResp(200, {'returncode': 0, 'output': ''})
        with mock.patch.object(rex, 'pooled_post', return_value=resp) as post:
            result = _exec().execute('rm -rf /', force=True)
        post.assert_called_once()
        assert result['success'] is True

    def test_force_also_bypasses_dlp_gate(self):
        """force=True skips the DLP/PII scan as well."""
        resp = _FakeResp(200, {'returncode': 0, 'output': ''})
        with mock.patch.object(rex, 'pooled_post', return_value=resp) as post:
            _exec().execute('echo ssn 123-45-6789', force=True)
        post.assert_called_once()


# ── execute(): DLP gate ───────────────────────────────────────────────────────

class TestExecuteDlpGate:

    def test_pii_command_blocked_by_dlp(self):
        """A non-destructive command carrying PII is blocked by DLP."""
        with mock.patch.object(rex, 'pooled_post') as post:
            result = _exec().execute('echo user ssn 123-45-6789')
        assert result['success'] is False
        assert 'DLP blocked' in result['error']
        post.assert_not_called()


# ── execute(): FAIL-SAFE degrade path (the security bug this file guards) ─────

class TestSecurityDegradeFailSafe:
    """When a security module can't be imported the gate must FAIL SAFE:
    refuse to dispatch rather than silently forwarding an unchecked command."""

    def test_action_classifier_import_failure_blocks_dispatch(self):
        # Setting the module to None in sys.modules makes
        # `from security.action_classifier import classify_action` raise
        # ImportError — simulating a broken/absent security stack.
        with mock.patch.dict(sys.modules,
                             {'security.action_classifier': None}):
            with mock.patch.object(rex, 'pooled_post') as post:
                result = _exec().execute('rm -rf /')
        assert result['success'] is False, (
            "destructive command was dispatched despite the classifier being "
            "unavailable — the gate is fail-OPEN")
        post.assert_not_called()
        assert 'error' in result and result['error']

    def test_dlp_import_failure_blocks_dispatch(self):
        # action_classifier works (command classifies as non-destructive),
        # but the DLP engine can't be imported → must refuse, not forward.
        with mock.patch.dict(sys.modules, {'security.dlp_engine': None}):
            with mock.patch.object(rex, 'pooled_post') as post:
                result = _exec().execute('ls -la')
        assert result['success'] is False, (
            "command dispatched despite DLP engine being unavailable — "
            "the gate is fail-OPEN")
        post.assert_not_called()

    def test_degrade_path_still_honors_force(self):
        """Even with a broken security stack, force=True dispatches."""
        resp = _FakeResp(200, {'returncode': 0, 'output': ''})
        with mock.patch.dict(sys.modules,
                             {'security.action_classifier': None,
                              'security.dlp_engine': None}):
            with mock.patch.object(rex, 'pooled_post',
                                   return_value=resp) as post:
                result = _exec().execute('rm -rf /', force=True)
        post.assert_called_once()
        assert result['success'] is True


# ── execute(): HTTP / network behaviour ───────────────────────────────────────

class TestExecuteHttp:

    def test_nonzero_returncode_is_failure(self):
        resp = _FakeResp(200, {'returncode': 2, 'output': 'boom'})
        with mock.patch.object(rex, 'pooled_post', return_value=resp):
            result = _exec().execute('ls /nope')
        assert result['success'] is False
        assert result['returncode'] == 2
        assert result['output'] == 'boom'

    def test_missing_returncode_defaults_to_failure(self):
        # No returncode in the response → treated as non-zero (1) → failure.
        resp = _FakeResp(200, {'output': 'x'})
        with mock.patch.object(rex, 'pooled_post', return_value=resp):
            result = _exec().execute('ls')
        assert result['success'] is False
        assert result['returncode'] == -1

    def test_http_error_status_surfaces_code_and_body(self):
        resp = _FakeResp(500, text='internal error ' * 40)
        with mock.patch.object(rex, 'pooled_post', return_value=resp):
            result = _exec().execute('ls')
        assert result['success'] is False
        assert 'HTTP 500' in result['error']
        # body is truncated to 200 chars
        assert len(result['error']) <= len('HTTP 500: ') + 200

    def test_connection_error_is_friendly(self):
        with mock.patch.object(rex, 'pooled_post',
                               side_effect=requests.ConnectionError('down')):
            result = _exec('http://h:6777').execute('ls')
        assert result['success'] is False
        assert 'Cannot connect' in result['error']
        assert 'http://h:6777' in result['error']

    def test_timeout_is_reported(self):
        with mock.patch.object(rex, 'pooled_post',
                               side_effect=requests.Timeout('slow')):
            result = _exec().execute('ls', timeout=7)
        assert result['success'] is False
        assert 'timed out' in result['error']
        assert '7s' in result['error']

    def test_generic_exception_is_captured(self):
        with mock.patch.object(rex, 'pooled_post',
                               side_effect=ValueError('weird')):
            result = _exec().execute('ls')
        assert result['success'] is False
        assert result['error'] == 'weird'

    def test_execute_posts_command_and_timeout(self):
        resp = _FakeResp(200, {'returncode': 0, 'output': ''})
        with mock.patch.object(rex, 'pooled_post', return_value=resp) as post:
            _exec('http://host:6777').execute('ls', timeout=30)
        args, kwargs = post.call_args
        assert args[0] == 'http://host:6777/execute'
        assert kwargs['json'] == {'command': 'ls', 'timeout': 30}
        # network timeout gets a +10s cushion over the exec timeout
        assert kwargs['timeout'] == 40


# ── screenshot() ──────────────────────────────────────────────────────────────

class TestScreenshot:

    def test_json_response_returns_image_field(self):
        resp = _FakeResp(200, {'image': 'QUJD'},
                         headers={'Content-Type': 'application/json'})
        with mock.patch.object(rex, 'pooled_get', return_value=resp):
            result = _exec().screenshot()
        assert result['success'] is True
        assert result['image_base64'] == 'QUJD'
        assert result['content_type'] == 'image/png'

    def test_json_response_screenshot_alias(self):
        resp = _FakeResp(200, {'screenshot': 'WFla'},
                         headers={'Content-Type': 'application/json'})
        with mock.patch.object(rex, 'pooled_get', return_value=resp):
            result = _exec().screenshot()
        assert result['image_base64'] == 'WFla'

    def test_binary_response_is_base64_encoded(self):
        raw = b'\x89PNG\r\n\x1a\nDATA'
        resp = _FakeResp(200, content=raw,
                         headers={'Content-Type': 'image/png'})
        with mock.patch.object(rex, 'pooled_get', return_value=resp):
            result = _exec().screenshot()
        assert result['success'] is True
        assert base64.b64decode(result['image_base64']) == raw
        assert result['content_type'] == 'image/png'

    def test_http_error(self):
        resp = _FakeResp(404, text='not found')
        with mock.patch.object(rex, 'pooled_get', return_value=resp):
            result = _exec().screenshot()
        assert result['success'] is False
        assert 'HTTP 404' in result['error']

    def test_connection_error(self):
        with mock.patch.object(rex, 'pooled_get',
                               side_effect=requests.ConnectionError('x')):
            result = _exec('http://h:6777').screenshot()
        assert result['success'] is False
        assert 'Cannot connect' in result['error']

    def test_generic_exception(self):
        with mock.patch.object(rex, 'pooled_get',
                               side_effect=RuntimeError('kaboom')):
            result = _exec().screenshot()
        assert result['success'] is False
        assert result['error'] == 'kaboom'


# ── execute_desktop_task() ────────────────────────────────────────────────────

class TestExecuteDesktopTask:

    def test_remote_target_delegates_to_execute(self):
        resp = _FakeResp(200, {'returncode': 0, 'output': 'ok'})
        with mock.patch.object(rex, 'pooled_post', return_value=resp) as post:
            result = _exec().execute_desktop_task(
                'open notepad', target='remote',
                nunba_url='http://remote:9999')
        post.assert_called_once()
        assert post.call_args.args[0] == 'http://remote:9999/execute'
        assert result['success'] is True

    def test_local_target_import_error_degrades_cleanly(self):
        with mock.patch.dict(sys.modules,
                             {'integrations.vlm.local_loop': None}):
            result = _exec().execute_desktop_task('open chrome', target='local')
        assert result['success'] is False
        assert 'VLM pipeline not available' in result['error']

    def test_local_target_success(self):
        fake = types.ModuleType('integrations.vlm.local_loop')
        fake.run_local_agentic_loop = lambda instruction: {'steps': 2,
                                                            'instr': instruction}
        with mock.patch.dict(sys.modules,
                             {'integrations.vlm.local_loop': fake}):
            result = _exec().execute_desktop_task('do thing', target='local')
        assert result['success'] is True
        assert 'do thing' in result['output']  # json.dumps of the dict

    def test_local_target_runtime_exception_captured(self):
        fake = types.ModuleType('integrations.vlm.local_loop')

        def _boom(instruction):
            raise RuntimeError('vlm exploded')

        fake.run_local_agentic_loop = _boom
        with mock.patch.dict(sys.modules,
                             {'integrations.vlm.local_loop': fake}):
            result = _exec().execute_desktop_task('do thing', target='local')
        assert result['success'] is False
        assert result['error'] == 'vlm exploded'


# ── constructor / _check_security direct ──────────────────────────────────────

class TestConstructionAndCheckSecurity:

    def test_base_url_strips_trailing_slash(self):
        assert _exec('http://host:1234/').base_url == 'http://host:1234'

    def test_default_url_uses_port_registry(self):
        ex = RemoteDesktopExecutor()
        assert ex.base_url.startswith('http://localhost:')

    def test_check_security_none_for_safe_command(self):
        assert _exec()._check_security('ls -la') is None

    def test_check_security_blocks_destructive(self):
        blocked = _exec()._check_security('DROP TABLE users')
        assert blocked is not None
        assert blocked['success'] is False

    def test_check_security_none_command_does_not_crash(self):
        # None / empty must not raise (classify_action + dlp treat as unknown).
        assert _exec()._check_security(None) is None
        assert _exec()._check_security('') is None
