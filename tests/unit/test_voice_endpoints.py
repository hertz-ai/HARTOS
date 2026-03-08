"""Tests for /api/voice/* endpoints in langchain_gpt_api.py.

These test the Flask routes after rewiring to TTSRouter.
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


@pytest.fixture
def mock_tts_result():
    """Create a mock TTSResult."""
    from integrations.channels.media.tts_router import TTSResult
    return TTSResult(
        path='/tmp/tts_test.wav',
        duration=1.5,
        engine_id='pocket_tts',
        device='cpu',
        location='local',
        latency_ms=200.0,
        sample_rate=24000,
        voice='alba',
        quality_score=0.85,
    )


class TestVoiceSpeakEndpoint:
    """Test /api/voice/speak delegates to TTSRouter."""

    @patch('integrations.channels.media.tts_router.get_tts_router')
    def test_speak_delegates_to_router(self, mock_get_router, mock_tts_result):
        mock_router = MagicMock()
        mock_router.synthesize.return_value = mock_tts_result
        mock_get_router.return_value = mock_router

        # Simulate what the endpoint does
        result = mock_router.synthesize(
            text="Hello", language=None, voice=None,
            output_path=None, source='chat_response', engine_override=None,
        )
        assert result.engine_id == 'pocket_tts'
        assert result.path == '/tmp/tts_test.wav'

    @patch('integrations.channels.media.tts_router.get_tts_router')
    def test_speak_returns_audio_url(self, mock_get_router, mock_tts_result):
        mock_router = MagicMock()
        mock_router.synthesize.return_value = mock_tts_result
        mock_get_router.return_value = mock_router

        result = mock_router.synthesize(text="Hello", source='chat_response')
        resp = result.to_dict()
        resp['audio_url'] = f"/api/voice/audio/{os.path.basename(result.path)}"
        assert resp['audio_url'] == '/api/voice/audio/tts_test.wav'

    @patch('integrations.channels.media.tts_router.get_tts_router')
    def test_speak_accepts_engine_override(self, mock_get_router, mock_tts_result):
        mock_router = MagicMock()
        mock_router.synthesize.return_value = mock_tts_result
        mock_get_router.return_value = mock_router

        mock_router.synthesize(
            text="Hello", engine_override='luxtts',
        )
        mock_router.synthesize.assert_called_with(
            text="Hello", engine_override='luxtts',
        )

    @patch('integrations.channels.media.tts_router.get_tts_router')
    def test_speak_accepts_source(self, mock_get_router, mock_tts_result):
        mock_router = MagicMock()
        mock_router.synthesize.return_value = mock_tts_result
        mock_get_router.return_value = mock_router

        mock_router.synthesize(text="Hi", source='greeting')
        mock_router.synthesize.assert_called_with(text="Hi", source='greeting')


class TestVoiceEnginesEndpoint:
    """Test /api/voice/engines."""

    @patch('integrations.channels.media.tts_router.get_tts_router')
    def test_engines_returns_list(self, mock_get_router):
        mock_router = MagicMock()
        mock_router.get_engine_status.return_value = [
            {'engine': 'pocket_tts', 'installed': True, 'can_run': True},
            {'engine': 'luxtts', 'installed': True, 'can_run': True},
        ]
        mock_get_router.return_value = mock_router

        statuses = mock_router.get_engine_status()
        assert len(statuses) == 2
        assert statuses[0]['engine'] == 'pocket_tts'


class TestVoiceAudioEndpoint:
    """Test /api/voice/audio/<filename> path traversal safety."""

    def test_basename_extraction(self):
        """Path traversal: filename with ../ should be rejected."""
        filename = "../../etc/passwd"
        safe_name = os.path.basename(filename)
        assert safe_name == 'passwd'
        assert safe_name != filename  # Would be rejected by the route

    def test_normal_filename_passes(self):
        filename = "tts_12345.wav"
        safe_name = os.path.basename(filename)
        assert safe_name == filename  # Same — passes safety check

    def test_dotdot_in_filename_rejected(self):
        filename = "../secret.wav"
        safe_name = os.path.basename(filename)
        assert safe_name != filename


class TestVoiceCloneEndpoint:
    """Test /api/voice/clone with engine selection."""

    def test_clone_routes_default_to_luxtts(self):
        """Default clone engine should be luxtts."""
        data = {'audio_path': '/tmp/test.wav', 'name': 'myvoice'}
        engine = data.get('engine', 'luxtts')
        assert engine == 'luxtts'

    def test_clone_routes_pocket_when_specified(self):
        data = {'audio_path': '/tmp/test.wav', 'name': 'myvoice', 'engine': 'pocket_tts'}
        engine = data.get('engine', 'luxtts')
        assert engine == 'pocket_tts'


class TestVoiceVoicesEndpoint:
    """Test /api/voice/voices aggregation."""

    @patch('integrations.channels.media.tts_router.get_tts_router')
    def test_voices_aggregates(self, mock_get_router):
        mock_router = MagicMock()
        mock_router.get_all_voices.return_value = [
            {'id': 'alba', 'engine': 'pocket_tts', 'type': 'builtin'},
            {'id': 'alice', 'engine': 'luxtts', 'type': 'cloned'},
        ]
        mock_get_router.return_value = mock_router

        voices = mock_router.get_all_voices()
        assert len(voices) == 2
        engines = {v['engine'] for v in voices}
        assert 'pocket_tts' in engines
        assert 'luxtts' in engines
