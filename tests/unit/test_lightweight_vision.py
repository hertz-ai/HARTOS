"""
Tests for lightweight vision backends (Phase 4 - Embedded/Robot Support).

Tests: VisionBackend interface, NoneBackend, MiniCPMBackend, auto-selection,
backend registry.
"""
import contextlib
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
    """NoneBackend - zero overhead, always available."""

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
    """MiniCPMBackend - GPU sidecar."""

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
        """describe() makes HTTP POST to MiniCPM sidecar.

        The response key is `result` — that is what minicpm_server.py's
        describe_raw() returns.  This test used to assert `description`,
        a key no HART OS server has ever produced, so the mock agreed
        with the client while the client disagreed with the server.
        test_describe_matches_the_real_sidecar_contract (below) is the
        one that can catch that, because it drives the real server view.
        """
        import integrations.vision.lightweight_backend as lvb
        backend = MiniCPMBackend(port=9891)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'result': 'A cat sitting on a desk'}

        with patch.object(lvb, 'pooled_post',
                          return_value=mock_resp) as mock_post:
            result = backend.describe(b'fake_jpeg_bytes')
            assert result == 'A cat sitting on a desk'
            mock_post.assert_called_once()

    def test_describe_matches_the_real_sidecar_contract(self):
        """The request describe() builds is one minicpm_server actually serves.

        Both halves are real: MiniCPMBackend.describe builds the request,
        and integrations.vision.minicpm_server's own Flask view handles it
        (only the weights are stubbed).  A base64-JSON body — what this
        client sent before 2026-09-21 — reaches PIL.Image.open as JSON
        text and comes back HTTP 500, so this fails on the old shape.
        """
        import integrations.vision.lightweight_backend as lvb
        from integrations.vision import minicpm_server

        png = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00'
               b'\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx'
               b'\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00I'
               b'END\xaeB`\x82')
        client = minicpm_server.app.test_client()
        seen = {}

        def _forward(url, **kwargs):
            seen['url'] = url
            resp = client.post(
                '/describe',
                data=kwargs.get('data'),
                query_string=kwargs.get('params') or {},
                headers=kwargs.get('headers') or {},
            )
            out = MagicMock()
            out.status_code = resp.status_code
            out.json.return_value = resp.get_json()
            out.text = resp.get_data(as_text=True)
            return out

        with patch.object(minicpm_server, '_process_image_sync',
                          lambda image, prompt: f'a 1x1 image; asked: {prompt}'), \
             patch.object(lvb, 'pooled_post', _forward):
            result = MiniCPMBackend(port=9891).describe(png, 'What is this?')

        assert result == 'a 1x1 image; asked: What is this?'
        assert seen['url'].endswith('/describe')

    def test_is_available_needs_the_weights_not_just_a_gpu(self):
        """A GPU with no MiniCPM weights is not a MiniCPM node.

        get_vision_backend() gates its catalog branch and its last-resort
        branch on this, so a True here made a node SELECT a backend with
        nothing behind it.
        """
        from integrations.vision import minicpm_installer as mi

        backend = MiniCPMBackend()
        with patch.object(mi.MiniCPMInstaller, 'detect_gpu', return_value=True), \
             patch.object(mi.MiniCPMInstaller, 'is_installed', return_value=False):
            assert backend.is_available() is False
        with patch.object(mi.MiniCPMInstaller, 'detect_gpu', return_value=True), \
             patch.object(mi.MiniCPMInstaller, 'is_installed', return_value=True):
            assert backend.is_available() is True
        with patch.object(mi.MiniCPMInstaller, 'detect_gpu', return_value=False), \
             patch.object(mi.MiniCPMInstaller, 'is_installed', return_value=True):
            assert backend.is_available() is False

    def test_resolve_port_prefers_the_sidecar_runtime_manager_started(self):
        """A RUNNING RTM sidecar's dynamic port wins over the fixed 9891.

        RTM allocates an OS-assigned port; before this, MiniCPMBackend only
        ever read port_registry's 'vision', so start_tool('minicpm') could
        succeed while the backend posted into a dead port.
        """
        from integrations.service_tools import runtime_manager as rm

        backend = MiniCPMBackend()
        registry_port = backend._registry_port

        fake_rtm = MagicMock()
        fake_rtm.get_tool_port.return_value = 55897
        with patch.object(rm, 'runtime_tool_manager', fake_rtm):
            assert backend._resolve_port() == 55897
        fake_rtm.get_tool_port.assert_called_with('minicpm')

        # Nothing running -> fall back to the fixed-port deployment
        fake_rtm.get_tool_port.return_value = None
        with patch.object(rm, 'runtime_tool_manager', fake_rtm):
            assert backend._resolve_port() == registry_port

    def test_explicit_port_outranks_the_runtime_manager(self):
        """HEVOLVE_MINICPM_PORT / port= is an operator override; it wins."""
        from integrations.service_tools import runtime_manager as rm

        fake_rtm = MagicMock()
        fake_rtm.get_tool_port.return_value = 55897
        with patch.object(rm, 'runtime_tool_manager', fake_rtm):
            assert MiniCPMBackend(port=9999)._resolve_port() == 9999
            with patch.dict(os.environ, {'HEVOLVE_MINICPM_PORT': '7777'}):
                assert MiniCPMBackend()._resolve_port() == 7777

    def test_describe_failure_returns_none(self):
        import integrations.vision.lightweight_backend as lvb
        backend = MiniCPMBackend(port=9891)
        with patch.object(lvb, 'pooled_post',
                          side_effect=Exception("connection refused")):
            result = backend.describe(b'fake_bytes')
            assert result is None


class TestMobileVLMBackend:
    """MobileVLMBackend - ONNX Runtime CPU."""

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
    """CLIPBackend - classification only."""

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

    def test_backends_registered(self):
        assert len(_BACKENDS) == 6  # +1 for qwen08b caption model

    def test_all_names_present(self):
        assert 'qwen3vl' in _BACKENDS
        assert 'qwen08b' in _BACKENDS
        assert 'minicpm' in _BACKENDS
        assert 'mobilevlm' in _BACKENDS
        assert 'clip' in _BACKENDS
        assert 'none' in _BACKENDS

    def test_list_available_backends(self):
        results = list_available_backends()
        assert len(results) == 6
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

    @staticmethod
    @contextlib.contextmanager
    def _upper_tiers_off():
        """Pin every selection tier ABOVE the direct VRAM/RAM fallback OFF.

        get_vision_backend probes, in order: Qwen0.8B server → Qwen3-VL server
        → catalog → direct VRAM/RAM query. These tests are about the LAST
        tier, so all earlier ones must be declared absent — the probes ask the
        REAL machine (is a server up, is a model file installed), so an
        unpinned tier makes the test answer differently per box. That is
        exactly what happened: the tests pinned Qwen3-VL when it was the first
        probe, production then grew the Qwen0.8B preference ABOVE it, and on
        any box with the 0.8B model installed auto-select returned 'qwen08b'
        before reaching a single mocked layer. CI stayed green only because
        the runner has no models — accident, not hermeticity.
        """
        from integrations.vision.lightweight_backend import (
            Qwen08BBackend, Qwen3VLVisionBackend,
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HEVOLVE_VISION_BACKEND', None)
            with patch.object(Qwen08BBackend, 'is_available',
                              return_value=False), \
                 patch.object(Qwen3VLVisionBackend, 'is_available',
                              return_value=False), \
                 patch.dict('sys.modules',
                            {'integrations.service_tools.model_orchestrator': None}):
                yield

    def test_auto_select_gpu(self):
        """With 4GB+ VRAM, auto-selects minicpm via direct VRAM fallback."""
        mock_caps = MagicMock()
        mock_caps.hardware.gpu_vram_gb = 8
        mock_caps.hardware.ram_gb = 16

        with self._upper_tiers_off(), \
             patch('security.system_requirements.get_capabilities',
                   return_value=mock_caps):
            backend = get_vision_backend()
            assert backend.name == 'minicpm'

    def test_auto_select_no_gpu_with_onnx(self):
        """With 2GB+ RAM, no GPU, and ONNX available, selects mobilevlm."""
        mock_caps = MagicMock()
        mock_caps.hardware.gpu_vram_gb = 0
        mock_caps.hardware.ram_gb = 4

        with self._upper_tiers_off(), \
             patch('security.system_requirements.get_capabilities',
                   return_value=mock_caps), \
             patch.object(MobileVLMBackend, 'is_available', return_value=True):
            backend = get_vision_backend()
            assert backend.name == 'mobilevlm'

    def test_auto_select_fallback_to_none(self):
        """With no GPU, no ONNX, no CLIP → NoneBackend."""
        mock_caps = MagicMock()
        mock_caps.hardware.gpu_vram_gb = 0
        mock_caps.hardware.ram_gb = 0.5  # Below 1GB

        with self._upper_tiers_off(), \
             patch('security.system_requirements.get_capabilities',
                   return_value=mock_caps), \
             patch.object(MiniCPMBackend, 'is_available', return_value=False):
            backend = get_vision_backend()
            assert backend.name == 'none'


class TestReadDocument:
    """read_document(): a book page through the node's own vision backend."""

    @staticmethod
    def _page(size=(1700, 2200)):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', size, 'white').save(buf, 'JPEG')
        return buf.getvalue()

    @staticmethod
    def _answer(content='page text', finish='stop', status=200):
        resp = MagicMock(status_code=status)
        resp.json.return_value = {'choices': [{'message': {'content': content},
                                               'finish_reason': finish}]}
        return resp

    @staticmethod
    def _sent_size(post):
        import base64
        import io
        from PIL import Image
        url = post.call_args.kwargs['json']['messages'][0]['content'][0]['image_url']['url']
        with Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))) as im:
            return im.size

    @pytest.mark.parametrize("cls", [MiniCPMBackend, MobileVLMBackend,
                                      CLIPBackend, NoneBackend])
    def test_a_backend_that_cannot_read_a_page_says_so(self, cls):
        """None, so the caller reads the page from elsewhere -- never a scene
        label or a caption stored as the page's text."""
        assert cls().read_document(self._page(), 'read it') is None

    def test_the_caption_server_reads_a_page_at_a_readable_size(self):
        import integrations.vision.lightweight_backend as lvb
        from core.http_pool import LLM_COMPLETION_TIMEOUT
        backend = lvb.Qwen08BBackend(port=9555)
        with patch.object(backend, '_ensure_running', return_value=True), \
             patch.object(lvb, 'pooled_post', return_value=self._answer()) as post:
            assert backend.read_document(self._page(), 'read every line') == 'page text'
        # The caption server's own port, the one describe() uses.
        assert post.call_args.args[0] == 'http://127.0.0.1:9555/v1/chat/completions'
        body = post.call_args.kwargs['json']
        assert body['chat_template_kwargs'] == {'enable_thinking': False}
        assert body['max_tokens'] == lvb.PAGE_MAX_TOKENS
        assert post.call_args.kwargs['timeout'] == LLM_COMPLETION_TIMEOUT
        assert body['messages'][0]['content'][1] == {'type': 'text', 'text': 'read every line'}
        # Not the caption shrink (512x288): the long side at PAGE_LONG_SIDE.
        assert self._sent_size(post) == (989, lvb.PAGE_LONG_SIDE)

    def test_a_small_page_is_not_enlarged(self):
        import integrations.vision.lightweight_backend as lvb
        backend = lvb.Qwen08BBackend(port=9555)
        with patch.object(backend, '_ensure_running', return_value=True), \
             patch.object(lvb, 'pooled_post', return_value=self._answer()) as post:
            backend.read_document(self._page((600, 800)), 'x')
        assert self._sent_size(post) == (600, 800)

    def test_no_caption_server_means_no_request(self):
        import integrations.vision.lightweight_backend as lvb
        backend = lvb.Qwen08BBackend(port=9555)
        with patch.object(backend, '_ensure_running', return_value=False), \
             patch.object(lvb, 'pooled_post') as post:
            assert backend.read_document(self._page(), 'x') is None
        post.assert_not_called()

    @pytest.mark.parametrize('answer,expected', [
        (dict(content=''), ''),                          # answered with nothing
        (dict(content='cut off', finish='length'), 'cut off'),
        (dict(status=500), None),
    ])
    def test_what_the_server_answered_is_what_comes_back(self, answer, expected):
        import integrations.vision.lightweight_backend as lvb
        backend = lvb.Qwen08BBackend(port=9555)
        with patch.object(backend, '_ensure_running', return_value=True), \
             patch.object(lvb, 'pooled_post', return_value=self._answer(**answer)):
            assert backend.read_document(self._page(), 'x') == expected

    def test_an_unreachable_server_is_none_not_an_exception(self):
        import requests
        import integrations.vision.lightweight_backend as lvb
        backend = lvb.Qwen08BBackend(port=9555)
        with patch.object(backend, '_ensure_running', return_value=True), \
             patch.object(lvb, 'pooled_post', side_effect=requests.ConnectionError('refused')):
            assert backend.read_document(self._page(), 'x') is None

    def test_qwen3vl_reads_a_page_through_its_own_endpoint(self):
        import integrations.vision.lightweight_backend as lvb
        backend = lvb.Qwen3VLVisionBackend()
        backend._backend = MagicMock(base_url='http://10.0.0.5:7000/v1/',
                                     model_name='qwen-vl', api_key='k')
        with patch.object(lvb, 'pooled_post', return_value=self._answer('p')) as post:
            assert backend.read_document(self._page(), 'x') == 'p'
        assert post.call_args.args[0] == 'http://10.0.0.5:7000/v1/chat/completions'
        assert post.call_args.kwargs['headers'] == {'Authorization': 'Bearer k'}
        assert post.call_args.kwargs['json']['model'] == 'qwen-vl'
        assert post.call_args.kwargs['json']['chat_template_kwargs'] == {'enable_thinking': False}


class TestDocumentReaders:
    """get_document_readers(): the vision backend first, then the node's own
    main model -- where book pages were read before -- so no node reads
    fewer pages than it did."""

    MAIN = 'http://127.0.0.1:8123/v1'

    def _readers(self, node, main=MAIN):
        import integrations.vision.lightweight_backend as lvb
        with patch.object(lvb, 'get_vision_backend', return_value=node), \
             patch('core.port_registry.get_local_llm_url', return_value=main):
            return lvb.get_document_readers()

    @staticmethod
    def _posted_to(read):
        import integrations.vision.lightweight_backend as lvb
        resp = TestReadDocument._answer('p')
        with patch.object(lvb, 'pooled_post', return_value=resp) as post:
            assert read(TestReadDocument._page(), 'x') == 'p'
        return post.call_args

    def test_the_vision_backend_then_the_nodes_own_main_model(self):
        import integrations.vision.lightweight_backend as lvb
        node = lvb.Qwen08BBackend(port=9555)
        readers = self._readers(node)
        assert len(readers) == 2 and readers[0] == node.read_document
        call = self._posted_to(readers[1])
        assert call.args[0] == f'{self.MAIN}/chat/completions'
        assert 'headers' not in call.kwargs              # no remote endpoint's key
        assert call.kwargs['json']['chat_template_kwargs'] == {'enable_thinking': False}

    @pytest.mark.parametrize("cls", [MiniCPMBackend, MobileVLMBackend,
                                      CLIPBackend, NoneBackend])
    def test_a_backend_that_cannot_read_pages_leaves_the_main_model(self, cls):
        readers = self._readers(cls())
        assert len(readers) == 1
        assert self._posted_to(readers[0]).args[0] == f'{self.MAIN}/chat/completions'

    def test_a_vlm_endpoint_that_is_the_main_model_is_not_asked_twice(self):
        import integrations.vision.lightweight_backend as lvb
        node = lvb.Qwen3VLVisionBackend()
        node._backend = MagicMock(base_url=f'{self.MAIN}/', model_name='m', api_key='k')
        assert self._readers(node) == [node.read_document]

    def test_a_remote_vlm_endpoint_still_falls_back_to_this_nodes_model(self):
        import integrations.vision.lightweight_backend as lvb
        node = lvb.Qwen3VLVisionBackend()
        node._backend = MagicMock(base_url='http://10.0.0.5:7000/v1', model_name='m',
                                  api_key='k')
        readers = self._readers(node)
        assert len(readers) == 2
        assert self._posted_to(readers[1]).args[0] == f'{self.MAIN}/chat/completions'

    def test_no_main_model_address_means_no_fallback(self):
        import integrations.vision.lightweight_backend as lvb
        node = lvb.Qwen08BBackend(port=9555)
        with patch.object(lvb, 'get_vision_backend', return_value=node), \
             patch('core.port_registry.get_local_llm_url', side_effect=RuntimeError('none')):
            assert lvb.get_document_readers() == [node.read_document]


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


class TestVisionServiceBackendIntegration:
    """Verify VisionService uses lightweight backends when MiniCPM unavailable."""

    def test_detect_mode_embedded(self):
        """EMBEDDED tier → headless mode."""
        from integrations.vision.vision_service import VisionService
        svc = VisionService.__new__(VisionService)
        mock_caps = MagicMock()
        mock_caps.tier_name = 'embedded'
        with patch('security.system_requirements.get_capabilities',
                   return_value=mock_caps):
            assert svc._detect_mode() == 'headless'

    def test_detect_mode_lite(self):
        """LITE tier → lite mode."""
        from integrations.vision.vision_service import VisionService
        svc = VisionService.__new__(VisionService)
        mock_caps = MagicMock()
        mock_caps.tier_name = 'lite'
        with patch('security.system_requirements.get_capabilities',
                   return_value=mock_caps):
            assert svc._detect_mode() == 'lite'

    def test_detect_mode_standard(self):
        """STANDARD tier → full mode."""
        from integrations.vision.vision_service import VisionService
        svc = VisionService.__new__(VisionService)
        mock_caps = MagicMock()
        mock_caps.tier_name = 'standard'
        with patch('security.system_requirements.get_capabilities',
                   return_value=mock_caps):
            assert svc._detect_mode() == 'full'

    def test_detect_mode_fallback(self):
        """If detection fails → full mode."""
        from integrations.vision.vision_service import VisionService
        svc = VisionService.__new__(VisionService)
        with patch('security.system_requirements.get_capabilities',
                   side_effect=Exception("no caps")):
            assert svc._detect_mode() == 'full'

    def test_headless_mode_starts_no_threads(self):
        """Headless mode only initializes FrameStore, no threads."""
        from integrations.vision.vision_service import VisionService
        from integrations.vision.frame_store import FrameStore
        svc = VisionService.__new__(VisionService)
        svc._running = False
        svc._ws_thread = None
        svc._desc_thread = None
        svc._vision_backend = None
        svc.store = FrameStore()
        svc.start(mode='headless')
        assert svc._running is True
        assert svc._ws_thread is None
        assert svc._desc_thread is None

    def test_describe_frame_uses_lightweight_backend(self):
        """When _vision_backend is set, _describe_frame() uses it — and
        FORWARDS the prompt. Every backend's describe() signature is
        (frame_bytes, prompt); the old assertion pinned the promptless call,
        so the service growing prompt forwarding (the caller's intent actually
        reaching the model) read as a failure."""
        from integrations.vision.vision_service import VisionService
        svc = VisionService.__new__(VisionService)
        svc._circuit_open = False
        mock_backend = MagicMock()
        mock_backend.describe.return_value = 'A robot arm moving'
        svc._vision_backend = mock_backend
        result = svc._describe_frame('user1', b'fake_jpeg')
        assert result == 'A robot arm moving'
        mock_backend.describe.assert_called_once()
        args, _ = mock_backend.describe.call_args
        assert args[0] == b'fake_jpeg'
        assert len(args) == 2 and isinstance(args[1], str) and args[1], (
            'describe() must receive the prompt — a promptless call silently '
            'falls back to the backend default and drops the caller\'s intent')

    def test_describe_frame_forwards_a_caller_prompt_verbatim(self):
        """An explicit prompt from the caller must reach the backend untouched."""
        from integrations.vision.vision_service import VisionService
        svc = VisionService.__new__(VisionService)
        svc._circuit_open = False
        mock_backend = MagicMock()
        mock_backend.describe.return_value = 'ok'
        svc._vision_backend = mock_backend
        svc._describe_frame('user1', b'fake_jpeg', prompt='count the chairs')
        mock_backend.describe.assert_called_once_with(b'fake_jpeg',
                                                      'count the chairs')

    def test_describe_frame_minicpm_path(self):
        """When _vision_backend is None, uses MiniCPM HTTP."""
        from integrations.vision.vision_service import VisionService
        svc = VisionService.__new__(VisionService)
        svc._circuit_open = False
        svc._vision_backend = None
        svc._minicpm_port = 9891
        svc._consecutive_failures = 0
        svc._max_failures = 5
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'result': 'User typing on keyboard'}
        with patch('integrations.vision.vision_service.pooled_post', return_value=mock_resp):
            result = svc._describe_frame('user1', b'fake_jpeg')
            assert result == 'User typing on keyboard'

    def test_get_status_includes_backend(self):
        """get_status() includes active backend name."""
        from integrations.vision.vision_service import VisionService
        svc = VisionService.__new__(VisionService)
        svc._running = True
        svc._circuit_open = False
        svc._consecutive_failures = 0
        svc._frames_described = 10
        svc._frames_skipped = 5
        svc._minicpm_port = 9891
        svc._ws_port = 5460
        svc._trigger_manager = None
        svc._installer = MagicMock()
        svc._installer.get_status.return_value = {}
        svc.store = MagicMock()
        svc.store.stats.return_value = {}

        mock_backend = MagicMock()
        mock_backend.name = 'mobilevlm'
        svc._vision_backend = mock_backend
        status = svc.get_status()
        assert status['backend'] == 'mobilevlm'

    def test_stop_cleans_up_backend(self):
        """stop() calls backend.stop() and clears it."""
        from integrations.vision.vision_service import VisionService
        svc = VisionService.__new__(VisionService)
        svc._running = True
        svc._minicpm_process = None
        svc._frames_described = 0
        svc._frames_skipped = 0
        mock_backend = MagicMock()
        mock_backend.name = 'clip'
        svc._vision_backend = mock_backend
        svc.stop()
        mock_backend.stop.assert_called_once()
        assert svc._vision_backend is None
        assert svc._running is False


class TestModelRegistryVisionLite:
    """Verify mobilevlm-1.7b-onnx registration in ModelRegistry."""

    def test_mobilevlm_registered_when_enabled(self):
        """HEVOLVE_VISION_LITE_ENABLED=true → mobilevlm in registry."""
        with patch.dict(os.environ, {'HEVOLVE_VISION_LITE_ENABLED': 'true'}):
            from integrations.agent_engine.model_registry import (
                ModelRegistry, ModelBackend, ModelTier,
            )
            reg = ModelRegistry()
            reg.register(ModelBackend(
                model_id='mobilevlm-1.7b-onnx',
                display_name='MobileVLM 1.7B (ONNX CPU)',
                tier=ModelTier.FAST,
                config_list_entry={'model': 'mobilevlm-1.7b', 'api_key': 'local',
                                   'base_url': 'local://onnxruntime', 'price': [0, 0]},
                avg_latency_ms=500.0, accuracy_score=0.45,
                cost_per_1k_tokens=0.0, is_local=True,
                hardware_dependent=True, gpu_tdp_watts=0.0,
            ))
            model = reg.get_model('mobilevlm-1.7b-onnx')
            assert model is not None
            assert model.tier == ModelTier.FAST
            assert model.is_local is True
            assert model.gpu_tdp_watts == 0.0

    def test_mobilevlm_is_fast_tier(self):
        from integrations.agent_engine.model_registry import ModelBackend, ModelTier
        mb = ModelBackend(
            model_id='mobilevlm-1.7b-onnx',
            display_name='MobileVLM', tier=ModelTier.FAST,
            config_list_entry={}, gpu_tdp_watts=0.0,
        )
        assert mb.tier == ModelTier.FAST


# ── #102: a failed launch must not kill captioning for good ───────────

def test_a_failed_launch_is_retried_after_the_cooldown():
    """Found by hartos-94, read from the path and fixed here.

    _launch_attempted was cleared in exactly ONE place -- the tail of
    stop() -- and check_idle only reaches stop() through
    `if self._server_proc`, which is None after a FAILED launch. So one
    failure set the flag forever and captioning was dead for the life of
    the process, on a backend whose whole design is a lazy per-frame
    start. Transient failure is the normal case: the event wait is 5x1s,
    the standalone path needs a llama-server binary that aborts on this
    box, and it competes for VRAM with the resident LLM.
    """
    from integrations.vision.lightweight_backend import Qwen08BBackend

    backend = Qwen08BBackend(port=59999)
    backend.is_available = lambda: False

    # An ATTEMPT is what moves the timestamp. is_available() is consulted
    # on every call before the cooldown -- correctly, it is the cheap "is
    # it already up" check -- so counting it proves nothing about whether
    # a launch was tried.
    assert backend._ensure_running() is False
    first_attempt = backend._launch_attempted_at
    assert first_attempt > 0, 'never even tried'

    # immediately after: suppressed by the cooldown, as intended
    assert backend._ensure_running() is False
    assert backend._launch_attempted_at == first_attempt, (
        'hammered the launch instead of waiting out the cooldown')

    # once the cooldown has passed, it tries AGAIN rather than latching off
    backend._launch_attempted_at -= (backend.LAUNCH_RETRY_S + 1)
    stale = backend._launch_attempted_at
    assert backend._ensure_running() is False
    assert backend._launch_attempted_at > stale, (
        'captioning stayed dead after one failed launch (#102)')


def test_the_cooldown_is_not_a_permanent_latch():
    """A guard on the mechanism itself, since the defect was its permanence."""
    from integrations.vision.lightweight_backend import Qwen08BBackend

    assert isinstance(Qwen08BBackend.LAUNCH_RETRY_S, (int, float))
    assert Qwen08BBackend.LAUNCH_RETRY_S > 0
    backend = Qwen08BBackend(port=59998)
    assert hasattr(backend, '_launch_attempted_at'), (
        'without a timestamp the flag can only be permanent')
