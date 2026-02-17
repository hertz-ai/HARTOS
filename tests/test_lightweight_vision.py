"""
Tests for lightweight vision backends (Phase 4 — Embedded/Robot Support).

Tests: VisionBackend interface, NoneBackend, MiniCPMBackend, auto-selection,
backend registry.
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

from integrations.vision.lightweight_backend import (
    VisionBackend, MiniCPMBackend, MobileVLMBackend, CLIPBackend, NoneBackend,
    get_vision_backend, list_available_backends, _BACKENDS,
)


class TestVisionBackendInterface:
    """Verify all backends implement the VisionBackend interface."""

    @pytest.mark.parametrize("cls", [MiniCPMBackend, MobileVLMBackend,
                                      CLIPBackend, NoneBackend])
    def test_is_vision_backend(self, cls):
        backend = cls()
        assert isinstance(backend, VisionBackend)

    @pytest.mark.parametrize("cls", [MiniCPMBackend, MobileVLMBackend,
                                      CLIPBackend, NoneBackend])
    def test_has_name(self, cls):
        backend = cls()
        assert isinstance(backend.name, str)
        assert len(backend.name) > 0

    @pytest.mark.parametrize("cls", [MiniCPMBackend, MobileVLMBackend,
                                      CLIPBackend, NoneBackend])
    def test_has_requires_gpu(self, cls):
        backend = cls()
        assert isinstance(backend.requires_gpu, bool)

    @pytest.mark.parametrize("cls", [MiniCPMBackend, MobileVLMBackend,
                                      CLIPBackend, NoneBackend])
    def test_has_ram_mb(self, cls):
        backend = cls()
        assert isinstance(backend.ram_mb, int)
        assert backend.ram_mb >= 0


class TestNoneBackend:
    """NoneBackend — zero overhead, always available."""

    def test_name(self):
        assert NoneBackend().name == 'none'

    def test_always_available(self):
        assert NoneBackend().is_available() is True

    def test_no_gpu_required(self):
        assert NoneBackend().requires_gpu is False

    def test_zero_ram(self):
        assert NoneBackend().ram_mb == 0

    def test_describe_returns_none(self):
        assert NoneBackend().describe(b'\xff\xd8\xff') is None

    def test_start_returns_true(self):
        assert NoneBackend().start() is True


class TestMiniCPMBackend:
    """MiniCPMBackend — GPU sidecar."""

    def test_name(self):
        assert MiniCPMBackend().name == 'minicpm'

    def test_requires_gpu(self):
        assert MiniCPMBackend().requires_gpu is True

    def test_ram_4gb(self):
        assert MiniCPMBackend().ram_mb == 4000

    def test_port_from_env(self):
        with patch.dict(os.environ, {'HEVOLVE_MINICPM_PORT': '9999'}):
            backend = MiniCPMBackend()
            assert backend._port == 9999

    def test_describe_http_call(self):
        """describe() makes HTTP POST to MiniCPM sidecar."""
        import integrations.vision.lightweight_backend as lvb
        backend = MiniCPMBackend(port=9891)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'description': 'A cat sitting on a desk'}

        with patch.object(lvb.requests, 'post',
                          return_value=mock_resp) as mock_post:
            result = backend.describe(b'fake_jpeg_bytes')
            assert result == 'A cat sitting on a desk'
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            assert '9891' in call_kwargs[0][0]

    def test_describe_failure_returns_none(self):
        import integrations.vision.lightweight_backend as lvb
        import requests as req_mod
        backend = MiniCPMBackend(port=9891)
        with patch.object(lvb.requests, 'post',
                          side_effect=req_mod.RequestException("connection refused")):
            result = backend.describe(b'fake_bytes')
            assert result is None


class TestMobileVLMBackend:
    """MobileVLMBackend — ONNX Runtime CPU."""

    def test_name(self):
        assert MobileVLMBackend().name == 'mobilevlm'

    def test_no_gpu_required(self):
        assert MobileVLMBackend().requires_gpu is False

    def test_ram_300mb(self):
        assert MobileVLMBackend().ram_mb == 300

    def test_available_if_onnxruntime(self):
        """Available only if onnxruntime is installed."""
        mock_onnx = MagicMock()
        with patch.dict(sys.modules, {'onnxruntime': mock_onnx}):
            assert MobileVLMBackend().is_available() is True

    def test_unavailable_without_onnxruntime(self):
        with patch.dict(sys.modules, {'onnxruntime': None}):
            assert MobileVLMBackend().is_available() is False

    def test_describe_without_start_returns_none(self):
        assert MobileVLMBackend().describe(b'bytes') is None


class TestCLIPBackend:
    """CLIPBackend — classification only."""

    def test_name(self):
        assert CLIPBackend().name == 'clip'

    def test_no_gpu_required(self):
        assert CLIPBackend().requires_gpu is False

    def test_ram_400mb(self):
        assert CLIPBackend().ram_mb == 400

    def test_describe_without_start_returns_none(self):
        assert CLIPBackend().describe(b'bytes') is None


class TestBackendRegistry:
    """Verify backend registry."""

    def test_four_backends_registered(self):
        assert len(_BACKENDS) == 4

    def test_all_names_present(self):
        assert 'minicpm' in _BACKENDS
        assert 'mobilevlm' in _BACKENDS
        assert 'clip' in _BACKENDS
        assert 'none' in _BACKENDS

    def test_list_available_backends(self):
        results = list_available_backends()
        assert len(results) == 4
        names = [r['name'] for r in results]
        assert 'none' in names
        # NoneBackend is always available
        none_entry = [r for r in results if r['name'] == 'none'][0]
        assert none_entry['available'] is True
        assert none_entry['requires_gpu'] is False
        assert none_entry['ram_mb'] == 0


class TestGetVisionBackend:
    """Verify auto-selection and explicit selection."""

    def test_explicit_none(self):
        backend = get_vision_backend('none')
        assert backend.name == 'none'

    def test_explicit_minicpm(self):
        backend = get_vision_backend('minicpm')
        assert backend.name == 'minicpm'

    def test_explicit_mobilevlm(self):
        backend = get_vision_backend('mobilevlm')
        assert backend.name == 'mobilevlm'

    def test_explicit_clip(self):
        backend = get_vision_backend('clip')
        assert backend.name == 'clip'

    def test_env_var_override(self):
        with patch.dict(os.environ, {'HEVOLVE_VISION_BACKEND': 'none'}):
            backend = get_vision_backend()
            assert backend.name == 'none'

    def test_unknown_backend_returns_none_backend(self):
        backend = get_vision_backend('imaginary')
        assert backend.name == 'none'

    def test_auto_select_gpu(self):
        """With 4GB+ VRAM, auto-selects minicpm."""
        mock_caps = MagicMock()
        mock_caps.hardware.gpu_vram_gb = 8
        mock_caps.hardware.ram_gb = 16

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HEVOLVE_VISION_BACKEND', None)
            with patch('security.system_requirements.get_capabilities',
                       return_value=mock_caps):
                backend = get_vision_backend()
                assert backend.name == 'minicpm'

    def test_auto_select_no_gpu_with_onnx(self):
        """With 2GB+ RAM, no GPU, and ONNX available, selects mobilevlm."""
        mock_caps = MagicMock()
        mock_caps.hardware.gpu_vram_gb = 0
        mock_caps.hardware.ram_gb = 4

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HEVOLVE_VISION_BACKEND', None)
            with patch('security.system_requirements.get_capabilities',
                       return_value=mock_caps):
                with patch.object(MobileVLMBackend, 'is_available', return_value=True):
                    backend = get_vision_backend()
                    assert backend.name == 'mobilevlm'

    def test_auto_select_fallback_to_none(self):
        """With no GPU, no ONNX, no CLIP → NoneBackend."""
        mock_caps = MagicMock()
        mock_caps.hardware.gpu_vram_gb = 0
        mock_caps.hardware.ram_gb = 0.5  # Below 1GB

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HEVOLVE_VISION_BACKEND', None)
            with patch('security.system_requirements.get_capabilities',
                       return_value=mock_caps):
                with patch.object(MiniCPMBackend, 'is_available', return_value=False):
                    backend = get_vision_backend()
                    assert backend.name == 'none'


class TestBackendProperties:
    """Verify backend property consistency."""

    def test_gpu_backends_have_high_ram(self):
        """GPU backends should need more RAM."""
        for name, cls in _BACKENDS.items():
            backend = cls()
            if backend.requires_gpu:
                assert backend.ram_mb >= 1000, \
                    f"{name}: GPU backend with low RAM claim"

    def test_cpu_backends_under_1gb(self):
        """CPU-only backends should be under 1GB."""
        for name, cls in _BACKENDS.items():
            backend = cls()
            if not backend.requires_gpu:
                assert backend.ram_mb <= 1000, \
                    f"{name}: CPU backend claiming > 1GB RAM"
