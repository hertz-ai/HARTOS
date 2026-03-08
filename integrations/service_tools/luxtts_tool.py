"""
LuxTTS tool — high-quality voice cloning TTS via ZipVoice-Distill (sherpa-onnx).

Benefits:
  - Zero-shot voice cloning from 3+ seconds of audio
  - 4-step distilled flow-matching diffusion
  - 24kHz output via Vocos vocoder (ONNX INT8)
  - Runs on CPU (sherpa-onnx), no GPU required
  - ~130MB models, auto-downloaded from GitHub releases
  - espeak-ng G2P for multilingual phonemization (EN + ZH)
  - Apache 2.0 license

Models: sherpa-onnx-zipvoice-distill-int8-zh-en-emilia + vocos_24khz.onnx

Fallback chain: LuxTTS (sherpa-onnx) → Pocket TTS → espeak-ng

Public API:
  luxtts_synthesize(text, voice_audio, output_path, ...) → JSON
  luxtts_list_voices()                                    → JSON
  luxtts_clone_voice(audio_path, name)                    → JSON
  luxtts_benchmark(text, ...)                             → JSON
  unload_luxtts()                                         → None
"""

import json
import logging
import os
import time
import wave as wave_mod
from pathlib import Path
from typing import Optional

import numpy as np

from .registry import ServiceToolInfo, service_tool_registry

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

SAMPLE_RATE = 24000
MODEL_TARBALL = "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia"
MODEL_DOWNLOAD_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
    f"{MODEL_TARBALL}.tar.bz2"
)
VOCODER_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models/"
    "vocos_24khz.onnx"
)

# ═══════════════════════════════════════════════════════════════
# Cached engine (singleton)
# ═══════════════════════════════════════════════════════════════

_tts_engine = None
_prompt_cache = {}  # voice_name -> (samples, sample_rate)


# ═══════════════════════════════════════════════════════════════
# Directory helpers
# ═══════════════════════════════════════════════════════════════

def _get_tts_dir() -> Path:
    """Get the LuxTTS model/output storage directory."""
    try:
        from .model_storage import model_storage
        tts_dir = model_storage.get_tool_dir("luxtts")
    except (ImportError, Exception):
        tts_dir = Path(os.path.expanduser("~/.hevolve/models/luxtts"))
    tts_dir.mkdir(parents=True, exist_ok=True)
    return tts_dir


def _get_output_dir() -> Path:
    out_dir = _get_tts_dir() / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _get_voices_dir() -> Path:
    vdir = _get_tts_dir() / "voices"
    vdir.mkdir(parents=True, exist_ok=True)
    return vdir


def _get_model_dir() -> Path:
    return _get_tts_dir() / MODEL_TARBALL


# ═══════════════════════════════════════════════════════════════
# Model download & engine init
# ═══════════════════════════════════════════════════════════════

def _ensure_models() -> Path:
    """Download models if not cached. Returns model directory path."""
    model_dir = _get_model_dir()
    encoder_path = model_dir / "encoder.int8.onnx"
    vocoder_path = _get_tts_dir() / "vocos_24khz.onnx"

    if encoder_path.exists() and vocoder_path.exists():
        return model_dir

    import urllib.request
    import tarfile

    # Download and extract model tarball
    if not encoder_path.exists():
        tarball_path = _get_tts_dir() / f"{MODEL_TARBALL}.tar.bz2"
        if not tarball_path.exists():
            logger.info(f"Downloading ZipVoice models (~109MB)...")
            urllib.request.urlretrieve(MODEL_DOWNLOAD_URL, str(tarball_path))
            logger.info("Download complete.")

        logger.info("Extracting models...")
        with tarfile.open(str(tarball_path), 'r:bz2') as tar:
            tar.extractall(str(_get_tts_dir()))
        tarball_path.unlink(missing_ok=True)
        logger.info("Models extracted.")

    # Download vocoder
    if not vocoder_path.exists():
        logger.info("Downloading Vocos vocoder (~54MB)...")
        urllib.request.urlretrieve(VOCODER_URL, str(vocoder_path))
        logger.info("Vocoder downloaded.")

    return model_dir


def _load_engine():
    """Load sherpa-onnx TTS engine (lazy, cached)."""
    global _tts_engine

    if _tts_engine is not None:
        return _tts_engine

    import sherpa_onnx

    model_dir = _ensure_models()
    vocoder_path = _get_tts_dir() / "vocos_24khz.onnx"
    num_threads = int(os.environ.get('LUXTTS_CPU_THREADS', '4'))

    tts_config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            zipvoice=sherpa_onnx.OfflineTtsZipvoiceModelConfig(
                tokens=str(model_dir / "tokens.txt"),
                encoder=str(model_dir / "encoder.int8.onnx"),
                decoder=str(model_dir / "decoder.int8.onnx"),
                data_dir=str(model_dir / "espeak-ng-data"),
                lexicon=str(model_dir / "lexicon.txt"),
                vocoder=str(vocoder_path),
                feat_scale=0.15,
                t_shift=0.4,
                target_rms=0.1,
                guidance_scale=1.2,
            ),
            provider='cpu',
            debug=False,
            num_threads=num_threads,
        ),
        max_num_sentences=1,
    )

    if not tts_config.validate():
        raise RuntimeError("ZipVoice TTS config validation failed")

    logger.info(f"Loading ZipVoice TTS engine ({num_threads} threads)...")
    _tts_engine = sherpa_onnx.OfflineTts(tts_config)
    logger.info("ZipVoice TTS engine ready.")
    return _tts_engine


def _read_prompt_wav(wav_path: str):
    """Read a WAV file as float32 samples + sample_rate for sherpa-onnx prompt."""
    with wave_mod.open(wav_path) as f:
        assert f.getnchannels() == 1, f"Expected mono, got {f.getnchannels()} channels"
        assert f.getsampwidth() == 2, f"Expected 16-bit, got {f.getsampwidth()*8}-bit"
        num_samples = f.getnframes()
        raw = f.readframes(num_samples)
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return samples, f.getframerate()


def _get_prompt(voice: Optional[str]):
    """Resolve voice name to (samples, sample_rate) tuple."""
    if voice and voice in _prompt_cache:
        return _prompt_cache[voice]

    # Find the WAV file
    wav_path = None
    if voice:
        # Check voices directory
        saved = _get_voices_dir() / f"{voice}.wav"
        if saved.exists():
            wav_path = str(saved)
        elif os.path.isfile(voice):
            wav_path = voice

    if wav_path is None:
        # Try default voice
        default = _get_voices_dir() / "default.wav"
        if default.exists():
            wav_path = str(default)
            voice = "default"

    if wav_path is None:
        return None

    samples, sr = _read_prompt_wav(wav_path)
    _prompt_cache[voice] = (samples, sr)
    return samples, sr


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

def luxtts_synthesize(
    text: str,
    voice_audio: Optional[str] = None,
    output_path: Optional[str] = None,
    device: Optional[str] = None,
    num_steps: int = 4,
    speed: float = 1.0,
    rms: float = 0.01,
) -> str:
    """Synthesize text to speech using ZipVoice (sherpa-onnx).

    Args:
        text: Text to synthesize.
        voice_audio: Path to reference voice audio (.wav, mono 16-bit, 3+ seconds).
                     Or name of a previously cloned voice.
        output_path: Optional output .wav path. Auto-generated if None.
        device: Ignored (sherpa-onnx uses CPU; kept for API compat).
        num_steps: Diffusion steps (3-4 optimal). Default 4.
        speed: Playback speed. Default 1.0.
        rms: Ignored (sherpa-onnx handles internally; kept for API compat).

    Returns:
        JSON string with 'path', 'duration', 'device', 'rtf', 'latency_ms'.
    """
    if not text or not text.strip():
        return json.dumps({"error": "Text is required"})

    if output_path is None:
        import hashlib
        h = hashlib.md5(f"{text[:50]}:{voice_audio or 'default'}".encode()).hexdigest()[:12]
        output_path = str(_get_output_dir() / f"luxtts_{h}.wav")

    try:
        engine = _load_engine()

        # Resolve voice prompt
        prompt = _get_prompt(voice_audio)
        if prompt is None:
            return json.dumps({
                "error": "voice_audio required — provide a .wav reference (mono 16-bit, 3+ seconds)"
            })

        prompt_samples, prompt_sr = prompt
        # Use a generic prompt text (sherpa-onnx needs it for alignment)
        prompt_text = "This is a sample of my voice."

        t0 = time.time()
        audio = engine.generate(
            text,
            prompt_text,
            prompt_samples,
            prompt_sr,
            speed=speed,
            num_steps=num_steps,
        )
        gen_time = time.time() - t0

        if len(audio.samples) == 0:
            return json.dumps({"error": "TTS generation produced no audio"})

        import soundfile as sf
        sf.write(output_path, audio.samples, samplerate=audio.sample_rate, subtype='PCM_16')

        duration = len(audio.samples) / audio.sample_rate
        rtf = gen_time / duration if duration > 0 else 0

        return json.dumps({
            "path": output_path,
            "duration": round(duration, 2),
            "sample_rate": audio.sample_rate,
            "voice": voice_audio or "default",
            "engine": "zipvoice-sherpa-onnx",
            "device": "cpu",
            "num_steps": num_steps,
            "latency_ms": round(gen_time * 1000, 1),
            "rtf": round(rtf, 4),
            "realtime_factor": round(1.0 / rtf, 1) if rtf > 0 else 0,
        })
    except ImportError as e:
        logger.info(f"sherpa-onnx not installed: {e}")
        return json.dumps({"error": f"sherpa-onnx not available (pip install sherpa-onnx): {e}"})
    except Exception as e:
        logger.warning(f"LuxTTS synthesis failed: {e}")
        return json.dumps({"error": f"LuxTTS synthesis failed: {e}"})


def luxtts_list_voices() -> str:
    """List available cloned voices for LuxTTS.

    Returns:
        JSON with 'voices' list and availability info.
    """
    voices = []
    voices_dir = _get_voices_dir()
    if voices_dir.exists():
        for f in sorted(voices_dir.glob("*.wav")):
            voices.append({
                "id": f.stem,
                "name": f.stem.replace("-", " ").replace("_", " ").title(),
                "type": "cloned",
                "format": "wav",
                "path": str(f),
            })

    engine_available = False
    try:
        import sherpa_onnx  # noqa: F401
        engine_available = True
    except ImportError:
        pass

    return json.dumps({
        "voices": voices,
        "count": len(voices),
        "engine": "zipvoice-sherpa-onnx" if engine_available else "not_installed",
        "device": "cpu",
        "sample_rate": SAMPLE_RATE,
    })


def luxtts_clone_voice(audio_path: str, name: str) -> str:
    """Save a voice reference audio for LuxTTS voice cloning.

    LuxTTS encodes the reference at synthesis time, so this just copies
    the audio file to the voices directory for reuse.

    Args:
        audio_path: Path to .wav/.mp3 audio (3+ seconds of clear speech).
        name: Name to save the voice as.

    Returns:
        JSON with 'saved', 'name', 'path'.
    """
    if not audio_path or not os.path.isfile(audio_path):
        return json.dumps({"error": "Valid audio_path required"})
    if not name or not name.strip():
        return json.dumps({"error": "Voice name required"})

    name = name.strip().lower().replace(" ", "-")
    save_path = _get_voices_dir() / f"{name}.wav"

    try:
        import shutil
        # If not WAV, convert using soundfile
        if not audio_path.lower().endswith('.wav'):
            import soundfile as sf
            data, sr = sf.read(audio_path)
            sf.write(str(save_path), data, sr)
        else:
            shutil.copy2(audio_path, str(save_path))

        # Clear cache entry so it reloads
        _prompt_cache.pop(name, None)

        logger.info(f"LuxTTS voice saved: {name} from {audio_path}")
        return json.dumps({
            "saved": True,
            "name": name,
            "path": str(save_path),
        })
    except Exception as e:
        return json.dumps({"error": f"Voice save failed: {e}"})


def luxtts_benchmark(
    text: str = "Hello, this is a benchmark test for measuring text to speech performance.",
    device: Optional[str] = None,
    voice_audio: Optional[str] = None,
    num_runs: int = 3,
) -> str:
    """Benchmark LuxTTS performance on the current hardware.

    Args:
        text: Text to synthesize for benchmarking.
        device: Ignored (kept for API compat).
        voice_audio: Reference voice audio path or voice name.
        num_runs: Number of benchmark runs (default 3).

    Returns:
        JSON with timing statistics, RTF, device info.
    """
    try:
        engine = _load_engine()

        prompt = _get_prompt(voice_audio)
        if prompt is None:
            return json.dumps({"error": "voice_audio required for benchmark"})

        prompt_samples, prompt_sr = prompt
        prompt_text = "This is a sample of my voice."

        # Warmup
        engine.generate(text, prompt_text, prompt_samples, prompt_sr, speed=1.0, num_steps=4)

        times = []
        durations = []
        for _ in range(num_runs):
            t0 = time.time()
            audio = engine.generate(text, prompt_text, prompt_samples, prompt_sr, speed=1.0, num_steps=4)
            elapsed = time.time() - t0
            times.append(elapsed)
            durations.append(len(audio.samples) / audio.sample_rate)

        avg_time = sum(times) / len(times)
        avg_duration = sum(durations) / len(durations)
        avg_rtf = avg_time / avg_duration if avg_duration > 0 else 0

        return json.dumps({
            "engine": "zipvoice-sherpa-onnx",
            "device": "cpu",
            "num_runs": num_runs,
            "text_length": len(text),
            "avg_gen_time_ms": round(avg_time * 1000, 1),
            "min_gen_time_ms": round(min(times) * 1000, 1),
            "max_gen_time_ms": round(max(times) * 1000, 1),
            "avg_audio_duration_s": round(avg_duration, 2),
            "avg_rtf": round(avg_rtf, 4),
            "avg_realtime_factor": round(1.0 / avg_rtf, 1) if avg_rtf > 0 else 0,
            "sample_rate": SAMPLE_RATE,
        })
    except ImportError as e:
        return json.dumps({"error": f"sherpa-onnx not available: {e}"})
    except Exception as e:
        return json.dumps({"error": f"Benchmark failed: {e}"})


def unload_luxtts():
    """Unload LuxTTS engine to free memory."""
    global _tts_engine
    _tts_engine = None
    _prompt_cache.clear()

    import gc
    gc.collect()
    logger.info("LuxTTS engine unloaded")


# ═══════════════════════════════════════════════════════════════
# Service tool registration
# ═══════════════════════════════════════════════════════════════

class LuxTTSTool:
    """Register LuxTTS as an in-process service tool."""

    @classmethod
    def register_functions(cls):
        """Register LuxTTS functions with service_tool_registry."""
        tool_info = ServiceToolInfo(
            name="luxtts",
            description=(
                "High-quality voice cloning TTS via ZipVoice-Distill (sherpa-onnx). "
                "Zero-shot voice cloning from 3s audio. 24kHz Vocos vocoder. "
                "CPU ONNX inference. Apache 2.0 license."
            ),
            base_url="inprocess://luxtts",
            endpoints={
                "synthesize": {
                    "path": "/synthesize",
                    "method": "POST",
                    "description": (
                        "Synthesize text to speech with voice cloning. "
                        "Input: text, voice_audio (path to reference .wav), "
                        "device (cuda/cpu/mps), num_steps (3-4). "
                        "Returns 48kHz WAV."
                    ),
                    "params_schema": {
                        "text": {"type": "string", "description": "Text to speak"},
                        "voice_audio": {"type": "string", "description": "Path to reference voice audio"},
                        "device": {"type": "string", "description": "cuda, cpu, or mps"},
                        "num_steps": {"type": "integer", "description": "Diffusion steps (3-4 optimal)"},
                    },
                },
                "list_voices": {
                    "path": "/voices",
                    "method": "GET",
                    "description": "List saved voice references.",
                    "params_schema": {},
                },
                "clone_voice": {
                    "path": "/clone",
                    "method": "POST",
                    "description": "Save a voice reference for reuse (3+ seconds audio).",
                    "params_schema": {
                        "audio_path": {"type": "string", "description": "Path to audio sample"},
                        "name": {"type": "string", "description": "Name for the voice"},
                    },
                },
                "benchmark": {
                    "path": "/benchmark",
                    "method": "POST",
                    "description": "Run performance benchmark on current hardware.",
                    "params_schema": {
                        "text": {"type": "string", "description": "Text to benchmark with"},
                        "device": {"type": "string", "description": "cuda, cpu, or mps"},
                    },
                },
            },
            health_endpoint="/health",
            tags=["tts", "speech", "voice-cloning", "luxtts", "48khz", "gpu"],
            timeout=60,
        )
        tool_info.is_healthy = True
        service_tool_registry._tools["luxtts"] = tool_info
        return True
