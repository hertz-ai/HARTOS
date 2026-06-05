# Tool-Name Unification — Detailed Implementation Plan (#510 follow-up → #511)

Grounded in full reads of: `core/agent_tools.py` (1257L), `core/session_cache.py`,
`reuse_recipe.py` (3719L — targeted spans 1-50, 170-200, 340-365, 680-810,
1090-1240, 1700-1840, 2250-2510), `create_recipe.py` (5100L — spans 280-400,
780-810, 900-1000, 1400-1550, 2580-2900), `integrations/channels/agent_tools.py`,
`core/labeled_autogen_function.py`, `core/tool_logging.py`, `helper.py:2370-2680`.

---

## §0 — Architectural picture (from the reads)

### Three agent pairs co-exist in `reuse_recipe.create_agents_for_user`

| Pair | Created at | Tool registration source |
|---|---|---|
| **Main flow** (`helper`, `assistant`, `executor`, `multi_role_agent`, `verify`, `chat_instructor`) | reuse_recipe.py:1094-1188 | **Inline AH-stack decorators** at 1242-2113 |
| **Time flow** (`helper1`, `time_agent`) | (created by `helper_fun.create_time_agent`) | `register_core_tools(core_tools, helper1, time_agent)` at reuse_recipe.py:2262 + `register_channel_tools(helper1, time_agent, _tool_ctx)` at 2267 |
| **Visual flow** (`helper2`, `visual_agent`) | reuse_recipe.py:2297 (via `helper_fun.create_visual_agent`) | `register_core_tools(core_tools, helper2, visual_agent)` at 2301 + `register_channel_tools(helper2, visual_agent, _tool_ctx)` at 2306 |

This invalidates my earlier audit's "registered LAST wins" claim. The pairs are
**parallel** — each pair has its own `_function_map`. The MAIN flow sees ONLY the
AH-stack impls; the TIME and VISUAL flows see ONLY the core_AT canonicals.

Recipes written by the LLM are silent about which agent pair calls the tool —
autogen routes to the registered impl on whichever agent the speaker-selection
elected. If main-flow drift differs from time-flow canonical, the same recipe
step behaves differently depending on which agent pair the LLM picks.

### Create_recipe has parallel structure

| Pair | Created at | Registration source |
|---|---|---|
| Main flow | create_recipe.py:?? | `register_core_tools(core_tools, helper, assistant)` at 909 + inline `register_dual` calls at 931, 961, 981, 1404, 1439, 1458, 1504, 1544, 1659, 1671, 1682, 1704 |
| Time flow | create_recipe.py:?? | `register_core_tools(core_tools, ?, ?)` at 2941 |
| (no visual flow) | — | — |

Create_recipe's MAIN flow uses `register_core_tools` FIRST then `register_dual`
overrides — same `_function_map`, second registration wins for the 6 inline
override names (`get_user_details`, `consult_expert`, `execute_windows_or_android_command`,
`delegate_to_specialist`, `share_context_with_agents`, `get_shared_context`).

So in create-mode the MAIN flow has core_AT canonicals PLUS 6 inline overrides
that win on their names.

### Persona registry (concrete locations)

| Cache | Type | Defined at | All usage sites |
|---|---|---|---|
| `agents_session` | `TTLCache(7200, 500)` | `reuse_recipe.py:181` | `reuse_recipe.py:352, 359, 723, 728, 740, 802` |
| `agents_roles` | `TTLCache(7200, 500)` | `reuse_recipe.py:186` | `reuse_recipe.py:726, 731, 743, 806` |
| `chat_joinees` | `TTLCache(7200, 500)` | `reuse_recipe.py:185` | `reuse_recipe.py:357, 358, 745` |

No reads/writes outside `reuse_recipe.py` per repo-wide grep — clean extraction
target.

### Decorator order bug (from my #509 bulk-AST insertion)

25 inner functions in `reuse_recipe.py` look like:
```python
@log_tool_execution                                   # OUTERMOST — applied LAST
@assistant.register_for_execution()
@helper.register_for_llm(api_style="function", description="...")
def fn(...) -> str:
    ...
```

Python applies decorators bottom-up. `helper.register_for_llm` and
`assistant.register_for_execution` both **return the function unchanged after
their side-effect** (the side-effect being: store fn in `helper._llm_signatures`
and `assistant._function_map[fn.__name__]`). They store the **unwrapped** `fn`.

Then `@log_tool_execution` wraps the module-level name `fn`, but autogen's
`_function_map` already holds the unwrapped reference. **Runtime tool calls
bypass `@log_tool_execution` entirely.** No emit, no logging.

Confirmed at file:line:
- 710 (`update_persona`)
- 1242 (`txt2img`)
- 1255 (`img2txt` — actual line is 1253 per AST output; reading shows function
  named `img2txt` registered as `func.__name__='img2txt'`)
- 1278 (`save_data_in_memory`)
- 1354 (`get_saved_metadata`)
- 1362 (`get_data_by_key`)
- 1391 (`get_user_id`)
- 1399 (`get_prompt_id`)
- 1408 (`Generate_video`)
- 1480 (`get_user_uploaded_file`)
- 1492 (`get_user_camera_inp`)
- 1520 (`get_chat_history`)
- 1532 (`search_visual_history`)
- 1546 (`register_visual_watcher`)
- 1562 (`search_long_term_memory`)
- 1579 (`save_to_long_term_memory`)
- 1621 (`create_scheduled_jobs`)
- 1642 (`send_message_to_user`)
- 1672 (`send_presynthesized_video_to_user`)
- 1700 (`send_message_in_seconds` — but this one already has @log_tool_execution AT LINE 1699 above the existing decorators? Need to confirm.)
- 1714 (`consult_expert`)
- 1732 (`get_user_camera_inp_by_mins`)
- 1746 (`execute_windows_or_android_command`)
- 2089 (`create_new_agent`)
- 2097 (`google_search` — re-registered? need check)

Fix is mechanical: move `@log_tool_execution` to be INNERMOST (closest to
`def`). New order:
```python
@assistant.register_for_execution()    # OUTERMOST
@helper.register_for_llm(...)
@log_tool_execution                    # INNERMOST — applied first
def fn(...) -> str: ...
```

### Hallucination traps (audit Section 3)

| Name in prompt | Reality | Files (line) |
|---|---|---|
| `get_data_from_memory` | Not registered. Conceptually = `get_data_by_key` | reuse_recipe.py:1102, 1129, 2151, 2174 |
| `send_message_to_roles` | Commented-out closure at reuse_recipe.py:1203-1240 | reuse_recipe.py:1101, 1128, 2150, 2173 |
| `connect_time_main` | Registered as `Connect_to_main_agent` via `name=` override at reuse_recipe.py:2293 | create_recipe.py:2823, reuse_recipe.py:2127 |

### Name aliases (different name → same concept, different impls)

| Concept | Names (impl) | Registered on |
|---|---|---|
| Text→image | `text_2_image` (core_AT:108, `helper_fun.txt2img`); `txt2img` (reuse:1242, cloud Rasa) | core_AT version on time+visual pairs; reuse:1242 version on main pair |
| Image→text | `get_text_from_image` (core_AT:508, SSRF-validated Qwen/cloud LLaVA); `img2txt` (reuse:1255 — same name but func body uses `pooled_post` direct) | core_AT version on time+visual; reuse:1255 on main |
| Camera by minutes | `search_visual_history` (core_AT:613, time-range query); `get_user_camera_inp_by_mins` (reuse:1732, `helper_fun.get_visual_context`) | core_AT on time+visual; reuse on main |

### Same-name-different-impl pairs

| Name | core_AT (line) | reuse-inline (line) | create-inline (line) |
|---|---|---|---|
| `save_data_in_memory` | 133 — disk persist + verify | 1278 — adds MemoryGraph dual-write | (via register_core_tools at 909) |
| `get_saved_metadata` | 195 — lazy-load on miss | 1354 — no lazy load | (via register_core_tools) |
| `get_data_by_key` | 213 — lazy-load | 1362 — falls back to MemoryGraph semantic search | (via register_core_tools) |
| `Generate_video` | 266 — LTX-2 + avatar + hive fallback | 1408 — avatar-only | (via register_core_tools) |
| `create_scheduled_jobs` | 518 — stub | 1621 — real cron job creation | (via register_core_tools) |
| `send_message_to_user` | 536 — unconditional thread spawn | 1642 — has @statusverifier/@helper/@executor mention guard | (via register_core_tools) |
| `get_user_details` | 1059 — DB lookup + cloud fallback | (not present) | 928 — `helper_fun.parse_user_id(int(user_id))` only |
| `consult_expert` | (not present) | 1714 — match_expert_for_context + send_message_to_user1 | 967 — identical body |
| `execute_windows_or_android_command` | (not present) | 1746 — `_active_tools_lock` re-entry guard + `recipes` cache | 1404 — no guard + `vlm_recipes` cache |
| `delegate_to_specialist`, `share_context_with_agents`, `get_shared_context` | (not present) | 2449, 2470, 2479 | 1659, 1671, 1682 |

---

## §1 — Guiding rules (per user directives in this session)

| Rule | Source |
|---|---|
| **Never remove a registered tool** | "never remove a registerdd tool find an existing iomplementation which matches that canonically" |
| **Prompts adapt to registrations** | "capaitaliszation variants just change the function names in prompts so tat the changes are minimalitic" |
| **Recipe JSON schema stays compatible, values may break** | "the json structure shd not be chsnged so that the prompts are compatible, values nned not be respected" |
| **Same impl behind each name** | "if there are implementation drift" → "check the best implementation" |
| **Persona-registry single source of truth** | "why do we iport agent sesssions fromm another file :O" |
| **Gather requirements already populates persona data** | "gather requirements gets us the needed persona/role always why do we need stub?" |
| **No silent gulps; always log** | "do not gulp any exception, always log everything" |

---

## §2 — Phase-by-phase plan

### Phase 0 — Fix decorator order on 25 reuse-inner functions (HIGHEST PRIORITY)

**Problem:** `@log_tool_execution` is currently the OUTERMOST decorator on 25
functions in `reuse_recipe.py`. Because `helper.register_for_llm` and
`assistant.register_for_execution` store the unwrapped function in
`_function_map` and `_llm_signatures` BEFORE `@log_tool_execution` runs, the
wrapper is dead code at runtime.

**Solution:** Move `@log_tool_execution` to be the INNERMOST decorator (closest
to `def`) so it wraps first and the registrations see the wrapped function.

**Files & line changes:**

For each of the 25 functions (lines listed in §0 above), change:
```python
    @log_tool_execution                                   # CURRENT
    @assistant.register_for_execution()                   # (or @helper.register_for_execution for update_persona at L710)
    @helper.register_for_llm(api_style="function", description="...")
    def fn(...) -> str: ...
```
to:
```python
    @assistant.register_for_execution()                   # NEW: outermost
    @helper.register_for_llm(api_style="function", description="...")
    @log_tool_execution                                   # NEW: innermost
    def fn(...) -> str: ...
```

Special case `update_persona` at L710 (HA-stack):
```python
    @helper.register_for_execution()
    @assistant.register_for_llm(api_style="function", description="update the role/persona in db")
    @log_tool_execution
    def update_persona(...) -> str: ...
```

**Mechanical implementation:** identical to the bulk-AST insertion I did earlier
in this session — but inserting the decorator BELOW the existing two, not above.

**Test:** new test `test_decorator_order_registered_function_is_wrapped`:
```python
import asyncio
from unittest.mock import MagicMock

def test_decorator_order_registered_function_is_wrapped():
    """Autogen's register_for_execution stores the function reference at
    decorator-application time.  If @log_tool_execution wraps AFTER the
    registration decorators, autogen holds the unwrapped reference and
    runtime tool calls bypass logging+UI emit.  This test runs the
    actual decorator stack used in reuse_recipe.py and asserts the
    function stored in assistant._function_map has __wrapped__ set."""
    import autogen
    from core.tool_logging import log_tool_execution

    helper = autogen.AssistantAgent(name="Helper", llm_config=False)
    assistant = autogen.UserProxyAgent(name="assistant", llm_config=False,
                                        code_execution_config=False)

    @assistant.register_for_execution()
    @helper.register_for_llm(name="probe_tool", description="...")
    @log_tool_execution
    def probe_tool(x: str) -> str:
        return f"got:{x}"

    registered = assistant._function_map.get("probe_tool")
    assert registered is not None, "tool not in function_map"
    assert hasattr(registered, "__wrapped__"), (
        "Registered function has no __wrapped__ — @log_tool_execution "
        "is not the innermost decorator.  Check decorator order: it "
        "MUST be closest to `def`."
    )
```

**Risk:** zero — pure decorator-stack reorder. Behavior was already broken; this
makes it work as intended.

**Verification:** import smoke + 1 unit test + spot-check at `reuse_recipe.py:1242`
(txt2img) that the order looks like:
```python
@assistant.register_for_execution()
@helper.register_for_llm(...)
@log_tool_execution
def txt2img(...): ...
```

**Time:** 30 min (bulk AST replace + 1 test).

**Commit:** `fix(reuse_recipe): @log_tool_execution must be innermost decorator (#509 followup)`

---

### Phase 1.1 — `get_data_from_memory` alias

**Problem:** Helper system prompts at `reuse_recipe.py:1102, 1129, 2151, 2174`
advertise `get_data_from_memory` to the LLM. No tool with that name is
registered anywhere.

**Solution:** In `core/agent_tools.py:228`, after appending the canonical
`get_data_by_key` tuple, append a SECOND tuple wiring the SAME function under
the alias name `get_data_from_memory`. Single closure, dual registration. Both
flows (create + reuse, all 3 pairs) pick up both names via `register_core_tools`.

**File & line:** `core/agent_tools.py` after line 232.

**Before:**
```python
    tools.append((
        "get_data_by_key",
        "Returns all data from the internal Memory using key",
        get_data_by_key,
    ))

    # ------------------------------------------------------------------
    # 6. get_user_id
```

**After:**
```python
    tools.append((
        "get_data_by_key",
        "Returns all data from the internal Memory using key",
        get_data_by_key,
    ))
    # Alias — Helper system prompts in reuse_recipe.py reference this name (#510)
    tools.append((
        "get_data_from_memory",
        "Returns all data from the internal Memory using key (alias of get_data_by_key)",
        get_data_by_key,
    ))

    # ------------------------------------------------------------------
    # 6. get_user_id
```

**Risk:** zero — same function under a second name. Both names resolve to the
same `get_data_by_key` closure (identity check confirms).

**Test:** in `tests/unit/test_tool_registration_parity.py`:
```python
def test_get_data_from_memory_aliases_get_data_by_key():
    """The Helper system prompts advertise `get_data_from_memory` — it must
    be a registered alias of `get_data_by_key` so LLM calls resolve."""
    ctx = _make_mock_ctx()
    from core.agent_tools import build_core_tool_closures
    closures = build_core_tool_closures(ctx)
    by_name = {name: func for name, _, func in closures}
    assert 'get_data_from_memory' in by_name
    assert 'get_data_by_key' in by_name
    assert by_name['get_data_from_memory'] is by_name['get_data_by_key'], (
        "alias must share the same closure object so behavior is identical")
```

**Time:** 15 min.

**Commit:** `fix(autogen): alias get_data_from_memory → get_data_by_key (#510)`

---

### Phase 1.2 — `send_message_to_roles` restore + `core/persona_registry.py`

**Problem:** Helper system prompts at `reuse_recipe.py:1101, 1128, 2150, 2173`
advertise `send_message_to_roles` — for multi-persona agent instances (a single
agent shared by student/parent/teacher roles, broadcast a message to a specific
role). Closure at `reuse_recipe.py:1203-1240` is commented out.

**Solution:**

1. **NEW MODULE** `core/persona_registry.py` — single source of truth for the
   3 TTLCaches + the canonical broadcast impl. Both `create_recipe.py` and
   `reuse_recipe.py` import from here.

2. **`reuse_recipe.py:181, 185, 186`** — DELETE the local TTLCache declarations
   for `agents_session`, `chat_joinees`, `agents_roles`. Replace with import.

3. **`reuse_recipe.py:1203-1240`** — REPLACE the commented closure with a real
   registration using the canonical impl.

4. **`create_recipe.py`** — at top of `set_for_creating_actions` (after
   `list_of_persona = config['flows'][...]['persona']` at line 790), call
   `register_persona_for_session(...)` to populate the registry from the
   gather-requirements output.

5. **`create_recipe.py:~983`** — register `send_message_to_roles` via
   `register_dual(helper, assistant, _wrapped, "send_message_to_roles", "<desc>")`
   where `_wrapped` closes over user_id+prompt_id and delegates to
   `_send_message_to_roles_impl`.

**New module content** (`core/persona_registry.py`):

```python
"""Per-(user, prompt) persona/role registry.

A single agent instance can be shared across multiple personas (student /
parent / teacher).  This module hosts the TTLCache singletons that map
{user_id_prompt_id} → list of role dicts, plus the canonical broadcast
routine that the `send_message_to_roles` tool delegates to.

Both `create_recipe.py` (recipe-authoring flow) and `reuse_recipe.py`
(recipe-replay flow) import from here.  Neither file may redeclare these
caches — single-writer invariant enforced by drift-guard test.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.session_cache import TTLCache

logger = logging.getLogger(__name__)

# {f"{user_id}_{prompt_id}": [{agentInstanceID, user_id, role, deviceID, ...}, ...]}
agents_session: TTLCache = TTLCache(
    ttl_seconds=7200, max_size=500, name='persona_agents_session')

# {f"{user_id}_{prompt_id}": {user_id: role_name}}
agents_roles: TTLCache = TTLCache(
    ttl_seconds=7200, max_size=500, name='persona_agents_roles')

# {user_id: {prompt_id: chat_creator_user_id}} for cross-user joined chats
chat_joinees: TTLCache = TTLCache(
    ttl_seconds=7200, max_size=500, name='persona_chat_joinees')


def register_persona_for_session(
    user_id: Any,
    prompt_id: Any,
    persona_list: List[Dict[str, Any]],
    device_id: str = 'something',
) -> int:
    """Populate `agents_session` + `agents_roles` from a persona list.

    Called at the start of each recipe flow:
      - create_recipe.set_for_creating_actions (after config load)
      - reuse_recipe.create_agents_for_user (via update_persona flow)

    `persona_list` is the list of {name, description, ...} dicts from
    `config['personas']` (gather-requirements output) — same shape both
    flows produce.

    Returns count of personas registered.  Idempotent — re-call
    overwrites the current session's entries (refresh after persona
    edits).
    """
    key = f"{user_id}_{prompt_id}"
    session_entries: List[Dict[str, Any]] = []
    roles_map: Dict[Any, str] = {}
    for persona in (persona_list or []):
        role_name = persona.get('name') or persona.get('role')
        if not role_name:
            logger.warning(
                "register_persona_for_session: skipping persona without "
                "name/role: %r", persona)
            continue
        session_entries.append({
            'agentInstanceID': f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
            'user_id': user_id,
            'role': role_name,
            'deviceID': device_id,
        })
        roles_map[user_id] = role_name
    agents_session[key] = session_entries
    agents_roles[key] = roles_map
    logger.info(
        "register_persona_for_session: registered %d persona(s) for %s",
        len(session_entries), key)
    return len(session_entries)


def _send_message_to_roles_impl(
    user_id: Any,
    prompt_id: Any,
    role: str,
    message: str,
    client: Optional[Any] = None,
) -> str:
    """Canonical multi-persona broadcast.

    Looks up the target persona by `role` in `agents_session`, then
    publishes the message to crossbar topic
    `com.hertzai.hevolve.agent.multichat` with caller metadata.

    Falls back to `chat_joinees` lookup if this user joined someone
    else's chat session.

    Defensive lookups + helpful-error strings (per user directive
    2026-05-11: no silent gulps, log everything).
    """
    if not role:
        logger.warning(
            "send_message_to_roles: empty role for %s_%s",
            user_id, prompt_id)
        return "Cannot broadcast: role argument is empty"

    key = f"{user_id}_{prompt_id}"
    sessions = agents_session.get(key, [])

    # Cross-user join fallback
    if not sessions:
        joinees_for_user = chat_joinees.get(user_id, {})
        creator = joinees_for_user.get(prompt_id)
        if creator:
            creator_key = f"{creator}_{prompt_id}"
            sessions = agents_session.get(creator_key, [])
            key = creator_key

    if not sessions:
        logger.warning(
            "send_message_to_roles: no agent_session for %s; "
            "personas not initialized yet?", key)
        return (
            f"No personas registered for session {key}. "
            "Run gather-requirements first.")

    caller_role = (agents_roles.get(key, {}) or {}).get(user_id)

    # Resolve crossbar client
    if client is None:
        try:
            from create_recipe import client as _client
            client = _client
        except Exception:
            try:
                from reuse_recipe import client as _client
                client = _client
            except Exception:
                logger.error(
                    "send_message_to_roles: cannot resolve crossbar "
                    "client", exc_info=True)
                return "Crossbar client unavailable"

    for entry in sessions:
        if entry.get('role') == role:
            crossbar_msg = dict(entry)
            crossbar_msg.update({
                'message': message,
                'caller_role': caller_role,
                'caller_user_id': user_id,
                'caller_prompt_id': prompt_id,
            })
            try:
                client.publish(
                    'com.hertzai.hevolve.agent.multichat', crossbar_msg)
                logger.info(
                    "send_message_to_roles: published to %s role=%s",
                    key, role)
                return 'Message sent Successfully'
            except Exception:
                logger.error(
                    "send_message_to_roles: crossbar publish failed for "
                    "%s role=%s", key, role, exc_info=True)
                return f"Failed to publish to role={role}"
    logger.warning(
        "send_message_to_roles: no persona with role=%r in session %s",
        role, key)
    return f"No persona with role={role!r} in session"
```

**Edits to `reuse_recipe.py`:**

| Line | Before | After |
|---|---|---|
| 181 | `agents_session = TTLCache(7200, 500, name='reuse_agents_session')` | DELETE |
| 185 | `chat_joinees = TTLCache(7200, 500, name='reuse_chat_joinees')` | DELETE |
| 186 | `agents_roles = TTLCache(7200, 500, name='reuse_agents_roles')` | DELETE |
| 187 (insert) | — | `from core.persona_registry import agents_session, agents_roles, chat_joinees, _send_message_to_roles_impl, register_persona_for_session` |
| 1203-1240 (replace commented closure) | `# @executor.register_for_execution() ... (commented impl)` | (see below) |

Replacement for reuse_recipe.py:1203-1240:
```python
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Send a message to a specific persona/role within this multi-persona agent (e.g. student/parent/teacher).")
    @log_tool_execution
    def send_message_to_roles(
        role: Annotated[str, "Target persona/role name to deliver the message to"],
        message: Annotated[str, "The question to ask or message to send"],
    ) -> str:
        return _send_message_to_roles_impl(
            user_id, prompt_id, role, message, client=client)
```

Note: `client` is the crossbar publisher already in scope in `reuse_recipe.py`
(imported from `helper_fun`-side at module load).

**Edits to `create_recipe.py`:**

After the `with open(os.path.join(PROMPTS_DIR, f"{prompt_id}.json"), 'r')` block
at line 788-791, append (~line 807):
```python
    # Register persona/role data with the canonical registry so
    # `send_message_to_roles` broadcasts have data to resolve
    # against (#510 follow-up — persona registry is the single
    # source of truth populated by gather-requirements).
    try:
        from core.persona_registry import register_persona_for_session
        _personas = config.get('personas') or [
            {'name': p} if isinstance(p, str) else p
            for p in (list_of_persona or [])
        ]
        register_persona_for_session(user_id, prompt_id, _personas)
    except Exception:
        tool_logger.warning(
            "persona registry populate failed (recipe creation flow)",
            exc_info=True)
```

After the `register_dual(helper, assistant, consult_expert, "consult_expert", ...)` at line 981, insert:
```python
    # send_message_to_roles — multi-persona broadcast.  Uses the canonical
    # impl from core.persona_registry; same code path runs in both
    # create + reuse flows (persona TTLCaches are module-level singletons).
    from core.persona_registry import _send_message_to_roles_impl as _smr_impl
    @log_tool_execution
    def send_message_to_roles(
        role: Annotated[str, "Target persona/role name to deliver the message to"],
        message: Annotated[str, "The question to ask or message to send"],
    ) -> str:
        return _smr_impl(user_id, prompt_id, role, message, client=client)
    register_dual(helper, assistant, send_message_to_roles,
                  "send_message_to_roles",
                  "Send a message to a specific persona/role within this multi-persona agent (e.g. student/parent/teacher).")
```

**Tests:**
- `test_persona_registry_single_writer` — AST scan: assert `agents_session = TTLCache`, `agents_roles = TTLCache`, `chat_joinees = TTLCache` are assigned in EXACTLY ONE file (`core/persona_registry.py`).
- `test_send_message_to_roles_registered_in_both_flows` — boot mock helper/assistant pair; call `create_recipe`-mode + `reuse_recipe`-mode setup paths; assert `send_message_to_roles` in `assistant._function_map`.
- `test_send_message_to_roles_broadcasts_with_role_match` — populate `agents_session` with two roles, invoke impl with one role, assert `client.publish` called with the right payload (mocked client).
- `test_send_message_to_roles_helpful_error_when_no_personas` — empty `agents_session`, assert return string mentions "No personas registered".

**cx_Freeze:** `core/persona_registry.py` is a new module under `core/` (already
tracked). Static `from core.persona_registry import ...` statements in
create_recipe + reuse_recipe ensure cx_Freeze tracer picks it up. No
`scripts/setup_freeze_nunba.py packages[]` edit needed; verify via build smoke.

**Risk:**
- Circular import: `_send_message_to_roles_impl` lazy-imports `client` from
  `create_recipe` or `reuse_recipe`. Both files import `core.persona_registry`
  at module load. To avoid cycles, the `client` lookup happens INSIDE the
  function (only on tool invocation, after both modules are fully loaded).
- TTLCache identity: existing reuse-mode users have entries in the OLD
  `reuse_agents_session` cache (name='reuse_agents_session'). After this
  refactor the new cache is named `persona_agents_session`. The data is in-
  memory only (no on-disk state) — restart wipes both. No migration needed.

**Time:** 1.5 hr.

**Commit:** `feat(autogen): restore send_message_to_roles multi-persona router (#510)`

---

### Phase 2.1 — `Connect_to_main_agent` → `connect_time_main` rename

**Problem:** LLM system prompt at `create_recipe.py:2823` (Time Agent prompt)
mentions `connect_time_main` as the tool name. Reuse_recipe registers it as
`Connect_to_main_agent` via `register_dual`'s `name=` override at
`reuse_recipe.py:2294`. LLM emits the wrong name → autogen returns "function
not found".

**Solution:** Change the `name=` override to match the function name and the
prompt.

**File & line:** `reuse_recipe.py:2294`.

**Before:**
```python
    register_dual(helper1, time_agent, connect_time_main,
                  "Connect_to_main_agent",
                  "Connects time agent to main assistant agemt to perform actions which time agent cannot perform")
```

**After:**
```python
    register_dual(helper1, time_agent, connect_time_main,
                  "connect_time_main",
                  "Connects time agent to main assistant agent to perform actions which time agent cannot perform")
```
(Also fixes typo "agemt" → "agent".)

**Risk:** zero. Repo-wide grep confirms no saved recipe references
`Connect_to_main_agent`.

**Test:**
```python
def test_register_dual_name_matches_func_name_or_is_aliased():
    """For every register_dual(helper, executor, func, name, desc) call in
    create_recipe.py + reuse_recipe.py, the `name` arg must equal
    func.__name__ — OR appear in a documented alias allowlist.
    Aliases must point to a function whose __name__ is in the LLM-prompt
    tool-name set."""
    ALLOWLIST = {
        # Format: registered_name -> func_name
        # Aliases are explicit intent.  Add a comment when extending.
    }
    # AST walk both files, assert all register_dual name args match.
    ...
```

**Time:** 5 min.

**Commit:** `fix(autogen): rename Connect_to_main_agent → connect_time_main to match LLM prompt (#510)`

---

### Phase 3 — Same-name impl unification (6 pairs)

For each pair below, the **canonical impl** is the one that's currently the
"better" version (richer error handling, defensive guards, broader fallback).
The CANONICAL becomes the single function body in `core/agent_tools.py`. The
parallel inline registrations (in create_recipe.py or reuse_recipe.py) are
**kept** but their bodies become one-line delegates so all flows execute the
same code.

#### 3.1 `get_user_details` — pick core_AT canonical

| | core_AT (1031-1058) | create:927-932 |
|---|---|---|
| Body | local DB lookup → cloud fallback (lines 1031-1058) | `helper_fun.parse_user_id(int(user_id))` only |
| Better | ✓ | ✗ |

**Edit `create_recipe.py:927-932`:** Replace inline body with delegate.

**Before:**
```python
    @log_tool_execution
    def get_user_details()->str:
        tool_logger.info('INSIDE get user details')
        return helper_fun.parse_user_id(int(user_id))
    register_dual(helper, assistant, get_user_details,
                  "get_user_details", "Get User details like name, dob, gender")
```

**After:**
```python
    # get_user_details — delegate to the canonical core_AT closure built
    # above via register_core_tools (it's already registered on this
    # agent pair via line 909).  register_dual re-registers under the
    # same name with the same closure — autogen overwrites with the
    # same reference, effectively a no-op.  Kept for symmetry with
    # reuse_recipe and to make the call site explicit.
    _gud = next((f for n, _, f in core_tools if n == 'get_user_details'), None)
    if _gud is not None:
        register_dual(helper, assistant, _gud,
                      "get_user_details",
                      "Get the user's profile information (name, email, preferences, etc.)")
```

This drops the parallel `helper_fun.parse_user_id` fallback. The single canonical
impl is the core_AT one (DB lookup + cloud fallback).

**Risk:** behavior change for users whose DB lookup fails — they'll fall back
to cloud `getstudent_by_user_id` instead of `helper_fun.parse_user_id`. The
audit's "limited fallback" assessment was wrong-direction (parse_user_id IS the
limited one).

#### 3.2 `consult_expert` — identical bodies (audit Section 2.2)

**Edit:** NO body change. Add a drift-guard test asserting the two function
sources stay identical (or both delegate to a shared `core.agent_tools._consult_expert_canonical` helper).

**Cleanest:** move the body to `core/agent_tools.py` as a new closure, have
both inline defs at `create_recipe.py:967` and `reuse_recipe.py:1718` become
delegates.

**Edit core/agent_tools.py:** add closure block after `request_resource`
(~line 1141, before line 1143):
```python
    # ------------------------------------------------------------------
    # consult_expert — match a domain expert agent for the current task
    # ------------------------------------------------------------------
    @log_tool_execution
    def consult_expert(
        task_description: Annotated[str, "Describe what expertise you need"],
    ) -> str:
        """Consult a domain expert agent for specialized guidance."""
        try:
            from integrations.expert_agents import match_expert_for_context
            match = match_expert_for_context(task_description, top_k=3, min_score=2)
            if not match:
                return "No domain expert matched this task. Proceeding with general knowledge."
            send_message_to_user1(
                user_id, f"Consulting expert: {match['name']}",
                "Expert consultation", prompt_id)
            return f"Expert guidance from {match['name']}:\n{match['prompt_block']}"
        except Exception as e:
            tool_logger.warning(f"consult_expert failed: {e}")
            return f"Expert consultation unavailable: {str(e)}"

    tools.append((
        "consult_expert",
        "Consult a specialized domain expert for the current task",
        consult_expert,
    ))
```

**Edit `create_recipe.py:966-983`:** replace inline body with delegate to
core_AT closure.

**Edit `reuse_recipe.py:1714-1730`:** same — replace body with delegate
(keeping the AH-stack registration so the main flow gets it).

**Risk:** zero — identical bodies.

#### 3.3 `execute_windows_or_android_command` — pick reuse canonical (has re-entry guard)

| | reuse:1746 | create:1404 |
|---|---|---|
| Re-entry guard | `_active_tools_lock` + `_active_tools` dict | none |
| Cache | `recipes` (reuse's TTLCache) | `vlm_recipes` (create's TTLCache) |
| Better | ✓ (guard prevents concurrent runs of same command) | ✗ |

**Approach:** Extract the canonical impl into `core/agent_tools.py` as a
parameterized closure (takes `recipes_cache` arg). Both inline call sites pass
their respective cache dict and call the canonical.

**Edit core/agent_tools.py:** add closure (parameterize on `recipes_cache` and
`_active_tools` + `_active_tools_lock`, which would need to be created in
core_AT too):

```python
# At module level of core/agent_tools.py:
_exec_active_tools: dict = {}
_exec_active_tools_lock = threading.Lock()


# Inside build_core_tool_closures, near get_user_camera_inp / Generate_video:
    @log_tool_execution
    async def execute_windows_or_android_command(
        instructions: Annotated[str, "Command in plain English to execute on the user's computer or mobile device"],
        os_to_control: Annotated[str, "The OS to control: 'windows', 'linux', 'macos', or 'android'"],
    ) -> str:
        """Execute a command on any desktop/Android device with re-entry
        guard.  Uses pyautogui for cross-platform GUI automation."""
        command_key = f"windows_command_{user_id}_{prompt_id}"
        with _exec_active_tools_lock:
            if command_key in _exec_active_tools and _exec_active_tools[command_key]['active']:
                return f"A Windows command is already being executed in your device. Please wait for it to complete."
            _exec_active_tools[command_key] = {'active': True, 'started_at': time.time()}
        try:
            # ... full body from reuse_recipe.py:1771-... here, parameterized on
            # ctx['recipes_cache'] (created when this closure is built) ...
            pass
        finally:
            with _exec_active_tools_lock:
                _exec_active_tools[command_key]['active'] = False
        return ...

    tools.append((
        "execute_windows_or_android_command",
        "Execute a command on any desktop or Android device.  Uses pyautogui for cross-platform GUI automation, with re-entry guard.",
        execute_windows_or_android_command,
    ))
```

The `recipes_cache` plumbing requires passing it through `ctx` —
`reuse_recipe.py`'s `_tool_ctx` already builds it from local scope, so we add
`'recipes_cache': recipes` (or `'vlm_recipes_cache': vlm_recipes` for create).
Need to read create's `_tool_ctx` build at line 887-907 to confirm.

**Risk:** the impl is non-trivial (lines 1746-2000ish of reuse_recipe.py); needs
careful extraction. Higher implementation risk than 3.1/3.2.

**Mitigation:** unit test the canonical with a mocked cache, then live-verify
in both create + reuse modes.

#### 3.4 / 3.5 / 3.6 `delegate_to_specialist`, `share_context_with_agents`, `get_shared_context`

Need a side-by-side read of create_recipe.py:1659-1704 and reuse_recipe.py:2449-2481
to determine drift. The audit hasn't covered these in detail. Brief diff-read
pass (~30 min) before implementing each.

**Approach for each:** same as 3.2/3.3 — canonical to core_AT, inline defs in
both files become delegates.

**Time for all of 3.1-3.6:** 2-3 hours.

**Commits (one per pair):**
- `refactor(autogen): unify get_user_details impl through core.agent_tools (#510)`
- `refactor(autogen): move consult_expert to core.agent_tools (#510)`
- `refactor(autogen): unify execute_windows_or_android_command with re-entry guard (#510)`
- `refactor(autogen): unify delegate_to_specialist through core.agent_tools (#510)`
- `refactor(autogen): unify share_context_with_agents through core.agent_tools (#510)`
- `refactor(autogen): unify get_shared_context through core.agent_tools (#510)`

---

### Phase 4 — Recipe-portability aliases (no removal, behavior consistency)

#### 4.1 `txt2img` alias → same impl as `text_2_image`

**Problem:** Main flow registers `txt2img` (reuse:1242) with cloud-Rasa body.
Time/Visual flows register `text_2_image` (core_AT:108) with `helper_fun.txt2img`.
Two names, two impls, two flows.

**Solution:** Make BOTH names register the SAME canonical impl (core_AT's
`helper_fun.txt2img` — local-first, sovereignty per CLAUDE.md).

1. **core/agent_tools.py:** after the `text_2_image` tuple at line 112, append
   alias:
```python
    # Alias — reuse_recipe.py main flow LLM prompts reference this name (#510)
    tools.append((
        "txt2img",
        "Text to image Creator (alias of text_2_image)",
        text_2_image,
    ))
```

2. **reuse_recipe.py:1242-1258 (`txt2img` inline AH-stack):** REPLACE body with
   delegate to the core_AT closure. Keep the decorator stack (so it remains
   registered on the main pair), but the body becomes:
```python
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Text to image Creator")
    @log_tool_execution
    def txt2img(text: Annotated[str, "Text to create image"]) -> str:
        # Delegate to canonical core.agent_tools impl (helper_fun.txt2img — local-first).
        return helper_fun.txt2img(text)
```

Effect: main flow's `txt2img` and time/visual flows' `text_2_image` + `txt2img`
all execute `helper_fun.txt2img(text)`.

#### 4.2 `img2txt` — same shape as 4.1

1. **core/agent_tools.py:** after `get_text_from_image` tuple at line 512,
   append:
```python
    # Alias — reuse_recipe.py main flow LLM prompts reference this name (#510)
    tools.append((
        "img2txt",
        "Image to Text/Question Answering from image (alias of get_text_from_image)",
        img2txt,
    ))
```

2. **reuse_recipe.py:1255 (inline `img2txt`):** REPLACE body with delegate to
   the core_AT `img2txt` impl (the SSRF-validated Qwen/cloud LLaVA version):
```python
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Image to Text/Question Answering from image")
    @log_tool_execution
    def img2txt(
        image_url: Annotated[str, "image url of which you want text"],
        text: Annotated[str, "the details you want from image"] = 'Describe the Images & Text data in this image in detail',
    ) -> str:
        # Delegate to canonical core.agent_tools impl (SSRF-validated).
        # We need to call the canonical via the core_tools list.
        _impl = next((f for n, _, f in core_tools if n == 'get_text_from_image'), None)
        if _impl is None:
            return "Image-to-text canonical not registered"
        return _impl(image_url, text)
```

Risk: `core_tools` is built earlier in reuse_recipe.py:2261 (after
create_agents_for_user runs). The inline AH-stack at line 1255 fires
DURING create_agents_for_user before line 2261. So `core_tools` is not yet
available at the time of decoration.

**Mitigation:** delegate to `helper_fun.image_to_text(...)` directly OR move the
core_AT closure to a module-level function importable from core_AT. Need to
check `helper_fun` for the right entry point.

#### 4.3 `get_user_camera_inp_by_mins` — currently reuse-only

Move closure to core/agent_tools.py so create-mode recipes can reference it.
Audit Section 5 confirms it's not in core_AT or channels_AT today.

#### 4.4 `register_visual_watcher`, `create_new_agent`, `update_persona`

Same pattern: closure moves to core/agent_tools.py (where applicable). Note that
`update_persona` is HA-stack (assistant.register_for_llm) — different from the
AH-stack convention. Per user's "minimal" directive, leave the HA-stack as-is
but route the body through the canonical.

**Time:** 1 hr.

**Commit:** `feat(autogen): txt2img/img2txt aliases for main-flow recipe portability (#510)`

---

### Phase 5 — Capitalization variants (no-op per audit + user directive)

Audit Section 3 confirms NO autogen-flow system prompt references the
`Observe_User_Experience` / `Self_Critique_And_Enhance` / `Suggest_Share_Worthy_Content`
capitalized variants. They're orphaned-from-prompts registrations (LangChain Tool
name parity).

**Action:** No prompt edits. Drift-guard test (Phase 6) will catch any future
prompt that mentions a capitalized variant.

**Time:** 0.

---

### Phase 6 — Drift-guard tests

New test file: `tests/unit/test_tool_registration_parity.py`.

| Test | Asserts |
|---|---|
| `test_decorator_order_registered_function_is_wrapped` (Phase 0) | `assistant._function_map[name]` has `__wrapped__` for a probe stack |
| `test_persona_registry_single_writer` (Phase 1.2) | AST scan: `agents_session = TTLCache(...)`, `agents_roles = TTLCache(...)`, `chat_joinees = TTLCache(...)` appear in EXACTLY ONE file (`core/persona_registry.py`) |
| `test_get_data_from_memory_aliases_get_data_by_key` (Phase 1.1) | `build_core_tool_closures(ctx)` returns both names; same closure identity |
| `test_send_message_to_roles_registered_in_both_flows` (Phase 1.2) | Mock-boot create + reuse; both have the tool in `_function_map` |
| `test_send_message_to_roles_broadcasts_with_role_match` (Phase 1.2) | Populate agents_session, invoke, assert `client.publish` payload |
| `test_send_message_to_roles_helpful_error_when_no_personas` (Phase 1.2) | Empty registry → return string contains "No personas registered" |
| `test_register_dual_name_matches_func_name_or_is_aliased` (Phase 2.1 + general) | For every `register_dual(...)` 4th arg in create+reuse, equals `func.__name__` OR in `ALLOWLIST` |
| `test_no_phantom_prompt_names` (Phase 1.1, 1.2, 2.1) | AST scan every `system_message=` in create+reuse; tokenize tool-name-shaped strings; assert each token resolves to a real registration |
| `test_txt2img_aliases_text_2_image` (Phase 4.1) | `build_core_tool_closures(ctx)` returns both names; same closure |
| `test_img2txt_aliases_get_text_from_image` (Phase 4.2) | Same shape |
| `test_canonical_consult_expert_in_core_at` (Phase 3.2) | After unification: `build_core_tool_closures(ctx)` returns `consult_expert` |
| `test_inline_register_dual_delegates_match_canonical` (Phase 3.x) | For the 6 unified names, the inline def's body source AST matches a delegate pattern (single return calling core_AT helper) |

**Time:** 1.5 hr.

**Commit:** `test(autogen): drift-guard tests for tool-name registration parity (#510)`

---

### Phase 7 — Live verification (per `feedback_test_what_we_ship.md`)

1. **Build smoke:** `python -c "import core.agent_tools, core.persona_registry, reuse_recipe, create_recipe"` — no ImportError.
2. **cx_Freeze tracer:** `python scripts/build.py` smoke build (optional — heavyweight, can defer to a separate Phase 8 commit if time-bound).
3. **Dev-mode probe:** `python main.py --port 5001`.
4. **Create-mode trigger:** POST `/chat` with text that triggers recipe creation
   exercising each canonical name (`text_2_image`, `Generate_video`,
   `save_data_in_memory`, `consult_expert`, `execute_windows_or_android_command`).
   Verify in `langchain.log` + `gui_app.log`:
   - No "function not found" responses
   - `[TOOL] {name} called` log lines fire (proving decorator-order fix works)
   - `publish_chat_stage('tool_call', text=…)` SSE event observable via DevTools
5. **Reuse-mode trigger:** Replay a saved recipe (`prompts/fw4_1_0_*.json`).
   Verify same signals.
6. **Phantom-tool probe:** Send a prompt that causes Helper to emit
   `get_data_from_memory` and `send_message_to_roles`. Verify each resolves.
7. **Alias probe:** Force a recipe authored from reuse-mode that uses `txt2img`.
   Save the recipe to disk. Replay in create-mode. Verify it resolves.

Capture log excerpts as evidence. Mark each task `completed` only when its
live-verify signal is observed.

**Time:** 30 min.

---

## §3 — Commit order

1. **Phase 0** — `fix(reuse_recipe): @log_tool_execution must be innermost decorator (#509 followup)`
2. **Phase 1.1 + 2.1** — `fix(autogen): alias get_data_from_memory + rename Connect_to_main_agent → connect_time_main (#510)` (combined since both are tiny)
3. **Phase 1.2** — `feat(autogen): restore send_message_to_roles multi-persona router via core/persona_registry (#510)`
4. **Phase 3.1** — `refactor(autogen): unify get_user_details impl through core.agent_tools (#510)`
5. **Phase 3.2** — `refactor(autogen): move consult_expert to core.agent_tools (#510)`
6. **Phase 3.3** — `refactor(autogen): unify execute_windows_or_android_command with re-entry guard (#510)`
7. **Phase 3.4-3.6** — `refactor(autogen): unify delegate_to_specialist + share/get_shared_context through core.agent_tools (#510)`
8. **Phase 4.1+4.2** — `feat(autogen): txt2img/img2txt aliases for main-flow recipe portability (#510)`
9. **Phase 4.3-4.4** — `feat(autogen): move register_visual_watcher + create_new_agent + get_user_camera_inp_by_mins to core.agent_tools (#510)`
10. **Phase 6** — `test(autogen): drift-guard tests for tool-name registration parity (#510)`
11. **Phase 7** — live-verify; close task #510 + #511.

Each commit ≤ ~200 LOC change. Run import smoke + test subset between commits.
No `Co-Authored-By: Claude`. No `--no-verify`.

---

## §4 — Risk register

| Risk | Mitigation |
|---|---|
| Phase 0 fix breaks an existing test that relied on the bug | Run full test suite after Phase 0; any failure is a real bug that was masked |
| Circular import core.persona_registry ↔ reuse_recipe | Lazy import of `client` inside `_send_message_to_roles_impl` (not at module load) |
| Phase 3.3 extraction breaks the `_active_tools_lock` semantics | Unit test with concurrent invocation mock before merging |
| Recipe in `prompts/*.json` references an old name we silently rename | Per user rule: recipe values may break; the JSON schema stays valid (rule was relaxed mid-session) |
| cx_Freeze tracer misses `core/persona_registry` | Static `from core.persona_registry import ...` ensures detection; Phase 7 build smoke validates |
| 18 conceptually-same tools under different names confuse the LLM mid-recipe | LLM picks whichever was advertised in its system prompt; after Phase 4 aliases all names execute the same impl |
| `update_persona` HA-stack stays special-cased | Drift-guard test allowlists it explicitly |

---

## §5 — Total estimate

| Phase | Time |
|---|---|
| 0 | 30 min |
| 1.1 | 15 min |
| 1.2 | 1.5 hr |
| 2.1 | 5 min |
| 3.1-3.6 | 2.5-3 hr |
| 4.1-4.4 | 1 hr |
| 5 | 0 |
| 6 | 1.5 hr |
| 7 | 30 min |

**Total: ~7 hours.** Atomic, gated, recipe-replay-best-effort, no silent gulps.

---

## §6 — Open questions before kickoff (none — user has already answered all)

| Question | User's answer |
|---|---|
| Phase 1.2 stub variant? | "no stub" — gather requirements populates personas before autogen brainstorm |
| Phase 1.2 architecture? | Canonical `core/persona_registry.py`, not cross-file imports |
| Phase 3 impl drift? | "check the best implementation if there are implementation drift" → unify per-pair, canonical wins |
| Phase 5 prompts? | "just change function names in prompts so changes are minimalistic" — and audit confirms no prompts mention the cap variants → no edits needed |
| Recipe values? | "the json structure shd not be chsnged so that the prompts are compatible, values nned not be respected" → recipe values may break |
| Never remove a tool? | Yes — "never remove a registerdd tool find an existing iomplementation which matches that canonically and add em for phantoom tool" |

Ready to start Phase 0 on user "go".
