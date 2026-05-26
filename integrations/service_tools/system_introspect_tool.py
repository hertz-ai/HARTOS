"""System self-introspection tool for agent self-awareness.

Exposes the Nunba/HARTOS runtime state (GPU tier, active models, TTS
backend, WAMP router state, draft-gate decision rationale) so the LLM
can answer "what model is running?", "why is chat slow?", "do I have
a GPU?", "is speculation on?" using real live data instead of
hallucinating.

Every function in this module:
  1. Calls the LOCAL Nunba HTTP API (http://127.0.0.1:5000) — the
     authoritative source of runtime state.  No direct module imports
     from Nunba internals, so this works in both in-process (agent
     running inside Flask) and cross-process (agent running in its
     own Python) deployments.
  2. Returns a dict with both structured fields AND a natural-language
     summary in `summary` — so the LLM can quote the summary verbatim
     for conversational replies, or read structured fields for
     follow-up logic.
  3. Has a short timeout (3s) — if the local Flask is down, returns
     `{available: False, reason: ...}` instead of hanging the agent.

Dual registration:
  - LangChain: `get_langchain_tools()` wraps each function as a
    `langchain.tools.Tool` — importable from Nunba's langchain path.
  - AutoGen:   `register_autogen(agent, user_proxy)` calls
    `autogen.register_function` for each.

Extension pattern:
  - To add a new introspection endpoint, write a small Python function
    here, append it to `_TOOL_FUNCTIONS`.  Both loaders pick it up
    automatically — no two-sided wiring.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_NUNBA_BASE = os.environ.get(
    'NUNBA_BASE_URL', 'http://127.0.0.1:5000',
).rstrip('/')
_TIMEOUT = 3.0


# ═══════════════════════════════════════════════════════════════════
# Low-level HTTP helper
# ═══════════════════════════════════════════════════════════════════

def _get(path: str) -> Dict[str, Any]:
    """GET a Nunba admin endpoint.  Returns `{available: False, ...}` on
    timeout / connection error so callers never hang + never raise into
    the LLM's reasoning loop."""
    url = f"{_NUNBA_BASE}{path}"
    try:
        r = requests.get(url, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json()
        return {
            'available': False,
            'status_code': r.status_code,
            'reason': f"GET {path} returned {r.status_code}",
        }
    except requests.exceptions.ConnectionError:
        return {'available': False, 'reason': f"Nunba Flask not reachable at {_NUNBA_BASE}"}
    except requests.exceptions.Timeout:
        return {'available': False, 'reason': f"Nunba {path} timed out after {_TIMEOUT}s"}
    except Exception as e:
        return {'available': False, 'reason': f"GET {path} failed: {e!s}"}


def _summarize_gpu(h: Dict[str, Any]) -> str:
    """Translate `/backend/health` payload into a one-line English."""
    if h.get('available') is False:
        return "GPU status unavailable (backend not reachable)."
    tier = h.get('gpu_tier', 'unknown')
    name = h.get('gpu_name', 'unknown GPU')
    total = h.get('vram_total_gb', 0) or 0
    free = h.get('vram_free_gb', 0) or 0
    spec = 'on' if h.get('speculation_enabled') else 'off'
    tier_human = {
        'ultra':    'Ultra (≥24GB) — every backend fits concurrently',
        'full':     'Full (≥10GB) — speculative decoding unlocked for all languages',
        'standard': 'Standard (4-10GB) — heavy model only, except cohort fast-path',
        'none':     'No GPU (<4GB or CUDA unavailable) — CPU-only',
    }.get(tier, tier)
    if total > 0:
        return (
            f"{tier_human}.  Device: {name}, {total:.1f}GB VRAM, "
            f"{free:.1f}GB free.  Speculative decoding: {spec}."
        )
    return f"{tier_human}.  Device: {name}.  Speculative decoding: {spec}."


# ═══════════════════════════════════════════════════════════════════
# Public tool functions (registered to langchain + autogen)
# ═══════════════════════════════════════════════════════════════════

def get_gpu_tier() -> Dict[str, Any]:
    """Report the GPU tier + VRAM + whether speculative decoding is active.

    Use this when the user asks: "do I have a GPU?", "why is chat
    slow?", "is speculative decoding on?", "how much VRAM do I have?",
    "what tier am I?".
    """
    h = _get('/backend/health')
    return {
        'gpu_tier': h.get('gpu_tier'),
        'gpu_name': h.get('gpu_name'),
        'vram_total_gb': h.get('vram_total_gb'),
        'vram_free_gb': h.get('vram_free_gb'),
        'cuda_available': h.get('cuda_available'),
        'speculation_enabled': h.get('speculation_enabled'),
        'available': h.get('available', True),
        'summary': _summarize_gpu(h),
    }


def list_running_models() -> Dict[str, Any]:
    """List all models the system knows about + their load state.

    Use this when the user asks: "what models are installed?", "what
    language model is running?", "what TTS voice am I using?", "do I
    have vision?".
    """
    h = _get('/api/admin/models')
    if h.get('available') is False:
        return {'available': False, 'summary': h.get('reason', 'models API unreachable'), 'models': []}
    models = h.get('models') or h.get('data') or []
    loaded = [m for m in models if m.get('loaded') or m.get('status') == 'loaded']
    lines: List[str] = []
    lines.append(f"{len(models)} model(s) registered, {len(loaded)} currently loaded.")
    for m in loaded[:15]:
        name = m.get('name') or m.get('id') or '?'
        mtype = m.get('model_type') or m.get('type') or '?'
        lang = ','.join(m.get('lang_priority') or []) or 'any'
        lines.append(f"  - {name} ({mtype}, lang={lang})")
    return {
        'available': True,
        'total_count': len(models),
        'loaded_count': len(loaded),
        'loaded': loaded,
        'all': models,
        'summary': '\n'.join(lines),
    }


def get_tts_status() -> Dict[str, Any]:
    """Report the active TTS backend + language ladder + fallback chain.

    Use this when the user asks: "what voice am I using?", "why does
    Tamil sound weird?", "why is the voice robotic?", "which TTS
    engines are loaded?".
    """
    h = _get('/api/admin/tts/status')
    if h.get('available') is False:
        # fallback: derive from /backend/health if tts/status endpoint
        # isn't live yet
        gh = _get('/backend/health')
        return {
            'available': False,
            'summary': (
                'TTS status endpoint unavailable.  '
                + (f"GPU state: {_summarize_gpu(gh)}" if gh.get('available') is not False else '')
            ),
        }
    active = h.get('active_backend') or '?'
    lang = h.get('language') or h.get('preferred_lang') or 'en'
    ladder = h.get('ladder') or []
    return {
        'available': True,
        'active_backend': active,
        'language': lang,
        'ladder': ladder,
        'summary': (
            f"Active TTS backend: {active} for language '{lang}'.  "
            f"Fallback ladder: {' → '.join(ladder) if ladder else '(unknown)'}."
        ),
    }


def get_tier_thresholds() -> Dict[str, Any]:
    """Return the canonical GPU tier threshold table.

    Use this when the user asks: "what's the difference between
    standard and full tier?", "what GPU do I need for speculation?",
    "at what VRAM does Indic Parler load?".
    """
    h = _get('/api/v1/system/tiers')
    if h.get('available') is False:
        # static fallback — must mirror core/gpu_tier.py TIER_THRESHOLDS
        return {
            'available': True,
            'source': 'fallback',
            'tiers': [
                {'name': 'ultra',    'min_vram_gb': 24, 'description': 'Every backend fits concurrently'},
                {'name': 'full',     'min_vram_gb': 10, 'description': 'Speculative decoding for all languages'},
                {'name': 'standard', 'min_vram_gb': 4,  'description': 'Heavy model only, cohort fast-path for English+Kokoro/Piper'},
                {'name': 'none',     'min_vram_gb': 0,  'description': 'CPU-only'},
            ],
            'summary': 'Tier thresholds: ultra≥24GB, full≥10GB, standard≥4GB, none<4GB.',
        }
    tiers = h.get('tiers') or []
    lines = ['GPU tier thresholds:']
    for t in tiers:
        lines.append(f"  - {t.get('name')}: ≥{t.get('min_vram_gb')}GB — {t.get('description', '')}")
    return {**h, 'summary': '\n'.join(lines)}


def get_boot_decision() -> Dict[str, Any]:
    """Report why the current draft-gate / speculation state was chosen.

    Reads the last line of `~/Documents/Nunba/logs/draft_decision.jsonl`
    (written by `LlamaConfig.should_boot_draft` — commit 12c9304).

    Use when the user asks: "why is speculation off on my 8GB GPU?",
    "why didn't Nunba load the draft model?", "what's the cohort
    fast-path?".
    """
    import json
    from pathlib import Path
    log_path = Path.home() / 'Documents' / 'Nunba' / 'logs' / 'draft_decision.jsonl'
    if not log_path.exists():
        return {
            'available': False,
            'summary': (
                "Draft decision log not yet written — this usually means "
                "Nunba has not been booted since the cohort-aware gate "
                "landed (commit 12c9304), or log directory is missing."
            ),
        }
    try:
        with log_path.open(encoding='utf-8') as f:
            lines = [line for line in f if line.strip()]
        if not lines:
            return {'available': False, 'summary': 'Draft decision log empty.'}
        last = json.loads(lines[-1])
        return {
            'available': True,
            'decision': last.get('decision'),
            'reason': last.get('reason'),
            'lang': last.get('lang'),
            'vram_total_gb': last.get('vram_total_gb'),
            'vram_free_gb': last.get('vram_free_gb'),
            'active_tts': last.get('active_tts'),
            'ts': last.get('ts'),
            'summary': (
                f"Last boot decision (ts={last.get('ts')}): "
                f"{last.get('decision')} — reason: {last.get('reason')}.  "
                f"Context: lang={last.get('lang')}, "
                f"VRAM={last.get('vram_total_gb')}GB total / "
                f"{last.get('vram_free_gb')}GB free, "
                f"active_tts={last.get('active_tts')}."
            ),
        }
    except Exception as e:
        return {
            'available': False,
            'summary': f"Could not parse draft decision log: {e!s}",
        }


# ═══════════════════════════════════════════════════════════════════
# Decision-logic RAG — agent reads its own code
# ═══════════════════════════════════════════════════════════════════
#
# Curated registry of "why did Nunba do X?" questions → the exact
# module + symbol that decides it.  `explain_decision(topic)` uses
# inspect.getsource() to return the live source, so the agent can
# explain itself by quoting the actual rules, not a copy-pasted
# paraphrase that drifts over time.

_DECISION_REGISTRY: Dict[str, Dict[str, str]] = {
    'draft_gate': {
        'question': "Why did Nunba enable/disable the draft (0.8B) speculative-decoding model?",
        'module': 'llama.llama_config',
        'symbol': 'should_boot_draft',
        'description': (
            "Cohort-aware VRAM gate.  ≥10GB → always on.  8-10GB → on "
            "ONLY when lang=en AND active_tts ∈ {kokoro,piper} (the "
            "cohort fast-path, commit 12c9304).  <8GB or Indic/heavy-TTS "
            "users in 8-10GB band → off so voice has VRAM headroom."
        ),
    },
    'tts_lang_ladder': {
        'question': "Which TTS engine does Nunba pick for a given language?",
        'module': 'tts.tts_engine',
        'symbol': '_FALLBACK_LANG_ENGINE_PREFERENCE',
        'description': (
            "Per-language engine preference list.  Engine chosen by "
            "walking the list and taking the first one that fits in "
            "current VRAM.  English: Chatterbox Turbo → F5 → Indic "
            "Parler → Kokoro → Piper.  Indic langs: Indic Parler only."
        ),
    },
    'tts_lang_capability': {
        'question': "Which backends can actually speak a given language (vs mumble the wrong phonemes)?",
        'module': 'tts.tts_engine',
        'symbol': '_LANG_CAPABLE_BACKENDS',
        'description': (
            "Safety allowlist: if no capable backend fits, synth returns "
            "None and publishes com.hertzai.hevolve.tts.lang_unsupported "
            "instead of letting CosyVoice mumble Tamil in English "
            "phonemes (commit 9064554)."
        ),
    },
    'gpu_tier_thresholds': {
        'question': "What GPU VRAM tiers does Nunba recognize?",
        'module': 'core.gpu_tier',
        'symbol': 'TIER_THRESHOLDS',
        'description': (
            "Canonical tier table.  ultra≥24GB, full≥10GB, standard≥4GB, "
            "none<4GB.  Single source of truth consumed by backend "
            "/backend/health AND frontend GpuTierBadge via /api/v1/"
            "system/tiers (architect refactor 57e820b)."
        ),
    },
    'mcp_auth': {
        'question': "How does Nunba authenticate MCP /api/mcp/local requests?",
        'module': 'integrations.mcp.mcp_http_bridge',
        'symbol': '_mcp_auth_gate',
        'description': (
            "Bearer token from %LOCALAPPDATA%/Nunba/mcp.token (or "
            "HARTOS_MCP_TOKEN env).  /health open, /tools/list loopback-"
            "ok, /tools/execute requires bearer.  HARTOS_MCP_DISABLE_AUTH "
            "=1 bypasses for air-gapped deploys (commits f5b99d8, 49d829d)."
        ),
    },
    'hf_install_gates': {
        'question': "Why did Nunba reject an HF model install?",
        'module': 'main',
        'symbol': 'admin_models_hub_install',
        'description': (
            "4 supply-chain gates: (1) NFKC-normalize hf_id + reject "
            "non-ASCII (homoglyph defense), (2) trusted-org allowlist "
            "(unknown orgs need confirm_unverified=true), (3) 5s timeout "
            "on list_repo_files, (4) reject pickle-only repos — require "
            "safetensors variant (commits 7b0e312, 86c44aa, 48d6752)."
        ),
    },
    'hub_allowlist': {
        'question': "Which HF organizations does Nunba trust by default?",
        'module': 'core.hub_allowlist',
        'symbol': 'HubAllowlist',
        'description': (
            "Runtime-editable list at ~/.nunba/hub_allowlist.json. "
            "Default seeded from code (google, microsoft, Qwen, "
            "ai4bharat, etc.).  Admin API: GET/POST/DELETE "
            "/api/admin/hub/allowlist (architect refactor 48d6752)."
        ),
    },
    'vram_swap': {
        'question': "Why did Nunba evict an idle model?",
        'module': 'integrations.service_tools.model_lifecycle',
        'symbol': 'request_swap',
        'description': (
            "Pressure-eviction: when requested model can't fit, evict "
            "oldest idle non-ACTIVE non-LLM worker and retry load.  Guards "
            "against evicting pinned models (draft 0.8B) and active "
            "inferences (commit fe45daf)."
        ),
    },
    'language_detection': {
        'question': "How does Nunba decide what language the user is speaking?",
        'module': 'hart_intelligence_entry',
        'symbol': '_read_preferred_lang',
        'description': (
            "Reads ~/Documents/Nunba/data/hart_language.json written by "
            "the frontend language selector.  Falls back to 'en' if "
            "missing.  Passed through to whisper.transcribe(language=) "
            "on STT path so short Tamil utterances aren't misrouted as "
            "English (commit 07da0fb)."
        ),
    },
    'watchdog': {
        'question': "What happens if a HARTOS daemon freezes?",
        'module': 'security.node_watchdog',
        'symbol': 'NodeWatchdog',
        'description': (
            "Per-thread heartbeat monitor.  If heartbeat missed for "
            ">threshold (default 300s), dumps all thread stacks via "
            "core.diag, marks thread 'frozen', restarts.  Caps at 5 "
            "restarts in 5min then marks dormant (commit eb05d0f)."
        ),
    },
    'wamp_lifecycle': {
        'question': "When does Nunba start the WAMP router?",
        'module': 'wamp_router',
        'symbol': 'ensure_wamp_running',
        'description': (
            "Deferred-start: NOT at boot (saves ~100MB).  Started on "
            "first non-web channel activation OR first peer upgrade.  "
            "Protected by threading.Lock so concurrent ensure calls can't "
            "double-start (commits 48854dc, 852f4ac, 1a8c8e6)."
        ),
    },
}


def explain_decision(topic: str = '') -> Dict[str, Any]:
    """Return the SOURCE CODE of a decision-making function/variable so
    the agent can explain itself by quoting the live rules, not a stale
    paraphrase.

    Use this when the user asks "why did you do X?", "explain your
    reasoning for Y", "show me the logic that decides Z".  Covers the
    major decision points: draft gate, TTS ladder, GPU tiers, MCP
    auth, HF gates, VRAM eviction, language detection, watchdog, WAMP.

    If `topic` matches a known key (see list_decisions) returns the
    full source.  If `topic` is empty, returns the list of all topics
    so the agent can pick the right one for the user's question.
    """
    import inspect
    import importlib

    topic = (topic or '').strip().lower()
    if not topic:
        return {
            'available': True,
            'topics': sorted(_DECISION_REGISTRY.keys()),
            'summary': (
                'Known decision topics: '
                + ', '.join(sorted(_DECISION_REGISTRY.keys()))
                + '.  Call explain_decision(topic=<name>) to get the '
                  'source code + rationale.'
            ),
        }

    # Exact match first, then fuzzy prefix match
    entry = _DECISION_REGISTRY.get(topic)
    if not entry:
        for k in _DECISION_REGISTRY:
            if k.startswith(topic) or topic in k:
                entry = _DECISION_REGISTRY[k]
                topic = k
                break

    if not entry:
        return {
            'available': False,
            'summary': (
                f"Unknown decision topic '{topic}'.  Known topics: "
                + ', '.join(sorted(_DECISION_REGISTRY.keys()))
            ),
        }

    # Try to import the module and grab the source
    try:
        mod = importlib.import_module(entry['module'])
        obj = getattr(mod, entry['symbol'], None)
        if obj is None:
            return {
                'available': False,
                'question': entry['question'],
                'description': entry['description'],
                'module': entry['module'],
                'symbol': entry['symbol'],
                'summary': (
                    f"Module '{entry['module']}' imported but symbol "
                    f"'{entry['symbol']}' not found.  Description: "
                    + entry['description']
                ),
            }
        try:
            src = inspect.getsource(obj)
        except (OSError, TypeError):
            # Variable (not a function) — render its repr
            src = f"{entry['symbol']} = {obj!r}"
        # Truncate to keep LLM context usable
        if len(src) > 4000:
            src = src[:4000] + '\n... (truncated)'
        return {
            'available': True,
            'topic': topic,
            'question': entry['question'],
            'description': entry['description'],
            'module': entry['module'],
            'symbol': entry['symbol'],
            'source': src,
            'summary': (
                f"{entry['question']}\n\n"
                f"Rationale: {entry['description']}\n\n"
                f"Source ({entry['module']}.{entry['symbol']}):\n{src}"
            ),
        }
    except ImportError as e:
        return {
            'available': False,
            'question': entry['question'],
            'description': entry['description'],
            'summary': (
                f"Cannot import {entry['module']}: {e!s}.  "
                f"Rationale-only: {entry['description']}"
            ),
        }


def list_decisions() -> Dict[str, Any]:
    """List all decision topics the agent can explain via
    explain_decision().  Use this when the user asks a general "how
    does Nunba decide X?" question and you need to pick the right
    topic."""
    return {
        'available': True,
        'topics': [
            {'name': k, 'question': v['question'], 'description': v['description']}
            for k, v in sorted(_DECISION_REGISTRY.items())
        ],
        'summary': (
            f"{len(_DECISION_REGISTRY)} decision topic(s):\n"
            + '\n'.join(
                f"  - {k}: {v['question']}"
                for k, v in sorted(_DECISION_REGISTRY.items())
            )
        ),
    }


def get_system_health() -> Dict[str, Any]:
    """Top-level system health — combines GPU tier + Flask liveness.

    Use when the user asks: "is Nunba healthy?", "what's broken?",
    "can you diagnose why X isn't working?".
    """
    gpu = get_gpu_tier()
    hb = _get('/health')
    flask_ok = hb.get('available') is not False
    parts = [f"Nunba Flask: {'up' if flask_ok else 'down'}."]
    parts.append(gpu.get('summary', ''))
    # If we can, add counts
    models = _get('/api/admin/models')
    if models.get('available') is not False:
        m = models.get('models') or models.get('data') or []
        loaded = [x for x in m if x.get('loaded') or x.get('status') == 'loaded']
        parts.append(f"{len(m)} models registered, {len(loaded)} loaded.")
    return {
        'flask_ok': flask_ok,
        'gpu': gpu,
        'summary': '  '.join(p for p in parts if p),
    }


# ═══════════════════════════════════════════════════════════════════
# Registry + dual-loader
# ═══════════════════════════════════════════════════════════════════

_TOOL_FUNCTIONS: List[Callable[..., Dict[str, Any]]] = [
    get_gpu_tier,
    list_running_models,
    get_tts_status,
    get_tier_thresholds,
    get_boot_decision,
    get_system_health,
    list_decisions,     # agent picks the right topic for user's "why" question
    explain_decision,   # agent reads its own source code (code-RAG)
]


def get_tool_functions() -> List[Callable[..., Dict[str, Any]]]:
    """Canonical list for anyone who wants the raw functions."""
    return list(_TOOL_FUNCTIONS)


def get_langchain_tools() -> List[Any]:
    """Wrap each introspect function as a LangChain Tool.

    Returns `[]` if langchain isn't importable — no-op for non-agent
    deployments.  Each Tool's `name` matches the function name so agent
    prompts can reference them directly ("call get_gpu_tier").
    """
    try:
        from langchain_core.tools import Tool
    except ImportError:
        try:
            from langchain.agents import Tool  # type: ignore
        except ImportError:
            logger.debug("langchain not importable — system_introspect tools unavailable")
            return []

    import inspect as _inspect

    tools = []
    for fn in _TOOL_FUNCTIONS:
        # Functions that take a real argument (explain_decision(topic))
        # get the LangChain string passed through as the first positional
        # arg.  Argless functions ignore it.  Introspection-based so
        # future additions auto-pick the right calling convention.
        _sig = _inspect.signature(fn)
        _takes_arg = any(
            p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
            and p.default is not p.empty
            for p in _sig.parameters.values()
        )

        def _make_runner(_fn: Callable, _takes: bool) -> Callable[[str], str]:
            def _run(query: str = '') -> str:
                result = _fn(query) if _takes and query else _fn()
                return result.get('summary') or str(result)
            _run.__name__ = _fn.__name__
            return _run

        tools.append(Tool(
            name=fn.__name__,
            func=_make_runner(fn, _takes_arg),
            description=(fn.__doc__ or fn.__name__).strip().split('\n')[0],
        ))
    return tools


def register_autogen(assistant_agent: Any, user_proxy_agent: Any) -> int:
    """Register every introspect function with an autogen agent pair.

    autogen's `register_function` wires the agent's tool-calling layer
    to the Python callable.  We register the `caller` (assistant) so it
    CAN call, and the `executor` (user proxy) so it RUNS the call.

    Returns count registered.  Silent no-op if autogen isn't importable.
    """
    try:
        from autogen import register_function
    except ImportError:
        logger.debug("autogen not importable — system_introspect tools unavailable")
        return 0
    count = 0
    for fn in _TOOL_FUNCTIONS:
        try:
            register_function(
                fn,
                caller=assistant_agent,
                executor=user_proxy_agent,
                name=fn.__name__,
                description=(fn.__doc__ or fn.__name__).strip().split('\n')[0],
            )
            count += 1
        except Exception as e:
            logger.warning(f"autogen register_function failed for {fn.__name__}: {e}")
    return count


__all__ = [
    'get_gpu_tier',
    'list_running_models',
    'get_tts_status',
    'get_tier_thresholds',
    'get_boot_decision',
    'get_system_health',
    'list_decisions',
    'explain_decision',
    'get_tool_functions',
    'get_langchain_tools',
    'register_autogen',
]
