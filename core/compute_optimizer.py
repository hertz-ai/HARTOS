"""
OS Compute Optimizer — Make HARTOS a net positive on any system.

Event-based system resource optimizer that:
1. Monitors: CPU, RAM, disk I/O, network, swap, process priorities, temp files
2. Optimizes: auto-tune process priorities, clean caches, manage swap, power profiles
3. Proactive: random-interval hive action stream exploration for network-wide optimization
4. Reports: emit EventBus events for dashboard, federation delta for hive-wide stats

Design principles:
- Event-driven: only wake when thresholds breach or random hive tick fires
- Zero overhead when system is healthy — sleep when idle
- Cross-platform: Windows + Linux + macOS
- No ML in HARTOS — heuristic only
- NEVER kills processes, NEVER deletes user files — only system temp/cache

Integration:
- EventBus: emits 'system.health.snapshot', 'system.optimization.applied'
- GPU detection: vram_manager.detect_gpu() (single source)
- Federation: anonymized stats contributed via delta channel
- Resource governor: respects governor mode (ACTIVE/IDLE/SLEEP)

Singleton: get_optimizer() returns the module-level instance.
Flask blueprint: create_optimizer_blueprint() for REST API.
"""

import collections
import enum
import logging
import os
import platform
import random
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# =====================================================================
# Lazy Imports
# =====================================================================

def _try_import_psutil():
    """Return psutil module or None if unavailable."""
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def _try_detect_gpu() -> Dict[str, Any]:
    """Detect GPU via vram_manager (single source of truth)."""
    try:
        from integrations.service_tools.vram_manager import detect_gpu
        return detect_gpu()
    except Exception:
        return {'name': 'none', 'total_gb': 0, 'free_gb': 0, 'cuda_available': False}


def _emit(topic: str, data: Any = None) -> None:
    """Emit an event on the platform EventBus (best-effort, no-op if not bootstrapped)."""
    try:
        from core.platform.events import emit_event
        emit_event(topic, data)
    except Exception:
        pass


# =====================================================================
# Thresholds (configurable via env)
# =====================================================================

CPU_HIGH_THRESHOLD = float(os.environ.get('OPTIMIZER_CPU_HIGH', '80'))
RAM_HIGH_THRESHOLD = float(os.environ.get('OPTIMIZER_RAM_HIGH', '85'))
SWAP_HIGH_THRESHOLD = float(os.environ.get('OPTIMIZER_SWAP_HIGH', '50'))
DISK_HIGH_THRESHOLD = float(os.environ.get('OPTIMIZER_DISK_HIGH', '90'))

# Monitor loop sleeps this long between threshold checks (seconds)
MONITOR_INTERVAL = float(os.environ.get('OPTIMIZER_MONITOR_INTERVAL', '15'))

# Hive exploration: random interval bounds (seconds)
HIVE_EXPLORE_MIN = float(os.environ.get('OPTIMIZER_HIVE_MIN', '300'))   # 5 min
HIVE_EXPLORE_MAX = float(os.environ.get('OPTIMIZER_HIVE_MAX', '1800'))  # 30 min

# Max age of temp files to clean (seconds) — default 24h
TEMP_CLEAN_MAX_AGE = float(os.environ.get('OPTIMIZER_TEMP_MAX_AGE', '86400'))

# Optimization history buffer size
HISTORY_MAXLEN = 200


# =====================================================================
# Enums & Dataclasses
# =====================================================================

class ActionType(enum.Enum):
    """Types of optimization actions the optimizer can take."""
    PRIORITY_ADJUST = 'priority_adjust'
    CACHE_CLEAN = 'cache_clean'
    SWAP_MANAGE = 'swap_manage'
    POWER_TUNE = 'power_tune'
    PROCESS_SUGGEST = 'process_suggest'
    NETWORK_TUNE = 'network_tune'


@dataclass
class SystemSnapshot:
    """Point-in-time snapshot of system resource utilization."""
    timestamp: float = 0.0
    cpu_percent: float = 0.0
    ram_percent: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    swap_percent: float = 0.0
    disk_usage_percent: float = 0.0      # Root/C: partition
    disk_io_read_mb: float = 0.0         # Cumulative MB read
    disk_io_write_mb: float = 0.0        # Cumulative MB written
    net_sent_mb: float = 0.0             # Cumulative MB sent
    net_recv_mb: float = 0.0             # Cumulative MB received
    top_processes: List[Dict] = field(default_factory=list)  # [{name, pid, cpu%, mem%}]
    gpu_util_percent: float = 0.0
    gpu_mem_used_gb: float = 0.0
    gpu_mem_total_gb: float = 0.0
    platform_name: str = ''

    def to_dict(self) -> Dict:
        return {
            'timestamp': self.timestamp,
            'cpu_percent': round(self.cpu_percent, 1),
            'ram_percent': round(self.ram_percent, 1),
            'ram_used_gb': round(self.ram_used_gb, 2),
            'ram_total_gb': round(self.ram_total_gb, 2),
            'swap_percent': round(self.swap_percent, 1),
            'disk_usage_percent': round(self.disk_usage_percent, 1),
            'disk_io_read_mb': round(self.disk_io_read_mb, 1),
            'disk_io_write_mb': round(self.disk_io_write_mb, 1),
            'net_sent_mb': round(self.net_sent_mb, 1),
            'net_recv_mb': round(self.net_recv_mb, 1),
            'top_processes': self.top_processes[:10],
            'gpu_util_percent': round(self.gpu_util_percent, 1),
            'gpu_mem_used_gb': round(self.gpu_mem_used_gb, 2),
            'gpu_mem_total_gb': round(self.gpu_mem_total_gb, 2),
            'platform': self.platform_name,
        }


@dataclass
class OptimizationAction:
    """A single optimization action the optimizer can take or suggest."""
    action_type: ActionType
    target: str                     # What is being optimized (e.g., process name, path)
    params: Dict = field(default_factory=dict)
    impact_estimate: str = ''       # Human-readable expected impact
    applied: bool = False
    timestamp: float = 0.0
    result: str = ''                # Outcome after applying

    def to_dict(self) -> Dict:
        return {
            'action_type': self.action_type.value,
            'target': self.target,
            'params': self.params,
            'impact_estimate': self.impact_estimate,
            'applied': self.applied,
            'timestamp': self.timestamp,
            'result': self.result,
        }


# =====================================================================
# ComputeOptimizer
# =====================================================================

class ComputeOptimizer:
    """Event-based system optimizer — makes HARTOS a net positive on any host.

    Only wakes when thresholds breach or a random hive-exploration tick fires.
    All optimizations are non-destructive: never kills processes, never deletes
    user files, only cleans system temp/cache and adjusts priorities.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._hive_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Snapshot history (bounded)
        self._snapshots: collections.deque = collections.deque(maxlen=60)
        self._last_snapshot: Optional[SystemSnapshot] = None

        # Optimization history (bounded)
        self._history: collections.deque = collections.deque(maxlen=HISTORY_MAXLEN)

        # Stats counters
        self._optimizations_applied: int = 0
        self._suggestions_made: int = 0
        self._hive_explorations: int = 0
        self._cache_bytes_freed: int = 0

        # Cooldowns: action_type -> last_applied_timestamp
        self._cooldowns: Dict[str, float] = {}

        # Platform
        self._platform = platform.system()  # 'Windows', 'Linux', 'Darwin'

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Start background monitor and hive exploration threads."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name='compute_optimizer_monitor',
            daemon=True,
        )
        self._hive_thread = threading.Thread(
            target=self._hive_explore_loop,
            name='compute_optimizer_hive',
            daemon=True,
        )
        self._monitor_thread.start()
        self._hive_thread.start()
        logger.info("ComputeOptimizer started (monitor=%.0fs, hive=%.0f-%.0fs)",
                     MONITOR_INTERVAL, HIVE_EXPLORE_MIN, HIVE_EXPLORE_MAX)

    def stop(self) -> None:
        """Stop background threads gracefully."""
        with self._lock:
            if not self._running:
                return
            self._running = False
        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5)
        if self._hive_thread and self._hive_thread.is_alive():
            self._hive_thread.join(timeout=5)
        logger.info("ComputeOptimizer stopped")

    # ── Monitor Loop (event-driven: sleep between checks) ─────────

    def _monitor_loop(self) -> None:
        """Periodic threshold check — sleeps between iterations."""
        while not self._stop_event.is_set():
            try:
                snap = self.snapshot()
                self._emit_stats(snap)

                # Only act when thresholds breached
                actions = self._check_thresholds(snap)
                if actions:
                    suggestions = self._suggest_optimizations(snap)
                    for action in suggestions:
                        self._apply_optimization(action)
            except Exception as e:
                logger.debug("ComputeOptimizer monitor error: %s", e)

            self._stop_event.wait(MONITOR_INTERVAL)

    def _hive_explore_loop(self) -> None:
        """Random-interval hive action stream exploration."""
        while not self._stop_event.is_set():
            delay = random.uniform(HIVE_EXPLORE_MIN, HIVE_EXPLORE_MAX)
            if self._stop_event.wait(delay):
                break  # Stop requested during sleep
            try:
                self._explore_hive_stream()
                with self._lock:
                    self._hive_explorations += 1
            except Exception as e:
                logger.debug("ComputeOptimizer hive exploration error: %s", e)

    # ── Snapshot ──────────────────────────────────────────────────

    def snapshot(self) -> SystemSnapshot:
        """Capture current system state. Returns a SystemSnapshot.

        Uses psutil if available; falls back to minimal OS-level data.
        GPU info via vram_manager (single source of truth).
        """
        snap = SystemSnapshot(
            timestamp=time.time(),
            platform_name=self._platform,
        )

        psutil = _try_import_psutil()
        if psutil is not None:
            # CPU — non-blocking (interval=None returns since-last-call)
            snap.cpu_percent = psutil.cpu_percent(interval=None)

            # RAM
            mem = psutil.virtual_memory()
            snap.ram_percent = mem.percent
            snap.ram_used_gb = mem.used / (1024 ** 3)
            snap.ram_total_gb = mem.total / (1024 ** 3)

            # Swap
            swap = psutil.swap_memory()
            snap.swap_percent = swap.percent if swap.total > 0 else 0.0

            # Disk usage (root partition)
            try:
                root = 'C:\\' if self._platform == 'Windows' else '/'
                disk = psutil.disk_usage(root)
                snap.disk_usage_percent = disk.percent
            except Exception:
                pass

            # Disk I/O
            try:
                dio = psutil.disk_io_counters()
                if dio:
                    snap.disk_io_read_mb = dio.read_bytes / (1024 ** 2)
                    snap.disk_io_write_mb = dio.write_bytes / (1024 ** 2)
            except Exception:
                pass

            # Network I/O
            try:
                nio = psutil.net_io_counters()
                if nio:
                    snap.net_sent_mb = nio.bytes_sent / (1024 ** 2)
                    snap.net_recv_mb = nio.bytes_recv / (1024 ** 2)
            except Exception:
                pass

            # Top processes by CPU (fast: one-shot, no interval)
            try:
                procs = []
                for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
                    info = proc.info
                    if info.get('cpu_percent', 0) > 0.1:
                        procs.append({
                            'pid': info['pid'],
                            'name': info.get('name', ''),
                            'cpu_percent': round(info.get('cpu_percent', 0), 1),
                            'mem_percent': round(info.get('memory_percent', 0), 1),
                        })
                procs.sort(key=lambda p: p['cpu_percent'], reverse=True)
                snap.top_processes = procs[:10]
            except Exception:
                pass

        # GPU (via vram_manager)
        gpu_info = _try_detect_gpu()
        if gpu_info.get('cuda_available'):
            snap.gpu_mem_total_gb = gpu_info.get('total_gb', 0)
            snap.gpu_mem_used_gb = snap.gpu_mem_total_gb - gpu_info.get('free_gb', 0)
            if snap.gpu_mem_total_gb > 0:
                snap.gpu_util_percent = (snap.gpu_mem_used_gb / snap.gpu_mem_total_gb) * 100

        with self._lock:
            self._snapshots.append(snap)
            self._last_snapshot = snap

        return snap

    # ── Threshold Detection ───────────────────────────────────────

    def _check_thresholds(self, snap: SystemSnapshot) -> List[str]:
        """Detect which thresholds are breached. Returns list of breach names."""
        breaches = []
        if snap.cpu_percent > CPU_HIGH_THRESHOLD:
            breaches.append('cpu_high')
        if snap.ram_percent > RAM_HIGH_THRESHOLD:
            breaches.append('ram_high')
        if snap.swap_percent > SWAP_HIGH_THRESHOLD:
            breaches.append('swap_high')
        if snap.disk_usage_percent > DISK_HIGH_THRESHOLD:
            breaches.append('disk_high')
        return breaches

    # ── Suggest Optimizations (heuristic) ─────────────────────────

    def _suggest_optimizations(self, snap: SystemSnapshot) -> List[OptimizationAction]:
        """Generate non-destructive optimization suggestions based on snapshot."""
        actions = []
        now = time.time()

        # CPU high: suggest lowering heavy non-essential processes
        if snap.cpu_percent > CPU_HIGH_THRESHOLD:
            if self._cooldown_ok(ActionType.PROCESS_SUGGEST, now, 120):
                hogs = [p for p in snap.top_processes
                        if p.get('cpu_percent', 0) > 20 and p.get('name', '') != '']
                for proc in hogs[:3]:
                    actions.append(OptimizationAction(
                        action_type=ActionType.PROCESS_SUGGEST,
                        target=proc.get('name', 'unknown'),
                        params={'pid': proc.get('pid', 0), 'cpu_percent': proc['cpu_percent']},
                        impact_estimate=f"Lower priority of {proc['name']} "
                                        f"(using {proc['cpu_percent']}% CPU)",
                    ))

        # RAM high: clean temp/cache
        if snap.ram_percent > RAM_HIGH_THRESHOLD:
            if self._cooldown_ok(ActionType.CACHE_CLEAN, now, 300):
                actions.append(OptimizationAction(
                    action_type=ActionType.CACHE_CLEAN,
                    target='system_temp',
                    params={'max_age_seconds': TEMP_CLEAN_MAX_AGE},
                    impact_estimate='Free RAM by clearing stale temp files',
                ))

        # Swap high: suggest reducing memory pressure
        if snap.swap_percent > SWAP_HIGH_THRESHOLD:
            if self._cooldown_ok(ActionType.SWAP_MANAGE, now, 600):
                actions.append(OptimizationAction(
                    action_type=ActionType.SWAP_MANAGE,
                    target='swap_pressure',
                    params={'swap_percent': snap.swap_percent},
                    impact_estimate='Reduce swap usage by freeing cached memory',
                ))

        # Disk high: clean temp directories
        if snap.disk_usage_percent > DISK_HIGH_THRESHOLD:
            if self._cooldown_ok(ActionType.CACHE_CLEAN, now, 600):
                actions.append(OptimizationAction(
                    action_type=ActionType.CACHE_CLEAN,
                    target='disk_temp',
                    params={'max_age_seconds': TEMP_CLEAN_MAX_AGE},
                    impact_estimate='Free disk space by clearing stale temp files',
                ))

        with self._lock:
            self._suggestions_made += len(actions)

        return actions

    def _cooldown_ok(self, action_type: ActionType, now: float,
                     min_interval: float) -> bool:
        """Check if enough time has passed since last action of this type."""
        key = action_type.value
        last = self._cooldowns.get(key, 0)
        return (now - last) >= min_interval

    # ── Apply Optimizations (safe, non-destructive) ───────────────

    def _apply_optimization(self, action: OptimizationAction) -> None:
        """Execute an optimization action. Only non-destructive operations."""
        action.timestamp = time.time()

        try:
            if action.action_type == ActionType.PROCESS_SUGGEST:
                action.result = self._apply_priority_adjust(action)
            elif action.action_type == ActionType.CACHE_CLEAN:
                action.result = self._apply_cache_clean(action)
            elif action.action_type == ActionType.SWAP_MANAGE:
                action.result = self._apply_swap_manage(action)
            elif action.action_type == ActionType.POWER_TUNE:
                action.result = self._apply_power_tune(action)
            elif action.action_type == ActionType.NETWORK_TUNE:
                action.result = self._apply_network_tune(action)
            else:
                action.result = 'no handler'
                return

            action.applied = True
            with self._lock:
                self._optimizations_applied += 1
                self._cooldowns[action.action_type.value] = time.time()
                self._history.append(action)

            logger.info("Optimization applied: %s on %s -> %s",
                        action.action_type.value, action.target, action.result)
            _emit('system.optimization.applied', action.to_dict())

        except Exception as e:
            action.result = f'error: {e}'
            with self._lock:
                self._history.append(action)
            logger.debug("Optimization failed: %s on %s: %s",
                         action.action_type.value, action.target, e)

    def _apply_priority_adjust(self, action: OptimizationAction) -> str:
        """Lower priority of a CPU-hogging process (NEVER kill)."""
        psutil = _try_import_psutil()
        if psutil is None:
            return 'psutil not available'

        pid = action.params.get('pid', 0)
        if not pid:
            return 'no pid'

        try:
            proc = psutil.Process(pid)
            name = proc.name()

            # Safety: never touch system-critical processes
            protected = {'systemd', 'init', 'kernel', 'csrss.exe', 'svchost.exe',
                         'wininit.exe', 'services.exe', 'lsass.exe', 'smss.exe',
                         'System', 'explorer.exe', 'dwm.exe', 'loginwindow',
                         'launchd', 'WindowServer'}
            if name in protected:
                return f'skipped (protected: {name})'

            # Lower priority by one notch — never idle/realtime
            if self._platform == 'Windows':
                current = proc.nice()
                # Windows priority classes: IDLE=64, BELOW_NORMAL=16384,
                # NORMAL=32, ABOVE_NORMAL=32768, HIGH=128, REALTIME=256
                if current in (128, 256, 32768):  # HIGH, REALTIME, ABOVE_NORMAL
                    proc.nice(32)  # NORMAL_PRIORITY_CLASS
                    return f'lowered {name} (pid={pid}) to NORMAL priority'
            else:
                current_nice = proc.nice()
                if current_nice < 10:
                    proc.nice(10)
                    return f'set {name} (pid={pid}) nice=10'

            return f'{name} already at acceptable priority'

        except psutil.NoSuchProcess:
            return f'process {pid} no longer exists'
        except psutil.AccessDenied:
            return f'access denied for pid {pid}'
        except Exception as e:
            return f'error: {e}'

    def _apply_cache_clean(self, action: OptimizationAction) -> str:
        """Clean stale temp files. NEVER deletes user files."""
        max_age = action.params.get('max_age_seconds', TEMP_CLEAN_MAX_AGE)
        now = time.time()
        freed = 0
        cleaned = 0

        # System temp directory only
        temp_dir = tempfile.gettempdir()
        try:
            for entry in os.scandir(temp_dir):
                try:
                    # Only delete old files (not directories with content)
                    stat = entry.stat(follow_symlinks=False)
                    age = now - stat.st_mtime
                    if age < max_age:
                        continue

                    if entry.is_file(follow_symlinks=False):
                        size = stat.st_size
                        os.unlink(entry.path)
                        freed += size
                        cleaned += 1
                    elif entry.is_dir(follow_symlinks=False):
                        # Only empty directories
                        try:
                            os.rmdir(entry.path)
                            cleaned += 1
                        except OSError:
                            pass  # Not empty — skip
                except (PermissionError, OSError):
                    continue
        except Exception as e:
            logger.debug("Cache clean scan error: %s", e)

        with self._lock:
            self._cache_bytes_freed += freed

        freed_mb = freed / (1024 * 1024)
        return f'cleaned {cleaned} items, freed {freed_mb:.1f} MB from {temp_dir}'

    def _apply_swap_manage(self, action: OptimizationAction) -> str:
        """Reduce swap pressure by dropping filesystem caches (Linux only)."""
        if self._platform == 'Linux':
            try:
                # Drop page cache only (safest option: value 1)
                # Requires root — will fail gracefully if not root
                with open('/proc/sys/vm/drop_caches', 'w') as f:
                    f.write('1')
                return 'dropped page cache (Linux)'
            except PermissionError:
                return 'drop_caches requires root — skipped'
            except Exception as e:
                return f'swap manage error: {e}'
        elif self._platform == 'Windows':
            # On Windows, cleaning temp files is the safest approach
            return self._apply_cache_clean(OptimizationAction(
                action_type=ActionType.CACHE_CLEAN,
                target='swap_relief',
                params={'max_age_seconds': 3600},  # More aggressive: 1h
            ))
        return 'swap management not available on this platform'

    def _apply_power_tune(self, action: OptimizationAction) -> str:
        """Adjust power profile. Invoked by hive exploration when needed."""
        profile = action.params.get('profile', 'balanced')

        if self._platform == 'Windows':
            # Windows power scheme GUIDs
            schemes = {
                'powersave': '381b4222-f694-41f0-9685-ff5bb260df2e',  # Balanced (most compatible)
                'balanced': '381b4222-f694-41f0-9685-ff5bb260df2e',
                'performance': '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c',
            }
            guid = schemes.get(profile, schemes['balanced'])
            try:
                import subprocess
                subprocess.run(
                    ['powercfg', '/setactive', guid],
                    capture_output=True, timeout=10,
                )
                return f'set power scheme to {profile}'
            except Exception as e:
                return f'power tune error: {e}'

        elif self._platform == 'Linux':
            # cpufreq governor
            governors = {
                'powersave': 'powersave',
                'balanced': 'schedutil',
                'performance': 'performance',
            }
            gov = governors.get(profile, 'schedutil')
            try:
                # Apply to all CPUs
                cpu_count = os.cpu_count() or 1
                applied = 0
                for i in range(cpu_count):
                    path = f'/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_governor'
                    try:
                        with open(path, 'w') as f:
                            f.write(gov)
                        applied += 1
                    except (PermissionError, FileNotFoundError):
                        pass
                return f'set governor={gov} on {applied}/{cpu_count} CPUs'
            except Exception as e:
                return f'power tune error: {e}'

        return 'power tuning not available on this platform'

    def _apply_network_tune(self, action: OptimizationAction) -> str:
        """Network tuning — currently informational only."""
        return 'network tuning noted (advisory only)'

    # ── Hive Exploration (proactive, random-interval) ─────────────

    def _explore_hive_stream(self) -> None:
        """Check hive action stream for optimization goals from other nodes.

        At random intervals (5-30 min), peek at the hive to see if:
        - Nodes in the region are overloaded -> this node can accept more tasks
        - A benchmark is running -> minimize background work
        - New optimization strategies discovered -> apply locally
        - Resource bottlenecks detected network-wide -> contribute stats
        """
        snap = self._last_snapshot
        if snap is None:
            snap = self.snapshot()

        # Contribute anonymized stats to hive
        self._contribute_to_hive(snap)

        # Check for hive optimization goals
        goals = self._fetch_hive_goals()
        if not goals:
            return

        for goal in goals:
            goal_type = goal.get('type', '')

            if goal_type == 'region_overloaded':
                # Other nodes struggling — if we have headroom, signal availability
                if snap.cpu_percent < 50 and snap.ram_percent < 60:
                    _emit('system.hive.available', {
                        'cpu_headroom': round(100 - snap.cpu_percent, 1),
                        'ram_headroom': round(100 - snap.ram_percent, 1),
                        'goal_id': goal.get('id', ''),
                    })
                    logger.info("Hive: signaled availability for overloaded region")

            elif goal_type == 'benchmark_running':
                # Hive is benchmarking — reduce our background work
                action = OptimizationAction(
                    action_type=ActionType.PRIORITY_ADJUST,
                    target='self_throttle',
                    params={'reason': 'hive_benchmark'},
                    impact_estimate='Reduce background work during hive benchmark',
                )
                self._apply_optimization(action)

            elif goal_type == 'power_save_request':
                # Network-wide power saving (e.g., peak grid hours)
                action = OptimizationAction(
                    action_type=ActionType.POWER_TUNE,
                    target='power_profile',
                    params={'profile': 'powersave'},
                    impact_estimate='Switch to powersave for network-wide efficiency',
                )
                self._apply_optimization(action)

    def _fetch_hive_goals(self) -> List[Dict]:
        """Fetch optimization goals from the hive (best-effort).

        Queries the goal manager for active system-optimization goals.
        Returns empty list if hive is not available.
        """
        try:
            from integrations.agent_engine.goal_manager import get_goal_manager
            gm = get_goal_manager()
            if gm is None:
                return []
            # Look for active optimization goals
            goals = []
            all_goals = getattr(gm, 'get_active_goals', lambda: [])()
            for g in all_goals:
                tags = getattr(g, 'tags', []) or []
                if 'system_optimization' in tags or 'compute_optimization' in tags:
                    goals.append({
                        'id': getattr(g, 'id', ''),
                        'type': getattr(g, 'goal_type', ''),
                        'params': getattr(g, 'params', {}),
                    })
            return goals
        except Exception:
            return []

    # ── EventBus & Federation ─────────────────────────────────────

    def _emit_stats(self, snap: SystemSnapshot) -> None:
        """Emit system health snapshot to EventBus for dashboard consumption."""
        data = snap.to_dict()
        data['health_score'] = self.get_health_score(snap)
        _emit('system.health.snapshot', data)

    def _contribute_to_hive(self, snap: SystemSnapshot) -> None:
        """Share anonymized system stats via federation delta channel.

        Stats are aggregated, not personally identifiable:
        - CPU/RAM/disk utilization bands (not exact values)
        - Optimization counts
        - Platform type
        No IP addresses, hostnames, or process names are shared.
        """
        try:
            from integrations.agent_engine.federated_aggregator import get_aggregator
            agg = get_aggregator()
            if agg is None:
                return

            # Anonymized: bucket utilization into bands (low/medium/high/critical)
            def _band(pct: float) -> str:
                if pct < 30:
                    return 'low'
                if pct < 60:
                    return 'medium'
                if pct < 85:
                    return 'high'
                return 'critical'

            delta = {
                'channel': 'compute_health',
                'cpu_band': _band(snap.cpu_percent),
                'ram_band': _band(snap.ram_percent),
                'disk_band': _band(snap.disk_usage_percent),
                'optimizations_applied': self._optimizations_applied,
                'platform': self._platform,
                'has_gpu': snap.gpu_mem_total_gb > 0,
                'timestamp': time.time(),
            }

            # Use broadcast_delta if available
            broadcast = getattr(agg, 'broadcast_delta', None)
            if callable(broadcast):
                broadcast(delta)
        except Exception:
            pass

    # ── Public API ────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        """Return optimization statistics and recent history."""
        with self._lock:
            history = [a.to_dict() for a in self._history]
            return {
                'running': self._running,
                'optimizations_applied': self._optimizations_applied,
                'suggestions_made': self._suggestions_made,
                'hive_explorations': self._hive_explorations,
                'cache_bytes_freed': self._cache_bytes_freed,
                'cache_mb_freed': round(self._cache_bytes_freed / (1024 * 1024), 2),
                'history_count': len(history),
                'recent_history': history[-20:],
                'platform': self._platform,
            }

    def get_health_score(self, snap: Optional[SystemSnapshot] = None) -> float:
        """Compute overall system health score (0.0 = critical, 1.0 = healthy).

        Weighted average of resource utilization:
        - CPU:  30% weight
        - RAM:  30% weight
        - Swap: 20% weight
        - Disk: 20% weight

        Lower utilization = higher score. Score inverts percentage to health.
        """
        if snap is None:
            snap = self._last_snapshot
        if snap is None:
            return 1.0  # No data = assume healthy

        # Convert utilization % to health (100% used = 0.0, 0% used = 1.0)
        cpu_health = max(0.0, 1.0 - snap.cpu_percent / 100.0)
        ram_health = max(0.0, 1.0 - snap.ram_percent / 100.0)
        swap_health = max(0.0, 1.0 - snap.swap_percent / 100.0)
        disk_health = max(0.0, 1.0 - snap.disk_usage_percent / 100.0)

        score = (cpu_health * 0.30
                 + ram_health * 0.30
                 + swap_health * 0.20
                 + disk_health * 0.20)

        return round(max(0.0, min(1.0, score)), 3)

    def trigger_optimization(self) -> Dict:
        """Manually trigger an optimization check. Returns actions taken.

        Feeds attribution chain: health baseline → actions → final health score.
        """
        # Attribution: track this optimization cycle
        attribution_id = None
        try:
            from integrations.agent_engine.agent_attribution import (
                begin_action, record_step, complete_action,
            )
            attribution_id = begin_action(
                agent_id='compute_optimizer',
                action_type='optimization_cycle',
                expected_outcome={'health_score_delta': 0.05},
                acceptance_criteria=['health_score > baseline'],
            )
        except Exception:
            pass

        snap = self.snapshot()
        baseline_health = self.get_health_score(snap)
        if attribution_id:
            try:
                record_step(attribution_id, 'baseline_snapshot',
                            state={'cpu_pct': snap.cpu_percent, 'ram_pct': snap.ram_percent,
                                   'health_score': baseline_health},
                            decision='assess', confidence=1.0)
            except Exception:
                pass

        suggestions = self._suggest_optimizations(snap)
        results = []
        for action in suggestions:
            self._apply_optimization(action)
            results.append(action.to_dict())
            if attribution_id:
                try:
                    record_step(attribution_id, f'applied:{action.action_type.value}',
                                state={'target': action.target,
                                       'result': action.to_dict().get('result', '')},
                                decision=action.action_type.value, confidence=0.7)
                except Exception:
                    pass

        # If no threshold breached, still do a cache clean as maintenance
        if not results:
            action = OptimizationAction(
                action_type=ActionType.CACHE_CLEAN,
                target='maintenance',
                params={'max_age_seconds': TEMP_CLEAN_MAX_AGE},
                impact_estimate='Routine maintenance cache clean',
            )
            self._apply_optimization(action)
            results.append(action.to_dict())

        final_snap = self.snapshot()
        final_health = self.get_health_score(final_snap)

        # Attribution: complete with delta outcome
        if attribution_id:
            try:
                complete_action(attribution_id, outcome={
                    'status': 'completed',
                    'baseline_health_score': baseline_health,
                    'final_health_score': final_health,
                    'health_score_delta': round(final_health - baseline_health, 4),
                    'actions_applied': len(results),
                })
            except Exception:
                pass

        return {
            'snapshot': snap.to_dict(),
            'health_score': final_health,
            'actions': results,
        }


# =====================================================================
# Singleton
# =====================================================================

_optimizer: Optional[ComputeOptimizer] = None
_optimizer_lock = threading.Lock()


def get_optimizer() -> ComputeOptimizer:
    """Get or create the singleton ComputeOptimizer."""
    global _optimizer
    if _optimizer is None:
        with _optimizer_lock:
            if _optimizer is None:
                _optimizer = ComputeOptimizer()
    return _optimizer


# =====================================================================
# Flask Blueprint
# =====================================================================

def create_optimizer_blueprint():
    """Create a Flask Blueprint for system optimizer API endpoints.

    Endpoints:
        GET  /api/system/health         - Current snapshot + health score
        GET  /api/system/optimizations  - Recent optimizations applied
        POST /api/system/optimize       - Trigger manual optimization check

    Returns:
        Flask Blueprint instance, or None if Flask is not available.
    """
    try:
        from flask import Blueprint, jsonify, request
    except ImportError:
        logger.debug("Flask not available -- optimizer blueprint not created")
        return None

    bp = Blueprint('compute_optimizer', __name__, url_prefix='/api/system')

    @bp.route('/health', methods=['GET'])
    def system_health():
        optimizer = get_optimizer()
        snap = optimizer.snapshot()
        return jsonify({
            'health_score': optimizer.get_health_score(snap),
            'snapshot': snap.to_dict(),
            'optimizations_applied': optimizer.get_stats()['optimizations_applied'],
        })

    @bp.route('/optimizations', methods=['GET'])
    def system_optimizations():
        optimizer = get_optimizer()
        stats = optimizer.get_stats()
        limit = request.args.get('limit', 20, type=int)
        stats['recent_history'] = stats['recent_history'][-limit:]
        return jsonify(stats)

    @bp.route('/optimize', methods=['POST'])
    def trigger_optimize():
        optimizer = get_optimizer()
        result = optimizer.trigger_optimization()
        return jsonify(result)

    return bp
