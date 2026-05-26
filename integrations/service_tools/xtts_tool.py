"""
XTTS-v2 tool — Coqui XTTS-v2, 17 languages with cross-lingual voice cloning.

VRAM: ~2.5 GB on GPU; CPU inference is slow but supported.
HF: coqui/XTTS-v2 (16-language production checkpoint, hi added in v2).

Requires: pip install coqui-tts (the maintained 2026 fork — the
original Coqui company shut down, the idiap fork ships fixes since
late 2024 and is the recommended path on PyPI).  Both packages
expose `from TTS.api import TTS` so the import is unchanged.

SUBPROCESS ISOLATED: same convention as f5_tts_tool — `_load` +
`_synthesize` callbacks; the gpu_worker dispatcher imports this
module in a worker subprocess.

Public API (parent side):
  xtts_synthesize(text, language, voice, output_path) → JSON
  unload_xtts() → None
"""

from typing import Optional

import os
import sys

from integrations.service_tools.gpu_worker import ToolWorker

# Default reference voice for XTTS voice cloning — same path Nunba uses
# for chatterbox / F5 so existing reference audio Just Works.
_DEFAULT_REF_VOICE = os.path.join(os.path.expanduser('~'), 'Downloads', 'Lily.mp3')

# XTTS-v2 supported language codes (ISO 639-1 + 'zh-cn' kept verbatim).
# Mirror of the model card; canonical for the router's language gate.
# Used both for input validation here and for the language list in
# tts_router.ENGINE_REGISTRY.
XTTS_LANGUAGES = (
    'en', 'es', 'fr', 'de', 'it', 'pt', 'pl', 'tr', 'ru', 'nl',
    'cs', 'ar', 'zh', 'hu', 'ko', 'ja', 'hi',
)


def _resolve_xtts_lang(req_lang: Optional[str]) -> str:
    """Map an incoming ISO code to the dialect XTTS expects.

    Specifically: XTTS uses 'zh-cn' (not 'zh') for Chinese.  Everything
    else is the bare 2-letter code.
    """
    if not req_lang:
        return 'en'
    code = req_lang.replace('_', '-').split('-')[0].lower()
    if code == 'zh':
        return 'zh-cn'
    return code if code in XTTS_LANGUAGES else 'en'


def _load():
    """Load XTTS-v2 once on the best available device.

    Uses Coqui's TTS API:
        TTS("tts_models/multilingual/multi-dataset/xtts_v2", gpu=True)
    The first call downloads ~1.5GB of weights into ~/.local/share/tts
    (or HF cache) — subsequent loads are fast.
    """
    from TTS.api import TTS

    # Prefer CUDA when available; XTTS in CPU mode is slow but works.
    try:
        import torch
        gpu = bool(torch.cuda.is_available())
    except Exception:
        gpu = False

    return TTS('tts_models/multilingual/multi-dataset/xtts_v2', gpu=gpu)


def _synthesize(model, req: dict) -> dict:
    text = req.get('text', '')
    if not text or not text.strip():
        return {'error': 'Text is required'}

    output_path = req.get('output_path')
    if not output_path:
        return {'error': 'output_path is required'}

    language = _resolve_xtts_lang(req.get('language', 'en'))

    # Resolve reference voice for cloning.  XTTS REQUIRES a speaker_wav
    # for inference — there's no built-in "default voice" mode, so we
    # use the same Lily.mp3 fallback the rest of Nunba uses.
    ref_voice = req.get('voice')
    if not ref_voice and os.path.isfile(_DEFAULT_REF_VOICE):
        ref_voice = _DEFAULT_REF_VOICE

    if not ref_voice or not os.path.isfile(ref_voice):
        return {
            'error': (
                'XTTS-v2 requires a speaker_wav reference; none found '
                f'(checked: {_DEFAULT_REF_VOICE})'
            ),
        }

    model.tts_to_file(
        text=text,
        file_path=output_path,
        speaker_wav=ref_voice,
        language=language,
    )

    # Probe duration via soundfile (lightweight).  XTTS sample rate
    # is 24kHz on the multi-dataset checkpoint.
    try:
        import soundfile as _sf
        info = _sf.info(output_path)
        duration = round(info.frames / info.samplerate, 2)
        sr = info.samplerate
    except Exception:
        duration = 0.0
        sr = 24000

    return {
        'path': output_path,
        'duration': duration,
        'sample_rate': sr,
        'engine': 'xtts-v2',
        'device': 'cuda' if getattr(model, 'gpu', False) else 'cpu',
        'language': req.get('language', 'en'),
        'voice': ref_voice,
    }


# ── Parent-side: one ToolWorker instance ─────────────────────────

_tool = ToolWorker(
    tool_name='xtts_v2',
    tool_module='integrations.service_tools.xtts_tool',
    vram_budget='tts_xtts_v2',
    output_subdir='xtts/output',
    engine='xtts-v2',
    startup_timeout=180.0,   # first-load downloads ~1.5GB of weights
    request_timeout=120.0,
)


def xtts_synthesize(
    text: str,
    language: str = 'en',
    voice: Optional[str] = None,
    output_path: Optional[str] = None,
) -> str:
    """Synthesize speech using XTTS-v2 (17 langs, voice cloning).

    Returns JSON. On subprocess crash the response contains
    `transient: true` so the caller can fall back.
    """
    return _tool.synthesize(
        text=text,
        language=language,
        voice=voice,
        output_path=output_path,
    )


def unload_xtts():
    """Stop the XTTS worker subprocess and free VRAM."""
    _tool.stop()


class XTTSTool:
    """Register XTTS-v2 as an in-process service tool."""

    @classmethod
    def register_functions(cls):
        from .registry import ServiceToolInfo, service_tool_registry
        tool_info = ServiceToolInfo(
            name="xtts_v2",
            description=(
                "XTTS-v2: Coqui's multilingual voice-cloning TTS. "
                "17 languages, 6-second cloning from any reference clip, "
                "~2.5 GB VRAM, cross-lingual transfer. "
                "Requires: pip install coqui-tts"
            ),
            base_url="inprocess://xtts_v2",
            endpoints={
                "synthesize": {
                    "path": "/synthesize",
                    "method": "POST",
                    "description": "Synthesize with XTTS-v2 (17 langs, voice clone).",
                    "params_schema": {
                        "text": {"type": "string"},
                        "language": {"type": "string"},
                        "voice": {"type": "string",
                                  "description": "Reference audio path (.wav/.mp3, ≥6s)"},
                    },
                },
            },
            tags=["tts", "speech", "voice-cloning", "multilingual", "xtts", "gpu"],
            timeout=120,
        )
        tool_info.is_healthy = True
        service_tool_registry._tools["xtts_v2"] = tool_info
        return True


# NOTE: no `if __name__ == '__main__':` block — gpu_worker dispatcher
# resolves `_load` / `_synthesize` by convention.
