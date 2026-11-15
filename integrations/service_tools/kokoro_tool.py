"""
Kokoro TTS tool — 82M-parameter English-first voice, CPU or GPU.

Kokoro (https://huggingface.co/hexgrad/Kokoro-82M) is a tiny neural
TTS model that fits between Piper (fast but robotic) and the big
voice-clone engines (F5, Chatterbox, CosyVoice3). It runs at ~1x real
time on CPU with better quality than Piper's best voices, so it's the
right second rung on the English fallback ladder:

    chatterbox_turbo (GPU)  →  kokoro (CPU/GPU)  →  piper (CPU bundled)

Why it lives here instead of in Nunba:
  - It's a neural model; we isolate it in a subprocess the same way
    the other TTS engines are isolated via gpu_worker so a crash can't
    take down the main process.
  - Nunba's tts_engine.py routes to this via the shared
    `_SubprocessTTSBackend` adapter — no parallel in-process impl.

VRAM: ~200MB if GPU, else CPU-only.
Requires: pip install kokoro (from hexgrad/kokoro)

Public API (parent side):
  kokoro_synthesize(text, language, voice, output_path, speed) → JSON
  unload_kokoro() → None
"""

from typing import Optional

from integrations.service_tools.gpu_worker import ToolWorker


# ── Worker callbacks (run in subprocess) ──────────────────────────

# Default voice — Kokoro ships multiple English voices (af_bella,
# af_sarah, af_sky, af_nicole, am_adam, am_michael, bf_emma, bf_isabella,
# bm_george, bm_lewis, ...). 'af_bella' is a clean, neutral US female
# voice that matches the default feel of Piper's Lessac high-quality
# model, so the fallback ladder stays tonally consistent when Kokoro
# takes over from chatterbox_turbo.
_DEFAULT_VOICE = 'af_bella'


def _load():
    """Load Kokoro once at subprocess startup (~3-5s).

    Uses the GPU if CUDA is available and has room for it, otherwise the
    CPU (tts_router.gpu_preferred_device). This is the right
    place to burn one warm-up cost — subsequent synth calls amortise
    over the life of the worker process. On a modest consumer CPU the
    warm 82M model produces ~1x real-time speech, which beats every
    realtime-capable voice-clone engine at the same quality level.
    """
    import torch
    try:
        from kokoro import KPipeline
    except ImportError as e:
        raise ImportError(
            "kokoro package not installed. "
            "Install with: pip install kokoro"
        ) from e

    # The GPU only when it has room (the registry says kokoro runs on the CPU).
    from integrations.channels.media.tts_router import gpu_preferred_device
    device = gpu_preferred_device('kokoro')
    # lang_code='a' = American English, 'b' = British English.
    # The worker is registered once with lang_code='a'; multi-accent
    # support would spawn a second worker — out of scope for MVP.
    pipeline = KPipeline(lang_code='a', device=device)
    return {'pipeline': pipeline, 'device': device}


def _synthesize(model, req: dict) -> dict:
    """Run one synthesis request inside the worker.

    Accumulates the generator output into a single waveform, writes
    it to output_path as WAV via soundfile. Kokoro returns PCM at
    24 kHz by default — matches the other neural engines.
    """
    text = req.get('text', '')
    if not text or not text.strip():
        return {'error': 'Text is required'}

    output_path = req.get('output_path')
    if not output_path:
        return {'error': 'output_path is required'}

    voice = req.get('voice') or _DEFAULT_VOICE
    # Kokoro's `speed` arg is a multiplier on phoneme duration: 1.0 is
    # natural speed, >1 speeds up, <1 slows down. Forward from the
    # parent so synthesize_text(..., speed=0.9) reaches Kokoro.
    speed = float(req.get('speed') or 1.0)

    import numpy as np
    import soundfile as sf

    pipeline = model['pipeline']
    # KPipeline returns a generator of (gs, ps, audio) tuples — one
    # per sentence. Concatenate into a single numpy array so we write
    # a single WAV. KPipeline itself does sentence splitting, so
    # long prompts work without us doing our own chunking.
    audio_segments = []
    sample_rate = 24000
    for _gs, _ps, audio in pipeline(text, voice=voice, speed=speed):
        if audio is None:
            continue
        # Some versions return torch tensors, others numpy arrays
        if hasattr(audio, 'cpu'):
            audio = audio.cpu().numpy()
        audio_segments.append(audio)

    if not audio_segments:
        return {'error': 'Kokoro returned no audio'}

    full_wave = np.concatenate(audio_segments)
    sf.write(output_path, full_wave, sample_rate)

    return {
        'path': output_path,
        'duration': round(len(full_wave) / sample_rate, 2),
        'sample_rate': sample_rate,
        'engine': 'kokoro',
        'device': model['device'],
        'voice': voice,
    }


# ── Parent-side: one ToolWorker instance ─────────────────────────

_tool = ToolWorker(
    tool_name='kokoro',
    tool_module='integrations.service_tools.kokoro_tool',
    vram_budget='tts_kokoro',
    output_subdir='kokoro/output',
    engine='kokoro',
    startup_timeout=30.0,
    request_timeout=45.0,
)


def kokoro_synthesize(
    text: str,
    language: str = 'en',
    voice: Optional[str] = None,
    output_path: Optional[str] = None,
    speed: float = 1.0,
) -> str:
    """Synthesize speech using Kokoro 82M (CPU or GPU subprocess).

    Args:
        text: Text to speak.
        language: ISO code — 'en' for now (Kokoro supports other
                  lang_codes but each needs its own pipeline).
        voice: Optional voice preset (e.g. 'af_bella', 'am_adam').
               Defaults to 'af_bella'.
        output_path: Where to write the WAV.
        speed: Speed multiplier passed through to KPipeline.

    Returns JSON. On subprocess crash the response contains
    `{"error": ..., "transient": true}` so the caller can fall back
    to the next engine in the English chain (piper).
    """
    return _tool.synthesize(
        text=text,
        language=language,
        voice=voice,
        output_path=output_path,
        extra_request={'speed': speed} if speed != 1.0 else None,
    )


def unload_kokoro():
    """Stop the Kokoro worker subprocess and free its memory."""
    _tool.stop()


class KokoroTool:
    """Register Kokoro as an in-process service tool."""

    @classmethod
    def register_functions(cls):
        from .registry import ServiceToolInfo, service_tool_registry
        tool_info = ServiceToolInfo(
            name="kokoro",
            description=(
                "Kokoro 82M: small neural English TTS. "
                "Runs on CPU at ~1x real-time or GPU at ~0.1x real-time. "
                "Quality sits between Piper and the big voice-clone engines. "
                "Requires: pip install kokoro"
            ),
            base_url="inprocess://kokoro",
            endpoints={
                "synthesize": {
                    "path": "/synthesize",
                    "method": "POST",
                    "description": "Synthesize with Kokoro (English, CPU or GPU).",
                    "params_schema": {
                        "text": {"type": "string"},
                        "language": {"type": "string"},
                        "voice": {"type": "string", "description": "Voice preset name"},
                        "speed": {"type": "number"},
                    },
                },
            },
            tags=["tts", "speech", "english", "small-model", "kokoro"],
            timeout=45,
        )
        tool_info.is_healthy = True
        service_tool_registry._tools["kokoro"] = tool_info
        return True

# NOTE: no `if __name__ == '__main__':` block here. The centralized
# dispatcher at integrations.service_tools.gpu_worker imports this
# module and calls `_load` / `_synthesize` directly when spawned.
