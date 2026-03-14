"""reuse_recipe.py"""
from enum import Enum
import random
try:
    import autogen
except ImportError:
    autogen = None
import os
import pytz
from core.http_pool import pooled_get, pooled_post, pooled_request
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
from helper import ToolMessageHandler, strip_json_values, get_time_based_history, retrieve_json, load_vlm_agent_files, _is_terminate_msg
try:
    from helper import PROMPTS_DIR
except Exception:
    PROMPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), 'prompts'))
os.makedirs(PROMPTS_DIR, exist_ok=True)
import helper as helper_fun
from autogen.agentchat.contrib.capabilities import transform_messages, transforms
import threading
from concurrent.futures import ThreadPoolExecutor
import traceback
from autobahn.asyncio.component import Component

from threadlocal import thread_local_data

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
from helper_ledger import (
    add_subtasks_to_ledger,
    check_and_unblock_parent,
    get_pending_subtasks,
    get_default_llm_client
)

# Import sync function from lifecycle_hooks
from lifecycle_hooks import sync_action_state_to_ledger
from cultural_wisdom import get_cultural_prompt


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
    """Delegate to the canonical publish_async in langchain_gpt_api."""
    from langchain_gpt_api import publish_async as _publish
    _publish(topic, message, timeout)

scheduler = BackgroundScheduler()
scheduler.start()
# logging_session_id = runtime_logging.start(config={"dbname": "logs.db"})
# Store user-specific agents & their chat history
# Performance: TTL caches replace unbounded global dicts (auto-expire after 2 hours)
user_agents: Dict[str, Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]] = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_user_agents')
role_agents: Dict[str, Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]] = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_role_agents')
agents_session = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_agents_session')
recipes = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_recipes', loader=load_recipe)
user_journey = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_user_journey')
temp_users = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_temp_users')
chat_joinees = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_chat_joinees')
agents_roles = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_agents_roles')
llm_call_track = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_llm_call_track')

_active_tools = {}
_active_tools_lock = threading.Lock()

redis_client = redis.StrictRedis(
    host='azure_all_vms.hertzai.com', port=6369, db=0)
agent_data = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_agent_data', loader=load_agent_data)
user_simplemem = TTLCache(ttl_seconds=7200, max_size=500, name='reuse_user_simplemem', loader=load_user_simplemem)
# Azure OpenAI configuration (fallback - gpt-4o)
# config_list = [{
#     "model": 'gpt-4o',
#     "api_type": "azure",
#     "api_key": '4xmi9X9pGCwRn2Pb0vldz6t6FQaAe29bUIkFjKRC7ytrVZ1Ni5cWJQQJ99BAACHYHv6XJ3w3AAABACOG99Zf',
#     "base_url": 'https://hertzai-gpt4.openai.azure.com/',
#     "api_version": "2024-02-15-preview",
#     "price": [0.0025, 0.01]
# }]

# Mode-aware config_list: cloud/regional use external LLM, flat uses local
# (user's wizard-configured endpoint via HEVOLVE_LOCAL_LLM_URL or LLAMA_CPP_PORT)
_node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
_active_cloud = os.environ.get('HEVOLVE_ACTIVE_CLOUD_PROVIDER', '')
if _node_tier in ('regional', 'central') and os.environ.get('HEVOLVE_LLM_ENDPOINT_URL'):
    config_list = [{
        "model": os.environ.get('HEVOLVE_LLM_MODEL_NAME', 'gpt-4.1-mini'),
        "api_key": os.environ.get('HEVOLVE_LLM_API_KEY', 'dummy'),
        "base_url": os.environ['HEVOLVE_LLM_ENDPOINT_URL'],
        "price": [0.0025, 0.01]
    }]
elif _active_cloud and os.environ.get('HEVOLVE_LLM_API_KEY'):
    # Wizard-configured cloud provider (flat mode desktop user)
    _cloud_cfg = {
        "model": os.environ.get('HEVOLVE_LLM_MODEL_NAME', 'gpt-4o-mini'),
        "api_key": os.environ['HEVOLVE_LLM_API_KEY'],
        "price": [0.0025, 0.01],
    }
    if os.environ.get('HEVOLVE_LLM_ENDPOINT_URL'):
        _cloud_cfg["base_url"] = os.environ['HEVOLVE_LLM_ENDPOINT_URL']
    config_list = [_cloud_cfg]
else:
    # Dynamic: reads from user's LLM Setup Wizard config (set by Nunba app.py)
    from core.port_registry import get_port as _get_llm_port
    _llama_port = os.environ.get('LLAMA_CPP_PORT', str(_get_llm_port('llm')))
    _local_llm_url = os.environ.get('HEVOLVE_LOCAL_LLM_URL', f'http://localhost:{_llama_port}/v1')
    config_list = [{
        "model": os.environ.get('HEVOLVE_LOCAL_LLM_MODEL', 'local'),
        "api_key": 'dummy',
        "base_url": _local_llm_url,
        "price": [0, 0]
    }]

# Per-request model config override (speculative execution, hive compute routing)
def get_llm_config():
    """Get LLM config — checks thread-local override before falling back to global."""
    from threadlocal import thread_local_data
    override = thread_local_data.get_model_config_override()
    return {"cache_seed": None, "config_list": override or config_list, "max_tokens": 1500}

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
                    timeout = timeout_seconds
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

    try:
        # Start the component
        await component.start()

        # Calculate timeout with a small buffer
        actual_timeout = (time/1000) + 5 # Add 5 second buffer

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


from core.config_cache import get_db_url
database_url = get_db_url() or 'https://mailer.hertzai.com'


def save_conversation_db(text, user_id, prompt_id, database_url, request_id):
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
    res = pooled_post("{}/conversation".format(database_url),
                        data=json.dumps(data), headers=headers).json()
    conv_id = res['conv_id']
    return conv_id


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
                        current_app.logger.info(
                            f"Found video Reasoning entry: {obj['action']} (created {time_diff} ago)")
                        if time_diff <= timedelta(minutes=5):
                            recent_video_reasoning_entries.append(obj)
                            current_app.logger.info(
                                f"Found recent Video Reasoning entry: {obj['action']} (created {time_diff} ago)")
                    except (ValueError, KeyError) as e:
                        current_app.logger.warning(f"Error parsing date for entry {obj.get('action_id')}: {e}")
                        continue

            # Execute visual task if at least one recent Video Reasoning entry is found
            if recent_video_reasoning_entries:
                current_app.logger.info(
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
                    current_app.logger.info(f"Visual agent response: {res.status_code}")
                    return 'done'
                except Exception as e:
                    current_app.logger.error(f"Failed to call visual agent: {e}")
                    return 'error'
            else:
                current_app.logger.info(
                    "No recent Video Reasoning entries found (within last 5 minutes) - skipping visual task")
                return None

        else:
            current_app.logger.error(f"Failed to get user actions: {response.status_code}")
            return 'error'

    except Exception as e:
        current_app.logger.error(f"Error getting user action details: {e}")
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
    '''
        This function helps to extract actions that the user has performed till now.
    '''
    unwanted_actions = ['Topic Cofirmation', 'Langchain', 'Assessment Ended', 'Casual Conversation',
                        'Topic confirmation',
                        'Topic not found', 'Topic Confirmation', 'Topic Listing', 'Probe', 'Question Answering',
                        'Fallback']
    action_url = f"{ACTION_API}?user_id={user_id}"

    # Todo: get, and populate timezone from client
    time_zone = "Asia/Kolkata"

    india_tz = pytz.timezone(time_zone)

    payload = {}
    headers = {}

    response = pooled_request(
        "GET", action_url, headers=headers, data=payload)

    if response.status_code == 200:

        data = response.json()

        # Filter out unwanted actions
        filtered_data = [obj for obj in data if obj["action"]
                         not in unwanted_actions and obj["zeroshot_label"]
                         not in ['Video Reasoning', 'Screen Reasoning']]

        filtered_data_video = [
            obj for obj in data if obj["zeroshot_label"] == 'Video Reasoning']
        filtered_data_screen = [
            obj for obj in data if obj["zeroshot_label"] == 'Screen Reasoning']
        # Dictionary to store the first and last occurrence dates for each action
        action_occurrences = {}

        # Iterate over the filtered data
        for obj in filtered_data:
            action = obj["action"]
            date = parse_date(obj["created_date"])
            gpt3_label = obj["gpt3_label"]

            if action not in action_occurrences:
                action_occurrences[action] = [date, date]
            else:
                first_date, last_date = action_occurrences[action]
                first_date = min(first_date, date)
                last_date = max(last_date, date)
                action_occurrences[action] = [first_date, last_date]

        # Construct the final list of actions with first and last occurrences
        action_texts = []
        for action, dates in action_occurrences.items():
            first_date, last_date = dates
            first_action_text = f"{action} on {first_date.astimezone(india_tz).strftime('%Y-%m-%dT%H:%M:%S')}"
            action_texts.append(first_action_text)
            if first_date != last_date:
                last_action_text = f"{action} on {last_date.astimezone(india_tz).strftime('%Y-%m-%dT%H:%M:%S')}"
                action_texts.append(last_action_text)

        # Process video data
        video_context_texts = []
        for obj in filtered_data_video:
            action = obj["action"]
            date = parse_date(obj["created_date"])
            gpt3_label = obj["gpt3_label"]

            if gpt3_label == 'Visual Context':
                now = datetime.now()
                # Check if the action is older than 5 minutes
                if (now - date) > timedelta(minutes=5):
                    continue
            first_action_text = f"{action} on {date.astimezone(india_tz).strftime('%Y-%m-%dT%H:%M:%S')}"

            video_context_texts.append(first_action_text)

        if video_context_texts:
            action_texts.append('<Last_5_Minutes_Visual_Context_Start>')
            action_texts.extend(video_context_texts)
            action_texts.append('<Last_5_Minutes_Visual_Context_End>')
            action_texts.append(
                'If a person is identified in Visual_Context section that\'s most probably the user (me) & most likely not taking any selfie.')

        # Process screen context data (shorter window — 2 minutes)
        screen_context_texts = []
        for obj in filtered_data_screen:
            action = obj["action"]
            date = parse_date(obj["created_date"])
            now = datetime.now()
            if (now - date) > timedelta(minutes=2):
                continue
            screen_text = f"{action} on {date.astimezone(india_tz).strftime('%Y-%m-%dT%H:%M:%S')}"
            screen_context_texts.append(screen_text)

        if screen_context_texts:
            action_texts.append('<Last_2_Minutes_Screen_Context_Start>')
            action_texts.extend(screen_context_texts)
            action_texts.append('<Last_2_Minutes_Screen_Context_End>')
            action_texts.append(
                'Screen_Context shows what is currently displayed on the user\'s computer screen.')

        if len(action_texts) == 0:
            action_texts = ['user has not performed any actions yet.']

        actions = ", ".join(action_texts)
        # Get the current time

        # Format the time in the desired format
        formatted_time = datetime.now(pytz.utc).astimezone(
            india_tz).strftime('%Y-%m-%d %H:%M:%S')

        actions = actions + ". List of actions ends. <PREVIOUS_USER_ACTION_END> \n " + "Today's datetime in " + time_zone + "is: " + formatted_time + \
                  " in this format:'%Y-%m-%dT%H:%M:%S' \n Whenever user is asking about current date or current time at particular location then use this datetime format by asking what user's location is. Use the previous sentence datetime info to answer current time based questions coupled with google_search for current time or full_history for historical conversation based answers. Take a deep breath and think step by step.\n"
        # user detail api
    else:
        post_dict = {'user_id': user_id, 'status': ActionExecutionStatus.ERROR.value,
                     'task_name': TaskNames.GET_ACTION_USER_DETAILS.value, 'uid': thread_local_data.get_request_id(
            ), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}",
                     'request_id': thread_local_data.get_request_id(),
                     'failure_reason': 'Exception happend at get action api end'}
        publish_async('com.hertzai.longrunning.log', post_dict)

    url = STUDENT_API
    payload = json.dumps({
        "user_id": user_id
    })
    headers = {
        'Content-Type': 'application/json'
    }
    response = pooled_request("POST", url, headers=headers, data=payload)
    if response.status_code == 200:
        user_data = response.json()

        user_details = f'''Below are the information about the user.
        user_name: {user_data["name"]} (Call the user by this name only when required and not always),gender: {user_data["gender"]}, who_pays_for_course: {user_data["who_pays_for_course"]}(Entity Responsible for Paying the Course Fees), preferred_language: {user_data["preferred_language"]}(User's Preferred Language), date_of_birth: {user_data["dob"]}, english_proficiency: {user_data["english_proficiency"]}(User's English Proficiency Level), created_date: {user_data["created_date"]}(user creation date), standard: {user_data["standard"]}(User's Standard in which user studying)
        '''
    else:
        post_dict = {'user_id': user_id, 'status': ActionExecutionStatus.ERROR.value,
                     'task_name': TaskNames.GET_ACTION_USER_DETAILS.value, 'uid': thread_local_data.get_request_id(
            ), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}",
                     'request_id': thread_local_data.get_request_id(),
                     'failure_reason': 'Exception happend at get user detail api end'}
        publish_async('com.hertzai.longrunning.log', post_dict)
    return user_details, actions


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
        with open(os.path.join(PROMPTS_DIR, f"{prompt_id}.json"), 'r') as f:
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
            code_execution_config={"work_dir": "coding", "use_docker": False},
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
            code_execution_config={"work_dir": "coding", "use_docker": False},
            system_message="""You Help the assistant agent to complete the task, you are helper agent not user/n
            if you get any request related you user redicrect that conversation to user don't asumer anything or answer anything on your own""",
            is_termination_msg=_is_terminate_msg,
        )

        @helper.register_for_execution()
        @assistant.register_for_llm(api_style="function", description="update the role/persona in db")
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

        select_speaker_transforms = transform_messages.TransformMessages(
            transforms=[
                transforms.MessageHistoryLimiter(max_messages=5),
                transforms.MessageTokenLimiter(max_tokens=3000, max_tokens_per_message=500, min_tokens=300),
            ]
        )
        group_chat = autogen.GroupChat(
            agents=[assistant, helper, user_proxy],
            messages=[],
            max_round=3,
            select_speaker_prompt_template=f"Read the above conversation, select the next person from [Assistant, Helper, & User] & only return the role as agent. Return User only if the previous message demands it",
            select_speaker_transform_messages=select_speaker_transforms,
            speaker_selection_method=state_transition,  # using an LLM to decide
            allow_repeat_speaker=False,  # Prevent same agent speaking twice
            send_introductions=False
        )

        manager = autogen.GroupChatManager(
            groupchat=group_chat,
            llm_config={"cache_seed": None, "config_list": config_list}
        )

        return assistant, user_proxy, group_chat, manager, helper, False
    else:
        agents_session[f"{user_id}_{prompt_id}"] = [
            {'agentInstanceID': f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
             'user_id': user_id, 'role': personas[0]['name'], 'deviceID': 'something'}]

        agents_roles[f"{user_id}_{prompt_id}"] = {user_id: personas[0]['name']}
        return 'TERMINATE', 'TERMINATE', 'TERMINATE', 'TERMINATE', 'TERMINATE', True


def create_agents_for_user(user_id: str, prompt_id) -> Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]:
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

    with open(os.path.join(PROMPTS_DIR, f"{prompt_id}_{role_number}_recipe.json"), 'r') as f:
        config = json.load(f)
        recipes[user_prompt] = config
        final_recipe[prompt_id] = config
    goal = ''
    with open(os.path.join(PROMPTS_DIR, f"{prompt_id}.json"), 'r') as f:
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
        ledger = create_ledger_from_actions(user_id, prompt_id, role_actions, backend=backend)
        user_ledgers[user_prompt] = ledger

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

    individual_recipe = []
    for i in range(1, (len(recipes[user_prompt]['actions']) + 1)):
        current_app.logger.info(f'checking for {os.path.join(PROMPTS_DIR, f"{prompt_id}_{role_number}_{i}.json")}')
        try:
            with open(os.path.join(PROMPTS_DIR, f"{prompt_id}_{role_number}_{i}.json"), 'r') as f:
                config = json.load(f)
                individual_recipe.append(config)
        except Exception as e:
            current_app.logger.error(f'Got error as :{e} while checking for {os.path.join(PROMPTS_DIR, f"{prompt_id}_{role_number}_{i}.json")}')

    # Build experience hints from accumulated recipe experience data
    experience_hints = ''
    try:
        from recipe_experience import build_experience_hints
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
        code_execution_config={"work_dir": "coding", "use_docker": False},
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
        code_execution_config={"last_n_messages": 2, "work_dir": "coding", "use_docker": False},
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
            transforms.MessageHistoryLimiter(max_messages=50, keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(user_tasks=user_tasks, user_prompt=user_prompt),
        ]
    )

    context_handling.add_to_agent(assistant)
    context_handling.add_to_agent(helper)
    context_handling.add_to_agent(executor)
    context_handling.add_to_agent(verify)

    # @executor.register_for_execution()
    # @helper.register_for_llm(api_style="function", description="sends message/ask questions to different roles/personas")
    # def send_message_to_roles(role: Annotated[str, "the role to which the message to send"],
    #                         message: Annotated[str, "The question to ask or message to send"]) -> str:
    #     current_app.logger.info('INSIDE send_message_to_roles')
    #     if f"{user_id}_{prompt_id}" in agents_session.keys():
    #         for i in agents_session[f"{user_id}_{prompt_id}"]:
    #             if i['role'] == role:
    #                 current_app.logger.info(f'got role: {i}')
    #                 crossbar_message = i
    #                 crossbar_message['message'] = message
    #                 crossbar_message['caller_role'] = agents_roles[f"{user_id}_{prompt_id}"][user_id]
    #                 crossbar_message['caller_user_id'] = user_id
    #                 crossbar_message['caller_prompt_id'] = prompt_id
    #                 result = client.publish(
    #                     f"com.hertzai.hevolve.agent.multichat", crossbar_message)
    #                 current_app.logger.info('Published to chat')
    #                 return 'Message sent Successfully'
    #         return 'Not able to send Message try again later'
    #     elif user_id in chat_joinees.keys() and prompt_id in chat_joinees[user_id].keys():
    #         current_app.logger.info('contacting user with chat_joinees')
    #         current_app.logger.info(f'chat_joinees[user_id][prompt_id] {chat_joinees[user_id][prompt_id]}  prompt_id{prompt_id}')
    #         chat_creator_user_id = f"{chat_joinees[user_id][prompt_id]}_{prompt_id}"
    #         current_app.logger.info(f'chat_creator_user_id {chat_creator_user_id}')
    #         for i in agents_session[f"{chat_creator_user_id}"]:
    #             if i['role'] == role:
    #                 current_app.logger.info(f'got role: {i}')
    #                 crossbar_message = i
    #                 crossbar_message['message'] = message
    #                 crossbar_message['caller_role'] = agents_roles[chat_creator_user_id][user_id]
    #                 crossbar_message['caller_user_id'] = user_id
    #                 crossbar_message['caller_prompt_id'] = prompt_id
    #                 result = client.publish(
    #                     f"com.hertzai.hevolve.agent.multichat", crossbar_message)
    #                 current_app.logger.info(result)
    #                 current_app.logger.info('Published to chat')
    #                 return 'Message sent Successfully'
    #         return 'Not able to send Message try again later'
    #
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Text to image Creator")
    def txt2img(text: Annotated[str, "Text to create image"]) -> str:
        current_app.logger.info('INSIDE txt2img')
        url = f"http://aws_rasa.hertzai.com:5459/txt2img?prompt={text}"

        payload = ""
        headers = {}

        response = pooled_post(url, headers=headers, data=payload)
        return response.json()['img_url']

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Image to Text/Question Answering from image")
    def img2txt(image_url: Annotated[str, "image url of which you want text"], text: Annotated[
        str, "the details you want from image"] = 'Describe the Images & Text data in this image in detail') -> str:
        current_app.logger.info('INSIDE img2txt')
        from core.config_cache import get_vision_api
        url = get_vision_api() or "http://azurekong.hertzai.com:8000/llava/image_inference"

        payload = {
            'url': image_url,
            'prompt': text
        }
        files = []
        headers = {}

        response = pooled_request(
            "POST", url, headers=headers, data=payload, files=files, timeout=300)
        if response.status_code == 200:
            return response.text
        else:
            return 'Not able to get this page details try later'

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Use this to Store and retrieve data using key-value storage system")
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
    def get_saved_metadata() -> str:
        stripped_json = strip_json_values(agent_data[prompt_id])
        return f'{stripped_json}'

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Returns all data from the internal Memory using key")
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
    def get_user_id() -> str:
        current_app.logger.info('INSIDE get_user_id')
        return f'{user_id}'

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Returns the unique identifier (prompt_id) associated with the current prompt or conversation.")
    def get_prompt_id() -> str:
        current_app.logger.info('INSIDE get_prompt_id')
        return f'{prompt_id}'

    database_url = get_db_url() or 'https://mailer.hertzai.com'

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Generate video with text and save it in database")
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
    def get_user_uploaded_file() -> str:
        current_app.logger.info('INSIDE get_user_uploaded_file')
        if recent_file_id[user_id]:
            return f'Got user uploaded file the file_id is {recent_file_id[user_id]}'

        return 'No file uploaded from user'

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Get user's visual information to process somethings")
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
    def get_chat_history(text: Annotated[str, "Text related to which you want history"],
                         start: Annotated[str, "start date in format %Y-%m-%dT%H:%M:%S.%fZ"],
                         end: Annotated[str, "end date in format %Y-%m-%dT%H:%M:%S.%fZ"]) -> str:
        current_app.logger.info('INSIDE get_chat_history')
        return get_time_based_history(text, f'user_{user_id}', start, end)

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Search past camera and screen descriptions by keyword and time range. Use for visual history queries.")
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

    # --- SimpleMem long-term memory tools ---
    if simplemem_store is not None:
        @assistant.register_for_execution()
        @helper.register_for_llm(api_style="function",
                                 description="Search long-term memory for past conversations, facts, and context using natural language query. More powerful than get_chat_history for finding relevant information.")
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
    def send_presynthesized_video_to_user(
            conv_id: Annotated[str, "Conversation ID associated with the text from memory"]) -> str:
        current_app.logger.info('INSIDE send_presynthesized_video_to_user')
        current_app.logger.info(f'SENDING DATA 2 user with value: conv_id:{conv_id}.')
        return 'Message sent successfully to user'

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",
                             description="Sends a presynthesized message/video/dialogue to user using conv_id with a timer.")
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
    async def execute_windows_or_android_command(
            instructions: Annotated[str, "Command in plain English to execute on the Windows machine"],
            os_to_control: Annotated[str, "The os to control, possible values are 'windows' or 'android' only "]) -> str:
        """
        Executes a command on a Windows machine and returns the response within 500 seconds.
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

            direct_vlm_path = os.path.join(PROMPTS_DIR, f"{prompt_id}_{role_number}_{current_action_id}_vlm_agent.json")
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
                response = await subscribe_and_return({'prompt_id': prompt_id}, topic, 2000)
                current_app.logger.info(f'Response from call of {topic}: {response}')
                if not response:
                    return 'Ask UserProxy to go to hevolve.ai login and start Nunba - Your Local HART Companion App'

                topic = 'com.hertzai.hevolve.action'
                current_app.logger.info(f'calling {topic} for 1800 seconds')
                response = await subscribe_and_return(crossbar_message, topic, 1800000)

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
                        base_path = os.path.join(PROMPTS_DIR, f"{prompt_id}_{role_number}")

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

                        # Function to format action text consistently
                        def format_action_text(text):
                            # For JSON-like strings
                            if text.strip().startswith("{") and "action" in text:
                                try:
                                    # Try to parse the string as JSON/Python dict safely
                                    try:
                                        action_data = json.loads(text.strip())
                                    except (json.JSONDecodeError, ValueError):
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
                                except Exception:
                                    # If eval fails, try regex
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
                                    else:
                                        return "Perform action"

                            # For text descriptions containing "Perform"
                            elif "Perform" in text and "action" in text:
                                return text  # Already in desired format

                            return text

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
        code_execution_config={"work_dir": "coding", "use_docker": False},
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
        code_execution_config={"work_dir": "coding", "use_docker": False},
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
        code_execution_config={"last_n_messages": 2, "work_dir": "coding", "use_docker": False},
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
            transforms.MessageHistoryLimiter(max_messages=50, keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(user_tasks=user_tasks, user_prompt=user_prompt),
        ]
    )
    context_handling.add_to_agent(time_agent)
    context_handling.add_to_agent(helper1)
    context_handling.add_to_agent(executor1)
    context_handling.add_to_agent(multi_role_agent1)
    context_handling.add_to_agent(verify1)

    # --- Core tools for time_agent (defined once in core/agent_tools.py) ---
    from core.agent_tools import build_core_tool_closures, register_core_tools
    _tool_ctx = {
        'user_id': user_id, 'prompt_id': prompt_id,
        'agent_data': agent_data, 'helper_fun': helper_fun,
        'user_prompt': user_prompt, 'request_id_list': request_id_list,
        'recent_file_id': recent_file_id, 'scheduler': scheduler,
        'simplemem_store': simplemem_store,
        'memory_graph': memory_graph,
        # log_tool_execution not defined in reuse_recipe — uses passthrough default
        'send_message_to_user1': send_message_to_user1,
        'retrieve_json': retrieve_json,
        'strip_json_values': strip_json_values,
        'save_conversation_db': save_conversation_db,
    }
    core_tools = build_core_tool_closures(_tool_ctx)
    register_core_tools(core_tools, helper1, time_agent)

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

    # Register the tool signature with the assistant agent.
    helper1.register_for_llm(name="Connect_to_main_agent",
                             description="Connects time agent to main assistant agemt to perform actions which time agent cannot perform")(
        connect_time_main)

    # Register the tool function with the user proxy agent.
    time_agent.register_for_execution(name="Connect_to_main_agent")(connect_time_main)

    visual_agent, visual_user, helper2, executor2, multi_role_agent2, verify2, chat_instructor2 = helper_fun.create_visual_agent(
        user_id, prompt_id)

    # --- Core tools for visual_agent (reuse same tool closures) ---
    register_core_tools(core_tools, helper2, visual_agent)

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

                    # Register for LLM (helper agent suggests tool use)
                    helper.register_for_llm(name=tool_name, description=description)(tool_func)

                    # Register for execution (assistant agent executes tool)
                    assistant.register_for_execution(name=tool_name)(tool_func)

                    current_app.logger.info(f"Registered MCP tool: {tool_name}")
        else:
            current_app.logger.info("No MCP servers configured - continuing with default tools")
    except Exception as e:
        current_app.logger.warning(f"MCP integration error (non-critical): {e}")
        # Continue with default tools if MCP fails

    # Service Tools: Register HTTP microservice tools (Crawl4AI, AceStep, etc.)
    # Follows same pattern as MCP block above — register tools, get functions, wire to agents
    try:
        from integrations.service_tools import service_tool_registry, Crawl4AITool, AceStepTool

        Crawl4AITool.register()   # port 11235
        AceStepTool.register()    # port 8001
        service_tool_registry.load_config()  # load any user-added tools from service_tools.json

        svc_tools = service_tool_registry.get_all_tool_functions()
        svc_defs = service_tool_registry.get_tool_definitions()

        for tool_name, tool_func in svc_tools.items():
            tool_def = next((d for d in svc_defs if d['name'] == tool_name), None)
            if tool_def:
                description = tool_def.get('description', f'Service tool: {tool_name}')
                helper.register_for_llm(name=tool_name, description=description)(tool_func)
                assistant.register_for_execution(name=tool_name)(tool_func)
                current_app.logger.info(f"Registered service tool: {tool_name}")
    except Exception as e:
        current_app.logger.warning(f"Service tools integration error (non-critical): {e}")

    # HART Skills: Register ingested agent skills (Claude Code, Markdown, GitHub)
    try:
        from integrations.skills import skill_registry
        skill_funcs = skill_registry.get_autogen_tools()
        for func_name, func in skill_funcs.items():
            description = func.__doc__ or f"HART skill: {func_name}"
            helper.register_for_llm(name=func_name, description=description)(func)
            assistant.register_for_execution(name=func_name)(func)
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

        helper.register_for_llm(name="delegate_to_specialist",
                               description="Delegate complex tasks to specialist agents based on required skills")(delegate_to_specialist)
        assistant.register_for_execution(name="delegate_to_specialist")(delegate_to_specialist)

        def share_context_with_agents(context_key: Annotated[str, "Context identifier"],
                                      context_value: Annotated[Any, "Context data"]) -> str:
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

        helper.register_for_llm(name="share_context_with_agents",
                               description="Share context information with other agents")(share_context_with_agents)
        assistant.register_for_execution(name="share_context_with_agents")(share_context_with_agents)

        def get_shared_context(context_key: Annotated[str, "Context identifier"]) -> str:
            """Retrieve context information shared by other agents"""
            retrieval_func = create_context_retrieval_function()
            return retrieval_func(context_key)

        helper.register_for_llm(name="get_shared_context",
                               description="Retrieve context information shared by other agents")(get_shared_context)
        assistant.register_for_execution(name="get_shared_context")(get_shared_context)

        current_app.logger.info("Internal Agent Communication complete - agents can now delegate tasks and share context")

    except Exception as e:
        current_app.logger.warning(f"Internal Agent Communication error (non-critical): {e}")
        # Continue without internal communication if it fails

    # AP2 (Agent Protocol 2): Agentic Commerce - Payment workflows
    try:
        current_app.logger.info("Initializing AP2 (Agent Protocol 2) - Agentic Commerce...")

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

            current_app.logger.info(f"Registered AP2 payment tool: {tool_name}")

        current_app.logger.info("AP2 Agentic Commerce integration complete - agents can now handle payment workflows")

    except Exception as e:
        current_app.logger.warning(f"AP2 Agentic Commerce error (non-critical): {e}")
        # Continue without payment capabilities if AP2 fails

    # Goal-aware Tier 2 tool loading (marketing, coding, etc.)
    try:
        from integrations.agent_engine.marketing_tools import register_marketing_tools
        # Detect goal type from prompt_id prefix (e.g. 'marketing_xxx', 'coding_xxx')
        if str(prompt_id).startswith('marketing'):
            register_marketing_tools(helper, assistant, user_id)
            current_app.logger.info("Marketing tools loaded (Tier 2) for reuse agent")
        if str(prompt_id).startswith('ip_protection'):
            from integrations.agent_engine.ip_protection_tools import register_ip_protection_tools
            register_ip_protection_tools(helper, assistant, user_id)
            current_app.logger.info("IP protection tools loaded (Tier 2) for reuse agent")
    except Exception as e:
        current_app.logger.debug(f"Goal-aware tool loading skipped: {e}")

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
                        current_app.logger.info('GOT COMPLETED FOR ACTION')
                        try:
                            user_tasks[user_prompt].current_action = int(last_json['action_id'])
                        except Exception as e:
                            current_app.logger.error(f'GOT ERROR WHILE UPDATING CURRENT ACTION:{e}')
                            current_app.logger.error(traceback.format_exc())
                        return chat_instructor

                    currentaction_id = last_json['action_id']
                    if individual_recipe[currentaction_id - 1]['can_perform_without_user_input'] == 'yes':
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
                    temp_message = messages[-1]["content"]
                    temp_message = temp_message.replace("'", '"')
                    json_match = re.search(r'{[\s\S]*}', temp_message)
                    if json_match:
                        try:
                            json_part = json_match.group(0)
                            json_obj = json.loads(json_part)
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
                    current_app.logger.info('GOT COMPLETED FOR ACTION')
                    try:
                        time_actions[user_prompt].current_action += 1
                    except Exception:
                        current_app.logger.error('GOT ERROR WHILE UPDATING CURRENT ACTION')
                        time_actions[user_prompt].current_action += 1
                    return chat_instructor1

                currentaction_id = last_json['action_id']
                if final_recipe[prompt_id]['actions'][currentaction_id - 1]['can_perform_without_user_input'] == 'yes':
                    return time_agent
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
            temp_message = messages[-1]["content"]
            temp_message = temp_message.replace("'", '"')
            json_match = re.search(r'{[\s\S]*}', temp_message)
            if json_match:
                try:
                    current_app.logger.info('GOT Json')
                    current_app.logger.info(f'got json object')
                    json_part = json_match.group(0)
                    current_app.logger.info('Sending user the message')
                    json_obj = json.loads(json_part)
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
            temp_message = messages[-1]["content"]
            temp_message = temp_message.replace("'", '"')
            json_match = re.search(r'{[\s\S]*}', temp_message)
            if json_match:
                try:
                    current_app.logger.info('GOT Json')
                    current_app.logger.info(f'got json object')
                    json_part = json_match.group(0)
                    current_app.logger.info('Sending user the message')
                    json_obj = json.loads(json_part)
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
        try:
            if (last_speaker.name not in ['UserProxy', 'User'] and messages[-1]["content"] != '' and messages[-1]["content"] is not None
                    and 'Message already sent successfully to user with request_id' not in messages[-1]["content"]
                    and 'Message sent successfully to user with request_id' not in messages[-1]["content"]
                    and '@user' not in messages[-1]["content"]):
                crossbar_message = {"text": [f'{messages[-1]["content"]}'], "priority": 49,
                                    "action": 'Thinking', "historical_request_id": [], "preferred_language": 'en-US',
                                    "options": [], "newoptions": [], "bot_type": 'Agent', "page_image_url": "",
                                    "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
                        'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0},
                        'bottom_left': {'x': 0, 'y': 0}}}
                publish_async(
                    f"com.hertzai.hevolve.chat.{user_id}", json.dumps(crossbar_message))
        except Exception as e:
            current_app.logger.error(f"Error publishing crossbar message: {e}")

    select_speaker_transforms = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=50, keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
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
        send_introductions=False
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
        send_introductions=False
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
        send_introductions=False
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

    # Auto-ingest group_chat messages into SimpleMem
    if simplemem_store is not None:
        for gc in [group_chat, group_chat_1, group_chat_2]:
            _original_append = gc.messages.append
            def _make_hook(orig_append, store=simplemem_store):
                def _simplemem_ingest_hook(msg):
                    orig_append(msg)
                    try:
                        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                        speaker = msg.get("name", "Agent") if isinstance(msg, dict) else "Agent"
                        if content and len(content.strip()) > 5:
                            loop = get_or_create_event_loop()
                            loop.run_until_complete(store.add(content, {
                                "sender_name": speaker,
                                "user_id": user_id,
                                "prompt_id": prompt_id,
                            }))
                    except Exception:
                        pass  # Non-blocking
                return _simplemem_ingest_hook
            gc.messages.append = _make_hook(_original_append)

    # Auto-ingest group_chat messages into MemoryGraph (provenance tracking)
    if memory_graph is not None:
        for gc in [group_chat, group_chat_1, group_chat_2]:
            _prev_append = gc.messages.append
            def _make_graph_hook(prev_append, graph=memory_graph, session=user_prompt):
                def _graph_ingest_hook(msg):
                    prev_append(msg)
                    try:
                        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                        speaker = msg.get("name", "Agent") if isinstance(msg, dict) else "Agent"
                        if content and len(content.strip()) > 5:
                            graph.register_conversation(speaker, content, session)
                    except Exception:
                        pass  # Non-blocking
                return _graph_ingest_hook
            gc.messages.append = _make_graph_hook(_prev_append)

    return assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user, group_chat_1, manager_1, chat_instructor, visual_agent_group


def get_agent_response(assistant: autogen.AssistantAgent, chat_instructor: autogen.UserProxyAgent,
                       helper: autogen.AssistantAgent, user_proxy: autogen.UserProxyAgent,
                       manager: autogen.GroupChatManager, group_chat: autogen.GroupChat, message: str, role: str,
                       user_id: int, prompt_id: int, request_id: str) -> str:
    """Get a single response from the agent for the given message."""
    user_prompt = f'{user_id}_{prompt_id}'
    try:

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
                        # === LLM CLAIM VALIDATION: cross-reference against known state ===
                        _llm_action_id = int(json_obj.get("action_id", _reuse_current_action))
                        if _llm_action_id != _reuse_current_action:
                            current_app.logger.warning(
                                f"[HALLUCINATION?] LLM claims action_id={_llm_action_id} "
                                f"but pipeline assigned {_reuse_current_action} — using known value")
                        current_app.logger.info(f'UPDATING CURRENT ACTION AS :{_reuse_current_action}')
                        user_tasks[user_prompt].current_action = _reuse_current_action
                        action_message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action - 1)[
                            'action']
                        steps = [{x['steps']: {'tool_name': x.get('tool_name', None),
                                               'code': x.get('generalized_functions', None)}} for x in
                                 recipes[user_prompt]['actions'][user_tasks[user_prompt].current_action - 1]['recipe']]
                        user_message = f"Perform this action -> Action #{user_tasks[user_prompt].current_action}:{action_message}\n follow these steps: {steps}"
                        chat_instructor.initiate_chat(recipient=manager, message=user_message, clear_history=False,
                                                      silent=False)
                        continue
                except IndexError as e:
                    current_app.logger.info(f"COmpleted ALL ACTIONS:")
                    return ''
                except Exception:
                    try:
                        json_match = re.search(r'{[\s\S]*}', group_chat.messages[-2]["content"])
                        if json_match:
                            json_part = json_match.group(0)
                            json_obj = json.loads(json_part)
                            current_app.logger.info(f'got json object {json_obj}')
                            if json_obj['status'].lower() == 'completed':
                                # Use KNOWN action_id from scope, not LLM's claim
                                _known_action = user_tasks[user_prompt].current_action
                                _llm_claimed = int(json_obj.get("action_id", _known_action))
                                if _llm_claimed != _known_action:
                                    current_app.logger.warning(
                                        f"[HALLUCINATION?] LLM claims action_id={_llm_claimed} "
                                        f"but pipeline has {_known_action}")
                                current_app.logger.info(f'UPDATING CURRENT ACTION AS :{_known_action}')
                                user_tasks[user_prompt].current_action = _known_action
                                action_message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action - 1)['action']
                                steps = [{x['steps']: {'tool_name': x.get('tool_name', None),
                                                       'code': x.get('generalized_functions', None)}} for x in
                                         recipes[user_prompt]['actions'][user_tasks[user_prompt].current_action - 1][
                                             'recipe']]
                                user_message = f"Perform this action -> Action #{user_tasks[user_prompt].current_action}:{action_message}\n follow these steps: {steps}"
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
                        assistant.initiate_chat(recipient=manager, message=message, clear_history=False, silent=False)
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
                    helper.initiate_chat(recipient=manager, message=message, clear_history=False, silent=False)

            except Exception as e:
                current_app.logger.error(f'WE have some indexx error here: {e}')
                error_message = traceback.format_exc()  # Capture full traceback
                current_app.logger.error(f"Error in get_agent_response indexx:\n{error_message}")

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
        last_message = group_chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
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
        return f"Error getting response: {str(e)}"


def get_flow_number(user_id, prompt_id):
    role = get_role(user_id, prompt_id)
    if not role:
        role = None
    current_app.logger.info(f'Got role as {role}')
    file_path = os.path.join(PROMPTS_DIR, f'{prompt_id}.json')
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
    with open(os.path.join(PROMPTS_DIR, f"{prompt_id}_{role_number}_recipe.json"), 'r') as f:
        config = json.load(f)
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

        _sched_log('info', 'Creating Visual scheduled tasks')
        trigger = IntervalTrigger(seconds=int(2))
        job_id = f"job_{int(time.time())}"
        scheduler.add_job(call_visual_task, trigger=trigger, id=job_id,
                          args=['get past 1 mins visual information', user_id, prompt_id])
        _sched_log('info', 'Successfully created scheduler job')
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
                                # Use KNOWN action_id, not LLM's claim
                                _llm_aid = int(json_obj.get("action_id", _w2_current))
                                if _llm_aid != _w2_current:
                                    current_app.logger.warning(
                                        f"[HALLUCINATION?] LLM claims action_id={_llm_aid} "
                                        f"but pipeline has {_w2_current}")
                                current_app.logger.info(f'UPDATING CURRENT ACTION AS :{_w2_current}')
                                user_tasks[user_prompt].current_action = _w2_current
                                action_message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action - 1)['action']
                                steps = [{x['steps']: {'tool_name': x.get('tool_name', None),
                                                       'code': x.get('generalized_functions', None)}} for x in
                                         recipes[user_prompt]['actions'][user_tasks[user_prompt].current_action - 1][
                                             'recipe']]
                                user_message = f"Perform this action -> Action #{user_tasks[user_prompt].current_action}:{action_message}\n follow these steps: {steps}"
                                chat_instructor.initiate_chat(recipient=manager, message=user_message,
                                                              clear_history=False, silent=False)
                                continue
                        except Exception:
                            try:
                                json_match = re.search(r'{[\s\S]*}', group_chat.messages[-2]["content"])
                                if json_match:
                                    json_part = json_match.group(0)
                                    json_obj = json.loads(json_part)
                                    current_app.logger.info(f'got json object {json_obj}')
                                    if json_obj['status'].lower() == 'completed':
                                        # Use KNOWN action_id, not LLM's claim
                                        _llm_aid2 = int(json_obj.get("action_id", _w2_current))
                                        if _llm_aid2 != _w2_current:
                                            current_app.logger.warning(
                                                f"[HALLUCINATION?] LLM claims action_id={_llm_aid2} "
                                                f"but pipeline has {_w2_current}")
                                        current_app.logger.info(
                                            f'UPDATING CURRENT ACTION AS :{_w2_current}')
                                        user_tasks[user_prompt].current_action = _w2_current
                                        action_message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action - 1)['action']
                                        steps = [{x['steps']: {'tool_name': x.get('tool_name', None),
                                                               'code': x.get('generalized_functions', None)}} for x in
                                                 recipes[user_prompt]['actions'][
                                                     user_tasks[user_prompt].current_action - 1]['recipe']]
                                        user_message = f"Perform this action -> Action #{user_tasks[user_prompt].current_action}:{action_message}\n follow these steps: {steps}"
                                        chat_instructor.initiate_chat(recipient=manager, message=user_message,
                                                                      clear_history=False, silent=False)
                                        continue


                                else:
                                    raise ValueError('No json found')
                            except IndexError as e:
                                current_app.logger.info(f"COmpleted ALL ACTIONS:")
                                return ''
                            except Exception as e:
                                current_app.logger.warning(f'it is not a json object the error is: {e}')
                                current_app.logger.info(
                                    'it is not a json object You should ask status verifier to give response in proper format & not move ahead to next action')
                                actions_prompt = user_tasks[user_prompt].get_action(
                                    user_tasks[user_prompt].current_action - 1)
                                message = 'Hey @StatusVerifier Agent, Please verify the status of the action ' + f'{user_tasks[user_prompt].current_action}: {actions_prompt}' + '\n performed and Respond in the following format {"status": "status here","action": "current action","action_id": ' + f'{user_tasks[user_prompt].current_action}' + ',"message": "message here"}'
                                assistant.initiate_chat(recipient=manager, message=message, clear_history=False,
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
