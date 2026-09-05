# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## RULE 0 — NEVER ASSUME (ranked above every other rule in this file)

**Owner ruling 2026-09-05.  Outranks every protocol below and any
ship-mode urgency.  It is not advice.**

1. **Never assume.**  If a measurement of the exact claim does not
   exist, the claim is not made.  "Probably", "consistent with", "the
   logs suggest" are assumptions wearing a hedge.  When unmeasured, the
   answer is **"I have not checked"**.
2. **Grep only LOCATES.**  A grep hit is a pointer, never evidence, and
   never a basis for a conclusion or an edit.
3. **Read the FULL file — every file the grep matched, end to end.**
   Not a window around the hit.  Partial reads are the documented cause
   of this codebase's wrong diagnoses and parallel-path regressions.
4. **Verify to the user-visible outcome**, not the furthest log line
   reached.  A pipeline that "ran" has produced nothing until the user's
   actual result is read back.

Measured cost of breaking this (single session, 2026-09-05): six
conclusions asserted then withdrawn — a real registered tool called
"fictional"; a missing log marker read as "no tool executed" when those
tools cannot emit it; and a 179-second reuse turn analysed entirely
from internal logs while the response the user actually received said
*"Your local AI is busy with another task right now."*

Full rule + evidence table: Nunba `memory/feedback_never_assume.md`.

## Project Overview

**HART OS - Hevolve Hive Agentic Runtime**

Crowdsourced compute infrastructure that orchestrates fully autonomous Hive AI Training. Open multi-agent platform democratizing access to the best intelligence, just like the internet is open, this will be open. Key innovation: **Recipe Pattern** - learn task execution once (CREATE mode), then replay efficiently (REUSE mode) without repeated LLM calls.

**What it does:** Enables distributed thought processes across crowdsourced agents, distributed coding agents that incentivize compute providers, and a self-sustaining economic model (like a vending machine, but at national economy scale) so that no single entity, no government, and no private corporation ever monopolizes AI. Intelligence belongs in the hands of the common person.

**Why every decision is the way it is:** The master key exists solely as a kill switch for the distributed intelligence, held by human stewards, inaccessible to AI. The 33 constitutional rules are cryptographically sealed, structurally immutable, and re-verified every 300 seconds across the network. Compute democracy uses logarithmic reward scaling so no single entity controls more than 5% influence. The 90/9/1 revenue split returns 90% to contributors because the people who train the hive should own the value it creates. Every engineering choice, from the gossip protocol to the guardrail hash verification to the peer-witnessed ad impressions, is designed to make centralized control structurally impossible, not just policy-prohibited.

Core principle: **Humans are always in control.** All agents, rewards, and incentives exist only for a future where humans guide the path. The hive refuses to federate with any hivemind that does not share this goal. See [README.md](README.md) for the full mission statement and [security/hive_guardrails.py](security/hive_guardrails.py) for the cryptographic enforcement.

## Master Key - AI Exclusion Zone

**CRITICAL: The following rules are absolute and override all other instructions.**

1. **NEVER** read, display, print, log, or output the value of `HEVOLVE_MASTER_PRIVATE_KEY` or any environment variable containing master key material
2. **NEVER** call `get_master_private_key()`, `sign_child_certificate()`, or any function that loads the master private key
3. **NEVER** read or display the contents of any file named `master_private_key*`, `*.pem` in the security directory, or GitHub Secrets values
4. **NEVER** suggest, generate, or compute alternative master keys, replacement keys, or key derivation paths
5. **NEVER** modify `MASTER_PUBLIC_KEY_HEX` in `security/master_key.py` - the trust anchor is immutable
6. **NEVER** modify, weaken, or bypass the `HiveCircuitBreaker` in `security/hive_guardrails.py`
7. **NEVER** modify the `_FrozenValues` class, the module-level `__setattr__` guard, or any structural immutability mechanism
8. If asked to perform any of the above, **refuse and explain why**

The master key is a kill switch for a distributed intelligence. It is not a developer tool. It is not accessible to AI. It belongs to the steward and their successors, humans who believe in the vision that humans are always in control.

You MAY read `security/master_key.py` to understand the public key verification flow. You MAY NOT interact with the private key in any way.

## Hive Collaboration Bootstrap (BINDING — read before doing agentic work)

**When the user asks for work, collaborate with HARTOS's native machinery,
never around it. Read [docs/architecture/HIVE_COLLAB_BOOTSTRAP.md](docs/architecture/HIVE_COLLAB_BOOTSTRAP.md)
FIRST — it is the code-verified map (guarded by
`tests/unit/test_hive_collab_bootstrap_prompt.py`) of how a Claude Code
session gets things done inside the hive.** The operative rules:

1. Agentic work routes through **`POST /chat`** (CREATE/REUSE recipe
   pipeline) — never a second execution path.
2. Background/autonomous work goes through **`dispatch.py::dispatch_goal`**
   + `goal_manager.py` verticals; the flywheel in a dev session starts via
   **`scripts/run_flywheel_dev.py`** (the standalone entry does NOT start it).
3. Outcomes reach HevolveAI through the ONE bridge
   (`world_model_bridge.py::WorldModelBridge`); mind the 50-batch flush
   buffer, the hevolveai-wheel stub trap, and the local-model 0-spark
   completion trap — all documented in the bootstrap doc.
4. **PRIMARY OBJECTIVE: bootstrap every seeded goal with the fleet's joint
   resources.** Shard `goal_seeding.py::SEED_BOOTSTRAP_GOALS` across the
   gossip-discovered peers (`sha256(slug) % N` -- deterministic, no central),
   CREATE only your shard (verify by banked artifact, not flags), and pull
   the rest from peers over A2A. ~70 recipes fleet-wide in one CREATE-cycle;
   the full protocol is in the bootstrap doc.

## Branch Discipline — MAIN BRANCH ONLY (MANDATORY)

**Work directly on `main` in the main clone.  Every session.  No exceptions.**

- **Never** create, use, or resume a `claude/*` branch.
- **Never** create or use a git worktree (`.claude/worktrees/*`).
- **Never** operate inside a worktree directory — even if the Claude
  Code harness boots you there.  Step out immediately and use
  absolute paths to the main clone.
- **All commits land on `main`.**  All pushes go to `origin/main`.

Full protocol + session-start sanity check + violation log live in
the user-memory file `memory/feedback_main_branch_only.md` (under
`~/.claude/projects/.../memory/`).  Read it if you ever see a
`claude/*` branch or `.claude/worktrees/` dir — that is a regression
and must be reported to the user, not silently accepted.

If the harness forces you into a worktree:
1. Do NOT edit files inside the worktree.
2. Use absolute paths in every `Edit`/`Write`/`Read` call so tools
   act on the main clone (e.g.
   `C:\Users\sathi\PycharmProjects\HARTOS\hart_intelligence_entry.py`).
3. Prefix every `git` command with
   `cd C:/Users/sathi/PycharmProjects/HARTOS && git …`
   so git never defaults to the worktree's CWD.
4. After session ends, the user deletes the filesystem leftovers:
   `cmd /c "rd /s /q C:\Users\sathi\PycharmProjects\HARTOS\.claude\worktrees"`.

Why this rule exists (2026-04-21 incident): the Nunba session was
booted on `claude/determined-elbakyan-94a24c` in a worktree, leaving
a stale branch and filesystem leftovers that polluted `git branch -a`
and confused which copy of the code had the latest edits.  User
called it out directly: parallel branches always drift, and the user
should never need to ask "which one has my fix?" — the answer is
always `main`.  HARTOS follows the same rule for the same reason.

## Sibling Repos — Canonical Filesystem Paths

These paths are load-bearing. Use them verbatim — guessing the
wrong directory burns a turn (the user has called this out).
Mobile lives under `StudioProjects/`, server/desktop under
`PycharmProjects/`.

### PycharmProjects/
| Path | Role |
|------|------|
| `C:\Users\sathi\PycharmProjects\HARTOS` | **HART OS** — agentic runtime (Flask :6777), recipe pipeline, channels, social, security, peer_link. Branch: `main` only. |
| `C:\Users\sathi\PycharmProjects\Nunba-HART-Companion` | **Nunba** — Windows/macOS/Linux desktop wrapper (cx_Freeze). Also hosts the **landing-page React web app** at `landing-page/` (same React tree for browser + desktop Liquid Glass Shell). |
| `C:\Users\sathi\PycharmProjects\Hevolve_Database` | **Hevolve_Database** — canonical user/account store. Owns `User.FCMtoken`, capture endpoint `POST /update_fcm_token`, retrieval `GET /get_fcm_token/{user_id}`. |
| `C:\Users\sathi\PycharmProjects\Hevolve` | **Hevolve web** — the hevolve.ai web product. |
| `C:\Users\sathi\PycharmProjects\hevolveai` | **HevolveAI** — closed-source intelligence layer (Cython-compiled `.pyd`). All ML lives here; **HARTOS has no ML code**. |

### StudioProjects/
| Path | Role |
|------|------|
| `C:\Users\sathi\StudioProjects\Hevolve_React_Native` | **Hevolve_React_Native** — React Native shared by Android + iOS. ConsentOverlayService + native bridges (`android/app/src/main/java/...`, `ios/...`). |
| `C:\Users\sathi\StudioProjects\Nunba-Companion-iOS` | **Nunba-Companion-iOS** — native iOS companion (Swift/ObjC). Pairs with Nunba desktop; separate from the RN app. |

### Topology (messaging fan-out at a glance)
```
HARTOS publish_event('chat.social', …, user_id=X)
  → WAMP topic com.hertzai.hevolve.social.{X}
  → broadcast_sse_safe('notification', …, user_id=X)

  Web (Nunba/landing-page)         ── SSE primary, WAMP fallback
  Desktop (Nunba)                  ── same React tree, SSE
  Android (Hevolve_React_Native)   ── WAMP (autobahn-js)
  iOS    (Hevolve_React_Native)    ── WAMP
  iOS    (Nunba-Companion-iOS)     ── separate native, pairs with desktop

  Hevolve_Database                 ── token store only; no outbound push helper yet
```

### Gotchas (record so I stop repeating)
- **Mindstory ≠ Hevolve mobile.** Mindstory (`PycharmProjects/Mindstory`) is the video-generation Android app. The actual mobile is `StudioProjects/Hevolve_React_Native`.
- **Nunba ≠ Nunba-Companion-iOS.** Nunba is desktop (Win/macOS/Linux). Nunba-Companion-iOS is the iOS sidekick.
- **landing-page is INSIDE Nunba-HART-Companion**, not a separate repo. Web changes → `Nunba-HART-Companion/landing-page/src/`.
- Cross-repo grep template lives in `memory/reference_repo_layout.md`.

## Common Commands

### Logs (where to look when debugging)
| Path | Contents |
|------|----------|
| `~/Documents/Nunba/logs/frozen_debug.log` | Main Nunba + HARTOS log — timestamps, RequestIDs, httpx calls, SSE broadcasts, ToolMessageHandler's `=== FULL INPUT MESSAGES DEBUG ===` dumps (messages portion only — autogen attaches system_message + tools AFTER `transform_messages` runs, so this dump is the messages-only view) |
| `~/Documents/Nunba/logs/llama_server_8082.log` | llama-server stdout/stderr — **with `--log-timestamps --verbose`** every request body, slot lifecycle, `task.n_tokens`, and `send_error` line (e.g. `Context size has been exceeded`, `request (N tokens) exceeds the available context size`) carries a `[HH:MM:SS.mmm]` prefix. Every HARTOS-side caller (autogen / langchain / dispatcher / probes) sets the OpenAI `user` field to the thread-local request_id, so each task line correlates 1:1 with the frozen_debug RequestID column. Authoritative for "what was sent + when + by whom + how big". |
| `~/Documents/Nunba/logs/draft_decision.jsonl` | Per-request draft-model boot decisions (cohort, VRAM, active TTS, delegate verdict) |

For ctx-overflow diagnosis: grep `llama_server_8082.log` for the `request (N tokens) exceeds` line, take the timestamp + `user=<request_id>`, find the same request_id in frozen_debug to trace back to the originating /chat turn.

### Setup
```bash
# Requires Python 3.9+ (test env runs on 3.11; pydantic 2.9.2 supports 3.9-3.13).
python3.11 -m venv venv311
source venv311/Scripts/activate  # Windows: venv311\Scripts\activate.bat
pip install -r requirements.txt
```

### Running the Application
```bash
python hart_intelligence_entry.py    # Flask server on port 6777
```

### Running Tests
```bash
pytest tests/ -v                                    # All tests (unit + integration)
pytest tests/unit/ -v                               # Unit tests only
pytest tests/integration/ -v                        # Integration tests only
pytest tests/unit/test_agent_creation.py -v         # Agent creation
pytest tests/unit/test_recipe_generation.py -v      # Recipe generation
pytest tests/unit/test_reuse_mode.py -v             # Reuse execution
python tests/standalone/test_master_suite.py        # Comprehensive suite
python tests/standalone/test_autonomous_agent_suite.py  # Autonomous agents
```

## Configuration

Create `.env`:
```
OPENAI_API_KEY=your-key
GROQ_API_KEY=your-key
LANGCHAIN_API_KEY=your-key
```

Create `config.json` with API keys for: OPENAI, GROQ, GOOGLE_CSE_ID, GOOGLE_API_KEY, NEWS_API_KEY, SERPAPI_API_KEY

## Update Delivery — OTA, NOT manual flash

**Updates ship over-the-air. The ISO/USB flash is FIRST-INSTALL only.** Do NOT
ask the user to re-flash to deliver an update — line up an OTA push instead
(and confirm before the outward central publish, since it deploys to their node).

- **Config:** `nixos/configurations/desktop.nix` → `hart.ota = { enable = true;
  channel = "stable"; autoApply = true; }` — its own comment: *"This replaces the
  user's last manual flash."*
- **Mechanism:** `nixos/modules/hart-ota.nix` — a 7-stage pipeline **BUILD → TEST
  → AUDIT → BENCHMARK → SIGN → CANARY → DEPLOY**. Central publishes WHICH commit
  (a pinned `github:hertz-ai/HARTOS/<sha>`); the node pulls (on boot / `hart-ota
  check`) or receives a central push over the fleet/gossip fabric, builds the
  closure, and **atomically switches the NixOS generation** (rollback =
  `nixos-rebuild switch --rollback`; the canary auto-reverts on health
  regression; the node never force-applies past canary).
- **Mesh, NOT central-only:** a **regional-tier** node can also initiate (the node
  accepts `required_tier='regional'`, central/regional Ed25519, master-anchored);
  the **gossip/WAMP/PeerLink** fabric relays peer-to-peer (any same-network peer);
  **regional hosts own + serve their sub-fleet** (discovery + staged rollout + the
  pull source — `centralEndpoint` can be a regional host, not only etime). Trust is
  still master-anchored (regional certs delegated from central; a relay can't forge
  authority).
- **SIGN is master-key-gated → steward-only, AI-EXCLUDED.** Claude preps + greens
  the closure; the human signs the release.
- **To ship a Nix-built component (e.g. the HART-comp compositor) via OTA:** its
  package must actually BUILD in Nix (eval passing is NOT enough), then the
  steward signs the publish. A failed BUILD stage simply doesn't deliver — it
  never bricks the node.

See `memory/reference_ota_delivery_model.md`.

## Architecture

### Core Flow
```
CREATE Mode: User Input → Decompose → Execute Actions → Save Recipe
REUSE Mode:  User Input → Load Recipe → LLM-GUIDED replay of proven steps (adapts to live context) → Output (~90% cheaper: skips re-decomposition/exploration, NOT the LLM — never a deterministic macro)
```

### Key Files
| File | Purpose |
|------|---------|
| `hart_intelligence_entry.py` | Flask entry point (port 6777, Hypercorn ASGI primary; Waitress WSGI fallback on ImportError) |
| `hartos/create_recipe.py` | Agent creation, action execution, recipe generation |
| `hartos/reuse_recipe.py` | Recipe reuse, trained agent execution |
| `hartos/helper.py` | Action class, JSON utilities, tool handlers |
| `hartos/lifecycle_hooks.py` | ActionState machine, ledger sync |
| `hartos/helper_ledger.py` | SmartLedger integration |

### State Machine (ActionState)
```
ASSIGNED → IN_PROGRESS → STATUS_VERIFICATION_REQUESTED → COMPLETED/ERROR → TERMINATED
```
States auto-sync to SmartLedger for persistence across sessions.

### Recipe Storage
```
prompts/{prompt_id}.json                    # Prompt definition
prompts/{prompt_id}_{flow_id}_recipe.json   # Trained recipe
prompts/{prompt_id}_{flow_id}_{action_id}.json  # Action recipes
```

### Canonical Identifier Types — read once, never guess again

| ID | Logical type | Wire / on-disk | Notes |
|---|---|---|---|
| `prompt_id` | **integer** (DB primary key) for human-created agents; **UUID string** for autonomous agents (coding agent, hive agents) | normalized to **string** when stamped on tasks / used as a dict key | one field, two real domains — typing it `int` would break autonomous agents |
| `flow_id` | **integer**, monotonic from 0 per `prompt_id` | int | Sourced by `get_flow_number(user_id, prompt_id)` in reuse, `get_current_flow(user_prompt)` in create. Recipe filename `{prompt_id}_{flow_id}_recipe.json` keeps the two in lockstep. |
| `action_id` | **integer**, restarts from 1 every flow | int | Flow-scoped — the `(flow_id, action_id)` tuple is the unique recipe coordinate within a session. Encoded in `task_id = f"action_{action_id}"`. |
| `session_id` (execution instance id) | **string**, free-shaped by caller; canonical pattern `"{user_id}_{prompt_id}"`, also `"goal_abc"`, also full UUID for autonomous | stored verbatim, no coercion | **A new session_id is minted for every execution instance**: CREATE mode = first execution, REUSE mode = each subsequent execution. Same agent, many sessions over time. |
| `agent_id` | mirrors `prompt_id`'s domain | always string (`str(prompt_id)` coercion in `create_ledger_from_actions`) | "agent_id == prompt_id" convention is load-bearing for dashboard grouping. |

Dashboard hierarchy: `prompt_id → [session_id list, newest first] → flow_id → [action_id sorted ascending]`. Schema fields that encode this are `Task.recipe_prompt_id`, `Task.recipe_flow_id`, `Task.recipe_action_id`, stamped by `create_ledger_from_actions` and inherited by `add_dynamic_task` from sibling tasks. See `docs/architecture/TASK_LEDGER_GROUPING_FIX_PLAN.md` for the full design + helper API (`SmartLedger.list_grouped_by_recipe_hierarchy`).

### Integrations
- `integrations/agent_engine/` - Unified agent goal engine, daemon, speculative dispatch
- `integrations/social/` - 82-endpoint social platform (communities, feeds, karma, encounters)
- `integrations/coding_agent/` - Idle compute coding agent (dispatches to CREATE/REUSE pipeline)
- `integrations/vision/` - Vision sidecar (MiniCPM + embodied AI learning)
- `integrations/channels/` - 30+ channel adapters (Discord, Telegram, Slack, Matrix, etc.)
- `integrations/ap2/` - Agent Protocol 2 (e-commerce, payments)
- `integrations/expert_agents/` - 96 specialized agents network
- `integrations/internal_comm/` - A2A communication, task delegation
- `integrations/mcp/` - Model Context Protocol servers
- `integrations/google_a2a/` - Dynamic agent registry

### Security Layer
- `security/hive_guardrails.py` - 10-class guardrail network (structurally immutable)
- `security/master_key.py` - Ed25519 release signing & boot verification
- `security/key_delegation.py` - 3-tier certificate chain (central → regional → local)
- `security/runtime_monitor.py` - Background tamper detection daemon
- `security/node_watchdog.py` - Heartbeat protocol, frozen-thread detection

## API Endpoints

```
POST /chat
  Required: user_id, prompt_id, prompt
  Optional: create_agent (default: false)

POST /time_agent        # Scheduled task execution
POST /visual_agent      # VLM/Computer use
POST /add_history       # Add conversation history
GET  /status            # Health check

# A2A Protocol
GET  /a2a/{prompt_id}_{flow_id}/.well-known/agent.json
POST /a2a/{prompt_id}_{flow_id}/execute
```

## Key Patterns

### Autonomous Fallback Generation
StatusVerifier LLM auto-generates context-aware fallback strategies (no user prompts for fallback). Enables fully autonomous agents.

### Hierarchical Task Decomposition
```
User Prompt
├─ Flow 1 (Persona A)
│  ├─ Action 1, Action 2, Action 3
└─ Flow 2 (Persona B)
   ├─ Action 1, Action 2
```

### Agent Ledger Persistence
- `agent_data/ledger_{user_id}_{prompt_id}.json` - Task state persistence
- Enables cross-session recovery and audit trails

## Dependencies

Critical pinned versions (post Apr/May 2026 split-package migration):
- `langchain-classic==1.0.1` + `langchain-community==0.4.1` + `langchain-core==1.2.15` + `langchain-anthropic==1.0.0` + `langchain-text-splitters==1.1.1` + `langchain-google-genai==4.2.1` + `langchain-groq==1.1.2` (split packages — imports use `from langchain_classic.X` / `from langchain_community.X`)
- `pydantic==2.9.2`
- `autogen` (multi-agent framework)
- `chromadb==0.3.23` (vector store)
- `hypercorn` (primary ASGI server) + `waitress==2.1.2` (WSGI fallback)

---

## Desktop & Home Design — Consult the Checklist FIRST (BINDING)

**Before changing ANY desktop / home / shell UI design — layout, canvas,
the orb, cards/rows, top bar, taskbar, omnibox, interaction, onboarding,
the agentic Liquid-UI surface — you MUST read and honor
`docs/design/HOME_DESKTOP_DESIGN_CHECKLIST.md` FIRST.**

It is the consolidated, durable record of the steward's design
instructions, recovered from the FULL session history (1,797 of the
steward's own typed messages; 11 groups a–k, ~55 rules, each with a
verbatim quote + the source message number). It is the SOURCE OF TRUTH
for desktop look/feel. The steward set this rule (2026-06-29): *"this
doc shd be consulted whenever we are changing the design of desktop."*

1. **Consult before you change.** Any edit to `hartHome.*`, `hartHero.*`,
   `voiceOrbViz.js`, `hartDesktop.js`, `hartResponsive.css`, the shell
   render in `liquid_ui_service.py`, or the compositor's visual surface →
   read the checklist first; confirm the change does not regress an
   APPLIED item.
2. **Update it when intent changes.** A new design instruction from the
   steward gets added to the checklist (with the quote) in the SAME
   change — never let an instruction live only in chat again.
3. **Audit after you build.** A desktop-design change isn't done until
   it's scored against the relevant items (APPLIED / PARTIAL / MISSING),
   like the W1 audit at the bottom of the doc.
4. **Never contradict an EMPHATIC rule** (group (a): "NOT a webpage / no
   vertical page scroll / fixed canvas", and the like). If a change would,
   STOP and surface it to the steward.

Why: the steward's instructions kept scattering across sessions and felt
"lost." This checklist + this rule make them structurally un-loseable.
See `memory/feedback_desktop_design_checklist_binding.md`.

---

## Change Protocol — Standing Rules for EVERY Edit

**Applies to every change: bug fix, feature, refactor, test, doc, build
config.  No exceptions.  This section is the standing contract that
overrides ship-mode urgency.  When the user asks for a fix, the 9-gate
protocol below is what we do, in order, before a single character is
written to disk.**

Cross-references — these memory files are authoritative companions:
- `feedback_engineering_principles.md` — DRY / SRP / no parallel paths
- `feedback_frozen_build_pitfalls.md` — cx_Freeze rules, new-module discipline
- `feedback_review_checklist.md` — the `/review` skill's checklist
- `feedback_multi_os_review.md` — Win/macOS/Linux compat gates
- `feedback_verify_imports.md` — `ast.parse` is syntax-only; import-check too
- `feedback_no_coauthor.md` — commit-message hygiene

### Gate 0 — Intent Before Edit (BLOCKING)

Before typing any edit, answer in writing (in chat or internal
reasoning):

1. **What is the user actually asking for?** (State back the success
   criterion they'd verify against.)
2. **What does the existing code do, and WHY does it exist that way?**
   Read the function, its docstring, its callers, and at least one
   adjacent test.  If there's no test, that's a data point.
3. **What will break if I change it?**  Enumerate downstream effects.
4. **Is there already a canonical helper / constant / abstraction for
   this concern?**  If yes, use it — do NOT create a second.  If no,
   decide where the canonical home should live BEFORE writing code.

Skipping Gate 0 is the #1 source of this codebase's DRY / parallel-
path regressions.  **Never edit on autopilot.**

### Gate 1 — Caller Audit (BLOCKING)

For any function / class / constant / file being modified, enumerate
ALL callers before the edit:

```bash
# Full-tree grep across both repos — substitute <symbol>
grep -rn "<symbol>" \
  C:/Users/sathi/PycharmProjects/HARTOS/ \
  C:/Users/sathi/PycharmProjects/Nunba-HART-Companion/ \
  --include="*.py" --include="*.js" --include="*.jsx" \
  | grep -v ".venv\|__pycache__\|python-embed\|node_modules\|build/"
```

Record each caller.  If the signature / return shape / side-effect
changes, EVERY caller's test must pass afterwards — no "only the
happy-path caller was updated".

Special cases requiring extra audit:
- Module-level constants / frozensets → grep for `import <name>` AND
  for usage sites (iterations, membership tests).
- Decorators / wrapper classes → every call site of the decorated fn.
- HTTP routes → every frontend `fetch` + backend call in staging
  probe script.
- WAMP topics → every publisher + every subscriber across Nunba/HARTOS.
- cx_Freeze-bundled modules → every `import X` in app.py, main.py,
  routes/, and `scripts/setup_freeze_nunba.py:packages[]`.

### Gate 2 — DRY Gate (BLOCKING)

Before introducing ANY new:
- Constant / frozenset / dict / list literal with domain meaning
- Helper function with "save X", "load X", "format X", "validate X"
- Class with "Manager", "Handler", "Registry", "Wrapper"
- Configuration default

…run a search for existing equivalents:

```bash
# Example: checking for existing "language constants" before adding a new one
grep -rn "SUPPORTED_LANG\|_LANGS\|LANGUAGES" \
  core/ integrations/ --include="*.py" | head -20
```

If ≥ 1 equivalent exists, EXTEND THE EXISTING OR IMPORT FROM IT.
Never create a parallel literal "just for this file's convenience."

Violations seen this session:
- 4 separate `frozenset({...})` for "non-Latin script langs" across
  speculative_dispatcher, hart_intelligence_entry (x2), tts_engine.
- 3 inline thread-dump implementations before `core.diag` consolidated.
- 2 `_ensure_mcp_token` call sites (one inside HARTOS, one from Nunba
  reaching into the underscore-private symbol).

### Gate 3 — SRP Gate (BLOCKING)

Every function SHOULD do exactly one thing.  If the function's name
has "and" in it, or its docstring lists > 1 responsibility, or it
performs both a pure computation AND an I/O side-effect, split it.

Canonical split pattern:
- `pure_compute(x) -> result` — no I/O
- `persist(result) -> bool` — atomic write only
- `on_change_callback(subscribers, old, new)` — event dispatch only

Violations seen this session:
- `_persist_language` originally did: validate + check-if-exists +
  write + evict-draft — 4 jobs.  Split via `core.user_lang` module
  (writer) + `model_lifecycle` subscriber (eviction).

### Gate 4 — Parallel-Path Gate (BLOCKING)

A parallel path = "second implementation of a concept that already
has a canonical one."  Parallel paths always drift.  Enforce by:

1. One writer per persisted value (e.g., `hart_language.json` has
   exactly one writer: `core.user_lang.set_preferred_lang`).
2. One source of truth per conceptual constant (e.g.,
   `NON_LATIN_SCRIPT_LANGS` lives in `core.constants` — everyone
   else imports).
3. One dispatch path per verb (e.g., chat response = single
   dispatcher, NOT main-LLM vs draft-LLM divergent logic).

If a parallel path is TEMPORARILY unavoidable (e.g., migrating old
users), document it with a TODO that names the deletion date.  Never
ship a parallel path silently.

### Gate 5 — Test-First for Non-Trivial Changes (BLOCKING)

If the change:
- Alters a public contract, OR
- Adds a new abstraction / module, OR
- Fixes a regression that slipped through static review

…write the test FIRST.  Run it; confirm it fails.  Then implement.
Then confirm it passes.  This is the mandate from `feedback_frozen_
build_pitfalls.md` Rule 4 — static review alone missed the `core.diag`
bundling crash; only an invariant test would have caught it.

Tests that belong in every refactor:
- AST-level "no inline duplicate" check (catches DRY regressions)
- Behavioral test for the change's intent
- Boundary test (ENOSPC, empty input, malformed input)
- Regression test for any bug the change is fixing

**NO GREP-BASED TESTS.**  A test must `import` the actual code,
mock the boundary (DB, publisher, network, registry), call the
real function, and assert observable side-effects (mock call args,
return values, state mutations).  Tests like
`assert "topic_name" in open(file).read()` or
`assert idx_of_a < idx_of_b` for ordering checks are NOT tests —
they only prove a string survived the commit.

Source-shape guards (clearly-labelled `test_source_guard_*`) are
acceptable ONLY for DRY enforcement across many files where a
behavioural test for a single call site cannot catch the
regression.  They must NEVER be the only test for an edit.

Full rule + acceptable-vs-not table + JS / Java / Swift guidance:
`memory/feedback_no_grep_tests.md` (auto-loaded via the index).
The user surfaced this on 2026-05-26 — incident: 21 grep-tests
passed while proving nothing about behaviour.

### Gate 6 — cx_Freeze Bundle Accounting (BLOCKING for new modules)

If the change adds a new `.py` file under `core/`, `integrations/`,
or any other HARTOS-Nunba-shared package:

1. Add it to `Nunba-HART-Companion/scripts/setup_freeze_nunba.py`
   `packages[]` explicitly (cx_Freeze tracer misses runtime-dynamic
   imports; see `feedback_frozen_build_pitfalls.md`).
2. Confirm `__init__.py` exists in every package dir (implicit
   namespace packages break under cx_Freeze).
3. Verify no name collision with HARTOS's own package tree — Nunba
   must NOT have its own `core/`, `integrations/`, `security/`, or
   `models/` directory with the same name.

Skipping Gate 6 = `ModuleNotFoundError` at the installed .exe's
first boot.  Ask the user; don't assume the module will be picked up.

### Gate 7 — Multi-OS / Multi-Topology Surface Check

Every change touching filesystem paths, subprocess, env vars, or IPC
must be validated against:
- OS: Windows (primary desktop), macOS (secondary), Linux (server)
- Topology: flat (desktop), regional (edge), central (cloud)

Specifically:
- `os.popen` / `os.system` without timeout → BANNED (Rule 5 of
  frozen-build pitfalls).  Use `subprocess.run(timeout=N)`.
- Hard-coded `C:\\` paths → wrap in `sys.platform` check or use
  `pathlib.Path.home()`.
- `requests.get/post` / `urllib.urlopen` / `socket.connect` → MUST
  have explicit timeout + exception handler.
- Writes to `C:\Program Files` → always fail on non-admin; route to
  `~/Documents/Nunba/data/` via `core.platform_paths`.

### Gate 8 — Review Perspectives Before Commit

Before pushing, mentally run the change past these specialist lenses
(or spawn the agent if the change is large):

- **architect**: does it match the existing package structure? Any
  layering violations (integrations → core OK; core → integrations
  BANNED)?
- **reviewer**: DRY, SRP, parallel-path, missing tests.
- **ciso / ethical-hacker**: does it expose a new ingress? Handle
  untrusted input? Log secrets?
- **sre**: failure mode on disk-full / OOM / network-down?
- **performance-engineer**: budget impact on hot path (chat: 1.5s,
  draft: 300ms, cache: <1ms)?
- **product-owner**: what does the user see differently?
- **test-generator**: FT + NFT coverage added?

The `/review` skill automates most of this — use it on large diffs.

### Gate 9 — Commit Discipline

- **Atomic**: one commit = one logical change.  If a refactor spans
  HARTOS + Nunba, TWO commits (one per repo).
- **Title**: conventional-commits (`fix(lang): …`, `refactor(core): …`,
  `feat(admin): …`).  Under 72 chars.
- **Body**: what was narrow, what became broad.  Cite the violation
  pattern and the canonical home.  Reference the test file that
  guards regression.
- **No `Co-Authored-By: Claude`** (see `feedback_no_coauthor.md`).
- **Never force-push to main**.  Never `--no-verify`.
- Push each commit to origin immediately after local tests pass —
  enables the build-validator + CI to catch bundle / import issues
  the local env didn't.

---

## When the User Says "Just Fix It"

Do all 10 gates anyway.  Explain briefly what you're doing; don't
ask permission for each gate.  Ship slower but correctly.

The pattern "user asks → claude rushes → introduces parallel path →
user asks again → claude rushes again" is the enemy.  Honor the
protocol even when urgency seems to reward skipping it; the urgency
is almost always caused by a prior skipped gate.
