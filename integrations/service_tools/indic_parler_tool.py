"""
Indic Parler TTS tool — 22 Indian languages + English (GPU).

Supports: hi, ta, te, bn, gu, kn, ml, mr, or, pa, ur, as, bho, doi,
          kok, mai, mni, ne, sa, sat, sd, en
VRAM: 1.8GB model size, 2GB recommended.
Requires: pip install indic-parler-tts

No voice cloning — uses style-conditioned synthesis instead.
When package is not installed, functions return a JSON error (no crash).

Public API:
  indic_parler_synthesize(text, language, voice, output_path) → JSON
  unload_indic_parler() → None
"""

import gc
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

_model = None
_tokenizer = None
SAMPLE_RATE = 44100  # Indic Parler default


def _get_output_dir():
    from pathlib import Path
    d = Path(os.environ.get(
        'HEVOLVE_MODEL_DIR',
        os.path.expanduser('~/.hevolve/models'),
    )) / 'indic_parler' / 'output'
    d.mkdir(parents=True, exist_ok=True)
    return d


def indic_parler_synthesize(
    text: str,
    language: str = 'hi',
    voice: Optional[str] = None,
    output_path: Optional[str] = None,
) -> str:
    """Synthesize speech using Indic Parler TTS (22 languages, GPU).

    Args:
        text: Text to synthesize.
        language: ISO 639-1 code (hi, ta, te, bn, gu, kn, ml, etc.).
        voice: Style description (e.g. "A female speaker with calm tone").
            Not a reference audio — Indic Parler uses text-conditioned styles.
        output_path: Where to write WAV. Auto-generated if None.

    Returns:
        JSON string with path, duration, engine, device, etc.
    """
    if not text or not text.strip():
        return json.dumps({"error": "Text is required"})

    global _model, _tokenizer
    try:
        from parler_tts import ParlerTTSForConditionalGeneration
        from transformers import AutoTokenizer
    except ImportError:
        return json.dumps({
            "error": "indic-parler-tts not installed. pip install indic-parler-tts"
        })

    try:
        try:
            from integrations.service_tools.vram_manager import get_vram_manager
            get_vram_manager().allocate('tts_indic_parler')
        except (ImportError, Exception):
            pass

        import torch

        if _model is None:
            _model = ParlerTTSForConditionalGeneration.from_pretrained(
                "ai4bharat/indic-parler-tts"
            ).to('cuda')
            _tokenizer = AutoTokenizer.from_pretrained("ai4bharat/indic-parler-tts")

        style = voice or f"A female speaker speaks clearly in {language}."

        t0 = time.time()
        input_ids = _tokenizer(style, return_tensors="pt").input_ids.to('cuda')
        prompt_ids = _tokenizer(text, return_tensors="pt").input_ids.to('cuda')
        with torch.no_grad():
            generation = _model.generate(input_ids=input_ids, prompt_input_ids=prompt_ids)

        wav = generation.cpu().float().squeeze()
        elapsed = time.time() - t0

        if output_path is None:
            output_path = str(_get_output_dir() / f'indic_{int(time.time()*1000)}.wav')

        import soundfile as sf
        sf.write(output_path, wav.numpy(), SAMPLE_RATE)

        duration = len(wav) / SAMPLE_RATE
        return json.dumps({
            "path": output_path,
            "duration": round(duration, 2),
            "engine": "indic-parler-tts",
            "device": "cuda",
            "sample_rate": SAMPLE_RATE,
            "voice": style,
            "language": language,
            "latency_ms": round(elapsed * 1000, 1),
            "rtf": round(elapsed / duration, 4) if duration > 0 else 0,
        })
    except Exception as e:
        return json.dumps({"error": f"Indic Parler synthesis failed: {e}"})


def unload_indic_parler():
    """Unload Indic Parler model to free VRAM."""
    global _model, _tokenizer
    _model = None
    _tokenizer = None
    gc.collect()
    try:
        from integrations.service_tools.vram_manager import get_vram_manager
        get_vram_manager().release('tts_indic_parler')
    except (ImportError, Exception):
        pass
    logger.info("Indic Parler model unloaded")


class IndicParlerTool:
    """Register Indic Parler as an in-process service tool."""

    @classmethod
    def register_functions(cls):
        from .registry import ServiceToolInfo, service_tool_registry
        tool_info = ServiceToolInfo(
            name="indic_parler",
            description=(
                "Indic Parler TTS: 22 Indian languages + English. "
                "Style-conditioned synthesis (no voice cloning). "
                "1.8GB VRAM. Requires: pip install indic-parler-tts"
            ),
            base_url="inprocess://indic_parler",
            endpoints={
                "synthesize": {
                    "path": "/synthesize",
                    "method": "POST",
                    "description": "Synthesize with Indic Parler TTS (22 Indic languages, GPU).",
                    "params_schema": {
                        "text": {"type": "string"},
                        "language": {"type": "string"},
                        "voice": {"type": "string", "description": "Style description text"},
                    },
                },
            },
            tags=["tts", "speech", "gpu", "indic", "multilingual"],
            timeout=60,
        )
        tool_info.is_healthy = True
        service_tool_registry._tools["indic_parler"] = tool_info
        return True
