"""
CosyVoice 3 TTS tool — multilingual zero-shot voice cloning (GPU).

Supports: zh, ja, ko, de, es, fr, it, ru, en (9 languages).
VRAM: 3.5GB model size, 4GB recommended.
Requires: pip install cosyvoice

When package is not installed, functions return a JSON error (no crash).

Public API:
  cosyvoice_synthesize(text, language, voice, output_path) → JSON
  unload_cosyvoice() → None
"""

import gc
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

_model = None
SAMPLE_RATE = 22050  # CosyVoice default


def _get_output_dir():
    from pathlib import Path
    d = Path(os.environ.get(
        'HEVOLVE_MODEL_DIR',
        os.path.expanduser('~/.hevolve/models'),
    )) / 'cosyvoice' / 'output'
    d.mkdir(parents=True, exist_ok=True)
    return d


def cosyvoice_synthesize(
    text: str,
    language: str = 'zh',
    voice: Optional[str] = None,
    output_path: Optional[str] = None,
) -> str:
    """Synthesize speech using CosyVoice 3 (9 languages, GPU).

    Args:
        text: Text to synthesize.
        language: ISO 639-1 code (zh, ja, ko, de, es, fr, it, ru, en).
        voice: Path to reference audio for zero-shot voice cloning.
        output_path: Where to write WAV. Auto-generated if None.

    Returns:
        JSON string with path, duration, engine, device, etc.
    """
    if not text or not text.strip():
        return json.dumps({"error": "Text is required"})

    global _model
    try:
        from cosyvoice.cli.cosyvoice import CosyVoice as CosyVoiceModel
    except ImportError:
        return json.dumps({
            "error": "cosyvoice not installed. pip install cosyvoice"
        })

    try:
        try:
            from integrations.service_tools.vram_manager import get_vram_manager
            get_vram_manager().allocate('tts_cosyvoice3')
        except (ImportError, Exception):
            pass

        if _model is None:
            _model = CosyVoiceModel('CosyVoice-300M-SFT')

        t0 = time.time()
        if voice:
            output = _model.inference_zero_shot(text, '', voice)
        else:
            output = _model.inference_sft(text, 'default')

        wav_data = next(output)['tts_speech']
        elapsed = time.time() - t0

        if output_path is None:
            output_path = str(_get_output_dir() / f'cosyvoice_{int(time.time()*1000)}.wav')

        import torchaudio
        torchaudio.save(output_path, wav_data.cpu(), SAMPLE_RATE)

        duration = wav_data.shape[-1] / SAMPLE_RATE
        return json.dumps({
            "path": output_path,
            "duration": round(duration, 2),
            "engine": "cosyvoice3",
            "device": "cuda",
            "sample_rate": SAMPLE_RATE,
            "voice": voice or "default",
            "language": language,
            "latency_ms": round(elapsed * 1000, 1),
            "rtf": round(elapsed / duration, 4) if duration > 0 else 0,
        })
    except Exception as e:
        return json.dumps({"error": f"CosyVoice synthesis failed: {e}"})


def unload_cosyvoice():
    """Unload CosyVoice model to free VRAM."""
    global _model
    _model = None
    gc.collect()
    try:
        from integrations.service_tools.vram_manager import get_vram_manager
        get_vram_manager().release('tts_cosyvoice3')
    except (ImportError, Exception):
        pass
    logger.info("CosyVoice model unloaded")


class CosyVoiceTool:
    """Register CosyVoice as an in-process service tool."""

    @classmethod
    def register_functions(cls):
        from .registry import ServiceToolInfo, service_tool_registry
        tool_info = ServiceToolInfo(
            name="cosyvoice",
            description=(
                "CosyVoice 3: multilingual zero-shot TTS. "
                "9 languages (zh/ja/ko/de/es/fr/it/ru/en), 3.5GB VRAM. "
                "Requires: pip install cosyvoice"
            ),
            base_url="inprocess://cosyvoice",
            endpoints={
                "synthesize": {
                    "path": "/synthesize",
                    "method": "POST",
                    "description": "Synthesize with CosyVoice 3 (9 languages, GPU).",
                    "params_schema": {
                        "text": {"type": "string"},
                        "language": {"type": "string"},
                        "voice": {"type": "string", "description": "Reference audio path"},
                    },
                },
            },
            tags=["tts", "speech", "voice-cloning", "gpu", "cosyvoice", "multilingual"],
            timeout=60,
        )
        tool_info.is_healthy = True
        service_tool_registry._tools["cosyvoice"] = tool_info
        return True
