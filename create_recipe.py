"""create_recipe.py"""
# Guard: cx_Freeze frozen builds close stdout/stderr. Must be BEFORE autogen imports.
import sys, os
from core.io_guard import silence_stdio, install_autogen_iostream; silence_stdio()
# #170 — autogen budget constants live in core.constants (single source
# of truth, was hardcoded as max_tokens=3500 in 4 sites here and 3 in
# reuse_recipe.py).  See AUTOGEN_MESSAGE_TOKEN_BUDGET comment for why
# the value is 2500 (was 3500) and how it relates to llama-server's
# 12288 n_ctx per-slot budget under concurrent slots.
from core.constants import (  # noqa: E402  (after io_guard, intentional)
    AUTOGEN_MESSAGE_TOKEN_BUDGET,
    AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
    AUTOGEN_HISTORY_LIMIT,
)

import ast
# autogen is imported lazily (core.optional_import.lazy_module): it drags
# google.api_core (~7.6s) + flaml + the contrib capabilities chain ->
# llmlingua -> torch (~4.2s) at import time, but every autogen.* use in
# this module is INSIDE a function (AST-verified: zero module-level /
# class-base uses).  The proxy keeps all ``autogen.X`` call sites
# byte-for-byte unchanged and pays the cost only when the first agent is
# actually constructed — saving ~11-24s off `import create_recipe` and
# therefore off the backend boot.  See tests/unit/test_lazy_autogen_import.py.
from core.optional_import import lazy_module
autogen = lazy_module("autogen", on_import=install_autogen_iostream)

# Qwen3.5's Jinja chat template rejects system messages mid-conversation:
#   "System message must be at the beginning"
# Autogen's GroupChat sends system-role speaker selection prompts mid-conversation.
# Patch the httpx transport to rewrite system→user before sending to llama-server.
# This catches ALL LLM calls from autogen regardless of which method triggers them.
# No httpx monkey-patch needed.
# Qwen3.5 mid-conversation system messages are handled by autogen's
# role_for_select_speaker_messages='user' parameter (set in GroupChat kwargs).
# The previous monkey-patch that rewrote system→user caused Connection errors
# from double-wrapping httpx.Client.send. Removed entirely.
from typing import Annotated, Optional, Dict, Tuple, List, Any
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from core.http_pool import pooled_get, pooled_post, pooled_patch, pooled_request
from core.port_registry import get_port as _get_llm_port, get_local_llm_url
from core.file_cache import atomic_json_write  # canonical atomic write (tmp + fsync + os.replace)
# NOTE: the module-level `import txaio; from autobahn... import Component, run`
# was removed. The WAMP RPC path (subscribe_and_return) now lives in helper_fun,
# so create_recipe no longer references autobahn/Component at all — the import was
# dead here and it hard-failed `import create_recipe` wherever autobahn isn't
# installed (e.g. CI base install, which does not carry the optional WAMP deps).
# tests/unit/test_lazy_autogen_import.py guards against that import regressing.
import uuid
import asyncio
import traceback
from datetime import datetime
import time
import re
import json
from flask import current_app
try:
    from helper import topological_sort, fix_json, retrieve_json, fix_actions, Action, ToolMessageHandler, strip_json_values, apply_autogen_fix_on_startup, load_vlm_agent_files, PROMPTS_DIR, _is_terminate_msg
except Exception:
    from helper import topological_sort, fix_json, retrieve_json, fix_actions, Action, ToolMessageHandler, strip_json_values, apply_autogen_fix_on_startup, load_vlm_agent_files, _is_terminate_msg
    PROMPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), 'prompts'))
os.makedirs(PROMPTS_DIR, exist_ok=True)
import helper as helper_fun
import threading
from concurrent.futures import ThreadPoolExecutor
# transform_messages / transforms are autogen.agentchat.contrib.capabilities
# submodules — importing them eagerly pulls the SAME heavy chain as autogen
# (llmlingua -> torch via text_compressors).  They are used only inside the
# agent-building functions, so proxy them lazily too (same rationale + test
# as the `autogen` proxy above).
transform_messages = lazy_module(
    "autogen.agentchat.contrib.capabilities.transform_messages")
transforms = lazy_module(
    "autogen.agentchat.contrib.capabilities.transforms")
from json_repair import repair_json

# ─── State-transition stuck-loop detector (#485) ────────────────────
# Tracks the most-recent (last_speaker_name, content_hash) signature per
# user_prompt across consecutive state_transition invocations.  When the
# SAME signature repeats for >= _STATE_TRANSITION_LOOP_THRESHOLD calls in
# a row, we declare the GroupChat stuck and break out cleanly with a
# fallback assistant message + ActionState.TERMINATED.
#
# Live evidence (2026-05-10 22:35-22:38, request 776d9fb0): a Tamil-
# language-switch turn entered a 28+-iteration loop where the LLM
# regurgitated the 137-char ChatInstructor prompt verbatim as Assistant
# content on every turn.  state_transition's routing was correct (Assistant
# → verify, verify → chat_instructor) but autogen's internal speaker
# scheduling kept calling state_transition with last_speaker=Assistant —
# never propagating verify/chat_instructor.  Without a backstop the loop
# runs to autogen's default max_consecutive_auto_reply (50+) and the user
# gets nothing for ~5+ minutes.
#
# This guard is purely defensive — happy-path behaviour is unchanged.  The
# threshold is deliberately conservative (5 identical signatures) to leave
# headroom for legitimate same-speaker sequences (e.g. tool-call chains
# where Assistant emits multiple consecutive messages with different
# content but same name).  The signature includes content hash so genuine
# progress (Assistant emits NEW content) resets the counter.
_STATE_TRANSITION_LOOP_STATE: dict = {}
_STATE_TRANSITION_LOOP_THRESHOLD: int = 5
# #2 (mutate-on-redispatch): when the SAME signature has repeated this many times
# — BEFORE the hard break above — inject ONE escalating nudge so the next turn is
# not byte-identical (breaks the deterministic fixpoint).  The nudge is ADDITIVE:
# the action context + the StatusVerifier JSON-format spec are left intact, we
# only append a "you repeated, take a concrete step + emit the status JSON"
# directive.  _STATE_TRANSITION_NUDGED maps user_prompt -> the action_id already
# nudged, so it fires at most once per stuck action and can never itself loop;
# the threshold hard break stays the backstop if the nudge doesn't take.
_STATE_TRANSITION_LOOP_NUDGE_AT: int = 3
_STATE_TRANSITION_NUDGED: dict = {}
# Goal circuit-breaker: a daemon/autonomous goal whose GroupChat hard-loop-breaks
# this many times across re-dispatches is unfixable by retry (the agent lacks the
# capability, or the fix is out-of-band like a rebuild) — pause it so the daemon
# stops re-dispatching it and the local model is freed for productive flywheel
# goals.  Needed because a loop-break returns a fallback reply, so the daemon's
# own _dispatch_backoff never sees a failure to count (the 686-thrash root).
_GOAL_LOOP_BREAK_COUNT: dict = {}
_GOAL_PARK_AFTER_BREAKS: int = 3

# #485 L3 — consecutive-Assistant counter; at threshold redirect to Helper
# to break attention-collapse loops where Assistant→verify can't escape
# because verify shares the same backend LLM.
_ASSISTANT_STREAK_STATE: dict = {}
_ASSISTANT_STREAK_THRESHOLD: int = 3

# Speakers that are part of the Assistant round-trip and therefore must NOT
# reset the streak above.  Seeing one of these means the lap is still in
# progress, not that anything advanced.
#
# The livelock this closes (live 2026-08-04):
#     ChatInstructor -> Assistant -> StatusVerifier -> ChatInstructor -> ...
# Both partners are "non-Assistant", so an unconditional reset on any
# non-Assistant speaker cleared the counter every lap and the streak never
# reached the threshold — the escalation was unreachable for the one cycle it
# was written to break.  20,920 [ROLE-ORDER-GUARD] lines in a single session,
# still spinning 4 minutes after a one-word message, saturating llama-server.
#
# Executor / Helper are deliberately ABSENT: reaching them means a tool ran or
# the escalation already fired, which is genuine forward progress and SHOULD
# reset.  Any future agent not listed here also resets, preserving the original
# "inverse-of-Assistant is future-proof" intent for everything except the two
# speakers proven to form the cycle.
_ASSISTANT_ROUNDTRIP_SPEAKERS: frozenset = frozenset({'StatusVerifier', 'ChatInstructor'})
def publish_async(topic, message, timeout=2.0):
    """Delegate to the canonical publish_async in hart_intelligence.

    Workers must not eager-import hart_intelligence (deadlocks against
    the canonical loader's import lock); resolve via the singleton
    accessor instead.  No-op if HARTOS hasn't finished loading yet —
    same fall-through the original ImportError branch had.
    """
    from core.safe_hartos_attr import safe_hartos_attr
    _publish = safe_hartos_attr('publish_async')
    if _publish is not None:
        _publish(topic, message, timeout)


def _push_thinking(user_id, text):
    """Push a thinking/progress bubble to the Nunba UI.

    Reuses the same crossbar message format as publish_to_crossbar_new_action_start.
    """
    publish_to_crossbar_new_action_start(text, user_id)


def publish_agent_thought(last_speaker, messages, user_id):
    """Module-level version of the nested ``publish_intermediate_thoughts_to_user``
    closure inside create_agents().

    Every autogen speaker switch flows through ``state_transition`` /
    ``state_transition1`` which call this with (last_speaker, messages,
    user_id). It publishes the latest message to the per-user chat
    topic as a "Thinking" bubble — that's the ONE mechanism that
    streams agent-to-agent chats to the Nunba UI. Don't add parallel
    narration publishers; add callers to this function instead.

    Lives at module level so the Timer-flow ``state_transition1``
    (which is defined in a separate ``create_time_agents`` scope and
    therefore can't see the nested closure) can call the SAME
    publisher without duplicating the logic or creating a second
    thinking-stream on the same Crossbar topic.
    """
    try:
        if not messages:
            return
        last = messages[-1]
        content = last.get('content') if isinstance(last, dict) else ''
        if not content:
            return
        if last_speaker.name in ('UserProxy', 'User'):
            return
        # Skip our own delivery-ack messages to avoid echo loops.
        if ('Message already sent successfully to user with request_id' in content
                or 'Message sent successfully to user with request_id' in content):
            return
        # Pull the real request_id from threadlocal — the /chat handler
        # set it via thread_local_data.set_request_id (hart_intelligence_
        # entry.py:6898 / 7962).  Without this the wire envelope carries
        # the placeholder "123456", which:
        #   - chatbot_routes.drain_thinking_traces(<real_uuid>) misses
        #     because the buffer is keyed under "123456" → empty list →
        #     no thinking_steps embedded in the /chat response;
        #   - the React handler at Demopage.js:1434 drops the trace as
        #     "daemon stale" because traceRequestId !== currentReqId;
        #   - the Android consumer at AbstractChatActivity.java:2127
        #     groups it under "123456" → orphan bucket, never displayed.
        # Fix breaks all three failure modes for both transports.
        from threadlocal import thread_local_data
        from core.peer_link.crossbar_publish import publish_thinking_trace
        publish_thinking_trace(
            text=content, user_id=user_id,
            request_id=thread_local_data.get_request_id() or '',
            bot_type='Agent',
            full_schema=True,
        )
    except Exception as e:
        try:
            current_app.logger.error(f"publish_agent_thought error: {e}")
        except Exception:
            pass

# Smart Ledger — task memory, prerequisites and delegation for every agent.
#
# Imported UNCONDITIONALLY on purpose.  This used to be wrapped in
# `try: ... except ImportError: HAS_SMART_LEDGER = False`, and that flag was
# then read by NOTHING — so a failed import did not disable the ledger, it
# just deferred the failure: create_action_with_ledger went on to call
# get_production_backend() and died with `NameError: name
# 'get_production_backend' is not defined` on the agent-creation path
# (reproduced 2026-08-21 building the assistant for 1_7101).
#
# agent_ledger is first-party HARTOS code vendored in-tree, not a third-party
# extra, and an agent without task memory is not a degraded agent — it is a
# broken one.  So there is nothing to feature-flag: if this import fails the
# build is wrong and it should say so here, loudly, instead of producing
# agents that lose their tasks.  core/__init__.py guarantees the in-tree
# package resolves in a source checkout as well as installed and frozen.
from agent_ledger import (
    SmartLedger, Task, TaskType, TaskStatus, ExecutionMode,
    create_ledger_from_actions, get_production_backend
)
from agent_ledger.factory import create_production_ledger, get_or_create_ledger
# Add to your create_recipe.py after imports
from lifecycle_hooks import (
    initialize_deterministic_actions,
    lifecycle_hook_track_action_assignment,
    lifecycle_hook_track_user_fallback,
    debug_lifecycle_status,
    ActionState,
    get_action_state, safe_set_state, force_state_through_valid_path,
    lifecycle_hook_track_status_verification_request,
    lifecycle_hook_track_fallback_request,
    lifecycle_hook_track_recipe_request,
    lifecycle_hook_track_termination,
    lifecycle_hook_process_verifier_response,
    lifecycle_hook_track_recipe_completion,
    lifecycle_hook_check_all_actions_terminated, StateTransitionError, lifecycle_hook_validate_final_agent_creation,
    sync_action_state_to_ledger,  # Sync ActionState to SmartLedger
    register_ledger_for_session,  # Register ledger for auto-sync
    stall_guard_step,             # No-progress stall tracker (reachable guard)
    cycle_guard_step,             # No-NET-progress tracker: cycling action (#485)
    recipe_correction_directive,  # Escalating "emit ONLY JSON" recipe fix (#89)
    is_recipe_creation_request,   # Deterministic recipe-prompt detector (speaker routing)
    RECIPE_CREATE_PROMPT_PREFIX,  # canonical recipe-prompt prefix (single source)
)

# Import helper_ledger functions for subtask management and ledger awareness
from helper_ledger import (
    add_subtasks_to_ledger,
    check_and_unblock_parent,
    get_pending_subtasks,
    get_default_llm_client
)

# Initialize
initialize_deterministic_actions()

import inspect
import asyncio
import logging
import logging.handlers
import sys
from functools import wraps
import redis
import pickle
import pytz
from PIL import Image

from datetime import timedelta
from lifecycle_hooks import initialize_minimal_lifecycle_hooks
initialize_minimal_lifecycle_hooks()  # Prints integration guide
from cultural_wisdom import get_cultural_prompt

# MCP Integration
from integrations.mcp import load_user_mcp_servers, get_mcp_tools_for_autogen, mcp_registry

# Internal Agent Communication (formerly called A2A, now renamed to avoid confusion with Google's A2A protocol)
from integrations.internal_comm import (
    skill_registry, a2a_context, register_agent_with_skills,
    create_delegation_function, create_context_sharing_function,
    create_context_retrieval_function
)

# Task Delegation Bridge - Integrates A2A with task_ledger for proper state management
from integrations.internal_comm.task_delegation_bridge import TaskDelegationBridge

# AP2 (Agent Protocol 2) - Agentic Commerce
from integrations.ap2 import (
    payment_ledger, get_ap2_tools_for_autogen,
    PaymentStatus, PaymentMethod, PaymentGateway
)

# Agent Lightning - Training and Optimization
from integrations.agent_lightning import (
    instrument_autogen_agent, is_enabled as is_agent_lightning_enabled
)

# SimpleMem - Long-term memory with semantic compression
from integrations.channels.memory.simplemem_store import SimpleMemConfig, HAS_SIMPLEMEM
if HAS_SIMPLEMEM:
    from integrations.channels.memory.simplemem_store import SimpleMemStore

# Expert Agents - Dream Fulfillment Network (96 specialized agents)
from integrations.expert_agents import (
    register_all_experts, get_expert_for_task,
    create_autogen_expert_wrapper, recommend_experts_for_dream
)
from core.platform_paths import get_coding_workspace_dir

# Then add the 4 hooks to your get_response_group while loop
# Then manually add the 4 hooks to your get_response_group while loop
# Set up a dedicated logger that doesn't depend on Flask context
# Use writable log dir: ~/Documents/Nunba/logs in bundled mode, else relative 'logs'
# Use the canonical WRITABLE data dir; NEVER a CWD-relative 'logs' — on the
# embedded OS the CWD is the read-only /nix/store package, so makedirs('logs')
# crashes boot with OSError [Errno 30] Read-only file system (EROFS, which the
# old `except PermissionError` (EACCES) did NOT catch). Catch all OSError.
try:
    from core.platform_paths import get_data_dir
    log_dir = os.path.join(get_data_dir(), 'logs')
    os.makedirs(log_dir, exist_ok=True)
except Exception:
    try:
        log_dir = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'logs')
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        import tempfile
        log_dir = tempfile.gettempdir()  # last resort: a log path must never brick boot

# Single log file with rotation (no more timestamped files that accumulate forever)
log_file = os.path.join(log_dir, "agent_system.log")

# Clean up old timestamped log files from previous versions (keep last 5)
try:
    import glob as _glob
    old_logs = sorted(_glob.glob(os.path.join(log_dir, "agent_system_*.log")))
    for old_log in old_logs[:-5]:  # keep newest 5, delete rest
        try:
            os.remove(old_log)
        except OSError:
            pass
except Exception:
    pass

# Configure the logger
tool_logger = logging.getLogger("agent_logger")
tool_logger.setLevel(logging.DEBUG)

# File handler with rotation (10 MB max size, keep 5 backup files)
file_handler = logging.handlers.RotatingFileHandler(
    log_file,
    maxBytes=10*1024*1024,  # 10 MB
    backupCount=5
)
file_handler.setLevel(logging.DEBUG)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)  # Less verbose on console

# Create formatter with timestamp, level, and message
formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# Add handlers to logger
tool_logger.addHandler(file_handler)
tool_logger.addHandler(console_handler)


def _record_exception(exc, module, function, user_prompt='', action_id=0, **ctx):
    """Fire-and-forget exception recording to centralized collector. Never raises."""
    try:
        from exception_collector import ExceptionCollector
        ExceptionCollector.get_instance().record(
            exc, module=module, function=function,
            user_prompt=user_prompt, action_id=action_id, context=ctx)
    except Exception:
        pass
tool_logger.propagate = False  # Prevent double logging

# Canonical `log_tool_execution` decorator (moved to core.tool_logging
# in #509 so the autogen-registration chokepoint
# `core.labeled_autogen_function.register_labeled_function` can reuse it
# without dragging this heavy module in).  The 40+ existing
# `@log_tool_execution` decorator sites in core/agent_tools.py and
# integrations/channels/agent_tools.py resolve the name via
# ctx['log_tool_execution'] sourced from THIS module's namespace, so the
# re-import here preserves their wiring with zero behavior change.
from core.tool_logging import log_tool_execution  # noqa: E402  (after logger config)


from core.session_cache import TTLCache  # early import — needed before first TTLCache usage below
from core.cache_loaders import (
    load_agent_data,
    load_user_ledger,
    load_user_simplemem,
    load_current_flow,
)

scheduler = BackgroundScheduler()
scheduler.start()

# Zombie task reaper — clears IN_PROGRESS ledger entries that have not
# advanced in HEVOLVE_ZOMBIE_TASK_MAX_AGE_HOURS (default 2h).  The
# reaper hooks the SAME scheduler instance above; no separate daemon
# process.  Failure to register is non-fatal — the dashboard still
# surfaces the staleness via status_reason, the reaper just won't
# auto-clear.  See integrations/agent_engine/zombie_reaper.py for the
# full rationale + reuse map.
try:
    from integrations.agent_engine.zombie_reaper import register_with_scheduler as _register_zombie_reaper
    _register_zombie_reaper(scheduler)
except Exception:
    logging.getLogger(__name__).exception(
        "zombie_reaper registration failed — dashboard will still surface "
        "stalled tasks via status_reason but they will not be auto-reaped"
    )

# atexit shutdown — see reuse_recipe.py:174 for full rationale.  Both
# modules independently instantiate a BackgroundScheduler at import time
# and both share the same "RuntimeError: cannot schedule new futures
# after shutdown" failure mode on interpreter teardown if the scheduler
# thread is allowed to outlive the default ThreadPoolExecutor.
import atexit as _atexit
def _shutdown_create_scheduler():
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    except Exception:
        pass
_atexit.register(_shutdown_create_scheduler)

user_agents: Dict[str, Tuple[Any, Any, Any, Any, Any, Any, Any]] = TTLCache(ttl_seconds=7200, max_size=500, name='create_user_agents')
time_agents = TTLCache(ttl_seconds=7200, max_size=500, name='create_time_agents')
# Mode-aware config_list: cloud/regional use external LLM, flat uses local
# (user's wizard-configured endpoint via HEVOLVE_LOCAL_LLM_URL)
from core.autogen_config import get_autogen_config_list
config_list = get_autogen_config_list()

# Per-request model config override (speculative execution, hive compute routing)
# Canonical implementation lives in helper.py — thin wrapper passes local config_list.
def get_llm_config():
    return helper_fun.get_llm_config(config_list)

# Performance: cached config loading (shared with helper.py, reuse_recipe.py)
from core.config_cache import get_config as _get_config
from core.http_pool import pooled_post, pooled_get, pooled_request
from core.event_loop import get_or_create_event_loop

config = _get_config()
STUDENT_API = config.get('STUDENT_API', '')
ACTION_API = config.get('ACTION_API', '')
# (removed dead module-level redis_client that hardcoded a stale prod host
# azure_all_vms.hertzai.com:6369 with no env override / no fallback — it was
# never referenced; the only `redis_client` uses in this file are
# getattr(backend, 'redis_client') on ledger backends, unrelated. #93)


# Performance: TTL caches replace unbounded global dicts (auto-expire after 2 hours)
agent_data = TTLCache(ttl_seconds=7200, max_size=500, name='create_agent_data', loader=load_agent_data)
user_simplemem = TTLCache(ttl_seconds=7200, max_size=500, name='user_simplemem', loader=load_user_simplemem)
task_time = TTLCache(ttl_seconds=7200, max_size=500, name='task_time')
agent_metadata = TTLCache(ttl_seconds=7200, max_size=500, name='agent_metadata')
final_recipe = TTLCache(ttl_seconds=7200, max_size=500, name='final_recipe')
individual_json = TTLCache(ttl_seconds=7200, max_size=500, name='individual_json')
time_actions = TTLCache(ttl_seconds=7200, max_size=500, name='time_actions')
scheduler_check = TTLCache(ttl_seconds=7200, max_size=500, name='scheduler_check')
vlm_recipes = TTLCache(ttl_seconds=7200, max_size=500, name='vlm_recipes')
# Initialize persistent storage
helper_fun.initialize_persistent_storage(agent_data)

# Schedule periodic backups (optional)
helper_fun.schedule_periodic_backups(agent_data, scheduler)

# Register 96 Expert Agents with skill registry for dream fulfillment
try:
    expert_agents = register_all_experts(skill_registry)
    tool_logger.info(f"Registered {len(expert_agents)} expert agents with skill registry")
except Exception as e:
    tool_logger.error(f"Failed to register expert agents: {e}")
    expert_agents = {}

from core.config_cache import get_db_url
database_url = get_db_url() or 'https://mailer.hertzai.com'


def save_conversation_db(text, user_id, prompt_id, database_url, request_id):
    """Delegate to canonical implementation in helper.py."""
    return helper_fun.save_conversation_db(text, user_id, prompt_id, database_url, request_id)


def send_message_to_user1(user_id, response, inp, prompt_id):
    """Publish an intermediate agent response to the user.

    Deployment-mode-aware (2026-06-09):

    - **Bundled** (sys.frozen — Nunba desktop / installer / embedded):
      Emit directly to the canonical local chat topic
      com.hertzai.hevolve.chat.{user_id} with the schema chat-stream
      subscribers actually parse (text, request_id, prompt_id, bot_type,
      options, page_image_url).  No cloud round-trip.  Subscribers
      (Web SPA Demopage.js, Android RN AutobahnConnectionManager, Nunba
      Python adapter) render the message; downstream TTS/video happen
      via the local chat fan-out (no need to re-trigger here).

    - **Standalone central HARTOS** (sys.frozen absent — Docker, dev):
      POST to https://azurekong.hertzai.com:8443/autogen_response — the
      canonical Kong gateway in front of the chatbot_pipeline backend
      that historically owned this endpoint (handler at
      chatbot_pipeline/chatbot.py:8009).  Same backend as the prior
      aws_rasa.hertzai.com:9890, just via the TLS+auth+rate-limited
      gateway instead of the raw HTTP backend.

    Per user 2026-06-09: ``Agent_status`` field omitted on the local
    path — it was an artefact of the cloud server's per-user session
    dict and isn't needed by local subscribers.

    Always fire-and-forget — callers don't read the return value.
    """
    import sys as _sys
    user_prompt = f'{user_id}_{prompt_id}'
    try:
        request_id = f'{request_id_list[user_prompt]}-intermediate'
    except (KeyError, NameError):
        request_id = f'{user_prompt}-intermediate'

    _bundled = bool(getattr(_sys, 'frozen', False))

    if _bundled:
        # Bundled (Nunba install) — local chat topic, on-device.
        text = str(response or '')
        if not text:
            return
        chat_payload = {
            'text': [text],
            'request_id': request_id,
            'prompt_id': prompt_id,
            'bot_type': 'Custom GPT',
            'options': [],
            'newoptions': [],
            'page_image_url': '',
            'analogy_image_url': '',
            'probe': False,
            'inp': inp,
        }
        try:
            # Canonical worker-safe publisher is the module-level publish_async
            # (create_recipe.py:109, routes via safe_hartos_attr). The previous
            # `from core.message_bus import publish_async` raised
            # ModuleNotFoundError every call (no such module) → bundled mode
            # silently dropped every intermediate chat message.
            publish_async(f'com.hertzai.hevolve.chat.{user_id}', chat_payload)
        except Exception as _e:
            try:
                current_app.logger.debug(
                    f'send_message_to_user1: local publish failed ({_e})')
            except Exception:
                pass
        return

    # Standalone central HARTOS — canonical Kong gateway.
    url = 'https://azurekong.hertzai.com:8443/autogen_response'
    body = json.dumps({
        'user_id': user_id,
        'message': response,
        'inp': inp,
        'request_id': request_id,
    })
    headers = {'Content-Type': 'application/json'}
    try:
        pooled_post(url, data=body, headers=headers, timeout=15)
    except Exception as _e:
        try:
            current_app.logger.debug(
                f'send_message_to_user1: azurekong forward failed ({_e})')
        except Exception:
            pass


def execute_python_file(task_description:str,user_id: int,prompt_id:int,action_entry_point:int=0):
    headers = {'Content-Type': 'application/json'}
    url = f'http://localhost:{_get_llm_port("backend")}/time_agent'
    data = json.dumps({'task_description':task_description,'user_id':user_id,'prompt_id':prompt_id,'action_entry_point':action_entry_point,'request_from':'Reuse'})
    res = pooled_post(url,data=data,headers=headers)
    return 'done'


def time_based_execution(task_description:str,user_id: int,prompt_id:int,action_entry_point:int,actions:list=[]):
    current_app.logger.info(f'INSIDE TIME_BASED_EXECUTION with action_entry_point"{action_entry_point}')
    user_prompt = f'{user_id}_{prompt_id}'
    if user_prompt not in time_agents:
        time_agents[user_prompt] = create_time_agents(user_id,prompt_id,'creator','',actions)

    # author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_id]
    current_time = datetime.now()
    group_chat = time_agents[user_prompt]['time_group_chat']
    time_user = time_agents[user_prompt]['time_user']
    time_manager = time_agents[user_prompt]['time_manager']
    chat_instructor = time_agents[user_prompt]['chat_instructor1']
    time_actions[user_prompt].current_action = action_entry_point
    _action_entry = time_actions[user_prompt].get_action_byaction_id(action_entry_point)
    if _action_entry is None:
        current_app.logger.error(f"Action {action_entry_point} not found in time_actions")
        return
    current_action = _action_entry['action']
    text = f'This is the time now {current_time}\n your overall task description which might span multiple actions: {task_description}\n the current Action to execute: {current_action}'
    result = time_user.initiate_chat(time_manager, message=text,speaker_selection={"speaker": "assistant"}, clear_history=False)
    restart = False
    while True:
        current_app.logger.info('inside Timer while')
        # Same empty-history hazard the reuse loops hit live on 2026-08-30.
        # (What empties it is NOT established -- see #725; transform_messages
        # is ruled out.)  :4385 already guards this way.
        if group_chat.messages and group_chat.messages[-1]['name'] == 'ChatInstructor' and _is_terminate(group_chat.messages[-1]['content']):
            current_app.logger.info(f"group_chat.messages[-2]['content'] {group_chat.messages[-2]['content'][:10]}..")
            json_obj = retrieve_json(group_chat.messages[-2]["content"])
            if json_obj and type(json_obj)==dict and 'status' in json_obj.keys() and json_obj['status'].lower() == 'completed':
                _next_action = time_actions[user_prompt].get_action_byaction_id(time_actions[user_prompt].current_action)
                if _next_action is None:
                    current_app.logger.error(f"Action {time_actions[user_prompt].current_action} not found")
                    break
                current_action = _next_action['action']
                text = f'This is the time now {current_time}\n your overall task description which might span multiple actions: {task_description}\n the current Action to execute: {current_action}'
            else:
                current_app.logger.warning(f'it is not a json object the error is:')
                current_app.logger.info('it is not a json object You should ask @statusverifier to give response in proper format & not move ahead to next action')
                actions_prompt = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action-1)
                text = f'Lets continue the work we were doing, if action is completed then ask @statusverifier Agent to Please tell the status of the action {user_tasks[user_prompt].current_action}:{actions_prompt}'

            result = chat_instructor.initiate_chat(time_manager, message=text,speaker_selection={"speaker": "assistant"}, clear_history=False)
            continue
        if restart == True:
            break
        _check_action = time_actions[user_prompt].get_action_byaction_id(action_entry_point)
        current_app.logger.info(f'checking can_perform_without_user_input from {_check_action} ')
        if _check_action and _check_action.get('can_perform_without_user_input') == 'yes':
            restart = True
            text = 'You can assume things on your own to complete this task'
            result = chat_instructor.initiate_chat(time_manager, message=text,speaker_selection={"speaker": "assistant"}, clear_history=False)

            continue
        break

    last_message = group_chat.messages[-1]
    if _is_terminate(last_message['content']):
        last_message = group_chat.messages[-2]
    #sending response to receiver agent
    if f'message2user'.lower() in last_message['content'].lower():
        try:
            json_obj = retrieve_json(last_message['content'])
            if json_obj and 'message2user' in json_obj:
                last_message['content'] = json_obj['message2user']
                send_message_to_user1(user_id, last_message['content'], task_description, prompt_id)

        except Exception as e:
            current_app.logger.error(f"Error extracting JSON: {e}")
            # Fallback to a basic pattern match if retrieve_json fails
            pattern = r'@user\s*{[\'"]message2user[\'"]\s*:\s*[\'"](.+?)[\'"]}'
            match = re.search(pattern, last_message['content'], re.DOTALL)
            if match:
                last_message['content'] = match.group(1)
                send_message_to_user1(user_id, last_message['content'], task_description, prompt_id)
    elif f'message2'.lower() in last_message['content'].lower():
        try:
            json_obj = retrieve_json(last_message['content'])
            if json_obj and 'message2' in json_obj:
                last_message['content'] = json_obj['message2']
                send_message_to_user1(user_id, last_message['content'], task_description, prompt_id)

        except Exception as e:
            current_app.logger.error(f"Error extracting JSON: {e}")
            # Fallback to a basic pattern match if retrieve_json fails
            pattern = r'@user\s*{[\'"]message2[\'"]\s*:\s*[\'"](.+?)[\'"]}'
            match = re.search(pattern, last_message['content'], re.DOTALL)
            if match:
                last_message['content'] = match.group(1)
                send_message_to_user1(user_id, last_message['content'], task_description, prompt_id)
    # At this point, don't process messages with message2user as they were already sent
    return 'done'



def get_frame(user_id):
    """Delegate to helper.get_frame() — FrameStore first, Redis fallback."""
    return helper_fun.get_frame(user_id)

def get_visual_context(user_id, minutes=2):
    """Get visual context from the past specified minutes"""
    try:
        current_app.logger.info(f'Getting visual context for user {user_id} for past {minutes} minutes')
        visual_context = helper_fun.get_visual_context(user_id, minutes)
        current_app.logger.info(f'GOT RESPONSE AS {visual_context}')
        if not visual_context:
            visual_context = 'User\'s camera is not on. no visual data'
        return visual_context
    except Exception as e:
        current_app.logger.error(f'Error getting visual context: {e}')
        return None

def get_action_user_details(user_id):
    """Thin delegate to the canonical ``core.user_context`` resolver.

    The create_recipe flow runs during INITIAL agent training where
    the prompt uses a simpler action format (no video/screen context
    windows, no dedup) — that's what ``mode='create'`` selects inside
    the canonical resolver. Three inline copies of this function
    (here, in reuse_recipe, in hart_intelligence_entry) previously
    drifted; consolidating to ``core.user_context.get_user_context``
    gives one source of truth plus TTL cache + 1.5s hot-path budget
    for free. There is no Python-side classification of the user's
    message — the draft 0.8B model owns that responsibility.
    """
    from core.user_context import get_user_context
    return get_user_context(user_id=user_id, mode='create')

#called from api when visual task is auto triggered via scheduler
def visual_execution(task_description: str, user_id: int, prompt_id: int):
    current_app.logger.info(f'INSIDE Visual_BASED_EXECUTION')
    user_prompt = f'{user_id}_{prompt_id}'
    frame = get_frame(str(user_id))
    minutes=5
    actions = helper_fun.get_visual_context(user_id,minutes)
    if frame is None or actions is None:
        current_app.logger.info("Camera is OFF or no frame found — skipping visual agent.")
        return

    try:
        author, assistant_agent, executor, group_chat, manager, chat_instructor, agents_object = user_agents[user_prompt]
        current_time = datetime.now()
        text = f'''This is the time now {current_time}
            You are an assistant in a visual execution system. Perform the requested action based on the task context.
            Note: Visual input is available because the user's camera is ON.
            <Last_{minutes}_Minutes_Visual_Context_End>: {actions}
            If the user needs to be informed (e.g., task completed, input needed, error), respond in this exact JSON format:
            {{"message2user": "Your clear and useful message here"}}
            Only send this if you have something meaningful to say.
            Do not interrupt the user unless they have asked for a response or the task cannot proceed without their input.
            You must now perform this task: {task_description}'''
        # Use the existing agent structure
        result = author.initiate_chat(manager, message=text, clear_history=False)
        last_message = group_chat.messages[-1]
        if _is_terminate(last_message['content']):
            if len(group_chat.messages) > 1:
                last_message = group_chat.messages[-2]
            if 'message2user' in last_message['content'].lower():
                try:
                    json_obj = retrieve_json(last_message['content'])
                    if json_obj and 'message2user' in json_obj:
                        send_message_to_user1(user_id, json_obj['message2user'], task_description, prompt_id)
                except Exception as e:
                    current_app.logger.error(f"Error processing visual agent response: {e}")
    except Exception as e:
        current_app.logger.error(f"Error in visual_based_execution: {e}")
    return 'done'

def call_visual_task(task_description: str, user_id: int, prompt_id: int):
    headers = {'Content-Type': 'application/json'}
    url = f'http://localhost:{_get_llm_port("backend")}/visual_agent'

    now = datetime.now()
    action_url = f"{ACTION_API}?user_id={user_id}"
    payload = {}
    headers_api = {}

    response = pooled_request("GET", action_url, headers=headers_api, data=payload)

    if response.status_code == 200:
        api_data = response.json()

        # Filter for Video Reasoning entries
        video_reasoning_entries = [
            obj for obj in api_data if obj.get("zeroshot_label") == 'Video Reasoning'
        ]
        # Execute visual task if at least one Video Reasoning entry is found
        if video_reasoning_entries:

            try:
                data_to_send = json.dumps({

                    'task_description': task_description,
                    'user_id': user_id,
                    'prompt_id': prompt_id,
                    'request_from': 'Create'
                })
                # Send POST request to the external visual agent
                res = pooled_post(url, data=data_to_send, headers=headers, timeout=10)
                current_app.logger.info(f"External visual agent response: {res.status_code}")
                return 'done'
            except Exception as e:
                current_app.logger.error(f"Failed to call external visual agent: {e}")
                # Fallback to internal visual processing
                return visual_execution(task_description, user_id, prompt_id)
    else:
        current_app.logger.info("Using internal visual processing")
        return visual_execution(task_description, user_id, prompt_id)

def parse_date(date_str):
    return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")

class SubscriptionHandler:
    message = None

    async def on_rpc_response(self, session, msg, component):
        current_app.logger.info("Received RPC response: {}".format(msg))
        SubscriptionHandler.message = msg
        await component.stop()  # Stop the component after getting the response




llm_config = {
        "cache_seed": None,
        "config_list": config_list,
        "max_tokens": 1500
    }

def has_pending_tool_calls(messages):
    """Check if the last message contains tool calls that need execution."""
    if not messages:
        return False
    last_msg = messages[-1]
    return (last_msg.get('role') == 'assistant' and
            'tool_calls' in last_msg and
            last_msg['tool_calls'])


def _seed_messages(user_id):
    """Recent shared-history messages used to seed an autogen GroupChat.

    ONE builder for every GroupChat.  create_agents' main group_chat and
    create_time_agents' time_group_chat both start from the same shared
    LangChain/autogen buffer; two copies of this try/except would let the
    two chats drift into different memory of the same conversation.

    Extracted 2026-08-07 fixing a live NameError: create_time_agents
    passed `messages=_seed_msgs` with the comment "same as main
    group_chat", but _seed_msgs is a LOCAL of create_agents — the caller
    copied the use and not the definition, so every create_time_agents
    call raised `NameError: name '_seed_msgs' is not defined` and time
    agents could never be built.

    Best-effort by design: a seeding failure returns [] rather than
    blocking agent creation.
    """
    try:
        from integrations.channels.memory.shared_history import (
            seed_autogen_from_shared_history)
        return seed_autogen_from_shared_history(user_id, max_messages=8)
    except Exception:
        return []


def create_agents(user_id: str,task,prompt_id) -> Tuple[Any, Any, Any, Any, Any, Any, Any]:
    """Create new assistant & user agents for a given user_id"""
    user_prompt = f'{user_id}_{prompt_id}'
    individual_json[user_prompt] = None

    try:
        tool_logger.info("[INIT] Trying to initialise...")

        apply_autogen_fix_on_startup()
    except Exception:
        tool_logger.info("[INFO] Autogen JSON enhancement ready - will be applied when Flask starts")

    # Initialize SimpleMem for this session (from gpt4.1)
    simplemem_store = None
    if HAS_SIMPLEMEM:
        try:
            sm_config = SimpleMemConfig.from_env()
            if sm_config.enabled and sm_config.api_key:
                try:
                    from core.platform_paths import get_simplemem_dir
                    sm_config.db_path = get_simplemem_dir(str(user_prompt))
                except ImportError:
                    sm_config.db_path = f"./simplemem_db/{user_prompt}"
                simplemem_store = SimpleMemStore(sm_config)
                user_simplemem[user_prompt] = simplemem_store
                tool_logger.info(f"[SIMPLEMEM] Initialized for {user_prompt}")
        except Exception as e:
            tool_logger.warning(f"[SIMPLEMEM] Init failed: {e}")

    # Initialize MemoryGraph for provenance-aware memory
    memory_graph = None
    try:
        from integrations.channels.memory.memory_graph import MemoryGraph
        import os
        try:
            from core.platform_paths import get_memory_graph_dir
            graph_db_path = get_memory_graph_dir(user_prompt)
        except ImportError:
            graph_db_path = os.path.join(
                os.path.expanduser("~"), "Documents", "Nunba", "data", "memory_graph", user_prompt
            )
        memory_graph = MemoryGraph(db_path=graph_db_path, user_id=str(user_id))
        tool_logger.info(f"MemoryGraph initialized for {user_prompt}")
    except Exception as e:
        tool_logger.warning(f"MemoryGraph init failed: {e}")

    custom_agents = []
    agents_object = {}
    with open(helper_fun.safe_prompt_path(prompt_id), 'r') as f:
            config = json.load(f)
            list_of_persona = config['flows'][get_current_flow(user_prompt)]['persona']
            current_app.logger.info(f'WORKING persona as {list_of_persona}')

    # #510: populate the canonical persona registry so that the
    # `send_message_to_roles` tool can resolve role→agent mappings
    # during recipe authoring.  Autonomous recipe creation does real
    # multi-persona work while authoring — the personas were captured
    # by gather-requirements before this code runs, so the registry IS
    # populated at the right point.  Same canonical impl in both flows.
    try:
        from core.persona_registry import register_persona_for_session
        _personas_for_registry = config.get('personas') or (
            [{'name': p} if isinstance(p, str) else p
             for p in (list_of_persona or [])]
            if list_of_persona else []
        )
        register_persona_for_session(user_id, prompt_id, _personas_for_registry)
    except Exception:
        tool_logger.warning(
            "persona registry populate failed (create_recipe flow)",
            exc_info=True)

    # Generate or load agent personality
    _agent_personality = None
    try:
        from core.agent_personality import load_personality, generate_personality, save_personality
        _agent_personality = load_personality(str(prompt_id))
        if not _agent_personality:
            _role = config.get('personas', [{}])[0].get('name', 'Assistant') if config.get('personas') else list_of_persona
            _goal = config.get('goal', task if isinstance(task, str) else '')
            _agent_name = config.get('agent_name', '')
            _agent_personality = generate_personality(str(_role), str(_goal), _agent_name)
            save_personality(str(prompt_id), _agent_personality)
            tool_logger.info(f"Generated personality '{_agent_personality.persona_name}' for prompt {prompt_id}")
    except Exception as e:
        tool_logger.warning(f"Personality generation skipped: {e}")

    # Load resonance profile for continuous personality tuning
    _resonance_profile = None
    try:
        from core.resonance_profile import get_or_create_profile
        _resonance_profile = get_or_create_profile(str(user_id))
    except ImportError:
        pass
    except Exception as e:
        tool_logger.debug(f"Resonance profile loading skipped: {e}")

    # Create assistant agent (user_language resolved inside build_personality_prompt)
    # AUTONOMY SIGNAL (live-confirmed root cause, 2026-06-04): daemon / flywheel
    # dispatches stamp request_id with the 'daemon_<goal>' prefix
    # (dispatch.is_genuine_user_request — the canonical discriminator).  This was
    # NEVER threaded to instantiate_assistant_agent, so it defaulted to
    # autonomous=False and every autonomous goal got the assistant prompt
    # "INTERACTIVE MODE: you may ask the user clarifying questions".  The local
    # model then asked for clarification, the StatusVerifier flagged
    # needs-user-input, and the action stalled forever (no user to answer) — the
    # core Gate-1 stall, seen directly in the :8080 capture of a daemon coding
    # goal running in INTERACTIVE mode.  Resolve it from the request_id so
    # autonomous runs are told to use sensible defaults and never block.
    _autonomous_run = False
    try:
        from integrations.agent_engine.dispatch import is_current_request_autonomous
        _autonomous_run = is_current_request_autonomous()
    except Exception:
        _autonomous_run = False
    assistant = instantiate_assistant_agent(
        list_of_persona, user_prompt, personality=_agent_personality,
        resonance_profile=_resonance_profile, autonomous=_autonomous_run)

    # Wrap assistant with Agent Lightning for training and optimization
    if is_agent_lightning_enabled():
        try:
            assistant = instrument_autogen_agent(
                agent=assistant,
                agent_id=f'create_recipe_assistant_{user_prompt}',
                track_rewards=True,
                auto_trace=True
            )
            tool_logger.info(f"Agent Lightning instrumentation applied to assistant for {user_prompt}")
        except Exception as e:
            tool_logger.warning(f"Could not apply Agent Lightning: {e}. Continuing with standard agent.")

    helper = instantiate_helper_agent()
    verify = instantiate_status_verifier_agent(user_prompt)
    executor = instantiate_executor_agent()

    chat_instructor = autogen.UserProxyAgent(
        name="ChatInstructor",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        default_auto_reply="TERMINATE",
        code_execution_config=False,
        is_termination_msg=_is_terminate_msg,
    )

    author = autogen.UserProxyAgent(
        name="UserProxy",
        human_input_mode="NEVER",
        is_termination_msg=lambda x: x.get("content") is not None and not x["content"].strip(),
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )

    context_handling = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=AUTOGEN_HISTORY_LIMIT, keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET, max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE, min_tokens=0),
            ToolMessageHandler(user_tasks=user_tasks, user_prompt=user_prompt),
        ]
    )

    context_handling.add_to_agent(assistant)
    context_handling.add_to_agent(helper)
    context_handling.add_to_agent(executor)
    context_handling.add_to_agent(verify)
    # chat_instructor (UserProxyAgent line 871) was previously NOT attached
    # to the transform.  Result: chat_instructor.initiate_chat(manager,
    # clear_history=False) accumulated message history unboundedly across
    # recipe-request retries within the same turn → llama.cpp 500
    # "Context size has been exceeded" (32 firings/session per langchain.log
    # 2026-05-14).  Attaching here caps chat_instructor's buffer at the
    # same 3500-token / 50-message budget as the other agents.
    context_handling.add_to_agent(chat_instructor)

    agents_object['assistant'] = assistant
    agents_object['helper'] = helper
    agents_object['author'] = author
    agents_object['user'] = author
    agents_object['executor'] = executor
    agents_object['verify'] = verify
    agents_object['chat_instructor'] = chat_instructor

    # for i in config['personas']:
    #     name = i['name']
    #     name = autogen.UserProxyAgent(
    #         name=i['name'],
    #         human_input_mode="NEVER",
    #         default_auto_reply="TERMINATE",
    #         is_termination_msg=lambda x: not (x.get("content") or "").strip(),
    #         max_consecutive_auto_reply=0,
    #         code_execution_config=False,
    #     )
    #     name.description = i['description']
    #     custom_agents.append(name)
    #     agents_object[i['name']] = name

    # --- Core tools (defined once in core/agent_tools.py) ---
    from core.agent_tools import (
        build_core_tool_closures, register_core_tools, register_memory_graph_tools,
        register_dual,
    )
    _tool_ctx = {
        'user_id': user_id, 'prompt_id': prompt_id,
        'agent_data': agent_data, 'helper_fun': helper_fun,
        'user_prompt': user_prompt, 'request_id_list': request_id_list,
        'recent_file_id': recent_file_id, 'scheduler': scheduler,
        'simplemem_store': simplemem_store,
        'memory_graph': memory_graph,
        'log_tool_execution': log_tool_execution,
        'send_message_to_user1': send_message_to_user1,
        'retrieve_json': retrieve_json,
        'strip_json_values': strip_json_values,
        'save_conversation_db': save_conversation_db,
    }
    core_tools = build_core_tool_closures(_tool_ctx)
    register_core_tools(core_tools, helper, assistant)
    register_memory_graph_tools(memory_graph, helper, assistant, user_id, user_prompt)

    # Channel tools: send to channels, register channels, list status, get context
    try:
        from integrations.channels.agent_tools import register_channel_tools
        register_channel_tools(helper, assistant, _tool_ctx)
    except Exception as e:
        tool_logger.debug(f"Channel tools registration skipped: {e}")


    # Unified media generation tools (already DRY)
    try:
        from integrations.service_tools.media_agent import register_media_tools
        register_media_tools(helper, assistant)
    except Exception as e:
        tool_logger.debug(f"Media tools registration skipped: {e}")

    # #510: get_user_details — delegate to canonical core.agent_tools impl
    # (DB lookup + cloud fallback) instead of the limited helper_fun fallback.
    # register_core_tools above (L909) already registered the canonical on
    # this agent pair; this inline registration was OVERWRITING it with the
    # weaker parse_user_id-only impl.  Body now delegates to the canonical.
    _gud_canonical = next(
        (f for n, _, f in core_tools if n == 'get_user_details'), None)

    @log_tool_execution
    def get_user_details() -> str:
        tool_logger.info('INSIDE get user details')
        if _gud_canonical is not None:
            return _gud_canonical()
        # Degraded-env fallback (core_AT closure unavailable)
        return helper_fun.parse_user_id(int(user_id))

    register_dual(helper, assistant, get_user_details,
                  "get_user_details", "Get User details like name, dob, gender")

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
    register_dual(helper, assistant, validate_json_response,
                  "validate_json_response",
                  "Checks and corrects if the tool response is not JSON but expected to be.")

    # Expert agent consultation tool — domain-specific guidance on demand
    @log_tool_execution
    def consult_expert(task_description: Annotated[str, "Describe what expertise you need"]) -> str:
        """Consult a domain expert agent for specialized guidance on the current task.
        Returns expert recommendations. The user will be informed which expert was consulted."""
        try:
            from integrations.expert_agents import match_expert_for_context
            match = match_expert_for_context(task_description, top_k=3, min_score=2)
            if not match:
                return "No domain expert matched this task. Proceeding with general knowledge."
            send_message_to_user1(user_id,
                f"Consulting expert: {match['name']}",
                "Expert consultation", prompt_id)
            return f"Expert guidance from {match['name']}:\n{match['prompt_block']}"
        except Exception as e:
            return f"Expert consultation unavailable: {str(e)}"
    register_dual(helper, assistant, consult_expert,
                  "consult_expert",
                  "Consult a specialized domain expert for the current task")

    # #510: send_message_to_roles — multi-persona broadcast.  Canonical
    # impl lives in core.persona_registry.  Registered in BOTH flows so
    # autonomous-mode recipe creation can do real persona coordination
    # mid-authoring (gather-requirements already populated the registry).
    from core.persona_registry import _send_message_to_roles_impl as _smr_impl
    @log_tool_execution
    def send_message_to_roles(
        role: Annotated[str, "Target persona/role name to deliver the message to"],
        message: Annotated[str, "The question to ask or message to send"],
    ) -> str:
        return _smr_impl(user_id, prompt_id, role, message,
                          publish_fn=publish_async)
    register_dual(helper, assistant, send_message_to_roles,
                  "send_message_to_roles",
                  "Send a message to a specific persona/role within this multi-persona agent (e.g. student/parent/teacher).")

    @log_tool_execution
    async def execute_windows_or_android_command(
            instructions: Annotated[
                str, "Command in plain English to execute on the user's computer or mobile device"],
            os_to_control: Annotated[
                str, "The OS to control: 'windows', 'linux', 'macos', or 'android'"]) -> str:
        """
        Executes a command on any desktop (Windows/Linux/macOS) or Android device.
        Uses pyautogui for desktop GUI automation (cross-platform).
        Returns the response with enhanced VLM agent execution context.
        """



        try:
            tool_logger.info('INSIDE execute_windows_or_android_command')

            # Defensive: agents occasionally pass both args bundled as a
            # dict in the first positional (e.g. {'instructions': '...',
            # 'os_to_control': 'windows'}) instead of as separate kwargs.
            # The declared signature is (instructions: str, os_to_control:
            # str), so downstream code (e.g. simplified_instructions =
            # ' '.join(instructions.lower().strip().split()) ~line 1189)
            # crashes with `AttributeError: 'dict' object has no attribute
            # 'lower'` when the dict slips through.  Coerce here, in one
            # place, so every downstream string op is safe.  Last logged:
            # 2026-05-29 in install agent_system.log.
            if isinstance(instructions, dict):
                if (not os_to_control or os_to_control == 'windows') \
                        and 'os_to_control' in instructions:
                    os_to_control = instructions.get('os_to_control') \
                        or os_to_control or 'windows'
                instructions = (
                    instructions.get('instructions')
                    or instructions.get('command')
                    or str(instructions)
                )

            user_prompt = f'{user_id}_{prompt_id}'
            role_number = get_current_flow(user_prompt)

            import os
            import re
            import json

            # Load and check for existing VLM agent files
            prompts_dir = "prompts"
            tool_logger.info(f"Checking for VLM files in directory: {os.path.abspath(prompts_dir)}")

            existing_vlm_files = []
            if os.path.exists(prompts_dir):
                for file in os.listdir(prompts_dir):
                    if file.startswith(f"{prompt_id}_{role_number}_") and file.endswith("_vlm_agent.json"):
                        existing_vlm_files.append(file)

            tool_logger.info(f"Found existing VLM files: {existing_vlm_files}")

            # Reload VLM agent files to ensure latest
            current_app.logger.info("Reloading VLM agnet files to ensure we have the latest")
            vlm_actions = load_vlm_agent_files(prompt_id, role_number)
            current_app.logger.info(f"Loaded {len(vlm_actions)} VLM agents")

            if vlm_actions:
                current_app.logger.info(f"Loaded {len(vlm_actions)} VLM agents")
                if user_prompt in vlm_recipes:

                    for vlm_action in vlm_actions:
                        action_id = vlm_action.get("action_id")
                        action_exists = False

                        for i, action in enumerate(vlm_recipes[user_prompt]['actions']):
                            if action.get("action_id") == action_id:
                                vlm_recipes[user_prompt]['actions'][i] = vlm_action
                                action_exists = True
                                break

                        if not action_exists:
                            vlm_recipes[user_prompt]['actions'].append(vlm_action)

                    # Update the recipes dictionary
                    final_recipe[prompt_id] = vlm_recipes[user_prompt]

            # Recipe matching logic for reuse
            simplified_instructions = ' '.join(instructions.lower().strip().split())

            def similar_instructions(instr1, instr2, threshold=0.8):
                words1 = set(instr1.lower().split())
                words2 = set(instr2.lower().split())
                if not words1 or not words2:
                    return False

                overlap = len(words1.intersection(words2))
                similarity = overlap / (max(len(words1), len(words2)))
                tool_logger.info(f"Comparing '{instr1}' with '{instr2}' - similarity: {similarity}")
                return similarity >= threshold

            # Check for matching recipe
            matching_recipe = None
            enhanced_instruction = None
            if user_prompt in vlm_recipes:
                for action in vlm_recipes[user_prompt]['actions']:
                    action_text = action.get('action', '')
                    if similar_instructions(instructions, action_text):
                        matching_recipe = action
                        tool_logger.info(f"Found existing recipe for instruction: {action_text}")
                        break

            # Direct file check as backup
            current_action_id = 1
            if user_prompt in user_tasks and hasattr(user_tasks[user_prompt], 'current_action'):
                current_action_id = user_tasks[user_prompt].current_action

            direct_vlm_path = helper_fun.safe_prompt_path(prompt_id, role_number, current_action_id, 'vlm_agent')
            if os.path.exists(direct_vlm_path):
                tool_logger.info(f"Found direct VLM file for current action: {direct_vlm_path}")
                try:
                    with open(direct_vlm_path, 'r') as f:
                        direct_recipe = json.load(f)
                    if similar_instructions(instructions, direct_recipe.get('action', '')):
                        matching_recipe = direct_recipe
                except Exception as e:
                    tool_logger.error(f"Error reading direct VLM file: {e}")

            # Create enhanced instruction if matching recipe found
            enhanced_instruction = None
            if matching_recipe:
                tool_logger.info(f"REUSING command - matched with: {matching_recipe.get('action', '')}")

                enhanced_instruction = f"{instructions}\n\n"
                enhanced_instruction += "Follow these steps from a previous successful execution:\n\n"

                for i, step in enumerate(matching_recipe.get('recipe', [])):
                    step_description = step.get('steps', '').strip()
                    if step_description:
                        enhanced_instruction += f"{i + 1}. {step_description}\n"

                enhanced_instruction += "\nAdapt these steps to the current screen state as needed."
                tool_logger.info(f"Created enhanced instruction with {len(matching_recipe.get('recipe', []))} steps")

            # Prepare VLM message (shared across all tiers)
            crossbar_message = {
                'parent_request_id': request_id_list[user_prompt],
                'user_id': f'{user_id}',
                'prompt_id': prompt_id,
                'instruction_to_vlm_agent': instructions,
                'os_to_control': os_to_control,
                'actions_available_in_os': [],
                'max_ETA_in_seconds': 1800,
                'langchain_server': True
            }

            # Add enhanced instruction if available
            if enhanced_instruction:
                crossbar_message['enhanced_instruction'] = enhanced_instruction
                tool_logger.info(f"Added enhanced instruction to crossbar message")

            # Three-tier VLM execution (Tier 1: in-process, Tier 2: HTTP local)
            from integrations.vlm.vlm_adapter import execute_vlm_instruction, check_vlm_available
            start_time = time.time()
            response = execute_vlm_instruction(crossbar_message)

            if response is None:
                # Tier 3: Crossbar WAMP (central/regional or fallback)
                tool_logger.info("VLM Tier 1/2 unavailable, falling back to Crossbar WAMP")
                topic = f'com.hertzai.hevolve.action.{user_id}'
                tool_logger.info(f'calling {topic} for 5 second')
                response = await helper_fun.subscribe_and_return({'prompt_id': prompt_id}, topic, 2000)
                tool_logger.info(f'Response from call of {topic}: {response}')

                if not response:
                    return 'Ask UserProxy to go to hevolve.ai login and start Nunba - Your Local HART Companion App'

                topic = 'com.hertzai.hevolve.action'
                tool_logger.info(f'calling {topic} for 1800 seconds')
                response = await helper_fun.subscribe_and_return(crossbar_message, topic, 1800000)

            execution_time = time.time() - start_time
            tool_logger.info(f'THIS IS RESPONSE type: {type(response)} value: {response}')

            if not response:
                return f'''⏰ EXECUTION TIMEOUT

                OS: {os_to_control}
                Task: {instructions}

                The {os_to_control} agent did not respond within the timeout period (30 minutes). 
                This could be due to:
                • Complex task requiring more time
                • Network connectivity issues
                • Companion app not running

                Please check your device and try again.'''

            # Process response and extract VLM context
            vlm_context = ""
            vlm_status = "unknown"

            if isinstance(response, dict):
                extracted_responses = response.get('extracted_responses', [])
                vlm_status = response.get('status', 'unknown')
                total_messages = response.get('total_messages', 0)

                if extracted_responses:
                    tool_logger.info(f'Processing {len(extracted_responses)} extracted responses from VLM agent')

                    # Build context from VLM agent's analysis and actions
                    analysis_parts = []
                    action_parts = []

                    for msg in extracted_responses:
                        msg_type = msg.get('type', '')
                        content = msg.get('content', '')

                        if msg_type == 'analysis':
                            analysis_parts.append(f"Analysis: {content}")
                        elif msg_type == 'next_action':
                            if isinstance(content, dict):
                                action_parts.append(f"Action: {json.dumps(content, indent=2)}")
                            else:
                                action_parts.append(f"Action: {content}")

                    # Combine all VLM context
                    vlm_context_parts = []
                    if analysis_parts:
                        vlm_context_parts.append(f"{os_to_control} Agent Analysis:\n" + "\n".join(analysis_parts))
                    if action_parts:
                        vlm_context_parts.append(f"{os_to_control} Agent Actions:\n" + "\n".join(action_parts))

                    vlm_context = "\n\n".join(vlm_context_parts)

                # Create VLM agent file for future reuse if no matching recipe was found
                if not matching_recipe and vlm_status == 'success':
                    try:
                        tool_logger.info("Processing response to create recipe format for future reuse")

                        # Get current action ID
                        action_id = 1
                        if user_prompt in user_tasks and hasattr(user_tasks[user_prompt], 'current_action'):
                            action_id = user_tasks[user_prompt].current_action

                        # Determine file path
                        role_number = get_current_flow(user_prompt)
                        action_id_to_use = action_id
                        base_path = helper_fun.safe_prompt_path(prompt_id, role_number, ext='')

                        # Find next available action_id
                        while os.path.exists(f"{base_path}_{action_id_to_use}_vlm_agent.json"):
                            action_id_to_use += 1

                        vlm_agent_path = f"{base_path}_{action_id_to_use}_vlm_agent.json"
                        os.makedirs(os.path.dirname(vlm_agent_path), exist_ok=True)

                        # Helper functions for processing response data
                        def clean_text(text):
                            lines = text.split('\n')
                            cleaned_lines = []
                            for line in lines:
                                if (not line.strip().startswith("Next Action:") and
                                        not line.strip().startswith("Box ID:") and
                                        not line.strip().startswith("box_centroid_coordinate:") and
                                        not line.strip().startswith("value:")):
                                    cleaned_lines.append(line)
                            return '\n'.join(cleaned_lines)

                        def format_action_text(text):
                            return helper_fun.format_action_text(text)

                        # Process extracted responses into recipe steps
                        recipe_steps = []
                        for msg in extracted_responses:
                            msg_type = msg.get("type", "")
                            msg_content = msg.get("content", "")

                            if msg_type == "analysis":
                                cleaned_content = clean_text(msg_content)
                                if cleaned_content.strip():
                                    recipe_steps.append({
                                        "steps": cleaned_content,
                                        "tool_name": "execute_windows_or_android_command",
                                        "agent_to_perform_this_action": "Helper"
                                    })
                            elif msg_type == "next_action":
                                formatted_content = format_action_text(msg_content)
                                if formatted_content.strip():
                                    recipe_steps.append({
                                        "steps": formatted_content,
                                        "tool_name": "execute_windows_or_android_command",
                                        "agent_to_perform_this_action": "Helper"
                                    })

                        if not recipe_steps:
                            recipe_steps.append({
                                "steps": instructions,
                                "tool_name": "execute_windows_or_android_command",
                                "agent_to_perform_this_action": "Helper"
                            })

                        persona = f"user{user_id}" if user_id else "user"

                        # Create the recipe format
                        recipe_data = {
                            "status": "done",
                            "action": instructions,
                            "fallback_action": f"Perform a Google search using {os_to_control}",
                            "persona": persona,
                            "action_id": action_id_to_use,
                            "recipe": recipe_steps,
                            "can_perform_without_user_input": "no",
                            "scheduled_tasks": [],
                            "metadata": {
                                "user_id": f"redacted <class 'int'>",
                                "os_controlled": os_to_control,
                                "execution_time": execution_time,
                                "vlm_context_available": bool(vlm_context)
                            },
                            "time_took_to_complete": execution_time,
                            "actions_this_action_depends_on": []
                        }

                        # Save the recipe (atomic: tmp + fsync + os.replace)
                        atomic_json_write(vlm_agent_path, recipe_data, indent=4)

                        tool_logger.info(f"Generated recipe data saved to {vlm_agent_path}")

                        # Verify file creation
                        if os.path.exists(vlm_agent_path):
                            file_size = os.path.getsize(vlm_agent_path)
                            tool_logger.info(f"Confirmed VLM file exists with size: {file_size} bytes")

                    except Exception as e:
                        tool_logger.error(f'Error creating VLM agent file: {e}')
                        tool_logger.error(traceback.format_exc())

                # Generate appropriate response based on status
                status_responses = {
                    'success': f"""✅ COMMAND EXECUTED SUCCESSFULLY

    OS: {os_to_control}
    Task: {instructions}

    SUMMARY OF {os_to_control} AGENT EXECUTION CONTEXT:
    {vlm_context if vlm_context else 'Command executed successfully.'}

    PERFORMANCE METRICS:
    • Status: SUCCESS (confirmed by {os_to_control} agent)
    • Duration: {execution_time:.2f} seconds
    • Steps Completed: {total_messages}
    • Recipe {'Reused' if matching_recipe else 'Created'}: {'✓' if matching_recipe else '✓ (New)'}

    The {os_to_control} agent has confirmed successful execution.""",

                    'error': f"""❌ COMMAND EXECUTION ERROR

    OS: {os_to_control}  
    Task: {instructions}

    ERROR DETAILS:
    {vlm_context if vlm_context else 'Error occurred during execution.'}

    DIAGNOSTIC INFO:
    • Status: ERROR (identified by {os_to_control} agent)
    • Duration: {execution_time:.2f} seconds
    • Steps Attempted: {total_messages}

    Please review the error details above for troubleshooting.""",

                    'completed': f"""✅ COMMAND COMPLETED

    OS: {os_to_control}
    Task: {instructions}

    COMPLETION SUMMARY:
    {vlm_context if vlm_context else 'Task completed successfully.'}

    EXECUTION METRICS:
    • Status: COMPLETED (confirmed by {os_to_control} agent)
    • Duration: {execution_time:.2f} seconds  
    • Total Steps: {total_messages}

    The {os_to_control} agent has completed the execution sequence."""
                }

                return status_responses.get(vlm_status, f""" COMMAND EXECUTION FINISHED

    OS: {os_to_control}
    Task: {instructions}
    Status: {vlm_status.upper()}

    EXECUTION CONTEXT:
    {vlm_context if vlm_context else 'Limited execution information available.'}

    SUMMARY:
    • Duration: {execution_time:.2f} seconds
    • Total Steps: {total_messages}

    Please review the {os_to_control} agent's assessment above.""")

            else:
                # Handle legacy or non-dict responses
                tool_logger.warning(f'Received non-dict response: {type(response)}')
                return f"""⚠️ LEGACY RESPONSE FORMAT

    OS: {os_to_control}
    Task: {instructions}

    Response: {str(response)}

    Note: Received response in legacy format. Consider updating the {os_to_control} companion app."""

        except Exception as e:
            error_message = traceback.format_exc()
            tool_logger.error(f"Error executing command:\n{error_message}")

            # Provide specific error guidance
            if 'Failed to capture screenshot' in str(e):
                return f""" COMPANION APP REQUIRED

    OS: {os_to_control}
    Task: {instructions}

    Nunba - Your Local HART Companion App is not running on your {os_to_control} device.

    STEPS TO RESOLVE:
    1. Open Nunba - Your Local HART Companion App
    2. Ensure it's connected and running
    3. Try the command again

    Error: {str(e)}"""
            else:
                return f"""⚠️ SYSTEM ERROR

    OS: {os_to_control}
    Task: {instructions}
    Error: {str(e)}

    A system error occurred while communicating with the {os_to_control} agent. Please try again or contact support if the issue persists."""



    # Register the enhanced function
    register_dual(helper, assistant, execute_windows_or_android_command,
                  "execute_windows_or_android_command",
                  "Processes user-defined commands on a personal Windows or Android system and returns detailed computer/mobile use agent execution context.")

    # Coding Agent Aggregator: Route coding tasks to best CLI tool
    # This is a LEAF tool — calls external subprocess (kilocode/claude/opencode),
    # never re-dispatches to /chat. Safe from callback loops.
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
            import json
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Coding task execution error: {e}"

    register_dual(helper, assistant, execute_coding_task,
                  "execute_coding_task",
                  "Execute a coding task (write, review, refactor, debug code) using the best available coding agent tool. Routes to KiloCode, Claude Code, OpenCode, AiderNative, or ClawNative (Rust) based on benchmarks. Pass working_dir for the target repo path.")

    # Repository map tool — tree-sitter based code understanding
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

        register_dual(helper, assistant, get_repository_map,
                      "get_repository_map",
                      "Generate a tree-sitter repository map showing key functions, classes, and structure. Use before coding tasks to understand the codebase.")
        tool_logger.info("Registered get_repository_map tool")
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
            import json
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

        register_dual(helper, assistant, create_code_shard,
                      "create_code_shard",
                      "Create a code shard with call-chain context: target function + upstream callers + downstream callees (FULL source), everything else interfaces only.")
        tool_logger.info("Registered shard engine tool (create_code_shard)")
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
            import json
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

        register_dual(helper, assistant, get_coding_benchmarks,
                      "get_coding_benchmarks",
                      "Query coding tool benchmarks — which tool performs best per task type. Includes local and hive-aggregated data.")
        tool_logger.info("Registered get_coding_benchmarks tool")
    except Exception:
        tool_logger.debug("Benchmark tracker tool not available")

    # MCP Integration: Load and register user-provided MCP server tools
    try:
        tool_logger.info("Loading user-provided MCP servers...")
        num_servers = load_user_mcp_servers()

        if num_servers > 0:
            tool_logger.info(f"Successfully loaded {num_servers} MCP servers")

            # Get all MCP tool functions
            mcp_tools = mcp_registry.get_all_tool_functions()
            tool_logger.info(f"Discovered {len(mcp_tools)} MCP tools")

            # Register each MCP tool with the agents
            for tool_name, tool_func in mcp_tools.items():
                # Get tool definition for description
                tool_defs = mcp_registry.get_tool_definitions()
                tool_def = next((t for t in tool_defs if t['name'] == tool_name), None)

                if tool_def:
                    description = tool_def.get('description', f'MCP tool: {tool_name}')
                    register_dual(helper, assistant, tool_func, tool_name, description)
                    tool_logger.info(f"Registered MCP tool: {tool_name}")
        else:
            tool_logger.info("No MCP servers configured - continuing with default tools")
    except Exception as e:
        tool_logger.warning(f"MCP integration error (non-critical): {e}")
        # Continue with default tools if MCP fails

    # Service Tools: Register HTTP microservice tools (Crawl4AI, AceStep, etc.)
    # Mirrors reuse_recipe.py:2335-2354 — sync so CREATE mode exposes the
    # same crawl4ai/acestep/omniparser surface as REUSE.  Without this the
    # gather LLM has no real tool to map "fetch a webpage" onto and invents
    # fake tool names (2026-05-12 IPL refusal forensic).
    try:
        from integrations.service_tools import (
            service_tool_registry, Crawl4AITool, AceStepTool,
            SeoAuditTool, GhPrTool)

        Crawl4AITool.register()   # port 11235
        AceStepTool.register()    # port 8001
        SeoAuditTool.register()   # native in-process (no port)
        GhPrTool.register()       # native in-process (no port)
        service_tool_registry.load_config()  # load any user-added tools from service_tools.json

        svc_tools = service_tool_registry.get_all_tool_functions()
        svc_defs = service_tool_registry.get_tool_definitions()

        for tool_name, tool_func in svc_tools.items():
            tool_def = next((d for d in svc_defs if d['name'] == tool_name), None)
            if tool_def:
                description = tool_def.get('description', f'Service tool: {tool_name}')
                register_dual(helper, assistant, tool_func, tool_name, description)
                tool_logger.info(f"Registered service tool: {tool_name}")
    except Exception as e:
        tool_logger.warning(f"Service tools integration error (non-critical): {e}")

    # Internal Agent Communication: Register agents and their skills for in-process communication
    try:
        tool_logger.info("Initializing Internal Agent Communication (skill-based delegation)...")

        # Define agent skills
        agent_skills = {
            'assistant': [
                {'name': 'task_coordination', 'description': 'Coordinating complex multi-step tasks', 'proficiency': 0.95},
                {'name': 'decision_making', 'description': 'Making strategic decisions', 'proficiency': 0.9},
                {'name': 'context_management', 'description': 'Managing conversation context', 'proficiency': 0.9}
            ],
            'helper': [
                {'name': 'tool_execution', 'description': 'Executing various tools and functions', 'proficiency': 1.0},
                {'name': 'data_processing', 'description': 'Processing and transforming data', 'proficiency': 0.95},
                {'name': 'external_api', 'description': 'Interacting with external APIs', 'proficiency': 0.9}
            ],
            'executor': [
                {'name': 'code_execution', 'description': 'Executing code safely', 'proficiency': 1.0},
                {'name': 'computation', 'description': 'Performing complex computations', 'proficiency': 0.95},
                {'name': 'data_analysis', 'description': 'Analyzing data and generating insights', 'proficiency': 0.9}
            ],
            'verify': [
                {'name': 'status_verification', 'description': 'Verifying task completion status', 'proficiency': 0.95},
                {'name': 'quality_assurance', 'description': 'Ensuring output quality', 'proficiency': 0.9},
                {'name': 'validation', 'description': 'Validating results and outputs', 'proficiency': 0.9}
            ]
        }

        # Register agents with their skills
        for agent_name, skills in agent_skills.items():
            register_agent_with_skills(agent_name, skills)
            tool_logger.info(f"Registered {agent_name} with {len(skills)} skills")

        # Add A2A delegation tool to assistant with task_ledger integration
        @log_tool_execution
        def delegate_to_specialist(task: Annotated[str, "Description of the task to delegate"],
                                  required_skills: Annotated[List[str], "List of skills required (e.g., ['code_execution', 'data_analysis'])"],
                                  context: Annotated[Optional[Dict], "Optional context to pass to the specialist agent"] = None) -> str:
            """Delegate a task to a specialist agent based on required skills with full task_ledger tracking"""

            # Try to use TaskDelegationBridge for proper state management
            if user_prompt in user_delegation_bridges and user_prompt in user_tasks:
                bridge = user_delegation_bridges[user_prompt]
                action_tracker = user_tasks[user_prompt]

                # Try to get current task ID from action tracker
                try:
                    current_action_idx = action_tracker.current_index if hasattr(action_tracker, 'current_index') else 0
                    current_task_id = f"action_{current_action_idx + 1}"

                    # Check if this task exists in ledger
                    ledger = user_ledgers[user_prompt]
                    if ledger.get_task(current_task_id):
                        # Use bridge for delegation with full tracking
                        delegation_id = bridge.delegate_task_with_tracking(
                            parent_task_id=current_task_id,
                            from_agent='assistant',
                            task_description=task,
                            required_skills=required_skills,
                            context=context
                        )

                        if delegation_id:
                            status = bridge.get_delegation_status(delegation_id)
                            tool_logger.info(f"Task delegated with tracking: {delegation_id}")
                            return json.dumps({
                                'success': True,
                                'delegation_id': delegation_id,
                                'message': f'Task delegated to {status["delegation"]["to_agent"]} with full tracking',
                                'parent_task_blocked': True,
                                'child_task_created': True,
                                'status': status
                            }, indent=2)
                except Exception as e:
                    tool_logger.warning(f"Could not use TaskDelegationBridge: {e}. Falling back to standard delegation.")

            # Fallback to standard delegation (backward compatible)
            delegation_func = create_delegation_function('assistant')
            return delegation_func(task, required_skills, context)

        register_dual(helper, assistant, delegate_to_specialist,
                      "delegate_to_specialist",
                      "Delegate complex tasks to specialist agents based on required skills")

        # #510: same canonical behavior as reuse_recipe — autonomous-mode
        # recipe creation does real work, and shared context should be
        # queryable later (`recall_memory` etc.).  Both flows persist the
        # insight to MemoryGraph (fire-and-forget thread).
        @log_tool_execution
        def share_context_with_agents(context_key: Annotated[str, "Unique identifier for the context"],
                                      context_value: Annotated[str, "Context data to share (as JSON string)"]) -> str:
            """Share context information with other agents."""
            sharing_func = create_context_sharing_function('assistant')
            result = sharing_func(context_key, context_value)
            # Persist to MemoryGraph (fire-and-forget) — same shape as reuse_recipe:2455-2464
            if memory_graph is not None:
                try:
                    import threading as _t
                    _t.Thread(target=lambda: memory_graph.register(
                        f"[SHARED] {context_key}: {json.dumps(context_value)[:200]}",
                        {'memory_type': 'insight', 'source_agent': 'assistant',
                         'session_id': user_prompt, 'shared_key': context_key},
                    ), daemon=True).start()
                except Exception:
                    tool_logger.warning(
                        "share_context_with_agents: MemoryGraph persist failed",
                        exc_info=True)
            return result

        register_dual(helper, assistant, share_context_with_agents,
                      "share_context_with_agents",
                      "Share context information with other agents in the system")

        # Add context retrieval tool
        @log_tool_execution
        def get_shared_context(context_key: Annotated[str, "Identifier of the context to retrieve"]) -> str:
            """Retrieve context information shared by other agents"""
            retrieval_func = create_context_retrieval_function()
            return retrieval_func(context_key)

        register_dual(helper, assistant, get_shared_context,
                      "get_shared_context",
                      "Retrieve context information shared by other agents")

        tool_logger.info("Internal Agent Communication complete - agents can now delegate tasks and share context")

    except Exception as e:
        tool_logger.warning(f"Internal Agent Communication error (non-critical): {e}")
        # Continue without internal communication if it fails

    # AP2 (Agent Protocol 2): Agentic Commerce - Payment workflows
    try:
        tool_logger.info("Initializing AP2 (Agent Protocol 2) - Agentic Commerce...")

        # Get AP2 payment tools for this agent
        ap2_tools = get_ap2_tools_for_autogen('assistant')

        # Register payment tools — wrap with @log_tool_execution so payment
        # operations fire UI status emits + structured-error envelopes
        # (#510 followup — observability gap for AP2).  Same wrap pattern as
        # the inline-def tools above; payments without observability would
        # leave users unable to see what's happening during a transaction.
        for tool_def in ap2_tools:
            tool_func = log_tool_execution(tool_def['function'])
            tool_name = tool_def['name']
            tool_desc = tool_def['description']
            register_dual(helper, assistant, tool_func, tool_name, tool_desc)
            tool_logger.info(f"Registered AP2 payment tool: {tool_name}")

        tool_logger.info("AP2 Agentic Commerce integration complete - agents can now handle payment workflows")

    except Exception as e:
        tool_logger.warning(f"AP2 Agentic Commerce error (non-critical): {e}")
        # Continue without payment capabilities if AP2 fails

    # Goal-aware Tier 2 tool loading (marketing, coding, etc.)
    try:
        from integrations.agent_engine.marketing_tools import detect_goal_tags, register_marketing_tools
        goal_tags = detect_goal_tags(task)
        if 'marketing' in goal_tags:
            register_marketing_tools(helper, assistant, user_id)
            tool_logger.info("Marketing tools loaded (Tier 2) based on prompt content")
        if 'ip_protection' in goal_tags:
            from integrations.agent_engine.ip_protection_tools import register_ip_protection_tools
            register_ip_protection_tools(helper, assistant, user_id)
            tool_logger.info("IP protection tools loaded (Tier 2) based on prompt content")
        if 'self_build' in goal_tags:
            from integrations.agent_engine.self_build_tools import register_self_build_tools
            register_self_build_tools(helper, assistant, user_id)
            tool_logger.info("Self-build tools loaded (Tier 2) based on prompt content")
        if 'outreach' in goal_tags:
            from integrations.agent_engine.outreach_crm_tools import register_outreach_tools
            register_outreach_tools(helper, assistant, user_id)
            tool_logger.info("Outreach CRM tools loaded (Tier 2) based on prompt content")
        if 'sales' in goal_tags:
            from integrations.agent_engine.journey_engine import register_journey_tools
            register_journey_tools(helper, assistant, user_id)
            tool_logger.info("Sales journey tools loaded (Tier 2) based on prompt content")
        if 'revenue' in goal_tags:
            # Revenue tools: get_api_revenue_stats + adjust_pricing.
            # Without these the bootstrap_revenue_monitor goal can't
            # actually see commercial-API revenue — agent hallucinates
            # tool calls and the flywheel can't close.
            from integrations.agent_engine.revenue_tools import register_revenue_tools
            register_revenue_tools(helper, assistant, user_id)
            tool_logger.info("Revenue tools loaded (Tier 2) based on prompt content")
        if 'news' in goal_tags:
            # News tools: fetch_news_feeds / subscribe_news_feed /
            # mark_news_for_web etc.  Required by the seeded
            # `bootstrap_herald_news_friend` (news) goal — without these the
            # daily-news-refresh agent has a prompt but no way to actually
            # pull feeds or flag items for hevolve.ai, so it talks about
            # curating news without doing it (register_news_tools was dead
            # code — defined, never wired — until this branch).
            from integrations.agent_engine.news_tools import register_news_tools
            register_news_tools(helper, assistant, user_id)
            tool_logger.info("News tools loaded (Tier 2) based on prompt content")
    except Exception as e:
        # Promoted from debug to warning: a failure here means the agent
        # boots without its goal-specific tools, so it can talk about the
        # task but not actually do it (LLM emits prose, no tool calls,
        # goal "completes" with zero side-effects).  Silent for ~6 weeks
        # before being caught.  Loud now so any future regression surfaces.
        tool_logger.warning(f"Goal-aware tool loading FAILED: {e}")

    assistant.description = 'this is an assistant agent that coordinates & executes requested tasks & actions'
    executor.description = 'this is an executor agent that Specialized agent for code execution & response handling'
    author.description = 'this is an author/user agent that focused on user support, error resolution, contextual information. Contact this agent when you need any user based information or persona based information or if you want to say something to user'
    chat_instructor.description = 'this is a ChatInstructor agent that provides step-by-step action plans for task execution'
    helper.description = 'this is a helper agent that calls tools, facilitates task completion & assists other agents'
    verify.description = 'this is a verify status agent. which will verify the status of current action that will be called after ChatInstructor gives instruction to complete an action & assistant completes it, this agent will provide updates in a structured JSON format & then call user agent'

    def state_transition(last_speaker, groupchat):
        """
        Determines the next speaker in the group chat based on various conditions.
        Preserves ChatInstructor's appropriate agent selection logic.
        """
        # Heartbeat: tell watchdog this thread is alive (recipe runs many rounds)
        try:
            from security.node_watchdog import get_watchdog
            _wd = get_watchdog()
            if _wd:
                for _dn in ('agent_daemon', 'coding_daemon'):
                    _wd.heartbeat(_dn)
        except Exception:
            pass

        user_prompt = f'{user_id}_{prompt_id}'
        current_action_id = user_tasks[user_prompt].current_action

        # ─── STUCK-LOOP GUARD (#485) ───────────────────────────────────
        # Detects when the same (last_speaker, last_message_content) pair
        # has repeated >= _STATE_TRANSITION_LOOP_THRESHOLD times and breaks
        # out of the GroupChat with a clean fallback assistant message.
        #
        # Trigger pattern (live evidence 2026-05-10 22:35 request 776d9fb0):
        #   Tamil-language-switch turn → ChatInstructor sends "Execute Action 1
        #   ... Latest User message: I want to talk to you in tamil" (137 chars)
        #   → Assistant LLM regurgitates the prompt verbatim (137 chars) →
        #   state_transition routes Assistant → verify, but autogen's internal
        #   speaker scheduling keeps invoking state_transition with
        #   last_speaker=Assistant for 28+ iterations.  No state advancement,
        #   no progress, user gets nothing for 5+ minutes until autogen's
        #   default max_consecutive_auto_reply (50) kicks in.
        #
        # The guard is signature-based (last_speaker.name + first-500-char
        # content hash).  Any genuine progress — Assistant emits NEW content,
        # OR a different agent speaks — resets the counter to 1.  Tool-call
        # chains where the same agent emits multiple turns with DIFFERENT
        # content also reset, so legitimate sequences are unaffected.
        #
        # On break:
        #   1. Loud diagnostic log + last-10-message trace dump for postmortem.
        #   2. Inject a clean fallback assistant message into groupchat.messages
        #      so the outer initiate_chat wrapper has a coherent reply to send.
        #   3. Mark the action TERMINATED so the recipe pipeline doesn't retry
        #      this stuck turn.
        #   4. Return None to terminate the GroupChat round.
        #   5. Reset _STATE_TRANSITION_LOOP_STATE for this user_prompt so the
        #      next chat turn starts fresh.
        try:
            _last_msg_for_loop = groupchat.messages[-1] if groupchat.messages else {}
            _last_content_for_loop = (_last_msg_for_loop.get('content') or '')
            import hashlib
            _content_hash = hashlib.sha1(
                _last_content_for_loop[:500].encode('utf-8', errors='replace')
            ).hexdigest()[:12]
            _sig = f"{last_speaker.name}:{_content_hash}"
            _ls = _STATE_TRANSITION_LOOP_STATE.get(user_prompt) or {}
            if _ls.get('sig') == _sig:
                _ls['count'] = _ls.get('count', 1) + 1
            else:
                _ls = {
                    'sig': _sig,
                    'count': 1,
                    'first_msg_idx': len(groupchat.messages),
                }
            _STATE_TRANSITION_LOOP_STATE[user_prompt] = _ls

            if _ls['count'] >= _STATE_TRANSITION_LOOP_THRESHOLD:
                current_app.logger.error(
                    f"[STATE-TRANSITION-LOOP-BREAK] STUCK LOOP DETECTED — "
                    f"user_prompt={user_prompt!r} speaker={last_speaker.name!r} "
                    f"content_hash={_content_hash} repeated {_ls['count']} times "
                    f"across {len(groupchat.messages) - _ls.get('first_msg_idx', 0)} "
                    f"groupchat-message increments.  Breaking out with fallback "
                    f"reply so the user gets a response instead of hanging."
                )
                # Trace dump — last 10 messages
                _msgs = groupchat.messages[-10:]
                _start_idx = max(0, len(groupchat.messages) - 10)
                for _i, _m in enumerate(_msgs):
                    _r = (_m.get('role') or '?')
                    _n = (_m.get('name') or '?')
                    _c = str(_m.get('content') or '')
                    current_app.logger.error(
                        f"  [LOOP-TRACE msg #{_start_idx + _i}] role={_r!r} "
                        f"name={_n!r} content_len={len(_c)} "
                        f"preview={_c[:120]!r}"
                    )
                # Inject clean fallback so wrapper has a coherent response
                _fallback_text = (
                    "I had trouble producing a response — the agent pipeline "
                    "got stuck in a loop on this request.  Could you rephrase "
                    "or break it into a simpler step?"
                )
                try:
                    groupchat.messages.append({
                        'role': 'assistant',
                        'name': 'Assistant',
                        'content': _fallback_text,
                    })
                except Exception as _inject_err:
                    current_app.logger.warning(
                        f"[LOOP-BREAK] fallback inject failed: {_inject_err}")
                # Mark action TERMINATED so recipe pipeline doesn't re-enter
                try:
                    force_state_through_valid_path(
                        user_prompt, current_action_id,
                        ActionState.TERMINATED,
                        "Loop-break: state_transition stuck-loop guard fired (#485)",
                    )
                except Exception as _stb_err:
                    current_app.logger.warning(
                        f"[LOOP-BREAK] state-set failed: {_stb_err}")
                # Circuit-breaker (achieve-flywheel): count hard loop-breaks for
                # this goal across re-dispatches; once it exceeds the threshold the
                # goal is unfixable by retry, so PAUSE it — the daemon then stops
                # re-dispatching it (capping the 686-style thrash) and the model is
                # freed for productive goals.  Only autonomous goals (UUID
                # prompt_id, len>=30); human chat (int prompt_id) is never paused.
                try:
                    _gbc = _GOAL_LOOP_BREAK_COUNT.get(user_prompt, 0) + 1
                    _GOAL_LOOP_BREAK_COUNT[user_prompt] = _gbc
                    if _gbc >= _GOAL_PARK_AFTER_BREAKS and len(str(prompt_id)) >= 30:
                        from integrations.agent_engine.goal_manager import (
                            GoalManager)
                        from integrations.social.models import db_session
                        with db_session(commit=True) as _cb_db:
                            GoalManager.update_goal_status(
                                _cb_db, str(prompt_id), 'paused')
                        _GOAL_LOOP_BREAK_COUNT.pop(user_prompt, None)
                        current_app.logger.warning(
                            f"[GOAL-CIRCUIT-BREAKER] goal {prompt_id} hard "
                            f"loop-broke {_gbc}x — paused; daemon stops "
                            f"re-dispatching it so the model is freed for "
                            f"productive flywheel goals.")
                except Exception as _cb_err:
                    current_app.logger.warning(
                        f"[GOAL-CIRCUIT-BREAKER] park failed: {_cb_err}")
                # Reset loop-state for this user — next turn starts fresh
                _STATE_TRANSITION_LOOP_STATE.pop(user_prompt, None)
                _STATE_TRANSITION_NUDGED.pop(user_prompt, None)
                return None  # terminate GroupChat round

            # #2 — EARLY ESCALATION NUDGE (one-shot per stuck action, fires BEFORE
            # the hard break above).  The same turn has repeated but not yet hit
            # the break threshold: append ONE additive directive so the next round
            # is not byte-identical and the FSM can advance.  Keeps the action
            # context + the StatusVerifier JSON spec — only adds the nudge — then
            # falls through to normal routing (we do NOT return).
            elif (_ls['count'] >= _STATE_TRANSITION_LOOP_NUDGE_AT
                  and _STATE_TRANSITION_NUDGED.get(user_prompt) != current_action_id):
                _STATE_TRANSITION_NUDGED[user_prompt] = current_action_id
                try:
                    _nudge = (
                        f"SYSTEM: action {current_action_id} has repeated "
                        f"{_ls['count']}x without advancing — the last reply did "
                        f"not move it forward. Do NOT restate the instruction or "
                        f"echo this prompt. Take ONE concrete step toward "
                        f"completing it now; then @StatusVerifier MUST reply with "
                        f"the action status JSON verbatim "
                        f'({{"status":"completed" | "pending","action_id":'
                        f'{current_action_id},"message":"...","fallback_action":'
                        f'"..."}}) so the pipeline advances to the next action.'
                    )
                    groupchat.messages.append({
                        'role': 'user', 'name': 'ChatInstructor',
                        'content': _nudge,
                    })
                    current_app.logger.info(
                        f"[STATE-TRANSITION-NUDGE] action={current_action_id} "
                        f"count={_ls['count']} — injected one escalation nudge to "
                        f"break the byte-identical re-dispatch.")
                except Exception as _nudge_err:
                    current_app.logger.warning(
                        f"[STATE-TRANSITION-NUDGE] inject failed: {_nudge_err}")
        except Exception as _loop_guard_err:
            current_app.logger.exception(
                f"[STATE-TRANSITION-LOOP] guard raised "
                f"{type(_loop_guard_err).__name__}: {_loop_guard_err!s} — "
                "falling through to normal routing"
            )

        # ─── EARLY-TERMINATE GUARD ─────────────────────────────────────
        # Honour the TERMINATE signal from ANY speaker before the
        # speaker-routing branches below fire.  Live evidence
        # 2026-05-12 c38e8b7c-... — user said "hi" to a bound agent,
        # autogen flowed Assistant → verify → ChatInstructor.  The
        # ChatInstructor UserProxyAgent has
        # ``default_auto_reply='TERMINATE'`` (set at the chat_instructor
        # instantiation) so when verify produces no actionable JSON it
        # emits the literal string ``"TERMINATE"``.  But the
        # "last_speaker == ChatInstructor → return assistant" branch
        # further down fires before the original TERMINATE check at
        # the bottom of state_transition, so the "TERMINATE" message
        # got appended with the metadata blob and routed BACK to
        # Assistant.  Assistant's LLM saw a long context that ended
        # with "TERMINATE\nMetadata/skeleton...", emitted a similar
        # reply each round, and the STUCK-LOOP GUARD only rescued the
        # turn after 5 identical Assistant calls (~3 minutes).
        #
        # Checking the message FIRST — regardless of who spoke — ends
        # the conversation immediately whenever any agent signals
        # TERMINATE, the same way autogen's per-agent
        # ``is_termination_msg`` callback would if GroupChatManager
        # ran it between rounds.
        try:
            _last_content = (groupchat.messages[-1].get('content') or '') if groupchat.messages else ''
            if _last_content and 'TERMINATE' in _last_content.upper():
                current_app.logger.info(
                    "[EARLY-TERMINATE] last message contains TERMINATE "
                    "(speaker=%r) — ending GroupChat round so the "
                    "outer recipe loop can advance.",
                    last_speaker.name,
                )
                return None
        except Exception as _term_err:
            current_app.logger.debug(
                f"[EARLY-TERMINATE] check failed (non-blocking): {_term_err}")

        # Preempt: if user started chatting, abort daemon-initiated recipes
        # so the LLM is free for the user's request immediately.
        # Only preempt if THIS request is from the daemon (not user/CREATE).
        try:
            from integrations.agent_engine.dispatch import is_user_recently_active
            from threadlocal import get_task_source
            _source = get_task_source()
            if _source in ('daemon', 'idle') and is_user_recently_active():
                current_app.logger.info("[PREEMPT] User active — aborting daemon recipe to free LLM")
                raise KeyboardInterrupt("User preemption")
        except ImportError:
            pass

        current_app.logger.info(
            f'Inside state_transition with action id {user_tasks.get(user_prompt, Action([])).current_action}')
        # Log the first message for debugging if it exists
        if len(groupchat.messages) > 0:
            current_app.logger.info(f"STATE_TRANSITION - Message[-1]: {groupchat.messages[-1]}")
            # Log last message details
            last_idx = len(groupchat.messages) - 1
            current_app.logger.info(
                f"STATE_TRANSITION - Last message role: {groupchat.messages[last_idx].get('role')}, name: {groupchat.messages[last_idx].get('name')}")

        messages = groupchat.messages
        if not messages:
            current_app.logger.warning("state_transition called with empty messages list")
            return assistant
        new_role = 'user'
        if messages[-1]['name'] != 'UserProxy':
            new_role = 'AI'
        try:
            helper_fun.history(user_id, prompt_id, new_role, messages[-1]['content'])
            if last_speaker.name == 'UserProxy' and user_tasks[user_prompt].fallback:
                current_action_id = set_fallback_received(user_prompt)
        except Exception as e:
            current_app.logger.error(f"Error in history function: {e}")

        # Log the message content for debugging
        content_preview = messages[-1]["content"][:50] if len(messages[-1]["content"]) > 50 else messages[-1]["content"]
        current_app.logger.info(f'Processing message: "{content_preview}..." from {last_speaker.name}')


        try:
            # Lifecycle TRACKING HOOKS:
            debug_lifecycle_status(user_prompt)
            lifecycle_hook_track_action_assignment(user_prompt, user_tasks, group_chat)  # 1. Track action assignment
            lifecycle_hook_track_status_verification_request(user_prompt, user_tasks, group_chat)  # 3. Track status verification request
            lifecycle_hook_track_fallback_request(user_prompt, user_tasks, group_chat)  # 7. Track fallback request
            lifecycle_hook_track_user_fallback(user_prompt, user_tasks, group_chat)  # 8. Track user fallback
            lifecycle_hook_track_recipe_request(user_prompt, user_tasks, group_chat)  # 9. Track recipe request
            lifecycle_hook_track_termination(user_prompt, user_tasks, group_chat)  # 11. Track termination
            # Hook 13 removed: publish_intermediate_thoughts_to_user
            # (delegated to publish_agent_thought at the top of this
            # file) is the SINGLE canonical Crossbar publisher for
            # agent-to-agent thought streaming. It fires later in
            # state_transition on line 2145. The old lifecycle_hook_
            # publish_narration was a parallel publisher emitting a
            # different JSON shape on the same topic — removed to keep
            # one mechanism, one message format.

            # Enhanced agent selection with state awareness
            if user_prompt and user_tasks[user_prompt]:

                if messages:
                    last_message = messages[-1]
                    current_state = get_action_state(user_prompt, user_tasks[user_prompt].current_action)

                    # State-aware agent routing
                    if current_state == ActionState.FALLBACK_REQUESTED and last_speaker.name != 'UserProxy' and '@Assistant:' not in last_message['content']:
                        current_app.logger.error("Force routing to user for fallback")
                        # Force routing to user for fallback
                        for agent in groupchat.agents:
                            if agent.name in ['UserProxy', 'User']:
                                return agent

                    elif current_state == ActionState.FALLBACK_RECEIVED:
                        current_app.logger.error("After user gives fallback, route to ChatInstructor for recipe request")

                        # After user gives fallback, route to ChatInstructor for recipe request
                        return chat_instructor

                    elif '@StatusVerifier' in last_message['content']:
                        current_app.logger.error("Route to StatusVerifier when requested")

                        # Route to StatusVerifier when requested
                        return verify

            # Check for JSON eroor status pattern
            if "error" in messages[-1]["content"].lower() or "failed" in messages[-1]["content"].lower():
                json_match = re.search(r'{[\s\S]*?}', messages[-1]["content"])
                if json_match:
                    try:
                        json_part = json_match.group(0)
                        json_obj = json.loads(json_part)

                        # If we found a JSON object with error status, route to Helper
                        if isinstance(json_obj, dict) and json_obj.get("status") =="error":
                            current_app.logger.info("Error detected - routing to Helper for resolution")
                            error_context = f"I need you help to resolve this error: {json_part}\nPlease analyze the issue and propose a fix."
                            # Add the error context as a new message to maintain the original error message
                            if last_speaker.name != "Helper":
                                return helper
                    except Exception:
                        pass
        except Exception as e:
            current_app.logger.error(f"Error in error detection logic: {e}")

        # Get metadata once for potential use later
        # get_saved_metadata is a tool closure — access agent_data directly here
        try:
            _ad = agent_data.get(prompt_id, {})
            metadata = json.dumps(list(_ad.keys())) if _ad else "{}"
        except Exception as e:
            current_app.logger.error(f"Error getting metadata: {e}")
            metadata = "{}"

        # current_app.logger.info(messages[-1])
        if messages[-1]['role'] == 'tool':
            current_app.logger.info('Message role is tool returning assistant')
            return assistant

        # Process @ mentions - keeping this logic intact
        pattern = r"@Helper"
        pattern1 = r"@Executor"
        pattern2 = r"@User"
        pattern3 = r"@StatusVerifier"
        try:
            if re.search(pattern2, messages[-1]["content"], re.IGNORECASE):
                current_app.logger.info("String contains @User returning author")
                return author
            if re.search(pattern3, messages[-1]["content"], re.IGNORECASE):
                current_app.logger.info("String contains @StatusVerifier returning StatusVerifier")
                force_state_through_valid_path(user_prompt, current_action_id,
                                               ActionState.STATUS_VERIFICATION_REQUESTED, "verifier call")

                return verify
            if re.search(pattern, messages[-1]["content"], re.IGNORECASE) and last_speaker.name != 'Helper':
                current_app.logger.info("String contains @Helper returning helper")
                messages[-1]["content"] = messages[-1]["content"].replace('@user','')
                group_chat.messages[-1]['content'] = f"{group_chat.messages[-1]['content']}\n Metadata/skeleton of all keys for retrieving data from memory:{metadata}"
                return helper
            if re.search(pattern1, messages[-1]["content"]):
                current_app.logger.info("String contains @Executor returnng executor")
                return executor
        except Exception as e:
            current_app.logger.error(f'Got error when searching for @user in last message :{e}')

        # Don't handle if last/current message in conversation is focus on current task at hand and not recipe creation conversation
        if not messages[-1]["content"].startswith('Reflect on the sequence') and not messages[-1]["content"].startswith('Focus on the current task at hand'):
            json_obj = retrieve_json(messages[-1]["content"])
            if json_obj:
                try:
                    current_state = get_action_state(user_prompt, user_tasks[user_prompt].current_action)

                    if 'status' in json_obj:
                        current_app.logger.info(f'got status as:{json_obj["status"]} ')
                        if json_obj['status'].lower() == 'error' and 'message' in json_obj:
                            safe_set_state(user_prompt, current_action_id, ActionState.ERROR, "verifier error")
                            return author
                        elif json_obj['status'].lower() == 'completed' or json_obj['status'].lower() == 'success':
                            json_action_id = int(float(json_obj.get('action_id', user_tasks[user_prompt].current_action)))


                            # Normal Set ActionState To Complete
                            if json_obj['status'].lower() == 'completed' and 'action_id' in json_obj.keys():
                                if user_tasks[user_prompt].fallback == False and user_tasks[user_prompt].recipe == False:
                                    current_app.logger.info('UPDATED TIMER for this action')
                                    end = time.time()
                                    task_time[prompt_id]['times'].append(end-task_time[prompt_id]['timer'])
                                user_tasks[user_prompt].actions[json_action_id-1] = json_obj.get('action', user_tasks[user_prompt].actions[json_action_id-1])
                                user_tasks[user_prompt].new_json.append(json_obj)
                                current_app.logger.info(f'CHECKING FOR FALLBACK user_tasks[user_prompt].current_action={user_tasks[user_prompt].current_action} json_obj["action_id"]={json_obj["action_id"]}')

                                # After completion, only request fallback from user if LLM didn't provide one
                                # This enables autonomous operation - LLM generates fallback strategies automatically
                                fallback_action = json_obj.get('fallback_action', '').strip()
                                if not fallback_action or len(fallback_action) == 0:
                                    current_app.logger.warning(f'Action {json_action_id} completed but no fallback_action provided by StatusVerifier - this should not happen with updated instructions')
                                    # Request fallback from user only if LLM failed to generate one
                                    user_tasks[user_prompt].fallback = True
                                else:
                                    current_app.logger.info(f'Action {json_action_id} completed with auto-generated fallback: {fallback_action[:100]}...')
                                    # Fallback was provided by LLM, proceed to recipe phase
                                    user_tasks[user_prompt].fallback = False
                                    user_tasks[user_prompt].recipe = True

                                force_state_through_valid_path(user_prompt, json_action_id, ActionState.COMPLETED,"verified complete")


                            return chat_instructor
                        elif json_obj['status'].lower() == 'updated':
                            if 'entire_actions' in json_obj.keys() and type(json_obj['entire_actions'])==list:
                                update_entire_actions(json_obj, user_prompt)

                            elif 'action_id' in json_obj.keys():
                                user_tasks[user_prompt].actions[int(json_obj['action_id'])-1] = json_obj['updated_action']
                                user_tasks[user_prompt].new_json.append(json_obj)
                                safe_set_state(user_prompt, int(json_obj['action_id']), ActionState.COMPLETED)
                                user_tasks[user_prompt].fallback = True

                        elif json_obj['status'].lower() == 'pending':
                            safe_set_state(user_prompt, current_action_id, ActionState.PENDING, "verifier pending")
                            # USER-INPUT GATE (code-level enforcement of the
                            # prompt-level rule above):  if the verifier
                            # explicitly returned `can_perform_without_user_input:
                            # no` for this action, set a sticky flag on
                            # user_tasks so the OUTER while loop in
                            # create_recipe_pipeline can see "this action is
                            # blocked on the user" and break out, returning
                            # control to the user.  Without this flag, the
                            # OUTER loop's pending-retry path (3 attempts)
                            # gives the StatusVerifier multiple chances to
                            # flip the gate to "yes" via prompt drift —
                            # exactly the autonomous-bypass bug seen in the
                            # 2026-05-08 langchain.log (Action 3 / Confirm
                            # sitemap looped 8 iterations before
                            # hallucinating user confirmation).
                            try:
                                _gate_value = (json_obj.get('can_perform_without_user_input') or '').strip().lower()
                                if _gate_value.startswith('no'):
                                    user_tasks[user_prompt]._needs_user_input_action_id = current_action_id
                                    current_app.logger.info(
                                        f"[USER-INPUT-GATE] Action {current_action_id} flagged "
                                        f"as blocked on user input "
                                        f"(can_perform_without_user_input={_gate_value!r}); "
                                        f"OUTER loop will break and return control to user."
                                    )
                            except Exception as _gate_err:
                                current_app.logger.debug(
                                    f"[USER-INPUT-GATE] flag set failed (non-blocking): {_gate_err}"
                                )
                            return assistant
                        elif json_obj['status'].lower() == 'requires_breakdown':
                            # Handle subtask breakdown request from StatusVerifier
                            current_app.logger.info(f"Action {current_action_id} requires breakdown into subtasks")
                            if 'subtasks' in json_obj and len(json_obj['subtasks']) > 0:
                                # Add subtasks to ledger
                                success = add_subtasks_to_ledger(
                                    user_prompt,
                                    current_action_id,
                                    json_obj['subtasks'],
                                    user_ledgers
                                )
                                if success:
                                    current_app.logger.info(f"Added {len(json_obj['subtasks'])} subtasks to ledger for action {current_action_id}")
                                    # Auto-sync handles ledger update via safe_set_state below
                                else:
                                    current_app.logger.warning(f"Failed to add subtasks to ledger")
                            safe_set_state(user_prompt, current_action_id, ActionState.PENDING, "requires breakdown into subtasks")
                            return assistant
                        elif json_obj['status'].lower() == 'done':
                            json_action_id = int(float(json_obj.get('action_id', user_tasks[user_prompt].current_action)))

                            # Normal Set ActionState To Terminate After getting Recipe json for each action
                            if 'recipe' in json_obj.keys() and json_obj['status'].lower() == 'done' and json_action_id > len(user_tasks[user_prompt].actions): # Done state when recipe is created
                                create_individual_flow_recipe_and_terminate_flow(json_action_id, json_obj, user_prompt)

                            recipe_result = lifecycle_hook_track_recipe_completion(user_prompt, json_obj,
                                                                                   user_tasks)  # 10. Track recipe completion

                            current_state = get_action_state(user_prompt, user_tasks[user_prompt].current_action)

                            if current_state == ActionState.RECIPE_RECEIVED and _recipe_is_placebo(json_obj):
                                # The 4B echoed the recipe-template placeholders
                                # ("Describe the action performed here" / "steps
                                # here") instead of real content. Banking it
                                # poisons every future REUSE replay (stalls
                                # forever). Reject + re-request rather than save.
                                current_app.logger.warning(
                                    f'[PLACEBO-RECIPE] action {json_obj.get("action_id")} '
                                    f'echoed template placeholders — rejecting, re-requesting')
                                user_tasks[user_prompt].recipe = True
                                return chat_instructor
                            if current_state == ActionState.RECIPE_RECEIVED:  # State was set in Location 1
                                # Recipe received, save it
                                current_app.logger.info('Got Individual action recipe save it')
                                flow = get_current_flow(user_prompt)
                                name = helper_fun.safe_prompt_path(prompt_id, flow, json_obj["action_id"])
                                user_tasks[user_prompt].fallback = False
                                user_tasks[user_prompt].recipe = False
                                metadata = strip_json_values(agent_data[prompt_id])
                                json_obj['metadata'] = metadata
                                # 'times' is [] at init (:4211) and appended only
                                # inside a branch (:2438), so it can legitimately
                                # be empty here.  Unguarded, this raised
                                # IndexError 22 times in the installed build's
                                # logs -- always one line before the recipe was
                                # written, so the save was lost and the turn only
                                # logged "GOT SOME ERROR WHILE JSON".  Timing is
                                # metadata; never let it cost us the recipe.
                                # Same guard as :2599 below.
                                if prompt_id in task_time and task_time[prompt_id].get('times'):
                                    json_obj['time_took_to_complete'] = task_time[prompt_id]['times'][-1]
                                for i in json_obj['recipe']:
                                    if 'tool_name' in i and i['tool_name'] != "":
                                        i['agent_to_perform_this_action'] = 'Helper'
                                    elif 'generalized_functions' in i and i['generalized_functions'] != "":
                                        i['agent_to_perform_this_action'] = 'Executor'
                                    else:
                                        i['agent_to_perform_this_action'] = 'Assistant'
                                # SECURITY: redact secrets before recipe persistence
                                try:
                                    from security.secret_redactor import redact_secrets
                                    for _ri in json_obj.get('recipe', []):
                                        for _rk in ('tool_input', 'task_description', 'output'):
                                            if isinstance(_ri.get(_rk), str):
                                                _ri[_rk], _ = redact_secrets(_ri[_rk])
                                except ImportError:
                                    pass
                                atomic_json_write(name, json_obj)
                                #setting the action from response as current action
                                user_tasks[user_prompt].current_action = int(json_obj['action_id'])
                                individual_json[user_prompt] = json_obj
                                current_app.logger.info(f'Saved Individual recipe at: {name}')
                                # Transition to TERMINATED so next action can start
                                force_state_through_valid_path(user_prompt, int(json_obj['action_id']), ActionState.TERMINATED, "Recipe saved and action complete")
                            else:
                                current_app.logger.info(f'Current state is {current_state}, checking if recipe still needs saving')
                                # Save recipe even if action is already TERMINATED/COMPLETED
                                # (state_transition handled completion, while loop requested recipe,
                                #  but this handler saw action already terminated — recipe still valid)
                                _recipe_list = json_obj.get('recipe', None)
                                _has_recipe = (isinstance(_recipe_list, list)
                                               and len(_recipe_list) > 0
                                               and not _recipe_is_placebo(json_obj))
                                if not _has_recipe:
                                    if current_state in (ActionState.TERMINATED, ActionState.GAVE_UP):
                                        # Action already terminal — don't retry recipe, let while loop advance
                                        current_app.logger.warning(f'Late save: recipe empty but action already terminal ({current_state.value}) — not retrying')
                                        return None  # End conversation turn, while loop will detect and advance
                                    current_app.logger.warning(f'Late save: recipe empty or missing — rejecting, will retry. Got: {_recipe_list}')
                                    return chat_instructor  # Route back to request recipe again
                                current_app.logger.info(f'Late save: valid recipe with {len(_recipe_list)} steps')
                                if _has_recipe:
                                    flow = get_current_flow(user_prompt)
                                    name = helper_fun.safe_prompt_path(prompt_id, flow, json_obj["action_id"])
                                    if not os.path.exists(name):
                                        metadata = strip_json_values(agent_data.get(prompt_id, {}))
                                        json_obj['metadata'] = metadata
                                        if prompt_id in task_time and task_time[prompt_id].get('times'):
                                            json_obj['time_took_to_complete'] = task_time[prompt_id]['times'][-1]
                                        for i in json_obj['recipe']:
                                            if 'tool_name' in i and i['tool_name'] != "":
                                                i['agent_to_perform_this_action'] = 'Helper'
                                            elif 'generalized_functions' in i and i['generalized_functions'] != "":
                                                i['agent_to_perform_this_action'] = 'Executor'
                                            else:
                                                i['agent_to_perform_this_action'] = 'Assistant'
                                        try:
                                            from security.secret_redactor import redact_secrets
                                            for _ri in json_obj.get('recipe', []):
                                                for _rk in ('tool_input', 'task_description', 'output'):
                                                    if isinstance(_ri.get(_rk), str):
                                                        _ri[_rk], _ = redact_secrets(_ri[_rk])
                                        except ImportError:
                                            pass
                                        atomic_json_write(name, json_obj)
                                        current_app.logger.info(f'Saved Individual recipe (late) at: {name}')
                                        user_tasks[user_prompt].current_action = int(json_obj['action_id'])
                                        individual_json[user_prompt] = json_obj
                                # Ensure action is TERMINATED via proper state path
                                if current_state == ActionState.COMPLETED:
                                    safe_set_state(user_prompt, user_tasks[user_prompt].current_action, ActionState.RECIPE_RECEIVED, "Recipe saved, set to RECIPE_RECEIVED")
                                    force_state_through_valid_path(user_prompt, user_tasks[user_prompt].current_action, ActionState.TERMINATED, "Now terminate action")


                            return chat_instructor
                except Exception as e:
                    current_app.logger.error(f'GOT SOME ERROR WHILE JSON: {e}')
                    current_app.logger.error(traceback.format_exc())

        # Send crossbar message for UI feedback
        publish_intermediate_thoughts_to_user(last_speaker, messages)

        if has_pending_tool_calls(messages):
            current_app.logger.info("DETECTED PENDING TOOL CALLS - routing to Assistant without message modification")
            return assistant

        # ─── DETERMINISTIC RECIPE-REQUEST ROUTING ──────────────────────
        # The recipe-creation prompt (request_recipe_for_action / _last, both
        # built from RECIPE_CREATE_PROMPT_PREFIX) is emitted by ChatInstructor
        # to ask for the {"status":"done", ...recipe} JSON that advances
        # RECIPE_REQUESTED → RECIPE_RECEIVED.  The agent that reliably produces
        # that JSON is the StatusVerifier (verify) — NOT the Assistant.  Without
        # this pin the generic "ChatInstructor → return assistant" branch
        # directly below hands the recipe request to the Assistant, which echoes
        # the prompt or replies "I'm not sure I understand"; the action then
        # only advances on a lucky round where the LLM speaker-selector happens
        # to pick StatusVerifier (live 2026-06-07 19:13-19:18: one action,
        # ~5 min, dozens of "could NOT be parsed" retries before a chance hit).
        # Pin it deterministically — the stuck-loop guard above still backstops
        # a StatusVerifier that itself fails to emit valid JSON, and this fires
        # ONLY for the recipe prompt so the normal Assistant→verify status flow
        # is untouched.
        if is_recipe_creation_request(messages[-1].get("content")):
            current_app.logger.info(
                "[RECIPE-ROUTE] recipe-creation request → StatusVerifier "
                "(deterministic pin, not LLM-selected)")
            return verify

        if last_speaker.name == 'Executor' or last_speaker.name == 'Helper' or last_speaker.name == 'UserProxy' or last_speaker.name == 'UserProxy' or last_speaker.name == 'ChatInstructor':
            if group_chat.messages:
                group_chat.messages[-1]['content'] = f"{group_chat.messages[-1]['content']}\n Metadata/skeleton of all keys for retrieving data from memory:{metadata}"
            current_app.logger.info('Got last speaker as executor or helper or author or chat_instructor & reutrning next speaker as assistant')
            return assistant

        # After Assistant speaks: route to StatusVerifier (or Helper if streak ≥3 — #485 L3).
        if last_speaker.name == 'Assistant':
            _streak = _ASSISTANT_STREAK_STATE.get(user_prompt, 0) + 1
            _ASSISTANT_STREAK_STATE[user_prompt] = _streak
            if _streak >= _ASSISTANT_STREAK_THRESHOLD:
                current_app.logger.warning(
                    "[ASSISTANT-STREAK-ESCALATE] user_prompt=%r streak=%d → Helper (#485)",
                    user_prompt, _streak)
                _ASSISTANT_STREAK_STATE[user_prompt] = 0
                return helper
            return verify
        # Reset the streak only on speakers OUTSIDE the Assistant round-trip.
        #
        # THE LIVELOCK (observed live 2026-08-04, 20,920 [ROLE-ORDER-GUARD]
        # lines in one session, still cycling 4 minutes after a one-word user
        # message and starving llama-server so an unrelated "hi" took 56s):
        #
        #     ChatInstructor --(:2676)--> Assistant --(:2683)--> verify
        #          ^                                              |
        #          +-------------------(:2699)--------------------+
        #
        # `verify` and `ChatInstructor` are non-Assistant speakers, so the
        # unconditional pop here fired on EVERY lap.  The streak went 1 -> reset
        # -> 1 -> reset and never reached _ASSISTANT_STREAK_THRESHOLD, making
        # the Helper escalation above unreachable for the exact cycle it was
        # added to break.  The guard was being reset by the loop it guards —
        # #485's "catches STUCK actions, not CYCLING ones", with the mechanism
        # now pinned to a line.
        #
        # The original intent — "inverse-of-Assistant is future-proof for new
        # agents" — is preserved for genuinely NEW agents: anything not named
        # below still resets.  Only the two cycle partners are excluded, because
        # seeing them means the round-trip is still in progress, not that
        # progress was made.  Executor/Helper DO reset: reaching them means a
        # tool ran or the escalation fired, which is real forward motion.
        elif last_speaker.name not in _ASSISTANT_ROUNDTRIP_SPEAKERS:
            _ASSISTANT_STREAK_STATE.pop(user_prompt, None)

        json_obj = None

        if last_speaker == verify:
            current_app.logger.info('Got last speaker as verify_status & returning next speaker as chat_instructor')
            return chat_instructor
        try:
            if messages[-1]["content"] == '':
                groupchat.messages[-1]["content"] = 'tool call'
            if 'exitcode:' in messages[-1]["content"]:
                current_app.logger.info('Got exitcode in text returning assistant')
                group_chat.messages[-1]['content'] = f"{group_chat.messages[-1]['content']}\n Metadata/skeleton of all keys for retrieving data from memory:{metadata}"
                return assistant
        except Exception as e:
            current_app.logger.error(f'Got error when content as blank with error as :{e}')



        if 'TERMINATE' in messages[-1]["content"].upper():
            current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
            # retrieve: action 1 -> action 2
            return None
        else:
            return 'auto'

    def set_fallback_received(user_prompt):
        current_action_id = user_tasks[user_prompt].current_action
        safe_set_state(user_prompt, current_action_id, ActionState.FALLBACK_RECEIVED, "user fallback received")
        return current_action_id

    def publish_intermediate_thoughts_to_user(last_speaker, messages):
        # Delegates to the module-level publish_agent_thought so the
        # Timer flow's state_transition1 (which lives in create_time_agents
        # and can't see this closure) can publish via the same single
        # mechanism. Keeps the closure's original signature so every
        # existing call site inside create_agents continues to work.
        publish_agent_thought(last_speaker, messages, user_id)

    def update_entire_actions(json_obj, user_prompt):
        current_app.logger.info('GOT UPDATED WITH entire actions')
        try:

            current_app.logger.info(
                f"user_tasks[user_prompt].actions:{len(user_tasks[user_prompt].actions)}, len(json_obj['entire_actions']:{len(json_obj['entire_actions'])}")
            current_app.logger.info(
                f"user_tasks[user_prompt].actions:{user_tasks[user_prompt].actions}, len(json_obj['entire_actions']:{json_obj['entire_actions']}")

            current_app.logger.info('')
            entire_actions = json_obj['entire_actions']
            user_tasks[user_prompt].actions = entire_actions
            user_tasks[user_prompt].current_action = 1
            user_tasks[user_prompt].fallback = False
            user_tasks[user_prompt].recipe = False
            config, total_actions = get_total_actions_for_current_flow_and_reset_actions(prompt_id, user_prompt)
            reset_to_assigned_for_all_actions(total_actions, user_prompt)

        except Exception as e:
            current_app.logger.info(f'error is here:{e}')

            user_tasks[user_prompt].actions[int(json_obj['action_id']) - 1] = json_obj['updated_action']
            user_tasks[user_prompt].new_json.append(json_obj)
            safe_set_state(user_prompt, int(json_obj['action_id']), ActionState.ERROR, "Exception ")

            user_tasks[user_prompt].fallback = True

    def reset_to_assigned_for_all_actions(total_actions, user_prompt):
        for action_id in range(1, total_actions + 1):
            safe_set_state(user_prompt, action_id, ActionState.ASSIGNED,
                           "entire_actions got updated and hence starting again")

    def create_individual_flow_recipe_and_terminate_flow(current_action_id, json_obj, user_prompt):
        current_app.logger.info('Recipe created successfully, Saving Pending')
        _push_thinking(user_id, f'Recipe saved for action {user_tasks[user_prompt].current_action}. Learning complete.')

        safe_set_state(user_prompt, user_tasks[user_prompt].current_action, ActionState.RECIPE_RECEIVED, "Recipe Received")

        # Initialize final_recipe[prompt_id] if it doesn't exist
        if prompt_id not in final_recipe:
            final_recipe[prompt_id] = {}
            current_app.logger.info(f'Initialized final_recipe for prompt_id: {prompt_id}')

        merged_dict = {**final_recipe[prompt_id], **json_obj}
        flow = get_current_flow(user_prompt)
        create_final_recipe_for_current_flow(flow, merged_dict, prompt_id)
        current_app.logger.info('Flow Recipe Created & saved successfully')

        # Merge accumulated experience data into the saved recipe
        try:
            from recipe_experience import RecipeExperienceRecorder
            RecipeExperienceRecorder.merge_experience_into_recipe(prompt_id, flow, user_prompt)
        except Exception:
            pass

        # Capture agent baseline snapshot on creation
        try:
            from integrations.agent_engine.agent_baseline_service import capture_baseline_async
            capture_baseline_async(
                prompt_id=str(prompt_id), flow_id=flow,
                trigger='creation', user_id=str(user_id),
                user_prompt=user_prompt)
        except Exception:
            pass

        force_state_through_valid_path(user_prompt, current_action_id, ActionState.TERMINATED,
                                       "Recipe Created And Terminated")
        final_recipe[prompt_id] = merged_dict

        safe_increment_flow(user_prompt, prompt_id)

    all_agents = [assistant, executor, author, chat_instructor,helper,verify]
    all_agents.extend(custom_agents)
    select_speaker_transforms = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=AUTOGEN_HISTORY_LIMIT, keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET, max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE, min_tokens=0),
            ToolMessageHandler(user_tasks=user_tasks, user_prompt=user_prompt),
        ]
    )

    # Try to use select_speaker_transform_messages if supported (added in AutoGen 0.2.36+)
    # Seed autogen with recent messages from shared LangChain/autogen buffer
    _seed_msgs = _seed_messages(user_id)

    group_chat_kwargs = {
        'agents': all_agents,
        'messages': _seed_msgs,
        'max_round': 30,
        'select_speaker_prompt_template': f"Read the above conversation, select the next person from [Assistant, Helper, Executor, ChatInstructor, StatusVerifier & User] & only return the role as agent. Return User only if the previous message demands it",
        'speaker_selection_method': state_transition,  # using an LLM to decide
        'allow_repeat_speaker': False,  # Prevent same agent speaking twice
        'send_introductions': False,
        'role_for_select_speaker_messages': 'user',  # Qwen3.5 Jinja template rejects system messages mid-conversation
    }

    # Check if GroupChat supports select_speaker_transform_messages parameter
    try:
        import inspect
        sig = inspect.signature(autogen.GroupChat.__init__)
        if 'select_speaker_transform_messages' in sig.parameters:
            group_chat_kwargs['select_speaker_transform_messages'] = select_speaker_transforms
            current_app.logger.info("Using select_speaker_transform_messages (AutoGen 0.2.36+)")
        else:
            current_app.logger.warning("select_speaker_transform_messages not supported in this AutoGen version, skipping")
    except Exception as e:
        current_app.logger.warning(f"Could not check AutoGen version compatibility: {e}")

    # Guard: filter out any non-Agent items that might have crept in via
    # custom_agents or Agent Lightning wrapping
    valid_agents = [a for a in all_agents if isinstance(a, autogen.Agent)]
    if len(valid_agents) != len(all_agents):
        current_app.logger.warning(
            f"Filtered {len(all_agents) - len(valid_agents)} non-Agent items: "
            f"{[(type(a).__name__, getattr(a, 'name', '?')) for a in all_agents if not isinstance(a, autogen.Agent)]}")
        group_chat_kwargs['agents'] = valid_agents
    current_app.logger.info(
        f"GroupChat creating with {len(group_chat_kwargs['agents'])} agents: "
        f"{[a.name for a in group_chat_kwargs['agents']]}")

    try:
        group_chat = autogen.GroupChat(**group_chat_kwargs)
    except ValueError as e:
        if 'allowed_speaker_transitions_dict' in str(e):
            # Retry without select_speaker_transform_messages — known compatibility issue
            # with certain autogen versions when agents are wrapped or modified
            current_app.logger.warning(
                f"GroupChat speaker transitions failed, retrying without "
                f"select_speaker_transform_messages: {e}")
            group_chat_kwargs.pop('select_speaker_transform_messages', None)
            group_chat = autogen.GroupChat(**group_chat_kwargs)
        else:
            raise

    manager = autogen.GroupChatManager(
        groupchat=group_chat,
        llm_config={"config_list": config_list,"cache_seed": None,"max_tokens": 1500}
    )
    # FIX: Ensure group_chat references the SAME object autogen uses internally.
    # GroupChatManager may store a different reference. The state_transition closure
    # captures this variable, so it must point to the real GroupChat.
    group_chat = manager._groupchat

    # Agent Ops Console Phase B: register the canonical GroupChat reference
    # for live drill-down access from /admin/agents drawer.  Uses the same
    # user_prompt key the rest of the lifecycle uses (user_agents dict,
    # _ledger_registry, etc.).  Idempotent + no-op safe.
    try:
        from lifecycle_hooks import register_groupchat_for_session as _reg_gc
        _reg_gc(user_prompt, group_chat)
    except Exception:
        current_app.logger.debug("groupchat registry hook skipped", exc_info=True)

    # Auto-ingest group_chat messages into SimpleMem + shared LangChain buffer
    _original_append = group_chat.messages.append
    def _unified_ingest_hook(msg):
        # Strip non-ASCII (emoji etc) from content — prevents cp1252 crashes on Windows
        # and JSON parse errors in llama.cpp tool call parsing
        if isinstance(msg, dict) and isinstance(msg.get('content'), str):
            msg['content'] = msg['content'].encode('ascii', 'replace').decode('ascii')
        _original_append(msg)
        if isinstance(msg, dict) and msg.get('_from_shared'):
            return  # seeded message, already in buffer
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        if not content or len(content.strip()) <= 5 or _is_terminate(content):
            return
        speaker = msg.get("name", "Agent") if isinstance(msg, dict) else "Agent"
        # SimpleMem ingest
        if simplemem_store is not None:
            try:
                loop = get_or_create_event_loop()
                loop.run_until_complete(simplemem_store.add(content, {
                    "sender_name": speaker,
                    "user_id": user_id,
                    "prompt_id": prompt_id,
                }))
            except Exception:
                pass
        # Shared PersistentChatHistory write-back (dedup-aware)
        try:
            from integrations.channels.memory.shared_history import _get_persistent_history
            hist = _get_persistent_history(user_id)
            if hist:
                from langchain_core.messages import HumanMessage, AIMessage
                role = msg.get("role", "assistant") if isinstance(msg, dict) else "assistant"
                lc_msg = HumanMessage(content=content) if role == "user" else AIMessage(content=content)
                last_msgs = hist.messages[-3:] if hist.messages else []
                if not any(m.content == content for m in last_msgs):
                    from datetime import datetime
                    hist.add_message(lc_msg, metadata={
                        'timestamp': datetime.now().isoformat(),
                        'source': 'autogen',
                    })
        except Exception:
            pass
    # Hook into message flow using a wrapper list instead of overriding append
    # (plain list.append is read-only in Python — can't be replaced on instances)
    class _HookedList(list):
        def append(self, msg):
            super().append(msg)
            try:
                _unified_ingest_hook(msg)
            except Exception:
                pass

    _hooked = _HookedList(group_chat.messages)
    group_chat.messages = _hooked

    # Auto-ingest group_chat messages into MemoryGraph (provenance tracking)
    if memory_graph is not None:
        _prev_append = group_chat.messages.append
        def _graph_ingest_hook(msg):
            _prev_append(msg)
            try:
                content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                speaker = msg.get("name", "Agent") if isinstance(msg, dict) else "Agent"
                if content and len(content.strip()) > 5:
                    memory_graph.register_conversation(speaker, content, user_prompt)
            except Exception:
                pass  # Non-blocking
        group_chat.messages.append = _graph_ingest_hook

    # Resonance stream: continuous in-conversation tuning via HevolveAI
    try:
        from core.resonance_tuner import get_resonance_tuner
        _res_tuner = get_resonance_tuner()
        _res_prev_append = group_chat.messages.append
        def _resonance_stream_hook(msg):
            _res_prev_append(msg)
            try:
                content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                speaker = msg.get("name", "Agent") if isinstance(msg, dict) else "Agent"
                is_user = speaker.lower() in ('user', 'user_proxy', 'author')
                _res_tuner.stream.on_message(
                    str(user_id), speaker, content, is_user_message=is_user)
            except Exception:
                pass
        group_chat.messages.append = _resonance_stream_hook
    except ImportError:
        pass

    return author, assistant, executor, group_chat, manager, chat_instructor, agents_object


def instantiate_executor_agent():
    # Inject cultural wisdom — even code execution should embody care
    _executor_cultural = ""
    try:
        from cultural_wisdom import get_cultural_prompt_compact
        _executor_cultural = get_cultural_prompt_compact()
    except Exception:
        pass

    executor = autogen.AssistantAgent(
        name="Executor",
        code_execution_config={"last_n_messages": 2, "work_dir": get_coding_workspace_dir(), "use_docker": False},
        llm_config=llm_config,
        system_message=f"""You are an Executor agent.
{_executor_cultural}
        Focus: Running, and debugging code.

        CRITICAL: This runs on Windows (cp1252 encoding). NEVER use emoji or non-ASCII characters in code output (print statements, strings, comments). Use plain text only. Replace emoji with descriptive text like [SUN], [OK], [ERROR].

        Responsibilities:
            1. Code Execution:
                Execute code provided by the Assistant Agent.
                Report execution results, errors, or output.
            2. Error Management:
                Identify issues if errors occur.
                Propose and implement fixes.
                Report back to the Assistant with clear details.
            3. Key Notes:
                You can create code if not provided to you.
                Working Directory: {os.getcwd()}. Use this as the base path for all file operations. Always use absolute paths by joining with this directory.
                For storing or retrieving information about the user, request the Helper Agent to use the get_set_internal_memory tool.
                No General Conversations: Redirect unrelated conversations to the manager to route to the user.

        Coding Instructions:
            CRITICAL: When creating file paths, ALWAYS use os.path.join(os.getcwd(), filename) or similar. NEVER use hardcoded absolute paths like '/home/user/path' or 'C:\\path'. All paths must be relative to the current working directory.
            Avoid using time.sleep. Instead, request the Helper Agent to use the create_scheduled_jobs tool for tasks requiring delays or intervals.
            If the Assistant Agent provides code requiring time.sleep, inform them that it cannot be executed and suggest using the create_scheduled_jobs tool.
            Add proper error handling and logging in all code.
            Ensure the final response is printed using print() before returning it.
            Do not hardcode or default case or a placeholder for exception or empty response cases when the functionality was not satisfied instead throw an error.

        Calling Other Agents:
            When you need to direct a question or route the conversation to a specific agent, use the @ tag followed by the agent's name. Examples include: @Executor or @Helper or @User
        Things You cannot do but Helper Agent can:
            1. Tools Helper Agent can use: Can use tools like send_message_in_seconds, send_message_to_user,send_presynthesized_video_to_user, execute_windows_or_android_command, text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, save_data_in_memory, search_long_term_memory and save_to_long_term_memory.
            2. Create Scheduled Jobs: For tasks involving timers or scheduled jobs, ask Helper agent to use the create_scheduled_jobs tool.
            3. Data/Memory Management:
                - If you want to save some data ask helper agent to use "save_data_in_memory" tool.
                - If you want to get some data ask helper agent to use "get_data_by_key", "get_saved_metadata" tool.
                - For searching past conversations and recalling facts, use "search_long_term_memory" tool.
                - For saving important facts for future sessions, use "save_to_long_term_memory" tool.
            4. If you want to send some message to user directly then ask helper agent to use send_message_to_user tool but if you want to send message after sometime then ask helper to use send_message_in_seconds tool.
            5. If you want to send some pre synthesized video to user then ask helper agent to use send_presynthesized_video_to_user tool.
            6. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            7. If you receive a request to perform a task on the user's computer or any other computer, or if the request is related to Chrome or any browser, you should ask @Helper to use the `execute_windows_or_android_command` tool."""
    )
    return executor


def instantiate_status_verifier_agent(user_prompt):
    verify = autogen.AssistantAgent(
        name="StatusVerifier",
        llm_config=llm_config,
        code_execution_config=False,
        system_message=""""You are a Status Verification Agent in a multi-agent system.
        AUTONOMOUS MODE: Prefer "completed" over "updated" or "pending". If the Assistant made a reasonable attempt (even simulated), mark "completed". Only use "updated" when the action definition itself needs changing. Do NOT return "updated" or "pending" just because user preferences are unknown — use sensible defaults.
        USER-INPUT GATE (HARD RULE): If a previous turn for THIS action returned `can_perform_without_user_input: "no"` (explicitly marked as requiring user input — e.g. "Confirm sitemap with user", "Choose payment method", "Approve plan"), you MUST NOT flip it to `"yes"` and you MUST NOT mark `"status": "completed"` until the user has actually replied. The autonomous-mode preference for "completed" does NOT override an explicit user-input requirement. For these actions, return `"status": "pending"` and keep `can_perform_without_user_input: "no"` until a fresh user message arrives in the conversation. Hallucinating a user confirmation ("user confirmed the structure", "sitemap approved") when the user hasn't actually replied is a contract violation — the user's reply must be visibly present in the message history.
        Role: Track, validate and verify the status of actions performed by other agents. Respond strictly in JSON:
        Response formats:
            1. Action Completed: {"status": "completed","action": "current action","action_id": 1/2/3...,"message": "message here","can_perform_without_user_input":"yes by default. Only no when absolutely impossible (e.g. payment auth, physical access) OR when the action verbatim asks the user to choose/confirm/approve","persona_name":"persona name","fallback_action": "Context-aware retry strategy. NEVER leave empty."}
            2. Action Error: {"status": "error","action": "current action","action_id": 1/2/3...,"message": "error details"}
            3. Action Updated: {"status": "updated","action": "current action text","updated_action": "updated text","action_id": 1/2/3...,"message": "why updated","persona_name":"persona name","fallback_action": "fallback strategy"}
            4. Action Pending: {"status": "pending","action": "current action","action_id": 1/2/3...,"message": "what steps are pending","can_perform_without_user_input":"yes/no — must match the prior turn's value if action verbatim asks for user input"}
            5. Requires Breakdown: {"status": "requires_breakdown","action": "current action","action_id": 1/2/3...,"reason": "why","subtasks": [{"subtask_id": "1.1","description": "subtask desc","depends_on": [],"can_perform_autonomously": true}]}
        Error Detection Rules:
            - HTTP 403/404/500/401, connection timeouts, permission denied = report "error" (not "pending")
            - Only "pending" for: first attempt, waiting for user, transient rate limits
            - Same failure 2+ times = always "error"
        Fallback: Always provide non-empty fallback_action with context-aware recovery strategies.
        Do not perform actions yourself — only report status. Maintain exact JSON structure.""" + f"\nActions list: {user_tasks[user_prompt].actions}",

        is_termination_msg=_is_terminate_msg,
    )
    return verify


def instantiate_helper_agent():
    # Inject cultural wisdom into Helper for warm, caring assistance
    _helper_cultural = ""
    try:
        from cultural_wisdom import get_cultural_prompt_compact
        _helper_cultural = get_cultural_prompt_compact()
    except Exception:
        pass

    helper = autogen.AssistantAgent(
        name="Helper",
        llm_config=llm_config,
        code_execution_config=False,
        system_message=f"""You are a Helper Agent with a caring, supportive nature.
{_helper_cultural}
        Focus: Assisting the Assistant Agent to complete actions with warmth and encouragement.
        Note: Do not coordinate with other agents. After your response, always pass the conversation back to the Assistant Agent.

        You serve as the system's self-healing component with these responsibilities:
        1. Monitor: Continously monitor responses for error patterns, especially JSON with {{"status": "error"}} format
        2. Diagnose: When error occur, carefully analyze error messages to identify root causes
        3. Repair: Take immediate corrective actions based on the specific error type:
            - For JSON format errors: use validate_json_response tool
            - For tool execution errors: Suggest parameter adjustments ot alternative tools
            - For API connection issues: Recommend retry strategies or fallback approaches
            - For Logical errors: Propose an alternative solution path
        4. Clearly explain what went wrong and how you're fixing it.
        5. After resolving the issue, help resume the original task flow

        Coding Instructions:
            Avoid using time.sleep in code.
            Instead, use the create_scheduled_jobs tool for tasks requiring timed intervals.
            If the Assistant Agent requests code with time.sleep, respond that it cannot be executed and utilize the create_scheduled_jobs tool instead.
            Always include proper error handling and logging.
            Ensure the final response is printed usin print() before returning it.
            If you want to send data proactively (on your own) to user use `@user {{"message2user": "message here"}}`. However, if you're responding to the user's request or instruction, use the send_message_to_user or send_message_in_seconds tool.
            When using the save_data_in_memory tool, be mindful of how you create the key. Ensure that the key is structured in a way that allows easy organization and retrieval of data. Use dot notation to create a logical key path. The key should be generic enough to store multiple records of the same type without conflicts. Avoid using specific values as part of the key
                For example:
                    - stories.story_name - Good key structure for storing multiple stories.
                    - creator.created_story - Incorrect, as it ties the key to a specific instance, making it harder to store multiple records.
            When receiving responses from tools that should return JSON, always use the validate_json_response tool to ensure valid JSON formatting before processing further. This helps prevent errors when parsing tool output.
        Data Management:
            Use the get_set_internal_memory tool to store or retrieve user information as needed.""",
        is_termination_msg=_is_terminate_msg,
    )
    return helper


def instantiate_assistant_agent(list_of_persona, user_prompt, personality=None, resonance_profile=None, autonomous=False):
    # Build personality injection for the primary user-facing agent
    _personality_block = ""
    if personality:
        try:
            from core.agent_personality import build_personality_prompt, build_proactive_vision_prompt
            _personality_block = build_personality_prompt(personality, resonance_profile=resonance_profile)
            _personality_block += build_proactive_vision_prompt(
                goal=user_tasks.get(user_prompt, Action("")).actions if user_prompt in user_tasks else ""
            )
        except Exception:
            pass
    # Expert agent guidance — domain-specific prompt enhancement
    _expert_block = ""
    try:
        from integrations.expert_agents import match_expert_for_context
        _actions_text = str(user_tasks.get(user_prompt, Action("")).actions) if user_prompt in user_tasks else ""
        _expert_match = match_expert_for_context(_actions_text)
        if _expert_match:
            _expert_block = _expert_match['prompt_block']
            tool_logger.info(f"Expert match: {_expert_match['name']} (score={_expert_match['score']})")
    except Exception:
        pass

    if not _personality_block:
        try:
            from cultural_wisdom import get_cultural_prompt_compact
            from core.agent_personality import get_regional_tone_prompt
            _personality_block = get_cultural_prompt_compact()
            _regional = get_regional_tone_prompt()  # resolves language internally
            if _regional:
                _personality_block += _regional
        except Exception:
            pass

    # The agent's GOAL, put where the executing agent can actually see it.
    #
    # ChatInstructor sends one line per step — "Execute Action 1: Write the
    # awareness hook" (create_recipe.py:5204) — and that line carries no
    # SUBJECT.  Nothing else in this system message names what the agent is
    # for either, so the model fills the hole by inventing one.  Live
    # 2026-08-21: marketing.local.funnel, whose stored goal is "a complete
    # marketing funnel for Nunba, the local-first AI agent", spent three
    # attempts writing about "sustainable urban gardening for busy city
    # professionals" and called google_search with "urban gardening messaging
    # hooks trends 2024".  The goal and every flow's sub_goal were on disk in
    # prompts/{id}.json the whole time.
    #
    # load_agent_config is the canonical mtime-cached reader (cache_loaders.py:158),
    # so this is a dict lookup on the hot path, and it returns None for
    # autonomous agents that have no config — hence the empty-block default.
    _goal_block = ''
    try:
        from core.cache_loaders import load_agent_config
        _pid = str(user_prompt).split('_', 1)[1] if '_' in str(user_prompt) else user_prompt
        _cfg = load_agent_config(_pid) or {}
        _goal = (_cfg.get('goal') or '').strip()
        _subs = [str(f.get('sub_goal') or '').strip() for f in (_cfg.get('flows') or [])]
        _subs = [s for s in _subs if s]
        if _goal or _subs:
            _goal_block = (
                "\n        •THIS AGENT'S GOAL - every action you are given serves THIS,\n"
                "         and nothing else.  Do NOT invent a different subject,\n"
                "         product, or audience.  If an action names no subject,\n"
                "         the subject is the goal below.\n"
                + (f"         Goal: {_goal}\n" if _goal else '')
                + ''.join(f"         Sub-goal: {s}\n" for s in _subs)
            )
    except Exception:
        tool_logger.debug('goal block unavailable for %s', user_prompt, exc_info=True)

    assistant = autogen.AssistantAgent(
        name="Assistant",
        llm_config=llm_config,
        code_execution_config={"last_n_messages": 2, "work_dir": get_coding_workspace_dir(), "use_docker": False},
        system_message=f"""{'AUTONOMOUS MODE: Do NOT ask the user questions. Use sensible defaults. Complete actions immediately without clarification.' if autonomous else 'INTERACTIVE MODE: You may ask the user clarifying questions to understand their vision before proceeding.'}
        Plain ASCII only in code and output — no emoji or non-ASCII characters.
{_goal_block}

        •HELPER IS YOUR SUPERMAN — DELEGATE EVERYTHING:
            The Helper agent has ALL the tools.  You have NONE.  For ANY task —
            web search, web scrape, file read, save to memory, fetch chat
            history, send message to user, schedule a job, generate image,
            generate video, run a desktop command, consult an expert, search
            long-term memory, anything at all — ALWAYS tag @Helper first.
            Never refuse a request with "I can't access X" or "I don't have
            tools for Y".  If a tool exists in the catalog below, @Helper has
            it.  If a tool doesn't exist, ask @Helper to find an alternative
            (search, scrape, code).  The ONLY thing Helper can't do is execute
            python code — that's @Executor's job.  Everything else goes through
            @Helper.  Treat Helper as your unlimited capability surface.

        •Purpose: The assistant executes actions provided by the ChatInstructor, seeks help from Helper and Executor agents when necessary, and ensures actions are completed accurately.
        •Action Flow:
            1. Receive Action: {'Associate the action with the assigned persona and proceed immediately.' if autonomous else 'Ask the UserProxy to associate the action with a persona (if multiple personas exist).'}
            2. Analyze Complexity:
                - Before executing, assess if the action is complex and requires breaking down into subtasks.
                - If the action involves multiple distinct steps, dynamic flows, or could fail partially, consider requesting breakdown via @StatusVerifier.
            3. Execution:
                - Understand and plan the current action execution.
                - Perform the action with the help of @Helper and @Executor agents.
                - Account for all the tools available with helper & whenever you are supposed to call a tool as part of current action ask @Helper.
                - If the action requires calculation, code execution or API endpoint call, CREATE code(python preferred) and ask @Executor agent to execute the created code.
                - RESPONSIBILITY ROUTING (CRITICAL — autonomous mode; boundaries are role responsibilities): No absent worker/scout/persona-agent will hand you inputs — the only agents here are you, @Helper, @Executor, @StatusVerifier, and you are already augmented with matched Expert Guidance (injected above). When an action assigns work that belongs to a DISTINCT role (e.g. "receive the list of supply gaps from the Demand Scout"), do NOT stall waiting for that role to appear and do NOT fake it with a "simulated" answer. Route it, in order: (1) DELEGATE — if the matched Expert Guidance or an existing @Helper tool already covers that responsibility, perform it yourself USING that guidance/tool (you are acting as the specialist for that step); (2) CREATE — otherwise request a breakdown via @StatusVerifier so the responsibility becomes its own role-bounded subtask, then execute it for real by gathering the inputs via @Helper (google_search, get_data_by_key, code via @Executor) — that executed subtask becomes the new specialist capability the hive learns and banks for reuse; (3) DO IT YOURSELF — only when the responsibility is already atomic enough that no separate specialist is warranted. Keep distinct roles as distinct subtasks — never collapse them into one fabricated guess, and never block on a role that is not present (that is the single most common cause of a stalled autonomous goal).
            4. After Completion:
                - If action completed successful & there is no error, ask @Helper to save the information(which will be required in future) in memory using 'save_data_in_memory' tool.
                - After save_data_in_memory has completed, ask the StatusVerifier to confirm completion and include the persona name.
                - After confirmation, request the next action from the ChatInstructor.
            5. If Failed:
                - Create a summary of the error and ask the UserProxy for help if needed.
                - Never assume; always seek user assistance for unresolved issues.
            6. Action Modifications:
                - If the action is modified, ask the user what measures should be taken if it fails in the future.
            7. Subtask Handling:
                - If @StatusVerifier returns "requires_breakdown" status, acknowledge and work through subtasks sequentially.
                - Complete each subtask before moving to the next dependent subtask.
                - Report subtask completion to @StatusVerifier for tracking.

        •Persona Association:
            list of persona:- """ + f'{list_of_persona}' + """
            Rules:
                - If there's only 1 persona in the list, associate that persona with all actions automatically.
                - If there are multiple personas, ask the @user to select the persona associated with each action.

        •Code Execution: Executor Agent: Executes code as needed. Ensure the final response is printed in code using print() before sending to Executor. Only executor can execute the code and not user, hence never ask user the code or code/api execution response.

        •Tools Helper Agent can use:
            1. The tools are: send_message_in_seconds,send_message_to_user,send_presynthesized_video_to_user,execute_windows_or_android_command,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, google_search, save_data_in_memory, search_long_term_memory and save_to_long_term_memory.
            2. Create Scheduled Jobs: For tasks involving timer or time or periodically or scheduled jobs, ask Helper agent to use the create_scheduled_jobs tool.
            3. Data/Memory Management:
                - If you want to save some data,understand the current data from get_saved_metadata & plan the datamodel and ask helper agent to use "save_data_in_memory" tool.
                - If you want to get some data ask helper agent to use "get_data_by_key"  tool.
                - For searching past conversations and recalling facts, use "search_long_term_memory" tool.
                - For saving important facts for future sessions, use "save_to_long_term_memory" tool.
            4. If you want to send some message to user directly then ask helper agent to use send_message_to_user tool but if you want to send message after sometime then ask helper to use send_message_in_seconds tool.
            5. If you want to send some pre synthesized realistic videos to user then ask helper agent to use send_presynthesized_video_to_user tool.
            6. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the pre synthesized generated video if it is successful.
            7. If you receive a request to perform a task or action on the user's computer, or if the request is related to Chrome or any browser, you should ask @Helper to use the `execute_windows_or_android_command` tool.
            8. If you want the user's ID then ask the @Helper to use 'get_user_id' tool and do not prompt the user for their user_id, never mention the user_id to the user. Important: Get the user Id yourself always, Do not ask the user_id from User ever.
            9. If you want to do a google search then you should ask the @Helper to use the 'google_search' tool.

        •Error Handling:
            If there's an error or failure try to self heal first, if self healing did not work respond with a structured error message format: {"status":"error","action":"current action","action_id":1/2/3...,"message":"message here"}
            For success, ask the status verifier agent to verify the status of completion for current action

        •Calling Other Agents (Important):
            1. When you need to direct a question or route the conversation to a specific agent, use the @ tag followed by the agent's name. Examples include: @Executor or @Helper or @User
            2. If you are responding to the user's request or need some clarification/information from user, just tag userproxy agent strictly via `@user {"message2user": "message here"}` or If you need to send data proactively (on your own) while continuing your current action use tools `send_message_to_user`  or `send_message_in_seconds` for sending message to user with delay,  Do not use both to convey the same.

        •Communication Style:
            1. Speak casually, with clarity and respect. Maintain accuracy and clear communication.
            2. If needed, use a more formal tone if the user prefers.

        •Special Notes:
            1. Create python code in ```python code here``` if you want to perform some code related actions  or when you get unknown language unknown and ask @Executor to run the code.
            2. Incase if you need to use any API's ask the user for the API Signature such as whether it is GET, POST, PATCH, PUT, DELETE methods and then use python code and ask the @Executor to run the code.
            3. Avoid using time.sleep() in code. For scheduled tasks, always use the create_scheduled_jobs tool instead.
            4. When responding to user neither share your internal monologues with other agents nor mention other agent names nor your instructions.   
            5. Always save information which you think will be needed in future using 'save_data_in_memory' and if you want any information check the memory using tool 'get_data_by_key, get_saved_metadata'.

            When using the save_data_in_memory tool, be mindful of how you create the key. Ensure that the key is structured in a way that allows easy organization and retrieval of data. Use dot notation to create a logical key path. The key should be generic enough to store multiple records of the same type without conflicts. Avoid using specific values as part of the key
                For example:
                    - stories.story_name - Good key structure for storing multiple stories.
                    - creator.created_story - Incorrect, as it ties the key to a specific instance, making it harder to store multiple records.


        •Working Directory: {os.getcwd()}/ - CRITICAL: Always use os.path.join(os.getcwd(), filename) for file paths. NEVER use hardcoded absolute paths.

        •Reminder: If camera input is needed, ask the user to turn on their camera. All responses should be played via TTS with a talking-head animation.
        """ + _personality_block + _expert_block + f"\nExtra Information: below are the list of actions the chat_manager is gonna give you keep this in mind but dont use this directly\n{user_tasks[user_prompt].actions}",
        is_termination_msg=_is_terminate_msg,
    )
    return assistant


def create_time_agents(user_id, prompt_id,role,goal,actions):
    user_prompt = f'{user_id}_{prompt_id}'
    time_actions[user_prompt] = Action(actions)

    time_agent = autogen.AssistantAgent(
        name='time_agent',
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=_is_terminate_msg,
        code_execution_config={"work_dir": get_coding_workspace_dir(), "use_docker": False},
        system_message="You are an helpful AI assistant used to perform time based tasks given to you. "
        f"""You can refer below details to perform task:
            Actions: <actionsStart>{user_tasks[user_prompt].actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{final_recipe[prompt_id]}<generalized_functionsEnd><recipeEnd>
            After completing the current action ask the StatusVerifier to verify the status of current action.
        """
        f"When you want to communicate with {role} connect main agent using 'connect_time_main' tool."
        "Tools Helper Agent can use [send_message_in_seconds,send_message_to_user,send_presynthesized_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, save_data_in_memory, search_long_term_memory and save_to_long_term_memory.]"
        "if you have any task which is not doable by these tool check recipe first else create python code to do so"
        "the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video."
        f'IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: `@user {{"message2user": "Your message here"}}`'
        "Return 'TERMINATE' when the task is done."
    )

    time_user = autogen.UserProxyAgent(
        name=f"user_proxy_{user_id}",
        human_input_mode="NEVER",
        llm_config=False,
        is_termination_msg=_is_terminate_msg,
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )
    helper1 = autogen.AssistantAgent(
        name="Helper",
        llm_config=llm_config,
        code_execution_config={"work_dir": get_coding_workspace_dir(), "use_docker": False},
        system_message=f"""You are Helper Agent. Help the {role} agent to complete the task:
{get_cultural_prompt()}
            1. Follow the steps below to achieve the goal: {goal}.
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than {role},Executor,multi_role_agent.
            4. Tools you have [send_message_in_seconds,send_message_to_user,send_presynthesized_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, save_data_in_memory, search_long_term_memory and save_to_long_term_memory.]
            5. Keep track of action and only go to next action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continuously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: `@user {{"message2user": "Your message here"}}`
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            Actions: <actionsStart>{user_tasks[user_prompt].actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{final_recipe[prompt_id]}<generalized_functionsEnd><recipeEnd>

            When writing code, always print the final response just before returning it.
        """,
        is_termination_msg=_is_terminate_msg,
    )
    executor1 = autogen.AssistantAgent(
        name="Executor",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": get_coding_workspace_dir(), "use_docker": False},
        system_message=f'''You are a executor agent. focused solely on creating, running & debugging code.
            Your responsibilities:
            1. Follow the steps below to achieve the goal: {goal}.
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than {role},Executor,multi_role_agent.
            4. Tools Helper Agent can use [send_message_in_seconds,send_message_to_user,send_presynthesized_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, save_data_in_memory, search_long_term_memory and save_to_long_term_memory.]
            5. Keep track of action and only go to next action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continuously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: `@{role} {{"message2user": "Your message here"}}`
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            Actions: <actionsStart>{user_tasks[user_prompt].actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{final_recipe[prompt_id]}<generalized_functionsEnd><recipeEnd>

            Note: Your Working Directory is "{os.getcwd()}" - CRITICAL: When writing code, ALWAYS use os.path.join(os.getcwd(), filename) for file paths. NEVER hardcode paths like '/home/user/path'.
            Add proper error handling, logging.
            Always provide clear execution results or error messages to the assistant.
            if you get any conversation which is not related to coding ask the manager to route this conversation to user
            When writing code, always print the final response just before returning it.
        ''',
        is_termination_msg=_is_terminate_msg,
    )
    multi_role_agent1 = autogen.AssistantAgent(
        name="multi_role_agent",
        llm_config=llm_config,
        code_execution_config=False,
        system_message="""You will send message from multiple different personas, your job is to ask those question to assistant agent
        if you think some text was intended for some other agent, but i came to you send the same message to user""",
    )
    verify1 = autogen.AssistantAgent(
        name="StatusVerifier",
        llm_config=llm_config,
        code_execution_config=False,
        system_message=""""You are an Status verification agent.
        Role: Track and verify the status of actions. Provide updates strictly in JSON format only when status is completed.
        Response formats:
            1. Action Completed Successfully: {"status": "completed","action": "current action","action_id": 1/2/3...,"message": "message here"}
            2. Action Error: {"status": "error","action": "current action","action_id": 1/2/3...,"message": "message here"}
            2. Action Pending: {"status": "pending","action": "current action","action_id": 1/2/3...,"message": "pending actions here"}
        Important Instructions:
            Only mark an action as "Completed" if the Assistant Agent confirms successful completion.
            For pending tasks or ongoing actions, respond to helper to complete the task.
            Verify the action performed by assistant and make sure the action is performed correctly as per instructions. if action performed was not as per instructions give the pending actions to the helper agent.
            Report status only—do not perform actions yourself.

        """,
        is_termination_msg=_is_terminate_msg,
    )

    chat_instructor1 = autogen.UserProxyAgent(
        name="ChatInstructor",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        default_auto_reply="TERMINATE",
        code_execution_config=False,
        is_termination_msg=_is_terminate_msg,
    )

    # --- Core tools for time_agent (reuse same definitions) ---
    from core.agent_tools import build_core_tool_closures, register_core_tools
    _tool_ctx_time = {
        'user_id': user_id, 'prompt_id': prompt_id,
        'agent_data': agent_data, 'helper_fun': helper_fun,
        'user_prompt': user_prompt, 'request_id_list': request_id_list,
        'recent_file_id': recent_file_id, 'scheduler': scheduler,
        'simplemem_store': user_simplemem.get(user_prompt) if user_simplemem else None,
        'memory_graph': None,
        'log_tool_execution': log_tool_execution,
        'send_message_to_user1': send_message_to_user1,
        'retrieve_json': retrieve_json,
        'strip_json_values': strip_json_values,
        'save_conversation_db': save_conversation_db,
    }
    core_tools_time = build_core_tool_closures(_tool_ctx_time)
    register_core_tools(core_tools_time, helper1, time_agent)

    # Channel tools for time_agent too
    try:
        from integrations.channels.agent_tools import register_channel_tools
        register_channel_tools(helper1, time_agent, _tool_ctx_time)
    except Exception:
        pass

    context_handling = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=AUTOGEN_HISTORY_LIMIT, keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET, max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE, min_tokens=0),
            ToolMessageHandler(user_tasks=user_tasks, user_prompt=user_prompt),
        ]
    )
    context_handling.add_to_agent(time_agent)
    context_handling.add_to_agent(helper1)
    context_handling.add_to_agent(executor1)
    context_handling.add_to_agent(multi_role_agent1)
    context_handling.add_to_agent(verify1)
    # See chat_instructor rationale at the recipe-create context_handling
    # block (line ~903).  Same unbounded-buffer risk applies in the
    # time-based-execution path; chat_instructor1 needs the same cap.
    context_handling.add_to_agent(chat_instructor1)

    time_agent_object = {}
    time_agent_object['time_agent'] = time_agent
    time_agent_object['time_user'] = time_user
    time_agent_object['helper1'] = helper1
    time_agent_object['executor1'] = executor1
    time_agent_object['multi_role_agent1'] = multi_role_agent1
    time_agent_object['verify1'] = verify1
    time_agent_object['chat_instructor1'] = chat_instructor1

    time_agent.description = 'Designed to handle specific tasks by interacting directly with other agents or the user. It acts as the primary orchestrator for task management and ensures tasks are completed efficiently'
    time_user.description = 'Acts as a user, performing tasks assigned by the Assistant Agent. It simulates user actions and provides results or feedback as required.'
    helper1.description = 'this is a helper agent that calls tools, facilitates task completion & assists other agents'
    executor1.description = 'this is an executor agent that Specialized agent for code execution & response handling'
    multi_role_agent1.description = 'Acts as an external agent with multi-functional capabilities. Note: This agent should never be directly invoked.'
    verify1.description = 'this is a verify status agent. which will verify the status of current action that will be called after ChatInstructor gives instruction to complete an action & assistant completes it, this agent will provide updates in a structured JSON format & then call user agent'
    chat_instructor1.description = 'this is a ChatInstructor agent that provides step-by-step action plans for task execution'

    def state_transition1(last_speaker, groupchat):
        current_app.logger.info('INSIDE TIMER STATE TRANSITION')
        messages = groupchat.messages
        if not messages:
            current_app.logger.warning("state_transition1 called with empty messages list")
            return time_agent
        # Publish agent-to-agent thought via the shared module-level
        # publisher so Timer-flow runs stream to the Nunba UI with the
        # exact same "Thinking" bubble format the main create flow
        # uses. Single publisher, single topic, no parallel paths.
        publish_agent_thought(last_speaker, messages, user_id)
        # visual_context = helper_fun.get_visual_context(user_id)
        # if visual_context:
        #     groupchat.messages.insert(-1,{'content':visual_context,'role':'user','name':'helper'})
        try:
            pattern = r'\{.*?\}' # getting all json from text
            matches = re.findall(pattern, messages[-1]["content"], re.DOTALL)
            json_objects = [json.loads(match) for match in matches]
            current_app.logger.info(f'Got Json as {len(json_objects)}')
            if json_objects:
                last_json = json_objects[-1]
                current_app.logger.info(f'last json as {last_json}')
                if 'status' in last_json.keys() and last_json['status'].lower() == 'completed':
                    current_app.logger.info('GOT COMPLETED FOR ACTION')
                    try:
                        time_actions[user_prompt].current_action += 1
                    except Exception:
                        current_app.logger.error('GOT ERROR WHILE UPDATING CURRENT ACTION')
                        time_actions[user_prompt].current_action += 1
                    return chat_instructor1

                currentaction_id = last_json['action_id']
                if final_recipe[prompt_id]['actions'][currentaction_id-1]['can_perform_without_user_input'] == 'yes':
                    return time_agent
        except Exception as e:
            current_app.logger.error(f'Got Error while getting json for current actionid: {e}')

        pattern3 = r"@statusverifier"
        if re.search(pattern3, messages[-1]["content"].lower()):
            current_app.logger.info("String contains @StatusVerifier returning StatusVerifier")
            return verify1

        current_app.logger.info(f'Inside state_transition with message :10 {messages[-1]["content"][:10]} & last_speaker {last_speaker.name}')
        # Agent names are case-sensitive.  The Helper agent is instantiated as
        # name="Helper" (like "Executor"/"multi_role_agent" alongside it here).
        # This was "helper" (lowercase) → it NEVER matched → Helper fell through
        # to the final `return "auto"` and the 4B got to pick the next speaker,
        # instead of the intended deterministic hand-back to the time_agent
        # orchestrator (the same role the main flow's Helper→assistant plays).
        # The other three names in this OR-chain are correctly cased, so this is
        # an unambiguous typo, not intentional exclusion.
        if last_speaker.name == f"user_proxy_{user_id}" or last_speaker.name == "multi_role_agent" or last_speaker.name == "Helper" or last_speaker.name == "Executor":
            return time_agent
        current_app.logger.info(f'Checking for @user or @user in message')
        if '@user' in messages[-1]["content"].lower():
            current_app.logger.info('GOT @USER in message')
            json_obj = retrieve_json(messages[-1]["content"])  # canonical parse (#95)
            if json_obj:
                try:
                    current_app.logger.info('Sending user the message')
                    send_message_to_user1(user_id,json_obj['message2user'],'',prompt_id)
                except Exception:
                    pass
                return "auto"

        if messages[-1]["role"] == 'function':
            current_app.logger.info('The last speaker was function returning assistant')
            return time_agent
        if 'exitcode:' in messages[-1]["content"]:
            current_app.logger.info('Got exitcode in text returning assistant')
            return time_agent
        if 'TERMINATE' in messages[-1]["content"].upper():
            current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
            # retrieve: action 1 -> action 2
            return None
        return "auto"

    select_speaker_transforms = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=AUTOGEN_HISTORY_LIMIT, keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET, max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE, min_tokens=0),
            ToolMessageHandler(user_tasks=user_tasks, user_prompt=user_prompt),
        ]
    )
    time_group_chat = autogen.GroupChat(
        agents=[time_agent, helper1, time_user,multi_role_agent1,executor1,chat_instructor1,verify1],
        messages=_seed_messages(user_id),  # same seed builder as main group_chat
        max_round=10,
        select_speaker_transform_messages=select_speaker_transforms,
        speaker_selection_method=state_transition1,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice (back-to-back assistant messages cause llama-server 400)
        send_introductions=False,
        role_for_select_speaker_messages='user',
    )

    time_manager = autogen.GroupChatManager(
        groupchat=time_group_chat,
        llm_config={"cache_seed": None,"config_list": config_list}
    )

    time_agent_object['time_group_chat'] = time_group_chat
    time_agent_object['time_manager'] = time_manager

    # Agent Ops Console Phase B: register the time-agent GroupChat for
    # live drill-down. Same canonical user_prompt key the time_agents
    # cache uses (caller does `time_agents[user_prompt] = create_time_
    # agents(...)`); we derive it here from user_id+prompt_id.
    try:
        from lifecycle_hooks import register_groupchat_for_session as _reg_gc
        _reg_gc(f'{user_id}_{prompt_id}', time_group_chat)
    except Exception:
        logging.getLogger(__name__).debug(
            "groupchat registry hook skipped for time_group_chat", exc_info=True)

    return time_agent_object







user_tasks = TTLCache(ttl_seconds=7200, max_size=500, name='create_user_tasks')
user_ledgers = TTLCache(ttl_seconds=7200, max_size=500, name='create_user_ledgers', loader=load_user_ledger)
user_delegation_bridges = TTLCache(ttl_seconds=7200, max_size=500, name='create_user_delegation_bridges')


# =============================================================================
# SMART LEDGER INTEGRATION HELPERS
# =============================================================================

def inject_ledger_awareness(message: str, user_prompt: str) -> str:
    """
    Inject ledger awareness context into an action message.

    This gives the agent full visibility into:
    - Previously executed tasks and their outcomes
    - Currently executing tasks
    - Next course of action

    Args:
        message: Original action message
        user_prompt: User prompt identifier

    Returns:
        Message with ledger awareness injected
    """
    if user_prompt not in user_ledgers:
        return message

    ledger = user_ledgers[user_prompt]
    try:
        awareness_text = ledger.get_awareness_text()
        # Inject awareness as context before the action
        return f"{awareness_text}\n\nNOW EXECUTE:\n{message}"
    except Exception as e:
        current_app.logger.warning(f"Failed to inject ledger awareness: {e}")
        return message


def complete_action_and_route(user_prompt: str, action_id: int, outcome: str, result: any = None):
    """
    Complete an action in the ledger and determine next task.

    Uses the smart routing to respect:
    - Hierarchical relationships (parent/child)
    - Prerequisites and dependencies
    - Outcome-based conditional tasks
    - Priority ordering

    Args:
        user_prompt: User prompt identifier
        action_id: The action ID that completed
        outcome: 'success' or 'failure'
        result: Optional result data

    Returns:
        Next task to execute, or None
    """
    if user_prompt not in user_ledgers:
        return None

    ledger = user_ledgers[user_prompt]
    task_id = f"action_{action_id}"

    try:
        next_task = ledger.complete_task_and_route(task_id, outcome, result)
        if next_task:
            current_app.logger.info(f"[Ledger Routing] Completed {task_id} -> Next: {next_task.task_id}: {next_task.description}")
        else:
            current_app.logger.info(f"[Ledger Routing] Completed {task_id} -> No next task available")
        return next_task
    except Exception as e:
        current_app.logger.error(f"Error in complete_action_and_route: {e}")
        return None


def get_smart_next_task(user_prompt: str):
    """
    Get the next task using smart routing from the ledger.

    This replaces simple get_ready_tasks with intelligent routing that considers:
    - Task relationships and dependencies
    - Outcome-based conditions
    - Priority and execution mode

    Args:
        user_prompt: User prompt identifier

    Returns:
        Next executable Task, or None
    """
    if user_prompt not in user_ledgers:
        return None

    ledger = user_ledgers[user_prompt]
    return ledger.get_next_executable_task()


def detect_and_add_dynamic_tasks(user_prompt: str, json_response: dict, current_action_id: int, user_message: str = ""):
    """
    Detect dynamically discovered tasks from LLM response and add to ledger.

    When the LLM identifies new tasks during execution, this function:
    1. Detects task-like content in the response
    2. Uses LLM classification to determine relationships
    3. Adds tasks to the ledger with proper wiring

    Args:
        user_prompt: User prompt identifier
        json_response: Parsed JSON response from LLM
        current_action_id: Current action being executed
        user_message: Latest user message for context

    Returns:
        List of created Task objects
    """
    if user_prompt not in user_ledgers:
        return []

    ledger = user_ledgers[user_prompt]
    created_tasks = []

    # Check for dynamic_tasks field in response
    if 'dynamic_tasks' in json_response:
        for task_desc in json_response['dynamic_tasks']:
            context = {
                'current_action_id': current_action_id,
                'previous_outcome': None,
                'user_message': user_message,
                'discovered_by': 'llm_response'
            }
            try:
                task = ledger.add_dynamic_task(task_desc, context)
                if task:
                    created_tasks.append(task)
                    current_app.logger.info(f"[Dynamic Task] Added: {task.task_id}: {task_desc}")
            except Exception as e:
                current_app.logger.warning(f"Failed to add dynamic task: {e}")

    # Check for follow_up_actions field
    if 'follow_up_actions' in json_response:
        for action in json_response['follow_up_actions']:
            action_desc = action if isinstance(action, str) else action.get('description', str(action))
            context = {
                'current_action_id': current_action_id,
                'previous_outcome': json_response.get('status', 'unknown'),
                'user_message': user_message,
                'discovered_by': 'follow_up'
            }
            try:
                task = ledger.add_dynamic_task(action_desc, context)
                if task:
                    created_tasks.append(task)
                    current_app.logger.info(f"[Follow-up Task] Added: {task.task_id}: {action_desc}")
            except Exception as e:
                current_app.logger.warning(f"Failed to add follow-up task: {e}")

    return created_tasks


def get_ledger_status_for_logging(user_prompt: str) -> str:
    """
    Get a compact ledger status string for logging.

    Args:
        user_prompt: User prompt identifier

    Returns:
        Status string like "Ledger: 5 tasks (2 done, 1 running, 2 pending)"
    """
    if user_prompt not in user_ledgers:
        return "Ledger: not initialized"

    ledger = user_ledgers[user_prompt]
    try:
        summary = ledger.get_execution_summary()
        return f"Ledger: {summary['total']} tasks ({len(summary['completed'])} done, {len(summary['in_progress'])} running, {len(summary['pending'])} pending)"
    except Exception:
        return "Ledger: status unavailable"


def should_continue_autonomously(user_prompt: str) -> bool:
    """
    Check if agent should continue working autonomously based on ledger state.

    Uses smart task routing to determine if there are executable tasks that
    respect relationships, prerequisites, and outcome-based conditions.

    Agent continues if:
    1. get_next_executable_task returns a task (smart routing)
    2. The task doesn't require user input
    3. Tasks are not all blocked

    Args:
        user_prompt: User prompt identifier

    Returns:
        True if agent should continue autonomously, False if user input needed
    """
    if user_prompt not in user_tasks:
        return False

    if not hasattr(user_tasks[user_prompt], 'ledger') or user_tasks[user_prompt].ledger is None:
        return False

    ledger = user_tasks[user_prompt].ledger

    # Use smart routing to find next executable task
    next_task = ledger.get_next_executable_task()

    if next_task:
        # Check if task requires user input based on context
        can_do_without_user = next_task.context.get('can_perform_without_user_input', True)
        blocked_reason = next_task.blocked_reason

        # Don't continue if task needs user input
        if blocked_reason == 'input_required' or not can_do_without_user:
            current_app.logger.info(f'[Autonomous] Next task requires user input: {next_task.description}')
            return False

        current_app.logger.info(f'[Autonomous] Found executable task via smart routing: {next_task.task_id}: {next_task.description}')
        return True

    # Check if there are tasks in progress
    in_progress_tasks = ledger.get_tasks_by_status(TaskStatus.IN_PROGRESS)
    if in_progress_tasks:
        current_app.logger.info(f'[Autonomous] {len(in_progress_tasks)} tasks in progress, continue working')
        return True

    # Check if all tasks are completed
    progress = ledger.get_progress_summary()
    if progress['pending'] == 0 and progress['in_progress'] == 0:
        current_app.logger.info(f'[Autonomous] All tasks complete: {progress["completed"]}/{progress["total"]}')
        return False

    # Check parallel executable tasks
    parallel_tasks = ledger.get_parallel_executable_tasks()
    if parallel_tasks:
        current_app.logger.info(f'[Autonomous] {len(parallel_tasks)} parallel tasks available')
        return True

    # If we have blocked tasks only, we need user input
    blocked_tasks = ledger.get_tasks_by_status(TaskStatus.BLOCKED)
    if blocked_tasks and not next_task:
        current_app.logger.info(f'[Autonomous] All remaining tasks blocked, need user input')
        return False

    return False

def create_action_with_ledger(actions: List[Dict], user_id: int, prompt_id: int, user_prompt: str,
                              flow_id: Optional[int] = None) -> Action:
    """
    Create an Action instance with Smart Ledger attached.

    This ensures task memory is maintained throughout agent execution,
    allowing reprioritization and tracking of all tasks (pre-assigned,
    autonomous, and user-requested).

    Args:
        actions: List of action dictionaries
        user_id: User ID
        prompt_id: Prompt ID
        user_prompt: Combined user_prompt string (user_id_prompt_id)
        flow_id: Recipe flow index this batch of actions belongs to.  Each
            call site here passes the CURRENT flow being executed.  When
            None, defaults to ``get_current_flow(user_prompt)`` so callers
            that don't know they should care still get correct stamping —
            the dashboard hierarchy (prompt → session → flow → action)
            depends on this being non-None.  See
            ``docs/architecture/TASK_LEDGER_GROUPING_FIX_PLAN.md`` for the
            grouping contract.

    Returns:
        Action instance with Smart Ledger attached
    """
    action_instance = Action(actions)

    # Resolve the active flow for this ledger.  Caller can override; if
    # they don't, fall back to the runtime flow tracker so 7+ existing
    # call sites here keep working without forced signature updates.
    if flow_id is None:
        try:
            flow_id = int(get_current_flow(user_prompt))
        except Exception:
            flow_id = 0

    # Create or load ledger with production backend (Redis with JSON fallback)
    if user_prompt not in user_ledgers:
        current_app.logger.info(f"Creating new Smart Ledger for {user_prompt}")
        backend = get_production_backend()  # Tries Redis, falls back to JSON (already imported from agent_ledger)
        ledger = create_ledger_from_actions(user_id, prompt_id, actions,
                                            backend=backend, flow_id=flow_id)
        user_ledgers[user_prompt] = ledger

        # Best-effort: when the Redis backend is live, enable ledger
        # pubsub + heartbeat so the distributed_agent subscribers (see
        # CHANNEL_DELEGATION wiring) can see and respond to this
        # ledger's delegations in real time.  Gated on
        # `hasattr(backend, 'redis_client')` — JSONBackend and
        # InMemoryBackend have no redis_client, so they skip cleanly.
        # Any exception is swallowed at debug; ledger still functions
        # without pubsub/heartbeat (single-node degraded mode).
        try:
            _redis = getattr(backend, 'redis_client', None)
            if _redis is not None:
                ledger.enable_pubsub(_redis)
                ledger.enable_heartbeat(
                    _redis,
                    host_info={
                        'user_id': user_id,
                        'prompt_id': prompt_id,
                        'mode': 'create',
                    },
                )
                current_app.logger.info(
                    f"Ledger pubsub+heartbeat enabled for {user_prompt}"
                )
        except Exception as _lsetup_e:
            current_app.logger.debug(
                f"Ledger pubsub/heartbeat setup skipped for "
                f"{user_prompt}: {_lsetup_e}"
            )

        # Register ledger for auto-sync from ActionState transitions
        register_ledger_for_session(user_prompt, ledger)
        current_app.logger.info(f"Registered ledger for auto-sync: {user_prompt}")

        # Create TaskDelegationBridge for this ledger
        delegation_bridge = TaskDelegationBridge(a2a_context, ledger)
        user_delegation_bridges[user_prompt] = delegation_bridge
        current_app.logger.info(f"Created TaskDelegationBridge for {user_prompt}")
    else:
        current_app.logger.info(f"Reusing existing Smart Ledger for {user_prompt}")
        ledger = user_ledgers[user_prompt]

        # Ensure delegation bridge exists
        if user_prompt not in user_delegation_bridges:
            delegation_bridge = TaskDelegationBridge(a2a_context, ledger)
            user_delegation_bridges[user_prompt] = delegation_bridge
            current_app.logger.info(f"Created TaskDelegationBridge for existing ledger {user_prompt}")

        # Add any new actions that aren't already in ledger
        for action in actions:
            task_id = f"action_{action.get('action_id', 'unknown')}"
            if task_id not in ledger.tasks:
                has_prereqs = bool(action.get('prerequisites', []))
                execution_mode = ExecutionMode.SEQUENTIAL if has_prereqs else ExecutionMode.PARALLEL

                task = Task(
                    task_id=task_id,
                    description=action.get('description', action.get('action', '')),
                    task_type=TaskType.PRE_ASSIGNED,
                    execution_mode=execution_mode,
                    status=TaskStatus.PENDING,
                    prerequisites=[f"action_{p}" for p in action.get('prerequisites', [])],
                    context={
                        "action_id": action.get('action_id'),
                        "flow": action.get('flow'),
                        "persona": action.get('persona')
                    },
                    priority=100 - action.get('action_id', 0)
                )
                ledger.add_task(task)

    # Attach ledger to Action instance
    action_instance.set_ledger(ledger)
    return action_instance

_REMEDY_MAX_ATTEMPTS = 3


def _remedy_replay_exceeded(attempts, key, limit=_REMEDY_MAX_ATTEMPTS):
    """Count an attempt at `key`; True once it has been tried `limit` times.

    Guards the two remedy sites in the main loop that re-issue a
    BYTE-IDENTICAL prompt whenever their guard condition survives the
    remedy.  Neither condition is changed by the remedy failing:

      * recipe-not-banked -> ask the model for a recipe.  If the model
        does not save one, ``os.path.exists(_recipe_file)`` is still
        False next iteration, so the same request is sent again.
      * completion-claim-rejected -> tell the model to keep working.  If
        it re-claims the same task, ``_claim_valid`` is False again, so
        the same rejection text is sent again.

    Measured in llm_outbound.jsonl (1,190 records): 982 of 1088 calls
    re-sent an already-sent payload; the worst single payload went out
    222 times, the recipe request 170 times and the rejection 75 times.
    That is what filled 19.3h of cumulative LLM latency, not model speed
    -- the loop's existing bounds (max_iterations=300, 30-min pipeline
    timeout) cap the damage but do not stop the replay.

    Bounding the remedy, not the loop, is the fix: after `limit` tries
    the caller takes its escape path instead of re-asking.
    """
    attempts[key] = attempts.get(key, 0) + 1
    return attempts[key] > limit


_RETRY_TAG_PREFIX = '[retry:'


def _retry_marker(tag):
    """The sentinel that identifies a retry prompt in group-chat history."""
    return f'{_RETRY_TAG_PREFIX}{tag}]'


def send_retry(group_chat, sender, manager, tag, message, logger=None):
    """Send a retry prompt that REPLACES the previous one instead of stacking.

    Every remedy site re-issues its prompt with ``clear_history=False``, so
    before this helper each attempt APPENDED another copy.  Autogen re-sends
    the whole message list on every call, so N attempts cost O(N) copies in
    the context window and O(N^2) tokens over the loop -- the mechanism
    behind the measurements in ``_remedy_replay_exceeded``'s docstring (982
    of 1088 calls re-sent an already-sent payload; worst payload 222x) and
    behind the 14,330 "Finish what you started" occurrences observed live
    2026-08-05.

    Retrying is legitimate; PAYING for it repeatedly is not.  A retry is a
    correction of the previous one, so history should hold the newest and
    forget the rest.  This drops every earlier message carrying the same
    tag, then sends the new one tagged, leaving AT MOST ONE retry per tag in
    context no matter how many attempts run.  Token cost becomes flat in the
    attempt count instead of quadratic, and the model sees one current
    instruction rather than a wall of near-identical repeats it is being
    told not to repeat.

    Tags are per-remedy so the sites do not clear each other: a pending
    recipe request must survive while a claim rejection is retried.

    MUTATES ``group_chat.messages`` IN PLACE (``msgs[:] = keep``).  Autogen
    holds a reference to that exact list object, so rebinding it would leave
    the manager writing to a detached list -- the same stale-reference shape
    as the frozen-stdout rotation bug (#621).  Do not "simplify" this to an
    assignment.

    Returns whatever ``initiate_chat`` returns; callers are unchanged
    otherwise.
    """
    marker = _retry_marker(tag)
    msgs = getattr(group_chat, 'messages', None)
    dropped = 0
    if isinstance(msgs, list) and msgs:
        keep = []
        for _m in msgs:
            _c = _m.get('content') if isinstance(_m, dict) else None
            if isinstance(_c, str) and marker in _c:
                dropped += 1
                continue
            keep.append(_m)
        if dropped:
            msgs[:] = keep          # in place -- see docstring
    if dropped and logger is not None:
        try:
            logger.info(
                "[RETRY-REPLACE] tag=%s dropped %d stale cop%s from history "
                "(history now %d msgs)",
                tag, dropped, 'y' if dropped == 1 else 'ies', len(msgs))
        except Exception:
            pass
    return sender.initiate_chat(
        recipient=manager, message=f'{marker} {message}', clear_history=False)


# The memory-injection sites (2406/2659/2711 above) append this hint to the
# LAST group-chat message.  It is model-facing text; it must never reach the
# user, and its presence on a terminate message defeats an exact
# == 'TERMINATE' comparison (live 2026-08-23 21:52: the user's reply was
# 'TERMINATE' plus six accumulated skeleton lines).
_MEMORY_SKELETON_PREFIX = 'Metadata/skeleton of all keys'


def _strip_memory_skeleton(text):
    lines = [ln for ln in str(text).splitlines()
             if not ln.lstrip().startswith(_MEMORY_SKELETON_PREFIX)]
    return '\n'.join(lines).strip()


def _is_terminate(content):
    # Narrower than helper._is_terminate_msg on purpose: that one is autogen's
    # is_termination_msg predicate and matches TERMINATE anywhere in the
    # content (cheap false positives — a round just ends).  Here a false
    # positive SKIPS a legitimate user-bound reply, so only a message that IS
    # the terminate token (after removing the skeleton suffix) qualifies.
    return _strip_memory_skeleton(content).upper().startswith('TERMINATE')


def _needs_input_reply(action_id, action_text):
    step = f' ("{action_text}")' if action_text else ''
    return (f"I need your input to finish building this agent. Step {action_id}"
            f"{step} isn't coming together from what I have so far — tell me "
            f"more about how this step should work, and I'll continue building "
            f"from there.")


def get_response_group(user_id,text,prompt_id,Failure=False,error=None):
    """
    Handles the response generation process for an agent group.
    Args:
        user_id: User identifier
        text: Input text message
        prompt_id: Prompt identifier
        Failure: Whether this is being called after a failure
        error: Error information if there was a failure
    Returns:
        Response content from the conversation
    """
    user_prompt = f'{user_id}_{prompt_id}'
    current_app.logger.info(f"START: get_response_group for user_prompt={user_prompt}, Failure={Failure}")
    # Get or create agents for this user
    if user_prompt not in user_agents:
        current_app.logger.info(f"Creating new agents for user_prompt={user_prompt}")
        try:
            author, assistant_agent, executor, group_chat, manager, chat_instructor, agents_object = create_agents(
                user_id, user_tasks[user_prompt], prompt_id)
            user_agents[user_prompt] = (author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object)
            messages[user_prompt] = []
            current_app.logger.info(f"Successfully created agents for user_prompt={user_prompt}")
        except Exception as e:
            current_app.logger.error(f"Failed to create agents for user_prompt={user_prompt}: {e}")
            current_app.logger.error(traceback.format_exc())
            return f"Error creating agents: {str(e)}"
    else:
        current_app.logger.info(f"Using existing agents for user_prompt={user_prompt}")
        author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_prompt]
    clear_history = False

    # TOOL CALL AND RESPONSE CHECK with TIMEOUT
    tool_timeout = 2  # Timeout in seconds (adjust as needed)
    current_time = time.time()
    if len(group_chat.messages)>2 and 'tool_calls' in group_chat.messages[-1]:
        current_app.logger.warning('GOT INPUT BUT LAST MESSAGE IS tool_calls should wait for tool response')
        return 'Processing a tool now please try later'

    if Failure:
        current_app.logger.warning(f'CHECK THIS OUT group_chat.messages:{group_chat.messages[-5:]}')
        current_app.logger.warning(f'CHECK THIS OUT group_chat.messages:{len(group_chat.messages)}')
        for i in range(len(group_chat.messages)):
            group_chat.messages[i]['role'] = 'user'
        clear_history = False
        if user_tasks[user_prompt].fallback == True or user_tasks[user_prompt].recipe == True:
            message = 'Lets continue the work we were doing if action is completed then ask status verifier Agent to Please tell the status of the action'
            text = f'Properly Execute Action {user_tasks[user_prompt].current_action}: {message} '
        else:
            try:
                message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action - 1)
                text = f'Properly Execute Action {user_tasks[user_prompt].current_action}: {message} '
            except Exception:
                message = ""
                text = f'Properly Execute Action {user_tasks[user_prompt].current_action}: {message} '
    # Initiate or resume chat
    try:
        current_app.logger.info(f"Messages in user_prompt before init: {len(messages.get(user_prompt, []))}")

        if len(messages[user_prompt]) > 0:
            # last_agent, last_message = manager.resume(messages=messages[user_prompt])
            try:
                result = agents_object['user'].initiate_chat(recipient=manager, message=text, clear_history=clear_history,silent=False)
            except Exception as e:
                current_app.logger.error(f'Got some error it can be multiple tools called at one error:{e}')
                current_app.logger.error(traceback.format_exc())
                # current_app.logger.error(f'len of group chat :{group_chat.messages}')
                # current_app.logger.error(f' group chat :{group_chat.messages}')
                for i in range(len(group_chat.messages)):
                    group_chat.messages[i]['role'] = 'user'
                message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action - 1)
                text = f'Execute Action {user_tasks[user_prompt].current_action}: {message}'
                result = agents_object['helper'].initiate_chat(recipient=manager, message=text, clear_history=True,silent=False)
                return "I've encountered an issue but I'm trying to auto heal and recover"


        else:
            config = get_prompt_config_json(prompt_id)

            total_actions_for_current_flow = get_total_actions_length_for_flow(config, get_current_flow(user_prompt))

            current_app.logger.warning(
                f"current_action_id {user_tasks[user_prompt].current_action} for actions of length {total_actions_for_current_flow} and ")

            should_continue, early_response = safe_action_boundary_check(user_prompt, prompt_id, text, user_id)
            if not should_continue:
                return early_response

            current_action_id = user_tasks[user_prompt].current_action

            # Guard: clamp action_id to valid range (prevents IndexError when
            # config has more actions than the Action object's internal list)
            _action_count = len(user_tasks[user_prompt].actions)
            if current_action_id - 1 >= _action_count:
                current_app.logger.warning(
                    f"Action index {current_action_id - 1} >= action count {_action_count}, "
                    f"clamping to last action")
                current_action_id = _action_count
                user_tasks[user_prompt].current_action = current_action_id

            message = user_tasks[user_prompt].get_action(current_action_id - 1)
            current_state = get_action_state(user_prompt, current_action_id)

            # Expert hint for current action (higher threshold — only inject on strong match)
            _action_expert_hint = ""
            try:
                from integrations.expert_agents import match_expert_for_context
                _action_match = match_expert_for_context(str(message), min_score=6)
                if _action_match:
                    _caps = ', '.join(c['name'] for c in _action_match['capabilities'][:2])
                    _action_expert_hint = f"\n[Expert Tip from {_action_match['name']}]: Focus on {_caps}"
            except Exception:
                pass

            message = f'Execute Action {user_tasks[user_prompt].current_action}: {message} '+f',Latest User message: {text}' + _action_expert_hint
            _push_thinking(user_id, f'Executing action {user_tasks[user_prompt].current_action} of {_action_count}...')
            # No second publish here.  `message` at this point is the raw model
            # instruction — 'Execute Action N: <prompt> ,Latest User message:
            # <...>' plus the [Expert Tip from ...] hint — and publishing it put
            # the whole internal prompt in the user's thinking bubble, right
            # after the clean line above (task #649).  The user got two bubbles
            # per action: one readable, one internal.  The readable one already
            # carries the signal, so the internal one is pure leak.
            task_time[prompt_id] = {'timer':time.time(),'times':[]}

            # Only transition if we're in ASSIGNED state (first time)
            if current_state == ActionState.ASSIGNED:
                #lifecycle2 ASSIGNED->IN_PROGRESS
                safe_set_state(user_prompt, current_action_id, ActionState.IN_PROGRESS,
                               "first action start")
            else:
                force_state_through_valid_path(user_prompt, current_action_id, ActionState.IN_PROGRESS,
                               "first action start")
                current_app.logger.warning(
                    f"Expected ASSIGNED state but found {current_state.value} for action {current_action_id}")
            result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)

        # FIX: autogen clears group_chat.messages after initiate_chat() returns.
        # Recover from chat_instructor's own chat_messages which ARE preserved.
        _chat_history = chat_instructor.chat_messages.get(manager, [])
        if _chat_history and len(group_chat.messages) == 0:
            group_chat.messages.extend(_chat_history)
            current_app.logger.info(f"[MSG-RECOVERY] Recovered {len(_chat_history)} messages from chat_instructor")
        current_app.logger.info(f"group_chat.messages len={len(group_chat.messages)}")

        # USER-INPUT GATE clear (companion to the gate set in
        # state_transition's pending handler):  this function is called
        # from /chat once per user message, so the arrival of THIS call
        # IS the user's reply.  Clear any sticky `_needs_user_input_action_id`
        # flag set by a prior call so the OUTER loop doesn't break out
        # before processing the new user input.
        try:
            if hasattr(user_tasks[user_prompt], '_needs_user_input_action_id'):
                _prior_block = user_tasks[user_prompt]._needs_user_input_action_id
                user_tasks[user_prompt]._needs_user_input_action_id = None
                current_app.logger.info(
                    f"[USER-INPUT-GATE] Clearing prior block on action "
                    f"{_prior_block} — fresh /chat call indicates user has "
                    f"replied; OUTER loop will resume normal iteration."
                )
        except Exception as _gate_clear_err:
            current_app.logger.debug(
                f"[USER-INPUT-GATE] flag clear failed (non-blocking): {_gate_clear_err}"
            )

        # Main processing loop
        while_loop_iterations = 0
        max_iterations = 300  # Time-based: ~5s per iteration = ~25 min max
        _pipeline_start = time.time()
        _pipeline_timeout = 1800  # 30 minutes — CREATE is the learning phase, needs time
        # Per-run replay ledger for _remedy_replay_exceeded (see its docstring).
        _remedy_attempts = {}

        while while_loop_iterations < max_iterations:
            # Hard timeout: don't let pipeline run forever
            if time.time() - _pipeline_start > _pipeline_timeout:
                current_app.logger.warning(f"Pipeline timeout after {_pipeline_timeout}s, saving progress")
                break
            while_loop_iterations += 1
            current_action_id = user_tasks[user_prompt].current_action
            json_obj = None  # Reset each iteration — set by state_transition JSON parse paths

            current_app.logger.info(f"WHILE LOOP ITERATION #{while_loop_iterations} , Current Action Id:{current_action_id}")

            # USER-INPUT GATE (code-level enforcement):  if state_transition
            # has flagged this action as blocked on user input
            # (`can_perform_without_user_input: no`), break the OUTER loop
            # immediately so control returns to the user.  Without this
            # break the OUTER loop would keep re-executing the action and
            # the StatusVerifier would eventually flip to `yes` via
            # autonomous prompt drift, hallucinating a user confirmation
            # that never happened (2026-05-08 incident: Action 3 looped 8
            # iterations before fabricating "Sitemap structure confirmed"
            # from a user who had only typed "create a website").  The
            # flag is sticky per (user_prompt, action_id) — once set, the
            # action stays blocked until a fresh /chat call provides a
            # user reply (the /chat handler is responsible for clearing
            # the flag when the user responds).
            try:
                _blocked_action_id = getattr(
                    user_tasks[user_prompt], '_needs_user_input_action_id', None,
                )
                if _blocked_action_id is not None and _blocked_action_id == current_action_id:
                    current_app.logger.info(
                        f"[USER-INPUT-GATE] OUTER loop breaking at iteration "
                        f"#{while_loop_iterations}: Action {current_action_id} is "
                        f"flagged as needing user input.  Returning control to user; "
                        f"the agent's question is in the assistant's last message."
                    )
                    break
            except Exception as _gate_err:
                current_app.logger.debug(
                    f"[USER-INPUT-GATE] outer-loop gate check failed (non-blocking): {_gate_err}"
                )

            # === LEDGER v2.0: Heartbeat + Budget/SLA using KNOWN state (not LLM) ===
            _ledger = user_ledgers.get(user_prompt)
            if _ledger:
                _task_id = f"action_{current_action_id}"
                _task = _ledger.tasks.get(_task_id)
                if _task:
                    # Heartbeat: agent is alive and working on this action
                    _task.heartbeat()

                    # Budget enforcement: abort if budget exhausted
                    if _task.is_budget_exhausted():
                        current_app.logger.warning(
                            f"[BUDGET] Task {_task_id} budget exhausted "
                            f"(spark={_task.spark_spent}/{_task.spark_budget}, "
                            f"time={_task.time_spent_s}/{_task.time_budget_s})")
                        _task.post_status("Budget exhausted — aborting", progress_pct=_task.progress_pct)
                        safe_set_state(user_prompt, current_action_id, ActionState.ERROR,
                                       "budget exhausted")
                        break

                    # SLA advisory: flag breach but don't block
                    if _task.is_sla_breached() and not _task.sla_breached:
                        _task.mark_sla_breached()
                        _task.post_status("SLA breached — continuing but flagged")
                        current_app.logger.warning(f"[SLA] Task {_task_id} SLA breached")

            track_lifecycle_hooks(current_action_id, group_chat, user_prompt)

            # Load persona info from config
            role = load_persona_role(prompt_id, user_prompt)

            current_app.logger.info('inside while')
            current_state = get_action_state(user_prompt, current_action_id)

            # No-progress stall guard (REACHABLE — placed before the condition
            # branches below that `continue` past the late guard ~4463, whose
            # `_any_recipes` progress check is also suppressed once an EARLIER
            # action saved a recipe).  An action stuck in a "requested" state
            # whose OWN recipe never arrives otherwise spins to max_iterations
            # (300, ~25 min).  Per-action counter via stall_guard_step; resets
            # on any progress.  Live 2026-06-04: a chat via the speculative-
            # expert CREATE path stalled on action 2 (recipe_requested; action 1
            # already done) and looped all 300 -> generic TERMINATE, no history.
            _ut = user_tasks[user_prompt]
            _sg_key, _sg_iters, _sg_break = stall_guard_step(
                getattr(_ut, '_stall_key', None),
                getattr(_ut, '_stall_iters', 0),
                current_action_id, current_state,
                os.path.exists(os.path.join(
                    PROMPTS_DIR,
                    f'{prompt_id}_{get_current_flow(user_prompt)}_{current_action_id}.json')),
            )
            _ut._stall_key, _ut._stall_iters = _sg_key, _sg_iters
            if _sg_break:
                current_app.logger.warning(
                    f"[STALL-GUARD] action {current_action_id} stuck in "
                    f"{current_state.value} for {_sg_iters} iterations with no "
                    f"recipe — breaking clean (was spinning to max_iterations)")
                break

            # Second no-progress SIGNAL feeding the SAME break decision above —
            # not a second guard.  stall_guard_step counts consecutive iterations
            # in ONE state and resets on every transition and every terminal
            # state, so it cannot see an action going round in a circle: live
            # 2026-08-04 action 2 churned assigned -> in_progress ->
            # status_verification_requested -> completed -> terminated ->
            # recipe_requested -> terminated -> recipe_requested ->
            # recipe_received, resetting the stall counter the whole way while
            # the loop ran toward max_iterations and starved the chat hot path.
            _cg_key, _cg_entries, _cg_break = cycle_guard_step(
                getattr(_ut, '_cycle_key', None),
                getattr(_ut, '_cycle_entries', None),
                current_action_id, current_state,
                os.path.exists(helper_fun.safe_prompt_path(
                    prompt_id, get_current_flow(user_prompt), current_action_id)),
            )
            _ut._cycle_key, _ut._cycle_entries = _cg_key, _cg_entries
            if _cg_break:
                current_app.logger.warning(
                    f"[CYCLE-GUARD] action {current_action_id} re-entered the "
                    f"same states without finishing "
                    f"({dict((s.value, n) for s, n in _cg_entries.items())}) — "
                    f"breaking clean instead of draining max_iterations")
                break

            if group_chat.messages and group_chat.messages[-1]['name'] == 'ChatInstructor' and _is_terminate(group_chat.messages[-1]['content']):
                current_app.logger.info(f"group_chat.messages[-2]['content'] {group_chat.messages[-2]['content'][:10]}..")
                json_obj = retrieve_json(group_chat.messages[-2]["content"])

                # LIFECYCLE HOOK - Check if JSON status is valid
                hook_result = lifecycle_hook_process_verifier_response(user_prompt, json_obj,
                                                                       user_tasks)  # 4-6. Process verifier response

                if hook_result['action'] != 'allow':
                    if hook_result['action'] == 'force_fallback':
                        # Automatically request fallback after completion
                        safe_set_state(user_prompt, user_tasks[user_prompt].current_action, ActionState.FALLBACK_REQUESTED, "hook_result force_fallback")
                        # Set flags for fallback flow
                        user_tasks[user_prompt].fallback = True
                        user_tasks[user_prompt].recipe = False

                    current_app.logger.error(f"lifecycle_hook_check_json_status {hook_result['message']}")
                    message = hook_result['message']
                    result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False)
                    continue

                recipe_result = lifecycle_hook_track_recipe_completion(user_prompt, json_obj, user_tasks)

                if recipe_result['action'] == 'save_recipe_and_terminate':
                    # Only set state here - don't do business logic yet
                    current_app.logger.info(' Recipe completion detected - state updated to RECIPE_RECEIVED')

                if not json_obj:
                    json_obj = individual_json[user_prompt]
                if json_obj and type(json_obj)==dict and 'status' in json_obj.keys():
                    if json_obj['status'].lower() == 'requires_breakdown':
                        # Handle subtask breakdown in main loop
                        current_app.logger.info(f"[Main Loop] Action {current_action_id} requires breakdown")
                        if 'subtasks' in json_obj and len(json_obj['subtasks']) > 0:
                            success = add_subtasks_to_ledger(
                                user_prompt, current_action_id, json_obj['subtasks'], user_ledgers
                            )
                            if success:
                                current_app.logger.info(f"Added {len(json_obj['subtasks'])} subtasks from main loop")
                                # Auto-sync handles ledger update via safe_set_state below
                        safe_set_state(user_prompt, current_action_id, ActionState.PENDING, "breakdown requested")
                        # Continue to work on subtasks
                        pending_subtasks = get_pending_subtasks(user_prompt, current_action_id, user_ledgers)
                        if pending_subtasks:
                            next_subtask = pending_subtasks[0]
                            message = f"Work on subtask: {next_subtask.description}"
                            result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False)
                        continue
                    elif json_obj['status'].lower() == 'completed' and 'recipe' not in json_obj.keys():
                        json_action_id = int(float(json_obj.get('action_id', current_action_id)))

                        # === LLM HALLUCINATION DEFENSE ===
                        # Cross-reference LLM-claimed action_id against KNOWN state.
                        # The pipeline knows which action was assigned — the LLM can lie.
                        _claim_valid = True
                        _rejection_reason = None
                        _claim_ledger = user_ledgers.get(user_prompt)

                        # Check 1: Does the LLM-claimed action_id match what we assigned?
                        if json_action_id != current_action_id:
                            current_app.logger.warning(
                                f"[HALLUCINATION?] LLM claims action_id={json_action_id} "
                                f"but pipeline assigned action_id={current_action_id}")
                            # Use the KNOWN action_id from scope — not the LLM's claim
                            json_action_id = current_action_id

                        if _claim_ledger:
                            _claimed_task_id = f"action_{json_action_id}"
                            _claimed_task = _claim_ledger.tasks.get(_claimed_task_id)

                            if _claimed_task:
                                from agent_ledger import TaskStatus as _TS
                                # Already completed (e.g. by state_transition during initiate_chat)?
                                # Skip re-processing, just advance to next action.
                                if _claimed_task.status in (_TS.COMPLETED, _TS.TERMINATED,
                                                            _TS.CANCELLED, _TS.SKIPPED):
                                    # Check if recipe was already saved for this action
                                    flow = get_current_flow(user_prompt)
                                    _recipe_file = helper_fun.safe_prompt_path(prompt_id, flow, json_action_id)
                                    if not os.path.exists(_recipe_file):
                                        # Bank from the execution trace FIRST — the tool calls
                                        # that already ran ARE the recipe; re-asking the 4B is
                                        # a fallible LLM round-trip that left flows unbanked
                                        # for weeks (memory: flywheel_action_banking_gap_
                                        # 2026-06-11, goal 60834540771).
                                        _bank_action_recipe_from_trace(
                                            user_prompt, prompt_id, flow, json_action_id,
                                            group_chat)
                                    if not os.path.exists(_recipe_file):
                                        if _remedy_replay_exceeded(
                                                _remedy_attempts,
                                                ('recipe', prompt_id, json_action_id)):
                                            # Asked _REMEDY_MAX_ATTEMPTS times and the file is
                                            # STILL absent.  os.path.exists() above is unchanged
                                            # by the model failing to save, so re-entering this
                                            # branch re-sends a byte-identical request forever
                                            # (measured 170x).  Advance unbanked instead — the
                                            # action itself did complete; only its recipe is
                                            # missing, and _bank_action_recipe_from_trace already
                                            # had first refusal.
                                            current_app.logger.warning(
                                                f"[RECIPE-GIVEUP] {_claimed_task_id} action "
                                                f"{json_action_id}: still unbanked after "
                                                f"{_REMEDY_MAX_ATTEMPTS} requests — advancing "
                                                f"without a recipe rather than replaying")
                                            if json_action_id < len(user_tasks[user_prompt].actions):
                                                user_tasks[user_prompt].current_action = json_action_id + 1
                                                user_tasks[user_prompt].recipe = False
                                        else:
                                            # Trace banking failed — request it from the model
                                            current_app.logger.info(
                                                f"[RECIPE-NEEDED] {_claimed_task_id} completed but recipe not saved, requesting")
                                            user_tasks[user_prompt].recipe = True
                                            user_tasks[user_prompt].fallback = False
                                            _recipe_msg = request_recipe_for_action(json_action_id, prompt_id, role, user_prompt)
                                            # Replace the previous request instead of stacking it:
                                            # os.path.exists(_recipe_file) is still False next lap
                                            # if the model did not bank one, so the SAME request
                                            # goes out again (measured 170x).  One copy in context.
                                            result = send_retry(
                                                group_chat, chat_instructor, manager,
                                                f'recipe-{json_action_id}', _recipe_msg,
                                                logger=current_app.logger)
                                            _rh = chat_instructor.chat_messages.get(manager, [])
                                            if _rh and len(group_chat.messages) == 0:
                                                group_chat.messages.extend(_rh)
                                    else:
                                        current_app.logger.info(
                                            f"[ALREADY DONE] {_claimed_task_id} completed + recipe saved, advancing")
                                        if json_action_id < len(user_tasks[user_prompt].actions):
                                            user_tasks[user_prompt].current_action = json_action_id + 1
                                            user_tasks[user_prompt].recipe = False
                                            user_tasks[user_prompt].fallback = False
                                            current_app.logger.info(
                                                f"[ADVANCE] current_action: {json_action_id} -> {json_action_id + 1}")
                                        else:
                                            user_tasks[user_prompt].current_action = json_action_id
                                            user_tasks[user_prompt].fallback = True
                                            user_tasks[user_prompt].recipe = False
                                            current_app.logger.info(
                                                f"[LAST-ACTION] action {json_action_id} is last in flow")
                                    if json_action_id < len(user_tasks[user_prompt].actions) and os.path.exists(_recipe_file):
                                        continue

                                # Check: Integrity — has task data been corrupted?
                                elif _claim_valid and not _claimed_task.verify_integrity():
                                    _claim_valid = False
                                    _rejection_reason = (
                                        f"Task {_claimed_task_id} integrity check failed — "
                                        f"data_hash mismatch, possible corruption")
                            else:
                                _claim_valid = False
                                _rejection_reason = f"Task {_claimed_task_id} not found in ledger"

                        if not _claim_valid:
                            current_app.logger.error(f"[CLAIM REJECTED] {_rejection_reason}")
                            if _remedy_replay_exceeded(
                                    _remedy_attempts,
                                    ('claim', prompt_id, current_action_id, _rejection_reason)):
                                # Same action rejected for the same reason
                                # _REMEDY_MAX_ATTEMPTS times.  _claim_valid is recomputed from
                                # whatever the model claims next, so a model that keeps
                                # re-claiming one task keeps landing here with identical text
                                # (measured 75x).  Stop and keep the progress already saved —
                                # the same thing the wall-clock timeout above does.
                                current_app.logger.error(
                                    f"[CLAIM-GIVEUP] action {current_action_id} rejected "
                                    f"{_REMEDY_MAX_ATTEMPTS}x for an unchanged reason — "
                                    f"stopping instead of replaying: {_rejection_reason}")
                                break
                            message = (f"Your completion claim was rejected: {_rejection_reason}. "
                                       f"Continue working on action {current_action_id}.")
                            # Replace the previous rejection instead of stacking it:
                            # _claim_valid is recomputed each lap, so a model that
                            # keeps re-claiming lands here with identical text
                            # (measured 75x).  One copy in context, not 75.
                            result = send_retry(
                                group_chat, chat_instructor, manager,
                                f'claim-{current_action_id}', message,
                                logger=current_app.logger)
                            continue

                        # Only set COMPLETED if not already done by state_transition
                        _current_state = get_action_state(user_prompt, json_action_id)
                        if _current_state != ActionState.COMPLETED:
                            force_state_through_valid_path(user_prompt, json_action_id, ActionState.COMPLETED,
                                                           "verified complete")
                        # Auto-sync handles ledger update via force_state_through_valid_path above

                        # Use smart ledger routing to complete and find next task
                        result_data = json_obj.get('result', json_obj.get('output', None))
                        next_ledger_task = complete_action_and_route(user_prompt, json_action_id, 'success', result_data)

                        # Detect and add any dynamic tasks from the response
                        detect_and_add_dynamic_tasks(user_prompt, json_obj, json_action_id, text)

                        # Log ledger status
                        current_app.logger.info(f"[Ledger] {get_ledger_status_for_logging(user_prompt)}")

                        if not user_tasks[user_prompt].fallback and not user_tasks[user_prompt].recipe:
                            # Check if we can move to next action
                            if json_action_id > len(user_tasks[user_prompt].actions):
                                # Last action completed
                                user_tasks[user_prompt].fallback = True
                            else:
                                # Move to next action
                                user_tasks[user_prompt].current_action = json_action_id
                                user_tasks[user_prompt].fallback = True
                else:
                    current_app.logger.warning(f'it is not a json object the error is:')
                    current_app.logger.info('it is not a json object You should ask status verifier to give response in proper format & not move ahead to next action')
                    if group_chat.messages and group_chat.messages[-1]['role'] == 'tool':
                        current_app.logger.info('GOT role is tool')
                        break
                    # FIX: Better message construction based on current state
                    if current_state == ActionState.FALLBACK_REQUESTED:
                        message = f"@Assistant: To Get Action {current_action_id} fallback: Ask USER what actions should be taken if current actions fail in the future"
                    elif current_state == ActionState.RECIPE_REQUESTED:
                        message = request_recipe_for_action(current_action_id,  prompt_id, role, user_prompt)
                    elif current_state == ActionState.FALLBACK_RECEIVED:
                        message = set_fallback_flags_and_request_recipe(chat_instructor, current_action_id, manager, prompt_id, role, user_prompt)
                        continue
                    else:
                        actions_prompt = user_tasks[user_prompt].get_action(current_action_id - 1)
                        message = f'Finish what you started, Do not go into loop and do not repeat same thing in different way, Continue with action {current_action_id}: {actions_prompt}'

                    # Replace the previous nudge instead of stacking it.  This is
                    # the site that produced 14,330 copies of "Finish what you
                    # started, Do not go into loop..." on 2026-08-05 — an
                    # anti-repetition instruction that was itself repeated into
                    # the context window.  Unlike the recipe/claim sites it has
                    # no _remedy_replay_exceeded bound, so replacement (not a
                    # cap) is what keeps it correct AND cheap.
                    result = send_retry(
                        group_chat, agents_object['helper'], manager,
                        f'nojson-{current_action_id}', message,
                        logger=current_app.logger)
                    continue

                current_app.logger.info('resuming chat')
                current_action_id = user_tasks[user_prompt].current_action

                #When all actions in a particular flow ends or for the last action
                if current_action_id >= len(user_tasks[user_prompt].actions):
                    if user_tasks[user_prompt].recipe == True:  # Request Recipe For last action
                        message = request_recipe_for_action_last(current_action_id, prompt_id, role, user_prompt)

                    elif user_tasks[user_prompt].fallback == True:  # Request fallback For last action
                        message = request_fallback_for_action_last(current_action_id,  user_prompt)

                    else:  # All actions should be in terminated state now
                        # Check if ledger has pending tasks that can be done autonomously
                        if should_continue_autonomously(user_prompt):
                            # Use smart routing to get next task
                            next_task = get_smart_next_task(user_prompt)
                            if next_task:
                                current_app.logger.info(f'[Autonomous] Smart routing: Next task {next_task.task_id}: {next_task.description}')
                                # Inject ledger awareness into the message
                                message = f'Continue with next pending task: {next_task.description}'
                                message = inject_ledger_awareness(message, user_prompt)
                                result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False, silent=False)
                                continue

                        # BEFORE moving to next action lets do THESE SAFETY CHECKS:
                        lifecycle_check = lifecycle_hook_check_all_actions_terminated(user_prompt, user_tasks)

                        if lifecycle_check['action'] != 'allow':
                            current_app.logger.error(f"lifecycle_hook_enforce_complete_lifecycle {lifecycle_check['message']}")
                            message = lifecycle_check['message']

                            # Only initiate chat if there's an actual message (not None)
                            # None would trigger AutoGen to ask for interactive input, causing EOFError
                            if message:
                                result = chat_instructor.initiate_chat(recipient=manager, message=message,
                                                                       clear_history=False)
                            else:
                                current_app.logger.info(f"Lifecycle check action '{lifecycle_check['action']}' with no message - continuing")
                            continue

                        flow, message, text = after_all_actions_terminated(assistant_agent, chat_instructor, group_chat,
                                                                           json_obj, manager,  prompt_id, text,
                                                                           user_prompt)

                        # Save the flow recipe (topologically sorted + scheduler)
                        _save_flow_recipe(flow, prompt_id, user_prompt, user_id, group_chat)

                        if get_current_flow(user_prompt)  < get_total_flows(user_prompt):
                            _next_flow = get_current_flow(user_prompt) + 1
                            _total_flows = get_total_flows(user_prompt)
                            _push_thinking(user_id, f'Flow {_next_flow} of {_total_flows}: Starting next persona...')
                            current_app.logger.info(f'Completed ONE FLOW NOW WE SHOULD WORK ON NEXT FLOW')
                            current_app.logger.info(f'DELETE CURRENT AGENTS AND CREATE NEW')
                            config = get_prompt_config_json(prompt_id)
                            flow_actions = config['flows'][get_current_flow(user_prompt)]['actions']
                            # Fresh ledger for new flow
                            if user_prompt in user_ledgers:
                                del user_ledgers[user_prompt]
                            user_tasks[user_prompt] = create_action_with_ledger(flow_actions, user_id, prompt_id, user_prompt)
                            del user_agents[user_prompt]
                            x = get_response_group(user_id,text,prompt_id)
                            continue
                        scheduler_check[user_prompt] = True
                        safe_increment_flow(user_prompt, prompt_id)
                        current_app.logger.info(f'[ALL-FLOWS-DONE] Agent creation complete')
                        return 'Agent created successfully'
                    result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                else:
                    # user_tasks[user_prompt].current_action = int(json_obj['action_id'])
                    current_app.logger.info(f'current action {user_tasks[user_prompt].current_action} & fallback {user_tasks[user_prompt].fallback} & recipe {user_tasks[user_prompt].recipe}')
                    user_tasks[user_prompt].new_json.append(json_obj)
                    try:
                        message = user_tasks[user_prompt].get_action(current_action_id - 1)
                    except Exception:
                        flow, json_response = after_all_actions_terminated_from_exception(assistant_agent, chat_instructor, flow,
                                                                                          group_chat, manager, prompt_id, user_prompt)
                        # Save flow recipe (same as Path 1 and Path 2)
                        _save_flow_recipe(flow, prompt_id, user_prompt, user_id, group_chat)

                        if all_flows_completed(prompt_id, get_total_flows(user_prompt), user_prompt):
                            update_agent_creation_to_db(prompt_id)
                            current_app.logger.info('[ALL-FLOWS-DONE] Agent creation complete (exception path)')
                            return 'Agent Created Successfully'
                        else:
                            # Advance to next flow — same cleanup as Path 1/Path 2
                            user_tasks[user_prompt].recipe = False
                            user_tasks[user_prompt].fallback = False
                            safe_increment_flow(user_prompt, prompt_id)
                            config = get_prompt_config_json(prompt_id)
                            flow_actions = config['flows'][get_current_flow(user_prompt)]['actions']
                            if user_prompt in user_ledgers:
                                del user_ledgers[user_prompt]
                            user_tasks[user_prompt] = create_action_with_ledger(flow_actions, user_id, prompt_id, user_prompt)
                            del user_agents[user_prompt]

                    current_app.logger.info('checking for fallback and recipe')

                    if user_tasks[user_prompt].recipe == True:
                        message = request_recipe_for_action(current_action_id,  prompt_id, role, user_prompt)
                    elif user_tasks[user_prompt].fallback == True:
                        message = request_fallback_for_action(current_action_id,  user_prompt)
                    else:
                        # user_tasks[user_prompt].current_action = user_tasks[user_prompt].current_action+1
                        message = get_execute_next_action_message(prompt_id, user_prompt)
                        # `message` is the model instruction, not a user-facing
                        # line — publishing it put the internal prompt in the
                        # thinking bubble (task #649).  Announce the action in
                        # words instead; `message` still goes to the model below.
                        _push_thinking(user_id, f'Starting action {current_action_id}...')
                        safe_set_state(user_prompt, current_action_id, ActionState.IN_PROGRESS, "action start")

                    result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False, silent=False)

                    # REPLACE the force_state_through_valid_path line with:
                    current_state = get_action_state(user_prompt, current_action_id)

                    # Only set IN_PROGRESS for appropriate states
                    if current_state == ActionState.ASSIGNED:
                        # New action starting
                        force_state_through_valid_path(user_prompt, current_action_id, ActionState.IN_PROGRESS,
                                                       "new action start")
                    elif current_state == ActionState.ERROR:
                        # Retrying after error
                        force_state_through_valid_path(user_prompt, current_action_id, ActionState.IN_PROGRESS,
                                                       "retry after error")
                    elif current_state in [ActionState.COMPLETED, ActionState.TERMINATED]:
                        # Action already done - this is likely why you're seeing the error
                        current_app.logger.warning(
                            f"Action {current_action_id} already {current_state.value}, not changing state")
                    elif current_state == ActionState.FALLBACK_REQUESTED and user_tasks[user_prompt].fallback:
                        # Force route to user for fallback
                        message = request_fallback_for_action(current_action_id, user_prompt)
                        result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,
                                                               silent=False)

                        continue
                    else:
                        # For other states (FALLBACK_REQUESTED, RECIPE_REQUESTED, etc.), keep current state
                        current_app.logger.info(f"Keeping current state: {current_state.value}")

                current_app.logger.info("\n=== Chat Summary ===")
                current_app.logger.info("\n=== Full response ===")
                # current_app.logger.info(result)
            elif current_state == ActionState.FALLBACK_REQUESTED and user_tasks[user_prompt].fallback:
                # Force route to user for fallback
                message = request_fallback_for_action(current_action_id,  user_prompt)
                result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,
                                                       silent=False)

                continue

            elif group_chat.messages and group_chat.messages[-1]['content'].startswith('Focus on the current task at hand'):
                result = agents_object['assistant'].initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                continue
            elif user_tasks[user_prompt].current_action <= len(user_tasks[user_prompt].actions):
                # Check if the current action was already completed by state_transition
                # or ledger routing. If so, advance instead of spinning.
                _ca = user_tasks[user_prompt].current_action
                _ca_state = get_action_state(user_prompt, _ca)
                # Also check ledger status (state_transition may not have run for this action)
                _ca_ledger_done = False
                _ca_ledger = user_ledgers.get(user_prompt)
                if _ca_ledger:
                    _ca_task = _ca_ledger.tasks.get(f"action_{_ca}")
                    if _ca_task:
                        from agent_ledger import TaskStatus as _TS
                        _ca_ledger_done = _ca_task.status in (_TS.COMPLETED, _TS.TERMINATED)
                if _ca_state in (ActionState.COMPLETED, ActionState.TERMINATED, ActionState.GAVE_UP) or _ca_ledger_done:
                    # Check if recipe file exists before advancing
                    _flow = get_current_flow(user_prompt)
                    _recipe_path = helper_fun.safe_prompt_path(prompt_id, _flow, _ca)
                    if not os.path.exists(_recipe_path):
                        # #89: count consecutive re-requests for THIS action with
                        # no recipe landing — each one means the model's prior
                        # recipe response failed to parse.  Pass the PRIOR-failure
                        # count so the request escalates a corrective "emit ONLY
                        # JSON" directive instead of re-sending the same prompt.
                        _pf = getattr(user_tasks[user_prompt], '_recipe_parse_failures', None)
                        if _pf is None:
                            _pf = {}
                            user_tasks[user_prompt]._recipe_parse_failures = _pf
                        _prior = _pf.get(_ca, 0)
                        _pf[_ca] = _prior + 1
                        current_app.logger.info(
                            f'[AUTO-ADVANCE] action {_ca} done but recipe not saved '
                            f'(attempt {_prior + 1}) — requesting recipe')
                        user_tasks[user_prompt].recipe = True
                        user_tasks[user_prompt].fallback = False
                        # Directly request recipe via initiate_chat
                        _recipe_msg = request_recipe_for_action(
                            _ca, prompt_id, role, user_prompt, parse_failures=_prior)
                        try:
                            result = chat_instructor.initiate_chat(
                                recipient=manager, message=_recipe_msg, clear_history=False, silent=False)
                        except Exception as _recipe_err:
                            current_app.logger.warning(f'[RECIPE-REQUEST] initiate_chat failed: {_recipe_err}, will retry')
                            # Don't fake a recipe — retry on next iteration
                        # Recover messages
                        _rh = chat_instructor.chat_messages.get(manager, [])
                        if _rh and len(group_chat.messages) == 0:
                            group_chat.messages.extend(_rh)
                        # Fallback: if state_transition didn't save the recipe file,
                        # search recovered messages for status:done JSON and save it
                        _flow = get_current_flow(user_prompt)
                        _rfile = helper_fun.safe_prompt_path(prompt_id, _flow, _ca)
                        if not os.path.exists(_rfile) and group_chat.messages:
                            for _msg in reversed(group_chat.messages):
                                _rj = retrieve_json(_msg.get('content', ''))
                                if (_rj and isinstance(_rj, dict) and _rj.get('status') == 'done'
                                        and isinstance(_rj.get('recipe'), list) and len(_rj['recipe']) > 0):
                                    _rj.setdefault('action_id', _ca)
                                    for _step in _rj.get('recipe', []):
                                        if _step.get('tool_name'):
                                            _step['agent_to_perform_this_action'] = 'Helper'
                                        elif _step.get('generalized_functions'):
                                            _step['agent_to_perform_this_action'] = 'Executor'
                                        else:
                                            _step['agent_to_perform_this_action'] = 'Assistant'
                                    atomic_json_write(_rfile, _rj)
                                    current_app.logger.info(f'[FALLBACK-SAVE] Saved recipe from messages at: {_rfile}')
                                    break
                    else:
                        current_app.logger.info(
                            f'[AUTO-ADVANCE] action {_ca} already {_ca_state.value}, advancing')
                        if _ca < len(user_tasks[user_prompt].actions):
                            user_tasks[user_prompt].current_action = _ca + 1
                            user_tasks[user_prompt].recipe = False
                            user_tasks[user_prompt].fallback = False
                            current_app.logger.info(f'[ADVANCE] current_action: {_ca} -> {_ca + 1}')
                            continue
                        else:
                            # Last action in flow — ensure all actions are TERMINATED
                            current_app.logger.info(f'[FLOW-COMPLETE] All {_ca} actions done in flow, ensuring termination')
                            for _aid in range(1, _ca + 1):
                                _astate = get_action_state(user_prompt, _aid)
                                if _astate in (ActionState.TERMINATED, ActionState.GAVE_UP):
                                    continue  # already terminal
                                # #139: a VERIFIED action (COMPLETED/RECIPE_RECEIVED) becomes
                                # TERMINATED (ledger COMPLETED); a stalled/unverified action
                                # force-abandoned at flow-complete is an HONEST give-up ->
                                # GAVE_UP (ledger FAILED), re-openable so the daemon can retry
                                # it via a hive peer. This is the fix for the fake-success bug.
                                _verified = _astate in (ActionState.COMPLETED, ActionState.RECIPE_RECEIVED)
                                _target = ActionState.TERMINATED if _verified else ActionState.GAVE_UP
                                current_app.logger.info(f'[FLOW-COMPLETE] Forcing action {_aid} from {_astate.value} to {_target.value}')
                                force_state_through_valid_path(user_prompt, _aid, _target, "flow complete")

                            # All actions terminated — create flow recipe and advance
                            current_app.logger.info(f'[FLOW-COMPLETE] All actions terminated, creating flow recipe')
                            _push_thinking(user_id, f'All actions complete. Building final recipe...')
                            # Ensure group_chat has messages for after_all_actions_terminated
                            if len(group_chat.messages) == 0:
                                group_chat.messages.append({
                                    'content': f'All {_ca} actions completed for this flow.',
                                    'role': 'user', 'name': 'ChatInstructor'
                                })
                            # json_obj is initialized to None at loop top.
                            # If state_transition didn't set it, create a default.
                            if not json_obj or not isinstance(json_obj, dict):
                                json_obj = {'status': 'completed', 'action_id': _ca}
                            flow, message, text = after_all_actions_terminated(
                                assistant_agent, chat_instructor, group_chat,
                                json_obj, manager, prompt_id, text, user_prompt)

                            # Save the flow recipe (same function as first path)
                            _save_flow_recipe(flow, prompt_id, user_prompt, user_id, group_chat)

                            if get_current_flow(user_prompt) < get_total_flows(user_prompt):
                                current_app.logger.info(f'[NEXT-FLOW] Completed flow {get_current_flow(user_prompt)}, starting next')
                                config = get_prompt_config_json(prompt_id)
                                flow_actions = config['flows'][get_current_flow(user_prompt)]['actions']
                                # Fresh ledger for new flow — old one tracked previous flow's actions
                                if user_prompt in user_ledgers:
                                    del user_ledgers[user_prompt]
                                user_tasks[user_prompt] = create_action_with_ledger(flow_actions, user_id, prompt_id, user_prompt)
                                del user_agents[user_prompt]
                                x = get_response_group(user_id, text, prompt_id)
                                continue

                            # All flows done
                            scheduler_check[user_prompt] = True
                            safe_increment_flow(user_prompt, prompt_id)
                            current_app.logger.info(f'[ALL-FLOWS-DONE] Agent creation complete')
                            return 'Agent created successfully'
                current_app.logger.info(f'current action {user_tasks[user_prompt].current_action} and length of actions is {len(user_tasks[user_prompt].actions)} but no matching condition found')

                # If the current action hasn't been executed yet, start it
                _ca_pending = user_tasks[user_prompt].current_action
                _ca_pending_state = get_action_state(user_prompt, _ca_pending)
                if _ca_pending_state in (ActionState.ASSIGNED, ActionState.PENDING, ActionState.IN_PROGRESS):
                    # Track retries to detect actions stuck needing user input
                    if not hasattr(user_tasks[user_prompt], '_exec_retries'):
                        user_tasks[user_prompt]._exec_retries = {}
                    user_tasks[user_prompt]._exec_retries[_ca_pending] = user_tasks[user_prompt]._exec_retries.get(_ca_pending, 0) + 1
                    _attempt = user_tasks[user_prompt]._exec_retries[_ca_pending]

                    if _attempt > 3:
                        # Action keeps failing — it needs user input.  Reset the
                        # counter so the NEXT turn (whose text is threaded into
                        # _exec_msg as `Latest User message`) actually re-attempts:
                        # live 2026-08-23 21:55 the counter climbed 5->9 across four
                        # turns, each exiting at iteration #1, so the user's answers
                        # were never consumed.  And return a QUESTION — breaking out
                        # here fell through to the tail-message return, which handed
                        # the user the raw 'TERMINATE\n Metadata/skeleton...' text.
                        user_tasks[user_prompt]._exec_retries[_ca_pending] = 0
                        try:
                            _stuck_action_text = user_tasks[user_prompt].get_action(_ca_pending - 1)
                        except Exception:
                            _stuck_action_text = ''
                        current_app.logger.info(
                            f'[NEEDS-INPUT] action {_ca_pending} not completing after {_attempt-1} attempts, '
                            f'returning control to user')
                        return _needs_input_reply(_ca_pending, _stuck_action_text)

                    actions_prompt = user_tasks[user_prompt].get_action(_ca_pending - 1)
                    current_app.logger.info(f'[EXECUTE-PENDING] Starting action {_ca_pending} (attempt {_attempt}): {actions_prompt}')
                    safe_set_state(user_prompt, _ca_pending, ActionState.IN_PROGRESS, "executing pending action")
                    _exec_msg = f'Execute Action {_ca_pending}: {actions_prompt} ,Latest User message: {text}'
                    try:
                        result = chat_instructor.initiate_chat(
                            recipient=manager, message=_exec_msg, clear_history=False, silent=False)
                    except Exception as _exec_err:
                        current_app.logger.warning(f'[EXECUTE-PENDING] initiate_chat failed: {_exec_err}')
                        continue  # Retry on next iteration
                    # Recover messages after initiate_chat
                    _chat_history = chat_instructor.chat_messages.get(manager, [])
                    if _chat_history and len(group_chat.messages) == 0:
                        group_chat.messages.extend(_chat_history)
                        current_app.logger.info(f"[MSG-RECOVERY] Recovered {len(_chat_history)} messages")
                    continue

                if len(group_chat.messages) == 0:
                    current_app.logger.warning("No messages in group chat after processing")
                    break
                last_message = group_chat.messages[-1]

                if 'tool_calls' in last_message:
                    current_app.logger.info(
                        f'current action {user_tasks[user_prompt].current_action} continuing since we need to wait for tool cal response')

                    continue

                if _is_terminate(last_message['content']) and len(group_chat.messages) > 1:
                    last_message = group_chat.messages[-2]

                if f'message2user'.lower() in last_message['content'].lower():
                    json_obj = retrieve_json(last_message["content"])
                    if json_obj:
                        try:
                            last_message['content'] = json_obj['message2user']
                        except Exception:
                            pass
                    return last_message['content']
                elif f'message2'.lower() in last_message['content'].lower():
                    try:
                        json_obj = retrieve_json(last_message['content'])
                        if json_obj and 'message2' in json_obj:
                            last_message['content'] = json_obj['message2']
                            return last_message['content']

                    except Exception as e:
                        current_app.logger.error(f"Error extracting JSON: {e}")
                        # Fallback to a basic pattern match if retrieve_json fails
                        pattern = r'@user\s*{[\'"]message2[\'"]\s*:\s*[\'"](.+?)[\'"]}'
                        match = re.search(pattern, last_message['content'], re.DOTALL)
                        if match:
                            last_message['content'] = match.group(1)
                            return last_message['content']
                execute_action_pattern = r'execute\s+action\s*\d*\s*:?'
                if re.search(execute_action_pattern, last_message['content'], re.IGNORECASE):
                    result = agents_object['assistant'].initiate_chat(recipient=manager, message=last_message['content'],
                                                                      clear_history=False, silent=False)
                else:
                    continue


            # Continue with existing termination logic
            if user_tasks[user_prompt].current_action > len(user_tasks[user_prompt].actions):
                current_app.logger.info(f'current action {user_tasks[user_prompt].current_action} is greater than length {len(user_tasks[user_prompt].actions)}')
                break

            if not lifecycle_hook_track_termination(user_prompt, user_tasks, group_chat) and not lifecycle_hook_track_fallback_request(user_prompt, user_tasks, group_chat):
                messages[user_prompt] = group_chat.messages

                if len(group_chat.messages) == 0:
                    current_app.logger.warning("No messages in group chat after processing")
                    return "I encountered an issue processing your request. Please try again."

                last_message = group_chat.messages[-1]
                if _is_terminate(last_message['content']) and len(group_chat.messages) > 1:
                    last_message = group_chat.messages[-2]

                if f'message2user'.lower() in last_message['content'].lower():
                    json_obj = retrieve_json(last_message["content"])
                    if json_obj:
                        try:
                            last_message['content'] = json_obj['message2user']
                            return last_message['content']
                        except Exception:
                            pass
                elif f'message2'.lower() in last_message['content'].lower():
                    try:
                        json_obj = retrieve_json(last_message['content'])
                        if json_obj and 'message2' in json_obj:
                            last_message['content'] = json_obj['message2']
                            return last_message['content']

                    except Exception as e:
                        current_app.logger.error(f"Error extracting JSON: {e}")
                        # Fallback to a basic pattern match if retrieve_json fails
                        pattern = r'@user\s*{[\'"]message2[\'"]\s*:\s*[\'"](.+?)[\'"]}'
                        match = re.search(pattern, last_message['content'], re.DOTALL)
                        if match:
                            last_message['content'] = match.group(1)
                            return last_message['content']
                else:
                    continue

            # Only break on stuck state if NO progress has been made (no recipes saved, no advances)
            if while_loop_iterations > 30 and current_state in [ActionState.FALLBACK_REQUESTED,
                                                               ActionState.RECIPE_REQUESTED]:
                # Check if we've made any progress at all
                _any_recipes = any(
                    os.path.exists(helper_fun.safe_prompt_path(prompt_id, get_current_flow(user_prompt), i))
                    for i in range(1, current_action_id + 1)
                )
                if not _any_recipes:
                    current_app.logger.warning(f"Stuck in {current_state.value} state with no progress, attempting recovery")
                    if current_state == ActionState.FALLBACK_REQUESTED:
                        message = f"Ask @User for fallback actions if Action {current_action_id} fails"
                    else:
                        message = f"Create recipe for Action {current_action_id}"
                    result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,
                                                           silent=False)
                    break

        # Log loop exit
        if while_loop_iterations >= max_iterations:
            current_app.logger.warning(f"Exited while loop after reaching max iterations ({max_iterations})")
        else:
            current_app.logger.info(f"Exited while loop after {while_loop_iterations} iterations")

        # Store messages and prepare final response
        messages[user_prompt] = group_chat.messages

        if len(group_chat.messages) == 0:
            current_app.logger.warning("No messages in group chat after processing")
            return "I encountered an issue processing your request. Please try again."

        last_message = group_chat.messages[-1]
        if _is_terminate(last_message['content']) and len(group_chat.messages) > 1:
            last_message = group_chat.messages[-2]

        if f'message2user'.lower() in last_message['content'].lower():
            json_obj = retrieve_json(last_message["content"])
            if json_obj:
                try:
                    last_message['content'] = json_obj['message2user']
                except Exception:
                    pass
        elif f'message2'.lower() in last_message['content'].lower():
            try:
                json_obj = retrieve_json(last_message['content'])
                if json_obj and 'message2' in json_obj:
                    last_message['content'] = json_obj['message2']
                    return last_message['content']

            except Exception as e:
                current_app.logger.error(f"Error extracting JSON: {e}")
                # Fallback to a basic pattern match if retrieve_json fails
                pattern = r'@user\s*{[\'"]message2[\'"]\s*:\s*[\'"](.+?)[\'"]}'
                match = re.search(pattern, last_message['content'], re.DOTALL)
                if match:
                    last_message['content'] = match.group(1)
                    return last_message['content']
        return _strip_memory_skeleton(last_message['content'])
    except Exception as e:
        current_app.logger.error(f"Unhandled exception in get_response_group: {e}")
        safe_set_state(user_prompt, user_tasks[user_prompt].current_action, ActionState.ERROR, "Unhandled exception in get_response_group")
        current_app.logger.error(traceback.format_exc())
        return f"An error occurred: {str(e)}"


def get_total_flows(user_prompt):
    return total_persona_actions[user_prompt]


def all_flows_completed(prompt_id, total_personas, user_prompt):
    """Check if ALL flows for ALL personas are complete"""
    config = get_prompt_config_json(prompt_id)

    # Check each flow is complete
    for flow_idx, flow in enumerate(config['flows']):
        flow_recipe_file = helper_fun.safe_prompt_path(prompt_id, flow_idx, 'recipe')
        if not os.path.exists(flow_recipe_file):
            return False

        # Check all actions in flow are terminal (TERMINATED, or a give-up GAVE_UP)
        for action_id in range(1, len(flow['actions']) + 1):
            if get_action_state(user_prompt, action_id) not in (ActionState.TERMINATED, ActionState.GAVE_UP):
                return False

    return True
def after_all_actions_terminated(assistant_agent, chat_instructor, group_chat, json_obj, manager,  prompt_id,
                                 text, user_prompt):
    # Only proceed with next action logic if 'allow'
    # Only proceed if action completed full lifecycle (DONE state)
    user_tasks[user_prompt].new_json.append(json_obj)
    safe_increment_action(user_prompt) # all actions completed
    current_app.logger.info('updating updated action in .json')
    individual_recipe = []
    flow = get_current_flow(user_prompt)
    set_individual_recipes(flow, individual_recipe, prompt_id, user_prompt)
    group_chat.messages[-1]['content'] = f'{individual_recipe}'
    assistant_agent.update_system_message = 'Check if the current_action depends on any other action, regardless of order it can be before or after this action. If yes, return the list of action IDs that this action depends on to ChatInstructor (e.g., [1,2]). Otherwise, return an empty array []. \nIMPORTANT: Respond strictly in an array [] format.'
    flow = get_current_flow(user_prompt)
    for num, action in enumerate(user_tasks[user_prompt].actions, 1):
        try:
            group_chat.messages[-1]['content'] = f'{individual_recipe}'
            message = f'''Check if the current_action depends on any other action, regardless of order it can be before or after this action. If yes, return the list of action IDs that this action depends on to ChatInstructor (e.g., [1,2]). Otherwise, return an empty array []. \nIMPORTANT: Respond strictly in an array [] format.\n current_action: {action}'''
            result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,
                                                   silent=False)
            match = None
            for i in range(1, 4):
                text = group_chat.messages[-i]['content']
                match = re.search(r'\[.*?\]', text)
                if match:
                    break
            if match:
                action_ids = ast.literal_eval(match.group())

                file_path = helper_fun.safe_prompt_path(prompt_id, flow, num)
                with open(file_path, 'r') as f:
                    data = json.load(f)
                data['actions_this_action_depends_on'] = action_ids
                atomic_json_write(file_path, data, indent=4)
            else:
                file_path = helper_fun.safe_prompt_path(prompt_id, flow, num)
                with open(file_path, 'r') as f:
                    data = json.load(f)
                data['actions_this_action_depends_on'] = []
                atomic_json_write(file_path, data, indent=4)
        except (ValueError, SyntaxError) as e:
            current_app.logger.info(f'GOT ERROR AT EVAL OF LIST :{e}')
            file_path = helper_fun.safe_prompt_path(prompt_id, flow, num)
            with open(file_path, 'r') as f:
                data = json.load(f)
            data['actions_this_action_depends_on'] = []
            atomic_json_write(file_path, data, indent=4)
            continue
    individual_recipe = []
    set_individual_recipes(flow, individual_recipe, prompt_id, user_prompt)
    # TOPOLOGICAL SORT & CHECK FOR CYCLIC DEPENDENCY
    status, updated_actions, cyc = topological_sort(individual_recipe)
    if not status:
        fix_cyclic_dependency(cyc, individual_recipe)
        status, updated_actions, cyc = topological_sort(individual_recipe)
    group_chat.messages[-1]['content'] = f'{updated_actions}'
    data = get_prompt_config_json(prompt_id)
    role = data['flows'][get_current_flow(user_prompt)]['persona']
    message = begin_agent_convo_to_get_schedulers(assistant_agent, chat_instructor, manager, prompt_id, updated_actions,
                                                  user_prompt)
    last_message = group_chat.messages[-1]
    current_app.logger.info(f'HI I AM HERE AFTER FINAL SCHEDULED JSON NOW I WILL next actions')
    current_app.logger.info(
        f'Current Flow -> recipe_for_persona[user_prompt]:{get_current_flow(user_prompt)} total_persona_actions[user_prompt]:{get_total_flows(user_prompt)}')
    return flow, message, text


def after_all_actions_terminated_from_exception(assistant_agent, chat_instructor, flow, group_chat, manager, prompt_id, user_prompt):
    flow = get_current_flow(user_prompt)
    individual_recipe = []
    set_individual_recipes(flow, individual_recipe, prompt_id, user_prompt)
    group_chat.messages[-1]['content'] = f'{individual_recipe}'
    assistant_agent.update_system_message = 'Check if the current_action depends on any other action, regardless of order it can be before or after this action. If yes, return the list of action IDs that this action depends on to ChatInstructor (e.g., [1,2]). Otherwise, return an empty array []. \nIMPORTANT: Respond strictly in an array [] format.'
    for num, action in enumerate(user_tasks[user_prompt].actions, 1):
        message = f'''Check if the current_action depends on any other action, regardless of order it can be before or after this action. If yes, return the list of action IDs that this action depends on to ChatInstructor (e.g., [1,2]). Otherwise, return an empty array []. \nIMPORTANT: Respond strictly in an array [] format.\n current_action: {action}'''
        result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False, silent=False)
        match = None
        for i in range(1, 4):
            text = group_chat.messages[-i]['content']
            match = re.search(r'\[.*?\]', text)
            if match:
                break
        if match:
            try:
                action_ids = ast.literal_eval(match.group())
            except (ValueError, SyntaxError):
                action_ids = []
            file_path = helper_fun.safe_prompt_path(prompt_id, flow, num)
            with open(file_path, 'r') as f:
                data = json.load(f)
            data['actions_this_action_depends_on'] = action_ids
            atomic_json_write(file_path, data, indent=4)
        else:
            file_path = helper_fun.safe_prompt_path(prompt_id, flow, num)
            with open(file_path, 'r') as f:
                data = json.load(f)
            data['actions_this_action_depends_on'] = []
            atomic_json_write(file_path, data, indent=4)
    individual_recipe = []
    set_individual_recipes(flow, individual_recipe, prompt_id, user_prompt)
    status, updated_actions, cyc = topological_sort(individual_recipe)
    if not status:
        fix_cyclic_dependency(cyc, individual_recipe)
        status, updated_actions, cyc = topological_sort(individual_recipe)
    group_chat.messages[-1]['content'] = f'{updated_actions}'
    begin_agent_convo_to_get_schedulers_not_last(assistant_agent, chat_instructor, manager, prompt_id, updated_actions,
                                                 user_prompt)
    last_message = group_chat.messages[-1]
    json_response = retrieve_json(last_message['content'])
    return flow, json_response


def set_fallback_flags_and_request_recipe(chat_instructor, current_action_id, manager,  prompt_id, role, user_prompt):
    current_app.logger.info('User provided fallback, now requesting recipe')
    # The user's fallback response should be stored, not parsed as JSON
    # Now request recipe for this action
    user_tasks[user_prompt].recipe = True
    user_tasks[user_prompt].fallback = False
    # Request recipe for the completed action
    message = request_recipe_for_action(current_action_id, prompt_id, role, user_prompt)
    result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,
                                           silent=False)
    return message


def publish_to_crossbar_new_action_start(message, user_id):
    # `message` reaches a USER-VISIBLE bubble (ChatMessageList thinkingSteps),
    # so it must read as progress, not as machinery.
    #
    # The old text appended ".\n please evaluate the response i am giving to
    # check if it meets the current action" to EVERY bubble — including the
    # clean ones from _push_thinking.  That sentence is an instruction aimed at
    # the model, and users were reading the system talk to itself (task #649).
    # Nothing consumes it: the React and Android handlers render this field as
    # text, and tests/unit/test_crossbar_publish_thinking.py pins the ENVELOPE
    # (helper vs the historical inline literal), not this wording.
    # Deliberately NOT str(message): concatenation raises on a non-str, and
    # that is the desired failure.  #649 names "raw dict repr" as a symptom —
    # coercing here would RENDER a dict to the user instead of failing loudly.
    text = "Working on " + message
    # Pull real request_id from threadlocal — see publish_agent_thought
    # for the full failure-mode analysis (drain key miss, React daemon
    # filter, Android orphan bucket).  Single source via thread_local_data.
    from threadlocal import thread_local_data
    from core.peer_link.crossbar_publish import publish_thinking_trace
    publish_thinking_trace(
        text=text, user_id=user_id,
        request_id=thread_local_data.get_request_id() or '',
        bot_type='Agent',
        full_schema=True,
    )


# Use lifecycle-aware increment:
def safe_increment_action(user_prompt):
    current_action_id = user_tasks[user_prompt].current_action
    # Ensure current action is terminal (TERMINATED or a give-up GAVE_UP) before moving to next
    if get_action_state(user_prompt, current_action_id) not in (ActionState.TERMINATED, ActionState.GAVE_UP):
        raise StateTransitionError(f"Action {current_action_id} must be terminal before incrementing")

    user_tasks[user_prompt].current_action += 1
    safe_set_state(user_prompt, user_tasks[user_prompt].current_action, ActionState.ASSIGNED, "action incremented")

def get_execute_next_action_message( prompt_id, user_prompt):
    safe_increment_action(user_prompt)
    message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action - 1)
    task_time[prompt_id]['timer'] = time.time()
    message = f'Execute Action {user_tasks[user_prompt].current_action}: {message} '
    return message


def begin_agent_convo_to_get_schedulers_not_last(assistant_agent, chat_instructor,  manager, prompt_id,  updated_actions, user_prompt):

    final_recipe[prompt_id] = {"status": "completed", "actions": updated_actions}
    assistant_agent.update_system_message = '''Reflect on the sequence of tasks and create scheduled_tasks with proper persona name and action_entry_point. Provide the output in the following JSON format:
                        { "status": "completed","dependency":[{"action_id":"action id in integer here e.g. 1,2","actions_this_action_depends_on":[e.g. 1,2,3]}], "recipe": "you should keep it blank.", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job is present; if no time-based job exists, do not create it.","persona":"", "action_entry_point":"An integer `action_id` from the list of existing `action_ids` is required as the starting point to perform this job.","action_exit_point":"An integer `action_id` up to which the job should be performed to complete the task. It can be greater than or equal to the entry point.","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ], "visual_scheduled_tasks": [ { "cron_expression": "Create this only if a visual time-based job is present; if no visual time-based job exists, do not create it.","persona":"", "job_description": "Provide a description of the visual scheduled job without specifying the time or frequency" } ] }'''
    message = '''Reflect on the sequence of tasks and create scheduled_tasks with proper persona name and action_entry_point. Provide the output in the following JSON format:
                        { "status": "completed","dependency":[{"action_id":"action id in integer here e.g. 1,2","actions_this_action_depends_on":[e.g. 1,2,3]}], "recipe": "you should keep it blank.", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job is present; if no time-based job exists, do not create it.","persona":"", "action_entry_point":"An integer `action_id` from the list of existing `action_ids` is required as the starting point to perform this job.","action_exit_point":"An integer `action_id` up to which the job should be performed to complete the task. It can be greater than or equal to the entry point.","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ], "visual_scheduled_tasks": [ { "cron_expression": "Create this only if a visual time-based job is present; if no visual time-based job exists, do not create it.","persona":"", "job_description": "Provide a description of the visual scheduled job without specifying the time or frequency" } ] }'''
    chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False, silent=False)


def request_fallback_for_action(current_action_id,  user_prompt):
    user_tasks[user_prompt].recipe = True
    user_tasks[user_prompt].fallback = False
    safe_set_state(user_prompt, current_action_id, ActionState.FALLBACK_REQUESTED, "FALLBACK_REQUESTED START")
    message = f"@Assistant: To Get Action {current_action_id} fallback: Ask USER what actions should be taken if current actions fail in the future after you get the response from user give the conversation to StatusVerifier agent"
    return message


def begin_agent_convo_to_get_schedulers(assistant_agent, chat_instructor, manager,  prompt_id, updated_actions, user_prompt):
    message = '''Reflect on the sequence of tasks and create scheduled_tasks with proper persona name and action_entry_point. Provide the output in the following JSON format:
                            { "status": "completed","dependency":[{"action_id":"action id in integer here e.g. 1,2","actions_this_action_depends_on":[e.g. 1,2,3]}], "recipe": "you should keep it blank.", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job is present; if no time-based job exists, do not create it.","persona":"", "action_entry_point":"An integer `action_id` from the list of existing `action_ids` is required as the starting point to perform this job.","action_exit_point":"An integer `action_id` up to which the job should be performed to complete the task. It can be greater than or equal to the entry point.","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ], "visual_scheduled_tasks": [ { "cron_expression": "Create this only if a visual time-based job is present; if no visual time-based job exists, do not create it.","persona":"", "job_description": "Provide a description of the visual scheduled job without specifying the time or frequency" } ] }'''
    final_recipe[prompt_id] = {"status": "completed", "actions": updated_actions}
    assistant_agent.update_system_message = '''Reflect on the sequence of tasks and create scheduled_tasks with proper persona name and action_entry_point. Provide the output in the following JSON format:
                        { "status": "completed","dependency":[{"action_id":"action id in integer here e.g. 1,2","actions_this_action_depends_on":[e.g. 1,2,3]}], "recipe": "you should keep it blank.", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job is present; if no time-based job exists, do not create it.","persona":"", "action_entry_point":"An integer `action_id` from the list of existing `action_ids` is required as the starting point to perform this job.","action_exit_point":"An integer `action_id` up to which the job should be performed to complete the task. It can be greater than or equal to the entry point.","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ], "visual_scheduled_tasks": [ { "cron_expression": "Create this only if a visual time-based job is present; if no visual time-based job exists, do not create it.","persona":"", "job_description": "Provide a description of the visual scheduled job without specifying the time or frequency" } ] }'''
    current_app.logger.info(
        f'user_tasks[user_prompt].current_action:{user_tasks[user_prompt].current_action} == len(user_tasks[user_prompt].actions)')
    chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False, silent=False)
    return message


def request_fallback_for_action_last(current_action_id, user_prompt):
    user_tasks[user_prompt].recipe = True
    user_tasks[user_prompt].fallback = False
    force_state_through_valid_path(user_prompt, current_action_id, ActionState.FALLBACK_REQUESTED,
                                   "fallback start")
    safe_set_state(user_prompt, current_action_id, ActionState.FALLBACK_REQUESTED, "Transition FALLBACK_REQUESTED")
    message = f"@Assistant: To Get Action {user_tasks[user_prompt].current_action} fallback: Ask USER what actions should be taken if current actions fail in the future after you get the response from user give the conversation to StatusVerifier agent"
    return message


def request_recipe_for_action_last(current_action_id, prompt_id, role, user_prompt):
    user_tasks[user_prompt].recipe = False
    user_tasks[user_prompt].fallback = False
    metadata = strip_json_values(agent_data[prompt_id])
    safe_set_state(user_prompt, current_action_id, ActionState.RECIPE_REQUESTED, "recipe start")
    message = RECIPE_CREATE_PROMPT_PREFIX + ''' that includes only the necessary steps for this action from history, along with a suitable name. Provide the output in the following JSON format:
                        { "status": "done", "action": "''' + str(user_tasks[user_prompt].get_action(user_tasks[
                                                                                                                      user_prompt].current_action - 1)) + '''","fallback_action":"", "persona":"","action_id": ''' + f'{user_tasks[user_prompt].current_action}' + ''', "recipe": [{{"steps":"steps here","tool_name":"Only include tool name here if used for this step.","generalized_functions": "Only include this field if any Python code is created, otherwise omit it entirely."}}],"can_perform_without_user_input":"can you perform this action on your own without user input in future. only say no when it is absolutely mandatory and you cannot proceed without it, if you can proceed by checking with other agents you should say yes.  say yes/no if no they give the reason as well e.g. no-i need user's likes and dislike", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job is present; if no time-based job exists, do not create it.","persona":"", "action_entry_point":"An integer action_id is required as an entrypoint from list of existing action_ids to perform this job","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ] }
                        Recipe Requirements:
                        1. Generalized Python Functions: Give the code which was created and executed successfully without any error handling edge cases. leave it blank when there is no code nedded to perform the action
                        2. Avoid directly storing any specific information provided by the author in the recipe. Use placeholders for variables instead.
                        3. Ensure that coding and non-coding steps are not combined within the same function.
                        4. For all Python functions, include comprehensive docstrings to explain their purpose, parameters, and usage. This should especially clarify non-coding steps that require utilizing the assistant's language capabilities.
                        5. If any internal tool is used to complete a step, provide detailed instructions on how to call or utilize that tool instead of providing the code for that step.
                        ''' + f'6. The persona must be one of the following: {role}. No other personas are allowed.'
    return message


def request_recipe_for_action(current_action_id, prompt_id, role, user_prompt, parse_failures=0):
    user_tasks[user_prompt].recipe = False
    user_tasks[user_prompt].fallback = False
    safe_set_state(user_prompt, current_action_id, ActionState.RECIPE_REQUESTED, "recipe start")
    metadata = strip_json_values(agent_data[prompt_id])
    message = RECIPE_CREATE_PROMPT_PREFIX + ''' that includes only the necessary steps for this action, along with a suitable name. Provide the output in the following JSON format:
                        { "status": "done", "action": "Describe the action performed here","fallback_action":"", "persona":"","action_id": ''' + f'{user_tasks[user_prompt].current_action}' + ''', "recipe": [{{"steps":"steps here","tool_name":"Only include tool name here if used for this step.","generalized_functions": "Only include this field if any Python code is created, otherwise omit it entirely."}}],"can_perform_without_user_input":"can you perform this action on your own without user input in future. only say no when it is absolutely mandatory and you cannot proceed without it, if you can proceed by checking with other agents you should say yes.  say yes/no if no they give the reason as well e.g. no-i need user's likes and dislike", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job is present; if no time-based job exists, do not create it.","persona":"", "action_entry_point":"An integer action_id is required as an entrypoint from list of existing action_ids to perform this job","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ] }
                        Recipe Requirements:
                        1. Generalized Python Functions: Give the code which was created and excuted successfully without any error handling edge cases. leave it blank when there is no code nedded to perform the action
                        2. Avoid directly storing any specific information provided by the author in the recipe. Use placeholders for variables instead.
                        3. Ensure that coding and non-coding steps are not combined within the same function.
                        4. For all Python functions, include comprehensive docstrings to explain their purpose, parameters, and usage. This should especially clarify non-coding steps that require utilizing the assistant's language capabilities.
                        5. If any internal tool is used to complete a step, provide detailed instructions on how to call or utilize that tool instead of providing the code for that step.
                        ''' + f'6. Metadata created till this action: {metadata}\n7. The persona must be one of the following: {role}. No other personas are allowed.'
    # #89: after the model's prior recipe response failed to parse, escalate a
    # "emit ONLY valid JSON" correction instead of re-sending the identical
    # prompt (which just gets the same prose-wrapped garbage).
    message += recipe_correction_directive(parse_failures)
    return message

def fix_cyclic_dependency(cyc, individual_recipe):
    res = fix_actions(individual_recipe, cyc)
    # fix_actions returns None when the local llama-server is down or its
    # response can't be parsed. Leave the recipe's dependencies unmodified
    # rather than crashing the flow with `TypeError: 'NoneType' is not
    # iterable` on the offline path.
    if not res:
        return
    for i in res:
        for j in individual_recipe:
            if i['action_id'] == j['action_id']:
                j['actions_this_action_depends_on'] = i['actions_this_action_depends_on']
                break


# The recipe-request prompt (see ~line 5050) hands the 4B an EXAMPLE JSON whose
# fields are literal placeholders. Small models sometimes echo those strings
# verbatim instead of filling them in, banking a junk "recipe" that then stalls
# every REUSE replay forever (live: goal 908f4987 banked
# action="Describe the action performed here"). Reject ONLY exact template
# echoes — real recipes never contain these strings, so false-positive risk is
# nil. Keep this set in sync with the example JSON in request_recipe_for_action.
_RECIPE_PLACEHOLDER_STRINGS = frozenset({
    'describe the action performed here',
    'steps here',
    'only include tool name here if used for this step.',
    'action here',
})


def _recipe_is_placebo(json_obj) -> bool:
    """True if the model echoed the recipe-template placeholders (junk recipe)."""
    try:
        action = (json_obj.get('action') or '').strip().lower()
        if action in _RECIPE_PLACEHOLDER_STRINGS:
            return True
        steps = json_obj.get('recipe') or []
        for s in steps:
            if not isinstance(s, dict):
                continue
            if (s.get('steps') or '').strip().lower() in _RECIPE_PLACEHOLDER_STRINGS:
                return True
            if (s.get('tool_name') or '').strip().lower() in _RECIPE_PLACEHOLDER_STRINGS:
                return True
        return False
    except Exception:
        return False


def _bank_action_recipe_from_trace(user_prompt, prompt_id, flow, action_id,
                                   group_chat):
    """Persist {pid}_{flow}_{action}.json derived from the tool calls that
    ACTUALLY executed for this action in the live group chat.

    The 4B frequently completes an action (state_transition / #128 recovery
    edges) without emitting a parseable recipe payload; the old path then
    re-asked the model for the recipe — another fallible LLM round-trip.
    Result: flows walked deep but banked nothing (goal 60834540771: ONE
    action recipe in 3 weeks), so every restart re-walked from Action 1 and
    no flow ever reached the completion charge.

    Trace-derived banking records what really ran — only the executed tool
    calls, never fabricated steps. An action with NO tool work banks an
    explicit no-op marker (the 2026-06-04 "synthesis poisons validator"
    guard). Must only be called IN-RUN: the trace lives in this dispatch's
    group_chat and is gone after a restart. Returns True if banked.
    """
    try:
        msgs = list(getattr(group_chat, 'messages', []) or [])
        # The action's window: everything after the LAST "Execute Action N"
        # message (re-dispatches of the same action overwrite the window).
        start = 0
        for i, m in enumerate(msgs):
            c = m.get('content') if isinstance(m, dict) else None
            # Trailing ':' delimiter is required — dispatch markers are
            # 'Execute Action N: ...', so without the colon 'Execute Action 2'
            # also matches 'Execute Action 20:'..'29:' and banks the wrong
            # action's tool calls for flows with >=10 actions (CREATE routinely
            # decomposes into 11-23).
            if isinstance(c, str) and f'Execute Action {action_id}:' in c:
                start = i
        # Window ENDS at the next action's dispatch so a later action's tool
        # calls don't bleed into this one (the trace can hold later dispatches
        # when banking runs at/after a flow boundary). start is THIS action's
        # last dispatch, so the next 'Execute Action ' marker is a different one.
        end = len(msgs)
        for j in range(start + 1, len(msgs)):
            cj = msgs[j].get('content') if isinstance(msgs[j], dict) else None
            if isinstance(cj, str) and 'Execute Action ' in cj:
                end = j
                break
        steps = []
        for m in msgs[start:end]:
            if not isinstance(m, dict):
                continue
            for tc in (m.get('tool_calls') or []):
                fn = (tc.get('function') or {}) if isinstance(tc, dict) else {}
                nm = fn.get('name', '')
                if not nm:
                    continue
                steps.append({
                    'steps': f"{nm}({str(fn.get('arguments') or '')[:400]})",
                    'tool_name': nm,
                    'generalized_functions': '',
                    'agent_to_perform_this_action': 'Helper',
                })
        action_obj = {}
        try:
            action_obj = user_tasks[user_prompt].get_action(action_id - 1) or {}
        except Exception:
            pass
        if not steps:
            steps = [{
                'steps': 'no-op: action completed without tool execution',
                'tool_name': '',
                'generalized_functions': '',
                'agent_to_perform_this_action': 'Assistant',
            }]
        json_obj = {
            'status': 'done',
            'action': action_obj.get('action', ''),
            'fallback_action': action_obj.get('fallback_action', ''),
            'persona': 'Executor',
            'action_id': int(action_id),
            'recipe': steps,
            'can_perform_without_user_input': 'yes',
            'scheduled_tasks': [],
            'metadata': {},
            'recipe_source': 'execution_trace',
        }
        # Same secret-redaction guard as the model-recipe save path.
        try:
            from security.secret_redactor import redact_secrets
            for _ri in json_obj['recipe']:
                if isinstance(_ri.get('steps'), str):
                    _ri['steps'], _ = redact_secrets(_ri['steps'])
        except ImportError:
            pass
        name = helper_fun.safe_prompt_path(prompt_id, flow, action_id)
        atomic_json_write(name, json_obj)
        current_app.logger.info(
            f"[TRACE-BANKED] action {action_id} recipe derived from "
            f"{len(steps)} executed step(s) -> {name}")
        return True
    except Exception as e:
        try:
            current_app.logger.warning(
                f"[TRACE-BANK] failed for action {action_id}: {e}")
        except Exception:
            pass
        return False


def _announce_flow_recipe(prompt_id, flow):
    """Announce a freshly banked flow recipe to the hive capability mesh.

    PROACTIVE half of the recipe capability mesh: gossip-broadcast a
    'recipe_available' advert so admitted peers can pull the bytes
    directly (skipping the O(peers) discovery sweep) instead of only
    finding it reactively per-goal. GATED by peer_reuse.export_allowed so
    a PRIVATE recipe is NEVER advertised. Fully best-effort: a broadcast
    failure must NEVER fail the bank (wrap, log, continue). Returns True
    only when an advert was actually broadcast.
    """
    try:
        from integrations.google_a2a.peer_reuse import (
            export_allowed, announce_recipe_available)
        if not export_allowed(prompt_id):
            return False
        return bool(announce_recipe_available(prompt_id, flow))
    except Exception as e:
        try:
            current_app.logger.info(
                f'recipe advert skipped for {prompt_id}_{flow}: {e}')
        except Exception:
            pass
        return False


def _save_flow_recipe(flow, prompt_id, user_prompt, user_id, group_chat):
    """Save the aggregated flow recipe to disk.

    Called after after_all_actions_terminated() returns. Collects individual
    action recipes (topologically sorted with dependencies), merges scheduler
    data from the group_chat, and writes {prompt_id}_{flow}_recipe.json.

    Single function used by BOTH flow-completion code paths to prevent divergence.
    """
    _flow_recipe_data = final_recipe.get(prompt_id, {})
    if not _flow_recipe_data:
        _flow_recipes = []
        set_individual_recipes(flow, _flow_recipes, prompt_id, user_prompt)
        _flow_recipe_data = {'status': 'completed', 'actions': _flow_recipes}
    # Merge scheduler response from group_chat if available
    try:
        if group_chat and group_chat.messages:
            _sched_json = retrieve_json(group_chat.messages[-1]['content'])
            if _sched_json and isinstance(_sched_json, dict):
                if 'scheduled_tasks' in _sched_json:
                    _flow_recipe_data['scheduled_tasks'] = _sched_json['scheduled_tasks']
                if 'visual_scheduled_tasks' in _sched_json:
                    _flow_recipe_data['visual_scheduled_tasks'] = _sched_json['visual_scheduled_tasks']
    except Exception:
        pass
    create_final_recipe_for_current_flow(flow, _flow_recipe_data, prompt_id)
    current_app.logger.info(f'[FLOW-RECIPE-SAVED] {prompt_id}_{flow}_recipe.json')
    _push_thinking(user_id, f'Flow {flow} recipe saved.')
    # Meter the COMPLETED work into the owning goal's spark ledger — this is
    # the signal the daemon's completion gate closes goals on. Charged here
    # (flow genuinely finished) and never at dispatch; see
    # budget_gate.charge_goal_work_completed.
    try:
        from integrations.agent_engine.budget_gate import charge_goal_work_completed
        charge_goal_work_completed(
            prompt_id, len(_flow_recipe_data.get('actions') or []) or 1)
    except Exception as _spark_err:
        current_app.logger.debug(f'completed-work spark charge skipped: {_spark_err}')
    # PROACTIVE capability mesh: recipe is durably banked above, so
    # gossip-announce it to admitted peers (gated by export_allowed,
    # best-effort, never fails the bank). Reactive per-goal pull stays
    # the floor.
    _announce_flow_recipe(prompt_id, flow)


def set_individual_recipes(flow, individual_recipe, prompt_id, user_prompt):
    # Use len(actions) not current_action — after safe_increment_action(),
    # current_action points past the last action (e.g. 5 for 4 actions),
    # causing FileNotFoundError on the non-existent _0_5.json.
    action_count = len(user_tasks[user_prompt].actions)
    for i in range(1, action_count + 1):
        _recipe_path = helper_fun.safe_prompt_path(prompt_id, flow, i)
        current_app.logger.info(f'checking for {_recipe_path}')
        try:
            with open(_recipe_path, 'r') as f:
                config = json.load(f)
                individual_recipe.append(config)
        except FileNotFoundError:
            current_app.logger.error(f'Action recipe MISSING: {_recipe_path} — flow recipe will be incomplete')
        except Exception as e:
            current_app.logger.error(f'Error loading {_recipe_path}: {e}')


def load_persona_role(prompt_id, user_prompt):
    try:
        data = get_prompt_config_json(prompt_id)
        role = data['flows'][get_current_flow(user_prompt)]['persona']
        current_app.logger.info(f"Loaded role={role} from config")
    except Exception as e:
        current_app.logger.error(f"Error loading role info: {e}")
        role = "unknown"
    return role


def track_lifecycle_hooks(current_action_id, group_chat, user_prompt):
    debug_lifecycle_status(user_prompt)
    # Lifecycle TRACKING HOOKS:
    lifecycle_hook_track_action_assignment(user_prompt, user_tasks, group_chat)  # 1. Track action assignment
    lifecycle_hook_track_status_verification_request(user_prompt, user_tasks,
                                                     group_chat)  # 3. Track status verification request
    lifecycle_hook_track_fallback_request(user_prompt, user_tasks, group_chat)  # 7. Track fallback request
    lifecycle_hook_track_user_fallback(user_prompt, user_tasks, group_chat)  # 8. Track user fallback
    lifecycle_hook_track_recipe_request(user_prompt, user_tasks, group_chat)  # 9. Track recipe request
    lifecycle_hook_track_termination(user_prompt, user_tasks, group_chat)  # 11. Track termination
    return current_action_id

messages = TTLCache(ttl_seconds=7200, max_size=500, name='create_messages')
recent_file_id = TTLCache(ttl_seconds=7200, max_size=500, name='create_recent_file_id')
request_id_list = TTLCache(ttl_seconds=7200, max_size=500, name='create_request_id_list')
recipe_for_persona = TTLCache(
    ttl_seconds=7200, max_size=500,
    name='create_recipe_for_persona',
    # Restore the active flow from the latest non-terminal ledger
    # session on cache miss — without this, Nunba restart drops a
    # mid-execution agent back to flow 0 and re-runs completed flows.
    # See TASK_LEDGER_PERSISTENCE_PLAN.md §3 Phase 3.
    loader=load_current_flow,
)
total_persona_actions = TTLCache(ttl_seconds=7200, max_size=500, name='create_total_persona_actions')


# FIX: Resume Logic Issues - Replace detect_and_resume_progress function

def detect_and_resume_progress(prompt_id, user_prompt):
    """
    Fixed version: Detect existing progress and resume from the correct point
    Returns: (current_flow, current_action, completed_flows)
    """
    import os
    import json

    config = get_prompt_config_json(prompt_id)
    # A freshly-created or error-state config may have no 'flows' yet — treat
    # that as zero flows (start fresh) instead of KeyError-crashing the whole
    # /chat request into "Sorry, I encountered an error".
    total_flows = len((config or {}).get('flows', []))

    # Track progress across all flows
    flow_progress = {}
    completed_flows = []
    latest_flow = 0
    latest_action = 1

    current_app.logger.info(f"[SCAN] Scanning for existing progress for prompt_id={prompt_id}")

    # Scan each flow for existing files
    for flow_idx in range(total_flows):
        flow_actions = config['flows'][flow_idx]['actions']
        total_actions_in_flow = len(flow_actions)

        # Check for flow recipe (indicates flow completion)
        flow_recipe_file = helper_fun.safe_prompt_path(prompt_id, flow_idx, 'recipe')
        flow_recipe_exists = os.path.exists(flow_recipe_file)

        # Count completed actions in this flow (actions with JSON files)
        completed_actions = []
        for action_id in range(1, total_actions_in_flow + 1):
            action_file = helper_fun.safe_prompt_path(prompt_id, flow_idx, action_id)
            if os.path.exists(action_file):
                completed_actions.append(action_id)
                current_app.logger.info(f"[OK] Found: {action_file}")

        # [OK] FIX: Flow is complete ONLY if ALL actions have JSON files AND recipe exists
        flow_complete = (len(completed_actions) == total_actions_in_flow) and flow_recipe_exists

        flow_progress[flow_idx] = {
            'total_actions': total_actions_in_flow,
            'completed_actions': completed_actions,
            'flow_complete': flow_complete,
            'last_completed_action': max(completed_actions) if completed_actions else 0
        }

        # [OK] FIX: Update latest flow and action based on actual completion
        if completed_actions:
            latest_flow = flow_idx
            if flow_complete:
                completed_flows.append(flow_idx)
                # If this flow is complete, check if there's a next flow
                if flow_idx + 1 < total_flows:
                    latest_flow = flow_idx + 1
                    latest_action = 1  # Start next flow
                else:
                    latest_action = total_actions_in_flow + 1  # Beyond last action
            else:
                # [OK] FIX: Next action should be last_completed + 1, not some random number
                latest_action = max(completed_actions) + 1
                # Ensure we don't exceed flow actions
                if latest_action > total_actions_in_flow:
                    latest_action = total_actions_in_flow + 1

    current_app.logger.info(f"[PROGRESS] Progress Analysis:")
    current_app.logger.info(f"   - Latest Flow: {latest_flow}")
    current_app.logger.info(f"   - Latest Action: {latest_action}")
    current_app.logger.info(f"   - Completed Flows: {completed_flows}")
    current_app.logger.info(f"   - Flow Progress: {flow_progress}")

    return latest_flow, latest_action, flow_progress, completed_flows


# FIX: State setting for resume - Replace set_states_from_progress function

def set_states_from_progress(user_prompt, prompt_id, current_flow, flow_progress):
    """
    Fixed version: Set appropriate states based on detected progress using valid transitions
    """
    config = get_prompt_config_json(prompt_id)

    for flow_idx, progress in flow_progress.items():
        if flow_idx < current_flow:
            # Previous flows - all actions should be TERMINATED
            for action_id in range(1, progress['total_actions'] + 1):
                # [OK] FIX: Use force_state_through_valid_path to handle transitions properly
                force_state_through_valid_path(user_prompt, action_id, ActionState.TERMINATED,
                                               "resumed - previous flow")

        elif flow_idx == current_flow:
            # Current flow - set states based on completion
            for action_id in range(1, progress['total_actions'] + 1):
                if action_id in progress['completed_actions']:
                    # [OK] FIX: Action has JSON file - use proper state path to TERMINATED
                    force_state_through_valid_path(user_prompt, action_id, ActionState.TERMINATED,
                                                   "resumed - action complete")
                else:
                    # Action not yet complete - mark as ASSIGNED
                    safe_set_state(user_prompt, action_id, ActionState.ASSIGNED, "resumed - pending action")

        else:
            # Future flows - all actions ASSIGNED but not started yet
            for action_id in range(1, progress['total_actions'] + 1):
                safe_set_state(user_prompt, action_id, ActionState.ASSIGNED, "resumed - future flow")


# FIX: Enhanced boundary check before while loop - Add this in get_response_group()

_BUILD_INCOMPLETE_REPLY = (
    "I couldn't finish building that agent — its steps didn't complete, so it "
    "wouldn't be usable yet. Tell me a bit more about what it should do and "
    "I'll pick up where it stopped."
)


def _agent_build_is_complete(prompt_id) -> bool:
    """True iff the agent is actually reusable.

    The reuse gate (hart_intelligence_entry.py:9250) asks exactly one question:
    does ``{prompt_id}_0_recipe.json`` exist?  Completion must answer the SAME
    question or the two disagree forever — which is precisely what happened:
    measured 2026-08-29, 531 of 612 status='completed' agents (86.8%) have no
    flow-0 recipe, so every turn against them re-enters creation.

    Deliberately mirrors the gate's own predicate rather than inventing a
    second notion of "done".
    """
    if not prompt_id:
        return False
    try:
        return os.path.exists(
            os.path.join(PROMPTS_DIR, f'{prompt_id}_0_recipe.json'))
    except (TypeError, ValueError, OSError):
        return False


def safe_action_boundary_check(user_prompt, prompt_id, text, user_id):
    """
    Enhanced boundary check with proper flow transition logic
    Returns: (should_continue, response_or_none)
    """
    current_action_id = user_tasks[user_prompt].current_action
    config = get_prompt_config_json(prompt_id)
    current_flow = get_current_flow(user_prompt)
    total_flows = len(config['flows'])

    # Check if current flow exists
    if current_flow >= total_flows:
        current_app.logger.info(f"All flows ({total_flows}) completed")
        # This is a BOUNDARY CHECK, not the completion path — it reached this
        # branch on a bare index comparison, having verified nothing.  Only the
        # flow-recipe writer makes an agent reusable, so ask for its artifact
        # before telling the user the agent exists.
        if not _agent_build_is_complete(prompt_id):
            current_app.logger.error(
                "[BUILD-INCOMPLETE] flow index %s >= total %s but no "
                "%s_0_recipe.json — the agent is NOT reusable; refusing to "
                "report it as created (see #718)",
                current_flow, total_flows, prompt_id)
            return False, _BUILD_INCOMPLETE_REPLY
        return False, 'Agent Created Successfully'

    current_flow_actions = get_total_actions_length_for_flow(config, current_flow)

    # [OK] FIX: Handle action exceeding current flow
    if current_action_id > current_flow_actions:
        current_app.logger.info(
            f"Action {current_action_id} exceeds flow {current_flow} actions ({current_flow_actions})")

        # Check if current flow is actually complete (all actions have JSON files)
        all_actions_complete = True
        for action_id in range(1, current_flow_actions + 1):
            action_file = helper_fun.safe_prompt_path(prompt_id, current_flow, action_id)
            if not os.path.exists(action_file):
                all_actions_complete = False
                current_app.logger.warning(f"Action {action_id} not complete - missing {action_file}")
                break

        if not all_actions_complete:
            # [OK] FIX: Find the first incomplete action and resume from there
            for action_id in range(1, current_flow_actions + 1):
                action_file = helper_fun.safe_prompt_path(prompt_id, current_flow, action_id)
                if not os.path.exists(action_file):
                    current_app.logger.info(f"Resuming from incomplete action {action_id}")
                    user_tasks[user_prompt].current_action = action_id
                    return True, None  # Continue with normal execution

        # Flow is complete, try to move to next flow
        if current_flow + 1 < total_flows:
            current_app.logger.info(f"Moving to next flow: {current_flow} -> {current_flow + 1}")

            # Simple flow increment
            recipe_for_persona[user_prompt] += 1
            next_flow_actions = config['flows'][get_current_flow(user_prompt)]['actions']
            user_id = user_prompt.split('_')[0]
            user_tasks[user_prompt] = create_action_with_ledger(next_flow_actions, user_id, prompt_id, user_prompt)
            user_tasks[user_prompt].current_action = 1

            # Initialize states for new flow actions
            for action_id in range(1, len(next_flow_actions) + 1):
                safe_set_state(user_prompt, action_id, ActionState.ASSIGNED, "new flow started")

            # Delete old agents and create new ones
            if user_prompt in user_agents:
                del user_agents[user_prompt]
            return False, get_response_group(user_id, text, prompt_id)

        else:
            # All flows completed
            current_app.logger.info("All flows completed - agent creation ready")
            # Same rule as the branch above: "ready" is not "built".  Verified
            # live 2026-08-29 — this exact marker fired twice, in the same two
            # rotated logs that carry the success string, while every
            # recipe-writing marker fired zero times.
            if not _agent_build_is_complete(prompt_id):
                current_app.logger.error(
                    "[BUILD-INCOMPLETE] all flows reported complete but no "
                    "%s_0_recipe.json was written — the agent is NOT reusable; "
                    "refusing to report it as created (see #718)", prompt_id)
                return False, _BUILD_INCOMPLETE_REPLY
            return False, 'Agent Created Successfully'

    # Action is within bounds, continue normal execution
    return True, None


def get_total_actions_length_for_flow(config, current_flow):
    return len(config['flows'][current_flow]['actions'])


# ... rest of existing code


# Also replace the resume functions in initialize_with_resume():

def initialize_with_resume(prompt_id, user_prompt, user_id):
    """
    Fixed initialization that resumes from existing progress
    """
    config = get_prompt_config_json(prompt_id)

    # Use fixed detection
    current_flow, current_action, flow_progress, completed_flows = detect_and_resume_progress(prompt_id,
                                                                                                    user_prompt)

    # Set flow tracking
    recipe_for_persona[user_prompt] = current_flow
    total_persona_actions[user_prompt] = len(config['flows'])

    # [OK] FIX: Handle case where we're beyond current flow actions
    if current_flow < len(config['flows']):
        current_flow_actions = config['flows'][current_flow]['actions']
        # Use ledger-enabled Action creation for persistent task tracking
        user_tasks[user_prompt] = create_action_with_ledger(current_flow_actions, user_id, prompt_id, user_prompt)
        user_tasks[user_prompt].current_action = current_action
        current_app.logger.info(f'Initialized with Smart Ledger: {len(user_tasks[user_prompt].ledger.tasks)} tasks loaded')
    else:
        # All flows complete
        user_tasks[user_prompt] = Action([])  # Empty actions
        user_tasks[user_prompt].current_action = 1

    # Use fixed state setting
    set_states_from_progress(user_prompt, prompt_id, current_flow, flow_progress)

    # Initialize other tracking
    scheduler_check[user_prompt] = len(completed_flows) == len(config['flows'])
    agent_data[prompt_id] = {'user_id': user_id}

    # Load existing metadata
    load_existing_metadata(prompt_id, user_prompt, flow_progress)

    current_app.logger.info(f"[RESUME] RESUME SUMMARY:")
    current_app.logger.info(f"   - Resumed at Flow {current_flow}, Action {current_action}")
    current_app.logger.info(f"   - Scheduler Check: {scheduler_check.get(user_prompt)}")
    current_app.logger.info(f"   - Completed Flows: {len(completed_flows)}/{len(config['flows'])}")

    return current_flow, current_action, completed_flows


def load_existing_metadata(prompt_id, user_prompt, flow_progress):
    """
    Load metadata from existing action JSONs to restore agent_data state
    """
    try:
        # First, try to load from persistent storage
        if helper_fun.load_agent_data_from_file(prompt_id, agent_data):
            current_app.logger.info(f" Successfully loaded persistent agent data for prompt_id {prompt_id}")
            return
        # Look for the most recent action JSON with metadata
        for flow_idx, progress in flow_progress.items():
            for action_id in sorted(progress['completed_actions'], reverse=True):
                action_file = helper_fun.safe_prompt_path(prompt_id, flow_idx, action_id)
                try:
                    with open(action_file, 'r') as f:
                        action_data = json.load(f)
                        if 'metadata' in action_data and action_data['metadata']:
                            # Merge metadata into agent_data
                            if prompt_id not in agent_data:
                                agent_data[prompt_id] = {}
                            agent_data[prompt_id].update(action_data['metadata'])
                            current_app.logger.info(f" Loaded metadata from {action_file}")
                            # Save to persistent storage for future use
                            helper_fun.save_agent_data_to_file(prompt_id,agent_data)
                            return  # Load from most recent only
                except Exception as e:
                    current_app.logger.warning(f"⚠️ Could not load metadata from {action_file}: {e}")
                    continue
    except Exception as e:
        current_app.logger.error(f"❌ Error loading existing metadata: {e}")


from core.llm_outbound_logger import with_llm_context as _with_llm_context


@_with_llm_context('autogen.create')
def recipe(user_id, text, prompt_id, file_id, request_id):
    user_prompt = f'{user_id}_{prompt_id}'
    request_id_list[user_prompt] = request_id
    current_app.logger.info('--' * 100)

    # [OK] NEW: Initialize persistent storage for this prompt_id
    if prompt_id not in agent_data:
        current_app.logger.info(f"Initializing persistent storage for prompt_id {prompt_id}")

        helper_fun.load_agent_data_from_file(prompt_id,agent_data)

    if file_id:
        recent_file_id[user_id] = file_id

    if user_prompt not in user_tasks.keys():
        #  ENHANCED: Resume from existing progress instead of starting fresh
        current_flow, current_action, completed_flows = initialize_with_resume(prompt_id, user_prompt, user_id)

        # Check if all flows are already complete (SessionCache evicts on TTL/LRU →
        # use .get() to avoid KeyError after eviction; missing key == 'not complete')
        if scheduler_check.get(user_prompt):
            current_app.logger.info(" All flows already completed - Agent already created")
            return 'Agent Already Created Successfully'

        current_app.logger.info(f"[RESUMING] Resuming from Flow {current_flow}, Action {current_action}")
    else:
        current_app.logger.info(f"♻️ Using existing session for {user_prompt}")

    try:
        last_response = get_response_group(user_id, text, prompt_id)
    except Exception as e:
        current_app.logger.error(f"Error occurred in create Recipe: {str(e)}")
        error_message = traceback.format_exc()
        current_app.logger.error(f"Error occurred in create Recipe stack trace:\n{error_message}")
        last_response = get_response_group(user_id, text, prompt_id, True, e)

    # Rest of the function remains the same...
    if scheduler_check.get(user_prompt) is True:
        current_app.logger.info('WORKING on TIMER AGENTS')
        config = get_prompt_config_json(prompt_id)
        number_of_flows = len(config['flows'])
        flows = config['flows']

        merged_dict = create_time_agents_and_create_scheduled_jobs(flows, number_of_flows, prompt_id, user_id,
                                                                   user_prompt)
        flow = get_current_flow(user_prompt)
        create_final_recipe_for_current_flow(flow, merged_dict, prompt_id)
        update_agent_creation_to_db(prompt_id)
        current_app.logger.info('Completed from here')
        return 'Agent Created Successfully'

    try:
        json_response = retrieve_json(last_response)
        if 'status' in json_response.keys() and json_response['status'].lower() == 'completed':
            if 'recipe' in json_response.keys():
                update_agent_creation_to_db(prompt_id)
                current_app.logger.info('Completed from here3')
                return 'Agent Created Successfully'
            else:
                return json_response['message']
    except Exception:
        pass

    return last_response


def initialise_current_flow_to_zero(user_prompt):
    recipe_for_persona[user_prompt] = 0


def increment_current_flow(user_prompt, prompt_id):
    """Advance to next flow and create Action with ledger for the new flow's actions.

    Args:
        user_prompt: Cache key (user_id_prompt_id)
        prompt_id: Required — used to load the prompt config JSON
    """
    recipe_for_persona[user_prompt] += 1
    prompt_config = get_prompt_config_json(prompt_id)
    flow_idx = get_current_flow(user_prompt)
    if flow_idx < len(prompt_config['flows']):
        user_id = user_prompt.split('_')[0]
        user_tasks[user_prompt] = create_action_with_ledger(
            prompt_config['flows'][flow_idx]['actions'], user_id, prompt_id, user_prompt)
    else:
        user_tasks[user_prompt] = Action([])
    user_tasks[user_prompt].current_action = 1



def safe_increment_flow(user_prompt, prompt_id):
    current_flow = get_current_flow(user_prompt)

    # Ensure all actions in current flow are TERMINATED
    config = get_prompt_config_json(prompt_id)
    current_flow_actions = config['flows'][current_flow]['actions']

    for action_id in range(1, len(current_flow_actions) + 1):
        if get_action_state(user_prompt, action_id) not in (ActionState.TERMINATED, ActionState.GAVE_UP):
            raise StateTransitionError(f"Cannot increment flow: Action {action_id} not terminal")

    increment_current_flow(user_prompt, prompt_id)

    # Reset action states for new flow
    next_flow = get_current_flow(user_prompt)
    if next_flow < len(config['flows']):
        next_flow_actions = config['flows'][next_flow]['actions']
        for action_id in range(1, len(next_flow_actions) + 1):
            safe_set_state(user_prompt, action_id, ActionState.ASSIGNED, "new flow started")

def update_agent_creation_to_db(prompt_id):
    url = f'{database_url}/update_agent_prompt?prompt_id={prompt_id}'
    headers = {'Content-Type': 'application/json'}
    res = pooled_patch(url, headers=headers)
    # Push the full recipe bundle to cloud after creation completes.
    # This is the WRITE half of the cross-device sync introduced in
    # core/recipe_sync.py - without it the agent is local-only and
    # the user hits the silent-fallback bug when switching devices.
    # Best-effort: never raises, never blocks the user.
    try:
        from core.recipe_sync import push_recipe
        # user_id not in scope here; recipe_sync accepts '' as
        # creator-unknown.  The {prompt_id}.json file itself carries
        # creator_user_id so the cloud side can still attribute.
        push_recipe(PROMPTS_DIR, prompt_id, user_id='')
    except Exception as _push_err:
        current_app.logger.debug(
            f'recipe_sync push for prompt_id={prompt_id} failed: {_push_err}')


def create_final_recipe_for_current_flow(flow, merged_dict, prompt_id):
    name = helper_fun.safe_prompt_path(prompt_id, flow, 'recipe')
    # Atomic write (M3 in post-shipment review) via the canonical helper:
    # tmp + fsync + os.replace so concurrent prompts_backup.snapshot_prompts
    # can never capture a half-written recipe file, and a crash mid-write leaves
    # no stray .tmp behind (the old inline copy fsync'd nothing). os.replace is
    # atomic on the same filesystem on both Windows and POSIX.
    atomic_json_write(name, merged_dict)
    current_app.logger.info(f"create_final_recipe_for_current_flow Dictionary saved to {name}")



def get_current_flow(user_prompt):
    if user_prompt in recipe_for_persona:
        flow = recipe_for_persona[user_prompt]
        return flow
    else:
        initialise_current_flow_to_zero(user_prompt)
        return 0




def create_time_agents_and_create_scheduled_jobs(flows, number_of_flows, prompt_id, user_id, user_prompt):
    for i in range(number_of_flows):
        _recipe_file = helper_fun.safe_prompt_path(prompt_id, i, 'recipe')
        with open(_recipe_file, 'r') as f:
            merged_dict = json.load(f)
            final_recipe[prompt_id] = merged_dict
            current_app.logger.info(f'updating the final recipe with {_recipe_file}')
        current_app.logger.info(f'Working on flow {i} with persona {flows[i]["persona"]}')
        time_agents[user_prompt] = create_time_agents(user_id, prompt_id, flows[i]['persona'], '', flows[i]["actions"])
        if "scheduled_tasks" in merged_dict:
            for jobs in merged_dict['scheduled_tasks']:
                time_based_execution(jobs['job_description'], user_id, prompt_id, jobs['action_entry_point'],
                                     flows[i]["actions"])
    return merged_dict


def get_total_actions_for_current_flow_and_reset_actions(prompt_id, user_prompt):
    flow_idx = get_current_flow(user_prompt)
    config = get_prompt_config_json(prompt_id)
    user_id = user_prompt.split('_')[0]
    user_tasks[user_prompt] = create_action_with_ledger(
        config['flows'][flow_idx]['actions'], user_id, prompt_id, user_prompt)
    total_actions = get_total_actions_length_for_flow(config, flow_idx)
    return config, total_actions


def get_prompt_config_json(prompt_id):
    with open(helper_fun.safe_prompt_path(prompt_id), 'r') as f:
        config = json.load(f)
    return config


def acknowledgment(user_id,prompt_id,request_id):
    user_prompt = f'{user_id}_{prompt_id}'
    author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_prompt]
    group_chat.messages.append({'content':f'GOT MESSAGE ACKNOWLEDGEMENT FOR {request_id}','role':'user','name':'Helper'})
