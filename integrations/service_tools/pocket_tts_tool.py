"""
TTS tool — in-process text-to-speech via Pocket TTS (Kyutai).

Pocket TTS benefits:
  - 100M params — runs at 6x real-time on CPU (no GPU required)
  - MIT license, fully open source
  - Zero-shot voice cloning from 5 seconds of audio
  - ~200ms latency for first audio chunk
  - English (more languages planned by upstream)
  - 100% local, zero cloud costs

Model downloaded lazily on first use to ~/.hevolve/models/tts/

Fallback: espeak-ng (if pocket-tts not installed).

Public API:
  pocket_tts_synthesize(text, voice, output_path) → JSON
  pocket_tts_list_voices()                        → JSON
  pocket_tts_clone_voice(audio_path, name)        → JSON
  unload_pocket_tts()                             → None
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from .registry import ServiceToolInfo, service_tool_registry

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Built-in voices (shipped with pocket-tts)
# ═══════════════════════════════════════════════════════════════

_BUILTIN_VOICES = [
    "alba", "marius", "javert", "jean",
    "fantine", "cosette", "eponine", "azelma",
]

# ═══════════════════════════════════════════════════════════════
# Cached model (avoid reloading on every call)
# ═══════════════════════════════════════════════════════════════

_tts_model = None
_voice_states = {}  # voice_name -> voice_state cache


# ═══════════════════════════════════════════════════════════════
# Model management
# ═══════════════════════════════════════════════════════════════

def _get_tts_dir() -> Path:
    """Get the TTS model/output storage directory."""
    try:
        from .model_storage import model_storage
        tts_dir = model_storage.get_tool_dir("tts")
    except (ImportError, Exception):
        tts_dir = Path(os.path.expanduser("~/.hevolve/models/tts"))
    tts_dir.mkdir(parents=True, exist_ok=True)
    return tts_dir


def _get_output_dir() -> Path:
    """Get the audio output directory."""
    out_dir = _get_tts_dir() / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _get_custom_voices_dir() -> Path:
    """Get the directory for user-cloned voice states."""
    vdir = _get_tts_dir() / "voices"
    vdir.mkdir(parents=True, exist_ok=True)
    return vdir


def _load_model():
    """Load Pocket TTS model (lazy, cached)."""
    global _tts_model
    if _tts_model is not None:
        return _tts_model

    from pocket_tts import TTSModel

    logger.info("Loading Pocket TTS model (100M params, CPU)...")
    _tts_model = TTSModel.load_model()
    logger.info("Pocket TTS model ready")
    return _tts_model


def _get_voice_state(voice: str):
    """Get or create a cached voice state."""
    if voice in _voice_states:
        return _voice_states[voice]

    model = _load_model()

    # Check custom cloned voices first
    custom_path = _get_custom_voices_dir() / f"{voice}.safetensors"
    if custom_path.exists():
        from safetensors.torch import load_file
        state = load_file(str(custom_path))
        _voice_states[voice] = state
        logger.info(f"Loaded custom voice: {voice}")
        return state

    # Check if it's a path to an audio file (for ad-hoc cloning)
    if os.path.isfile(voice):
        state = model.get_state_for_audio_prompt(voice)
        _voice_states[voice] = state
        return state

    # Built-in voice
    state = model.get_state_for_audio_prompt(voice)
    _voice_states[voice] = state
    return state


# ═══════════════════════════════════════════════════════════════
# espeak-ng fallback (for systems without pocket-tts)
# ═══════════════════════════════════════════════════════════════

def _espeak_synthesize(text: str, output_path: str, voice: str = "en") -> bool:
    """Fallback: use espeak-ng for basic TTS."""
    import subprocess
    try:
        result = subprocess.run(
            ["espeak-ng", "-v", voice, "-w", output_path, text],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

def pocket_tts_synthesize(
    text: str,
    voice: str = "alba",
    output_path: Optional[str] = None,
    sample_rate: Optional[int] = None,
) -> str:
    """Synthesize text to speech using Pocket TTS.

    Tries pocket-tts first (high quality, CPU, 6x real-time),
    falls back to espeak-ng (basic quality, always available on NixOS).

    Args:
        text: Text to synthesize.
        voice: Voice name (built-in like 'alba', custom name, or path to .wav).
        output_path: Optional output .wav path. Auto-generated if None.
        sample_rate: Override sample rate (default: model's native rate).

    Returns:
        JSON string with 'path', 'duration', 'voice', 'engine' keys.
    """
    if not text or not text.strip():
        return json.dumps({"error": "Text is required"})

    if output_path is None:
        import hashlib
        h = hashlib.md5(f"{text[:50]}:{voice}".encode()).hexdigest()[:12]
        output_path = str(_get_output_dir() / f"tts_{h}.wav")

    # Try Pocket TTS (preferred)
    try:
        import numpy as np
        model = _load_model()
        voice_state = _get_voice_state(voice)
        audio = model.generate_audio(voice_state, text)

        sr = sample_rate or model.sample_rate
        audio_np = audio.numpy() if hasattr(audio, 'numpy') else audio

        import scipy.io.wavfile
        scipy.io.wavfile.write(output_path, sr, audio_np)

        duration = len(audio_np) / sr
        return json.dumps({
            "path": output_path,
            "duration": round(duration, 2),
            "sample_rate": sr,
            "voice": voice,
            "engine": "pocket-tts",
        })
    except ImportError:
        logger.info("pocket-tts not installed, trying espeak-ng fallback")
    except Exception as e:
        logger.warning(f"Pocket TTS synthesis failed: {e}")

    # Fallback: espeak-ng
    if _espeak_synthesize(text, output_path, voice="en"):
        return json.dumps({
            "path": output_path,
            "duration": 0,  # espeak doesn't report duration
            "voice": "en",
            "engine": "espeak-ng",
        })

    return json.dumps({"error": "No TTS engine available (install pocket-tts or espeak-ng)"})


def pocket_tts_list_voices() -> str:
    """List available TTS voices.

    Returns built-in voices plus any user-cloned voices.

    Returns:
        JSON string with 'voices' list and 'engine' info.
    """
    voices = []

    # Built-in voices
    for name in _BUILTIN_VOICES:
        voices.append({
            "id": name,
            "name": name.title(),
            "type": "builtin",
            "language": "en",
        })

    # Custom cloned voices
    custom_dir = _get_custom_voices_dir()
    if custom_dir.exists():
        for f in sorted(custom_dir.glob("*.safetensors")):
            name = f.stem
            if name not in _BUILTIN_VOICES:
                voices.append({
                    "id": name,
                    "name": name.title(),
                    "type": "cloned",
                    "language": "en",
                })

    # Check which engine is available
    engine = "none"
    try:
        import pocket_tts  # noqa: F401
        engine = "pocket-tts"
    except ImportError:
        try:
            import subprocess
            r = subprocess.run(["espeak-ng", "--version"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                engine = "espeak-ng"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return json.dumps({
        "voices": voices,
        "count": len(voices),
        "engine": engine,
        "builtin_count": len(_BUILTIN_VOICES),
    })


def pocket_tts_clone_voice(audio_path: str, name: str) -> str:
    """Clone a voice from an audio sample (5+ seconds recommended).

    Extracts voice embedding from the audio and saves it for reuse.
    Requires pocket-tts (no fallback for voice cloning).

    Args:
        audio_path: Path to .wav/.mp3 audio sample (5+ seconds of clear speech).
        name: Name to save the cloned voice as.

    Returns:
        JSON string with 'cloned', 'name', 'path' keys.
    """
    if not audio_path or not os.path.isfile(audio_path):
        return json.dumps({"error": "Valid audio_path required"})
    if not name or not name.strip():
        return json.dumps({"error": "Voice name required"})

    name = name.strip().lower().replace(" ", "-")

    try:
        model = _load_model()
        voice_state = model.get_state_for_audio_prompt(audio_path)

        # Export voice state for fast loading
        save_path = _get_custom_voices_dir() / f"{name}.safetensors"
        state_dict = model.export_model_state()
        from safetensors.torch import save_file
        save_file(state_dict, str(save_path))

        # Cache it
        _voice_states[name] = voice_state
        logger.info(f"Voice cloned: {name} from {audio_path}")

        return json.dumps({
            "cloned": True,
            "name": name,
            "path": str(save_path),
        })
    except ImportError:
        return json.dumps({"error": "pocket-tts required for voice cloning"})
    except Exception as e:
        return json.dumps({"error": f"Voice cloning failed: {e}"})


def unload_pocket_tts():
    """Unload Pocket TTS model to free memory."""
    global _tts_model, _voice_states
    _tts_model = None
    _voice_states.clear()

    from .vram_manager import clear_cuda_cache
    clear_cuda_cache()

    import gc
    gc.collect()
    logger.info("Pocket TTS model unloaded")


# ═══════════════════════════════════════════════════════════════
# Service tool registration
# ═══════════════════════════════════════════════════════════════

class PocketTTSTool:
    """Register Pocket TTS as an in-process service tool.

    Like WhisperTool, runs in-process (no sidecar server).
    Functions are registered directly as callables.
    """

    @classmethod
    def register_functions(cls):
        """Register TTS functions with service_tool_registry."""
        tool_info = ServiceToolInfo(
            name="pocket_tts",
            description=(
                "Offline text-to-speech via Pocket TTS (Kyutai). "
                "100M params, 6x real-time on CPU, zero-shot voice cloning "
                "from 5s audio. Falls back to espeak-ng if unavailable. "
                "MIT license, 100% local, zero cloud costs."
            ),
            base_url="inprocess://pocket_tts",
            endpoints={
                "synthesize": {
                    "path": "/synthesize",
                    "method": "POST",
                    "description": (
                        "Convert text to speech audio. "
                        "Input: text (string), voice (optional voice name, "
                        "default 'alba'), output_path (optional .wav path). "
                        "Returns JSON with audio file path and duration."
                    ),
                    "params_schema": {
                        "text": {"type": "string", "description": "Text to speak"},
                        "voice": {"type": "string", "description": "Voice name or .wav path (default: alba)"},
                        "output_path": {"type": "string", "description": "Output .wav path (optional)"},
                    },
                },
                "list_voices": {
                    "path": "/voices",
                    "method": "GET",
                    "description": "List available TTS voices (built-in + cloned).",
                    "params_schema": {},
                },
                "clone_voice": {
                    "path": "/clone",
                    "method": "POST",
                    "description": (
                        "Clone a voice from an audio sample. "
                        "Input: audio_path (path to .wav), name (voice name to save). "
                        "Requires 5+ seconds of clear speech."
                    ),
                    "params_schema": {
                        "audio_path": {"type": "string", "description": "Path to audio sample"},
                        "name": {"type": "string", "description": "Name for the cloned voice"},
                    },
                },
            },
            health_endpoint="/health",
            tags=["tts", "speech", "synthesis", "voice", "offline", "pocket-tts"],
            timeout=60,
        )
        tool_info.is_healthy = True
        service_tool_registry._tools["pocket_tts"] = tool_info
        return True
