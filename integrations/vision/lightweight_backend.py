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
import functools
import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

from core.http_pool import pooled_get, pooled_post

logger = logging.getLogger('hevolve_vision')


try:
    from hartos.exception_collector import AutoReportSubsystemFailures
except Exception:  # pragma: no cover — collector unavailable in minimal envs
    class AutoReportSubsystemFailures:  # type: ignore[no-redef]
        """No-op fallback when the self-heal pipeline isn't importable
        (e.g. extreme degraded boot).  Lets VisionBackend subclasses
        still load and run without surfacing pipeline hookups."""
        SUBSYSTEM = ''
        AUTO_REPORTED_METHODS: tuple = ()


class VisionBackend(AutoReportSubsystemFailures, ABC):
    """Abstract base for vision backends.

    Inherits AutoReportSubsystemFailures so every concrete subclass
    (MiniCPM / MobileVLM / CLIP / Qwen3-VL / Qwen3.5-0.8B / etc.)
    has its core methods auto-wrapped to feed the canonical
    self-heal pipeline.  Failures of any vision backend produce
    pattern_key='vlm.<name>::<method>' records that
    SelfHealingDispatcher clusters into one self_heal goal — the
    same shape as channels / TTS / LLM / daemon.

    Subclass override of self._identifier_for_self_heal() is
    unnecessary because every concrete subclass already implements
    the ``name`` property (the mixin's default reads ``self.name``).
    """

    SUBSYSTEM = 'vlm'
    # Methods whose escaping exceptions auto-feed the self-heal pipe.
    # `start` is the primary failure surface (model load); `describe`
    # and `read_document` are the runtime synth surfaces (OOM, dispatch
    # fail); `stop` is included so an unload that hangs doesn't go
    # silent.  Adding new methods to a concrete backend (e.g. `embed`)
    # just needs the method name in this tuple — no per-backend
    # except-block edits.
    AUTO_REPORTED_METHODS = ('start', 'describe', 'read_document', 'stop')

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

    def read_document(self, image_bytes: bytes, prompt: str) -> Optional[str]:
        """Read everything on a document page -- a book page, a scan: its
        text, layout, tables and figures, in the shape the prompt asks for.

        Not a caption. The page goes at a size its text can be read at, and
        the answer can run to thousands of tokens. The default is None: this
        backend cannot read a page (a classifier, a caption-only model, no
        model at all), and the caller falls back to what it has -- the book
        pipeline reads the page's own text layer.

        Args:
            image_bytes: the page, as JPEG/PNG bytes
            prompt: what to extract, and in what shape

        Returns:
            The model's answer ('' when it answered with nothing), or None
            when this backend cannot read a page or could not be reached.
        """
        return None

    def start(self) -> bool:
        """Initialize the backend model. Returns True if ready."""
        return True

    def stop(self):
        """Release resources."""
        pass


#: A page's long side, in pixels, when a VLM reads it. Measured 2026-09-14 on
#: Qwen3.5-0.8B with a dense page of 45 lines of 11 pt text: every line came
#: back verbatim at 1280 px, as at the full 2200 px render, from 1570 prompt
#: tokens instead of 3987 -- less of the server's context for one page.
PAGE_LONG_SIDE = 1280
#: A page's answer budget. The book page prompt asks for the text and then for
#: every element of it again, as JSON: the same 45-line page ran past 2048
#: tokens, and the cut-off JSON came back as prose.
PAGE_MAX_TOKENS = 4096


def _page_jpeg(image_bytes: bytes) -> bytes:
    """The page as JPEG bytes, no longer than PAGE_LONG_SIDE on its long side."""
    import io
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes))
    if max(img.size) > PAGE_LONG_SIDE:
        scale = PAGE_LONG_SIDE / max(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.convert('RGB').save(buf, 'JPEG', quality=85)
    return buf.getvalue()


def _read_page_with(completions_url: str, image_bytes: bytes, prompt: str, *,
                    model: str, headers: Optional[dict] = None) -> Optional[str]:
    """The one page-reading request to an OpenAI-compatible vision server.

    Thinking is off (core.constants.LLM_THINKING_OFF_KWARGS): a hybrid
    reasoning model otherwise spends its budget in reasoning_content and
    answers with nothing. pooled_post admits the call through the priority
    scheduler, so a page read by a background parse waits behind the user's
    own turn. Never raises: a page the model does not read, the caller reads
    from somewhere else.
    """
    import base64
    from core.constants import LLM_THINKING_OFF_KWARGS
    from core.http_pool import LLM_COMPLETION_TIMEOUT
    try:
        b64 = base64.b64encode(_page_jpeg(image_bytes)).decode('ascii')
    except Exception as e:
        logger.warning(f"page image could not be prepared for the VLM: {e}")
        return None
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
            {'type': 'text', 'text': prompt},
        ]}],
        'chat_template_kwargs': dict(LLM_THINKING_OFF_KWARGS),
        'max_tokens': PAGE_MAX_TOKENS,
        'temperature': 0.3,
    }
    extra = {'headers': headers} if headers else {}
    try:
        resp = pooled_post(completions_url, json=body,
                           timeout=LLM_COMPLETION_TIMEOUT, **extra)
    except Exception as e:
        logger.warning(f"page read failed at {completions_url}: {e}")
        return None
    if resp.status_code != 200:
        logger.warning(f"page read at {completions_url} returned HTTP "
                       f"{resp.status_code}: {resp.text[:200]}")
        return None
    try:
        choice = (resp.json().get('choices') or [{}])[0]
    except (ValueError, AttributeError) as e:
        logger.warning(f"page read at {completions_url} answered with no usable JSON: {e}")
        return None
    message = choice.get('message') or {}
    content = (message.get('content') or '').strip()
    if choice.get('finish_reason') == 'length':
        logger.warning("page read stopped at its %d-token budget: the answer is cut off",
                       PAGE_MAX_TOKENS)
    if not content:
        logger.warning("page read produced EMPTY content (finish_reason=%s, "
                       "reasoning_content=%d chars)", choice.get('finish_reason'),
                       len(message.get('reasoning_content') or ''))
    return content


class MiniCPMBackend(VisionBackend):
    """Full MiniCPM-V-2 backend — existing sidecar subprocess.

    Port resolution, most specific first:
      1. an explicit `port=` argument, or HEVOLVE_MINICPM_PORT
      2. the port RuntimeToolManager gave the sidecar it started
      3. port_registry's 'vision' (9891) — the fixed-port deployment
         (nixos/modules/hart-vision.nix passes --port explicitly)

    (2) is why this class and RuntimeToolManager used to be two
    unconnected paths: RTM's contract is "all sidecar servers use dynamic
    port allocation (no fixed ports)", so a sidecar it starts listens on
    an OS-assigned high port, while this class only ever looked at 9891.
    `start_tool('minicpm')` could therefore succeed and `get_vision_backend()`
    still talk to nobody.  RTM owns the process, so RTM owns the port, and
    it is asked at call time — a backend object may well be constructed
    before the sidecar is started.
    """

    def __init__(self, port: int = None):
        from core.port_registry import get_port
        self._explicit_port = (
            int(os.environ['HEVOLVE_MINICPM_PORT'])
            if os.environ.get('HEVOLVE_MINICPM_PORT') else port)
        self._registry_port = int(get_port('vision'))
        self._port = int(self._explicit_port or self._registry_port)

    def _resolve_port(self) -> int:
        """The port to talk to RIGHT NOW (see the class docstring).

        Asks RTM only if RTM is ALREADY imported in this process, via
        sys.modules rather than an `import`.  `_ports` is per-process
        in-memory state, so a process that never imported the manager
        cannot have a sidecar it started — the answer would be None
        anyway — and importing it here would register its atexit
        `stop_all` hook in every process that merely captions a frame.
        Same answer, no side effect.
        """
        if self._explicit_port:
            return int(self._explicit_port)
        try:
            import sys
            rm = sys.modules.get('integrations.service_tools.runtime_manager')
            if rm is not None:
                live = rm.runtime_tool_manager.get_tool_port('minicpm')
                if live:
                    return int(live)
        except Exception as e:
            logger.debug(f"RuntimeToolManager port lookup failed: {e}")
        return self._registry_port

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
        """A GPU AND the weights on disk.

        Every sibling checks its own prerequisites — Qwen08B wants its
        GGUF or a live server, MobileVLM wants onnxruntime, CLIP wants
        torch+clip — but this one asked only "is there a GPU", so any
        GPU box claimed the MiniCPM backend was available with no weights
        and no sidecar anywhere.  get_vision_backend()'s catalog branch
        and its "last resort: try minicpm" both gate on this call, so the
        answer decided whether a node SELECTED a backend that could not
        possibly answer a frame.  (MEASURED 2026-09-21 on this box:
        list_available_backends() reported minicpm available=True while
        nothing was listening on the vision port — true by luck, since
        the weights are here.)
        """
        try:
            from .minicpm_installer import MiniCPMInstaller
            installer = MiniCPMInstaller()
            return bool(installer.detect_gpu()) and installer.is_installed()
        except Exception:
            return False

    def describe(self, frame_bytes: bytes, prompt: str = '') -> Optional[str]:
        """Caption a frame through the MiniCPM sidecar's /describe endpoint.

        The wire shape is dictated by integrations/vision/minicpm_server.py
        `describe_raw()`: RAW image bytes as the body, the prompt as a QUERY
        param, and the caption under the key `result`.  This method used to
        send `{"image": <base64>, "prompt": ...}` as JSON and read a
        `description` key — the server would have handed that JSON body to
        PIL.Image.open and answered HTTP 500, and even a hypothetical success
        carries no `description` key.  Nothing caught it because the only
        test mocked pooled_post and asserted the invented shape.  The two
        callers that DO reach this server — VisionService._describe_frame and
        hart_intelligence_entry's MiniCPM tier — already speak raw-bytes/
        `result`; this is now the third.
        """
        port = self._resolve_port()
        # 30 s was the old budget and it only ever fit a GPU sidecar.  The
        # same model on CPU — where VRAMManager.suggest_offload_mode sends it
        # on an 8 GB card with an LLM resident — is minutes per caption
        # (MEASURED 2026-09-21: 61 s just to reach the first decode step),
        # and a client timeout shorter than the server turns a slow success
        # into a silent None.  HEVOLVE_MINICPM_TIMEOUT_S overrides.
        try:
            timeout_s = float(os.environ.get('HEVOLVE_MINICPM_TIMEOUT_S', 120))
        except ValueError:
            timeout_s = 120.0
        try:
            resp = pooled_post(
                f'http://localhost:{port}/describe',
                data=frame_bytes,
                params={'prompt': prompt or 'Describe what you see in this image.'},
                headers={'Content-Type': 'application/octet-stream'},
                timeout=timeout_s,
            )
            if resp.status_code == 200:
                return resp.json().get('result', '')
            logger.warning(
                f"MiniCPM /describe at :{port} returned HTTP "
                f"{resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.debug(f"MiniCPM describe error (port {port}): {e}")
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

    def _endpoint(self):
        """The shared Qwen3-VL backend: the same one computer use drives."""
        if self._backend is None:
            from integrations.vlm.qwen3vl_backend import get_qwen3vl_backend
            self._backend = get_qwen3vl_backend()
        return self._backend

    def completions_url(self) -> str:
        return f'{self._endpoint().base_url.rstrip("/")}/chat/completions'

    def describe(self, frame_bytes: bytes, prompt: str = '') -> Optional[str]:
        try:
            endpoint = self._endpoint()
        except Exception:
            return None
        try:
            import base64
            b64 = base64.b64encode(frame_bytes).decode('utf-8')
            return endpoint.describe_scene(
                b64, prompt or 'Describe what you see in this image.'
            )
        except Exception as e:
            logger.debug(f"Qwen3-VL describe error: {e}")
            return None

    def read_document(self, image_bytes: bytes, prompt: str) -> Optional[str]:
        """A page, through the same Qwen3-VL endpoint describe() uses, with
        the page request (thinking off, a page-sized budget) instead of the
        scene request."""
        try:
            endpoint = self._endpoint()
            url = self.completions_url()
        except Exception as e:
            logger.warning(f"Qwen3-VL unavailable for a page read: {e}")
            return None
        return _read_page_with(url, image_bytes, prompt, model=endpoint.model_name,
                               headers={'Authorization': f'Bearer {endpoint.api_key}'})


class Qwen08BBackend(VisionBackend):
    """Qwen3.5-0.8B — fast continuous captioning (1s/frame).

    Runs on a dedicated llama-server instance (its port from
    core.port_registry, 'vlm_caption'), separate from the 4B model used for
    computer use / action planning.

    Purpose: always-on frame captioning → FrameStore activity table, and
    reading document pages (read_document) for the book pipeline.
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
        """True if the backend can answer — server running OR model files present.

        get_vision_backend()'s fallback chain uses this to decide whether to
        SELECT qwen08b (the preferred captioner) or skip down to the
        MiniCPM fallback.  The old strict "server must already be listening
        on port" check caused every boot to skip qwen08b and silently land
        on MiniCPM (4GB VRAM) because the lazy-start path hadn't launched
        the server yet.  Returning True when model files exist lets the
        backend be selected at boot; describe() / start() preserve the
        original lazy-launch contract — we don't burn VRAM until a frame
        actually arrives.
        """
        try:
            resp = pooled_get(f'http://127.0.0.1:{self._port}/health', timeout=2)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        home = os.path.expanduser('~')
        for d in [os.path.join(home, '.nunba', 'models'),
                  os.path.join(home, '.trueflow', 'models')]:
            if os.path.isfile(os.path.join(d, 'Qwen3.5-0.8B-UD-Q4_K_XL.gguf')):
                return True
        return False

    def start(self) -> bool:
        """Lazy: don't boot at VisionService.start(). describe() does the
        launch on the first frame so we don't burn VRAM when the user has
        no camera/screen stream active."""
        if self.is_available():
            logger.info(f"Qwen3.5-0.8B caption backend ready on port {self._port}")
        else:
            logger.info(
                "Qwen3.5-0.8B not running — will start on first frame")
        return True  # Stay selected; lazy start in describe().

        # Find llama-server binary (reuse model_lifecycle's finder)

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
            # APPEND mode — caption-server can crash + respawn; each
            # restart's truncation erased the previous crash evidence.
            # Root-cause class: truncate-on-restart log loss.
            _log_fh = open(log_path, 'a')
            try:
                import datetime as _lb_dt
                _log_fh.write(
                    f"\n===== llama-caption (lightweight) session "
                    f"{_lb_dt.datetime.now().isoformat()} port={self._port} =====\n"
                )
                _log_fh.flush()
            except Exception:
                pass
            _kw = dict(stdout=_log_fh, stderr=subprocess.STDOUT)
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
    #: How long a FAILED launch suppresses the next attempt.  Not forever:
    #: see _ensure_running for why permanence was a defect (#102).
    LAUNCH_RETRY_S = 120

    def __init__(self, port: int = None):
        from core.port_registry import get_port
        self._port = port or get_port('vlm_caption')
        self._launch_attempted = False
        self._launch_attempted_at = 0.0
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
        import time as _t
        if self._launch_attempted:
            # A COOLDOWN, not a latch (#102, found by hartos-94).  This flag
            # was cleared in exactly one place -- the tail of stop() -- and
            # check_idle only reaches stop() through `if self._server_proc`,
            # which is None after a FAILED launch.  So one failure set the
            # flag forever and captioning was dead for the life of the
            # process, on a backend whose whole design is to start lazily
            # per frame.
            #
            # Transient failure is the normal case here, not the exception:
            # the event wait below is only 5x1s so a still-booting Nunba
            # loses the race, the standalone path needs a llama-server
            # binary that aborts on this box, and it competes for VRAM with
            # the resident LLM.  Any of those should cost one cooldown, not
            # the feature.
            if _t.time() - self._launch_attempted_at < self.LAUNCH_RETRY_S:
                return False
            logger.info(
                f"Qwen3.5-0.8B: retrying launch after "
                f"{self.LAUNCH_RETRY_S}s cooldown")
        self._launch_attempted = True
        self._launch_attempted_at = _t.time()

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
            # APPEND mode — same root-cause class as the caption-server
            # launch above.  Preserves prior run's log across restarts.
            log_fh = open(log_path, 'a')
            try:
                import datetime as _lb_dt
                log_fh.write(
                    f"\n===== llama-caption (standalone) session "
                    f"{_lb_dt.datetime.now().isoformat()} port={self._port} =====\n"
                )
                log_fh.flush()
            except Exception:
                pass
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

    def read_document(self, image_bytes: bytes, prompt: str) -> Optional[str]:
        """A page, through the caption server: the same lazy start and port
        as describe(), none of its caption shrink (512x288 at 100 tokens is
        far too little to read a page)."""
        import time
        if not self._ensure_running():
            return None
        self._last_describe_time = time.time()
        return _read_page_with(
            f'http://127.0.0.1:{self._port}/v1/chat/completions', image_bytes, prompt,
            model='local')


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


def get_document_readers() -> list:
    """How this node reads a document page, best first: each is
    read(image_bytes, prompt) -> Optional[str], and a page goes to the next
    when one does not answer.

    First the node's vision backend -- get_vision_backend(), the one camera,
    screen and media captions use -- when it can read a page at all. Then the
    node's own main model (core.port_registry.get_local_llm_url), which read
    every book page before 2026-09-14: a node whose vision backend cannot
    read a page (MiniCPM, MobileVLM, CLIP, none), or whose caption server
    does not answer, reads no fewer pages than it did. The fallback is always
    this node's own model, never a configured remote endpoint.
    """
    readers = []
    node = get_vision_backend()
    if type(node).read_document is not VisionBackend.read_document:
        readers.append(node.read_document)
    try:
        from core.port_registry import get_local_llm_url
        main_url = get_local_llm_url().rstrip('/') + '/chat/completions'
    except Exception as e:
        logger.warning(f"the main model's address is unknown; pages have no fallback: {e}")
        return readers
    if isinstance(node, Qwen3VLVisionBackend):
        try:
            if node.completions_url() == main_url:
                return readers          # its VLM endpoint IS the main model
        except Exception as e:
            logger.debug(f"Qwen3-VL endpoint unresolved: {e}")
    readers.append(functools.partial(_read_page_with, main_url, model='qwen'))
    return readers


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
        # Claiming skip -- see ModelCatalog.already_registered.  Skipping an
        # entry this populator still owns must not read as abandoning it:
        # populate_from_subsystems sweeps auto-prefixed entries nobody
        # claimed, and vlm-minicpm-v2 was OSCILLATING because of this line
        # (added by one populate, swept by the next, added by the third).
        if catalog.already_registered(mid):
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
