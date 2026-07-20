"""
Behavioural tests for the TYPED + NATIVE Shell<->OS bridge — power slice (#133 / W3).

These build the REAL contract / power handler / route module and drive them with the
NATIVE D-Bus boundary MOCKED (no bus on the dev box) — per the no-grep-tests rule
every test imports the real code, mocks the boundary, calls the real function, and
asserts the observable side-effects (which login1 method was invoked, the HTTP
shape, the result-check).

Covers:
  * each power op maps to the right login1 Manager method + interactive flag,
  * firmware_setup is a capability-gated two-step (arm then reboot; refuse on
    legacy BIOS; do NOT reboot if arming fails),
  * a denial is surfaced as a REAL error (500), never a masked success,
  * the typed /api/os/invoke dispatcher: validation, unknown op (400), planned
    domain (501), contract manifest,
  * the logind arg-translation + the shell_os_apis._logind_call delegation that
    "replaces the subprocess power route" without touching the off-limits files.
"""

import threading
import time
import unittest
from unittest.mock import patch

from integrations.agent_engine.os_bridge import contract as bridge_contract
from integrations.agent_engine.os_bridge import power as bridge_power
from integrations.agent_engine.os_bridge import logind as bridge_logind
from integrations.agent_engine.os_bridge.routes import register_os_bridge_routes

_POWER = 'integrations.agent_engine.os_bridge.power'
_LOGIND = 'integrations.agent_engine.os_bridge.logind'
_OS_APIS = 'integrations.agent_engine.shell_os_apis'


def _make_app():
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    register_os_bridge_routes(app)
    return app, app.test_client()


# ───────────────────────── power handler: op -> logind method ─────────────────

class TestPowerOpMapping(unittest.TestCase):
    def test_single_step_ops_map_to_login1_methods(self):
        expected = {'reboot': 'Reboot', 'shutdown': 'PowerOff',
                    'suspend': 'Suspend', 'hibernate': 'Hibernate'}
        for op, method in expected.items():
            with patch(f'{_POWER}.logind_call', return_value=(True, None)) as lc:
                status, payload = bridge_power.invoke_power(op)
            self.assertEqual(status, 200, op)
            self.assertTrue(payload['ok'], op)
            # method + interactive boolean 'true' so logind may consult polkit.
            self.assertEqual(lc.call_args.args[0], method, op)
            self.assertEqual(lc.call_args.args[1], ('b', 'true'), op)

    def test_lock_maps_to_locksessions_no_arg(self):
        with patch(f'{_POWER}.logind_call', return_value=(True, None)) as lc:
            status, payload = bridge_power.invoke_power('lock')
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        self.assertEqual(lc.call_args.args, ('LockSessions',))

    def test_unknown_op_is_400(self):
        with patch(f'{_POWER}.logind_call', return_value=(True, None)) as lc:
            status, payload = bridge_power.invoke_power('explode')
        self.assertEqual(status, 400)
        self.assertFalse(payload['ok'])
        lc.assert_not_called()


# ───────────────────────── result-check: denial is a REAL error ───────────────

class TestPowerResultCheck(unittest.TestCase):
    def test_denial_surfaces_real_error_not_masked_success(self):
        with patch(f'{_POWER}.logind_call',
                   return_value=(False, 'Access denied')):
            status, payload = bridge_power.invoke_power('reboot')
        self.assertEqual(status, 500)
        self.assertFalse(payload['ok'])
        self.assertIn('Access denied', payload['error'])


# ───────────────────────── firmware_setup: gated two-step ─────────────────────

class TestFirmwareSetup(unittest.TestCase):
    def test_capable_arms_then_reboots(self):
        with patch(f'{_OS_APIS}.firmware_setup_supported', return_value=True), \
                patch(f'{_POWER}.logind_call', return_value=(True, None)) as lc:
            status, payload = bridge_power.invoke_power('firmware_setup')
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        methods = [c.args[0] for c in lc.call_args_list]
        self.assertEqual(methods, ['SetRebootToFirmwareSetup', 'Reboot'])

    def test_legacy_bios_refused_and_never_touches_logind(self):
        with patch(f'{_OS_APIS}.firmware_setup_supported', return_value=False), \
                patch(f'{_POWER}.logind_call', return_value=(True, None)) as lc:
            status, payload = bridge_power.invoke_power('firmware_setup')
        self.assertEqual(status, 400)
        self.assertFalse(payload['ok'])
        lc.assert_not_called()

    def test_arm_failure_does_not_reboot(self):
        with patch(f'{_OS_APIS}.firmware_setup_supported', return_value=True), \
                patch(f'{_POWER}.logind_call',
                      return_value=(False, 'denied')) as lc:
            status, payload = bridge_power.invoke_power('firmware_setup')
        self.assertEqual(status, 500)
        self.assertIn('denied', payload['error'])
        methods = [c.args[0] for c in lc.call_args_list]
        # Only the arm was attempted; Reboot must NOT follow a failed arm.
        self.assertEqual(methods, ['SetRebootToFirmwareSetup'])


# ───────────────────────── capabilities probe ─────────────────────────────────

class TestPowerCapabilities(unittest.TestCase):
    def test_firmware_capability_reflects_probe(self):
        with patch(f'{_OS_APIS}.firmware_setup_supported', return_value=True):
            caps = bridge_power.power_capabilities()
        self.assertTrue(caps['firmware_setup'])
        self.assertTrue(caps['reboot'])
        with patch(f'{_OS_APIS}.firmware_setup_supported', return_value=False):
            caps = bridge_power.power_capabilities()
        self.assertFalse(caps['firmware_setup'])


# ───────────────────────── typed contract ─────────────────────────────────────

class TestContract(unittest.TestCase):
    def test_get_op_only_returns_implemented(self):
        self.assertIsNotNone(bridge_contract.get_op('power', 'reboot'))
        self.assertIsNone(bridge_contract.get_op('power', 'nope'))
        # disk is DECLARED planned but not implemented -> get_op is None, is_planned True.
        self.assertIsNone(bridge_contract.get_op('disk', 'mount'))
        self.assertTrue(bridge_contract.is_planned('disk'))
        self.assertFalse(bridge_contract.is_planned('power'))
        self.assertTrue(bridge_contract.is_implemented('power'))

    def test_validate_params_rejects_unknown_keys(self):
        spec = bridge_contract.get_op('power', 'reboot')
        ok, cleaned = bridge_contract.validate_params(spec, {})
        self.assertTrue(ok)
        self.assertEqual(cleaned, {})
        ok, err = bridge_contract.validate_params(spec, {'force': True})
        self.assertFalse(ok)
        self.assertIn('unknown param', err)

    def test_describe_contract_lists_power_impl_and_planned_next_slices(self):
        manifest = bridge_contract.describe_contract()
        domains = manifest['domains']
        self.assertTrue(domains['power']['implemented'])
        power_ops = {o['name'] for o in domains['power']['ops']}
        self.assertEqual(power_ops,
                         {'reboot', 'shutdown', 'suspend', 'hibernate',
                          'lock', 'firmware_setup'})
        for planned in ('disk', 'network', 'display'):
            self.assertFalse(domains[planned]['implemented'], planned)
            self.assertTrue(domains[planned].get('planned'), planned)


# ───────────────────────── /api/os/invoke dispatcher ──────────────────────────

class TestInvokeRoute(unittest.TestCase):
    def test_invoke_reboot_dispatches_native_power(self):
        _app, client = _make_app()
        with patch(f'{_POWER}.logind_call', return_value=(True, None)) as lc:
            r = client.post('/api/os/invoke',
                            json={'domain': 'power', 'op': 'reboot'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual(lc.call_args.args[0], 'Reboot')

    def test_invoke_denial_is_500(self):
        _app, client = _make_app()
        with patch(f'{_POWER}.logind_call',
                   return_value=(False, 'Interactive authentication required')):
            r = client.post('/api/os/invoke',
                            json={'domain': 'power', 'op': 'shutdown'})
        self.assertEqual(r.status_code, 500)
        self.assertFalse(r.get_json()['ok'])

    def test_invoke_unknown_op_is_400(self):
        _app, client = _make_app()
        r = client.post('/api/os/invoke', json={'domain': 'power', 'op': 'nope'})
        self.assertEqual(r.status_code, 400)

    def test_invoke_planned_domain_is_501(self):
        _app, client = _make_app()
        r = client.post('/api/os/invoke', json={'domain': 'disk', 'op': 'mount'})
        self.assertEqual(r.status_code, 501)
        self.assertIn('planned', r.get_json()['error'])

    def test_invoke_bad_params_is_400(self):
        _app, client = _make_app()
        r = client.post('/api/os/invoke',
                        json={'domain': 'power', 'op': 'reboot',
                              'params': {'force': True}})
        self.assertEqual(r.status_code, 400)

    def test_contract_and_capabilities_endpoints(self):
        _app, client = _make_app()
        r = client.get('/api/os/contract')
        self.assertEqual(r.status_code, 200)
        self.assertIn('power', r.get_json()['domains'])
        with patch(f'{_OS_APIS}.firmware_setup_supported', return_value=False):
            r = client.get('/api/os/power/capabilities')
        self.assertEqual(r.status_code, 200)
        self.assertIn('reboot', r.get_json()['capabilities'])


# ───────────────────────── logind: native translation + busctl fallback ───────

class TestLogindTranslation(unittest.TestCase):
    def test_busctl_args_to_body_supported_shapes(self):
        self.assertEqual(bridge_logind._busctl_args_to_body(()), ('', ()))
        self.assertEqual(bridge_logind._busctl_args_to_body(('b', 'true')),
                         ('b', (True,)))
        self.assertEqual(bridge_logind._busctl_args_to_body(('b', 'false')),
                         ('b', (False,)))
        self.assertEqual(bridge_logind._busctl_args_to_body(('s', 'sess-1')),
                         ('s', ('sess-1',)))

    def test_busctl_args_to_body_rejects_unsupported_signature(self):
        with self.assertRaises(ValueError):
            bridge_logind._busctl_args_to_body(('u', '3'))


class TestBusctlFallback(unittest.TestCase):
    def _fake_run(self, returncode=0, stderr='', stdout=''):
        class _R:
            pass
        r = _R()
        r.returncode = returncode
        r.stderr = stderr
        r.stdout = stdout
        return r

    def test_native_unavailable_falls_back_to_busctl_success(self):
        # Force the native transport off, then a successful busctl exit -> (True, None).
        with patch(f'{_LOGIND}._NATIVE_AVAILABLE', False), \
                patch(f'{_LOGIND}.subprocess.run',
                      return_value=self._fake_run(returncode=0)) as run:
            ok, err = bridge_logind.logind_call('Reboot', ('b', 'true'))
        self.assertTrue(ok)
        self.assertIsNone(err)
        # It really shelled to busctl call --system ... Reboot b true.
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ['busctl', 'call', '--system'])
        self.assertIn('Reboot', argv)

    def test_native_unavailable_busctl_denial_is_real_error(self):
        with patch(f'{_LOGIND}._NATIVE_AVAILABLE', False), \
                patch(f'{_LOGIND}.subprocess.run',
                      return_value=self._fake_run(returncode=1,
                                                  stderr='Access denied')):
            ok, err = bridge_logind.logind_call('PowerOff', ('b', 'true'))
        self.assertFalse(ok)
        self.assertIn('Access denied', err)

    def test_native_success_short_circuits_busctl(self):
        # Native path returns authoritative success -> busctl (subprocess.run) is
        # NEVER called.
        with patch(f'{_LOGIND}._NATIVE_AVAILABLE', True), \
                patch(f'{_LOGIND}._native_logind_call',
                      return_value=(True, None)), \
                patch(f'{_LOGIND}.subprocess.run') as run:
            ok, err = bridge_logind.logind_call('Reboot', ('b', 'true'))
        self.assertTrue(ok)
        run.assert_not_called()

    def test_native_denial_is_authoritative_no_busctl_fallback(self):
        # A REAL method error from native must NOT fall back to busctl.
        with patch(f'{_LOGIND}._NATIVE_AVAILABLE', True), \
                patch(f'{_LOGIND}._native_logind_call',
                      return_value=(False, 'denied')), \
                patch(f'{_LOGIND}.subprocess.run') as run:
            ok, err = bridge_logind.logind_call('Reboot', ('b', 'true'))
        self.assertFalse(ok)
        self.assertEqual(err, 'denied')
        run.assert_not_called()

    def test_native_transport_unavailable_falls_through_to_busctl(self):
        # _native_logind_call returns None (transport down) -> busctl runs.
        with patch(f'{_LOGIND}._NATIVE_AVAILABLE', True), \
                patch(f'{_LOGIND}._native_logind_call', return_value=None), \
                patch(f'{_LOGIND}.subprocess.run',
                      return_value=self._fake_run(returncode=0)) as run:
            ok, err = bridge_logind.logind_call('Suspend', ('b', 'true'))
        self.assertTrue(ok)
        run.assert_called_once()


class TestNativeNoHang(unittest.TestCase):
    """The hang-free baseline: a wedged system bus can never pin the shell pool."""

    def test_hung_native_call_is_bounded_and_does_not_also_run_busctl(self):
        # Simulate a native D-Bus call that never returns (a hung/unresponsive bus).
        # logind_call must BOUND it and return (False, timed out) quickly, WITHOUT
        # then running busctl (which would wedge the same way) — so the 1-2 thread
        # shell pool is never pinned. The worker is a daemon thread, so the lingering
        # sleep cannot block the test process either.
        entered = threading.Event()

        def _hang(*_a, **_k):
            entered.set()
            time.sleep(30)  # far longer than the bound; daemon thread -> non-blocking

        with patch(f'{_LOGIND}._NATIVE_AVAILABLE', True), \
                patch(f'{_LOGIND}._native_logind_call', side_effect=_hang), \
                patch(f'{_LOGIND}.subprocess.run') as run:
            t0 = time.time()
            ok, err = bridge_logind.logind_call('Reboot', ('b', 'true'), timeout=1)
            elapsed = time.time() - t0

        self.assertTrue(entered.is_set(), 'the native worker actually started')
        self.assertFalse(ok)
        self.assertIn('timed out', err)
        # The bound (timeout + 2) is ~3s — must return well before the 30s hang.
        self.assertLess(elapsed, 15, 'logind_call returned bounded, not after 30s')
        # A native timeout is authoritative: do NOT fall back to a busctl that would
        # hang identically.
        run.assert_not_called()


# ───────────── shell_os_apis._logind_call delegation (the migration wiring) ────

class TestShellOsApisDelegation(unittest.TestCase):
    def test_shell_logind_call_delegates_to_native_client(self):
        """The existing `_logind_call` (imported by liquid_ui_service + the shell
        power route) now delegates to the ONE native client — the subprocess power
        path is replaced without touching the off-limits files."""
        import integrations.agent_engine.shell_os_apis as os_apis
        with patch(f'{_LOGIND}.logind_call', return_value=(True, None)) as lc:
            ok, err = os_apis._logind_call('Reboot', 'b', 'true')
        self.assertTrue(ok)
        self.assertIsNone(err)
        # busctl-form args are forwarded as the tuple the native client expects.
        self.assertEqual(lc.call_args.args[0], 'Reboot')
        self.assertEqual(lc.call_args.args[1], ('b', 'true'))


if __name__ == '__main__':
    unittest.main()
