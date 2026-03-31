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

        try:
            success = self._dispatch_load(entry, fit)
            if success:
                self._catalog.mark_loaded(model_id, device=fit)
                self._register_vram(entry, fit)
                self._register_lifecycle(entry)
                self._register_service_tool(entry)
                logger.info(f"Loaded {model_id} on {fit}")
                return entry
            else:
                self._catalog.mark_error(model_id, 'Loader returned failure')
                return None
        except Exception as e:
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

    def _register_vram(self, entry: ModelEntry, run_mode: str) -> None:
        """Register VRAM allocation — idempotent (same key = overwrite, not stack)."""
        if run_mode != 'gpu' or entry.vram_gb <= 0:
            return
        try:
            from integrations.service_tools.vram_manager import vram_manager
            tool_key = self._vram_key(entry)
            vram_manager._allocations[tool_key] = entry.vram_gb
            logger.info(f"VRAM allocated: {tool_key} = {entry.vram_gb}GB")
        except ImportError:
            pass

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
        """Register model with ModelLifecycleManager for idle eviction."""
        try:
            from integrations.service_tools.model_lifecycle import get_model_lifecycle_manager
            mlm = get_model_lifecycle_manager()
            if mlm and hasattr(mlm, 'notify_access'):
                mlm.notify_access(entry.id)
        except ImportError:
            pass

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
        self._register_lifecycle(entry)
        logger.info(f"Catalog synced: {entry.id} loaded on {device} (external)")

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

    def get_status(self) -> dict:
        """Full system state for admin dashboard."""
        cs = self._get_compute_state()
        entries = self._catalog.to_json()

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
