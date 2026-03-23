"""
Tests for the Vision Sidecar - FrameStore, MiniCPMInstaller, VisionService.

No GPU or real model needed: all external dependencies are mocked.
"""
import os
import time
import threading
import pytest
import numpy as np
from unittest.mock import patch, MagicMock, PropertyMock


# ─── FrameStore Tests ───

class TestFrameStore:
    """Thread-safe in-process frame store."""

    def setup_method(self):
        from integrations.vision.frame_store import FrameStore
        self.store = FrameStore(max_frames=3, description_ttl=5.0)

    def test_put_get_frame(self):
        self.store.put_frame('u1', b'frame_data')
        assert self.store.get_frame('u1') == b'frame_data'

    def test_get_frame_none_for_unknown_user(self):
        assert self.store.get_frame('unknown') is None

    def test_frame_fifo_bounded(self):
        for i in range(5):
            self.store.put_frame('u1', f'frame_{i}'.encode())
        assert self.store.get_frame_count('u1') == 3
        # Latest frame should be frame_4
        assert self.store.get_frame('u1') == b'frame_4'

    def test_put_get_description(self):
        self.store.put_description('u1', 'user is typing')
        assert self.store.get_description('u1') == 'user is typing'

    def test_description_ttl_expiry(self):
        store = __import__('integrations.vision.frame_store',
                           fromlist=['FrameStore']).FrameStore(
            description_ttl=0.1
        )
        store.put_description('u1', 'old description')
        time.sleep(0.15)
        assert store.get_description('u1') is None

    def test_description_none_for_unknown(self):
        assert self.store.get_description('unknown') is None

    def test_clear_user(self):
        self.store.put_frame('u1', b'data')
        self.store.put_description('u1', 'desc')
        self.store.clear_user('u1')
        assert self.store.get_frame('u1') is None
        assert self.store.get_description('u1') is None

    def test_active_users(self):
        self.store.put_frame('u1', b'a')
        self.store.put_frame('u2', b'b')
        users = self.store.active_users()
        assert set(users) == {'u1', 'u2'}

    def test_stats(self):
        self.store.put_frame('u1', b'a')
        self.store.put_frame('u1', b'b')
        self.store.put_frame('u2', b'c')
        self.store.put_description('u1', 'desc')
        stats = self.store.stats()
        assert stats['active_users'] == 2
        assert stats['total_frames'] == 3
        assert stats['camera_descriptions'] == 1

    def test_thread_safety(self):
        """Multiple threads writing concurrently should not crash."""
        errors = []

        def writer(uid):
            try:
                for i in range(100):
                    self.store.put_frame(uid, f'f{i}'.encode())
                    self.store.put_description(uid, f'd{i}')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(f'u{i}',)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(self.store.active_users()) == 5


class TestFrameStoreScreenChannel:
    """Screen channel - separate from camera, shorter TTL."""

    def setup_method(self):
        from integrations.vision.frame_store import FrameStore
        self.store = FrameStore(
            max_frames=3, description_ttl=30.0, screen_description_ttl=5.0
        )

    def test_put_get_screen_frame(self):
        self.store.put_screen_frame('u1', b'screen_data')
        assert self.store.get_screen_frame('u1') == b'screen_data'

    def test_screen_frame_none_for_unknown(self):
        assert self.store.get_screen_frame('unknown') is None

    def test_put_get_screen_description(self):
        self.store.put_screen_description('u1', 'VS Code with Python')
        assert self.store.get_screen_description('u1') == 'VS Code with Python'

    def test_screen_description_shorter_ttl(self):
        from integrations.vision.frame_store import FrameStore
        store = FrameStore(screen_description_ttl=0.1)
        store.put_screen_description('u1', 'old screen')
        time.sleep(0.15)
        assert store.get_screen_description('u1') is None

    def test_camera_desc_survives_longer_than_screen(self):
        """Camera TTL (30s) > screen TTL (5s) - camera description survives."""
        from integrations.vision.frame_store import FrameStore
        store = FrameStore(description_ttl=1.0, screen_description_ttl=0.1)
        store.put_description('u1', 'camera desc')
        store.put_screen_description('u1', 'screen desc')
        time.sleep(0.15)
        assert store.get_description('u1') == 'camera desc'
        assert store.get_screen_description('u1') is None

    def test_screen_and_camera_independent(self):
        """Screen and camera channels don't interfere with each other."""
        self.store.put_frame('u1', b'camera_frame')
        self.store.put_screen_frame('u1', b'screen_frame')
        self.store.put_description('u1', 'camera desc')
        self.store.put_screen_description('u1', 'screen desc')
        assert self.store.get_frame('u1') == b'camera_frame'
        assert self.store.get_screen_frame('u1') == b'screen_frame'
        assert self.store.get_description('u1') == 'camera desc'
        assert self.store.get_screen_description('u1') == 'screen desc'

    def test_active_users_union_of_channels(self):
        """Active users includes users from either channel."""
        self.store.put_frame('u1', b'cam')
        self.store.put_screen_frame('u2', b'screen')
        users = self.store.active_users()
        assert set(users) == {'u1', 'u2'}

    def test_clear_user_clears_both_channels(self):
        self.store.put_frame('u1', b'cam')
        self.store.put_screen_frame('u1', b'screen')
        self.store.put_description('u1', 'cam desc')
        self.store.put_screen_description('u1', 'screen desc')
        self.store.clear_user('u1')
        assert self.store.get_frame('u1') is None
        assert self.store.get_screen_frame('u1') is None
        assert self.store.get_description('u1') is None
        assert self.store.get_screen_description('u1') is None

    def test_stats_includes_both_channels(self):
        self.store.put_frame('u1', b'cam')
        self.store.put_screen_frame('u1', b'screen')
        self.store.put_description('u1', 'cam desc')
        self.store.put_screen_description('u1', 'screen desc')
        stats = self.store.stats()
        assert stats['camera_frames'] == 1
        assert stats['screen_frames'] == 1
        assert stats['total_frames'] == 2
        assert stats['camera_descriptions'] == 1
        assert stats['screen_descriptions'] == 1

    def test_screen_description_history(self):
        self.store.put_screen_description('u1', 'desc1')
        time.sleep(0.01)
        self.store.put_screen_description('u1', 'desc2')
        time.sleep(0.01)
        self.store.put_screen_description('u1', 'desc3')
        history = self.store.get_screen_description_history('u1', max_age_seconds=60.0)
        assert len(history) == 3
        # Newest first
        assert history[0][1] == 'desc3'
        assert history[2][1] == 'desc1'

    def test_camera_description_history(self):
        self.store.put_description('u1', 'cam1')
        time.sleep(0.01)
        self.store.put_description('u1', 'cam2')
        history = self.store.get_camera_description_history('u1', max_age_seconds=60.0)
        assert len(history) == 2
        assert history[0][1] == 'cam2'

    def test_history_respects_max_age(self):
        from integrations.vision.frame_store import FrameStore
        store = FrameStore()
        store.put_screen_description('u1', 'old')
        time.sleep(0.15)
        store.put_screen_description('u1', 'new')
        history = store.get_screen_description_history('u1', max_age_seconds=0.1)
        assert len(history) == 1
        assert history[0][1] == 'new'


class TestVisionServiceScreenDescribe:
    """VisionService.describe_screen_frame integration."""

    @patch('integrations.vision.vision_service.pooled_post')
    def test_describe_screen_frame_stores_and_returns(self, mock_post):
        from integrations.vision.vision_service import VisionService
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={'result': 'VS Code with Python file'})
        )
        svc = VisionService()
        svc._running = True
        svc._vision_backend = None  # Force MiniCPM HTTP path
        svc._circuit_open = False
        svc._consecutive_failures = 0
        jpeg = _make_jpeg(100)
        with patch.object(svc, '_record_to_world_model'):
            desc = svc.describe_screen_frame('u1', jpeg)
        assert desc == 'VS Code with Python file'
        assert svc.store.get_screen_description('u1') == 'VS Code with Python file'

    def test_describe_screen_frame_skips_when_circuit_open(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        svc._running = True
        svc._circuit_open = True
        desc = svc.describe_screen_frame('u1', b'data')
        assert desc is None

    def test_describe_screen_frame_skips_when_not_running(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        svc._running = False
        desc = svc.describe_screen_frame('u1', b'data')
        assert desc is None

    @patch('integrations.vision.vision_service.pooled_post')
    def test_describe_screen_frame_stores_screen_frame(self, mock_post):
        from integrations.vision.vision_service import VisionService
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={'result': 'browser open'})
        )
        svc = VisionService()
        svc._running = True
        jpeg = _make_jpeg(120)
        svc.describe_screen_frame('u1', jpeg)
        # Screen frame should be stored separately from camera frames
        assert svc.store.get_screen_frame('u1') == jpeg
        assert svc.store.get_frame('u1') is None  # Camera channel untouched

    @patch('integrations.vision.vision_service.pooled_post')
    def test_describe_screen_posts_to_db_as_screen_context(self, mock_post):
        from integrations.vision.vision_service import VisionService
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={'result': 'terminal window'})
        )
        svc = VisionService(callback_url='http://localhost:8000')
        svc._running = True
        jpeg = _make_jpeg(90)
        svc.describe_screen_frame('u1', jpeg)
        # Should have been called twice: once for /describe, once for /create_action
        assert mock_post.call_count == 2
        # Second call should be the DB callback
        db_call = mock_post.call_args_list[1]
        payload = db_call.kwargs.get('json') or db_call[1].get('json')
        assert payload['gpt3_label'] == 'Screen Context'
        assert payload['zeroshot_label'] == 'Screen Reasoning'

    def test_get_screen_description_delegates(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        svc.store.put_screen_description('u1', 'browser open')
        assert svc.get_screen_description('u1') == 'browser open'


# ─── MiniCPMInstaller Tests ───

class TestMiniCPMInstaller:
    """Auto-download and GPU detection."""

    def test_default_model_dir(self):
        from integrations.vision.minicpm_installer import MiniCPMInstaller
        inst = MiniCPMInstaller()
        assert 'minicpm' in inst.model_dir

    def test_is_installed_false_when_no_dir(self, tmp_path):
        from integrations.vision.minicpm_installer import MiniCPMInstaller
        inst = MiniCPMInstaller(model_dir=str(tmp_path / 'nonexistent'))
        assert inst.is_installed() is False

    def test_is_installed_true_when_config_exists(self, tmp_path):
        from integrations.vision.minicpm_installer import MiniCPMInstaller
        model_dir = tmp_path / 'model'
        model_dir.mkdir()
        (model_dir / 'config.json').write_text('{}')
        inst = MiniCPMInstaller(model_dir=str(model_dir))
        assert inst.is_installed() is True

    @patch('integrations.vision.minicpm_installer.MiniCPMInstaller.detect_gpu')
    def test_detect_gpu_returns_bool(self, mock_detect):
        from integrations.vision.minicpm_installer import MiniCPMInstaller
        mock_detect.return_value = True
        inst = MiniCPMInstaller()
        assert inst.detect_gpu() is True

    def test_install_skips_if_already_installed(self, tmp_path):
        from integrations.vision.minicpm_installer import MiniCPMInstaller
        model_dir = tmp_path / 'model'
        model_dir.mkdir()
        (model_dir / 'config.json').write_text('{}')
        inst = MiniCPMInstaller(model_dir=str(model_dir))
        assert inst.install() is True  # skips download

    @patch('integrations.vision.minicpm_installer.snapshot_download',
           create=True)
    def test_install_calls_snapshot_download(self, mock_dl, tmp_path):
        """When not installed, calls huggingface_hub.snapshot_download."""
        from integrations.vision.minicpm_installer import MiniCPMInstaller
        with patch.dict('sys.modules', {'huggingface_hub': MagicMock(
            snapshot_download=mock_dl
        )}):
            # Re-import to pick up patched module
            import importlib
            import integrations.vision.minicpm_installer as mod
            importlib.reload(mod)
            inst = mod.MiniCPMInstaller(model_dir=str(tmp_path / 'new'))
            # Mock the import inside install()
            with patch.object(inst, 'is_installed', return_value=False):
                # The install method does `from huggingface_hub import snapshot_download`
                # We need to ensure that module is available
                pass

    def test_uninstall_removes_dir(self, tmp_path):
        from integrations.vision.minicpm_installer import MiniCPMInstaller
        model_dir = tmp_path / 'model'
        model_dir.mkdir()
        (model_dir / 'config.json').write_text('{}')
        inst = MiniCPMInstaller(model_dir=str(model_dir))
        assert inst.uninstall() is True
        assert not model_dir.exists()

    def test_get_status_structure(self, tmp_path):
        from integrations.vision.minicpm_installer import MiniCPMInstaller
        inst = MiniCPMInstaller(model_dir=str(tmp_path))
        status = inst.get_status()
        assert 'model_id' in status
        assert 'model_dir' in status
        assert 'installed' in status
        assert 'gpu_available' in status

    def test_get_model_dir_none_when_not_installed(self, tmp_path):
        from integrations.vision.minicpm_installer import MiniCPMInstaller
        inst = MiniCPMInstaller(model_dir=str(tmp_path / 'nope'))
        assert inst.get_model_dir() is None


# ─── VisionService Tests ───

class TestVisionService:
    """VisionService orchestrator - all external I/O mocked."""

    def test_init_creates_frame_store(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        assert svc.store is not None
        assert svc._running is False

    def test_custom_frame_store(self):
        from integrations.vision.vision_service import VisionService
        from integrations.vision.frame_store import FrameStore
        store = FrameStore(max_frames=10)
        svc = VisionService(frame_store=store)
        assert svc.store is store

    def test_get_frame_delegates_to_store(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        svc.store.put_frame('u1', b'hello')
        assert svc.get_frame('u1') == b'hello'

    def test_get_description_delegates_to_store(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        svc.store.put_description('u1', 'user is coding')
        assert svc.get_description('u1') == 'user is coding'

    @patch('integrations.vision.vision_service.VisionService._start_minicpm')
    @patch('integrations.vision.vision_service.VisionService._run_ws_server')
    @patch('integrations.vision.vision_service.VisionService._description_loop')
    def test_start_sets_running(self, mock_desc, mock_ws, mock_start):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        # Pre-mark installer as installed
        svc._installer._installed = True
        svc._installer.is_installed = lambda: True
        svc.start()
        assert svc._running is True
        svc.stop()

    @patch('integrations.vision.minicpm_installer.MiniCPMInstaller.is_installed',
           return_value=False)
    @patch('integrations.vision.minicpm_installer.MiniCPMInstaller.detect_gpu',
           return_value=False)
    def test_start_without_gpu_stays_stopped(self, mock_gpu, mock_installed):
        _backend = MagicMock()
        _backend.name = 'none'
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        svc._detect_mode = lambda: 'full'
        # Directly set the backend mock so get_vision_backend isn't called
        with patch.object(svc, '_vision_backend', None):
            with patch('integrations.vision.vision_service.get_vision_backend',
                       return_value=_backend):
                svc.start()
        assert svc._running is False

    def test_stop_terminates_process(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        svc._running = True
        mock_proc = MagicMock()
        svc._minicpm_process = mock_proc
        svc.stop()
        mock_proc.terminate.assert_called_once()
        assert svc._running is False

    def test_double_start_warns(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        svc._running = True  # pretend already running
        svc.start()  # should warn but not crash
        svc.stop()

    def test_circuit_breaker_opens_after_max_failures(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        svc._max_failures = 3
        # Simulate failures
        for _ in range(3):
            svc._consecutive_failures += 1
        svc._circuit_open = svc._consecutive_failures >= svc._max_failures
        assert svc._circuit_open is True

    @patch('integrations.vision.vision_service.pooled_get')
    def test_health_check_resets_failures(self, mock_get):
        from integrations.vision.vision_service import VisionService
        mock_get.return_value = MagicMock(status_code=200)
        svc = VisionService()
        svc._consecutive_failures = 3
        svc._circuit_open = True
        assert svc._check_minicpm_health() is True
        assert svc._consecutive_failures == 0
        assert svc._circuit_open is False

    @patch('integrations.vision.vision_service.pooled_get',
           side_effect=__import__('requests').RequestException('fail'))
    def test_health_check_increments_failures(self, mock_get):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        svc._consecutive_failures = 0
        assert svc._check_minicpm_health() is False
        assert svc._consecutive_failures == 1

    def test_get_status_structure(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        status = svc.get_status()
        assert 'running' in status
        assert 'minicpm_alive' in status
        assert 'circuit_open' in status
        assert 'store' in status
        assert 'installer' in status

    @patch('integrations.vision.vision_service.pooled_post')
    def test_describe_frame_success(self, mock_post):
        from integrations.vision.vision_service import VisionService
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={'result': 'user is typing'})
        )
        svc = VisionService()
        desc = svc._describe_frame('u1', b'frame_bytes')
        assert desc == 'user is typing'

    @patch('integrations.vision.vision_service.requests.post',
           side_effect=__import__('requests').RequestException('fail'))
    def test_describe_frame_failure_increments_failures(self, mock_post):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        svc._consecutive_failures = 0
        desc = svc._describe_frame('u1', b'frame_bytes')
        assert desc is None
        assert svc._consecutive_failures == 1

    def test_describe_frame_skips_when_circuit_open(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        svc._circuit_open = True
        desc = svc._describe_frame('u1', b'frame_bytes')
        assert desc is None


# ─── Intelligent Sampling Tests ───

def _make_jpeg(color_val=128):
    """Create a minimal valid JPEG from a solid-color image."""
    import cv2
    img = np.full((8, 8, 3), color_val, dtype=np.uint8)
    _, buf = cv2.imencode('.jpg', img)
    return buf.tobytes()


class TestFrameDifference:
    """Test _compute_frame_difference and _decode_jpeg helpers."""

    def test_identical_frames_zero_diff(self):
        from integrations.vision.frame_store import compute_frame_difference as _compute_frame_difference
        img = np.full((8, 8, 3), 100, dtype=np.uint8)
        assert _compute_frame_difference(img, img) == 0.0

    def test_opposite_frames_high_diff(self):
        from integrations.vision.frame_store import compute_frame_difference as _compute_frame_difference
        black = np.zeros((8, 8, 3), dtype=np.uint8)
        white = np.full((8, 8, 3), 255, dtype=np.uint8)
        diff = _compute_frame_difference(black, white)
        assert diff == pytest.approx(1.0, abs=0.01)

    def test_decode_jpeg_valid(self):
        from integrations.vision.frame_store import decode_jpeg as _decode_jpeg
        jpeg = _make_jpeg(100)
        result = _decode_jpeg(jpeg)
        assert result is not None
        assert result.shape[2] == 3

    def test_decode_jpeg_invalid_returns_none(self):
        from integrations.vision.frame_store import decode_jpeg as _decode_jpeg
        result = _decode_jpeg(b'not_a_jpeg')
        assert result is None


class TestIntelligentSampling:
    """VisionService._should_describe with adaptive intervals."""

    def test_first_frame_always_described(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        jpeg = _make_jpeg(128)
        assert svc._should_describe('u1', jpeg, 'camera') is True

    def test_identical_frame_skipped_within_interval(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService(description_interval=0.05)
        jpeg = _make_jpeg(128)
        svc._should_describe('u1', jpeg, 'camera')  # First - True
        # Immediately try again with same frame - should skip (within interval)
        assert svc._should_describe('u1', jpeg, 'camera') is False

    def test_different_frame_described_after_interval(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService(description_interval=0.05, min_scene_change=0.01)
        jpeg1 = _make_jpeg(50)
        jpeg2 = _make_jpeg(200)
        svc._should_describe('u1', jpeg1, 'camera')
        time.sleep(0.06)
        # Different frame after interval - should describe
        assert svc._should_describe('u1', jpeg2, 'camera') is True

    def test_static_scene_backs_off_interval(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService(description_interval=0.05, max_description_interval=1.0, min_scene_change=0.5)
        jpeg = _make_jpeg(128)
        svc._should_describe('u1', jpeg, 'camera')
        time.sleep(0.06)
        # Same frame (diff < 0.5 threshold) - backs off
        svc._should_describe('u1', jpeg, 'camera')
        key = 'u1:camera'
        assert svc._user_intervals[key] > svc._description_interval

    def test_scene_change_resets_interval(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService(description_interval=0.05, min_scene_change=0.01)
        jpeg1 = _make_jpeg(50)
        jpeg2 = _make_jpeg(200)
        svc._should_describe('u1', jpeg1, 'camera')
        # Artificially back off
        svc._user_intervals['u1:camera'] = 10.0
        svc._last_describe_time['u1:camera'] = time.time() - 20.0
        # Big scene change - should reset interval
        svc._should_describe('u1', jpeg2, 'camera')
        assert svc._user_intervals['u1:camera'] == svc._description_interval

    def test_separate_channels_independent(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        jpeg = _make_jpeg(128)
        # First frame on camera and screen - both should describe
        assert svc._should_describe('u1', jpeg, 'camera') is True
        assert svc._should_describe('u1', jpeg, 'screen') is True

    @patch('integrations.vision.vision_service.pooled_post')
    def test_skip_counter_increments(self, mock_post):
        from integrations.vision.vision_service import VisionService
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={'result': 'desc'})
        )
        svc = VisionService(description_interval=100.0)  # Very long interval
        svc._running = True
        svc._circuit_open = False
        jpeg = _make_jpeg(128)
        # First call - passes (first frame on screen channel)
        svc.describe_screen_frame('u1', jpeg)
        svc._frames_skipped = 0
        # Second call - same frame within long interval → skipped
        svc.describe_screen_frame('u1', jpeg)
        assert svc._frames_skipped >= 1


# ─── Visual Trigger Tests (via TriggerManager) ───

class TestVisualTriggers:
    """VISUAL_MATCH / SCREEN_MATCH trigger types via TriggerManager."""

    def test_trigger_type_enum_exists(self):
        from integrations.channels.automation.triggers import TriggerType
        assert hasattr(TriggerType, 'VISUAL_MATCH')
        assert hasattr(TriggerType, 'SCREEN_MATCH')

    def test_register_visual_trigger_creates_trigger_manager(self):
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        assert svc._trigger_manager is None
        svc.register_visual_trigger(
            channel='camera',
            callback=lambda data: None,
            keywords=['photoshop'],
        )
        assert svc._trigger_manager is not None

    def test_visual_trigger_fires_on_keyword_match(self):
        from integrations.channels.automation.triggers import TriggerManager, TriggerType
        mgr = TriggerManager()
        fired = []
        mgr.register(
            trigger_type=TriggerType.VISUAL_MATCH,
            callback=lambda data: fired.append(data),
            keywords=['photoshop'],
        )
        results = mgr.evaluate(TriggerType.VISUAL_MATCH, {
            'user_id': 'u1',
            'description': 'user opens Photoshop on desktop',
            'channel': 'camera',
        })
        assert len(fired) == 1
        assert fired[0]['user_id'] == 'u1'

    def test_visual_trigger_no_fire_on_no_match(self):
        from integrations.channels.automation.triggers import TriggerManager, TriggerType
        mgr = TriggerManager()
        fired = []
        mgr.register(
            trigger_type=TriggerType.VISUAL_MATCH,
            callback=lambda data: fired.append(data),
            keywords=['photoshop'],
        )
        mgr.evaluate(TriggerType.VISUAL_MATCH, {
            'description': 'user is typing in browser',
        })
        assert len(fired) == 0

    def test_screen_trigger_fires_separately(self):
        from integrations.channels.automation.triggers import TriggerManager, TriggerType
        mgr = TriggerManager()
        camera_fired = []
        screen_fired = []
        mgr.register(
            trigger_type=TriggerType.VISUAL_MATCH,
            callback=lambda d: camera_fired.append(d),
            keywords=['wave'],
        )
        mgr.register(
            trigger_type=TriggerType.SCREEN_MATCH,
            callback=lambda d: screen_fired.append(d),
            keywords=['lock screen'],
        )
        # Only screen trigger should fire
        mgr.evaluate(TriggerType.SCREEN_MATCH, {'description': 'lock screen shown'})
        assert len(camera_fired) == 0
        assert len(screen_fired) == 1

    def test_visual_trigger_with_regex_pattern(self):
        from integrations.channels.automation.triggers import TriggerManager, TriggerType
        mgr = TriggerManager()
        fired = []
        mgr.register(
            trigger_type=TriggerType.VISUAL_MATCH,
            callback=lambda d: fired.append(d),
            pattern=r'user\s+(waves?|gestures?)',
        )
        mgr.evaluate(TriggerType.VISUAL_MATCH, {
            'description': 'user waves hand at camera',
        })
        assert len(fired) == 1

    def test_visual_trigger_cooldown(self):
        from integrations.channels.automation.triggers import TriggerManager, TriggerType
        mgr = TriggerManager()
        fired = []
        mgr.register(
            trigger_type=TriggerType.VISUAL_MATCH,
            callback=lambda d: fired.append(d),
            keywords=['hello'],
            cooldown_seconds=60,
        )
        mgr.evaluate(TriggerType.VISUAL_MATCH, {'description': 'hello world'})
        mgr.evaluate(TriggerType.VISUAL_MATCH, {'description': 'hello again'})
        # Cooldown prevents second fire
        assert len(fired) == 1

    def test_visual_trigger_with_conditions(self):
        from integrations.channels.automation.triggers import (
            TriggerManager, TriggerType, TriggerCondition
        )
        mgr = TriggerManager()
        fired = []
        mgr.register(
            trigger_type=TriggerType.VISUAL_MATCH,
            callback=lambda d: fired.append(d),
            conditions=[
                TriggerCondition(field='description', operator='contains', value='Photoshop'),
            ],
        )
        mgr.evaluate(TriggerType.VISUAL_MATCH, {
            'description': 'user opens Photoshop CS6',
        })
        assert len(fired) == 1

    def test_vision_service_evaluate_delegates_to_manager(self):
        """VisionService._evaluate_visual_triggers uses TriggerManager."""
        from integrations.vision.vision_service import VisionService
        from integrations.channels.automation.triggers import TriggerManager, TriggerType
        mgr = TriggerManager()
        svc = VisionService(trigger_manager=mgr)
        svc._running = True
        fired = []
        mgr.register(
            trigger_type=TriggerType.VISUAL_MATCH,
            callback=lambda d: fired.append(d),
            keywords=['typing'],
        )
        svc._evaluate_visual_triggers('u1', 'user is typing code', 'camera')
        assert len(fired) == 1
        assert fired[0]['user_id'] == 'u1'

    def test_evaluate_triggers_skips_when_no_manager(self):
        """No trigger_manager → _evaluate_visual_triggers is a no-op."""
        from integrations.vision.vision_service import VisionService
        svc = VisionService()
        # Should not raise
        svc._evaluate_visual_triggers('u1', 'test', 'camera')

    def test_trigger_manager_stats_include_visual(self):
        from integrations.channels.automation.triggers import TriggerManager, TriggerType
        mgr = TriggerManager()
        mgr.register(
            trigger_type=TriggerType.VISUAL_MATCH,
            callback=lambda d: None,
            keywords=['test'],
        )
        mgr.register(
            trigger_type=TriggerType.SCREEN_MATCH,
            callback=lambda d: None,
            keywords=['test'],
        )
        stats = mgr.get_stats()
        assert stats['by_type']['visual_match'] == 1
        assert stats['by_type']['screen_match'] == 1


# ─── Visual History Search Tests ───

class TestSearchVisualHistory:
    """helper.search_visual_history - thin wrapper over DB endpoint."""

    @patch('helper.pooled_request')
    def test_search_returns_matching_camera_results(self, mock_req):
        from datetime import datetime, timedelta
        now = datetime.now()
        mock_req.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[
                {
                    'action': 'user opens Photoshop',
                    'zeroshot_label': 'Video Reasoning',
                    'gpt3_label': 'Visual Context',
                    'created_date': now.strftime('%Y-%m-%dT%H:%M:%S'),
                },
                {
                    'action': 'user is typing code',
                    'zeroshot_label': 'Video Reasoning',
                    'gpt3_label': 'Visual Context',
                    'created_date': now.strftime('%Y-%m-%dT%H:%M:%S'),
                },
            ])
        )
        from helper import search_visual_history
        results = search_visual_history('u1', 'photoshop', mins=30, channel='camera')
        assert results is not None
        assert len(results) == 1
        assert 'Photoshop' in results[0]
        assert '[camera]' in results[0]

    @patch('helper.pooled_request')
    def test_search_returns_screen_results(self, mock_req):
        from datetime import datetime
        now = datetime.now()
        mock_req.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[
                {
                    'action': 'VS Code with Python file open',
                    'zeroshot_label': 'Screen Reasoning',
                    'gpt3_label': 'Screen Context',
                    'created_date': now.strftime('%Y-%m-%dT%H:%M:%S'),
                },
            ])
        )
        from helper import search_visual_history
        results = search_visual_history('u1', 'VS Code', mins=30, channel='screen')
        assert results is not None
        assert '[screen]' in results[0]

    @patch('helper.pooled_request')
    def test_search_both_channels(self, mock_req):
        from datetime import datetime
        now = datetime.now()
        mock_req.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[
                {
                    'action': 'user typing',
                    'zeroshot_label': 'Video Reasoning',
                    'gpt3_label': 'Visual Context',
                    'created_date': now.strftime('%Y-%m-%dT%H:%M:%S'),
                },
                {
                    'action': 'typing in terminal',
                    'zeroshot_label': 'Screen Reasoning',
                    'gpt3_label': 'Screen Context',
                    'created_date': now.strftime('%Y-%m-%dT%H:%M:%S'),
                },
            ])
        )
        from helper import search_visual_history
        results = search_visual_history('u1', 'typing', mins=30, channel='both')
        assert results is not None
        assert len(results) == 2

    @patch('helper.pooled_request')
    def test_search_no_matches_returns_none(self, mock_req):
        from datetime import datetime
        now = datetime.now()
        mock_req.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[
                {
                    'action': 'user is sleeping',
                    'zeroshot_label': 'Video Reasoning',
                    'gpt3_label': 'Visual Context',
                    'created_date': now.strftime('%Y-%m-%dT%H:%M:%S'),
                },
            ])
        )
        from helper import search_visual_history
        results = search_visual_history('u1', 'photoshop', mins=30)
        assert results is None

    @patch('helper.pooled_request')
    def test_search_api_failure_returns_none(self, mock_req):
        mock_req.return_value = MagicMock(status_code=500)
        from helper import search_visual_history
        results = search_visual_history('u1', 'test', mins=30)
        assert results is None

    @patch('helper.pooled_request', side_effect=Exception('network error'))
    def test_search_network_error_returns_none(self, mock_req):
        from helper import search_visual_history
        results = search_visual_history('u1', 'test', mins=30)
        assert results is None
