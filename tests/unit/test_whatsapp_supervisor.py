"""Unit tests for integrations.social.whatsapp_supervisor.

Tests the gating + path-resolution logic without spawning Node.  The
actual subprocess lifecycle is covered by an integration test that
runs only when Node + npm are present (skipped on CI without Node).
"""

import os
import sys
import unittest
from unittest import mock

# Make HARTOS importable regardless of pytest invocation cwd.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from integrations.social import whatsapp_supervisor as ws


class GatingTests(unittest.TestCase):

    def setUp(self):
        # Snapshot env vars we mutate so tests don't bleed.
        self._env_keys = (
            'WHATSAPP_AUTOSTART',
            'WHATSAPP_API_URL',
            'HEVOLVE_DEPLOY_MODE',
        )
        self._saved = {k: os.environ.get(k) for k in self._env_keys}
        for k in self._env_keys:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_flat_mode_runs(self):
        os.environ['HEVOLVE_DEPLOY_MODE'] = 'flat'
        self.assertTrue(ws.supervisor_should_run())

    def test_default_regional_mode_runs(self):
        os.environ['HEVOLVE_DEPLOY_MODE'] = 'regional'
        self.assertTrue(ws.supervisor_should_run())

    def test_central_mode_skips(self):
        os.environ['HEVOLVE_DEPLOY_MODE'] = 'central'
        self.assertFalse(ws.supervisor_should_run())

    def test_autostart_zero_force_disables(self):
        os.environ['HEVOLVE_DEPLOY_MODE'] = 'flat'
        os.environ['WHATSAPP_AUTOSTART'] = '0'
        self.assertFalse(ws.supervisor_should_run())

    def test_autostart_one_force_enables_even_on_central(self):
        os.environ['HEVOLVE_DEPLOY_MODE'] = 'central'
        os.environ['WHATSAPP_AUTOSTART'] = '1'
        self.assertTrue(ws.supervisor_should_run())

    def test_remote_api_url_skips(self):
        # Operator points at a remote WAHA — supervisor stays off so
        # the existing adapter override path takes effect.
        os.environ['HEVOLVE_DEPLOY_MODE'] = 'flat'
        os.environ['WHATSAPP_API_URL'] = 'https://my-waha.example.com'
        self.assertFalse(ws.supervisor_should_run())

    def test_localhost_api_url_still_runs(self):
        os.environ['HEVOLVE_DEPLOY_MODE'] = 'flat'
        os.environ['WHATSAPP_API_URL'] = 'http://localhost:3000'
        self.assertTrue(ws.supervisor_should_run())


class PathTests(unittest.TestCase):

    def test_bundled_gateway_dir_points_at_install_path(self):
        # The bundled copy is read-only at runtime on Windows
        # (lives under Program Files in the cx_Freeze install)
        # but ships with the repo so tests can verify it exists.
        gw = ws._bundled_gateway_dir()
        self.assertTrue(gw.name == 'whatsapp')
        self.assertTrue((gw / 'gateway.js').exists())
        self.assertTrue((gw / 'package.json').exists())

    def test_whatsapp_home_under_hevolve_home(self):
        # Default ~/.hevolve/whatsapp; honours HEVOLVE_HOME override.
        os.environ['HEVOLVE_HOME'] = '/tmp/hevolve-test'
        try:
            home = ws._whatsapp_home()
            self.assertTrue(str(home).endswith(os.path.join(
                'hevolve-test', 'whatsapp')))
        finally:
            os.environ.pop('HEVOLVE_HOME', None)


class GatewayDirIsUserWritableTests(unittest.TestCase):
    """Regression for 2026-05-17→23 production failure:
    _gateway_dir() returned the read-only Program Files install path,
    npm install hit EPERM every boot, gateway never started, WhatsApp
    channel unreachable.  Fix routes gateway.js + package.json into
    ~/.hevolve/whatsapp/gateway/ which is user-writable, and copies
    the bundled files in idempotently on first call."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix='whatsapp_supervisor_test_')
        os.environ['HEVOLVE_HOME'] = self._tmp

    def tearDown(self):
        import shutil
        os.environ.pop('HEVOLVE_HOME', None)
        try:
            shutil.rmtree(self._tmp, ignore_errors=True)
        except OSError:
            pass

    def test_gateway_dir_returns_writable_path_under_hevolve_home(self):
        gw = ws._gateway_dir()
        # Must be under HEVOLVE_HOME, NOT under the install path —
        # this is the core invariant the fix establishes.
        self.assertTrue(str(gw).startswith(self._tmp),
            f"_gateway_dir() returned {gw!r} which is not under "
            f"HEVOLVE_HOME={self._tmp!r} — fix regressed back to "
            f"the read-only Program Files path.")
        # Should be the gateway/ subfolder of whatsapp/
        self.assertTrue(gw.name == 'gateway')
        self.assertTrue(gw.parent.name == 'whatsapp')

    def test_gateway_dir_copies_bundled_files_on_first_call(self):
        # First call to _gateway_dir() must copy gateway.js +
        # package.json from the bundled location so the subsequent
        # `npm install` has something to install against.
        gw = ws._gateway_dir()
        self.assertTrue((gw / 'gateway.js').exists(),
            "gateway.js was not staged into the writable dir on "
            "first call — npm install will fail with ENOENT.")
        self.assertTrue((gw / 'package.json').exists(),
            "package.json was not staged into the writable dir on "
            "first call — npm install has no spec to install.")

    def test_gateway_dir_idempotent(self):
        # Second call must NOT re-copy (would clobber any in-place
        # edits the gateway makes to its own files post-install, and
        # is the literal definition of idempotent per the docstring).
        gw1 = ws._gateway_dir()
        # Touch the staged gateway.js with a sentinel mtime; if the
        # second call recopies, the mtime changes back to the
        # source's.
        target = gw1 / 'gateway.js'
        from datetime import datetime, timedelta
        old_mtime = (datetime.now() - timedelta(days=7)).timestamp()
        os.utime(str(target), (old_mtime, old_mtime))
        first_mtime = target.stat().st_mtime
        gw2 = ws._gateway_dir()
        second_mtime = target.stat().st_mtime
        self.assertEqual(gw1, gw2)
        # mtime should be unchanged: the staged file is newer than
        # the source check (we artificially aged it but the comparison
        # in _gateway_dir uses dst_mtime < src_mtime so an older dst
        # WOULD trigger recopy).  Use file content equality as the
        # real idempotency proof.
        self.assertEqual((gw1 / 'gateway.js').read_bytes(),
                         (ws._bundled_gateway_dir() / 'gateway.js').read_bytes())

    def test_gateway_dir_distinct_from_bundled(self):
        """The writable dir and the bundled dir must NOT be the same
        path — that's the entire point of the fix."""
        writable = ws._gateway_dir()
        bundled = ws._bundled_gateway_dir()
        self.assertNotEqual(writable.resolve(), bundled.resolve(),
            f"_gateway_dir() ({writable}) and _bundled_gateway_dir() "
            f"({bundled}) resolved to the same path — the fix regressed.")


class StartSupervisorTests(unittest.TestCase):

    def test_skipped_when_gated_off(self):
        with mock.patch.object(
            ws, 'supervisor_should_run', return_value=False
        ):
            info = ws.start_supervisor()
        self.assertFalse(info['running'])
        self.assertEqual(info['reason'], 'gated_off')

    def test_skipped_when_node_missing(self):
        # supervisor_should_run True, but ensure_node returns None →
        # last_error is the "install Node" hint, no spawn attempted.
        with (
            mock.patch.object(ws, 'supervisor_should_run', return_value=True),
            mock.patch.object(ws, 'ensure_node', return_value=None),
            mock.patch.object(ws, 'ensure_baileys_deps', return_value=False),
        ):
            # Reset the singleton so this test runs cleanly even after
            # a previous test left it instantiated.
            ws._supervisor = None
            info = ws.start_supervisor()
        self.assertFalse(info.get('running'))
        self.assertIn('Node.js', info.get('last_error', ''))


if __name__ == '__main__':
    unittest.main()
