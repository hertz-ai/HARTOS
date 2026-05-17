"""Smart TTS Router — selects the best TTS engine based on constraints.

Decision factors (in priority order):
1. Language — which engines support the target language?
2. Availability — is the engine installed locally?
3. Hardware — GPU present? Enough VRAM? CPU-only fallback?
4. Compute policy — local_only | local_preferred | any (hive offload)
5. Latency — instant (espeak/browser) vs quality (neural)
6. Voice cloning — only clone-capable engines if voice requested
7. Hive peers — offload to GPU peer when local can't serve
"""

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Source → Urgency mapping (backend auto-infers, frontends send source)
# ═══════════════════════════════════════════════════════════════

SOURCE_URGENCY: Dict[str, str] = {
    'chat_response': 'normal',       # Agent reply in chat
    'notification': 'instant',       # System notification
    'greeting': 'instant',           # Boot/login greeting
    'read_aloud': 'quality',         # User clicked "speak this"
    'channel': 'normal',             # Discord/Telegram response
    'cli': 'quality',                # hart voice "text"
    'agent_tool': 'normal',          # Agent using TTS tool
}

# ═══════════════════════════════════════════════════════════════
# Engine Registry — static capabilities of every TTS engine
# ═══════════════════════════════════════════════════════════════

class TTSDevice(Enum):
    GPU_ONLY = "gpu_only"
    GPU_PREFERRED = "gpu_preferred"  # works on CPU too, GPU better
    CPU_ONLY = "cpu_only"
    CLOUD = "cloud"


@dataclass(frozen=True)
class TTSEngineSpec:
    """Static specification of a TTS engine's capabilities."""
    engine_id: str
    device: TTSDevice
    vram_key: str               # key in VRAM_BUDGETS (vram_manager.py)
    languages: Tuple[str, ...]  # ISO 639-1 codes, or ('*',) for all
    quality: float              # 0.0-1.0 subjective quality score
    voice_clone: bool
    latency_gpu_ms: int         # estimated latency on GPU (0 if N/A)
    latency_cpu_ms: int         # estimated latency on CPU (0 if N/A)
    latency_cloud_ms: int       # estimated latency on cloud (0 if N/A)
    tool_module: Optional[str]  # Python module path for the tool
    tool_function: Optional[str]  # parent-side synthesize function name
    tool_worker_attr: Optional[str] = None  # ToolWorker attribute name
                                             # on the tool module; None for
                                             # CPU-only engines that have no
                                             # subprocess worker.
    required_package: Optional[str] = None   # pip package name that must be
                                             # importable at runtime; None
                                             # for engines whose deps are
                                             # bundled (e.g. piper) or CPU-only
                                             # with no extra deps.
    pip_install_plan: Tuple[str, ...] = ()   # canonical pip-spec list to make
                                             # `required_package` actually
                                             # importable + synth-functional —
                                             # includes transitive deps the
                                             # upstream package may forget to
                                             # declare in its install_requires
                                             # (e.g. chatterbox-tts ships
                                             # `import librosa` in tts.py but
                                             # doesn't list librosa as a hard
                                             # dep, so a no-deps pip install
                                             # leaves a broken package on disk
                                             # that imports far enough for
                                             # find_spec() but blows up on
                                             # actual synthesize calls — see
                                             # ~/Documents/Nunba/logs/probe_
                                             # chatterbox_turbo.err).  Single
                                             # source of truth for the desktop
                                             # installer (Nunba) so it doesn't
                                             # carry a parallel dict that
                                             # drifts.  Empty tuple = nothing
                                             # to install (bundled / CPU stub).
    install_target: str = 'main'             # WHERE pip_install_plan should
                                             # land on the desktop installer.
                                             # Valid values:
                                             #   'main'      — into the main
                                             #                 python-embed
                                             #                 site-packages
                                             #                 (legacy default;
                                             #                 risky, dep
                                             #                 conflicts mask
                                             #                 silent failures)
                                             #   'venv'      — into a private
                                             #                 venv at
                                             #                 ~/Documents/
                                             #                 Nunba/data/
                                             #                 venvs/<engine>/.
                                             #                 Requires a
                                             #                 per-engine
                                             #                 worker file
                                             #                 (tts/<engine>_
                                             #                 worker.py) that
                                             #                 the parent
                                             #                 dispatches into
                                             #                 via backend_
                                             #                 venv.invoke_in_
                                             #                 venv().
                                             #   'bundled'   — already on
                                             #                 disk via the
                                             #                 frozen build
                                             #                 (piper voices,
                                             #                 luxtts, espeak)
                                             #   'cloud'     — HTTP-only,
                                             #                 nothing to
                                             #                 install
                                             #                 (makeittalk)
                                             #   'git_clone' — needs git clone
                                             #                 of an upstream
                                             #                 repo + pip
                                             #                 install -e
                                             #                 (cosyvoice3 →
                                             #                 FunAudioLLM/
                                             #                 CosyVoice)
                                             # Default 'main' preserves
                                             # current behavior; flipping a
                                             # GPU engine to 'venv' requires
                                             # the matching worker file in
                                             # Nunba (or the dispatch falls
                                             # back to in-process import,
                                             # which only works if the engine
                                             # is also installed in main).
    sample_rate: int = 24000


# Shared pip-spec constants — keep here so the install plans below stay
# readable and so a single edit updates every engine that pins them.
#
# huggingface_hub 0.29+ removes is_offline_mode that transformers <5.x
# still imports, so we cap below 0.29 for the chatterbox / kokoro chain.
_HF_HUB_PIN = 'huggingface_hub>=0.27.0,<0.29.0'

# Chatterbox plan — `chatterbox-tts` on PyPI omits MULTIPLE runtime
# imports from its install_requires.  Each one only surfaces when the
# install proceeds far enough for the next one to be reached:
#
#   chatterbox/__init__.py:9 → from .tts import ChatterboxTTS
#   chatterbox/tts.py:4       → import librosa     (missing #1)
#   chatterbox/tts.py:6       → import perth       (missing #2)
#
# Each was discovered from a real failed install at
# ~/Documents/Nunba/logs/probe_chatterbox_turbo.err on the user's
# desktop — first librosa, then once that was added, perth.  Listing
# them all here means a fresh chatterbox install completes in one
# pip pass instead of needing 2-3 self-heal iterations (each of
# which downloads ~10 MB of pip metadata).  The Nunba self-heal
# loop catches future un-declared transitives on the install screen
# without surfacing a synth failure to the user.
_CHATTERBOX_PIP_PLAN: Tuple[str, ...] = (
    _HF_HUB_PIN,
    'torchaudio',
    'chatterbox-tts',
    'librosa',     # missing transitive #1 — chatterbox/tts.py:4
    'soundfile',   # librosa needs it on Windows for non-WAV outputs
    'resemble-perth',  # missing transitive #2 — chatterbox/tts.py:6
                       # `import perth`; PyPI pkg name = resemble-perth
                       # (the watermark library Resemble AI uses to
                       # tag synthesized audio).
    # NOTE on the rest of chatterbox-tts==0.1.7's requires_dist
    # (omegaconf, conformer, pyloudnorm, pykakasi, spacy-pkuseg,
    # diffusers, einops, s3tokenizer, etc.):
    # We deliberately do NOT pre-install them in one pip pass.
    # When pip is asked to install many at once with
    # `--no-build-isolation` (frozen build constraint, see
    # package_installer._run_pip), and one of the transitives needs
    # a source build (omegaconf → antlr4-python3-runtime==4.9.* is
    # sdist-only on PyPI), pip's parallel-builds path races against
    # the bundle's setuptools and surfaces as
    #   BackendUnavailable: Cannot import 'setuptools.build_meta'
    # (observed 2026-04-28 on the user's bundle f2d4567 — full pip
    # invocation aborts rc=2, no transitive gets installed).
    # _self_heal_missing_transitives in package_installer.py handles
    # them one-at-a-time AFTER the chatterbox-tts top-level install
    # — single-package mode never triggers the parallel-build race.
    # Combined with the PYTHONNOUSERSITE=1 fix in tts/_torch_probe.py
    # (probe no longer leaks system Python's site-packages), each
    # heal cycle finds a REAL missing transitive, not a phantom one.
    # The original 5-cycle trail (librosa → perth → einops →
    # s3tokenizer → omegaconf) is fine because each cycle resolves
    # in ~10-30s of single-package pip work, not 5 minutes of
    # parallel-build resolver thrash.
)


# All known TTS engines
ENGINE_REGISTRY: Dict[str, TTSEngineSpec] = {
    'chatterbox_turbo': TTSEngineSpec(
        engine_id='chatterbox_turbo',
        device=TTSDevice.GPU_ONLY,
        vram_key='tts_chatterbox_turbo',
        languages=('en',),
        quality=0.95,
        voice_clone=True,
        latency_gpu_ms=150,
        latency_cpu_ms=0,
        latency_cloud_ms=0,
        tool_module='integrations.service_tools.chatterbox_tool',
        tool_function='chatterbox_synthesize',
        tool_worker_attr='_turbo',
        required_package='chatterbox',
        pip_install_plan=_CHATTERBOX_PIP_PLAN,
        # chatterbox-tts 0.1.7 hard-pins torch==2.6.0, transformers==5.2.0,
        # numpy<2.0.0, diffusers==0.29.0, safetensors==0.5.3 — all in
        # direct conflict with HARTOS's main interpreter (torch 2.11,
        # transformers 5.1, numpy 2.4, diffusers 0.37, safetensors 0.7).
        # Auto-heal can never satisfy these because main-interpreter
        # downgrades would break llama-server, indic_parler, faster-whisper,
        # and every other ML stack.  Quarantine into its own venv —
        # same pattern indic_parler uses (parler-tts pinned
        # transformers<4.47).  Nunba's tts/package_installer.py routes
        # the install into ~/.nunba/venvs/chatterbox_turbo/, and the
        # HARTOS ToolWorker's python_exe is set to the venv's python
        # at runtime via desktop/_wire_venv_engines.py at boot, so the
        # synth subprocess sees the pinned chatterbox-compatible deps
        # instead of the main interpreter's incompatible newer ones.
        install_target='venv',
    ),
    # luxtts REMOVED — poor audio quality, not suitable for any use case.
    'cosyvoice3': TTSEngineSpec(
        engine_id='cosyvoice3',
        device=TTSDevice.GPU_ONLY,
        vram_key='tts_cosyvoice3',
        languages=('zh', 'ja', 'ko', 'de', 'es', 'fr', 'it', 'ru', 'en'),
        quality=0.92,
        voice_clone=True,
        latency_gpu_ms=200,
        latency_cpu_ms=0,
        latency_cloud_ms=0,
        tool_module='integrations.service_tools.cosyvoice_tool',
        tool_function='cosyvoice_synthesize',
        tool_worker_attr='_tool',
        required_package='cosyvoice',
        # cosyvoice is NOT pip-installable — needs a `git clone` of
        # FunAudioLLM/CosyVoice plus model weight download via
        # huggingface_hub.  Empty plan + install_target='git_clone'
        # signals Nunba to skip the pip path entirely and route
        # through its git-clone install handler instead.  The
        # verify-synth probe must also short-circuit on git_clone
        # engines when the package isn't importable (current Nunba
        # bug: probe runs `import cosyvoice` blindly + always fails).
        pip_install_plan=(),
        install_target='git_clone',
    ),
    'f5_tts': TTSEngineSpec(
        engine_id='f5_tts',
        device=TTSDevice.GPU_ONLY,
        vram_key='tts_f5',
        languages=('en', 'zh'),
        quality=0.91,
        voice_clone=True,
        latency_gpu_ms=200,
        latency_cpu_ms=0,
        latency_cloud_ms=0,
        tool_module='integrations.service_tools.f5_tts_tool',
        tool_function='f5_synthesize',
        tool_worker_attr='_tool',
        required_package='f5_tts',
        pip_install_plan=('torchaudio', 'f5-tts'),
    ),
    'indic_parler': TTSEngineSpec(
        engine_id='indic_parler',
        device=TTSDevice.GPU_ONLY,
        vram_key='tts_indic_parler',
        languages=(
            'hi', 'ta', 'te', 'bn', 'gu', 'kn', 'ml', 'mr', 'or', 'pa', 'ur',
            'as', 'bho', 'doi', 'kok', 'mai', 'mni', 'ne', 'sa', 'sat', 'sd', 'en',
        ),
        quality=0.90,
        voice_clone=False,
        latency_gpu_ms=300,
        latency_cpu_ms=0,
        latency_cloud_ms=0,
        tool_module='integrations.service_tools.indic_parler_tool',
        tool_function='indic_parler_synthesize',
        tool_worker_attr='_tool',
        required_package='parler_tts',
        # Indic Parler quarantines into its own venv on the desktop —
        # parler-tts 0.2.2 hard-pins transformers<4.47 which conflicts
        # with the main interpreter's transformers 5.1.0.  The full
        # pip plan lives here so it travels with the engine spec; the
        # desktop installer routes the install into the venv when
        # install_target='venv'.  Worker file:
        # tts/indic_parler_worker.py (Nunba).  HARTOS server side runs
        # Indic Parler in its own subprocess worker so the main
        # interpreter pin doesn't apply there either.
        pip_install_plan=(
            # tqdm + colorama pinned FIRST to stop pip's resolver from
            # backtracking through colorama 0.1.x (no setup.py, breaks
            # install).  Witnessed user-facing failure:
            #   "Indic Parler TTS unavailable — using fallback voice engine"
            # Root-caused from ~/Documents/Nunba/logs/venv_indic_parler.log.
            'colorama>=0.4.6',
            'tqdm>=4.65',
            'transformers==4.46.1',  # parler-tts 0.2.2 requires <4.47
            'torch',                  # CPU-ish fallback; replaced by CUDA if GPU
            'torchaudio',
            'sentencepiece',
            'descript-audio-codec',
            'parler-tts==0.2.2',      # 0.2.3 has DacModel.decode() API mismatch
            'soundfile',
            _HF_HUB_PIN,
        ),
        install_target='venv',
    ),
    'chatterbox_ml': TTSEngineSpec(
        engine_id='chatterbox_ml',
        device=TTSDevice.GPU_ONLY,
        vram_key='tts_chatterbox_ml',
        languages=(
            'en', 'zh', 'ja', 'ko', 'de', 'es', 'fr', 'it', 'ru', 'pt',
            'ar', 'nl', 'pl', 'sv', 'tr', 'hi', 'ta', 'te', 'bn', 'id',
            'th', 'vi', 'cs',
        ),
        quality=0.94,
        voice_clone=True,
        latency_gpu_ms=300,
        latency_cpu_ms=0,
        latency_cloud_ms=0,
        tool_module='integrations.service_tools.chatterbox_tool',
        tool_function='chatterbox_ml_synthesize',
        tool_worker_attr='_ml',
        required_package='chatterbox',
        pip_install_plan=_CHATTERBOX_PIP_PLAN,
    ),
    'pocket_tts': TTSEngineSpec(
        engine_id='pocket_tts',
        device=TTSDevice.CPU_ONLY,
        vram_key='',
        languages=('en',),
        quality=0.85,
        voice_clone=True,
        latency_gpu_ms=0,
        latency_cpu_ms=200,
        latency_cloud_ms=0,
        tool_module='integrations.service_tools.pocket_tts_tool',
        tool_function='pocket_tts_synthesize',
        pip_install_plan=('pocket-tts',),
    ),
    # Kokoro 82M — tiny neural English TTS. Runs on CPU (≈1× real-time,
    # 200MB RAM) or GPU (≈0.1× real-time, 200MB VRAM). Quality sits
    # above Piper and below the big voice-clone engines, so it's the
    # right second rung on the English ladder — tried when the GPU
    # engines can't run (no CUDA, VRAM full, package missing) but
    # BEFORE we fall all the way down to Piper.
    #
    # Benchmark context (vs piper, on English):
    #   - quality:     kokoro 0.88   vs  piper 0.70   (subjective MOS gap)
    #   - cpu latency: kokoro 400ms  vs  piper 200ms  (per ~10 words)
    #   - disk:        kokoro 160MB  vs  piper 60MB   (per voice)
    #   - voices:      kokoro ~25    vs  piper ~15    (per-language catalog)
    # Piper still wins on raw CPU speed and disk, which is why it
    # stays the absolute last-resort fallback.
    'kokoro': TTSEngineSpec(
        engine_id='kokoro',
        device=TTSDevice.GPU_PREFERRED,
        vram_key='tts_kokoro',
        languages=('en',),
        quality=0.88,
        voice_clone=False,
        latency_gpu_ms=120,
        latency_cpu_ms=400,
        latency_cloud_ms=0,
        tool_module='integrations.service_tools.kokoro_tool',
        tool_function='kokoro_synthesize',
        tool_worker_attr='_tool',
        required_package='kokoro',
        pip_install_plan=(
            _HF_HUB_PIN,
            'kokoro',     # pulls misaki phonemizer transitively
            'espeakng',   # espeak-ng Python bindings (ships binary on Windows)
        ),
    ),
    # OmniVoice — universal TTS.  Qwen3-0.6B backbone + diffusion head,
    # 646 languages (581k training hours spanning every Indic script,
    # zh/ja/ko, European, Arabic, low-resource).  Zero-shot voice cloning
    # from 3-10 s of reference audio.  Apache 2.0.
    #
    # Languages tuple is ('*',) — same wildcard convention as espeak —
    # but select_engines() only considers engines explicitly listed in
    # LANG_ENGINE_PREFERENCE for the resolved language.  We prepend
    # 'omnivoice' to every Indic + non-English entry + _DEFAULT_PREFERENCE
    # so it wins unless it's uninstalled or the GPU can't hold it.
    #
    # VRAM is stubbed at 3.0 GB in vram_manager.VRAM_BUDGETS; the worker
    # self-reports actual usage on first load via '__WORKER_VRAM_GB__'
    # and vram_manager.record_actual_usage tightens the budget.
    'omnivoice': TTSEngineSpec(
        engine_id='omnivoice',
        device=TTSDevice.GPU_ONLY,
        vram_key='tts_omnivoice',
        languages=('*',),  # 646 languages
        quality=0.93,
        voice_clone=True,
        latency_gpu_ms=250,
        latency_cpu_ms=0,
        latency_cloud_ms=0,
        tool_module='integrations.service_tools.omnivoice_tool',
        tool_function='omnivoice_synthesize',
        tool_worker_attr='_tool',
        required_package='omnivoice',
        # See omnivoice_tool.py docstring: "Requires: pip install
        # omnivoice torch soundfile".  torch is bundled.
        pip_install_plan=('omnivoice', 'soundfile'),
    ),
    'espeak': TTSEngineSpec(
        engine_id='espeak',
        device=TTSDevice.CPU_ONLY,
        vram_key='',
        languages=('*',),  # 100+ languages
        quality=0.40,
        voice_clone=False,
        latency_gpu_ms=0,
        latency_cpu_ms=10,
        latency_cloud_ms=0,
        tool_module='integrations.service_tools.pocket_tts_tool',
        tool_function='pocket_tts_synthesize',  # espeak is fallback inside pocket
        install_target='bundled',
    ),
    'makeittalk': TTSEngineSpec(
        engine_id='makeittalk',
        device=TTSDevice.CLOUD,
        vram_key='',
        languages=('en',),
        quality=0.88,
        voice_clone=False,
        latency_gpu_ms=0,
        latency_cpu_ms=0,
        latency_cloud_ms=5000,
        tool_module=None,  # Special cloud path in model_bus_service
        tool_function=None,
        install_target='cloud',
    ),
    # Piper — bundled CPU engine, multilingual via downloadable voice
    # files. Uses ('*',) wildcard (same convention as espeak) so one
    # spec covers every language Piper has voices for — no parallel
    # per-language list. Runtime synth attempt raises on missing voice
    # files and the router falls through to a neural engine.
    'piper': TTSEngineSpec(
        engine_id='piper',
        device=TTSDevice.CPU_ONLY,
        vram_key='',
        languages=('*',),
        quality=0.70,
        voice_clone=False,
        latency_gpu_ms=0,
        latency_cpu_ms=200,
        latency_cloud_ms=0,
        tool_module=None,  # In-process via Nunba tts/piper_tts.py —
                           # no subprocess worker, no required_package.
        tool_function=None,
        install_target='bundled',
    ),
    # ── Mid-VRAM coverage tier (1–3 GB) ───────────────────────────
    # These three engines fill the gap so every SUPPORTED_LANG_DICT
    # code has at least one engine with vram_gb≤3.0 in its preference
    # ladder.  Indic Parler (2.0) + F5 (2.5) cover en/zh + 22 Indic;
    # the trio below adds the rest of the major language families
    # without forcing users onto the 12-14 GB Chatterbox-ML or the
    # uninstallable git-clone CosyVoice path.
    'melotts': TTSEngineSpec(
        engine_id='melotts',
        device=TTSDevice.GPU_PREFERRED,   # works on CPU at real-time too
        vram_key='tts_melotts',
        languages=('en', 'es', 'fr', 'zh', 'ja', 'ko'),
        quality=0.86,
        voice_clone=False,
        latency_gpu_ms=180,
        latency_cpu_ms=600,
        latency_cloud_ms=0,
        tool_module='integrations.service_tools.melotts_tool',
        tool_function='melotts_synthesize',
        tool_worker_attr='_tool',
        required_package='melo',          # `from melo.api import TTS`
        pip_install_plan=(
            _HF_HUB_PIN,
            'melotts',                    # PyPI package; ships `melo` import root
            'soundfile',                  # used for duration probe
        ),
    ),
    'xtts_v2': TTSEngineSpec(
        engine_id='xtts_v2',
        device=TTSDevice.GPU_ONLY,
        vram_key='tts_xtts_v2',
        languages=(
            'en', 'es', 'fr', 'de', 'it', 'pt', 'pl', 'tr', 'ru', 'nl',
            'cs', 'ar', 'zh', 'hu', 'ko', 'ja', 'hi',
        ),
        quality=0.92,
        voice_clone=True,
        latency_gpu_ms=350,
        latency_cpu_ms=0,
        latency_cloud_ms=0,
        tool_module='integrations.service_tools.xtts_tool',
        tool_function='xtts_synthesize',
        tool_worker_attr='_tool',
        required_package='TTS',           # `from TTS.api import TTS`
        pip_install_plan=(
            _HF_HUB_PIN,
            'coqui-tts',                  # idiap-maintained 2026 fork on PyPI;
                                          # ships `from TTS.api import TTS` so
                                          # the import path is stable.
            'soundfile',
        ),
    ),
    'mms_tts': TTSEngineSpec(
        engine_id='mms_tts',
        device=TTSDevice.GPU_PREFERRED,   # CPU works, GPU faster
        vram_key='tts_mms_tts',
        languages=(
            # Roman-script languages where mms_tts_tool routes without
            # uroman.  Non-Roman scripts (ar/hi/zh/ko/ja/...) ALSO have
            # mms-tts checkpoints but require uroman pre-processing —
            # the tool gracefully fails when uroman isn't installed and
            # the router falls through to the next preference.  We list
            # the broader set here because the tool decides per-call
            # whether it can serve; the router's job is to attempt.
            'en', 'es', 'fr', 'de', 'it', 'pt', 'pl', 'tr', 'ru', 'nl',
            'cs', 'hu', 'sv', 'fi', 'el', 'ro', 'bg', 'uk', 'cy', 'is',
            'zh', 'ja', 'ko', 'vi', 'th', 'id', 'ms', 'km', 'lo', 'my',
            'hi', 'bn', 'ta', 'te', 'mr', 'gu', 'kn', 'ml', 'pa', 'or',
            'ne', 'as', 'sd', 'sa', 'ur', 'si',
            'ar', 'fa', 'he', 'sw',
        ),
        quality=0.78,
        voice_clone=False,
        latency_gpu_ms=200,
        latency_cpu_ms=500,
        latency_cloud_ms=0,
        tool_module='integrations.service_tools.mms_tts_tool',
        tool_function='mms_tts_synthesize',
        tool_worker_attr='_tool',
        required_package='transformers',  # already bundled — no install plan
        pip_install_plan=(
            _HF_HUB_PIN,
            'soundfile',                  # for WAV write
            # uroman is OPTIONAL — only needed for non-Roman scripts.
            # The tool falls through cleanly when missing, so we don't
            # bundle the perl repo + extra pip dep into every install.
            # Users who want broad Indic/Arabic/CJK coverage from MMS
            # specifically can `pip install uroman` separately.
        ),
    ),
}


# ═══════════════════════════════════════════════════════════════
# Language → Engine Preference Table
# ═══════════════════════════════════════════════════════════════

# Ordered by quality for each language — first available wins
LANG_ENGINE_PREFERENCE: Dict[str, List[str]] = {
    # English ladder (quality-then-speed):
    #   1. chatterbox_turbo — big GPU voice-clone, highest quality
    #   2. kokoro           — 82M neural, CPU-friendly, best non-GPU quality
    #   3. pocket_tts       — small cloneable fallback
    #   4. cosyvoice3       — big multilingual GPU, usable for EN
    #   5. piper            — bundled CPU fallback, always ships
    #   6. espeak           — absolute last-resort phoneme synth
    # luxtts dropped from default ladder (poor naturalness); still available
    # for explicit voice-clone requests via direct engine selection.
    # chatterbox_turbo wins on English quality; omnivoice sits above
    # kokoro/pocket/cosyvoice for cross-engine consistency when the
    # user also runs non-English traffic and we want to avoid swapping
    # engines on every language switch.
    'en': ['chatterbox_turbo', 'omnivoice', 'melotts', 'xtts_v2', 'kokoro', 'pocket_tts', 'cosyvoice3', 'mms_tts', 'piper', 'espeak'],
    # Indic languages — omnivoice replaces indic_parler as the primary
    # (parler kept as fallback for one release cycle).  OmniVoice has
    # 100-400 training hours per major Indic language vs parler's ~10,
    # and adds voice cloning which parler lacks entirely.  XTTS-v2
    # adds Hindi (only); MMS-TTS adds the rest as 1 GB-tier coverage.
    'hi': ['omnivoice', 'indic_parler', 'xtts_v2', 'chatterbox_ml', 'cosyvoice3', 'mms_tts', 'espeak'],
    'ta': ['omnivoice', 'indic_parler', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'te': ['omnivoice', 'indic_parler', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'bn': ['omnivoice', 'indic_parler', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'gu': ['omnivoice', 'indic_parler', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'kn': ['omnivoice', 'indic_parler', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'ml': ['omnivoice', 'indic_parler', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'mr': ['omnivoice', 'indic_parler', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'or': ['omnivoice', 'indic_parler', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'pa': ['omnivoice', 'indic_parler', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'ur': ['omnivoice', 'indic_parler', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'as': ['omnivoice', 'indic_parler', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'ne': ['omnivoice', 'indic_parler', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'sa': ['omnivoice', 'indic_parler', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'si': ['omnivoice', 'chatterbox_ml', 'mms_tts', 'espeak'],  # Sinhala — mms-tts adds 1 GB-tier
    'sd': ['omnivoice', 'indic_parler', 'chatterbox_ml', 'mms_tts', 'espeak'],  # Sindhi — Indic Parler + mms
    # CJK — omnivoice has 500k+ hours of CJK in training; promote over cosyvoice.
    # MeloTTS slots above the heavy Chatterbox-ML for the 1.5 GB tier.
    'zh': ['omnivoice', 'melotts', 'cosyvoice3', 'f5_tts', 'xtts_v2', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'ja': ['omnivoice', 'melotts', 'cosyvoice3', 'xtts_v2', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'ko': ['omnivoice', 'melotts', 'cosyvoice3', 'xtts_v2', 'chatterbox_ml', 'mms_tts', 'espeak'],
    # European — XTTS-v2 (2.5 GB, voice clone) and MeloTTS (1.5 GB)
    # slot above the 12 GB Chatterbox-ML so users on 4-8 GB GPUs get
    # quality TTS without the 14 GB allocation that pushes other
    # workers off the GPU.
    'de': ['omnivoice', 'xtts_v2', 'cosyvoice3', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'es': ['omnivoice', 'melotts', 'xtts_v2', 'cosyvoice3', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'fr': ['omnivoice', 'melotts', 'xtts_v2', 'cosyvoice3', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'it': ['omnivoice', 'xtts_v2', 'cosyvoice3', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'ru': ['omnivoice', 'xtts_v2', 'cosyvoice3', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'pt': ['omnivoice', 'xtts_v2', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'ar': ['omnivoice', 'xtts_v2', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'nl': ['omnivoice', 'xtts_v2', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'pl': ['omnivoice', 'xtts_v2', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'sv': ['omnivoice', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'tr': ['omnivoice', 'xtts_v2', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'id': ['omnivoice', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'th': ['omnivoice', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'vi': ['omnivoice', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'cs': ['omnivoice', 'xtts_v2', 'chatterbox_ml', 'mms_tts', 'espeak'],
    # Newly-covered SUPPORTED_LANG_DICT entries — these had no
    # explicit ladder before and would have hit _DEFAULT_PREFERENCE
    # (omnivoice → chatterbox_ml → espeak), where chatterbox_ml needs
    # 14 GB.  MMS-TTS at 1 GB now provides the always-runnable fallback.
    'hu': ['omnivoice', 'xtts_v2', 'chatterbox_ml', 'mms_tts', 'espeak'],
    'el': ['omnivoice', 'mms_tts', 'chatterbox_ml', 'espeak'],
    'fi': ['omnivoice', 'mms_tts', 'espeak'],
    'ro': ['omnivoice', 'mms_tts', 'espeak'],
    'bg': ['omnivoice', 'mms_tts', 'espeak'],
    'uk': ['omnivoice', 'mms_tts', 'espeak'],
    'cy': ['omnivoice', 'mms_tts', 'espeak'],          # Welsh
    'is': ['omnivoice', 'mms_tts', 'espeak'],          # Icelandic
    'ms': ['omnivoice', 'mms_tts', 'espeak'],          # Malay
    'fa': ['omnivoice', 'mms_tts', 'espeak'],          # Persian (uroman)
    'he': ['omnivoice', 'mms_tts', 'espeak'],          # Hebrew (uroman)
    'sw': ['omnivoice', 'mms_tts', 'espeak'],          # Swahili
    'km': ['omnivoice', 'mms_tts', 'espeak'],          # Khmer (uroman)
    'lo': ['omnivoice', 'mms_tts', 'espeak'],          # Lao (uroman)
    'my': ['omnivoice', 'mms_tts', 'espeak'],          # Burmese (uroman)
    # Additional Indic codes that exist in SUPPORTED_LANG_DICT but
    # weren't in the language preference table previously — these
    # ride Indic Parler's 22-language coverage, then mms_tts.
    'brx': ['omnivoice', 'indic_parler', 'mms_tts', 'espeak'],   # Bodo
    'doi': ['omnivoice', 'indic_parler', 'mms_tts', 'espeak'],   # Dogri
    'kok': ['omnivoice', 'indic_parler', 'mms_tts', 'espeak'],   # Konkani
    'mai': ['omnivoice', 'indic_parler', 'mms_tts', 'espeak'],   # Maithili
    'mni': ['omnivoice', 'indic_parler', 'mms_tts', 'espeak'],   # Manipuri
    'sat': ['omnivoice', 'indic_parler', 'mms_tts', 'espeak'],   # Santali
    'ks':  ['omnivoice', 'mms_tts', 'espeak'],                    # Kashmiri
    # Misc that were previously routed via _DEFAULT_PREFERENCE only.
    'lv': ['omnivoice', 'mms_tts', 'espeak'],          # Latvian
    'sr': ['omnivoice', 'mms_tts', 'chatterbox_ml', 'espeak'],   # Serbian
    'zh-cn': ['omnivoice', 'melotts', 'cosyvoice3', 'f5_tts', 'xtts_v2', 'chatterbox_ml', 'mms_tts', 'espeak'],
}

# Fallback for unlisted languages — omnivoice covers 646 + mms_tts covers
# 1100+, so this is reached only when both are uninstalled / can't fit.
# chatterbox_ml is the heaviest local clone, espeak is the absolute floor.
_DEFAULT_PREFERENCE = ['omnivoice', 'mms_tts', 'chatterbox_ml', 'espeak']


# ═══════════════════════════════════════════════════════════════
# Route result
# ═══════════════════════════════════════════════════════════════

class TTSLocation(Enum):
    LOCAL = "local"
    HIVE_PEER = "hive_peer"
    CLOUD = "cloud"


@dataclass
class TTSCandidate:
    """A scored TTS engine candidate."""
    engine: TTSEngineSpec
    location: TTSLocation
    device: str                 # 'gpu', 'cpu', 'cloud'
    estimated_latency_ms: int
    quality_score: float
    peer_address: Optional[str] = None  # if location == HIVE_PEER
    warnings: List[str] = field(default_factory=list)


@dataclass
class TTSResult:
    """Result of a TTS synthesis."""
    path: str
    duration: float
    engine_id: str
    device: str
    location: str
    latency_ms: float
    sample_rate: int
    voice: str
    quality_score: float
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            'path': self.path,
            'duration': self.duration,
            'engine': self.engine_id,
            'device': self.device,
            'location': self.location,
            'latency_ms': self.latency_ms,
            'sample_rate': self.sample_rate,
            'voice': self.voice,
            'quality_score': self.quality_score,
        }
        if self.warnings:
            d['warnings'] = self.warnings
        if self.error:
            d['error'] = self.error
        return d


# ═══════════════════════════════════════════════════════════════
# Language Detection
# ═══════════════════════════════════════════════════════════════

def detect_language(text: str) -> str:
    """Detect language of text. Returns ISO 639-1 code (e.g. 'en', 'hi').

    Uses langdetect if available, falls back to heuristics.
    """
    if not text or not text.strip():
        return 'en'
    try:
        from langdetect import detect
        return detect(text)
    except ImportError:
        pass
    except Exception:
        pass

    # Heuristic fallback: check Unicode script ranges
    sample = text[:500]
    devanagari = sum(1 for c in sample if '\u0900' <= c <= '\u097F')
    cjk = sum(1 for c in sample if '\u4E00' <= c <= '\u9FFF')
    hangul = sum(1 for c in sample if '\uAC00' <= c <= '\uD7AF')
    katakana = sum(1 for c in sample if '\u30A0' <= c <= '\u30FF')
    hiragana = sum(1 for c in sample if '\u3040' <= c <= '\u309F')
    tamil = sum(1 for c in sample if '\u0B80' <= c <= '\u0BFF')
    telugu = sum(1 for c in sample if '\u0C00' <= c <= '\u0C7F')
    arabic = sum(1 for c in sample if '\u0600' <= c <= '\u06FF')
    cyrillic = sum(1 for c in sample if '\u0400' <= c <= '\u04FF')
    bengali = sum(1 for c in sample if '\u0980' <= c <= '\u09FF')
    gujarati = sum(1 for c in sample if '\u0A80' <= c <= '\u0AFF')
    kannada = sum(1 for c in sample if '\u0C80' <= c <= '\u0CFF')
    malayalam = sum(1 for c in sample if '\u0D00' <= c <= '\u0D7F')

    threshold = max(3, len(sample) * 0.1)
    if devanagari > threshold:
        return 'hi'
    if tamil > threshold:
        return 'ta'
    if telugu > threshold:
        return 'te'
    if bengali > threshold:
        return 'bn'
    if gujarati > threshold:
        return 'gu'
    if kannada > threshold:
        return 'kn'
    if malayalam > threshold:
        return 'ml'
    if cjk > threshold:
        return 'zh'
    if hangul > threshold:
        return 'ko'
    if (katakana + hiragana) > threshold:
        return 'ja'
    if arabic > threshold:
        return 'ar'
    if cyrillic > threshold:
        return 'ru'
    return 'en'


# ═══════════════════════════════════════════════════════════════
# Engine Availability Detection
# ═══════════════════════════════════════════════════════════════

# Cache for engine availability (avoid repeated import checks)
_engine_available_cache: Dict[str, Tuple[bool, float]] = {}
_CACHE_TTL = 60.0  # seconds


def _is_engine_installed(engine_id: str) -> bool:
    """Check if a TTS engine's Python package is available.

    TODO REFACTOR: move to model_catalog as ModelEntry.is_installed() —
    a model that isn't pip-importable shouldn't be selectable by any caller.
    """
    now = time.time()
    cached = _engine_available_cache.get(engine_id)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    spec = ENGINE_REGISTRY.get(engine_id)
    if not spec or not spec.tool_module:
        _engine_available_cache[engine_id] = (False, now)
        return False

    available = False
    try:
        if engine_id == 'espeak':
            # espeak availability checked via shutil
            import shutil
            available = shutil.which('espeak-ng') is not None or shutil.which('espeak') is not None
        elif engine_id == 'pocket_tts':
            from integrations.service_tools.pocket_tts_tool import pocket_tts_synthesize  # noqa: F401
            available = True
        elif engine_id == 'luxtts':
            from integrations.service_tools.luxtts_tool import luxtts_synthesize  # noqa: F401
            available = True
        elif engine_id == 'cosyvoice3':
            from integrations.service_tools.cosyvoice_tool import cosyvoice_synthesize  # noqa: F401
            available = True
        elif engine_id == 'indic_parler':
            from integrations.service_tools.indic_parler_tool import indic_parler_synthesize  # noqa: F401
            available = True
        elif engine_id in ('chatterbox_turbo', 'chatterbox_ml'):
            from integrations.service_tools.chatterbox_tool import chatterbox_synthesize  # noqa: F401
            available = True
        elif engine_id == 'f5_tts':
            from integrations.service_tools.f5_tts_tool import f5_synthesize  # noqa: F401
            available = True
        elif engine_id == 'kokoro':
            from integrations.service_tools.kokoro_tool import kokoro_synthesize  # noqa: F401
            available = True
        elif engine_id == 'melotts':
            # `melotts` PyPI package ships the `melo` import root.
            import importlib.util as _ils
            available = _ils.find_spec('melo') is not None
        elif engine_id == 'xtts_v2':
            # `coqui-tts` PyPI package ships `from TTS.api import TTS`.
            import importlib.util as _ils
            available = _ils.find_spec('TTS') is not None
        elif engine_id == 'mms_tts':
            # transformers is bundled; check the VitsModel symbol so we
            # detect outright-broken transformers installs early.
            from transformers import VitsModel  # noqa: F401
            available = True
        elif engine_id == 'makeittalk':
            import os
            available = bool(os.environ.get('MAKEITTALK_API_URL'))
    except (ImportError, Exception):
        available = False

    _engine_available_cache[engine_id] = (available, now)
    return available


def _get_gpu_info() -> Dict[str, Any]:
    """Get GPU info from VRAMManager (cached singleton)."""
    try:
        from integrations.service_tools.vram_manager import get_vram_manager
        mgr = get_vram_manager()
        return mgr.detect_gpu()
    except (ImportError, Exception):
        return {'cuda_available': False, 'total_gb': 0, 'free_gb': 0}


def _can_fit_on_gpu(engine_id: str) -> bool:  # TODO REFACTOR: remove — duplicates catalog.matches_compute()
    """Check if this engine's model fits in available VRAM."""
    spec = ENGINE_REGISTRY.get(engine_id)
    if not spec or not spec.vram_key:
        return False
    try:
        from integrations.service_tools.vram_manager import get_vram_manager
        return get_vram_manager().can_fit(spec.vram_key)
    except (ImportError, Exception):
        return False


def _get_compute_policy() -> Dict[str, Any]:
    """Get user's compute policy (local_only / local_preferred / any)."""
    try:
        from integrations.agent_engine.compute_config import get_compute_policy
        return get_compute_policy()
    except (ImportError, Exception):
        return {'compute_policy': 'local_preferred'}


# ═══════════════════════════════════════════════════════════════
# Hive Peer TTS Offload
# ═══════════════════════════════════════════════════════════════

def _find_hive_peer_for_tts(language: str) -> Optional[Dict[str, Any]]:
    # TODO REFACTOR: move to orchestrator as find_peer_for(model_type, language) —
    # hive peer offloading applies to all model types (STT, VLM, LLM), not just TTS.
    """Find a hive peer with GPU that can serve TTS for this language.

    Returns peer info dict or None.
    """
    try:
        from integrations.agent_engine.compute_mesh_service import get_compute_mesh
        mesh = get_compute_mesh()
        if not mesh or not mesh.peers:
            return None

        for peer in mesh.peers.values():
            if not peer.available_compute or peer.available_compute < 0.1:
                continue
            # Peer has GPU and capacity
            caps = peer.capabilities or {}
            if caps.get('gpu'):
                return {
                    'peer_id': peer.peer_id,
                    'address': peer.address,
                    'latency_ms': peer.latency_ms or 500,
                    'gpu': caps.get('gpu', 'unknown'),
                }
        return None
    except (ImportError, Exception):
        return None


def _offload_tts_to_peer(peer: Dict, text: str, language: str,
                         voice: Optional[str] = None) -> Optional[Dict]:
    """Offload TTS synthesis to a hive peer via compute mesh (DRY — reuses mesh service)."""
    try:
        from integrations.agent_engine.compute_mesh_service import get_compute_mesh
        mesh = get_compute_mesh()
        if not mesh:
            return None
        result = mesh.offload_to_best_peer(
            model_type='tts',
            prompt=text,
            options={'language': language, 'voice': voice or 'default'},
        )
        if result and 'error' not in result:
            return result
    except (ImportError, Exception) as e:
        logger.debug("Hive TTS offload failed: %s", e)
    return None


# ═══════════════════════════════════════════════════════════════
# TTSRouter — the brain
# ═══════════════════════════════════════════════════════════════

class TTSRouter:
    """Smart TTS engine selector and dispatcher.

    Considers language, hardware, compute policy, latency, and hive peers
    to select the best engine for each synthesis request.
    """

    def select_engines(  # TODO REFACTOR: remove — catalog.select_best() is the single selector.
        # Language preferences feed into catalog via populate_tts_catalog()'s language_priority.
        # Move _is_engine_installed() to catalog, _find_hive_peer to orchestrator.
        self,
        text: str,
        language: Optional[str] = None,
        voice: Optional[str] = None,
        urgency: str = 'normal',
        require_clone: bool = False,
    ) -> List[TTSCandidate]:
        """Select and rank TTS engines for the given request.

        Args:
            text: Text to synthesize
            language: ISO 639-1 code (auto-detected if None)
            voice: Voice reference (triggers clone-capable filter)
            urgency: 'instant' (fastest), 'normal', 'quality' (best quality)
            require_clone: Only return engines with voice cloning

        Returns:
            Ranked list of TTSCandidate (best first), never empty
        """
        # Step 1: Detect language
        lang = language or detect_language(text)
        lang = lang[:2].lower()  # normalize to 2-char code

        # Step 2: Get preferred engines for this language
        preferred = LANG_ENGINE_PREFERENCE.get(lang, _DEFAULT_PREFERENCE)

        # Step 3: Gather constraints
        gpu_info = _get_gpu_info()
        has_gpu = gpu_info.get('cuda_available', False)
        policy = _get_compute_policy()
        compute_mode = policy.get('compute_policy', 'local_preferred')

        # Step 4: Score each candidate
        candidates: List[TTSCandidate] = []
        seen = set()

        for engine_id in preferred:
            if engine_id in seen:
                continue
            seen.add(engine_id)

            spec = ENGINE_REGISTRY.get(engine_id)
            if not spec:
                continue

            # Voice cloning filter
            if require_clone and not spec.voice_clone:
                continue

            warnings: List[str] = []

            # --- LOCAL availability ---
            if spec.device == TTSDevice.CLOUD:
                # Cloud engines: skip if local_only
                if compute_mode == 'local_only':
                    continue
                if _is_engine_installed(engine_id):
                    candidates.append(TTSCandidate(
                        engine=spec,
                        location=TTSLocation.CLOUD,
                        device='cloud',
                        estimated_latency_ms=spec.latency_cloud_ms,
                        quality_score=spec.quality,
                    ))
                continue

            if spec.device == TTSDevice.GPU_ONLY:
                if has_gpu and _can_fit_on_gpu(engine_id):
                    if _is_engine_installed(engine_id):
                        candidates.append(TTSCandidate(
                            engine=spec,
                            location=TTSLocation.LOCAL,
                            device='gpu',
                            estimated_latency_ms=spec.latency_gpu_ms,
                            quality_score=spec.quality,
                        ))
                        continue

                # GPU engine not available locally — try hive peer
                if compute_mode != 'local_only':
                    peer = _find_hive_peer_for_tts(lang)
                    if peer:
                        candidates.append(TTSCandidate(
                            engine=spec,
                            location=TTSLocation.HIVE_PEER,
                            device='gpu',
                            estimated_latency_ms=spec.latency_gpu_ms + peer['latency_ms'],
                            quality_score=spec.quality * 0.95,  # slight penalty for network
                            peer_address=peer['address'],
                            warnings=[f"Offloaded to hive peer {peer['peer_id']}"],
                        ))
                continue

            if spec.device == TTSDevice.GPU_PREFERRED:
                if not _is_engine_installed(engine_id):
                    continue
                if has_gpu and _can_fit_on_gpu(engine_id):
                    candidates.append(TTSCandidate(
                        engine=spec,
                        location=TTSLocation.LOCAL,
                        device='gpu',
                        estimated_latency_ms=spec.latency_gpu_ms,
                        quality_score=spec.quality,
                    ))
                else:
                    # CPU fallback
                    candidates.append(TTSCandidate(
                        engine=spec,
                        location=TTSLocation.LOCAL,
                        device='cpu',
                        estimated_latency_ms=spec.latency_cpu_ms,
                        quality_score=spec.quality * 0.9,  # CPU quality slightly lower
                        warnings=['Running on CPU (slower, install GPU for better perf)'],
                    ))
                continue

            if spec.device == TTSDevice.CPU_ONLY:
                if _is_engine_installed(engine_id):
                    candidates.append(TTSCandidate(
                        engine=spec,
                        location=TTSLocation.LOCAL,
                        device='cpu',
                        estimated_latency_ms=spec.latency_cpu_ms,
                        quality_score=spec.quality,
                    ))
                continue

        # Step 5: Always ensure espeak as ultimate fallback
        if not any(c.engine.engine_id == 'espeak' for c in candidates):
            espeak_spec = ENGINE_REGISTRY['espeak']
            candidates.append(TTSCandidate(
                engine=espeak_spec,
                location=TTSLocation.LOCAL,
                device='cpu',
                estimated_latency_ms=10,
                quality_score=espeak_spec.quality,
                warnings=['Fallback: no neural TTS available for this language'],
            ))

        # Step 6: Sort by urgency-weighted score
        if urgency == 'instant':
            # Minimize latency — instant response
            candidates.sort(key=lambda c: (c.estimated_latency_ms, -c.quality_score))
        elif urgency == 'quality':
            # Maximize quality — don't care about latency
            candidates.sort(key=lambda c: (-c.quality_score, c.estimated_latency_ms))
        else:
            # Balance: quality * 0.6 + inverse_latency * 0.4
            max_latency = max(c.estimated_latency_ms for c in candidates) or 1
            candidates.sort(key=lambda c: -(
                c.quality_score * 0.6 +
                (1 - c.estimated_latency_ms / max_latency) * 0.4
            ))

        return candidates

    def synthesize(
        self,
        text: str,
        language: Optional[str] = None,
        voice: Optional[str] = None,
        output_path: Optional[str] = None,
        source: Optional[str] = None,
        urgency: str = 'normal',
        engine_override: Optional[str] = None,
    ) -> TTSResult:
        """Synthesize text using the best available TTS engine.

        Tries engines in ranked order until one succeeds.

        Args:
            text: Text to synthesize
            language: ISO 639-1 code (auto-detected if None)
            voice: Voice reference for cloning (path or saved name)
            output_path: Where to write WAV (auto-generated if None)
            source: Context hint (e.g. 'chat_response', 'greeting') —
                    auto-maps to urgency via SOURCE_URGENCY
            urgency: 'instant' | 'normal' | 'quality' (used if source not set)
            engine_override: Force a specific engine (bypasses selection)

        Returns:
            TTSResult with synthesis details
        """
        # Auto-infer urgency from source hint
        if source:
            urgency = SOURCE_URGENCY.get(source, urgency)
        if not text or not text.strip():
            return TTSResult(
                path='', duration=0, engine_id='none', device='none',
                location='none', latency_ms=0, sample_rate=0, voice='',
                quality_score=0, error='Text is required',
            )

        lang = language or detect_language(text)

        # Normalize numbers, currency, URLs, units to spoken form BEFORE
        # engine selection — every TTS engine benefits (single converging
        # path).  Latency-sensitive ('instant' urgency) skips the LLM
        # fallback but keeps the fast rule pass.
        try:
            from integrations.channels.media.tts_text_normalizer import (
                normalize_for_tts,
            )
            text = normalize_for_tts(
                text, lang, use_llm=(urgency != 'instant'),
            )
        except Exception as _e:  # never let normalization block synthesis
            logger.debug(f'tts normalization skipped: {_e}')

        require_clone = voice is not None and voice not in ('default', '', None)

        # Engine override
        if engine_override and engine_override in ENGINE_REGISTRY:
            spec = ENGINE_REGISTRY[engine_override]
            candidates = [TTSCandidate(
                engine=spec,
                location=TTSLocation.LOCAL,
                device='gpu' if spec.device in (TTSDevice.GPU_ONLY, TTSDevice.GPU_PREFERRED) else 'cpu',
                estimated_latency_ms=spec.latency_gpu_ms or spec.latency_cpu_ms,
                quality_score=spec.quality,
            )]
        else:
            candidates = self.select_engines(
                text, lang, voice, urgency, require_clone,
            )

        # Try each candidate in order
        all_warnings = []
        for candidate in candidates:
            t0 = time.time()
            try:
                result = self._execute(candidate, text, lang, voice, output_path)
                elapsed = (time.time() - t0) * 1000
                if result and not result.get('error'):
                    all_warnings.extend(candidate.warnings)
                    return TTSResult(
                        path=result.get('path', ''),
                        duration=result.get('duration', 0),
                        engine_id=candidate.engine.engine_id,
                        device=candidate.device,
                        location=candidate.location.value,
                        latency_ms=round(elapsed, 1),
                        sample_rate=result.get('sample_rate', candidate.engine.sample_rate),
                        voice=result.get('voice', voice or 'default'),
                        quality_score=candidate.quality_score,
                        warnings=all_warnings,
                    )
                else:
                    err = result.get('error', 'unknown') if result else 'no result'
                    all_warnings.append(
                        f"{candidate.engine.engine_id} failed: {err}"
                    )
            except Exception as e:
                all_warnings.append(f"{candidate.engine.engine_id} error: {e}")
                logger.debug("TTS engine %s failed: %s", candidate.engine.engine_id, e)

        # All engines failed
        return TTSResult(
            path='', duration=0, engine_id='none', device='none',
            location='none', latency_ms=0, sample_rate=0, voice='',
            quality_score=0, warnings=all_warnings,
            error='All TTS engines failed',
        )

    def _execute(
        self, candidate: TTSCandidate, text: str,
        language: str, voice: Optional[str], output_path: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Execute TTS on a specific candidate engine."""

        # Hive peer offload
        if candidate.location == TTSLocation.HIVE_PEER:
            peer_info = {
                'address': candidate.peer_address,
                'peer_id': 'hive',
                'latency_ms': candidate.estimated_latency_ms,
            }
            result = _offload_tts_to_peer(peer_info, text, language, voice)
            return result

        # Cloud (MakeItTalk)
        if candidate.location == TTSLocation.CLOUD:
            return self._execute_makeittalk(text, voice)

        # Local engine
        engine_id = candidate.engine.engine_id
        spec = candidate.engine

        if engine_id == 'luxtts':
            return self._call_luxtts(text, voice, output_path, candidate.device)
        elif engine_id == 'pocket_tts':
            return self._call_pocket_tts(text, voice, output_path)
        elif engine_id == 'espeak':
            return self._call_espeak(text, language, output_path)
        elif engine_id == 'cosyvoice3':
            return self._call_gpu_engine(
                'integrations.service_tools.cosyvoice_tool',
                'cosyvoice_synthesize',
                text, language, voice, output_path,
            )
        elif engine_id == 'indic_parler':
            return self._call_gpu_engine(
                'integrations.service_tools.indic_parler_tool',
                'indic_parler_synthesize',
                text, language, voice, output_path,
            )
        elif engine_id == 'chatterbox_turbo':
            return self._call_gpu_engine(
                'integrations.service_tools.chatterbox_tool',
                'chatterbox_synthesize',
                text, language, voice, output_path,
            )
        elif engine_id == 'chatterbox_ml':
            return self._call_gpu_engine(
                'integrations.service_tools.chatterbox_tool',
                'chatterbox_ml_synthesize',
                text, language, voice, output_path,
            )
        elif engine_id == 'f5_tts':
            return self._call_gpu_engine(
                'integrations.service_tools.f5_tts_tool',
                'f5_synthesize',
                text, language, voice, output_path,
            )
        elif engine_id == 'kokoro':
            return self._call_gpu_engine(
                'integrations.service_tools.kokoro_tool',
                'kokoro_synthesize',
                text, language, voice, output_path,
            )
        elif engine_id == 'melotts':
            return self._call_gpu_engine(
                'integrations.service_tools.melotts_tool',
                'melotts_synthesize',
                text, language, voice, output_path,
            )
        elif engine_id == 'xtts_v2':
            return self._call_gpu_engine(
                'integrations.service_tools.xtts_tool',
                'xtts_synthesize',
                text, language, voice, output_path,
            )
        elif engine_id == 'mms_tts':
            return self._call_gpu_engine(
                'integrations.service_tools.mms_tts_tool',
                'mms_tts_synthesize',
                text, language, voice, output_path,
            )
        return {'error': f'Unknown engine: {engine_id}'}

    def _call_luxtts(self, text, voice, output_path, device):
        from integrations.service_tools.luxtts_tool import luxtts_synthesize
        result_str = luxtts_synthesize(
            text, voice_audio=voice, output_path=output_path, device=device,
        )
        return json.loads(result_str)

    def _call_pocket_tts(self, text, voice, output_path):
        from integrations.service_tools.pocket_tts_tool import pocket_tts_synthesize
        voice_name = voice if voice and voice != 'default' else 'alba'
        result_str = pocket_tts_synthesize(text, voice_name, output_path)
        return json.loads(result_str)

    def _call_espeak(self, text, language, output_path):
        """Call espeak-ng via pocket_tts_tool (DRY — reuses existing impl)."""
        import os

        if not output_path:
            out_dir = os.environ.get('TTS_TEMP_DIR', '/tmp/tts')
            os.makedirs(out_dir, exist_ok=True)
            output_path = os.path.join(out_dir, f'espeak_{int(time.time()*1000)}.wav')

        try:
            from integrations.service_tools.pocket_tts_tool import _espeak_synthesize
            espeak_lang = language if language else 'en'
            if _espeak_synthesize(text[:5000], output_path, voice=espeak_lang):
                return {
                    'path': output_path,
                    'duration': len(text.split()) / 150 * 60,  # estimate
                    'sample_rate': 22050,
                    'voice': espeak_lang,
                    'engine': 'espeak-ng',
                }
            return {'error': 'espeak-ng not installed'}
        except (ImportError, Exception):
            return {'error': 'espeak-ng not available'}

    def _call_gpu_engine(self, module_path, function_name, text, language,
                         voice, output_path):
        """Generic caller for GPU TTS service tools."""
        import importlib
        try:
            mod = importlib.import_module(module_path)
            fn = getattr(mod, function_name)
            result_str = fn(text, language=language, voice=voice,
                            output_path=output_path)
            return json.loads(result_str)
        except ImportError as e:
            return {'error': f'{module_path} not installed: {e}'}
        except Exception as e:
            return {'error': str(e)}

    def _execute_makeittalk(self, text, voice):
        """Cloud TTS via MakeItTalk API."""
        import os
        base_url = os.environ.get('MAKEITTALK_API_URL')
        if not base_url:
            return {'error': 'MAKEITTALK_API_URL not set'}
        try:
            import requests
            resp = requests.post(
                f"{base_url}/video-gen/",
                json={
                    'text': text,
                    'voiceName': voice or 'af_bella',
                    'audio_only': True,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                audio_url = data.get('audio_url') or data.get('url', '')
                return {
                    'path': audio_url,
                    'duration': data.get('duration', 0),
                    'voice': voice or 'af_bella',
                    'engine': 'makeittalk',
                    'sample_rate': 24000,
                }
            return {'error': f'MakeItTalk HTTP {resp.status_code}'}
        except Exception as e:
            return {'error': f'MakeItTalk: {e}'}

    def get_engine_status(self) -> List[Dict[str, Any]]:
        """Report status of all TTS engines for diagnostics."""
        gpu_info = _get_gpu_info()
        has_gpu = gpu_info.get('cuda_available', False)
        statuses = []

        for eid, spec in ENGINE_REGISTRY.items():
            installed = _is_engine_installed(eid)
            can_run = False
            device = 'n/a'

            if spec.device == TTSDevice.CPU_ONLY:
                can_run = installed
                device = 'cpu'
            elif spec.device == TTSDevice.GPU_ONLY:
                can_run = installed and has_gpu and _can_fit_on_gpu(eid)
                device = 'gpu' if can_run else 'n/a'
            elif spec.device == TTSDevice.GPU_PREFERRED:
                can_run = installed
                device = 'gpu' if (has_gpu and _can_fit_on_gpu(eid)) else 'cpu'
            elif spec.device == TTSDevice.CLOUD:
                can_run = installed
                device = 'cloud'

            statuses.append({
                'engine': eid,
                'installed': installed,
                'can_run': can_run,
                'device': device,
                'languages': list(spec.languages),
                'quality': spec.quality,
                'voice_clone': spec.voice_clone,
                'vram_gb': spec.vram_key,
            })

        return statuses

    def get_all_voices(self) -> List[Dict[str, Any]]:
        """Aggregate available voices from all installed TTS engines."""
        voices: List[Dict[str, Any]] = []
        try:
            from integrations.service_tools.pocket_tts_tool import (
                _BUILTIN_VOICES,
            )
            for v in _BUILTIN_VOICES:
                voices.append({'id': v, 'engine': 'pocket_tts', 'type': 'builtin'})
        except (ImportError, Exception):
            pass
        try:
            from integrations.service_tools.luxtts_tool import luxtts_list_voices
            import json as _json
            result = _json.loads(luxtts_list_voices())
            for v in result.get('voices', []):
                voices.append({'id': v.get('id', ''), 'engine': 'luxtts', 'type': 'cloned'})
        except (ImportError, Exception):
            pass
        return voices


# ═══════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════

_router_instance: Optional[TTSRouter] = None


def get_tts_router() -> TTSRouter:
    """Get the singleton TTS router."""
    global _router_instance
    if _router_instance is None:
        _router_instance = TTSRouter()
    return _router_instance


# ═══════════════════════════════════════════════════════════════
# ModelCatalog integration — populate_tts_catalog()
# ═══════════════════════════════════════════════════════════════

# Reflection-dispatch contract for catalog entries that have NO
# `tool_module` (pure-JSON model registration via admin UI / hive
# federation / model_catalog.json edit).  An entry without `tool_module`
# MUST declare every field below in its `capabilities` dict — otherwise
# the dispatcher has no way to know how to instantiate the class, marshal
# the request, or normalize the return.  See task #58 for the full
# rationale; the schema is finalized at 5 fields, no more.
_REFLECTION_FIELDS: Tuple[str, ...] = (
    'import_path',     # 'pkg.module:ClassName'
    'init_args',       # dict — kwargs for ClassName(**init_args); {} OK
    'synth_method',    # str — instance method name
    'params_map',      # dict — {payload_key → method_kwarg}
    'output_format',   # canonical id (see _OUTPUT_FORMATS below)
)

# Canonical return-shape identifiers the reflection dispatcher knows
# how to normalize into a wire-format wav (or path).  Engines that
# return shapes outside this set MUST use the `tool_module` escape
# hatch instead — the dispatcher won't guess.
_OUTPUT_FORMATS: Tuple[str, ...] = (
    'wav_bytes',       # bytes object holding a WAV-formatted byte stream
    'numpy_24k',       # 1-D float32 numpy array @ 24 kHz mono
    'file_path',       # str path to a wav file the engine wrote
    'bytesio',         # io.BytesIO containing wav bytes
)


def _validate_engine_caps(caps: Dict[str, Any]) -> Optional[str]:
    """Validate a TTS catalog entry's capabilities dict.

    Returns None when the entry is dispatchable, OR a human-readable
    error string when it is not.  Two valid shapes:

      1. Python-tool path (escape hatch):
            caps['tool_module'] = 'pkg.module'  # required
         The entry will be dispatched via the existing
         `gpu_worker._dispatch_and_run` path: import the module, pick
         up `_load[_<variant>]` / `_synthesize[_<variant>]` callbacks
         by convention.  This is what every code-shipped engine in
         ENGINE_REGISTRY uses today.

      2. Pure-config / reflection path:
            caps lacks tool_module BUT declares ALL of _REFLECTION_FIELDS.
         The dispatcher will use reflection to instantiate the class
         and call the synth method — no .py file needed for adding
         new models that fit a homogeneous load+method API (Kokoro,
         Pocket-TTS, etc., evaluated empirically per engine).

    Validation fires at INGEST time (populate_tts_catalog upsert path
    AND _catalog_entry_to_spec read path) so a malformed entry cannot
    reach the dispatcher.  This guards against the "user discovers the
    error only when they request the voice" failure mode.
    """
    if not isinstance(caps, dict):
        return f'capabilities must be a dict, got {type(caps).__name__}'

    if caps.get('tool_module'):
        # Python-tool entry — tool_module on its own is sufficient.  The
        # dispatcher will pick up _load / _synthesize via convention.
        return None

    # Reflection entry — every field is required.  No partial schemas.
    missing = [f for f in _REFLECTION_FIELDS if f not in caps]
    if missing:
        return (
            f'entry has no tool_module and is missing reflection fields '
            f'{missing}; reflection dispatch needs the full 5-field '
            f'contract: {list(_REFLECTION_FIELDS)}'
        )

    # Cheap shape sanity — early-fail with a precise message rather than
    # let the dispatcher trip on a bad type at synth time.
    if not isinstance(caps.get('init_args'), dict):
        return f'init_args must be a dict, got {type(caps.get("init_args")).__name__}'
    if not isinstance(caps.get('params_map'), dict):
        return f'params_map must be a dict, got {type(caps.get("params_map")).__name__}'
    if not isinstance(caps.get('synth_method'), str) or not caps['synth_method']:
        return 'synth_method must be a non-empty str'
    if not isinstance(caps.get('import_path'), str) or ':' not in caps['import_path']:
        return (
            f'import_path must be "pkg.module:ClassName", got '
            f'{caps.get("import_path")!r}'
        )
    if caps.get('output_format') not in _OUTPUT_FORMATS:
        return (
            f'output_format must be one of {list(_OUTPUT_FORMATS)}, got '
            f'{caps.get("output_format")!r}'
        )
    return None


# Human-readable display names for each engine (used in admin UI)
_ENGINE_DISPLAY_NAMES: Dict[str, str] = {
    'chatterbox_turbo': 'Chatterbox Turbo (GPU, English, voice-clone)',
    'luxtts':           'LuxTTS (CPU, English, voice-clone)',
    'cosyvoice3':       'CosyVoice 3 (GPU, multilingual, voice-clone)',
    'f5_tts':           'F5-TTS (GPU, EN/ZH, voice-clone)',
    'indic_parler':     'Indic Parler-TTS (GPU, 22 Indic languages)',
    'chatterbox_ml':    'Chatterbox Multilingual (GPU, 23 languages, voice-clone)',
    'pocket_tts':       'Pocket TTS (CPU, English, voice-clone)',
    'kokoro':           'Kokoro 82M (CPU/GPU, English, neural)',
    'espeak':           'eSpeak-NG (CPU, 100+ languages, instant fallback)',
    'makeittalk':       'MakeItTalk (Cloud, English)',
    'melotts':          'MeloTTS (CPU/GPU, 6 langs, neural)',
    'xtts_v2':          'XTTS-v2 (GPU, 17 langs, voice-clone)',
    'mms_tts':          'MMS-TTS (CPU/GPU, 50+ langs via VITS)',
}

# Extra capabilities per engine that don't map 1-to-1 onto TTSEngineSpec fields
_ENGINE_EXTRA_CAPS: Dict[str, Dict[str, Any]] = {
    'chatterbox_turbo': {
        'streaming': False,
        'paralinguistic': ['emotion_happy', 'emotion_sad', 'emotion_angry',
                           'emotion_surprised', 'laughing', 'whispering'],
        'emotion_tags': True,
    },
    'luxtts': {
        'streaming': False,
        'paralinguistic': [],
        'emotion_tags': False,
    },
    'cosyvoice3': {
        'streaming': True,
        'paralinguistic': ['emotion_happy', 'emotion_sad', 'whispering'],
        'emotion_tags': True,
    },
    'f5_tts': {
        'streaming': False,
        'paralinguistic': [],
        'emotion_tags': False,
    },
    'indic_parler': {
        'streaming': False,
        'paralinguistic': [],
        'emotion_tags': False,
    },
    'chatterbox_ml': {
        'streaming': False,
        'paralinguistic': ['emotion_happy', 'emotion_sad', 'whispering'],
        'emotion_tags': True,
    },
    'pocket_tts': {
        'streaming': False,
        'paralinguistic': [],
        'emotion_tags': False,
    },
    'kokoro': {
        'streaming': False,
        'paralinguistic': [],
        'emotion_tags': False,
    },
    'espeak': {
        'streaming': False,
        'paralinguistic': [],
        'emotion_tags': False,
    },
    'makeittalk': {
        'streaming': False,
        'paralinguistic': [],
        'emotion_tags': False,
    },
    'melotts': {
        'streaming': False,
        'paralinguistic': [],
        'emotion_tags': False,
    },
    'xtts_v2': {
        'streaming': False,
        'paralinguistic': [],
        'emotion_tags': False,
    },
    'mms_tts': {
        'streaming': False,
        'paralinguistic': [],
        'emotion_tags': False,
    },
}

# Device → backend string mapping for ModelEntry.backend field
_DEVICE_TO_BACKEND: Dict[str, str] = {
    TTSDevice.GPU_ONLY.value:       'torch',
    TTSDevice.GPU_PREFERRED.value:  'torch',
    TTSDevice.CPU_ONLY.value:       'in_process',
    TTSDevice.CLOUD.value:          'api',
}

# Device → supports_gpu / supports_cpu flags
_DEVICE_TO_COMPUTE: Dict[str, Tuple[bool, bool]] = {
    # (supports_gpu, supports_cpu)
    TTSDevice.GPU_ONLY.value:       (True,  False),
    TTSDevice.GPU_PREFERRED.value:  (True,  True),
    TTSDevice.CPU_ONLY.value:       (False, True),
    TTSDevice.CLOUD.value:          (False, False),
}

# DEPRECATED: VRAM specs now live in vram_manager.VRAM_BUDGETS (single
# source of truth). Use _engine_vram_gb(engine_id) helper below.
# This dict is kept for backward compatibility but should NOT be edited.
_ENGINE_VRAM_GB: Dict[str, float] = {}  # populated lazily by _engine_vram_gb


def _engine_vram_gb(engine_id: str) -> float:
    """Single source of truth for engine VRAM requirement.

    Reads from vram_manager.VRAM_BUDGETS — the canonical specs.
    The vram_manager key convention is 'tts_<engine_id>' (e.g. 'tts_indic_parler').
    Returns 0.0 only if engine has no GPU requirement (CPU-only engine).
    Logs a warning if engine is GPU-capable but missing from VRAM_BUDGETS
    (catches drift between the two registries).
    """
    if engine_id in _ENGINE_VRAM_GB:
        return _ENGINE_VRAM_GB[engine_id]
    try:
        from integrations.service_tools.vram_manager import VRAM_BUDGETS
        key = f'tts_{engine_id}'
        if key in VRAM_BUDGETS:
            vram = VRAM_BUDGETS[key][0]  # (gpu_gb, cpu_gb)
            _ENGINE_VRAM_GB[engine_id] = vram
            return vram
        # Engine not registered in vram_manager — log once, assume CPU
        logger.debug(
            "TTS engine %r has no VRAM_BUDGETS entry (key=%r) — "
            "assuming CPU-only. Add to vram_manager.VRAM_BUDGETS if GPU-capable.",
            engine_id, key,
        )
    except ImportError:
        logger.debug("vram_manager unavailable, assuming CPU-only for %r", engine_id)
    _ENGINE_VRAM_GB[engine_id] = 0.0
    return 0.0

# Approximate disk footprint per engine (GB)
_ENGINE_DISK_GB: Dict[str, float] = {
    'chatterbox_turbo': 2.0,
    'luxtts':           0.5,
    'cosyvoice3':       3.5,
    'f5_tts':           2.5,
    'indic_parler':     4.0,
    'chatterbox_ml':    3.0,
    'pocket_tts':       0.1,
    'espeak':           0.05,
    'makeittalk':       0.0,
    'melotts':          1.5,    # 6 per-lang checkpoints, ~250 MB each
    'xtts_v2':          2.0,    # weights + speakers + config
    'mms_tts':          0.2,    # ~150 MB per lang lazy-downloaded
}

# Approximate RAM needed for CPU-capable engines (GB)
_ENGINE_RAM_GB: Dict[str, float] = {
    'chatterbox_turbo': 2.0,
    'luxtts':           2.0,
    'cosyvoice3':       4.0,
    'f5_tts':           2.0,
    'indic_parler':     4.0,
    'chatterbox_ml':    4.0,
    'pocket_tts':       0.5,
    'espeak':           0.1,
    'makeittalk':       0.1,
    'melotts':          2.0,
    'xtts_v2':          3.0,
    'mms_tts':          1.5,
}


def populate_tts_catalog(catalog) -> int:
    """Convert ENGINE_REGISTRY into ModelEntry objects and register them.

    Called by ModelCatalog.populate_from_subsystems() via the populator
    plugin mechanism — keeps tts_router as the single source of truth for
    TTS engine capabilities.

    Validation contract (#58): admin- or hive-supplied catalog entries
    that exist BEFORE this populator runs are validated against
    `_validate_engine_caps`.  Invalid entries are removed from the
    catalog with a logged WARNING — they cannot reach the dispatcher.
    This is the "fail-fast at catalog ingest, not synth time" half of
    the contract; the other half (validation on every read) lives in
    `_catalog_entry_to_spec`.

    Args:
        catalog: ModelCatalog instance (accepts Any to avoid a hard import
                 at module level — the catalog is passed in by the caller).

    Returns:
        Number of new entries added (skips already-registered IDs).
    """
    # Lazy import inside function body — avoids circular import at module load
    from integrations.service_tools.model_catalog import ModelEntry, ModelType

    # Pre-pass: validate any existing TTS entries (admin/hive seeded the
    # catalog before us).  Invalid entries are removed + logged so they
    # don't poison `_refresh_engine_registry_from_catalog` below.  Code-
    # shipped engines (ENGINE_REGISTRY) ALWAYS have tool_module so they
    # never trip this; the gate exists for foreign manifests.
    _drop_ids: List[str] = []
    for entry in list(catalog.list_by_type('tts')):
        err = _validate_engine_caps(entry.capabilities or {})
        if err:
            logger.warning(
                'TTS catalog entry %r rejected at ingest: %s', entry.id, err,
            )
            _drop_ids.append(entry.id)
        elif not (entry.capabilities or {}).get('tool_module'):
            # Reflection-only entries are valid per the schema but not
            # dispatchable until #58 Scope-2 ships the --catalog-id
            # path in gpu_worker._dispatch_and_run.  Reject up front
            # so the admin sees the error at boot, not silence at
            # synth time.  Once Scope-2 lands, remove this branch and
            # let reflection-only entries through.
            logger.warning(
                'TTS catalog entry %r is reflection-only (no tool_module); '
                '#58 Scope-2 reflection dispatcher has not landed yet — '
                'rejecting at ingest so the admin sees the error '
                'immediately rather than at first synth call.', entry.id,
            )
            _drop_ids.append(entry.id)
    for _eid in _drop_ids:
        try:
            catalog.unregister(_eid, persist=False)
        except Exception as _re:
            logger.debug('failed to unregister invalid TTS entry %r: %s',
                         _eid, _re)

    added = 0
    for engine_id, spec in ENGINE_REGISTRY.items():
        # Skip if already registered (preserves user edits from admin UI)
        if catalog.get(f'tts-{engine_id.replace("_", "-")}') is not None:
            continue

        device_value = spec.device.value
        supports_gpu, supports_cpu = _DEVICE_TO_COMPUTE.get(
            device_value, (False, True)
        )
        backend = _DEVICE_TO_BACKEND.get(device_value, 'in_process')

        # Build language_priority from LANG_ENGINE_PREFERENCE:
        # lower rank in the preference list → lower priority number → preferred
        lang_priority: Dict[str, int] = {}
        for lang, engine_list in LANG_ENGINE_PREFERENCE.items():
            if engine_id in engine_list:
                rank = engine_list.index(engine_id)   # 0 = most preferred
                lang_priority[lang] = rank * 10       # 0, 10, 20, ...

        # Pick the best latency figure for quality/speed scores
        best_latency_ms = min(
            (v for v in (spec.latency_gpu_ms, spec.latency_cpu_ms,
                          spec.latency_cloud_ms) if v > 0),
            default=5000,
        )
        # speed_score: 1.0 = instant (≤10 ms), 0.0 = very slow (≥5000 ms)
        speed_score = max(0.0, 1.0 - (best_latency_ms - 10) / 4990)

        # Build capabilities dict — TTS-specific fields + extras
        extra = _ENGINE_EXTRA_CAPS.get(engine_id, {})
        capabilities: Dict[str, Any] = {
            'voice_clone':    spec.voice_clone,
            'sample_rate':    spec.sample_rate,
            'latency_gpu_ms': spec.latency_gpu_ms,
            'latency_cpu_ms': spec.latency_cpu_ms,
            'latency_cloud_ms': spec.latency_cloud_ms,
            'tool_module':    spec.tool_module,
            'tool_function':  spec.tool_function,
            'vram_key':       spec.vram_key,
            'streaming':      extra.get('streaming', False),
            'paralinguistic': extra.get('paralinguistic', []),
            'emotion_tags':   extra.get('emotion_tags', False),
        }

        # languages list — ('*',) means "all"; store as-is so select_best
        # language matching still works (catalog treats '*' as wildcard)
        languages = list(spec.languages)

        entry = ModelEntry(
            id=f'tts-{engine_id.replace("_", "-")}',
            name=_ENGINE_DISPLAY_NAMES.get(engine_id, engine_id),
            model_type=ModelType.TTS,
            version='1.0',
            source='cloud' if spec.device == TTSDevice.CLOUD else 'local',
            vram_gb=_engine_vram_gb(engine_id),
            ram_gb=_ENGINE_RAM_GB.get(engine_id, 0.5),
            disk_gb=_ENGINE_DISK_GB.get(engine_id, 0.0),
            min_capability_tier='lite' if supports_cpu else 'standard',
            backend=backend,
            supports_gpu=supports_gpu,
            supports_cpu=supports_cpu,
            supports_cpu_offload=False,
            idle_timeout_s=300.0,
            capabilities=capabilities,
            quality_score=spec.quality,
            speed_score=round(speed_score, 3),
            priority=50,
            languages=languages,
            language_priority=lang_priority,
            tags=['tts', 'local' if spec.device != TTSDevice.CLOUD else 'cloud'],
            enabled=True,
            auto_load=False,
        )
        catalog.register(entry, persist=False)
        added += 1

    # Post-upsert: rebuild ENGINE_REGISTRY in place so it reflects the
    # current catalog state (admin/hive-edited entries become visible
    # to existing call sites).  Snapshot semantics — runtime catalog
    # mutations after this point do NOT auto-propagate; a re-bootstrap
    # is required.  Matches the dict-iter assumption every existing
    # ENGINE_REGISTRY caller relies on.  See task #58 acceptance #5.
    _refresh_engine_registry_from_catalog(catalog)

    return added


def _refresh_engine_registry_from_catalog(catalog) -> int:
    """Rebuild ENGINE_REGISTRY in place from the post-upsert catalog.

    Reflection-only entries (no tool_module) are excluded — they live
    only in the catalog and are dispatched via the `--catalog-id`
    path.  TTSEngineSpec callers continue to see only spec-shaped
    entries, exactly as before this refactor.

    Returns the number of entries in the rebuilt registry.

    Idempotent: calling twice with the same catalog state produces the
    same registry contents.
    """
    new_entries: Dict[str, TTSEngineSpec] = {}
    for entry in catalog.list_by_type('tts'):
        spec = _catalog_entry_to_spec(entry)
        if spec is None:
            continue  # validation failed, or reflection-only entry
        new_entries[spec.engine_id] = spec
    ENGINE_REGISTRY.clear()
    ENGINE_REGISTRY.update(new_entries)
    return len(new_entries)


def _catalog_entry_to_spec(entry) -> Optional[TTSEngineSpec]:
    """Convert a ModelCatalog ModelEntry back to a TTSEngineSpec.

    Used by code that needs a TTSEngineSpec but only has a catalog entry
    (e.g. when the router consults the catalog for dynamically registered
    engines that were not present in ENGINE_REGISTRY at startup).

    Returns None if:
      * the entry's capabilities fail validation (#58 contract — see
        `_validate_engine_caps`); the caller should NOT see that entry
        because the dispatcher cannot route to it.
      * the entry uses the reflection-only dispatch path (no tool_module).
        TTSEngineSpec carries `tool_module` as a non-optional dispatch
        handle for the existing call sites; reflection-only entries are
        dispatched directly from the catalog and are intentionally
        excluded from the ENGINE_REGISTRY snapshot.
    """
    caps = entry.capabilities or {}
    err = _validate_engine_caps(caps)
    if err:
        # Loud at ingest, silent on subsequent re-reads — the catalog
        # populator/loader already logged this; don't spam every read.
        return None
    tool_module = caps.get('tool_module')
    if not tool_module:
        # Valid reflection-only entry, but TTSEngineSpec needs a
        # tool_module.  Caller (`_refresh_engine_registry_from_catalog`)
        # will skip None entries and dispatch reflection-only IDs via
        # the catalog path instead.
        return None
    tool_function = caps.get('tool_function')

    # Determine TTSDevice from backend + supports_* flags
    if caps.get('latency_cloud_ms', 0) > 0 and not entry.supports_gpu and not entry.supports_cpu:
        device = TTSDevice.CLOUD
    elif entry.supports_gpu and not entry.supports_cpu:
        device = TTSDevice.GPU_ONLY
    elif entry.supports_gpu and entry.supports_cpu:
        device = TTSDevice.GPU_PREFERRED
    else:
        device = TTSDevice.CPU_ONLY

    # Strip the 'tts-' prefix that populate_tts_catalog adds
    raw_id = entry.id[4:] if entry.id.startswith('tts-') else entry.id

    return TTSEngineSpec(
        engine_id=raw_id,
        device=device,
        vram_key=caps.get('vram_key', ''),
        languages=tuple(entry.languages) if entry.languages else ('en',),
        quality=entry.quality_score,
        voice_clone=caps.get('voice_clone', False),
        latency_gpu_ms=caps.get('latency_gpu_ms', 0),
        latency_cpu_ms=caps.get('latency_cpu_ms', 0),
        latency_cloud_ms=caps.get('latency_cloud_ms', 0),
        tool_module=tool_module,
        tool_function=tool_function,
        sample_rate=caps.get('sample_rate', 24000),
    )
