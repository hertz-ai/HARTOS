"""
MeloTTS tool — myshell-ai's multilingual neural TTS (CPU-friendly, GPU optional).

VRAM: ~1.5 GB on GPU; runs at real-time on CPU too.
Per-language model checkpoints on HuggingFace:
    myshell-ai/MeloTTS-English   (also -English-v2, -English-v3)
    myshell-ai/MeloTTS-Spanish
    myshell-ai/MeloTTS-French
    myshell-ai/MeloTTS-Chinese   (mixed Chinese + English)
    myshell-ai/MeloTTS-Japanese
    myshell-ai/MeloTTS-Korean

Requires: pip install melotts (PyPI 'melotts' package).  Ships with the
`melo` import root, so the Python entry is `from melo.api import TTS`.

SUBPROCESS ISOLATED: same convention as f5_tts_tool / chatterbox_tool —
this module exposes `_load` + `_synthesize` callbacks that the gpu_worker
dispatcher imports in a worker subprocess.  CUDA OOM only kills the
worker; parent receives `transient: true` and can fall back.

Public API (parent side):
  melotts_synthesize(text, language, voice, output_path) → JSON
  unload_melotts() → None
"""

from typing import Optional

import os
import sys

from integrations.service_tools.gpu_worker import ToolWorker

# ── Language code → MeloTTS variant + speaker id ─────────────────
#
# MeloTTS exposes one HF model per language family + a fixed list of
# speaker ids inside each.  Map ISO 639-1 codes to (model_lang_arg,
# default_speaker_key).  Unknown codes raise — the router falls
# through to the next engine in the language preference list.
#
# Source of truth: model card on huggingface.co/myshell-ai/MeloTTS-*

_LANG_TO_MELO = {
    'en': ('EN',    'EN-US'),          # also EN-BR / EN_INDIA / EN-AU / EN-Default
    'es': ('ES',    'ES'),
    'fr': ('FR',    'FR'),
    'zh': ('ZH',    'ZH'),              # Chinese (also handles mixed EN-ZH)
    'ja': ('JP',    'JP'),
    'ko': ('KR',    'KR'),
}


def _resolve_lang(req_lang: Optional[str]) -> tuple[str, str]:
    """Map ISO 639-1 → (MeloTTS language arg, speaker key).

    Defaults to English on unknown codes — caller (the router) is
    expected to filter via _LANG_CAPABLE_BACKENDS / language preference
    before selecting MeloTTS, so this default only triggers when caller
    set language=None.
    """
    if not req_lang:
        return _LANG_TO_MELO['en']
    code = req_lang.replace('_', '-').split('-')[0].lower()
    return _LANG_TO_MELO.get(code, _LANG_TO_MELO['en'])


def _load():
    """Load the English MeloTTS model on the best available device.

    MeloTTS loads one language at a time.  We default to English at
    spawn; subsequent calls with a different language re-instantiate
    the TTS class for that language inside `_synthesize`.  The model
    instance is stored on the cached object as `.tts` and the active
    language on `.lang`.

    On CPU machines MeloTTS still runs in real-time (~1× rtf), so
    we don't hard-fail when CUDA is missing — let torch decide.
    """
    from melo.api import TTS  # noqa: F401  — exception bubbles to worker

    # Pick device based on CUDA availability.  Worker self-reports
    # post-load VRAM via the __WORKER_VRAM_GB__ marker so vram_manager
    # auto-tightens the budget if we declared too high.
    try:
        import torch
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
    except Exception:
        device = 'cpu'

    instance = TTS(language='EN', device=device)

    # Box state in a small object so _synthesize can swap language
    # without losing access to the device hint or the original class.
    class _State:
        def __init__(self_):
            self_.tts = instance
            self_.lang = 'EN'
            self_.device = device

    return _State()


def _synthesize(state, req: dict) -> dict:
    text = req.get('text', '')
    if not text or not text.strip():
        return {'error': 'Text is required'}

    output_path = req.get('output_path')
    if not output_path:
        return {'error': 'output_path is required'}

    melo_lang, speaker_key = _resolve_lang(req.get('language', 'en'))

    # Re-instantiate on language switch (one model per language).
    if melo_lang != state.lang:
        from melo.api import TTS
        state.tts = TTS(language=melo_lang, device=state.device)
        state.lang = melo_lang

    speaker_ids = state.tts.hps.data.spk2id
    # Fall back to first speaker if the requested key isn't in this
    # model's speaker list (e.g. EN-BR not present in some EN variants).
    spk_id = speaker_ids.get(speaker_key) if hasattr(speaker_ids, 'get') \
        else speaker_ids[speaker_key] if speaker_key in speaker_ids \
        else None
    if spk_id is None:
        spk_id = next(iter(speaker_ids.values())) if speaker_ids else 0

    speed = float(req.get('speed') or 1.0)
    state.tts.tts_to_file(
        text=text,
        speaker_id=spk_id,
        output_path=output_path,
        speed=speed,
    )

    # Best-effort duration probe — soundfile is the lightest reader.
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
        'engine': 'melotts',
        'device': state.device,
        'language': req.get('language', 'en'),
        'voice': speaker_key,
    }


# ── Parent-side: one ToolWorker instance ─────────────────────────

_tool = ToolWorker(
    tool_name='melotts',
    tool_module='integrations.service_tools.melotts_tool',
    vram_budget='tts_melotts',
    output_subdir='melotts/output',
    engine='melotts',
    startup_timeout=90.0,
    request_timeout=90.0,
)


def melotts_synthesize(
    text: str,
    language: str = 'en',
    voice: Optional[str] = None,
    output_path: Optional[str] = None,
    speed: float = 1.0,
) -> str:
    """Synthesize speech using MeloTTS (multilingual neural TTS).

    Returns a JSON string compatible with the rest of the TTS tools.
    On subprocess crash the response contains `transient: true` so
    the caller (Nunba TTSEngine / HARTOS tts_router) can fall back.
    """
    return _tool.synthesize(
        text=text,
        language=language,
        voice=voice,
        output_path=output_path,
        extra_request={'speed': speed} if speed != 1.0 else None,
    )


def unload_melotts():
    """Stop the MeloTTS worker subprocess and release VRAM."""
    _tool.stop()


class MeloTTSTool:
    """Register MeloTTS as an in-process service tool."""

    @classmethod
    def register_functions(cls):
        from .registry import ServiceToolInfo, service_tool_registry
        tool_info = ServiceToolInfo(
            name="melotts",
            description=(
                "MeloTTS: myshell-ai multilingual neural TTS. "
                "6 languages (en/es/fr/zh/ja/ko), ~1.5GB VRAM, runs on "
                "CPU at real-time too.  Multiple English accents "
                "(US/BR/IN/AU).  No voice cloning. "
                "Requires: pip install melotts"
            ),
            base_url="inprocess://melotts",
            endpoints={
                "synthesize": {
                    "path": "/synthesize",
                    "method": "POST",
                    "description": "Synthesize with MeloTTS (multilingual, GPU/CPU).",
                    "params_schema": {
                        "text": {"type": "string"},
                        "language": {"type": "string"},
                        "voice": {"type": "string", "description": "Speaker id (EN-US, EN-BR, ...)"},
                    },
                },
            },
            tags=["tts", "speech", "multilingual", "neural", "melotts"],
            timeout=60,
        )
        tool_info.is_healthy = True
        service_tool_registry._tools["melotts"] = tool_info
        return True


# NOTE: no `if __name__ == '__main__':` block — the gpu_worker
# dispatcher picks up `_load` / `_synthesize` when invoked via
# `python -m integrations.service_tools.gpu_worker
#  integrations.service_tools.melotts_tool`.
