"""
OmniVoice TTS tool — 646 languages, zero-shot voice cloning (GPU).

Backbone: Qwen3-0.6B + diffusion decoder.  Trained on 581k hours spanning
646 languages — covers every Indic script with substantially more hours
per language than Indic-Parler (e.g. Tamil 423h vs parler's ~10h), and
also covers English, Mandarin, Japanese, Korean, European, Arabic,
African, and low-resource tongues.  Apache 2.0.

VRAM: stubbed at 3.0 GB until first real load.  vram_manager's
record_actual_usage() auto-tightens the budget from the worker's
'__WORKER_VRAM_GB__' telemetry marker on startup.

Requires: pip install omnivoice torch soundfile
Model: HF hub 'k2-fsa/OmniVoice' (~1.5 GB safetensors)

Public API (parent):
  omnivoice_synthesize(text, language, voice, output_path) -> JSON
  unload_omnivoice() -> None

Worker entry (via dispatcher):
  python -m integrations.service_tools.gpu_worker \\
      integrations.service_tools.omnivoice_tool

SUBPROCESS ISOLATED: model + tokenizer live in the worker.  Parent
forwards requests through ToolWorker.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from integrations.service_tools.gpu_worker import ToolWorker

# OmniVoice outputs at 24 kHz (matches every other neural TTS in this
# registry — simplifies audio concatenation in the Nunba chat pipeline).
SAMPLE_RATE = 24000

# Hugging Face repo id — 646-language 0.6B checkpoint
HF_MODEL_ID = 'k2-fsa/OmniVoice'

# Sentence-chunking thresholds — diffusion decoders trail off at long
# contexts; chunk + concat with a short gap for clean prosody.
_INTER_SENTENCE_GAP_S = 0.12
_END_PAD_S = 0.4
_PEAK_TARGET_DB = -1.0
_SPLIT_THRESHOLD_CHARS = 120   # OmniVoice handles longer spans than parler
_MIN_CHUNK_CHARS = 20
_TAIL_MERGE_CHARS = 15

# Extensions we recognise as reference audio file paths (voice cloning);
# anything else passed in `voice` is treated as a free-form descriptor
# (passed to the model's `instruct` argument for voice design).
_AUDIO_SUFFIXES = ('.wav', '.mp3', '.flac', '.ogg', '.m4a')


# ─── Helpers ────────────────────────────────────────────────────────────

def _split_sentences(text: str) -> list:
    """Split at Latin + Indic sentence boundaries, protect '...', merge
    shorts.  Same logic as indic_parler_tool._split_sentences — duplicated
    here deliberately to keep worker modules self-contained (no shared
    runtime state between sibling TTS workers)."""
    protected = text.replace('...', '\x00ELLIPSIS\x00')
    parts = re.split(r'(?<=[^\.\s])[.?!।৷]\s+', protected)
    parts = [p.replace('\x00ELLIPSIS\x00', '...') for p in parts]
    merged = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if merged and len(merged[-1]) < _MIN_CHUNK_CHARS:
            merged[-1] = merged[-1] + ' ' + p
        else:
            merged.append(p)
    if len(merged) > 1 and len(merged[-1]) < _TAIL_MERGE_CHARS:
        merged[-2] = merged[-2] + ' ' + merged[-1]
        merged.pop()
    return merged if len(merged) > 1 else [text]


def _is_audio_path(voice: Optional[str]) -> bool:
    if not voice:
        return False
    v = voice.strip()
    if v.lower().endswith(_AUDIO_SUFFIXES):
        return True
    # Windows-absolute (C:\..) or POSIX-absolute (/..) that exists on disk
    if os.path.isabs(v) and os.path.isfile(v):
        return True
    return False


# ─── Worker-side callbacks (run in subprocess) ──────────────────────────

def _load():
    """Load OmniVoice on GPU (fp16).

    If the official `omnivoice` package isn't installed we fall back to
    a clear error — the worker exits with code 2 and the parent returns
    'insufficient compute' so the router demotes to the next engine in
    LANG_ENGINE_PREFERENCE (indic_parler for Indic, chatterbox_ml for
    others, espeak as final fallback).
    """
    import torch
    try:
        from omnivoice import OmniVoice  # type: ignore
    except ImportError as e:
        raise ImportError(
            "omnivoice package not installed.  "
            "Install with: pip install omnivoice"
        ) from e

    model = OmniVoice.from_pretrained(
        HF_MODEL_ID,
        device_map='cuda:0',
        dtype=torch.float16,
    )
    return {
        'model': model,
        'sample_rate': SAMPLE_RATE,
    }


def _generate_chunk(state: dict, text: str, voice: Optional[str]):
    """One chunk through OmniVoice.  Returns np.ndarray (float32)."""
    model = state['model']
    kwargs = {'text': text}
    if voice:
        if _is_audio_path(voice):
            kwargs['ref_audio'] = voice
            # OmniVoice auto-transcribes the reference if ref_text is
            # omitted — wrapped in try/except inside model.generate.
            kwargs['ref_text'] = ''
        else:
            # Free-form speaker descriptor → voice-design path
            kwargs['instruct'] = voice

    audio = model.generate(**kwargs)
    # OmniVoice returns a list of np.ndarray; take the first clip
    if isinstance(audio, (list, tuple)):
        audio = audio[0]
    import numpy as np
    if hasattr(audio, 'detach'):
        audio = audio.detach().cpu().float().numpy()
    return np.asarray(audio, dtype='float32').squeeze()


def _synthesize(state, req: dict) -> dict:
    text = req.get('text', '')
    if not text or not text.strip():
        return {'error': 'Text is required'}
    output_path = req.get('output_path')
    if not output_path:
        return {'error': 'output_path is required'}

    import numpy as np
    import soundfile as sf

    language = req.get('language', 'en')
    voice = req.get('voice')
    sr = state['sample_rate']

    if len(text) > _SPLIT_THRESHOLD_CHARS:
        sentences = _split_sentences(text)
    else:
        sentences = [text]

    if len(sentences) == 1:
        audio = _generate_chunk(state, text, voice)
    else:
        gap = np.zeros(int(sr * _INTER_SENTENCE_GAP_S), dtype=np.float32)
        chunks = []
        for i, sent in enumerate(sentences):
            chunk_audio = _generate_chunk(state, sent, voice)
            if chunk_audio is not None and len(chunk_audio) > 0:
                chunks.append(chunk_audio)
                if i < len(sentences) - 1:
                    chunks.append(gap)
        audio = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)

    end_pad = np.zeros(int(sr * _END_PAD_S), dtype=np.float32)
    audio = np.concatenate([audio, end_pad])

    peak = float(np.abs(audio).max())
    if peak > 0:
        target_peak = 10 ** (_PEAK_TARGET_DB / 20.0)
        audio = audio * (target_peak / peak)

    sf.write(output_path, audio, sr)

    return {
        'path': output_path,
        'duration': round(len(audio) / sr, 2),
        'sample_rate': sr,
        'engine': 'omnivoice',
        'device': 'cuda',
        'language': language,
        'voice': voice or 'default',
    }


# ─── Parent-side: ToolWorker instance ───────────────────────────────────

_tool = ToolWorker(
    tool_name='omnivoice',
    tool_module='integrations.service_tools.omnivoice_tool',
    vram_budget='tts_omnivoice',
    output_subdir='omnivoice/output',
    engine='omnivoice',
    startup_timeout=180.0,   # first run downloads ~1.5 GB checkpoint
    request_timeout=120.0,
)


def omnivoice_synthesize(
    text: str,
    language: str = 'en',
    voice: Optional[str] = None,
    output_path: Optional[str] = None,
) -> str:
    """Synthesize with OmniVoice (646 languages, zero-shot clone, GPU).

    `voice` semantics:
      - Path to a .wav/.mp3/.flac reference  → voice cloning (ref_audio)
      - Free-form descriptor (e.g. "female, low pitch, british accent")
                                            → voice design (instruct)
      - None / 'default'                    → default speaker

    Language is auto-detected by the model from the input text; the
    `language` argument is carried through as metadata for logging and
    the downstream router.
    """
    return _tool.synthesize(
        text=text,
        language=language,
        voice=voice,
        output_path=output_path,
        default_sample_rate=SAMPLE_RATE,
    )


def unload_omnivoice():
    """Stop the OmniVoice worker subprocess and free its VRAM."""
    _tool.stop()


class OmniVoiceTool:
    """Register OmniVoice as an in-process service tool."""

    @classmethod
    def register_functions(cls):
        from .registry import ServiceToolInfo, service_tool_registry
        tool_info = ServiceToolInfo(
            name="omnivoice",
            description=(
                "OmniVoice TTS: 646 languages (every Indic script, "
                "zh/ja/ko, European, Arabic, low-resource).  "
                "Zero-shot voice cloning from 3-10 s reference.  "
                "Qwen3-0.6B + diffusion (Apache 2.0).  ~2-3 GB VRAM.  "
                "Requires: pip install omnivoice"
            ),
            base_url="inprocess://omnivoice",
            endpoints={
                "synthesize": {
                    "path": "/synthesize",
                    "method": "POST",
                    "description": (
                        "Synthesize with OmniVoice (646 langs, voice "
                        "cloning, GPU)."
                    ),
                    "params_schema": {
                        "text": {"type": "string"},
                        "language": {"type": "string"},
                        "voice": {
                            "type": "string",
                            "description": (
                                "Reference audio path (.wav/.mp3/.flac) "
                                "for cloning, OR a descriptor string "
                                "for voice design"
                            ),
                        },
                    },
                },
            },
            tags=[
                "tts", "speech", "gpu", "multilingual", "universal",
                "voice-clone", "indic",
            ],
            timeout=60,
        )
        tool_info.is_healthy = True
        service_tool_registry._tools["omnivoice"] = tool_info
        return True

# NOTE: no `if __name__ == '__main__':` block — the centralized
# dispatcher (gpu_worker) imports this module and calls _load/_synthesize.
