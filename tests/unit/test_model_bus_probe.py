"""
Behavioral tests for the robot Model-Bus capability probe.

The probe is the embodied twin of hart-compat-smoketest: it MEASURES whether a
robot can actually reach the core intelligences over the Model Bus and writes an
honest per-capability status file. These tests drive the HTTP-facing probes with
injected fake clients (no live bus) and assert the honest verdicts + that the
probe writes the status file + never raises when the bus is down. The two
in-process probes (VLA catalog / robot fusion API) are stubbed here so the HTTP
routing is deterministic; they are exercised for real in the nixosTest / an
integration run.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from integrations.robotics import model_bus_probe as mbp


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, raise_json=False):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError('not json')
        return self._json


def _stub_inprocess(vla='no-model', intel=('unavailable', 0)):
    """Patch the two in-process probes so HTTP assertions are deterministic."""
    return (
        patch.object(mbp, '_probe_vla', return_value=vla),
        patch.object(mbp, '_probe_intelligence_api', return_value=intel),
    )


class TestProbeHttpVerdicts(unittest.TestCase):

    def test_all_up_ok(self):
        def fake_get(url, timeout=None):
            if url.endswith('/health'):
                return _FakeResp(200, {'status': 'ok'})
            if url.endswith('/v1/models'):
                return _FakeResp(200, {'models': [{'id': 'v', 'type': 'vision'}]})
            return _FakeResp(404, {})

        def fake_post(url, json=None, timeout=None):
            return _FakeResp(200, {'response': 'pong', 'backend': 'local_llm'})

        p_vla, p_intel = _stub_inprocess(vla='ready', intel=('ok', 3))
        with p_vla, p_intel:
            out = mbp.probe(status_path=None, base_url='http://localhost:6790',
                            http_get=fake_get, http_post=fake_post)
        self.assertEqual(out['model_bus'], 'ok')
        self.assertEqual(out['llm'], 'ok')
        self.assertEqual(out['vision'], 'ready')
        self.assertEqual(out['vla'], 'ready')
        self.assertEqual(out['intelligence_api'], 'ok')
        self.assertEqual(out['robots'], '3')

    def test_bus_answers_but_no_llm_backend(self):
        def fake_get(url, timeout=None):
            if url.endswith('/health'):
                return _FakeResp(200, {})
            if url.endswith('/v1/models'):
                return _FakeResp(200, {'models': [{'id': 'l', 'type': 'llm'}]})
            return _FakeResp(404, {})

        def fake_post(url, json=None, timeout=None):
            # The bus's honest "nothing can serve" shape.
            return _FakeResp(200, {'error': 'No LLM backend available',
                                   'response': None})

        p_vla, p_intel = _stub_inprocess()
        with p_vla, p_intel:
            out = mbp.probe(status_path=None, http_get=fake_get,
                            http_post=fake_post)
        self.assertEqual(out['model_bus'], 'ok')
        self.assertEqual(out['llm'], 'no-model')
        # models reachable but no vision entry present
        self.assertEqual(out['vision'], 'no-model')

    def test_bus_down_all_transport_probes_degrade(self):
        def fake_get(url, timeout=None):
            raise ConnectionError('refused')

        def fake_post(url, json=None, timeout=None):
            raise ConnectionError('refused')

        p_vla, p_intel = _stub_inprocess()
        with p_vla, p_intel:
            out = mbp.probe(status_path=None, http_get=fake_get,
                            http_post=fake_post)
        self.assertEqual(out['model_bus'], 'down')
        # llm/vision are SKIPPED when the bus is down -> honest 'down', not a crash
        self.assertEqual(out['llm'], 'down')
        self.assertEqual(out['vision'], 'down')

    def test_never_raises_on_broken_clients(self):
        def boom(*a, **k):
            raise RuntimeError('kaboom')

        p_vla, p_intel = _stub_inprocess()
        with p_vla, p_intel:
            # Must return a dict, never propagate the exception.
            out = mbp.probe(status_path=None, http_get=boom, http_post=boom)
        self.assertIsInstance(out, dict)
        self.assertEqual(out['model_bus'], 'down')


class TestProbeStatusFile(unittest.TestCase):

    def test_writes_honest_key_value_lines(self):
        def fake_get(url, timeout=None):
            if url.endswith('/health'):
                return _FakeResp(200, {})
            if url.endswith('/v1/models'):
                return _FakeResp(200, {'models': [{'type': 'vision'}]})
            return _FakeResp(404, {})

        def fake_post(url, json=None, timeout=None):
            return _FakeResp(200, {'response': 'ok'})

        tmpdir = tempfile.mkdtemp()
        status_path = os.path.join(tmpdir, 'robot-capability-status')
        p_vla, p_intel = _stub_inprocess(vla='ready', intel=('ok', 0))
        with p_vla, p_intel:
            out = mbp.probe(status_path=status_path, http_get=fake_get,
                            http_post=fake_post)

        with open(status_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # Every returned verdict is on its own key=value line.
        for key, value in out.items():
            self.assertIn(f'{key}={value}', content)
        self.assertIn('model_bus=ok', content)
        self.assertIn('llm=ok', content)

    def test_status_write_failure_is_non_fatal(self):
        def fake_get(url, timeout=None):
            return _FakeResp(200, {})

        def fake_post(url, json=None, timeout=None):
            return _FakeResp(200, {'response': 'ok'})

        p_vla, p_intel = _stub_inprocess()
        with p_vla, p_intel:
            # An unwritable directory must not raise — the return still carries
            # the measurement.
            out = mbp.probe(
                status_path='/this/does/not/exist/and/is/unwritable/status',
                http_get=fake_get, http_post=fake_post)
        self.assertEqual(out['model_bus'], 'ok')


class TestProbeEntrypoint(unittest.TestCase):

    def test_main_always_returns_zero(self):
        # main() runs the real probe() (which resolves the pooled HTTP clients);
        # with no bus up it records honest fail-states and still returns 0. It
        # must never raise or exit non-zero (a measurement, never a gate).
        with patch.object(mbp, 'probe', return_value={'model_bus': 'down'}):
            self.assertEqual(mbp.main(), 0)

    def test_main_swallows_probe_error(self):
        with patch.object(mbp, 'probe', side_effect=RuntimeError('x')):
            self.assertEqual(mbp.main(), 0)


if __name__ == '__main__':
    unittest.main()
