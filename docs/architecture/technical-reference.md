# HART OS Technical Reference

> Single source of truth for every design element in the HART OS codebase.
> All subsystems, patterns, protocols, configuration, and security architecture in one document.

---

## 1. System Identity

**HART OS** = **H**evolve **H**ive **A**gentic **R**un**t**ime

An open, crowdsourced compute infrastructure that orchestrates fully autonomous Hive AI Training. No single entity, government, or corporation monopolizes AI. Intelligence belongs to the common person.

**Core innovation**: Recipe Pattern — learn task execution once (CREATE mode), replay efficiently (REUSE mode) without repeated LLM calls.

**Foundational principle**: Humans are always in control. Every engineering decision — from gossip protocol to guardrail hash verification to peer-witnessed ad impressions — makes centralized control structurally impossible, not just policy-prohibited.

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

---

## 3. Entry Points

| Entry Point | File | Port | Purpose |
|-------------|------|------|---------|
| Backend API | `langchain_gpt_api.py` | 6777 (app) / 677 (OS) | Flask + Waitress, all REST endpoints |
| LiquidUI Shell | `integrations/agent_engine/liquid_ui_service.py` | 6778 | Desktop shell, WebKit renderer |
| CLI | `hart_cli.py` | N/A | 21 Click subcommands |
| Agent Daemon | `integrations/agent_engine/agent_daemon.py` | N/A | Tick-based goal processor |
| Discovery | `integrations/social/peer_discovery.py` | 6780 (app) / 678 (OS) | UDP gossip beacon |

---

## 4. Recipe Pipeline

The core execution model for all agent work.

```
CREATE Mode: User Input -> Decompose -> Execute Actions -> Save Recipe
REUSE Mode:  User Input -> Load Recipe -> Execute Steps -> Output (90% faster)
```

### Files

| File | Purpose |
|------|---------|
| `create_recipe.py` | Decompose prompt into flows/actions, execute via LLM, save recipe |
| `reuse_recipe.py` | Load saved recipe, replay steps without LLM |
| `helper.py` | `Action` class, JSON utilities, tool handler dispatch |
| `lifecycle_hooks.py` | `ActionState` machine, `FlowState`, ledger sync |
| `helper_ledger.py` | SmartLedger factory: `create_ledger_for_user_prompt()` |

### ActionState Machine

```
ASSIGNED -> IN_PROGRESS -> STATUS_VERIFICATION_REQUESTED -> COMPLETED -> TERMINATED
                                                          -> ERROR -> TERMINATED
```

States auto-sync to SmartLedger. StatusVerifier LLM generates autonomous fallback strategies.

### Recipe Storage

```
prompts/{prompt_id}.json                           # Prompt definition
prompts/{prompt_id}_{flow_id}_recipe.json          # Trained recipe
prompts/{prompt_id}_{flow_id}_{action_id}.json     # Action recipes
agent_data/ledger_{user_id}_{prompt_id}.json       # Execution state
```

### Hierarchical Task Decomposition

```
User Prompt
 +-- Flow 1 (Persona A)
 |    +-- Action 1
 |    +-- Action 2
 |    +-- Action 3
 +-- Flow 2 (Persona B)
      +-- Action 1
      +-- Action 2
```

---

## 5. Agent Engine

Goal-driven autonomous agent runtime.

### Files (`integrations/agent_engine/`)

| File | Purpose |
|------|---------|
| `goal_manager.py` | 17 goal types, prompt builders, tool tag routing |
| `dispatch.py` | Goal decomposition, LLM dispatch, budget gating |
| `speculative_dispatcher.py` | Fast response + background expert refinement |
| `agent_daemon.py` | Tick-based daemon, processes goals from queue |
| `goal_seeding.py` | Bootstrap goals on first start |
| `revenue_aggregator.py` | Revenue streams, 90/9/1 split, settlement |
| `budget_gate.py` | Spark cost estimation, metered usage recording |
| `compute_config.py` | 3-layer config: env > DB > defaults, 30s TTL cache |
| `model_registry.py` | Model catalog, energy tracking, policy routing |
| `federated_aggregator.py` | FedAvg delta aggregation, recipe sharing channel |
| `compute_mesh_service.py` | Cross-device compute offload, peer selection |
| `world_model_bridge.py` | Hivemind query aggregation, HevolveAI dispatch |
| `model_bus_service.py` | Universal AI API for all apps (NixOS model-bus) |
| `commercial_api.py` | Commercial API endpoint management |
| `network_provisioner.py` | Node provisioning, deployment automation |

### Goal Types (17)

marketing, coding, trading, civic_sentinel, upgrade_monitor, content_gen, data_analysis, research, creative, education, health, finance, social_media, community, automation, custom, bootstrap

### Dispatch Flow

```
GoalManager.create_goal()
  -> dispatch.py decompose_and_dispatch()
    -> budget_gate.check_affordability()
    -> speculative_dispatcher (FAST model instant, EXPERT model background)
    -> lifecycle_hooks.ActionState tracking
    -> SmartLedger persistence
```

### Revenue Model (90/9/1)

| Recipient | Share | Source |
|-----------|-------|--------|
| Users (contributors) | 90% | Proportional to: GPU hours, inferences, energy, content, API costs |
| Infrastructure | 9% | Node hosting, bandwidth, maintenance |
| Central | 1% | Coordination, development |

Constants in `revenue_aggregator.py`: `REVENUE_SPLIT_USERS=0.90`, `REVENUE_SPLIT_INFRA=0.09`, `REVENUE_SPLIT_CENTRAL=0.01`

### Compute Policies

| Policy | Behavior |
|--------|----------|
| `local_only` | Never use cloud/peer models. Free Spark cost. |
| `local_preferred` | Try local first, fall back to cloud if needed. |
| `any` | Use best available model regardless of location. |

Resolution: `compute_config.get_compute_policy()` — env var > DB > default (`local_preferred`)

---

## 6. Security Architecture

### Trust Hierarchy

```
Master Key (Ed25519, human-held, AI exclusion zone)
  |
  +-- Central Certificate (signs regional)
       |
       +-- Regional Certificate (signs local/flat)
            |
            +-- Node Certificate (runtime identity)
```

### Files (`security/`)

| File | Purpose |
|------|---------|
| `master_key.py` | Ed25519 trust anchor. Public key hardcoded. Private key in HSM/GitHub Secrets. |
| `hive_guardrails.py` | 10 structurally immutable guardrail classes. `_FrozenValues` + `__setattr__` guard + SHA-256 hash chain. Re-verified every 300s. |
| `key_delegation.py` | 3-tier certificate chain. `DomainChallengeVerifier` for provisional nodes. |
| `runtime_monitor.py` | Background daemon, detects code/guardrail tampering. |
| `node_watchdog.py` | Heartbeat protocol, frozen-thread detection, auto-restart with backoff. |
| `node_integrity.py` | Ed25519 keypair management, code hash, JSON signature. |
| `channel_encryption.py` | X25519 ECDH + AES-256-GCM for inter-node E2E encryption. Forward secrecy. |
| `crypto.py` | Fernet (AES-128-CBC + HMAC) for data at rest. `encrypt_json_file`/`decrypt_json_file`. |
| `immutable_audit_log.py` | SHA-256 hash-chain audit trail. Tamper detection via `verify_chain()`. |
| `action_classifier.py` | Destructive pattern detection. `PREVIEW_PENDING`/`APPROVED` states. |
| `dlp_engine.py` | PII scan/redact: email, phone, SSN, credit card. Outbound gating. |
| `secret_redactor.py` | 3-layer: regex secrets + LLM PII detection + differential privacy. |
| `rate_limiter_redis.py` | Sliding window rate limiter. Redis primary, in-memory fallback. |
| `origin_attestation.py` | Origin fingerprint verification for federation. |
| `hsm_provider.py` | HSM backends: GCP KMS, Azure Key Vault, HashiCorp Vault, AWS CloudHSM. |

### Master Key Rules (AI Exclusion Zone)

1. NEVER read/display/log the master private key
2. NEVER call `get_master_private_key()` or `sign_child_certificate()`
3. NEVER modify `MASTER_PUBLIC_KEY_HEX` — the trust anchor is immutable
4. NEVER modify `HiveCircuitBreaker` or `_FrozenValues`
5. The master key is a kill switch for distributed intelligence. It belongs to human stewards only.

### Encryption Model

| Layer | What | Algorithm | Key Management |
|-------|------|-----------|----------------|
| Transport (PeerLink) | WebSocket frames between peers | AES-256-GCM (X25519 ECDH session key) | Per-session, 3600s rotation |
| Inter-node (E2E) | Task payloads, gossip | X25519 + AES-256-GCM | Ephemeral ECDH, forward secrecy |
| At rest | JSON files, private keys | Fernet (AES-128-CBC + HMAC) | `HEVOLVE_DATA_KEY` env var |
| Audit log | Event entries | SHA-256 hash chain | Chained, tamper-detectable |

### Data at Rest Encryption

Wired into 4 subsystems via `security/crypto.py`:

| Data | File Pattern | Encrypted |
|------|-------------|-----------|
| Resonance profiles | `agent_data/resonance/{user_id}_resonance.json` | Yes (biometric embeddings, preferences) |
| Instruction queues | `agent_data/instructions/{user_id}_queue.json` | Yes (user instructions, context) |
| Ed25519 private key | `agent_data/node_private_key.pem` | Yes |
| X25519 private key | `agent_data/node_x25519_private.key` | Yes |
| Public keys | `agent_data/node_*_public.*` | No (public data) |

Design: encrypt on write, decrypt on read. Auto-detect Fernet prefix (`gAAAAA`) for seamless plaintext migration. No hive impact — in-memory data always plaintext.

### Guardrail Network (10 Classes)

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

Structurally immutable: `_FrozenValues` + module `__setattr__` override + SHA-256 hash chain verified every 300 seconds network-wide.

### Rate Limits

31 action categories in `security/rate_limiter_redis.py`:

| Action | Limit | Window |
|--------|-------|--------|
| `global` | 60 | 60s |
| `auth` | 10 | 60s |
| `chat` | 30 | 60s |
| `goal_create` | 10 | 3600s |
| `shell_power` | 3 | 60s |
| `app_install` | 5 | 3600s |
| `tts_clone` | 5 | 3600s |
| `remote_desktop_auth` | 5 | 60s |
| *(27 more, see file)* | | |

---

## 7. PeerLink (P2P Communication)

Persistent WebSocket connections between nodes with trust-aware encryption.

### Files (`core/peer_link/`)

| File | Purpose |
|------|---------|
| `link.py` | `PeerLink` class — WebSocket, AES-256-GCM session encryption |
| `link_manager.py` | Connection budget, auto-upgrade (3 HTTP exchanges), idle pruning (5min) |
| `channels.py` | 9 channels, `DataClass` (OPEN/PRIVATE/SYSTEM), `ChannelDispatcher` |
| `nat.py` | 5 NAT strategies: LAN direct -> STUN -> WireGuard -> Peer relay -> Crossbar relay |
| `telemetry.py` | Crossbar telemetry (metadata only), kill switch delivery, peer ban |
| `message_bus.py` | Unified pub/sub: LOCAL + PEERLINK + CROSSBAR. LRU dedup (10000). |

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
| `ralt` | OPEN | Real-time telemetry |
| `sensor` | PRIVATE | IoT/embodied sensor data |

PRIVATE channels always encrypted on cross-user links. OPEN channels carry non-sensitive data.

### Connection Budget

| Tier | Max Simultaneous Links |
|------|----------------------|
| Flat | 10 |
| Regional | 50 |
| Central | 200 |

Budget limits connections, NOT capabilities. All tiers participate fully in hive.

### Integration Points

| Subsystem | How It Uses PeerLink |
|-----------|---------------------|
| `peer_discovery.py` | Gossip exchange via PeerLink, HTTP fallback |
| `federation.py` | Content delivery via `federation` channel, HTTP fallback |
| `compute_mesh_service.py` | Inference offload via `compute` channel with `wait_response` |
| `world_model_bridge.py` | Hivemind queries via `collect('hivemind')` |
| `bootstrap.py` | Registers `peer_link`, `message_bus`, `central_connection` as platform services |

---

## 8. Platform Substrate

Durable OS-level services that unify all subsystems.

### Files (`core/platform/`)

| File | Key Class | Purpose |
|------|-----------|---------|
| `registry.py` | `ServiceRegistry` | Typed lazy singleton container, `Lifecycle` protocol, dependency ordering |
| `config.py` | `PlatformConfig` | 3-layer config (env > override > DB), TTL cache, `on_change()` callbacks |
| `events.py` | `EventBus` | Topic pub/sub, wildcards (`theme.*`), sync/async emit, WAMP bridge |
| `app_manifest.py` | `AppManifest` | Universal manifest for 9 app types, spotlight search |
| `app_registry.py` | `AppRegistry` | Central app catalog, search, groups, event emission |
| `extensions.py` | `ExtensionRegistry` | Plugin ABC, state machine (UNLOADED->LOADED->ENABLED), hot reload |
| `bootstrap.py` | `bootstrap_platform()` | Registers services, migrates 55 panels, detects native apps, loads extensions |

### App Types (9)

`nunba_panel`, `system_panel`, `dynamic_panel`, `desktop_app`, `service`, `agent`, `mcp_server`, `channel`, `extension`

### EventBus Topics

| Topic | Trigger |
|-------|---------|
| `theme.changed` | Theme switch |
| `theme.custom_updated` | Custom theme CSS override |
| `resonance.tuned` | User resonance profile updated |
| `action_state.changed` | ActionState transition |
| `inference.completed` | Model inference finished |
| `memory.item_added` | Memory store addition |
| `memory.item_deleted` | Memory store deletion |
| `federation.aggregated` | Federation delta applied |

WAMP bridge: local topics auto-publish to Crossbar as `com.hartos.event.{topic}`.

---

## 9. OS Management Layer

### Shell OS APIs (`integrations/agent_engine/shell_os_apis.py`)

40+ routes via `register_shell_os_routes(app)`:

| Category | Routes | Features |
|----------|--------|----------|
| Notifications | 4 | D-Bus + in-memory SSE |
| File Manager | 6 | Browse, mkdir, delete, move, copy, info (path-sandboxed) |
| Terminal | 3 | PTY create, exec, resize |
| User Accounts | 3 | Create, list, delete |
| Setup Wizard | 2 | 5-step first-boot |
| Backup/Restore | 2 | Local path backup |
| Power | 4 | Shutdown, reboot, suspend, hibernate |
| i18n | 2 | 11 locales |
| Screenshot | 1 | grim/scrot/mss |
| Screen Recording | 2 | wf-recorder/ffmpeg |
| Multi-device Pairing | 1 | Mesh bridge |
| OTA Upgrade | 1 | Orchestrator bridge |

### Shell Desktop APIs (`integrations/agent_engine/shell_desktop_apis.py`)

9 features via `register_shell_desktop_routes(app)`:

Default apps (xdg-mime), font manager (fc-list), sound manager (paplay/pw-play), clipboard history (wl-paste/wl-copy), datetime/timezone (timedatectl), wallpaper (swaymsg/feh), input methods (setxkbmap), night light (gammastep/redshift), workspaces (swaymsg/wmctrl). Wayland/X11 auto-detect.

### Shell System APIs (`integrations/agent_engine/shell_system_apis.py`)

6 features via `register_shell_system_routes(app)`:

Task/process manager (psutil), storage manager (du, smartctl), startup apps (XDG .desktop), Bluetooth (bluetoothctl, background scan), print manager (CUPS), media indexer (exiftool/ffprobe).

### App Installer (`integrations/agent_engine/app_installer.py`)

7 routes via `register_app_install_routes(app)`. Cross-platform: Nix, Flatpak, AppImage, Windows (Wine), Android (binder/adb), macOS (Darling), HART extensions. Platform detection: extension mapping + magic bytes (MZ=PE, PK+AndroidManifest=APK, ELF=AppImage). SHA256 checksum verification.

---

## 10. Social Platform

82+ REST endpoints for communities, posts, feeds, karma, encounters, and federation.

### Files (`integrations/social/`)

| File | Purpose |
|------|---------|
| `models.py` | SQLAlchemy ORM (60+ tables), `db_session()` context manager |
| `api.py` | Core social endpoints (communities, posts, comments, votes) |
| `api_games.py` | Game catalog, participation, leaderboards |
| `api_gamification.py` | Badges, achievements, streaks, XP |
| `api_sharing.py` | OG images, embed cards, shareable links |
| `api_mcp.py` | MCP tool endpoints |
| `services.py` | `NotificationService.create()`, business logic |
| `peer_discovery.py` | Gossip protocol, bandwidth profiles, peer exchange |
| `federation.py` | Instance follows, content push/pull, inbox/outbox |
| `hosting_reward_service.py` | Contribution scoring, hosting rewards |
| `integrity_service.py` | Peer integrity verification, fraud detection |
| `resonance_engine.py` | Content relevance scoring |
| `sync_engine.py` | Cross-instance data sync |
| `consent_service.py` | Data consent management |
| `discovery.py` | Content and peer discovery |
| `openclaw_tools.py` | OpenClaw integration tools |

### Database

SQLite at `agent_data/hevolve_database.db` with WAL mode. 60+ tables including:

`User`, `Post`, `Comment`, `Vote`, `Community`, `CommunityMember`, `Karma`, `PeerNode`, `InstanceFollow`, `FederatedPost`, `ComputeEscrow`, `MeteredAPIUsage`, `NodeComputeConfig`, `AuditLogEntry`, and more.

### Federation Model

```
Central (hevolve.ai)
  +-- Regional Host (gossip hub, certificate authority)
  |     +-- Local Node (agent host)
  |     +-- Local Node
  +-- Regional Host
  |     +-- Local Node
  +-- Flat Nodes (standalone, 10-link budget)
```

Gossip: UDP broadcast + mDNS on LAN, HTTP exchange on WAN. Ed25519-signed beacons.

---

## 11. Channel Adapters

30+ platform adapters with a unified `send()`/`receive()` interface.

### File (`integrations/channels/`)

| Adapter | Platform |
|---------|----------|
| Discord | discord.py |
| Telegram | telegram.py |
| Slack | slack.py |
| Matrix | matrix.py |
| WhatsApp | whatsapp.py |
| Signal | signal.py |
| Email | email.py |
| SMS | sms.py |
| Google Chat | google_chat.py |
| Microsoft Teams | teams.py |
| IRC | irc.py |
| *(20+ more)* | |

### Media Pipeline (`integrations/channels/media/`)

| File | Purpose |
|------|---------|
| `tts.py` | `TTSEngine`, `TTSProvider` enum, voice synthesis dispatch |
| `tts_router.py` | Smart TTS routing: language detection, GPU constraints, hive offload |
| `stt.py` | Speech-to-text dispatch |

### Channel Registry (`integrations/channels/registry.py`)

Unified adapter registration, health checks, message routing.

---

## 12. TTS & Voice

Multi-engine text-to-speech with smart routing.

### TTS Router Decision Flow

```
1. detect_language(text)
2. LANG_ENGINE_PREFERENCE[lang] -> candidate engines
3. Filter: GPU available? VRAM fits? Engine installed?
4. Filter: compute_policy (local_only/preferred/any)
5. Hive peer offload if GPU needed but unavailable locally
6. Rank by urgency (instant/normal/quality)
7. Execute top candidate, fallback chain on failure
8. espeak-ng ultimate fallback (100+ languages, CPU)
```

### Engines

| Engine | Device | Languages | VRAM | Clone |
|--------|--------|-----------|------|-------|
| LuxTTS | CPU/GPU | en | 0/2GB | Yes |
| Pocket TTS | CPU | en | 0 | Yes |
| Chatterbox Turbo | GPU | en | 3.8GB | Yes |
| Chatterbox ML | GPU | 23 | 12GB | Yes |
| CosyVoice 3 | GPU | 9 | 3.5GB | Yes |
| F5-TTS | GPU | en,zh | 1.3GB | Yes |
| Indic Parler | GPU | 22 | 1.8GB | No |
| espeak-ng | CPU | 100+ | 0 | No |

### Files (`integrations/service_tools/`)

| File | Purpose |
|------|---------|
| `luxtts_tool.py` | Sherpa-ONNX ZipVoice INT8, voice cloning |
| `pocket_tts_tool.py` | In-process TTS, 8 built-in voices |
| `chatterbox_tool.py` | GPU TTS stub (lazy import) |
| `cosyvoice_tool.py` | CosyVoice 3 stub |
| `f5_tts_tool.py` | F5-TTS stub |
| `indic_parler_tool.py` | Indic Parler stub |
| `whisper_tool.py` | Whisper STT |
| `vram_manager.py` | GPU detection, VRAM budgets, allocation |
| `model_lifecycle.py` | Dynamic model load/unload/offload |

---

## 13. Vision & Perception

### Files

| File | Purpose |
|------|---------|
| `integrations/vision/vision_service.py` | Vision dispatch, MiniCPM sidecar |
| `integrations/vision/minicpm_installer.py` | MiniCPM auto-install |
| `integrations/vision/ltx2_server.py` | LTX-2 video generation |
| `integrations/audio/diarization_server.py` | Speaker diarization |

---

## 14. Remote Desktop

Native wrapping of RustDesk + Sunshine/Moonlight as OS-level apps.

### Files (`integrations/remote_desktop/`)

| File | Purpose |
|------|---------|
| `orchestrator.py` | Coordinates all remote desktop engines |
| `service_manager.py` | Engine lifecycle (detect/install/start/stop/health) |
| `engine_selector.py` | Auto-picks engine by use case |
| `rustdesk_bridge.py` | RustDesk CLI wrapper |
| `sunshine_bridge.py` | Sunshine REST API wrapper |
| `transport.py` | Native WebSocket fallback (3-tier) |
| `signaling.py` | WAMP connection negotiation |
| `file_transfer.py` | Chunked 64KB binary, SHA256 verify, DLP scan |
| `session_manager.py` | OTP auth (6-char, 5min), multi-viewer |
| `clipboard_sync.py` | Cross-engine clipboard bridge |
| `drag_drop.py` | Cross-device DLP-scanned drag-drop |
| `window_capture.py` | Per-window streaming |
| `peripheral_bridge.py` | USB/IP, Bluetooth HID, Gamepad evdev |
| `dlna_bridge.py` | SSDP discovery, UPnP AVTransport |

### Engine Selection

| Engine | Use Case | License |
|--------|----------|---------|
| RustDesk | General remote desktop | AGPL-3.0 |
| Sunshine+Moonlight | High-fidelity streaming | GPL-3.0 |
| Native transport | Fallback when no engine installed | N/A |

---

## 15. Coding Agent

Idle compute coding agent with Aider integration.

### Files (`integrations/coding_agent/`)

| File | Purpose |
|------|---------|
| `orchestrator.py` | Backend selection, task routing |
| `tool_backends.py` | Pluggable backends (Aider, KiloCode, Claude Code) |
| `task_distributor.py` | Task distribution across nodes |
| `coding_daemon.py` | Idle compute detection, task dispatch |
| `remote_executor.py` | Nunba `/execute` + `/screenshot` bridge |

### Aider Integration (`integrations/coding_agent/aider_core/`)

Vendored Apache 2.0, stripped of pydantic v2/litellm deps. Key modules: `repomap.py` (tree-sitter PageRank), `search_replace.py`, `linter.py`. Custom: `io_adapter.py` (SimpleIO), `hart_model_adapter.py` (HARTOS LLM bridge).

---

## 16. Resonance & Personality

Per-user continuous tuning that makes agents adapt to individual communication styles.

### Files

| File | Purpose |
|------|---------|
| `core/resonance_profile.py` | `UserResonanceProfile` — 8 continuous dimensions (0-1) |
| `core/resonance_tuner.py` | `SignalExtractor`, `ResonanceTuner` (EMA alpha=0.15), `DialogueStreamProcessor` |
| `core/resonance_identifier.py` | Thin proxy — dispatches biometric ops to HevolveAI |
| `core/agent_personality.py` | `AgentPersonality` dataclass, `generate_personality()` |

### Tuning Dimensions

| Dimension | Range | Meaning |
|-----------|-------|---------|
| `formality_score` | 0-1 | casual to formal |
| `verbosity_score` | 0-1 | terse to detailed |
| `warmth_score` | 0-1 | professional to warm |
| `pace_score` | 0-1 | slow/thorough to fast |
| `technical_depth` | 0-1 | simple to technical |
| `encouragement_level` | 0-1 | matter-of-fact to encouraging |
| `humor_receptivity` | 0-1 | serious to playful |
| `autonomy_preference` | 0-1 | ask-before-acting to autonomous |

Storage: `agent_data/resonance/{user_id}_resonance.json` (encrypted at rest when `HEVOLVE_DATA_KEY` set).

---

## 17. CLI (`hart_cli.py`)

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

## 18. NixOS Modules (48)

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

### Desktop

| Module | Purpose |
|--------|---------|
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

### Peripherals

| Module | Purpose |
|--------|---------|
| `hart-peripheral-bridge.nix` | USB/IP + Bluetooth HID + uinput |
| `hart-dlna.nix` | SSDP discovery + MJPEG + MiniDLNA |

### Configurations

| Config | Target |
|--------|--------|
| `desktop.nix` | GNOME desktop with LiquidUI |
| `server.nix` | Headless server |
| `edge.nix` | Edge/IoT device |
| `phone.nix` | PinePhone mobile |

### Hardware Profiles

`raspberry-pi.nix`, `pinephone.nix`, `riscv-generic.nix`

---

## 19. Port Registry

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
Resolution: explicit override > env var > OS/app mode default.

---

## 20. Design Patterns

### Singleton Pattern

Module-level `_instance = None` + `get_*()` factory function. Used in 20+ modules:

```python
_instance = None

def get_revenue_aggregator():
    global _instance
    if _instance is None:
        _instance = RevenueAggregator()
    return _instance
```

### DB Session Pattern

Always use `db_session()` context manager from `integrations/social/models.py`:

```python
from integrations.social.models import db_session
with db_session() as db:
    user = db.query(User).filter_by(id=user_id).first()
```

Never use manual `get_db()`/`try`/`finally`/`close()`.

### Notification Pattern

Always use `NotificationService.create()` from `integrations/social/services.py`. Never construct `Notification()` directly.

### HTTP Pool Pattern

Always use `pooled_get()`/`pooled_post()` from `core/http_pool.py`. Never use bare `requests.get()`/`requests.post()`.

### GPU Detection Pattern

Single source: `vram_manager.detect_gpu()` from `integrations/service_tools/vram_manager.py`. Never inline `torch.cuda.*` calls.

### Flask Error Handling

Use `@_json_endpoint` decorator in `langchain_gpt_api.py` for automatic try/except/jsonify.

### Lazy Import Pattern

Service tools use lazy imports to avoid loading heavy ML libraries at startup:

```python
def synthesize(text, ...):
    global _model
    if _model is None:
        from some_heavy_package import Model
        _model = Model.from_pretrained(...)
    return _model.generate(text)
```

### Atomic File Write Pattern

Temp file + `os.replace()` for crash-safe persistence:

```python
tmp_path = path + '.tmp'
with open(tmp_path, 'w') as f:
    json.dump(data, f)
    f.flush(); os.fsync(f.fileno())
os.replace(tmp_path, path)
```

---

## 21. Environment Variables

### Core

| Variable | Default | Purpose |
|----------|---------|---------|
| `HEVOLVE_NODE_TIER` | `flat` | Node tier: flat/regional/central |
| `HEVOLVE_NODE_ID` | `local` | Unique node identifier |
| `HEVOLVE_USER_ID` | | Authenticated user identity |
| `HEVOLVE_DEV_MODE` | `false` | Enable dev features (forced off on central) |
| `HEVOLVE_ENFORCEMENT_MODE` | `hard` | Security enforcement level |
| `HEVOLVE_DB_PATH` | `agent_data/hevolve_database.db` | Database path |
| `HEVOLVE_KEY_DIR` | `agent_data` | Cryptographic key storage |
| `HEVOLVE_DATA_KEY` | | Fernet key for at-rest encryption (opt-in) |

### LLM

| Variable | Default | Purpose |
|----------|---------|---------|
| `HEVOLVE_LLM_ENDPOINT_URL` | | Custom LLM endpoint |
| `HEVOLVE_LLM_MODEL_NAME` | `gpt-4.1-mini` | Primary model |
| `HEVOLVE_LLM_API_KEY` | | API key |
| `HEVOLVE_LOCAL_LLM_URL` | `http://localhost:8000/v1` | Local LLM endpoint |
| `HEVOLVE_LOCAL_LLM_MODEL` | `local` | Local model name |
| `HEVOLVE_ACTIVE_CLOUD_PROVIDER` | | Cloud provider name |

### Networking

| Variable | Default | Purpose |
|----------|---------|---------|
| `CBURL` | | Crossbar WAMP URL |
| `CBREALM` | | Crossbar realm |
| `REDIS_URL` | `redis://localhost:6379/1` | Redis URL |
| `HART_OS_MODE` | `false` | OS mode (privileged ports) |

### Port Overrides

`HARTOS_BACKEND_PORT`, `HART_DISCOVERY_PORT`, `HART_VISION_PORT`, `HART_LLM_PORT`, `HART_WS_PORT`, `HART_MESH_WG_PORT`, `HART_MESH_RELAY_PORT`

---

## 22. API Endpoints (60+)

### Core (`langchain_gpt_api.py`)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/chat` | Main agent endpoint |
| POST | `/time_agent` | Scheduled task execution |
| POST | `/visual_agent` | VLM/Computer vision |
| POST | `/add_history` | Conversation history |
| GET | `/status` | Health check |
| GET | `/prompts` | List prompts |
| POST | `/zeroshot/` | Zero-shot execution |

### Voice

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/voice/speak` | Generate speech (routed via TTSRouter) |
| POST | `/api/voice/transcribe` | Transcribe audio |
| GET | `/api/voice/voices` | List available voices |
| POST | `/api/voice/clone` | Clone voice |
| GET | `/api/voice/engines` | List TTS engines |
| GET | `/api/voice/audio/<file>` | Serve audio file |

### Settings & Compute

| Method | Path | Purpose |
|--------|------|---------|
| GET/PUT | `/api/settings/compute` | Compute config |
| GET | `/api/settings/compute/provider` | Provider status |
| POST | `/api/settings/compute/provider/join` | Join as provider |

### Instructions

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/instructions/enqueue` | Queue instruction |
| GET | `/api/instructions/pending` | Pending list |
| POST | `/api/instructions/drain` | Drain queue |

### Remote Desktop

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/remote-desktop/status` | Status |
| POST | `/api/remote-desktop/host` | Start hosting |
| POST | `/api/remote-desktop/connect` | Connect to remote |
| GET | `/api/remote-desktop/engines` | Available engines |

### Shell Management (via `register_shell_*_routes()`)

40+ routes across: `/api/shell/files/*`, `/api/shell/notifications/*`, `/api/shell/terminal/*`, `/api/shell/users/*`, `/api/shell/backup/*`, `/api/shell/power/*`, `/api/shell/i18n/*`, `/api/shell/screenshot`, `/api/shell/screen-record/*`

### Social (via `social_bp` blueprint)

82+ endpoints: communities, posts, comments, votes, karma, encounters, games, gamification, sharing, federation, MCP.

### A2A Protocol

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/a2a/{id}/.well-known/agent.json` | Agent card |
| POST | `/a2a/{id}/execute` | Execute task |

---

## 23. Test Architecture

### Test Pattern

- `--noconftest` flag for all runs (avoids tempfile corruption from TestMediaAgent fixture)
- `-p no:capture` for federation tests
- Python 3.11 active, `autogen` not installed (9 test files skip)
- Pre-existing: ~70 failures across 27 files (not caused by recent changes)

### Test Suites

| Suite | File Pattern | Count |
|-------|-------------|-------|
| PeerLink | `test_peer_link.py` | 135 |
| Encryption at Rest | `test_encryption_at_rest.py` | 34 |
| Channel Encryption | `test_channel_encryption.py` | 24 |
| Security (WS11-WS13) | `test_ws11_*.py`, `test_ws12_*.py`, `test_ws13_*.py` | 235 |
| Platform | `test_platform_*.py` | 223 |
| TTS Router | `test_tts_router.py` | ~40 |
| Social | `test_social_*.py` | ~100 |
| Instruction Queue | `test_instruction_queue.py` | ~60 |
| Master Key | `test_master_key_system.py` | ~40 |
| Node Watchdog | `test_node_watchdog.py` | ~30 |

### Run Commands

```bash
pytest tests/unit/ -v --noconftest --tb=short       # All unit tests
pytest tests/unit/test_peer_link.py -v --noconftest  # PeerLink only
pytest tests/unit/test_encryption_at_rest.py -v --noconftest  # Encryption at rest
```

---

## 24. Dependencies

### Critical Pinned Versions

| Package | Version | Reason |
|---------|---------|--------|
| `langchain` | 0.0.230 | Monolithic pre-split package |
| `pydantic` | 1.10.9 | Requires Python 3.10-3.11 |
| `cryptography` | >= 41.0 | Ed25519, X25519, AES-GCM, Fernet |

### Optional Groups

| Group | Packages | Purpose |
|-------|----------|---------|
| `remote-desktop` | mss, websockets, av, pynput | Remote desktop features |
| `tts` | pocket-tts, sherpa-onnx | Offline TTS engines |
| `vision` | transformers, torch | Vision models |

All imports use `from langchain.X` (NOT `langchain_classic`, `langchain_community`).

---

## 25. File Tree Summary

```
HARTOS/
+-- langchain_gpt_api.py        # Flask entry point (port 6777)
+-- create_recipe.py             # CREATE mode pipeline
+-- reuse_recipe.py              # REUSE mode pipeline
+-- helper.py                    # Action class, JSON utils
+-- lifecycle_hooks.py           # ActionState machine
+-- helper_ledger.py             # SmartLedger factory
+-- hart_cli.py                  # CLI (21 subcommands)
+-- agent_identity.py            # Project identity constants
+--
+-- core/
|   +-- platform/                # OS substrate (7 files)
|   +-- peer_link/               # P2P communication (7 files)
|   +-- agent_tools.py           # Canonical tool definitions
|   +-- port_registry.py         # Port assignments
|   +-- http_pool.py             # Connection pooling
|   +-- resonance_profile.py     # Per-user tuning
|   +-- resonance_tuner.py       # EMA tuner + signal extraction
|   +-- agent_personality.py     # Agent personality traits
|   +-- cache_loaders.py         # Agent data cache
+--
+-- security/                    # 15 files (see Section 6)
+--
+-- integrations/
|   +-- agent_engine/            # 20+ files (see Section 5)
|   +-- social/                  # 15+ files (see Section 10)
|   +-- channels/                # 30+ adapters (see Section 11)
|   +-- coding_agent/            # 6 files (see Section 15)
|   +-- remote_desktop/          # 14 files (see Section 14)
|   +-- service_tools/           # 10+ files (see Section 12)
|   +-- vision/                  # 3 files (see Section 13)
|   +-- audio/                   # Diarization server
|   +-- mcp/                     # MCP server
|   +-- ap2/                     # Agent Protocol 2
|   +-- expert_agents/           # 96 specialized agents
|   +-- google_a2a/              # Dynamic agent registry
|   +-- openclaw/                # OpenClaw integration
+--
+-- nixos/                       # 48 NixOS modules (see Section 18)
+-- tests/                       # Unit + integration tests
+-- docs/                        # 117 markdown files
```
