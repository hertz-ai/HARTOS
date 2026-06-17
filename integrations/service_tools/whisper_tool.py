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

import array
import json
import logging
import math
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

    # Detect if CUDA is available for CTranslate2 (separate from torch CUDA).
    #
    # CTranslate2 is the engine faster-whisper actually runs on, so its
    # supported-compute-types probe is the AUTHORITATIVE GPU gate — torch
    # CUDA being present is neither necessary nor sufficient.  When the probe
    # says no CUDA, we fall back to CPU int8 AND emit ONE clear warning naming
    # WHY, so an operator on a CUDA box (e.g. RTX 3070) immediately sees that
    # the GPU isn't engaged and what to install — instead of only the bare
    # INFO "loaded on cpu" that today gives no actionable signal.
    device = "cpu"
    compute_type = "int8"
    _cuda_reason = ""
    try:
        import ctranslate2
        if 'cuda' in ctranslate2.get_supported_compute_types('cuda'):
            device = "cuda"
            compute_type = "float16"
            logger.info("CTranslate2 CUDA available — loading faster-whisper on GPU")
        else:
            _cuda_reason = (
                "ctranslate2 reports no CUDA compute types "
                "(CPU-only ctranslate2 build, or no NVIDIA driver/runtime)"
            )
    except ImportError as e:
        _cuda_reason = f"ctranslate2 not importable ({e})"
    except Exception as e:
        # get_supported_compute_types can raise on a broken CUDA runtime;
        # treat as CPU and surface the reason rather than silently swallowing.
        _cuda_reason = f"ctranslate2 CUDA probe failed ({e})"

    if device == "cpu":
        logger.warning(
            "ctranslate2 CUDA not available — STT on CPU (int8); %s. "
            "Install the ctranslate2 CUDA build for GPU whisper "
            "(see whisper_tool GPU-install note).",
            _cuda_reason or "reason unknown",
        )

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


# ── Anti-hallucination gate: "did a human actually speak, or is this noise?" ──
# faster-whisper's vad_filter strips silent AUDIO regions before decoding, but a
# short noise/music burst that VAD mis-reads as speech still decodes to a
# hallucinated phrase (the classic "さて、さて、もみ" / "Thank you for watching" on
# near-silence — submitted as a chat message, it makes the model reply in the
# hallucinated language).  The reliable post-decode signals are the per-segment
# no_speech_prob (P[this window is non-speech]) and avg_logprob (token
# confidence).  These are Whisper's OWN thresholds, so the gate matches the
# model's internal silence definition rather than inventing new numbers.
NO_SPEECH_PROB_MAX = 0.6   # openai-whisper's default no_speech_threshold
AVG_LOGPROB_MIN = -1.0     # openai-whisper's default logprob_threshold


def _filter_speech_text(segments) -> str:
    """Join only the segments that are real speech; drop silence/noise.

    ``segments`` is an iterable of ``(text, no_speech_prob, avg_logprob)``.
    Single source for the anti-hallucination gate shared by the faster-whisper
    and openai-whisper (legacy) transcribe paths — no parallel filter.  A
    segment is kept only when BOTH signals look like speech; a missing signal
    (None) is treated as speech so we never over-drop when a backend doesn't
    report it.
    """
    kept = []
    for text, no_speech_prob, avg_logprob in segments:
        nsp = no_speech_prob if no_speech_prob is not None else 0.0
        alp = avg_logprob if avg_logprob is not None else 0.0
        if nsp <= NO_SPEECH_PROB_MAX and alp >= AVG_LOGPROB_MIN:
            kept.append((text or '').strip())
    return " ".join(t for t in kept if t).strip()


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
        # Anti-hallucination params (fixes the "1.5% 1.5% 1.5%…" repetition
        # loop on silence/non-speech, reported 2026-06-12):
        #   - vad_filter=True → Silero VAD strips non-speech BEFORE decoding,
        #     so a silent/noise window transcribes to '' instead of a
        #     hallucinated repeated token. This is the #1 fix.
        #   - condition_on_previous_text=False → don't feed the model its own
        #     prior output back; that feedback is what makes whisper get stuck
        #     repeating a token in an autoregressive loop.
        # Matters most on the realtime streaming path, where a bounded window
        # is re-decoded every 2s and frequently contains gaps/silence.
        kwargs = {
            "beam_size": 5,
            "vad_filter": True,
            "condition_on_previous_text": False,
        }
        if language:
            kwargs["language"] = language
        segments, info = model.transcribe(audio_path, **kwargs)
        # Speech-only join: drop silence/noise hallucinations via the shared
        # no_speech_prob/avg_logprob gate (vad_filter alone still lets a short
        # noise burst decode to a hallucinated phrase).
        text = _filter_speech_text(
            (seg.text, getattr(seg, 'no_speech_prob', None),
             getattr(seg, 'avg_logprob', None))
            for seg in segments
        )
        _record_whisper_success()
        return json.dumps({
            "text": text,
            # Nothing survived the speech gate → the window was noise/silence.
            # Report 'unknown', not the language Whisper hallucinated from the
            # noise (fixes wrong-language replies to non-speech audio).
            "language": (info.language if (text and info.language) else "unknown"),
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
        # Same anti-hallucination guard as the faster-whisper path. openai-
        # whisper has no vad_filter, but condition_on_previous_text=False is
        # the key lever that breaks the repeat-on-silence loop.
        kwargs = {"condition_on_previous_text": False}
        if language:
            kwargs["language"] = language
        result = model.transcribe(audio_path, **kwargs)
        # Same speech-only gate as the faster-whisper path (shared helper).
        # openai-whisper exposes per-segment no_speech_prob/avg_logprob in
        # result['segments']; if segments are absent, fall back to raw text.
        segs = result.get("segments") or []
        if segs:
            text = _filter_speech_text(
                (s.get("text", ""), s.get("no_speech_prob"), s.get("avg_logprob"))
                for s in segs
            )
        else:
            text = (result.get("text") or "").strip()
        return json.dumps({
            "text": text,
            "language": (result.get("language", "unknown") if text else "unknown"),
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
# Realtime guard: interim results re-decode only the most-recent N seconds of
# audio, NOT the whole accumulated buffer.  Without this bound, each interim
# pass at t=Ns re-transcribes all N seconds — O(n²) total work over an
# utterance, so latency grows the longer the user speaks (the live symptom:
# "responding but VERY DELAYED").  Capping the interim window makes per-interim
# cost flat (O(window)) regardless of utterance length.  The FINAL pass (control
# 'final' + MAX_BUFFER force-flush) still decodes the full buffer for accuracy.
STREAM_INTERIM_WINDOW_SECONDS = 6
STREAM_INTERIM_WINDOW_BYTES = (
    STREAM_SAMPLE_RATE * STREAM_BYTES_PER_SAMPLE * STREAM_CHANNELS
    * STREAM_INTERIM_WINDOW_SECONDS
)

# ── Server-side VAD pause-finalization (#131) ────────────────────────
# The streaming server finalizes on three signals: an explicit client
# {control:final}, a 30s max-buffer flush, OR — added here — a detected
# end-of-utterance silence.  The first relied entirely on the client being
# smart enough to send {control:final}; a raw-PCM feeder (or a client that
# never signals end-of-speech) would otherwise only ever finalize on the 30s
# overflow.  Energy-based silence detection makes finalization robust for ANY
# client without changing the client-driven path (both still fire).
STREAM_VAD_RMS_THRESHOLD = 400      # PCM16 RMS below this == silence (speech ~1-5k)
STREAM_VAD_SILENCE_MS = 800         # trailing silence after speech that ends an utterance
STREAM_VAD_MIN_SPEECH_MS = 300      # require this much speech first (ignore leading silence)
# Cap the per-chunk RMS sum to a bounded tail so the GIL-held Python loop on
# the async STT handler doesn't scale with chunk size (~100ms @ 16kHz).
STREAM_RMS_MAX_SAMPLES = int(os.environ.get('HEVOLVE_STT_RMS_MAX_SAMPLES', '1600'))


def _pcm_rms(pcm: bytes) -> float:
    """Root-mean-square amplitude of PCM16LE mono ``pcm`` (0.0 == digital silence).

    Pure + stdlib-only (``array``) so ``_StreamVadGate`` stays unit-testable
    without numpy / faster-whisper loaded.  Single source for "how loud is this
    chunk" — the handler calls this, not its own inline RMS.
    """
    n = len(pcm) - (len(pcm) % 2)
    if n <= 0:
        return 0.0
    samples = array.array('h')
    samples.frombytes(pcm[:n])
    if not samples:
        return 0.0
    # Bound the GIL-held sum to a recent tail — end-of-utterance VAD only needs
    # recent energy, and an unbounded per-chunk loop on the async handler would
    # grow with chunk size.
    if len(samples) > STREAM_RMS_MAX_SAMPLES:
        samples = samples[-STREAM_RMS_MAX_SAMPLES:]
    acc = 0
    for s in samples:
        acc += s * s
    return math.sqrt(acc / len(samples))


class _StreamVadGate:
    """Energy-based end-of-utterance detector for the streaming STT server.

    Pure decision logic (no I/O, no model) so it is unit-testable in isolation:
    feed each chunk's RMS energy + duration; ``update`` returns True exactly
    once per utterance — when enough speech has been seen AND a trailing
    silence gap then exceeds the threshold.  Fires once, then re-arms on the
    next speech (auto-reset), so continued silence does not re-fire.
    """

    def __init__(self, rms_threshold: float = STREAM_VAD_RMS_THRESHOLD,
                 silence_ms: float = STREAM_VAD_SILENCE_MS,
                 min_speech_ms: float = STREAM_VAD_MIN_SPEECH_MS):
        self.rms_threshold = rms_threshold
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self._speech_ms = 0.0
        self._silence_ms = 0.0

    def update(self, rms: float, chunk_ms: float) -> bool:
        """Feed one audio chunk. Returns True iff this chunk completes an
        end-of-utterance pause (speech seen, then silence ≥ threshold)."""
        if rms >= self.rms_threshold:
            self._speech_ms += chunk_ms
            self._silence_ms = 0.0
            return False
        # Silence chunk — only counts once we've heard enough speech, so
        # leading/standalone silence never finalizes an empty buffer.
        if self._speech_ms < self.min_speech_ms:
            return False
        self._silence_ms += chunk_ms
        if self._silence_ms >= self.silence_ms:
            self.reset()  # one final per utterance; next speech re-arms
            return True
        return False

    def reset(self):
        self._speech_ms = 0.0
        self._silence_ms = 0.0


async def _emit_final(websocket, audio_buffer, stt_lang, call_id, user_id) -> bool:
    """Transcribe the FULL accumulated buffer and emit one final result.

    Single source for finalization, shared by the {control:final}, 30s
    max-buffer flush, and VAD pause-finalize paths so all three behave
    identically (no parallel finalization logic — #131 Gate-2/4).  Returns
    whether a non-empty transcription was sent.  Caller resets the buffer +
    VAD gate (those are caller-local).
    """
    text, lang = _transcribe_buffer(audio_buffer, language=stt_lang)
    if text:
        await websocket.send(json.dumps({
            'text': text, 'language': lang, 'is_final': True,
        }))
        _maybe_enqueue_call_segment(call_id, user_id, text, lang, True)
    return bool(text)


def _ws_path(websocket) -> str:
    """Best-effort URL-path extractor across websockets lib versions.

    websockets 11+ moved the request to ``websocket.request.path``;
    earlier versions exposed ``websocket.path`` directly.  We probe
    both and fall back to empty string when neither is available.
    Never raises.
    """
    for getter in (
        lambda ws: ws.request.path,
        lambda ws: ws.path,
    ):
        try:
            val = getter(websocket)
            if isinstance(val, str):
                return val
        except Exception:
            continue
    return ''


def _parse_call_context(ws_path: str) -> tuple:
    """Parse ``?call_id=<id>&user_id=<u>`` from a WS request path.

    UNIF-G7 / W1.7 Producer C — the RN mic stream (and any other
    browser/mobile audio source) opens the streaming-STT WebSocket
    with these query params attached when the audio belongs to a
    voice room.  Without the params, behavior is unchanged (today's
    one-shot transcription clients still work).

    Returns ``(call_id, user_id)`` — either may be ``None``.  Never
    raises.
    """
    if not ws_path:
        return (None, None)
    from urllib.parse import urlparse, parse_qs
    try:
        parsed = urlparse(ws_path)
        qs = parse_qs(parsed.query or '')
        call_id = (qs.get('call_id') or [None])[0]
        user_id = (qs.get('user_id') or [None])[0]
        return (call_id or None, user_id or None)
    except Exception:
        return (None, None)


def _maybe_enqueue_call_segment(
    call_id: Optional[str],
    user_id: Optional[str],
    text: str,
    lang: str,
    is_final: bool,
) -> None:
    """If the WS client opted in via ``?call_id=`` AND this is a final
    segment, land it in the canonical per-call queue so the
    AgentBridgeWorker can drain it (UNIF-G3 / W1.2 consumer).

    Single canonical writer for browser/mobile-mic-driven transcripts —
    same sink as the future Discord-voice-recv (Producer A) and
    LiveKit-RTC (Producer B) audio paths.  No parallel queue.

    Best-effort: never raises out of the WS handler hot path.
    """
    if not call_id or not is_final or not text:
        return
    try:
        enqueue_stt_segment(call_id, {
            'text': text,
            'lang': lang,
            'author_id': user_id or 'unknown',
            'is_final': True,
            # t0/t1/speaker stay None — RN mic stream is single-speaker
            # by definition (the user typing into the SPA); future
            # multi-speaker producers will set speaker.
        })
    except Exception as e:
        logger.debug(
            "whisper_tool._maybe_enqueue_call_segment failed "
            "(call=%s): %s", call_id, e)


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

    Optional UNIF-G7 hook (Producer C):
      The connection URL MAY include ``?call_id=<id>&user_id=<u>``.
      When present, every final segment is ALSO landed in the per-call
      STT queue (whisper_tool.enqueue_stt_segment) so the
      AgentBridgeWorker can drain it and emit the meet_copilot card.
      Absence of the params preserves today's behavior exactly — RN
      one-shot transcription clients are unaffected.

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
    # Server-side VAD: auto-finalizes on a detected end-of-utterance silence
    # so finalization no longer depends solely on the client sending
    # {control:final} (#131).  Re-arms after each final.
    vad = _StreamVadGate()
    # Connection-level forced language, honoured from a {"type":"config",
    # "language":"<code>"} control message (the docstring's promise — the code
    # previously ignored it).  None => faster-whisper auto-detects per utterance.
    stt_lang = None
    # UNIF-G7 Producer C: extract optional call context from the
    # WS request path.  When absent (call_id is None), the
    # _maybe_enqueue_call_segment helper degrades to a no-op so plain
    # transcription clients see ZERO behavior change.
    call_id, user_id = _parse_call_context(_ws_path(websocket))

    try:
        async for message in websocket:
            # Control messages (JSON)
            if isinstance(message, str):
                try:
                    ctrl = json.loads(message)
                    if ctrl.get('control') == 'reset':
                        audio_buffer = io.BytesIO()
                        last_transcribe_size = 0
                        vad.reset()
                        continue
                    if ctrl.get('control') == 'final':
                        # Force final transcription of remaining buffer
                        await _emit_final(
                            websocket, audio_buffer, stt_lang, call_id, user_id)
                        audio_buffer = io.BytesIO()
                        last_transcribe_size = 0
                        vad.reset()
                        continue
                    if ctrl.get('type') == 'config':
                        # Honour the documented {type:config, language} message:
                        # set the forced language once for this connection.
                        _cfg_lang = ctrl.get('language')
                        if _cfg_lang:
                            stt_lang = _cfg_lang
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
                chunk_pcm = _container_to_pcm(message) or b''
            else:
                # Raw PCM16 mono 16kHz
                chunk_pcm = bytes(message)
            if chunk_pcm:
                audio_buffer.write(chunk_pcm)

            buf_size = audio_buffer.getbuffer().nbytes

            # Force transcription if buffer exceeds max
            if buf_size >= STREAM_MAX_BUFFER_BYTES:
                await _emit_final(
                    websocket, audio_buffer, stt_lang, call_id, user_id)
                audio_buffer = io.BytesIO()
                last_transcribe_size = 0
                vad.reset()
                continue

            # VAD pause-finalize: when this chunk completes an end-of-utterance
            # silence, flush a final immediately (don't wait for the client's
            # {control:final} or the 30s overflow).  Energy on the decoded PCM
            # of THIS chunk; chunk_ms = bytes / (16kHz * 2 bytes/ms).
            #
            # GATED to continuous voice-room streams (call_id present).  The
            # push-to-talk chat mic (call_id=None) finalizes client-side via
            # {control:final} on mic-release; the client fires onResult (which
            # submits) on every is_final, so an auto-final on a mid-utterance
            # thinking pause would split one utterance into two chat
            # submissions.  Voice rooms WANT pause segmentation (one
            # conversational turn per pause) — so VAD runs only there, leaving
            # the chat-mic flow's behavior unchanged.
            if call_id and chunk_pcm:
                chunk_ms = len(chunk_pcm) / (
                    STREAM_SAMPLE_RATE * STREAM_BYTES_PER_SAMPLE / 1000.0)
                if vad.update(_pcm_rms(chunk_pcm), chunk_ms):
                    await _emit_final(
                        websocket, audio_buffer, stt_lang, call_id, user_id)
                    audio_buffer = io.BytesIO()
                    last_transcribe_size = 0
                    continue

            # Interim transcription every STREAM_CHUNK_BYTES.
            #
            # Realtime fix: decode ONLY the most-recent
            # STREAM_INTERIM_WINDOW_BYTES of audio, not the whole buffer
            # from t=0.  This caps per-interim cost to O(window) so latency
            # stays flat no matter how long the user has been speaking.  The
            # accumulating ``audio_buffer`` is left untouched (the FINAL pass
            # still decodes the full utterance for accuracy); we transcribe a
            # throwaway BytesIO holding just the tail window.
            if buf_size - last_transcribe_size >= STREAM_CHUNK_BYTES:
                interim_buf = _tail_window_buffer(
                    audio_buffer, STREAM_INTERIM_WINDOW_BYTES)
                text, lang = _transcribe_buffer(interim_buf, keep_buffer=True, language=stt_lang)
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


def _tail_window_buffer(audio_buffer, window_bytes: int):
    """Return a fresh BytesIO holding only the last ``window_bytes`` of audio.

    Used by the interim transcription path to bound re-decode cost: instead of
    re-transcribing the entire accumulated utterance every chunk (O(n²) over
    the utterance), we decode just the recent window (O(window)).

    The source ``audio_buffer`` is read non-destructively — its position and
    contents are unchanged, so ongoing accumulation in the WS handler is not
    corrupted.

    Audio at this stage is raw PCM16 mono 16kHz (already decoded), so a byte
    tail == a time tail.  The slice is aligned DOWN to an even
    ``STREAM_BYTES_PER_SAMPLE`` boundary so we never split a sample frame.
    """
    import io
    data = audio_buffer.getvalue()
    if window_bytes <= 0 or len(data) <= window_bytes:
        tail = data
    else:
        start = len(data) - window_bytes
        # Align to a whole-sample boundary so we don't slice mid-sample.
        start -= start % STREAM_BYTES_PER_SAMPLE
        tail = data[start:]
    return io.BytesIO(tail)


def _transcribe_buffer(audio_buffer, keep_buffer: bool = False,
                       language: Optional[str] = None) -> tuple:
    """Transcribe accumulated audio buffer via the subprocess STT worker.

    Returns (text, language) tuple.  ``language`` is an optional forced
    language code (from the stream's {type:config} message); None lets
    faster-whisper auto-detect per utterance.

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
            'language': language,
        })
        if 'error' in result and not result.get('raw_json'):
            logger.warning(f"Streaming STT transcribe failed (returning empty text): {result.get('error')}")
            return ('', 'unknown')
        raw = result.get('raw_json') or json.dumps(result)
        try:
            parsed = json.loads(raw)
            return (parsed.get('text', ''), parsed.get('language', 'unknown'))
        except json.JSONDecodeError:
            return ('', 'unknown')
    except Exception as e:
        logger.warning(f"Streaming STT transcribe failed (returning empty text): {e}", exc_info=True)
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

    # Surface STT-engine availability LOUDLY at startup. A missing engine is why
    # streaming STT silently returned '' "for so long" — the per-transcribe
    # failure was only logged at debug (below). sherpa-onnx is primary,
    # openai-whisper the fallback. find_spec checks importability without the
    # cost of importing.
    try:
        import importlib.util as _ilu
        if _ilu.find_spec('sherpa_onnx') is None:
            _legacy_ok = _ilu.find_spec('whisper') is not None
            logger.error(
                "STT engine NOT installed: sherpa-onnx is missing%s. The :8005 "
                "streaming STT server will bind but EVERY transcribe returns '' "
                "(empty) — add sherpa-onnx>=1.11.0 (+ onnxruntime) to the bundle "
                "deps.",
                "" if _legacy_ok else
                " AND the openai-whisper fallback is also missing")
    except Exception:
        pass

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
# Per-call STT segment queue (UNIF-G3 / W1.2)
# ═══════════════════════════════════════════════════════════════
#
# When a call has subscribers (LiveKit room, Discord voice channel,
# Teams meet, etc.), an audio-frame producer feeds frames through the
# streaming STT WebSocket above and receives ``{text, language,
# is_final}`` events back.  ``enqueue_stt_segment`` is the canonical
# place to land FINAL segments so the AgentBridgeWorker (which doesn't
# care which adapter produced the audio) can drain them via
# ``dequeue_segments`` from its tick loop.
#
# This is the single canonical home for STT-segment buffering — every
# audio-source adapter (LiveKit subscriber, Discord voice receiver,
# RN mic stream) lands segments here; every consumer (agent_voice_bridge
# worker, transcript recorder) reads here.  No parallel queues.

import threading
from collections import deque
from typing import Any, Dict, List, Tuple

# Per-call queues of (segment_id, segment_dict) tuples.  Bounded by the
# bridge worker drain cadence (~250ms) so unbounded growth is a bug
# elsewhere; we still keep a soft cap to defend against producer leaks.
_STT_SEGMENT_QUEUE: Dict[str, deque] = {}
_STT_SEGMENT_NEXT_ID: Dict[str, int] = {}
_STT_SEGMENT_LOCK = threading.Lock()
_STT_SEGMENT_CAP_PER_CALL = 1024  # segments; older are evicted with WARN


def enqueue_stt_segment(call_id: str, segment: Dict[str, Any]) -> int:
    """Append a final STT segment for ``call_id``.

    Producer-side: any audio-adapter that has decoded a final transcript
    chunk calls this.  ``segment`` SHOULD include:
      - ``text``    : transcript text
      - ``lang``    : detected language (BCP-47-ish)
      - ``t0``,``t1``: float seconds (segment span on the call timeline)
      - ``speaker`` : optional speaker id / name (None ⇒ unknown)
      - ``author_id``: caller-supplied participant identifier — used by
                       the consumer to skip self-authored segments
      - ``is_final``: optional, defaults True on enqueue (interim
                       segments don't belong here)

    Returns the assigned segment_id (monotonic int per call) so the
    producer can correlate downstream events.

    Best-effort: never raises.  Caller's ``call_id`` is required.
    """
    if not call_id:
        return -1
    seg = dict(segment or {})
    seg.setdefault('is_final', True)
    with _STT_SEGMENT_LOCK:
        next_id = _STT_SEGMENT_NEXT_ID.get(call_id, 0) + 1
        _STT_SEGMENT_NEXT_ID[call_id] = next_id
        seg['segment_id'] = next_id
        q = _STT_SEGMENT_QUEUE.setdefault(call_id, deque())
        q.append((next_id, seg))
        # Evict oldest if soft cap exceeded — defends against a leaked
        # producer that never has its consumer attach.  Real flows
        # drain at 250ms cadence so this should never fire.
        while len(q) > _STT_SEGMENT_CAP_PER_CALL:
            _evicted = q.popleft()
            logger.warning(
                "whisper_tool.enqueue_stt_segment: call=%s queue cap "
                "%d exceeded; evicted seg_id=%s",
                call_id, _STT_SEGMENT_CAP_PER_CALL, _evicted[0])
    return next_id


def dequeue_segments(
    call_id: str,
    since: int | None = None,
) -> List[Dict[str, Any]]:
    """Drain final STT segments for ``call_id`` newer than ``since``.

    Consumer-side: the agent_voice_bridge worker calls this on each
    tick.  Returns segments in arrival order (FIFO).  Each returned
    dict carries the ``segment_id`` the producer was given so the
    caller can update its ``since`` watermark for the next tick.

    ``since=None`` returns all queued segments and resets the queue
    for that call.  ``since=N`` returns only segments with id > N
    AND prunes those segments from the queue.

    Best-effort: missing call_id → ``[]``.
    """
    if not call_id:
        return []
    with _STT_SEGMENT_LOCK:
        q = _STT_SEGMENT_QUEUE.get(call_id)
        if not q:
            return []
        if since is None:
            drained = [seg for (_sid, seg) in q]
            q.clear()
            return drained
        # Prune everything ≤ since, return everything > since.
        drained: List[Dict[str, Any]] = []
        # Iterate from left; any item with id ≤ since is consumed but
        # not returned (already-acked).  Items > since are returned
        # AND removed (so the next dequeue with the same since is a
        # no-op, but normally callers update their watermark).
        keep: deque = deque()
        for sid, seg in q:
            if sid > since:
                drained.append(seg)
            # else: already acked; drop
        # Replace the queue contents with whatever's still > since
        # but un-drained (none currently — we drained ALL items > since).
        # Future-proofing for partial drains: keep is empty here, but
        # the structure makes the intent explicit.
        q.clear()
        q.extend(keep)
        return drained


def reset_stt_segment_queue(call_id: str) -> None:
    """Drop all queued segments for a call (called on detach / hangup).

    Best-effort: missing call_id is a no-op.
    """
    if not call_id:
        return
    with _STT_SEGMENT_LOCK:
        _STT_SEGMENT_QUEUE.pop(call_id, None)
        _STT_SEGMENT_NEXT_ID.pop(call_id, None)


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
