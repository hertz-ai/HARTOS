# Auto Evolve

Democratic thought experiment selection with autonomous iteration at hive scale.

## Purpose

Auto Evolve is the single entry point for autonomous system improvement. One button press triggers a full democratic cycle: eligible thought experiments are gathered, constitutionally filtered, democratically ranked, and the winners are dispatched to type-aware agent iteration loops. The system evolves through structured deliberation, not unilateral action.

## How It Works

```
                    ┌──────────────────┐
                    │  Auto Evolve     │
                    │  (single button) │
                    └────────┬─────────┘
                             │
              ┌──────────────▼──────────────┐
              │  1. GATHER                   │
              │  Pull eligible experiments   │
              │  (voting/evaluating status)  │
              └──────────────┬──────────────┘
                             │
              ┌──────────────▼──────────────┐
              │  2. CONSTITUTIONAL FILTER    │
              │  ConstitutionalFilter gate   │
              │  (hive_guardrails)           │
              └──────────────┬──────────────┘
                             │
              ┌──────────────▼──────────────┐
              │  3. DEMOCRATIC VOTE TALLY   │
              │  Human + agent votes        │
              │  Context-aware weighting    │
              └──────────────┬──────────────┘
                             │
              ┌──────────────▼──────────────┐
              │  4. SELECT top-N winners    │
              │  by approval score          │
              │  (above min threshold)      │
              └──────────────┬──────────────┘
                             │
              ┌──────────────▼──────────────┐
              │  5. DISPATCH to agent goals │
              │  Type-aware iteration loop  │
              │  via request_agent_eval()   │
              └──────────────┬──────────────┘
                             │
         ┌───────────────────┼───────────────────┐
         │                   │                   │
    ┌────▼─────┐       ┌────▼─────┐       ┌────▼──────┐
    │ software │       │tradition │       │physical_ai│
    │autoresrch│       │reason &  │       │observe &  │
    │edit→run→ │       │refine    │       │measure    │
    │score→keep│       │LLM loop  │       │visual ctx │
    └──────────┘       └──────────┘       └───────────┘
```

## Type-Aware Iteration

When an experiment reaches the "evaluating" phase, a **type-specific iteration recipe** is generated and passed to the agent goal. The agent's autogen conversation loop drives iteration -- not a hardcoded Python while loop.

| Experiment Type | Strategy | Tools Used | Max Iterations | Scoring |
|----------------|----------|-----------|----------------|---------|
| `software` | `autoresearch` | `launch_experiment_autoresearch`, `get_experiment_research_status` | 50 | Metric extraction (regex) |
| `traditional` | `reason_and_refine` | `iterate_hypothesis`, `score_hypothesis_result`, web search, `recall_memory` | 10 | LLM rubric |
| `physical_ai` | `observe_and_measure` | `iterate_hypothesis`, `Visual_Context_Camera`, `score_hypothesis_result` | 20 | LLM rubric |
| any new type | `reason_and_refine` (fallback) | `iterate_hypothesis`, `score_hypothesis_result` | 10 | LLM rubric |

### Iteration Recipe

The recipe is stored in the agent goal's `config_json.iteration_recipe`:

```json
{
  "strategy": "reason_and_refine",
  "description": "ITERATIVE THOUGHT EXPERIMENT\n\nHypothesis: ...\n\nLOOP PATTERN:\n1. iterate_hypothesis\n2. Research...\n3. score_hypothesis_result\n4. Repeat...",
  "tools": ["iterate_hypothesis", "score_hypothesis_result", "get_iteration_history"],
  "max_iterations": 10,
  "scoring": "llm_rubric"
}
```

## Generic Iteration Tools

Three tools available for ALL experiment types (not just software):

### `iterate_hypothesis(experiment_id, hypothesis, approach, evidence, iteration)`

Proposes and tracks a hypothesis iteration. Returns experiment context for the agent to evaluate. Respects owner pause -- if the experiment creator paused evolution, returns a stop signal.

### `score_hypothesis_result(experiment_id, iteration, score, reasoning, evidence_quality, clarity, feasibility, impact)`

Scores a hypothesis with a structured rubric. Returns:

- **Score record**: Overall score (-2 to +2) plus sub-scores (0-1 each)
- **Trend analysis**: best_score, improving, stagnant detection
- **Convergence advice**: `CONTINUE`, `CONVERGE` (3 same scores), `BUDGET` (10 iterations), `STRONG` (score >= 1.5)

Iteration history persisted at `agent_data/experiment_iterations/{experiment_id}.json`.

### `get_iteration_history(experiment_id, last_n)`

Returns past iterations with summary statistics (total, best, worst, avg, trend). Used by the agent to inform its next hypothesis refinement.

## Owner Pause/Resume

The experiment creator can pause and resume their experiment's iteration at any time:

```
POST /api/social/experiments/<id>/pause-evolve   { "user_id": "<creator_id>" }
POST /api/social/experiments/<id>/resume-evolve  { "user_id": "<creator_id>" }
```

- Only the creator (owner) can pause
- Only the user who paused can resume
- `iterate_hypothesis()` checks pause state before every iteration
- Paused experiments remain in `evaluating` status but stop iterating

## AutoResearch Engine (Software Type)

For `experiment_type='software'`, the autoresearch engine provides specialized code iteration:

```
1. BASELINE   -- run unmodified code, capture baseline metric
2. HYPOTHESIS -- LLM proposes code edit (AiderNativeBackend)
3. EXECUTE    -- apply edit, run experiment (subprocess + timeout)
4. SCORE      -- extract metric from output (regex patterns)
5. DECIDE     -- improved? keep (git commit). worse? revert (git checkout)
6. ITERATE    -- repeat until budget exhausted or max_iterations
7. REPORT     -- save to agent_data/autoresearch/{session_id}.json
```

### Budget Gating

- **Spark budget**: `spark_consumed + spark_per_iteration > spark_budget` stops the loop
- **Time budget**: Per-iteration timeout (`time_budget_s`, default 300s)
- **Iteration cap**: `max_iterations` (default 50)

### Hive Parallel Mode

When `hive_parallel=True`, multiple hypothesis variants run simultaneously across compute mesh peers:

1. Generate N diverse hypotheses
2. Dispatch each to a peer (encrypted via X25519)
3. Tournament selection picks the best result
4. Winning edit applied locally
5. Falls back to sequential if <2 peers available

## Evolution Stack Integration

AutoResearch feeds into the existing multi-level evolution stack:

```
AutoResearchEngine.run_loop()
    │
    ├─ _run_experiment()
    │   └─ BenchmarkTracker.record()              ← per-iteration metrics
    │
    ├─ _generate_and_apply_edit()
    │   └─ BenchmarkTracker.get_best_tool()       ← benchmark-informed context
    │
    ├─ _commit_improvement()
    │   ├─ CodingRecipeBridge.capture_edit_as_recipe_step()  ← recipe reuse
    │   └─ AgentBaselineService.capture_snapshot()            ← regression detection
    │
    └─ _save_report()
        └─ _export_learning_delta()               ← hive-wide federation
            └─ BenchmarkTracker.export_learning_delta()
                └─ FederatedAggregator picks up on next tick
                    └─ broadcast_delta() to peers
```

| Stack Layer | Component | What It Receives |
|-------------|-----------|-----------------|
| Per-task | BenchmarkTracker | `task_type='autoresearch'`, duration, success/fail |
| Per-agent | AgentBaselineService | Snapshot after each improvement (trigger: `autoresearch_improvement`) |
| Per-recipe | CodingRecipeBridge | Winning edits saved as replayable recipe steps |
| Hive-wide | FederatedAggregator | Learning delta with autoresearch session summary |
| RL feedback | WorldModelBridge | Experiment outcome when `decide()` records final decision |

## API Endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/social/experiments/auto-evolve` | Start democratic auto-evolve cycle |
| GET | `/api/social/experiments/auto-evolve/status` | Get current cycle status |
| POST | `/api/social/experiments/<id>/pause-evolve` | Owner pauses iteration |
| POST | `/api/social/experiments/<id>/resume-evolve` | Owner resumes iteration |
| POST | `/api/social/experiments/<id>/evaluate` | Trigger single experiment evaluation |

### Start Auto-Evolve

```json
POST /api/social/experiments/auto-evolve
{
  "user_id": "admin_1",
  "max_experiments": 5,
  "min_approval_score": 0.3
}

Response:
{
  "success": true,
  "session_id": "a1b2c3d4e5f6",
  "status": "selecting"
}
```

### Cycle Status

```json
GET /api/social/experiments/auto-evolve/status

Response:
{
  "session_id": "a1b2c3d4e5f6",
  "status": "running",
  "elapsed_s": 45.2,
  "candidates": 12,
  "filtered": 10,
  "selected": 3,
  "dispatched": 3,
  "experiments": [
    {
      "id": "exp_1",
      "title": "Optimize embedding pipeline",
      "type": "software",
      "approval_score": 1.8,
      "goal_id": "goal_abc",
      "status": "dispatched"
    }
  ]
}
```

## Agent Tools

| Tool | Tags | Purpose |
|------|------|---------|
| `start_auto_evolve` | `auto_evolve, thought_experiment` | Start democratic cycle |
| `get_auto_evolve_status` | `auto_evolve` | Poll cycle progress |
| `pause_evolve_experiment` | `auto_evolve, thought_experiment` | Owner pauses iteration |
| `resume_evolve_experiment` | `auto_evolve, thought_experiment` | Owner resumes iteration |
| `iterate_hypothesis` | `thought_experiment, iteration` | Propose hypothesis for any type |
| `score_hypothesis_result` | `thought_experiment, iteration` | Score with rubric + trend |
| `get_iteration_history` | `thought_experiment, iteration` | Past iterations + stats |
| `launch_experiment_autoresearch` | `thought_experiment, autoresearch` | Software-only code iteration |
| `get_experiment_research_status` | `thought_experiment, autoresearch` | Software iteration progress |

## EventBus Topics

| Topic | When |
|-------|------|
| `auto_evolve.dispatching` | Cycle selected winners, dispatching |
| `auto_evolve.started` | Experiments dispatched, cycle running |
| `auto_evolve.no_candidates` | No eligible experiments found |
| `auto_evolve.none_approved` | All candidates below approval threshold |
| `autoresearch.started` | Software code loop started |
| `autoresearch.baseline` | Baseline metric captured |
| `autoresearch.iteration` | Each iteration result |
| `autoresearch.completed` | Loop finished |
| `autoresearch.failed` | Loop error |

## Test Coverage

| Test File | Tests | Coverage |
|-----------|-------|----------|
| `tests/unit/test_auto_evolve.py` | 20 | Session state, singleton, pause/resume ownership, constitutional filter, vote ranking, tools |
| `tests/unit/test_autoresearch.py` | 53 | Session management, metric extraction, budget gating, type-aware recipes, iteration tools, benchmark integration |

## Source Files

| File | Purpose |
|------|---------|
| `integrations/agent_engine/auto_evolve.py` | AutoEvolveOrchestrator, pause/resume, AUTO_EVOLVE_TOOLS |
| `integrations/agent_engine/thought_experiment_tools.py` | 11 tools including generic iteration (THOUGHT_EXPERIMENT_TOOLS) |
| `integrations/coding_agent/autoevolve_code_tools.py` | AutoResearchEngine for software experiments (AUTORESEARCH_TOOLS) |
| `integrations/social/thought_experiment_service.py` | Lifecycle, `_build_iteration_recipe()`, `request_agent_evaluation()` |
| `integrations/social/api_thought_experiments.py` | Flask blueprint (17 endpoints including auto-evolve) |
| `integrations/agent_engine/goal_manager.py` | `goal_type='thought_experiment'` registration |
| `integrations/coding_agent/benchmark_tracker.py` | Per-task performance tracking |
| `integrations/agent_engine/agent_baseline_service.py` | Per-agent snapshots + regression detection |
| `integrations/agent_engine/federated_aggregator.py` | Hive-wide learning delta distribution |

## Related Docs

- [thought-experiments.md](thought-experiments.md) -- Lifecycle and voting
- [benchmark-tracking.md](benchmark-tracking.md) -- Evolution stack details
- [coding-agent.md](coding-agent.md) -- Tool backends
- [federation.md](federation.md) -- Hive-wide sync protocol
- [budget-gating.md](budget-gating.md) -- Spark budget system
