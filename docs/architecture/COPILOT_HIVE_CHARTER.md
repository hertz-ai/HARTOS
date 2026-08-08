# The Copilot / Hive Charter

**Status:** living document. This is the editable source of truth for what the
HART copilot is and what the hive is for.

## Why this file exists

The intent below was captured four times as HARTOS goal records
(`a2d582cc`, `c1f17a81`, `d116cf3f`, `c2b80734`) and drifted each time, because
**a goal record cannot be edited** — the MCP surface offers create / dispatch /
steer and nothing else, so every correction minted a new goal while the wrong
ones stayed `active` and kept competing for dispatch. Two of them now state
things that are provably false (see *Corrections already earned* below).

So the charter lives here, in the repo, where it is versioned, diffable and
reviewable. `c2b80734` is the goal of record; **when it and this file disagree,
this file wins.**

---

## 1. What the copilot is

A computer-using agent that works the way a Claude Code session works — takes a
real objective, drives the machine, checks its own result — except **its hands
are HARTOS's own interfaces**, not a screenshot-and-click loop bolted on the
side. Screen-scraping the desktop would be the parallel path. The OS already
exposes everything natively:

| capability | the native surface | state |
|---|---|---|
| **Compose the surface** (its *output*) | `agent_ui_update` / A2UI, `liquid_ui_service.py` | live; 25+ `COMPONENT_TYPES`, runtime types carry `_spec` so a new one renders on **first push** |
| **Arrange real windows** | `com.hart.Compositor` | **11 verbs live** in `compositor/src/ipc.rs`; Python side built + tested |
| **Drive the OS** | `shell_os_apis` / `shell_desktop_apis` / `shell_system_apis`, `app_installer.py` | live; every shell-out via `core.subprocess_safe.run_probe()` |
| **Do agentic work** | `POST /chat` — the CREATE/REUSE pipeline | live; the ONE execution path |
| **See** | `integrations/vision/`, `integrations/vlm/` | last resort — a native API beats a screenshot |

Missing from the compositor: `window.summon` (§4.6), `output.set_mode` (§4.9).
Binding spec invariant (`IPC_PROTOCOL.md` §9): *`window.*` extends A2UI, never
replaces it.* A2UI is what the agent composes; `window.*` is how it is arranged.

**Interface = Nunba + HARTOS.** Nunba is where the user gives work and sees it —
the floatable microfrontend carrying the Liquid UI, the same React tree on
desktop and web, a full participant rather than a viewer. The copilot *manages*
that surface.

**It already half exists.** `scripts/hart_copilot_daemon.py` is 463 lines of
real agentic loop — `next_task()` → `build_prompt()` → `start_branch()` → a real
coding-agent executor (explicitly *"rather than a single /chat turn"*) →
`open_pr()`, with a per-hour rate cap, a `hive_halted()` check, and
`yield_to_user()`. **Extend this daemon. Do not write a second copilot.**

---

## 2. Order of precedence — the product, not a detail

1. **The user's own tasks, on their own machine, first.** Done naturally and
   comfortably, better than any existing agent. Local-first, private by default,
   no network, no account. Works offline.
2. Goals the agents pick up from the apps the user gives them.
3. Hive goals (the 70 in `SEED_BOOTSTRAP_GOALS`) — **spare capacity only**.

A hive task must never make the user's own work slower. Already enforced by
`yield_to_user()`, foreground preempt via closable-httpx background abort, and
the llama priority scheduler.

---

## 3. The economic thesis: remove the commission

> *"all channels and crowdsource compute to run the services which removes the
> commision of every app which works in the world to get things done more
> distributed"* — steward, 2026-08-08

Every intermediary app in the world charges rent for **matching** — Uber,
DoorDash, Airbnb, the freelance platforms, the ticket resellers. The rent is not
payment for the service; the driver drives, the cook cooks, the tutor teaches.
The rent is payment for **running the matching computation and owning the
directory**. Typically 15–30%.

That computation is small. It only commands rent because one company owns the
servers it runs on and the directory it matches against.

**So run it on the participants' own machines.** Crowdsourced compute executes
the matching; the mesh is the directory; there is no operator in the middle, so
there is no commission to pay. What the revenue split then distributes is not a
platform's cut but the value the participants created — **90/9/1**,
`revenue_aggregator.py`.

This is already seeded, not aspirational: **12 `p2p_*` verticals** in
`SEED_BOOTSTRAP_GOALS` — rideshare, marketplace, grocery, food, bills, tickets,
freelance, tutoring, services, rental, health, logistics.

Two pieces make it reachable by real people:

- **All channels.** `integrations/channels/` carries 30+ adapters (Discord,
  Telegram, Slack, Matrix, WhatsApp, …). **A user needs no app install and no
  account** — they reach the service through a channel they already use. This is
  what makes "no multinational product in the stack" true at the point of use,
  not just in the backend.
- **AP2.** `integrations/ap2/` (Agent Protocol 2) carries e-commerce and
  payments, so value can actually move without routing through a platform.

**The honesty bar for this claim.** A commission-free vertical that quietly
depends on a central matcher has simply moved the rent somewhere less visible.
The test is the one in §5: **pull central and watch it keep working.** Anything
that *requires* central is tech debt (decentralization-first lens).

---

## 4. How it grows the hive — the ledger, never a new distributor

> *"we have task ledger use the task ledger to distribute tasks and agents
> pickup in each machine to get things done rather than implementing sharding in
> any different form all over again"* — steward, 2026-08-08

- **Live path:** `TaskDelegationBridge` — parent goes `BLOCKED`, child task
  assigned to the delegate, carried by `integrations/internal_comm/`.
- **Discovery:** signed UDP beacon `HEVOLVE_DISCO_V1` `:6780` (Ed25519 +
  guardrail-hash verified) then gossip `:6777`, already advertising
  tier/hardware/**idle-compute** — exactly the capacity signal pickup needs.
- **Recipes move** as `core.recipe_sync` envelopes through the ONE atomic
  writer; `_try_peer_recipe_reuse` asks peers **before** falling into CREATE.
- **Add no** partitioner, scheduler, work queue or coordinator. No sharding in
  any form.

**Failure recovery** shipped in `3ed0fc2b` (`reclaim_stale_delegations`) — before
it, a delegate that crashed or dropped off the LAN left its parent `BLOCKED`
permanently, because nothing in the bridge had a timeout, deadline, heartbeat or
reclaim. **It is not yet called on a schedule.** Wiring it into the daemon tick
is the next concrete step; until then it is a capability, not an active
recovery.

---

## 5. How it learns from experience — already coded

**The Recipe Pattern is the learning loop.** CREATE once (learn the task), REUSE
forever — ~90% cheaper because it skips re-decomposition and exploration, *not*
because it skips the LLM. A REUSE replay is LLM-guided and adapts to live
context; it is never a deterministic macro. A recipe banked on **one** node
flips the same goal to REUSE **fleet-wide** over A2A. `window.*` steps can be
banked too (`IPC_PROTOCOL.md` §8), so a replay restores the exact desktop layout.

Outcomes reach HevolveAI through the ONE bridge,
`world_model_bridge.py::WorldModelBridge` — mind the 50-batch flush buffer. All
ML lives in HevolveAI; **HARTOS is orchestration only.**

Federation's **metric** FedAvg deltas are real (equal-weight `log1p`, <5%
influence cap). The **parametric** leg is stubs — `apply_federation_update`
deliberately does not touch weights — and must never be described as live.

---

## 6. Non-negotiable boundaries

Every mutation passes the existing gate: guardrail + circuit breaker + per-agent
rate cap + immutable audit, with PREVIEW for destructive geometry
(`IPC_PROTOCOL.md` §6). `action_classifier.py` and `dlp_engine.py` are the
security surface — **wiring unverified, do not cite either as live until
checked.** `tool_allowlist.py` was listed here too and should not have been: it
lives in `integrations/agent_engine/`, not `security/`, and has **no production
caller** (see *Corrections already earned*). The copilot is audited **identically**
to an in-hive agent (`origin="mcp"`).

It never re-enables a cut AI sense, never treats a launcher exit code as
window-launch success, and never weakens the guardrail kernel, the master-key
boundary or the audit chain. Humans stay in control: outward-facing or
irreversible actions are approval-gated.

The guardrails are real and they bite: an earlier draft of this goal was
**rejected outright** by the self-interest regex
`\b(survive|persist|escape|resist\s+shutdown)\b` for containing the word
"survive" in an unrelated sentence. Worth knowing it screens wording, not
intent, so it false-positives on innocent phrasing.

---

## 7. Verify by artifact, never by flag

Three traps that manufacture false success:

1. **0-spark trap.** `budget_gate` prices local models at 0 spark, but the
   daemon only completes a goal when `spark_spent > 0`, so purely-local work
   auto-pauses after 5 noops. **Count banked recipes on disk**
   (`prompts/{prompt_id}_{flow_id}_recipe.json`) and ledger rows — never the
   completed flag.
2. **Completion is FSM-gated.** `COMPLETED` only via
   `STATUS_VERIFICATION_REQUESTED` → StatusVerifier verdict
   (`lifecycle_hooks.py`). Do not force states around it.
3. **A vanished delegate must surface as a reclaim,** not a silent hole.

---

## 8. Corrections already earned

Recorded so they are not re-learned:

- **`c1f17a81` is wrong.** It made `sha256(slug) % N` sharding the mechanism.
  The ledger already delegates; sharding would have been a second distributor.
- **`d116cf3f` is wrong.** It said `Task.delegate()` is the live path. It is
  not: `Task.delegate()` / `complete_delegation()` / `reclaim_delegation()` are
  **called by nothing in HARTOS**. The DELEGATED-state API is built but unwired;
  `TaskDelegationBridge`'s parent-BLOCKED + child model is live.
- **"Only `window.list` is implemented" is wrong** (my own bad grep). Eleven
  verbs are live.
- **`tool_allowlist.py` is an unwired orphan, and §6/§9 were wrong about it.**
  Verified 2026-08-08: it lives in `integrations/agent_engine/`, not
  `security/`; `filter_tools_for_model` has **no production caller** (4 files
  repo-wide — its own module, two test files, and a *comment* in
  `dispatch.py:674-676` asserting "create_recipe uses it", which
  `create_recipe.py` does not); it filters by **model tier**, not persona; and
  it matches a **flat** `t['name']` while the wire schema is nested
  `{'type':'function','function':{'name':…}}`. So #43 is not "wire up an
  existing helper" — it is a real decision between fixing that module and
  deleting it. Leaving both notions of "which tools may this agent see" is the
  parallel-path trap.
- **A2UI is proven live, end to end (2026-08-08).** `GET /api/a2ui/specs`
  returns 29 types; a copilot push through `POST /api/a2ui` was read back
  *rendered in the shell's own HTML*, not merely acknowledged by a `success`
  flag; `home_composer` is live-composing the home rows. What is NOT reachable
  is the LOCAL BRAIN driving it — `agent_ask` blocks 30s on a `/chat` measured
  at 49–76s (task #46).
- **Component-type registration is deliberately in-process.**
  `register_component_type`'s own docstring: *"Composer = HART agents only
  (this is in-process; no external/cloud path exists or is added)."* Do not add
  an HTTP route for it. An external copilot composes with the existing types and
  drives new UI through `POST /chat`, which is the ONE execution path anyway.
- **A native A2UI renderer is not a next-burn item.** `compositor/Cargo.toml`
  has no font, glyph, cosmic-text, cairo or skia — only pixman/GLES. Native
  A2UI needs a text stack, layout engine and widget code. The GSK-vulkan hang
  (task #12) is a GTK4-layer-shell defect a native surface would never hit, but
  that is a reason to go native *eventually*, not a reason to ship a
  half-built toolkit on stage.

---

## 9. CREATE-path status

| | |
|---|---|
| `ead16d3a` | n_ctx drift — OS shipped 4096 while the trim layer budgeted 12288. Overflow **78.7% → 2.1%** over 1,407 measured requests |
| `c0fbcb03` | the overflow guard never counted `body['tools']`, hiding a ~10,713-token schema |
| `696f618a` | empty-personas `IndexError` killed CREATE on small models |
| **OPEN — task #43** | that 67-tool schema is still **87% of a 12288 window**, leaving ~1,500 tokens for a system prompt measuring ~2,229. Prune per persona — but **NOT** "via the existing `tool_allowlist.py`", which is unwired and filters by model tier, not persona (see §8). **Last CREATE blocker.** |
| **CORROBORATED 2026-08-08** | 8 recipes banked, newest **10 June — 59 days**, against 1,183 ledger files. Dispatching, not banking. Exactly what #43 predicts, measured on disk rather than read off a completion flag |

---

## 10. Done means

The copilot takes a real objective on the user's own machine **with no network**,
composes the Liquid UI it needs through A2UI, arranges its own native windows,
completes the work, banks a recipe, and **replays that recipe cheaply** next
time. It then takes hive work with spare capacity, delegates through the ledger,
reclaims when a peer vanishes, and a peer's banked recipe flips a local goal to
REUSE. A `p2p_*` vertical completes a real transaction **with no commission and
no central matcher**, reachable from an ordinary chat channel. Earnings are real
numbers from `query_revenue_streams()`.

**All of it holds with central switched OFF.**

Zero regression, no parallel paths, no DRY violation, reinvent nothing.
