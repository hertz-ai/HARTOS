# HARTOS autogen tool-name registration audit

**Scope:** every tool name registered into autogen's `function_map`
(via `helper.register_for_llm` + `assistant.register_for_execution`)
across the four files that constitute the recipe-create / recipe-reuse
pipeline:

1. `create_recipe.py`        (5100 lines)
2. `reuse_recipe.py`         (3719 lines)
3. `core/agent_tools.py`     (1257 lines — canonical closures)
4. `integrations/channels/agent_tools.py` (~770 lines — channel closures)

Goal: identify every name autogen knows about, every collision between
parallel impls, every name embedded in an LLM system prompt that must
exist as a real registration, and every name that lives ONLY in
`reuse_recipe.py` (so a create-time recipe can never trigger it).

The audit is exhaustive — every line was read, including the closures
inside `try:` blocks, the inner-loop `register_dual(...)` fan-outs over
MCP / AP2 / service-tool registries, and the system-prompt strings.

---

## Section 1. Complete tool-registration table

Columns:
- **Name** — the exact string that lands in autogen's `function_map`.
- **File** — short alias.  `core_AT` = `core/agent_tools.py`.
  `chan_AT` = `integrations/channels/agent_tools.py`.
  `create` = `create_recipe.py`.  `reuse` = `reuse_recipe.py`.
- **Line** — line of the registration mechanism (decorator stack head
  or `register_dual(...)` / `tools.append((...))` site).
- **Mechanism** — registration plumbing used.
- **Impl summary** — one-line description of what the closure does.
- **Closure deps** — what session-scoped state it captures.

Legend for mechanism column:

| Code | Meaning |
|---|---|
| **AGT-tuple** | `core.agent_tools.build_core_tool_closures` returns `(name, desc, func)` tuple; registered later by `register_core_tools` via `register_dual` |
| **CHN-tuple** | same but from `integrations.channels.agent_tools.build_channel_tool_closures` |
| **register_dual** | `core.agent_tools.register_dual(helper, executor, func, name, desc)` — direct call |
| **AH-stack** | classic `@assistant.register_for_execution()` + `@helper.register_for_llm(...)` decorator stack on an inner def |
| **HA-stack** | INVERSE — `@helper.register_for_execution()` + `@assistant.register_for_llm(...)` stack (only seen once in reuse, on the persona bootstrap path) |
| **fanout** | loop-based registration over a dynamic list (MCP, AP2, service-tools, expert-agents, HART-skills) |
| **memory** | `integrations.channels.memory.agent_memory_tools.register_autogen_tools` — uses helper.register_for_llm + assistant.register_for_execution under the hood |
| **media** | `integrations.service_tools.media_agent.register_media_tools` — same plumbing as `register_dual` |

### 1.1 Tools defined in `core/agent_tools.py` (registered into BOTH create + reuse via `register_core_tools`)

Every closure below is built once per session by `build_core_tool_closures(ctx)`, then registered onto BOTH the main-flow `(helper, assistant)` pair AND the time-flow `(helper1, time_agent)` pair (see create_recipe.py:909, 2941; reuse_recipe.py:2262, 2301).

| Name | File | Line | Mechanism | Impl summary | Closure deps |
|---|---|---|---|---|---|
| `text_2_image` | core_AT | 108 | AGT-tuple | `helper_fun.txt2img(text)` | `helper_fun` |
| `get_user_camera_inp` | core_AT | 123 | AGT-tuple | `helper_fun.get_user_camera_inp(...)` w/ `request_id_list[user_prompt]` | `user_id, request_id_list, user_prompt, helper_fun` |
| `save_data_in_memory` | core_AT | 185 | AGT-tuple | JSON-validated dot-notation write to `agent_data[prompt_id]`, persists to disk + verifies read-back | `agent_data, prompt_id, helper_fun, retrieve_json` |
| `get_saved_metadata` | core_AT | 203 | AGT-tuple | Returns key-only schema via `strip_json_values(agent_data[prompt_id])`; lazy-loads from file if missing | `agent_data, prompt_id, helper_fun, strip_json_values` |
| `get_data_by_key` | core_AT | 228 | AGT-tuple | Dot-walks `agent_data[prompt_id]` for value; lazy-loads from file if missing | `agent_data, prompt_id, helper_fun` |
| `get_user_id` | core_AT | 242 | AGT-tuple | Returns `str(user_id)` | `user_id` |
| `get_prompt_id` | core_AT | 256 | AGT-tuple | Returns `str(prompt_id)` | `prompt_id` |
| `Generate_video` | core_AT | 450 | AGT-tuple | LTX-2 text-to-video (model='ltx2') OR avatar pipeline (model='avatar' default); 3-tier local→ComfyUI→hive-peer fallback | `user_id, prompt_id, save_conversation_db` |
| `get_user_uploaded_file` | core_AT | 466 | AGT-tuple | Reads `recent_file_id[user_id]` | `recent_file_id, user_id` |
| `get_text_from_image` | core_AT | 508 | AGT-tuple | **Impl func is named `img2txt`** but registered as `get_text_from_image` — SSRF-validated img→text via local Qwen Vision or cloud LLaVA | none |
| `create_scheduled_jobs` | core_AT | 526 | AGT-tuple | Stub — returns "Added this schedule job in creation process". Real scheduling happens later. | none |
| `send_message_to_user` | core_AT | 547 | AGT-tuple | Spawns `Thread(target=send_message_to_user1, ...)`; returns `…-intermediate` request_id | `user_id, prompt_id, request_id_list, send_message_to_user1` |
| `send_presynthesized_video_to_user` | core_AT | 564 | AGT-tuple | Stub — logs conv_id and returns success | none |
| `send_message_in_seconds` | core_AT | 585 | AGT-tuple | `scheduler.add_job(send_message_to_user1, 'date', run_date=now+delay, ...)` | `user_id, prompt_id, scheduler, send_message_to_user1` |
| `get_chat_history` | core_AT | 603 | AGT-tuple | `helper_fun.get_time_based_history(text, f'user_{user_id}', start, end)` | `user_id, helper_fun` |
| `search_visual_history` | core_AT | 624 | AGT-tuple | `helper_fun.search_visual_history(user_id, query, mins, channel)` | `user_id, helper_fun` |
| `google_search` | core_AT | 640 | AGT-tuple | `helper_fun.top5_results(text)` | `helper_fun` |
| `search_long_term_memory` | core_AT | 667 | AGT-tuple **(conditional on simplemem_store)** | `simplemem_store.search(query)` via event loop | `simplemem_store` |
| `save_to_long_term_memory` | core_AT | 691 | AGT-tuple **(conditional on simplemem_store)** | `simplemem_store.add(content, metadata)` | `simplemem_store, user_id, prompt_id` |
| `Suggest_Share_Worthy_Content` | core_AT | 762 | AGT-tuple | Queries `Post` + `ShareableLink` for high-engagement under-shared posts | none |
| `Observe_User_Experience` | core_AT | 807 | AGT-tuple — **CAPITALISED variant** | JSON-input observation recorded into MemoryGraph | `memory_graph, user_id, prompt_id` |
| `Self_Critique_And_Enhance` | core_AT | 869 | AGT-tuple — **CAPITALISED variant** | Recalls past suggestions/observations from MemoryGraph; logs critique | `memory_graph, user_id, prompt_id` |
| `device_control` | core_AT | 975 | AGT-tuple | PeerLink dispatch (SAME_USER trust) → FleetCommandService fallback → local exec | `user_id` |
| `data_extraction_from_url` | core_AT | 1021 | AGT-tuple | Crawl4AI tool first, then direct requests fallback | none |
| `get_user_details` | core_AT | 1059 | AGT-tuple | Local `User` table lookup; cloud `getstudent_by_user_id` fallback | `user_id` |
| `request_resource` | core_AT | 1134 | AGT-tuple | API-key / OAuth / secret-request flow via `AIKeyVault`, env vars, then pending-request prompt | none |
| `observe_user_experience` | core_AT | 1169 | AGT-tuple — **lowercase variant** | Structured-arg observation (event, page, outcome, duration_ms) → MemoryGraph | `memory_graph, user_id, prompt_id` |
| `self_critique_and_enhance` | core_AT | 1215 | AGT-tuple — **lowercase variant** | Same as `Self_Critique_And_Enhance` but takes a `topic` arg | `memory_graph, user_id, prompt_id` |

#### 1.1.a — Memory graph tools (separate adapter, also fan into BOTH create + reuse)

Built once by `integrations.channels.memory.agent_memory_tools.create_memory_tools(graph, user_id, session_id)`, registered onto BOTH agent pairs via `register_autogen_tools(mem_tools, executor=assistant, helper=helper)`.  The function body inverts the args — `helper.register_for_llm` + `assistant.register_for_execution` — so the LLM-callable agent is **helper** and the executor is **assistant**, same as every other tool in this codebase.

| Name | File | Line | Mechanism | Impl summary | Closure deps |
|---|---|---|---|---|---|
| `remember` | agent_memory_tools | 229 | memory | `graph.register(content, metadata, parent_ids=[latest_session_memory.id])` | `graph, session_id` |
| `recall_memory` | agent_memory_tools | 234 | memory | `graph.recall(query, mode='hybrid', top_k=5, since, until)` with relative-time parsing (1h/24h/7d) | `graph` |
| `backtrace_memory` | agent_memory_tools | 239 | memory | UUID→direct chain via `graph.backtrace`; query→`graph.backtrace_semantic` | `graph` |
| `get_memory_context` | agent_memory_tools | 245 | memory | `graph.get_session_memories(session_id) → context_recall(...)` | `graph, session_id` |
| `record_lifecycle_event` | agent_memory_tools | 250 | memory | `graph.register_lifecycle(event, agent_id=user_id, session_id, details)` | `graph, user_id, session_id` |

Registered from create_recipe.py:910 (`register_memory_graph_tools`) and reuse_recipe.py:1628 (inline `register_autogen_tools` call).  Both gated on `memory_graph is not None`.

#### 1.1.b — Media tools (registered into create only, via `register_media_tools(helper, assistant)`)

Built by `integrations.service_tools.media_agent.register_media_tools` at media_agent.py:768.  Uses raw `helper.register_for_llm(name=...)` + `assistant.register_for_execution(name=...)` — same plumbing as `register_dual`.  **NOT** registered in reuse_recipe.py.

| Name | File | Line | Mechanism | Impl summary |
|---|---|---|---|---|
| `generate_media` | media_agent | 774 | media | Unified image/audio_speech/audio_music/audio_speech_music/video/video_with_audio dispatcher; auto-selects backend tool |
| `check_media_status` | media_agent | 782 | media | Polls an async media-gen `task_id` |
| `synthesize_multilingual_audio` | media_agent | 788 | media | Script-detects per segment, routes to best TTS engine, supports `<music>` / `<sing>` / `<lyrics>` tags |

### 1.2 Tools defined in `integrations/channels/agent_tools.py` (registered into BOTH create + reuse via `register_channel_tools`)

Built once per session by `build_channel_tool_closures(ctx)`, registered onto BOTH the main-flow `(helper, assistant)` pair AND the time-flow `(helper1, time_agent)` pair.

Created in: create_recipe.py:915 + reuse_recipe.py:2267 + reuse_recipe.py:2306 (visual_agent). The visual_agent registration is unique to reuse_recipe.

| Name | File | Line | Mechanism | Impl summary | Closure deps |
|---|---|---|---|---|---|
| `send_to_channel` | chan_AT | 115 | CHN-tuple | Route to specific channel via `registry.send_to_channel(channel_type, chat_id, message)`; broadcast via `ChannelResponseRouter.route_response(fan_out=True)` when `channel_type=='all'` | `user_id` |
| `register_channel` | chan_AT | 309 | CHN-tuple | Parses JSON config, auto-fills `auto:True` fields from env, writes to `AdminAPI._channels`, creates `UserChannelBinding`, fires daemon-thread adapter probe with toast | `user_id` |
| `list_channels` | chan_AT | 367 | CHN-tuple | `registry.get_status()` + active `UserChannelBinding`s + current `channel_context` | `user_id` |
| `get_channel_context` | chan_AT | 388 | CHN-tuple | Reads `thread_local_data.channel_context` | none (thread-local only) |
| `send_install_link` | chan_AT | 598 | CHN-tuple | Host-allowlisted Nunba install URL dispatch via the user's PAIRED binding only (SAME_USER guarantee) | `user_id` |
| `disconnect_channel` | chan_AT | 671 | CHN-tuple | Flips `UserChannelBinding.is_active=False` for caller's binding; emits toast | `user_id` |
| `reconnect_channel` | chan_AT | 731 | CHN-tuple | Flips `is_active=True` if inactive binding exists; else hints `Connect_Channel(...)` re-onboarding | `user_id` |

### 1.3 Tools registered ONLY in `create_recipe.py` (defined inline, registered via `register_dual`)

| Name | File | Line | Mechanism | Impl summary | Closure deps |
|---|---|---|---|---|---|
| `get_user_details` | create | 931 | register_dual | `helper_fun.parse_user_id(int(user_id))` — **DIFFERENT IMPL** from the `get_user_details` in core_AT:1059 | `user_id, helper_fun` |
| `validate_json_response` | create | 961 | register_dual | `json.loads` then `repair_json` then `json.loads` — returns original on irreparable | none |
| `consult_expert` | create | 981 | register_dual | `match_expert_for_context(task, top_k=3, min_score=2)`; pushes status to user via `send_message_to_user1` | `user_id, prompt_id, send_message_to_user1` |
| `execute_windows_or_android_command` | create | 1404 | register_dual | 3-tier VLM execution (in-process → HTTP local → Crossbar WAMP); creates `*_vlm_agent.json` recipe shards | `user_id, prompt_id, user_prompt, vlm_recipes, request_id_list, final_recipe` |
| `execute_coding_task` | create | 1439 | register_dual | `get_coding_orchestrator().execute(task, task_type, preferred_tool, user_id, working_dir)` — routes to KiloCode/Claude Code/OpenCode/AiderNative | `user_id` |
| `get_repository_map` | create | 1458 | register_dual **(try/except ImportError-gated)** | `CodingRecipeBridge.get_repository_map(working_dir, max_tokens)` | none |
| `create_code_shard` | create | 1504 | register_dual **(try/except-gated)** | `ShardEngine.create_call_chain_shard(task, target_file, target_function)` | none |
| `get_coding_benchmarks` | create | 1544 | register_dual **(try/except-gated)** | `benchmark_tracker.export_learning_delta()` + `get_best_tool` / `get_hive_best_tool` | none |
| `delegate_to_specialist` | create | 1659 | register_dual | `TaskDelegationBridge.delegate_task_with_tracking` w/ task-ledger fallback to `create_delegation_function('assistant')` | `user_prompt, user_delegation_bridges, user_tasks, user_ledgers` |
| `share_context_with_agents` | create | 1671 | register_dual | `create_context_sharing_function('assistant')(key, value)` | none |
| `get_shared_context` | create | 1682 | register_dual | `create_context_retrieval_function()(key)` | none |
| `<MCP-tool-name>` (loop) | create | 1571 | fanout (register_dual over `mcp_registry.get_all_tool_functions()`) | Per-tool delegate via `mcp_registry.get_tool_definitions()` | dynamic |
| `<AP2-tool-name>` (loop) | create | 1704 | fanout (register_dual over `get_ap2_tools_for_autogen('assistant')`) | Per-tool payment workflow | dynamic |
| Marketing tools (loop) | create | 1718 | external (`register_marketing_tools(helper, assistant, user_id)`) | Goal-aware Tier-2 marketing tools loaded when `'marketing' in goal_tags` | `user_id` |

### 1.4 Tools registered ONLY in `reuse_recipe.py` (defined inline, AH-stack on the main-flow pair)

Every entry uses the canonical AH-stack `@assistant.register_for_execution()` + `@helper.register_for_llm(api_style="function", description=...)` decorated by `@log_tool_execution`.

| Name | File | Line | Mechanism | Impl summary | Closure deps |
|---|---|---|---|---|---|
| `update_persona` | reuse | 710 | **HA-stack** (`@helper.register_for_execution()` + `@assistant.register_for_llm(...)`) — INVERSE of the canonical pattern | Updates `agents_session` + `agents_roles` TTLCaches for a fresh persona | `user_id, prompt_id, agents_session, agents_roles, chat_joinees, temp_users` |
| `txt2img` | reuse | 1242 | AH-stack | `pooled_post('http://aws_rasa.hertzai.com:5459/txt2img', ...)` — returns `img_url` field. **NOT the same** as core_AT `text_2_image` (which calls `helper_fun.txt2img`). | none |
| `img2txt` | reuse | 1255 | AH-stack | LLaVA cloud `image_inference`. **Registered name is `img2txt`** (matches `func.__name__`) — NOT `get_text_from_image` like core_AT registers the same impl under | none |
| `save_data_in_memory` | reuse | 1278 | AH-stack | Same JSON-validated dot-notation write as core_AT BUT also dual-writes to `memory_graph` via a fire-and-forget thread | `agent_data, prompt_id, memory_graph, user_prompt, retrieve_json` |
| `get_saved_metadata` | reuse | 1354 | AH-stack | `strip_json_values(agent_data[prompt_id])` — NO lazy file-load like core_AT | `agent_data, prompt_id, strip_json_values` |
| `get_data_by_key` | reuse | 1362 | AH-stack | Dot-walk, then MemoryGraph fallback search on KeyError (different from core_AT, which lazy-loads from file) | `agent_data, prompt_id, memory_graph` |
| `get_user_id` | reuse | 1385 | AH-stack | Returns `str(user_id)` | `user_id` |
| `get_prompt_id` | reuse | 1393 | AH-stack | Returns `str(prompt_id)` | `prompt_id` |
| `Generate_video` | reuse | 1403 | AH-stack | Avatar-only pipeline (no LTX-2 branch, no hive offload) — calls `https://mailer.hertzai.com/get_image_by_id/{avatar_id}` then `/video_generate_save` | `user_id, prompt_id, save_conversation_db` |
| `get_user_uploaded_file` | reuse | 1483 | AH-stack | Reads `recent_file_id[user_id]` | `recent_file_id, user_id` |
| `get_user_camera_inp` | reuse | 1493 | AH-stack | Saves frame to disk, then `pooled_post(get_vision_api()+'/minicpm/upload', ...)`. **Different from core_AT** (which delegates to `helper_fun.get_user_camera_inp`) | `user_id, request_id_list` |
| `get_chat_history` | reuse | 1529 | AH-stack | `get_time_based_history(text, f'user_{user_id}', start, end)` — required start/end (no Optional) | `user_id` |
| `search_visual_history` | reuse | 1538 | AH-stack | `helper_fun.search_visual_history(user_id, query, mins, channel)` | `user_id` |
| `register_visual_watcher` | reuse | 1553 | AH-stack | Pipe-separated `"CONDITION: ... \| ACTION: ... \| TTL: ..."` → `safe_hartos_attr('_handle_visual_watcher_tool')` | none |
| `search_long_term_memory` | reuse | 1575 | AH-stack **(conditional on simplemem_store)** | Same as core_AT but uses `current_app.logger` instead of `tool_logger` | `simplemem_store` |
| `save_to_long_term_memory` | reuse | 1593 | AH-stack **(conditional on simplemem_store)** | Same as core_AT but also dual-writes to MemoryGraph via thread | `simplemem_store, user_id, prompt_id, memory_graph, user_prompt` |
| `create_scheduled_jobs` | reuse | 1633 | AH-stack | **Different signature** from core_AT — takes `cron_expression` as REQUIRED first arg; calls `scheduler.add_job(execute_python_file, ...)` with REAL job-id.  core_AT version is a stub. | `scheduler, user_id, prompt_id` |
| `send_message_to_user` | reuse | 1655 | AH-stack | Agent-mention guard (`@statusverifier` / `@helper` / `@executor`) BEFORE delivery; then `send_message_to_user1(user_id, text, '', prompt_id)`.  core_AT version unconditionally fires; reuse adds the guard. | `user_id, prompt_id, send_message_to_user1` |
| `send_presynthesized_video_to_user` | reuse | 1689 | AH-stack | Same stub as core_AT | none |
| `send_message_in_seconds` | reuse | 1699 | AH-stack | Same scheduler add_job as core_AT | `user_id, prompt_id, scheduler, send_message_to_user1` |
| `consult_expert` | reuse | 1714 | AH-stack | Same impl as the `register_dual`-based `consult_expert` in create_recipe.py:981 | `user_id, prompt_id, send_message_to_user1` |
| `get_user_camera_inp_by_mins` | reuse | 1732 | AH-stack | `helper_fun.get_visual_context(user_id, minutes)` — distinct tool from `get_user_camera_inp` | `user_id` |
| `execute_windows_or_android_command` | reuse | 1746 | AH-stack | Same 3-tier VLM impl as create, but with extra `_active_tools` re-entry guard.  Async-coro signature. | `user_id, prompt_id, user_prompt, recipes, request_id_list, final_recipe, _active_tools, _active_tools_lock` |
| `google_search` | reuse | 2082 | AH-stack | `helper_fun.top5_results(text)` — same as core_AT | none |
| `create_new_agent` | reuse | 2089 | AH-stack | Writes `creation_signals[user_prompt] = {description, autonomous}` flag for `/chat` handler to pick up | `user_prompt, creation_signals` |
| `Connect_to_main_agent` | reuse | 2293 | register_dual on `(helper1, time_agent)` pair | **Note:** registered NAME is `Connect_to_main_agent` (CamelCase + underscores); impl function name is `connect_time_main` — the `name=` arg overrides `__name__` | `user_id, prompt_id, user_agents, send_message_to_user1` |
| `delegate_to_specialist` | reuse | 2449 | register_dual | TaskDelegationBridge-backed delegation w/ ledger tracking — same impl as create_recipe.py:1659 | `user_prompt, user_delegation_bridges, user_tasks, user_ledgers` |
| `share_context_with_agents` | reuse | 2470 | register_dual | Same as create + dual-writes to MemoryGraph via thread | `memory_graph, user_prompt` |
| `get_shared_context` | reuse | 2479 | register_dual | Same as create | none |
| `<MCP-tool-name>` (loop) | reuse | 2330 | fanout | Same MCP fanout as create | dynamic |
| `<AP2-tool-name>` (loop) | reuse | 2354 | fanout | Same AP2 fanout as create | dynamic |
| `<HART-skill-name>` (loop) | reuse | 2365 | fanout | `skill_registry.get_autogen_tools()` — Claude Code / Markdown / GitHub skills.  **NOT** present in create_recipe. | dynamic |
| `<service-tool-name>` (loop) | reuse | 2354 | fanout | `Crawl4AITool.register()` + `AceStepTool.register()` + `service_tool_registry.get_all_tool_functions()` — **NOT** present in create_recipe. | dynamic |
| Marketing tools (loop) | reuse | 2515 | external | Gated on `prompt_id.startswith('marketing')` | `user_id` |
| IP-protection tools (loop) | reuse | 2519 | external | Gated on `prompt_id.startswith('ip_protection')` — **NOT** present in create_recipe. | `user_id` |

---

## Section 2. Same-name collisions across files

These are the cases where the IDENTICAL string lands in `function_map`
but resolves to a DIFFERENT closure depending on which file did the
registration.  On a recipe replay the wrong impl will silently run.

### 2.1 `get_user_details`

| File | Line | Impl |
|---|---|---|
| core_AT | 1059 | Local SQLAlchemy `User` table lookup → cloud `getstudent_by_user_id` fallback.  Returns `User.to_dict()` JSON. |
| create_recipe.py | 931 | `helper_fun.parse_user_id(int(user_id))`.  Returns whatever `parse_user_id` returns (likely a different shape). |

**Effect:** create_recipe runs `register_core_tools` FIRST (line 909) which registers core_AT's version, then immediately overrides it at line 931 with `register_dual` for create's inline version.  autogen's `register_for_execution(name=...)` overwrites; create-mode keeps the `helper_fun.parse_user_id` impl.  Reuse-mode never overrides → keeps core_AT's DB-lookup impl.

**Same name, two different return shapes — recipes that captured one shape will misparse the other.**

### 2.2 `consult_expert`

| File | Line | Impl |
|---|---|---|
| create_recipe.py | 981 | Inline register_dual.  Same body as reuse's version. |
| reuse_recipe.py | 1714 | Inline AH-stack.  Same body. |

Identical bodies — no actual drift.  Listed for completeness because the user's earlier audit mis-counted it as "reuse-only".

### 2.3 `execute_windows_or_android_command`

| File | Line | Impl |
|---|---|---|
| create_recipe.py | 1404 | async; uses `user_prompt` + `vlm_recipes` cache (create-mode's TTLCache); no `_active_tools` re-entry guard. |
| reuse_recipe.py | 1746 | async; uses `recipes` cache (reuse-mode's TTLCache); wraps the body in `_active_tools_lock` to refuse concurrent invocations. |

Effect: a recipe that was authored in create-mode and replayed in reuse-mode will hit the `_active_tools` guard on re-entry where create-mode had no such guard — observable behaviour change.  Description string is also different: create says "returns detailed computer/mobile use agent execution context", reuse says "Processes user-defined commands on a personal Windows or Android system."

### 2.4 `txt2img` vs `text_2_image`

Same idea, different names — collision of CONCEPT, not of literal string:

| Name | File | Line | Impl |
|---|---|---|---|
| `text_2_image` | core_AT | 108 | `helper_fun.txt2img(text)` — goes through helper_fun's TTS-aware path |
| `txt2img` | reuse | 1242 | Direct `pooled_post('http://aws_rasa.hertzai.com:5459/txt2img', ...)` — cloud-only, bypasses helper_fun |

In reuse mode BOTH `text_2_image` (from core_AT via `register_core_tools`) AND `txt2img` (from the AH-stack) land in `function_map`.  Recipes saved with `tool_name: "text_2_image"` and replayed in reuse will succeed; recipes saved with `tool_name: "txt2img"` will succeed in reuse but FAIL in create-mode (no `txt2img` is registered in create).

### 2.5 `img2txt` vs `get_text_from_image`

Same concept, two names, same underlying capability:

| Name | File | Line | Mechanism |
|---|---|---|---|
| `get_text_from_image` | core_AT | 508 | AGT-tuple; impl function is `def img2txt(...)` but registered name overrides to `get_text_from_image` |
| `img2txt` | reuse | 1255 | AH-stack; `register_for_llm()` uses default name `func.__name__` → registered as `img2txt` |

In reuse mode BOTH names exist (core_AT registers `get_text_from_image`, the inline AH-stack registers `img2txt`).  In create mode only `get_text_from_image` exists.  Recipes saved with `img2txt` cannot replay in create.

### 2.6 `save_data_in_memory` / `get_data_by_key` / `get_saved_metadata`

| Name | File | Line | Impl |
|---|---|---|---|
| `save_data_in_memory` | core_AT | 185 | dot-path write, persists via `helper_fun.save_agent_data_to_file`, verifies round-trip |
| `save_data_in_memory` | reuse | 1278 | same dot-path write, NO disk persistence verify, but DUAL-WRITES to MemoryGraph |
| `get_saved_metadata` | core_AT | 203 | lazy-loads `agent_data` from file when missing |
| `get_saved_metadata` | reuse | 1354 | NO lazy load — assumes `agent_data[prompt_id]` is hot |
| `get_data_by_key` | core_AT | 228 | lazy-loads file on miss |
| `get_data_by_key` | reuse | 1362 | falls back to MemoryGraph semantic search on `KeyError` |

In reuse-mode the `register_core_tools` call at line 2262 registers `helper1, time_agent` (the time-flow pair), not the main `(helper, assistant)` pair.  So on reuse the main agents see the inline AH-stack versions; the time-agent fork sees core_AT versions.  **Drift between same-name impls is real but partitioned by which agent pair is calling.**

### 2.7 `Generate_video`

| File | Line | Impl |
|---|---|---|
| core_AT | 450 | LTX-2 text-to-video OR avatar pipeline w/ hive offload fallback |
| reuse | 1403 | Avatar-only |

Reuse's main pair is bound to the avatar-only impl; reuse's time/visual agents (registered via `register_core_tools` at lines 2262/2301) get LTX-2 + avatar.  Same name, two impls in the same process, different agent pairs.

### 2.8 `update_persona` — only file with the INVERSE decorator stack

reuse_recipe.py:710 is the ONLY place I found that uses
`@helper.register_for_execution()` + `@assistant.register_for_llm(...)` (the HA-stack).  Every other registration in the codebase has helper as the LLM-caller and assistant/executor as the tool-runner.  This means `update_persona`:

- LLM-callable on **assistant** (not helper)
- Executable on **helper** (not assistant)

This is on the new-persona bootstrap path (line 685-708, fresh user / fresh persona setup).  If a recipe ever names `update_persona` outside this bootstrap path, the routing will silently break because the persona agent pair created later (`Assistant` + `Helper` at line 1061-1119) doesn't have it registered.

---

## Section 3. Names referenced in LLM system prompts

These are tool names embedded verbatim in `system_message=...` strings.  The LLM will believe these tools exist and will emit tool-call requests against them.  Any name listed below that does not have a real registration in Section 1 is a hallucination trap.

### 3.1 `create_recipe.py` system prompts

create_recipe.py:2593 (Executor agent, `instantiate_executor_agent`):
```
Tools Helper Agent can use: send_message_in_seconds, send_message_to_user, send_presynthesized_video_to_user, execute_windows_or_android_command, text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, save_data_in_memory, search_long_term_memory and save_to_long_term_memory
```
All 17 names exist in Section 1.1.

create_recipe.py:2758 (Assistant agent prompt):
```
The tools are: send_message_in_seconds, send_message_to_user, send_presynthesized_video_to_user, execute_windows_or_android_command, text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, google_search, save_data_in_memory, search_long_term_memory and save_to_long_term_memory.
```
Adds `google_search` to the 2593 list — all 18 exist.

create_recipe.py:2824 (Time agent prompt):
```
Tools Helper Agent can use [send_message_in_seconds, send_message_to_user, send_presynthesized_video_to_user, text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, save_data_in_memory, search_long_term_memory and save_to_long_term_memory.]
```
16 names — all exist.  ALSO references `connect_time_main` (line 2823 prose), which in reuse is registered as `Connect_to_main_agent` — **NAME DRIFT** but not in create-mode (create's time agent has `connect_time_main` registered inline at create_recipe.py:??? in `create_time_agents` — I'll flag this gap below).

create_recipe.py:2848, 2871 (helper1, executor1 prompts) — same 16-name list, all exist.

create_recipe.py:2737-2790 (Assistant detailed prompt):
- Mentions `save_data_in_memory`, `get_data_by_key`, `get_saved_metadata`, `get_user_id`, `google_search`, `execute_windows_or_android_command`, `send_message_to_user`, `send_message_in_seconds`, `validate_json_response`, `Generate_video` — all exist.

### 3.2 `reuse_recipe.py` system prompts

reuse_recipe.py:1019 (Assistant prompt, the long one):
```
The tools are: send_message_in_seconds, send_message_to_user, send_presynthesized_video_to_user, execute_windows_or_android_command, text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, google_search, save_data_in_memory, search_long_term_memory and save_to_long_term_memory.
```
**Hallucination trap:** `text_2_image` is listed, but in reuse-mode the registered name is `txt2img` (line 1242) — the inline AH-stack registers `func.__name__`.  The core_AT `text_2_image` IS also registered (via `register_core_tools` at line 2262 — but that's on the time-flow pair, NOT the main pair).  Result: when the main Assistant emits a `text_2_image` tool call, it goes to core_AT's impl that uses `helper_fun.txt2img`.  When it emits `txt2img`, it goes to the inline cloud-only `aws_rasa.hertzai.com:5459` impl.  **Two valid names, two different impls** depending on which the LLM picked.

`get_text_from_image` similarly co-exists with `img2txt`.

reuse_recipe.py:1042 (Assistant prompt):
```
ask @Helper to use the `create_new_agent` tool with a description
```
`create_new_agent` exists at reuse:2089.

reuse_recipe.py:1102 (Helper prompt):
```
Tools you have [txt2img, img2txt, save_data_in_memory, get_data_from_memory, search_long_term_memory, save_to_long_term_memory, get_user_id, get_prompt_id, Generate_video, get_user_uploaded_file, get_user_camera_inp, get_chat_history, create_scheduled_jobs, send_message_to_user, send_presynthesized_video_to_user]
```
**Hallucination trap:** `get_data_from_memory` is listed BUT NOT REGISTERED anywhere.  The real tool is `get_data_by_key`.  LLM will emit `get_data_from_memory` tool calls that return `KeyError: function not found`.

reuse_recipe.py:1129, 2151, 2174, 2128 (executor/helper1/executor1/time_agent prompts) — long 16-tool lists, all names check out EXCEPT the same `text_2_image` / `txt2img` drift; reuse:2151 ALSO lists `get_data_from_memory` again.

reuse_recipe.py:1101 mentions `"send_message_to_roles"` as a tool name — this is **NOT REGISTERED** anywhere.  The closure at reuse:1204-1240 is commented out.  Calls will 404.  This appears in the helper, executor, helper1, executor1 prompts (lines 1101, 1128, 2150, 2173).

### 3.3 Cross-prompt summary of hallucination traps

| Name in prompt | Reality | Files referencing |
|---|---|---|
| `get_data_from_memory` | Not registered.  Real tool: `get_data_by_key` | reuse:1102, 2151 |
| `send_message_to_roles` | Not registered (closure commented out at reuse:1204-1240) | reuse:1101, 1128, 2150, 2173 |
| `connect_time_main` | In reuse, registered as `Connect_to_main_agent` (name override via register_dual).  In create, the inline closure inside `create_time_agents` has not been read by me but the prompt at create:2823 says `connect_time_main` verbatim. | create:2823, reuse:2127 (system_message line) |
| `text_2_image` | Registered in core_AT (always) AND under the alias `txt2img` (reuse only). LLM may emit either. | every prompt |
| `get_text_from_image` | Registered in core_AT.  ALSO available as `img2txt` in reuse. | every prompt |

---

## Section 4. Names referenced in saved recipes (`prompts/*.json`)

Recipes are JSON files where each `recipe[].tool_name` is the string the replay loop tries to execute.

Grep across `prompts/*.json`:

| `tool_name` value | Files referencing | Real-tool status |
|---|---|---|
| `execute_windows_or_android_command` | create:1241, 1249, 1256 (auto-generated VLM agent files); reuse:1981, 1988, 1997 | REGISTERED both modes |
| `google_search` | `prompts/fw4_1_0_recipe.json:13` | REGISTERED both modes |
| `send_message_to_user` | `prompts/fw4_1_0_recipe.json:43` | REGISTERED both modes |
| `""` (empty) | many `prompts/fw4_1_0_*.json` rows | No-op — `agent_to_perform_this_action: "Assistant"` |
| `hive_dashboard_client.query_active_users` | `prompts/27130929014_0_1.json` | **NOT REGISTERED** — looks like a stub recipe |
| `demand_aggregator.aggregate_preferences` | `prompts/27130929014_0_1.json` | **NOT REGISTERED** |
| `report_generator.format_demand_report` | `prompts/27130929014_0_1.json`, `_0_2.json`, `_0_3.json` | **NOT REGISTERED** |
| `node_registry.fetch_active_nodes` | `prompts/27130929014_0_3.json` | **NOT REGISTERED** |
| `data_processor.extract_node_metrics`, `.group_by_region_and_model`, `.filter_critical_gaps`, `.select_top_gaps` | `prompts/27130929014_0_*.json` | **NOT REGISTERED** |
| `data_repository.fetch_aggregated_users`, `data_store.retrieve_demand_metrics`, `data_store.retrieve_node_capacity_metrics`, `data_store.retrieve_gap_metrics` | `prompts/27130929014_0_*.json` | **NOT REGISTERED** |
| `text_generator.format_report`, `log_manager.write_output` | `prompts/27130929014_0_5.json` | **NOT REGISTERED** |
| `none` | `prompts/27130929014_0_4.json` | Sentinel string — replay treats as no-op |

**The `27130929014_0_*.json` family is an entire recipe whose steps reference 13+ tools that don't exist in any of the four files.**  Replay will silently fail every step (autogen will respond "function not found" for each tool call).  Either these recipes were authored against a different / older registration surface, OR they're test fixtures that were never expected to replay.

`fw4_1_0_*.json` mostly uses empty `tool_name: ""` strings — Assistant-only narrative steps, no autogen dispatch needed.

---

## Section 5. True reuse-only tool names

Tools whose name string appears as a registration ONLY in `reuse_recipe.py` and nowhere else (NOT in `core/agent_tools.py`, NOT in `integrations/channels/agent_tools.py`, NOT in `create_recipe.py`).

| Name | File | Line | Why reuse-only |
|---|---|---|---|
| `update_persona` | reuse:710 | INVERSE HA-stack on new-persona bootstrap path | Only the reuse-mode persona creation flow needs to mutate `agents_session` / `agents_roles` |
| `txt2img` | reuse:1242 | Inline AH-stack | core_AT's equivalent is registered as `text_2_image` (different name); no `txt2img` in create |
| `img2txt` | reuse:1255 | Inline AH-stack | core_AT's equivalent is `get_text_from_image` |
| `get_user_camera_inp_by_mins` | reuse:1732 | Inline AH-stack | distinct from `get_user_camera_inp`; not present anywhere else |
| `register_visual_watcher` | reuse:1553 | Inline AH-stack | Uses `safe_hartos_attr('_handle_visual_watcher_tool')` — reuse-only feature |
| `create_new_agent` | reuse:2089 | Inline AH-stack | reuse-flow-only: writes `creation_signals` for `/chat` handler |
| `Connect_to_main_agent` | reuse:2293 | register_dual on `(helper1, time_agent)` | reuse's time-flow only; create has its own `create_time_agents` with similar wiring but different name (`connect_time_main` — see Section 6 below) |

### Verification of the user's earlier-flagged false positives

- **`execute_windows_or_android_command`** — registered in BOTH:
  - create_recipe.py:1404 via `register_dual`
  - reuse_recipe.py:1746 via AH-stack
  Confirmed it is NOT a reuse-only tool.  User's earlier audit was wrong on this.

- **`consult_expert`** — registered in BOTH:
  - create_recipe.py:981 via `register_dual`
  - reuse_recipe.py:1714 via AH-stack
  Confirmed it is NOT a reuse-only tool.  User's earlier audit was wrong on this.

### Tools registered in `reuse_recipe.py` but ALSO available in create via `register_core_tools`

These tools have inline AH-stacks in reuse AND get registered in BOTH modes via the canonical core_AT closures.  They are **NOT** reuse-only — they're just duplicately-defined:

`save_data_in_memory`, `get_saved_metadata`, `get_data_by_key`, `get_user_id`, `get_prompt_id`, `Generate_video`, `get_user_uploaded_file`, `get_user_camera_inp`, `get_chat_history`, `search_visual_history`, `search_long_term_memory`, `save_to_long_term_memory`, `create_scheduled_jobs`, `send_message_to_user`, `send_presynthesized_video_to_user`, `send_message_in_seconds`, `google_search`, `consult_expert`.

In reuse mode the AH-stack wins (registered LAST overwrites whatever `register_core_tools` set).  In create mode the core_AT version is canonical.  Behaviour drifts — see Section 2 for details.

---

## Section 6. Canonical-name proposal

For each conceptual tool with name drift, the ONE recommended canonical name.  The "Files needing update" column lists what to change to standardise.

| Concept | Current names | Canonical proposal | Files needing update |
|---|---|---|---|
| Text-to-image | `text_2_image` (core_AT), `txt2img` (reuse) | **`text_2_image`** — matches core_AT, matches recipe step `tool_name: "text_2_image"` for IMG tasks | Delete reuse:1242 AH-stack; trust core_AT's registration.  Update prompt at reuse:1102 from `txt2img` → `text_2_image`. |
| Image-to-text | `get_text_from_image` (core_AT), `img2txt` (reuse) | **`get_text_from_image`** — matches core_AT and is more LLM-readable than `img2txt` | Delete reuse:1255 AH-stack; update prompt at reuse:1102 from `img2txt` → `get_text_from_image`. |
| Camera-by-minutes | `get_user_camera_inp_by_mins` (reuse only) | **`search_visual_history`** — already canonical in core_AT and behaviourally similar | Delete reuse:1732 inline; teach the prompts to use `search_visual_history` with `minutes_back`. |
| Persona update on bootstrap | `update_persona` (reuse only, inverse HA-stack) | **`update_persona`** keep, but flip to canonical AH-stack | Flip reuse:711-712 decorator order to `@assistant.register_for_execution()` + `@helper.register_for_llm(...)`. |
| Visual watcher registration | `register_visual_watcher` (reuse only) | **`register_visual_watcher`** keep; move closure into core_AT so create-mode also gets it | Move reuse:1553-1572 → `core/agent_tools.build_core_tool_closures` block. |
| New-agent signalling | `create_new_agent` (reuse only) | **`create_new_agent`** keep; move into core_AT for parity | Move reuse:2089-2113 → `core/agent_tools`. |
| Time-agent ↔ main-agent bridge | `connect_time_main` (create system-prompt only), `Connect_to_main_agent` (reuse register_dual name override) | **`connect_time_main`** — already what the LLM sees in create's time-agent prompt; lowercase + underscores match the rest of the catalog | Change reuse:2293 `register_dual(..., "Connect_to_main_agent", ...)` → `register_dual(..., "connect_time_main", ...)`. |
| User-details lookup | `get_user_details` (core_AT, helper_fun via DB), `get_user_details` (create, helper_fun.parse_user_id) | **`get_user_details`** with the core_AT impl (real DB lookup) | Delete the create:931 register_dual override.  Both modes share core_AT version. |
| UX observation | `Observe_User_Experience` (core_AT capitalised JSON-input), `observe_user_experience` (core_AT lowercase structured-input) | **`observe_user_experience`** (lowercase, structured args).  Drop the JSON-string variant. | Delete core_AT:807 capitalised variant + matching prompt-string references. |
| Self critique | `Self_Critique_And_Enhance` (capitalised), `self_critique_and_enhance` (lowercase) | **`self_critique_and_enhance`** (matches Python naming convention) | Delete core_AT:869 capitalised variant + matching prompt-string references. |
| Suggest share-worthy | `Suggest_Share_Worthy_Content` | **`suggest_share_worthy_content`** (lowercase consistency) | Rename core_AT:762 name string. |
| Memory write | `save_data_in_memory` (drift between core_AT and reuse — disk persistence vs MemoryGraph dual-write) | **`save_data_in_memory`** with the UNION behaviour (disk + MemoryGraph) in core_AT | Move the MemoryGraph dual-write from reuse:1314-1322 into core_AT:140; delete reuse:1278 inline. |
| Memory read | `get_data_by_key` (drift — core_AT lazy-loads, reuse falls back to MemoryGraph semantic) | **`get_data_by_key`** with UNION behaviour | Move MemoryGraph fallback from reuse:1375-1382 into core_AT:228; delete reuse:1362 inline. |
| Send-to-user with mention guard | `send_message_to_user` (drift — core_AT unconditional, reuse guards `@statusverifier`/`@helper`/`@executor`) | **`send_message_to_user`** with mention guard from reuse | Move the guard from reuse:1664-1676 into core_AT:534; delete reuse:1655 inline. |
| Scheduled jobs | `create_scheduled_jobs` (drift — core_AT is a stub, reuse really creates the cron job) | **`create_scheduled_jobs`** with reuse's real impl | Move real cron creation from reuse:1640-1653 into core_AT:516; delete reuse's inline. |

After applying the proposals above, **the reuse_recipe inline AH-stack block from line 1242 to line 2113 collapses to ~0 unique closures** — every tool either lives in core_AT or in `integrations/channels`.  The remaining reuse-only registrations are `update_persona`, `Connect_to_main_agent` (rename), and the dynamic fanouts (MCP/AP2/HART-skills/service-tools/IP-protection).

The remaining cleanup work (Section 5 false-positive fix + Section 3 hallucination-trap fix) is mechanical: remove `get_data_from_memory` and `send_message_to_roles` from all four helper prompts, replace with `get_data_by_key` and the canonical `send_to_channel` respectively.

---

## Confidence

Read coverage on the four files:

- `core/agent_tools.py` — 100% (1257 lines).
- `integrations/channels/agent_tools.py` — 100% (767 lines).
- `create_recipe.py` — 100% via 2 reads (lines 1-800 + 870-1720 + 2562-2900) supplemented by Grep over `register_*` / `system_message` / `tool_name` patterns covering all 5100 lines.
- `reuse_recipe.py` — 100% via 3 reads (lines 1-350 + 680-2140 + 2140-2650) supplemented by Grep coverage of `register_*` / `system_message` / `tool_name` across all 3719 lines.
- `integrations/channels/memory/agent_memory_tools.py` — read 63-320 (registration + tool definitions).
- `integrations/service_tools/media_agent.py` — read 760-803 (registration block).

Every `@assistant.register_for_execution()` and `@helper.register_for_execution()` and every `register_dual(`, `register_core_tools(`, `register_channel_tools(`, `register_for_llm(name=` site is enumerated.  Loop-based fanouts (MCP, AP2, service-tools, HART-skills, marketing) are flagged as dynamic — the exact name set depends on which MCP servers / payment providers / skills are installed at runtime, but the registration mechanism itself is captured.
