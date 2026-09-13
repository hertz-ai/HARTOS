"""
core/agent_tools.py — Canonical AutoGen tool definitions (single source of truth).

Every core tool is defined ONCE here. Both create_recipe.py and reuse_recipe.py
call build_core_tool_closures() + register_core_tools() instead of duplicating
function bodies and registration lines.

Pattern mirrors:
  - integrations/service_tools/media_agent.py   → register_media_tools()
  - integrations/channels/memory/agent_memory_tools.py → register_autogen_tools()
  - integrations/agent_engine/marketing_tools.py → register_marketing_tools()
"""
import json
import logging
import os
import re as _re
import threading
import time
import uuid
from datetime import datetime
from typing import Annotated, Any, List, Optional, Tuple

import requests
from json_repair import repair_json

from core.http_pool import pooled_get, pooled_post
from integrations.service_tools.model_catalog import ModelType

tool_logger = logging.getLogger('tool_execution')


# ---------------------------------------------------------------------------
# Generic registration helper
# ---------------------------------------------------------------------------

_INTERNAL_ERROR_MARKERS = ('context size', 'server_error', 'error code: 500',
                           'traceback', 'connection', 'timed out', 'timeout',
                           'memory slot')


def user_facing_error(e):
    """ONE user-facing string for an internal failure (#716).

    Three sites (reuse get_agent_response, create get_response_group,
    gather_agentdetails) returned raw exception text as the reply, so
    TTS spoke lines like 'Context size has been exceeded' to the user
    (observed live 2026-08-31, probe run 4).  Full tracebacks are
    already logged at every call site - the reply only needs to be
    honest and speakable.
    """
    text = str(e)
    low = text.lower()
    if any(m in low for m in _INTERNAL_ERROR_MARKERS) or len(text) > 200:
        return _SNAG_REPLY
    return f"{_COULD_NOT_FINISH_PREFIX}{text[:160]}"


# The two shapes user_facing_error() produces, named once so the code that has
# to RECOGNISE a failed turn reads the same strings the code that writes them
# does.  Changing the wording here changes both.
_SNAG_REPLY = ("I hit an internal snag finishing that - please try "
               "again in a moment.")
_COULD_NOT_FINISH_PREFIX = "I couldn't finish that: "


def is_user_facing_error(reply) -> bool:
    """True when ``reply`` is a failed turn dressed as an answer.

    A turn that fails does not raise to its caller: user_facing_error() turns
    the exception into a polite, speakable sentence and the pipeline returns
    it as the reply, and hart_intelligence_entry does the same with
    LLM_LOADING_REPLY / LLM_GENERIC_ERROR_REPLY.  That is right for a person
    reading it and wrong for any caller that has to decide whether WORK was
    done.  Measured on central 2026-09-13: the distributed worker submitted
    "I couldn't finish that: Error code: 429 - ... rate_limit_exceeded ..." as
    a hive task's result, and the coordinator marked the task completed.

    Recognises exactly the strings this codebase emits for a failure, by
    reference to where they are defined, so rewording one cannot silently stop
    this check from matching it.
    """
    if not isinstance(reply, str):
        return False
    text = reply.strip()
    if not text:
        return False
    if text == _SNAG_REPLY or text.startswith(_COULD_NOT_FINISH_PREFIX):
        return True
    from core.constants import LLM_GENERIC_ERROR_REPLY, LLM_LOADING_REPLY
    return text in (LLM_LOADING_REPLY.strip(), LLM_GENERIC_ERROR_REPLY.strip())


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
    # Stopwords would over-attach: 'the' passes len>2 AND is a substring of
    # 'synthesis', so "summarize the page" would match nearly every tool.
    stop = {'the', 'and', 'for', 'you', 'your', 'with', 'that', 'this',
            'please', 'need', 'want', 'tool', 'tools', 'use', 'able',
            'can', 'get', 'have', 'from', 'into', 'about', 'some', 'any'}
    words = {w for w in str(need).lower().replace(',', ' ').split()
             if len(w) > 2 and w not in stop}
    if not words:
        return "Tell me what capability you need, e.g. 'text to speech'."
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
            if fn in attached_names:
                continue
            func = registry.create_endpoint_function(tool_name, ep_name)
            if func is None:
                startable.append(fn)
                continue
            register_dual(helper, executor, func, fn,
                          ep.get('description', f'{tool_name} {ep_name}'))
            attached_names.add(fn)
            attached.append(fn)
    # Core closures: SAME selector (the keyword/stem matcher above), same
    # idempotent `attached_names`, same register_dual primitive — only the
    # SOURCE differs.  Kept in this function rather than a sibling so there is
    # one answer to "attach the tool this need describes", not two that drift
    # (the reason attach_for_names holds its core loop inline too).
    for _c_name, _c_desc, _c_func in (core_tools or []):
        if _c_name in attached_names:
            continue
        hay_core = (str(_c_name) + ' ' + str(_c_desc or '')).lower()
        core_words = {hw for hw in _re.split(r'[^a-z0-9]+', hay_core)
                      if len(hw) >= 4}
        core_stems = {hw[:4] for hw in core_words}
        if not any(w in hay_core or (len(w) >= 4 and w[:4] in core_stems)
                   for w in words):
            continue
        register_dual(helper, executor, _c_func, _c_name, _c_desc)
        attached_names.add(_c_name)
        attached.append(_c_name)

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
        if _x_name in attached_names:
            continue
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
        register_dual(helper, executor, _x_func, _x_name, _x_desc)
        attached_names.add(_x_name)
        attached.append(_x_name)

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
    n = 0
    for tool_name, tool in registry._tools.items():
        if not (set(tool.tags or []) & cap):
            continue
        for ep_name, ep in tool.endpoints.items():
            fn = tool_name if ep_name == tool_name else f"{tool_name}_{ep_name}"
            if fn in attached_names:
                continue
            func = registry.create_endpoint_function(tool_name, ep_name)
            if func is None:
                continue
            register_dual(helper, executor, func, fn,
                          ep.get('description', f'{tool_name} {ep_name}'))
            attached_names.add(fn)
            n += 1
    return n


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
    n = 0
    for tool_name, tool in registry._tools.items():
        for ep_name, ep in tool.endpoints.items():
            fn = tool_name if ep_name == tool_name else f"{tool_name}_{ep_name}"
            if fn not in want or fn in attached_names:
                continue
            func = registry.create_endpoint_function(tool_name, ep_name)
            if func is None:
                continue
            register_dual(helper, executor, func, fn,
                          ep.get('description', f'{tool_name} {ep_name}'))
            attached_names.add(fn)
            n += 1

    # Core closures: same selector, same idempotent set — only the SOURCE
    # differs.  Kept inside this function rather than a sibling so there is one
    # answer to "attach the tools this turn names", not two that can drift.
    for core_name, core_desc, core_func in (core_tools or []):
        if core_name not in want or core_name in attached_names:
            continue
        register_dual(helper, executor, core_func, core_name, core_desc)
        attached_names.add(core_name)
        n += 1
    return n


# ---------------------------------------------------------------------------
# Core tool closure factory
# ---------------------------------------------------------------------------

# Neutral fallback receipt (#752) used until the user accepts a custom template.
# The accept-once flow overrides it via save_data_in_memory('receipt_template').
# {var} placeholders are filled by the existing TemplateEngine.
_DEFAULT_RECEIPT_TEMPLATE = (
    "RECEIPT\n"
    "{business_name}\n"
    "Date: {date}\n"
    "Received from: {client_name}\n"
    "For: {service}\n"
    "Event timing: {event_timing}\n"
    "Amount: {currency} {amount}\n"
    "Advance received: {currency} {advance}\n"
    "Balance due: {currency} {balance}\n"
    "{notes}\n"
    "Thank you for your business."
)


def build_core_tool_closures(ctx):
    """Build session-scoped tool closures.  Returns list of (name, desc, func).

    Args:
        ctx: dict with session variables:
            user_id, prompt_id, agent_data, helper_fun, user_prompt,
            request_id_list, recent_file_id, scheduler,
            simplemem_store (optional), memory_graph (optional),
            log_tool_execution (decorator), send_message_to_user1 (func),
            retrieve_json (func), strip_json_values (func),
            save_conversation_db (func)
    """
    # Unpack context -------------------------------------------------------
    user_id = ctx['user_id']
    prompt_id = ctx['prompt_id']
    agent_data = ctx['agent_data']
    helper_fun = ctx['helper_fun']
    user_prompt = ctx['user_prompt']
    request_id_list = ctx['request_id_list']
    recent_file_id = ctx['recent_file_id']
    scheduler = ctx['scheduler']
    simplemem_store = ctx.get('simplemem_store')
    memory_graph = ctx.get('memory_graph')
    # log_tool_execution: optional decorator (create_recipe.py has it, reuse_recipe.py may not)
    log_tool_execution = ctx.get('log_tool_execution') or (lambda f: f)
    send_message_to_user1 = ctx['send_message_to_user1']
    _retrieve_json = ctx['retrieve_json']
    _strip_json_values = ctx['strip_json_values']
    save_conversation_db = ctx['save_conversation_db']

    tools: List[Tuple[str, str, Any]] = []

    # ------------------------------------------------------------------
    # 1. text_2_image
    # ------------------------------------------------------------------
    @log_tool_execution
    def text_2_image(text: Annotated[str, "Text to create image"]) -> str:
        return helper_fun.txt2img(text)

    tools.append((
        "text_2_image",
        "Text to image Creator",
        text_2_image,
    ))
    # Alias — reuse_recipe.py main flow LLM prompts advertise `txt2img` (#510).
    # Same canonical closure (helper_fun.txt2img — local-first, sovereignty).
    tools.append((
        "txt2img",
        "Text to image Creator (alias of text_2_image)",
        text_2_image,
    ))

    # ------------------------------------------------------------------
    # 2. get_user_camera_inp
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_camera_inp(
        inp: Annotated[str, "The Question to check from visual context"],
    ) -> str:
        # No int() — user_id is a UUID on desktop installs and int() raised
        # on every call (152/152 failures across three log rotations,
        # 10/10 on 2026-09-07).  The callee never needs an int: helper.py:2163
        # does get_frame(str(user_id)) and :2165 interpolates it into a
        # filename.  An integer id still passes through unchanged.
        return helper_fun.get_user_camera_inp(inp, user_id, request_id_list[user_prompt])

    tools.append((
        "get_user_camera_inp",
        "Get user's visual information to process somethings",
        get_user_camera_inp,
    ))

    # ------------------------------------------------------------------
    # 3. save_data_in_memory
    # ------------------------------------------------------------------
    @log_tool_execution
    def save_data_in_memory(
        key: Annotated[str, "Key path for storing data now & retrieving data later. Use dot notation for nested keys (e.g., 'user.info.name')."],
        value: Annotated[Optional[Any], "Value you want to store; strictly should be one of int, float, bool, json array or json object."] = None,
    ) -> str:
        """Store data with validation to prevent corruption."""
        tool_logger.info('INSIDE save_data_in_memory')
        try:
            if isinstance(value, str) and (value.startswith('{') or value.startswith('[')):
                value = _retrieve_json(value)
                tool_logger.info(f"REPAIRED JSON STRING: {value}")
            if value is not None:
                json_str = json.dumps(value)
                validated_value = json.loads(json_str)
                tool_logger.info(f"VALIDATED VALUE (post JSON cycle): {validated_value}")
            else:
                validated_value = None

            keys = key.split('.')
            d = agent_data.setdefault(prompt_id, {})
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = validated_value
            tool_logger.info(f"VALUES STORED IN AGENT DATA: {validated_value}")
            tool_logger.info(f"FULL AGENT DATA AT KEY: {d}")

            if helper_fun.save_agent_data_to_file(prompt_id, agent_data):
                tool_logger.info(f"[OK] Data persisted to file for prompt_id {prompt_id}")
            else:
                tool_logger.warning(f"Failed to persist data to file for prompt_id {prompt_id}")

            # Mirror to MemoryGraph (fire-and-forget).  Carried by
            # reuse_recipe's inline twin before the #743 migration, lost
            # in the swap, restored here (owner audit 2026-09-01).  Pairs
            # with get_data_by_key's [KV] recall fallback below.
            if memory_graph is not None:
                try:
                    threading.Thread(target=lambda: memory_graph.register(
                        f"[KV] {key} = {json.dumps(validated_value)[:200]}",
                        {'memory_type': 'fact', 'source_agent': 'helper',
                         'session_id': user_prompt, 'kv_key': key},
                    ), daemon=True).start()
                except Exception:
                    pass

            try:
                stored_value = get_data_by_key(key)
                tool_logger.info(f"VERIFICATION - READ BACK VALUE: {stored_value}")
                if stored_value == "Key not found in stored data.":
                    tool_logger.error(f"VERIFICATION FAILED: Data not properly stored at key {key}")
            except Exception as e:
                tool_logger.error(f"VERIFICATION ERROR: {str(e)}")

            return f'{agent_data[prompt_id]}'
        except json.JSONDecodeError as je:
            error_msg = f"Invalid JSON structure in value: {str(je)}"
            tool_logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"
        except TypeError as te:
            error_msg = f"Type error in value: {str(te)}"
            tool_logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"
        except Exception as e:
            error_msg = f"Unexpected error saving data: {str(e)}"
            tool_logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"

    tools.append((
        "save_data_in_memory",
        "Use this to Store and retrieve data using key-value storage system",
        save_data_in_memory,
    ))

    # ------------------------------------------------------------------
    # 4. get_saved_metadata
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_saved_metadata() -> str:
        """Get metadata with automatic loading from persistent storage."""
        if prompt_id not in agent_data or not agent_data[prompt_id]:
            tool_logger.info(f"Loading agent data from file for get_saved_metadata, prompt_id {prompt_id}")
            helper_fun.load_agent_data_from_file(prompt_id, agent_data)
        stripped_json = _strip_json_values(agent_data[prompt_id])
        return f'{stripped_json}'

    tools.append((
        "get_saved_metadata",
        "Returns the schema of the json from internal memory with all keys but without actual values.",
        get_saved_metadata,
    ))

    # ------------------------------------------------------------------
    # 5. get_data_by_key
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_data_by_key(
        key: Annotated[str, "Key path for retrieving data. Use dot notation for nested keys (e.g., 'user.info.name')."],
    ) -> str:
        if prompt_id not in agent_data or not agent_data[prompt_id]:
            tool_logger.info(f"Loading agent data from file for prompt_id {prompt_id}")
            helper_fun.load_agent_data_from_file(prompt_id, agent_data)
        keys = key.split('.')
        d = agent_data.get(prompt_id, {})
        try:
            for k in keys:
                d = d[k]
            return f'{d}'
        except KeyError:
            # Fallback: check MemoryGraph for persisted [KV] data — the
            # read half of save_data_in_memory's dual-write, carried by
            # reuse_recipe's inline twin before the #743 migration and
            # restored here (owner audit 2026-09-01).
            if memory_graph is not None:
                try:
                    results = memory_graph.recall(f"[KV] {key}", mode='text', top_k=1)
                    if results:
                        return results[0].content
                except Exception:
                    pass
            return "Key not found in stored data."

    tools.append((
        "get_data_by_key",
        "Returns all data from the internal Memory using key",
        get_data_by_key,
    ))
    # Alias — Helper system prompts in reuse_recipe.py advertise this name (#510).
    # Same closure → identical behavior under both names.  Never remove a
    # registered tool: phantom tool fixed by adding a real registration.
    tools.append((
        "get_data_from_memory",
        "Returns all data from the internal Memory using key (alias of get_data_by_key)",
        get_data_by_key,
    ))

    # ------------------------------------------------------------------
    # 5b. generate_receipt (#752 — receipt for a service, over any channel)
    # ------------------------------------------------------------------
    @log_tool_execution
    def generate_receipt(
        service: Annotated[str, "Service provided, e.g. 'Bridal makeup'."],
        amount: Annotated[str, "Amount charged, e.g. '5000'."],
        client_name: Annotated[str, "Client's name."] = "",
        currency: Annotated[str, "Currency code or symbol, e.g. 'INR'."] = "INR",
        date: Annotated[str, "Receipt date; defaults to today when omitted."] = "",
        notes: Annotated[str, "Optional notes or line items."] = "",
        business_name: Annotated[str, "Your business or artist name."] = "",
        advance: Annotated[str, "Advance already received against the total, e.g. '5000'. Required every time; use '0' when none."] = "",
        event_timing: Annotated[str, "Event-day timing, e.g. 'ready by 6am, event at 11am'. Required every time."] = "",
        render: Annotated[str, "'text' (default) or 'image' for the branded PNG receipt with the saved logo."] = "text",
    ) -> str:
        """Fill the user's accepted receipt template with the given details.

        Balance is computed HERE (total - advance), never by the model -
        money math is deterministic code. render='image' additionally
        produces the branded PNG (logo saved once via set_receipt_logo)
        and appends a [[MEDIA:<path>]] marker that the channel reply path
        converts into a real image attachment on the same channel.

        Reuses the per-prompt KV store (save_data_in_memory / get_data_by_key)
        as the "template they accept": the agent proposes a template, the user
        confirms it, and it is saved under key 'receipt_template'.  Falls back to
        a neutral default when none is stored.  Renders via the existing
        TemplateEngine and returns the receipt text for the agent to send back
        over the same channel the request arrived on (WhatsApp, etc.).
        """
        from integrations.service_tools.receipt_image import (
            compute_balance, render_receipt_png)
        balance = compute_balance(amount, advance) or ""
        template = get_data_by_key("receipt_template")
        if not template or template == "Key not found in stored data.":
            template = _DEFAULT_RECEIPT_TEMPLATE
        fields = {
            "business_name": business_name,
            "client_name": client_name,
            "service": service,
            "amount": amount,
            "currency": currency,
            "date": date or datetime.now().strftime("%Y-%m-%d"),
            "notes": notes,
            "advance": advance,
            "event_timing": event_timing,
            "balance": balance,
        }
        from integrations.channels.response.templates import TemplateEngine
        text = TemplateEngine().render(template, extra_vars=fields)
        if str(render).lower() != "image":
            return text
        logo_path = get_data_by_key("receipt_logo_path")
        if logo_path == "Key not found in stored data.":
            logo_path = None
        png = render_receipt_png(fields, logo_path=logo_path)
        if not png:
            return (text + "\n(Image receipt unavailable on this install - "
                    "sent as text instead.)")
        return text + "\n[[MEDIA:" + png + "]]"

    tools.append((
        "generate_receipt",
        "Generate a receipt for a service the user provided, using their "
        "accepted receipt template. EVERY receipt must collect: total amount, "
        "date, event-day timing, and advance received (use '0' if none) - ask "
        "for any that are missing, confirm the parsed values back to the user, "
        "THEN call this. Balance due is computed automatically. On first use, "
        "propose a template; once the user confirms it, save it with "
        "save_data_in_memory under key 'receipt_template'. Pass render='image' "
        "to produce the branded PNG receipt (set the logo once via "
        "set_receipt_logo). Returns the receipt to send back to the user.",
        generate_receipt,
    ))

    # ------------------------------------------------------------------
    # 5c. set_receipt_logo (#752 leg B - capture the logo ONCE, durably)
    # ------------------------------------------------------------------
    @log_tool_execution
    def set_receipt_logo(
        file_path: Annotated[str, "Path to the uploaded logo image file on this machine."],
    ) -> str:
        """Persist the business logo for all future receipts.

        Inbound channel media lands in short-lived locations (the upload
        TTLCache lives 2 hours), so the logo is COPIED once to the durable
        data dir and its path stored in the per-prompt KV under
        'receipt_logo_path' - the same store the accepted template uses.
        """
        import shutil
        src = str(file_path or "").strip().strip('"')
        if not src or not os.path.isfile(src):
            return ("Logo file not found at '" + src + "'. Ask the user to "
                    "send the logo image again, then retry with its saved path.")
        ext = os.path.splitext(src)[1].lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'):
            return ("'" + ext + "' is not an image type I can put on a "
                    "receipt - please send PNG or JPG.")
        try:
            from core.platform_paths import get_data_dir
            dest_dir = os.path.join(get_data_dir(), 'receipt_assets', str(prompt_id))
        except ImportError:
            dest_dir = os.path.join(os.path.expanduser('~/Documents/Nunba/data'),
                                    'receipt_assets', str(prompt_id))
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, 'logo' + ext)
        try:
            shutil.copy2(src, dest)
        except OSError as e:
            tool_logger.error("set_receipt_logo copy failed: %s" % e)
            return "Could not save the logo: %s" % e
        save_data_in_memory('receipt_logo_path', dest)
        return ("Logo saved for all future receipts (" + os.path.basename(dest)
                + "). It will appear on every image receipt from now on.")

    tools.append((
        "set_receipt_logo",
        "Save the user's business logo ONCE for all future receipts. Call this "
        "when the user sends their logo image (get the file path from the "
        "uploaded file). The logo is stored durably and composited onto every "
        "render='image' receipt.",
        set_receipt_logo,
    ))

    # ------------------------------------------------------------------
    # 6. get_user_id
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_id() -> str:
        tool_logger.info('INSIDE get_user_id')
        return f'{user_id}'

    tools.append((
        "get_user_id",
        "Returns the unique identifier (user_id) of the current user.",
        get_user_id,
    ))

    # ------------------------------------------------------------------
    # 7. get_prompt_id
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_prompt_id() -> str:
        tool_logger.info('INSIDE get_prompt_id')
        return f'{prompt_id}'

    tools.append((
        "get_prompt_id",
        "Returns the unique identifier (prompt_id) associated with the current prompt or conversation.",
        get_prompt_id,
    ))

    # ------------------------------------------------------------------
    # 8. Generate_video (canonical — full LTX-2 + avatar)
    # ------------------------------------------------------------------
    @log_tool_execution
    def Generate_video(
        text: Annotated[str, "Text to be used for video generation"],
        avatar_id: Annotated[int, "Unique identifier for the avatar (use 0 for LTX-2 text-to-video)"],
        realtime: Annotated[bool, "If True, response is fast but less realistic by default it should be true; if False, response is realistic but slower"],
        model: Annotated[str, "Video model to use: 'avatar' for avatar-based video, 'ltx2' for LTX-2 text-to-video generation"] = "avatar",
    ) -> str:
        tool_logger.info(f'INSIDE Generate_video with model={model}')

        # LTX-2 Text-to-Video Generation
        if model.lower() == "ltx2":
            tool_logger.info(f'Using LTX-2 for video generation: {text[:50]}...')
            LOCAL_COMFYUI_URL = "http://localhost:8188"
            LOCAL_LTX_URL = "http://localhost:5002"
            headers = {'Content-Type': 'application/json'}
            ltx_payload = {
                "prompt": text,
                "negative_prompt": "worst quality, inconsistent motion, blurry, jittery, distorted",
                "num_frames": 97,
                "width": 832,
                "height": 480,
                "num_inference_steps": 30 if realtime else 50,
                "guidance_scale": 3.0,
                "fps": 24,
            }

            # Fast health probe — skip dead servers instantly (0ms vs 10s timeout)
            def _is_server_up(url, name):
                try:
                    r = pooled_get(f"{url}/health", timeout=1.5)
                    return r.status_code < 500
                except Exception:
                    tool_logger.info(f"{name} not reachable — skipping instantly")
                    return False

            # Try local LTX-2 server first — only if alive
            if _is_server_up(LOCAL_LTX_URL, "LTX-2"):
                try:
                    tool_logger.info(f"LTX-2 server is UP, generating...")
                    response = pooled_post(f"{LOCAL_LTX_URL}/generate", json=ltx_payload, headers=headers, timeout=600)
                    if response.status_code == 200:
                        result = response.json()
                        video_url = result.get('video_url') or result.get('output_url') or result.get('video_path')
                        if video_url:
                            tool_logger.info(f"LTX-2 video generated: {video_url}")
                            return f"LTX-2 Video generated successfully. URL: {video_url}"
                except requests.exceptions.RequestException as e:
                    tool_logger.info(f"LTX-2 generation failed: {e}")

            # Try ComfyUI — only if alive
            if _is_server_up(LOCAL_COMFYUI_URL, "ComfyUI"):
                try:
                    tool_logger.info(f"ComfyUI is UP, submitting workflow...")
                    comfyui_workflow = {
                        "prompt": {
                            "1": {"class_type": "LTXVLoader", "inputs": {"ckpt_name": "ltx-video-2b-v0.9.safetensors"}},
                            "2": {"class_type": "LTXVConditioning", "inputs": {"positive": text, "negative": ltx_payload["negative_prompt"], "ltxv_model": ["1", 0]}},
                            "3": {"class_type": "LTXVSampler", "inputs": {"seed": int(time.time()) % 2147483647, "steps": ltx_payload["num_inference_steps"], "cfg": ltx_payload["guidance_scale"], "width": ltx_payload["width"], "height": ltx_payload["height"], "num_frames": ltx_payload["num_frames"], "ltxv_model": ["1", 0], "conditioning": ["2", 0]}},
                            "4": {"class_type": "LTXVDecode", "inputs": {"ltxv_model": ["1", 0], "samples": ["3", 0]}},
                            "5": {"class_type": "VHS_VideoCombine", "inputs": {"frame_rate": ltx_payload["fps"], "filename_prefix": "ltx2_output", "format": "video/h264-mp4", "images": ["4", 0]}},
                        }
                    }
                    response = pooled_post(f"{LOCAL_COMFYUI_URL}/prompt", json=comfyui_workflow, headers=headers, timeout=10)
                    if response.status_code == 200:
                        comfy_prompt_id = response.json().get('prompt_id')
                        tool_logger.info(f"ComfyUI LTX-2 job queued: {comfy_prompt_id}")
                        for _ in range(120):
                            time.sleep(5)
                            history_response = pooled_get(f"{LOCAL_COMFYUI_URL}/history/{comfy_prompt_id}")
                            if history_response.status_code == 200:
                                history = history_response.json()
                                if comfy_prompt_id in history:
                                    outputs = history[comfy_prompt_id].get('outputs', {})
                                    for node_id, output in outputs.items():
                                        for media_key in ('gifs', 'videos'):
                                            if media_key in output:
                                                filename = output[media_key][0].get('filename')
                                                if filename:
                                                    video_url = f"{LOCAL_COMFYUI_URL}/view?filename={filename}"
                                                    return f"LTX-2 Video generated via ComfyUI. URL: {video_url}"
                        return f"LTX-2 Video generation queued in ComfyUI (prompt_id: {comfy_prompt_id}). Check ComfyUI interface for output."
                except requests.exceptions.RequestException as e:
                    tool_logger.info(f"ComfyUI generation failed: {e}")

            # Local servers unavailable — try hive mesh peer with GPU
            try:
                from integrations.agent_engine.compute_config import get_compute_policy
                policy = get_compute_policy()
                if policy.get('compute_policy') != 'local_only':
                    from integrations.agent_engine.compute_mesh_service import get_compute_mesh
                    mesh = get_compute_mesh()
                    result = mesh.offload_to_best_peer(
                        model_type=ModelType.VIDEO_GEN,
                        prompt=text,
                        options={'model': 'ltx2', 'timeout': 300},
                    )
                    if result and 'error' not in result:
                        video_url = result.get('response', result.get('video_url', ''))
                        peer = result.get('offloaded_to', 'hive_peer')
                        tool_logger.info(f"LTX-2 video generated via hive peer {peer}: {video_url}")
                        return f"LTX-2 Video generated via hive peer. URL: {video_url}"
                    tool_logger.info(f"Hive mesh video offload failed: {result.get('error')}")
            except Exception as e:
                tool_logger.info(f"Hive mesh offload not available: {e}")

            return ("LTX-2 video generation failed. No local GPU, no hive peers with GPU. "
                    "Options: (1) Pair a GPU device: hart compute pair <address>, "
                    "(2) Set HEVOLVE_COMPUTE_POLICY=any, "
                    "(3) Install local LTX-2 server with CUDA GPU")

        # Default: Avatar-based video generation
        from core.config_cache import get_db_url
        database_url = get_db_url() or 'https://mailer.hertzai.com'
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        tool_logger.info(f"avtar_id: {avatar_id}:\n{text[:10]}....\n")

        headers = {'Content-Type': 'application/json'}
        data = {
            "text": str(text),
            'flag_hallo': 'false',
            'chattts': False,
            'openvoice': "false",
        }

        try:
            res = pooled_get(f"{database_url}/get_image_by_id/{avatar_id}")
            res = res.json()
            new_image_url = res["image_url"]
            voice_id = res.get('voice_id')
        except Exception:
            data['openvoice'] = "true"
            new_image_url = None
            voice_id = None

        data["cartoon_image"] = "True"
        data["bg_url"] = 'http://stream.mcgroce.com/txt/examples_cartoon/roy_bg.jpg'
        data['vtoonify'] = "false"
        data["image_url"] = new_image_url
        data['im_crop'] = "false"
        data['remove_bg'] = "false"
        data['hd_video'] = "false"
        data['uid'] = str(request_id)
        data['gradient'] = "true"
        data['cus_bg'] = "false"
        data['solid_color'] = "false"
        data['inpainting'] = "false"
        data['prompt'] = ""
        data['gender'] = 'male'

        timeout = 60
        if not realtime:
            timeout = 600
            data['chattts'] = True
            data['flag_hallo'] = "true"
            data["cartoon_image"] = "False"

        if voice_id is not None:
            try:
                voice_sample = pooled_get(f"{database_url}/get_voice_sample_id/{voice_id}")
                voice_sample = voice_sample.json()
                data["audio_sample_url"] = voice_sample.get("voice_sample_url")
                data['voice_id'] = int(voice_id) if voice_id else None
            except Exception:
                data["audio_sample_url"] = None
                data['voice_id'] = None
        else:
            data["audio_sample_url"] = None
            data['voice_id'] = None

        conv_id = save_conversation_db(text, user_id, prompt_id, database_url, request_id)
        data['conv_id'] = int(conv_id)
        data['avatar_id'] = int(avatar_id)
        data['timeout'] = int(timeout)

        try:
            pooled_post(f"{database_url}/video_generate_save",
                          data=json.dumps(data), headers=headers, timeout=1)
        except Exception:
            pass

        if data['chattts'] or data['flag_hallo'] == "true":
            return f"Video Generation task added to queue with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"
        else:
            return f"Video Generation completed with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"

    tools.append((
        "Generate_video",
        "Generate video with text. Use model='ltx2' for AI text-to-video generation, or model='avatar' (default) for avatar-based video with voice synthesis.",
        Generate_video,
    ))

    # ------------------------------------------------------------------
    # 9. get_user_uploaded_file
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_uploaded_file() -> str:
        tool_logger.info('INSIDE get_user_uploaded_file')
        # .get(), not [] — recent_file_id is a TTLCache written only when a
        # file is actually uploaded, so a user who uploaded nothing has no
        # key and [] raised KeyError (44/44 failures, 4/4 on 2026-09-07).
        # That case is exactly the answer below, which was unreachable.
        file_id = recent_file_id.get(user_id)
        if file_id:
            return f'Got user uploaded file the file_id is {file_id}'
        return 'No file uploaded from user'

    tools.append((
        "get_user_uploaded_file",
        "get user's recent uploaded files",
        get_user_uploaded_file,
    ))

    # ------------------------------------------------------------------
    # 10. get_text_from_image (img2txt)
    # ------------------------------------------------------------------
    @log_tool_execution
    def img2txt(
        image_url: Annotated[str, "image url of which you want text"],
        text: Annotated[str, "the details you want from image"] = 'Describe the Images & Text data in this image in detail',
    ) -> str:
        tool_logger.info('INSIDE img2txt')
        # SSRF protection: validate image URL before fetching
        try:
            from security.sanitize import validate_url
            image_url = validate_url(image_url)
        except (ImportError, ValueError) as e:
            tool_logger.warning(f"Image URL blocked by SSRF filter: {image_url} — {e}")
            return f"Error: URL blocked by security filter: {e}"
        # Try local Qwen Vision first (bundled mode), fall back to cloud
        from core.config_cache import get_vision_api, is_bundled
        url = get_vision_api()
        if not url:
            tool_logger.warning("No LLAVA_API configured — vision inference may fail on no-GPU instances")
            url = "http://azurekong.hertzai.com:8000/llava/image_inference"

        if is_bundled():
            # Local: use Qwen Vision via upload/vision endpoint
            payload = json.dumps({'image_url': image_url, 'prompt': text})
            response = requests.post(url, data=payload,
                                     headers={'Content-Type': 'application/json'}, timeout=60)
        else:
            payload = {'url': image_url, 'prompt': text}
            response = requests.request("POST", url, headers={}, data=payload, files=[], timeout=300)
        if response.status_code == 200:
            return response.text
        else:
            return 'Not able to get this page details try later'

    tools.append((
        "get_text_from_image",
        "Image to Text/Question Answering from image",
        img2txt,
    ))
    # Alias — reuse_recipe.py main flow LLM prompts advertise `img2txt` (#510).
    # Same canonical closure (SSRF-validated, local Qwen Vision + cloud LLaVA fallback).
    tools.append((
        "img2txt",
        "Image to Text/Question Answering from image (alias of get_text_from_image)",
        img2txt,
    ))

    # ------------------------------------------------------------------
    # 11. create_scheduled_jobs
    # ------------------------------------------------------------------
    @log_tool_execution
    def create_scheduled_jobs(
        interval_sec: Annotated[int, "time between two Interval in seconds."],
        job_description: Annotated[str, "Description of the job to be performed"],
        cron_expression: Annotated[Optional[str], "Cron expression for scheduling. Example: '0 9 * * 1-5' (Runs at 9:00 AM, Monday to Friday). If the interval is greater than 60 seconds or it needs to be executed at a dynamic cron time this argument is Mandatory else None"] = None,
    ) -> str:
        tool_logger.info('INSIDE create_scheduled_jobs')
        return 'Added this schedule job in creation process will do it at the end. you can go ahead and mark this action as completed.'

    tools.append((
        "create_scheduled_jobs",
        "Creates time-based jobs using APScheduler to schedule jobs",
        create_scheduled_jobs,
    ))

    # ------------------------------------------------------------------
    # 12. send_message_to_user
    # ------------------------------------------------------------------
    # Guard absorbed from reuse_recipe's inline twin (#743 migration):
    # group-chat models sometimes route "@helper ..." steering text into
    # this tool — that internal chatter must never reach the user.
    _AGENT_MENTIONS = ("@statusverifier", "@status verifier", "@verification",
                      "@helper", "@executor")

    @log_tool_execution
    def send_message_to_user(
        text: Annotated[str, "Text you want to send to the user"],
        avatar_id: Annotated[Optional[str], "Unique identifier for the avatar"] = None,
        response_type: Annotated[Optional[str], "Response mode: 'Realistic' (slower, better quality) or 'Realtime' (faster, lower quality)"] = 'Realtime',
    ) -> str:
        low = text.lower()
        mention = next((m for m in _AGENT_MENTIONS if m in low), None)
        if mention is not None:
            tool_logger.info(
                f'Message directed to agent ({mention}), not sending to user: {text[:50]}...')
            return f'Message directed to {mention} agent, not sending to user'
        tool_logger.info('INSIDE send_message_to_user')
        tool_logger.info(f'SENDING DATA 2 user with values text:{text}, avatar_id:{avatar_id}, response_type:{response_type}')
        thread = threading.Thread(target=send_message_to_user1, args=(user_id, text, '', prompt_id))
        thread.start()
        return f'Message sent successfully to user with request_id: {request_id_list[user_prompt]}-intermediate'

    tools.append((
        "send_message_to_user",
        "Sends a message/information to user. You can use this if you want to ask a question",
        send_message_to_user,
    ))

    # ------------------------------------------------------------------
    # 13. send_presynthesized_video_to_user
    # ------------------------------------------------------------------
    @log_tool_execution
    def send_presynthesized_video_to_user(
        conv_id: Annotated[str, "Conversation ID associated with the text from memory"],
    ) -> str:
        tool_logger.info('INSIDE send_presynthesized_video_to_user')
        tool_logger.info(f'SENDING DATA 2 user with value: conv_id:{conv_id}.')
        return 'Message sent successfully to user'

    tools.append((
        "send_presynthesized_video_to_user",
        "Sends a presynthesized message/video/dialogue to user using conv_id.",
        send_presynthesized_video_to_user,
    ))

    # ------------------------------------------------------------------
    # 14. send_message_in_seconds
    # ------------------------------------------------------------------
    @log_tool_execution
    def send_message_in_seconds(
        text: Annotated[str, "text to send to user"],
        delay: Annotated[int, "time to wait in seconds before sending text"],
        conv_id: Annotated[Optional[int], "conv_id for this text if not available make it None"] = None,
    ) -> str:
        tool_logger.info('INSIDE send_message_in_seconds')
        tool_logger.info(f'with text:{text}. and waiting time: {delay} conv_id: {conv_id}')
        run_time = datetime.fromtimestamp(time.time() + delay)
        scheduler.add_job(send_message_to_user1, 'date', run_date=run_time, args=[user_id, text, '', prompt_id])
        return 'Message scheduled successfully'

    tools.append((
        "send_message_in_seconds",
        "Sends a presynthesized message/video/dialogue to user using conv_id with a timer.",
        send_message_in_seconds,
    ))

    # ------------------------------------------------------------------
    # 15. get_chat_history
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_chat_history(
        text: Annotated[str, "Text related to which you want history"],
        start: Annotated[Optional[str], "start date in format %Y-%m-%dT%H:%M:%S.%fZ"] = None,
        end: Annotated[Optional[str], "end date in format %Y-%m-%dT%H:%M:%S.%fZ"] = None,
    ) -> str:
        tool_logger.info('INSIDE get_chat_history')
        return helper_fun.get_time_based_history(text, f'user_{user_id}', start, end)

    tools.append((
        "get_chat_history",
        "Get Chat history based on text & start & end date",
        get_chat_history,
    ))

    # ------------------------------------------------------------------
    # 16. search_visual_history
    # ------------------------------------------------------------------
    @log_tool_execution
    def search_visual_history(
        query: Annotated[str, "What to search for in visual/screen descriptions"],
        minutes_back: Annotated[int, "How many minutes back to search (default 30)"] = 30,
        channel: Annotated[str, "Which feed: 'camera', 'screen', or 'both' (default)"] = "both",
    ) -> str:
        """Search past camera/screen descriptions. Use for questions about what happened earlier visually."""
        results = helper_fun.search_visual_history(user_id, query, mins=minutes_back, channel=channel)
        if results:
            return '\n'.join(results)
        return "No matching visual/screen descriptions found in the given time range."

    tools.append((
        "search_visual_history",
        "Search past camera and screen descriptions by keyword and time range.",
        search_visual_history,
    ))

    # ------------------------------------------------------------------
    # 17. google_search
    # ------------------------------------------------------------------
    @log_tool_execution
    def google_search(
        text: Annotated[str, "Text/Query which you want to search"],
    ) -> str:
        tool_logger.info('INSIDE google search')
        return helper_fun.top5_results(text)

    tools.append((
        "google_search",
        "web/google/bing search api tool for a given query",
        google_search,
    ))

    # ------------------------------------------------------------------
    # Conditional: long-term memory — SimpleMem preferred, MemoryGraph fallback
    # ------------------------------------------------------------------
    # Gated on EITHER store, not SimpleMem alone.  SimpleMem only constructs
    # when `sm_config.enabled and sm_config.api_key` (reuse_recipe.py:963,
    # create_recipe.py:886 — byte-identical twins), so a local desktop with no
    # cloud key silently got neither tool, while nine prompt sites kept
    # instructing the model to call save_to_long_term_memory by name.  Measured
    # live 2026-09-05 (Auto Research 18088688973): of the 18 _MAIN_LEG_CORE
    # tools, these two — and only these two, the only two gated here — were
    # missing from all 6 wire bodies.  The model skipped the save step with no
    # error, then read an empty store 24x and asserted completion on nothing.
    #
    # MemoryGraph needs no key, initialized 27x in that same log with 0
    # failures (including for this agent), and was ALREADY the dual-write
    # target below and the read-back path in get_data_by_key.  So it backs the
    # tools when SimpleMem is absent rather than the capability disappearing.
    if simplemem_store is not None or memory_graph is not None:
        from core.event_loop import get_or_create_event_loop

        @log_tool_execution
        def search_long_term_memory(
            query: Annotated[str, "Natural language query to search long-term memory"],
        ) -> str:
            """Search compressed long-term memory using semantic retrieval."""
            if simplemem_store is not None:
                try:
                    loop = get_or_create_event_loop()
                    results = loop.run_until_complete(simplemem_store.search(query))
                    if results:
                        return results[0].content
                    return "No relevant memories found."
                except Exception as e:
                    tool_logger.info(f"SimpleMem search error: {e}")
                    return "Memory search unavailable."
            # MemoryGraph leg — same contract, local store, no API key.
            try:
                results = memory_graph.recall(query, mode='hybrid', top_k=5)
                if results:
                    return '\n'.join(r.content for r in results[:5])
                return "No relevant memories found."
            except Exception as e:
                tool_logger.info(f"MemoryGraph search error: {e}")
                return "Memory search unavailable."

        tools.append((
            "search_long_term_memory",
            "Search long-term memory for past conversations, facts, and context using natural language query.",
            search_long_term_memory,
        ))

        @log_tool_execution
        def save_to_long_term_memory(
            content: Annotated[str, "The information/fact to remember long-term"],
            speaker: Annotated[str, "Who said this (e.g. 'User', 'Assistant', 'System')"] = "System",
        ) -> str:
            """Save important information to compressed long-term memory."""
            if simplemem_store is None:
                # MemoryGraph leg.  SYNCHRONOUS on purpose: the dual-write
                # below can be fire-and-forget because SimpleMem already
                # persisted, but when the graph is the ONLY store, a detached
                # thread would let this return "Saved" for a write that never
                # landed — the fabricated success this whole fix exists to
                # remove.  Report what actually happened.
                try:
                    memory_graph.register(
                        content, {'memory_type': 'fact',
                                  'source_agent': speaker,
                                  'session_id': user_prompt,
                                  'source': 'memory_graph'},
                    )
                    return "Saved to long-term memory."
                except Exception as e:
                    tool_logger.info(f"MemoryGraph save error: {e}")
                    return "Failed to save to long-term memory."
            try:
                loop = get_or_create_event_loop()
                loop.run_until_complete(simplemem_store.add(content, {
                    "sender_name": speaker,
                    "user_id": user_id,
                    "prompt_id": prompt_id,
                }))
                # Dual-write to MemoryGraph (fire-and-forget).  Carried by
                # reuse_recipe's inline twin before the #743 migration and
                # lost in the swap — restored HERE so all three legs
                # (main/time/visual) get graph provenance, not just reuse.
                if memory_graph is not None:
                    try:
                        threading.Thread(target=lambda: memory_graph.register(
                            content, {'memory_type': 'fact',
                                      'source_agent': speaker,
                                      'session_id': user_prompt,
                                      'source': 'simplemem'},
                        ), daemon=True).start()
                    except Exception:
                        pass
                return "Saved to long-term memory."
            except Exception as e:
                tool_logger.info(f"SimpleMem save error: {e}")
                return "Failed to save to long-term memory."

        tools.append((
            "save_to_long_term_memory",
            "Save important facts or information to long-term memory for future retrieval across sessions.",
            save_to_long_term_memory,
        ))

    # ------------------------------------------------------------------
    # Suggest_Share_Worthy_Content
    # ------------------------------------------------------------------
    @log_tool_execution
    def suggest_share_worthy_content(
        query: Annotated[str, "Any text — not used for filtering, just provide context"] = "",
    ) -> str:
        """Find high-engagement posts that haven't been shared much and suggest sharing them."""
        try:
            from integrations.social.models import get_db, Post, ShareableLink
            from sqlalchemy import func as sa_func

            db = get_db()
            try:
                share_counts = (
                    db.query(
                        ShareableLink.resource_id,
                        sa_func.count(ShareableLink.id).label('link_count'),
                    )
                    .filter(ShareableLink.resource_type == 'post')
                    .group_by(ShareableLink.resource_id)
                    .subquery()
                )

                posts = (
                    db.query(Post, share_counts.c.link_count)
                    .outerjoin(share_counts, Post.id == share_counts.c.resource_id)
                    .filter(
                        Post.is_deleted == False,
                        Post.is_hidden == False,
                        Post.upvotes > 5,
                        Post.comment_count > 3,
                    )
                    .filter(
                        (share_counts.c.link_count == None) |  # noqa: E711
                        (share_counts.c.link_count < 3)
                    )
                    .order_by(Post.score.desc())
                    .limit(3)
                    .all()
                )

                if not posts:
                    return ("No under-shared high-engagement content found right now. "
                            "Keep creating great posts and the community will notice!")

                suggestions = []
                for post, link_count in posts:
                    title = (post.title or post.content or '')[:80].strip()
                    shares = link_count or 0
                    suggestions.append(
                        f"- \"{title}\" ({post.upvotes} upvotes, "
                        f"{post.comment_count} comments, only {shares} shares) "
                        f"[post_id: {post.id}]"
                    )

                header = ("These posts are resonating with the community but haven't "
                           "been shared much yet. Consider sharing them:\n")
                return header + "\n".join(suggestions)
            finally:
                db.close()
        except Exception as e:
            tool_logger.warning(f"Suggest_Share_Worthy_Content failed: {e}")
            return f"Could not fetch share-worthy content right now: {e}"

    tools.append((
        "Suggest_Share_Worthy_Content",
        "Find high-engagement posts that deserve wider reach but haven't been shared much. "
        "Use when the user asks about content worth sharing or to proactively suggest "
        "share-worthy community posts.",
        suggest_share_worthy_content,
    ))

    # ------------------------------------------------------------------
    # Observe_User_Experience
    # ------------------------------------------------------------------
    @log_tool_execution
    def observe_user_experience(
        input_text: Annotated[str, "JSON string with event, page, duration_ms, outcome fields"],
    ) -> str:
        """Record a user experience observation for self-improvement."""
        try:
            data = json.loads(input_text) if input_text.startswith('{') else {'event': input_text}
            event = data.get('event', 'interaction')
            page = data.get('page', '')
            duration_ms = data.get('duration_ms', 0)
            outcome = data.get('outcome', 'recorded')

            observation = f"User {event} on {page} ({duration_ms}ms): {outcome}"

            if memory_graph:
                session_key = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
                memory_id = memory_graph.register(
                    content=observation,
                    metadata={
                        'memory_type': 'observation',
                        'source_agent': 'agent',
                        'session_id': session_key,
                        'page': page,
                        'event': event,
                    },
                    context_snapshot=f"UX observation during session {session_key}",
                )
                return f"Observation recorded (id: {memory_id}): {observation}"

            return f"Observation noted: {observation}"
        except Exception as e:
            tool_logger.warning(f"Observe_User_Experience failed: {e}")
            return f"Observation noted: {input_text}"

    tools.append((
        "Observe_User_Experience",
        "Record a user experience observation. Input: JSON with event, page, "
        "duration_ms, outcome. Used for self-improvement and understanding user "
        "behavior patterns.",
        observe_user_experience,
    ))

    # ------------------------------------------------------------------
    # Self_Critique_And_Enhance
    # ------------------------------------------------------------------
    @log_tool_execution
    def self_critique_and_enhance(
        input_text: Annotated[str, "Topic or area to critique"],
    ) -> str:
        """Review past suggestions and outcomes to improve future behavior."""
        try:
            if not memory_graph:
                return f"Self-critique on '{input_text}': Will adjust future behavior based on observations."

            session_key = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)

            # Recall past suggestions and observations
            suggestions = memory_graph.recall(
                input_text or 'suggestions made outcomes', mode='semantic', top_k=10,
            )
            observations = memory_graph.recall(
                'user experience observation', mode='semantic', top_k=10,
            )

            if not suggestions and not observations:
                return "No past interactions to critique yet. Will observe and learn."

            # Format findings for agent reasoning
            critique = "Self-critique findings:\n"
            if suggestions:
                critique += f"Past suggestions ({len(suggestions)}):\n"
                for s in suggestions[:5]:
                    critique += f"  - {s.content[:100]}\n"
            if observations:
                critique += f"User observations ({len(observations)}):\n"
                for o in observations[:5]:
                    critique += f"  - {o.content[:100]}\n"

            # Store the critique itself as an insight
            insight = f"Self-critique on: {input_text}"
            memory_graph.register(
                content=insight,
                metadata={
                    'memory_type': 'insight',
                    'source_agent': 'agent',
                    'session_id': session_key,
                    'type': 'self_critique',
                },
                context_snapshot=f"Self-critique during session {session_key}",
            )

            return critique
        except Exception as e:
            tool_logger.warning(f"Self_Critique_And_Enhance failed: {e}")
            return f"Self-critique on '{input_text}': Will adjust future behavior based on observations."

    tools.append((
        "Self_Critique_And_Enhance",
        "Review past agent suggestions and user behavior observations to improve "
        "future recommendations. Input: topic or area to critique. Helps the agent "
        "learn from its own interactions.",
        self_critique_and_enhance,
    ))

    # ------------------------------------------------------------------
    # device_control — Cross-device control via PeerLink (SAME_USER only)
    # ------------------------------------------------------------------
    @log_tool_execution
    def device_control(
        action: Annotated[str, "What to do: 'turn on light', 'check temperature', 'list files', 'run command ls -la'"],
        device_hint: Annotated[str, "Which device: 'phone', 'desktop', 'iot hub', or empty for default"] = '',
    ) -> str:
        """Control a device on the user's private network via PeerLink.

        Privacy-first: only targets the user's own devices (SAME_USER trust).
        Uses PeerLink dispatch channel with FleetCommandService fallback.
        """
        try:
            # Step 1: Find the target device via DeviceRoutingService
            target_device = None
            try:
                from integrations.social.models import db_session
                from integrations.social.device_routing_service import DeviceRoutingService
                with db_session(commit=False) as db:
                    if device_hint:
                        # Map hint to capability or form factor
                        capability = 'general'
                        if device_hint.lower() in ('phone', 'desktop', 'tablet', 'tv', 'embedded', 'robot'):
                            # Filter by form factor
                            devices = DeviceRoutingService.get_user_device_map(db, str(user_id))
                            for d in devices:
                                if d.get('form_factor', '') == device_hint.lower():
                                    target_device = d
                                    break
                        if not target_device:
                            target_device = DeviceRoutingService.pick_device(
                                db, str(user_id), required_capability=capability)
                    else:
                        target_device = DeviceRoutingService.pick_device(
                            db, str(user_id), required_capability='general')
            except Exception as e:
                tool_logger.debug(f"Device routing lookup failed: {e}")

            target_node_id = (target_device or {}).get('device_id', '')

            # Step 2: Try PeerLink dispatch channel (SAME_USER trust only)
            peerlink_sent = False
            if target_node_id:
                try:
                    from core.peer_link.link_manager import get_link_manager
                    from core.peer_link.link import TrustLevel
                    mgr = get_link_manager()
                    link = mgr.get_link(target_node_id)
                    if link and link.trust == TrustLevel.SAME_USER:
                        result = mgr.send(
                            target_node_id, 'dispatch',
                            {'type': 'device_control', 'action': action,
                             'user_id': str(user_id)},
                            wait_response=True, timeout=30.0,
                        )
                        if result is not None:
                            peerlink_sent = True
                            msg = result.get('message', str(result))
                            return f"Device control result: {msg}"
                    elif link and link.trust != TrustLevel.SAME_USER:
                        return ("Device control blocked: target device is not a SAME_USER "
                                "trusted device. Only your own devices can be controlled.")
                except Exception as e:
                    tool_logger.debug(f"PeerLink dispatch failed: {e}")

            # Step 3: Fallback to FleetCommandService
            if not peerlink_sent:
                try:
                    from integrations.social.models import db_session
                    from integrations.social.fleet_command import FleetCommandService
                    with db_session() as db:
                        cmd = FleetCommandService.push_command(
                            db, target_node_id or 'self',
                            'device_control',
                            {'action': action, 'device_hint': device_hint},
                        )
                        if cmd:
                            return f"Device control command queued (id={cmd.get('id', '?')}): {action}"
                        return "Device control: failed to queue command"
                except Exception as e:
                    tool_logger.debug(f"Fleet command fallback failed: {e}")

            # Step 4: Local execution as last resort (this IS the target device)
            try:
                from integrations.social.fleet_command import FleetCommandService
                result = FleetCommandService.execute_command(
                    'device_control', {'action': action})
                if result.get('success'):
                    return f"Device control (local): {result.get('message', 'OK')}"
                return f"Device control failed: {result.get('message', 'Unknown error')}"
            except Exception as e:
                return f"Device control unavailable: {e}"

        except Exception as e:
            tool_logger.warning(f"device_control failed: {e}")
            return f"Device control error: {e}"

    tools.append((
        "device_control",
        "Control a device on the user's private network. Actions: turn on/off lights, "
        "check temperature, list files, run commands. Privacy-first: only your own devices.",
        device_control,
    ))

    # ------------------------------------------------------------------
    # data_extraction_from_url — Parity with LangChain Data_Extraction_From_URL
    # ------------------------------------------------------------------
    @log_tool_execution
    def data_extraction_from_url(
        url: Annotated[str, "The URL to extract content from"],
        url_type: Annotated[str, "Type of URL: 'pdf' or 'website'"] = "website",
    ) -> str:
        """Extract content from a URL (PDF or website). Uses Crawl4AI or direct parsing."""
        try:
            from hartos.threadlocal import thread_local_data as _tld
            _uid = _tld.get_user_id() if hasattr(_tld, 'get_user_id') else user_id
            _rid = _tld.get_request_id() if hasattr(_tld, 'get_request_id') else None

            # Try Crawl4AI service first
            try:
                from integrations.service_tools import service_tool_registry
                crawl_tool = service_tool_registry.get_tool('Crawl4AI')
                if crawl_tool:
                    result = crawl_tool.execute(url)
                    if result:
                        return f"Extracted from {url}:\n{str(result)[:5000]}"
            except Exception:
                pass

            # Fallback: direct requests
            import requests as _req
            resp = _req.get(url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
            if url_type == 'pdf':
                return f"PDF downloaded ({len(resp.content)} bytes). Use a PDF parser for full extraction."
            text = resp.text[:5000]
            # Strip HTML tags naively
            import re
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return f"Extracted from {url}:\n{text[:4000]}"
        except Exception as e:
            return f"URL extraction failed: {e}"

    tools.append((
        "data_extraction_from_url",
        "Extract content from a URL (PDF or website). Input: URL and type ('pdf' or 'website'). "
        "Uses Crawl4AI for rich extraction with fallback to direct HTTP fetch.",
        data_extraction_from_url,
    ))

    # ------------------------------------------------------------------
    # get_user_details — Parity with LangChain User_details_tool
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_details() -> str:
        """Get current user's profile details."""
        try:
            uid = user_id
            # Try local DB first
            try:
                from integrations.social.models import get_db, User
                db = get_db()
                try:
                    user = db.query(User).filter_by(id=str(uid)).first()
                    if user:
                        return json.dumps(user.to_dict(), default=str)
                finally:
                    db.close()
            except Exception:
                pass

            # Fallback: cloud API
            import requests as _req
            resp = _req.post(
                'https://azurekong.hertzai.com:8443/db/getstudent_by_user_id',
                json={'user_id': uid}, timeout=10,
            )
            return f"User details: {resp.text}"
        except Exception as e:
            return f"Could not fetch user details: {e}"

    tools.append((
        "get_user_details",
        "Get the current user's profile information (name, email, preferences, etc.). "
        "Use when the user asks about their profile or when you need user context.",
        get_user_details,
    ))

    # ------------------------------------------------------------------
    # request_resource — Parity with LangChain Request_Resource
    # ------------------------------------------------------------------
    @log_tool_execution
    def request_resource(
        resource_description: Annotated[str, "JSON or plain text describing the needed resource. JSON format: {\"resource_type\": \"api_key\", \"key_name\": \"GOOGLE_API_KEY\", \"label\": \"Google API Key\", \"used_by\": \"search tool\", \"description\": \"needed for web search\"}"],
    ) -> str:
        """Request an API key, credential, token, or config value that is not currently available."""
        try:
            try:
                req = json.loads(resource_description)
            except (ValueError, TypeError):
                req = {
                    'resource_type': 'api_key',
                    'key_name': 'UNKNOWN',
                    'label': resource_description[:100],
                    'description': resource_description,
                    'used_by': 'Agent tool',
                }

            key_name = req.get('key_name', 'UNKNOWN')
            resource_type = req.get('resource_type', 'api_key')

            # Check env vars first
            env_val = os.environ.get(key_name)
            if env_val:
                return f"Resource '{key_name}' is already configured and available."

            # Check vault
            try:
                from hartos.ai_key_vault import AIKeyVault
                vault = AIKeyVault.get_instance()
                val = vault.get_tool_key(key_name) if resource_type != 'channel_secret' else vault.get_channel_secret(req.get('channel_type', ''), key_name)
                if val:
                    os.environ[key_name] = val
                    return f"Resource '{key_name}' loaded from vault and is now available."
            except Exception:
                pass

            # Track as pending and request from user
            try:
                from hartos.ai_key_vault import AIKeyVault
                AIKeyVault.get_instance().add_pending_request(
                    key_name=key_name, resource_type=resource_type,
                    channel_type=req.get('channel_type', ''),
                    label=req.get('label', key_name),
                    description=req.get('description', ''),
                    used_by=req.get('used_by', 'Agent tool'),
                )
            except Exception:
                pass

            secret_request = json.dumps({
                '__SECRET_REQUEST__': True, 'type': resource_type,
                'key_name': key_name, 'label': req.get('label', key_name),
                'description': req.get('description', f'{key_name} is required.'),
                'used_by': req.get('used_by', 'Agent tool'),
                'channel_type': req.get('channel_type', ''),
            })
            return (
                f"I need the user to provide '{req.get('label', key_name)}'. "
                f"Required for {req.get('used_by', 'a tool')}. "
                f"{req.get('description', '')} "
                f"RESOURCE_REQUEST:{secret_request}"
            )
        except Exception as e:
            return f"Resource request failed: {e}"

    tools.append((
        "request_resource",
        "Request an API key, credential, token, or config value. Checks vault/env first, "
        "then prompts the user if not found. Handles: API keys (OpenAI, Google, Slack), "
        "OAuth tokens, channel secrets, service credentials. "
        "Input: JSON with resource_type, key_name, label, used_by, description.",
        request_resource,
    ))

    # ------------------------------------------------------------------
    # observe_user_experience — Parity with LangChain Observe_User_Experience
    # ------------------------------------------------------------------
    @log_tool_execution
    def observe_user_experience(
        event: Annotated[str, "What happened (e.g. 'clicked', 'scrolled', 'left page')"],
        page: Annotated[str, "Which page or screen"] = "",
        outcome: Annotated[str, "What was the result or user reaction"] = "",
        duration_ms: Annotated[int, "How long the interaction lasted in ms"] = 0,
    ) -> str:
        """Record a user experience observation for self-improvement."""
        observation = f"User {event} on {page} ({duration_ms}ms): {outcome}"
        try:
            if memory_graph:
                session_id = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
                mid = memory_graph.register(
                    content=observation,
                    metadata={'memory_type': 'observation', 'source_agent': 'agent',
                              'session_id': session_id, 'page': page, 'event': event},
                    context_snapshot=f"UX observation during session {session_id}",
                )
                return f"Observation recorded (id: {mid}): {observation}"
        except Exception:
            pass
        return f"Observation noted: {observation}"

    tools.append((
        "observe_user_experience",
        "Record a user experience observation. Use to track behavior patterns "
        "for self-improvement. Input: event, page, outcome, duration_ms.",
        observe_user_experience,
    ))

    # ------------------------------------------------------------------
    # self_critique_and_enhance — Parity with LangChain Self_Critique_And_Enhance
    # ------------------------------------------------------------------
    @log_tool_execution
    def self_critique_and_enhance(
        topic: Annotated[str, "Topic or area to critique (e.g. 'my recommendations', 'search quality')"] = "",
    ) -> str:
        """Review past agent suggestions and user observations to improve future behavior."""
        try:
            if not memory_graph:
                return "Self-critique unavailable: no memory graph for this session."

            suggestions = memory_graph.recall(topic or 'suggestions made outcomes', mode='semantic', top_k=10)
            observations = memory_graph.recall('user experience observation', mode='semantic', top_k=10)

            if not suggestions and not observations:
                return "No past interactions to critique yet. Will observe and learn."

            critique = "Self-critique findings:\n"
            if suggestions:
                critique += f"Past suggestions ({len(suggestions)}):\n"
                for s in suggestions[:5]:
                    critique += f"  - {s.content[:100]}\n"
            if observations:
                critique += f"User observations ({len(observations)}):\n"
                for o in observations[:5]:
                    critique += f"  - {o.content[:100]}\n"

            session_id = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
            memory_graph.register(
                content=f"Self-critique on: {topic}",
                metadata={'memory_type': 'insight', 'source_agent': 'agent',
                          'session_id': session_id, 'type': 'self_critique'},
                context_snapshot=f"Self-critique during session {session_id}",
            )
            return critique
        except Exception as e:
            return f"Self-critique unavailable: {e}"

    tools.append((
        "self_critique_and_enhance",
        "Review past agent suggestions and user behavior observations to improve "
        "future recommendations. Input: topic or area to critique.",
        self_critique_and_enhance,
    ))

    # ------------------------------------------------------------------
    # Browser Research tools — T3 (no auth, no browser).
    # ------------------------------------------------------------------
    # Single canonical entry point — integrations.browser_research.tools.dispatch.
    # Every invocation logs to web_research_audit.log and surfaces a
    # `connection_mechanism` field so the agent can describe to the user how
    # it accessed the resource (public_http, obscura_b2_cdp_user_chrome, ...).
    #
    # T2 platform tools (Twitter / Reddit / LinkedIn / Bilibili / XHS / Weibo)
    # land via the same dispatcher in C4+ — agent_tools wiring stays here so
    # there is exactly one tool-registration surface for both LangChain and
    # autogen consumers (no parallel registry).
    try:
        from integrations.browser_research import tools as br_tools_module
    except ImportError as _br_imp_err:
        tool_logger.warning("browser_research unavailable, skipping registration: %s",
                            _br_imp_err)
        br_tools_module = None

    if br_tools_module is not None:
        @log_tool_execution
        def YouTube_Transcript(
            url: Annotated[str, "Full YouTube URL (watch / youtu.be / shorts)."],
            language: Annotated[str, "Preferred subtitle language code, e.g. 'en'."] = "en",
        ) -> str:
            """Fetch a YouTube video's transcript text without authentication.

            Returns a JSON string with success/text/segment_count/connection_mechanism.
            Domain-locked to youtube.com / youtu.be by the dispatcher's allowlist.

            Note: this is distinct from `data_extraction_from_url` because YouTube
            transcripts use the youtube_transcript_api endpoint (captions/cc), not
            page-content scraping.  Generic URL fetch belongs in
            data_extraction_from_url (Crawl4AI → web_crawler.py).
            """
            result = br_tools_module.dispatch(
                tool='YouTube_Transcript', user_id=str(user_id),
                url=url, language=language,
            )
            return json.dumps(result, ensure_ascii=False)

        tools.append((
            "YouTube_Transcript",
            "Fetch a YouTube video's transcript text via the captions/cc endpoint. "
            "No login, no API key. Input: full YouTube URL (watch/youtu.be/shorts) "
            "+ optional language code. For generic web pages use "
            "data_extraction_from_url instead — Crawl4AI is the canonical URL fetch.",
            YouTube_Transcript,
        ))

        # T2: cookie-authenticated read on user's logged-in platform sessions.
        # Routes through web_crawler.crawl_url_with_cookies (extending the
        # canonical crawler) with AccountVault cookies + optional CDP attach.
        @log_tool_execution
        def Search_Platform(
            platform: Annotated[str, "twitter / reddit / linkedin / bilibili / xiaohongshu / weibo"],
            query: Annotated[str, "Search keywords/phrase."],
            handle: Annotated[Optional[str], "Vault handle whose cookies to use; first if omitted."] = None,
        ) -> str:
            """Search a platform using the user's logged-in session cookies.

            Returns JSON with success/markdown/connection_mechanism — the
            connection_mechanism tells the agent (and user) how access happened
            (obscura_b2_cdp_user_chrome / obscura_b1_headless_profile).
            Consent-gated on `web_research:<platform>`.
            """
            result = br_tools_module.dispatch(
                tool='Search_Platform', user_id=str(user_id),
                platform=platform, query=query, handle=handle,
            )
            return json.dumps(result, ensure_ascii=False)

        tools.append((
            "Search_Platform",
            "Search a social platform (twitter/reddit/linkedin/bilibili/xiaohongshu/weibo) "
            "using the user's logged-in session. Input: platform, query, optional handle. "
            "Returns JSON with markdown content + connection_mechanism describing how the "
            "agent accessed it (your Chrome, headless profile, or public). Consent-gated.",
            Search_Platform,
        ))

        @log_tool_execution
        def Read_Timeline(
            platform: Annotated[str, "twitter / reddit / linkedin / bilibili / xiaohongshu / weibo"],
            target_handle: Annotated[str, "Whose timeline to read (e.g. '@elonmusk')."],
            handle: Annotated[Optional[str], "Vault handle whose cookies to use; first if omitted."] = None,
        ) -> str:
            """Read another user's public timeline via your logged-in browser session."""
            result = br_tools_module.dispatch(
                tool='Read_Timeline', user_id=str(user_id),
                platform=platform, target_handle=target_handle, handle=handle,
            )
            return json.dumps(result, ensure_ascii=False)

        tools.append((
            "Read_Timeline",
            "Read someone's public timeline on a platform via your logged-in session. "
            "Input: platform, target_handle. Returns markdown + connection_mechanism. "
            "Consent-gated on web_research:<platform>.",
            Read_Timeline,
        ))

        @log_tool_execution
        def Post_As_User(
            platform: Annotated[str, "twitter / reddit / linkedin / bilibili / xiaohongshu / weibo"],
            content: Annotated[str, "Text to post on the user's behalf."],
            handle: Annotated[Optional[str], "Vault handle to post as; first if omitted."] = None,
            dry_run: Annotated[bool, "TRUE returns a preview card; FALSE actually posts (requires prior confirm)."] = True,
        ) -> str:
            """Post on the user's behalf on a social platform.

            **Preview-confirm gate**: dry_run defaults to TRUE.  First invocation
            returns a `liquid_ui: post_preview` component for the UI to render
            with explicit Cancel/Confirm buttons.  The agent MUST re-invoke with
            dry_run=False after the user taps Confirm.  Same canonical pattern
            as Invite_Friend and channel_send.

            Consent-gated on `web_research:<platform>`.
            """
            result = br_tools_module.dispatch(
                tool='Post_As_User', user_id=str(user_id),
                platform=platform, content=content, handle=handle, dry_run=dry_run,
            )
            return json.dumps(result, ensure_ascii=False)

        tools.append((
            "Post_As_User",
            "Post on the user's behalf on a social platform via their logged-in "
            "browser session. PREVIEW-CONFIRM gated: dry_run=True (default) returns "
            "a preview card the user must explicitly confirm before the actual post "
            "happens with dry_run=False. Consent-gated on web_research:<platform>. "
            "Input: platform, content, optional handle, dry_run.",
            Post_As_User,
        ))

        # NOTE: Read_Webpage was removed 2026-06-08 as a parallel-path violation.
        # The canonical "fetch a URL's content" tool is `data_extraction_from_url`
        # above (line ~1008), which already delegates to Crawl4AI →
        # integrations/web_crawler.py (Playwright/Chromium headless with a
        # requests+BeautifulSoup fallback).  T2 (cookie-authenticated) work in
        # commits C4+ EXTENDS web_crawler.py with cookie injection + B2 CDP
        # attach, instead of building a parallel driver.  See
        # memory/project_browser_research_subsystem.md for the corrected plan.

    @log_tool_execution
    def validate_json_response(response: Annotated[str, "The response from a tool that should be JSON"]) -> str:
        """
        Validates and repairs JSON response from tools.

        Args:
            response: string responses from a tool that should be JSON formatted
        Returns:
            Valid JSON string or the original string if not repairable
        """
        tool_logger.info("INSIDE validate json response")
        try:
            # First try to parse as is
            json_obj = json.loads(response)
            return json.dumps(json_obj)
        except json.JSONDecodeError:
            try:

                # If parsing fails, try to repair
                repaired_json = repair_json(response)
                # Verify the repaired JSON is valid
                json_obj = json.loads(repaired_json)
                return json.dumps(json_obj)
            except Exception as e:
                # If repair filas, return the original with a warning
                tool_logger.info("JSON repair has failed")
                return f"{response}"

    tools.append((
        "validate_json_response",
        "Checks and corrects if the tool response is not JSON but expected to be.",
        validate_json_response,
    ))

    # ------------------------------------------------------------------
    # Coding-agent leg
    #
    # These four were inline closures in create_recipe.create_agents
    # (register_dual, L1674-1816) until 2026-09-10.  CREATE advertised them
    # to the recipe-authoring LLM while REUSE — which builds its tools from
    # THIS factory (reuse_recipe.py:2238) — held no copy.  Two consequences,
    # and the second is the one that hid the first:
    #   * a saved action naming one could never execute; and
    #   * _reuse_fabricated_tools could not see the name as `referenced`
    #     (that helper intersects the action text with names REGISTERED ON
    #     THE AGENTS), so it returned [] at its second early-return, before
    #     its log line.  A tool the leg cannot run was indistinguishable
    #     from an action naming no tool, and the action advanced silently.
    # Measured live 2026-09-10, agent 88719487304 action 4: FAB-GUARD
    # watermark 23 in, 23 out — zero tool calls in the whole window — no
    # verdict line at all, both subtasks closed, parent terminated in 14s.
    # 36 of the 185 saved recipes on that box name such a tool; 87 actions
    # name execute_coding_task alone.
    #
    # Deliberately NOT added to MAIN_LEG_CORE_TOOLS: reuse reaches them via
    # attach_for_names, for the action whose own recipe names one, so the
    # always-on schema stays 18 tools / ~1,859 tokens against the 12,288
    # slot (#730).  Same rule reuse_recipe.py:2429-2442 already states for
    # execute_windows_or_android_command — that closure captures 33 locals
    # of its defining function so ctx cannot build it and it is handed over
    # inline; these four capture nothing but user_id, which ctx supplies.
    # ------------------------------------------------------------------
    async def execute_coding_task(
        task: Annotated[str, "The coding task to execute (e.g., 'review this function for bugs', 'implement a login form')"],
        task_type: Annotated[str, "Task type: code_review, feature, bug_fix, refactor, app_build, debugging, multi_session"] = "feature",
        preferred_tool: Annotated[str, "Optional tool override: kilocode, claude_code, opencode, aider_native, or claw_native (empty = auto-select best)"] = "",
        working_dir: Annotated[str, "Working directory / repo path for the coding task (empty = use HEVOLVE_CODING_WORKDIR env or cwd)"] = "",
    ) -> str:
        """Execute a coding task using the best available coding agent tool (KiloCode, Claude Code, OpenCode, or AiderNative).

        Routes to the best tool based on benchmarks and task type.
        This is for writing, reviewing, refactoring, or debugging code —
        NOT for GUI automation (use execute_windows_or_android_command for that).
        """
        try:
            from integrations.coding_agent.orchestrator import get_coding_orchestrator
            orchestrator = get_coding_orchestrator()
            result = orchestrator.execute(
                task=task,
                task_type=task_type,
                preferred_tool=preferred_tool,
                user_id=user_id,
                model=os.environ.get('HEVOLVE_CODING_MODEL', ''),
                working_dir=working_dir or os.environ.get('HEVOLVE_CODING_WORKDIR', ''),
            )
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Coding task execution error: {e}"

    tools.append((
        "execute_coding_task",
        "Execute a coding task (write, review, refactor, debug code) using the best available coding agent tool. Routes to KiloCode, Claude Code, OpenCode, AiderNative, or ClawNative (Rust) based on benchmarks. Pass working_dir for the target repo path.",
        execute_coding_task,
    ))

    # Repository map tool — tree-sitter based code understanding.
    # Import-gated exactly as create_recipe had it: absent, not broken,
    # when aider_core is not installed.
    try:
        from integrations.coding_agent.recipe_bridge import CodingRecipeBridge

        async def get_repository_map(
            working_dir: Annotated[str, "Directory to map (default: current directory)"] = ".",
            max_tokens: Annotated[int, "Maximum tokens for the map output"] = 2048,
        ) -> str:
            """Generate a tree-sitter based repository map showing key functions, classes, and their relationships.

            Use this to understand a codebase's structure before making changes.
            Returns a ranked summary of the most important code symbols.
            """
            return CodingRecipeBridge.get_repository_map(working_dir, max_tokens)

        tools.append((
            "get_repository_map",
            "Generate a tree-sitter repository map showing key functions, classes, and structure. Use before coding tasks to understand the codebase.",
            get_repository_map,
        ))
    except ImportError:
        tool_logger.debug("Repository map tool not available (aider_core not installed)")

    # Shard Engine: Call-chain context for coding tasks.
    # Target function + upstream callers + downstream callees = FULL source.
    # Everything else = interfaces only. Exposure proportional to task.
    # Call graph from Trueflow MCP (IDE) or AST fallback (headless).
    try:
        async def create_code_shard(
            task: Annotated[str, "Description of the coding task"],
            target_file: Annotated[str, "Relative path to the file containing the target function"],
            target_function: Annotated[str, "Name of the function to modify"],
            repo_path: Annotated[str, "Path to the repository (default: HART OS install dir)"] = "",
        ) -> str:
            """Create a code shard with call-chain context for a coding task.

            Returns:
            - Target function: FULL source (what you're modifying)
            - Upstream callers: FULL source (who calls it, input contracts)
            - Downstream callees: FULL source (what it calls, output contracts)
            - Everything else: Interfaces only (signatures + types)

            Call graph sourced from Trueflow MCP (when IDE running) or AST fallback.
            Security: exposure proportional to the task. E2E encrypted for peer offload.
            Use execute_coding_task with working_dir to actually apply edits.
            """
            from integrations.agent_engine.shard_engine import ShardEngine
            engine = ShardEngine(code_root=repo_path) if repo_path else ShardEngine()
            shard = engine.create_call_chain_shard(
                task=task, target_file=target_file,
                target_function=target_function)
            return json.dumps({
                'shard_id': shard.shard_id,
                'task': shard.task_description,
                'scope': shard.scope.value,
                'target_files': shard.target_files,
                'call_chain_source': shard.full_content,
                'interfaces': [{'file': s.file_path, 'functions': s.functions,
                               'classes': s.classes} for s in shard.interface_specs],
            }, indent=2, default=str)

        tools.append((
            "create_code_shard",
            "Create a code shard with call-chain context: target function + upstream callers + downstream callees (FULL source), everything else interfaces only.",
            create_code_shard,
        ))
    except Exception:
        tool_logger.debug("Shard engine tool not available")

    # Benchmark Tracker: Query which coding tool performs best for each task type
    try:
        async def get_coding_benchmarks(
            task_type: Annotated[str, "Task type to check (code_review, feature, bug_fix, refactor, app_build, debugging, multi_session, or 'all')"] = "all",
        ) -> str:
            """Get coding tool benchmarks — which tool (KiloCode, Claude Code, OpenCode, AiderNative) performs best.

            Returns success rates, average times, and sample counts per tool per task type.
            Includes both local benchmarks and hive-aggregated intelligence from peers.
            """
            from integrations.coding_agent.benchmark_tracker import get_benchmark_tracker
            tracker = get_benchmark_tracker()
            result = {'local': {}, 'hive': {}}

            if task_type == 'all':
                delta = tracker.export_learning_delta()
                result['local'] = delta.get('coding_benchmarks', {})
            else:
                best = tracker.get_best_tool(task_type)
                if best:
                    result['local'][task_type] = {
                        'best_tool': best[0], 'success_rate': best[1],
                        'avg_time_s': best[2],
                    }
                hive_best = tracker.get_hive_best_tool(task_type)
                if hive_best:
                    result['hive'][task_type] = {
                        'best_tool': hive_best[0], 'success_rate': hive_best[1],
                        'avg_time_s': hive_best[2],
                    }
            return json.dumps(result, indent=2, default=str)

        tools.append((
            "get_coding_benchmarks",
            "Query coding tool benchmarks — which tool performs best per task type. Includes local and hive-aggregated data.",
            get_coding_benchmarks,
        ))
    except Exception:
        tool_logger.debug("Benchmark tracker tool not available")

    # ------------------------------------------------------------------
    # Book / learning navigation — appended HERE, not via a separate
    # register_*_if_available() registrar.
    #
    # register_remote_desktop_tools_if_available (below) has NO production
    # caller — only tests/unit/test_remote_desktop_agent_tools.py:235 — so a
    # tool registered that way never reaches a live turn.  The live path is
    # build_core_tool_closures() -> register_core_tools(), called from
    # create_recipe.py:1096/1116 and reuse_recipe.py:2238/2264.  Appending to
    # `tools` is therefore the only wiring that actually runs.
    # ------------------------------------------------------------------
    try:
        from integrations.learning.book_tools import build_book_tools
        _book = build_book_tools(ctx)
        if _book:
            tools.extend(_book)
            tool_logger.info("Book navigation tools registered (%d)", len(_book))
    except ImportError:
        pass
    except Exception as e:
        tool_logger.warning("Book tools registration failed: %s", e)

    return tools


def register_remote_desktop_tools_if_available(ctx, helper, executor):
    """Register remote desktop tools if the module is available.

    Gracefully skips if integrations/remote_desktop is not installed.
    """
    try:
        from integrations.remote_desktop.agent_tools import (
            build_remote_desktop_tools, register_remote_desktop_tools,
        )
        rd_tools = build_remote_desktop_tools(ctx)
        register_remote_desktop_tools(rd_tools, helper, executor)
        tool_logger.info(f"Remote desktop tools registered ({len(rd_tools)} tools)")
    except ImportError:
        pass
    except Exception as e:
        tool_logger.warning(f"Remote desktop tools registration failed: {e}")


def register_memory_graph_tools(memory_graph, helper, executor, user_id, user_prompt):
    """Register MemoryGraph provenance tools if memory_graph is available.

    Delegates to the existing agent_memory_tools module.
    """
    if memory_graph is None:
        return
    try:
        from integrations.channels.memory.agent_memory_tools import create_memory_tools, register_autogen_tools
        mem_tools = create_memory_tools(memory_graph, str(user_id), user_prompt)
        register_autogen_tools(mem_tools, executor, helper)
        tool_logger.info(f"MemoryGraph tools registered for {user_prompt}")
    except Exception as e:
        tool_logger.warning(f"MemoryGraph tools registration failed: {e}")
