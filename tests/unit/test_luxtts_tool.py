"""Tests for LuxTTS service tool (sherpa-onnx ZipVoice backend)."""
import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def reset_globals():
    """Reset module-level globals between tests."""
    import integrations.service_tools.luxtts_tool as mod
    mod._tts_engine = None
    mod._prompt_cache.clear()
    yield
    mod._tts_engine = None
    mod._prompt_cache.clear()


# ═══════════════════════════════════════════════════════════════
# luxtts_synthesize tests
# ═══════════════════════════════════════════════════════════════

class TestLuxTTSSynthesize:
    """Tests for luxtts_synthesize()."""

    def test_empty_text_returns_error(self):
        from integrations.service_tools.luxtts_tool import luxtts_synthesize
        result = json.loads(luxtts_synthesize(""))
        assert "error" in result
        assert "Text is required" in result["error"]

    def test_whitespace_text_returns_error(self):
        from integrations.service_tools.luxtts_tool import luxtts_synthesize
        result = json.loads(luxtts_synthesize("   "))
        assert "error" in result

    @patch('integrations.service_tools.luxtts_tool._load_engine')
    @patch('integrations.service_tools.luxtts_tool._get_prompt')
    def test_synthesize_success(self, mock_prompt, mock_load, tmp_path):
        import numpy as np
        mock_engine = MagicMock()
        mock_audio = MagicMock()
        mock_audio.samples = np.zeros(24000, dtype=np.float32)
        mock_audio.sample_rate = 24000
        mock_engine.generate.return_value = mock_audio
        mock_load.return_value = mock_engine

        mock_prompt.return_value = (np.zeros(24000, dtype=np.float32), 24000)

        out_path = str(tmp_path / "test_out.wav")
        mock_sf = MagicMock()
        with patch.dict('sys.modules', {'soundfile': mock_sf}):
            from integrations.service_tools.luxtts_tool import luxtts_synthesize
            result = json.loads(luxtts_synthesize(
                "Hello world", voice_audio="test_voice",
                output_path=out_path,
            ))

        assert 'error' not in result
        assert result['engine'] == 'zipvoice-sherpa-onnx'
        assert result['device'] == 'cpu'
        assert 'latency_ms' in result
        assert 'rtf' in result

    @patch('integrations.service_tools.luxtts_tool._load_engine')
    @patch('integrations.service_tools.luxtts_tool._get_prompt', return_value=None)
    def test_no_voice_returns_error(self, mock_prompt, mock_load):
        mock_load.return_value = MagicMock()

        from integrations.service_tools.luxtts_tool import luxtts_synthesize
        result = json.loads(luxtts_synthesize("Hello"))
        assert "error" in result
        assert "voice_audio required" in result["error"]

    def test_import_error_returns_error(self):
        with patch('integrations.service_tools.luxtts_tool._load_engine',
                   side_effect=ImportError('No module named sherpa_onnx')):
            from integrations.service_tools.luxtts_tool import luxtts_synthesize
            result = json.loads(luxtts_synthesize("Hello", voice_audio="test.wav"))
            assert "error" in result

    @patch('integrations.service_tools.luxtts_tool._load_engine')
    @patch('integrations.service_tools.luxtts_tool._get_prompt')
    @patch('integrations.service_tools.luxtts_tool._get_output_dir')
    def test_auto_generates_output_path(self, mock_outdir, mock_prompt, mock_load, tmp_path):
        import numpy as np
        mock_engine = MagicMock()
        mock_audio = MagicMock()
        mock_audio.samples = np.zeros(24000, dtype=np.float32)
        mock_audio.sample_rate = 24000
        mock_engine.generate.return_value = mock_audio
        mock_load.return_value = mock_engine
        mock_prompt.return_value = (np.zeros(24000, dtype=np.float32), 24000)
        mock_outdir.return_value = tmp_path

        mock_sf = MagicMock()
        with patch.dict('sys.modules', {'soundfile': mock_sf}):
            from integrations.service_tools.luxtts_tool import luxtts_synthesize
            result = json.loads(luxtts_synthesize(
                "Auto path test", voice_audio="test",
            ))

        assert 'error' not in result
        assert 'luxtts_' in result['path']


# ═══════════════════════════════════════════════════════════════
# luxtts_list_voices tests
# ═══════════════════════════════════════════════════════════════

class TestLuxTTSListVoices:

    @patch('integrations.service_tools.luxtts_tool._get_voices_dir')
    def test_empty_voices(self, mock_vdir, tmp_path):
        voices_dir = tmp_path / "voices"
        voices_dir.mkdir()
        mock_vdir.return_value = voices_dir

        from integrations.service_tools.luxtts_tool import luxtts_list_voices
        result = json.loads(luxtts_list_voices())
        assert result['count'] == 0
        assert result['voices'] == []
        assert result['sample_rate'] == 24000

    @patch('integrations.service_tools.luxtts_tool._get_voices_dir')
    def test_lists_saved_voices(self, mock_vdir, tmp_path):
        voices_dir = tmp_path / "voices"
        voices_dir.mkdir()
        (voices_dir / "alice.wav").write_bytes(b'\x00' * 100)
        (voices_dir / "bob.wav").write_bytes(b'\x00' * 100)
        mock_vdir.return_value = voices_dir

        from integrations.service_tools.luxtts_tool import luxtts_list_voices
        result = json.loads(luxtts_list_voices())
        assert result['count'] == 2
        names = [v['id'] for v in result['voices']]
        assert 'alice' in names
        assert 'bob' in names

    @patch('integrations.service_tools.luxtts_tool._get_voices_dir')
    def test_engine_not_installed(self, mock_vdir, tmp_path):
        voices_dir = tmp_path / "voices"
        voices_dir.mkdir()
        mock_vdir.return_value = voices_dir

        with patch.dict('sys.modules', {'sherpa_onnx': None}):
            from integrations.service_tools.luxtts_tool import luxtts_list_voices
            result = json.loads(luxtts_list_voices())
            assert result['engine'] == 'not_installed'


# ═══════════════════════════════════════════════════════════════
# luxtts_clone_voice tests
# ═══════════════════════════════════════════════════════════════

class TestLuxTTSCloneVoice:

    def test_missing_audio_path(self):
        from integrations.service_tools.luxtts_tool import luxtts_clone_voice
        result = json.loads(luxtts_clone_voice("", "test"))
        assert "error" in result
        assert "audio_path" in result["error"]

    def test_missing_name(self, tmp_path):
        audio = tmp_path / "test.wav"
        audio.write_bytes(b'\x00' * 100)
        from integrations.service_tools.luxtts_tool import luxtts_clone_voice
        result = json.loads(luxtts_clone_voice(str(audio), ""))
        assert "error" in result
        assert "name" in result["error"]

    @patch('integrations.service_tools.luxtts_tool._get_voices_dir')
    def test_clone_copies_wav(self, mock_vdir, tmp_path):
        voices_dir = tmp_path / "voices"
        voices_dir.mkdir()
        mock_vdir.return_value = voices_dir

        audio = tmp_path / "source.wav"
        audio.write_bytes(b'\x00' * 1000)

        from integrations.service_tools.luxtts_tool import luxtts_clone_voice
        result = json.loads(luxtts_clone_voice(str(audio), "My Voice"))
        assert result['saved'] is True
        assert result['name'] == 'my-voice'
        assert (voices_dir / 'my-voice.wav').exists()

    @patch('integrations.service_tools.luxtts_tool._get_voices_dir')
    def test_clone_clears_cache(self, mock_vdir, tmp_path):
        voices_dir = tmp_path / "voices"
        voices_dir.mkdir()
        mock_vdir.return_value = voices_dir

        import integrations.service_tools.luxtts_tool as mod
        mod._prompt_cache['test-voice'] = MagicMock()

        audio = tmp_path / "source.wav"
        audio.write_bytes(b'\x00' * 1000)

        from integrations.service_tools.luxtts_tool import luxtts_clone_voice
        luxtts_clone_voice(str(audio), "test voice")
        assert 'test-voice' not in mod._prompt_cache


# ═══════════════════════════════════════════════════════════════
# unload_luxtts tests
# ═══════════════════════════════════════════════════════════════

class TestUnloadLuxTTS:

    def test_unload_clears_state(self):
        import integrations.service_tools.luxtts_tool as mod
        mod._tts_engine = MagicMock()
        mod._prompt_cache['test'] = MagicMock()

        mod.unload_luxtts()

        assert mod._tts_engine is None
        assert len(mod._prompt_cache) == 0


# ═══════════════════════════════════════════════════════════════
# LuxTTSTool registration tests
# ═══════════════════════════════════════════════════════════════

class TestLuxTTSToolRegistration:

    def test_registers_with_registry(self):
        from integrations.service_tools.luxtts_tool import LuxTTSTool
        from integrations.service_tools.registry import service_tool_registry
        result = LuxTTSTool.register_functions()
        assert result is True
        assert 'luxtts' in service_tool_registry._tools

    def test_registered_tool_has_endpoints(self):
        from integrations.service_tools.luxtts_tool import LuxTTSTool
        from integrations.service_tools.registry import service_tool_registry
        LuxTTSTool.register_functions()
        tool = service_tool_registry._tools['luxtts']
        assert 'synthesize' in tool.endpoints
        assert 'list_voices' in tool.endpoints
        assert 'clone_voice' in tool.endpoints
        assert 'benchmark' in tool.endpoints

    def test_registered_tool_tags(self):
        from integrations.service_tools.luxtts_tool import LuxTTSTool
        from integrations.service_tools.registry import service_tool_registry
        LuxTTSTool.register_functions()
        tool = service_tool_registry._tools['luxtts']
        assert 'tts' in tool.tags
        assert 'voice-cloning' in tool.tags


# ═══════════════════════════════════════════════════════════════
# Model bus integration tests
# ═══════════════════════════════════════════════════════════════

class TestModelBusLuxTTS:
    """Test LuxTTS integration in model_bus_service."""

    def test_route_tts_tries_luxtts_first(self):
        from integrations.agent_engine.model_bus_service import ModelBusService
        bus = ModelBusService()

        # Disable router so legacy fallback chain runs
        with patch('integrations.channels.media.tts_router.get_tts_router',
                   side_effect=ImportError('disabled for test')):
            with patch.object(bus, '_try_luxtts') as mock_lux:
                mock_lux.return_value = {
                    'response': '/tmp/test.wav',
                    'model': 'luxtts-48k',
                    'backend': 'local_tts',
                }
                result = bus._route_tts("Hello", {})
                mock_lux.assert_called_once()
                assert result['model'] == 'luxtts-48k'

    def test_route_tts_falls_through_on_luxtts_error(self):
        from integrations.agent_engine.model_bus_service import ModelBusService
        bus = ModelBusService()

        # Disable router so legacy fallback chain runs
        with patch('integrations.channels.media.tts_router.get_tts_router',
                   side_effect=ImportError('disabled for test')):
            with patch.object(bus, '_try_luxtts') as mock_lux, \
                 patch.object(bus, '_try_pocket_tts') as mock_pocket:
                mock_lux.return_value = {'error': 'not installed'}
                mock_pocket.return_value = {
                    'response': '/tmp/pocket.wav',
                    'model': 'pocket-tts-100m',
                    'backend': 'local_tts',
                }
                result = bus._route_tts("Hello", {})
                assert result['model'] == 'pocket-tts-100m'

    def test_try_luxtts_calls_synthesize(self):
        from integrations.agent_engine.model_bus_service import ModelBusService
        bus = ModelBusService()

        mock_result = json.dumps({
            'path': '/tmp/test.wav',
            'duration': 1.5,
            'device': 'cpu',
            'voice': 'default',
            'rtf': 0.007,
            'realtime_factor': 142.8,
        })
        with patch('integrations.service_tools.luxtts_tool.luxtts_synthesize',
                   return_value=mock_result):
            result = bus._try_luxtts("Hello", {'voice': 'alice'})
            assert result['model'] == 'luxtts-48k'


# ═══════════════════════════════════════════════════════════════
# Model registry tests
# ═══════════════════════════════════════════════════════════════

class TestModelRegistryLuxTTS:
    """Test LuxTTS registration in model_registry."""

    def test_luxtts_registered_when_available(self):
        """LuxTTS should be registrable as a local model."""
        from integrations.agent_engine.model_registry import ModelRegistry, ModelBackend, ModelTier
        reg = ModelRegistry()
        reg.register(ModelBackend(
            model_id='luxtts-48k',
            display_name='LuxTTS 48kHz',
            tier=ModelTier.FAST,
            config_list_entry={'model': 'luxtts-48k'},
            avg_latency_ms=50.0,
            accuracy_score=0.93,
            cost_per_1k_tokens=0.0,
            is_local=True,
        ))
        model = reg.get_model('luxtts-48k')
        assert model is not None
        assert model.is_local is True
        assert model.cost_per_1k_tokens == 0.0


# ═══════════════════════════════════════════════════════════════
# TTSProvider enum test
# ═══════════════════════════════════════════════════════════════

class TestTTSProviderEnum:

    def test_luxtts_in_providers(self):
        from integrations.channels.media.tts import TTSProvider
        assert hasattr(TTSProvider, 'LUXTTS')
        assert TTSProvider.LUXTTS.value == 'luxtts'

    def test_luxtts_before_pocket(self):
        """LuxTTS should be listed before Pocket in enum (priority order)."""
        from integrations.channels.media.tts import TTSProvider
        members = list(TTSProvider)
        lux_idx = members.index(TTSProvider.LUXTTS)
        pocket_idx = members.index(TTSProvider.POCKET)
        assert lux_idx < pocket_idx


# ═══════════════════════════════════════════════════════════════
# Engine constants
# ═══════════════════════════════════════════════════════════════

class TestLuxTTSConstants:
    """Verify key constants match the sherpa-onnx ZipVoice setup."""

    def test_sample_rate(self):
        from integrations.service_tools.luxtts_tool import SAMPLE_RATE
        assert SAMPLE_RATE == 24000

    def test_model_tarball_name(self):
        from integrations.service_tools.luxtts_tool import MODEL_TARBALL
        assert 'zipvoice' in MODEL_TARBALL
        assert 'int8' in MODEL_TARBALL

    def test_prompt_cache_is_dict(self):
        from integrations.service_tools.luxtts_tool import _prompt_cache
        assert isinstance(_prompt_cache, dict)
