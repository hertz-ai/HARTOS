"""
F5-TTS tool — flow-matching voice cloning (English + Chinese, GPU).

VRAM: 1.3GB model size, 2GB recommended.
Requires: pip install f5-tts

When package is not installed, functions return a JSON error (no crash).

Public API:
  f5_synthesize(text, language, voice, output_path) → JSON
  unload_f5_tts() → None
"""

import gc
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

_model = None
SAMPLE_RATE = 24000


def _get_output_dir():
    from pathlib import Path
    d = Path(os.environ.get(
        'HEVOLVE_MODEL_DIR',
        os.path.expanduser('~/.hevolve/models'),
    )) / 'f5_tts' / 'output'
    d.mkdir(parents=True, exist_ok=True)
    return d


def f5_synthesize(
    text: str,
    language: str = 'en',
    voice: Optional[str] = None,
    output_path: Optional[str] = None,
) -> str:
    """Synthesize speech using F5-TTS (English + Chinese, GPU).

    Args:
        text: Text to synthesize.
        language: ISO 639-1 code ('en' or 'zh').
        voice: Path to reference audio for voice cloning.
        output_path: Where to write WAV. Auto-generated if None.

    Returns:
        JSON string with path, duration, engine, device, etc.
    """
    if not text or not text.strip():
        return json.dumps({"error": "Text is required"})

    global _model
    try:
        from f5_tts.api import F5TTS
    except ImportError:
        return json.dumps({
            "error": "f5-tts not installed. pip install f5-tts"
        })

    try:
        try:
            from integrations.service_tools.vram_manager import get_vram_manager
            get_vram_manager().allocate('tts_f5')
        except (ImportError, Exception):
            pass

        if _model is None:
            _model = F5TTS()

        t0 = time.time()
        wav, sr, _ = _model.infer(
            ref_file=voice or "",
            ref_text="",
            gen_text=text,
        )
        elapsed = time.time() - t0

        if output_path is None:
            output_path = str(_get_output_dir() / f'f5_{int(time.time()*1000)}.wav')

        import soundfile as sf
        sf.write(output_path, wav, sr)

        duration = len(wav) / sr
        return json.dumps({
            "path": output_path,
            "duration": round(duration, 2),
            "engine": "f5-tts",
            "device": "cuda",
            "sample_rate": sr,
            "voice": voice or "default",
            "language": language,
            "latency_ms": round(elapsed * 1000, 1),
            "rtf": round(elapsed / duration, 4) if duration > 0 else 0,
        })
    except Exception as e:
        return json.dumps({"error": f"F5-TTS synthesis failed: {e}"})


def unload_f5_tts():
    """Unload F5-TTS model to free VRAM."""
    global _model
    _model = None
    gc.collect()
    try:
        from integrations.service_tools.vram_manager import get_vram_manager
        get_vram_manager().release('tts_f5')
    except (ImportError, Exception):
        pass
    logger.info("F5-TTS model unloaded")


class F5TTSTool:
    """Register F5-TTS as an in-process service tool."""

    @classmethod
    def register_functions(cls):
        from .registry import ServiceToolInfo, service_tool_registry
        tool_info = ServiceToolInfo(
            name="f5_tts",
            description=(
                "F5-TTS: flow-matching voice cloning. "
                "English + Chinese, 1.3GB VRAM. "
                "Requires: pip install f5-tts"
            ),
            base_url="inprocess://f5_tts",
            endpoints={
                "synthesize": {
                    "path": "/synthesize",
                    "method": "POST",
                    "description": "Synthesize with F5-TTS (English + Chinese, GPU).",
                    "params_schema": {
                        "text": {"type": "string"},
                        "language": {"type": "string"},
                        "voice": {"type": "string", "description": "Reference audio path"},
                    },
                },
            },
            tags=["tts", "speech", "voice-cloning", "gpu", "f5"],
            timeout=60,
        )
        tool_info.is_healthy = True
        service_tool_registry._tools["f5_tts"] = tool_info
        return True
