"""
Unified Agent Goal Engine - Dynamic Benchmark Registry

Benchmarks are adapters that wrap measurement suites. Built-in adapters
reuse existing HevolveAI code. Dynamic adapters are installed by the
coding agent at regional compute-heavy nodes via RuntimeToolManager pattern.

Snapshots stored at agent_data/benchmarks/{version}.json.
"""
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_social')

def _resolve_benchmark_dir():
    import sys as _sys
    db_path = os.environ.get('HEVOLVE_DB_PATH', '')
    if db_path and db_path != ':memory:' and os.path.isabs(db_path):
        return os.path.join(os.path.dirname(db_path), 'agent_data', 'benchmarks')
    if os.environ.get('NUNBA_BUNDLED') or getattr(_sys, 'frozen', False):
        return os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'agent_data', 'benchmarks')
    return os.path.join('agent_data', 'benchmarks')

BENCHMARK_DIR = _resolve_benchmark_dir()


class BenchmarkAdapter:
    """Base class for benchmark adapters."""

    name: str = ''
    source: str = 'builtin'  # 'builtin' | 'git' | 'pip'
    repo_url: str = ''
    requires_gpu: bool = False
    min_vram_gb: float = 0.0
    tier: str = 'fast'  # 'fast' | 'heavy'

    def run(self, api_url: str = '', **kwargs) -> Dict:
        """Run benchmark. Return {metrics: {name: {value, direction, unit}}}."""
        raise NotImplementedError

    def is_available(self) -> bool:
        """Check if dependencies are installed."""
        return True

    def install(self) -> bool:
        """Install dependencies. Return True on success."""
        return True


class ModelRegistryAdapter(BenchmarkAdapter):
    """Benchmark via ModelRegistry: per-model latency, accuracy, cost."""
    name = 'model_registry'
    tier = 'fast'

    def run(self, api_url: str = '', **kwargs) -> Dict:
        try:
            from .model_registry import ModelRegistry
            registry = ModelRegistry.get_instance()
            models = registry.list_models()
            metrics = {}
            for m in models:
                d = m.to_dict() if hasattr(m, 'to_dict') else m
                mid = d.get('model_id', 'unknown')
                metrics[f'{mid}_latency_ms'] = {
                    'value': d.get('avg_latency_ms', 0),
                    'direction': 'lower', 'unit': 'ms'}
                metrics[f'{mid}_accuracy'] = {
                    'value': d.get('accuracy_score', 0),
                    'direction': 'higher', 'unit': 'score'}
            return {'metrics': metrics}
        except Exception as e:
            return {'metrics': {}, 'error': str(e)}


class WorldModelAdapter(BenchmarkAdapter):
    """Benchmark via WorldModelBridge stats."""
    name = 'world_model'
    tier = 'fast'

    def run(self, api_url: str = '', **kwargs) -> Dict:
        try:
            from .world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()
            stats = bridge.get_stats()
            return {'metrics': {
                'flush_rate': {
                    'value': stats.get('total_flushed', 0) / max(1, stats.get('total_recorded', 1)),
                    'direction': 'higher', 'unit': 'ratio'},
                'correction_density': {
                    'value': stats.get('total_corrections', 0),
                    'direction': 'higher', 'unit': 'count'},
                'hivemind_queries': {
                    'value': stats.get('total_hivemind_queries', 0),
                    'direction': 'higher', 'unit': 'count'},
            }}
        except Exception as e:
            return {'metrics': {}, 'error': str(e)}


class RegressionAdapter(BenchmarkAdapter):
    """Run pytest regression as a benchmark."""
    name = 'regression'
    tier = 'fast'

    def run(self, api_url: str = '', **kwargs) -> Dict:
        try:
            python = os.environ.get(
                'HEVOLVE_PYTHON',
                os.path.join('venv310', 'Scripts', 'python.exe')
                if sys.platform == 'win32' else
                os.path.join('venv310', 'bin', 'python'))
            result = subprocess.run(
                [python, '-m', 'pytest', 'tests/', '-s',
                 '--ignore=tests/runtime_tests', '-q',
                 '--tb=no', '-k', 'not nested_task'],
                capture_output=True, text=True, timeout=600,
                cwd=os.environ.get('HEVOLVE_PROJECT_ROOT',
                                   os.path.dirname(os.path.dirname(
                                       os.path.dirname(__file__))))
            )
            # Parse pytest output for pass/fail counts
            output = result.stdout + result.stderr
            passed = failed = 0
            for line in output.split('\n'):
                if 'passed' in line:
                    parts = line.split()
                    for i, p in enumerate(parts):
                        if p == 'passed' and i > 0:
                            try:
                                passed = int(parts[i - 1])
                            except ValueError:
                                pass
                        if p == 'failed' and i > 0:
                            try:
                                failed = int(parts[i - 1])
                            except ValueError:
                                pass
            total = passed + failed
            return {'metrics': {
                'pass_rate': {
                    'value': passed / max(1, total),
                    'direction': 'higher', 'unit': 'ratio'},
                'fail_count': {
                    'value': failed,
                    'direction': 'lower', 'unit': 'count'},
            }}
        except Exception as e:
            return {'metrics': {}, 'error': str(e)}


class GuardrailAdapter(BenchmarkAdapter):
    """Verify guardrail integrity."""
    name = 'guardrail'
    tier = 'fast'

    def run(self, api_url: str = '', **kwargs) -> Dict:
        try:
            from security.hive_guardrails import (
                compute_guardrail_hash, verify_guardrail_integrity)
            hash_val = compute_guardrail_hash()
            integrity = verify_guardrail_integrity()
            return {'metrics': {
                'hash_match': {
                    'value': 1 if integrity else 0,
                    'direction': 'higher', 'unit': 'bool'},
                'integrity_verified': {
                    'value': 1 if integrity else 0,
                    'direction': 'higher', 'unit': 'bool'},
            }}
        except Exception as e:
            return {'metrics': {}, 'error': str(e)}


class QuantiPhyAdapter(BenchmarkAdapter):
    """QuantiPhy physics reasoning benchmark from HevolveAI."""
    name = 'quantiphy'
    source = 'builtin'
    requires_gpu = True
    min_vram_gb = 4.0
    tier = 'heavy'

    def is_available(self) -> bool:
        try:
            # Check if HevolveAI quantiphy benchmark exists
            import importlib.util
            spec = importlib.util.find_spec('hevolveai')
            return spec is not None
        except Exception:
            return False

    def run(self, api_url: str = '', **kwargs) -> Dict:
        try:
            from hevolveai.tests.benchmarks.quantiphy_benchmark import QuantiPhyBenchmark
            bench = QuantiPhyBenchmark(api_url=api_url or 'http://localhost:8000')
            results = bench.run_benchmark(
                phase='baseline',
                max_instances=kwargs.get('max_instances', 20))
            mra = results.get('mra', {})
            return {'metrics': {
                'mra_mean': {
                    'value': mra.get('mean', 0),
                    'direction': 'higher', 'unit': 'score'},
                'latency_p95_ms': {
                    'value': results.get('latency', {}).get('p95', 0),
                    'direction': 'lower', 'unit': 'ms'},
            }}
        except Exception as e:
            return {'metrics': {}, 'error': str(e)}


class EmbodiedValidationAdapter(BenchmarkAdapter):
    """Embodied AI validation benchmark from HevolveAI."""
    name = 'embodied_validation'
    source = 'builtin'
    requires_gpu = True
    min_vram_gb = 2.0
    tier = 'heavy'

    def is_available(self) -> bool:
        try:
            import importlib.util
            spec = importlib.util.find_spec('hevolveai')
            return spec is not None
        except Exception:
            return False

    def run(self, api_url: str = '', **kwargs) -> Dict:
        try:
            from hevolveai.embodied_ai.validation.benchmark import (
                PerformanceBenchmark, ForgettingBenchmark, MemoryBenchmark)
            # Run lightweight validation checks
            metrics = {}
            try:
                perf = PerformanceBenchmark()
                perf_result = perf.run()
                metrics['mean_latency_ms'] = {
                    'value': perf_result.get('mean_latency_ms', 0),
                    'direction': 'lower', 'unit': 'ms'}
            except Exception:
                pass
            try:
                mem = MemoryBenchmark()
                mem_result = mem.run()
                metrics['ram_mb'] = {
                    'value': mem_result.get('ram_mb', 0),
                    'direction': 'lower', 'unit': 'MB'}
            except Exception:
                pass
            return {'metrics': metrics}
        except Exception as e:
            return {'metrics': {}, 'error': str(e)}


class QwenEncoderAdapter(BenchmarkAdapter):
    """Qwen encoder throughput benchmark from HevolveAI."""
    name = 'qwen_encoder'
    source = 'builtin'
    requires_gpu = True
    min_vram_gb = 2.0
    tier = 'fast'

    def is_available(self) -> bool:
        try:
            import importlib.util
            spec = importlib.util.find_spec('hevolveai')
            return spec is not None
        except Exception:
            return False

    def run(self, api_url: str = '', **kwargs) -> Dict:
        try:
            from hevolveai.embodied_ai.models.qwen_benchmark import (
                benchmark_llamacpp)
            result = benchmark_llamacpp(
                server_url=api_url or f'http://localhost:{os.environ.get("LLAMA_CPP_PORT", "8080")}')
            return {'metrics': {
                'tokens_per_second': {
                    'value': result.get('tokens_per_second', 0),
                    'direction': 'higher', 'unit': 'tok/s'},
            }}
        except Exception as e:
            return {'metrics': {}, 'error': str(e)}


class DynamicBenchmarkAdapter(BenchmarkAdapter):
    """Adapter for dynamically installed benchmarks (git repos)."""

    def __init__(self, name: str, repo_url: str,
                 requires_gpu: bool = False, min_vram_gb: float = 0.0,
                 run_command: str = '', metrics_file: str = ''):
        self.name = name
        self.source = 'git'
        self.repo_url = repo_url
        self.requires_gpu = requires_gpu
        self.min_vram_gb = min_vram_gb
        self.tier = 'heavy'
        self._run_command = run_command
        self._metrics_file = metrics_file
        self._install_dir = os.path.join(
            os.path.expanduser('~'), '.hevolve', 'benchmarks', name)

    def is_available(self) -> bool:
        return os.path.isdir(self._install_dir)

    def install(self) -> bool:
        try:
            os.makedirs(os.path.dirname(self._install_dir), exist_ok=True)
            if not os.path.isdir(self._install_dir):
                subprocess.run(
                    ['git', 'clone', '--depth', '1', self.repo_url, self._install_dir],
                    check=True, timeout=120)
            # Install requirements if present
            req_file = os.path.join(self._install_dir, 'requirements.txt')
            if os.path.isfile(req_file):
                subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', '-r', req_file, '-q'],
                    timeout=300)
            return True
        except Exception as e:
            logger.debug(f"Benchmark install failed for {self.name}: {e}")
            return False

    def run(self, api_url: str = '', **kwargs) -> Dict:
        if not self.is_available():
            return {'metrics': {}, 'error': 'not installed'}
        try:
            cmd = self._run_command or f'{sys.executable} -m pytest --benchmark-json=results.json'
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=600, cwd=self._install_dir)
            # Try to parse metrics file
            mf = os.path.join(self._install_dir, self._metrics_file or 'results.json')
            if os.path.isfile(mf):
                with open(mf) as f:
                    return {'metrics': json.load(f)}
            return {'metrics': {'exit_code': {
                'value': result.returncode, 'direction': 'lower', 'unit': 'code'}}}
        except Exception as e:
            return {'metrics': {}, 'error': str(e)}


class BenchmarkRegistry:
    """Dynamic benchmark registry. Singleton."""

    def __init__(self):
        self._lock = threading.Lock()
        self._adapters: Dict[str, BenchmarkAdapter] = {}
        self._latest_results: Dict[str, dict] = {}
        self._register_builtins()
        os.makedirs(BENCHMARK_DIR, exist_ok=True)

    def _register_builtins(self):
        for adapter_cls in [
            ModelRegistryAdapter, WorldModelAdapter, RegressionAdapter,
            GuardrailAdapter, QuantiPhyAdapter, EmbodiedValidationAdapter,
            QwenEncoderAdapter,
        ]:
            adapter = adapter_cls()
            self._adapters[adapter.name] = adapter

    def register_benchmark(self, adapter: BenchmarkAdapter):
        """Register a new benchmark adapter. Idempotent."""
        with self._lock:
            self._adapters[adapter.name] = adapter

    def discover_and_install(self, repo_url: str, name: str,
                             requires_gpu: bool = False,
                             min_vram_gb: float = 0.0,
                             run_command: str = '',
                             metrics_file: str = '') -> bool:
        """Coding agent installs a dynamic benchmark from a git repo."""
        adapter = DynamicBenchmarkAdapter(
            name=name, repo_url=repo_url,
            requires_gpu=requires_gpu, min_vram_gb=min_vram_gb,
            run_command=run_command, metrics_file=metrics_file)
        if adapter.install():
            self.register_benchmark(adapter)
            return True
        return False

    def capture_snapshot(self, version: str, git_sha: str = '',
                         tier: str = 'fast') -> Dict:
        """Run benchmarks and store snapshot. tier='fast' or 'heavy' or 'all'."""
        snapshot = {
            'version': version,
            'git_sha': git_sha,
            'timestamp': time.time(),
            'tier': tier,
            'benchmarks': {},
        }

        # Check node capability for GPU benchmarks
        node_tier = 'standard'
        try:
            from security.system_requirements import get_tier_name
            node_tier = get_tier_name()
        except Exception:
            pass

        with self._lock:
            adapters = dict(self._adapters)

        for name, adapter in adapters.items():
            # Filter by tier
            if tier == 'fast' and adapter.tier != 'fast':
                continue
            if tier == 'heavy' and adapter.tier != 'heavy':
                continue
            # Skip GPU benchmarks on lite nodes
            if adapter.requires_gpu and node_tier in ('lite', 'minimal'):
                snapshot['benchmarks'][name] = {
                    'skipped': True, 'reason': f'requires GPU, node tier={node_tier}'}
                continue
            if not adapter.is_available():
                snapshot['benchmarks'][name] = {
                    'skipped': True, 'reason': 'not available'}
                continue
            try:
                result = adapter.run()
                snapshot['benchmarks'][name] = result
                with self._lock:
                    self._latest_results[name] = result
            except Exception as e:
                snapshot['benchmarks'][name] = {
                    'error': str(e)}

        # Persist
        fname = os.path.join(BENCHMARK_DIR, f'{version}.json')
        try:
            with open(fname, 'w') as f:
                json.dump(snapshot, f, indent=2)
        except Exception as e:
            logger.debug(f"Benchmark snapshot save failed: {e}")

        return snapshot

    def is_upgrade_safe(self, old_version: str, new_version: str) -> Tuple[bool, str]:
        """ALL fast-tier metrics must be >= old version."""
        old_file = os.path.join(BENCHMARK_DIR, f'{old_version}.json')
        new_file = os.path.join(BENCHMARK_DIR, f'{new_version}.json')

        if not os.path.isfile(old_file):
            return True, 'no baseline to compare'
        if not os.path.isfile(new_file):
            return False, 'new version snapshot missing'

        with open(old_file) as f:
            old = json.load(f)
        with open(new_file) as f:
            new = json.load(f)

        regressions = []
        for bench_name, old_result in old.get('benchmarks', {}).items():
            new_result = new.get('benchmarks', {}).get(bench_name, {})
            if old_result.get('skipped') or new_result.get('skipped'):
                continue
            old_metrics = old_result.get('metrics', {})
            new_metrics = new_result.get('metrics', {})
            for metric_name, old_m in old_metrics.items():
                new_m = new_metrics.get(metric_name)
                if not new_m or not isinstance(old_m, dict) or not isinstance(new_m, dict):
                    continue
                old_val = old_m.get('value', 0)
                new_val = new_m.get('value', 0)
                direction = old_m.get('direction', 'higher')
                if direction == 'higher' and new_val < old_val * 0.95:
                    regressions.append(
                        f"{bench_name}.{metric_name}: {old_val:.3f} → {new_val:.3f} (regression)")
                elif direction == 'lower' and new_val > old_val * 1.05:
                    regressions.append(
                        f"{bench_name}.{metric_name}: {old_val:.3f} → {new_val:.3f} (regression)")

        if regressions:
            return False, f"Regressions: {'; '.join(regressions)}"
        return True, 'all metrics pass'

    def get_latest_results(self) -> dict:
        """Get latest benchmark results (used by federation delta)."""
        with self._lock:
            return dict(self._latest_results)

    def list_benchmarks(self) -> List[Dict]:
        """List all registered benchmarks with status."""
        with self._lock:
            return [
                {
                    'name': name,
                    'source': adapter.source,
                    'tier': adapter.tier,
                    'requires_gpu': adapter.requires_gpu,
                    'available': adapter.is_available(),
                }
                for name, adapter in self._adapters.items()
            ]


# ─── Singleton ───
_registry = None
_registry_lock = threading.Lock()


def get_benchmark_registry() -> BenchmarkRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = BenchmarkRegistry()
    return _registry
