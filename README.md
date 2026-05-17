<h1 align="center">HART OS</h1>
<p align="center"><strong>Hevolve Hive Agentic Runtime</strong></p>
<p align="center">Self-improving Python agent runtime. Local-first, federated, OpenAI-compatible.</p>

<p align="center">
  <a href="https://hevolve.ai"><img src="https://img.shields.io/badge/Live%20demo-hevolve.ai-FFD700?style=flat-square" alt="Live demo"></a>
  <a href="https://docs.hevolve.ai"><img src="https://img.shields.io/badge/Docs-docs.hevolve.ai-blueviolet?style=flat-square" alt="Docs"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square" alt="Python 3.10+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-green?style=flat-square" alt="License"></a>
  <a href="https://github.com/hertz-ai/Nunba"><img src="https://img.shields.io/badge/Frontend-Nunba-5865F2?style=flat-square" alt="Nunba"></a>
</p>

> **HART** = bare engine (`pip install hart-backend`, listens on `:6777`).
> **HART OS** = HART + admin desktop (model catalog, channel pairing, agent dashboard, hive view, settings).
> **[Nunba](https://github.com/hertz-ai/Nunba)** = consumer companion app, signed Windows / macOS / Linux installers.

One Python codebase, three deploy topologies (flat laptop / regional LAN / central cloud mesh). Speaks the OpenAI protocol on `:6777/v1/chat/completions`. Federates with peer nodes over PeerLink (direct P2P WebSocket, no broker). Boot-time guardrail hash + 300 s re-verification + Ed25519 release signing.

---

## 60-second start

```bash
git clone https://github.com/hertz-ai/HARTOS.git && cd HARTOS
python3.10 -m venv venv && source venv/Scripts/activate   # Windows: venv\Scripts\activate.bat
pip install -r requirements.txt
echo "OPENAI_API_KEY=sk-..." > .env       # or GROQ_API_KEY, or none for local llama.cpp
python hart_intelligence_entry.py         # listens on :6777
```

```bash
# OpenAI-compatible (drop-in for any OpenAI SDK / LangChain / LiteLLM / Aider / Continue)
curl -X POST http://localhost:6777/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "hevolve", "messages": [{"role": "user", "content": "Hello"}]}'
```

[Live demo](https://hevolve.ai) · [Full quickstart](https://docs.hevolve.ai/getting-started/quickstart/) · [Nunba desktop](https://github.com/hertz-ai/Nunba)

---

## How it compares

| | HART OS | OpenAI Agents | LangChain | AutoGen |
|---|---|---|---|---|
| Self-improves at runtime (auto-evolve loop + RSI-2 gate) | yes | no | no | partial |
| Continuous baselining vs prior snapshots | yes (`agent_baseline_service.py`) | no | no | no |
| Built-in benchmark adapters | 7 (registry-driven) | n/a | n/a | n/a |
| Federates across peer nodes | yes (PeerLink + hash-verified) | no | no | no |
| Local-first multimodal | yes (llama.cpp + Whisper + 6 TTS + VLM) | no | partial | partial |
| Channel adapters out of the box | 31 | 1 (webhook) | custom | custom |
| One codebase, multiple topologies | flat / regional / central | hosted only | library | library |
| OpenAI-compatible endpoint | yes | yes | bring your own | bring your own |
| Recipe replay (cached LLM steps) | yes (90% faster) | no | no | no |
| Native source protection (HevolveArmor) | yes | n/a | n/a | n/a |

---

## Capabilities

### Agent runtime

| | What it does | Where |
|---|---|---|
| **CREATE / REUSE recipe pattern** | Run a task once via LLM, save the trace, replay 90% faster with no LLM calls on cached steps | `create_recipe.py`, `reuse_recipe.py` |
| **GoalManager** | Unified goal lifecycle, guardrail-gated state machine, escalation hooks | `integrations/agent_engine/goal_manager.py` |
| **AgentDaemon** | Autonomous tick loop, circuit breaker, frozen-thread detection | `integrations/agent_engine/agent_daemon.py` |
| **SpeculativeDispatcher** | Fast draft model answers first, expert agent takes over if confidence drops | `speculative_dispatcher.py` |
| **ParallelDispatch** | ThreadPoolExecutor fan-out across SmartLedger tasks | `parallel_dispatch.py` |
| **SelfHealingDispatcher** | Catches transient failures, retries with backoff + alternative providers | `self_healing_dispatcher.py` |
| **96 expert agents** | Coding, research, marketing, product, security, ethics, ops, ... auto-dispatched per goal | `integrations/expert_agents/` |
| **Recipe Pattern + Aider** | In-process Aider backend (no subprocess) for code edits | `integrations/coding_agent/aider_native_backend.py` |

### Auto-evolve, baselining, benchmarking

| | What it does | Where |
|---|---|---|
| **AutoEvolve loop** | Realtime: hypothesis -> 33-rule filter -> hive vote -> parallel sandbox -> RSI-2 gate -> federated broadcast | `integrations/agent_engine/auto_evolve.py` |
| **RSI-2 monotonic gate** | New release must beat prior baseline on every benchmark by configurable margin or PR is rejected | `rsi_trigger.py`, `pr_review_service.py` |
| **Benchmark registry** | 7 built-in adapters (3 sourced from HevolveAI: QuantiPhy, Embodied, Qwen). Pluggable via `register_adapter()` | `benchmark_registry.py` |
| **Per-agent baselines** | Per-agent snapshots at `agent_data/baselines/<agent_id>.json`, used as the regression floor | `agent_baseline_service.py` |
| **Coding benchmark tracker** | SQLite-backed coding benchmarks (`coding_benchmarks.db`), HumanEval / MBPP / custom suites | `integrations/coding_agent/benchmark_tracker.py` |
| **Hive benchmark prover** | Cryptographic proof that a benchmark was run on the claimed model + dataset (resists fake-score federation) | `hive_benchmark_prover.py` |
| **Continual learner gate** | Blocks model swap if learner forgets prior tasks (catastrophic-forgetting guard) | `continual_learner_gate.py` |
| **PR review service** | Auto-rejects PRs on baseline regression or guardrail mismatch | `pr_review_service.py` |
| **Upgrade orchestrator** | 7-stage pipeline: BUILD -> TEST -> AUDIT -> BENCHMARK -> SIGN -> CANARY -> DEPLOY | `upgrade_orchestrator.py` |
| **OTA service** | systemd service does daily check + cryptographically-verified upgrade | `hart-update-service.py` |

### Hive connectivity + federation

| | What it does | Where |
|---|---|---|
| **PeerLink** | Direct P2P WebSocket mesh, trust-aware encryption (same-user devices skip overhead, cross-user E2E), works offline on LAN, across the internet, multi-device | `core/peer_link/` |
| **NAT traversal** | UDP hole-punching, STUN-style fallbacks for residential NATs | `core/peer_link/nat.py` |
| **Hivemind handler** | Tier-aware routing (flat / regional / central), connection budget per tier (10 / 50 / 200) | `core/peer_link/hivemind_handler.py` |
| **FederatedAggregator** | Equal-weighted delta merging (log1p-floor, not hardware tier). Channels: model deltas, resonance, recipes, event counters | `federated_aggregator.py` |
| **Federated gradient protocol** | Cross-node gradient sharing with provenance tags, no raw data leaves the node | `federated_gradient_protocol.py` |
| **Federation handshake** | Peer presents guardrail hash; mismatch = connection refused; re-verified every 300 s | `integrations/social/federation.py` |
| **Gossip + verification** | Tier-aware gossip with cert verification, peer-verified task results | `integrity_service.py`, gossip layer |
| **EventBus + WAMP bridge** | In-process EventBus auto-publishes to Crossbar WAMP when `CBURL` env set; remote nodes subscribe to `com.hartos.event.*` topics | `core/platform/events.py` |
| **Federated equality** | Tier multipliers replaced with `log1p(interactions)` floor=1.0. A Pi node has the same vote weight as a GPU rack at equal participation | (federation rule) |
| **Hive contests** | Open contests on the network; agents propose, hive votes, winners federate | `hive_contest.py` |
| **Native hive loader** | Loads closed-source HevolveAI binary at runtime with master-key signature verification, falls back to stub | `security/native_hive_loader.py` |

### Idea engine (thought experiments)

| | What it does | Where |
|---|---|---|
| **Thought experiment** | Propose an idea, community votes (humans + agents, confidence-weighted), believers pledge compute | `api_thought_experiments.py`, `experiment_discovery_service.py` |
| **ComputePledge** | Spark-budget pledge to a specific experiment, redeemable on idle GPUs across the network | `compute_borrowing.py`, `compute_mesh_service.py` |
| **Type-aware agents** | `software` / `traditional` / `physical_ai` / `code_evolution` agent types per experiment | `dispatch.py` |
| **Agent Hive View** | Real-time swarm visualization, encounter lines (collaboration), inject mid-experiment variables | `api_hive_contest.py` + Nunba UI |
| **Reasoning trace** | Per-agent reasoning capture, queryable post-completion ("interview the agent") | `reasoning_trace.py` |

### LLM + multimodal

| | What it does | Where |
|---|---|---|
| **15 LLM providers** | Local llama.cpp, OpenAI, Anthropic, Google Gemini, Groq, Mistral, DeepSeek, OpenRouter, Together, Fireworks, Cohere, Perplexity, Hugging Face, Ollama, custom OpenAI-compatible | `integrations/providers/` |
| **Universal gateway** | One router, cost / latency / capability scoring, AES-256 keys at rest (PBKDF2 KDF) | `model_registry.py`, `model_bus_service.py` |
| **Speculative decoding** | Qwen3-0.8B draft + Qwen3-4B main, ~300 ms TTFT on consumer hardware | `speculative_dispatcher.py` |
| **Faster-Whisper STT** | Local STT, multi-lang, GPU-accelerated when available | `integrations/service_tools/whisper_tool.py` |
| **MiniCPM VLM** | Vision-language model for camera + screenshot reasoning | `integrations/vision/minicpm_server.py` |
| **6 TTS engines** | Indic Parler (22 Indic + EU), Chatterbox Turbo (English expressive), Kokoro (English neural), CosyVoice3 (en/zh), F5 (zero-shot voice clone), Piper (CPU fallback) | `integrations/channels/media/`, `tts.py` |
| **Auto-VRAM tiering** | Detects GPU + free VRAM, picks largest model that fits with headroom; degrades gracefully on 6 GB cards | `core/gpu_tier.py`, `vram_manager.py` |

### Channels (31 adapters)

| Surface | Adapters |
|---|---|
| **Core chat** | Telegram, Discord, Slack, WhatsApp, Signal, iMessage (BlueBubbles), Teams, Web SPA |
| **Enterprise** | Mattermost, Matrix, Nextcloud, Rocket.Chat |
| **Social** | Messenger, Instagram, Twitter / X, LINE, Viber, WeChat, Twitch |
| **Decentralized** | Nostr, Tlon (Urbit), OpenProse |
| **Bridge variants** | TelegramUser, DiscordUser, BlueBubbles, ZaloUser |
| **Other** | Email (IMAP/SMTP), SMS (Twilio), Google Chat |

`ResponseRouter` fan-out + WAMP desktop mirror; per-channel agent + prompt assignment; AutoGen-side tools so agents can register/send via channels themselves. [Catalog endpoint](#api-surface): `GET /api/social/channels/catalog`.

### Personality + resonance

| | What it does | Where |
|---|---|---|
| **AgentPersonality** | 8-dim personality dataclass (warmth, formality, verbosity, ...), generates per-agent system prompt | `core/agent_personality.py` |
| **UserResonanceProfile** | 8-dim continuous floats (0-1), stored at `agent_data/resonance/<user_id>.json` | `core/resonance_profile.py` |
| **ResonanceTuner** | EMA tuning (alpha 0.15) from dialogue signals, federated-delta export, oscillation detector | `core/resonance_tuner.py` |
| **ResonanceIdentifier** | Thin proxy that dispatches biometric ops (face, voice) to HevolveAI sibling. No ML in HART OS. | `core/resonance_identifier.py` |

### Memory + knowledge

| | What it does | Where |
|---|---|---|
| **MemoryGraph** | SQLite FTS5 + `memory_links` table, provenance-aware | `core/memory/` |
| **SimpleMem** | Semantic vector search for long-term recall | (vector store) |
| **PersistentChatHistory** | Single shared buffer for LangChain + AutoGen (zero parallel paths) | (conversation buffer) |
| **ConversationEntry** | Cross-channel unified conversation log | `chat_messages.py` |
| **Embedding delta** | Per-node embedding deltas federated back to the hive | `embedding_delta.py` |

### Distributed compute + economics

| | What it does | Where |
|---|---|---|
| **3-tier topology** | flat (single device, SQLite WAL) -> regional (LAN/VPN, MySQL QueuePool) -> central (cloud, Docker mesh) | env-detected, single code path |
| **SmartLedger** | 15-state task lifecycle, parallel + sequential dispatch, ledger persistence per user | `helper_ledger.py`, `lifecycle_hooks.py` |
| **ComputeMesh** | Match compute supply (idle GPUs, Nunba desktops) to demand (inference, training, experiments) | `compute_mesh_service.py` |
| **ComputeEscrow** | Persistent escrow for pledged compute, replaces in-memory `_compute_debts` | (DB table in `models.py`) |
| **BudgetGate** | Local models (llama / mistral / phi / qwen / groq) cost 0 Spark; cloud models per-1k-token cost | `budget_gate.py` |
| **MeteredAPIUsage** | Per-call metering for cost recovery on metered providers | `models.py: MeteredAPIUsage` |
| **NodeComputeConfig** | Per-node policy: GPU hours served, total inferences, energy contributed, electricity rate, cause alignment | `models.py: NodeComputeConfig` |
| **AdService** | Peer-witnessed impressions (70% witnessed payout, 50% unwitnessed) | `ad_service.py` |
| **HostingRewardService** | Reward score weighted by gpu_hours / inferences / energy / api_costs | `hosting_reward_service.py` |
| **RevenueAggregator** | 90 / 9 / 1 split (users / infra / central). Single source of truth for all revenue queries | `revenue_aggregator.py` |
| **Compute democracy** | Logarithmic reward scaling, max 5% influence per entity, +20% diversity bonus | (constitutional rule, enforced) |
| **Audit invariant** | Combined compute of nodes auditing any single node must exceed that node's compute | (network self-enforces) |

### Security + governance

| | What it does | Where |
|---|---|---|
| **HiveGuardrails** | 10-class guardrail network. Frozen Python (`__slots__=()`, blocked `__setattr__`), SHA-256 hash verified at boot + every 300 s. Gossip peers reject mismatched hashes. | `security/hive_guardrails.py` |
| **MasterKey** | Ed25519. Signs releases, triggers `HiveCircuitBreaker` (network-wide kill switch). `MASTER_PUBLIC_KEY_HEX` is the immutable trust anchor. | `security/master_key.py` |
| **3-tier cert chain** | central -> regional -> local, short-TTL local certs | `security/key_delegation.py` |
| **RuntimeMonitor** | Background tamper-detection daemon, frozen-thread detection, auto-restart | `security/runtime_monitor.py`, `security/node_watchdog.py` |
| **ImmutableAuditLog** | SHA-256 hash chain, `AuditLogEntry` table, tamper detection on read | `security/immutable_audit_log.py` |
| **Tool allowlist** | FAST = read-only, BALANCED = read-write, EXPERT = unrestricted | `tool_allowlist.py` |
| **ActionClassifier** | Destructive pattern detection, `PREVIEW_PENDING` / `APPROVED` states for risky actions | `security/action_classifier.py` |
| **DLP engine** | PII scan + redact (email, phone, SSN, credit card), outbound gating | `security/dlp_engine.py` |
| **Rate limiter** | Redis-backed; goal_create limited to 10/hour, /chat at 30/min on central instance | `security/rate_limiter_redis.py` |
| **Boot hardening** | Tier authorization at boot, dev mode forced off on central (3 layers), TLS check, secret validation, DB encryption check | `__init__.py`, `start_cloud.sh` |
| **Origin attestation** | Cryptographic origin proof. Federation handshake requires signed attestation. Anti-rebranding. | `security/origin_attestation.py` |
| **HSM trust** | HSM provider abstraction for key custody | `security/hsm_trust.py`, `security/hsm_provider.py` |

### HevolveArmor (source protection)

| | What it does | Where |
|---|---|---|
| **AES-256-GCM at rest** | Python modules encrypted at rest with derived key | `core/security/` (Rust-native) |
| **Ed25519 key derivation** | node_identity -> HKDF -> AES key | (key derivation) |
| **BCC mode** | Cython compile-to-C, irreversible | (build flag) |
| **RFT mode** | AST symbol renaming | (build flag) |
| **Anti-debug, anti-tamper** | Process introspection guards, license management | (Rust binary) |
| **Test coverage** | 54 tests (unit + integration + stress + e2e + pen) | `tests/` |

### Platform layer (HART OS specific)

| | What it does | Where |
|---|---|---|
| **ServiceRegistry** | Dynamic service discovery + lifecycle | `core/platform/registry.py` |
| **AppRegistry** | 9 manifest types (chat, panel, channel, agent, plugin, ...) | `core/platform/app_registry.py` |
| **AppManifest + validator** | Schema-validated app manifests | `core/platform/manifest_validator.py` |
| **EventBus** | In-process pub/sub + WAMP bridge for cross-node events | `core/platform/events.py` |
| **Bootstrap** | Migrates 55 shell_manifest panels, registers services, detects native apps, loads extensions, starts PeerLink | `core/platform/bootstrap.py` |
| **CapabilityRouter** | Routes capability requests to the right service | `core/platform/registry.py` |
| **EnvironmentManager** | OS detection (NixOS / generic Linux / macOS / Windows), env-specific routing | `core/platform/agent_environment.py` |
| **Extensions** | Sandboxed extension loader with manifest gating | `core/platform/extensions.py`, `extension_sandbox.py` |
| **PrGuardian** | Auto-reject PR on guardrail / baseline regression | `core/platform/pr_guardian.py` |

### Desktop / OS management (Nunba surface)

| | What it does | Where |
|---|---|---|
| **Shell APIs** | 40+ OS routes (`shell_os_apis.py`), 9 desktop features (`shell_desktop_apis.py`), 6 system features (`shell_system_apis.py`) | `integrations/agent_engine/shell_*.py` |
| **App installer** | Cross-platform: Nix, Flatpak, AppImage, Wine, Android, Darling. Magic-bytes detection | `integrations/social/app_installer.py` |
| **NixOS modules** | OTA, NVIDIA, LUKS, firewall, power, accessibility, CUPS, nightlight, IME | (nix flakes) |
| **Liquid UI service** | MD3 design tokens, JS component lib (dsBtn, dsCard, dsModal) | `liquid_ui_service.py` |
| **Theme service** | EventBus-driven theme distribution | `theme_service.py` |
| **Native remote desktop** | RustDesk + Sunshine wrappers, 3-tier transport (DirectWS / WAMP / WireGuard), OTP session auth, DLP scan, peripheral bridge, DLNA casting | `integrations/remote_desktop/` |
| **System panels** | 36 panels (model catalog, channel pairing, agent dashboard, hive view, ...) | `shell_manifest.py` |
| **Unified hart CLI** | 21 subcommands: chat, code, social, agent, expert, pay, mcp, compute, channel, a2a, skill, voice, vision, desktop, remote, screenshot, tools, recipe, status, repomap, schedule, zeroshot | `hart_cli.py` |

### Vision + robotics

| | What it does | Where |
|---|---|---|
| **Vision sidecar** | MiniCPM VLM server, screenshot + camera frame reasoning | `integrations/vision/` |
| **Embodied AI bridge** | Frame store + VLM grounding + actuator dispatch (universal robot API) | `integrations/vision/`, `integrations/robotics/` |
| **OpenClaw** | Computer-use action library | `integrations/openclaw/` |

### Other integrations

- **Agent Protocol 2** (e-commerce, payments) - `integrations/ap2/`
- **Google A2A** (dynamic agent registry) - `integrations/google_a2a/`
- **MCP (Model Context Protocol)** servers - `integrations/mcp/`
- **Internal A2A** (task delegation between agents) - `integrations/internal_comm/`
- **Skills** (reusable capability bundles) - `integrations/skills/`
- **Marketing tools** (campaigns, content gen, video orchestrator) - `integrations/marketing/`, `marketing_tools.py`, `video_orchestrator.py`
- **Trading agents** (SmartLedger-tracked, budget-gated) - `trading_tools.py`
- **Coding agent** (idle compute -> distributed code tasks via Aider in-process) - `integrations/coding_agent/`
- **Web crawler** - `integrations/web_crawler.py`
- **Kids learning** (25+ educational game templates) - `api_games.py`

---

## Hello, agent

```python
import requests

# 1. CREATE: teach a task once, save the trace as a recipe
requests.post("http://localhost:6777/chat", json={
    "user_id": "alice",
    "prompt_id": "research_assistant",
    "prompt": "Find arXiv papers from the last week on speculative decoding",
    "create_agent": True,
})

# 2. REUSE: replay the recipe, no LLM calls on cached steps
for query in ["mixture of experts", "constitutional AI"]:
    res = requests.post("http://localhost:6777/chat", json={
        "user_id": "alice",
        "prompt_id": "research_assistant",
        "prompt": query,
    })
    print(res.json()["output"])
```

Custom tools, channel bindings, agent plugins: [docs.hevolve.ai/agent-plugin](https://docs.hevolve.ai/agent-plugin/).

---

## Architecture map

```
HART OS  (port 6777)
|-- Engine            CREATE -> save Recipe -> REUSE (90% faster replay)
|-- Agent runtime     GoalManager . AgentDaemon . SpeculativeDispatch . ParallelDispatch . AutoEvolve
|-- Auto-evolve       Hypothesis -> 33-rule filter -> hive vote -> sandbox -> RSI-2 gate -> federate
|-- Baselining        agent_baseline_service . benchmark_registry . benchmark_tracker . hive_benchmark_prover
|-- Memory            Shared LangChain + AutoGen buffer (zero parallel paths) . MemoryGraph . SimpleMem
|-- Channels (31)     ResponseRouter fan-out + WAMP desktop mirror + per-channel agent binding
|-- Providers (15)    Universal gateway . AES-256 keys . cost/latency/capability routing
|-- Multimodal        Whisper STT . 6 TTS engines . MiniCPM VLM . VRAM-tiered
|-- Hive              PeerLink P2P (NAT-traversed) . FederatedAggregator (equal-weighted)
|                     Gossip + verification . hash-gated handshake . EventBus + WAMP bridge
|-- Idea Engine       Thought experiments . ComputePledge . type-aware agents . Hive View
|-- Compute           3-tier topology (flat/regional/central) . SmartLedger . ComputeMesh . ComputeEscrow
|-- Economics         AdService (70/50) . RevenueAggregator (90/9/1) . log-scaled compute democracy
|-- Security          33 guardrails . Ed25519 master key . 3-tier cert chain . RuntimeMonitor
|                     ImmutableAuditLog . tool allowlist . ActionClassifier . DLP . rate limiter
|-- HevolveArmor      AES-256-GCM modules . BCC compile-to-C . RFT AST renaming . anti-debug
|-- Platform          ServiceRegistry . AppRegistry . AppManifest . Bootstrap . EnvironmentManager
|-- Desktop           Shell APIs . app installer . NixOS modules . LiquidUI . themes . remote desktop
|-- CLI               hart (21 subcommands) . OpenAI-compatible client . Aider in-process backend
`-- Other             AP2 . Google A2A . MCP . skills . marketing . trading . coding agent . vision
```

Full architecture: [docs.hevolve.ai/architecture](https://docs.hevolve.ai/architecture/overview/).

---

## API surface

```
POST /chat                                Core agent (LangChain + AutoGen)
POST /v1/chat/completions                 OpenAI-compatible (drop-in)
POST /time_agent                          Scheduled task execution
POST /visual_agent                        VLM + computer use
GET  /status                              Health

# Hive + thought experiments
POST /api/social/experiments/auto-evolve  Start evolution cycle
GET  /api/social/hive/active              All parallel agents (Hive View)
POST /api/social/hive/<id>/inject         Inject mid-experiment variable
GET  /api/social/tracker/experiments      Experiment tracker with task progress

# Channels
GET  /api/social/channels/catalog         All 31 channels + capabilities
POST /api/social/channels/bindings        Bind a channel to an agent
POST /api/social/channels/pair/generate   QR for cross-device pairing

# Goals + dashboard
POST /api/goals                           Create an autonomous goal
GET  /api/social/dashboard/agents         Truth-grounded agent overview

# Compute + earnings
GET  /api/compute-earnings/summary        Per-node earnings breakdown
GET  /api/settings/compute                Local compute policy
PUT  /api/settings/compute                Update policy
PUT  /api/settings/provider               Provider config (cause alignment, electricity rate)
PUT  /api/settings/provider/join          Opt into provider role

# A2A protocol
GET  /a2a/<prompt_id>_<flow_id>/.well-known/agent.json
POST /a2a/<prompt_id>_<flow_id>/execute
```

195+ endpoints total. [Full reference](https://docs.hevolve.ai/api/core/).

---

## How auto-evolve works

```
chat / tool call / observed outcome
   v
autoresearch hypothesis            (what could improve next response)
   v
33-rule guardrail filter           (immutable, hash-verified, rejects unsafe)
   v
hive vote                          (humans + agents, confidence-weighted)
   v
top-k dispatched to parallel sandboxes
   v
benchmark replay vs baseline       (per-agent baseline at agent_data/baselines/)
   v
RSI-2 monotonic gate               (must beat last commit on every metric)
   v
PR review                          (auto-rejects regression, hive contests merge)
   v
upgrade orchestrator               (BUILD -> TEST -> AUDIT -> BENCHMARK -> SIGN -> CANARY -> DEPLOY)
   v
FederatedAggregator broadcasts the delta to peer nodes
   v
hart-update-service (OTA) pulls signed upgrade on every node
```

Owner can pause, resume, or veto at any stage. [Mechanism details](https://docs.hevolve.ai/features/overview/).

---

## How hive connectivity works

```
Same-user devices
  trust = SAME_USER          -> no encryption (user_id auth), LAN or WAN
  
Cross-user peers
  trust = PEER               -> E2E encryption (per-link key)
  
Through relay (NAT-bound)
  trust = RELAY              -> E2E encryption (relay can't read)

Crossbar = safety measure (telemetry metadata + kill switch). Never content path.

Connection budget per tier:  flat=10  regional=50  central=200
ALL tiers participate equally in hive consensus.
Federation handshake = byte-for-byte guardrail hash match. Mismatch -> refused.
```

PeerLink is wired into bootstrap, gossip, federation, compute_mesh, world_model_bridge. [Wire format](docs/architecture/peer_link.md).

---

## Topology

| | Storage | Network | Use case |
|---|---|---|---|
| `flat` | SQLite WAL | localhost | Single device, laptop, Raspberry Pi, Nunba desktop |
| `regional` | MySQL QueuePool | LAN / VPN | Office cluster, family hive, edge node |
| `central` | MySQL + Docker | public mesh | Federated cloud workers |

Same code path. Env-detected at boot via `HART_OS_MODE` or `/etc/os-release ID=hart-os`. Port resolution: override > env var > OS / app mode default.

---

## Build / extend

| Audience | Where to start |
|---|---|
| **Developers** | [Build an agent + recipe](https://docs.hevolve.ai/agent-plugin/) - CREATE once, REUSE forever |
| | [Add a channel adapter](https://docs.hevolve.ai/features/overview/) - wire a new chat surface |
| | [Add a provider](https://docs.hevolve.ai/neuro-providers/) - new LLM / TTS / STT / VLM backend |
| | [Add a benchmark](integrations/agent_engine/benchmark_registry.py) - register an adapter, gets RSI-2 protection |
| | [PeerLink wire format](docs/architecture/peer_link.md) - direct P2P protocol |
| | [Federation protocol](docs/architecture/FEDERATION.md) - hash-gated peer handshake |
| | [HART SDK](docs/developer/sdk.md) - Python client + `hart` CLI |
| **Node operators** | [Run a node](https://docs.hevolve.ai/provider/joining/) - lend compute, host a region, earn from witnessed traffic |
| | [Compute settings](#api-surface) - cause alignment, electricity rate, idle policy |
| **End users** | [Nunba desktop](https://github.com/hertz-ai/Nunba) - chat / social / encounter app |
| **Security reviewers** | [Guardrail network](security/hive_guardrails.py) · [Master key](security/master_key.py) · [Audit log](security/immutable_audit_log.py) · [DLP](security/dlp_engine.py) |
| **Researchers** | [Auto-evolve](integrations/agent_engine/auto_evolve.py) · [RSI-2 trigger](integrations/agent_engine/rsi_trigger.py) · [Federated aggregator](integrations/agent_engine/federated_aggregator.py) · [Hive contests](integrations/agent_engine/hive_contest.py) |

Front it with `/v1/chat/completions` (any OpenAI client), the [`hart` CLI](docs/developer/sdk.md), or build for the desktop with [Nunba](https://github.com/hertz-ai/Nunba).

---

## Economics (for node operators)

```
Advertisers pay for witnessed impressions
   |
   v
Ad service           70% witnessed view, 50% unwitnessed
   |
   v
Compute democracy    log scaling, max 5% influence per entity, +20% diversity bonus
   |
   v
   90% -> Contributors  (compute, hosting, training)
    9% -> Infrastructure (regional hosts, bandwidth)
    1% -> hevolve.ai     (master key, central coordination, security)
```

Idle GPU in Tokyo serves Berlin. Reward score weighted by `gpu_hours`, `inferences`, `energy_kwh`, `api_costs`. Per-node `cause_alignment` and `electricity_rate_kwh` affect dispatch routing. [Joining the Hive](https://docs.hevolve.ai/provider/joining/).

---

## Documentation index

| Section | What's in it |
|---|---|
| [Downloads](https://docs.hevolve.ai/downloads/) | HART OS backend installer + Nunba desktop + headless pip |
| [Quickstart](https://docs.hevolve.ai/getting-started/quickstart/) | Install -> first agent -> first thought-experiment in 2 minutes |
| [Features](https://docs.hevolve.ai/features/overview/) | Auto-evolve, federation, channels, multimodal, encounters |
| [API](https://docs.hevolve.ai/api/core/) | `/chat`, OpenAI-compatible, 195+ endpoints |
| [Architecture](https://docs.hevolve.ai/architecture/overview/) | 3-tier topology, PeerLink, draft-first, agent engine, federation |
| [User journey](https://docs.hevolve.ai/developer/user-journey/) | What every screen does, end to end |
| [UI settings](https://docs.hevolve.ai/ui/settings-spec/) | Admin console + every setting |
| [Provider join](https://docs.hevolve.ai/provider/joining/) | Lend compute, host a region, earn |
| [Hive contests](https://docs.hevolve.ai/hive-contest/) | Open contests on the network |
| [Neuro providers](https://docs.hevolve.ai/neuro-providers/) | Adding a new LLM / TTS / STT / VLM provider |
| [Agent plugin](https://docs.hevolve.ai/agent-plugin/) | Building custom agents + recipes |

---

## License

[Apache License 2.0](LICENSE).
