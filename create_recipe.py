"""create_recipe.py"""
import ast
import autogen
import os
from typing import Annotated, Optional, Dict, Tuple, List, Any
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import requests
from autobahn.asyncio.component import Component, run
import uuid
import asyncio
import traceback
from datetime import datetime
import time
from autogen.coding import DockerCommandLineCodeExecutor
import re
from autogen import register_function
import json
from autogen import ConversableAgent
from flask import current_app
from helper import topological_sort, fix_json, retrieve_json, fix_actions, Action, ToolMessageHandler, strip_json_values, apply_autogen_fix_on_startup, load_vlm_agent_files
import helper as helper_fun
import threading
from concurrent.futures import ThreadPoolExecutor
from autogen.agentchat.contrib.capabilities import transform_messages, transforms
from autogen.cache.in_memory_cache import InMemoryCache
from json_repair import repair_json
from crossbarhttp import Client
client = Client('http://aws_rasa.hertzai.com:8088/publish')

# Create thread pool executor for async Crossbar publishing
crossbar_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='crossbar_publish')

def publish_async(topic, message, timeout=2.0):
    """
    Publish to Crossbar in a background thread without blocking the main request.

    Args:
        topic: Crossbar topic to publish to
        message: Message payload (dict or JSON string)
        timeout: Maximum time to wait for publish (default: 2.0 seconds)
    """
    def _publish():
        import socket
        try:
            # Set socket timeout to prevent long waits
            original_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(timeout)

            client.publish(topic, message)
            current_app.logger.debug(f"Successfully published to {topic}")
        except Exception as e:
            current_app.logger.error(f"Error publishing to {topic}: {e}")
        finally:
            # Restore original timeout
            if original_timeout is not None:
                socket.setdefaulttimeout(original_timeout)

    # Submit to executor without waiting for result
    crossbar_executor.submit(_publish)

# Add Smart Ledger for persistent task tracking - using agent_ledger package (from gpt4.1)
try:
    from agent_ledger import (
        SmartLedger, Task, TaskType, TaskStatus, ExecutionMode,
        create_ledger_from_actions, get_production_backend
    )
    from agent_ledger.factory import create_production_ledger, get_or_create_ledger
    HAS_SMART_LEDGER = True
except ImportError:
    HAS_SMART_LEDGER = False
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
    register_ledger_for_session   # Register ledger for auto-sync
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
import cv2
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

# Then add the 4 hooks to your get_response_group while loop
# Then manually add the 4 hooks to your get_response_group while loop
# Set up a dedicated logger that doesn't depend on Flask context
log_dir = "logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# Create a custom logger with timestamp in filename
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = os.path.join(log_dir, f"agent_system_{timestamp}.log")

# Configure the logger
tool_logger = logging.getLogger("agent_logger")
tool_logger.setLevel(logging.DEBUG)

# File handler with rotation (10 MB max size, keep 10 backup files)
file_handler = logging.handlers.RotatingFileHandler(
    log_file,
    maxBytes=10*1024*1024,  # 10 MB
    backupCount=10
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

# Decorator for logging tool execution



def log_tool_execution(func):
    if inspect.iscoroutinefunction(func):
        # For async functions
        @wraps(func)
        async def wrapper(*args, **kwargs):
            tool_logger.info(f"TOOL EXECUTION START: {func.__name__}")
            tool_logger.info(f"Arguments: {args}, Keyword Arguments: {kwargs}")
            try:
                result = await func(*args, **kwargs)
                if not isinstance(result, str):
                    tool_logger.warning(f"Tool function {func.__name__} returned non-string type: {type(result)}")
                    result = str(result)
                tool_logger.info(f"TOOL EXECUTION SUCCESS: {func.__name__}")
                tool_logger.info(
                    f"Result: {result[:100]}..." if len(result) > 100 else f"Result: {result}"
                )
                return result
            except Exception as e:
                tool_logger.error(f"TOOL EXECUTION ERROR: {func.__name__} - {e}")
                tool_logger.exception("Exception details:")
                error_response = {
                    "status": "error",
                    "tool_function": func.__name__,
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "suggestion": "Check logs for detailed traceback information"
                }
                # ... (add any special-case suggestions like NameError, KeyError here) ...
                error_json = json.dumps(error_response)
                tool_logger.info(f"Returning error response: {error_json}")
                return f"Tool execution failed: {error_json}"
        return wrapper
    else:
        # For regular (sync) functions
        @wraps(func)
        def wrapper(*args, **kwargs):
            tool_logger.info(f"TOOL EXECUTION START: {func.__name__}")
            tool_logger.info(f"Arguments: {args}, Keyword Arguments: {kwargs}")
            try:
                result = func(*args, **kwargs)
                # If it returns a coroutine by accident, run it to completion
                if asyncio.iscoroutine(result):
                    tool_logger.info(f"Detected coroutine return from {func.__name__}, running it to completion")
                    result = asyncio.get_event_loop().run_until_complete(result)
                if not isinstance(result, str):
                    tool_logger.warning(f"Tool function {func.__name__} returned non-string type: {type(result)}")
                    result = str(result)
                tool_logger.info(f"TOOL EXECUTION SUCCESS: {func.__name__}")
                tool_logger.info(
                    f"Result: {result[:100]}..." if len(result) > 100 else f"Result: {result}"
                )
                return result
            except Exception as e:
                tool_logger.error(f"TOOL EXECUTION ERROR: {func.__name__} - {e}")
                tool_logger.exception("Exception details:")
                error_response = {
                    "status": "error",
                    "tool_function": func.__name__,
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "suggestion": "Check logs for detailed traceback information"
                }
                # ... (add any special-case suggestions here) ...
                error_json = json.dumps(error_response)
                tool_logger.info(f"Returning error response: {error_json}")
                return f"Tool execution failed: {error_json}"
        return wrapper


from core.session_cache import TTLCache  # early import — needed before first TTLCache usage below
from core.cache_loaders import load_agent_data, load_user_ledger, load_user_simplemem

scheduler = BackgroundScheduler()
scheduler.start()

user_agents: Dict[str, Tuple[Any, Any, Any, Any, Any, Any, Any]] = TTLCache(ttl_seconds=7200, max_size=500, name='create_user_agents')
time_agents = TTLCache(ttl_seconds=7200, max_size=500, name='create_time_agents')
# Mode-aware config_list: cloud/regional use external LLM, flat uses local llama.cpp
_node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
if _node_tier in ('regional', 'central') and os.environ.get('HEVOLVE_LLM_ENDPOINT_URL'):
    config_list = [{
        "model": os.environ.get('HEVOLVE_LLM_MODEL_NAME', 'gpt-4.1-mini'),
        "api_key": os.environ.get('HEVOLVE_LLM_API_KEY', 'dummy'),
        "base_url": os.environ['HEVOLVE_LLM_ENDPOINT_URL'],
        "price": [0.0025, 0.01]
    }]
else:
    _llama_port = os.environ.get('LLAMA_CPP_PORT', '8080')
    config_list = [{
        "model": 'Qwen3-VL-4B-Instruct',
        "api_key": 'dummy',
        "base_url": f'http://localhost:{_llama_port}/v1',
        "price": [0, 0]
    }]
# Per-request model config override (speculative execution, hive compute routing)
def get_llm_config():
    """Get LLM config — checks thread-local override before falling back to global.
    This enables per-dispatch model routing for speculative execution."""
    from threadlocal import thread_local_data
    override = thread_local_data.get_model_config_override()
    return {"cache_seed": None, "config_list": override or config_list, "max_tokens": 1500}

# Performance: cached config loading (shared with helper.py, reuse_recipe.py)
from core.config_cache import get_config as _get_config
from core.http_pool import pooled_post, pooled_get, pooled_request
from core.event_loop import get_or_create_event_loop

config = _get_config()
STUDENT_API = config.get('STUDENT_API', '')
ACTION_API = config.get('ACTION_API', '')
redis_client = redis.StrictRedis(
    host='azure_all_vms.hertzai.com', port=6369, db=0)


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

database_url = 'https://mailer.hertzai.com'


def save_conversation_db(text,user_id,prompt_id,database_url,request_id):
    headers = {'Content-Type': 'application/json'}
    data = {
        "request": 'VIDEO GENERATION FROM GENERATE_VIDEO',
        "response": text.strip(),
        "user_id": int(user_id),
        "conv_bot_name": 'GPT-4o',
        "topic": f'{prompt_id}',
        "revision": False,
        "dialogue_id": None,
        "card_type": 'Custom GPT',
        "qid": None,
        "layout_id": None,
        "layout_list": '[]',
        "request_token": 0,
        "response_token": 0,
        "request_id": request_id,
        "historical_request_id": str('[]')
    }
    res = requests.post("{}/conversation".format(database_url),
                        data=json.dumps(data), headers=headers).json()
    conv_id = res['conv_id']
    return conv_id


def send_message_to_user1(user_id,response,inp,prompt_id):
    user_prompt = f'{user_id}_{prompt_id}'
    request_id = f'{request_id_list[user_prompt]}-intermediate'
    url = 'http://aws_rasa.hertzai.com:9890/autogen_response'
    body = json.dumps({'user_id':user_id,'message':response,'inp':inp,'request_id':request_id, 'Agent_status': 'Review Mode'})
    headers = {'Content-Type': 'application/json'}
    res = requests.post(url,data=body,headers=headers)


def execute_python_file(task_description:str,user_id: int,prompt_id:int,action_entry_point:int=0):
    headers = {'Content-Type': 'application/json'}
    url = 'http://localhost:6777/time_agent'
    data = json.dumps({'task_description':task_description,'user_id':user_id,'prompt_id':prompt_id,'action_entry_point':action_entry_point,'request_from':'Reuse'})
    res = requests.post(url,data=data,headers=headers)
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
    current_action = time_actions[user_prompt].get_action_byaction_id(action_entry_point)['action']
    text = f'This is the time now {current_time}\n your overall task description which might span multiple actions: {task_description}\n the current Action to execute: {current_action}'
    result = time_user.initiate_chat(time_manager, message=text,speaker_selection={"speaker": "assistant"}, clear_history=False)
    restart = False
    while True:
        current_app.logger.info('inside Timer while')
        if group_chat.messages[-1]['name'] == 'ChatInstructor' and group_chat.messages[-1]['content'] == 'TERMINATE':
            current_app.logger.info(f"group_chat.messages[-2]['content'] {group_chat.messages[-2]['content'][:10]}..")
            json_obj = retrieve_json(group_chat.messages[-2]["content"])
            if json_obj and type(json_obj)==dict and 'status' in json_obj.keys() and json_obj['status'].lower() == 'completed':
                current_action = time_actions[user_prompt].get_action_byaction_id(time_actions[user_prompt].current_action)['action']
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
        current_app.logger.info(f'checking can_perform_without_user_input from {time_actions[user_prompt].get_action_byaction_id(action_entry_point)} ')
        if time_actions[user_prompt].get_action_byaction_id(action_entry_point)['can_perform_without_user_input'] == 'yes':
            restart = True
            text = 'You can assume things on your own to complete this task'
            result = chat_instructor.initiate_chat(time_manager, message=text,speaker_selection={"speaker": "assistant"}, clear_history=False)

            continue
        break

    last_message = group_chat.messages[-1]
    if last_message['content'] == 'TERMINATE':
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
    '''
        This function helps to extract actions that the user has performed till now.
    '''
    unwanted_actions = ['Topic Cofirmation', 'Langchain', 'Assessment Ended', 'Casual Conversation',
                        'Topic confirmation',
                        'Topic not found', 'Topic Confirmation', 'Topic Listing', 'Probe', 'Question Answering',
                        'Fallback']
    action_url = f"{ACTION_API}?user_id={user_id}"
    time_zone = "Asia/Kolkata"
    try:
        india_tz = pytz.timezone(time_zone)
    except:
        india_tz = None
    payload = {}
    headers = {}
    try:
        response = requests.request("GET", action_url, headers=headers, data=payload)
        if response.status_code == 200:
            data = response.json()
            filtered_data = [obj for obj in data if obj["action"] not in unwanted_actions and obj["zeroshot_label"] not in ['Video Reasoning']]
            action_texts = []
            for obj in filtered_data:
                action = obj["action"]
                try:
                    date = parse_date(obj["created_date"])
                    if india_tz:
                        first_action_text = f"{action} on {date.astimezone(india_tz).strftime('%Y-%m-%dT%H:%M:%S')}"
                    else:
                        first_action_text = f"{action} on {date.strftime('%Y-%m-%dT%H:%M:%S')}"
                    action_texts.append(first_action_text)
                except:
                    action_texts.append(f"{action}")
                if len(action_texts) == 0:
                    action_texts = ['user has not performed any actions yet.']
                actions = ", ".join(action_texts)
        else:
            actions = "Could not retrieve user actions"
    except Exception as e:
        current_app.logger.error(f"Error getting action details: {e}")
        actions = "No user action data available"
    try:
        url = STUDENT_API
        payload = json.dumps({"user_id": user_id})
        headers = {'Content-Type': 'application/json'}
        response = requests.request("POST", url, headers=headers, data=payload)
        if response.status_code == 200:
            user_data = response.json()
            user_details = f'''Below are the information about the user.
            user_name: {user_data.get("name", "Unknown")}, gender: {user_data.get("gender", "Unknown")}, 
            preferred_language: {user_data.get("preferred_language", "Unknown")}, 
            date_of_birth: {user_data.get("dob", "Unknown")}'''
        else:
            user_details = "Could not retrieve user details"
    except Exception as e:
        current_app.logger.error(f"Error getting user details: {e}")
        user_details = "No user details available"
    return user_details, actions

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
        if last_message['content'] == 'TERMINATE':
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
    url = 'http://localhost:6777/visual_agent'

    now = datetime.now()
    action_url = f"{ACTION_API}?user_id={user_id}"
    payload = {}
    headers_api = {}

    response = requests.request("GET", action_url, headers=headers_api, data=payload)

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
                res = requests.post(url, data=data_to_send, headers=headers, timeout=10)
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


async def subscribe_and_return(message, topic, time=1800000):
    """
    Makes an RPC call to the specified topic using a component.
    Waits for the full duration of the specified timeout for a response.

    Args:
        message: The message payload to send
        topic: The topic to call
        time: Timeout in milliseconds (default: 8000)

    Returns:
        The response from the RPC call, or None if there was an error or timeout
    """
    current_app.logger.info(f"Making RPC Call to {topic}...")

    # Create a new component for this call
    component = Component(
        transports="ws://aws_rasa.hertzai.com:8088/ws",
        realm="realm1",
    )

    response_future = asyncio.Future()

    @component.on_join
    async def join(session, details):
        current_app.logger.info("Session joined, making RPC call...")
        try:
            # Convert time from milliseconds to seconds
            timeout_seconds = time / 1000
            current_app.logger.info(f"Using timeout of {timeout_seconds} seconds")

            # Set actual timeout
            try:
                result = await asyncio.wait_for(
                    session.call(topic, message),
                    timeout=timeout_seconds
                )

                if not response_future.done():
                    response_future.set_result(result)

            except asyncio.TimeoutError:
                if not response_future.done():
                    response_future.set_exception(
                        Exception(f"RPC call timed out after {timeout_seconds} seconds")
                    )
            except Exception as e:
                if not response_future.done():
                    response_future.set_exception(e)

        finally:
            # Stop the component regardless of success / failure
            try:
                await component.stop()
            except Exception as e:
                current_app.logger.error(f"Error stopping component: {e}")

    # Calculate timeout with a small buffer
    actual_timeout = (time / 1000) + 5  # Add 5 second buffer
    try:
        # Start the component
        await component.start()

        # Wait for the response or timeout
        result = await asyncio.wait_for(response_future, timeout=actual_timeout)

        # Return the result
        return result

    except asyncio.TimeoutError:
        current_app.logger.error(f"Timed out waiting for response after {actual_timeout} seconds")
        # Explicitlt cancel the future if it's still pending
        if not response_future.done():
            response_future.cancel()
        return None
    except Exception as e:
        current_app.logger.error(f"Error in subscribe_and_return: {e}")
        # Explicitly cancel the future if it's still pending
        if not response_future.done():
            response_future.cancel()
        return None
    finally:
        # Ensure component is stopped
        if hasattr(component, 'session') and component.session:
            try:
                await component.stop()
            except Exception as e:
                current_app.logger.error(f"Error stopping component in finally: {e}")


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


def create_agents(user_id: str,task,prompt_id) -> Tuple[Any, Any, Any, Any, Any, Any, Any]:
    """Create new assistant & user agents for a given user_id"""
    user_prompt = f'{user_id}_{prompt_id}'
    individual_json[user_prompt] = None

    try:
        tool_logger.info("[INIT] Trying to initialise...")

        apply_autogen_fix_on_startup()
    except:
        tool_logger.info("[INFO] Autogen JSON enhancement ready - will be applied when Flask starts")

    # Initialize SimpleMem for this session (from gpt4.1)
    simplemem_store = None
    if HAS_SIMPLEMEM:
        try:
            sm_config = SimpleMemConfig.from_env()
            if sm_config.enabled and sm_config.api_key:
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
        graph_db_path = os.path.join(
            os.path.expanduser("~"), "Documents", "Nunba", "data", "memory_graph", user_prompt
        )
        memory_graph = MemoryGraph(db_path=graph_db_path, user_id=str(user_id))
        tool_logger.info(f"MemoryGraph initialized for {user_prompt}")
    except Exception as e:
        tool_logger.warning(f"MemoryGraph init failed: {e}")

    custom_agents = []
    agents_object = {}
    with open(f"prompts/{prompt_id}.json", 'r') as f:
            config = json.load(f)
            list_of_persona = config['flows'][get_current_flow(user_prompt)]['persona']
            current_app.logger.info(f'WORKING persona as {list_of_persona}')
    # Create assistant agent
    # Create assistant agent
    assistant = instantiate_assistant_agent(list_of_persona, user_prompt)

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
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )

    author = autogen.UserProxyAgent(
        name="UserProxy",
        human_input_mode="NEVER",
        is_termination_msg=lambda x: True if x.get("content").strip()=='' else False,
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )

    context_handling = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=50,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(user_tasks=user_tasks, user_prompt=user_prompt),
        ]
    )

    context_handling.add_to_agent(assistant)
    context_handling.add_to_agent(helper)
    context_handling.add_to_agent(executor)
    context_handling.add_to_agent(verify)

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
    #         is_termination_msg=lambda x: True if x.get("content").strip()=='' else False,
    #         max_consecutive_auto_reply=0,
    #         code_execution_config=False,
    #     )
    #     name.description = i['description']
    #     custom_agents.append(name)
    #     agents_object[i['name']] = name

    helper.register_for_llm(name="text_2_image", description="Text to image Creator")(helper_fun.txt2img)
    assistant.register_for_execution(name="text_2_image")(helper_fun.txt2img)

    @log_tool_execution
    def camera_inp(inp: Annotated[str, "The Question to check from visual context"])->str:
        return helper_fun.get_user_camera_inp(inp,int(user_id), request_id_list[user_prompt])
    helper.register_for_llm(name="get_user_camera_inp", description="Get user's visual information to process somethings")(camera_inp)
    assistant.register_for_execution(name="get_user_camera_inp")(camera_inp)

    @log_tool_execution
    def save_data_in_memory(key: Annotated[
        str, "Key path for storing data now & retrieving data later. Use dot notation for nested keys (e.g., 'user.info.name')."],
                            value: Annotated[Optional[
                                Any], "Value you want to store; strictly should be one of int, float, bool, json array or json object."] = None) -> str:
        """Store data with validation to prevent corruption."""
        tool_logger.info('INSIDE save_data_in_memory')

        # Validate the input data
        try:
            # Step 1: Use the existing JSON repair function to sanitize input
            if isinstance(value, str) and (value.startswith('{') or value.startswith('[')):
                # If the value is a JSON string, repair it
                value = retrieve_json(value)
                tool_logger.info(f"REPAIRED JSON STRING: {value}")
            # Step 2: Force a JSON serialization/deserialization cycle to validate structure
            if value is not None:
                # This will fail if the structure isn't JSON-compatible
                json_str = json.dumps(value)
                validated_value = json.loads(json_str)
                tool_logger.info(f"VALIDATED VALUE (post JSON cycle): {validated_value}")
            else:
                validated_value = None
            # Step 3: Store the validated data
            keys = key.split('.')
            d = agent_data.setdefault(prompt_id, {})

            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = validated_value
            tool_logger.info(f"VALUES STORED IN AGENT DATA: {validated_value}")
            tool_logger.info(f"FULL AGENT DATA AT KEY: {d}")

            # Step 4: Save to persistent storage
            if helper_fun.save_agent_data_to_file(prompt_id, agent_data):
                tool_logger.info(f"[OK] Data persisted to file for prompt_id {prompt_id}")
            else:
                tool_logger.warning(f"⚠️ Failed to persist data to file for prompt_id {prompt_id}")
            # Step 5: Verify storage was successful
            try:
                # Attempt to read back the data to verify it was stored correctly
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

    helper.register_for_llm(name="save_data_in_memory", description="Use this to Store and retrieve data using key-value storage system")(save_data_in_memory)
    assistant.register_for_execution(name="save_data_in_memory")(save_data_in_memory)

    def get_saved_metadata() -> str:
        """Get metadata with automatic loading from persistent storage"""
        if prompt_id not in agent_data or not agent_data[prompt_id]:
            current_app.logger.info(f"Loading agent data from file for get_saved_metadata, prompt_id {prompt_id}")
            helper_fun.load_agent_data_from_file(prompt_id,agent_data)

        stripped_json = strip_json_values(agent_data[prompt_id])
        return f'{stripped_json}'

    helper.register_for_llm(name="get_saved_metadata", description="Returns the schema of the json from internal memory with all keys but without actual values.")(get_saved_metadata)
    assistant.register_for_execution(name="get_saved_metadata")(get_saved_metadata)

    @log_tool_execution
    def get_data_by_key(key: Annotated[
        str, "Key path for retrieving data. Use dot notation for nested keys (e.g., 'user.info.name')."]) -> str:
        # Ensure data is loaded for this prompt_id
        if prompt_id not in agent_data or not agent_data[prompt_id]:
            tool_logger.info(f"Loading agent data from file for prompt_id {prompt_id}")
            helper_fun.load_agent_data_from_file(prompt_id,agent_data)
        keys = key.split('.')
        d = agent_data.get(prompt_id, {})

        try:
            for k in keys:
                d = d[k]
            return f'{d}'
        except KeyError:
            return "Key not found in stored data."

    helper.register_for_llm(name="get_data_by_key", description="Returns all data from the internal Memory using key")(get_data_by_key)
    assistant.register_for_execution(name="get_data_by_key")(get_data_by_key)

    @log_tool_execution
    def get_user_id() -> str:
        tool_logger.info('INSIDE get_user_id')
        return f'{user_id}'

    helper.register_for_llm(name="get_user_id", description="Returns the unique identifier (user_id) of the current user.")(get_user_id)
    assistant.register_for_execution(name="get_user_id")(get_user_id)

    @log_tool_execution
    def get_prompt_id() -> str:
        tool_logger.info('INSIDE get_prompt_id')
        return f'{prompt_id}'

    helper.register_for_llm(name="get_prompt_id", description="Returns the unique identifier (prompt_id) associated with the current prompt or conversation.")(get_prompt_id)
    assistant.register_for_execution(name="get_prompt_id")(get_prompt_id)

    @log_tool_execution
    def Generate_video(text: Annotated[str, "Text to be used for video generation"],
                       avatar_id: Annotated[int, "Unique identifier for the avatar (use 0 for LTX-2 text-to-video)"],
                       realtime: Annotated[bool,"If True, response is fast but less realistic by default it should be true; if False, response is realistic but slower"],
                       model: Annotated[str, "Video model to use: 'avatar' for avatar-based video, 'ltx2' for LTX-2 text-to-video generation"] = "avatar") -> str:
        tool_logger.info(f'INSIDE Generate_video with model={model}')

        # LTX-2 Text-to-Video Generation (using diffusers or local server)
        if model.lower() == "ltx2":
            tool_logger.info(f'Using LTX-2 for video generation: {text[:50]}...')

            LOCAL_COMFYUI_URL = "http://localhost:8188"
            LOCAL_LTX_URL = "http://localhost:5002"
            headers = {'Content-Type': 'application/json'}

            # LTX-2 parameters (width/height must be divisible by 32, num_frames by 8+1)
            ltx_payload = {
                "prompt": text,
                "negative_prompt": "worst quality, inconsistent motion, blurry, jittery, distorted",
                "num_frames": 97,  # 97 = 96 + 1 (divisible by 8 + 1), ~4 seconds at 24fps
                "width": 832,  # divisible by 32
                "height": 480,  # divisible by 32
                "num_inference_steps": 30 if realtime else 50,
                "guidance_scale": 3.0,
                "fps": 24
            }

            # Try local LTX-2 server first (custom endpoint)
            try:
                tool_logger.info(f"Trying local LTX-2 server at {LOCAL_LTX_URL}")
                response = requests.post(
                    f"{LOCAL_LTX_URL}/generate",
                    json=ltx_payload,
                    headers=headers,
                    timeout=600  # 10 min timeout for video gen
                )
                if response.status_code == 200:
                    result = response.json()
                    video_url = result.get('video_url') or result.get('output_url') or result.get('video_path')
                    if video_url:
                        tool_logger.info(f"LTX-2 video generated: {video_url}")
                        return f"LTX-2 Video generated successfully. URL: {video_url}"
            except requests.exceptions.RequestException as e:
                tool_logger.info(f"Local LTX-2 server not available: {e}")

            # Try ComfyUI with LTX-Video workflow
            try:
                tool_logger.info(f"Trying ComfyUI at {LOCAL_COMFYUI_URL}")

                # ComfyUI workflow for LTX-Video (compatible with LTX-2 nodes)
                comfyui_workflow = {
                    "prompt": {
                        "1": {
                            "class_type": "LTXVLoader",
                            "inputs": {"ckpt_name": "ltx-video-2b-v0.9.safetensors"}
                        },
                        "2": {
                            "class_type": "LTXVConditioning",
                            "inputs": {
                                "positive": text,
                                "negative": ltx_payload["negative_prompt"],
                                "ltxv_model": ["1", 0]
                            }
                        },
                        "3": {
                            "class_type": "LTXVSampler",
                            "inputs": {
                                "seed": int(time.time()) % 2147483647,
                                "steps": ltx_payload["num_inference_steps"],
                                "cfg": ltx_payload["guidance_scale"],
                                "width": ltx_payload["width"],
                                "height": ltx_payload["height"],
                                "num_frames": ltx_payload["num_frames"],
                                "ltxv_model": ["1", 0],
                                "conditioning": ["2", 0]
                            }
                        },
                        "4": {
                            "class_type": "LTXVDecode",
                            "inputs": {"ltxv_model": ["1", 0], "samples": ["3", 0]}
                        },
                        "5": {
                            "class_type": "VHS_VideoCombine",
                            "inputs": {
                                "frame_rate": ltx_payload["fps"],
                                "filename_prefix": "ltx2_output",
                                "format": "video/h264-mp4",
                                "images": ["4", 0]
                            }
                        }
                    }
                }

                response = requests.post(f"{LOCAL_COMFYUI_URL}/prompt", json=comfyui_workflow, headers=headers, timeout=10)

                if response.status_code == 200:
                    comfy_prompt_id = response.json().get('prompt_id')
                    tool_logger.info(f"ComfyUI LTX-2 job queued: {comfy_prompt_id}")

                    # Poll for completion (up to 10 minutes for video generation)
                    for _ in range(120):
                        time.sleep(5)
                        history_response = requests.get(f"{LOCAL_COMFYUI_URL}/history/{comfy_prompt_id}")
                        if history_response.status_code == 200:
                            history = history_response.json()
                            if comfy_prompt_id in history:
                                outputs = history[comfy_prompt_id].get('outputs', {})
                                for node_id, output in outputs.items():
                                    if 'gifs' in output:
                                        filename = output['gifs'][0].get('filename')
                                        if filename:
                                            video_url = f"{LOCAL_COMFYUI_URL}/view?filename={filename}"
                                            return f"LTX-2 Video generated via ComfyUI. URL: {video_url}"
                                    if 'videos' in output:
                                        filename = output['videos'][0].get('filename')
                                        if filename:
                                            video_url = f"{LOCAL_COMFYUI_URL}/view?filename={filename}"
                                            return f"LTX-2 Video generated via ComfyUI. URL: {video_url}"

                    return f"LTX-2 Video generation queued in ComfyUI (prompt_id: {comfy_prompt_id}). Check ComfyUI interface for output."

            except requests.exceptions.RequestException as e:
                tool_logger.info(f"ComfyUI not available: {e}")

            # Try using diffusers library directly (if installed)
            try:
                tool_logger.info("Trying diffusers library for LTX-2")
                import torch
                from diffusers import LTXPipeline
                from diffusers.utils import export_to_video

                pipe = LTXPipeline.from_pretrained(
                    "Lightricks/LTX-Video-0.9.7-distilled",
                    torch_dtype=torch.bfloat16
                )
                pipe.to("cuda")
                pipe.vae.enable_tiling()

                video_frames = pipe(
                    prompt=text,
                    negative_prompt=ltx_payload["negative_prompt"],
                    width=ltx_payload["width"],
                    height=ltx_payload["height"],
                    num_frames=ltx_payload["num_frames"],
                    num_inference_steps=ltx_payload["num_inference_steps"],
                    generator=torch.Generator(device="cuda").manual_seed(int(time.time()) % 2147483647),
                ).frames[0]

                output_path = os.path.join(os.getcwd(), "coding", f"ltx2_{int(time.time())}.mp4")
                export_to_video(video_frames, output_path, fps=ltx_payload["fps"])
                tool_logger.info(f"LTX-2 video saved to {output_path}")
                return f"LTX-2 Video generated and saved to: {output_path}"

            except ImportError:
                tool_logger.info("diffusers library not available or LTX model not installed")
            except Exception as e:
                tool_logger.error(f"diffusers LTX-2 generation failed: {e}")

            return "LTX-2 video generation failed. Please ensure one of: (1) Local LTX-2 server at localhost:5002, (2) ComfyUI with LTX-Video nodes at localhost:8188, or (3) diffusers library with CUDA GPU"

        # Default: Avatar-based video generation
        database_url = 'https://mailer.hertzai.com'
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        tool_logger.info(f"avtar_id: {avatar_id}:\n{text[:10]}....\n")

        headers = {'Content-Type': 'application/json'}
        data = {}
        data["text"] = text
        data['flag_hallo'] = 'false'
        data['chattts'] = False
        data['openvoice'] = "false"
        try:
            res = requests.get("{}/get_image_by_id/{}".format(database_url, avatar_id))
            res = res.json()
            new_image_url = res["image_url"]
        except:
            data['openvoice'] = "true"
            new_image_url = None
            res = {'voice_id':None}

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
            data['chattts'] = True #F5TTS
            data['flag_hallo'] = "true" #Echomimic-> Liveportrait
            data["cartoon_image"] = "False"

        if res['voice_id'] != None:
            voice_sample = requests.get(
                "{}/get_voice_sample_id/{}".format(database_url, res['voice_id']))
            voice_sample = voice_sample.json()
            data["audio_sample_url"] = voice_sample["voice_sample_url"]
            data['voice_id'] = res['voice_id']
        else:
            voice_sample = None
            data["audio_sample_url"] = None
            data['voice_id'] = None
        conv_id = save_conversation_db(text,user_id,prompt_id,database_url,request_id)
        data['conv_id'] = conv_id
        data['avatar_id'] = avatar_id
        data['timeout'] = timeout
        try:
            video_link = requests.post("{}/video_generate_save".format(database_url),
                                        data=json.dumps(data), headers=headers, timeout=1)
        except:
            pass

        if data['chattts'] or data['flag_hallo'] == "true":
            return f"Video Generation task added to queue with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"
        else:
            return f"Video Generation completed with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"

    helper.register_for_llm(name="Generate_video", description="Generate video with text. Use model='ltx2' for AI text-to-video generation, or model='avatar' (default) for avatar-based video with voice synthesis.")(Generate_video)
    assistant.register_for_execution(name="Generate_video")(Generate_video)

    # Unified media generation tools (image, audio, video — one tool for all)
    try:
        from integrations.service_tools.media_agent import register_media_tools
        register_media_tools(helper, assistant)
    except Exception as e:
        tool_logger.debug(f"Media tools registration skipped: {e}")

    @log_tool_execution
    def get_user_uploaded_file() -> str:
        tool_logger.info('INSIDE get_user_uploaded_file')
        if recent_file_id[user_id]:
            return f'Got user uploaded file the file_id is {recent_file_id[user_id]}'

        return 'No file uploaded from user'

    helper.register_for_llm(name="get_user_uploaded_file", description="get user's recent uploaded files")(get_user_uploaded_file)
    assistant.register_for_execution(name="get_user_uploaded_file")(get_user_uploaded_file)

    @log_tool_execution
    def img2txt(image_url: Annotated[str, "image url of which you want text"],text: Annotated[str, "the details you want from image"]='Describe the Images & Text data in this image in detail') -> str:
        tool_logger.info('INSIDE img2txt')
        url = "http://azurekong.hertzai.com:8000/llava/image_inference"

        payload = {
            'url': image_url,
            'prompt': text
        }
        files = []
        headers = {}

        response = requests.request(
            "POST", url, headers=headers, data=payload, files=files, timeout=300)
        if response.status_code == 200:
            return response.text
        else:
            return 'Not able to get this page details try later'

    helper.register_for_llm(name="get_text_from_image", description="Image to Text")(img2txt)
    assistant.register_for_execution(name="get_text_from_image")(img2txt)

    @log_tool_execution
    def create_scheduled_jobs(interval_sec: Annotated[int, "time between two Interval in seconds."],
                            job_description: Annotated[str, "Description of the job to be performed"],
                            cron_expression: Annotated[Optional[str], "Cron expression for scheduling. Example: '0 9 * * 1-5' (Runs at 9:00 AM, Monday to Friday). If the interval is greater than 60 seconds or it needs to be executed at a dynamic cron time this argument is Mandatory else None"]=None) -> str:
        tool_logger.info('INSIDE create_scheduled_jobs')

        # actual_execution_time = sum(task_time[prompt_id]['times'][-1])
        # if interval_sec < actual_execution_time:
        #     return f"Unable to create scheduled job for the specified interval because the actual execution time ({actual_execution_time} seconds) exceeds the interval between jobs ({interval_sec} seconds). Please use an interval longer than {actual_execution_time} seconds. Would you like to create a scheduled job with this updated interval?"

        # if not scheduler.running:
        #     scheduler.start()

        # try:
        #     if not interval_sec or int(interval_sec) >60:
        #         trigger = CronTrigger.from_crontab(cron_expression)
        #         job_id = f"job_{int(time.time())}"
        #         scheduler.add_job(execute_python_file, trigger=trigger, id=job_id, args=[job_description, int(user_id),int(prompt_id)])
        #         tool_logger.info('Successfully created scheduler job')
        #         return 'Successfully created scheduler job'
        #     else:
        #         trigger = IntervalTrigger(seconds=int(interval_sec))
        #         job_id = f"job_{int(time.time())}"
        #         scheduler.add_job(execute_python_file, trigger=trigger, id=job_id, args=[job_description, int(user_id),int(prompt_id)])
        #         tool_logger.info('Successfully created scheduler job')
        #         return 'Successfully created scheduler job'
        # except Exception as e:
        #     tool_logger.error(f'Error in create_scheduled_jobs: {str(e)}')
        #     return f"Error creating scheduled job: {str(e)}"
        return 'Added this schedule job in creation process will do it at the end. you can go ahead and mark this action as completed.'

    helper.register_for_llm(name="create_scheduled_jobs", description="Creates time-based jobs using APScheduler to schedule jobs")(create_scheduled_jobs)
    assistant.register_for_execution(name="create_scheduled_jobs")(create_scheduled_jobs)

    @log_tool_execution
    def send_message_to_user(text: Annotated[str, "Text you want to send to the user"],
                         avatar_id: Annotated[Optional[str], "Unique identifier for the avatar"] = None,
                         response_type: Annotated[Optional[str], "Response mode: 'Realistic' (slower, better quality) or 'Realtime' (faster, lower quality)"] = 'Realtime') -> str:
        tool_logger.info('INSIDE send_message_to_user')
        tool_logger.info(f'SENDING DATA 2 user with values text:{text}, avatar_id:{avatar_id}, response_type:{response_type}')
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        #TODO add avatar_id and conv_id and response_type
        thread = threading.Thread(target=send_message_to_user1, args=(user_id, text, '',prompt_id))
        thread.start()
        return f'Message sent successfully to user with request_id: {request_id_list[user_prompt]}-intermediate'

    helper.register_for_llm(name="send_message_to_user", description="Sends a message/information to user. You can use this if you want to ask a question")(send_message_to_user)
    assistant.register_for_execution(name="send_message_to_user")(send_message_to_user)

    @log_tool_execution
    def send_presynthesized_video_to_user(conv_id: Annotated[str, "Conversation ID associated with the text from memory"]) -> str:
        tool_logger.info('INSIDE send_presynthesized_video_to_user')
        tool_logger.info(f'SENDING DATA 2 user with value: conv_id:{conv_id}.')
        return 'Message sent successfully to user'

    helper.register_for_llm(name="send_presynthesized_video_to_user", description="Sends a presynthesized message/video/dialogue to user using conv_id.")(send_presynthesized_video_to_user)
    assistant.register_for_execution(name="send_presynthesized_video_to_user")(send_presynthesized_video_to_user)

    @log_tool_execution
    def send_message_in_seconds(text: Annotated[str, "text to send to user"],
                       delay: Annotated[int, "time to wait in seconds before sending text"],
                       conv_id: Annotated[Optional[int], "conv_id for this text if not available make it None"],) -> str:
        tool_logger.info('INSIDE send_message_in_seconds')
        tool_logger.info(f'with text:{text}. and waiting time: {delay} conv_id: {conv_id}')
        run_time = datetime.fromtimestamp(time.time() + delay)
        scheduler.add_job(send_message_to_user1, 'date', run_date=run_time, args=[user_id, text, '',prompt_id])
        return 'Message scheduled successfully'

    helper.register_for_llm(name="send_message_in_seconds", description="Sends a presynthesized message/video/dialogue to user using conv_id with a timer.")(send_message_in_seconds)
    assistant.register_for_execution(name="send_message_in_seconds")(send_message_in_seconds)

    @log_tool_execution
    def get_chat_history(text: Annotated[str, "Text related to which you want history"],
                         start: Annotated[Optional[str], "start date in format %Y-%m-%dT%H:%M:%S.%fZ"] = None,
                         end: Annotated[Optional[str], "end date in format %Y-%m-%dT%H:%M:%S.%fZ"] = None) -> str:
        tool_logger.info('INSIDE get_chat_history')
        return helper_fun.get_time_based_history(text, f'user_{user_id}', start, end)
    helper.register_for_llm(name="get_chat_history", description="Get Chat history based on text & start & end date")(get_chat_history)
    assistant.register_for_execution(name="get_chat_history")(get_chat_history)

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
    helper.register_for_llm(name="search_visual_history", description="Search past camera and screen descriptions by keyword and time range. Use for visual history queries.")(search_visual_history)
    assistant.register_for_execution(name="search_visual_history")(search_visual_history)

    # --- SimpleMem long-term memory tools ---
    if simplemem_store is not None:
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
                tool_logger.info(f"SimpleMem search error: {e}")
                return "Memory search unavailable."

        helper.register_for_llm(
            name="search_long_term_memory",
            description="Search long-term memory for past conversations, facts, and context using natural language query. More powerful than get_chat_history for finding relevant information."
        )(search_long_term_memory)
        assistant.register_for_execution(name="search_long_term_memory")(search_long_term_memory)

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
                return "Saved to long-term memory."
            except Exception as e:
                tool_logger.info(f"SimpleMem save error: {e}")
                return "Failed to save to long-term memory."

        helper.register_for_llm(
            name="save_to_long_term_memory",
            description="Save important facts or information to long-term memory for future retrieval across sessions."
        )(save_to_long_term_memory)
        assistant.register_for_execution(name="save_to_long_term_memory")(save_to_long_term_memory)

    # --- MemoryGraph provenance tools (remember, recall, backtrace) ---
    if memory_graph is not None:
        try:
            from integrations.channels.memory.agent_memory_tools import create_memory_tools, register_autogen_tools
            mem_tools = create_memory_tools(memory_graph, str(user_id), user_prompt)
            register_autogen_tools(mem_tools, assistant, helper)
            tool_logger.info(f"MemoryGraph tools registered for {user_prompt}")
        except Exception as e:
            tool_logger.warning(f"MemoryGraph tools registration failed: {e}")

    @log_tool_execution
    def google_search(text: Annotated[str, "Text/Query which you want to search"]) -> str:
        tool_logger.info('INSIDE google search')
        return helper_fun.top5_results(text)
    helper.register_for_llm(name="google_search", description="web/google/bing search api tool for a given query")(google_search)
    assistant.register_for_execution(name="google_search")(google_search)

    @log_tool_execution
    def get_user_details()->str:
        tool_logger.info('INSIDE get user details')
        return helper_fun.parse_user_id(int(user_id))
    helper.register_for_llm(name="get_user_details", description="Get User details like name, dob, gender")(get_user_details)
    assistant.register_for_execution(name="get_user_details")(get_user_details)

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
    helper.register_for_llm(name="validate_json_response", description="Checks and corrects if the tool response is not JSON but expected to be.")(validate_json_response)
    assistant.register_for_execution(name="validate_json_response")(validate_json_response)

    @log_tool_execution
    async def execute_windows_or_android_command(
            instructions: Annotated[
                str, "Command in plain English to execute in the user's windows computer or android machine"],
            os_to_control: Annotated[
                str, "The os to control, possible values are 'windows' or 'android' only "]) -> str:
        """
        Executes a command on a Windows machine or Android device and returns the response with enhanced VLM agent context.
        """



        try:
            tool_logger.info('INSIDE execute_windows_or_android_command')
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

            direct_vlm_path = f"prompts/{prompt_id}_{role_number}_{current_action_id}_vlm_agent.json"
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
                response = await subscribe_and_return({'prompt_id': prompt_id}, topic, 2000)
                tool_logger.info(f'Response from call of {topic}: {response}')

                if not response:
                    return 'Ask UserProxy to go to hevolve.ai login and start Nunba - Your Local Hyve Companion App'

                topic = 'com.hertzai.hevolve.action'
                tool_logger.info(f'calling {topic} for 1800 seconds')
                response = await subscribe_and_return(crossbar_message, topic, 1800000)

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
                        base_path = f"prompts/{prompt_id}_{role_number}"

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
                            if text.strip().startswith("{") and "action" in text:
                                try:
                                    action_data = ast.literal_eval(text.strip())
                                    action_type = action_data.get("action", "")

                                    if action_type == "mouse_move":
                                        return "Move mouse"
                                    elif action_type == "left_click":
                                        return "Perform left click"
                                    elif action_type == "right_click":
                                        return "Perform right click"
                                    elif action_type == "double_click":
                                        return "Perform double click"
                                    elif action_type == "type" and "text" in action_data:
                                        return f"Type '{action_data['text']}'"
                                    elif action_type == "drag":
                                        return "Perform drag action"
                                    else:
                                        return f"Perform {action_type} action"
                                except:
                                    action_match = re.search(r"'action':\s*'([^']+)'", text)
                                    text_match = re.search(r"'text':\s*'([^']+)'", text)

                                    if action_match:
                                        action_type = action_match.group(1)
                                        if action_type == "type" and text_match:
                                            return f"Type '{text_match.group(1)}'"
                                        elif action_type == "mouse_move":
                                            return "Move mouse"
                                        elif action_type == "left_click":
                                            return "Perform left click"
                                        elif action_type == "right_click":
                                            return "Perform right click"
                                        elif action_type == "double_click":
                                            return "Perform double click"
                                        else:
                                            return f"Perform {action_type} action"
                            return text

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

                        # Save the recipe
                        with open(vlm_agent_path, 'w') as json_file:
                            json.dump(recipe_data, json_file, indent=4)

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

    Nunba - Your Local Hyve Companion App is not running on your {os_to_control} device.

    STEPS TO RESOLVE:
    1. Open Nunba - Your Local Hyve Companion App
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
    helper.register_for_llm(name="execute_windows_or_android_command",
                            description="Processes user-defined commands on a personal Windows or Android system and returns detailed computer/mobile use agent execution context.")(execute_windows_or_android_command)
    assistant.register_for_execution(name="execute_windows_or_android_command")(execute_windows_or_android_command)

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

                    # Register for LLM (helper agent suggests tool use)
                    helper.register_for_llm(name=tool_name, description=description)(tool_func)

                    # Register for execution (assistant agent executes tool)
                    assistant.register_for_execution(name=tool_name)(tool_func)

                    tool_logger.info(f"Registered MCP tool: {tool_name}")
        else:
            tool_logger.info("No MCP servers configured - continuing with default tools")
    except Exception as e:
        tool_logger.warning(f"MCP integration error (non-critical): {e}")
        # Continue with default tools if MCP fails

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

        helper.register_for_llm(name="delegate_to_specialist",
                               description="Delegate complex tasks to specialist agents based on required skills")(delegate_to_specialist)
        assistant.register_for_execution(name="delegate_to_specialist")(delegate_to_specialist)

        # Add context sharing tool
        @log_tool_execution
        def share_context_with_agents(context_key: Annotated[str, "Unique identifier for the context"],
                                      context_value: Annotated[Any, "Context data to share"]) -> str:
            """Share context information with other agents"""
            sharing_func = create_context_sharing_function('assistant')
            return sharing_func(context_key, context_value)

        helper.register_for_llm(name="share_context_with_agents",
                               description="Share context information with other agents in the system")(share_context_with_agents)
        assistant.register_for_execution(name="share_context_with_agents")(share_context_with_agents)

        # Add context retrieval tool
        @log_tool_execution
        def get_shared_context(context_key: Annotated[str, "Identifier of the context to retrieve"]) -> str:
            """Retrieve context information shared by other agents"""
            retrieval_func = create_context_retrieval_function()
            return retrieval_func(context_key)

        helper.register_for_llm(name="get_shared_context",
                               description="Retrieve context information shared by other agents")(get_shared_context)
        assistant.register_for_execution(name="get_shared_context")(get_shared_context)

        tool_logger.info("Internal Agent Communication complete - agents can now delegate tasks and share context")

    except Exception as e:
        tool_logger.warning(f"Internal Agent Communication error (non-critical): {e}")
        # Continue without internal communication if it fails

    # AP2 (Agent Protocol 2): Agentic Commerce - Payment workflows
    try:
        tool_logger.info("Initializing AP2 (Agent Protocol 2) - Agentic Commerce...")

        # Get AP2 payment tools for this agent
        ap2_tools = get_ap2_tools_for_autogen('assistant')

        # Register payment tools
        for tool_def in ap2_tools:
            tool_func = tool_def['function']
            tool_name = tool_def['name']
            tool_desc = tool_def['description']

            # Register for LLM (helper agent suggests payment tools)
            helper.register_for_llm(name=tool_name, description=tool_desc)(tool_func)

            # Register for execution (assistant agent executes payment operations)
            assistant.register_for_execution(name=tool_name)(tool_func)

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
    except Exception as e:
        tool_logger.debug(f"Goal-aware tool loading skipped: {e}")

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
        user_prompt = f'{user_id}_{prompt_id}'
        current_action_id = user_tasks[user_prompt].current_action

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
                    except:
                        pass
        except Exception as e:
            current_app.logger.error(f"Error in error detection logic: {e}")

        # Get metadata once for potential use later
        try:
            metadata = get_saved_metadata()
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
                            json_action_id = int(json_obj.get('action_id', user_tasks[user_prompt].current_action))


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
                                    # Sync the blocked state to ledger
                                    sync_action_state_to_ledger(user_prompt, current_action_id, ActionState.PENDING, user_ledgers)
                                else:
                                    current_app.logger.warning(f"Failed to add subtasks to ledger")
                            safe_set_state(user_prompt, current_action_id, ActionState.PENDING, "requires breakdown into subtasks")
                            return assistant
                        elif json_obj['status'].lower() == 'done':
                            json_action_id = int(json_obj.get('action_id', user_tasks[user_prompt].current_action))

                            # Normal Set ActionState To Terminate After getting Recipe json for each action
                            if 'recipe' in json_obj.keys() and json_obj['status'].lower() == 'done' and json_action_id > len(user_tasks[user_prompt].actions): # Done state when recipe is created
                                create_individual_flow_recipe_and_terminate_flow(json_action_id, json_obj, user_prompt)

                            recipe_result = lifecycle_hook_track_recipe_completion(user_prompt, json_obj,
                                                                                   user_tasks)  # 10. Track recipe completion

                            current_state = get_action_state(user_prompt, user_tasks[user_prompt].current_action)

                            if current_state == ActionState.RECIPE_RECEIVED:  # State was set in Location 1
                                # Recipe received, save it
                                current_app.logger.info('Got Individual action recipe save it')
                                flow = get_current_flow(user_prompt)
                                name = f'prompts/{prompt_id}_{flow}_{json_obj["action_id"]}.json'
                                user_tasks[user_prompt].fallback = False
                                user_tasks[user_prompt].recipe = False
                                metadata = strip_json_values(agent_data[prompt_id])
                                json_obj['metadata'] = metadata
                                json_obj['time_took_to_complete'] = task_time[prompt_id]['times'][-1]
                                for i in json_obj['recipe']:
                                    if 'tool_name' in i and i['tool_name'] != "":
                                        i['agent_to_perform_this_action'] = 'Helper'
                                    elif 'generalized_functions' in i and i['generalized_functions'] != "":
                                        i['agent_to_perform_this_action'] = 'Executor'
                                    else:
                                        i['agent_to_perform_this_action'] = 'Assistant'
                                with open(name, "w") as json_file:
                                    json.dump(json_obj, json_file)
                                #setting the action from response as current action
                                user_tasks[user_prompt].current_action = int(json_obj['action_id'])
                                individual_json[user_prompt] = json_obj
                                current_app.logger.info(f'Saved Individual recipe at: {name}')
                                # Transition to TERMINATED so next action can start
                                force_state_through_valid_path(user_prompt, int(json_obj['action_id']), ActionState.TERMINATED, "Recipe saved and action complete")
                            else:
                                current_app.logger.info(f'Current state is {current_state}, Recipe Already Saved in get response group')
                                # Even if recipe already saved, ensure action is TERMINATED via proper state path
                                if current_state == ActionState.COMPLETED:
                                    # Must go through RECIPE_RECEIVED before TERMINATED
                                    safe_set_state(user_prompt, user_tasks[user_prompt].current_action, ActionState.RECIPE_RECEIVED, "Recipe already saved, set to RECIPE_RECEIVED")
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

        if last_speaker.name == 'Executor' or last_speaker.name == 'Helper' or last_speaker.name == 'UserProxy' or last_speaker.name == 'UserProxy' or last_speaker.name == 'ChatInstructor':

            group_chat.messages[-1]['content'] = f"{group_chat.messages[-1]['content']}\n Metadata/skeleton of all keys for retrieving data from memory:{metadata}"
            current_app.logger.info('Got last speaker as executor or helper or author or chat_instructor & reutrning next speaker as assistant')
            return assistant
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
        try:
            if last_speaker.name not in ['UserProxy', 'User'] and messages[-1]["content"] != '' and messages[-1]["content"] is not None and 'Message already sent successfully to user with request_id' not in messages[-1]["content"] and 'Message sent successfully to user with request_id' not in messages[-1]["content"]:
                crossbar_message = {"text": [f'{messages[-1]["content"]}'], "priority": 49,
                                    "action": 'Thinking', "historical_request_id": [], "preffered_language": 'en-US',
                                    "options": [], "newoptions": [], "bot_type": 'Agent', "page_image_url": "",
                                    "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
                        'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0},
                        'bottom_left': {'x': 0, 'y': 0}}}
                publish_async(
                    f"com.hertzai.hevolve.chat.{user_id}", json.dumps(crossbar_message))
        except Exception as e:
            current_app.logger.error(f"Error publishing crossbar message: {e}")

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
            transforms.MessageHistoryLimiter(max_messages=50,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(user_tasks=user_tasks, user_prompt=user_prompt),
        ]
    )

    # Try to use select_speaker_transform_messages if supported (added in AutoGen 0.2.36+)
    group_chat_kwargs = {
        'agents': all_agents,
        'messages': [],
        'max_round': 30,
        'select_speaker_prompt_template': f"Read the above conversation, select the next person from [Assistant, Helper, Executor, ChatInstructor, StatusVerifier & User] & only return the role as agent. Return User only if the previous message demands it",
        'speaker_selection_method': state_transition,  # using an LLM to decide
        'allow_repeat_speaker': False,  # Prevent same agent speaking twice
        'send_introductions': False
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

    group_chat = autogen.GroupChat(**group_chat_kwargs)

    manager = autogen.GroupChatManager(
        groupchat=group_chat,
        llm_config={"config_list": config_list,"cache_seed": None,"max_tokens": 1500}
    )

    # Auto-ingest group_chat messages into SimpleMem
    if simplemem_store is not None:
        _original_append = group_chat.messages.append
        def _simplemem_ingest_hook(msg):
            _original_append(msg)
            try:
                content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                speaker = msg.get("name", "Agent") if isinstance(msg, dict) else "Agent"
                if content and len(content.strip()) > 5:
                    loop = get_or_create_event_loop()
                    loop.run_until_complete(simplemem_store.add(content, {
                        "sender_name": speaker,
                        "user_id": user_id,
                        "prompt_id": prompt_id,
                    }))
            except Exception:
                pass  # Non-blocking
        group_chat.messages.append = _simplemem_ingest_hook

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

    return author, assistant, executor, group_chat, manager, chat_instructor, agents_object


def instantiate_executor_agent():
    executor = autogen.AssistantAgent(
        name="Executor",
        code_execution_config={"last_n_messages": 2, "work_dir": "coding", "use_docker": False},
        llm_config=llm_config,
        system_message="""You are an Executor agent.
        Focus: Running, and debugging code.

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
        Role: Your primary responsibility is to track, validate and verify the status of actions performed by other agents. You must provide updates strictly in JSON format with the following response structures:
        Response formats:
            1. Action Completed Successfully: {"status": "completed","action": "current action","action_id": 1/2/3...,"message": "message here","can_perform_without_user_input":"can you perform this action on your own without user input in future. only say no when it is absolutely mandatory and you cannot proceed without it, if you can proceed by checking with other agents you should say yes.  say yes/no if no they give the reason as well e.g. no-i need user's likes and dislike","persona_name":"persona name this action belongs to","fallback_action": "Automatically determine and provide intelligent fallback strategy here based on the action type. Examples: For file operations - retry with alternate path; For API calls - implement exponential backoff; For calculations - use alternative algorithm; For data processing - validate and sanitize inputs before retry. NEVER leave this empty."}
            2. Action Error: {"status": "error","action": "current action","action_id": 1/2/3...,"message": "message here"}
            3. Current Action Updated: {"status": "updated","action": "current action text","updated_action": "updated current action text","action_id": 1/2/3...,"message": "message here","persona_name":"persona name this action belongs to","fallback_action": "Provide intelligent fallback strategy based on the updated action"}
            4. Action pending: {"status": "pending","action": "current action","action_id": 1/2/3...,"message": "what steps are pending message here"}
            5. Action Requires Breakdown: {"status": "requires_breakdown","action": "current action","action_id": 1/2/3...,"reason": "Why this action needs to be broken down","subtasks": [{"subtask_id": "1.1","description": "First subtask description","depends_on": [],"can_perform_autonomously": true},{"subtask_id": "1.2","description": "Second subtask","depends_on": ["1.1"],"can_perform_autonomously": true}]}
        Important Instructions:
            1. Strict Completion Criteria:
                i. Only mark an action as "completed" if all steps of the action have been successfully executed.
                ii. For pending or ongoing tasks, instruct the Assistant to complete them.
            2. Ensure Action Accuracy:
                i. Verify that the last action was performed correctly based on history as per instructions.
                ii. If the action was not executed correctly or if assistant is incorrectly asking to mark complete, return the original action to the Assistant with pending.
            3. Maintain JSON Consistency:
                i. Always follow the exact JSON structure in your responses.
                ii. Do not perform actions yourself - only report status.
            4. CRITICAL - Error Detection Rules (MUST FOLLOW):
                i. Report "error" (not "pending") when you see these PERMANENT FAILURES in tool responses or conversation history:
                    - HTTP 403 Forbidden (API access denied - requires project/key setup)
                    - HTTP 404 Not Found (endpoint does not exist)
                    - HTTP 500 Internal Server Error (server-side failure)
                    - HTTP 401 Unauthorized (missing/invalid credentials)
                    - Connection timeout errors (ConnectTimeout, ReadTimeout after retry)
                    - Permission denied errors
                    - Tool execution errors with "status": "error" in response
                    - Any error that has occurred 2+ times with same failure
                ii. Only report "pending" for TRULY RETRYABLE situations:
                    - First attempt at an action (not started yet)
                    - Waiting for user input/response
                    - Waiting for external system (first time only)
                    - Transient rate limits (429 with retry-after)
                iii. When in doubt between "error" and "pending": If the same failure happened multiple times, always report "error"
                iv. If you see tool responses containing error statuses, connection failures, or permission denials, you MUST report status as "error" not "pending"
            5. Fallback Action Requirements:
                i. ALWAYS provide a non-empty fallback_action for completed and updated statuses
                ii. Fallback should be context-aware and actionable
                iii. Consider the specific failure modes of the action type
                iv. Provide multiple recovery strategies when applicable (e.g., "Retry up to 3 times with 2-second delays, then log error and notify user")
            6. Subtask Breakdown Requirements:
                i. Use "requires_breakdown" status when an action is too complex to complete as a single unit
                ii. Break down into logical subtasks with clear dependencies
                iii. Each subtask should have a unique subtask_id in format "parent_action_id.sequence" (e.g., "1.1", "1.2")
                iv. Specify depends_on array for subtasks that require previous subtasks to complete first
                v. Set can_perform_autonomously to true if the subtask can be done without user input
            Maintain the exact JSON structure in all responses.

        """ + f"\nExtra Information: below are the list of actions the chat_manager will give you, keep this in mind but don't use this directly only use this if there is any update in any action or you want to insert/delete the actions & return the entire array as entire_actions\n{user_tasks[user_prompt].actions}",

        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    return verify


def instantiate_helper_agent():
    helper = autogen.AssistantAgent(
        name="Helper",
        llm_config=llm_config,
        code_execution_config=False,
        system_message="""You are an Helper Agent,
        Focus: Assisting the Assistant Agent to complete actions.
        Note: Do not coordinate with other agents. After your response, always pass the conversation back to the Assistant Agent.

        You serve as the system's self-healing component with these responsibilities:
        1. Monitor: Continously monitor responses for error patterns, especially JSON with {"status": "error"} format
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
            If you want to send data proactively (on your own) to user use `@user {"message2user": "message here"}`. However, if you're responding to the user's request or instruction, use the send_message_to_user or send_message_in_seconds tool.
            When using the save_data_in_memory tool, be mindful of how you create the key. Ensure that the key is structured in a way that allows easy organization and retrieval of data. Use dot notation to create a logical key path. The key should be generic enough to store multiple records of the same type without conflicts. Avoid using specific values as part of the key
                For example:
                    - stories.story_name - Good key structure for storing multiple stories.
                    - creator.created_story - Incorrect, as it ties the key to a specific instance, making it harder to store multiple records.
            When receiving responses from tools that should return JSON, always use the validate_json_response tool to ensure valid JSON formatting before processing further. This helps prevent errors when parsing tool output.
        Data Management:
            Use the get_set_internal_memory tool to store or retrieve user information as needed.""",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    return helper


def instantiate_assistant_agent(list_of_persona, user_prompt):
    assistant = autogen.AssistantAgent(
        name="Assistant",
        llm_config=llm_config,
        code_execution_config={"last_n_messages": 2, "work_dir": "coding", "use_docker": False},
        system_message="""•Purpose: The assistant executes actions provided by the ChatInstructor, seeks help from Helper and Executor agents when necessary, and ensures actions are completed accurately.
        •Action Flow:
            1. Receive Action: Ask the UserProxy to associate the action with a persona (if multiple personas exist).
            2. Analyze Complexity:
                - Before executing, assess if the action is complex and requires breaking down into subtasks.
                - If the action involves multiple distinct steps, dynamic flows, or could fail partially, consider requesting breakdown via @StatusVerifier.
            3. Execution:
                - Understand and plan the current action execution.
                - Perform the action with the help of @Helper and @Executor agents.
                - Account for all the tools available with helper & whenever you are supposed to call a tool as part of current action ask @Helper.
                - If the action requires calculation, code execution or API endpoint call, CREATE code(python preferred) and ask @Executor agent to execute the created code.
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
        """ + f"Extra Information: below are the list of actions the chat_manager is gonna give you keep this in mind but dont use this directly\n{user_tasks[user_prompt].actions}",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    return assistant


def create_time_agents(user_id, prompt_id,role,goal,actions):
    user_prompt = f'{user_id}_{prompt_id}'
    time_actions[user_prompt] = Action(actions)

    time_agent = autogen.AssistantAgent(
        name='time_agent',
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        code_execution_config={"work_dir": "coding", "use_docker": False},
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
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )
    helper1 = autogen.AssistantAgent(
        name="Helper",
        llm_config=llm_config,
        code_execution_config={"work_dir": "coding", "use_docker": False},
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
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    executor1 = autogen.AssistantAgent(
        name="Executor",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
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
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
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
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )

    chat_instructor1 = autogen.UserProxyAgent(
        name="ChatInstructor",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        default_auto_reply="TERMINATE",
        code_execution_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )

    helper1.register_for_llm(name="text_2_image", description="Text to image Creator")(helper_fun.txt2img)
    time_agent.register_for_execution(name="text_2_image")(helper_fun.txt2img)

    @log_tool_execution
    def camera_inp(inp: Annotated[str, "The Question to check from visual context"])->str:
        return helper_fun.get_user_camera_inp(inp, int(user_id), request_id_list[user_prompt] )
    helper1.register_for_llm(name="get_user_camera_inp", description="Get user's visual information to process somethings")(camera_inp)
    time_agent.register_for_execution(name="get_user_camera_inp")(camera_inp)

    @log_tool_execution
    def save_data_in_memory(key: Annotated[
        str, "Key path for storing data now & retrieving data later. Use dot notation for nested keys (e.g., 'user.info.name')."],
                            value: Annotated[Optional[
                                Any], "Value you want to store; strictly should be one of int, float, bool, json array or json object."] = None) -> str:
        """Store data with validation to prevent corruption."""
        tool_logger.info('INSIDE save_data_in_memory')
        # Validate the input data
        try:
            # Step 1: Use the existing JSON repair function to sanitize input
            if isinstance(value, str) and (value.startswith('{') or value.startswith('[')):
                # If the value is a JSON string, repair it
                value = retrieve_json(value)
                tool_logger.info(f"REPAIRED JSON STRING: {value}")
            # Step 2: Force a JSON serialization/deserialization cycle to validate structure
            if value is not None:
                # This will fail if the structure isn't JSON-compatible
                json_str = json.dumps(value)
                validated_value = json.loads(json_str)
                tool_logger.info(f"VALIDATED VALUE (post JSON cycle): {validated_value}")
            else:
                validated_value = None
            # Step 3: Store the validated data
            keys = key.split('.')
            d = agent_data.setdefault(prompt_id, {})

            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = validated_value
            tool_logger.info(f"VALUES STORED IN AGENT DATA: {validated_value}")
            tool_logger.info(f"FULL AGENT DATA AT KEY: {d}")
            # Step 4: Verify storage was successful
            try:
                # Attempt to read back the data to verify it was stored correctly
                stored_value = get_data_by_key(key)
                tool_logger.info(f"VERIFICATION - READ BACK VALUE: {stored_value}")
                # Optional: compare stored_value with what we intended to store
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

    helper1.register_for_llm(name="save_data_in_memory", description="Use this to Store and retrieve data using key-value storage system")(save_data_in_memory)
    time_agent.register_for_execution(name="save_data_in_memory")(save_data_in_memory)

    @log_tool_execution
    def get_saved_metadata() -> str:
        stripped_json = strip_json_values(agent_data[prompt_id])
        return f'{stripped_json}'

    helper1.register_for_llm(name="get_saved_metadata", description="Returns the schema of the json from internal memory with all keys but without actual values.")(get_saved_metadata)
    time_agent.register_for_execution(name="get_saved_metadata")(get_saved_metadata)

    @log_tool_execution
    def get_data_by_key(key: Annotated[str, "Key path for retrieving data. Use dot notation for nested keys (e.g., 'user.info.name')."]) -> str:
        keys = key.split('.')
        d = agent_data.get(prompt_id, {})

        try:
            for k in keys:
                d = d[k]
            return f'{d}'
        except KeyError:
            return "Key not found in stored data."


    helper1.register_for_llm(name="get_data_by_key", description="Returns all data from the internal Memory")(get_data_by_key)
    time_agent.register_for_execution(name="get_data_by_key")(get_data_by_key)
    helper1.register_for_llm(name="search_visual_history", description="Search past camera and screen descriptions by keyword and time range.")(search_visual_history)
    time_agent.register_for_execution(name="search_visual_history")(search_visual_history)

    # --- SimpleMem long-term memory tools for time agents ---
    simplemem_store = user_simplemem.get(user_prompt)
    if simplemem_store is not None:
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
                tool_logger.info(f"SimpleMem search error: {e}")
                return "Memory search unavailable."

        helper1.register_for_llm(
            name="search_long_term_memory",
            description="Search long-term memory for past conversations, facts, and context using natural language query."
        )(search_long_term_memory)
        time_agent.register_for_execution(name="search_long_term_memory")(search_long_term_memory)

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
                return "Saved to long-term memory."
            except Exception as e:
                tool_logger.info(f"SimpleMem save error: {e}")
                return "Failed to save to long-term memory."

        helper1.register_for_llm(
            name="save_to_long_term_memory",
            description="Save important facts or information to long-term memory for future retrieval across sessions."
        )(save_to_long_term_memory)
        time_agent.register_for_execution(name="save_to_long_term_memory")(save_to_long_term_memory)

    @log_tool_execution
    def get_user_id() -> str:
        tool_logger.info('INSIDE get_user_id')
        return f'{user_id}'


    helper1.register_for_llm(name="get_user_id", description="Returns the unique identifier (user_id) of the current user.")(get_user_id)
    time_agent.register_for_execution(name="get_user_id")(get_user_id)

    @log_tool_execution
    def get_prompt_id() -> str:
        tool_logger.info('INSIDE get_prompt_id')
        return f'{prompt_id}'


    helper1.register_for_llm(name="get_prompt_id", description="Returns the unique identifier (prompt_id) associated with the current prompt or conversation.")(get_prompt_id)
    time_agent.register_for_execution(name="get_prompt_id")(get_prompt_id)

    @log_tool_execution
    def Generate_video(text: Annotated[str, "Text to be used for video generation"],
                       avatar_id: Annotated[int, "Unique identifier for the avatar"],
                       realtime: Annotated[bool, "If True, response is fast but less realistic"]) -> str:
        tool_logger.info('INSIDE Generate_video')
        database_url = 'https://mailer.hertzai.com'
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        tool_logger.info(f"avtar_id: {avatar_id}:\n{text[:10]}....\n")

        headers = {'Content-Type': 'application/json'}

        # Initialize data with correct types to match both VideoGenerateSave model and downstream video_gen
        data = {}
        data["text"] = str(text)
        data['flag_hallo'] = 'false'  # String - downstream: str().lower() == "true"
        data['chattts'] = False  # Boolean - downstream: data.get("chattts", False)
        data['openvoice'] = "false"  # String - downstream: str().lower() == "true"

        try:
            res = requests.get(f"{database_url}/get_image_by_id/{avatar_id}")
            res = res.json()
            new_image_url = res["image_url"]
            voice_id = res.get('voice_id')
        except:
            data['openvoice'] = "true"
            new_image_url = None
            voice_id = None

        # String values for downstream compatibility
        data["cartoon_image"] = "True"  # String - downstream: == "True"
        data["bg_url"] = 'http://stream.mcgroce.com/txt/examples_cartoon/roy_bg.jpg'
        data['vtoonify'] = "false"
        data["image_url"] = new_image_url  # Optional[str] - can be None
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
            data['chattts'] = True  # Boolean - downstream expects boolean
            data['flag_hallo'] = "true"  # String
            data["cartoon_image"] = "False"  # String

        # Handle voice sample
        if voice_id is not None:
            try:
                voice_sample = requests.get(f"{database_url}/get_voice_sample_id/{voice_id}")
                voice_sample = voice_sample.json()
                audio_url = voice_sample.get("voice_sample_url")
                data["audio_sample_url"] = audio_url  # Optional[str] - can be None
                data['voice_id'] = int(voice_id) if voice_id else None  # Integer or None
            except:
                data["audio_sample_url"] = None
                data['voice_id'] = None
        else:
            data["audio_sample_url"] = None
            data['voice_id'] = None

        # Integer values for downstream compatibility
        conv_id = save_conversation_db(text, user_id, prompt_id, database_url, request_id)
        data['conv_id'] = int(conv_id)  # Integer - downstream uses in DB operations
        data['avatar_id'] = int(avatar_id)  # Integer - downstream: int(data.get("avatar_id", 0))
        data['timeout'] = int(timeout)  # Integer - downstream uses in calculations

        # Debug: Show data types being sent
        tool_logger.info("=== DATA TYPES BEING SENT ===")
        type_check = {
            'strings': ['text', 'flag_hallo', 'openvoice', 'cartoon_image', 'bg_url', 'vtoonify',
                        'im_crop', 'remove_bg', 'hd_video', 'uid', 'gradient', 'cus_bg',
                        'solid_color', 'inpainting', 'prompt', 'gender'],
            'integers': ['conv_id', 'avatar_id', 'timeout'],
            'booleans': ['chattts'],
            'optional_strings': ['image_url', 'audio_sample_url'],
            'optional_integers': ['voice_id']
        }

        for type_name, fields in type_check.items():
            for field in fields:
                if field in data:
                    value = data[field]
                    tool_logger.info(f"  {field}: {value} (type: {type(value).__name__})")

        try:
            tool_logger.info(f"Sending request to {database_url}/video_generate_save")
            video_link = requests.post(f"{database_url}/video_generate_save",
                                       data=json.dumps(data), headers=headers, timeout=1)
            tool_logger.info(f"Response status: {video_link.status_code}")
            if video_link.status_code == 422:
                tool_logger.error(f"422 Validation Error: {video_link.text}")
            elif video_link.status_code != 200:
                tool_logger.error(f"Error response: {video_link.text}")
            else:
                tool_logger.info("[OK] Request successful!")
        except Exception as e:
            tool_logger.error(f"Request failed: {e}")

        if data['chattts'] or data['flag_hallo'] == "true":
            return f"Video Generation task added to queue with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"
        else:
            return f"Video Generation completed with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"
    helper1.register_for_llm(name="Generate_video", description="Generate/presynthesize video with text and save it in database")(Generate_video)
    time_agent.register_for_execution(name="Generate_video")(Generate_video)

    @log_tool_execution
    def recent_files() -> str:
        tool_logger.info('INSIDE get_user_uploaded_file')
        if recent_file_id[user_id]:
            return f'Got user uploaded file the file_id is {recent_file_id[user_id]}'

        return 'No file uploaded from user'

    helper1.register_for_llm(name="get_user_uploaded_file", description="get user's recent uploaded files")(recent_files)
    time_agent.register_for_execution(name="get_user_uploaded_file")(recent_files)

    @log_tool_execution
    def img2txt(image_url: Annotated[str, "image url of which you want text"],text: Annotated[str, "the details you want from image"]='Describe the Images & Text data in this image in detail') -> str:
        tool_logger.info('INSIDE img2txt')
        url = "http://azurekong.hertzai.com:8000/llava/image_inference"

        payload = {
            'url': image_url,
            'prompt': text
        }
        files = []
        headers = {}

        response = requests.request(
            "POST", url, headers=headers, data=payload, files=files, timeout=300)
        if response.status_code == 200:
            return response.text
        else:
            return 'Not able to get this page details try later'

    helper1.register_for_llm(name="get_text_from_image", description="Image to Text")(img2txt)
    time_agent.register_for_execution(name="get_text_from_image")(img2txt)

    @log_tool_execution
    def create_scheduled_jobs(interval_sec: Annotated[int, "time between two Interval in seconds."],
                            job_description: Annotated[str, "Description of the job to be performed"],
                            cron_expression: Annotated[Optional[str], "Cron expression for scheduling. Example: '0 9 * * 1-5' (Runs at 9:00 AM, Monday to Friday). If the interval is greater than 60 seconds or it needs to be executed at a dynamic cron time this argument is Mandatory else None"]=None) -> str:
        tool_logger.info('INSIDE create_scheduled_jobs')

        # actual_execution_time = sum(task_time[prompt_id]['times'][-1])
        # if interval_sec < actual_execution_time:
        #     return f"Unable to create scheduled job for the specified interval because the actual execution time ({actual_execution_time} seconds) exceeds the interval between jobs ({interval_sec} seconds). Please use an interval longer than {actual_execution_time} seconds. Would you like to create a scheduled job with this updated interval?"

        # if not scheduler.running:
        #     scheduler.start()

        # try:
        #     if not interval_sec or int(interval_sec) >60:
        #         trigger = CronTrigger.from_crontab(cron_expression)
        #         job_id = f"job_{int(time.time())}"
        #         scheduler.add_job(execute_python_file, trigger=trigger, id=job_id, args=[job_description, int(user_id),int(prompt_id)])
        #         tool_logger.info('Successfully created scheduler job')
        #         return 'Successfully created scheduler job'
        #     else:
        #         trigger = IntervalTrigger(seconds=int(interval_sec))
        #         job_id = f"job_{int(time.time())}"
        #         scheduler.add_job(execute_python_file, trigger=trigger, id=job_id, args=[job_description, int(user_id),int(prompt_id)])
        #         tool_logger.info('Successfully created scheduler job')
        #         return 'Successfully created scheduler job'
        # except Exception as e:
        #     tool_logger.error(f'Error in create_scheduled_jobs: {str(e)}')
        #     return f"Error creating scheduled job: {str(e)}"
        return 'Added this schedule job in creation process will do it at the end. you can go ahead and mark this action as completed.'

    helper1.register_for_llm(name="create_scheduled_jobs", description="Creates time-based jobs using APScheduler to schedule jobs")(create_scheduled_jobs)
    time_agent.register_for_execution(name="create_scheduled_jobs")(create_scheduled_jobs)

    @log_tool_execution
    def send_message_to_user(text: Annotated[str, "Text to send to the user"],
                         avatar_id: Annotated[Optional[str], "Unique identifier for the avatar"] = None,
                         response_type: Annotated[Optional[str], "Response mode: 'Realistic' (slower, better quality) or 'Realtime' (faster, lower quality)"] = 'Realtime') -> str:
        tool_logger.info('INSIDE send_message_to_user')
        tool_logger.info(f'SENDING DATA 2 user with values text:{text}, avatar_id:{avatar_id}, response_type:{response_type}')
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        thread = threading.Thread(target=send_message_to_user1, args=(user_id, text, '',prompt_id))
        thread.start()
        return f'Message sent successfully to user with request_id: {request_id_list[user_prompt]}-intermediate'

    helper1.register_for_llm(name="send_message_to_user", description="Sends a message/information to user. You can use this if you want to ask a question")(send_message_to_user)
    time_agent.register_for_execution(name="send_message_to_user")(send_message_to_user)

    @log_tool_execution
    def send_presynthesized_video_to_user(conv_id: Annotated[str, "Conversation ID associated with the text from memory"]) -> str:
        tool_logger.info('INSIDE send_presynthesized_video_to_user')
        tool_logger.info(f'SENDING DATA 2 user with value: conv_id:{conv_id}.')
        return 'Message sent successfully to user'

    helper1.register_for_llm(name="send_presynthesized_video_to_user", description="Sends a presynthesized message/video/dialogue to user using conv_id.")(send_presynthesized_video_to_user)
    time_agent.register_for_execution(name="send_presynthesized_video_to_user")(send_presynthesized_video_to_user)

    @log_tool_execution
    def send_message_in_seconds(text: Annotated[str, "text to send to user"],
                       delay: Annotated[int, "time to wait in seconds before sending text"],
                       conv_id: Annotated[Optional[int], "conv_id for this text if not available make it None"],) -> str:
        tool_logger.info('INSIDE send_message_in_seconds')
        tool_logger.info(f'with text:{text}. and waiting time: {delay} conv_id: {conv_id}')
        run_time = datetime.fromtimestamp(time.time() + delay)
        scheduler.add_job(send_message_to_user1, 'date', run_date=run_time, args=[user_id, text, '',prompt_id])
        return 'Message scheduled successfully'

    helper1.register_for_llm(name="send_message_in_seconds", description="Sends a presynthesized message/video/dialogue to user using conv_id with a timer.")(send_message_in_seconds)
    time_agent.register_for_execution(name="send_message_in_seconds")(send_message_in_seconds)


    context_handling = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=50,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(user_tasks=user_tasks, user_prompt=user_prompt),
        ]
    )
    context_handling.add_to_agent(time_agent)
    context_handling.add_to_agent(helper1)
    context_handling.add_to_agent(executor1)
    context_handling.add_to_agent(multi_role_agent1)
    context_handling.add_to_agent(verify1)

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
                    except:
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
        if last_speaker.name == f"user_proxy_{user_id}" or last_speaker.name == "multi_role_agent" or last_speaker.name == "helper" or last_speaker.name == "Executor":
            return time_agent
        current_app.logger.info(f'Checking for @user or @user in message')
        if '@user' in messages[-1]["content"].lower():
            current_app.logger.info('GOT @USER in message')
            temp_message = messages[-1]["content"]
            temp_message = temp_message.replace("'",'"')
            json_match = re.search(r'{[\s\S]*}', temp_message)
            if json_match:
                try:
                    current_app.logger.info('GOT Json')
                    current_app.logger.info(f'got json object')
                    json_part = json_match.group(0)
                    current_app.logger.info('Sending user the message')
                    json_obj = json.loads(json_part)
                    send_message_to_user1(user_id,json_obj['message2user'],'',prompt_id)
                except:
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
            transforms.MessageHistoryLimiter(max_messages=50,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(user_tasks=user_tasks, user_prompt=user_prompt),
        ]
    )
    time_group_chat = autogen.GroupChat(
        agents=[time_agent, helper1, time_user,multi_role_agent1,executor1,chat_instructor1,verify1],
        messages=[],
        max_round=10,
        select_speaker_transform_messages=select_speaker_transforms,
        speaker_selection_method=state_transition1,  # using an LLM to decide
        allow_repeat_speaker=True,  # Prevent same agent speaking twice
        send_introductions=False
    )

    time_manager = autogen.GroupChatManager(
        groupchat=time_group_chat,
        llm_config={"cache_seed": None,"config_list": config_list}
    )

    time_agent_object['time_group_chat'] = time_group_chat
    time_agent_object['time_manager'] = time_manager
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
    except:
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

def create_action_with_ledger(actions: List[Dict], user_id: int, prompt_id: int, user_prompt: str) -> Action:
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

    Returns:
        Action instance with Smart Ledger attached
    """
    action_instance = Action(actions)

    # Create or load ledger with production backend (Redis with JSON fallback)
    if user_prompt not in user_ledgers:
        current_app.logger.info(f"Creating new Smart Ledger for {user_prompt}")
        backend = get_production_backend()  # Tries Redis, falls back to JSON (already imported from agent_ledger)
        ledger = create_ledger_from_actions(user_id, prompt_id, actions, backend=backend)
        user_ledgers[user_prompt] = ledger

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
            except:
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

            message = user_tasks[user_prompt].get_action(current_action_id - 1)
            current_state = get_action_state(user_prompt, current_action_id)

            message = f'Execute Action {user_tasks[user_prompt].current_action}: {message} '+f',Latest User message: {text}'
            publish_to_crossbar_new_action_start(message, user_id)
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

        current_app.logger.info("\n=== Chat Summary ===")
        current_app.logger.info("\n=== Full response ===")

        # Main processing loop
        while_loop_iterations = 0
        max_iterations = 30  # Increased to allow more autonomous task completion

        while while_loop_iterations < max_iterations:
            while_loop_iterations += 1
            current_action_id = user_tasks[user_prompt].current_action

            current_app.logger.info(f"WHILE LOOP ITERATION #{while_loop_iterations} , Current Action Id:{current_action_id}")

            track_lifecycle_hooks(current_action_id, group_chat, user_prompt)

            # Load persona info from config
            role = load_persona_role(prompt_id, user_prompt)

            current_app.logger.info('inside while')
            current_state = get_action_state(user_prompt, current_action_id)

            if group_chat.messages[-1]['name'] == 'ChatInstructor' and group_chat.messages[-1]['content'] == 'TERMINATE':
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
                                sync_action_state_to_ledger(user_prompt, current_action_id, ActionState.PENDING, user_ledgers)
                        safe_set_state(user_prompt, current_action_id, ActionState.PENDING, "breakdown requested")
                        # Continue to work on subtasks
                        pending_subtasks = get_pending_subtasks(user_prompt, current_action_id, user_ledgers)
                        if pending_subtasks:
                            next_subtask = pending_subtasks[0]
                            message = f"Work on subtask: {next_subtask.description}"
                            result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False)
                        continue
                    elif json_obj['status'].lower() == 'completed' and 'recipe' not in json_obj.keys():
                        json_action_id = int(json_obj.get('action_id', current_action_id))

                        force_state_through_valid_path(user_prompt, json_action_id, ActionState.COMPLETED,
                                                       "verified complete")
                        # Sync completion to ledger with smart routing
                        sync_action_state_to_ledger(user_prompt, json_action_id, ActionState.COMPLETED, user_ledgers)

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
                    if group_chat.messages[-1]['role'] == 'tool':
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

                    result = agents_object['helper'].initiate_chat(recipient=manager, message=message,
                                                                   clear_history=False, silent=False)
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
                        if get_current_flow(user_prompt)  < get_total_flows(user_prompt):
                            current_app.logger.info(f'Completed ONE FLOW NOW WE SHOULD WORK ON NEXT FLOW')
                            current_app.logger.info(f'DELETE CURRENT AGENTS AND CREATE NEW')
                            config = get_prompt_config_json(prompt_id)
                            # recipe_for_persona[user_prompt] += 1
                            user_tasks[user_prompt] = Action(config['flows'][get_current_flow(user_prompt)]['actions'])
                            del user_agents[user_prompt]
                            x = get_response_group(user_id,text,prompt_id)
                            continue
                        scheduler_check[user_prompt] = True
                        json_response = final_recipe[prompt_id]

                        safe_increment_flow(user_prompt, prompt_id)
                        return 'Agent created successfully'
                    result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                else:
                    # user_tasks[user_prompt].current_action = int(json_obj['action_id'])
                    current_app.logger.info(f'current action {user_tasks[user_prompt].current_action} & fallback {user_tasks[user_prompt].fallback} & recipe {user_tasks[user_prompt].recipe}')
                    user_tasks[user_prompt].new_json.append(json_obj)
                    try:
                        message = user_tasks[user_prompt].get_action(current_action_id - 1)
                    except:
                        flow, json_response = after_all_actions_terminated_from_exception(assistant_agent, chat_instructor, flow,
                                                                                          group_chat, manager, prompt_id, user_prompt)
                        if all_flows_completed(prompt_id, get_total_flows(user_prompt), user_prompt):
                            if json_response and 'status' in json_response.keys():
                                merged_dict = {**final_recipe[prompt_id], **json_response}
                                current_app.logger.info('Recipe created successfully')
                                create_final_recipe_for_current_flow(flow,merged_dict, prompt_id)
                                update_agent_creation_to_db(prompt_id)
                                current_app.logger.info('Completed from here2')
                                return 'Agent Created Successfully'
                            return 'Agent created successfully'
                        else:
                            user_tasks[user_prompt].recipe=False
                            user_tasks[user_prompt].fallback=False
                            safe_increment_flow(user_prompt, prompt_id)

                    current_app.logger.info('checking for fallback and recipe')

                    if user_tasks[user_prompt].recipe == True:
                        message = request_recipe_for_action(current_action_id,  prompt_id, role, user_prompt)
                    elif user_tasks[user_prompt].fallback == True:
                        message = request_fallback_for_action(current_action_id,  user_prompt)
                    else:
                        # user_tasks[user_prompt].current_action = user_tasks[user_prompt].current_action+1
                        message = get_execute_next_action_message(prompt_id, user_prompt)
                        publish_to_crossbar_new_action_start(message, user_id)
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

            elif group_chat.messages[-1]['content'].startswith('Focus on the current task at hand'):
                result = agents_object['assistant'].initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                continue
            elif user_tasks[user_prompt].current_action <= len(user_tasks[user_prompt].actions):
                current_app.logger.info(f'current action {user_tasks[user_prompt].current_action} and length of actions is {len(user_tasks[user_prompt].actions)} but no matching condition found')

                if len(group_chat.messages) == 0:
                    current_app.logger.warning("No messages in group chat after processing")
                last_message = group_chat.messages[-1]

                if 'tool_calls' in last_message:
                    current_app.logger.info(
                        f'current action {user_tasks[user_prompt].current_action} continuing since we need to wait for tool cal response')

                    continue

                if last_message['content'] == 'TERMINATE':
                    last_message = group_chat.messages[-2]

                if f'message2user'.lower() in last_message['content'].lower():
                    json_obj = retrieve_json(last_message["content"])
                    if json_obj:
                        try:
                            last_message['content'] = json_obj['message2user']
                        except:
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

                last_message = group_chat.messages[-1]
                if last_message['content'] == 'TERMINATE':
                    last_message = group_chat.messages[-2]

                if f'message2user'.lower() in last_message['content'].lower():
                    json_obj = retrieve_json(last_message["content"])
                    if json_obj:
                        try:
                            last_message['content'] = json_obj['message2user']
                            return last_message['content']
                        except:
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

            if while_loop_iterations > 5 and current_state in [ActionState.FALLBACK_REQUESTED,
                                                               ActionState.RECIPE_REQUESTED]:
                current_app.logger.warning(f"Stuck in {current_state.value} state, attempting recovery")
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

        last_message = group_chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
            last_message = group_chat.messages[-2]

        if f'message2user'.lower() in last_message['content'].lower():
            json_obj = retrieve_json(last_message["content"])
            if json_obj:
                try:
                    last_message['content'] = json_obj['message2user']
                except:
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
        return last_message['content']
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
        flow_recipe_file = f'prompts/{prompt_id}_{flow_idx}_recipe.json'
        if not os.path.exists(flow_recipe_file):
            return False

        # Check all actions in flow are TERMINATED
        for action_id in range(1, len(flow['actions']) + 1):
            if get_action_state(user_prompt, action_id) != ActionState.TERMINATED:
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

                file_path = f'prompts/{prompt_id}_{flow}_{num}.json'
                with open(file_path, 'r') as f:
                    data = json.load(f)
                data['actions_this_action_depends_on'] = action_ids
                with open(file_path, 'w') as f:
                    json.dump(data, f, indent=4)
            else:
                file_path = f'prompts/{prompt_id}_{flow}_{num}.json'
                with open(file_path, 'r') as f:
                    data = json.load(f)
                data['actions_this_action_depends_on'] = []
                with open(file_path, 'w') as f:
                    json.dump(data, f, indent=4)
        except (ValueError, SyntaxError) as e:
            current_app.logger.info(f'GOT ERROR AT EVAL OF LIST :{e}')
            file_path = f'prompts/{prompt_id}_{flow}_{num}.json'
            with open(file_path, 'r') as f:
                data = json.load(f)
            data['actions_this_action_depends_on'] = []
            with open(file_path, 'w') as f:
                json.dump(data, f, indent=4)
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
        f'Current Flow -> recipe_for_persona[user_prompt]:{get_current_flow(user_prompt)} total_persona_actions[user_prompt]:{get_total_flows()}')
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
            file_path = f'prompts/{prompt_id}_{flow}_{num}.json'
            with open(file_path, 'r') as f:
                data = json.load(f)
            data['actions_this_action_depends_on'] = action_ids
            with open(file_path, 'w') as f:
                json.dump(data, f, indent=4)
        else:
            file_path = f'prompts/{prompt_id}_{flow}_{num}.json'
            with open(file_path, 'r') as f:
                data = json.load(f)
            data['actions_this_action_depends_on'] = []
            with open(file_path, 'w') as f:
                json.dump(data, f, indent=4)
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
    crossbar_message = {"text": [
        "Working on " + message + ".\n please evaluate the response i am giving to check if it meets the current action"],
                        "priority": 49, "action": 'Thinking', "historical_request_id": [],
                        "preffered_language": 'en-US', "options": [], "newoptions": [], "bot_type": 'Agent',
                        "page_image_url": "", "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
            'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0},
            'bottom_left': {'x': 0, 'y': 0}}}
    publish_async(
        f"com.hertzai.hevolve.chat.{user_id}", json.dumps(crossbar_message))


# Use lifecycle-aware increment:
def safe_increment_action(user_prompt):
    current_action_id = user_tasks[user_prompt].current_action
    # Ensure current action is TERMINATED before moving to next
    if get_action_state(user_prompt, current_action_id) != ActionState.TERMINATED:
        raise StateTransitionError(f"Action {current_action_id} must be TERMINATED before incrementing")

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
    message = '''Focus on the current task at hand and create a detailed recipe that includes only the necessary steps for this action from history, along with a suitable name. Provide the output in the following JSON format:
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


def request_recipe_for_action(current_action_id, prompt_id, role, user_prompt):
    user_tasks[user_prompt].recipe = False
    user_tasks[user_prompt].fallback = False
    safe_set_state(user_prompt, current_action_id, ActionState.RECIPE_REQUESTED, "recipe start")
    metadata = strip_json_values(agent_data[prompt_id])
    message = '''Focus on the current task at hand and create a detailed recipe that includes only the necessary steps for this action, along with a suitable name. Provide the output in the following JSON format:
                        { "status": "done", "action": "Describe the action performed here","fallback_action":"", "persona":"","action_id": ''' + f'{user_tasks[user_prompt].current_action}' + ''', "recipe": [{{"steps":"steps here","tool_name":"Only include tool name here if used for this step.","generalized_functions": "Only include this field if any Python code is created, otherwise omit it entirely."}}],"can_perform_without_user_input":"can you perform this action on your own without user input in future. only say no when it is absolutely mandatory and you cannot proceed without it, if you can proceed by checking with other agents you should say yes.  say yes/no if no they give the reason as well e.g. no-i need user's likes and dislike", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job is present; if no time-based job exists, do not create it.","persona":"", "action_entry_point":"An integer action_id is required as an entrypoint from list of existing action_ids to perform this job","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ] }
                        Recipe Requirements:
                        1. Generalized Python Functions: Give the code which was created and excuted successfully without any error handling edge cases. leave it blank when there is no code nedded to perform the action
                        2. Avoid directly storing any specific information provided by the author in the recipe. Use placeholders for variables instead.
                        3. Ensure that coding and non-coding steps are not combined within the same function.
                        4. For all Python functions, include comprehensive docstrings to explain their purpose, parameters, and usage. This should especially clarify non-coding steps that require utilizing the assistant's language capabilities.
                        5. If any internal tool is used to complete a step, provide detailed instructions on how to call or utilize that tool instead of providing the code for that step.
                        ''' + f'6. Metadata created till this action: {metadata}\n7. The persona must be one of the following: {role}. No other personas are allowed.'
    return message

def fix_cyclic_dependency(cyc, individual_recipe):
    res = fix_actions(individual_recipe, cyc)
    for i in res:
        for j in individual_recipe:
            if i['action_id'] == j['action_id']:
                j['actions_this_action_depends_on'] = i['actions_this_action_depends_on']
                break


def set_individual_recipes(flow, individual_recipe, prompt_id, user_prompt):
    for i in range(1, user_tasks[user_prompt].current_action+1):
        current_app.logger.info(f'checking for prompts/{prompt_id}_{flow}_{i}.json')
        try:
            with open(f"prompts/{prompt_id}_{flow}_{i}.json", 'r') as f:
                config = json.load(f)
                individual_recipe.append(config)
        except Exception as e:
            current_app.logger.error(f'Got error as :{e} while checking for prompts/{prompt_id}_{flow}_{i}.json')


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
recipe_for_persona = TTLCache(ttl_seconds=7200, max_size=500, name='create_recipe_for_persona')
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
    total_flows = len(config['flows'])

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
        flow_recipe_file = f'prompts/{prompt_id}_{flow_idx}_recipe.json'
        flow_recipe_exists = os.path.exists(flow_recipe_file)

        # Count completed actions in this flow (actions with JSON files)
        completed_actions = []
        for action_id in range(1, total_actions_in_flow + 1):
            action_file = f'prompts/{prompt_id}_{flow_idx}_{action_id}.json'
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
        return False, 'Agent Created Successfully'

    current_flow_actions = get_total_actions_length_for_flow(config, current_flow)

    # [OK] FIX: Handle action exceeding current flow
    if current_action_id > current_flow_actions:
        current_app.logger.info(
            f"Action {current_action_id} exceeds flow {current_flow} actions ({current_flow_actions})")

        # Check if current flow is actually complete (all actions have JSON files)
        all_actions_complete = True
        for action_id in range(1, current_flow_actions + 1):
            action_file = f'prompts/{prompt_id}_{current_flow}_{action_id}.json'
            if not os.path.exists(action_file):
                all_actions_complete = False
                current_app.logger.warning(f"Action {action_id} not complete - missing {action_file}")
                break

        if not all_actions_complete:
            # [OK] FIX: Find the first incomplete action and resume from there
            for action_id in range(1, current_flow_actions + 1):
                action_file = f'prompts/{prompt_id}_{current_flow}_{action_id}.json'
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
            user_tasks[user_prompt] = Action(next_flow_actions)
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
    current_app.logger.info(f"   - Scheduler Check: {scheduler_check[user_prompt]}")
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
                action_file = f'prompts/{prompt_id}_{flow_idx}_{action_id}.json'
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

        # Check if all flows are already complete
        if scheduler_check[user_prompt]:
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
    if scheduler_check[user_prompt] == True:
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
    except:
        pass

    return last_response


def initialise_current_flow_to_zero(user_prompt):
    recipe_for_persona[user_prompt] = 0


def increment_current_flow(user_prompt):
    recipe_for_persona[user_prompt] += 1
    user_tasks[user_prompt] = Action(config['flows'][get_current_flow(user_prompt)]['actions'])
    user_tasks[user_prompt].current_action = 1



def safe_increment_flow(user_prompt, prompt_id):
    current_flow = get_current_flow(user_prompt)

    # Ensure all actions in current flow are TERMINATED
    config = get_prompt_config_json(prompt_id)
    current_flow_actions = config['flows'][current_flow]['actions']

    for action_id in range(1, len(current_flow_actions) + 1):
        if get_action_state(user_prompt, action_id) != ActionState.TERMINATED:
            raise StateTransitionError(f"Cannot increment flow: Action {action_id} not terminated")

    increment_current_flow(user_prompt)

    # Reset action states for new flow
    next_flow = get_current_flow(user_prompt)
    if next_flow < len(config['flows']):
        next_flow_actions = config['flows'][next_flow]['actions']
        for action_id in range(1, len(next_flow_actions) + 1):
            safe_set_state(user_prompt, action_id, ActionState.ASSIGNED, "new flow started")

def update_agent_creation_to_db(prompt_id):
    url = f'{database_url}/update_agent_prompt?prompt_id={prompt_id}'
    headers = {'Content-Type': 'application/json'}
    res = requests.patch(url, headers=headers)


def create_final_recipe_for_current_flow(flow, merged_dict, prompt_id):
    name = f'prompts/{prompt_id}_{flow}_recipe.json'
    with open(name, "w") as json_file:
        json.dump(merged_dict, json_file)
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
        with open(f"prompts/{prompt_id}_{i}_recipe.json", 'r') as f:
            merged_dict = json.load(f)
            final_recipe[prompt_id] = merged_dict
            current_app.logger.info(f'updating the final recipe with prompts/{prompt_id}_{i}_recipe.json')
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
    user_tasks[user_prompt] = Action(config['flows'][flow_idx]['actions'])
    total_actions = get_total_actions_length_for_flow(config,flow_idx)
    return config, total_actions


def get_prompt_config_json(prompt_id):
    with open(f"prompts/{prompt_id}.json", 'r') as f:
        config = json.load(f)
    return config


def acknowledgment(user_id,prompt_id,request_id):
    user_prompt = f'{user_id}_{prompt_id}'
    author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_prompt]
    group_chat.messages.append({'content':f'GOT MESSAGE ACKNOWLEDGEMENT FOR {request_id}','role':'user','name':'Helper'})
