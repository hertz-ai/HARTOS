"""
Resource Governor — HARTOS never slows down the host OS.

Three modes:
  ACTIVE:  User is busy -> HARTOS uses <5% CPU, no GPU, minimal RAM
  IDLE:    User is idle -> HARTOS uses up to 50% CPU, available GPU, moderate RAM
  SLEEP:   System on battery/low resources -> HARTOS suspends all non-essential work

Enforcement layer (ResourceEnforcer):
  Hard OS-level caps so Nunba CANNOT exceed its budget, regardless of code bugs:
  - Windows: Job Object with CPU rate limit + memory limit on the process tree
  - Linux: cgroup v2 cpu.max + memory.max, fallback to RLIMIT_AS
  - macOS: RLIMIT_AS (soft cap) + process priority
  - GPU: CUDA_MPS_ACTIVE_THREAD_PERCENTAGE (NVIDIA MPS) or VRAM budget via lifecycle
  - Process priority: BELOW_NORMAL on Windows, nice +10 on POSIX

  System buffer: always reserves 25% CPU, 2 GB RAM, 1 GB VRAM for the rest of the OS.
  Caps tighten/relax on mode transitions (ACTIVE → tight, IDLE → relaxed, SLEEP → minimal).

Proactive Action Stream:
  When IDLE, periodically samples hive intelligence:
  - Check if any hive tasks match this node's capabilities
  - Pre-download popular models that users nearby are requesting
  - Run background benchmarks to optimize inference settings
  - Explore community signal feed for actionable insights

All sampling is randomized (jitter) to avoid thundering herd across hive nodes.

Integration:
  - Uses VRAMManager for GPU detection (vram_manager.detect_gpu())
  - Reads model_lifecycle get_system_pressure() for existing throttle awareness
  - Emits EventBus events: 'resource.mode_changed', 'resource.proactive_action'
  - AgentDaemon and HiveTaskDispatcher check get_throttle() / should_proceed()
  - Lazy imports for all hive modules (zero startup cost)

Singleton: get_governor() returns the module-level instance.
Convenience: should_proceed(resource) for quick checks from any module.
"""

import ctypes
import logging
import os
import random
import struct
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════

# Mode thresholds
IDLE_THRESHOLD_SECONDS = 120        # 2 min of no input -> idle
ACTIVE_CPU_LIMIT = 0.05             # 5% CPU max when user is active
IDLE_CPU_LIMIT = 0.50               # 50% CPU max when idle
SLEEP_CPU_LIMIT = 0.0               # Nothing when sleeping

# ── System buffer — always reserved for the rest of the OS ──
SYSTEM_BUFFER_CPU_FRACTION = 0.25   # Reserve 25% CPU cores for OS
SYSTEM_BUFFER_RAM_GB = 2.0          # Reserve 2 GB RAM for OS
SYSTEM_BUFFER_VRAM_GB = 1.0         # Reserve 1 GB VRAM for other apps

# Proactive action intervals (seconds) — randomized +/-50%
SIGNAL_CHECK_INTERVAL = 60          # Check hive signals
TASK_CHECK_INTERVAL = 600           # Check pending tasks
MODEL_PREFETCH_INTERVAL = 1800      # Pre-download models
BENCHMARK_INTERVAL = 3600           # Run benchmarks

# Battery thresholds
BATTERY_SLEEP_THRESHOLD = 0.20      # Force sleep below 20%
BATTERY_THROTTLE_THRESHOLD = 0.40   # Reduce activity below 40%

# Monitor loop interval
_MONITOR_INTERVAL_SECONDS = 5

# Mode constants
MODE_ACTIVE = 'active'
MODE_IDLE = 'idle'
MODE_SLEEP = 'sleep'


# ═══════════════════════════════════════════════════════════════════════
# Helpers — platform-agnostic resource detection (psutil optional)
# ═══════════════════════════════════════════════════════════════════════

def _try_import_psutil():
    """Return psutil module or None if unavailable."""
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def _jitter(base_seconds: float, spread: float = 0.5) -> float:
    """Return base_seconds +/- spread*base_seconds (uniform random).

    Prevents thundering herd when many hive nodes use the same interval.
    """
    low = base_seconds * (1.0 - spread)
    high = base_seconds * (1.0 + spread)
    return random.uniform(low, high)


# ═══════════════════════════════════════════════════════════════════════
# ResourceGovernor
# ═══════════════════════════════════════════════════════════════════════

class ResourceGovernor:
    """Central resource controller for HARTOS.

    Monitors CPU, memory, battery, and user activity to transition between
    ACTIVE / IDLE / SLEEP modes.  Exposes a throttle factor that all HARTOS
    subsystems should check before doing heavy work.

    When the user is idle, runs a proactive action stream that samples hive
    intelligence at randomized intervals.
    """

    def __init__(self, idle_threshold_seconds: float = IDLE_THRESHOLD_SECONDS):
        # Mode state
        self._mode: str = MODE_ACTIVE
        self._cpu_limit: float = ACTIVE_CPU_LIMIT
        self._gpu_allowed: bool = False
        self._last_user_activity: float = time.monotonic()
        self._idle_threshold_seconds: float = idle_threshold_seconds

        # Threading
        self._proactive_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()  # instant wake/cancel for proactive

        # Prime psutil CPU counter (first call always returns 0.0)
        try:
            import psutil
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

        # Stats (exposed for dashboards)
        self._stats: dict = {
            'mode_changes': 0,
            'proactive_actions': 0,
            'signals_checked': 0,
            'tasks_dispatched': 0,
            'models_prefetched': 0,
            'benchmarks_run': 0,
            'last_mode_change': 0.0,
            'uptime_start': 0.0,
        }

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Start the governor background monitor and proactive stream."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._cancel_event.clear()
            self._stats['uptime_start'] = time.time()

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name='ResourceGovernor-Monitor',
            daemon=True,
        )
        self._monitor_thread.start()

        self._proactive_thread = threading.Thread(
            target=self._proactive_action_stream,
            name='ResourceGovernor-Proactive',
            daemon=True,
        )
        self._proactive_thread.start()

        logger.info("ResourceGovernor started (idle threshold=%.0fs)",
                     self._idle_threshold_seconds)

    def stop(self) -> None:
        """Stop the governor, release all throttles."""
        with self._lock:
            if not self._running:
                return
            self._running = False

        self._cancel_event.set()

        # Wait for threads to exit (bounded timeout)
        for t in (self._monitor_thread, self._proactive_thread):
            if t is not None and t.is_alive():
                t.join(timeout=3.0)

        self._monitor_thread = None
        self._proactive_thread = None
        logger.info("ResourceGovernor stopped")

    # ── Public API ────────────────────────────────────────────────

    def get_mode(self) -> str:
        """Current mode: 'active', 'idle', or 'sleep'."""
        return self._mode

    def get_throttle(self) -> float:
        """Current throttle factor 0.0 (full stop) to 1.0 (unlimited).

        Other HARTOS subsystems should multiply their resource usage by
        this value before proceeding with heavy work.
        """
        return self._calculate_throttle()

    def should_allow(self, resource: str) -> bool:
        """Quick check: should this resource usage be allowed right now?

        Args:
            resource: One of 'cpu_heavy', 'gpu', 'network_heavy', 'disk_heavy'

        Returns:
            True if the governor permits the resource usage.
        """
        mode = self._mode
        if mode == MODE_SLEEP:
            return False

        if mode == MODE_ACTIVE:
            # Active mode: only permit lightweight work
            if resource in ('gpu', 'cpu_heavy', 'disk_heavy'):
                return False
            if resource == 'network_heavy':
                return False
            return True

        # IDLE mode: allow most things
        if resource == 'gpu':
            return self._gpu_allowed
        return True

    def report_user_activity(self) -> None:
        """Signal that the user is active.  Immediately switches to ACTIVE mode.

        Called by UI/input handlers, Flask endpoints, or any user-facing code.
        """
        self._last_user_activity = time.monotonic()
        if self._mode != MODE_ACTIVE:
            self._transition_to(MODE_ACTIVE)

    def get_stats(self) -> dict:
        """Return a copy of governor statistics for dashboards."""
        with self._lock:
            stats = dict(self._stats)
        stats['mode'] = self._mode
        stats['throttle'] = self._calculate_throttle()
        stats['cpu_limit'] = self._cpu_limit
        stats['gpu_allowed'] = self._gpu_allowed
        return stats

    # ── Mode Transitions ──────────────────────────────────────────

    def _transition_to(self, new_mode: str) -> None:
        """Switch modes, update limits, emit event."""
        old_mode = self._mode

        if new_mode == old_mode:
            return

        self._mode = new_mode

        if new_mode == MODE_ACTIVE:
            self._cpu_limit = ACTIVE_CPU_LIMIT
            self._gpu_allowed = False
            # Wake the cancel event so proactive stream backs off instantly
            self._cancel_event.set()
        elif new_mode == MODE_IDLE:
            self._cpu_limit = IDLE_CPU_LIMIT
            self._gpu_allowed = True
            # Clear cancel so proactive stream can run
            self._cancel_event.clear()
        elif new_mode == MODE_SLEEP:
            self._cpu_limit = SLEEP_CPU_LIMIT
            self._gpu_allowed = False
            self._cancel_event.set()

        with self._lock:
            self._stats['mode_changes'] += 1
            self._stats['last_mode_change'] = time.time()

        logger.info("ResourceGovernor: %s -> %s (cpu_limit=%.2f, gpu=%s)",
                     old_mode, new_mode, self._cpu_limit, self._gpu_allowed)

        # Emit EventBus event (best-effort, lazy import)
        try:
            from core.platform.events import emit_event
            emit_event('resource.mode_changed', {
                'old_mode': old_mode,
                'new_mode': new_mode,
                'cpu_limit': self._cpu_limit,
                'gpu_allowed': self._gpu_allowed,
                'timestamp': time.time(),
            })
        except Exception:
            pass

    # ── Monitor Loop ──────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        """Background thread: monitor system state every 5 seconds.

        Three cheap checks per tick: CPU, user idle, battery.
        Transitions modes based on combined signals.
        """
        while self._running:
            try:
                cpu = self._get_cpu_usage()
                mem = self._get_memory_pressure()
                user_idle = self._detect_user_idle()
                battery_level, on_battery = self._get_battery_status()

                # Decision tree
                if on_battery and battery_level < BATTERY_SLEEP_THRESHOLD:
                    # Critical battery: force sleep
                    self._transition_to(MODE_SLEEP)
                elif not user_idle:
                    # User is active
                    self._transition_to(MODE_ACTIVE)
                elif cpu > 0.85 or mem > 0.90:
                    # System is heavily loaded even though user is idle
                    # (e.g., background renders, compiles) — stay conservative
                    self._transition_to(MODE_ACTIVE)
                elif on_battery and battery_level < BATTERY_THROTTLE_THRESHOLD:
                    # Low battery: allow idle work but keep it light
                    self._transition_to(MODE_IDLE)
                    self._cpu_limit = IDLE_CPU_LIMIT * 0.5  # half the idle budget
                else:
                    # User idle, system not overloaded, power is fine
                    self._transition_to(MODE_IDLE)

                # Update GPU allowance based on VRAM availability
                if self._mode == MODE_IDLE:
                    self._gpu_allowed = self._check_gpu_available()

            except Exception as e:
                logger.debug("ResourceGovernor monitor error: %s", e)

            # Sleep for the monitor interval, but wake early if stopping
            self._cancel_event.wait(timeout=_MONITOR_INTERVAL_SECONDS)
            if not self._running:
                break
            # If cancel_event was set by mode transition, clear it for proactive
            # (only if we're not in a mode that should keep it set)
            if self._mode == MODE_IDLE:
                self._cancel_event.clear()

    # ── Platform-Specific Detection ───────────────────────────────

    def _detect_user_idle(self) -> bool:
        """Detect whether the user is idle (no input for threshold seconds).

        Platform-specific:
          Windows: GetLastInputInfo via ctypes
          Linux: /proc/interrupts delta or fallback
          macOS: ioreg idle time
          Fallback: report_user_activity() timestamp
        """
        idle_ms = self._get_os_idle_ms()
        if idle_ms is not None:
            return idle_ms >= (self._idle_threshold_seconds * 1000)

        # Fallback: use last reported user activity
        elapsed = time.monotonic() - self._last_user_activity
        return elapsed >= self._idle_threshold_seconds

    def _get_os_idle_ms(self) -> Optional[float]:
        """Get OS-level user idle time in milliseconds, or None if unavailable."""
        if sys.platform == 'win32':
            return self._get_idle_ms_windows()
        elif sys.platform == 'linux':
            return self._get_idle_ms_linux()
        elif sys.platform == 'darwin':
            return self._get_idle_ms_macos()
        return None

    def _get_idle_ms_windows(self) -> Optional[float]:
        """Windows: GetLastInputInfo returns tick count of last input event."""
        try:
            # LASTINPUTINFO struct: cbSize (UINT), dwTime (DWORD)
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [
                    ('cbSize', ctypes.c_uint),
                    ('dwTime', ctypes.c_uint),
                ]

            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                current_tick = ctypes.windll.kernel32.GetTickCount()
                idle_ms = current_tick - lii.dwTime
                # Handle tick count wraparound (every ~49.7 days)
                if idle_ms < 0:
                    idle_ms += 0xFFFFFFFF + 1
                return float(idle_ms)
        except Exception:
            pass
        return None

    def _get_idle_ms_linux(self) -> Optional[float]:
        """Linux: try xprintidle, then /proc/interrupts delta estimation."""
        # Try xprintidle first (X11 desktops)
        try:
            import subprocess
            result = subprocess.run(
                ['xprintidle'], capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                return float(result.stdout.strip())
        except Exception:
            pass
        return None

    def _get_idle_ms_macos(self) -> Optional[float]:
        """macOS: ioreg HIDIdleTime (nanoseconds -> milliseconds)."""
        try:
            import subprocess
            result = subprocess.run(
                ['ioreg', '-c', 'IOHIDSystem'],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if 'HIDIdleTime' in line:
                        # Line format: "HIDIdleTime" = 1234567890
                        parts = line.split('=')
                        if len(parts) >= 2:
                            ns = int(parts[-1].strip())
                            return ns / 1_000_000.0  # ns -> ms
        except Exception:
            pass
        return None

    # ── CPU / Memory / Battery / GPU ──────────────────────────────

    def _get_cpu_usage(self) -> float:
        """Get current CPU usage as a float 0.0 to 1.0.

        Tries psutil, then platform fallbacks.
        """
        psutil = _try_import_psutil()
        if psutil is not None:
            try:
                return psutil.cpu_percent(interval=None) / 100.0
            except Exception:
                pass

        # Linux fallback: os.getloadavg()
        if hasattr(os, 'getloadavg'):
            try:
                load_1min = os.getloadavg()[0]
                cpu_count = os.cpu_count() or 1
                return min(1.0, load_1min / cpu_count)
            except Exception:
                pass

        # Windows fallback without psutil: assume moderate usage
        return 0.3

    def _get_memory_pressure(self) -> float:
        """Get memory pressure as a float 0.0 to 1.0.

        Tries psutil, then /proc/meminfo on Linux.
        """
        psutil = _try_import_psutil()
        if psutil is not None:
            try:
                return psutil.virtual_memory().percent / 100.0
            except Exception:
                pass

        # Linux fallback: /proc/meminfo
        if sys.platform == 'linux':
            try:
                with open('/proc/meminfo', 'r') as f:
                    mem_info = {}
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 2:
                            key = parts[0].rstrip(':')
                            mem_info[key] = int(parts[1])
                total = mem_info.get('MemTotal', 1)
                available = mem_info.get('MemAvailable',
                                         mem_info.get('MemFree', total))
                return 1.0 - (available / total)
            except Exception:
                pass

        # Fallback: assume moderate
        return 0.4

    def _get_battery_status(self) -> tuple:
        """Return (battery_level: float 0-1, on_battery: bool).

        Returns (1.0, False) if no battery detected (desktop).
        """
        psutil = _try_import_psutil()
        if psutil is not None:
            try:
                battery = psutil.sensors_battery()
                if battery is not None:
                    return (battery.percent / 100.0, not battery.power_plugged)
            except Exception:
                pass

        # Windows fallback: GetSystemPowerStatus via ctypes
        if sys.platform == 'win32':
            try:
                class SYSTEM_POWER_STATUS(ctypes.Structure):
                    _fields_ = [
                        ('ACLineStatus', ctypes.c_byte),
                        ('BatteryFlag', ctypes.c_byte),
                        ('BatteryLifePercent', ctypes.c_byte),
                        ('SystemStatusFlag', ctypes.c_byte),
                        ('BatteryLifeTime', ctypes.c_ulong),
                        ('BatteryFullLifeTime', ctypes.c_ulong),
                    ]

                status = SYSTEM_POWER_STATUS()
                if ctypes.windll.kernel32.GetSystemPowerStatus(
                        ctypes.byref(status)):
                    pct = status.BatteryLifePercent
                    if pct == 255:
                        # Unknown / no battery
                        return (1.0, False)
                    on_battery = (status.ACLineStatus == 0)
                    return (pct / 100.0, on_battery)
            except Exception:
                pass

        # Linux fallback: /sys/class/power_supply
        if sys.platform == 'linux':
            try:
                base = '/sys/class/power_supply'
                for entry in os.listdir(base):
                    supply_path = os.path.join(base, entry)
                    type_path = os.path.join(supply_path, 'type')
                    if os.path.exists(type_path):
                        with open(type_path) as f:
                            if f.read().strip() != 'Battery':
                                continue
                        cap_path = os.path.join(supply_path, 'capacity')
                        status_path = os.path.join(supply_path, 'status')
                        if os.path.exists(cap_path):
                            with open(cap_path) as f:
                                pct = int(f.read().strip())
                            on_battery = True
                            if os.path.exists(status_path):
                                with open(status_path) as f:
                                    on_battery = f.read().strip() == 'Discharging'
                            return (pct / 100.0, on_battery)
            except Exception:
                pass

        # No battery detected (desktop)
        return (1.0, False)

    def _check_gpu_available(self) -> bool:
        """Check if GPU has usable free VRAM for hive work."""
        try:
            from integrations.service_tools.vram_manager import vram_manager
            info = vram_manager.detect_gpu()
            if not info.get('cuda_available'):
                return False
            free_gb = info.get('free_gb', 0.0)
            # Need at least 1 GB free to be useful for hive tasks
            return free_gb >= 1.0
        except Exception:
            return False

    # ── Throttle Calculation ──────────────────────────────────────

    def _calculate_throttle(self) -> float:
        """Combine all signals into a single throttle factor 0.0 - 1.0.

        ACTIVE mode:  0.05 — bare minimum for event processing
        IDLE + low CPU: 1.0 — full speed
        IDLE + moderate CPU: 0.5
        SLEEP: 0.0 — suspend everything
        Battery < 20%: force 0.0
        """
        mode = self._mode

        if mode == MODE_SLEEP:
            return 0.0

        if mode == MODE_ACTIVE:
            return ACTIVE_CPU_LIMIT  # 0.05

        # IDLE mode — scale based on current resource usage
        cpu = self._get_cpu_usage()
        mem = self._get_memory_pressure()

        throttle = 1.0

        if cpu > 0.80:
            throttle *= 0.2
        elif cpu > 0.60:
            throttle *= 0.5
        elif cpu > 0.40:
            throttle *= 0.8

        if mem > 0.90:
            throttle *= 0.3
        elif mem > 0.80:
            throttle *= 0.6

        # Battery factor
        battery_level, on_battery = self._get_battery_status()
        if on_battery:
            if battery_level < BATTERY_SLEEP_THRESHOLD:
                return 0.0
            elif battery_level < BATTERY_THROTTLE_THRESHOLD:
                throttle *= 0.5

        return max(0.0, min(1.0, throttle))

    # ── Proactive Action Stream ───────────────────────────────────

    def _proactive_action_stream(self) -> None:
        """Run during IDLE mode.  Randomized sampling of hive intelligence.

        Timers:
          - 30-120s: check hive signal feed for actionable items
          - 5-15min: check if pending hive tasks match this node
          - 30-60min: pre-download trending models if VRAM available
          - ~1h: run quick benchmark on active model

        All timers use random jitter to prevent thundering herd.
        Immediately stops when mode switches to ACTIVE (via _cancel_event).
        """
        # Initialize next-action timestamps with jitter from now
        now = time.monotonic()
        next_signal_check = now + _jitter(SIGNAL_CHECK_INTERVAL)
        next_task_check = now + _jitter(TASK_CHECK_INTERVAL)
        next_model_prefetch = now + _jitter(MODEL_PREFETCH_INTERVAL)
        next_benchmark = now + _jitter(BENCHMARK_INTERVAL)

        while self._running:
            # Sleep in short increments, checking cancel event
            # Wait returns True if the event is set (cancel requested)
            cancelled = self._cancel_event.wait(timeout=5.0)

            if not self._running:
                break

            # Only do proactive work in IDLE mode
            if self._mode != MODE_IDLE:
                # Reset timers when re-entering idle (with fresh jitter)
                now = time.monotonic()
                next_signal_check = now + _jitter(SIGNAL_CHECK_INTERVAL)
                next_task_check = now + _jitter(TASK_CHECK_INTERVAL)
                next_model_prefetch = now + _jitter(MODEL_PREFETCH_INTERVAL)
                next_benchmark = now + _jitter(BENCHMARK_INTERVAL)
                continue

            now = time.monotonic()

            # Check hive signal feed
            if now >= next_signal_check:
                self._proactive_check_signals()
                next_signal_check = now + _jitter(SIGNAL_CHECK_INTERVAL)

            # Check pending hive tasks
            if now >= next_task_check:
                self._proactive_check_tasks()
                next_task_check = now + _jitter(TASK_CHECK_INTERVAL)

            # Pre-download trending models
            if now >= next_model_prefetch:
                self._proactive_prefetch_models()
                next_model_prefetch = now + _jitter(MODEL_PREFETCH_INTERVAL)

            # Run benchmark
            if now >= next_benchmark:
                self._proactive_run_benchmark()
                next_benchmark = now + _jitter(BENCHMARK_INTERVAL)

    def _proactive_check_signals(self) -> None:
        """Check hive signal feed for actionable insights."""
        if self._mode != MODE_IDLE:
            return
        try:
            from integrations.channels.hive_signal_bridge import get_signal_bridge
            bridge = get_signal_bridge()
            signals = bridge.get_signal_feed(limit=10)
            with self._lock:
                self._stats['signals_checked'] += 1
                self._stats['proactive_actions'] += 1
            if signals:
                logger.debug("ResourceGovernor: checked %d hive signals",
                             len(signals))
                self._emit_proactive_event('signal_check', {
                    'signal_count': len(signals),
                })
        except Exception as e:
            logger.debug("ResourceGovernor: signal check failed: %s", e)

    def _proactive_check_tasks(self) -> None:
        """Check if any pending hive tasks match this node's capabilities."""
        if self._mode != MODE_IDLE:
            return
        try:
            from integrations.coding_agent.hive_task_protocol import (
                get_dispatcher,
            )
            dispatcher = get_dispatcher()
            dispatched = dispatcher.dispatch_pending()
            with self._lock:
                self._stats['tasks_dispatched'] += dispatched
                self._stats['proactive_actions'] += 1
            if dispatched:
                logger.info("ResourceGovernor: dispatched %d hive tasks "
                            "during idle", dispatched)
                self._emit_proactive_event('task_dispatch', {
                    'dispatched': dispatched,
                })
        except Exception as e:
            logger.debug("ResourceGovernor: task check failed: %s", e)

    def _proactive_prefetch_models(self) -> None:
        """Pre-download trending models if GPU VRAM is available."""
        if self._mode != MODE_IDLE or not self._gpu_allowed:
            return
        try:
            # Check with model lifecycle for prefetch suggestions
            from integrations.service_tools.model_lifecycle import (
                get_model_lifecycle_manager,
            )
            mgr = get_model_lifecycle_manager()
            pressure = mgr.get_system_pressure()
            if pressure.get('throttle_factor', 0) < 0.3:
                # System too busy for prefetch
                return
            with self._lock:
                self._stats['models_prefetched'] += 1
                self._stats['proactive_actions'] += 1
            logger.debug("ResourceGovernor: model prefetch check complete")
            self._emit_proactive_event('model_prefetch', {
                'throttle_factor': pressure.get('throttle_factor', 0),
            })
        except Exception as e:
            logger.debug("ResourceGovernor: model prefetch failed: %s", e)

    def _proactive_run_benchmark(self) -> None:
        """Run efficiency benchmarks on cloud providers during idle time.

        Uses the EfficiencyMatrix to benchmark all configured API providers,
        building a continuous picture of speed/quality/cost for optimal routing.
        Also checks local model performance via the model_lifecycle manager.
        """
        if self._mode != MODE_IDLE:
            return

        # Check system pressure — don't benchmark if system is busy
        try:
            from integrations.service_tools.model_lifecycle import (
                get_model_lifecycle_manager,
            )
            mgr = get_model_lifecycle_manager()
            pressure = mgr.get_system_pressure()
            if pressure.get('throttle_factor', 0) < 0.5:
                return
        except Exception:
            pass

        # Run provider efficiency benchmarks
        try:
            from integrations.providers.efficiency_matrix import get_matrix
            matrix = get_matrix()
            matrix.run_benchmark(model_type='llm')
            with self._lock:
                self._stats['benchmarks_run'] += 1
                self._stats['proactive_actions'] += 1
            logger.info("ResourceGovernor: provider benchmarks complete")
            self._emit_proactive_event('benchmark', {
                'summary': matrix.get_matrix_summary(),
            })
        except Exception as e:
            logger.debug("ResourceGovernor: benchmark failed: %s", e)

    def _emit_proactive_event(self, action: str, data: dict) -> None:
        """Emit a 'resource.proactive_action' event (best-effort)."""
        try:
            from core.platform.events import emit_event
            emit_event('resource.proactive_action', {
                'action': action,
                'mode': self._mode,
                'timestamp': time.time(),
                **data,
            })
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════

_governor: Optional[ResourceGovernor] = None
_governor_lock = threading.Lock()


def get_governor() -> ResourceGovernor:
    """Get or create the singleton ResourceGovernor."""
    global _governor
    if _governor is None:
        with _governor_lock:
            if _governor is None:
                _governor = ResourceGovernor()
    return _governor


def should_proceed(resource: str = 'cpu_heavy') -> bool:
    """Module-level convenience: should HARTOS proceed with this resource usage?

    Returns True if the governor allows the requested resource, or if the
    governor has not been started (no throttling by default).

    Args:
        resource: One of 'cpu_heavy', 'gpu', 'network_heavy', 'disk_heavy'

    Usage:
        from core.resource_governor import should_proceed

        if should_proceed('gpu'):
            run_inference()
    """
    gov = _governor
    if gov is None or not gov._running:
        return True
    return gov.should_allow(resource)
