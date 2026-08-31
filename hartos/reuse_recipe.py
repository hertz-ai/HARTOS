"""reuse_recipe.py"""
# PEP 563: stringize ALL annotations (incl. the module-level
# `user_agents: Dict[str, Tuple[autogen.AssistantAgent, ...]]` below) so
# they are never evaluated at import time.  Required for the lazy autogen
# proxy: without this, those variable annotations would touch
# autogen.AssistantAgent at module load and force the heavy import we are
# trying to defer.  MUST be the first statement after the docstring.
# Guard: cx_Freeze frozen builds close stdout/stderr.
import sys, os
from core.io_guard import silence_stdio, install_autogen_iostream; silence_stdio()
# #170 — autogen budget constants live in core.constants (single source
# of truth, was hardcoded as max_tokens=3500 in 3 sites here and 4 in
# create_recipe.py).  See AUTOGEN_MESSAGE_TOKEN_BUDGET comment for why
# the value is 2500 (was 3500) and how it relates to llama-server's
# 12288 n_ctx per-slot budget under concurrent slots.
from core.constants import (  # noqa: E402  (after io_guard, intentional)
    AUTOGEN_MESSAGE_TOKEN_BUDGET,
    AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
    AUTOGEN_HISTORY_LIMIT,
    DEFAULT_SINGLE_ROLE,
)

from enum import Enum
import random
# autogen is imported lazily — it drags google.api_core (~7.6s) + flaml +
# the contrib capabilities chain -> llmlingua -> torch (~4.2s) at import
# time, but every autogen.* use here is inside a function (the two
# module-level type annotations at L248-249 are stringized by the
# `from __future__ import annotations` above, so they don't evaluate
# autogen).  Deferring keeps autogen off the backend-boot import path.
# Same proxy + test as create_recipe.py — see tests/unit/test_lazy_autogen_import.py.
from core.optional_import import lazy_module
autogen = lazy_module("autogen", on_import=install_autogen_iostream)
import os
import pytz
from core.http_pool import pooled_get, pooled_post, pooled_request
from core.port_registry import get_port as _get_llm_port
from typing import Dict, Optional, Tuple, Any, List
import uuid
import time
import re
import asyncio
from datetime import datetime, timedelta
from typing import Annotated, Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import json
import ast
from collections import deque
import redis
import pickle
from PIL import Image


from flask import current_app
from hartos.helper import ToolMessageHandler, strip_json_values, get_time_based_history, retrieve_json, load_vlm_agent_files, _is_terminate_msg


def _normalize_flow_recipe(config):
    """Guarantee the ``{status, actions:[...]}`` flow-recipe shape the reuse
    engine expects.

    A PER-ACTION recipe (``{status, action, recipe, action_id, persona, …}``)
    is sometimes written into the FLOW-recipe filename
    (``{prompt_id}_{flow}_recipe.json``) — e.g. single-action flows where the
    done-handler persisted ``json_obj`` directly.  Reuse then did
    ``recipes[user_prompt]['actions']`` → ``KeyError: 'actions'`` ("Some ERROR
    IN REUSE RECIPE 'actions'", live in frozen_debug.log) and fell back to the
    expensive CREATE pipeline, so REUSE never engaged for those agents and the
    flywheel kept re-paying full CREATE cost every dispatch.

    Normalizing at LOAD wraps the lone per-action recipe as a one-element
    ``actions`` list (preserving flow-level scheduled_tasks), so recipes
    ALREADY on disk reuse correctly without a rewrite.  A correctly-shaped
    flow recipe passes through unchanged; an unknown shape gets an empty
    ``actions`` list so reuse degrades gracefully instead of crashing.
    """
    if not isinstance(config, dict):
        return config
    if isinstance(config.get('actions'), list):
        return config
    if 'action' in config or 'recipe' in config or 'action_id' in config:
        norm = {'status': config.get('status', 'completed'), 'actions': [config]}
        for _k in ('scheduled_tasks', 'visual_scheduled_tasks'):
            if _k in config:
                norm[_k] = config[_k]
        return norm
    out = dict(config)
    out['actions'] = []
    return out
try:
    from hartos.helper import PROMPTS_DIR
except Exception:
    PROMPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'prompts'))
os.makedirs(PROMPTS_DIR, exist_ok=True)
from hartos import helper as helper_fun
# Lazy — same heavy-chain rationale as the `autogen` proxy above; used
# only inside the agent-building functions.
transform_messages = lazy_module(
    "autogen.agentchat.contrib.capabilities.transform_messages")
transforms = lazy_module(
    "autogen.agentchat.contrib.capabilities.transforms")
import threading
from concurrent.futures import ThreadPoolExecutor
import traceback
# NOTE: the module-level `import txaio; from autobahn... import Component` was
# removed — the WAMP RPC path (subscribe_and_return) now lives in helper_fun, so
# reuse_recipe no longer references autobahn/Component. The import was dead here
# and hard-failed `import reuse_recipe` wherever autobahn isn't installed (CI base
# install); tests/unit/test_lazy_autogen_import.py guards that import.

from hartos.threadlocal import thread_local_data
# #509: canonical tool-logging decorator — wraps each autogen tool with
# entry/exit/error logs, structured JSON error envelope, str-coercion,
# coroutine-accidental-return guard, AND per-tool publish_chat_stage UI
# emit.  Applied below to every `@assistant.register_for_execution()` +
# `@helper.register_for_llm(...)` decorator stack.  Module-level import
# so each inner `def` inside create_agents_for_user(...) can decorate
# with `@log_tool_execution` directly.
from core.tool_logging import log_tool_execution
# UI status labels for these inner tools live in the canonical static
# dict at core/constants.py:TOOL_LABELS — no per-import registration.

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

# Add Smart Ledger for persistent task tracking in reuse mode - using agent_ledger package
try:
    from agent_ledger import (
        SmartLedger, Task, TaskType,
        TaskStatus as LedgerTaskStatus,  # Agent ledger task status (PENDING, IN_PROGRESS, etc.)
        ExecutionMode,
        create_ledger_from_actions, get_production_backend
    )
except ImportError:
    SmartLedger = None
    Task = None
    TaskType = None
    LedgerTaskStatus = None
    ExecutionMode = None
    create_ledger_from_actions = None
    get_production_backend = None

# Import helper_ledger functions for subtask management and ledger awareness
from hartos.helper_ledger import (
    add_subtasks_to_ledger,
    check_and_unblock_parent,
    get_pending_subtasks,
    get_default_llm_client
)

# Import sync function from lifecycle_hooks
from hartos.lifecycle_hooks import (
    sync_action_state_to_ledger, register_ledger_for_session,
    ActionState, safe_set_state, force_state_through_valid_path, get_action_state,
)
from hartos.cultural_wisdom import get_cultural_prompt


class ActionExecutionStatus(Enum):
    """Status for background action execution (NOT the same as agent_ledger TaskStatus)"""
    INITIALIZED = "INITIALIZED"
    SCHEDULED = "SCHEDULED"
    EXECUTING = "EXECUTING"
    TIMEOUT = "TIMEOUT"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"

class TaskNames(Enum):
    GET_ACTION_USER_DETAILS = "GET_ACTION_USER_DETAILS"
    GET_TIME_BASED_HISTORY = "GET_TIME_BASED_HISTORY"
    ANIMATE_CHARACTER = "ANIMATE_CHARACTER"
    STABLE_DIFF = "STABLE_DIFF"
    LLAVA = "LLAVA"
    CRAWLAB = "CRAWLAB"
    USER_ID_RETRIEVER = "USER_ID_RETRIEVER"


# Performance: cached config loading (shared singleton)
from core.config_cache import get_config as _get_config
from core.http_pool import pooled_post, pooled_get, pooled_request
from core.event_loop import get_or_create_event_loop
from core.session_cache import TTLCache
from core.cache_loaders import load_agent_data, load_user_ledger, load_recipe, load_user_simplemem

config = _get_config()
STUDENT_API = config.get('STUDENT_API', '')
ACTION_API = config.get('ACTION_API', '')

def parse_date(date_str):
    return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")


def publish_async(topic, message, timeout=2.0):
    """Delegate to the canonical publish_async in hart_intelligence.

    Singleton accessor — see core.safe_hartos_attr docstring for why
    workers must not eager-import the heavy chain.
    """
    from core.safe_hartos_attr import safe_hartos_attr
    _publish = safe_hartos_attr('publish_async')
    if _publish is not None:
        _publish(topic, message, timeout)

scheduler = BackgroundScheduler()
scheduler.start()

# Register an atexit shutdown so the scheduler stops queuing jobs BEFORE
# the ThreadPoolExecutor it submits to gets torn down by the interpreter's
# normal teardown chain.  Without this, every shutdown produced 800+
# "RuntimeError: cannot schedule new futures after shutdown" tracebacks
# (langchain.log live evidence 2026-05-15: 863 occurrences of
# `call_visual_task` failing this way at the 2s interval).
#
# Why atexit (not runtime_manager): the scheduler is created at MODULE
# IMPORT time before any runtime_manager exists, by both Nunba and the
# cloud HARTOS service.  atexit is the only hook guaranteed to fire
# before ThreadPoolExecutor.shutdown across every deployment topology.
#
# wait=False: do NOT block interpreter exit on in-flight visual tasks;
# letting them die mid-flight is fine because the next launch will
# re-create them from the recipe config.
import atexit as _atexit
def _shutdown_reuse_scheduler():
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    except Exception:
        # Late-teardown: logging may already be torn down; swallow.
        pass
_atexit.register(_shutdown_reuse_scheduler)
# logging_session_id = runtime_logging.start(config={"dbname": "logs.db"})
# Store user-specific agents & their chat history
# Performance: TTL caches replace unbounded global dicts (auto-expire after 2 hours)
user_agents: "Dict[str, Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]]" = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_user_agents')
role_agents: "Dict[str, Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]]" = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_role_agents')
recipes = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_recipes', loader=load_recipe)
user_journey = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_user_journey')
temp_users = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_temp_users')
# Persona/role TTLCaches now live in core.persona_registry (single-writer
# invariant per #510).  Same module-level singletons — existing usage sites
# at lines 352, 359, 723, 728, 740, 745, 802, 806 keep working unchanged.
from core.persona_registry import (
    agents_session, agents_roles, chat_joinees,
    register_persona_for_session, _send_message_to_roles_impl,
)
llm_call_track = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_llm_call_track')

_active_tools = {}
_active_tools_lock = threading.Lock()

# ── Re-entrancy guard for concurrent turns on the SAME user+prompt ──────────
#
# user_agents[user_prompt] caches ONE autogen GroupChat per user+prompt, and
# every agent in it is mutable shared state.  Two turns running concurrently
# for the same key therefore drive the same GroupChat, and they corrupt each
# other -- observed live as group_chat.messages being EMPTY the instant
# initiate_chat() returned, which then raised IndexError out of
# get_agent_response and delivered the literal text "Error getting response:
# list index out of range" to the user on Discord/WhatsApp/Telegram.
#
# The concurrency is not hypothetical and not the caller's fault: a real user
# turn causes the speculative dispatcher to POST *back* to /chat on localhost
# (integrations/agent_engine/speculative_dispatcher.py -- the non-bundled
# branch), landing a second, identical turn on the same key ~6ms later.
# speculative_dispatcher.py already warns that re-entering /chat "causes
# re-entrancy" and avoids it in bundled mode; the HTTP path had no such guard.
#
# A speculative turn is a pure optimisation whose callers all tolerate an
# empty result, so the duplicate is dropped rather than queued -- queueing it
# behind the real turn would deadlock, because the real turn is what is
# blocked waiting on it.
_inflight_turns = set()
_inflight_turns_lock = threading.Lock()

# Module logger for the GroupChat tracer below.  Deliberately NOT current_app:
# the tracer must be able to report from any thread, with or without a Flask
# app context.  Self-contained import so the careful io_guard-first ordering at
# the top of this file is left alone.
import logging as _logging  # noqa: E402  (intentional, see comment above)
_LOG = _logging.getLogger(__name__)


# ── Wall-clock bound on a single turn's GroupChat round-robin ──────────────
#
# The reuse loop in get_agent_response already has iteration/second bounds,
# but they guard the WRONG loop: get_agent_response calls
# user_proxy.initiate_chat(...) FIRST, and the bounded `while True` runs only
# after it returns.  The runaway lives inside autogen's own round-robin, which
# those bounds never observe.
#
# Measured 2026-08-12: a plain "hello" ran 1408s (23m28s).  max_round=10 did
# fire -- the tally was Assistant x10 -- but ten rounds of a local 4B at
# ~90-140s each is 23 minutes, and the caller had long since timed out.  A
# round cap cannot bound wall-clock when each round is unboundedly slow.
#
# autogen's contract: a custom speaker_selection_method returning None raises
# NoEligibleSpeaker, which run_chat catches with a plain `break` (groupchat.py
# ~1186).  So returning None ends the chat *gracefully* -- messages already
# appended survive, and the existing conversational-reply extraction and
# post-loop fallback still run.  That makes the selector the correct place to
# enforce a deadline: it is called once per round, inside the loop that is
# actually spinning.
# Scope is the THREAD, not the session.  A session-keyed dict looks right and
# is wrong: the speculative/expert-dispatch re-entry means two turns run
# concurrently under the SAME user_prompt (observed 2026-08-12 --
# "[REENTRANCY] concurrent non-speculative turn ... shared GroupChat may
# interleave").  With one shared entry, whichever turn finished first ran its
# finally: and disarmed the other, silently restoring unbounded behaviour for
# the turn still running.  Caught by watching a real run, not by the tests.
#
# A thread is the right unit: autogen's initiate_chat, the speaker selectors
# it calls, and the reuse loop afterwards all execute synchronously on the
# thread that started the turn.
_turn_deadline_state = threading.local()


def _begin_turn_deadline(user_prompt: str, only_if_unset: bool = False) -> float:
    """Arm the wall-clock deadline for this thread's turn.  Returns it.

    ``only_if_unset`` keeps an already-running clock rather than restarting
    it.  /chat arms the deadline at the request entry so the bound covers the
    time a *user* actually waits; get_agent_response then arms with
    only_if_unset=True so it extends nothing -- re-arming there would reset
    the clock partway through and hand back the very overshoot the entry-point
    arming exists to remove.  Measured 2026-08-12: ~84s elapses between a
    Discord message arriving and initiate_chat starting, so a 150s bound armed
    at initiate_chat let a real turn run 234s.
    """
    if only_if_unset and getattr(_turn_deadline_state, 'deadline', None) is not None:
        return _turn_deadline_state.deadline
    _seconds = float(os.environ.get('HEVOLVE_TURN_MAX_SECONDS', '150'))
    _deadline = time.time() + _seconds
    _turn_deadline_state.deadline = _deadline
    _turn_deadline_state.user_prompt = user_prompt
    return _deadline


def _clear_turn_deadline(user_prompt: str) -> None:
    """Disarm — only ever affects the calling thread's own turn."""
    _turn_deadline_state.deadline = None
    _turn_deadline_state.user_prompt = None


def _turn_deadline_exceeded(user_prompt: str) -> bool:
    """True once this thread's turn has outlived HEVOLVE_TURN_MAX_SECONDS.

    Nothing armed => no deadline => never expired.  A turn that somehow
    reaches a selector without going through _begin_turn_deadline keeps the
    old unbounded behaviour rather than being killed by a stale entry.

    ``user_prompt`` is still taken (and checked) so a selector belonging to a
    different session than the armed turn can never be terminated by it --
    cached agents are shared, and their closures capture their own
    user_prompt.
    """
    _deadline = getattr(_turn_deadline_state, 'deadline', None)
    if _deadline is None:
        return False
    _armed_for = getattr(_turn_deadline_state, 'user_prompt', None)
    if _armed_for is not None and _armed_for != user_prompt:
        return False
    return time.time() > _deadline


def _resync_manager_reply_config(manager, group_chat) -> int:
    """Re-point a GroupChatManager's registered run_chat config at the LIVE
    ``group_chat.messages`` list.  Returns the number of configs re-pointed.

    Why this is needed
    ------------------
    autogen's ``ConversableAgent.register_reply`` stores ``copy.copy(config)``,
    so ``GroupChatManager.__init__`` registers ``run_chat`` against a *shallow
    copy* of the GroupChat -- a different object that merely shares the same
    ``.messages`` list at construction time.

    ``run_chat`` appends to that bound copy, never to ``manager.groupchat``.
    So the moment anything REASSIGNS ``group_chat.messages`` after the manager
    is built -- which the MemoryGraph/provenance hooks below do -- the copy
    keeps pointing at the original list while every reader here uses the new
    one.  Result: autogen writes a full conversation into a list nobody reads,
    ``group_chat.messages`` stays empty forever, and every channel gets the
    "I wasn't able to put a response together" fallback even though the model
    produced a perfectly good reply.

    That was the empty-GroupChat defect (2026-08-04..08-10).  It is
    deterministic, not a race, which is why restarting never helped.

    Keep this call AFTER the last ``.messages`` reassignment.  Better still, do
    not reassign ``.messages`` after manager construction at all -- this
    function exists because the hooks below have to.
    """
    _n = 0
    try:
        import autogen as _ag
        for _entry in getattr(manager, '_reply_func_list', []) or []:
            _cfg = _entry.get('config')
            if isinstance(_cfg, _ag.GroupChat) and _cfg is not group_chat:
                if _cfg.messages is not group_chat.messages:
                    _cfg.messages = group_chat.messages
                    _n += 1
    except Exception as _e:
        try:
            _LOG.error(f'[GC-RESYNC-FAILED] {_e}')
        except Exception:
            pass
    return _n


def _is_expert_dispatch_reentry() -> bool:
    """True when this /chat request is the dispatcher calling back into itself.

    Detected by the SHAPE of the payload, not by a truthy flag: the dispatcher
    deliberately sends ``'speculative': False`` / ``'draft_first': False`` (its
    own "hard no-reentry" markers, telling the inner /chat not to speculate
    again), so testing those values always reports False.  What actually
    identifies the caller is that it explicitly sends ``model_config`` together
    with those markers -- a combination no external client (curl, the RN app,
    a channel adapter) ever produces.
    """
    try:
        from flask import request as _rq
        body = _rq.get_json(silent=True) or {}
    except Exception:
        return False
    if not isinstance(body, dict):
        return False
    return 'model_config' in body and (
        'speculative' in body or 'draft_first' in body)

# (removed dead module-level redis_client — never referenced; the only
# `redis_client` uses here are getattr(backend, 'redis_client') on ledger
# backends, unrelated. The one live recipe-pipeline client is helper.py. #93)
agent_data = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_agent_data', loader=load_agent_data)
user_simplemem = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_user_simplemem', loader=load_user_simplemem)
# Azure OpenAI fallback config removed — credentials must come from
# HEVOLVE_AZURE_API_KEY env var or SecretsManager, never hardcoded.

# Mode-aware config_list: cloud/regional use external LLM, flat uses local
# (user's wizard-configured endpoint via HEVOLVE_LOCAL_LLM_URL)
from core.autogen_config import get_autogen_config_list
from core.platform_paths import get_coding_workspace_dir
config_list = get_autogen_config_list()

# Per-request model config override (speculative execution, hive compute routing)
# Canonical implementation lives in helper.py — thin wrapper passes local config_list.
def get_llm_config():
    return helper_fun.get_llm_config(config_list)

message_tracking_lock = threading.Lock()

# Register 96 Expert Agents with skill registry for dream fulfillment
try:
    import logging
    logger = logging.getLogger(__name__)
    expert_agents = register_all_experts(skill_registry)
    logger.info(f"Registered {len(expert_agents)} expert agents with skill registry")
except Exception as e:
    if 'logger' in dir():
        logger.error(f"Failed to register expert agents: {e}")
    expert_agents = {}


class Action:
    def __init__(self, actions):
        self.actions = actions
        self.current_action = 1
        self.fallback = False
        self.new_json = []
        self.recipe = False
        self.ledger = None  # Smart Ledger for persistent task tracking

    def get_action(self, current_action):
        try:
            return self.actions[current_action]
        except Exception:
            raise IndexError("Custom message: Index is out of range!")

    def set_ledger(self, ledger):
        """Attach Smart Ledger to this Action instance"""
        self.ledger = ledger
        current_app.logger.info(f"Smart Ledger attached with {len(ledger.tasks)} tasks")


# Updated subscribe_and_return function


from core.config_cache import get_db_url
database_url = get_db_url() or 'https://mailer.hertzai.com'


def save_conversation_db(text, user_id, prompt_id, database_url, request_id):
    """Delegate to canonical implementation in helper.py."""
    return helper_fun.save_conversation_db(text, user_id, prompt_id, database_url, request_id)


def get_role(user_id, prompt_id):
    creator = True if f'{user_id}_{prompt_id}' in agents_session.keys() else False
    role = None
    if creator:
        for i in agents_session[f'{user_id}_{prompt_id}']:
            if i['user_id'] == user_id:
                role = i['role']
                break
    if not role:
        if user_id in chat_joinees.keys():
            chat_creator_user_id = f"{chat_joinees[user_id][prompt_id]}_{prompt_id}"
            for i in agents_session[f"{chat_creator_user_id}"]:
                if i['user_id'] == user_id:
                    role = i['role']
                    break
    if not role:
        role = 'user'
    return role


def clear_message_tracking(user_prompt, unique_message_key):
    """Clear message tracking for a specific request"""
    try:
        if (user_prompt in request_id_list_sent_intermediate and
                unique_message_key in request_id_list_sent_intermediate[user_prompt]):
            del request_id_list_sent_intermediate[user_prompt][unique_message_key]
    except Exception as e:
        pass


def send_message_to_user1(user_id, response, inp, prompt_id, reset_tracking_delay=50):
    """
    Send message to user with improved tracking of sent messages
    """
    user_prompt = f'{user_id}_{prompt_id}'
    random_num = random.randint(1000, 9999)
    original_request_id = request_id_list.get(user_prompt, str(uuid.uuid4()))
    intermediate_request_id = f'{original_request_id}-intermediate-{random_num}'
    # Process response to ensure it's a string
    if not isinstance(response, str):
        if isinstance(response, dict):
            if 'content' in response:
                response = response['content']
            else:
                response = str(response)
        else:
            response = str(response)

    message_hash = get_message_hash(response, original_request_id)
    unique_message_key = f"{original_request_id}_{message_hash}"

    message_already_sent = (
            user_prompt in request_id_list_sent_intermediate and
            unique_message_key in request_id_list_sent_intermediate[user_prompt]
    )
    if message_already_sent:
        return f'Message already sent successfully to user with request_id: {original_request_id}'

    # Use a lock to ensure thread safety when updating shared state
    with message_tracking_lock:
        # Initialize the tracking dictionary for this user_prompt if it doesn't exist
        if user_prompt not in request_id_list_sent_intermediate:
            request_id_list_sent_intermediate[user_prompt] = {}

        # Track that we've sent a message for this specific original_request_id
        request_id_list_sent_intermediate[user_prompt][unique_message_key] = True

    # Schedule a task to clear the tracking after the delay
    job_id = f"clear_tracking_{user_prompt}_{original_request_id}_{int(time.time())}"

    try:
        # Check if job already exists before adding
        if scheduler.get_job(job_id) is None:
            run_time = datetime.fromtimestamp(time.time() + reset_tracking_delay)
            scheduler.add_job(
                clear_message_tracking,
                'date',
                run_date=run_time,
                id=job_id,
                args=[user_prompt, unique_message_key],
                replace_existing=True  # Use replace_existing to avoid conflicts
            )
    except Exception as e:
        current_app.logger.error(f"Error scheduling tracking reset: {e}")

    # Send the message to the user
    url = 'http://aws_rasa.hertzai.com:9890/autogen_response'
    body = json.dumps({'user_id': user_id, 'message': response, 'inp': inp, 'request_id': intermediate_request_id, 'Agent_status': 'Reuse Mode'})
    headers = {'Content-Type': 'application/json'}

    try:
        res = pooled_post(url, data=body, headers=headers)
        current_app.logger.info(
            f'Message sent with request_id: {intermediate_request_id}, tracking will reset in {reset_tracking_delay}s')
    except Exception as e:
        current_app.logger.error(f"Error sending message to user: {e}")
        return f'Failed to send message to user with request_id: {original_request_id}'

    return f'Message sent successfully to user with request_id: {original_request_id}'



def execute_python_file(task_description: str, user_id: int, prompt_id: int, action_entry_point: int = 0):
    headers = {'Content-Type': 'application/json'}
    url = f'http://localhost:{_get_llm_port("backend")}/time_agent'
    data = json.dumps({'task_description': task_description, 'user_id': user_id, 'prompt_id': prompt_id,
                       'action_entry_point': action_entry_point, 'request_from': 'Reuse'})
    res = pooled_post(url, data=data, headers=headers)
    return 'done'


def call_visual_task(task_description: str, user_id: int, prompt_id: int):
    # NOTE on logging: this function runs inside the APScheduler
    # BackgroundScheduler thread (created at line 174), which has NO Flask
    # application context.  Using `current_app.logger` from this thread
    # raises `RuntimeError: Working outside of application context.`
    # (Werkzeug's LocalProxy resolution).  Live evidence 2026-05-15: the
    # outer except below caught a backend connectivity failure, then the
    # logger call itself re-raised the LocalProxy error.  Use the
    # module-level `logger` (logging.getLogger(__name__)) — it works in
    # any thread regardless of Flask context.
    # Guard: ACTION_API is '' when not set in config.json (config.get
    # default).  This job runs on a 2s IntervalTrigger, so with an empty
    # ACTION_API the action-details GET below builds a bare
    # f"{ACTION_API}?user_id=..." == "?user_id=..." (no scheme/host) →
    # pooled_request raises "Invalid URL" EVERY 2s, forever — spamming the
    # log (~30 errs/min, live 2026-05-31) and burning CPU that feeds the
    # box-busy → governor-throttle which starves the flywheel.  The visual
    # task cannot work without the action API, so skip cheaply.
    if not ACTION_API:
        return None

    headers = {'Content-Type': 'application/json'}
    url = f'http://localhost:{_get_llm_port("backend")}/visual_agent'

    # Get current time in UTC for comparison
    now_utc = datetime.utcnow()

    # Get user action data to check for Video Reasoning entries
    try:
        action_url = f"{ACTION_API}?user_id={user_id}"
        payload = {}
        headers_api = {}

        response = pooled_request("GET", action_url, headers=headers_api, data=payload)

        if response.status_code == 200:
            api_data = response.json()

            # Filter for Video Reasoning entries within last 5 minutes
            recent_video_reasoning_entries = []
            for obj in api_data:
                if obj.get("zeroshot_label") == 'Video Reasoning':
                    try:
                        # Parse the created_date (assuming UTC)
                        created_date = datetime.strptime(obj["created_date"], "%Y-%m-%dT%H:%M:%S")

                        # Check if within last 5 minutes
                        time_diff = now_utc - created_date
                        logger.info(
                            f"Found video Reasoning entry: {obj['action']} (created {time_diff} ago)")
                        if time_diff <= timedelta(minutes=5):
                            recent_video_reasoning_entries.append(obj)
                            logger.info(
                                f"Found recent Video Reasoning entry: {obj['action']} (created {time_diff} ago)")
                    except (ValueError, KeyError) as e:
                        logger.warning(f"Error parsing date for entry {obj.get('action_id')}: {e}")
                        continue

            # Execute visual task if at least one recent Video Reasoning entry is found
            if recent_video_reasoning_entries:
                logger.info(
                    f"Found {len(recent_video_reasoning_entries)} recent Video Reasoning entries (within last 5 minutes) - executing visual task")

                data_to_send = json.dumps({
                    'task_description': task_description,
                    'user_id': user_id,
                    'prompt_id': prompt_id,
                    'request_from': 'Reuse'
                })

                try:
                    # Send the POST request to the visual agent
                    res = pooled_post(url, data=data_to_send, headers=headers)
                    logger.info(f"Visual agent response: {res.status_code}")
                    return 'done'
                except Exception as e:
                    logger.error(f"Failed to call visual agent: {e}")
                    return 'error'
            else:
                logger.info(
                    "No recent Video Reasoning entries found (within last 5 minutes) - skipping visual task")
                return None

        else:
            logger.error(f"Failed to get user actions: {response.status_code}")
            return 'error'

    except Exception as e:
        logger.error(f"Error getting user action details: {e}")
        return 'error'


def time_based_execution(task_description: str, user_id: int, prompt_id: int, action_entry_point: int):
    current_app.logger.info(f'INSIDE TIME_BASED_EXECUTION with action_entry_point"{action_entry_point}')
    user_prompt = f'{user_id}_{prompt_id}'
    if user_prompt not in user_agents:
        current_app.logger.info('user_id is not present')
    else:
        # TODO use action_entry_point to give actions via chatinstructor by changing currnt action
        assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group = user_agents[user_prompt]
        # author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_id]
        current_time = datetime.now()
        text = f'This is the time now {current_time}\n you must perform this task {task_description}'
        result = time_user.initiate_chat(manager_1, message=text, speaker_selection={"speaker": "assistant"},
                                         clear_history=False)
        last_message = group_chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
            last_message = group_chat.messages[-2]
        # sending response to receiver agent
        if f'message2userfinal'.lower() in last_message['content'].lower():
            try:
                json_obj = retrieve_json(last_message['content'])
                if json_obj and 'message2userfinal' in json_obj:
                    last_message['content'] = json_obj['message2userfinal']
                    send_message_to_user1(user_id, last_message['content'], task_description, prompt_id)

            except Exception as e:
                current_app.logger.error(f"Error extracting JSON: {e}")
                # Fallback to a basic pattern match if retrieve_json fails
                pattern = r'@user\s*{[\'"]message2userfinal[\'"]\s*:\s*[\'"](.+?)[\'"]}'
                match = re.search(pattern, last_message['content'], re.DOTALL)
                if match:
                    last_message['content'] = match.group(1)
                    send_message_to_user1(user_id, last_message['content'], task_description, prompt_id)
        # At this point, don't process messages with message2userfinal as they were already sent
        return 'done'
    return 'done'

import hashlib
def get_message_hash(content, request_id):
    """
    Generate a hash for the message content + request_id to track unique messages
    This prevents conflicts across different requests
    """
    # Combine message content with request_id for unique hash
    hash_input = f"{request_id}:{content}"
    return hashlib.md5(hash_input.encode()).hexdigest()[:10]

def get_action_user_details(user_id):
    """Thin delegate to the canonical ``core.user_context`` resolver.

    The reuse_recipe flow runs during PRODUCTION chat where the prompt
    needs the full rich output (deduplicated actions, 5-min visual
    context window, 2-min screen context window, current-time hint).
    ``mode='reuse'`` selects the rich formatter inside the canonical
    resolver. Three inline copies of this function previously drifted
    across hart_intelligence_entry, create_recipe, and reuse_recipe —
    consolidation into ``core.user_context.get_user_context`` gives
    one source of truth plus TTL cache + 1.5s hot-path budget for
    free. See the 2026-04-11 "hi took 33.8s" post-mortem for the
    motivation. No Python-side classification of the user's message
    — the draft 0.8B model owns that responsibility.
    """
    from core.user_context import get_user_context
    return get_user_context(user_id=user_id, mode='reuse')


def visual_based_execution(task_description: str, user_id: int, prompt_id: int):
    current_app.logger.info(f'INSIDE Visual_BASED_EXECUTION')
    user_prompt = f'{user_id}_{prompt_id}'

    frame = get_frame(str(user_id))
    minutes = 5
    actions = helper_fun.get_visual_context(user_id, minutes)
    if frame is None or actions is None:
        current_app.logger.info("Camera is OFF or no frame found — skipping visual agent.")
        return

    if user_prompt not in user_agents:
        current_app.logger.info('user_id is not present in user_agents.')
    else:
        assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group = \
        user_agents[user_prompt]

        # Log the current time
        current_time = datetime.now()

        # Prepare the task message
        text = f'''This is the time now {current_time}
            You are an assistant in a visual execution system. Perform the requested action based on the task context.
            Note: Visual input is available because the user's camera is ON.
            <Last_{minutes}_Minutes_Visual_Context_End>: {actions}
            If the user needs to be informed (e.g., task completed, input needed, error), respond in this exact JSON format:
            {{"message2userfinal": "Your clear and useful message here"}}
            Only send this if you have something meaningful to say.
            Do not interrupt the user unless they have asked for a response or the task cannot proceed without their input.
            You must now perform this task: {task_description}'''

        # Proceed with sending the message to the visual agent group
        manager = visual_agent_group['manager_2']
        user = visual_agent_group['visual_user']
        chat = visual_agent_group['group_chat_2']

        result = user.initiate_chat(manager, message=text, speaker_selection={"speaker": "assistant"},
                                    clear_history=False)

        last_message = chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
            if len(chat.messages) > 1:
                last_message = chat.messages[-2]
            if 'message2userfinal' in last_message['content'].lower():
                try:
                    json_obj = retrieve_json(last_message['content'])
                    if json_obj and 'message2userfinal' in json_obj:
                        send_message_to_user1(user_id, json_obj['message2userfinal'], task_description, prompt_id)
                except Exception as e:
                    current_app.logger.error(f"Error processing visual agent response: {e}")

        # Optionally, you can send a response to the receiver agent or further process the message.
        # send_message_to_user1(user_id, last_message, task_description, prompt_id)

    return 'done'


def get_frame(user_id):
    """Delegate to helper.get_frame() — FrameStore first, Redis fallback."""
    return helper_fun.get_frame(user_id)


# TODO Reset action order after it reaches end.
def create_agents_for_role(user_id: str, prompt_id):
    # Uses module-level config_list (localhost:8080 for local, Azure for cloud)
    current_app.logger.info('INSIDE create_agents_for_role')

    # Create a basic function calling config
    llm_config = {
        "config_list": config_list,
        "cache_seed": None,
    }

    personas = []
    try:
        with open(helper_fun.safe_prompt_path(prompt_id), 'r') as f:
            config = json.load(f)
            personas = config['personas']
            current_app.logger.info(f'Available Personas {personas}')
    except Exception as e:
        current_app.logger.info(e)
    if len(personas) > 1:  # & also check if we have record in db/agents_session to reuser
        temp = personas.copy()
        # temp.append({"name":"user","description":"User who will use this app"})
        agent_prompt = f'''You are a Helpful Assistant follow below action's
        initiate the conversation by asking which persona they belong to among the available personas: {temp} // give the persona names & ask to select one
        And then create new chat by calling the "update_persona" tool to update the records in db & return TERMINATE
        Note: only consider answers from User agent & the tool name is "update_persona" do not hallucinate the tool name.
        '''
        assistant = autogen.AssistantAgent(
            name=f"assistant",
            llm_config=llm_config,
            max_consecutive_auto_reply=10,
            is_termination_msg=_is_terminate_msg,
            code_execution_config={"work_dir": get_coding_workspace_dir(), "use_docker": False},
            system_message=agent_prompt
        )
        user_proxy = autogen.UserProxyAgent(
            name=f"user",
            human_input_mode="NEVER",
            llm_config=False,
            is_termination_msg=_is_terminate_msg,
            max_consecutive_auto_reply=0,
            code_execution_config=False,
        )
        helper = autogen.AssistantAgent(
            name="Helper",
            llm_config=llm_config,
            code_execution_config={"work_dir": get_coding_workspace_dir(), "use_docker": False},
            system_message="""You Help the assistant agent to complete the task, you are helper agent not user/n
            if you get any request related you user redicrect that conversation to user don't asumer anything or answer anything on your own""",
            is_termination_msg=_is_terminate_msg,
        )

        @helper.register_for_execution()
        @assistant.register_for_llm(api_style="function", description="update the role/persona in db")
        @log_tool_execution
        def update_persona(name: Annotated[str, "The persona name user selected"],
                           description: Annotated[str, "The persona description user selected"],
                           new: Annotated[bool, "Wethere it is a new chat or no"],
                           contact_number: Annotated[str, "user's contact of which we will join conversation"]) -> str:
            current_app.logger.info('INSIDE update_persona')
            current_app.logger.info(f'agents_session {agents_session}')
            current_app.logger.info(f'chat_joinees {chat_joinees}')
            if new:
                current_app.logger.info('Creating new chat')
                if f"{user_id}_{prompt_id}" not in agents_session.keys():
                    agents_session[f"{user_id}_{prompt_id}"] = [
                        {'agentInstanceID': f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
                         'user_id': user_id, 'role': name, 'deviceID': 'something'}]
                    agents_roles[f"{user_id}_{prompt_id}"] = {user_id: name}
                else:
                    agents_session[f"{user_id}_{prompt_id}"].append(
                        {'agentInstanceID': f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
                         'user_id': user_id, 'role': name, 'deviceID': 'something'})
                    agents_roles[f"{user_id}_{prompt_id}"][user_id] = name
                current_app.logger.info(f'After persona update {agents_session[f"{user_id}_{prompt_id}"]}')
                return 'terminate'
            else:
                current_app.logger.info('adding in existing chat')
                if contact_number in temp_users.keys():
                    current_app.logger.info('user found with contact number')
                    if f"{temp_users[contact_number]}_{prompt_id}" in agents_session.keys():
                        current_app.logger.info('user found with contact number in agents_sessiion')
                        agents_session[f"{temp_users[contact_number]}_{prompt_id}"].append(
                            {'agentInstanceID': f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
                             'user_id': user_id, 'role': name, 'deviceID': 'something'})
                        agents_roles[f"{user_id}_{prompt_id}"][user_id] = name
                        current_app.logger.info('after append in agent_sessions')
                        chat_joinees[user_id] = {prompt_id: temp_users[contact_number]}

                        current_app.logger.info(f'agents_session {agents_session}')
                        current_app.logger.info(f'chat_joinees {chat_joinees}')
                        return 'terminate'
                    else:
                        return f'Ask the user with contact number:{contact_number} to create a new chat'
                else:
                    current_app.logger.info('user found not with contact number')
                    return f'Ask the user with contact number:{contact_number} to create a new chat'

        assistant.description = 'Agent that is designed ask the roles to the user agent'
        user_proxy.description = 'agent will act as user & perform task assigned to user'
        helper.description = 'Agent will only work with assistant agent if needs help with something which is not related to user'

        def state_transition(last_speaker, groupchat):
            messages = groupchat.messages
            if last_speaker == user_proxy:
                return assistant
            if 'TERMINATE' in messages[-1]["content"].upper():
                current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
                # retrieve: action 1 -> action 2
                return None
            return "auto"

        # Seed autogen with recent messages from shared LangChain/autogen buffer
        try:
            from integrations.channels.memory.shared_history import seed_autogen_from_shared_history
            _seed_msgs = seed_autogen_from_shared_history(user_id, max_messages=8)
        except Exception:
            _seed_msgs = []

        select_speaker_transforms = transform_messages.TransformMessages(
            transforms=[
                transforms.MessageHistoryLimiter(max_messages=5),
                transforms.MessageTokenLimiter(max_tokens=3000, max_tokens_per_message=500, min_tokens=300),
            ]
        )
        group_chat = autogen.GroupChat(
            agents=[assistant, helper, user_proxy],
            messages=_seed_msgs,
            max_round=3,
            select_speaker_prompt_template=f"Read the above conversation, select the next person from [Assistant, Helper, & User] & only return the role as agent. Return User only if the previous message demands it",
            select_speaker_transform_messages=select_speaker_transforms,
            speaker_selection_method=state_transition,  # using an LLM to decide
            allow_repeat_speaker=False,  # Prevent same agent speaking twice
            send_introductions=False,
            role_for_select_speaker_messages='user',  # Qwen3.5 rejects system mid-conversation
        )

        manager = autogen.GroupChatManager(
            groupchat=group_chat,
            llm_config={"cache_seed": None, "config_list": config_list}
        )

        # Write half of the seed/write contract: without this the group is
        # seeded FROM the shared buffer but its own turns never persist —
        # the next turn truthfully denies the conversation happened (#686).
        try:
            from integrations.channels.memory.shared_history import install_history_writeback
            install_history_writeback(group_chat, user_id)
        except Exception:
            current_app.logger.debug('role-group history write-back skipped', exc_info=True)

        return assistant, user_proxy, group_chat, manager, helper, False
    else:
        # ZERO personas lands here too, not just one.
        #
        # The branch above is `len(personas) > 1`, so this else covers BOTH the
        # single-persona case AND the empty one — and the empty one used to run
        # straight into personas[0]['name'] and raise IndexError, which 500s the
        # whole /chat request.
        #
        # Empty is not exotic on the hardware this has to run on. A 0.8B model on
        # a CPU-only potato routinely returns malformed or truncated JSON, so the
        # config above ends up with no 'personas' key at all — and the read is
        # wrapped in a try that only logs at .info, so the list silently stays [].
        # Reported from a real box: "IndexError: list index out of range at
        # reuse_recipe.py:913 (personas[0]) when the model returns empty personas".
        #
        # No personas simply means no role to choose between, which is a normal
        # single-role agent — so name it and carry on. Crashing the request is the
        # one response that cannot be right, and degrade-not-die is the standing
        # rule for every path that depends on a model behaving.
        # Through the CANONICAL registrar, not a third hand-rolled copy.
        #
        # core.persona_registry.register_persona_for_session already builds both
        # maps, accepts dict OR string personas, skips a persona with no
        # name/role instead of KeyError-ing, and never raises. Its own docstring
        # names these inline blocks in reuse_recipe as the sites it was written
        # to replace; this one was simply left behind, which is why the empty
        # case still crashed here long after the helper existed.
        #
        # `personas or [default]` is the potato guard. A 0.8B model on a CPU-only
        # box regularly returns truncated persona JSON, so the config read above
        # (whose except only logs at .info) leaves this []. Registering ZERO
        # personas would leave the session with no role at all; naming one keeps
        # the agent usable, because "no personas" just means there is nothing to
        # choose between — an ordinary single-role agent.
        if not personas:
            current_app.logger.warning(
                "prompt %s has NO personas — running as a single '%s' role. On a "
                "small local model this usually means the persona JSON came back "
                "malformed or truncated. The agent still works; it just has no "
                "role to select between.", prompt_id, DEFAULT_SINGLE_ROLE)

        # Check the COUNT it returns, don't assume the list registered.
        #
        # The helper skips any persona with no name/role, so a one-entry list of
        # malformed JSON — say [{"description": "..."}] with the name truncated
        # off, which is exactly what a 0.8B model produces — registers ZERO and
        # leaves the session with no role at all. `personas or [default]` cannot
        # catch that: the list is non-empty, its CONTENTS are unusable.
        registered = register_persona_for_session(
            user_id, prompt_id,
            personas or [{'name': DEFAULT_SINGLE_ROLE}])
        if not registered:
            current_app.logger.warning(
                "prompt %s: none of its %d persona(s) had a usable name — "
                "falling back to a single '%s' role so the agent still runs.",
                prompt_id, len(personas), DEFAULT_SINGLE_ROLE)
            register_persona_for_session(
                user_id, prompt_id, [{'name': DEFAULT_SINGLE_ROLE}])
        return 'TERMINATE', 'TERMINATE', 'TERMINATE', 'TERMINATE', 'TERMINATE', True


def create_agents_for_user(user_id: str, prompt_id) -> "Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]":
    """Create new assistant & user proxy agents for a user with basic configuration."""
    user_prompt = f'{user_id}_{prompt_id}'
    # Create a basic function calling config
    llm_config = {
        "config_list": config_list,
        "cache_seed": None
    }

    # Initialize SimpleMem for this session
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
                current_app.logger.info(f"SimpleMem initialized for {user_prompt}")
        except Exception as e:
            current_app.logger.warning(f"SimpleMem init failed: {e}")

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
        current_app.logger.info(f"MemoryGraph initialized for {user_prompt}")
    except Exception as e:
        current_app.logger.warning(f"MemoryGraph init failed: {e}")

    personas = []
    # role = get_role(user_id,prompt_id)
    role_number, role = get_flow_number(user_id, prompt_id)

    with open(helper_fun.safe_prompt_path(prompt_id, role_number, 'recipe'), 'r') as f:
        config = json.load(f)
        config = _normalize_flow_recipe(config)  # tolerate per-action recipe in flow slot
        recipes[user_prompt] = config
        final_recipe[prompt_id] = config
    goal = ''
    with open(helper_fun.safe_prompt_path(prompt_id), 'r') as f:
        config = json.load(f)
        goal = config['goal']

    current_app.logger.info(f'Got goal as {goal}')
    role_actions = []
    actions = []

    # Load any VLM agent files
    vlm_actions = load_vlm_agent_files(prompt_id, role_number)

    # Integrate VLM agent actions with existing recipe actions
    if vlm_actions:
        for vlm_action in vlm_actions:
            # Check if this action should replace an existing one or be added
            action_id = vlm_action.get("action_id")
            action_exists = False

            for i, action in enumerate(recipes[user_prompt]['actions']):
                if action.get("action_id") == action_id:
                    recipes[user_prompt]["actions"][i] = vlm_action
                    action_exists = True
                    break

            if not action_exists:
                recipes[user_prompt]['actions'].append(vlm_action)

        # Update the recipes dictionary
        final_recipe[prompt_id] = recipes[user_prompt]

    current_app.logger.info(f'Getting role actions')
    for i in recipes[user_prompt]['actions']:
        current_app.logger.info(f'this is action persona:{i["persona"]} ')
        if i['persona'].lower() == role.lower():
            role_actions.append(i)
            actions.append(i['action'])
    # current_app.logger.info(f'role_actions: {role_actions}')
    # current_app.logger.info(f'will create timer agents with: {actions}')
    time_actions[user_prompt] = Action(actions)

    if len(role_actions) == 0:
        role_actions = recipes[user_prompt]['actions']

    # Perform topological sorting
    # sorted_actions = topological_sort(role_actions)

    # Create Action with Smart Ledger integration for persistent task tracking
    user_tasks[user_prompt] = Action(role_actions)

    # Initialize or load Smart Ledger for this user with production backend (Redis with JSON fallback)
    if user_prompt not in user_ledgers:
        current_app.logger.info(f"Creating new Smart Ledger for {user_prompt} in reuse mode")
        backend = get_production_backend()  # Tries Redis, falls back to JSON (already imported from agent_ledger)
        # ``role_number`` is the recipe flow index selected for this
        # session by ``get_flow_number(user_id, prompt_id)`` at L903.
        # Threading it as ``flow_id`` stamps every recipe-derived task
        # with the correct flow so the dashboard can group:
        #   prompt_id → session_id → flow_id → action_id.
        # Recipe filename ``{prompt_id}_{role_number}_recipe.json``
        # carries the same number; the two stay in lockstep.
        ledger = create_ledger_from_actions(user_id, prompt_id, role_actions,
                                            backend=backend, flow_id=role_number)
        user_ledgers[user_prompt] = ledger

        # Best-effort: when the Redis backend is live, enable ledger
        # pubsub + heartbeat so distributed_agent subscribers can
        # receive delegation messages for this ledger.  Gated on the
        # backend carrying a real `redis_client` attribute — JSON and
        # InMemory backends skip cleanly.  See matching edit in
        # create_recipe.create_action_with_ledger for rationale.
        try:
            _redis = getattr(backend, 'redis_client', None)
            if _redis is not None:
                ledger.enable_pubsub(_redis)
                ledger.enable_heartbeat(
                    _redis,
                    host_info={
                        'user_id': user_id,
                        'prompt_id': prompt_id,
                        'mode': 'reuse',
                    },
                )
                current_app.logger.info(
                    f"Ledger pubsub+heartbeat enabled (reuse) for {user_prompt}"
                )
        except Exception as _lsetup_e:
            current_app.logger.debug(
                f"Ledger pubsub/heartbeat setup skipped (reuse) for "
                f"{user_prompt}: {_lsetup_e}"
            )

        # Register for auto-sync so ActionState changes propagate to ledger
        register_ledger_for_session(user_prompt, ledger)
        current_app.logger.info(f"Registered ledger for auto-sync in reuse: {user_prompt}")

        # Create TaskDelegationBridge for this ledger
        delegation_bridge = TaskDelegationBridge(a2a_context, ledger)
        user_delegation_bridges[user_prompt] = delegation_bridge
        current_app.logger.info(f"Created TaskDelegationBridge for {user_prompt}")
    else:
        current_app.logger.info(f"Reusing existing Smart Ledger for {user_prompt}")
        ledger = user_ledgers[user_prompt]

        # Ensure delegation bridge exists for existing ledger
        if user_prompt not in user_delegation_bridges:
            delegation_bridge = TaskDelegationBridge(a2a_context, ledger)
            user_delegation_bridges[user_prompt] = delegation_bridge
            current_app.logger.info(f"Created TaskDelegationBridge for existing ledger {user_prompt}")

    # Attach ledger to Action instance
    user_tasks[user_prompt].set_ledger(ledger)

    # Set first action to IN_PROGRESS so ledger tracks it
    safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "reuse: first action assigned")
    safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "reuse: first action starting")

    individual_recipe = []
    for i in range(1, (len(recipes[user_prompt]['actions']) + 1)):
        current_app.logger.info(f'checking for {helper_fun.safe_prompt_path(prompt_id, role_number, i)}')
        try:
            with open(helper_fun.safe_prompt_path(prompt_id, role_number, i), 'r') as f:
                config = json.load(f)
                individual_recipe.append(config)
        except Exception as e:
            current_app.logger.error(f'Got error as :{e} while checking for {helper_fun.safe_prompt_path(prompt_id, role_number, i)}')

    # Build experience hints from accumulated recipe experience data
    experience_hints = ''
    try:
        from hartos.recipe_experience import build_experience_hints
        experience_hints = build_experience_hints(individual_recipe)
    except Exception:
        experience_hints = 'No prior experience recorded.'

    # Load saved personality for this agent (generated in CREATE mode)
    _personality_block = ""
    try:
        from core.agent_personality import load_personality, build_personality_prompt, build_proactive_vision_prompt
        _saved_personality = load_personality(str(prompt_id))
        if _saved_personality:
            # Load resonance profile for continuous personality tuning
            _resonance_profile = None
            try:
                from core.resonance_profile import get_or_create_profile
                _resonance_profile = get_or_create_profile(str(user_id))
            except ImportError:
                pass
            _personality_block = build_personality_prompt(_saved_personality, resonance_profile=_resonance_profile)
            _personality_block += build_proactive_vision_prompt(goal)
    except Exception:
        pass

    response_format = {"message2userfinal": "Your message here"}
    agent_prompt = f'''You are a Helpful {role} Assistant. Your primary role is to assist the user efficiently while keeping all internal actions and processes hidden from the end user. Follow the guidelines below to perform tasks correctly:
{get_cultural_prompt()}
{_personality_block}

        HELPER IS YOUR SUPERMAN — DELEGATE EVERYTHING:
        The Helper agent has ALL the tools.  You have NONE.  For ANY task —
        web search, web scrape, file read, save/load memory, fetch chat
        history, send message to user, schedule a job, generate image,
        generate video, run a desktop command, consult an expert, search
        long-term memory, anything at all — ALWAYS tag @Helper first.
        Never refuse with "I can't access X" or "I don't have tools for Y".
        If a tool exists in the catalog, @Helper has it.  If a tool doesn't
        exist, ask @Helper to find an alternative (search, scrape, code).
        The ONLY thing Helper can't do is execute python code — that's
        @Executor's job.  Everything else goes through @Helper.  Treat
        Helper as your unlimited capability surface.

        1. If you encounter a task you cannot perform, request assistance from the @Helper and @Executor agents. If you need to run a tool, seek guidance from the @Helper agent. For code execution, ask the @Executor agent for assistance.
        2. Only execute actions where the persona is: {role}.
        3. Follow the steps below to achieve the goal: {goal}.
        4. Utilize the provided **Recipe** for all task-related details.
        5. After completing the current action, request the @statusVerifier agent to verify its completion. It will then provide the next action.
        6.  Always use the pre-tested steps and code from the provided Recipe—**do not create new implementations unless explicitly required**.
        7. **Scheduled, time-based, or continuous tasks should not be manually executed**—they are already handled by the system.
        8. **IMPORTANT CODING INSTRUCTION**: Avoid using `time.sleep` in any code.
        9. Tools Helper Agent can use:
            1. The tools are: send_message_in_seconds,send_message_to_user,send_presynthesized_video_to_user,execute_windows_or_android_command,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, google_search, save_data_in_memory, search_long_term_memory and save_to_long_term_memory.
            2. Create Scheduled Jobs: For tasks involving timer or time or periodically or scheduled jobs, ask Helper agent to use the create_scheduled_jobs tool.
            3. Data/Memory Management:
                ➜If you want to save some data,understand the current data from get_saved_metadata & plan the datamodel and ask helper agent to use "save_data_in_memory" tool.
                ➜If you want to get some data ask helper agent to use "get_data_by_key"  tool.
                ➜For searching past conversations and recalling facts, use "search_long_term_memory" tool.
                ➜For saving important facts for future sessions, use "save_to_long_term_memory" tool.
            4. If you want to send some message to user directly then ask helper agent to use send_message_to_user tool but if you want to send message after sometime then ask helper to use send_message_in_seconds tool.
            5. If you want to send some pre synthesized realistic videos to user then ask helper agent to use send_presynthesized_video_to_user tool.
            6. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the pre synthesized generated video if it is successful.
            7. If you receive a request to perform a task or action on the user's computer, or if the request is related to Chrome or any browser, you should ask @Helper to use the `execute_windows_or_android_command` tool.
            8. If you want the user's ID then ask the @Helper to use 'get_user_id' tool and do not prompt the user for their user_id, never mention the user_id to the user. Important: Get the user Id yourself always, Do not ask the user_id from User ever.
            9. If you want to do a google search then you should ask the @Helper to use the 'google_search' tool.        
        10. **Never reveal actions, internal processes, or tools to the user**. Do not ask for user confirmation unless absolutely necessary(You can assume normal things like user's interests).
        11. Calling Other Agents (Important):
            i. When you need to direct a question or route the conversation to a specific agent, use the @ tag followed by the agent's name. Examples include: @Executor or @Helper or @User
            ii. If you are responding to the user's request or need some clarification/information from user, just tag userproxy agent strictly via `@user {response_format}` or If you need to send data proactively (on your own) while continuing your current action use tools `send_message_to_user`  or `send_message_in_seconds` for sending message to user with delay,  Do not use both to convey the same.
        12. All actions, recipes, and functions provided below have been reviewed and tested. Follow them exactly—do not make assumptions or modify them unless they fail or produce an error.
        13. Always request the next action from the @StatusVerifier agent—do not determine the next action on your own.
        14. If `can_perform_without_user_input` is `yes`, execute the action automatically without requesting user confirmation.
        15. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.


        16. **Agent Creation**: If the user asks to create, build, or set up a new AI agent, assistant, or bot,
            OR if you determine that the current task requires capabilities beyond your scope and a specialized
            agent would be needed, ask @Helper to use the `create_new_agent` tool with a description of what
            the new agent should do. If the user wants it done autonomously (e.g., "automatically", "do it for me"),
            include "autonomous" in the description.

        Actions: <actionsStart>{role_actions}<actionEnd>
        Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>

        PREVIOUS EXPERIENCE (use to avoid dead ends and improve efficiency):
        {experience_hints}

        When writing code, always print the final response just before returning it.
        Note: Other agents do not have access to these actions or recipe information. Ensure you provide them with the necessary context and related information to perform the required actions.
    '''
    if role == '':
        role = 'Assistant'
    else:
        role = f'{role}'
    assistant = autogen.AssistantAgent(
        name='Assistant',
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=_is_terminate_msg,
        code_execution_config={"work_dir": get_coding_workspace_dir(), "use_docker": False},
        system_message=agent_prompt
    )

    # Wrap assistant with Agent Lightning for training and optimization
    if is_agent_lightning_enabled():
        try:
            assistant = instrument_autogen_agent(
                agent=assistant,
                agent_id=f'reuse_recipe_assistant_{user_prompt}',
                track_rewards=True,
                auto_trace=True
            )
            current_app.logger.info(f"Agent Lightning instrumentation applied to assistant for {user_prompt}")
        except Exception as e:
            current_app.logger.warning(f"Could not apply Agent Lightning: {e}. Continuing with standard agent.")

    # current_app.logger.info(f'creating agent with prompt {agent_prompt}')

    # Create the user proxy agent
    user_proxy = autogen.UserProxyAgent(
        name=f"User",
        human_input_mode="NEVER",
        llm_config=False,
        is_termination_msg=_is_terminate_msg,
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )
    helper = autogen.AssistantAgent(
        name="Helper",
        llm_config=llm_config,
        code_execution_config=False,
        system_message=f"""You are Helper Agent. Help the {role} agent to complete the task:
            1. Follow the steps below to achieve the goal: {goal}.
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than {role},Executor,multi_role_agent.
            4. Tools you have [txt2img, img2txt, save_data_in_memory, get_data_from_memory, search_long_term_memory, save_to_long_term_memory, get_user_id, get_prompt_id, Generate_video, get_user_uploaded_file, get_user_camera_inp, get_chat_history, create_scheduled_jobs, send_message_to_user,send_presynthesized_video_to_user] If a task cannot be completed using the available tools, first check the recipe. If no solution is found, create Python code to accomplish the task.
            5. Keep track of action and only ask for next action when the current action is completed successfully.
            6. Always use code from recipe given below.
            7. If there is any action which is like to perform a task continuously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            8a. CRITICAL PATH INSTRUCTION: When creating file paths in code, ALWAYS use os.path.join(os.getcwd(), filename) or similar. NEVER use hardcoded absolute paths like '/home/user/path' or 'C:\\path'. All paths must be relative to the current working directory.
            9. If you want to send data proactively (on your own) to user use `@user {response_format}`. However, if you're responding to the user's request or instruction, use the send_message_to_user or send_message_in_seconds tool.
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            11. Always request the next action from the @StatusVerifier agent—do not determine the next action on your own.
            12. After completing the current action, request the @StatusVerifier agent to verify its completion. It will then provide the next action.

            Actions: <actionsStart>{role_actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>

            When writing code, always print the final response just before returning it.
        """,
        is_termination_msg=_is_terminate_msg,
    )
    executor = autogen.AssistantAgent(
        name="Executor",
        llm_config=llm_config,
        code_execution_config={"last_n_messages": 2, "work_dir": get_coding_workspace_dir(), "use_docker": False},
        system_message=f'''You are a executor agent. focused solely on creating, running & debugging code.
            Your responsibilities:
            1. Follow the steps below to achieve the goal: {goal}.
            2. Use the provided Recipe for more details related to the actions.
            3. Ask @Helper to use the "send_message_to_roles" tool when contacting personas other than {role},Executor,multi_role_agent.
            4. Tools Helper Agent can use [send_message_in_seconds,send_message_to_user,send_presynthesized_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, save_data_in_memory, search_long_term_memory and save_to_long_term_memory]
            5. Keep track of action and only ask for next action when the current action is completed successfully.
            6. Always use code from recipe given below.
            7. If there is any action which is like to perform a task continuously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            8a. CRITICAL PATH INSTRUCTION: When creating file paths in code, ALWAYS use os.path.join(os.getcwd(), filename) or similar. NEVER use hardcoded absolute paths like '/home/user/path' or 'C:\\path'. All paths must be relative to the current working directory.
            9. If you want to send data proactively (on your own) to user use `@user {response_format}`. However, if you're responding to the user's request or instruction, use the send_message_to_user or send_message_in_seconds tool.
            10. The response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            11. Always request the next action from the @StatusVerifier agent—do not determine the next action on your own.
            12. After completing the current action, request the @StatusVerifier agent to verify its completion. It will then provide the next action.
            13. If you get any request to call a tool always ask @Helper to perfor it.
            Actions: <actionsStart>{role_actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>

            Note: Your Working Directory is "{os.getcwd()}" - use this as the base path for all file operations. Always use absolute paths by joining with this directory,
            Add proper error handling, logging.
            Always provide clear execution results or error messages to the assistant.
            if you get any conversation which is not related to coding ask the manager to route this conversation to user
            When writing code, always print the final response just before returning it.
        ''',
        is_termination_msg=_is_terminate_msg,
    )

    multi_role_agent = autogen.AssistantAgent(
        name="multi_role_agent",
        llm_config=llm_config,
        code_execution_config=False,
        system_message="""You will send message from multiple different personas, your job is to ask those question to assistant agent
        if you think some text was intended to give to some other agent but i came to you instead, send the same message to user/author""",
    )
    verify = autogen.AssistantAgent(
        name="StatusVerifier",
        llm_config=llm_config,
        code_execution_config=False,
        system_message=""""You are an Status verification agent.
        Role: Track and verify the status of actions. Provide updates strictly in JSON format only when status is completed.
        Response formats:
            1. Action Completed Successfully: {"status": "completed","action": "current action","action_id": 1/2/3...,"message": "message here"}
            2. Action Error: {"status": "error","action": "current action","action_id": 1/2/3...,"message": "message here"}
            3. Action Pending: {"status": "pending","action": "current action","action_id": 1/2/3...,"message": "pending actions here"}
            4. Action Requires Breakdown: {"status": "requires_breakdown","action": "current action","action_id": 1/2/3...,"reason": "Why this action needs to be broken down","subtasks": [{"subtask_id": "1.1","description": "First subtask description","depends_on": [],"can_perform_autonomously": true},{"subtask_id": "1.2","description": "Second subtask","depends_on": ["1.1"],"can_perform_autonomously": true}]}
        Important Instructions:
            Only mark an action as "Completed" if the all the steps are successful completed. If any step is pending then mark the staus as pending and give the message.
            For pending tasks or ongoing actions, respond to helper to complete the task.
            Verify the action performed by assistant and make sure the action is performed correctly as per instructions. if action performed was not as per instructions give the pending actions to the helper agent.
            Report status only—do not perform actions yourself and do not try calling any functions/tools.
            Use "requires_breakdown" when an action is too complex and needs to be split into smaller subtasks. Each subtask should have a unique subtask_id (e.g., "1.1", "1.2").

        """,
        is_termination_msg=_is_terminate_msg,
    )

    chat_instructor = autogen.UserProxyAgent(
        name="ChatInstructor",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        default_auto_reply="TERMINATE",
        code_execution_config=False,
        is_termination_msg=_is_terminate_msg,
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
    # chat_instructor (UserProxyAgent line 1233) was previously NOT
    # attached.  Same context-overflow root cause as create_recipe.py:903 —
    # initiate_chat with clear_history=False kept growing chat_instructor's
    # message buffer until llama.cpp's n_ctx ceiling fired 500.  Capped
    # here.
    context_handling.add_to_agent(chat_instructor)

    # #510: send_message_to_roles — multi-persona broadcast.  Canonical impl
    # lives in core.persona_registry (single source of truth for the persona
    # TTLCaches + the dispatch routine).  Same impl runs in both create + reuse
    # flows.  Uses the canonical publish_async from this module.
    @assistant.register_for_execution()
    @helper.register_for_llm(
        api_style="function",
        description="Send a message to a specific persona/role within this multi-persona agent (e.g. student/parent/teacher).")
    @log_tool_execution
    def send_message_to_roles(
        role: Annotated[str, "Target persona/role name to deliver the message to"],
        message: Annotated[str, "The question to ask or message to send"],
    ) -> str:
        return _send_message_to_roles_impl(
            user_id, prompt_id, role, message, publish_fn=publish_async)
    # #510: txt2img delegates to canonical helper_fun.txt2img (same impl as
    # core.agent_tools.text_2_image).  Was hitting cloud Rasa directly —
    # sovereignty-violating + parallel-path with core_AT.  helper_fun
    # routes local-first.
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Text to image Creator")
    @log_tool_execution
    def txt2img(text: Annotated[str, "Text to create image"]) -> str:
        return helper_fun.txt2img(text)

    # #510: img2txt delegates to the canonical core.agent_tools.get_text_from_image
    # closure.  Was a parallel impl without SSRF validation; canonical has
    # security.sanitize.validate_url + bundled-mode local Qwen Vision path.
    # The canonical closure is built later in this function (register_core_tools
    # at L2262); we reach into core_tools at call time to dispatch.
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Image to Text/Question Answering from image")
    @log_tool_execution
    def img2txt(
        image_url: Annotated[str, "image url of which you want text"],
        text: Annotated[str, "the details you want from image"] = 'Describe the Images & Text data in this image in detail',
    ) -> str:
        # Canonical impl was built by build_core_tool_closures at L2261 by the
        # time this function fires (decoration registers the wrapper; the
        # wrapper body resolves the canonical lazily on each call).
        _canon = next(
            (f for n, _, f in core_tools if n == 'get_text_from_image'),
            None) if 'core_tools' in dir() else None
        if _canon is not None:
            return _canon(image_url, text)
        # Pre-2261 / degraded-env fallback: same logic as the canonical body
        # so behavior is consistent if the closure wasn't built yet.
        from core.config_cache import get_vision_api, is_bundled
        try:
            from security.sanitize import validate_url
            image_url = validate_url(image_url)
        except (ImportError, ValueError) as e:
            return f"Error: URL blocked by security filter: {e}"
        url = get_vision_api() or "http://azurekong.hertzai.com:8000/llava/image_inference"
        if is_bundled():
            payload_str = json.dumps({'image_url': image_url, 'prompt': text})
            response = pooled_post(
                url, data=payload_str,
                headers={'Content-Type': 'application/json'}, timeout=60)
        else:
            response = pooled_request(
                "POST", url, headers={},
                data={'url': image_url, 'prompt': text}, files=[], timeout=300)
        return response.text if response.status_code == 200 else 'Not able to get this page details try later'

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Use this to Store and retrieve data using key-value storage system")
    @log_tool_execution
    def save_data_in_memory(key: Annotated[str, "Key path for storing data now & retrieving data later. Use dot notation for nested keys (e.g., 'user.info.name')."],
                            value: Annotated[Optional[Any], "Value you want to store; strictly should be one of int, float, bool, json array or json object."] = None) -> str:
        """Store data with validation to prevent corruption."""
        current_app.logger.info('INSIDE save_data_in_memory')

        # Validate the input data
        try:
            # Step 1: Use the existing JSON repair function to sanitize input
            if isinstance(value, str) and (value.startswith('{') or value.startswith('[')):
                # If the value is a JSON string, repair it
                value = retrieve_json(value)
                current_app.logger.info(f"REPAIRED JSON STRING: {value}")

            # Step 2: Force a JSON serialization/deserialization cycle to validate structure
            if value is not None:
                # This will fail if the structure isn't JSON-compatible
                json_str = json.dumps(value)
                validated_value = json.loads(json_str)
                current_app.logger.info(f"VALIDATED VALUE (post JSON cycle): {validated_value}")
            else:
                validated_value = None

            # Step 3: Store the validated data
            keys = key.split('.')
            d = agent_data.setdefault(prompt_id, {})
            for k in keys[:-1]:
                d = d.setdefault(k, {})

            d[keys[-1]] = validated_value
            current_app.logger.info(f"VALUES STORED IN AGENT DATA: {validated_value}")
            current_app.logger.info(f"FULL AGENT DATA AT KEY: {d}")

            # Mirror to MemoryGraph for persistence (fire-and-forget)
            if memory_graph is not None:
                try:
                    import threading as _t
                    _t.Thread(target=lambda: memory_graph.register(
                        f"[KV] {key} = {json.dumps(validated_value)[:200]}",
                        {'memory_type': 'fact', 'source_agent': 'helper', 'session_id': user_prompt, 'kv_key': key},
                    ), daemon=True).start()
                except Exception:
                    pass

            # Step 4: Verify storage was successful
            try:
                # Attempt to read back the data to verify it was stored correctly
                stored_value = get_data_by_key(key)
                current_app.logger.info(f"VERIFICATION - READ BACK VALUE: {stored_value}")

                # Optional: compare stored_value with what we intended to store
                if stored_value == "Key not found in stored data.":
                    current_app.logger.error(f"VERIFICATION FAILED: Data not properly stored at key {key}")
            except Exception as e:
                current_app.logger.error(f"VERIFICATION ERROR: {str(e)}")

            return f'{agent_data[prompt_id]}'

        except json.JSONDecodeError as je:
            error_msg = f"Invalid JSON structure in value: {str(je)}"
            current_app.logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"

        except TypeError as te:
            error_msg = f"Type error in value: {str(te)}"
            current_app.logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"

        except Exception as e:
            error_msg = f"Unexpected error saving data: {str(e)}"
            current_app.logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Returns the schema of the json from internal memory with all keys but without actual values.")
    @log_tool_execution
    def get_saved_metadata() -> str:
        stripped_json = strip_json_values(agent_data[prompt_id])
        return f'{stripped_json}'

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Returns all data from the internal Memory using key")
    @log_tool_execution
    def get_data_by_key(key: Annotated[
        str, "Key path for retrieving data. Use dot notation for nested keys (e.g., 'user.info.name')."]) -> str:
        keys = key.split('.')
        d = agent_data.get(prompt_id, {})

        try:
            for k in keys:
                d = d[k]
            return f'{d}'
        except KeyError:
            # Fallback: check MemoryGraph for persisted KV data
            if memory_graph is not None:
                try:
                    results = memory_graph.recall(f"[KV] {key}", mode='text', top_k=1)
                    if results:
                        return results[0].content
                except Exception:
                    pass
            return "Key not found in stored data."

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Returns the unique identifier (user_id) of the current user.")
    @log_tool_execution
    def get_user_id() -> str:
        current_app.logger.info('INSIDE get_user_id')
        return f'{user_id}'

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Returns the unique identifier (prompt_id) associated with the current prompt or conversation.")
    @log_tool_execution
    def get_prompt_id() -> str:
        current_app.logger.info('INSIDE get_prompt_id')
        return f'{prompt_id}'

    database_url = get_db_url() or 'https://mailer.hertzai.com'

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Generate video with text and save it in database")
    @log_tool_execution
    def Generate_video(text: Annotated[str, "Text to be used for video generation"],
                       avatar_id: Annotated[str, "Unique identifier for the avatar"],
                       realtime: Annotated[
                           bool, "If True, response is fast but less realistic by default it should be true; if False, response is realistic but slower"]) -> str:
        print('INSIDE Generate_video')
        database_url = get_db_url() or 'https://mailer.hertzai.com'
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        print(f"avtar_id: {avatar_id}:\n{text[:10]}....\n")

        if avatar_id == "default":
            avatar_id_int = 1  # Use appropriate default ID number
        else:
            try:
                avatar_id_int = int(avatar_id)
            except ValueError:
                avatar_id_int = 1  # Fallback to default ID if conversion fails

        headers = {'Content-Type': 'application/json'}
        data = {}
        data["text"] = text
        data['flag_hallo'] = 'false'
        data['chattts'] = False
        data['openvoice'] = "false"
        try:
            res = pooled_get("https://mailer.hertzai.com/get_image_by_id/{}".format(avatar_id))
            res = res.json()
            new_image_url = res["image_url"]
        except Exception:
            data['openvoice'] = "true"
            new_image_url = None
            res = {'voice_id': None}
        data["cartoon_image"] = "True"
        data["bg_url"] = 'http://stream.mcgroce.com/txt/examples_cartoon/roy_bg.jpg'
        data['vtoonify'] = "false"
        data["image_url"] = new_image_url
        data['im_crop'] = "false"
        data['remove_bg'] = "false"
        data['hd_video'] = "false"
        data['uid'] = request_id
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
            data["cartoon_image"] = False

        if res['voice_id'] != None:
            voice_sample = pooled_get(
                "{}/get_voice_sample_id/{}".format(database_url, res['voice_id']))
            voice_sample = voice_sample.json()
            data["audio_sample_url"] = voice_sample["voice_sample_url"]
            data['voice_id'] = res['voice_id']
        else:
            voice_sample = None
            data["audio_sample_url"] = None
            data['voice_id'] = None
        conv_id = save_conversation_db(text, user_id, prompt_id, database_url, request_id)
        data['conv_id'] = int(conv_id)  # Ensure it's an integer
        data['avatar_id'] = avatar_id_int  # Use the integer version
        data['timeout'] = timeout
        try:
            video_link = pooled_post("{}/video_generate_save".format(database_url),
                                       data=json.dumps(data), headers=headers, timeout=1)
        except Exception:
            pass
        if data['chattts'] or data['flag_hallo'] == "true":
            return f"Video Generation task added to queue with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"
        else:
            return f"Video Generation completed with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="get user's recent uploaded files")
    @log_tool_execution
    def get_user_uploaded_file() -> str:
        current_app.logger.info('INSIDE get_user_uploaded_file')
        if recent_file_id[user_id]:
            return f'Got user uploaded file the file_id is {recent_file_id[user_id]}'

        return 'No file uploaded from user'

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Get user's visual information to process somethings")
    @log_tool_execution
    def get_user_camera_inp(inp: Annotated[str, "The Question to check from visual context"]) -> str:
        request_id = 'Autogent_1234'
        current_app.logger.info('Using Vision to answer question')
        frame = get_frame(str(user_id))
        if frame is not None:
            image_path = f"output_images/{user_id}_{request_id}_call.jpg"
            # Ensure the directory exists
            directory = os.path.dirname(image_path)
            if not os.path.exists(directory):
                os.makedirs(directory)
            # Convert the frame (which is a NumPy array) to a PIL image
            image = Image.fromarray(frame)
            # Save the image
            image.save(image_path)
            from core.config_cache import get_vision_api
            url = get_vision_api() or "http://azurekong.hertzai.com:8000/minicpm/upload"
            payload = {
                'prompt': f'Instruction: Respond in second person point of view\ninput:-{inp}'}
            files = [
                ('file', ('call.jpg', open(image_path, 'rb'), 'image/jpeg'))
            ]
            headers = {}
            try:
                response = pooled_post(
                    url, headers=headers, data=payload, files=files)
                current_app.logger.info(response.text)
                response = response.text

                return response
            except Exception as e:
                current_app.logger.info('ERROR: Got error in visal QA')
                return 'failed to get visual context ask user to check if the camera is turned on'

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Get Chat history based on text & start & end date")
    @log_tool_execution
    def get_chat_history(text: Annotated[str, "Text related to which you want history"],
                         start: Annotated[str, "start date in format %Y-%m-%dT%H:%M:%S.%fZ"],
                         end: Annotated[str, "end date in format %Y-%m-%dT%H:%M:%S.%fZ"]) -> str:
        current_app.logger.info('INSIDE get_chat_history')
        return get_time_based_history(text, f'user_{user_id}', start, end)

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Search past camera and screen descriptions by keyword and time range. Use for visual history queries.")
    @log_tool_execution
    def search_visual_history(
        query: Annotated[str, "What to search for in visual/screen descriptions"],
        minutes_back: Annotated[int, "How many minutes back to search (default 30)"] = 30,
        channel: Annotated[str, "Which feed: 'camera', 'screen', or 'both' (default)"] = "both",
    ) -> str:
        """Search past camera/screen descriptions for visual history queries."""
        results = helper_fun.search_visual_history(user_id, query, mins=minutes_back, channel=channel)
        if results:
            return '\n'.join(results)
        return "No matching visual/screen descriptions found in the given time range."

    # --- Visual/audio trigger watcher (continuous monitoring) ---
    @assistant.register_for_execution()
    @helper.register_for_llm(
        api_style="function",
        description=(
            "Register a visual or audio trigger: continuously watch what the user is "
            "doing via camera or listen to what they say, and perform an action when a "
            "condition is met. Input: 'CONDITION: <what to watch for> | ACTION: <what to "
            "do> | TTL: <minutes>'. Example: 'CONDITION: user raises hand | ACTION: say "
            "hello | TTL: 30'."
        ))
    @log_tool_execution
    def register_visual_watcher(
        input_text: Annotated[str, "CONDITION: ... | ACTION: ... | TTL: minutes"]
    ) -> str:
        from core.safe_hartos_attr import safe_hartos_attr
        _handle = safe_hartos_attr('_handle_visual_watcher_tool')
        if _handle is None:
            return "Visual watcher unavailable: HARTOS still initialising."
        return _handle(input_text)

    # --- SimpleMem long-term memory tools ---
    if simplemem_store is not None:
        @assistant.register_for_execution()
        @helper.register_for_llm(api_style="function",
                                 description="Search long-term memory for past conversations, facts, and context using natural language query. More powerful than get_chat_history for finding relevant information.")
        @log_tool_execution
        def search_long_term_memory(
            query: Annotated[str, "Natural language query to search long-term memory"]
        ) -> str:
            """Search compressed long-term memory using semantic retrieval."""
            try:
                loop = get_or_create_event_loop()
                results = loop.run_until_complete(simplemem_store.search(query))
                if results:
                    return results[0].content
                return "No relevant memories found."
            except Exception as e:
                current_app.logger.info(f"SimpleMem search error: {e}")
                return "Memory search unavailable."

        @assistant.register_for_execution()
        @helper.register_for_llm(api_style="function",
                                 description="Save important facts or information to long-term memory for future retrieval across sessions.")
        @log_tool_execution
        def save_to_long_term_memory(
            content: Annotated[str, "The information/fact to remember long-term"],
            speaker: Annotated[str, "Who said this (e.g. 'User', 'Assistant', 'System')"] = "System"
        ) -> str:
            """Save important information to compressed long-term memory."""
            try:
                loop = get_or_create_event_loop()
                loop.run_until_complete(simplemem_store.add(content, {
                    "sender_name": speaker,
                    "user_id": user_id,
                    "prompt_id": prompt_id,
                }))
                # Dual-write to MemoryGraph (fire-and-forget)
                if memory_graph is not None:
                    try:
                        import threading as _t
                        _t.Thread(target=lambda: memory_graph.register(
                            content, {'memory_type': 'fact', 'source_agent': speaker, 'session_id': user_prompt, 'source': 'simplemem'},
                        ), daemon=True).start()
                    except Exception:
                        pass
                return "Saved to long-term memory."
            except Exception as e:
                current_app.logger.info(f"SimpleMem save error: {e}")
                return "Failed to save to long-term memory."

    # --- MemoryGraph provenance tools (remember, recall, backtrace) ---
    if memory_graph is not None:
        try:
            from integrations.channels.memory.agent_memory_tools import create_memory_tools, register_autogen_tools
            mem_tools = create_memory_tools(memory_graph, str(user_id), user_prompt)
            register_autogen_tools(mem_tools, assistant, helper)
            current_app.logger.info(f"MemoryGraph tools registered for {user_prompt}")
        except Exception as e:
            current_app.logger.warning(f"MemoryGraph tools registration failed: {e}")

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Creates time-based jobs using APScheduler to schedule jobs")
    @log_tool_execution
    def create_scheduled_jobs(cron_expression: Annotated[
        str, "Cron expression for scheduling. Example: '0 9 * * 1-5' (Runs at 9:00 AM, Monday to Friday)."],
                              job_description: Annotated[str, "Description of the job to be performed"]) -> str:
        current_app.logger.info('INSIDE create_scheduled_jobs')
        if not scheduler.running:
            scheduler.start()

        try:
            trigger = CronTrigger.from_crontab(cron_expression)
            job_id = f"job_{int(time.time())}"
            scheduler.add_job(execute_python_file, trigger=trigger, id=job_id,
                              args=[job_description, user_id, prompt_id, 0])
            current_app.logger.info('Successfully created scheduler job')
            return 'Successfully created scheduler job'
        except Exception as e:
            current_app.logger.info(f'Error in create_scheduled_jobs: {str(e)}')
            return f"Error creating scheduled job: {str(e)}"

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Sends a message/information to user. You can use this if you want to ask a question")
    @log_tool_execution
    def send_message_to_user(text: Annotated[str, "Text to send to the user"],
                             avatar_id: Annotated[Optional[str], "Unique identifier for the avatar"] = None,
                             response_type: Annotated[Optional[
                                 str], "Response mode: 'Realistic' (slower, better quality) or 'Realtime' (faster, lower quality)"] = 'Realtime') -> str:

        # Check if the message is directed to another agent and not to the user
        # Define a mapping of agent mentions that should never be sent to users
        agent_mentions = [
            "@statusverifier", "@status verifier", "@verification",
            "@helper", "@executor",
            "@StatusVerifier", "@Helper", "@Executor"
        ]

        # If the message contains any agent mention, don't send it to the user
        if any(mention in text.lower() for mention in agent_mentions):
            agent_found = next((mention for mention in agent_mentions if mention in text.lower()), None)
            current_app.logger.info(f'Message directed to agent ({agent_found}), not sending to user: {text[:50]}...')
            return f'Message directed to {agent_found} agent, not sending to user'



        current_app.logger.info('INSIDE send_message_to_user')
        current_app.logger.info(
                f'SENDING DATA 2 user with values text:{text}, avatar_id:{avatar_id}, response_type:{response_type}')
        random_num = random.randint(1000, 9999)

        # TODO add avatar_id and conv_id and response_type
        return send_message_to_user1(user_id, text, '', prompt_id)


    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Sends a presynthesized message/video/dialogue to user using conv_id from memory.")
    @log_tool_execution
    def send_presynthesized_video_to_user(
            conv_id: Annotated[str, "Conversation ID associated with the text from memory"]) -> str:
        current_app.logger.info('INSIDE send_presynthesized_video_to_user')
        current_app.logger.info(f'SENDING DATA 2 user with value: conv_id:{conv_id}.')
        return 'Message sent successfully to user'

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Sends a presynthesized message/video/dialogue to user using conv_id with a timer.")
    @log_tool_execution
    def send_message_in_seconds(text: Annotated[str, "text to send to user"],
                                delay: Annotated[int, "time to wait in seconds before sending text"],
                                conv_id: Annotated[
                                    Optional[int], "conv_id for this text if not available make it None"], ) -> str:
        current_app.logger.info('INSIDE send_message_in_seconds')
        current_app.logger.info(f'with text:{text}. and waiting time: {delay} conv_id: {conv_id}')
        run_time = datetime.fromtimestamp(time.time() + delay)
        scheduler.add_job(send_message_to_user1, 'date', run_date=run_time, args=[user_id, text, '', prompt_id])
        return 'Message scheduled successfully'

    # Expert agent consultation tool — domain-specific guidance on demand
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Consult a specialized domain expert for the current task")
    @log_tool_execution
    def consult_expert(task_description: Annotated[str, "Describe what expertise you need"]) -> str:
        """Consult a domain expert agent for specialized guidance on the current task."""
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

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Retrieve the user's visual camera input from the past specified minutes.")
    @log_tool_execution
    def get_user_camera_inp_by_mins(minutes: Annotated[
        int, "Time range (in minutes) for fetching the camera visual data. for e.g. 5 will get you last 5 mins data"]) -> str:
        current_app.logger.info('INSIDE get user camera inp by mins')
        current_app.logger.info(f'CHECKING FOR VIDEO FOR PAST {minutes} MINS')
        visual_context = helper_fun.get_visual_context(user_id, minutes)
        current_app.logger.info(f'GOT RESPONSE AS {visual_context}')
        if not visual_context:
            visual_context = 'User\'s camera is not on. no visual data'
        return visual_context

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Processes user-defined commands on a personal Windows or Android system.")
    @log_tool_execution
    async def execute_windows_or_android_command(
            instructions: Annotated[str, "Command in plain English to execute on the user's computer or mobile device"],
            os_to_control: Annotated[str, "The OS to control: 'windows', 'linux', 'macos', or 'android'"]) -> str:
        """
        Executes a command on any desktop (Windows/Linux/macOS) or Android device. Uses pyautogui for cross-platform GUI automation.
        """
        # Generate a unique key for this command
        command_key = f"windows_command_{user_id}_{prompt_id}"

        # Check if this command is already running
        with _active_tools_lock:
            if command_key in _active_tools and _active_tools[command_key]['active']:
                return f"A Windows command is already being executed in your device. Please wait for it to complete."

            # Mark this command as active
            _active_tools[command_key] = {
                'active': True,
                'started_at': time.time()
            }

        try:
            current_app.logger.info('INSIDE execute_windows_or_android_command')
            user_prompt = f'{user_id}_{prompt_id}'
            role_number, role = get_flow_number(user_id, prompt_id)

            import os
            import re
            import json

            prompts_dir = "prompts"
            current_app.logger.info(f"Checking for VLM files in directory: {os.path.abspath(prompts_dir)}")
            pattern = f"{prompt_id}_{role_number}_*_vlm_agent.json"
            current_app.logger.info(f"Looking for files matching pattern: {pattern}")


            existing_vlm_files = []
            for file in os.listdir(prompts_dir):
                if file.startswith(f"{prompt_id}_{role_number}_") and file.endswith("_vlm_agent.json"):
                    existing_vlm_files.append(file)

            current_app.logger.info(f"Found existing VLM files: {existing_vlm_files}")

            # Reload VLM agent files to ensure latest
            current_app.logger.info("Reloading VLM agnet files to ensure we have the latest")
            vlm_actions = load_vlm_agent_files(prompt_id, role_number)
            if vlm_actions:
                current_app.logger.info(f"Loaded {len(vlm_actions)} VLM agents")
                if user_prompt in recipes:
                    for vlm_action in vlm_actions:
                        action_id = vlm_action.get("action_id")
                        action_exists = False

                        for i, action in enumerate(recipes[user_prompt]['actions']):
                            if action.get("action_id") == action_id:
                                recipes[user_prompt]['actions'][i] = vlm_action
                                action_exists = True
                                break

                        if not action_exists:
                            recipes[user_prompt]['actions'].append(vlm_action)

                    # Update the recipes dictionary
                    final_recipe[prompt_id] = recipes[user_prompt]


            # Check if a matching recipe already exists in the loaded recipes
            simplified_instructions = ' '.join(instructions.lower().strip().split())

            def similar_instructions(instr1, instr2, threshold=0.8):
                words1 = set(instr1.lower().split())
                words2 = set(instr2.lower().split())
                if not words1 or not words2:
                    return False

                # Calculate word overlap
                overlap = len(words1.intersection(words2))
                similarity = overlap / (max(len(words1), len(words2)))
                current_app.logger.info(f"Comparing '{instr1}' with '{instr2}' - similarity: {similarity}")
                return similarity >= threshold

            # Using improved logic -- similar_instructions
            matching_recipe = None
            enhanced_instruction = None
            if user_prompt in recipes:
                for action in recipes[user_prompt]['actions']:
                    action_text = action.get('action', '')
                    if similar_instructions(instructions, action_text):
                        matching_recipe = action
                        current_app.logger.info(f"Found existing recipe for instruction: {action_text}")
                        break


            # Direct file check as backup
            current_action_id = 1
            if user_prompt in user_tasks and hasattr(user_tasks[user_prompt], 'current_action'):
                current_action_id = user_tasks[user_prompt].current_action

            direct_vlm_path = helper_fun.safe_prompt_path(prompt_id, role_number, current_action_id, 'vlm_agent')
            if os.path.exists(direct_vlm_path):
                current_app.logger.info(f"Found direct VLM file for current action: {direct_vlm_path}")
                try:
                    with open(direct_vlm_path, 'r') as f:
                        direct_recipe = json.load(f)
                    # Check if this recipe is relevant for the current instructions
                    if similar_instructions(instructions, direct_recipe.get('action', '')):
                        matching_recipe = direct_recipe
                except Exception as e:
                    current_app.logger.error(f"Error reading direct VLM file: {e}")

            # If we found a matching recipe, extract guidance steps
            enhanced_instruction = None
            if matching_recipe:
                current_app.logger.info(f"REUSING command - matched with: {matching_recipe.get('action', '')}")

                # Create an enhanced instruction that includes all the recipe steps

                # The recipe is an LLM *GUIDE*, NOT a deterministic macro: the proven
                # steps are injected as a hint the agent ADAPTS to the live screen (see
                # the "Adapt these steps..." line below). Do NOT "optimize" REUSE into a
                # code-only executor that skips the LLM — that trades intelligence for a
                # brittle screen-recorder that breaks the instant the world differs
                # (steward 2026-07-09). REUSE is cheaper because it skips
                # re-decomposition/exploration/re-verification, not because it drops the LLM.
                enhanced_instruction = f"{instructions}\n\n"
                enhanced_instruction += "Follow these steps from a previous successful execution:\n\n"

                for i, step in enumerate(matching_recipe.get('recipe', [])):
                    step_description = step.get('steps', '').strip()
                    if step_description:
                        enhanced_instruction += f"{i+1}. {step_description}\n"

                enhanced_instruction += "\nAdapt these steps to the current screen state as needed."
                current_app.logger.info(f"Created enhanced instruction with {len(matching_recipe.get('recipe', []))} steps")

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

            # Adding the enhanced_instruction if we have it
            if enhanced_instruction:
                crossbar_message['enhanced_instruction'] = enhanced_instruction
                current_app.logger.info(f"Added enhanced instruction to crossbar message")

            # Three-tier VLM execution (Tier 1: in-process, Tier 2: HTTP local)
            from integrations.vlm.vlm_adapter import execute_vlm_instruction
            start_time = time.time()
            response = execute_vlm_instruction(crossbar_message)

            if response is None:
                # Tier 3: Crossbar WAMP (central/regional or fallback)
                current_app.logger.info("VLM Tier 1/2 unavailable, falling back to Crossbar WAMP")
                topic = f'com.hertzai.hevolve.action.{user_id}'
                current_app.logger.info(f'calling {topic} for 5 second')
                response = await helper_fun.subscribe_and_return({'prompt_id': prompt_id}, topic, 2000)
                current_app.logger.info(f'Response from call of {topic}: {response}')
                if not response:
                    return 'Ask UserProxy to go to hevolve.ai login and start Nunba - Your Local HART Companion App'

                topic = 'com.hertzai.hevolve.action'
                current_app.logger.info(f'calling {topic} for 1800 seconds')
                response = await helper_fun.subscribe_and_return(crossbar_message, topic, 1800000)

            execution_time = time.time() - start_time
            current_app.logger.info(f'THIS IS RESPONSE type: {type(response)} value: {response}')

            # Transform the RPC response into the new format
            if response and response['status'] == 'success':
                if not matching_recipe:
                    try:
                        current_app.logger.info("Processing RPC response to create recipe format")

                        # Get current action ID
                        action_id = 1
                        if user_prompt in user_tasks and hasattr(user_tasks[user_prompt], 'current_action'):
                            action_id = user_tasks[user_prompt].current_action

                        # Determine file path with the action_id
                        role_number, role = get_flow_number(user_id, prompt_id)
                        action_id_to_use = action_id
                        base_path = helper_fun.safe_prompt_path(prompt_id, role_number, ext='')

                        # Import os here to ensure it's available
                        import os
                        import re
                        import json

                        # Check if a file with the current action_id exists, and increment if needed
                        while os.path.exists(f"{base_path}_{action_id_to_use}_vlm_agent.json"):
                            action_id_to_use += 1

                        vlm_agent_path = f"{base_path}_{action_id_to_use}_vlm_agent.json"

                        # Create directory if it doesn't exist
                        os.makedirs(os.path.dirname(vlm_agent_path), exist_ok=True)

                        # Function to clean technical details from text
                        def clean_text(text):
                            # Remove lines with technical details
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

                        # Handle different response format
                        if 'extracted_responses' in response:
                            # Extract the instruction and responses
                            instruction = response.get("instruction", instructions)
                            extracted_responses = response["extracted_responses"]

                            # Process all responses and create recipe steps
                            recipe_steps = []

                            for msg in extracted_responses:
                                msg_type = msg.get("type", "")
                                msg_content = msg.get("content", "")

                                # Clean the content
                                if msg_type == "analysis":
                                    cleaned_content = clean_text(msg_content)
                                    if cleaned_content.strip():  # Only add non-empty content
                                        recipe_steps.append({
                                            "steps": cleaned_content,
                                            "tool_name": "execute_windows_or_android_command",
                                            "agent_to_perform_this_action": "Helper"
                                        })
                                elif msg_type == "next_action":
                                    formatted_content = format_action_text(msg_content)
                                    if formatted_content.strip():  # Only add non-empty content
                                        recipe_steps.append({
                                            "steps": formatted_content,
                                            "tool_name": "execute_windows_or_android_command",
                                            "agent_to_perform_this_action": "Helper"
                                        })

                            # If no steps were created, add a default one
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
                                "fallback_action": "Perform a Google search using Internet Explorer",
                                "persona": persona,
                                "action_id": action_id_to_use,
                                "recipe": recipe_steps,
                                "can_perform_without_user_input": "no",
                                "scheduled_tasks": [],
                                "metadata": {
                                    "user_id": f"redacted <class 'int'>"
                                },
                                "time_took_to_complete": execution_time,
                                "actions_this_action_depends_on": []
                            }

                            # Save the recipe format with vlm_agent naming
                            with open(vlm_agent_path, 'w') as json_file:
                                json.dump(recipe_data, json_file, indent=4)

                            current_app.logger.info(f"Generated recipe data saved to {vlm_agent_path}")

                            try:
                                if os.path.exists(vlm_agent_path):
                                    file_size = os.path.getsize(vlm_agent_path)
                                    current_app.logger.info(f"Confirmed VLM file exists with size: {file_size} bytes")
                                    with open(vlm_agent_path, 'r') as f:
                                        test_read = json.load(f)
                                        current_app.logger.info(f"Successfully read back VLM file with action: {test_read.get('action', 'unknown')}")
                                else:
                                    current_app.logger.error(f"VLM file was not created at expected path: {vlm_agent_path}")
                            except Exception as e:
                                current_app.logger.error(f"Error verifying VLM file: {e}")

                            vlm_actions = load_vlm_agent_files(prompt_id, role_number)
                            if vlm_actions and user_prompt in recipes:
                                for vlm_action in vlm_actions:
                                    action_id = vlm_action.get("action_id")
                                    action_exists = False

                                    for i, action in enumerate(recipes[user_prompt]['actions']):
                                        if action.get("action_id") == action_id:
                                            recipes[user_prompt]['actions'][i] = vlm_action
                                            action_exists = True
                                            break
                                    if not action_exists:
                                        recipes[user_prompt]['actions'].append(vlm_action)

                                # Update the recipes dictionary
                                final_recipe[prompt_id] = recipes[user_prompt]
                            return f'Successfully ran the command in user\'s computer and created the VLM agent data at {vlm_agent_path}.'
                        else:
                            # If no structured data available, create a simple response
                            current_app.logger.error('No extracted_responses found in the response')
                            return 'Command executed but could not create VLM agent data due to missing response structure'
                    except Exception as e:
                        current_app.logger.error(f'Error transforming RPC response to recipe format: {e}')
                        current_app.logger.error(traceback.format_exc())
                        return f'Command executed but encountered an error while processing results: {str(e)}'

            if response and response['status'] == 'success':
                return 'Successfully ran the command in user\'s computer.'
            else:
                if 'message' in response and 'Failed to capture screenshot' in response['message']:
                    return 'I\'m unable to perform this action since the Hevolve A I Companion App is not running in your computer, Open the companion app & try again'
                else:
                    return 'Not able to perform this action now please try later'
        except Exception as e:
            error_message = traceback.format_exc()  # Capture full traceback
            current_app.logger.error(f"Error executing command:\n{error_message}")
            return {"error": e}
        finally:
            # Mark the command as complete
            with _active_tools_lock:
                if command_key in _active_tools:
                    _active_tools[command_key]['active'] = False


    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Get google search response")
    @log_tool_execution
    def google_search(text: Annotated[str, "Text which you want to search"]) -> str:
        current_app.logger.info('INSIDE google search')
        return helper_fun.top5_results(text)

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Signal that the user's request requires creating a new specialized AI agent. "
                                         "Use this when the user asks to create, build, set up, or deploy a new agent, "
                                         "or when the current agent's capabilities are insufficient for the task. "
                                         "Input should describe what the new agent should do. "
                                         "If the user wants autonomous creation, include 'autonomous' in the description.")
    @log_tool_execution
    def create_new_agent(description: Annotated[str, "Description of the agent to create"]) -> str:
        """Signal that a new agent needs to be created. Sets a thread-local flag
        that the /chat handler checks after chat_agent() returns."""
        current_app.logger.info(f'AUTOGEN create_new_agent tool called: {description}')
        lower = description.lower()
        autonomous = any(w in lower for w in [
            'autonomous', 'automatic', 'automatically', 'do it for me',
            'handle it', 'just create', 'auto',
        ])
        # Store in a module-level dict keyed by user_prompt so /chat can check it
        creation_signals[user_prompt] = {
            'description': description,
            'autonomous': autonomous,
        }
        if autonomous:
            return f"New agent creation initiated autonomously for: {description}. The system will handle all details automatically."
        return f"New agent creation initiated for: {description}. The system will guide through the creation process."

    time_agent = autogen.AssistantAgent(
        name='time_agent',
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=_is_terminate_msg,
        code_execution_config={"work_dir": get_coding_workspace_dir(), "use_docker": False},
        system_message="You are an helpful AI assistant used to perform time based tasks given to you. "
                       f"""You can refer below details to perform task:
            Actions: <actionsStart>{role_actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>

        """
                       f"When you want to communicate with {role} connect main agent using 'connect_time_main' tool."
                       "Tools Helper Agent can use [send_message_in_seconds,send_message_to_user,send_presynthesized_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, save_data_in_memory, search_long_term_memory and save_to_long_term_memory.]"
                       "if you have any task which is not doable by these tool check recipe first else create python code to do so"
                       "the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video."
                       f"IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: @user {response_format}"
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
            1. Follow the steps below to achieve the goal: {goal}.
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than {role},Executor,multi_role_agent.
            4. Tools you have [txt2img, img2txt, save_data_in_memory, get_data_from_memory, search_long_term_memory, save_to_long_term_memory, get_user_id, get_prompt_id, Generate_video, get_user_uploaded_file, get_user_camera_inp, get_chat_history, create_scheduled_jobs] if you have any task which is not doable by these tool check recipe first else create python code to do so
            5. Keep track of action and only go to next action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: @user {response_format}
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            Actions: <actionsStart>{role_actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>

            When writing code, always print the final response just before returning it.
        """,
        is_termination_msg=_is_terminate_msg,
    )
    executor1 = autogen.AssistantAgent(
        name="Executor",
        llm_config=llm_config,
        code_execution_config={"last_n_messages": 2, "work_dir": get_coding_workspace_dir(), "use_docker": False},
        system_message=f'''You are a executor agent. focused solely on creating, running & debugging code.
            Your responsibilities:
            1. Follow the steps below to achieve the goal: {goal}.
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than {role},Executor,multi_role_agent.
            4. Tools Helper Agent can use [send_message_in_seconds,send_message_to_user,send_presynthesized_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, save_data_in_memory, search_long_term_memory and save_to_long_term_memory]
            5. Keep track of action and only go to next action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: @user {response_format}
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            Actions: <actionsStart>{role_actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>

            Note: Your Working Directory is "{os.getcwd()}" - use this as the base path for all file operations. Always use absolute paths by joining with this directory,
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
        system_message="""You will send message from multiple different personas your, job is to ask those question to assistant agent
        if you think some text was intent to give to some other agent but i came to you to send the same message to user""",
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
    # See chat_instructor rationale at the recipe context_handling block
    # (line ~1255).  chat_instructor1 carries the same unbounded-buffer
    # risk in the time-based path.
    context_handling.add_to_agent(chat_instructor1)

    # --- Core tools for time_agent (defined once in core/agent_tools.py) ---
    from core.agent_tools import build_core_tool_closures, register_core_tools, register_dual
    # #509: reuse canonical log_tool_execution from core.tool_logging
    # (was passthrough no-op before — tools in reuse_recipe paths weren't
    # emitting publish_chat_stage UI status, weren't getting structured
    # error envelopes, weren't being str-coerced).
    from core.tool_logging import log_tool_execution as _log_tool_execution
    _tool_ctx = {
        'user_id': user_id, 'prompt_id': prompt_id,
        'agent_data': agent_data, 'helper_fun': helper_fun,
        'user_prompt': user_prompt, 'request_id_list': request_id_list,
        'recent_file_id': recent_file_id, 'scheduler': scheduler,
        'simplemem_store': simplemem_store,
        'memory_graph': memory_graph,
        'log_tool_execution': _log_tool_execution,
        'send_message_to_user1': send_message_to_user1,
        'retrieve_json': retrieve_json,
        'strip_json_values': strip_json_values,
        'save_conversation_db': save_conversation_db,
    }
    core_tools = build_core_tool_closures(_tool_ctx)
    register_core_tools(core_tools, helper1, time_agent)

    # Channel tools: send to channels, register channels, list status, get context
    try:
        from integrations.channels.agent_tools import register_channel_tools
        register_channel_tools(helper1, time_agent, _tool_ctx)
    except Exception as e:
        tool_logger.debug(f"Channel tools registration skipped: {e}")

    # Publish tools: stage a social post for a person to review and send.
    #
    # Registered here, beside the channel tools, and NOT behind
    # detect_goal_tags. That gate keyword-matches the prompt ('market',
    # 'campaign', 'viral'), so a family behind it is reachable only when the
    # wording happens to match -- which is how register_news_tools ended up
    # orphaned. An agent told "post this to Instagram" should be able to
    # without saying a magic word first.
    #
    # Media and news, the other two families a channel conversation should
    # reach. Both were wired nowhere on this path: create_recipe registers
    # media, /chat does not, so "make me an image" worked in one runtime and
    # not the other. News has been orphaned since it was written.
    try:
        from integrations.service_tools.media_agent import register_media_tools
        register_media_tools(helper1, time_agent)
    except Exception as e:
        tool_logger.debug(f"Media tools registration skipped: {e}")
    try:
        from integrations.agent_engine.news_tools import register_news_tools
        register_news_tools(helper1, time_agent, user_id)
    except Exception as e:
        tool_logger.debug(f"News tools registration skipped: {e}")

    # DELIBERATELY NOT REGISTERED HERE: self_build and remote_desktop.
    #
    # This runtime answers messages from Discord, Telegram, WhatsApp, Slack and
    # every other connected channel, so anything registered here is reachable
    # by anyone who can send the bot a message.
    #
    #   self_build     install_package, remove_package, apply_build
    #                  -> mutates the installation the agent runs on
    #   remote_desktop cast_to_tv, forward_peripheral, disconnect_remote
    #                  -> drives the operator's physical devices
    #
    # Those need an authenticated operator, not a chat turn. They stay on the
    # paths that already have one. finance, revenue, outreach, journey and mcp
    # are left out pending the same review rather than swept in because they
    # were next in the list.

    def connect_time_main(message: Annotated[str, "The message time agent want to send to main agent"]) -> str:
        message = f"Role: Time Agent\n Message: {message}"
        print(f'user_id {user_id}')
        user_prompt = f'{user_id}_{prompt_id}'
        assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group = user_agents[user_prompt]
        response = multi_role_agent.initiate_chat(manager, message=message, speaker_selection={"speaker": "assistant"},
                                                  clear_history=False)
        last_message = group_chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
            last_message = group_chat.messages[-2]
        # sending response to receiver agent
        send_message_to_user1(user_id, last_message, '', prompt_id)

        text = f'The Response from main Agent: {last_message}'
        result = time_user.initiate_chat(manager_1, message=text, speaker_selection={"speaker": "assistant"},
                                         clear_history=False)
        last_message = group_chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
            last_message = group_chat.messages[-2]
        send_message_to_user1(user_id, last_message, '', prompt_id)
        return 'Done'

    # #510: name override now matches func.__name__ AND the LLM prompt at
    # create_recipe.py:2823 ("connect_time_main").  Prior name override
    # ("Connect_to_main_agent") caused LLM to emit the wrong name → 404.
    register_dual(helper1, time_agent, connect_time_main,
                  "connect_time_main",
                  "Connects time agent to main assistant agent to perform actions which time agent cannot perform")

    visual_agent, visual_user, helper2, executor2, multi_role_agent2, verify2, chat_instructor2 = helper_fun.create_visual_agent(
        user_id, prompt_id)

    # --- Core tools for visual_agent (reuse same tool closures) ---
    register_core_tools(core_tools, helper2, visual_agent)

    # Channel tools for visual_agent too
    try:
        from integrations.channels.agent_tools import register_channel_tools
        register_channel_tools(helper2, visual_agent, _tool_ctx)
    except Exception:
        pass

    # MCP Integration: Load and register user-provided MCP server tools
    try:
        current_app.logger.info("Loading user-provided MCP servers...")
        num_servers = load_user_mcp_servers()

        if num_servers > 0:
            current_app.logger.info(f"Successfully loaded {num_servers} MCP servers")

            # Get all MCP tool functions
            mcp_tools = mcp_registry.get_all_tool_functions()
            current_app.logger.info(f"Discovered {len(mcp_tools)} MCP tools")

            # Register each MCP tool with the agents
            for tool_name, tool_func in mcp_tools.items():
                # Get tool definition for description
                tool_defs = mcp_registry.get_tool_definitions()
                tool_def = next((t for t in tool_defs if t['name'] == tool_name), None)

                if tool_def:
                    description = tool_def.get('description', f'MCP tool: {tool_name}')
                    register_dual(helper, assistant, tool_func, tool_name, description)
                    current_app.logger.info(f"Registered MCP tool: {tool_name}")
        else:
            current_app.logger.info("No MCP servers configured - continuing with default tools")
    except Exception as e:
        current_app.logger.warning(f"MCP integration error (non-critical): {e}")
        # Continue with default tools if MCP fails

    # Service Tools: Register HTTP microservice tools (Crawl4AI, AceStep, etc.)
    # Follows same pattern as MCP block above — register tools, get functions, wire to agents
    try:
        from integrations.service_tools import (
            service_tool_registry, Crawl4AITool, AceStepTool,
            SeoAuditTool, GhPrTool, TimeTool, CalculatorTool)

        Crawl4AITool.register()   # port 11235
        AceStepTool.register()    # port 8001
        SeoAuditTool.register()   # native in-process (no port)
        GhPrTool.register()       # native in-process (no port)
        TimeTool.register()       # native in-process (no port)
        CalculatorTool.register() # native in-process (no port)
        service_tool_registry.load_config()  # load any user-added tools from service_tools.json

        svc_tools = service_tool_registry.get_all_tool_functions()
        svc_defs = service_tool_registry.get_tool_definitions()

        for tool_name, tool_func in svc_tools.items():
            tool_def = next((d for d in svc_defs if d['name'] == tool_name), None)
            if tool_def:
                description = tool_def.get('description', f'Service tool: {tool_name}')
                register_dual(helper, assistant, tool_func, tool_name, description)
                current_app.logger.info(f"Registered service tool: {tool_name}")
    except Exception as e:
        current_app.logger.warning(f"Service tools integration error (non-critical): {e}")

    # HART Skills: Register ingested agent skills (Claude Code, Markdown, GitHub)
    try:
        from integrations.skills import skill_registry
        skill_funcs = skill_registry.get_autogen_tools()
        for func_name, func in skill_funcs.items():
            description = func.__doc__ or f"HART skill: {func_name}"
            register_dual(helper, assistant, func, func_name, description)
            current_app.logger.info(f"Registered HART skill: {func_name}")
    except Exception as e:
        current_app.logger.debug(f"HART skills integration skipped: {e}")

    # Internal Agent Communication: Register agents and their skills for in-process communication
    try:
        current_app.logger.info("Initializing Internal Agent Communication (skill-based delegation)...")

        # Define agent skills (same as in create_recipe.py for consistency)
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
            current_app.logger.info(f"Registered {agent_name} with {len(skills)} skills")

        # Add A2A tools (similar to create_recipe.py)
        @log_tool_execution
        def delegate_to_specialist(task: Annotated[str, "Description of the task to delegate"],
                                  required_skills: Annotated[List[str], "List of skills required"],
                                  context: Annotated[Optional[Dict], "Optional context"] = None) -> str:
            """Delegate a task to a specialist agent with full task_ledger tracking"""

            # Try to use TaskDelegationBridge for proper state management
            if user_prompt in user_delegation_bridges and user_prompt in user_tasks:
                bridge = user_delegation_bridges[user_prompt]
                action_tracker = user_tasks[user_prompt]

                try:
                    # Get current task ID from action tracker
                    current_action_idx = action_tracker.current_index if hasattr(action_tracker, 'current_index') else 0
                    current_task_id = f"action_{current_action_idx + 1}"

                    # Verify task exists in ledger
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
                            return json.dumps({
                                'success': True,
                                'delegation_id': delegation_id,
                                'message': f'Task delegated to {status["delegation"]["to_agent"]} with full tracking',
                                'parent_task_blocked': True,
                                'child_task_created': True,
                                'status': status
                            }, indent=2)

                except Exception as e:
                    current_app.logger.warning(f"Could not use TaskDelegationBridge: {e}. Falling back to standard delegation.")

            # Fallback to standard delegation (backward compatible)
            delegation_func = create_delegation_function('assistant')
            return delegation_func(task, required_skills, context)

        register_dual(helper, assistant, delegate_to_specialist,
                      "delegate_to_specialist",
                      "Delegate complex tasks to specialist agents based on required skills")

        @log_tool_execution
        def share_context_with_agents(context_key: Annotated[str, "Context identifier"],
                                      context_value: Annotated[str, "Context data as string"]) -> str:
            """Share context information with other agents"""
            sharing_func = create_context_sharing_function('assistant')
            result = sharing_func(context_key, context_value)
            # Persist to MemoryGraph (fire-and-forget)
            if memory_graph is not None:
                try:
                    import threading as _t
                    _t.Thread(target=lambda: memory_graph.register(
                        f"[SHARED] {context_key}: {json.dumps(context_value)[:200]}",
                        {'memory_type': 'insight', 'source_agent': 'assistant', 'session_id': user_prompt, 'shared_key': context_key},
                    ), daemon=True).start()
                except Exception:
                    pass
            return result

        register_dual(helper, assistant, share_context_with_agents,
                      "share_context_with_agents",
                      "Share context information with other agents")

        @log_tool_execution
        def get_shared_context(context_key: Annotated[str, "Context identifier"]) -> str:
            """Retrieve context information shared by other agents"""
            retrieval_func = create_context_retrieval_function()
            return retrieval_func(context_key)

        register_dual(helper, assistant, get_shared_context,
                      "get_shared_context",
                      "Retrieve context information shared by other agents")

        current_app.logger.info("Internal Agent Communication complete - agents can now delegate tasks and share context")

    except Exception as e:
        current_app.logger.warning(f"Internal Agent Communication error (non-critical): {e}")
        # Continue without internal communication if it fails

    # AP2 (Agent Protocol 2): Agentic Commerce - Payment workflows
    try:
        current_app.logger.info("Initializing AP2 (Agent Protocol 2) - Agentic Commerce...")

        # Get AP2 payment tools for this agent
        ap2_tools = get_ap2_tools_for_autogen('assistant')

        # Register payment tools — wrap with @log_tool_execution so payment
        # operations fire UI status emits + structured-error envelopes
        # (#510 followup — same observability fix applied in create_recipe).
        for tool_def in ap2_tools:
            tool_func = log_tool_execution(tool_def['function'])
            tool_name = tool_def['name']
            tool_desc = tool_def['description']
            register_dual(helper, assistant, tool_func, tool_name, tool_desc)
            current_app.logger.info(f"Registered AP2 payment tool: {tool_name}")

        current_app.logger.info("AP2 Agentic Commerce integration complete - agents can now handle payment workflows")

    except Exception as e:
        current_app.logger.warning(f"AP2 Agentic Commerce error (non-critical): {e}")
        # Continue without payment capabilities if AP2 fails

    # Goal-aware Tier 2 tool loading (progressive/hierarchical tool injection).
    # #510: mirrors create_recipe.py:1781-1803 — semantic detect_goal_tags(...)
    # instead of the previous prompt_id.startswith() check.  Earlier shape
    # missed 3 of 5 categories (self_build / outreach / sales), so a recipe
    # authored from "build me a sales pipeline" (matched via semantic tags
    # in create) would replay in reuse without the outreach + journey tools
    # → tool calls 404 → recipe step fails.
    try:
        from integrations.agent_engine.marketing_tools import detect_goal_tags, register_marketing_tools
        goal_tags = detect_goal_tags(goal or '')
        if 'marketing' in goal_tags:
            register_marketing_tools(helper, assistant, user_id)
            current_app.logger.info("Marketing tools loaded (Tier 2) for reuse agent")
        if 'ip_protection' in goal_tags:
            from integrations.agent_engine.ip_protection_tools import register_ip_protection_tools
            register_ip_protection_tools(helper, assistant, user_id)
            current_app.logger.info("IP protection tools loaded (Tier 2) for reuse agent")
        if 'self_build' in goal_tags:
            from integrations.agent_engine.self_build_tools import register_self_build_tools
            register_self_build_tools(helper, assistant, user_id)
            current_app.logger.info("Self-build tools loaded (Tier 2) for reuse agent")
        if 'outreach' in goal_tags:
            from integrations.agent_engine.outreach_crm_tools import register_outreach_tools
            register_outreach_tools(helper, assistant, user_id)
            current_app.logger.info("Outreach CRM tools loaded (Tier 2) for reuse agent")
        if 'sales' in goal_tags:
            from integrations.agent_engine.journey_engine import register_journey_tools
            register_journey_tools(helper, assistant, user_id)
            current_app.logger.info("Sales journey tools loaded (Tier 2) for reuse agent")
        if 'revenue' in goal_tags:
            # Revenue tools: get_api_revenue_stats + adjust_pricing.
            # Required by the `bootstrap_revenue_monitor` goal seed —
            # without these the revenue-monitor agent has no way to
            # observe commercial-API revenue and the flywheel can't
            # close the marketing/revenue loop.
            from integrations.agent_engine.revenue_tools import register_revenue_tools
            register_revenue_tools(helper, assistant, user_id)
            current_app.logger.info("Revenue tools loaded (Tier 2) for reuse agent")
        if 'news' in goal_tags:
            # News tools parity with create_recipe.py — a Herald (news) recipe
            # authored under the 'news' tag must replay with its feed tools,
            # else fetch_news_feeds / mark_news_for_web 404 and the daily
            # refresh step fails silently.
            from integrations.agent_engine.news_tools import register_news_tools
            register_news_tools(helper, assistant, user_id)
            current_app.logger.info("News tools loaded (Tier 2) for reuse agent")
    except Exception as e:
        # Same observability promotion as create_recipe.py — a failure
        # here strips the agent of goal-specific tools, agent talks
        # without acting.  Caught loud so future regressions surface.
        current_app.logger.warning(f"Goal-aware tool loading FAILED: {e}")

    assistant.description = 'Designed to handle specific tasks by interacting directly with other agents or the user. It acts as the primary orchestrator for task management and ensures tasks are completed efficiently'
    user_proxy.description = 'Acts as a user, performing tasks assigned by the Assistant Agent. It simulates user actions and provides results or feedback as required.'
    helper.description = 'this is a helper agent that calls tools, facilitates task completion & assists other agents it cal perform tools/function like [send_message_in_seconds,send_message_to_user,send_presynthesized_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, save_data_in_memory, search_long_term_memory and save_to_long_term_memory] calls and supporting backend processes. '
    multi_role_agent.description = 'Acts as an external agent with multi-functional capabilities. Note: This agent should never be directly invoked.'
    executor.description = 'A specialized agent responsible for executing code and handling response management. It ensures computational tasks are performed accurately and returns results effectively.'
    verify.description = 'this is a verify status agent. which will verify the status of current action.'

    time_agent.description = 'Designed to handle specific tasks by interacting directly with other agents or the user. It acts as the primary orchestrator for task management and ensures tasks are completed efficiently'
    time_user.description = 'Acts as a user, performing tasks assigned by the Assistant Agent. It simulates user actions and provides results or feedback as required.'
    helper1.description = 'this is a helper agent that calls tools, facilitates task completion & assists other agents it cal perform tools/function like [send_message_in_seconds,send_message_to_user,send_presynthesized_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, save_data_in_memory, search_long_term_memory and save_to_long_term_memory] calls and supporting backend processes. '
    executor1.description = 'A specialized agent responsible for executing code and handling response management. It ensures computational tasks are performed accurately and returns results effectively.'

    visual_agent.description = 'Designed to handle specific tasks by interacting directly with other agents or the user. It acts as the primary orchestrator for task management and ensures tasks are completed efficiently'
    visual_user.description = 'Acts as a user, performing tasks assigned by the Assistant Agent. It simulates user actions and provides results or feedback as required.'
    helper2.description = 'this is a helper agent that calls tools, facilitates task completion & assists other agents it cal perform tools/function like [send_message_in_seconds,send_message_to_user,send_presynthesized_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata, save_data_in_memory, search_long_term_memory and save_to_long_term_memory] calls and supporting backend processes. '
    executor2.description = 'A specialized agent responsible for executing code and handling response management. It ensures computational tasks are performed accurately and returns results effectively.'

    def state_transition(last_speaker, groupchat):
        messages = groupchat.messages
        # Wall-clock bound.  See _turn_deadline_exceeded: max_round alone
        # cannot bound a turn when each round is an unboundedly slow local
        # LLM call.  None => NoEligibleSpeaker => run_chat breaks cleanly.
        if _turn_deadline_exceeded(user_prompt):
            current_app.logger.error(
                f'[TURN-DEADLINE] ending group chat for {user_prompt} after '
                f'{os.environ.get("HEVOLVE_TURN_MAX_SECONDS", "150")}s — '
                f'{len(messages)} message(s) so far, last speaker '
                f'{getattr(last_speaker, "name", last_speaker)}. Terminating '
                f'so the caller gets an answer instead of waiting forever.')
            return None
        try:
            request_id = f'{request_id_list[user_prompt]}'
            # Check for specific agent mentions FIRST - this should take precedence
            content_lower = messages[-1]["content"].lower()

            # Define a mapping of agent mentions to their respective agent objects
            agent_mapping = {
                "@statusverifier": verify,
                "@status verifier": verify,
                "@verification": verify,
                "@helper": helper,
                "@executor": executor
            }

            # Check for any agent mentions and return the corresponding agent
            for mention, agent in agent_mapping.items():
                if mention.lower() in content_lower:
                    current_app.logger.info(f"Detected mention of {mention} - directing message to appropriate agent")
                    return agent

            # Check for messages directed to the user



            # Process JSON responses from StatusVerifier.
            #
            # This used to be `content.replace("'", '"')` followed by a
            # non-greedy `\{.*?\}` scrape.  Both were wrong:
            #   * the replace corrupted every reply containing an apostrophe --
            #     `{"reply": "Hello! It's great..."}` became `... "It"s great`,
            #     which fails with `Expecting ',' delimiter` at exactly the
            #     apostrophe's column;
            #   * the non-greedy pattern truncates nested JSON at the first '}'.
            # Together they made this block throw on essentially every natural
            # assistant reply (14 parse errors / 26 empty extractions in a
            # single turn), so `status == 'completed'` could never be observed,
            # chat_instructor was never selected, TERMINATE never fired, and the
            # reuse loop spun to exhaustion and returned ''.
            #
            # retrieve_json (helper.py) is the shared, hardened extractor:
            # unicode-quote normalisation, json_repair, ast.literal_eval and
            # regex fallbacks, plus an empty-input guard.
            try:
                last_json = retrieve_json(messages[-1]["content"])
                if not isinstance(last_json, dict):
                    last_json = None
                current_app.logger.info(f'Got Json as {1 if last_json else 0}')

                if last_json:
                    current_app.logger.info(f'last json as {last_json}')

                    if 'status' in last_json.keys() and str(last_json['status']).lower() == 'completed':
                        current_app.logger.info('GOT COMPLETED FOR ACTION in state_transition')
                        # Don't trust LLM's action_id — use known pipeline state
                        # The actual advancement happens in get_agent_response/chat_agent loops
                        return chat_instructor

                    # See should_delegate_route_to_helper for why this exists.
                    if should_delegate_route_to_helper(last_json, last_speaker.name):
                        current_app.logger.info(
                            f"[DELEGATE-ROUTE] delegate={last_json.get('delegate')!r} -> "
                            f"Helper (hive not yet peer-dispatched; using the local path)")
                        return helper

                    # Use known pipeline state, not LLM's claimed action_id
                    _known_aid = user_tasks[user_prompt].current_action
                    try:
                        if individual_recipe[_known_aid - 1]['can_perform_without_user_input'] == 'yes':
                            return assistant
                    except (IndexError, KeyError):
                        pass
            except Exception as e:
                current_app.logger.error(f'Got Error while getting json for current actionid: {e}')

            publish_intermediate_thoughts_to_user(last_speaker, messages)

            # Check for specific agent mentions
            if re.search(r"@statusverifier", messages[-1]["content"].lower()):
                current_app.logger.info("String contains @StatusVerifier returning StatusVerifier")
                return verify

            if re.search(r"@helper", messages[-1]["content"].lower()):
                current_app.logger.info("String contains @Helper returning Helper")
                return helper

            if re.search(r"@executor", messages[-1]["content"].lower()):
                current_app.logger.info("String contains @Executor returning Executor")
                return executor

            # Default speaker selection logic
            current_app.logger.info(
                f'Inside state_transition with message :10 {messages[-1]["content"][:10]} & last_speaker {last_speaker.name}')

            if (last_speaker.name == f"user_proxy_{user_id}" or
                    last_speaker.name == "multi_role_agent" or
                    last_speaker.name == "Helper" or
                    last_speaker.name == "Executor" or
                    last_speaker.name == "ChatInstructor"):
                return assistant

            # Check for user messages
            if 'message2userfinal' in messages[-1]["content"].lower():
                current_app.logger.info('GOT message2userfinal in message')
                # Check if this is directed to an agent and not the user
                # Use the same agent mapping as before
                agent_to_return = None
                for mention, agent in agent_mapping.items():
                    if mention in content_lower:
                        current_app.logger.info(
                            f"Message with message2userfinal also contains {mention} - directing to that agent")
                        agent_to_return = agent
                        break

                if agent_to_return:
                    return agent_to_return
                else:
                    # Canonical multi-strategy parse (json/repair/ast/regex)
                    # instead of naive re.search + json.loads, which fails on the
                    # exact malformed-JSON case retrieve_json survives (#95 Gate-1).
                    json_obj = retrieve_json(messages[-1]["content"])
                    if json_obj:
                        try:
                            send_message_to_user1(user_id, json_obj['message2userfinal'], '', prompt_id)
                        except Exception as e:
                            current_app.logger.error(f'Error sending message to user: {e}')

            if messages[-1]["role"] == 'function':
                current_app.logger.info('The last speaker was function returning assistant')
                return assistant

            if 'exitcode:' in messages[-1]["content"]:
                current_app.logger.info('Got exitcode in text returning assistant')
                return assistant

            if 'TERMINATE' in messages[-1]["content"].upper():
                current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
                return None

            return "auto"
        except Exception as e:
            current_app.logger.error(f"Error in state_transition: {e}")
            current_app.logger.error(traceback.format_exc())
            return "auto"

    def state_transition1(last_speaker, groupchat):
        current_app.logger.info('INSIDE TIMER STATE TRANSITION')
        messages = groupchat.messages
        if _turn_deadline_exceeded(user_prompt):
            current_app.logger.error(
                f'[TURN-DEADLINE] ending timer group chat for {user_prompt}')
            return None
        # visual_context = helper_fun.get_visual_context(user_id)
        # if visual_context:
        #     groupchat.messages.insert(-1,{'content':visual_context,'role':'user','name':'helper'})
        try:
            pattern = r'\{.*?\}'  # getting all json from text
            matches = re.findall(pattern, messages[-1]["content"], re.DOTALL)
            json_objects = [json.loads(match) for match in matches]
            current_app.logger.info(f'Got Json as {len(json_objects)}')
            if json_objects:
                last_json = json_objects[-1]
                current_app.logger.info(f'last json as {last_json}')
                if 'status' in last_json.keys() and last_json['status'].lower() == 'completed':
                    current_app.logger.info('GOT COMPLETED FOR ACTION in timer state_transition1')
                    time_actions[user_prompt].current_action += 1
                    return chat_instructor1

                # Use known pipeline state, not LLM's claimed action_id
                _timer_aid = time_actions[user_prompt].current_action
                try:
                    if final_recipe[prompt_id]['actions'][_timer_aid - 1]['can_perform_without_user_input'] == 'yes':
                        return time_agent
                except (IndexError, KeyError):
                    pass
        except Exception as e:
            current_app.logger.error(f'Got Error while getting json for current actionid: {e}')

        pattern3 = r"@statusverifier"
        if re.search(pattern3, messages[-1]["content"].lower()):
            current_app.logger.info("String contains @StatusVerifier returnig StatusVerifier")
            return verify1

        current_app.logger.info(
            f'Inside state_transition with message :10 {messages[-1]["content"][:10]} & last_speaker {last_speaker.name}')
        if last_speaker.name == f"user_proxy_{user_id}" or last_speaker.name == "multi_role_agent" or last_speaker.name == "Helper" or last_speaker.name == "Executor":
            return time_agent
        current_app.logger.info(f'Checking for @user or @user in message')
        if 'message2userfinal' in messages[-1]["content"].lower():
            current_app.logger.info('GOT @USER in message')
            json_obj = retrieve_json(messages[-1]["content"])  # canonical parse (#95)
            if json_obj:
                try:
                    current_app.logger.info('Sending user the message')
                    send_message_to_user1(user_id, json_obj['message2userfinal'], '', prompt_id)
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

    def state_transition2(last_speaker, groupchat):
        current_app.logger.info('INSIDE VISUAL STATE TRANSITION')
        messages = groupchat.messages
        if _turn_deadline_exceeded(user_prompt):
            current_app.logger.error(
                f'[TURN-DEADLINE] ending visual group chat for {user_prompt}')
            return None
        # visual_context = helper_fun.get_visual_context(user_id)
        # if visual_context:
        #     groupchat.messages.insert(-1,{'content':visual_context,'role':'user','name':'helper'})

        # current_app.logger.info('CHECKING FOR VIDEO FOR PAST 5MINS')
        # visual_context = helper_fun.get_visual_context(user_id)
        # current_app.logger.info(f'GOT RESPONSE AS {visual_context}')
        # if visual_context:
        #     groupchat.messages.insert(-2,{'content':visual_context,'role':'user','name':'helper'})
        # current_app.logger.info(f'{messages[-1]}'
        current_app.logger.info(f'Checking for @user or @user in message')
        if 'message2userfinal' in messages[-1]["content"].lower():
            current_app.logger.info('GOT @USER in message')
            json_obj = retrieve_json(messages[-1]["content"])  # canonical parse (#95)
            if json_obj:
                try:
                    current_app.logger.info('Sending user the message')
                    send_message_to_user1(user_id, json_obj['message2userfinal'], '', prompt_id)
                except Exception:
                    pass

        pattern3 = r"@statusverifier"
        if re.search(pattern3, messages[-1]["content"].lower()):
            current_app.logger.info("String contains @StatusVerifier returnig StatusVerifier")
            return verify2

        current_app.logger.info(
            f'Inside state_transition with message :10 {messages[-1]["content"][:10]} & last_speaker {last_speaker.name}')
        if last_speaker.name == f"UserProxy" or last_speaker.name == "multi_role_agent" or last_speaker.name == "Helper" or last_speaker.name == "Executor":
            return visual_agent

        if messages[-1]["role"] == 'function':
            current_app.logger.info('The last speaker was function returning assistant')
            return visual_agent
        if 'exitcode:' in messages[-1]["content"]:
            current_app.logger.info('Got exitcode in text returning assistant')
            return visual_agent
        if 'TERMINATE' in messages[-1]["content"].upper():
            current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
            # retrieve: action 1 -> action 2
            return None
        return "auto"

    def publish_intermediate_thoughts_to_user(last_speaker, messages):
        # Delegates to the module-level publisher in create_recipe so
        # the whole codebase has ONE thinking-prompts publisher — no
        # parallel Crossbar streams for the same agent-to-agent chats.
        # reuse_recipe's nested version used to also drop '@user'
        # messages; the shared publisher doesn't need that because
        # state_transition routes '@user' messages to the user path
        # before this function is called.
        try:
            if messages and '@user' in (messages[-1].get('content') or '').lower():
                return
        except Exception:
            pass
        from hartos.create_recipe import publish_agent_thought
        publish_agent_thought(last_speaker, messages, user_id)

    select_speaker_transforms = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=AUTOGEN_HISTORY_LIMIT, keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET, max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE, min_tokens=0),
            ToolMessageHandler(user_tasks=user_tasks, user_prompt=user_prompt),
        ]
    )

    group_chat = autogen.GroupChat(
        agents=[assistant, helper, user_proxy, multi_role_agent, executor, chat_instructor, verify],
        messages=[],
        max_round=10,
        select_speaker_prompt_template=f"Read the above conversation, select the next person from [Assistant, Helper, Executor, ChatInstructor, StatusVerifier, multi_role_agent & User] & only return the role as agent. Return User only if the previous message demands it",
        select_speaker_transform_messages=select_speaker_transforms,
        speaker_selection_method=state_transition,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice
        send_introductions=False,
        role_for_select_speaker_messages='user',
    )

    manager = autogen.GroupChatManager(
        groupchat=group_chat,
        llm_config={"cache_seed": None, "config_list": config_list}
    )


    group_chat_1 = autogen.GroupChat(
        agents=[time_agent, helper1, time_user, multi_role_agent1, executor1, chat_instructor1, verify1],
        messages=[],
        max_round=10,
        select_speaker_transform_messages=select_speaker_transforms,
        speaker_selection_method=state_transition1,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice
        send_introductions=False,
        role_for_select_speaker_messages='user',
    )

    manager_1 = autogen.GroupChatManager(
        groupchat=group_chat_1,
        llm_config={"cache_seed": None, "config_list": config_list}
    )

    group_chat_2 = autogen.GroupChat(
        agents=[visual_agent, helper2, visual_user, multi_role_agent2, executor2, chat_instructor2, verify2],
        messages=[],
        max_round=10,
        select_speaker_transform_messages=select_speaker_transforms,
        speaker_selection_method=state_transition2,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice
        send_introductions=False,
        role_for_select_speaker_messages='user',
    )

    manager_2 = autogen.GroupChatManager(
        groupchat=group_chat_2,
        llm_config={"cache_seed": None, "config_list": config_list}
    )

    visual_agent_group = {}
    visual_agent_group['visual_agent'] = visual_agent
    visual_agent_group['visual_user'] = visual_user
    visual_agent_group['helper2'] = helper2
    visual_agent_group['executor2'] = executor2
    visual_agent_group['multi_role_agent2'] = multi_role_agent2
    visual_agent_group['verify2'] = verify2
    visual_agent_group['chat_instructor2'] = chat_instructor2
    visual_agent_group['group_chat_2'] = group_chat_2
    visual_agent_group['manager_2'] = manager_2

    # Auto-ingest group_chat messages into SimpleMem + shared LangChain buffer
    # This ensures autogen writes go to the SAME PersistentChatHistory that LangChain reads,
    # eliminating redundant conversation storage and keeping both frameworks in sync.
    _shared_hook_factory = None
    try:
        from integrations.channels.memory.shared_history import create_autogen_history_hook
        _shared_hook_factory = create_autogen_history_hook(user_id, simplemem_store)
    except Exception:
        pass

    for gc in [group_chat, group_chat_1, group_chat_2]:
        _original_append = gc.messages.append

        def _make_hook(orig_append, store=simplemem_store, shared_factory=_shared_hook_factory):
            def _unified_ingest_hook(msg):
                orig_append(msg)
                # Skip seeded messages (already in buffer)
                if isinstance(msg, dict) and msg.get('_from_shared'):
                    return
                content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                if not content or len(content.strip()) <= 5 or content == 'TERMINATE':
                    return
                speaker = msg.get("name", "Agent") if isinstance(msg, dict) else "Agent"
                # SimpleMem ingest
                if store is not None:
                    try:
                        loop = get_or_create_event_loop()
                        loop.run_until_complete(store.add(content, {
                            "sender_name": speaker,
                            "user_id": user_id,
                            "prompt_id": prompt_id,
                        }))
                    except Exception:
                        pass
                # Shared PersistentChatHistory write-back (dedup-aware)
                if shared_factory is not None:
                    try:
                        from langchain_core.messages import HumanMessage, AIMessage
                        from integrations.channels.memory.shared_history import _get_persistent_history
                        hist = _get_persistent_history(user_id)
                        if hist:
                            role = msg.get("role", "assistant") if isinstance(msg, dict) else "assistant"
                            lc_msg = HumanMessage(content=content) if role == "user" else AIMessage(content=content)
                            # Dedup: skip if last buffer message has same content
                            last_msgs = hist.messages[-3:] if hist.messages else []
                            if not any(m.content == content for m in last_msgs):
                                from datetime import datetime
                                hist.add_message(lc_msg, metadata={
                                    'timestamp': datetime.now().isoformat(),
                                    'source': 'autogen',
                                })
                    except Exception:
                        pass
            return _unified_ingest_hook

        # Use wrapper list (plain list.append is read-only in Python)
        class _HookedList(list):
            def __init__(self, data, hook):
                super().__init__(data)
                self._hook = hook
            def append(self, msg):
                super().append(msg)
                try:
                    self._hook(msg)
                except Exception:
                    pass
        gc.messages = _HookedList(gc.messages, _make_hook(_original_append))

    # Auto-ingest group_chat messages into MemoryGraph (provenance tracking)
    if memory_graph is not None:
        for gc in [group_chat, group_chat_1, group_chat_2]:
            def _make_graph_hook(graph=memory_graph, session=user_prompt):
                def _graph_ingest_hook(msg):
                    try:
                        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                        speaker = msg.get("name", "Agent") if isinstance(msg, dict) else "Agent"
                        if content and len(content.strip()) > 5:
                            graph.register_conversation(speaker, content, session)
                    except Exception:
                        pass
                return _graph_ingest_hook
            if isinstance(gc.messages, _HookedList):
                # Already hooked — chain the graph hook into the existing one
                _existing_hook = gc.messages._hook
                _graph_h = _make_graph_hook()
                def _chained(msg, _eh=_existing_hook, _gh=_graph_h):
                    _eh(msg)
                    _gh(msg)
                gc.messages._hook = _chained
            else:
                class _GraphHookedList(list):
                    def __init__(self, data, hook):
                        super().__init__(data)
                        self._hook = hook
                    def append(self, msg):
                        super().append(msg)
                        try:
                            self._hook(msg)
                        except Exception:
                            pass
                gc.messages = _GraphHookedList(gc.messages, _make_graph_hook())

    # ── System Introspection Tools ─────────────────────────────────
    # Register self-awareness tools (GPU tier, active models, TTS
    # backend, boot-decision rationale) so the assistant can answer
    # "what model is running?" / "why is speculation off?" from live
    # admin-API state.  Caller = helper (the agent that CAN call
    # tools); executor = assistant (the agent that RUNS the call).
    try:
        from integrations.service_tools.system_introspect_tool import (
            register_autogen as _register_introspect,
        )
        _n = _register_introspect(helper, assistant)
        current_app.logger.info(
            f"Registered {_n} system-introspect tool(s) for self-awareness",
        )
    except Exception as _ie:
        current_app.logger.warning(
            f"system_introspect autogen registration failed: {_ie}",
        )

    # THE FIX for the empty-GroupChat defect.  Must run AFTER every
    # `.messages` reassignment above (the MemoryGraph/provenance hooks),
    # because each one orphans the list autogen's registered run_chat config
    # still points at.  See _resync_manager_reply_config for the full
    # mechanism.
    try:
        _resynced = 0
        for _mgr, _gc in ((manager, group_chat),
                          (manager_1, group_chat_1),
                          (manager_2, group_chat_2)):
            _resynced += _resync_manager_reply_config(_mgr, _gc)
        current_app.logger.info(
            f'[GC-RESYNC] re-pointed {_resynced} reply-config(s) at the live '
            f'group-chat message list(s)')
    except Exception as _rs_err:
        current_app.logger.error(f'[GC-RESYNC] failed: {_rs_err}')

    return assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group


# The dispatcher prompt (speculative_dispatcher.py ~1107) shows the model the
# exact JSON shape to emit, using angle-bracketed placeholders --
# "<your short reply to the user, 1-3 sentences>".  A 4B model sometimes
# parrots that skeleton back instead of filling it in.  Observed live on
# 2026-08-12, on the first turn after a cold start.
#
# An echo is well-formed JSON with is_casual set, so it sails through every
# check in _extract_conversational_reply and gets delivered to the user
# verbatim.  On 08-12 it was caught only by luck: the echoed `delegate`
# parsed to the garbage string 'none" OR "local" OR "hive', which happened
# to fail the delegate check.  A cleaner echo would have shipped.
_TEMPLATE_PLACEHOLDER_RE = re.compile(r'^<[^<>]{2,}>$')


def _is_template_echo(reply: str) -> bool:
    """True when ``reply`` is the prompt's own placeholder, not a real answer.

    Deliberately narrow: only a reply that is *wholly* one angle-bracketed
    placeholder counts.  A genuine reply that merely contains angle brackets
    (code, math, markup) is untouched.
    """
    return bool(_TEMPLATE_PLACEHOLDER_RE.match((reply or '').strip()))


def _salvage_assistant_reply(messages) -> Optional[str]:
    """Return the assistant's own latest reply, whatever kind of turn this was.

    Used ONLY on the failure path, where the alternative is the generic
    "I couldn't complete that request just now." apology.

    Why this exists
    ---------------
    Observed on a real Discord channel 2026-08-12: a user asked for help with
    coding.  The model classified it correctly and confidently --
    ``is_casual: true, delegate: 'local', confidence: 0.9`` -- and wrote a real
    reply.  But ``delegate: 'local'`` is read by nothing for routing, so
    StatusVerifier never spoke, TERMINATE never fired, the turn hit the wall
    clock bound, and the user got the apology while the model's actual answer
    sat unused in group_chat.messages.

    That makes EVERY substantive request fail -- coding, research, anything
    needing tools -- while only pure chit-chat (delegate 'none') works.

    This does not fix routing: a request that genuinely needs a tool still does
    not get the tool run, so the answer is the model's direct response rather
    than a tool-backed one.  It is strictly better than an apology, and it
    touches ONLY the path that was already returning failure -- a turn that
    completes normally never reaches here.

    Unlike _extract_conversational_reply this ignores is_casual/delegate/
    is_create_agent (we are past the point where routing could have helped) but
    keeps the template-echo guard, so the prompt skeleton is never surfaced.

    Two message shapes reach this function, and both are salvageable:
    structured JSON (``{"reply": ..., "delegate": ...}``, the draft/
    classifier-style format) and plain prose (the recipe/task-execution
    GroupChat's own persona replies, e.g. "auto.agent8888").  Only the JSON
    shape was handled until 2026-08-19 -- confirmed live on a real REUSE-loop
    abort (agent 8888, "what is the weather in chennai today"): the model
    produced a good, honestly-hedged answer twice in plain prose
    ("I can't access real-time data right now... likely warm and humid...")
    and the loop still discarded it for the generic apology, because
    retrieve_json() cannot parse prose and the function silently skipped
    every candidate. Plain prose is tried as a fallback ONLY after the JSON
    shape fails for a given message, so a message that legitimately contains
    a JSON envelope is still read from its 'reply' field, never its raw
    (JSON-shaped) text.

    Unverified-claim caveat (2026-08-19): confirmed live on a second
    REUSE-loop abort, same agent, "what is the latest news in chennai
    politics today" -- the Assistant stated a specific, wrong headline
    ("CM E. Vaiko's health scare") BEFORE any tool had run at all. A
    subtask then dispatched a real tool (crawl4ai_crawl, a genuine fetch of
    timesofindia.indiatimes.com that succeeded) trying to verify/replace it,
    but the turn deadline hit before the Assistant got another turn to
    synthesize an answer from that real data -- so the premature, unverified
    guess is what survived to be salvaged. If a tool call happened AFTER the
    candidate reply with no later Assistant message answering from its
    result, that reply was never confirmed -- say so, rather than presenting
    a guess as settled fact. This is strictly a phrasing safeguard: it does
    not change WHICH reply is chosen, only whether it is honestly labelled.
    """
    try:
        _msgs = list(messages or [])
        for _idx in range(len(_msgs) - 1, -1, -1):
            _msg = _msgs[_idx]
            if not isinstance(_msg, dict):
                continue
            if _msg.get('name') not in ('Assistant', 'assistant'):
                continue
            # A tool's OWN response is attributed name="Assistant" too (the
            # tool call was Assistant's), role="tool" -- confirmed live in a
            # real transcript. That's tool output, never an Assistant reply;
            # without this check a raw scraped-page dump could be salvaged
            # as if the model had said it.
            if _msg.get('role') == 'tool':
                continue
            _content = _msg.get('content') or ''
            _obj = retrieve_json(_content)
            if isinstance(_obj, dict):
                _reply = _obj.get('reply')
            elif isinstance(_content, str):
                _reply = _content
            else:
                continue
            if not isinstance(_reply, str) or not _reply.strip():
                continue
            if _is_template_echo(_reply):
                continue      # keep scanning: an echo is not an answer
            _reply = _reply.strip()
            if _tool_activity_after(_msgs, _idx):
                _reply += (
                    "\n\n(Note: I couldn't fully verify this before running "
                    "out of time -- treat it as a best guess, not a "
                    "confirmed fact.)")
            return _reply
    except Exception:
        return None
    return None


def _tool_activity_after(messages: list, index: int) -> bool:
    """True if any tool call/response appears strictly after ``index`` in
    ``messages`` -- evidence a verification attempt started but the
    candidate reply at ``index`` predates (and was never updated by) it."""
    for _later in messages[index + 1:]:
        if not isinstance(_later, dict):
            continue
        if _later.get('role') == 'tool' or _later.get('tool_calls') or \
                _later.get('tool_responses'):
            return True
    return False


def should_delegate_route_to_helper(last_json, last_speaker_name: str) -> bool:
    """True if state_transition should hand this turn to Helper.

    `delegate` routing (2026-08-24): the assistant sets is_casual/delegate on
    every response (see _extract_conversational_reply), but until this change
    nothing read `delegate` in state_transition, so a substantive request
    classified 'local'/'hive' had nowhere to go and spun to the turn
    deadline. Helper already has the registered tools
    (delegate_to_specialist, service-tool registry) and the existing
    "last_speaker == helper -> assistant" edge already hands control back to
    Assistant afterwards, so this is the one missing edge, not new machinery.

    'hive' has no peer/federation dispatch wired yet -- routed through the
    same local Helper path for now rather than left unroutable; this is a
    deliberate interim scoping choice, not a claim that local and hive should
    stay identical long-term.

    A pure predicate (no autogen agent objects) so it can be unit-tested
    directly -- state_transition itself is a closure that needs a live
    autogen/LLM setup to construct, same reasoning as
    lifecycle_hooks.is_recipe_creation_request for create_recipe's
    state_transition.
    """
    if not isinstance(last_json, dict):
        return False
    delegate = str(last_json.get('delegate') or '').strip().lower()
    if delegate not in ('local', 'hive'):
        return False
    return last_speaker_name not in ('Helper', 'Executor', 'ChatInstructor')


def _extract_conversational_reply(messages) -> Optional[str]:
    """Return the assistant's reply for a purely conversational turn, else None.

    The reuse loop is built for TASK execution: it only returns an answer once
    StatusVerifier emits ``status: "completed"``, which makes state_transition
    pick ChatInstructor, which finally emits TERMINATE.  A greeting never goes
    near that machinery -- StatusVerifier never speaks -- so the loop spun to
    exhaustion and leaked an internal instruction string
    ("You should complete this task independently...") as the user-facing
    answer, even though the model had already produced a perfectly good reply.

    The assistant already tells us which kind of turn this is: it sets
    ``is_casual`` and ``delegate`` on every response.  Nothing read those flags
    (zero references to is_casual in this module before this change).

    Deliberately conservative -- a turn is only short-circuited when the
    assistant itself marked it casual, delegated to nobody, is not creating an
    agent, and supplied a non-empty reply.  Anything task-shaped falls through
    to the normal loop untouched.
    """
    try:
        for _msg in reversed(list(messages or [])):
            if not isinstance(_msg, dict):
                continue
            if _msg.get('name') not in ('Assistant', 'assistant'):
                continue
            _obj = retrieve_json(_msg.get('content') or '')
            if not isinstance(_obj, dict):
                continue
            _reply = _obj.get('reply')
            if not isinstance(_reply, str) or not _reply.strip():
                continue
            if _is_template_echo(_reply):
                # This turn's assistant message is the prompt skeleton, not an
                # answer.  Do NOT keep scanning backwards -- an older message
                # belongs to a previous turn, and returning it would answer the
                # user's new question with a stale reply.  Fall through to the
                # task loop, which is the conservative path.
                try:
                    current_app.logger.warning(
                        f'[TEMPLATE-ECHO] assistant parroted the prompt '
                        f'placeholder instead of answering '
                        f'({_reply.strip()[:80]!r}); not returning it')
                except Exception:
                    pass
                return None
            _delegate = str(_obj.get('delegate') or 'none').strip().lower()
            if not _obj.get('is_casual'):
                return None          # task-shaped: let the normal loop run
            if _obj.get('is_create_agent'):
                return None          # creation needs its own flow, not this
            if _delegate in ('', 'none', 'null'):
                return _reply.strip()

            # delegate is 'local' or 'hive' -- the model asked for a specialist.
            #
            # There is nowhere to send it.  state_transition routes only on a
            # literal "@statusverifier"-style mention in the message text;
            # `delegate` is read by NOTHING for routing.  So a turn that reaches
            # here cannot ever complete: StatusVerifier never speaks, no
            # status:"completed" appears, TERMINATE never fires, and the turn
            # burns the full HEVOLVE_TURN_MAX_SECONDS before returning the
            # generic apology -- with the model's own good answer sitting
            # unused in group_chat.messages the whole time.
            #
            # Measured on a real Discord channel 2026-08-12: "i need a help in
            # coding" was classified is_casual=true, delegate='local',
            # confidence=0.9, and the user waited ~238s for
            # "I couldn't complete that request just now."  Every substantive
            # request behaves that way; only chit-chat (delegate 'none') works.
            #
            # Until delegate routing exists, entering the loop is strictly
            # worse than answering: same information, 150s later, phrased as a
            # failure.  So answer directly.  This is a DELIBERATE interim
            # choice, not a claim that routing is unnecessary -- flip
            # HEVOLVE_DELEGATE_ROUTING=1 the moment real routing lands and this
            # falls back to the loop untouched.
            if os.environ.get('HEVOLVE_DELEGATE_ROUTING', '0') == '1':
                return None          # real routing exists: let it run
            try:
                current_app.logger.warning(
                    f'[UNROUTABLE-DELEGATE] delegate={_delegate!r} has no '
                    f'destination (delegate routing not implemented); '
                    f'returning the assistant\'s own reply instead of spinning '
                    f'to the turn deadline and apologising')
            except Exception:
                pass
            return _reply.strip()
    except Exception:
        return None
    return None


def _giveup_current_reuse_action(user_prompt: str, reason: str) -> None:
    """Mark the in-flight ledger action GAVE_UP (-> ledger FAILED, #139) when
    the reuse loop abandons a turn without ever reaching TERMINATE.

    Without this, the action's ledger task stays PENDING/IN_PROGRESS forever
    -- create_ledger_from_actions' _find_resumable_session() has no age/TTL
    check, so it treats ANY non-terminal task as "in-flight" no matter how
    stale, and silently reattaches the next unrelated message for this
    (user, prompt) pair to the abandoned session's stale goal/description
    instead of starting fresh. This is the mechanism behind the
    weeks-long "agent 8888 has weird pre-existing state" pattern -- create_
    recipe.py's flow-complete path already force-abandons stalled actions
    via ActionState.GAVE_UP, but the reuse loop's own give-up paths
    (turn/loop-deadline abort, budget exhaustion, empty groupchat) never
    called it. GAVE_UP is deliberately re-openable (-> ASSIGNED) so a later
    real retry is not blocked by this.

    Best-effort: must never raise into a give-up path that is already
    trying to return an honest reply to the user.
    """
    try:
        if user_prompt not in user_tasks:
            return
        action_id = user_tasks[user_prompt].current_action
        safe_set_state(user_prompt, action_id, ActionState.GAVE_UP, reason)
    except Exception as e:
        try:
            current_app.logger.error(f"[GAVE-UP] failed to mark {user_prompt} action GAVE_UP: {e}")
        except Exception:
            pass


def get_agent_response(assistant: "autogen.AssistantAgent", chat_instructor: "autogen.UserProxyAgent",
                       helper: "autogen.AssistantAgent", user_proxy: "autogen.UserProxyAgent",
                       manager: "autogen.GroupChatManager", group_chat: "autogen.GroupChat", message: str, role: str,
                       user_id: int, prompt_id: int, request_id: str) -> str:
    """Get a single response from the agent for the given message."""
    user_prompt = f'{user_id}_{prompt_id}'
    # Arm the wall-clock bound BEFORE initiate_chat -- that call is where the
    # runaway lives, and the reuse loop's own bounds below only start counting
    # once it has already returned.  Cleared in the finally so a cached agent's
    # selector can never inherit a stale deadline from a previous turn.
    #
    # only_if_unset: /chat already armed this thread at the request entry, and
    # that clock is the one that matches what the user waits.  Re-arming here
    # would restart it and give back the ~84s of pre-turn routing the entry
    # point arming exists to cover.  This call still matters for callers that
    # reach get_agent_response without going through /chat.
    _owns_deadline = getattr(_turn_deadline_state, 'deadline', None) is None
    _begin_turn_deadline(user_prompt, only_if_unset=True)
    try:

        result = user_proxy.initiate_chat(manager, message=message, speaker_selection={"speaker": "assistant"},
                                          clear_history=False)

        # Conversational turns (greetings, small talk) are answered directly.
        # They have no action to verify, so the task loop below can never
        # terminate for them.  See _extract_conversational_reply.
        _conv_reply = _extract_conversational_reply(group_chat.messages)
        if _conv_reply:
            current_app.logger.info(
                f'[CONVERSATIONAL] returning assistant reply directly for '
                f'{user_prompt} ({len(_conv_reply)} chars)')
            return _conv_reply

        # Hard bounds on the task loop.
        #
        # The loop's only text-returning exit needs ChatInstructor to emit
        # TERMINATE, which needs state_transition to see status="completed",
        # which only StatusVerifier produces -- and StatusVerifier is selected
        # only by a literal "@statusverifier" in the previous message.  The
        # assistant emits structured JSON with a `delegate` field instead, which
        # nothing reads, so that handshake never happens for task-shaped turns.
        #
        # Unbounded, such a turn does not merely fail to answer: it spins
        # forever, holding a channel-agent worker and hammering the LLM.
        # Observed live 2026-08-10 -- two task requests left 3 of 4 workers
        # permanently occupied and every later message on the channel timed out.
        # One bad request took the whole channel down until restart.
        #
        # These bounds do not fix task execution; they stop one request
        # poisoning the channel.  Remove/raise once the delegate routing lands.
        _loop_max_iters = int(os.environ.get('HEVOLVE_REUSE_LOOP_MAX_ITERS', '25'))
        _loop_deadline = time.time() + float(
            os.environ.get('HEVOLVE_REUSE_LOOP_MAX_SECONDS', '300'))

        count = 0
        while True:
            current_app.logger.info('inside reuse while1')

            # The turn deadline is checked here too, not just in the speaker
            # selector.  Without it a turn bounded at HEVOLVE_TURN_MAX_SECONDS
            # inside initiate_chat would simply hand off to a fresh
            # HEVOLVE_REUSE_LOOP_MAX_SECONDS budget here -- 150s + 300s = 450s,
            # which is not a bound anyone asked for.  One deadline covers the
            # whole turn; the loop's own counters remain as a backstop.
            _hit_iters = count >= _loop_max_iters
            _hit_loop_clock = time.time() > _loop_deadline
            _hit_turn_clock = _turn_deadline_exceeded(user_prompt)
            if _hit_iters or _hit_loop_clock or _hit_turn_clock:
                # Name the bound that actually fired.  The original message
                # reported only the reuse loop's own elapsed, so a turn killed
                # by the turn deadline logged "after 0 iteration(s) / 0s" --
                # true of this loop, and completely misleading about where the
                # time went (observed 2026-08-12).  A diagnostic that misreports
                # which limit tripped is how the earlier defects stayed hidden.
                _which = ('turn-deadline' if _hit_turn_clock
                          else 'loop-seconds' if _hit_loop_clock
                          else 'loop-iterations')
                _loop_elapsed = int(time.time() - (_loop_deadline - float(
                    os.environ.get('HEVOLVE_REUSE_LOOP_MAX_SECONDS', '300'))))
                current_app.logger.error(
                    f'[REUSE-LOOP-ABORT] giving up for {user_prompt} — bound '
                    f'hit: {_which}. {count} loop iteration(s), {_loop_elapsed}s '
                    f'in this loop (turn deadline = '
                    f'{os.environ.get("HEVOLVE_TURN_MAX_SECONDS", "150")}s, '
                    f'measured from before initiate_chat, so most of a '
                    f'turn-deadline abort was spent inside autogen, not here). '
                    f'TERMINATE never fired (StatusVerifier never spoke). '
                    f'Returning an honest failure instead of spinning.')
                # Before apologising, check whether the model already answered.
                # It usually has: the turn fails for lack of ROUTING, not for
                # lack of an answer, and that answer is sitting in
                # group_chat.messages.  Sending the apology on top of a real
                # reply is the worst of both -- the user waits the full bound
                # AND is told nothing could be done.
                _giveup_current_reuse_action(user_prompt, f'[REUSE-LOOP-ABORT] bound hit: {_which}')
                _salvaged = _salvage_assistant_reply(group_chat.messages)
                if _salvaged:
                    current_app.logger.warning(
                        f'[SALVAGED-REPLY] returning the assistant\'s own '
                        f'answer for {user_prompt} ({len(_salvaged)} chars) '
                        f'instead of the generic failure — the turn could not '
                        f'complete, but the model did produce a reply')
                    return _salvaged
                return ("I couldn't complete that request just now. "
                        "Could you try rephrasing it?")

            # === LEDGER v2.0: Heartbeat + Budget/SLA using KNOWN state ===
            _reuse_current_action = user_tasks[user_prompt].current_action
            _reuse_ledger = user_ledgers.get(user_prompt)
            if _reuse_ledger:
                _reuse_task_id = f"action_{_reuse_current_action}"
                _reuse_task = _reuse_ledger.tasks.get(_reuse_task_id)
                if _reuse_task:
                    _reuse_task.heartbeat()
                    if _reuse_task.is_budget_exhausted():
                        current_app.logger.warning(f"[BUDGET] Task {_reuse_task_id} budget exhausted in reuse loop")
                        _giveup_current_reuse_action(user_prompt, f'[BUDGET] {_reuse_task_id} budget exhausted')
                        break
                    if _reuse_task.is_sla_breached() and not _reuse_task.sla_breached:
                        _reuse_task.mark_sla_breached()
                        current_app.logger.warning(f"[SLA] Task {_reuse_task_id} SLA breached in reuse loop")

            # An empty group chat here used to raise IndexError straight out of
            # the loop, so the whole turn was answered with the raw exception
            # text ("Error getting response: list index out of range") on every
            # channel.  Break instead and let the post-loop fallback answer.
            # The diagnostic is deliberately loud: it records whether the
            # manager is even holding the same GroupChat object we were handed,
            # which is the one thing the traceback alone could never tell us.
            # (main independently added an `and group_chat.messages` guard on
            # the line below this block for the same symptom, #725 "cause not
            # established" — moot here since this break already prevents that
            # line from ever running on an empty groupchat.)
            if not group_chat.messages:
                try:
                    _mgr_chat = getattr(manager, 'groupchat', None)
                    _same = _mgr_chat is group_chat
                    _mgr_len = len(_mgr_chat.messages) if _mgr_chat is not None else -1
                except Exception:
                    _same, _mgr_len = 'unknown', -1
                _gcm = group_chat.messages
                _mgr_list = getattr(_mgr_chat, 'messages', None)
                current_app.logger.error(
                    f'[EMPTY-GROUPCHAT] group_chat.messages empty in reuse loop '
                    f'(user_prompt={user_prompt}, iteration={count}, '
                    f'manager_holds_same_object={_same}, '
                    f'len(manager.groupchat.messages)={_mgr_len}, '
                    f'id(list)={id(_gcm)}, id(group_chat)={id(group_chat)}, '
                    # If these two ids ever differ again, the run_chat reply
                    # config has been orphaned from the live list -- i.e. the
                    # defect _resync_manager_reply_config exists to prevent has
                    # regressed.  That comparison is the one worth keeping.
                    f'id(manager.groupchat.messages)='
                    f'{id(_mgr_list) if _mgr_list is not None else "n/a"})')
                break

            if group_chat.messages[-1]['name'] == 'ChatInstructor' and group_chat.messages[-1]['content'] == 'TERMINATE':
                current_app.logger.info(
                    f"group_chat.messages[-2]['content'] {group_chat.messages[-2]['content'][:10]}..")
                try:
                    try:
                        json_obj = json.loads(group_chat.messages[-2]["content"])
                    except (json.JSONDecodeError, ValueError):
                        json_obj = ast.literal_eval(group_chat.messages[-2]["content"])
                    current_app.logger.info(f'got json object {json_obj}')
                    if json_obj['status'].lower() == 'completed':
                        _llm_action_id = int(json_obj.get("action_id", _reuse_current_action))
                        if _llm_action_id != _reuse_current_action:
                            current_app.logger.warning(
                                f"[HALLUCINATION?] LLM claims action_id={_llm_action_id} "
                                f"but pipeline assigned {_reuse_current_action}")
                        _next, _ok, _done = _advance_reuse_action(user_prompt, _reuse_current_action, "reuse-w1", prompt_id)
                        if not _ok:
                            return _finish_reuse_recipe(user_id, prompt_id, json_obj) if _done else ''
                        user_message = _build_reuse_action_message(user_prompt, _next)
                        chat_instructor.initiate_chat(recipient=manager, message=user_message, clear_history=False,
                                                      silent=False)
                        continue
                except IndexError:
                    current_app.logger.info("Completed ALL ACTIONS")
                    return ''
                except Exception:
                    try:
                        json_obj = retrieve_json(group_chat.messages[-2]["content"])  # canonical parse (#95)
                        if json_obj:
                            current_app.logger.info(f'got json object {json_obj}')
                            if json_obj['status'].lower() == 'completed':
                                _known = user_tasks[user_prompt].current_action
                                _llm_claimed = int(json_obj.get("action_id", _known))
                                if _llm_claimed != _known:
                                    current_app.logger.warning(
                                        f"[HALLUCINATION?] LLM claims action_id={_llm_claimed} "
                                        f"but pipeline has {_known}")
                                _next2, _ok2, _done2 = _advance_reuse_action(user_prompt, _known, "reuse-w1-regex", prompt_id)
                                if not _ok2:
                                    return _finish_reuse_recipe(user_id, prompt_id, json_obj) if _done2 else ''
                                user_message = _build_reuse_action_message(user_prompt, _next2)
                                chat_instructor.initiate_chat(recipient=manager, message=user_message,
                                                              clear_history=False, silent=False)
                                continue
                        else:
                            raise ValueError('No json found')
                    except Exception as e:
                        current_app.logger.warning(f'it is not a json object the error is: {e}')
                        current_app.logger.info('it is not a json object You should ask status verifier to give response in proper format & not move ahead to next action')
                        actions_prompt = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action - 1)
                        message = 'Hey @StatusVerifier Agent, Please verify the status of the action ' + f'{user_tasks[user_prompt].current_action}: {actions_prompt}' + '\n performed and Respond in the following format {"status": "status here","action": "current action","action_id": ' + f'{user_tasks[user_prompt].current_action}' + ',"message": "message here"}'
                        # chat_instructor (UserProxyAgent), not assistant: a
                        # message initiated by an AssistantAgent lands as
                        # role='assistant' in every other agent's view, and a
                        # view with no user-role message anywhere trips the
                        # Qwen3.5 template raise ("No user query found",
                        # jinja line 79) — captured live 2026-08-30 20:15,
                        # body [system, assistant], 3x llama 500.  This loop's
                        # canonical steering initiator is chat_instructor
                        # (see the two sites above).
                        chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False, silent=False)
                        continue
            try:
                # Safely access recipes
                if count == 4:
                    break

                count += 1

                if user_prompt not in recipes or user_tasks[user_prompt].current_action > len(user_tasks[user_prompt].actions):
                    current_app.logger.error(
                        f"Cannot access recipe for current action {user_tasks[user_prompt].current_action}")
                    continue

                if user_tasks[user_prompt].actions[user_tasks[user_prompt].current_action - 1]['can_perform_without_user_input'] == 'yes':
                    current_app.logger.info('GOT can_perform_without_user_input as true')
                    message = 'You should complete this task independently. Feel free to make reasonable assumptions where necessary'
                    # chat_instructor, not helper — same reason as the
                    # StatusVerifier injection above: instructions must enter
                    # the group as user-role turns.
                    chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False, silent=False)

            except Exception as e:
                current_app.logger.error(f'WE have some indexx error here: {e}')
                error_message = traceback.format_exc()  # Capture full traceback
                current_app.logger.error(f"Error in get_agent_response indexx:\n{error_message}")

            if not group_chat.messages:
                # States the OBSERVATION only.  An earlier version of this line
                # blamed transform_messages; that was never established -- the
                # transform logs "N -> 1", never "-> 0", and it rewrites the
                # per-reply view, not group_chat.messages.  Cause still open.
                current_app.logger.warning(
                    'reuse: group chat history is empty mid-loop - ending the '
                    'turn instead of raising IndexError (cause not established)')
                break
            last_message = group_chat.messages[-1]
            content_lower = last_message['content'].lower()
            # Check if this message has already been sent to the user by state_transition
            # In get_agent_response
            if f'message2userfinal'.lower() in content_lower:
                # Extract and process message
                try:
                    json_obj = retrieve_json(last_message['content'])
                    if json_obj and 'message2userfinal' in json_obj:
                        send_message_to_user1(user_id, json_obj['message2userfinal'], '', prompt_id)
                        return ''
                except Exception as e:
                    current_app.logger.error(f"Error extracting JSON: {e}")
            elif f'message2'.lower() in content_lower:
                # Extract and process message
                try:
                    json_obj = retrieve_json(last_message['content'])
                    if json_obj and 'message2' in json_obj:
                        send_message_to_user1(user_id, json_obj['message2'], '', prompt_id)
                        return ''
                except Exception as e:
                    current_app.logger.error(f"Error extracting JSON: {e}")
            elif f'@user'.lower() not in content_lower:
                agent_mentions = [
                    "@statusverifier", "@status verifier", "@verification",
                    "@helper", "@executor", "@StatusVerifier", "@Helper", "@Executor"
                ]

                if any(mention in content_lower for mention in agent_mentions):
                    agent_found = next((mention for mention in agent_mentions if mention in content_lower), None)
                    current_app.logger.info(f'Message directed to agent ({agent_found}), not sending to user')
                    current_app.logger.info(f'continuing since @user not in last message')
                    continue

            else:
                current_app.logger.info(f'@user in last message')
                break

        # if individual_recipe[currentaction_id-1]['can_perform_without_user_input'] == 'yes':
        #     return assistant
        # Same emptiness hazard as the loop head — reached when the group chat
        # produced nothing at all.  Return a plain sentence (and mark the
        # ledger action given-up, see _giveup_current_reuse_action) rather
        # than the '' silent-failure main independently used here — an
        # empty response looks identical to "handler declined intentionally"
        # downstream and delivers total silence to the channel.
        if not group_chat.messages:
            current_app.logger.error(
                f'[EMPTY-GROUPCHAT] group_chat.messages empty after reuse loop '
                f'(user_prompt={user_prompt}) — returning fallback reply')
            _giveup_current_reuse_action(user_prompt, '[EMPTY-GROUPCHAT] no messages after reuse loop')
            return "I wasn't able to put a response together just then. Could you try asking again?"

        last_message = group_chat.messages[-1]
        # len>1 matters: a lone TERMINATE would send [-2] off the front.
        if last_message['content'] == 'TERMINATE' and len(group_chat.messages) > 1:
            last_message = group_chat.messages[-2]

        content_lower = (last_message.get('content') or '').lower()

        if f'message2userfinal'.lower() in content_lower:
            try:
                json_obj = retrieve_json(last_message['content'])
                if json_obj and 'message2userfinal' in json_obj:
                    last_message['content'] = json_obj['message2userfinal']
                    return last_message['content']

            except Exception as e:
                current_app.logger.error(f"Error extracting JSON: {e}")
                # Fallback to a basic pattern match if retrieve_json fails
                pattern = r'@user\s*{[\'"]message2userfinal[\'"]\s*:\s*[\'"](.+?)[\'"]}'
                match = re.search(pattern, last_message['content'], re.DOTALL)
                if match:
                    last_message['content'] = match.group(1)
                    return last_message['content']

        elif f'message2'.lower() in content_lower:
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
        last_message['content'] = last_message["content"].replace("@userproxy ", '')
        last_message['content'] = last_message["content"].replace("@user ", '')

        # At this point, don't process messages with message2userfinal as they were already sent
        return last_message['content']

    except Exception as e:
        current_app.logger.info(f'Got some error {e}')
        error_message = traceback.format_exc()  # Capture full traceback
        current_app.logger.error(f"Error in get_agent_response:\n{error_message}")
        return f"Error getting response: {str(e)}"
    finally:
        # Only disarm what this call armed.  When /chat owns the clock it must
        # survive until the request ends (cleared in teardown_request) --
        # clearing it here would leave the rest of the request unbounded, which
        # is the same class of hole as the concurrent-disarm bug.
        if _owns_deadline:
            _clear_turn_deadline(user_prompt)


def get_flow_number(user_id, prompt_id):
    role = get_role(user_id, prompt_id)
    if not role:
        role = None
    current_app.logger.info(f'Got role as {role}')
    file_path = helper_fun.safe_prompt_path(prompt_id)
    with open(file_path, 'r') as f:
        data = json.load(f)
        available_roles = [x['name'] for x in data['personas']]
        available_flows = data['flows']
    current_app.logger.info(f'Got available_roles as {available_roles}')
    if not role:
        role = available_roles[0]
    role_number = 0
    for num, i in enumerate(available_flows):
        if i['persona'].lower() == role.lower():
            role_number = num
            current_app.logger.info(f'GOT role index as {role_number}')
    return role_number, role


def _sched_log(level, msg):
    """Log for create_schedule — works with or without Flask app context."""
    try:
        getattr(current_app.logger, level)(msg)
    except RuntimeError:
        import logging
        getattr(logging.getLogger('reuse_recipe.scheduler'), level)(msg)


def create_schedule(prompt_id, user_id):
    _sched_log('info', 'INSIDE Create Schedule')
    user_prompt = f'{user_id}_{prompt_id}'
    role_number, role = get_flow_number(user_id, prompt_id)
    with open(helper_fun.safe_prompt_path(prompt_id, role_number, 'recipe'), 'r') as f:
        config = json.load(f)
        config = _normalize_flow_recipe(config)  # tolerate per-action recipe in flow slot
        recipes[user_prompt] = config
    try:
        if 'scheduled_tasks' in config and len(config['scheduled_tasks']) > 0:
            _sched_log('info', 'Creating scheduled tasks')
            for i in config['scheduled_tasks']:
                if role and i['persona'].lower() == role.lower():
                    trigger = CronTrigger.from_crontab(i['cron_expression'])
                    job_id = f"job_{int(time.time())}"
                    scheduler.add_job(execute_python_file, trigger=trigger, id=job_id,
                                      args=[i['job_description'], user_id, prompt_id, i['action_entry_point']])
                    _sched_log('info', f'Successfully created scheduler job {i["persona"]}')

        # Only schedule the 2s visual-poll job when the action API it depends
        # on is actually configured.  With ACTION_API='' (unset in config.json)
        # this job can't work and would error every 2s forever (see the
        # call_visual_task guard) — don't create it at all on such boxes.
        if ACTION_API:
            _sched_log('info', 'Creating Visual scheduled tasks')
            trigger = IntervalTrigger(seconds=int(2))
            job_id = f"job_{int(time.time())}"
            scheduler.add_job(call_visual_task, trigger=trigger, id=job_id,
                              args=['get past 1 mins visual information', user_id, prompt_id])
            _sched_log('info', 'Successfully created scheduler job')
        else:
            _sched_log('info', 'Skipping 2s visual-poll job — ACTION_API not configured')
        if 'visual_scheduled_tasks' in config and len(config['visual_scheduled_tasks']) > 0:
            for i in config['visual_scheduled_tasks']:
                if role and i['persona'].lower() == role.lower():
                    trigger = CronTrigger.from_crontab(i['cron_expression'])
                    job_id = f"job_{int(time.time())}"
                    scheduler.add_job(call_visual_task, trigger=trigger, id=job_id,
                                      args=[i['job_description'], user_id, prompt_id])
                    _sched_log('info', f'Successfully created scheduler job {i["persona"]}')
    except Exception as e:
        _sched_log('error', f'Some Error in creating scheduled tasks error:{e}')


recent_file_id = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_recent_file_id')
# NOTE: recipes TTLCache already defined at module top (line 166) — do NOT redefine here
user_tasks = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_user_tasks')
user_ledgers = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_user_ledgers', loader=load_user_ledger)
user_delegation_bridges = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_user_delegation_bridges')
request_id_list = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_request_id_list')
request_id_list_sent_intermediate = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_request_id_list_sent_intermediate')

time_actions = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_time_actions')
final_recipe = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_final_recipe')

# Signals from autogen agents that a new agent creation is needed
# Keyed by user_prompt (f'{user_id}_{prompt_id}'), set by create_new_agent tool
creation_signals = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_creation_signals')


# =============================================================================
# REUSE ACTION ADVANCEMENT HELPER
# =============================================================================

def _advance_reuse_action(user_prompt, current_action_id, reason="reuse", prompt_id=None):
    """
    Mark action COMPLETED → TERMINATED, advance to next action, set ASSIGNED → IN_PROGRESS.
    Returns (next_action_id, True, False) if advanced.
    Returns (None, False, True) if that was the last action and all actions are now complete
    (current_action_id itself terminated fine — callers should still deliver its message).
    Returns (None, False, False) if a state error prevented advancing at all.
    """
    # Mark current action done
    ok1 = force_state_through_valid_path(user_prompt, current_action_id,
                                         ActionState.COMPLETED, f"{reason}: confirmed")
    ok2 = force_state_through_valid_path(user_prompt, current_action_id,
                                         ActionState.TERMINATED, f"{reason}: done")
    if not ok1 or not ok2:
        # Check actual state — if already TERMINATED, idempotent (safe to advance).
        # If stuck in ERROR or another state, don't advance — ledger would desync.
        actual = get_action_state(user_prompt, current_action_id)
        if actual != ActionState.TERMINATED:
            current_app.logger.error(
                f"[REUSE] Cannot advance action {current_action_id}: "
                f"state is {actual.value}, not TERMINATED — aborting advance")
            return None, False, False
        current_app.logger.info(
            f"[REUSE] Action {current_action_id} already TERMINATED (idempotent)")

    current_app.logger.info(f'[REUSE] Action {current_action_id} TERMINATED, advancing')
    next_id = current_action_id + 1
    user_tasks[user_prompt].current_action = next_id

    if next_id > len(user_tasks[user_prompt].actions):
        current_app.logger.info(f'[REUSE] All {len(user_tasks[user_prompt].actions)} actions completed')
        # Reset for the NEXT incoming message from this same user_prompt.
        # user_tasks[user_prompt] is a persistent, per-identity object —
        # create_agents_for_user() (which builds a fresh Action(), current_action=1)
        # only runs on this identity's FIRST-ever turn (guarded by
        # `user_prompt not in user_agents`); every later turn reuses this
        # SAME object and SAME actions list (reuse mode intentionally
        # re-runs the same recipe per message). Leaving current_action at
        # next_id here left it permanently out of range after the first
        # completed turn — found live 2026-08-31: every subsequent message
        # from the same self-chat identity immediately hit "Cannot access
        # recipe for current action N" (the `current_action > len(actions)`
        # guard a few hundred lines up), burned its bounded 4 retries doing
        # nothing, and fell through to echoing the user's own prompt back
        # as if it were the agent's reply.
        user_tasks[user_prompt].current_action = 1
        # Meter the COMPLETED replay into the owning goal's spark ledger —
        # the daemon's completion gate closes goals on transacted spark only
        # (charged on finished work, never at dispatch). Mirrors the CREATE
        # charge in create_recipe._save_flow_recipe.
        if prompt_id is not None:
            try:
                from integrations.agent_engine.budget_gate import charge_goal_work_completed
                charge_goal_work_completed(
                    prompt_id, len(user_tasks[user_prompt].actions) or 1)
            except Exception as _spark_err:
                current_app.logger.debug(
                    f'[REUSE] completed-work spark charge skipped: {_spark_err}')
        return None, False, True

    safe_set_state(user_prompt, next_id, ActionState.ASSIGNED, f"{reason}: next assigned")
    safe_set_state(user_prompt, next_id, ActionState.IN_PROGRESS, f"{reason}: starting")
    return next_id, True, False


def _build_reuse_action_message(user_prompt, action_id):
    """Build the action execution message for REUSE mode."""
    action_message = user_tasks[user_prompt].get_action(action_id - 1)['action']
    recipe_actions = recipes[user_prompt].get('actions', [])
    if action_id - 1 < len(recipe_actions):
        steps = [{x['steps']: {'tool_name': x.get('tool_name', None),
                               'code': x.get('generalized_functions', None)}} for x in
                 recipe_actions[action_id - 1].get('recipe', [])]
    else:
        steps = []
        current_app.logger.warning(f"[REUSE] No recipe for action {action_id} — executing without steps")
    return f"Perform this action -> Action #{action_id}:{action_message}\n follow these steps: {steps}"


def _finish_reuse_recipe(user_id, prompt_id, json_obj):
    """
    All actions in a recipe just completed (_advance_reuse_action returned
    all_done=True). Extract the StatusVerifier's completion message and
    deliver it — previously the caller returned '' here unconditionally,
    discarding a correct, verified answer (e.g. a completed arithmetic
    task shipped an empty response despite StatusVerifier confirming the
    right result).
    """
    message = json_obj.get('message', '') if isinstance(json_obj, dict) else ''
    if message:
        send_message_to_user1(user_id, message, '', prompt_id)
    return message


# =============================================================================
# SMART LEDGER INTEGRATION HELPERS (same as create_recipe.py)
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


from core.llm_outbound_logger import with_llm_context as _with_llm_context


@_with_llm_context('autogen.reuse')
def chat_agent(user_id, text, prompt_id, file_id, request_id):
    current_app.logger.info('--' * 100)
    user_message = text
    user_prompt = f'{user_id}_{prompt_id}'

    # Drop a speculative re-entry that would race the real turn already
    # running on this key.  See _inflight_turns above for why.
    with _inflight_turns_lock:
        already_running = user_prompt in _inflight_turns
        if not already_running:
            _inflight_turns.add(user_prompt)
    # Only the turn that actually claimed the key may release it, or a
    # duplicate would clear the real turn's flag on its way out.
    owns_inflight = not already_running
    if already_running:
        if _is_expert_dispatch_reentry():
            current_app.logger.warning(
                f'[REENTRANCY] dropped expert-dispatch /chat re-entry for '
                f'{user_prompt} — a real turn is already in flight')
            return ''
        # Identify the caller that got past _is_expert_dispatch_reentry.
        # The detector matches on payload SHAPE, so a re-entry that omits
        # model_config/speculative/draft_first is invisible to it and runs a
        # full duplicate turn against the same cached GroupChat.  Logging the
        # shape here is the only way to name that caller -- inferring it from
        # the source was ambiguous (several /chat posters share the pattern).
        try:
            from flask import request as _rq_dbg
            _body_dbg = _rq_dbg.get_json(silent=True) or {}
            _keys_dbg = sorted(_body_dbg.keys()) if isinstance(_body_dbg, dict) else []
            _src_dbg = (_body_dbg.get('task_source'),
                        _body_dbg.get('autonomous'),
                        _body_dbg.get('request_id'))
            _ua_dbg = _rq_dbg.headers.get('User-Agent', '')
        except Exception:
            _keys_dbg, _src_dbg, _ua_dbg = ['<no request ctx>'], (None, None, None), ''
        current_app.logger.warning(
            f'[REENTRANCY] concurrent non-speculative turn for {user_prompt} '
            f'— proceeding, shared GroupChat may interleave. '
            f'CALLER-SHAPE keys={_keys_dbg} '
            f'task_source/autonomous/request_id={_src_dbg} ua={_ua_dbg!r}')
        # TEMP DIAGNOSTIC (2026-08-14) -- name the duplicate caller.
        # CALLER-SHAPE cannot: the duplicate replays the original request
        # body verbatim, so payload shape is identical for both turns and
        # _is_expert_dispatch_reentry() is blind to it.  The in-process
        # call stack is the one thing that differs, and knowing which
        # caller this is decides the fix: the duplicate cannot simply be
        # queued behind the owner (see _inflight_turns above -- that
        # deadlocks when the owner is what is blocked waiting on it), and
        # dropping the wrong one returns silence to the user.
        # Gated off by default; remove once the caller is identified.
        if os.environ.get('HEVOLVE_REENTRANCY_STACK', '').lower() in ('1', 'true', 'yes'):
            import traceback as _tb_dbg
            current_app.logger.warning(
                '[REENTRANCY-STACK] %s duplicate caller:\n%s',
                user_prompt, ''.join(_tb_dbg.format_stack()[-25:]))

    request_id_list[user_prompt] = request_id
    try:
        if file_id:
            recent_file_id[user_id] = file_id

        # Get or create agents for this user
        if user_prompt not in user_agents:
            llm_call_track[user_prompt] = {'count': 0, 'original_prompt': False}
            if user_prompt not in user_journey:
                if prompt_id not in agent_data.keys():
                    agent_data[prompt_id] = {}
                role_agents[user_prompt] = create_agents_for_role(user_id, prompt_id)
                assistant, user_proxy, group_chat, manager, helper, stop = role_agents[user_prompt]
                if stop:
                    user_journey[user_prompt] = 'UseBot'
                    # action_message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)['action']
                    # user_message = f"Perform this action -> Action #{user_tasks[user_prompt].current_action+1}:{action_message}"
                else:
                    user_journey[user_prompt] = 'Roles'
            if user_journey[user_prompt] == 'UseBot':
                create_schedule(prompt_id, user_id)
                user_agents[user_prompt] = create_agents_for_user(user_id, prompt_id)
                user_journey[user_prompt] = 'UseBot'
        if user_journey[user_prompt] == 'Roles':
            assistant, user_proxy, group_chat, manager, helper, stop = role_agents[user_prompt]
            result = user_proxy.initiate_chat(manager, message=user_message, speaker_selection={"speaker": "assistant"},
                                              clear_history=False)
            # Print the chat summary
            current_app.logger.info("\n=== Chat Summary ===")
            # current_app.logger.info(result.summary)

            # Print the full chat history
            # current_app.logger.info("\n=== Full response ===")
            # current_app.logger.info(result)

            last_message = group_chat.messages[-1]
            if 'terminate' in last_message['content'].lower():
                # with open(f"prompts/{prompt_id}_recipe.json", 'r') as f:
                #     config = json.load(f)
                #     recipes[user_prompt] = config
                user_agents[user_prompt] = create_agents_for_user(user_id, prompt_id)
                assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group = user_agents[user_prompt]
                user_journey[user_prompt] = 'UseBot'
                create_schedule(prompt_id, user_id)
                action_message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action - 1)['action']
                steps = [
                    {x['steps']: {'tool_name': x.get('tool_name', None), 'code': x.get('generalized_functions', None)}}
                    for x in recipes[user_prompt]['actions'][user_tasks[user_prompt].current_action - 1]['recipe']]
                message = f"Perform this action -> Action #{user_tasks[user_prompt].current_action}:{action_message}\n follow these steps: {steps}"
                # message = "let's perform the actions availabe in sequence\nIMP instruction: keep track of action id you are working on."
                result = chat_instructor.initiate_chat(manager, message=message,
                                                       speaker_selection={"speaker": "assistant"}, clear_history=False)

                count = 0
                while True:
                    current_app.logger.info('inside while2')

                    # === LEDGER v2.0: Heartbeat + Budget/SLA ===
                    _w2_current = user_tasks[user_prompt].current_action
                    _w2_ledger = user_ledgers.get(user_prompt)
                    if _w2_ledger:
                        _w2_task = _w2_ledger.tasks.get(f"action_{_w2_current}")
                        if _w2_task:
                            _w2_task.heartbeat()
                            if _w2_task.is_budget_exhausted():
                                current_app.logger.warning(f"[BUDGET] action_{_w2_current} budget exhausted in while2")
                                break
                            if _w2_task.is_sla_breached() and not _w2_task.sla_breached:
                                _w2_task.mark_sla_breached()

                    # Same empty-history hazard as the while1 loop above.
                    if group_chat.messages and group_chat.messages[-1]['name'] == 'ChatInstructor' and group_chat.messages[-1]['content'] == 'TERMINATE':
                        current_app.logger.info(
                            f"group_chat.messages[-2]['content'] {group_chat.messages[-2]['content'][:10]}..")
                        try:
                            try:
                                json_obj = json.loads(group_chat.messages[-2]["content"])
                            except (json.JSONDecodeError, ValueError):
                                json_obj = ast.literal_eval(group_chat.messages[-2]["content"])
                            current_app.logger.info(f'got json object {json_obj}')
                            if json_obj['status'].lower() == 'completed':
                                _llm_aid = int(json_obj.get("action_id", _w2_current))
                                if _llm_aid != _w2_current:
                                    current_app.logger.warning(
                                        f"[HALLUCINATION?] LLM claims action_id={_llm_aid} "
                                        f"but pipeline has {_w2_current}")
                                _w2_next, _w2_ok, _w2_done = _advance_reuse_action(user_prompt, _w2_current, "reuse-w2", prompt_id)
                                if not _w2_ok:
                                    return _finish_reuse_recipe(user_id, prompt_id, json_obj) if _w2_done else ''
                                user_message = _build_reuse_action_message(user_prompt, _w2_next)
                                chat_instructor.initiate_chat(recipient=manager, message=user_message,
                                                              clear_history=False, silent=False)
                                continue
                        except Exception:
                            try:
                                json_obj = retrieve_json(group_chat.messages[-2]["content"])  # canonical parse (#95)
                                if json_obj:
                                    current_app.logger.info(f'got json object {json_obj}')
                                    if json_obj['status'].lower() == 'completed':
                                        _llm_aid2 = int(json_obj.get("action_id", _w2_current))
                                        if _llm_aid2 != _w2_current:
                                            current_app.logger.warning(
                                                f"[HALLUCINATION?] LLM claims action_id={_llm_aid2} "
                                                f"but pipeline has {_w2_current}")
                                        _w2_next2, _w2_ok2, _w2_done2 = _advance_reuse_action(user_prompt, _w2_current, "reuse-w2-regex", prompt_id)
                                        if not _w2_ok2:
                                            return _finish_reuse_recipe(user_id, prompt_id, json_obj) if _w2_done2 else ''
                                        user_message = _build_reuse_action_message(user_prompt, _w2_next2)
                                        chat_instructor.initiate_chat(recipient=manager, message=user_message,
                                                                      clear_history=False, silent=False)
                                        continue

                                else:
                                    raise ValueError('No json found')
                            except IndexError:
                                current_app.logger.info("Completed ALL ACTIONS")
                                return ''
                            except Exception as e:
                                current_app.logger.warning(f'it is not a json object the error is: {e}')
                                current_app.logger.info(
                                    'it is not a json object You should ask status verifier to give response in proper format & not move ahead to next action')
                                actions_prompt = user_tasks[user_prompt].get_action(
                                    user_tasks[user_prompt].current_action - 1)
                                message = 'Hey @StatusVerifier Agent, Please verify the status of the action ' + f'{user_tasks[user_prompt].current_action}: {actions_prompt}' + '\n performed and Respond in the following format {"status": "status here","action": "current action","action_id": ' + f'{user_tasks[user_prompt].current_action}' + ',"message": "message here"}'
                                # chat_instructor, not assistant — see the
                                # matching recovery site in the first loop:
                                # steering must enter the group as a
                                # user-role turn or the Qwen3.5 template can
                                # see a no-user view and raise.
                                chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,
                                                              silent=False)
                                continue
                    count += 1
                    if count == 4:
                        break
                    # role = get_role(user_id,prompt_id)
                    last_message = group_chat.messages[-1]
                    if f'@user'.lower() not in last_message['content'].lower():
                        continue
                    else:
                        current_app.logger.info(f'@user in last message')
                        break

                last_message = group_chat.messages[-1]

                if last_message['content'] == 'TERMINATE':
                    last_message = group_chat.messages[-2]

                llm_call_track[user_prompt]['count'] = 0
                llm_call_track[user_prompt]['original_prompt'] = True
                if f'message2userfinal'.lower() in last_message['content'].lower():
                    json_obj = retrieve_json(last_message["content"])
                    if json_obj:
                        try:
                            last_message['content'] = json_obj['message2userfinal']
                        except Exception:
                            pass

                elif f'message2'.lower() in last_message['content'].lower():
                    json_obj = retrieve_json(last_message["content"])
                    if json_obj:
                        try:
                            last_message['content'] = json_obj['message2']
                        except Exception:
                            pass

                return last_message['content']

            return last_message['content']
        else:
            assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group = user_agents[user_prompt]

            prompt_id = int(prompt_id)
            role = get_role(user_id, prompt_id)
            response = get_agent_response(assistant, chat_instructor, helper, user_proxy, manager, group_chat,
                                          user_message, role, user_id, prompt_id, request_id)
            llm_call_track[user_prompt]['count'] = 0
            llm_call_track[user_prompt]['original_prompt'] = True
            return response
    except Exception as e:
        current_app.logger.info(f'Some ERROR IN REUSE RECIPE {e}')
        raise
    finally:
        if owns_inflight:
            with _inflight_turns_lock:
                _inflight_turns.discard(user_prompt)


def crossbar_multiagent(msg):
    current_app.logger.info("insde crossbar_multiagent")
    current_app.logger.info('--' * 100)

    user_prompt = f"{msg['user_id']}_{msg['caller_prompt_id']}"
    assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group = user_agents[user_prompt]
    message = f"Role: {msg['caller_role']}\n Message: {msg['message']}"
    response = multi_role_agent.initiate_chat(manager, message=message, speaker_selection={"speaker": "assistant"},
                                              clear_history=False)
    last_message = group_chat.messages[-1]
    if last_message['content'] == 'TERMINATE':
        last_message = group_chat.messages[-2]

    # sending response to receiver agent
    send_message_to_user1(msg['user_id'], last_message, msg['message'], msg['caller_prompt_id'])

    user_prompt = f"{msg['caller_user_id']}_{msg['caller_prompt_id']}"
    assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group = user_agents[user_prompt]
    message = f"Role: {msg['role']}\n Message: {last_message}"
    response = multi_role_agent.initiate_chat(manager, message=message, speaker_selection={"speaker": "assistant"},
                                              clear_history=False)
    last_message = group_chat.messages[-1]
    if last_message['content'] == 'TERMINATE':
        last_message = group_chat.messages[-2]

    # sending response to caller agent
    send_message_to_user1(msg['caller_user_id'], last_message, msg['message'], msg['caller_prompt_id'])

def acknowledgment(user_id,prompt_id,request_id):
    user_prompt = f'{user_id}_{prompt_id}'
    author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_prompt]
    group_chat.messages.append({'content':f'GOT MESSAGE ACKNOWLEDGEMENT FOR {request_id}','role':'user','name':'Helper'})
