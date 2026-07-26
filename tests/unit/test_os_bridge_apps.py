"""
Behavioural tests for the TYPED app-integration bridge — apps slice (#117 / W3).

Per the no-grep-tests rule every test imports the REAL code (contract / apps handler
/ app_bridge_service / route module), mocks only the boundary (the launcher
``subprocess.run`` for launch; the WM client for list/focus/close), calls the real
function, and asserts the observable side-effects — which launcher argv was built,
which window verb was dispatched, and the result-check (a failure is a real error,
never a masked success).

Covers:
  * the contract exposes the ``apps`` domain (implemented) with launch/list/focus/
    close, and validates launch params (app_id required, subsystem choices);
  * ``launch`` routes through the EXISTING app_bridge SemanticRouter dispatch to
    ``gtk-launch`` (linux) / Wine (windows) with the app_id, result-checked;
  * an invalid app_id is refused BEFORE any launcher runs;
  * ``list`` / ``focus`` / ``close`` route to the canonical WM client (one window-op
    path), mapping window_id -> con_id, and a refused close is not masked;
  * the typed /api/os/invoke dispatcher wires apps.launch end-to-end (200) and the
    contract's param validation rejects a missing app_id (400).
"""

import unittest
from unittest.mock import patch, MagicMock

from integrations.agent_engine.os_bridge import contract as bridge_contract
from integrations.agent_engine.os_bridge import apps as bridge_apps
from integrations.agent_engine.os_bridge.routes import register_os_bridge_routes
import integrations.agent_engine.app_bridge_service as abs_mod

_ABS = 'integrations.agent_engine.app_bridge_service'
_WM = 'integrations.agent_engine.hart_wm_client.get_wm_client'


class _FakeRun:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_app():
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    register_os_bridge_routes(app)
    return app, app.test_client()


# ───────────────────────── contract: apps domain ──────────────────────────────

class TestAppsContract(unittest.TestCase):
    def test_apps_domain_implemented_with_the_four_ops(self):
        self.assertTrue(bridge_contract.is_implemented('apps'))
        self.assertIsNotNone(bridge_contract.get_op('apps', 'launch'))
        m = bridge_contract.describe_contract()['domains']['apps']
        self.assertTrue(m['implemented'])
        ops = {o['name'] for o in m['ops']}
        self.assertEqual(ops, {'launch', 'list', 'focus', 'close'})

    def test_close_is_flagged_destructive(self):
        spec = bridge_contract.get_op('apps', 'close')
        self.assertTrue(spec.destructive)

    def test_launch_validates_app_id_and_subsystem_choices(self):
        spec = bridge_contract.get_op('apps', 'launch')
        # app_id is required.
        ok, _err = bridge_contract.validate_params(spec, {})
        self.assertFalse(ok)
        # a bare app_id is enough (subsystem optional).
        ok, cleaned = bridge_contract.validate_params(spec, {'app_id': 'firefox'})
        self.assertTrue(ok)
        self.assertEqual(cleaned['app_id'], 'firefox')
        # subsystem is constrained to the known launchers.
        ok, _err = bridge_contract.validate_params(
            spec, {'app_id': 'x', 'subsystem': 'bogus'})
        self.assertFalse(ok)
        ok, cleaned = bridge_contract.validate_params(
            spec, {'app_id': 'x', 'subsystem': 'windows'})
        self.assertTrue(ok)
        self.assertEqual(cleaned['subsystem'], 'windows')

    def test_focus_requires_int_window_id(self):
        spec = bridge_contract.get_op('apps', 'focus')
        ok, _err = bridge_contract.validate_params(spec, {'window_id': 'seven'})
        self.assertFalse(ok)
        ok, cleaned = bridge_contract.validate_params(spec, {'window_id': 7})
        self.assertTrue(ok)
        self.assertEqual(cleaned['window_id'], 7)


# ───────────── launch: routes through the EXISTING app_bridge dispatch ─────────

class TestLaunchRoutesToAppBridgeDispatch(unittest.TestCase):
    def setUp(self):
        abs_mod._service = None  # fresh in-process singleton per test

    def test_linux_launch_runs_gtk_launch_with_the_app_id(self):
        bridge = abs_mod.get_app_bridge()
        with patch(_ABS + '.subprocess.run',
                   return_value=_FakeRun(returncode=0)) as run:
            res = bridge.launch_app('org.mozilla.firefox', 'linux')
        self.assertTrue(res['ok'])
        self.assertEqual(res['subsystem'], 'linux')
        # The REAL SemanticRouter dispatch built the gtk-launch argv with the app_id.
        self.assertEqual(run.call_args.args[0], ['gtk-launch', 'org.mozilla.firefox'])

    def test_windows_launch_uses_wine_with_the_exe(self):
        bridge = abs_mod.get_app_bridge()
        with patch(_ABS + '.subprocess.run',
                   return_value=_FakeRun(returncode=0)) as run:
            res = bridge.launch_app('notepad.exe', 'windows')
        self.assertTrue(res['ok'])
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], 'wine')
        self.assertIn('notepad.exe', argv)

    def test_launch_failure_is_result_checked_not_masked(self):
        bridge = abs_mod.get_app_bridge()
        with patch(_ABS + '.subprocess.run',
                   return_value=_FakeRun(returncode=1, stderr='no such app')):
            res = bridge.launch_app('firefox', 'linux')
        self.assertFalse(res['ok'])
        self.assertIn('error', res)

    def test_invalid_app_id_refused_before_any_launcher_runs(self):
        bridge = abs_mod.get_app_bridge()
        with patch(_ABS + '.subprocess.run') as run:
            res = bridge.launch_app('rm -rf ; evil', 'linux')
        self.assertFalse(res['ok'])
        self.assertIn('invalid app_id', res['error'])
        run.assert_not_called()

    def test_empty_app_id_refused(self):
        bridge = abs_mod.get_app_bridge()
        with patch(_ABS + '.subprocess.run') as run:
            res = bridge.launch_app('', 'linux')
        self.assertFalse(res['ok'])
        run.assert_not_called()


# ───────────── list/focus/close: route to the canonical WM client ──────────────

class TestWindowOpsRouteToWmClient(unittest.TestCase):
    def test_list_dispatches_window_list(self):
        wm = MagicMock()
        wm.dispatch_verb.return_value = {'ok': True, 'windows': [{'id': 3}]}
        with patch(_WM, return_value=wm):
            status, payload = bridge_apps.invoke_apps('list', {})
        self.assertEqual(status, 200)
        self.assertEqual(wm.dispatch_verb.call_args.args[0], 'window.list')
        self.assertEqual(payload['windows'], [{'id': 3}])
        self.assertEqual(payload['op'], 'list')

    def test_focus_maps_window_id_to_con_id(self):
        wm = MagicMock()
        wm.dispatch_verb.return_value = {'ok': True}
        with patch(_WM, return_value=wm):
            status, payload = bridge_apps.invoke_apps('focus', {'window_id': 7})
        self.assertEqual(status, 200)
        verb = wm.dispatch_verb.call_args.args[0]
        args = wm.dispatch_verb.call_args.args[1]
        self.assertEqual(verb, 'window.focus')
        self.assertEqual(args['con_id'], 7)

    def test_close_routes_to_the_gated_window_close(self):
        wm = MagicMock()
        wm.dispatch_verb.return_value = {'ok': True}
        with patch(_WM, return_value=wm):
            status, payload = bridge_apps.invoke_apps('close', {'window_id': 9})
        self.assertEqual(status, 200)
        self.assertEqual(wm.dispatch_verb.call_args.args[0], 'window.close')
        self.assertEqual(wm.dispatch_verb.call_args.args[1]['con_id'], 9)

    def test_close_refusal_surfaces_500_not_masked_success(self):
        wm = MagicMock()
        wm.dispatch_verb.return_value = {'ok': False,
                                         'error': 'refused-by-constitution'}
        with patch(_WM, return_value=wm):
            status, payload = bridge_apps.invoke_apps('close', {'window_id': 9})
        self.assertEqual(status, 500)
        self.assertFalse(payload['ok'])

    def test_unknown_apps_op_is_400(self):
        status, payload = bridge_apps.invoke_apps('explode', {})
        self.assertEqual(status, 400)
        self.assertFalse(payload['ok'])


# ───────────────────────── /api/os/invoke dispatcher ──────────────────────────

class TestAppsInvokeRoute(unittest.TestCase):
    def setUp(self):
        abs_mod._service = None

    def test_invoke_launch_dispatches_gtk_launch_end_to_end(self):
        _app, client = _make_app()
        with patch(_ABS + '.subprocess.run',
                   return_value=_FakeRun(returncode=0)) as run:
            r = client.post('/api/os/invoke',
                            json={'domain': 'apps', 'op': 'launch',
                                  'params': {'app_id': 'firefox'}})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual(run.call_args.args[0], ['gtk-launch', 'firefox'])

    def test_invoke_launch_missing_app_id_is_400_by_contract(self):
        _app, client = _make_app()
        r = client.post('/api/os/invoke',
                        json={'domain': 'apps', 'op': 'launch', 'params': {}})
        self.assertEqual(r.status_code, 400)

    def test_invoke_list_routes_to_wm(self):
        _app, client = _make_app()
        wm = MagicMock()
        wm.dispatch_verb.return_value = {'ok': True, 'windows': []}
        with patch(_WM, return_value=wm):
            r = client.post('/api/os/invoke',
                            json={'domain': 'apps', 'op': 'list'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(wm.dispatch_verb.call_args.args[0], 'window.list')

    def test_invoke_unknown_apps_op_is_400(self):
        _app, client = _make_app()
        r = client.post('/api/os/invoke',
                        json={'domain': 'apps', 'op': 'nope'})
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()
