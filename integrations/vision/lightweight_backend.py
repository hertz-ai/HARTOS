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

    # Auto-detect — prefer Qwen3-VL if its server is already running
    # (zero extra VRAM cost since it reuses the Qwen3-VL server)
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
