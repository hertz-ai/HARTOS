# Promise vs Code Scorecard

> Transparency notice: this page lists every major claim HART OS makes and
> whether the codebase actually delivers it. OSS contributors and evaluators
> should be able to verify each item themselves.

**Result: 18 / 20 DELIVERED, 2 PARTIAL**

## Scoring Key

| Status | Meaning |
|--------|---------|
| DELIVERED | Feature exists, tests pass, code path exercised |
| PARTIAL | Core logic exists but has a known gap (documented below) |

---

## The 20 Claims

### 1. Recipe Pattern (CREATE / REUSE)

**Status: DELIVERED**

- `create_recipe.py` decomposes prompts into flows and actions, saves JSON recipes.
- `reuse_recipe.py` replays saved recipes without LLM calls.
- Storage: `prompts/{prompt_id}_{flow_id}_recipe.json`.
- Tests: `test_pipeline_lifecycle_functional.py` (22 tests) validates save/load round-trip and state machine.

### 2. ActionState Machine

**Status: DELIVERED**

- `lifecycle_hooks.py` implements ASSIGNED -> IN_PROGRESS -> STATUS_VERIFICATION_REQUESTED -> COMPLETED/ERROR -> TERMINATED.
- Invalid transitions are rejected; thread-safe under concurrent access.
- Tests: `test_pipeline_lifecycle_functional.py` covers valid paths, invalid transitions, and concurrent state changes.

### 3. 90/9/1 Revenue Split

**Status: DELIVERED**

- Canonical constants in `integrations/agent_engine/revenue_aggregator.py`: `REVENUE_SPLIT_USERS=0.90`, `REVENUE_SPLIT_INFRA=0.09`, `REVENUE_SPLIT_CENTRAL=0.01`.
- Imported by `ad_service.py`, `hosting_reward_service.py`, `finance_tools.py`.
- Tests: `test_revenue_functional.py` (6 tests) validates the split math against real SQLite.

### 4. Spark Settlement & Metered API Cost Recovery

**Status: DELIVERED**

- `revenue_aggregator.py` `settle_metered_api_costs()` converts USD to Spark at `SPARK_PER_USD=100`.
- `budget_gate.py` records metered usage; `MeteredAPIUsage` table in `models.py`.
- Tests: `test_revenue_functional.py` covers settlement, self-task skip, write-off, and env overrides.

### 5. Federation Protocol (Equal Weighting)

**Status: DELIVERED**

- `integrations/agent_engine/federated_aggregator.py` uses `log1p(interactions)` with floor=1.0.
- No tier multipliers -- data quality, not hardware, determines weight.
- HMAC signing with tamper detection; stale/future delta rejection.
- Tests: `test_federation_functional.py` (23 tests) covers 3-node convergence, domination prevention, guardrail hash enforcement.

### 6. EventBus (Local + WAMP Bridge)

**Status: DELIVERED**

- `core/platform/events.py` with Crossbar WAMP bridge via autobahn.
- Topic mapping: `theme.changed` <-> `com.hartos.event.theme.changed`.
- `_from_wamp` flag prevents echo loops.
- Tests: `test_message_bus_functional.py` (8 tests) covers pub/sub, wildcards, thread safety.

### 7. Guardrail Immutability

**Status: DELIVERED**

- `security/hive_guardrails.py` -- 10-class guardrail network, `_FrozenValues`, module-level `__setattr__` guard.
- 33 constitutional rules, re-verified every 300 s.
- Protected files list includes `origin_attestation.py` and `LICENSE`.
- Tests: `test_security_modules_functional.py` validates hash chain integrity and tamper detection.

### 8. Master Key (Ed25519 Kill Switch)

**Status: DELIVERED**

- `security/master_key.py` -- public key verification flow, boot signature check.
- `security/key_delegation.py` -- 3-tier certificate chain (central -> regional -> local).
- Private key excluded from all code paths (AI exclusion zone enforced in CLAUDE.md).

### 9. Input Sanitization & DLP

**Status: DELIVERED**

- `security/sanitize.py` -- SQL-escape, HTML-escape, path-traversal stripping, input validation.
- `security/dlp_engine.py` -- PII scan/redact (email, phone, SSN, credit card, IP).
- Tests: `test_security_modules_functional.py` (141 tests) -- 7 security modules exercised.

### 10. Action Classifier (Destructive Detection)

**Status: DELIVERED**

- `security/action_classifier.py` -- pattern matching for destructive commands (rm, mkfs, dd, kill).
- PREVIEW_PENDING / APPROVED states for human confirmation.
- Tests: 19 classifier tests + 3 preview edge-case tests in the security functional suite.

### 11. Immutable Audit Log

**Status: DELIVERED**

- `security/immutable_audit_log.py` -- SHA-256 hash chain, `AuditLogEntry` table.
- Deletion or modification breaks chain; tamper detection catches it.
- Tests: genesis hash, chain continuation, deletion detection, 100-entry chain validation, detail-JSON redaction.

### 12. Channel-to-Device Control

**Status: DELIVERED**

- `integrations/channels/device_control_tool.py` -- any channel adapter routes commands to the user's local device via PeerLink.
- SAME_USER trust only; fleet-command fallback; embedded handler for GPIO/serial.
- Tests: `test_device_control_functional.py` (16 tests).

### 13. PeerLink P2P Communication

**Status: DELIVERED**

- `core/peer_link/` -- 7 files. Trust levels: SAME_USER, PEER (E2E), RELAY (E2E).
- Connection budget: flat=10, regional=50, central=200.
- Wired into bootstrap, gossip, federation, compute_mesh, world_model_bridge.

### 14. Agent Personality & Resonance Tuning

**Status: DELIVERED**

- `core/agent_personality.py` -- 37 tests. `core/resonance_tuner.py` -- EMA tuning, oscillation detection, federation delta export.
- `core/resonance_profile.py` -- 8-dimensional continuous floats.
- 100 tests total (63 resonance + 37 personality).

### 15. Better Tomorrow Seed Goal

**Status: DELIVERED**

- `integrations/agent_engine/goal_seeding.py` line 812 -- `bootstrap_better_tomorrow`.
- Scans community needs, developer requests, contributor wellbeing, environmental impact.
- Scores by `lives_impacted x urgency x feasibility / cost`.
- `requires_human_approval: True` -- never auto-spends. Humans decide.

### 16. Autonomous Upgrade Pipeline

**Status: DELIVERED**

- `upgrade_orchestrator.py` -- 7-stage pipeline (BUILD -> TEST -> AUDIT -> BENCHMARK -> SIGN -> CANARY -> DEPLOY).
- `is_upgrade_safe()` blocks on 5% regression.
- `hart-update-service.py` -- OTA systemd service (daily).

### 17. Anti-Fork Protection (BSL-1.1 + Origin Attestation)

**Status: DELIVERED**

- `LICENSE` -- BSL-1.1 with anti-rebranding, master key integrity clause. Auto-converts to Apache 2.0 on 2030-01-01.
- `security/origin_attestation.py` -- cryptographic origin proof, brand marker verification, federation handshake attestation.
- 34 tests in `test_origin_attestation.py`.

### 18. Compute Democracy (Logarithmic Scaling)

**Status: DELIVERED**

- Federation uses `log1p(interactions)` so no single entity controls >5% influence.
- Budget gate enforces per-model Spark costs; local models cost 0 Spark.
- Hosting reward uses weighted scoring across GPU hours, inferences, energy, API costs.

### 19. VLM Computer-Use Loop

**Status: PARTIAL**

- `integrations/vision/local_loop.py` -- Qwen 3.5 VLM with action parsing, bbox handling, safety-gate stubs.
- Tests: `test_vlm_loop_functional.py` (17 tests) validates control flow and action parsing.
- **Gap**: The iteration loop uses a fixed 30-iteration cap instead of a goal-aware termination condition. Redesign plan documented in MEMORY.md sections A2-A8.

### 20. Offline TTS (LuxTTS / Pocket TTS)

**Status: PARTIAL**

- `integrations/service_tools/luxtts_tool.py` -- sherpa-onnx ZipVoice-Distill INT8, zero-shot voice cloning, 24 kHz output.
- `integrations/service_tools/pocket_tts_tool.py` -- 8 built-in voices, in-process TTS.
- Fallback chain in `model_bus_service.py`: MakeItTalk cloud -> Pocket TTS offline.
- **Gap**: INT8 quantization produces audible quality artifacts in some voice-cloning scenarios. Under investigation.

---

## Summary

| Category | Delivered | Partial | Total |
|----------|----------:|--------:|------:|
| Core pipeline | 4 | 0 | 4 |
| Economics & federation | 3 | 0 | 3 |
| Security | 5 | 0 | 5 |
| Infrastructure | 4 | 0 | 4 |
| AI capabilities | 2 | 2 | 4 |
| **Total** | **18** | **2** | **20** |

The two PARTIAL items have working code paths and passing tests. The gaps are
in output quality (TTS) and loop termination strategy (VLM), not missing
implementations.
