# Task Ledger Grouping Fix — Hand-off Plan

**Repo**: HARTOS
**Status**: in progress — schema additions (§4a) landed 2026-05-27
**Author**: Claude Code session 2026-05-26
**Audience**: any session/dev picking this up cold

This document is self-contained. You do not need prior context.

---

## 0. CANONICAL IDENTIFIER TYPES — the domain model, in one screen

| Identifier | Logical type | On-disk / wire type | Why the distinction matters |
|---|---|---|---|
| `prompt_id` | **integer** (DB primary key) for human-created agents; **UUID string** for autonomous agents (coding agent, hive agents). | When stamped on `Task.recipe_prompt_id` and serialised in the ledger JSON, normalized to **string** to cover both cases (`"42"` or `"177bdda1-c710-..."`). Dashboard can `int()` it when numeric for sort order. | One field, two real domains.  Typing it `int` would break autonomous agents; typing it `str` everywhere matches the existing sibling `Task.owner_prompt_id: Optional[str]` and the `agent_id = str(prompt_id)` coercion already done in `create_ledger_from_actions`. |
| `flow_id` | **integer**, monotonic from 0 per `prompt_id`. | Stored as int in `Task.recipe_flow_id: Optional[int]`. Sourced at runtime by `get_flow_number(user_id, prompt_id)` in reuse mode, `get_current_flow(user_prompt)` in create mode. | Recipe filename `{prompt_id}_{flow_id}_recipe.json` keeps the two in lockstep.  Multi-flow ledgers are not yet in production but the schema reserves the field so the day a `{prompt}_1_recipe.json` ships, no code path has to disambiguate by filename parsing. |
| `action_id` | **integer**, restarts from 1 every flow. | Stored as int in `Task.recipe_action_id: Optional[int]`. Also encoded implicitly in `task_id = f"action_{action_id}"` for the recipe-action task type; legacy ledgers parse this back when the explicit field is None. | Because action_id is flow-scoped (not prompt-scoped), the `(flow_id, action_id)` tuple is the unique recipe coordinate within a session. |
| `session_id` (execution instance id) | **String**, freely-shaped by the caller.  Real values in production: `"{user_id}_{prompt_id}"` (canonical), `"goal_abc"` (admin-created goals), full UUID (autonomous agents). | Stored verbatim in the ledger filename suffix `ledger_{agent_id}_{session_id}.json` and in `SmartLedger.session_id`. **No type coercion** — preserves whatever the caller supplied. | A **new session_id is minted for every execution instance** of a `prompt_id/agent_id`:<br>• **CREATE mode** — the recipe's *first* execution: `create_recipe.py:create_action_with_ledger` mints the session_id.<br>• **REUSE mode** — every subsequent execution of the same recipe: `reuse_recipe.py:963 → create_ledger_from_actions` mints a fresh session_id.<br>Same agent, many sessions over time → the dashboard hierarchy reads `prompt_id → [session_1, session_2, …]`. |
| `agent_id` | Mirrors `prompt_id`'s domain (int for humans, UUID for autonomous). | Always stored as **string** in `SmartLedger.agent_id` (`create_ledger_from_actions` does `agent_id = str(prompt_id)` explicitly). | The "agent_id == prompt_id" convention is the identity the dashboard groups by; keeping them physically the same string avoids one more denormalised mapping to maintain. |

**Practical implication for any code touching this schema**: when you read a `prompt_id` from a ledger or stamp one onto a task, expect a string.  When you stamp it from the CREATE/REUSE pipeline, you already have an int from the DB → coerce with `str(prompt_id)` (which is what `create_ledger_from_actions` does today; this plan preserves that).

---

## 1. WHAT — the problem in one screen

The dashboard that lists agent tasks renders **4427 tasks across 401 sessions** as flat `prompt=XXXXXX → 1 task` rows. The user-stated correct hierarchy is:

```
prompt_id   (= agent identity)
└── session_id     (one execution instance of that agent)
    └── flow_id    (a flow inside the session — comes from the recipe)
        └── action_N (each step of the flow, with state from ledger lifecycle hooks)
```

Today's dashboard collapses this to one row per session and shows one task — because the ledger schema doesn't carry `flow_id` or `action_id` as explicit fields. The information is stored implicitly (in filenames and in the `task_id` string), so the dashboard can't reconstruct the hierarchy without ad-hoc string parsing.

User intent (quoted): *"they shd be all actions for a flow grouped under one execution instance and each time an agent (agents are identified by prompt id) so the structure is promptid -> <list of executions by session id> -> for each session flowid list and for each flow all actions with execution state from ledger/lifecycle hooks"*

---

## 2. WHY — why it's actually broken vs why it looks broken

### 2a. The data exists, just not where the dashboard reads it

| Hierarchy layer | Where it lives today |
|---|---|
| `prompt_id` (agent) | Recipe filename prefix `prompts/{prompt_id}_..._recipe.json`; also matches `agent_id` in ledger filename `agent_data/ledger_{agent_id}_{session_id}.json` (`agent_id == prompt_id` by convention). |
| `session_id` (execution) | Ledger filename suffix. |
| `flow_id` | **Only** in the recipe filename: `prompts/{prompt_id}_{flow_idx}_recipe.json` — never copied into the ledger. |
| `action_id` | In the recipe (`recipe.actions[].action_id`) **and implicitly** in the ledger task_id (`task_id = f"action_{action_id}"`, e.g. `reuse_recipe.py:3482`). Never an explicit field. |
| Lifecycle state per action | Lives on `Task.state_history` in the ledger (works fine). |

### 2b. Why the dashboard shows 1 task / prompt

Three compounding causes — only one of them is a grouping bug:

1. **Schema gap (grouping bug, this plan fixes it)**: ledger tasks carry no `recipe_prompt_id` / `recipe_flow_id` / `recipe_action_id`, so the dashboard has nothing to group hierarchically by and falls back to one row per ledger file.
2. **One ledger file per `(agent_id, session_id)` pair**: 431 files in `agent_data/ledger_*.json` — naturally produces 431 buckets in any flat reader.
3. **Planner-quality symptom (out of scope)**: many recipes that DO have multiple actions only produce a single ledger task because the planner short-circuits or fails on the first action. That's why each bucket also shows just "1 task". This is a *planner-quality* bug, not a *grouping* bug. Fixing grouping alone makes the multi-action sessions render correctly; single-action sessions still look like single-action sessions because they truly are.

This plan addresses **(1)** and unblocks the dashboard, leaving **(3)** for separate planner work.

### 2c. Why a tuple `(flow_id, action_id)` instead of just `action_id`

Recipes restart `action_id` from 1 every flow:

- `prompts/27130929014_0_recipe.json` → actions 1, 2, 3, 4, 5
- `prompts/9901_0_recipe.json` → actions 1, 2, ...
- `prompts/fw4_1_0_recipe.json` → actions 1, 2, ...

Today every recipe on disk is `*_0_recipe.json` (only one flow per prompt). The day someone produces `{prompt}_1_recipe.json`, an `action_id=2` ledger task would be ambiguous between the two flows. Adding `recipe_flow_id` removes that ambiguity preemptively at no cost (one extra optional integer per task).

---

## 3. WHERE — the exact files and line anchors

All three coordinated edits live in **one file**: `agent-ledger-opensource/agent_ledger/core.py`. The stamping caller in `reuse_recipe.py` needs a small parameter passthrough. The dashboard reader is in an as-yet-unlocated file (open question; see §6).

### 3a. `agent-ledger-opensource/agent_ledger/core.py`

| Anchor | Approx line | What's there today | What to add |
|---|---|---|---|
| `Task.__init__` ownership tracking block | ~L280-284 (`owner_node_id`, `owner_user_id`, `owner_prompt_id`, `owned_at`, `ownership_history`) | sets ownership fields | append three new attrs: `self.recipe_prompt_id: Optional[str] = None`, `self.recipe_flow_id: Optional[int] = None`, `self.recipe_action_id: Optional[int] = None` |
| `Task.to_dict` ownership block | ~L368-373 (`"owner_*"` keys) | serializes ownership fields | append three keys: `"recipe_prompt_id"`, `"recipe_flow_id"`, `"recipe_action_id"` |
| `Task.from_dict` ownership block | ~L455-460 (`task.owner_* = data.get(...)`) | restores ownership fields | append three `task.recipe_* = data.get("recipe_*")` lines |
| `def create_ledger_from_actions(...)` | L3507 (signature) | currently takes `(agent_id, session_id, actions, backend)` | extend signature: accept optional `flow_id: int = 0`. Inside the loop where each Task is created from a `recipe action`, stamp: `task.recipe_prompt_id = session_id` (or new `prompt_id` arg if different), `task.recipe_flow_id = flow_id`, `task.recipe_action_id = action.get('action_id')` |

### 3b. `reuse_recipe.py`

| Anchor | Approx line | What's there today | What to do |
|---|---|---|---|
| Call site to `create_ledger_from_actions` | L963 | `ledger = create_ledger_from_actions(user_id, prompt_id, role_actions, backend=backend)` | pass `flow_id=0` explicitly (single-flow invariant today). When multi-flow lands, derive from the recipe filename parser. |
| `ledger.add_dynamic_task(task_desc, context)` | L3552, L3570 | adds tasks discovered mid-flight | also stamp `recipe_prompt_id=prompt_id`, `recipe_flow_id=<current flow_id>` on the returned Task object. Leave `recipe_action_id=None` (dynamic, not from recipe). |

### 3c. `create_recipe.py` (if it also calls `create_ledger_from_actions` or constructs tasks)

Same treatment as reuse_recipe.py at every callsite. The grep `grep -n "create_ledger_from_actions\|add_dynamic_task" create_recipe.py` will enumerate them. (File is 5306 lines; spot-checking suggests at least one such site.)

### 3d. The dashboard reader

Location **not yet found**. One grep before edit:
```
grep -rln "ledger_.*\.json\|owner_prompt_id\|task_order" desktop/ routes/ hart_backend_src/ hart_intelligence_entry.py
```
Most likely a JS/TS bundle under `desktop/` or a Flask route in `hart_intelligence_entry.py`. Whoever owns the UI codepath does this step.

---

## 4. HOW — the surgical change pattern

### 4a. Schema additions in core.py (zero-risk, additive)

```python
# In Task.__init__ — append AFTER the existing ownership block
self.recipe_prompt_id: Optional[str] = None   # which agent's recipe this task belongs to
self.recipe_flow_id: Optional[int] = None     # which flow within that recipe
self.recipe_action_id: Optional[int] = None   # which action_id in recipe.actions[]
```

```python
# In Task.to_dict — append BEFORE the closing brace
"recipe_prompt_id": self.recipe_prompt_id,
"recipe_flow_id": self.recipe_flow_id,
"recipe_action_id": self.recipe_action_id,
```

```python
# In Task.from_dict — append AFTER the existing ownership restoration
task.recipe_prompt_id = data.get("recipe_prompt_id")
task.recipe_flow_id   = data.get("recipe_flow_id")
task.recipe_action_id = data.get("recipe_action_id")
```

### 4b. Stamping in `create_ledger_from_actions`

```python
def create_ledger_from_actions(
    agent_id, session_id, actions, backend=None,
    flow_id: int = 0,                  # NEW, optional, defaults to 0 (single-flow invariant)
    recipe_prompt_id: Optional[str] = None,  # NEW, defaults to agent_id if None
):
    ...
    prompt = recipe_prompt_id or str(agent_id)
    for action in actions:
        task = Task(
            task_id=f"action_{action.get('action_id', i+1)}",
            description=action.get('action', ''),
            ...
        )
        task.recipe_prompt_id = prompt
        task.recipe_flow_id   = flow_id
        task.recipe_action_id = action.get('action_id')
        ledger.tasks[task.task_id] = task
    ...
```

### 4c. Dashboard reader — hierarchical grouping with legacy fallback

```python
def list_grouped_by_recipe_hierarchy(ledger_dir: Path) -> Dict[str, Dict[str, Dict[int, List[Task]]]]:
    """
    Returns: prompt_id -> session_id -> flow_id -> [Task]
    Tolerates legacy ledgers without recipe_* fields (falls back to filename
    parsing for prompt+session, flow_id=0, and task_id string-split for action_id).
    """
    groups: Dict[str, Dict[str, Dict[int, List[Task]]]] = {}
    for f in ledger_dir.glob('ledger_*_*.json'):
        # filename pattern: ledger_{agent_id}_{session_id}.json
        m = re.match(r'ledger_(.+?)_(.+?)\.json$', f.name)
        if not m:
            continue
        fname_agent, fname_session = m.group(1), m.group(2)
        data = json.loads(f.read_text())
        for tid, td in data.get('tasks', {}).items():
            prompt = td.get('recipe_prompt_id') or fname_agent
            flow   = td.get('recipe_flow_id', 0) or 0
            action = td.get('recipe_action_id')
            if action is None:
                # legacy fallback: parse from task_id="action_N"
                try:
                    action = int(tid.rsplit('_', 1)[-1])
                except (ValueError, IndexError):
                    action = -1
            groups.setdefault(prompt, {}) \
                  .setdefault(fname_session, {}) \
                  .setdefault(flow, []).append((action, td))
    # sort actions inside each flow
    for p in groups.values():
        for s in p.values():
            for fl, actions in s.items():
                s[fl] = sorted(actions, key=lambda x: x[0])
    return groups
```

This goes in `agent-ledger-opensource/agent_ledger/core.py` as a helper on `SmartLedger` (so every consumer gets the same grouping), then the dashboard calls it.

---

## 5. WHEN — execution order

1. **Schema additions** (§4a) — three additive blocks, can ship alone. New ledger files will start carrying the fields immediately; old ones load as None.
2. **Stamping in `create_ledger_from_actions`** (§4b) — write fields at task creation. After this, all NEW ledgers have the hierarchy.
3. **Stamping at `add_dynamic_task` sites** in reuse_recipe.py:3552/3570 and any equivalents in create_recipe.py.
4. **Hierarchical reader helper** (§4c) on SmartLedger — works on both new and legacy ledgers thanks to fallbacks.
5. **Dashboard wired to call the helper** — only step that touches the UI codepath. Out-of-scope until the renderer file is located.

Steps 1+2+3 are independent of step 4. Steps 1-4 must all land before step 5 is useful, but each is independently testable.

---

## 6. OPEN QUESTIONS — must answer before step 5

- **Q1**: Where is the dashboard renderer that produces the `prompt=XXXX → 1 task` rows the user screenshotted? The file isn't `routes/dashboard.py`-style and isn't `hart_intelligence_entry.py`. One grep should locate it:
  ```
  grep -rln "prompt=\|task_order\|ledger_.*\.json" desktop/ landing-page/ templates/ hart_backend_src/
  ```
- **Q2**: Does `create_recipe.py` also build ledger tasks directly (not via `create_ledger_from_actions`)? If yes, those sites need the same stamping. One grep:
  ```
  grep -n "Task(\|add_task\|new_task" create_recipe.py | head -20
  ```
- **Q3**: When there's a single recipe per prompt (today's invariant), is `flow_id=0` always correct, or do some flows carry non-zero suffixes? Check by listing recipe filenames:
  ```
  ls prompts/*_*_recipe.json | head -20
  # confirm: do any non-zero flow_idx exist?
  ```

---

## 7. RISK & REVERSIBILITY

| Step | Risk | Reversible? | Verification |
|---|---|---|---|
| Schema additions (1a/1b/1c) | Zero — purely additive; old ledgers round-trip because `data.get` returns None | Yes — revert diff | `python -m pytest agent-ledger-opensource/tests` |
| Stamping in `create_ledger_from_actions` (2) | Low — single function; bug in stamping would only affect new tasks | Yes — revert diff | create a new ledger via test, assert three fields populated |
| Stamping in dynamic-task sites (3) | Low — but two sites, must edit both consistently | Yes | as above |
| Hierarchical reader (4) | Low — read-only; legacy fallback well-defined | Yes | unit test with 5 legacy ledgers + 5 new ledgers |
| Dashboard call into reader (5) | Medium — UI change, possible visual regressions | Yes — revert UI patch | manual: open dashboard, see hierarchy expand correctly |

---

## 8. OUT OF SCOPE (deliberately not in this plan)

- **Planner producing single-action sessions** (the *deeper* reason most prompts show "1 task" even though their recipes have 5+ actions). This is a planner-quality problem; fix it separately. Symptoms: a recipe `prompts/{X}_0_recipe.json` shows 5 actions all `"status": "done"`, but `agent_data/ledger_*.json` for that session has only one task. Likely cause: an early `action_1` failure aborts before subsequent actions get materialized. Root-cause work lives in `reuse_recipe.py`'s action-loop and `agent_daemon.py`'s dispatch logic; out of scope here.
- **Migrating the 431 legacy ledgers** to backfill `recipe_*` fields. The reader's legacy fallback (§4c) handles them in place. Backfill is one-time `for ledger_X.json in 431 files: parse + write`, but not required for the dashboard to work.
- **Multi-flow per prompt** beyond the schema reservation. Schema accepts non-zero `flow_id`; no current code path produces it.

---

## 9. WHO — the smallest possible owner per step

- Steps 1-3 (schema + stamping): one dev, one PR, in `agent-ledger-opensource/` + `reuse_recipe.py` (+ optionally `create_recipe.py`).
- Step 4 (reader helper): same dev, can land in the same PR.
- Step 5 (dashboard call): UI/frontend owner, separate PR once Q1 is answered.

---

## 10. APPENDIX — file:line evidence cited

| Evidence | Location |
|---|---|
| `Task.__init__` body (struct of `owner_*` block immediately before where new fields go) | `agent-ledger-opensource/agent_ledger/core.py:200-311` |
| `Task.to_dict` (one-key-per-line dict serialization) | `agent-ledger-opensource/agent_ledger/core.py:313-396` |
| `Task.from_dict` (`task.X = data.get("X")` pattern) | `agent-ledger-opensource/agent_ledger/core.py:398-479` |
| `task.owner_prompt_id` (sibling of new field) | `agent-ledger-opensource/agent_ledger/core.py:282` |
| `def create_ledger_from_actions(...)` definition | `agent-ledger-opensource/agent_ledger/core.py:3507` |
| `task_id = f"action_{action_id}"` convention | `reuse_recipe.py:3482`, also `:2483, :3114` |
| `create_ledger_from_actions(...)` call site (reuse) | `reuse_recipe.py:963` |
| `ledger.add_dynamic_task(...)` sites | `reuse_recipe.py:3552, 3570` |
| Recipe schema (actions[].action_id) sample | `prompts/27130929014_0_recipe.json` |
| Recipe→experience telemetry layer (proves data is available at runtime) | `recipe_experience.py:34-35, 123-254` |
| 431 ledger files (the symptom) | `agent_data/ledger_*.json` (count via `ls | wc -l`) |
| HiveMind tasks file (unrelated schema, distinct from per-session ledgers) | `agent_data/hive_tasks.json` |

---

## 11. PROGRESS LOG

| Date | Step | Status |
|---|---|---|
| 2026-05-27 | §4a — Task schema additions (init / to_dict / from_dict) | **landed** `agent-ledger-opensource/agent_ledger/core.py` |
| 2026-05-27 | §4b — `create_ledger_from_actions` `flow_id` + `recipe_prompt_id` kwargs | **landed** same file |
| 2026-05-27 | §4b — `add_dynamic_task` central stamping (inherits flow_id from siblings) | **landed** same file |
| 2026-05-27 | §4c — `SmartLedger.list_grouped_by_recipe_hierarchy` classmethod + legacy fallback | **landed** same file |
| 2026-05-27 | Call-site fix: `reuse_recipe.py:963` passes `flow_id=role_number` (from `get_flow_number`) — NOT hardcoded 0 as the plan originally suggested. `role_number` IS the real flow id, sourced from the recipe filename `{prompt_id}_{role_number}_recipe.json`. | **landed** `reuse_recipe.py` |
| 2026-05-27 | Call-site fix: `create_recipe.py:create_action_with_ledger` accepts `flow_id` kwarg, defaulting to `get_current_flow(user_prompt)`. Covers all 7+ existing callers without per-site edits. | **landed** `create_recipe.py` |
| 2026-05-27 | 11 regression tests in `agent-ledger-opensource/tests/test_core.py` (`TestTaskRecipeHierarchy`, `TestRecipeHierarchyStamping`). 34/34 pass. | **landed** |
| — | §5 — dashboard wiring | out of scope until Q1 answered |

### Plan-vs-implementation divergence (intentional)

- Plan §4b said `flow_id` default 0 with a TODO "when multi-flow lands, derive from the recipe filename parser."  In HARTOS today, the real flow id is already available at every call site: `get_flow_number(user_id, prompt_id)` in reuse, `get_current_flow(user_prompt)` in create.  Threading those through is one-line and removes the TODO immediately — done in this PR instead of left as future work.
- Plan §3b said to stamp `recipe_flow_id` on the Task object **at every call site** of `add_dynamic_task`.  Central stamping inside `add_dynamic_task` (inheriting from sibling tasks in the same ledger) is strictly cleaner: every existing and future caller gets correct stamping without touching code.  The "one ledger = one flow" invariant is enforced upstream by `reuse_recipe.py:944`'s persona filter and `create_recipe.py:5006`'s per-flow ledger reset, so inheritance is safe.
- Plan §4c grouping helper used the regex `r'ledger_(.+?)_(.+?)\.json$'` (non-greedy on both groups), which mis-splits `ledger_10202_42_abc.json` as `agent="10202", session="42_abc"`.  Implementation uses `r'^ledger_(.+)_([^_]+)\.json$'` — greedy on agent, atomic on session — so the session is always the right-most underscore-delimited token.
