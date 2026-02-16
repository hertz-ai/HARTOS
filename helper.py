from collections import deque
import requests
import re
import ast
import autogen

from autogen.agentchat.contrib.capabilities import transform_messages, transforms
import json
from flask import current_app
from typing import List, Dict, Tuple, Annotated, Set, FrozenSet, Any
import pickle
from PIL import Image
import uuid
from datetime import datetime, timedelta
import time
import redis
from langchain.schema import AgentAction, AgentFinish, OutputParserException, HumanMessage, AIMessage, SystemMessage
import pytz
from langchain.utilities import GoogleSearchAPIWrapper
import aiohttp
import asyncio
import os
from bs4 import BeautifulSoup
from langchain.memory import ZepMemory
from json_repair import repair_json
import traceback

# Performance: cached config loading (single read instead of 3+)
from core.config_cache import get_config as _get_config
# Performance: connection-pooled HTTP sessions
from core.http_pool import get_http_session, pooled_post, pooled_get, pooled_request
# Performance: singleton event loop
from core.event_loop import get_or_create_event_loop

config = _get_config()

os.environ["OPENAI_API_KEY"] = config.get('OPENAI_API_KEY', '')
os.environ["GOOGLE_CSE_ID"] = config.get('GOOGLE_CSE_ID', '')
os.environ["GOOGLE_API_KEY"] = config.get('GOOGLE_API_KEY', '')
os.environ["NEWS_API_KEY"] = config.get('NEWS_API_KEY', '')
os.environ["SERPAPI_API_KEY"] = config.get('SERPAPI_API_KEY', '')

ACTION_API = config.get('ACTION_API', '')
STUDENT_API = config.get('STUDENT_API', '')
ZEP_API_URL = config.get('ZEP_API_URL', '')
ZEP_API_KEY = config.get('ZEP_API_KEY', '')

search = GoogleSearchAPIWrapper(k=4)
redis_client = redis.StrictRedis(
    host='azure_all_vms.hertzai.com', port=6369, db=0)

async def fetch(session, url):
    try:
        async with session.get(url) as response:
            start_time = time.time()
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            end_time = time.time()
            elapsed_time = end_time - start_time
            print(f"time taken to crawl {url} is {elapsed_time}")
            return soup.get_text()
    except Exception as e:
        print(f"An error occurred while fetching {url}: {e}")
        return ""


async def async_main(urls):
    async with aiohttp.ClientSession() as session:
        tasks = [fetch(session, url) for url in urls]
        return await asyncio.gather(*tasks)



# Configuration for Crawl4AI API service
CRAWL4AI_API_URL = "http://localhost:8094"  # Update this to your actual service URL

# --- Path traversal protection for prompt file access ---
PROMPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), 'prompts'))


def sanitize_path_component(value):
    """Reject any value containing path separators or traversal sequences.

    Accepts alphanumeric characters, underscores, and hyphens only.
    Returns the value unchanged if safe, raises ValueError otherwise.
    """
    s = str(value)
    if not re.match(r'^[a-zA-Z0-9_\-]+$', s):
        raise ValueError(f"Invalid path component: {s!r}")
    return s


def safe_prompt_path(*parts, ext='.json'):
    """Build a path under prompts/ safely.

    Usage:
        safe_prompt_path(prompt_id)                    -> prompts/{prompt_id}.json
        safe_prompt_path(prompt_id, flow)              -> prompts/{prompt_id}_{flow}.json
        safe_prompt_path(prompt_id, flow, action)      -> prompts/{prompt_id}_{flow}_{action}.json
        safe_prompt_path(prompt_id, flow, 'recipe')    -> prompts/{prompt_id}_{flow}_recipe.json

    Raises ValueError if any component contains path traversal characters.
    """
    sanitized = [sanitize_path_component(p) for p in parts]
    filename = '_'.join(sanitized) + ext
    full = os.path.join(PROMPTS_DIR, filename)
    # Belt-and-suspenders: verify the resolved path is under PROMPTS_DIR
    if not os.path.abspath(full).startswith(PROMPTS_DIR):
        raise ValueError(f"Path escapes prompts directory: {full}")
    return full



def crawl4ai_fetch(url: str, timeout: int = 30) -> str:
    """
    Fetch content from URL using Crawl4AI API service
    Simple HTTP client - no dependency conflicts!
    """
    try:
        current_app.logger.info(f"Fetching via Crawl4AI API: {url}")

        # Prepare request
        payload = {
            "url": url,
            "word_count_threshold": 50,
            "timeout": timeout * 1000,  # Convert to milliseconds
            "bypass_cache": True,
            "wait_for": "css:body"
        }

        # Call Crawl4AI API (connection pooled)
        response = pooled_post(
            f"{CRAWL4AI_API_URL}/crawl",
            json=payload,
            timeout=timeout + 10,  # Add buffer for API processing
            headers={"Content-Type": "application/json"}
        )

        if response.status_code == 200:
            result = response.json()

            if result["success"] and result["markdown"]:
                content = result["markdown"]
                current_app.logger.info(f"Crawl4AI success: {result['word_count']} words from {url}")
                return content
            else:
                error_msg = result.get("error_message", "Unknown error")
                current_app.logger.warning(f"Crawl4AI failed for {url}: {error_msg}")
                return ""
        else:
            current_app.logger.error(f"Crawl4AI API error {response.status_code} for {url}")
            return ""

    except requests.exceptions.Timeout:
        current_app.logger.warning(f"Crawl4AI API timeout for {url}")
        return ""
    except requests.exceptions.ConnectionError:
        current_app.logger.error(f"Cannot connect to Crawl4AI API service for {url}")
        return ""
    except Exception as e:
        current_app.logger.error(f"Crawl4AI API error for {url}: {e}")
        return ""


def crawl4ai_batch_fetch(urls: List[str], max_concurrent: int = 2) -> List[str]:
    """
    Fetch multiple URLs using Crawl4AI batch API
    """
    try:
        current_app.logger.info(f"Batch fetching {len(urls)} URLs via Crawl4AI API")

        # Prepare batch request
        payload = {
            "urls": urls,
            "word_count_threshold": 50,
            "timeout": 30000,  # 30 seconds per URL
            "max_concurrent": max_concurrent
        }

        # Call Crawl4AI batch API (connection pooled)
        response = pooled_post(
            f"{CRAWL4AI_API_URL}/crawl/batch",
            json=payload,
            timeout=120,  # 2 minutes for batch processing
            headers={"Content-Type": "application/json"}
        )

        if response.status_code == 200:
            result = response.json()

            # Extract content from results
            extracted_content = []
            for crawl_result in result["results"]:
                if crawl_result["success"] and crawl_result["markdown"]:
                    extracted_content.append(crawl_result["markdown"])
                else:
                    extracted_content.append("")  # Empty string for failed URLs

            current_app.logger.info(
                f"Batch crawl completed: {result['successful_crawls']}/{result['total_urls']} successful")
            return extracted_content
        else:
            current_app.logger.error(f"Crawl4AI batch API error {response.status_code}")
            return [""] * len(urls)  # Return empty strings for all URLs

    except Exception as e:
        current_app.logger.error(f"Crawl4AI batch API error: {e}")
        return [""] * len(urls)  # Return empty strings for all URLs


def fallback_fetch(url: str) -> str:
    """
    Fallback fetch using requests + BeautifulSoup (your original method)
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = pooled_get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')

        # Remove unwanted elements
        for element in soup(["script", "style", "nav", "header", "footer"]):
            element.decompose()

        text = soup.get_text()
        # Clean text
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        cleaned_text = ' '.join(chunk for chunk in chunks if chunk)

        return cleaned_text
    except Exception as e:
        current_app.logger.error(f"Fallback fetch failed for {url}: {e}")
        return ""


def check_crawl4ai_service() -> bool:
    """
    Check if Crawl4AI API service is available
    """
    try:
        response = pooled_get(f"{CRAWL4AI_API_URL}/health", timeout=5)
        return response.status_code == 200
    except:
        return False


def top5_results(query):
    """
    Enhanced top5_results using Crawl4AI API service
    Maintains the same interface as your original function
    """
    current_app.logger.info(f"Enhanced search for: {query}")

    final_res = []

    try:
        # Your existing Google search
        top_2_search_res = search.results(query, 2)
        top_2_search_res_link = [res['link'] for res in top_2_search_res]

        if not top_2_search_res_link:
            current_app.logger.warning("No search links found")
            return search.results(query, 4)

        current_app.logger.info(f"Processing {len(top_2_search_res_link)} URLs")

        # Check if Crawl4AI service is available
        if check_crawl4ai_service():
            current_app.logger.info("Using Crawl4AI API service")

            # Use batch API for better performance
            extracted_content = crawl4ai_batch_fetch(top_2_search_res_link, max_concurrent=2)

            # Process results
            processed_texts = []
            for i, content in enumerate(extracted_content):
                if content and len(content.strip()) > 100:
                    # Clean and truncate content
                    cleaned_text = re.sub(r'\s+', ' ', content.strip())

                    if len(cleaned_text) > 4000:
                        # Try to break at sentence boundary
                        truncate_pos = cleaned_text.rfind('.', 0, 4000)
                        if truncate_pos > 3000:
                            cleaned_text = cleaned_text[:truncate_pos + 1] + " [Content truncated]"
                        else:
                            cleaned_text = cleaned_text[:4000] + "..."

                    processed_texts.append(cleaned_text)
                    current_app.logger.info(f"Processed {len(cleaned_text)} chars from {top_2_search_res_link[i]}")

            if processed_texts:
                combined_text = " ".join(processed_texts)

                result = {
                    'text': combined_text,
                    'source': top_2_search_res_link,
                    'enhanced': True,
                    'method': 'crawl4ai_api',
                    'word_count': len(combined_text.split()),
                    'sources_processed': len(processed_texts)
                }

                final_res.append(result)
                current_app.logger.info(
                    f"Crawl4AI API success: {len(combined_text)} chars from {len(processed_texts)} sources")
            else:
                raise Exception("No content extracted via Crawl4AI API")

        else:
            current_app.logger.warning("Crawl4AI API service unavailable, using fallback")
            raise Exception("Crawl4AI service unavailable")

    except Exception as e:
        current_app.logger.warning(f"Crawl4AI API method failed: {e}, trying fallback")

        # Fallback to requests + BeautifulSoup
        try:
            processed_texts = []

            for i, url in enumerate(top_2_search_res_link):
                current_app.logger.info(f"Fallback processing URL {i + 1}: {url}")

                content = fallback_fetch(url)

                if content and len(content.strip()) > 100:
                    cleaned_text = re.sub(r'\s+', ' ', content.strip())

                    if len(cleaned_text) > 3000:
                        cleaned_text = cleaned_text[:3000] + "..."

                    processed_texts.append(cleaned_text)
                    current_app.logger.info(f"Fallback extracted {len(cleaned_text)} chars from {url}")

                # Small delay between requests
                time.sleep(0.5)

            if processed_texts:
                combined_text = " ".join(processed_texts)

                result = {
                    'text': combined_text,
                    'source': top_2_search_res_link,
                    'enhanced': True,
                    'method': 'fallback_requests',
                    'word_count': len(combined_text.split()),
                    'sources_processed': len(processed_texts)
                }

                final_res.append(result)
                current_app.logger.info(
                    f"Fallback success: {len(combined_text)} chars from {len(processed_texts)} sources")
            else:
                raise Exception("Fallback method also failed")

        except Exception as fallback_error:
            current_app.logger.error(f"All methods failed: {fallback_error}")

            # Final fallback to your original async method if it exists
            try:
                text = asyncio.run(async_main(top_2_search_res_link))
                cleaned_text = re.sub(r'[^\w\s]', '', text[0] + " " + text[1])
                cleaned_text = re.sub(r'\n+', '\n', cleaned_text).strip()

                if cleaned_text:
                    final_res.append({'text': cleaned_text, 'source': top_2_search_res_link})
                    current_app.logger.info("Original async method fallback successful")

            except Exception as async_error:
                current_app.logger.error(f"Original async method also failed: {async_error}")

    # Your original final fallback
    if len(final_res) == 0:
        current_app.logger.info("All methods failed, using Google API results")
        return search.results(query, 4)

    current_app.logger.info(f"Returning {len(final_res)} results")
    return final_res

def parse_user_id(user_id:int):
    url = 'http://azurekong.hertzai.com:8000/db/getstudent_by_user_id'

    headers = {
        'Content-Type': 'application/json'
    }

    payload = json.dumps({
        "user_id": user_id
    })

    response = pooled_request("POST", url, headers=headers, data=payload)
    return response.text

def topological_sort(actions):
    # Create adjacency list and in-degree dictionary
    adj_list = {action["action_id"]: [] for action in actions}
    in_degree = {action["action_id"]: 0 for action in actions}
    action_map = {action["action_id"]: action for action in actions}  # Map ID to full action
    current_app.logger.info(f'got the actions in topological function')
    current_app.logger.info(f'the actions in topological function: - \n {actions}')
    # Build the graph
    for action in actions:

        if action["actions_this_action_depends_on"]:  # Ensure it's not None
            for dep in action["actions_this_action_depends_on"]:
                if dep != action["action_id"]:  # Ignore self-dependency
                    adj_list[dep].append(action["action_id"])
                    in_degree[action["action_id"]] += 1

    # Initialize queue with actions having in-degree 0 (no dependencies)
    queue = deque([aid for aid in in_degree if in_degree[aid] == 0])

    sorted_actions = []
    processed_count = 0  # Track number of processed actions

    while queue:
        aid = queue.popleft()
        sorted_actions.append(action_map[aid])  # Append action to sorted list
        processed_count += 1

        # Reduce in-degree of dependent actions
        for neighbor in adj_list[aid]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    # If processed actions are less than total actions, a cycle exists
    if processed_count != len(actions):
        # Find the actions still having in-degree > 0 (part of cycle)
        cyclic_actions = [aid for aid in in_degree if in_degree[aid] > 0]
        print("Cyclic dependency detected! The following actions are involved in a cycle:")
        cyclic_ids = []
        for aid in cyclic_actions:
            cyclic_ids.append(action_map[aid]['action_id'])  # Print full action details
        print(cyclic_ids)
        return False, None, cyclic_ids

    return True, sorted_actions, None

def fix_actions(array_of_actions,cyclic_ids):
    url = "http://aws_rasa.hertzai.com:5459/gpt3"
    text = f"""From the Below json array of action we are getting cyclic dependency. the action_ids which are creating the cyclic dependecy are {cyclic_ids}.
            You can Refer the below array of actions \n{array_of_actions}\n and return the corrected action dependency without cyclic dependency.
            complete json array without cyclic dependency, RESPONSE FORMAT: e.g. [{{"action_id":"An integer action_id","actions_this_action_depends_on":[]}}]
            IMPORTANT INSTRUCTIONS: Do not add any unnecessary hallucinated dependencies in actions
            Output array:"""
    payload = json.dumps({
    "text": text,
    "model": "3",
    "temperature": 0,
    "max_tokens": 3000,
    "top_p": 1,
    "frequency_penalty": 0
    })
    headers = {
    'Content-Type': 'application/json'
    }
    try:
        response = pooled_post(url, headers=headers, data=payload)
        response = response.json()
        print(response)
        x = ast.literal_eval(response['text'])
        print(f'got json object')
        return x
    except Exception as e:
        print(f'GOT ERROR WHILE JSON FIX:{e}')
        return None


def gpt_call(prompt):
    url = "http://aws_rasa.hertzai.com:5459/gpt3"
    text = prompt
    payload = json.dumps({
    "text": text,
    "model": "3",
    "temperature": 0,
    "max_tokens": 3000,
    "top_p": 1,
    "frequency_penalty": 0
    })
    headers = {
    'Content-Type': 'application/json'
    }
    try:
        response = pooled_post(url, headers=headers, data=payload)
        response = response.json()
        print(response)
        return response['text']
    except Exception as e:
        print(f'GOT ERROR WHILE JSON FIX:{e}')
        return None

def gpt_mini(prompt,request_id,history):
    url = "http://aws_rasa.hertzai.com:5459/gpt-json"
    prompt = f'{prompt} conside below as history {history}'
    response = pooled_post(
        url,
        json={
            "model": "gpt-4o",
            "data": [{"role": "user", "content": prompt}],
            "max_token": 1000,
            "request_id": request_id
        })
    print(f"gpt 4o-mini response is {response}")
    print(f"gpt 4o-mini response is {response.json()}")
    return response.json()["text"]


def strip_json_values(obj: Any) -> Any:
    """
    Recursively walk obj.
    - If dict: recurse on each value, preserving keys.
    - If list/tuple: recurse on each element, preserving order & type.
    - Otherwise (leaf): return redacted marker.
    """
    #current_app.logger.info(f"GOT JSON FOR STRIPPING: {obj}")
    # 1. Dig into dict
    if isinstance(obj, dict):
        return { key: strip_json_values(val) for key, val in obj.items() }

    # 2. Dig into list or tuple
    elif isinstance(obj, list):
        return [ strip_json_values(item) for item in obj ]
    elif isinstance(obj, tuple):
        return tuple(strip_json_values(item) for item in obj)

    # 3. Optional: if you know some strings actually contain JSON and you want to descend into them,
    #    uncomment this block.
    elif isinstance(obj, str):
        try:
            parsed = json.loads(obj)
        except json.JSONDecodeError:
            pass
        else:
            return strip_json_values(parsed)

    # 4. Everything else is a true leaf → redact it
    else:
        return f"redacted {type(obj).__name__}"


def fix_json(json_text):
    url = "http://aws_rasa.hertzai.com:5459/gpt3"
    text = """You are an expert JSON fixer. Your task is to correct a given JSON string, ensuring it is compatible with Python’s `eval()`.

    ### Instructions:
    1. **Fix Formatting Issues:**
    - Convert single quotes (`'`) to double quotes (`"`) where necessary (except inside stringified JSON).
    - Ensure correct placement of commas, brackets, and braces.
    - Fix missing or extra quotes.
    - Properly escape special characters like newlines (`\n`).

    2. **Convert JSON to Python-Compatible Format:**
    - Ensure `true`, `false`, and `null` are replaced with `True`, `False`, and `None`.
    - If the JSON contains a string representation of a dictionary inside a field (e.g., `'{"key": "value"}'`), ensure it remains correctly formatted.

    3. **Preserve Key-Value Data:**
    - Do not change any key names or values, only correct formatting.

    4. **Output Only the Fixed JSON:**
    - Provide only the corrected JSON without explanations or extra text.

    ### Input JSON: """+f"{json_text}"+"""
    Output Json:
    """
    payload = json.dumps({
    "text": text,
    "model": "3",
    "temperature": 0,
    "max_tokens": 3000,
    "top_p": 1,
    "frequency_penalty": 0
    })
    headers = {
    'Content-Type': 'application/json'
    }
    try:
        response = pooled_post(url, headers=headers, data=payload)
        response = response.json()
        x = ast.literal_eval(response['text'])
        current_app.logger.info(f'got json object')
        return x
    except Exception as e:
        current_app.logger.info(f'GOT ERROR WHILE JSON FIX:{e}')
        return None


def retrieve_json(json_message):
    json_obj = None

    # First, try to extract just the JSON part (without the @user prefix)
    if '@user' in json_message:
        # Find everything after @user
        prefix_match = re.search(r'@user\s*(.*)', json_message, re.DOTALL)
        if prefix_match:
            json_message = prefix_match.group(1).strip()

    try:
        return json.loads(repair_json(json_message))
    except Exception as e:
        current_app.logger.info(f'json_repair failed: {e}')

    json_message = json_message.replace(''', "'").replace(''', "'").replace('"', '"').replace('"', '"')

    # Try using ast.literal_eval which can handle Python dict syntax with single quotes
    try:
        json_obj = ast.literal_eval(json_message)
        current_app.logger.info('got json object using ast.literal_eval')
        return json_obj
    except Exception as e:
        current_app.logger.info(f'ast.literal_eval failed: {e}')
        json_obj = None

    # Fall back to regex + json.loads approach with more careful quote handling
    try:
        json_match = re.search(r'{[\s\S]*}', json_message)
        if json_match:
            json_part = json_match.group(0)

            # A more careful approach to handle quotes correctly
            # This only replaces outer quotes, not quotes within the content
            processed_json = re.sub(r"'([^']+)':", r'"\1":', json_part)  # Fix keys
            # Now handle the string values, being careful about nested quotes
            processed_json = re.sub(r':\s*\'([^\']*)\'', r': "\1"', processed_json)

            json_obj = json.loads(processed_json)
            current_app.logger.info('got json object')
            return json_obj
        return None
    except Exception as e:
        current_app.logger.info(f'json processing failed: {e}')
        json_obj = fix_json(json_message)
        return json_obj


class ToolMessageHandler:
    """Handles tool messages in the conversation history to prevent tool_call_id errors.

    This implementation maintains proper message structure for OpenAI API requirements,
    fixing historical inconsistencies while allowing active tool calls to be processed
    naturally by the framework. handles references between assistant tool calls
    and tool responses to prevent "Invalid parameter: 'tool_call_id' not found" errors.
    It also handles the "only messages with role 'assistant' can have a function call" error.
    """

    def __init__(self, user_tasks=None, user_prompt=None):
        """
        Initialize the ToolMessageHandler.

        Args:
            user_tasks: Global user_tasks dictionary containing session data
            user_prompt: Current session identifier (e.g., "10077_123")
        """
        self.user_tasks = user_tasks
        self.user_prompt = user_prompt

    def get_current_action_id(self):
        """Get current action ID from user_tasks."""
        if not self.user_tasks or not self.user_prompt:
            return None

        try:
            if self.user_prompt in self.user_tasks:
                current_action_id = self.user_tasks[self.user_prompt].current_action
                current_app.logger.info(
                    f"Retrieved current_action_id: {current_action_id} for session: {self.user_prompt}")
                return current_action_id
        except Exception as e:
            current_app.logger.error(f"Error getting current_action_id from user_tasks: {e}")

        return None

    def validate_messages(self, messages: List[Dict]) -> List[Dict]:
        for i, msg in enumerate(messages):
            if 'content' in msg and msg['content'] is None:
                # Log detailed information about the problematic message
                current_app.logger.warning(f"NULL CONTENT DETECTED: Message at index {i} has null content")
                current_app.logger.warning(
                    f"Message type: {msg.get('role', 'unknown')}, name: {msg.get('name', 'unknown')}")

                # Log additional message properties to help debugging
                tool_calls = "Yes" if "tool_calls" in msg else "No"
                function_call = "Yes" if "function_call" in msg else "No"
                current_app.logger.warning(f"Has tool_calls: {tool_calls}, Has function_call: {function_call}")

                # Log message context (previous message if available)
                if i > 0 and i < len(messages):
                    prev_msg = messages[i - 1]
                    current_app.logger.warning(
                        f"Previous message: role={prev_msg.get('role')}, type={prev_msg.get('type')}")

                # Replace null with empty string
                messages[i]['content'] = ""
                current_app.logger.info(f"FIXED: Replaced null content with empty string in message {i}")

        return messages

    def remove_orphan_tool_messages(self, messages):
        # 1. Collect every tool-call id that appears in an assistant message
        valid_tool_call_ids = {
            tc["id"]
            for msg in messages
            if msg.get("role") == "assistant" and "tool_calls" in msg
            for tc in msg["tool_calls"]
            if "id" in tc
        }

        # Helper ── does this consolidated reply reference at least one valid id?
        def consolidated_has_valid_id(msg) -> bool:
            """Return True when a consolidated tool message carries
            a tool_call_id that belongs to some earlier assistant message."""

            nested_ids = self.get_tool_call_ids_from_consolidated(msg)
            return any(tcid in valid_tool_call_ids for tcid in nested_ids)

        cleaned: list[dict] = []
        for msg in messages:
            if msg.get("role") == "tool":
                tcid = msg.get("tool_call_id")

                # ── ordinary single-tool reply ───────────────────────────────
                if tcid is not None:
                    if tcid not in valid_tool_call_ids:
                        current_app.logger.warning(
                            f"Dropping orphan tool message with tool_call_id={tcid}"
                        )
                        continue

                # ── consolidated reply (no top-level tool_call_id) ──────────
                elif self.is_consolidated_response(msg) and not consolidated_has_valid_id(msg):
                    current_app.logger.warning(
                        "Dropping orphan consolidated tool message (no matching IDs)"
                    )
                    continue

            cleaned.append(msg)

        return cleaned

    def is_consolidated_response(self, message):
        """Improved method to detect consolidated tool responses."""
        # Check for standard consolidated response format
        if (message.get('role') == 'tool' and
                'tool_responses' in message and
                isinstance(message['tool_responses'], list) and
                len(message['tool_responses']) > 1):
            return True

        # Also check for multiple tool_call_ids in a single message (alternative format)
        if message.get('role') == 'tool' and 'tool_call_ids' in message and isinstance(message['tool_call_ids'], list):
            return True

        return False

    def get_tool_call_ids_from_consolidated(self, message):
        """Extract all tool call IDs from a consolidated response."""
        if not self.is_consolidated_response(message):
            return []

        tool_call_ids = []

        # Check for direct tool_call_ids array
        if 'tool_call_ids' in message and isinstance(message['tool_call_ids'], list):
            tool_call_ids.extend(message['tool_call_ids'])

        # Check for main message tool_call_id
        if 'tool_call_id' in message:
            tool_call_ids.append(message['tool_call_id'])

        # Extract IDs from each tool response in the array
        if 'tool_responses' in message and isinstance(message['tool_responses'], list):
            for tool_response in message['tool_responses']:
                if 'tool_call_id' in tool_response:
                    tool_call_ids.append(tool_response['tool_call_id'])

        # Ensure unique IDs only
        return list(set(tool_call_ids))

    def find_assistant_for_tool_call_ids(self, messages, tool_call_ids):
        """Find the assistant message that generated all of the specified tool call IDs.

        Returns the index of the assistant message in messages, or None if not found.
        """
        # Reverse the messages to find the most recent matching assistant first
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if msg.get('role') == 'assistant' and 'tool_calls' in msg:
                # Get all tool call IDs from this assistant
                assistant_ids = {tc['id'] for tc in msg['tool_calls'] if 'id' in tc}

                # Check if all of the requested IDs are in this assistant message
                if all(tc_id in assistant_ids for tc_id in tool_call_ids):
                    return i

        return None

    def validate_consolidated_response(self, message):
        """Validate and potentially fix consolidated response structure."""
        if not self.is_consolidated_response(message):
            return message

        tool_call_ids = self.get_tool_call_ids_from_consolidated(message)

        # Ensure we have a valid structure
        fixed_message = message.copy()

        # If using tool_call_ids format, make sure content is appropriate
        if 'tool_call_ids' in fixed_message and isinstance(fixed_message['tool_call_ids'], list):
            if 'content' not in fixed_message or not fixed_message['content']:
                current_app.logger.warning(
                    "Consolidated response with tool_call_ids has no content. Adding placeholder.")
                fixed_message['content'] = json.dumps({"consolidated_result": "Multiple tools executed"})

        # If using tool_responses format, ensure each response has proper structure
        if 'tool_responses' in fixed_message and isinstance(fixed_message['tool_responses'], list):
            for i, response in enumerate(fixed_message['tool_responses']):
                if 'tool_call_id' not in response:
                    current_app.logger.warning(f"Tool response at index {i} missing tool_call_id. Skipping.")
                    continue

                if 'content' not in response or response['content'] is None:
                    current_app.logger.warning(
                        f"Tool response for {response['tool_call_id']} has null content. Adding empty string.")
                    fixed_message['tool_responses'][i]['content'] = ""

        return fixed_message

    def remove_recipe_prompt_messages(self, messages):
        """
        Remove messages starting with 'Focus on the current task at hand and create a detailed recipe'
        if the last message is 'Execute action'. Only removes from older messages, preserving the
        last two messages regardless of content.
        """
        if len(messages) < 3:  # Need at least 3 messages to have something to remove
            return messages

        # Check if the last message contains "Execute action" pattern
        last_message = messages[-1]
        last_content = last_message.get('content', '')

        # Use regex to match "Execute Action" followed by optional number and colon
        execute_action_pattern = r'execute\s+action\s*\d*\s*:?'
        if not re.search(execute_action_pattern, last_content, re.IGNORECASE):
            return messages

        current_app.logger.info(
            "Last message contains 'Execute action' - checking older messages for recipe prompts to remove")

        # Split messages: process older messages, preserve last 2
        messages_to_process = messages[:-2]  # All except last 2
        last_two_messages = messages[-2:]  # Last 2 messages (always preserved)

        cleaned_older_messages = []
        removed_count = 0

        for i, msg in enumerate(messages_to_process):
            should_remove = False

            if 'content' in msg and isinstance(msg['content'], str):
                # Check if message starts with the recipe prompt pattern
                content = msg['content'].strip()
                if content.startswith('Focus on the current task at hand and create a detailed recipe that includes'):
                    should_remove = True
                    removed_count += 1
                    current_app.logger.info(f"Removing recipe prompt message at index {i}: {content[:100]}...")

            if not should_remove:
                cleaned_older_messages.append(msg)

        if removed_count > 0:
            current_app.logger.info(
                f"Removed {removed_count} recipe prompt messages from older conversation history (preserved last 2 messages)")

        # Combine cleaned older messages with preserved last 2 messages
        return cleaned_older_messages + last_two_messages

    def truncate_content(self, content, max_words=10):
        """Truncate content to specified number of words for logging purposes."""
        if not isinstance(content, str):
            return content

        words = content.split()
        if len(words) <= max_words:
            return content

        truncated = ' '.join(words[:max_words])
        return f"{truncated}... [truncated from {len(words)} words]"

    def create_log_safe_message(self, msg, max_words=10):
        """Create a log-safe version of message with truncated content."""
        log_msg = msg.copy()

        # Truncate main content
        if 'content' in log_msg and log_msg['content']:
            log_msg['content'] = self.truncate_content(log_msg['content'], max_words)

        # Truncate tool_responses content if present
        if 'tool_responses' in log_msg and isinstance(log_msg['tool_responses'], list):
            for i, response in enumerate(log_msg['tool_responses']):
                if 'content' in response and response['content']:
                    log_msg['tool_responses'][i] = response.copy()
                    log_msg['tool_responses'][i]['content'] = self.truncate_content(
                        response['content'], max_words
                    )

        # Truncate tool_calls arguments if they're very large
        if 'tool_calls' in log_msg and isinstance(log_msg['tool_calls'], list):
            for i, tool_call in enumerate(log_msg['tool_calls']):
                if ('function' in tool_call and
                        'arguments' in tool_call['function'] and
                        len(str(tool_call['function']['arguments'])) > 200):
                    log_msg['tool_calls'][i] = tool_call.copy()
                    log_msg['tool_calls'][i]['function'] = tool_call['function'].copy()
                    log_msg['tool_calls'][i]['function']['arguments'] = (
                            str(tool_call['function']['arguments'])[:1000] + "... [truncated]"
                    )

        return log_msg

    def compress_action_messages(self, messages, current_action_id=None):
        """
        Compress 'Execute Action X' messages to 'Action X' for older messages.
        Only applies to messages except the last 2, and only for action IDs less than current_action_id.

        Args:
            messages: List of message dictionaries
            current_action_id: Current action ID (int). If None, will try to detect from recent messages.

        Returns:
            List of messages with compressed action references
        """
        if len(messages) <= 2:
            return messages

        # Auto-detect current action ID if not provided
        if current_action_id is None:
            current_action_id = self._detect_current_action_id(messages)

        # Process all messages except last 2
        messages_to_process = messages[:-2]
        recent_messages = messages[-2:]

        compressed_messages = []

        for msg in messages_to_process:
            if 'content' in msg and isinstance(msg['content'], str):
                compressed_content = self._compress_execute_action_text(
                    msg['content'],
                    current_action_id
                )

                if compressed_content != msg['content']:
                    # Create a copy with compressed content
                    compressed_msg = msg.copy()
                    compressed_msg['content'] = compressed_content
                    compressed_messages.append(compressed_msg)
                    current_app.logger.info(
                        f"Compressed action message: '{msg['content'][:50]}...' -> '{compressed_content[:50]}...'")
                else:
                    compressed_messages.append(msg)
            else:
                compressed_messages.append(msg)

        # Combine compressed messages with recent unmodified messages
        return compressed_messages + recent_messages

    def _detect_current_action_id(self, messages):
        """
        Try to detect the current action ID from recent messages.
        Looks for patterns like 'Execute Action X' or 'Action X' in recent messages.
        """
        # Check last few messages for action patterns
        action_pattern = r'(?:Execute\s+)?Action\s+(\d+)'

        for msg in reversed(messages[-5:]):  # Check last 5 messages
            if 'content' in msg and isinstance(msg['content'], str):
                matches = re.findall(action_pattern, msg['content'], re.IGNORECASE)
                if matches:
                    try:
                        return int(matches[-1])  # Return the last (most recent) action ID found
                    except ValueError:
                        continue

        return None  # Couldn't detect current action ID

    def _compress_execute_action_text(self, content, current_action_id):
        """
        Replace 'Execute Action X' with 'Action X' for action IDs less than current_action_id.

        Args:
            content: Message content string
            current_action_id: Current action ID (int or None)

        Returns:
            String with compressed action references
        """
        if not current_action_id:
            return content

        # Pattern to match "Execute Action X" where X is a number
        pattern = r'Execute\s+Action\s+(\d+)'

        def replace_if_older(match):
            action_id_str = match.group(1)
            try:
                action_id = int(action_id_str)
                if action_id < current_action_id:
                    return f"Action {action_id_str}"
                else:
                    return match.group(0)  # Keep original if not older
            except ValueError:
                return match.group(0)  # Keep original if not a valid number

        return re.sub(pattern, replace_if_older, content, flags=re.IGNORECASE)

    def apply_transform(self, messages: List[Dict]) -> List[Dict]:
        """Applies the tool message handling transformation to ensure valid tool call/response pairings."""
        if not messages:
            current_app.logger.info("ToolMessageHandler: No messages to process")
            return messages
        # Get current action ID from user_tasks
        current_action_id = self.get_current_action_id()

        """Removes the done status to remove ambiguity for agent to reinforce current action completion without just giving status done"""
        messages = self.remove_recipe_prompt_messages(messages)

        """Removes the word Execute for historical actions and not for current action"""
        messages = self.compress_action_messages(messages, current_action_id)

        current_app.logger.info(f"ToolMessageHandler: Processing {len(messages)} messages")
        # DEBUGGING: Print the entire conversation structure with full message details
        current_app.logger.info(f"=== FULL INPUT MESSAGES DEBUG ===")
        for i, msg in enumerate(messages):
            log_safe_msg = self.create_log_safe_message(msg, max_words=70)
            current_app.logger.info(f"Message[{i}]: {json.dumps(log_safe_msg, indent=2)}")
        current_app.logger.info(f"=== END FULL INPUT MESSAGES DEBUG ===")

        # DEBUGGING: Print the entire conversation structure
        current_app.logger.info(f"=== CONVERSATION STRUCTURE DEBUG ===")
        for i, msg in enumerate(messages):
            role = msg.get('role', 'unknown')
            name = msg.get('name', 'unknown')
            tool_calls_info = f", tool_calls=[{','.join([tc.get('id') for tc in msg.get('tool_calls', []) if 'id' in tc])}]" if 'tool_calls' in msg else ""
            tool_call_id_info = f", tool_call_id={msg.get('tool_call_id')}" if 'tool_call_id' in msg else ""

            debug_info = f"Message[{i}]: role={role}, name={name}{tool_calls_info}{tool_call_id_info}"
            current_app.logger.info(debug_info)
        current_app.logger.info(f"=== END CONVERSATION STRUCTURE ===")

        processed_messages = messages.copy()

        # STEP 1: Handle first message if it's a tool message (special case)
        if processed_messages and processed_messages[0].get('role') == 'tool':
            current_app.logger.info('GOT TOOL AS FIRST MESSAGE CHANGING IT')
            processed_messages[0]['role'] = 'user'
            processed_messages[0]['name'] = 'Helper'
            if 'tool_call_id' in processed_messages[0]:
                del processed_messages[0]['tool_call_id']
            processed_messages = processed_messages[1:]

        # STEP 2: Pre-identify consolidated responses and assistants with tool calls
        final_messages = []
        tool_call_mapping = {}  # Maps tool_call_id -> assistant_idx
        pending_tool_calls = []  # Track tool calls that need responses
        assistant_tool_calls = {}  # Track tool calls grouped by assistant message index
        consolidated_responses = []  # Track consolidated responses for later processing

        # First sweep: Identify consolidated responses to prevent them from being processed as regular tool messages
        for i, msg in enumerate(processed_messages):
            if msg.get('role') == 'tool' and self.is_consolidated_response(msg):
                consolidated_responses.append((i, msg))
                # Mark this message to be skipped in the main processing
                processed_messages[i] = {"__skip__": True, "original_index": i}
                current_app.logger.info(f"Marked consolidated response at index {i} for special handling")
            # Also identify all assistant messages with tool calls for later reference
            elif msg.get('role') == 'assistant' and 'tool_calls' in msg:
                for tool_call in msg.get('tool_calls', []):
                    if 'id' in tool_call:
                        tool_call_id = tool_call['id']
                        tool_call_mapping[tool_call_id] = i
                        # We'll populate assistant_tool_calls in the main pass

        # Main pass: Process regular messages, skipping marked consolidated responses
        for i, msg in enumerate(processed_messages):
            # Skip messages marked for special handling
            if isinstance(msg, dict) and "__skip__" in msg:
                continue

            # Track assistant messages with tool calls
            if msg.get('role') == 'assistant' and 'tool_calls' in msg:
                assistant_idx = len(final_messages)
                assistant_tool_calls[assistant_idx] = []

                # Register all tool call IDs from this assistant message
                for tool_call in msg.get('tool_calls', []):
                    if 'id' in tool_call:
                        tool_call_id = tool_call['id']
                        tool_call_mapping[tool_call_id] = assistant_idx
                        pending_tool_calls.append(tool_call_id)  # Add to pending list
                        assistant_tool_calls[assistant_idx].append(tool_call_id)
                        current_app.logger.info(
                            f"Registered tool_call_id {tool_call_id} at assistant index {assistant_idx}")

                final_messages.append(msg)

            # Handle tool messages - ensure they have proper tool_call_id
            elif msg.get('role') == 'tool':
                # If this tool message has a tool_call_id
                if 'tool_call_id' in msg:
                    tool_call_id = msg.get('tool_call_id')

                    # Check if this tool_call_id exists in our mapping
                    if tool_call_id in tool_call_mapping:
                        # If this tool call ID is in our pending list, remove it
                        if tool_call_id in pending_tool_calls:
                            pending_tool_calls.remove(tool_call_id)  # Mark as responded

                        current_app.logger.info(f"Valid tool message at index {i} with tool_call_id {tool_call_id}")
                        final_messages.append(msg)
                    else:
                        # No matching tool_call_id found - convert to user message
                        current_app.logger.warning(f"Tool message with invalid tool_call_id - converting to user")
                        final_messages.append({
                            'role': 'user',
                            'name': 'Helper',
                            'content': msg.get('content', '')
                        })
                else:
                    # Tool message without tool_call_id
                    # Check if it directly follows an assistant message with tool calls
                    if len(final_messages) > 0 and final_messages[-1].get('role') == 'assistant' and 'tool_calls' in \
                            final_messages[-1]:
                        last_assistant_idx = len(final_messages) - 1

                        # Get all pending tool calls from the previous assistant message
                        tool_calls_for_assistant = [tc_id for tc_id in assistant_tool_calls.get(last_assistant_idx, [])
                                                    if tc_id in pending_tool_calls]

                        if len(tool_calls_for_assistant) == 1:
                            # If only one pending tool call, assign it directly
                            tool_call_id = tool_calls_for_assistant[0]
                            current_app.logger.info(f"Adding missing tool_call_id {tool_call_id} to tool message")

                            tool_msg = msg.copy()
                            tool_msg['tool_call_id'] = tool_call_id
                            pending_tool_calls.remove(tool_call_id)  # Mark as responded
                            final_messages.append(tool_msg)

                        elif len(tool_calls_for_assistant) > 1:
                            # Avoid adding duplicate tool responses for the same call IDs
                            # Insert each tool message directly after its matching assistant
                            inserted_count = 0
                            for tool_call_id in tool_calls_for_assistant:
                                if any(m.get("tool_call_id") == tool_call_id and m.get("role") == "tool" for m in
                                       final_messages):
                                    current_app.logger.info(
                                        f"Tool response for {tool_call_id} already exists. Skipping.")
                                    continue

                                assistant_idx = tool_call_mapping.get(tool_call_id)
                                if assistant_idx is None:
                                    current_app.logger.warning(
                                        f"No assistant found for tool_call_id {tool_call_id}. Skipping.")
                                    continue

                                # Find the real index of assistant in final_messages
                                actual_assistant_index = None
                                for j in range(len(final_messages) - 1, -1, -1):
                                    if final_messages[j].get("role") == "assistant" and tool_call_id in [
                                        tc["id"] for tc in final_messages[j].get("tool_calls", []) if "id" in tc
                                    ]:
                                        actual_assistant_index = j
                                        break

                                if actual_assistant_index is None:
                                    current_app.logger.warning(
                                        f"Could not locate assistant message for tool_call_id {tool_call_id}. Skipping.")
                                    continue

                                tool_msg = msg.copy()
                                tool_msg["tool_call_id"] = tool_call_id
                                final_messages.insert(actual_assistant_index + 1 + inserted_count, tool_msg)
                                inserted_count += 1
                                pending_tool_calls.remove(tool_call_id)
                                current_app.logger.info(
                                    f"Inserted tool response for {tool_call_id} after assistant[{actual_assistant_index}]")
                        else:
                            # No pending tool calls for this assistant message
                            current_app.logger.warning(
                                f"Tool message without tool_call_id and no pending calls - converting to user")
                            final_messages.append({
                                'role': 'user',
                                'name': 'Helper',
                                'content': msg.get('content', '')
                            })
                    else:
                        # Tool message without tool_call_id and not following an assistant with tool calls
                        current_app.logger.warning(
                            f"Tool message without tool_call_id and no preceding assistant - converting to user")
                        final_messages.append({
                            'role': 'user',
                            'name': 'Helper',
                            'content': msg.get('content', '')
                        })
            else:
                # For all other message types
                final_messages.append(msg)

        # STEP 3: Process consolidated responses with improved handling
        current_app.logger.info(f"Processing {len(consolidated_responses)} consolidated responses")

        # ────────────────────────────────────────────────────────────────
        #  remember which consolidated-ID sets we have already accepted
        # ────────────────────────────────────────────────────────────────
        seen_consolidated_id_sets: set[frozenset[str]] = set()

        for orig_idx, consolidated_msg in consolidated_responses:
            # Validate and fix the consolidated response structure
            fixed_consolidated = self.validate_consolidated_response(consolidated_msg)

            # Get all tool call IDs from this consolidated response
            tool_call_ids = self.get_tool_call_ids_from_consolidated(fixed_consolidated)

            if not tool_call_ids:
                current_app.logger.warning(
                    f"Consolidated response at original index {orig_idx} "
                    f"has no valid tool_call_ids. Converting to user message.")
                final_messages.append({
                    'role': 'user',
                    'name': 'Helper',
                    'content': fixed_consolidated.get('content', '')
                })
                continue

            # ─── Duplicate guard ───────────────────────────────────────
            id_set = frozenset(tool_call_ids)
            if id_set in seen_consolidated_id_sets:
                current_app.logger.info(
                    "Duplicate consolidated response detected – skipping second copy"
                )
                continue
            seen_consolidated_id_sets.add(id_set)
            # ───────────────────────────────────────────────────────────

            current_app.logger.info(f"Processing consolidated response with tool_call_ids: {tool_call_ids}")

            # First try to find the most likely assistant index from our tool_call_mapping
            most_likely_assistant_idx = None

            # Map each tool_call_id to its assistant original index
            assistant_indices = []
            for tc_id in tool_call_ids:
                if tc_id in tool_call_mapping:
                    assistant_indices.append(tool_call_mapping[tc_id])

            # If we have assistant indices, find the most common one (mode)
            if assistant_indices:
                # Simple mode calculation (most frequent value)
                index_counts = {}
                for idx in assistant_indices:
                    if idx not in index_counts:
                        index_counts[idx] = 0
                    index_counts[idx] += 1

                most_likely_assistant_orig_idx = max(index_counts, key=index_counts.get)

                # Now find this assistant in our final_messages
                for i, msg in enumerate(final_messages):
                    if (msg.get('role') == 'assistant' and
                            'tool_calls' in msg and
                            any(tc.get('id') in tool_call_ids for tc in msg.get('tool_calls', []) if 'id' in tc)):
                        most_likely_assistant_idx = i
                        break

            # If we couldn't find it by mapping, try the usual method
            if most_likely_assistant_idx is None:
                most_likely_assistant_idx = self.find_assistant_for_tool_call_ids(final_messages, tool_call_ids)

            if most_likely_assistant_idx is None:
                current_app.logger.warning(
                    f"Could not find corresponding assistant for consolidated response with tool_call_ids: {tool_call_ids}. Converting to user message.")
                final_messages.append({
                    'role': 'user',
                    'name': 'Helper',
                    'content': fixed_consolidated.get('content', '')
                })
                continue

            current_app.logger.info(
                f"Found corresponding assistant at index {most_likely_assistant_idx} for consolidated response")

            # Insert the consolidated response right after the assistant message
            # Add 1 to position it after the assistant message
            insert_position = most_likely_assistant_idx + 1

            # If we already have tool responses after this assistant,
            # insert after the last one to maintain proper sequence
            for j in range(insert_position, len(final_messages)):
                if final_messages[j].get('role') != 'tool':
                    break
                insert_position = j + 1

            # Insert the consolidated response
            final_messages.insert(insert_position, fixed_consolidated)
            current_app.logger.info(
                f"Inserted consolidated response with {len(tool_call_ids)} tool_call_ids after assistant message at index {most_likely_assistant_idx}")

            # Mark these tool calls as responded
            for tool_call_id in tool_call_ids:
                if tool_call_id in pending_tool_calls:
                    pending_tool_calls.remove(tool_call_id)
                    current_app.logger.info(
                        f"Marked tool_call_id {tool_call_id} as responded via consolidated response")

        # STEP 4: Check for active vs. historical pending tool calls
        if pending_tool_calls:
            # Identify active tool calls from the most recent assistant message
            active_tool_call_ids = set()
            most_recent_assistant_idx = None

            # Find the most recent assistant message with tool calls
            for i in range(len(final_messages) - 1, -1, -1):
                if final_messages[i].get('role') == 'assistant' and 'tool_calls' in final_messages[i]:
                    most_recent_assistant_idx = i
                    break

            if most_recent_assistant_idx is not None:
                # Get tool calls from most recent assistant message
                last_assistant_msg = final_messages[most_recent_assistant_idx]
                active_tool_call_ids = {
                    tc.get('id') for tc in last_assistant_msg.get('tool_calls', [])
                    if 'id' in tc
                }

            # Distinguish between active and historical pending tool calls
            historical_pending_calls = [
                tc_id for tc_id in pending_tool_calls
                if tc_id not in active_tool_call_ids
            ]

            active_pending_calls = [
                tc_id for tc_id in pending_tool_calls
                if tc_id in active_tool_call_ids
            ]

            # Log but don't interfere with active tool calls
            if active_pending_calls:
                current_app.logger.info(
                    f"Detected {len(active_pending_calls)} active tool calls - letting framework handle execution"
                )

            # Only fix historical tool calls with missing responses
            if historical_pending_calls:
                current_app.logger.warning(
                    f"Found {len(historical_pending_calls)} historical tool calls with missing responses"
                )

                # Add placeholders only for historical pending tool calls
                for tool_call_id in historical_pending_calls:
                    if tool_call_id in tool_call_mapping:
                        assistant_idx = tool_call_mapping[tool_call_id]

                        # Only add placeholder if the assistant message still exists
                        if assistant_idx < len(final_messages) and final_messages[assistant_idx].get(
                                'role') == 'assistant':
                            assistant_msg = final_messages[assistant_idx]

                            # Find the function name for this tool call
                            function_name = None
                            for tc in assistant_msg.get('tool_calls', []):
                                if tc.get('id') == tool_call_id and tc.get('type') == 'function':
                                    function_name = tc.get('function', {}).get('name')
                                    break

                            placeholder = {
                                'role': 'tool',
                                'name': function_name or assistant_msg.get('name', 'Assistant'),
                                'tool_call_id': tool_call_id,
                                'content': "Placeholder response for historical tool call"
                            }

                            # Insert the placeholder right after the assistant message
                            insert_position = assistant_idx + 1

                            # If we already have tool responses after this assistant,
                            # insert after the last one to maintain proper sequence
                            for j in range(insert_position, len(final_messages)):
                                if final_messages[j].get('role') != 'tool':
                                    break
                                insert_position = j + 1

                            final_messages.insert(insert_position, placeholder)
                            current_app.logger.info(
                                f"Added placeholder for historical tool_call_id {tool_call_id}"
                            )



        final_messages = self.remove_orphan_tool_messages(final_messages)

        current_app.logger.info(f"Processed {len(messages)} messages into {len(final_messages)} validated messages")
        return self.validate_messages(final_messages)

    def get_logs(self, pre_transform_messages: List[Dict], post_transform_messages: List[Dict]) -> Tuple[str, bool]:
        """Generates logs about the transformation.

        Args:
            pre_transform_messages (List[Dict]): Messages before transformation
            post_transform_messages (List[Dict]): Messages after transformation

        Returns:
            Tuple[str, bool]: A tuple containing the log message and whether a transformation occurred
        """
        if len(pre_transform_messages) != len(post_transform_messages):
            return f"Message count changed: {len(pre_transform_messages)} → {len(post_transform_messages)}", True

        # Count role changes
        changes = 0
        for i in range(min(len(pre_transform_messages), len(post_transform_messages))):
            if pre_transform_messages[i].get('role') != post_transform_messages[i].get('role'):
                changes += 1

        if changes > 0:
            return f"Modified {changes} message roles", True

        return "No message transformations needed", False

class Action:
    def __init__(self,actions):
        self.actions = actions
        self.current_action = 1
        self.fallback = False
        self.new_json = []
        self.recipe = False
        self.ledger = None  # Smart Ledger for persistent task tracking

    def get_action(self, array_index):
        if array_index < 0 or array_index >= len(self.actions):
            raise IndexError(f"Array index {array_index} out of range")

        return self.actions[array_index]

    def get_action_byaction_id(self,action_id):
        for i in self.actions:
            if i['action_id'] == action_id:
                return i
        return None

    def set_ledger(self, ledger):
        """Attach Smart Ledger to this Action instance"""
        self.ledger = ledger
        current_app.logger.info(f"Smart Ledger attached with {len(ledger.tasks)} tasks")

def txt2img(text: Annotated[str, "Text to create image"]) -> str:
    current_app.logger.info('INSIDE txt2img')
    url = f"http://aws_rasa.hertzai.com:5459/txt2img?prompt={text}"

    payload = ""
    headers = {}

    response = pooled_post(url, headers=headers, data=payload)
    return response.json()['img_url']


def get_frame(user_id):
    current_app.logger.info('inside get_frame')
    serialized_frame = redis_client.get(user_id)
    current_app.logger.info('after redis client')
    try:
        if serialized_frame is not None:
            from security.safe_deserialize import safe_load_frame
            frame_bgr = safe_load_frame(serialized_frame)
            current_app.logger.info(
                f"Frame for user_id {user_id} retrieved successfully.")
            frame = frame_bgr[:, :, ::-1]
            return frame
        else:
            current_app.logger.info(f"No frame found for user_id {user_id}.")
            return None
    except ModuleNotFoundError as e:
        raise e

def get_user_camera_inp(inp: Annotated[str, "The Question to check from visual context"],user_id:int,request_id:str) -> str:
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
        url = "http://azurekong.hertzai.com:8000/minicpm/upload"
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
    else:
        return 'failed to get visual context ask user to check if the camera is turned on'



def get_time_based_history(prompt: str, session_id: str, start_date: str, end_date: str):
    '''
        This function help to extract messages till specified time
        inputs:
            prompt: text from user from which we need to extract similar messages
            session_id: user_{user_id}
            start_date: time of search start
            end_date: time till search
    '''

    start_time = time.time()
    memory = ZepMemory(
        session_id=session_id,
        url='http://azure_all_vms.hertzai.com:8000',
        api_key='***REMOVED***',
        memory_key="chat_history",
    )

    try:
        metadata = {}
        if start_date:
            metadata['start_date'] = start_date
        if end_date:
            metadata['end_date'] = end_date

        try:
            messages = memory.chat_memory.search(prompt, metadata=metadata)
            current_app.logger.info(f'GOT THE messages from search {messages}')
        except Exception as e:
            current_app.logger.info(f'Error: {e}')
        try:
            extracted_metadata = [message.message['metadata']
                                  for message in messages]
            list_req_ids = [data.get('request_Id', None)
                            for data in extracted_metadata]
            current_app.logger.info(f'GOT THE EXTRACTED METADATA AS {extracted_metadata}')
        except Exception as e:
            current_app.logger.info(f"Error while getting req ids {e}")

        # messages = [message.dict() for message in messages]
        serialized_results = []
        for result in messages:
            serialized_result = result.dict(exclude_unset=True)
            # Process the 'message' field to include only specific subfields
            if 'message' in serialized_result and isinstance(serialized_result['message'], dict):
                message = serialized_result['message']
                filtered_message = {
                    'content': message.get('content'),
                    'role': message.get('role'),
                    'created_at': message.get('created_at'),
                    'request_id': message.get('metadata', {}).get('request_id') if 'metadata' in message else None
                }
                # Replace the original message with the filtered message
                serialized_result['message'] = filtered_message
            serialized_results.append(serialized_result)
        messages = serialized_results
        final_res = {'res_in_filter': messages}
        current_app.logger.info(f"final-->{final_res}")
        end_time = time.time()
        elapsed_time = end_time - start_time
        return json.dumps(final_res)
    except Exception as e:
        current_app.logger.info(f"Exception {e}")
        try:
            messages = memory.chat_memory.search(prompt)
        except:
           current_app.logger.info(f'Error: {e}')

        # current_app.logger.info(f"final messages in except-->{messages}")
        try:
            extracted_metadata = [message.message['metadata']
                                  for message in messages]
            list_req_ids = [data.get('request_Id', None)
                            for data in extracted_metadata]
        except Exception as e:
            current_app.logger.info(f"Error while getting req ids {e}")
        end_time = time.time()
        elapsed_time = end_time - start_time
        current_app.logger.info("time taken for zep is {elapsed_time}")
        return json.dumps({'res': [message.message['content'] for message in messages]})

def parse_date(date_str):
    return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")

def get_visual_context(user_id,mins=5):
    '''
        This function help to extract action that user have perfomed till time
    '''
    # action_url = f"{ACTION_API}?user_id={user_id}"
    action_url = f"https://mailer.hertzai.com/get_visual_bymins?user_id={user_id}&mins={mins}"
    # Todo: get, and populate timezone from client
    time_zone = "Asia/Kolkata"

    india_tz = pytz.timezone(time_zone)

    payload = {}
    headers = {}

    response = pooled_request(
        "GET", action_url, headers=headers, data=payload)

    if response.status_code == 200:
        data = response.json()
        filtered_data_video = [
            obj for obj in data if obj["zeroshot_label"] == 'Video Reasoning']
        # Process video data
        video_context_texts = []
        for obj in filtered_data_video:
            action = obj["action"]
            date = parse_date(obj["created_date"])
            gpt3_label = obj["gpt3_label"]
            if gpt3_label == 'Visual Context':
                now = datetime.now()
                # Check if the action is older than 5 minutes
                if (now - date) > timedelta(minutes=mins):
                    continue
            first_action_text = f"{action} on {date.astimezone(india_tz).strftime('%Y-%m-%dT%H:%M:%S')}"

            video_context_texts.append(first_action_text)
        if video_context_texts:
            return video_context_texts[:10]
        else:
            return None
    else:
        return None


def get_screen_context(user_id, mins=2):
    '''
        Get recent screen understanding descriptions (shorter window than visual).
        Screen context goes stale faster — default 2 minute window.
    '''
    action_url = f"https://mailer.hertzai.com/get_visual_bymins?user_id={user_id}&mins={mins}"
    time_zone = "Asia/Kolkata"
    india_tz = pytz.timezone(time_zone)

    try:
        response = pooled_request("GET", action_url, headers={}, data={})
    except Exception:
        return None

    if response.status_code == 200:
        data = response.json()
        filtered_data_screen = [
            obj for obj in data if obj["zeroshot_label"] == 'Screen Reasoning']
        screen_context_texts = []
        for obj in filtered_data_screen:
            action = obj["action"]
            date = parse_date(obj["created_date"])
            now = datetime.now()
            if (now - date) > timedelta(minutes=mins):
                continue
            screen_text = f"{action} on {date.astimezone(india_tz).strftime('%Y-%m-%dT%H:%M:%S')}"
            screen_context_texts.append(screen_text)
        if screen_context_texts:
            return screen_context_texts[:10]
        else:
            return None
    else:
        return None

def search_visual_history(user_id, query, mins=30, channel='both'):
    '''
        Search past camera/screen descriptions by substring match within a time window.
        Reuses the same DB endpoint as get_visual_context/get_screen_context.
        channel: 'camera', 'screen', or 'both'
    '''
    action_url = f"https://mailer.hertzai.com/get_visual_bymins?user_id={user_id}&mins={mins}"
    time_zone = "Asia/Kolkata"
    india_tz = pytz.timezone(time_zone)

    try:
        response = pooled_request("GET", action_url, headers={}, data={})
    except Exception:
        return None

    if response.status_code != 200:
        return None

    data = response.json()
    query_lower = query.lower()
    results = []

    for obj in data:
        label = obj.get("zeroshot_label", "")
        # Filter by channel
        if channel == 'camera' and label != 'Video Reasoning':
            continue
        if channel == 'screen' and label != 'Screen Reasoning':
            continue
        if channel == 'both' and label not in ('Video Reasoning', 'Screen Reasoning'):
            continue

        action = obj.get("action", "")
        # Substring match on query
        if query_lower and query_lower not in action.lower():
            continue

        date = parse_date(obj["created_date"])
        now = datetime.now()
        if (now - date) > timedelta(minutes=mins):
            continue

        ch = 'camera' if label == 'Video Reasoning' else 'screen'
        results.append(f"[{ch}] {action} at {date.astimezone(india_tz).strftime('%Y-%m-%dT%H:%M:%S')}")

    return results[:20] if results else None


def get_memory(user_id: int):
    '''
        Get memory object from zep
    '''
    session_id = "user_"+str(user_id)
    memory = ZepMemory(
        session_id=session_id,
        url=ZEP_API_URL,
        memory_key="chat_history",
        api_key=ZEP_API_KEY,
        return_messages=True,
        input_key="input"
    )
    return memory

def history(user_id,prompt_id,role,message):
    try:
        memory = get_memory(user_id=int(user_id))
    except:
        return "Invalid user ID"
    if memory:
        if role == 'user':
            memory.chat_memory.add_message(
                HumanMessage(content=message),
                metadata={'prompt_id': prompt_id}
            )
        else:
            memory.chat_memory.add_message(
                AIMessage(content=message),
                metadata={'prompt_id': prompt_id}
            )
        return "Messages are saved!!!"
    else:
        return "Memory object not found"


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

llm_config = {
    "config_list": config_list,
    "cache_seed": None
}


def create_visual_agent(user_id,prompt_id):
    visual_agent = autogen.AssistantAgent(
        name='visual_agent',
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message="You are an helpful AI assistant used to perform visual based tasks given to you. "
    )

    visual_user = autogen.UserProxyAgent(
        name=f"UserProxy",
        human_input_mode="NEVER",
        llm_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )
    helper2 = autogen.AssistantAgent(
        name="Helper",
        llm_config=llm_config,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message=f"""You are Helper Agent. Help the visual_agent to complete the task:
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than ,Executor,multi_role_agent.
            4. Tools you have [txt2img, img2txt, save_data_in_memory, get_data_from_memory, get_user_id, get_prompt_id, Generate_video, get_user_uploaded_file, get_user_camera_inp, get_chat_history, create_scheduled_jobs] if you have any task which is not doable by these tool check recipe first else create python code to do so
            5. Keep track of action and only go to next action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the, always use this format: @user {{'message_2_user':'message here'}}
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            When writing code, always print the final response just before returning it.
        """,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    executor2 = autogen.AssistantAgent(
        name="Executor",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        system_message=f'''You are a executor agent. focused solely on creating, running & debugging code.
            Your responsibilities:
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than,Executor,multi_role_agent.
            4. Tools Helper Agent can use [send_message_in_seconds,send_message_to_user,send_presynthesized_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory]
            5. Keep track of action and only go to next action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continuously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the user, always use this format: @user {{'message_2_user':'message here'}}
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.

            Note: Your Working Directory is "{os.getcwd()}" - CRITICAL: When writing code, ALWAYS use os.path.join(os.getcwd(), filename) for file paths. NEVER hardcode paths like '/home/user/path'.
            Add proper error handling, logging.
            Always provide clear execution results or error messages to the assistant.
            if you get any conversation which is not related to coding ask the manager to route this conversation to user
            When writing code, always print the final response just before returning it.
        ''',
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    multi_role_agent2 = autogen.AssistantAgent(
        name="multi_role_agent",
        llm_config=llm_config,
        code_execution_config=False,
        system_message="""You will send message from multiple different personas your, job is to ask those question to assistant agent
        if you think some text was intent to give to some other agent but i came to you send the same message to user""",
    )
    verify2 = autogen.AssistantAgent(
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
            Only mark an action as "Completed" if the Assistant Agent confirms successful completion.
            For pending tasks or ongoing actions, respond to helper to complete the task.
            Verify the action performed by assistant and make sure the action is performed correctly as per instructions. if action performed was not as per instructions give the pending actions to the helper agent.
            Report status only—do not perform actions yourself.
            Use "requires_breakdown" when an action is too complex and needs to be split into smaller subtasks.

        """,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )

    chat_instructor2 = autogen.UserProxyAgent(
        name="ChatInstructor",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        default_auto_reply="TERMINATE",
        code_execution_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )

    context_handling = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=50,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(),
        ]
    )
    context_handling.add_to_agent(visual_agent)
    context_handling.add_to_agent(helper2)
    context_handling.add_to_agent(executor2)
    context_handling.add_to_agent(multi_role_agent2)
    context_handling.add_to_agent(verify2)

    return visual_agent, visual_user, helper2, executor2, multi_role_agent2, verify2, chat_instructor2


# Create agent_data directory if it doesn't exist
AGENT_DATA_DIR = "agent_data"
if not os.path.exists(AGENT_DATA_DIR):
    os.makedirs(AGENT_DATA_DIR)


def get_agent_data_file_path(prompt_id: int) -> str:
    """Get the file path for storing agent data for a specific prompt_id"""
    return os.path.join(AGENT_DATA_DIR, f"{prompt_id}_agent_data.json")


def save_agent_data_to_file(prompt_id: int, agent_data: Dict) -> bool:
    """
    Save current agent_data[prompt_id] to a JSON file

    Args:
        prompt_id: The prompt ID to save data for
        agent_data: The agent data dictionary
    Returns:
        bool: True if saved successfully, False otherwise
    """
    try:
        file_path = get_agent_data_file_path(prompt_id)

        # Get current agent data for this prompt_id
        data_to_save = agent_data.get(prompt_id, {})

        # Add metadata about when this was saved
        save_metadata = {
            "prompt_id": prompt_id,
            "saved_at": datetime.now().isoformat(),
            "data": data_to_save
        }

        # Write to file with encryption (falls back to plaintext if no key configured)
        try:
            from security.crypto import encrypt_json_file
            encrypt_json_file(file_path, save_metadata)
        except ImportError:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(save_metadata, f, indent=2, ensure_ascii=False)

        current_app.logger.info(f" Saved agent data to: {file_path}")
        return True

    except Exception as e:
        current_app.logger.error(f" Error saving agent data for prompt_id {prompt_id}: {e}")
        return False


def load_agent_data_from_file(prompt_id: int, agent_data: Dict) -> bool:
    """
    Load agent_data[prompt_id] from JSON file

    Args:
        prompt_id: The prompt ID to load data for
        agent_data: The agent data dictionary

    Returns:
        bool: True if loaded successfully, False otherwise
    """
    try:
        file_path = get_agent_data_file_path(prompt_id)

        # Check if file exists
        if not os.path.exists(file_path):
            current_app.logger.info(f"[FILE] No saved agent data found for prompt_id {prompt_id}")
            # Initialize with default data
            agent_data[prompt_id] = {}
            return False

        # Load from file (supports encrypted and plaintext)
        try:
            from security.crypto import decrypt_json_file
            loaded_data = decrypt_json_file(file_path)
            if loaded_data is None:
                current_app.logger.warning(f"Failed to decrypt/load: {file_path}")
                agent_data[prompt_id] = {}
                return False
        except ImportError:
            with open(file_path, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)

        # Extract the actual data (skip metadata)
        if 'data' in loaded_data:
            agent_data[prompt_id] = loaded_data['data']
            current_app.logger.info(f" Loaded agent data from: {file_path}")
            current_app.logger.info(f" Loaded data keys: {list(agent_data[prompt_id].keys())}")
            return True
        else:
            # Handle old format (direct data)
            agent_data[prompt_id] = loaded_data
            current_app.logger.info(f" Loaded agent data (old format) from: {file_path}")
            return True

    except Exception as e:
        current_app.logger.error(f" Error loading agent data for prompt_id {prompt_id}: {e}")
        # Initialize with default data on error
        agent_data[prompt_id] = {}
        return False


def schedule_periodic_backups(agent_data, scheduler):
    """Schedule periodic backups of agent data"""

    def backup_all_agent_data():
        """Backup all active agent data"""
        backup_count = 0
        for prompt_id in agent_data.keys():
            if agent_data[prompt_id]:  # Only backup if there's data
                if backup_agent_data_file(prompt_id):
                    backup_count += 1

    # Schedule daily backups at 2 AM
    if scheduler.running:
        scheduler.add_job(
            backup_all_agent_data,
            'cron',
            hour=2,
            minute=0,
            id='periodic_agent_data_backup'
        )


def initialize_persistent_storage(agent_data: Dict):
    """
    Initialize persistent storage and migrate existing data
    Call this during application startup
        Args:
        agent_data: The agent data dictionary

    """
    try:
        # Create agent_data directory if it doesn't exist
        if not os.path.exists(AGENT_DATA_DIR):
            os.makedirs(AGENT_DATA_DIR)

        return True

    except Exception as e:
        return False


def backup_agent_data_file(prompt_id: int) -> bool:
    """
    Create a backup of the current agent data file

    Args:
        prompt_id: The prompt ID to backup data for

    Returns:
        bool: True if backup created successfully, False otherwise
    """
    try:
        file_path = get_agent_data_file_path(prompt_id)

        if not os.path.exists(file_path):
            return False

        # Create backup with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = file_path.replace('.json', f'_backup_{timestamp}.json')

        # Copy file
        import shutil
        shutil.copy2(file_path, backup_path)

        current_app.logger.info(f" Created backup: {backup_path}")
        return True

    except Exception as e:
        current_app.logger.error(f" Error creating backup for prompt_id {prompt_id}: {e}")
        return False


def cleanup_old_backups(prompt_id: int, keep_count: int = 5) -> int:
    """
    Clean up old backup files, keeping only the most recent ones

    Args:
        prompt_id: The prompt ID to clean backups for
        keep_count: Number of backup files to keep

    Returns:
        int: Number of files deleted
    """
    try:
        backup_pattern = f"{prompt_id}_agent_data_backup_"
        backup_files = []

        # Find all backup files for this prompt_id
        for filename in os.listdir(AGENT_DATA_DIR):
            if filename.startswith(backup_pattern) and filename.endswith('.json'):
                file_path = os.path.join(AGENT_DATA_DIR, filename)
                # Get file modification time
                mtime = os.path.getmtime(file_path)
                backup_files.append((mtime, file_path))

        # Sort by modification time (newest first)
        backup_files.sort(reverse=True)

        # Delete old backups
        deleted_count = 0
        for i, (mtime, file_path) in enumerate(backup_files):
            if i >= keep_count:  # Keep only the newest keep_count files
                os.remove(file_path)
                deleted_count += 1
                current_app.logger.info(f" Deleted old backup: {file_path}")

        return deleted_count

    except Exception as e:
        current_app.logger.error(f" Error cleaning up backups for prompt_id {prompt_id}: {e}")
        return 0


def get_agent_data_info(prompt_id: int) -> Dict[str, Any]:
    """
    Get information about saved agent data file

    Args:
        prompt_id: The prompt ID to get info for

    Returns:
        dict: Information about the file
    """
    try:
        file_path = get_agent_data_file_path(prompt_id)

        if not os.path.exists(file_path):
            return {"exists": False, "path": file_path}

        # Get file stats
        stat = os.stat(file_path)

        # Try to get save metadata
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            saved_at = data.get('saved_at', 'unknown')
            data_keys = list(data.get('data', {}).keys()) if 'data' in data else list(data.keys())
        except:
            saved_at = 'unknown'
            data_keys = []

        return {
            "exists": True,
            "path": file_path,
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "saved_at": saved_at,
            "data_keys": data_keys
        }

    except Exception as e:
        current_app.logger.error(f" Error getting agent data info for prompt_id {prompt_id}: {e}")
        return {"exists": False, "error": str(e)}


# ========================================================================================
# AUTOGEN JSON HANDLING ENHANCEMENT
# ========================================================================================
def safe_function_call(func, arguments):
    """Fixed version that handles list with dict properly"""
    import logging

    logger = logging.getLogger("safe_function_call")

    logger.info(" SAFE_FUNCTION_CALL DEBUG:")
    logger.info(f"   Function: {func.__name__ if hasattr(func, '__name__') else func}")
    logger.info(f"   Arguments type: {type(arguments)}")
    logger.info(f"   Arguments content: {arguments}")

    try:
        # Try original AutoGen approach first
        if isinstance(arguments, dict):
            logger.info("   → Using **kwargs approach")
            result = func(**arguments)
            logger.info("    Success with **kwargs")
            return result

        # Handle list case - FIXED LOGIC
        elif isinstance(arguments, list):
            logger.info("   → Analyzing list content")

            # Check if first item is a dict (common pattern from retrieve_json)
            if len(arguments) >= 1 and isinstance(arguments[0], dict):
                # The first item is the actual arguments dict
                actual_args = arguments[0]
                logger.info(f"   → Found dict in list[0]: {actual_args}")
                logger.info("   → Using **kwargs approach on extracted dict")
                result = func(**actual_args)
                logger.info("    Success with **kwargs from list")
                return result
            else:
                # Fallback to treating as positional args
                logger.info("   → Using *args approach")
                result = func(*arguments)
                logger.info("    Success with *args")
                return result

        # Handle single argument case
        else:
            logger.info("   → Using single argument approach")
            result = func(arguments)
            logger.info("    Success with single arg")
            return result

    except TypeError as e:
        logger.error(f"    TypeError: {e}")
        logger.error(f"   TypeError traceback:\n{traceback.format_exc()}")

        # Enhanced intelligent mapping for lists
        if isinstance(arguments, list):
            logger.info("   → Trying enhanced list handling")

            try:
                # If it's a list with a dict, extract the dict
                if len(arguments) >= 1 and isinstance(arguments[0], dict):
                    logger.info("   → Extracting dict from list and retrying")
                    result = func(**arguments[0])
                    logger.info("    Success with extracted dict")
                    return result

                # If it's a simple list, try intelligent parameter mapping
                elif hasattr(func, '__annotations__'):
                    import inspect
                    sig = inspect.signature(func)
                    param_names = list(sig.parameters.keys())
                    logger.info(f"   → Function expects parameters: {param_names}")

                    # Filter out truncation indicators
                    clean_args = [arg for arg in arguments if
                                  not (isinstance(arg, list) and len(arg) == 1 and arg[0] == 'truncated')]

                    if len(clean_args) <= len(param_names):
                        kwargs = dict(zip(param_names, clean_args))
                        logger.info(f"   → Mapped to kwargs: {kwargs}")
                        result = func(**kwargs)
                        logger.info("    Success with intelligent mapping")
                        return result

            except Exception as mapping_error:
                logger.error(f"    Enhanced list handling failed: {mapping_error}")
                logger.error(f"   Mapping traceback:\n{traceback.format_exc()}")

        # Re-raise if we can't handle it
        logger.error("    Cannot handle - re-raising original TypeError")
        raise e

    except Exception as e:
        logger.error(f"    Unexpected error: {e}")
        logger.error(f"   Unexpected error traceback:\n{traceback.format_exc()}")
        raise e


def force_apply_autogen_json_fix():
    """Force apply the autogen JSON fix with robust error handling."""

    def enhanced_execute_function(self, func_call, verbose: bool = False):
        """Enhanced execute_function that falls back to retrieve_json only when original fails."""
        try:
            from autogen.io.base import IOStream
            iostream = IOStream.get_default()
        except:
            class MockIOStream:
                def print(self, *args, **kwargs):
                    print(*args)

            iostream = MockIOStream()

        func_name = func_call.get("name", "")
        func = self._function_map.get(func_name, None)

        is_exec_success = False
        if func is not None:
            # ========== PRESERVE ORIGINAL AUTOGEN LOGIC ==========
            # Extract arguments from a json-like string and put it into a dict.
            input_string = func_call.get("arguments", "{}")

            try:
                # Try original autogen approach first
                formatted_string = self._format_json_str(input_string)
                arguments = json.loads(formatted_string)
                print(f" ORIGINAL AUTOGEN: Successfully parsed arguments for {func_name}")
            except (json.JSONDecodeError, Exception) as e:
                # Only if original fails, fall back to our enhanced parsing
                print(f" ORIGINAL AUTOGEN FAILED: {e} - falling back to enhanced parsing for {func_name}")
                try:
                    arguments = retrieve_json(input_string)
                    if arguments is None:
                        arguments = {}
                    elif isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    print(f" FALLBACK SUCCESSFUL: Enhanced parsing worked for {func_name}")
                except Exception as fallback_error:
                    print(f" FALLBACK FAILED: {fallback_error}")
                    arguments = None
                    content = f"Error: {e}\n The argument must be in JSON format."

            # ========== PRESERVE ORIGINAL EXECUTION LOGIC ==========
            if arguments is not None:
                iostream.print(f"\n>>>>>>>> EXECUTING FUNCTION {func_name}...", flush=True)
                try:
                    print(" Function being called details:")
                    print(f"   Function: {func}")
                    print(f"   Function name: {getattr(func, '__name__', 'NO_NAME')}")
                    print(" Parsed arguments analysis:")
                    print(f"   Arguments type: {type(arguments)}")
                    print(f"   Arguments content: {arguments}")
                    content = safe_function_call(func, arguments)  # Original autogen always uses **kwargs
                    is_exec_success = True
                    print(f" EXECUTED: Successfully executed {func_name}")
                except Exception as e:
                    content = f"Error: {e}"
                    print(f" EXECUTION FAILED: {func_name}: {e}")
        else:
            content = f"Error: Function {func_name} not found."

        if verbose:
            iostream.print(f"\nInput arguments: {arguments}\nOutput:\n{content}", flush=True)

        return is_exec_success, {
            "name": func_name,
            "role": "function",
            "content": str(content),
        }

    async def enhanced_a_execute_function(self, func_call):
        """Enhanced async execute_function that falls back to retrieve_json only when original fails."""
        try:
            from autogen.io.base import IOStream
            iostream = IOStream.get_default()
        except:
            class MockIOStream:
                def print(self, *args, **kwargs):
                    print(*args)

            iostream = MockIOStream()

        func_name = func_call.get("name", "")
        func = self._function_map.get(func_name, None)

        is_exec_success = False
        if func is not None:
            input_string = func_call.get("arguments", "{}")

            try:
                # Try original autogen approach first
                formatted_string = self._format_json_str(input_string)
                arguments = json.loads(formatted_string)
                print(f" ORIGINAL AUTOGEN ASYNC: Successfully parsed arguments for {func_name}")
            except (json.JSONDecodeError, Exception) as e:
                # Only if original fails, fall back to our enhanced parsing
                print(f" ORIGINAL AUTOGEN ASYNC FAILED: {e} - falling back to enhanced parsing for {func_name}")
                try:
                    arguments = retrieve_json(input_string)
                    if arguments is None:
                        arguments = {}
                    elif isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    print(f" FALLBACK ASYNC SUCCESSFUL: Enhanced parsing worked for {func_name}")
                except Exception as fallback_error:
                    print(f" FALLBACK ASYNC FAILED: {fallback_error}")
                    arguments = None
                    content = f"Error: {e}\n The argument must be in JSON format."

            if arguments is not None:
                iostream.print(f"\n>>>>>>>> EXECUTING ASYNC FUNCTION {func_name}...", flush=True)
                try:
                    print(" Function being called details:")
                    print(f"   Function: {func}")
                    print(f"   Function name: {getattr(func, '__name__', 'NO_NAME')}")
                    print(" Parsed arguments analysis:")
                    print(f"   Arguments type: {type(arguments)}")
                    print(f"   Arguments content: {arguments}")
                    import inspect
                    if inspect.iscoroutinefunction(func):
                        if isinstance(arguments, dict):
                            content = await func(**arguments)  # Original autogen always uses **kwargs
                        # Handle list case - convert to positional arguments
                        elif isinstance(arguments, list):
                            content = await func(*arguments)  # Original autogen always uses **kwargs
                        # Handle single argument case
                        else:
                            content = await func(arguments)  # Original autogen always uses **kwargs
                    else:
                        content = safe_function_call(func, arguments)
                    is_exec_success = True
                    print(f" EXECUTED ASYNC: Successfully executed {func_name}")
                except Exception as e:
                    content = f"Error: {e}"
                    print(f" EXECUTION ASYNC FAILED: {func_name}: {e}")
        else:
            content = f"Error: Function {func_name} not found."

        return is_exec_success, {
            "name": func_name,
            "role": "function",
            "content": str(content),
        }

    # Force import autogen and apply patches
    try:
        import autogen
        from autogen.agentchat.conversable_agent import ConversableAgent

        # Store original methods for verification
        original_execute = getattr(ConversableAgent, 'execute_function', None)
        original_a_execute = getattr(ConversableAgent, 'a_execute_function', None)

        # Apply patches
        ConversableAgent.execute_function = enhanced_execute_function
        ConversableAgent.a_execute_function = enhanced_a_execute_function

        # Verify patches were applied
        new_execute = getattr(ConversableAgent, 'execute_function', None)
        new_a_execute = getattr(ConversableAgent, 'a_execute_function', None)

        if new_execute is not original_execute:
            print(" SUCCESS: Autogen sync execute_function has been patched!")
        else:
            print(" FAILED: Autogen sync execute_function patch was not applied")

        if new_a_execute is not original_a_execute:
            print(" SUCCESS: Autogen async execute_function has been patched!")
        else:
            print(" FAILED: Autogen async execute_function patch was not applied")

        print(" Autogen JSON handling enhanced - tool calls can now handle unlimited length!")
        return True

    except ImportError as e:
        print(f" Could not import autogen for patching: {e}")
        return False
    except Exception as e:
        print(f" Error applying autogen patches: {e}")
        import traceback
        traceback.print_exc()
        return False



# Also provide a manual trigger function for Flask startup
def apply_autogen_fix_on_startup():
    """Manual function to call during Flask app startup if automatic patch fails."""
    print("[INIT] Manually applying autogen JSON fix...")
    return force_apply_autogen_json_fix()

# ========================================================================================
# END AUTOGEN JSON HANDLING ENHANCEMENT
# ========================================================================================
def load_vlm_agent_files(prompt_id, role_number):
    """Loads any VLM agent JSON files for the given prompt_id and role_number and integrates them with existing recipes."""
    vlm_actions = []

    # Look for existing VLM agent files
    try:
        for file in os.listdir("prompts"):
            if file.startswith(f"{prompt_id}_{role_number}_") and file.endswith("_vlm_agent.json"):
                file_path = os.path.join("prompts", file)
                try:
                    with open(file_path, 'r') as f:
                        recipe_data = json.load(f)
                        current_app.logger.info(f"Found VLM agent recipe: {file_path}")

                        # Extract the action ID from the filename (assuming format: prompt_id_role_number_action_id_vlm_agent.json)
                        parts = file.split('_')
                        if len(parts) >= 4:
                            try:
                                action_id = int(parts[2]) # Get the action ID
                                # Add or replace action in the actions list
                                recipe_data["action_id"] = action_id
                                vlm_actions.append(recipe_data)
                            except (ValueError, IndexError):
                                current_app.logger.error(f"Couldn't parse action ID from filename {file}")
                except Exception as e:
                    current_app.logger.error(f"Error reading VLM agent file {file_path}: {e}")
    except Exception as e:
        current_app.logger.error(f"Error listing files in prompts directory: {e}")

    return vlm_actions
