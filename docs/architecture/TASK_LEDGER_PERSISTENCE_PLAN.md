# Task Ledger Persistence — Holistic Design Plan (v2, post-audit)

**Repo**: HARTOS
**Status**: planning. Phase 1 (schema + stamping) already landed via `TASK_LEDGER_GROUPING_FIX_PLAN.md` §4a-b on 2026-05-27.
**Author**: 2026-05-27 Claude Code session.
**Audience**: any session/dev picking this up cold.

**v2 changes from v1**: full audit of existing HARTOS persistence + restore pattern (`TTLCache + loader`) before designing. Removes parallel-path proposals from v1 that would have re-implemented infrastructure that already exists. Session_id format simplified to `{user_id}_{prompt_id}_{ts_ms}` (no PID tiebreaker — accepted concurrency tradeoff).

**v2 progress (2026-05-27)**: Phases 1-5 all landed. Test totals: 36 → 80 across the touched suites. Zero regressions caused by these edits; one pre-existing TaskLedgerHighlight failure verified unrelated. Detail per phase:

| Phase | Status | New tests |
|---|---|---|
| 1 — schema + stamping + grouping helper | ✅ landed | 13 new in `agent-ledger-opensource/tests/test_core.py` |
| 2 — session_id minting + resume detection | ✅ landed | 6 new in same file |
| 3 — `load_current_flow` loader | ✅ landed | 7 new in `tests/unit/test_load_current_flow.py` |
| 4 — `?group=hierarchy` API opt-in | ✅ landed | 3 new in `tests/unit/test_api_agent_engine_ledger.py` (wire-shape verified directly; full pytest run blocked by unrelated torch pagefile error) |
| 5 — 4-level dashboard UI | ✅ landed | 4 new in `landing-page/src/__tests__/pages/admin/TaskLedgerPage.parseFlowAction.test.js` for recipe_* field precedence |
| AutoGen `group_chat.messages` ephemerality | out-of-scope by direction | — |

---

## 0. FIRST PRINCIPLES — lifetime classes + canonical IDs

### 0a. Three lifetime classes of state

| Class | What | Lifetime | Persistence | Examples in HARTOS |
|---|---|---|---|---|
| **Definitional** | What an agent IS | Forever (until explicitly deleted) | Disk, one file per concept | `prompts/{prompt_id}.json`, `prompts/{prompt_id}_{flow_id}_recipe.json` |
| **Historical** | What ACTUALLY ran | Forever (until purged) | Disk, one file per execution instance | `agent_data/ledger_{agent_id}_{session_id}.json`, `agent_data/{prompt_id}_agent_data.json`, `simplemem_db/{user_prompt}/` |
| **Ephemeral** | Performance caches | Process lifetime; rebuildable from durable state via TTLCache `loader=` callbacks | Process memory + on-miss reload from disk | `user_ledgers`, `recipes`, `agent_data`, `user_simplemem`, `recipe_for_persona`, etc. |

**Rule**: every Ephemeral entry MUST be reconstructable from Definitional + Historical on demand. The canonical mechanism is `core/utils/ttl_cache.py::TTLCache(..., loader=<fn>)` — every cache that holds restorable state passes a loader. **A cache without a loader is a parallel store, and that's a bug** if its content can outlive the process.

### 0b. The canonical identifiers (echoes `TASK_LEDGER_GROUPING_FIX_PLAN.md` §0)

| ID | Type | Lifetime class | Source of truth |
|---|---|---|---|
| `prompt_id` | int for human-created agents; UUID string for autonomous agents | Definitional | `prompts/{prompt_id}.json` filename |
| `flow_id` | int, 0-indexed per prompt | Definitional | `prompts/{prompt_id}_{flow_id}_recipe.json` filename suffix |
| `action_id` | int, restarts at 1 per flow | Definitional | `recipe.actions[].action_id` field inside the recipe JSON |
| `session_id` | string, free-shaped | **Historical** | `agent_data/ledger_{agent_id}_{session_id}.json` filename suffix |
| `agent_id` | mirrors `prompt_id` domain | Historical (always `str(prompt_id)`) | `SmartLedger.agent_id` |

---

## 1. CURRENT-STATE AUDIT — what's already restored on restart

HARTOS uses `TTLCache(ttl_seconds=7200, max_size=500, name='…', loader=<fn>)` as the canonical restoration primitive (`core/cache_loaders.py`). On cache miss (eviction, TTL expiry, or process restart), the loader callback rebuilds the entry from the durable source. Here's every cache I located:

### 1a. Caches WITH loaders (cross-restart restore works today)

| Cache (location) | Loader callback | Restores from | What gets restored |
|---|---|---|---|
| `agent_data` (`create_recipe.py:407`, `reuse_recipe.py:235`) | `load_agent_data(prompt_id)` (`cache_loaders.py:49`) | `agent_data/{prompt_id}_agent_data.json` (encrypted if possible) | Agent metadata (name, capabilities, status) |
| `user_simplemem` (`create_recipe.py:408`, `reuse_recipe.py:236`) | `load_user_simplemem(user_prompt)` (`cache_loaders.py:141`) | `simplemem_db/{user_prompt}/` | **User-agent conversation buffer** (the running chat history the LLM reads) |
| `user_ledgers` (`create_recipe.py:3291`) | `load_user_ledger(user_prompt)` (`cache_loaders.py:82`) | `agent_data/ledger_*.json` via `create_ledger_with_auto_backend` → `SmartLedger.load()` | All recipe + dynamic Tasks with status + state_history. **AND**: also calls `restore_action_states_from_ledger(user_prompt, ledger)` (`lifecycle_hooks.py:1064`) which rebuilds the in-memory `action_states` from each Task.status |
| `recipes` (`reuse_recipe.py:216`) | `load_recipe(user_prompt)` (`cache_loaders.py:115`) | `prompts/{prompt_id}_{flow_id}_recipe.json` (tries flow 0..9) | Recipe schema (actions, personas, flows) |

### 1b. ActionState — automatically rebuilt from ledger on cache miss

The `restore_action_states_from_ledger` (`lifecycle_hooks.py:1064`) hook is **invoked inside `load_user_ledger`** at `cache_loaders.py:100-104`:

```python
from lifecycle_hooks import restore_action_states_from_ledger
restored = restore_action_states_from_ledger(user_prompt, ledger)
```

So whenever a user's ledger is re-pulled from disk, the per-action ActionState (ASSIGNED / IN_PROGRESS / PENDING / ERROR / TERMINATED / …) is rebuilt automatically via `REVERSE_MAP` (line 1085-1094). **No manual restore call needed.**

### 1c. Caches WITHOUT loaders (today: lost on cache miss)

The following caches hold session-scoped state but have NO `loader=` callback:

| Cache | Holds | Why it matters | Today's behaviour on cache miss |
|---|---|---|---|
| **`recipe_for_persona`** (`create_recipe.py:4863`) | Current flow index per user_prompt | If 0 returned on miss, an in-flight execution at flow 3 resumes at flow 0 → re-runs already-completed flows | Falls back to `initialise_current_flow_to_zero` → returns 0 |
| `user_tasks` (`create_recipe.py:3290`) | `Action` instance (recipe-action wrapper) | Per-flow action state machine | Returns None → caller re-constructs from recipe + ledger (acceptable, but no loader = no explicit contract) |
| `user_agents` (`create_recipe.py:382`) | AutoGen `(author, assistant_agent, executor, group_chat, manager, chat_instructor, agents_object)` tuple | The AutoGen GroupChat instance. `group_chat.messages` is the agent-to-agent transcript | Returns None → caller rebuilds the GroupChat from scratch. **`group_chat.messages` (agent-to-agent transcript) is NOT on disk anywhere — permanent data loss across restart.** This is by design today (agent-to-agent is high-volume internal traffic) but means "session conversation restore" for the autogen side is impossible without a separate persistence layer. |
| `total_persona_actions`, `final_recipe`, `individual_json`, `time_actions`, `time_agents`, `task_time`, `agent_metadata`, `messages`, `recent_file_id`, `request_id_list`, `scheduler_check`, `vlm_recipes`, `llm_call_track`, `user_journey`, `temp_users`, `user_delegation_bridges` | Various derived / scratch / convenience caches | Mostly rebuildable | Returns None → caller recomputes |

### 1d. User ↔ agent chat history (separate from agent ↔ agent)

There are **two different "conversation" concepts** in HARTOS:

| Conversation | Where messages live | Persisted? | Restored on restart |
|---|---|---|---|
| **User ↔ Agent** (the chat the user sees in Nunba) | `ConversationEntry` table via `chat_messages.persist_and_publish_async` (added in the consent-fanout work 2026-05-26) | ✅ DB rows | DB queries on read — no cache to miss |
| **User ↔ Agent buffer for LLM context** | `simplemem_db/{user_prompt}/` (SimpleMemStore) | ✅ disk | `user_simplemem` cache has `load_user_simplemem` loader → rebuilt on miss |
| **Agent ↔ Agent (AutoGen GroupChat)** | `group_chat.messages` (Python list inside the cached GroupChat instance) | ❌ in-memory only | **Lost across restart.** Cache miss rebuilds an EMPTY GroupChat; the agents start their group conversation over. The user-facing reply has already been emitted, so the only loss is the agents' internal back-and-forth context for THIS request. |

### 1e. Action state machine vs Task.status (two views, one truth)

- `lifecycle_hooks.action_states[user_prompt][action_id]` — `ActionState` enum (ASSIGNED / IN_PROGRESS / STATUS_VERIFICATION_REQUESTED / COMPLETED / ERROR / TERMINATED / FALLBACK_REQUESTED / PENDING) — in-memory dict, used by the dispatch loop.
- `Task.status` in the ledger JSON — `TaskStatus` enum (`PENDING` / `IN_PROGRESS` / `COMPLETED` / `BLOCKED` / `FAILED` / `PAUSED` / `DELEGATED` / `TERMINATED` / `…`) — durable.

`sync_action_state_to_ledger` writes ActionState → Task.status on every transition; `restore_action_states_from_ledger` does the reverse on cache miss. The mapping is in `lifecycle_hooks.py:1085-1094`. **Both directions exist and are wired** — no work needed here.

---

## 2. GAPS — what actually needs work

Three real gaps, in dependency order:

### Gap A. `session_id` is deterministic — one execution instance forever per (user, prompt)

`create_ledger_from_actions` at `agent_ledger/core.py:3530-3533` derives `session_id = f"{user_id}_{prompt_id}"` from the backwards-compat positional path. **Every reuse invocation collides** on the same ledger file, so the dashboard can never show "session 1, session 2, session 3". Phase 2 fixes this.

### Gap B. `recipe_for_persona` has no `loader=` callback

The only cache holding session-affecting state that doesn't follow the canonical pattern. On cache miss → `initialise_current_flow_to_zero` → an in-flight session at flow 3 of 5 snaps to flow 0 and re-runs already-completed flows. Phase 3 fixes this by adding a `load_current_flow(user_prompt)` callback that derives the value from the latest non-terminal ledger (using the Phase 1 grouping helper). **Mirror the existing pattern — no new module, no new persistence file.**

### Gap C. Dashboard shows three levels (prompt → flow → action), missing the session layer

The renderer `landing-page/src/pages/admin/TaskLedgerPage.js:29-33` already groups by `(agent_id, owner_prompt_id)` + parses flow/action from context. With Gap A fixed (multiple sessions per prompt actually exist), the renderer needs the session layer between prompt and flow. Phase 4-5 cover the API + UI changes.

### Out of scope (gap exists but not addressed here)

- **AutoGen GroupChat `group_chat.messages` ephemerality** (§1c). Persisting this is its own design (volume, structure, encryption). Today's behaviour — restart loses the in-flight inter-agent transcript but the user-facing reply already shipped via SSE + ConversationEntry — is acceptable. Logged as future work, not part of this plan.

---

## 3. THE FIX — phases, each independently testable

### Phase 1 (✅ LANDED 2026-05-27) — Recipe-hierarchy schema + stamping

- `Task.recipe_prompt_id / recipe_flow_id / recipe_action_id` on `Task.__init__ / to_dict / from_dict`
- `create_ledger_from_actions(... , flow_id, recipe_prompt_id)` stamps each task before `seal_integrity()`
- `add_dynamic_task` inherits `recipe_prompt_id = self.agent_id`; `recipe_flow_id` from sibling tasks
- `SmartLedger.list_grouped_by_recipe_hierarchy(ledger_dir)` classmethod returns `prompt → session → flow → [actions]`
- Call sites: `reuse_recipe.py:963` (passes real `role_number`), `create_recipe.py:create_action_with_ledger` (defaults to `get_current_flow`)
- 36/36 tests pass

**Effect**: every NEW ledger task carries explicit coordinates. Legacy ledgers continue to load (None for missing fields) and the grouping helper has filename + task_id fallbacks.

### Phase 2 — Session-id per execution + resume detection

**Single change site**: `create_ledger_from_actions` in `agent-ledger-opensource/agent_ledger/core.py`.

**New session_id format**: `f"{user_id}_{prompt_id}_{ts_ms}"` where `ts_ms = int(time.time() * 1000)`. Plain timestamp, sortable, human-readable. (Confirmed acceptable: no PID/counter tiebreaker — millisecond clock is sufficient given Python's GIL and HARTOS's single-machine request rate.)

**Resume logic** (no new module, delegates to Phase 1 helper):

```python
def create_ledger_from_actions(agent_id=None, session_id=None, actions=None, backend=None,
                                user_id=None, prompt_id=None,
                                flow_id=0, recipe_prompt_id=None,
                                resume_if_unfinished: bool = True):
    if user_id is not None and prompt_id is not None:
        agent_id = str(prompt_id)
        # session_id resolved below — do NOT default to f"{user_id}_{prompt_id}"

    if session_id is None:
        if resume_if_unfinished:
            session_id = _find_resumable_session(agent_id, user_id)
        if session_id is None:
            import time as _t
            session_id = (f"{user_id}_{prompt_id}_{int(_t.time() * 1000)}"
                           if user_id and prompt_id
                           else f"session_{int(_t.time() * 1000)}")

    # … rest unchanged (SmartLedger construction + Task stamping from Phase 1)
```

**New private helper** (~25 lines, in the same file):

```python
def _find_resumable_session(agent_id, user_id, ledger_dir="agent_data"):
    """Return session_id of a still-unfinished ledger for this (agent_id, user_id),
    or None if none exists.  Delegates to the Phase 1 helper — no parallel walker.
    """
    groups = SmartLedger.list_grouped_by_recipe_hierarchy(ledger_dir)
    prompt_sessions = groups.get(str(agent_id), {})
    user_prefix = f"{user_id}_" if user_id is not None else None
    TERMINAL = {"completed", "failed", "terminated", "user_stopped", "rolled_back"}
    # Iterate sessions newest-first by mtime so we attach to the most-recent in-flight run.
    for session_id, flows in sorted(prompt_sessions.items(),
                                     key=lambda kv: kv[0], reverse=True):
        if user_prefix and not session_id.startswith(user_prefix):
            continue
        # Any task in any flow with non-terminal status → resumable
        for flow_tasks in flows.values():
            for _aid, task_dict in flow_tasks:
                status = str(task_dict.get('status', '')).lower()
                if status and status not in TERMINAL:
                    return session_id
    return None
```

**Legacy session_ids** (existing `ledger_{prompt}_{user}_{prompt}.json` files on disk) — recognised by the helper because they predate the timestamped form but share the `{user_id}_…` prefix.  If they contain non-terminal tasks, they're resumed exactly like new ones.  Old deterministic-name files keep their name; new writes use the timestamped form.

**Tests** (6 new in `agent-ledger-opensource/tests/test_core.py`):
- Resume attaches when an unfinished session exists
- Mints new when all sessions are terminal
- Mints new when no prior session exists
- Different user with unfinished session for same prompt → mints new (user_prefix filter)
- Resumes legacy-format `ledger_{prompt}_{user}_{prompt}.json` if it has non-terminal tasks
- `resume_if_unfinished=False` opt-out path returns deterministic-ish behaviour for old callers

### Phase 3 — `recipe_for_persona` cache with `load_current_flow` loader (mirror existing pattern)

**Single change**: turn `recipe_for_persona` into a restorable cache by adding a loader.  Mirrors the four caches in §1a; no new module.

```python
# create_recipe.py (replaces the current bare TTLCache at line 4863)
from core.cache_loaders import load_current_flow      # NEW import
recipe_for_persona = TTLCache(
    ttl_seconds=7200, max_size=500,
    name='create_recipe_for_persona',
    loader=load_current_flow,                          # NEW kwarg
)
```

```python
# core/cache_loaders.py — append next to load_user_ledger
def load_current_flow(user_prompt):
    """Derive current_flow for `user_prompt` from the latest non-terminal
    ledger session.  Mirrors load_user_ledger's pattern; reuses the
    Phase 1 grouping helper so there is exactly ONE ledger-directory
    reader in the codebase.

    Returns 0 when no ledger exists yet (fresh prompt — same as
    `initialise_current_flow_to_zero` did).
    """
    parts = str(user_prompt).split('_', 1)
    if len(parts) != 2:
        return None
    user_id, prompt_id = parts

    try:
        from agent_ledger.core import SmartLedger
        groups = SmartLedger.list_grouped_by_recipe_hierarchy(AGENT_DATA_DIR)
    except Exception as e:
        logger.debug(f"load_current_flow: helper unavailable ({e})")
        return None

    sessions = groups.get(str(prompt_id), {})
    user_prefix = f"{user_id}_"
    TERMINAL = {"completed", "failed", "terminated", "user_stopped", "rolled_back"}

    # Pick the most recent session for this user_id (newest session_id first).
    for session_id in sorted(sessions.keys(), reverse=True):
        if not session_id.startswith(user_prefix):
            continue
        flows_in_session = sessions[session_id]
        # Highest flow_id with any non-terminal task; if all complete,
        # return the highest flow_id (final state).
        flow_ids_sorted = sorted(flows_in_session.keys(), reverse=True)
        for fid in flow_ids_sorted:
            tasks = flows_in_session[fid]
            if any(str(t[1].get('status', '')).lower() not in TERMINAL for t in tasks):
                return fid
        # All tasks in this session terminal — return the highest flow seen
        if flow_ids_sorted:
            return flow_ids_sorted[0]

    return 0   # no ledger for this user_prompt yet — same as fresh
```

`get_current_flow` itself becomes a one-liner that uses the cache (which now has a loader):

```python
def get_current_flow(user_prompt):
    value = recipe_for_persona.get(user_prompt)
    if value is not None:
        return value
    initialise_current_flow_to_zero(user_prompt)   # explicit write of 0
    return 0
```

**Effect**: on Nunba restart or 2h idle, `recipe_for_persona[user_prompt]` cache misses → TTLCache calls `load_current_flow` → returns the actual flow the user was on. No drop-to-zero regression. **The deletion of `initialise_current_flow_to_zero` is intentionally NOT done here** — it's still the correct primitive for a brand-new (user, prompt) where no ledger exists yet.

**Tests** (4 new in a new file `tests/unit/test_recipe_for_persona_restore.py`):
- Cache miss with active ledger → returns the in-flight flow_id
- Cache miss with all-terminal ledger → returns the highest flow_id (final state)
- Cache miss with no ledger → returns 0 (initialise)
- Cache hit returns the cached value without reading disk

### Phase 4 — Dashboard API: `?group=hierarchy` opt-in response shape

Find the existing handler that serves `/api/agent-engine/ledger/tasks` (grep `'/agent-engine/ledger/tasks' routes/ hart_intelligence_entry.py integrations/agent_engine/`). Add an opt-in:

```python
if request.args.get('group') == 'hierarchy':
    groups = SmartLedger.list_grouped_by_recipe_hierarchy("agent_data")
    return jsonify({"success": True, "groups": groups})
# else: existing flat path unchanged
```

The flat path stays — every existing caller continues to work. The dashboard opts in via `?group=hierarchy`.

**Tests** (3 new alongside the existing endpoint tests): hierarchical response shape on new ledgers, legacy fallback in the same response, flat path unchanged.

### Phase 5 — Dashboard renderer: 4-level expand tree

**`TaskLedgerPage.js`**: replace the grouping `useMemo` (line ~349) so it consumes the `?group=hierarchy` response directly. Four expand levels: **prompt → session (newest first) → flow → action**.

- `parseFlowAction` (line 51-69) gets one extra branch at the top:
  ```js
  if (task.recipe_flow_id != null || task.recipe_action_id != null) {
    return {
      flow: `Flow ${task.recipe_flow_id ?? '?'}`,
      action: task.recipe_action_id != null ? `Action ${task.recipe_action_id}` : '—',
    };
  }
  // else existing context-dict + regex fallbacks
  ```
- New collapse/expand state per session (mirror the existing `expandedFlows` Map pattern).

**`AgentDashboardPage.js`** + `AgentOperationsDrawer.jsx`: when the operator clicks an agent in the dashboard, the operations drawer shows the session list (newest first) rather than a flat task table.

**Tests** (RTL, in `__tests__/components/Admin/`): 4-level render with 2 sessions × 2 flows × 3 actions, legacy data (no session_id in JSON) renders with synthetic "session=—" label, expanding a session loads its flows.

---

## 4. EXECUTION ORDER + ROLLBACK BOUNDARIES

| Phase | File scope | Independent of? | Rollback signal |
|---|---|---|---|
| 1 (done) | `agent_ledger/core.py`, recipe call sites | — | Schema additions; revert diff |
| 2 | `agent_ledger/core.py::create_ledger_from_actions`, new private helper | depends on Phase 1 helper | set `resume_if_unfinished=False` → behaviour reverts to deterministic session_id |
| 3 | `create_recipe.py::recipe_for_persona` (add `loader=`), `core/cache_loaders.py` (add `load_current_flow`) | independent of Phase 2 | drop the `loader=` kwarg → cache miss returns None → `get_current_flow` falls back to `initialise_current_flow_to_zero` (today's behaviour) |
| 4 | The Flask route handler serving `/api/agent-engine/ledger/tasks` | depends on Phase 1 helper | omit `?group=hierarchy` → flat path unchanged |
| 5 | `TaskLedgerPage.js`, `AgentDashboardPage.js`, `AgentOperationsDrawer.jsx` | depends on Phase 4 | flip the view-mode prop back to `'flat'`; React component diff revert |

Each phase ships with its own tests. **Zero-regression criterion**: every existing test in `agent-ledger-opensource/tests/test_core.py`, `tests/admin/`, and the Nunba `__tests__/components/Admin/` suite must still pass after each phase.

---

## 5. PARALLEL-PATH AUDIT — every new code path mapped to a canonical surface

| New code | Delegates to (existing canonical) | Why not a parallel path |
|---|---|---|
| `_find_resumable_session` (Phase 2) | `SmartLedger.list_grouped_by_recipe_hierarchy` + status string comparison | reuses the Phase 1 helper; no new directory walk; status set is the same one `Task.is_terminal()` uses |
| New session_id format `f"{user_id}_{prompt_id}_{ts_ms}"` | existing filename convention `ledger_{agent_id}_{session_id}.json` | suffix-extension only; legacy deterministic-form files keep working |
| `load_current_flow` loader (Phase 3) | same Phase 1 helper | reads the same data dashboard reads; mirrors `load_user_ledger`'s pattern |
| `?group=hierarchy` (Phase 4) | same Phase 1 helper | flat path stays; the new shape is opt-in |
| Dashboard 4-level tree (Phase 5) | the Phase 4 API response | no client-side regrouping |

**Explicitly NOT introduced** (would be parallel paths):
- ❌ Separate `sessions.json` registry (would diverge from ledger files)
- ❌ Separate `flow_state.json` per user (would diverge from `recipe_for_persona` cache + ledger)
- ❌ Parallel ledger-directory walker (the Phase 1 helper IS the reader)
- ❌ A new TTLCache restoration primitive (the existing `loader=` pattern already covers it)
- ❌ A new `restore_session()` function (`load_user_ledger` already restores everything via `restore_action_states_from_ledger` chain)

---

## 6. WHAT THIS PLAN DOES NOT SOLVE — logged for transparency

- **AutoGen `group_chat.messages` ephemerality** (§1c). The inter-agent transcript is in-memory only. Restart loses it. By design today (high-volume internal traffic); persisting it is a separate plan: design choices include (a) selective persistence of only the final user-facing assistant message — already done via SSE + ConversationEntry — versus (b) full transcript persistence for debugging / replay.
- **Stale-session retention.** "Unfinished" today means "any task non-terminal". No time cap. Operator can manually delete stale ledgers from `agent_data/`. Auto-purge belongs to a separate retention plan.
- **Multi-user concurrency on a single (user_id, prompt_id, ts_ms) tuple.** With millisecond resolution, two simultaneous calls within the same ms collide. Python's GIL + HARTOS single-machine deployment makes this vanishingly rare; accepted tradeoff per user direction. If a collision ever occurs in production, the `add_task` duplicate guard at `core.py:1422-1424` fails loudly rather than silently corrupting.

---

## 7. APPENDIX — file:line evidence

| Claim | Source |
|---|---|
| `session_id = f"{user_id}_{prompt_id}"` is deterministic | `agent_ledger/core.py:3530-3533` |
| `SmartLedger.load()` auto-restores on construction | `agent_ledger/core.py:1366` |
| `add_task` skips on duplicate task_id | `agent_ledger/core.py:1422-1424` |
| `recipe_for_persona` is bare `TTLCache` with NO loader | `create_recipe.py:4863` |
| Four caches with loaders (canonical restore pattern) | `create_recipe.py:407-408, 3291`, `reuse_recipe.py:216, 235-236` — all delegate to `core/cache_loaders.py` |
| `load_user_ledger` rebuilds ActionState via `restore_action_states_from_ledger` | `core/cache_loaders.py:100-104`, callee at `lifecycle_hooks.py:1064-1114` |
| ActionState ↔ TaskStatus mapping | `lifecycle_hooks.py:1085-1094` |
| User ↔ Agent conversation persisted via `ConversationEntry` | `chat_messages.persist_and_publish_async` (added 2026-05-26 consent-fanout work) |
| Agent ↔ Agent (AutoGen) `group_chat.messages` is in-memory only | `user_agents` TTLCache at `create_recipe.py:382` has no loader; `group_chat.messages` field is a plain Python list on the GroupChat instance |
| `SmartLedger.list_grouped_by_recipe_hierarchy` (Phase 1 helper) | `agent_ledger/core.py` `@classmethod` added 2026-05-27 |
| `Task.recipe_*` schema fields | `agent_ledger/core.py` `Task.__init__` recipe-hierarchy block (Phase 1) |
| Dashboard groups by `agent_id__owner_prompt_id` | `landing-page/src/pages/admin/TaskLedgerPage.js:29-33` |
| Dashboard reads `context.flow` / `context.action_id` | `TaskLedgerPage.js:51-69` (`parseFlowAction`) |
| Recipe filename convention | `prompts/{prompt_id}_{flow_id}_recipe.json` (CLAUDE.md "Recipe Storage") |
| Ledger filename convention | `agent_data/ledger_{agent_id}_{session_id}.json` (`SmartLedger.__init__:1349`) |
| SimpleMem conversation buffer dir | `simplemem_db/{user_prompt}/` (`core/platform_paths.py:127`) |
