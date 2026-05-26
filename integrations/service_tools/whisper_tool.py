"""
STT tool — in-process speech-to-text.

Engine priority (first available wins):
  1. faster-whisper (CTranslate2) — preferred, 4x faster than openai-whisper,
     multilingual, GPU+CPU, auto-downloads models from HuggingFace.
  2. sherpa-onnx — lightweight ONNX alternative, no PyTorch dependency.
  3. openai-whisper — legacy fallback (requires PyTorch).

Model selection by hardware (via select_whisper_model):
  - CPU, low RAM  → tiny / moonshine-tiny (English, fastest)
  - CPU, 4-8GB    → base / whisper-tiny (multilingual)
  - GPU, 2-5GB    → small (multilingual)
  - GPU, 5-10GB   → medium (multilingual)
  - GPU, 10+GB    → large-v3 (multilingual, best accuracy)

Models downloaded lazily on first use to ~/.hevolve/models/stt/
100% local, zero cloud costs — Nunba is forever free.
"""

import json
import logging
import os
import tarfile
import urllib.request
from pathlib import Path
from typing import Optional

from .registry import ServiceToolInfo, service_tool_registry

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Model registry — sherpa-onnx model configurations
# ═══════════════════════════════════════════════════════════════

_SHERPA_MODEL_BASE = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
)

_SHERPA_MODELS = {
    "moonshine-tiny": {
        "type": "moonshine",
        "archive": "sherpa-onnx-moonshine-tiny-en-int8.tar.bz2",
        "dir": "sherpa-onnx-moonshine-tiny-en-int8",
        "files": {
            "preprocessor": "preprocess.onnx",
            "encoder": "encode.int8.onnx",
            "uncached_decoder": "uncached_decode.int8.onnx",
            "cached_decoder": "cached_decode.int8.onnx",
            "tokens": "tokens.txt",
        },
        "multilingual": False,
    },
    "moonshine-base": {
        "type": "moonshine",
        "archive": "sherpa-onnx-moonshine-base-en-int8.tar.bz2",
        "dir": "sherpa-onnx-moonshine-base-en-int8",
        "files": {
            "preprocessor": "preprocess.onnx",
            "encoder": "encode.int8.onnx",
            "uncached_decoder": "uncached_decode.int8.onnx",
            "cached_decoder": "cached_decode.int8.onnx",
            "tokens": "tokens.txt",
        },
        "multilingual": False,
    },
    "whisper-tiny": {
        "type": "whisper",
        "archive": "sherpa-onnx-whisper-tiny.tar.bz2",
        "dir": "sherpa-onnx-whisper-tiny",
        "files": {
            "encoder": "tiny-encoder.int8.onnx",
            "decoder": "tiny-decoder.int8.onnx",
            "tokens": "tiny-tokens.txt",
        },
        "multilingual": True,
    },
    "whisper-base": {
        "type": "whisper",
        "archive": "sherpa-onnx-whisper-base.tar.bz2",
        "dir": "sherpa-onnx-whisper-base",
        "files": {
            "encoder": "base-encoder.int8.onnx",
            "decoder": "base-decoder.int8.onnx",
            "tokens": "base-tokens.txt",
        },
        "multilingual": True,
    },
    "whisper-small": {
        "type": "whisper",
        "archive": "sherpa-onnx-whisper-small.tar.bz2",
        "dir": "sherpa-onnx-whisper-small",
        "files": {
            "encoder": "small-encoder.int8.onnx",
            "decoder": "small-decoder.int8.onnx",
            "tokens": "small-tokens.txt",
        },
        "multilingual": True,
    },
    "whisper-medium": {
        "type": "whisper",
        "archive": "sherpa-onnx-whisper-medium.tar.bz2",
        "dir": "sherpa-onnx-whisper-medium",
        "files": {
            "encoder": "medium-encoder.int8.onnx",
            "decoder": "medium-decoder.int8.onnx",
            "tokens": "medium-tokens.txt",
        },
        "multilingual": True,
    },
}

# ═══════════════════════════════════════════════════════════════
# Cached recognizers (avoid reloading on every call)
# ═══════════════════════════════════════════════════════════════

_sherpa_recognizer = None
_sherpa_model_name = None

# Legacy openai-whisper fallback
_whisper_model = None
_whisper_model_name = None

# faster-whisper (CTranslate2) — preferred engine
_faster_whisper_model = None
_faster_whisper_model_size = None

# Backoff + circuit-breaker for the model-load retry storm.
# Symptom #10 (Stage-A, 2026-04-16): frozen_debug.log showed ~2Hz
# 'faster-whisper transcription failed: HF_HUB_OFFLINE' spam because
# every realtime transcribe call re-hit the network. Now:
#   - backoff: 1s -> 2s -> 4s -> ... capped at 300s
#   - breaker: after 5 consecutive model-load failures, OPEN for 300s
# The backoff is KEYED by model_size so changing size resets the timer.
# The breaker is a process-wide single instance (one faster-whisper
# model at a time in this module).
try:
    from core.circuit_breaker import CircuitBreaker, PeerBackoff
    _whisper_load_backoff = PeerBackoff(initial=1.0, maximum=300.0)
    _whisper_load_breaker = CircuitBreaker(
        name='faster_whisper_load', threshold=5, cooldown=300.0,
    )
except ImportError:  # dev tree without HARTOS core — defense-in-depth
    _whisper_load_backoff = None
    _whisper_load_breaker = None

# Track the last user-visible error so /api/admin/stt/status can
# surface it instead of the UI silently limping along. This is the
# 'visible error to the user' half of the symptom #10 mandate.
_whisper_last_error: Optional[str] = None


def get_whisper_last_error() -> Optional[str]:
    """Return the most recent faster-whisper failure reason, or None.

    Exposed so the admin/status UI can show a concrete error to the
    user instead of letting the retry storm accumulate silently in
    the log. Cleared on the next successful load or transcribe.
    """
    return _whisper_last_error


def _record_whisper_failure(reason: str) -> None:
    """Record a whisper failure across backoff + breaker + last-error."""
    global _whisper_last_error
    _whisper_last_error = reason
    if _whisper_load_breaker is not None:
        _whisper_load_breaker.record_failure()
    if _whisper_load_backoff is not None:
        _whisper_load_backoff.record_failure('faster_whisper')


def _record_whisper_success() -> None:
    """Reset backoff + breaker + clear last-error on success."""
    global _whisper_last_error
    _whisper_last_error = None
    if _whisper_load_breaker is not None:
        _whisper_load_breaker.record_success()
    if _whisper_load_backoff is not None:
        _whisper_load_backoff.record_success('faster_whisper')


# ═══════════════════════════════════════════════════════════════
# faster-whisper (primary engine)
# ═══════════════════════════════════════════════════════════════

# Default faster-whisper model size. Can be overridden by the user via the
# admin Model Management UI, which sets HEVOLVE_STT_MODEL_SIZE in the
# orchestrator and then stops the worker so the next call respawns with
# the new value picked up at subprocess startup.
_FASTER_WHISPER_MODEL_SIZE = os.environ.get(
    'HEVOLVE_STT_MODEL_SIZE', 'base',
)  # CPU int8 — preserves GPU VRAM for TTS/VLM


def _get_faster_whisper_model(model_size: str = "base"):
    """Lazy-load faster-whisper model (CTranslate2, auto-downloads from HuggingFace).

    Device selection:
      - NVIDIA GPU (CUDA): CTranslate2 uses its own CUDA runtime (not torch)
      - AMD GPU: CTranslate2 doesn't support ROCm/Vulkan -> CPU fallback
      - CPU: int8 quantization for speed

    Retry semantics (Symptom #10, 2026-04-16):
      - Backoff-gated: if the last load failed, refuses to retry until
        the exponential backoff window elapses.
      - Circuit-breaker-gated: after 5 consecutive failures, the
        breaker OPENS for 300s; calls raise RuntimeError instead of
        hitting the network on every streaming chunk.
      - Raises on refusal so the caller can log ONCE and skip the
        realtime path cleanly instead of absorbing the exception 2x
        per second.
    """
    global _faster_whisper_model, _faster_whisper_model_size
    if _faster_whisper_model is not None and _faster_whisper_model_size == model_size:
        return _faster_whisper_model

    # Gate 1: circuit breaker. If OPEN, refuse without even importing.
    if _whisper_load_breaker is not None and _whisper_load_breaker.is_open():
        raise RuntimeError(
            f"faster-whisper circuit breaker OPEN (stats="
            f"{_whisper_load_breaker.get_stats()}, last_error="
            f"{_whisper_last_error!r})"
        )

    # Gate 2: exponential backoff keyed by model name. If recently
    # failed, refuse until the window elapses. Caller gets a clear
    # single log line per refusal, not 2Hz spam.
    if (_whisper_load_backoff is not None
            and _whisper_load_backoff.is_backed_off('faster_whisper')):
        raise RuntimeError(
            f"faster-whisper load is backed off (last_error="
            f"{_whisper_last_error!r})"
        )

    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        _record_whisper_failure(f"faster_whisper import failed: {e}")
        raise

    # Detect if CUDA is available for CTranslate2 (separate from torch CUDA)
    device = "cpu"
    compute_type = "int8"
    try:
        import ctranslate2
        if 'cuda' in ctranslate2.get_supported_compute_types('cuda'):
            device = "cuda"
            compute_type = "float16"
            logger.info("CTranslate2 CUDA available — loading faster-whisper on GPU")
    except Exception:
        pass

    logger.info(f"Loading faster-whisper model '{model_size}' on {device} ({compute_type})...")
    try:
        _faster_whisper_model = WhisperModel(
            model_size, device=device, compute_type=compute_type
        )
    except Exception as e:
        reason = f"WhisperModel({model_size}, {device}, {compute_type}) failed: {e}"
        logger.warning(reason)
        _record_whisper_failure(reason)
        raise
    _faster_whisper_model_size = model_size
    logger.info(f"faster-whisper model '{model_size}' loaded on {device}")
    _record_whisper_success()

    # Register with central lifecycle tracker via orchestrator
    try:
        from .model_orchestrator import get_orchestrator
        get_orchestrator().notify_loaded('stt', f'whisper-{model_size}',
                                         device=device, vram_gb=3.0 if device == 'cuda' else 0)
    except Exception:
        pass

    return _faster_whisper_model


def _faster_whisper_transcribe(audio_path: str, language: str = None) -> Optional[str]:
    """Transcribe using faster-whisper. Returns JSON string or None on failure.

    Symptom #10 guard (2026-04-16): each call is gated by the module
    circuit breaker, so after N consecutive load failures we refuse
    silently (one log every cooldown window, not 2Hz spam). The
    backoff is shared with _get_faster_whisper_model so a model-load
    refusal here shortcircuits the full transcribe path too.
    """
    # Fast-path refusal: if breaker is OPEN, skip the whole try block.
    if _whisper_load_breaker is not None and _whisper_load_breaker.is_open():
        return None

    try:
        model = _get_faster_whisper_model(_FASTER_WHISPER_MODEL_SIZE)
    except Exception as e:
        # _get_faster_whisper_model already records the failure +
        # emits one warning. Don't re-log at 2Hz here.
        logger.debug(f"faster-whisper unavailable: {e}")
        return None

    try:
        kwargs = {"beam_size": 5}
        if language:
            kwargs["language"] = language
        segments, info = model.transcribe(audio_path, **kwargs)
        text = " ".join(seg.text for seg in segments).strip()
        _record_whisper_success()
        return json.dumps({
            "text": text,
            "language": info.language if info.language else "unknown",
        })
    except Exception as e:
        reason = f"transcribe({audio_path}) failed: {e}"
        logger.warning(f"faster-whisper transcription failed: {e}")
        _record_whisper_failure(reason)
        return None


# ═══════════════════════════════════════════════════════════════
# Model download (sherpa-onnx)
# ═══════════════════════════════════════════════════════════════

def _get_stt_dir() -> Path:
    """Get the STT model storage directory."""
    from .model_storage import model_storage
    stt_dir = model_storage.get_tool_dir("stt")
    stt_dir.mkdir(parents=True, exist_ok=True)
    return stt_dir


def _download_model(model_name: str) -> Path:
    """Download and extract a sherpa-onnx model if not already present.

    Returns the path to the extracted model directory.
    """
    cfg = _SHERPA_MODELS[model_name]
    stt_dir = _get_stt_dir()
    model_dir = stt_dir / cfg["dir"]

    if model_dir.exists() and (model_dir / cfg["files"]["tokens"]).exists():
        return model_dir

    archive_url = f"{_SHERPA_MODEL_BASE}/{cfg['archive']}"
    archive_path = stt_dir / cfg["archive"]

    logger.info(f"Downloading STT model '{model_name}' from {archive_url}...")
    try:
        urllib.request.urlretrieve(archive_url, str(archive_path))
        logger.info(f"Extracting {cfg['archive']}...")
        with tarfile.open(str(archive_path), "r:bz2") as tar:
            tar.extractall(path=str(stt_dir))
        # Clean up archive
        archive_path.unlink(missing_ok=True)
        logger.info(f"STT model '{model_name}' ready at {model_dir}")
    except Exception as e:
        logger.error(f"Failed to download model '{model_name}': {e}")
        archive_path.unlink(missing_ok=True)
        raise

    return model_dir


# ═══════════════════════════════════════════════════════════════
# sherpa-onnx recognizer creation
# ═══════════════════════════════════════════════════════════════

def _get_sherpa_recognizer(model_name: str = "whisper-tiny"):
    """Create or return cached sherpa-onnx OfflineRecognizer."""
    global _sherpa_recognizer, _sherpa_model_name
    if _sherpa_recognizer is not None and _sherpa_model_name == model_name:
        return _sherpa_recognizer

    import sherpa_onnx

    cfg = _SHERPA_MODELS[model_name]
    model_dir = _download_model(model_name)

    num_threads = min(os.cpu_count() or 2, 4)

    if cfg["type"] == "moonshine":
        _sherpa_recognizer = sherpa_onnx.OfflineRecognizer.from_moonshine(
            preprocessor=str(model_dir / cfg["files"]["preprocessor"]),
            encoder=str(model_dir / cfg["files"]["encoder"]),
            uncached_decoder=str(model_dir / cfg["files"]["uncached_decoder"]),
            cached_decoder=str(model_dir / cfg["files"]["cached_decoder"]),
            tokens=str(model_dir / cfg["files"]["tokens"]),
            num_threads=num_threads,
        )
    elif cfg["type"] == "whisper":
        _sherpa_recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=str(model_dir / cfg["files"]["encoder"]),
            decoder=str(model_dir / cfg["files"]["decoder"]),
            tokens=str(model_dir / cfg["files"]["tokens"]),
            num_threads=num_threads,
        )
    else:
        raise ValueError(f"Unknown model type: {cfg['type']}")

    _sherpa_model_name = model_name
    logger.info(f"sherpa-onnx recognizer ready: {model_name}")
    return _sherpa_recognizer


def _sherpa_transcribe(audio_path: str, model_name: str) -> Optional[str]:
    """Transcribe using sherpa-onnx. Returns JSON string or None on failure."""
    try:
        recognizer = _get_sherpa_recognizer(model_name)
        stream = recognizer.create_stream()
        stream.accept_wave_file(audio_path)
        recognizer.decode_stream(stream)
        text = stream.result.text.strip()

        # Language: Moonshine is English-only, Whisper auto-detects
        cfg = _SHERPA_MODELS.get(model_name, {})
        lang = "en" if not cfg.get("multilingual") else "auto"

        return json.dumps({"text": text, "language": lang})
    except Exception as e:
        logger.warning(f"sherpa-onnx transcription failed ({model_name}): {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# Legacy openai-whisper fallback
# ═══════════════════════════════════════════════════════════════

def _get_whisper_model(model_name: str = "base"):
    """Lazy-load openai-whisper model (fallback if sherpa-onnx unavailable)."""
    global _whisper_model, _whisper_model_name
    if _whisper_model is not None and _whisper_model_name == model_name:
        return _whisper_model

    import whisper

    from .model_storage import model_storage
    model_dir = model_storage.get_tool_dir("whisper")
    model_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("XDG_CACHE_HOME", str(model_dir.parent))
    logger.info(f"Loading openai-whisper model '{model_name}' (fallback)...")
    _whisper_model = whisper.load_model(model_name, download_root=str(model_dir))
    _whisper_model_name = model_name
    logger.info(f"openai-whisper model '{model_name}' loaded")
    return _whisper_model


def _legacy_transcribe(audio_path: str, language: str = None) -> Optional[str]:
    """Transcribe using openai-whisper (fallback). Returns JSON string or None."""
    try:
        model_name = _select_legacy_model()
        model = _get_whisper_model(model_name)
        kwargs = {}
        if language:
            kwargs["language"] = language
        result = model.transcribe(audio_path, **kwargs)
        return json.dumps({
            "text": result["text"].strip(),
            "language": result.get("language", "unknown"),
        })
    except ImportError:
        return None
    except Exception as e:
        logger.warning(f"openai-whisper fallback failed: {e}")
        return None


def _select_legacy_model() -> str:
    """Select openai-whisper model by VRAM (legacy path)."""
    try:
        from .vram_manager import vram_manager
        gpu = vram_manager.detect_gpu()
        if not gpu["cuda_available"]:
            return "base"
        free = vram_manager.get_free_vram()
        if free >= 10:
            return "large-v3"
        elif free >= 5:
            return "medium"
        elif free >= 2:
            return "small"
    except Exception:
        pass
    return "base"


# ═══════════════════════════════════════════════════════════════
# Public API (same interface for all callers)
# ═══════════════════════════════════════════════════════════════

def populate_stt_catalog(catalog) -> int:
    """Register all STT model variants into the ModelCatalog.

    Called by ModelCatalog._populate_stt_models() so the catalog is the
    single source of truth for model names, VRAM requirements, and tier gates.
    Replaces the hardcoded VRAM thresholds in select_whisper_model().

    Returns number of new entries added.
    """
    from integrations.service_tools.model_catalog import ModelEntry, ModelType

    # (id, name, vram_gb, ram_gb, disk_gb, quality, speed, tags, min_tier)
    models = [
        # faster-whisper (primary engine, CTranslate2 INT8)
        ('stt-faster-whisper-tiny',   'Whisper Tiny (faster-whisper)',   0.0, 0.3, 0.15,
         0.60, 0.98, ['multilingual', 'cpu-friendly', 'faster-whisper'], 'lite'),
        ('stt-faster-whisper-base',   'Whisper Base (faster-whisper)',   0.2, 0.5, 0.30,
         0.72, 0.95, ['multilingual', 'cpu-friendly', 'faster-whisper'], 'lite'),
        ('stt-faster-whisper-small',  'Whisper Small (faster-whisper)',  0.5, 1.0, 0.46,
         0.80, 0.85, ['multilingual', 'faster-whisper'], 'lite'),
        ('stt-faster-whisper-medium', 'Whisper Medium (faster-whisper)', 1.5, 2.0, 1.50,
         0.87, 0.72, ['multilingual', 'faster-whisper'], 'standard'),
        ('stt-faster-whisper-large',  'Whisper Large v3 (faster-whisper)', 3.0, 4.0, 3.10,
         0.94, 0.55, ['multilingual', 'faster-whisper'], 'full'),
        # sherpa-onnx (lightweight ONNX, no PyTorch)
        ('stt-sherpa-moonshine-tiny', 'Moonshine Tiny (sherpa-onnx, EN)', 0.0, 0.2, 0.08,
         0.62, 0.99, ['english-only', 'onnx', 'sherpa-onnx', 'cpu-friendly'], 'lite'),
        ('stt-sherpa-moonshine-base', 'Moonshine Base (sherpa-onnx, EN)', 0.0, 0.3, 0.15,
         0.68, 0.96, ['english-only', 'onnx', 'sherpa-onnx', 'cpu-friendly'], 'lite'),
        ('stt-sherpa-whisper-tiny',   'Whisper Tiny (sherpa-onnx)',      0.0, 0.3, 0.15,
         0.61, 0.97, ['multilingual', 'onnx', 'sherpa-onnx', 'cpu-friendly'], 'lite'),
        ('stt-sherpa-whisper-base',   'Whisper Base (sherpa-onnx)',      0.0, 0.4, 0.30,
         0.72, 0.92, ['multilingual', 'onnx', 'sherpa-onnx'], 'lite'),
        ('stt-sherpa-whisper-small',  'Whisper Small (sherpa-onnx)',     0.0, 0.7, 0.46,
         0.79, 0.80, ['multilingual', 'onnx', 'sherpa-onnx'], 'lite'),
        ('stt-sherpa-whisper-medium', 'Whisper Medium (sherpa-onnx)',    0.0, 1.5, 1.50,
         0.86, 0.65, ['multilingual', 'onnx', 'sherpa-onnx'], 'standard'),
    ]

    added = 0
    for (mid, name, vram, ram, disk, quality, speed, tags, min_tier) in models:
        if catalog.get(mid) is not None:
            continue
        entry = ModelEntry(
            id=mid, name=name, model_type=ModelType.STT,
            source='github' if 'sherpa' in mid else 'huggingface',
            vram_gb=vram, ram_gb=ram, disk_gb=disk,
            min_capability_tier=min_tier,
            backend='onnx' if 'sherpa' in mid else 'torch',
            supports_gpu=(vram > 0), supports_cpu=True,
            supports_cpu_offload=False,
            idle_timeout_s=300,
            capabilities={
                'realtime': True,
                'diarization': False,
                'multilingual': ('multilingual' in tags),
            },
            quality_score=quality, speed_score=speed,
            languages=['multilingual'] if 'multilingual' in tags else ['en'],
            tags=tags,
        )
        catalog.register(entry, persist=False)
        added += 1
    return added


# ── Catalog-aware model name → sherpa-onnx key mapping ─────────────────────
_CATALOG_ID_TO_SHERPA = {
    'stt-sherpa-moonshine-tiny': 'moonshine-tiny',
    'stt-sherpa-moonshine-base': 'moonshine-base',
    'stt-sherpa-whisper-tiny':   'whisper-tiny',
    'stt-sherpa-whisper-base':   'whisper-base',
    'stt-sherpa-whisper-small':  'whisper-small',
    'stt-sherpa-whisper-medium': 'whisper-medium',
}

_CATALOG_ID_TO_FASTER_WHISPER_SIZE = {
    'stt-faster-whisper-tiny':   'tiny',
    'stt-faster-whisper-base':   'base',
    'stt-faster-whisper-small':  'small',
    'stt-faster-whisper-medium': 'medium',
    'stt-faster-whisper-large':  'large-v3',
}


def select_whisper_model() -> str:
    """Select best STT model for this hardware.

    Tries ModelCatalog first (single source of truth for VRAM thresholds).
    Falls back to direct VRAM query if catalog is unavailable.

    Returns a sherpa-onnx model key (from _SHERPA_MODELS) when sherpa-onnx
    is available, or an openai-whisper model name as a legacy fallback.
    """
    # ── Primary path: ask the catalog ───────────────────────────────────────
    try:
        from integrations.service_tools.model_orchestrator import get_orchestrator
        orch = get_orchestrator()
        entry = orch.select_best('stt')
        if entry:
            # Map catalog entry ID back to the engine-specific key
            sherpa_key = _CATALOG_ID_TO_SHERPA.get(entry.id)
            if sherpa_key and sherpa_key in _SHERPA_MODELS:
                try:
                    import sherpa_onnx  # noqa: F401
                    return sherpa_key
                except ImportError:
                    pass
            # faster-whisper size
            fw_size = _CATALOG_ID_TO_FASTER_WHISPER_SIZE.get(entry.id)
            if fw_size:
                return fw_size
    except Exception:
        pass

    # ── Fallback: direct VRAM query (no catalog dependency) ─────────────────
    try:
        import sherpa_onnx  # noqa: F401 — check availability
    except ImportError:
        return _select_legacy_model()

    from .vram_manager import vram_manager
    gpu = vram_manager.detect_gpu()

    if gpu["cuda_available"]:
        free = vram_manager.get_free_vram()
        if free >= 5:
            return "whisper-medium"
        elif free >= 2:
            return "whisper-small"
        else:
            return "whisper-base"
    else:
        # CPU-only: prefer Moonshine (fastest) for English,
        # Whisper tiny for multilingual
        # Caller can override with language hint
        return "moonshine-tiny"


# ═══════════════════════════════════════════════════════════════
# Subprocess isolation
# ═══════════════════════════════════════════════════════════════
#
# STT engines (faster-whisper / sherpa-onnx / openai-whisper) all have
# native C runtimes that can crash the parent process on CUDA OOM,
# DLL conflicts, or audio decoder edge cases. Running them in a worker
# subprocess contains those crashes: the worker dies, the parent catches
# the exit code and returns an error JSON without bringing Nunba down.

from .gpu_worker import ToolWorker  # noqa: E402

_stt_tool = ToolWorker(
    tool_name='whisper',
    tool_module='integrations.service_tools.whisper_tool',
    vram_budget='whisper_base',
    output_subdir='stt',   # not used — STT doesn't generate files
    engine='whisper',
    startup_timeout=60.0,
    request_timeout=180.0,  # long audio files can take a while
    idle_timeout=300.0,     # free the model after 5 min idle
)


def _transcribe_impl(audio_path: str, language: str = None) -> str:
    """Transcribe audio — runs inside the worker subprocess.

    Engine priority: faster-whisper → sherpa-onnx → openai-whisper.

    Returns JSON string with 'text' and 'language' keys.
    """
    # 1. Try faster-whisper (preferred — CTranslate2, 4x faster, multilingual)
    try:
        import faster_whisper  # noqa: F401
        result = _faster_whisper_transcribe(audio_path, language)
        if result:
            return result
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"faster-whisper failed, trying fallback: {e}")

    # 2. Try sherpa-onnx (lightweight ONNX, no PyTorch)
    try:
        import sherpa_onnx  # noqa: F401

        model_name = select_whisper_model()

        # If a non-English language is explicitly requested and the selected
        # model is English-only (Moonshine), switch to multilingual Whisper
        cfg = _SHERPA_MODELS.get(model_name, {})
        if language and language != "en" and not cfg.get("multilingual"):
            model_name = "whisper-tiny"

        result = _sherpa_transcribe(audio_path, model_name)
        if result:
            return result
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"sherpa-onnx failed, trying openai-whisper: {e}")

    # 3. Fallback: openai-whisper (PyTorch) — the risky path
    result = _legacy_transcribe(audio_path, language)
    if result:
        return result

    return json.dumps({"error": "No STT engine available (install faster-whisper)"})


def whisper_transcribe(audio_path: str, language: str = None) -> str:
    """Transcribe audio file to text (subprocess-isolated).

    Runs the STT engine chain in a dedicated worker subprocess so that
    CUDA OOM / CTranslate2 crashes / PyTorch DLL segfaults can't kill
    the parent process.

    Args:
        audio_path: Path to audio file (WAV, MP3, WebM, etc.)
        language: Optional language code. Auto-detect if None.

    Returns:
        JSON string with 'text' and 'language' keys.
    """
    result = _stt_tool.call({
        'op': 'transcribe',
        'audio_path': audio_path,
        'language': language,
    })
    if 'error' in result:
        return json.dumps(result)
    return result.get('raw_json', json.dumps(result))


def _detect_language_impl(audio_path: str) -> str:
    """Language detection — runs inside the worker subprocess.

    Returns JSON string with 'language' and 'probability' keys.
    """
    # Try faster-whisper first (has built-in language detection)
    try:
        from faster_whisper import WhisperModel  # noqa: F401
        model = _get_faster_whisper_model(_FASTER_WHISPER_MODEL_SIZE)
        _, info = model.transcribe(audio_path, beam_size=1)
        return json.dumps({
            "language": info.language if info.language else "unknown",
            "probability": round(info.language_probability, 4) if info.language_probability else 0.0,
        })
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"faster-whisper language detection failed: {e}")

    try:
        import whisper
        model = _get_whisper_model()
        audio = whisper.load_audio(audio_path)
        audio = whisper.pad_or_trim(audio)
        mel = whisper.log_mel_spectrogram(audio).to(model.device)
        _, probs = model.detect_language(mel)
        lang = max(probs, key=probs.get)
        return json.dumps({
            "language": lang,
            "probability": round(probs[lang], 4),
        })
    except ImportError:
        # No openai-whisper — transcribe with multilingual Whisper and infer
        try:
            import sherpa_onnx  # noqa: F401
            result = _sherpa_transcribe(audio_path, "whisper-tiny")
            if result:
                parsed = json.loads(result)
                return json.dumps({
                    "language": parsed.get("language", "unknown"),
                    "probability": 0.8,
                })
        except Exception:
            pass
        return json.dumps({"error": "Language detection unavailable"})
    except Exception as e:
        return json.dumps({"error": f"Language detection failed: {e}"})


def whisper_detect_language(audio_path: str) -> str:
    """Detect the language of an audio file (subprocess-isolated)."""
    result = _stt_tool.call({
        'op': 'detect_language',
        'audio_path': audio_path,
    })
    if 'error' in result:
        return json.dumps(result)
    return result.get('raw_json', json.dumps(result))


def unload_whisper():
    """Unload all STT models to free memory.

    The actual models live inside the `_stt_tool` subprocess (see the
    SUBPROCESS ISOLATION section above), so the authoritative unload is
    stopping that worker. We ALSO clear the parent-side globals in case
    a worker-side reference leaked into the parent module state during
    an import.
    """
    # 1. Stop the worker subprocess — this is the real VRAM release.
    try:
        _stt_tool.stop()
    except Exception as e:
        logger.warning(f"failed to stop STT worker: {e}")

    # 2. Also clear parent-side caches (only ever populated in the worker,
    #    but defensive in case something called a legacy helper in-process).
    global _sherpa_recognizer, _sherpa_model_name
    global _whisper_model, _whisper_model_name
    global _faster_whisper_model, _faster_whisper_model_size

    _faster_whisper_model = None
    _faster_whisper_model_size = None
    _sherpa_recognizer = None
    _sherpa_model_name = None
    _whisper_model = None
    _whisper_model_name = None

    from .vram_manager import clear_cuda_cache
    clear_cuda_cache()
    logger.info("STT models unloaded")


# ═══════════════════════════════════════════════════════════════
# Streaming STT WebSocket Server (faster-whisper with VAD)
#
# Pattern: same as diarization_server.py — standalone asyncio WebSocket
# server started as a daemon thread by DiarizationService-style manager.
#
# Protocol:
#   Client → Server: binary PCM16 audio chunks (16kHz mono) OR
#                     binary WebM/Opus blobs (auto-detected, converted via ffmpeg)
#   Server → Client: JSON {"text": "...", "language": "en", "is_final": true/false}
#
# The server accumulates audio in a per-connection buffer. When VAD detects
# a speech pause (or buffer exceeds 30s), it transcribes the buffer with
# faster-whisper and sends back the result. Partial results are sent
# every 2s of accumulated audio for low-latency interim display.
# ═══════════════════════════════════════════════════════════════

_stt_ws_server = None
_stt_ws_port = None

STREAM_SAMPLE_RATE = 16000
STREAM_BYTES_PER_SAMPLE = 2
STREAM_CHANNELS = 1
# Transcribe every 2s of audio for interim results
STREAM_CHUNK_SECONDS = 2
STREAM_CHUNK_BYTES = STREAM_SAMPLE_RATE * STREAM_BYTES_PER_SAMPLE * STREAM_CHANNELS * STREAM_CHUNK_SECONDS
# Max buffer before forced transcription (30s)
STREAM_MAX_BUFFER_BYTES = STREAM_SAMPLE_RATE * STREAM_BYTES_PER_SAMPLE * STREAM_CHANNELS * 30


async def _stt_stream_handler(websocket):
    """Handle a single streaming STT WebSocket connection.

    Accepts:
      - Raw PCM16 16kHz mono binary frames
      - WebM/Opus blobs (auto-converted to PCM via temp file + faster-whisper)
      - JSON {"control": "reset"} to clear buffer
      - JSON {"control": "final"} to force final transcription

    Sends back:
      - {"text": "...", "language": "en", "is_final": false} for interim
      - {"text": "...", "language": "en", "is_final": true} for final (pause detected)

    CRASH ISOLATION:
      - Model crashes are isolated: _transcribe_buffer routes through
        _stt_tool.call() which handles subprocess crashes and returns
        empty strings on failure.
      - Protocol-level code (websocket frames, VAD, buffering) runs
        in the daemon thread with an outer try/except (below) so
        Python exceptions are caught and the connection closes cleanly.
      - Remaining risks are C-level crashes in the websockets library
        or audio decoders — low-probability and would require moving
        the entire server into a subprocess with per-frame IPC, which
        costs realtime latency. Deferred until evidence of actual crashes.
    """
    import io
    import tempfile
    import numpy as np

    audio_buffer = io.BytesIO()
    last_transcribe_size = 0

    try:
        async for message in websocket:
            # Control messages (JSON)
            if isinstance(message, str):
                try:
                    ctrl = json.loads(message)
                    if ctrl.get('control') == 'reset':
                        audio_buffer = io.BytesIO()
                        last_transcribe_size = 0
                        continue
                    if ctrl.get('control') == 'final':
                        # Force final transcription of remaining buffer
                        text, lang = _transcribe_buffer(audio_buffer)
                        if text:
                            await websocket.send(json.dumps({
                                'text': text, 'language': lang, 'is_final': True,
                            }))
                        audio_buffer = io.BytesIO()
                        last_transcribe_size = 0
                        continue
                except (json.JSONDecodeError, ValueError):
                    pass
                continue

            # Binary audio data
            if not isinstance(message, (bytes, bytearray)):
                continue

            # Detect format: WebM/Opus starts with 0x1A45DFA3 (EBML header)
            # or "OggS" (Ogg container). Raw PCM has no header.
            is_container = (
                message[:4] == b'\x1a\x45\xdf\xa3' or  # WebM/Matroska
                message[:4] == b'OggS' or               # Ogg/Opus
                message[:4] == b'RIFF'                   # WAV
            )

            if is_container:
                # Save to temp file, let faster-whisper handle decoding
                pcm_bytes = _container_to_pcm(message)
                if pcm_bytes:
                    audio_buffer.write(pcm_bytes)
            else:
                # Raw PCM16 mono 16kHz
                audio_buffer.write(message)

            buf_size = audio_buffer.getbuffer().nbytes

            # Force transcription if buffer exceeds max
            if buf_size >= STREAM_MAX_BUFFER_BYTES:
                text, lang = _transcribe_buffer(audio_buffer)
                if text:
                    await websocket.send(json.dumps({
                        'text': text, 'language': lang, 'is_final': True,
                    }))
                audio_buffer = io.BytesIO()
                last_transcribe_size = 0
                continue

            # Interim transcription every STREAM_CHUNK_BYTES
            if buf_size - last_transcribe_size >= STREAM_CHUNK_BYTES:
                text, lang = _transcribe_buffer(audio_buffer, keep_buffer=True)
                last_transcribe_size = buf_size
                if text:
                    await websocket.send(json.dumps({
                        'text': text, 'language': lang, 'is_final': False,
                    }))

    except Exception as e:
        logger.debug(f"STT stream connection ended: {e}")


def _container_to_pcm(data: bytes) -> Optional[bytes]:
    """Convert WebM/Opus/WAV container to raw PCM16 16kHz mono via temp file.

    faster-whisper can read any ffmpeg-supported format, so we save to a
    temp file, transcribe, and extract the raw audio. But for streaming we
    need raw PCM — use ffmpeg subprocess if available, else return raw bytes
    and let faster-whisper handle it at transcribe time.
    """
    import subprocess as _sp
    import tempfile

    tmp_in = None
    tmp_out = None
    try:
        tmp_in = tempfile.NamedTemporaryFile(suffix='.webm', delete=False)
        tmp_in.write(data)
        tmp_in.close()

        tmp_out = tempfile.NamedTemporaryFile(suffix='.pcm', delete=False)
        tmp_out.close()

        _kw = dict(capture_output=True, timeout=10)
        if hasattr(_sp, 'CREATE_NO_WINDOW'):
            _kw['creationflags'] = _sp.CREATE_NO_WINDOW

        result = _sp.run([
            'ffmpeg', '-y', '-i', tmp_in.name,
            '-ar', str(STREAM_SAMPLE_RATE), '-ac', '1', '-f', 's16le',
            tmp_out.name,
        ], **_kw)

        if result.returncode == 0:
            with open(tmp_out.name, 'rb') as f:
                return f.read()
    except FileNotFoundError:
        # ffmpeg not available — save raw container bytes,
        # _transcribe_buffer will write to temp file for faster-whisper
        return data
    except Exception as e:
        logger.debug(f"PCM conversion failed: {e}")
    finally:
        for p in (tmp_in, tmp_out):
            if p:
                try:
                    os.unlink(p.name)
                except Exception:
                    pass
    return None


def _transcribe_buffer(audio_buffer, keep_buffer: bool = False) -> tuple:
    """Transcribe accumulated audio buffer via the subprocess STT worker.

    Returns (text, language) tuple.

    Runs through `_stt_tool` so CUDA OOM or faster-whisper/CTranslate2
    crashes on the realtime path only kill the worker subprocess — the
    streaming WebSocket server (and the whole Nunba process) stay alive.
    """
    import tempfile

    buf_bytes = audio_buffer.getvalue()
    if len(buf_bytes) < STREAM_SAMPLE_RATE * 2:  # < 1s of audio, skip
        return ('', 'unknown')

    if not keep_buffer:
        audio_buffer.seek(0)
        audio_buffer.truncate(0)

    # Write PCM to a temp WAV file — the subprocess worker reads by
    # path (simpler than shipping raw bytes over JSON). For realtime
    # workloads this is still cheap (local disk, a few KB per chunk).
    tmp = None
    try:
        import wave
        tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        with wave.open(tmp.name, 'wb') as wf:
            wf.setnchannels(STREAM_CHANNELS)
            wf.setsampwidth(STREAM_BYTES_PER_SAMPLE)
            wf.setframerate(STREAM_SAMPLE_RATE)
            wf.writeframes(buf_bytes)
        tmp.close()

        result = _stt_tool.call({
            'op': 'transcribe',
            'audio_path': tmp.name,
            'language': None,
        })
        if 'error' in result and not result.get('raw_json'):
            logger.debug(f"Streaming transcribe failed: {result.get('error')}")
            return ('', 'unknown')
        raw = result.get('raw_json') or json.dumps(result)
        try:
            parsed = json.loads(raw)
            return (parsed.get('text', ''), parsed.get('language', 'unknown'))
        except json.JSONDecodeError:
            return ('', 'unknown')
    except Exception as e:
        logger.debug(f"Streaming transcribe failed: {e}")
        return ('', 'unknown')
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass


def start_stt_stream_server(port: int = 0) -> Optional[int]:
    """Start the streaming STT WebSocket server in a daemon thread.

    Same pattern as DiarizationService — asyncio event loop in a thread.

    Args:
        port: Port to bind (0 = auto-select from port registry or dynamic)

    Returns:
        Actual port number, or None if failed.
    """
    global _stt_ws_server, _stt_ws_port

    if _stt_ws_port is not None:
        return _stt_ws_port  # already running

    if port == 0:
        try:
            from core.port_registry import get_port
            port = get_port('stt_stream')
        except Exception:
            port = 8005  # default fallback

    import asyncio
    import threading

    def _run_server():
        global _stt_ws_server, _stt_ws_port
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            import websockets

            async def _serve():
                global _stt_ws_server, _stt_ws_port
                server = await websockets.serve(
                    _stt_stream_handler, '127.0.0.1', port,
                    max_size=2 * 1024 * 1024,  # 2MB max message (30s audio ~960KB)
                )
                actual_port = port
                if server.sockets:
                    actual_port = server.sockets[0].getsockname()[1]
                _stt_ws_server = server
                _stt_ws_port = actual_port
                logger.info(f"Streaming STT WebSocket server on ws://127.0.0.1:{actual_port}")
                await asyncio.Future()  # run forever

            loop.run_until_complete(_serve())
        except Exception as e:
            logger.error(f"STT stream server failed: {e}")
            _stt_ws_port = None

    thread = threading.Thread(target=_run_server, daemon=True, name='stt-stream-ws')
    thread.start()

    # Wait for port to be assigned
    import time
    for _ in range(30):
        if _stt_ws_port is not None:
            return _stt_ws_port
        time.sleep(0.1)

    logger.warning("STT stream server did not start within 3s")
    return None


def get_stt_stream_port() -> Optional[int]:
    """Get the port of the running streaming STT WebSocket server."""
    return _stt_ws_port


# ═══════════════════════════════════════════════════════════════
# Service tool registration
# ═══════════════════════════════════════════════════════════════

class WhisperTool:
    """Register STT as an in-process service tool.

    Unlike other tools, STT runs in-process (no sidecar server).
    The tool functions are registered directly as callables.
    """

    @classmethod
    def register_functions(cls):
        """Register STT functions directly with service_tool_registry."""
        whisper_transcribe.__name__ = "whisper_transcribe"
        whisper_transcribe.__doc__ = (
            "Transcribe audio file to text using STT. "
            "Input: audio_path (string path to WAV/MP3/WebM file), "
            "language (optional language code like 'en'). "
            "Returns JSON with 'text' and 'language'."
        )

        whisper_detect_language.__name__ = "whisper_detect_language"
        whisper_detect_language.__doc__ = (
            "Detect the language spoken in an audio file. "
            "Input: audio_path (string path to audio file). "
            "Returns JSON with 'language' code and 'probability'."
        )

        tool_info = ServiceToolInfo(
            name="whisper",
            description=(
                "Speech-to-text transcription. Converts audio files to text "
                "using sherpa-onnx (Moonshine/Whisper ONNX) or OpenAI Whisper. "
                "Supports 100+ languages with automatic language detection."
            ),
            base_url="inprocess://whisper",
            endpoints={
                "transcribe": {
                    "path": "/transcribe",
                    "method": "POST",
                    "description": whisper_transcribe.__doc__,
                    "params_schema": {
                        "audio_path": {"type": "string", "description": "Path to audio file"},
                        "language": {"type": "string", "description": "Language code (optional)"},
                    },
                },
                "detect_language": {
                    "path": "/detect_language",
                    "method": "POST",
                    "description": whisper_detect_language.__doc__,
                    "params_schema": {
                        "audio_path": {"type": "string", "description": "Path to audio file"},
                    },
                },
            },
            health_endpoint="/health",
            tags=["stt", "speech", "transcription", "audio", "whisper", "sherpa-onnx"],
            timeout=60,
        )
        tool_info.is_healthy = True
        service_tool_registry._tools["whisper"] = tool_info
        return True


# ═══════════════════════════════════════════════════════════════
# Worker callbacks (picked up by the centralized gpu_worker dispatcher)
# ═══════════════════════════════════════════════════════════════
#
# The STT worker has no upfront load — each transcribe call lazy-
# initializes the appropriate engine (faster-whisper / sherpa / legacy)
# inside the subprocess. `_load` is a no-op; `_synthesize` dispatches
# on the request op so one worker handles transcribe + detect_language.

def _load():
    """No upfront load — engines lazy-initialize per request."""
    return None


def _synthesize(_model, req: dict) -> dict:
    """Dispatch STT requests inside the worker subprocess."""
    op = req.get('op', 'transcribe')
    if op == 'transcribe':
        raw = _transcribe_impl(req.get('audio_path'), req.get('language'))
    elif op == 'detect_language':
        raw = _detect_language_impl(req.get('audio_path'))
    else:
        return {'error': f'Unknown op: {op}'}
    # Return both the raw JSON (for pass-through) and parsed fields
    try:
        return {'raw_json': raw, **json.loads(raw)}
    except json.JSONDecodeError:
        return {'error': f'Invalid engine response: {raw[:200]}'}

# NOTE: no `if __name__ == '__main__':` block — the centralized
# dispatcher at integrations.service_tools.gpu_worker imports this
# module and calls _load / _synthesize on spawn.
