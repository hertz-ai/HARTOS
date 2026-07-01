"""Behavioural tests for the Model Bus realtime-voice wiring (#123 / W9).

These drive the REAL ModelBusService methods with the inference *backends*
mocked at their import boundary, and assert the observable routing side-effects
(which backend was called, with what audio/text, and how the turn is composed) —
never source substrings (CLAUDE.md Gate 5 / feedback_no_grep_tests).

Covered:
  * _route_stt  dispatches the audio to whisper_transcribe and surfaces the text.
  * _route_tts  dispatches the text to the TTS router and surfaces the audio path.
  * voice_turn  chains STT -> agent (LLM) -> TTS through the SAME routes, composes
                {transcript, response, audio_path}, and fails cleanly per stage.
  * the Unix-socket op 'voice_turn' reaches voice_turn (native/robot clients).
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.agent_engine.model_bus_service import ModelBusService
from integrations.service_tools.model_catalog import ModelType


@pytest.fixture
def bus():
    return ModelBusService()


# ─── _route_stt: audio -> registered whisper backend -> text ────────────────

class TestRouteStt:
    def test_stt_dispatches_audio_to_whisper(self, bus):
        with patch('integrations.service_tools.whisper_tool.whisper_transcribe') as m:
            m.return_value = json.dumps({'text': 'turn on the lights',
                                         'language': 'en'})
            result = bus._route_stt('', {'audio_path': '/tmp/utt.wav',
                                         'language': 'en'})
        # Routed the real recorded file (not the empty prompt) to the backend.
        m.assert_called_once()
        assert m.call_args[0][0] == '/tmp/utt.wav'
        assert result['response'] == 'turn on the lights'
        assert result['backend'] == 'local_stt'
        assert result['language'] == 'en'

    def test_stt_falls_back_to_prompt_as_audio_path(self, bus):
        # When no explicit audio_path, the prompt IS the path (the /v1/stt route
        # passes it that way) — assert the backend still gets a real path.
        with patch('integrations.service_tools.whisper_tool.whisper_transcribe') as m:
            m.return_value = json.dumps({'text': 'hi'})
            bus._route_stt('/tmp/only.wav', {})
        assert m.call_args[0][0] == '/tmp/only.wav'

    def test_stt_surfaces_backend_error(self, bus):
        with patch('integrations.service_tools.whisper_tool.whisper_transcribe') as m:
            m.return_value = json.dumps({'error': 'no model'})
            result = bus._route_stt('', {'audio_path': '/tmp/x.wav'})
        assert 'error' in result and result['response'] is None


# ─── _route_tts: text -> registered TTS router -> audio path ────────────────

class TestRouteTts:
    def _mock_result(self, **over):
        r = MagicMock()
        r.error = over.get('error', None)
        r.path = over.get('path', '/tmp/reply.wav')
        r.engine_id = over.get('engine_id', 'pocket_tts')
        r.location = over.get('location', 'local')
        r.duration = over.get('duration', 1.2)
        r.latency_ms = over.get('latency_ms', 180.0)
        r.device = over.get('device', 'cpu')
        return r

    def test_tts_dispatches_text_to_router(self, bus):
        router = MagicMock()
        router.synthesize.return_value = self._mock_result()
        with patch('integrations.channels.media.tts_router.get_tts_router',
                   return_value=router):
            result = bus._route_tts('Lights are on now',
                                    {'language': 'en', 'voice': 'alba'})
        # The router got the reply text + forwarded options.
        router.synthesize.assert_called_once()
        kwargs = router.synthesize.call_args.kwargs
        assert kwargs['text'] == 'Lights are on now'
        assert kwargs['language'] == 'en'
        assert kwargs['voice'] == 'alba'
        assert result['response'] == '/tmp/reply.wav'
        assert result['backend'] == 'local_tts'

    def test_tts_router_error_falls_back_to_pocket(self, bus):
        router = MagicMock()
        router.synthesize.return_value = self._mock_result(error='engine down')
        with patch('integrations.channels.media.tts_router.get_tts_router',
                   return_value=router), \
             patch.object(bus, '_try_pocket_tts',
                          return_value={'response': '/tmp/pk.wav',
                                        'backend': 'local_tts'}) as pk:
            result = bus._route_tts('hello', {})
        pk.assert_called_once()
        assert result['response'] == '/tmp/pk.wav'


# ─── voice_turn: full spoken turn composed from the three routes ────────────

class TestVoiceTurn:
    def _wire(self, bus, stt=None, llm=None, tts=None):
        """Patch the three routes + open the guardrail so we test orchestration."""
        bus._route_stt = MagicMock(return_value=stt or {
            'response': 'what is the weather', 'model': 'whisper-stt-local',
            'backend': 'local_stt'})
        bus._route_llm = MagicMock(return_value=llm or {
            'response': 'It is sunny and warm.', 'model': 'llama',
            'backend': 'local_llm'})
        bus._route_tts = MagicMock(return_value=tts or {
            'response': '/tmp/out.wav', 'model': 'pocket-tts',
            'backend': 'local_tts'})
        bus._check_guardrails = MagicMock(return_value=True)

    def test_full_turn_chains_stt_agent_tts(self, bus):
        self._wire(bus)
        result = bus.voice_turn('/tmp/utt.wav',
                                {'user_id': 'u1', 'language': 'en'})
        # 1) STT got the recorded audio.
        stt_opts = bus._route_stt.call_args[0][1]
        assert stt_opts['audio_path'] == '/tmp/utt.wav'
        # 2) The agent got the TRANSCRIPT as its prompt.
        assert bus._route_llm.call_args[0][0] == 'what is the weather'
        # 3) TTS got the AGENT REPLY as its text, never the input audio path.
        assert bus._route_tts.call_args[0][0] == 'It is sunny and warm.'
        assert 'audio_path' not in bus._route_tts.call_args[0][1]
        # Composed result carries all three stages.
        assert result['transcript'] == 'what is the weather'
        assert result['response'] == 'It is sunny and warm.'
        assert result['audio_path'] == '/tmp/out.wav'
        assert result['stt_model'] == 'whisper-stt-local'
        assert result['tts_model'] == 'pocket-tts'
        assert 'error' not in result

    def test_missing_audio_short_circuits(self, bus):
        self._wire(bus)
        result = bus.voice_turn('', {})
        assert result['error'] and result['stage'] == 'stt'
        bus._route_stt.assert_not_called()

    def test_stt_failure_stops_before_agent(self, bus):
        self._wire(bus, stt={'error': 'no speech', 'response': None})
        result = bus.voice_turn('/tmp/x.wav', {})
        assert result['stage'] == 'stt'
        bus._route_llm.assert_not_called()
        bus._route_tts.assert_not_called()

    def test_empty_transcript_stops_before_agent(self, bus):
        self._wire(bus, stt={'response': '   ', 'backend': 'local_stt'})
        result = bus.voice_turn('/tmp/x.wav', {})
        assert result['stage'] == 'stt'
        bus._route_llm.assert_not_called()

    def test_agent_failure_stops_before_tts(self, bus):
        self._wire(bus, llm={'error': 'no backend', 'response': None})
        result = bus.voice_turn('/tmp/x.wav', {})
        assert result['stage'] == 'agent'
        assert result['transcript'] == 'what is the weather'
        bus._route_tts.assert_not_called()

    def test_tts_failure_still_returns_text_turn(self, bus):
        self._wire(bus, tts={'error': 'tts engine down'})
        result = bus.voice_turn('/tmp/x.wav', {})
        # The turn SUCCEEDS (recognition + text reply survive); only audio is empty.
        assert 'error' not in result
        assert result['response'] == 'It is sunny and warm.'
        assert result['audio_path'] == ''
        assert result['tts_error'] == 'tts engine down'

    def test_text_only_turn_skips_tts(self, bus):
        self._wire(bus)
        result = bus.voice_turn('/tmp/x.wav', {'speak': False})
        bus._route_tts.assert_not_called()
        assert result['response'] == 'It is sunny and warm.'
        assert result['audio_path'] == ''


# ─── Unix-socket transport op reaches the same voice_turn (native clients) ──

class TestSocketVoiceOp:
    def test_socket_op_dispatches_to_voice_turn(self, bus):
        with patch.object(bus, 'voice_turn',
                          return_value={'transcript': 't', 'response': 'r'}) as m:
            out = bus._handle_socket_request(
                {'op': 'voice_turn', 'audio_path': '/tmp/a.wav',
                 'options': {'language': 'en'}})
        m.assert_called_once_with(audio_path='/tmp/a.wav',
                                  options={'language': 'en'})
        assert out['response'] == 'r'
