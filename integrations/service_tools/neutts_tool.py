"""TTS tool — text-to-speech via NeuTTS Air (Neuphonic).

NeuTTS Air benefits:
  - 748M params on Qwen2 backbone, GGUF Q4 (~600MB) / Q8 (~800MB)
  - Apache 2.0, fully open source
  - On-device: RTF<0.5 on CPU, 24kHz output
  - Instant voice cloning from 3-15s reference audio
  - English primary; CPU-friendly (no GPU required for Q4)

Pip package: ``neutts`` on PyPI.  Optional extras: ``neutts[all]`` pulls
``llama-cpp-python`` (GGUF inference) + ``soundfile`` + ``onnxruntime``
(codec decoder).  License Apache-2.0.

Architecture (ToolWorker pattern — same as kokoro / chatterbox / f5):
  - This module exposes ``_load`` + ``_synthesize`` (subprocess-side
    callbacks) and ``_tool`` (parent-side ``ToolWorker`` instance).
  - ``neutts_synthesize`` (the canonical public entry point referenced
    by ``tts_router.ENGINE_REGISTRY['neutts_air'].tool_function``)
    forwards through ``_tool.synthesize``.
  - The desktop installer (Nunba) routes ``neutts`` into a dedicated
    venv (``install_target='venv'`` in tts_router.py) because
    ``neutts[all]`` pulls ``llama-cpp-python`` whose torch / numpy
    pins can drift from the main interpreter.  ``ToolWorker``'s
    ``python_exe`` is wired to the venv's python at install time so
    the synth subprocess sees the pinned neutts deps.

Reference voices (NeuTTS requires a reference audio + transcript for
cloning — there is no zero-config default voice the way pocket_tts has
built-in 'alba'/'jean'/etc.).  Resolution order:
  1. Path to a .wav (with companion .txt at the same stem) — ad-hoc
  2. Custom name → ``~/.hevolve/models/tts/neutts/voices/<name>.wav``
     (with companion .txt) — persistent user-cloned voices
  3. ``'jo'`` (default) → upstream sample at
     ``<site-packages>/neutts/samples/jo.{wav,txt}``

Model downloaded lazily on first use (HuggingFace
``neuphonic/neutts-air-q4-gguf`` backbone + ``neuphonic/neucodec``
codec).  Env overrides:
  - ``NEUTTS_BACKBONE_REPO`` (default ``neuphonic/neutts-air-q4-gguf``)
  - ``NEUTTS_BACKBONE_DEVICE`` (default ``cpu``)
  - ``NEUTTS_CODEC_REPO`` (default ``neuphonic/neucodec``)
  - ``NEUTTS_CODEC_DEVICE`` (default ``cpu``)

Public API (parent side):
  neutts_synthesize(text, voice, output_path, language) -> JSON
  neutts_list_voices() -> JSON
  unload_neutts() -> None
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

from integrations.service_tools.gpu_worker import ToolWorker

from .registry import ServiceToolInfo, service_tool_registry

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Storage paths (parent + subprocess use the same resolver — single
# source of truth for "where do user-cloned voice files live?")
# ═══════════════════════════════════════════════════════════════

def _get_tts_dir() -> Path:
    """Get the NeuTTS storage directory."""
    try:
        from .model_storage import model_storage
        tts_dir = model_storage.get_tool_dir("neutts")
    except (ImportError, Exception):
        tts_dir = Path(os.path.expanduser("~/.hevolve/models/tts/neutts"))
    tts_dir.mkdir(parents=True, exist_ok=True)
    return tts_dir


def _get_output_dir() -> Path:
    """Get the audio output directory."""
    out_dir = _get_tts_dir() / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _get_voices_dir() -> Path:
    """Get the directory for user-cloned voice references."""
    vdir = _get_tts_dir() / "voices"
    vdir.mkdir(parents=True, exist_ok=True)
    return vdir


def _resolve_reference(voice: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a `voice` argument to (ref_audio_path, ref_text).

    Resolution order:
      1. Path to a .wav file (companion .txt at same stem for transcript)
      2. Custom voice name → ~/.hevolve/models/tts/neutts/voices/{name}.wav
      3. Built-in name 'jo' → upstream sample shipped with neutts package
      4. Anything else → (None, None) — caller MUST treat as failure
    """
    # 1. Direct path
    if voice and os.path.isfile(voice):
        wav = voice
        txt_path = os.path.splitext(voice)[0] + '.txt'
        if os.path.isfile(txt_path):
            with open(txt_path, encoding='utf-8') as fp:
                return wav, fp.read().strip()
        # No transcript — refuse rather than guess
        return None, None

    # 2. Custom user-cloned voice in our voices dir
    custom_wav = _get_voices_dir() / f"{voice}.wav"
    custom_txt = _get_voices_dir() / f"{voice}.txt"
    if custom_wav.is_file() and custom_txt.is_file():
        with open(custom_txt, encoding='utf-8') as fp:
            return str(custom_wav), fp.read().strip()

    # 3. Upstream sample 'jo' shipped with the neutts package
    if voice == 'jo':
        try:
            import neutts  # noqa: F401
            pkg_dir = Path(neutts.__file__).parent
            sample_wav = pkg_dir / 'samples' / 'jo.wav'
            sample_txt = pkg_dir / 'samples' / 'jo.txt'
            if sample_wav.is_file() and sample_txt.is_file():
                with open(sample_txt, encoding='utf-8') as fp:
                    return str(sample_wav), fp.read().strip()
        except ImportError:
            return None, None

    return None, None


# ═══════════════════════════════════════════════════════════════
# Subprocess-side callbacks (invoked by gpu_worker dispatcher)
# ═══════════════════════════════════════════════════════════════

def _load() -> dict:
    """Load NeuTTS Air once at subprocess startup.

    Default device is CPU because NeuTTS's Q4 GGUF runs at <0.5x RTF
    on a modest consumer CPU per the upstream README — we don't burn
    GPU for an engine the CPU can already serve in real time.  Users
    on big GPUs can override via ``NEUTTS_BACKBONE_DEVICE=cuda``.

    Raises:
        ImportError: if the `neutts` package isn't installed.
            ToolWorker propagates this to the parent, which receives
            an `{error: ..., transient: false}` JSON and the TTS
            ladder traverses past us.
    """
    try:
        from neutts import NeuTTS  # type: ignore
    except ImportError as e:
        raise ImportError(
            f"neutts package not installed. "
            f"Install with: pip install neutts[all]  ({e})"
        ) from e

    backbone_device = os.environ.get('NEUTTS_BACKBONE_DEVICE', 'cpu')
    codec_device = os.environ.get('NEUTTS_CODEC_DEVICE', 'cpu')
    backbone_repo = os.environ.get(
        'NEUTTS_BACKBONE_REPO', 'neuphonic/neutts-air-q4-gguf')
    codec_repo = os.environ.get('NEUTTS_CODEC_REPO', 'neuphonic/neucodec')

    logger.info(
        "Loading NeuTTS Air (backbone=%s on %s, codec=%s on %s)...",
        backbone_repo, backbone_device, codec_repo, codec_device,
    )
    model = NeuTTS(
        backbone_repo=backbone_repo,
        backbone_device=backbone_device,
        codec_repo=codec_repo,
        codec_device=codec_device,
    )
    logger.info("NeuTTS Air ready")
    return {
        'model': model,
        'device': backbone_device,
        # Reference codes are expensive to compute (run the codec
        # encoder over the wav).  Cache per-voice for the life of the
        # subprocess so consecutive synth calls with the same voice
        # share the encode cost.
        'ref_cache': {},
    }


def _synthesize(state, req: dict) -> dict:
    """Run one synthesis request inside the worker.

    Args:
        state: dict returned by ``_load`` — holds the loaded model and
               the per-subprocess ref-codes cache.
        req: ``{text, voice, output_path, sample_rate?}`` request.

    Returns:
        ``{path, duration, sample_rate, voice, engine}`` on success or
        ``{error, engine, transient}`` on failure.  ``transient=False``
        for "voice not configured" (deterministic — same input retries
        will fail the same way) and for missing-package errors.
    """
    text = req.get('text', '')
    if not text or not text.strip():
        return {'error': 'Text is required', 'engine': 'neutts_air'}

    output_path = req.get('output_path')
    if not output_path:
        return {'error': 'output_path is required', 'engine': 'neutts_air'}

    voice = req.get('voice') or 'jo'
    sample_rate = int(req.get('sample_rate') or 24000)

    # Resolve + cache the reference codes (codec encoding is the slow
    # part; one-time cost per voice per subprocess).
    cache = state.get('ref_cache', {})
    cached = cache.get(voice)
    if cached is None:
        ref_wav, ref_text = _resolve_reference(voice)
        if not ref_wav or not ref_text:
            return {
                'error': (
                    f"NeuTTS voice {voice!r} not configured (no reference "
                    f"audio + transcript found). Provide a .wav with "
                    f"companion .txt at the same stem, or use the "
                    f"upstream 'jo' sample after installing the neutts "
                    f"package."
                ),
                'engine': 'neutts_air',
                'transient': False,
            }
        try:
            ref_codes = state['model'].encode_reference(ref_wav)
        except Exception as e:
            return {
                'error': f"Reference encode failed: {type(e).__name__}: {e}",
                'engine': 'neutts_air',
                'transient': False,
            }
        cached = (ref_codes, ref_text)
        cache[voice] = cached
        state['ref_cache'] = cache
    ref_codes, ref_text = cached

    try:
        wav = state['model'].infer(text, ref_codes, ref_text)
    except Exception as e:
        # Surface as transient ONLY for likely-recoverable error modes
        # (CUDA OOM, runtime allocation).  Default to non-transient so
        # the TTS ladder doesn't waste cycles re-trying neutts on a
        # deterministic failure (bad weights, missing codec, etc.).
        msg = str(e).lower()
        transient = any(t in msg for t in (
            'out of memory', 'cuda', 'device-side assert',
        ))
        return {
            'error': f"{type(e).__name__}: {e}",
            'engine': 'neutts_air',
            'transient': transient,
        }

    # Write WAV via soundfile — required dep listed in pip_install_plan.
    try:
        import numpy as np
        import soundfile as sf
    except ImportError as e:
        return {
            'error': f"required dep missing: {e}",
            'engine': 'neutts_air',
            'transient': False,
        }

    try:
        arr = np.asarray(wav)
        sf.write(output_path, arr, sample_rate)
        duration = len(arr) / sample_rate
    except Exception as e:
        return {
            'error': f"WAV write failed: {type(e).__name__}: {e}",
            'engine': 'neutts_air',
            'transient': False,
        }

    return {
        'path': output_path,
        'duration': round(float(duration), 2),
        'sample_rate': sample_rate,
        'engine': 'neutts_air',
        'device': state.get('device', 'cpu'),
        'voice': voice,
    }


# ═══════════════════════════════════════════════════════════════
# Parent-side: one ToolWorker instance + canonical public functions
# ═══════════════════════════════════════════════════════════════

# NeuTTS Air on CPU produces RTF<0.5 → for a 10-word utterance, the
# subprocess needs ~3-5s warm + ~1-2s synth.  Q4 GGUF model load is
# the slow part; once loaded subsequent calls are quick.  Match the
# kokoro / chatterbox shape for budgets.
_tool = ToolWorker(
    tool_name='neutts_air',
    tool_module='integrations.service_tools.neutts_tool',
    vram_budget='tts_neutts',
    output_subdir='neutts/output',
    engine='neutts-air',
    startup_timeout=120.0,   # GGUF Q4 (~600MB) cold-start on CPU
    request_timeout=60.0,    # CPU synth dominated by RTF, generous
)


def neutts_synthesize(
    text: str,
    language: str = 'en',
    voice: Optional[str] = None,
    output_path: Optional[str] = None,
) -> str:
    """Synthesize text to speech using NeuTTS Air (English only).

    Forwards through ``_tool.synthesize`` which runs the actual model
    in a subprocess.  On worker crash / model error the result JSON
    contains ``{error: ..., transient: bool}`` so the TTS ladder
    traverses past us to the next engine (kokoro / piper).

    Args:
        text: Text to synthesize.
        language: ISO code — only 'en' is supported (NeuTTS Air is
            English-only).  Accepted-and-ignored for ladder symmetry
            with multi-lang engines; the actual model has no
            language switch.
        voice: 'jo' (upstream sample, default), a path to a .wav
            (with companion .txt transcript), or a custom name in
            ``~/.hevolve/models/tts/neutts/voices/``.
        output_path: Optional output .wav path.  Auto-generated under
            ``~/.hevolve/models/tts/neutts/output/`` when None.

    Returns:
        JSON string — see ``_synthesize`` return shape.
    """
    return _tool.synthesize(
        text=text,
        language='en',                    # NeuTTS is English-only
        voice=voice or 'jo',
        output_path=output_path,
    )


def neutts_list_voices() -> str:
    """List available NeuTTS reference voices.

    Inspects upstream-bundled samples + the user's persistent voices
    dir.  No subprocess needed — reads filesystem only.
    """
    voices = []

    # 1. Built-in upstream samples (only listable if neutts package
    # is installed AND its samples/ dir is present).
    try:
        import neutts  # noqa: F401
        pkg_dir = Path(neutts.__file__).parent
        samples_dir = pkg_dir / 'samples'
        if samples_dir.is_dir():
            for wav in sorted(samples_dir.glob('*.wav')):
                if (samples_dir / f"{wav.stem}.txt").is_file():
                    voices.append({
                        "id": wav.stem,
                        "name": wav.stem.title(),
                        "type": "builtin",
                        "language": "en",
                    })
    except ImportError:
        logger.debug("neutts_list_voices: swallowed ImportError")

    # 2. User-cloned voices
    voices_dir = _get_voices_dir()
    if voices_dir.is_dir():
        for wav in sorted(voices_dir.glob('*.wav')):
            txt = voices_dir / f"{wav.stem}.txt"
            if txt.is_file():
                voices.append({
                    "id": wav.stem,
                    "name": wav.stem.title(),
                    "type": "cloned",
                    "language": "en",
                })

    # Engine availability (probes the parent — the venv-routed import
    # may not surface here; the subprocess's _load is the real probe).
    try:
        import neutts  # noqa: F401
        engine = "neutts_air"
    except ImportError:
        engine = "none"

    return json.dumps({"voices": voices, "engine": engine})


def unload_neutts() -> None:
    """Stop the NeuTTS worker subprocess and free its memory."""
    _tool.stop()


# ═══════════════════════════════════════════════════════════════
# Service tool registration
# ═══════════════════════════════════════════════════════════════

class NeuTTSAirTool:
    """Register NeuTTS Air as an in-process service tool.

    Same shape as KokoroTool — registers an entry in
    ``service_tool_registry`` so the catalog UI shows the engine.
    Synth itself goes through ``_tool.synthesize`` (subprocess).
    """

    @classmethod
    def register_functions(cls):
        """Register NeuTTS functions with service_tool_registry."""
        tool_info = ServiceToolInfo(
            name="neutts_air",
            description=(
                "On-device English text-to-speech via NeuTTS Air "
                "(Neuphonic, Apache 2.0). 748M Qwen2-backbone, "
                "Q4 GGUF ~600MB, RTF<0.5 on CPU, 24kHz output. "
                "Instant voice cloning from 3-15s reference audio."
            ),
            base_url="inprocess://neutts_air",
            endpoints={
                "synthesize": {
                    "path": "/synthesize",
                    "method": "POST",
                    "description": (
                        "Convert text to speech audio. "
                        "Input: text (string), voice (default 'jo' = "
                        "upstream sample; also accepts path to .wav with "
                        "companion .txt, or custom name from "
                        "~/.hevolve/models/tts/neutts/voices/), "
                        "output_path (optional). Returns JSON with audio "
                        "file path and duration."
                    ),
                    "params_schema": {
                        "text": {"type": "string", "description": "Text to speak"},
                        "voice": {"type": "string", "description": "Voice name or path (default: jo)"},
                        "output_path": {"type": "string", "description": "Output .wav path (optional)"},
                    },
                },
                "list_voices": {
                    "path": "/voices",
                    "method": "GET",
                    "description": "List available NeuTTS voices.",
                    "params_schema": {},
                },
            },
            tags=["tts", "english", "voice_clone", "on_device"],
        )
        service_tool_registry.register(tool_info)


# Auto-register on import (matches kokoro_tool / chatterbox_tool
# pattern).  The registration is robust to neutts package absence —
# only synth subprocess calls fail with clean JSON; the catalog entry
# stays so the admin UI can offer "Install NeuTTS Air".
try:
    NeuTTSAirTool.register_functions()
except Exception as _reg_err:
    logger.debug(f"NeuTTS tool registration skipped: {_reg_err}")


# NOTE: no `if __name__ == '__main__':` block here.  The centralized
# dispatcher at integrations.service_tools.gpu_worker imports this
# module and calls `_load` / `_synthesize` directly when spawned.
