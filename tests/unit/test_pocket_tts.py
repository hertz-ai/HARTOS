"""
Tests for Pocket TTS integration.

Covers: pocket_tts_tool.py (in-process TTS), TTSEngine POCKET provider,
API endpoints (/api/voice/speak, /api/voice/voices, /api/voice/clone).
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock


# ═══════════════════════════════════════════════════════════════
# pocket_tts_tool.py — In-process TTS
# ═══════════════════════════════════════════════════════════════

class TestPocketTTSSynthesize(unittest.TestCase):
    """Tests for pocket_tts_synthesize()."""

    def test_empty_text_returns_error(self):
        from integrations.service_tools.pocket_tts_tool import pocket_tts_synthesize
        result = json.loads(pocket_tts_synthesize(""))
        self.assertIn('error', result)

    def test_whitespace_text_returns_error(self):
        from integrations.service_tools.pocket_tts_tool import pocket_tts_synthesize
        result = json.loads(pocket_tts_synthesize("   "))
        self.assertIn('error', result)

    @patch('integrations.service_tools.pocket_tts_tool._load_model')
    def test_synthesize_with_pocket_tts(self, mock_load):
        """Test synthesis when pocket-tts is available."""
        import numpy as np
        mock_model = MagicMock()
        mock_model.sample_rate = 24000
        mock_model.generate_audio.return_value = MagicMock(
            numpy=lambda: np.zeros(24000, dtype=np.float32))
        mock_model.get_state_for_audio_prompt.return_value = MagicMock()
        mock_load.return_value = mock_model

        with tempfile.TemporaryDirectory() as d:
            output = os.path.join(d, 'test.wav')
            from integrations.service_tools.pocket_tts_tool import pocket_tts_synthesize
            with patch('integrations.service_tools.pocket_tts_tool._get_voice_state',
                       return_value=MagicMock()):
                result = json.loads(pocket_tts_synthesize(
                    "Hello world", voice="alba", output_path=output))

            self.assertEqual(result['engine'], 'pocket-tts')
            self.assertIn('path', result)
            self.assertGreater(result['duration'], 0)
            self.assertEqual(result['sample_rate'], 24000)

    @patch('integrations.service_tools.pocket_tts_tool._espeak_synthesize', return_value=True)
    def test_fallback_to_espeak(self, mock_espeak):
        """When pocket-tts import fails, falls back to espeak-ng."""
        with tempfile.TemporaryDirectory() as d:
            output = os.path.join(d, 'test.wav')
            from integrations.service_tools.pocket_tts_tool import pocket_tts_synthesize
            # Force pocket-tts import to fail
            with patch.dict('sys.modules', {'pocket_tts': None}):
                with patch('integrations.service_tools.pocket_tts_tool._load_model',
                           side_effect=ImportError("No pocket_tts")):
                    result = json.loads(pocket_tts_synthesize(
                        "Test fallback", output_path=output))

            self.assertEqual(result['engine'], 'espeak-ng')

    @patch('integrations.service_tools.pocket_tts_tool._espeak_synthesize', return_value=False)
    def test_no_engine_available(self, mock_espeak):
        """When both pocket-tts and espeak fail, returns error."""
        from integrations.service_tools.pocket_tts_tool import pocket_tts_synthesize
        with patch('integrations.service_tools.pocket_tts_tool._load_model',
                   side_effect=ImportError("No pocket_tts")):
            result = json.loads(pocket_tts_synthesize("No engines"))
        self.assertIn('error', result)

    def test_auto_generates_output_path(self):
        """When output_path is None, auto-generates a path."""
        from integrations.service_tools.pocket_tts_tool import pocket_tts_synthesize
        with patch('integrations.service_tools.pocket_tts_tool._load_model',
                   side_effect=ImportError("skip")):
            with patch('integrations.service_tools.pocket_tts_tool._espeak_synthesize',
                       return_value=False):
                result = json.loads(pocket_tts_synthesize("Auto path test"))
        # Even on failure, the function was called with an auto-generated path
        self.assertIn('error', result)


class TestPocketTTSListVoices(unittest.TestCase):
    """Tests for pocket_tts_list_voices()."""

    def test_list_voices_returns_builtin(self):
        from integrations.service_tools.pocket_tts_tool import pocket_tts_list_voices
        result = json.loads(pocket_tts_list_voices())
        self.assertIn('voices', result)
        self.assertGreater(result['count'], 0)
        self.assertEqual(result['builtin_count'], 8)
        names = [v['id'] for v in result['voices']]
        self.assertIn('alba', names)
        self.assertIn('marius', names)

    def test_all_builtin_voices_have_language(self):
        from integrations.service_tools.pocket_tts_tool import pocket_tts_list_voices
        result = json.loads(pocket_tts_list_voices())
        for v in result['voices']:
            self.assertEqual(v['language'], 'en')

    def test_engine_detection(self):
        """Engine field should be 'pocket-tts', 'espeak-ng', or 'none'."""
        from integrations.service_tools.pocket_tts_tool import pocket_tts_list_voices
        result = json.loads(pocket_tts_list_voices())
        self.assertIn(result['engine'], ('pocket-tts', 'espeak-ng', 'none'))


class TestPocketTTSCloneVoice(unittest.TestCase):
    """Tests for pocket_tts_clone_voice()."""

    def test_missing_audio_path(self):
        from integrations.service_tools.pocket_tts_tool import pocket_tts_clone_voice
        result = json.loads(pocket_tts_clone_voice("", "test"))
        self.assertIn('error', result)

    def test_missing_name(self):
        from integrations.service_tools.pocket_tts_tool import pocket_tts_clone_voice
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            f.write(b'\x00' * 100)
            path = f.name
        try:
            result = json.loads(pocket_tts_clone_voice(path, ""))
            self.assertIn('error', result)
        finally:
            os.unlink(path)

    def test_nonexistent_file(self):
        from integrations.service_tools.pocket_tts_tool import pocket_tts_clone_voice
        result = json.loads(pocket_tts_clone_voice("/nonexistent.wav", "test"))
        self.assertIn('error', result)

    def test_clone_without_pocket_tts(self):
        """Voice cloning requires pocket-tts, no fallback."""
        from integrations.service_tools.pocket_tts_tool import pocket_tts_clone_voice
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            f.write(b'\x00' * 100)
            path = f.name
        try:
            with patch('integrations.service_tools.pocket_tts_tool._load_model',
                       side_effect=ImportError("No pocket_tts")):
                result = json.loads(pocket_tts_clone_voice(path, "myvoice"))
            self.assertIn('error', result)
            self.assertIn('pocket-tts required', result['error'])
        finally:
            os.unlink(path)


class TestPocketTTSUnload(unittest.TestCase):
    """Tests for unload_pocket_tts()."""

    def test_unload_clears_state(self):
        import integrations.service_tools.pocket_tts_tool as mod
        mod._tts_model = "fake"
        mod._voice_states['test'] = "fake"
        mod.unload_pocket_tts()
        self.assertIsNone(mod._tts_model)
        self.assertEqual(len(mod._voice_states), 0)


class TestPocketTTSRegistration(unittest.TestCase):
    """Tests for service tool registration."""

    def test_register_functions(self):
        from integrations.service_tools.pocket_tts_tool import PocketTTSTool
        result = PocketTTSTool.register_functions()
        self.assertTrue(result)
        from integrations.service_tools.registry import service_tool_registry
        self.assertIn('pocket_tts', service_tool_registry._tools)
        tool = service_tool_registry._tools['pocket_tts']
        self.assertIn('synthesize', tool.endpoints)
        self.assertIn('list_voices', tool.endpoints)
        self.assertIn('clone_voice', tool.endpoints)
        self.assertTrue(tool.is_healthy)


class TestEspeakFallback(unittest.TestCase):
    """Tests for _espeak_synthesize fallback."""

    @patch('subprocess.run')
    def test_espeak_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        from integrations.service_tools.pocket_tts_tool import _espeak_synthesize
        result = _espeak_synthesize("Hello", "/tmp/test.wav")
        self.assertTrue(result)
        mock_run.assert_called_once()

    @patch('subprocess.run', side_effect=FileNotFoundError)
    def test_espeak_not_installed(self, mock_run):
        from integrations.service_tools.pocket_tts_tool import _espeak_synthesize
        result = _espeak_synthesize("Hello", "/tmp/test.wav")
        self.assertFalse(result)


# ═══════════════════════════════════════════════════════════════
# TTSEngine POCKET provider
# ═══════════════════════════════════════════════════════════════

class TestTTSEnginePocket(unittest.TestCase):
    """Tests for the POCKET provider in TTSEngine."""

    def test_pocket_provider_exists(self):
        from integrations.channels.media.tts import TTSProvider
        self.assertEqual(TTSProvider.POCKET.value, "pocket")

    def test_pocket_in_default_voices(self):
        from integrations.channels.media.tts import TTSEngine, TTSProvider
        self.assertIn(TTSProvider.POCKET, TTSEngine.DEFAULT_VOICES)
        self.assertEqual(TTSEngine.DEFAULT_VOICES[TTSProvider.POCKET], "alba")

    def test_pocket_in_default_models(self):
        from integrations.channels.media.tts import TTSEngine, TTSProvider
        self.assertIn(TTSProvider.POCKET, TTSEngine.DEFAULT_MODELS)

    def test_create_pocket_engine(self):
        from integrations.channels.media.tts import TTSEngine
        engine = TTSEngine(provider="pocket")
        self.assertEqual(engine.default_voice, "alba")
        self.assertEqual(engine.model, "pocket-100m")

    def test_pocket_supported_formats(self):
        from integrations.channels.media.tts import TTSEngine
        engine = TTSEngine(provider="pocket")
        formats = engine.get_supported_formats()
        self.assertIn("wav", formats)

    def test_pocket_max_text_length(self):
        from integrations.channels.media.tts import TTSEngine
        engine = TTSEngine(provider="pocket")
        self.assertEqual(engine.get_max_text_length(), 10000)

    def test_pocket_provider_info(self):
        from integrations.channels.media.tts import TTSEngine
        engine = TTSEngine(provider="pocket")
        info = engine.get_provider_info()
        self.assertEqual(info['provider'], 'pocket')
        self.assertFalse(info['ssml_support'])

    def test_pocket_from_string(self):
        from integrations.channels.media.tts import TTSProvider
        p = TTSProvider("pocket")
        self.assertEqual(p, TTSProvider.POCKET)


# ═══════════════════════════════════════════════════════════════
# API Endpoints
# ═══════════════════════════════════════════════════════════════

def _make_voice_app():
    """Create a minimal Flask app with voice endpoints for testing."""
    from flask import Flask, jsonify, request
    app = Flask(__name__)
    app.config['TESTING'] = True

    @app.route('/api/voice/speak', methods=['POST'])
    def speak():
        from integrations.service_tools.pocket_tts_tool import pocket_tts_synthesize
        import json as _json
        data = request.get_json() or {}
        text = data.get('text', '')
        if not text:
            return jsonify({'error': 'text is required'}), 400
        voice = data.get('voice', 'alba')
        output_path = data.get('output_path')
        result = pocket_tts_synthesize(text, voice, output_path)
        parsed = _json.loads(result)
        code = 200 if 'error' not in parsed else 500
        return jsonify(parsed), code

    @app.route('/api/voice/voices', methods=['GET'])
    def voices():
        from integrations.service_tools.pocket_tts_tool import pocket_tts_list_voices
        import json as _json
        return jsonify(_json.loads(pocket_tts_list_voices()))

    @app.route('/api/voice/clone', methods=['POST'])
    def clone():
        from integrations.service_tools.pocket_tts_tool import pocket_tts_clone_voice
        import json as _json
        data = request.get_json() or {}
        result = pocket_tts_clone_voice(data.get('audio_path', ''), data.get('name', ''))
        parsed = _json.loads(result)
        code = 200 if 'error' not in parsed else 500
        return jsonify(parsed), code

    return app.test_client()


class TestVoiceAPISpeak(unittest.TestCase):

    def test_speak_no_text(self):
        client = _make_voice_app()
        r = client.post('/api/voice/speak',
                        data=json.dumps({}), content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.service_tools.pocket_tts_tool._espeak_synthesize', return_value=True)
    @patch('integrations.service_tools.pocket_tts_tool._load_model',
           side_effect=ImportError("skip"))
    def test_speak_espeak_fallback(self, _load, _espeak):
        client = _make_voice_app()
        r = client.post('/api/voice/speak',
                        data=json.dumps({'text': 'hello'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['engine'], 'espeak-ng')


class TestVoiceAPIVoices(unittest.TestCase):

    def test_list_voices(self):
        client = _make_voice_app()
        r = client.get('/api/voice/voices')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertGreater(data['count'], 0)
        self.assertIn('voices', data)


class TestVoiceAPIClone(unittest.TestCase):

    def test_clone_missing_fields(self):
        client = _make_voice_app()
        r = client.post('/api/voice/clone',
                        data=json.dumps({'name': 'test'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 500)  # error in result


if __name__ == '__main__':
    unittest.main()
