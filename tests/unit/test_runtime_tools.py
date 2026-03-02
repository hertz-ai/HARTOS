"""
Tests for Runtime Media Tool Integration.

Covers: ModelStorageManager, VRAMManager, tool wrappers, RuntimeToolManager
lifecycle, state persistence, dynamic port allocation, voice transcription.
"""

import json
import os
import sys
import shutil
import socket
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, Mock, PropertyMock

import pytest

# ─── Ensure project root is on path ─────────────────────────────
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


# ══════════════════════════════════════════════════════════════════
# ModelStorageManager Tests
# ══════════════════════════════════════════════════════════════════

class TestModelStorageManager:
    """Tests for integrations/service_tools/model_storage.py"""

    @pytest.fixture
    def tmp_storage(self, tmp_path):
        """Create a ModelStorageManager with a temp base dir."""
        from integrations.service_tools.model_storage import ModelStorageManager
        return ModelStorageManager(base_dir=tmp_path)

    def test_get_tool_dir(self, tmp_storage, tmp_path):
        d = tmp_storage.get_tool_dir('whisper')
        assert d == tmp_path / 'whisper'

    def test_manifest_roundtrip(self, tmp_storage):
        """Write and read manifest."""
        assert tmp_storage.get_manifest() == {"tools": {}}
        tmp_storage.mark_downloaded('test_tool', 'https://example.com', 1000)
        m = tmp_storage.get_manifest()
        assert 'test_tool' in m['tools']
        assert m['tools']['test_tool']['source_url'] == 'https://example.com'
        assert m['tools']['test_tool']['size_bytes'] == 1000

    def test_is_downloaded_false_when_no_manifest(self, tmp_storage):
        assert tmp_storage.is_downloaded('nonexistent') is False

    def test_is_downloaded_false_when_dir_empty(self, tmp_storage):
        """Manifest says downloaded but directory is gone."""
        tmp_storage.mark_downloaded('ghost', 'url', 0)
        assert tmp_storage.is_downloaded('ghost') is False

    def test_is_downloaded_true(self, tmp_storage):
        """Tool is in manifest and directory has files."""
        tool_dir = tmp_storage.get_tool_dir('real')
        tool_dir.mkdir()
        (tool_dir / 'config.json').write_text('{}')
        tmp_storage.mark_downloaded('real', 'url', 100)
        assert tmp_storage.is_downloaded('real') is True

    def test_get_tool_size(self, tmp_storage):
        tool_dir = tmp_storage.get_tool_dir('sized')
        tool_dir.mkdir()
        (tool_dir / 'file.bin').write_bytes(b'x' * 1024)
        assert tmp_storage.get_tool_size('sized') == 1024

    def test_get_total_size(self, tmp_storage):
        d1 = tmp_storage.get_tool_dir('a')
        d1.mkdir()
        (d1 / 'f.bin').write_bytes(b'x' * 500)
        d2 = tmp_storage.get_tool_dir('b')
        d2.mkdir()
        (d2 / 'f.bin').write_bytes(b'y' * 300)
        # manifest.json also contributes some bytes
        total = tmp_storage.get_total_size()
        assert total >= 800

    def test_remove_tool(self, tmp_storage):
        tool_dir = tmp_storage.get_tool_dir('removable')
        tool_dir.mkdir()
        (tool_dir / 'data.bin').write_bytes(b'data')
        tmp_storage.mark_downloaded('removable', 'url', 4)
        assert tmp_storage.is_downloaded('removable') is True

        tmp_storage.remove_tool('removable')
        assert not tool_dir.exists()
        assert 'removable' not in tmp_storage.get_manifest().get('tools', {})

    @patch('subprocess.run')
    def test_clone_repo(self, mock_run, tmp_storage):
        """Git clone creates directory and marks downloaded."""
        mock_run.return_value = MagicMock(returncode=0)
        # Pre-create a file so is_downloaded returns True
        tool_dir = tmp_storage.get_tool_dir('testrepo')
        tool_dir.mkdir(parents=True)
        (tool_dir / 'readme.md').write_text('test')

        result = tmp_storage.clone_repo('testrepo', 'https://github.com/test/repo')
        assert result is not None
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert 'git' in cmd
        assert 'clone' in cmd

    @patch('subprocess.run')
    def test_clone_repo_failure(self, mock_run, tmp_storage):
        mock_run.return_value = MagicMock(returncode=1, stderr='fatal: error')
        result = tmp_storage.clone_repo('fail', 'https://bad-url')
        assert result is None

    @patch('subprocess.run')
    def test_clone_repo_pull_when_exists(self, mock_run, tmp_storage):
        """If .git exists, pull instead of clone."""
        tool_dir = tmp_storage.get_tool_dir('existing')
        (tool_dir / '.git').mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0)

        result = tmp_storage.clone_repo('existing', 'https://github.com/test/repo')
        assert result == tool_dir
        cmd = mock_run.call_args[0][0]
        assert 'pull' in cmd

    def test_download_hf_model_already_downloaded(self, tmp_storage):
        """Skip download if already present."""
        tool_dir = tmp_storage.get_tool_dir('whisper')
        tool_dir.mkdir()
        (tool_dir / 'model.bin').write_bytes(b'model')
        tmp_storage.mark_downloaded('whisper', 'hf://test', 5)

        result = tmp_storage.download_hf_model('whisper', 'openai/whisper-base')
        assert result == tool_dir

    def test_download_hf_model_no_huggingface_hub(self, tmp_storage):
        """Graceful failure when huggingface_hub not installed."""
        with patch.dict('sys.modules', {'huggingface_hub': None}):
            # Force is_downloaded to return False by not marking it
            result = tmp_storage.download_hf_model('test', 'repo/id')
            # Should return None on import error
            assert result is None


# ══════════════════════════════════════════════════════════════════
# VRAMManager Tests
# ══════════════════════════════════════════════════════════════════

class TestVRAMManager:
    """Tests for integrations/service_tools/vram_manager.py"""

    @pytest.fixture
    def vm(self):
        from integrations.service_tools.vram_manager import VRAMManager
        return VRAMManager()

    def test_detect_gpu_no_torch(self, vm):
        """Graceful when torch not available."""
        with patch.dict('sys.modules', {'torch': None}):
            vm._gpu_info = None  # force re-detect
            info = vm.detect_gpu()
            # Should return safe defaults
            assert 'cuda_available' in info

    @patch('subprocess.run', side_effect=FileNotFoundError)
    @patch('torch.cuda.is_available', return_value=True)
    @patch('torch.cuda.get_device_name', return_value='RTX 3070')
    @patch('torch.cuda.get_device_properties')
    @patch('torch.cuda.memory_allocated', return_value=2 * 1024**3)
    @patch('torch.cuda.memory_reserved', return_value=3 * 1024**3)
    def test_detect_gpu_with_cuda(self, mock_reserved, mock_alloc,
                                   mock_props, mock_name, mock_avail,
                                   mock_smi, vm):
        mock_props.return_value = MagicMock(total_mem=8 * 1024**3)
        vm._gpu_info = None
        info = vm.detect_gpu()
        assert info['cuda_available'] is True
        assert info['name'] == 'RTX 3070'
        assert info['total_gb'] == 8.0

    def test_allocate_and_release(self, vm):
        vm._gpu_info = {'cuda_available': True, 'total_gb': 8.0, 'free_gb': 8.0, 'name': 'test'}
        vm.allocate('whisper')
        assert 'whisper' in vm.get_allocations()
        assert vm.get_allocations()['whisper'] == 1.5  # from VRAM_BUDGETS

        vm.release('whisper')
        assert 'whisper' not in vm.get_allocations()

    def test_can_fit_unknown_tool(self, vm):
        """Unknown tools are assumed to fit."""
        assert vm.can_fit('unknown_tool_xyz') is True

    def test_can_fit_no_gpu(self, vm):
        vm._gpu_info = {'cuda_available': False, 'total_gb': 0, 'free_gb': 0, 'name': None}
        assert vm.can_fit('wan2gp') is False

    def test_can_fit_when_already_allocated(self, vm):
        vm._gpu_info = {'cuda_available': True, 'total_gb': 8.0, 'free_gb': 8.0, 'name': 'test'}
        vm.allocate('wan2gp')
        assert vm.can_fit('wan2gp') is True  # already allocated

    def test_suggest_offload_mode_no_gpu(self, vm):
        vm._gpu_info = {'cuda_available': False, 'total_gb': 0, 'free_gb': 0, 'name': None}
        assert vm.suggest_offload_mode('whisper') == 'cpu_only'

    def test_suggest_offload_mode_plenty_vram(self, vm):
        vm._gpu_info = {'cuda_available': True, 'total_gb': 24.0, 'free_gb': 24.0, 'name': 'test'}
        assert vm.suggest_offload_mode('whisper') == 'gpu'

    def test_suggest_offload_mode_tight_vram(self, vm):
        vm._gpu_info = {'cuda_available': True, 'total_gb': 4.0, 'free_gb': 1.0, 'name': 'test'}
        assert vm.suggest_offload_mode('whisper') in ('cpu_offload', 'cpu_only')

    def test_get_status(self, vm):
        vm._gpu_info = {'cuda_available': True, 'total_gb': 8.0, 'free_gb': 8.0, 'name': 'test'}
        status = vm.get_status()
        assert 'gpu' in status
        assert 'allocations' in status
        assert 'effective_free_gb' in status

    def test_refresh_gpu_info(self, vm):
        vm._gpu_info = {'cuda_available': False, 'total_gb': 0, 'free_gb': 0, 'name': None}
        # After refresh, should re-detect (mock will give same result but cache is cleared)
        info = vm.refresh_gpu_info()
        assert isinstance(info, dict)


# ══════════════════════════════════════════════════════════════════
# Tool Wrapper Tests
# ══════════════════════════════════════════════════════════════════

class TestToolWrappers:
    """Tests for tool wrapper registration."""

    def test_wan2gp_create_tool_info(self):
        from integrations.service_tools.wan2gp_tool import Wan2GPTool
        info = Wan2GPTool.create_tool_info('http://127.0.0.1:9999')
        assert info.name == 'wan2gp'
        assert info.base_url == 'http://127.0.0.1:9999'
        assert 'generate' in info.endpoints
        assert 'check_result' in info.endpoints
        assert 'video' in info.tags

    def test_tts_audio_suite_create_tool_info(self):
        from integrations.service_tools.tts_audio_suite_tool import TTSAudioSuiteTool
        info = TTSAudioSuiteTool.create_tool_info('http://127.0.0.1:9998')
        assert info.name == 'tts_audio_suite'
        assert 'synthesize' in info.endpoints
        assert 'list_models' in info.endpoints
        assert 'tts' in info.tags

    def test_omniparser_create_tool_info(self):
        from integrations.service_tools.omniparser_tool import OmniParserTool
        info = OmniParserTool.create_tool_info()
        assert info.name == 'omniparser'
        assert info.health_endpoint == '/probe'
        assert 'parse_screen' in info.endpoints
        assert 'execute_action' in info.endpoints

    def test_whisper_tool_functions_exist(self):
        from integrations.service_tools.whisper_tool import (
            whisper_transcribe, whisper_detect_language, unload_whisper,
            select_whisper_model
        )
        assert callable(whisper_transcribe)
        assert callable(whisper_detect_language)
        assert callable(unload_whisper)
        assert callable(select_whisper_model)

    def test_whisper_select_model_no_gpu(self):
        from integrations.service_tools.whisper_tool import select_whisper_model
        from integrations.service_tools.vram_manager import vram_manager
        # Directly set the cached gpu_info to avoid mock path issues
        original = vram_manager._gpu_info
        try:
            vram_manager._gpu_info = {
                'cuda_available': False, 'total_gb': 0,
                'free_gb': 0, 'name': None,
            }
            # Hide sherpa_onnx to force legacy whisper path (returns 'base')
            with patch.dict(sys.modules, {'sherpa_onnx': None}):
                model = select_whisper_model()
            assert model == 'base'
        finally:
            vram_manager._gpu_info = original

    def test_wan2gp_register(self):
        """Wan2GP register adds to service_tool_registry."""
        from integrations.service_tools.wan2gp_tool import Wan2GPTool
        from integrations.service_tools.registry import service_tool_registry

        # Temporarily clear
        service_tool_registry._tools.pop('wan2gp', None)
        result = Wan2GPTool.register('http://127.0.0.1:12345')
        assert result is True
        assert 'wan2gp' in service_tool_registry._tools
        # Cleanup
        service_tool_registry._tools.pop('wan2gp', None)

    def test_tts_audio_suite_register(self):
        from integrations.service_tools.tts_audio_suite_tool import TTSAudioSuiteTool
        from integrations.service_tools.registry import service_tool_registry

        service_tool_registry._tools.pop('tts_audio_suite', None)
        result = TTSAudioSuiteTool.register('http://127.0.0.1:12346')
        assert result is True
        assert 'tts_audio_suite' in service_tool_registry._tools
        service_tool_registry._tools.pop('tts_audio_suite', None)


# ══════════════════════════════════════════════════════════════════
# RuntimeToolManager Tests
# ══════════════════════════════════════════════════════════════════

class TestRuntimeToolManager:
    """Tests for integrations/service_tools/runtime_manager.py"""

    @pytest.fixture
    def rtm(self, tmp_path):
        """Create a RuntimeToolManager with temp storage."""
        from integrations.service_tools.model_storage import ModelStorageManager
        from integrations.service_tools.vram_manager import VRAMManager
        from integrations.service_tools.runtime_manager import RuntimeToolManager

        storage = ModelStorageManager(base_dir=tmp_path / 'models')
        vram = VRAMManager()
        vram._gpu_info = {
            'cuda_available': True, 'total_gb': 8.0,
            'free_gb': 8.0, 'name': 'TestGPU',
        }
        return RuntimeToolManager(storage=storage, vram=vram)

    def test_unknown_tool(self, rtm):
        result = rtm.setup_tool('nonexistent')
        assert 'error' in result

    def test_get_tool_status_unknown(self, rtm):
        result = rtm.get_tool_status('nonexistent')
        assert 'error' in result

    def test_get_tool_status_not_downloaded(self, rtm):
        result = rtm.get_tool_status('wan2gp')
        assert result['downloaded'] is False
        assert result['running'] is False

    def test_start_tool_not_downloaded(self, rtm):
        result = rtm.start_tool('wan2gp')
        assert 'error' in result

    def test_get_all_status(self, rtm):
        status = rtm.get_all_status()
        assert 'wan2gp' in status
        assert 'whisper' in status
        assert 'tts_audio_suite' in status
        assert 'vram' in status
        assert 'storage' in status

    def test_save_and_load_state(self, rtm, tmp_path):
        """State persistence roundtrip."""
        from integrations.service_tools.runtime_manager import STATE_FILE

        # Override STATE_FILE for test
        test_state_file = tmp_path / 'test_state.json'
        with patch('integrations.service_tools.runtime_manager.STATE_FILE', test_state_file):
            rtm.save_state()
            assert test_state_file.exists()
            state = json.loads(test_state_file.read_text())
            assert 'tools' in state

    def test_stop_all(self, rtm):
        """stop_all runs without error even with no running tools."""
        rtm.stop_all()

    @patch('integrations.service_tools.runtime_manager.RuntimeToolManager._start_sidecar')
    @patch('subprocess.run')
    def test_setup_tool_downloads_and_starts(self, mock_run, mock_start, rtm):
        """setup_tool clones repo then starts sidecar."""
        mock_run.return_value = MagicMock(returncode=0)
        mock_start.return_value = {'running': True, 'port': 54321}

        # Pre-create directory so is_downloaded check passes after clone
        tool_dir = rtm.storage.get_tool_dir('wan2gp')
        tool_dir.mkdir(parents=True)
        (tool_dir / 'readme.md').write_text('test')

        result = rtm.setup_tool('wan2gp')
        assert result.get('downloaded') is True

    def test_is_server_alive_no_process(self, rtm):
        assert rtm._is_server_alive('wan2gp') is False

    def test_kill_server_no_process(self, rtm):
        """Kill on non-running tool doesn't crash."""
        rtm._kill_server('wan2gp')

    def test_register_tool_at_port_wan2gp(self, rtm):
        """Registration dispatches to correct tool wrapper."""
        from integrations.service_tools.registry import service_tool_registry
        service_tool_registry._tools.pop('wan2gp', None)

        rtm._register_tool_at_port('wan2gp', 55555)
        assert 'wan2gp' in service_tool_registry._tools
        assert service_tool_registry._tools['wan2gp'].base_url == 'http://127.0.0.1:55555'
        service_tool_registry._tools.pop('wan2gp', None)

    def test_register_tool_at_port_tts(self, rtm):
        from integrations.service_tools.registry import service_tool_registry
        service_tool_registry._tools.pop('tts_audio_suite', None)

        rtm._register_tool_at_port('tts_audio_suite', 55556)
        assert 'tts_audio_suite' in service_tool_registry._tools
        service_tool_registry._tools.pop('tts_audio_suite', None)


# ══════════════════════════════════════════════════════════════════
# Dynamic Port Tests
# ══════════════════════════════════════════════════════════════════

class TestDynamicPort:
    """Test dynamic port allocation pattern."""

    def test_find_free_port(self):
        """OS assigns a free port when binding to 0."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        assert isinstance(port, int)
        assert port > 0

    def test_read_port_from_stdout(self):
        """RuntimeToolManager reads PORT=NNNNN from child stdout."""
        from integrations.service_tools.runtime_manager import RuntimeToolManager

        rtm = RuntimeToolManager.__new__(RuntimeToolManager)

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout.readline.side_effect = [
            'Loading model...\n',
            'PORT=54321\n',
        ]

        port = rtm._read_port_from_stdout(mock_proc, timeout=5)
        assert port == 54321

    def test_read_port_process_dies(self):
        """Returns None if process dies before reporting port."""
        from integrations.service_tools.runtime_manager import RuntimeToolManager

        rtm = RuntimeToolManager.__new__(RuntimeToolManager)

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # process exited
        mock_proc.stderr.read.return_value = 'ImportError: ...'

        port = rtm._read_port_from_stdout(mock_proc, timeout=2)
        assert port is None


# ══════════════════════════════════════════════════════════════════
# Voice Transcription Endpoint Tests
# ══════════════════════════════════════════════════════════════════

class TestVoiceTranscription:
    """Tests for /api/voice/transcribe endpoint."""

    @pytest.fixture
    def client(self):
        """Flask test client."""
        from langchain_gpt_api import app
        app.config['TESTING'] = True
        with app.test_client() as c:
            yield c

    def test_transcribe_no_audio(self, client):
        """Returns 400 when no audio provided."""
        resp = client.post('/api/voice/transcribe',
                          json={})
        assert resp.status_code == 400

    @patch('integrations.service_tools.whisper_tool.whisper_transcribe')
    def test_transcribe_json_path(self, mock_transcribe, client):
        """Transcribe from audio_path in JSON body."""
        mock_transcribe.return_value = json.dumps({
            'text': 'hello world',
            'language': 'en',
        })
        resp = client.post('/api/voice/transcribe',
                          json={'audio_path': '/tmp/test.wav'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['text'] == 'hello world'


# ══════════════════════════════════════════════════════════════════
# API Endpoints Tests
# ══════════════════════════════════════════════════════════════════

class TestToolsAPIEndpoints:
    """Tests for /api/tools/* endpoints."""

    @pytest.fixture
    def client(self):
        from langchain_gpt_api import app
        app.config['TESTING'] = True
        with app.test_client() as c:
            yield c

    def test_tools_status(self, client):
        resp = client.get('/api/tools/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_tools_vram(self, client):
        resp = client.get('/api/tools/vram')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'gpu' in data or 'error' in data

    def test_tools_setup_unknown(self, client):
        resp = client.post('/api/tools/nonexistent/setup')
        data = resp.get_json()
        assert 'error' in data

    def test_tools_stop(self, client):
        resp = client.post('/api/tools/wan2gp/stop',
                          content_type='application/json')
        assert resp.status_code == 200

    def test_tools_unload(self, client):
        resp = client.post('/api/tools/wan2gp/unload',
                          content_type='application/json')
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════
# Media Agent Tests
# ══════════════════════════════════════════════════════════════════

class TestMediaAgent:
    """Tests for integrations/service_tools/media_agent.py"""

    def test_invalid_modality(self):
        from integrations.service_tools.media_agent import generate_media
        result = json.loads(generate_media(
            context="test", output_modality="hologram"))
        assert result['status'] == 'error'
        assert 'Invalid output_modality' in result['error']

    def test_valid_modalities_list(self):
        from integrations.service_tools.media_agent import VALID_MODALITIES
        assert 'image' in VALID_MODALITIES
        assert 'audio_speech' in VALID_MODALITIES
        assert 'audio_music' in VALID_MODALITIES
        assert 'video' in VALID_MODALITIES
        assert 'video_with_audio' in VALID_MODALITIES
        assert 'audio_speech_music' in VALID_MODALITIES

    @patch('integrations.service_tools.media_agent._generate_image')
    def test_generate_image_routing(self, mock_img):
        from integrations.service_tools.media_agent import generate_media
        mock_img.return_value = {
            'status': 'completed', 'output_modality': 'image',
            'results': [{'type': 'image', 'url': 'http://test/img.png'}],
            'model_used': 'txt2img',
        }
        result = json.loads(generate_media(
            context="a sunset", output_modality="image"))
        assert result['status'] == 'completed'
        assert result['output_modality'] == 'image'
        mock_img.assert_called_once()

    @patch('integrations.service_tools.media_agent._generate_audio_speech')
    def test_generate_audio_speech_routing(self, mock_tts):
        from integrations.service_tools.media_agent import generate_media
        mock_tts.return_value = {
            'status': 'completed', 'output_modality': 'audio_speech',
            'results': [{'type': 'audio', 'url': 'http://test/audio.wav'}],
            'model_used': 'tts_audio_suite',
        }
        result = json.loads(generate_media(
            context="hello world", output_modality="audio_speech"))
        assert result['status'] == 'completed'
        mock_tts.assert_called_once()

    @patch('integrations.service_tools.media_agent._generate_audio_music')
    def test_generate_audio_music_routing(self, mock_music):
        from integrations.service_tools.media_agent import generate_media
        mock_music.return_value = {
            'status': 'pending', 'output_modality': 'audio_music',
            'task_id': 'acestep_abc123', 'poll_tool': 'check_media_status',
            'model_used': 'acestep',
        }
        result = json.loads(generate_media(
            context="a song about AI", output_modality="audio_music"))
        assert result['status'] == 'pending'
        assert result['task_id'] == 'acestep_abc123'
        mock_music.assert_called_once()

    @patch('integrations.service_tools.media_agent._generate_video')
    def test_generate_video_routing(self, mock_vid):
        from integrations.service_tools.media_agent import generate_media
        mock_vid.return_value = {
            'status': 'pending', 'output_modality': 'video',
            'task_id': 'wan2gp_xyz', 'poll_tool': 'check_media_status',
            'model_used': 'wan2gp',
        }
        result = json.loads(generate_media(
            context="a sunset timelapse", output_modality="video"))
        assert result['status'] == 'pending'
        mock_vid.assert_called_once()

    @patch('integrations.service_tools.media_agent._generate_audio_speech')
    @patch('integrations.service_tools.media_agent._generate_video')
    def test_video_with_audio_routing(self, mock_vid, mock_speech):
        from integrations.service_tools.media_agent import generate_media
        mock_vid.return_value = {
            'status': 'completed', 'output_modality': 'video',
            'results': [{'type': 'video', 'url': 'http://test/vid.mp4'}],
        }
        mock_speech.return_value = {
            'status': 'completed', 'output_modality': 'audio_speech',
            'results': [{'type': 'audio', 'url': 'http://test/speech.wav'}],
        }
        result = json.loads(generate_media(
            context="narrated sunset", output_modality="video_with_audio"))
        assert result['status'] == 'completed'
        assert len(result['results']) == 2

    def test_generation_time_included(self):
        from integrations.service_tools.media_agent import generate_media
        result = json.loads(generate_media(
            context="test", output_modality="invalid_thing"))
        assert 'generation_time_seconds' in result

    def test_select_video_tool_no_gpu(self):
        from integrations.service_tools.media_agent import _select_video_tool
        from integrations.service_tools.vram_manager import vram_manager
        original = vram_manager._gpu_info
        try:
            vram_manager._gpu_info = {
                'cuda_available': False, 'total_gb': 0,
                'free_gb': 0, 'name': None,
            }
            assert _select_video_tool() == 'ltx2'
        finally:
            vram_manager._gpu_info = original

    def test_select_video_tool_plenty_vram(self):
        from integrations.service_tools.media_agent import _select_video_tool
        from integrations.service_tools.vram_manager import vram_manager
        original = vram_manager._gpu_info
        try:
            vram_manager._gpu_info = {
                'cuda_available': True, 'total_gb': 12.0,
                'free_gb': 10.0, 'name': 'RTX 3080',
            }
            assert _select_video_tool() == 'wan2gp'
        finally:
            vram_manager._gpu_info = original

    def test_ensure_tool_running_already_running(self):
        from integrations.service_tools.media_agent import _ensure_tool_running
        mock_rtm = MagicMock()
        mock_rtm.get_tool_status.return_value = {'running': True}
        with patch('integrations.service_tools.runtime_manager.runtime_tool_manager', mock_rtm):
            result = _ensure_tool_running('tts_audio_suite')
            assert result is True

    def test_check_media_status_invalid_format(self):
        from integrations.service_tools.media_agent import check_media_status
        result = json.loads(check_media_status(task_id="notaskprefix"))
        assert result['status'] == 'error'
        assert 'Invalid task_id' in result['error']

    def test_check_media_status_unknown_prefix(self):
        from integrations.service_tools.media_agent import check_media_status
        result = json.loads(check_media_status(task_id="unknown_123"))
        assert result['status'] == 'error'
        assert 'Unknown tool prefix' in result['error']

    @patch('integrations.service_tools.media_agent._get_tool_base_url')
    @patch('core.http_pool.pooled_post')
    def test_check_media_status_wan2gp(self, mock_post, mock_url):
        from integrations.service_tools.media_agent import check_media_status
        mock_url.return_value = 'http://127.0.0.1:9999'
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'status': 'completed', 'video_url': 'http://test/vid.mp4',
        }
        mock_post.return_value = mock_resp
        result = json.loads(check_media_status(task_id="wan2gp_task42"))
        assert result['status'] == 'completed'
        assert result['results'][0]['type'] == 'video'
        mock_post.assert_called_once()

    @patch('integrations.service_tools.media_agent._get_tool_base_url')
    @patch('core.http_pool.pooled_post')
    def test_check_media_status_acestep_default_url(self, mock_post, mock_url):
        from integrations.service_tools.media_agent import check_media_status
        mock_url.return_value = None  # not registered → use default
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'status': 'processing', 'progress': 50,
        }
        mock_post.return_value = mock_resp
        result = json.loads(check_media_status(task_id="acestep_abc"))
        assert result['status'] == 'processing'
        assert result['progress'] == 50
        # Should use default URL http://localhost:8001
        call_url = mock_post.call_args[0][0]
        assert 'localhost:8001' in call_url

    def test_register_media_tools(self):
        """Verify register_media_tools registers both tools."""
        from integrations.service_tools.media_agent import register_media_tools
        mock_helper = MagicMock()
        mock_assistant = MagicMock()

        # Make register_for_llm return a callable decorator
        mock_helper.register_for_llm.return_value = lambda f: f
        mock_assistant.register_for_execution.return_value = lambda f: f

        register_media_tools(mock_helper, mock_assistant)

        # Should register generate_media + check_media_status = 2 tools
        assert mock_helper.register_for_llm.call_count == 2
        assert mock_assistant.register_for_execution.call_count == 2

        # Check the names
        names = [call[1]['name'] for call in mock_helper.register_for_llm.call_args_list]
        assert 'generate_media' in names
        assert 'check_media_status' in names
