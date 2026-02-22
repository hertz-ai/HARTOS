"""
System Requirements - Hardware detection, tier classification, and adaptive feature gating.

The HART OS equilibrium layer.  Runs early in the boot sequence to detect
actual hardware capabilities and auto-configure features to match what this
node can sustain.

Philosophy:
    Every node is net-positive.  A Raspberry Pi Zero running gossip matters.
    A 64-core GPU server generating video matters.  The system finds equilibrium:
    each node auto-adapts to what it can sustain, and the network as a whole is
    always net positive.  No node is punished for being small.  No node is
    penalized for being powerful.  Every drop counts.

Contribution Tiers (what you CAN contribute, not what you're excluded from):

    EMBEDDED      -  Any device that can run Python - sensors, GPIO, serial, MQTT bridge
    OBSERVER      -  < 2 cores / < 4 GB - gossip-only, audit witness, Flask server
    LITE          -  2 cores,  4 GB RAM,   1 GB disk - chat + gossip + audit
    STANDARD      -  4 cores,  8 GB RAM,  10 GB disk - + TTS, Whisper, agents
    FULL          -  8 cores, 16 GB RAM,  50 GB disk, 8 GB VRAM - + video, media, local 7B LLM
    COMPUTE_HOST  - 16 cores, 32 GB RAM, 100 GB disk, 12 GB VRAM - regional host, local 13B+ LLM

Mathematical Derivation (from vram_manager.py VRAM_BUDGETS + model disk sizes):

    EMBEDDED:  Python(0.05) + gossip(0.05) + adapters(0.02) = ~120 MB → no floor
    OBSERVER:  OS(1) + Flask(0.5) + gossip(0.2) = 1.7 GB RAM → floor 2 GB
    LITE:      OS(1) + Flask(0.5) + cloud_chat(0.5) + relay(0.5) = 3 GB → 4 GB
    STANDARD:  OS(1) + Flask(0.5) + Whisper_CPU(0.5) + TTS_CPU(2)
               + agents(1.5) + gossip(0.5) = 6.5 GB → 8 GB
               Disk: code(2) + whisper_base(0.15) + TTS(3) + recipes(2) = 7.15 GB → 10 GB
    FULL:      OS(1) + Flask(0.5) + Ollama_7B(5) + agents(1.5)
               + gossip(0.5) + GPU_overhead(2) = 10.5 GB → 16 GB
               Disk: code(2) + Ollama_7B(4) + MiniCPM(4) + LTX-2(27)
               + TTS(3) + Whisper(3) + cache(5) = 48 GB → 50 GB
               VRAM: Wan2GP(8) OR MiniCPM(6) + Whisper(2) → 8 GB
    COMPUTE_HOST: OS(1) + Flask(0.5) + Ollama_13B(9) + agents(2)
               + peer_serving(3) + gossip(0.5) = 16 GB → 32 GB
               Disk: all models(48) + Ollama_13B(8) + serving_cache(20)
               + logs(5) = 81 GB → 100 GB
               VRAM: 13B on GPU OR multiple tools concurrent → 12 GB

Usage:
    from security.system_requirements import run_system_check, get_capabilities
    caps = run_system_check()  # Called once at boot
    caps.tier                  # NodeTierLevel.STANDARD
    caps.enabled_features      # ['agent_engine', 'coding_agent', 'tts', 'whisper']
"""

import logging
import os
import platform
import shutil
import socket
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_security')

# Allow tier override for testing / dev deployments
FORCE_TIER_ENV = 'HEVOLVE_FORCE_TIER'


# ═══════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════

class NodeTierLevel(Enum):
    """Contribution tier - what this node can offer the network."""
    EMBEDDED = "embedded"           # Any device - sensors, GPIO, serial, MQTT bridge
    OBSERVER = "observer"           # Below lite - still gossips, still audits, Flask
    LITE = "lite"                   # Basic chat + gossip + audit + storage relay
    STANDARD = "standard"           # + TTS, Whisper, coding agent, goal engine
    FULL = "full"                   # + Video gen, media agent, full model registry
    COMPUTE_HOST = "compute_host"   # Can serve as regional host for other nodes


# Ordered from highest to lowest for classification
_TIER_ORDER = [
    NodeTierLevel.COMPUTE_HOST,
    NodeTierLevel.FULL,
    NodeTierLevel.STANDARD,
    NodeTierLevel.LITE,
    NodeTierLevel.OBSERVER,
    NodeTierLevel.EMBEDDED,
]

# Numeric rank for comparison (higher = more capable)
_TIER_RANK = {t: i for i, t in enumerate(reversed(_TIER_ORDER))}


@dataclass
class TierRequirement:
    """Hardware thresholds for a contribution tier."""
    tier: NodeTierLevel
    min_cpu_cores: int
    min_ram_gb: float
    min_disk_gb: float
    min_gpu_vram_gb: float = 0.0


@dataclass
class HardwareProfile:
    """Detected hardware capabilities of this node."""
    cpu_cores: int = 0
    cpu_model: str = ''
    ram_gb: float = 0.0
    disk_free_gb: float = 0.0
    disk_total_gb: float = 0.0
    gpu_name: Optional[str] = None
    gpu_vram_gb: float = 0.0
    cuda_available: bool = False
    os_platform: str = ''
    python_version: str = ''
    network_reachable: bool = False
    # Embedded / hardware I/O detection
    is_read_only_fs: bool = False
    has_gpio: bool = False
    has_serial: bool = False
    has_camera_hw: bool = False
    has_imu: bool = False
    has_gps: bool = False
    has_lidar: bool = False
    detected_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            'cpu_cores': self.cpu_cores,
            'cpu_model': self.cpu_model,
            'ram_gb': round(self.ram_gb, 2),
            'disk_free_gb': round(self.disk_free_gb, 2),
            'disk_total_gb': round(self.disk_total_gb, 2),
            'gpu_name': self.gpu_name,
            'gpu_vram_gb': round(self.gpu_vram_gb, 2),
            'cuda_available': self.cuda_available,
            'os_platform': self.os_platform,
            'python_version': self.python_version,
            'network_reachable': self.network_reachable,
            'is_read_only_fs': self.is_read_only_fs,
            'has_gpio': self.has_gpio,
            'has_serial': self.has_serial,
            'has_camera_hw': self.has_camera_hw,
            'has_imu': self.has_imu,
            'has_gps': self.has_gps,
            'has_lidar': self.has_lidar,
        }


@dataclass
class NodeCapabilities:
    """Resolved capabilities: tier + hardware + enabled features."""
    tier: NodeTierLevel
    hardware: HardwareProfile
    enabled_features: List[str]
    disabled_features: Dict[str, str]   # feature → reason
    env_vars_set: Dict[str, str]        # env var → value that was set
    detected_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            'tier': self.tier.value,
            'hardware': self.hardware.to_dict(),
            'enabled_features': self.enabled_features,
            'disabled_features': self.disabled_features,
            'env_vars_set': self.env_vars_set,
        }


# ═══════════════════════════════════════════════════════════════
# Model resource table (source of truth for tier derivation)
# ═══════════════════════════════════════════════════════════════
# Each entry: (min_vram_gb, model_disk_gb, ram_on_cpu_gb)
# From vram_manager.py VRAM_BUDGETS + measured model sizes.

MODEL_RESOURCE_TABLE = {
    'whisper_base':    (0.0,  0.15,  0.5),   # CPU-safe, 140MB model
    'whisper_large':   (2.0,  3.0,   0.0),   # GPU only
    'tts_audio_suite': (4.0,  3.0,   2.0),   # 4GB VRAM or 2GB CPU
    'acestep_music':   (6.0,  6.0,   0.0),   # GPU only
    'minicpm_vision':  (6.0,  4.0,   0.0),   # GPU only
    'ltx2_video':      (6.0,  27.0,  0.0),   # 27GB disk (fp8 weights)
    'wan2gp_video':    (8.0,  8.0,   0.0),   # 8GB VRAM minimum
    'ollama_7b_q4':    (4.5,  4.0,   5.0),   # 7B quantised, CPU or GPU
    'ollama_13b_q4':   (8.0,  8.0,   9.0),   # 13B quantised
}


# ═══════════════════════════════════════════════════════════════
# Tier requirements (checked highest → lowest)
# Derived deterministically from MODEL_RESOURCE_TABLE above.
# ═══════════════════════════════════════════════════════════════

TIER_REQUIREMENTS: List[TierRequirement] = [
    TierRequirement(NodeTierLevel.COMPUTE_HOST, 16, 32.0, 100.0, 12.0),
    TierRequirement(NodeTierLevel.FULL,          8, 16.0,  20.0,  8.0),
    TierRequirement(NodeTierLevel.STANDARD,      4,  8.0,   2.0,  0.0),
    TierRequirement(NodeTierLevel.LITE,          2,  4.0,   1.0,  0.0),
    TierRequirement(NodeTierLevel.OBSERVER,      1,  2.0,   0.0,  0.0),
    # EMBEDDED has no requirements - it's the floor. Any device that runs Python.
]


# ═══════════════════════════════════════════════════════════════
# Feature-to-tier mapping
# ═══════════════════════════════════════════════════════════════

# (minimum_tier, env_var_name)
FEATURE_TIER_MAP: Dict[str, Tuple[NodeTierLevel, str]] = {
    # Embedded tier - any device that runs Python
    'gossip':               (NodeTierLevel.EMBEDDED,  'HEVOLVE_GOSSIP_ENABLED'),
    'sensor_bridge':        (NodeTierLevel.EMBEDDED,  'HEVOLVE_SENSOR_BRIDGE_ENABLED'),
    'sensor_fusion':        (NodeTierLevel.EMBEDDED,  'HEVOLVE_SENSOR_FUSION_ENABLED'),
    'protocol_adapter':     (NodeTierLevel.EMBEDDED,  'HEVOLVE_PROTOCOL_ADAPTER_ENABLED'),
    # Observer tier - minimal server
    'flask_server':         (NodeTierLevel.OBSERVER,  'HEVOLVE_FLASK_ENABLED'),
    # Lite tier - cloud-backed chat
    'vision_lightweight':   (NodeTierLevel.LITE,      'HEVOLVE_VISION_LITE_ENABLED'),
    # Standard tier - full agent capabilities
    'agent_engine':         (NodeTierLevel.STANDARD,  'HEVOLVE_AGENT_ENGINE_ENABLED'),
    'coding_agent':         (NodeTierLevel.STANDARD,  'HEVOLVE_CODING_AGENT_ENABLED'),
    'tts':                  (NodeTierLevel.STANDARD,  'HEVOLVE_TTS_ENABLED'),
    'whisper':              (NodeTierLevel.STANDARD,  'HEVOLVE_WHISPER_ENABLED'),
    # Full tier - GPU workloads
    'video_gen':            (NodeTierLevel.FULL,      'HEVOLVE_VIDEO_GEN_ENABLED'),
    'media_agent':          (NodeTierLevel.FULL,      'HEVOLVE_MEDIA_AGENT_ENABLED'),
    'speculative_dispatch': (NodeTierLevel.FULL,      'HEVOLVE_SPECULATIVE_ENABLED'),
    'local_llm':            (NodeTierLevel.FULL,      'HEVOLVE_LOCAL_LLM_ENABLED'),
    # Compute host tier - regional hosting
    'local_llm_large':      (NodeTierLevel.COMPUTE_HOST, 'HEVOLVE_LOCAL_LLM_LARGE_ENABLED'),
    'regional_host':        (NodeTierLevel.COMPUTE_HOST, 'HEVOLVE_REGIONAL_HOST_ELIGIBLE'),
}


# ═══════════════════════════════════════════════════════════════
# Hardware detection
# ═══════════════════════════════════════════════════════════════

def detect_hardware() -> HardwareProfile:
    """Probe CPU, RAM, disk, GPU, and network.

    Uses psutil for RAM (with platform-specific fallback).
    Reuses VRAMManager for GPU detection (no duplicate logic).
    """
    hw = HardwareProfile()
    hw.os_platform = platform.system()
    hw.python_version = platform.python_version()

    # CPU
    hw.cpu_cores = os.cpu_count() or 1
    try:
        hw.cpu_model = platform.processor() or ''
    except Exception:
        hw.cpu_model = ''

    # RAM
    hw.ram_gb = _detect_ram_gb()

    # Disk (check the code root or home directory)
    hw.disk_free_gb, hw.disk_total_gb = _detect_disk_gb()

    # GPU (reuse existing VRAMManager)
    try:
        from integrations.service_tools.vram_manager import vram_manager
        gpu_info = vram_manager.detect_gpu()
        hw.cuda_available = gpu_info.get('cuda_available', False)
        hw.gpu_vram_gb = gpu_info.get('total_gb', 0.0)
        hw.gpu_name = gpu_info.get('name')
    except Exception:
        pass

    # Network
    hw.network_reachable = check_network_connectivity()

    # Embedded / hardware I/O
    hw.is_read_only_fs = _detect_read_only_fs()
    hw.has_gpio = _detect_gpio()
    hw.has_serial = _detect_serial()
    hw.has_camera_hw = _detect_camera_hw()
    hw.has_imu = _detect_imu()
    hw.has_gps = _detect_gps()
    hw.has_lidar = _detect_lidar()

    hw.detected_at = time.time()
    return hw


def _detect_ram_gb() -> float:
    """Detect total RAM in GB. Uses psutil with platform fallback."""
    # Try psutil first (most reliable cross-platform)
    try:
        import psutil
        return round(psutil.virtual_memory().total / (1024 ** 3), 2)
    except ImportError:
        pass

    # Fallback: platform-specific
    system = platform.system()
    try:
        if system == 'Linux':
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        kb = int(line.split()[1])
                        return round(kb / (1024 ** 2), 2)
        elif system == 'Windows':
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return round(stat.ullTotalPhys / (1024 ** 3), 2)
        elif system == 'Darwin':
            import subprocess
            result = subprocess.run(
                ['sysctl', '-n', 'hw.memsize'],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return round(int(result.stdout.strip()) / (1024 ** 3), 2)
    except Exception as e:
        logger.debug(f"RAM detection fallback failed: {e}")

    # Last resort: assume 4 GB (conservative, won't over-promise)
    return 4.0


def _detect_disk_gb() -> Tuple[float, float]:
    """Detect free and total disk space in GB for the code root."""
    try:
        code_root = os.environ.get('HEVOLVE_CODE_ROOT',
                                   os.path.dirname(os.path.dirname(
                                       os.path.abspath(__file__))))
        usage = shutil.disk_usage(code_root)
        free_gb = round(usage.free / (1024 ** 3), 2)
        total_gb = round(usage.total / (1024 ** 3), 2)
        return free_gb, total_gb
    except Exception:
        return 0.0, 0.0


def _detect_read_only_fs() -> bool:
    """Detect if the filesystem is read-only (ROM, SD card in read mode).

    Uses user-writable Nunba data dir (~/Documents/Nunba/data/) first,
    then system temp dir as fallback. Never writes to the app install dir
    (e.g. C:\\Program Files) which can hang on Windows due to Defender/UAC.
    """
    try:
        import tempfile
        # Prefer the Nunba data directory (always user-writable)
        user_data = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data')
        if os.path.isdir(user_data):
            test_dir = user_data
        else:
            # Fall back to system temp dir (guaranteed writable)
            test_dir = tempfile.gettempdir()
        fd, path = tempfile.mkstemp(dir=test_dir, prefix='.hevolve_ro_test_')
        os.close(fd)
        os.unlink(path)
        return False
    except (OSError, IOError):
        return True
    except Exception:
        return False  # If we can't determine, assume writable


def _detect_gpio() -> bool:
    """Detect GPIO availability (Raspberry Pi, embedded Linux boards)."""
    # Check for gpiod (modern Linux GPIO)
    try:
        import importlib
        importlib.import_module('gpiod')
        return True
    except ImportError:
        pass
    # Check for RPi.GPIO (Raspberry Pi specific)
    try:
        import importlib
        importlib.import_module('RPi.GPIO')
        return True
    except ImportError:
        pass
    # Check for sysfs GPIO (Linux)
    if os.path.isdir('/sys/class/gpio'):
        return True
    return False


def _detect_serial() -> bool:
    """Detect serial port availability (USB-to-serial, UART)."""
    try:
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        return len(ports) > 0
    except ImportError:
        pass
    # Fallback: check for common Linux serial devices
    for dev in ['/dev/ttyUSB0', '/dev/ttyACM0', '/dev/ttyS0', '/dev/ttyAMA0']:
        if os.path.exists(dev):
            return True
    return False


def _detect_camera_hw() -> bool:
    """Detect camera hardware (USB webcam, CSI camera, V4L2)."""
    # Check for V4L2 video devices (Linux)
    if os.path.exists('/dev/video0'):
        return True
    # Check for Raspberry Pi camera via vcgencmd
    try:
        import subprocess
        result = subprocess.run(
            ['vcgencmd', 'get_camera'], capture_output=True, text=True, timeout=3)
        if result.returncode == 0 and 'detected=1' in result.stdout:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return False


def _detect_imu() -> bool:
    """Detect IMU hardware (I2C accelerometer/gyroscope)."""
    # Check for I2C bus (common on embedded Linux)
    for bus in range(4):
        if os.path.exists(f'/dev/i2c-{bus}'):
            return True
    # Check for common IMU sysfs entries
    for path in ['/sys/bus/iio/devices/iio:device0',
                 '/sys/class/misc/accel', '/sys/class/misc/gyro']:
        if os.path.exists(path):
            return True
    return False


def _detect_gps() -> bool:
    """Detect GPS hardware (serial GPS, gpsd)."""
    # Check for gpsd socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(('127.0.0.1', 2947))
        s.close()
        return True
    except (OSError, ConnectionRefusedError):
        pass
    # Check for common GPS serial devices
    for dev in ['/dev/ttyGPS0', '/dev/ttyGPS', '/dev/serial/by-id/*GPS*']:
        if os.path.exists(dev):
            return True
    return False


def _detect_lidar() -> bool:
    """Detect LiDAR hardware (USB LiDAR, ROS topics)."""
    # Check for common USB LiDAR devices
    for dev in ['/dev/ttyUSB0', '/dev/rplidar']:
        if os.path.exists(dev):
            # Could be LiDAR or other serial device - best effort
            pass
    # Check for ROS LiDAR topics (if rclpy available)
    try:
        ros_topics_env = os.environ.get('HEVOLVE_ROS_TOPICS', '')
        if 'scan' in ros_topics_env.lower() or 'lidar' in ros_topics_env.lower():
            return True
    except Exception:
        pass
    return False


def check_network_connectivity(timeout: float = 5.0) -> bool:
    """Quick TCP connectivity check."""
    host = os.environ.get('HEVOLVE_CONNECTIVITY_HOST', '8.8.8.8')
    port = int(os.environ.get('HEVOLVE_CONNECTIVITY_PORT', '443'))
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except (socket.timeout, socket.error, OSError):
        return False


# ═══════════════════════════════════════════════════════════════
# Tier classification
# ═══════════════════════════════════════════════════════════════

def classify_tier(hw: HardwareProfile) -> NodeTierLevel:
    """Determine the highest tier this hardware qualifies for.

    Iterates from COMPUTE_HOST down to OBSERVER. If none match, returns EMBEDDED.
    EMBEDDED is never excluded - every node contributes. A Raspberry Pi Zero
    running gossip + sensor bridge matters.
    """
    # Check for forced tier override
    force_tier = os.environ.get(FORCE_TIER_ENV, '').lower()
    if force_tier:
        for tier in NodeTierLevel:
            if tier.value == force_tier:
                logger.info(f"Tier forced to {tier.value} via {FORCE_TIER_ENV}")
                return tier

    for req in TIER_REQUIREMENTS:
        if (hw.cpu_cores >= req.min_cpu_cores and
                hw.ram_gb >= req.min_ram_gb and
                hw.disk_free_gb >= req.min_disk_gb and
                hw.gpu_vram_gb >= req.min_gpu_vram_gb):
            return req.tier

    # Below all thresholds - embedded mode. Sensors, GPIO, gossip relay.
    # Still counts. Still matters. Every drop.
    return NodeTierLevel.EMBEDDED


# ═══════════════════════════════════════════════════════════════
# Feature resolution
# ═══════════════════════════════════════════════════════════════

def resolve_features(
    tier: NodeTierLevel,
    hw: HardwareProfile,
) -> Tuple[List[str], Dict[str, str]]:
    """Determine which features are enabled/disabled for this tier.

    Returns (enabled_features, disabled_features_with_reasons).
    """
    tier_rank = _TIER_RANK[tier]
    enabled = []
    disabled = {}

    for feature, (min_tier, env_var) in FEATURE_TIER_MAP.items():
        min_rank = _TIER_RANK[min_tier]
        if tier_rank >= min_rank:
            enabled.append(feature)
        else:
            disabled[feature] = (
                f"Requires {min_tier.value} tier "
                f"(node is {tier.value})"
            )

    return enabled, disabled


def apply_feature_gates(
    enabled: List[str],
    disabled: Dict[str, str],
) -> Dict[str, str]:
    """Set environment variables for features based on tier.

    Rule: We NEVER override a user's explicit env var.
    If the user set HEVOLVE_AGENT_ENGINE_ENABLED=true on a Lite node,
    we log a warning but respect their choice.  We inform, not dictate.
    """
    env_set = {}

    for feature, (_, env_var) in FEATURE_TIER_MAP.items():
        existing = os.environ.get(env_var)

        if feature in enabled:
            # Feature should be enabled
            if existing is None:
                os.environ[env_var] = 'true'
                env_set[env_var] = 'true'
            # If already set (by user), leave it alone
        else:
            # Feature should be disabled for this tier
            if existing is None:
                os.environ[env_var] = 'false'
                env_set[env_var] = 'false'
            elif existing.lower() == 'true':
                # User explicitly enabled a feature above their tier
                logger.warning(
                    f"Feature '{feature}' is enabled via {env_var} but "
                    f"hardware may not support it. "
                    f"Reason: {disabled.get(feature, 'unknown')}. "
                    f"Respecting user override."
                )
                # DON'T override - user knows their hardware

    return env_set


# ═══════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════

_capabilities: Optional[NodeCapabilities] = None
_lock = threading.Lock()


def run_system_check() -> NodeCapabilities:
    """Full system check: detect → classify → resolve → gate.

    This is the HART OS boot probe.  Called once from init_social().
    Thread-safe.  Result is cached for the lifetime of the process.
    """
    global _capabilities

    with _lock:
        if _capabilities is not None:
            return _capabilities

        t0 = time.time()

        # Step 1: Detect hardware
        hw = detect_hardware()

        # Step 2: Classify contribution tier
        tier = classify_tier(hw)

        # Step 3: Resolve features
        enabled, disabled = resolve_features(tier, hw)

        # Step 4: Apply feature gates (set env vars)
        env_set = apply_feature_gates(enabled, disabled)

        caps = NodeCapabilities(
            tier=tier,
            hardware=hw,
            enabled_features=enabled,
            disabled_features=disabled,
            env_vars_set=env_set,
            detected_at=time.time(),
        )

        elapsed = round(time.time() - t0, 2)
        logger.info(
            f"HART OS equilibrium: tier={tier.value}, "
            f"cpu={hw.cpu_cores}, ram={hw.ram_gb}GB, "
            f"disk={hw.disk_free_gb}GB, gpu_vram={hw.gpu_vram_gb}GB, "
            f"enabled={len(enabled)}, disabled={len(disabled)}, "
            f"detection={elapsed}s"
        )

        _capabilities = caps
        return caps


def get_capabilities() -> Optional[NodeCapabilities]:
    """Get cached capabilities, or None if run_system_check() hasn't been called."""
    return _capabilities


def get_tier() -> NodeTierLevel:
    """Get current tier. Returns EMBEDDED if not yet detected."""
    if _capabilities:
        return _capabilities.tier
    return NodeTierLevel.EMBEDDED


def get_tier_name() -> str:
    """Get current tier as string. Convenience for gossip/API."""
    return get_tier().value


def reset_for_testing():
    """Reset cached state. For tests only."""
    global _capabilities
    with _lock:
        _capabilities = None
