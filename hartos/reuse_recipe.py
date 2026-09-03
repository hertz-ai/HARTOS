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
    NUNBA_WEB_FETCH_POLICY,
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

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="tool",
                             description="Processes user-defined commands on a personal Windows or Android system.")
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

    # #743 Tier-0: the MAIN leg's core tools come from the same factory as
    # the time/visual legs — its 19 inline decorator stacks are deleted
    # above (they had drifted from canon: current_app.logger inside worker
    # threads, mandatory start/end on get_chat_history, a direct-minicpm
    # get_user_camera_inp that bypassed helper_fun's local-first path).
    # Name-filtered to exactly the set the main leg registered before the
    # migration: zero schema growth, no new tools; per-tag gating at the
    # factory is the next step and depends on this consolidation.
    _MAIN_LEG_CORE = {
        'txt2img', 'img2txt', 'save_data_in_memory', 'get_saved_metadata',
        'get_data_by_key', 'get_user_id', 'get_prompt_id', 'Generate_video',
        'get_user_uploaded_file', 'get_user_camera_inp', 'get_chat_history',
        'search_visual_history', 'search_long_term_memory',
        'save_to_long_term_memory',
        'send_message_to_user', 'send_presynthesized_video_to_user',
        'send_message_in_seconds', 'google_search',
    }
    # create_scheduled_jobs is NOT in the filter: the factory's twin is a
    # create-flow stub; the real live-scheduling version stays inline
    # above (see the #511 name-collision note at its definition).
    register_core_tools(
        [t for t in core_tools if t[0] in _MAIN_LEG_CORE], helper, assistant)

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

        def request_tools(need: str) -> str:
            from core.agent_tools import discover_and_attach
            return discover_and_attach(need, helper, assistant,
                                       service_tool_registry, _attached_names)
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
    multi_role_agent.description = 'INTERNAL persona-routing helper — NOT a selectable speaker. Never choose multi_role_agent as the next role under any circumstances.'
    executor.description = 'A specialized agent responsible for executing code and handling response management. It ensures computational tasks are performed accurately and returns results effectively.'
    verify.description = 'The status-verification agent. Select StatusVerifier immediately after Assistant, Helper, or Executor has done the work for the current action (produced a tool result or finished a reply): it checks whether that action is complete and then supplies the next action. Whenever the last message reports a result or finishes a step, choose StatusVerifier next.'

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
        # #725 snapshot: keep the last non-empty view so get_agent_response can
        # still see the 'completed' verdict + TERMINATE after autogen empties
        # group_chat.messages.  Runs on every selection incl. the final
        # TERMINATE turn, so the snapshot ends as [.., completed, TERMINATE].
        try:
            if messages:
                _reuse_msg_snapshot[user_prompt] = list(messages)
        except Exception:
            pass
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



            # Process JSON responses from StatusVerifier
            temp_message = messages[-1]["content"].replace("'", '"')
            pattern = r'\{.*?\}'  # getting all json from text
            matches = re.findall(pattern, temp_message, re.DOTALL)

            try:
                json_objects = [json.loads(match) for match in matches]
                current_app.logger.info(f'Got Json as {len(json_objects)}')

                if json_objects:
                    last_json = json_objects[-1]
                    current_app.logger.info(f'last json as {last_json}')

                    if 'status' in last_json.keys() and last_json['status'].lower() == 'completed':
                        current_app.logger.info('GOT COMPLETED FOR ACTION in state_transition')
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
                    # the action spun until the count==4 cap, never advancing and
                    # (for requires_breakdown) silently discarding the subtasks.
                    # Handle them per the StatusVerifier system_message: route
                    # error/pending back to the helper to actually finish/fix the
                    # work, and persist requires_breakdown's subtasks to the
                    # ledger (the imported-but-unwired add_subtasks machinery).
                    # Bounded by the loop's count==4; NEVER fake-advances — only a
                    # truthful 'completed' advances, a persistent error/pending
                    # ends the turn honestly instead of looping.
                    _st = str(last_json.get('status', '')).lower()
                    if _cp_yes and _st == 'requires_breakdown':
                        _subs = last_json.get('subtasks') or []
                        try:
                            if _subs and user_prompt in user_ledgers:
                                user_ledgers[user_prompt].add_subtasks(_known_aid, _subs)
                                current_app.logger.info(
                                    f'reuse: requires_breakdown -> persisted {len(_subs)} subtasks '
                                    f'to ledger for action {_known_aid}; routing to helper to execute')
                        except Exception as _bd_e:
                            current_app.logger.warning(f'reuse: requires_breakdown add_subtasks failed: {_bd_e}')
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

            return "auto"
        except Exception as e:
            current_app.logger.error(f"Error in state_transition: {e}")
            current_app.logger.error(traceback.format_exc())
            return "auto"

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
                    _d = msg if isinstance(msg, dict) else {}
                    _tc = [(t.get('id') or '')[:8] for t in (_d.get('tool_calls') or [])]
                    _ph = 'PH' if 'Placeholder response for historical' in str(_d.get('content') or '') else ''
                    current_app.logger.info(
                        "[APPEND-PROBE] n=%d role=%s name=%s tc=%s tcid=%s clen=%d %s" % (
                            len(self), _d.get('role'), _d.get('name', ''), _tc,
                            (_d.get('tool_call_id') or '')[:8], len(str(_d.get('content') or '')), _ph))
                except Exception:
                    pass
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

    return assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group


# One-shot re-steer budget per (user_prompt, action_id).  Bounds the
# fabricated-completion guard below so it can NEVER loop or permanently stall
# an action — after one re-steer it fails open and advances.
_reuse_resteer_counts = {}

# #725: autogen intermittently empties group_chat.messages after initiate_chat
# returns, which blinds the advancement gate in get_agent_response (it reads
# messages[-1]/[-2] to detect a 'completed' verdict + TERMINATE).  The verdict
# IS seen inside state_transition, where groupchat.messages is still populated
# on every speaker-selection turn — including the final TERMINATE turn.  So
# state_transition snapshots the last NON-EMPTY view here, keyed by the same
# user_prompt the loop uses, and the loop falls back to it ONLY when the live
# list is empty.  This is a read-only fallback of the SAME conversation, not a
# second history: when messages are present the behaviour is unchanged.
_reuse_msg_snapshot = {}


def _reuse_action_text(user_prompt, current_action):
    """Text of the current reuse action (1-based; get_action is 0-based, see
    the StatusVerifier steering site).  Fail-open: '' on any error."""
    try:
        task = user_tasks.get(user_prompt)
        if task is None:
            return ''
        return str(task.get_action(current_action - 1) or '')
    except Exception:
        return ''


def _reuse_fabricated_tools(user_prompt, current_action, group_chat, agents):
    """Registered tool names the current action NAMES but that produced ZERO
    tool results anywhere in the group chat — i.e. a fabricated 'completed'.

    Deliberately COARSE + fail-open (returns [] unless certain): only flags
    when (a) the action text names a registered tool AND (b) the whole chat has
    no role=='tool' result at all.  This catches the pure-fabrication case
    (live 2026-09-03: revenue agent claimed get_api_revenue_stats returned 90%
    with zero tool_execution) while NEVER stalling an action once any tool has
    run, and NEVER touching prose actions that name no tool.
    """
    try:
        text = _reuse_action_text(user_prompt, current_action).lower()
        if not text:
            return []
        names = set()
        for ag in agents:
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
        referenced = [n for n in names if n and len(n) > 3 and n.lower() in text]
        if not referenced:
            return []
        # Which of the referenced tools ACTUALLY executed?  A tool ran if the
        # chat carries a role=='tool' result named for it OR an assistant
        # tool_call for it.  (The earlier coarse "any tool ran anywhere" check
        # was defeated by unrelated MemoryGraph tool results — live
        # revwarm5407: get_api_revenue_stats never ran yet a memory tool did,
        # so any_tool_ran was true and the guard let the fabrication through.)
        executed = set()
        # Fall back to the #725 snapshot when the live list has been emptied, so
        # the fabrication guard still sees the tool results and does not fail
        # open on an empty history (which would let a fabricated 'completed'
        # advance unchecked).
        _fab_msgs = (getattr(group_chat, 'messages', None)
                     or _reuse_msg_snapshot.get(user_prompt) or [])
        for m in _fab_msgs:
            if not isinstance(m, dict):
                continue
            if m.get('role') == 'tool' and m.get('name'):
                executed.add(m.get('name'))
            for tc in (m.get('tool_calls') or []):
                fn = ((tc or {}).get('function') or {}).get('name')
                if fn:
                    executed.add(fn)
        unrun = [n for n in referenced if n not in executed]
        try:
            current_app.logger.info(
                f"[FAB-GUARD] action {current_action} names tool(s) {referenced}; "
                f"executed={sorted(executed)}; unrun={unrun}")
        except Exception:
            pass
        return unrun
    except Exception:
        return []


def _reuse_should_resteer(user_prompt, action_id, group_chat, agents):
    """(True, tool_name) if the action's 'completed' claim is fabricated and
    the one-shot re-steer budget is unused (consumes it); else (False, None).
    Fail-open on any uncertainty."""
    fab = _reuse_fabricated_tools(user_prompt, action_id, group_chat, agents)
    if not fab:
        return (False, None)
    key = (user_prompt, action_id)
    if _reuse_resteer_counts.get(key, 0) >= 1:
        return (False, None)
    _reuse_resteer_counts[key] = _reuse_resteer_counts.get(key, 0) + 1
    return (True, fab[0])


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
        _amap = set((getattr(assistant, '_function_map', {}) or {}).keys())
        _emap = set((getattr(group_chat, 'agent_by_name', lambda n: None)('Executor') and
                     getattr(group_chat.agent_by_name('Executor'), '_function_map', {}) or {}).keys())
        current_app.logger.info(
            "[EXEC-MAP-PROBE] gsearch_in_assistant=%s assistant_fmap_n=%d gsearch_in_executor=%s sample=%s" % (
                'google_search' in _amap, len(_amap), 'google_search' in _emap, sorted(_amap)[:12]))
    except Exception as _ep:
        try:
            current_app.logger.info("[EXEC-MAP-PROBE] failed: %s" % _ep)
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
                _new = [t for t in detect_goal_tags(message or '')
                        if t not in _unlocked]
                if _new:
                    from core.agent_tools import attach_for_tags
                    from integrations.agent_engine.goal_manager import get_tool_tags
                    from integrations.service_tools import service_tool_registry
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

        result = user_proxy.initiate_chat(manager, message=message, speaker_selection={"speaker": "assistant"},
                                          clear_history=False)

        count = 0
        while True:
            current_app.logger.info('inside reuse while1')

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

            # group_chat.messages can be empty here (live 2026-08-30).  CAUSE
            # NOT ESTABLISHED -- see #725; transform_messages was my first
            # guess and is ruled out (it logs "10 -> 1", never "-> 0", and
            # helper.py:1786 only COMPARES pre/post, it does not mutate).
            # What matters for this line: the subscript SELECTS the body, so
            # the `except IndexError` below -- which opens on the next line --
            # can never cover it; the turn died with "Error getting response:
            # list index out of range".  Guard as create_recipe.py:4385 does.
            # #725: read the live list, or the last-non-empty snapshot when
            # autogen has emptied it, so the completed+TERMINATE verdict is not
            # lost (which used to freeze every action at 1).
            _live_msgs = group_chat.messages if group_chat.messages else _reuse_msg_snapshot.get(user_prompt, [])
            if _live_msgs and _live_msgs[-1]['name'] == 'ChatInstructor' and _live_msgs[-1]['content'] == 'TERMINATE':
                current_app.logger.info(
                    f"group_chat.messages[-2]['content'] {_live_msgs[-2]['content'][:10]}..")
                try:
                    try:
                        json_obj = json.loads(_live_msgs[-2]["content"])
                    except (json.JSONDecodeError, ValueError):
                        json_obj = ast.literal_eval(_live_msgs[-2]["content"])
                    current_app.logger.info(f'got json object {json_obj}')
                    if json_obj['status'].lower() == 'completed':
                        _llm_action_id = int(json_obj.get("action_id", _reuse_current_action))
                        if _llm_action_id != _reuse_current_action:
                            current_app.logger.warning(
                                f"[HALLUCINATION?] LLM claims action_id={_llm_action_id} "
                                f"but pipeline assigned {_reuse_current_action}")
                        _rs_do, _rs_tool = _reuse_should_resteer(
                            user_prompt, _reuse_current_action, group_chat, (assistant, helper))
                        if _rs_do:
                            current_app.logger.warning(
                                f"[FABRICATED-COMPLETE] action {_reuse_current_action} reported "
                                f"completed but its tool {_rs_tool} produced no result (0 tool "
                                f"executions in chat) — re-steering once to actually run it")
                            chat_instructor.initiate_chat(
                                recipient=manager,
                                message=("Do NOT mark this action complete — you have not actually "
                                         f"called the required tool. Call {_rs_tool} now and use the "
                                         "REAL value it returns; do not invent numbers. Then report "
                                         "status."),
                                clear_history=False, silent=False)
                            continue
                        _next, _ok = _advance_reuse_action(user_prompt, _reuse_current_action, "reuse-w1", prompt_id)
                        if not _ok:
                            return ''
                        # advanced on a truthful completion: drop the consumed
                        # snapshot so its 'completed' can't be re-read for the
                        # next action, and refresh the per-action work budget so
                        # a multi-action recipe can reach its final action (count
                        # is otherwise a whole-turn cap that stops part-way).
                        _reuse_msg_snapshot.pop(user_prompt, None)
                        count = 0
                        user_message = _build_reuse_action_message(user_prompt, _next)
                        chat_instructor.initiate_chat(recipient=manager, message=user_message, clear_history=False,
                                                      silent=False)
                        continue
                except IndexError:
                    current_app.logger.info("Completed ALL ACTIONS")
                    return ''
                except Exception:
                    try:
                        json_obj = retrieve_json(_live_msgs[-2]["content"])  # canonical parse (#95)
                        if json_obj:
                            current_app.logger.info(f'got json object {json_obj}')
                            if json_obj['status'].lower() == 'completed':
                                _known = user_tasks[user_prompt].current_action
                                _llm_claimed = int(json_obj.get("action_id", _known))
                                if _llm_claimed != _known:
                                    current_app.logger.warning(
                                        f"[HALLUCINATION?] LLM claims action_id={_llm_claimed} "
                                        f"but pipeline has {_known}")
                                _rs_do2, _rs_tool2 = _reuse_should_resteer(
                                    user_prompt, _known, group_chat, (assistant, helper))
                                if _rs_do2:
                                    current_app.logger.warning(
                                        f"[FABRICATED-COMPLETE] action {_known} reported completed "
                                        f"but its tool {_rs_tool2} produced no result — re-steering "
                                        f"once to actually run it")
                                    chat_instructor.initiate_chat(
                                        recipient=manager,
                                        message=("Do NOT mark this action complete — you have not "
                                                 f"actually called the required tool. Call {_rs_tool2} "
                                                 "now and use the REAL value it returns; do not invent "
                                                 "numbers. Then report status."),
                                        clear_history=False, silent=False)
                                    continue
                                _next2, _ok2 = _advance_reuse_action(user_prompt, _known, "reuse-w1-regex", prompt_id)
                                if not _ok2:
                                    return ''
                                _reuse_msg_snapshot.pop(user_prompt, None)
                                count = 0
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
                    # UNBLOCK: after the work round, deterministically request a
                    # StatusVerifier verdict.  The 4B never @mentions it on its
                    # own (measured 0×), so the advancement chain
                    # (StatusVerifier 'completed' -> state_transition:2468 ->
                    # chat_instructor default_auto_reply 'TERMINATE' ->
                    # loop:3040 -> _advance_reuse_action) never fired and the
                    # action froze at 1.  Reuses the canonical verify message
                    # and the deterministic @statusverifier route in
                    # state_transition's agent_mapping (2438); bounded by the
                    # count==4 break above so a non-'completed' verdict cannot
                    # spin forever.
                    try:
                        _sv_ap = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action - 1)
                        # Build with concatenation (not one f-string) so the JSON
                        # braces stay in plain string segments — matches the
                        # canonical construction at the StatusVerifier injection
                        # above; a lone '}' inside an f-string is a SyntaxError.
                        _sv_msg = ('Hey @StatusVerifier Agent, Please verify the status of the action '
                                   + f'{user_tasks[user_prompt].current_action}: {_sv_ap}'
                                   + '\n performed and Respond in the following format '
                                   + '{"status": "status here","action": "current action","action_id": '
                                   + f'{user_tasks[user_prompt].current_action}'
                                   + ',"message": "message here"}')
                        chat_instructor.initiate_chat(recipient=manager, message=_sv_msg,
                                                      clear_history=False, silent=False)
                    except Exception as _sv_e:
                        current_app.logger.warning(f'reuse: StatusVerifier steer failed: {_sv_e}')

            except Exception as e:
                current_app.logger.error(f'WE have some indexx error here: {e}')
                error_message = traceback.format_exc()  # Capture full traceback
                current_app.logger.error(f"Error in get_agent_response indexx:\n{error_message}")

            # Re-resolve after the injection block: the nested initiate_chats
            # above just ran a real conversation whose verdict state_transition
            # snapshotted, even though autogen may have emptied the live list.
            # Only end the turn when BOTH the live list and the snapshot are
            # empty (nothing happened) — not when autogen merely reset the
            # accumulator (#725), which used to freeze every action at 1.
            _live_msgs = group_chat.messages if group_chat.messages else _reuse_msg_snapshot.get(user_prompt, [])
            if not _live_msgs:
                current_app.logger.warning(
                    'reuse: group chat history is empty mid-loop - ending the '
                    'turn instead of raising IndexError (cause not established)')
                break
            last_message = _live_msgs[-1]
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
        if not group_chat.messages:
            current_app.logger.warning(
                'reuse: no messages to extract a reply from after trimming')
            return ''
        last_message = group_chat.messages[-1]
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
    Returns (next_action_id, True) if advanced, or (None, False) if all actions done or state error.
    """
    # FABRICATION GATE (canonical single point — EVERY advance path calls this):
    # refuse to mark a tool-naming action COMPLETED when its specific tool never
    # executed in the group chat.  The StatusVerifier LLM self-attests
    # "completed"/"done" and the model fabricates the tool's output (live
    # 2026-09-03: revenue agent "92% verified", zero get_api_revenue_stats
    # execution).  Bounded to ONE refusal per action then fails open (advances)
    # so it can never permanently stall.  Reaches the group chat + agents via the
    # session registry, so it works no matter which loop (w1/w2/main) advanced.
    try:
        from hartos.lifecycle_hooks import get_registered_groupchat
        _gc = get_registered_groupchat(user_prompt)
        if _gc is not None:
            _agents = list(getattr(_gc, 'agents', None) or [])
            _fab = _reuse_fabricated_tools(user_prompt, current_action_id, _gc, _agents)
            _rk = (user_prompt, current_action_id)
            if _fab and _reuse_resteer_counts.get(_rk, 0) < 1:
                _reuse_resteer_counts[_rk] = _reuse_resteer_counts.get(_rk, 0) + 1
                current_app.logger.warning(
                    f"[FABRICATED-COMPLETE] refusing to advance action "
                    f"{current_action_id}: its tool(s) {_fab} produced NO result "
                    f"in the group chat (fabricated completion) — holding for one retry")
                return None, False
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
    next_id = current_action_id + 1
    user_tasks[user_prompt].current_action = next_id

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
                                _w2_next, _w2_ok = _advance_reuse_action(user_prompt, _w2_current, "reuse-w2", prompt_id)
                                if not _w2_ok:
                                    return ''
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
                                        _w2_next2, _w2_ok2 = _advance_reuse_action(user_prompt, _w2_current, "reuse-w2-regex", prompt_id)
                                        if not _w2_ok2:
                                            return ''
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
