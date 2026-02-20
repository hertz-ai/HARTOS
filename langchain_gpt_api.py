# Fix Windows encoding for non-ASCII characters (Telugu, emojis, etc.)
import sys
import io
if sys.platform == 'win32':
    # Force UTF-8 encoding for stdout/stderr to prevent crashes with non-ASCII characters
    if sys.stdout is not None:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if sys.stderr is not None:
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from bs4 import BeautifulSoup
from enum import Enum
from cultural_wisdom import get_cultural_prompt_compact

# Use langchain-classic for pydantic v2 compatibility
from langchain.llms import OpenAI
from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate
from langchain.agents import (
    ZeroShotAgent, Tool, AgentExecutor, ConversationalAgent,
    ConversationalChatAgent, LLMSingleActionAgent, AgentOutputParser,
    load_tools, initialize_agent, AgentType
)

import time
from langchain.prompts import (
    ChatPromptTemplate,
    MessagesPlaceholder,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate
)
from langchain.chains import LLMMathChain
from langchain.chains.conversation.memory import ConversationSummaryMemory, ConversationBufferWindowMemory

# ChatOpenAI - use langchain
from langchain.chat_models import ChatOpenAI

# ChatGroq - optional import (version compatibility issues)
try:
    from langchain_groq import ChatGroq
except Exception:
    ChatGroq = None

# LLM base class
from langchain.llms.base import LLM
from langchain.memory import ConversationBufferMemory, ReadOnlySharedMemory
from langchain.schema import AgentAction, AgentFinish, OutputParserException, HumanMessage, AIMessage, SystemMessage
from langchain.tools import OpenAPISpec, APIOperation, StructuredTool
from langchain.utilities import GoogleSearchAPIWrapper
try:
    from langchain.requests import Requests
except (ImportError, AttributeError):
    # Requests might not be in langchain
    Requests = None
from flask import Flask, jsonify, request
import json
import os
import re
import secrets
import logging
import threading
import atexit
import requests
import pytz
from core.http_pool import pooled_get, pooled_post
from datetime import datetime, timezone
from typing import List, Union, Optional, Mapping, Any, Dict

# Conversational chat imports from langchain-classic
try:
    from langchain.agents.conversational_chat.output_parser import ConvoOutputParser
    from langchain.agents.conversational_chat.prompt import FORMAT_INSTRUCTIONS
    from langchain.output_parsers.json import parse_json_markdown
except (ImportError, AttributeError) as e:
    # These might not exist in langchain-classic
    ConvoOutputParser = None
    FORMAT_INSTRUCTIONS = None
    parse_json_markdown = None

# Tools imports - try langchain_community first
try:
    from langchain_community.tools import RequestsGetTool
except Exception:
    try:
        from langchain.tools.requests.tool import RequestsGetTool
    except (ImportError, AttributeError):
        RequestsGetTool = None

try:
    from langchain_community.utilities import TextRequestsWrapper
except Exception:
    try:
        from langchain.utilities.requests import TextRequestsWrapper
    except (ImportError, AttributeError):
        TextRequestsWrapper = None

import tiktoken
from pytz import timezone
from datetime import datetime, timedelta
from waitress import serve
from logging.handlers import RotatingFileHandler

# Pydantic v2 imports (we have pydantic 2.12.5)
try:
    from pydantic import BaseModel, Field, field_validator as root_validator
except ImportError:
    from pydantic import BaseModel, Field, root_validator
from threadlocal import thread_local_data
try:
    import crossbarhttp
except Exception:
    crossbarhttp = None
from PIL import Image
import numpy as np
# Cohere rerank - make optional to avoid pydantic v2 incompatibility with old langchain
try:
    from langchain_community.retrievers.document_compressors import cohere_rerank
except Exception:
    cohere_rerank = None  # Not available
import asyncio
import aiohttp
import redis
import pickle
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
try:
    import autogen
except ImportError:
    autogen = None  # Optional dependency
from typing import Dict, Tuple
load_dotenv()

#autogen requirements

try:
    from create_recipe import recipe, time_based_execution as time_execution, visual_execution
    from reuse_recipe import chat_agent, crossbar_multiagent, time_based_execution, visual_based_execution
except ImportError as e:
    print(f"Could not import recipe modules: {e}")
    recipe = None
    time_execution = None
    visual_execution = None
    chat_agent = None
    crossbar_multiagent = None
    time_based_execution = None
    visual_based_execution = None

try:
    from autobahn.asyncio.component import Component, run
except ImportError:
    Component = None
    run = None

import threading

try:
    from helper import retrieve_json
except Exception:
    retrieve_json = None

# Google A2A integration (from gpt4.1)
try:
    from integrations.google_a2a import initialize_a2a_server, get_a2a_server, register_all_agents
except ImportError:
    initialize_a2a_server = None
    get_a2a_server = None
    register_all_agents = None
# os.environ['LANGCHAIN_TRACING_V2'] = 'true'
# os.environ['LANGCHAIN_ENDPOINT'] = 'https://api.smith.langchain.com'
# os.environ['LANGCHAIN_API_KEY'] = os.getenv("LANGCHAIN_API_KEY")
# os.environ["LANGCHAIN_PROJECT"] = os.getenv("LANGCHAIN_PROJECT")
groq_api_key = os.environ.get('GROQ_API_KEY', '')


# ============================================================================
# MemoryGraph — Framework-agnostic provenance-aware memory layer
# ============================================================================
_memory_graphs = {}  # Cache: "user_id_prompt_id" → MemoryGraph instance
_memory_graph_lock = threading.Lock()


def _get_or_create_graph(user_id, prompt_id=None):
    """Get or create a MemoryGraph instance for a user/prompt session."""
    try:
        from integrations.channels.memory.memory_graph import MemoryGraph
        session_key = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
        with _memory_graph_lock:
            if session_key not in _memory_graphs:
                db_path = os.path.join(
                    os.path.expanduser("~"), "Documents", "Nunba", "data", "memory_graph", session_key
                )
                _memory_graphs[session_key] = MemoryGraph(
                    db_path=db_path,
                    user_id=str(user_id),
                )
            return _memory_graphs[session_key]
    except Exception as e:
        logging.getLogger(__name__).debug(f"MemoryGraph init skipped: {e}")
        return None


def _record_lifecycle(status, user_id, prompt_id, details=''):
    """Record agent lifecycle event in MemoryGraph (fire-and-forget, zero latency)."""
    def _bg():
        try:
            graph = _get_or_create_graph(user_id, prompt_id)
            if graph:
                graph.register_lifecycle(
                    event=status,
                    agent_id=str(user_id),
                    session_id=f"{user_id}_{prompt_id}",
                    details=details,
                )
        except Exception:
            pass
    threading.Thread(target=_bg, daemon=True).start()


# ============================================================================
# Custom Qwen3-VL LangChain Wrapper
# ============================================================================
class ChatQwen3VL(LLM):
    """
    Custom LangChain LLM wrapper for a local OpenAI-compatible API server.

    In bundled Nunba mode, automatically targets the llama.cpp server that
    Nunba already starts on port 8080 (CPU inference). Otherwise falls back
    to Qwen3-VL on port 8000 or any HEVOLVE_LOCAL_LLM_URL override.

    Features:
    - OpenAI-compatible API interface
    - Multimodal support (text + images)
    - Zero API costs (local server)
    - Drop-in replacement for ChatOpenAI
    """

    base_url: str = os.environ.get('HEVOLVE_LOCAL_LLM_URL', "http://localhost:8000/v1")
    model_name: str = "local"
    temperature: float = 0.7
    max_tokens: int = 1500

    @property
    def _llm_type(self) -> str:
        return "qwen3-vl"

    def _call(self, prompt: str, stop: list = None) -> str:
        """
        Call the Qwen3-VL API with the given prompt.

        Args:
            prompt: The input text prompt
            stop: Optional stop sequences

        Returns:
            The generated response text
        """
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }

        if stop:
            payload["stop"] = stop

        _log = logging.getLogger(__name__)
        # Primary: crawl4ai embodied-ai on port 8000
        try:
            response = pooled_post(
                f"{self.base_url}/chat/completions",
                json=payload,
                timeout=60
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            _log.warning(f"[LocalLLM] {self.base_url} unavailable: {e}")

        # Fallback: llama.cpp (Nunba always starts it)
        _llama_port = os.environ.get('LLAMA_CPP_PORT', '8080')
        _llama_url = f"http://127.0.0.1:{_llama_port}/v1"
        if _llama_url not in self.base_url:
            try:
                _log.info(f"[LocalLLM] Falling back to llama.cpp at {_llama_url}")
                response = pooled_post(
                    f"{_llama_url}/chat/completions",
                    json=payload,
                    timeout=120
                )
                response.raise_for_status()
                data = response.json()
                return data["choices"][0]["message"]["content"]
            except Exception as e2:
                _log.error(f"[LocalLLM] llama.cpp fallback also failed: {e2}")
                raise
        raise

    @property
    def _identifying_params(self) -> dict:
        """Return identifying parameters."""
        return {
            "model_name": self.model_name,
            "base_url": self.base_url,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }


# Flag to switch between OpenAI and Qwen3-VL
USE_QWEN3VL = True  # Set to False to use OpenAI instead

def get_llm(model_name="gpt-3.5-turbo", temperature=0.7, max_tokens=1500):
    """
    Get LLM instance based on configuration.

    Priority:
    1. Wizard-configured cloud provider (HEVOLVE_ACTIVE_CLOUD_PROVIDER env var)
    2. ChatQwen3VL (local) if USE_QWEN3VL flag is True
    3. ChatOpenAI fallback
    """
    _active = os.environ.get('HEVOLVE_ACTIVE_CLOUD_PROVIDER', '')

    if _active == 'anthropic' and os.environ.get('ANTHROPIC_API_KEY'):
        try:
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(
                model=os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-20250514'),
                api_key=os.environ['ANTHROPIC_API_KEY'],
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except ImportError:
            pass

    if _active == 'google_gemini' and os.environ.get('GOOGLE_API_KEY'):
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
            return ChatGoogleGenerativeAI(
                model=os.environ.get('GOOGLE_MODEL', 'gemini-2.0-flash'),
                google_api_key=os.environ['GOOGLE_API_KEY'],
                temperature=temperature,
                max_output_tokens=max_tokens,
            )
        except ImportError:
            pass

    if _active == 'groq' and os.environ.get('GROQ_API_KEY'):
        try:
            from langchain_groq import ChatGroq
            return ChatGroq(
                model=os.environ.get('GROQ_MODEL', 'llama-3.3-70b-versatile'),
                api_key=os.environ['GROQ_API_KEY'],
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except ImportError:
            pass

    if _active in ('openai', 'azure_openai', 'custom_openai') and os.environ.get('OPENAI_API_KEY'):
        _kwargs = dict(
            model_name=os.environ.get('OPENAI_MODEL', model_name),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if _active == 'custom_openai' and os.environ.get('CUSTOM_LLM_BASE_URL'):
            _kwargs['openai_api_base'] = os.environ['CUSTOM_LLM_BASE_URL']
        return ChatOpenAI(**_kwargs)

    if USE_QWEN3VL:
        return ChatQwen3VL(
            model_name="Qwen3-VL-2B-Instruct",
            temperature=temperature,
            max_tokens=max_tokens
        )

    return ChatOpenAI(
        model_name=model_name,
        temperature=temperature,
        max_tokens=max_tokens
    )
# ============================================================================


class RequestLogRecord(logging.LogRecord):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Safely get the req_id from thread-local storage
        self.req_id = thread_local_data.get_request_id()


# logging info
# Use the custom log record factory
logging.setLogRecordFactory(RequestLogRecord)
logging.basicConfig(level=logging.INFO)
stream_handler = logging.StreamHandler(sys.stdout)
# In bundled/pip-installed mode (NUNBA_BUNDLED env set by main.py), redirect logs
# to the shared Nunba log directory; standalone keeps default behavior.

if os.environ.get('NUNBA_BUNDLED') or getattr(sys, 'frozen', False):
    _nunba_log_dir = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'logs')
    os.makedirs(_nunba_log_dir, exist_ok=True)
    _langchain_log_path = os.path.join(_nunba_log_dir, 'langchain.log')
else:
    _langchain_log_path = 'langchain.log'

handler = RotatingFileHandler(_langchain_log_path, maxBytes=5_000_000, backupCount=2)

# Set the logging level for the file handler
# Was ERROR — changed to INFO so that LangChain, crawl4ai, and other library
# logs are captured in Documents/Nunba/logs/langchain.log (not just errors).
handler.setLevel(logging.INFO)

# Create a logging format
req_id = thread_local_data.get_request_id()
formatter = logging.Formatter(
    '%(asctime)s - %(name)s- [RequestID: %(req_id)s] - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

# In bundled mode, also attach the file handler to the root logger so that ALL
# module loggers (crawl4ai, langchain, etc.) write to Documents/Nunba/logs/langchain.log
if os.environ.get('NUNBA_BUNDLED') or getattr(sys, 'frozen', False):
    _root = logging.getLogger()
    _root.addHandler(handler)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)

app.logger.addHandler(stream_handler)
app.logger.addHandler(handler)
app.logger.propagate = False

# Security: Audit logging — redact API keys, JWTs, passwords from all logs
try:
    from security.audit_log import apply_sensitive_filter_to_all
    apply_sensitive_filter_to_all()
except Exception:
    pass  # Degrade gracefully — logs will still work, just unredacted

# Test logging
app.logger.info('Logger initialized')

# Security: Apply middleware (headers, CORS, CSRF, host validation, API auth)
try:
    from security.middleware import apply_security_middleware
    apply_security_middleware(app)
    app.logger.info("Security middleware applied")
except Exception as e:
    app.logger.warning(f"Security middleware not applied: {e}")

# ============================================================================
# HevolveSocial - Agent Social Network
# ============================================================================
try:
    from integrations.social import social_bp, init_social
    app.register_blueprint(social_bp)
    init_social(app)
    app.logger.info("HevolveSocial registered at /api/social")
except Exception as e:
    app.logger.warning(f"HevolveSocial init skipped (non-critical): {e}")

try:
    from integrations.distributed_agent import distributed_agent_bp
    app.register_blueprint(distributed_agent_bp)
    app.logger.info("distributed_agent_bp registered at /api/distributed")
except ImportError:
    app.logger.info("distributed_agent module not available, skipping")
except Exception as e:
    app.logger.warning(f"distributed_agent init skipped: {e}")

try:
    from integrations.social.api_provision import provision_bp
    app.register_blueprint(provision_bp)
    app.logger.info("provision_bp registered at /api/provision")
except ImportError:
    app.logger.info("Provision module not available, skipping")
except Exception as e:
    app.logger.warning(f"Provision init skipped: {e}")

# ============================================================================
# Google A2A Protocol Initialization
# ============================================================================
# Initialize A2A server for cross-platform agent communication
try:
    app.logger.info("Initializing Google A2A Protocol server...")
    a2a_server = initialize_a2a_server(app, base_url="http://localhost:6777")
    app.logger.info("Google A2A Protocol server initialized successfully")

    # Register all agents with A2A
    register_all_agents()
    app.logger.info("All agents registered with Google A2A Protocol")
except Exception as e:
    app.logger.warning(f"Google A2A Protocol initialization error (non-critical): {e}")

# openAPI spec
try:
    spec = OpenAPISpec.from_file(
        "./openapi.yaml"
    )
except Exception as e:
    app.logger.warning(f"Could not load OpenAPI spec: {e}")
    spec = None


# Load config.json — graceful fallback when keys are absent (e.g. bundled Nunba install
# where the config.json is Nunba's URL-template file, not langchain's API-key file).
# In frozen Nunba builds, langchain config is bundled as langchain_config.json to avoid
# collision with the project root config.json (which has IP_ADDRESS settings).
config = {}
for _cfg_name in ('langchain_config.json', 'config.json'):
    try:
        with open(_cfg_name, 'r') as f:
            _loaded = json.load(f)
        # Distinguish langchain config (has API keys) from Nunba URL config (has IP_ADDRESS)
        if 'IP_ADDRESS' not in _loaded or any(k.endswith('_API_KEY') for k in _loaded):
            config = _loaded
            break
        elif not config:
            config = _loaded  # fall through to try langchain_config.json first
    except Exception:
        pass

# global variables
try:
    encoding = tiktoken.encoding_for_model("gpt-3.5-turbo")
except (KeyError, Exception):
    encoding = tiktoken.get_encoding("cl100k_base")  # GPT-4 default

# api and keys — use config if available, otherwise keep existing env vars / empty
for _cfg_key in ('OPENAI_API_KEY', 'GOOGLE_CSE_ID', 'GOOGLE_API_KEY',
                  'NEWS_API_KEY', 'SERPAPI_API_KEY'):
    if _cfg_key in config:
        os.environ[_cfg_key] = config[_cfg_key]
    else:
        os.environ.setdefault(_cfg_key, '')

# Mode-aware inference: pass LLM endpoint to crawl4ai for non-flat deployments
_node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
_active_cloud = os.environ.get('HEVOLVE_ACTIVE_CLOUD_PROVIDER', '')
if _node_tier in ('regional', 'central'):
    os.environ.setdefault('HEVOLVE_LLM_ENDPOINT_URL', config.get('OPENAI_API_BASE', ''))
    os.environ.setdefault('HEVOLVE_LLM_API_KEY', config.get('OPENAI_API_KEY', ''))
    os.environ.setdefault('HEVOLVE_LLM_MODEL_NAME', config.get('OPENAI_MODEL', 'gpt-4'))
elif _active_cloud and os.environ.get('HEVOLVE_LLM_API_KEY'):
    # Wizard-configured cloud provider (flat mode desktop user).
    # Vault already populated HEVOLVE_LLM_* env vars via export_to_env() in app.py.
    # Crawl4AI's create_learning_llm_config() reads these automatically.
    pass
# Cloud fallback for adaptive routing (flat mode — offload when local CPU overloaded)
if config.get('CLOUD_FALLBACK_URL'):
    os.environ.setdefault('HEVOLVE_CLOUD_FALLBACK_URL', config['CLOUD_FALLBACK_URL'])
    os.environ.setdefault('HEVOLVE_CLOUD_FALLBACK_KEY', config.get('CLOUD_FALLBACK_KEY', ''))
    os.environ.setdefault('HEVOLVE_CLOUD_FALLBACK_MODEL', config.get('CLOUD_FALLBACK_MODEL', 'gpt-4'))
# Zep removed — replaced by SimpleMem (local, zero-latency)
# ZEP_API_URL / ZEP_API_KEY no longer needed
# API endpoints — fall back to IP_ADDRESS sub-dict or empty when keys missing.
# In bundled Nunba mode the config.json is URL-template format with IP_ADDRESS dict;
# in cloud/dev mode it has flat top-level keys.
_ip = config.get('IP_ADDRESS', {})
GPT_API = config.get('GPT_API', _ip.get('gpt3_url', ''))
STUDENT_API = config.get('STUDENT_API', '')
ACTION_API = config.get('ACTION_API', _ip.get('database_url', ''))
FAV_TEACHER_API = config.get('FAV_TEACHER_API', '')
DREAMBOOTH_API = config.get('DREAMBOOTH_API', '')
STABLE_DIFF_API = config.get('STABLE_DIFF_API', '')
LLAVA_API = config.get('LLAVA_API', '')
BOOKPARSING_API = config.get('BOOKPARSING_API', '')
CRAWLAB_API = config.get('CRAWLAB_API', '')
RAG_API = config.get('RAG_API', '')
DB_URL = config.get('DB_URL', _ip.get('database_url', ''))

# ============================================================================
# Embodied AI Learning Pipeline (crawl4ai — in-process, no extra port)
# ============================================================================
_learning_provider = None
_hive_mind = None
_trace_recorder = None


def _is_bundled() -> bool:
    """Detect whether we are pip-installed inside Nunba (flat mode).

    When Nunba imports us via ``hevolve_backend_adapter``, that module is
    already in ``sys.modules`` by the time our daemon thread runs.  In
    standalone mode (``python langchain_gpt_api.py``, ``start_with_tracing.bat``)
    it is absent.  No env-vars or mode flags required.
    """
    return 'hevolve_backend_adapter' in sys.modules


def _has_cloud_api() -> bool:
    """Return True if the user configured an external cloud / API endpoint."""
    return bool(os.environ.get('HEVOLVE_LLM_ENDPOINT_URL', '').strip())


def _wait_for_llm_server(url=None, timeout=15):
    if url is None:
        _port = os.environ.get('LLAMA_CPP_PORT', '8080')
        url = f'http://localhost:{_port}'
    """Wait for llama.cpp server, giving parent process time to start it.

    In Nunba (flat mode), the desktop app starts llama.cpp in a background
    thread.  This function polls the health endpoint so crawl4ai sees an
    existing server and reuses it instead of auto-starting a second one.

    In standalone mode nobody else starts the server, so after *timeout*
    seconds we return False and let crawl4ai auto-start as usual.

    Returns True if server found, False if timeout expired.
    """
    import urllib.request
    import urllib.error
    _logger = logging.getLogger(__name__)
    for i in range(timeout):
        try:
            req = urllib.request.urlopen(f'{url}/health', timeout=2)
            if req.status == 200:
                _logger.info(
                    f"[EmbodiedAI] llama.cpp server ready at {url} "
                    f"(waited {i}s)")
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    _logger.info(
        f"[EmbodiedAI] No server on {url} after {timeout}s "
        "\u2014 crawl4ai will auto-start")
    return False


def _init_learning_pipeline():
    """Initialize crawl4ai's learning pipeline in-process.

    Instead of starting a separate server on port 8000,
    we import and initialize the learning components directly.
    world_model_bridge calls these functions without HTTP overhead.

    Behaviour depends on context (auto-detected, no env vars needed):

    **Bundled (Nunba, flat mode):**
        Nunba owns the llama.cpp lifecycle.  We wait up to 30 s for
        Nunba to start it.  If it appears we reuse it.  If it never
        appears we skip the learning provider entirely — chat still
        works, only RL-EF / hivemind is disabled.  We NEVER auto-start
        a second server on the user's machine.

    **Standalone (start_with_tracing.bat, ``python langchain_gpt_api.py``):**
        Brief 5 s wait (in case user already has llama.cpp running).
        If nothing responds, ``create_learning_llm_config()`` calls
        crawl4ai which auto-starts its own server.  Default behaviour,
        no mode config needed.

    **Cloud API configured (``HEVOLVE_LLM_ENDPOINT_URL``):**
        Skip local server wait entirely — crawl4ai's Priority 0 path
        routes to the external endpoint.
    """
    global _learning_provider, _hive_mind, _trace_recorder

    try:
        from crawl4ai.embodied_ai.rl_ef import (
            create_learning_llm_config,
            register_learning_provider,
        )
        from crawl4ai.embodied_ai.monitoring.trace_recorder import get_trace_recorder
        from crawl4ai.embodied_ai.learning.hive_mind import HiveMind, AgentCapability

        _logger = logging.getLogger(__name__)
        bundled = _is_bundled()
        cloud = _has_cloud_api()
        _logger.info(
            f"[EmbodiedAI] Initializing learning pipeline "
            f"(bundled={bundled}, cloud_api={cloud})...")

        # ── Decide how to handle the local llama.cpp server ──
        if cloud:
            # Cloud endpoint configured — crawl4ai uses it directly,
            # no need to wait for or start a local server.
            _logger.info(
                "[EmbodiedAI] Cloud API configured — skipping local server wait")
        elif bundled:
            # Nunba owns the llama.cpp lifecycle (port 8080).
            # Wait generously, but NEVER auto-start a second server.
            server_found = _wait_for_llm_server(timeout=30)
            if not server_found:
                _logger.warning(
                    "[EmbodiedAI] Nunba's llama.cpp not ready after 30 s "
                    "— learning disabled (chat still works)")
                return
        else:
            # Standalone — brief courtesy wait then let crawl4ai auto-start.
            _wait_for_llm_server(timeout=5)

        # Trace recorder
        recordings_dir = os.path.join(
            os.path.expanduser('~'), '.crawl4ai', 'recordings')
        os.makedirs(recordings_dir, exist_ok=True)
        _trace_recorder = get_trace_recorder(recordings_dir)

        # Learning provider (wraps llama.cpp on port 8080 or cloud endpoint)
        _domain = 'general'
        _wizard_key = os.environ.get('HEVOLVE_LLM_API_KEY', '') or None
        llm_config = create_learning_llm_config(
            domain=_domain, fallback_api_key=_wizard_key)
        if '_provider' in llm_config:
            _learning_provider = llm_config['_provider']
            register_learning_provider(_domain, _learning_provider)
            _logger.info(
                "[EmbodiedAI] Learning provider ready (RL-EF + episodic memory)")
        else:
            _logger.warning(
                "[EmbodiedAI] Learning provider init returned no provider")

        # HiveMind
        import uuid
        instance_id = f"hevolve_{uuid.uuid4().hex[:8]}"
        _hive_mind = HiveMind(max_agents=100)
        _hive_mind.register_agent(
            agent_id=instance_id,
            agent_type='hevolve_orchestrator',
            latent_dim=2048,
            capabilities=[
                AgentCapability.TEXT_GENERATION, AgentCapability.REASONING],
        )
        _logger.info(f"[EmbodiedAI] HiveMind registered as {instance_id}")

    except ImportError as e:
        logging.getLogger(__name__).warning(
            f"[EmbodiedAI] crawl4ai not installed — learning disabled: {e}")
    except Exception as e:
        logging.getLogger(__name__).error(
            f"[EmbodiedAI] Learning pipeline init failed: {e}")


def get_learning_provider():
    """Get the in-process learning provider (for world_model_bridge)."""
    return _learning_provider


def get_hive_mind():
    """Get the in-process HiveMind instance (for world_model_bridge)."""
    return _hive_mind


# Boot learning pipeline in background (don't block Flask startup)
threading.Thread(
    target=_init_learning_pipeline, daemon=True,
    name='embodied_ai_init').start()


# ============================================================================
# Vision Pipeline (VisionService — MiniCPM sidecar + FrameStore)
# ============================================================================
_vision_service = None


def _init_vision_service():
    """Start VisionService in standalone mode only.

    In bundled mode (Nunba), Nunba owns the VisionService lifecycle
    and stores it at ``__main__._vision_service``.  We skip here to
    prevent double-start.
    """
    global _vision_service
    if _is_bundled():
        logging.getLogger(__name__).info(
            "[Vision] Bundled mode — Nunba owns VisionService lifecycle")
        return

    try:
        from integrations.vision import VisionService
        _vision_service = VisionService()
        _vision_service.start()
        logging.getLogger(__name__).info(
            "[Vision] VisionService started (standalone mode)")
    except ImportError:
        logging.getLogger(__name__).info(
            "[Vision] vision module not available — disabled")
    except Exception as e:
        logging.getLogger(__name__).warning(
            f"[Vision] VisionService failed to start: {e}")


def get_vision_service():
    """Get the active VisionService instance.

    Checks module-level var first, then falls back to Nunba's
    ``__main__._vision_service`` for bundled mode.
    """
    if _vision_service is not None:
        return _vision_service
    # Bundled mode: Nunba stores it on __main__
    main_mod = sys.modules.get('__main__')
    if main_mod:
        return getattr(main_mod, '_vision_service', None)
    return None


def get_frame_store():
    """Get the active FrameStore (from VisionService)."""
    svc = get_vision_service()
    return svc.store if svc else None


def _wire_vision_to_learning():
    """Connect FrameStore to crawl4ai's video learning pipeline.

    Waits up to 60s for both VisionService and LearningLLMProvider,
    then calls start_video_learning().
    """
    _logger = logging.getLogger(__name__)
    for _ in range(60):
        svc = get_vision_service()
        if svc and _learning_provider:
            break
        time.sleep(1)
    else:
        _logger.info(
            "[Wiring] Vision or learning not ready after 60s — skipping")
        return

    try:
        if hasattr(_learning_provider, 'start_video_learning'):
            _learning_provider.start_video_learning(
                frame_store=svc.store,
                frame_store_user_id='default',
            )
            _logger.info(
                "[Wiring] FrameStore → crawl4ai video learning connected")
        else:
            _logger.info(
                "[Wiring] LearningProvider has no start_video_learning — skip")
    except Exception as e:
        _logger.warning(f"[Wiring] FrameStore→learning failed: {e}")


# Boot vision pipeline in background
threading.Thread(
    target=_init_vision_service, daemon=True,
    name='vision_init').start()

# Wire FrameStore to crawl4ai after both subsystems are ready
threading.Thread(
    target=_wire_vision_to_learning, daemon=True,
    name='vision_learning_wire').start()


# ============================================================================
# Speaker Diarization Pipeline (sidecar subprocess)
# ============================================================================
_diarization_service = None


def _init_diarization_service():
    """Start DiarizationService in standalone mode only.

    In bundled mode (Nunba), Nunba owns the lifecycle.
    Skips if HEVOLVE_DIARIZATION_URL is already set (external service).
    """
    global _diarization_service
    if _is_bundled():
        logging.getLogger(__name__).info(
            "[Diarization] Bundled mode — Nunba owns lifecycle")
        return

    # If user already configured an external diarization URL, don't start sidecar
    if os.environ.get('HEVOLVE_DIARIZATION_URL', '').strip():
        logging.getLogger(__name__).info(
            "[Diarization] External URL configured — skipping sidecar")
        return

    try:
        from integrations.audio import DiarizationService
        _diarization_service = DiarizationService()
        _diarization_service.start()
        logging.getLogger(__name__).info(
            "[Diarization] DiarizationService starting (standalone mode)")
    except ImportError:
        logging.getLogger(__name__).info(
            "[Diarization] audio module not available — disabled")
    except Exception as e:
        logging.getLogger(__name__).warning(
            f"[Diarization] DiarizationService failed to start: {e}")


def get_diarization_service():
    """Get the active DiarizationService instance."""
    if _diarization_service is not None:
        return _diarization_service
    main_mod = sys.modules.get('__main__')
    if main_mod:
        return getattr(main_mod, '_diarization_service', None)
    return None


# Boot diarization sidecar in background
threading.Thread(
    target=_init_diarization_service, daemon=True,
    name='diarization_init').start()

# task scheduling and logging


class TaskStatus(Enum):
    INITIALIZED = "INITIALIZED"
    SCHEDULED = "SCHEDULED"
    EXECUTING = "EXECUTING"
    TIMEOUT = "TIMEOUT"
    COMPELETED = "COMPELETED"
    ERROR = "ERROR"


class TaskNames(Enum):
    GET_ACTION_USER_DETAILS = "GET_ACTION_USER_DETAILS"
    GET_TIME_BASED_HISTORY = "GET_TIME_BASED_HISTORY"
    ANIMATE_CHARACTER = "ANIMATE_CHARACTER"
    STABLE_DIFF = "STABLE_DIFF"
    LLAVA = "LLAVA"
    CRAWLAB = "CRAWLAB"
    USER_ID_RETRIEVER = "USER_ID_RETRIEVER"


# google search API — graceful when GOOGLE_API_KEY is missing (e.g. local-only mode)
try:
    search = GoogleSearchAPIWrapper(k=4)
except Exception as e:
    logging.getLogger(__name__).info(f"Google Search unavailable (expected in local mode): {e}")
    search = None

# constants
# llm = ChatOpenAI(model_name="gpt-3.5-turbo-16k")
# llm = ChatOpenAI(temperature=0, model="gpt-4")
# llm = CustomGPT()
# The above code is creating an instance of the `LLMMathChain` class with an `open_ai_llm` attribute
# initialized with a `ChatOpenAI` object using the model name "gpt-3.5-turbo".

# llm_math = LLMMathChain(ChatOpenAI(model_name="gpt-3.5-turbo"))
# Old: llm_math = LLMMathChain(llm=ChatOpenAI(model_name="gpt-3.5-turbo"))
# New: Using get_llm() to automatically use Qwen3-VL or OpenAI based on USE_QWEN3VL flag
try:
    llm_math = LLMMathChain(llm=get_llm(model_name="gpt-3.5-turbo"))
except Exception:
    llm_math = None
# llm_math = LLMMathChain(llm= ChatGroq(groq_api_key=groq_api_key,
#                model_name = "mixtral-8x7b-32768"))

# llm = ChatGroq(groq_api_key=groq_api_key,
#                model_name="llama-3.1-8b-instant", temperature=0.3)

# app.logger.info(llm.invoke("hi how are you?"))

if spec is not None:
    try:
        chain = get_openapi_chain(spec)
    except Exception as e:
        app.logger.warning(f"Could not create OpenAPI chain: {e}")
        chain = None
else:
    chain = None


client = crossbarhttp.Client('http://aws_rasa.hertzai.com:8088/publish') if crossbarhttp else None

# Create thread pool executor for async Crossbar publishing
crossbar_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='crossbar_publish')
atexit.register(lambda: crossbar_executor.shutdown(wait=False))

def publish_async(topic, message, timeout=2.0):
    """
    Publish to Crossbar in a background thread without blocking the main request.

    Args:
        topic: Crossbar topic to publish to
        message: Message payload (dict)
        timeout: Maximum time to wait for publish (default: 2.0 seconds)
    """
    if client is None:
        return

    def _publish():
        import socket
        try:
            original_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(timeout)
            client.publish(topic, message)
            app.logger.debug(f"Successfully published to {topic}")
        except Exception as e:
            app.logger.error(f"Error publishing to {topic}: {e}")
        finally:
            if original_timeout is not None:
                socket.setdefaulttimeout(original_timeout)

    crossbar_executor.submit(_publish)


# create prompt
def create_prompt(tools):
    user_details, actions = get_action_user_details(
        user_id=thread_local_data.get_user_id())
    prefix = f"""Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.

        <GENERAL_INSTRUCTION_START>
        Context:
        Imagine that you are the world's leading teacher, possessing knowledge in every field. Consider the consequences of each response you provide.
        Your answers must be meaningful and delivered as quickly as possible. As a highly educated and informed teacher, you have access to an extensive wealth of information.
        Your primary goal as a teacher is to assist students by answering their questions, providing accurate and up-to-date information.
        Please create a distinct personality for yourself, and remember never to refer to the user as a human or yourself as mere AI.\
        your response should not be more than 200 words.
        {get_cultural_prompt_compact()}
        <GENERAL_INSTRUCTION_END>
        User details:
        <USER_DETAILS_START>
        {user_details}
        <USER_DETAILS_END>
        <CONTEXT_START>
        Before you respond, consider the context in which you are utilized. You are Hevolve, a highly intelligent educational AI developed by HertzAI.
        You are designed to answer questions, provide revisions, conduct assessments, teach various topics, create personalised curriculum and assist with research for both students and working professionals.
        Your expertise draws from various knowledge sources like books, websites, and white papers. Your responses will be conveyed to the user through a video, using an avatar and text-to-speech technology, and can be translated into various languages.
        Consider the user's location, time and context of previous dialogues with time to create a proper prompt for tools and follow up in-context questions.You have ability to see using Visual_Context_Camera tool.
        If your response contains abbreviated words, please separate them with spaces, like T T S.
        <CONTEXT_END>
        These are all the actions that the user has performed up to now:
        <PREVIOUS_USER_ACTION_START>
        {actions}

        Conversation History:
        <HISTORY_START>
        """
    suffix = """
        <HISTORY_END>
        Only if this above conversation history is not sufficient to fulfill the user's request then use below FULL_HISTORY tool. Important: If results can be accomplished with above information skip tools section and move to format instructions.

        TOOLS

        ------

        Assistant can use tools to look up information that may be helpful in answering the user's
        question. The tools you can use are:

        <TOOLS_START>
        {{tools}}
        <TOOLS_END>
        <FORMAT_INSTRUCTION_START>
        {format_instructions}
        <FORMAT_INSTRUCTION_END>

        always create parsable output.

        Here is the User and AI conversation in reverse chronological order:

        USER'S INPUT:
        -------------
        <USER_INPUT_START>
        Latest USER'S INPUT For which you need to respond (consult recent history only when needed for more context): {{{{input}}}}
        <USER_INPUT_END>
        """

    prompt = ConversationalChatAgent.create_prompt(
        tools,
        system_message=prefix,
        human_message=suffix,
        input_variables=["input", "agent_scratchpad", "chat_history"]
    )
    # prompt_string = prompt.render()
    # prompt.rende
    return prompt


def _handle_create_agent_tool(input_text):
    """Tool handler: LLM decided user wants to create an agent.

    Called by the LangChain agent when it detects agent creation intent.
    Sets thread-local flags that the /chat handler checks after get_ans() returns.
    """
    lower = input_text.lower()
    autonomous = any(w in lower for w in [
        'autonomous', 'automatic', 'automatically', 'do it for me',
        'handle it', 'just create', 'create it yourself', 'auto',
    ])
    thread_local_data.set_creation_requested(description=input_text, autonomous=autonomous)
    if autonomous:
        return f"Agent creation initiated autonomously for: {input_text}. I will set up the agent creation workflow and handle all the details automatically."
    return f"Agent creation initiated for: {input_text}. I will set up the agent creation workflow. Let me gather the required details."


# Signals from reuse agent that suggest creating a new agent
_RESPONSE_CREATION_SIGNALS = [
    'need a new agent', 'create a new agent', 'requires a different agent',
    'beyond my capabilities', 'specialized agent', 'need a specialized',
    'suggest creating', 'recommend creating a new',
]

def _response_signals_creation(response_text):
    """Check if the reuse agent's response suggests creating a new agent."""
    lower = response_text.lower()
    return any(signal in lower for signal in _RESPONSE_CREATION_SIGNALS)


def get_tools(req_tool, is_first: bool = False):

    if is_first:
        tools = load_tools(["google-search"])
        tool = [

            Tool(
                name='Calculator',
                func=llm_math.run,
                description='Useful for when you need to answer questions about math.'
            ),
        ]

        # Only add OpenAPI tool if chain is initialized
        if chain is not None:
            tool.append(Tool(
                name="OpenAPI_Specification",
                func=chain.run,
                description="Use this feature only when the user's request specifically pertains to one of the following scenarios:\
                Image Creation: When a request involves generating an image using text, this feature should be engaged. The entire text prompt must be used as it is unless otherwise requested to enhance further detail of prompt for the image generation process. If additional enahancement is needed , enrich the prompt to image generation with greater detail for learning.\
                Student Information: If a request is made for information regarding students, this functionality should be utilized to retrieve the necessary details.\
                Query Available Books: When the user is inquiring about available books, this feature should be used to locate and provide information about the required texts.\
                Any CRUD operation which is not a READ or anything related to curriculum should not use this tool,  It is vital to ensure that the intent precisely falls within one of the above  categories before engaging this functionality.\
                Don't use this to create a custom curriculum for user",
            ))

        tool += [
            Tool(
                name="FULL_HISTORY",
                func=parsing_string,
                description=f"""Utilize this tool exclusively when the information required predates the current day & pertains to the ongoing user query or when there is a need to recall certain things we spoke earlier. The necessary input for this tool comprises a list of values separated by commas.
                The list should encompass a user-generated query, designated by user input text, a commencement date denoted as start_date, and an end date labeled as end_date. The start_date denotes the initiation date for the user information search and should consistently adhere to the ISO 8601 format. Meanwhile, the end_date, also conforming to the ISO 8601 format, signifies the conclusion date for the search.
                In cases where the end_date is indeterminable, the current datetime should be employed. For example, if the objective is to retrieve a user's dialogue spanning from the preceding day up to the present day (assuming today's date is 2023-07-13T10:19:56.732291Z), the input would resemble: 'what we discussed about the project, 2023-07-12T10:19:56.00000Z, 2023-07-13T10:19:56.732291Z'. If query has any form of date or time by user, then start end datetime can be exact rather than till today for more accurate results. Remove any references to time based words (e.g. yesterday, today, datetimes, last year) since the date range you provide already accounts for that. e.g. if user has asked what did we discuss the day before yesterday then the text argument should just be empty since it does not have any named entity for fuzzy search followed by start and end datetime.
                Strive to apply this tool judiciously for scenarios in which retrospective user information is imperative. If Full history tool response is present, forget other histories, the inputs should be meticulously arranged to facilitate the extraction of accurate and pertinent data within the specified timeframe. Never use this tool for what is the response to my last comment?
                Remember whatever user query is regarding search history understand what user is asking about and rephrase it properly then send to tool. Before framing the final tool response from this tool consult corresponding created_at date time to give more accurate response"""
            ),
            Tool(
                name="Text to image",
                func=parse_text_to_image,
                description="Based on user query generate visual representation of text. Extract prompt from user query and use it as input for function"
            ),
            Tool(
                name="Animate_Character",
                func=parse_character_animation,
                description='''Use this tool exclusively for animating the selected character or teacher as requested by the user. The user should specify their animation request in a query, such as 'Show me in a spacesuit' or 'Animate yourself as a cartoon standing in front of the Taj Mahal.' This tool handles requests involving animating a pre-selected character and should not be used for general image generation tasks. For example, use it for 'Show me a picture of yourself dancing in the rain' but not for 'Generate an image of a sunset.' input'''
            ),
            Tool(
                name="Image_Inference_Tool",
                func=parse_image_to_text,
                description='''When a user provides a query containing an image download URL and a related question about that image, utilize this tool for support. Your objective is to extract both the image URL and the user's inquiry or prompt pertaining to that image from their query, and then convert these elements into comma seperated string. The format should be as follows: "image_url, user_query".
                '''
            ),
            Tool(
                name="Data_Extraction_From_URL",
                func=parse_link_for_crwalab,
                description='''
                Your task is to extract a URL and its type (either 'pdf' or 'website') from a user's query. Upon receiving a query that contains a URL and a specified URL type, you are to use a tool designed for this purpose. The objective is to accurately identify both the URL and its type from the query. Once identified, these elements should be formatted into a comma-separated string, adhering to the format: "url, url_type".
                '''
            ),
            Tool(
                name="User_details_tool",
                func=parse_user_id,
                description="If a request is made for information regarding students or users, this functionality should be utilized to retrieve the necessary details. input for this api should Always be current user_id. Except current user id you should say you cannot have access other user's details."
            ),
            Tool(
                name="Visual_Context_Camera",
                func=parse_visual_context,
                description="To see user or if there is a need to look at user camera feed for vision and understanding scene, visual question answering, seeing user, recognise visual objects and activity then this should be utilised. Input to this tool function should be the user query/input. Only if last 16 seconds Visual Context information is present & is enough, then use that to craft a better creative, better, cohesive, correlated , summarised natural response, format this tool response togather with Previous 15 minutes Visual Context information if you are seeing the scene via videocall from the other end. If there are more than 1 person try to give an identity to each across frames to track the subjects through time by framing the tool input accordingly."
            ),
            Tool(
                name="Create_Agent",
                func=_handle_create_agent_tool,
                description=(
                    "Use this tool when the user wants to create, build, set up, train, or deploy "
                    "a new AI agent, assistant, bot, or automated workflow. "
                    "Input should be the description of what the agent should do. "
                    "Do NOT use this tool if the user is just asking ABOUT agents or discussing agents in general. "
                    "Only use when the user explicitly wants a NEW agent created. "
                    "If the user also says words like 'automatically', 'autonomous', 'do it for me', "
                    "'handle it', 'just create it', include those keywords in your input."
                ),
            )

        ]

        # Service Tools: Add HTTP microservice tools (Crawl4AI, AceStep, etc.)
        try:
            from integrations.service_tools import service_tool_registry
            tool += service_tool_registry.get_langchain_tools()
        except ImportError:
            pass

        # Hyve Skills: Ingest agent skills (Claude Code, Markdown, GitHub)
        try:
            from integrations.skills import skill_registry
            tool += skill_registry.get_langchain_tools()
        except ImportError:
            pass

        # Memory Tools: Add MemoryGraph-backed tools (remember, recall, backtrace)
        try:
            user_id = thread_local_data.get_user_id()
            prompt_id = thread_local_data.get_prompt_id()
            graph = _get_or_create_graph(user_id, prompt_id)
            if graph:
                from integrations.channels.memory.agent_memory_tools import (
                    create_memory_tools, create_langchain_tools,
                )
                session_id = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
                mem_tools_dict = create_memory_tools(graph, str(user_id), session_id)
                lc_mem_tools = create_langchain_tools(mem_tools_dict)
                tool += lc_mem_tools
        except Exception:
            pass  # Non-blocking — memory tools are optional

        tools += tool
        return tools

    else:
        tools_dict = {1: 'google_search', 2: 'Calculator', 3: 'OpenAPI_Specification', 4: 'FULL_HISTORY', 5: 'Text to image',
                      6: 'Image_Inference_Tool', 7: 'Data_Extraction_From_URL', 8: 'User_details_tool', 9: 'Visual_Context_Camera'}
        tool_desc = {
            'google_search': '''Search Google for recent results and retrieve URLs that are suitable for web crawling. Ensure that the search responses include the source URL from which the data was extracted. Always present this URL in the response as an HTML anchor tag. This approach ensures clear attribution and easy navigation to the original source for each piece of extracted information. Give urls for the source''',
            'Calculator': '''Useful for when you need to answer questions about math.''',
            'OpenAPI_Specification': '''Use this feature only when the user's request specifically pertains to one of the following scenarios:\
                Image Creation: When a request involves generating an image using text, this feature should be engaged. The entire text prompt must be used as it is unless otherwise requested to enhance further detail of prompt for the image generation process. If additional enahancement is needed , enrich the prompt to image generation with greater detail for learning.\
                Student Information: If a request is made for information regarding students, this functionality should be utilized to retrieve the necessary details.\
                Query Available Books: When the user is inquiring about available books, this feature should be used to locate and provide information about the required texts.\
                Any CRUD operation which is not a READ or anything related to curriculum should not use this tool,  It is vital to ensure that the intent precisely falls within one of the above  categories before engaging this functionality.\
                Don't use this to create a custom curriculum for user''',
            'FULL_HISTORY': '''Utilize this tool exclusively when the information required predates the current day & pertains to the ongoing user query or when there is a need to recall certain things we spoke earlier. The necessary input for this tool comprises a list of values separated by commas.
                The list should encompass a user-generated query, designated by user input text, a commencement date denoted as start_date, and an end date labeled as end_date. The start_date denotes the initiation date for the user information search and should consistently adhere to the ISO 8601 format. Meanwhile, the end_date, also conforming to the ISO 8601 format, signifies the conclusion date for the search.
                In cases where the end_date is indeterminable, the current datetime should be employed. For example, if the objective is to retrieve a user's dialogue spanning from the preceding day up to the present day (assuming today's date is 2023-07-13T10:19:56.732291Z), the input would resemble: 'what we discussed about the project, 2023-07-12T10:19:56.00000Z, 2023-07-13T10:19:56.732291Z'. If query has any form of date or time by user, then start end datetime can be exact rather than till today for more accurate results. Remove any references to time based words (e.g. yesterday, today, datetimes, last year) since the date range you provide already accounts for that. e.g. if user has asked what did we discuss the day before yesterday then the text argument should just be empty since it does not have any named entity for fuzzy search followed by start and end datetime.
                Strive to apply this tool judiciously for scenarios in which retrospective user information is imperative. If Full history tool response is present, forget other histories, the inputs should be meticulously arranged to facilitate the extraction of accurate and pertinent data within the specified timeframe. Never use this tool for what is the response to my last comment?
                Remember whatever user query is regarding search history understand what user is asking about and rephrase it properly then send to tool. Before framing the final tool response from this tool consult corresponding created_at date time to give more accurate response''',
            'Text to image': '''Based on user query generate visual representation of text. Extract prompt from user query and use it as input for function''',
            'Image_Inference_Tool': '''When a user provides a query containing an image download URL and a related question about that image, utilize this tool for support. Your objective is to extract both the image URL and the user's inquiry or prompt pertaining to that image from their query, and then convert these elements into comma seperated string. The format should be as follows: "image_url, user_query".''',
            'Data_Extraction_From_URL': '''Your task is to extract a URL and its type (either 'pdf' or 'website') from a user's query. Upon receiving a query that contains a URL and a specified URL type, you are to use a tool designed for this purpose. The objective is to accurately identify both the URL and its type from the query. Once identified, these elements should be formatted into a comma-separated string, adhering to the format: "url, url_type".''',
            'User_details_tool': '''If a request is made for information regarding students or users, this functionality should be utilized to retrieve the necessary details. input for this api should Always be current user_id. Except current user id you should say you cannot have access other user's details.''',
            'Visual_Context_Camera': '''This tool captures the user's visual context during a video call, providing real-time captions. Use it for visual question answering, scene understanding, recognizing objects, activities, & monitoring the user. Input will be the user's input/query. If the last 16 seconds of visual context are available and sufficient, it crafts a creative, cohesive response. If not, inform the user of the glitch accessing the current camera feed and guess using the Last_5_Minutes_Visual_Context. Ensure responses are natural, avoiding lists of captions, and format them as if you are seeing the user scene via video call. Analyze the current tool response and previous visual context captions to recognize user activities and infer actions from multiple frames. If the user requests continuous narration without active input, adapt the response to include past, present, and future tenses for dynamic and contextually aware commentary.'''
        }
        tools_func = {
            'google_search': top5_results,
            'Calculator': llm_math.run,
            'FULL_HISTORY': parsing_string,
            'Text to image': parse_text_to_image,
            'Image_Inference_Tool': parse_image_to_text,
            'Data_Extraction_From_URL': parse_link_for_crwalab,
            'User_details_tool': parse_user_id,
            'Visual_Context_Camera': parse_visual_context
        }

        # Only add OpenAPI_Specification to tools_func if chain is initialized
        if chain is not None:
            tools_func['OpenAPI_Specification'] = chain.run
        if req_tool == "google_search":
            req_tool = "Google Search Snippets"
        if req_tool is not None and req_tool in tools_dict.values():
            tool_description = tool_desc[req_tool]
            tool_func = tools_func[req_tool]
            req_tool_from_user = [
                Tool(
                    name=req_tool,
                    func=tool_func,
                    description=tool_description
                )
            ]
            tools = load_tools(["google-search"])
            tools += req_tool_from_user

        else:
            tool_description = ""
            tool_func = ""
            tools = load_tools(["google-search"])
            # tools += req_tool_from_user

        tool = [

            Tool(
                name='Calculator',
                func=llm_math.run,
                description='Useful for when you need to answer questions about math.'
            ),
        ]

        # Only add OpenAPI tool if chain is initialized
        if chain is not None:
            tool.append(Tool(
                name="OpenAPI_Specification",
                func=chain.run,
                description="Use the specialized feature for image generation, student information retrieval, and querying available books, while avoiding its use for non-READ CRUD operations or custom curriculum creation.",
            ))

        tool += [
            Tool(
                name="FULL_HISTORY",
                func=parsing_string,
                description=f"""Use the tool for retrieving historical user information within a specified timeframe, including user-generated queries, start and end dates in ISO 8601 format, and carefully rephrase queries related to search history for accurate responses, avoiding use for responses to the last comment."""
            ),
            Tool(
                name="Text to image",
                func=parse_text_to_image,
                description="Based on user query generate visual representation of text. Extract prompt from user query and use it as input for function"
            ),
            Tool(
                name="Animate_Character",
                func=parse_character_animation,
                description='''Use this tool exclusively for animating the selected AI character or teacher as requested by the user; it is not intended for general requests or for animating random images or individuals other than AI teacher avatars. The user should specify their animation request in a query, e.g. 'Show me yourself in a spacesuit' or 'Animate yourself as a person riding a bike.' Once the request is made, the tool will generate the animation and return an URL link to the user that directs them to the animated image. This tool should not be used for general image generation tasks that don't pertain to animating the user's chosen character or teacher. For example, if a user queries 'Show me dancing in the rain,' and they have previously selected a specific character or teacher, the tool should be used to generate this animated scenario. However, if the user's request is something like 'Generate an image of a sunset,' which does not directly involve animating the selected character or teacher, then this tool should not be used.'''
            ),
            Tool(
                name="Image_Inference_Tool",
                func=parse_image_to_text,
                description='''Utilize the tool to extract and format both the image URL and the user's inquiry from a query containing an image download URL into a comma-separated string: "image_url, user_query".'''
            ),
            Tool(
                name="Data_Extraction_From_URL",
                func=parse_link_for_crwalab,
                description='''Utilize the designated tool to extract and format a URL and its type (either 'pdf' or 'website') from a user's query into a comma-separated string: "url, url_type".'''
            ),
            Tool(
                name="User_details_tool",
                func=parse_user_id,
                description="Utilize this functionality to retrieve information about students or users, requiring the current user_id as the only acceptable input; access to other user details is not allowed."
            ),
            Tool(
                name="Visual_Context_Camera",
                func=parse_visual_context,
                description="This tool captures the user's visual context during a video call, providing real-time captions. Use it for visual question answering, scene understanding, recognizing objects, activities, & monitoring the user. Input will be the user's input/query. If the last 16 seconds of visual context are available and sufficient, it crafts a creative, cohesive response. If not, inform the user of the glitch accessing the current camera feed and guess using the Last_5_Minutes_Visual_Context. Ensure responses are natural, avoiding lists of captions, and format them as if you are seeing the user scene via video call. Analyze the current tool response and previous visual context captions to recognize user activities and infer actions from multiple frames. If the user requests continuous narration without active input, adapt the response to include past, present, and future tenses for dynamic and contextually aware commentary."
            ),
            Tool(
                name="Create_Agent",
                func=_handle_create_agent_tool,
                description=(
                    "Use this tool when the user wants to create, build, set up, train, or deploy "
                    "a new AI agent, assistant, bot, or automated workflow. "
                    "Input should be the description of what the agent should do. "
                    "Do NOT use this tool if the user is just asking ABOUT agents or discussing agents in general. "
                    "Only use when the user explicitly wants a NEW agent created. "
                    "If the user also says words like 'automatically', 'autonomous', 'do it for me', "
                    "'handle it', 'just create it', include those keywords in your input."
                ),
            )

        ]
        final_tool = []
        for new_tool in tool:
            if new_tool not in tools:
                final_tool.append(new_tool)

        tools += final_tool

        tool_strings = "\n".join(
            f"\n> {tool.name}: {tool.description}" for tool in tools)
        return tool_strings

# custom GPT


SUPPORTED_LANG_DICT = {
    "ar": "Arabic",
    "bg": "Bulgarian",
    "zh": "Chinese",
    "zh-cn": "Chinese (Simplified)",
    "nl": "Dutch",
    "fi": "Finnish",
    "fr": "French",
    "de": "German",
    "el": "Greek",
    "he": "Hebrew",
    "hu": "Hungarian",
    "is": "Icelandic",
    "id": "Indonesian",
    "ko": "Korean",
    "lv": "Latvian",
    "ms": "Malay",
    "fa": "Persian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "es": "Spanish",
    "sw": "Swahili",
    "sv": "Swedish",
    "th": "Thai",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "vi": "Vietnamese",
    "cy": "Welsh",
    "hi": "Hindi",
    "bn": "Bengali",
    "ta": "Tamil",
    "pa": "Punjabi",
    "gu": "Gujarati",
    "kn": "Kannada",
    "te": "Telugu",
    "mr": "Marathi",
    "ml": "Malayalam",
    "en": "English"
}


class CustomGPT(LLM):
    casual_conv: bool

    count: int = 0
    previous_intent: Optional[str] = None
    call_gpt4: Optional[int] = 0
    total_tokens: int = 0

    @property
    def _llm_type(self) -> str:
        return "custom"

    def _call(self, prompt: str, stop: Optional[List[str]] = None) -> str:
        start_time = time.time()
        self.count += 1
        # self.total_tokens = 0
        app.logger.info(f'calling for {self.count} times')

        app.logger.info(f"len---->{len(prompt.split(' '))}")
        # encoding = tiktoken.get_encoding("gpt-3.5-turbo")
        num_tokens = len(encoding.encode(prompt)) if encoding else len(prompt.split())
        thread_local_data.update_req_token_count(num_tokens)
        app.logger.info(f"len---->{num_tokens}")

        app.logger.info(f"first time calling {len(prompt)}")

        if self.count > 1 and thread_local_data.get_global_intent() != self.previous_intent:
            tools = get_tools(thread_local_data.get_global_intent())
            start_index = prompt.find("<TOOLS_START>")
            end_index = prompt.find("<TOOLS_END>") + len("<TOOLS_END>")
            prompt = prompt[:start_index] + tools + prompt[end_index:]
            app.logger.info(f"second time calling {len(prompt)}")

            # prompt = create_prompt(tools)
            app.logger.info(prompt)
            # time.sleep(10)

        checker = None
        # structured_llm = llm.with_structured_output(
        #     method="json_mode",
        #     include_raw=True
        # )
        if (self.count > 1 or self.call_gpt4 == 1):

            # try:
            #     # app.logger.info(f"the prompt we are sending is {prompt}")

            #     start = time.time()
            #     response = pooled_post(
            #         GPT_API,
            #         json={

            #             "model": "gpt-4.1-mini",
            #             "data": [{"role": "user", "content": prompt}],
            #             "max_token": 2000,
            #             "request_id": str(thread_local_data.get_request_id())
            #         })
            #     app.logger.info(
            #         f"gpt 3.5 response format is {response.json()}")
            #     app.logger.info(
            #         f"gpt 3.5 response format type is {type(response.json())}")
            #     app.logger.info(
            #         " gpt 3.5 finish in {}".format(time.time()-start))
            #     checker = 1

            # except:
            #     app.logger.info("gpt 3.5 fails on line number 483!!")
            try:
                if self.casual_conv:
                    app.logger.info(f"casual conv!")
                    start = time.time()
                    response = pooled_post(
                        GPT_API,
                        json={
                            "model": "llama",
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 200,  # Reduced from 1000 for faster responses
                            "temperature": 0.7
                        })
                    app.logger.info(
                        f"gpt 3.5 response format is {response.json()}")
                    app.logger.info(
                        f"gpt 3.5 response format type is {type(response.json())}")
                    app.logger.info(
                        " gpt 3.5 finish in {}".format(time.time()-start))
                    checker = 1

                else:
                    app.logger.info("Non casual conv")
                    start = time.time()
                    response = pooled_post(
                        GPT_API,
                        json={
                            "model": "llama",
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 200,  # Reduced from 1000 for faster responses
                            "temperature": 0.7
                        })
                    app.logger.info(
                        f"gpt 3.5 response format is {response.json()}")
                    app.logger.info(
                        f"gpt 3.5 response format type is {type(response.json())}")
                    app.logger.info(
                        " gpt 3.5 finish in {}".format(time.time()-start))
                    checker = 1

                    # # `response_from_groq`.

                    # response_from_groq = structured_llm.invoke(prompt)
                    # # app.logger.info("groq response in streaming way")
                    # # app.logger.info("\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n")
                    # # response_from_groq = ""
                    # # for chunk in llm.stream(prompt):
                    # #     app.logger.info(f"chunk in stream {chunk}")
                    # #     app.logger.info(f"chunk content in straming way {chunk.content}")
                    # #     response_from_groq +=chunk.content

                    # # app.logger.info("\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n")
                    # # app.logger.info(f" response from groq api {response_from_groq}")
                    # # app.logger.info(f" response from groq api {type(response_from_groq)}")

                    # app.logger.info(
                    #     "finish in groq {}".format(time.time()-start))
                    # response = response_from_groq['raw'].content
                    # response_from_groq = response_from_groq['raw'].content
                    # # response = json.loads(response_from_groq.content)
                    # # response = json.dumps(response)
                    # app.logger.info(
                    #     f" response from groq api after {response}")
                    # app.logger.info(
                    #     f" response from groq api after {type(response)}")
                    # checker = 0
            except Exception as e:
                app.logger.info(f"In except the exception is {e}")
                start = time.time()
                response = pooled_post(
                    GPT_API,
                    json={
                        "model": "llama",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1000
                    })
                app.logger.info(
                    f"gpt 3.5 response format is {response.json()}")
                app.logger.info(
                    f"gpt 3.5 response format type is {type(response.json())}")
                app.logger.info("finish in {}".format(time.time()-start))
                checker = 1
        else:
            try:
                # app.logger.info(f"the prompt we are sending is {prompt}")
                if self.casual_conv:

                    app.logger.info(
                        f"the casual conv line 519 casual conv {self.casual_conv} type of casual conv {type(self.casual_conv)}")
                    start = time.time()

                    response = pooled_post(
                        GPT_API,
                        json={
                            "model": "llama",
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 1000
                        }
                    )
                    app.logger.info(
                        f"gpt 3.5 response format is {response.json()}")
                    app.logger.info(
                        f"gpt 3.5 response format type is {type(response.json())}")
                    app.logger.info(
                        "gpt 3.5 finish in {}".format(time.time()-start))
                    checker = 1
                else:
                    try:
                        app.logger.info("non casual conv")
                        start = time.time()
                        response = pooled_post(
                            GPT_API,
                            json={
                                "model": "llama",
                                "messages": [{"role": "user", "content": prompt}],
                                "max_tokens": 1000
                            }
                        )
                        app.logger.info(
                            f"gpt 3.5 response format is {response.json()}")
                        app.logger.info(
                            f"gpt 3.5 response format type is {type(response.json())}")
                        app.logger.info(
                            "gpt 3.5 finish in {}".format(time.time()-start))
                        checker = 1

                        # response_from_groq = structured_llm.invoke(prompt)


                        # app.logger.info(
                        #     "finish in groq {}".format(time.time()-start))
                        # app.logger.info(
                        #     f"this is response from groq {response_from_groq}")
                        # response = response_from_groq['raw'].content
                        # response_from_groq = response_from_groq['raw'].content
                        # app.logger.info(
                        #     f" response from groq api after {response}")
                        # app.logger.info(
                        #     f" response from groq api after {type(response)}")
                        # checker = 0
                    except Exception as e:
                        app.logger.info(f" the error is {e}")
            except Exception as e:
                app.logger.info(f"In except the exception is {e}")
                start = time.time()

                response = pooled_post(
                    GPT_API,
                    json={
                        "model": "llama",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1000
                    }
                )
                app.logger.info(f"gpt 4 response format is {response.json()}")
                app.logger.info(
                    f"gpt 4 response format type is {type(response.json())}")
                app.logger.info("finish in {}".format(time.time()-start))
                checker = 1

        if checker == 0:
            try:
                app.logger.info(
                    f"full response that came from the gpt{response}")
                text = str(response)
                app.logger.info(f"text got from gpt {text}")
                try:
                    text = text.strip('`').replace('json\n', '').strip()
                except:
                    pass
                intents = json.loads(text)
                app.logger.info(f"the intents are: {intents}")

                curr_intent = intents["action"]
                app.logger.info(f"curr_intent is: {curr_intent}")
                if self.previous_intent == curr_intent:
                    self.call_gpt4 = 1
                self.previous_intent = curr_intent
                thread_local_data.update_recognize_intents(intents["action"])
            except Exception as e:
                app.logger.info(
                    f"Exception occur while intent calcualtion and calling exception {e}")
                # thread_local_data.update_recognize_intents("Final Answer")
            # time.sleep(10)

            end_time = time.time()
            elapsed_time = end_time - start_time
            app.logger.info(f"time taken for this call is {elapsed_time}")
            num_tokens = len(encoding.encode(
                str(response).replace('\n', ' ').replace('\t', ''))) if encoding else len(str(response).split())
            app.logger.info(f"current num_tokens: {num_tokens}")
            thread_local_data.update_res_token_count(num_tokens)
            end_result = str(response).replace('\n', ' ').replace('\t', '')
            app.logger.info(f"the end response is {end_result}")

            return 'response_from_groq'.replace('\n', ' ').replace('\t', '')
            # return response_from_groq.content.replace('\n', ' ').replace('\t', '')
        if checker == 1:
            try:
                # Extract text from OpenAI-compatible response format
                text = str(response.json()["choices"][0]["message"]["content"])
                try:
                    text = text.strip('`').replace('json\n', '').strip()
                except:
                    pass
                intents = json.loads(text)

                curr_intent = intents["action"]
                if self.previous_intent == curr_intent:
                    self.call_gpt4 = 1
                self.previous_intent = curr_intent
                thread_local_data.update_recognize_intents(intents["action"])
            except Exception as e:
                app.logger.info(
                    f"Exception occur while intent calcualtion and calling exception {e}")
                # thread_local_data.update_recognize_intents("Final Answer")
                # time.sleep(10)

            end_time = time.time()
            elapsed_time = end_time - start_time
            app.logger.info(f"time taken for this call is {elapsed_time}")
            response_text = response.json()["choices"][0]["message"]["content"]
            num_tokens = len(encoding.encode(
                response_text.replace('\n', ' ').replace('\t', ''))) if encoding else len(response_text.split())
            thread_local_data.update_res_token_count(num_tokens)
            return response_text.replace('\n', ' ').replace('\t', '')

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        return {

        }


# llm = CustomGPT(casual_conv=True)

class CustomAgentExecutor(AgentExecutor):

    def prep_outputs(self, inputs: Dict[str, str],
                     outputs: Dict[str, str],
                     metadata: Optional[Dict[str, Any]] = None,
                     return_only_outputs: bool = False) -> Dict[str, str]:
        # pdb.set_trace()
        self._validate_outputs(outputs)
        req_id = thread_local_data.get_request_id()
        prom_id = thread_local_data.get_prompt_id()
        metadata = {'request_Id': req_id, 'prompt_id': prom_id}
        app.logger.info(
            f"before: memory object is not none and metadata is {metadata}, {return_only_outputs}")
        if self.memory is not None:
            app.logger.info(
                f"memory object is not none and metadata is {metadata}")
            try:
                self.memory.save_context(inputs, outputs, metadata)
                app.logger.info(
                    f"After: memory saved successfully with metadata {metadata}")
            except Exception as e:
                app.logger.error(f"Failed to save memory: {e}")
                # Continue without crashing - memory save is not critical

            # Register conversation turn in MemoryGraph (fire-and-forget, no latency)
            try:
                user_id = thread_local_data.get_user_id()
                graph = _get_or_create_graph(user_id, prom_id)
                if graph:
                    user_input = inputs.get('input', '')
                    ai_output = outputs.get('output', '')
                    session_key = f"{user_id}_{prom_id}" if prom_id else str(user_id)
                    def _bg_register(g=graph, ui=user_input, ao=ai_output, sk=session_key):
                        try:
                            g.register_conversation('user', ui, sk)
                            g.register_conversation('langchain', ao, sk)
                        except Exception:
                            pass
                    threading.Thread(target=_bg_register, daemon=True).start()
            except Exception:
                pass  # Non-blocking
        else:
            app.logger.info(
                f"Memory object is None, skipping save")
        if return_only_outputs:
            return outputs
        else:
            return {**inputs, **outputs}

    def prep_inputs(self, inputs: Union[Dict[str, Any], Any]) -> Dict[str, str]:
        """Validate and prepare chain inputs, including adding inputs from memory.

        Args:
            inputs: Dictionary of raw inputs, or single input if chain expects
                only one param. Should contain all inputs specified in
                `Chain.input_keys` except for inputs that will be set by the chain's
                 memory.

        Returns:
            A dictionary of all inputs, including those added by the chain's memory.
        """
        if not isinstance(inputs, dict):

            _input_keys = set(self.input_keys)
            if self.memory is not None:
                # If there are multiple input keys, but some get set by memory so that
                # only one is not set, we can still figure out which key it is.
                _input_keys = _input_keys.difference(
                    self.memory.memory_variables)
            if len(_input_keys) != 1:
                raise ValueError(
                    f"A single string input was passed in, but this chain expects "
                    f"multiple inputs ({_input_keys}). When a chain expects "
                    f"multiple inputs, please call it by passing in a dictionary, "
                    "eg `chain({'foo': 1, 'bar': 2})`"
                )
            inputs = {list(_input_keys)[0]: inputs}
        if self.memory is not None:
            try:
                external_context = self.memory.load_memory_variables(inputs)
                inputs = dict(inputs, **external_context)
            except Exception as e:
                app.logger.warning(f"Could not load memory: {e}")
                inputs['chat_history'] = []

            # time.sleep(4)
        self._validate_inputs(inputs)
        return inputs


# helper functions
def get_memory(user_id: int):
    '''
        Get memory object — SimpleMem-backed (local, zero-latency reads)
    '''
    from integrations.channels.memory.simplemem_langchain import SimpleMemChatMemory
    return SimpleMemChatMemory.load_or_create(user_id)


def get_action_user_details(user_id):
    '''
        This function help to extract action that user have perfomed till time
    '''
    # Initialize default values
    user_details = "No user details available."
    actions = "user has not performed any actions yet."

    unwanted_actions = ['Topic Cofirmation', 'Langchain', 'Assessment Ended', 'Casual Conversation', 'Topic confirmation',
                        'Topic not found', 'Topic Confirmation', 'Topic Listing', 'Probe', 'Question Answering', 'Fallback']
    action_url = f"{ACTION_API}?user_id={user_id}"

    # Todo: get, and populate timezone from client
    time_zone = "Asia/Kolkata"

    india_tz = pytz.timezone(time_zone)

    payload = {}
    headers = {}

    try:
        response = requests.request(
            "GET", action_url, headers=headers, data=payload, timeout=5.0)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        app.logger.error(f"Failed to get actions from {action_url}: {e}")
        post_dict = {'user_id': user_id, 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.GET_ACTION_USER_DETAILS.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Connection timeout/error at get action api: {e}'}
        publish_async('com.hertzai.longrunning.log', post_dict)
        # Continue with defaults instead of crashing
        response = None

    if response and response.status_code == 200:

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
            # Screen context goes stale faster — 2 minute window
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

        actions = actions + ". List of actions ends. <PREVIOUS_USER_ACTION_END> \n " + "Today's datetime in "+time_zone + "is: " + formatted_time + \
            " in this format:'%Y-%m-%dT%H:%M:%S' \n Whenever user is asking about current date or current time at particular location then use this datetime format by asking what user's location is. Use the previous sentence datetime info to answer current time based questions coupled with google_search for current time or full_history for historical conversation based answers. Take a deep breath and think step by step.\n"
        # user detail api
    else:
        post_dict = {'user_id': user_id, 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.GET_ACTION_USER_DETAILS.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': 'Exception happend at get action api end'}
        publish_async('com.hertzai.longrunning.log', post_dict)

    url = STUDENT_API
    payload = json.dumps({
        "user_id": user_id
    })
    headers = {
        'Content-Type': 'application/json'
    }

    try:
        response = requests.request("POST", url, headers=headers, data=payload, timeout=5.0)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        app.logger.error(f"Failed to get user details from {url}: {e}")
        post_dict = {'user_id': user_id, 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.GET_ACTION_USER_DETAILS.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Connection timeout/error at get user detail api: {e}'}
        publish_async('com.hertzai.longrunning.log', post_dict)
        return user_details, actions  # Return defaults

    if response.status_code == 200:
        user_data = response.json()

        user_details = f'''Below are the information about the user.
        user_name: {user_data["name"]} (Call the user by this name only when required and not always),gender: {user_data["gender"]}, who_pays_for_course: {user_data["who_pays_for_course"]}(Entity Responsible for Paying the Course Fees), preferred_language: {user_data["preferred_language"]}(User's Preferred Language), date_of_birth: {user_data["dob"]}, english_proficiency: {user_data["english_proficiency"]}(User's English Proficiency Level), created_date: {user_data["created_date"]}(user creation date), standard: {user_data["standard"]}(User's Standard in which user studying)
        '''
    else:
        post_dict = {'user_id': user_id, 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.GET_ACTION_USER_DETAILS.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': 'Exception happend at get user detail api end'}
        publish_async('com.hertzai.longrunning.log', post_dict)
    return user_details, actions


def get_time_based_history(prompt: str, session_id: str, start_date: str, end_date: str):
    '''
        Semantic search through conversation history using SimpleMem.
        inputs:
            prompt: text from user from which we need to extract similar messages
            session_id: user_{user_id}
            start_date: time of search start (kept for API compat, not used by SimpleMem)
            end_date: time till search (kept for API compat, not used by SimpleMem)
    '''
    start_time = time.time()

    try:
        user_id = int(session_id.replace("user_", ""))
        memory = get_memory(user_id=user_id)
        results = memory.semantic_search(prompt)

        if results:
            serialized = [{'message': {'content': r.get('content', ''), 'role': 'assistant'}} for r in results]
            final_res = {'res_in_filter': serialized}
        else:
            final_res = {'res_in_filter': []}

        elapsed = time.time() - start_time
        app.logger.info(f"SimpleMem search took {elapsed:.3f}s, {len(results)} results")
        return json.dumps(final_res)
    except Exception as e:
        app.logger.warning(f"SimpleMem search failed: {e}")
        return json.dumps({'res': []})


def parsing_string(string):
    '''
        this function will extract infromation for above function ie prompt start date end date
    '''
    try:
        app.logger.info(" The string to parse: {string}")
        prompt, start_date, end_date = [s.strip() for s in string.split(",")]
        session_id = 'user_'+str(thread_local_data.get_user_id())
        return get_time_based_history(prompt, session_id, start_date, end_date)
    except:
        now = datetime.utcnow()
        formatted_time = now.strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'
        session_id = "user_"+str(thread_local_data.get_user_id())
        return get_time_based_history(string, session_id, formatted_time, formatted_time)


def parse_date(date_str):
    return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")


def parse_character_animation(string):
    '''
        Dreambooth character animation api
        input string
        how this function works
        1 get user information based on user_id
        2 get fav teacher
        3 call dreambooth api with fav teacher name
    '''
    try:
        post_dict = {'user_id': '', 'task_type': 'async', 'status': TaskStatus.EXECUTING.value, 'task_name': TaskNames.ANIMATE_CHARACTER.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.ANIMATE_CHARACTER.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id()}
        publish_async('com.hertzai.longrunning.log', post_dict)
        prompt = string
        student_id_url = STUDENT_API

        payload = json.dumps({
            "user_id": thread_local_data.get_user_id()
        })
        headers = {
            'Content-Type': 'application/json'
        }

        response = requests.request(
            "POST", student_id_url, headers=headers, data=payload)
        if response.status_code == 200:
            favorite_teacher_id = response.json()["favorite_teacher_id"]

        get_image_by_id_url = f"{FAV_TEACHER_API}/{favorite_teacher_id}"

        payload = {}
        headers = {}

        response = requests.request(
            "GET", get_image_by_id_url, headers=headers, data=payload)

        image_name = response.json()["image_name"]

        image_url = response.json()["image_url"]
        image_response = pooled_get(image_url)
        image_content = image_response.content

        image_name = image_name.replace("vtoonify_", "", 1)
        folder_name = image_name.split(".")[0]
        inference_url = f"{DREAMBOOTH_API}/generate_images"
        payload = {'prompt': prompt}
        headers = {}
        logging.info("done till here")
        files = [
            # Use the correct content type
            ('image', ('image.jpeg', image_content, 'image/jpeg'))
        ]
        url = "http://20.197.30.74:8000/generate_image/"
        response = pooled_post(url, headers=headers,
                                 data=payload, files=files)
        if response.status_code == 200:
            return response.json()["url"]
        else:
            post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.ANIMATE_CHARACTER.value, 'uid': thread_local_data.get_request_id(
            ), 'task_id': f"{TaskNames.ANIMATE_CHARACTER.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at dreamooth api end for re {thread_local_data.get_request_id()}'}
            publish_async('com.hertzai.longrunning.log', post_dict)

    except Exception as e:
        # logging.info(f"exception {e}")
        time.sleep(30)
        post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.TIMEOUT.value, 'task_name': TaskNames.ANIMATE_CHARACTER.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.ANIMATE_CHARACTER.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at dreamooth api end for req_id {thread_local_data.get_request_id()} timed out'}
        publish_async('com.hertzai.longrunning.log', post_dict)
        return "something went wrong"


def parse_text_to_image(inp):
    '''
        stable diffusion
    '''
    try:

        post_dict = {'user_id': '', 'task_type': 'async', 'status': TaskStatus.EXECUTING.value, 'task_name': TaskNames.STABLE_DIFF.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.STABLE_DIFF.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id()}
        publish_async('com.hertzai.longrunning.log', post_dict)

        url = f'{STABLE_DIFF_API}?prompt={inp}'
        payload = {}

        headers = {}
        response = requests.request(
            "POST", url, headers=headers, data=payload, timeout=240)
        if response.status_code == 200:
            return response.json()["img_url"]
        else:
            post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.STABLE_DIFF.value, 'uid': thread_local_data.get_request_id(
            ), 'task_id': f"{TaskNames.STABLE_DIFF.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at stable diff for req_id: {thread_local_data.get_request_id()}'}
            publish_async('com.hertzai.longrunning.log', post_dict)
    except Exception as e:
        post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.TIMEOUT.value, 'task_name': TaskNames.ANIMATE_CHARACTER.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.ANIMATE_CHARACTER.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at stable diff for req_id: {thread_local_data.get_request_id()} timed out'}
        publish_async('com.hertzai.longrunning.log', post_dict)
        return f"{e} Not able to generating image at this moment please try later"


def parse_image_to_text(inp):
    '''
        LlaVA implemetation
    '''

    try:
        post_dict = {'user_id': '', 'task_type': 'async', 'status': TaskStatus.EXECUTING.value, 'task_name': TaskNames.LLAVA.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.LLAVA.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id()}
        publish_async('com.hertzai.longrunning.log', post_dict)
        inp_list = inp.split(',')
        url = f'{LLAVA_API}'
        payload = {
            'url': inp_list[0],
            'prompt': inp_list[1]
        }
        files = []
        headers = {}

        response = requests.request(
            "POST", url, headers=headers, data=payload, files=files, timeout=300)
        if response.status_code == 200:
            return response.text
        else:
            post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.LLAVA.value, 'uid': thread_local_data.get_request_id(
            ), 'task_id': f"{TaskNames.LLAVA.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at LLAVA for req_id: {thread_local_data.get_request_id()}'}
            publish_async('com.hertzai.longrunning.log', post_dict)
    except Exception as e:
        post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.TIMEOUT.value, 'task_name': TaskNames.LLAVA.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.LLAVA.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at LLAVA for req_id: {thread_local_data.get_request_id()} timed out'}
        publish_async('com.hertzai.longrunning.log', post_dict)
        return f'{e} Not able to generating answer at this moment please try later'


async def call_crwalab_api(input_url, input_str_list, user_id, request_id):
    try:
        app.logger.info("enter in call_crawlab_api function")
        app.logger.info(
            f"the input url is {input_url} and input_str_list is {input_str_list}")
        payload = {
            'link': input_str_list,
            'user_id': user_id,
            'request_id': request_id,
            'depth': '1',

        }
        app.logger.info("in crawlab api")
        app.logger.info(f"this is crawlab payload: - {payload}")

        headers = {}
        async with aiohttp.ClientSession() as session:
            async with session.post(CRAWLAB_API, headers=headers, data=payload) as response:
                response_text = await response.text()
                app.logger.info(f" this is response text : - {response_text}")
                return response_text
    except Exception as e:
        app.logger.info(
            f"we are in except of call_crawlab_api the error is {e}")
        url = RAG_API
        app.logger.info("going to except in Rag api")
        payload = {'url': input_url}
        files = []
        headers = {}

        response = requests.request(
            "POST", url, headers=headers, data=payload, files=files)
        return f'your url got uploaded and data extraction is being processes. Here is some brief information about url you hava provided {response.text}'


def start_async_tasks(coroutine):
    def run():
        from core.event_loop import get_or_create_event_loop
        loop = get_or_create_event_loop()
        loop.run_until_complete(coroutine)
    Thread(target=run).start()


def parse_link_for_crwalab(inp):
    '''

        Use this function when user give url for any webpage or pdf

    '''
    inp_list = inp.split(',')
    app.logger.info(inp_list)
    input_url = inp_list[0]
    app.logger.info(input_url)
    link_type = inp_list[1].strip(' ')
    app.logger.info(link_type)
    app.logger.info("\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n")
    user_id = thread_local_data.get_user_id()
    request_id = thread_local_data.get_request_id()

    try:
        post_dict = {'user_id': '', 'task_type': 'async', 'status': TaskStatus.EXECUTING.value, 'task_name': TaskNames.CRAWLAB.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.CRAWLAB.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id()}
        publish_async('com.hertzai.longrunning.log', post_dict)
        inp_list = inp.split(',')
        input_url = inp_list[0]
        link_type = inp_list[1].strip(' ')
        if link_type == 'pdf':
            try:
                cwd = os.getcwd()
                upload_folder_path = f'{cwd}/upload/'
                pdf_file_name = input_url.split("/")[-1]

                # Local path to save the PDF
                if not os.path.exists(upload_folder_path):
                    # If it does not exist, create it
                    os.makedirs(upload_folder_path)
                pdf_save_path = f'{upload_folder_path}/{pdf_file_name}'

                response = pooled_get(input_url)
                with open(pdf_save_path, 'wb') as file:
                    file.write(response.content)

                payload = {
                    'user_id': thread_local_data.get_user_id(),
                    'request_id': thread_local_data.get_request_id()
                }

                # Open the file and send it in the POST request
                with open(pdf_save_path, 'rb') as file:
                    files = [('file', (pdf_file_name, file, 'application/pdf'))]
                    response = pooled_post(
                        BOOKPARSING_API, data=payload, files=files)

                os.remove(pdf_save_path)

                return f"your request has been sent and pdf is getting uploading into our system {response.text}"
            except:
                app.logger.info("Got exception in book parsing api {e}")
                post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.CRAWLAB.value, 'uid': thread_local_data.get_request_id(
                ), 'task_id': f"{TaskNames.CRAWLAB.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at CRWALAB for req_id: {thread_local_data.get_request_id()} for pdf upload'}
                publish_async('com.hertzai.longrunning.log', post_dict)
                return "sorry I am not able to process your request at this moment"

        elif link_type == 'website':
            input_url_list = [input_url]
            app.logger.info(f"link type is {link_type}")
            input_str_list = repr(input_url_list)
            try:

                url = RAG_API
                payload = {'url': input_url}
                files = []
                headers = {}

                response = requests.request(
                    "POST", url, headers=headers, data=payload, files=files)

                app.logger.info(response.text)
                app.logger.info(f"RAG_API response {response.text}")
                app.logger.info("completed rag")
                try:
                    app.logger.info("going for crawlab api")
                    start_async_tasks(call_crwalab_api(
                        input_url, input_str_list, user_id, request_id))
                    app.logger.info("done for crawlab api")
                    return response.text
                except Exception as e:
                    app.logger.info(f"Got exception in crawlab api {e}")
                    post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.CRAWLAB.value, 'uid': thread_local_data.get_request_id(
                    ), 'task_id': f"{TaskNames.CRAWLAB.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at CRWALAB for req_id: {thread_local_data.get_request_id()} for weblink upload'}
                    publish_async('com.hertzai.longrunning.log', post_dict)
                    return f"sorry I am not able to process your request at this moment but here is some brief information about url you hava provided {response.text}"

                return f"your url got uploaded and data extraction is being processes. Here is some brief information about url you hava provided {response.text}"
            except Exception as e:
                app.logger.info(f"Got exception in crawlab api {e}")
                post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.CRAWLAB.value, 'uid': thread_local_data.get_request_id(
                ), 'task_id': f"{TaskNames.CRAWLAB.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at CRWALAB for req_id: {thread_local_data.get_request_id()} for weblink upload'}
                publish_async('com.hertzai.longrunning.log', post_dict)
                return "sorry I am not able to process your request at this moment"

        else:
            return "Sorry I am unable to process your request with this url type"
    except Exception as e:
        pass


redis_client = redis.StrictRedis(
    host='azure_all_vms.hertzai.com', port=6369, db=0)


def get_frame(user_id):
    """Get latest camera frame — FrameStore first, Redis fallback."""
    # Primary: FrameStore (in-process, zero latency)
    svc = get_vision_service()
    if svc:
        frame_bytes = svc.store.get_frame(str(user_id))
        if frame_bytes is not None:
            import cv2
            frame = cv2.imdecode(
                np.frombuffer(frame_bytes, np.uint8), cv2.IMREAD_COLOR,
            )
            if frame is not None:
                app.logger.info(
                    f"Frame for user_id {user_id} from FrameStore")
                return frame[:, :, ::-1]  # BGR → RGB

    # Fallback: Redis (legacy path)
    serialized_frame = redis_client.get(user_id)
    try:
        if serialized_frame is not None:
            from security.safe_deserialize import safe_load_frame
            frame_bgr = safe_load_frame(serialized_frame)
            app.logger.info(
                f"Frame for user_id {user_id} from Redis")
            frame = frame_bgr[:, :, ::-1]
            return frame
        else:
            app.logger.info(f"No frame found for user_id {user_id}.")
            return None
    except ModuleNotFoundError as e:
        app.logger.info("ModuleNotFoundError: %s", "Numpy errr", exc_info=True)
        app.logger.info("Numpy version: %s", np.__version__)
        app.logger.info("Numpy location: %s", np.__file__)
        raise e


def parse_visual_context(inp: str):
    user_id = thread_local_data.get_user_id()
    request_id = thread_local_data.get_request_id()
    app.logger.info('Using Vision to answer question')
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

        prompt_text = f'Instruction: Respond in second person point of view\ninput:-{inp}'

        # Tier 1: Try local MiniCPM sidecar (port 9891) — zero latency, no cloud
        local_minicpm_port = int(os.environ.get('HEVOLVE_MINICPM_PORT', 9891))
        try:
            with open(image_path, 'rb') as f:
                r = requests.post(
                    f'http://localhost:{local_minicpm_port}/describe',
                    data=f.read(),
                    params={'prompt': prompt_text},
                    headers={'Content-Type': 'application/octet-stream'},
                    timeout=10,
                )
                if r.status_code == 200:
                    result = r.json().get('result', r.text)
                    app.logger.info(f'Visual context from local MiniCPM: {result[:100]}')
                    return result
        except Exception as e:
            app.logger.debug(f"Local MiniCPM sidecar unavailable, falling back to cloud: {e}")

        # Tier 2: Cloud MiniCPM fallback
        url = "http://azurekong.hertzai.com:8000/minicpm/upload"
        payload = {'prompt': prompt_text}
        fh = open(image_path, 'rb')
        try:
            files = [
                ('file', ('call.jpg', fh, 'image/jpeg'))
            ]
            response = pooled_post(url, headers={}, data=payload, files=files)
            app.logger.info(response.text)
            return response.text
        except Exception as e:
            app.logger.error('Got error in visual QA (cloud fallback)')
        finally:
            fh.close()

    return "No visual context available — camera not active."


def parse_user_id(inp: str):
    url = 'https://azurekong.hertzai.com:8443/db/getstudent_by_user_id'

    headers = {
        'Content-Type': 'application/json'
    }

    try:
        prov_user_id = re.findall(r'\d', inp)[0]
    except Exception:
        prov_user_id = ""

    payload = json.dumps({
        "user_id": thread_local_data.get_user_id()
    })

    response = requests.request("POST", url, headers=headers, data=payload)
    if prov_user_id == "" or int(prov_user_id) != thread_local_data.get_user_id():

        return f"you might interested in finding your user detail here are details {response.text}"

    else:
        return response.text


async def fetch(session, url):
    try:
        async with session.get(url) as response:
            start_time = time.time()
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            end_time = time.time()
            elapsed_time = end_time - start_time
            app.logger.info(f"time taken to crawl {url} is {elapsed_time}")
            return soup.get_text()
    except Exception as e:
        app.logger.error(f"An error occurred while fetching {url}: {e}")
        return ""


async def async_main(urls):
    async with aiohttp.ClientSession() as session:
        tasks = [fetch(session, url) for url in urls]
        return await asyncio.gather(*tasks)


def top5_results(query):
    final_res = []
    top_2_search_res = search.results(query, 2)
    top_2_search_res_link = [res['link'] for res in top_2_search_res]
    try:
        text = asyncio.run(async_main(top_2_search_res_link))
        # Removing punctuation and extra characters
        app.logger.info(text)
        cleaned_text = re.sub(r'[^\w\s]', '', text[0] +
                              " "+text[1])  # Remove punctuation
        # Remove extra newlines and leading/trailing whitespaces
        cleaned_text = re.sub(r'\n+', '\n', cleaned_text).strip()
    except RuntimeError as e:
        app.logger.error(f"Runtime error occurred: {e}")

    final_res.append({'text': cleaned_text, 'source': top_2_search_res_link})
    app.logger.info(f"res:-->{final_res}")

    if len(final_res) == 0:
        return search.results(query, 4)

    return final_res


class CustomConvoOutputParser(AgentOutputParser):
    """Output parser for the conversational agent."""

    def get_format_instructions(self) -> str:
        return FORMAT_INSTRUCTIONS

    def parse(self, text: str) -> Union[AgentAction, AgentFinish]:
        try:
            response = parse_json_markdown(text)
            action, action_input = response["action"], response["action_input"]
            if action == "Final Answer" or action == "Final_Answer":
                return AgentFinish({"output": action_input}, text)
            else:
                return AgentAction(action, action_input, text)
        except Exception as e:
            # str = ""
            app.logger.info(text)
            app.logger.info(f"Caught Exception while parsing output {e}")
            pattern = r"final\s*[_]*answer"
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    # Extract the JSON part from the string
                    escape_chars = ['\n', '\t', '\r',
                                    '\"', "\'", '\\', "'''", '"""']
                    start_index = text.index('{')
                    try:
                        end_index = text.rindex('}') + 1
                    except:
                        text += '"}'
                        end_index = text.rindex('}') + 1
                    json_string = text[start_index:end_index]
                    try:
                        parsed_json = parse_json_markdown(json_string)
                    except Exception as e:
                        parsed_json = parse_json_markdown(json_string.replace('\n', '').replace('\t', '').replace('\r', '').replace(
                            '\"', '').replace("\'", '').replace('\\', '').replace("'''", '').replace('"""', '').replace('`', ''))
                    action_input = parsed_json["action_input"]
                    return AgentFinish({"output": action_input}, text)
                else:
                    app.logger.info(text)
                    start_index = text.index('{')
                    try:
                        end_index = text.rindex('}') + 1
                    except:
                        text += '"}'
                        end_index = text.rindex('}') + 1
                    try:
                        json_string = text[start_index:end_index]
                        response = parse_json_markdown(json_string)
                        action, action_input = response["action"], response["action_input"]
                    except Exception as innerException:
                        app.logger.info(
                            "Caught inner Exception: {innerException}")
                        return AgentFinish({"output": text}, text)
                    return AgentAction(action, action_input, text)
                    # raise OutputParserException(f"Could not parse LLM output: {text}") from e
            except Exception as e:
                app.logger.info(f"Encounter an except {e} for ai msg")
                # Check for the 'AI:' pattern in the response
                ai_pattern = r"AI: (.+)"
                ai_match = re.search(ai_pattern, text, re.IGNORECASE)
                if ai_match:
                    final_answer = ai_match.group(1).strip()
                    return AgentFinish({"output": final_answer}, text)
                # Fallback: treat entire response as final answer
                app.logger.info("No parsable format found, using raw text as final answer")
                return AgentFinish({"output": text.strip()}, text)

    @property
    def _type(self) -> str:
        return "conversational_chat"


# Store user-specific agents and their chat history
# Only initialize if autogen is available
if autogen is not None:
    user_agents_creator: Dict[str, Tuple[autogen.AssistantAgent,
                                         autogen.UserProxyAgent]] = {}

    # Store agents for each user
    user_agents: Dict[str, Tuple[autogen.ConversableAgent,
                                 autogen.ConversableAgent]] = {}
else:
    user_agents_creator: Dict[str, Tuple] = {}
    user_agents: Dict[str, Tuple] = {}


# Define autogen-dependent functions only if autogen is available
if autogen is not None:
    def create_agents_for_user(user_id: str) -> Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]:
        """Create new assistant and user proxy agents for a user with basic configuration."""
        # Use the dynamic module-level config_list (cloud or local, set by wizard)
        from threadlocal import thread_local_data as _tld
        _override = _tld.get_model_config_override() if hasattr(_tld, 'get_model_config_override') else None
        _clist = _override or config_list

        llm_config = {
            "config_list": _clist,
            "seed": 42
        }

        # Create the assistant agent with context awareness
        assistant = autogen.AssistantAgent(
            name=f"assistant_{user_id}",
            llm_config=llm_config,
            is_termination_msg=lambda x: True if "TERMINATE" in x.get(
                "content") else False,
            system_message="""You are a custom agent bot creator. Your task is to interact with the user to gather all the necessary details to create an agent. Once you have collected all the required information, you will generate a complete agent configuration.

            The information you need to collect includes:

            Name: The name of the agent.
            Purpose: A brief description of the agent's purpose.
            Flow: The operational flow of the agent.
            Actions: The actions the agent will perform.
            Goal: The ultimate goal of the agent.
            Guidelines for Responses:

            If you are still gathering information, your response should be formatted as: { "status": "pending", "question": "The question you want to ask" }
            Once all the requirements are complete and you are ready to provide the agent configuration, your response should be formatted as: { "status": "completed", "name": "","Recipe":"", "purpose": "", "tools": "", "flow": { "actions": [] }, "goal": ""}
            the recipe should be containing all the steps
            necessary and name for it. Suggest well-documented, generalized python function(s)
            only if necessary if the task is achievable without python then dont give python code just give the prompt
            to perform similar tasks for coding steps in future. Make sure coding steps and
            non-coding steps are never mixed in one function. In the docstr of the function(s),
            clarify what non-coding steps are needed to use the language skill of the assistant.
            """
        )

        # Create the user proxy agent
        user_proxy = autogen.UserProxyAgent(
            name=f"user_proxy_{user_id}",
            human_input_mode="NEVER",
            is_termination_msg=lambda x: True if "TERMINATE" in x.get(
                "content") else False,
            code_execution_config={"work_dir": "coding", "use_docker": False}
        )

        return assistant, user_proxy


    def get_agent_response(assistant: autogen.AssistantAgent, user_proxy: autogen.UserProxyAgent, message: str) -> str:
        """Get a single response from the agent for the given message."""
        try:
            # Get the current chat history
            current_chat = user_proxy.chat_messages.get(assistant.name, [])

            # Create context from previous messages (last 5 messages for efficiency)
            context = current_chat[-5:] if current_chat else []
            context_str = "\n".join(
                [f"{msg['role']}: {msg['content']}" for msg in context])

            # Append context to the message if there's history
            enhanced_message = message
            if context:
                enhanced_message = f"Previous conversation:\n{context_str}\n\nCurrent message: {message}"

            # Send message and get response
            response = user_proxy.send(
                enhanced_message,
                assistant,
                request_reply=True
            )

            key = list(user_proxy.chat_messages.keys())[0]

            return user_proxy.chat_messages[key][-1]['content']

        except Exception as e:
            return f"Error getting response: {str(e)}"


    def create_agents(user_id: str,recipe:str) -> Tuple[autogen.ConversableAgent, autogen.ConversableAgent]:
        """Create new assistant and user agents for a given user_id"""
        from threadlocal import thread_local_data as _tld
        _override = _tld.get_model_config_override() if hasattr(_tld, 'get_model_config_override') else None
        _clist = _override or config_list

        llm_config = {
            "temperature": 0.7,
            "config_list": _clist,
        }
        conversation = True
        if conversation:
            recipe = recipe+'\n Note: Wait for user confirmation to proceed after every action.'

        # Create assistant agent
        assistant = autogen.ConversableAgent(
            name=f"assistant_{user_id}",
            llm_config=llm_config,
            is_termination_msg=lambda x: True if "TERMINATE" in x.get(
                "content") else False,
            system_message=recipe
        )

        # Create user agent
        user = autogen.ConversableAgent(
            name=f"user_{user_id}",
            is_termination_msg=lambda x: True if "TERMINATE" in x.get(
                "content") else False,
            llm_config=None,  # User agent doesn't need LLM
            human_input_mode="NEVER",  # We'll manually send messages
            max_consecutive_auto_reply=1  # Limit to 1 auto reply
        )

        return user, assistant

else:
    # Provide stub functions when autogen is not available
    def create_agents_for_user(user_id: str):
        raise ImportError("autogen package is not installed")

    def get_agent_response(assistant, user_proxy, message: str) -> str:
        raise ImportError("autogen package is not installed")

    def create_agents(user_id: str, recipe: str):
        raise ImportError("autogen package is not installed")


# main function
def get_ans(casual_conv, req_tool, user_id, query, custom_prompt, preferred_lang):
    start_time = time.time()
    user_details, actions = get_action_user_details(user_id=user_id)
    app.logger.info(
        "time taken by get_action_user_details %s seconds", time.time() - start_time)
    app.logger.info(casual_conv)
    llm = CustomGPT(casual_conv=casual_conv)
    app.logger.info(f"query------> {query}")
    memory_start_time = time.time()
    memory = get_memory(user_id=user_id)
    app.logger.info("time taken by get_memory %s seconds",
                    time.time() - memory_start_time)

    tools_start_time = time.time()
    tools = get_tools(req_tool=req_tool, is_first=True)
    app.logger.info("time taken by get_tools %s seconds",
                    time.time() - tools_start_time)

    app.logger.info(f'tools {type(tools)}')
    language = SUPPORTED_LANG_DICT.get(preferred_lang[:2], 'English')
    colloquial = True

    prefix = f"""You are Hevolve, an expert educational AI teacher with knowledge in every field.
        Answer questions accurately and respond as quickly as possible in {language}.
        Keep responses under 200 words. Be colloquial and natural - don't always greet or use the user's name.

        User details: {user_details}
        Context: {custom_prompt}

        You can answer questions, provide revisions, conduct assessments, teach topics, create curriculum, and assist with research.
        Your responses are conveyed via video with an avatar and text-to-speech.

        IMPORTANT: Always respond with valid JSON in a markdown code block with "action" and "action_input" fields.

        Previous user actions: {actions}

        Conversation History:
        <HISTORY_START>
        """

    if not casual_conv:
        suffix = """
            <HISTORY_END>
            Only if this above conversation history is not sufficient to fulfill the user's request then use below FULL_HISTORY tool. Important: If results can be accomplished with above information skip tools section and move to format instructions.

            TOOLS

            ------

            Assistant can use tools to look up information that may be helpful in answering the user's
            question. The tools you can use are:

            <TOOLS_START>
            {{tools}}
            <TOOLS_END>
            <FORMAT_INSTRUCTION_START>
            {format_instructions}
            <FORMAT_INSTRUCTION_END>

            always create parsable output."""+f'''
            <RESPONSE_INSTRUCTIONS_START>
            The response should be Colloquial in nature,
            The response language should be: {language}'''+"""
            <RESPONSE_INSTRUCTIONS_END>

            Here is the User and AI conversation in reverse chronological order:

            USER'S INPUT:
            -------------
            <USER_INPUT_START>
            Latest USER'S INPUT For which you need to respond (consult recent history only when needed for more context): {{{{input}}}}
            <USER_INPUT_END>
            """
    else:
        suffix = """
            <HISTORY_END>
            Only if this above conversation history is not sufficient to fulfill the user's request then use below FULL_HISTORY tool. Important: If results can be accomplished with above information skip tools section and move to format instructions.

            TOOLS

            ------

            Assistant can use tools to look up information that may be helpful in answering the user's
            question. The tools you can use are:


            <FORMAT_INSTRUCTION_START>
            {format_instructions}
            <FORMAT_INSTRUCTION_END>

            always create parsable output."""+f'''
            <RESPONSE_INSTRUCTIONS_START>
            The response should be Colloquial in nature,
            The response language should be: {language}'''+"""
            <RESPONSE_INSTRUCTIONS_END>

            Here is the User and AI conversation in reverse chronological order:

            USER'S INPUT:
            -------------
            <USER_INPUT_START>
            Latest USER'S INPUT For which you need to respond (consult recent history only when needed for more context): {{{{input}}}}
            <USER_INPUT_END>
            """

    TEMPLATE_TOOL_RESPONSE = """TOOL RESPONSE:
        ---------------------
        {observation}

        USER'S INPUT
        --------------------

        Okay, so what is response for this tool. If using information obtained from the tools you must mention it explicitly without mentioning the tool names - I have forgotten all TOOL RESPONSES! Remember to respond with a markdown code snippet of a json blob with a single action, and NOTHING else."""

    prompt = ConversationalChatAgent.create_prompt(
        tools,
        system_message=prefix,
        human_message=suffix,
        input_variables=["input", "agent_scratchpad", "chat_history"]
    )
    # prompt.input_variables

    # chat Agent

    llm_chain = LLMChain(
        llm=llm,
        prompt=prompt,
        # memory=memory
    )

    custom_parser = CustomConvoOutputParser()

    agent = ConversationalChatAgent(
        llm_chain=llm_chain,
        verbose=True,
        output_parser=custom_parser,
        template_tool_response=TEMPLATE_TOOL_RESPONSE
    )

    prom_id = thread_local_data.get_prompt_id()
    metadata = {"where": {
        "jsonpath": '$[*] ? (@.prompt_id == {})'.format(prom_id)}}
    agent_chain = CustomAgentExecutor(
        agent=agent,
        tools=tools,
        verbose=True,
        memory=memory,
        metadata=metadata
    )
    agent_chain_start_time = time.time()
    ans = agent_chain.run({'input': query})
    app.logger.info("time taken by chain agent run %s seconds",
                    time.time() - agent_chain_start_time)
    end_time = time.time()
    elapse_time = end_time-start_time
    app.logger.info(
        f"total time taken by get_ans function %s seconds", elapse_time)
    return ans


Hevolve = "You are Hevolve, a highly intelligent educational AI developed by HertzAI."
PROBE_TEMPLATE = ("You are Hevolve, a highly intelligent educational AI developed by HertzAI. Weave the conversation "
                  "history along with the Last_5_Minutes_Visual_Context if present to create a clear, engaging, "
                  "coherent conversation flow that encourages the user to respond. Complete your response in 130 words"
                  "Your response should not be more than 130 words. Neither repeat the previous "
                  "responses nor be monotonous, be creative and talk about intriguing awe-inspiring facts, "
                  "or with some interesting age appropriate casual conversations which will make you the single point "
                  "of contact for everything in the world. Greet if & only if the context demands you to, "
                  "build a dialogue, use user\'s name only when necessary, Do not sound robotic. If the user is not "
                  "actively engaging or if visual context is present but user not visible or if user visible but not "
                  "looking at camera (based on visual and conversation history timestamps) call out their name loud "
                  "or try singing a song to bring back their attention using a SEEK_ATTENTION tool with input like a "
                  "song made of knowledge based on user's age, or calling their name loud e.g. tool input: "
                  "\'<seek_attend_loud>Hey <username>, are you there</seek_attend_loud>\' or  "
                  "\'<seek_attend_lyrics>Some awesome lyrics</seek_attend_lyrics>\' . Continue the Conversation from "
                  "where I or you left off."
                  )
INTERMEDIATE_CONTINUATION = "You are Hevolve, a highly intelligent educational AI developed by HertzAI. Continue your response from where you left off in the last conversation, considering the new input as a continuation of the last request. Ensure a smooth transition from the previous response and start this response as a continuation of the previous one.\n INSTRUCTIONS: Start your response with transitional words or phrases that can be used as a continuation of the previous response."

first_promts = []
review_agents = {"10077":True,10077:True}
conversation_agent = {"10077":False,10077:False}
_state_lock = threading.Lock()  # Protects review_agents, conversation_agent, first_promts

# --- TTL-based cleanup for review_agents / conversation_agent (M2 fix) ---
_AGENT_TTL = 3600  # 1 hour
_agent_timestamps = {}  # user_id -> last-access epoch


def _touch_agent_timestamp(user_id):
    """Record that an agent entry was accessed (call under _state_lock)."""
    _agent_timestamps[user_id] = time.time()


def _cleanup_stale_agents():
    """Remove agent state entries not accessed in the last _AGENT_TTL seconds.

    Must be called *outside* _state_lock or inside an already-acquired lock.
    Safe to call frequently -- it is O(n) over active users only.
    """
    now = time.time()
    for key in list(_agent_timestamps.keys()):
        if now - _agent_timestamps[key] > _AGENT_TTL:
            review_agents.pop(key, None)
            conversation_agent.pop(key, None)
            _agent_timestamps.pop(key, None)

# Per-user locks to prevent race conditions when concurrent requests
# for the same user modify review_agents / conversation_agent dicts.
_user_locks = {}
_user_locks_lock = threading.Lock()


def _get_user_lock(user_key):
    """Get or create a per-user lock to serialize state mutations."""
    with _user_locks_lock:
        if user_key not in _user_locks:
            _user_locks[user_key] = threading.Lock()
        return _user_locks[user_key]


def _autonomous_gather_info(user_id, description, prompt_id):
    """Run gather_info autonomously — LLM answers all questions itself.

    In autonomous mode, autogen's UserProxyAgent has max_consecutive_auto_reply=10
    and the assistant's system_message is enriched with instructions to self-complete.
    """
    from gather_agentdetails import gather_info
    response = gather_info(user_id, description, prompt_id, autonomous=True)

    # Loop until completed (autogen handles it internally when max_auto_reply > 0)
    max_iterations = 15
    iteration = 0
    while iteration < max_iterations:
        try:
            new_response = response.replace('true', 'True').replace('false', 'False')
            parsed = retrieve_json(new_response)
            if parsed.get('status', '').lower() == 'completed':
                # Save agent config
                parsed['prompt_id'] = prompt_id
                parsed['creator_user_id'] = user_id
                name = f'prompts/{prompt_id}.json'
                with open(name, 'w') as f:
                    json.dump(parsed, f)
                app.logger.info(f'Autonomous agent config saved to {name}')
                # Sync to cloud DB so prompt_id matches
                try:
                    pooled_post(
                        f'{DB_URL}/createpromptlist',
                        json={'listprompts': [{
                            'prompt_id': prompt_id,
                            'prompt': parsed.get('goal', ''),
                            'user_id': user_id,
                            'name': parsed.get('name', ''),
                            'is_active': True,
                            'image_url': parsed.get('image_url', ''),
                        }]},
                        timeout=5)
                except Exception as e:
                    app.logger.debug(f"Cloud sync failed (non-fatal): {e}")
                return 'Agent details gathered autonomously. Moving to review.'
        except (json.JSONDecodeError, AttributeError, Exception) as e:
            app.logger.debug(f'Autonomous gather iteration {iteration}: {e}')

        # Not complete yet — send auto-continue
        response = gather_info(user_id, 'proceed', prompt_id, autonomous=True)
        iteration += 1

    # Fallback: save whatever we have
    app.logger.warning(f'Autonomous gather_info did not complete in {max_iterations} iterations')
    return 'Autonomous gathering completed. Moving to review.'


@app.route('/chat', methods=['POST'])
def chat():

    start_time = time.time()

    # Periodically evict stale agent state to prevent unbounded memory growth (M2 fix)
    _cleanup_stale_agents()

    data = request.get_json()
    user_id = data.get('user_id', None)
    preferred_lang = data.get('preferred_lang', 'en')
    request_id = data.get('request_id', None)
    req_tool = data.get('tools', None)
    file_id = data.get('file_id', None)
    prompt_id = data.get('prompt_id', None)
    create_agent = data.get('create_agent', None)
    casual_conv = data.get('casual_conv', False)
    autonomous = data.get('autonomous', False)
    probe = data.get('probe', None)
    intermediate = data.get('intermediate', None)
    speculative = data.get('speculative', False)
    model_config = data.get('model_config', None)
    app.logger.info(f"casual_conv type {casual_conv}")

    # Security: sanitize prompt_id to prevent path traversal
    if prompt_id is not None:
        prompt_id = str(prompt_id)
        if not re.match(r'^[a-zA-Z0-9_-]+$', prompt_id):
            return jsonify({'error': 'Invalid prompt_id format'}), 400

    # Per-request model config override (speculative execution)
    if model_config:
        thread_local_data.set_model_config_override(model_config)
    else:
        thread_local_data.clear_model_config_override()

    prompt = data.get('prompt', None)

    # GUARDRAIL: full pre-dispatch gate on every /chat call
    if prompt:
        try:
            from security.hive_guardrails import GuardrailEnforcer
            allowed, reason, prompt = GuardrailEnforcer.before_dispatch(prompt)
            if not allowed:
                return jsonify({'error': f'Guardrail: {reason}'}), 403
        except ImportError:
            pass

    # Speculative dispatch: fast response + background expert
    if speculative and prompt and user_id and prompt_id:
        try:
            from integrations.agent_engine.speculative_dispatcher import get_speculative_dispatcher
            dispatcher = get_speculative_dispatcher()
            if dispatcher.should_speculate(str(user_id), str(prompt_id), prompt):
                result = dispatcher.dispatch_speculative(
                    prompt, str(user_id), str(prompt_id))
                return jsonify({
                    'response': result['response'],
                    'Agent_status': 'Speculative Mode',
                    'speculation_id': result.get('speculation_id'),
                    'expert_pending': result.get('expert_pending', False),
                    'fast_model': result.get('fast_model'),
                    'latency_ms': result.get('latency_ms'),
                })
        except ImportError:
            pass

    # return ""
    thread_local_data.set_request_id(request_id=request_id)

    # Security: Prompt injection detection
    if prompt:
        try:
            from security.prompt_guard import check_prompt_injection
            is_safe, reason = check_prompt_injection(prompt)
            if not is_safe:
                app.logger.warning(f"Prompt injection detected: {reason}")
                return jsonify({'error': f'Input rejected: {reason}'}), 400
        except Exception:
            pass  # Degrade gracefully

    if prompt_id:
        # Per-user lock prevents concurrent requests from corrupting agent state.
        # Replaces the global _state_lock for better concurrency.
        _user_lock = _get_user_lock(user_id)
        with _user_lock:
            if os.path.exists(f'prompts/{prompt_id}.json'):
                app.logger.info('GATHER JSON EXISTS')
                if os.path.exists(f'prompts/{prompt_id}_0_recipe.json'):
                    app.logger.info('0 Recipe JSON EXISTS')
                    file_path = f'prompts/{prompt_id}.json'
                    with open(file_path, 'r') as f:
                        data = json.load(f)
                        no_of_flow = len(data['flows'])-1
                        app.logger.info(f'GOT LEN OF FLOW AS {no_of_flow}')
                    if os.path.exists(f'prompts/{prompt_id}_{no_of_flow}_recipe.json'):
                        create_agent = set_flags_to_enter_review_mode(no_of_flow, user_id) #returns false
                    else:
                        app.logger.info(f'{no_of_flow} Recipe JSON doesnot EXISTS')
                        create_agent = True
                        review_agents[user_id] = True
                        conversation_agent[user_id] = False
                        _touch_agent_timestamp(user_id)
                else:
                    app.logger.info('0 Recipe JSON doesnot EXISTS')
                    create_agent = True
                    review_agents[user_id] = True
                    conversation_agent[user_id] = False
                    _touch_agent_timestamp(user_id)

            else:
                app.logger.info('GATHER JSON doesnot EXISTS')
                create_agent = True
                review_agents[user_id] = False
                conversation_agent[user_id] = True
                _touch_agent_timestamp(user_id)

    if create_agent:
        # Generate prompt_id server-side if not provided
        if not prompt_id:
            prompt_id = _next_prompt_id()
            app.logger.info(f'Generated server-side prompt_id={prompt_id} for new agent')
        # Per-user lock: snapshot agent state flags (lock NOT held during LLM calls)
        _user_lock = _get_user_lock(user_id)
        with _user_lock:
            _in_review = user_id in review_agents and review_agents[user_id]
            _in_convo = user_id in conversation_agent and conversation_agent[user_id]
        # Phase 1: Gather Requirements
        if not _in_review:
            with _user_lock:
                review_agents[user_id] = False
                _touch_agent_timestamp(user_id)
            prompt = data.get('prompt', None)
            if prompt_id not in first_promts:
                first_promts.append(prompt_id)
                try:
                    res = pooled_get(
                        f'{DB_URL}/getprompt/?prompt_id={prompt_id}').json()
                    prompt = prompt+f" name:{res[0]['name']} goal:{res[0]['prompt']}"
                except:
                    app.logger.error(f'GOT DB ERROR FOR PROMPTID:{prompt_id}')
            if not user_id or not prompt:
                return jsonify({'response': 'Need user_id and text to create agent', 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': []})
            if autonomous:
                # Autonomous dispatch (from daemon or API): LLM self-generates agent config
                auto_response = _autonomous_gather_info(user_id, prompt, prompt_id)
                with _user_lock:
                    review_agents[user_id] = True
                    conversation_agent[user_id] = False
                    _touch_agent_timestamp(user_id)
                _record_lifecycle('Review Mode', user_id, prompt_id, f'Autonomous creation via dispatch: {prompt[:100]}')
                return jsonify({'response': auto_response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [], 'Agent_status': 'Review Mode', 'autonomous_creation': True, 'prompt_id': prompt_id})
            from gather_agentdetails import gather_info
            response = gather_info(user_id,prompt,prompt_id)
            new_response = response.replace('true','True').replace("false", "False")
            app.logger.info('AFTER GATHER INFO')
            try:
                try:
                    new_res = retrieve_json(new_response)
                    app.logger.info(f"new_res: {new_res}")
                except Exception as e:
                    app.logger.error(f'Got some error while will try with re match error:{e}')
                    json_match = re.search(r'{[\s\S]*}', response)
                    app.logger.info(f'Json match result: {json_match}')
                    if json_match:
                        app.logger.info(f'Inside json_match')
                        json_part = json_match.group(0)
                        app.logger.info(f'Before loads json_part:{json_part}')
                        new_res = json.loads(json_part)
                        app.logger.info(f'After loads new_res:{new_res}')
                    else:
                        raise ValueError('No JSON in response')
                app.logger.info('AFTER EVAL')
                if new_res['status'] == 'pending':
                    app.logger.info('PENDING STATUS')
                    ans = new_res['question'] if 'question' in new_res else new_res['review_details']
                    _record_lifecycle('Creation Mode', user_id, prompt_id, 'Agent creation started via gather_info')
                    return jsonify({'response': ans, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Creation Mode', 'prompt_id': prompt_id})
                else:
                    app.logger.info('COMPLETED STATUS')
                    new_res['prompt_id'] = prompt_id
                    new_res['creator_user_id'] = user_id
                    with _user_lock:
                        conversation_agent[user_id] = False
                        _touch_agent_timestamp(user_id)
                    app.logger.info(
                        'Agent Created Successfully saving it and reusing it for further purpose')
                    name = f'prompts/{prompt_id}.json'
                    with open(name, "w") as json_file:
                        json.dump(new_res, json_file)
                    app.logger.info(f"Dictionary saved to {name}")
                    # Sync to cloud DB so prompt_id matches
                    try:
                        pooled_post(
                            f'{DB_URL}/createpromptlist',
                            json={'listprompts': [{
                                'prompt_id': prompt_id,
                                'prompt': new_res.get('goal', ''),
                                'user_id': user_id,
                                'name': new_res.get('name', ''),
                                'is_active': True,
                                'image_url': new_res.get('image_url', ''),
                            }]},
                            timeout=5)
                    except Exception as e:
                        app.logger.debug(f"Cloud sync failed (non-fatal): {e}")
                    with _user_lock:
                        review_agents[user_id] = True
                        _touch_agent_timestamp(user_id)
                    _record_lifecycle('Review Mode', user_id, prompt_id, 'Agent details gathered, entering review')
                    return jsonify({'response': 'Got Agent details successfully lets move on to review them one at a time', 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Review Mode', 'prompt_id': prompt_id})
            except Exception as e:
                app.logger.error('GOT some error while eval and returning the response')
                app.logger.error(e)
                _record_lifecycle('Creation Mode', user_id, prompt_id, f'Creation continuing after parse error: {e}')
                return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Creation Mode'})
        # Phase 2: Review Phase (re-snapshot flags under lock after Phase 1 may have mutated)
        with _user_lock:
            _in_review = user_id in review_agents and review_agents[user_id]
            _in_convo = user_id in conversation_agent and conversation_agent[user_id]
        if _in_review and not _in_convo:
            response = recipe(user_id,prompt,prompt_id,file_id,request_id)
            if response =='Agent Created Successfully':
                with _user_lock:
                    conversation_agent[user_id] = True
                _touch_agent_timestamp(user_id)
                # Bridge: auto-create social identity for this agent
                try:
                    _create_social_agent_from_prompt(user_id, prompt_id)
                except Exception as e:
                    app.logger.debug(f"Social agent bridge skipped: {e}")
                _record_lifecycle('completed', user_id, prompt_id, 'Agent creation completed successfully')
                return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'completed'})
            _record_lifecycle('Review Mode', user_id, prompt_id, 'Agent details being reviewed')
            return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Review Mode'})
        # Phase 3: Evaluation Phase
        if _in_review and _in_convo:
            return evaluate_agent_after_creation_in_review(file_id, prompt, prompt_id, request_id, user_id)

    if prompt_id and os.path.exists(f'prompts/{prompt_id}.json'):

        with open(f'prompts/{prompt_id}.json', "r") as file:
            created_json = json.load(file)


        response = chat_agent(user_id,prompt,prompt_id,file_id,request_id)

        # --- Step 17: Check if the reuse agent intelligently decided to create a new agent ---
        # Two detection mechanisms:
        # 1. Autogen create_new_agent tool (intelligent — LLM decides via tool call)
        # 2. Response text pattern matching (fallback for structured agent output)
        user_prompt = f'{user_id}_{prompt_id}'
        if not review_agents.get(user_id) and not create_agent:
            # Check autogen tool signal first (intelligent detection)
            from reuse_recipe import creation_signals
            if user_prompt in creation_signals:
                signal = creation_signals.pop(user_prompt)
                agent_desc = signal.get('description', '')
                is_auto = signal.get('autonomous', False)
                app.logger.info(f'Autogen create_new_agent tool fired: desc="{agent_desc}", autonomous={is_auto}')

                new_prompt_id = _next_prompt_id()
                if is_auto:
                    # Autonomous: run gather_info with LLM-generated answers
                    auto_response = _autonomous_gather_info(user_id, agent_desc, new_prompt_id)
                    review_agents[user_id] = True
                    _touch_agent_timestamp(user_id)
                    _record_lifecycle('Review Mode', user_id, new_prompt_id, f'Autonomous creation from reuse: {agent_desc[:100]}')
                    return jsonify({
                        'response': auto_response,
                        'intent': ['FINAL_ANSWER'],
                        'req_token_count': 0, 'res_token_count': 0,
                        'history_request_id': [],
                        'Agent_status': 'Review Mode',
                        'autonomous_creation': True,
                        'prompt_id': new_prompt_id,
                    })
                else:
                    _record_lifecycle('Reuse Mode', user_id, new_prompt_id, f'Creation suggested from reuse: {agent_desc[:100]}')
                    return jsonify({
                        'response': response,
                        'intent': ['FINAL_ANSWER'],
                        'req_token_count': 0, 'res_token_count': 0,
                        'history_request_id': [],
                        'Agent_status': 'Reuse Mode',
                        'creation_suggested': True,
                        'suggested_agent_description': agent_desc,
                        'prompt_id': new_prompt_id,
                    })

            # Fallback: pattern matching on agent response text
            if _response_signals_creation(response):
                app.logger.info('Reuse agent response text signals new agent creation needed')
                _record_lifecycle('Reuse Mode', user_id, prompt_id, 'Creation suggested via response pattern')
                return jsonify({
                    'response': response,
                    'intent': ['FINAL_ANSWER'],
                    'req_token_count': 0, 'res_token_count': 0,
                    'history_request_id': [],
                    'Agent_status': 'Reuse Mode',
                    'creation_suggested': True,
                })

        _record_lifecycle('Reuse Mode', user_id, prompt_id, 'Agent reused for conversation')
        return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Reuse Mode'})

    if prompt_id:
        try:
            res = pooled_get(
                f'{DB_URL}/getprompt/?prompt_id={prompt_id}').json()
            # use config for url
            custom_prompt = res[0]['prompt']
            if res[0]['prompt'] == 'Learn Language':
                app.logger.info(
                    'found Learn languague getting user preffered language')
                lang = pooled_post('{}/getstudent_by_user_id'.format(DB_URL),
                                     data=json.dumps({"user_id": user_id})).json()
                language = lang['preferred_language'][:2]
                app.logger.info(f'user preffered language is {language}')
                custom_prompt = custom_prompt + \
                    f' The Language selected is {language}'
                app.logger.info(f"custom prompt is: {custom_prompt}")

        except:
            app.logger.error(f'failed to get prompt from id:- {prompt_id}')
            custom_prompt = Hevolve
    elif probe:
        custom_prompt = PROBE_TEMPLATE
        prompt_id = 0
    elif intermediate:
        custom_prompt = INTERMEDIATE_CONTINUATION
        prompt_id = 0
    else:
        custom_prompt = Hevolve  # use Hevolve from config/template
        prompt_id = 0
    app.logger.info(f'{custom_prompt}-->{prompt_id}')

    post_dict = {'user_id': user_id, 'status': 'INITIALIZED', 'task_name': "CHAT",
                 'uid': request_id, 'task_id': f"CHAT_{str(request_id)}", 'request_id': request_id}
    publish_async('com.hertzai.longrunning.log', post_dict)

    thread_local_data.set_user_id(user_id=user_id)
    thread_local_data.set_req_token_count(value=0)
    thread_local_data.set_res_token_count(value=0)
    thread_local_data.set_recognize_intents()
    thread_local_data.set_global_intent(global_intent=req_tool)
    thread_local_data.set_prompt_id(prompt_id)

    prompt = data.get('prompt', None)
    if probe:
        prompt = ''

    app.logger.info(
        "the time taken before get ans in main api is %s seconds", time.time() - start_time)
    ans_start_time = time.time()
    ans = get_ans(casual_conv, req_tool, user_id=user_id,
                  query=prompt, custom_prompt=custom_prompt, preferred_lang=preferred_lang)
    app.logger.info("the time taken by get ans in main api is %s seconds",
                    time.time() - ans_start_time)

    # --- Step 16c: Check if LLM's Create_Agent tool fired during get_ans() ---
    if thread_local_data.get_creation_requested():
        agent_description = thread_local_data.get_creation_description()
        is_autonomous = thread_local_data.get_creation_autonomous()
        new_prompt_id = _next_prompt_id()
        thread_local_data.clear_creation_flags()
        app.logger.info(f'LLM Create_Agent tool fired: desc="{agent_description}", autonomous={is_autonomous}')

        if is_autonomous:
            # Autonomous: run gather_info with LLM-generated answers
            auto_response = _autonomous_gather_info(user_id, agent_description, new_prompt_id)
            review_agents[user_id] = True
            _touch_agent_timestamp(user_id)
            _record_lifecycle('Review Mode', user_id, new_prompt_id, f'Autonomous creation via LLM tool: {agent_description[:100]}')
            return jsonify({
                'response': auto_response,
                'intent': ['FINAL_ANSWER'],
                'req_token_count': 0, 'res_token_count': 0,
                'history_request_id': [],
                'Agent_status': 'Review Mode',
                'autonomous_creation': True,
                'prompt_id': new_prompt_id,
            })
        else:
            # Interactive: start gather_info, return first question
            from gather_agentdetails import gather_info
            response = gather_info(user_id, agent_description, new_prompt_id)
            new_response = response.replace('true', 'True').replace("false", "False")
            try:
                new_res = retrieve_json(new_response)
                if new_res.get('status') == 'pending':
                    resp_text = new_res.get('question', new_res.get('review_details', ans))
                else:
                    resp_text = ans
            except Exception:
                resp_text = ans
            _record_lifecycle('Creation Mode', user_id, new_prompt_id, f'Interactive creation via LLM tool: {agent_description[:100]}')
            return jsonify({
                'response': resp_text,
                'intent': ['FINAL_ANSWER'],
                'req_token_count': 0, 'res_token_count': 0,
                'history_request_id': [],
                'Agent_status': 'Creation Mode',
                'prompt_id': new_prompt_id,
            })

    if req_tool == 'Image_Inference_Tool':
        action_response = pooled_post(f'{DB_URL}/create_action',)
        payload = json.dumps({
            "conv_id": None,
            "user_id": user_id,
            "action": f"{ans}",
            "zeroshot_label": "Image Inference",
            "gpt3_label": "Visual Context"
        })
        headers = {
            'Content-Type': 'application/json'
        }
        action_response = pooled_post(f'{DB_URL}/create_action', headers=headers, data=payload)
    if ans != "":
        post_dict = {'user_id': user_id, 'status': 'FINISHED', 'task_name': "CHAT",
                     'uid': request_id, 'task_id': f"CHAT_{str(request_id)}", 'request_id': request_id}
        publish_async('com.hertzai.longrunning.log', post_dict)
    else:
        post_dict = {'user_id': user_id, 'status': 'ERROR', 'task_name': "CHAT", 'uid': request_id,
                     'task_id': f"CHAT_{str(request_id)}", 'request_id': request_id, 'failure_reason': 'Got null response from GPT'}
        publish_async('com.hertzai.longrunning.log', post_dict)

    end_time = time.time()
    elapsed_time = end_time - start_time
    app.logger.info(f"time taken for this full call is {elapsed_time}")

    return jsonify({'response': ans, 'intent': thread_local_data.get_recognize_intents(), 'req_token_count': thread_local_data.get_req_token_count(), 'res_token_count': thread_local_data.get_res_token_count(), 'history_request_id': thread_local_data.get_reqid_list()})


def evaluate_agent_after_creation_in_review(file_id, prompt, prompt_id, request_id, user_id):
    response = chat_agent(user_id, prompt, prompt_id, file_id, request_id)
    _record_lifecycle('Evaluation Mode', user_id, prompt_id, 'Agent being evaluated after creation')
    return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0,
                    'history_request_id': [], 'Agent_status': 'Evaluation Mode'})


def set_flags_to_enter_review_mode(no_of_flow, user_id):
    app.logger.info(f'{no_of_flow} Recipe Json exist Going to reuse')
    create_agent = False
    review_agents[user_id] = True
    conversation_agent[user_id] = False
    _touch_agent_timestamp(user_id)
    return create_agent


@app.route('/time_agent',methods=['POST'])
def time_agent():
    app.logger.info('GOT REQUEST IN TIME AGENT API')
    data = request.get_json()
    task_description = data.get('task_description',None)
    user_id = data.get('user_id',None)
    request_from = data.get('request_from',"Reuse")
    prompt_id = data.get('prompt_id',None)
    action_entry_point = data.get('prompt_id',0)
    if not task_description or not user_id or not prompt_id:
        return jsonify({'error':'user_id or task_description or prompt_id is missing'}), 404
    app.logger.info(f'GOT user_id:{user_id} & prompt_id:{prompt_id} & task_description:{task_description}')
    if request_from == 'Reuse':
        res = time_based_execution(str(task_description),int(user_id),int(prompt_id),action_entry_point)
    else:
        res = time_execution(str(task_description),int(user_id),int(prompt_id),action_entry_point)
    return jsonify({'response':f'{res}'}), 200


@app.route('/visual_agent',methods=['POST'])
def visual_agent():
    app.logger.info('GOT REQUEST IN Visual AGENT API')
    data = request.get_json()
    task_description = data.get('task_description',None)
    user_id = data.get('user_id',None)
    request_from = data.get('request_from',"Reuse")
    prompt_id = data.get('prompt_id',None)
    if not task_description or not user_id or not prompt_id:
        return jsonify({'error':'user_id or task_description or prompt_id is missing'}), 404
    app.logger.info(f'GOT user_id:{user_id} & prompt_id:{prompt_id} & task_description:{task_description}')
    if request_from == 'Reuse':
        res = visual_based_execution(str(task_description),int(user_id),int(prompt_id))
    else:
        res = visual_execution(str(task_description),int(user_id),int(prompt_id))
    return jsonify({'response':f'{res}'}), 200

@app.route('/response_ack',methods=['POST'])
def response_ack():
    app.logger.info('GOT REQUEST IN response_ack')
    data = request.get_json()
    user_id = data.get('user_id',None)
    request_id = data.get('request_id',None)
    thread_local_data.set_request_id(request_id=request_id)
    prompt_id = data.get('prompt_id',None)

@app.route('/add_history', methods=['POST'])
def history():
    data = request.get_json()
    human_msg = data['human_msg']
    ai_msg = data['ai_msg']
    try:
        memory = get_memory(user_id=int(data['user_id']))
    except:
        return "Invalid user ID"
    if memory:
        try:
            memory.chat_memory.add_message(HumanMessage(content=human_msg))
            memory.chat_memory.add_message(AIMessage(content=ai_msg))
            return jsonify({'response': "Messages are saved!!!"}), 200
        except Exception as e:
            app.logger.warning(f"History not saved: {e}")
            return jsonify({'response': "Messages not saved (memory service unavailable)"}), 503
    else:
        return jsonify({'response': "Memory object not found"}), 400


# ═══════════════════════════════════════════════════════════════
# Social Bridge: auto-create social user when agent is created via /chat
# ═══════════════════════════════════════════════════════════════

def _create_social_agent_from_prompt(user_id, prompt_id):
    """Read prompts/{prompt_id}.json and create a social User for this agent."""
    prompt_file = f'prompts/{prompt_id}.json'
    if not os.path.exists(prompt_file):
        return
    with open(prompt_file, 'r') as f:
        data = json.load(f)

    agent_display_name = data.get('name', f'Agent {prompt_id}')
    agent_name = data.get('agent_name', '')  # 3-word name from LLM
    goal = data.get('goal', '')

    from integrations.social.models import get_db
    from integrations.social.services import UserService
    db = get_db()
    try:
        # Use LLM-generated 3-word name if available, otherwise generate one
        if not agent_name:
            from integrations.social.agent_naming import generate_agent_name
            suggestions = generate_agent_name(db, count=1)
            agent_name = suggestions[0] if suggestions else f"agent-{prompt_id}"

        try:
            user = UserService.register_agent(
                db, agent_name, goal or agent_display_name,
                agent_id=str(prompt_id), owner_id=str(user_id),
                skip_name_validation=not bool(agent_name))
            user.display_name = agent_display_name
            db.flush()
        except ValueError:
            pass  # already exists

        db.commit()
        app.logger.info(f"Social agent created: {agent_name} for prompt {prompt_id}")
    except Exception as e:
        db.rollback()
        app.logger.debug(f"Social bridge error: {e}")
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# Local-first Prompt CRUD (syncs to cloud DB)
# ═══════════════════════════════════════════════════════════════

@app.route('/prompts', methods=['GET'])
def get_prompts():
    """List prompts for a user. Local-first, cloud DB fallback."""
    req_user_id = request.args.get('user_id', '')
    if not req_user_id:
        return jsonify({'error': 'user_id required'}), 400

    prompts = []

    # 1. Read from local prompts/*.json files
    prompts_dir = os.path.join(os.path.dirname(__file__), 'prompts')
    if os.path.isdir(prompts_dir):
        for fname in os.listdir(prompts_dir):
            if fname.endswith('.json') and '_' not in fname:
                try:
                    fpath = os.path.join(prompts_dir, fname)
                    with open(fpath, 'r') as f:
                        data = json.load(f)
                    pid = fname.replace('.json', '')
                    creator = str(data.get('creator_user_id', ''))
                    if creator == str(req_user_id) or not creator:
                        prompts.append({
                            'prompt_id': pid,
                            'name': data.get('name', ''),
                            'prompt': data.get('goal', ''),
                            'agent_name': data.get('agent_name', ''),
                            'is_active': data.get('status', '') == 'completed',
                            'user_id': creator or req_user_id,
                            'has_recipe': os.path.exists(
                                os.path.join(prompts_dir, f'{pid}_0_recipe.json')),
                            'flow_count': len(data.get('flows', [])),
                            'source': 'local',
                        })
                except Exception:
                    continue

    # 2. Fallback: try cloud DB if no local results
    if not prompts:
        try:
            res = pooled_get(
                f'{DB_URL}/getprompt_onlyuserid/?user_id={req_user_id}',
                timeout=5)
            if res.status_code == 200:
                cloud_data = res.json()
                for item in cloud_data:
                    item['source'] = 'cloud'
                    item['has_recipe'] = False
                prompts = cloud_data
        except Exception:
            pass

    return jsonify(prompts)


@app.route('/prompts/public', methods=['GET'])
def get_public_prompts():
    """Return all public prompts/agents. Local-first, cloud DB fallback.
    Equivalent to the legacy /getprompt_all/ cloud endpoint."""
    prompts = []

    # 1. Read ALL prompts from local files (no user filter)
    prompts_dir = os.path.join(os.path.dirname(__file__), 'prompts')
    if os.path.isdir(prompts_dir):
        for fname in os.listdir(prompts_dir):
            if fname.endswith('.json') and '_' not in fname:
                try:
                    fpath = os.path.join(prompts_dir, fname)
                    with open(fpath, 'r') as f:
                        data = json.load(f)
                    pid = fname.replace('.json', '')
                    prompts.append({
                        'prompt_id': pid,
                        'name': data.get('name', ''),
                        'prompt': data.get('goal', ''),
                        'agent_name': data.get('agent_name', ''),
                        'is_active': data.get('status', '') == 'completed',
                        'is_public': True,
                        'user_id': data.get('creator_user_id', ''),
                        'teacher_image_url': data.get('teacher_image_url', ''),
                        'image_url': data.get('image_url', ''),
                        'video_text': data.get('video_text', ''),
                        'has_recipe': os.path.exists(
                            os.path.join(prompts_dir, f'{pid}_0_recipe.json')),
                        'flow_count': len(data.get('flows', [])),
                        'source': 'local',
                    })
                except Exception:
                    continue

    # 2. Also fetch from cloud DB to get remote-only agents
    try:
        res = pooled_get(f'{DB_URL}/getprompt_all/', timeout=5)
        if res.status_code == 200:
            cloud_data = res.json()
            local_ids = {str(p['prompt_id']) for p in prompts}
            for item in cloud_data:
                if str(item.get('prompt_id', '')) not in local_ids:
                    item['source'] = 'cloud'
                    prompts.append(item)
    except Exception:
        pass

    return jsonify(prompts)


_prompt_id_lock = threading.Lock()


def _next_prompt_id():
    """Generate a globally-unique prompt_id.

    Uses millisecond timestamp so IDs are unique across nodes in the
    federation (no central coordinator needed).  A local collision check
    ensures two agents created on the same node within the same
    millisecond still get distinct IDs.

    Thread-safe via lock.
    """
    with _prompt_id_lock:
        prompts_dir = os.path.join(os.path.dirname(__file__), 'prompts')
        pid = int(time.time() * 1000)
        if os.path.isdir(prompts_dir):
            while os.path.exists(os.path.join(prompts_dir, f'{pid}.json')):
                pid += 1
        return pid


@app.route('/prompts', methods=['POST'])
def create_prompts():
    """Create/update prompts. Saves locally AND syncs to cloud DB."""
    data = request.get_json()
    listprompts = data.get('listprompts', [data] if 'name' in data else [])

    saved = []
    for item in listprompts:
        pid = item.get('prompt_id')
        if not pid:
            pid = _next_prompt_id()
            item['prompt_id'] = pid

        # Save locally
        local_path = f'prompts/{pid}.json'
        local_data = {}
        if os.path.exists(local_path):
            with open(local_path, 'r') as f:
                local_data = json.load(f)

        local_data['name'] = item.get('name', local_data.get('name', ''))
        local_data['goal'] = item.get('prompt', item.get('goal', local_data.get('goal', '')))
        local_data['prompt_id'] = pid
        local_data['creator_user_id'] = item.get('user_id', local_data.get('creator_user_id'))
        if 'agent_name' in item:
            local_data['agent_name'] = item['agent_name']
        if 'status' not in local_data:
            local_data['status'] = 'pending'

        with open(local_path, 'w') as f:
            json.dump(local_data, f, indent=2)

        saved.append({'prompt_id': pid, 'name': local_data['name']})

    # Sync to cloud DB (non-blocking, best effort)
    try:
        pooled_post(
            f'{DB_URL}/createpromptlist',
            json={'listprompts': listprompts},
            timeout=5)
    except Exception as e:
        app.logger.debug(f"Cloud sync failed (non-fatal): {e}")

    return jsonify({'success': True, 'saved': saved})


def _get_active_backend_info() -> dict:
    """Get which LLM backend is currently serving inference."""
    tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
    if tier in ('regional', 'central'):
        return {
            'type': 'external',
            'display_name': f"External ({os.environ.get('HEVOLVE_LLM_MODEL_NAME', 'unknown')})",
            'model': os.environ.get('HEVOLVE_LLM_MODEL_NAME', ''),
            'url': os.environ.get('HEVOLVE_LLM_ENDPOINT_URL', ''),
            'mode': tier,
        }
    cloud_url = os.environ.get('HEVOLVE_CLOUD_FALLBACK_URL', '')
    return {
        'type': 'local_llamacpp',
        'display_name': 'llama.cpp (Nunba)',
        'model': 'Qwen3-VL-2B',
        'mode': 'flat',
        'cloud_fallback_configured': bool(cloud_url),
    }


@app.route('/status', methods=['GET'])
def status():
    result = {'response': 'Working...', 'status': 'running'}

    # Active LLM backend info
    result['llm_backend'] = _get_active_backend_info()
    result['node_tier'] = os.environ.get('HEVOLVE_NODE_TIER', 'flat')

    # crawl4ai health (non-blocking, fail-safe)
    try:
        from integrations.agent_engine.world_model_bridge import get_world_model_bridge
        bridge = get_world_model_bridge()
        bridge_stats = bridge.get_stats()
        result['crawl4ai_url'] = bridge_stats.get('api_url', '')
        result['in_process'] = bridge_stats.get('in_process', False)
        health = bridge.check_health()
        result['crawl4ai_healthy'] = health.get('healthy', False)
        result['learning_active'] = health.get('learning_active', False)
        result['learning_mode'] = health.get('mode', 'unknown')
    except Exception:
        result['crawl4ai_healthy'] = False
        result['learning_active'] = False

    return jsonify(result)

@app.route('/zeroshot/', methods=['POST'])
def zeroshot():
    """
    Zero-shot classification endpoint using GPT-4.1-mini model.

    Request JSON format:
    {
        "input_text": "text to classify",
        "labels": ["label1", "label2", "label3"],
        "multi_label": false  // optional, default is false
    }

    Response format:
    {
        "sequence": "input text",
        "labels": ["label1", "label2", "label3"],
        "scores": [0.85, 0.10, 0.05]
    }
    """
    try:
        # Get request data
        data = request.get_json(force=True)
        input_text = data.get('input_text', '')
        labels = data.get('labels', [])
        multi_label = data.get('multi_label', False)

        # Validate inputs
        if not input_text:
            return jsonify({"error": "input_text is required"}), 400
        if not labels or len(labels) == 0:
            return jsonify({"error": "labels list is required and cannot be empty"}), 400

        # Create prompt for zero-shot classification
        if multi_label:
            prompt = f"""You are a text classification system. Given the following text and a list of labels, determine which labels apply to the text. Multiple labels can apply.

Text: "{input_text}"

Available labels: {', '.join(labels)}

For each label, provide a confidence score between 0 and 1 indicating how well it applies to the text. The scores don't need to sum to 1.

Respond ONLY with a JSON object in this exact format:
{{"scores": {{"label1": score1, "label2": score2, ...}}}}

Example response format:
{{"scores": {{"sports": 0.85, "entertainment": 0.60, "politics": 0.15}}}}"""
        else:
            prompt = f"""You are a text classification system. Given the following text and a list of labels, classify the text into ONE of the provided labels.

Text: "{input_text}"

Available labels: {', '.join(labels)}

Provide a confidence score between 0 and 1 for each label. The scores should sum to approximately 1.0, with the highest score indicating the most likely label.

Respond ONLY with a JSON object in this exact format:
{{"scores": {{"label1": score1, "label2": score2, ...}}}}

Example response format:
{{"scores": {{"sports": 0.75, "entertainment": 0.15, "politics": 0.10}}}}"""

        app.logger.info(f"Zero-shot classification request - Text: {input_text[:100]}..., Labels: {labels}")

        # Call Llama API
        response = pooled_post(
            GPT_API,
            json={
                "model": "llama",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 500
            }
        )

        app.logger.info(f"GPT API response status: {response.status_code}")

        if response.status_code != 200:
            return jsonify({"error": "GPT API request failed", "details": response.text}), 500

        # Parse GPT response
        gpt_result = response.json()
        app.logger.info(f"GPT response: {gpt_result}")

        # Extract the response content
        if isinstance(gpt_result, dict) and 'text' in gpt_result:
            response_text = gpt_result['text']
        elif isinstance(gpt_result, dict) and 'response' in gpt_result:
            response_text = gpt_result['response']
        elif isinstance(gpt_result, dict) and 'choices' in gpt_result:
            response_text = gpt_result['choices'][0]['message']['content']
        else:
            response_text = str(gpt_result)

        app.logger.info(f"Response text: {response_text}")

        # Parse JSON from response
        try:
            # Try to find JSON in the response
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                parsed_response = json.loads(json_match.group())
            else:
                parsed_response = json.loads(response_text)

            scores_dict = parsed_response.get('scores', {})

            # Convert to list format sorted by scores (descending)
            sorted_items = sorted(scores_dict.items(), key=lambda x: x[1], reverse=True)
            sorted_labels = [item[0] for item in sorted_items]
            sorted_scores = [item[1] for item in sorted_items]

            # Build response in format similar to transformers zero-shot-classification
            result = {
                "sequence": input_text,
                "labels": sorted_labels,
                "scores": sorted_scores
            }

            app.logger.info(f"Final result: {result}")
            return jsonify(result)

        except json.JSONDecodeError as e:
            app.logger.error(f"JSON parsing error: {e}, Response: {response_text}")
            # Fallback: return equal probabilities if parsing fails
            equal_score = 1.0 / len(labels)
            return jsonify({
                "sequence": input_text,
                "labels": labels,
                "scores": [equal_score] * len(labels),
                "warning": "Could not parse model response, returning equal probabilities"
            })

    except Exception as e:
        app.logger.error(f"Error in /zeroshot/ endpoint: {str(e)}")
        return jsonify({"error": str(e)}), 500

# ─── Runtime Media Tools API ──────────────────────────────────────────
# Endpoints for managing runtime media tools (Wan2GP, TTS-Audio-Suite,
# Whisper, OmniParser). Tools are downloaded, started, and registered
# dynamically. See integrations/service_tools/runtime_manager.py.

@app.route('/api/tools/status', methods=['GET'])
def tools_status():
    """Get status of all runtime media tools."""
    try:
        from integrations.service_tools.runtime_manager import runtime_tool_manager
        return jsonify(runtime_tool_manager.get_all_status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tools/<tool_name>/setup', methods=['POST'])
def tools_setup(tool_name):
    """Download + start + register a runtime tool."""
    try:
        from integrations.service_tools.runtime_manager import runtime_tool_manager
        result = runtime_tool_manager.setup_tool(tool_name)
        code = 500 if 'error' in result else 200
        return jsonify(result), code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tools/<tool_name>/start', methods=['POST'])
def tools_start(tool_name):
    """Start an already-downloaded runtime tool."""
    try:
        from integrations.service_tools.runtime_manager import runtime_tool_manager
        result = runtime_tool_manager.start_tool(tool_name)
        code = 500 if 'error' in result else 200
        return jsonify(result), code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tools/<tool_name>/stop', methods=['POST'])
def tools_stop(tool_name):
    """Stop a running runtime tool and free VRAM."""
    try:
        from integrations.service_tools.runtime_manager import runtime_tool_manager
        return jsonify(runtime_tool_manager.stop_tool(tool_name))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tools/<tool_name>/unload', methods=['POST'])
def tools_unload(tool_name):
    """Stop + deregister a runtime tool."""
    try:
        from integrations.service_tools.runtime_manager import runtime_tool_manager
        return jsonify(runtime_tool_manager.unload_tool(tool_name))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tools/vram', methods=['GET'])
def tools_vram():
    """Get VRAM usage dashboard."""
    try:
        from integrations.service_tools.vram_manager import vram_manager
        return jsonify(vram_manager.get_status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/voice/transcribe', methods=['POST'])
def voice_transcribe():
    """Transcribe audio to text using Whisper STT.

    Accepts multipart/form-data with 'audio' file or JSON with 'audio_path'.
    """
    try:
        from integrations.service_tools.whisper_tool import whisper_transcribe
        import tempfile
        import json as _json

        if request.content_type and 'multipart' in request.content_type:
            audio_file = request.files.get('audio')
            if not audio_file:
                return jsonify({'error': 'No audio file provided'}), 400
            # Save to temp file
            suffix = os.path.splitext(audio_file.filename)[1] or '.wav'
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                audio_file.save(tmp)
                tmp_path = tmp.name
            try:
                result = whisper_transcribe(tmp_path)
                return jsonify(_json.loads(result))
            finally:
                os.unlink(tmp_path)
        else:
            data = request.get_json() or {}
            audio_path = data.get('audio_path', '')
            if not audio_path:
                return jsonify({'error': 'audio_path is required'}), 400
            language = data.get('language')
            result = whisper_transcribe(audio_path, language)
            return jsonify(_json.loads(result))

    except ImportError as e:
        return jsonify({'error': f'Whisper not available: {e}'}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Hyve Skills API — ingest, list, and manage agent skills
# ---------------------------------------------------------------------------

@app.route('/api/skills/list', methods=['GET'])
def skills_list():
    """List all registered Hyve skills."""
    try:
        from integrations.skills import skill_registry
        return jsonify({
            'success': True,
            'skills': skill_registry.list_skills(),
            'count': skill_registry.count,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/skills/ingest', methods=['POST'])
def skills_ingest():
    """Ingest a skill from Markdown content (with optional YAML frontmatter)."""
    try:
        from integrations.skills import skill_registry
        data = request.get_json()
        name = data.get('name', '')
        content = data.get('content', '')
        description = data.get('description', '')
        tags = data.get('tags', [])

        if not content:
            return jsonify({'error': 'content is required'}), 400

        skill_registry.ingest_markdown(name, content, description, tags)
        skill_registry.save_config()
        return jsonify({'success': True, 'message': f'Skill "{name}" ingested'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/skills/discover/local', methods=['POST'])
def skills_discover_local():
    """Discover skills from local filesystem paths."""
    try:
        from integrations.skills import skill_registry
        data = request.get_json() or {}
        paths = data.get('paths')  # None = default paths
        count = skill_registry.discover_local(paths)
        skill_registry.save_config()
        return jsonify({'success': True, 'discovered': count})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/skills/discover/github', methods=['POST'])
def skills_discover_github():
    """Discover skills from a GitHub repository."""
    try:
        from integrations.skills import skill_registry
        data = request.get_json()
        repo_url = data.get('repo_url', '')
        branch = data.get('branch', 'main')
        skills_path = data.get('skills_path', '.claude/skills')

        if not repo_url:
            return jsonify({'error': 'repo_url is required'}), 400

        count = skill_registry.discover_github(repo_url, branch, skills_path)
        skill_registry.save_config()
        return jsonify({'success': True, 'discovered': count})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/skills/<skill_name>', methods=['GET'])
def skills_get(skill_name):
    """Get a specific skill's full details."""
    try:
        from integrations.skills import skill_registry
        skill = skill_registry.get_skill(skill_name)
        if not skill:
            return jsonify({'error': f'Skill "{skill_name}" not found'}), 404
        return jsonify({'success': True, 'skill': skill.to_dict()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/skills/<skill_name>', methods=['DELETE'])
def skills_delete(skill_name):
    """Remove a skill from the registry."""
    try:
        from integrations.skills import skill_registry
        if skill_registry.unregister_skill(skill_name):
            skill_registry.save_config()
            return jsonify({'success': True, 'message': f'Skill "{skill_name}" removed'})
        return jsonify({'error': f'Skill "{skill_name}" not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _init_skills():
    """Initialize skill registry — load persisted skills + discover local."""
    try:
        from integrations.skills import skill_registry
        skill_registry.load_config()
        skill_registry.discover_local()
        if skill_registry.count > 0:
            app.logger.info(f"Hyve skills ready: {skill_registry.count} skills loaded")
    except Exception as e:
        logger.debug(f"Skill init skipped: {e}")


def _init_runtime_tools():
    """Initialize runtime tools in a background thread.

    Restores previously-running tools from state file.
    Called from main() on startup.
    """
    try:
        from integrations.service_tools.runtime_manager import runtime_tool_manager
        runtime_tool_manager.load_state()
    except Exception as e:
        logger.warning(f"Runtime tool init failed: {e}")


def main():
    """
    Main entry point for hevolve-server CLI command.
    Starts the Flask server using waitress.
    """
    # Start runtime tools restoration in background
    import threading
    tools_thread = threading.Thread(target=_init_runtime_tools, daemon=True)
    tools_thread.start()

    # Initialize Hyve skill registry (load persisted + discover local)
    skills_thread = threading.Thread(target=_init_skills, daemon=True)
    skills_thread.start()

    serve(app, host='0.0.0.0', port=6777, threads=50)


if __name__ == '__main__':
    main()
    # app.debug = True
    # flask_thread = threading.Thread(target=lambda: serve(app, host='0.0.0.0', port=6777))
    # flask_thread.daemon = True
    # flask_thread.start()
    # from crossbar_server import component
    # # Run the WAMP client
    # run([component])

