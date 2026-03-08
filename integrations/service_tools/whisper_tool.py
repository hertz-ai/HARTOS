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


# ═══════════════════════════════════════════════════════════════
# faster-whisper (primary engine)
# ═══════════════════════════════════════════════════════════════

_FASTER_WHISPER_MODEL_SIZE = "base"  # CPU int8 — preserves GPU VRAM for TTS/VLM


def _get_faster_whisper_model(model_size: str = "base"):
    """Lazy-load faster-whisper model (CTranslate2, CPU int8, auto-downloads from HuggingFace)."""
    global _faster_whisper_model, _faster_whisper_model_size
    if _faster_whisper_model is not None and _faster_whisper_model_size == model_size:
        return _faster_whisper_model

    from faster_whisper import WhisperModel

    logger.info(f"Loading faster-whisper model '{model_size}' on cpu (int8)...")
    _faster_whisper_model = WhisperModel(
        model_size, device="cpu", compute_type="int8"
    )
    _faster_whisper_model_size = model_size
    logger.info(f"faster-whisper model '{model_size}' loaded")
    return _faster_whisper_model


def _faster_whisper_transcribe(audio_path: str, language: str = None) -> Optional[str]:
    """Transcribe using faster-whisper. Returns JSON string or None on failure."""
    try:
        model = _get_faster_whisper_model(_FASTER_WHISPER_MODEL_SIZE)
        kwargs = {"beam_size": 5}
        if language:
            kwargs["language"] = language
        segments, info = model.transcribe(audio_path, **kwargs)
        text = " ".join(seg.text for seg in segments).strip()
        return json.dumps({
            "text": text,
            "language": info.language if info.language else "unknown",
        })
    except Exception as e:
        logger.warning(f"faster-whisper transcription failed: {e}")
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

def select_whisper_model() -> str:
    """Select best sherpa-onnx STT model for this hardware (fallback path).

    Returns a model name from _SHERPA_MODELS if sherpa-onnx is available,
    otherwise falls back to openai-whisper model names.
    Primary engine is faster-whisper (base, CPU int8).
    """
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


def whisper_transcribe(audio_path: str, language: str = None) -> str:
    """Transcribe audio file to text.

    Engine priority: faster-whisper → sherpa-onnx → openai-whisper.

    Args:
        audio_path: Path to audio file (WAV, MP3, WebM, etc.)
        language: Optional language code (e.g. 'en', 'es'). Auto-detect if None.

    Returns:
        JSON string with 'text' and 'language' keys.
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

    # 3. Fallback: openai-whisper (PyTorch)
    result = _legacy_transcribe(audio_path, language)
    if result:
        return result

    return json.dumps({"error": "No STT engine available (install faster-whisper)"})


def whisper_detect_language(audio_path: str) -> str:
    """Detect the language of an audio file.

    Uses faster-whisper (preferred) or openai-whisper for language detection.

    Args:
        audio_path: Path to audio file.

    Returns:
        JSON string with 'language' and 'probability' keys.
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


def unload_whisper():
    """Unload all STT models to free memory."""
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
# Streaming recognizer (for real-time mic input)
# ═══════════════════════════════════════════════════════════════

def create_streaming_recognizer(sample_rate: int = 16000):
    """Create a sherpa-onnx OnlineRecognizer for real-time streaming STT.

    Usage:
        recognizer = create_streaming_recognizer()
        stream = recognizer.create_stream()
        # Feed audio chunks:
        stream.accept_waveform(sample_rate, numpy_samples)
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        partial_text = recognizer.get_result(stream).text

    Returns:
        sherpa_onnx.OnlineRecognizer, or None if streaming not available.
    """
    try:
        import sherpa_onnx
    except ImportError:
        logger.warning("sherpa-onnx not installed — streaming STT unavailable")
        return None

    # For streaming, use zipformer or similar online model
    # Moonshine and Whisper are offline (batch) models in sherpa-onnx
    # Online models need separate download
    # For now, return None — streaming to be wired when online models are added
    logger.info(
        "Streaming STT: use client-side Web Speech API for real-time. "
        "Server-side streaming requires sherpa-onnx online models (zipformer)."
    )
    return None


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
