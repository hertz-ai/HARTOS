# Architecture Overview

HART OS (Hevolve Hive Agentic Runtime) is crowdsourced compute infrastructure for autonomous Hive AI Training -a distributed multi-agent platform where no single entity monopolizes AI. The diagram below shows the high-level component relationships.

## System Diagram

```
                            ┌─────────────────────────┐
                            │     External Clients     │
                            │  (Discord, Telegram,     │
                            │   Slack, Matrix, React)  │
                            └───────────┬─────────────┘
                                        │
                                        ▼
                            ┌─────────────────────────┐
                            │   Flask Server (:6777)   │
                            │   hart_intelligence_entry.py   │
                            │                         │
                            │  /chat  /status  /a2a   │
                            │  /api/settings/compute  │
                            │  /api/revenue/dashboard │
                            │  /social/*  /coding/*   │
                            └──┬───┬───┬───┬───┬──────┘
                               │   │   │   │   │
              ┌────────────────┘   │   │   │   └────────────────┐
              ▼                    ▼   │   ▼                    ▼
    ┌──────────────────┐  ┌────────┴───┴────────┐  ┌──────────────────┐
    │  Recipe Pipeline  │  │   Agent Engine      │  │  Social Platform  │
    │                  │  │                     │  │                  │
    │  create_recipe   │  │  goal_manager       │  │  82+ endpoints   │
    │  reuse_recipe    │  │  dispatch           │  │  communities     │
    │  helper          │  │  speculative_disp   │  │  posts/comments  │
    │  lifecycle_hooks │  │  revenue_aggregator │  │  karma/gamify    │
    └──────┬───────────┘  │  budget_gate        │  │  encounters      │
           │              │  federated_agg      │  └──────┬───────────┘
           │              └──────────┬──────────┘         │
           │                         │                     │
           └────────────┬────────────┘                     │
                        ▼                                  │
              ┌──────────────────┐                         │
              │    SmartLedger    │◄────────────────────────┘
              │   agent_ledger   │
              │                  │
              │  JSON persistence│
              │  Task tracking   │
              │  Cross-session   │
              └──────────────────┘

              ┌──────────────────┐     ┌──────────────────┐
              │  Security Layer   │     │  Service Tools    │
              │                  │     │                  │
              │  master_key      │     │  vram_manager    │
              │  hive_guardrails │     │  runtime_manager │
              │  key_delegation  │     │  model_lifecycle │
              │  runtime_monitor │     │  whisper_tool    │
              │  node_watchdog   │     └──────────────────┘
              └──────────────────┘

              ┌──────────────────┐     ┌──────────────────┐
              │  Peer Network     │     │    Database       │
              │                  │     │                  │
              │  gossip_protocol │     │  SQLite + WAL    │
              │  peer_discovery  │     │  60+ tables      │
              │  federation_sync │     │  hevolve_db.db   │
              └──────────────────┘     └──────────────────┘
```

## Request Flow

1. Client sends POST to `/chat` with `user_id`, `prompt_id`, `prompt`
2. Flask server applies rate limiting, guardrail filtering, secret redaction
3. Budget gate estimates LLM cost
4. If `create_agent=true` or no existing recipe: **CREATE mode**
   - Decompose prompt into flows and actions
   - Execute each action via LLM/tools
   - Save recipe for future reuse
5. If recipe exists: **REUSE mode**
   - Load saved recipe
   - Execute steps without LLM (90% faster)
6. ActionState machine tracks each action's lifecycle
7. SmartLedger persists state for cross-session recovery
8. Response returned to client

## Network Topology

```
Central (hevolve.ai)
    │
    ├── Regional Host (us-east)
    │       ├── Local Node (Nunba)
    │       ├── Local Node (Nunba)
    │       └── Local Node (Nunba)
    │
    ├── Regional Host (eu-west)
    │       ├── Local Node (Nunba)
    │       └── Local Node (Nunba)
    │
    └── Flat Nodes (standalone)
```

Nodes discover each other via gossip protocol. Certificates are signed by parent tier. All messages are Ed25519-signed.

## See Also

- [task-delegation.md](task-delegation.md) -- Task decomposition flow
- [smart-ledger.md](smart-ledger.md) -- SmartLedger persistence
- [federation-protocol.md](federation-protocol.md) -- Gossip protocol details
