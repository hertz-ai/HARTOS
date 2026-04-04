"""
EfficiencyMatrix — continuous benchmarking for optimal model selection.

Tracks per-provider, per-model:
  - Tokens per second (throughput)
  - Time to first token (TTFT) latency
  - End-to-end latency
  - Quality score (from user feedback + automated eval)
  - Reliability (success rate)
  - Cost efficiency (quality per dollar)

The matrix runs benchmarks during idle time (via ResourceGovernor)
and also records live usage stats from every gateway call.

Persisted at ~/Documents/Nunba/data/efficiency_matrix.json.
Integrated with ResourceGovernor's proactive action stream.

The key formula:
  efficiency_score = (quality × speed × reliability) / cost
  → Higher is better. Used by registry.find_best() for 'balanced' strategy.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ModelBenchmark:
    """Benchmark results for a specific model on a specific provider."""
    provider_id: str
    model_id: str
    model_type: str = 'llm'

    # Throughput
    avg_tok_per_s: float = 0.0
    p50_tok_per_s: float = 0.0
    p95_tok_per_s: float = 0.0

    # Latency
    avg_ttft_ms: float = 0.0        # Time to first token
    avg_e2e_ms: float = 0.0         # End-to-end
    p95_e2e_ms: float = 0.0

    # Quality (0-1)
    quality_score: float = 0.5
    coherence: float = 0.5
    instruction_following: float = 0.5

    # Reliability
    success_rate: float = 1.0
    total_requests: int = 0
    failed_requests: int = 0

    # Cost
    avg_cost_per_request: float = 0.0
    cost_per_1k_output_tokens: float = 0.0

    # Computed
    efficiency_score: float = 0.0    # quality × speed × reliability / cost

    # Metadata
    last_benchmark: float = 0.0
    last_live_update: float = 0.0
    sample_count: int = 0

    def compute_efficiency(self):
        """Recalculate efficiency score."""
        speed = min(1.0, self.avg_tok_per_s / 100.0) if self.avg_tok_per_s > 0 else 0.3
        cost_factor = max(0.01, self.cost_per_1k_output_tokens) if self.cost_per_1k_output_tokens > 0 else 1.0
        self.efficiency_score = (
            self.quality_score * speed * self.success_rate
        ) / cost_factor


@dataclass
class BenchmarkTask:
    """A benchmark task to evaluate model capability."""
    id: str
    prompt: str
    model_type: str = 'llm'
    expected_keywords: List[str] = field(default_factory=list)
    max_tokens: int = 256
    category: str = 'general'       # general, reasoning, coding, creative


# Built-in benchmark tasks (lightweight — one request each)
_BENCHMARK_TASKS = [
    BenchmarkTask(
        id='general_1',
        prompt='Explain quantum computing in 3 sentences.',
        expected_keywords=['qubit', 'superposition', 'quantum'],
        category='general',
    ),
    BenchmarkTask(
        id='reasoning_1',
        prompt='If all roses are flowers and some flowers fade quickly, can we conclude all roses fade quickly? Explain.',
        expected_keywords=['no', 'some', 'logic', 'conclude'],
        category='reasoning',
    ),
    BenchmarkTask(
        id='coding_1',
        prompt='Write a Python function that checks if a string is a palindrome. Return only the function.',
        expected_keywords=['def', 'return', 'reverse', '[::-1]'],
        category='coding',
        max_tokens=200,
    ),
    BenchmarkTask(
        id='creative_1',
        prompt='Write a haiku about artificial intelligence.',
        expected_keywords=[],
        category='creative',
        max_tokens=100,
    ),
    BenchmarkTask(
        id='instruction_1',
        prompt='List exactly 5 prime numbers between 10 and 50. Output only the numbers, comma-separated.',
        expected_keywords=['11', '13', '17', '19', '23', '29', '31', '37', '41', '43', '47'],
        category='instruction_following',
        max_tokens=50,
    ),
]


class EfficiencyMatrix:
    """Continuous benchmarking system for provider/model selection."""

    def __init__(self, matrix_path: Optional[str] = None):
        try:
            from core.platform_paths import get_db_dir
            data_dir = Path(get_db_dir())
        except ImportError:
            data_dir = Path.home() / 'Documents' / 'Nunba' / 'data'
        data_dir.mkdir(parents=True, exist_ok=True)

        self._path = Path(matrix_path) if matrix_path else data_dir / 'efficiency_matrix.json'
        self._benchmarks: Dict[str, ModelBenchmark] = {}  # key = "provider_id:model_id"
        self._lock = threading.Lock()
        self._load()

    def _key(self, provider_id: str, model_id: str) -> str:
        return f"{provider_id}:{model_id}"

    def _load(self):
        if self._path.exists():
            try:
                with open(self._path, 'r') as f:
                    data = json.load(f)
                for k, v in data.items():
                    known = {fn.name for fn in ModelBenchmark.__dataclass_fields__.values()}
                    self._benchmarks[k] = ModelBenchmark(
                        **{fk: fv for fk, fv in v.items() if fk in known})
                logger.info("Efficiency matrix loaded: %d entries", len(self._benchmarks))
            except Exception as e:
                logger.warning("Failed to load efficiency matrix: %s", e)

    def save(self):
        with self._lock:
            data = {k: asdict(v) for k, v in self._benchmarks.items()}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("Failed to save efficiency matrix: %s", e)

    # ── Live updates (called by gateway after each request) ───────────

    def record_request(self, provider_id: str, model_id: str,
                       model_type: str = 'llm',
                       tok_per_s: float = 0, ttft_ms: float = 0,
                       e2e_ms: float = 0, cost_usd: float = 0,
                       output_tokens: int = 0, success: bool = True):
        """Record a live request result into the matrix."""
        key = self._key(provider_id, model_id)
        alpha = 0.1  # EMA smoothing

        with self._lock:
            bm = self._benchmarks.get(key)
            if not bm:
                bm = ModelBenchmark(
                    provider_id=provider_id, model_id=model_id,
                    model_type=model_type,
                )
                self._benchmarks[key] = bm

            bm.total_requests += 1
            if not success:
                bm.failed_requests += 1
            bm.success_rate = 1.0 - (bm.failed_requests / max(1, bm.total_requests))

            if tok_per_s > 0:
                bm.avg_tok_per_s = (bm.avg_tok_per_s * (1 - alpha) + tok_per_s * alpha
                                    if bm.avg_tok_per_s > 0 else tok_per_s)
            if ttft_ms > 0:
                bm.avg_ttft_ms = (bm.avg_ttft_ms * (1 - alpha) + ttft_ms * alpha
                                  if bm.avg_ttft_ms > 0 else ttft_ms)
            if e2e_ms > 0:
                bm.avg_e2e_ms = (bm.avg_e2e_ms * (1 - alpha) + e2e_ms * alpha
                                 if bm.avg_e2e_ms > 0 else e2e_ms)
            if cost_usd > 0:
                bm.avg_cost_per_request = (bm.avg_cost_per_request * (1 - alpha) + cost_usd * alpha
                                           if bm.avg_cost_per_request > 0 else cost_usd)
            if output_tokens > 0 and cost_usd > 0:
                cpt = (cost_usd / output_tokens) * 1000
                bm.cost_per_1k_output_tokens = (
                    bm.cost_per_1k_output_tokens * (1 - alpha) + cpt * alpha
                    if bm.cost_per_1k_output_tokens > 0 else cpt)

            bm.sample_count += 1
            bm.last_live_update = time.time()
            bm.compute_efficiency()

        # Periodic save (every 10 requests)
        if bm.total_requests % 10 == 0:
            self.save()

    # ── Benchmarking (run during idle via ResourceGovernor) ───────────

    def run_benchmark(self, provider_id: str = '', model_type: str = 'llm'):
        """Run lightweight benchmark against one or all providers.

        Called by ResourceGovernor during idle time.
        """
        from integrations.providers.registry import get_registry

        registry = get_registry()
        providers = ([registry.get(provider_id)] if provider_id
                     else registry.list_api_providers())

        for provider in providers:
            if not provider or not provider.has_api_key():
                continue

            for pm in provider.models.values():
                if pm.model_type != model_type or not pm.enabled:
                    continue

                # Skip if recently benchmarked (within 1 hour)
                key = self._key(provider.id, pm.model_id)
                bm = self._benchmarks.get(key)
                if bm and (time.time() - bm.last_benchmark) < 3600:
                    continue

                logger.info("Benchmarking %s on %s...", pm.model_id, provider.id)
                self._benchmark_model(provider, pm)

    def _benchmark_model(self, provider, provider_model):
        """Run benchmark tasks against a specific model."""
        from integrations.providers.gateway import get_gateway

        gw = get_gateway()
        results = []

        for task in _BENCHMARK_TASKS:
            if task.model_type != provider_model.model_type:
                continue

            try:
                t0 = time.time()
                result = gw.generate(
                    task.prompt,
                    model_type=task.model_type,
                    provider_id=provider.id,
                    model_id=provider_model.model_id,
                    max_tokens=task.max_tokens,
                    temperature=0.3,  # Low temp for consistent benchmarks
                )
                elapsed_ms = (time.time() - t0) * 1000

                if result.success:
                    # Score quality by checking expected keywords
                    quality = self._score_quality(result.content, task)
                    results.append({
                        'task': task.id,
                        'tok_per_s': result.tok_per_s,
                        'e2e_ms': elapsed_ms,
                        'cost': result.cost_usd,
                        'quality': quality,
                        'output_tokens': result.usage.get('output_tokens', 0),
                        'success': True,
                    })
                else:
                    results.append({
                        'task': task.id, 'success': False,
                        'error': result.error,
                    })
            except Exception as e:
                logger.debug("Benchmark task %s failed: %s", task.id, e)
                results.append({'task': task.id, 'success': False, 'error': str(e)})

        # Aggregate results into benchmark entry
        if results:
            self._aggregate_benchmark(provider.id, provider_model.model_id,
                                      provider_model.model_type, results)

    def _score_quality(self, content: str, task: BenchmarkTask) -> float:
        """Score response quality (0-1) based on expected keywords and length."""
        if not content:
            return 0.0

        score = 0.3  # Base score for any non-empty response

        # Keyword matching
        if task.expected_keywords:
            content_lower = content.lower()
            matches = sum(1 for kw in task.expected_keywords
                          if kw.lower() in content_lower)
            keyword_score = matches / len(task.expected_keywords)
            score += keyword_score * 0.5

        # Length appropriateness (penalize very short or very long)
        words = len(content.split())
        if 10 <= words <= task.max_tokens:
            score += 0.2
        elif words >= 5:
            score += 0.1

        return min(1.0, score)

    def _aggregate_benchmark(self, provider_id, model_id, model_type, results):
        """Aggregate benchmark task results into a single ModelBenchmark."""
        key = self._key(provider_id, model_id)
        successes = [r for r in results if r.get('success')]

        with self._lock:
            bm = self._benchmarks.get(key)
            if not bm:
                bm = ModelBenchmark(
                    provider_id=provider_id, model_id=model_id,
                    model_type=model_type,
                )
                self._benchmarks[key] = bm

            if successes:
                bm.avg_tok_per_s = sum(r['tok_per_s'] for r in successes) / len(successes)
                bm.avg_e2e_ms = sum(r['e2e_ms'] for r in successes) / len(successes)
                bm.quality_score = sum(r['quality'] for r in successes) / len(successes)
                total_cost = sum(r['cost'] for r in successes)
                total_tokens = sum(r.get('output_tokens', 0) for r in successes)
                if total_tokens > 0 and total_cost > 0:
                    bm.cost_per_1k_output_tokens = (total_cost / total_tokens) * 1000
                bm.avg_cost_per_request = total_cost / len(successes)

            bm.success_rate = len(successes) / len(results) if results else 0
            bm.last_benchmark = time.time()
            bm.compute_efficiency()

        logger.info("Benchmark complete: %s on %s — efficiency=%.3f, "
                     "tok/s=%.1f, quality=%.2f, success=%.0f%%",
                     model_id, provider_id, bm.efficiency_score,
                     bm.avg_tok_per_s, bm.quality_score,
                     bm.success_rate * 100)
        self.save()

    # ── Query API ─────────────────────────────────────────────────────

    def get_benchmark(self, provider_id: str, model_id: str) -> Optional[ModelBenchmark]:
        return self._benchmarks.get(self._key(provider_id, model_id))

    def get_leaderboard(self, model_type: str = 'llm',
                        sort_by: str = 'efficiency') -> List[ModelBenchmark]:
        """Return benchmarks sorted by efficiency, speed, quality, or cost."""
        entries = [bm for bm in self._benchmarks.values()
                   if bm.model_type == model_type and bm.total_requests > 0]

        key_map = {
            'efficiency': lambda b: b.efficiency_score,
            'speed': lambda b: b.avg_tok_per_s,
            'quality': lambda b: b.quality_score,
            'cost': lambda b: -b.cost_per_1k_output_tokens,  # Lower cost = better
            'reliability': lambda b: b.success_rate,
        }
        entries.sort(key=key_map.get(sort_by, key_map['efficiency']), reverse=True)
        return entries

    def get_matrix_summary(self) -> Dict[str, Any]:
        """Return a summary for dashboards."""
        return {
            'total_entries': len(self._benchmarks),
            'total_benchmark_requests': sum(
                bm.total_requests for bm in self._benchmarks.values()),
            'by_type': {
                mt: len([bm for bm in self._benchmarks.values()
                         if bm.model_type == mt])
                for mt in set(bm.model_type for bm in self._benchmarks.values())
            },
            'top_efficient': [
                {'provider': bm.provider_id, 'model': bm.model_id,
                 'efficiency': round(bm.efficiency_score, 3)}
                for bm in sorted(self._benchmarks.values(),
                                 key=lambda b: b.efficiency_score, reverse=True)[:5]
            ],
        }


# ═══════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════

_matrix: Optional[EfficiencyMatrix] = None
_matrix_lock = threading.Lock()


def get_matrix() -> EfficiencyMatrix:
    global _matrix
    if _matrix is None:
        with _matrix_lock:
            if _matrix is None:
                _matrix = EfficiencyMatrix()
    return _matrix
