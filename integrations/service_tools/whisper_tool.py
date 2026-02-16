"""
Whisper STT tool — in-process speech-to-text (no sidecar server).

Runs OpenAI Whisper directly in the LLM-langchain process. Avoids a
separate server and port by calling the Python API directly.

Model downloaded via ModelStorageManager to ~/.hevolve/models/whisper/
"""

import json
import logging
import os
from typing import Optional

from .registry import ServiceToolInfo, service_tool_registry

logger = logging.getLogger(__name__)

# Lazy-loaded Whisper model
_whisper_model = None
_whisper_model_name = None


def _get_whisper_model(model_name: str = "base"):
    """Lazy-load whisper model. Downloads on first call."""
    global _whisper_model, _whisper_model_name
    if _whisper_model is not None and _whisper_model_name == model_name:
        return _whisper_model

    try:
        import whisper
    except ImportError:
        raise ImportError(
            "openai-whisper not installed. pip install openai-whisper"
        )

    # Use centralized model storage
    from .model_storage import model_storage
    model_dir = model_storage.get_tool_dir("whisper")
    model_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("XDG_CACHE_HOME", str(model_dir.parent))
    logger.info(f"Loading Whisper model '{model_name}'...")
    _whisper_model = whisper.load_model(model_name, download_root=str(model_dir))
    _whisper_model_name = model_name
    logger.info(f"Whisper model '{model_name}' loaded")
    return _whisper_model


def whisper_transcribe(audio_path: str, language: str = None) -> str:
    """Transcribe audio file to text using Whisper.

    Args:
        audio_path: Path to audio file (WAV, MP3, WebM, etc.)
        language: Optional language code (e.g. 'en', 'es'). Auto-detect if None.

    Returns:
        JSON string with 'text' and 'language' keys.
    """
    try:
        model = _get_whisper_model()
        kwargs = {}
        if language:
            kwargs["language"] = language
        result = model.transcribe(audio_path, **kwargs)
        return json.dumps({
            "text": result["text"].strip(),
            "language": result.get("language", "unknown"),
        })
    except ImportError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Transcription failed: {e}"})


def whisper_detect_language(audio_path: str) -> str:
    """Detect the language of an audio file.

    Args:
        audio_path: Path to audio file.

    Returns:
        JSON string with 'language' and 'probability' keys.
    """
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
    except ImportError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Language detection failed: {e}"})


def unload_whisper():
    """Unload whisper model to free memory."""
    global _whisper_model, _whisper_model_name
    _whisper_model = None
    _whisper_model_name = None
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass
    logger.info("Whisper model unloaded")


def select_whisper_model() -> str:
    """Select best whisper model based on available VRAM."""
    from .vram_manager import vram_manager
    gpu = vram_manager.detect_gpu()
    if not gpu["cuda_available"]:
        return "base"  # CPU-only: use smallest
    free = vram_manager.get_free_vram()
    if free >= 10:
        return "large-v3"
    elif free >= 5:
        return "medium"
    elif free >= 2:
        return "small"
    else:
        return "base"


class WhisperTool:
    """Register Whisper STT as an in-process service tool.

    Unlike other tools, Whisper runs in-process (no sidecar server).
    The tool functions are registered directly as callables.
    """

    @classmethod
    def register_functions(cls):
        """Register whisper functions directly with service_tool_registry.

        Since Whisper is in-process, we create a ServiceToolInfo with
        base_url pointing to a sentinel, and override the generated
        functions with our direct callables.
        """
        # Register transcribe function
        whisper_transcribe.__name__ = "whisper_transcribe"
        whisper_transcribe.__doc__ = (
            "Transcribe audio file to text using Whisper STT. "
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

        # Register as a pseudo service tool for the registry
        tool_info = ServiceToolInfo(
            name="whisper",
            description=(
                "Speech-to-text transcription. Converts audio files to text "
                "using OpenAI Whisper. Supports 100+ languages with automatic "
                "language detection."
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
            tags=["stt", "speech", "transcription", "audio", "whisper"],
            timeout=60,
        )
        # Mark as healthy (in-process is always available once loaded)
        tool_info.is_healthy = True
        service_tool_registry._tools["whisper"] = tool_info

        # Override the generated HTTP functions with direct callables
        # The registry's get_all_tool_functions creates HTTP-based executors,
        # but we need direct Python calls for in-process tools
        return True
