"""Unit tests for ``core.health_probe`` — covers Wave 2 bugs
#458 (probe_llm SRP / extra-call), #459 (HTTP-fidelity probe), and
#460 (port-resolver instead of hardcoded :5000 / :6778).

All HTTP calls are mocked via ``unittest.mock.patch`` on
``core.http_pool.pooled_get`` so tests run offline with zero net I/O.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


def _make_response(status_code: int = 200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    if json_body is not None:
        resp.json = MagicMock(return_value=json_body)
    return resp


class ProbeLlmTest(unittest.TestCase):
    """#458 + #459 — probe_llm uses HTTP-fidelity probe + does NOT
    issue a second models-list call by default."""

    def test_up_when_models_endpoint_returns_200(self):
        from core import health_probe
        with patch('core.port_registry.get_local_llm_url',
                   return_value='http://127.0.0.1:8080/v1'), \
             patch('core.http_pool.pooled_get',
                   return_value=_make_response(200, {'data': []})) as mocked:
            out = health_probe.probe_llm()
        self.assertEqual(out['status'], 'up')
        self.assertEqual(out['url'], 'http://127.0.0.1:8080/v1')
        # Single HTTP call — that's the SRP fix (#458).
        self.assertEqual(mocked.call_count, 1)
        called_url = mocked.call_args.args[0]
        # HTTP-fidelity probe goes to /v1/models (#459) — NOT a
        # naked /v1/.
        self.assertTrue(called_url.endswith('/v1/models'),
                        f'expected /v1/models, got {called_url!r}')

    def test_down_when_models_endpoint_returns_5xx(self):
        """#459 — port-bound-but-stuck servers return 5xx; we report
        them as ``down`` instead of ``up`` (which a TCP-only probe
        would have returned)."""
        from core import health_probe
        with patch('core.port_registry.get_local_llm_url',
                   return_value='http://127.0.0.1:8080/v1'), \
             patch('core.http_pool.pooled_get',
                   return_value=_make_response(503)):
            out = health_probe.probe_llm()
        self.assertEqual(out['status'], 'down')
        self.assertEqual(out.get('code'), 503)

    def test_down_when_pool_get_raises(self):
        from core import health_probe
        with patch('core.port_registry.get_local_llm_url',
                   return_value='http://127.0.0.1:8080/v1'), \
             patch('core.http_pool.pooled_get',
                   side_effect=ConnectionRefusedError('nope')):
            out = health_probe.probe_llm()
        self.assertEqual(out['status'], 'down')
        self.assertIn('nope', out.get('error', ''))

    def test_default_does_not_attach_models_payload(self):
        """#458 — by default the response body is discarded; no
        ``models`` key in the result.  Saves callers a JSON round-trip
        when they only need 'is it up?'.
        """
        from core import health_probe
        with patch('core.port_registry.get_local_llm_url',
                   return_value='http://127.0.0.1:8080/v1'), \
             patch('core.http_pool.pooled_get',
                   return_value=_make_response(
                       200, {'data': [{'id': 'qwen-4b'}]})):
            out = health_probe.probe_llm()
        self.assertNotIn('models', out)

    def test_include_models_true_attaches_payload(self):
        from core import health_probe
        with patch('core.port_registry.get_local_llm_url',
                   return_value='http://127.0.0.1:8080/v1'), \
             patch('core.http_pool.pooled_get',
                   return_value=_make_response(
                       200, {'data': [{'id': 'qwen-4b'},
                                      {'id': 'qwen-0.8b'}]})):
            out = health_probe.probe_llm(include_models=True)
        self.assertEqual(out.get('models'), ['qwen-4b', 'qwen-0.8b'])

    def test_probe_error_when_url_resolution_fails(self):
        from core import health_probe
        with patch('core.port_registry.get_local_llm_url',
                   side_effect=RuntimeError('config corrupt')):
            out = health_probe.probe_llm()
        self.assertEqual(out['status'], 'probe_error')
        self.assertIn('config corrupt', out['error'])


class ProbeNunbaFlaskTest(unittest.TestCase):
    """#460 — uses get_port('flask') instead of hardcoded 5000."""

    def test_up_uses_resolved_port(self):
        from core import health_probe
        with patch('core.port_registry.get_port',
                   return_value=5050) as mock_port, \
             patch('core.http_pool.pooled_get',
                   return_value=_make_response(200)) as mock_get:
            out = health_probe.probe_nunba_flask()
        self.assertEqual(out['status'], 'up')
        self.assertEqual(out['port'], 5050)
        # The resolver was called — and with 'flask' service name,
        # which proves we routed through the canonical port_registry
        # API.
        mock_port.assert_called_once_with('flask')
        # The HTTP call uses the RESOLVED port, not 5000.
        called_url = mock_get.call_args.args[0]
        self.assertIn(':5050/', called_url)
        self.assertNotIn(':5000/', called_url)

    def test_down_when_pool_get_raises(self):
        from core import health_probe
        with patch('core.port_registry.get_port', return_value=5000), \
             patch('core.http_pool.pooled_get',
                   side_effect=OSError('refused')):
            out = health_probe.probe_nunba_flask()
        self.assertEqual(out['status'], 'down')
        self.assertEqual(out['port'], 5000)


class ProbeLangchainTest(unittest.TestCase):
    """#460 — uses get_port('langchain') instead of hardcoded 6778, AND
    answers for the topology it is actually in.

    The two sidecar tests now pin ``is_bundled`` False explicitly.  They
    used to rely on the ambient environment being un-bundled, which made
    them silently env-dependent — a runner exporting NUNBA_BUNDLED would
    have flipped them.  Pinning is strictly more deterministic.
    """

    def test_up_uses_resolved_port(self):
        from core import health_probe
        with patch('core.config_cache.is_bundled', return_value=False), \
             patch('core.port_registry.get_port',
                   return_value=7000) as mock_port, \
             patch('core.http_pool.pooled_get',
                   return_value=_make_response(200)) as mock_get:
            out = health_probe.probe_langchain()
        self.assertEqual(out['status'], 'up')
        self.assertEqual(out['port'], 7000)
        self.assertEqual(out['mode'], 'sidecar')
        mock_port.assert_called_once_with('langchain')
        called_url = mock_get.call_args.args[0]
        self.assertIn(':7000/', called_url)
        self.assertNotIn(':6778/', called_url)

    def test_down_swallows_exception(self):
        """A refused dial is still 'down' — but ONLY in sidecar mode,
        where a port genuinely should be listening."""
        from core import health_probe
        with patch('core.config_cache.is_bundled', return_value=False), \
             patch('core.port_registry.get_port', return_value=6778), \
             patch('core.http_pool.pooled_get',
                   side_effect=ConnectionResetError('reset')):
            out = health_probe.probe_langchain()
        self.assertEqual(out['status'], 'down')

    # --- bundled mode: langchain is in-process, no port exists ---------

    def test_bundled_loaded_reports_up_without_dialing(self):
        """The defect: bundled mode has no langchain listener, so the old
        unconditional dial could only ever answer 'down' about a healthy
        in-process engine."""
        from core import health_probe
        with patch('core.config_cache.is_bundled', return_value=True), \
             patch('core.safe_hartos_attr.hartos_loaded', return_value=True), \
             patch('core.http_pool.pooled_get') as mock_get:
            out = health_probe.probe_langchain()
        self.assertEqual(out['status'], 'up')
        self.assertEqual(out['mode'], 'in_process')
        mock_get.assert_not_called()          # no socket may be touched

    def test_bundled_not_loaded_is_unknown_not_down(self):
        """Cross-process honesty.  A sys.modules read describes only THIS
        process; the stdio MCP server runs in another one.  Reporting
        'down' there would be a fresh-zero false negative — the exact
        shadow-module trap that made the 14h outage undiagnosable."""
        from core import health_probe
        with patch('core.config_cache.is_bundled', return_value=True), \
             patch('core.safe_hartos_attr.hartos_loaded', return_value=False):
            out = health_probe.probe_langchain()
        self.assertEqual(out['status'], 'unknown')
        self.assertNotEqual(out['status'], 'down')
        self.assertIn('reason', out)

    def test_bundled_never_reports_a_port(self):
        """There IS no langchain port in bundled mode.  Emitting one would
        invite a reader to dial it."""
        from core import health_probe
        for loaded in (True, False):
            with patch('core.config_cache.is_bundled', return_value=True), \
                 patch('core.safe_hartos_attr.hartos_loaded',
                       return_value=loaded):
                out = health_probe.probe_langchain()
            self.assertNotIn('port', out)

    def test_unknowable_topology_falls_back_to_sidecar(self):
        """If is_bundled() itself cannot be resolved, keep the historical
        behaviour rather than inventing a verdict."""
        from core import health_probe
        with patch('core.config_cache.is_bundled',
                   side_effect=RuntimeError('boom')), \
             patch('core.port_registry.get_port', return_value=6778), \
             patch('core.http_pool.pooled_get',
                   return_value=_make_response(200)):
            out = health_probe.probe_langchain()
        self.assertEqual(out['mode'], 'sidecar')
        self.assertEqual(out['status'], 'up')


class PortRegistryDriftGuardTest(unittest.TestCase):
    """#460 drift guard — flask + langchain MUST be in the canonical
    port registry.  If a future refactor accidentally removes them,
    health_probe falls back to port=0 and the probes silently report
    ``down`` against the wrong target.  This test fails fast on that
    regression."""

    def test_flask_in_app_ports(self):
        from core.port_registry import APP_PORTS
        self.assertIn('flask', APP_PORTS)
        self.assertEqual(APP_PORTS['flask'], 5000)

    def test_langchain_in_app_ports(self):
        from core.port_registry import APP_PORTS
        self.assertIn('langchain', APP_PORTS)
        self.assertEqual(APP_PORTS['langchain'], 6778)

    def test_flask_env_override_registered(self):
        from core.port_registry import ENV_OVERRIDES
        self.assertEqual(ENV_OVERRIDES.get('flask'), 'HART_FLASK_PORT')

    def test_langchain_env_override_registered(self):
        from core.port_registry import ENV_OVERRIDES
        self.assertEqual(ENV_OVERRIDES.get('langchain'),
                         'HART_LANGCHAIN_PORT')

    def test_os_ports_no_collisions_for_new_entries(self):
        """Defends against the OS-mode collision I almost shipped —
        'langchain': 677 would have collided with 'backend': 677."""
        from core.port_registry import OS_PORTS
        # Build (service → port) → (port → [services]) and assert no
        # port has more than one service.
        port_to_services: dict = {}
        for svc, port in OS_PORTS.items():
            port_to_services.setdefault(port, []).append(svc)
        collisions = {p: svcs for p, svcs in port_to_services.items()
                      if len(svcs) > 1}
        self.assertEqual(collisions, {},
                         f'OS_PORTS collisions: {collisions}')


if __name__ == '__main__':
    unittest.main()
