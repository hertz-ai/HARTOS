"""
ModelCatalog — single source of truth for ALL model types.

One schema covers LLM, TTS, STT, VLM, image gen, video gen, etc.
JSON-backed so the admin UI can CRUD entries at runtime.

Adding a new model of ANY type:
  1. catalog.register(ModelEntry(...))        — programmatic
  2. POST /api/admin/models                   — via admin UI
  3. Edit model_catalog.json in the data dir  — manual

The catalog does NOT load/unload models — that's the orchestrator's job.
This is purely metadata + state tracking.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger('ModelCatalog')


# ── Model type enum — single source of truth ─────────────────────
# Use ModelType.LLM (not 'llm') everywhere. The .value IS the string
# so JSON serialization and dict key usage work unchanged.
#
# Usage:
#   ModelType.LLM          → <ModelType.LLM: 'llm'>
#   ModelType.LLM.value    → 'llm'
#   ModelType.LLM.label    → 'Large Language Model'
#   ModelType('llm')       → ModelType.LLM  (lookup from string)
#   str(ModelType.LLM)     → 'llm'          (clean string for JSON/logs)

class ModelType(str, Enum):
    """Canonical model type identifiers. Inherits from str so
    ModelType.LLM == 'llm' is True — backwards compatible with
    all existing string comparisons, dict keys, and JSON."""

    LLM       = 'llm'
    TTS       = 'tts'
    STT       = 'stt'
    VLM       = 'vlm'
    IMAGE_GEN = 'image_gen'
    VIDEO_GEN = 'video_gen'
    AUDIO_GEN = 'audio_gen'
    EMBEDDING = 'embedding'
    EMBODIED  = 'embodied'   # VLA / world-model robot policy (Qwen RobotSuite class)

    @property
    def label(self) -> str:
        return _MODEL_TYPE_LABELS[self]

    def __str__(self) -> str:
        return self.value


_MODEL_TYPE_LABELS = {
    ModelType.LLM:       'Large Language Model',
    ModelType.TTS:       'Text-to-Speech',
    ModelType.STT:       'Speech-to-Text',
    ModelType.VLM:       'Vision-Language Model',
    ModelType.IMAGE_GEN: 'Image Generation',
    ModelType.VIDEO_GEN: 'Video Generation',
    ModelType.AUDIO_GEN: 'Audio/Music Generation',
    ModelType.EMBEDDING: 'Embedding Model',
    ModelType.EMBODIED:  'Embodied VLA / World Model',
}

# Backwards-compatible dict for code that iterates MODEL_TYPES
MODEL_TYPES = {mt.value: mt.label for mt in ModelType}

# Backend runtimes
BACKENDS = {
    'llama.cpp':  'llama.cpp server (GGUF)',
    'torch':      'PyTorch (HuggingFace)',
    'onnx':       'ONNX Runtime',
    'piper':      'Piper TTS (ONNX, CPU)',
    'api':        'Remote API endpoint',
    'sidecar':    'Subprocess sidecar',
    'in_process': 'In-process Python module',
}

# Backends whose runtime is positively known NOT to be PyTorch.  Anything else
# — including an unrecognised or missing value — conservatively counts as
# needing torch.
#
# WHY (R1, measured live 2026-08-11): CUDA-torch provisioning was gated on the
# model's TYPE ("is this TTS/STT on a CUDA box?") instead of its RUNTIME.  A
# sherpa-onnx STT engine (Moonshine) therefore blocked on a 221s pip resolve
# plus a multi-GB CUDA PyTorch download for a runtime that never imports torch.
# The deciding field already existed — `ModelEntry.backend`, set correctly by
# whisper_tool.py — it just wasn't the thing being read.
#
# The set is deliberately an ALLOW-LIST of torch-free backends rather than a
# deny-list of torch ones: an unclassified backend keeps today's behaviour
# (install), so this can never under-install and break an engine at load time.
# Over-installing wastes minutes; under-installing breaks the feature.
TORCHLESS_BACKENDS = frozenset({'onnx', 'piper', 'llama.cpp', 'api'})


def backend_requires_torch(backend) -> bool:
    """True iff a model on this backend needs PyTorch installed to run.

    SINGLE source for "does this engine need torch?" — both the language
    bootstrap and the STT loader consult it, so the answer cannot diverge
    between the pre-install path and the download path.

    Unknown / empty / None => True (fail safe: install rather than risk an
    engine that cannot load).
    """
    return (backend or 'torch') not in TORCHLESS_BACKENDS


# Download sources
SOURCES = {
    'huggingface': 'HuggingFace Hub',
    'ollama':      'Ollama registry',
    'github':      'GitHub release',
    'pip':         'Python package (pip)',
    'api':         'Remote API (no download)',
    'local':       'Already on disk',
    'custom_url':  'Custom download URL',
}


@dataclass
class ModelEntry:
    """Universal model descriptor — works for any model type."""

    # ── Identity ──────────────────────────────────────────────────
    id: str                              # Unique slug: "qwen3.5-4b-vl", "chatterbox-turbo"
    name: str                            # Human-readable display name
    model_type: str                      # Key from MODEL_TYPES
    version: str = '1.0'                 # Semver or commit hash

    # ── Source & Files ────────────────────────────────────────────
    source: str = 'huggingface'          # Key from SOURCES
    repo_id: str = ''                    # HuggingFace repo, Ollama model name, pip package
    files: Dict[str, str] = field(default_factory=dict)
    download_url: str = ''               # For custom_url source

    # ── Compute Requirements ──────────────────────────────────────
    vram_gb: float = 0.0                 # GPU VRAM needed (0 = CPU-capable)
    ram_gb: float = 1.0                  # System RAM needed
    disk_gb: float = 0.0                 # Disk space for model files
    min_capability_tier: str = 'lite'    # 'lite', 'standard', 'full'

    # ── Runtime ───────────────────────────────────────────────────
    backend: str = 'torch'               # Key from BACKENDS
    supports_gpu: bool = True
    supports_cpu: bool = True
    supports_cpu_offload: bool = False
    cpu_offload_method: str = 'none'     # 'torch_to_cpu', 'restart_cpu', 'none'
    idle_timeout_s: float = 600.0
    min_build: Optional[int] = None

    # ── Capabilities (generic key-value) ──────────────────────────
    capabilities: Dict[str, Any] = field(default_factory=dict)

    # ── Selection metadata ────────────────────────────────────────
    quality_score: float = 0.5
    speed_score: float = 0.5
    cost_per_1k: float = 0.0
    priority: int = 50

    # ── Routing (for TTS/STT language-based routing) ──────────────
    languages: List[str] = field(default_factory=list)
    language_priority: Dict[str, int] = field(default_factory=dict)

    # ── State (runtime, NOT persisted to JSON) ────────────────────
    downloaded: bool = False
    loaded: bool = False
    device: str = 'unloaded'
    active_since: Optional[float] = None
    error: Optional[str] = None

    # ── Tags for filtering ────────────────────────────────────────
    tags: List[str] = field(default_factory=list)

    # ── User-configurable flags ───────────────────────────────────
    enabled: bool = True
    auto_load: bool = False
    pinned: bool = False
    purposes: List[str] = field(default_factory=list)  # e.g. ['draft', 'main', 'caption']

    def to_dict(self) -> dict:
        """Serialize to JSON-safe dict (excludes runtime state)."""
        d = asdict(self)
        for key in ('downloaded', 'loaded', 'device', 'active_since', 'error'):
            d.pop(key, None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'ModelEntry':
        """Deserialize from JSON dict, ignoring unknown keys."""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)

    # Backends whose models are FETCHED as weight files.  Only these need
    # the download fields below; an `api` / `in_process` model has nothing
    # to download and must not be blocked by the rule.
    _DOWNLOADED_BACKENDS = ('llama.cpp',)

    def validate(self) -> list:
        """Return a list of reasons this entry can never work, [] if sound.

        Registering an entry that cannot be downloaded is worse than
        refusing it: the caller sees success, the row persists, and the
        failure only surfaces later somewhere unrelated.

        2026-08-15 live: an entry was accepted with files={} and
        repo_id='unsloth/Qwen3.8-27B-UD-Q4_K_XL.gguf' (a FILE name, not a
        repo).  POST /api/admin/models returned 200 {"success": true} and
        persisted it; the download then failed for the rest of the
        install's life with

            LLM download: no preset for Qwen3.8-27B-UD-Q4_K_XL.gguf

        because Nunba's models/orchestrator.py::_entry_to_preset returns
        None precisely when files['model'] is empty.  This method moves
        that consumer-side requirement up to the producer, so the same
        invariant is enforced once, wherever an entry is created (admin
        register, hub install, catalog populator).
        """
        problems = []
        if not self.id:
            problems.append('id is required')
        if not self.model_type:
            problems.append('model_type is required')

        if self.backend in self._DOWNLOADED_BACKENDS:
            if not (self.files or {}).get('model'):
                problems.append(
                    f"files['model'] is required for backend "
                    f"'{self.backend}' - without the weight file name the "
                    f"model can never be downloaded or loaded")
            # A HuggingFace repo id is 'org/repo'.  It never names a
            # weight file, so a '.gguf' suffix means the file name was
            # pasted where the repo belongs and can never resolve.
            repo = (self.repo_id or '').strip()
            if repo.lower().endswith(('.gguf', '.bin', '.safetensors')):
                problems.append(
                    f"repo_id '{repo}' looks like a FILE, not a repository "
                    f"('org/repo') - put the file name in files['model']")
        return problems

    def matches_compute(self, budget_vram_gb: float, budget_ram_gb: float,
                        gpu_available: bool) -> str:
        """Check if this model can run given current compute.

        Returns: 'gpu', 'cpu', 'cpu_offload', or 'impossible'
        """
        if gpu_available and budget_vram_gb >= self.vram_gb:
            return 'gpu'
        if self.supports_cpu_offload and gpu_available and budget_vram_gb >= self.vram_gb * 0.5:
            return 'cpu_offload'
        if self.supports_cpu and budget_ram_gb >= self.ram_gb:
            return 'cpu'
        return 'impossible'


class ModelCatalog:
    """Central registry of all models across all subsystems.

    JSON-persisted. Thread-safe for concurrent reads; write-locked for mutations.

    Subsystem population is pluggable: call register_populator() to add
    a callback that discovers models from a subsystem (LLM presets, TTS engines,
    etc.). This avoids hard dependencies on application-layer modules.
    """

    def __init__(self, catalog_path: Optional[str] = None):
        try:
            from core.platform_paths import get_db_dir
            data_dir = Path(get_db_dir())
        except ImportError:
            data_dir = Path.home() / 'Documents' / 'Nunba' / 'data'
        data_dir.mkdir(parents=True, exist_ok=True)
        self._path = Path(catalog_path) if catalog_path else data_dir / 'model_catalog.json'
        self._entries: Dict[str, ModelEntry] = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._populators: List = []  # list of (name, callable)
        self._load()

    # ── Populator registration ─────────────────────────────────────

    def register_populator(self, name: str, fn) -> None:
        """Register a subsystem populator callback.

        The callback receives the catalog as its only argument and should call
        catalog.register(entry, persist=False) for each model it discovers.
        It must return the count of new entries added.
        """
        self._populators.append((name, fn))

    # ── CRUD ──────────────────────────────────────────────────────

    def register(self, entry: ModelEntry, persist: bool = True) -> None:
        """Add or update a model entry."""
        with self._lock:
            self._entries[entry.id] = entry
            self._dirty = True
        if persist:
            self._save()
        logger.info(f"Registered model: {entry.id} ({entry.model_type}, {entry.backend})")

    def unregister(self, model_id: str, persist: bool = True) -> bool:
        """Remove a model entry. Returns True if found."""
        with self._lock:
            removed = self._entries.pop(model_id, None)
            if removed:
                self._dirty = True
        if removed and persist:
            self._save()
            logger.info(f"Unregistered model: {model_id}")
        return removed is not None

    def override(self, model_id: str, *, persist: bool = False, **fields) -> bool:
        """Apply field-level overrides to an already-registered entry.

        Use this when one populator needs to narrow another populator's
        entry (e.g. Nunba's populate_media_gen amending HARTOS's fallback
        audio_gen-acestep surface). Unlike direct ``entry.field = value``
        mutation, override() takes the catalog lock, validates field names
        against the ModelEntry dataclass, sets the dirty flag, and logs
        the change — so the single-writer semantics of register/unregister
        extend to cross-populator amendments.

        Unknown fields raise ValueError. Returns False if model_id is not
        registered (no-op). Defaults to persist=False because overrides
        typically happen during populator boot (same convention as
        register(persist=False)).
        """
        allowed = set(ModelEntry.__dataclass_fields__)
        unknown = [k for k in fields if k not in allowed]
        if unknown:
            raise ValueError(
                f"override(): unknown field(s) for ModelEntry: {sorted(unknown)}",
            )
        with self._lock:
            entry = self._entries.get(model_id)
            if entry is None:
                return False
            for key, value in fields.items():
                setattr(entry, key, value)
            self._dirty = True
        if persist:
            self._save()
        logger.info(
            f"Overrode model {model_id} fields: {sorted(fields.keys())}",
        )
        return True

    def get(self, model_id: str) -> Optional[ModelEntry]:
        """Get a model by ID."""
        return self._entries.get(model_id)

    def list_all(self) -> List[ModelEntry]:
        """All registered models."""
        return list(self._entries.values())

    def list_types(self) -> List[str]:
        """All distinct model types that have at least one enabled entry."""
        return list({e.model_type for e in self._entries.values() if e.enabled})

    def list_by_type(self, model_type: str) -> List[ModelEntry]:
        """All models of a given type (e.g. 'tts', 'llm')."""
        return [e for e in self._entries.values()
                if e.model_type == model_type and e.enabled]

    def list_by_tag(self, tag: str) -> List[ModelEntry]:
        """All models with a given tag."""
        return [e for e in self._entries.values() if tag in e.tags]

    # ── Compute-aware selection ───────────────────────────────────

    def select_best(self, model_type: str, budget_vram_gb: float = 0,
                    budget_ram_gb: float = 4, gpu_available: bool = False,
                    language: Optional[str] = None,
                    require_capability: Optional[Dict[str, Any]] = None,
                    exclude: Optional[List[str]] = None,
                    ) -> Optional[ModelEntry]:
        """Select the best model of a given type for current compute.

        Selection priority:
          1. Filter by type + enabled + compute fit + capability tier
          2. If language specified, prefer models that serve it
          3. Sort by quality_score * speed_score * priority
          4. Return top pick

        ``exclude`` — set of model_ids to skip (used by fallback walks
        when a previously-selected entry just failed synth/load).  None
        or empty list means "no exclusions" (default).
        """
        candidates = self.list_by_type(model_type)

        # Fallback exclusion — caller-supplied IDs are filtered before
        # any scoring so the second-best engine surfaces cleanly when
        # the primary fails at runtime (e.g. TTS engine raised, walk
        # to the next entry in language_priority order).
        if exclude:
            _exclude_set = set(exclude)
            candidates = [e for e in candidates if e.id not in _exclude_set]

        # Get current capability tier to enforce min_capability_tier
        current_tier = self._get_capability_tier()

        # Filter by compute fit + capability tier
        scored = []
        for entry in candidates:
            # Capability tier gate
            if not self._tier_sufficient(current_tier, entry.min_capability_tier):
                continue

            # Already-loaded models always fit — they're using resources we
            # already allocated, so never skip them due to budget calculations
            if entry.loaded:
                fit = entry.device or 'cpu'
            else:
                fit = entry.matches_compute(budget_vram_gb, budget_ram_gb, gpu_available)
                if fit == 'impossible':
                    continue

            score = entry.quality_score * 100 + entry.priority

            if fit == 'gpu':
                score += 200
            elif fit == 'cpu_offload':
                score += 100

            if language and entry.languages:
                if language in entry.languages:
                    lang_prio = entry.language_priority.get(language, 50)
                    # Language preference is dominant — rank 0 (preferred engine
                    # for this language) gets +300, rank 1 gets +270, default +150.
                    # This ensures tts_router's LANG_ENGINE_PREFERENCE order wins
                    # over small quality_score differences between engines.
                    score += (300 - lang_prio * 3)
                else:
                    score -= 500

            if require_capability:
                cap_match = all(
                    entry.capabilities.get(k) == v
                    for k, v in require_capability.items()
                )
                if not cap_match:
                    continue

            if entry.downloaded:
                score += 50

            # Strongly prefer already-loaded models — avoids downloading a
            # second model when one of the same type is already running
            if entry.loaded:
                score += 1000

            scored.append((score, fit, entry))

        if not scored:
            return None

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_fit, best = scored[0]
        logger.info(f"Selected {best.id} ({best.model_type}) — "
                    f"fit={best_fit}, score={best_score:.0f}")
        return best

    def select_all_fitting(self, model_type: str, budget_vram_gb: float = 0,
                           budget_ram_gb: float = 4, gpu_available: bool = False,
                           ) -> List[tuple]:
        """Return all fitting models with their run modes, sorted by score."""
        candidates = self.list_by_type(model_type)
        result = []
        for entry in candidates:
            fit = entry.matches_compute(budget_vram_gb, budget_ram_gb, gpu_available)
            if fit != 'impossible':
                result.append((entry, fit))
        result.sort(key=lambda x: x[0].quality_score * 100 + x[0].priority, reverse=True)
        return result

    # ── State updates ─────────────────────────────────────────────

    def mark_downloaded(self, model_id: str, downloaded: bool = True) -> None:
        entry = self._entries.get(model_id)
        if entry:
            entry.downloaded = downloaded

    def mark_loaded(self, model_id: str, device: str = 'gpu') -> None:
        entry = self._entries.get(model_id)
        if entry:
            entry.loaded = True
            entry.device = device
            entry.active_since = time.time()
            entry.error = None

    def mark_unloaded(self, model_id: str) -> None:
        entry = self._entries.get(model_id)
        if entry:
            entry.loaded = False
            entry.device = 'unloaded'
            entry.active_since = None

    def mark_error(self, model_id: str, error: str) -> None:
        entry = self._entries.get(model_id)
        if entry:
            entry.error = error
            entry.loaded = False

    # ── Purpose assignment ──────────────────────────────────────

    # Universal purpose list — every task Nunba supports.  A single model
    # can serve ANY combination (e.g. Qwen3.5-0.8B → ['draft','caption',
    # 'grounding']; Qwen3-4B-Omni → ['main','tts','stt']).
    # Purposes are NOT gated by model_type — model capabilities drive it,
    # not an artificial type label.
    ALL_PURPOSES: List[str] = [
        'draft',        # Fast classifier / speculative decode LLM
        'main',         # Primary LLM for chat/reasoning
        'vision',       # Image understanding (VLM — generic)
        'caption',      # Image/video captioning (VLM)
        'grounding',    # GUI element grounding (VLM — click targets)
        'tts',          # Text-to-speech
        'stt',          # Speech-to-text / ASR
        'diarization',  # Speaker segmentation
        'vad',          # Voice activity detection
        'embedding',    # Text embeddings (retrieval, RAG)
        'rerank',       # Cross-encoder reranking for retrieval
        'ocr',          # Text extraction from images
        'music',        # Music generation
        'image-gen',    # Text-to-image
        'video-gen',    # Text-to-video
        'translate',    # Machine translation (when dedicated model)
    ]

    def get_by_purpose(self, purpose: str) -> Optional[ModelEntry]:
        """Return the model assigned to *purpose*, or None."""
        for entry in self._entries.values():
            if purpose in entry.purposes and entry.enabled:
                return entry
        return None

    def set_purpose(self, model_id: str, purpose: str, enabled: bool = True) -> bool:
        """Toggle a purpose on/off for a model.

        When enabling: clears the same purpose from any other model
        (one model per purpose globally), then adds it.
        When disabling: removes the purpose from this model.
        Persists to disk.  Returns True on success.
        """
        with self._lock:
            entry = self._entries.get(model_id)
            if entry is None:
                return False
            if purpose not in self.ALL_PURPOSES:
                return False
            if enabled:
                # Clear the same purpose from any other model
                for other in self._entries.values():
                    if other.id != model_id and purpose in other.purposes:
                        other.purposes = [p for p in other.purposes if p != purpose]
                if purpose not in entry.purposes:
                    entry.purposes.append(purpose)
            else:
                entry.purposes = [p for p in entry.purposes if p != purpose]
            self._dirty = True
        self._save()
        logger.info(f"Model {model_id} purpose {purpose!r} {'enabled' if enabled else 'disabled'} "
                    f"→ purposes={entry.purposes}")
        return True

    # ── Auto-populate from registered subsystem populators ─────────

    def populate_from_subsystems(self) -> int:
        """Run all registered populators + built-in STT/VLM entries.

        Called on first run or when catalog is empty. Does NOT overwrite
        existing entries (user edits via admin UI are preserved).
        Returns number of new entries added.
        """
        # Snapshot IDs BEFORE populator run so we can detect stale auto-entries
        ids_before = set(self._entries.keys())

        added = 0
        # Run application-registered populators (LLM, TTS, etc.)
        for name, fn in self._populators:
            try:
                count = fn(self)
                added += count
                if count:
                    logger.info(f"Populator '{name}' added {count} entries")
            except Exception as e:
                logger.debug(f"Populator '{name}' failed: {e}")
        # Built-in entries that don't depend on application modules
        added += self._populate_llm_models()
        added += self._populate_tts_models()
        added += self._populate_stt_models()
        added += self._populate_vlm_models()
        added += self._populate_embodied_models()
        added += self._populate_videogen_models()
        added += self._populate_audiogen_models()

        # Cleanup: remove stale auto-entries that no populator emitted this boot.
        # An entry is "auto-populated" if its ID starts with a known prefix and
        # it wasn't modified by the user (no custom tags, no non-default purposes,
        # not pinned).  Stale = prefix-matched but not re-registered this boot.
        ids_after = set(self._entries.keys())
        touched_this_boot = ids_after - ids_before  # new in this run
        # For entries that existed before AND still exist, populator.register()
        # would have overwritten them — so check timestamps on _entries that
        # weren't touched but have auto-populatable prefixes.
        AUTO_PREFIXES = ('tts-', 'stt-', 'vlm-', 'video_gen-', 'audio_gen-', 'embodied-')
        stale = []
        for eid, entry in list(self._entries.items()):
            if eid in touched_this_boot:
                continue  # freshly registered this boot
            if not any(eid.startswith(p) for p in AUTO_PREFIXES):
                continue  # not an auto-prefix (e.g. llm-* user-registered)
            if entry.pinned or entry.purposes or (entry.tags and set(entry.tags) - {'local', 'tts', 'stt', 'vision', 'cpu-friendly'}):
                continue  # user customized — preserve
            stale.append(eid)

        if stale:
            for eid in stale:
                self._entries.pop(eid, None)
                self._dirty = True
            logger.info(f"Cleaned {len(stale)} stale auto-entries: {stale}")

        if added > 0 or stale:
            self._save()
            logger.info(f"Auto-populated {added} entries, cleaned {len(stale)} stale")
        return added

    def _populate_tts_models(self) -> int:
        """Populate TTS engine entries from tts_router.ENGINE_REGISTRY.

        Lazy-imports populate_tts_catalog to avoid circular imports at
        module load time. tts_router → model_catalog direction is only
        present inside function bodies (never at module scope).
        """
        try:
            from integrations.channels.media.tts_router import populate_tts_catalog
            return populate_tts_catalog(self)
        except Exception as e:
            logger.debug(f"TTS catalog population skipped: {e}")
            return 0

    def _populate_stt_models(self) -> int:
        """STT model entries — delegated to whisper_tool.populate_stt_catalog().

        whisper_tool is the single source of truth for STT model specs
        (engine names, VRAM thresholds, sherpa-onnx archive mappings).
        Falls back to a minimal inline set if whisper_tool is unavailable.
        """
        try:
            from integrations.service_tools.whisper_tool import populate_stt_catalog
            return populate_stt_catalog(self)
        except Exception as e:
            logger.debug(f"STT catalog population via whisper_tool skipped: {e}")

        # Minimal fallback (whisper_tool not yet importable at catalog init time)
        added = 0
        _fallback = [
            ('stt-whisper-base',   'Whisper Base (faster-whisper)',      0.2, 0.5,  0.75, 0.9),
            ('stt-whisper-medium', 'Whisper Medium (faster-whisper)',    1.5, 2.0,  0.85, 0.7),
            ('stt-whisper-large',  'Whisper Large v3 (faster-whisper)', 3.0, 4.0,  0.93, 0.5),
        ]
        for mid, name, vram, ram, quality, speed in _fallback:
            if mid in self._entries:
                continue
            entry = ModelEntry(
                id=mid, name=name, model_type=ModelType.STT,
                source='huggingface',
                vram_gb=vram, ram_gb=ram,
                backend='torch', supports_gpu=vram > 0, supports_cpu=True,
                supports_cpu_offload=True, cpu_offload_method='torch_to_cpu',
                idle_timeout_s=300,
                capabilities={'realtime': True, 'diarization': False,
                              'multilingual': True},
                quality_score=quality, speed_score=speed,
                languages=['multilingual'],
                tags=['local', 'stt', 'cpu-friendly'],
            )
            self.register(entry, persist=False)
            added += 1
        return added

    def _populate_llm_models(self) -> int:
        """Chat/LLM entries — the ladder every hardware-based recommendation reads.

        This catalog is the SINGLE SOURCE OF TRUTH for which chat models exist.
        Before this existed the LLM rung was missing (populate_from_subsystems
        seeded tts/stt/vlm/embodied/videogen/audiogen and skipped llm), so three
        ad-hoc lists grew to fill the gap and drifted apart: model_onboarding's
        MODEL_TIERS still named Qwen2.5 while agent_engine/model_registry.py had
        moved to Qwen3.5, and Nunba kept a fourth list of its own. Anything that
        needs "which chat model suits this box" reads THIS, and nothing else
        hardcodes a ladder.

        Sizing carries BOTH budgets on purpose. vram_gb gates the GPU path and
        ram_gb gates the CPU path, because a box with no GPU but plenty of RAM
        can still run a mid-size model -- the VRAM-only ladder this replaces
        collapsed every CPU-only machine to the smallest entry regardless of how
        much RAM it had.

        repo_id values are taken from core/hub_allowlist.py, so every entry here
        is already download-allowlisted; adding a model means adding it there
        too, and the allowlist stays the security boundary.

        Extending: append an entry. Selection is data-driven (budget vs
        vram_gb/ram_gb, ranked by priority) so no code changes to add a family.
        """
        # Rows are DOWNLOAD-COMPLETE on purpose: repo_id alone is not enough to
        # fetch a GGUF, so each carries the exact file name and, for the VL
        # models, the mmproj projector. mmproj has TWO names because the file is
        # published as mmproj-F16.gguf in every repo and must be stored under a
        # model-specific name locally or the second model overwrites the first.
        #
        # Sourced from Nunba's llama/llama_installer.py MODEL_PRESETS, which is
        # what actually downloads today. NOT from core/hub_allowlist.py: that is
        # a security allowlist and lists transformers repos (google/gemma-2b-it)
        # that contain no GGUF at all, so seeding from it produced rows the
        # llama_cpp backend could never load. Gemma is therefore absent here
        # until a GGUF repo for it is allowlisted; a row that cannot download is
        # worse than no row.
        #
        # vram_gb/ram_gb are derived from weight size: GPU needs the weights
        # plus ~35% for KV cache and context, CPU needs roughly double the
        # weights to stay comfortable. Extending: add a row.
        MIN_BUILD_QWEN35 = 8148          # llama.cpp b8148+ required by Qwen3.5
        # (id, name, repo, gguf, mmproj|None, size_mb, tier, prio, quality,
        #  speed, purposes, min_build)
        _llms = [
            ('llm-qwen3.5-0.8b', 'Qwen3.5 0.8B VL', 'unsloth/Qwen3.5-0.8B-GGUF',
             'Qwen3.5-0.8B-UD-Q4_K_XL.gguf', 'mmproj-Qwen3.5-0.8B-F16.gguf',
             550, 'lite', 30, 0.45, 0.95, ['draft'], MIN_BUILD_QWEN35),
            ('llm-qwen3-2b-text', 'Qwen3 2B (text only)',
             'unsloth/Qwen3-2B-Instruct-GGUF', 'Qwen3-2B-Instruct-Q4_K_M.gguf',
             None, 1100, 'lite', 35, 0.50, 0.88, ['main'], None),
            ('llm-qwen3.5-2b', 'Qwen3.5 2B VL', 'unsloth/Qwen3.5-2B-GGUF',
             'Qwen3.5-2B-UD-Q4_K_XL.gguf', 'mmproj-Qwen3.5-2B-F16.gguf',
             1340, 'lite', 45, 0.55, 0.85, ['main'], MIN_BUILD_QWEN35),
            ('llm-qwen3.5-4b', 'Qwen3.5 4B VL', 'unsloth/Qwen3.5-4B-GGUF',
             'Qwen3.5-4B-UD-Q4_K_XL.gguf', 'mmproj-Qwen3.5-4B-F16.gguf',
             2910, 'standard', 60, 0.60, 0.70, ['main'], MIN_BUILD_QWEN35),
            ('llm-qwen3.5-9b', 'Qwen3.5 9B VL', 'unsloth/Qwen3.5-9B-GGUF',
             'Qwen3.5-9B-UD-Q4_K_XL.gguf', 'mmproj-Qwen3.5-9B-F16.gguf',
             6113, 'standard', 70, 0.72, 0.50, ['main'], MIN_BUILD_QWEN35),
            ('llm-qwen3.5-27b', 'Qwen3.5 27B VL', 'unsloth/Qwen3.5-27B-GGUF',
             'Qwen3.5-27B-UD-Q4_K_XL.gguf', 'mmproj-Qwen3.5-27B-F16.gguf',
             18022, 'full', 80, 0.85, 0.30, ['main'], MIN_BUILD_QWEN35),
            ('llm-qwen3.5-35b-a3b', 'Qwen3.5 35B-A3B MoE',
             'unsloth/Qwen3.5-35B-A3B-GGUF', 'Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf',
             'mmproj-Qwen3.5-35B-A3B-F16.gguf',
             22733, 'full', 85, 0.88, 0.35, ['main'], MIN_BUILD_QWEN35),
        ]
        # Rows seeded by an EARLIER version of this method that are now known to
        # be unloadable: google/gemma-*-it are transformers repos with no GGUF,
        # so the llama_cpp backend can never start them. Remove them rather than
        # leave a row the recommender might select. Scoped to these exact ids.
        for _bad in ('llm-gemma-2b-it', 'llm-gemma-7b-it'):
            if _bad in self._entries:
                self.unregister(_bad, persist=False)
                logger.info("Removed unloadable seeded LLM row %s (no GGUF in repo)", _bad)

        added = 0
        for (mid, name, repo, gguf, mmproj, size_mb, tier, prio,
             quality, speed, purposes, min_build) in _llms:
            weights_gb = round(size_mb / 1024.0, 2)
            files = {'model': gguf}
            if mmproj:
                # Local name is model-specific; source name is what the repo
                # publishes. Collapsing them overwrites across models.
                files['mmproj'] = mmproj
                files['mmproj_source'] = 'mmproj-F16.gguf'
            _definition = dict(
                name=name, model_type=ModelType.LLM,
                source='huggingface', repo_id=repo, files=files,
                vram_gb=round(weights_gb * 1.35, 1),
                ram_gb=round(weights_gb * 2.0, 1),
                disk_gb=weights_gb,
                min_capability_tier=tier,
                backend='llama_cpp',
                supports_gpu=True, supports_cpu=True,
                supports_cpu_offload=True, cpu_offload_method='restart_cpu',
                min_build=min_build,
                capabilities={'chat': True, 'vision': bool(mmproj),
                              'quant': 'Q4_K_M' if mmproj is None else 'UD-Q4_K_XL',
                              'size_mb': size_mb},
                quality_score=quality, speed_score=speed, priority=prio,
                purposes=list(purposes),
                tags=['local', 'chat', 'qwen'] + (['vision'] if mmproj else []),
            )
            if mid in self._entries:
                # UPDATE, do not skip. A seeded row is owned by this method, so a
                # corrected definition (a wrong file name, a missing mmproj, a
                # bumped min_build) has to reach boxes that already persisted the
                # old one -- "add if absent" silently pins the first version ever
                # written and makes the catalog uncorrectable. User-owned flags
                # (enabled / pinned / auto_load) and runtime state (downloaded /
                # loaded) are NOT in _definition, so they survive untouched.
                self.override(mid, persist=False, **_definition)
                continue
            self.register(ModelEntry(id=mid, **_definition), persist=False)
            added += 1
        return added

    def _populate_vlm_models(self) -> int:
        """VLM model entries — delegated to lightweight_backend.populate_vlm_catalog().

        lightweight_backend is the single source of truth for VLM backend names
        and hardware tier thresholds. Falls back to MiniCPM only if unavailable.
        """
        try:
            from integrations.vision.lightweight_backend import populate_vlm_catalog
            return populate_vlm_catalog(self)
        except Exception as e:
            logger.debug(f"VLM catalog population via lightweight_backend skipped: {e}")

        # Minimal fallback — MiniCPM only
        added = 0
        if 'vlm-minicpm-v2' not in self._entries:
            entry = ModelEntry(
                id='vlm-minicpm-v2', name='MiniCPM-V-2',  # 4GB VRAM → standard tier
                model_type=ModelType.VLM, source='huggingface',
                repo_id='openbmb/MiniCPM-V-2',
                vram_gb=4.0, ram_gb=4.0, disk_gb=4.0,
                min_capability_tier='standard',  # 4GB VRAM = standard, not full
                backend='sidecar', supports_gpu=True, supports_cpu=False,
                idle_timeout_s=900,
                capabilities={'image_input': True, 'video_input': False,
                              'description_loop': True},
                quality_score=0.8, speed_score=0.7,
                tags=['local', 'vision'],
            )
            self.register(entry, persist=False)
            added += 1
        return added

    def _populate_embodied_models(self) -> int:
        """Embodied model entries — Qwen-RobotSuite (THREE foundation models).

        RobotSuite is three INDEPENDENT models that run inside HevolveAI (raw
        native intelligence; HARTOS has no ML): RobotManip (VLA manipulation),
        RobotWorld (language-conditioned video world model), and RobotNav
        (navigation). This bootstraps their METADATA exactly like an LLM — the
        catalog record that makes each discoverable to the admin UI + the
        orchestrator: its action vocabulary (RobotAction factories), the sensor
        modalities it consumes (SensorReading schema), and the shared
        WorldModelBridge endpoints. Errors dispatching to any of them propagate
        through the hive via WorldModelBridge._propagate_embodied_error.
        """
        # The bridge owns endpoint selection per-method — HevolveAI is
        # sensor-ingest-centric (actions + sensors → /v1/sensor/ingest, feedback
        # → /v1/stats), and that single source of truth lives in WorldModelBridge.
        # The catalog must NOT hardcode endpoint URLs (a prior copy advertised
        # /v1/actions, /v1/sensors/batch, /v1/feedback/latest that don't exist on
        # HevolveAI → catalog↔bridge drift; removed).
        bridge_caps = {
            'bridge': 'integrations.agent_engine.world_model_bridge.WorldModelBridge',
        }
        common = dict(
            model_type=ModelType.EMBODIED, source='pip', backend='in_process',
            supports_gpu=True, supports_cpu=True, supports_cpu_offload=True,
            cpu_offload_method='torch_to_cpu', idle_timeout_s=600,
            min_capability_tier='standard', tags=['local', 'embodied', 'robotics'],
        )
        specs = [
            dict(  # RobotManip — VLA manipulation: camera + language → low-level actions
                id='embodied-qwen-robotmanip',
                name='Qwen-RobotManip (VLA manipulation)',
                repo_id='QwenLM/Qwen-VLA',
                vram_gb=8.0, ram_gb=8.0, disk_gb=16.0,
                quality_score=0.78, speed_score=0.6,
                capabilities={
                    'action_verbs': ['vla_instruct', 'manip_action',
                                     'action_chunk', 'end_effector_delta'],
                    'inputs': ['camera', 'language'],
                    'sensor_modalities': ['camera', 'depth', 'force_torque', 'encoder'],
                    'language_conditioned': True, 'closed_loop': True, 'control_hz': 10,
                    # canonical 80-D masked state-action (2×29 per-arm + 22 reserved)
                    'action_space': '80d_masked', 'action_dims': 80,
                    'per_arm_dims': 29, 'reserved_dims': 22,
                    'per_arm_blocks': ['joint_positions', 'end_effector_pose',
                                       'gripper', 'dexterous_hand'],
                },
            ),
            dict(  # RobotWorld — language-conditioned video world model
                id='embodied-qwen-robotworld',
                name='Qwen-RobotWorld (language-conditioned video world model)',
                repo_id='Qwen/Qwen-RobotWorld',
                vram_gb=12.0, ram_gb=12.0, disk_gb=24.0,
                quality_score=0.75, speed_score=0.4,
                capabilities={
                    'action_verbs': ['world_model_rollout'],
                    'inputs': ['language', 'camera'],
                    'sensor_modalities': ['camera'],
                    'language_conditioned': True, 'world_model': True,
                    'output': 'predicted_video', 'default_horizon': 8,
                },
            ),
            dict(  # RobotNav — navigation → 8 (x, y, theta) waypoints
                id='embodied-qwen-robotnav',
                name='Qwen-RobotNav (navigation)',
                repo_id='QwenLM/Qwen-RobotNav',
                vram_gb=6.0, ram_gb=6.0, disk_gb=12.0,
                quality_score=0.74, speed_score=0.7,
                capabilities={
                    'action_verbs': ['navigate'],
                    'inputs': ['camera', 'language'],
                    'sensor_modalities': ['camera', 'depth', 'lidar', 'imu', 'gps'],
                    'output': 'waypoints_xytheta', 'num_waypoints': 8,
                },
            ),
        ]
        added = 0
        for spec in specs:
            if spec['id'] in self._entries:
                continue
            caps = {**bridge_caps, **spec.pop('capabilities')}
            self.register(ModelEntry(capabilities=caps, **common, **spec),
                          persist=False)
            added += 1
        return added

    def _populate_videogen_models(self) -> int:
        """Video generation model entries — delegated to media_agent.populate_videogen_catalog().

        media_agent is the single source of truth for video gen tool names
        and VRAM routing thresholds. Falls back to inline entries if unavailable.
        """
        try:
            from integrations.service_tools.media_agent import populate_videogen_catalog
            return populate_videogen_catalog(self)
        except Exception as e:
            logger.debug(f"Video gen catalog population via media_agent skipped: {e}")

        # Minimal fallback
        added = 0
        _fallback = [
            ('video_gen-wan2gp', 'Wan2GP',  8.0, 12.0, 0.88, 0.65),
            ('video_gen-ltx2',   'LTX2',    4.0,  8.0, 0.75, 0.80),
        ]
        for mid, name, vram, ram, quality, speed in _fallback:
            if mid in self._entries:
                continue
            entry = ModelEntry(
                id=mid, name=name, model_type=ModelType.VIDEO_GEN,
                source='huggingface',
                vram_gb=vram, ram_gb=ram,
                backend='sidecar', supports_gpu=True,
                supports_cpu=(vram < 6),
                supports_cpu_offload=(vram < 6),
                idle_timeout_s=600,
                capabilities={'txt2vid': True, 'img2vid': False},
                quality_score=quality, speed_score=speed,
                tags=['local', 'video_gen'],
            )
            self.register(entry, persist=False)
            added += 1
        return added

    def _populate_audiogen_models(self) -> int:
        """Audio/music generation entries — delegated to media_agent.populate_audiogen_catalog().

        media_agent is the single source of truth for audio gen tool names
        (ACE Step, DiffRhythm) and capability routing. Falls back to inline entries.
        Removes stale entries from previous catalog versions.
        """
        # Clean up stale entries with no capabilities (from old catalog JSON)
        for old_id in list(self._entries.keys()):
            if old_id.startswith('audio_gen-') and not self._entries[old_id].capabilities:
                del self._entries[old_id]

        try:
            from integrations.service_tools.media_agent import populate_audiogen_catalog
            return populate_audiogen_catalog(self)
        except Exception as e:
            logger.debug(f"Audio gen catalog population via media_agent skipped: {e}")

        # Minimal fallback
        added = 0
        _fallback = [
            ('audio_gen-acestep',    'ACE-Step 1.5',    6.0, 6.0, 0.85, 0.90),
            ('audio_gen-diffrhythm', 'DiffRhythm v1.2', 4.0, 4.0, 0.80, 0.75),
        ]
        for mid, name, vram, ram, quality, speed in _fallback:
            if mid in self._entries:
                continue
            entry = ModelEntry(
                id=mid, name=name, model_type=ModelType.AUDIO_GEN,
                source='huggingface',
                vram_gb=vram, ram_gb=ram,
                backend='sidecar', supports_gpu=True,
                supports_cpu=(vram < 5),
                supports_cpu_offload=(vram < 5),
                idle_timeout_s=600,
                capabilities={'music_gen': 'acestep' in mid,
                              'singing': True, 'lyrics_input': True},
                quality_score=quality, speed_score=speed,
                tags=['local', 'audio_gen'],
            )
            self.register(entry, persist=False)
            added += 1
        return added

    # ── Capability tier helpers ────────────────────────────────────

    _TIER_RANK = {'embedded': 0, 'observer': 1, 'lite': 2, 'standard': 3,
                  'full': 4, 'compute_host': 5}

    def _get_capability_tier(self) -> str:
        """Get current node capability tier, or 'full' as fallback."""
        try:
            from security.system_requirements import get_tier_name, _capabilities
            tier_name = get_tier_name()
            if tier_name == 'embedded' and _capabilities is None:
                return 'full'
            return tier_name
        except ImportError:
            return 'full'

    @classmethod
    def _tier_sufficient(cls, current: str, required: str) -> bool:
        """Check if current capability tier meets the model's minimum requirement."""
        cur_rank = cls._TIER_RANK.get(current, 4)
        req_rank = cls._TIER_RANK.get(required, 0)
        return cur_rank >= req_rank

    # ── Persistence ───────────────────────────────────────────────

    def _load(self) -> None:
        """Load catalog from JSON file.

        On load, ALL entries have their ``loaded`` state cleared to False
        and ``device`` reset to 'unloaded'. This prevents stale
        "loaded" markers from a previous Nunba session from surviving
        across restarts — the old state claimed models were loaded even
        though the llama-server processes died with the previous session.
        ``ensure_loaded_async`` then trusted the stale state and skipped
        ``start_server()``, leaving the LLM down. See T21 #164.

        ``downloaded`` is NOT cleared — model files persist on disk
        across restarts and the catalog's downloaded flag is still valid.
        """
        if not self._path.exists():
            logger.info(f"No catalog at {self._path} — will auto-populate on first use")
            return
        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            _cleared = 0
            for d in data.get('models', []):
                try:
                    entry = ModelEntry.from_dict(d)
                    # Clear stale loaded state from previous session.
                    # Processes don't survive restart; loaded markers must
                    # not either. The eager-boot + ensure_loaded_async
                    # paths will re-mark as loaded once the server is
                    # actually alive and verified via /v1/models.
                    if entry.loaded:
                        entry.loaded = False
                        entry.device = 'unloaded'
                        entry.active_since = None
                        _cleared += 1
                    self._entries[entry.id] = entry
                except Exception as e:
                    logger.warning(f"Skipped malformed catalog entry: {e}")
            if _cleared:
                logger.info(
                    f"Loaded {len(self._entries)} models from catalog "
                    f"(cleared {_cleared} stale loaded markers)")
                self._save()  # Persist the cleared state
            else:
                logger.info(f"Loaded {len(self._entries)} models from catalog")
        except Exception as e:
            logger.error(f"Failed to load catalog: {e}")

    def _save(self) -> None:
        """Persist catalog to JSON."""
        with self._lock:
            data = {
                'version': 1,
                'updated_at': time.time(),
                'models': [e.to_dict() for e in self._entries.values()],
            }
        try:
            # Use unique temp file per save to prevent WinError 32 when
            # multiple populators call _save() concurrently.
            import tempfile
            fd, tmp_path = tempfile.mkstemp(
                suffix='.tmp', prefix='model_catalog_',
                dir=str(self._path.parent))
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                Path(tmp_path).replace(self._path)
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    logger.warning("_save: swallowed OSError", exc_info=True)
                raise
            self._dirty = False
        except Exception as e:
            logger.error(f"Failed to save catalog: {e}")

    def to_json(self) -> list:
        """Return all entries as JSON-safe list (for API responses)."""
        result = []
        for entry in self._entries.values():
            d = entry.to_dict()
            d['downloaded'] = entry.downloaded
            d['loaded'] = entry.loaded
            d['device'] = entry.device
            d['error'] = entry.error
            result.append(d)
        return result


# ── Singleton ─────────────────────────────────────────────────────
_catalog_instance: Optional[ModelCatalog] = None
_catalog_lock = threading.Lock()


def get_catalog() -> ModelCatalog:
    """Get or create the global ModelCatalog singleton."""
    global _catalog_instance
    if _catalog_instance is None:
        with _catalog_lock:
            if _catalog_instance is None:
                _catalog_instance = ModelCatalog()
                if not _catalog_instance.list_all():
                    _catalog_instance.populate_from_subsystems()
    return _catalog_instance
