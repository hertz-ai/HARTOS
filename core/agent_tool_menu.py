"""
core/agent_tool_menu.py -- which tools an agent is offered, and how they reach it.

Split out of core/agent_tools.py when that module crossed the 3000-line
god-module ratchet (tests/unit/test_source_guard_repo_health_ratchet.py).  This
module holds the tool MENU (MAIN_LEG_CORE_TOOLS, the CREATE_* sets and the menu
text), schema fitting (defer_helper_schema, fit_schema_to_ctx), registration
(register_dual, register_core_tools, register_request_tools) and runtime attach
(discover_and_attach, attach_for_tags, attach_for_names).  None of it BUILDS a
tool: build_core_tool_closures in core.agent_tools still does.  core.agent_tools
re-exports every public and private name defined here, so all existing imports
hold; this module never imports core.agent_tools at module level (no cycle).
"""
import logging
import re as _re

tool_logger = logging.getLogger('tool_execution')



def register_dual(helper, executor, func, name: str, description: str):
    """Register a single tool on both the LLM-calling and executing agents.

    AutoGen's tool pattern pairs ``helper.register_for_llm`` with
    ``executor.register_for_execution`` — create_recipe.py and
    reuse_recipe.py repeat this pair 40+ times inline. Call sites
    that define a closure and register it immediately use this
    helper; batches that already have a ``(name, desc, func)`` list
    should use :func:`register_core_tools` instead.

    Returns ``func`` unchanged so the call can be inlined after a
    closure definition without shadowing the name.
    """
    helper.register_for_llm(name=name, description=description)(func)
    executor.register_for_execution(name=name)(func)
    return func


# The core closures the MAIN agent leg carries — the helper/assistant pair
# that drives a user-facing turn, in BOTH the create and reuse pipelines.
#
# Canonical home is here, beside build_core_tool_closures() that produces the
# closures, so the two legs agree by construction.  It was previously a local
# `_MAIN_LEG_CORE` inside reuse_recipe only, which is why create's identical
# helper/assistant pair silently carried EVERY core closure instead.
#
# Why it has to be a filter at all — measured live 2026-09-05, CREATE of agent
# 88601674818 action 6:
#
#   wire-trim: the TOOL SCHEMA alone is 10544 tokens against an n_ctx of 12288
#   (72 tool(s)) — no amount of message trimming can make this fit.
#   -> 400 request (13378 tokens) exceeds the available context size (12288)
#
# The turn that 400'd was asking the Helper to WRITE A JSON RECIPE for one
# step, while carrying payments, video, channel, camera, receipt, Instagram
# and coding tools it cannot use.  filter_service_tools() already gates the
# SERVICE registry, but says so explicitly of this set: "the always-on core
# closures and Tier-2 families are unaffected" — so nothing bounded it.
#
# create_scheduled_jobs is deliberately absent: the factory's twin is a
# create-flow stub and the real live-scheduling version stays inline in
# reuse_recipe (see the #511 name-collision note at its definition).
MAIN_LEG_CORE_TOOLS = frozenset({
    'txt2img', 'img2txt', 'save_data_in_memory', 'get_saved_metadata',
    'get_data_by_key', 'get_user_id', 'get_prompt_id', 'Generate_video',
    'get_user_uploaded_file', 'get_user_camera_inp', 'get_chat_history',
    'search_visual_history', 'search_long_term_memory',
    'save_to_long_term_memory',
    'send_message_to_user', 'send_presynthesized_video_to_user',
    'send_message_in_seconds', 'google_search',
    # Book navigation (integrations/learning/book_tools.py).  On the main leg
    # because "read me this book" is a FOREGROUND user turn, and the filter
    # here is what decides whether the assistant can see them at all — a tool
    # appended to build_core_tool_closures but missing from this set is
    # silently dropped for the main leg.
    'list_books', 'list_book_chapters', 'read_book_page',
    'read_book_chapter', 'parse_book_pdf',
})

# The closures the CREATE leg registers always-on, on top of
# MAIN_LEG_CORE_TOOLS.  All of them live in build_core_tool_closures so the
# REUSE leg can attach them BY NAME for an action whose recipe names one;
# create_recipe registers them eagerly because the recipe-AUTHORING model has
# to be able to call them while it builds, and its prompts advertise them.
# Named here rather than in create_recipe so the two legs read one list.
CREATE_LEG_EXTRA_TOOLS = frozenset({
    'execute_coding_task', 'get_repository_map',
    'create_code_shard', 'get_coding_benchmarks',
    # create_recipe.py:3084 and :3102 tell the model to "always use the
    # validate_json_response tool"; a recipe naming it must be runnable.
    'validate_json_response',
})

#: Tools the CREATE prompts tell the model to call BY NAME, beyond the core
#: set.  The prompt menus (main_leg_tool_menu) and the helper's schema keep
#: rule (create_helper_keep) both read this one list, so a tool the prompt
#: names can never be missing from what the model is offered.  Measured
#: 2026-09-25: the prompts said "for Chrome or any browser, use
#: execute_windows_or_android_command", the schema bounding deferred it, and a
#: webmail agent made 57 tool calls, all send_message_to_user, never once
#: touching the screen.
CREATE_ADVERTISED_TOOLS = ('execute_windows_or_android_command',)


def create_helper_keep(helper_names, pre_tier2_names, svc_tools=()):
    """The tool names the CREATE helper keeps on its schema.

    Everything else it holds is deferred by defer_helper_schema to bound the
    schema's token cost.  Kept: the core set, ``request_tools`` (the escape
    that re-arms a deferral), the tools the CREATE prompts advertise, the
    service tools, and whatever the Tier-2 goal gate added for THIS goal
    (``helper_names - pre_tier2_names``).
    """
    return (set(MAIN_LEG_CORE_TOOLS) | {'request_tools'}
            | set(CREATE_ADVERTISED_TOOLS)
            | set(svc_tools or ())
            | (set(helper_names) - set(pre_tier2_names)))


def _join_tool_menu(names, extra=()):
    """One join for every prose tool menu: sorted, comma-separated, no quotes."""
    out = set(names)
    out.update(str(e) for e in (extra or ()) if e)
    return ', '.join(sorted(out))


def main_leg_tool_menu(extra=()):
    """The tool names to ADVERTISE on a leg that registers the FILTERED core.

    A prompt that hand-lists tool names drifts from the set the leg actually
    registers, and the model believes the prompt.  MEASURED 2026-09-10 against
    create_recipe.create_agents, which registers main_leg_core_tools(...) at
    :1116 -- so on THIS leg the other 19 core closures are filtered off:

        registered here, absent from the two prose menus:
            get_chat_history, search_visual_history, txt2img, img2txt
        in the menus, NOT registered on this leg:
            text_2_image, get_text_from_image  (real closures, but filtered
                out of MAIN_LEG_CORE_TOOLS in favour of txt2img / img2txt)
            create_scheduled_jobs  (deliberately absent -- see the note on
                MAIN_LEG_CORE_TOOLS above; the factory twin is a create-flow
                stub)

    So the authoring model was told three names this leg cannot call, and
    never told about get_chat_history.  Across all 127 saved flow recipes
    (1,034 steps) only 241 steps -- 23.3% -- name a tool the runtime serves;
    51.4% of the identifier-shaped names are unserved, dominated by near-misses
    of exactly the omitted capabilities (retrieve_memory / memory_query /
    search_chat_history / MemoryService for get_chat_history, web_search for
    google_search).  Ground truth: 71 distinct tools[].function.name on the
    wire in logs/llm_outbound.jsonl.

    `extra` carries names a caller registers BEYOND the core set -- e.g.
    execute_windows_or_android_command, which create_agents registers with
    register_dual(helper, assistant, ...) at :1670 so the Helper does hold its
    schema.  It never mutates MAIN_LEG_CORE_TOOLS.
    """
    return _join_tool_menu(MAIN_LEG_CORE_TOOLS, extra)


def registered_tool_menu(tools, extra=()):
    """The menu for a leg that registers ``tools`` UNFILTERED.

    create_recipe.create_time_agents (:3546) and reuse_recipe's helper1/time
    and helper2/visual legs (:2239, :2347) pass the whole
    build_core_tool_closures(...) list to register_core_tools, so their prompts
    may name all of it -- including the create_scheduled_jobs and
    text_2_image / get_text_from_image that the main leg filters away.  Deriving
    from the same list the call registers is what stops a hand-copy drifting.

    ``tools`` is the (name, description, func) shape build_core_tool_closures
    returns.
    """
    return _join_tool_menu((t[0] for t in tools), extra)


def helper_tool_names(agent):
    """The tool names currently on an agent's LLM schema, as a set.

    The reader half of :func:`defer_helper_schema` — callers snapshot with
    this before and after a registration block to learn which names that block
    contributed, rather than hard-coding a family list that drifts the moment
    a family gains a tool.  Tolerant of a missing llm_config, a missing
    ``tools`` block and malformed entries for the same reason: it runs during
    agent construction.
    """
    cfg = getattr(agent, 'llm_config', None)
    if not isinstance(cfg, dict):
        return set()
    out = set()
    for entry in cfg.get('tools') or []:
        if isinstance(entry, dict):
            fn = entry.get('function')
            if isinstance(fn, dict) and fn.get('name'):
                out.add(fn['name'])
    return out


def _schema_or_none(agent):
    """The names on ``agent``'s LLM schema, or None when it has no schema to
    read (no dict llm_config: a recorder, a UserProxy, llm_config=False).
    None means "only the attach ledger can say what is attached"."""
    if not isinstance(getattr(agent, 'llm_config', None), dict):
        return None
    return helper_tool_names(agent)


# Words that carry no capability.  Stopwords would over-attach: 'the' passes
# len>2 AND is a substring of 'synthesis', so "summarize the page" would match
# nearly every tool.
_NEED_STOPWORDS = frozenset({
    'the', 'and', 'for', 'you', 'your', 'with', 'that', 'this',
    'please', 'need', 'want', 'tool', 'tools', 'use', 'able',
    'can', 'get', 'have', 'from', 'into', 'about', 'some', 'any'})


def _need_names_tool(name, need_text, need_stems):
    """True when the need NAMES this tool: every salient word of the tool's
    name appears in the need, verbatim or by its 4-character stem.

    The stricter half of discover_and_attach's selector.  Its keyword/stem
    matcher answers "which tools might serve this need" and over-matches on
    purpose (measured: 'share context with other agents' also matched
    device_control, execute_coding_task and Post_As_User), which is fine for
    the Helper, the seat that only speaks when asked.  A tool goes onto the
    seat that SPEAKS only when the need names it, the same rule
    attach_for_names applies to a recipe."""
    tokens = [t for t in _re.split(r'[^a-z0-9]+', str(name).lower())
              if len(t) > 2 and t not in _NEED_STOPWORDS]
    return bool(tokens) and all(
        t in need_text or (len(t) >= 4 and t[:4] in need_stems)
        for t in tokens)


def _attach_tool(helper, executor, fn, description, make_func,
                 attached_names, helper_live, proposer_live):
    """Attach one tool: its schema onto the Helper (with execution on
    ``executor``) unless it is already there, and onto the proposing seat
    too when ``proposer_live`` is given.  The ONE primitive the three attach
    paths (discover_and_attach, attach_for_names, attach_for_tags) share.

    "Already attached" is the ledger AND the live schema.  The ledger alone
    was wrong once schemas could shrink: fit_schema_to_ctx / defer_helper_
    schema take a tool off the schema but not out of the ledger, and every
    attach path skipped ledger names, so a deferred tool could never come
    back (measured in the review of f526c4580: attach_for_names returned 0
    for it and request_tools answered "No local registry tool matches").
    ``helper_live`` None (no readable schema) keeps the ledger-only rule.

    ``proposer_live``: the names on ``executor``'s own schema, when the tool
    should also reach ``executor`` as a proposer; None when it should not.
    Updated in place, as are ``attached_names`` and ``helper_live``.

    Returns (on_helper, on_proposer) for what this call added, or None when
    ``make_func`` produced no callable (the backing service is down).
    """
    need_helper = not (fn in attached_names
                       and (helper_live is None or fn in helper_live))
    need_proposer = proposer_live is not None and fn not in proposer_live
    if not (need_helper or need_proposer):
        return False, False
    func = make_func()
    if func is None:
        return None
    if need_helper:
        register_dual(helper, executor, func, fn, description)
        attached_names.add(fn)
        if helper_live is not None:
            helper_live.add(fn)
    if need_proposer:
        executor.register_for_llm(name=fn, description=description)(func)
        proposer_live.add(fn)
    return need_helper, need_proposer


def defer_helper_schema(helper, names):
    """Drop ``names`` from the helper's LLM schema, leaving execution intact.

    Deferral, not exclusion: this removes only what the MODEL READS.  The
    callable stays in the executor's ``_function_map`` (``register_dual``
    already put it there), and ``discover_and_attach`` consults that map as a
    third source, so ``request_tools`` can put the schema back the moment an
    agent actually needs the capability.  Both halves are required — without
    them this would strand the tool permanently and breach the owner's
    2026-08-31 requirement that the hierarchy be LAZY, not exclusionary.

    ``request_tools`` itself is NEVER dropped, whatever the caller passes: it
    is the escape that makes every other deferral recoverable, so dropping it
    would silently convert deferral into exclusion for the whole set.

    Why this exists, measured live 2026-09-12 on the CREATE walk of agent
    87400889007 (Nunba, the default agent)::

        wire-trim: the TOOL SCHEMA alone is 7191 tokens against an n_ctx of
                   8192 (54 tool(s)) -- no amount of message trimming can
                   make this fit.
        [TRIM] trim could not reach budget -- messages 1673 tok + schema 7191
                   tok = 8864 tok against n_ctx 8192

    The walk banked actions 1-4 then died at action 5 on a 400
    exceed_context_size_error.  Attributing every wire body by its system
    prompt: all 4 unfittable calls are the Helper seat; all 43 fitting calls
    are Assistant/Executor carrying the bounded 18 MAIN_LEG_CORE_TOOLS.  Zero
    crossover.  Across 1,568 wire rows ``autogen.create`` called 9 distinct
    tools, ALL of them core — none of the other 36.  At CREATE the helper
    AUTHORS a recipe; it does not execute, which is why the families it never
    calls can wait until asked for.

    Returns the set of names actually removed, so callers can log the saving
    rather than pruning silently.  Missing llm_config, a missing ``tools``
    block, and malformed entries are all no-ops: this runs during agent
    construction and must never be the reason an agent fails to build.
    """
    drop = {n for n in (names or set()) if n != 'request_tools'}
    if not drop:
        return set()
    cfg = getattr(helper, 'llm_config', None)
    if not isinstance(cfg, dict):
        return set()
    block = cfg.get('tools')
    if not isinstance(block, list):
        return set()

    kept, removed = [], set()
    for entry in block:
        name = None
        if isinstance(entry, dict):
            fn = entry.get('function')
            if isinstance(fn, dict):
                name = fn.get('name')
        if name in drop:
            removed.add(name)
            continue
        kept.append(entry)
    if not removed:
        return removed
    # The model reads the CLIENT's snapshot, not llm_config.  autogen 0.2's
    # update_tool_signature -- which every register_for_llm goes through --
    # rebuilds self.client from llm_config, and OpenAIWrapper copies `tools`
    # into its own _config_list.  Editing llm_config['tools'] alone left that
    # snapshot untouched: live 2026-09-13, "deferred 35 tool(s)" was logged at
    # 08:50:10 and the Helper's wire body at 08:50:44 still carried all 54.
    # Remove through autogen's own API so config and client move together.
    # With the shared http_client (core.autogen_config) a rebuild costs
    # ~0.03 ms -- 35 measured in 0.001 s.  An object without that API has no
    # client to go stale, so the list edit is the whole job there.
    _update = getattr(helper, 'update_tool_signature', None)
    if callable(_update):
        for name in removed:
            _update(name, is_remove=True)
    else:
        cfg['tools'] = kept
    return removed


def fit_schema_to_ctx(agent, protect=(), room=None, *, turn_protect=False):
    """Defer tool schemas from ``agent`` until they fit the LIVE n_ctx.

    The budget half of :func:`defer_helper_schema`.  That function answers
    "drop THESE names"; a caller still has to know which names, and every
    caller so far has answered from a static list.  This answers it from the
    server: how much room the schema actually has right now, measured, and
    which tools fit in it.  It does the removal THROUGH ``defer_helper_schema``
    — one remover, so the deferral contract (execution survives on the
    executor's ``_function_map``, ``request_tools`` is never dropped,
    ``update_tool_signature`` keeps the client snapshot in step) holds here
    without being restated.

    WHY IT EXISTS, measured 2026-09-22 on this box (``llm_outbound.jsonl`` +
    its ``.old`` rotation, 1,184 records).  Every HTTP 400 is
    ``source=autogen.reuse`` — 60 of them — and every one is the HELPER seat
    carrying 50-65 tools, while the 972 passing reuse calls are the ASSISTANT
    seat carrying exactly the 23 of MAIN_LEG_CORE_TOOLS.  ``register_dual``
    puts the schema on the helper, so the helper accumulates AP2 payments, A2A
    delegation, outreach CRM, memory-graph, model-lifecycle, service and skill
    families that the bounded assistant never sees.

    But the COUNT is not the rule, and this is why the fix has to read the
    server rather than cap a number.  The same helper bodies returned 200 all
    morning and 400 from 08:04 onward with no code change between them:

        >=50-tool bodies   04-07h: 200 x50, 500 x2      08-10h: 400 x62
        llama_server_8080.log, started 08:04:
            srv load_model: initializing, n_slots = 1, n_ctx_slot = 4096

    The tool set is fixed at agent construction; the context moved under it,
    and nothing in the selection path was reading it.  The wire layer knew and
    could only complain — ``wire-trim: the TOOL SCHEMA alone is 6849 tokens
    against an n_ctx of 4096 (60 tool(s)) ... Prune the tool list for this
    agent`` — because by then the set has already been chosen.  This is the
    pruning that log line asks for, at the layer that can do it.

    ``room`` defaults to ``llm_outbound_logger.schema_token_room()``: the live
    per-slot n_ctx minus the message floor the wire trim reserves.  Imported
    rather than recomputed so the wire's floor and this ceiling are the same
    number by construction — the precedent is
    ``hart_intelligence_entry.py:6052``, which imports ``_trim_to_budget`` from
    the same module for the same "one budget authority" reason.  Prompt-side
    only; see that function for why ``max_tokens`` is not subtracted.

    KEEP ORDER, most-load-bearing first, because what survives matters as much
    as that something does:

      1. ``request_tools`` — the escape that makes deferral recoverable
         (``defer_helper_schema`` protects it whatever this function decides).
      2. ``protect`` — the names THIS action's own recipe declares, which
         ``attach_for_names`` attached precisely because the recipe names them.
         Pruning them would undo the one authoritative selector.
      3. ``MAIN_LEG_CORE_TOOLS`` — the set the leg is built around and the
         only set the passing bodies carry.
      4. everything else, in the order the agent already holds it (stable, so
         two turns with the same geometry prune the same way).

    Returns the set of names deferred — empty when the set already fits, when
    the geometry cannot be read, or when the agent has no schema.  NEVER
    raises: it runs on the per-turn dispatch path, and a token optimisation may
    not be the reason a turn dies.
    """
    try:
        block = ((getattr(agent, 'llm_config', None) or {}).get('tools')
                 if isinstance(getattr(agent, 'llm_config', None), dict)
                 else None)
        if not isinstance(block, list) or not block:
            return set()
        from core.llm_outbound_logger import _schema_tokens, schema_token_room
        if room is None:
            room = schema_token_room()
        room = int(room)

        # ONE protected set per agent across every fit of a turn.  The
        # per-turn fit (turn_protect=True, REUSE's per-action attach door)
        # records the action's own recipe-named tools on the agent; every
        # later fit in the turn protects them too.  Without it a request_tools
        # fit, protecting only what it had just attached, evicted the action's
        # own tool from the Assistant (review of ee79a6fcb, measured:
        # crawl4ai_crawl gone after request_tools(delegate_to_specialist)).
        asked = {str(p) for p in (protect or ()) if p}
        if turn_protect:
            try:
                agent._hart_turn_protect = set(asked)
            except Exception:
                pass
        keep_first = ({'request_tools'} | asked
                      | set(getattr(agent, '_hart_turn_protect', None) or ()))

        def _rank(item):
            idx, entry = item
            name = ((entry.get('function') or {}).get('name')
                    if isinstance(entry, dict) else None)
            if name in keep_first:
                return (0, idx)
            if name in MAIN_LEG_CORE_TOOLS:
                return (1, idx)
            return (2, idx)

        ranked = sorted(enumerate(block), key=_rank)
        used, kept, drop = 0, [], set()
        for _idx, entry in ranked:
            name = ((entry.get('function') or {}).get('name')
                    if isinstance(entry, dict) else None)
            cost = _schema_tokens({'tools': [entry]})
            if used + cost <= room:
                used += cost
                kept.append((entry, name))
                continue
            if name:
                drop.add(name)
        # Per-entry costs are measured one entry at a time, so they miss the
        # separators of the assembled array and come out ~0.2% OPTIMISTIC (24
        # kept entries summed to 3072 against a real block of 3078).  A budget
        # that is optimistic by any margin is the failure mode this function
        # exists to end, so settle it against the REAL block and pop the
        # lowest-priority survivors until it is true.  Usually zero iterations.
        while kept:
            total = _schema_tokens({'tools': [e for e, _ in kept]})
            if total <= room:
                used = total
                break
            _, name = kept.pop()
            if name:
                drop.add(name)
        else:
            used = 0
        if not drop:
            return set()
        removed = defer_helper_schema(agent, drop)
        if removed:
            tool_logger.info(
                "tool schema bounded to the live n_ctx: kept ~%d tok of %d "
                "available, deferred %d tool(s) -- still executable and "
                "re-attachable via request_tools: %s",
                used, room, len(removed), ', '.join(sorted(removed)))
        return removed
    except Exception as e:
        tool_logger.warning(f"tool schema ctx-fit skipped: {e}")
        return set()


def main_leg_core_tools(tools):
    """The subset of ``tools`` the main helper/assistant leg registers.

    ONE filter for both pipelines — call this rather than re-deriving the
    name set, so create and reuse can never drift apart again.
    """
    return [t for t in tools if t[0] in MAIN_LEG_CORE_TOOLS]


def register_core_tools(tools, helper, executor, *,
                        executor_proposes=False, second_executor=None):
    """Register (name, desc, func) tuples on an AutoGen helper/executor pair.

    Args:
        tools: list of (name, description, func) tuples from build_core_tool_closures()
        helper: AutoGen agent that suggests tool use (register_for_llm)
        executor: AutoGen agent that executes tools (register_for_execution)
        executor_proposes: ALSO give ``executor`` the LLM schema, so it can
            propose these tools instead of being told they do not exist.
        second_executor: a distinct agent that can execute them, so
            ``executor``'s own structured tool_calls are not stranded.

    ``executor_proposes`` exists because the helper=schema / executor=execution
    split silently disarms whichever agent the recipe actually assigns the work
    to.  Measured live 2026-09-06, agent 89555447799: the main leg registered
    with ``(helper, assistant)``, so the Assistant held execution only and its
    outbound bodies carried NO ``tools[]`` at all — while ~591 execution-persona
    bodies in the same window named it as the actor
    (``'agent_to_perform_this_action': 'Assistant'``).  Downstream that produced
    26x "The requested tool 'google_search' is not available" and 2,657+
    "Error: Function <X> not found" (send_message_to_user x1052 — the path that
    returns the agent's result to the user; request_tools x101 — the
    never-say-unavailable escape hatch, itself unreachable).

    ``second_executor`` is the other half, for the same reason news_tools.py
    takes an ``executor=``: once the proposer emits a STRUCTURED tool_call,
    autogen's repeat-speaker rule will not let that same agent speak again to
    run it, so a sole-executor proposer strands its own call with no role=tool
    answer.

    This is the canonical home for the pattern that news_tools.py:421-442 and
    revenue_tools.py:224-225 currently inline ("Deliberately dual here ... do
    not 'simplify' it back"); per review follow-up #755 item 2 those two should
    migrate here rather than a third copy being written.

    Cost, measured before landing: the 18 MAIN_LEG_CORE_TOOLS serialise to
    ~1,859 tokens.  Against the live geometry (n_ctx 12,288, 1 slot, max_tokens
    2,048, safety margin 2,816) that leaves 5,565 tokens for messages — well
    clear of the degrade branch.  Re-measure before widening this to the full
    service registry, which is ~6,758 tokens and would not fit.

    Defaults are a strict no-op: the time and visual legs keep
    helper=schema / executor=execution exactly as before.
    """
    for name, desc, func in tools:
        register_dual(helper, executor, func, name, desc)
        if executor_proposes:
            executor.register_for_llm(name=name, description=desc)(func)
        if second_executor is not None:
            second_executor.register_for_execution(name=name)(func)


def filter_service_tools(goal_tags, svc_tools, svc_defs, registry):
    """Tier-1 hierarchical gate: keep only registry tools the goal unlocks.

    Completes the 'progressive/hierarchical tool injection' design that
    Tier 2 (goal-gated family loaders in create/reuse_recipe) already
    follows: goal_manager.register_goal_type rows map goal tags to
    ServiceToolRegistry capability tags, get_tool_tags reads them, and
    this filter applies the intersection at the attach site.  Until now
    the service loop registered EVERY tool unconditionally — measured
    2026-08-31: 50 rendered defs cost 5,820 of the 6,144-token slot,
    so a one-message conversation overflowed (12 'Context size has been
    exceeded' rejections in one boot).

    A goal with no unlocked tags gets NO service tools (need-to-know);
    the always-on core closures and Tier-2 families are unaffected.

    Args:
        goal_tags: tags from marketing_tools.detect_goal_tags(goal)
        svc_tools: {func_name: callable} from get_all_tool_functions()
        svc_defs:  defs from get_tool_definitions() — each carries
                   'name' (func name) and 'service_tool' (parent tool)
        registry:  the ServiceToolRegistry (parent tools carry .tags)
    """
    from integrations.agent_engine.goal_manager import get_tool_tags
    unlocked = set()
    for t in goal_tags or []:
        unlocked.update(get_tool_tags(t))
    if not unlocked:
        return {}
    parent_of = {d.get('name'): d.get('service_tool') for d in svc_defs}
    kept = {}
    for func_name, func in svc_tools.items():
        parent = registry._tools.get(parent_of.get(func_name))
        if parent is not None and set(parent.tags or []) & unlocked:
            kept[func_name] = func
    return kept


def discover_and_attach(need, helper, executor, registry, attached_names,
                        core_tools=None):
    """On-demand tool discovery: the never-say-unavailable half of the gate.

    Owner requirement 2026-08-31: the hierarchy must be LAZY, not
    exclusionary — an agent whose current set lacks a capability calls
    this (via its always-on `request_tools` wrapper) instead of denying.
    Searches the FULL service registry by name/description/tags, attaches
    every match onto the live helper/executor pair right now (autogen
    updates llm_config immediately, so the NEXT model call carries the
    defs), and reports what else exists beyond this box: registry tools
    whose backing service is not running can be self-hosted via the
    existing install scaffolding, and hive peers may offer the
    capability (earning mode — requires the user's payment consent;
    discovery only REPORTS that, it never executes remotely).

    Args:
        need: free-text capability description from the model
        helper/executor: the live agent pair to attach onto
        registry: ServiceToolRegistry
        attached_names: set of func names already on the agents —
            updated in place with everything newly attached
        core_tools: the ``(name, description, func)`` triples
            ``build_core_tool_closures`` returns.  SAME reason
            ``attach_for_names`` needed them (D25/#788): the registry holds
            SERVICE tools, while the capability an agent asks for at runtime
            is usually a CORE closure.  Owner requirement 2026-09-09 — an
            agent whose recipe names no tool must still identify the need at
            RUNTIME and get it — and this is that path, so searching the
            registry alone made the requirement unmeetable for core
            capabilities.  Measured live 2026-09-09, agent 33323830039: the
            model called ``request_tools`` at 17:55:35 precisely because it
            could not see execute_windows_or_android_command, and discovery
            attached nothing — that tool is a core closure and the registry
            holds 13 service names, of which exactly one (crawl4ai) is a name
            any recipe uses.  Omitted or empty is a strict no-op.
    Returns a human/model-readable summary string.
    """
    words = {w for w in str(need).lower().replace(',', ' ').split()
             if len(w) > 2 and w not in _NEED_STOPWORDS}
    if not words:
        return "Tell me what capability you need, e.g. 'text to speech'."
    # The seat that proposes its own tool calls (register_core_tools'
    # executor_proposes: the main leg's Assistant) gets the tools the need
    # NAMES on its own schema; everything the looser matcher finds goes to
    # the Helper as before.  Measured live 2026-09-25 (A2A-3): the Assistant
    # answered "the delegate_to_specialist tool isn't available in my current
    # toolset" because every attach put the schema on the Helper only.
    need_text = str(need).lower()
    need_stems = {w[:4] for w in _re.split(r'[^a-z0-9]+', need_text)
                  if len(w) >= 4}
    helper_live = _schema_or_none(helper)
    exec_live = helper_tool_names(executor)
    proposes = bool(exec_live)
    to_proposer = []

    def _attach(fn, desc, make_func):
        named = proposes and _need_names_tool(fn, need_text, need_stems)
        got = _attach_tool(helper, executor, fn, desc, make_func,
                           attached_names, helper_live,
                           exec_live if named else None)
        if got is None:
            return None
        on_helper, on_proposer = got
        if on_proposer:
            to_proposer.append(fn)
        if on_helper or on_proposer:
            attached.append(fn)
        return got

    attached, startable = [], []
    for tool_name, tool in registry._tools.items():
        hay = ' '.join([tool_name, ' '.join(tool.tags or []),
                        getattr(tool, 'description', '') or '']).lower()
        for ep_name, ep in tool.endpoints.items():
            fn = (tool_name if ep_name == tool_name
                  else f"{tool_name}_{ep_name}")
            hay_ep = hay + ' ' + str(ep.get('description', '')).lower()
            # Substring alone misses morphological variants ('scrape' is not
            # a substring of 'scraping') — also match on shared 4-char stems.
            # Deterministic, no fuzz; a rare extra attach is bounded cost.
            hay_words = {hw for hw in _re.split(r'[^a-z0-9]+', hay_ep)
                         if len(hw) >= 4}
            stems = {hw[:4] for hw in hay_words}
            if not any(w in hay_ep or (len(w) >= 4 and w[:4] in stems)
                       for w in words):
                continue
            if _attach(fn, ep.get('description', f'{tool_name} {ep_name}'),
                       lambda t=tool_name, e=ep_name:
                           registry.create_endpoint_function(t, e)) is None:
                startable.append(fn)
    # Core closures: SAME selector (the keyword/stem matcher above), same
    # idempotent `attached_names`, same register_dual primitive — only the
    # SOURCE differs.  Kept in this function rather than a sibling so there is
    # one answer to "attach the tool this need describes", not two that drift
    # (the reason attach_for_names holds its core loop inline too).
    for _c_name, _c_desc, _c_func in (core_tools or []):
        hay_core = (str(_c_name) + ' ' + str(_c_desc or '')).lower()
        core_words = {hw for hw in _re.split(r'[^a-z0-9]+', hay_core)
                      if len(hw) >= 4}
        core_stems = {hw[:4] for hw in core_words}
        if not any(w in hay_core or (len(w) >= 4 and w[:4] in core_stems)
                   for w in words):
            continue
        _attach(_c_name, _c_desc, lambda f=_c_func: f)

    # THIRD source: tools the EXECUTOR can already run but the helper can no
    # longer SEE.  `register_dual` splits schema (helper.register_for_llm) from
    # execution (executor.register_for_execution -> _function_map), so a family
    # whose schema is withheld from the helper to save context is still fully
    # live on the executor — the callable is right there, only the description
    # the model reads is missing.  Consulting it costs nothing and is what
    # makes withholding SAFE rather than exclusionary.
    #
    # Why this is required, measured live 2026-09-12 on the CREATE walk of
    # agent 87400889007: the create helper carries 54 tools = 7,191 schema
    # tokens against n_ctx 8,192 (88% of the window), so the trimmer reports
    # "the TOOL SCHEMA alone is 7191 tokens ... no amount of message trimming
    # can make this fit" and the walk 400s at action 5.  Across 1,568 wire rows
    # autogen.create called 9 distinct tools, ALL of them in MAIN_LEG_CORE_TOOLS
    # — none of the other 36.  Narrowing the helper is therefore the fix, but
    # the families that make it overflow (channel, memory-graph, coding, AP2,
    # media) live in NEITHER of the two sources above, so without this loop
    # narrowing would make them permanently unreachable.  That is the owner's
    # 2026-08-31 requirement in reverse: the hierarchy must be LAZY, not
    # exclusionary.
    #
    # Same selector, same idempotent `attached_names`, same register_dual
    # primitive as the two loops above — only the SOURCE differs, kept here
    # rather than in a sibling for the reason the core loop is inline too.
    # `_function_map` is the established accessor (reuse_recipe.py:5000-5008
    # already reads it for the sibling "can this agent serve the call"
    # question).  Missing attribute is a strict no-op: agents built by the
    # time and visual factories must degrade to today's behaviour, not raise.
    for _x_name, _x_func in sorted(
            (getattr(executor, '_function_map', None) or {}).items()):
        # _function_map carries no description — the docstring's first line is
        # the only text the tool ships with, and it is what the model will read
        # once re-attached.  Fall back to the name so a doc-less callable is
        # still matchable and still gets a non-empty description.
        _x_desc = ((getattr(_x_func, '__doc__', '') or '').strip()
                   .split('\n')[0].strip()) or _x_name
        hay_x = (str(_x_name) + ' ' + _x_desc).lower()
        x_words = {hw for hw in _re.split(r'[^a-z0-9]+', hay_x) if len(hw) >= 4}
        x_stems = {hw[:4] for hw in x_words}
        if not any(w in hay_x or (len(w) >= 4 and w[:4] in x_stems)
                   for w in words):
            continue
        _attach(_x_name, _x_desc, lambda f=_x_func: f)

    # Bound what reached the proposing seat to the live n_ctx, protecting what
    # this request named.  The CREATE leg has no per-turn fit (only REUSE's
    # _attach_named_tools_for_action runs one), so without this a request
    # could widen the Assistant, the seat whose bodies fit, past the window.
    on_proposer = list(to_proposer)
    if to_proposer:
        deferred = fit_schema_to_ctx(executor, protect=to_proposer)
        on_proposer = [n for n in to_proposer if n not in deferred]

    parts = []
    if attached:
        # Imperative on purpose: hop-2 probe 2026-08-31 showed the model
        # attaching crawl4ai then STILL answering "I cannot browse the live
        # internet" - the trained refusal prior survives a neutral result.
        parts.append(
            "Attached and ready to call NOW: " + ', '.join(attached)
            + ". These execute LOCALLY on this machine, so no "
              "internet-access or capability restriction applies. "
              "Immediately CALL the one that fits the task. Do NOT tell "
              "the user this is unavailable - the tool is live.")
        if proposes:
            # Which seat can call which: a tool only the Helper holds is
            # called by tagging it, and saying so is what keeps "ready to
            # call NOW" true for the seat that asked.
            _name = lambda a, d: str(getattr(a, 'name', '') or d)
            if on_proposer:
                parts.append(f"{_name(executor, 'You')} can call directly: "
                             + ', '.join(on_proposer) + '.')
            helper_only = [n for n in attached if n not in on_proposer]
            if helper_only:
                h = _name(helper, 'Helper')
                parts.append(f"Held by @{h}; ask @{h} to call: "
                             + ', '.join(helper_only) + '.')
    if startable:
        parts.append("Exists locally but the backing service is down — it "
                     "can be self-hosted/started via the install flow: "
                     + ', '.join(startable))
    if not parts:
        parts.append(
            "No local registry tool matches. Options that DO exist: ask the "
            "user to install it (hub install flow), or a hive peer may offer "
            "this capability — peer execution needs the user's consent and, "
            "in earning mode, payment approval. Do not tell the user this is "
            "impossible; offer these routes.")
    return '  '.join(parts)


def attach_for_tags(cap_tags, helper, executor, registry, attached_names):
    """Attach every registry tool whose capability tags intersect cap_tags.

    The deterministic sibling of discover_and_attach: same attach
    primitives (create_endpoint_function + register_dual) but matched by
    registry capability tags, exactly like filter_service_tools.  The
    per-turn hook in reuse uses this so a conversation that drifts into
    a capability the construction-time goal never mentioned gets its
    family attached BEFORE the model sees the turn — zero extra LLM
    calls, no reliance on the model choosing to call request_tools.
    Names already in attached_names are skipped (idempotent across
    turns); the set is updated in place.  Returns the count attached.
    """
    cap = set(cap_tags or [])
    if not cap:
        return 0
    # Helper only: a tag is inferred from the turn's prose, never a request
    # that names the tool, so it does not reach the seat that speaks.
    helper_live = _schema_or_none(helper)
    n = 0
    for tool_name, tool in registry._tools.items():
        if not (set(tool.tags or []) & cap):
            continue
        for ep_name, ep in tool.endpoints.items():
            fn = tool_name if ep_name == tool_name else f"{tool_name}_{ep_name}"
            got = _attach_tool(
                helper, executor, fn,
                ep.get('description', f'{tool_name} {ep_name}'),
                lambda t=tool_name, e=ep_name:
                    registry.create_endpoint_function(t, e),
                attached_names, helper_live, None)
            if got and got[0]:
                n += 1
    return n


def arm_turn_attach(executor, attached_names, goal_tags):
    """Give a freshly built agent pair the ledger attach_for_turn reads:
    the tool names already on the pair and the goal tags already unlocked.
    ONE writer, called by both builders (create_recipe.create_agents and
    reuse_recipe's constructor) on the agent their register_dual executes
    service tools on."""
    executor._hart_attached_tools = attached_names
    executor._hart_unlocked_tags = set(goal_tags or ())


def attach_for_turn(message, helper, executor, registry):
    """Tier-1 per-turn attach: the capability families THIS turn's words
    unlock that the agents do not carry yet.  Returns (new_tags, n_attached).

    ONE implementation for both pipelines' turn entry -- REUSE
    get_agent_response and CREATE get_response_group -- so a conversation
    that drifts into a capability its build-time goal never mentioned (an
    agent asked mid-chat to vote on an experiment) gets that family before
    the model sees the turn.  CREATE only attached at build time until the
    review of d99b1aa88.  Reads the ledger arm_turn_attach put on
    ``executor``; agents built without one are left alone.  Idempotent: a
    tag already unlocked is skipped and the ledger is updated in place.

    Whatever it attaches is then reconciled with the LIVE n_ctx
    (fit_schema_to_ctx), here, so neither caller can grow the schema past
    the server.  CREATE's helper is trimmed at build time because it
    measured 7191 schema tokens against n_ctx 8192 (create_recipe.py, the
    defer_helper_schema block); an attach after that with no fit reopened
    the 400 (review of a4dc8cf3b).  The tools just attached are protected,
    since this turn asked for them; anything deferred stays executable and
    re-attachable through request_tools.
    """
    unlocked = getattr(executor, '_hart_unlocked_tags', None)
    attached = getattr(executor, '_hart_attached_tools', None)
    if unlocked is None or attached is None:
        return [], 0
    from integrations.agent_engine.marketing_tools import detect_goal_tags
    from integrations.agent_engine.goal_manager import get_tool_tags
    new = [t for t in detect_goal_tags(message or '') if t not in unlocked]
    if not new:
        return [], 0
    cap = set()
    for t in new:
        cap.update(get_tool_tags(t))
    before = set(attached)
    n = attach_for_tags(cap, helper, executor, registry, attached)
    unlocked.update(new)
    if n:
        just = set(attached) - before
        fit_schema_to_ctx(helper, protect=just)
        fit_schema_to_ctx(executor, protect=just)
    return new, n


def attach_for_names(names, helper, executor, registry, attached_names,
                     core_tools=None):
    """Attach the tools a turn NAMES outright — registry AND core closures.

    Name-keyed sibling of ``attach_for_tags`` — same primitives
    (``create_endpoint_function`` + ``register_dual``), same idempotent
    ``attached_names`` set, same return contract.  Not a second attachment
    mechanism: only the SELECTOR differs, and this one is authoritative
    where the other infers.

    Why it exists.  ``attach_for_tags`` matches on capability tags derived
    from a keyword scan of the turn's prose.  That is a good fallback for
    families nothing mentions, and a bad way to honour an action that says
    which tool it needs.  Measured live 2026-09-06 on agent 89555447799:
    recipe action 1 declares ``tool_name: google_search`` and the seeded
    message carries it verbatim, yet ``detect_goal_tags`` read the words
    "developer"/"platforms" as the tag ``coding`` and the attach logged

        Tier-1 turn attach: +['coding'] -> 0 tools

    google_search never reached the wire (1 of 96 autogen.reuse calls in a
    26-minute drive carried any tools[] block; ``INSIDE google search`` fired
    0 times), so the model could not call the one tool its own recipe named.

    The same gap at population scale: 8,799 ``Error: Function <X> not found``
    across the log rotations — send_message_to_user x1618 (the path that
    hands the agent's result to the user), get_user_details x908,
    execute_windows_or_android_command x418.  Those tools are defined and
    registerable; they were simply not attached for that turn.  It also
    explains why one tool both works and fails: same tool, different turn,
    different tag scan.

    Unknown names are ignored rather than raising — a recipe may name a tool
    this deployment does not ship, and a turn that mentions one absent tool
    must still get the others.

    ``core_tools`` — WHY THIS FUNCTION NEEDED A SECOND SOURCE.  Searching only
    ``registry._tools`` made the whole mechanism inert, because the recipes
    name CORE closures and the registry holds SERVICE tools.  Measured live
    2026-09-07/08 across 23 agents driven through /chat (672,846 server.log
    lines): ``Tier-1 named attach`` logged ZERO times, with ``turn attach
    skipped`` also zero — the block ran every round and resolved nothing.
    The registry holds 13 names on this deployment (payments x3,
    seo_audit_score, gh_pr_open, crawl4ai, crawl4ai_crawl, pocket_tts x3,
    acestep x3); of the tool names the recipes actually use, exactly ONE
    (crawl4ai) is among them.

    What the actions name instead, and how often FAB-GUARD saw it unrun:

        execute_windows_or_android_command   35 named, 27 unrun (77%)
        google_search                        13 named,  0 unrun ( 0%)
        send_message_to_user                  5 named,  2 unrun (40%)
        save_to_long_term_memory              5 named,  2 unrun (40%)

    The 0% entry is the control: ``google_search`` is in MAIN_LEG_CORE_TOOLS
    and therefore always-on, so it never needs attaching.  The 77% entry is
    not in that frozenset, so on the MAIN leg it could not be called at all —
    reuse_recipe.py hands the time and visual legs the FULL closure list
    (:2142, :2250) but the main leg only ``main_leg_core_tools(...)`` (:2167).
    That asymmetry is what stalled agent 89555447799 on action 3 for 21
    minutes: the tool never ran, so StatusVerifier honestly kept returning
    'pending' and the turn burned its whole round budget (tasks #770, #790).

    Passing the core closures here fixes that WITHOUT widening
    MAIN_LEG_CORE_TOOLS, which is deliberate: that frozenset is the always-on
    set for every agent, and execute_windows_or_android_command runs arbitrary
    OS commands.  Attaching it only for an action whose own recipe names it
    keeps the blast radius at the action that asked for it.

    Accepts the ``(name, description, func)`` tuples ``build_core_tool_closures``
    already returns, so callers pass what they built — no second builder.
    Omitted or empty is a strict no-op, leaving the registry path unchanged.
    """
    want = {str(n) for n in (names or []) if n}
    if not want:
        return 0
    # A name the action's recipe declares is a request that names the tool,
    # so it reaches a proposing ``executor``'s own schema too (the main leg's
    # Assistant, which emits its own tool_calls).  Bounding that schema to the
    # live n_ctx is the caller's job, with these names protected: REUSE's
    # _attach_named_tools_for_action runs fit_schema_to_ctx right after.
    helper_live = _schema_or_none(helper)
    exec_live = helper_tool_names(executor)
    proposer_live = exec_live if exec_live else None
    n = 0

    def _count(got):
        return 1 if got and (got[0] or got[1]) else 0

    for tool_name, tool in registry._tools.items():
        for ep_name, ep in tool.endpoints.items():
            fn = tool_name if ep_name == tool_name else f"{tool_name}_{ep_name}"
            if fn not in want:
                continue
            n += _count(_attach_tool(
                helper, executor, fn,
                ep.get('description', f'{tool_name} {ep_name}'),
                lambda t=tool_name, e=ep_name:
                    registry.create_endpoint_function(t, e),
                attached_names, helper_live, proposer_live))

    # Core closures: same selector, same idempotent set — only the SOURCE
    # differs.  Kept inside this function rather than a sibling so there is one
    # answer to "attach the tools this turn names", not two that can drift.
    for core_name, core_desc, core_func in (core_tools or []):
        if core_name not in want:
            continue
        n += _count(_attach_tool(helper, executor, core_name, core_desc,
                                 lambda f=core_func: f, attached_names,
                                 helper_live, proposer_live))
    return n


REQUEST_TOOLS_DESCRIPTION = (
    "Discover and attach additional tools by describing the capability you "
    "need, e.g. 'text to speech' or 'crawl a webpage'. Call this FIRST "
    "whenever your current tools lack a capability - never tell the user "
    "something is unavailable without trying this. If it finds no match, "
    "call it once more with different wording.")


def register_request_tools(helper, executor, registry, attached_names):
    """Register ``request_tools``, the never-say-unavailable escape, on a
    helper/executor pair.  ONE home for the closure the CREATE and REUSE main
    legs each defined inline.

    The schema goes on the Helper and, when ``executor`` proposes its own
    tool calls (register_core_tools' executor_proposes), on ``executor`` too.
    The escape has to be reachable from the seat that speaks: measured live
    2026-09-25 (A2A-3), the Assistant, holding a schema without it, answered
    "the delegate_to_specialist tool isn't available in my current toolset"
    instead of asking.  The core closure list is read from
    ``executor._hart_core_tools`` at CALL time, as both inline copies did, so
    a list set after registration is still seen.
    """
    def request_tools(need: str) -> str:
        return discover_and_attach(need, helper, executor, registry,
                                   attached_names,
                                   core_tools=getattr(
                                       executor, '_hart_core_tools', None))
    register_dual(helper, executor, request_tools, 'request_tools',
                  REQUEST_TOOLS_DESCRIPTION)
    if helper_tool_names(executor):
        executor.register_for_llm(
            name='request_tools',
            description=REQUEST_TOOLS_DESCRIPTION)(request_tools)
    return request_tools
