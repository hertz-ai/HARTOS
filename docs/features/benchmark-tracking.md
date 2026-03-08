# Benchmark & Baseline Tracking

HART OS tracks performance across multiple dimensions — agent quality, model latency, world model health, coding tool efficiency, and HevolveAI research benchmarks. Every upgrade is gated on benchmark comparison. Every agent snapshot is versioned for regression detection.

## Architecture

```
                    ┌─────────────────────────┐
                    │   BenchmarkRegistry     │
                    │   (7 built-in adapters)  │
                    └─────────┬───────────────┘
                              │
         ┌────────────────────┼────────────────────┐
         │                    │                    │
    Fast Tier            Heavy Tier          Dynamic Tier
    ┌─────────┐         ┌──────────┐       ┌──────────────┐
    │ModelReg │         │QuantiPhy │       │ Git-installed │
    │WorldMdl │         │Embodied  │       │   adapters    │
    │Regress  │         │QwenEnc   │       │              │
    │Guardrail│         └──────────┘       └──────────────┘
    └─────────┘          (GPU required)     (runtime)

         ┌─────────────────────────┐
         │  AgentBaselineService   │
         │  (per-agent snapshots)  │
         └─────────┬───────────────┘
                   │
         ┌─────────┼──────────┐
         │         │          │
    Recipe      Lightning   Trust/
    Metrics     Metrics     Evolution
    (actions,   (reward,    (score,
     duration,   trend,      generation,
     success)    errors)     specialization)

         ┌────────────────────────┐
         │  CodingBenchmarkTracker│
         │  (SQLite per-task)     │
         └────────────────────────┘
         (task_type, tool, model, time, success)
```

## Benchmark Registry

**File:** `integrations/agent_engine/benchmark_registry.py`

### Built-in Adapters

| Adapter | Tier | Metrics | Source |
|---------|------|---------|--------|
| **ModelRegistryAdapter** | fast | Per-model latency (ms), accuracy (score) | ModelRegistry |
| **WorldModelAdapter** | fast | Flush rate, correction density, hivemind queries | WorldModelBridge |
| **RegressionAdapter** | fast | Test pass rate (excluding `nested_task`) | pytest |
| **GuardrailAdapter** | fast | Guardrail integrity verified (bool) | hive_guardrails |
| **QuantiPhyAdapter** | heavy | Physics reasoning benchmark | HevolveAI (GPU, 4GB VRAM) |
| **EmbodiedValidationAdapter** | heavy | Performance, forgetting, memory benchmarks | HevolveAI (GPU, 2GB VRAM) |
| **QwenEncoderAdapter** | heavy | Encoder throughput (tokens/sec) | HevolveAI (GPU, 2GB VRAM) |

### HevolveAI Integration

The three heavy-tier adapters import directly from the `hevolveai` sibling package:

```python
# Conditional import — gracefully skips if hevolveai not installed
from hevolveai.tests.benchmarks.quantiphy_benchmark import QuantiPhyBenchmark
from hevolveai.embodied_ai.validation.benchmark import (
    PerformanceBenchmark, ForgettingBenchmark, MemoryBenchmark)
from hevolveai.embodied_ai.models.qwen_benchmark import benchmark_llamacpp
```

Uses `importlib.util.find_spec('hevolveai')` to check availability at runtime.

### Snapshot Storage

Snapshots stored at `agent_data/benchmarks/{version}.json`:

```json
{
  "version": "v2.1.0",
  "git_sha": "abc123",
  "captured_at": "2026-02-24T12:00:00",
  "tier": "fast",
  "metrics": {
    "gpt-4o_latency_ms": {"value": 850, "direction": "lower", "unit": "ms"},
    "gpt-4o_accuracy": {"value": 0.92, "direction": "higher", "unit": "score"},
    "flush_rate": {"value": 0.87, "direction": "higher", "unit": "ratio"},
    "test_pass_rate": {"value": 0.98, "direction": "higher", "unit": "ratio"},
    "guardrail_integrity": {"value": 1.0, "direction": "higher", "unit": "bool"}
  }
}
```

### Upgrade Safety Check

```python
registry.is_upgrade_safe(old_version, new_version) -> (bool, reason)
```

Compares all fast-tier metrics between versions. **5% regression threshold** — any metric degrading more than 5% blocks the upgrade:

- "higher" metrics (accuracy, pass rate): new < old * 0.95 = regression
- "lower" metrics (latency): new > old * 1.05 = regression

### Dynamic Benchmark Installation

```python
registry.discover_and_install(
    repo_url='https://github.com/org/benchmark',
    name='custom_benchmark',
    requires_gpu=True,
    min_vram_gb=4.0
)
```

Installs to `~/.hevolve/benchmarks/` via git clone + pip install. Coding agent at regional compute-heavy nodes can install dynamically.

### Core Methods

```python
registry.capture_snapshot(version, git_sha, tier='fast')  # 'fast' | 'heavy' | 'all'
registry.is_upgrade_safe(old_version, new_version)         # (bool, reason)
registry.get_latest_results()                              # For federation delta
registry.list_benchmarks()                                 # All registered adapters
registry.register_benchmark(adapter)                       # Custom adapter
```

## Agent Baseline Service

**File:** `integrations/agent_engine/agent_baseline_service.py` (665 lines)

### Purpose

Captures unified performance snapshots of agents at creation time and whenever recipe, prompt, or intelligence changes. Enables per-agent regression detection.

### Snapshot Structure

Stored at `agent_data/baselines/{prompt_id}_{flow_id}/v{N}.json`:

```json
{
  "version": 3,
  "trigger": "recipe_change",
  "timestamp": "2026-02-24T12:00:00",
  "user_id": "user_123",
  "recipe_metrics": {
    "action_count": 5,
    "total_expected_duration": 120,
    "success_rates": {"action_1": 0.95, "action_2": 0.88},
    "dead_ends": 1,
    "effective_fallbacks": 2
  },
  "lightning_metrics": {
    "avg_reward": 0.82,
    "total_reward": 41.0,
    "reward_trend": "improving",
    "execution_count": 50,
    "error_rate": 0.04,
    "avg_duration": 2.3
  },
  "benchmark_metrics": {
    "test_pass_rate": 0.98,
    "model_registry_accuracy": 0.91
  },
  "trust_evolution": {
    "composite_trust_score": 0.87,
    "generation": 3,
    "specialization_path": "coding/python",
    "evolution_xp": 1250
  }
}
```

### Triggers

| Trigger | When |
|---------|------|
| `creation` | Agent first created |
| `recipe_change` | Recipe file modified (debounced: skip if <60s since creation) |
| `prompt_change` | Prompt definition updated |
| `intelligence_change` | World model stats shift detected by AgentDaemon |

### Core Methods

```python
AgentBaselineService.capture_snapshot(prompt_id, flow_id, trigger, user_id, user_prompt)
AgentBaselineService.validate_against_baseline(prompt_id, flow_id)  # CI/CD gate
AgentBaselineService.compare_snapshots(prompt_id, flow_id, old_v, new_v)  # Delta
AgentBaselineService.compute_trend(prompt_id, flow_id)  # improving/declining/stable
AgentBaselineService.get_latest_snapshot(prompt_id, flow_id)
AgentBaselineService.list_snapshots(prompt_id, flow_id)
```

### Regression Detection

`validate_against_baseline()` checks:

- Recipe success rates per action: regression if <95% of baseline
- Benchmark pass rate: regression if <95% of baseline
- Returns: `{passed: bool, regressions: [list], baseline_version: int}`

### Trend Analysis

`compute_trend()` analyzes reward and duration trends across all snapshots:

```json
{
  "trend": "improving",
  "snapshot_count": 7,
  "reward_trend": "improving",
  "duration_trend": "stable"
}
```

## Coding Benchmark Tracker

**File:** `integrations/coding_agent/benchmark_tracker.py` (249 lines)

SQLite-backed performance tracking for coding agent tools, tasks, and models.

**Database:** `agent_data/coding_benchmarks.db`

### Tables

**`benchmarks`** — Individual task execution records:

| Column | Type | Purpose |
|--------|------|---------|
| task_type | TEXT | e.g., "code_review", "bug_fix" |
| tool_name | TEXT | e.g., "pylint", "ruff" |
| model_name | TEXT | e.g., "gpt-4o", "llama-3" |
| user_id | TEXT | Requesting user |
| completion_time_s | REAL | Execution duration |
| success | INTEGER | 1=pass, 0=fail |
| offloaded | INTEGER | 1=hive task |
| timestamp | TEXT | ISO-8601 |

**`hive_routing`** — Aggregated best-tool routing from hive peers.

### Core Methods

```python
tracker.record(task_type, tool_name, completion_time_s, success, model_name, user_id, offloaded)
tracker.get_best_tool(task_type)        # Local benchmark-based routing
tracker.get_hive_best_tool(task_type)   # Peer-aggregated routing
tracker.get_summary()                   # Dashboard data
tracker.export_learning_delta()         # Compact delta for hive
tracker.import_hive_delta(aggregated)   # Consume peer benchmarks
```

**Min samples:** 5 records required before a tool is considered "benchmarked".

### Hive-Wide Learning

The coding daemon exports benchmark deltas every 10 ticks (~5 min):

```python
# coding_daemon.py
if self._tick_count % 10 == 0:
    self._sync_benchmark_deltas()
```

FederatedAggregator picks up the delta and distributes to peers. Each peer imports the aggregated data for hive-wide tool routing intelligence.

## PR Review Service Integration

**File:** `integrations/agent_engine/pr_review_service.py`

Uses baseline validation as a **build breaker gate**:

```
PR Review Pipeline:
  1. Fetch PR diff stats
  2. Run pre-commit checks (ruff lint)
  3. Run test suite
  4. Validate baseline (no regression)    ← BUILD BREAKER
  5. Classify change complexity

Decision Matrix:
  Tests Pass + No Regression + Simple  → AUTO-APPROVE
  Tests Pass + No Regression + Complex → FLAG for steward
  Tests Pass + Regression              → AUTO-REJECT
  Tests Fail                           → AUTO-REJECT
```

## Agent Daemon Integration

**File:** `integrations/agent_engine/agent_daemon.py`

Background daemon periodically validates agent performance:

```python
# Every 2*remediate_every ticks
result = AgentBaselineService.validate_against_baseline(prompt_id, flow_id)
if result and not result.get('passed', True):
    capture_baseline_async(...)  # Auto-snapshot on regression
```

## Federation Integration

**File:** `integrations/agent_engine/federated_aggregator.py`

Benchmark results are synced across the hive via gossip protocol:

```python
def _get_benchmark_results(self) -> dict:
    """Pull latest benchmark results + coding agent deltas."""
    from integrations.coding_agent.benchmark_tracker import get_benchmark_tracker
    coding_delta = get_benchmark_tracker().export_learning_delta()
    results['coding_benchmarks'] = coding_delta.get('coding_benchmarks', {})
```

Aggregated data flows back to each node's tool router for hive-optimized task routing.

## Test Coverage

| Test File | Coverage |
|-----------|----------|
| `tests/unit/test_agent_baseline_service.py` (424 lines) | Snapshots, versioning, regression, trends |
| `tests/unit/test_federation_upgrade.py` | BenchmarkRegistry adapters, `is_upgrade_safe()` |
| `tests/unit/test_coding_tool_backends.py` | `get_best_tool()` with min samples |
