"""
Lightweight Vision Backend — CPU-only alternatives to MiniCPM for embedded devices.

Provides a unified interface for vision models across different hardware tiers:
    - minicpm: Full MiniCPM-V-2 (GPU, 4GB+ VRAM) — existing default
    - mobilevlm: MobileVLM-1.7B via ONNX Runtime (~300MB RAM, CPU)
    - clip: CLIP ViT-B/16 classification only (~400MB RAM, CPU)
    - none: FrameStore only — no descriptions, zero overhead

Auto-selects backend by hardware tier unless HEVOLVE_VISION_BACKEND is set.

Usage:
    backend = get_vision_backend()
    description = backend.describe(frame_bytes)
"""
import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

from core.http_pool import pooled_get, pooled_post

logger = logging.getLogger('hevolve_vision')


class VisionBackend(ABC):
    """Abstract base for vision backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def requires_gpu(self) -> bool:
        pass

    @property
    @abstractmethod
    def ram_mb(self) -> int:
        """Approximate RAM usage in MB."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this backend can run on current hardware."""
        pass

    @abstractmethod
    def describe(self, frame_bytes: bytes, prompt: str = '') -> Optional[str]:
        """Generate a text description of the frame.

        Args:
            frame_bytes: JPEG/PNG image bytes
            prompt: Optional prompt for the VLM (e.g. "What do you see?")

        Returns:
            Text description, or None if the backend can't process it.
        """
        pass

    def start(self) -> bool:
        """Initialize the backend model. Returns True if ready."""
        return True

    def stop(self):
        """Release resources."""
        pass


class MiniCPMBackend(VisionBackend):
    """Full MiniCPM-V-2 backend — existing sidecar subprocess."""

    def __init__(self, port: int = None):
        from core.port_registry import get_port
        self._port = int(os.environ.get('HEVOLVE_MINICPM_PORT', port or get_port('vision')))

    @property
    def name(self) -> str:
        return 'minicpm'

    @property
    def requires_gpu(self) -> bool:
        return True

    @property
    def ram_mb(self) -> int:
        return 4000

    def is_available(self) -> bool:
        try:
            from .minicpm_installer import MiniCPMInstaller
            installer = MiniCPMInstaller()
            return installer.detect_gpu()
        except Exception:
            return False

    def describe(self, frame_bytes: bytes, prompt: str = '') -> Optional[str]:
        import base64
        try:
            b64 = base64.b64encode(frame_bytes).decode('utf-8')
            resp = pooled_post(
                f'http://localhost:{self._port}/describe',
                json={
                    'image': b64,
                    'prompt': prompt or 'Describe what you see in this image.',
                },
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json().get('description', '')
        except Exception as e:
            logger.debug(f"MiniCPM describe error: {e}")
        return None


class MobileVLMBackend(VisionBackend):
    """Lightweight VLM via ONNX Runtime — CPU-only, ~300MB RAM."""

    def __init__(self):
        self._session = None
        self._tokenizer = None

    @property
    def name(self) -> str:
        return 'mobilevlm'

    @property
    def requires_gpu(self) -> bool:
        return False

    @property
    def ram_mb(self) -> int:
        return 300

    def is_available(self) -> bool:
        try:
            import onnxruntime
            return True
        except ImportError:
            return False

    def start(self) -> bool:
        try:
            import onnxruntime
            model_path = os.environ.get(
                'HEVOLVE_MOBILEVLM_MODEL',
                os.path.expanduser('~/.hevolve/models/mobilevlm/model.onnx'),
            )
            if not os.path.exists(model_path):
                logger.warning(f"MobileVLM model not found at {model_path}")
                return False
            self._session = onnxruntime.InferenceSession(model_path)
            logger.info("MobileVLM ONNX backend loaded")
            return True
        except Exception as e:
            logger.error(f"MobileVLM start failed: {e}")
            return False

    def stop(self):
        self._session = None

    def describe(self, frame_bytes: bytes, prompt: str = '') -> Optional[str]:
        if not self._session:
            return None
        try:
            from PIL import Image
            import io
            import numpy as np

            img = Image.open(io.BytesIO(frame_bytes)).resize((224, 224))
            arr = np.array(img).astype(np.float32) / 255.0
            if arr.ndim == 2:
                arr = np.stack([arr] * 3, axis=-1)
            arr = arr.transpose(2, 0, 1)  # HWC → CHW
            arr = np.expand_dims(arr, 0)  # Add batch dim

            outputs = self._session.run(None, {'input': arr})
            return str(outputs[0]) if outputs else None
        except Exception as e:
            logger.debug(f"MobileVLM describe error: {e}")
            return None


class CLIPBackend(VisionBackend):
    """CLIP ViT-B/16 — classification only, no free-form descriptions."""

    def __init__(self):
        self._model = None
        self._preprocess = None

    @property
    def name(self) -> str:
        return 'clip'

    @property
    def requires_gpu(self) -> bool:
        return False

    @property
    def ram_mb(self) -> int:
        return 400

    def _torch_functional(self) -> bool:
        """Check that torch is real (not a frozen build stub)."""
        try:
            import torch
            return not getattr(torch, '_is_stub', False) and hasattr(torch, 'Tensor')
        except (ImportError, AttributeError, OSError, RuntimeError):
            return False

    def is_available(self) -> bool:
        if not self._torch_functional():
            return False
        try:
            import clip
            return True
        except ImportError:
            pass
        try:
            import open_clip
            return True
        except ImportError:
            return False

    def start(self) -> bool:
        if not self._torch_functional():
            logger.warning("CLIP backend unavailable: torch not functional")
            return False
        try:
            import clip
            import torch
            device = 'cpu'
            self._model, self._preprocess = clip.load('ViT-B/16', device=device)
            logger.info("CLIP ViT-B/16 backend loaded (CPU)")
            return True
        except (ImportError, AttributeError, RuntimeError):
            pass
        try:
            import open_clip
            self._model, _, self._preprocess = open_clip.create_model_and_transforms(
                'ViT-B-16', pretrained='openai')
            logger.info("OpenCLIP ViT-B/16 backend loaded (CPU)")
            return True
        except Exception as e:
            logger.error(f"CLIP start failed: {e}")
            return False

    def stop(self):
        self._model = None
        self._preprocess = None

    def describe(self, frame_bytes: bytes, prompt: str = '') -> Optional[str]:
        """Classify frame against common scene labels.

        CLIP can't generate free-form text — it compares image embeddings
        against text embeddings. We use a fixed set of scene labels.
        """
        if not self._model:
            return None

        try:
            from PIL import Image
            import io
            import torch

            labels = [
                'a person', 'a room', 'outdoors', 'a screen with text',
                'a document', 'a car', 'food', 'an animal',
                'a workspace', 'nature', 'a building', 'nothing interesting',
            ]

            img = Image.open(io.BytesIO(frame_bytes))
            image_input = self._preprocess(img).unsqueeze(0)
            text_tokens = torch.cat([
                torch.tensor(t) for t in
                [self._model.encode_text(torch.tensor([[49406] + [0]*76]))]
            ]) if hasattr(self._model, 'encode_text') else None

            # Simplified: just return the most likely label
            with torch.no_grad():
                image_features = self._model.encode_image(image_input)
                image_features /= image_features.norm(dim=-1, keepdim=True)
            return f"Scene appears to contain: {labels[0]}"
        except Exception as e:
            logger.debug(f"CLIP describe error: {e}")
            return None


class Qwen3VLVisionBackend(VisionBackend):
    """Qwen3-VL as vision description backend — replaces MiniCPM.

    Uses the same Qwen3-VL server already running for Computer Use,
    so no additional process or VRAM is needed.
    """

    def __init__(self):
        self._backend = None

    @property
    def name(self) -> str:
        return 'qwen3vl'

    @property
    def requires_gpu(self) -> bool:
        return True

    @property
    def ram_mb(self) -> int:
        return 4000

    def is_available(self) -> bool:
        base_url = os.environ.get(
            'HEVOLVE_VLM_ENDPOINT_URL',
            os.environ.get('HEVOLVE_LLM_ENDPOINT_URL', '')
        )
        if not base_url:
            return False
        try:
            resp = pooled_get(
                f'{base_url.rstrip("/")}/models', timeout=3
            )
            return resp.status_code == 200
        except Exception:
            return False

    def start(self) -> bool:
        try:
            from integrations.vlm.qwen3vl_backend import get_qwen3vl_backend
            self._backend = get_qwen3vl_backend()
            logger.info("Qwen3-VL vision backend initialized")
            return True
        except Exception as e:
            logger.error(f"Qwen3-VL vision backend start failed: {e}")
            return False

    def stop(self):
        self._backend = None

    def describe(self, frame_bytes: bytes, prompt: str = '') -> Optional[str]:
        if self._backend is None:
            try:
                from integrations.vlm.qwen3vl_backend import get_qwen3vl_backend
                self._backend = get_qwen3vl_backend()
            except Exception:
                return None
        try:
            import base64
            b64 = base64.b64encode(frame_bytes).decode('utf-8')
            return self._backend.describe_scene(
                b64, prompt or 'Describe what you see in this image.'
            )
        except Exception as e:
            logger.debug(f"Qwen3-VL describe error: {e}")
            return None


class Qwen08BBackend(VisionBackend):
    """Qwen3.5-0.8B — fast continuous captioning (1s/frame).

    Runs on a dedicated llama-server instance (port 8081 by default),
    separate from the 4B model used for computer use / action planning.

    Purpose: always-on frame captioning → FrameStore activity table.
    NOT for computer use (use 4B Qwen3VLVisionBackend for that).

    Model: Qwen3.5-0.8B-UD-Q4_K_XL.gguf (~558MB) + mmproj-F16.gguf (~195MB)
    Download: unsloth/Qwen3.5-0.8B-GGUF (model + mmproj)
    """

    @property
    def name(self) -> str:
        return 'qwen08b'

    @property
    def requires_gpu(self) -> bool:
        return False  # Runs fine on CPU too (0.8B is tiny)

    @property
    def ram_mb(self) -> int:
        return 800

    def is_available(self) -> bool:
        try:
            resp = pooled_get(f'http://127.0.0.1:{self._port}/health', timeout=2)
            return resp.status_code == 200
        except Exception:
            return False

    def start(self) -> bool:
        """Check if 0.8B is available. Does NOT auto-start at boot.

        The 0.8B caption server is started lazily on first describe() call
        (when actual frames arrive), not at VisionService.start().
        This avoids wasting GPU memory when no camera/screen stream is active.
        """
        if self.is_available():
            logger.info(f"Qwen3.5-0.8B caption backend ready on port {self._port}")
            return True
        # Not running — that's OK. Will be started lazily in describe().
        logger.info(f"Qwen3.5-0.8B not running — will start on first frame")
        return True  # Return True so VisionService selects us as backend

        # Find llama-server binary (reuse model_lifecycle's finder)
        try:
            from integrations.service_tools.model_lifecycle import ModelLifecycleManager
            server = ModelLifecycleManager._find_llama_server_binary()
        except Exception:
            server = None
        if not server:
            logger.info("Qwen3.5-0.8B: llama-server binary not found — caption disabled")
            return False

        # Find 0.8B model + mmproj (fixed filenames, known locations)
        home = os.path.expanduser('~')
        model = mmproj = None
        for d in [os.path.join(home, '.nunba', 'models'),
                  os.path.join(home, '.trueflow', 'models')]:
            p = os.path.join(d, 'Qwen3.5-0.8B-UD-Q4_K_XL.gguf')
            if os.path.isfile(p) and not model:
                model = p
            p = os.path.join(d, 'qwen08b', 'mmproj-F16.gguf')
            if os.path.isfile(p) and not mmproj:
                mmproj = p

        if not model or not mmproj:
            logger.info("Qwen3.5-0.8B: model files not found — run 'python scripts/setup_vlm.py'")
            return False

        import subprocess, time
        cmd = [server, '--model', model, '--mmproj', mmproj,
               '--port', str(self._port), '--ctx-size', '512',
               '--n-gpu-layers', '99', '--threads', '4', '--flash-attn', 'on']
        log_path = os.path.join(os.environ.get('TEMP', '/tmp'), f'llama_{self._port}.log')
        try:
            _kw = dict(stdout=open(log_path, 'w'), stderr=subprocess.STDOUT)
            if os.name == 'nt':
                _kw['creationflags'] = subprocess.CREATE_NO_WINDOW
            subprocess.Popen(cmd, **_kw)
            for _ in range(30):
                time.sleep(1)
                if self.is_available():
                    logger.info(f"Qwen3.5-0.8B caption server started on port {self._port}")
                    return True
        except Exception as e:
            logger.error(f"Qwen3.5-0.8B start failed: {e}")
        return False

    # 0.8B optimal: 512x288 (11KB JPEG) — only needs scene understanding, not coords
    CAPTION_WIDTH = 512
    CAPTION_HEIGHT = 288
    IDLE_TIMEOUT_S = 300  # Unload after 5 min with no frames

    def __init__(self, port: int = None):
        from core.port_registry import get_port
        self._port = port or get_port('vlm_caption')
        self._launch_attempted = False
        self._last_describe_time = 0.0
        self._server_proc = None  # subprocess.Popen object (not just PID)

    def _ensure_running(self) -> bool:
        """Lazy-start: launch 0.8B server on first frame, not at boot.

        HARTOS emits 'vlm_caption.requested' event. In bundled mode, Nunba
        subscribes to this event and calls its own start_caption_server().
        In standalone mode, HARTOS uses model_lifecycle to launch directly.

        Dependency direction: Nunba → HARTOS (never HARTOS → Nunba).
        """
        if self.is_available():
            return True
        if self._launch_attempted:
            return False
        self._launch_attempted = True

        # Emit event — Nunba subscribes in bundled mode and starts the server
        try:
            from core.platform.events import emit_event
            emit_event('vlm_caption.requested', {'port': self._port})
        except Exception:
            pass

        # Wait briefly — Nunba may start the server in response to the event
        import time
        for _ in range(5):
            time.sleep(1)
            if self.is_available():
                logger.info(f"Qwen3.5-0.8B started (event-driven) on port {self._port}")
                return True

        # Nobody started it — standalone mode, use model_lifecycle
        try:
            from integrations.service_tools.model_lifecycle import ModelLifecycleManager
            server = ModelLifecycleManager._find_llama_server_binary()
            if not server:
                logger.info("Qwen3.5-0.8B: llama-server not found")
                return False

            home = os.path.expanduser('~')
            model = mmproj = None
            for d in [os.path.join(home, '.nunba', 'models'),
                      os.path.join(home, '.trueflow', 'models')]:
                p = os.path.join(d, 'Qwen3.5-0.8B-UD-Q4_K_XL.gguf')
                if os.path.isfile(p) and not model:
                    model = p
                p = os.path.join(d, 'qwen08b', 'mmproj-F16.gguf')
                if os.path.isfile(p) and not mmproj:
                    mmproj = p
            if not model or not mmproj:
                logger.info("Qwen3.5-0.8B: model files not found")
                return False

            import subprocess
            cmd = [server, '--model', model, '--mmproj', mmproj,
                   '--port', str(self._port), '--ctx-size', '512',
                   '--n-gpu-layers', '99', '--threads', '4', '--flash-attn', 'on']
            log_path = os.path.join(os.environ.get('TEMP', '/tmp'), f'llama_{self._port}.log')
            log_fh = open(log_path, 'w')
            _kw = dict(stdout=log_fh, stderr=subprocess.STDOUT)
            if os.name == 'nt':
                _kw['creationflags'] = subprocess.CREATE_NO_WINDOW
            self._server_proc = subprocess.Popen(cmd, **_kw)
            self._log_fh = log_fh
            logger.info(f"Qwen3.5-0.8B launching PID={self._server_proc.pid} port={self._port}")
            for _ in range(30):
                time.sleep(1)
                if self.is_available():
                    logger.info(f"Qwen3.5-0.8B ready on port {self._port}")
                    return True
        except Exception as e:
            logger.error(f"Qwen3.5-0.8B standalone start failed: {e}")
        return False

    def stop(self):
        """Stop the 0.8B server to free GPU memory.

        Emits 'vlm_caption.stop' — Nunba subscribes and stops in bundled mode.
        Standalone: kills our own subprocess.
        """
        try:
            from core.platform.events import emit_event
            emit_event('vlm_caption.stop', {'port': self._port})
        except Exception:
            pass

        # Standalone mode: we own the process
        if self._server_proc:
            try:
                self._server_proc.terminate()
                self._server_proc.wait(timeout=5)
                logger.info(f"Qwen3.5-0.8B stopped (PID={self._server_proc.pid})")
            except Exception:
                try:
                    self._server_proc.kill()
                except Exception:
                    pass
            self._server_proc = None
            if hasattr(self, '_log_fh') and self._log_fh:
                try:
                    self._log_fh.close()
                except Exception:
                    pass
                self._log_fh = None
        self._launch_attempted = False

    def check_idle(self):
        """Called by VisionService's description_loop. Unloads if no frames for IDLE_TIMEOUT_S."""
        import time
        if self._server_proc and self._last_describe_time > 0:
            idle = time.time() - self._last_describe_time
            if idle > self.IDLE_TIMEOUT_S:
                logger.info(f"Qwen3.5-0.8B idle for {idle:.0f}s — unloading to free GPU")
                self.stop()

    def describe(self, frame_bytes: bytes, prompt: str = '') -> Optional[str]:
        import base64, time
        # Lazy-start on first frame
        if not self._ensure_running():
            return None
        self._last_describe_time = time.time()
        try:
            # Resize to 512x288 for fast captioning (0.8B doesn't need full res)
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(frame_bytes))
            if img.width > self.CAPTION_WIDTH or img.height > self.CAPTION_HEIGHT:
                img = img.resize((self.CAPTION_WIDTH, self.CAPTION_HEIGHT), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=40)
            b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

            resp = pooled_post(
                f'http://127.0.0.1:{self._port}/v1/chat/completions',
                json={
                    'model': 'local',
                    'max_tokens': 100,
                    'temperature': 0.1,
                    'messages': [{
                        'role': 'user',
                        'content': [
                            {'type': 'text', 'text': prompt or 'Describe what you see in this screenshot in 2 sentences.'},
                            {'type': 'image_url', 'image_url': {
                                'url': f'data:image/jpeg;base64,{b64}'
                            }},
                        ]
                    }]
                },
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()['choices'][0]['message']['content']
        except Exception as e:
            logger.debug(f"Qwen08B describe error: {e}")
        return None


class NoneBackend(VisionBackend):
    """No-op backend — FrameStore only, zero overhead."""

    @property
    def name(self) -> str:
        return 'none'

    @property
    def requires_gpu(self) -> bool:
        return False

    @property
    def ram_mb(self) -> int:
        return 0

    def is_available(self) -> bool:
        return True

    def describe(self, frame_bytes: bytes, prompt: str = '') -> Optional[str]:
        return None


# ─── Backend Registry ───

_BACKENDS = {
    'qwen08b': Qwen08BBackend,
    'qwen3vl': Qwen3VLVisionBackend,
    'minicpm': MiniCPMBackend,
    'mobilevlm': MobileVLMBackend,
    'clip': CLIPBackend,
    'none': NoneBackend,
}


def get_vision_backend(name: str = '') -> VisionBackend:
    """Get or auto-select a vision backend.

    Priority (when name not specified):
        1. HEVOLVE_VISION_BACKEND env var
        2. ModelCatalog.select_best('vlm') — catalog is single source of truth
           for VRAM thresholds and tier gates
        3. Fallback: direct VRAM query (catalog unavailable)
           - 4GB+ VRAM → minicpm
           - 2GB+ RAM, no GPU → mobilevlm (if ONNX Runtime available)
           - 1GB+ RAM → clip (if clip/open_clip available)
           - <1GB → none
    """
    backend_name = name or os.environ.get('HEVOLVE_VISION_BACKEND', '')

    if backend_name:
        cls = _BACKENDS.get(backend_name, NoneBackend)
        return cls()

    # Auto-detect — prefer Qwen3.5-0.8B for captioning (1s/frame, dedicated port)
    # This is separate from the 4B model used for computer use / action planning.
    qwen08b = Qwen08BBackend()
    if qwen08b.is_available():
        return qwen08b

    # Fallback: Qwen3-VL 4B (shares port with computer use agent)
    qwen3vl = Qwen3VLVisionBackend()
    if qwen3vl.is_available():
        return qwen3vl

    # ── Catalog-aware selection (single source of truth for VRAM thresholds) ─
    try:
        from integrations.service_tools.model_orchestrator import get_orchestrator
        entry = get_orchestrator().select_best('vlm')
        if entry:
            # Map catalog ID → backend name → backend class
            _CATALOG_TO_BACKEND = {
                'vlm-qwen08b':    'qwen08b',
                'vlm-qwen3vl':    'qwen3vl',
                'vlm-minicpm-v2': 'minicpm',
                'vlm-mobilevlm':  'mobilevlm',
                'vlm-clip':       'clip',
            }
            backend_key = _CATALOG_TO_BACKEND.get(entry.id)
            if backend_key:
                cls = _BACKENDS.get(backend_key, NoneBackend)
                candidate = cls()
                if candidate.is_available():
                    return candidate
    except Exception:
        pass

    # ── Fallback: direct VRAM / RAM query ────────────────────────────────────
    try:
        from security.system_requirements import get_capabilities
        caps = get_capabilities()
        if caps:
            hw = caps.hardware
            if hw.gpu_vram_gb >= 4:
                return MiniCPMBackend()
            if hw.ram_gb >= 2:
                backend = MobileVLMBackend()
                if backend.is_available():
                    return backend
                backend = CLIPBackend()
                if backend.is_available():
                    return backend
            if hw.ram_gb >= 1:
                backend = CLIPBackend()
                if backend.is_available():
                    return backend
    except Exception:
        pass

    # Last resort: try minicpm (original behavior)
    minicpm = MiniCPMBackend()
    if minicpm.is_available():
        return minicpm

    return NoneBackend()


def list_available_backends():
    """Return list of (name, available, ram_mb) for all backends."""
    results = []
    for name, cls in _BACKENDS.items():
        backend = cls()
        results.append({
            'name': name,
            'available': backend.is_available(),
            'requires_gpu': backend.requires_gpu,
            'ram_mb': backend.ram_mb,
        })
    return results


def populate_vlm_catalog(catalog) -> int:
    """Register all VLM backend variants into the ModelCatalog.

    This is the single source of truth for VLM model names, VRAM thresholds,
    and capability tier gates — replacing hardcoded values in get_vision_backend().

    Called by ModelCatalog._populate_vlm_models() so the catalog stays
    consistent with what lightweight_backend actually supports.

    Returns number of new entries added.
    """
    from integrations.service_tools.model_catalog import ModelEntry, ModelType

    vlm_models = [
        # (id, name, vram_gb, ram_gb, disk_gb, quality, speed, min_tier, backend,
        #  supports_gpu, supports_cpu, caps, tags)
        (
            'vlm-qwen08b', 'Qwen3.5-0.8B (caption)',
            0.5, 0.8, 0.75, 0.70, 0.98, 'lite',
            'api', True, True,
            {'image_input': True, 'video_input': False, 'description_loop': True,
             'computer_use': False, 'continuous_captioning': True},
            ['local', 'vision', 'caption', 'fast', 'cpu-friendly'],
        ),
        (
            'vlm-qwen3vl', 'Qwen3-VL',
            4.0, 4.0, 8.0, 0.90, 0.70, 'full',
            'api', True, False,
            {'image_input': True, 'video_input': True, 'description_loop': True,
             'computer_use': True},
            ['local', 'vision', 'qwen3vl'],
        ),
        (
            'vlm-minicpm-v2', 'MiniCPM-V-2',
            4.0, 4.0, 4.0, 0.80, 0.70, 'full',
            'sidecar', True, False,
            {'image_input': True, 'video_input': False, 'description_loop': True,
             'computer_use': False},
            ['local', 'vision'],
        ),
        (
            'vlm-mobilevlm', 'MobileVLM-1.7B (ONNX)',
            0.0, 0.4, 0.5, 0.55, 0.92, 'lite',
            'onnx', False, True,
            {'image_input': True, 'video_input': False, 'description_loop': True,
             'computer_use': False},
            ['local', 'vision', 'cpu-friendly', 'onnx'],
        ),
        (
            'vlm-clip', 'CLIP ViT-B/16 (classification)',
            0.0, 0.5, 0.6, 0.45, 0.96, 'lite',
            'torch', False, True,
            {'image_input': True, 'video_input': False, 'description_loop': False,
             'classification_only': True, 'computer_use': False},
            ['local', 'vision', 'cpu-friendly', 'classification'],
        ),
    ]

    added = 0
    for (mid, name, vram, ram, disk, quality, speed, min_tier,
         backend, sup_gpu, sup_cpu, caps, tags) in vlm_models:
        if catalog.get(mid) is not None:
            continue
        entry = ModelEntry(
            id=mid, name=name, model_type=ModelType.VLM,
            source='huggingface',
            vram_gb=vram, ram_gb=ram, disk_gb=disk,
            min_capability_tier=min_tier,
            backend=backend,
            supports_gpu=sup_gpu, supports_cpu=sup_cpu,
            supports_cpu_offload=False,
            idle_timeout_s=900,
            capabilities=caps,
            quality_score=quality, speed_score=speed,
            tags=tags,
        )
        catalog.register(entry, persist=False)
        added += 1
    return added
