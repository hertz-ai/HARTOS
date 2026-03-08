# Agent Engine

Unified agent goal engine that orchestrates autonomous task execution across the platform.

## Core Components

| Component | Role |
|-----------|------|
| **GoalManager** | Creates and manages goals; assigns Spark budgets and deadlines. |
| **AgentDaemon** | Background loop that picks up pending goals, decomposes them into sub-tasks, and drives execution. |
| **SpeculativeDispatcher** | Runs budget-gated LLM calls, choosing the cheapest viable model and enforcing Spark limits before every invocation. |

## Goal Types

- **marketing** -- Content generation, social media campaigns, SEO tasks.
- **coding** -- Code generation, refactoring, bug fixing via the coding agent pipeline.
- **trading** -- Market analysis and paper/live trade execution (requires constitutional vote for live).

## Goal Lifecycle

```
pending --> active --> completed
                  \--> failed
```

1. **pending** -- Goal created by GoalManager, waiting for AgentDaemon pickup.
2. **active** -- AgentDaemon decomposes the goal and SpeculativeDispatcher begins executing sub-tasks.
3. **completed** -- All sub-tasks finished successfully; results persisted to the agent ledger.
4. **failed** -- A sub-task exceeded its budget or hit an unrecoverable error; the goal is marked failed with a reason.

## Budget Integration

Every LLM call passes through `pre_dispatch_budget_gate()` before execution. If the goal's remaining Spark budget is insufficient, the call is rejected and the goal may be paused or failed. See [budget-gating.md](budget-gating.md) for details.

## Source Files

- `integrations/agent_engine/goal_manager.py`
- `integrations/agent_engine/agent_daemon.py`
- `integrations/agent_engine/speculative_dispatcher.py`
