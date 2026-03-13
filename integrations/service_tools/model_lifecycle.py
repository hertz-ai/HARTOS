"""
Model Lifecycle Manager — intelligent load/unload/offload for ML models.

Daemon-driven (like AgentDaemon), tick-based loop that:
  1. Tracks per-model access patterns (last_access, count, device)
  2. Detects VRAM/RAM pressure via real-time nvidia-smi refresh
  3. Evicts idle models (LRU, configurable timeout per model)
  4. Offloads GPU models to CPU when VRAM pressure detected
  5. Reports usage deltas to FederatedAggregator for hive learning
  6. Applies hive-learned placement hints (pre-cache popular models)
  7. Continuous process health monitoring with dead-process recovery
  8. OOM crash detection + auto-restart with resource downgrade
  9. Model swap queue for sequential multi-model workloads
  10. Pressure alerts emitted to EventBus for frontend display

Integration:
  - Extends RuntimeToolManager via lifecycle hooks (composition, not inheritance)
  - Uses VRAMManager for GPU tracking (adds refresh_gpu_info calls)
  - Monitored by NodeWatchdog
  - Reports to FederatedAggregator
  - Exposed via /api/tools/lifecycle endpoint

Terminology: Uses NodeTierLevel CAPABILITY tiers (embedded/lite/standard/full/compute_host),
NOT topology modes (flat/regional/central) or model tiers (fast/balanced/expert).
"""
import collections
import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

LIFECYCLE_STATE_FILE = Path.home() / '.hevolve' / 'lifecycle_state.json'


# ═══════════════════════════════════════════════════════════════
# Enums and State
# ═══════════════════════════════════════════════════════════════

class ModelDevice(Enum):
    UNLOADED = "unloaded"
    GPU = "gpu"
    CPU = "cpu"
    CPU_OFFLOAD = "cpu_offload"


class ModelPriority(Enum):
    ACTIVE = "active"
    WARM = "warm"
    IDLE = "idle"
    EVICTABLE = "evictable"


# Priority sort order: ACTIVE (never touch) → WARM → IDLE → EVICTABLE (first to go)
_PRIORITY_RANK = {
    ModelPriority.ACTIVE: 0,
    ModelPriority.WARM: 1,
    ModelPriority.IDLE: 2,
    ModelPriority.EVICTABLE: 3,
}


@dataclass
class ModelState:
    """Per-model lifecycle state."""
    name: str
    device: ModelDevice = ModelDevice.UNLOADED
    priority: ModelPriority = ModelPriority.IDLE

    # Access tracking
    last_access_time: float = 0.0
    load_time: float = 0.0
    access_count: int = 0
    access_count_session: int = 0

    # Resource tracking
    vram_gb: float = 0.0
    ram_gb: float = 0.0

    # Configuration
    idle_timeout_s: float = 300.0
    is_sidecar: bool = False
    supports_cpu_offload: bool = False

    # Hive hints
    hive_popularity: float = 0.0
    hive_boost: bool = False

    # Inference guard
    active_inference_count: int = 0

    # Crash recovery tracking
    crash_count: int = 0              # Consecutive crashes (resets on successful access)
    last_crash_time: float = 0.0      # Timestamp of last crash
    last_exit_code: Optional[int] = None  # Last process exit code (137/9=OOM kill)
    restart_backoff_s: float = 0.0    # Current backoff delay (exponential)
    downgraded: bool = False          # True if restarted on lower resource tier

    def to_dict(self) -> dict:
        now = time.time()
        return {
            'name': self.name,
            'device': self.device.value,
            'priority': self.priority.value,
            'last_access_time': self.last_access_time,
            'load_time': self.load_time,
            'access_count': self.access_count,
            'idle_seconds': round(now - self.last_access_time, 1) if self.last_access_time else 0,
            'vram_gb': self.vram_gb,
            'ram_gb': self.ram_gb,
            'idle_timeout_s': self.idle_timeout_s,
            'hive_popularity': self.hive_popularity,
            'hive_boost': self.hive_boost,
            'active_inference_count': self.active_inference_count,
            'crash_count': self.crash_count,
            'last_exit_code': self.last_exit_code,
            'downgraded': self.downgraded,
            'healthy': self.device == ModelDevice.UNLOADED or self.crash_count == 0,
        }


# ═══════════════════════════════════════════════════════════════
# Configuration Tables
# ═══════════════════════════════════════════════════════════════

# Default idle timeouts per model (seconds). Expensive-to-reload = longer.
DEFAULT_IDLE_TIMEOUTS: Dict[str, float] = {
    'whisper':          300.0,
    'tts_audio_suite':  600.0,
    'minicpm':          900.0,
    'wan2gp':           600.0,
    'ltx2':             600.0,
    'acestep':          600.0,
    'omniparser':       300.0,
    'clip':             180.0,
    'sentence_transformers': 180.0,
    'mobilevlm':        300.0,
}

# CPU offload capability: (can_offload, cpu_ram_gb, method)
# method: 'torch_to_cpu' | 'restart_cpu' | 'none'
CPU_OFFLOAD_TABLE: Dict[str, Tuple[bool, float, str]] = {
    'whisper':          (True,  0.5,  'torch_to_cpu'),
    'tts_audio_suite':  (True,  2.0,  'restart_cpu'),
    'minicpm':          (False, 0.0,  'none'),
    'wan2gp':           (False, 0.0,  'none'),
    'ltx2':             (True,  4.0,  'restart_cpu'),
    'acestep':          (False, 0.0,  'none'),
    'omniparser':       (True,  2.0,  'torch_to_cpu'),
    'clip':             (True,  0.5,  'torch_to_cpu'),
    'sentence_transformers': (True, 0.3, 'torch_to_cpu'),
    'mobilevlm':        (True,  1.0,  'torch_to_cpu'),
}

# Capability tier requirements per model (uses NodeTierLevel, NOT topology)
# Imported lazily to avoid circular deps
MODEL_MIN_TIER: Dict[str, str] = {
    'whisper':          'standard',
    'tts_audio_suite':  'standard',
    'minicpm':          'full',
    'wan2gp':           'full',
    'ltx2':             'full',
    'acestep':          'full',
    'omniparser':       'full',
    'clip':             'standard',
    'sentence_transformers': 'standard',
    'mobilevlm':        'standard',
}


# ═══════════════════════════════════════════════════════════════
# ModelLifecycleManager
# ═══════════════════════════════════════════════════════════════

class ModelLifecycleManager:
    """Daemon-driven model lifecycle: load, track, offload, evict, report.

    Follows AgentDaemon pattern: tick-based loop, heartbeat to watchdog,
    never blocks user requests.

    Singleton via get_model_lifecycle_manager().
    """

    def __init__(self):
        self._models: Dict[str, ModelState] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._interval = int(os.environ.get('HEVOLVE_LIFECYCLE_INTERVAL', '15'))
        self._tick_count = 0

        # Pressure thresholds (configurable via env)
        self._vram_pressure_pct = float(
            os.environ.get('HEVOLVE_VRAM_PRESSURE_PCT', '85'))
        self._ram_pressure_pct = float(
            os.environ.get('HEVOLVE_RAM_PRESSURE_PCT', '90'))
        self._cpu_pressure_pct = float(
            os.environ.get('HEVOLVE_CPU_PRESSURE_PCT', '80'))
        self._disk_free_min_gb = float(
            os.environ.get('HEVOLVE_DISK_FREE_MIN_GB', '2.0'))

        # Throttle flags (read by AgentDaemon to reduce dispatch rate)
        self._cpu_throttle_active = False
        self._disk_throttle_active = False

        # Hive placement hints from FederatedAggregator
        self._hive_hints: Dict[str, float] = {}

        # Cached node tier
        self._node_tier = None

        # ── Crash recovery ────────────────────────────────────────
        self._max_crash_restarts = int(
            os.environ.get('HEVOLVE_MAX_CRASH_RESTARTS', '3'))
        self._base_backoff_s = 5.0      # First retry after 5s
        self._max_backoff_s = 300.0     # Cap at 5 min
        self._restart_pending: Dict[str, float] = {}  # name → retry_after timestamp

        # ── Swap queue ────────────────────────────────────────────
        # When model B is needed but A occupies the GPU, A gets evicted
        # and B loads. A is queued for restore when B finishes/idles.
        self._swap_queue: collections.deque = collections.deque(maxlen=8)
        # Each entry: {'name': str, 'device': str, 'evicted_for': str, 'timestamp': float}

        # ── Pressure alert state (debounce) ───────────────────────
        self._last_pressure_alert: Dict[str, float] = {}  # type → timestamp
        self._pressure_alert_cooldown = 60.0  # seconds between alerts of same type

    # ── Daemon lifecycle ──────────────────────────────────────

    def start(self):
        """Start the lifecycle daemon and register hooks."""
        with self._lock:
            if self._running:
                return
            self._running = True

        # Register hooks with RuntimeToolManager
        try:
            from .runtime_manager import runtime_tool_manager
            runtime_tool_manager.register_lifecycle_hook(
                'on_tool_started', self._on_tool_started)
            runtime_tool_manager.register_lifecycle_hook(
                'on_tool_stopped', self._on_tool_stopped)
        except Exception as e:
            logger.debug(f"Lifecycle hook registration skipped: {e}")

        # Sync from current RTM state
        self._sync_from_rtm()

        # Detect node tier
        self._detect_tier()

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(f"Model lifecycle manager started (interval={self._interval}s)")

    def stop(self):
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self):
        while self._running:
            time.sleep(self._interval)
            if not self._running:
                break
            # Heartbeat to watchdog
            try:
                from security.node_watchdog import get_watchdog
                wd = get_watchdog()
                if wd:
                    wd.heartbeat('model_lifecycle')
            except Exception:
                pass
            try:
                self._tick()
            except Exception as e:
                logger.debug(f"Model lifecycle tick error: {e}")

    def _wd_heartbeat(self):
        """Send heartbeat to watchdog between potentially blocking phases."""
        try:
            from security.node_watchdog import get_watchdog
            wd = get_watchdog()
            if wd:
                wd.heartbeat('model_lifecycle')
        except Exception:
            pass

    def _tick(self):
        """Single lifecycle pass with heartbeat checkpoints between phases."""
        self._tick_count += 1

        # Guardrail: circuit breaker
        try:
            from security.hive_guardrails import HiveCircuitBreaker
            if HiveCircuitBreaker.is_halted():
                return
        except (ImportError, AttributeError):
            pass

        # Phase 1: Refresh real GPU state (may call nvidia-smi subprocess)
        self._refresh_memory_state()
        self._wd_heartbeat()

        # Phase 2: Update priorities
        self._update_priorities()

        # Phase 3: VRAM pressure response
        if self._detect_vram_pressure():
            self._respond_to_vram_pressure()
        self._wd_heartbeat()

        # Phase 4: RAM pressure response
        if self._detect_ram_pressure():
            self._respond_to_ram_pressure()

        # Phase 5: CPU pressure response
        cpu_pressure = self._detect_cpu_pressure()
        self._cpu_throttle_active = cpu_pressure
        if cpu_pressure:
            self._respond_to_cpu_pressure()

        # Phase 6: Disk pressure response
        disk_pressure = self._detect_disk_pressure()
        self._disk_throttle_active = disk_pressure
        self._wd_heartbeat()

        # Phase 7: Background idle eviction
        self._evict_idle_models()

        # Phase 8: Apply hive hints (every 4th tick, ~60s)
        if self._tick_count % 4 == 0:
            self._apply_hive_hints()

        # Phase 9: Report to federation (every 6th tick, ~90s)
        if self._tick_count % 6 == 0:
            self._report_to_federation()
        self._wd_heartbeat()

        # Phase 10: Process health check + crash recovery
        self._check_process_health()
        self._wd_heartbeat()

        # Phase 11: Process pending crash restarts (with backoff)
        self._process_restart_queue()

        # Phase 12: Swap queue — restore evicted models when space frees up
        self._process_swap_queue()

        # Phase 13: Pressure alerts to frontend (debounced)
        self._emit_pressure_alerts()

    # ── Hook callbacks (from RuntimeToolManager) ──────────────

    def _on_tool_started(self, tool_name: str, **kwargs):
        """Called when RTM starts a tool."""
        device_str = kwargs.get('device', 'gpu')
        offload_mode = kwargs.get('offload_mode', 'gpu')
        is_inprocess = kwargs.get('inprocess', False)

        if offload_mode == 'cpu_only':
            device = ModelDevice.CPU
        elif offload_mode == 'cpu_offload':
            device = ModelDevice.CPU_OFFLOAD
        else:
            device = ModelDevice.GPU

        offload_info = CPU_OFFLOAD_TABLE.get(tool_name, (False, 0.0, 'none'))
        timeout = DEFAULT_IDLE_TIMEOUTS.get(tool_name, 300.0)

        # Override timeout from env
        env_timeout = os.environ.get(
            f'HEVOLVE_{tool_name.upper()}_IDLE_TIMEOUT')
        if env_timeout:
            try:
                timeout = float(env_timeout)
            except ValueError:
                pass

        from .vram_manager import VRAM_BUDGETS
        budget = VRAM_BUDGETS.get(tool_name, (0, 0))
        vram_gb = budget[1] if device == ModelDevice.GPU else 0.0
        ram_gb = offload_info[1] if device == ModelDevice.CPU else 0.0

        now = time.time()
        with self._lock:
            self._models[tool_name] = ModelState(
                name=tool_name,
                device=device,
                priority=ModelPriority.WARM,
                last_access_time=now,
                load_time=now,
                idle_timeout_s=timeout,
                is_sidecar=not is_inprocess,
                supports_cpu_offload=offload_info[0],
                vram_gb=vram_gb,
                ram_gb=ram_gb,
            )

    def _on_tool_stopped(self, tool_name: str, **kwargs):
        """Called when RTM stops a tool."""
        with self._lock:
            state = self._models.get(tool_name)
            if state:
                state.device = ModelDevice.UNLOADED
                state.priority = ModelPriority.IDLE
                state.vram_gb = 0.0
                state.ram_gb = 0.0
                state.active_inference_count = 0

    # ── Access tracking (called by tool wrappers) ─────────────

    @contextmanager
    def inference_guard(self, tool_name: str):
        """Context manager — prevents eviction during active inference."""
        with self._lock:
            state = self._models.get(tool_name)
            if state:
                state.active_inference_count += 1
                state.last_access_time = time.time()
                state.access_count += 1
                state.access_count_session += 1
        try:
            yield
        finally:
            with self._lock:
                state = self._models.get(tool_name)
                if state:
                    state.active_inference_count = max(
                        0, state.active_inference_count - 1)

    def notify_access(self, tool_name: str):
        """Lightweight access notification. Updates timestamps + counters.

        Also resets crash count on successful access — confirms recovery.
        """
        with self._lock:
            state = self._models.get(tool_name)
            if state:
                state.last_access_time = time.time()
                state.access_count += 1
                state.access_count_session += 1
                # Successful access = process is healthy, reset crash state
                if state.crash_count > 0:
                    logger.info(f"Model {tool_name} recovered after "
                                f"{state.crash_count} crash(es)")
                    state.crash_count = 0
                    state.restart_backoff_s = 0.0
                    state.downgraded = False

    # ── Tick phases ───────────────────────────────────────────

    def _refresh_memory_state(self):
        """Re-read actual GPU state and sync with RTM running state."""
        try:
            from .vram_manager import vram_manager
            vram_manager.refresh_gpu_info()
        except Exception:
            pass

        # Sync with RTM's actual process state
        try:
            from .runtime_manager import runtime_tool_manager, TOOL_CONFIGS
            for tool_name in TOOL_CONFIGS:
                is_alive = runtime_tool_manager._is_server_alive(tool_name)
                with self._lock:
                    state = self._models.get(tool_name)
                    if state and not is_alive and state.device != ModelDevice.UNLOADED:
                        state.device = ModelDevice.UNLOADED
                        state.priority = ModelPriority.IDLE
                        state.vram_gb = 0.0
                        state.ram_gb = 0.0
        except Exception:
            pass

    def _update_priorities(self):
        """Recalculate priority for every tracked model."""
        now = time.time()
        with self._lock:
            for state in self._models.values():
                if state.device == ModelDevice.UNLOADED:
                    continue

                if state.active_inference_count > 0:
                    state.priority = ModelPriority.ACTIVE
                    continue

                idle_s = (now - state.last_access_time
                          if state.last_access_time else float('inf'))

                if idle_s < state.idle_timeout_s * 0.5:
                    state.priority = ModelPriority.WARM
                elif idle_s < state.idle_timeout_s:
                    # Hive boost extends warm period
                    if state.hive_boost:
                        state.priority = ModelPriority.WARM
                    else:
                        state.priority = ModelPriority.IDLE
                else:
                    state.priority = ModelPriority.EVICTABLE

    def _detect_vram_pressure(self) -> bool:
        """True if current VRAM usage exceeds threshold."""
        try:
            from .vram_manager import vram_manager
            pct = vram_manager.get_vram_usage_pct()
            return pct >= self._vram_pressure_pct
        except Exception:
            return False

    def _detect_ram_pressure(self) -> bool:
        """True if system RAM usage exceeds threshold."""
        try:
            import psutil
            return psutil.virtual_memory().percent >= self._ram_pressure_pct
        except ImportError:
            return False

    def _detect_cpu_pressure(self) -> bool:
        """True if CPU usage exceeds threshold."""
        try:
            import psutil
            pct = psutil.cpu_percent(interval=None)
            return pct >= self._cpu_pressure_pct
        except ImportError:
            return False

    def _detect_disk_pressure(self) -> bool:
        """True if free disk space is below minimum threshold."""
        try:
            code_root = os.environ.get(
                'HEVOLVE_CODE_ROOT',
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            usage = shutil.disk_usage(code_root)
            free_gb = usage.free / (1024 ** 3)
            return free_gb < self._disk_free_min_gb
        except Exception:
            return False

    def _respond_to_vram_pressure(self):
        """Evict or offload GPU models to relieve VRAM pressure.

        Strategy: EVICTABLE first (LRU) → IDLE offload to CPU → WARM offload.
        Never touches ACTIVE models.
        """
        with self._lock:
            candidates = sorted(
                [s for s in self._models.values()
                 if s.device in (ModelDevice.GPU, ModelDevice.CPU_OFFLOAD)
                 and s.priority != ModelPriority.ACTIVE],
                key=lambda s: (_PRIORITY_RANK.get(s.priority, 99),
                               s.last_access_time)
            )
            # Work on a copy of names to avoid modifying dict during iteration
            candidate_names = [(s.name, s.priority, s.supports_cpu_offload)
                               for s in candidates]

        for name, priority, can_offload in reversed(candidate_names):
            if not self._detect_vram_pressure():
                break  # Pressure relieved

            if priority == ModelPriority.EVICTABLE:
                self._do_unload(name)
            elif can_offload:
                self._do_offload_to_cpu(name)
            elif priority in (ModelPriority.IDLE, ModelPriority.EVICTABLE):
                self._do_unload(name)

    def _respond_to_ram_pressure(self):
        """Unload CPU models under RAM pressure (LRU first)."""
        with self._lock:
            candidates = sorted(
                [s for s in self._models.values()
                 if s.device == ModelDevice.CPU
                 and s.priority != ModelPriority.ACTIVE],
                key=lambda s: s.last_access_time
            )
            names = [s.name for s in candidates]

        for name in names:
            if not self._detect_ram_pressure():
                break
            self._do_unload(name)

    def _respond_to_cpu_pressure(self):
        """Reduce system load by evicting non-essential models.

        Under CPU pressure, background model processes consume cycles.
        Evict EVICTABLE and IDLE models to free CPU for user-facing work.
        """
        with self._lock:
            evictable = [
                s.name for s in self._models.values()
                if s.priority in (ModelPriority.EVICTABLE, ModelPriority.IDLE)
                and s.device != ModelDevice.UNLOADED
                and s.active_inference_count == 0
            ]
        for name in evictable:
            logger.info(f"Lifecycle: evicting '{name}' due to CPU pressure")
            self._do_unload(name)

    def _evict_idle_models(self):
        """Background GC: unload models past their idle timeout."""
        now = time.time()
        with self._lock:
            evictable = [
                s.name for s in self._models.values()
                if s.priority == ModelPriority.EVICTABLE
                and s.device != ModelDevice.UNLOADED
                and s.active_inference_count == 0
            ]

        for name in evictable:
            with self._lock:
                state = self._models.get(name)
            if not state:
                continue
            idle_s = now - state.last_access_time if state.last_access_time else float('inf')
            if idle_s > state.idle_timeout_s:
                logger.info(
                    f"Lifecycle: evicting idle model '{name}' "
                    f"(idle {idle_s:.0f}s > timeout {state.idle_timeout_s:.0f}s)")
                self._do_unload(name)

    # ── Load/Unload/Offload operations ────────────────────────

    def _do_unload(self, tool_name: str):
        """Unload a model via RuntimeToolManager."""
        try:
            from .runtime_manager import runtime_tool_manager
            runtime_tool_manager.stop_tool(tool_name)
            # on_tool_stopped hook will update our state
            logger.info(f"Lifecycle: unloaded '{tool_name}'")
        except Exception as e:
            logger.debug(f"Lifecycle unload error for {tool_name}: {e}")

    def _do_offload_to_cpu(self, tool_name: str) -> bool:
        """Migrate a GPU model to CPU."""
        offload_info = CPU_OFFLOAD_TABLE.get(tool_name)
        if not offload_info or not offload_info[0]:
            return False

        with self._lock:
            state = self._models.get(tool_name)
            if not state or state.device == ModelDevice.CPU:
                return False
            if state.active_inference_count > 0:
                return False

        _, cpu_ram_gb, method = offload_info
        success = False

        if method == 'torch_to_cpu':
            success = self._offload_torch_to_cpu(tool_name)
        elif method == 'restart_cpu':
            success = self._offload_via_restart(tool_name)

        if success:
            with self._lock:
                state = self._models.get(tool_name)
                if state:
                    old_vram = state.vram_gb
                    state.device = ModelDevice.CPU
                    state.vram_gb = 0.0
                    state.ram_gb = cpu_ram_gb
            try:
                from .vram_manager import vram_manager
                vram_manager.release(tool_name)
            except Exception:
                pass
            logger.info(f"Lifecycle: offloaded '{tool_name}' to CPU")

        return success

    def _offload_torch_to_cpu(self, tool_name: str) -> bool:
        """Move in-process torch model tensors to CPU."""
        if tool_name == 'whisper':
            try:
                from .whisper_tool import _whisper_model
                if _whisper_model is not None:
                    _whisper_model.cpu()
                    from .vram_manager import clear_cuda_cache
                    clear_cuda_cache()
                    return True
            except Exception as e:
                logger.debug(f"Whisper CPU offload failed: {e}")
        return False

    def _offload_via_restart(self, tool_name: str) -> bool:
        """Stop and restart a sidecar tool with cpu_only offload mode."""
        try:
            from .runtime_manager import runtime_tool_manager, TOOL_CONFIGS
            runtime_tool_manager.stop_tool(tool_name)
            config = TOOL_CONFIGS.get(tool_name)
            if config and not config.get('is_inprocess'):
                result = runtime_tool_manager._start_sidecar(
                    tool_name, config, 'cpu_only')
                return result.get('running', False)
        except Exception as e:
            logger.debug(f"Restart offload for {tool_name} failed: {e}")
        return False

    # ── Phase 10-13: Process Health, Crash Recovery, Swap, Alerts ──

    # Exit codes that indicate OOM kill
    _OOM_EXIT_CODES = {
        137,   # Linux SIGKILL (128 + 9) — typical OOM killer
        -9,    # Python representation of SIGKILL
        9,     # Raw SIGKILL
        3221225477,  # Windows 0xC0000005 — access violation (often OOM-related)
        3221225725,  # Windows 0xC00000FD — stack overflow
    }

    def _check_process_health(self):
        """Phase 10: Detect dead processes and classify crash vs clean exit.

        For each model we think is loaded, verify the actual process is alive.
        If dead: record exit code, classify OOM vs clean, queue restart.
        """
        dead_models = []

        # Check RTM-managed sidecar processes
        try:
            from .runtime_manager import runtime_tool_manager, TOOL_CONFIGS
            for tool_name in TOOL_CONFIGS:
                with self._lock:
                    state = self._models.get(tool_name)
                    if not state or state.device == ModelDevice.UNLOADED:
                        continue

                proc = runtime_tool_manager._processes.get(tool_name)
                config = TOOL_CONFIGS.get(tool_name, {})

                if config.get('is_inprocess'):
                    # In-process models — check module-level state
                    if not runtime_tool_manager._is_server_alive(tool_name):
                        dead_models.append((tool_name, None, 'inprocess'))
                elif proc is not None:
                    exit_code = proc.poll()
                    if exit_code is not None:
                        dead_models.append((tool_name, exit_code, 'sidecar'))
                else:
                    # No process object but state says loaded — stale state
                    dead_models.append((tool_name, None, 'orphan'))
        except Exception as e:
            logger.debug(f"Health check RTM scan error: {e}")

        # Check LLM (llama.cpp) process — not managed by RTM
        try:
            self._check_llm_health(dead_models)
        except Exception:
            pass

        # Process each dead model
        for tool_name, exit_code, proc_type in dead_models:
            self._handle_dead_process(tool_name, exit_code, proc_type)

    def _check_llm_health(self, dead_models: list):
        """Check llama.cpp server health (separate from RTM sidecar tools)."""
        with self._lock:
            state = self._models.get('llm')
            if not state or state.device == ModelDevice.UNLOADED:
                return

        try:
            from llama.llama_config import LlamaConfig
            config = LlamaConfig()
            if config.server_process is not None:
                exit_code = config.server_process.poll()
                if exit_code is not None:
                    dead_models.append(('llm', exit_code, 'llm_server'))
            elif state.device != ModelDevice.UNLOADED:
                # No process object but we think it's loaded — verify via HTTP
                if not config.check_server_running():
                    dead_models.append(('llm', None, 'llm_server'))
        except ImportError:
            pass

    def _handle_dead_process(self, tool_name: str, exit_code: Optional[int],
                             proc_type: str):
        """Classify crash, update state, queue restart if appropriate."""
        is_oom = exit_code in self._OOM_EXIT_CODES if exit_code is not None else False
        crash_type = 'oom' if is_oom else ('crash' if exit_code else 'disappeared')

        logger.warning(
            f"Dead process detected: {tool_name} "
            f"(exit_code={exit_code}, type={crash_type}, proc={proc_type})")

        with self._lock:
            state = self._models.get(tool_name)
            if not state:
                return

            old_device = state.device
            old_vram = state.vram_gb

            # Update state to unloaded
            state.device = ModelDevice.UNLOADED
            state.priority = ModelPriority.IDLE
            state.crash_count += 1
            state.last_crash_time = time.time()
            state.last_exit_code = exit_code
            state.vram_gb = 0.0
            state.ram_gb = 0.0
            state.active_inference_count = 0

            # Exponential backoff: 5s, 10s, 20s, 40s... capped at 300s
            state.restart_backoff_s = min(
                self._base_backoff_s * (2 ** (state.crash_count - 1)),
                self._max_backoff_s
            )

            should_restart = state.crash_count <= self._max_crash_restarts

        # Release VRAM allocation
        try:
            from .vram_manager import vram_manager
            vram_manager.release(tool_name)
        except Exception:
            pass

        # Release from RTM process table
        try:
            from .runtime_manager import runtime_tool_manager
            runtime_tool_manager._processes.pop(tool_name, None)
            runtime_tool_manager._ports.pop(tool_name, None)
        except Exception:
            pass

        # Sync catalog state
        try:
            from integrations.service_tools.model_orchestrator import get_orchestrator
            get_orchestrator().notify_unloaded(
                self._guess_model_type(tool_name), tool_name)
        except Exception:
            pass

        # Emit crash event
        self._emit_event('model.crash', {
            'model': tool_name,
            'crash_type': crash_type,
            'exit_code': exit_code,
            'crash_count': state.crash_count if state else 0,
            'will_restart': should_restart,
        })

        # Queue restart with backoff (if under max retries)
        if should_restart:
            retry_after = time.time() + state.restart_backoff_s
            downgrade = is_oom  # OOM → restart on lower resource tier
            self._restart_pending[tool_name] = {
                'retry_after': retry_after,
                'downgrade': downgrade,
                'old_device': old_device.value if old_device else 'gpu',
            }
            logger.info(
                f"Queued restart for {tool_name} in {state.restart_backoff_s:.0f}s"
                f" (attempt {state.crash_count}/{self._max_crash_restarts})"
                f"{' [DOWNGRADE]' if downgrade else ''}")
        else:
            logger.error(
                f"Model {tool_name} exceeded max restarts "
                f"({self._max_crash_restarts}), giving up. "
                f"Manual intervention required.")
            self._emit_event('model.restart_exhausted', {
                'model': tool_name,
                'crash_count': state.crash_count if state else 0,
            })

    def _process_restart_queue(self):
        """Phase 11: Process pending crash restarts with exponential backoff."""
        now = time.time()
        ready = []
        with self._lock:
            for name, info in list(self._restart_pending.items()):
                if isinstance(info, dict) and now >= info.get('retry_after', 0):
                    ready.append((name, info))
                    del self._restart_pending[name]

        for name, info in ready:
            downgrade = info.get('downgrade', False)
            old_device = info.get('old_device', 'gpu')

            # Decide restart mode
            if downgrade and old_device == 'gpu':
                restart_mode = 'cpu_offload'  # OOM on GPU → try CPU offload
            elif downgrade and old_device == 'cpu_offload':
                restart_mode = 'cpu_only'     # OOM on offload → pure CPU
            else:
                restart_mode = old_device      # Same mode as before

            logger.info(f"Restarting {name} in {restart_mode} mode"
                        f" (was {old_device}, downgrade={downgrade})")

            success = False
            if name == 'llm':
                success = self._restart_llm(restart_mode)
            else:
                success = self._restart_rtm_tool(name, restart_mode)

            if success:
                with self._lock:
                    state = self._models.get(name)
                    if state:
                        state.downgraded = downgrade
                self._emit_event('model.restarted', {
                    'model': name, 'mode': restart_mode, 'downgraded': downgrade})
            else:
                # Re-queue with increased backoff
                with self._lock:
                    state = self._models.get(name)
                    if state and state.crash_count <= self._max_crash_restarts:
                        state.crash_count += 1
                        state.restart_backoff_s = min(
                            state.restart_backoff_s * 2, self._max_backoff_s)
                        self._restart_pending[name] = {
                            'retry_after': now + state.restart_backoff_s,
                            'downgrade': downgrade,
                            'old_device': restart_mode,
                        }

    def _restart_llm(self, mode: str) -> bool:
        """Restart llama.cpp server in specified mode."""
        try:
            from llama.llama_config import LlamaConfig
            config = LlamaConfig()
            config.stop_server()  # Clean up any zombie state
            config.config['use_gpu'] = (mode == 'gpu')
            config._save_config()
            return config.start_server()
        except Exception as e:
            logger.error(f"LLM restart failed: {e}")
            return False

    def _restart_rtm_tool(self, tool_name: str, mode: str) -> bool:
        """Restart a RuntimeToolManager-managed tool."""
        try:
            from .runtime_manager import runtime_tool_manager, TOOL_CONFIGS
            config = TOOL_CONFIGS.get(tool_name)
            if not config:
                return False
            if config.get('is_inprocess'):
                result = runtime_tool_manager._start_inprocess(tool_name, config)
            else:
                result = runtime_tool_manager._start_sidecar(
                    tool_name, config, mode)
            return result.get('running', False)
        except Exception as e:
            logger.error(f"RTM restart failed for {tool_name}: {e}")
            return False

    # ── Swap Queue ───────────────────────────────────────────────

    def request_swap(self, needed_model: str, needed_type: str = 'gpu',
                     evict_target: Optional[str] = None) -> bool:
        """Request a model swap: evict lowest-priority GPU model to make room.

        Called by orchestrator when a model can't fit alongside current loads.
        The evicted model is queued for restoration when the new model idles.

        Returns True if swap was initiated, False if nothing can be evicted.
        """
        with self._lock:
            # Find eviction candidate: lowest priority, non-ACTIVE GPU model
            if evict_target:
                candidates = [evict_target]
            else:
                gpu_models = sorted(
                    [s for s in self._models.values()
                     if s.device == ModelDevice.GPU
                     and s.priority != ModelPriority.ACTIVE
                     and s.active_inference_count == 0
                     and s.name != needed_model],
                    key=lambda s: (
                        _PRIORITY_RANK.get(s.priority, 99),
                        s.last_access_time,
                    )
                )
                candidates = [s.name for s in gpu_models]

        if not candidates:
            logger.warning(f"Swap failed for {needed_model}: no evictable GPU model")
            return False

        evicted = candidates[-1]  # Lowest priority, oldest access
        logger.info(f"Swap: evicting '{evicted}' to make room for '{needed_model}'")

        # Record in swap queue BEFORE eviction
        self._swap_queue.append({
            'name': evicted,
            'device': 'gpu',
            'evicted_for': needed_model,
            'timestamp': time.time(),
        })

        # Evict
        self._do_unload(evicted)

        self._emit_event('model.swapped', {
            'evicted': evicted,
            'loaded': needed_model,
            'swap_queue_depth': len(self._swap_queue),
        })
        return True

    def _process_swap_queue(self):
        """Phase 12: Restore evicted models when the model that displaced them idles.

        If model B evicted model A, and B is now IDLE/EVICTABLE, restore A if VRAM allows.
        """
        if not self._swap_queue:
            return

        restored = []
        for entry in list(self._swap_queue):
            evicted_for = entry.get('evicted_for')
            evicted_name = entry.get('name')

            with self._lock:
                # Check if the model that caused the eviction has become idle
                displacer = self._models.get(evicted_for)
                if displacer and displacer.priority in (
                        ModelPriority.IDLE, ModelPriority.EVICTABLE):
                    pass  # Displacer is idle — consider restoring
                elif displacer and displacer.device == ModelDevice.UNLOADED:
                    pass  # Displacer already gone — restore
                else:
                    continue  # Displacer still active — skip

            # Check if VRAM has room
            try:
                from .vram_manager import vram_manager, VRAM_BUDGETS
                budget = VRAM_BUDGETS.get(evicted_name, (0, 0))
                if vram_manager.get_free_vram() < budget[1]:
                    continue  # Still no room
            except Exception:
                continue

            # Restore the evicted model
            logger.info(f"Swap queue: restoring '{evicted_name}' "
                        f"(displaced by '{evicted_for}' which is now idle)")
            success = self._restart_rtm_tool(evicted_name, entry.get('device', 'gpu'))
            if success:
                restored.append(entry)

        for entry in restored:
            try:
                self._swap_queue.remove(entry)
            except ValueError:
                pass

    # ── Pressure Alerts ──────────────────────────────────────────

    def _emit_pressure_alerts(self):
        """Phase 13: Emit debounced pressure events to EventBus for frontend."""
        now = time.time()
        alerts = []

        if self._detect_vram_pressure():
            alerts.append(('vram', 'GPU memory is under pressure — models may be evicted'))
        if self._detect_ram_pressure():
            alerts.append(('ram', 'System memory is under pressure — performance may degrade'))
        if self._detect_cpu_pressure():
            alerts.append(('cpu', 'CPU is under heavy load — inference may be slower'))
        if self._detect_disk_pressure():
            alerts.append(('disk', 'Low disk space — downloads and caching disabled'))

        for ptype, message in alerts:
            last = self._last_pressure_alert.get(ptype, 0)
            if now - last >= self._pressure_alert_cooldown:
                self._last_pressure_alert[ptype] = now
                self._emit_event('system.pressure', {
                    'type': ptype,
                    'message': message,
                    'timestamp': now,
                    'throttle_factor': self._calculate_throttle_factor(),
                })

    # ── Event emission helper ────────────────────────────────────

    def _emit_event(self, event_type: str, data: dict):
        """Emit an event to the EventBus (non-blocking, safe to fail)."""
        try:
            from core.platform.events import emit_event
            emit_event(event_type, data)
        except Exception:
            pass  # EventBus not bootstrapped — silent

    def _guess_model_type(self, tool_name: str) -> str:
        """Map tool name to model_type for catalog sync."""
        if tool_name == 'llm' or 'llama' in tool_name:
            return 'llm'
        if 'tts' in tool_name or 'voice' in tool_name:
            return 'tts'
        if 'whisper' in tool_name or 'stt' in tool_name:
            return 'stt'
        if 'minicpm' in tool_name or 'vlm' in tool_name or 'vision' in tool_name:
            return 'vlm'
        return tool_name

    # ── Hive intelligence ─────────────────────────────────────

    def _apply_hive_hints(self):
        """Apply hive-learned placement hints to local priorities."""
        try:
            from integrations.agent_engine.federated_aggregator import (
                get_federated_aggregator)
            fed = get_federated_aggregator()
            aggregated = fed.aggregate_lifecycle()
            if not aggregated:
                return

            popularity = aggregated.get('popularity', {})
            with self._lock:
                for name, score in popularity.items():
                    state = self._models.get(name)
                    if state:
                        state.hive_popularity = score
                        state.hive_boost = score > 0.6
                    else:
                        self._hive_hints[name] = score
        except Exception as e:
            logger.debug(f"Hive hints error: {e}")

    def _report_to_federation(self):
        """Send local model usage stats to FederatedAggregator."""
        try:
            from integrations.agent_engine.federated_aggregator import (
                get_federated_aggregator)

            node_id = ''
            try:
                from security.node_integrity import get_node_identity
                node_id = get_node_identity().get('node_id', '')
            except Exception:
                pass

            models_stats = {}
            with self._lock:
                for name, state in self._models.items():
                    if state.device != ModelDevice.UNLOADED:
                        rate = (state.access_count_session /
                                max(1, self._interval * 6))
                        idle_s = (time.time() - state.last_access_time
                                  if state.last_access_time else 0)
                        models_stats[name] = {
                            'device': state.device.value,
                            'access_rate': round(rate, 4),
                            'idle_s': round(idle_s, 1),
                        }
                        state.access_count_session = 0

            if models_stats:
                delta = {
                    'models': models_stats,
                    'node_id': node_id,
                    'timestamp': time.time(),
                }
                fed = get_federated_aggregator()
                fed.receive_lifecycle_delta(node_id, delta)
        except Exception as e:
            logger.debug(f"Lifecycle federation report error: {e}")

    # ── Tier awareness ────────────────────────────────────────

    def _detect_tier(self):
        """Cache node capability tier."""
        try:
            from security.system_requirements import get_tier
            self._node_tier = get_tier()
        except Exception:
            self._node_tier = None

    def _is_tier_appropriate(self, model_name: str) -> bool:
        """Check if this model is appropriate for our capability tier."""
        if self._node_tier is None:
            return True
        try:
            from security.system_requirements import NodeTierLevel, _TIER_RANK
            min_tier_str = MODEL_MIN_TIER.get(model_name, 'standard')
            min_tier = NodeTierLevel(min_tier_str)
            return _TIER_RANK[self._node_tier] >= _TIER_RANK[min_tier]
        except Exception:
            return True

    # ── Helpers ───────────────────────────────────────────────

    def _sync_from_rtm(self):
        """Initialize model states from RuntimeToolManager's current state."""
        try:
            from .runtime_manager import runtime_tool_manager, TOOL_CONFIGS
            for tool_name in TOOL_CONFIGS:
                if runtime_tool_manager._is_server_alive(tool_name):
                    self._on_tool_started(tool_name, device='gpu')
        except Exception:
            pass

    def manual_offload(self, model_name: str) -> dict:
        """Manual GPU→CPU offload (admin API)."""
        with self._lock:
            state = self._models.get(model_name)
        if not state:
            return {'error': f'Model {model_name} not tracked'}
        if state.device == ModelDevice.UNLOADED:
            return {'error': f'Model {model_name} is not loaded'}
        if state.device == ModelDevice.CPU:
            return {'message': f'{model_name} already on CPU'}

        success = self._do_offload_to_cpu(model_name)
        return {'success': success, 'model': model_name,
                'device': 'cpu' if success else state.device.value}

    def set_priority(self, model_name: str, priority_str: str) -> dict:
        """Manual priority override (admin API)."""
        try:
            priority = ModelPriority(priority_str)
        except ValueError:
            return {'error': f'Invalid priority: {priority_str}'}

        with self._lock:
            state = self._models.get(model_name)
            if not state:
                return {'error': f'Model {model_name} not tracked'}
            state.priority = priority
        return {'model': model_name, 'priority': priority_str}

    def get_system_pressure(self) -> dict:
        """Return current pressure state for dispatch throttling.

        Called by AgentDaemon to decide whether to reduce concurrency.
        Detects each pressure source once and passes to throttle calculation.
        """
        vram = self._detect_vram_pressure()
        ram = self._detect_ram_pressure()
        cpu = self._detect_cpu_pressure()
        disk = self._detect_disk_pressure()
        return {
            'vram_pressure': vram,
            'ram_pressure': ram,
            'cpu_pressure': cpu,
            'disk_pressure': disk,
            'throttle_factor': self._calculate_throttle_factor(
                cpu_on=cpu, ram_on=ram, vram_on=vram, disk_on=disk),
        }

    def _calculate_throttle_factor(self, *, cpu_on: bool = None,
                                   ram_on: bool = None, vram_on: bool = None,
                                   disk_on: bool = None) -> float:
        """Compute dispatch throttle: 0.0 = fully throttled, 1.0 = no throttling.

        Accepts pre-computed pressure booleans to avoid re-detecting.
        Falls back to live detection if not provided (standalone calls).
        """
        factor = 1.0

        # CPU pressure — use granular percentage for proportional throttling
        try:
            import psutil
            cpu_pct = psutil.cpu_percent(interval=None)
            if cpu_pct >= 95:
                factor *= 0.1
            elif cpu_pct >= 90:
                factor *= 0.3
            elif cpu_pct >= self._cpu_pressure_pct:
                factor *= 0.5
        except ImportError:
            pass

        # RAM pressure
        if ram_on if ram_on is not None else self._detect_ram_pressure():
            factor *= 0.5

        # VRAM pressure
        if vram_on if vram_on is not None else self._detect_vram_pressure():
            factor *= 0.7

        # Disk pressure (critical — stop all heavy work)
        if disk_on if disk_on is not None else self._detect_disk_pressure():
            factor *= 0.2

        return max(0.0, min(1.0, factor))

    def get_status(self) -> dict:
        """Full lifecycle dashboard."""
        with self._lock:
            models = {name: s.to_dict() for name, s in self._models.items()}

        vram_status = {}
        try:
            from .vram_manager import vram_manager
            vram_status = vram_manager.get_status()
        except Exception:
            pass

        # Crash recovery state
        restart_queue = {}
        for name, info in self._restart_pending.items():
            if isinstance(info, dict):
                restart_queue[name] = {
                    'retry_in_s': max(0, round(info.get('retry_after', 0) - time.time(), 1)),
                    'downgrade': info.get('downgrade', False),
                }

        swap_q = [dict(e) for e in self._swap_queue]

        return {
            'running': self._running,
            'tick_count': self._tick_count,
            'interval_s': self._interval,
            'models': models,
            'vram': vram_status,
            'vram_pressure': self._detect_vram_pressure(),
            'ram_pressure': self._detect_ram_pressure(),
            'cpu_pressure': self._detect_cpu_pressure(),
            'disk_pressure': self._detect_disk_pressure(),
            'throttle_factor': self._calculate_throttle_factor(),
            'hive_hints': dict(self._hive_hints),
            'node_tier': (self._node_tier.value
                          if self._node_tier else 'unknown'),
            'restart_pending': restart_queue,
            'swap_queue': swap_q,
        }


# ═══════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════

_lifecycle_manager = None
_lifecycle_lock = threading.Lock()


def get_model_lifecycle_manager() -> ModelLifecycleManager:
    global _lifecycle_manager
    if _lifecycle_manager is None:
        with _lifecycle_lock:
            if _lifecycle_manager is None:
                _lifecycle_manager = ModelLifecycleManager()
    return _lifecycle_manager
