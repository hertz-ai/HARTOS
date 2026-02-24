# Agent Engine API

The agent engine manages goals, task dispatch, speculative execution, and revenue tracking.

## Goal Management

Goals are submitted via `GoalManager.create_goal()` and dispatched to the CREATE/REUSE pipeline. The dispatch system decomposes goals into sub-tasks and distributes them across peer nodes.

### Key Internal Functions

| Function | Module | Description |
|----------|--------|-------------|
| `submit_goal()` | `dispatch.py` | Submit goal for distributed execution |
| `_decompose_goal()` | `dispatch.py` | Break goal into sub-tasks |
| `decompose_goal_to_ledger()` | `parallel_dispatch.py` | Decompose with SmartLedger tracking |
| `GoalManager.create_goal()` | `goal_manager.py` | Create a new agent goal |

## Speculative Execution

When `speculative: true` is passed to `/chat`, the speculative dispatcher returns a fast response immediately and schedules a background expert execution.

## Revenue Dashboard

### GET /api/revenue/dashboard

Returns revenue streams, trading P&L, and compute borrowing status.

```json
{
  "revenue": {
    "period_days": 30,
    "api_revenue": 150.0,
    "ad_revenue": 75.0,
    "hosting_payouts": 20.0,
    "total_gross": 225.0,
    "user_pool_share": 202.5,
    "infra_pool_share": 20.25,
    "central_share": 2.25
  },
  "trading": {
    "portfolios": [],
    "total_pnl": 0.0
  },
  "compute_borrowing": {
    "active_escrows": 0,
    "total_spark_escrowed": 0
  }
}
```

## Tools API

### GET /api/tools/status

Status of all runtime media tools (Whisper, LTX2, MiniCPM, etc.).

### POST /api/tools/{tool_name}/setup

Download, start, and register a runtime tool.

### POST /api/tools/{tool_name}/start | /stop | /unload

Lifecycle management for individual tools.

### GET /api/tools/vram

VRAM usage dashboard from VRAMManager.

### GET /api/tools/lifecycle

Model lifecycle dashboard: loaded models, priorities, VRAM pressure, hive hints.

### POST /api/tools/lifecycle/{model_name}/priority

Set model priority (admin override). Body: `{"priority": "warm"}`.

### POST /api/tools/lifecycle/{model_name}/offload

Trigger GPU-to-CPU offload for a model.

### GET /api/system/pressure

Real-time system pressure: VRAM, RAM, CPU, disk, throttle factor.

## Coding Agent

### GET /coding/tools

List installed coding tools and capabilities.

### POST /coding/execute

Execute a coding task. Body: `{task, task_type?, preferred_tool?, model?}`.

### GET /coding/benchmarks

Coding tool benchmark dashboard.

### POST /coding/install

Install a coding tool. Body: `{"tool_name": "kilocode"}`.

## Skills API

### GET /api/skills/list

List all registered skills.

### POST /api/skills/ingest

Ingest a new skill definition.

### POST /api/skills/discover/local | /discover/github

Discover skills from local filesystem or GitHub.

### GET/DELETE /api/skills/{skill_name}

Get or remove a specific skill.

## See Also

- [core.md](core.md) -- Core chat endpoint
- [revenue.md](revenue.md) -- Revenue split details
- [settings.md](settings.md) -- Compute configuration
