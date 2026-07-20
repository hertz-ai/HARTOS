"""
Behavioral tests for the display-management backend routes added to
integrations.agent_engine.shell_desktop_apis:

  GET  /api/shell/displays/modes      — available-mode enumeration (picker data)
  GET  /api/shell/displays/profile    — read the saved multi-monitor arrangement
  POST /api/shell/displays/profile    — apply live + PERSIST (profile.json + kanshi)
  GET/POST /api/shell/display/font-scale — persist the font-scale preference

These import the REAL register_shell_desktop_routes (no inline re-implementation —
the parallel-path trap the legacy test_shell_display_api.py fell into), mock the
subprocess boundary (_run) with swaymsg/wlr-randr fixtures, and assert observable
behaviour: enumeration shape, ON-DISK persistence (profile.json + a real kanshi
config), read-back, and the degrade-not-die fallback when a mode is rejected.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import integrations.agent_engine.shell_desktop_apis as mod


# A swaymsg `get_outputs -r` fixture: one output, with a duplicate mode (must be
# de-duplicated) and a current_mode (must surface as `current`).
SWAY_OUTPUTS = json.dumps([{
    "name": "eDP-1",
    "active": True,
    "scale": 1.0,
    "current_mode": {"width": 1920, "height": 1080, "refresh": 60000},
    "modes": [
        {"width": 1920, "height": 1080, "refresh": 60000},
        {"width": 1280, "height": 720, "refresh": 60000},
        {"width": 1920, "height": 1080, "refresh": 60000},  # dup -> deduped
    ],
}])

WLR_RANDR_TEXT = """eDP-1 "Acme Display"
  Enabled: yes
  Modes:
    1920x1080 px, 60.000000 Hz (preferred, current)
    1280x720 px, 60.000000 Hz
  Scale: 1.000000
"""


def _make_client():
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    mod.register_shell_desktop_routes(app)
    return app.test_client()


class _DisplayTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self._tmp.name
        self._env = patch.dict(os.environ, {
            'HART_DISPLAY_DIR': os.path.join(d, 'hart', 'display'),
            'HART_KANSHI_CONFIG': os.path.join(d, 'kanshi', 'config'),
        })
        self._env.start()
        self.client = _make_client()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class TestModesEnumeration(_DisplayTestBase):

    @patch.object(mod, '_is_wayland', return_value=True)
    @patch.object(mod, '_run')
    def test_swaymsg_modes_enumerated_and_deduped(self, mock_run, _w):
        mock_run.return_value = MagicMock(returncode=0, stdout=SWAY_OUTPUTS)
        r = self.client.get('/api/shell/displays/modes')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['compositor'], 'wayland')
        outs = data['outputs']
        self.assertEqual(len(outs), 1)
        o = outs[0]
        self.assertEqual(o['name'], 'eDP-1')
        self.assertEqual(o['current'], '1920x1080')
        # the duplicate 1920x1080@60 collapsed -> exactly two modes
        self.assertEqual(len(o['modes']), 2)
        res = [m['resolution'] for m in o['modes']]
        self.assertIn('1920x1080', res)
        self.assertIn('1280x720', res)

    @patch.object(mod, '_is_wayland', return_value=True)
    @patch.object(mod, '_run')
    def test_falls_back_to_wlr_randr_when_swaymsg_absent(self, mock_run, _w):
        def fake(cmd, timeout=None, **kw):
            if cmd[:1] == ['swaymsg']:
                return None  # swaymsg not installed
            if cmd[:1] == ['wlr-randr']:
                return MagicMock(returncode=0, stdout=WLR_RANDR_TEXT)
            return MagicMock(returncode=0, stdout='')
        mock_run.side_effect = fake
        r = self.client.get('/api/shell/displays/modes')
        data = json.loads(r.data)
        self.assertEqual(len(data['outputs']), 1)
        o = data['outputs'][0]
        self.assertEqual(o['name'], 'eDP-1')
        self.assertEqual(o['current'], '1920x1080')
        self.assertEqual(len(o['modes']), 2)

    @patch.object(mod, '_is_wayland', return_value=True)
    @patch.object(mod, '_run', return_value=None)
    def test_degrades_to_empty_when_no_output_protocol(self, _run, _w):
        # Tier-1 hart-comp / no wlr-output-management: never an error, just empty.
        r = self.client.get('/api/shell/displays/modes')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.data)['outputs'], [])


class TestProfilePersistence(_DisplayTestBase):

    @patch.object(mod, '_is_wayland', return_value=True)
    @patch.object(mod, '_run')
    def test_post_applies_and_persists_json_and_kanshi(self, mock_run, _w):
        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
        body = {'outputs': [
            {'name': 'eDP-1', 'resolution': '1920x1080', 'position': '0,0',
             'scale': 1.0},
            {'name': 'HDMI-A-1', 'resolution': '2560x1440', 'position': '1920,0'},
        ]}
        r = self.client.post('/api/shell/displays/profile', json=body)
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['status'], 'ok')
        self.assertTrue(data['persisted'])
        self.assertFalse(data['degraded'])

        # profile.json written (UI source of truth)
        with open(mod._display_profile_path()) as f:
            saved = json.load(f)
        names = [o['name'] for o in saved['outputs']]
        self.assertEqual(names, ['eDP-1', 'HDMI-A-1'])

        # kanshi config written (daemon input) at the same path the daemon reads
        with open(mod._kanshi_config_path()) as f:
            kconf = f.read()
        self.assertIn('profile hart {', kconf)
        self.assertIn('output "eDP-1" mode 1920x1080 position 0,0 scale 1.0', kconf)
        self.assertIn('output "HDMI-A-1" mode 2560x1440 position 1920,0', kconf)

        # a real swaymsg apply was attempted for each output
        applied = [c.args[0] for c in mock_run.call_args_list
                   if c.args and c.args[0][:1] == ['swaymsg']
                   and 'output' in c.args[0]]
        self.assertTrue(any('eDP-1' in c for c in applied))
        self.assertTrue(any('HDMI-A-1' in c for c in applied))

    @patch.object(mod, '_is_wayland', return_value=True)
    @patch.object(mod, '_run')
    def test_get_reads_back_saved_profile(self, mock_run, _w):
        mock_run.return_value = MagicMock(returncode=0)
        body = {'outputs': [{'name': 'eDP-1', 'resolution': '1366x768'}]}
        self.client.post('/api/shell/displays/profile', json=body)
        r = self.client.get('/api/shell/displays/profile')
        data = json.loads(r.data)
        self.assertEqual(data['outputs'][0]['name'], 'eDP-1')
        self.assertEqual(data['outputs'][0]['resolution'], '1366x768')

    def test_post_rejects_empty(self):
        r = self.client.post('/api/shell/displays/profile', json={'outputs': []})
        self.assertEqual(r.status_code, 400)

    @patch.object(mod, '_is_wayland', return_value=True)
    @patch.object(mod, '_run')
    def test_degrade_not_die_when_mode_rejected(self, mock_run, _w):
        # swaymsg/wlr-randr reject the explicit mode; the bare `enable` retry
        # succeeds -> the output stays LIT and the call reports degraded, not failed.
        def fake(cmd, timeout=None, **kw):
            if cmd[:1] == ['swaymsg'] and 'output' in cmd and 'mode' in cmd:
                return MagicMock(returncode=1, stderr='mode not found')
            if cmd[:1] == ['wlr-randr']:
                return MagicMock(returncode=1, stderr='mode not found')
            if cmd[:1] == ['swaymsg'] and 'output' in cmd:  # bare enable retry
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)
        mock_run.side_effect = fake
        body = {'outputs': [{'name': 'eDP-1', 'resolution': '9999x9999'}]}
        r = self.client.post('/api/shell/displays/profile', json=body)
        data = json.loads(r.data)
        self.assertTrue(data['degraded'])
        self.assertTrue(data['outputs'][0]['applied'])
        self.assertTrue(data['outputs'][0]['degraded'])


class TestFontScalePreference(_DisplayTestBase):

    def test_get_default_is_identity(self):
        r = self.client.get('/api/shell/display/font-scale')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.data)['scale'], 1.0)

    def test_post_persists_and_reports_next_session(self):
        r = self.client.post('/api/shell/display/font-scale', json={'scale': 1.25})
        data = json.loads(r.data)
        self.assertEqual(data['scale'], 1.25)
        self.assertEqual(data['applied_on'], 'next-session')
        # round-trips from disk
        r2 = self.client.get('/api/shell/display/font-scale')
        self.assertEqual(json.loads(r2.data)['scale'], 1.25)

    def test_post_clamps_out_of_range(self):
        r = self.client.post('/api/shell/display/font-scale', json={'scale': 10.0})
        self.assertEqual(json.loads(r.data)['scale'], 3.0)
        r = self.client.post('/api/shell/display/font-scale', json={'scale': 0.1})
        self.assertEqual(json.loads(r.data)['scale'], 0.5)

    def test_post_rejects_non_numeric(self):
        r = self.client.post('/api/shell/display/font-scale', json={'scale': 'big'})
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()
