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

## Standing collaboration rules (unchanged, binding)

Master-key AI-exclusion zone; main branch only; no parallel paths (route to
canonical homes — `/chat`, `dispatch_goal`, `WorldModelBridge`, one writer per
persisted value); no grep tests; every caught error logs; consult
`docs/design/HOME_DESKTOP_DESIGN_CHECKLIST.md` before desktop UI changes.
