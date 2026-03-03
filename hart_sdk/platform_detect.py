"""
Platform Detection — Runtime capability and architecture detection.

Usage:
    from hart_sdk import detect_platform

    info = detect_platform()
    # {'arch': 'x86_64', 'os': 'linux', 'python_version': '3.11.4',
    #  'os_mode': 'app', 'gpu': {...}, 'capabilities': [...]}
"""

import platform
import sys
from typing import Any, Dict, List


# Architecture normalization
_ARCH_MAP = {
    'amd64': 'x86_64',
    'x86_64': 'x86_64',
    'arm64': 'aarch64',
    'aarch64': 'aarch64',
    'riscv64': 'riscv64',
    'armv7l': 'armv7l',
}


def detect_platform() -> Dict[str, Any]:
    """Detect current platform capabilities.

    Returns a dict with architecture, OS, GPU, and available capabilities.
    Works outside HART OS (returns what it can detect).
    """
    raw_arch = platform.machine().lower()
    arch = _ARCH_MAP.get(raw_arch, raw_arch)

    return {
        'arch': arch,
        'os': sys.platform,
        'python_version': platform.python_version(),
        'os_mode': _detect_os_mode(),
        'gpu': _detect_gpu(),
        'capabilities': get_capabilities(),
    }


def _detect_os_mode() -> str:
    """Detect if running as HART OS or app mode."""
    try:
        from core.port_registry import is_os_mode
        return 'os' if is_os_mode() else 'app'
    except ImportError:
        return 'unknown'


def _detect_gpu() -> Dict[str, Any]:
    """Detect GPU information."""
    import os
    if os.environ.get('HART_FORCE_CPU', '').lower() == 'true':
        return {'available': False, 'reason': 'HART_FORCE_CPU=true'}
    try:
        from integrations.service_tools.vram_manager import vram_manager
        if vram_manager:
            return vram_manager.detect_gpu() or {'available': False}
    except ImportError:
        pass
    return {'available': False, 'reason': 'vram_manager not available'}


def get_capabilities() -> List[str]:
    """Get list of available AI capability types.

    Checks what's actually available on this system.
    """
    import os
    if os.environ.get('HART_FORCE_CPU', '').lower() == 'true':
        return ['llm_cpu']

    caps = []
    try:
        from core.platform.registry import get_registry
        registry = get_registry()
        if registry.has('capability_router'):
            router = registry.get('capability_router')
            if router.health().get('has_model_registry'):
                caps.append('llm')
                caps.append('embedding')
                caps.append('code')
    except (ImportError, Exception):
        pass

    try:
        from integrations.service_tools.vram_manager import vram_manager
        if vram_manager:
            gpu = vram_manager.detect_gpu()
            if gpu and gpu.get('available', False):
                if 'vision' not in caps:
                    caps.append('vision')
    except (ImportError, Exception):
        pass

    return caps if caps else ['none']
