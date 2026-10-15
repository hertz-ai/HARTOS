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
from typing import Callable, Optional

from core.http_pool import LLM_COMPLETION_TIMEOUT, pooled_get, pooled_post

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
    #
    # `rate_frame` is deliberately NOT here.  A frame the model cannot rate
    # (server down, a prose answer, the caller's time budget spent) falls
    # back to a deterministic score in the caller; that is the designed
    # path, not a subsystem failure, and 25 of them per reel would only
    # flood the self-heal pipeline.
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

    def rate_frame(self, image_bytes: bytes, prompt: str, *, long_side: int = 768,
                   max_tokens: int = 80, timeout_s: float = 20.0) -> Optional[str]:
        """Judge one video frame against a rubric prompt.

        Reel Studio rates up to 25 keyframes of a reel this way (which makeup
        stage the frame shows, how appealing it is), asking for a short JSON
        answer that the caller parses.  The frame goes with its aspect kept,
        no longer than ``long_side`` on its long side, at temperature 0 with
        thinking off, and ``max_tokens`` of answer.

        Unlike describe() and read_document(), it never starts a model
        server: a server that does not answer /health is a None, and the
        caller falls back.  A caller that rates many frames asks
        ensure_running() itself, once, before its first frame (Reel Studio's
        score_moments does).  ``timeout_s`` bounds the request itself, so a
        caller with a time budget passes what is left of it.  It never
        raises, and it is not an auto-reported method (see
        AUTO_REPORTED_METHODS).

        Returns:
            The model's answer ('' when it answered with nothing), or None
            when this backend cannot rate a frame, its server is not up, or
            the request failed.  The default is None.
        """
        return None

    def ensure_running(self, on_wait: Optional[Callable[[], None]] = None) -> bool:
        """Make this backend able to answer now, starting its model server
        when it has one to start.  True when it can answer.

        describe() and read_document() ask it before they send anything;
        rate_frame() never does, its caller asks it once (Reel Studio's
        score_moments, before the first frame it needs the model for).

        ``on_wait`` is called once, just before this method waits for a
        server or starts one -- never when the backend is ready as it is --
        so a caller can say the model is being made ready only when it is
        (a reel's chat line).  A callback that raises is logged; the start
        goes on.

        The default is True and calls nothing: this backend has no server to
        start (it loads its model in start(), talks to a server something
        else runs, or has no model).  Qwen08BBackend's is the caption
        server's lazy launch.
        """
        return True

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


def _fit_jpeg(image_bytes: bytes, long_side: int) -> bytes:
    """The image as JPEG bytes (quality 85), its aspect kept, no longer than
    ``long_side`` on its long side.  A smaller image is not enlarged."""
    import io
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes))
    if max(img.size) > long_side:
        scale = long_side / max(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.convert('RGB').save(buf, 'JPEG', quality=85)
    return buf.getvalue()


def _vision_complete(completions_url: str, image_bytes: bytes, prompt: str, *,
                     model: str, long_side: int, max_tokens: int, temperature: float,
                     headers: Optional[dict] = None,
                     timeout=LLM_COMPLETION_TIMEOUT) -> Optional[str]:
    """The one image + prompt request to an OpenAI-compatible vision server.

    Page reads (_read_page_with) and frame ratings (rate_frame) both go
    through here; they differ only in the image size, the answer budget,
    the temperature and the timeout they pass.  The image goes first, then
    the prompt.

    Thinking is off (core.constants.LLM_THINKING_OFF_KWARGS): a hybrid
    reasoning model otherwise spends its budget in reasoning_content and
    answers with nothing. pooled_post admits a main-model call through the
    priority scheduler, so a background request waits behind the user's own
    turn. Never raises.

    Returns the stripped answer ('' when the model answered with nothing), or
    None when the image could not be prepared, the server could not be
    reached, or it answered with a non-200 or with no usable JSON.
    """
    import base64
    from core.constants import LLM_THINKING_OFF_KWARGS
    try:
        b64 = base64.b64encode(_fit_jpeg(image_bytes, long_side)).decode('ascii')
    except Exception as e:
        logger.warning(f"image could not be prepared for the VLM: {e}")
        return None
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
            {'type': 'text', 'text': prompt},
        ]}],
        'chat_template_kwargs': dict(LLM_THINKING_OFF_KWARGS),
        'max_tokens': max_tokens,
        'temperature': temperature,
    }
    extra = {'headers': headers} if headers else {}
    try:
        resp = pooled_post(completions_url, json=body, timeout=timeout, **extra)
    except Exception as e:
        logger.warning(f"VLM request failed at {completions_url}: {e}")
        return None
    if resp.status_code != 200:
        logger.warning(f"VLM request at {completions_url} returned HTTP "
                       f"{resp.status_code}: {resp.text[:200]}")
        return None
    try:
        choice = (resp.json().get('choices') or [{}])[0]
    except (ValueError, AttributeError) as e:
        logger.warning(f"VLM at {completions_url} answered with no usable JSON: {e}")
        return None
    message = choice.get('message') or {}
    content = (message.get('content') or '').strip()
    if choice.get('finish_reason') == 'length':
        logger.warning("VLM answer stopped at its %d-token budget: it is cut off",
                       max_tokens)
    if not content:
        logger.warning("VLM produced EMPTY content (finish_reason=%s, "
                       "reasoning_content=%d chars)", choice.get('finish_reason'),
                       len(message.get('reasoning_content') or ''))
    return content


def _read_page_with(completions_url: str, image_bytes: bytes, prompt: str, *,
                    model: str, headers: Optional[dict] = None) -> Optional[str]:
    """The one page-reading request to an OpenAI-compatible vision server:
    the page at PAGE_LONG_SIDE, PAGE_MAX_TOKENS of answer, temperature 0.3,
    the LLM completion timeout.  Never raises: a page the model does not
    read, the caller reads from somewhere else."""
    return _vision_complete(completions_url, image_bytes, prompt, model=model,
                            long_side=PAGE_LONG_SIDE, max_tokens=PAGE_MAX_TOKENS,
                            temperature=0.3, headers=headers)


#: A health probe of a local vision server: 1 s to connect, 2 s to answer.
#: llama-server answers /health 200 when its model is loaded and 503 while
#: it loads; anything else, or no answer in time, is "not serving".
HEALTH_TIMEOUT = (1, 2)


def _health_ok(server_root: str) -> bool:
    """True only when the server at ``server_root`` answers GET /health 200."""
    try:
        resp = pooled_get(f'{server_root.rstrip("/")}/health', timeout=HEALTH_TIMEOUT)
        return resp.status_code == 200
    except Exception:
        return False


def _server_root(base_url: str) -> str:
    """An OpenAI-compatible base URL without its trailing /v1:
    'http://127.0.0.1:8080/v1/' -> 'http://127.0.0.1:8080'."""
    root = base_url.rstrip('/')
    return root[:-len('/v1')] if root.endswith('/v1') else root


def _rate_frame_at(completions_url: str, serving, image_bytes: bytes, prompt: str, *,
                   model: str, long_side: int, max_tokens: int, timeout_s,
                   headers: Optional[dict] = None) -> Optional[str]:
    """VisionBackend.rate_frame for a backend with an OpenAI-compatible server.

    ``serving()`` is the backend's own health check, asked first; nothing is
    started when it says no.  Then one _vision_complete at temperature 0
    with ``timeout_s`` as the request timeout.  A timeout that is not a
    positive number is no time at all: no probe and no request.  Never
    raises.
    """
    import math
    try:
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) \
                or not math.isfinite(timeout_s) or timeout_s <= 0:
            logger.warning(f"rate_frame: no time to rate a frame (timeout_s={timeout_s!r})")
            return None
        if not serving():
            logger.info(f"rate_frame: no vision server answering at {completions_url}")
            return None
        return _vision_complete(completions_url, image_bytes, prompt, model=model,
                                long_side=long_side, max_tokens=max_tokens,
                                temperature=0.0, headers=headers, timeout=timeout_s)
    except Exception as e:
        logger.warning(f"rate_frame failed at {completions_url}: {e}")
        return None


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

    def rate_frame(self, image_bytes: bytes, prompt: str, *, long_side: int = 768,
                   max_tokens: int = 80, timeout_s: float = 20.0) -> Optional[str]:
        """A keyframe rating through the same endpoint, after its /health
        answers (see VisionBackend.rate_frame).  This backend has no
        launcher; it sends nothing when the endpoint is not serving."""
        try:
            endpoint = self._endpoint()
            url = self.completions_url()
            root = _server_root(endpoint.base_url)
        except Exception as e:
            logger.warning(f"Qwen3-VL unavailable to rate a frame: {e}")
            return None
        return _rate_frame_at(url, lambda: _health_ok(root), image_bytes, prompt,
                              model=endpoint.model_name, long_side=long_side,
                              max_tokens=max_tokens, timeout_s=timeout_s,
                              headers={'Authorization': f'Bearer {endpoint.api_key}'})


class Qwen08BBackend(VisionBackend):
    """Qwen3.5-0.8B — fast continuous captioning (1s/frame).

    Runs on a dedicated llama-server instance (its port from
    core.port_registry, 'vlm_caption'), separate from the 4B model used for
    computer use / action planning.

    Purpose: always-on frame captioning → FrameStore activity table,
    reading document pages (read_document) for the book pipeline, and
    rating Reel Studio keyframes (rate_frame).
    NOT for computer use (use 4B Qwen3VLVisionBackend for that).

    The server starts lazily, in ensure_running: describe() and
    read_document() ask it per call; a reel asks it ONCE, before its first
    keyframe that is not cached (score_moments), and rate_frame never asks
    it, so 25 frames cost at most one start.

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

        "Selectable", not "serving": the launch path asks _is_serving().

        The weights are looked for by the launch's own lookup, _model_files
        (the catalog row's file name; the folder it recorded, Nunba's
        models-folder rule, ~/.trueflow/models).  It was a second lookup, a
        literal name in ~/.nunba/models and ~/.trueflow/models only, so
        weights the launch would start (a custom NUNBA_MODELS_DIR, a saved
        models_dir, the catalog's folder) were never selected (review of
        6067e8806).  Whole weights alone make it selectable, not the
        projector: Nunba's start fetches a missing projector itself
        (llama_config._do_start_caption_server, _download_mmproj_only).
        Whole by the one completeness rule (see _model_files): a partial
        download, which the launch could only fail to load, is not
        selectable.  A lookup that fails -- the catalog unreadable, so the
        launch could not find the files either -- is not selectable.
        """
        if self._is_serving():
            return True
        try:
            model, _mmproj = self._model_files()
        except Exception as e:
            logger.warning(f"Qwen3.5-0.8B: model lookup failed ({e}); not selectable")
            return False
        return model is not None

    def _is_serving(self) -> bool:
        """True only when the caption server answers /health on its port."""
        return _health_ok(f'http://127.0.0.1:{self._port}')

    def completions_url(self) -> str:
        """The caption server's chat completions URL (this node, its port)."""
        return f'http://127.0.0.1:{self._port}/v1/chat/completions'

    def start(self) -> bool:
        """Lazy: don't boot at VisionService.start(). describe() does the
        launch on the first frame so we don't burn VRAM when the user has
        no camera/screen stream active."""
        if self._is_serving():
            logger.info(f"Qwen3.5-0.8B caption backend ready on port {self._port}")
        else:
            logger.info(
                "Qwen3.5-0.8B not running — will start on first frame")
        return True  # Stay selected; lazy start in describe().

        # DELETED 2026-09-22: ~60 lines of a SECOND caption-server launcher
        # used to sit here, after that unconditional `return True`.  It was
        # unreachable — dead since the lazy-start contract moved the launch
        # into ensure_running (then private: _ensure_running) — but it was a
        # byte-for-byte duplicate of the launcher below, including its own
        # hardcoded 512-token window.  A dead parallel path still costs: it is
        # the copy a reader greps up first, and it is the copy a future edit
        # lands in.  The live launcher is ensure_running; there is now exactly
        # one.

    # 0.8B optimal: 512x288 (11KB JPEG) — only needs scene understanding, not coords
    CAPTION_WIDTH = 512
    CAPTION_HEIGHT = 288
    IDLE_TIMEOUT_S = 300  # Unload after 5 min with no frames
    #: How long a FAILED launch suppresses the next attempt.  Not forever:
    #: see ensure_running for why permanence was a defect (#102).
    LAUNCH_RETRY_S = 120
    #: The event ensure_running sends; Nunba's subscriber (main.py ~:669)
    #: answers it with llama_config.start_caption_server.
    START_EVENT = 'vlm_caption.requested'
    #: Nunba's per-port caption start lock (start_lock_path) means "a start
    #: is in progress" only while it is younger than this; an older one was
    #: left by a start that died (Nunba killed mid-start), and Nunba's next
    #: start removes it.  ONE number for the writer and the reader: Nunba's
    #: start_caption_server (llama/llama_config.py) waits on and removes the
    #: lock by this attribute, and _launch_owner reads it by the same one.
    #: It lives here, not in Nunba, because HARTOS cannot import Nunba (a
    #: HARTOS-only process is the one a stale lock stopped).  Nunba's MAIN
    #: server lock (.server_starting.lock, llama_config.start_server) keeps
    #: its own 120 s: another file, guarding another server's start, which
    #: nothing in HARTOS reads -- its bound is its own question.
    START_LOCK_STALE_S = 120
    #: The model catalog row whose files this backend serves (the 0.8B and
    #: its projector, as Nunba's start_caption_server runs them):
    #: model_catalog._populate_llm_models.
    CATALOG_ID = 'llm-qwen3.5-0.8b'

    def __init__(self, port: int = None):
        from core.port_registry import get_port
        self._port = port or get_port('vlm_caption')
        self._launch_attempted = False
        self._launch_attempted_at = 0.0
        self._last_describe_time = 0.0
        self._server_proc = None  # subprocess.Popen object (not just PID)

    @staticmethod
    def start_lock_path(port, config_dir=None) -> str:
        """Nunba's per-port caption start lock, named once for both sides:
        <config_dir>/.caption_server_starting.<port>.lock.

        ``config_dir`` is Nunba's config folder (LlamaConfig.config_dir);
        left out, its default ~/.nunba, where Nunba keeps it (no LlamaConfig
        outside tests is built with another folder).  Nunba's
        start_caption_server writes the lock here, holds it while it starts
        the server and removes it; HARTOS only reads it (_launch_owner).
        """
        folder = (config_dir if config_dir is not None
                  else os.path.join(os.path.expanduser('~'), '.nunba'))
        return os.path.join(str(folder), f'.caption_server_starting.{port}.lock')

    def _launch_owner(self) -> str:
        """Why this backend must not spawn the caption server itself --
        Nunba is starting it -- or '' when nobody is.

        Both are Nunba's existing records, read here, never written:

        * its START_EVENT subscriber on this process's platform event bus
          (Nunba main.py ~:669): it answers the event ensure_running has
          just sent with llama_config.start_caption_server;
        * its per-port start lock, start_lock_path(port), which serializes
          that start across Nunba's own callers and processes -- held only
          while it is younger than START_LOCK_STALE_S, the number Nunba's
          start_caption_server ages it by too.  One older than that was left
          by a start that died (a Nunba killed mid-start): Nunba's next
          start removes it, and it stops nobody here either.  HARTOS reads
          the lock and never writes or removes it.

        Spawning while either holds is how two 0.8B servers landed on one
        port (2026-09-26).  A stale lock taken as held stopped a HARTOS-only
        process for good (review of 6067e8806): every start waited 35 s,
        spawned nothing and failed, until Nunba ran again.
        """
        import time
        try:
            from core.platform.registry import get_registry
            bus = get_registry().get_or_none('events')
            if bus is not None and bus.has_listeners(self.START_EVENT):
                return f"Nunba's {self.START_EVENT} subscriber is starting it"
        except Exception as e:
            logger.debug(f"Qwen3.5-0.8B: event bus not readable ({e})")
        lock = self.start_lock_path(self._port)
        try:
            age = time.time() - os.path.getmtime(lock)
        except OSError:             # no lock, or it went as we looked: no start
            return ''
        if age < self.START_LOCK_STALE_S:
            return f"Nunba's start lock {lock} is held ({age:.0f} s old)"
        logger.info(f"Qwen3.5-0.8B: Nunba's start lock {lock} is stale ({age:.0f} s "
                    f"old): no start in progress")
        return ''

    def _model_files(self) -> tuple:
        """(model, mmproj) on disk for the standalone launch; None for a
        file that is not there.  is_available() asks it too, for the model,
        so selection and the launch find the 0.8B in the same places.

        Names from the catalog row CATALOG_ID: files['model'], and the
        projector under the name Nunba's installer stores it by,
        files['mmproj'] (mmproj-Qwen3.5-0.8B-F16.gguf), or where
        scripts/setup_vlm.py puts it, files['mmproj_source'] in a 'qwen08b'
        folder.  Folders, first hit wins: the one the row's local_path names
        (where the loader that fetched the weights put them); the LLM models
        folder by Nunba's one storage rule, LlamaInstaller.resolve_models_dir
        (llama_installer.py ~:446), where Nunba is importable, else that
        rule's default ~/.nunba/models; then ~/.trueflow/models.

        The weights count only when whole, by the one completeness rule
        Nunba's get_model_path asks too: model_catalog.gguf_complete at
        gguf_min_bytes of the registered size (90% of it, the GGUF magic).
        A partial download is not found, so it is neither selected nor
        spawned, and a whole copy in a later folder is found past it.  The
        projector has no registered size and is taken as any file of its
        name, as before.
        """
        from integrations.service_tools.model_catalog import (
            get_catalog, gguf_complete, gguf_min_bytes, model_weight_bytes)
        entry = get_catalog().get(self.CATALOG_ID)
        if entry is None:
            logger.warning(f"Qwen3.5-0.8B: catalog row {self.CATALOG_ID} is missing")
            return None, None
        files = entry.files or {}
        home = os.path.expanduser('~')
        try:
            from llama.llama_installer import LlamaInstaller   # Nunba, when present
            models_dir = str(LlamaInstaller.resolve_models_dir())
        except Exception as e:
            logger.debug(f"Qwen3.5-0.8B: Nunba's models folder rule not here ({e})")
            models_dir = os.path.join(home, '.nunba', 'models')
        dirs = [os.path.dirname(entry.local_path)] if entry.local_path else []
        dirs += [models_dir, os.path.join(home, '.trueflow', 'models')]

        def first(*parts, usable=os.path.isfile):
            if not all(parts):
                return None
            return next((p for p in (os.path.join(d, *parts) for d in dirs)
                         if usable(p)), None)
        whole = gguf_min_bytes(model_weight_bytes(files.get('model') or ''))
        return (first(files.get('model'), usable=lambda p: gguf_complete(p, whole)),
                first(files.get('mmproj')) or first('qwen08b', files.get('mmproj_source')))

    @staticmethod
    def _launch_geometry(model: str) -> tuple:
        """(--ctx-size, --n-gpu-layers) for the standalone launch of the
        weights at ``model``, decided as the other start of this server
        decides them.

        The context is the one Nunba's start_caption_server gives the 0.8B
        (llama_config._derive_ctx_size, the 0.8B preset being a Qwen3.5
        one): core.llama_geometry.derive_ctx_size over the free VRAM of a
        FORCED sample (vram_manager.refresh_gpu_info(force=True), not the
        memo) and the weights measured from the file, or ctx_for_role('main')
        when the box cannot be measured.  The launch used a fixed 512
        (ctx_for_role('caption')): one caption fits, but a reel's keyframe
        rating is 550 prompt tokens and every frame got HTTP 400 (measured
        2026-10-14, A13 proof D2), and a page read (PAGE_LONG_SIDE,
        PAGE_MAX_TOKENS) never fitted either.

        The GPU layers are llamacpp_manager.get_optimal_params', HARTOS's one
        placement of a GGUF on this box: every layer (-1) where
        model_catalog.gguf_fits_gpu says it fits, a partial offload sized
        from the file's blocks where it does not, none without a usable GPU.
        It reads the sample just forced.  The launch passed 99 whatever the
        card held.
        """
        from core.llama_geometry import ctx_for_role, derive_ctx_size
        from integrations.service_tools.llamacpp_manager import get_llamacpp_manager
        from integrations.service_tools.vram_manager import vram_manager
        try:
            free_gib = vram_manager.refresh_gpu_info(force=True).get('free_gb', 0)
            ctx_size = derive_ctx_size(free_gib, os.path.getsize(model) / (1024 ** 3))
        except Exception as e:
            ctx_size = ctx_for_role('main')
            logger.info(f"Qwen3.5-0.8B: the box could not be measured ({e}); "
                        f"context {ctx_size}")
        return ctx_size, get_llamacpp_manager().get_optimal_params(model)['n_gpu_layers']

    def ensure_running(self, on_wait: Optional[Callable[[], None]] = None) -> bool:
        """Lazy-start: launch 0.8B server on first frame, not at boot.

        VisionBackend.ensure_running for the caption server (it was the
        private _ensure_running until 2026-10-14; describe(),
        read_document() and Reel Studio's score_moments call it).

        HARTOS emits 'vlm_caption.requested' event. In bundled mode, Nunba
        subscribes to this event and calls its own start_caption_server().
        While Nunba is starting it (_launch_owner: its subscriber took the
        event, or its per-port start lock is held, i.e. younger than
        START_LOCK_STALE_S) HARTOS spawns nothing and waits for Nunba's
        server, 5 s + 30 s at most, the bound its own launch has.  In
        standalone mode (neither; a stale lock is neither), HARTOS uses
        model_lifecycle to launch directly, with the files _model_files
        finds through the model catalog, at the context size and GPU layers
        _launch_geometry decides (as Nunba's start of this server does).

        ``on_wait`` is called once, after the server was found not serving
        and before the event is sent: not when it is up, not while a failed
        launch's cooldown holds.  In its own try, so a caller's progress
        line that raises never skips the start it announces (review of
        42735064e).

        Dependency direction: Nunba → HARTOS (never HARTOS → Nunba).  What
        is read of Nunba (its event subscriber, its start lock, its models
        folder rule when importable) is optional: a HARTOS-only node has none
        of it and launches standalone.

        Every "is it up" check here is _is_serving(), never is_available():
        is_available() is also True when only the weights are on disk, and
        using it here returned True with nothing listening -- no launch was
        ever asked for.  Measured 2026-09-27: GGUF present, :8081 refused,
        VisionService described=0 over 697 frames.
        """
        if self._is_serving():
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
            # a still-booting Nunba can take longer than the wait below, the
            # standalone path needs a llama-server binary that aborts on
            # this box, and it competes for VRAM with the resident LLM.  Any
            # of those should cost one cooldown, not the feature.
            if _t.time() - self._launch_attempted_at < self.LAUNCH_RETRY_S:
                return False
            logger.info(
                f"Qwen3.5-0.8B: retrying launch after "
                f"{self.LAUNCH_RETRY_S}s cooldown")
            if self._server_proc is not None:
                # The process WE launched is still not serving after a whole
                # cooldown: wedged, not slow.  Relaunching over it spawned a
                # second llama-server every LAUNCH_RETRY_S and leaked the
                # first process and its log handle (hartos-3a F7).  Stop it
                # through the one stop path first.
                logger.warning(
                    f"Qwen3.5-0.8B: PID={self._server_proc.pid} still not "
                    f"serving after {self.LAUNCH_RETRY_S}s; stopping it "
                    f"before relaunching")
                self.stop()
        self._launch_attempted = True
        self._launch_attempted_at = _t.time()

        if on_wait is not None:
            try:
                on_wait()
            except Exception as e:
                logger.warning(f"Qwen3.5-0.8B: on_wait raised ({e}); starting anyway")

        # Emit event — Nunba subscribes in bundled mode and starts the server
        try:
            from core.platform.events import emit_event
            emit_event(self.START_EVENT, {'port': self._port})
        except Exception:
            pass

        # Wait briefly — Nunba may start the server in response to the event
        import time
        for _ in range(5):
            time.sleep(1)
            if self._is_serving():
                logger.info(f"Qwen3.5-0.8B started (event-driven) on port {self._port}")
                return True

        # Not up yet.  If Nunba is starting it (its subscriber took the event,
        # or its per-port lock is held), a spawn here is a second 0.8B server
        # on the port -- the 2026-09-26 incident class.  Spawn nothing; wait
        # for its server the 30 s this backend waits for its own.  Nobody
        # starting it — standalone mode, use model_lifecycle.
        owner = self._launch_owner()
        if owner:
            logger.info(f"Qwen3.5-0.8B: not spawning on port {self._port}: {owner}; "
                        f"waiting for it")
        else:
            try:
                from integrations.service_tools.model_lifecycle import ModelLifecycleManager
                server = ModelLifecycleManager._find_llama_server_binary()
                if not server:
                    logger.info("Qwen3.5-0.8B: llama-server not found")
                    return False

                model, mmproj = self._model_files()
                if not model or not mmproj:
                    logger.info("Qwen3.5-0.8B: model files not found")
                    return False

                import subprocess
                # Sized and placed as the other start of this server sizes it
                # (_launch_geometry).  This spawn does NOT publish_geometry:
                # it is a caption server on the vlm_caption port, not the main
                # engine, and publishing its window would tell HARTOS's wire
                # trimmer what the MAIN server holds.
                ctx_size, gpu_layers = self._launch_geometry(model)
                cmd = [server, '--model', model, '--mmproj', mmproj,
                       '--port', str(self._port),
                       '--ctx-size', str(ctx_size),
                       '--n-gpu-layers', str(gpu_layers),
                       '--threads', '4', '--flash-attn', 'on']
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
                logger.info(f"Qwen3.5-0.8B launching PID={self._server_proc.pid} "
                            f"port={self._port} ctx={ctx_size} gpu_layers={gpu_layers}")
            except Exception as e:
                logger.error(f"Qwen3.5-0.8B standalone start failed: {e}")
                return False
        for _ in range(30):
            time.sleep(1)
            if self._is_serving():
                logger.info(f"Qwen3.5-0.8B ready on port {self._port}")
                return True
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
        if not self.ensure_running():
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
                self.completions_url(),
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
        if not self.ensure_running():
            return None
        self._last_describe_time = time.time()
        return _read_page_with(self.completions_url(), image_bytes, prompt, model='local')

    def rate_frame(self, image_bytes: bytes, prompt: str, *, long_side: int = 768,
                   max_tokens: int = 80, timeout_s: float = 20.0) -> Optional[str]:
        """A keyframe rating on the caption server, ONLY if it is already
        serving (see VisionBackend.rate_frame).

        Never ensure_running per frame: that start emits
        vlm_caption.requested and waits for the server, and a reel rates up
        to 25 frames on a time budget with a deterministic fallback.  The
        rule: the reel (score_moments) asks ensure_running ONCE, before its
        first frame that is not cached; each frame then only asks /health.
        Not serving is a None.
        """
        return _rate_frame_at(self.completions_url(), self._is_serving, image_bytes, prompt,
                              model='local', long_side=long_side, max_tokens=max_tokens,
                              timeout_s=timeout_s)


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
