"""
ModelOrchestrator — compute-aware model loading for ANY model type.

Lives in HARTOS so all deployment targets (Nunba desktop, embedded, cloud)
share the same orchestration logic. Application-specific loaders (LLM, TTS,
STT, VLM) are registered as plugins at startup via register_loader().

Bridges:
  - ModelCatalog     (what models exist)
  - VRAMManager      (how much GPU is free)
  - ModelLifecycle   (idle eviction, pressure response)
  - Pluggable loaders (registered by the application)

Usage:
    from integrations.service_tools.model_orchestrator import get_orchestrator

    orch = get_orchestrator()

    # Register application-specific loaders (typically at app startup)
    orch.register_loader('llm', my_llm_loader)
    orch.register_loader('tts', my_tts_loader)

    # Auto-select and load the best LLM for current hardware
    entry = orch.auto_load('llm')

    # Load a specific model by ID
    entry = orch.load('tts-chatterbox-turbo')
"""

import logging
import subprocess
import sys
import threading
import time
from typing import Optional, Dict, Any, List, Callable

from integrations.service_tools.model_catalog import (
    ModelCatalog, ModelEntry, get_catalog,
)

logger = logging.getLogger('ModelOrchestrator')


class ModelLoader:
    """Interface for subsystem-specific model loaders.

    Applications implement this to teach the orchestrator how to load/unload
    models of a specific type. Each method receives the catalog entry and
    should return True/False for success.
    """

    def load(self, entry: ModelEntry, run_mode: str) -> bool:
        """Load the model. run_mode is 'gpu', 'cpu', or 'cpu_offload'."""
        raise NotImplementedError

    def unload(self, entry: ModelEntry) -> None:
        """Unload/release the model."""
        pass

    def download(self, entry: ModelEntry) -> bool:
        """Download model files. Return True if successful."""
        return False

    def is_downloaded(self, entry: ModelEntry) -> bool:
        """Check if model files are on disk."""
        return False

    def is_loaded(self, entry: ModelEntry) -> bool:
        """Live probe — is the model actually running right now?

        Default reads `entry.loaded` (the catalog flag). Subclasses that
        have a real liveness signal (a running subprocess, an HTTP
        health endpoint, etc.) SHOULD override this. Catalog state alone
        can drift from reality after idle auto-stop, crashes, or
        external process kills.
        """
        return bool(getattr(entry, 'loaded', False))

    def validate(self, entry: ModelEntry) -> tuple:
        """Post-load capability probe.

        Called by the install pipeline after a successful ``load()`` to
        prove the loaded model actually answers for its declared
        capability — not merely "bytes on disk + subprocess alive".

        Modality-specific subclasses (VLMLoader, TTSLoader, STTLoader,
        LlamaLoader) SHOULD override this with a canned, deterministic
        round-trip (e.g. VLM: describe a 32×32 JPEG; TTS: synthesize a
        fixed phrase; STT: transcribe the TTS output; LLM: complete a
        canned prompt).

        Default returns ``(True, 'no capability probe defined')`` so
        loaders without an override don't gate installs.  Any override
        must be deterministic (fixed input) and fast (<5s wall clock)
        so the install-probe doesn't hang.

        Returns:
            (ok: bool, reason: str) — ``ok=True`` on pass; ``reason``
            is a one-line human-readable diagnostic used by the
            install-progress UI and the dispatcher's fallback logging.
        """
        return (True, 'no capability probe defined')


class ModelOrchestrator:
    """Compute-aware model loader that works for ANY model type.

    Subsystem-specific loaders are registered as plugins. The orchestrator
    handles compute state, VRAM tracking, lifecycle, and swap — the loaders
    only handle the actual load/unload/download mechanics.
    """

    def __init__(self, catalog: Optional[ModelCatalog] = None):
        self._catalog = catalog or get_catalog()
        self._lock = threading.Lock()
        self._loaders: Dict[str, ModelLoader] = {}
        self._scan_downloaded()

    # ── Loader registration ───────────────────────────────────────

    def register_loader(self, model_type: str, loader: ModelLoader) -> None:
        """Register a subsystem-specific loader for a model type.

        Example:
            orch.register_loader('llm', LlamaLoader())
            orch.register_loader('tts', TTSLoader())
        """
        self._loaders[model_type] = loader
        logger.info(f"Registered loader for model_type={model_type}: "
                    f"{loader.__class__.__name__}")

    # ── Compute state ─────────────────────────────────────────────

    def _get_compute_state(self) -> dict:
        """Get current compute availability from VRAMManager singleton."""
        state = {
            'gpu_available': False,
            'gpu_type': 'none',
            'vram_total_gb': 0.0,
            'vram_free_gb': 0.0,
            'ram_free_gb': 4.0,
            'allocations': {},
        }
        try:
            from integrations.service_tools.vram_manager import vram_manager
            gpu = vram_manager.detect_gpu()
            state['gpu_available'] = gpu.get('cuda_available', False) or gpu.get('metal_available', False)
            state['gpu_type'] = 'cuda' if gpu.get('cuda_available') else (
                'metal' if gpu.get('metal_available') else 'none')
            state['vram_total_gb'] = gpu.get('total_gb', 0.0)
            state['vram_free_gb'] = vram_manager.get_free_vram()
            state['allocations'] = vram_manager.get_allocations_display()
        except ImportError:
            pass
        try:
            import psutil
            state['ram_free_gb'] = round(psutil.virtual_memory().available / (1024**3), 2)
        except Exception:
            pass
        return state

    # ── Auto-selection ────────────────────────────────────────────

    def select_best(self, model_type: str, language: Optional[str] = None,
                    require_capability: Optional[Dict[str, Any]] = None,
                    ) -> Optional[ModelEntry]:
        """Select the best model for a type given current compute state."""
        cs = self._get_compute_state()
        return self._catalog.select_best(
            model_type=model_type,
            budget_vram_gb=cs['vram_free_gb'],
            budget_ram_gb=cs['ram_free_gb'],
            gpu_available=cs['gpu_available'],
            language=language,
            require_capability=require_capability,
        )

    # ── Load / Unload ─────────────────────────────────────────────

    def auto_load(self, model_type: str, language: Optional[str] = None,
                  **kwargs) -> Optional[ModelEntry]:
        """Select the best model for a type and load it."""
        entry = self.select_best(model_type, language=language, **kwargs)
        if not entry:
            logger.warning(f"No {model_type} model fits current compute")
            return None
        return self.load(entry.id)

    def ensure_loaded_async(self, model_type: str,
                            language: Optional[str] = None,
                            caller: str = 'unknown',
                            **kwargs) -> None:
        """Fire-and-forget wrapper around auto_load().

        THE single "bring me a model that can do X" entry point for
        every caller (chat fallback on cold LLM, TTS synth on cold
        engine, VLM request on cold vision, etc.). Replaces the old
        pattern where each model type had its own starter helper
        (LlamaConfig.ensure_running_async, tts_engine._switch_backend,
        …) — all of them did select_best → load under different names.

        Capability/task hints flow through **kwargs to select_best so
        language routing, voice_clone, emotion_tags, narration etc.
        all funnel into the same selection logic the catalog already
        indexes. No parallel path.

        Runs in a daemon thread so the caller returns immediately.
        Exceptions never escape — the caller already sent its
        response and this runs in the background.
        """
        import threading

        def _worker():
            try:
                entry = self.auto_load(model_type, language=language, **kwargs)
                if entry:
                    logger.info(
                        f'ensure_loaded_async: loaded {entry.id} '
                        f'on {entry.device} (type={model_type}, caller={caller})'
                    )
                else:
                    logger.warning(
                        f'ensure_loaded_async: no {model_type} fit '
                        f'(caller={caller})'
                    )
            except Exception as e:
                logger.error(f'ensure_loaded_async: worker crashed: {e}')

        threading.Thread(
            target=_worker, daemon=True,
            name=f'ensure_loaded_{model_type}',
        ).start()

    def load(self, model_id: str) -> Optional[ModelEntry]:
        """Load a specific model by ID. Downloads if needed."""
        entry = self._catalog.get(model_id)
        if not entry:
            logger.error(f"Model not found in catalog: {model_id}")
            return None

        if entry.loaded:
            logger.info(f"Model already loaded: {model_id} ({entry.device})")
            return entry

        cs = self._get_compute_state()
        fit = entry.matches_compute(
            cs['vram_free_gb'], cs['ram_free_gb'], cs['gpu_available'])
        if fit == 'impossible':
            if cs['gpu_available'] and entry.vram_gb > 0:
                swapped = self._attempt_swap(entry, cs)
                if swapped:
                    cs = self._get_compute_state()
                    fit = entry.matches_compute(
                        cs['vram_free_gb'], cs['ram_free_gb'], cs['gpu_available'])

            if fit == 'impossible':
                logger.error(f"Cannot load {model_id}: insufficient compute "
                             f"(need {entry.vram_gb}GB VRAM or {entry.ram_gb}GB RAM)")
                self._catalog.mark_error(model_id, 'Insufficient compute')
                return None

        logger.info(f"Loading {model_id} ({entry.model_type}) in {fit} mode...")

        # Allocate VRAM BEFORE load so other models see the reservation.
        # Rolled back on failure.
        if not self._register_vram(entry, fit):
            logger.warning(f"Skipping {model_id}: VRAM full")
            return None
        try:
            success = self._dispatch_load(entry, fit)
            if success:
                self._catalog.mark_loaded(model_id, device=fit)
                self._register_lifecycle(entry)
                self._register_service_tool(entry)
                logger.info(f"Loaded {model_id} on {fit}")
                return entry
            else:
                self._release_vram(entry)
                self._catalog.mark_error(model_id, 'Loader returned failure')
                return None
        except Exception as e:
            self._release_vram(entry)
            logger.error(f"Failed to load {model_id}: {e}")
            self._catalog.mark_error(model_id, str(e))
            return None

    def unload(self, model_id: str) -> bool:
        """Unload a model and release its resources."""
        entry = self._catalog.get(model_id)
        if not entry or not entry.loaded:
            return False

        try:
            self._dispatch_unload(entry)
        except Exception as e:
            logger.warning(f"Unload dispatch failed for {model_id}: {e}")

        self._release_vram(entry)
        self._deregister_service_tool(entry)
        self._catalog.mark_unloaded(model_id)
        logger.info(f"Unloaded {model_id}")
        return True

    def download(self, model_id: str) -> bool:
        """Download a model without loading it."""
        entry = self._catalog.get(model_id)
        if not entry:
            return False
        if entry.downloaded:
            return True
        try:
            success = self._dispatch_download(entry)
            if success:
                self._catalog.mark_downloaded(model_id)
            return success
        except Exception as e:
            logger.error(f"Download failed for {model_id}: {e}")
            self._catalog.mark_error(model_id, str(e))
            return False

    # ── Capability introspection ────────────────────────────────────

    def available_capabilities(self) -> Dict[str, Any]:
        """What this node can do right now — universal, any model type.

        Merges THREE sources into one capability map:
          1. ModelCatalog — static entries (LLM, TTS, STT, VLM, video_gen, audio_gen, etc.)
          2. ServiceToolRegistry — dynamic sidecars (ACE Step, DiffRhythm, txt2img, etc.)
          3. RuntimeToolManager — running services with health status

        Returns dict keyed by category (model_type OR service tag), each with:
          - available: bool
          - loaded: list of loaded model/service IDs
          - can_load: list that fit current compute but aren't loaded
          - capabilities: merged capability set across all entries
          - services: list of running dynamic services in this category

        New categories from dynamic services appear automatically —
        no code changes needed when a new service type registers.
        """
        cs = self._get_compute_state()
        result = {}

        # 1. Catalog models (static + dynamically registered)
        for mt in self._catalog.list_types():
            entries = self._catalog.list_by_type(mt)
            loaded = [e.id for e in entries if e.loaded]
            can_load = [
                e.id for e in entries
                if not e.loaded and e.enabled
                and e.matches_compute(
                    cs['vram_free_gb'], cs['ram_free_gb'],
                    cs['gpu_available']) != 'impossible'
            ]
            # Merge capabilities from all available models
            merged_caps = {}
            for e in entries:
                if e.loaded or (e.enabled and e.matches_compute(
                        cs['vram_free_gb'], cs['ram_free_gb'],
                        cs['gpu_available']) != 'impossible'):
                    for k, v in e.capabilities.items():
                        if v:
                            merged_caps[k] = True
            result[mt] = {
                'available': bool(loaded or can_load),
                'loaded': loaded,
                'can_load': can_load,
                'capabilities': merged_caps,
                'services': [],
            }

        # 2. Dynamic services (ServiceToolRegistry) — may introduce NEW categories
        try:
            from integrations.service_tools.registry import service_tool_registry
            for name, tool_info in service_tool_registry._tools.items():
                tags = getattr(tool_info, 'tags', []) or []
                # Each tag can map to a category
                categories = set()
                for tag in tags:
                    if tag in result:
                        categories.add(tag)
                # If no existing category matches, use the tool name as category
                if not categories:
                    cat = tags[0] if tags else name
                    categories.add(cat)

                for cat in categories:
                    if cat not in result:
                        result[cat] = {
                            'available': True,
                            'loaded': [],
                            'can_load': [],
                            'capabilities': {},
                            'services': [],
                        }
                    result[cat]['available'] = True
                    result[cat]['services'].append(name)
        except Exception:
            pass

        # 3. Running services (RuntimeToolManager) — mark health status
        try:
            from integrations.service_tools.runtime_manager import runtime_tool_manager
            all_status = runtime_tool_manager.get_all_status()
            for tool_name, status in all_status.items():
                if status.get('running'):
                    # Find which category this tool belongs to
                    for cat, info in result.items():
                        if tool_name in info.get('services', []):
                            info['available'] = True
        except Exception:
            pass

        return result

    def can_do(self, model_type: str, capability: str = None) -> bool:
        """Quick check: can this node handle a task right now?

        Works for ANY model type or service category — including ones
        that only exist as dynamic services (not in the catalog).

        Usage:
            orchestrator.can_do('tts')                    # any TTS?
            orchestrator.can_do('audio_gen', 'music_gen') # music specifically?
            orchestrator.can_do('video_gen', 'txt2vid')   # text-to-video?
            orchestrator.can_do('robot_locomotion')        # new category from service?
        """
        caps = self.available_capabilities()
        type_info = caps.get(model_type, {})
        if not type_info.get('available'):
            return False
        if not capability:
            return True
        # Check merged capabilities from models + services
        if type_info.get('capabilities', {}).get(capability):
            return True
        # Deeper check: individual model entries
        all_ids = type_info.get('loaded', []) + type_info.get('can_load', [])
        for mid in all_ids:
            entry = self._catalog.get(mid)
            if entry and entry.capabilities.get(capability):
                return True
        return False

    def capability_prompt(self) -> str:
        """Auto-generate a compact capability summary for LLM prompt injection.

        Dynamically built from live state — new services appear automatically,
        offline services disappear. No hardcoding needed.

        Returns empty string if nothing special is available (no prompt bloat).
        Format designed for small LLMs: one line per capability, action-oriented.
        """
        caps = self.available_capabilities()
        lines = []

        for cat, info in caps.items():
            if not info.get('available'):
                continue
            # Skip LLM (the agent IS the LLM) and embedding (internal)
            if cat in ('llm', 'embedding'):
                continue

            cap_list = sorted(info.get('capabilities', {}).keys())
            loaded = info.get('loaded', [])
            services = info.get('services', [])

            # Build human-readable one-liner
            status_parts = []
            if loaded:
                status_parts.append(f"ready: {', '.join(loaded[:3])}")
            elif services:
                status_parts.append(f"via: {', '.join(services[:3])}")
            elif info.get('can_load'):
                status_parts.append(f"available: {', '.join(info['can_load'][:2])}")
            cap_str = f" — {', '.join(cap_list[:5])}" if cap_list else ''

            label = cat.replace('_', ' ')
            status = f" ({'; '.join(status_parts)})" if status_parts else ''
            lines.append(f"- {label}{cap_str}{status}")

        if not lines:
            return ''

        return (
            'Available capabilities on this node (use via tools — '
            'call generate_media or synthesize_multilingual_audio):\n'
            + '\n'.join(lines)
        )

    # ── Loader dispatch ───────────────────────────────────────────

    def _dispatch_load(self, entry: ModelEntry, run_mode: str) -> bool:
        """Route loading to the registered loader for this model type."""
        loader = self._loaders.get(entry.model_type)
        if loader:
            return loader.load(entry, run_mode)
        # Fallback: try RuntimeToolManager for sidecar-based tools
        return self._load_generic(entry, run_mode)

    def _dispatch_unload(self, entry: ModelEntry) -> None:
        """Route unloading to the registered loader."""
        loader = self._loaders.get(entry.model_type)
        if loader:
            loader.unload(entry)

    def _dispatch_download(self, entry: ModelEntry) -> bool:
        """Route downloading to the registered loader or generic fallback."""
        loader = self._loaders.get(entry.model_type)
        if loader:
            return loader.download(entry)
        if entry.source == 'pip':
            return self._install_pip(entry)
        return False

    def _load_generic(self, entry: ModelEntry, run_mode: str) -> bool:
        """Fallback: try RuntimeToolManager for sidecar-based tools."""
        try:
            from integrations.service_tools.runtime_manager import runtime_tool_manager
            tool_name = entry.id.replace(f'{entry.model_type}-', '')
            result = runtime_tool_manager.setup_tool(tool_name)
            return result.get('running', False)
        except Exception as e:
            logger.warning(f"Generic load failed for {entry.id}: {e}")
            return False

    def _install_pip(self, entry: ModelEntry) -> bool:
        """Install a pip package for a model backend."""
        pkg = entry.files.get('package') or entry.repo_id
        if not pkg:
            return False
        try:
            _kw = dict(capture_output=True, text=True, timeout=300)
            if sys.platform == 'win32':
                _kw['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', pkg, '--quiet'],
                **_kw)
            return result.returncode == 0
        except Exception as e:
            logger.error(f"pip install failed for {pkg}: {e}")
            return False

    # ── VRAM integration ──────────────────────────────────────────
    #
    # KEY ALIGNMENT: VRAMManager._allocations and VRAM_BUDGETS use raw tool
    # names ("whisper", "tts_chatterbox_turbo"). RuntimeToolManager (RTM) also
    # uses these raw names. We use the SAME key convention to avoid
    # double-counting when both RTM and the Orchestrator register the same model.

    _CATALOG_TO_VRAM_KEY = {
        # STT — faster-whisper (primary engine)
        'stt-faster-whisper-tiny':   'whisper_tiny',
        'stt-faster-whisper-base':   'whisper_base',
        'stt-faster-whisper-small':  'whisper_small',
        'stt-faster-whisper-medium': 'whisper_medium',
        'stt-faster-whisper-large':  'whisper_large',
        # STT — sherpa-onnx (CPU-only ONNX, no GPU VRAM)
        'stt-sherpa-moonshine-tiny':  'sherpa_moonshine_tiny',
        'stt-sherpa-moonshine-base':  'sherpa_moonshine_base',
        'stt-sherpa-whisper-tiny':    'sherpa_whisper_tiny',
        'stt-sherpa-whisper-base':    'sherpa_whisper_base',
        'stt-sherpa-whisper-small':   'sherpa_whisper_small',
        'stt-sherpa-whisper-medium':  'sherpa_whisper_medium',
        # STT — legacy fallback IDs (used by old catalog entries)
        'stt-whisper-base':   'whisper_base',
        'stt-whisper-medium': 'whisper_medium',
        'stt-whisper-large':  'whisper_large',
        # TTS
        'tts-chatterbox-turbo': 'tts_chatterbox_turbo',
        'tts-f5-tts':           'tts_f5',
        'tts-indic-parler':     'tts_indic_parler',
        'tts-cosyvoice3':       'tts_cosyvoice3',
        'tts-chatterbox-ml':    'tts_chatterbox_ml',
        'tts-kokoro':           'tts_kokoro',
        # VLM
        'vlm-minicpm-v2': 'minicpm',
        'vlm-qwen3vl':    'qwen3vl',
        # VLM — CPU-only backends (no GPU VRAM tracking needed, included for completeness)
        'vlm-mobilevlm': 'mobilevlm',
        'vlm-clip':       'clip',
        # Video gen
        'video_gen-wan2gp': 'wan2gp',
        'video_gen-ltx2':   'ltx2',
    }

    def _vram_key(self, entry: ModelEntry) -> str:
        """Get the VRAMManager allocation key for a catalog entry.

        For LLMs, always uses 'llm' — there's only one LLM loaded at a time
        (llama-server is single-model). This makes registration idempotent
        regardless of whether LlamaConfig or the Orchestrator registers first.
        """
        if entry.model_type == 'llm':
            return 'llm'
        return self._CATALOG_TO_VRAM_KEY.get(entry.id, entry.id)

    def _register_vram(self, entry: ModelEntry, run_mode: str) -> bool:
        """Register VRAM allocation. Returns False if GPU is full."""
        if run_mode != 'gpu' or entry.vram_gb <= 0:
            return True
        try:
            from integrations.service_tools.vram_manager import vram_manager
            return vram_manager.allocate(self._vram_key(entry))
        except ImportError:
            return True

    def _release_vram(self, entry: ModelEntry) -> None:
        """Release VRAM allocation."""
        try:
            from integrations.service_tools.vram_manager import vram_manager
            tool_key = self._vram_key(entry)
            freed = vram_manager._allocations.pop(tool_key, 0)
            if freed:
                logger.info(f"VRAM released: {tool_key} = {freed}GB")
        except ImportError:
            pass

    # ── Lifecycle integration ─────────────────────────────────────

    def _register_lifecycle(self, entry: ModelEntry) -> None:
        """Register model with ModelLifecycleManager — central tracker for ALL GPU models.

        Every model that loads on GPU MUST register here so the lifecycle
        manager can evict/offload/restore models when VRAM is needed.

        Two LLM-specific eviction policies are applied here based on the
        catalog entry id:

          * **Draft 0.8B classifier** (``llm-qwen3.5-0.8b*``) →
            ``pinned=True``. The 0.8B is first-contact for EVERY chat
            message (speculative_dispatcher.dispatch_draft_first) so
            evicting it on idle means every message cold-starts the
            classifier → full LangChain pipeline fallthrough. Pinning
            costs ~550 MB + mmproj permanently, which is cheaper than
            paying cold-start cost per request.

          * **Main chat LLMs** (``llm-qwen*-2b*``, ``llm-qwen*-4b*``) →
            ``pressure_evict_only=True``. Can still evict when VRAM
            pressure is detected (phase 3), but won't evict on
            passive idle timeout (phase 7). Before this policy, the
            2026-04-11 incident showed the 4B being killed every 5
            minutes mid-session because its 340s idle exceeded the
            300s timeout, even though nothing else needed the VRAM.

          * All other models (STT, TTS, vision) keep the default
            passive idle eviction — their cold start is cheap and
            the VRAM is better spent on the model the user's about
            to use next.
        """
        try:
            from integrations.service_tools.model_lifecycle import (
                get_model_lifecycle_manager, ModelState, ModelDevice, ModelPriority)
            mlm = get_model_lifecycle_manager()
            device = (ModelDevice.GPU if entry.device == 'gpu'
                      else ModelDevice.CPU if entry.device in ('cpu', 'cpu_offload')
                      else ModelDevice.CPU)
            # Map catalog names to offload table names (e.g., 'stt-whisper-large' → 'whisper')
            offload_name = entry.id.split('-')[1] if '-' in entry.id else entry.id
            # Use the offload table key if it exists, otherwise the catalog ID
            from integrations.service_tools.model_lifecycle import CPU_OFFLOAD_TABLE
            if offload_name not in CPU_OFFLOAD_TABLE:
                offload_name = entry.id

            # Eviction-policy flags. See the ModelState docstring for the
            # full rationale and the 2026-04-12 incident context.
            _pinned = False
            _pressure_evict_only = False
            if entry.model_type == 'llm':
                # Use catalog purpose assignment (admin-configurable).
                # Fallback: pattern match on ID for backward compat.
                _purposes = getattr(entry, 'purposes', []) or []
                if 'draft' in _purposes or (
                    not _purposes and any(
                        t in entry.id.lower() for t in ('0.8b', 'draft', 'caption')
                    )
                ):
                    _pinned = True
                else:
                    # Main chat tier — survive idle sweeps, yield under
                    # real VRAM pressure.
                    _pressure_evict_only = True

            _priority = ModelPriority.ACTIVE if entry.model_type == 'llm' else ModelPriority.WARM
            mlm._models[offload_name] = ModelState(
                name=offload_name,
                device=device,
                priority=_priority,
                pinned=_pinned,
                pressure_evict_only=_pressure_evict_only,
            )
            if hasattr(mlm, 'notify_access'):
                mlm.notify_access(offload_name)
            _policy = ('pinned' if _pinned
                       else 'pressure_evict_only' if _pressure_evict_only
                       else 'default_idle_evict')
            logger.info(
                f"Lifecycle: registered {offload_name} "
                f"(device={device}, policy={_policy})"
            )
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"Lifecycle registration failed for {entry.id}: {e}")

    # ── Service tool registration ────────────────────────────────────
    # When a model loads, register its corresponding service tool so
    # the LLM sees the capability via get_tools() → {{tools}}.
    # Each tool class (AceStepTool, CosyVoiceTool, etc.) self-registers
    # with service_tool_registry — we just trigger the registration.

    # Maps catalog model_type or id-prefix to the tool module + class
    _SERVICE_TOOL_MAP = {
        'audio_gen-acestep': ('integrations.service_tools.acestep_tool', 'AceStepTool'),
        'stt-whisper': ('integrations.service_tools.whisper_tool', 'WhisperTool'),
        'tts-cosyvoice3': ('integrations.service_tools.cosyvoice_tool', 'CosyVoiceTool'),
        'tts-f5': ('integrations.service_tools.f5_tts_tool', 'F5TTSTool'),
        'tts-indic-parler': ('integrations.service_tools.indic_parler_tool', 'IndicParlerTool'),
        'tts-pocket': ('integrations.service_tools.pocket_tts_tool', 'PocketTTSTool'),
    }

    def _register_service_tool(self, entry: ModelEntry) -> None:
        """Register loaded model with service_tool_registry."""
        for prefix, (mod_path, cls_name) in self._SERVICE_TOOL_MAP.items():
            if entry.id.startswith(prefix):
                try:
                    import importlib
                    mod = importlib.import_module(mod_path)
                    tool_cls = getattr(mod, cls_name, None)
                    if tool_cls:
                        reg = getattr(tool_cls, 'register', None) or \
                              getattr(tool_cls, 'register_functions', None)
                        if reg:
                            reg()
                            logger.info(f"Service tool registered: {cls_name}")
                except Exception as e:
                    logger.debug(f"Service tool registration skipped for {entry.id}: {e}")
                return

    def _deregister_service_tool(self, entry: ModelEntry) -> None:
        """Remove tool from service_tool_registry on unload."""
        for prefix, (mod_path, cls_name) in self._SERVICE_TOOL_MAP.items():
            if entry.id.startswith(prefix):
                try:
                    from integrations.service_tools.registry import service_tool_registry
                    # Extract tool name from class convention (AceStepTool → acestep)
                    tool_name = prefix.split('-', 1)[-1] if '-' in prefix else prefix
                    if tool_name in service_tool_registry._tools:
                        service_tool_registry._tools[tool_name].is_healthy = False
                        logger.info(f"Service tool deregistered: {tool_name}")
                except Exception:
                    pass
                return

    # ── Model swapping ──────────────────────────────────────────────

    def _attempt_swap(self, needed: ModelEntry, cs: dict) -> bool:
        """Try to free GPU VRAM by evicting a lower-priority model."""
        try:
            from integrations.service_tools.model_lifecycle import (
                get_model_lifecycle_manager)
            mlm = get_model_lifecycle_manager()
            swapped = mlm.request_swap(
                needed_model=needed.id,
                needed_type='gpu',
            )
            if swapped:
                logger.info(f"Swap initiated to make room for {needed.id}")
                time.sleep(1.0)
                try:
                    from integrations.service_tools.vram_manager import vram_manager
                    vram_manager.refresh_gpu_info()
                except Exception:
                    pass
                return True
        except ImportError:
            pass
        return False

    # ── External sync — for bypass paths that load outside orchestrator ──

    def notify_loaded(self, model_type: str, model_name: str,
                      device: str = 'gpu', vram_gb: float = 0) -> None:
        """Called by subsystems that loaded a model outside the orchestrator."""
        entry = self._find_entry_by_name(model_type, model_name)
        if not entry:
            return
        self._catalog.mark_loaded(entry.id, device=device)
        if not entry.downloaded:
            self._catalog.mark_downloaded(entry.id)
        self._register_vram(entry, device)
        self._register_lifecycle(entry)
        logger.info(f"Catalog synced: {entry.id} loaded on {device} (external)")
        # Push capability notification to frontend via SSE + Liquid UI
        _cap_event = {
            'capability': model_type,
            'status': 'ready',
            'name': model_name,
        }
        from core.platform.events import broadcast_sse_safe
        broadcast_sse_safe('capability_update', _cap_event)
        try:
            from core.platform.service_registry import ServiceRegistry
            _lui = ServiceRegistry.get('LiquidUIService')
            if _lui:
                _lui.agent_ui_update('system', {
                    'type': 'notification',
                    'title': 'Capability Ready',
                    'message': f'{model_name} is ready',
                    'severity': 'success',
                })
        except Exception:
            pass

    def notify_unloaded(self, model_type: str, model_name: str) -> None:
        """Called by subsystems that unloaded a model outside the orchestrator."""
        entry = self._find_entry_by_name(model_type, model_name)
        if not entry:
            return
        self._release_vram(entry)
        self._catalog.mark_unloaded(entry.id)
        logger.info(f"Catalog synced: {entry.id} unloaded (external)")

    def notify_downloaded(self, model_type: str, model_name: str) -> None:
        """Called when a model is downloaded outside the orchestrator."""
        entry = self._find_entry_by_name(model_type, model_name)
        if entry and not entry.downloaded:
            self._catalog.mark_downloaded(entry.id)
            logger.info(f"Catalog synced: {entry.id} downloaded (external)")

    def _find_entry_by_name(self, model_type: str, model_name: str) -> Optional[ModelEntry]:
        """Find a catalog entry by type + display name, partial name, or file name."""
        name_lower = model_name.lower()
        for entry in self._catalog.list_by_type(model_type):
            if entry.name == model_name or model_name in entry.id:
                return entry
            if name_lower in entry.name.lower() or name_lower in entry.id:
                return entry
            if entry.files.get('model') and model_name in entry.files['model']:
                return entry
        return None

    # ── Scan downloaded state ─────────────────────────────────────

    def _scan_downloaded(self) -> None:
        """Check which catalog entries have their files on disk."""
        for entry in self._catalog.list_all():
            if entry.source == 'api':
                entry.downloaded = True
                continue
            # Delegate to registered loader if available
            loader = self._loaders.get(entry.model_type)
            if loader:
                try:
                    entry.downloaded = loader.is_downloaded(entry)
                except Exception:
                    pass
            elif entry.source == 'pip':
                pkg = entry.files.get('package') or entry.repo_id
                if pkg:
                    import importlib.util
                    entry.downloaded = importlib.util.find_spec(
                        pkg.replace('-', '_')) is not None

    # ── Dashboard ─────────────────────────────────────────────────

    def reconcile_live_state(self) -> int:
        """Sync catalog flags with the actual runtime state of each loader.

        For every catalog entry, ask the matching loader "is this
        actually loaded right now?" and update the catalog if the
        answer differs from the stored flag. This catches:

        - Workers that idle-auto-stopped (catalog says loaded, worker dead)
        - Workers that crashed (same)
        - Workers started outside the orchestrator (loaded but catalog False)

        Returns the number of entries whose state changed.
        """
        changed = 0
        for entry in self._catalog.list_all():
            loader = self._loaders.get(entry.model_type)
            if loader is None:
                continue
            try:
                live = bool(loader.is_loaded(entry))
            except Exception as e:
                logger.debug(f"is_loaded probe failed for {entry.id}: {e}")
                continue
            if live != bool(entry.loaded):
                if live:
                    self._catalog.mark_loaded(
                        entry.id, device=entry.device or 'unknown',
                    )
                else:
                    self._catalog.mark_unloaded(entry.id)
                    self._release_vram(entry)
                changed += 1
                logger.info(
                    f"reconcile: {entry.id} catalog={entry.loaded} → live={live}"
                )
        return changed

    def get_status(self) -> dict:
        """Full system state for admin dashboard.

        Reconciles catalog flags with live loader state before returning,
        so the UI always sees reality (not a stale snapshot).  Also tags
        each entry with stale-state diagnostics (loader probe outcome,
        worker health), so the UI can render warning pills.
        """
        reconcile_changed = self.reconcile_live_state()

        cs = self._get_compute_state()
        entries = self._catalog.to_json()

        # Per-entry stale-state annotation.  Four axes of suspicion:
        #   1. loader_missing   — catalog claims a model_type with no loader
        #   2. probe_failed     — is_loaded() raised (worker likely gone)
        #   3. has_error        — entry.error is set (last load failed)
        #   4. download_missing — entry marked downloaded but path absent
        # The UI reads `stale` (truthy) + `stale_reasons` (user-facing list).
        stale_total = 0
        for e in entries:
            reasons = []
            mtype = e.get('model_type')
            loader = self._loaders.get(mtype)
            if loader is None:
                reasons.append(f"no loader for type '{mtype}'")
            else:
                try:
                    live = bool(loader.is_loaded(self._catalog.get(e['id'])))
                    if live != bool(e.get('loaded')):
                        reasons.append('catalog/loader state drift')
                except Exception as ex:
                    reasons.append(f'probe failed: {ex}')
            if e.get('error'):
                reasons.append(f"last error: {e['error']}")
            e['stale'] = bool(reasons)
            e['stale_reasons'] = reasons
            if reasons:
                stale_total += 1

        by_type = {}
        for e in entries:
            t = e.get('model_type', 'unknown')
            by_type.setdefault(t, []).append(e)

        loaded = [e for e in entries if e.get('loaded')]
        downloaded = [e for e in entries if e.get('downloaded')]

        return {
            'compute': cs,
            'total_models': len(entries),
            'loaded_count': len(loaded),
            'downloaded_count': len(downloaded),
            'stale_count': stale_total,
            'reconcile_changed': reconcile_changed,
            'models_by_type': by_type,
            'loaded_models': loaded,
            'all_models': entries,
        }


# ── Singleton ─────────────────────────────────────────────────────
_orchestrator_instance: Optional[ModelOrchestrator] = None
_orchestrator_lock = threading.Lock()


def get_orchestrator() -> ModelOrchestrator:
    """Get or create the global ModelOrchestrator singleton."""
    global _orchestrator_instance
    if _orchestrator_instance is None:
        with _orchestrator_lock:
            if _orchestrator_instance is None:
                _orchestrator_instance = ModelOrchestrator()
    return _orchestrator_instance
