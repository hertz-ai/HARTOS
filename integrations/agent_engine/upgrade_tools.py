"""
Unified Agent Goal Engine - Upgrade AutoGen Tools

10 tools for the upgrade goal type. Follows ip_protection_tools.py pattern.
"""


def check_upgrade_status() -> dict:
    """Check current upgrade pipeline status and detect new versions."""
    try:
        from .upgrade_orchestrator import get_upgrade_orchestrator
        orch = get_upgrade_orchestrator()
        status = orch.get_status()
        new_version = orch.check_for_new_version()
        return {
            'success': True,
            'pipeline': status,
            'new_version': new_version,
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


def capture_benchmark(version: str = '', git_sha: str = '',
                      tier: str = 'fast') -> dict:
    """Capture a benchmark snapshot for the given version.

    Args:
        version: Version tag (auto-detected if empty)
        git_sha: Git commit SHA
        tier: 'fast' (upgrade gate) or 'heavy' (milestone) or 'all'
    """
    try:
        from .benchmark_registry import get_benchmark_registry
        registry = get_benchmark_registry()
        if not version:
            from .upgrade_orchestrator import get_upgrade_orchestrator
            version = get_upgrade_orchestrator()._detect_version()
        snapshot = registry.capture_snapshot(version, git_sha, tier=tier)
        return {'success': True, 'snapshot': snapshot}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def compare_benchmarks(old_version: str, new_version: str) -> dict:
    """Compare benchmarks between two versions. Reports regressions."""
    try:
        from .benchmark_registry import get_benchmark_registry
        registry = get_benchmark_registry()
        safe, reason = registry.is_upgrade_safe(old_version, new_version)
        return {'success': True, 'safe': safe, 'reason': reason}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def start_upgrade(new_version: str, git_sha: str = '') -> dict:
    """Start the 7-stage upgrade pipeline."""
    try:
        from .upgrade_orchestrator import get_upgrade_orchestrator
        return get_upgrade_orchestrator().start_upgrade(new_version, git_sha)
    except Exception as e:
        return {'success': False, 'error': str(e)}


def advance_upgrade_pipeline() -> dict:
    """Execute the next stage of the upgrade pipeline."""
    try:
        from .upgrade_orchestrator import get_upgrade_orchestrator
        return get_upgrade_orchestrator().advance_pipeline()
    except Exception as e:
        return {'success': False, 'error': str(e)}


def check_canary_health() -> dict:
    """Check health of canary nodes during deployment."""
    try:
        from .upgrade_orchestrator import get_upgrade_orchestrator
        return {
            'success': True,
            **get_upgrade_orchestrator().check_canary_health_status(),
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


def rollback_upgrade(reason: str = '') -> dict:
    """Safely rollback the upgrade at any stage."""
    try:
        from .upgrade_orchestrator import get_upgrade_orchestrator
        return get_upgrade_orchestrator().rollback(reason)
    except Exception as e:
        return {'success': False, 'error': str(e)}


def get_benchmark_history() -> dict:
    """Get all stored benchmark snapshots."""
    try:
        import os
        import json
        from .benchmark_registry import BENCHMARK_DIR
        snapshots = []
        if os.path.isdir(BENCHMARK_DIR):
            for fname in sorted(os.listdir(BENCHMARK_DIR)):
                if fname.endswith('.json'):
                    fpath = os.path.join(BENCHMARK_DIR, fname)
                    try:
                        with open(fpath) as f:
                            data = json.load(f)
                        snapshots.append({
                            'version': data.get('version', fname.replace('.json', '')),
                            'timestamp': data.get('timestamp', 0),
                            'tier': data.get('tier', 'unknown'),
                            'benchmark_count': len(data.get('benchmarks', {})),
                        })
                    except Exception:
                        pass
        return {'success': True, 'snapshots': snapshots}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def register_benchmark(name: str, repo_url: str,
                       requires_gpu: bool = False,
                       min_vram_gb: float = 0.0,
                       run_command: str = '',
                       metrics_file: str = '') -> dict:
    """Coding agent dynamically installs and registers a benchmark from a git repo."""
    try:
        from .benchmark_registry import get_benchmark_registry
        registry = get_benchmark_registry()
        installed = registry.discover_and_install(
            repo_url=repo_url, name=name,
            requires_gpu=requires_gpu, min_vram_gb=min_vram_gb,
            run_command=run_command, metrics_file=metrics_file)
        return {
            'success': installed,
            'name': name,
            'installed': installed,
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


def list_benchmarks() -> dict:
    """List all registered benchmarks with availability status."""
    try:
        from .benchmark_registry import get_benchmark_registry
        return {
            'success': True,
            'benchmarks': get_benchmark_registry().list_benchmarks(),
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


# Tool descriptors for AutoGen registration
UPGRADE_TOOLS = [
    {'name': 'check_upgrade_status',
     'description': 'Check upgrade pipeline status and detect new versions.',
     'function': check_upgrade_status},
    {'name': 'capture_benchmark',
     'description': 'Capture benchmark snapshot for a version.',
     'function': capture_benchmark},
    {'name': 'compare_benchmarks',
     'description': 'Compare benchmarks between two versions.',
     'function': compare_benchmarks},
    {'name': 'start_upgrade',
     'description': 'Start the 7-stage upgrade pipeline.',
     'function': start_upgrade},
    {'name': 'advance_upgrade_pipeline',
     'description': 'Execute the next pipeline stage.',
     'function': advance_upgrade_pipeline},
    {'name': 'check_canary_health',
     'description': 'Check canary node health during deployment.',
     'function': check_canary_health},
    {'name': 'rollback_upgrade',
     'description': 'Safely rollback upgrade at any stage.',
     'function': rollback_upgrade},
    {'name': 'get_benchmark_history',
     'description': 'Get all stored benchmark snapshots.',
     'function': get_benchmark_history},
    {'name': 'register_benchmark',
     'description': 'Dynamically install a benchmark from a git repo.',
     'function': register_benchmark},
    {'name': 'list_benchmarks',
     'description': 'List all registered benchmarks.',
     'function': list_benchmarks},
]
