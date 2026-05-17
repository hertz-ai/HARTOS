# Fix Windows encoding for non-ASCII characters (Telugu, emojis, etc.)
import sys
import io
if sys.platform == 'win32' and 'pytest' not in sys.modules:
    # Force UTF-8 encoding for stdout/stderr to prevent crashes with non-ASCII characters
    # Skip when running under pytest — pytest wraps stdout/stderr for capture,
    # and replacing them here closes pytest's file handles.
    # Skip in cx_Freeze windowed builds — sys.stdout.buffer is closed (no console),
    # and TextIOWrapper on a closed buffer raises ValueError at import time.
    if sys.stdout is not None and hasattr(sys.stdout, 'buffer'):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        except (ValueError, OSError):
            pass
    if sys.stderr is not None and hasattr(sys.stderr, 'buffer'):
        try:
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
        except (ValueError, OSError):
            pass

# ── Defang importlib.metadata.packages_distributions() before transformers ──
# transformers/utils/import_utils.py:45 calls
#     PACKAGE_DISTRIBUTION_MAPPING = importlib.metadata.packages_distributions()
# at module-import time, with NO guard.  The function walks every
# *.dist-info/METADATA in site-packages and accesses
# `dist.metadata['Name']`.  ANY corrupt dist-info (METADATA missing,
# Name: header malformed, or pip rewriting it concurrently) raises
# KeyError('Name') → transformers crashes its import → langchain →
# hart_intelligence_entry crashes → Tier-1 HARTOS init dies → agent
# daemon never starts → admin Agent Dashboard stays empty for the rest
# of the process lifetime (Tier-1 init is one-shot).
#
# Regression observed 2026-04-26 23:05:23 in dev .venv (transient
# corrupt dist-info).  The fix monkey-patches the stdlib function with
# a try/except wrapper so a single bad dist-info no longer cascades to
# a full HARTOS Tier-1 outage.  Running BEFORE any langchain /
# transformers import in this module guarantees transformers picks up
# the wrapped version.
import importlib.metadata as _md_safe
_orig_pd = getattr(_md_safe, 'packages_distributions', None)
if _orig_pd is not None and not getattr(_orig_pd, '_hartos_guarded', False):
    def _safe_packages_distributions():
        try:
            return _orig_pd()
        except Exception:
            # Best-effort fallback: empty mapping.  Auto-docstring
            # lookups against {} return generic guesses (worse but
            # nonfatal).
            return {}
    _safe_packages_distributions._hartos_guarded = True
    _md_safe.packages_distributions = _safe_packages_distributions
del _md_safe

# ── Defang transformers `_LazyModule.__getattr__` recursion ──
# transformers ships a `_LazyModule` class whose `__getattr__` does
# ``hasattr(self, candidate_name)`` which itself triggers
# ``__getattr__`` again on the not-yet-bound attribute → ~1500-frame
# recursion that holds the `transformers` per-package import lock for
# minutes on cold disk.  Triggered by ANY downstream
# ``from transformers import GPT2TokenizerFast`` (langchain_core does
# this transitively).  Live thread dump 2026-04-28 22:18 captured the
# hartos-init thread in 28+ frames of this recursion, blocking every
# concurrent caller of ``get_world_model_bridge`` (15 dashboard
# requests + peer_discovery + telemetry) for the rest of the process
# lifetime.
#
# Fix: import `GPT2TokenizerFast` from its underlying submodule and
# write it to ``transformers.__dict__`` BEFORE any lazy lookup fires.
# Once the attribute is bound, `hasattr()` short-circuits at the dict
# and never calls `__getattr__`.  Every downstream caller gets the
# bound class via plain dict lookup, no recursion.
#
# This used to live in Nunba's `app.py::_prewarm_hartos_chain` (commit
# c0894a2a, 2026-04-28).  That prewarm was deleted in favour of the
# `hartos_bootstrap` facade — but the direct-bind got deleted with it,
# letting the recursion regress.  The right home for this is HARTOS,
# not Nunba: it must run before HARTOS's own
# `from langchain_classic.llms import OpenAI` (next stmt below) AND
# before any consumer's deferred bootstrap touches transformers.
try:
    import transformers as _tf_safe
    from transformers.models.gpt2.tokenization_gpt2_fast import (
        GPT2TokenizerFast as _GPT2TokenizerFast_direct,
    )
    if not hasattr(_tf_safe, '__dict__') or \
            'GPT2TokenizerFast' not in _tf_safe.__dict__:
        _tf_safe.__dict__['GPT2TokenizerFast'] = _GPT2TokenizerFast_direct
    del _tf_safe, _GPT2TokenizerFast_direct
except Exception:
    # If transformers isn't installed or moved the symbol, fall through.
    # Worker threads will hit the lazy path and pay the recursion once;
    # bad but not fatal (and surfaced via the hartos_init_error.log).
    pass

# ── Defang transformers `_LazyModule.__getattr__` re-entry recursion ──
#
# Symptom: Tier-1 init thread spends minutes (and burns the GIL the
# whole time, starving Hypercorn workers) inside
# ``transformers.utils.import_utils.py:2215`` —
#
#     transformers_module = sys.modules.get("transformers")
#     if transformers_module and hasattr(transformers_module, candidate_name):
#                                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#
# That ``hasattr(transformers_module, candidate_name)`` triggers
# ``transformers.__getattr__(candidate_name)`` (because transformers'
# top-level module object IS a ``_LazyModule`` with a custom
# ``__getattr__``), which hits the SAME ``hasattr`` line on a different
# candidate, which recurses again.  130+ frames deep, all
# ``active+gil``, no other thread can run.  The recursion is
# bounded (SLOW_TO_FAST_CONVERTERS is finite) so ``RecursionError``
# never fires — it just burns CPU forever on a graph walk that should
# be a single dict lookup.
#
# Why the GPT2TokenizerFast bind above isn't enough: that fix only
# covers ONE leaf of the lazy graph.  Whichever ``*TokenizerFast``
# is requested first triggers the same recursion via different
# candidate paths.
#
# Architectural note (impact-assessed): we considered three other
# approaches and rejected each — see
# ``memory/feedback_langchain_uplift_path.md``:
#   1. ``sys.setrecursionlimit(N)`` — recursion is bounded, never
#      raises; forcing N too low breaks legitimate deep stacks.
#   2. Lazy-import langchain in HARTOS — multi-week refactor; AND
#      HevolveAI (encrypted .so.enc, NOT refactor-able) is a second
#      transformers consumer that would still trigger this recursion
#      at runtime, so the layer-A fix doesn't cover all consumers.
#   3. Uplift langchain-classic → langchain-core + LangGraph — same
#      multi-week scope as (2), still doesn't cover HevolveAI.
#
# (1)–(3) are all consumer-side fixes.  This patch is at the
# ``transformers._LazyModule`` layer, which means BOTH consumers
# (open langchain + closed-source HevolveAI) benefit without changes
# to either.  That's why a third-party monkey-patch is the
# architecturally correct answer here, not a band-aid.
#
# Mechanism: install a re-entry guard on ``_LazyModule.__getattr__``.
# When a thread is already mid-``__getattr__`` for a (module, name)
# pair and the same pair is requested again (which is what
# ``hasattr`` does inside the same method), raise ``AttributeError``
# immediately.  ``hasattr`` swallows ``AttributeError`` and returns
# ``False`` — semantically identical to "the attribute hasn't been
# bound yet" which is exactly what the calling code is asking.
#
# Cross-name recursion (``__getattr__('A')`` triggering
# ``__getattr__('B')``) is preserved — only same-name re-entry on
# the same thread is short-circuited.  That's the precise shape of
# the bug.
#
# TODO(upstream): file an issue with HuggingFace transformers
# proposing the fix at the source — replace the offending
# ``hasattr(transformers_module, candidate_name)`` with
# ``candidate_name in transformers_module.__dict__``.  Once a
# released transformers carries the upstream fix, this monkey-patch
# becomes vestigial and can be removed.  Track via
# ``memory/feedback_transformers_lazy_module_patch.md``.
try:
    import threading as _tf_threading
    from transformers.utils import import_utils as _tf_import_utils

    _tf_lazy_module_class = getattr(_tf_import_utils, '_LazyModule', None)
    if _tf_lazy_module_class is not None and not getattr(
            _tf_lazy_module_class, '_hartos_reentry_guarded', False):
        _orig_getattr = _tf_lazy_module_class.__getattr__
        _resolving = _tf_threading.local()

        # Bind both the original method AND the threading.local() store
        # via default arguments so the closure carries its own
        # references.  Module-body cleanup (`del _orig_getattr` etc.)
        # below is then safe — without this, Python's closure-by-name
        # lookup would NameError when the lazy module later actually
        # invokes __getattr__ at runtime (symptom:
        # ``Tier-1 FAILED — name '_orig_getattr' is not defined``).
        def _hartos_reentry_guarded_getattr(
                self, name, _orig=_orig_getattr, _local=_resolving):
            in_progress = getattr(_local, 'set', None)
            if in_progress is None:
                in_progress = set()
                _local.set = in_progress
            key = (id(self), name)
            if key in in_progress:
                # Same (module, name) re-entry on this thread —
                # ``hasattr`` probe inside ``__getattr__``.  The
                # caller is asking "is this already bound?"; we are
                # mid-resolution so the answer is "not yet".  Raising
                # AttributeError tells hasattr→False, which is the
                # semantic the caller wanted.
                raise AttributeError(
                    f"module {self.__name__!r} has no attribute {name!r} "
                    f"(HARTOS re-entry guard: same-name __getattr__ "
                    f"recursion broken)"
                )
            in_progress.add(key)
            try:
                return _orig(self, name)
            finally:
                in_progress.discard(key)

        _tf_lazy_module_class.__getattr__ = _hartos_reentry_guarded_getattr
        _tf_lazy_module_class._hartos_reentry_guarded = True
    # NOTE: not deleting _orig_getattr / _resolving — the patched
    # method's default args already hold references; deleting the
    # module-scope names would leave them dangling at module-body cleanup
    # but otherwise harmless.  Leave them visible (underscore-prefixed,
    # excluded from `from … import *` semantics) to keep the call site
    # debuggable via dir().
    del _tf_threading, _tf_import_utils, _tf_lazy_module_class
except Exception:
    # transformers not installed, version moved _LazyModule, or some
    # other surprise — fall through.  We've still got the
    # GPT2TokenizerFast direct-bind above; recursion may resurface
    # but won't crash boot.
    pass

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
import subprocess
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
from core.auth_local import (
    require_local_or_token, require_local_or_token_csrf_safe,
)
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
# Crossbar HTTP publisher — canonical path is `crossbarhttp3.CrossbarHttpPublisher`
# (the same publisher used by integrations/social/realtime.py:25).  We fall
# back to the legacy `crossbarhttp.Client` API only if the canonical
# publisher is not installed.  Either path is OPTIONAL — if neither is
# importable the module-load must NOT crash; this layer is dead-code in
# flat mode where MessageBus uses LOCAL EventBus + PeerLink without HTTP.
#
# Regression 2026-04-26: a `.venv` with broken `crossbarhttp` (no
# __init__.py, Client at .crossbarhttp.Client) made the previous
# `crossbarhttp.Client(...)` module-level call raise AttributeError,
# crashing the whole HARTOS import (Tier-1 dead → agent daemon dead →
# admin dashboard empty).  Wrapping in try/except + using the canonical
# crossbarhttp3 first kills that failure mode.
_crossbar_publisher = None
try:
    from crossbarhttp3 import CrossbarHttpPublisher as _CrossbarPub
except Exception:
    _CrossbarPub = None
try:
    import crossbarhttp as _legacy_cb  # may be a broken stub
except Exception:
    _legacy_cb = None
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

# Boot-time prompts/ snapshot (best-effort, non-blocking).  Bounds
# data loss to "since last reboot" if the user wipes data dir or
# hits a corruption.  Fires at module-import time so it runs in
# BOTH the CLI hevolve-server path AND the Nunba bundled path
# (Nunba imports hart_intelligence_entry as a library; the CLI
# main() is never called in that case).  See core/prompts_backup.py.
try:
    from core.prompts_backup import snapshot_at_boot as _hartos_snapshot_at_boot
    _hartos_snapshot_at_boot()
except Exception as _snap_err:
    logging.getLogger('hevolve_core').debug(
        f'prompts_backup at module-load skipped: {_snap_err}')

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

handler = RotatingFileHandler(_langchain_log_path, maxBytes=5_000_000, backupCount=2, encoding='utf-8')
handler.setLevel(logging.INFO)

# Prevent UnicodeEncodeError on Windows (cp1252) when LLM output contains emoji.
# In cx_Freeze frozen builds, stdout/stderr may be closed — redirect to devnull.
try:
    if sys.stdout is None or sys.stdout.closed:
        sys.stdout = open(os.devnull, 'w')
    else:
        sys.stdout.reconfigure(errors='replace')
except Exception:
    sys.stdout = open(os.devnull, 'w')
try:
    if sys.stderr is None or sys.stderr.closed:
        sys.stderr = open(os.devnull, 'w')
except Exception:
    pass
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

# Boot-time tier safety check: warn if HEVOLVE_NODE_TIER is unset on non-bundled.
# A misconfigured central/regional node defaults to 'flat' and silently accepts
# body-sourced user_id without auth — the CISO flagged this as a data-exfil risk.
_boot_tier = os.environ.get('HEVOLVE_NODE_TIER')
_is_bundled = bool(os.environ.get('NUNBA_BUNDLED') or getattr(sys, 'frozen', False))
if not _boot_tier and not _is_bundled:
    app.logger.warning(
        'HEVOLVE_NODE_TIER not set — defaulting to "flat" (no auth on /chat). '
        'Set HEVOLVE_NODE_TIER=regional or =central for multi-user deployments.')
elif _boot_tier in ('regional', 'central'):
    # Verify JWT secret exists, otherwise auth enforcement is pointless
    _jwt_secret = os.environ.get('SECRET_KEY', '') or app.config.get('SECRET_KEY', '')
    if not _jwt_secret:
        app.logger.error(
            f'HEVOLVE_NODE_TIER={_boot_tier} but no SECRET_KEY configured! '
            f'JWT auth will reject ALL requests. Set SECRET_KEY env var.')

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

# Legacy ``register_consent_routes`` block removed in the
# consent-surface consolidation (orchestrator review acd11f55,
# 2026-04-25).  The HTTP write surface for user consent now lives at
# ``integrations.social.consent_api`` (consent_bp, JWT-authed,
# append-only, mounted at ``/api/social/consent``).  ``init_social``
# in ``integrations/social/__init__.py`` registers the new blueprint;
# no extra wiring is required here.

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

# Model Onboarding API — HuggingFace → GGUF → llama.cpp in one call
try:
    from integrations.service_tools.model_onboarding import get_blueprint as _get_onboard_bp
    app.register_blueprint(_get_onboard_bp())
    app.logger.info("Model onboarding API registered at /api/models/")
except ImportError:
    app.logger.info("Model onboarding not available, skipping")
except Exception as e:
    app.logger.warning(f"Model onboarding init skipped: {e}")

# Robot Intelligence API — 7 intelligences fuse in parallel for any embodied AI
try:
    from integrations.robotics.intelligence_api import robot_intelligence_bp
    app.register_blueprint(robot_intelligence_bp)
    app.logger.info("Robot Intelligence API registered at /api/robot/")
except ImportError:
    app.logger.info("Robot Intelligence API not available, skipping")
except Exception as e:
    app.logger.warning(f"Robot Intelligence API init skipped: {e}")

# Hive Session API — Claude Code as hive worker node
try:
    from integrations.coding_agent.claude_hive_session import get_blueprint as _get_hive_bp
    app.register_blueprint(_get_hive_bp())
    app.logger.info("Hive Session API registered at /api/hive/session/")
except ImportError:
    app.logger.info("Hive Session API not available, skipping")
except Exception as e:
    app.logger.warning(f"Hive Session API init skipped: {e}")

# Hive Signal Bridge API — channel signals dashboard
try:
    from integrations.channels.hive_signal_bridge import create_signal_blueprint
    app.register_blueprint(create_signal_blueprint())
    app.logger.info("Hive Signal Bridge registered at /api/hive/signals/")
except ImportError:
    app.logger.info("Hive Signal Bridge not available, skipping")
except Exception as e:
    app.logger.warning(f"Hive Signal Bridge init skipped: {e}")

# Hive Benchmark Prover API — distributed benchmark proving
try:
    from integrations.agent_engine.hive_benchmark_prover import create_benchmark_prover_blueprint
    _bench_bp = create_benchmark_prover_blueprint()
    if _bench_bp:
        app.register_blueprint(_bench_bp)
        app.logger.info("Benchmark Prover API registered at /api/hive/benchmark/")
except ImportError:
    app.logger.info("Benchmark Prover not available, skipping")
except Exception as e:
    app.logger.warning(f"Benchmark Prover init skipped: {e}")

# Robotics Hardware Bridge API — sensor/actuator registration and action execution
try:
    from integrations.robotics.hardware_bridge import _create_blueprint as _create_hw_bp
    _hw_bp = _create_hw_bp()
    if _hw_bp:
        app.register_blueprint(_hw_bp)
        app.logger.info("Robotics Hardware Bridge registered at /api/robot/")
except ImportError:
    app.logger.info("Robotics Hardware Bridge not available, skipping")
except Exception as e:
    app.logger.warning(f"Robotics Hardware Bridge init skipped: {e}")

# Compute Optimizer API — system resource optimization
try:
    from core.compute_optimizer import create_optimizer_blueprint, get_optimizer
    _opt_bp = create_optimizer_blueprint()
    if _opt_bp:
        app.register_blueprint(_opt_bp)
    _optimizer = get_optimizer()
    _optimizer.start()
    app.logger.info("Compute Optimizer registered and started")
except ImportError:
    app.logger.info("Compute Optimizer not available, skipping")
except Exception as e:
    app.logger.warning(f"Compute Optimizer init skipped: {e}")

# App Marketplace — publish, compare, distribute, auto-promote AI apps
try:
    from integrations.agent_engine.app_marketplace import marketplace_bp
    app.register_blueprint(marketplace_bp)
    app.logger.info("App Marketplace registered at /api/marketplace/")
except ImportError:
    app.logger.info("App Marketplace not available, skipping")
except Exception as e:
    app.logger.warning(f"App Marketplace init skipped: {e}")

# Resource Governor — start background monitoring (task #260: watchdog-registered)
try:
    from core.resource_governor import get_governor
    _gov = get_governor()
    _gov.start()
    app.logger.info(f"Resource Governor started (mode={_gov.get_mode()})")
    # Register both daemon loops with the watchdog.  Monitor runs every 5s;
    # proactive waits up to 5s per iteration.  Allow 4× grace for noisy
    # system-state probes (nvidia-smi, /proc).
    try:
        from security.node_watchdog import get_watchdog
        _wd = get_watchdog()
        if _wd and _gov._running:
            _wd.register('resource_governor_monitor',
                         expected_interval=20,
                         restart_fn=_gov.start, stop_fn=_gov.stop)
            _wd.register('resource_governor_proactive',
                         expected_interval=20,
                         restart_fn=_gov.start, stop_fn=_gov.stop)
            app.logger.info("Resource Governor registered with watchdog")
    except Exception as e:
        app.logger.debug(f"Governor watchdog registration skipped: {e}")
except ImportError:
    pass
except Exception as e:
    app.logger.warning(f"Resource Governor start skipped: {e}")

# Central Orchestrator Client — heartbeat to hevolve.ai central + master
# kill-switch polling.  Env-gated: no-op when HEVOLVE_CENTRAL_ORCHESTRATOR_URL
# is unset (default flat-mode install), so we burn ZERO resources on a
# poll loop nobody asked for.  When configured, the client posts small
# heartbeats (node_id + guardrail_hash + benchmark best-scores + halted
# flag), polls /halt for a master-signed stop signal, and routes that
# through HiveCircuitBreaker.halt_network() — which itself verifies the
# master-key signature before tripping.  See docs/ml_intern_brief §5-D.
try:
    from core import central_orchestrator_client as _coc_mod
    _coc = _coc_mod.get_client()
    if _coc.is_configured():
        if _coc.start():
            app.logger.info(
                "Central Orchestrator Client started "
                "(env-gated: HEVOLVE_CENTRAL_ORCHESTRATOR_URL set)"
            )
            # Watchdog — catch silent thread death.  Expected interval
            # is 2× heartbeat (heartbeat loop wakes at min(heartbeat,
            # halt_poll, 5s) so 120s gives comfortable headroom without
            # triggering on a single missed poll.
            try:
                from security.node_watchdog import get_watchdog
                _wd = get_watchdog()
                if _wd:
                    _wd.register(
                        'central_orchestrator_client',
                        expected_interval=120,
                        restart_fn=_coc.start,
                        stop_fn=_coc.stop,
                    )
                    app.logger.info(
                        "Central Orchestrator Client registered with watchdog"
                    )
            except ImportError:
                pass
            except Exception as e:
                app.logger.debug(
                    f"Central Orchestrator watchdog registration skipped: {e}"
                )
            # atexit — signal the loop to exit cleanly on process shutdown.
            # Daemon thread exits with the process regardless; this just
            # flushes any in-flight heartbeat.
            import atexit as _atexit
            _atexit.register(_coc.stop)
        else:
            # start() returned False — either already running, or node
            # tier is 'central' itself (brief's design: central doesn't
            # self-heartbeat).  Both paths log inside start().
            pass
    else:
        app.logger.debug(
            "Central Orchestrator Client inactive "
            "(HEVOLVE_CENTRAL_ORCHESTRATOR_URL unset) — flat-mode default"
        )
except ImportError:
    app.logger.debug(
        "Central Orchestrator Client not available, skipping"
    )
except Exception as e:
    app.logger.warning(f"Central Orchestrator Client init skipped: {e}")

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


def _resolve_llm_endpoint(registry_fn_name: str, env_var: str) -> str:
    """Resolve an LLM chat-completions URL via port_registry → env var fallback.

    Both GPT_API and DRAFT_GPT_API use the same 2-step resolution:
      1. core.port_registry.<registry_fn_name>()  →  base URL  →  + /chat/completions
      2. os.environ[env_var]                       →  base URL  →  + /chat/completions
    Returns '' if neither resolves.
    """
    url = ''
    try:
        import importlib
        _mod = importlib.import_module('core.port_registry')
        _base = getattr(_mod, registry_fn_name)()
        if _base:
            url = _base.rstrip('/') + '/chat/completions'
    except Exception:
        pass
    if not url:
        _env = os.environ.get(env_var, '')
        if _env:
            url = _env.rstrip('/') + '/chat/completions'
    return url


# Main LLM endpoint (4B on :8080). Config.json first, then registry/env fallback.
GPT_API = config.get('GPT_API', _ip.get('gpt3_url', ''))
if not GPT_API:
    GPT_API = _resolve_llm_endpoint('get_local_llm_url', 'HEVOLVE_LOCAL_LLM_URL')

# Draft LLM endpoint — the 0.8B on :8081 that serves casual_conv=True.
# When casual_conv=True, CustomGPT._call() posts here instead of GPT_API.
# The 0.8B responds in ~300ms with a short reply. If the response signals
# delegate=local (needs tools/reasoning), the caller re-dispatches with
# casual_conv=False which hits GPT_API (the 4B on :8080).
DRAFT_GPT_API = _resolve_llm_endpoint('get_local_draft_url', 'HEVOLVE_LOCAL_DRAFT_URL')
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
    get_central_db_url,
)
DB_URL = get_db_url()
# Distinct from DB_URL: always points at the central cloud (kong-routed)
# instead of collapsing to localhost in bundled mode.  Used by /prompts
# and /prompts/public to merge in agents the user owns on hevolve.ai
# but never synced down to this machine.  Empty string disables the
# cross-device merge (local-only listing) — matches the same fail-quiet
# contract _merge_prompts_with_cloud already has on cloud failures.
CENTRAL_DB_URL = get_central_db_url()
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
        # G2: Ensure HevolveArmor's import hook is installed before the first
        # `import hevolveai.*` statement runs below.  `try_import_hevolveai`
        # is idempotent and a no-op when the package is already loaded or
        # no armored bundle is present on disk.
        try:
            from security.native_hive_loader import try_import_hevolveai
            try_import_hevolveai('hevolveai')
        except ImportError:
            # security package missing — fall through to the raw import path
            pass

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
                frame_store=get_frame_store(),
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


# URL precedence: WAMP_URL env (set by Nunba desktop / regional / central) →
# legacy cloud default.  Flat mode (no cloud) sets WAMP_URL to localhost so
# DNS to aws_rasa.hertzai.com is never attempted (#323).
_wamp_url = os.environ.get('WAMP_URL') or 'http://localhost:8088/publish'
try:
    if _CrossbarPub is not None:
        client = _CrossbarPub(_wamp_url)
    elif _legacy_cb is not None and hasattr(_legacy_cb, 'Client'):
        client = _legacy_cb.Client(_wamp_url)
    else:
        # Legacy package may expose Client at .crossbarhttp.Client
        # (broken namespace install).  Probe before giving up.
        _nested = getattr(_legacy_cb, 'crossbarhttp', None) if _legacy_cb else None
        client = _nested.Client(_wamp_url) if (_nested and hasattr(_nested, 'Client')) else None
except Exception as _cb_err:
    client = None
    try:
        import logging as _logging_cb_warn
        _logging_cb_warn.getLogger(__name__).warning(
            f"Crossbar HTTP publisher init skipped: {type(_cb_err).__name__}: {_cb_err} — "
            f"MessageBus LOCAL+PEERLINK transports remain active.")
    except Exception:
        pass

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

    SIDE-EFFECT — Nunba monkey-patches this at runtime:
        Nunba's ``routes/hartos_backend_adapter.py`` wraps this
        function in-place to also call ``_capture_thinking(message)``
        for every published message.  That feeds Nunba's per-request
        thinking-trace buffer that the ``/chat`` HTTP response embeds.

        Implication for callers: bypassing this function (publishing
        directly via ``MessageBus.publish``) silently skips the
        capture.  If you change the chat-bubble path to call the bus
        directly, Nunba's HTTP /chat responses lose their thinking
        traces.  Either keep going through ``publish_async`` or
        migrate Nunba's interceptor to a bus subscriber on
        ``chat.response`` (tracked in
        ``memory/project_publish_aop_migration.md`` — the right
        long-term shape).

    Args:
        topic: Crossbar topic to publish to (legacy format).  Use
            ``core.peer_link.message_bus.chat_topic_for(user_id)``
            to build per-user chat topic strings instead of inline
            f-strings.
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


def _get_dynamic_capability_prompt() -> str:
    """Auto-generate capability context for the LLM based on live node state.

    - Bundled (Nunba): includes audio synthesis instructions + dynamic capabilities
    - Text-only (Hevolve web/channels): only includes non-audio capabilities
    - Empty string if nothing special is available (zero prompt bloat)

    New models/services appear automatically — no hardcoding per model.
    """
    parts = []

    # Audio mode context (Nunba only — verify TTS is actually available)
    if _is_bundled() if callable(_is_bundled) else _is_bundled:
        _tts_ok = False
        try:
            from tts.tts_engine import get_tts_engine
            _tts_ok = get_tts_engine() is not None
        except Exception:
            pass
        if _tts_ok:
            parts.append(
                'Your text output is auto-synthesized as audio. You can freely '
                'mix languages (the system detects scripts and routes each to '
                'the right TTS engine). For rich audio, embed tags: '
                '<music genre="jazz" duration="10">mood</music> for music, '
                '<sing duration="15">lyrics</sing> for singing voice.')

    # Dynamic capabilities from orchestrator (any mode)
    try:
        from integrations.service_tools.model_orchestrator import get_orchestrator
        cap_prompt = get_orchestrator().capability_prompt()
        if cap_prompt:
            parts.append(cap_prompt)
    except Exception:
        pass

    return '\n'.join(parts)


# create prompt
def create_prompt(tools):
    # No per-query classification here — the 30s cache + 1.5s hot-path
    # budget inside get_user_context are the only speed wins. Repeat
    # constructions within the TTL window never re-hit the backend.
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
        {_get_dynamic_capability_prompt()}
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
        from core.peer_link.message_bus import chat_topic_for
        publish_async(chat_topic_for(user_id), json.dumps(crossbar_message))
    except Exception:
        pass


def _request_consent(agent_id: str, action: str, label: str, input_text: str) -> str:
    """Request capability consent from user via approval card (Liquid UI → SSE fallback)."""
    user_id = thread_local_data.get_user_id()
    description = f'{label} access needed: {input_text}'
    # Primary: Liquid UI (reaches Android/web/desktop)
    try:
        from core.platform.service_registry import ServiceRegistry
        svc = ServiceRegistry.get('LiquidUIService')
        if svc:
            svc.agent_request_approval(agent_id=agent_id, action=action, description=description)
            return f"{label} access request sent to user. Waiting for approval."
    except Exception:
        pass
    # Fallback: SSE (Nunba desktop WebView2)
    from core.platform.events import broadcast_sse_safe
    if broadcast_sse_safe('agent.ui.update', {
        'type': 'approval', 'agent_id': agent_id,
        'action': action, 'description': description,
    }, user_id=str(user_id)):
        return f"{label} access request sent to user. Waiting for approval."
    return f"Could not request {label.lower()} access — notification system unavailable."


def _request_capability_consent(input_text: str) -> str:
    return _request_consent('vision', 'enable_camera', 'Camera', input_text)


def _request_screen_consent(input_text: str) -> str:
    return _request_consent('computer_use', 'enable_screen', 'Screen', input_text)



def _handle_screenshot_tool(input_text: str) -> str:
    """Take screenshot and describe using VLM."""
    try:
        from PIL import ImageGrab
        import base64, io
        screenshot = ImageGrab.grab()
        buf = io.BytesIO()
        from integrations.vlm.local_computer_tool import VLM_IMG_W, VLM_IMG_H
        screenshot.resize((VLM_IMG_W, VLM_IMG_H)).save(buf, 'JPEG', quality=50)
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')

        _llm_port = int(os.environ.get('HEVOLVE_LLM_PORT', 8080))
        resp = pooled_post(
            f'http://127.0.0.1:{_llm_port}/v1/chat/completions',
            json={
                'model': 'local',
                'messages': [{'role': 'user', 'content': [
                    {'type': 'text', 'text': f'Describe what you see on this screen. Question: {input_text}'},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}}
                ]}],
                'max_tokens': 300,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get('choices', [{}])[0].get('message', {}).get('content', 'Could not describe screen.')
        return "Screenshot taken but VLM unavailable for description."
    except Exception as e:
        return f"Screenshot error: {str(e)[:200]}"


def _handle_shell_command_tool(input_text: str) -> str:
    """Execute a shell / PowerShell / bash command on the user's computer
    and return its stdout+stderr. This is the FAST path for anything that
    can be done programmatically — `notepad file.txt` is one command vs
    the 18-iteration VLM loop Computer_Action would do for the same task.

    Safety:
      - Input is NFKC-normalized + lowercased before the denylist regex
        runs, so unicode lookalikes (full-width 'ｒｍ', cyrillic 'р') can't
        slip past patterns written in ASCII.
      - Hard-denylist of clearly destructive patterns (format, rm -rf /,
        shred, dd if=/dev/, mkfs, del /s C:\\ etc.). Denylisted commands
        return an explanation without running.
      - 30-second timeout. Longer-running work should use Execute_Coding_Task.
      - Output truncated to 4000 characters so a `tree /` style flood can't
        blow the LLM context budget.
      - Logged via _with_tool_logging on the Tool wrapper so every
        invocation is auditable.

    Input formats:
      - 'notepad' / 'calc.exe' / 'explorer C:\\Users' — plain command string
      - 'powershell: Get-Process | Select-Object -First 5' — explicit shell
      - 'bash: ls -la ~ | head' — explicit shell

    On Windows we use cmd.exe by default; 'powershell:' prefix switches to
    pwsh. On Unix we use /bin/sh; 'bash:' prefix switches to bash.
    """
    import re as _re_shell
    import unicodedata as _unicodedata
    if not input_text or not isinstance(input_text, str):
        return "Shell_Command: empty input. Pass a command string like 'notepad file.txt'."

    text = input_text.strip()

    # Explicit shell selector: 'powershell: <cmd>' / 'bash: <cmd>' / 'cmd: <cmd>'
    shell_override = None
    m = _re_shell.match(r'^(powershell|pwsh|bash|sh|cmd)\s*:\s*(.+)$', text, _re_shell.IGNORECASE)
    if m:
        shell_override = m.group(1).lower()
        text = m.group(2).strip()

    # --- Denylist of destructive patterns (case-insensitive, conservative)
    _DENY_PATTERNS = [
        r'\bformat\s+[a-z]:',                 # format C:
        r'\brm\s+-rf\s+/',                    # rm -rf / (anywhere in string)
        r'\brm\s+-rf\s+--no-preserve-root',
        r'\brm\s+-rf\s+~',                    # rm -rf ~
        r'\bdel\s+/s\s+/q\s+[a-z]:\\',        # del /s /q C:\
        r'\brmdir\s+/s\s+/q\s+[a-z]:\\',
        r'\bmkfs\.',                          # mkfs.*
        r'\bdd\s+if=/dev/(zero|random|urandom)\s+of=/dev/[sh]d',
        r'\bshred\b',
        r':\(\)\s*\{\s*:\|:&\s*\}\s*;:',      # fork bomb
        r'\bhalt\b', r'\bpoweroff\b',
        r'\breboot\b',                        # conservative — often benign but blocking
        r'\bshutdown\s+-[srh]',
        r'\breset-computermachinepassword',   # dangerous ps cmdlet (lowercased)
        r'\bformat-volume',
        r'\bremove-item\s+-recurse\s+-force\s+[a-z]:\\',
        # Chaining patterns — ethical hacker flagged that 'echo hi && rm -rf /'
        # bypassed the old $-anchored pattern. These catch destructive commands
        # anywhere in a chained expression (&&, ;, |, ||, backtick, $()).
        r'\bmv\s+/\s+/dev/null',              # mv / /dev/null
        r'\bchmod\s+-[rR]\s+000\s+/',         # chmod -R 000 /
        r'\bfind\s+/\s+-delete',              # find / -delete
        r'\bcurl\s+.*\|\s*bash',              # curl ... | bash (pipe to shell)
        r'\bwget\s+.*\|\s*bash',              # wget ... | bash
        # Interpreter wrappers — ethical hacker found python -c / perl -e / ruby -e
        # bypass all above patterns by wrapping destructive code inside an interpreter.
        r'\bpython[23]?\s+-c\s',              # python -c "os.system(...)"
        r'\bperl\s+-e\s',                     # perl -e "system(...)"
        r'\bruby\s+-e\s',                     # ruby -e "system(...)"
        r'\bnode\s+-e\s',                     # node -e "require('child_process')..."
        r'\bpowershell.*-enc',                # powershell -EncodedCommand (obfuscation)
    ]
    # NFKC maps full-width / compatibility chars to their ASCII equivalents
    # ('ｒｍ' → 'rm', '＞' → '>'), defeating the common homoglyph bypass.
    # We also strip zero-width characters (ZWJ, ZWNJ, BOM) that would
    # otherwise split matches like 'r\u200cm'. Strip control chars too.
    #
    # IMPORTANT: we normalize for the denylist CHECK only. The command
    # actually executed is still the raw user text — so a legitimate
    # filename with ligatures ('ﬁle.txt') or full-width chars still
    # resolves to the real file on disk. If the raw text happened to
    # contain a homoglyph version of a denylisted token, the denylist
    # sees it after normalization and blocks before execution.
    _normalized = _unicodedata.normalize('NFKC', text)
    _normalized = ''.join(
        ch for ch in _normalized
        if _unicodedata.category(ch) not in ('Cf', 'Cc') or ch in ('\t', '\n')
    )
    text_l = _normalized.lower()
    for pat in _DENY_PATTERNS:
        if _re_shell.search(pat, text_l):
            return (
                f"Shell_Command refused: the command matches a destructive "
                f"pattern ('{pat}') and is blocked by the built-in safety list. "
                f"If you really need this, ask the user to run it manually."
            )

    # --- Choose shell + argv
    if sys.platform == 'win32':
        if shell_override in ('powershell', 'pwsh'):
            argv = ['powershell', '-NoProfile', '-Command', text]
        elif shell_override in ('bash', 'sh'):
            argv = ['bash', '-c', text]
        else:
            argv = ['cmd', '/c', text]
    else:
        if shell_override in ('powershell', 'pwsh'):
            argv = ['pwsh', '-NoProfile', '-Command', text]
        elif shell_override == 'bash':
            argv = ['bash', '-c', text]
        else:
            argv = ['/bin/sh', '-c', text]

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=30,
            # Do NOT pass shell=True — argv already routes through the
            # chosen shell and shell=True would double-parse the string.
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return (
            "Shell_Command timed out after 30s. For long-running work use "
            "Execute_Coding_Task instead, which has a longer budget."
        )
    except FileNotFoundError as e:
        return f"Shell_Command: interpreter not found — {e}"
    except Exception as e:
        return f"Shell_Command error: {type(e).__name__}: {str(e)[:200]}"

    out = (proc.stdout or '').strip()
    err = (proc.stderr or '').strip()
    rc = proc.returncode

    # Truncate combined output to 4000 chars max
    combined = []
    if out:
        combined.append(out[:2000])
    if err:
        combined.append(f"[stderr]\n{err[:2000]}")
    body = '\n'.join(combined) if combined else '(no output)'

    header = f"Exit code: {rc}"
    if len(body) > 4000:
        body = body[:4000] + '\n...(truncated)'
    return f"{header}\n{body}"


def _handle_navigate_app_tool(input_text: str) -> str:
    """Resolve a spoken destination to a Nunba SPA route and attach a
    structured ui_action the frontend LiquidActionBar can render.

    The heavy lifting lives in integrations.ui_actions.navigate_tool so
    autogen and LangChain share exactly one code path. Thread-local
    ui_actions are read by the /chat handler on the way out."""
    try:
        from integrations.ui_actions import navigate_tool as _nt
        role = getattr(thread_local_data, 'get_user_role', lambda: 'flat')() or 'flat'
        return _nt.handle_navigate_app(
            input_text, user_role=role, thread_local=thread_local_data,
        )
    except ImportError:
        return "Navigate_App is not available on this node."
    except Exception as e:
        return f"Navigate_App error: {str(e)[:200]}"


def _handle_computer_action_tool(input_text: str) -> str:
    """Execute GUI action on user's computer via VLM agentic loop.

    Reuses the same pipeline as execute_windows_or_android_command (autogen):
    vlm_adapter → local_loop → point_and_act → pyautogui.
    """
    user_id = thread_local_data.get_user_id()
    prompt_id = thread_local_data.get_prompt_id()
    try:
        os.environ.setdefault('HEVOLVE_VLM_UNIFIED', 'true')
        from integrations.vlm.vlm_adapter import execute_vlm_instruction
        # 180s is the floor for cold-start runs: first VLM forward pass alone
        # can take 8-15s on the 4B model, and a realistic 12-iteration loop
        # needs ~2.5 minutes. HEVOLVE_COMPUTER_ACTION_ETA lets the user raise
        # it further without touching code.
        max_eta = int(os.environ.get('HEVOLVE_COMPUTER_ACTION_ETA', '180'))
        message = {
            'instruction_to_vlm_agent': input_text,
            'user_id': str(user_id or 'guest'),
            'prompt_id': str(prompt_id or 0),
            'os_to_control': 'windows' if sys.platform == 'win32' else (
                'macos' if sys.platform == 'darwin' else 'linux'),
            'max_ETA_in_seconds': max_eta,
        }
        result = execute_vlm_instruction(message)
        if not (result and isinstance(result, dict)):
            return "Computer action completed (no detailed response)."

        exit_reason = result.get('exit_reason', 'done')
        responses = result.get('extracted_responses', [])
        elapsed = result.get('execution_time_seconds', 0)
        n_actions = len(responses)

        if exit_reason == 'done':
            if responses:
                last_content = responses[-1].get('content', '')
                if isinstance(last_content, str):
                    return last_content[:500]
                return f"Done — {n_actions} actions in {elapsed:.0f}s."
            return f"Done in {elapsed:.0f}s."

        # Non-done paths: be honest to the router so it doesn't confabulate.
        # The router sees this string as the tool's final answer and should
        # turn it into a truthful user reply instead of pretending success.
        tail_action = None
        for resp in reversed(responses):
            if resp.get('type') == 'action':
                tail_action = resp.get('content', {})
                break

        reason_msg = {
            'timeout': f"I tried for {elapsed:.0f}s ({n_actions} actions) but ran out of time before finishing.",
            'max_iterations': f"I tried {n_actions} actions but the task didn't reach a clear completion.",
            'action_error': f"I hit errors on 3 consecutive actions after trying {n_actions} steps and stopped to avoid breaking things.",
            'grounding_failed': f"I couldn't reliably locate the UI element after {n_actions} attempts.",
        }.get(exit_reason, f"Loop exited with reason '{exit_reason}' after {n_actions} actions.")

        if tail_action:
            reason_msg += f" Last attempt: {tail_action.get('action', '?')} ({tail_action.get('reasoning', '')[:120]})."
        reason_msg += " Want me to try a different approach, or should I describe the steps so you can do it manually?"
        return reason_msg
    except ImportError:
        return "Computer use unavailable — VLM adapter not installed."
    except Exception as e:
        return f"Computer use error: {str(e)[:200]}"


def _handle_list_pending_actions_tool(input_text: str) -> str:
    """List the user's pending scheduled messages / reminders / jobs.

    Answers questions like "what's next for me?", "what reminders do I
    have today?", "show my upcoming tasks". Pulls from
    ScheduledMessageManager (now durable via JSON persistence) and
    apscheduler jobs if the scheduler service is running. The LLM gets
    a plain-text summary with ISO timestamps so it can re-format for
    the user in whatever style fits.

    Input: optional filter hint like 'today', 'this week', 'tomorrow',
    or '' for all pending. The filter is lexical — the tool lists
    every pending message and lets the LLM narrow if it wants.
    """
    try:
        hint = (input_text or '').strip().lower()
        lines: list[str] = []
        # ── ScheduledMessageManager (durable JSON) ───────────────────
        try:
            from integrations.channels.automation.scheduled_messages import (
                ScheduledMessageManager,
            )
            mgr = ScheduledMessageManager()
            pending = mgr.list_pending()
            if pending:
                lines.append(f"Scheduled messages ({len(pending)} pending):")
                for m in pending[:20]:
                    when = m.scheduled_time.isoformat() if m.scheduled_time else '?'
                    chan = m.channel_id or 'default'
                    snip = (m.content or '')[:120]
                    lines.append(f"  • {when}  [{chan}]  {snip}")
                if len(pending) > 20:
                    lines.append(f"  … and {len(pending) - 20} more")
            else:
                lines.append("No scheduled messages pending.")
        except ImportError:
            pass
        except Exception as e:
            lines.append(f"(scheduled messages unavailable: {str(e)[:80]})")

        # ── APScheduler jobs (if active) ─────────────────────────────
        try:
            from integrations.channels.scheduler import get_scheduler
            sched = get_scheduler()
            if sched is not None:
                jobs = list(sched.get_jobs())
                if jobs:
                    lines.append(f"\nBackground jobs ({len(jobs)} active):")
                    for j in jobs[:20]:
                        next_run = getattr(j, 'next_run_time', None)
                        when = next_run.isoformat() if next_run else 'no next run'
                        lines.append(f"  • {when}  {j.id}  {j.name or ''}")
        except ImportError:
            pass
        except Exception as e:
            lines.append(f"(scheduler jobs unavailable: {str(e)[:80]})")

        if not lines:
            return "Nothing scheduled. Your action queue is empty."
        if hint:
            lines.insert(0, f"Filter hint: {hint}")
        return "\n".join(lines)
    except Exception as e:
        return f"List_Pending_Actions error: {str(e)[:200]}"


def _handle_connect_channel_tool(input_text: str) -> str:
    """Register / connect a messaging channel (WhatsApp, Telegram, Discord,
    Slack, etc.) from natural-language chat. Delegates to the existing
    `register_channel` closure in integrations.channels.agent_tools so there's
    exactly ONE registration code path — this is just the LangChain facade.

    Input formats accepted:
      - "whatsapp"                      → start setup, returns missing-fields
      - "telegram {\"bot_token\":...}"  → register with config
      - '{"channel": "slack",
          "config": {"bot_token":...}}' → full JSON dict
    """
    import json as _json
    try:
        channel_type = None
        config_json = '{}'
        text = (input_text or '').strip()

        if text.startswith('{'):
            try:
                parsed = _json.loads(text)
                channel_type = (parsed.get('channel') or parsed.get('channel_type') or '').lower()
                cfg = parsed.get('config') or {}
                config_json = _json.dumps(cfg) if isinstance(cfg, dict) else str(cfg)
            except _json.JSONDecodeError:
                pass

        if not channel_type:
            parts = text.split(None, 1)
            channel_type = parts[0].lower() if parts else ''
            if len(parts) > 1:
                config_json = parts[1].strip() or '{}'

        if not channel_type:
            return (
                "Which channel should I connect? Say 'whatsapp', 'telegram', 'slack', "
                "'discord', etc. You can also include credentials like "
                "'telegram {\"bot_token\": \"123:ABC\"}'."
            )

        from integrations.channels.agent_tools import build_channel_tool_closures
        ctx = {
            'user_id': thread_local_data.get_user_id(),
            'prompt_id': thread_local_data.get_prompt_id(),
        }
        tools = build_channel_tool_closures(ctx) or []
        register_fn = next(
            (t[2] for t in tools
             if isinstance(t, tuple) and len(t) >= 3 and t[0] == 'register_channel'),
            None,
        )
        if register_fn is None:
            return "Channel registration is not available on this node."
        return register_fn(channel_type, config_json)
    except Exception as e:
        return f"Channel connect error: {str(e)[:200]}"


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
    """Load google-search tool, returning empty list if package is missing.

    In bundled/flat mode (NUNBA_BUNDLED=1 or HEVOLVE_NODE_TIER=flat),
    the Google API key is expected to be missing — log at DEBUG instead
    of WARNING so the log doesn't fill with noise every request. On
    cloud/regional tiers, a missing key IS a warning worth surfacing.
    """
    try:
        return load_tools(["google-search"])
    except (ImportError, Exception) as e:
        _bundled = os.environ.get('NUNBA_BUNDLED') or os.environ.get('HEVOLVE_NODE_TIER', 'flat') == 'flat'
        if _bundled:
            logging.debug(f"Google Search tool unavailable (expected in local mode): {e}")
        else:
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

            Tool(
                name="Shell_Command",
                func=_handle_shell_command_tool,
                description=(
                    "Run a single shell / PowerShell / bash command on the user's "
                    "computer. ALWAYS prefer this over Computer_Action when the task "
                    "is expressible as a command — it's deterministic, 100ms instead "
                    "of 60+ seconds of VLM grounding, and doesn't need to see the "
                    "screen. Use Shell_Command for: launching apps (notepad, calc, "
                    "chrome, explorer C:\\path), opening files (notepad file.txt), "
                    "listing directories (dir, ls), system info (systeminfo, "
                    "Get-Process), creating/deleting files, running scripts, git "
                    "operations, package installs, etc. Input formats: plain command "
                    "string ('notepad hello.txt'), 'powershell: Get-Process | Select "
                    "-First 5' to force PowerShell, 'bash: ls -la ~' to force bash. "
                    "Destructive patterns (format C:, rm -rf /, fork bomb, etc.) are "
                    "denied by a built-in safety list. 30-second timeout. Output "
                    "truncated to 4000 chars. For long-running coding work use "
                    "Execute_Coding_Task instead."
                ),
            ),
            Tool(
                name="Computer_Action",
                func=_handle_computer_action_tool,
                description=(
                    "Execute a GUI action on the user's computer: click buttons, "
                    "type text, scroll, interact with visible UI elements that can "
                    "ONLY be reached through the graphical interface. Uses a VLM to "
                    "identify what to click, so it's ~10-60 seconds per task. "
                    "IMPORTANT: before picking this tool, ask yourself whether "
                    "Shell_Command could do the same job — launching apps, opening "
                    "files, running scripts, changing settings, etc. are almost "
                    "always faster and more reliable via Shell_Command. Reserve "
                    "Computer_Action for tasks that genuinely require visual "
                    "grounding: clicking a specific button inside an already-running "
                    "app's UI, picking a visible option from a GUI menu, dragging "
                    "something, etc. Input: natural language instruction (e.g. "
                    "'click the Save button in the open document', 'select the "
                    "second item in the dropdown')."
                ),
            ),
            Tool(
                name="Computer_Screenshot",
                func=_handle_screenshot_tool,
                description=(
                    "Take a screenshot of the user's screen and describe what you see. "
                    "Use when you need to understand what's on screen before acting. "
                    "Input: question about the screen (e.g. 'what apps are open?')."
                ),
            ),
            Tool(
                name="Request_Camera_Access",
                func=_request_capability_consent,
                description=(
                    "Request user permission to access their camera for visual "
                    "understanding. Use when you need to see the user but camera is "
                    "not active. Input: reason for needing camera access."
                ),
            ),
            Tool(
                name="Request_Screen_Access",
                func=_request_screen_consent,
                description=(
                    "Request user permission to see their screen for computer use. "
                    "Use when you need to see the screen but screen sharing is not "
                    "active. Input: reason for needing screen access."
                ),
            ),
            Tool(
                name="Connect_Channel",
                func=_handle_connect_channel_tool,
                description=(
                    "Connect / register a messaging channel like WhatsApp, Telegram, "
                    "Discord, Slack, Email, SMS, Teams, etc. so the user can chat with "
                    "the agent outside Nunba. Use this tool whenever the user asks to "
                    "'connect <service>', 'add <service>', 'link my <service>', "
                    "'set up <service>', 'hook up <service>', or similar — especially "
                    "for any of the 31 supported channels. Input: the channel name "
                    "alone (e.g. 'whatsapp') to start setup and get the list of "
                    "required credentials, OR 'channel_name <json_config>' to register "
                    # The doubled braces {{...}} are REQUIRED — LangChain's ReAct
                    # prompt template passes every tool description through
                    # str.format(), which treats literal { and } as placeholder
                    # markers. A single `{"bot_token":"..."}` in this string
                    # raises ValueError: Missing some input keys: {'"bot_token"'}
                    # at prompt-construction time and crashes /chat before any
                    # LLM call. Doubling escapes them to literals. See
                    # langchain.log 2026-04-11 22:46:01 for the crash.
                    "with credentials (e.g. 'telegram {{\"bot_token\":\"123:ABC\"}}'). "
                    "For WhatsApp specifically, passing just 'whatsapp' starts a QR "
                    "authentication flow. Do NOT ask the user for credentials first — "
                    "call this tool with just the channel name and it will tell you "
                    "what's needed."
                ),
            ),
            Tool(
                name="Navigate_App",
                func=_handle_navigate_app_tool,
                description=(
                    "Open a different page of the Nunba app for the user. Use this "
                    "whenever the user says things like 'take me to the social feed', "
                    "'open model management', 'show me the channels page', 'go to "
                    "admin', 'show the marketplace', 'open games', etc. Input: the "
                    "destination in natural language (e.g. 'model management' or "
                    "'social feed'). The tool resolves it against the app's page "
                    "registry and makes the frontend open that route — you don't "
                    "need to give a URL, just say the page. After calling this tool, "
                    "tell the user you're opening that page in a single short sentence."
                ),
            ),
            Tool(
                name="List_Pending_Actions",
                func=_handle_list_pending_actions_tool,
                description=(
                    "List the user's upcoming scheduled messages, reminders, and "
                    "background jobs. Use when the user asks 'what's next', "
                    "'what do I have scheduled', 'any reminders for today', "
                    "'what's coming up', 'show my upcoming tasks', etc. "
                    "Input: optional filter hint ('today', 'this week', "
                    "'tomorrow') or empty string for all pending. Returns a "
                    "plain-text list with ISO timestamps — reformat for the "
                    "user in a natural style. Pulls from both the durable "
                    "ScheduledMessageManager (reminders, channel messages) "
                    "and the live apscheduler job store (periodic tasks)."
                ),
            ),
        ]

        # Service Tools: Add HTTP microservice tools (Crawl4AI, AceStep, etc.)
        try:
            from integrations.service_tools import service_tool_registry
            tool += service_tool_registry.get_langchain_tools()
        except ImportError:
            pass

        # System Introspection Tools: agent self-awareness — GPU tier,
        # active models, TTS backend, boot-decision rationale.  Lets the
        # LLM answer "what model is running?", "why is speculation off?",
        # "do I have a GPU?" from real live admin-API data instead of
        # hallucinating.
        try:
            from integrations.service_tools.system_introspect_tool import (
                get_langchain_tools as _get_introspect_tools,
            )
            tool += _get_introspect_tools()
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

        # Provider gateway tools (Cloud_LLM, Generate_Image, etc.)
        try:
            from integrations.providers.agent_tools import get_provider_tools
            tools += get_provider_tools()
        except Exception as _prov_err:
            app.logger.debug(f"Provider gateway tools not loaded: {_prov_err}")

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


# Language dict — canonical in core/constants.py
from core.constants import SUPPORTED_LANG_DICT  # noqa: E402


# ---------------------------------------------------------------------------
# G12: Parallel SSM student executor shared across CustomGPT instances.
# ---------------------------------------------------------------------------
# llama.cpp (teacher) is reached via pooled_post to GPT_API / DRAFT_GPT_API.
# HevolveAI's SSM student previously never ran on this path — only the
# in-process LearningLLMProvider.create_chat_completion() branch invoked it,
# which HARTOS' CustomGPT bypasses entirely.  We now kick off the SSM
# forward pass in parallel with the teacher HTTP call.  After the teacher
# reply arrives we opportunistically pair the two for distillation.
#
# Failure modes (all silent — never block the teacher path):
#   * Bridge unavailable          → student_future is None
#   * In-process mode off         → predict_student returns None
#   * SSM forward raises          → predict_student returns None
#   * SSM exceeds collect timeout → skip distillation, return teacher only
import concurrent.futures as _g12_cf

_g12_ssm_executor: Optional[_g12_cf.ThreadPoolExecutor] = None
_g12_ssm_executor_lock = threading.Lock()


def _g12_get_executor() -> Optional[_g12_cf.ThreadPoolExecutor]:
    global _g12_ssm_executor
    if _g12_ssm_executor is not None:
        return _g12_ssm_executor
    with _g12_ssm_executor_lock:
        if _g12_ssm_executor is None:
            if os.environ.get('HEVOLVE_PARALLEL_SSM', '1') == '0':
                return None
            try:
                _g12_ssm_executor = _g12_cf.ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix='g12_ssm_student')
                atexit.register(
                    lambda: _g12_ssm_executor.shutdown(wait=False)
                    if _g12_ssm_executor else None)
            except Exception:
                _g12_ssm_executor = None
    return _g12_ssm_executor


def _g12_kick_off_student(prompt: str):
    """Submit a student SSM forward pass; return a Future or None."""
    if not prompt or len(prompt) < 1:
        return None
    ex = _g12_get_executor()
    if ex is None:
        return None
    try:
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge,
        )
        bridge = get_world_model_bridge()
    except Exception:
        return None

    def _worker(_bridge=bridge, _prompt=prompt):
        try:
            return _bridge.predict_student(_prompt)
        except Exception:
            return None

    try:
        return ex.submit(_worker)
    except RuntimeError:
        # Executor shut down (e.g., during interpreter teardown).
        return None


def _g12_finalize(prompt: str, teacher_response: str, student_future) -> None:
    """Collect the student future (short-timeout) and enqueue distillation.

    Fire-and-forget — we never hold up the teacher return.  Callers MUST
    pass the teacher text they're about to return; distillation only fires
    when both responses are available AND they differ.
    """
    if student_future is None or not teacher_response:
        return
    try:
        # Budget: we're about to return to the user anyway.  If the student
        # isn't done within 500ms, drop it for this turn — distillation
        # can pick up the next request.
        student = student_future.result(timeout=0.5)
    except _g12_cf.TimeoutError:
        # Let it finish in the background; we just don't use it this turn.
        return
    except Exception:
        return
    if not student or not isinstance(student, dict):
        return
    s_text = student.get('response')
    if not s_text:
        return
    try:
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge,
        )
        bridge = get_world_model_bridge()
        bridge.record_teacher_student_pair(
            prompt=prompt,
            teacher_response=teacher_response,
            student_response=str(s_text),
            student_action=student.get('action_tensor'),
        )
    except Exception:
        pass


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
        # G12: kick off HevolveAI SSM student in parallel with llama.cpp.
        # `_g12_student_future` is set for the lifetime of this call and
        # drained before each return path to feed the distillation engine.
        _g12_student_future = _g12_kick_off_student(prompt)
        # Guard: if no LLM endpoint is configured, return gracefully instead
        # of crashing with MissingSchema on every request (SRE FM3 fix).
        if not (DRAFT_GPT_API or GPT_API):
            app.logger.error(
                'No LLM endpoint configured (GPT_API and DRAFT_GPT_API both empty). '
                'Set HEVOLVE_LOCAL_LLM_URL or configure port_registry.')
            _fallback_msg = "I'm not available right now — no language model endpoint is configured."
            _g12_finalize(prompt, _fallback_msg, _g12_student_future)
            return _fallback_msg

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
                    app.logger.info(f"casual conv — routing to draft 0.8B")
                    start = time.time()
                    # T23: casual_conv=True routes to the 0.8B draft on
                    # :8081 via DRAFT_GPT_API. Falls back to GPT_API (4B)
                    # if the draft server isn't available.
                    _api = DRAFT_GPT_API or GPT_API
                    response = pooled_post(
                        _api,
                        json={
                            "model": "llama",
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 200,
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
                        f"casual conv first call — routing to draft 0.8B")
                    start = time.time()
                    _api = DRAFT_GPT_API or GPT_API
                    response = pooled_post(
                        _api,
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

            # G12: drain SSM student future and feed distillation engine
            _g12_finalize(prompt, end_result, _g12_student_future)
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
            resp_json = response.json()
            # Guard: llama-server returns an error dict (e.g. n_ctx exceeded)
            # instead of a choices array. Handle gracefully instead of crashing.
            if 'error' in resp_json:
                _err = resp_json['error']
                _msg = _err.get('message', str(_err)) if isinstance(_err, dict) else str(_err)
                app.logger.error(f"LLM server error: {_msg}")
                _err_reply = f"I couldn't process that request — {_msg}"
                # G12: drain SSM student future and feed distillation engine
                _g12_finalize(prompt, _err_reply, _g12_student_future)
                return _err_reply
            response_text = resp_json["choices"][0]["message"]["content"]
            num_tokens = len(encoding.encode(
                response_text.replace('\n', ' ').replace('\t', ''))) if encoding else len(response_text.split())
            thread_local_data.update_res_token_count(num_tokens)
            _final_text = response_text.replace('\n', ' ').replace('\t', '')
            # G12: drain SSM student future and feed distillation engine
            _g12_finalize(prompt, _final_text, _g12_student_future)
            return _final_text

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
    """Thin delegate to the canonical user-context resolver.

    The full action-history + profile fetch lives in
    ``core.user_context.get_user_context`` which layers:

      1. 30-second per-user TTL cache — subsequent /chat messages
         within the window reuse the last fetch in microseconds.
      2. 1.5-second total hot-path budget — a stalled backend can't
         block chat more than 1.5s; background refresh populates the
         cache for the next request.
      3. Unified format — the same rich output that was previously
         duplicated across hart_intelligence_entry, create_recipe,
         and reuse_recipe with subtle drift.

    There is no message-content classifier here. Chat intent is
    classified by the draft 0.8B model in dispatch_draft_first with
    the augmented classifier prompt; callers that want to skip this
    fetch for a casual message should consult that envelope upstream,
    not re-classify in Python.

    This shim is retained (rather than removing the symbol) so any
    dynamic caller via ``getattr(hart_intelligence_entry, 'get_action_user_details')``
    keeps working. New code should import ``get_user_context`` directly.
    """
    from core.user_context import get_user_context
    return get_user_context(user_id=user_id, mode='reuse')


def get_time_based_history(prompt: str, session_id: str, start_date: str, end_date: str):
    '''
    Time-filtered + semantic conversation history retrieval.

    If either ``start_date`` or ``end_date`` is provided (and parseable as
    ISO-8601), we query ConversationEntry directly with a created_at
    BETWEEN range — this is the "what did I do yesterday between 3pm
    and 5pm" path. Otherwise we fall back to SimpleMem semantic search
    for the "find messages about X" path.

    The caller (parsing_string) passes BOTH params every time — the old
    implementation accepted them and discarded them, which meant every
    temporal query silently degraded to semantic match and returned
    messages from the wrong day. Now the date filter actually fires.

    inputs:
        prompt: text from user from which we need to extract similar messages
        session_id: user_{user_id}
        start_date: ISO-8601 start (or empty / sentinel to mean "no lower bound")
        end_date:   ISO-8601 end   (or empty / sentinel to mean "no upper bound")
    '''
    start_time = time.time()

    try:
        user_id = int(session_id.replace("user_", ""))
    except Exception as e:
        app.logger.warning(f"get_time_based_history: bad session_id {session_id}: {e}")
        return json.dumps({'res': []})

    # Parse ISO-8601 dates loosely. Empty / sentinel strings skip the
    # bound so a single date still works ("everything since 2026-04-10").
    def _parse_iso(s):
        if not s or not isinstance(s, str):
            return None
        s2 = s.strip().rstrip('Z').rstrip('z')
        if not s2 or s2.lower() in ('none', 'null', 'na'):
            return None
        for fmt in (
            '%Y-%m-%dT%H:%M:%S.%f',
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d',
        ):
            try:
                return datetime.strptime(s2, fmt)
            except ValueError:
                continue
        return None

    dt_start = _parse_iso(start_date)
    dt_end = _parse_iso(end_date)
    has_time_range = dt_start is not None or dt_end is not None
    # Reject the "both bounds equal and coerced from now()" sentinel the
    # fallback parser used to emit — that was a no-op range that still
    # produced a misleading "filtered" log line.
    if dt_start is not None and dt_end is not None and dt_start == dt_end:
        has_time_range = False

    if has_time_range:
        try:
            from integrations.social._models_local import ConversationEntry
            from integrations.social.models import get_db
            results = []
            db = get_db()
            try:
                q = db.query(ConversationEntry).filter(
                    ConversationEntry.user_id == str(user_id),
                )
                if dt_start is not None:
                    q = q.filter(ConversationEntry.created_at >= dt_start)
                if dt_end is not None:
                    q = q.filter(ConversationEntry.created_at <= dt_end)
                # Cap at 50 rows so a wide range doesn't flood the LLM.
                rows = q.order_by(
                    ConversationEntry.created_at.desc()
                ).limit(50).all()
                for r in rows:
                    results.append({
                        'message': {
                            'content': getattr(r, 'content', '') or '',
                            'role': getattr(r, 'role', 'assistant'),
                        },
                        'created_at': (r.created_at.isoformat()
                                       if r.created_at else ''),
                        'channel_type': getattr(r, 'channel_type', ''),
                    })
            finally:
                try:
                    db.close()
                except Exception:
                    pass
            elapsed = time.time() - start_time
            app.logger.info(
                f"Time-filtered history: {len(results)} rows in {elapsed:.3f}s "
                f"(range={dt_start}..{dt_end})"
            )
            return json.dumps({'res_in_filter': results})
        except Exception as e:
            app.logger.warning(
                f"Time-filtered ConversationEntry query failed, "
                f"falling back to semantic: {e}"
            )
            # Fall through to the semantic path below

    try:
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
        from core.peer_link.crossbar_publish import publish_thinking_trace
        publish_thinking_trace(
            text=msg, user_id=user_id, request_id=request_id,
            bot_type='Agent',
        )

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


try:
    redis_client = redis.StrictRedis(
        host='azure_all_vms.hertzai.com', port=6369, db=0,
        socket_connect_timeout=2, socket_timeout=2)
    redis_client.ping()
except Exception:
    redis_client = None


def get_frame(user_id):
    """Get latest camera frame — FrameStore first, screenshot fallback, Redis last."""
    # Primary: FrameStore (in-process, zero latency). Go through
    # get_frame_store() rather than get_vision_service().store so
    # every frame-store access funnels through a single accessor —
    # if tomorrow we move the store behind a proxy/cache/adapter, we
    # change one helper, not three reach-ins.
    fs = get_frame_store()
    if fs is not None:
        frame_bytes = fs.get_frame(str(user_id))
        if frame_bytes is not None:
            import cv2
            frame = cv2.imdecode(
                np.frombuffer(frame_bytes, np.uint8), cv2.IMREAD_COLOR,
            )
            if frame is not None:
                app.logger.info(
                    f"Frame for user_id {user_id} from FrameStore")
                return frame[:, :, ::-1]  # BGR → RGB

    # Desktop screenshot fallback (for computer use mode)
    try:
        from PIL import ImageGrab
        screenshot = ImageGrab.grab()
        frame = np.array(screenshot)
        app.logger.info(f"Frame for user_id {user_id} from desktop screenshot ({frame.shape})")
        return frame  # Already RGB
    except Exception as _ss_err:
        app.logger.debug(f"Screenshot fallback failed: {_ss_err}")

    # Last resort: Redis (legacy camera path)
    if redis_client is None:
        app.logger.info(f"No frame for user_id {user_id} — Redis unavailable, no camera/screenshot.")
        return None
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
    # No frame yet? The VisionService might simply be off. Start it once,
    # give frames a moment to arrive, retry. If still nothing the tool
    # returns an honest message instead of pretending there was no
    # visual context. Single start/stop path — reuses the same
    # get_vision_service() singleton the admin toggle and approval
    # handler use, so we never end up with two VisionService instances
    # fighting over the WebSocket port.
    if frame is None:
        try:
            from integrations.vision import get_vision_service
            vs = get_vision_service()
            if not vs.is_running():
                app.logger.info(
                    'parse_visual_context: VisionService idle — starting for tool call'
                )
                vs.start(mode='full')
                # Brief poll for the first frame to land in FrameStore.
                # Description loop runs every ~4s so we give it a short
                # window before giving up.
                import time as _t
                for _ in range(20):  # 20 × 0.25s = 5s total
                    _t.sleep(0.25)
                    frame = get_frame(str(user_id))
                    if frame is not None:
                        break
        except ImportError:
            pass
        except Exception as _e:
            app.logger.debug(f'parse_visual_context auto-start failed: {_e}')
    if frame is None:
        # Still nothing — tell the caller the truth instead of silently
        # returning None, which the LangChain agent would interpret as
        # "no visual info" and confabulate an answer from memory.
        return (
            "No camera frames available yet. I just started the vision "
            "service — give it a couple of seconds and ask again, or "
            "check that your camera is on."
        )
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

        # Tier 0: Qwen+mmproj on local llama-server (already running, zero extra VRAM)
        _llm_port = int(os.environ.get('HEVOLVE_LLM_PORT', 8080))
        try:
            import base64 as _b64
            with open(image_path, 'rb') as _imgf:
                _img_b64 = _b64.b64encode(_imgf.read()).decode('ascii')
            _vlm_resp = requests.post(
                f'http://127.0.0.1:{_llm_port}/v1/chat/completions',
                json={
                    'model': 'local',
                    'messages': [{'role': 'user', 'content': [
                        {'type': 'text', 'text': prompt_text},
                        {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{_img_b64}'}}
                    ]}],
                    'max_tokens': 300,
                },
                timeout=15,
            )
            if _vlm_resp.status_code == 200:
                _vlm_data = _vlm_resp.json()
                result = _vlm_data.get('choices', [{}])[0].get('message', {}).get('content', '')
                if result:
                    app.logger.info(f'Visual context from Qwen+mmproj: {result[:100]}')
                    return result
        except Exception as e:
            app.logger.debug(f"Qwen+mmproj VLM unavailable: {e}")

        # Tier 1: Try local MiniCPM sidecar (port 9891) — dedicated VLM server
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
            app.logger.debug(f"Local MiniCPM sidecar unavailable, falling back: {e}")

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


# ---------------------------------------------------------------------------
# G10: Agent-agent ingestion callback
# ---------------------------------------------------------------------------
# Each tool step inside the LangChain agent loop is a mini "agent talking to
# another agent" exchange.  HevolveAI treats these just like user<->AI turns:
# they become training data via WorldModelBridge.record_interaction().
# Without this hook the ENTIRE inner reasoning loop was invisible to the
# learner — only the final reply was ingested.
#
# On each tool invocation we record:
#   prompt   = "<Thought + Action JSON emitted by the planner agent>"
#   response = "<Observation returned by the tool/agent>"
#   model_id = "langchain-tool:<tool_name>"
#
# All calls are best-effort and fire-and-forget — the callback must NEVER
# raise or add latency to the agent loop.
try:
    from langchain_classic.callbacks.base import BaseCallbackHandler as _LC_BaseCallbackHandler
except ImportError:
    _LC_BaseCallbackHandler = None


if _LC_BaseCallbackHandler is not None:

    class AgentInteractionIngestor(_LC_BaseCallbackHandler):
        """Feed every intra-agent tool step to WorldModelBridge as training data.

        Correlates each tool invocation by capturing the AgentAction emitted
        by ``on_agent_action`` and pairing it with the matching
        ``on_tool_end`` observation.  LangChain may interleave parallel tools
        in the future, so we key by ``run_id`` not a single latest slot.
        """

        def __init__(self, user_id, prompt_id, node_id=None):
            super().__init__()
            self._user_id = str(user_id) if user_id is not None else 'anonymous'
            self._prompt_id = str(prompt_id) if prompt_id is not None else ''
            self._node_id = node_id
            self._pending = {}  # run_id -> (tool, tool_input, action_log, start_ts)
            self._bridge = None

        def _get_bridge(self):
            if self._bridge is not None:
                return self._bridge
            try:
                from integrations.agent_engine.world_model_bridge import (
                    get_world_model_bridge,
                )
                self._bridge = get_world_model_bridge()
            except Exception:
                self._bridge = False  # sentinel meaning "gave up"
            return self._bridge if self._bridge else None

        def on_agent_action(self, action, *, run_id=None, **kwargs):
            try:
                rid = str(run_id) if run_id is not None else 'default'
                self._pending[rid] = (
                    getattr(action, 'tool', '') or '',
                    getattr(action, 'tool_input', '') or '',
                    getattr(action, 'log', '') or '',
                    time.time(),
                )
            except Exception:
                pass

        def on_tool_end(self, output, *, run_id=None, **kwargs):
            try:
                rid = str(run_id) if run_id is not None else 'default'
                ctx = self._pending.pop(rid, None)
                if ctx is None:
                    return
                tool_name, tool_input, action_log, start_ts = ctx
                bridge = self._get_bridge()
                if bridge is None:
                    return
                latency_ms = max(0.0, (time.time() - start_ts) * 1000.0)
                prompt_text = (
                    f"[agent-step]\nAction: {tool_name}\n"
                    f"Input: {str(tool_input)[:2000]}\n"
                    f"Reasoning: {str(action_log)[:1500]}"
                )
                response_text = str(output)[:4000]
                bridge.record_interaction(
                    user_id=self._user_id,
                    prompt_id=self._prompt_id,
                    prompt=prompt_text,
                    response=response_text,
                    model_id=f"langchain-tool:{tool_name}",
                    latency_ms=latency_ms,
                    node_id=self._node_id,
                )
            except Exception:
                # Never propagate — training ingestion must not break the agent.
                pass

        def on_tool_error(self, error, *, run_id=None, **kwargs):
            # Tool errors are ALSO training signal (negative observations).
            try:
                rid = str(run_id) if run_id is not None else 'default'
                ctx = self._pending.pop(rid, None)
                if ctx is None:
                    return
                tool_name, tool_input, action_log, start_ts = ctx
                bridge = self._get_bridge()
                if bridge is None:
                    return
                latency_ms = max(0.0, (time.time() - start_ts) * 1000.0)
                prompt_text = (
                    f"[agent-step]\nAction: {tool_name}\n"
                    f"Input: {str(tool_input)[:2000]}\n"
                    f"Reasoning: {str(action_log)[:1500]}"
                )
                response_text = f"[TOOL_ERROR] {str(error)[:3000]}"
                bridge.record_interaction(
                    user_id=self._user_id,
                    prompt_id=self._prompt_id,
                    prompt=prompt_text,
                    response=response_text,
                    model_id=f"langchain-tool-error:{tool_name}",
                    latency_ms=latency_ms,
                    node_id=self._node_id,
                )
            except Exception:
                pass
else:
    AgentInteractionIngestor = None  # noqa: N816


# main function
def get_ans(casual_conv, req_tool, user_id, query, custom_prompt, preferred_lang):
    start_time = time.time()
    # casual_conv is the draft classifier's `is_casual` flag propagated
    # from the /chat handler — when True we skip the action+profile
    # fetch entirely because a casual acknowledgement doesn't consult
    # either. No Python-side classification of `query` happens anywhere
    # in the user_context module; the draft 0.8B is the only classifier.
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
    # Skip tool loading for casual_conv=True — the 0.8B draft handles
    # casual chat without tools, saving ~2.5s of tool registry loading.
    if casual_conv:
        # Minimal first-pass tools — enough for the LLM to help with common
        # tasks (calculator, history, image) without loading the full registry.
        tools = get_tools(req_tool=req_tool, is_first=True)
        app.logger.info("get_tools loaded (is_first=True, casual_conv=True) %s seconds",
                        time.time() - tools_start_time)
    else:
        # Exhaustive intent-based tool set — full registry scan based on
        # detected intent. This is the heavy path (~2.5s) but gives the LLM
        # access to every tool for complex/agentic requests.
        tools = get_tools(req_tool=req_tool, is_first=False)
        app.logger.info("get_tools loaded (is_first=False, casual_conv=False) %s seconds",
                        time.time() - tools_start_time)

    app.logger.info(f'tools {type(tools)}')
    language = SUPPORTED_LANG_DICT.get(preferred_lang[:2], 'English')
    colloquial = True

    # Build dynamic identity for fast-path too
    _fast_agent_config = thread_local_data.get_agent_config() if hasattr(thread_local_data, 'get_agent_config') else None
    _fast_identity = build_identity_prompt(_fast_agent_config, '', user_details)

    # Regional tone + resonance (per-user adaptive tone)
    _tone_block = ''
    try:
        from core.agent_personality import get_regional_tone_prompt
        _tone_block = get_regional_tone_prompt(preferred_lang)
    except Exception:
        pass
    _resonance_block = ''
    try:
        from core.resonance_profile import get_or_create_profile
        from core.resonance_tuner import build_resonance_prompt, pre_tune_from_input
        _res_profile = get_or_create_profile(str(user_id))
        _res_profile = pre_tune_from_input(_res_profile, prompt)
        _resonance_block = build_resonance_prompt(_res_profile) or ''
    except Exception:
        pass

    # Strong language directive — Qwen3.5-4B (and many multilingual
    # models) will ignore a weak "respond in English" when the system
    # prompt mentions Indian/Asian cultural traits or the user's email/
    # name hints at a regional origin.  Seen on 2026-04-15: English
    # user input "hi" → "Vanakkam! Nan ungal nanban. Hevolve-va romba
    # nalla tool, na idea build pannura help pannu..." (Romanised Tamil).
    # Fix: explicit script + policy block, placed AFTER identity +
    # tone so it wins by recency.
    _lang_directive = (
        f"\n\nRESPONSE LANGUAGE POLICY (STRICT):\n"
        f"- Respond in {language} only.  Match the script the USER wrote in.\n"
        f"- If the user writes in Latin/English script, respond in Latin/"
        f"English script — do NOT transliterate other languages into "
        f"Latin letters (no Romanised Tamil/Hindi/etc).\n"
        f"- Cultural references are welcome, but the response body must be "
        f"in {language}.\n"
        if (language or '').lower() == 'english' else ''
    )

    prefix = f"""{_fast_identity}
        Answer questions accurately and respond as quickly as possible in {language}.
        {_tone_block}{_resonance_block}
        Keep responses under 200 words. Be colloquial and natural - don't always greet or use the user's name.
        IMPORTANT: Do NOT re-introduce yourself if you already did in the conversation history below. Continue naturally.
        {_lang_directive}

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
    # G10: Feed every intra-agent tool step into WorldModelBridge so the
    # inner reasoning loop (not just the final answer) becomes training data.
    _ingestor_callbacks = None
    # Global suppression flag: once we've proven that the running langchain_classic
    # does NOT accept `callbacks=` on `.run()`, keep going but skip attempting it
    # again.  Without this we'd hit the TypeError on every chat turn and still
    # record/log the silent-drop metric every time.  The metric counter captures
    # the fallback events so a /metrics endpoint can expose G10 ingestion health.
    if AgentInteractionIngestor is not None and not getattr(
            get_ans, '_g10_callbacks_unsupported', False):
        try:
            _ingestor_callbacks = [AgentInteractionIngestor(
                user_id=user_id,
                prompt_id=prom_id,
            )]
        except Exception:
            _ingestor_callbacks = None
    try:
        try:
            if _ingestor_callbacks is not None:
                ans = agent_chain.run(
                    {'input': query}, callbacks=_ingestor_callbacks)
            else:
                ans = agent_chain.run({'input': query})
        except TypeError as _cb_te:
            # Older langchain_classic does not accept callbacks on .run().
            # Silently swallowing this caused G10 ingestion to disable globally
            # with zero alarm (see /api/admin/metrics — g10_silent_drops).
            # Log once, flip the suppression flag, bump the counter, and fall
            # back so the agent call still succeeds.  Also emit to the
            # centralized exception_collector so SelfHealingDispatcher /
            # ExceptionWatcher pick up the pattern and can surface it in the
            # operator /health feed — a future schema drift must not silently
            # degrade G10 ingestion a second time.  Intentionally NOT
            # broadened to bare Exception: the goal is graceful-degrade on a
            # known langchain-version mismatch, not hide every failure.
            get_ans._g10_callbacks_unsupported = True
            get_ans._g10_silent_drops = (
                getattr(get_ans, '_g10_silent_drops', 0) + 1)
            _payload_preview = (str(query or ''))[:100]
            app.logger.warning(
                "[G10] agent_chain.run(callbacks=) raised TypeError; "
                "G10 training ingestion disabled for this process. "
                "exc=%r payload=%r count=%d",
                _cb_te, _payload_preview, get_ans._g10_silent_drops,
                extra={
                    'component': 'AgentInteractionIngestor',
                    'action': 'record_interaction_failed',
                    'silent_drops': get_ans._g10_silent_drops,
                })
            try:
                from exception_collector import record_exception
                record_exception(
                    _cb_te, module=__name__, function='get_ans',
                    component='AgentInteractionIngestor',
                    action='record_interaction_failed',
                    silent_drops=get_ans._g10_silent_drops,
                    payload_preview=_payload_preview,
                )
            except Exception:
                # Fire-and-forget — never let telemetry break the agent.
                pass
            ans = agent_chain.run({'input': query})
    except Exception as _agent_err:
        # G13: Report text-modality generation failures to the learner so
        # the modality router can re-weight future routing decisions.
        # submit_output_feedback(status='error') lands on /v1/corrections and
        # also fires the C1 hive-hint lookup.  Fire-and-forget: never mask
        # the underlying agent error.
        try:
            from integrations.agent_engine.world_model_bridge import (
                get_world_model_bridge as _g_wmb,
            )
            _bridge = _g_wmb()
            _elapsed_s = time.time() - agent_chain_start_time
            _bridge.submit_output_feedback(
                output_modality='text',
                status='error',
                context=(query or '')[:2000],
                model_used='langchain-agent',
                error_message=f'{type(_agent_err).__name__}: {str(_agent_err)[:500]}',
                generation_time_seconds=float(_elapsed_s),
                user_id=str(user_id),
            )
        except Exception as _fb_err:
            app.logger.debug(
                f"[get_ans] submit_output_feedback skipped: {_fb_err}")
        raise
    app.logger.info("time taken by chain agent run %s seconds",
                    time.time() - agent_chain_start_time)

    # G13 (success-side): empty / trivial / fallback replies are ALSO weak
    # text outputs worth tagging for router adjustment.  We only flag truly
    # empty or the sentinel "model_not_available" / exception-shaped strings
    # — genuine short replies shouldn't be penalised.
    try:
        _ans_text = str(ans or '').strip()
        _looks_empty = (len(_ans_text) == 0)
        _looks_error = any(
            marker in _ans_text.lower()
            for marker in ('model_not_available', 'internal server error',
                           '[tool_error]', 'exception:', 'traceback')
        )
        if _looks_empty or _looks_error:
            from integrations.agent_engine.world_model_bridge import (
                get_world_model_bridge as _g_wmb2,
            )
            _bridge2 = _g_wmb2()
            _bridge2.submit_output_feedback(
                output_modality='text',
                status='error',
                context=(query or '')[:2000],
                model_used='langchain-agent',
                error_message='empty_or_degenerate_reply' if _looks_empty
                else f'error_markers_in_reply: {_ans_text[:300]}',
                generation_time_seconds=float(
                    time.time() - agent_chain_start_time),
                user_id=str(user_id),
            )
    except Exception as _fb_err2:
        app.logger.debug(
            f"[get_ans] success-side feedback skipped: {_fb_err2}")
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
            # EMA math is <1ms — do it synchronously for the response payload.
            # HevolveAI dispatch (HTTP/bridge) is async — never blocks response.
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


_tts_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tts_async')

# --- Language persistence (DRY: one write path) ---
_HART_LANG_PATH = os.path.join(
    os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'hart_language.json')


def _persist_language(lang: str) -> bool:
    """Thin shim for backward compatibility.  Real implementation is in
    core.user_lang.set_preferred_lang — atomic write + on_lang_change
    subscriber bus (for draft eviction etc.).

    Kept as a named function here because older code in this file +
    downstream refs import it; new code should call the core API
    directly."""
    from core.user_lang import set_preferred_lang
    return set_preferred_lang(lang)


def _vision_keyword_override(prompt: str) -> bool:
    """Safety net that overrides the draft classifier's `is_casual`
    verdict when the user's prompt clearly needs vision.

    Thin wrapper — the canonical keyword set + compiled regex live
    in core.constants.VISION_INTENT_PATTERN so the single source of
    truth is a constant file that multiple callers can import.

    Returns True → caller should fall through to the full LangChain
    tool path (Visual_Context_Camera / parse_visual_context becomes
    reachable).  Returns False → keep the draft's standby reply.
    """
    try:
        from core.constants import prompt_needs_vision
        return prompt_needs_vision(prompt or '')
    except Exception:
        return False


def _chat_reply(user_id, request_id, response_text: str, **payload):
    """Return a /chat JSON response AND fire TTS synthesis in the background.

    Every reply path from the /chat handler must go through this helper
    so the TTS synthesis trigger lives in ONE place:

      - Reuse path (chat_agent from reuse_recipe)
      - Autonomous creation from reuse
      - Creation suggested from reuse
      - Agentic_Router tool fired
      - Create_Agent tool fired (autonomous)
      - Create_Agent tool fired (interactive)
      - Normal LangChain get_ans path
      - Evaluation mode after creation

    Before this helper existed, only the normal LangChain path fired
    ``_tts_synthesize_and_publish`` inline (hart_intelligence_entry.py
    line 6095), so every other reply route delivered text-only to the
    frontend and the user saw no audio — the exact symptom reported
    on 2026-04-11 ("hi got text reply but no TTS"). Consolidating here
    makes it impossible for a new reply branch to accidentally skip
    TTS: you can't return from /chat without calling this helper
    (the old jsonify was renamed at each return site to match).

    The TTS call is fire-and-forget — it submits work to the
    background ``_tts_executor`` and returns immediately. No hot-path
    latency is added; the frontend already subscribes to
    com.hertzai.pupit.{userId} and plays whatever audio lands there.

    Args:
        user_id: User id for pupit topic routing.
        request_id: Request id for correlating TTS audio to the chat
            reply on the frontend.
        response_text: The assistant's reply text. Empty strings skip
            the TTS call (the old line 6096 else-branch still logs
            the empty-response error for observability).
        **payload: Extra keys to merge into the JSON response body.
            The caller's previous jsonify arguments become kwargs.

    Returns:
        A Flask ``Response`` from ``jsonify(...)`` with the merged
        payload — semantically identical to the old inline
        ``return jsonify({'response': response_text, **payload})``.
    """
    if response_text:
        try:
            # preferred_lang resolution must match the chat entry path:
            # body/kwarg → canonical persisted reader → 'en'.  Bare
            # 'en' default forced English Piper on Tamil replies.
            _lang = payload.get('preferred_lang') or payload.get('language')
            if not _lang:
                try:
                    from core.user_lang import get_preferred_lang
                    _lang = get_preferred_lang() or 'en'
                except Exception:
                    _lang = 'en'
            _tts_synthesize_and_publish(response_text, user_id, request_id, language=_lang)
        except Exception as e:
            # Never let a TTS failure block delivery of the text reply.
            app.logger.debug(f"_chat_reply: TTS dispatch skipped: {e}")

        # Persist conversation turn to SimpleMem so multi-turn context works.
        # prep_outputs (the normal LangChain path) never fires for casual_conv
        # or draft-first early returns, so we save here — the ONE place every
        # reply goes through. The user prompt comes via the 'user_prompt' kwarg
        # from callers that have it (most /chat paths do).
        _prompt = payload.pop('user_prompt', '')
        try:
            if _prompt and response_text:
                _mem = get_memory(user_id=user_id)
                if _mem and hasattr(_mem, 'save_context'):
                    from langchain_classic.schema.messages import HumanMessage, AIMessage
                    _mem.save_context(
                        {'input': _prompt},
                        {'output': response_text[:500]},
                    )
        except Exception as _mem_err:
            app.logger.debug(f"_chat_reply: memory save skipped: {_mem_err}")

        # U1-U8 cross-device mirroring (task #389): submit persist + WAMP
        # publish for both sides of the turn to the chat_messages
        # executor — the chat response is already serialized and on its
        # way to the client; the DB write and chat.new fan-out must not
        # add latency to this path.  On worker saturation we fall back
        # to inline (see persist_and_publish_async).  request_id links
        # the (user, assistant) pair for downstream dedup.  device_id /
        # agent_id / lang / attachments are optional; callers pass them
        # via **payload when known.
        try:
            from integrations.social import chat_messages as _cm
            _dev = payload.get('device_id')
            _agent = payload.get('agent_id')
            _prompt_id = payload.get('prompt_id')
            _lang_hint = (payload.get('preferred_lang')
                          or payload.get('language') or _lang)
            _atts = payload.get('attachments')
            _rid = str(request_id) if request_id is not None else None
            if _prompt:
                _cm.persist_and_publish_async(
                    str(user_id), 'user', _prompt,
                    agent_id=_agent, prompt_id=_prompt_id,
                    request_id=_rid, device_id=_dev,
                    lang=_lang_hint, attachments=_atts,
                )
            _cm.persist_and_publish_async(
                str(user_id), 'assistant', response_text,
                agent_id=_agent, prompt_id=_prompt_id,
                request_id=_rid, device_id=_dev,
                lang=_lang_hint, attachments=None,
            )
        except Exception as _chat_sync_err:
            app.logger.debug(
                f"_chat_reply: chat-sync mirror skipped: {_chat_sync_err}")

    payload['response'] = response_text
    return jsonify(payload)


def _tts_synthesize_and_publish(text, user_id, request_id, language='en'):
    """Fire-and-forget: synthesize TTS, push audio via WAMP.

    Same pattern as chatbot_pipeline/chatbot.py:
      make_it_talk(text) → publish(res, user_id, 'pupit')
    Frontend subscribes to com.hertzai.pupit.{userId} and plays.

    Calls the existing synthesize_text() which handles everything:
    language segmentation, multi-lang routing, <music>/<sing> tags,
    model orchestration, GPU swap — the full pipeline.

    Only fires when TTS engine exists (Nunba bundled mode).
    """
    if not text or not text.strip():
        app.logger.debug("TTS: skipped (empty text)")
        return
    try:
        from tts.tts_engine import get_tts_engine
        engine = get_tts_engine()
        if not engine:
            app.logger.warning("TTS: skipped (engine is None)")
            return
        app.logger.info(f"TTS: engine OK ({engine.backend_name}), submitting for '{text[:50]}...'")
    except ImportError as ie:
        app.logger.warning(f"TTS: skipped (ImportError: {ie})")
        return

    def _bg():
        app.logger.info(f"TTS _bg: thread started for request_id={request_id}")
        try:
            from tts.tts_engine import synthesize_text
            # Clean text for speech — remove artifacts that TTS can't pronounce
            import re as _re
            _clean = text
            _clean = _re.sub(r'```[\s\S]*?```', '', _clean)       # code blocks
            _clean = _re.sub(r'`[^`]+`', '', _clean)              # inline code
            _clean = _re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', _clean)  # [text](url) → text
            _clean = _re.sub(r'https?://\S+', '', _clean)         # URLs
            _clean = _re.sub(r'[*_~]{1,3}', '', _clean)           # markdown bold/italic
            _clean = _re.sub(r'#{1,6}\s', '', _clean)             # markdown headers
            _clean = _re.sub(r'\[.*?\]', '', _clean)              # [bracketed content]
            _clean = _re.sub(r"['\"]", '', _clean)                # quotes that cause artifacts
            # Remove emojis (Unicode emoji ranges)
            _clean = _re.sub(
                r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF'
                r'\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF'
                r'\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002600-\U000026FF'
                r'\U0000FE00-\U0000FE0F\U0000200D]+', '', _clean)
            _clean = _re.sub(r'\s+', ' ', _clean).strip()         # collapse whitespace
            if not _clean:
                return  # nothing left after cleaning
            _raw = synthesize_text(_clean, language=language)
            app.logger.info(f"TTS async: synthesize_text returned: {_raw}")
            # synthesize_text may return a file path string OR a JSON dict/string
            # with {"path": "...", "duration": ...}. Normalize to a file path.
            audio_path = _raw
            if isinstance(_raw, dict):
                audio_path = _raw.get('path', '')
            elif isinstance(_raw, str) and _raw.startswith('{'):
                try:
                    audio_path = json.loads(_raw).get('path', '')
                except (json.JSONDecodeError, AttributeError):
                    pass
            if audio_path and os.path.isfile(audio_path):
                audio_filename = os.path.basename(audio_path)
                # Use absolute URL if this node's external URL is known —
                # required when the request was delegated from a remote peer,
                # since relative URLs resolve against the originator's server
                # which doesn't have this audio file (task #266).
                _ext_url = os.environ.get('HEVOLVE_EXTERNAL_URL', '').rstrip('/')
                if _ext_url:
                    audio_url = f'{_ext_url}/tts/audio/{audio_filename}'
                else:
                    audio_url = f'/tts/audio/{audio_filename}'
                app.logger.info(f"TTS async: publishing audio {audio_url} to pupit.{user_id}")
                _tts_payload = {
                    'text': [text[:200]],
                    'generated_audio_url': audio_url,
                    'request_id': str(request_id),
                    'action': 'TTS',
                }
                publish_async(f'com.hertzai.pupit.{user_id}', json.dumps(_tts_payload))
                # Also push via SSE directly — publish_async → MessageBus/WAMP doesn't
                # reach SSE clients, so the unified helper fans out to the Nunba
                # main.py SSE broker. See core/platform/events.broadcast_sse_safe.
                from core.platform.events import broadcast_sse_safe
                if not broadcast_sse_safe('message', _tts_payload, user_id=user_id):
                    app.logger.debug("TTS SSE: broadcast_sse_event unavailable (Nunba main not loaded)")
                app.logger.info(f"TTS async: published successfully")
            else:
                app.logger.warning(f"TTS async: no audio file — path={audio_path}, exists={os.path.isfile(audio_path) if audio_path else False}")
        except ImportError:
            pass  # TTS not available (cloud mode)
        except Exception as e:
            app.logger.error(f"TTS async failed: {e}", exc_info=True)

    global _tts_executor
    try:
        _tts_executor.submit(_bg)
    except RuntimeError:
        # Executor was shut down (interpreter restart, previous crash).
        # Recreate and retry — TTS must not silently stop working.
        app.logger.warning("TTS executor dead — recreating")
        _tts_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tts_async')
        _tts_executor.submit(_bg)


@app.route('/chat', methods=['POST'])
def chat():
    # ── G6: Authentication gate ──
    # Three-layer auth: API key > JWT > body user_id (backward compat)
    # HEVOLVE_API_KEY env var: if set, all requests MUST provide it.
    # If not set (dev mode), allow unauthenticated but log a warning.
    _api_key = os.environ.get('HEVOLVE_API_KEY', '')
    auth_header = request.headers.get('Authorization', '')
    _bearer_token = auth_header[7:] if auth_header.startswith('Bearer ') else ''

    if _api_key:
        # API key is configured — validate it first (Layer 0)
        # Accept either API key directly or a valid JWT
        import hmac as _hmac_mod
        _api_key_match = (
            _bearer_token and
            _hmac_mod.compare_digest(_bearer_token, _api_key)
        )
        if _api_key_match:
            g.auth_source = 'api_key'
        elif _bearer_token:
            # Try JWT decode (Layer 1 / Layer 2)
            try:
                from integrations.social.auth import decode_jwt
                jwt_payload = decode_jwt(_bearer_token)
                if jwt_payload and 'user_id' in jwt_payload:
                    g.auth_source = 'jwt'
                else:
                    return jsonify({
                        'error': 'Invalid or expired token.',
                        'response': None,
                    }), 401
            except Exception:
                return jsonify({
                    'error': 'Invalid or expired token.',
                    'response': None,
                }), 401
        else:
            return jsonify({
                'error': 'Authentication required. Provide Authorization: Bearer <token> header.',
                'response': None,
            }), 401
    else:
        # No API key configured (dev/flat mode) — log warning once
        if not getattr(chat, '_auth_warned', False):
            app.logger.warning(
                'HEVOLVE_API_KEY not set — /chat endpoint accepts unauthenticated '
                'requests. Set HEVOLVE_API_KEY env var for production.'
            )
            chat._auth_warned = True
        g.auth_source = 'none'

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

    data = request.get_json() or {}

    # Early validation — return 400 instead of 500 on missing required fields
    if not data.get('user_id') and not request.headers.get('Authorization', '').startswith('Bearer '):
        return jsonify({'error': 'user_id is required (in body or via Bearer token)', 'response': None}), 400
    if data.get('prompt_id') is None and 'prompt_id' not in data:
        return jsonify({'error': 'prompt_id is required', 'response': None}), 400
    if not data.get('prompt') and not data.get('input_text'):
        return jsonify({'error': 'prompt is required', 'response': None}), 400

    # ── Two-layer auth: extract user_id from JWT if present ──
    # Layer 1 (LOCAL): Bearer token signed by this node's HS256 secret
    # Layer 2 (HIVE): Bearer token with Ed25519 node_sig (cross-node)
    # Fallback: body user_id (backward compat for desktop/Nunba mode)
    if g.auth_source not in ('jwt', 'api_key'):
        if _bearer_token:
            try:
                from integrations.social.auth import decode_jwt
                jwt_payload = decode_jwt(_bearer_token)
                if jwt_payload and 'user_id' in jwt_payload:
                    _body_uid = data.get('user_id')
                    _jwt_uid = jwt_payload['user_id']
                    if _body_uid and str(_body_uid) != str(_jwt_uid):
                        # Sanitize for log injection (strip newlines/ANSI escapes)
                        import re as _re
                        _safe_body = _re.sub(r'[\x00-\x1f\x7f-\x9f]', '', str(_body_uid))[:64]
                        _safe_jwt = _re.sub(r'[\x00-\x1f\x7f-\x9f]', '', str(_jwt_uid))[:64]
                        app.logger.warning(
                            f'/chat user_id mismatch: body={_safe_body} jwt={_safe_jwt} '
                            f'— using JWT (body value dropped)')
                    data['user_id'] = _jwt_uid
                    g.auth_source = 'jwt'
                    g.token_scope = jwt_payload.get('scope', 'local')
                else:
                    g.auth_source = 'body'
            except Exception:
                g.auth_source = 'body'
        else:
            g.auth_source = 'body' if g.auth_source == 'none' else g.auth_source
    elif g.auth_source == 'jwt':
        # JWT already decoded above in the API-key-present path
        try:
            from integrations.social.auth import decode_jwt
            jwt_payload = decode_jwt(_bearer_token)
            if jwt_payload and 'user_id' in jwt_payload:
                data['user_id'] = jwt_payload['user_id']
                g.token_scope = jwt_payload.get('scope', 'local')
        except Exception:
            pass

    # Reject unauthenticated requests on multi-user deployments.
    # Body-sourced user_id is only safe on flat tier (single-user desktop).
    # On regional (multi-user LAN) or central (cloud), accepting body user_id
    # lets any client impersonate any user.
    _node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
    if g.auth_source == 'body' and (
        _node_tier in ('central', 'regional') or
        os.environ.get('HEVOLVE_REQUIRE_AUTH', '').lower() == 'true'
    ):
        return jsonify({
            'error': 'Authentication required. Provide Authorization: Bearer <token> header.',
            'response': None,
        }), 401

    user_id = data.get('user_id', None)
    # Sanitize user_id globally (not just JWT-mismatch branch) to prevent
    # log injection via newlines/ANSI escapes in body-sourced user_id.
    if user_id is not None:
        import re as _re
        user_id = _re.sub(r'[\x00-\x1f\x7f-\x9f]', '', str(user_id))[:64]
        data['user_id'] = user_id

    # Reject oversized prompts to prevent DoS (worker-pool starvation).
    _prompt = data.get('prompt') or data.get('input_text') or ''
    if len(_prompt) > 16384:
        return jsonify({
            'error': f'Prompt too large ({len(_prompt)} chars, max 16384).',
            'response': None,
        }), 413

    # Fall back to the canonical hart_language.json when frontend omits
    # preferred_lang.  Previously defaulted to 'en' which bypassed the
    # draft skip-gate for Tamil (hart_language.json says 'ta' but /chat
    # never read it).  Single source of truth: core.user_lang.
    _req_lang = data.get('preferred_lang') or data.get('language')
    if _req_lang and _req_lang.strip():
        preferred_lang = _req_lang.strip()
    else:
        try:
            from core.user_lang import get_preferred_lang
            preferred_lang = get_preferred_lang()
        except Exception:
            preferred_lang = 'en'

    # Persist language preference so next startup warm-up loads the right
    # TTS + right draft-boot decision (see llama_config.should_boot_draft
    # cohort gate, commit 12c9304).
    #
    # PREVIOUS BUGGY LOGIC (removed 2026-04-15):
    #   if preferred_lang != 'en' and not _lang_persisted and not file_exists:
    #       _persist_language(...)
    #
    # The 3 guards caused "hart_language.json stuck at whatever was first
    # written" — explicitly:
    #   - `!= 'en'` skipped English switches (user moves Tamil → English,
    #     nothing persists, next boot reads stale 'ta')
    #   - `not file_exists` meant after ANY write, subsequent switches
    #     no-op (user's Tamil selection never overwrote stale 'en' from
    #     first-run default)
    #   - `_lang_persisted` one-shot flag capped to one write per process
    #
    # `_persist_language` now read-first-skips-if-unchanged internally,
    # so calling it every request is cheap (~10us stat call).
    if preferred_lang:
        _persist_language(preferred_lang)
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
    # Tier ladder preference forwarded from Nunba Demopage toggle (see
    # routes/hartos_backend_adapter.py + routes/chatbot_routes.py).
    # Values: 'local_only' | 'auto' | 'hive_preferred'.  Default 'auto'
    # preserves today's behavior for every existing caller (old Nunba
    # builds, direct HTTP clients, MCP bridge, tests).  Validated on
    # Nunba side too; we revalidate here to keep HARTOS self-contained.
    intelligence_preference = data.get('intelligence_preference', 'auto')
    if intelligence_preference not in ('local_only', 'auto', 'hive_preferred'):
        intelligence_preference = 'auto'
    # Draft-first dispatch: Qwen3.5-0.8B standby reply + delegate signal.
    # Default ON — the dispatcher gracefully returns 'no_draft_model' when
    # the 0.8B isn't loaded and we transparently fall through to the normal
    # path, so enabling by default is safe on fresh installs. Request body
    # or env var can still override:
    #   HEVOLVE_DRAFT_FIRST=0  → hard off (overrides request body)
    #   HEVOLVE_DRAFT_FIRST=1  → hard on  (overrides request body)
    #   draft_first in body     → per-request override
    #   (nothing)               → default ON
    _draft_first_env = os.environ.get('HEVOLVE_DRAFT_FIRST', '').strip()
    if _draft_first_env == '0':
        draft_first = False
    elif _draft_first_env == '1':
        draft_first = True
    elif 'draft_first' in data:
        draft_first = bool(data['draft_first'])
    else:
        draft_first = True
    # Confidence floor for routing decisions driven by the draft
    # classifier's intent flags (is_create_agent). Below this the draft's
    # classification is treated as uncertain and we stay on the safer
    # path — returning the draft's standby reply instead of force-
    # switching into autogen CREATE. Keeps the draft in charge but
    # refuses to make an irreversible routing change on a coin flip.
    _DRAFT_INTENT_CONFIDENCE = 0.75
    # Persona block forwarded to the draft-first dispatcher so a system
    # agent's voice comes back on the standby reply. Only populated on
    # the system-agent path below; initialised here so the draft-first
    # branch (which runs BEFORE that path for non-system-agent requests)
    # can reference it safely.
    custom_prompt = None
    model_config = data.get('model_config', None)
    task_source = data.get('task_source', 'own')
    thread_local_data.set_task_source(task_source)
    channel_context = data.get('channel_context', None)
    if channel_context:
        thread_local_data.channel_context = channel_context

    # USER PRIORITY: mark user activity so daemon dispatch yields the LLM
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
        # Recipe pull-on-demand: when prompt_id has no local recipe
        # but the cloud has one (cross-device user), pull it before
        # falling into the create-mode branch.  Best-effort - if cloud
        # is offline / blob missing / schema mismatch, returns False
        # and we proceed to create-mode as before.  Closes the
        # cross-device gap from the 2026-05-04 Speech Therapy
        # silent-fallback incident.
        _prompt_path = os.path.join(PROMPTS_DIR, f'{prompt_id}.json')
        if not os.path.exists(_prompt_path):
            try:
                from core.recipe_sync import pull_recipe
                if pull_recipe(PROMPTS_DIR, prompt_id, str(user_id)):
                    app.logger.info(
                        f'recipe_sync: pulled recipe bundle for '
                        f'prompt_id={prompt_id} from cloud - REUSE path '
                        f'now reachable')
            except Exception as e:
                app.logger.debug(f'recipe_sync: pull skipped: {e}')

        # System agents (like Nunba) route directly to langchain casual chat
        # instead of entering gather_info/CREATE mode
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
                        # All flows complete → REUSE
                        create_agent = set_flags_to_enter_review_mode(no_of_flow, user_id, prompt_id) #returns false
                    else:
                        # Some flows complete, some not.
                        # Enter CREATE — recipe() has resume logic (initialize_with_resume)
                        # that will skip completed flows and resume from where it left off.
                        # It also detects existing sessions (user_prompt in user_tasks) to
                        # avoid re-creating agents, making this idempotent.
                        app.logger.info(f'{no_of_flow} Recipe JSON does not exist — resuming CREATE for remaining flows')
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

    # ── Draft-first dispatch (Path 1: default chat  +  Path 2: system agents) ──
    # This runs AFTER the prompt_id branch so we know whether the request
    # is bound for autogen CREATE/REUSE. Skipped entirely for agentic
    # orchestration — those are multi-step flows a 0.8B draft can't handle.
    # For system agents (is_system_agent=True), custom_prompt is already
    # populated with the persona and is forwarded so the standby reply
    # comes back in character.
    _is_agentic_orchestration = bool(create_agent) or bool(autonomous)
    # Skip draft-first for non-Latin-script languages — the 0.8B can't produce
    # correct native script (outputs garbled Kannada for Tamil, etc.). Going
    # straight to the 4B avoids wasted compute on a useless standby reply.
    # Skip draft-first for non-Latin-script languages.  Canonical set
    # lives in core.constants — see also the dispatcher's own guard at
    # dispatch_draft_first (belt + suspenders).
    from core.constants import NON_LATIN_SCRIPT_LANGS as _NON_LATIN_LANGS
    _skip_draft_for_lang = preferred_lang and preferred_lang[:2] in _NON_LATIN_LANGS
    if draft_first and not _skip_draft_for_lang and prompt and user_id and not _is_agentic_orchestration:
        try:
            from integrations.agent_engine.speculative_dispatcher import get_speculative_dispatcher
            dispatcher = get_speculative_dispatcher()
            # agent_bound=True when prompt_id refers to a real agent on
            # disk — the dispatcher uses this signal to never let the
            # 0.8B draft short-circuit the specialist on trivial Q&A.
            # Computed BEFORE the request-id coalesce so a fallback
            # request_id doesn't masquerade as an agent.
            _agent_bound = bool(prompt_id)
            result = dispatcher.dispatch_draft_first(
                prompt, str(user_id),
                str(prompt_id) if prompt_id else str(request_id or 'anon'),
                agent_persona=custom_prompt or None,
                preferred_lang=preferred_lang,
                user_pref=intelligence_preference,
                agent_bound=_agent_bound,
            )
            # Only commit when the dispatcher actually produced a reply.
            # no_draft_model / circuit breaker / guardrail block all leave
            # result['response'] empty — fall through to the normal path.
            if result.get('response'):
                # If the draft classifier flagged a NEW agent creation intent
                # with high confidence, fall through to the autogen CREATE
                # flow below instead of returning the draft's standby reply.
                # This is the SINGLE source of truth for create_agent
                # routing — callers no longer need to keyword-match user
                # text before calling /chat, the 0.8B draft handles it.
                _draft_conf = float(result.get('draft_confidence') or 0.0)
                # Language change: the draft detected "talk to me in
                # tamil" → language_change="ta". Override preferred_lang
                # so the main LLM prompt says "respond in Tamil" and TTS
                # routes to a Tamil-capable engine (Indic Parler).
                _lang_change = result.get('language_change', '')
                if _lang_change:
                    preferred_lang = _lang_change
                    app.logger.info(
                        f'draft classifier: language_change={_lang_change} '
                        f'— overriding preferred_lang for this request'
                    )
                    # Persist so next requests also use the new language
                    _persist_language(_lang_change)

                if (result.get('is_create_agent')
                        and _draft_conf >= _DRAFT_INTENT_CONFIDENCE):
                    app.logger.info(
                        'draft classifier: is_create_agent=true '
                        f'(conf={_draft_conf:.2f}) — routing to autogen CREATE'
                    )
                    create_agent = True
                    _is_agentic_orchestration = True
                    # Fall through — do NOT return the draft reply; the
                    # create_agent branch below runs in this same request.
                elif _vision_keyword_override(prompt):
                    # Vision safety-net: the 0.8B draft has no sight,
                    # so a `is_casual=True` draft reply to "can you see
                    # me?" would be a confabulation.  When the prompt
                    # clearly asks for camera/scene/screen access, force
                    # the full LangChain tool path so
                    # `Visual_Context_Camera` (parse_visual_context)
                    # can run.  Single-source regex lives in
                    # core.constants.VISION_INTENT_PATTERN.
                    app.logger.info(
                        'draft classifier returned casual but prompt '
                        'matches VISION_INTENT_PATTERN — falling through '
                        'to LangChain tool path for Visual_Context_Camera'
                    )
                    # Fall through — do NOT return the draft standby
                    # reply; the tool-based path below will run and the
                    # VLM tool will produce a grounded answer.
                else:
                    return _chat_reply(
                        user_id, request_id, result['response'],
                        Agent_status='Draft-First Mode',
                        speculation_id=result.get('speculation_id'),
                        expert_pending=result.get('expert_pending', False),
                        delegate=result.get('delegate'),
                        draft_model=result.get('draft_model'),
                        draft_confidence=result.get('draft_confidence'),
                        is_correction=bool(result.get('is_correction', False)),
                        is_casual=bool(result.get('is_casual', False)),
                        is_create_agent=bool(result.get('is_create_agent', False)),
                        channel_connect=result.get('channel_connect', ''),
                        language_change=result.get('language_change', ''),
                        latency_ms=result.get('latency_ms'),
                        user_prompt=prompt,
                        preferred_lang=preferred_lang,
                    )
        except ImportError:
            pass

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
                return _chat_reply(
                    user_id, request_id,
                    'Need user_id and text to create agent',
                    intent=['FINAL_ANSWER'],
                    req_token_count=0, res_token_count=0, history_request_id=[],
                )
            if autonomous:
                # Check if an existing agent can handle this task before creating new one
                try:
                    from integrations.agentic_router import find_matching_agent
                    _match = find_matching_agent(prompt, PROMPTS_DIR)
                    if _match and _match.get('agent_id'):
                        _mid = _match['agent_id']
                        _mconfig = os.path.join(PROMPTS_DIR, f'{_mid}.json')
                        if os.path.exists(_mconfig) and os.path.exists(
                                os.path.join(PROMPTS_DIR, f'{_mid}_0_recipe.json')):
                            app.logger.info(
                                f'Matched existing agent {_match["name"]} ({_mid}) '
                                f'— routing to REUSE instead of CREATE')
                            return chat_agent(user_id, prompt, _mid, file_id, request_id)
                except Exception as _me:
                    app.logger.debug(f'Agent matching skipped: {_me}')

                # No existing agent matches — create new one
                # Autonomous dispatch (from daemon or API): LLM self-generates agent config
                # AND immediately creates recipe — no human review step needed.
                # Full pipeline: gather_info → save config → recipe() → completed
                auto_response = _autonomous_gather_info(user_id, prompt, prompt_id)

                # Now immediately create the recipe so next dispatch enters REUSE
                _config_path = os.path.join(PROMPTS_DIR, f'{prompt_id}.json')
                if os.path.exists(_config_path):
                    try:
                        try:
                            from integrations.agent_engine.dispatch import mark_create_start, mark_create_end
                            mark_create_start()
                        except ImportError:
                            mark_create_end = None
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
                            return _chat_reply(
                                user_id, request_id, recipe_response,
                                intent=['FINAL_ANSWER'],
                                req_token_count=0, res_token_count=0,
                                history_request_id=[],
                                Agent_status='completed',
                                autonomous_creation=True, prompt_id=prompt_id,
                            )
                        else:
                            app.logger.info(f'Autonomous recipe() returned: {str(recipe_response)[:100]}')
                    except Exception as e:
                        import traceback
                        app.logger.error(f'Autonomous recipe creation failed: {e}\n{traceback.format_exc()}')
                    finally:
                        try:
                            from integrations.agent_engine.dispatch import mark_create_end
                            mark_create_end()
                        except ImportError:
                            pass

                # Fallback: config saved but recipe failed — next dispatch will retry
                with _user_lock:
                    review_agents[_ak] = True
                    conversation_agent[_ak] = False
                    _touch_agent_timestamp(_ak)
                _record_lifecycle('Review Mode', user_id, prompt_id,
                                 f'Autonomous creation via dispatch: {prompt[:100]}')
                _push_workflow_flowchart(user_id, prompt_id, request_id)
                return _chat_reply(
                    user_id, request_id, auto_response,
                    intent=['FINAL_ANSWER'],
                    req_token_count=0, res_token_count=0,
                    history_request_id=[],
                    Agent_status='Review Mode',
                    autonomous_creation=True, prompt_id=prompt_id,
                )
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
                    return _chat_reply(
                        user_id, request_id, ans,
                        intent=['FINAL_ANSWER'],
                        req_token_count=0, res_token_count=0, history_request_id=[],
                        Agent_status='Creation Mode', prompt_id=prompt_id,
                    )
                else:
                    # Completed (or forced completion after max turns)
                    app.logger.info('COMPLETED STATUS')
                    _save_and_enter_review(new_res)
                    _push_workflow_flowchart(user_id, prompt_id, request_id)
                    return _chat_reply(
                        user_id, request_id,
                        'Got Agent details successfully lets move on to review them one at a time',
                        intent=['FINAL_ANSWER'],
                        req_token_count=0, res_token_count=0, history_request_id=[],
                        Agent_status='Review Mode', prompt_id=prompt_id,
                    )

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
                    return _chat_reply(
                        user_id, request_id,
                        'Agent created with available details. Moving to review.',
                        intent=['FINAL_ANSWER'],
                        req_token_count=0, res_token_count=0, history_request_id=[],
                        Agent_status='Review Mode', prompt_id=prompt_id,
                    )
                _record_lifecycle('Creation Mode', user_id, prompt_id, f'Creation continuing after parse error: {e}')
                return _chat_reply(
                    user_id, request_id, response,
                    intent=['FINAL_ANSWER'],
                    req_token_count=0, res_token_count=0, history_request_id=[],
                    Agent_status='Creation Mode', prompt_id=prompt_id,
                )
        # Phase 2: Review Phase (re-snapshot flags under lock after Phase 1 may have mutated)
        with _user_lock:
            _in_review = _ak in review_agents and review_agents[_ak]
            _in_convo = _ak in conversation_agent and conversation_agent[_ak]
        if _in_review and not _in_convo:
            try:
                from integrations.agent_engine.dispatch import mark_create_start, mark_create_end
                mark_create_start()
            except ImportError:
                mark_create_end = None
            try:
                response = recipe(user_id,prompt,prompt_id,file_id,request_id)
            finally:
                try:
                    from integrations.agent_engine.dispatch import mark_create_end as _mce
                    _mce()
                except ImportError:
                    pass
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
                return _chat_reply(
                    user_id, request_id, response,
                    intent=['FINAL_ANSWER'],
                    req_token_count=0, res_token_count=0, history_request_id=[],
                    Agent_status='completed',
                )
            _record_lifecycle('Review Mode', user_id, prompt_id, 'Agent details being reviewed')
            return _chat_reply(
                user_id, request_id, response,
                intent=['FINAL_ANSWER'],
                req_token_count=0, res_token_count=0, history_request_id=[],
                Agent_status='Review Mode',
            )
        # Phase 3: Evaluation Phase
        if _in_review and _in_convo:
            return evaluate_agent_after_creation_in_review(file_id, prompt, prompt_id, request_id, user_id)

    if prompt_id and os.path.exists(os.path.join(PROMPTS_DIR, f'{prompt_id}.json')):
        _ak = f'{user_id}_{prompt_id}'  # Ensure compound key available for reuse path
        with open(os.path.join(PROMPTS_DIR, f'{prompt_id}.json'), "r") as file:
            created_json = json.load(file)


        if chat_agent is None:
            return _chat_reply(
                user_id, request_id,
                'Agent reuse module is unavailable. Please check server dependencies.',
                intent=['FINAL_ANSWER'],
                req_token_count=0, res_token_count=0, history_request_id=[],
            )

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
                    return _chat_reply(
                        user_id, request_id, auto_response,
                        intent=['FINAL_ANSWER'],
                        req_token_count=0, res_token_count=0,
                        history_request_id=[],
                        Agent_status='Review Mode',
                        autonomous_creation=True,
                        prompt_id=new_prompt_id,
                    )
                else:
                    _record_lifecycle('Reuse Mode', user_id, new_prompt_id, f'Creation suggested from reuse: {agent_desc[:100]}')
                    return _chat_reply(
                        user_id, request_id, response,
                        intent=['FINAL_ANSWER'],
                        req_token_count=0, res_token_count=0,
                        history_request_id=[],
                        Agent_status='Reuse Mode',
                        creation_suggested=True,
                        suggested_agent_description=agent_desc,
                        prompt_id=new_prompt_id,
                    )

            # Fallback: pattern matching on agent response text
            if _response_signals_creation(response):
                app.logger.info('Reuse agent response text signals new agent creation needed')
                _record_lifecycle('Reuse Mode', user_id, prompt_id, 'Creation suggested via response pattern')
                return _chat_reply(
                    user_id, request_id, response,
                    intent=['FINAL_ANSWER'],
                    req_token_count=0, res_token_count=0,
                    history_request_id=[],
                    Agent_status='Reuse Mode',
                    creation_suggested=True,
                )

        _record_lifecycle('Reuse Mode', user_id, prompt_id, 'Agent reused for conversation')
        _resonance = _tune_resonance_after_chat(user_id, prompt, response)
        # Route through _chat_reply so TTS fires on the reuse path too
        # (the legacy jsonify here skipped synth, causing the 2026-04-11
        # "hi got text but no audio" regression).
        return _chat_reply(
            user_id, request_id, response,
            intent=['FINAL_ANSWER'], req_token_count=0, res_token_count=0,
            history_request_id=[], Agent_status='Reuse Mode', **_resonance,
        )

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

        return _chat_reply(
            user_id, request_id, ans,
            intent=['FINAL_ANSWER'],
            Agent_status='Plan Mode',
            agentic_plan={
                'task_description': task_desc,
                'steps': plan_steps,
                'matched_agent_id': matched_agent,
                'requires_consent': True,
            },
            prompt_id=prompt_id if prompt_id else _next_prompt_id(),
            req_token_count=thread_local_data.get_req_token_count(),
            res_token_count=thread_local_data.get_res_token_count(),
            history_request_id=thread_local_data.get_reqid_list(),
        )

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
            return _chat_reply(
                user_id, request_id, auto_response,
                intent=['FINAL_ANSWER'],
                req_token_count=0, res_token_count=0,
                history_request_id=[],
                Agent_status='Review Mode',
                autonomous_creation=True,
                prompt_id=new_prompt_id,
            )
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
            return _chat_reply(
                user_id, request_id, resp_text,
                intent=['FINAL_ANSWER'],
                req_token_count=0, res_token_count=0,
                history_request_id=[],
                Agent_status='Creation Mode',
                prompt_id=new_prompt_id,
            )

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
        # TTS fires below via _chat_reply — don't duplicate the call here.
    else:
        post_dict = {'user_id': user_id, 'status': 'ERROR', 'task_name': "CHAT", 'uid': request_id,
                     'task_id': f"CHAT_{str(request_id)}", 'request_id': request_id, 'failure_reason': 'Got null response from GPT'}
        publish_async('com.hertzai.longrunning.log', post_dict)

    end_time = time.time()
    elapsed_time = end_time - start_time
    app.logger.info(f"time taken for this full call is {elapsed_time}")

    # If the Navigate_App tool fired during get_ans(), lift the resolved
    # ui_actions into the response so the Nunba LiquidActionBar renders
    # clickable page chips without a separate round-trip. Cleared afterward
    # so the next request on the same thread starts fresh.
    _ui_actions = thread_local_data.get_ui_actions()
    if _ui_actions:
        thread_local_data.clear_ui_actions()

    return _chat_reply(
        user_id, request_id, ans,
        intent=thread_local_data.get_recognize_intents(),
        req_token_count=thread_local_data.get_req_token_count(),
        res_token_count=thread_local_data.get_res_token_count(),
        history_request_id=thread_local_data.get_reqid_list(),
        ui_actions=_ui_actions,
        user_prompt=prompt,
        **_resonance,
    )


def evaluate_agent_after_creation_in_review(file_id, prompt, prompt_id, request_id, user_id):
    if chat_agent is None:
        return _chat_reply(
            user_id, request_id,
            'Agent reuse module is unavailable. Please check server dependencies.',
            intent=['FINAL_ANSWER'], req_token_count=0, res_token_count=0,
            history_request_id=[], Agent_status='Evaluation Mode',
        )
    response = chat_agent(user_id, prompt, prompt_id, file_id, request_id)
    _record_lifecycle('Evaluation Mode', user_id, prompt_id, 'Agent being evaluated after creation')
    _resonance = _tune_resonance_after_chat(user_id, prompt, response)
    return _chat_reply(
        user_id, request_id, response,
        intent=['FINAL_ANSWER'], req_token_count=0, res_token_count=0,
        history_request_id=[], Agent_status='Evaluation Mode', **_resonance,
    )


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
    # Support both string and numeric IDs
    _uid = int(user_id) if str(user_id).isdigit() else str(user_id)
    _pid = int(prompt_id) if str(prompt_id).isdigit() else str(prompt_id)
    if request_from == 'Reuse':
        res = time_based_execution(str(task_description), _uid, _pid, action_entry_point)
    else:
        res = time_execution(str(task_description), _uid, _pid, action_entry_point)
    return jsonify({'response':f'{res}'}), 200


@app.route('/api/vlm/stop', methods=['POST'])
@require_local_or_token_csrf_safe
def vlm_stop():
    """Halt an in-progress VLM computer-use loop for a (user, prompt).

    Ports OmniParser/omnitool/gradio/agentic_rpc.py:/stop into HARTOS
    proper.  When the user clicks Stop in Nunba's indicator window,
    the request lands here, the loop's stop flag is set, and the
    next iteration of run_local_agentic_loop exits cleanly with
    exit_reason='stopped' before another action runs on the screen.

    Auth: ``@require_local_or_token_csrf_safe``.  Nunba's bundled install
    POSTs here from desktop/indicator_window.py over localhost without a
    JWT — the decorator allows that.  Remote callers (production HARTOS
    on regional/central tier) must send ``Authorization: Bearer
    <HARTOS_API_TOKEN>``.  Without this gate, any unauthenticated
    network client could bulk-halt another user's VLM sessions.

    The ``_csrf_safe`` variant adds an Origin/Referer header check on
    top of the localhost gate, closing the same-machine
    cross-origin-browser-CSRF vector: a malicious page on a different
    origin in the same browser cannot trigger a Stop on the user's
    active VLM session.  Native desktop callers (the indicator window)
    don't send Origin/Referer at all, so they pass through unaffected.

    Body (JSON):
        {"user_id": "<uid>", "prompt_id": "<pid>"}
    Response:
        {"status": "stopped"|"no_active_session", "user_id", "prompt_id"}

    Empty body / missing prompt_id → bulk-stop every active session
    for the given user_id.  Empty user_id is rejected (bulk-stop
    across all users would be a foot-gun).
    """
    data = request.get_json(silent=True) or {}
    user_id = data.get('user_id')
    prompt_id = data.get('prompt_id')

    if not user_id:
        return jsonify({'error': 'user_id required'}), 400

    from integrations.vlm.local_loop import (
        request_stop, list_active_sessions,
    )

    if prompt_id:
        found = request_stop(str(user_id), str(prompt_id))
        return jsonify({
            'status': 'stopped' if found else 'no_active_session',
            'user_id': str(user_id),
            'prompt_id': str(prompt_id),
        }), 200

    # Bulk-stop: every session for this user_id
    stopped = []
    for uid, pid in list_active_sessions():
        if uid == str(user_id):
            if request_stop(uid, pid):
                stopped.append({'user_id': uid, 'prompt_id': pid})
    return jsonify({
        'status': 'stopped' if stopped else 'no_active_session',
        'user_id': str(user_id),
        'stopped_sessions': stopped,
    }), 200


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
    # Quick VLM health check — delegate to lightweight_backend (single path)
    from integrations.vision.lightweight_backend import get_vision_backend
    _vlm_backend = get_vision_backend()
    if not _vlm_backend.is_available():
        return jsonify({'response': 'Visual agent: no VLM server available. Start llama-server with --mmproj or MiniCPM.', 'vlm_status': 'offline'}), 200
    _uid = int(user_id) if str(user_id).isdigit() else str(user_id)
    _pid = int(prompt_id) if str(prompt_id).isdigit() else str(prompt_id)

    # Computer use mode: use VLM point_and_act directly with screenshot
    mode = data.get('mode', 'auto')  # 'computer_use', 'camera', 'auto'
    if mode == 'computer_use' or (mode == 'auto' and request_from != 'Reuse'):
        try:
            from integrations.vlm.qwen3vl_backend import get_qwen3vl_backend
            import base64, io
            from PIL import ImageGrab, Image
            backend = get_qwen3vl_backend()
            screenshot = ImageGrab.grab()
            from integrations.vlm.local_computer_tool import VLM_IMG_W, VLM_IMG_H
            img_resized = screenshot.resize((VLM_IMG_W, VLM_IMG_H), Image.LANCZOS)
            buf = io.BytesIO()
            img_resized.save(buf, 'JPEG', quality=50)
            b64 = base64.b64encode(buf.getvalue()).decode('ascii')
            result = backend.point_and_act(b64, str(task_description))
            return jsonify({
                'response': result.get('reasoning', ''),
                'action': result.get('action', 'none'),
                'screen_x': result.get('screen_x', 0),
                'screen_y': result.get('screen_y', 0),
                'norm_x': result.get('norm_x', 0),
                'norm_y': result.get('norm_y', 0),
                'text': result.get('text', ''),
                'done': result.get('done', False),
                'strategy': result.get('strategy', ''),
                'latency': result.get('latency', 0),
                'vlm_status': 'ok',
            }), 200
        except Exception as e:
            app.logger.error(f'Computer use error: {e}')
            return jsonify({'response': f'Computer use error: {str(e)[:200]}', 'vlm_status': 'error'}), 200

    # Camera/legacy mode: use existing visual agent pipeline
    try:
        if request_from == 'Reuse':
            res = visual_based_execution(str(task_description), _uid, _pid)
        else:
            res = visual_execution(str(task_description), _uid, _pid)
        if res is None:
            return jsonify({'response': 'No camera frame available. Enable camera or send a frame via channel.', 'vlm_status': 'no_frame'}), 200
        return jsonify({'response': f'{res}'}), 200
    except KeyError:
        return jsonify({'response': 'No active visual agent session. Send a frame first via camera or channel.', 'vlm_status': 'no_session'}), 200
    except Exception as e:
        app.logger.error(f'Visual agent error: {e}')
        return jsonify({'response': f'Visual agent error: {str(e)[:100]}', 'vlm_status': 'error'}), 200

@app.route('/response_ack',methods=['POST'])
def response_ack():
    app.logger.info('GOT REQUEST IN response_ack')
    data = request.get_json()
    user_id = data.get('user_id',None)
    request_id = data.get('request_id',None)
    thread_local_data.set_request_id(request_id=request_id)
    prompt_id = data.get('prompt_id',None)
    return jsonify({'status': 'acknowledged'}), 200


@app.route('/api/agent/approval', methods=['POST'])
def agent_approval():
    """Process a user's approval decision on an agent capability request.

    The frontend LiquidUI overlay (see liquid_ui_service.py:3452) POSTs
    `{agent_id, action, decision}` when the user clicks Approve / Deny
    on a consent card the agent raised via _request_capability_consent
    or _request_screen_consent. Actions we honour:

        enable_camera  → VisionService.start('full')  (camera+screen feed)
        enable_screen  → VisionService.start('lite')  (screen only)

    Both route through _apply_embodied_toggle in the channels admin API
    so the manual settings toggle and the agentic approval path share
    ONE start/stop implementation — no parallel lifecycle.
    """
    try:
        data = request.get_json(silent=True) or {}
        agent_id = data.get('agent_id', '')
        action = str(data.get('action', '')).strip().lower()
        decision = str(data.get('decision', '')).strip().lower()
        if decision not in ('approve', 'approved', 'allow', 'yes', 'deny', 'denied', 'no'):
            return jsonify({'status': 'error', 'reason': 'invalid decision'}), 400
        approved = decision in ('approve', 'approved', 'allow', 'yes')
        if not approved:
            # Stage-C (Symptom #6): publish the deny event on WAMP too
            # so subscribers (VisionService, UI) can tear down cleanly
            # without inspecting HTTP responses.
            try:
                _cu_d = data.get('user_id') or data.get('userId') or 'local'
                publish_async(
                    f'com.hertzai.hevolve.vision.{_cu_d}',
                    json.dumps({
                        'type': 'consent',
                        'user_id': _cu_d,
                        'agent_id': agent_id,
                        'action': action,
                        'decision': 'denied',
                        'ts': int(time.time()),
                    }),
                )
            except Exception as _pub_exc:
                app.logger.debug(f"agent_approval deny WAMP publish failed: {_pub_exc}")
            app.logger.info(f'agent_approval: agent={agent_id} action={action} DENIED')
            return jsonify({'status': 'denied', 'action': action}), 200

        # Map the approval to a feed toggle and reuse the single
        # _apply_embodied_toggle codepath admin settings use.
        feed = None
        if action in ('enable_camera', 'camera', 'vision'):
            feed = 'camera'
        elif action in ('enable_screen', 'screen', 'computer_use'):
            feed = 'screen'
        elif action in ('enable_audio', 'audio', 'mic'):
            feed = 'audio'
        else:
            # Unknown actions are accepted but do nothing — the agent
            # may be asking for a capability we don't manage here.
            app.logger.info(f'agent_approval: agent={agent_id} action={action} '
                            f'approved but no feed mapping — noop')
            return jsonify({'status': 'approved', 'action': action, 'applied': False}), 200

        try:
            from integrations.channels.admin.api import (
                get_api, _apply_embodied_toggle,
            )
            api = get_api()
            cfg = api._global_config.embodied_ai
            # Flip the persisted flag so the next restart sees it and so
            # get_embodied_status reports the right state.
            if feed == 'camera':
                cfg.camera_enabled = True
            elif feed == 'screen':
                cfg.screen_capture_enabled = True
            elif feed == 'audio':
                cfg.audio_enabled = True
            api._save_config()
            _apply_embodied_toggle(feed, True, cfg)
            # Stage-C (Symptom #6, 2026-04-16) — publish the consent
            # event on Crossbar WAMP so subscribers (VisionService,
            # frontend, mobile) never have to poll or watch a raw WS
            # for the decision. House-rule 6: 'ALL realtime push uses
            # Crossbar WAMP'. Topic format matches the existing
            # com.hertzai.hevolve.* convention.
            try:
                _cu = data.get('user_id') or data.get('userId') or 'local'
                _consent_topic = f'com.hertzai.hevolve.vision.{_cu}'
                _consent_payload = {
                    'type': 'consent',
                    'user_id': _cu,
                    'agent_id': agent_id,
                    'action': action,
                    'feed': feed,
                    'decision': 'approved',
                    'ts': int(time.time()),
                }
                publish_async(_consent_topic, json.dumps(_consent_payload))
                app.logger.info(
                    f"agent_approval: WAMP consent published to "
                    f"{_consent_topic} feed={feed}"
                )
            except Exception as _pub_exc:
                # Publish failure MUST NOT roll back the approval.
                # The HTTP response still tells the caller the feed
                # was applied — the WAMP channel is for notification,
                # not authorization.
                app.logger.debug(f"agent_approval WAMP publish failed: {_pub_exc}")
            app.logger.info(f'agent_approval: agent={agent_id} action={action} '
                            f'APPROVED → feed={feed} started')
            return jsonify({'status': 'approved', 'action': action,
                            'feed': feed, 'applied': True}), 200
        except Exception as inner:
            app.logger.warning(f'agent_approval: applied failed: {inner}')
            return jsonify({'status': 'approved', 'action': action,
                            'applied': False, 'error': str(inner)[:200]}), 200
    except Exception as e:
        app.logger.error(f'agent_approval error: {e}')
        return jsonify({'status': 'error', 'error': str(e)[:200]}), 500

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

def _merge_prompts_with_cloud(local_prompts: list, cloud_url: str) -> list:
    """Append cloud prompts to ``local_prompts``, dedup by prompt_id.

    Local-first: when the same prompt_id exists in both, the local copy
    is kept (it carries the newer recipe payload + filesystem-side
    metadata).  Cloud failures are swallowed so a flaky DB never
    breaks the local-only listing — same best-effort contract both
    /prompts and /prompts/public have always offered.

    Single source of truth for the merge so /prompts (user-scoped) and
    /prompts/public (catalogue) cannot drift in dedup logic, error
    handling, or the 'cloud'/'has_recipe' field defaults.
    """
    local_ids = {str(p.get('prompt_id', '')) for p in local_prompts}
    try:
        res = pooled_get(cloud_url, timeout=5)
        if res.status_code == 200:
            for item in (res.json() or []):
                if str(item.get('prompt_id', '')) in local_ids:
                    continue
                item['source'] = 'cloud'
                item.setdefault('has_recipe', False)
                local_prompts.append(item)
    except Exception:
        pass
    return local_prompts


@app.route('/prompts', methods=['GET'])
def get_prompts():
    """List prompts for a user.  Local files (filtered by creator) merged
    with the user's cloud-side prompts via _merge_prompts_with_cloud."""
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

    # 2. Merge in cloud-only agents the user owns on hevolve.ai —
    # the local store doesn't know about agents the user created from
    # another device.  Powers Nunba's "Hybrid" intelligence preference.
    # MUST use CENTRAL_DB_URL not DB_URL — in bundled Nunba mode DB_URL
    # collapses to http://localhost:5000 (this same Flask process), so
    # merging would just hit our own /getprompt_onlyuserid handler and
    # see the same local prompts again — silent no-op.
    if CENTRAL_DB_URL:
        _merge_prompts_with_cloud(
            prompts,
            f'{CENTRAL_DB_URL}/getprompt_onlyuserid/?user_id={req_user_id}')

    return jsonify(prompts)


@app.route('/prompts/public', methods=['GET'])
def get_public_prompts():
    """Return all public prompts/agents.  Local-first, cloud-merge.
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

    # 2. Merge in cloud-only public agents.  Same central-vs-local-DB_URL
    # rationale as /prompts above — DB_URL is localhost in bundled mode
    # and would no-op the merge.
    if CENTRAL_DB_URL:
        _merge_prompts_with_cloud(
            prompts, f'{CENTRAL_DB_URL}/getprompt_all/')

    return jsonify(prompts)


@app.route('/api/admin/agent-data/<int:prompt_id>/info', methods=['GET'])
def admin_agent_data_info(prompt_id):
    """Diagnostic info for an agent's persisted data file.

    Returns size, last-modified, saved_at metadata, and the top-level
    data keys so the admin UI can show disk footprint per agent and
    decide when to prune stale agents. Wraps helper.get_agent_data_info
    — the info-gathering stays in helper where the file-layout logic
    lives; this route is a thin JSON facade."""
    try:
        from helper import get_agent_data_info as _info
    except ImportError:
        return jsonify({'error': 'helper.get_agent_data_info unavailable'}), 500
    return jsonify(_info(int(prompt_id)))


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


# ───────────────────────── Recipe sync endpoints ─────────────────────
# Cross-device sync for recipe-file BUNDLES (the {id}.json + flow +
# personality + per-action recipes), distinct from /createpromptlist
# which only syncs metadata.  See core/recipe_sync.py for the
# canonical wire envelope.  Closes the cross-device gap that caused
# the 2026-05-04 Speech Therapy silent fallback.

# In-process recipe blob store (cloud tier installs override via
# DB-backed implementation; flat installs fine with on-disk).
_RECIPE_BLOB_DIR = os.path.join(PROMPTS_DIR, '..', 'recipe_blobs')


@app.route('/prompts/sync', methods=['POST'])
def upload_recipe_bundle():
    """POST a recipe bundle for one prompt_id.

    Body shape: see core.recipe_sync.build_envelope.  The cloud
    deployment of HARTOS persists this; flat installs may also
    accept it for testing but the file is intended to round-trip
    via the central instance.

    Defenses (added in post-shipment hardening pass):
      M5 - prompt_id MUST equal os.path.basename(prompt_id) so an
           attacker can't write to ../../etc/passwd via the path
           construction at the bottom of this handler
      M6 - reject when incoming uploaded_at < existing uploaded_at
           UNLESS body carries force=True.  Prevents an older client
           from silently clobbering a newer cloud state.  Idempotent
           on identical-checksum re-pushes (200 OK without rewrite)
    """
    try:
        from core.recipe_sync import SCHEMA_VERSION, _safe_filename
    except ImportError:
        return jsonify({'error': 'recipe_sync unavailable'}), 503
    envelope = request.get_json() or {}
    if envelope.get('schema_version') != SCHEMA_VERSION:
        return jsonify({
            'error': 'schema_mismatch',
            'expected': SCHEMA_VERSION,
            'got': envelope.get('schema_version'),
        }), 400
    prompt_id = envelope.get('prompt_id')
    files = envelope.get('files') or {}
    if not prompt_id or not files:
        return jsonify({'error': 'prompt_id + files required'}), 400

    # M5: prompt_id flows into a filesystem path - validate it the
    # same way the recipe_sync.pull_recipe path validates filenames.
    pid_str = str(prompt_id)
    if not _safe_filename(pid_str + '.json'):
        return jsonify({
            'error': 'unsafe prompt_id - must be basename-safe',
            'prompt_id': pid_str,
        }), 400

    try:
        os.makedirs(_RECIPE_BLOB_DIR, exist_ok=True)
    except OSError as e:
        return jsonify({'error': f'blob dir create failed: {e}'}), 500
    blob_path = os.path.join(_RECIPE_BLOB_DIR, f'{pid_str}.json')

    # M6: staleness guard - reject if incoming envelope is older than
    # what's on disk, unless caller explicitly opts into overwrite.
    # The 'force' query param is a deliberate, explicit override; the
    # body field exists for symmetry.
    force = bool(envelope.get('force')) or \
        request.args.get('force', '').lower() in ('1', 'true', 'yes')
    incoming_ts = int(envelope.get('uploaded_at') or 0)
    incoming_checksum = envelope.get('checksum', '')
    if os.path.isfile(blob_path):
        try:
            with open(blob_path, 'r', encoding='utf-8') as f:
                existing = json.load(f)
            existing_ts = int(existing.get('uploaded_at') or 0)
            existing_checksum = existing.get('checksum', '')
            # Idempotent re-push: same checksum → 200 OK without rewrite
            if incoming_checksum and incoming_checksum == existing_checksum:
                return jsonify({
                    'stored': True, 'unchanged': True,
                    'checksum': incoming_checksum,
                    'files_count': len(files),
                })
            if not force and incoming_ts < existing_ts:
                return jsonify({
                    'error': 'stale_upload',
                    'reason': (
                        f'incoming uploaded_at={incoming_ts} < '
                        f'existing={existing_ts}; pass force=true to '
                        f'override (will silently overwrite newer cloud state)'),
                    'existing_checksum': existing_checksum,
                }), 409  # Conflict
        except (IOError, OSError, json.JSONDecodeError, ValueError) as e:
            app.logger.debug(
                f'/prompts/sync staleness check failed for {pid_str}: {e}')
            # Fall through and write - existing blob is corrupt anyway

    # Atomic write so concurrent GET can't read a half-written blob.
    tmp_path = blob_path + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(envelope, f)
        os.replace(tmp_path, blob_path)
    except (IOError, OSError) as e:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return jsonify({'error': f'blob write failed: {e}'}), 500
    return jsonify({
        'stored': True,
        'checksum': envelope.get('checksum', ''),
        'files_count': len(files),
    })


@app.route('/prompts/sync/<prompt_id>', methods=['GET'])
def download_recipe_bundle(prompt_id):
    """GET the recipe bundle for *prompt_id*.

    Returns 404 when no blob has been uploaded yet.  Cloud tier
    installs serve from a DB-backed store; flat installs read from
    the on-disk blob dir written by upload_recipe_bundle.

    Defense (post-shipment hardening, mirrors upload_recipe_bundle's
    M5 check): prompt_id flows from the URL path into a filesystem
    join.  Flask's default ``string`` URL converter forbids ``/`` but
    permits ``..`` after URL-decode, so a request like
    ``GET /prompts/sync/..%2F..%2Fetc%2Fpasswd`` would otherwise
    resolve outside _RECIPE_BLOB_DIR.  We validate the same way the
    upload path does — via core.recipe_sync._safe_filename — and add
    a realpath containment check as belt+suspenders.
    """
    try:
        from core.recipe_sync import _safe_filename
    except ImportError:
        return jsonify({'error': 'recipe_sync unavailable'}), 503

    pid_str = str(prompt_id)
    if not _safe_filename(pid_str + '.json'):
        return jsonify({
            'error': 'unsafe prompt_id - must be basename-safe',
        }), 400

    blob_path = os.path.join(_RECIPE_BLOB_DIR, f'{pid_str}.json')
    # Realpath containment: even if _safe_filename misses some
    # platform-specific edge case (symlink dir, NTFS short name),
    # this catch-all keeps reads inside the blob dir.
    blob_real = os.path.realpath(blob_path)
    base_real = os.path.realpath(_RECIPE_BLOB_DIR)
    if not (blob_real == base_real
            or blob_real.startswith(base_real + os.sep)):
        return jsonify({'error': 'unsafe prompt_id'}), 400

    if not os.path.isfile(blob_path):
        return jsonify({'error': 'not_found'}), 404
    try:
        with open(blob_path, 'r', encoding='utf-8') as f:
            envelope = json.load(f)
    except (IOError, OSError, json.JSONDecodeError) as e:
        return jsonify({'error': f'blob read failed: {e}'}), 500
    return jsonify(envelope)


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

    # Check 5: Vision frame store (optional — reports nothing if vision is off)
    try:
        fs = get_frame_store()
        if fs is None:
            checks['vision_frame_store'] = 'unavailable'
        else:
            stats = fs.stats() if hasattr(fs, 'stats') else {}
            checks['vision_frame_store'] = stats or 'ok'
    except Exception as e:
        checks['vision_frame_store'] = f'fail: {e}'

    # Check 6: Diarization sidecar (optional — reports nothing if disabled)
    try:
        diar = get_diarization_service()
        if diar is None:
            checks['diarization'] = 'unavailable'
        else:
            checks['diarization'] = 'ready' if diar.is_ready() else 'starting'
    except Exception as e:
        checks['diarization'] = f'fail: {e}'

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
    # NOTE: prompts_backup.snapshot_at_boot runs at module-import
    # time (line ~452) so it fires for BOTH the CLI path here AND
    # Nunba's library-import path.  Removing the duplicate call here
    # avoids two snapshots per CLI boot.

    # Guardrail hash verification — refuse to boot with tampered
    # hive_guardrails values unless HEVOLVE_GUARDRAIL_HASH_ENFORCE=0
    # (dev override). The module-level call inside hive_guardrails is
    # self-consistent at pristine import; this explicit boot re-check
    # surfaces any post-import monkey-patch or dynamic module substitution
    # before any ConstitutionalFilter entry point has a chance to run.
    try:
        from security.hive_guardrails import (
            enforce_guardrail_integrity,
            get_guardrail_hash,
        )
        enforce_guardrail_integrity()
        logging.getLogger('hevolve_social').info(
            "[Guardrail] hash verified: %s", get_guardrail_hash()[:16] + '...')
    except RuntimeError:
        # enforce_guardrail_integrity already logged CRITICAL and we want
        # the hard abort to propagate — this is a guardian-angel integrity
        # check and we refuse to boot.
        raise
    except Exception as e:
        logging.getLogger('hevolve_social').warning(
            f"[Guardrail] hash verification skipped (import error): {e}")

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
    _serve_app(app, host='0.0.0.0', port=get_port('backend'))


def _serve_app(app, host: str, port: int) -> None:
    """Boot the WSGI app on Hypercorn (preferred) or Waitress (fallback).

    Hypercorn = ASGI: asyncio event loop multiplexes connection IO so
    idle keep-alive / SSE clients don't burn worker threads (waitress
    holds one thread per connection regardless of activity).  Sync
    Flask handlers run in `loop.run_in_executor()` against a pool of
    HEVOLVE_WORKER_THREADS (default 256) — slow paths (LLM inference,
    pip install in /tts/setup-engine) occupy executor threads but
    never block the IO layer, so /dashboard/agents and /health stay
    responsive while a TTS install grinds.

    Single-process today.  Multi-process (HEVOLVE_WORKERS=N) requires
    sharding the in-process singletons first (EventBus, ResourceGovernor,
    agent_daemon, watchdog, goal_seeding) — each fork would otherwise
    spawn N parallel agent fleets fighting over the same goals + the
    same Windows Job Object handle.  Until then `workers=1` is the
    correct value; same code path will lift to N workers for free
    once the sharding work lands.

    Falls back to Waitress on ImportError so older deployments / partial
    installs / cx_Freeze bundles missing the hypercorn h2/wsproto chain
    still boot — this preserves the prior behavior bit-for-bit.
    """
    log = logging.getLogger('hevolve_social')
    try:
        worker_threads = int(os.environ.get('HEVOLVE_WORKER_THREADS', '256'))
    except (TypeError, ValueError):
        worker_threads = 256

    try:
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        from hypercorn.asyncio import serve as _hcserve
        from hypercorn.config import Config
        from hypercorn.middleware import AsyncioWSGIMiddleware

        config = Config()
        config.bind = [f'{host}:{port}']
        config.keep_alive_timeout = 120     # SSE-friendly long polls
        config.h11_max_incomplete_size = 16 * 1024 * 1024  # 16MB request bodies
        config.accesslog = None             # HARTOS emits its own access log
        config.errorlog = '-'

        asgi_app = AsyncioWSGIMiddleware(app)

        async def _runner():
            loop = asyncio.get_running_loop()
            loop.set_default_executor(
                ThreadPoolExecutor(max_workers=worker_threads,
                                   thread_name_prefix='hartos'))
            await _hcserve(asgi_app, config)

        log.info(
            f"Starting Hypercorn (ASGI) on {host}:{port} "
            f"(executor_threads={worker_threads}, single-process)"
        )
        asyncio.run(_runner())
        return
    except ImportError as exc:
        log.warning(
            f"Hypercorn unavailable ({exc}) — falling back to Waitress")

    log.info(f"Starting Waitress (WSGI) on {host}:{port} (threads=50)")
    serve(app, host=host, port=port, threads=50)


if __name__ == '__main__':
    main()
    # app.debug = True
    # flask_thread = threading.Thread(target=lambda: serve(app, host='0.0.0.0', port=6777))
    # flask_thread.daemon = True
    # flask_thread.start()
    # from crossbar_server import component
    # # Run the WAMP client
    # run([component])

