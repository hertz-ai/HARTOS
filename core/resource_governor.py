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
from core.subprocess_safe import no_window_kwargs
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
# CPU budget for HARTOS's OWN orchestration (NOT the model — llama-server is
# always pinned to every core; see ResourceEnforcer._unrestrict_llm_affinity).
# Single source: the advisory throttle AND the hard affinity cap both read these.
# Env-tunable so the active/idle split can be adjusted without a code change.
ACTIVE_CPU_LIMIT = float(os.environ.get('HEVOLVE_ACTIVE_CPU_LIMIT', '0.50'))  # at keyboard: usable
IDLE_CPU_LIMIT = float(os.environ.get('HEVOLVE_IDLE_CPU_LIMIT', '0.80'))      # away: agents progress
SLEEP_CPU_LIMIT = 0.0               # battery-critical: full stop (unchanged)

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

# ── Load-based backoff thresholds ──
# When the user is idle we still back off to ACTIVE if ANOTHER app is
# loading the box (a render, a compile, a game) — but NOT for HARTOS's
# own idle-compute work.  The decision uses EXTERNAL cpu (total minus
# HARTOS's own process tree), so the flywheel/LLM raising CPU does not
# make the governor throttle the very work it exists to run.
#
# Why this exists (2026-05-30 self-defeat loop): the monitor read TOTAL
# system CPU, so when the user stepped away and the daemon's own LLM
# work pushed CPU past LOAD_BACKOFF_CPU, the governor flipped to ACTIVE
# (throttle 0.05) and should_yield_to_user() blocked every tick — the
# flywheel halted the moment it started.  Measuring external CPU keeps
# the "be polite to other apps" intent while letting an idle-compute
# node actually contribute the spare cycles it was opted in to give.
LOAD_BACKOFF_CPU = 0.85
LOAD_BACKOFF_MEM = 0.90


def _find_llm_pids() -> set:
    """PIDs listening on the local LLM-server port(s) (llama-server).  SINGLE
    source used by BOTH the governor's own-CPU attribution
    (``_resolve_llm_server_pid``) and the enforcer's affinity exemption
    (``_unrestrict_llm_affinity``), so they never drift on which process is
    'the model'.  Empty set when psutil is unavailable or nothing is listening.
    """
    psutil = _try_import_psutil()
    if psutil is None:
        return set()
    ports = set()
    try:
        from core.port_registry import get_port
        ports.add(int(get_port('llm')))
    except Exception:
        pass
    try:
        from core.port_registry import get_local_llm_url
        import re
        m = re.search(r':(\d+)', get_local_llm_url() or '')
        if m:
            ports.add(int(m.group(1)))
    except Exception:
        pass
    ports.update((8082, 8080))  # common llama-server defaults
    pids = set()
    try:
        for c in psutil.net_connections(kind='inet'):
            if (c.status == 'LISTEN' and c.laddr
                    and c.laddr.port in ports and c.pid):
                pids.add(c.pid)
    except Exception:
        pass
    return pids


# ═══════════════════════════════════════════════════════════════════════
# ResourceEnforcer — hard OS-level process caps
# ═══════════════════════════════════════════════════════════════════════

class ResourceEnforcer:
    """Enforce hard resource caps at the OS level.

    The governor ADVISES. The enforcer CONSTRAINS.
    Even buggy code cannot exceed the caps set here.

    Platform-specific mechanisms:
      Windows: Job Object (CPU rate limit + memory limit on process tree)
      Linux:   cgroup v2 (cpu.max + memory.max), fallback RLIMIT_AS
      macOS:   RLIMIT_AS (soft cap) + process priority

    Process priority: BELOW_NORMAL on Windows, nice +10 on POSIX.
    This alone prevents Nunba from competing with foreground apps.
    """

    def __init__(self):
        self._enforced = False
        self._job_handle = None   # Windows Job Object handle
        self._cgroup_path = None  # Linux cgroup path
        self._original_nice = None

    def enforce(self, cpu_fraction: float = 0.75, ram_fraction: float = 0.75,
                gpu_fraction: float = 0.75):
        """Apply hard caps. Call once at startup.

        Args:
            cpu_fraction: Max fraction of total CPU Nunba can use (0.75 = 75%)
            ram_fraction: Max fraction of total RAM Nunba can use
            gpu_fraction: Max fraction of GPU Nunba can use (advisory for CUDA)
        """
        if self._enforced:
            return

        # Always subtract system buffer from the requested fraction
        total_ram_gb = self._get_total_ram_gb()
        usable_ram_gb = max(0.5, total_ram_gb * ram_fraction - SYSTEM_BUFFER_RAM_GB)

        total_cores = os.cpu_count() or 4
        usable_cores = max(1, int(total_cores * (cpu_fraction - SYSTEM_BUFFER_CPU_FRACTION)))

        logger.info("ResourceEnforcer: total_ram=%.1fGB, cap=%.1fGB, "
                     "total_cores=%d, cap_cores=%d",
                     total_ram_gb, usable_ram_gb, total_cores, usable_cores)

        self._set_process_priority()
        self._enforce_cpu(cpu_fraction, usable_cores, total_cores)
        self._enforce_ram(usable_ram_gb)
        self._enforce_gpu(gpu_fraction)
        self._enforced = True
        logger.info("ResourceEnforcer: hard caps applied")

    def update_caps(self, mode: str):
        """Tighten or relax caps based on governor mode.

        ACTIVE: usable (50% CPU, 60% RAM, GPU visible) — Nunba stays responsive
                while you use it; the model is exempt from the CPU cap entirely.
        IDLE:   relaxed (80% CPU, 80% RAM, GPU) — agents make progress.
        SLEEP:  minimal (5% CPU, 25% RAM, no GPU) — battery-critical only.
        """
        if not self._enforced:
            return

        # CPU comes from the single-source limits above; the model is exempt
        # (affinity), so these only bound OUR orchestration.  GPU stays VISIBLE
        # when active — the model is the foreground work the user waits on, not
        # a background hog to hide the GPU from (gpu_frac=0 → CUDA_VISIBLE_DEVICES='').
        caps = {
            #            cpu,              ram,  gpu
            MODE_ACTIVE: (ACTIVE_CPU_LIMIT, 0.60, 0.75),
            MODE_IDLE:   (IDLE_CPU_LIMIT,   0.80, 0.90),
            MODE_SLEEP:  (0.05,             0.25, 0.0),
        }
        cpu_frac, ram_frac, gpu_frac = caps.get(mode, (IDLE_CPU_LIMIT, 0.50, 0.0))

        total_ram_gb = self._get_total_ram_gb()
        usable_ram_gb = max(0.5, total_ram_gb * ram_frac - SYSTEM_BUFFER_RAM_GB)
        total_cores = os.cpu_count() or 4

        self._enforce_cpu(cpu_frac, max(1, int(total_cores * cpu_frac)), total_cores)
        self._enforce_ram(usable_ram_gb)
        self._enforce_gpu(gpu_frac)

    # ── Process priority ──────────────────────────────────────────────

    def _set_process_priority(self):
        """Set process to below-normal priority so foreground apps always win."""
        try:
            if sys.platform == 'win32':
                # BELOW_NORMAL_PRIORITY_CLASS = 0x4000
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.GetCurrentProcess()
                kernel32.SetPriorityClass(handle, 0x4000)
                logger.info("ResourceEnforcer: set BELOW_NORMAL priority (Windows)")
            else:
                self._original_nice = os.nice(0)
                os.nice(10)  # Lower priority (higher nice = lower priority)
                logger.info("ResourceEnforcer: set nice +10 (POSIX)")
        except Exception as e:
            logger.debug("ResourceEnforcer: priority set failed: %s", e)

    # ── CPU enforcement ───────────────────────────────────────────────

    def _enforce_cpu(self, cpu_fraction: float, usable_cores: int,
                     total_cores: int):
        """Limit HARTOS's OWN orchestration to ``usable_cores`` via CPU affinity
        + BELOW_NORMAL priority — deliberately NOT a job-wide hard rate cap.

        A Job Object CPU-rate cap throttles the WHOLE job, and llama-server is a
        CHILD of this process (it inherits our Job Object), so a rate cap would
        strangle the model — the foreground work the user is waiting on.
        Per-process affinity bounds only our threads; the model is then pinned
        back to EVERY core (_unrestrict_llm_affinity).  The Windows Job Object is
        still used for the MEMORY ceiling, created ONCE and reused (not recreated
        per mode flip, which on Windows accumulates to the most-restrictive cap).
        """
        if sys.platform == 'win32':
            self._ensure_job_windows()            # memory ceiling only; no CPU rate
        elif sys.platform == 'linux':
            self._enforce_cpu_linux(cpu_fraction, total_cores)
        # macOS: rely on BELOW_NORMAL nice + affinity below.

        psutil = _try_import_psutil()
        if psutil is not None:
            try:
                p = psutil.Process()
                available = list(range(total_cores))
                # Use last N cores (leave the first cores for OS/foreground apps).
                target = available[-usable_cores:] if usable_cores < total_cores else available
                p.cpu_affinity(target)
                logger.info("ResourceEnforcer: CPU affinity = %d/%d cores %s",
                            usable_cores, total_cores, target)
            except Exception as e:
                logger.debug("ResourceEnforcer: CPU affinity failed: %s", e)
        # The model is never core-restricted — pin it back to every core.
        self._unrestrict_llm_affinity(total_cores)

    def _ensure_job_windows(self):
        """Create the process Job Object ONCE (idempotent) and assign this
        process to it.  Used for the MEMORY ceiling only — there is deliberately
        NO CPU-rate control, because the rate cap is job-wide and llama-server is
        a child that would inherit it (see _enforce_cpu).  Reused across mode
        changes; _enforce_ram_windows updates the memory limit on this same
        handle.  Creating a fresh Job Object per mode flip (the old behaviour)
        accumulated nested jobs on Windows, pinning the process at the
        most-restrictive cap so 'relax to idle' silently never took effect.
        """
        if self._job_handle:
            return
        try:
            kernel32 = ctypes.windll.kernel32
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                logger.debug("ResourceEnforcer: CreateJobObject failed")
                return
            kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
            self._job_handle = job
            logger.info("ResourceEnforcer: Job Object ready "
                        "(memory ceiling; CPU governed by affinity, not a rate cap)")
        except Exception as e:
            logger.debug("ResourceEnforcer: Job Object create failed: %s", e)

    def _unrestrict_llm_affinity(self, total_cores: int):
        """Pin llama-server to EVERY core.  The model is a child of this process
        and would otherwise inherit the restricted affinity set we apply to our
        own orchestration — but the model must never be core-starved; it is the
        user's foreground request running on the GPU.  Best-effort; runs only on
        a mode change."""
        psutil = _try_import_psutil()
        if psutil is None:
            return
        all_cores = list(range(total_cores))
        for pid in _find_llm_pids():
            try:
                psutil.Process(pid).cpu_affinity(all_cores)
            except Exception:
                pass

    def _enforce_cpu_linux(self, cpu_fraction: float, total_cores: int):
        """Linux: use cgroup v2 cpu.max."""
        try:
            # Try cgroup v2
            cg_path = f'/sys/fs/cgroup/nunba_{os.getpid()}'
            if os.path.isdir('/sys/fs/cgroup/cgroup.controllers'):
                os.makedirs(cg_path, exist_ok=True)
                period = 100000  # 100ms
                quota = int(period * cpu_fraction * total_cores)
                with open(os.path.join(cg_path, 'cpu.max'), 'w') as f:
                    f.write(f'{quota} {period}')
                # Move current process into the cgroup
                with open(os.path.join(cg_path, 'cgroup.procs'), 'w') as f:
                    f.write(str(os.getpid()))
                self._cgroup_path = cg_path
                logger.info("ResourceEnforcer: cgroup v2 cpu.max = %d/%d (%.0f%%)",
                             quota, period, cpu_fraction * 100)
        except PermissionError:
            logger.debug("ResourceEnforcer: cgroup v2 requires root, skipping")
        except Exception as e:
            logger.debug("ResourceEnforcer: cgroup cpu failed: %s", e)

    # ── RAM enforcement ───────────────────────────────────────────────

    def _enforce_ram(self, max_ram_gb: float):
        """Enforce RAM cap via Job Object (Windows), cgroup (Linux), or RLIMIT."""
        max_bytes = int(max_ram_gb * 1024 * 1024 * 1024)

        if sys.platform == 'win32':
            self._enforce_ram_windows(max_bytes)
        elif sys.platform == 'linux':
            self._enforce_ram_linux(max_bytes)
        else:
            self._enforce_ram_rlimit(max_bytes)

    def _enforce_ram_windows(self, max_bytes: int):
        """Windows: Job Object memory limit."""
        if not self._job_handle:
            return
        try:
            kernel32 = ctypes.windll.kernel32

            # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [('_' + str(i), ctypes.c_ulonglong) for i in range(6)]

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ('PerProcessUserTimeLimit', ctypes.c_longlong),
                    ('PerJobUserTimeLimit', ctypes.c_longlong),
                    ('LimitFlags', ctypes.c_ulong),
                    ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', ctypes.c_ulong),
                    ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', ctypes.c_ulong),
                    ('SchedulingClass', ctypes.c_ulong),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ('IoInfo', IO_COUNTERS),
                    ('ProcessMemoryLimit', ctypes.c_size_t),
                    ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t),
                    ('PeakJobMemoryUsed', ctypes.c_size_t),
                ]

            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            # JOB_OBJECT_LIMIT_JOB_MEMORY = 0x200
            info.BasicLimitInformation.LimitFlags = 0x200
            info.JobMemoryLimit = max_bytes

            # SetInformationJobObject with JobObjectExtendedLimitInformation (9)
            kernel32.SetInformationJobObject(
                self._job_handle, 9, ctypes.byref(info), ctypes.sizeof(info))

            logger.info("ResourceEnforcer: Windows Job memory limit = %.1f GB",
                         max_bytes / (1024**3))
        except Exception as e:
            logger.debug("ResourceEnforcer: Windows memory limit failed: %s", e)

    def _enforce_ram_linux(self, max_bytes: int):
        """Linux: cgroup v2 memory.max — and NOTHING else on failure.

        The RLIMIT_AS fallback that used to live here crash-looped hart-backend
        in every nixosTest VM (2026-07-28, task #15): inside the systemd unit
        the hart user has no cgroup delegation, so the memory.max write fails
        and the fallback set RLIMIT_AS = 0.75 x total RAM — an ADDRESS-SPACE
        cap, which on a 2 GB VM is ~1.5 GB of virtual space. A fully imported
        CPython exhausts that before RSS reaches 110 MB (shared-library maps,
        arenas, guard pages all count), so the next mmap — the 8 MB thread
        stack at hart_intelligence_entry.py:1909 — failed EAGAIN and surfaced
        as "can't start new thread", deterministically, five restarts to
        start-limit. Address space is not RAM; that mismatch is exactly why
        cgroup memory.max exists.

        Not enforcing is CORRECT here, not a gap: on HART OS the systemd unit
        already owns memory enforcement (hart-backend.nix MemoryMax/MemoryHigh
        at the cgroup level). An in-process second enforcer is a parallel path
        that can only agree (redundant) or disagree (this incident). macOS,
        which has no systemd, keeps its direct _enforce_ram_rlimit dispatch arm
        in _enforce_ram — only this Linux fallback is removed.
        """
        if self._cgroup_path:
            try:
                with open(os.path.join(self._cgroup_path, 'memory.max'), 'w') as f:
                    f.write(str(max_bytes))
                logger.info("ResourceEnforcer: cgroup memory.max = %.1f GB",
                             max_bytes / (1024**3))
                return
            except Exception as e:
                logger.debug("ResourceEnforcer: cgroup memory failed: %s", e)
        logger.info(
            "ResourceEnforcer: no writable cgroup — leaving memory enforcement "
            "to the service manager (systemd MemoryMax owns it on HART OS)")

    def _enforce_ram_rlimit(self, max_bytes: int):
        """POSIX: RLIMIT_AS (soft cap — process gets MemoryError on exceed)."""
        try:
            import resource
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            resource.setrlimit(resource.RLIMIT_AS, (max_bytes, hard))
            logger.info("ResourceEnforcer: RLIMIT_AS = %.1f GB",
                         max_bytes / (1024**3))
        except Exception as e:
            logger.debug("ResourceEnforcer: RLIMIT_AS failed: %s", e)

    # ── GPU enforcement ───────────────────────────────────────────────

    def _enforce_gpu(self, gpu_fraction: float):
        """Enforce GPU cap via CUDA environment variables.

        CUDA_MPS_ACTIVE_THREAD_PERCENTAGE limits GPU SM utilization.
        CUDA_VISIBLE_DEVICES can restrict which GPUs are used.
        Also reserves SYSTEM_BUFFER_VRAM_GB via VRAMManager budget.
        """
        if gpu_fraction <= 0:
            # No GPU allowed in this mode
            os.environ['CUDA_VISIBLE_DEVICES'] = ''
            return

        pct = max(10, int(gpu_fraction * 100))
        os.environ['CUDA_MPS_ACTIVE_THREAD_PERCENTAGE'] = str(pct)

        # Restore GPU visibility if previously hidden
        if os.environ.get('CUDA_VISIBLE_DEVICES') == '':
            os.environ.pop('CUDA_VISIBLE_DEVICES', None)

        # Set VRAM budget via lifecycle (leaves buffer for other apps)
        try:
            from integrations.service_tools.vram_manager import vram_manager
            info = vram_manager.detect_gpu()
            total_vram = info.get('total_gb', 0)
            if total_vram > 0:
                budget = max(0.5, total_vram * gpu_fraction - SYSTEM_BUFFER_VRAM_GB)
                os.environ['HARTOS_VRAM_BUDGET_GB'] = str(round(budget, 1))
                logger.info("ResourceEnforcer: GPU %d%% SM, VRAM budget %.1fGB "
                             "(total %.1fGB, buffer %.1fGB)",
                             pct, budget, total_vram, SYSTEM_BUFFER_VRAM_GB)
        except Exception:
            pass

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _get_total_ram_gb() -> float:
        psutil = _try_import_psutil()
        if psutil:
            try:
                return psutil.virtual_memory().total / (1024**3)
            except Exception:
                pass
        # Windows fallback
        if sys.platform == 'win32':
            try:
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ('dwLength', ctypes.c_ulong),
                        ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong),
                        ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong),
                        ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong),
                        ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                    ]
                ms = MEMORYSTATUSEX()
                ms.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
                return ms.ullTotalPhys / (1024**3)
            except Exception:
                pass
        return 8.0  # Safe default


# Module-level enforcer singleton
_enforcer: Optional[ResourceEnforcer] = None


_enforcer_lock = threading.Lock()


def get_enforcer() -> ResourceEnforcer:
    """Get or create the singleton ResourceEnforcer."""
    global _enforcer
    if _enforcer is None:
        with _enforcer_lock:
            if _enforcer is None:
                _enforcer = ResourceEnforcer()
    return _enforcer


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
        # Dedicated shutdown signal for the MONITOR loop's interval sleep. It
        # must NOT share _cancel_event: that event is SET for the whole time the
        # mode is ACTIVE/SLEEP (to keep the proactive thread backed off), so a
        # monitor that slept on it would never block — it would busy-spin the
        # expensive _refresh_cpu_attribution()/children() walk and peg a core
        # (the 2026-06-05 "Nunba hung + no TTS" GIL-starvation incident). Only
        # stop() sets this; the monitor always sleeps its full interval.
        self._stop_event = threading.Event()

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

        # Managed subprocesses spawned by HARTOS (e.g. hevolveai server).
        # Keyed by display name -> pid.  Visible in get_stats() so the
        # monitor / dashboards account for the full HARTOS process tree
        # instead of just the current process.
        self._managed_subprocesses: dict = {}

        # CPU attribution cache (refreshed once per monitor tick).  Splits
        # total system CPU into HARTOS's own process tree vs everything
        # else so load-based backoff reacts to OTHER apps, not our own
        # idle-compute work.  get_throttle() reads these cached floats so
        # the per-call hot path never walks the process tree.
        self._own_proc_cache: dict = {}   # pid -> psutil.Process (kept across ticks for cpu_percent baseline)
        self._cached_total_cpu: float = 0.0
        self._cached_own_cpu: float = 0.0
        self._cached_external_cpu: float = 0.0
        self._cached_llm_pid = None       # llama-server pid, resolved by port (re-resolved when stale)
        # Prime the main-process cpu_percent baseline so the first
        # attribution tick returns a real own-CPU value (psutil's first
        # cpu_percent(None) on a fresh handle always returns 0.0).
        try:
            import psutil as _ps
            _main = _ps.Process(os.getpid())
            _main.cpu_percent(None)
            self._own_proc_cache[os.getpid()] = _main
        except Exception:
            pass

    def register_subprocess(self, name: str, pid: int) -> None:
        """Track a child process owned by HARTOS so the monitor accounts
        for it.  Called by subsystem supervisors (e.g. hevolveai) right
        after spawn.  Idempotent — re-registering the same name updates
        the pid (covers supervisor restart loops).
        """
        if not name or not isinstance(pid, int) or pid <= 0:
            return
        with self._lock:
            self._managed_subprocesses[name] = pid

    def unregister_subprocess(self, name: str, pid: int) -> None:
        """Untrack a child process — called after it exits.  Guards on
        pid match so a stale unregister after restart doesn't clobber
        the fresh PID."""
        if not name:
            return
        with self._lock:
            cur = self._managed_subprocesses.get(name)
            if cur == pid:
                self._managed_subprocesses.pop(name, None)

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self, defer_memory_limit: bool = False) -> None:
        """Start the governor background monitor and proactive stream.

        Args:
            defer_memory_limit: If True, apply priority + CPU caps now but
                skip the Job Object / cgroup memory limit.  The caller is
                responsible for calling apply_memory_limit() later (after
                the heavy import spike is over).  This prevents Windows
                from terminating the process during the boot-time memory
                peak (autogen + flaml + llmlingua + transformers + 96
                expert agents all imported before webview.start).
        """
        with self._lock:
            if self._running:
                return
            self._running = True
            self._cancel_event.clear()
            self._stop_event.clear()   # reset shutdown signal for (re)start
            self._stats['uptime_start'] = time.time()

        # Apply hard OS-level resource caps at startup
        try:
            enforcer = get_enforcer()
            if defer_memory_limit:
                # Priority + CPU only — memory cap deferred to avoid
                # SIGKILL on boot-time spike (see app.py comment block).
                enforcer._set_process_priority()
                enforcer._enforce_cpu(0.75, max(1, int((os.cpu_count() or 4) * 0.75)), os.cpu_count() or 4)
                enforcer._enforced = True  # mark so update_caps doesn't re-enforce
                logger.info("ResourceEnforcer: priority + CPU applied (memory deferred)")
            else:
                enforcer.enforce(cpu_fraction=0.75, ram_fraction=0.75, gpu_fraction=0.75)
        except Exception as e:
            logger.warning("ResourceEnforcer failed at startup: %s", e)

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

    def apply_memory_limit(self) -> None:
        """Apply the deferred RAM limit — call AFTER boot-time spike is over.

        Pairs with start(defer_memory_limit=True).  Safe to call multiple
        times (idempotent via enforcer._enforce_ram's Job Object update).
        """
        try:
            enforcer = get_enforcer()
            total_ram_gb = enforcer._get_total_ram_gb()
            usable_ram_gb = max(0.5, total_ram_gb * 0.75 - SYSTEM_BUFFER_RAM_GB)
            enforcer._enforce_ram(usable_ram_gb)
            logger.info("ResourceEnforcer: memory cap now active (%.1fGB)", usable_ram_gb)
        except Exception as e:
            logger.warning("ResourceEnforcer: deferred memory cap failed: %s", e)

    def stop(self) -> None:
        """Stop the governor, release all throttles."""
        with self._lock:
            if not self._running:
                return
            self._running = False

        self._cancel_event.set()   # wake the proactive thread for shutdown
        self._stop_event.set()     # wake the monitor loop's interval sleep

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

        # GPU follows the gpu_allowed flag in BOTH active and idle — the model
        # is foreground work the user waits on, never blocked just because the
        # user is at the keyboard.  Politeness to other apps comes from CPU
        # affinity + BELOW_NORMAL priority, not from refusing the GPU.
        if resource == 'gpu':
            return self._gpu_allowed
        # Heavy disk / network BACKGROUND work still backs off in ACTIVE to stay
        # polite; CPU is bounded by affinity, so cpu_heavy is fine either way.
        if mode == MODE_ACTIVE and resource in ('disk_heavy', 'network_heavy'):
            return False
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
            stats['managed_subprocesses'] = dict(self._managed_subprocesses)
        stats['mode'] = self._mode
        stats['throttle'] = self._calculate_throttle()
        stats['cpu_limit'] = self._cpu_limit
        stats['gpu_allowed'] = self._gpu_allowed
        # CPU attribution (why the mode/throttle is what it is): total vs
        # HARTOS's own tree vs external.  external is what drives backoff.
        stats['cpu_total'] = round(self._cached_total_cpu, 3)
        stats['cpu_own'] = round(self._cached_own_cpu, 3)
        stats['cpu_external'] = round(self._cached_external_cpu, 3)
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
            # GPU stays available — the model is the user's foreground request,
            # not a background hog.  (Background proactive work still backs off
            # immediately via the cancel event below.)
            self._gpu_allowed = True
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

        # Update hard OS-level caps to match new mode
        try:
            get_enforcer().update_caps(new_mode)
        except Exception:
            pass

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

    def _target_mode_for(self, user_idle: bool, ext_cpu: float, mem: float,
                         battery_level: float, on_battery: bool) -> str:
        """Pure mode decision from sampled signals — no side effects, so
        it is directly unit-testable.

        ``ext_cpu`` is EXTERNAL cpu (total minus HARTOS's own process
        tree).  Using external rather than total CPU here is the
        2026-05-30 self-defeat fix: HARTOS's own idle-compute work no
        longer counts as "system heavily loaded", so the flywheel can run
        when the user is away instead of throttling itself the instant it
        starts.  We still go ACTIVE when ANOTHER app loads the box.
        """
        if on_battery and battery_level < BATTERY_SLEEP_THRESHOLD:
            return MODE_SLEEP            # critical battery → suspend
        if not user_idle:
            return MODE_ACTIVE           # user at the keyboard → yield to them
        if ext_cpu > LOAD_BACKOFF_CPU or mem > LOAD_BACKOFF_MEM:
            return MODE_ACTIVE           # OTHER apps / memory pressure → be polite
        return MODE_IDLE                 # user away, only our own work (if any)

    # ── Monitor Loop ──────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        """Background thread: monitor system state every 5 seconds.

        Three cheap checks per tick: CPU, user idle, battery.
        Transitions modes based on combined signals.
        """
        while self._running:
            # Heartbeat to watchdog — prevents false-positive FROZEN alerts
            try:
                from security.node_watchdog import get_watchdog
                _wd = get_watchdog()
                if _wd:
                    _wd.heartbeat('resource_governor_monitor')
            except Exception:
                pass
            try:
                # Refresh CPU attribution once per tick (total / own /
                # external) so this decision AND get_throttle() share one
                # process-tree walk via the cache.
                self._refresh_cpu_attribution()
                mem = self._get_memory_pressure()
                user_idle = self._detect_user_idle()
                battery_level, on_battery = self._get_battery_status()
                ext_cpu = self._cached_external_cpu

                target = self._target_mode_for(
                    user_idle, ext_cpu, mem, battery_level, on_battery)
                self._transition_to(target)
                # Low-battery-but-not-critical: stay IDLE but on half budget.
                if (target == MODE_IDLE and on_battery
                        and battery_level < BATTERY_THROTTLE_THRESHOLD):
                    self._cpu_limit = IDLE_CPU_LIMIT * 0.5

                # Update GPU allowance based on VRAM availability
                if self._mode == MODE_IDLE:
                    self._gpu_allowed = self._check_gpu_available()

            except Exception as e:
                logger.debug("ResourceGovernor monitor error: %s", e)

            # Sleep the monitor interval. Wait on the DEDICATED _stop_event, NOT
            # _cancel_event: the latter stays SET for the whole duration of
            # ACTIVE/SLEEP (to keep the proactive thread backed off), so waiting
            # on it here returned instantly every tick and busy-spun the
            # expensive _refresh_cpu_attribution()/children() walk — pegging a
            # core, starving the GIL, killing TTS (2026-06-05 hang). _stop_event
            # is set only by stop(), so this blocks the full interval in normal
            # operation. Mode transitions manage _cancel_event themselves.
            self._stop_event.wait(timeout=_MONITOR_INTERVAL_SECONDS)
            if not self._running:
                break

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
             **no_window_kwargs())
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
             **no_window_kwargs())
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

    def _get_optimizer_snapshot(self):
        """Read the latest cached SystemSnapshot from ComputeOptimizer.

        Returns the snap or None when the optimizer module/singleton
        isn't available (e.g. degraded boot, or a deployment that
        explicitly disables it).  ``include_per_process=False`` is
        critical here — Governor only needs cpu/ram aggregates, and
        forcing the optimizer's process_iter walk on every Governor
        tick under sustained pressure would actually INCREASE GIL
        contention (the opposite of why this consolidation exists).

        Single source of truth for system pressure data — eliminates
        the 2026-05-01 parallel-psutil-poll between Governor and
        Optimizer that py-spy caught with BOTH monitors active+gil
        simultaneously.
        """
        try:
            from core.compute_optimizer import get_optimizer
            return get_optimizer().get_latest_snapshot(
                max_age_s=10.0, include_per_process=False,
            )
        except Exception:
            return None

    def _get_cpu_usage(self) -> float:
        """Get current CPU usage as a float 0.0 to 1.0.

        Read order:
          1. ComputeOptimizer's cached snapshot (canonical psutil reader).
          2. Direct psutil fallback when optimizer unavailable.
          3. Linux ``os.getloadavg()`` when psutil itself is missing.
          4. Constant 0.3 as a last-resort moderate-usage assumption.

        The first path is the steady-state hot path post-2026-05-01
        unification — Governor used to call psutil.cpu_percent itself
        every 5s, duplicating ComputeOptimizer's 15s poll.  The cached
        snap gives us the same data with one writer.
        """
        snap = self._get_optimizer_snapshot()
        if snap is not None:
            return snap.cpu_percent / 100.0

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

    def _get_own_cpu_usage(self) -> float:
        """Fraction (0..1 of total capacity) consumed by HARTOS's OWN
        process tree: this process + its children + every registered
        managed subprocess (llama-server, hevolveai, …) + their children.

        Used to compute EXTERNAL cpu for load-based backoff so the
        governor never treats HARTOS's own idle-compute work as a reason
        to throttle itself (the 2026-05-30 self-defeat loop).  Process
        handles are cached across ticks because psutil's
        ``cpu_percent(None)`` needs a prior call to establish its delta
        baseline.  Bounded walk (own tree only) — never a full
        ``process_iter``.  Returns 0.0 when psutil is unavailable, which
        makes external==total (conservative: backs off as before).
        """
        psutil = _try_import_psutil()
        if psutil is None:
            return 0.0
        try:
            ncpu = psutil.cpu_count() or 1
        except Exception:
            ncpu = 1

        own_pids = set()
        # Main process tree.
        try:
            main = self._own_proc_cache.get(os.getpid())
            if main is None:
                main = psutil.Process(os.getpid())
                main.cpu_percent(None)  # prime baseline
                self._own_proc_cache[os.getpid()] = main
            own_pids.add(os.getpid())
            for child in main.children(recursive=True):
                own_pids.add(child.pid)
        except Exception:
            pass
        # Registered managed-subprocess trees (hevolveai…) PLUS the
        # llama-server resolved by port.  llama-server is NOT spawned by
        # HARTOS (it's a configured endpoint) so it's never
        # register_subprocess'd — but the agent daemon's inference IS
        # HARTOS's own work.  Without counting it as own, its inference CPU
        # reads as a foreign app and trips both the governor backoff AND
        # model_lifecycle pressure → the yield gate flaps and no goal ever
        # completes a tick (2026-05-31 idle-hour: 0 executions).
        with self._lock:
            managed = list(self._managed_subprocesses.values())
        _llm_pid = self._resolve_llm_server_pid()
        if _llm_pid and _llm_pid not in managed:
            managed.append(_llm_pid)
        for pid in managed:
            try:
                proc = self._own_proc_cache.get(pid)
                if proc is None:
                    proc = psutil.Process(pid)
                    proc.cpu_percent(None)  # prime baseline
                    self._own_proc_cache[pid] = proc
                own_pids.add(pid)
                for child in proc.children(recursive=True):
                    own_pids.add(child.pid)
            except Exception:
                self._own_proc_cache.pop(pid, None)

        total_pct = 0.0
        for pid in own_pids:
            proc = self._own_proc_cache.get(pid)
            if proc is None:
                try:
                    proc = psutil.Process(pid)
                    proc.cpu_percent(None)  # prime; contributes 0 this tick
                    self._own_proc_cache[pid] = proc
                    continue
                except Exception:
                    continue
            try:
                total_pct += proc.cpu_percent(None)  # since last call (~tick)
            except Exception:
                self._own_proc_cache.pop(pid, None)
        # Prune handles for pids no longer in our tree (bounds cache size).
        for pid in list(self._own_proc_cache.keys()):
            if pid not in own_pids and pid != os.getpid():
                self._own_proc_cache.pop(pid, None)

        # Process.cpu_percent() sums across cores (can exceed 100); divide
        # by core count to get the same 0..1 fraction _get_cpu_usage uses.
        own_frac = (total_pct / 100.0) / ncpu
        return max(0.0, min(1.0, own_frac))

    def _refresh_cpu_attribution(self) -> None:
        """Refresh cached total / own / external CPU.  Called once per
        monitor tick so ``get_throttle()`` reads cheap cached floats
        instead of walking the process tree on every call."""
        total = self._get_cpu_usage()
        own = self._get_own_cpu_usage()
        self._cached_total_cpu = total
        self._cached_own_cpu = own
        self._cached_external_cpu = max(0.0, total - own)

    def _external_cpu_for_throttle(self) -> float:
        """External (non-HARTOS) CPU fraction for throttle scaling.

        Falls back to total CPU when attribution hasn't run yet (governor
        not started / first tick) so the pre-start default stays
        conservative rather than optimistically 0.
        """
        if self._cached_total_cpu > 0.0:
            return self._cached_external_cpu
        return self._get_cpu_usage()

    def get_external_cpu_fraction(self):
        """External (non-HARTOS) CPU fraction 0..1 — the SINGLE source other
        subsystems should consult instead of raw ``psutil.cpu_percent`` so
        HARTOS's own LLM/daemon work is excluded from "is the system
        overloaded" decisions (e.g. ``model_lifecycle._calculate_throttle_
        factor`` → the model_pressure yield-gate reason).  Returns ``None``
        before the first monitor tick has populated the cache — callers
        must fall back to total CPU in that window.
        """
        if self._cached_total_cpu > 0.0:
            return self._cached_external_cpu
        return None

    def _resolve_llm_server_pid(self):
        """PID of the local LLM server (llama-server), resolved by the TCP
        port it listens on.  HARTOS does not spawn llama-server, so it's
        never ``register_subprocess``'d — this is how its CPU gets
        attributed to HARTOS's own work.  Cached: re-resolves only when the
        cached pid has exited (the per-tick fast path is a cheap
        ``pid_exists``).  Returns ``None`` when psutil is unavailable or no
        listener is found (degrades to "llama counts as external", i.e. the
        pre-fix conservative behaviour)."""
        psutil = _try_import_psutil()
        if psutil is None:
            return None
        cached = self._cached_llm_pid
        if cached is not None:
            try:
                if psutil.pid_exists(cached):
                    return cached
            except Exception:
                pass
            self._cached_llm_pid = None
            self._own_proc_cache.pop(cached, None)
        # Resolve via the shared port-scan (single source — _find_llm_pids).
        for pid in _find_llm_pids():
            self._cached_llm_pid = pid
            return pid
        return None

    def _get_memory_pressure(self) -> float:
        """Get memory pressure as a float 0.0 to 1.0.

        Read order (mirrors ``_get_cpu_usage`` for symmetry):
          1. ComputeOptimizer's cached snapshot (canonical psutil reader).
          2. Direct psutil fallback when optimizer unavailable.
          3. Linux ``/proc/meminfo`` when psutil itself is missing.
          4. Constant 0.4 as a last-resort moderate-usage assumption.
        """
        snap = self._get_optimizer_snapshot()
        if snap is not None:
            return snap.ram_percent / 100.0

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

        # IDLE mode — scale based on current resource usage.  Use EXTERNAL
        # cpu (total - HARTOS's own tree): scaling on total here would make
        # the daemon's own work drop its own throttle below should_yield's
        # 0.3 gate, halting the flywheel it just started (2026-05-30 loop).
        cpu = self._external_cpu_for_throttle()
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
            # Heartbeat to watchdog each loop iteration
            try:
                from security.node_watchdog import get_watchdog
                _wd = get_watchdog()
                if _wd:
                    _wd.heartbeat('resource_governor_proactive')
            except Exception:
                pass
            # Sleep the proactive interval. Wait on the DEDICATED _stop_event,
            # NOT _cancel_event: the latter is SET for the whole duration of
            # ACTIVE/SLEEP (to signal "stop proactive work"), so waiting on it
            # here returned instantly every iteration and busy-spun the loop
            # (watchdog heartbeat + 4 _jitter() calls) on a core — exactly while
            # the user is active (2026-06-05 hang, the sibling of the monitor
            # busy-spin). _stop_event is set only by stop(); the _mode != IDLE
            # check below still suppresses proactive work while ACTIVE, and
            # in-flight proactive tasks still abort via _cancel_event.
            self._stop_event.wait(timeout=5.0)

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
        """Check hive signals AND run agentic service discovery."""
        if self._mode != MODE_IDLE:
            return

        # Hive signal feed
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

        # Agentic service discovery — autonomously find new providers/models
        try:
            from integrations.providers.discovery_agent import get_discovery_agent
            discoveries = get_discovery_agent().run_discovery_cycle()
            if discoveries:
                with self._lock:
                    self._stats['proactive_actions'] += 1
                self._emit_proactive_event('service_discovery', {
                    'discoveries': len(discoveries),
                })
        except Exception as e:
            logger.debug("ResourceGovernor: discovery agent failed: %s", e)

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
