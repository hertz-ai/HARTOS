"""TTS tool — in-process text-to-speech via NeuTTS Air (Neuphonic).

NeuTTS Air benefits:
  - 748M params on Qwen2 backbone, GGUF Q4 (~600MB) / Q8 (~800MB)
  - Apache 2.0, fully open source
  - On-device: RTF<0.5 on CPU, 24kHz output
  - Instant voice cloning from 3-15s reference audio
  - English primary; CPU-friendly (no GPU required for Q4)

Conservative add (2026-05-07): wrapper imports `neutts` lazily.
If the package isn't installed, every entry point returns a clean
JSON failure ({error: ...}) — the TTS ladder traverses past us
to the next engine (kokoro / pocket_tts / cosyvoice3 / piper).
This honors the "failure of one shouldn't block another" contract
(verified against tts/package_installer.py:1357 + Nunba/models/
orchestrator.py:327 — per-engine independent install + WAV-synth
+ duration-validation).

Public API (mirrors pocket_tts_tool.py shape):
  neutts_synthesize(text, voice, output_path) -> JSON
  neutts_list_voices() -> JSON

Reference voices (NeuTTS requires a reference audio + transcript
for cloning — there is no zero-config default voice the way
pocket_tts has built-in 'alba'/'jean'/etc.).  We expose:
  - 'jo' (default): the upstream sample shipped with the neutts
    package install at <site-packages>/neutts/samples/jo.{wav,txt}
  - any path to a user-provided .wav (with companion .txt of the
    transcript at the same stem) for ad-hoc cloning
  - any name in ~/.hevolve/models/tts/neutts_voices/<name>.{wav,txt}
    for persistent user-cloned voices

Model downloaded lazily on first use (HuggingFace neuphonic/neutts-
air for the backbone, neuphonic/neucodec for the audio decoder).
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

from .registry import ServiceToolInfo, service_tool_registry

logger = logging.getLogger(__name__)

# Cached model + reference codes (avoid reloading on every call)
_tts_model = None
_ref_codes_cache: dict = {}  # voice_name -> (ref_codes, ref_text)


# ═══════════════════════════════════════════════════════════════
# Storage paths
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


# ═══════════════════════════════════════════════════════════════
# Model + reference management
# ═══════════════════════════════════════════════════════════════

def _load_model():
    """Load NeuTTS Air model (lazy, cached).

    Raises:
        ImportError: if the `neutts` package isn't installed.
            Callers MUST handle this to keep the TTS ladder
            traversing past us.
    """
    global _tts_model
    if _tts_model is not None:
        return _tts_model

    from neutts import NeuTTS  # lazy — let ImportError bubble

    logger.info("Loading NeuTTS Air model (748M params, Qwen2 backbone)...")
    # Default to CPU — Q4 GGUF runs at <0.5x RTF on CPU per the
    # upstream README.  GPU configurations can override via env var.
    backbone_device = os.environ.get('NEUTTS_BACKBONE_DEVICE', 'cpu')
    codec_device = os.environ.get('NEUTTS_CODEC_DEVICE', 'cpu')
    backbone_repo = os.environ.get(
        'NEUTTS_BACKBONE_REPO', 'neuphonic/neutts-air-q4-gguf')
    codec_repo = os.environ.get('NEUTTS_CODEC_REPO', 'neuphonic/neucodec')

    _tts_model = NeuTTS(
        backbone_repo=backbone_repo,
        backbone_device=backbone_device,
        codec_repo=codec_repo,
        codec_device=codec_device,
    )
    logger.info(
        f"NeuTTS Air ready (backbone={backbone_repo} on {backbone_device}, "
        f"codec={codec_repo} on {codec_device})"
    )
    return _tts_model


def _resolve_reference(voice: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a `voice` argument to (ref_audio_path, ref_text).

    Resolution order:
      1. Path to a .wav file (companion .txt at same stem for transcript)
      2. Custom voice name → ~/.hevolve/models/tts/neutts/voices/{name}.wav
      3. Built-in name 'jo' → upstream sample shipped with neutts package
      4. Anything else → (None, None) — caller MUST treat as failure
    """
    # 1. Direct path
    if os.path.isfile(voice):
        wav = voice
        txt_path = os.path.splitext(voice)[0] + '.txt'
        if os.path.isfile(txt_path):
            with open(txt_path, 'r', encoding='utf-8') as fp:
                return wav, fp.read().strip()
        # No transcript — refuse rather than guess
        return None, None

    # 2. Custom user-cloned voice in our voices dir
    custom_wav = _get_voices_dir() / f"{voice}.wav"
    custom_txt = _get_voices_dir() / f"{voice}.txt"
    if custom_wav.is_file() and custom_txt.is_file():
        with open(custom_txt, 'r', encoding='utf-8') as fp:
            return str(custom_wav), fp.read().strip()

    # 3. Upstream sample 'jo' shipped with the neutts package
    if voice == 'jo':
        try:
            import neutts
            pkg_dir = Path(neutts.__file__).parent
            sample_wav = pkg_dir / 'samples' / 'jo.wav'
            sample_txt = pkg_dir / 'samples' / 'jo.txt'
            if sample_wav.is_file() and sample_txt.is_file():
                with open(sample_txt, 'r', encoding='utf-8') as fp:
                    return str(sample_wav), fp.read().strip()
        except ImportError:
            return None, None

    return None, None


def _get_reference_codes(voice: str):
    """Get or build cached reference codes for the chosen voice.

    NeuTTS uses encoded reference codes (precomputed via the codec)
    to drive cloning — caching them per-voice keeps subsequent
    synth calls cheap (the encoding is the slow part).
    """
    if voice in _ref_codes_cache:
        return _ref_codes_cache[voice]

    ref_wav, ref_text = _resolve_reference(voice)
    if not ref_wav or not ref_text:
        return None

    model = _load_model()
    ref_codes = model.encode_reference(ref_wav)
    _ref_codes_cache[voice] = (ref_codes, ref_text)
    return _ref_codes_cache[voice]


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

def neutts_synthesize(
    text: str,
    voice: str = "jo",
    output_path: Optional[str] = None,
    sample_rate: int = 24000,
) -> str:
    """Synthesize text to speech using NeuTTS Air.

    Args:
        text: Text to synthesize.
        voice: 'jo' (default upstream sample), a path to a .wav
            (with companion .txt transcript), or a custom name in
            ~/.hevolve/models/tts/neutts/voices/.
        output_path: Optional output .wav path. Auto-generated if None.
        sample_rate: Output sample rate. Default 24000 (NeuTTS native).

    Returns:
        JSON string with {path, duration, sample_rate, voice, engine} on
        success or {error: ...} on failure.

    Failure modes (TTS ladder traverses past us — same contract as
    every other engine):
      - neutts package not installed → {error: 'neutts not installed'}
      - reference voice not resolvable → {error: 'voice not configured'}
      - synth raised → {error: '<exc type>: <exc msg>'}
    """
    if not text or not text.strip():
        return json.dumps({"error": "Text is required"})

    if output_path is None:
        import hashlib
        h = hashlib.md5(f"{text[:50]}:{voice}".encode()).hexdigest()[:12]
        output_path = str(_get_output_dir() / f"neutts_{h}.wav")

    import time as _time
    _t0 = _time.monotonic()

    try:
        ref = _get_reference_codes(voice)
        if ref is None:
            return json.dumps({
                "error": (
                    f"NeuTTS voice {voice!r} not configured (no reference "
                    f"audio + transcript found).  Provide a .wav with "
                    f"companion .txt at the same stem, or use the "
                    f"upstream 'jo' sample after installing the neutts "
                    f"package."
                ),
                "engine": "neutts_air",
            })
        ref_codes, ref_text = ref

        model = _load_model()
        wav = model.infer(text, ref_codes, ref_text)

        # Write WAV
        try:
            import soundfile as sf
            sf.write(output_path, wav, sample_rate)
        except ImportError:
            return json.dumps({
                "error": "soundfile not installed (required to write .wav)",
                "engine": "neutts_air",
            })

        # Compute duration from the array length / sample rate
        try:
            import numpy as np
            arr = np.asarray(wav)
            duration = len(arr) / sample_rate
        except Exception:
            duration = 0.0

        elapsed_ms = int((_time.monotonic() - _t0) * 1000)
        logger.info(
            f"neutts_air synthesized {len(text)}ch → {output_path} "
            f"(sr={sample_rate}Hz, dur={duration:.2f}s, voice={voice}, "
            f"latency={elapsed_ms}ms)"
        )
        return json.dumps({
            "path": output_path,
            "duration": round(duration, 2),
            "sample_rate": sample_rate,
            "voice": voice,
            "engine": "neutts_air",
        })

    except ImportError as e:
        logger.info(
            f"NeuTTS not installed (pip install neutts to enable): {e}"
        )
        return json.dumps({
            "error": f"neutts not installed: {e}",
            "engine": "neutts_air",
        })
    except Exception as e:
        logger.warning(f"NeuTTS synthesis failed: {e}")
        return json.dumps({
            "error": f"{type(e).__name__}: {e}",
            "engine": "neutts_air",
        })


def neutts_list_voices() -> str:
    """List available NeuTTS reference voices.

    Returns built-in upstream samples plus any user-cloned voices in
    ~/.hevolve/models/tts/neutts/voices/.
    """
    voices = []

    # 1. Built-in upstream samples (only listable if neutts package
    # is installed AND its samples/ dir is present).
    try:
        import neutts
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
        pass

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

    # Engine availability
    try:
        import neutts  # noqa: F401
        engine = "neutts_air"
    except ImportError:
        engine = "none"

    return json.dumps({"voices": voices, "engine": engine})


def unload_neutts():
    """Unload NeuTTS model + reference cache to free memory."""
    global _tts_model, _ref_codes_cache
    _tts_model = None
    _ref_codes_cache.clear()

    try:
        from .vram_manager import clear_cuda_cache
        clear_cuda_cache()
    except Exception:
        pass

    import gc
    gc.collect()
    logger.info("NeuTTS model unloaded")


# ═══════════════════════════════════════════════════════════════
# Service tool registration
# ═══════════════════════════════════════════════════════════════

class NeuTTSAirTool:
    """Register NeuTTS Air as an in-process service tool.

    Same shape as PocketTTSTool — runs in-process (no sidecar),
    functions are registered directly as callables.  When the
    neutts package isn't installed, the tool registers anyway and
    returns clean {error: ...} JSON on every call so the catalog
    sees the entry but the TTS ladder traverses past us at synth
    time.
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


# Auto-register on import (matches pocket_tts_tool pattern).  The
# registration is robust to neutts package absence — only synth
# calls fail with clean JSON; the catalog entry stays.
try:
    NeuTTSAirTool.register_functions()
except Exception as _reg_err:
    logger.debug(f"NeuTTS tool registration skipped: {_reg_err}")
