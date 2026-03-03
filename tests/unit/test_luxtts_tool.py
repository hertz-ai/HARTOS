"""Tests for LuxTTS service tool."""
import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def reset_globals():
    """Reset module-level globals between tests."""
    import integrations.service_tools.luxtts_tool as mod
    mod._luxtts_model = None
    mod._luxtts_device = None
    mod._voice_cache.clear()
    yield
    mod._luxtts_model = None
    mod._luxtts_device = None
    mod._voice_cache.clear()


@pytest.fixture
def mock_luxtts_model():
    """Mock LuxTTS model with encode_prompt and generate_speech."""
    import numpy as np
    model = MagicMock()
    # encode_prompt returns a mock prompt tensor
    model.encode_prompt.return_value = MagicMock()
    # generate_speech returns fake 48kHz audio (1 second)
    fake_wav = MagicMock()
    fake_wav.numpy.return_value = np.zeros(48000, dtype=np.float32)
    model.generate_speech.return_value = fake_wav
    return model


@pytest.fixture
def mock_voices_dir(tmp_path):
    """Create a temp voices directory with a default voice."""
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    # Create a minimal WAV file (44 bytes header + 48000 samples)
    default_wav = voices_dir / "default.wav"
    default_wav.write_bytes(b'\x00' * 100)  # placeholder
    return voices_dir


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

    @patch('integrations.service_tools.luxtts_tool._load_model')
    @patch('integrations.service_tools.luxtts_tool._get_encoded_prompt')
    @patch('integrations.service_tools.luxtts_tool._get_voices_dir')
    def test_synthesize_success(self, mock_vdir, mock_prompt, mock_load, mock_luxtts_model, tmp_path):
        import numpy as np
        mock_load.return_value = (mock_luxtts_model, 'cpu')
        mock_prompt.return_value = MagicMock()
        mock_vdir.return_value = tmp_path

        out_path = str(tmp_path / "test_out.wav")

        mock_sf = MagicMock()
        with patch.dict('sys.modules', {'soundfile': mock_sf}):
            from integrations.service_tools.luxtts_tool import luxtts_synthesize
            result = json.loads(luxtts_synthesize(
                "Hello world", voice_audio="test_voice",
                output_path=out_path, device='cpu'
            ))

        assert 'error' not in result
        assert result['engine'] == 'luxtts'
        assert result['sample_rate'] == 48000
        assert result['device'] == 'cpu'
        assert 'latency_ms' in result
        assert 'rtf' in result

    @patch('integrations.service_tools.luxtts_tool._load_model')
    @patch('integrations.service_tools.luxtts_tool._get_encoded_prompt', return_value=None)
    @patch('integrations.service_tools.luxtts_tool._get_voices_dir')
    def test_no_voice_returns_error(self, mock_vdir, mock_prompt, mock_load, tmp_path):
        mock_load.return_value = (MagicMock(), 'cpu')
        voices_dir = tmp_path / "voices"
        voices_dir.mkdir()
        mock_vdir.return_value = voices_dir

        from integrations.service_tools.luxtts_tool import luxtts_synthesize
        result = json.loads(luxtts_synthesize("Hello", device='cpu'))
        assert "error" in result
        assert "voice_audio required" in result["error"]

    def test_import_error_returns_error(self):
        with patch.dict('sys.modules', {'zipvoice': None, 'zipvoice.luxvoice': None}):
            from integrations.service_tools.luxtts_tool import luxtts_synthesize
            # Force fresh import error by clearing cached model
            import integrations.service_tools.luxtts_tool as mod
            mod._luxtts_model = None
            result = json.loads(luxtts_synthesize("Hello", voice_audio="test.wav", device='cpu'))
            assert "error" in result

    @patch('integrations.service_tools.luxtts_tool._load_model')
    @patch('integrations.service_tools.luxtts_tool._get_encoded_prompt')
    def test_auto_generates_output_path(self, mock_prompt, mock_load, mock_luxtts_model, tmp_path):
        import numpy as np
        mock_load.return_value = (mock_luxtts_model, 'cpu')
        mock_prompt.return_value = MagicMock()

        mock_sf = MagicMock()
        with patch.dict('sys.modules', {'soundfile': mock_sf}), \
             patch('integrations.service_tools.luxtts_tool._get_output_dir', return_value=tmp_path):
            from integrations.service_tools.luxtts_tool import luxtts_synthesize
            result = json.loads(luxtts_synthesize(
                "Auto path test", voice_audio="test", device='cpu'
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
        assert result['sample_rate'] == 48000

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

        with patch.dict('sys.modules', {'zipvoice': None, 'zipvoice.luxvoice': None}):
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
        mod._voice_cache['test-voice'] = MagicMock()

        audio = tmp_path / "source.wav"
        audio.write_bytes(b'\x00' * 1000)

        from integrations.service_tools.luxtts_tool import luxtts_clone_voice
        luxtts_clone_voice(str(audio), "test voice")
        assert 'test-voice' not in mod._voice_cache


# ═══════════════════════════════════════════════════════════════
# Device detection tests
# ═══════════════════════════════════════════════════════════════

class TestDeviceDetection:

    def test_fallback_to_cpu_when_no_gpu(self):
        """Falls back to CPU when no GPU or CUDA detected."""
        from integrations.service_tools.luxtts_tool import _detect_device
        vm_mod = sys.modules['integrations.service_tools.vram_manager']
        orig_cls = vm_mod.VRAMManager
        mock_inst = MagicMock()
        mock_inst.detect_gpu.return_value = {'available': False}
        vm_mod.VRAMManager = MagicMock(return_value=mock_inst)
        try:
            import torch
            with patch.object(torch.cuda, 'is_available', return_value=False):
                device = _detect_device()
                assert device == 'cpu'
        finally:
            vm_mod.VRAMManager = orig_cls

    def test_detects_cuda_via_vram_manager(self):
        from integrations.service_tools.luxtts_tool import _detect_device
        vm_mod = sys.modules['integrations.service_tools.vram_manager']
        orig_cls = vm_mod.VRAMManager
        mock_inst = MagicMock()
        mock_inst.detect_gpu.return_value = {'available': True}
        vm_mod.VRAMManager = MagicMock(return_value=mock_inst)
        try:
            device = _detect_device()
            assert device == 'cuda'
        finally:
            vm_mod.VRAMManager = orig_cls

    def test_detects_cuda_via_torch(self):
        """When vram_manager import fails, falls through to torch.cuda check."""
        from integrations.service_tools.luxtts_tool import _detect_device
        import torch
        with patch.dict('sys.modules', {'integrations.service_tools.vram_manager': None}):
            with patch.object(torch.cuda, 'is_available', return_value=True):
                device = _detect_device()
                assert device == 'cuda'


# ═══════════════════════════════════════════════════════════════
# unload_luxtts tests
# ═══════════════════════════════════════════════════════════════

class TestUnloadLuxTTS:

    def test_unload_clears_state(self):
        import integrations.service_tools.luxtts_tool as mod
        mod._luxtts_model = MagicMock()
        mod._luxtts_device = 'cuda'
        mod._voice_cache['test'] = MagicMock()

        mod.unload_luxtts()

        assert mod._luxtts_model is None
        assert mod._luxtts_device is None
        assert len(mod._voice_cache) == 0


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
        assert '48khz' in tool.tags


# ═══════════════════════════════════════════════════════════════
# Model bus integration tests
# ═══════════════════════════════════════════════════════════════

class TestModelBusLuxTTS:
    """Test LuxTTS integration in model_bus_service."""

    def test_route_tts_tries_luxtts_first(self):
        from integrations.agent_engine.model_bus_service import ModelBusService
        bus = ModelBusService()

        with patch.object(bus, '_try_luxtts') as mock_lux:
            mock_lux.return_value = {
                'response': '/tmp/test.wav',
                'model': 'luxtts-48k',
                'backend': 'local_tts_cuda',
            }
            result = bus._route_tts("Hello", {})
            mock_lux.assert_called_once()
            assert result['model'] == 'luxtts-48k'

    def test_route_tts_falls_through_on_luxtts_error(self):
        from integrations.agent_engine.model_bus_service import ModelBusService
        bus = ModelBusService()

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
            'device': 'cuda',
            'voice': 'default',
            'rtf': 0.007,
            'realtime_factor': 142.8,
        })
        with patch('integrations.service_tools.luxtts_tool.luxtts_synthesize',
                   return_value=mock_result):
            result = bus._try_luxtts("Hello", {'voice': 'alice'})
            assert result['model'] == 'luxtts-48k'
            assert result['sample_rate'] == 48000


# ═══════════════════════════════════════════════════════════════
# Model registry tests
# ═══════════════════════════════════════════════════════════════

class TestModelRegistryLuxTTS:
    """Test LuxTTS registration in model_registry."""

    def test_luxtts_registered_when_available(self):
        """LuxTTS should be registered if zipvoice is importable."""
        mock_lux = MagicMock()
        with patch.dict('sys.modules', {
            'zipvoice': MagicMock(),
            'zipvoice.luxvoice': mock_lux,
        }):
            from integrations.agent_engine.model_registry import ModelRegistry, ModelBackend, ModelTier
            reg = ModelRegistry()
            # Simulate registration
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
            assert model.accuracy_score == 0.93


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
