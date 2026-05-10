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

    def test_gateway_dir_points_at_repo_path(self):
        gw = ws._gateway_dir()
        self.assertTrue(gw.name == 'whatsapp')
        # gateway.js MUST exist at this path (Phase B contract).
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
