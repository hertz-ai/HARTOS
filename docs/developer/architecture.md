# System Architecture

HART OS (Hevolve Agentic Runtime) is a multi-agent platform built on Flask, AutoGen, and LangChain. It creates, trains, and reuses autonomous AI agents via the Recipe Pattern.

## Core Pipeline

```
CREATE Mode: User Input --> Decompose --> Execute Actions --> Save Recipe
REUSE Mode:  User Input --> Load Recipe --> Execute Steps --> Output (90% faster)
```

## Module Map

### Entry Point

| File | Purpose |
|------|---------|
| `langchain_gpt_api.py` | Flask application, all HTTP routes, Waitress server on port 6777 |

### Recipe Pipeline

| File | Purpose |
|------|---------|
| `create_recipe.py` | Agent creation, action execution, recipe generation |
| `reuse_recipe.py` | Recipe reuse, trained agent execution |
| `helper.py` | Action class, JSON utilities, tool handlers |
| `lifecycle_hooks.py` | ActionState machine, FlowState, ledger sync |
| `helper_ledger.py` | SmartLedger factory functions |
| `agent_ledger.py` | SmartLedger core: Task, TaskType, TaskStatus |

### Agent Engine (`integrations/agent_engine/`)

| File | Purpose |
|------|---------|
| `goal_manager.py` | Goal creation, lifecycle management |
| `dispatch.py` | Goal decomposition, distributed dispatch |
| `parallel_dispatch.py` | Parallel task decomposition with SmartLedger |
| `speculative_dispatcher.py` | Fast response + background expert execution |
| `revenue_aggregator.py` | Revenue streams, 90/9/1 split, settlement |
| `budget_gate.py` | LLM cost estimation, Spark budgeting |
| `compute_config.py` | Compute policy resolution (env > DB > defaults) |
| `compute_borrowing.py` | Compute escrow service |
| `federated_aggregator.py` | Federated learning delta aggregation |
| `model_registry.py` | Model catalog, energy tracking |

### Social Platform (`integrations/social/`)

| File | Purpose |
|------|---------|
| `models.py` | SQLAlchemy ORM (60+ tables), db_session() |
| `api.py` | 82+ REST endpoints via `social_bp` blueprint |
| `services.py` | NotificationService, business logic |
| `peer_discovery.py` | Gossip protocol, bandwidth profiles |
| `hosting_reward_service.py` | Hosting rewards, contribution scoring, compute stats |
| `rate_limiter.py` | Token bucket rate limiting |

### Security (`security/`)

| File | Purpose |
|------|---------|
| `master_key.py` | Ed25519 trust anchor (AI exclusion zone) |
| `hive_guardrails.py` | 10 structurally immutable guardrail classes |
| `key_delegation.py` | 3-tier certificate chain (central > regional > local) |
| `runtime_monitor.py` | Background tamper detection daemon |
| `node_watchdog.py` | Heartbeat protocol, frozen-thread auto-restart |
| `system_requirements.py` | NodeTierLevel capability detection |

### Service Tools (`integrations/service_tools/`)

| File | Purpose |
|------|---------|
| `vram_manager.py` | GPU detection, VRAM tracking, allocation |
| `runtime_manager.py` | Media tool lifecycle (Whisper, LTX2, MiniCPM) |
| `model_lifecycle.py` | Dynamic model load/unload/offload |

### Other Integrations

| Directory | Purpose |
|-----------|---------|
| `integrations/coding_agent/` | Coding tool orchestrator (KiloCode, Claude Code) |
| `integrations/vision/` | Vision sidecar (MiniCPM + embodied AI) |
| `integrations/channels/` | 30+ channel adapters (Discord, Telegram, Slack, Matrix) |
| `integrations/ap2/` | Agent Protocol 2 (e-commerce, payments) |
| `integrations/expert_agents/` | 96 specialized agents network |
| `integrations/internal_comm/` | A2A communication, task delegation |
| `integrations/mcp/` | Model Context Protocol servers |
| `integrations/google_a2a/` | Dynamic agent registry, A2A protocol |

## Database

SQLite at `agent_data/hevolve_database.db` with WAL mode for concurrent access. See [schema.md](schema.md) for table details.

## See Also

- [patterns.md](patterns.md) -- Key code patterns
- [security.md](security.md) -- Security model
- [../architecture/overview.md](../architecture/overview.md) -- High-level diagram
