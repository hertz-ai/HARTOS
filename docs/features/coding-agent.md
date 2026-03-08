# Coding Agent (Idle Compute)

Dispatches coding tasks to the CREATE/REUSE pipeline during node idle time.

## How It Works

When a node detects idle compute capacity, the coding agent picks up pending coding goals from the network and executes them through the standard CREATE/REUSE pipeline. This turns unused compute into productive work for the network.

## Tool Backends

The orchestrator selects the best backend for each task:

| Backend | License | Best For |
|---------|---------|----------|
| **KiloCode** | Apache 2.0 | General-purpose code generation; open-source friendly. |
| **Claude Code** | Proprietary | Complex reasoning and multi-file refactoring. |
| **OpenCode** | MIT | Lightweight tasks; fully open-source. |

The orchestrator considers task complexity, required context window, and the node's compute policy when choosing a backend. If the compute policy is `local_only`, only backends that can run locally are eligible.

## Task Flow

1. **Idle detection** -- The node's resource monitor detects idle CPU/GPU capacity.
2. **Task pickup** -- The coding agent queries for pending coding goals.
3. **Backend selection** -- The orchestrator picks the best tool backend.
4. **Execution** -- The task is dispatched to the CREATE/REUSE pipeline.
5. **Result delivery** -- Output is written back to the goal's ledger entry.

## Metered API Costs

If the coding agent uses a metered backend (e.g., Claude Code) for a hive task, the cost is recorded in MeteredAPIUsage with `task_source=idle` and compensated via `settle_metered_api_costs()`. See [metered-api-recovery.md](metered-api-recovery.md).

## Source Files

- `integrations/coding_agent/`
- `integrations/agent_engine/speculative_dispatcher.py`
