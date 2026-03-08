"""
Chatterbox TTS tool — GPU-accelerated emotional speech synthesis.

Two models:
  - Chatterbox Turbo: English-only, 3.8GB VRAM, voice cloning, [laugh]/[chuckle] tags
  - Chatterbox ML: 23 languages, 12GB VRAM, voice cloning

Both require the `chatterbox` pip package and an NVIDIA GPU.
When package is not installed, functions return a JSON error (no crash).

Public API:
  chatterbox_synthesize(text, language, voice, output_path) → JSON
  chatterbox_ml_synthesize(text, language, voice, output_path) → JSON
  unload_chatterbox() → None
"""

import gc
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Module-level singletons (same pattern as luxtts_tool.py)
# ═══════════════════════════════════════════════════════════════

_turbo_model = None
_ml_model = None
SAMPLE_RATE = 24000


def _get_output_dir():
    """Get output directory for generated audio files."""
    from pathlib import Path
    d = Path(os.environ.get(
        'HEVOLVE_MODEL_DIR',
        os.path.expanduser('~/.hevolve/models'),
    )) / 'chatterbox' / 'output'
    d.mkdir(parents=True, exist_ok=True)
    return d


# ═══════════════════════════════════════════════════════════════
# Chatterbox Turbo (English, 3.8GB VRAM)
# ═══════════════════════════════════════════════════════════════

def chatterbox_synthesize(
    text: str,
    language: str = 'en',
    voice: Optional[str] = None,
    output_path: Optional[str] = None,
) -> str:
    """Synthesize speech using Chatterbox Turbo (English, GPU).

    Args:
        text: Text to synthesize. Supports [laugh], [chuckle] tags.
        language: Language code (only 'en' supported for Turbo).
        voice: Path to reference audio for voice cloning, or None.
        output_path: Where to write WAV. Auto-generated if None.

    Returns:
        JSON string with path, duration, engine, device, etc.
    """
    if not text or not text.strip():
        return json.dumps({"error": "Text is required"})

    global _turbo_model
    try:
        from chatterbox.tts import ChatterboxTTS
    except ImportError:
        return json.dumps({
            "error": "chatterbox not installed. pip install chatterbox"
        })

    try:
        # VRAM allocation
        try:
            from integrations.service_tools.vram_manager import get_vram_manager
            get_vram_manager().allocate('tts_chatterbox_turbo')
        except (ImportError, Exception):
            pass

        if _turbo_model is None:
            _turbo_model = ChatterboxTTS.from_pretrained(device='cuda')

        t0 = time.time()
        wav = _turbo_model.generate(text, audio_prompt_path=voice)
        elapsed = time.time() - t0

        if output_path is None:
            output_path = str(_get_output_dir() / f'chatterbox_{int(time.time()*1000)}.wav')

        import torchaudio
        torchaudio.save(output_path, wav.unsqueeze(0).cpu(), SAMPLE_RATE)

        duration = wav.shape[-1] / SAMPLE_RATE
        return json.dumps({
            "path": output_path,
            "duration": round(duration, 2),
            "engine": "chatterbox-turbo",
            "device": "cuda",
            "sample_rate": SAMPLE_RATE,
            "voice": voice or "default",
            "latency_ms": round(elapsed * 1000, 1),
            "rtf": round(elapsed / duration, 4) if duration > 0 else 0,
        })
    except Exception as e:
        return json.dumps({"error": f"Chatterbox Turbo synthesis failed: {e}"})


# ═══════════════════════════════════════════════════════════════
# Chatterbox ML (23 languages, 12GB VRAM)
# ═══════════════════════════════════════════════════════════════

def chatterbox_ml_synthesize(
    text: str,
    language: str = 'en',
    voice: Optional[str] = None,
    output_path: Optional[str] = None,
) -> str:
    """Synthesize speech using Chatterbox ML (23 languages, GPU).

    Args:
        text: Text to synthesize.
        language: ISO 639-1 code (en, zh, ja, ko, de, es, fr, etc.).
        voice: Path to reference audio for voice cloning.
        output_path: Where to write WAV. Auto-generated if None.

    Returns:
        JSON string with path, duration, engine, device, etc.
    """
    if not text or not text.strip():
        return json.dumps({"error": "Text is required"})

    global _ml_model
    try:
        from chatterbox.tts import ChatterboxTTS
    except ImportError:
        return json.dumps({
            "error": "chatterbox not installed. pip install chatterbox"
        })

    try:
        try:
            from integrations.service_tools.vram_manager import get_vram_manager
            get_vram_manager().allocate('tts_chatterbox_ml')
        except (ImportError, Exception):
            pass

        if _ml_model is None:
            _ml_model = ChatterboxTTS.from_pretrained(
                model_name='chatterbox-ml', device='cuda',
            )

        t0 = time.time()
        wav = _ml_model.generate(text, audio_prompt_path=voice, lang=language)
        elapsed = time.time() - t0

        if output_path is None:
            output_path = str(_get_output_dir() / f'chatterbox_ml_{int(time.time()*1000)}.wav')

        import torchaudio
        torchaudio.save(output_path, wav.unsqueeze(0).cpu(), SAMPLE_RATE)

        duration = wav.shape[-1] / SAMPLE_RATE
        return json.dumps({
            "path": output_path,
            "duration": round(duration, 2),
            "engine": "chatterbox-ml",
            "device": "cuda",
            "sample_rate": SAMPLE_RATE,
            "voice": voice or "default",
            "language": language,
            "latency_ms": round(elapsed * 1000, 1),
            "rtf": round(elapsed / duration, 4) if duration > 0 else 0,
        })
    except Exception as e:
        return json.dumps({"error": f"Chatterbox ML synthesis failed: {e}"})


# ═══════════════════════════════════════════════════════════════
# Unload
# ═══════════════════════════════════════════════════════════════

def unload_chatterbox():
    """Unload Chatterbox models to free VRAM."""
    global _turbo_model, _ml_model
    _turbo_model = None
    _ml_model = None
    gc.collect()
    try:
        from integrations.service_tools.vram_manager import get_vram_manager
        mgr = get_vram_manager()
        mgr.release('tts_chatterbox_turbo')
        mgr.release('tts_chatterbox_ml')
    except (ImportError, Exception):
        pass
    logger.info("Chatterbox models unloaded")


# ═══════════════════════════════════════════════════════════════
# Service tool registration
# ═══════════════════════════════════════════════════════════════

class ChatterboxTool:
    """Register Chatterbox as an in-process service tool."""

    @classmethod
    def register_functions(cls):
        from .registry import ServiceToolInfo, service_tool_registry
        tool_info = ServiceToolInfo(
            name="chatterbox",
            description=(
                "GPU-accelerated emotional TTS. Turbo: English + [laugh]/[chuckle] tags, "
                "3.8GB VRAM. ML: 23 languages, 12GB VRAM. Voice cloning. "
                "Requires: pip install chatterbox"
            ),
            base_url="inprocess://chatterbox",
            endpoints={
                "synthesize": {
                    "path": "/synthesize",
                    "method": "POST",
                    "description": "Synthesize with Chatterbox Turbo (English, GPU).",
                    "params_schema": {
                        "text": {"type": "string"},
                        "voice": {"type": "string", "description": "Reference audio path"},
                    },
                },
                "synthesize_ml": {
                    "path": "/synthesize_ml",
                    "method": "POST",
                    "description": "Synthesize with Chatterbox ML (23 languages, GPU).",
                    "params_schema": {
                        "text": {"type": "string"},
                        "language": {"type": "string"},
                        "voice": {"type": "string"},
                    },
                },
            },
            tags=["tts", "speech", "voice-cloning", "gpu", "chatterbox"],
            timeout=60,
        )
        tool_info.is_healthy = True
        service_tool_registry._tools["chatterbox"] = tool_info
        return True
