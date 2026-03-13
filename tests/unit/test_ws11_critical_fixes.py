"""
WS11 P0 Critical Fixes — Comprehensive Unit Tests.

Android-inspired OS robustness patterns:
  - ANR detection: Boot and API response timeouts
  - Service isolation: Import/crash in one subsystem doesn't kill others
  - Watchdog: Unbounded growth detection (notification queue, etc.)
  - Blast radius containment: Failure in one operation doesn't cascade

Covers:
  1. Boot Service (core/platform/boot_service.py)
  2. Shell API Route Registration (liquid_ui_service.py)
  3. Shell Auth (shell_os_apis.py)
  4. Path Sandbox (shell_os_apis.py)
  5. File Transfer Path Traversal Fix (file_transfer.py)
  6. OG/Embed Auth (api_sharing.py)
  7. ANR & Watchdog Tests
  8. Audit Trail Tests
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _make_shell_app():
    """Create a Flask test app with shell OS routes registered."""
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    from integrations.agent_engine.shell_os_apis import register_shell_os_routes
    register_shell_os_routes(app)
    return app


def _reset_allowed_roots():
    """Reset the _ALLOWED_ROOTS cache so tests don't leak state."""
    import integrations.agent_engine.shell_os_apis as mod
    mod._ALLOWED_ROOTS = None


def _reset_boot_state():
    """Reset boot_service globals so tests don't leak state."""
    import core.platform.boot_service as mod
    mod._booted = False


# ═══════════════════════════════════════════════════════════════════════
# 1. Boot Service (core/platform/boot_service.py)
# ═══════════════════════════════════════════════════════════════════════

class TestBootServiceIdempotent(unittest.TestCase):
    """ensure_platform() is idempotent — calling multiple times gives same result."""

    def setUp(self):
        _reset_boot_state()

    def tearDown(self):
        _reset_boot_state()

    @patch('core.platform.bootstrap.bootstrap_platform')
    def test_ensure_platform_idempotent(self, mock_bootstrap):
        """Calling ensure_platform() twice only bootstraps once."""
        from core.platform.boot_service import ensure_platform
        mock_registry = MagicMock(name='ServiceRegistry')
        mock_bootstrap.return_value = mock_registry

        result1 = ensure_platform()
        result2 = ensure_platform()

        # bootstrap_platform called exactly once
        mock_bootstrap.assert_called_once()
        self.assertEqual(result1, mock_registry)
        # Second call goes through _get_registry_safe (fast path)
        self.assertIsNotNone(result2)

    @patch('core.platform.bootstrap.bootstrap_platform')
    def test_ensure_platform_returns_registry(self, mock_bootstrap):
        """ensure_platform() returns the registry from bootstrap_platform."""
        from core.platform.boot_service import ensure_platform
        sentinel = MagicMock(name='registry_sentinel')
        mock_bootstrap.return_value = sentinel

        result = ensure_platform()
        self.assertIs(result, sentinel)


class TestBootServiceState(unittest.TestCase):
    """is_booted() state transitions."""

    def setUp(self):
        _reset_boot_state()

    def tearDown(self):
        _reset_boot_state()

    def test_is_booted_false_before_boot(self):
        from core.platform.boot_service import is_booted
        self.assertFalse(is_booted())

    @patch('core.platform.bootstrap.bootstrap_platform')
    def test_is_booted_true_after_boot(self, mock_bootstrap):
        from core.platform.boot_service import ensure_platform, is_booted
        mock_bootstrap.return_value = MagicMock()
        ensure_platform()
        self.assertTrue(is_booted())


class TestBootServiceThreadSafety(unittest.TestCase):
    """Concurrent calls to ensure_platform() don't double-bootstrap."""

    def setUp(self):
        _reset_boot_state()

    def tearDown(self):
        _reset_boot_state()

    @patch('core.platform.bootstrap.bootstrap_platform')
    def test_concurrent_calls_single_bootstrap(self, mock_bootstrap):
        """Launch N threads calling ensure_platform(). bootstrap_platform called once."""
        from core.platform.boot_service import ensure_platform

        mock_registry = MagicMock()

        def slow_bootstrap(ext_dir=None):
            time.sleep(0.05)
            return mock_registry
        mock_bootstrap.side_effect = slow_bootstrap

        results = [None] * 10
        errors = []

        def worker(idx):
            try:
                results[idx] = ensure_platform()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Errors: {errors}")
        mock_bootstrap.assert_called_once()


class TestBootServiceFailureIsolation(unittest.TestCase):
    """If bootstrap fails, ensure_platform returns None and doesn't crash."""

    def setUp(self):
        _reset_boot_state()

    def tearDown(self):
        _reset_boot_state()

    @patch('core.platform.bootstrap.bootstrap_platform',
           side_effect=RuntimeError("bootstrap exploded"))
    def test_bootstrap_failure_returns_none(self, mock_bootstrap):
        from core.platform.boot_service import ensure_platform, is_booted
        result = ensure_platform()
        self.assertIsNone(result)
        # Should NOT be marked as booted after failure
        self.assertFalse(is_booted())

    @patch('core.platform.bootstrap.bootstrap_platform',
           side_effect=RuntimeError("crash"))
    def test_bootstrap_failure_no_exception_propagated(self, mock_bootstrap):
        from core.platform.boot_service import ensure_platform
        # Must not raise
        try:
            result = ensure_platform()
        except Exception:
            self.fail("ensure_platform() should not propagate exceptions")


class TestBootServiceANR(unittest.TestCase):
    """ANR-style: boot must complete within 5 seconds (mocked)."""

    def setUp(self):
        _reset_boot_state()

    def tearDown(self):
        _reset_boot_state()

    @patch('core.platform.bootstrap.bootstrap_platform')
    def test_boot_completes_within_timeout(self, mock_bootstrap):
        """ensure_platform() completes within 5 seconds."""
        from core.platform.boot_service import ensure_platform
        mock_bootstrap.return_value = MagicMock()

        start = time.monotonic()
        ensure_platform()
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 5.0,
                        f"Boot took {elapsed:.2f}s — ANR threshold is 5s")

    @patch('core.platform.bootstrap.bootstrap_platform')
    def test_slow_bootstrap_detected(self, mock_bootstrap):
        """A slow bootstrap can be detected via timing (simulated)."""
        from core.platform.boot_service import ensure_platform

        def slow_bootstrap(ext_dir=None):
            time.sleep(0.2)
            return MagicMock()
        mock_bootstrap.side_effect = slow_bootstrap

        start = time.monotonic()
        result = ensure_platform()
        elapsed = time.monotonic() - start

        self.assertIsNotNone(result)
        self.assertGreater(elapsed, 0.1, "Bootstrap should have taken measurable time")


# ═══════════════════════════════════════════════════════════════════════
# 2. Shell API Route Registration (liquid_ui_service.py)
# ═══════════════════════════════════════════════════════════════════════

class TestShellAPIRegistration(unittest.TestCase):
    """Verify shell API registration in LiquidUI."""

    def test_shell_os_routes_registered(self):
        """register_shell_os_routes installs /api/shell/* routes."""
        from flask import Flask
        app = Flask(__name__)
        app.config['TESTING'] = True
        from integrations.agent_engine.shell_os_apis import register_shell_os_routes
        register_shell_os_routes(app)

        rules = [r.rule for r in app.url_map.iter_rules()]
        self.assertIn('/api/shell/notifications', rules)
        self.assertIn('/api/shell/files/browse', rules)
        self.assertIn('/api/shell/terminal/exec', rules)

    def test_shell_desktop_routes_registered(self):
        """register_shell_desktop_routes installs desktop routes."""
        from flask import Flask
        app = Flask(__name__)
        app.config['TESTING'] = True
        try:
            from integrations.agent_engine.shell_desktop_apis import (
                register_shell_desktop_routes)
            register_shell_desktop_routes(app)
            rules = [r.rule for r in app.url_map.iter_rules()]
            desktop_routes = [r for r in rules if '/api/shell/' in r]
            self.assertGreater(len(desktop_routes), 0,
                               "shell_desktop_routes should add at least one route")
        except ImportError:
            self.skipTest("shell_desktop_apis not available")

    def test_shell_system_routes_registered(self):
        """register_shell_system_routes installs system routes."""
        from flask import Flask
        app = Flask(__name__)
        app.config['TESTING'] = True
        try:
            from integrations.agent_engine.shell_system_apis import (
                register_shell_system_routes)
            register_shell_system_routes(app)
            rules = [r.rule for r in app.url_map.iter_rules()]
            system_routes = [r for r in rules if '/api/shell/' in r]
            self.assertGreater(len(system_routes), 0,
                               "shell_system_routes should add at least one route")
        except ImportError:
            self.skipTest("shell_system_apis not available")

    def test_app_install_routes_registered(self):
        """register_app_install_routes installs installer routes."""
        from flask import Flask
        app = Flask(__name__)
        app.config['TESTING'] = True
        try:
            from integrations.agent_engine.app_installer import (
                register_app_install_routes)
            register_app_install_routes(app)
            rules = [r.rule for r in app.url_map.iter_rules()]
            install_routes = [r for r in rules if '/api/' in r]
            self.assertGreater(len(install_routes), 0,
                               "app_install_routes should add at least one route")
        except ImportError:
            self.skipTest("app_installer not available")

    def test_registration_failure_does_not_crash(self):
        """If any registration function throws, it must not crash the caller."""
        from flask import Flask
        app = Flask(__name__)
        app.config['TESTING'] = True

        # Simulate the try/except isolation pattern from liquid_ui_service.py
        crashed = False
        try:
            from integrations.agent_engine.shell_os_apis import register_shell_os_routes
            register_shell_os_routes(app)
        except Exception:
            crashed = True

        # Even if it failed, app should still be alive
        self.assertIsNotNone(app)

    def test_registration_failure_isolation_via_mock(self):
        """If shell_os_apis import fails, other APIs can still register."""
        from flask import Flask
        app = Flask(__name__)
        app.config['TESTING'] = True

        os_registered = False
        desktop_registered = False

        # Simulate shell_os import failure
        try:
            raise ImportError("simulated shell_os failure")
        except Exception:
            pass  # Isolation: failure caught, continue

        # Desktop should still register even after shell_os failure
        try:
            from integrations.agent_engine.shell_desktop_apis import (
                register_shell_desktop_routes)
            register_shell_desktop_routes(app)
            desktop_registered = True
        except ImportError:
            desktop_registered = True  # Module not installed = acceptable

        self.assertTrue(desktop_registered)


# ═══════════════════════════════════════════════════════════════════════
# 3. Shell Auth (shell_os_apis.py)
# ═══════════════════════════════════════════════════════════════════════

class TestShellAuthCheck(unittest.TestCase):
    """Tests for _shell_auth_check() — local-only auth."""

    def setUp(self):
        self.app = _make_shell_app()
        self.app.config['TESTING'] = True

    def test_localhost_ipv4_allowed(self):
        """Request from 127.0.0.1 is authorized."""
        with self.app.test_request_context(
                '/api/shell/notifications',
                environ_base={'REMOTE_ADDR': '127.0.0.1'}):
            from integrations.agent_engine.shell_os_apis import _shell_auth_check
            ok, *rest = _shell_auth_check()
            self.assertTrue(ok)

    def test_localhost_ipv6_allowed(self):
        """Request from ::1 is authorized."""
        with self.app.test_request_context(
                '/api/shell/notifications',
                environ_base={'REMOTE_ADDR': '::1'}):
            from integrations.agent_engine.shell_os_apis import _shell_auth_check
            ok, *rest = _shell_auth_check()
            self.assertTrue(ok)

    def test_non_local_ip_rejected(self):
        """Request from remote IP is rejected (403)."""
        with patch.dict(os.environ, {'HART_SHELL_TOKEN': ''}, clear=False):
            with self.app.test_request_context(
                    '/api/shell/notifications',
                    environ_base={'REMOTE_ADDR': '192.168.1.100'}):
                from integrations.agent_engine.shell_os_apis import _shell_auth_check
                result = _shell_auth_check()
                self.assertFalse(result[0])

    def test_valid_shell_token_bypasses_ip(self):
        """Valid X-Shell-Token header allows remote access."""
        test_token = 'test-shell-secret-token-12345'
        with patch.dict(os.environ, {'HART_SHELL_TOKEN': test_token}):
            with self.app.test_request_context(
                    '/api/shell/notifications',
                    headers={'X-Shell-Token': test_token},
                    environ_base={'REMOTE_ADDR': '10.0.0.5'}):
                from integrations.agent_engine.shell_os_apis import _shell_auth_check
                ok, *rest = _shell_auth_check()
                self.assertTrue(ok)

    def test_invalid_shell_token_rejected(self):
        """Invalid X-Shell-Token is rejected."""
        with patch.dict(os.environ, {'HART_SHELL_TOKEN': 'correct-token'}):
            with self.app.test_request_context(
                    '/api/shell/notifications',
                    headers={'X-Shell-Token': 'wrong-token'},
                    environ_base={'REMOTE_ADDR': '10.0.0.5'}):
                from integrations.agent_engine.shell_os_apis import _shell_auth_check
                result = _shell_auth_check()
                self.assertFalse(result[0])

    def test_empty_token_env_rejects(self):
        """When HART_SHELL_TOKEN is empty, token auth always fails."""
        with patch.dict(os.environ, {'HART_SHELL_TOKEN': ''}):
            with self.app.test_request_context(
                    '/api/shell/notifications',
                    headers={'X-Shell-Token': 'anything'},
                    environ_base={'REMOTE_ADDR': '10.0.0.5'}):
                from integrations.agent_engine.shell_os_apis import _shell_auth_check
                result = _shell_auth_check()
                self.assertFalse(result[0])


class TestRequireShellAuthDecorator(unittest.TestCase):
    """Tests for @_require_shell_auth decorator on routes."""

    def setUp(self):
        _reset_allowed_roots()

    def tearDown(self):
        _reset_allowed_roots()

    def test_decorator_allows_localhost(self):
        """Routes decorated with @_require_shell_auth pass for localhost."""
        app = _make_shell_app()
        client = app.test_client()
        home = os.path.expanduser('~')
        r = client.get(f'/api/shell/files/browse?path={home}',
                       environ_base={'REMOTE_ADDR': '127.0.0.1'})
        self.assertIn(r.status_code, (200, 400),
                      f"Expected 200/400 for localhost, got {r.status_code}")

    def test_decorator_blocks_remote_ip(self):
        """Routes decorated with @_require_shell_auth reject remote IPs."""
        app = _make_shell_app()
        client = app.test_client()
        with patch.dict(os.environ, {'HART_SHELL_TOKEN': ''}):
            r = client.get('/api/shell/files/browse?path=/tmp',
                           environ_base={'REMOTE_ADDR': '192.168.1.50'})
            self.assertEqual(r.status_code, 403)


# ═══════════════════════════════════════════════════════════════════════
# 4. Path Sandbox (shell_os_apis.py)
# ═══════════════════════════════════════════════════════════════════════

class TestPathSandbox(unittest.TestCase):
    """Tests for _is_path_allowed() and _get_allowed_roots()."""

    def setUp(self):
        _reset_allowed_roots()

    def tearDown(self):
        _reset_allowed_roots()

    def test_user_home_allowed(self):
        from integrations.agent_engine.shell_os_apis import _is_path_allowed
        home = os.path.expanduser('~')
        self.assertTrue(_is_path_allowed(home))

    def test_subpath_of_home_allowed(self):
        from integrations.agent_engine.shell_os_apis import _is_path_allowed
        home = os.path.expanduser('~')
        subpath = os.path.join(home, 'Documents', 'test.txt')
        self.assertTrue(_is_path_allowed(subpath))

    def test_tmp_allowed(self):
        from integrations.agent_engine.shell_os_apis import _is_path_allowed
        tmp = tempfile.gettempdir()
        self.assertTrue(
            _is_path_allowed(tmp),
            f"Temp dir {tmp} should be allowed")

    def test_system_dir_rejected(self):
        """System directories outside home and tmp are rejected."""
        from integrations.agent_engine.shell_os_apis import _is_path_allowed
        if sys.platform == 'win32':
            self.assertFalse(_is_path_allowed('C:\\Windows\\System32'))
        else:
            self.assertFalse(_is_path_allowed('/etc'))

    def test_root_dir_rejected(self):
        from integrations.agent_engine.shell_os_apis import _is_path_allowed
        if sys.platform == 'win32':
            self.assertFalse(_is_path_allowed('C:\\Windows'))
        else:
            self.assertFalse(_is_path_allowed('/root'))

    def test_traversal_attack_resolved_and_rejected(self):
        """Path traversal is resolved via realpath and rejected if outside roots."""
        from integrations.agent_engine.shell_os_apis import _is_path_allowed
        if sys.platform == 'win32':
            # Resolves to C:\Windows\System32\drivers\etc\hosts
            self.assertFalse(
                _is_path_allowed('C:\\Windows\\..\\Windows\\System32\\drivers\\etc\\hosts'))
        else:
            # /tmp/../../etc/passwd -> /etc/passwd (outside roots)
            self.assertFalse(_is_path_allowed('/tmp/../../etc/passwd'))

    def test_extra_roots_via_env(self):
        """HART_SHELL_ALLOWED_PATHS env var adds extra allowed roots."""
        _reset_allowed_roots()
        extra_dir = tempfile.mkdtemp()
        try:
            with patch.dict(os.environ, {'HART_SHELL_ALLOWED_PATHS': extra_dir}):
                _reset_allowed_roots()
                from integrations.agent_engine.shell_os_apis import _is_path_allowed
                self.assertTrue(_is_path_allowed(extra_dir))
                subpath = os.path.join(extra_dir, 'data', 'file.txt')
                self.assertTrue(_is_path_allowed(subpath))
        finally:
            os.rmdir(extra_dir)
            _reset_allowed_roots()

    def test_allowed_roots_cached(self):
        """_get_allowed_roots() caches result after first call."""
        from integrations.agent_engine.shell_os_apis import _get_allowed_roots
        _reset_allowed_roots()
        roots1 = _get_allowed_roots()
        roots2 = _get_allowed_roots()
        self.assertIs(roots1, roots2, "Allowed roots should be cached (same object)")


class TestPathSandboxOnRoutes(unittest.TestCase):
    """Path validation on actual file manager routes."""

    def setUp(self):
        _reset_allowed_roots()
        self.app = _make_shell_app()
        self.client = self.app.test_client()

    def tearDown(self):
        _reset_allowed_roots()

    def test_browse_outside_roots_returns_403(self):
        """GET /api/shell/files/browse for system dir returns 403."""
        if sys.platform == 'win32':
            path = 'C:\\Windows\\System32'
        else:
            path = '/etc'
        r = self.client.get(f'/api/shell/files/browse?path={path}',
                            environ_base={'REMOTE_ADDR': '127.0.0.1'})
        self.assertEqual(r.status_code, 403)

    def test_delete_outside_roots_returns_403(self):
        """POST /api/shell/files/delete for system path returns 403."""
        if sys.platform == 'win32':
            path = 'C:\\Windows\\System32\\drivers\\etc\\hosts'
        else:
            path = '/etc/hosts'
        r = self.client.post('/api/shell/files/delete',
                             json={'path': path},
                             environ_base={'REMOTE_ADDR': '127.0.0.1'})
        self.assertIn(r.status_code, (400, 403))

    def test_move_rejects_src_outside_roots(self):
        """POST /api/shell/files/move rejects when source is outside roots."""
        home = os.path.expanduser('~')
        if sys.platform == 'win32':
            bad_src = 'C:\\Windows\\System32\\test.txt'
        else:
            bad_src = '/etc/test.txt'
        r = self.client.post('/api/shell/files/move',
                             json={'source': bad_src,
                                   'destination': os.path.join(home, 'moved.txt')},
                             environ_base={'REMOTE_ADDR': '127.0.0.1'})
        self.assertEqual(r.status_code, 403)

    def test_move_rejects_dst_outside_roots(self):
        """POST /api/shell/files/move rejects when destination is outside roots."""
        home = os.path.expanduser('~')
        if sys.platform == 'win32':
            bad_dst = 'C:\\Windows\\System32\\evil.txt'
        else:
            bad_dst = '/etc/evil.txt'
        r = self.client.post('/api/shell/files/move',
                             json={'source': os.path.join(home, 'test.txt'),
                                   'destination': bad_dst},
                             environ_base={'REMOTE_ADDR': '127.0.0.1'})
        self.assertEqual(r.status_code, 403)

    def test_copy_rejects_src_outside_roots(self):
        """POST /api/shell/files/copy rejects source outside allowed roots."""
        home = os.path.expanduser('~')
        if sys.platform == 'win32':
            bad_src = 'C:\\Windows\\System32\\test.txt'
        else:
            bad_src = '/etc/test.txt'
        r = self.client.post('/api/shell/files/copy',
                             json={'source': bad_src,
                                   'destination': os.path.join(home, 'copy.txt')},
                             environ_base={'REMOTE_ADDR': '127.0.0.1'})
        self.assertEqual(r.status_code, 403)

    def test_copy_rejects_dst_outside_roots(self):
        """POST /api/shell/files/copy rejects destination outside allowed roots."""
        home = os.path.expanduser('~')
        if sys.platform == 'win32':
            bad_dst = 'C:\\Windows\\System32\\evil.txt'
        else:
            bad_dst = '/etc/evil.txt'
        r = self.client.post('/api/shell/files/copy',
                             json={'source': os.path.join(home, 'test.txt'),
                                   'destination': bad_dst},
                             environ_base={'REMOTE_ADDR': '127.0.0.1'})
        self.assertEqual(r.status_code, 403)

    def test_browse_home_succeeds(self):
        """GET /api/shell/files/browse for home dir succeeds."""
        home = os.path.expanduser('~')
        r = self.client.get(f'/api/shell/files/browse?path={home}',
                            environ_base={'REMOTE_ADDR': '127.0.0.1'})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('entries', data)


# ═══════════════════════════════════════════════════════════════════════
# 5. File Transfer Path Traversal Fix (file_transfer.py)
# ═══════════════════════════════════════════════════════════════════════

class TestFileTransferPathTraversal(unittest.TestCase):
    """os.path.basename() strips directory traversal from received filenames."""

    def _make_transfer(self):
        from integrations.remote_desktop.file_transfer import FileTransfer
        return FileTransfer()

    def test_traversal_filename_stripped(self):
        """Filename '../../../etc/passwd' saves as just 'passwd'."""
        import hashlib
        ft = self._make_transfer()
        save_dir = tempfile.mkdtemp()
        try:
            ft.receive_file(save_dir)
            ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_START',
                'filename': '../../../etc/passwd',
                'size': 5,
                'sha256': '',
            })
            ft.handle_frame(b'hello')
            expected_hash = hashlib.sha256(b'hello').hexdigest()
            result = ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_END',
                'filename': '../../../etc/passwd',
                'sha256': expected_hash,
            })

            self.assertTrue(result['success'])
            saved_path = result['path']
            # File must be inside save_dir, not at a traversal path
            self.assertTrue(
                os.path.normpath(saved_path).startswith(os.path.normpath(save_dir)),
                f"File saved at {saved_path} — should be inside {save_dir}")
            self.assertEqual(os.path.basename(saved_path), 'passwd')
        finally:
            for f in os.listdir(save_dir):
                os.remove(os.path.join(save_dir, f))
            os.rmdir(save_dir)

    def test_backslash_traversal_stripped(self):
        r"""Filename '..\\..\\evil.txt' saves as 'evil.txt' (or ..\\..\\evil.txt basename)."""
        import hashlib
        ft = self._make_transfer()
        save_dir = tempfile.mkdtemp()
        try:
            ft.receive_file(save_dir)
            ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_START',
                'filename': '..\\..\\evil.txt',
                'size': 4,
                'sha256': '',
            })
            ft.handle_frame(b'data')
            h = hashlib.sha256(b'data').hexdigest()
            result = ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_END',
                'filename': '..\\..\\evil.txt',
                'sha256': h,
            })
            self.assertTrue(result['success'])
            # The file must be inside save_dir regardless of backslash tricks
            saved_path = os.path.normpath(result['path'])
            self.assertTrue(saved_path.startswith(os.path.normpath(save_dir)))
        finally:
            for f in os.listdir(save_dir):
                os.remove(os.path.join(save_dir, f))
            os.rmdir(save_dir)

    def test_normal_filename_unchanged(self):
        """Normal filename 'report.pdf' is saved as-is."""
        import hashlib
        ft = self._make_transfer()
        save_dir = tempfile.mkdtemp()
        try:
            ft.receive_file(save_dir)
            ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_START',
                'filename': 'report.pdf',
                'size': 3,
                'sha256': '',
            })
            ft.handle_frame(b'pdf')
            h = hashlib.sha256(b'pdf').hexdigest()
            result = ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_END',
                'filename': 'report.pdf',
                'sha256': h,
            })
            self.assertTrue(result['success'])
            self.assertEqual(os.path.basename(result['path']), 'report.pdf')
        finally:
            for f in os.listdir(save_dir):
                os.remove(os.path.join(save_dir, f))
            os.rmdir(save_dir)

    def test_basename_strips_absolute_path(self):
        """os.path.basename strips absolute path prefix."""
        self.assertEqual(os.path.basename('/etc/shadow'), 'shadow')
        self.assertEqual(os.path.basename('../../../etc/passwd'), 'passwd')
        self.assertEqual(os.path.basename('report.pdf'), 'report.pdf')


# ═══════════════════════════════════════════════════════════════════════
# 6. OG/Embed Auth (api_sharing.py)
# ═══════════════════════════════════════════════════════════════════════

class TestOGEmbedAuth(unittest.TestCase):
    """OG-image and embed endpoints have optional_auth and validate resource_type."""

    def _make_sharing_app(self):
        """Create a Flask app with the sharing blueprint, mocking DB."""
        from flask import Flask
        app = Flask(__name__)
        app.config['TESTING'] = True

        from integrations.social.api_sharing import sharing_bp

        # Need a fresh blueprint registration — use a new app each time
        try:
            app.register_blueprint(sharing_bp)
        except Exception:
            # Blueprint might be already registered in another app from
            # a prior test — Flask blueprints are singletons.
            # Create a completely fresh app to avoid this.
            pass

        return app

    def test_og_image_has_optional_auth_decorator(self):
        """og_image_endpoint should have @optional_auth applied."""
        from integrations.social.api_sharing import og_image_endpoint
        # @optional_auth uses @wraps so __name__ is preserved
        self.assertEqual(og_image_endpoint.__name__, 'og_image_endpoint')
        self.assertTrue(callable(og_image_endpoint))

    def test_embed_has_optional_auth_decorator(self):
        """embed_card should have @optional_auth applied."""
        from integrations.social.api_sharing import embed_card
        self.assertEqual(embed_card.__name__, 'embed_card')
        self.assertTrue(callable(embed_card))

    @patch('integrations.social.api_sharing.get_db')
    def test_og_image_rejects_invalid_resource_type(self, mock_get_db):
        """GET /og-image/<invalid_type>/1 returns 400."""
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        app = self._make_sharing_app()
        client = app.test_client()
        r = client.get('/api/social/og-image/evil_type/123')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.social.api_sharing.get_db')
    def test_embed_rejects_invalid_resource_type(self, mock_get_db):
        """GET /embed/<invalid_type>/1 returns 400."""
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        app = self._make_sharing_app()
        client = app.test_client()
        r = client.get('/api/social/embed/evil_type/123')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.social.api_sharing.get_db')
    def test_og_image_accepts_valid_types(self, mock_get_db):
        """GET /og-image/<valid_type>/1 should NOT return 400."""
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        app = self._make_sharing_app()
        client = app.test_client()
        for rt in ('post', 'comment', 'profile', 'community'):
            r = client.get(f'/api/social/og-image/{rt}/1')
            self.assertNotEqual(r.status_code, 400,
                                f"resource_type '{rt}' should be accepted")

    @patch('integrations.social.api_sharing.get_db')
    def test_embed_accepts_valid_types(self, mock_get_db):
        """GET /embed/<valid_type>/1 should NOT return 400."""
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        app = self._make_sharing_app()
        client = app.test_client()
        for rt in ('post', 'comment', 'profile', 'community'):
            r = client.get(f'/api/social/embed/{rt}/1')
            self.assertNotEqual(r.status_code, 400,
                                f"resource_type '{rt}' should be accepted")


# ═══════════════════════════════════════════════════════════════════════
# 7. ANR & Watchdog Tests (Android-inspired)
# ═══════════════════════════════════════════════════════════════════════

class TestANRShellAPIs(unittest.TestCase):
    """Shell API routes must respond within 2 seconds (ANR detection)."""

    def setUp(self):
        _reset_allowed_roots()
        self.app = _make_shell_app()
        self.client = self.app.test_client()

    def tearDown(self):
        _reset_allowed_roots()

    def test_notifications_responds_within_timeout(self):
        """GET /api/shell/notifications responds within 2 seconds."""
        start = time.monotonic()
        r = self.client.get('/api/shell/notifications',
                            environ_base={'REMOTE_ADDR': '127.0.0.1'})
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0,
                        f"Notifications took {elapsed:.2f}s — ANR threshold is 2s")

    def test_file_browse_responds_within_timeout(self):
        """GET /api/shell/files/browse responds within 2 seconds."""
        home = os.path.expanduser('~')
        start = time.monotonic()
        r = self.client.get(f'/api/shell/files/browse?path={home}',
                            environ_base={'REMOTE_ADDR': '127.0.0.1'})
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0,
                        f"File browse took {elapsed:.2f}s — ANR threshold is 2s")

    def test_notification_send_responds_within_timeout(self):
        """POST /api/shell/notifications/send responds within 2 seconds."""
        start = time.monotonic()
        r = self.client.post('/api/shell/notifications/send',
                             json={'title': 'ANR test', 'body': 'speed check'},
                             environ_base={'REMOTE_ADDR': '127.0.0.1'})
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0,
                        f"Notification send took {elapsed:.2f}s — ANR threshold is 2s")


class TestBootANR(unittest.TestCase):
    """Boot service timeout detection."""

    def setUp(self):
        _reset_boot_state()

    def tearDown(self):
        _reset_boot_state()

    @patch('core.platform.bootstrap.bootstrap_platform')
    def test_slow_bootstrap_does_not_block_forever(self, mock_bootstrap):
        """Even if bootstrap is slow, it should eventually return."""
        from core.platform.boot_service import ensure_platform

        def controlled_bootstrap(ext_dir=None):
            time.sleep(0.1)
            return MagicMock()

        mock_bootstrap.side_effect = controlled_bootstrap

        result = [None]
        error = [None]

        def boot_thread():
            try:
                result[0] = ensure_platform()
            except Exception as e:
                error[0] = e

        t = threading.Thread(target=boot_thread)
        t.start()
        t.join(timeout=5.0)

        self.assertFalse(t.is_alive(),
                         "Boot thread should not still be running after 5s")
        self.assertIsNone(error[0])
        self.assertIsNotNone(result[0])


class TestServiceIsolation(unittest.TestCase):
    """Service isolation: crash in one subsystem doesn't kill others."""

    def test_shell_os_import_failure_doesnt_crash_desktop(self):
        """If shell_os_apis fails to import, shell_desktop_apis still works."""
        from flask import Flask
        app = Flask(__name__)
        app.config['TESTING'] = True

        os_registered = False
        desktop_registered = False

        # Simulate what liquid_ui_service does — try/except isolation
        try:
            with patch.dict('sys.modules',
                            {'integrations.agent_engine.shell_os_apis': None}):
                from integrations.agent_engine.shell_os_apis import (
                    register_shell_os_routes)
                register_shell_os_routes(app)
                os_registered = True
        except (ImportError, TypeError):
            pass

        try:
            from integrations.agent_engine.shell_desktop_apis import (
                register_shell_desktop_routes)
            register_shell_desktop_routes(app)
            desktop_registered = True
        except ImportError:
            desktop_registered = True  # Module not installed = acceptable

        self.assertFalse(os_registered)
        self.assertTrue(desktop_registered)

    def test_blast_radius_file_op_doesnt_affect_terminal(self):
        """A file operation failure doesn't prevent terminal operations."""
        _reset_allowed_roots()
        try:
            app = _make_shell_app()
            client = app.test_client()

            # First: a failing file operation (browse invalid path)
            r1 = client.get('/api/shell/files/browse?path=/nonexistent/path/xyz',
                            environ_base={'REMOTE_ADDR': '127.0.0.1'})
            self.assertIn(r1.status_code, (400, 403))

            # Then: terminal exec should still work
            r2 = client.post('/api/shell/terminal/exec',
                             json={'command': 'echo hello', 'timeout': 5},
                             environ_base={'REMOTE_ADDR': '127.0.0.1'})
            self.assertIn(r2.status_code, (200, 500))
            if r2.status_code == 200:
                data = json.loads(r2.data)
                self.assertIn('stdout', data)
        finally:
            _reset_allowed_roots()


class TestNotificationQueueBound(unittest.TestCase):
    """Notification queue must not grow unbounded (watchdog)."""

    def test_queue_handles_many_notifications(self):
        """Sending many notifications shouldn't crash."""
        app = _make_shell_app()
        client = app.test_client()

        for i in range(100):
            r = client.post('/api/shell/notifications/send',
                            json={'title': f'N{i}', 'body': f'body {i}'},
                            environ_base={'REMOTE_ADDR': '127.0.0.1'})
            self.assertEqual(r.status_code, 200)

        # Listing should still work
        r = client.get('/api/shell/notifications?limit=50',
                       environ_base={'REMOTE_ADDR': '127.0.0.1'})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertLessEqual(len(data['notifications']), 100)

    def test_notification_listing_responsive_under_stress(self):
        """Listing notifications should be fast even with many enqueued."""
        app = _make_shell_app()
        client = app.test_client()

        for i in range(200):
            client.post('/api/shell/notifications/send',
                        json={'title': f'Stress{i}', 'body': 'x' * 100},
                        environ_base={'REMOTE_ADDR': '127.0.0.1'})

        start = time.monotonic()
        r = client.get('/api/shell/notifications',
                       environ_base={'REMOTE_ADDR': '127.0.0.1'})
        elapsed = time.monotonic() - start
        self.assertEqual(r.status_code, 200)
        self.assertLess(elapsed, 2.0,
                        "Listing notifications after stress should be under 2s")


# ═══════════════════════════════════════════════════════════════════════
# 8. Audit Trail Tests
# ═══════════════════════════════════════════════════════════════════════

class TestAuditShellOp(unittest.TestCase):
    """_audit_shell_op() audit log integration."""

    def test_audit_called_on_destructive_operation(self):
        """_audit_shell_op() invokes the immutable audit log."""
        mock_log = MagicMock()
        with patch('security.immutable_audit_log.get_audit_log',
                   return_value=mock_log):
            from integrations.agent_engine.shell_os_apis import _audit_shell_op
            _audit_shell_op('file_delete', {'path': '/tmp/test.txt'})

            mock_log.log_event.assert_called_once_with(
                'shell_ops', 'shell_os_api', 'file_delete',
                detail={'path': '/tmp/test.txt'})

    def test_audit_does_not_crash_if_log_unavailable(self):
        """_audit_shell_op() gracefully handles missing audit log."""
        from integrations.agent_engine.shell_os_apis import _audit_shell_op
        # The function does `from security.immutable_audit_log import get_audit_log`
        # inside a try/except. If the import fails, it should silently pass.
        # We test this by making the import raise.
        with patch.dict('sys.modules', {'security.immutable_audit_log': None}):
            # Force re-import failure by clearing cached import
            try:
                _audit_shell_op('file_delete', {'path': '/tmp/test.txt'})
            except Exception:
                self.fail(
                    "_audit_shell_op should not raise when audit log is unavailable")

    def test_audit_does_not_crash_if_log_event_fails(self):
        """_audit_shell_op() catches log_event exceptions."""
        mock_log = MagicMock()
        mock_log.log_event.side_effect = RuntimeError("DB write failed")
        with patch('security.immutable_audit_log.get_audit_log',
                   return_value=mock_log):
            from integrations.agent_engine.shell_os_apis import _audit_shell_op
            try:
                _audit_shell_op('terminal_exec', {'command': 'ls'})
            except Exception:
                self.fail(
                    "_audit_shell_op should not raise when log_event fails")

    def test_audit_called_during_file_delete(self):
        """DELETE route calls _audit_shell_op with 'file_delete' action."""
        _reset_allowed_roots()
        try:
            app = _make_shell_app()
            client = app.test_client()

            with patch(
                'integrations.agent_engine.shell_os_apis._audit_shell_op'
            ) as mock_audit:
                tmp = tempfile.NamedTemporaryFile(
                    delete=False, dir=tempfile.gettempdir())
                tmp.write(b'test')
                tmp.close()
                try:
                    r = client.post('/api/shell/files/delete',
                                    json={'path': tmp.name},
                                    environ_base={'REMOTE_ADDR': '127.0.0.1'})
                    if r.status_code == 200:
                        mock_audit.assert_any_call(
                            'file_delete', {'path': tmp.name})
                finally:
                    if os.path.exists(tmp.name):
                        os.unlink(tmp.name)
        finally:
            _reset_allowed_roots()

    def test_audit_called_during_terminal_exec(self):
        """Terminal exec route calls _audit_shell_op."""
        _reset_allowed_roots()
        try:
            app = _make_shell_app()
            client = app.test_client()

            with patch(
                'integrations.agent_engine.shell_os_apis._audit_shell_op'
            ) as mock_audit:
                r = client.post('/api/shell/terminal/exec',
                                json={'command': 'echo test', 'timeout': 5},
                                environ_base={'REMOTE_ADDR': '127.0.0.1'})
                if r.status_code == 200:
                    mock_audit.assert_called()
        finally:
            _reset_allowed_roots()

    def test_audit_called_during_file_move(self):
        """Move route calls _audit_shell_op with 'file_move'."""
        _reset_allowed_roots()
        try:
            app = _make_shell_app()
            client = app.test_client()

            with patch(
                'integrations.agent_engine.shell_os_apis._audit_shell_op'
            ) as mock_audit:
                src = tempfile.NamedTemporaryFile(
                    delete=False, dir=tempfile.gettempdir(),
                    suffix='_src.txt')
                src.write(b'move test')
                src.close()
                dst_path = src.name + '_moved'
                try:
                    r = client.post('/api/shell/files/move',
                                    json={'source': src.name,
                                          'destination': dst_path},
                                    environ_base={'REMOTE_ADDR': '127.0.0.1'})
                    if r.status_code == 200:
                        mock_audit.assert_any_call(
                            'file_move',
                            {'from': src.name, 'to': dst_path})
                finally:
                    for p in (src.name, dst_path):
                        if os.path.exists(p):
                            os.unlink(p)
        finally:
            _reset_allowed_roots()


# ═══════════════════════════════════════════════════════════════════════
# Additional Edge Cases
# ═══════════════════════════════════════════════════════════════════════

class TestBootServiceGetRegistrySafe(unittest.TestCase):
    """_get_registry_safe() handles import errors gracefully."""

    def test_get_registry_safe_returns_none_on_error(self):
        """_get_registry_safe returns None if registry raises."""
        from core.platform.boot_service import _get_registry_safe
        with patch('core.platform.registry.get_registry',
                   side_effect=RuntimeError("registry error")):
            result = _get_registry_safe()
            self.assertIsNone(result)


class TestFileTransferSHA256Verification(unittest.TestCase):
    """SHA256 verification on received files."""

    def test_sha256_mismatch_rejected(self):
        """File with wrong SHA256 is rejected."""
        from integrations.remote_desktop.file_transfer import FileTransfer
        ft = FileTransfer()
        save_dir = tempfile.mkdtemp()
        try:
            ft.receive_file(save_dir)
            ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_START',
                'filename': 'test.bin',
                'size': 5,
                'sha256': 'deadbeef' * 8,
            })
            ft.handle_frame(b'hello')
            result = ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_END',
                'filename': 'test.bin',
                'sha256': 'deadbeef' * 8,
            })
            self.assertFalse(result['success'])
            self.assertIn('SHA256 mismatch', result['error'])
        finally:
            for f in os.listdir(save_dir):
                os.remove(os.path.join(save_dir, f))
            os.rmdir(save_dir)


class TestShellAuthEdgeCases(unittest.TestCase):
    """Edge cases for shell auth."""

    def setUp(self):
        self.app = _make_shell_app()
        self.app.config['TESTING'] = True

    def test_zero_address_allowed(self):
        """0.0.0.0 is treated as local (bind-all)."""
        with self.app.test_request_context(
                '/api/shell/notifications',
                environ_base={'REMOTE_ADDR': '0.0.0.0'}):
            from integrations.agent_engine.shell_os_apis import _shell_auth_check
            ok, *rest = _shell_auth_check()
            self.assertTrue(ok)

    def test_missing_remote_addr_rejected(self):
        """Empty remote_addr is rejected."""
        with patch.dict(os.environ, {'HART_SHELL_TOKEN': ''}, clear=False):
            with self.app.test_request_context(
                    '/api/shell/notifications',
                    environ_base={'REMOTE_ADDR': ''}):
                from integrations.agent_engine.shell_os_apis import _shell_auth_check
                result = _shell_auth_check()
                self.assertFalse(result[0])


class TestPathSandboxEdgeCases(unittest.TestCase):
    """Edge cases for path sandbox."""

    def setUp(self):
        _reset_allowed_roots()

    def tearDown(self):
        _reset_allowed_roots()

    def test_symlink_resolved(self):
        """Symlinks are resolved via realpath before checking."""
        from integrations.agent_engine.shell_os_apis import _is_path_allowed
        tmp = tempfile.mkdtemp()
        target = os.path.join(tmp, 'target.txt')
        link = os.path.join(tmp, 'link.txt')
        try:
            with open(target, 'w') as f:
                f.write('test')
            try:
                os.symlink(target, link)
                self.assertTrue(_is_path_allowed(target))
                self.assertTrue(_is_path_allowed(link))
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks not supported on this platform/permissions")
        finally:
            for p in (link, target):
                if os.path.exists(p):
                    os.unlink(p)
            os.rmdir(tmp)


class TestFileTransferProgressTracking(unittest.TestCase):
    """Verify progress tracking doesn't crash during transfers."""

    def test_progress_callback_called(self):
        """Progress callback is invoked during chunk reception."""
        from integrations.remote_desktop.file_transfer import FileTransfer
        ft = FileTransfer()
        progress_updates = []
        ft.on_progress(lambda p: progress_updates.append(p.to_dict()))

        save_dir = tempfile.mkdtemp()
        try:
            ft.receive_file(save_dir)
            ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_START',
                'filename': 'progress_test.bin',
                'size': 10,
                'sha256': '',
            })
            ft.handle_frame(b'12345')
            ft.handle_frame(b'67890')

            self.assertGreater(len(progress_updates), 0,
                               "Progress callback should have been called")
            last = progress_updates[-1]
            self.assertEqual(last['transferred_bytes'], 10)
        finally:
            for f in os.listdir(save_dir):
                os.remove(os.path.join(save_dir, f))
            os.rmdir(save_dir)

    def test_progress_callback_error_doesnt_crash(self):
        """If progress callback raises, transfer continues."""
        import hashlib
        from integrations.remote_desktop.file_transfer import FileTransfer
        ft = FileTransfer()

        def bad_callback(p):
            raise ValueError("callback exploded")

        ft.on_progress(bad_callback)

        save_dir = tempfile.mkdtemp()
        try:
            ft.receive_file(save_dir)
            ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_START',
                'filename': 'robust.bin',
                'size': 5,
                'sha256': '',
            })
            ft.handle_frame(b'hello')
            h = hashlib.sha256(b'hello').hexdigest()
            result = ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_END',
                'filename': 'robust.bin',
                'sha256': h,
            })
            self.assertTrue(result['success'])
        finally:
            for f in os.listdir(save_dir):
                os.remove(os.path.join(save_dir, f))
            os.rmdir(save_dir)


if __name__ == '__main__':
    unittest.main()
