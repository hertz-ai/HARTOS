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
                    ) -> Optional[ModelEntry]:
        """Select the best model of a given type for current compute.

        Selection priority:
          1. Filter by type + enabled + compute fit + capability tier
          2. If language specified, prefer models that serve it
          3. Sort by quality_score * speed_score * priority
          4. Return top pick
        """
        candidates = self.list_by_type(model_type)

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
        added += self._populate_tts_models()
        added += self._populate_stt_models()
        added += self._populate_vlm_models()
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
        AUTO_PREFIXES = ('tts-', 'stt-', 'vlm-', 'video_gen-', 'audio_gen-')
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
                    pass
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
