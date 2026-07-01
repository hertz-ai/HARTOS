"""
Behavioral tests: the Model Bus actually SERVES a modality through infer().

The socket-transport tests (test_model_bus_socket.py) prove the wire framing +
dispatch by MOCKING infer() wholesale. The probe tests (test_model_bus_probe.py)
drive the bus over HTTP with fake clients. Neither proves the ROUTING itself —
that a real infer() call walks the health-gated backend list, reaches a backend,
and returns a served answer (and degrades, never crashes, when nothing can
serve). That is what an app or a robot depends on when it asks the bus for
"intelligence", so it needs its own behavioral proof.

These tests call the REAL ModelBusService.infer() / _route_llm / _route_tts /
list_models with the network boundary (pooled_post) and the TTS engine mocked,
asserting the served shape, the guardrail gate, and every degrade path.
"""
import unittest
from unittest.mock import MagicMock, patch

from integrations.agent_engine import model_bus_service as mbs
from integrations.agent_engine.model_bus_service import ModelBusService
from integrations.service_tools.model_catalog import ModelType


class _FakeResp:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json


class _TTSResult:
    """Mimics the TTSRouter result object the bus unpacks."""
    def __init__(self, error=None, path='/run/hart/out.wav', engine_id='pocket',
                 location='local', duration=1.2, latency_ms=42, device='cpu'):
        self.error = error
        self.path = path
        self.engine_id = engine_id
        self.location = location
        self.duration = duration
        self.latency_ms = latency_ms
        self.device = device


class TestServesLLM(unittest.TestCase):
    """The bus routes an LLM request to a healthy backend and returns the answer."""

    def setUp(self):
        self.svc = ModelBusService()
        # A discovered, healthy local LLM backend.
        self.svc._backends = {
            'llm': {'type': ModelType.LLM, 'url': 'http://llm',
                    'status': 'ready', 'local': True},
        }

    def test_llm_modality_is_served(self):
        fake = _FakeResp(200, {
            'choices': [{'message': {'content': 'Paris'}}],
            'model': 'llama-local',
        })
        with patch.object(self.svc, '_is_backend_alive', return_value=True), \
             patch.object(self.svc, '_check_guardrails', return_value=True), \
             patch('core.http_pool.pooled_post', return_value=fake):
            out = self.svc.infer(ModelType.LLM, 'capital of France?')
        self.assertEqual(out['response'], 'Paris')
        self.assertEqual(out['backend'], 'local_llm')
        self.assertEqual(out['model'], 'llama-local')
        # infer() stamps latency on every served answer.
        self.assertIn('latency_ms', out)

    def test_all_llm_backends_dead_degrades_to_error_not_crash(self):
        # The one backend is "alive" per cache but the actual call fails; the bus
        # marks it dead, exhausts the list, and returns an honest error object —
        # never raises.
        with patch.object(self.svc, '_is_backend_alive', return_value=True), \
             patch.object(self.svc, '_check_guardrails', return_value=True), \
             patch.object(self.svc, 'discover_backends', return_value={}), \
             patch('core.http_pool.pooled_post',
                   side_effect=ConnectionError('refused')):
            out = self.svc.infer(ModelType.LLM, 'hello')
        self.assertIn('error', out)
        self.assertIsNone(out.get('response'))

    def test_guardrail_block_is_honored_before_routing(self):
        # A blocked prompt must never reach a backend.
        with patch.object(self.svc, '_check_guardrails', return_value=False), \
             patch('core.http_pool.pooled_post') as post:
            out = self.svc.infer(ModelType.LLM, 'do something forbidden')
        self.assertIn('error', out)
        post.assert_not_called()


class TestServesTTS(unittest.TestCase):
    """The bus routes a TTS request through the router and degrades safely."""

    def setUp(self):
        self.svc = ModelBusService()
        # Silence the crossbar routing-status publisher (no live UI in a unit test).
        self._patcher = patch.object(mbs, '_publish_routing_status')
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_tts_modality_is_served_via_router(self):
        router = MagicMock()
        router.synthesize.return_value = _TTSResult()
        with patch.object(self.svc, '_check_guardrails', return_value=True), \
             patch('integrations.channels.media.tts_router.get_tts_router',
                   return_value=router):
            out = self.svc.infer(ModelType.TTS, 'hello there')
        self.assertEqual(out['response'], '/run/hart/out.wav')
        self.assertEqual(out['backend'], 'local_tts')
        self.assertIn('latency_ms', out)
        router.synthesize.assert_called_once()

    def test_tts_router_unavailable_degrades_to_pocket_fallback(self):
        # The smart router blows up; the bus must fall back to the always-local
        # pocket engine, not crash.
        with patch.object(self.svc, '_check_guardrails', return_value=True), \
             patch('integrations.channels.media.tts_router.get_tts_router',
                   side_effect=RuntimeError('router down')), \
             patch.object(self.svc, '_try_pocket_tts',
                          return_value={'response': '/run/hart/pocket.wav',
                                        'backend': 'local_tts',
                                        'model': 'pocket-tts-100m'}) as pocket:
            out = self.svc.infer(ModelType.TTS, 'hello there')
        self.assertEqual(out['response'], '/run/hart/pocket.wav')
        pocket.assert_called_once()


class TestUnknownModalityDegrades(unittest.TestCase):
    def test_unknown_model_type_is_error_not_crash(self):
        svc = ModelBusService()
        with patch.object(svc, '_check_guardrails', return_value=True):
            out = svc.infer('telepathy', 'think at me')
        self.assertIn('error', out)


class TestAdvertisesModalities(unittest.TestCase):
    """list_models() always advertises the always-local TTS + STT modalities so a
    caller can discover the bus serves them even with no LLM/vision backend up."""

    def test_tts_and_stt_are_always_advertised(self):
        svc = ModelBusService()
        svc._backends = {}  # nothing discovered
        models = svc.list_models()
        types = {m.get('type') for m in models}
        self.assertIn(ModelType.TTS, types)
        self.assertIn(ModelType.STT, types)


if __name__ == '__main__':
    unittest.main()
