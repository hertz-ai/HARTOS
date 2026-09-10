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
    HISTORICAL_TOOL_PLACEHOLDER,
    NUNBA_WEB_FETCH_POLICY,
    TOOL_FAILURE_RESULTS,
    VERDICT_COMPLETION_STATUSES,
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
from hartos.helper import ToolMessageHandler, strip_json_values, get_time_based_history, retrieve_json, load_vlm_agent_files, _is_terminate_msg, answered_call_ids


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


# Fields a VLM re-authoring must NOT overwrite: they are the action's contract
# (who owns it, whether it may run unattended), not its content.  Both are
# emitted as constants by the VLM writer — persona is always a user id, and
# can_perform_without_user_input was 'no' in 47 of 47 files measured on this
# box — so letting them through replaces an authored decision with noise.
_VLM_PRESERVED_CONTRACT_FIELDS = ('persona', 'can_perform_without_user_input')


def _action_persona(action, role):
    """Owner of a recipe action, defaulting to the role this turn runs as.

    `persona` is OPTIONAL in a saved action.  MEASURED live 2026-09-10 on the
    installed build: create wrote actions 4 and 5 of agent 28160128202 with no
    `persona` key at all while their siblings carried persona='Executor', and
    88656227144 has none on ANY of its actions (2 of the 127 saved flow
    recipes on this box).  The consumer subscripted it unguarded, so
    create_agents_for_user raised KeyError('persona') INSIDE ITS OWN LOG LINE
    (:1138), /chat 500'd, and the turn fell through to a toolless LLM that
    invented carrier rates for the user.

    Defaulting to `role` is not a new rule: _vlm_merged_actions below is handed
    `role` as its `flow_persona` (see the call at ~:1127) and assigns exactly
    that to an appended action that has no owner.  Same question, same answer,
    ONE derivation.  Defaulting also keeps the action in role_actions, so it
    still RUNS -- making the read merely safe would have traded a loud crash
    for a silent omission.
    """
    if isinstance(action, dict):
        _p = action.get('persona')
        if _p:
            return str(_p)
    return str(role or '')


def _vlm_merged_actions(existing_actions, vlm_actions, flow_persona=None):
    """``existing_actions`` with each VLM re-authoring applied, OWNER kept.

    A ``*_vlm_agent.json`` file re-authors the STEPS of an action; it does not
    reassign whose action it is.  But its ``persona`` field carries a USER id
    (``usercf125371-...``), while a flow recipe's carries a ROLE name
    (``Executor``) — two producers, one field, different vocabularies.  Three
    verbatim copies of the merge replaced the flow action wholesale
    (``recipes[user_prompt]['actions'][i] = vlm_action``), so the role filter
    at L1057 stopped matching every overridden action.

    MEASURED 2026-09-08 20:44 on the installed build, agent 89555447799:
    24 flow actions all persona 'Executor', 22 VLM files carrying user ids ->
    in memory 19 + 3 user-id personas and only 2 'Executor'.  role_actions
    became 2, ``Action(role_actions)`` made 2 the ledger's whole world, and
    the run ended '[REUSE] All 2 actions completed' having silently skipped
    22.  The ``len(role_actions) == 0`` fallback could not help: 2 is not 0,
    so a PARTIAL match reads as a successful narrow instead of a failure.

    Reconciling at LOAD rather than rewriting the 22 files on disk follows
    the precedent ``_normalize_flow_recipe`` set directly above.

    Returns a NEW list — the three call sites all merge into the shared
    ``recipes[user_prompt]`` and one of them re-runs on reload, so mutating
    in place let a second pass compound onto an already-merged list.
    Never raises: this runs while the agent is being built.
    """
    try:
        out = [dict(a) for a in (existing_actions or []) if isinstance(a, dict)]
    except Exception:
        return list(existing_actions or [])
    if not isinstance(vlm_actions, list):
        return out
    for vlm_action in vlm_actions:
        if not isinstance(vlm_action, dict):
            continue
        action_id = vlm_action.get('action_id')
        if action_id is None:
            continue          # unplaceable: no id to match or append against
        merged = dict(vlm_action)
        replaced = False
        for i, action in enumerate(out):
            if action.get('action_id') == action_id:
                # CONTRACT fields survive; only CONTENT is replaced.  Both of
                # these are constants in the VLM writer's output, so taking
                # them would overwrite the flow author's decision with noise:
                #   persona                       -> a user id, never a role
                #   can_perform_without_user_input-> 'no' in 47 of 47 files on
                #                                    this box (zero variance)
                # The autonomy one cost 99 rounds on action 2/24 live at
                # 21:34: _reuse_action_is_autonomous went False, the "complete
                # this task independently" steer never fired, and every round
                # re-read an identical message and made no progress.
                for _keep in _VLM_PRESERVED_CONTRACT_FIELDS:
                    if action.get(_keep) is not None:
                        merged[_keep] = action[_keep]
                out[i] = merged
                replaced = True
                break
        if not replaced:
            # An appended action has no predecessor to inherit from; the flow's
            # persona is the only correct owner.  Without one, leave the file's
            # own value alone rather than invent an owner.
            if flow_persona:
                merged['persona'] = flow_persona
            out.append(merged)
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
    clear_action_states,
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
        # tool_call ids that already existed when the CURRENT action was
        # dispatched.  The fabrication gate ignores their results, so an
        # action cannot inherit credit for an earlier action's tool run.
        # Empty for action 1 — nothing has run yet, so nothing to discount.
        self.evidence_seen_call_ids = set()
        # Action id whose named tools ALL returned empty, stamped as that
        # action finishes; None when the last one got data (or ran no tool).
        self.evidence_vacuous_action = None

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



def _coerce_instruction_text(value) -> str:
    """Normalize a tool 'instructions' argument to plain text.

    Qwen sometimes nests tool args (#653 family): live 2026-09-01
    15:14:35 the hive-training agent passed a dict and
    execute_windows_or_android_command crashed on .lower() before the
    VLM loop could start.  A dict keeps its natural text field when one
    exists; anything else stringifies rather than raising.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ('instructions', 'command', 'task', 'text'):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner
        return json.dumps(value, ensure_ascii=False)
    return str(value)


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

    # Create a basic function calling config.
    #
    # PER-DISPATCH MODEL ROUTING: /chat stashes the caller's chosen model_config
    # in thread-local (hart_intelligence_entry:9200) and it MUST win over the
    # import-time module `config_list`.  This is model-agnostic on purpose — it
    # honours whatever tier the dispatcher selected (hive peer, cloud endpoint,
    # or a locally hosted expert); no backend is special-cased here.
    #
    # No expert is MANDATORY.  When no override is set the local model serves
    # the turn, and local-only is a fully supported configuration — an expert
    # tier augments the agent, it is never a prerequisite for reaching a goal.
    # Hence `or config_list`: the fallback is the contract, not a safety net.
    #
    # Without it the module-level list, bound once at import, silently answered
    # every speculative EXPERT turn on the default local model: measured
    # 2026-09-01 as 233 outbound calls all carrying model="local" while the
    # dispatcher believed it had routed to the EXPERT tier.
    # Same pattern as hart_intelligence_entry.create_agents_for_user (:7242).
    llm_config = {
        "config_list": thread_local_data.get_model_config_override() or config_list,
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
        @assistant.register_for_llm(api_style="tool", description="update the role/persona in db")
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
            # Speaker order is pipeline state, never a model decision (same
            # rule as the main reuse group): the Assistant asks the role
            # question -> the user agent answers it; anything else (the
            # Helper) hands back to the Assistant.  user_proxy was routed
            # above.
            return user_proxy if last_speaker is assistant else assistant

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
    # New session: the fabrication gate's re-steer budget is keyed
    # (user_prompt, action_id); clear this session's entries so a re-run of
    # the same agent gets the budget back instead of one spent for the
    # process lifetime.  The pending-refusal record is keyed the same way and
    # must not survive the session either, or a stale entry would steer the
    # next run about a tool it already ran.
    for _k in [k for k in _reuse_resteer_counts if k[0] == user_prompt]:
        _reuse_resteer_counts.pop(_k, None)
    for _k in [k for k in _reuse_fab_pending if k[0] == user_prompt]:
        _reuse_fab_pending.pop(_k, None)
    # Create a basic function calling config.
    # Per-dispatch model routing — see create_agents_for_role above for why the
    # thread-local override must win over the import-time module config_list.
    llm_config = {
        "config_list": thread_local_data.get_model_config_override() or config_list,
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
            else:
                # THE silent exit.  Measured 2026-09-05: the live log had
                # "SimpleMem initialized" 0 times AND "SimpleMem init failed"
                # 0 times — this branch left no trace at all, so a keyless
                # desktop lost both long-term memory tools invisibly.  Say it.
                current_app.logger.info(
                    "SimpleMem not configured (enabled=%s, api_key=%s) — "
                    "long-term memory runs on MemoryGraph for %s",
                    bool(sm_config.enabled), bool(sm_config.api_key),
                    user_prompt)
        except Exception as e:
            current_app.logger.warning(f"SimpleMem init failed: {e}")
    else:
        current_app.logger.info(
            "SimpleMem package unavailable — long-term memory runs on "
            "MemoryGraph for %s", user_prompt)

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
    stored_goal_tags = None
    with open(helper_fun.safe_prompt_path(prompt_id), 'r') as f:
        config = json.load(f)
        goal = config['goal']
        # Optional semantic tags stamped on the agent record at creation
        # (Lever 2, owner 2026-09-01).  Absent on legacy records — the
        # gate below then resolves to pure detection, exactly as before.
        stored_goal_tags = config.get('goal_tags')

    current_app.logger.info(f'Got goal as {goal}')
    role_actions = []
    actions = []

    # Load any VLM agent files
    vlm_actions = load_vlm_agent_files(prompt_id, role_number)

    # Integrate VLM agent actions with existing recipe actions.  ONE merge —
    # this block was inlined verbatim at three sites, and all three dropped the
    # flow action's persona, which is what reduced this agent's ledger from 24
    # actions to 2.  See _vlm_merged_actions.
    if vlm_actions:
        _before = len(recipes[user_prompt]['actions'])
        recipes[user_prompt]['actions'] = _vlm_merged_actions(
            recipes[user_prompt]['actions'], vlm_actions, role)
        final_recipe[prompt_id] = recipes[user_prompt]
        current_app.logger.info(
            f"[VLM-MERGE] {len(vlm_actions)} override(s); actions "
            f"{_before} -> {len(recipes[user_prompt]['actions'])}, "
            f"persona-matching role {role!r}: "
            f"{sum(1 for a in recipes[user_prompt]['actions'] if _action_persona(a, role).lower() == str(role or '').lower())}")

    current_app.logger.info(f'Getting role actions')
    for i in recipes[user_prompt]['actions']:
        _persona = _action_persona(i, role)
        current_app.logger.info(f'this is action persona:{_persona} ')
        if _persona.lower() == str(role or '').lower():
            role_actions.append(i)
            actions.append(i['action'])
    # current_app.logger.info(f'role_actions: {role_actions}')
    # current_app.logger.info(f'will create timer agents with: {actions}')
    time_actions[user_prompt] = Action(actions)

    if len(role_actions) == 0:
        role_actions = recipes[user_prompt]['actions']

    # Perform topological sorting
    # sorted_actions = topological_sort(role_actions)

    # Create Action with Smart Ledger integration for persistent task tracking.
    #
    # Clear this session's ActionStates FIRST.  They are process-global and keyed
    # only by user_prompt, so a CREATE that ran earlier in this same process left
    # every action TERMINATED (its flow-complete force-terminate).  Without the
    # reset, the loop below reads those terminals and `[AUTO-ADVANCE]`s through
    # the whole recipe without executing anything — measured live 2026-09-05 on
    # agent 90210554431: 4 actions skipped in 14 ms, zero tool calls, while the
    # identical run on a fresh process executed google_search for real.
    # A new Action object IS a new run; its states must start where a new run
    # starts.  See lifecycle_hooks.clear_action_states.
    clear_action_states(user_prompt)
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
        # Pass user_id/prompt_id BY KEYWORD.  The signature is
        # ``create_ledger_from_actions(agent_id, session_id, actions, ...)``,
        # so the positional form this used to use bound user_id -> agent_id
        # and prompt_id -> SESSION_ID.  Two consequences, both measured live
        # 2026-09-05 on Scout2 (77712340019, a 2-action recipe):
        #
        #   * session_id was never None, so the whole session-resolution block
        #     (core.py:3884 — resume an in-flight session, else mint a fresh
        #     f"{user_id}_{prompt_id}_{ts_ms}") was unreachable, and with it
        #     ``resume_if_unfinished`` in either direction.  Dead code in
        #     production; it only ever ran from tests, which pass keywords.
        #   * create_recipe used the identical positional form, so CREATE and
        #     REUSE both constructed SmartLedger(user_id, prompt_id) — the SAME
        #     ledger — which then accumulated every run's tasks forever.  The
        #     marker below read "session=77712340019 tasks=77 actions=2": the
        #     raw prompt_id as the session, holding 75 inherited tasks whose
        #     terminal statuses blocked every transition ("Cannot transition
        #     from terminal state COMPLETED", core.py:624 — correct: terminal
        #     means terminal), so the action never advanced.  Daemon 43104584497
        #     hit the same wall: current_action_id 1 read 52 times in an hour.
        #
        # With keywords, agent_id becomes str(prompt_id) (the documented
        # ``agent_id == prompt_id`` convention the dashboard's
        # prompt -> session -> flow -> action grouping depends on) and the
        # default resume_if_unfinished=True does the right thing for both
        # callers: resume a genuinely in-flight session, otherwise mint a fresh
        # timestamped one.  Explicitly passing False would instead pin the
        # legacy deterministic f"{user_id}_{prompt_id}" — i.e. re-create the
        # accumulate-forever bug — so it is deliberately NOT passed here.
        ledger = create_ledger_from_actions(user_id=user_id, prompt_id=prompt_id,
                                            actions=role_actions,
                                            backend=backend, flow_id=role_number)
        user_ledgers[user_prompt] = ledger
        # Measures the line above: a fresh session carries one task per recipe
        # action, and its id is NOT the bare prompt_id.  Pre-fix this read
        # "session=77712340019 tasks=77 actions=2".
        current_app.logger.info(
            f"[REUSE-LEDGER] session={ledger.session_id} "
            f"tasks={len(getattr(ledger, 'tasks', None) or {})} "
            f"actions={len(role_actions)}")

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
            # REUSE = execution: the recipe IS the plan.  execution_mode=True
            # suppresses the persona's "ask 1-2 clarifying questions before
            # executing" behaviour, and we deliberately do NOT append
            # build_proactive_vision_prompt(goal) here — its "understand the
            # DEEPER VISION … ask 1-2 questions before executing the first
            # action" block is a SECOND source of the same stall: it is what
            # made the 4B open with discovery questions ("what deeper vision?")
            # and send holding messages instead of running the saved recipe and
            # synthesising from the real tool outputs (measured live 2026-09-03,
            # agent 18088688973).  Both are CREATE-time behaviours; in REUSE the
            # requirements are already gathered and banked in the recipe.
            _personality_block = build_personality_prompt(
                _saved_personality, resonance_profile=_resonance_profile,
                execution_mode=True)
    except Exception:
        pass

    response_format = {"message2userfinal": "Your message here"}
    # DATE-CONTEXT: the local model's training cutoff makes it assume an earlier
    # year — it refused "2026" research as "the future" and injected "2024" into
    # its own search queries (measured live 2026-09-05, agent 18088688973).
    # State today's date as ground truth so current/future-year work is treated
    # as real and searchable.  Reused for the StatusVerifier below (one source).
    _date_ctx = (
        f"CURRENT DATE: today is {datetime.now().strftime('%A, %B %d, %Y')}. "
        f"Treat this as the present; do NOT assume an earlier year or refuse a "
        f"task as 'in the future'. Current-year information is real and searchable."
    )
    agent_prompt = f'''{_date_ctx}

You are a Helpful {role} Assistant. Your primary role is to assist the user efficiently while keeping all internal actions and processes hidden from the end user. Follow the guidelines below to perform tasks correctly:
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
        {NUNBA_WEB_FETCH_POLICY}
        In this group, asking for a tool means: tag @Helper to call
        request_tools.
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
            10. If a capability you need is NOT covered by any tool listed above, ask @Helper to call the 'request_tools' tool with a short description of the capability (for example: request_tools with need='crawl a webpage'). When it replies "Attached and ready to call NOW: <tool names>", those tools are live IMMEDIATELY in this same conversation - ask @Helper to call them to finish the task. Never tell the user a capability is unavailable before trying request_tools.
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
            4a. If the task needs a capability none of your tools cover, FIRST call the 'request_tools' tool with a short description (for example need='crawl a webpage'); the tools it attaches are callable immediately in this same conversation - call them to complete the task. If it reports no match, retry once with different wording, then offer the routes it names. Never claim a capability is unavailable without trying request_tools.
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
            Judge ONLY from evidence already present in this conversation. A tool result carrying the tool's real output is proof that step RAN: mark it completed even though you yourself cannot call tools.
            NEVER justify ANY verdict — "pending", "error", or any other — with the reason that YOU cannot perform or cannot call a tool. Your own inability says nothing about whether the Assistant already called it, and nothing about whether the tool exists. "pending" means the conversation shows the work has not been done yet; "error" means the conversation shows an attempt that FAILED, with the failure visible.
            You are not given tools, so you can never observe which tools exist. NEVER report that a tool is missing, unavailable, or not built in. If an action names a tool you cannot see, that is expected and is NOT an error — judge the action from the Assistant's messages and tool results instead.
            Use "requires_breakdown" when an action is too complex and needs to be split into smaller subtasks. Each subtask should have a unique subtask_id (e.g., "1.1", "1.2").

        """,
        is_termination_msg=_is_terminate_msg,
    )
    # Give the StatusVerifier the same date ground-truth as the Assistant so it
    # does not reject current/future-year work as impossible (see DATE-CONTEXT).
    try:
        verify.update_system_message(_date_ctx + "\n\n" + verify.system_message)
    except Exception:
        pass

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
            # peer_agents: the seat that RAN a tool is not usually the seat
            # whose next request is being built (tools execute in a pairwise
            # Assistant<->Executor exchange — see the note at :3363).  Handing
            # the handler the peers lets it fill an unanswered tool slot with
            # the result they already hold instead of a placeholder.  Live
            # 2026-09-07: 88 placeholders minted in one drive, median wire tool
            # result 45 chars = the placeholder, so the brief could not cite a
            # search that really ran.  Same list the transform is attached to.
            ToolMessageHandler(user_tasks=user_tasks, user_prompt=user_prompt,
                               peer_agents=[assistant, helper, executor, verify,
                                            chat_instructor]),
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
        api_style="tool",
        description="Send a message to a specific persona/role within this multi-persona agent (e.g. student/parent/teacher).")
    @log_tool_execution
    def send_message_to_roles(
        role: Annotated[str, "Target persona/role name to deliver the message to"],
        message: Annotated[str, "The question to ask or message to send"],
    ) -> str:
        return _send_message_to_roles_impl(
            user_id, prompt_id, role, message, publish_fn=publish_async)
    database_url = get_db_url() or 'https://mailer.hertzai.com'

    # --- Visual/audio trigger watcher (continuous monitoring) ---
    @assistant.register_for_execution()
    @helper.register_for_llm(
        api_style="tool",
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

    # create_scheduled_jobs stays INLINE on the reuse-main leg: the
    # factory's same-named tool is a create-flow STUB ("creation process
    # will do it at the end" — end-of-creation machinery schedules), but
    # a LIVE reuse agent must schedule NOW.  Two behaviors under one
    # name = a #511 name collision; until that is resolved canonically,
    # this restores the exact pre-#743 body (owner audit 2026-09-01
    # found the swap had silently stubbed real scheduling).
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="tool",
                             description="Use this to Create scheduled jobs")
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

    # --- MemoryGraph provenance tools (remember, recall, backtrace) ---
    if memory_graph is not None:
        try:
            from integrations.channels.memory.agent_memory_tools import create_memory_tools, register_autogen_tools
            mem_tools = create_memory_tools(memory_graph, str(user_id), user_prompt)
            register_autogen_tools(mem_tools, assistant, helper)
            current_app.logger.info(f"MemoryGraph tools registered for {user_prompt}")
        except Exception as e:
            current_app.logger.warning(f"MemoryGraph tools registration failed: {e}")


    # Expert agent consultation tool — domain-specific guidance on demand
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="tool",
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
    @helper.register_for_llm(api_style="tool",
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

    # ONE description string.  The named-attach hand-over at L2392 builds this
    # closure's (name, desc, func) triple from the same constant, so the schema
    # the per-turn attach shows the model can never drift from the one
    # register_for_llm shows it.
    _EXEC_CMD_DESC = ("Processes user-defined commands on a personal Windows "
                      "or Android system.")

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="tool", description=_EXEC_CMD_DESC)
    @log_tool_execution
    async def execute_windows_or_android_command(
            instructions: Annotated[str, "Command in plain English to execute on the user's computer or mobile device"],
            os_to_control: Annotated[str, "The OS to control: 'windows', 'linux', 'macos', or 'android'"]) -> str:
        """
        Executes a command on any desktop (Windows/Linux/macOS) or Android device. Uses pyautogui for cross-platform GUI automation.
        """
        # Models sometimes nest the args (#653 family) — live 15:14:35
        # crash: instructions arrived as a dict and :1508's .lower()
        # raised AttributeError, killing the tool before the VLM loop.
        instructions = _coerce_instruction_text(instructions)
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

            # PROMPTS_DIR (module scope, from hartos.helper) is the canonical
            # deployment-aware recipe store, and helper creates it at import so
            # listdir cannot ENOENT.  A CWD-relative "prompts" resolved against
            # the frozen install's CWD (Program Files), which has no such dir:
            # every one of 38 live calls on 2026-09-06 raised FileNotFoundError
            # here before any real work ran.
            current_app.logger.info(f"Checking for VLM files in directory: {PROMPTS_DIR}")
            pattern = f"{prompt_id}_{role_number}_*_vlm_agent.json"
            current_app.logger.info(f"Looking for files matching pattern: {pattern}")


            existing_vlm_files = []
            for file in os.listdir(PROMPTS_DIR):
                if file.startswith(f"{prompt_id}_{role_number}_") and file.endswith("_vlm_agent.json"):
                    existing_vlm_files.append(file)

            current_app.logger.info(f"Found existing VLM files: {existing_vlm_files}")

            # Reload VLM agent files to ensure latest
            current_app.logger.info("Reloading VLM agnet files to ensure we have the latest")
            vlm_actions = load_vlm_agent_files(prompt_id, role_number)
            if vlm_actions:
                current_app.logger.info(f"Loaded {len(vlm_actions)} VLM agents")
                if user_prompt in recipes:
                    recipes[user_prompt]['actions'] = _vlm_merged_actions(
                        recipes[user_prompt]['actions'], vlm_actions)
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

                        # Import os here to ensure it's available
                        import os
                        import re
                        import json

                        # Same builder the read site above uses, so writer and reader
                        # agree by construction.  The number in this filename is NOT a
                        # uniquifier: helper.load_vlm_agent_files parses it back as the
                        # action's identity (parts[2]), and _vlm_merged_actions appends
                        # any id no existing action carries.  A counter that walked to
                        # the next free slot therefore filed each re-learned command as
                        # a NEW action.  Measured on agent 33323830039: a 1-action
                        # recipe grew to 4 actions over two drives, and the 3 appended
                        # ones each carry can_perform_without_user_input 'no' below,
                        # which disarms every driver.  Re-learning an action overwrites
                        # that action's file.
                        vlm_agent_path = helper_fun.safe_prompt_path(
                            prompt_id, role_number, action_id, 'vlm_agent')

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
                                "action_id": action_id,
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
                                recipes[user_prompt]['actions'] = _vlm_merged_actions(
                                    recipes[user_prompt]['actions'], vlm_actions)
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
                # Returned from core.constants, not restated here: the
                # fabrication gate keys on these exact strings to tell "the
                # tool ran and refused" from "the tool did the work".  A
                # literal at this end could drift from the reader's copy and
                # a failed action would silently count as completed again.
                if 'message' in response and 'Failed to capture screenshot' in response['message']:
                    return TOOL_FAILURE_RESULTS[1]
                else:
                    return TOOL_FAILURE_RESULTS[0]
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
    @helper.register_for_llm(api_style="tool",
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
            # Same peer wiring as the recipe path above (see rationale there).
            ToolMessageHandler(user_tasks=user_tasks, user_prompt=user_prompt,
                               peer_agents=[time_agent, helper1, executor1,
                                            multi_role_agent1, verify1,
                                            chat_instructor1]),
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
    from core.agent_tools import (
        build_core_tool_closures, register_core_tools, register_dual,
        main_leg_core_tools,
    )
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

    # #743 Tier-0: the MAIN leg's core tools come from the same factory as
    # the time/visual legs — its 19 inline decorator stacks are deleted
    # above (they had drifted from canon: current_app.logger inside worker
    # threads, mandatory start/end on get_chat_history, a direct-minicpm
    # get_user_camera_inp that bypassed helper_fun's local-first path).
    # Name-filtered to exactly the set the main leg registered before the
    # migration: zero schema growth, no new tools; per-tag gating at the
    # factory is the next step and depends on this consolidation.
    #
    # The name set moved to core.agent_tools.MAIN_LEG_CORE_TOOLS (beside the
    # factory that builds the closures) so create_recipe's identical
    # helper/assistant leg applies the SAME filter — it had none, and shipped
    # all 72 tools / 10,544 schema tokens into a 12,288-token slot.
    # executor_proposes: the Assistant carries the tool SCHEMA, not execution
    # alone.  The recipes name IT as the actor
    # ('agent_to_perform_this_action': 'Assistant'), but the helper=schema /
    # assistant=execution split meant its outbound bodies carried no tools[] —
    # measured 2026-09-06 on agent 89555447799: 11 of 1,182 autogen.reuse
    # bodies had a tools block, and the tools-less execution-persona ones drove
    # 26x "tool 'google_search' is not available" + 2,657+ "Function <X> not
    # found".  second_executor keeps the Assistant's own structured calls from
    # stranding under autogen's repeat-speaker rule — same reason
    # register_news_tools/register_revenue_tools below are passed executor=.
    register_core_tools(main_leg_core_tools(core_tools), helper, assistant,
                        executor_proposes=True, second_executor=executor)

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
        register_news_tools(helper1, time_agent, user_id, executor=executor1)
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
    goal_tags = []  # bound before the gated blocks; detected inside the try
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

        # Tier-1 hierarchical gate: ONE detection per constructor, consumed
        # here and by the Tier-2 family loaders below.  Ungated, all 50
        # rendered defs cost 5,820 of the 6,144-token slot (2026-08-31).
        from integrations.agent_engine.marketing_tools import resolve_goal_tags
        from core.agent_tools import filter_service_tools
        goal_tags = resolve_goal_tags(stored_goal_tags, goal or '')
        _n_all_svc = len(svc_tools)
        svc_tools = filter_service_tools(goal_tags, svc_tools, svc_defs,
                                         service_tool_registry)
        current_app.logger.info(
            f"Tier-1 tool gate: goal_tags={goal_tags} kept "
            f"{len(svc_tools)}/{_n_all_svc} service tools")

        # Never-say-unavailable: always-on discovery that attaches gated-out
        # or newly-needed tools mid-conversation (owner req 2026-08-31).
        _attached_names = set(svc_tools)
        # Shared per-conversation state for the per-turn attach hook in
        # get_agent_response — same set object request_tools mutates, so
        # both layers see one attach ledger.
        assistant._hart_attached_tools = _attached_names
        assistant._hart_unlocked_tags = set(goal_tags)
        # The FULL core closure list, for the per-turn named attach in
        # get_agent_response — that runs in a different function, so the list
        # built at L2141 is out of scope there and has to ride the agent like
        # its two siblings above.  Full, not main_leg_core_tools(...): the
        # whole point is to reach a closure the main leg was NOT given, only
        # for an action whose own recipe names it (see attach_for_names).
        # ...plus the one named tool the builder cannot hold.
        # execute_windows_or_android_command is a nested def in THIS function
        # (L1683) closing over 33 locals — assistant, helper, final_recipe,
        # recipes, user_tasks, prompt_id — so build_core_tool_closures(ctx),
        # which only gets a ctx dict, cannot construct it.  It was therefore in
        # neither list attach_for_names searches, and 28 saved recipes name it:
        # measured live 2026-09-09, "named attach ... -> 0 tools" 5/5 for this
        # name (google_search and send_message_to_user, which ARE in the
        # builder, resolved 1/1).  The action then wedges honestly — 15:14:31
        # FAB-GUARD unrun=['execute_windows_or_android_command'], and the agent
        # itself said "I don't have the execute_windows_or_android_command tool
        # available".  Handed over here, in the (name, desc, func) shape
        # attach_for_names already unpacks (core/agent_tools.py:423), because
        # here is the only scope where the closure exists.
        # Still named-attach ONLY: this runs arbitrary OS commands, and
        # attach_for_names' own docstring keeps the blast radius at the action
        # whose recipe asked for it.  Nothing is added to the main leg.
        assistant._hart_core_tools = core_tools + [
            ('execute_windows_or_android_command', _EXEC_CMD_DESC,
             execute_windows_or_android_command),
        ]
        # The FULL recipe list, so the per-turn hook in get_agent_response can
        # narrow the system prompt to the action actually being dispatched
        # (see _recipe_section_for_action).  Same scope problem as above: the
        # list is built at L1189 inside THIS function, the hook runs in
        # another.  Stored by reference and never mutated — L2682 indexes the
        # same list for can_perform_without_user_input.
        assistant._hart_individual_recipe = individual_recipe

        def request_tools(need: str) -> str:
            from core.agent_tools import discover_and_attach
            # _hart_core_tools is the FULL closure list stashed at L2415 —
            # the same source attach_for_names reads.  Without it the runtime
            # discovery path can only see the 13 service tools.
            return discover_and_attach(need, helper, assistant,
                                       service_tool_registry, _attached_names,
                                       core_tools=getattr(
                                           assistant, '_hart_core_tools', None))
        register_dual(helper, assistant, request_tools, 'request_tools',
                      "Discover and attach additional tools by describing the "
                      "capability you need, e.g. 'text to speech' or 'crawl a "
                      "webpage'. Call this FIRST whenever your current tools "
                      "lack a capability - never tell the user something is "
                      "unavailable without trying this. If it finds no "
                      "match, call it once more with different wording.")

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
        # goal_tags comes from the single Tier-1 detection above — the
        # second detect_goal_tags call this block used to make is gone.
        from integrations.agent_engine.marketing_tools import register_marketing_tools
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
            register_revenue_tools(helper, assistant, user_id, executor=executor)
            current_app.logger.info("Revenue tools loaded (Tier 2) for reuse agent")
        if 'finance' in goal_tags:
            # Finance tools: get_financial_health + track_revenue_split +
            # assess_sustainability + manage_invite_participation.  This
            # branch did not exist, so register_finance_tools had ZERO
            # production callers (only tests/e2e/test_e2e_pipelines.py:615)
            # and a Finance agent's action reported 'error' twelve
            # consecutive nudges because the tool it names never attached.
            from integrations.agent_engine.finance_tools import register_finance_tools
            register_finance_tools(helper, assistant, user_id, executor=executor)
            current_app.logger.info("Finance tools loaded (Tier 2) for reuse agent")
        if 'news' in goal_tags:
            # News tools parity with create_recipe.py — a Herald (news) recipe
            # authored under the 'news' tag must replay with its feed tools,
            # else fetch_news_feeds / mark_news_for_web 404 and the daily
            # refresh step fails silently.
            from integrations.agent_engine.news_tools import register_news_tools
            register_news_tools(helper, assistant, user_id, executor=executor)
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

            # A tool call routes to the agent whose function_map actually holds
            # the function — autogen's own func_call_filter rule
            # (groupchat.py _prepare_and_select_agents: agents that
            # can_execute_function(funcs)).  A custom speaker_selection_method
            # returns BEFORE that filter runs, so it must be applied here;
            # hardcoding one executor sent google_search to an agent whose map
            # lacked it -> "Function google_search not found", the tool never
            # ran and the turn fell back to a knowledge-cutoff answer (live
            # 2026-09-05 01:25).  register_dual puts service-tool execution on
            # the executor and core-tool execution on the assistant, so which
            # agent runs a call is per-tool; ask, don't assume.
            _last = messages[-1]
            _funcs = []
            if isinstance(_last, dict):
                if _last.get("function_call"):
                    _funcs.append((_last["function_call"] or {}).get("name"))
                for _tc in (_last.get("tool_calls") or []):
                    if (_tc or {}).get("type") == "function":
                        _funcs.append((_tc.get("function") or {}).get("name"))
            _funcs = [f for f in _funcs if f]
            # Return the first agent that can execute, exactly as autogen's
            # func_call_filter does — an early return, so allow_repeat_speaker
            # is not applied (a tool whose executor IS the proposer still runs).
            if _funcs:
                for _ag in groupchat.agents:
                    try:
                        if _ag.can_execute_function(_funcs):
                            current_app.logger.info(
                                f"reuse: tool_call {_funcs} -> {_ag.name} (holds the function)")
                            return _ag
                    except Exception:
                        pass

            # Check for messages directed to the user



            # Process JSON responses from StatusVerifier.  Parse with the
            # canonical retrieve_json (json / repair_json / ast / regex), NOT a
            # naive single-to-double quote swap + non-greedy brace regex +
            # json.loads: swapping every quote corrupts apostrophes in the
            # verdict (developer's becomes developer"s), json.loads then dies on
            # "Expecting ',' delimiter", the 'completed' status is never read,
            # and the action loops until the count cap (measured live
            # 2026-09-05: 21 parse-fails on one Auto Research turn, action never
            # advanced).  Same fix the sibling parse site below carries (#95).
            try:
                last_json = retrieve_json(messages[-1]["content"])

                if isinstance(last_json, dict) and last_json:
                    # Session-qualified, like the sibling "Retrieved
                    # current_action_id: N for session: X" line.  Without the
                    # qualifier this verdict cannot be attributed: measured
                    # 2026-09-06 08:41-08:57, THREE sessions were emitting
                    # verdicts at once (cf125371-..._89555447799 x84,
                    # 219b8c80-..._65708210992 x68, 219b8c80-..._88663405573 x6
                    # — the last two driven by background daemons, not by any
                    # operator), and 0 of 41 'last json as' lines named their
                    # session.  Reading them as one agent's produced two wrong
                    # conclusions in one day: an "action 6" that belonged to
                    # another agent, and a "cross-agent contamination" defect
                    # that was simply a concurrent agent's own action text.
                    # A log line that cannot say whose it is is not evidence.
                    current_app.logger.info(
                        f'last json as {last_json} for session: {user_prompt}')

                    if 'status' in last_json.keys() and str(last_json.get('status', '')).lower() == 'completed':
                        current_app.logger.info(
                            f'GOT COMPLETED FOR ACTION in state_transition for session: {user_prompt}')
                        # Don't trust LLM's action_id — use known pipeline state
                        # The actual advancement happens in get_agent_response/chat_agent loops
                        return chat_instructor

                    # Use known pipeline state, not LLM's claimed action_id
                    _known_aid = user_tasks[user_prompt].current_action
                    _cp_yes = False
                    try:
                        _cp_yes = individual_recipe[_known_aid - 1]['can_perform_without_user_input'] == 'yes'
                    except (IndexError, KeyError):
                        pass

                    # DEAD-STATE HANDLING: StatusVerifier emits four states
                    # (completed/error/pending/requires_breakdown) but only
                    # 'completed' was handled — the other three fell through and
                    # the action spun until the turn's round cap, never advancing
                    # and (for requires_breakdown) silently discarding the subtasks.
                    # Handle them per the StatusVerifier system_message: route
                    # error/pending back to the helper to actually finish/fix the
                    # work, and persist requires_breakdown's subtasks to the
                    # ledger (the imported-but-unwired add_subtasks machinery).
                    # Bounded by the loop's round budget (_reuse_turn_round_budget,
                    # recipe-derived — it was a literal 4 when this was written);
                    # NEVER fake-advances — only a
                    # truthful 'completed' advances, a persistent error/pending
                    # ends the turn honestly instead of looping.
                    _st = str(last_json.get('status', '')).lower()
                    if _cp_yes and _st == 'requires_breakdown':
                        # ROUTING ONLY.  The subtask WRITE that used to sit here
                        # has moved to the [BREAKDOWN] block in get_agent_response,
                        # which is the scope that also reads the ledger back.
                        #
                        # It never fired from here and could not: this function is
                        # autogen's speaker-selection callback, so it runs to pick
                        # who speaks NEXT and is therefore never invoked on a
                        # round's terminal message — which is precisely what a
                        # requires_breakdown verdict is.  Measured live 2026-09-06
                        # 18:07-18:33: this JSON branch was entered 11 times, all
                        # error/pending, 0 requires_breakdown, while the consumer
                        # saw requires_breakdown 27 times on an empty ledger.
                        # Keeping a second writer here would just be a parallel
                        # path that drifts (Gate 4) — one writer, and it lives
                        # where the data actually arrives.
                        current_app.logger.info(
                            f'reuse: requires_breakdown for action {_known_aid} '
                            f'-> routing to helper (subtasks persisted by the '
                            f'breakdown executor) for session: {user_prompt}')
                        return helper
                    if _cp_yes and _st in ('error', 'pending'):
                        current_app.logger.info(
                            f'reuse: non-terminal status {_st!r} for action {_known_aid} '
                            f'-> routing to helper to continue (bounded)')
                        return helper

                    if _cp_yes:
                        return assistant
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
                    last_speaker.name == "helper" or
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

            # Speaker order is pipeline state, never a model decision.  This
            # group is a fixed loop (Assistant proposes -> Executor runs the
            # tool -> Assistant synthesises -> StatusVerifier judges ->
            # ChatInstructor advances) and every hop is already decided above
            # from what just happened: @mention, tool call, verifier verdict,
            # who spoke.  The only speakers that can reach this line are the
            # Assistant with a plain turn (no tool call, no mention, no
            # verdict: its work is done -> verify it) and the StatusVerifier
            # or another agent with an unparseable turn (-> back to the
            # Assistant); Helper, Executor, user_proxy and ChatInstructor were
            # routed at the name check above.  Returning "auto" would spend an
            # extra model call per hop to guess what this state already
            # knows, and that call runs outside the AgentLightningWrapper, so
            # any engine failure inside it ends the whole turn (measured
            # 2026-09-03 23:26 on the installed build).
            return verify if last_speaker is assistant else assistant
        except Exception as e:
            current_app.logger.error(f"Error in state_transition: {e}")
            current_app.logger.error(traceback.format_exc())
            return assistant

    def state_transition1(last_speaker, groupchat):
        current_app.logger.info('INSIDE TIMER STATE TRANSITION')
        messages = groupchat.messages
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
                if ('status' in last_json.keys()
                        and last_json['status'].lower() in VERDICT_COMPLETION_STATUSES):
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
                return verify1 if last_speaker is time_agent else time_agent

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
        # Speaker order is pipeline state, never a model decision (same rule
        # as the main reuse group): the timer agent's plain turn -> verify1
        # judges it; anything else -> back to the timer agent.  user_proxy,
        # multi_role_agent, Helper and Executor were routed above.
        return verify1 if last_speaker is time_agent else time_agent

    def state_transition2(last_speaker, groupchat):
        current_app.logger.info('INSIDE VISUAL STATE TRANSITION')
        messages = groupchat.messages
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
        # Same rule as the main reuse group: the visual agent's plain turn ->
        # verify2 judges it; anything else -> back to the visual agent.
        return verify2 if last_speaker is visual_agent else visual_agent

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

    # _reuse_group_terminate is module-level (see its docstring) — the three
    # managers below share that ONE predicate.
    group_chat = autogen.GroupChat(
        agents=[assistant, helper, user_proxy, multi_role_agent, executor, chat_instructor, verify],
        messages=[],
        max_round=10,
        # {agentlist} is filled by autogen's select_speaker_prompt() with the
        # ELIGIBLE candidate set — i.e. the last speaker is dropped when it is
        # barred from repeating (allow_repeat_speaker=False, groupchat.py:509).
        # A hardcoded list here diverged from that eligible set: the 4B was
        # SHOWN Assistant, picked it, but the validator (_mentioned_agents,
        # groupchat.py:853) checked against the Assistant-excluded set → endless
        # "You didn't choose a speaker" reprompt. Must be a plain str (not f"")
        # so the {agentlist} token survives to autogen's .format(agentlist=...).
        select_speaker_prompt_template="Read the above conversation, select the next person from {agentlist} & only return the role as agent. Return User only if the previous message demands it",
        select_speaker_transform_messages=select_speaker_transforms,
        speaker_selection_method=state_transition,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice
        send_introductions=False,
        role_for_select_speaker_messages='user',
    )

    manager = autogen.GroupChatManager(
        groupchat=group_chat,
        llm_config={"cache_seed": None, "config_list": config_list},
        is_termination_msg=_reuse_group_terminate,
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
        llm_config={"cache_seed": None, "config_list": config_list},
        is_termination_msg=_reuse_group_terminate,
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
        llm_config={"cache_seed": None, "config_list": config_list},
        is_termination_msg=_reuse_group_terminate,
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

    # Group-chat write-back — shared PersistentChatHistory + SimpleMem +
    # MemoryGraph — through the CANONICAL installer, the same one
    # create_agents_for_role already uses (L871).
    #
    # This block used to hand-roll the wrapping and carried three defects the
    # canonical installer does not have:
    #   1. it captured `gc.messages.append` and called it from inside the hook,
    #      so every message was appended a SECOND time into the pre-wrap list —
    #      an orphan nothing reads.  The canonical installer documents this
    #      exact hazard and hands the factory a no-op instead;
    #   2. its list subclass was defined INSIDE the for-loop, so the
    #      `isinstance(gc.messages, _HookedList)` re-check below compared
    #      against the LAST iteration's class object and was False for
    #      group_chat and group_chat_1;
    #   3. those two therefore got DOUBLE-wrapped, which silently killed their
    #      shared-history + SimpleMem write-back: an outer list subclass's
    #      append calls plain list.append, never the inner subclass's append.
    # One wrap, one sink fan-out, one place to fix.
    def _graph_sink(msg, graph=memory_graph, session=user_prompt):
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        speaker = msg.get("name", "Agent") if isinstance(msg, dict) else "Agent"
        if content and len(content.strip()) > 5:
            graph.register_conversation(speaker, content, session)

    try:
        from integrations.channels.memory.shared_history import install_history_writeback
        for gc in [group_chat, group_chat_1, group_chat_2]:
            install_history_writeback(
                gc, user_id, simplemem_store,
                extra_sinks=[_graph_sink] if memory_graph is not None else None,
                simplemem_metadata={'prompt_id': prompt_id})
    except Exception:
        current_app.logger.debug(
            'group-chat history write-back skipped', exc_info=True)

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

    return assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group


# Re-steer budget per (user_prompt, action_id) for the fabrication gate in
# _advance_reuse_action.  Each refusal steers the agent to actually call the
# unrun tool; after _REUSE_FAB_STEER_MAX the gate advances anyway (loudly), so
# it can never loop or permanently stall an action.
_reuse_resteer_counts = {}
_REUSE_FAB_STEER_MAX = 3

# The MIRROR of the fabrication gate.  That gate catches an action CLAIMING
# completion whose tool never ran.  This one catches the opposite, measured
# live 2026-09-05 on Scout2 (77712340019): google_search really ran (three
# HTTP 200s to brave/wikipedia/grokipedia at 19:45:24-25) and the agent wrote
# the correct 2-bullet summary from the results — then reported
# {'status': 'pending', 'action_id': 1}.  Every advance site gates on
# status == 'completed', so none fired, the turn ended on '@user', and
# current_action_id never left 1.  Five agents in the sweep sit at 1/N this way.
#
# An under-reporting model must not strand a finished action, but "pending"
# is also the honest answer when an action genuinely needs another tool call.
# So steer first, bounded, and only then proceed — and only ever when the
# action's own tools are EVIDENCED to have executed (_reuse_fabricated_tools
# returns nothing outstanding).  Advancing without that evidence would be the
# "force-completed by a nudge" the verification contract forbids.
_reuse_pending_counts = {}
_REUSE_PENDING_STEER_MAX = 2

# The StatusVerifier's not-done verdicts.  'pending' was the only one this
# branch knew; 'requires_breakdown' strands an action exactly the same way and
# was measured doing it live 2026-09-06 on agent 89555447799 (24-action
# recipe): action 1 is autonomous, its google_search really executed ("INSIDE
# google search"), and the verifier answered {'status': 'requires_breakdown',
# 'action_id': 1} 35 times.  The turn ran 107 rounds / 40 minutes and never
# left action 1 — `Retrieved current_action_id: 1 for session:
# cf125371-..._89555447799` 175x, and zero [REUSE]/advance markers.
#
# Speaker routing already handles all four statuses (:2635 persists
# requires_breakdown's subtasks, :2646 routes error/pending to the helper), so
# the gap was only here, on the ADVANCE side.  The consequence is worse than
# one slow action: the sole bound was the WHOLE-TURN round budget
# (max(4, n_actions*4+4) = 100 for this recipe), so one wedged action consumed
# the entire turn and the remaining 23 actions never ran.
#
# 'error' is deliberately NOT here.  It reports a failure, not an
# under-reported success — advancing past it would bury the failure.
#
# The tokens live in core.constants, not here: this file spells the same
# StatusVerifier vocabulary at 11 other sites and create_recipe.py spells it
# at 5 more, so a set defined locally would be a 17th private copy of a
# shared vocabulary.  Only the GROUPING is local — see each constant's own
# comment for why these groupings must stay different:
#   :2646                            error+pending          — speaker routing
#   _REUSE_UNDERREPORT_STATUSES      pending                — advancing
#   VERDICT_ROUND_TERMINAL_STATUSES  completed+breakdown    — ending the round
#   VERDICT_COMPLETION_STATUSES      completed+success+done — action finished
#
# The last one was the 2026-09-07 migration of five of those "11 other
# sites" (the completion readers at :2821, :3865, :3882, :4841, :4855).
# Each asked the pure "is this action finished" question and advanced the
# action pointer, so they were the sites a shared token set fits without
# changing any grouping — and they were dropping the model's 'done'
# spelling, 6 verdicts of 22 measured live.
from core.constants import (
    VERDICT_ROUND_TERMINAL_STATUSES,
    VERDICT_UNDERREPORT_STATUSES as _REUSE_UNDERREPORT_STATUSES,
)


# The harness's own voice inside the group.  All three reuse stacks name their
# steering UserProxy "ChatInstructor" (reuse_recipe :1531 main, :2144 timer,
# helper.py :2854 visual), and every steering site in this file initiates
# through it — the three `chat_instructor.initiate_chat(recipient=manager...)`
# calls say so explicitly.  Its messages are instructions TO the group, so they
# can never be the group's answer, however they are worded.
_REUSE_STEER_INITIATOR_NAMES = ("ChatInstructor",)

# How every action dispatch this module posts begins.  ONE definition:
# `_build_reuse_action_message` emits it and `_reuse_message_is_user_answer`
# refuses it, so rewording the dispatch moves both at once.
#
# WHY A PREFIX AND NOT JUST THE SEAT NAME.  The seat check above reads
# `msg['name']`, which is right for the wire body but NOT for
# `group_chat.messages`: the #725 sync slice-assigns the longest per-agent
# buffer out of `manager._oai_messages`, and in that buffer the same dispatch
# can arrive under another agent's name.  Measured live 2026-09-10 04:42:04 —
# the answer-recovery walked the resynced 12-entry buffer, the dispatch there
# was NOT name='ChatInstructor', it passed the shape test as ordinary prose,
# and the user's whole reply was
#   "Perform this action -> Action #4:Return the summarized text to the user."
# Keying on the producer's own literal is what the seat-name comment asked
# for ("keyed on WHO, not on the wording") and what the name field could not
# deliver across the sync.
_REUSE_ACTION_MESSAGE_PREFIX = 'Perform this action -> Action #'


# How every refusal re-steer opens — BOTH branches of
# _reuse_fab_steer_message ("...the tool(s) X did not produce a real result"
# and "...it produced no output").  One marker, emitted by the producer, so
# the recogniser below cannot drift from the wording.
_REUSE_NOT_COMPLETE_MARKER = ' is NOT complete: '

# PRODUCER FOUR.  Posted into the group to tell the agent to proceed without
# asking the user; never an answer to anyone.  Measured live 2026-09-10
# 12:55:21 (agent 77712340019, "google_search then send a 2-bullet summary"):
# google_search really ran and the action advanced honestly, then
# _reuse_written_answer recovered THIS sentence from the group log and the
# user received all 101 characters of it as the summary --
#   [SYNTHESIS] the action already wrote the answer - recovered 101 chars ...
#   head='You should complete this task independently. Feel free to make ...'
# A constant, not a literal at the post site, because the predicate below can
# only recognise what the producer actually emits -- which is exactly how this
# family "kept being one producer behind".
_REUSE_AUTONOMY_NUDGE = (
    'You should complete this task independently. Feel free to make '
    'reasonable assumptions where necessary'
)

# PRODUCER FIVE.  The UNDER-REPORTED steer: posted when an autonomous action
# reports not-done while its tools have evidently run.  Measured live
# 2026-09-10 14:23:10 (agent 92583386981, "English Learning Session"), where
# the agent HAD done its work --
#   14:23:16  Tier-1 named attach: action 1 names ['get_chat_history'] -> 1 tools
#   14:23:43  [FAB-GUARD] action 1 ... executed=['get_chat_history', ...]; unrun=[]
# -- and the user received this steer, all 197 characters of it, as the lesson.
_REUSE_UNDER_REPORT_STEER = (
    'Your tool for this action has already executed and returned its result. '
    'Do not re-run it. Either report this action completed, or state the '
    'exact remaining step that still needs a tool call.'
)

# PRODUCER SIX, closed at the same time rather than waiting for its own
# incident.  The BREAKDOWN steer interpolates the subtask description, so the
# marker is the fixed opening; containment then covers every subtask.
_REUSE_SUBTASK_STEER_PREFIX = 'Work on subtask: '


def _reuse_is_pipeline_text(content):
    """True when *content* is text THIS MODULE wrote to steer the group.

    Never an answer to the user, whatever it says.  Three producers, and the
    gap each one opened was found the hard way:

      _build_reuse_action_message   the action dispatch                 04:42
      _reuse_seed_message           user's words + the dispatch          10:05
      _reuse_fab_steer_message      the fabrication gate's re-steer      10:22

    all three measured live on 2026-09-10 being delivered to the user as the
    reply.  The third was reachable only after 800c6eb53 made the gate refuse
    prose actions, but the exposure was already there for its tool branch —
    a refused action leaves its re-steer as the tail, and the extractor takes
    the tail.

    Was `_reuse_is_pipeline_text`, which named one producer and so kept
    being one producer behind.  The durable question is "did this module
    write this", and the answer is a marker per producer, each emitted by
    that producer.

    CONTAINMENT, not `startswith`, because the producer does not always put
    its marker first.  There are two producers and they compose it
    differently:

      _build_reuse_action_message   the dispatch alone — actions 2..N
      _reuse_seed_message           ``f"{message}\\n\\n{dispatch}"`` — the
                                    opening turn puts the USER'S WORDS first

    Measured live 2026-09-10 10:05:25 (agent 88094979291): the second shape
    reached the user verbatim, all 904 characters of it —

        Summarize a given text into exactly three bullet points.

        Perform this action -> Action #1:Receive the input text from the user.
         follow these steps: [{... 'code': "def extract_user_input(): ..."}]

    — because a prefix test cannot see a marker that sits in the middle.  The
    93fdaac3f refusal closed the first shape and left the second open; the
    question both readers ask is "did this module write this text", and that
    has one answer wherever the producer chose to put it.

    ONE implementation, two callers (_reuse_message_is_user_answer and
    _reuse_written_answer).  They already shared the constant; sharing only
    the constant is what let one be fixed while the other kept the hole.

    A genuine answer that quotes the dispatch back is refused too, and that
    is correct — echoing the dispatch is exactly the 04:42:04 regression
    this family exists to stop.

    What this must NOT swallow is an honest failure report.  "Since no
    specific text was provided in your input, I could not summarize
    anything." (measured 10:21:11) is the agent talking to the user and has
    to reach them; only the module's own markers are refused, never the
    sentiment.
    """
    _c = str(content or '')
    return (_REUSE_ACTION_MESSAGE_PREFIX in _c
            or _REUSE_NOT_COMPLETE_MARKER in _c
            or _REUSE_AUTONOMY_NUDGE in _c
            or _REUSE_UNDER_REPORT_STEER in _c
            or _REUSE_SUBTASK_STEER_PREFIX in _c
            # Both synthesis steers, via the ONE shape they each append —
            # _REUSE_SYNTHESIS_STEER and _REUSE_SYNTHESIS_STEER_INCOMPLETE
            # end `+ _REUSE_SYNTHESIS_ANSWER_SHAPE`, and the INCOMPLETE one
            # is .format(unrun=...)ed before posting, which leaves the shape
            # untouched (it carries no braces, by its own design note).  So
            # one marker covers both producers and survives the formatting.
            # It is defined further down, beside the steers that emit it —
            # resolved at call time like every other global here.
            or _REUSE_SYNTHESIS_ANSWER_SHAPE in _c)

# Recorded in _reuse_fab_pending when the thing that did not happen is not a
# tool run but the ACTION'S OWN TEXT.  Angle brackets, so _TOOL_IDENT_RE can
# never match it and it can never collide with a registered tool name sharing
# that record.  Internal only — _reuse_fab_steer_message never shows it.
_REUSE_NO_OUTPUT_SENTINEL = '<no output produced>'


def _reuse_action_declares_tool(user_prompt, action_id):
    """True when this action's recipe names a tool to call.

    Reads the SAME authored ``recipe[].tool_name`` that
    ``_build_reuse_action_message`` renders into the dispatch, so an action's
    shape is decided by what CREATE wrote, not by a second guess at what a
    tool action looks like.

    Why it matters: ``_reuse_fabricated_tools`` answers "did the NAMED tools
    execute", and says in its own docstring that it never touches "prose
    actions that name no tool".  For an action whose deliverable IS the text
    there is nothing for it to check, so `completed` rests on the model's
    word.  This predicate is what tells the two shapes apart at the gate.

    Unknown session / malformed recipe -> False, i.e. treated as a text
    action and therefore held to the stricter evidence rule.  An action we
    cannot classify is never given the weaker check.
    """
    try:
        actions = (recipes.get(user_prompt) or {}).get('actions') or []
        steps = (actions[action_id - 1] or {}).get('recipe') or []
        return any(str((s or {}).get('tool_name') or '').strip()
                   for s in steps)
    except Exception:
        return False


def _reuse_group_terminate(msg):
    """End an action's group-chat round on a verdict the OUTER loop must act on.

    Module-level, not a closure, for two reasons: it needs nothing from the
    enclosing scope, and as a closure its contract could only ever be guarded
    by string-matching the source — which is exactly how the
    requires_breakdown gap below survived a guard test that already existed.

    Each reuse action runs as an autogen group chat.  ``initiate_chat``
    returns only when this predicate says the round is over; until then the
    group talks to itself up to ``max_round``.  Everything the w1/w2 loop does
    between actions — advancing, executing a decomposition, steering — is
    therefore unreachable for any verdict this predicate does not recognise.

    Two live measurements, both the same defect in this one function:

      2026-09-05  'completed' was not recognised.  The manager fell back to
                  the default (content == 'TERMINATE'), which a
                  human_input_mode='NEVER' UserProxy does not reliably emit,
                  so the group spun to max_round=10.  Auto Research
                  18088688973: verdict at 03:29:12, current_action_id still 1,
                  actions 2-6 never ran.

      2026-09-06  'requires_breakdown' was not recognised, identically.
                  Agent 89555447799 (24 actions): 17 requires_breakdown
                  verdicts for action 1, 0 breakdown executions, 0 advances.
                  The breakdown-execution block had already shipped and was
                  PROVEN loaded on the running process (py-spy frame
                  `get_agent_response (hartos\\reuse_recipe.py:3662)` matches
                  the patched copy's line numbering; the unpatched copies put
                  that def at 3280) — it sits in the loop that never regained
                  control, so it could not run.

    The membership set is canonical (core.constants), so adding a third
    round-terminal verdict is a one-line change there, not a fourth private
    spelling here.  Fails closed: anything unparseable is NOT terminal.

    THIRD MEASUREMENT, 2026-09-09, agent 33323830039 — the ANSWER is terminal
    too, and for the identical reason.  The synthesis steer (#799/D33) asked
    the group for the user-facing reply and got it:

        04:38:12  last json as {'message2userfinal': "The chatterbox_turbo
                  worker startup failure has been diagnosed... The directory
                  check confirmed the existence of the 'Nunba' folder..."}

    grounded in the `dir` output its own tool had returned.  Nothing ended the
    round, so the group talked for four more turns and closed on a verdict:

        04:38:48  [SYNTHESIS] round returned (23 -> 23 msgs);
                  still control JSON: True

    and the user got the verdict.  The answer was TALKED OVER.  get_agent_
    response ALREADY treats message2userfinal as turn-ending — its in-loop
    branch unwraps it and returns it as the reply — but it only ever SEES it
    when the answer happens to be messages[-1] at the moment initiate_chat
    returns.  So this is the existing rule applied where it decides anything,
    not a new one.

    Two guards keep it from firing on the ASK instead of the answer, both from
    the same window:
      * the steer carries the example `{"message2userfinal": "<your answer
        here>"}`, and the pipeline's own parser read it as such at 04:37:58 —
        so a placeholder or empty value is not an answer;
      * ChatInstructor is this file's canonical steering initiator (its three
        initiate_chat sites say so), so its messages are instructions to the
        group and can never be the group's answer.
    """
    if _is_terminate_msg(msg):
        return True
    try:
        _vj = retrieve_json((msg or {}).get('content') or '')
        if not isinstance(_vj, dict):
            return False
        if (str(_vj.get('status', '')).lower()
                in VERDICT_ROUND_TERMINAL_STATUSES):
            return True
        if str((msg or {}).get('name') or '') in _REUSE_STEER_INITIATOR_NAMES:
            return False
        for _k, _v in _vj.items():
            if str(_k).lower() != 'message2userfinal':
                continue
            return _reuse_is_written_answer(_v)
        return False
    except Exception:
        return False


def _reuse_is_written_answer(value):
    """True when a message2userfinal VALUE is text a user can read.

    Was inline in _reuse_group_terminate only, so _reuse_needs_synthesis --
    asking the same question -- checked the KEY instead of the VALUE.  Live
    2026-09-09 18:25:22 (agent 33323830039) that gap sent the steer's own
    template as a whole 313-second turn's reply: `REPLY: <your answer here>`,
    len=18, logged as "still control JSON: False".  One function, both
    callers.  Matches `<...>` as a shape, not a specific sentence.
    """
    _v = str(value or '').strip()
    return bool(_v) and not (_v.startswith('<') and _v.endswith('>'))


def _reuse_evidence_count(group_chat):
    """How many tool RESULTS are visible in this group chat's evidence.

    The round budget exists to stop stalls, and this is how a loop tells a
    stall from work.  It counts RESULTS — ``role == 'tool'`` messages — not
    tool_call entries: a tool_call is the model ASKING, and a model that
    keeps asking without ever running anything is precisely the stall the
    budget is there to end.  That is the same distinction the fabrication
    gate draws (see `_record_result`), and it reads the same message lists
    via `_reuse_evidence_msg_lists`, so there is no second notion of
    evidence.  Placeholders are excluded for the gate's reason: the stand-in
    is minted BECAUSE nothing executed.

    Returns -1 when it cannot be measured, which callers compare with `>` so
    an unmeasurable state can never be read as progress and extend a budget
    forever.  Never raises: it runs inside the loop that produces the reply.
    """
    try:
        n = 0
        for _ml in _reuse_evidence_msg_lists(
                group_chat, getattr(group_chat, 'agents', None) or []):
            for m in (_ml or []):
                if not isinstance(m, dict) or m.get('role') != 'tool':
                    continue
                if HISTORICAL_TOOL_PLACEHOLDER in str(m.get('content') or ''):
                    continue
                n += 1
        return n
    except Exception:
        return -1


def _reuse_own_tool_progress(user_prompt, action_id, group_chat, agents):
    """How many of THIS action's OWN named tools produced a real result.

    The round budget asks "is the action moving?".  ``_reuse_evidence_count``
    answers "did ANY tool anywhere produce a result", which is a different
    question, and the gap between them is a live spin: measured 2026-09-10,
    agent 88719487304 action 9 ("Deliver the final research summary ... to
    the user"), 22:12:50 -> 22:21:47.  The model called get_data_by_key,
    save_data_in_memory and txt2img -- image generation, on a delivery
    action -- while the action's own send_message_to_user and
    save_to_long_term_memory never ran once.  Every unrelated call raised
    the global count, reset the allowance and bought another window: 17
    rounds, 53 tool calls, `unrun` never shrinking, nothing delivered.

    Returns None when the action names NO tool.  There the global count is
    the only progress signal available, so the caller keeps today's
    behaviour (D46 already governs whether a prose action may complete).
    Returns -1 when unmeasurable; callers compare with `>` so uncertainty
    can never read as progress and extend a budget forever.

    Reuses the gate's own primitives -- ``_reuse_registered_and_referenced_tools``
    for "which tools does this action name", ``_reuse_evidence_msg_lists`` +
    ``_reuse_call_id_to_tool_name`` for "what really ran", and the same
    placeholder/failure exclusions ``_record_result`` applies.  This is the
    gate's notion of evidence narrowed by NAME, not a second notion of it.
    """
    try:
        task = user_tasks.get(user_prompt)
        text = str(task.get_action(int(action_id) - 1) or '').lower() if task else ''
        if not text:
            return -1
        _all_names, referenced = _reuse_registered_and_referenced_tools(agents, text)
        if not referenced:
            return None
        wanted = set(referenced)
        msg_lists = _reuse_evidence_msg_lists(group_chat, agents)
        call_fn = _reuse_call_id_to_tool_name(msg_lists)
        ran = set()

        def _credit(call_id, content, fallback_name):
            body = str(content or '')
            # Same two exclusions the gate draws: the placeholder is minted
            # BECAUSE nothing executed, and a tool that ran and reported it
            # could not do the work has not advanced this action either.
            if HISTORICAL_TOOL_PLACEHOLDER in body:
                return
            if any(f in body for f in TOOL_FAILURE_RESULTS):
                return
            fn = call_fn.get(call_id) or fallback_name
            if fn in wanted:
                ran.add(fn)

        for _ml in msg_lists:
            for m in (_ml or []):
                if not isinstance(m, dict) or m.get('role') != 'tool':
                    continue
                # Both live shapes, exactly as _reuse_fabricated_tools reads
                # them: the aggregate envelope with per-call entries under
                # `tool_responses`, and the flat per-call message.
                responses = m.get('tool_responses')
                if isinstance(responses, list) and responses:
                    for r in responses:
                        if isinstance(r, dict):
                            _credit(r.get('tool_call_id'), r.get('content'),
                                    r.get('name'))
                else:
                    _credit(m.get('tool_call_id'), m.get('content'), m.get('name'))
        return len(ran)
    except Exception:
        return -1


def _reuse_current_action_id(user_prompt):
    """The action the pipeline believes it is on, or None if unreadable.

    Both loops read ``user_tasks[user_prompt].current_action`` in a dozen
    places behind bare subscripts; this is the guarded read the round-budget
    counters use, so a missing session cannot raise on the budget path (the
    same failure mode the canonical `_reuse_action_is_autonomous` reader was
    added for: a KeyError there skipped the steering for that whole round).
    """
    try:
        return user_tasks[user_prompt].current_action
    except Exception:
        return None


# Rounds ONE action may spend before the turn ends.  Measured, not chosen —
# see _reuse_turn_round_budget's docstring for the 05:29:49-05:30:49 count.
# Both loops check it per action AND derive the turn ceiling from it, so the
# per-action allowance and the turn total can never drift apart.
_REUSE_ROUNDS_PER_ACTION = 12


def _reuse_turn_round_budget(user_prompt):
    """Iteration budget for ONE reuse turn, derived from the recipe it runs.

    Both reuse loops used a literal ``if count == 4: break``.  That budgets the
    whole turn for what a SINGLE action may legitimately need: the fabrication
    gate allows ``_REUSE_FAB_STEER_MAX`` (3) re-steers per action before it
    advances anyway, so one action can honestly consume 4 rounds.

    Measured live 2026-09-05 driving every saved agent through /chat: six
    agents stopped at EXACTLY action 4 regardless of how long their recipe
    was — 4/24, 4/15, 4/15, 4/6, 4/6, 4/6 — and the only two that "finished"
    were the two whose recipes are SHORTER than the cap (4/3 and 4/2).  Their
    logs carry `state_transition with action id 1..4`, so advancement was
    working; the turn simply ran out of rounds.  75 of the 127 saved agents
    have >= 2 actions and the largest has 24, so a fixed 4 truncates most of
    the population.

    Budget per action what one action may need, plus a small constant so a
    single-action recipe keeps its previous headroom.  The loop's other exits
    (budget exhausted, SLA breach, empty history, '@user') are unchanged —
    this only stops the counter from ending a turn that is still progressing.

    SIZED FROM A LIVE ACTION, 2026-09-09 05:29:49-05:30:49 (agent
    33323830039).  Action 1 did its real work and needed, counted from the
    log: the ordinary group rounds, ONE fabrication refusal
    (`[FAB-GUARD] ... unrun=[...]` at 05:30:01) and ONE under-report re-steer
    (`[UNDER-REPORTED] ... attempt 1/2` at 05:30:43) before it advanced
    honestly at 05:30:49 — 16 while1 iterations, 12 counted rounds.  The old
    per-action term was 4.  So an action that WORKS legitimately needs about
    three times what the formula allowed it, which is why both drives that
    day ended on `exhausted 12 rounds at action 2/2`: the turn total and one
    action's honest need were the same number.

    _REUSE_ROUNDS_PER_ACTION is that measured need, and the turn ceiling is
    n_actions of them — one constant, used by the per-action check in both
    loops and by this ceiling, so the two cannot drift.

    COST, stated plainly: a turn may now run n_actions times longer than
    before in the worst case (that drive: 141s at 12 rounds, so ~12s/round).
    The trade is deliberate — an agent whose later actions can never be
    reached does not do its job at all.
    """
    try:
        n_actions = len(user_tasks[user_prompt].actions)
    except Exception:
        n_actions = 1
    return max(1, n_actions) * _REUSE_ROUNDS_PER_ACTION

# Unrun tool names recorded by the fabrication gate for the refusal it just
# returned, keyed the same way, and consumed by _reuse_fab_steer_message.
_reuse_fab_pending = {}


def _reuse_fab_steer_message(user_prompt, current_action_id):
    """Re-steer text for a fabrication refusal, or None if it was not one.

    _advance_reuse_action returns (None, False) for BOTH "this action's tool
    never ran" and "all actions done / state error", so every caller treated a
    refusal as done and ended the turn with an empty reply (review 2026-09-03
    #1).  Popping the pending refusal here tells the two cases apart and gives
    the caller a message that names the tool the agent must actually call.
    """
    tools = _reuse_fab_pending.pop((user_prompt, current_action_id), None)
    if not tools:
        return None
    if list(tools) == [_REUSE_NO_OUTPUT_SENTINEL]:
        # The refusal was recorded by the text half of the gate: this action
        # names no tool, so "@Helper call <names>" would name nothing and
        # prescribe a step that does not exist for it.  Ask for the one thing
        # that IS missing — the action's own output, written where the user
        # can read it.  The sentinel itself never appears in this text; it is
        # only how the two halves share one pending record.
        return (
            f"Action {current_action_id}{_REUSE_NOT_COMPLETE_MARKER}it produced no "
            f"output. This action calls no tool — its result IS the text you "
            f"write — and nothing was written for the user in this action. Do "
            f"not report this action as completed. Write the action's actual "
            f"result now, in full, as your reply. If you cannot produce it, "
            f"say plainly what is missing instead of claiming success."
        )
    names = ', '.join(str(t) for t in tools)
    # The guard now holds an action for TWO causes -- the tool was never
    # called, and the tool ran but returned one of TOOL_FAILURE_RESULTS -- so
    # this text may no longer assert non-invocation.  It said "were never
    # actually called", which for the second cause tells the model something
    # false and prescribes the wrong remedy: call it again, unchanged, and it
    # fails again the same way (live 2026-09-07: the VLM computer-use loop
    # exhausted max_iterations trying to focus a window that is not open).
    # State the property the guard actually established -- no real result --
    # and require the failure to be reported rather than dressed as success.
    return (
        f"Action {current_action_id}{_REUSE_NOT_COMPLETE_MARKER}the tool(s) {names} did "
        f"not produce a real result — either they were never called, or they "
        f"ran and returned a failure. Do not report this action as completed. "
        f"@Helper call {names} now with real arguments and report the actual "
        f"returned result verbatim. If the tool reports a failure, say so "
        f"plainly and do not claim the action succeeded."
    )


def _reuse_action_is_autonomous(user_prompt, action_id):
    """True when the recipe marks this action runnable without the user.

    Reads the same ``can_perform_without_user_input`` field the recipe author
    writes and the prompt at L1296 instructs the model to honour.  Absent or
    unparseable -> False, so an unknown action is never auto-advanced.
    """
    try:
        action = user_tasks[user_prompt].actions[action_id - 1]
        return str(action.get('can_perform_without_user_input', '')).lower() == 'yes'
    except Exception:
        return False


# How far back to look for the StatusVerifier's verdict.  Bounded because
# clear_history=False makes these lists thousands of entries long, and an
# unbounded scan would resurrect an ancient verdict.
_REUSE_VERDICT_TAIL_SCAN = 12


def _reuse_latest_verdict(group_chat):
    """The most recent StatusVerifier verdict in the group log, or None.

    The under-report escape below used to read ONLY ``messages[-1]``, and that
    position is structurally never the verdict.  Measured live 2026-09-08
    22:18-22:43 (agent 89555447799, action 4): of 128 state_transition calls in
    the wedge, 120 saw "You should" there -- the ChatInstructor nudge.  The
    #725 sync explains why it cannot be anything else:

        [725-SYNC-COMPOSITION] (*=picked)
          User*:n=240,calls=100,answers=0 | Assistant:n=120,answers=0 |
          Helper:n=240,answers=0 | ... | Assistant:n=120,calls=42,answers=30

    the sync picks the LONGEST buffer (User), whose tail is the steering nudge;
    the only buffer holding tool answers is half its length and never picked
    (#789/D22).  So the escape's precondition could not be satisfied, and it
    fired 0 times in this drive and 0 times across every retained log -- while
    40 real verdicts for that one action went by.  Cost: a 23-minute wedge, the
    round budget drained (rounds 4 -> 85), the user's /chat timing out with no
    reply for work the machine had actually completed, and ~8 browser windows
    opened by the retry loop.

    Deliberately does NOT filter on the verdict's ``action_id``.  Measured over
    the same 72 verdicts: 25 of 65 for action 4 carried ``action_id: 24`` -- the
    recipe's TOTAL action count -- while their own text read "Action #4".
    Filtering on that integer would discard 38% of genuine verdicts and could
    credit them to action 24.  This file already treats the model's id as
    advisory (``_advance_or_steer``'s ``claimed_action_id`` and its
    [HALLUCINATION?] log); the caller keys on the PIPELINE's current_action.

    Scoping to the current action is likewise NOT done here, because it is
    already done downstream: the caller's tool-evidence check
    (``_reuse_outstanding_tools``) asks whether THIS action's tools ran since
    THIS action was dispatched, which 209478af5 made per-action correct.  A
    stale verdict from an earlier action therefore cannot advance the current
    one -- that check blocks it -- so no second scoping scheme is needed.

    Uses ``retrieve_json``, the canonical parse (#95), like every sibling
    verdict reader.  Never raises: this runs inside the live turn loop.
    """
    try:
        messages = list(getattr(group_chat, 'messages', None) or [])
    except Exception:
        return None
    for msg in reversed(messages[-_REUSE_VERDICT_TAIL_SCAN:]):
        if not isinstance(msg, dict):
            continue
        content = msg.get('content')
        if not isinstance(content, str) or not content.strip():
            continue
        try:
            parsed = retrieve_json(content)
        except Exception:
            continue
        if isinstance(parsed, dict) and 'status' in parsed:
            return parsed
    return None


# Prefixes that mean a message is addressed to ANOTHER AGENT, not the user.
# Lifted out of get_agent_response's inline `agent_mentions` list so the
# synthesis gate asks the same question the loop already asks, instead of
# growing a second notion of "who is this message for".
_REUSE_AGENT_MENTIONS = (
    "@statusverifier", "@status verifier", "@verification",
    "@helper", "@executor",
)


def _reuse_needs_synthesis(group_chat):
    """True when the turn is about to answer the user with something that is
    NOT AN ANSWER.

    WIDENED after the 2026-09-09 04:48-04:51 drive.  The first cut asked "is
    messages[-1] control JSON?", which is only ONE of the ways the tail fails
    to be an answer.  That drive ended on
    `[REUSE-ROUNDS] while1 exhausted 12 rounds at action 2/2`, the tail was
    NOT control JSON, the gate returned False, `[SYNTHESIS]` logged 0 times —
    and the user received a 5,078-char essay that diagnosed the agent from its
    NAME ("chatterbox_turbo ... implies a high-velocity worker") and prescribed
    docker/kubectl remediation on a Windows desktop, ignoring the `dir` output
    its own tool had just produced.

    So the predicate is now the property that actually matters: the tail is not
    a reply TO THE USER.  Each branch is a shape measured on this pipeline:
      * empty / whitespace           — nothing to say
      * 'TERMINATE'                  — the control token
      * role == 'tool'               — a tool RESULT, not a reply
      * <tool_call> / <function=…>   — an UNEXECUTED call in the model's own
                                       syntax; measured 05:31:01 as the WHOLE
                                       reply, the user's home path in it
      * @Helper / @StatusVerifier /… — addressed to another agent (same list
                                       the loop's own routing uses)
      * dict carrying 'status'       — a StatusVerifier verdict (the original
                                       case, measured 04:14:22)
    Prose with none of those is a real answer and is left alone — anti-vacuity
    matters here, because a false positive burns a group round on every good
    turn.

    WHY THE TAIL IS STRUCTURALLY NOT AN ANSWER (#799/D33, the original case):

    ``_reuse_group_terminate`` ends the round ON the StatusVerifier verdict, so
    ``messages[-1]`` is STRUCTURALLY that verdict whenever an action completes.
    The post-loop extractor only unwraps ``message2userfinal`` / ``message2``;
    anything else falls through to ``return last_message['content']`` — i.e. the
    raw verdict is handed to the user.

    Measured live 2026-09-09 04:14:22 (agent 33323830039), the whole reply:

        {"status": "completed", "action": "Action #1: cd C:\\\\Users\\\\sathi\\\\
         Documents && dir Nunba", "action_id": 1, "message": "Action completed
         successfully. The directory listing ... was retrieved."}

    The `response_format` at L1315 asks for ``message2userfinal`` and the agent
    prompt instructs it, but nobody is ever given a turn to produce it once the
    verdict has ended the round — producer contract and consumer both ship, and
    the step between them does not exist (#799/D33).

    NOT fixable by walking back to an earlier message: measured on the same
    turn, the closing history is tool traffic only —
    ``[725-SYNC-COMPOSITION] User*:n=10,calls=7,answers=0 |
    Assistant:n=3,calls=1`` — the assistant entries are ``name=unknown`` with
    ``tool_calls`` and no prose, and the only non-tool entries are a
    ChatInstructor nudge and a bare user-role line.  There is no synthesis to
    recover; it has to be asked for.

    CORRECTION 2026-09-10 — "not fixable by walking back" was true of THAT
    turn, not of the pipeline.  Agent 88094979291 closed with its finished
    answer at ``messages[-2]``; see ``_reuse_written_answer``, which recovers
    it and is bounded so the 33323830039 shape still reaches the steer.

    The per-message shape test now lives in ``_reuse_message_is_user_answer``
    so the recovery asks exactly the question this gate asks.
    """
    try:
        messages = list(getattr(group_chat, 'messages', None) or [])
        if not messages:
            # Nothing to synthesise FROM.  The extractor's own empty-history
            # guard handles this; asking the group would only add a round.
            return False
        return not _reuse_message_is_user_answer(messages[-1])
    except Exception:
        return False


def _reuse_message_is_user_answer(message):
    """True when *message* is a reply the USER can read.

    The shape list is the one documented on ``_reuse_needs_synthesis`` above,
    in positive form, factored out so the gate ("is the tail an answer?") and
    the recovery ("did this action already write one?") cannot grow two
    different notions of it.  Every shape was measured on this pipeline one
    live failure at a time; a second copy would miss the next one.

    Fails CLOSED — a message this cannot judge is not an answer.  For the gate
    that still means "leave the tail alone" (its caller's except returns
    False); for the recovery it means "never deliver what we cannot read".
    """
    try:
        last = message or {}
        content = last.get('content')
        if not isinstance(content, str) or not content.strip():
            return False                     # empty is not an answer
        low = content.lower()
        if _reuse_is_pipeline_text(content):
            # THIS MODULE POSTED IT.  Hoisted ABOVE the message2userfinal
            # branch on 2026-09-10: every synthesis steer NAMES that key by
            # construction (_REUSE_SYNTHESIS_ANSWER_SHAPE tells the model to
            # use it), and that branch answers True for any text it cannot
            # parse as JSON — so this check could never be reached for the
            # one family it most needed to refuse.  Measured live 14:30:55,
            # agent 92583386981: the steer was the tail, was called an
            # answer, logged the reassuring "still control JSON: False", and
            # was delivered to the user as the whole reply.
            #
            # Also still ABOVE the seat name below, because the seat name
            # does not survive the #725 sync — see the constant's comment for
            # the 2026-09-10 04:42:04 measurement.
            return False
        if 'message2userfinal' in low or 'message2' in low:
            # The KEY present is not the ANSWER present: live 18:25:22 the
            # model returned the steer's template and this branch called it
            # "already there".  Unparseable / key-absent keeps the old
            # "answer", so this narrows the gate, it does not re-open
            # synthesis.
            _ans = retrieve_json(content)
            if isinstance(_ans, dict):
                for _ak in ('message2userfinal', 'message2'):
                    if _ak in _ans:
                        return _reuse_is_written_answer(_ans[_ak])
            return True                      # the answer is already there
        if str(last.get('name') or '') in _REUSE_STEER_INITIATOR_NAMES:
            # THE LOOP'S OWN STEER.  This seat exists to steer; it never
            # speaks TO the user, so whatever it said is plumbing.  Measured
            # live 2026-09-09 08:53:22, delivered verbatim as the answer:
            #   "Perform this action -> Action #2:cd C:\\Users\\sathi\\Documents
            #    |  follow these steps: [{'cd C:\\\\Users\\\\sathi\\\\Documents':
            #    {'tool_name': 'execute_windows_or_android_command',
            #    'code': None}}]"
            # (tail confirmed as `Message[14]: role=user, name=ChatInstructor`,
            # `last_speaker ChatInstructor`).  It matched none of the shapes
            # below — prose, role='user', no '@' mention, and retrieve_json of
            # its trailing "[{...}]" yields a LIST not a status dict — so the
            # gate fell through to "prose for the user" and the extractor
            # handed it over.
            #
            # Keyed on WHO, not on the wording: matching the sentence would
            # break the moment a steer is reworded, and there are several.
            # Same constant _reuse_group_terminate:3307 already reads for the
            # same semantic ("the steer's own voice is not a terminal
            # answer") — one notion of it, not two.  Deliberately BELOW the
            # message2userfinal check: if a steer-seat message ever does carry
            # the answer key, it IS the answer.
            return False
        if content.strip() == 'TERMINATE':
            return False                     # control token
        if last.get('role') == 'tool':
            return False                     # a tool RESULT, not a reply
        if '<tool_call>' in low or '<function=' in low:
            # An unexecuted tool call in the model's own call syntax.  The
            # WHOLE reply at 05:31:01 was this, user's home path included:
            #   <tool_call>\n<function=execute_windows_or_android_command>
            #   \n<parameter=instructions>\ncd C:\\Users\\sathi\\Documents...
            # Markup, not a sentence — matching the SYNTAX, never prose that
            # merely names a tool, which stays a legitimate answer.
            return False
        if any(m in low for m in _REUSE_AGENT_MENTIONS):
            return False                     # addressed to another agent
        parsed = retrieve_json(content)
        if isinstance(parsed, dict) and 'status' in parsed:
            return False                     # StatusVerifier verdict
        return True                          # prose for the user
    except Exception:
        return False


def _reuse_written_answer(group_chat):
    """The answer THIS action already wrote, or None.

    MEASURED live 2026-09-10 03:37:33 (agent 88094979291, "summarize into
    exactly three bullet points").  The closing history was:

        Message[10]  user      ChatInstructor   "Perform this action ->
                                                 Action #4: Return the
                                                 summarized text ..."
        Message[11]  assistant Assistant        "... into exactly three bullet
                                                 points for you: • ... • ... •"
        Message[12]  user      StatusVerifier   {"status": "completed", ...}

    The deliverable was at ``messages[-2]`` and the synthesis round replaced
    it with 426 characters of prose carrying zero bullets (rowid 206313, what
    the user actually read).  ``_REUSE_SYNTHESIS_STEER`` says "in your own
    words" — for an agent whose whole goal IS the format, being asked again is
    what destroys the answer.

    BOUNDED AT THIS ACTION'S OWN DISPATCH.  The walk stops at the first
    ``_REUSE_STEER_INITIATOR_NAMES`` seat going backwards, which is the
    "Perform this action -> Action #N" that opened the action.  Anything
    before it belongs to an earlier action — or to an earlier TURN, since
    every steer runs with ``clear_history=False`` — and delivering that would
    answer a question the user did not ask.

    Not a replacement for the steer: on the 2026-09-09 33323830039 shape the
    span holds only tool traffic, nothing matches, and the caller steers
    exactly as before.
    """
    try:
        for msg in reversed(list(getattr(group_chat, 'messages', None) or [])):
            _m = msg or {}
            if str(_m.get('name') or '') in _REUSE_STEER_INITIATOR_NAMES:
                return None                  # reached this action's dispatch
            # ...and by CONTENT, because the seat name does not survive the
            # #725 sync.  Measured 2026-09-10 09:10: the dispatch arrived as
            # name='Assistant', so a name-only bound walked straight past it
            # into an EARLIER action and would credit that action's output to
            # this one.  The SHARED predicate, so this bound and the answer
            # test cannot drift — one of them was already fixed alone.
            if _reuse_is_pipeline_text(_m.get('content')):
                return None
            if _reuse_message_is_user_answer(msg):
                return msg
        return None
    except Exception:
        return None


# Asks for the ONE thing the round never produced.  Deliberately names the
# response_format key the prompt already defines, so the existing extractor
# unwraps it with no new parsing rule.
#
# USED ONLY WHEN THE TOOLS REALLY RAN.  This text asserts completion, and for
# a long time it was the ONLY steer — posted whenever the tail was control
# JSON, with no reference to whether anything had executed.  Measured live
# 2026-09-09 08:14:20 (agent 33323830039): the fabrication gate had just
# refused action 2 ("executed=[]; unrun=['execute_windows_or_android_command']",
# 0 advances) and this steer still told the model its tools had already run,
# so the user was told "The directory change ... has been successfully
# completed."  The model was obeying an instruction, not hallucinating.
# HOW to reply, in ONE place, appended to both steers below.
#
# Both used to end "Reply to @user with exactly: {"message2userfinal":
# "<your answer here>"}".  Live 2026-09-09 18:25:22 (agent 33323830039) the
# model reproduced that literal as the whole reply of a 313-second turn.
# "with exactly" is an instruction to copy; same finding as the false-premise
# wording above -- obeying an instruction, not hallucinating.  So no copyable
# literal ships: the slot is described, not shown.
#
# ONE constant because that session logged `unrun=none`, i.e. the COMPLETE
# steer fired -- a fix applied to one copy would have missed the failing path,
# which is exactly what happened to the D44 scope fix the same evening.
#
# Keeps the message2userfinal name (get_agent_response unwraps it, #797/D31)
# and carries no braces, so .format(unrun=...) below stays safe.
_REUSE_SYNTHESIS_ANSWER_SHAPE = (
    "Address your reply to @user, and send one JSON object whose only key is "
    "message2userfinal and whose value is the answer itself, written out as "
    "sentences the user will read. Substitute the real text — a placeholder, "
    "an empty value, or anything in angle brackets is not an answer."
)

# "using the real tool results from this conversation" WAS A FALSE PREMISE
# whenever the tools ran and returned NOTHING, and this file already records
# what a false premise does here — see the note above
# _REUSE_SYNTHESIS_STEER_INCOMPLETE: "The model was obeying an instruction,
# not hallucinating."  That fix corrected the premise for "the tools did not
# run"; this is the complementary case, tools RAN and returned EMPTY.
#
# Measured live 2026-09-10 15:14:12 (agent 92583386981, driven as its owner,
# AFTER a3905aabf so the store was genuinely consulted):
#   SimpleMem search took 0.002s, 0 results
#   tool result  res_in_filter": []  x14, zero non-empty payloads
#   15:15:16 [SYNTHESIS] ... unrun=none   -> THIS steer, not the INCOMPLETE one
#   15:15:21 [SYNTHESIS] round returned (14 -> 17 msgs)  (+3 = a real turn)
# and the user was told "you are currently at a B1 level and have a
# vocabulary of about 1,500 words".  Neither figure appears in any tool
# output in that window.  A language learner cannot tell that is invented.
#
# So the premise is now stated conditionally and the empty case is named.
# This is an INSTRUCTION, not a gate: it removes the sentence that invited
# the invention.  A hard groundedness check is separate and still open
# (#817) — do not read this as making fabrication impossible.
_REUSE_SYNTHESIS_STEER = (
    "The actions are finished and their tools have already run — do NOT run "
    "any tool again and do NOT emit another status object. Write the ANSWER "
    "for the user now, in your own words, using ONLY what the tool results "
    "in this conversation actually contain. If those results are empty or do "
    "not contain the information that was asked for, say so plainly and say "
    "what you can do next — do not supply values, figures or facts of your "
    "own. " + _REUSE_SYNTHESIS_ANSWER_SHAPE
)

# The same request, minus the false premise, for the case the gate says some
# tool did NOT execute.  SCOPED TO THE ACTION, because that is all the
# evidence covers: the caller computes `unrun` from
# _reuse_outstanding_tools(user_prompt, _reuse_current_action_id(...), ...)
# i.e. outstanding for the CURRENT action.  Saying "in this conversation"
# generalised that per-action fact and was FALSE whenever an earlier action
# had run the same tool -- measured live 2026-09-09 on agent 33323830039:
# execute_windows_or_android_command ran at 18:10:07 and FAB-GUARD recorded
# unrun=[] for action 1 at 18:10:19, yet the 18:10:32 steer told the model
# it had never executed, while the SAME steer orders it to "use only the
# real tool results present in this conversation".  Two contradictory
# instructions in one message.  Names the tools so the answer can be specific: a
# model told only "something failed" writes a vaguer report than the user
# deserves.  Asks for the SAME message2userfinal key, so the existing
# extractor unwraps it unchanged (a different shape would produce an answer
# nobody reads — #797/D31).
_REUSE_SYNTHESIS_STEER_INCOMPLETE = (
    "Do NOT run any tool again and do NOT emit another status object. These "
    "tools did not run for the action just attempted: {unrun}. Write the "
    "ANSWER "
    "for the user now, in your own words: report what WAS actually done, "
    "using only the real tool results present in this conversation, and say "
    "plainly which part could not be completed. Do not describe unexecuted "
    "work as done. " + _REUSE_SYNTHESIS_ANSWER_SHAPE
)


# THE HONEST ANSWER WHEN THE LOOKUP FOUND NOTHING.  Authored here, not asked
# of the model, because THREE live runs proved an instruction does not hold.
# Agent 92583386981, same prompt, driven as its owner:
#   14:58:34  "...you are currently at a B1 (Intermediate) CEFR level..."
#   15:14:12  after a3905aabf (read fixed, store really queried, 0 results)
#             "...a B1 level and have a vocabulary of about 1,500 words"
#   15:23:56  after b8dd44a49, with the explicit honesty instruction VERIFIED
#             delivered in-window ("If those results are empty" x3, old
#             premise x0) — "...a B1 (Intermediate) CEFR level. You have a
#             strong vocabulary for daily topics..."
# Every figure invented; res_in_filter":[] x14 and "SimpleMem search took
# 0.002s, 0 results" in each of the last two windows.
#
# Carries NONE of the _reuse_is_pipeline_text markers, so a5855f996's refusal
# cannot swallow it, and it reads as prose so the extractor delivers it.
# Never '' — #797/D31 stands.
_REUSE_NO_DATA_REPORT = (
    "I checked, and there is nothing recorded for that yet — the lookup came "
    "back empty. I have not made anything up to fill the gap. Tell me where "
    "you would like to start and I will record it as we go."
)


def _reuse_result_is_vacuous(body):
    """True when one tool RESULT carries no data.

    FAILS OPEN: a body this cannot parse is treated as substantive, so the
    caller's gate can never fire on something it does not understand.  A 0 or
    a False is DATA, not absence — only emptiness is emptiness.
    """
    s = str(body if body is not None else '').strip()
    if not s:
        return True
    try:
        v = json.loads(s)
    except Exception:
        return False                       # cannot judge -> substantive

    def _empty(x):
        if x is None:
            return True
        if isinstance(x, str):
            return not x.strip()
        if isinstance(x, (list, tuple, set)):
            return len(x) == 0
        if isinstance(x, dict):
            return all(_empty(i) for i in x.values())
        return False                       # numbers/bools are answers
    return _empty(v)


def _reuse_tool_results_all_vacuous(group_chat, agents, seen_ids, names):
    """True ONLY when the tools this action NAMED all came back empty.

    `names` is the action's REFERENCED tools.  Judging any tool result was
    the defect: live 2026-09-10 15:37, action 1 named ['get_chat_history']
    and its result was {"res_in_filter": []}, but send_message_to_user had
    also run and its receipt -- "Message sent successfully to user with
    request_id: ..." -- is not JSON, so the vacuity test failed open and one
    unnamed delivery tool's ACK vetoed the whole gate.  This family's rule,
    already stated above _reuse_fabricated_tools, is to key on the SPECIFIC
    function name and never on "any tool ran".

    Reads through ``_reuse_evidence_msg_lists`` — the ONE definition of where
    tool evidence lives — so this cannot look somewhere the fabrication gate
    does not.  Scoped by the SAME ``evidence_seen_call_ids`` watermark that
    gate uses, so a stale empty result from an earlier action cannot speak
    for this one (the failure that watermark exists to stop).

    Returns False when there are no tool results at all, which is what makes
    a PROSE action safe: it names no tool, produces no results, and must
    never be answered with "the lookup came back empty".

    Fail-open on anything unexpected — the caller then keeps its existing
    behaviour, i.e. this can only ever REPLACE an invented answer, never
    suppress a real one.
    """
    if not names:
        return False
    found = False
    try:
        _lists = _reuse_evidence_msg_lists(group_chat, agents)
        _call_fn = _reuse_call_id_to_tool_name(_lists)
        for _ml in _lists:
            for m in (_ml or []):
                if not isinstance(m, dict) or m.get('role') != 'tool':
                    continue
                _rs = m.get('tool_responses')
                entries = _rs if isinstance(_rs, list) and _rs else [m]
                for r in entries:
                    if not isinstance(r, dict):
                        return False
                    _cid = r.get('tool_call_id') or m.get('tool_call_id')
                    if _cid and _cid in (seen_ids or set()):
                        continue           # an earlier action's work
                    if _call_fn.get(_cid) not in names:
                        continue           # not a tool this action named
                    found = True
                    if not _reuse_result_is_vacuous(r.get('content')):
                        return False
    except Exception:
        return False
    return found


def _reuse_synthesis_turn(user_prompt, group_chat, manager, chat_instructor):
    """Give the synthesis the one turn the pipeline never gives it.

    Posts through ``chat_instructor.initiate_chat(recipient=manager, ...)`` —
    the same initiator, recipient and kwargs every other steer in this file
    uses (see ``_advance_or_steer`` and the two StatusVerifier recovery
    sites), so the reply lands in ``group_chat.messages`` where the extractor
    already reads it.  No new publisher, no second delivery path.

    Bounded by the group's own ``max_round=10``; at the measured closing pace
    (~1-3 s per speaker turn on 2026-09-09 04:14:10-19) that is ~10-30 s.
    Deliberately does NOT pass ``max_turns``: no sibling call does, and the
    installed autogen's support for it could not be verified from the shipped
    bundle (no loose conversable_agent.py, no library.zip member).

    Called from the post-loop extractor ONLY, which runs exactly once per
    turn — so no latch is needed to keep it from firing twice.

    Returns True when a steer was posted.  Never raises: this is the last step
    before the user gets an answer, and an exception here would turn a bad
    reply into no reply.
    """
    if not _reuse_needs_synthesis(group_chat):
        return False
    _before = len(getattr(group_chat, 'messages', None) or [])

    def _say(level, msg):
        # Logging is NEVER on the path that can skip the steer.  The first cut
        # put current_app.logger inside the same try as initiate_chat, and
        # current_app RAISES outside an app context — so a logging error would
        # have been swallowed by the except and the user would have silently
        # got the control JSON back.  _ctx_safe_log already cannot raise; this
        # keeps that true even if someone swaps it for a logger that can.
        try:
            _ctx_safe_log(level, msg)
        except Exception:
            pass

    # WHAT ACTUALLY RAN decides what we are allowed to assert.  Asked of
    # _reuse_outstanding_tools — the fabrication gate's own predicate, one
    # function below — so the sentence the user reads is bound to the same
    # evidence the gate uses to refuse an advance.  Without this the two
    # disagreed: the gate refused action 2 and the reply still claimed
    # success (measured 2026-09-09 08:13:41 vs 08:14:20).  Its error path
    # returns ['<unknown>'], so an unmeasurable state is treated as
    # outstanding — asserting success on what we cannot measure is the same
    # defect wearing a different hat.
    try:
        _unrun = _reuse_outstanding_tools(
            user_prompt, _reuse_current_action_id(user_prompt), group_chat)
    except Exception:
        _unrun = ['<unknown>']
    if _unrun:
        _steer = _REUSE_SYNTHESIS_STEER_INCOMPLETE.format(
            unrun=', '.join(str(t) for t in _unrun))
    else:
        # EVERY TOOL RAN AND EVERY RESULT WAS EMPTY.  Then the honest answer
        # is fully determined and the model is not needed for it — asking is
        # what produced three fabricated CEFR levels (14:58, 15:14, 15:23),
        # the last of them WITH the explicit honesty instruction delivered.
        # So the pipeline says it, exactly as #808/D42 already had the
        # pipeline say "that tool did not run" instead of accepting the
        # model's "successfully completed".
        #
        # ABOVE the written-answer recovery on purpose: if the tools returned
        # nothing, model text written earlier in THIS action is no better
        # grounded, and recovering it would just deliver the same invention
        # by another route.
        #
        # READ, never recompute.  By the time this runs the action pointer
        # has moved past the recipe and the watermark has swallowed the very
        # results that would have to be judged, so the answer has to be
        # taken while the action is still current -- see
        # _stamp_action_result_vacuity for the 48 ms that proved it.
        # Honoured only for the action that just finished, so a flag left by
        # an earlier turn cannot speak for this one.
        try:
            _t = user_tasks.get(user_prompt)
            _va = getattr(_t, 'evidence_vacuous_action', None)
            _cur = getattr(_t, 'current_action', None)
        except Exception:
            _va = _cur = None
        if _va is not None and _cur is not None and (_cur - 1) == _va:
            try:
                group_chat.messages.append({'content': _REUSE_NO_DATA_REPORT,
                                            'name': 'Assistant',
                                            'role': 'assistant'})
            except Exception as _nd_err:
                _say('warning', f"[SYNTHESIS] no-data report append failed: "
                                f"{_nd_err!r} — steering instead")
            else:
                _say('info', f"[SYNTHESIS] every tool result was empty — "
                             f"reporting the absence instead of asking for an "
                             f"answer there is no data for (session: "
                             f"{user_prompt}, {_before} msgs)")
                return False

        # THE ACTION MAY HAVE ALREADY WRITTEN THE ANSWER.  Only asked once
        # everything really ran — a recovered message asserts whatever the
        # model asserted, and over an unrun tool that is exactly the
        # "successfully completed" the honest-report fix removed (#808).
        # Deliberately BELOW the _unrun branch for that reason.
        _written = _reuse_written_answer(group_chat)
        if _written is not None:
            # A COPY: the extractor edits last_message['content'] in place
            # (strips '@user '), and that must not rewrite the history it
            # came from.  Appending rather than reordering keeps the record
            # honest, and the next #725 sync slice-assigns the whole list, so
            # this cannot accumulate.
            try:
                group_chat.messages.append(dict(_written))
            except Exception as _app_err:
                _say('warning', f"[SYNTHESIS] recovery append failed: "
                                f"{_app_err!r} — steering instead")
            else:
                # NAME AND HEAD, not just a length.  The first cut logged
                # "recovered 391 chars" and that number was equally true of
                # the finished answer and of the action dispatch it actually
                # delivered on 2026-09-10 04:42:04 — a count that cannot tell
                # a right answer from a wrong one is not a measurement.
                _txt = _written.get('content') or ''
                _say('info',
                     f"[SYNTHESIS] the action already wrote the answer — "
                     f"recovered {len(_txt)} chars from the group log, no "
                     f"steer posted (session: {user_prompt}, {_before} msgs, "
                     f"from={_written.get('name') or '?'}, "
                     f"head={_txt[:120]!r})")
                return False
        _steer = _REUSE_SYNTHESIS_STEER
    _say('info', f"[SYNTHESIS] reply would be raw control JSON — asking for "
                 f"the user-facing answer (session: {user_prompt}, "
                 f"{_before} msgs, unrun={_unrun or 'none'})")
    try:
        chat_instructor.initiate_chat(
            recipient=manager, message=_steer,
            clear_history=False, silent=False)
    except Exception as err:
        _say('warning', f"[SYNTHESIS] steer failed: {err}")
        return False
    # The answer the group just wrote lives in manager._oai_messages, NOT in
    # group_chat.messages — autogen appends to the former and the #725 sync
    # copies it to the latter.  That sync only ran inside the while1 loop,
    # which has already exited here, so without this call the synthesis
    # answer is invisible to the extractor: measured 2026-09-09 04:38:48,
    # "round returned (23 -> 23 msgs); still control JSON: True" while the
    # real message2userfinal sat in the buffer (04:38:12).  Same function the
    # loop calls — one sync, two callers.
    _reuse_sync_group_log(group_chat, manager)
    _after = len(getattr(group_chat, 'messages', None) or [])
    _say('info', f"[SYNTHESIS] round returned ({_before} -> {_after} msgs); "
                 f"still control JSON: {_reuse_needs_synthesis(group_chat)}")
    return True


def _reuse_outstanding_tools(user_prompt, action_id, group_chat):
    """The action's named tools that have NOT executed in this group chat.

    Thin adapter over ``_reuse_fabricated_tools`` (the fabrication gate's own
    predicate) so the under-reported-completion branch asks exactly the
    question the gate asks, instead of growing a second notion of "did the
    tool run".  Empty result == every tool this action names really executed.
    On any error returns a non-empty sentinel, so uncertainty blocks the
    advance rather than permitting it.
    """
    try:
        agents = list(getattr(group_chat, 'agents', None) or [])
        return _reuse_fabricated_tools(user_prompt, action_id, group_chat, agents)
    except Exception as err:
        # The log must never be able to fail the fail-closed path itself:
        # current_app raises outside an app context, which would let the
        # exception escape and hand the caller a green light instead of the
        # blocking sentinel.  Caught by test_error_reports_outstanding_tools.
        try:
            current_app.logger.debug(f"_reuse_outstanding_tools: {err}")
        except Exception:
            pass
        return ['<unknown>']


def _reuse_log(level, message):
    """Log through current_app when there is one, else the module logger.

    current_app raises off a request thread, so a bare current_app.logger call
    inside a try/except turns a logging problem into a wrong RETURN VALUE.
    """
    try:
        getattr(current_app.logger, level)(message)
    except Exception:
        getattr(logging.getLogger(__name__), level, logging.getLogger(__name__).info)(message)


def _reuse_complete_pending_subtask(user_prompt, action_id, ledgers=None):
    """Close the subtask the group just finished.  True if one was closed.

    Hop 4 of the flow core/constants.py:1124-1128 writes down --
    add_subtasks() -> get_pending_subtasks() -> execute each ->
    check_and_unblock_parent() -> parent completes -- and the only hop never
    wired.  check_and_unblock_parent has zero call sites in hartos/ or core/.

    It could not have worked as written, either.  MEASURED 2026-09-10 against
    the real SmartLedger:

        complete_task('4.1', 'success')            -> False, 4.1 stays 'pending'
        complete_task_and_route('4.1', 'success')  -> Task,  4.1 stays 'pending'
        4.1 = IN_PROGRESS; complete_task_and_route -> 4.1 == 'completed'

    The ledger requires PENDING -> IN_PROGRESS -> COMPLETED, and refuses a
    never-started task SILENTLY (a bare False, an unchanged status).  The
    BREAKDOWN block steers a child to the model but never marks it started, so
    every child stays PENDING for the life of the session and
    get_pending_subtasks -- which filters strictly on status == PENDING --
    keeps returning the same ones.

    Prefer the child already IN_PROGRESS (the one that was actually steered);
    fall back to the next PENDING one so a verdict that arrives before the
    mark still closes real work rather than none.
    """
    _ledgers = user_ledgers if ledgers is None else ledgers
    if LedgerTaskStatus is None or user_prompt not in _ledgers:
        return False
    try:
        ledger = _ledgers[user_prompt]
        parent_task_id = 'action_%s' % action_id
        started = [t for t in ledger.tasks.values()
                   if t.parent_task_id == parent_task_id
                   and t.status == LedgerTaskStatus.IN_PROGRESS]
        child = started[0] if started else None
        if child is None:
            pending = get_pending_subtasks(user_prompt, int(action_id), _ledgers)
            if not pending:
                return False
            child = pending[0]
            child.status = LedgerTaskStatus.IN_PROGRESS
        ledger.complete_task_and_route(child.task_id, 'success')
        done = ledger.tasks[child.task_id].status == LedgerTaskStatus.COMPLETED
    except Exception as _sub_err:
        _reuse_log('warning',
                   f"[SUBTASK] could not close a subtask of action {action_id} "
                   f"for session {user_prompt}: {_sub_err}")
        return False
    # Log AFTER the outcome is decided and never inside the try: current_app
    # raises "Working outside of application context" off a request thread, and
    # a logging failure caught by the block above would report False for a
    # subtask that HAD completed -- failing closed after the side effect already
    # landed.  Measured while greening this file's own tests.
    _reuse_log('info',
               f"[SUBTASK] action {action_id} subtask {child.task_id} "
               f"-> {ledger.tasks[child.task_id].status.value} "
               f"for session: {user_prompt}")
    return done


def _advance_or_steer(user_prompt, action_id, reason, prompt_id,
                      manager, chat_instructor,
                      claimed_action_id=None, advanced_latch=None):
    """Move the reuse pipeline past `action_id` — or, when the action only
    CLAIMED completion, steer the agent to actually run its tools.

    One rule, one home.  Five call sites used to inline this same block
    (w1-completed / w1 / w1-regex / w2 / w2-regex), each with locals named
    after the loop they sat in — _rc_next, _next, _next2, _w2_next,
    _w2_next2 and their _ok/_steer twins.  A name that says WHERE the code
    is tells a reader nothing about WHAT the value holds, and five copies of
    one rule drift apart: the 2026-09-05 fabricated-completion fix had to be
    applied five times, and the [HALLUCINATION?] check four.

    Args:
        action_id: the action the PIPELINE believes is current.
        claimed_action_id: the action id the LLM asserted, when it named one.
            Logged when it disagrees with `action_id` — an agent claiming a
            different action than the one assigned is a hallucination signal.
        advanced_latch: the caller's once-per-action set, when it keeps one
            (only get_agent_response does).  A refusal drops the latch so the
            action can be re-verified once the agent really runs the tool.

    Returns:
        True  — the next action's message, or a re-steer, was posted; the
                caller should keep looping.
        False — no next action and nothing to steer; the caller should end
                the turn.

    False is what a SUCCESSFULLY FINISHED recipe looks like: the last action
    advanced, so there is no next action, and it was not fabricated, so
    _reuse_fab_steer_message has nothing to say.  End the turn by `break`ing
    to the extractor that already sits after each loop
    (get_agent_response :4258-4304, chat_agent :5288-5310) — never by
    `return ''`.  All six call sites used to return '', which threw away the
    answer of an agent that had done its whole job; in Nunba that empty string
    trips the empty-reply check and silently reroutes the user to the
    tool-less Tier-2 fallback, so they were told something that contradicted
    the work the machine had just done and saved (#797/#798, measured live
    2026-09-09 on agent 90210554431).  Guarded by
    tests/unit/test_completion_is_not_an_empty_reply.py.
    """
    if claimed_action_id is not None and claimed_action_id != action_id:
        current_app.logger.warning(
            f"[HALLUCINATION?] LLM claims action_id={claimed_action_id} "
            f"but pipeline has {action_id}")

    # A DECOMPOSED action is not finished when the model says "completed" -- it
    # is finished when its own subtasks are.  Close the child this verdict is
    # about, then refuse to advance while any sibling is still outstanding, and
    # steer that sibling instead.  Same shape as the fabrication refusal below:
    # no advance, drop the latch, post one steer, keep looping.
    #
    # This is the ONE door all six advance sites go through, which is why the
    # guard lives here and not in the caller that happened to catch it.
    # MEASURED live 2026-09-10, agent 88719487304 action 4:
    #   20:23:12  [BREAKDOWN] action 4 has 2 pending subtask(s) - working '...'
    #   20:23:21  reuse-w1-completed: terminal 'completed' verdict - advancing
    #   20:23:21  [REUSE] Action 4 TERMINATED, advancing
    # Nine seconds, execute_coding_task never ran, and action 4 is the only one
    # of the nine with no FAB-GUARD verdict line -- the fabrication gate sits on
    # the normal advance path and reuse-w1-completed had stepped over it.
    _closed_subtask = _reuse_complete_pending_subtask(user_prompt, action_id)
    _outstanding = get_pending_subtasks(user_prompt, int(action_id), user_ledgers) \
        if LedgerTaskStatus is not None else []
    if _outstanding:
        _next_sub = _outstanding[0]
        current_app.logger.info(
            f"[SUBTASK-HOLD] action {action_id} keeps {len(_outstanding)} unrun "
            f"subtask(s) (closed={_closed_subtask}); steering "
            f"'{str(_next_sub.description)[:60]}' instead of advancing "
            f"for session: {user_prompt}")
        if advanced_latch is not None:
            advanced_latch.discard(action_id)
        _narrow_assistant_to_current_action(user_prompt)
        chat_instructor.initiate_chat(
            recipient=manager,
            message=(_REUSE_SUBTASK_STEER_PREFIX + str(_next_sub.description)),
            clear_history=False, silent=False)
        return True

    next_action_id, advanced = _advance_reuse_action(
        user_prompt, action_id, reason, prompt_id)

    # Both branches below post a command into the SAME group chat the
    # assistant is already in, so its cached system prompt must name the
    # ledger's action before either goes out.  Placed after the advance (it
    # is what moves current_action) and above the branch, so the advance and
    # the re-steer are covered by one call rather than two that can drift.
    _narrow_assistant_to_current_action(user_prompt)

    if not advanced:
        # A fabrication refusal is NOT "all actions done" — it wants the
        # tool actually run, so steer instead of ending the turn.
        steer_message = _reuse_fab_steer_message(user_prompt, action_id)
        if not steer_message:
            return False
        if advanced_latch is not None:
            advanced_latch.discard(action_id)
        chat_instructor.initiate_chat(
            recipient=manager, message=steer_message,
            clear_history=False, silent=False)
        return True

    chat_instructor.initiate_chat(
        recipient=manager,
        message=_build_reuse_action_message(user_prompt, next_action_id),
        clear_history=False, silent=False)
    return True


def _reuse_present_call_ids(msg_lists):
    """Every tool_call id currently visible in ``msg_lists``.

    Stamped when an action is dispatched so the fabrication gate can tell
    THIS action's tool runs from an earlier action's.  Ids (not indices)
    because the lists are mutated in place and spliced by the #725 sync, so
    a position is not stable; a tool_call id is.
    """
    out = set()
    for ml in (msg_lists or []):
        for m in (ml or []):
            if not isinstance(m, dict):
                continue
            for tc in (m.get('tool_calls') or []):
                cid = (tc or {}).get('id')
                if cid:
                    out.add(cid)
    return out


def _reuse_evidence_msg_lists(group_chat, agents):
    """The message lists the gate treats as evidence — ONE definition.

    Both the watermark stamp and the gate must look at the same places, or
    the stamp would miss a buffer the gate later credits.
    """
    lists = [getattr(group_chat, 'messages', None) or []]
    for ag in (agents or []):
        conv = getattr(ag, '_oai_messages', None)
        if isinstance(conv, dict):
            lists.extend(conv.values())
    return lists


def _stamp_action_evidence_watermark(user_prompt):
    """Record which tool runs already existed when this action was dispatched.

    Called from the ONE site that writes ``current_action`` so the watermark
    and the action id can never disagree.  Reaches the group chat the same
    way the gate does (get_registered_groupchat), so no new plumbing.
    """
    try:
        from hartos.lifecycle_hooks import get_registered_groupchat
        gc = get_registered_groupchat(user_prompt)
        if gc is None:
            return
        seen = _reuse_present_call_ids(
            _reuse_evidence_msg_lists(gc, getattr(gc, 'agents', None) or []))
        user_tasks[user_prompt].evidence_seen_call_ids = seen
        _ctx_safe_log('info',
                      f"[FAB-GUARD] watermark for action "
                      f"{user_tasks[user_prompt].current_action}: "
                      f"{len(seen)} pre-existing tool call(s) "
                      f"for session: {user_prompt}")
    except Exception as err:
        _ctx_safe_log('debug', f"evidence watermark skipped: {err}")


def _stamp_action_result_vacuity(user_prompt, action_id):
    """Record whether the action that just finished got DATA back.

    Called from the ONE site that advances ``current_action``, BEFORE it
    moves and BEFORE the watermark is re-stamped -- because both facts this
    needs are gone one line later.  That is exactly why the synthesis turn
    could not compute it for itself.  Measured live 2026-09-10 on a
    ONE-action recipe (agent 92583386981):

        15:37:31,777  [REUSE] Action 1 TERMINATED, advancing
        15:37:31,780  [FAB-GUARD] watermark for action 2: 20 pre-existing
        15:37:31,782  [REUSE] All 1 actions completed
        15:37:31,828  [SYNTHESIS] ... unrun=none

    48 ms apart.  By synthesis every call id the gate had to judge was
    already inside ``evidence_seen_call_ids`` and got skipped as "an earlier
    action's work", and ``current_action`` was 2 with only one action, so
    the action text was out of range too.  The gate shipped, was reachable,
    and fired zero times.

    Stamps on EVERY completion, including False, so a later prose action can
    never inherit an earlier lookup's emptiness.  Records the action id with
    it so a stale flag from a previous turn cannot be honoured.

    Reaches the group chat exactly as _stamp_action_evidence_watermark does
    (get_registered_groupchat), so there is no new plumbing.  Never raises:
    it sits on the path that produces the user's reply.
    """
    task = user_tasks.get(user_prompt)
    if task is None:
        return
    task.evidence_vacuous_action = None
    try:
        from hartos.lifecycle_hooks import get_registered_groupchat
        gc = get_registered_groupchat(user_prompt)
        if gc is None:
            return
        agents = getattr(gc, 'agents', None) or []
        _names, _referenced = _reuse_registered_and_referenced_tools(
            agents, task.get_action(action_id - 1))
        _seen = getattr(task, 'evidence_seen_call_ids', None)
        _seen = _seen if isinstance(_seen, (set, frozenset)) else set()
        if _reuse_tool_results_all_vacuous(gc, agents, _seen,
                                           set(_referenced)):
            task.evidence_vacuous_action = action_id
            _ctx_safe_log('info',
                          f"[FAB-GUARD] action {action_id} ran "
                          f"{sorted(_referenced)} and EVERY result was "
                          f"empty for session: {user_prompt}")
    except Exception as err:
        _ctx_safe_log('debug', f"result-vacuity stamp skipped: {err}")


def _reuse_registered_and_referenced_tools(agents, action_text):
    """(every tool name this leg can SERVE, the ones this action's TEXT names).

    Lifted verbatim out of _reuse_fabricated_tools so the vacuity stamp asks
    the same question by the same rule.  Two callers, ONE derivation: a
    second copy would drift the moment either side changed what counts as
    "this action names that tool".

    "Can serve" is three sources, not two: already REGISTERED (_function_map),
    already in the SCHEMA (llm_config['tools']), and ATTACHABLE BY NAME
    (_hart_core_tools).  The third was missing and made the gate structurally
    blind -- see the loop below.
    """
    names = set()
    for ag in (agents or []):
        try:
            names.update((getattr(ag, '_function_map', None) or {}).keys())
        except Exception:
            pass
        cfg = getattr(ag, 'llm_config', None)
        if isinstance(cfg, dict):
            for t in (cfg.get('tools') or []):
                fn = ((t or {}).get('function') or {}).get('name')
                if fn:
                    names.add(fn)
        # Closures this leg can attach BY NAME but has not attached yet.
        # attach_for_names(core_tools=...) serves exactly these, unpacking the
        # same (name, description, func) tuples build_core_tool_closures
        # returns -- so read [0] the way it does.
        #
        # Without this the two sources above only see tools ALREADY attached,
        # so a recipe-named closure was invisible until something else had
        # attached it, and _reuse_fabricated_tools returned [] at its
        # `if not referenced` early-return, which sits ABOVE its log line.
        # Measured live 2026-09-10, agent 88719487304 action 4 (rid
        # d60c-223537, taken AFTER 7fd83678f made these closures buildable):
        # watermark 12 -> 13, and it was the ONLY action of nine with no
        # `[FAB-GUARD] action N names tool(s)` verdict line at all.  An action
        # naming execute_coding_task read exactly like a prose action naming
        # nothing, and advanced having run nothing.
        # Population: 87 actions across 35 agents name execute_coding_task.
        for _ct in (getattr(ag, '_hart_core_tools', None) or []):
            try:
                _cn = _ct[0]
            except Exception:
                continue
            if _cn:
                names.add(_cn)
    text = str(action_text or '').lower()
    referenced = [n for n in names if n and len(n) > 3 and n.lower() in text]
    return names, referenced


def _reuse_call_id_to_tool_name(msg_lists):
    """call_id -> function name, read off the PROPOSING assistant message.

    A tool result's own `name` is the EXECUTING AGENT, never the function, so
    this join is the only way to say which tool a result belongs to.  Lifted
    out of _reuse_fabricated_tools for the vacuity stamp; one rule, no second
    vocabulary.
    """
    out = {}
    for _ml in (msg_lists or []):
        for m in (_ml or []):
            if not isinstance(m, dict):
                continue
            for tc in (m.get('tool_calls') or []):
                _cid = (tc or {}).get('id')
                _fn = ((tc or {}).get('function') or {}).get('name')
                if _cid and _fn:
                    out[_cid] = _fn
    return out


def _reuse_fabricated_tools(user_prompt, current_action, group_chat, agents):
    """Registered tool names the current action NAMES but that produced ZERO
    tool results anywhere in the group chat — i.e. a fabricated 'completed'.

    Deliberately COARSE + fail-open (returns [] unless certain): only flags
    when (a) the action text names a registered tool AND (b) the chat carries
    no tool result or tool call for it.  This catches the pure-fabrication
    case (live 2026-09-03: revenue agent claimed get_api_revenue_stats
    returned 90% with zero tool_execution) while NEVER stalling an action
    once its tool has run, and NEVER touching prose actions that name no tool.
    """
    try:
        task = user_tasks.get(user_prompt)
        text = str(task.get_action(current_action - 1) or '').lower() if task else ''
        if not text:
            return []
        names, referenced = _reuse_registered_and_referenced_tools(agents, text)
        if not referenced:
            return []
        # Which of the referenced tools ACTUALLY executed?  A tool ran if a
        # role=='tool' result named for it OR an assistant tool_call for it
        # appears ANYWHERE the conversation is recorded — the group-chat log OR
        # any agent's pairwise _oai_messages buffer.  Scanning only
        # group_chat.messages was blind to tool activity autogen records in an
        # agent buffer but not the (hooked) group log: live 2026-09-05 Trading
        # 33204307184, google_search made a real HTTP 200 (primp) yet
        # executed=[] because its assistant tool_call + role=tool result lived
        # in the Helper<->Assistant buffer, not group_chat.messages — so the
        # guard could not tell a real completion from a fabricated one.  Still
        # keyed on the SPECIFIC function name (never "any tool ran"), so the
        # revwarm5407 defeat — an unrelated MemoryGraph tool marking a
        # never-run get_api_revenue_stats as executed — cannot recur.
        # A tool counts as executed ONLY on a real role=='tool' RESULT.
        #
        # Two things that look like execution and are not — both were being
        # counted, which made this guard unable to fail for its own defect
        # (measured 2026-09-06, agent 89555447799, actions 16..24):
        #   * the HISTORICAL_TOOL_PLACEHOLDER stand-in, which helper.py mints
        #     precisely BECAUSE a tool_call produced no result.  On 625 wire
        #     bodies every named tool-role message was one of these (real=0),
        #     so the set filled with proof of NON-execution.
        #   * a bare `tool_calls` entry, which is the model PROPOSING a call —
        #     1,200 proposals for execute_windows_or_android_command alone,
        #     against ONE actual tool-body entry in 21 minutes.
        # With clear_history=False the set also accumulates all session, so it
        # saturated to the whole 25-name roster and unrun=[] became
        # unreachable; nine fabricated 'completed' verdicts advanced.
        #
        # Fail-open is preserved by SCOPE, not by leniency: every message list
        # is still scanned (group log + each agent's pairwise buffer), which is
        # what the 2026-09-05 Trading widening actually needed — that tool had
        # a REAL result, just in a buffer the old scan missed.
        # A tool result's own top-level `name` is the EXECUTING AGENT's name,
        # never the function's, so it cannot identify the tool.  Measured live
        # 2026-09-06 19:22-19:43 (agent 89555447799): this gate logged
        # `executed=['Assistant']; unrun=['google_search']` while
        # agent_system.log recorded google_search 10 START / 10 SUCCESS / 0
        # ERROR and execute_windows_or_android_command 28/28/0 in the SAME
        # window — 50 real executions, every one called unrun, so each action
        # burned its 3 re-steers and force-advanced "NOT tool-backed".
        #
        # The function name lives on the PROPOSING assistant message's
        # tool_calls[].function.name and is joined to the result by
        # tool_call_id.  helper.py:1898-1907 already resolves it exactly this
        # way when it builds the outgoing body — which is why the live wire
        # bodies carried 12 properly-named google_search tool messages in the
        # very window this gate saw none.  Same mapping here: one rule for
        # "which tool ran", no second vocabulary.
        executed = set()
        _msg_lists = _reuse_evidence_msg_lists(group_chat, agents)
        # Tool runs that already existed when THIS action was dispatched are
        # someone else's work.  Without this window the set accumulates for
        # the whole session (clear_history=False), so one real result makes
        # the gate unable to fail for every later action naming that tool —
        # measured live 2026-09-08: execute_windows_or_android_command
        # DISPATCHED 2, named by 17 advanced actions; actions 4..18 all
        # advanced after the last dispatch with unrun=[].  The docstring
        # above already records this failure once; the remedy then widened
        # WHERE the gate looks, never WHEN.
        try:
            _seen = getattr(user_tasks.get(user_prompt), 'evidence_seen_call_ids', None)
        except Exception:
            _seen = None
        _seen = _seen if isinstance(_seen, (set, frozenset)) else set()
        _call_fn = _reuse_call_id_to_tool_name(_msg_lists)

        def _record_result(call_id, content, fallback_name=None):
            """Count one tool RESULT, resolved to its function name."""
            # This result belongs to an EARLIER action — its call id was
            # already on the wire when the current action was dispatched.
            # Crediting it is what let 15 actions advance on 0 dispatches.
            if call_id and call_id in _seen:
                return
            _body = str(content or '')
            if HISTORICAL_TOOL_PLACEHOLDER in _body:
                return  # the stand-in minted BECAUSE nothing executed
            # The tool RAN and reported it could not do the work.  Running is
            # not the property this gate protects — the action's work getting
            # done is.  Live 2026-09-07 (agent 60834540771 as its real owner)
            # action 1's execute_windows_or_android_command drove the VLM loop
            # into a Notepad error dialog, exited max_iterations, returned
            # "Not able to perform this action now please try later" — and the
            # action advanced 25s later with unrun=[].  Treated like the
            # placeholder above: report UNRUN so _advance_reuse_action
            # re-steers (bounded) instead of silently marking it verified.
            if any(f in _body for f in TOOL_FAILURE_RESULTS):
                return
            fn = _call_fn.get(call_id) or fallback_name
            # Only a REGISTERED tool name counts.  An agent name satisfies
            # nothing and only pollutes the set — that pollution is what
            # `executed=['Assistant']` was.
            if fn in names:
                executed.add(fn)

        for _ml in _msg_lists:
            for m in (_ml or []):
                if not isinstance(m, dict) or m.get('role') != 'tool':
                    continue
                # Two shapes, both live: the aggregate envelope autogen puts
                # in the buffers (per-call entries under `tool_responses`),
                # and the flat per-call message (22 of 80 in the measured
                # window carried a tool_call_id and NO name at all — the old
                # `not m.get('name')` skip discarded every one of them).
                responses = m.get('tool_responses')
                if isinstance(responses, list) and responses:
                    for r in responses:
                        if isinstance(r, dict):
                            _record_result(r.get('tool_call_id'),
                                           r.get('content'), r.get('name'))
                else:
                    _record_result(m.get('tool_call_id'), m.get('content'),
                                   m.get('name'))
        unrun = [n for n in referenced if n not in executed]
        try:
            # 'for session:' suffix, same as "Retrieved current_action_id": one
            # server.log carries every agent's and daemon's lines, so unqualified
            # this scored 88764372848 "reached=[1] tools-ran=[4]" on 2026-09-09 --
            # action 4's tool cannot run without action 4 being reached.
            current_app.logger.info(
                f"[FAB-GUARD] action {current_action} names tool(s) {referenced}; "
                f"executed={sorted(executed)}; unrun={unrun} "
                f"for session: {user_prompt}")
        except Exception:
            pass
        return unrun
    except Exception:
        return []


def _reuse_sync_group_log(group_chat, manager):
    """Bring ``group_chat.messages`` up to date from ``manager._oai_messages``.

    The body is the #725 sync, lifted OUT of the while1 loop so it has more
    than one caller.  While it was inline, the only moment the group log could
    catch up was inside that loop — and the post-loop synthesis steer
    (#799/D33) runs after the loop has exited.

    Measured live 2026-09-09 04:37:58-04:38:48: the synthesis round DID produce
    the answer —

        04:38:12  last json as {'message2userfinal': "The chatterbox_turbo
                  worker startup failure has been diagnosed..."}

    — but the group log never moved:

        04:38:48  [SYNTHESIS] round returned (23 -> 23 msgs);
                  still control JSON: True

    The answer was in _oai_messages and the extractor could not see it, so the
    user still got the verdict.  One implementation, two callers; NOT a second
    sync.

    Never raises: both callers sit on the path that produces the user's reply.
    """
    try:
        _mgr_msgs = getattr(manager, '_oai_messages', None)
        if not _mgr_msgs:
            return False
        _conv = max(_mgr_msgs.values(), key=len, default=None)
        # Gate on the BASE length last synced, NOT on len(group_chat.messages).
        # The merge splices answers in, so the group log is legitimately LONGER
        # than its own source; comparing against it would stop this ever firing
        # again — the freeze the original `if not group_chat.messages` had.
        _base_n = getattr(group_chat, '_hart_sync_base_n',
                          len(group_chat.messages))
        if not _conv or len(_conv) <= _base_n:
            return False
        _was = len(group_chat.messages)
        # The longest buffer is the right SKELETON but carries zero tool
        # answers (measured 16/16, see _merge_tool_answers).  Fill its
        # unanswered calls from the sibling buffers before handing it over,
        # or helper.py:1940 mints a placeholder over each one.
        _merged = _merge_tool_answers(_conv, list(_mgr_msgs.values()))
        group_chat.messages[:] = list(_merged)
        try:
            group_chat._hart_sync_base_n = len(_conv)
        except Exception:
            pass
        _ctx_safe_log('info',
                      f"[725-SYNC] group_chat.messages stale ({_was} < "
                      f"{len(_conv)}) — resynced from manager._oai_messages"
                      f"; spliced {len(_merged) - len(_conv)} real tool "
                      f"answer(s) {dict(_merge_last_stats)}")
        # DIAGNOSTIC ONLY (no behaviour change).  _oai_messages is keyed PER
        # AGENT and each value is a pairwise broadcast log, so "longest" is a
        # LENGTH proxy for "most complete" — not the same thing.  Measured live
        # 2026-09-07 on agent 18088688973: 418 announced tool_call ids against
        # 106 role=tool answers (312 unanswered, 74.6%).  Slice-assignment
        # cannot create duplicates, so they are in the SOURCE.  Records, per
        # buffer, whether the answers live somewhere other than the buffer we
        # picked — the fact needed before changing the selector (task #789).
        try:
            _picked = id(_conv)
            _parts = []
            for _k, _v in _mgr_msgs.items():
                _calls = sum(len(m.get('tool_calls') or [])
                             for m in _v if isinstance(m, dict))
                _answers = sum(1 for m in _v
                               if isinstance(m, dict) and m.get('role') == 'tool')
                _parts.append(
                    f"{getattr(_k, 'name', str(_k))[:18]}"
                    f"{'*' if id(_v) == _picked else ''}"
                    f":n={len(_v)},calls={_calls},answers={_answers}")
            _ctx_safe_log('info',
                          "[725-SYNC-COMPOSITION] (*=picked) " + " | ".join(_parts))
        except Exception:
            pass
        return True
    except Exception as _sync_err:
        _ctx_safe_log('debug', f"[725-SYNC] skipped: {_sync_err}")
        return False


def get_agent_response(assistant: "autogen.AssistantAgent", chat_instructor: "autogen.UserProxyAgent",
                       helper: "autogen.AssistantAgent", user_proxy: "autogen.UserProxyAgent",
                       manager: "autogen.GroupChatManager", group_chat: "autogen.GroupChat", message: str, role: str,
                       user_id: int, prompt_id: int, request_id: str) -> str:
    """Get a single response from the agent for the given message."""
    user_prompt = f'{user_id}_{prompt_id}'
    # Register this session's group chat so the fabrication gate in
    # _advance_reuse_action can reach it (via get_registered_groupchat) on
    # EVERY advance path — w1/w2/regex.  Reuse never registered it before, so
    # the gate silently no-op'd (get_registered_groupchat returned None).
    try:
        from hartos.lifecycle_hooks import register_groupchat_for_session
        register_groupchat_for_session(user_prompt, group_chat)
    except Exception:
        pass
    try:
        # Tier-1 per-turn attach: deterministic keyword scan of THIS message
        # unlocks families the construction-time goal never mentioned — zero
        # extra LLM calls, and no reliance on the model choosing to call
        # request_tools.  Attach happens before the model sees the turn.
        try:
            _unlocked = getattr(assistant, '_hart_unlocked_tags', None)
            if _unlocked is not None:
                from integrations.agent_engine.marketing_tools import detect_goal_tags
                from integrations.service_tools import service_tool_registry

                # (a) NAMED — the tools this action's own recipe declares.
                # Authoritative: the authoring pipeline recorded exactly which
                # tool the action needs, so honour it before inferring anything.
                # Live 2026-09-06, agent 89555447799: action 1 declares
                # tool_name google_search, yet the tag scan below read the
                # words "developer"/"platforms" as ['coding'] and logged
                # "Tier-1 turn attach: +['coding'] -> 0 tools".  google_search
                # never reached the wire (1 of 96 autogen.reuse calls carried
                # any tools[]; INSIDE google search fired 0x), so the model
                # could not call the one tool its recipe named.  Population
                # scale: 8,799 "Error: Function <X> not found" across the log
                # rotations — send_message_to_user x1618 (the path that returns
                # the agent's result to the user), get_user_details x908.
                try:
                    _aid = user_tasks[user_prompt].current_action
                except Exception:
                    _aid = None
                # Narrow the system prompt to the action being dispatched.
                # The prompt is built ONCE at construction (L1328) and cached
                # in user_agents, so without this every turn ships all N
                # recipes and the wire-trim left-trims the early ones away —
                # measured 2026-09-08: body carried action_id [13..24] while
                # the turn asked for #2, and the agent correctly told the user
                # it could not see the action.  ONE implementation, shared
                # with the advance path: see _narrow_assistant_to_current_action.
                _narrow_assistant_to_current_action(user_prompt)

                _named = _reuse_action_tool_names(user_prompt, _aid) if _aid else []
                if _named:
                    from core.agent_tools import attach_for_names
                    _nn = attach_for_names(_named, helper, assistant,
                                           service_tool_registry,
                                           assistant._hart_attached_tools,
                                           core_tools=getattr(
                                               assistant, '_hart_core_tools', None))
                    # Log BOTH outcomes, not just the non-zero one.  The old
                    # `if _nn:` made a resolved-nothing round indistinguishable
                    # from a round that never ran, and that is exactly how this
                    # hook read as healthy while doing nothing: measured live
                    # 2026-09-07/08 over 23 driven agents, "Tier-1 named attach"
                    # appeared ZERO times and no line said why.
                    # The SESSION KEY belongs on this half too.  These two
                    # branches are one diagnostic pair -- the whole point is
                    # telling a resolved-nothing round from a real one -- so
                    # identifying only the empty half leaves the other half
                    # exactly as unattributable as before.  MEASURED 2026-09-11
                    # 01:47:46, minutes after the empty branch gained its key:
                    # "action 1 names ['google_search'] -> 1 tools" arrived
                    # with a reuse walk in flight AND daemon traffic AND a
                    # second user's agents on the box, and there was no way to
                    # say whose it was.
                    current_app.logger.info(
                        f"Tier-1 named attach: action {_aid} names {_named} "
                        f"-> {_nn} tools for session: {user_prompt}")
                elif _aid:
                    # INFO, not debug.  gui_app.log captured ZERO "- DEBUG -"
                    # lines across the whole 2026-09-11 drive, so at debug this
                    # branch never reaches production and a resolved-nothing
                    # round stays indistinguishable from one that never ran --
                    # the exact gap the comment above says the both-outcomes
                    # logging was added to close.  Measured rid d62-232532:
                    # "Tier-1 prompt narrow" fired for actions 1,2,3,4 (the
                    # statement immediately above), "Tier-1 named attach" for
                    # action 1 only, and nothing said why.
                    #
                    # The COUNT is what separates []'s causes, which is why it
                    # is in the line: 0 = no recipe stored for this session
                    # (the helper's `except` swallowed a KeyError), n < _aid =
                    # the id is past the end of the stored list, n >= _aid =
                    # the action genuinely names no tool.  Offline against the
                    # real recipe the helper returns non-empty for every one of
                    # actions 1,2,3,4,9, so live [] is the STORE, not the
                    # helper -- five hypotheses were eliminated for want of
                    # this one number (#828).
                    # ...and the SESSION KEY, because the count alone is not
                    # attributable on a live box.  Measured 2026-09-11 on the
                    # first drive that carried this line: five occurrences all
                    # read "holds 1 action(s)" while the agent under test
                    # (88719487304) has NINE actions in both its flow recipes
                    # on disk -- and the surrounding log showed a rival driver
                    # (a marketing reuse agent for user cf125371) plus daemon
                    # traffic in the same window.  444 of 880 stored agents are
                    # single-action stubs (#758), so "holds 1" is the NORMAL
                    # reading for a stub and says nothing about this agent.
                    # Without the key the number cannot be attributed to a
                    # session, which is the same ambiguity this line exists to
                    # remove -- so it names the session it measured.
                    _store = (recipes.get(user_prompt) or {}).get('actions') or []
                    current_app.logger.info(
                        f"Tier-1 named attach: action {_aid} names no tool "
                        f"(recipes store holds {len(_store)} action(s)) "
                        f"for session: {user_prompt}")

                # (b) TAGS — unchanged fallback for capability families the
                # recipe never mentions but the conversation drifted into.
                _new = [t for t in detect_goal_tags(message or '')
                        if t not in _unlocked]
                if _new:
                    from core.agent_tools import attach_for_tags
                    from integrations.agent_engine.goal_manager import get_tool_tags
                    _cap = set()
                    for _t in _new:
                        _cap.update(get_tool_tags(_t))
                    _n = attach_for_tags(_cap, helper, assistant,
                                         service_tool_registry,
                                         assistant._hart_attached_tools)
                    _unlocked.update(_new)
                    current_app.logger.info(
                        f"Tier-1 turn attach: +{_new} -> {_n} tools")
        except Exception as _e:
            current_app.logger.debug(f"turn attach skipped: {_e}")

        result = user_proxy.initiate_chat(manager,
                                          message=_reuse_seed_message(user_prompt, message),
                                          speaker_selection={"speaker": "assistant"},
                                          clear_history=False)

        # TWO counters, because the budget is PER ACTION but was spent per
        # TURN (#790/D23).  Measured 2026-09-09 on agent 33323830039, twice in
        # one hour: 04:50:52 the turn died with action 1 having used 2 loop
        # iterations and action 2 fourteen; 05:31:01 it died the other way
        # round, action 1 having used almost all of them doing REAL work.
        # Both ended `exhausted 12 rounds at action 2/2` — whichever action
        # goes first spends the whole turn's allowance, and the rest of the
        # recipe is unreachable no matter how well it would have run.
        _action_rounds = 0                # spent on the CURRENT action
        count = 0                         # spent on the whole turn
        _budget_action = _reuse_current_action_id(user_prompt)
        _action_evidence = _reuse_own_tool_progress(
            user_prompt, _budget_action, group_chat,
            getattr(group_chat, 'agents', None) or [])
        if _action_evidence is None:
            _action_evidence = _reuse_evidence_count(group_chat)  # progress mark
        _budget_action = _reuse_current_action_id(user_prompt)
        _round_budget = _reuse_turn_round_budget(user_prompt)
        _reuse_advanced_actions = set()  # one robust completion-advance per action id
        while True:
            current_app.logger.info('inside reuse while1')

            # #725 ROOT-CAUSE FIX (proven live 2026-09-05: nappend=0, conversation in
            # manager._oai_messages).  In this reuse flow autogen accumulates the
            # exchange in the agents' pairwise _oai_messages, NOT in group_chat.messages
            # (which the factory wrapped as a _GraphHookedList that never gets appended
            # to).  Every group_chat.messages read below therefore saw an empty list and
            # the turn bailed "empty mid-loop" — the general reuse blocker.  Sync the
            # group log from the manager's richest conversation buffer (autogen's own
            # store — no parallel path) so the existing reads work unchanged.  Cheap:
            # only runs when the group log has fallen behind.
            #
            # The gate was `if not group_chat.messages` and that was VACUOUS after
            # its first fire: it cures EMPTINESS, but the list still receives no
            # appends, so once seeded it FREEZES and a non-empty-but-stale log can
            # never re-enter the branch.  Measured live 2026-09-06 (agent
            # 89555447799, 13:06-13:19): [725-SYNC] fired ONCE at 13:08:02 with 10
            # msgs, and state_transition's own messages[-1] log then shows 191 of
            # 193 calls over ~12 minutes seeing the SAME ChatInstructor nudge
            # ("You should ") — a StatusVerifier verdict never once reached [-1].
            # Since every advance path reads group_chat.messages[-1]
            # (state_transition's verdict parse, and this loop's completed /
            # breakdown / under-report branches), all of them were unreachable:
            # GOT COMPLETED 0, FAB-GUARD 0, advancing 0, 101 iterations, action
            # stuck at 1, and the user got "you haven't specified what task".
            #
            # Gate on SHORTER-THAN instead, and replace by slice-assignment: a
            # blind extend() onto a now-non-empty list would append a SECOND copy
            # of the whole conversation, and rebinding `group_chat.messages = [...]`
            # would detach the wrapper autogen holds a reference to.  Same source,
            # same shape, same single mechanism — no parallel path.
            _reuse_sync_group_log(group_chat, manager)

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
                        break
                    if _reuse_task.is_sla_breached() and not _reuse_task.sla_breached:
                        _reuse_task.mark_sla_breached()
                        current_app.logger.warning(f"[SLA] Task {_reuse_task_id} SLA breached in reuse loop")

            # ROBUST COMPLETION-ADVANCE.  With the manager now terminating on the
            # StatusVerifier 'completed' verdict (is_termination_msg=
            # _reuse_group_terminate), initiate_chat RETURNS the moment an action
            # completes and group_chat.messages[-1] IS that verdict.  The
            # ChatInstructor-'TERMINATE'-at-[-1] gate below does NOT fire on this
            # path (name=StatusVerifier, content=JSON), so advance here off the
            # TERMINAL verdict, using the KNOWN pipeline action — do NOT require the
            # LLM to echo action_id (the verdict shape is inconsistent: some carry
            # 'action_id', some don't) and read only [-1] not a [-4:] tail (history
            # accumulates across actions under clear_history=False, so a prior
            # action's verdict lingers — [-1] is always the just-terminated action).
            # One-advance-per-action guarded.  Measured live 2026-09-05: 'completed'
            # fired at 03:29:12 yet current_action_id stayed 1 because the group ran
            # to max_round without terminating and this loop never regained control.
            # Same _advance_reuse_action the TERMINATE path uses — no parallel path.
            try:
                _term = group_chat.messages[-1] if group_chat.messages else None
                _term_vj = retrieve_json((_term or {}).get('content') or '') if _term else None
                if (isinstance(_term_vj, dict)
                        and str(_term_vj.get('status', '')).lower() == 'completed'
                        and _reuse_current_action not in _reuse_advanced_actions):
                    _reuse_advanced_actions.add(_reuse_current_action)
                    current_app.logger.info(
                        f"reuse-w1-completed: terminal 'completed' verdict for action "
                        f"{_reuse_current_action} — advancing (manager terminated on verdict)")
                    if not _advance_or_steer(
                            user_prompt, _reuse_current_action,
                            "reuse-w1-completed", prompt_id,
                            manager, chat_instructor,
                            advanced_latch=_reuse_advanced_actions):
                        break  # finished recipe -> post-loop extractor (#798)
                    continue
            except Exception as _rc_err:
                # ERROR, not debug.  This handler wraps the ONLY call that moves
                # the pipeline forward, so when it catches, the walk silently
                # stops advancing -- and at debug nothing says so.
                #
                # MEASURED live 2026-09-11, session
                # 6c2dc0fc-7c93-4fe0-973e-f7466ff63f29_88719487304, window
                # 01:49:12-02:09:58, hevolveai excluded:
                #     "reuse-w1-completed ... for action 1 — advancing"   20
                #     "[REUSE] Action N TERMINATED, advancing"             0   <- the
                #         line immediately preceding the pointer write at :6072
                #     "[REUSE] Cannot advance action ..."                  0
                #     "already TERMINATED (idempotent)"                    0
                #     "[FABRICATED-COMPLETE] refusing to advance"          0
                #     "All N actions completed"                            0
                #     "[SUBTASK-HOLD]"                                     0   <- the
                #         only early return in _advance_or_steer
                # Six zeros: every visible exit of the advance is absent, so the
                # call raised and landed HERE.  The same log holds 45,599 lines
                # since restart and ZERO at "- DEBUG -", so this message has
                # never once reached production.  Result: action 1 executed its
                # tool (FAB-GUARD unrun=[]) and the walk spun on it for 20
                # minutes, reaching 1 of the agent's 9 actions.
                #
                # exc_info so the NEXT drive names the actual exception instead
                # of leaving it to be inferred from absences, which is what this
                # whole investigation had to do.  Same logger, same handler, no
                # new machinery -- only the level and the traceback.
                #
                # SIBLINGS, deliberately NOT touched here: :5210 turn-attach,
                # :5412 breakdown, :5460 under-reported-advance, :6049 FAB-GUARD
                # gate, :6089 spark.  They are the same CLASS (a failure logged
                # where production cannot see it) but none is PROVEN to be
                # swallowing anything today, and widening an unproven fix is how
                # a real defect gets buried under noise.  Tracked in #831.
                current_app.logger.error(
                    f"robust completion-advance FAILED for action "
                    f"{_reuse_current_action} — the pipeline did not advance "
                    f"for session: {user_prompt}: {_rc_err}", exc_info=True)

            # BREAKDOWN EXECUTION.  'requires_breakdown' is not a failure and not
            # an under-report: the model is saying the action needs decomposing,
            # and it supplies the subtasks.  The designed flow is
            #   add_subtasks() -> get_pending_subtasks() -> work each -> parent
            # and create_recipe.py:4503-4520 wires exactly that.
            #
            # Reuse did not.  state_transition:2635 persisted the subtasks and
            # returned a speaker; get_pending_subtasks and check_and_unblock_parent
            # were imported at :181-182 and NEVER CALLED, so the children went into
            # a ledger nobody read and the parent could never complete.  Measured
            # live 2026-09-06 on agent 89555447799: 35 requires_breakdown verdicts
            # for action 1, subtasks re-persisted each time (3 then 4), 107 rounds,
            # 0 advances, nothing executed.
            #
            # Same helpers, same message shape, same `continue` as create — the
            # loop that was missing, not a second mechanism.  Do NOT "fix" this by
            # treating requires_breakdown as an under-report and advancing on tool
            # evidence: that marks the parent done while its own subtasks sit
            # unrun, which is the force-completion the verification contract
            # forbids (I made that mistake first; see VERDICT_UNDERREPORT_STATUSES).
            try:
                _bd = group_chat.messages[-1] if group_chat.messages else None
                _bd_vj = retrieve_json((_bd or {}).get('content') or '') if _bd else None
                if (isinstance(_bd_vj, dict)
                        and str(_bd_vj.get('status', '')).lower() == 'requires_breakdown'):
                    # PERSIST HERE, not in state_transition.  The only
                    # add_subtasks call used to live in state_transition — which
                    # is autogen's SPEAKER SELECTOR.  Selection picks who talks
                    # NEXT, so it is never invoked on a round's final message,
                    # and the requires_breakdown verdict carrying the subtasks
                    # IS that final message.  Measured live 2026-09-06
                    # 18:07-18:33 (agent 89555447799): 27 [BREAKDOWN] entries
                    # here, 0 subtasks persisted, and across the last 24
                    # consecutive iterations ZERO state_transition events of any
                    # kind — action 9 span every ~6s on an empty ledger until
                    # the turn died, returning raw control JSON to the user.
                    #
                    # This block already parses the verdict and already reads
                    # the ledger back, so writing here makes producer and
                    # consumer the same scope — exactly the shape
                    # create_recipe.py:4506-4523 has always had.  Same
                    # canonical helper it uses (add_subtasks_to_ledger, already
                    # imported at :181 and until now never called), so this is
                    # one writer relocated, not a second one added.
                    _bd_subs = _bd_vj.get('subtasks') or []
                    if _bd_subs:
                        try:
                            _bd_ok = add_subtasks_to_ledger(
                                user_prompt, _reuse_current_action,
                                _bd_subs, user_ledgers)
                            current_app.logger.info(
                                f"[BREAKDOWN] action {_reuse_current_action} "
                                f"persisted {len(_bd_subs)} subtask(s) "
                                f"(ok={_bd_ok}) for session: {user_prompt}")
                        except Exception as _bd_add_e:
                            current_app.logger.warning(
                                f"[BREAKDOWN] add_subtasks_to_ledger failed for "
                                f"action {_reuse_current_action}, session "
                                f"{user_prompt}: {_bd_add_e}")
                    _pending = get_pending_subtasks(
                        user_prompt, _reuse_current_action, user_ledgers)
                    if _pending:
                        _next_sub = _pending[0]
                        # Mark it STARTED before handing it over.  The ledger
                        # only accepts PENDING -> IN_PROGRESS -> COMPLETED, so a
                        # child that is steered but never started can never be
                        # completed — complete_task returns a bare False and
                        # complete_task_and_route leaves the status untouched
                        # (both measured 2026-09-10).  Without this line hop 4
                        # of core/constants.py:1124-1128 is unreachable no
                        # matter who calls it.
                        if LedgerTaskStatus is not None:
                            try:
                                _next_sub.status = LedgerTaskStatus.IN_PROGRESS
                            except Exception:
                                pass
                        current_app.logger.info(
                            f"[BREAKDOWN] action {_reuse_current_action} has "
                            f"{len(_pending)} pending subtask(s) for session: "
                            f"{user_prompt} — working '{_next_sub.description[:60]}'")
                        chat_instructor.initiate_chat(
                            recipient=manager,
                            message=(_REUSE_SUBTASK_STEER_PREFIX
                                     + str(_next_sub.description)),
                            clear_history=False, silent=False)
                        continue
                    current_app.logger.info(
                        f"[BREAKDOWN] action {_reuse_current_action} reported "
                        f"requires_breakdown but the ledger holds no pending "
                        f"subtask for session: {user_prompt} — letting the normal "
                        f"paths decide")
            except Exception as _bd_err:
                current_app.logger.debug(f"breakdown execution skipped: {_bd_err}")

            # UNDER-REPORTED COMPLETION (mirror of the fabrication gate above).
            # A not-done verdict from an autonomous action whose tools have
            # EVIDENTLY run is a reporting failure, not unfinished work — see
            # the _REUSE_UNDERREPORT_STATUSES comment for the Scout2 ('pending')
            # and 89555447799 ('requires_breakdown') measurements.
            # Steer first (bounded), then let the normal advance path decide;
            # that path re-runs the fabrication gate, so nothing advances whose
            # tool did not actually execute.
            try:
                # Was `group_chat.messages[-1]`, which is the ChatInstructor
                # nudge 120 times in 128 (see _reuse_latest_verdict) -- that one
                # read is why this whole escape had never fired.
                _pend_vj = _reuse_latest_verdict(group_chat)
                _pend_st = str((_pend_vj or {}).get('status', '')).lower()
                if (isinstance(_pend_vj, dict)
                        and _pend_st in _REUSE_UNDERREPORT_STATUSES
                        and _reuse_current_action not in _reuse_advanced_actions
                        and _reuse_action_is_autonomous(user_prompt, _reuse_current_action)
                        and not _reuse_outstanding_tools(user_prompt, _reuse_current_action,
                                                        group_chat)):
                    _pk = (user_prompt, _reuse_current_action)
                    _pn = _reuse_pending_counts.get(_pk, 0)
                    if _pn < _REUSE_PENDING_STEER_MAX:
                        _reuse_pending_counts[_pk] = _pn + 1
                        current_app.logger.info(
                            f"[UNDER-REPORTED] action {_reuse_current_action} says "
                            f"{_pend_st!r} but its tools already executed — steering for a "
                            f"truthful verdict (attempt {_pn + 1}/{_REUSE_PENDING_STEER_MAX})")
                        chat_instructor.initiate_chat(
                            recipient=manager,
                            message=_REUSE_UNDER_REPORT_STEER,
                            clear_history=False, silent=False)
                        continue
                    _reuse_advanced_actions.add(_reuse_current_action)
                    current_app.logger.warning(
                        f"[UNDER-REPORTED] action {_reuse_current_action} still reports "
                        f"{_pend_st!r} after {_REUSE_PENDING_STEER_MAX} steers while its tools "
                        f"are evidenced as executed — advancing on the tool evidence")
                    if not _advance_or_steer(
                            user_prompt, _reuse_current_action,
                            "reuse-under-reported", prompt_id,
                            manager, chat_instructor,
                            advanced_latch=_reuse_advanced_actions):
                        break  # finished recipe -> post-loop extractor (#798)
                    continue
            except Exception as _ur_err:
                current_app.logger.debug(f"under-reported advance skipped: {_ur_err}")

            if _reuse_current_action in _reuse_advanced_actions:
                continue

            # group_chat.messages can be empty here (live 2026-08-30).  CAUSE
            # NOT ESTABLISHED -- see #725; transform_messages was my first
            # guess and is ruled out (it logs "10 -> 1", never "-> 0", and
            # helper.py:1786 only COMPARES pre/post, it does not mutate).
            # What matters for this line: the subscript SELECTS the body, so
            # the `except IndexError` below -- which opens on the next line --
            # can never cover it; the turn died with "Error getting response:
            # list index out of range".  Guard as create_recipe.py:4385 does.
            if group_chat.messages and group_chat.messages[-1]['name'] == 'ChatInstructor' and group_chat.messages[-1]['content'] == 'TERMINATE':
                current_app.logger.info(
                    f"group_chat.messages[-2]['content'] {group_chat.messages[-2]['content'][:10]}..")
                try:
                    try:
                        json_obj = json.loads(group_chat.messages[-2]["content"])
                    except (json.JSONDecodeError, ValueError):
                        json_obj = ast.literal_eval(group_chat.messages[-2]["content"])
                    current_app.logger.info(f'got json object {json_obj}')
                    if json_obj['status'].lower() in VERDICT_COMPLETION_STATUSES:
                        if not _advance_or_steer(
                                user_prompt, _reuse_current_action, "reuse-w1",
                                prompt_id, manager, chat_instructor,
                                claimed_action_id=int(json_obj.get(
                                    "action_id", _reuse_current_action)),
                                advanced_latch=_reuse_advanced_actions):
                            break  # finished recipe -> post-loop extractor (#798)
                        continue
                except IndexError:
                    # BREAK to the post-loop extractor, never `return ''`.
                    # #798 converted the six advance-path empty returns for
                    # exactly this reason and missed this one, whose own log
                    # line claims the recipe COMPLETED — the worst case to
                    # answer with nothing (see _advance_or_steer's docstring
                    # and #803/D37).  The extractor below already handles a
                    # genuinely empty history.
                    current_app.logger.info("Completed ALL ACTIONS")
                    break
                except Exception:
                    try:
                        json_obj = retrieve_json(group_chat.messages[-2]["content"])  # canonical parse (#95)
                        if json_obj:
                            current_app.logger.info(f'got json object {json_obj}')
                            if json_obj['status'].lower() in VERDICT_COMPLETION_STATUSES:
                                pipeline_action_id = user_tasks[user_prompt].current_action
                                if not _advance_or_steer(
                                        user_prompt, pipeline_action_id,
                                        "reuse-w1-regex", prompt_id,
                                        manager, chat_instructor,
                                        claimed_action_id=int(json_obj.get(
                                            "action_id", pipeline_action_id)),
                                        advanced_latch=_reuse_advanced_actions):
                                    break  # finished recipe -> post-loop extractor (#798)
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
                _now_action = _reuse_current_action_id(user_prompt)
                if _now_action != _budget_action:
                    # The action advanced: its successor starts with a full
                    # allowance instead of inheriting a spent counter.
                    _budget_action = _now_action
                    _action_rounds = 0
                    _action_evidence = _reuse_own_tool_progress(
                        user_prompt, _now_action, group_chat,
                        getattr(group_chat, 'agents', None) or [])
                    if _action_evidence is None:
                        _action_evidence = _reuse_evidence_count(group_chat)
                # PROGRESS RESETS THE ALLOWANCE.  Measured 2026-09-09:
                # action 2's tool executed at 05:55:49,887 and the per-action
                # cap ended the turn at 05:55:50,332 — 0.445 s later, with
                # the tool answers already spliced in by the sync at
                # 05:55:50,122.  A cap meant to stop STALLS ended an action
                # that was moving.  New tool evidence means the action is
                # working, so it earns a fresh window; the TURN ceiling above
                # still bounds the whole thing, and an unmeasurable evidence
                # count (-1) can never satisfy `>`.
                # PROGRESS MEANS THIS ACTION'S OWN TOOLS.  A global count
                # lets unrelated calls buy a fresh window forever -- agent
                # 88719487304 action 9, 2026-09-10 22:12:50-22:21:47, 17
                # rounds / 53 calls with unrun never shrinking.
                _evidence_now = _reuse_own_tool_progress(
                    user_prompt, _now_action, group_chat,
                    getattr(group_chat, 'agents', None) or [])
                if _evidence_now is None:
                    _evidence_now = _reuse_evidence_count(group_chat)
                if _evidence_now > _action_evidence:
                    current_app.logger.info(
                        f"[REUSE-ROUNDS] action {_now_action} produced new evidence for "
                        f"its own tool(s) ({_action_evidence} -> {_evidence_now}) — "
                        f"resetting its round allowance (turn spend "
                        f"{count}/{_round_budget})")
                    _action_evidence = _evidence_now
                    _action_rounds = 0
                if count >= _round_budget:
                    current_app.logger.warning(
                        f"[REUSE-ROUNDS] while1 exhausted {_round_budget} TURN rounds at "
                        f"action {user_tasks[user_prompt].current_action}/"
                        f"{len(user_tasks[user_prompt].actions)} — ending turn")
                    break
                if _action_rounds >= _REUSE_ROUNDS_PER_ACTION:
                    current_app.logger.warning(
                        f"[REUSE-ROUNDS] while1 action "
                        f"{user_tasks[user_prompt].current_action}/"
                        f"{len(user_tasks[user_prompt].actions)} used its "
                        f"{_REUSE_ROUNDS_PER_ACTION} rounds without completing "
                        f"— ending turn (turn spend {count}/{_round_budget})")
                    break

                count += 1
                _action_rounds += 1

                if user_prompt not in recipes or user_tasks[user_prompt].current_action > len(user_tasks[user_prompt].actions):
                    current_app.logger.error(
                        f"Cannot access recipe for current action {user_tasks[user_prompt].current_action}")
                    continue

                # Canonical reader (:3214), not a raw subscript.  A recipe
                # action that omits `can_perform_without_user_input` raised
                # KeyError here, and the blanket except below swallowed it as
                # "WE have some indexx error here: 'can_perform_without_user_
                # input'" — 20 times in ONE live turn (agent 74769894436,
                # 2026-09-07 00:12).  The cost is not the log line: the raise
                # happens BEFORE initiate_chat, so the "complete this task
                # independently" steering never reaches the group for that
                # round, and the action cannot advance on its own.  Every
                # sibling read of this field is already guarded (:2650, :2808)
                # or goes through the helper (:3719); this was the one site
                # left, and the helper's docstring already fixes the semantics
                # for an absent field ("-> False, so an unknown action is
                # never auto-advanced").
                if _reuse_action_is_autonomous(
                        user_prompt, user_tasks[user_prompt].current_action):
                    current_app.logger.info('GOT can_perform_without_user_input as true')
                    message = _REUSE_AUTONOMY_NUDGE
                    # chat_instructor, not helper — same reason as the
                    # StatusVerifier injection above: instructions must enter
                    # the group as user-role turns.
                    chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False, silent=False)

            except Exception as e:
                current_app.logger.error(f'WE have some indexx error here: {e}')
                error_message = traceback.format_exc()  # Capture full traceback
                current_app.logger.error(f"Error in get_agent_response indexx:\n{error_message}")

            if not group_chat.messages:
                # Defensive: the loop-top #725 sync (extend from
                # manager._oai_messages) normally keeps this populated once a
                # conversation exists.  If it is still empty here, end the turn
                # gracefully rather than raise IndexError on messages[-1].
                current_app.logger.warning(
                    'reuse: group chat history is empty mid-loop - ending the '
                    'turn instead of raising IndexError')
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
                        # RETURN the answer, do not drop it.  This return value IS
                        # the reply: hart_intelligence_entry:10165 assigns it and
                        # hands it to _chat_reply (see the note at :2023, "the
                        # /chat handler checks after chat_agent() returns").
                        # send_message_to_user1 is a SECOND, off-box leg whose
                        # URL is pointed at the wrong address (:524).  Measured
                        # 2026-09-09: it POSTs to aws_rasa.hertzai.com:9890
                        # (-> 106.51.181.24), which refuses; the service is
                        # REAL and RUNNING on the LAN box — sathish-linux-deep
                        # container `chatbot_pipeline` publishes
                        # 0.0.0.0:8001->9890/tcp, and POST
                        # http://192.168.0.9:8001/autogen_response answers in
                        # 35 ms.  9890 is the CONTAINER-INTERNAL port, never
                        # the published one.  So this leg delivers nothing on
                        # this deployment, and its failure comes back as a
                        # string nobody reads.  Address fix tracked in #803;
                        # it does not change the rule below.
                        # Returning '' here therefore lost the finished answer
                        # entirely: Nunba's empty-reply check then rerouted the
                        # user to the tool-less Tier-2 fallback, which answered
                        # from training data and contradicted the work this
                        # agent had just done and saved (#797/D31, #803/D37).
                        return json_obj['message2userfinal']
                except Exception as e:
                    current_app.logger.error(f"Error extracting JSON: {e}")
            elif f'message2'.lower() in content_lower:
                # Extract and process message
                try:
                    json_obj = retrieve_json(last_message['content'])
                    if json_obj and 'message2' in json_obj:
                        send_message_to_user1(user_id, json_obj['message2'], '', prompt_id)
                        # Same as the message2userfinal branch above — the
                        # return value is the reply, the POST is a dead leg.
                        return json_obj['message2']
                except Exception as e:
                    current_app.logger.error(f"Error extracting JSON: {e}")
            elif f'@user'.lower() not in content_lower:
                # _REUSE_AGENT_MENTIONS (module scope) — ONE list, shared with
                # the synthesis gate so "who is this message for" has a single
                # answer.  The four CamelCase entries this list used to carry
                # ("@StatusVerifier", "@Helper", "@Executor") were DEAD: the
                # subject here is `content_lower`, so an uppercase needle can
                # never match.  The five lowercase forms are the whole
                # effective set and are what the constant holds.
                agent_mentions = _REUSE_AGENT_MENTIONS

                if any(mention in content_lower for mention in agent_mentions):
                    agent_found = next((mention for mention in agent_mentions if mention in content_lower), None)
                    current_app.logger.info(f'Message directed to agent ({agent_found}), not sending to user')
                    current_app.logger.info(f'continuing since @user not in last message')
                    continue

                # NOTHING CAN DRIVE THIS ACTION — end the turn, don't re-loop.
                # Falling through here re-enters the loop with the conversation
                # unchanged.  For an AUTONOMOUS action that is fine: the next
                # pass calls initiate_chat above and the group really moves.
                # For a NON-autonomous one that call is gated off, and so is
                # every advance path (completion / breakdown / under-report all
                # test the same predicate), so the next pass is byte-identical
                # to this one and the loop spins at CPU speed until the round
                # cap stops it.
                #
                # Measured live 2026-09-09 (agent 33323830039, action 2 of 4,
                # 'cd C:\\Users\\sathi\\Documents', can_perform_without_user_input
                # = 'no'): all 13 `inside reuse while1` passes inside ONE 10 ms
                # window 07:27:31,833 -> ,843, 2 outbound LLM calls in the whole
                # phase, allowance gone, `[REUSE-ROUNDS] ... used its 12 rounds`.
                # Every other branch's log marker is absent from that window,
                # which is what identifies this path.
                #
                # Break to the post-loop extractor — the same exit the
                # TERMINATE/IndexError path takes (#798).  It already
                # synthesises the user-facing reply, observed one line after
                # the cap fired ('[SYNTHESIS] reply would be raw control JSON
                # — asking for the user-facing answer'), so this reaches the
                # identical outcome deterministically instead of after 11
                # wasted rounds.  Never `return ''` here (#797/D31).
                if not _reuse_action_is_autonomous(
                        user_prompt, user_tasks[user_prompt].current_action):
                    current_app.logger.warning(
                        f"[REUSE-NODRIVER] action "
                        f"{user_tasks[user_prompt].current_action} is not "
                        f"autonomous and the last message is addressed to no "
                        f"one — nothing would change on the next pass, so "
                        f"ending the turn instead of spinning")
                    break

            else:
                current_app.logger.info(f'@user in last message')
                break

        # if individual_recipe[currentaction_id-1]['can_perform_without_user_input'] == 'yes':
        #     return assistant
        if not group_chat.messages:
            current_app.logger.warning(
                'reuse: no messages to extract a reply from after trimming')
            return ''
        # The round ended ON the StatusVerifier verdict (_reuse_group_terminate),
        # so messages[-1] is control JSON and the extractor below would hand it
        # to the user verbatim.  Ask for the answer first — once, here, where
        # the turn is finalised exactly once (#799/D33).
        _reuse_synthesis_turn(user_prompt, group_chat, manager, chat_instructor)
        last_message = group_chat.messages[-1]
        # THE SYNTHESIS ROUND CAN LEAVE ITS OWN STEER AS THE TAIL.  It posts
        # the steer and then depends on the group taking a turn; when no turn
        # happens the steer IS messages[-1], and every line below hands the
        # tail to the user.  Measured live 2026-09-10 14:30:55, agent
        # 92583386981: HTTP 200 in 103.1s and the whole reply the user read
        # was _REUSE_SYNTHESIS_STEER verbatim.  The round produced no model
        # call at all — llm_outbound.jsonl holds 12 calls for that
        # request_id, the last at 14:32:38,150, and the steer was posted at
        # ,907 (20 -> 21 messages in 109 ms, and no "[SYNTHESIS] steer
        # failed", so initiate_chat returned normally having done nothing).
        #
        # Ask the SAME predicate the synthesis gate asks, so "is this an
        # answer?" has one definition here and there, and walk back to the
        # last message that really is one.  Unbounded by the action dispatch
        # on purpose: this is the turn being finalised, not an action being
        # credited (that is _reuse_written_answer's bounded job).  If nothing
        # qualifies, the tail stands — this narrows what is delivered and
        # never returns '' (#797/D31).
        if not _reuse_message_is_user_answer(last_message):
            for _cand in reversed(group_chat.messages):
                if _reuse_message_is_user_answer(_cand):
                    current_app.logger.info(
                        f"[SYNTHESIS] tail is not an answer "
                        f"({str((last_message or {}).get('name') or '?')}); "
                        f"delivering the last real one instead "
                        f"(session: {user_prompt}, "
                        f"from={_cand.get('name') or '?'}, "
                        f"head={str(_cand.get('content'))[:120]!r})")
                    last_message = _cand
                    break
        # len>1 matters: a lone TERMINATE would send [-2] off the front.
        if last_message['content'] == 'TERMINATE' and len(group_chat.messages) > 1:
            last_message = group_chat.messages[-2]

        content_lower = last_message['content'].lower()

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
        # #716: this string is the reply and gets SPOKEN by TTS - never
        # return raw internals ('Context size has been exceeded' was read
        # aloud to the user, observed live 2026-08-31)
        from core.agent_tools import user_facing_error
        return user_facing_error(e)


def get_flow_number(user_id, prompt_id):
    role = get_role(user_id, prompt_id)
    if not role:
        role = None
    current_app.logger.info(f'Got role as {role}')
    file_path = helper_fun.safe_prompt_path(prompt_id)
    with open(file_path, 'r') as f:
        data = json.load(f)
        # .get(), not [].  These two subscripts killed the whole /chat POST:
        # 2026-09-11 02:50:43, "Some ERROR IN REUSE RECIPE 'personas'" ->
        # Flask "Exception on /chat [POST]" -> KeyError: 'personas' here, so
        # the user's turn died before any agent work began.
        # MEASURED blast radius, counting only real agent prompt files
        # (<digits>.json, the shape safe_prompt_path resolves -- the prompts
        # dir is majority non-agent artifacts (#774), and the unfiltered
        # number reads as a misleading 69%):
        #     736 agent prompt files, 702 with 'personas', 34 WITHOUT.
        # Two shapes, neither malformed by accident: a cloud-synced stub
        # ('prompt_id','goal','user_id','name','is_active','image_url',
        # 'synced_at') which has no 'flows' either, and an authored agent
        # that has 'flows' but no 'personas'.  So both keys need .get().
        available_roles = [x['name'] for x in (data.get('personas') or [])]
        available_flows = data.get('flows') or []
    current_app.logger.info(f'Got available_roles as {available_roles}')
    if not available_roles:
        # Loud, not swallowed: an agent with no persona list is a DATA defect,
        # and 34 of them were un-chattable in silence.  Degrading to flow 0 is
        # what all four callers (:1123, :1746, :1901, :5925) would select for a
        # single-flow agent anyway -- they all feed role_number straight into
        # helper_fun.safe_prompt_path(prompt_id, role_number, 'recipe').
        _ctx_safe_log(
            'warning',
            f'prompt {prompt_id} declares no personas '
            f'({len(available_flows)} flow(s)); falling back to flow 0')
    role_number = 0
    if not role:
        role = available_roles[0] if available_roles else None
    for num, i in enumerate(available_flows):
        # `role` can now be None (no personas AND get_role found nothing);
        # .lower() on it would trade the KeyError for an AttributeError.
        if role and i['persona'].lower() == role.lower():
            role_number = num
            current_app.logger.info(f'GOT role index as {role_number}')
            # FIRST match, like the sibling get_role (:445, :452).  Without
            # this break every later flow on the same persona overwrote
            # role_number, so a persona owning N flows always selected the
            # LAST one and flows 0..N-2 were unreachable for reuse.
            # Live 2026-09-10, agent 92583386981 (5 flows, all "Executor"):
            # "GOT role index as 0..4" then it walked flow 4's
            # save_data_in_memory and never called flow 0's get_chat_history.
            # 58 of 711 stored agents have a persona owning 2+ flows; the
            # other 653 select the same index either way.
            break
    return role_number, role


def _ctx_safe_log(level, msg):
    """Log from anywhere in this module — with or without a Flask app context.

    Was ``_sched_log``, named for its first caller.  The name promised the
    helper belonged to create_schedule, so the prompt-narrow path was about
    to grow a second identical copy; renamed to say what it DOES, and both
    now share it.

    ``except Exception`` rather than ``except RuntimeError``: callers sit on
    fail-safe paths where an escaping logging error turns a cosmetic problem
    into a dead turn.  The fallback still EMITS to a module logger — a
    swallowed log is how a silent failure stays silent.
    """
    try:
        getattr(current_app.logger, level)(msg)
    except Exception:
        try:
            import logging
            getattr(logging.getLogger('reuse_recipe'), level)(msg)
        except Exception:
            pass


def create_schedule(prompt_id, user_id):
    _ctx_safe_log('info', 'INSIDE Create Schedule')
    user_prompt = f'{user_id}_{prompt_id}'
    role_number, role = get_flow_number(user_id, prompt_id)
    with open(helper_fun.safe_prompt_path(prompt_id, role_number, 'recipe'), 'r') as f:
        config = json.load(f)
        config = _normalize_flow_recipe(config)  # tolerate per-action recipe in flow slot
        recipes[user_prompt] = config
    try:
        if 'scheduled_tasks' in config and len(config['scheduled_tasks']) > 0:
            _ctx_safe_log('info', 'Creating scheduled tasks')
            for i in config['scheduled_tasks']:
                if role and i['persona'].lower() == role.lower():
                    trigger = CronTrigger.from_crontab(i['cron_expression'])
                    job_id = f"job_{int(time.time())}"
                    scheduler.add_job(execute_python_file, trigger=trigger, id=job_id,
                                      args=[i['job_description'], user_id, prompt_id, i['action_entry_point']])
                    _ctx_safe_log('info', f'Successfully created scheduler job {i["persona"]}')

        # Only schedule the 2s visual-poll job when the action API it depends
        # on is actually configured.  With ACTION_API='' (unset in config.json)
        # this job can't work and would error every 2s forever (see the
        # call_visual_task guard) — don't create it at all on such boxes.
        if ACTION_API:
            _ctx_safe_log('info', 'Creating Visual scheduled tasks')
            trigger = IntervalTrigger(seconds=int(2))
            job_id = f"job_{int(time.time())}"
            scheduler.add_job(call_visual_task, trigger=trigger, id=job_id,
                              args=['get past 1 mins visual information', user_id, prompt_id])
            _ctx_safe_log('info', 'Successfully created scheduler job')
        else:
            _ctx_safe_log('info', 'Skipping 2s visual-poll job — ACTION_API not configured')
        if 'visual_scheduled_tasks' in config and len(config['visual_scheduled_tasks']) > 0:
            for i in config['visual_scheduled_tasks']:
                if role and i['persona'].lower() == role.lower():
                    trigger = CronTrigger.from_crontab(i['cron_expression'])
                    job_id = f"job_{int(time.time())}"
                    scheduler.add_job(call_visual_task, trigger=trigger, id=job_id,
                                      args=[i['job_description'], user_id, prompt_id])
                    _ctx_safe_log('info', f'Successfully created scheduler job {i["persona"]}')
    except Exception as e:
        _ctx_safe_log('error', f'Some Error in creating scheduled tasks error:{e}')


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
    Returns (next_action_id, True) if advanced, or (None, False) if all actions done or state error.
    """
    # FABRICATION GATE (canonical single point — EVERY advance path calls this):
    # refuse to mark a tool-naming action COMPLETED when its specific tool never
    # executed in the group chat.  The StatusVerifier LLM self-attests
    # "completed"/"done" and the model fabricates the tool's output (live
    # 2026-09-03: revenue agent "92% verified", zero get_api_revenue_stats
    # execution).  Reaches the group chat + agents via the session registry, so
    # it works no matter which loop (w1/w2/main) advanced.
    #
    # The refusal RE-STEERS (bounded), it does not silently fail open after one
    # hold.  Live 2026-09-05 (Trading 33204307184): the StatusVerifier claimed
    # action 1 "completed" BEFORE its google_search ever ran; the old one-shot
    # budget was spent on that first claim and the very next claim advanced an
    # action whose tool had still not executed — exactly the "force-completed by
    # a nudge" the verification contract forbids.  Now each refusal records the
    # unrun tools so the caller can steer the agent to actually call them
    # (_reuse_fab_steer_message); only after _REUSE_FAB_STEER_MAX real re-steers
    # do we advance anyway — and then LOUDLY, so a non-tool-backed action is
    # never reported as verified.
    try:
        from hartos.lifecycle_hooks import get_registered_groupchat
        _gc = get_registered_groupchat(user_prompt)
        if _gc is not None:
            _agents = list(getattr(_gc, 'agents', None) or [])
            _fab = _reuse_fabricated_tools(user_prompt, current_action_id, _gc, _agents)
            _rk = (user_prompt, current_action_id)
            if _fab:
                _n = _reuse_resteer_counts.get(_rk, 0)
                if _n < _REUSE_FAB_STEER_MAX:
                    _reuse_resteer_counts[_rk] = _n + 1
                    _reuse_fab_pending[_rk] = list(_fab)
                    # "no real result" not "never executed": since a6fd615e5
                    # this also holds a tool that RAN and returned one of
                    # TOOL_FAILURE_RESULTS.  Live 2026-09-07 03:03:12 the
                    # refusal that fired here was exactly that case, and a log
                    # line reading "never executed" sends the next reader
                    # hunting a registration bug that is not there.
                    current_app.logger.warning(
                        f"[FABRICATED-COMPLETE] refusing to advance action "
                        f"{current_action_id}: its tool(s) {_fab} produced no real "
                        f"result (never called, or ran and returned a failure) "
                        f"— re-steering the agent to run "
                        f"them (attempt {_n + 1}/{_REUSE_FAB_STEER_MAX})")
                    return None, False
                current_app.logger.error(
                    f"[FABRICATED-COMPLETE] action {current_action_id} still claims "
                    f"completion with tool(s) {_fab} producing no real result after "
                    f"{_REUSE_FAB_STEER_MAX} re-steers — advancing to avoid a "
                    f"permanent stall; this action's output is NOT tool-backed")
            elif not _reuse_action_declares_tool(user_prompt, current_action_id) \
                    and _reuse_written_answer(_gc) is None:
                # SAME GATE, THE OTHER HALF OF THE EVIDENCE.  The tool check
                # above answers "did the NAMED tools execute" and says in its
                # own docstring that it never touches "prose actions that name
                # no tool".  For an action whose deliverable IS the text there
                # is nothing for it to check, so `completed` rested on the
                # model's word — for that whole class of action there was no
                # evidence gate at all.
                #
                # Measured live 2026-09-10 09:08:58-09:10:17 (agent
                # 88094979291, "summarize into exactly three bullet points"):
                # all four actions completed, unrun=none, and NOT ONE message
                # in the 12-entry group log was written by the Assistant to
                # the user — every name=Assistant entry was a verbatim echo of
                # the dispatch.  Action 3's verdict still read "Output
                # formatted successfully with exactly three bullet points."
                # No bullet points existed anywhere in that conversation.
                #
                # The same agent DID write the deliverable on the 03:37 run.
                # Same recipe, same four "completed" verdicts, opposite
                # outcome — the pipeline could not tell those runs apart.
                # This is what makes them distinguishable: evidence, not
                # variance.
                #
                # Rides the EXISTING machinery — same _reuse_resteer_counts
                # budget, same _reuse_fab_pending record, same
                # _reuse_fab_steer_message caller contract, same loud advance
                # once the budget is spent.  No prompt text and no recipe is
                # changed by this.
                _n = _reuse_resteer_counts.get(_rk, 0)
                if _n < _REUSE_FAB_STEER_MAX:
                    _reuse_resteer_counts[_rk] = _n + 1
                    _reuse_fab_pending[_rk] = [_REUSE_NO_OUTPUT_SENTINEL]
                    current_app.logger.warning(
                        f"[FABRICATED-COMPLETE] refusing to advance action "
                        f"{current_action_id}: it names no tool, so its "
                        f"deliverable is the text itself — and the group "
                        f"produced no message for the user during it "
                        f"(attempt {_n + 1}/{_REUSE_FAB_STEER_MAX})")
                    return None, False
                current_app.logger.error(
                    f"[FABRICATED-COMPLETE] action {current_action_id} still "
                    f"claims completion with no output produced after "
                    f"{_REUSE_FAB_STEER_MAX} re-steers — advancing to avoid a "
                    f"permanent stall; this action's output does NOT exist")
    except Exception as _fg_err:
        current_app.logger.debug(f"[FAB-GUARD] advance-gate skipped: {_fg_err}")
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
            return None, False
        current_app.logger.info(
            f"[REUSE] Action {current_action_id} already TERMINATED (idempotent)")

    current_app.logger.info(f'[REUSE] Action {current_action_id} TERMINATED, advancing')
    # BEFORE the pointer and the watermark move -- this is the last moment
    # the finished action's evidence can still be scoped correctly.
    _stamp_action_result_vacuity(user_prompt, current_action_id)
    next_id = current_action_id + 1
    user_tasks[user_prompt].current_action = next_id
    # Stamp the evidence watermark HERE — the one site that writes
    # current_action — so the window and the action id cannot disagree.
    _stamp_action_evidence_watermark(user_prompt)

    if next_id > len(user_tasks[user_prompt].actions):
        current_app.logger.info(f'[REUSE] All {len(user_tasks[user_prompt].actions)} actions completed')
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
        return None, False

    safe_set_state(user_prompt, next_id, ActionState.ASSIGNED, f"{reason}: next assigned")
    safe_set_state(user_prompt, next_id, ActionState.IN_PROGRESS, f"{reason}: starting")
    return next_id, True


def _reuse_seed_message(user_prompt, message):
    """The opening turn's message: the user's words PLUS the current action's
    execution command.

    Actions 2..N are commanded explicitly — every advance posts
    ``_build_reuse_action_message`` ("Perform this action -> Action #N: ...
    follow these steps: [{... 'tool_name': 'google_search' ...}]").  Action 1
    never was: the loop opened with the raw user text and nothing told the
    group chat to execute anything.

    Measured live 2026-09-05 (Trading 33204307184, 467s drive, from
    llm_outbound.jsonl): of 39 autogen.reuse calls, 0 carried "Perform this
    action ->" while 30 carried the recipe in their SYSTEM prompt.  So the
    agents could SEE the recipe but were never told to run step 1 —
    google_search executed 0x, `Retrieved current_action_id: 1` 44x, no
    advance, no outcome.  A closed loop: no action-1 command -> nothing
    executes -> nothing completes -> no advance -> the builder never runs.

    It stayed hidden because agents whose request happens to imply action 1
    (18088688973 "what is Tokyo's population" -> google_search) work off the
    raw question alone; only meta requests ("run my recipe") stall.

    ADDITIVE, never a replacement — the user's intent must reach the chat, and
    the agents that already succeed on the raw text must not regress.  Fail-safe:
    any problem reading the recipe returns the user's message unchanged, which
    is exactly today's behaviour.
    """
    # Both outcomes log at INFO on purpose.  The first cut logged only the
    # failure, at DEBUG — which this deployment does not emit, so "no skip
    # line" could not distinguish "seeded" from "fell back silently".  A
    # signal that cannot show its own failure is not a signal: live on
    # 2026-09-05 the command reached the wire 6x for Trading and 0x for Auto
    # Research on the SAME code path, and the log could not say why.
    try:
        current_action_id = user_tasks[user_prompt].current_action
        seeded = f"{message}\n\n{_build_reuse_action_message(user_prompt, current_action_id)}"
        current_app.logger.info(
            f"[REUSE-SEED] seeded action {current_action_id} "
            f"(+{len(seeded) - len(message)} chars)")
        return seeded
    except Exception as _seed_err:
        current_app.logger.info(
            f"[REUSE-SEED] FELL BACK to the bare user message — the opening "
            f"turn will NOT command an action: {_seed_err!r}")
        return message


def _build_reuse_action_message(user_prompt, action_id):
    """Build the action execution message for REUSE mode."""
    action_message = user_tasks[user_prompt].get_action(action_id - 1)['action']
    recipe_actions = recipes[user_prompt].get('actions', [])
    if action_id - 1 < len(recipe_actions):
        # str() because `steps` is model-authored and its type is not fixed:
        # in prompts/18088688973_0_recipe.json action 1 carries a LIST of
        # {description, tool_name} dicts while actions 2..6 carry a str.  Used
        # raw as a dict key that raised TypeError("unhashable type: 'list'"),
        # which killed the builder for exactly the action the opening turn
        # seeds — measured live 2026-09-05 14:06:38.  Trading 33204307184,
        # whose action 1 is a str, was unaffected and got its command 6x on
        # the wire from this same code.  Where the key is already a str,
        # str(s) is s, so the rendered message is byte-identical.
        steps = [{str(x['steps']): {'tool_name': x.get('tool_name', None),
                                    'code': x.get('generalized_functions', None)}} for x in
                 recipe_actions[action_id - 1].get('recipe', [])]
    else:
        steps = []
        current_app.logger.warning(f"[REUSE] No recipe for action {action_id} — executing without steps")
    return (f"{_REUSE_ACTION_MESSAGE_PREFIX}{action_id}:{action_message}"
            f"\n follow these steps: {steps}")


# A registry tool name as `attach_for_names` compares it: the registry key, or
# `{tool}_{endpoint}`.  Dots are legal (`tts.package_installer` is real, 5 uses
# in the banked corpus).  The >=3-char floor is what stops a Windows drive
# letter surviving as the candidate `C` when a path is split on ':'.
_TOOL_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]{2,}$')


def _tool_name_candidates(raw):
    """The registry-shaped identifiers inside one authored ``tool_name``.

    ``attach_for_names`` compares EXACTLY (core/agent_tools.py:372,
    ``if fn not in want``), which is right for a matcher.  The authoring model,
    however, routinely writes the tool AND its argument into the single field,
    so an exact comparison rejects a tool that is registered and working.

    Measured 2026-09-07 across all 165 banked recipes in
    ~/Documents/Nunba/data/prompts — 1,473 steps, 978 naming a tool:

        identifier-shaped   848
        prose-shaped        130      37 of 165 files (22.4%) carry >=1

    Splitting the 130 on ':' / ',' recovers an identifier for 34 of them:

        execute_windows_or_android_command: click the 'Search' button
        google_search, crawl4ai, retry_logic

    HOW MUCH THAT ACTUALLY BUYS — measured, after an earlier version of this
    docstring overstated it.  ``attach_for_names`` iterates
    ``service_tool_registry._tools`` ONLY, and the live app registers just 13
    names into it (payments x3, seo_audit_score, gh_pr_open, crawl4ai,
    crawl4ai_crawl, pocket_tts x3, acestep x3 — counted from its own
    "Registered service tool:" log lines).  ``execute_windows_or_android_command``
    and ``google_search`` are NOT there: they are core tools registered by the
    decorators at :1483-1948, a different path this matcher never reads.  So of
    the 34 recovered names exactly ONE (crawl4ai) is one the matcher can act
    on; the 30 execute_windows_or_android_command and 3 google_search
    occurrences stay unattachable by this route no matter how they are spelled.

    This function is therefore CORRECTNESS, not throughput: it stops the reader
    handing the matcher strings that are not names, and it stops 90 junk fields
    (pasted Python, 'N/A') from being offered at all.  Do not cite it as the
    reason a tool started working.

    The other 96 are not tool names at all — the model pasting Python source
    line by line (``ENGINE_REGISTRY = router.ENGINE_REGISTRY``,
    ``for eid in engine_ids``) or the literal ``N/A``.  The identifier shape
    drops every one of them, so they yield nothing instead of a plausible
    candidate.

    Deliberately NOT a fuzzy or semantic match: a candidate still has to match
    a registry entry exactly downstream.  This only recovers a name the author
    actually wrote; it never guesses which tool an action "probably meant".
    An unknown candidate is free — the matcher already ignores it.
    """
    out = []
    for tok in re.split(r'[:,]', str(raw or '')):
        tok = tok.strip().strip('\'"`')
        if _TOOL_IDENT_RE.match(tok) and tok not in out:
            out.append(tok)
    return out


# Filled by the last _merge_tool_answers call; read by the sync log line
# below so the numbers travel without changing the function's return.
_merge_last_stats: dict = {}


# `answered_call_ids` (imported from hartos.helper) is the ONE reader of
# "which tool calls does this message answer".  It lived here briefly as a
# private copy; that duplicated knowledge helper.py already owns
# (is_consolidated_response keys on the same 'tool_responses' shape), and
# helper.py is where the placeholder-vs-real decision is made.  One reader
# on both sides, so a shape autogen changes cannot be half-learned.


def _merge_tool_answers(base, buffers):
    """Splice the REAL tool answers from sibling buffers into `base`.

    ``manager._oai_messages`` is keyed PER AGENT, so each value is one seat's
    view of the group conversation.  The #725 sync picks the LONGEST as the
    transcript, and measured live 2026-09-07 (agent 18088688973, 16 of 16
    samples) that buffer holds ZERO tool answers:

        User*:n=30,calls=9,answers=0   ... StatusVerifier:n=30,calls=9,answers=0
        Assistant:n=14,calls=10,answers=4      <-- only buffer with answers

    Six buffers are identical-length broadcast copies (the manager relays every
    message to every member); the EXECUTING agent's buffer carries the results
    and is SHORTER, so `max(key=len)` can never reach it.  Length is not a weak
    proxy for completeness here — it is anti-correlated with it.

    Downstream that is #786 in full: helper.py:1897 sees a tool_call_id with no
    answer, calls it "historical pending", and mints HISTORICAL_TOOL_PLACEHOLDER
    (:1940) to satisfy an API that rejects an unanswered tool_call — 83
    placeholders over 30 repair events in one drive.  The model then had a
    45-character string where a page of search results should have been, and
    produced a brief with no citations from a search that really ran.

    MERGE, don't re-pick: the long buffer is the right SKELETON (it has every
    agent's turns); the short one only has answers the skeleton lacks.  Choosing
    the answer-bearing buffer instead would drop the other agents' messages.  So
    keep the base and fill exactly the slots helper.py would otherwise
    placeholder — the same operation already in the codebase, with the real
    result instead of a manufactured one.

    Never invents content: a call no buffer answers is left missing, so
    helper.py still fills it and the fabrication gate still sees the truth.
    Never raises — it runs on every sync of a live turn.
    """
    try:
        out = list(base or [])
        announced = []
        for m in out:
            # A `tool_calls` key is what makes a message a CALL.  Do NOT also
            # require role=='assistant': _oai_messages[agent] stores the
            # conversation from THAT AGENT'S SEAT, so in the buffer the sync
            # picks (keyed "User") every other agent's call arrives as
            # role='user'.  The first version of this function gated on
            # 'assistant' and therefore found ZERO announced ids on real data
            # — 15 sync events, "spliced 0", while a sibling buffer sat on 10
            # answers (measured 2026-09-07 after deploying 50e4df7ae).
            if not isinstance(m, dict):
                continue
            for tc in (m.get('tool_calls') if isinstance(m.get('tool_calls'), list) else []):
                if isinstance(tc, dict) and tc.get('id'):
                    announced.append(tc['id'])
        if not announced:
            return out
        answered = set()
        for m in out:
            answered |= answered_call_ids(m)
        missing = [i for i in announced if i not in answered]
        if not missing:
            return out

        # (anchor_id, message) pairs — anchor is the call the message is
        # positioned after.  A consolidated reply answers several ids but is
        # spliced ONCE, so `claimed` stops it being inserted per id.
        found = []
        claimed = set()
        answers_seen = 0
        for buf in (buffers or []):
            for m in (buf or []):
                if not isinstance(m, dict) or m.get('role') != 'tool':
                    continue
                answers_seen += 1
                ids = answered_call_ids(m)
                hits = [i for i in missing if i in ids and i not in claimed]
                if hits:
                    claimed.update(hits)
                    found.append((hits[0], m))
        # DIAGNOSTIC.  Two live runs spliced 0 while a sibling buffer visibly
        # held answers, and the deployed pyc was byte-verified against source,
        # so the gap is in the SET RELATION, not the deploy.  These three
        # numbers separate the remaining possibilities without another guess:
        #   missing>0, answers_seen=0  -> no buffer holds any answer at all
        #   missing>0, answers_seen>0, found=0 -> answers exist but under
        #        tool_call_ids the picked buffer never announced (different
        #        conversations, not one conversation from two seats)
        #   found>0 -> the merge should splice; anything else is a bug here
        # The live answer was the middle case: answers_seen>0 with found=0,
        # because the ids sat inside tool_responses (see answered_call_ids in helper.py).
        try:
            _merge_last_stats.clear()
            _merge_last_stats.update(announced=len(announced), missing=len(missing),
                                     answers_seen=answers_seen, found=len(claimed),
                                     msgs=len(found))
        except Exception:
            pass
        if not found:
            return out

        for tid, msg in found:
            pos = None
            for i, m in enumerate(out):
                # Role-agnostic for the same reason as the announce scan above.
                if not isinstance(m, dict):
                    continue
                tcs = m.get('tool_calls') if isinstance(m.get('tool_calls'), list) else []
                if any(isinstance(tc, dict) and tc.get('id') == tid for tc in tcs):
                    pos = i
                    break
            if pos is None:
                continue
            # After the assistant's existing run of answers, so a parallel
            # tool_calls block keeps one answer per call in order — the same
            # placement rule helper.py:1946-1951 uses.
            j = pos + 1
            while j < len(out) and isinstance(out[j], dict) and out[j].get('role') == 'tool':
                j += 1
            out.insert(j, msg)
        return out
    except Exception:
        return list(base or [])


def _recipe_section_for_action(system_message, individual_recipe, action_id):
    """``system_message`` with the recipe block narrowed to ONE action.

    The reuse system prompt (built at L1310) interpolates the WHOLE recipe
    list between ``<recipeStart><generalized_functionsStart>`` and
    ``<generalized_functionsEnd><recipeEnd>``.  For a 24-action agent that is
    ~15,879 estimated tokens, and the turn only ever asks for one action.

    WHY THIS EXISTS — measured at the wire 2026-09-08 19:44 on the installed
    build, agent 89555447799 (growth.local.executor, 24 actions):

        wire-trim: the TOOL SCHEMA alone is 8487 tokens against an n_ctx of
        12288 (66 tool(s)) -- no amount of message trimming can make this fit

        [TRIM] left-trimmed 13 msg(s) + 39363 char(s)
               est tokens 15879 -> 4308, budget 5484

    66 tools take 69% of the window; the recipe prompt does not fit in what
    is left; the trim removes from the FRONT.  The body that actually reached
    the model carried action_id [13..24] ONLY, while the user message said
    "Perform this action -> Action #2".  The agent then told the user
    "those actions do not exist in my immediate view ... please paste the
    list of specific actions" -- which was TRUE of its context.

    Sending the dispatched action instead of all 24 is both smaller and
    strictly more correct.  The action-title list (``role_actions``, a
    separate interpolation) still carries the overall plan, so the model does
    not lose sight of the sequence.

    NOT the loader: 24 load attempts, 0 errors, indices 1..24, all files
    present and json.load()-clean.  ``individual_recipe`` is built whole at
    L1189 and is INDEXED ELSEWHERE (e.g. L2682
    ``individual_recipe[_known_aid - 1]['can_perform_without_user_input']``),
    so this returns a NEW STRING and never mutates that list.

    Returns ``system_message`` unchanged for an out-of-range or non-integer
    action_id, a missing recipe block, an empty recipe list, or any
    exception: this runs on every reuse turn and must not be able to kill
    one.
    """
    OPEN, CLOSE = '<generalized_functionsStart>', '<generalized_functionsEnd>'
    try:
        if not system_message or not individual_recipe:
            return system_message
        if not isinstance(action_id, int) or isinstance(action_id, bool):
            return system_message
        if not 1 <= action_id <= len(individual_recipe):
            return system_message
        i0 = system_message.find(OPEN)
        i1 = system_message.find(CLOSE, i0 + 1) if i0 >= 0 else -1
        if i0 < 0 or i1 <= i0:
            return system_message
        return (system_message[:i0 + len(OPEN)]
                + str([individual_recipe[action_id - 1]])
                + system_message[i1:])
    except Exception:
        return system_message


def _narrow_assistant_to_current_action(user_prompt):
    """Re-pin the assistant's system prompt to the action the LEDGER commands.

    Takes no action id on purpose.  ``user_tasks[user_prompt].current_action``
    is the single field ``_advance_reuse_action`` writes (:4297) and every
    dispatch site already reads, so keying off it here makes prompt and
    command agree by construction — an id passed in could be the caller's
    stale copy, which is the exact failure this fixes.

    WHY A SECOND CALL SITE EXISTS.  The narrow in ``get_agent_response`` runs
    once per entry into that function.  The group chat then advances INSIDE a
    single autogen loop, and those rounds never re-enter it.  Measured live
    2026-09-08 20:21-20:27 on agent 89555447799:

        20:22:19  [REUSE] Action 1 TERMINATED, advancing
        20:24:07  [FAB-GUARD] action 2 names ['execute_windows_or_android_command']

    yet of 37 recipe-bearing calls on the wire, the 32 that came AFTER the
    advance all still carried ``'action_id': 1``.  Same defect as the
    [13..24] slice the narrowing replaced — the prompt describes an action
    the pipeline has left — so the fix is to follow the ledger, not the turn.

    Returns True when the prompt actually changed, False otherwise (already
    narrowed to this action, no agents cached, no recipe stashed).  Never
    raises: it runs on the dispatch path and must not be able to kill a turn.
    """
    try:
        agents = user_agents.get(user_prompt)
        if not agents:
            return False
        assistant = agents[0]
        narrowed = _recipe_section_for_action(
            assistant.system_message,
            getattr(assistant, '_hart_individual_recipe', None),
            user_tasks[user_prompt].current_action)
        if not narrowed or narrowed == assistant.system_message:
            return False
        was = len(assistant.system_message or '')
        # CALL, never assignment — create_recipe.py:5328 records that
        # assigning to update_system_message shadows the bound method.
        assistant.update_system_message(narrowed)
        _ctx_safe_log('info',
                      f"Tier-1 prompt narrow: action "
                      f"{user_tasks[user_prompt].current_action} -> system "
                      f"{was} to {len(narrowed)} chars")
        return True
    except Exception as err:
        _ctx_safe_log('debug', f"prompt narrow skipped: {err}")
        return False


def _reuse_action_tool_names(user_prompt, action_id):
    """The tool names the given action's recipe steps declare.

    Reads the same ``recipes[user_prompt]['actions'][n]`` entry that
    ``_build_reuse_action_message`` renders into the turn message, and returns
    the tool names it declares — from each step's ``tool_name`` AND from the
    action's own title, which the authoring pipeline writes in the very same
    ``<tool>: <argument>`` convention.  Both, because the pipeline uses both:
    measured on agent 88719487304's saved recipe, ``execute_coding_task``
    appears as a step ``tool_name`` on some actions and ONLY as the title on
    action 3.

    Used by the Tier-1 per-turn attach.  Before this, the attach chose tools
    only from ``detect_goal_tags(message)`` — a prose keyword scan — and so
    could miss the tool the action names outright: measured live 2026-09-06 on
    agent 89555447799, whose action 1 declares ``tool_name: google_search``
    while the scan inferred ``['coding']`` and attached 0 tools.

    Returns [] for a prose action that names no tool, an out-of-range id, or
    an unknown session — never raises, because the attach hook runs on every
    round and must not be able to kill a turn.
    """
    try:
        actions = (recipes[user_prompt] or {}).get('actions') or []
        if not 1 <= action_id <= len(actions):
            return []
        action = actions[action_id - 1] or {}
        out = []
        for step in (action.get('recipe') or []):
            # _tool_name_candidates, not the raw field: the authored value is
            # frequently `<real tool>: <its argument>`, which no exact match
            # can ever resolve.  Junk (pasted source, 'N/A') yields nothing.
            for name in _tool_name_candidates((step or {}).get('tool_name')):
                if name not in out:
                    out.append(name)
        # The TITLE is the other authoring site, in that same
        # `<tool>: <argument>` shape -- so it gets the SAME extractor, not a
        # second rule, and junk still yields nothing.
        #
        # Without it the attach and the fabrication gate disagreed, and after
        # c5a2035cb taught the gate to read the action text that disagreement
        # became a deadlock: the gate demanded a tool the attach would never
        # attach.  Measured live 2026-09-10, agent 88719487304 action 3, rid
        # d58b-230854 -- title "execute_coding_task: 'Write Python script to
        # parse HART OS documentation...'", steps ['google_search', '', ''].
        # 23:12:20 unrun=['google_search','execute_coding_task'], four
        # 'completed' verdicts refused, ZERO "Tier-1 named attach" lines, and
        # 23:18:19 the action ended on its round budget having never run the
        # tool that is its entire job.
        for name in _tool_name_candidates(action.get('action')):
            if name not in out:
                out.append(name)
        return out
    except Exception:
        return []


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

    request_id_list[user_prompt] = request_id
    try:
        if file_id:
            recent_file_id[user_id] = file_id

        # RUN BOUNDARY — deliberately OUTSIDE the cache guard below.
        # `chat_agent` is where a reuse RUN starts; `user_agents` is where AGENT
        # OBJECTS are cached, and the two have opposite lifetimes (agents want
        # to stay warm, run state wants to reset per run).  Binding the reset to
        # the cache miss is the defect this line exists to close: the reset
        # inside `create_agents_for_user` (:1081) only ever runs on a cache MISS,
        # and `user_agents` touch-on-reads its own TTL, so a regularly-used agent
        # never misses and never resets.
        #
        # Passing `user_tasks` is what makes this the RUN boundary and not just a
        # state wipe — clear_action_states then rewinds the action pointer too,
        # but only when the previous run went past the end of the recipe.  Both
        # stores, one authority; see its docstring for why that is safe here.
        clear_action_states(user_prompt, user_tasks)

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
                # ONE builder for the action command.  This was a verbatim
                # inline copy — same get_action lookup, same steps
                # comprehension, same template — so the str() coercion that
                # fixed the builder left THIS copy still raising
                # TypeError("unhashable type: 'list'") on a list-valued
                # `steps`, and it never carried the builder's
                # `action_id - 1 < len(recipe_actions)` bounds check either.
                message = _build_reuse_action_message(
                    user_prompt, user_tasks[user_prompt].current_action)
                # Same invariant as the advance path: the prompt must name the
                # action this message commands.  create_agents_for_user was
                # just called above, so the assistant here is freshly built
                # with ALL N recipes — exactly the body the wire-trim eats.
                _narrow_assistant_to_current_action(user_prompt)
                # message = "let's perform the actions availabe in sequence\nIMP instruction: keep track of action id you are working on."
                result = chat_instructor.initiate_chat(manager, message=message,
                                                       speaker_selection={"speaker": "assistant"}, clear_history=False)

                count = 0                  # spent on the whole turn
                _action_rounds = 0         # spent on the CURRENT action
                _budget_action = _reuse_current_action_id(user_prompt)
                _action_evidence = _reuse_own_tool_progress(
                    user_prompt, _budget_action, group_chat,
                    getattr(group_chat, 'agents', None) or [])
                if _action_evidence is None:
                    _action_evidence = _reuse_evidence_count(group_chat)
                _budget_action = _reuse_current_action_id(user_prompt)
                _round_budget = _reuse_turn_round_budget(user_prompt)
                while True:
                    current_app.logger.info('inside while2')

                    # === LEDGER v2.0: Heartbeat + Budget/SLA ===
                    current_action_id = user_tasks[user_prompt].current_action
                    ledger = user_ledgers.get(user_prompt)
                    if ledger:
                        action_task = ledger.tasks.get(f"action_{current_action_id}")
                        if action_task:
                            action_task.heartbeat()
                            if action_task.is_budget_exhausted():
                                current_app.logger.warning(
                                    f"[BUDGET] action_{current_action_id} budget exhausted in while2")
                                break
                            if action_task.is_sla_breached() and not action_task.sla_breached:
                                action_task.mark_sla_breached()

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
                            if json_obj['status'].lower() in VERDICT_COMPLETION_STATUSES:
                                if not _advance_or_steer(
                                        user_prompt, current_action_id,
                                        "reuse-w2", prompt_id,
                                        manager, chat_instructor,
                                        claimed_action_id=int(json_obj.get(
                                            "action_id", current_action_id))):
                                    break  # finished recipe -> post-loop extractor (#798)
                                continue
                        except Exception:
                            try:
                                json_obj = retrieve_json(group_chat.messages[-2]["content"])  # canonical parse (#95)
                                if json_obj:
                                    current_app.logger.info(f'got json object {json_obj}')
                                    if json_obj['status'].lower() in VERDICT_COMPLETION_STATUSES:
                                        if not _advance_or_steer(
                                                user_prompt, current_action_id,
                                                "reuse-w2-regex", prompt_id,
                                                manager, chat_instructor,
                                                claimed_action_id=int(json_obj.get(
                                                    "action_id", current_action_id))):
                                            break  # finished recipe -> post-loop extractor (#798)
                                        continue

                                else:
                                    raise ValueError('No json found')
                            except IndexError:
                                # BREAK, not `return ''` — same reason as the
                                # while1 twin above (#798 / #803 D37).  The
                                # extractor after this loop is what turns the
                                # finished conversation into the reply.
                                current_app.logger.info("Completed ALL ACTIONS")
                                break
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
                    # Same per-action/per-turn split as while1 (#790/D23) —
                    # one semantics for both loops, from one constant — and
                    # the same progress reset, so a working action is not
                    # capped in either loop.
                    _now_action = _reuse_current_action_id(user_prompt)
                    if _now_action != _budget_action:
                        _budget_action = _now_action
                        _action_rounds = 0
                        _action_evidence = _reuse_own_tool_progress(
                            user_prompt, _now_action, group_chat,
                            getattr(group_chat, 'agents', None) or [])
                        if _action_evidence is None:
                            _action_evidence = _reuse_evidence_count(group_chat)
                    # PROGRESS MEANS THIS ACTION'S OWN TOOLS.  A global count
                    # lets unrelated calls buy a fresh window forever -- agent
                    # 88719487304 action 9, 2026-09-10 22:12:50-22:21:47, 17
                    # rounds / 53 calls with unrun never shrinking.
                    _evidence_now = _reuse_own_tool_progress(
                        user_prompt, _now_action, group_chat,
                        getattr(group_chat, 'agents', None) or [])
                    if _evidence_now is None:
                        _evidence_now = _reuse_evidence_count(group_chat)
                    if _evidence_now > _action_evidence:
                        current_app.logger.info(
                            f"[REUSE-ROUNDS] action {_now_action} produced new evidence for "
                            f"its own tool(s) ({_action_evidence} -> {_evidence_now}) — "
                            f"resetting its round allowance (turn spend "
                            f"{count}/{_round_budget})")
                        _action_evidence = _evidence_now
                        _action_rounds = 0
                    count += 1
                    _action_rounds += 1
                    if count >= _round_budget:
                        current_app.logger.warning(
                            f"[REUSE-ROUNDS] while2 exhausted {_round_budget} TURN rounds at "
                            f"action {user_tasks[user_prompt].current_action}/"
                            f"{len(user_tasks[user_prompt].actions)} — ending turn")
                        break
                    if _action_rounds >= _REUSE_ROUNDS_PER_ACTION:
                        current_app.logger.warning(
                            f"[REUSE-ROUNDS] while2 action "
                            f"{user_tasks[user_prompt].current_action}/"
                            f"{len(user_tasks[user_prompt].actions)} used its "
                            f"{_REUSE_ROUNDS_PER_ACTION} rounds without completing "
                            f"— ending turn (turn spend {count}/{_round_budget})")
                        break
                    # role = get_role(user_id,prompt_id)
                    last_message = group_chat.messages[-1]
                    if f'@user'.lower() not in last_message['content'].lower():
                        continue
                    else:
                        current_app.logger.info(f'@user in last message')
                        break

                # Guard the subscript, exactly as the while1 extractor does
                # (:4272).  while2 never had it, so an empty history raised
                # IndexError here instead of ending the turn — and the
                # "Completed ALL ACTIONS" break above now reaches this line
                # on precisely the short-history case that raised it.
                if not group_chat.messages:
                    current_app.logger.warning(
                        'reuse while2: no messages to extract a reply from')
                    return ''
                last_message = group_chat.messages[-1]

                # len>1 matters: a lone TERMINATE would send [-2] off the
                # front — same reason the while1 twin carries this check.
                if last_message['content'] == 'TERMINATE' and len(group_chat.messages) > 1:
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
