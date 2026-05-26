# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

## Common Commands

### Setup
```bash
# Requires Python 3.10 (pydantic 1.10.9 incompatible with 3.12+)
python3.10 -m venv venv310
source venv310/Scripts/activate  # Windows: venv310\Scripts\activate.bat
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

## Architecture

### Core Flow
```
CREATE Mode: User Input → Decompose → Execute Actions → Save Recipe
REUSE Mode:  User Input → Load Recipe → Execute Steps → Output (90% faster)
```

### Key Files
| File | Purpose |
|------|---------|
| `hart_intelligence_entry.py` | Flask entry point (port 6777, Waitress server) |
| `create_recipe.py` | Agent creation, action execution, recipe generation |
| `reuse_recipe.py` | Recipe reuse, trained agent execution |
| `helper.py` | Action class, JSON utilities, tool handlers |
| `lifecycle_hooks.py` | ActionState machine, ledger sync |
| `helper_ledger.py` | SmartLedger integration |

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

Critical pinned versions:
- `langchain==0.0.230`
- `pydantic==1.10.9` (requires Python 3.10)
- `autogen` (multi-agent framework)
- `chromadb==0.3.23` (vector store)

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
