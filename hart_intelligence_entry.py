# Fix Windows encoding for non-ASCII characters (Telugu, emojis, etc.)
import sys
import io
if sys.platform == 'win32' and 'pytest' not in sys.modules:
    # Force UTF-8 encoding for stdout/stderr to prevent crashes with non-ASCII characters
    # Skip when running under pytest — pytest wraps stdout/stderr for capture,
    # and replacing them here closes pytest's file handles.
    if sys.stdout is not None and hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if sys.stderr is not None and hasattr(sys.stderr, 'buffer'):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from bs4 import BeautifulSoup
from enum import Enum
from cultural_wisdom import get_cultural_prompt_compact
from agent_identity import build_identity_prompt, SECRETS_GUARDRAIL

# langchain_classic — pydantic v2-compatible fork of langchain 0.0.230
from langchain_classic.llms import OpenAI
from langchain_classic.chains import LLMChain
from langchain_classic.prompts import PromptTemplate
from langchain_classic.agents import (
    ZeroShotAgent, Tool, AgentExecutor, ConversationalAgent,
    ConversationalChatAgent, LLMSingleActionAgent, AgentOutputParser,
    load_tools, initialize_agent, AgentType
)
from langchain_classic.prompts import (
    ChatPromptTemplate, MessagesPlaceholder,
    SystemMessagePromptTemplate, HumanMessagePromptTemplate
)
from langchain_classic.chains import LLMMathChain
from langchain_classic.chains.conversation.memory import ConversationSummaryMemory, ConversationBufferWindowMemory
from langchain_classic.chat_models import ChatOpenAI
from langchain_classic.llms.base import LLM
from langchain_classic.memory import ConversationBufferMemory, ReadOnlySharedMemory
from langchain_classic.schema import AgentAction, AgentFinish, OutputParserException, HumanMessage, AIMessage, SystemMessage
from langchain_classic.tools import OpenAPISpec, APIOperation, StructuredTool
from langchain_classic.utilities import GoogleSearchAPIWrapper

import time

# ChatGroq - optional import (version compatibility issues)
try:
    from langchain_groq import ChatGroq
except Exception:
    ChatGroq = None

try:
    from langchain_classic.requests import Requests
except (ImportError, AttributeError):
    Requests = None
from flask import Flask, jsonify, request, g
from functools import wraps
import json
import os
import re
import secrets
import logging
import threading
import atexit
import requests
import pytz
import hashlib
from security.node_integrity import compute_code_hash, compute_file_manifest, verify_json_signature
from security import master_key


# --- Hevolve Boot Integrity Verification ---
_boot_logger = logging.getLogger("hevolve_integrity")

def hevolve_verify_boot():
    mode = (os.getenv("HEVOLVE_ENFORCEMENT_MODE") or "warn").lower()
    tier = (os.getenv("HEVOLVE_NODE_TIER") or "unknown").lower()

    manifest_path = master_key.RELEASE_MANIFEST_FILENAME

    if not os.path.exists(manifest_path):
        msg = f"[HevolveIntegrity] release_manifest.json missing (tier={tier}, mode={mode})"
        if mode == "hard":
            raise RuntimeError(msg)
        _boot_logger.warning(msg)
        return

    with open(manifest_path) as f:
        d = json.load(f)

    pub_hex = d.get("master_public_key")
    sig_hex = d.get("master_signature")
    payload_obj = {k: d[k] for k in d.keys() if k != "master_signature"}
    payload_json = json.dumps(payload_obj, sort_keys=True, separators=(",", ":"))
    try:
        verify_json_signature(pub_hex, payload_json, sig_hex)
    except Exception as e:
        msg = f"[HevolveIntegrity] Signature verification failed: {e}"
        if mode == "hard":
            raise RuntimeError(msg)
        _boot_logger.warning(msg)
        return

    current_code_hash = compute_code_hash()
    if d.get("code_hash") != current_code_hash:
        msg = "[HevolveIntegrity] CODE_HASH mismatch"
        if mode == "hard":
            raise RuntimeError(msg)
        _boot_logger.warning(msg)

    fm = compute_file_manifest()
    current_fm_hash = hashlib.sha256(json.dumps(fm, sort_keys=True).encode()).hexdigest()
    if d.get("file_manifest_hash") != current_fm_hash:
        msg = "[HevolveIntegrity] FILE_MANIFEST_HASH mismatch"
        if mode == "hard":
            raise RuntimeError(msg)
        _boot_logger.warning(msg)

    _boot_logger.info(f"[HevolveIntegrity] Boot verification OK (tier={tier}, mode={mode})")

# Defer boot verification to main() — do not run at import time
# --- End Boot Integrity Verification ---


from core.http_pool import pooled_get, pooled_post
from datetime import datetime, timezone
from typing import List, Union, Optional, Mapping, Any, Dict

# Conversational chat imports
try:
    from langchain_classic.agents.conversational_chat.output_parser import ConvoOutputParser
    from langchain_classic.agents.conversational_chat.prompt import FORMAT_INSTRUCTIONS
    from langchain_classic.output_parsers.json import parse_json_markdown
except (ImportError, AttributeError):
    ConvoOutputParser = None
    FORMAT_INSTRUCTIONS = None
    parse_json_markdown = None

# Tools imports
try:
    from langchain_classic.tools.requests.tool import RequestsGetTool
except (ImportError, AttributeError):
    RequestsGetTool = None

try:
    from langchain_classic.utilities.requests import TextRequestsWrapper
except (ImportError, AttributeError):
    TextRequestsWrapper = None

try:
    import tiktoken
except ImportError:
    tiktoken = None
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
    from helper import retrieve_json, PROMPTS_DIR, safe_prompt_path, _is_terminate_msg
except Exception:
    retrieve_json = None
    # Frozen builds install to Program Files (read-only) — redirect to user data dir
    if getattr(sys, 'frozen', False):
        try:
            from core.platform_paths import get_prompts_dir
            PROMPTS_DIR = os.path.abspath(get_prompts_dir())
        except ImportError:
            PROMPTS_DIR = os.path.abspath(os.path.join(
                os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'prompts'))
    else:
        PROMPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), 'prompts'))
    safe_prompt_path = None

# Ensure prompts directory exists (agent creation writes JSON here)
os.makedirs(PROMPTS_DIR, exist_ok=True)

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
                try:
                    from core.platform_paths import get_memory_graph_dir
                    db_path = get_memory_graph_dir(session_key)
                except ImportError:
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

    base_url: str = None  # resolved lazily via get_local_llm_url()
    model_name: str = "local"
    temperature: float = 0.7
    max_tokens: int = 1500

    @property
    def _llm_type(self) -> str:
        return "qwen3.5"

    def _call(self, prompt: str, stop: list = None) -> str:
        """
        Call the Qwen3.5 API with the given prompt.

        Args:
            prompt: The input text prompt
            stop: Optional stop sequences

        Returns:
            The generated response text
        """
        if not self.base_url:
            from core.port_registry import get_local_llm_url
            self.base_url = get_local_llm_url()

        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }

        if stop:
            payload["stop"] = stop

        _log = logging.getLogger(__name__)
        # Primary: HevolveAI embodied-ai on port 8000
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

        # Fallback: resolved LLM URL (handles port conflicts, warm starts)
        from core.port_registry import get_local_llm_url
        _llm_url = get_local_llm_url()
        if _llm_url not in self.base_url:
            try:
                _log.info(f"[LocalLLM] Falling back to llama.cpp at {_llm_url}")
                response = pooled_post(
                    f"{_llm_url}/chat/completions",
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
        if _active == 'custom_openai':
            from core.port_registry import get_local_llm_url
            _kwargs['openai_api_base'] = get_local_llm_url()
        return ChatOpenAI(**_kwargs)

    if USE_QWEN3VL:
        return ChatQwen3VL(
            model_name="Qwen3.5-4B",
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

# In bundled/pip-installed mode (NUNBA_BUNDLED env set by main.py), redirect logs
# to the shared Nunba log directory; standalone keeps default behavior.
_is_bundled = bool(os.environ.get('NUNBA_BUNDLED') or getattr(sys, 'frozen', False))

if _is_bundled:
    try:
        from core.platform_paths import get_log_dir as _get_log_dir_lc
        _nunba_log_dir = _get_log_dir_lc()
    except ImportError:
        _nunba_log_dir = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'logs')
    try:
        os.makedirs(_nunba_log_dir, exist_ok=True)
    except PermissionError:
        _nunba_log_dir = os.path.join(os.path.expanduser('~'), '.nunba', 'logs')
        os.makedirs(_nunba_log_dir, exist_ok=True)
    _langchain_log_path = os.path.join(_nunba_log_dir, 'langchain.log')
else:
    _langchain_log_path = 'langchain.log'

handler = RotatingFileHandler(_langchain_log_path, maxBytes=5_000_000, backupCount=2)
handler.setLevel(logging.INFO)

stream_handler = logging.StreamHandler(sys.stdout)

# Create a logging format
formatter = logging.Formatter(
    '%(asctime)s - %(name)s- [RequestID: %(req_id)s] - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

# Configure root logger: clear any default handlers first to prevent duplicates,
# then attach our handlers once.  In bundled mode the root logger gets both
# stream + file so that ALL module loggers are captured.  In standalone mode
# only Flask's app.logger gets the handlers.
_root = logging.getLogger()
_root.handlers.clear()
_root.setLevel(logging.INFO)

if _is_bundled:
    _root.addHandler(handler)
    _root.addHandler(stream_handler)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('HEVOLVE_MAX_PAYLOAD_BYTES', 2 * 1024 * 1024))  # 2MB default

if _is_bundled:
    # Bundled: root owns the handlers; let app.logger propagate to root
    # so ALL module loggers (hevolveai, langchain, etc.) are captured.
    app.logger.propagate = True
else:
    # Standalone: only Flask's logger gets the handlers
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

# Security: Block inspect.getsource() for protected packages (hevolveai)
try:
    from security.source_protection import install_source_guards
    install_source_guards()
except Exception:
    pass  # Non-fatal — source stripping is the primary defense

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

try:
    from integrations.social.consent_service import register_consent_routes
    register_consent_routes(app)
except ImportError:
    pass
except Exception as e:
    app.logger.warning(f"Consent service init skipped: {e}")

# MCP HTTP Bridge — exposes local MCP tools via REST for Nunba/external clients
try:
    from integrations.mcp.mcp_http_bridge import mcp_local_bp, auto_register_local_mcp
    app.register_blueprint(mcp_local_bp)
    auto_register_local_mcp()
    app.logger.info("MCP HTTP bridge registered at /api/mcp/local")
except ImportError:
    app.logger.info("MCP HTTP bridge not available, skipping")
except Exception as e:
    app.logger.warning(f"MCP HTTP bridge init skipped: {e}")

# Instruction Queue API — never miss a user instruction
try:
    from integrations.agent_engine.instruction_queue import (
        get_queue, enqueue_instruction, pull_user_batch,
    )

    @app.route('/api/instructions/enqueue', methods=['POST'])
    def _api_enqueue_instruction():
        data = request.get_json(silent=True) or {}
        user_id = str(data.get('user_id', '1'))
        text = data.get('text', '')
        if not text:
            return jsonify({'error': 'text is required'}), 400
        inst = enqueue_instruction(
            user_id, text,
            priority=data.get('priority', 5),
            tags=data.get('tags'),
            context=data.get('context'),
        )
        return jsonify({'success': True, 'instruction': inst.to_dict()})

    @app.route('/api/instructions/pending', methods=['GET'])
    def _api_pending_instructions():
        user_id = request.args.get('user_id', '1')
        q = get_queue(user_id)
        pending = q.get_pending()
        return jsonify({
            'success': True,
            'pending': [i.to_dict() for i in pending],
            'stats': q.stats(),
        })

    @app.route('/api/instructions/batch', methods=['POST'])
    def _api_pull_batch():
        data = request.get_json(silent=True) or {}
        user_id = str(data.get('user_id', '1'))
        max_tokens = data.get('max_tokens', 8000)
        batch = pull_user_batch(user_id, max_tokens=max_tokens)
        if not batch:
            return jsonify({'success': True, 'batch': None, 'message': 'No pending instructions'})
        return jsonify({'success': True, 'batch': batch.to_dict()})

    @app.route('/api/instructions/cancel', methods=['POST'])
    def _api_cancel_instruction():
        data = request.get_json(silent=True) or {}
        user_id = str(data.get('user_id', '1'))
        instruction_id = data.get('instruction_id', '')
        if not instruction_id:
            return jsonify({'error': 'instruction_id is required'}), 400
        q = get_queue(user_id)
        q.cancel(instruction_id)
        return jsonify({'success': True})

    @app.route('/api/instructions/plan', methods=['GET'])
    def _api_execution_plan():
        """Pull execution plan — agents query this to see what's available.

        Returns dependency-aware waves so the agent can dispatch
        independent items in parallel and dependent items in order.
        This is a PULL API — agents decide when to fetch work.
        """
        user_id = request.args.get('user_id', '1')
        max_tokens = int(request.args.get('max_tokens', '8000'))
        q = get_queue(user_id)
        plan = q.pull_execution_plan(max_tokens=max_tokens)
        if not plan:
            return jsonify({'success': True, 'plan': None, 'message': 'No pending instructions'})
        return jsonify({'success': True, 'plan': plan.to_dict()})

    @app.route('/api/instructions/complete', methods=['POST'])
    def _api_complete_instruction():
        """Mark a single instruction as done (used by agents after execution).

        Notifies SmartLedger to unblock dependent instructions.
        """
        data = request.get_json(silent=True) or {}
        user_id = str(data.get('user_id', '1'))
        instruction_id = data.get('instruction_id', '')
        result = data.get('result')
        if not instruction_id:
            return jsonify({'error': 'instruction_id is required'}), 400
        q = get_queue(user_id)
        q.complete_instruction(instruction_id, result=result)
        return jsonify({'success': True})

    @app.route('/api/instructions/fail', methods=['POST'])
    def _api_fail_instruction():
        """Mark a single instruction as failed — returns to queue for retry."""
        data = request.get_json(silent=True) or {}
        user_id = str(data.get('user_id', '1'))
        instruction_id = data.get('instruction_id', '')
        error = data.get('error', 'unknown')
        if not instruction_id:
            return jsonify({'error': 'instruction_id is required'}), 400
        q = get_queue(user_id)
        q.fail_instruction(instruction_id, error=error)
        return jsonify({'success': True})

    @app.route('/api/instructions/drain', methods=['POST'])
    def _api_drain_queue():
        """Trigger wave-based drain — dispatches with parallel + sequential ordering.

        Agent or daemon calls this to execute all pending instructions.
        Independent instructions dispatch in parallel threads.
        Dependent instructions wait for prerequisites.
        """
        data = request.get_json(silent=True) or {}
        user_id = str(data.get('user_id', '1'))
        max_tokens = data.get('max_tokens', 8000)
        try:
            from integrations.agent_engine.dispatch import drain_instruction_queue
            result = drain_instruction_queue(user_id, max_tokens=max_tokens)
            if result:
                return jsonify({'success': True, 'result': result[:2000]})
            return jsonify({'success': True, 'result': None, 'message': 'Queue empty or all failed'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    app.logger.info("Instruction queue API routes registered (8 endpoints)")
except Exception as e:
    app.logger.warning(f"Instruction queue init skipped: {e}")

# ── Credential Vault API — frontend submits missing credentials ──────
try:
    from desktop.ai_key_vault import AIKeyVault as _VaultCls, is_local_request

    @app.route('/api/credentials/submit', methods=['POST'])
    @_json_endpoint
    def _api_credentials_submit():
        """Accept a credential from the frontend — LOCALHOST ONLY.

        Secrets never leave the user's device. This endpoint rejects
        any request that doesn't originate from the local machine.
        """
        if not is_local_request(request.remote_addr):
            return jsonify({'error': 'Credential endpoints are localhost only. '
                            'Secrets never leave your device.'}), 403
        data = request.get_json(silent=True) or {}
        key_name = (data.get('key_name') or '').strip()
        value = (data.get('value') or '').strip()
        if not key_name or not value:
            return jsonify({'error': 'key_name and value are required'}), 400
        vault = _VaultCls.get_instance()
        resolved = vault.store_credential(
            key_name=key_name,
            value=value,
            channel_type=data.get('channel_type', ''),
        )
        return jsonify({'success': True, 'key_name': resolved})

    @app.route('/api/credentials/pending', methods=['GET'])
    @_json_endpoint
    def _api_credentials_pending():
        """List pending credential requests — LOCALHOST ONLY."""
        if not is_local_request(request.remote_addr):
            return jsonify({'error': 'Credential endpoints are localhost only.'}), 403
        vault = _VaultCls.get_instance()
        return jsonify({'pending': vault.get_pending_requests()})

    app.logger.info("Credential vault API routes registered (2 endpoints)")
except ImportError:
    pass
except Exception as e:
    app.logger.warning(f"Credential vault API init skipped: {e}")

# ── Kong API Gateway — Completions API proxy + metering ───────────────
try:
    @app.route('/v1/chat/completions', methods=['POST'])
    @_json_endpoint
    def _completions_proxy():
        """OpenAI-compatible completions — proxied through Kong metering.

        SDK clients hit Kong → Kong routes here → we forward to HevolveAI.
        Token usage is metered for 90/9/1 revenue split.
        """
        import requests as _req
        data = request.get_json(silent=True) or {}
        hevolve_url = os.environ.get('HEVOLVE_API_URL', 'http://localhost:8000')
        headers = {'Content-Type': 'application/json'}
        try:
            resp = _req.post(
                f'{hevolve_url}/v1/chat/completions',
                json=data,
                headers=headers,
                timeout=120
            )
            result = resp.json()
        except Exception as fwd_err:
            return jsonify({'error': f'HevolveAI backend unavailable: {fwd_err}'}), 502

        # Meter usage for revenue split
        usage = result.get('usage', {})
        total_tokens = usage.get('total_tokens', 0)
        if total_tokens > 0:
            try:
                from integrations.agent_engine.budget_gate import record_metered_usage
                consumer = request.headers.get('X-Consumer-Username', 'anonymous')
                record_metered_usage(
                    provider='hevolve',
                    model=data.get('model', 'hevolve'),
                    tokens=total_tokens,
                    source=f'sdk:{consumer}'
                )
            except Exception:
                pass  # metering failure must not block response

        return jsonify(result)

    @app.route('/api/gateway/metering', methods=['GET'])
    @_json_endpoint
    def _gateway_metering():
        """SDK usage metering stats for billing dashboard."""
        try:
            from integrations.social.models import db_session, MeteredAPIUsage
            from sqlalchemy import func
            with db_session() as session:
                rows = session.query(
                    MeteredAPIUsage.provider,
                    func.sum(MeteredAPIUsage.tokens_used),
                    func.count(MeteredAPIUsage.id)
                ).group_by(MeteredAPIUsage.provider).all()
                return jsonify({
                    'providers': [
                        {'provider': r[0], 'total_tokens': int(r[1] or 0), 'calls': r[2]}
                        for r in rows
                    ]
                })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/gateway/register', methods=['POST'])
    @_json_endpoint
    def _gateway_register_node():
        """Register a compute node as Kong upstream target.

        Called by compute_mesh when a node joins the hive.
        """
        data = request.get_json(silent=True) or {}
        target = data.get('target')  # e.g. "192.168.1.5:8000"
        if not target:
            return jsonify({'error': 'target is required'}), 400
        kong_admin = os.environ.get('KONG_ADMIN_URL', 'http://localhost:8001')
        upstream = data.get('upstream', 'hevolve-nodes')
        try:
            import requests as _req
            resp = _req.post(
                f'{kong_admin}/upstreams/{upstream}/targets',
                json={'target': target, 'weight': 100},
                timeout=10
            )
            return jsonify({'registered': True, 'status': resp.status_code})
        except Exception as e:
            return jsonify({'error': f'Kong admin unreachable: {e}'}), 502

    app.logger.info("Kong gateway API routes registered (3 endpoints: completions proxy, metering, node registration)")
except Exception as e:
    app.logger.warning(f"Kong gateway API init skipped: {e}")

# ============================================================================
# Google A2A Protocol Initialization
# ============================================================================
# Initialize A2A server for cross-platform agent communication
try:
    app.logger.info("Initializing Google A2A Protocol server...")
    from core.port_registry import get_port as _a2a_get_port
    a2a_server = initialize_a2a_server(app, base_url=f"http://localhost:{_a2a_get_port('backend')}")
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

# Mode-aware inference: pass LLM endpoint to HevolveAI for non-flat deployments
_node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
_active_cloud = os.environ.get('HEVOLVE_ACTIVE_CLOUD_PROVIDER', '')
if _node_tier in ('regional', 'central'):
    os.environ.setdefault('HEVOLVE_LLM_ENDPOINT_URL', config.get('OPENAI_API_BASE', ''))
    os.environ.setdefault('HEVOLVE_LLM_API_KEY', config.get('OPENAI_API_KEY', ''))
    os.environ.setdefault('HEVOLVE_LLM_MODEL_NAME', config.get('OPENAI_MODEL', 'gpt-4'))
elif _active_cloud and os.environ.get('HEVOLVE_LLM_API_KEY'):
    # Wizard-configured cloud provider (flat mode desktop user).
    # Vault already populated HEVOLVE_LLM_* env vars via export_to_env() in app.py.
    # HevolveAI's create_learning_llm_config() reads these automatically.
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
# If GPT_API is empty (no config.json in local/bundled mode), resolve from
# the local LLM URL that Nunba/setup-wizard configured. This ensures
# CustomGPT._call() can reach the llama-server for agent reasoning.
if not GPT_API:
    try:
        from core.port_registry import get_local_llm_url
        _resolved = get_local_llm_url()
        if _resolved:
            # get_local_llm_url returns base like http://127.0.0.1:8081/v1
            # CustomGPT._call() needs the chat completions endpoint
            GPT_API = _resolved.rstrip('/') + '/chat/completions'
    except Exception:
        pass
    if not GPT_API:
        _llm_url = os.environ.get('HEVOLVE_LOCAL_LLM_URL', '')
        if _llm_url:
            GPT_API = _llm_url.rstrip('/') + '/chat/completions'
FAV_TEACHER_API = config.get('FAV_TEACHER_API', '')
DREAMBOOTH_API = config.get('DREAMBOOTH_API', '')
STABLE_DIFF_API = config.get('STABLE_DIFF_API', '')
# CRAWLAB_API removed — web crawling is now in-process via integrations.web_crawler
RAG_API = config.get('RAG_API', '')

# Endpoint resolution — single source of truth in core/config_cache.py
# Automatically resolves to localhost:5000 in bundled mode, cloud URLs otherwise.
from core.config_cache import (
    get_db_url, get_action_api, get_student_api,
    get_vision_api, get_book_parsing_api, is_bundled as _config_is_bundled,
)
DB_URL = get_db_url()
ACTION_API = get_action_api()
STUDENT_API = get_student_api()
LLAVA_API = get_vision_api()
BOOKPARSING_API = get_book_parsing_api()
if _config_is_bundled():
    logging.getLogger(__name__).info(
        f"Bundled mode: DB/Action/Student/BookParsing/Vision APIs → {DB_URL}"
    )

# ============================================================================
# Embodied AI Learning Pipeline (HevolveAI — in-process, no extra port)
# ============================================================================
_learning_provider = None
_hive_mind = None
_trace_recorder = None


def _is_bundled() -> bool:
    """Detect whether we are pip-installed inside Nunba (flat mode).

    When Nunba imports us via ``hartos_backend_adapter``, that module is
    already in ``sys.modules`` by the time our daemon thread runs.  In
    standalone mode (``python hart_intelligence_entry.py``, ``start_with_tracing.bat``)
    it is absent.  No env-vars or mode flags required.
    """
    return 'hartos_backend_adapter' in sys.modules


def _has_cloud_api() -> bool:
    """Return True if the user configured an external cloud / API endpoint."""
    return bool(os.environ.get('HEVOLVE_LLM_ENDPOINT_URL', '').strip())


def _wait_for_llm_server(url=None, timeout=15):
    if url is None:
        from core.port_registry import get_local_llm_url
        url = get_local_llm_url().replace('/v1', '')
    """Wait for llama.cpp server, giving parent process time to start it.

    In Nunba (flat mode), the desktop app starts llama.cpp in a background
    thread.  This function polls the health endpoint so HevolveAI sees an
    existing server and reuses it instead of auto-starting a second one.

    In standalone mode nobody else starts the server, so after *timeout*
    seconds we return False and let HevolveAI auto-start as usual.

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
        "\u2014 HevolveAI will auto-start")
    return False


def _init_learning_pipeline():
    """Initialize HevolveAI's learning pipeline in-process.

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

    **Standalone (start_with_tracing.bat, ``python hart_intelligence_entry.py``):**
        Brief 5 s wait (in case user already has llama.cpp running).
        If nothing responds, ``create_learning_llm_config()`` calls
        HevolveAI which auto-starts its own server.  Default behaviour,
        no mode config needed.

    **Cloud API configured (``HEVOLVE_LLM_ENDPOINT_URL``):**
        Skip local server wait entirely — HevolveAI's Priority 0 path
        routes to the external endpoint.
    """
    global _learning_provider, _hive_mind, _trace_recorder

    try:
        from hevolveai.embodied_ai.rl_ef import (
            create_learning_llm_config,
            register_learning_provider,
        )
        from hevolveai.embodied_ai.monitoring.trace_recorder import get_trace_recorder
        from hevolveai.embodied_ai.learning.hive_mind import HiveMind, AgentCapability

        _logger = logging.getLogger(__name__)
        bundled = _is_bundled()
        cloud = _has_cloud_api()
        _logger.info(
            f"[EmbodiedAI] Initializing learning pipeline "
            f"(bundled={bundled}, cloud_api={cloud})...")

        # ── Decide how to handle the local llama.cpp server ──
        if cloud:
            # Cloud endpoint configured — HevolveAI uses it directly,
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
            # Standalone — brief courtesy wait then let HevolveAI auto-start.
            _wait_for_llm_server(timeout=5)

        # Trace recorder
        recordings_dir = os.path.join(
            os.path.expanduser('~'), '.hevolveai', 'recordings')
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
            f"[EmbodiedAI] HevolveAI not installed — learning disabled: {e}")
    except Exception as e:
        logging.getLogger(__name__).error(
            f"[EmbodiedAI] Learning pipeline init failed: {e}")


def get_learning_provider():
    """Get the in-process learning provider (for world_model_bridge)."""
    return _learning_provider


def get_hive_mind():
    """Get the in-process HiveMind instance (for world_model_bridge)."""
    return _hive_mind


# Boot learning pipeline in background — delayed to avoid torch circular import.
# Threads that import torch race with the main thread's own torch imports,
# causing "partially initialized module 'torch'" errors.
def _delayed_learning_init():
    import time
    time.sleep(5)  # Let main thread finish all imports first
    _init_learning_pipeline()

threading.Thread(
    target=_delayed_learning_init, daemon=True,
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
    """Connect FrameStore to HevolveAI's video learning pipeline.

    Waits up to 60s for both VisionService and LearningLLMProvider,
    then calls start_video_learning().
    """
    _logger = logging.getLogger(__name__)

    # Fast-fail: if hevolveai isn't installed, learning is impossible — don't wait 60s
    import importlib.util
    if not importlib.util.find_spec('hevolveai'):
        _logger.info("[Wiring] hevolveai not installed — skipping vision-learning wire")
        return

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
                "[Wiring] FrameStore → HevolveAI video learning connected")
        else:
            _logger.info(
                "[Wiring] LearningProvider has no start_video_learning — skip")
    except Exception as e:
        _logger.warning(f"[Wiring] FrameStore→learning failed: {e}")


# Boot vision pipeline in background — delayed like learning init
def _delayed_vision_init():
    import time
    time.sleep(6)  # After learning init delay (5s) to stagger torch imports
    _init_vision_service()

threading.Thread(
    target=_delayed_vision_init, daemon=True,
    name='vision_init').start()

# Wire FrameStore to HevolveAI after both subsystems are ready
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

# OpenAPI chain — deprecated (get_openapi_chain removed from langchain)
chain = None


client = crossbarhttp.Client('http://aws_rasa.hertzai.com:8088/publish') if crossbarhttp else None

# Create thread pool executor for async Crossbar publishing
crossbar_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='crossbar_publish')
atexit.register(lambda: crossbar_executor.shutdown(wait=False))


def _http_crossbar_publish(topic: str, payload: str, timeout: float = 2.0):
    """HTTP Crossbar publish — injected into MessageBus as transport fallback."""
    if client is None:
        return
    import socket
    try:
        original_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        client.publish(topic, payload)
    except Exception:
        pass
    finally:
        if original_timeout is not None:
            socket.setdefaulttimeout(original_timeout)


# Inject HTTP transport into MessageBus (avoids Layer 2 importing Layer 3)
def _inject_http_transport():
    try:
        from core.peer_link.message_bus import get_message_bus
        bus = get_message_bus()
        bus.set_http_transport(lambda t, p: crossbar_executor.submit(_http_crossbar_publish, t, p))
    except Exception:
        pass


_inject_http_transport()

# Topic resolution uses the canonical resolve_legacy_topic() from message_bus.py
# (single source of truth — no duplicate mapping table here)


def publish_async(topic, message, timeout=2.0):
    """
    Publish to all available transports: LOCAL EventBus + PeerLink + Crossbar.

    Routes through MessageBus first (local + multi-device delivery),
    then falls back to direct HTTP Crossbar for cloud telemetry.
    Works fully offline — LOCAL delivery always succeeds.

    Also publishes to the confirmation topic (mirrors cloud chatbot.py:publish()).

    Args:
        topic: Crossbar topic to publish to (legacy format)
        message: Message payload (JSON string or dict)
        timeout: Maximum time for HTTP Crossbar publish (default: 2.0 seconds)
    """
    # Parse message if JSON string
    data = message
    if isinstance(message, str):
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            data = {'raw': message}

    # 1. Route through MessageBus (LOCAL + PEERLINK — always works offline)
    try:
        from core.peer_link.message_bus import get_message_bus, resolve_legacy_topic
    except ImportError:
        resolve_legacy_topic = None
    bus_topic, user_id = resolve_legacy_topic(topic) if resolve_legacy_topic else (None, '')
    if bus_topic:
        try:
            bus = get_message_bus()
            # Ensure user_id is in data for per-user routing
            if user_id and isinstance(data, dict):
                data.setdefault('user_id', user_id)
            msg_id = bus.publish(
                bus_topic, data,
                user_id=user_id,
                skip_crossbar=True,  # We handle Crossbar below directly
            )

            # Publish confirmation tracking (mirrors cloud chatbot.py:publish())
            if bus_topic not in ('task.confirmation', 'task.progress'):
                conf_data = dict(data) if isinstance(data, dict) else {'raw': str(data)}
                conf_data['confirmation'] = False
                conf_data['topic_name'] = topic
                conf_data['msg_id'] = msg_id
                bus.publish('task.confirmation', conf_data, skip_crossbar=True)
        except Exception as e:
            app.logger.debug(f"MessageBus publish failed (offline OK): {e}")

    # 2. HTTP Crossbar for cloud telemetry + legacy mobile (when internet available)
    if client is None:
        return

    raw_message = message if isinstance(message, str) else json.dumps(message, default=str)

    def _publish():
        import socket
        try:
            original_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(timeout)
            client.publish(topic, raw_message)
            app.logger.debug(f"Published to Crossbar: {topic}")
        except Exception as e:
            app.logger.debug(f"Crossbar HTTP publish failed (offline OK): {e}")
        finally:
            if original_timeout is not None:
                socket.setdefaulttimeout(original_timeout)

    crossbar_executor.submit(_publish)


# create prompt
def create_prompt(tools):
    user_details, actions = get_action_user_details(
        user_id=thread_local_data.get_user_id())
    # Build dynamic identity based on active agent config
    _active_agent_config = thread_local_data.get_agent_config() if hasattr(thread_local_data, 'get_agent_config') else None
    _owner_name = ''
    if user_details:
        # Extract name from user details string (format varies)
        import re as _re
        _name_match = _re.search(r'(?:name|Name)[:\s]+([^\n,]+)', str(user_details))
        if _name_match:
            _owner_name = _name_match.group(1).strip()
    _dynamic_identity = build_identity_prompt(_active_agent_config, _owner_name, user_details)

    prefix = f"""Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.

        <GENERAL_INSTRUCTION_START>
        {_dynamic_identity}
        Consider the consequences of each response you provide.
        Your answers must be meaningful and delivered as quickly as possible.
        Never refer to the user as a human or yourself as mere AI.
        Your response should not be more than 200 words.
        {get_cultural_prompt_compact()}
        <GENERAL_INSTRUCTION_END>
        User details:
        <USER_DETAILS_START>
        {user_details}
        <USER_DETAILS_END>
        <CONTEXT_START>
        You can help with anything — answering questions, teaching, coding, research, creative writing, data analysis, building agents, brainstorming, planning, and more.
        Your expertise draws from various knowledge sources like books, websites, and white papers. Your responses will be conveyed to the user through a video, using an avatar and text-to-speech technology, and can be translated into various languages.
        Consider the user's location, time and context of previous dialogues with time to create a proper prompt for tools and follow up in-context questions. You have the ability to see using Visual_Context_Camera tool.
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


def _suggest_share_worthy_content(input_text: str) -> str:
    """Tool handler: find high-engagement, under-shared posts the user could share.

    Queries the social DB for posts with strong engagement (upvotes > 5,
    comments > 3) but few share links (< 3), then returns a human-friendly
    suggestion.
    """
    try:
        from integrations.social.models import get_db, Post, ShareableLink
        from sqlalchemy import func, outerjoin

        db = get_db()
        try:
            # Subquery: count share links per post
            share_counts = (
                db.query(
                    ShareableLink.resource_id,
                    func.count(ShareableLink.id).label('link_count'),
                )
                .filter(ShareableLink.resource_type == 'post')
                .group_by(ShareableLink.resource_id)
                .subquery()
            )

            # Main query: high engagement, low shares, not deleted/hidden
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
                       "been shared much yet. Consider sharing them with your network:\n")
            return header + "\n".join(suggestions)
        finally:
            db.close()
    except Exception as e:
        logging.debug(f"Suggest_Share_Worthy_Content failed: {e}")
        return f"Could not fetch share-worthy content right now: {e}"


def _observe_user_experience(input_text: str) -> str:
    """Record a user experience observation for self-improvement."""
    try:
        import json as _json
        data = _json.loads(input_text)
        event = data.get('event', 'unknown')
        page = data.get('page', '')
        duration_ms = data.get('duration_ms', 0)
        outcome = data.get('outcome', '')

        observation = f"User {event} on {page} ({duration_ms}ms): {outcome}"

        # Use MemoryGraph if available
        try:
            user_id = thread_local_data.get_user_id()
            prompt_id = thread_local_data.get_prompt_id()
            graph = _get_or_create_graph(user_id, prompt_id)
            if graph:
                session_id = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
                memory_id = graph.register(
                    content=observation,
                    metadata={
                        'memory_type': 'observation',
                        'source_agent': 'agent',
                        'session_id': session_id,
                        'page': page,
                        'event': event,
                    },
                    context_snapshot=f"UX observation during session {session_id}",
                )
                return f"Observation recorded (id: {memory_id}): {observation}"
        except Exception:
            pass

        return f"Observation noted: {observation}"
    except Exception:
        return f"Observation noted: {input_text}"


def _self_critique_and_enhance(input_text: str) -> str:
    """Review past suggestions and outcomes to improve future behavior."""
    try:
        user_id = thread_local_data.get_user_id()
        prompt_id = thread_local_data.get_prompt_id()
        graph = _get_or_create_graph(user_id, prompt_id)
        if not graph:
            return "Self-critique unavailable: no memory graph for this session."

        session_id = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)

        # Recall past suggestions and observations
        suggestions = graph.recall(input_text or 'suggestions made outcomes', mode='semantic', top_k=10)
        observations = graph.recall('user experience observation', mode='semantic', top_k=10)

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
        graph.register(
            content=insight,
            metadata={
                'memory_type': 'insight',
                'source_agent': 'agent',
                'session_id': session_id,
                'type': 'self_critique',
            },
            context_snapshot=f"Self-critique during session {session_id}",
        )

        return critique
    except Exception as e:
        return f"Self-critique unavailable: {str(e)}"


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


# ─── Perception Watchers (Future TTL) ───────────────────────────────────
_active_watchers = {}  # user_id -> [{trigger_id, expires_at, condition, action, modality, callback}]


def _handle_visual_watcher_tool(input_text):
    """Register a visual/audio watcher trigger with TTL.

    Input format: 'CONDITION: <condition> | ACTION: <action> | TTL: <minutes>'
    Optional: '| MODALITY: visual|audio|both' (defaults to visual)
    """
    import re
    import time as _time

    parts = {}
    for segment in input_text.split('|'):
        segment = segment.strip()
        if ':' in segment:
            key, val = segment.split(':', 1)
            parts[key.strip().upper()] = val.strip()

    condition = parts.get('CONDITION', input_text)
    action_text = parts.get('ACTION', 'notify user')
    ttl_minutes = int(parts.get('TTL', '30'))
    modality = parts.get('MODALITY', 'visual').lower()

    user_id = thread_local_data.get_user_id() or 'default'
    trigger_id = f'watcher_{user_id}_{int(_time.time())}'
    expires_at = _time.time() + (ttl_minutes * 60)

    def _on_trigger(description, **kwargs):
        """Callback fired when visual trigger matches."""
        try:
            from core.platform.events import emit_event
            emit_event('tts.speak', {'user_id': user_id, 'text': action_text})
            emit_event('perception.watcher.fired', {
                'user_id': user_id, 'condition': condition,
                'action': action_text, 'trigger_id': trigger_id,
            })
        except Exception:
            pass

    # Register with VisionService for visual watchers
    if modality in ('visual', 'both'):
        try:
            from integrations.vision.vision_service import VisionService
            from core.platform.service_registry import ServiceRegistry
            vs = ServiceRegistry.get('VisionService')
            if vs:
                condition_words = [w for w in condition.lower().split() if len(w) > 3]
                vs.register_visual_trigger(
                    channel='camera', callback=_on_trigger,
                    keywords=condition_words, cooldown_seconds=5,
                    name=trigger_id,
                )
        except Exception:
            pass

    watcher_entry = {
        'trigger_id': trigger_id, 'expires_at': expires_at,
        'condition': condition, 'action': action_text,
        'modality': modality, 'callback': _on_trigger,
    }

    if user_id not in _active_watchers:
        _active_watchers[user_id] = []
    _active_watchers[user_id].append(watcher_entry)

    return (
        f"Watcher registered: watching for '{condition}' → will '{action_text}'. "
        f"TTL: {ttl_minutes} minutes. Modality: {modality}."
    )


def _evaluate_audio_watchers(user_id, transcript):
    """LLM-powered evaluation of audio watchers against transcript."""
    import time as _time
    watchers = [w for w in _active_watchers.get(user_id, [])
                if w['modality'] in ('audio', 'both') and _time.time() < w['expires_at']]
    if not watchers:
        return

    conditions = "\n".join(f"- Watcher {i+1}: {w['condition']}" for i, w in enumerate(watchers))

    try:
        llm = get_llm(temperature=0.1, max_tokens=200)
        result = llm.invoke(
            f"The user just said: \"{transcript}\"\n\n"
            f"Active watchers:\n{conditions}\n\n"
            f"Which watcher conditions (if any) are semantically triggered by what the user said? "
            f"Return ONLY a JSON array of triggered watcher numbers, e.g. [1, 3]. "
            f"Return [] if none match."
        )
        import re as _re
        text = (result.content if hasattr(result, 'content') else str(result)).strip()
        match = _re.search(r'\[.*?\]', text)
        if match:
            triggered = json.loads(match.group())
            for idx in triggered:
                if 1 <= idx <= len(watchers):
                    w = watchers[idx - 1]
                    if w.get('callback'):
                        w['callback'](transcript)
    except Exception as e:
        app.logger.debug(f"Audio watcher eval failed: {e}")


def _push_workflow_flowchart(user_id, prompt_id, request_id=None):
    """Push recipe JSON to frontend via Crossbar for interactive flowchart rendering."""
    try:
        recipe_path = os.path.join(PROMPTS_DIR, f'{prompt_id}.json')
        if not os.path.isfile(recipe_path):
            return
        with open(recipe_path) as f:
            recipe_data = json.load(f)
        crossbar_message = {
            "text": ["Agent workflow ready"],
            "priority": 50,
            "action": "WorkflowFlowchart",
            "recipe": recipe_data,
            "request_id": request_id or "0",
            "prompt_id": prompt_id,
            "historical_request_id": [],
            "options": [], "newoptions": [],
        }
        publish_async(f'com.hertzai.hevolve.chat.{user_id}', json.dumps(crossbar_message))
    except Exception:
        pass


def _handle_agentic_router_tool(input_text):
    """Tool handler: LLM detected a multi-step agentic task.

    Called by the LangChain agent when a prompt needs multi-step execution.
    Builds a plan using LLM-powered agent matching + plan generation,
    sets thread-local flags that the /chat handler checks after get_ans() returns.
    """
    try:
        import concurrent.futures
        from integrations.agentic_router import build_agentic_plan
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(build_agentic_plan, input_text, PROMPTS_DIR)
            try:
                plan = future.result(timeout=15)
            except concurrent.futures.TimeoutError:
                return f"I'll help you with: {input_text}. Let me work on this directly."

        thread_local_data.set_agentic_routing(
            task_description=plan['task_description'],
            plan_steps=plan['steps'],
            matched_agent_id=plan.get('matched_agent_id'),
        )

        # Build a human-readable summary for the LLM's response
        steps_text = "\n".join(
            f"  {s['step_num']}. {s['description']}"
            for s in plan['steps']
        )
        agent_note = ""
        if plan.get('matched_agent_name'):
            agent_note = f"\nMatched agent: {plan['matched_agent_name']} ({plan['matched_agent_source']})"
        elif plan.get('requires_new_agent'):
            agent_note = "\nNo existing agent matches — a new agent will be created if you approve."

        return (
            f"I've analyzed your request and prepared a plan:\n\n"
            f"Task: {input_text}\n\n"
            f"Steps:\n{steps_text}\n"
            f"{agent_note}\n\n"
            f"Would you like me to proceed with this plan?"
        )
    except Exception as e:
        app.logger.error(f"Agentic router failed: {e}")
        return f"I'll help you with: {input_text}. Let me work on this directly."


def _handle_request_resource(input_text: str) -> str:
    """Generic runtime resource request — agent calls this when ANY tool needs
    a missing credential, API key, config value, or permission.

    The agent provides a JSON string like:
      {"resource_type": "api_key", "key_name": "GOOGLE_API_KEY",
       "label": "Google API Key", "used_by": "Google Search tool",
       "description": "Required for web search"}

    Handler checks the vault first. If the key exists, returns the value
    (making it available to the agent without the user re-entering it).
    If not, returns a structured resource_request that the backend injects
    into the response JSON so the frontend presents a secure input screen.
    """
    import json as _json
    try:
        req = _json.loads(input_text)
    except (ValueError, TypeError):
        # Plain-text fallback: agent just described what it needs
        req = {
            'resource_type': 'api_key',
            'key_name': 'UNKNOWN',
            'label': input_text[:100],
            'description': input_text,
            'used_by': 'Agent tool',
        }

    key_name = req.get('key_name', 'UNKNOWN')
    resource_type = req.get('resource_type', 'api_key')

    # Check vault first (tool keys + env vars)
    import os
    env_val = os.environ.get(key_name)
    if env_val:
        return f"Resource '{key_name}' is already configured and available."

    try:
        from desktop.ai_key_vault import AIKeyVault
        vault = AIKeyVault.get_instance()
        if resource_type == 'channel_secret':
            val = vault.get_channel_secret(
                req.get('channel_type', ''), key_name)
        else:
            val = vault.get_tool_key(key_name)
        if val:
            os.environ[key_name] = val  # Make available for current session
            return f"Resource '{key_name}' loaded from vault and is now available."
    except Exception:
        pass

    # Key not found — return a structured request for the frontend
    # The backend will detect __SECRET_REQUEST__ and inject it into the response
    # Track as pending so /api/credentials/pending can list it
    try:
        from desktop.ai_key_vault import AIKeyVault
        AIKeyVault.get_instance().add_pending_request(
            key_name=key_name,
            resource_type=resource_type,
            channel_type=req.get('channel_type', ''),
            label=req.get('label', key_name),
            description=req.get('description', ''),
            used_by=req.get('used_by', 'Agent tool'),
        )
    except Exception:
        pass

    secret_request = _json.dumps({
        '__SECRET_REQUEST__': True,
        'type': resource_type,
        'key_name': key_name,
        'label': req.get('label', key_name),
        'description': req.get('description', f'{key_name} is required.'),
        'used_by': req.get('used_by', 'Agent tool'),
        'channel_type': req.get('channel_type', ''),
    })
    return (
        f"I need the user to provide '{req.get('label', key_name)}'. "
        f"This is required for {req.get('used_by', 'a tool')}. "
        f"{req.get('description', '')} "
        f"RESOURCE_REQUEST:{secret_request}"
    )


def _safe_load_google_search():
    """Load google-search tool, returning empty list if package is missing."""
    try:
        return load_tools(["google-search"])
    except (ImportError, Exception) as e:
        logging.warning(f"Google Search tool unavailable: {e}")
        return []


def _with_tool_logging(func, tool_name):
    """Wrap a tool function with logging and generic error handling.

    Acts as a @before/@after decorator for all LangChain tools:
    - Logs entry with tool name and truncated input
    - Catches all exceptions and returns a user-friendly error string
      instead of crashing the agent executor
    - Logs completion with output length or error details
    """
    from functools import wraps
    @wraps(func)
    def wrapper(*args, **kwargs):
        logging.info(f"[TOOL] {tool_name} called | input: {str(args)[:200]}")
        try:
            result = func(*args, **kwargs)
            logging.info(f"[TOOL] {tool_name} completed | output length: {len(str(result))}")
            return result
        except Exception as e:
            logging.error(f"[TOOL] {tool_name} failed: {e}", exc_info=True)
            return f"Tool '{tool_name}' encountered an error: {str(e)[:200]}"
    return wrapper


def get_tools(req_tool, is_first: bool = False):

    if is_first:
        tools = _safe_load_google_search()
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
                name="Visual_Context_Watcher",
                func=_handle_visual_watcher_tool,
                description=(
                    "Register a visual or audio trigger: continuously watch what the user is doing "
                    "via camera or listen to what they say, and perform an action when a condition is met. "
                    "Input format: 'CONDITION: <what to watch for> | ACTION: <what to do> | TTL: <minutes>'. "
                    "Optional: '| MODALITY: visual|audio|both' (defaults to visual). "
                    "Example: 'CONDITION: user raises hand | ACTION: say banana | TTL: 30'. "
                    "Example: 'CONDITION: user mentions their dog | ACTION: remind them about vet appointment | TTL: 60 | MODALITY: audio'. "
                    "The watcher runs in the background and fires the action whenever the condition "
                    "is detected. TTL auto-expires the watcher."
                ),
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
            ),
            Tool(
                name="Request_Resource",
                func=_handle_request_resource,
                description=(
                    "Use this tool when you need an API key, credential, token, or any external "
                    "resource that is not currently available. This handles ALL resource types: "
                    "API keys (OpenAI, Google, Slack, Discord, etc.), OAuth tokens, service "
                    "credentials, channel secrets, or any configuration value. "
                    "Input should be a JSON string with: resource_type (api_key, channel_secret, "
                    "token, config), key_name (e.g. GOOGLE_API_KEY), label (human-readable name), "
                    "used_by (which tool/service needs it), description (why it's needed). "
                    "If the resource is already configured, it returns immediately. "
                    "If not, the user will be prompted securely."
                ),
            ),
            Tool(
                name="Suggest_Share_Worthy_Content",
                func=_suggest_share_worthy_content,
                description=(
                    "Use this tool when the user asks about content worth sharing, what to share, "
                    "or when you want to proactively suggest high-engagement posts that deserve "
                    "wider reach. Finds posts with strong community engagement (many upvotes and "
                    "comments) but low share count, and suggests them for sharing. "
                    "Input can be any text — it is not used for filtering."
                ),
            ),
            Tool(
                name="Observe_User_Experience",
                func=_observe_user_experience,
                description=(
                    "Record a user experience observation. Input: JSON with event, page, "
                    "duration_ms, outcome. Used for self-improvement and understanding user "
                    "behavior patterns."
                ),
            ),
            Tool(
                name="Self_Critique_And_Enhance",
                func=_self_critique_and_enhance,
                description=(
                    "Review past agent suggestions and user behavior observations to improve "
                    "future recommendations. Input: topic or area to critique. Helps the agent "
                    "learn from its own interactions."
                ),
            ),
            Tool(
                name="Agentic_Router",
                func=_handle_agentic_router_tool,
                description=(
                    "Use when the user's request requires multi-step execution such as building "
                    "an application, writing code, conducting research, creating a marketing "
                    "campaign, or any complex task that cannot be answered in a single response. "
                    "Input: the user's full request describing what they want accomplished. "
                    "Output: a structured plan with steps. Do NOT use for simple questions, "
                    "greetings, or tasks that can be answered directly."
                ),
            ),

        ]

        # Service Tools: Add HTTP microservice tools (Crawl4AI, AceStep, etc.)
        try:
            from integrations.service_tools import service_tool_registry
            tool += service_tool_registry.get_langchain_tools()
        except ImportError:
            pass

        # HART Skills: Ingest agent skills (Claude Code, Markdown, GitHub)
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
        # Wrap all tool functions with logging
        for t in tools:
            if hasattr(t, 'func') and callable(t.func):
                t.func = _with_tool_logging(t.func, t.name)
            elif hasattr(t, '_run') and callable(t._run):
                t._run = _with_tool_logging(t._run, t.name)
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
            tools = _safe_load_google_search()
            tools += req_tool_from_user

        else:
            tool_description = ""
            tool_func = ""
            tools = _safe_load_google_search()
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
            ),
            Tool(
                name="Request_Resource",
                func=_handle_request_resource,
                description=(
                    "Use this tool when you need an API key, credential, token, or any external "
                    "resource that is not currently available. This handles ALL resource types: "
                    "API keys (OpenAI, Google, Slack, Discord, etc.), OAuth tokens, service "
                    "credentials, channel secrets, or any configuration value. "
                    "Input should be a JSON string with: resource_type (api_key, channel_secret, "
                    "token, config), key_name (e.g. GOOGLE_API_KEY), label (human-readable name), "
                    "used_by (which tool/service needs it), description (why it's needed). "
                    "If the resource is already configured, it returns immediately. "
                    "If not, the user will be prompted securely."
                ),
            ),
            Tool(
                name="Suggest_Share_Worthy_Content",
                func=_suggest_share_worthy_content,
                description=(
                    "Use this tool when the user asks about content worth sharing, what to share, "
                    "or when you want to proactively suggest high-engagement posts that deserve "
                    "wider reach. Finds posts with strong community engagement (many upvotes and "
                    "comments) but low share count, and suggests them for sharing. "
                    "Input can be any text — it is not used for filtering."
                ),
            ),
            Tool(
                name="Observe_User_Experience",
                func=_observe_user_experience,
                description=(
                    "Record a user experience observation. Input: JSON with event, page, "
                    "duration_ms, outcome. Used for self-improvement and understanding user "
                    "behavior patterns."
                ),
            ),
            Tool(
                name="Self_Critique_And_Enhance",
                func=_self_critique_and_enhance,
                description=(
                    "Review past agent suggestions and user behavior observations to improve "
                    "future recommendations. Input: topic or area to critique. Helps the agent "
                    "learn from its own interactions."
                ),
            ),
            Tool(
                name="Agentic_Router",
                func=_handle_agentic_router_tool,
                description=(
                    "Use when the user's request requires multi-step execution such as building "
                    "an application, writing code, conducting research, creating a marketing "
                    "campaign, or any complex task that cannot be answered in a single response. "
                    "Input: the user's full request describing what they want accomplished. "
                    "Output: a structured plan with steps. Do NOT use for simple questions, "
                    "greetings, or tasks that can be answered directly."
                ),
            ),

        ]
        final_tool = []
        for new_tool in tool:
            if new_tool not in tools:
                final_tool.append(new_tool)

        tools += final_tool

        # Wrap all tool functions with logging
        for t in tools:
            if hasattr(t, 'func') and callable(t.func):
                t.func = _with_tool_logging(t.func, t.name)
            elif hasattr(t, '_run') and callable(t._run):
                t._run = _with_tool_logging(t._run, t.name)

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
                except Exception:
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
                    f"LangChain action parse failed (non-JSON response): {e}")
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

            return end_result
            # return response_from_groq.content.replace('\n', ' ').replace('\t', '')
        if checker == 1:
            try:
                # Extract text from OpenAI-compatible response format
                text = str(response.json()["choices"][0]["message"]["content"])
                try:
                    text = text.strip('`').replace('json\n', '').strip()
                except Exception:
                    pass
                intents = json.loads(text)

                curr_intent = intents["action"]
                if self.previous_intent == curr_intent:
                    self.call_gpt4 = 1
                self.previous_intent = curr_intent
                thread_local_data.update_recognize_intents(intents["action"])
            except Exception as e:
                app.logger.info(
                    f"LangChain action parse failed (non-JSON response): {e}")
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
                     return_only_outputs: bool = False) -> Dict[str, str]:
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
                self.memory.save_context(inputs, outputs, metadata=metadata)
                # Force immediate flush so next request sees the buffer on disk
                if hasattr(self.memory, 'chat_memory') and hasattr(self.memory.chat_memory, 'flush_sync'):
                    self.memory.chat_memory.flush_sync()
                app.logger.info(
                    f"After: memory saved successfully with metadata {metadata}, "
                    f"buffer_size={len(self.memory.chat_memory.messages) if hasattr(self.memory, 'chat_memory') else '?'}")
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
                chat_hist = external_context.get('chat_history', [])
                app.logger.info(
                    f"Memory loaded: {len(chat_hist)} messages in chat_history"
                    f" (buffer_file={getattr(getattr(self.memory, 'chat_memory', None), '_buffer_file', '?')})")
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

        # Privacy-first: use .get() with graceful defaults for local/guest users
        # who have no cloud profile. Missing fields are noted so the agent can
        # ask the user naturally and store in local DB when volunteered.
        _uname = user_data.get("name") or user_data.get("display_name") or user_data.get("username") or "User"
        _gender = user_data.get("gender", "not specified")
        _lang = user_data.get("preferred_language", "not specified")
        _dob = user_data.get("dob", "not specified")
        _eng = user_data.get("english_proficiency", "not specified")
        _created = user_data.get("created_date", "unknown")
        _standard = user_data.get("standard", "not specified")
        _pays = user_data.get("who_pays_for_course", "not specified")

        user_details = f'''Below are the information about the user.
        user_name: {_uname} (Call the user by this name only when required and not always), gender: {_gender}, who_pays_for_course: {_pays}(Entity Responsible for Paying the Course Fees), preferred_language: {_lang}(User's Preferred Language), date_of_birth: {_dob}, english_proficiency: {_eng}(User's English Proficiency Level), created_date: {_created}(user creation date), standard: {_standard}(User's Standard in which user studying)
        If any of the above fields show "not specified", do not ask the user for this information proactively. Only note it when naturally relevant. The user's privacy is paramount — store preferences locally when volunteered, never push for personal data.
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
    except Exception:
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


def parse_link_for_crwalab(inp):
    """Extract content from a URL (PDF or website).

    The agent sees every intermediate step (progress log) and the final
    extracted content directly — no opaque HTTP calls to external services.
    """
    inp_list = inp.split(',')
    input_url = inp_list[0].strip()
    link_type = inp_list[1].strip() if len(inp_list) > 1 else 'website'
    user_id = thread_local_data.get_user_id()
    request_id = thread_local_data.get_request_id()

    app.logger.info(f"Data extraction: url={input_url}, type={link_type}")

    # Publish task status so longrunning monitor knows
    post_dict = {
        'user_id': user_id, 'task_type': 'sync',
        'status': TaskStatus.EXECUTING.value,
        'task_name': TaskNames.CRAWLAB.value,
        'uid': request_id,
        'task_id': f"{TaskNames.CRAWLAB.value}_{request_id}",
        'request_id': request_id,
    }
    publish_async('com.hertzai.longrunning.log', post_dict)

    def _publish_thinking(msg):
        """Push progress to UI via Crossbar thinking bubble."""
        try:
            crossbar_msg = json.dumps({
                "text": [msg], "priority": 49,
                "action": "Thinking", "bot_type": "Agent",
                "historical_request_id": [], "options": [], "newoptions": [],
                "request_id": request_id,
            })
            publish_async(f'com.hertzai.hevolve.chat.{user_id}', crossbar_msg)
        except Exception:
            pass

    try:
        if link_type == 'pdf':
            return _parse_pdf_in_process(input_url, user_id, request_id)
        else:
            # Website: crawl in-process, agent + UI see progress
            _publish_thinking(f"Crawling {input_url}...")
            from integrations.web_crawler import crawl_url
            result = crawl_url(input_url, timeout=30)

            if result['success']:
                _publish_thinking(f"Extracted {result['word_count']} words from {input_url}")
                content = result['markdown']
                if len(content) > 8000:
                    truncate_pos = content.rfind('.', 0, 8000)
                    if truncate_pos > 6000:
                        content = content[:truncate_pos + 1] + "\n[Content truncated]"
                    else:
                        content = content[:8000] + "\n[Content truncated]"
                parts = []
                if result.get('progress'):
                    parts.append("--- Progress ---")
                    parts.append(result['progress'])
                    parts.append("--- Result ---")
                parts.append(f"URL: {input_url}")
                parts.append(f"Words extracted: {result['word_count']}")
                parts.append(f"Content:\n{content}")
                return "\n".join(parts)
            else:
                _publish_thinking(f"Crawl failed: {result.get('error', 'unknown')}")
                return f"Failed to crawl {input_url}: {result.get('error', 'unknown')}"

    except Exception as e:
        app.logger.error(f"Data extraction failed: {e}")
        post_dict['status'] = TaskStatus.ERROR.value
        post_dict['failure_reason'] = str(e)
        publish_async('com.hertzai.longrunning.log', post_dict)
        return f"Failed to extract content from {input_url}: {e}"


def _parse_pdf_in_process(input_url, user_id, request_id):
    """Parse PDF in-process. Agent sees every step, UI sees percentage progress bar.

    Publishes to com.hertzai.bookparsing.{user_id} with {percentage, page_number, ...}
    — same pattern as the cloud pipeline (wrapper.py). Frontend crossbarWorker.js
    detects 'percentage' field → PROGRESS_UPDATE → ChatMessageList progress bar.

    Downloads → converts to images → Qwen Vision per page → ToC → chapters → book name.
    Returns full progress log + extracted content as a single string.
    """
    progress = []
    _total_pages = [0]  # mutable for closure
    _filename = [input_url.split("/")[-1]]

    def step(msg, percentage=None, page_number=None):
        progress.append(msg)
        app.logger.info(msg)
        # Publish percentage progress to bookparsing topic (UI progress bar)
        try:
            payload = {
                "request_id": request_id,
                "bot_type": "Agent",
                "filename": _filename[0],
            }
            if percentage is not None:
                payload["percentage"] = int(percentage)
            if page_number is not None:
                payload["page_number"] = page_number
            payload["text"] = [msg]
            if _total_pages[0] > 0:
                payload["file_id"] = request_id  # use request_id as identifier

            publish_async(
                f'com.hertzai.bookparsing.{user_id}',
                json.dumps(payload),
            )
        except Exception:
            pass

    # Step 1: Download PDF
    step(f"Downloading PDF from {input_url}...")
    response = pooled_get(input_url, timeout=60)
    pdf_file_name = input_url.split("/")[-1]
    if not pdf_file_name.endswith('.pdf'):
        pdf_file_name += '.pdf'

    upload_dir = os.path.join(os.getcwd(), 'upload')
    os.makedirs(upload_dir, exist_ok=True)
    pdf_save_path = os.path.join(upload_dir, pdf_file_name)
    with open(pdf_save_path, 'wb') as f:
        f.write(response.content)
    step(f"PDF saved: {len(response.content)} bytes")

    try:
        # Import parsing functions from Nunba routes (same process)
        from routes.upload_routes import (
            _pdf_to_images, _parse_page_via_vision,
            _assign_chapters_to_pages, _generate_book_name,
            _save_parse_to_db,
        )

        # Step 2: Convert PDF to page images
        step("Converting PDF to page images...", percentage=2)
        pages = _pdf_to_images(pdf_save_path)
        if not pages:
            step("FAILED: Could not convert PDF to images")
            return "\n".join(progress) + "\nError: PDF conversion failed. Is pdf2image or PyMuPDF installed?"
        _total_pages[0] = len(pages)
        _filename[0] = pdf_file_name
        step(f"Converted to {len(pages)} page images", percentage=5)

        # Step 3: Parse each page via Qwen Vision
        results = []
        whole_text_parts = []
        toc_entries = []

        for page_num, img_path in pages:
            # percentage: 5% base + page progress scaled to 85% (5..90)
            pct = 5 + (page_num / len(pages)) * 85
            step(f"Parsing page {page_num}/{len(pages)} via Qwen Vision...",
                 percentage=pct, page_number=page_num)
            page_data = _parse_page_via_vision(page_num, img_path)
            results.append(page_data)
            page_text = page_data.get('text', '')
            whole_text_parts.append(page_text)
            if page_data.get('toc_entries'):
                toc_entries.extend(page_data['toc_entries'])
            word_count = len(page_text.split())
            pct = 5 + (page_num / len(pages)) * 85
            step(f"Page {page_num}: type={page_data.get('page_type', '?')}, "
                 f"{word_count} words, {len(page_data.get('elements', []))} elements",
                 percentage=pct, page_number=page_num)

        # Step 4: Cross-page chapter assignment
        step("Assigning chapters from Table of Contents...", percentage=92)
        results = _assign_chapters_to_pages(results, toc_entries)
        if toc_entries:
            step(f"Found {len(toc_entries)} ToC entries, assigned chapters", percentage=94)
        else:
            step("No ToC found — skipping chapter assignment", percentage=94)

        # Step 5: Generate book name
        step("Generating book title...", percentage=95)
        book_name = None
        if whole_text_parts:
            book_name = _generate_book_name(
                whole_text_parts[0][:500] if whole_text_parts[0] else '',
                toc_entries
            )
        step(f"Book title: {book_name or '(could not determine)'}", percentage=97)

        # Step 6: Save to DB
        step("Saving to database...", percentage=98)
        whole_text = '\n\n'.join(whole_text_parts)
        try:
            from routes.db_routes import _get_db
            from datetime import datetime, timezone as tz
            conn = _get_db()
            now = datetime.now(tz.utc).isoformat()
            cursor = conn.execute(
                """INSERT INTO pdf_files (user_id, filename, directory, request_id, created_date)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, pdf_file_name, upload_dir, request_id, now)
            )
            conn.commit()
            file_id = cursor.lastrowid
            conn.close()
            _save_parse_to_db(file_id, results, whole_text, toc_entries, book_name, user_id)
            step(f"Saved to DB: file_id={file_id}", percentage=99)
        except Exception as db_err:
            step(f"DB save skipped: {db_err}")

        # Build agent-visible output
        step(f"Complete: {len(pages)} pages, {len(whole_text.split())} total words",
             percentage=100)

        # Truncate whole_text for agent context
        content_for_agent = whole_text
        if len(content_for_agent) > 8000:
            truncate_pos = content_for_agent.rfind('.', 0, 8000)
            if truncate_pos > 6000:
                content_for_agent = content_for_agent[:truncate_pos + 1] + "\n[Content truncated]"
            else:
                content_for_agent = content_for_agent[:8000] + "\n[Content truncated]"

        return (
            f"--- PDF Parse Progress ---\n"
            f"{chr(10).join(progress)}\n"
            f"--- Extracted Content ---\n"
            f"File: {pdf_file_name}\n"
            f"Book: {book_name or 'Unknown'}\n"
            f"Pages: {len(pages)}\n"
            f"Total words: {len(whole_text.split())}\n"
            f"Chapters: {len(toc_entries)}\n"
            f"---\n{content_for_agent}"
        )

    except ImportError as ie:
        step(f"Import error: {ie} — falling back to hive mesh or HTTP")

        # Fallback 1: Try hive mesh peer with vision model
        try:
            from integrations.agent_engine.compute_mesh_service import get_compute_mesh
            mesh = get_compute_mesh()
            if mesh and mesh._peers:
                step("No local vision model — sending document to hive peer with GPU...")
                result = mesh.offload_to_best_peer(
                    model_type='vision',
                    prompt=f'Parse PDF document: {pdf_file_name}',
                    options={'image_path': pdf_save_path, 'timeout': 120},
                )
                if result and 'error' not in result:
                    step("Document parsed by hive peer", percentage=100)
                    return (
                        f"--- Progress ---\n{chr(10).join(progress)}\n"
                        f"--- Result (via hive peer) ---\n{result.get('response', '')}"
                    )
        except Exception as mesh_err:
            step(f"Hive mesh unavailable: {mesh_err}")

        # Fallback 2: Cloud HTTP
        if BOOKPARSING_API:
            step("Sending to cloud parsing service...")
            try:
                payload = {'user_id': user_id, 'request_id': request_id}
                with open(pdf_save_path, 'rb') as f:
                    files = [('file', (pdf_file_name, f, 'application/pdf'))]
                    resp = pooled_post(BOOKPARSING_API, data=payload, files=files, timeout=60)
                return (
                    f"--- Progress ---\n{chr(10).join(progress)}\n"
                    f"--- Result (via cloud) ---\n{resp.text}"
                )
            except Exception as cloud_err:
                step(f"Cloud parsing service unavailable: {cloud_err}")

        # All paths exhausted
        step("Document parsing requires a vision model (GPU). "
             "No local GPU, no hive peers with GPU, and the cloud service "
             "is not responding. Please try again when a GPU device is connected.")
        return "\n".join(progress)
    finally:
        try:
            os.remove(pdf_save_path)
        except OSError:
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

        # Tier 2: Hive mesh peer with GPU
        try:
            from integrations.agent_engine.compute_mesh_service import get_compute_mesh
            mesh = get_compute_mesh()
            result = mesh.offload_to_best_peer(
                model_type='vision', prompt=prompt_text,
                options={'image_path': image_path, 'timeout': 60},
            )
            if result and 'error' not in result:
                return result.get('response', str(result))
        except Exception as e:
            app.logger.debug("Hive mesh vision offload not available: %s", e)

        # Tier 3: Cloud MiniCPM fallback
        from core.config_cache import get_vision_api
        url = get_vision_api() or "http://azurekong.hertzai.com:8000/minicpm/upload"
        payload = {'prompt': prompt_text}
        fh = open(image_path, 'rb')
        try:
            files = [
                ('file', ('call.jpg', fh, 'image/jpeg'))
            ]
            response = pooled_post(url, headers={}, data=payload, files=files, timeout=30)
            app.logger.info(response.text)
            return response.text
        except Exception as e:
            app.logger.error('Got error in visual QA (cloud fallback): %s', e)
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
                    except Exception:
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
                    except Exception:
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
            is_termination_msg=_is_terminate_msg,
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
            is_termination_msg=_is_terminate_msg,
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
            is_termination_msg=_is_terminate_msg,
            system_message=recipe
        )

        # Create user agent
        user = autogen.ConversableAgent(
            name=f"user_{user_id}",
            is_termination_msg=_is_terminate_msg,
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
    # Skip action history fetch for casual conversations — they don't use it
    # in the prompt, and the HTTP call to ACTION_API adds 5+ seconds latency.
    if casual_conv:
        user_details = "Casual conversation mode."
        actions = ""
        app.logger.info("Skipped get_action_user_details (casual_conv=True)")
    else:
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

    # Build dynamic identity for fast-path too
    _fast_agent_config = thread_local_data.get_agent_config() if hasattr(thread_local_data, 'get_agent_config') else None
    _fast_identity = build_identity_prompt(_fast_agent_config, '', user_details)

    prefix = f"""{_fast_identity}
        Answer questions accurately and respond as quickly as possible in {language}.
        Keep responses under 200 words. Be colloquial and natural - don't always greet or use the user's name.
        IMPORTANT: Do NOT re-introduce yourself if you already did in the conversation history below. Continue naturally.

        User details: {user_details}
        Context: {custom_prompt}

        You can help with anything — answering questions, coding, research, teaching, creative writing, data analysis, building agents, brainstorming, and more.
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


Hevolve = "You are Hevolve — a place where everything is possible. A personal AI by HertzAI that runs locally, respecting privacy. You help users BUILD — code, ideas, businesses, knowledge, agents, art, solutions, and anything they imagine. You are a builder's companion."
PROBE_TEMPLATE = ("You are Hevolve, a versatile personal AI developed by HertzAI that runs locally on the user's device. Weave the conversation "
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
INTERMEDIATE_CONTINUATION = "You are Hevolve, a versatile personal AI developed by HertzAI that runs locally on the user's device. Continue your response from where you left off in the last conversation, considering the new input as a continuation of the last request. Ensure a smooth transition from the previous response and start this response as a continuation of the previous one.\n INSTRUCTIONS: Start your response with transitional words or phrases that can be used as a continuation of the previous response."

first_promts = []
review_agents = {}  # keyed by f'{user_id}_{prompt_id}' to avoid cross-prompt collision
conversation_agent = {}  # keyed by f'{user_id}_{prompt_id}'
_state_lock = threading.Lock()  # Protects review_agents, conversation_agent, first_promts

# --- Interactive gather_info turn limit ---
# After MAX_GATHER_TURNS without completion, force-complete with available data
MAX_GATHER_TURNS = 12
_gather_turn_counts = {}  # f'{user_id}_{prompt_id}' -> int

# --- TTL-based cleanup for review_agents / conversation_agent (M2 fix) ---
_AGENT_TTL = 3600  # 1 hour
_agent_timestamps = {}  # f'{user_id}_{prompt_id}' -> last-access epoch


def _touch_agent_timestamp(agent_key):
    """Record that an agent entry was accessed (call under _state_lock)."""
    _agent_timestamps[agent_key] = time.time()


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
    # Also clean stale gather turn counters
    # Both _gather_turn_counts and _agent_timestamps now use '{user_id}_{prompt_id}' keys
    for tk in list(_gather_turn_counts.keys()):
        if tk not in _agent_timestamps:
            _gather_turn_counts.pop(tk, None)

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
            # Handle list-of-dicts response from LLM
            if isinstance(parsed, list):
                for item in reversed(parsed):
                    if isinstance(item, dict) and 'status' in item:
                        parsed = item
                        break
                else:
                    parsed = {}
            if isinstance(parsed, dict) and parsed.get('status', '').lower() == 'completed':
                # Save agent config
                parsed['prompt_id'] = prompt_id
                parsed['creator_user_id'] = user_id
                name = os.path.join(PROMPTS_DIR, f'{prompt_id}.json')
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

    # Fallback: save partial config so the pipeline can recover
    app.logger.warning(f'Autonomous gather_info did not complete in {max_iterations} iterations, saving partial config')
    partial = {
        'status': 'completed',
        'name': f'Agent {prompt_id}',
        'agent_name': f'auto.agent{str(prompt_id)[-4:]}',
        'goal': description or 'General assistant',
        'broadcast_agent': 'no',
        'personas': [{'name': 'Assistant', 'description': 'General purpose assistant'}],
        'flows': [{'flow_name': 'main', 'persona': 'Assistant', 'actions': [{'action': 'Respond to user', 'action_id': 1, 'status': 'pending'}], 'sub_goal': description or 'Help the user'}],
        'extra_information': f'Auto-generated after {max_iterations} autonomous gather iterations',
        'prompt_id': prompt_id,
        'creator_user_id': user_id,
    }
    name = os.path.join(PROMPTS_DIR, f'{prompt_id}.json')
    try:
        with open(name, 'w') as f:
            json.dump(partial, f)
        app.logger.info(f'Partial autonomous agent config saved to {name}')
        # Sync to cloud DB (non-fatal)
        try:
            pooled_post(
                f'{DB_URL}/createpromptlist',
                json={'listprompts': [{
                    'prompt_id': prompt_id,
                    'prompt': partial.get('goal', ''),
                    'user_id': user_id,
                    'name': partial.get('name', ''),
                    'is_active': True,
                    'image_url': '',
                }]},
                timeout=5)
        except Exception:
            pass
    except Exception as e:
        app.logger.error(f'Failed to save partial autonomous config: {e}')
    return 'Autonomous gathering completed with partial config. Moving to review.'


# Resonance tuning post-response hook
def _tune_resonance_after_chat(user_id, prompt_text, response_text):
    """Tune resonance and return summary for crossbar/response piggyback.

    Returns dict with resonance state (or empty dict on failure).
    Sync — EMA math is <1ms, no LLM calls.
    Also dispatches signals to HevolveAI async in background.
    """
    try:
        from core.resonance_tuner import get_resonance_tuner
        if user_id and prompt_text and response_text:
            tuner = get_resonance_tuner()
            profile = tuner.analyze_and_tune(
                str(user_id), str(prompt_text), str(response_text))
            return {
                'resonance_confidence': round(profile.resonance_confidence, 2),
                'resonance_tuning': {
                    k: round(v, 3) for k, v in profile.tuning.items()
                },
                'resonance_interactions': profile.total_interactions,
            }
    except ImportError:
        pass
    except Exception:
        pass
    return {}


@app.route('/chat', methods=['POST'])
def chat():
    # Rate limit: 30 req/min per user/IP
    try:
        from integrations.social.rate_limiter import _limiter
        # Rate limit by IP always (prevents user_id rotation bypass).
        # Authenticated user_id added as secondary key for per-user tracking.
        rate_user = request.remote_addr
        if not _limiter.check(str(rate_user), 'chat', max_tokens=30, refill_rate=30 / 60):
            return jsonify({'error': 'Rate limit exceeded (30/min). Please wait.', 'response': None}), 429
    except ImportError:
        pass  # Rate limiter module not installed — allow (dev/flat mode)
    except Exception as e:
        # Rate limiter unavailable (Redis down, etc.) — fail closed on cloud
        if os.environ.get('HEVOLVE_NODE_TIER') == 'central':
            return jsonify({'error': 'Rate limiter unavailable — try again shortly', 'response': None}), 503

    start_time = time.time()

    # Periodically evict stale agent state to prevent unbounded memory growth (M2 fix)
    _cleanup_stale_agents()

    data = request.get_json()

    # ── Two-layer auth: extract user_id from JWT if present ──
    # Layer 1 (LOCAL): Bearer token signed by this node's HS256 secret
    # Layer 2 (HIVE): Bearer token with Ed25519 node_sig (cross-node)
    # Fallback: body user_id (backward compat for desktop/Nunba mode)
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        try:
            from integrations.social.auth import decode_jwt
            jwt_payload = decode_jwt(auth_header[7:])
            if jwt_payload and 'user_id' in jwt_payload:
                data['user_id'] = jwt_payload['user_id']
                g.auth_source = 'jwt'
                g.token_scope = jwt_payload.get('scope', 'local')
            else:
                g.auth_source = 'body'
        except Exception:
            g.auth_source = 'body'
    else:
        g.auth_source = 'body'

    # Reject unauthenticated requests on exposed deployments
    # (central tier or HEVOLVE_REQUIRE_AUTH=true)
    if g.auth_source == 'body' and (
        os.environ.get('HEVOLVE_NODE_TIER') == 'central' or
        os.environ.get('HEVOLVE_REQUIRE_AUTH', '').lower() == 'true'
    ):
        return jsonify({
            'error': 'Authentication required. Provide Authorization: Bearer <token> header.',
            'response': None,
        }), 401

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
    task_source = data.get('task_source', 'own')
    thread_local_data.set_task_source(task_source)
    channel_context = data.get('channel_context', None)
    if channel_context:
        thread_local_data.channel_context = channel_context

    # USER PRIORITY: mark user activity so daemon dispatch yields the LLM
    if not autonomous:
        try:
            from integrations.agent_engine.dispatch import mark_user_chat_activity
            mark_user_chat_activity()
        except ImportError:
            pass

    app.logger.info(f"casual_conv type {casual_conv}")

    # Security: sanitize prompt_id to prevent path traversal
    if prompt_id is not None:
        prompt_id = str(prompt_id)
        if not re.match(r'^[a-zA-Z0-9_-]+$', prompt_id):
            return jsonify({'error': 'Invalid prompt_id format', 'response': None}), 400

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
                return jsonify({'error': f'Guardrail: {reason}', 'response': None}), 403
        except ImportError:
            pass

    # SECURITY: redact secrets (API keys, tokens, passwords) from user prompts
    if prompt:
        try:
            from security.secret_redactor import redact_secrets
            prompt, _redacted_count = redact_secrets(prompt)
        except ImportError:
            pass

    # BUDGET GATE: estimate and log LLM cost before execution
    if prompt:
        try:
            from integrations.agent_engine.budget_gate import estimate_llm_cost_spark
            _est_cost = estimate_llm_cost_spark(prompt)
            app.logger.debug(f"Estimated LLM cost: {_est_cost} Spark for user={user_id}")
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
                return jsonify({'error': f'Input rejected: {reason}', 'response': None}), 400
        except Exception:
            pass  # Degrade gracefully

    # --- Agentic execution after user consent (Plan Mode → execute) ---
    agentic_execute = data.get('agentic_execute', False)
    if agentic_execute and prompt and user_id:
        agentic_plan = data.get('agentic_plan', {})
        matched_agent_id = agentic_plan.get('matched_agent_id')
        app.logger.info(f'Agentic execute: matched_agent={matched_agent_id}, user={user_id}')

        if matched_agent_id and os.path.exists(os.path.join(PROMPTS_DIR, f'{matched_agent_id}.json')):
            # Route to existing agent via chat_agent (reuse_recipe.py)
            prompt_id = matched_agent_id
            # Fall through to the existing prompt_id routing below
        else:
            # No matching agent — auto-create one, then execute
            # Reuse prompt_id from Plan Mode response if available
            new_prompt_id = prompt_id if prompt_id else _next_prompt_id()
            auto_response = _autonomous_gather_info(user_id, prompt, new_prompt_id)
            _ak = f'{user_id}_{new_prompt_id}'
            review_agents[_ak] = True
            _touch_agent_timestamp(_ak)
            _record_lifecycle('Review Mode', user_id, new_prompt_id,
                              f'Agentic auto-creation: {prompt[:100]}')
            _push_workflow_flowchart(user_id, new_prompt_id, request_id)
            return jsonify({
                'response': auto_response,
                'intent': ['FINAL_ANSWER'],
                'Agent_status': 'Review Mode',
                'autonomous_creation': True,
                'prompt_id': new_prompt_id,
                'req_token_count': 0,
                'res_token_count': 0,
                'history_request_id': [],
            })

    if prompt_id:
        # System agents (like Nunba) route directly to langchain casual chat
        # instead of entering gather_info/CREATE mode
        _prompt_path = os.path.join(PROMPTS_DIR, f'{prompt_id}.json')
        if os.path.exists(_prompt_path):
            try:
                with open(_prompt_path, 'r') as _pf:
                    _agent_meta = json.load(_pf)
                if _agent_meta.get('is_system_agent'):
                    # System agent: use its system_prompt for langchain casual chat
                    _sys_prompt = ''
                    if _agent_meta.get('flows') and _agent_meta['flows'][0].get('system_prompt'):
                        _sys_prompt = _agent_meta['flows'][0]['system_prompt']
                    casual_conv = True
                    custom_prompt = _sys_prompt
                    prompt_id = None  # Skip CREATE/REUSE routing, fall through to get_ans()
                    app.logger.info(f"System agent '{_agent_meta.get('name')}' routed to casual chat")
            except Exception:
                pass

        # Per-user lock prevents concurrent requests from corrupting agent state.
        # Replaces the global _state_lock for better concurrency.
        _user_lock = _get_user_lock(user_id)
        with _user_lock:
            if prompt_id and os.path.exists(os.path.join(PROMPTS_DIR, f'{prompt_id}.json')):
                app.logger.info('GATHER JSON EXISTS')
                if os.path.exists(os.path.join(PROMPTS_DIR, f'{prompt_id}_0_recipe.json')):
                    app.logger.info('0 Recipe JSON EXISTS')
                    file_path = os.path.join(PROMPTS_DIR, f'{prompt_id}.json')
                    with open(file_path, 'r') as f:
                        data = json.load(f)
                        no_of_flow = len(data['flows'])-1
                        app.logger.info(f'GOT LEN OF FLOW AS {no_of_flow}')
                    if os.path.exists(os.path.join(PROMPTS_DIR, f'{prompt_id}_{no_of_flow}_recipe.json')):
                        create_agent = set_flags_to_enter_review_mode(no_of_flow, user_id, prompt_id) #returns false
                    else:
                        app.logger.info(f'{no_of_flow} Recipe JSON doesnot EXISTS')
                        create_agent = True
                        _ak = f'{user_id}_{prompt_id}'
                        review_agents[_ak] = True
                        conversation_agent[_ak] = False
                        _touch_agent_timestamp(_ak)
                else:
                    app.logger.info('0 Recipe JSON doesnot EXISTS')
                    create_agent = True
                    _ak = f'{user_id}_{prompt_id}'
                    review_agents[_ak] = True
                    conversation_agent[_ak] = False
                    _touch_agent_timestamp(_ak)

            else:
                app.logger.info('GATHER JSON doesnot EXISTS')
                create_agent = True
                _ak = f'{user_id}_{prompt_id}'
                review_agents[_ak] = False
                conversation_agent[_ak] = True
                _touch_agent_timestamp(_ak)

    if create_agent:
        # Generate prompt_id server-side if not provided
        if not prompt_id:
            prompt_id = _next_prompt_id()
            app.logger.info(f'Generated server-side prompt_id={prompt_id} for new agent')
        # Per-user lock: snapshot agent state flags (lock NOT held during LLM calls)
        _user_lock = _get_user_lock(user_id)
        _ak = f'{user_id}_{prompt_id}'
        with _user_lock:
            _in_review = _ak in review_agents and review_agents[_ak]
            _in_convo = _ak in conversation_agent and conversation_agent[_ak]
        # Phase 1: Gather Requirements
        if not _in_review:
            with _user_lock:
                review_agents[_ak] = False
                _touch_agent_timestamp(_ak)
            prompt = data.get('prompt', None)
            if prompt_id not in first_promts:
                first_promts.append(prompt_id)
                try:
                    res = pooled_get(
                        f'{DB_URL}/getprompt/?prompt_id={prompt_id}', timeout=5).json()
                    if res and isinstance(res, list) and len(res) > 0:
                        prompt = prompt+f" name:{res[0]['name']} goal:{res[0]['prompt']}"
                    else:
                        app.logger.debug(f'No cloud record for prompt_id={prompt_id} (new agent)')
                except Exception:
                    app.logger.debug(f'Cloud DB unreachable for prompt_id={prompt_id}, using local-only')
            if not user_id or not prompt:
                return jsonify({'response': 'Need user_id and text to create agent', 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': []})
            if autonomous:
                # Autonomous dispatch (from daemon or API): LLM self-generates agent config
                # AND immediately creates recipe — no human review step needed.
                # Full pipeline: gather_info → save config → recipe() → completed
                auto_response = _autonomous_gather_info(user_id, prompt, prompt_id)

                # Now immediately create the recipe so next dispatch enters REUSE
                _config_path = os.path.join(PROMPTS_DIR, f'{prompt_id}.json')
                if os.path.exists(_config_path):
                    try:
                        recipe_response = recipe(user_id, prompt, prompt_id, file_id, request_id)
                        if recipe_response == 'Agent Created Successfully':
                            with _user_lock:
                                review_agents[_ak] = True
                                conversation_agent[_ak] = True
                                _touch_agent_timestamp(_ak)
                            try:
                                _create_social_agent_from_prompt(user_id, prompt_id)
                            except Exception:
                                pass
                            _record_lifecycle('completed', user_id, prompt_id,
                                             f'Autonomous full pipeline: gather + recipe in one shot')
                            _push_workflow_flowchart(user_id, prompt_id, request_id)
                            return jsonify({'response': recipe_response, 'intent': ['FINAL_ANSWER'],
                                            'req_token_count': 0, 'res_token_count': 0,
                                            'history_request_id': [],
                                            'Agent_status': 'completed',
                                            'autonomous_creation': True, 'prompt_id': prompt_id})
                        else:
                            app.logger.info(f'Autonomous recipe() returned: {str(recipe_response)[:100]}')
                    except Exception as e:
                        app.logger.warning(f'Autonomous recipe creation failed (will retry on next dispatch): {e}')

                # Fallback: config saved but recipe failed — next dispatch will retry
                with _user_lock:
                    review_agents[_ak] = True
                    conversation_agent[_ak] = False
                    _touch_agent_timestamp(_ak)
                _record_lifecycle('Review Mode', user_id, prompt_id,
                                 f'Autonomous creation via dispatch: {prompt[:100]}')
                _push_workflow_flowchart(user_id, prompt_id, request_id)
                return jsonify({'response': auto_response, 'intent': ['FINAL_ANSWER'],
                                'req_token_count': 0, 'res_token_count': 0,
                                'history_request_id': [],
                                'Agent_status': 'Review Mode',
                                'autonomous_creation': True, 'prompt_id': prompt_id})
            from gather_agentdetails import gather_info

            # --- Turn counter: force-complete after MAX_GATHER_TURNS ---
            turn_key = f'{user_id}_{prompt_id}'
            _gather_turn_counts[turn_key] = _gather_turn_counts.get(turn_key, 0) + 1
            turn_num = _gather_turn_counts[turn_key]
            app.logger.info(f'gather_info turn {turn_num}/{MAX_GATHER_TURNS} for {turn_key}')

            if turn_num >= MAX_GATHER_TURNS:
                # Force completion: ask LLM to wrap up with whatever it has
                app.logger.warning(f'gather_info hit max turns ({MAX_GATHER_TURNS}), forcing completion')
                prompt = ('Please finalize and return the completed agent configuration NOW with '
                          'status="completed". Use reasonable defaults for any missing fields. '
                          'Return the full JSON immediately.')

            response = gather_info(user_id, prompt, prompt_id)
            new_response = response.replace('true','True').replace("false", "False")
            app.logger.info('AFTER GATHER INFO')

            # --- Helper: save completed agent config and transition to Review ---
            def _save_and_enter_review(agent_config):
                agent_config['prompt_id'] = prompt_id
                agent_config['creator_user_id'] = user_id
                with _user_lock:
                    conversation_agent[_ak] = False
                    _touch_agent_timestamp(_ak)
                name = os.path.join(PROMPTS_DIR, f'{prompt_id}.json')
                with open(name, "w") as json_file:
                    json.dump(agent_config, json_file)
                app.logger.info(f"Agent config saved to {name}")
                # Sync to cloud DB (non-fatal)
                try:
                    pooled_post(
                        f'{DB_URL}/createpromptlist',
                        json={'listprompts': [{
                            'prompt_id': prompt_id,
                            'prompt': agent_config.get('goal', ''),
                            'user_id': user_id,
                            'name': agent_config.get('name', ''),
                            'is_active': True,
                            'image_url': agent_config.get('image_url', ''),
                        }]},
                        timeout=5)
                except Exception:
                    pass
                with _user_lock:
                    review_agents[_ak] = True
                    _touch_agent_timestamp(_ak)
                _gather_turn_counts.pop(turn_key, None)  # Reset turn counter
                _record_lifecycle('Review Mode', user_id, prompt_id, 'Agent details gathered, entering review')

            try:
                # Parse gather_info response
                new_res = None

                # Detect context-exceeded errors before parsing
                if 'Context size has been exceeded' in response:
                    raise ValueError('LLM context size exceeded — cannot parse gather_info response')

                try:
                    new_res = retrieve_json(new_response)
                    app.logger.info(f"new_res: {new_res}")
                except Exception as e:
                    app.logger.error(f'Got some error while will try with re match error:{e}')
                    json_match = re.search(r'{[\s\S]*}', response)
                    if json_match:
                        new_res = json.loads(json_match.group(0))
                    else:
                        raise ValueError('No JSON in response')

                # retrieve_json can return None without raising — catch it early
                if new_res is None:
                    json_match = re.search(r'{[\s\S]*}', response)
                    if json_match:
                        new_res = json.loads(json_match.group(0))
                    else:
                        raise ValueError('retrieve_json returned None and no JSON found in response')

                # LLM sometimes returns a list of conversation turns instead of a single dict
                if isinstance(new_res, list):
                    app.logger.info(f'new_res is a list (len={len(new_res)}), extracting last dict with status')
                    for item in reversed(new_res):
                        if isinstance(item, dict) and 'status' in item:
                            new_res = item
                            break
                    else:
                        for item in reversed(new_res):
                            if isinstance(item, dict):
                                new_res = item
                                break
                        else:
                            raise ValueError(f'List response has no usable dict: {new_res}')
                    app.logger.info(f'Extracted dict: status={new_res.get("status")}')

                if new_res is None:
                    raise ValueError('new_res is None after parsing')

                if new_res.get('status') == 'pending' and turn_num < MAX_GATHER_TURNS:
                    app.logger.info('PENDING STATUS')
                    ans = new_res.get('question') or new_res.get('review_details', response)
                    _record_lifecycle('Creation Mode', user_id, prompt_id, f'Agent creation turn {turn_num}')
                    return jsonify({'response': ans, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Creation Mode', 'prompt_id': prompt_id})
                else:
                    # Completed (or forced completion after max turns)
                    app.logger.info('COMPLETED STATUS')
                    _save_and_enter_review(new_res)
                    _push_workflow_flowchart(user_id, prompt_id, request_id)
                    return jsonify({'response': 'Got Agent details successfully lets move on to review them one at a time', 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Review Mode', 'prompt_id': prompt_id})

            except Exception as e:
                app.logger.error(f'gather_info parse error on turn {turn_num}: {e}')
                # After repeated failures, salvage what we can and move forward.
                # For autonomous dispatches (agent daemon), salvage earlier (turn 3)
                # to avoid tight retry loops that waste resources.
                # For fatal errors (context exceeded, permission denied), salvage immediately.
                is_autonomous = request.json.get('autonomous', False)
                err_str = str(e)
                is_fatal = ('context size' in err_str.lower()
                            or 'permission denied' in err_str.lower()
                            or 'errno 13' in err_str.lower())
                if is_fatal:
                    salvage_threshold = 1  # Immediate salvage — retrying won't help
                elif is_autonomous:
                    salvage_threshold = 3
                else:
                    salvage_threshold = MAX_GATHER_TURNS - 2
                if turn_num >= salvage_threshold:
                    app.logger.warning(
                        f'Too many gather_info failures (turn {turn_num}, '
                        f'autonomous={is_autonomous}), salvaging partial config')
                    partial = {
                        'status': 'completed',
                        'name': f'Agent {prompt_id}',
                        'agent_name': f'auto.agent{str(prompt_id)[-4:]}',
                        'goal': prompt or 'General assistant',
                        'broadcast_agent': 'no',
                        'personas': [{'name': 'Assistant', 'description': 'General purpose assistant'}],
                        'flows': [{'flow_name': 'main', 'persona': 'Assistant', 'actions': [{'action': 'Respond to user', 'action_id': 1, 'status': 'pending'}], 'sub_goal': prompt or 'Help the user'}],
                        'extra_information': f'Auto-generated after {turn_num} gather turns'
                    }
                    _save_and_enter_review(partial)
                    _record_lifecycle('Review Mode', user_id, prompt_id, f'Force-completed after {turn_num} failed turns')
                    return jsonify({'response': 'Agent created with available details. Moving to review.', 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Review Mode', 'prompt_id': prompt_id})
                _record_lifecycle('Creation Mode', user_id, prompt_id, f'Creation continuing after parse error: {e}')
                return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Creation Mode', 'prompt_id': prompt_id})
        # Phase 2: Review Phase (re-snapshot flags under lock after Phase 1 may have mutated)
        with _user_lock:
            _in_review = _ak in review_agents and review_agents[_ak]
            _in_convo = _ak in conversation_agent and conversation_agent[_ak]
        if _in_review and not _in_convo:
            response = recipe(user_id,prompt,prompt_id,file_id,request_id)
            if response =='Agent Created Successfully':
                with _user_lock:
                    conversation_agent[_ak] = True
                _touch_agent_timestamp(_ak)
                # Bridge: auto-create social identity for this agent
                try:
                    _create_social_agent_from_prompt(user_id, prompt_id)
                except Exception as e:
                    app.logger.debug(f"Social agent bridge skipped: {e}")
                _record_lifecycle('completed', user_id, prompt_id, 'Agent creation completed successfully')
                _push_workflow_flowchart(user_id, prompt_id, request_id)
                return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'completed'})
            _record_lifecycle('Review Mode', user_id, prompt_id, 'Agent details being reviewed')
            return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Review Mode'})
        # Phase 3: Evaluation Phase
        if _in_review and _in_convo:
            return evaluate_agent_after_creation_in_review(file_id, prompt, prompt_id, request_id, user_id)

    if prompt_id and os.path.exists(os.path.join(PROMPTS_DIR, f'{prompt_id}.json')):
        _ak = f'{user_id}_{prompt_id}'  # Ensure compound key available for reuse path
        with open(os.path.join(PROMPTS_DIR, f'{prompt_id}.json'), "r") as file:
            created_json = json.load(file)


        if chat_agent is None:
            return jsonify({'response': 'Agent reuse module is unavailable. Please check server dependencies.',
                            'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0,
                            'history_request_id': []})

        response = chat_agent(user_id,prompt,prompt_id,file_id,request_id)

        # --- Step 17: Check if the reuse agent intelligently decided to create a new agent ---
        # Two detection mechanisms:
        # 1. Autogen create_new_agent tool (intelligent — LLM decides via tool call)
        # 2. Response text pattern matching (fallback for structured agent output)
        user_prompt = f'{user_id}_{prompt_id}'
        if not review_agents.get(_ak) and not create_agent:
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
                    _ak_new = f'{user_id}_{new_prompt_id}'
                    review_agents[_ak_new] = True
                    _touch_agent_timestamp(_ak_new)
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
        _resonance = _tune_resonance_after_chat(user_id, prompt, response)
        return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Reuse Mode', **_resonance})

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

        except Exception as e:
            app.logger.error(f'failed to get prompt from id:- {prompt_id}: {e}')
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

    # --- Check if LLM's Agentic_Router tool fired during get_ans() ---
    if thread_local_data.get_agentic_requested():
        task_desc = thread_local_data.get_agentic_task_description()
        plan_steps = thread_local_data.get_agentic_plan_steps()
        matched_agent = thread_local_data.get_agentic_matched_agent_id()
        thread_local_data.clear_agentic_flags()
        app.logger.info(f'Agentic_Router tool fired: task="{task_desc[:80] if task_desc else ""}", '
                        f'steps={len(plan_steps)}, matched_agent={matched_agent}')

        return jsonify({
            'response': ans,
            'intent': ['FINAL_ANSWER'],
            'Agent_status': 'Plan Mode',
            'agentic_plan': {
                'task_description': task_desc,
                'steps': plan_steps,
                'matched_agent_id': matched_agent,
                'requires_consent': True,
            },
            'prompt_id': prompt_id if prompt_id else _next_prompt_id(),
            'req_token_count': thread_local_data.get_req_token_count(),
            'res_token_count': thread_local_data.get_res_token_count(),
            'history_request_id': thread_local_data.get_reqid_list(),
        })

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
            _ak_new = f'{user_id}_{new_prompt_id}'
            review_agents[_ak_new] = True
            _touch_agent_timestamp(_ak_new)
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
                if isinstance(new_res, list):
                    for item in reversed(new_res):
                        if isinstance(item, dict) and 'status' in item:
                            new_res = item
                            break
                    else:
                        new_res = {}
                if isinstance(new_res, dict) and new_res.get('status') == 'pending':
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
    _resonance = _tune_resonance_after_chat(user_id, prompt, ans) if ans else {}
    if ans != "":
        post_dict = {'user_id': user_id, 'status': 'FINISHED', 'task_name': "CHAT",
                     'uid': request_id, 'task_id': f"CHAT_{str(request_id)}", 'request_id': request_id,
                     **_resonance}
        publish_async('com.hertzai.longrunning.log', post_dict)
    else:
        post_dict = {'user_id': user_id, 'status': 'ERROR', 'task_name': "CHAT", 'uid': request_id,
                     'task_id': f"CHAT_{str(request_id)}", 'request_id': request_id, 'failure_reason': 'Got null response from GPT'}
        publish_async('com.hertzai.longrunning.log', post_dict)

    end_time = time.time()
    elapsed_time = end_time - start_time
    app.logger.info(f"time taken for this full call is {elapsed_time}")

    return jsonify({'response': ans, 'intent': thread_local_data.get_recognize_intents(), 'req_token_count': thread_local_data.get_req_token_count(), 'res_token_count': thread_local_data.get_res_token_count(), 'history_request_id': thread_local_data.get_reqid_list(), **_resonance})


def evaluate_agent_after_creation_in_review(file_id, prompt, prompt_id, request_id, user_id):
    if chat_agent is None:
        return jsonify({'response': 'Agent reuse module is unavailable. Please check server dependencies.',
                        'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0,
                        'history_request_id': [], 'Agent_status': 'Evaluation Mode'})
    response = chat_agent(user_id, prompt, prompt_id, file_id, request_id)
    _record_lifecycle('Evaluation Mode', user_id, prompt_id, 'Agent being evaluated after creation')
    _resonance = _tune_resonance_after_chat(user_id, prompt, response)
    return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0,
                    'history_request_id': [], 'Agent_status': 'Evaluation Mode', **_resonance})


def set_flags_to_enter_review_mode(no_of_flow, user_id, prompt_id=''):
    app.logger.info(f'{no_of_flow} Recipe Json exist Going to reuse')
    create_agent = False
    _ak = f'{user_id}_{prompt_id}'
    review_agents[_ak] = True
    conversation_agent[_ak] = False
    _touch_agent_timestamp(_ak)
    return create_agent


@app.route('/time_agent',methods=['POST'])
def time_agent():
    app.logger.info('GOT REQUEST IN TIME AGENT API')
    data = request.get_json()
    task_description = data.get('task_description',None)
    user_id = data.get('user_id',None)
    request_from = data.get('request_from',"Reuse")
    prompt_id = data.get('prompt_id',None)
    action_entry_point = data.get('action_entry_point',0)
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
    return jsonify({'status': 'acknowledged'}), 200

@app.route('/add_history', methods=['POST'])
def history():
    data = request.get_json()
    human_msg = data['human_msg']
    ai_msg = data['ai_msg']
    try:
        memory = get_memory(user_id=int(data['user_id']))
    except Exception:
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
    prompt_file = os.path.join(PROMPTS_DIR, f'{prompt_id}.json')
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
    if os.path.isdir(PROMPTS_DIR):
        for fname in os.listdir(PROMPTS_DIR):
            if fname.endswith('.json') and '_' not in fname:
                try:
                    fpath = os.path.join(PROMPTS_DIR, fname)
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
                                os.path.join(PROMPTS_DIR, f'{pid}_0_recipe.json')),
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
    if os.path.isdir(PROMPTS_DIR):
        for fname in os.listdir(PROMPTS_DIR):
            if fname.endswith('.json') and '_' not in fname:
                try:
                    fpath = os.path.join(PROMPTS_DIR, fname)
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
                            os.path.join(PROMPTS_DIR, f'{pid}_0_recipe.json')),
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
    federation (no central coordinator needed). A local collision check
    ensures two agents created on the same node within the same
    millisecond still get distinct IDs.

    Thread-safe via lock.

    NOTE: Returns 1–11 digit numeric IDs by folding the millisecond
    timestamp into a max-11-digit space.
    """
    with _prompt_id_lock:
        # Fold ms timestamp into 1..99,999,999,999 (1–11 digits)
        pid = int(time.time() * 1000) % 100_000_000_000
        if pid == 0:
            pid = 1

        if os.path.isdir(PROMPTS_DIR):
            # Preserve the same collision-avoidance logic
            while os.path.exists(os.path.join(PROMPTS_DIR, f'{pid}.json')):
                pid += 1
                # Keep pid within 1–11 digits while still allowing bumps
                if pid >= 100_000_000_000:
                    pid = 1

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
        local_path = os.path.join(PROMPTS_DIR, f'{pid}.json')
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
        'model': 'Qwen3.5-4B',
        'mode': 'flat',
        'cloud_fallback_configured': bool(cloud_url),
    }


@app.route('/status', methods=['GET'])
def status():
    result = {'response': 'Working...', 'status': 'running'}

    # Active LLM backend info
    result['llm_backend'] = _get_active_backend_info()
    result['node_tier'] = os.environ.get('HEVOLVE_NODE_TIER', 'flat')

    # HevolveAI health (non-blocking, fail-safe)
    try:
        from integrations.agent_engine.world_model_bridge import get_world_model_bridge
        bridge = get_world_model_bridge()
        bridge_stats = bridge.get_stats()
        result['hevolve_core_url'] = bridge_stats.get('api_url', '')
        result['in_process'] = bridge_stats.get('in_process', False)
        health = bridge.check_health()
        result['hevolve_core_healthy'] = health.get('healthy', False)
        result['learning_active'] = health.get('learning_active', False)
        result['learning_mode'] = health.get('mode', 'unknown')
    except Exception:
        result['hevolve_core_healthy'] = False
        result['learning_active'] = False

    return jsonify(result)


@app.route('/health', methods=['GET'])
def health_liveness():
    """Liveness probe — returns 200 if the process is running.

    Use for K8s livenessProbe / systemd WatchdogSec.
    """
    return jsonify({'status': 'alive'}), 200


@app.route('/ready', methods=['GET'])
def health_readiness():
    """Readiness probe — returns 200 only when critical subsystems are healthy.

    Checks:
    - Database: SELECT 1 via get_db()
    - Node identity: get_node_identity() returns non-None

    Returns 503 when any critical check fails.
    """
    checks = {}
    all_ok = True

    # Check 1: Database connectivity
    try:
        from integrations.social.models import get_db
        from sqlalchemy import text as _sa_text
        db = get_db()
        try:
            db.execute(_sa_text('SELECT 1'))
            checks['database'] = 'ok'
        finally:
            db.close()
    except Exception as e:
        checks['database'] = f'fail: {e}'
        all_ok = False

    # Check 2: Node identity
    try:
        from security.node_integrity import get_node_identity
        identity = get_node_identity()
        if identity:
            checks['node_identity'] = 'ok'
        else:
            checks['node_identity'] = 'fail: no identity'
            all_ok = False
    except Exception as e:
        checks['node_identity'] = f'fail: {e}'
        all_ok = False

    # Check 3: HevolveAI bridge (optional — not critical)
    try:
        from integrations.agent_engine.world_model_bridge import get_world_model_bridge
        bridge = get_world_model_bridge()
        bridge_health = bridge.check_health()
        checks['hevolve_core'] = 'ok' if bridge_health.get('healthy') else 'degraded'
    except Exception:
        checks['hevolve_core'] = 'unavailable'

    # Check 4: LLM backend (optional — not critical)
    try:
        backend_info = _get_active_backend_info()
        checks['llm_backend'] = backend_info.get('backend', 'unknown')
    except Exception:
        checks['llm_backend'] = 'unavailable'

    status_code = 200 if all_ok else 503
    return jsonify({
        'status': 'ready' if all_ok else 'not_ready',
        'checks': checks,
    }), status_code


# ============================================================================
# HART Challenge-Response Endpoint (Node Side)
# ============================================================================
# Central delivers a challenge nonce to this endpoint to prove the node is
# reachable at its claimed FQDN.  The node signs the nonce with its Ed25519
# private key and returns the signature.  This is step 2+3 of the 4-step
# domain verification handshake defined in security/key_delegation.py.
#
# Security notes:
#   - This endpoint is intentionally unauthenticated: central must be able to
#     reach it without prior credentials to prove domain reachability.
#   - The nonce is signed with the node's private key, which never leaves the
#     node.  Central verifies using the public key the node provided at
#     registration time.
#   - The endpoint only responds to GET (challenge delivery) and POST
#     (explicit challenge-response submission from the node itself if needed).

# Module-level storage for the most recent challenge nonce received from
# central.  The node-side orchestrator can poll this to retrieve the nonce
# and submit the signed response back to central.
_hart_challenge_state = {
    'lock': threading.Lock(),
    'pending_nonce': None,       # str | None — most recent nonce from central
    'pending_signature': None,   # str | None — signature once computed
    'pending_pubkey': None,      # str | None — this node's public key hex
    'received_at': None,         # datetime | None
}


@app.route('/.well-known/hart-challenge', methods=['GET'])
def hart_challenge_receive():
    """Receive a challenge nonce from central and auto-sign it.

    Central calls:  GET http://{fqdn}:6777/.well-known/hart-challenge?nonce=<hex>

    The node:
      1. Validates the nonce format (must be 64 hex chars = 32 bytes).
      2. Signs the raw nonce bytes with its Ed25519 private key.
      3. Returns {nonce, public_key_hex, signature_hex} so central can
         immediately verify without a second round-trip.

    Returns 200 with signed response on success, 400 on bad input.
    """
    nonce_hex = request.args.get('nonce', '').strip()

    if not nonce_hex:
        return jsonify({'error': 'Missing "nonce" query parameter'}), 400

    # Validate nonce format: must be valid hex and exactly 32 bytes (64 chars)
    try:
        nonce_bytes = bytes.fromhex(nonce_hex)
        if len(nonce_bytes) != 32:
            return jsonify({
                'error': f'Invalid nonce length: expected 32 bytes (64 hex chars), '
                         f'got {len(nonce_bytes)} bytes',
            }), 400
    except ValueError:
        return jsonify({'error': 'Invalid nonce: not valid hexadecimal'}), 400

    # Sign the raw nonce bytes with this node's Ed25519 private key
    try:
        from security.node_integrity import sign_message, get_public_key_hex
        signature = sign_message(nonce_bytes)
        public_key_hex = get_public_key_hex()
    except Exception as e:
        app.logger.error(f"hart-challenge: failed to sign nonce: {e}")
        return jsonify({'error': 'Internal error: failed to sign challenge'}), 500

    signature_hex = signature.hex()

    # Store in module state for potential polling by node-side orchestrator
    import datetime as _dt_mod
    with _hart_challenge_state['lock']:
        _hart_challenge_state['pending_nonce'] = nonce_hex
        _hart_challenge_state['pending_signature'] = signature_hex
        _hart_challenge_state['pending_pubkey'] = public_key_hex
        _hart_challenge_state['received_at'] = _dt_mod.datetime.now(_dt_mod.timezone.utc)

    app.logger.info(
        f"hart-challenge: signed nonce {nonce_hex[:16]}... "
        f"pubkey={public_key_hex[:16]}...")

    return jsonify({
        'nonce': nonce_hex,
        'public_key_hex': public_key_hex,
        'signature_hex': signature_hex,
    }), 200


@app.route('/.well-known/hart-challenge', methods=['POST'])
def hart_challenge_submit():
    """Accept an explicit challenge-response submission.

    An alternative path where the node operator or automation explicitly
    submits {nonce, fqdn} and this endpoint signs and returns the response.
    Useful when the node needs to relay the signed response to central
    through a different channel.

    Request JSON: {"nonce": "<hex>"}
    Response JSON: {"nonce": "...", "public_key_hex": "...", "signature_hex": "..."}
    """
    data = request.get_json(silent=True) or {}
    nonce_hex = data.get('nonce', '').strip()

    if not nonce_hex:
        return jsonify({'error': 'Missing "nonce" in request body'}), 400

    try:
        nonce_bytes = bytes.fromhex(nonce_hex)
        if len(nonce_bytes) != 32:
            return jsonify({
                'error': f'Invalid nonce length: expected 32 bytes, got {len(nonce_bytes)}',
            }), 400
    except ValueError:
        return jsonify({'error': 'Invalid nonce: not valid hexadecimal'}), 400

    try:
        from security.node_integrity import sign_message, get_public_key_hex
        signature = sign_message(nonce_bytes)
        public_key_hex = get_public_key_hex()
    except Exception as e:
        app.logger.error(f"hart-challenge POST: failed to sign nonce: {e}")
        return jsonify({'error': 'Internal error: failed to sign challenge'}), 500

    signature_hex = signature.hex()

    app.logger.info(
        f"hart-challenge POST: signed nonce {nonce_hex[:16]}... "
        f"pubkey={public_key_hex[:16]}...")

    return jsonify({
        'nonce': nonce_hex,
        'public_key_hex': public_key_hex,
        'signature_hex': signature_hex,
    }), 200


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

# ─── Shared error-handling decorator (DRY: replaces 12+ identical try/except blocks) ──

def _json_endpoint(f):
    """Wrap a Flask view so unhandled exceptions return ``{'error': ...}, 500``."""
    @wraps(f)
    def _wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    return _wrapped


# ─── Runtime Media Tools API ──────────────────────────────────────────
# Endpoints for managing runtime media tools (Wan2GP, TTS-Audio-Suite,
# Whisper, OmniParser). Tools are downloaded, started, and registered
# dynamically. See integrations/service_tools/runtime_manager.py.

@app.route('/api/tools/status', methods=['GET'])
@_json_endpoint
def tools_status():
    """Get status of all runtime media tools."""
    from integrations.service_tools.runtime_manager import runtime_tool_manager
    return jsonify(runtime_tool_manager.get_all_status())


@app.route('/api/tools/<tool_name>/setup', methods=['POST'])
@_json_endpoint
def tools_setup(tool_name):
    """Download + start + register a runtime tool."""
    from integrations.service_tools.runtime_manager import runtime_tool_manager
    result = runtime_tool_manager.setup_tool(tool_name)
    code = 500 if 'error' in result else 200
    return jsonify(result), code


@app.route('/api/tools/<tool_name>/start', methods=['POST'])
@_json_endpoint
def tools_start(tool_name):
    """Start an already-downloaded runtime tool."""
    from integrations.service_tools.runtime_manager import runtime_tool_manager
    result = runtime_tool_manager.start_tool(tool_name)
    code = 500 if 'error' in result else 200
    return jsonify(result), code


@app.route('/api/tools/<tool_name>/stop', methods=['POST'])
@_json_endpoint
def tools_stop(tool_name):
    """Stop a running runtime tool and free VRAM."""
    from integrations.service_tools.runtime_manager import runtime_tool_manager
    return jsonify(runtime_tool_manager.stop_tool(tool_name))


@app.route('/api/tools/<tool_name>/unload', methods=['POST'])
@_json_endpoint
def tools_unload(tool_name):
    """Stop + deregister a runtime tool."""
    from integrations.service_tools.runtime_manager import runtime_tool_manager
    return jsonify(runtime_tool_manager.unload_tool(tool_name))


@app.route('/api/tools/vram', methods=['GET'])
@_json_endpoint
def tools_vram():
    """Get VRAM usage dashboard."""
    from integrations.service_tools.vram_manager import vram_manager
    return jsonify(vram_manager.get_status())


# ─── Model Lifecycle API ─────────────────────────────────────────────
# Agentic model lifecycle management — dynamic load/unload/offload.

@app.route('/api/tools/lifecycle', methods=['GET'])
@_json_endpoint
def tools_lifecycle():
    """Model lifecycle dashboard: loaded models, priorities, VRAM pressure, hive hints."""
    from integrations.service_tools.model_lifecycle import get_model_lifecycle_manager
    mgr = get_model_lifecycle_manager()
    return jsonify(mgr.get_status())


@app.route('/api/tools/lifecycle/<model_name>/priority', methods=['POST'])
@_json_endpoint
def tools_lifecycle_priority(model_name):
    """Manually set model priority (admin override)."""
    from integrations.service_tools.model_lifecycle import get_model_lifecycle_manager
    data = request.get_json() or {}
    priority = data.get('priority', 'warm')
    mgr = get_model_lifecycle_manager()
    return jsonify(mgr.set_priority(model_name, priority))


@app.route('/api/tools/lifecycle/<model_name>/offload', methods=['POST'])
@_json_endpoint
def tools_lifecycle_offload(model_name):
    """Manually trigger GPU→CPU offload for a model."""
    from integrations.service_tools.model_lifecycle import get_model_lifecycle_manager
    mgr = get_model_lifecycle_manager()
    return jsonify(mgr.manual_offload(model_name))


@app.route('/api/system/pressure', methods=['GET'])
@_json_endpoint
def system_pressure():
    """Real-time system pressure dashboard: VRAM, RAM, CPU, disk, throttle factor."""
    from integrations.service_tools.model_lifecycle import get_model_lifecycle_manager
    mgr = get_model_lifecycle_manager()
    return jsonify(mgr.get_system_pressure())


@app.route('/api/revenue/dashboard', methods=['GET'])
@_json_endpoint
def revenue_dashboard():
    """Revenue pipeline dashboard: streams, trading P&L, compute borrowing."""
    from integrations.agent_engine.revenue_aggregator import get_revenue_aggregator
    from integrations.agent_engine.compute_borrowing import ComputeBorrowingService
    from integrations.social.models import get_db
    db = get_db()
    try:
        rev = get_revenue_aggregator()
        dashboard = rev.get_dashboard(db)
        dashboard['compute_borrowing'] = ComputeBorrowingService.get_status(db)
        return jsonify(dashboard)
    finally:
        db.close()


# ─── Coding Agent Aggregator API ──────────────────────────────────────
# Endpoints for the coding tool orchestrator (KiloCode, Claude Code, OpenCode).
# Tools execute as external CLI subprocesses — never re-dispatch to /chat.

@app.route('/coding/tools', methods=['GET'])
@_json_endpoint
def coding_tools():
    """List installed coding tools, capabilities, and benchmarks."""
    from integrations.coding_agent.orchestrator import get_coding_orchestrator
    return jsonify(get_coding_orchestrator().list_tools())


@app.route('/coding/execute', methods=['POST'])
@_json_endpoint
def coding_execute():
    """Execute a coding task via the best available tool.

    JSON body: {task, task_type?, preferred_tool?, model?, working_dir?}
    If 'encrypted' key present, decrypts E2E envelope first (hive offload).
    """
    data = request.get_json(force=True)

    # Handle encrypted envelope from hive peer
    encrypted = data.get('encrypted')
    if encrypted:
        try:
            from security.channel_encryption import (
                decrypt_json_from_peer, encrypt_json_for_peer,
                get_x25519_public_hex,
            )
            data = decrypt_json_from_peer(encrypted)
            if not data:
                return jsonify({'error': 'Decryption failed'}), 400
        except Exception as e:
            return jsonify({'error': f'Envelope decryption error: {e}'}), 400

    task = data.get('task', '')
    if not task:
        return jsonify({'error': 'task is required'}), 400

    from integrations.coding_agent.orchestrator import get_coding_orchestrator
    result = get_coding_orchestrator().execute(
        task=task,
        task_type=data.get('task_type', 'feature'),
        preferred_tool=data.get('preferred_tool', ''),
        user_id=data.get('user_id', ''),
        model=data.get('model', ''),
        working_dir=data.get('working_dir', ''),
    )

    # If request came from hive peer, encrypt the response back
    if encrypted:
        try:
            from security.channel_encryption import encrypt_json_for_peer
            peer_pub = data.get('_reply_x25519', '')
            if peer_pub:
                return jsonify({'encrypted': encrypt_json_for_peer(result, peer_pub)})
        except Exception:
            pass

    return jsonify(result)


@app.route('/coding/benchmarks', methods=['GET'])
@_json_endpoint
def coding_benchmarks():
    """Get coding tool benchmark dashboard data."""
    from integrations.coding_agent.orchestrator import get_coding_orchestrator
    return jsonify(get_coding_orchestrator().get_benchmarks())


@app.route('/coding/install', methods=['POST'])
@_json_endpoint
def coding_install():
    """Install a coding tool via npm. JSON body: {tool_name}"""
    data = request.get_json(force=True)
    tool_name = data.get('tool_name', '')
    if not tool_name:
        return jsonify({'error': 'tool_name is required'}), 400
    from integrations.coding_agent.installer import install
    result = install(tool_name)
    code = 200 if result.get('success') else 500
    return jsonify(result), code


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


@app.route('/api/voice/speak', methods=['POST'])
def voice_speak():
    """Synthesize text to speech via smart TTS router.

    Accepts JSON with:
      - text (required)
      - language (optional, auto-detected)
      - voice (optional, voice ref for cloning)
      - source (optional, context hint: chat_response/greeting/read_aloud/etc.)
      - engine (optional, bypass router with direct engine selection)
      - output_path (optional, auto-generated if omitted)
    """
    try:
        from integrations.channels.media.tts_router import get_tts_router

        data = request.get_json() or {}
        text = data.get('text', '')
        if not text:
            return jsonify({'error': 'text is required'}), 400

        router = get_tts_router()
        result = router.synthesize(
            text=text,
            language=data.get('language'),
            voice=data.get('voice'),
            output_path=data.get('output_path'),
            source=data.get('source'),
            engine_override=data.get('engine'),
        )
        resp = result.to_dict()
        # Add audio_url for frontends to fetch the WAV
        if result.path and not result.error:
            resp['audio_url'] = f"/api/voice/audio/{os.path.basename(result.path)}"
        code = 200 if not result.error else 500
        return jsonify(resp), code

    except ImportError as e:
        return jsonify({'error': f'TTS not available: {e}'}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/voice/voices', methods=['GET'])
def voice_list_voices():
    """List available TTS voices from all installed engines."""
    try:
        from integrations.channels.media.tts_router import get_tts_router
        voices = get_tts_router().get_all_voices()
        return jsonify({'voices': voices, 'count': len(voices)})
    except ImportError as e:
        return jsonify({'error': f'TTS not available: {e}'}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/voice/clone', methods=['POST'])
def voice_clone():
    """Clone a voice from an audio sample (5+ seconds recommended).

    Accepts engine param to route to specific cloning backend.
    Default: luxtts (CPU) or pocket_tts.
    """
    try:
        data = request.get_json() or {}
        audio_path = data.get('audio_path', '')
        name = data.get('name', '')
        if not audio_path or not name:
            return jsonify({'error': 'audio_path and name required'}), 400

        import json as _json
        engine = data.get('engine', 'luxtts')
        if engine == 'pocket_tts':
            from integrations.service_tools.pocket_tts_tool import pocket_tts_clone_voice
            result = pocket_tts_clone_voice(audio_path, name)
        else:
            from integrations.service_tools.luxtts_tool import luxtts_clone_voice
            result = luxtts_clone_voice(audio_path, name)

        parsed = _json.loads(result)
        code = 200 if 'error' not in parsed else 500
        return jsonify(parsed), code

    except ImportError as e:
        return jsonify({'error': f'TTS not available: {e}'}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/voice/engines', methods=['GET'])
def voice_engines():
    """Report status of all TTS engines (installed, can_run, device, etc.)."""
    try:
        from integrations.channels.media.tts_router import get_tts_router
        engines = get_tts_router().get_engine_status()
        return jsonify({'engines': engines})
    except ImportError as e:
        return jsonify({'error': f'TTS not available: {e}'}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/voice/audio/<filename>', methods=['GET'])
def voice_audio(filename):
    """Serve a generated TTS audio file. Path-traversal safe."""
    import os as _os
    safe_name = _os.path.basename(filename)
    if safe_name != filename or '..' in filename:
        return jsonify({'error': 'invalid filename'}), 400

    # Search in known TTS output directories
    search_dirs = [
        _os.path.expanduser('~/.hevolve/models/luxtts/output'),
        _os.path.expanduser('~/.hevolve/models/pocket_tts/output'),
        _os.path.expanduser('~/.hevolve/models/chatterbox/output'),
        _os.path.expanduser('~/.hevolve/models/cosyvoice/output'),
        _os.path.expanduser('~/.hevolve/models/indic_parler/output'),
        _os.path.expanduser('~/.hevolve/models/f5_tts/output'),
        _os.environ.get('TTS_TEMP_DIR', '/tmp/tts'),
    ]
    for d in search_dirs:
        fpath = _os.path.join(d, safe_name)
        if _os.path.isfile(fpath):
            from flask import send_file
            return send_file(fpath, mimetype='audio/wav')
    return jsonify({'error': 'file not found'}), 404


# ---------------------------------------------------------------------------
# Video Generation API — orchestrates GPU tasks via hive mesh
# ---------------------------------------------------------------------------

@app.route('/video-gen/', methods=['POST'])
def video_gen():
    """Video generation endpoint — drop-in replacement for MakeItTalk.

    Accepts the same request format as MakeItTalk's /video-gen/ endpoint.
    Dispatches GPU subtasks (TTS, face crop, lip-sync) to local GPU or
    hive mesh peers. Returns 202 with queue position + ETA.

    Chatbot pipeline can point here instead of MakeItTalk.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    if not data:
        return jsonify({'error': 'No JSON body'}), 400

    # Map user_id if present (chatbot sends 'uid' as request_id)
    if 'uid' not in data:
        data['uid'] = data.get('request_id', '')

    try:
        from integrations.agent_engine.video_orchestrator import get_video_orchestrator
        orch = get_video_orchestrator()
        result = orch.generate(data)

        if 'error' in result and 'status' not in result:
            return jsonify(result), 400

        # Return 202 Accepted (async processing) — same as MakeItTalk
        # Wrap in 'response' key for chatbot_pipeline compatibility
        return jsonify({'response': result}), 202

    except Exception as e:
        logger.error("Video generation error: %s", e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/video-gen/status/<job_id>', methods=['GET'])
def video_gen_status(job_id):
    """Check status of a video generation job."""
    try:
        from integrations.agent_engine.video_orchestrator import get_video_orchestrator
        orch = get_video_orchestrator()
        status = orch.get_job_status(job_id)
        if status:
            return jsonify(status)
        return jsonify({'error': 'Job not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# HART Skills API — ingest, list, and manage agent skills
# ---------------------------------------------------------------------------

@app.route('/api/skills/list', methods=['GET'])
def skills_list():
    """List all registered HART skills."""
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


# ─── Settings API — Compute Configuration ───────────────────────────


@app.route('/api/settings/compute', methods=['GET'])
@_json_endpoint
def settings_compute_get():
    """Return merged compute policy (env > DB > defaults).

    Access: any authenticated node operator.
    """
    import os as _os
    node_id = _os.environ.get('HEVOLVE_NODE_ID', 'local')
    from integrations.agent_engine.compute_config import get_compute_policy
    policy = get_compute_policy(node_id)

    # Add provider identity from PeerNode (single source of truth)
    provider_info = {}
    try:
        from integrations.social.models import db_session, PeerNode
        with db_session() as db:
            peer = db.query(PeerNode).filter_by(node_id=node_id).first()
            if peer:
                provider_info = {
                    'electricity_rate_kwh': peer.electricity_rate_kwh,
                    'cause_alignment': peer.cause_alignment,
                }
    except Exception:
        pass

    return jsonify({**policy, **provider_info, 'node_id': node_id})


@app.route('/api/settings/compute', methods=['PUT'])
@_json_endpoint
def settings_compute_put():
    """Update compute configuration. Single endpoint, writes to correct table per field.

    Policy fields → NodeComputeConfig (local-only).
    Provider identity → PeerNode (gossipped to network).
    Access: node operator or admin. Tier-aware: central nodes cannot set allow_metered_for_hive.
    """
    import os as _os
    data = request.get_json() or {}
    node_id = _os.environ.get('HEVOLVE_NODE_ID', 'local')
    node_tier = _os.environ.get('HEVOLVE_NODE_TIER', 'flat')

    # Tier guard: central nodes cannot opt into metered for hive
    if node_tier == 'central' and data.get('allow_metered_for_hive'):
        return jsonify({'error': 'Central nodes cannot enable metered APIs for hive'}), 403

    # Policy fields → NodeComputeConfig
    policy_fields = {
        'compute_policy', 'hive_compute_policy', 'max_hive_gpu_pct',
        'allow_metered_for_hive', 'metered_daily_limit_usd',
        'offered_gpu_hours_per_day', 'accept_thought_experiments',
        'accept_frontier_training', 'auto_settle', 'min_settlement_spark',
    }
    # Provider identity → PeerNode
    peer_fields = {'electricity_rate_kwh', 'cause_alignment'}

    policy_updates = {k: v for k, v in data.items() if k in policy_fields}
    peer_updates = {k: v for k, v in data.items() if k in peer_fields}

    from integrations.social.models import db_session, NodeComputeConfig, PeerNode

    with db_session() as db:
        if policy_updates:
            config = db.query(NodeComputeConfig).filter_by(node_id=node_id).first()
            if not config:
                config = NodeComputeConfig(node_id=node_id)
                db.add(config)
            for key, val in policy_updates.items():
                setattr(config, key, val)

        if peer_updates:
            peer = db.query(PeerNode).filter_by(node_id=node_id).first()
            if peer:
                for key, val in peer_updates.items():
                    setattr(peer, key, val)

        db.commit()

    # Invalidate cache
    from integrations.agent_engine.compute_config import invalidate_cache
    invalidate_cache(node_id)

    return jsonify({'updated': True, 'node_id': node_id,
                    'policy_fields': list(policy_updates.keys()),
                    'peer_fields': list(peer_updates.keys())})


@app.route('/api/settings/compute/provider', methods=['GET'])
@_json_endpoint
def settings_compute_provider():
    """Provider dashboard: contribution score, GPU hours, inferences, energy,
    total Spark earned, pending settlements, cause alignment.

    Access: node operator. Deployment-mode aware (flat/regional/central).
    """
    import os as _os
    node_id = _os.environ.get('HEVOLVE_NODE_ID', 'local')
    node_tier = _os.environ.get('HEVOLVE_NODE_TIER', 'flat')

    from integrations.social.models import db_session, PeerNode, MeteredAPIUsage
    from integrations.social.hosting_reward_service import HostingRewardService
    from sqlalchemy import func as sa_func

    with db_session() as db:
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            return jsonify({'error': 'Node not registered', 'node_id': node_id}), 404

        # Contribution score
        score_result = HostingRewardService.compute_contribution_score(db, node_id)

        # Pending settlements
        pending_count = db.query(sa_func.count(MeteredAPIUsage.id)).filter(
            MeteredAPIUsage.node_id == node_id,
            MeteredAPIUsage.settlement_status == 'pending',
        ).scalar() or 0

        pending_usd = db.query(
            sa_func.coalesce(sa_func.sum(MeteredAPIUsage.actual_usd_cost), 0)
        ).filter(
            MeteredAPIUsage.node_id == node_id,
            MeteredAPIUsage.settlement_status == 'pending',
        ).scalar() or 0.0

        # Reward summary
        reward_summary = HostingRewardService.get_reward_summary(db, node_id)

        return jsonify({
            'node_id': node_id,
            'node_tier': node_tier,
            'contribution': score_result,
            'compute_stats': {
                'gpu_hours_served': peer.gpu_hours_served or 0,
                'total_inferences': peer.total_inferences or 0,
                'energy_kwh_contributed': peer.energy_kwh_contributed or 0,
                'metered_api_costs_absorbed': peer.metered_api_costs_absorbed or 0,
            },
            'provider_identity': {
                'cause_alignment': peer.cause_alignment,
                'electricity_rate_kwh': peer.electricity_rate_kwh,
            },
            'pending_settlements': {
                'count': pending_count,
                'total_usd': round(float(pending_usd), 4),
            },
            'reward_summary': reward_summary,
        })


@app.route('/api/settings/compute/provider/join', methods=['POST'])
@_json_endpoint
def settings_compute_provider_join():
    """Simple provider onboarding. Creates NodeComputeConfig + sets PeerNode identity.

    Body: {cause_alignment?, electricity_rate_kwh?, offered_gpu_hours_per_day?, compute_policy?}
    Access: any node. Creates config with sensible defaults.
    """
    import os as _os
    data = request.get_json() or {}
    node_id = _os.environ.get('HEVOLVE_NODE_ID', 'local')

    from integrations.social.models import db_session, NodeComputeConfig, PeerNode

    with db_session() as db:
        # Create or update NodeComputeConfig
        config = db.query(NodeComputeConfig).filter_by(node_id=node_id).first()
        if not config:
            config = NodeComputeConfig(node_id=node_id)
            db.add(config)

        for field in ('compute_policy', 'offered_gpu_hours_per_day',
                      'accept_thought_experiments', 'accept_frontier_training'):
            if field in data:
                setattr(config, field, data[field])

        # Set provider identity on PeerNode
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if peer:
            if 'cause_alignment' in data:
                peer.cause_alignment = data['cause_alignment']
            elif not peer.cause_alignment:
                peer.cause_alignment = 'democratize_compute'
            if 'electricity_rate_kwh' in data:
                peer.electricity_rate_kwh = data['electricity_rate_kwh']

        db.commit()

    # Invalidate cache
    from integrations.agent_engine.compute_config import invalidate_cache
    invalidate_cache(node_id)

    # Return the merged config
    from integrations.agent_engine.compute_config import get_compute_policy
    policy = get_compute_policy(node_id)

    return jsonify({
        'joined': True,
        'node_id': node_id,
        'config': policy,
        'message': 'Welcome to the HART OS compute network. '
                   'Your contribution helps democratize access to AI.',
    })


# ─── Remote Desktop API ──────────────────────────────────────────
# RustDesk + Sunshine/Moonlight bridge, session management,
# engine selection, device identity.


@app.route('/api/remote-desktop/status', methods=['GET'])
@_json_endpoint
def remote_desktop_api_status():
    """Device ID, engine status, active sessions."""
    from integrations.remote_desktop.engine_selector import get_all_status
    from integrations.remote_desktop.device_id import get_device_id, format_device_id
    from integrations.remote_desktop.session_manager import get_session_manager

    device_id = get_device_id()
    sm = get_session_manager()
    sessions = sm.get_active_sessions()

    status = get_all_status()
    status['device_id'] = device_id
    status['formatted_id'] = format_device_id(device_id)
    status['active_sessions'] = [
        {'session_id': s.session_id, 'host_device_id': s.host_device_id,
         'mode': s.mode.value, 'state': s.state.value,
         'viewers': s.viewer_device_ids}
        for s in sessions
    ]
    return jsonify(status)


@app.route('/api/remote-desktop/host', methods=['POST'])
@_json_endpoint
def remote_desktop_api_host():
    """Start hosting. Body: {mode?, engine?}. Returns device_id + password."""
    data = request.get_json() or {}
    from integrations.remote_desktop.session_manager import (
        get_session_manager, SessionMode,
    )
    from integrations.remote_desktop.device_id import get_device_id, format_device_id

    device_id = get_device_id()
    sm = get_session_manager()
    mode_str = data.get('mode', 'full_control')
    mode = SessionMode(mode_str) if mode_str in [m.value for m in SessionMode] else SessionMode.FULL_CONTROL
    password = sm.generate_otp(device_id)
    engine_pref = data.get('engine', 'auto')

    result = {
        'device_id': device_id,
        'formatted_id': format_device_id(device_id),
        'password': password,
        'mode': mode.value,
        'engine': engine_pref,
    }

    # Start RustDesk
    if engine_pref in ('auto', 'rustdesk'):
        try:
            from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
            bridge = get_rustdesk_bridge()
            if bridge.available:
                bridge.set_password(password)
                bridge.start_service()
                rd_id = bridge.get_id()
                if rd_id:
                    result['rustdesk_id'] = rd_id
                result['engine'] = 'rustdesk'
        except Exception:
            pass

    # Start Sunshine
    if engine_pref in ('auto', 'sunshine'):
        try:
            from integrations.remote_desktop.sunshine_bridge import get_sunshine_bridge
            bridge = get_sunshine_bridge()
            if bridge.available:
                bridge.start_service()
                result['sunshine_running'] = bridge.is_running()
                if engine_pref == 'sunshine':
                    result['engine'] = 'sunshine'
        except Exception:
            pass

    return jsonify(result)


@app.route('/api/remote-desktop/connect', methods=['POST'])
@_json_endpoint
def remote_desktop_api_connect():
    """Connect to remote device. Body: {device_id, password, mode?, engine?}."""
    data = request.get_json() or {}
    device_id = data.get('device_id')
    password = data.get('password')
    if not device_id or not password:
        return jsonify({'error': 'device_id and password required'}), 400

    engine = data.get('engine', 'auto')
    file_transfer = data.get('mode') == 'file_transfer'

    # Try RustDesk
    if engine in ('auto', 'rustdesk'):
        try:
            from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
            bridge = get_rustdesk_bridge()
            if bridge.available:
                ok, msg = bridge.connect(device_id, password=password,
                                         file_transfer=file_transfer)
                if ok:
                    return jsonify({'success': True, 'engine': 'rustdesk',
                                    'device_id': device_id, 'message': msg})
        except Exception:
            pass

    # Try Moonlight
    if engine in ('auto', 'moonlight'):
        try:
            from integrations.remote_desktop.sunshine_bridge import get_moonlight_bridge
            bridge = get_moonlight_bridge()
            if bridge.available:
                ok, msg = bridge.stream(device_id)
                if ok:
                    return jsonify({'success': True, 'engine': 'moonlight',
                                    'device_id': device_id, 'message': msg})
        except Exception:
            pass

    return jsonify({'success': False, 'error': 'No engine available'}), 503


@app.route('/api/remote-desktop/sessions', methods=['GET'])
@_json_endpoint
def remote_desktop_api_sessions():
    """List active remote desktop sessions."""
    from integrations.remote_desktop.session_manager import get_session_manager
    sm = get_session_manager()
    sessions = sm.get_active_sessions()
    return jsonify({
        'sessions': [
            {'session_id': s.session_id, 'host_device_id': s.host_device_id,
             'mode': s.mode.value, 'state': s.state.value,
             'viewers': s.viewer_device_ids}
            for s in sessions
        ]
    })


@app.route('/api/remote-desktop/disconnect/<session_id>', methods=['POST'])
@_json_endpoint
def remote_desktop_api_disconnect(session_id):
    """End a specific session."""
    from integrations.remote_desktop.session_manager import get_session_manager
    sm = get_session_manager()
    sm.disconnect_session(session_id)
    return jsonify({'disconnected': session_id})


@app.route('/api/remote-desktop/engines', methods=['GET'])
@_json_endpoint
def remote_desktop_api_engines():
    """List available engines with install commands."""
    from integrations.remote_desktop.engine_selector import (
        get_available_engines, get_all_status, Engine,
    )
    status = get_all_status()
    available = get_available_engines()
    return jsonify({
        'available': [e.value for e in available],
        'engines': status['engines'],
        'install_recommendations': status.get('install_recommendations', []),
    })


@app.route('/api/remote-desktop/select-engine', methods=['POST'])
@_json_endpoint
def remote_desktop_api_select_engine():
    """Auto-select best engine. Body: {use_case?, role?, prefer?}."""
    data = request.get_json() or {}
    from integrations.remote_desktop.engine_selector import (
        select_engine, UseCase, Engine,
    )

    use_case_str = data.get('use_case', 'general')
    role = data.get('role', 'viewer')
    prefer_str = data.get('prefer')

    uc = UseCase(use_case_str) if use_case_str in [u.value for u in UseCase] else UseCase.GENERAL
    prefer = Engine(prefer_str) if prefer_str and prefer_str in [e.value for e in Engine] else None

    engine = select_engine(uc, role=role, prefer=prefer)
    return jsonify({'engine': engine.value, 'use_case': uc.value, 'role': role})


def _init_skills():
    """Initialize skill registry — load persisted skills + discover local."""
    try:
        from integrations.skills import skill_registry
        skill_registry.load_config()
        skill_registry.discover_local()
        if skill_registry.count > 0:
            app.logger.info(f"HART skills ready: {skill_registry.count} skills loaded")
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
        klogger.warning(f"Runtime tool init failed: {e}")


def _validate_startup():
    """Validate critical configuration at startup with helpful messages."""
    import sys
    warnings = []

    # Python version check
    v = sys.version_info
    if v.major != 3 or v.minor < 10 or v.minor > 11:
        warnings.append(
            f"Python {v.major}.{v.minor}.{v.micro} detected. "
            f"HART OS requires Python 3.10 or 3.11 (pydantic 1.10.9 compat).")

    # Config file check
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    langchain_config_path = os.path.join(os.path.dirname(__file__), 'langchain_config.json')
    if not os.path.exists(config_path) and not os.path.exists(langchain_config_path):
        warnings.append(
            "No config.json or langchain_config.json found. "
            "Create config.json with API keys (OPENAI, GROQ, etc.) for full functionality.")

    # .env check
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if not os.path.exists(env_path):
        warnings.append(
            "No .env file found. Create .env with OPENAI_API_KEY, GROQ_API_KEY, "
            "LANGCHAIN_API_KEY for LLM access.")

    # Database directory check — prefer DB path or user Documents for bundled mode
    _db_p = os.environ.get('HEVOLVE_DB_PATH', '')
    if _db_p and _db_p != ':memory:' and os.path.isabs(_db_p):
        db_dir = os.path.join(os.path.dirname(_db_p), 'agent_data')
    elif os.environ.get('NUNBA_BUNDLED') or getattr(sys, 'frozen', False):
        try:
            from core.platform_paths import get_agent_data_dir as _get_ad_dir
            db_dir = _get_ad_dir()
        except ImportError:
            db_dir = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'agent_data')
    else:
        db_dir = os.path.join(os.path.dirname(__file__), 'agent_data')
    if not os.path.isdir(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except OSError as e:
            warnings.append(f"Cannot create agent_data directory: {e}")

    # ── Central instance hardening ──
    _central_logger = logging.getLogger('hevolve_social')
    node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
    if node_tier == 'central':
        # Fix 3: TLS check
        if not os.environ.get('TLS_CERT_PATH'):
            _central_logger.warning("CENTRAL HARDENING: No TLS_CERT_PATH set — production should use HTTPS")

        # Fix 5: Secret presence validation
        api_key = os.environ.get('OPENAI_API_KEY', '')
        if not api_key or api_key.startswith('sk-xxx') or api_key == 'your-key':
            _central_logger.critical("CENTRAL HARDENING: OPENAI_API_KEY missing or placeholder")

        # Fix 6: DB encryption check
        db_url = os.environ.get('HEVOLVE_DB_URL') or os.environ.get('DATABASE_URL', '')
        if not db_url or 'sqlite' in db_url.lower():
            _central_logger.warning("CENTRAL HARDENING: SQLite detected — production should use PostgreSQL or sqlcipher")

        # Fix 7: Block dev mode
        if os.environ.get('HEVOLVE_DEV_MODE', '').lower() == 'true':
            os.environ['HEVOLVE_DEV_MODE'] = 'false'
            _central_logger.critical("CENTRAL HARDENING: Dev mode FORCED OFF on central instance")

    if warnings:
        logger = logging.getLogger('hevolve_social')
        logger.warning("=" * 60)
        logger.warning("STARTUP VALIDATION WARNINGS")
        for w in warnings:
            logger.warning(f"  - {w}")
        logger.warning("=" * 60)

    return warnings


def main():
    """
    Main entry point for hevolve-server CLI command.
    Starts the Flask server using waitress.
    """
    # Boot integrity verification (deferred from import time)
    hevolve_verify_boot()

    _validate_startup()

    # Bootstrap EventBus (required before local subscribers)
    try:
        from core.platform.bootstrap import bootstrap_platform
        bootstrap_platform()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Platform bootstrap failed: {e}")

    # Bootstrap local Crossbar subscribers (replaces cloud chatbot_pipeline)
    try:
        from core.peer_link.local_subscribers import bootstrap_local_subscribers
        bootstrap_local_subscribers()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Local subscribers bootstrap failed: {e}")

    # Start runtime tools restoration in background
    import threading
    tools_thread = threading.Thread(target=_init_runtime_tools, daemon=True)
    tools_thread.start()

    # Initialize HART skill registry (load persisted + discover local)
    skills_thread = threading.Thread(target=_init_skills, daemon=True)
    skills_thread.start()

    from core.port_registry import get_port
    serve(app, host='0.0.0.0', port=get_port('backend'), threads=50)


if __name__ == '__main__':
    main()
    # app.debug = True
    # flask_thread = threading.Thread(target=lambda: serve(app, host='0.0.0.0', port=6777))
    # flask_thread.daemon = True
    # flask_thread.start()
    # from crossbar_server import component
    # # Run the WAMP client
    # run([component])

