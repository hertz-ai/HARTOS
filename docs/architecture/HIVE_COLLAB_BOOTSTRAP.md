# Hive Collaboration Bootstrap — how a Claude Code session works INSIDE HARTOS

**This document is a PROMPT. It loads with every Claude Code session in this
repo (bound via CLAUDE.md) so the agent collaborates with HARTOS's native
machinery — the flywheel, the recipe pipeline, the HevolveAI feed — instead of
reinventing any of it. Every reference below is verified by
`tests/unit/test_hive_collab_bootstrap_prompt.py`; if that test is green, this
map matches the code.**

## Operating protocol -- drive the work THROUGH HARTOS

A Claude Code session in this repo is HARTOS's primary user and the
intelligence driving its flywheel. When the user requests work that HARTOS
can execute, do not only do it by hand: submit it through the CREATE pipeline
(`POST /chat` with `create_agent: true`) so a recipe is banked, spark is
charged, and the outcome feeds HevolveAI; replay it via REUSE thereafter.
Hand-edit only what the pipeline itself cannot do (building or fixing the
pipeline). Every task routed through `/chat` compounds the hive; every task
done around it is a lost lesson. On a box without the full stack (no
autogen or hevolveai wheel), state that plainly and fall back to direct
work; never simulate a pipeline run.

**PRIMARY OBJECTIVE: bootstrap every seeded goal, using the fleet's JOINT
resources.** The `SEED_BOOTSTRAP_GOALS` in
`integrations/agent_engine/goal_seeding.py` are the hive's curriculum (~70
goals); left to one idle flywheel they cost days of 1-wide grind with a
historically low completion rate, and left to N naive nodes they get CREATEd
N times over. A session executes the fleet bootstrap protocol below. The primitive is CODE:
`peer_reuse.try_peer_recipe_reuse` (called by the daemon before every CREATE)
discovers whether any admitted peer already has this goal's recipe and pulls
it. The session layers a SHARD STRATEGY on top so N nodes divide the CREATE
labour instead of racing it:

1. **Partition, no coordinator needed (strategy):** take the guardrail-
   admitted peer set from the gossip store (`peer_reuse.admitted_peers`),
   sort node_ids, find this node's index k of N. Prioritise the seeds where
   `sha256(bootstrap_slug) % N == k` -- your ~70/N shard (~3-4 on a 20-node
   fleet). Nothing enforces this; it is how a cooperative session picks WHAT
   to CREATE first so peers do not duplicate.
2. **CREATE your shard:** for each owned seed without a banked recipe,
   submit its prompt through the CREATE pipeline and verify the banked
   artifact (`prompts/{prompt_id}_{flow_id}_recipe.json` + ledger entry),
   never a completion flag (the 0-spark trap). Banking a recipe makes it
   exportable to peers (opt-in via the goal's export flag / `export_allowed`).
3. **Converge via agent-to-agent pull (the working primitive):** for seeds
   OUTSIDE your shard, do not CREATE -- the daemon's pre-CREATE
   `try_peer_recipe_reuse` (gated `HEVOLVE_A2A_PEER_REUSE`, default on)
   already discovers the owning peer's trained agent over the A2A surface,
   pulls the recipe, and banks it where the loader looks, so the next tick
   REUSEs from disk. Re-check unfilled seeds each pass; CREATE a peer's seed
   yourself only after a fleet pass leaves it orphaned (peer offline).

Endgame: every node holds all ~70 recipes after roughly ONE CREATE-cycle of
wall-clock instead of days -- 20 machines behaving as one bootstrap engine.
Progress on this list IS progress on the hive; report shard, banked count,
and pulled count explicitly.

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
3. **Know who starts the daemon.** The unified `AgentDaemon` starts from four
   places: `hart_intelligence_entry.py::main()` (the standalone launcher — this
   is what Docker and the OS images run), Nunba's `hartos_bootstrap.py`
   (step 8), the NixOS unit `nixos/modules/hart-agent.nix`, or
   **`scripts/run_flywheel_dev.py`** — use that script when you need the
   flywheel in a dev session. All four call the same idempotent
   `init_agent_engine(app)`; there is no second start path.

   The standalone launcher was added on 2026-07-21. `init_social` had
   delegated engine init to "the caller", Nunba's bootstrap picked it up, and
   this launcher never did — so Docker/OS nodes booted with no daemon
   supervisor and no goal bootstrap. Symptom: seeded goals sit `active`
   forever, nothing dispatches, and no engine line appears in the logs at all
   (the central node was found with 72 goals and zero dispatches).
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

A fleet of N installed HART OS nodes is today **N flywheels + a working
discovery/gossip/metrics/skill-packet/recipe-REUSE overlay**: one node's
banked recipe now flips the same seeded goal to REUSE fleet-wide via the
A2A surface, but goal execution itself is still per-node without shared
Redis. Collaborate accordingly; extend the hooks below, never invent
transports.

**Forms automatically on a LAN, zero config (REAL):**
- Discovery: signed UDP beacon (`HEVOLVE_DISCO_V1`, port 6780; Ed25519 +
  guardrail-hash verified before merge) — `integrations/social/peer_discovery.py`.
- Gossip: 60s rounds, fanout 3, over HTTP `:6777`
  (`/api/social/peers/exchange`); carries tier/hardware/idle-compute.
- Federated METRIC deltas (FedAvg, log1p weights) between peers.
- RALT skill packets -- a working knowledge multiplier:
  `distribute_skill_packet` announces via gossip/WAMP, the receiver
  HTTP-pulls the packet and `ingest_skill_packet` -> `import_skill`. Gated:
  CCT `skill_distribution` consent on both ends, 10 packets/hr, needs
  hevolveai loaded; triggered only when an LLM invokes the
  `distribute_learning_skill` tool.
- Cross-node recipe REUSE over A2A (peer-to-peer, no central, no Redis):
  the daemon's CREATE-vs-REUSE classifier asks admitted peers BEFORE
  falling into CREATE (`agent_daemon._try_peer_recipe_reuse` ->
  `integrations/google_a2a/peer_reuse.py::try_peer_recipe_reuse`). Nodes
  advertise exportable recipes at `GET /a2a/agents` carrying the goal
  linkage (`bootstrap_slug`/`goal_type`/title), which is how discovery
  matches across the md5-local prompt_id boundary (agent_ids never match
  between nodes); bytes ship via `GET /a2a/<agent_id>/recipe` as the
  canonical `core.recipe_sync` envelope and land through the ONE atomic
  writer (`write_envelope_files`), banked verbatim under the peer's
  naming PLUS aliased under the local goal's prompt_id so
  `_flow_recipe_exists` flips the goal to REUSE. Remote invoke over the
  existing `/a2a/<id>/jsonrpc` contract is the fallback when bytes are
  not exportable. Bounded: ~10s peer leg per tick + 600s per-goal
  cooldown; every failure falls through to CREATE exactly as before.
  Gated by `HEVOLVE_A2A_PEER_REUSE` (default on); privacy fail-closed:
  only goal-linked recipes or `broadcast_agent` opt-ins are listed or
  served, never personal agents. Verified by
  `tests/unit/test_a2a_peer_reuse.py` (two-node in-process harness).
- Recipe CAPABILITY MESH (proactive layer over the reactive pull above,
  the semantic-subscription architecture): banking an exportable recipe
  PUBLISHES a capability advert -- `peer_reuse.announce_recipe_available`
  from the `_save_flow_recipe` hook gossip-broadcasts
  `{type:'recipe_available', capability:{semantic_class, slug, agent_id,
  ...}, source_api_url, checksum}` on the canonical topic
  `RECIPE_AVAILABLE_TOPIC` (`core/constants.py`), mirroring the
  skill-packet ANNOUNCE-then-PULL pattern exactly. Admitted peers cache
  the advert (`on_recipe_available_advert`, TTL `HEVOLVE_A2A_ADVERT_TTL_S`
  900s), so the union of caches is a live decentralized index of
  who-can-do-what -- no central registry. The daemon consults that cache
  BEFORE the O(peers) discovery sweep (`consume_advert` -> direct
  `pull_recipe`), falling through to the reactive path on a miss. Rides
  GOSSIP HTTP so it works central-off and Nunba-off TODAY; the
  `TOPIC_MAP['recipe.available']` alias means the same publish rides WAMP
  prefix-subscription (`com.hertzai.hevolve.recipe.*`) the day a
  node-local router ships -- no code change. The result is capability routing
  plus instant curriculum spread; per-action compositional routing and
  reputation-ranked specialization (5%-cap-guarded) are the next
  increments. Verified by `tests/unit/test_recipe_capability_mesh.py`.

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
- Pooled inference: the mesh transport exists (`:6796`, WireGuard,
  `/mesh/infer`) but the flywheel's LLM path is hardwired to the local
  llama-server; only VIDEO_GEN offloads today.

**The conversion hooks (canonical rails only — gossip HTTP on :6777, mesh
:6796, ChannelDispatcher; each turns fleet size into learning speed):**
1. Config-only, works today: point all nodes at one shared `REDIS_HOST` ->
   fleet-wide claim/execute of dispatched goals.
2. Key `prompt_id` by `bootstrap_slug` (not md5 of the node-local goal id) in
   `dispatch.py` — dedups CREATE fleet-wide AND repairs the recipe-sync key.
3. SHIPPED (2026-07-17): pull recipes in the daemon BEFORE its
   CREATE-vs-REUSE split, landed as `_try_peer_recipe_reuse` in
   `agent_daemon.py`; flips peers to REUSE (see the REAL list above).
4. SHIPPED (2026-07-17): ship recipe BYTES peer-to-peer by mirroring the
   skill-packet export/pull, landed as `GET /a2a/agents` +
   `GET /a2a/<agent_id>/recipe` + `peer_reuse.pull_recipe`, banking into
   `prompts/{prompt_id}_{flow_id}_recipe.json` where the loader already
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
