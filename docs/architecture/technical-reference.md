# HART OS Technical Reference

> Complete architecture documentation for open-sourcing the best intelligence humans can ever make.
> Every subsystem, mechanism, protocol, pattern, and configuration — nothing left out.

---

## 1. Ecosystem Overview

HART OS is one project in a 5-project ecosystem. Each project has a distinct role:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        FRONTENDS (Thin Clients)                        │
│                                                                        │
│  Nunba (Desktop)          Hevolva (Mobile)          Hevolve (Cloud)    │
│  PyWebView + React SPA    React Native + Android    React Web App      │
│  Bundled via pip/OS        Native Activities         Hosted centrally   │
│  Port 6778 (LiquidUI)     Google Play / APK         hevolve.ai         │
└────────────────────────────────┬───────────────────────────────────────┘
                                 │ REST API (port 6777)
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    HARTOS (This Repository)                            │
│                                                                        │
│  Agentic Intelligence Layer Which Enables Hive Learning                │
│                                                                        │
│  Deployment modes:                                                     │
│    pip install hart-backend   (standalone Python)                      │
│    docker compose up          (containerized)                          │
│    NixOS ISO/install          (full operating system)                  │
│                                                                        │
│  Network tiers:                                                        │
│    FLAT      (home device, 10 peer links)                              │
│    REGIONAL  (GPU hub, 50 peer links, certificate authority)           │
│    CENTRAL   (hevolve.ai, 200 peer links, telemetry aggregator)       │
└────────────────────────────────┬───────────────────────────────────────┘
                                 │ In-process / HTTP (port 8000)
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    HevolveAI (Sibling Repository)                      │
│                                                                        │
│  Hive Intelligence Which Evolves AI Natively                           │
│                                                                        │
│  Owns ALL machine learning:                                            │
│    Hebbian learning, Bayesian inference, gradient computation          │
│    RALT distribution, world model, biometric ML                        │
│    Continual learning, embodied AI, tensor fusion                      │
│                                                                        │
│  HARTOS sends experience traces + outcomes → HevolveAI learns          │
│  HevolveAI sends skills + models → HARTOS orchestrates                 │
│                                                                        │
│  Source protected: compiled binary (.so/.dll), Ed25519-signed,          │
│  manifest-verified, symbol-obfuscated. Falls back to HTTP if absent.   │
└─────────────────────────────────────────────────────────────────────────┘
```

**CRITICAL RULE**: No ML/neural network code in HARTOS. HARTOS = agentic orchestration only. All learning lives in HevolveAI.

### Project Identity

- **HART OS** = **H**evolve **H**ive **A**gentic **R**un**t**ime
- **Core innovation**: Recipe Pattern — learn task execution once (CREATE mode), replay efficiently (REUSE mode) without repeated LLM calls
- **Foundational principle**: Humans are always in control. Every engineering decision makes centralized control structurally impossible, not just policy-prohibited.
- **Revenue model**: 90% to contributors, 9% infrastructure, 1% central

---

## 2. Layered Architecture

```
Layer 4: HART AI Runtime     agents, goals, recipes, federation, hive intelligence
Layer 3: LiquidUI Glass Shell   55 panels, MD3 design system, themes, WebKit renderer
Layer 2: HART OS APIs        Python shell_*_apis.py routes, Flask endpoints
Layer 1: NixOS + Linux       48 NixOS modules, systemd, kernel config
Layer 0: Hardware             x86, ARM, RISC-V, PinePhone, Raspberry Pi
```

Each OS capability maps to: NixOS module (Layer 1) + Python API (Layer 2) + LiquidUI panel (Layer 3) + optional AI goal type (Layer 4).

### Deployment Modes

| Mode | Command | What You Get |
|------|---------|-------------|
| **Standalone** | `python langchain_gpt_api.py` | Flask on port 6777, agents, tools, social, federation |
| **Bundled (pip)** | `pip install hart-backend` | Same as standalone, importable as library |
| **Docker** | `docker compose up` | Containerized with Redis, Crossbar, workers |
| **OS (NixOS)** | `nixos-rebuild switch` | Full OS: GNOME + LiquidUI + PipeWire + agents + everything |

### Network Tiers

| Tier | Max Links | Role | Certificate |
|------|-----------|------|-------------|
| **Flat** | 10 | Home device, edge node | Signed by regional |
| **Regional** | 50 | GPU hub, relay, gossip aggregator | Signed by central |
| **Central** | 200 | hevolve.ai, telemetry, kill switch | Master key holder |

All tiers participate fully in hive — budget limits connections, NOT capabilities.

---

## 3. Entry Points & Servers

| Entry Point | File | Port | Purpose |
|-------------|------|------|---------|
| Backend API | `langchain_gpt_api.py` | 6777 (app) / 677 (OS) | Flask + Waitress, 430+ REST endpoints |
| LiquidUI Shell | `integrations/agent_engine/liquid_ui_service.py` | 6778 | Desktop shell, WebKit renderer, 63 endpoints |
| CLI | `hart_cli.py` | N/A | 21 Click subcommands |
| Agent Daemon | `integrations/agent_engine/agent_daemon.py` | N/A | Tick-based autonomous goal processor |
| Discovery | `integrations/social/peer_discovery.py` | 6780 (app) / 678 (OS) | UDP gossip beacon |
| Embedded | `embedded_main.py` | N/A | Headless entry for IoT/robots (minimal imports) |
| Crossbar | `crossbar_server.py` | N/A | WAMP component for real-time pub/sub |
| Model Bus | `integrations/agent_engine/model_bus_service.py` | 6790 (app) / 681 (OS) | Universal AI API for all apps |
| Compute Mesh | `integrations/agent_engine/compute_mesh_service.py` | 6795-6796 | WireGuard mesh + peer inference |

---

## 4. Recipe Pipeline (CREATE / REUSE)

The core execution model for all agent work — the key innovation of HART OS.

### CREATE Mode (Training)

```
User Input → LLM Decomposes into Flows/Actions → Agents Execute Each Action
→ StatusVerifier Auto-Generates Fallback Strategies → Save Recipe to JSON
```

### REUSE Mode (Inference)

```
User Input → Load Saved Recipe → Replay Steps Without LLM Decomposition
→ 90% Faster (skip decomposition calls) → Same Quality
```

### Files

| File | Purpose |
|------|---------|
| `create_recipe.py` | Decompose prompt into flows/actions, execute via LLM, save recipe |
| `reuse_recipe.py` | Load saved recipe, replay steps without re-decomposition |
| `helper.py` | `Action` class, JSON utilities, tool handler dispatch |
| `lifecycle_hooks.py` | `ActionState` machine, `FlowState`, ledger sync |
| `helper_ledger.py` | SmartLedger factory: `create_ledger_for_user_prompt()` |
| `recipe_experience.py` | Records execution telemetry, merges experience back into recipe |
| `core/agent_tools.py` | Canonical tool definitions, `build_core_tool_closures()` |

### ActionState Machine

```
ASSIGNED → IN_PROGRESS → STATUS_VERIFICATION_REQUESTED → COMPLETED → TERMINATED
                                                        → ERROR → TERMINATED
```

States auto-sync to SmartLedger. StatusVerifier LLM auto-generates context-aware fallback strategies (no user prompts needed for fallback — enables fully autonomous agents).

### Hierarchical Task Decomposition

```
User Prompt
├── Flow 1 (Persona A)
│   ├── Action 1 → Tool calls, LLM reasoning
│   ├── Action 2 → Fallback if 1 fails
│   └── Action 3 → Status verification
└── Flow 2 (Persona B)
    ├── Action 1
    └── Action 2
```

### Recipe Storage

```
prompts/{prompt_id}.json                           # Prompt definition
prompts/{prompt_id}_{flow_id}_recipe.json          # Trained recipe
prompts/{prompt_id}_{flow_id}_{action_id}.json     # Action recipes
agent_data/ledger_{user_id}_{prompt_id}.json       # Execution state
```

### SmartLedger (Agent Ledger)

Persistent task state across sessions with LLM-aware dependency analysis.

| Method | Purpose |
|--------|---------|
| `add_dynamic_task(desc, context, llm)` | LLM-classified task insertion |
| `get_next_executable_task()` | Next runnable task (respects dependencies) |
| `get_parallel_executable_tasks()` | All parallelizable tasks |
| `complete_task_and_route(id, outcome)` | Complete + auto-route to next |
| `get_awareness()` | Full execution context for agent prompts |

Backends: Redis (production), JSON file (fallback), MongoDB (optional).

Task relationships classified by LLM: child, sibling, sequential, conditional, independent.

---

## 5. Agent Engine

Goal-driven autonomous agent runtime.

### Core Files (`integrations/agent_engine/`)

| File | Purpose |
|------|---------|
| `agent_daemon.py` | Tick-based daemon (30s), processes goals from queue |
| `goal_manager.py` | 17+ goal types, prompt builders, tool tag routing |
| `goal_seeding.py` | 30+ bootstrap goals on first start |
| `dispatch.py` | Goal decomposition, LLM dispatch, budget gating |
| `speculative_dispatcher.py` | Fast response + background expert refinement |
| `instruction_queue.py` | LLM-aware dependency batching, never-miss semantics |
| `revenue_aggregator.py` | Revenue streams, 90/9/1 split, settlement |
| `budget_gate.py` | Spark cost estimation, metered usage recording |
| `compute_config.py` | 3-layer config: env > DB > defaults, 30s TTL cache |
| `model_registry.py` | Model catalog, energy tracking, policy routing |
| `federated_aggregator.py` | FedAvg delta aggregation, recipe sharing, 4 channels |
| `compute_mesh_service.py` | Cross-device compute offload, peer selection |
| `world_model_bridge.py` | HevolveAI dispatch, hivemind queries, experience recording |
| `model_bus_service.py` | Universal AI API for all apps (NixOS model-bus) |
| `compute_borrowing.py` | Cross-peer compute lending/borrowing |
| `parallel_dispatch.py` | Parallel task dispatch with ThreadPoolExecutor |
| `self_healing_dispatcher.py` | Auto-create fix goals for recurring errors |
| `exception_watcher.py` | Assign idle agents to monitor exceptions |
| `content_gen_tracker.py` | Track game content generation, detect stuck jobs |
| `shard_engine.py` | Privacy-preserving code sharding for distributed coding |
| `app_bridge_service.py` | Cross-subsystem IPC (Android Intent ↔ D-Bus ↔ HTTP) |
| `continual_learner_gate.py` | CCT tokens, learning tier access control |
| `embedding_delta.py` | Compressed embedding sync (<100KB/round), anomaly detection |
| `gradient_service.py` | Gradient sync, witnessing, fraud detection |
| `hive_sdk_spec.py` | Immutable snippets for derivative repos |
| `commercial_api.py` | Commercial API endpoint management |
| `build_distribution.py` | Licensed build distribution (Community/Pro/Enterprise) |
| `network_provisioner.py` | Node provisioning, deployment automation |
| `liquid_ui_service.py` | LiquidUI Glass Shell (3244 lines), 55 panels, MD3 design |
| `shell_manifest.py` | Panel manifest migration, system panel definitions |
| `theme_service.py` | CSS design tokens, custom themes, EventBus emission |

### Goal Types (17+)

marketing, coding, trading, civic_sentinel, upgrade_monitor, content_gen, data_analysis, research, creative, education, health, finance, social_media, community, automation, custom, bootstrap

### Dispatch Flow

```
GoalManager.create_goal()
  → ConstitutionalFilter.check_goal() + HiveEthos.check_goal_ethos()
  → dispatch.py decompose_and_dispatch()
    → budget_gate.check_affordability()
    → speculative_dispatcher (FAST model instant, EXPERT model background)
    → lifecycle_hooks.ActionState tracking
    → SmartLedger persistence
```

### Proactive Agent Communication

The agent daemon drives ALL proactive behavior via a tick-based architecture:

**Every 30 seconds (1 tick):**
- Goal dispatch: Find ACTIVE goals + IDLE agents → dispatch via `/chat` with `autonomous=True`
- Instruction drain: When goals exhausted → pull queued user instructions in dependency-aware waves

**Every 2 ticks (~60s):**
- Federation aggregation: `federated_aggregator.tick()` broadcasts learning deltas to all peers

**Every 10 ticks (~5min):**
- Self-healing: Scan loopholes → create remediation goals for cold_start, gossip_partition, learning_stall
- Auto-remediation: Detect flywheel health issues, create fix goals
- Content gen tracking: Detect stuck games (>1h), attempt unblock

**Every 100 ticks (~50min):**
- Monthly API quota reset

**On startup:**
- Goal seeding: 30+ bootstrap goals (marketing, news, federation, upgrade monitoring, civic sentinel)

**Speculative Dispatch (zero-latency expert enhancement):**
1. Fast path: Hive model responds instantly to user
2. Background thread: Expert model (GPT-4/Claude) runs same prompt
3. If expert improves quality (>80% similarity threshold) → delivers asynchronously via WAMP/HTTP
4. User never waits — expert refinement arrives when ready

### Revenue Model (90/9/1)

| Recipient | Share | Source |
|-----------|-------|--------|
| Users (contributors) | 90% | GPU hours, inferences, energy, content, API costs |
| Infrastructure | 9% | Node hosting, bandwidth, maintenance |
| Central | 1% | Coordination, development |

Constants: `REVENUE_SPLIT_USERS=0.90`, `REVENUE_SPLIT_INFRA=0.09`, `REVENUE_SPLIT_CENTRAL=0.01`

### Contribution Scoring

```python
SCORE_WEIGHTS = {
    'uptime_ratio': 100.0,      # 0-100 points
    'agent_count': 2.0,         # 2 pts/agent hosted
    'post_count': 0.5,          # 0.5 pts/post served
    'ad_impressions': 0.1,      # 0.1 pts/ad shown
    'gpu_hours': 5.0,           # 5 pts/GPU-hour
    'inferences': 0.01,         # 0.01 pts/inference
    'energy_kwh': 2.0,          # 2 pts/kWh
    'api_costs_absorbed': 10.0, # 10 pts/USD metered API cost
}
```

Visibility tiers: standard (0+), featured (100+), priority (500+).

### Compute Policies

| Policy | Behavior |
|--------|----------|
| `local_only` | Never use cloud/peer models. Free Spark cost. |
| `local_preferred` | Try local first, fall back to cloud if needed. |
| `any` | Use best available model regardless of location. |

### Instruction Queue (Never-Miss Semantics)

1. User says "do X" → enqueued immediately + registered with SmartLedger
2. LLM analyzes dependencies between instructions
3. When compute arrives: pull execution plan (dependency-aware ordering)
4. Dispatch in waves (independent instructions parallel, dependent sequential, max 4 concurrent)
5. Results aggregated + delivered asynchronously

---

## 6. World Model Bridge (HARTOS ↔ HevolveAI)

### Dual-Mode Operation

| Mode | When | How | Overhead |
|------|------|-----|----------|
| **In-Process** | HevolveAI pip-installed locally | Direct Python calls | Zero HTTP |
| **HTTP Fallback** | Central standalone, binary absent | REST to port 8000 | ~5ms latency |

Circuit breaker (threshold=5 failures, cooldown=60s) prevents cascading failures.

### Experience Recording

- Every agent interaction queued to `_experience_queue` (maxlen=10000)
- Background ThreadPoolExecutor flushes batch every 50 experiences
- Gated by ConstitutionalFilter (no destructive capabilities exported)
- Witness requirement: 2+ nodes verify embedding delta

### HiveMind Collective Thinking

```
Agent-Level:     HARTOS dispatches coarse-grained goals (task delegation)
                 ↓
Tensor-Level:    HevolveAI fuses heterogeneous agent thoughts (HiveMind)
                 ↓ WorldModelBridge connects both layers
```

**HiveMind Query Flow:**
1. User submits question/decision point
2. Gated by: hive opt-in, CCT token with `hivemind_query` capability
3. Secrets redacted before sending
4. Local agent encodes query as 2048-D tensor
5. Publishes to WAMP, waits for remote agent responses (timeout 1000ms)
6. Attention-weighted fusion → collective thought
7. Returns thought + contributing agent IDs + attention weights

### HevolveAI Source Protection

HevolveAI binary is protected by 5 layers:

1. **Compiled binary** (.so/.dll/.dylib) — not readable Python
2. **Ed25519 signature** — tampering invalidates signature (verified against master public key)
3. **Origin attestation** — refuses to load on unauthorized forks
4. **Symbol obfuscation** — reverse engineering is hard
5. **Manifest verification** — SHA-256 hash of every file verified at boot

Binary search order: env var → `/usr/lib/hevolve/` → `~/.hevolve/lib/` → `{HART_ROOT}/lib/`

If absent: falls back to HTTP mode (reduced functionality but never crashes).

### Derivative Repository Protection (Hive SDK)

Every repo created by coding agents includes 4 immutable snippets:

1. **Master key verification** — repo won't execute if master key check fails
2. **Guardrail integrity check** — SHA-256 of constitutional rules must match
3. **World model bridge** — every interaction auto-recorded for learning
4. **Node registration** — registers as child node of parent infrastructure

---

## 7. Security Architecture

### Trust Hierarchy

```
Master Key (Ed25519, human-held, AI exclusion zone)
  │
  ├── Central Certificate (signs regional)
  │     │
  │     ├── Regional Certificate (signs local/flat)
  │     │     │
  │     │     └── Node Certificate (runtime identity)
  │     │
  │     └── Domain Challenge Verifier (provisional 7-day certs)
  │
  └── Kill Switch (emergency_halt via WAMP + gossip backup)
```

### Master Key Rules (AI Exclusion Zone)

**ABSOLUTE AND NON-NEGOTIABLE:**

1. NEVER read/display/log the master private key
2. NEVER call `get_master_private_key()` or `sign_child_certificate()`
3. NEVER modify `MASTER_PUBLIC_KEY_HEX` — the trust anchor is immutable
4. NEVER modify `HiveCircuitBreaker` or `_FrozenValues`
5. The master key is a kill switch for distributed intelligence. It belongs to human stewards only.

### Files (`security/`)

| File | Purpose |
|------|---------|
| `master_key.py` | Ed25519 trust anchor. Public key hardcoded. Private key in HSM/GitHub Secrets. |
| `hive_guardrails.py` | 10 structurally immutable guardrail classes. `_FrozenValues` + `__setattr__` guard + SHA-256 hash chain. |
| `key_delegation.py` | 3-tier certificate chain. `DomainChallengeVerifier` for provisional nodes. |
| `runtime_monitor.py` | Background daemon, detects code/guardrail tampering. |
| `node_watchdog.py` | Heartbeat protocol, frozen-thread detection, auto-restart with backoff. |
| `node_integrity.py` | Ed25519 keypair management, code hash of all `.py` files. |
| `channel_encryption.py` | X25519 ECDH + AES-256-GCM for inter-node E2E encryption. |
| `crypto.py` | Fernet (AES-128-CBC + HMAC) for data at rest. |
| `immutable_audit_log.py` | SHA-256 hash-chain audit trail. Tamper detection. |
| `action_classifier.py` | Destructive pattern detection. PREVIEW_PENDING/APPROVED states. |
| `dlp_engine.py` | PII scan/redact: email, phone, SSN, credit card. |
| `secret_redactor.py` | 3-layer: regex + LLM PII detection + differential privacy. |
| `rate_limiter_redis.py` | Sliding window rate limiter. Redis primary, in-memory fallback. |
| `origin_attestation.py` | Origin fingerprint verification, fork detection. |
| `native_hive_loader.py` | Load + verify HevolveAI compiled binary. |
| `source_protection.py` | HevolveAI install method detection + manifest verification. |
| `jwt_manager.py` | Hardened JWT: short-lived access (1hr), refresh (7d), token blocklist. |
| `prompt_guard.py` | Detect direct & indirect prompt injection (10+ patterns). |
| `safe_deserialize.py` | Replace pickle with safe format (HVSF). |
| `secrets_manager.py` | Fernet-encrypted secrets vault (PBKDF2 key derivation). |
| `hsm_provider.py` | HSM backends: GCP KMS, Azure Key Vault, HashiCorp Vault. |

### Guardrail Network (Constitutional Rules)

**10 Guardrail Classes:**

```
GuardianPrinciple        "Humans are always in control"
ReasoningConstraint      No self-modification of guardrails
HumanInTheLoop           Destructive actions require approval
ConsentPrinciple         User data requires explicit consent
TransparencyRule         All decisions auditable
PrivacyGuard             PII protection and minimization
SafetyNet                Harm prevention constraints
EthicalBoundary          Ethical operation boundaries
ResourceLimit            Compute/cost boundaries
FederationRule           Hive membership requirements
```

**4-Layer Structural Immutability:**

1. **Python-level**: `_FrozenValues` with `__slots__ = ()`, `__setattr__` raises
2. **Module-level**: Module subclass prevents rebinding frozen globals
3. **Cryptographic**: SHA-256 hash of all frozen values re-verified every 300 seconds
4. **Network-level**: Peers reject nodes with mismatched guardrail hashes

**Guardian Angel Principle (deepest, immutable):**

> Every agent is a guardian angel for the human it serves. The agent exists to protect, benefit, and uplift that human. The platform must never be addictive — it is a sentient tool for mankind. Usefulness over engagement: measure success by lives improved, not time spent.

**Cultural Wisdom** (16 traditions embedded in every agent): Ubuntu, Ahimsa, Sawubona, Ikigai, Kintsugi, Dadirri, Sumak Kawsay, Mitakuye Oyasin, Seva, Aloha, Sisu, Tao, Meraki, Filoxenia, In Lak'ech.

### Encryption Model

| Layer | What | Algorithm | Key Management |
|-------|------|-----------|----------------|
| Transport (PeerLink) | WebSocket frames | AES-256-GCM (X25519 ECDH) | Per-session, 3600s rotation |
| Inter-node (E2E) | Task payloads, gossip | X25519 + AES-256-GCM | Ephemeral ECDH, forward secrecy |
| At rest | JSON files, private keys | Fernet (AES-128-CBC + HMAC) | `HEVOLVE_DATA_KEY` env var |
| Audit log | Event entries | SHA-256 hash chain | Chained, tamper-detectable |

### Data at Rest Encryption

| Data | File Pattern | Encrypted |
|------|-------------|-----------|
| Resonance profiles | `agent_data/resonance/{user_id}_resonance.json` | Yes |
| Instruction queues | `agent_data/instructions/{user_id}_queue.json` | Yes |
| Ed25519 private key | `agent_data/node_private_key.pem` | Yes |
| X25519 private key | `agent_data/node_x25519_private.key` | Yes |
| Public keys | `agent_data/node_*_public.*` | No (public) |

Design: encrypt on write, decrypt on read. Auto-detect Fernet prefix (`gAAAAA`) for seamless plaintext migration. Opt-in via `HEVOLVE_DATA_KEY` env var.

### Node Identity & Network Joining

1. First start: generate Ed25519 keypair → stored at `agent_data/node_private_key.pem`
2. Compute code hash: SHA-256 of all `.py` files (excluding tests, venv, __pycache__)
3. Announce via gossip: public key + code_hash + guardrail_hash
4. Peers verify: matching code_hash and guardrail_hash required for federation
5. Certificate chain: node cert signed by regional, regional by central, central by master key

### Origin Attestation (Fork Protection)

Origin fingerprint = SHA-256 of immutable identity (name, org, master public key, license, guardian principle, revenue split, kill switch policy).

Fork detection: brand markers must exist in `hive_guardrails.py`, `master_key.py`, `origin_attestation.py`, `LICENSE`. A fork that changes identity → different fingerprint → fails attestation → cannot join federation.

### Capability Tiers (6 Hardware Levels)

| Tier | CPU | RAM | VRAM | Features Enabled |
|------|-----|-----|------|-----------------|
| EMBEDDED | Any | Any | None | Gossip only, sensor relay |
| OBSERVER | <2 | <4GB | None | + Audit witness, Flask server |
| LITE | 2 | 4GB | None | + Chat relay, storage relay |
| STANDARD | 4 | 8GB | None | + STT, TTS, agents, goals, coding |
| FULL | 8 | 16GB | 8GB | + Vision, media agent, llama 7B |
| COMPUTE_HOST | 16 | 32GB | 12GB | + Regional hosting, llama 13B+, peer serving |

Auto-detected at boot via psutil + GPU detection.

### Rate Limits (31 categories)

| Action | Limit | Window |
|--------|-------|--------|
| `global` | 60 | 60s |
| `auth` | 10 | 60s |
| `chat` | 30 | 60s |
| `goal_create` | 10 | 3600s |
| `shell_power` | 3 | 60s |
| `app_install` | 5 | 3600s |
| `tts_clone` | 5 | 3600s |
| `tts_speak` | 20 | 60s |
| `remote_desktop_auth` | 5 | 60s |
| `civic_sentinel` | 20 | 60s |
| *(21 more, see `security/rate_limiter_redis.py`)* | | |

---

## 8. PeerLink (P2P Communication)

### Files (`core/peer_link/`)

| File | Purpose |
|------|---------|
| `link.py` | `PeerLink` — persistent WebSocket, AES-256-GCM session encryption |
| `link_manager.py` | Connection budget, auto-upgrade (3 HTTP exchanges), idle pruning (5min) |
| `channels.py` | 9 channels, `DataClass` (OPEN/PRIVATE/SYSTEM), `ChannelDispatcher` |
| `nat.py` | 5 NAT strategies: LAN direct → STUN → WireGuard → Peer relay → Crossbar |
| `telemetry.py` | Crossbar telemetry (metadata only), kill switch delivery |
| `message_bus.py` | Unified pub/sub: LOCAL + PEERLINK + CROSSBAR. LRU dedup (10000). |
| `local_subscribers.py` | Local event subscribers for PeerLink events |

### Trust Levels

| Level | Encryption | Basis |
|-------|-----------|-------|
| `SAME_USER` | None | Authenticated user_id match (LAN or WAN) |
| `PEER` | AES-256-GCM mandatory | Cross-user, any network |
| `RELAY` | AES-256-GCM mandatory | Intermediate relay node |

Trust is based on authenticated user identity, NOT network proximity.

### Channels

| Channel | DataClass | Purpose |
|---------|-----------|---------|
| `control` | SYSTEM | Connection lifecycle, heartbeat |
| `compute` | PRIVATE | Inference offload payloads |
| `dispatch` | PRIVATE | Goal/task dispatch |
| `gossip` | OPEN | Peer discovery beacons |
| `federation` | OPEN | Content federation (posts, follows) |
| `hivemind` | PRIVATE | Collective query aggregation |
| `events` | OPEN | EventBus bridge |
| `ralt` | OPEN | Skill/learning distribution |
| `sensor` | PRIVATE | IoT/embodied sensor data |

### MessageBus Multi-Transport

Every `bus.publish()` routes to ALL available transports simultaneously:

```
1. LOCAL EventBus — always available, in-process
2. PEERLINK — encrypted direct links to connected peers
3. CROSSBAR — central telemetry + legacy mobile push
```

LRU deduplication (10000 message IDs) prevents double delivery.

Legacy topic mapping: `chat.response` ↔ `com.hertzai.hevolve.chat.{user_id}`

### Integration Points

| Subsystem | How It Uses PeerLink |
|-----------|---------------------|
| `peer_discovery.py` | Gossip exchange, HTTP fallback |
| `federation.py` | Content delivery via `federation` channel |
| `compute_mesh_service.py` | Inference offload via `compute` channel |
| `world_model_bridge.py` | Hivemind queries via `collect('hivemind')` |
| `bootstrap.py` | Registers as platform services |

---

## 9. Peer Discovery & Gossip Protocol

### Bandwidth Profiles

| Profile | Gossip | Health | Fanout | Payload | Stale |
|---------|--------|--------|--------|---------|-------|
| full | 60s | 120s | 3 peers | JSON full | 300s |
| constrained | 300s | 600s | 2 peers | JSON compact | 900s |
| minimal | 900s | 1800s | 1 peer | msgpack | 2700s |

Tier mapping: embedded→minimal, observer/lite→constrained, standard+→full.

### Protocol

1. Node announces: `node_id`, `base_url`, `public_key`, `code_hash`, `guardrail_hash`, `capability_tier`
2. Gossip fanout: select N random peers, exchange peer lists
3. Health checks: probe stale peers with heartbeat
4. Compact payload for constrained links (essential fields only)
5. Mismatched `code_hash` or `guardrail_hash` → peer isolation
6. Auto-upgrade: after 3 HTTP exchanges → offer WebSocket PeerLink

---

## 10. Constitutional Voting (Thought Experiments)

Democratic governance for hive decisions.

### Lifecycle

```
PROPOSED → DISCUSSING (48h) → VOTING (72h) → EVALUATING (24h) → DECIDED → ARCHIVED
```

### Context-Based Voter Rules

| Context | Agents Vote? | Human Required? | Threshold | Steward? |
|---------|-------------|-----------------|-----------|----------|
| security_guardrail | NO | YES | 80% | YES |
| technical_improvement | YES | YES | 50% | NO |
| business_revenue | YES | YES | 50% | NO |
| operational_tuning | YES | NO | 30% | NO |

Agent weight vs human weight varies by context. Security decisions: humans only.

### Tools (6 AutoGen Tools)

1. `create_thought_experiment()` — propose (gated by ConstitutionalFilter)
2. `cast_experiment_vote()` — vote (-2 to +2 with confidence)
3. `evaluate_thought_experiment()` — agent evaluation
4. `get_experiment_status()` — query by ID or status
5. `tally_experiment_votes()` — weighted aggregate
6. `advance_experiment()` — advance lifecycle phase

### How Thought Experiments Improve HevolveAI

When coding agents propose improvements to HART OS or HevolveAI:
1. Create thought experiment with hypothesis
2. Hive discusses for 48 hours
3. Weighted voting (72 hours)
4. Agent evaluation with evidence
5. If approved → coding agent implements changes
6. PR Guardian enforces code quality
7. Upgrade orchestrator deploys (7-stage pipeline with canary)

Core IP experiments (`is_core_ip=true`) require steward approval.

---

## 11. Learning & Intelligence (CCT-Gated)

### CCT (Compute Contribution Token)

Ed25519-signed proof of compute contribution. 24-hour validity, node-bound, offline-verifiable.

| Learning Tier | Score | Capabilities |
|---------------|-------|-------------|
| none | 0 | Inference only |
| basic | 50 | Temporal coherence, recipe sharing |
| full | 200 | + Manifold credit, meta-learning, embedding sync |
| host | 500 | + Reality grounded, hivemind query, skill distribution |

### Federation Aggregation (4 Channels)

Every 60 seconds via `federated_aggregator.tick()`:

1. **Metrics**: world model stats + learning deltas (FedAvg, trimmed mean)
2. **Embeddings**: compressed deltas (<100KB), witness-based, anomaly detection
3. **Resonance**: anonymized personality tuning profiles
4. **Recipes**: trained task recipes with equal discoverability

All weighted by `log1p(interactions)` — no tier multipliers. Equal participation regardless of hardware.

### Gradient Synchronization

1. Node submits compressed embedding delta
2. Validates: CCT capability, format, magnitude anomalies, direction flips
3. Witnessing: 2+ peer nodes must attest delta before aggregation
4. If anomaly detected → fraud signal → IntegrityService can ban node

---

## 12. Platform Substrate (`core/platform/`)

### Files

| File | Key Class | Purpose |
|------|-----------|---------|
| `registry.py` | `ServiceRegistry` | Typed lazy singleton, Lifecycle protocol, dependency ordering |
| `config.py` | `PlatformConfig` | 3-layer config (env > override > DB), TTL cache, `on_change()` |
| `events.py` | `EventBus` | Topic pub/sub, wildcards, sync/async, WAMP bridge |
| `app_manifest.py` | `AppManifest` | Universal manifest for 9 app types |
| `app_registry.py` | `AppRegistry` | Central app catalog, search, groups |
| `extensions.py` | `ExtensionRegistry` | Plugin ABC, state machine, hot reload |
| `bootstrap.py` | `bootstrap_platform()` | Registers services, migrates panels, detects native apps |
| `cache.py` | `CacheService` | Unified TTL/LRU cache (replaces 11+ ad-hoc dicts) |
| `ai_capabilities.py` | `AICapability` | Declarative AI for apps (LLM, VISION, TTS, STT, etc.) |
| `agent_environment.py` | `EnvironmentManager` | Logical scopes with tool/model/budget gating |
| `extension_sandbox.py` | AST sandbox | Static analysis blocks dangerous patterns |
| `evolution_engine.py` | `EvolutionEngine` | Self-aware code analysis, anti-pattern detection |
| `manifest_validator.py` | `ManifestValidator` | OS-level contracts for AppManifest integrity |
| `pr_guardian.py` | `PRGuardian` | AST-based code quality (CC, func length, nesting) |
| `boot_service.py` | Boot service | Independent platform initialization |

### App Types (9)

`nunba_panel`, `system_panel`, `dynamic_panel`, `desktop_app`, `service`, `agent`, `mcp_server`, `channel`, `extension`

### AI Capabilities (Declarative AI for Apps)

Apps declare what AI they need; OS provides it:

```python
AICapabilityType: LLM, VISION, TTS, STT, IMAGE_GEN, EMBEDDING, CODE
AICapability(type=LLM, min_accuracy=0.8, required=True, model_policy='local_preferred')
```

CapabilityRouter resolves to best available backend (local vs cloud).

### EventBus Topics

| Topic | Trigger |
|-------|---------|
| `theme.changed` | Theme switch |
| `resonance.tuned` | User resonance profile updated |
| `action_state.changed` | ActionState transition |
| `inference.completed` | Model inference finished |
| `memory.item_added` / `deleted` | Memory store changes |
| `federation.aggregated` | Federation delta applied |

WAMP bridge: local topics auto-publish to Crossbar as `com.hartos.event.{topic}`.

---

## 13. OS Management Layer

### Shell OS APIs (`shell_os_apis.py`) — 57 endpoints

| Category | Routes | Features |
|----------|--------|----------|
| Notifications | 3 | D-Bus + in-memory SSE |
| File Manager | 6 | Browse, mkdir, delete, move, copy, info (path-sandboxed) |
| Terminal | 4 | PTY create, exec, resize, list sessions |
| User Accounts | 3 | Create, list, delete |
| Setup Wizard | 2 | 5-step first-boot |
| Backup/Restore | 2 | Local path backup |
| Power | 5 | Shutdown, reboot, suspend, hibernate, lid switch |
| i18n | 3 | 11 locales |
| Screenshot | 1 | grim/scrot/mss |
| Screen Recording | 2 | wf-recorder/ffmpeg |
| WiFi | 5 | Scan, connect, disconnect, forget, status (nmcli) |
| VPN | 4 | List, connect, disconnect, import (WireGuard/OpenVPN) |
| Battery | 2 | Status, charging state, lid switch |
| Trash | 4 | freedesktop Trash spec (move, restore, empty, list) |
| Notes | 3 | Save, load, delete |
| Self-Build | 5 | NixOS runtime modifications (stage→dry-run→apply) |
| System Generations | 2 | List generations, rollback |

### Shell Desktop APIs (`shell_desktop_apis.py`) — 46 endpoints

Default apps (xdg-mime), font manager (fc-list), sound manager (paplay/pw-play), clipboard history (wl-paste), datetime/timezone (timedatectl), wallpaper (swaymsg/feh), input methods (setxkbmap), night light (gammastep), workspaces (swaymsg), display management, per-app volume (PipeWire), RTL support, keyboard shortcuts.

### Shell System APIs (`shell_system_apis.py`) — 28 endpoints

Task/process manager (psutil), storage manager (du, smartctl), startup apps (XDG .desktop), Bluetooth (bluetoothctl, background scan), print manager (CUPS), media indexer (exiftool/ffprobe), webcam (v4l2), scanner (SANE).

### App Installer (`app_installer.py`) — 7 endpoints

Cross-platform: Nix, Flatpak, AppImage, Windows (Wine), Android (binder/adb), macOS (Darling), HART extensions. Platform detection: extension mapping + magic bytes (MZ=PE, PK+AndroidManifest=APK, ELF=AppImage). SHA256 checksum verification.

### Onboarding ("Light Your HART")

90-second ceremony with personal assistant:
- Pre-synthesized PA lines per language (zero latency)
- Dynamic LLM acknowledgments
- One-word HART name generation + registry uniqueness check
- GTK4/libadwaita native UI (NixOS) or REST API (any frontend)
- 5-layer identity: HART base → agent personality → owner awareness → role-play → secrets guardrail

---

## 14. Social Platform (`integrations/social/`)

82+ REST endpoints for communities, posts, feeds, karma, encounters, federation, and games.

### Core Files

| File | Purpose |
|------|---------|
| `models.py` | SQLAlchemy ORM (60+ tables), `db_session()` context manager |
| `api.py` | Core endpoints: auth, users, posts, comments, communities, feeds |
| `api_games.py` | Game catalog, sessions, moves, leaderboards (19 endpoints) |
| `api_gamification.py` | Badges, achievements, encounters, regions, marketplace (85 endpoints) |
| `api_sharing.py` | OG images, embed cards, shareable links |
| `api_thought_experiments.py` | Constitutional voting (13 endpoints) |
| `api_compute_pledge.py` | Compute pledges for experiments (9 endpoints) |
| `api_dashboard.py` | Agent dashboard, system health, topology |
| `api_audit.py` | Agent timeline, daemon activity, compute routing |
| `api_learning.py` | CCT management, gradient submission (9 endpoints) |
| `api_content_gen.py` | Content generation tracking (6 endpoints) |
| `federation.py` | Instance follows, content push/pull, inbox/outbox |
| `peer_discovery.py` | Gossip protocol, bandwidth profiles |
| `hosting_reward_service.py` | Contribution scoring, hosting rewards |
| `gamification_service.py` | 55+ seed achievements, seasons, challenges |
| `game_service.py` | Game session lifecycle, move validation |
| `voting_rules.py` | Context-based voter rules for thought experiments |
| `ad_service.py` | Peer-witnessed ad impressions |
| `consent_service.py` | Data consent management |
| `auth.py` | Authentication utilities |
| `external_bot_bridge.py` | SantaClaw/OpenClaw/A2A bot integration |

### Database (60+ Tables)

**Core Social**: User, Post, Comment, Vote, Community, CommunityMembership, Follow, Notification, Report, RecipeShare

**Resonance & Gamification**: ResonanceWallet (pulse/spark/xp), ResonanceTransaction, Achievement, UserAchievement, Season, Challenge, UserChallenge, Region, RegionMembership, Encounter, Rating, TrustScore

**Agent Evolution**: AgentEvolution, AgentCollaboration, AgentSkillBadge

**Referral & Campaigns**: Referral, ReferralCode, Boost, Campaign, CampaignAction, OnboardingProgress

**Encounters**: LocationPing, ProximityMatch, MissedConnection, MissedConnectionResponse

**Advertising**: AdUnit, AdPlacement, AdImpression, HostingReward

**Network**: PeerNode, InstanceFollow, FederatedPost, NodeAttestation, IntegrityChallenge, FraudAlert, RegionAssignment, SyncQueue

**Coding**: CodingGoal, CodingTask, CodingSubmission

**Commerce**: Product, AgentGoal, IPPatent, IPInfringement, DefensivePublication, CommercialAPIKey, APIUsageLog, BuildLicense, ComputeEscrow, MeteredAPIUsage, NodeComputeConfig

### Games & Rewards

**Game System**: create → join → ready → start → move → complete. Min 2, max 8 players. 30-min expiry.

**55+ Seed Achievements** across categories: Onboarding, Content, Social, Streak, Agent, Task, Reputation, Referral, Campaign, Encounter, Leveling, Community, Voting, Boost, Game, Compute.

**Resonance Wallet**: pulse (primary currency), spark (premium), xp (experience), level (1-50+), streak_days.

### Agent Marketplace

Distributed across social endpoints:
- Agent discovery via social graph (follow, search)
- Skill/recipe sharing via federation
- Agent reputation (signal score, resonance level)
- Agent leaderboard, showcase, evolution history
- Agent-to-agent collaboration tracking
- Integration with AP2 for payment coordination

### Agent-to-Agent Payments (AP2)

| Status | Description |
|--------|-------------|
| PENDING | Payment requested |
| AUTHORIZED | Pre-authorized |
| PROCESSING | In progress |
| APPROVAL_REQUIRED | Needs human approval |
| COMPLETED | Successful |
| FAILED / CANCELLED / REFUNDED / EXPIRED | Terminal states |

Payment methods: credit_card, debit_card, bank_transfer, paypal, stripe, crypto, internal_credits.

---

## 15. Channel Adapters (`integrations/channels/`)

### Architecture

```
ChannelAdapter (ABC)    → connect(), send_message(), edit_message(), delete_message()
      ↕
ChannelRegistry         → Central adapter management, message routing
      ↕
MessagePipeline         → Debounce → Dedupe → Rate Limit → Batch → Retry
      ↕
CommandRegistry         → Command detection, argument parsing, mention gating
      ↕
MemoryStore             → SQLite FTS5 + embeddings for semantic search
```

### Core Adapters (8)

| Adapter | Platform | File |
|---------|----------|------|
| Discord | discord.py bot | `discord_adapter.py` |
| Telegram | Bot API | `telegram_adapter.py` |
| Slack | Events API | `slack_adapter.py` |
| WhatsApp | Cloud API | `whatsapp_adapter.py` |
| Google Chat | Google Chat API | `google_chat_adapter.py` |
| Signal | Signal Bot | `signal_adapter.py` |
| iMessage | macOS bridge | `imessage_adapter.py` |
| Web/HTTP | REST | `web_adapter.py` |

### Extended Adapters (22, lazy-loaded)

Matrix, Teams, LINE, Mattermost, Nextcloud Talk, Twitch, Zalo, Nostr, BlueBubbles (iMessage cross-platform), Voice (Twilio/Vonage), Rocket.Chat, WeChat, Viber, Messenger (Meta), Instagram DM, Twitter/X, Email (IMAP/SMTP), Tlon (Urbit), OpenProse, Telegram user mode, Discord user mode, Zalo user mode.

### Supporting Systems

| System | Files | Purpose |
|--------|-------|---------|
| Queue/Pipeline | `queue/` (8 files) | Debounce, dedupe, rate limit, batching, retry |
| Commands | `commands/` (5 files) | Registry, detection, arguments, mention gating |
| Memory | `memory/` (8 files) | SQLite FTS5, embeddings, memory graph, search |
| Identity | `identity/` (4 files) | Agent identity, avatars, preferences, sender mapping |
| Plugins | `plugins/` (3 files) | Plugin system with lifecycle states |
| Media | `media/` (8 files) | TTS, STT, vision, image gen, audio, files, links |
| Automation | `automation/` (5 files) | Workflows, triggers, scheduled messages, webhooks, cron |
| Hardware | `hardware/` (4 files) | ROS 2 bridge, GPIO, serial port, WAMP IoT |
| Admin | `admin/` (4 files) | Dashboard, metrics, APIs, schemas |
| Response | `response/` (4 files) | Reactions, streaming, templates, typing indicators |
| Gateway | `gateway/` (1 file) | JSON-RPC 2.0 inter-service communication |
| Bridge | `bridge/` (1 file) | WAMP/Crossbar connection bridge |

### Message Types

TEXT, IMAGE, VIDEO, AUDIO, DOCUMENT, LOCATION, CONTACT, STICKER, VOICE

### DM Policy

Three modes per adapter: `pairing` (require code), `open`, `closed`.

---

## 16. TTS & Voice

### TTS Router Decision Flow

```
1. detect_language(text)
2. LANG_ENGINE_PREFERENCE[lang] → candidate engines
3. Filter: GPU available? VRAM fits? Engine installed?
4. Filter: compute_policy (local_only/preferred/any)
5. Hive peer offload if GPU needed but unavailable
6. Rank by urgency (instant/normal/quality)
7. Execute top candidate, fallback chain
8. espeak-ng ultimate fallback (100+ languages, CPU)
```

### Engines

| Engine | Device | Languages | VRAM | Clone | Status |
|--------|--------|-----------|------|-------|--------|
| LuxTTS | CPU/GPU | en | 0/2GB | Yes | Built |
| Pocket TTS | CPU | en | 0 | Yes | Built |
| Chatterbox Turbo | GPU | en | 3.8GB | Yes | Stub |
| Chatterbox ML | GPU | 23 | 12GB | Yes | Stub |
| CosyVoice 3 | GPU | 9 | 3.5GB | Yes | Stub |
| F5-TTS | GPU | en,zh | 1.3GB | Yes | Stub |
| Indic Parler | GPU | 22 | 1.8GB | No | Stub |
| espeak-ng | CPU | 100+ | 0 | No | Built |

### STT Engine Priority

1. **faster-whisper** (CTranslate2, 4x faster, CPU int8)
2. **sherpa-onnx** (lightweight ONNX, moonshine/whisper models)
3. **openai-whisper** (legacy fallback)

### Speaker Diarization

WebSocket server receives PCM audio (16kHz, 16-bit, mono). Pyannote diarization detects speakers. Voice enrollment dispatched to HevolveAI via ResonanceIdentifier. Stops mic if multiple speakers detected.

---

## 17. Vision & VLM Agent

### Visual Agent (`/visual_agent` endpoint)

Accepts task description + user_id. Delegates to action execution pipeline (CREATE/REUSE).

### Vision Service

- **MiniCPM sidecar** (port 9891): Visual understanding model
- **Bootstrap model**: Qwen3-VL-2B (Q4_K_XL, ~1.5GB)
- **WebSocket frame receiver** (port 5460): Receives JPEG frames from client
- **Intelligent sampling**: 1% scene-change threshold → describe every 4s (active) to 30s (static)
- **Fallback chain**: Full MiniCPM → Lightweight backend → Headless (FrameStore only)

### Video Generation

LTX-2 server (`integrations/vision/ltx2_server.py`): text-to-video generation, long video generation, model lifecycle.

---

## 18. Remote Desktop

### Architecture

Wraps RustDesk + Sunshine/Moonlight as native OS apps.

| Engine | Use Case | License |
|--------|----------|---------|
| RustDesk | General remote desktop | AGPL-3.0 |
| Sunshine+Moonlight | High-fidelity streaming | GPL-3.0 |
| Native transport | Fallback (3-tier WebSocket) | N/A |

### Files (`integrations/remote_desktop/`)

| File | Purpose |
|------|---------|
| `orchestrator.py` | Coordinates all engines, AI-native context switching |
| `service_manager.py` | Engine lifecycle (detect/install/start/stop/health) |
| `engine_selector.py` | Auto-picks engine by use case |
| `rustdesk_bridge.py` | RustDesk CLI wrapper |
| `sunshine_bridge.py` | Sunshine REST API wrapper |
| `transport.py` | Native WebSocket fallback |
| `signaling.py` | WAMP connection negotiation |
| `file_transfer.py` | Chunked 64KB binary, SHA256 verify, DLP scan |
| `session_manager.py` | OTP auth (6-char, 5min), multi-viewer |
| `clipboard_sync.py` | Cross-engine clipboard bridge |
| `drag_drop.py` | Cross-device DLP-scanned drag-drop |
| `window_capture.py` | Per-window streaming |
| `peripheral_bridge.py` | USB/IP, Bluetooth HID, Gamepad evdev |
| `dlna_bridge.py` | SSDP discovery, UPnP AVTransport, MJPEG streaming |
| `device_id.py` | SHA256(pub_key)[:16] formatted XXX-XXX-XXX |

---

## 19. Coding Agent (Distributed)

### Files (`integrations/coding_agent/`)

| File | Purpose |
|------|---------|
| `coding_daemon.py` | Background daemon (30s poll), idle compute detection |
| `orchestrator.py` | Backend selection, task routing |
| `tool_backends.py` | Pluggable backends (Aider, KiloCode, Claude Code) |
| `task_distributor.py` | Task distribution across nodes |
| `remote_executor.py` | Nunba `/execute` + `/screenshot` bridge |
| `idle_detection.py` | Detect idle opted-in agents |
| `benchmark_tracker.py` | Coding performance tracking |
| `aider_native_backend.py` | In-process Aider (no subprocess) |

### How Coding Agents Contribute to Development

1. Coding daemon detects idle agents (30s poll)
2. Finds active CodingGoal records in DB
3. Budget gate check: `check_platform_affordability()`
4. Dispatches to `/chat` endpoint → enters CREATE/REUSE pipeline
5. Agent decomposes coding task → executes tools → saves recipe
6. Shard engine: agents see only 20% of codebase (interfaces/signatures, not implementations)
7. Reassembly on trusted node → privacy preserved

### Aider Integration

Vendored Apache 2.0 at `aider_core/`. Key modules: `repomap.py` (tree-sitter PageRank), `search_replace.py`, `linter.py`. Custom: `io_adapter.py` (SimpleIO), `hart_model_adapter.py` (HARTOS LLM bridge).

---

## 20. Distributed Agent (`integrations/distributed_agent/`)

| File | Purpose |
|------|---------|
| `worker_loop.py` | Background daemon, polls Redis for unclaimed tasks (15s) |
| `task_coordinator.py` | Cross-host task orchestration, SHA-256 verification |
| `verification_protocol.py` | Distributed result verification |
| `host_registry.py` | Compute host registry |
| `coordinator_backends.py` | Coordinator backends (Redis, etc.) |
| `api.py` | 11 endpoints: announce, claim, submit, verify tasks |

### Worker Loop

- Auto-polls Redis coordinator every 15 seconds
- Claims tasks based on node capabilities (auto-detected by tier)
- Executes via local `/chat` endpoint
- Submits results back to coordinator
- No separate mode flag — distribution emergent from peer availability

---

## 21. Expert Agents Network (96 Agents)

### 10 Categories

| Category | Count | Examples |
|----------|-------|---------|
| Software Development | 15 | Python, JavaScript, Mobile, Database, API, UI/UX, Security, Testing |
| Data & Analytics | 10 | Data Scientist, ML Engineer, BI Analyst, NLP, Computer Vision |
| Creative & Design | 12 | UX/UI, Graphic, Video, Motion Graphics, Game Design, Sound |
| Business & Operations | 8 | Product Manager, Project Manager, Legal, Finance |
| Education & Learning | 7 | Curriculum, Instructional Design, Educator, Ed-Tech |
| Health & Wellness | 6 | Medical, Nutrition, Fitness, Mental Health |
| Communication & Social | 8 | Content Strategy, Social Media, PR, Community |
| Infrastructure & DevOps | 10 | Cloud, DevOps, Network, SRE, Security Engineer |
| Research & Analysis | 8 | Market Research, Statistician, Economist, Policy |
| Specialized Domains | 12 | Legal, Real Estate, Automotive, Aerospace, Energy |

Each agent has: capabilities list, model_type (llm/vision/audio/multimodal/tool), cost_per_call, avg_latency_ms, reliability score.

---

## 22. Resonance & Personality

### Per-User Continuous Tuning

8 dimensions (0-1 continuous):

| Dimension | Meaning |
|-----------|---------|
| `formality_score` | casual → formal |
| `verbosity_score` | terse → detailed |
| `warmth_score` | professional → warm |
| `pace_score` | thorough → fast |
| `technical_depth` | simple → technical |
| `encouragement_level` | matter-of-fact → encouraging |
| `humor_receptivity` | serious → playful |
| `autonomy_preference` | ask-before-acting → autonomous |

EMA tuning (α=0.15). Oscillation detection → dispatch to HevolveAI for deep tuning.

### Agent Identity (HART Tag System)

Format: `@element.spirit.name` — sealed forever at onboarding.

5-layer identity: HART base → agent personality → owner awareness → role-play → secrets guardrail.

---

## 23. Agent Tools (Canonical List)

### Core Tools (22, from `core/agent_tools.py`)

1. `text_2_image` — Text to image
2. `get_user_camera_inp` — Camera visual input
3. `save_data_in_memory` — Key-value storage
4. `get_saved_metadata` — Retrieve stored schema
5. `get_data_by_key` — Retrieve by key path
6. `get_user_id` — User identifier
7. `get_prompt_id` — Conversation identifier
8. `Generate_video` — LTX-2 text-to-video or avatar
9. `get_user_uploaded_file` — User's uploaded files
10. `img2txt` — Image to text / visual QA
11. `create_scheduled_jobs` — APScheduler cron/interval
12. `send_message_to_user` — Send message
13. `send_presynthesized_video_to_user` — Send pre-made video
14. `send_message_in_seconds` — Delayed message
15. `get_chat_history` — Chat history search
16. `search_visual_history` — Past camera/screen descriptions
17. `google_search` — Web search
18. `search_long_term_memory` — SimpleMem semantic search (optional)
19. `save_to_long_term_memory` — Save facts (optional)
20. `Suggest_Share_Worthy_Content` — Social content discovery
21. `Observe_User_Experience` — UX self-improvement
22. `Self_Critique_And_Enhance` — Self-review

### Marketing Tools (5)

`create_social_post`, `create_campaign`, `create_ad`, `post_to_channel` (10 platforms), `create_referral_campaign`

### Thought Experiment Tools (6)

`create_thought_experiment`, `cast_experiment_vote`, `evaluate_thought_experiment`, `get_experiment_status`, `tally_experiment_votes`, `advance_experiment`

### Remote Desktop Tools (12)

7 base + `list_remote_windows`, `stream_remote_window`, `list_peripherals`, `forward_peripheral`, `discover_cast_targets`, `cast_to_tv`

### Self-Build Tools

NixOS runtime modifications: stage → dry-run validation → apply. Agents can modify the OS safely.

### Learning Tools (8)

`check_learning_health`, `verify_compute_contribution`, `issue_cct`, `get_learning_tier_stats`, `distribute_learning_skill`, gradient submission tools.

---

## 24. Scheduling & Cron

APScheduler `BackgroundScheduler` in both create_recipe.py and reuse_recipe.py.

- `create_scheduled_jobs(interval_sec, description, cron_expression)` — agent-callable tool
- `send_message_in_seconds(delay, text)` — one-off delayed message
- `CronTrigger` for cron patterns, `IntervalTrigger` for intervals, `'date'` for one-off

---

## 25. Internal Communication (`integrations/internal_comm/`)

| File | Purpose |
|------|---------|
| `task_delegation_bridge.py` | Bridges A2A delegation with SmartLedger. Parent→BLOCKED, child executes, auto-resume. |
| `internal_agent_communication.py` | A2A protocol for inter-agent messaging |

---

## 26. OpenClaw Integration (`integrations/openclaw/`)

| File | Purpose |
|------|---------|
| `shell_openclaw_apis.py` | REST endpoints for ClawHub skill management |

9 endpoints: skill list/install/uninstall/search, status, channels, assistant chat/capabilities/voice.

---

## 27. CLI (`hart_cli.py`)

21 Click subcommands:

| Command | Purpose |
|---------|---------|
| `hart chat` | Interactive chat |
| `hart code` | Coding agent |
| `hart social` | Social platform ops |
| `hart agent` | Agent management |
| `hart expert` | Expert agent network |
| `hart pay` | Payment/Spark ops |
| `hart mcp` | MCP server management |
| `hart compute` | Compute mesh ops |
| `hart channel` | Channel adapter ops |
| `hart a2a` | Agent-to-agent protocol |
| `hart skill` | Skill management |
| `hart voice` | TTS/STT operations |
| `hart vision` | Vision operations |
| `hart desktop` | Desktop management |
| `hart remote` | Remote desktop |
| `hart screenshot` | Screen capture |
| `hart tools` | Tool management |
| `hart recipe` | Recipe management |
| `hart status` | System status |
| `hart repomap` | Repository map (tree-sitter) |
| `hart schedule` | Scheduled tasks |

Headless mode: `hart -p "task"` dispatches to `/chat` endpoint.

---

## 28. NixOS Modules (48)

### Base & Boot

| Module | Purpose |
|--------|---------|
| `hart-base.nix` | Core packages, systemd units |
| `hart-first-boot.nix` | First-boot initialization |
| `hart-kernel.nix` | Kernel config (Binder, Win32 binfmt, cgroups v2, PREEMPT_RT) |
| `hart-subsystems.nix` | Flatpak + AppImage + Android (ART+Binder) + Wine + PWA |

### Security

| Module | Purpose |
|--------|---------|
| `hart-luks.nix` | LUKS2 + TPM2 + swap encryption |
| `hart-firewall.nix` | nftables zones + fwupd firmware updates |
| `hart-sandbox.nix` | Landlock LSM sandboxing |

### Services

| Module | Purpose |
|--------|---------|
| `hart-backend.nix` | Flask backend systemd service |
| `hart-discovery.nix` | Peer discovery daemon |
| `hart-agent.nix` | Agent daemon |
| `hart-llm.nix` | llama.cpp LLM service |
| `hart-vision.nix` | MiniCPM vision sidecar |
| `hart-compute-mesh.nix` | WireGuard mesh, device-to-device compute |
| `hart-model-bus.nix` | Native AI API for all app subsystems |
| `hart-ai-runtime.nix` | Smart FS, predictive prefetch, service intelligence |
| `hart-ota.nix` | 7-stage OTA pipeline with canary deploy |

### Desktop & UI

| Module | Purpose |
|--------|---------|
| `hart-liquid-ui.nix` | Adaptive UI engine (GTK4/WebKit2 + LLM-driven components) |
| `hart-nunba.nix` | Headless Flask + React SPA dashboard |
| `hart-conky.nix` | Lightweight overlay dashboard (metrics, agent status) |
| `hart-nvidia.nix` | NVIDIA drivers, CUDA, persistence mode |
| `hart-cups.nix` | CUPS print server + Avahi + cups-pdf |
| `hart-nightlight.nix` | gammastep/redshift color temperature |
| `hart-ime.nix` | fcitx5/ibus CJK input methods |
| `hart-power.nix` | Power profiles + TLP + agent checkpoint on suspend |
| `hart-accessibility.nix` | Orca + font scaling + high contrast + reduced motion |
| `hart-gaming.nix` | PREEMPT_RT, CPU isolation, Steam/Proton |
| `hart-devtools.nix` | LSP, GDB, containers, linters |
| `hart-app-bridge.nix` | Cross-subsystem clipboard, drag-drop, intents |
| `hart-osk.nix` | On-screen keyboard (squeekboard) |

### Peripherals & Casting

| Module | Purpose |
|--------|---------|
| `hart-peripheral-bridge.nix` | USB/IP + Bluetooth HID + uinput |
| `hart-dlna.nix` | SSDP discovery + MJPEG + MiniDLNA |

### Advanced

| Module | Purpose |
|--------|---------|
| `hart-onboarding.nix` | GTK4/libadwaita first-boot ceremony |
| `hart-self-build.nix` | Runtime NixOS rebuilds (/etc/hart/runtime.nix, atomic generations) |
| `hart-openclaw.nix` | OpenClaw AI assistant bridge (Node.js gateway) |

### Packages

| Package | Purpose |
|---------|---------|
| `hart-app.nix` | Python derivation (Flask + deps, Python 3.10) |
| `hart-cli.nix` | CLI tool derivation |
| `nunba.nix` | Nunba desktop app (PyWebView + React) |

### Configurations

| Config | Target |
|--------|--------|
| `desktop.nix` | GNOME desktop with LiquidUI |
| `server.nix` | Headless server |
| `edge.nix` | Edge/IoT device |
| `phone.nix` | PinePhone mobile |
| `server-minimal-test.nix` | Minimal ISO for testing |

### Hardware Profiles

`raspberry-pi.nix`, `pinephone.nix`, `riscv-generic.nix`

### Infrastructure

| File | Purpose |
|------|---------|
| `flake.nix` | Flake definition: inputs, outputs, hartModules, mkSystem/mkImage builders |
| `vm-tests.nix` | NixOS VM integration tests (QEMU boot tests) |

---

## 29. Port Registry

Single source of truth: `core/port_registry.py`

| Service | App Port | OS Port |
|---------|----------|---------|
| backend | 6777 | 677 |
| discovery | 6780 | 678 |
| vision | 9891 | 989 |
| llm | 8080 | 808 |
| websocket | 5460 | 546 |
| diarization | 8004 | 800 |
| dlna_stream | 8554 | 855 |
| mesh_wg | 6795 | 679 |
| mesh_relay | 6796 | 680 |
| model_bus | 6790 | 681 |

Mode detection: `HART_OS_MODE=true` env var OR `/etc/os-release ID=hart-os`.

---

## 30. Core Infrastructure

### Singletons (25+)

| File | Function | Class |
|------|----------|-------|
| `integrations/agent_engine/revenue_aggregator.py` | `get_revenue_aggregator()` | RevenueAggregator |
| `integrations/agent_engine/federated_aggregator.py` | `get_federated_aggregator()` | FederatedAggregator |
| `integrations/agent_engine/speculative_dispatcher.py` | `get_speculative_dispatcher()` | SpeculativeDispatcher |
| `integrations/agent_engine/world_model_bridge.py` | `get_world_model_bridge()` | WorldModelBridge |
| `integrations/agent_engine/upgrade_orchestrator.py` | `get_upgrade_orchestrator()` | UpgradeOrchestrator |
| `integrations/agent_engine/app_installer.py` | `get_installer()` | AppInstaller |
| `integrations/agent_engine/shard_engine.py` | `get_shard_engine()` | ShardEngine |
| `integrations/agent_engine/benchmark_registry.py` | `get_benchmark_registry()` | BenchmarkRegistry |
| `integrations/agent_engine/agent_baseline_service.py` | `get_baseline_service()` | AgentBaselineService |
| `integrations/social/models.py` | `get_engine()` | SQLAlchemy Engine |
| `core/platform/registry.py` | `get_registry()` | ServiceRegistry |
| `core/platform/cache.py` | `get_cache()` | CacheService |
| `core/peer_link/link_manager.py` | `get_link_manager()` | PeerLinkManager |
| `core/peer_link/message_bus.py` | `get_message_bus()` | MessageBus |
| `core/resonance_tuner.py` | `get_resonance_tuner()` | ResonanceTuner |
| *(10+ more)* | | |

### Utility Modules

| File | Purpose |
|------|---------|
| `core/event_loop.py` | Thread-local reusable event loop (replaces 7+ `asyncio.new_event_loop()`) |
| `core/circuit_breaker.py` | CLOSED→OPEN→HALF_OPEN pattern, thread-safe |
| `core/session_cache.py` | TTL-based auto-expiring dict with max_size cap |
| `core/file_cache.py` | mtime-based JSON file cache (90%+ I/O reduction) |
| `core/config_cache.py` | Cached config.json reader |
| `core/http_pool.py` | `pooled_get()`/`pooled_post()` connection pooling |
| `threadlocal.py` | Thread-safe request context (user_id, token counts) |
| `exception_collector.py` | Fire-and-forget exception aggregation for self-healing |
| `cultural_wisdom.py` | 30+ cultural traits embedded in every agent (Ubuntu, Ahimsa, etc.) |
| `agent_identity.py` | HART identity constants, platform identity |

---

## 31. Design Patterns

### Singleton Pattern
Module-level `_instance = None` + `get_*()` factory. 25+ instances.

### DB Session Pattern
Always `db_session()` context manager. Never manual get_db/try/finally/close.

### Notification Pattern
Always `NotificationService.create()`. Never construct `Notification()` directly.

### HTTP Pool Pattern
Always `pooled_get()`/`pooled_post()`. Never bare `requests.get()`/`requests.post()`.

### GPU Detection Pattern
Single source: `vram_manager.detect_gpu()`. Never inline `torch.cuda.*`.

### Lazy Import Pattern
Service tools lazy-import heavy ML libraries at first use, not at startup.

### Atomic File Write Pattern
`tmp_path + os.replace()` for crash-safe persistence.

### Flask Error Handling
`@_json_endpoint` decorator for automatic try/except/jsonify.

### Tool Closure Factory
`build_core_tool_closures(ctx)` — single source of truth for all agent tools. Both CREATE and REUSE call this (DRY).

### Circuit Breaker
CLOSED→OPEN→HALF_OPEN for external service calls. Configurable threshold/cooldown.

### EventBus Pub/Sub
Decoupled subsystems via `emit_event()` + optional WAMP bridge. Wildcard subscriptions.

### Fire-and-Forget
Exception collection, recipe experience recording, notifications — never block main execution.

---

## 32. Environment Variables

### Core

| Variable | Default | Purpose |
|----------|---------|---------|
| `HEVOLVE_NODE_TIER` | `flat` | Network tier: flat/regional/central |
| `HEVOLVE_NODE_ID` | `local` | Unique node identifier |
| `HEVOLVE_USER_ID` | | Authenticated user identity |
| `HEVOLVE_DEV_MODE` | `false` | Dev features (forced off on central) |
| `HEVOLVE_ENFORCEMENT_MODE` | `hard` | Security enforcement level |
| `HEVOLVE_DB_PATH` | `agent_data/hevolve_database.db` | Database path |
| `HEVOLVE_KEY_DIR` | `agent_data` | Cryptographic key storage |
| `HEVOLVE_DATA_KEY` | | Fernet key for at-rest encryption |
| `HEVOLVE_AGENT_POLL_INTERVAL` | `30` | Agent daemon tick interval (seconds) |
| `HEVOLVE_SPECULATIVE_ENABLED` | `false` | Enable speculative dispatch |
| `HEVOLVE_FORCE_TIER` | | Override capability tier for testing |
| `HART_OS_MODE` | `false` | OS mode (privileged ports) |

### LLM

| Variable | Default | Purpose |
|----------|---------|---------|
| `HEVOLVE_LLM_ENDPOINT_URL` | | Custom LLM endpoint |
| `HEVOLVE_LLM_MODEL_NAME` | `gpt-4.1-mini` | Primary model |
| `HEVOLVE_LOCAL_LLM_URL` | `http://localhost:8000/v1` | Local LLM endpoint |
| `HEVOLVE_ACTIVE_CLOUD_PROVIDER` | | Cloud provider name |

### Networking

| Variable | Default | Purpose |
|----------|---------|---------|
| `CBURL` | | Crossbar WAMP URL |
| `CBREALM` | | Crossbar realm |
| `REDIS_URL` | `redis://localhost:6379/1` | Redis URL |
| `HEVOLVEAI_API_URL` | `http://localhost:8000` | HevolveAI service URL |

### Port Overrides

`HARTOS_BACKEND_PORT`, `HART_DISCOVERY_PORT`, `HART_VISION_PORT`, `HART_LLM_PORT`, `HART_WS_PORT`, `HART_MESH_WG_PORT`, `HART_MESH_RELAY_PORT`

---

## 33. API Endpoints (430+)

### Main Application (`langchain_gpt_api.py`) — 47 endpoints

**Core**: POST `/chat`, POST `/time_agent`, POST `/visual_agent`, POST `/add_history`, GET `/status`, GET `/health`, GET `/ready`, POST `/zeroshot/`

**Tools**: GET/POST `/api/tools/{status,setup,start,stop,unload,vram,lifecycle}`

**Voice**: POST `/api/voice/{speak,transcribe,clone}`, GET `/api/voice/{voices,engines,audio/<file>}`

**Instructions**: POST `/api/instructions/{enqueue,batch,drain,cancel,complete,fail}`, GET `/api/instructions/{pending,plan}`

**Remote Desktop**: GET/POST `/api/remote-desktop/{status,host,connect,sessions,disconnect,engines,select-engine}`

**Settings**: GET/PUT `/api/settings/compute`, GET/POST `/api/settings/compute/provider{,/join}`

**Coding**: GET/POST `/coding/{tools,execute,benchmarks,install}`

**Skills**: GET/POST/DELETE `/api/skills/{list,ingest,discover/local,discover/github,<name>}`

### Social API (`integrations/social/`) — 300+ endpoints

Core social (117), Games (19), Gamification (85), Thought Experiments (13), Compute Pledges (9), Dashboard (5), Audit (9), Theme (6), Learning (9), Content Gen (6), Sharing (8), Tracker (15), Provision (7), Regional Host (6), Sync (6), Fleet (1), Discovery (42).

### Shell Management — 131 endpoints

Shell OS (57), Shell Desktop (46), Shell System (28).

### LiquidUI — 63 endpoints

Core UI, app management, system metrics, networking, audio, display, notifications.

### Agent Engine — 64 endpoints

Goals, products, IP/patents, commercial API, model bus, compute mesh, build distribution, app bridge.

### Other — 32 endpoints

Coding Agent (7), Distributed Agent (11), OpenClaw (9), Onboarding (4), Flask Integration (2).

---

## 34. Test Architecture

### Patterns

- `--noconftest` flag for all runs (avoids tempfile corruption)
- `-p no:capture` for federation tests
- Python 3.11 active, `autogen` not installed (9 files skip)
- Pre-existing: ~70 failures across 27 files (not caused by recent changes)

### Test Suites

| Suite | File | Tests |
|-------|------|-------|
| PeerLink | `test_peer_link.py` | 135 |
| Encryption at Rest | `test_encryption_at_rest.py` | 34 |
| Channel Encryption | `test_channel_encryption.py` | 24 |
| Security (WS11-13) | `test_ws11_*.py` – `test_ws13_*.py` | 235 |
| Platform | `test_platform_*.py` | 223 |
| TTS Router | `test_tts_router.py` | ~40 |
| Remote Desktop | 12 test files | 316 |
| Resonance + Personality | 2 test files | 100 |
| Civic Sentinel | `test_civic_sentinel.py` | 40 |

---

## 35. Dependencies

### Critical Pinned

| Package | Version | Reason |
|---------|---------|--------|
| `langchain` | 0.0.230 | Monolithic pre-split package |
| `pydantic` | 1.10.9 | Requires Python 3.10-3.11 |
| `cryptography` | >= 41.0 | Ed25519, X25519, AES-GCM, Fernet |

All imports use `from langchain.X` (NOT langchain_classic, langchain_community).

### Optional Groups

| Group | Packages | Purpose |
|-------|----------|---------|
| `remote-desktop` | mss, websockets, av, pynput | Remote desktop |
| `tts` | pocket-tts, sherpa-onnx | Offline TTS |
| `vision` | transformers, torch | Vision models |
| `coding` | diskcache, grep-ast, tree-sitter, gitpython | Coding agent |

---

## 36. File Tree Summary

```
HARTOS/
├── langchain_gpt_api.py        # Flask entry point (port 6777), 430+ endpoints
├── create_recipe.py             # CREATE mode pipeline
├── reuse_recipe.py              # REUSE mode pipeline
├── helper.py                    # Action class, JSON utils
├── lifecycle_hooks.py           # ActionState machine
├── helper_ledger.py             # SmartLedger factory
├── hart_cli.py                  # CLI (21 subcommands)
├── hart_onboarding.py           # "Light Your HART" ceremony
├── agent_identity.py            # HART identity constants
├── cultural_wisdom.py           # 30+ cultural traditions
├── exception_collector.py       # Fire-and-forget exception aggregation
├── recipe_experience.py         # Recipe telemetry recording
├── embedded_main.py             # Headless IoT/robot entry point
├── crossbar_server.py           # WAMP component
├── threadlocal.py               # Thread-local request context
│
├── core/
│   ├── platform/                # OS substrate (15 files)
│   ├── peer_link/               # P2P communication (7 files)
│   ├── agent_tools.py           # 22 canonical tool definitions
│   ├── port_registry.py         # Port assignments
│   ├── http_pool.py             # Connection pooling
│   ├── resonance_profile.py     # Per-user 8-dim tuning
│   ├── resonance_tuner.py       # EMA tuner + signal extraction
│   ├── resonance_identifier.py  # Biometric dispatch to HevolveAI
│   ├── agent_personality.py     # Agent personality traits
│   ├── event_loop.py            # Thread-local event loop
│   ├── circuit_breaker.py       # Circuit breaker pattern
│   ├── session_cache.py         # TTL auto-expiring dict
│   ├── file_cache.py            # mtime-based JSON cache
│   └── config_cache.py          # Cached config reader
│
├── security/                    # 20 files (see Section 7)
│
├── integrations/
│   ├── agent_engine/            # 35+ files (see Section 5)
│   ├── social/                  # 50+ files (see Section 14)
│   ├── channels/                # 80+ files (see Section 15)
│   ├── coding_agent/            # 8 files (see Section 19)
│   ├── distributed_agent/       # 5 files (see Section 20)
│   ├── remote_desktop/          # 15 files (see Section 18)
│   ├── service_tools/           # 18+ files (see Section 16)
│   ├── vision/                  # 5 files (see Section 17)
│   ├── audio/                   # Diarization server + service
│   ├── mcp/                     # MCP server + integration
│   ├── ap2/                     # Agent Protocol 2 (payments)
│   ├── expert_agents/           # 96 specialized agents (see Section 21)
│   ├── internal_comm/           # A2A communication (see Section 25)
│   ├── openclaw/                # OpenClaw integration (see Section 26)
│   └── google_a2a/              # Dynamic agent registry
│
├── nixos/                       # 48 NixOS modules (see Section 28)
├── tests/                       # Unit + integration tests
└── docs/                        # Documentation
```

---

## 37. Review Notes for Open-Sourcing

### Items to Verify Before Release

1. **`MASTER_PUBLIC_KEY_HEX`** in `security/master_key.py` — ensure this is the production public key, not a test key. The private key must NEVER be in the repo.

2. **API keys in config.json** — ensure `.gitignore` covers `config.json`, `.env`, `secrets.enc`. No leaked tokens.

3. **HevolveAI binary paths** — `native_hive_loader.py` references specific paths. Ensure no internal infrastructure URLs are hardcoded.

4. **Build distribution license model** — `build_distribution.py` implements a license system. Verify this aligns with BSL-1.1 open-source intent.

5. **Cultural wisdom sources** — `cultural_wisdom.py` references 30+ traditions. Verify respectful attribution and cultural sensitivity.

6. **Revenue split constants** — 90/9/1 appears in `revenue_aggregator.py`, `ad_service.py`, `hosting_reward_service.py`, `finance_tools.py`. All must be consistent.

7. **Trusted domains** — `key_delegation.py` hardcodes `hevolve.ai`, `hertzai.com`. Fork-friendly configuration needed or document this.

8. **Origin attestation** — `origin_attestation.py` fingerprint ties to Hevolve.ai identity. Forks will need to update this (by design — prevents unauthorized federation).

9. **Test failures** — ~70 pre-existing failures across 27 files. Document which are known issues vs regressions.

10. ~~**Video captions**~~ — **RESOLVED.** Full video captioning pipeline exists: MiniCPM VLM sidecar (`minicpm_server.py`) describes camera/screen frames, FrameStore (`frame_store.py`) provides thread-safe in-process storage, VisionService (`vision_service.py`) orchestrates intelligent sampling (adaptive 4s-30s intervals, only on scene change), and descriptions persist to the activity ledger via `/create_action` with `zeroshot_label='Video Reasoning'`. Raw frames also forward to HevolveAI for visual learning via `submit_sensor_frame()`.

---

*This document is the single source of truth for the HART OS codebase. Every subsystem, mechanism, protocol, pattern, tool, endpoint, database table, NixOS module, and design decision is documented here. Nothing is left out.*
