# Hive Collaboration Bootstrap — how a Claude Code session works INSIDE HARTOS

**This document is a PROMPT. It loads with every Claude Code session in this
repo (bound via CLAUDE.md) so the agent collaborates with HARTOS's native
machinery — the flywheel, the recipe pipeline, the HevolveAI feed — instead of
reinventing any of it. Every reference below is verified by
`tests/unit/test_hive_collab_bootstrap_prompt.py`; if that test is green, this
map matches the code.**

## When the user asks for work, route it natively

1. **The ONE work entry point is `POST /chat`** on the :6777 app
   (`hart_intelligence_entry.py`) with `{user_id, prompt_id, prompt,
   create_agent}`. CREATE-vs-REUSE is decided by recipe existence — a banked
   recipe replays (~90% cheaper, still LLM-guided); no recipe means CREATE
   learns one. Never build a second task-execution path: if it is agentic
   work, it goes through `/chat` so it banks recipes and charges spark.
2. **Autonomous/background work goes through the flywheel, not ad-hoc
   threads.** `integrations/agent_engine/dispatch.py::dispatch_goal` posts the
   goal into the same `/chat` pipeline with `create_agent=True,
   autonomous=True`. Goals live in `goal_manager.py` (12 verticals);
   bootstrap goals seed from `goal_seeding.py::SEED_BOOTSTRAP_GOALS`.
3. **Know who starts the daemon.** `python hart_intelligence_entry.py` starts
   ONLY the coding daemon. The unified `AgentDaemon` starts from exactly three
   places: Nunba's `hartos_bootstrap.py` (step 8), the NixOS unit
   `nixos/modules/hart-agent.nix`, or **`scripts/run_flywheel_dev.py`** — use
   that script when you need the flywheel in a dev session.
4. **Respect the yield contract.** The daemon yields to the foreground user
   (`dispatch.py::should_yield_to_user`: live request, user active in the
   last 600s, model/governor throttle). A Claude Code session IS foreground —
   your work preempts the flywheel by design; do not fight it, and do not
   disable the guardrail gate (fail-closed) or the `HiveCircuitBreaker`.

## How outcomes reach HevolveAI (the hive-native intelligence)

- The ONE bridge is `integrations/agent_engine/world_model_bridge.py::
  WorldModelBridge`. Dispatched goals auto-feed via `record_interaction`
  (called from `dispatch.py` when a goal returns; also per chat turn from the
  speculative dispatcher). Skill packets flow both ways
  (`distribute_skill_packet` / `ingest_skill_packet` — imports mutate local
  agent weights).
- **Feed pacing trap:** experiences buffer until `HEVOLVE_WM_FLUSH_BATCH`
  (default 50) with NO timer flush — on a 1-wide desktop that is hours-to-days
  of latency. On a node where feed latency matters, set
  `HEVOLVE_WM_FLUSH_BATCH=1`.
- **Stub trap:** without the hevolveai wheel the entire feed silently no-ops
  (stub mode). Check availability before claiming outcomes are being fed.
- **Known gap (do not "fix" by a parallel path):** banked recipes and FSM/
  ledger completions have NO HevolveAI call site — only the goal-level
  prompt/response crosses. The sanctioned hook point for closing this is the
  recipe-save path in `create_recipe.py` (where the spark charge fires),
  feeding the SAME `WorldModelBridge` — never a second bridge.

## Bootstrap timing (idle box, defaults) — what "quickly" means

| Milestone | Default | Tuned* |
|---|---|---|
| Daemon thread live | seconds | seconds |
| First dispatch | ~T+5.5 min (300s boot grace + 30s tick) | ~T+30s |
| First action executing | ~T+7 min | ~T+1-2 min (REUSE) |
| First recipe banked | ~T+20-40 min (CREATE, 1-wide) | ~T+3-10 min |
| First outcome inside HevolveAI | hours+ (50-batch buffer) | ~T+5-12 min |

*Tuned: `HEVOLVE_DAEMON_BOOT_DELAY=0`, `HEVOLVE_WM_FLUSH_BATCH=1`, recipes
banked, user idle, many-core box. Other pacing knobs:
`HEVOLVE_AGENT_POLL_INTERVAL` (30s tick), `HEVOLVE_AGENT_MAX_CONCURRENT`
(capped by cores: `(cores-6)//4` -> 1 pipeline on an 8-core desktop),
`HEVOLVE_LOCAL_LLM_MAX_CONCURRENT` (default 1).

## Live traps a collaborating agent must know

1. **Local-model 0-spark completion trap:** budget_gate prices local models at
   0 spark, but the daemon only marks a goal completed when `spark_spent > 0`
   — purely-local completed work auto-pauses after 5 noops. If your work runs
   local-only, verify completion by ledger/recipe artifacts, not by the
   daemon's completed flag.
2. **Completion is FSM-gated:** COMPLETED only via
   STATUS_VERIFICATION_REQUESTED -> StatusVerifier verdict
   (`lifecycle_hooks.py`). Do not force states around it.
3. **Federation's parametric-learning leg is aspirational** (gradient
   protocol stubs; `apply_federation_update` deliberately does not touch
   weights). Do not describe it as live.

## The 20-node cluster: what is REAL today (code-verified 2026-07-17)

A fleet of N installed HART OS nodes is today **N solo flywheels + a working
discovery/gossip/metrics/skill-packet overlay** — not yet a compounding hive.
Collaborate accordingly; extend the hooks below, never invent transports.

**Forms automatically on a LAN, zero config (REAL):**
- Discovery: signed UDP beacon (`HEVOLVE_DISCO_V1`, port 6780; Ed25519 +
  guardrail-hash verified before merge) — `integrations/social/peer_discovery.py`.
- Gossip: 60s rounds, fanout 3, over HTTP `:6777`
  (`/api/social/peers/exchange`); carries tier/hardware/idle-compute.
- Federated METRIC deltas (FedAvg, log1p weights) between peers.
- RALT skill packets — the cluster's ONE working knowledge multiplier:
  `distribute_skill_packet` announces via gossip/WAMP, the receiver
  HTTP-pulls the packet and `ingest_skill_packet` -> `import_skill`. Gated:
  CCT `skill_distribution` consent on both ends, 10 packets/hr, needs
  hevolveai loaded; triggered only when an LLM invokes the
  `distribute_learning_skill` tool.

**Does NOT work today (do not assume it):**
- PeerLink is DIAL-ONLY: no `/peer_link` listener and no
  `/api/peer-link/message` route exists anywhere, so the encrypted-link layer
  never connects (budgets 10/50/200 are therefore moot).
- Cross-node WAMP: the router lives in Nunba (`nunba.enable=false` shipped).
- Cross-node goal execution: real ONLY with a shared `REDIS_HOST`
  (`integrations/distributed_agent/coordinator_backends.py` Redis backend +
  `integrations/distributed_agent/worker_loop.py` claim/execute).
  Default backend is process-local -> 20 isolated dispatchers, the same seed
  goals CREATEd 20x under 20 different md5-local prompt_ids.
- Recipe REUSE across nodes: NEVER as coded. Recipe bytes do not travel
  (the federation recipe channel is metadata-only with no subscriber); the
  daemon never pulls from `core/recipe_sync.py`; push/pull disagree on
  user_id; md5-local keys make node B's query a structural 404.
- Pooled inference: the mesh transport exists (`:6796`, WireGuard,
  `/mesh/infer`) but the flywheel's LLM path is hardwired to the local
  llama-server; only VIDEO_GEN offloads today.

**The conversion hooks (canonical rails only — gossip HTTP on :6777, mesh
:6796, ChannelDispatcher; each turns fleet size into learning speed):**
1. Config-only, works today: point all nodes at one shared `REDIS_HOST` ->
   fleet-wide claim/execute of dispatched goals.
2. Key `prompt_id` by `bootstrap_slug` (not md5 of the node-local goal id) in
   `dispatch.py` — dedups CREATE fleet-wide AND repairs the recipe-sync key.
3. Pull recipes in the daemon BEFORE its CREATE-vs-REUSE split (and push with
   a matching user_id key) — flips peers to REUSE.
4. Ship recipe BYTES peer-to-peer by mirroring the skill-packet export/pull
   into `prompts/{prompt_id}_{flow_id}_recipe.json` where the loader already
   looks (`agent_baseline_service.py`).
5. Add the inbound PeerLink listener on `:6777` feeding the existing
   `ChannelDispatcher` (`core/peer_link/channels.py`) per the fallback
   contract in `link_manager.py`.
The feed buffer is PER-NODE (50-batch, no timer): 20 nodes fill 20 separate
buffers into 20 HevolveAI instances — fleet size does not shorten
time-to-first-fed-outcome until the hooks above land.

## Standing collaboration rules (unchanged, binding)

Master-key AI-exclusion zone; main branch only; no parallel paths (route to
canonical homes — `/chat`, `dispatch_goal`, `WorldModelBridge`, one writer per
persisted value); no grep tests; every caught error logs; consult
`docs/design/HOME_DESKTOP_DESIGN_CHECKLIST.md` before desktop UI changes.
