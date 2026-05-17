"""
Unified Agent Goal Engine - Speculative Dispatcher

Fast-first, expert-takeover speculative execution:
1. Fast model (hive compute / cheap API) responds synchronously → user sees instantly
2. Expert model (GPT-4 / Claude) runs in background thread
3. Fast response conveyed to expert as context
4. If expert meaningfully improves, delivered asynchronously
5. Compute provider (hive node) earns ad revenue for serving fast response

Guardrails enforced at EVERY layer:
- ConstitutionalFilter.check_prompt() before ANY dispatch
- HiveCircuitBreaker.is_halted() before ANY dispatch
- EnergyAwareness tracked on EVERY model call
- ComputeDemocracy.adjusted_reward() on EVERY contribution
- HiveEthos.rewrite_prompt_for_togetherness() on EVERY prompt
- Budget enforcement via ResonanceService.spend_spark()
"""
import atexit
import json
import logging
import os
import re
import time
import uuid
import threading
from collections import deque

from core.port_registry import get_port
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

logger = logging.getLogger('hevolve_social')

# Similarity threshold — below this, expert response is considered
# a meaningful improvement over fast response
_SIMILARITY_THRESHOLD = 0.80
_RESPONSE_ADEQUATE = 'RESPONSE_ADEQUATE'

# Minimum draft confidence required to commit the draft's reply as the
# FINAL answer (delegate="none" path). Below this we schedule an expert
# verification in the background regardless of what the draft said —
# reasoning quality must never regress just because the 0.8B thought
# it could handle the question. A confident "none" still takes the
# fast path; an unsure "none" is treated as a quiet "local".
_DRAFT_CONFIDENCE_FLOOR = 0.85

# Refusal patterns the draft must NEVER emit.  See the role-contract
# block in `_build_draft_classifier_prompt` — the draft is the
# first-responder, NOT the authority on system capability.  Any reply
# that asserts the system can't do something is a prompt-following
# failure (or the model slipped a refusal through the system prompt)
# and must be replaced with a standby + escalation to the expert.
#
# Targets HIGH-CONFIDENCE capability refusals only — not legitimate
# negative phrasing like "I don't know the answer".  Match shape:
# "I" + negation + (capability noun OR system-action verb).  Word
# boundaries + IGNORECASE guard against false positives like
# "I cannot wait to help".
# Capability verbs the draft might falsely claim it can't perform.
# Factored out so the refusal-pattern alternatives stay in sync.
_REFUSAL_VERBS = (
    r"access|fetch|reach|browse|connect|connect to|read|retrieve|"
    r"download|verify|view|see|check|crawl|open|load|hit|resolve|"
    r"directly|currently|presently"
)
# Capability nouns paired with "I do(n't| not) have <NOUN>" or
# "I lack <NOUN>" / "I have no <NOUN>" — anything that frames an
# absent capability rather than negative recall ("I don't know").
_REFUSAL_NOUNS = (
    r"access|tools?|the ability|the capability|permission|a way|any way|"
    r"built-?in|external|the internet|web access|internet access|"
    r"means|way to"
)
_REFUSAL_PATTERN = re.compile(
    r"\b(?:"
    # "I cannot/can't <optional softener> <verb>"
    r"I (?:can'?t|cannot)\s+"
    r"(?:directly\s+|currently\s+|presently\s+)?"
    r"(?:" + _REFUSAL_VERBS + r")"
    r"|"
    # "I am unable/not able TO <optional softener> <verb>"
    # and contraction "I'm unable/not able TO <verb>"
    # (regex split because "I'm" has no space between I and m,
    # while "I am" requires the space)
    r"(?:I'?m|I am)\s+(?:unable|not able)\s+to\s+"
    r"(?:directly\s+|currently\s+|presently\s+)?"
    r"(?:" + _REFUSAL_VERBS + r")"
    r"|"
    # "I (don't|do not) have <noun>"  e.g. "I don't have built-in tools"
    r"I do(?:n'?t| not) have\s+"
    r"(?:" + _REFUSAL_NOUNS + r")"
    r"|"
    # "I lack <noun>"  e.g. "I lack access to GitHub"
    r"I lack\s+(?:" + _REFUSAL_NOUNS + r")"
    r"|"
    # "I have no <noun>"  e.g. "I have no tools to retrieve…"
    r"I have no\s+(?:access|way|tools?|ability|means)"
    r"|"
    # "I'm just/only a (large language model|LLM|AI|chatbot|…)"
    # — split for I'm vs I am as above.
    r"(?:I'?m|I am) (?:just|only) (?:a|an) "
    r"(?:large language model|LLM|AI|language model|chatbot|"
    r"text-based assistant)"
    r")",
    re.IGNORECASE,
)

# Generic standby reply substituted when the draft slips a refusal
# through. Keeps the user comfortable while the expert path runs.
# Intentionally short and capability-neutral — the expert's actual
# answer will replace this within the latency budget.
_REFUSAL_STANDBY_REPLY = "Let me check that for you…"


def _capability_summary_safe() -> str:
    """Return the runtime capability summary for the draft prompt, or
    an empty string when the helper itself can't be imported.

    Lazy import — tool_allowlist pulls model_registry / ModelCatalog /
    MCP / channel subsystems on first call.  In bare unit-test envs
    those aren't loaded, so we treat any failure as 'no summary' and
    let the rest of the prompt carry on.  Empty string is the signal
    the f-string in _build_draft_classifier_prompt skips the section.
    """
    try:
        from integrations.agent_engine.tool_allowlist import (
            get_capability_summary,
        )
        return get_capability_summary() or ""
    except Exception:
        return ""


class SpeculativeDispatcher:
    """Fast-first, expert-takeover speculative execution engine.

    Every method enforces guardrails — no code path bypasses safety.
    """

    def __init__(self, model_registry=None):
        from .model_registry import model_registry as _default_registry
        self._registry = model_registry or _default_registry
        self._expert_pool = ThreadPoolExecutor(
            max_workers=int(os.environ.get('HEVOLVE_EXPERT_WORKERS', '4')),
            thread_name_prefix='spec_expert',
        )
        atexit.register(lambda: self._expert_pool.shutdown(wait=False))
        self._active: Dict[str, dict] = {}  # speculation_id → metadata
        self._lock = threading.Lock()
        self._results: Dict[str, dict] = {}  # speculation_id → expert result
        self._results_max = 1000  # evict oldest when exceeded
        # HiveMind fusion consult results (speculation_id → {result, ts}).
        # Populated by the best-effort `_schedule_hive_consult` fired when
        # the user selected `intelligence_preference='hive_preferred'` AND
        # the draft self-delegated to hive.  Read by observers / tests —
        # does NOT feed back into the chat reply path (non-blocking; the
        # hot-path latency budget is preserved).  Capped at 1000 entries
        # via the same eviction helper as `_results`.
        self._last_hive_consult: Dict[str, dict] = {}

    # ─── Gate: should we speculate? ───

    def should_speculate(self, user_id: str, prompt_id: str,
                         prompt: str, goal: dict = None) -> bool:
        """Gate: expert model available + budget remaining + not halted + not casual."""
        # GUARDRAIL: circuit breaker
        from security.hive_guardrails import HiveCircuitBreaker
        if HiveCircuitBreaker.is_halted():
            return False

        # GUARDRAIL: constitutional check on prompt
        from security.hive_guardrails import ConstitutionalFilter
        passed, _ = ConstitutionalFilter.check_prompt(prompt)
        if not passed:
            return False

        # Need both a fast and expert model
        fast = self._registry.get_fast_model()
        expert = self._registry.get_expert_model()
        if not fast or not expert:
            return False
        if fast.model_id == expert.model_id:
            return False  # Same model — no point speculating

        # Budget check (if goal has spark budget)
        if goal and goal.get('spark_budget', 0) > 0:
            spent = goal.get('spark_spent', 0)
            remaining = goal['spark_budget'] - spent
            # Estimate ~4k tokens per speculation (prompt + response)
            if remaining < expert.cost_per_1k_tokens * 4:
                return False

        return True

    # ─── Main entry point ───

    def dispatch_speculative(self, prompt: str, user_id: str, prompt_id: str,
                             goal_id: str = None, goal_type: str = 'general',
                             node_id: str = None) -> dict:
        """
        1. Guardrail-check the prompt
        2. Pick fast model → dispatch synchronously → user gets response
        3. Record compute contribution for hive node (ad revenue)
        4. Pick expert model → dispatch in background thread
        5. Return fast response immediately

        Returns:
            {
                'response': str,           # Fast agent's response
                'speculation_id': str,     # Track the background expert
                'fast_model': str,         # Which model served fast
                'expert_pending': bool,    # True if expert is working
                'latency_ms': float,       # Fast response latency
                'energy_kwh': float,       # Energy consumed
            }
        """
        speculation_id = str(uuid.uuid4())[:12]

        # GUARDRAIL: circuit breaker
        from security.hive_guardrails import HiveCircuitBreaker
        if HiveCircuitBreaker.is_halted():
            return {'response': '', 'speculation_id': speculation_id,
                    'error': 'Hive is halted', 'expert_pending': False}

        # GUARDRAIL: constitutional filter
        from security.hive_guardrails import ConstitutionalFilter
        passed, reason = ConstitutionalFilter.check_prompt(prompt)
        if not passed:
            return {'response': '', 'speculation_id': speculation_id,
                    'error': reason, 'expert_pending': False}

        # GUARDRAIL: rewrite prompt for togetherness
        from security.hive_guardrails import HiveEthos
        prompt = HiveEthos.rewrite_prompt_for_togetherness(prompt)

        # ── FAST PATH ──
        fast_model = self._registry.get_fast_model()
        if not fast_model:
            return {'response': '', 'speculation_id': speculation_id,
                    'error': 'No fast model available', 'expert_pending': False}

        start = time.time()
        fast_response = self._dispatch_to_model(
            fast_model, prompt, user_id, prompt_id, goal_type, goal_id)
        elapsed_ms = (time.time() - start) * 1000
        self._track_call_telemetry(fast_model, elapsed_ms, node_id)

        # ── EXPERT PATH (background) ──
        expert_model = self._registry.get_expert_model()
        expert_pending = self._schedule_expert_background(
            speculation_id=speculation_id,
            prompt=prompt,
            fast_response=fast_response,
            expert_model=expert_model,
            user_id=user_id, prompt_id=prompt_id,
            goal_id=goal_id, goal_type=goal_type,
            origin_model_id=fast_model.model_id,
            origin_model_role='fast_model',
        )

        return {
            'response': fast_response,
            'speculation_id': speculation_id,
            'fast_model': fast_model.model_id,
            'expert_pending': expert_pending,
            'latency_ms': round(elapsed_ms, 1),
            'energy_kwh': round(
                self._registry.get_total_energy_kwh(hours=0.01), 6),
        }

    # ─── Draft-first dispatch (Qwen3.5-0.8B standby + delegate signal) ───

    def dispatch_draft_first(self, prompt: str, user_id: str, prompt_id: str,
                             goal_id: str = None, goal_type: str = 'general',
                             node_id: str = None,
                             agent_persona: Optional[str] = None,
                             preferred_lang: str = 'en',
                             user_pref: str = 'auto',
                             agent_bound: bool = False) -> dict:
        """Draft-first dispatch: tiny model answers immediately, signals whether
        to delegate.

        Architecture (the piece the user asked for on top of the speculative
        dispatcher):

          1. The DRAFT tier model (Qwen3.5-0.8B) receives a wrapped prompt that
             asks it to emit JSON:
               { "reply": "...",
                 "delegate": "none" | "local" | "hive",
                 "confidence": 0.0-1.0 }
             `delegate` is the draft's self-assessment of its place in the
             hierarchy: can it answer, or should a bigger model take over?
          2. Regardless of the delegate signal, the draft's ``reply`` is
             returned SYNCHRONOUSLY as a standby response — the user sees
             something within ~300ms even when delegation is needed.
          3. When ``delegate != "none"`` (or the JSON can't be parsed), a
             background expert task runs on the local FAST tier or is
             dispatched to the hive — same code path as dispatch_speculative.
          4. Both the draft's reply AND the eventual expert reply get fed
             through ``WorldModelBridge.record_interaction`` with distinct
             model_id tags so HevolveAI's continual learner can distill
             the expert's improvements back into the draft over time.

        Guardrails: the outer /chat handler already ran GuardrailEnforcer +
        prompt_guard before calling us, so we only re-check constitutional
        filter (cheap) and circuit breaker here. Budget + hive_ethos still
        apply on the expert path via dispatch_speculative's helpers.

        Returns a dict shaped like dispatch_speculative's response plus
        ``delegate``, ``draft_model``, and ``draft_confidence`` fields so
        callers can discriminate.
        """
        speculation_id = str(uuid.uuid4())[:12]

        # ── 1. Local preconditions (cheapest first, so a missing registry
        # entry never triggers an unnecessary network probe).
        draft_model = self._registry.get_draft_model()
        if draft_model is None:
            return {
                'response': '', 'speculation_id': speculation_id,
                'delegate': 'none', 'error': 'no_draft_model',
                'expert_pending': False,
            }

        # ── Language guard: skip 0.8B draft for non-Latin scripts ──
        # Qwen3.5-0.8B-UD-Q4_K_XL has weak Unicode coverage for the
        # scripts listed in core.constants.NON_LATIN_SCRIPT_LANGS —
        # falls back to Latin-transliterated output ("Vanakkam! Nan
        # ungal nanban..." for Tamil) which is unusable for TTS + UX.
        # The 4B main has native Unicode coverage.  Canonical set lives
        # in core.constants; this file imports rather than duplicates.
        from core.constants import NON_LATIN_SCRIPT_LANGS
        _lang_key = (preferred_lang or 'en').split('-')[0].lower()
        if _lang_key and _lang_key != 'en' and _lang_key in NON_LATIN_SCRIPT_LANGS:
            logger.info(
                f"Skipping 0.8B draft for preferred_lang={_lang_key!r} "
                f"(weak Unicode script coverage); routing direct to 4B.",
            )
            return {
                'response': '', 'speculation_id': speculation_id,
                'delegate': 'none', 'error': 'draft_skipped_non_english',
                'expert_pending': False,
            }

        # ── 2. Gate checks (constitutional + circuit breaker + draft server probe)
        gate_error = self._check_draft_first_gates(prompt)
        if gate_error is not None:
            return {
                'response': '', 'speculation_id': speculation_id,
                'delegate': 'none', 'error': gate_error, 'expert_pending': False,
            }

        # ── 2. Dispatch the draft with the classifier prompt ──
        draft_prompt = self._build_draft_classifier_prompt(
            prompt, agent_persona=agent_persona, preferred_lang=preferred_lang)
        start = time.time()
        draft_raw = self._dispatch_to_model(
            draft_model, draft_prompt, user_id, prompt_id, goal_type, goal_id)
        draft_latency_ms = (time.time() - start) * 1000
        self._track_call_telemetry(draft_model, draft_latency_ms, node_id)

        # ── 3. Parse envelope + record draft interaction ──
        parsed = self._parse_draft_envelope(draft_raw)
        draft_reply = parsed.get('reply') or draft_raw.strip()[:500]
        delegate = parsed.get('delegate', 'local')  # default on parse fail
        confidence = float(parsed.get('confidence') or 0.0)

        # REFUSAL GUARD: the draft is the first-responder role, NOT the
        # authority on system capability. Any reply that asserts the
        # system can't do something is a prompt-following failure — the
        # role contract in the classifier prompt explicitly forbids
        # refusals of this shape.  When the model slips one through
        # anyway (typically when the user asks for a tool-bound
        # capability the draft can't see — URL fetch, file read,
        # GitHub PR check, etc.), we replace the standby with a generic
        # holding reply and force escalation to the local expert.  The
        # user never sees the refusal; the expert (with full tool
        # access) produces the real answer and the SSE/WAMP fan-out
        # delivers it to replace the standby.
        # Size-agnostic — same rule applies whether the draft slot
        # holds a 0.8B, 4B, or 27B model.  None of them see the full
        # tool registry the expert binds.
        refusal_overridden = False
        if draft_reply and _REFUSAL_PATTERN.search(draft_reply):
            logger.info(
                "draft-first: refusal detected in draft reply "
                "(delegate=%r, conf=%.2f) — replacing with standby + "
                "forcing delegate=local. Original reply prefix: %r",
                delegate, confidence, draft_reply[:120],
            )
            draft_reply = _REFUSAL_STANDBY_REPLY
            delegate = 'local'
            refusal_overridden = True

        # REASONING-QUALITY GUARD: an unsure "none" is not good enough to
        # ship as the final answer. Promote it to "local" so an expert
        # verifier still runs in the background. Keeps the single
        # dispatch path — this is just delegate normalization, no new
        # branch below. Ensures the draft model can never regress the
        # reasoning quality the user gets — worst case they see the
        # draft reply briefly as a standby and it's replaced when the
        # 4B expert finishes via the existing crossbar delivery.
        if delegate == 'none' and confidence < _DRAFT_CONFIDENCE_FLOOR:
            logger.info(
                f"draft-first: low-confidence 'none' ({confidence:.2f} < "
                f"{_DRAFT_CONFIDENCE_FLOOR}) → escalating to local verifier"
            )
            delegate = 'local'

        # AGENT-BINDING GUARD: when the caller bound this turn to a
        # specific agent (prompt_id resolves to a real agent on disk,
        # not the request-id fallback), the user has chosen a
        # specialist and expects THAT specialist's voice — not the
        # 0.8B draft answering in its generic voice.  Even a trivial
        # greeting like "hi" should pass through the specialist so
        # its persona / system prompt / tool registry shapes the
        # reply.  Promote delegate=none → local so the expert path
        # always takes the turn for agent-bound requests.
        #
        # When agent_bound=False (no specific agent in scope, e.g.
        # default chat or guest free-floating), the draft's "none"
        # decision stays — the 0.8B can handle trivial questions
        # without paying the 4B cost.
        if delegate == 'none' and agent_bound:
            logger.info(
                "draft-first: prompt_id=%r is bound to a specific "
                "agent — escalating delegate=none → 'local' so the "
                "agent's expert path takes the turn instead of the "
                "0.8B draft's generic voice.",
                prompt_id,
            )
            delegate = 'local'

        # Non-Latin languages skip draft entirely (hart_intelligence_entry.py)
        # so this code path is only reached for English/Latin-script languages.

        # ── Draft telemetry: log full envelope for offline calibration ──
        # The data scientist requires this to build a confidence calibration
        # curve and detect intent classification drift over time.
        try:
            _telemetry = {
                'speculation_id': speculation_id,
                'user_id': user_id,
                'confidence': confidence,
                'delegate': delegate,
                'is_casual': parsed.get('is_casual'),
                'is_correction': parsed.get('is_correction'),
                'is_create_agent': parsed.get('is_create_agent'),
                'channel_connect': parsed.get('channel_connect'),
                'language_change': parsed.get('language_change'),
                'draft_model': draft_model.model_id if draft_model else None,
                'latency_ms': draft_latency_ms,
                'reply_len': len(draft_reply) if draft_reply else 0,
                'escalated': delegate != parsed.get('delegate', 'local'),
                # refusal_overridden lets us calibrate per-model adherence
                # to the role contract.  A draft model with the right
                # prompt should hit this near-zero — sustained non-zero
                # rate is a signal that either the prompt isn't being
                # followed (model too small / fine-tune mismatch) or the
                # model is the wrong fit for the draft slot on this
                # hardware.
                'refusal_overridden': refusal_overridden,
            }
            logger.info(f"draft-telemetry: {json.dumps(_telemetry)}")
        except Exception:
            pass  # telemetry must never break the hot path

        self._record_interaction_safely(
            user_id=user_id, prompt_id=prompt_id, prompt=prompt,
            response=draft_reply, model_id=draft_model.model_id,
            latency_ms=draft_latency_ms, node_id=node_id, goal_id=goal_id,
        )

        # ── 4. Schedule expert if the draft self-delegated ──
        expert_pending = False
        hive_consult_scheduled = False
        if delegate in ('local', 'hive'):
            expert_model = self._pick_expert_for_delegate(
                delegate, user_pref=user_pref)
            expert_pending = self._schedule_expert_background(
                speculation_id=speculation_id,
                prompt=prompt,
                fast_response=draft_reply,
                expert_model=expert_model,
                user_id=user_id, prompt_id=prompt_id,
                goal_id=goal_id, goal_type=goal_type,
                origin_model_id=draft_model.model_id,
                origin_model_role='draft_model',
                delegate=delegate,
            )
            # When the user explicitly asked for `hive_preferred` AND the
            # draft self-delegated to hive, also fire a best-effort MoE
            # HiveMind fusion consult in the background.  The consult
            # result is stored on self._last_hive_consult for observers
            # and future wiring; it does NOT replace the expert's reply
            # on the hot path (preserves the 1.5s chat budget).  Safe on
            # `auto` and `local_only` paths — gated by user_pref so
            # existing callers see no behavior change.
            if delegate == 'hive' and user_pref == 'hive_preferred':
                hive_consult_scheduled = self._schedule_hive_consult(
                    prompt=prompt,
                    user_id=user_id,
                    speculation_id=speculation_id,
                )

        # Channel name defensively coerced: draft model sometimes emits None,
        # null, or a capitalised string. Normalise to a lowercased str so
        # callers can treat an empty string as "no channel connect intent".
        _channel = parsed.get('channel_connect') or ''
        if not isinstance(_channel, str):
            _channel = ''
        # Language change — same defensive coercion as channel_connect.
        # Validated against the canonical SUPPORTED_LANG_DICT (single
        # source of truth for language codes, lives in hart_intelligence_entry).
        _lang = parsed.get('language_change') or ''
        if not isinstance(_lang, str):
            _lang = ''
        _lang = _lang.strip().lower()[:5]
        if _lang:
            from core.safe_hartos_attr import safe_hartos_attr
            SUPPORTED_LANG_DICT = safe_hartos_attr('SUPPORTED_LANG_DICT')
            if SUPPORTED_LANG_DICT is not None:
                if _lang not in SUPPORTED_LANG_DICT:
                    logger.debug(
                        "draft: language_change '%s' not in "
                        "SUPPORTED_LANG_DICT — ignoring", _lang)
                    _lang = ''
            # else: HARTOS not yet loaded — accept the code as-is, same
            # fall-through the original ImportError branch had.
        return {
            'response': draft_reply,
            'speculation_id': speculation_id,
            'draft_model': draft_model.model_id,
            'delegate': delegate,
            'draft_confidence': confidence,
            'is_correction': bool(parsed.get('is_correction', False)),
            'is_casual': bool(parsed.get('is_casual', False)),
            'is_create_agent': bool(parsed.get('is_create_agent', False)),
            'channel_connect': _channel.strip().lower(),
            'language_change': _lang.strip().lower(),
            'expert_pending': expert_pending,
            # Additive field: observers (J282, telemetry, admin/diag) can
            # tell a hive fusion consult was fired in background.  Legacy
            # callers that don't read this field are unaffected.
            'hive_consult_scheduled': hive_consult_scheduled,
            'latency_ms': round(draft_latency_ms, 1),
            'energy_kwh': round(
                self._registry.get_total_energy_kwh(hours=0.01), 6),
        }

    # ─── SRP helpers extracted from dispatch_draft_first ───

    # Class-level toggle the health probe can flip off in tests. Prod
    # leaves it enabled so dead-port POSTs short-circuit cleanly; unit
    # tests that mock _dispatch_to_model set it to False so the mocked
    # dispatch actually runs. Kept as a class attribute (not instance)
    # so fixtures can patch once for the whole suite.
    _health_probe_enabled: bool = True

    def _check_draft_first_gates(self, prompt: str) -> Optional[str]:
        """Run the cheap gates (circuit breaker + constitutional filter +
        draft-server health probe) that must pass before we spend any
        model time.

        Returns None on success, or an error string identifying which gate
        rejected the request. Keeps dispatch_draft_first's orchestration
        thin — this method owns "is the system healthy enough to proceed".
        """
        from security.hive_guardrails import HiveCircuitBreaker, ConstitutionalFilter
        if HiveCircuitBreaker.is_halted():
            return 'Hive is halted'
        passed, reason = ConstitutionalFilter.check_prompt(prompt)
        if not passed:
            return reason or 'constitutional filter'
        # Fast TCP probe against the draft server. If the 0.8B caption
        # server (port 8081) isn't listening, fall through to the normal
        # 4B path instead of POSTing to a dead port and waiting for a
        # socket timeout on every chat request. Cache the result for 30s
        # so we don't probe on every message.
        if self._health_probe_enabled and not self._draft_server_alive():
            return 'draft_server_offline'
        return None

    _draft_probe_ts: float = 0.0
    _draft_probe_ok: bool = False

    def _draft_server_alive(self) -> bool:
        """Cheap TCP probe against the draft server endpoint. Cached
        for 30s so the dispatcher stays responsive under chat load.
        Returns True if a connect() to the draft host:port succeeds."""
        import socket
        import time as _t
        now = _t.time()
        if now - self._draft_probe_ts < 30.0:
            return self._draft_probe_ok
        ok = False
        try:
            from core.port_registry import get_local_draft_url
            url = get_local_draft_url()
            # http://host:port/v1 → (host, port)
            _body = url.split('://', 1)[-1].split('/', 1)[0]
            host, _, port_s = _body.partition(':')
            port = int(port_s) if port_s else 80
            with socket.create_connection((host, port), timeout=0.5):
                ok = True
        except Exception:
            ok = False
        self.__class__._draft_probe_ts = now
        self.__class__._draft_probe_ok = ok
        return ok

    def _build_draft_classifier_prompt(
        self, user_prompt: str, agent_persona: Optional[str] = None,
        preferred_lang: str = 'en',
    ) -> str:
        """Wrap the user prompt with the draft-first classifier instruction.

        The draft's job is twofold: (a) produce a short standby reply fit
        for simple chat, (b) self-assess whether a bigger model is needed.
        The JSON schema is flat so a 0.8B model can reliably emit it.

        If ``agent_persona`` is provided, it's prepended to the instruction
        so the draft's reply is in the voice of the custom / system agent
        the user is talking to instead of a generic first-responder. Used
        for the Path-2 system-agent case (e.g. Nunba personality agent).

        Owns ONLY prompt construction — no I/O, no side effects.
        """
        # Default brand identity when no explicit persona is supplied.
        # Without this fall-back, the user's "who are you?" turn drops
        # through to the underlying model's training-default name
        # ("I'm Qwen3.5...") because cf3e337 deliberately removed every
        # "You are <internal-role>" sentence to fix an identity-leak
        # where the draft echoed "first-responder" architecture jargon.
        # That fix was correct in spirit but went one step too far —
        # the BRAND identity (the user-facing product name "Nunba") is
        # not architecture jargon and is exactly what the user expects
        # to hear when no per-agent persona is selected.  The
        # ``agent_persona`` branch below overrides this for any turn
        # where a specific persona is in scope, so explicit personas
        # are unaffected.
        #
        # Single source of truth — core.constants.NUNBA_BRAND_IDENTITY.
        # Same constant is imported by Nunba's _fallback_chat in
        # routes/hartos_backend_adapter.py so the two paths can never
        # drift on brand wording.
        from core.constants import NUNBA_BRAND_IDENTITY
        persona_block = f"{NUNBA_BRAND_IDENTITY}\n\n"
        if agent_persona:
            # Cap the persona at ~800 chars so a long system prompt doesn't
            # blow the 0.8B model's context budget on a single-turn call.
            snippet = agent_persona.strip()[:800]
            persona_block = (
                "You are playing the following persona — reply in this "
                "voice, but keep the JSON schema below exactly as "
                "specified. Persona:\n"
                f"{snippet}\n\n"
            )
        lang_block = ''
        if preferred_lang and not preferred_lang.startswith('en'):
            try:
                from core.constants import SUPPORTED_LANG_DICT
                lang_name = SUPPORTED_LANG_DICT.get(preferred_lang[:2], preferred_lang)
            except ImportError:
                lang_name = preferred_lang
            # Same language + tone prompt the 4B path uses (with examples, code-mixing rules)
            _tone = ''
            try:
                from core.agent_personality import get_regional_tone_prompt
                _tone = get_regional_tone_prompt(preferred_lang)
            except Exception:
                pass
            lang_block = (
                f"Answer questions accurately and respond as quickly as possible in {lang_name}. "
                f"Keep responses under 200 words. Be colloquial and natural.\n"
                f"{_tone}\n\n"
            )

        # Compute the runtime capability summary ONCE per prompt build so
        # the static-tools / ModelCatalog / MCP / channel walks don't run
        # twice for the conditional injection below.
        cap_summary = _capability_summary_safe()
        cap_block = (
            f"Available capabilities (the system can do these via the "
            f"routing path below): {cap_summary}.\n\n"
            if cap_summary else ""
        )

        return (
            persona_block
            + lang_block
            # ── Job + answering rules — size-agnostic, identity-free ─────
            # Same wording works whether the draft is 0.8B, 4B, or 27B.
            # The model in this slot does NOT see the full tool registry
            # (web fetch, code exec, GitHub, filesystem, vision, computer
            # control, MCP servers, channels), the user's loaded persona,
            # multi-turn memory, or the ReAct loop — so it must never
            # refuse on behalf of the system.
            #
            # The 3ea8648 prompt opened with "You are a fast local
            # first-responder" and the model would echo it verbatim on
            # "who are you?" → "I'm your fast local first-responder,
            # ready to assist you right away."  cf3e337 fixed that by
            # removing every internal-role identity sentence, but went
            # one step too far — with NO identity at all the 0.8B fell
            # through to its training-default name ("I'm Qwen…").  The
            # default brand identity ("You are Nunba…") now lives in
            # persona_block above, so this section deliberately does
            # NOT add another "You are <X>" line — only the BRAND
            # identity above is allowed; INTERNAL-ROLE jargon
            # ("first-responder", "draft", "classifier") stays out.
            # All instructions below are phrased as the *job* and
            # *rules*, never as architecture identity.
            + "Your job is to produce a short reply to the user AND "
            "classify the user's intent on several independent axes. The "
            "classification flags route the message downstream — be "
            "accurate.\n\n"
            # Positive capability summary — primary teaching mechanism so
            # the model knows what the system CAN do.  Auto-discovered:
            # static tool list + ModelCatalog (TTS/STT/VLM/video/audio,
            # rolled up by type) + MCP servers + channels + expert-agent
            # categories.  Computed once into cap_block above.
            + cap_block
            + "ANSWERING RULES — READ BEFORE REPLYING:\n"
            "You only see this single turn. The system's actual tool / "
            "integration / capability set is dynamic and not visible from "
            "here — so you don't get to decide what the system can or "
            "can't do.  Therefore:\n"
            "- NEVER write 'I cannot', 'I don't have access', 'I'm unable', "
            "'I'm just a', 'I do not have the ability', or any phrase "
            "asserting the system can't do something.\n"
            "- NEVER claim no internet/tools/file access; you have no way "
            "to verify what is or isn't reachable.\n"
            "- You MAY answer directly ONLY for trivial recall, simple "
            "math, greetings, or questions that need NO live data, NO "
            "external system access, NO filesystem, NO code execution, "
            "and NO per-user state beyond this single turn.\n"
            "- For ANYTHING else (URLs, code, files, current state, "
            "multi-step reasoning, capability questions): set "
            "delegate=\"local\" (or \"hive\" for very large requests) "
            "and write a brief standby reply such as \"Let me check that "
            "for you…\", \"Looking that up…\", or \"One moment…\".  The "
            "standby is replaced by the authoritative answer automatically "
            "— your only job is to keep the user comfortable while that "
            "runs.\n"
            "- Refusals are not your call.  If you ever feel the urge to "
            "refuse: pick a standby instead and delegate.\n\n"
            f"User: {user_prompt}\n\n"
            "Respond with ONE JSON object on a single line and NOTHING else:\n"
            '{"reply": "<your short reply to the user, 1-3 sentences>", '
            '"delegate": "none" OR "local" OR "hive", '
            '"confidence": <float 0-1>, '
            '"is_correction": true OR false, '
            '"is_casual": true OR false, '
            '"is_create_agent": true OR false, '
            '"channel_connect": "<channel name or empty string>", '
            '"language_change": "<ISO 639-1 code or empty string>", '
            '"reason": "<why you chose this delegate value>"}\n\n'
            # ── delegate ────────────────────────────────────────────────
            "delegate: Use \"none\" for greetings, small-talk, factual "
            "questions you can fully answer yourself, or anything that needs "
            "no external tools. Use \"local\" if the request needs tools, "
            "code, reasoning, or multi-step work the 4B model can handle. "
            "Use \"hive\" if it needs large-model expertise, long-context "
            "research, or specialized skill distribution.\n\n"
            # ── is_correction ────────────────────────────────────────────
            "is_correction: true when the user is telling you something "
            "in the previous assistant turn was wrong, inaccurate, or "
            "they're restating what they actually meant (e.g. 'no that's "
            "wrong', 'actually, I meant X', 'not quite', 'you got it "
            "wrong'). Otherwise false. Routes the turn into the hive's "
            "real-time learning pipeline, so prefer false when unsure.\n\n"
            # ── is_casual ────────────────────────────────────────────────
            "is_casual: true when the message is pure chit-chat, a "
            "greeting, an acknowledgement, or anything that clearly "
            "doesn't need any tools, search, computer control, agent "
            "creation, or multi-step reasoning. Used to skip the heavy "
            "tool-resolution pipeline. If in doubt (looks substantive), "
            "prefer false.\n\n"
            # ── is_create_agent ─────────────────────────────────────────
            "is_create_agent: true when the user is explicitly asking to "
            "create, build, train, or set up a NEW AI agent / bot / "
            "assistant / automated workflow. Not true for questions "
            "ABOUT agents, or for using an existing agent. Routes the "
            "turn into the autogen CREATE flow.\n\n"
            # ── channel_connect ─────────────────────────────────────────
            "channel_connect: if the user is asking to connect, add, "
            "link, or set up a messaging channel (WhatsApp, Telegram, "
            "Slack, Discord, Gmail, SMS, Teams, Messenger, etc.) put "
            "the lowercased channel name here (e.g. \"whatsapp\"). "
            "Otherwise use an empty string \"\". This routes the turn "
            "to the Connect_Channel tool.\n\n"
            # ── language_change ─────────────────────────────────────────
            "language_change: if the user is asking to switch language "
            "(e.g. \"talk to me in tamil\", \"hablame en español\", "
            "\"parle-moi en français\", \"日本語で話して\"), put the "
            "ISO 639-1 code here (e.g. \"ta\" for Tamil, \"es\" for "
            "Spanish, \"fr\" for French, \"ja\" for Japanese, \"hi\" "
            "for Hindi, \"zh\" for Chinese, \"ko\" for Korean, \"ar\" "
            "for Arabic, \"de\" for German, \"ru\" for Russian). "
            "Otherwise use an empty string \"\". This overrides the "
            "session's preferred_lang so the main LLM responds in "
            "the requested language and TTS routes to an engine that "
            "supports it."
        )

    def _track_call_telemetry(
        self, model: 'ModelBackend', latency_ms: float, node_id: Optional[str],
    ) -> None:
        """Record the per-model telemetry trio (energy + latency +
        compute-contribution for hive reward).

        Owns ONLY the telemetry side-effects so dispatch_draft_first,
        dispatch_speculative, and any future dispatch variant can share
        one call path. No return value — this is fire-and-forget."""
        self._registry.record_energy(model.model_id, latency_ms)
        self._registry.record_latency(model.model_id, latency_ms)
        self._record_compute_contribution(node_id, model.model_id, latency_ms)

    def _schedule_expert_background(
        self,
        speculation_id: str,
        prompt: str,
        fast_response: str,
        expert_model: Optional['ModelBackend'],
        user_id: str,
        prompt_id: str,
        goal_id: Optional[str],
        goal_type: str,
        origin_model_id: str,
        origin_model_role: str = 'fast_model',
        delegate: Optional[str] = None,
    ) -> bool:
        """Schedule the expert-improvement task in the background pool.

        Centralizes the registration into self._active + thread submit so
        both dispatch_draft_first and dispatch_speculative share one code
        path. Returns True if the expert was actually scheduled.

        Guards:
        - no expert model → nothing to schedule
        - expert_model.model_id == origin_model_id → pointless, skip
        - budget denied → skip
        """
        if expert_model is None:
            return False
        if expert_model.model_id == origin_model_id:
            return False
        if not self._check_and_reserve_budget(user_id, goal_id, expert_model):
            return False

        with self._lock:
            entry = {
                origin_model_role: origin_model_id,
                'expert_model': expert_model.model_id,
                'user_id': user_id,
                'prompt_id': prompt_id,
                'goal_id': goal_id,
                'started_at': time.time(),
            }
            if delegate is not None:
                entry['delegate'] = delegate
            self._active[speculation_id] = entry

        self._expert_pool.submit(
            self._expert_background_task,
            speculation_id, prompt, fast_response,
            expert_model, user_id, prompt_id, goal_id, goal_type,
        )
        return True

    def _parse_draft_envelope(self, raw: str) -> dict:
        """Extract the {reply, delegate, confidence} JSON the draft should
        have produced. Tolerant of markdown fences, prose wrappers, and
        trailing commas.

        Returns an empty dict on total parse failure — callers should treat
        that as 'delegate to local' via the default in dispatch_draft_first."""
        if not raw:
            return {}
        import json as _json
        import re as _re

        text = raw.strip()

        # Strip ```json ... ``` fences if present
        fence = _re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, _re.DOTALL)
        if fence:
            text = fence.group(1)

        # Try raw parse first
        try:
            return _json.loads(text)
        except (_json.JSONDecodeError, TypeError):
            pass

        # Fall back to the first {...} we can find
        match = _re.search(r'\{.*\}', text, _re.DOTALL)
        if match:
            candidate = match.group(0)
            # Trim trailing commas before } or ]
            candidate = _re.sub(r',\s*([\}\]])', r'\1', candidate)
            try:
                return _json.loads(candidate)
            except (_json.JSONDecodeError, TypeError):
                pass

        logger.debug(f"draft envelope parse failed: {raw[:120]!r}")
        return {}

    def _pick_expert_for_delegate(self, delegate: str,
                                  user_pref: str = 'auto'):
        """Select the background model for a given delegate value.

        - "local": local FAST-tier model (e.g. Qwen3.5-4B)
        - "hive":  highest-accuracy hive/expert model, falls back to local
                   if no remote expert is available

        ``user_pref`` honours the Demopage intelligence toggle
        (``local_only`` | ``auto`` | ``hive_preferred``):

        - ``local_only`` + ``delegate='hive'`` → **downgrade to local
          fast model**.  The user explicitly opted out of hive compute;
          we respect that even if the draft self-delegated.
        - ``auto`` (default) → today's behavior: hive delegate picks the
          expert, falls back to fast if no expert registered.
        - ``hive_preferred`` → today's behavior for model selection; the
          ADDITIONAL MoE HiveMind fusion consult is fired separately by
          ``dispatch_draft_first`` via ``_schedule_hive_consult``.

        Install-validation gate:
        A model that was installed via `/api/admin/models/hub/install`
        is stamped ``capabilities['install_validated']`` — ``False`` on
        register, flipped to ``True`` only when the post-download
        ``loader.load()`` probe succeeded.  If the dispatcher picks an
        entry whose validation has not yet flipped (or explicitly
        failed), fall back to the local fast model rather than serve
        the user an unproven weight.  Seeded / manually-registered
        entries have no such flag and are trusted by default.

        Returns None if no suitable model exists — caller then treats
        the draft's reply as the final answer."""
        if delegate == 'local':
            return self._pass_validation_gate(
                self._registry.get_fast_model())
        if delegate == 'hive':
            # Respect the user's explicit "local only" preference even
            # when the draft says hive would be better — the toggle is
            # the final authority on egress.
            if user_pref == 'local_only':
                return self._pass_validation_gate(
                    self._registry.get_fast_model())
            expert = self._registry.get_expert_model()
            gated = self._pass_validation_gate(expert)
            if gated:
                return gated
            # Graceful fallback: no (valid) hive expert → use local fast
            return self._pass_validation_gate(
                self._registry.get_fast_model())
        return None

    def _pass_validation_gate(self, model):
        """Refuse to route to a hub-installed model whose post-download
        load probe has not yet succeeded.

        Catalog entries registered by `/api/admin/models/hub/install`
        carry ``source == 'hub-install'`` and
        ``capabilities['install_validated']`` (False at register time,
        flipped to True by the background validate probe).  Any other
        entry — seeded preset, manual register, legacy — has no such
        flag and passes through unchanged, preserving every existing
        code path byte-for-byte.

        Returns ``None`` when the gate rejects the pick so the caller
        can cascade to the next fallback."""
        if model is None:
            return None
        try:
            model_id = getattr(model, 'id', None) or getattr(model, 'model_id', None)
            if not model_id:
                return model  # unknown shape — don't second-guess
            # Late import — the catalog lives in the Nunba-side shim
            # but is a transitive dependency through service_tools.
            from integrations.service_tools.model_catalog import (
                get_catalog,
            )
            entry = get_catalog().get(model_id)
            if entry is None:
                return model  # not a catalog entry — trust the registry
            source = getattr(entry, 'source', '')
            if source != 'hub-install':
                return model  # seeded / manual → no validation requirement
            caps = getattr(entry, 'capabilities', {}) or {}
            if caps.get('install_validated') is True:
                return model
            logger.info(
                f"[dispatcher] refusing unvalidated hub-install "
                f"{model_id}; caller will fall back"
            )
            return None
        except Exception as e:
            # Any error in the gate degrades open — the dispatcher must
            # not fail chat because the validation lookup misbehaved.
            logger.debug(f"[dispatcher] validation gate error: {e}")
            return model

    def _schedule_hive_consult(self, prompt: str, user_id: str,
                               speculation_id: str) -> bool:
        """Best-effort MoE HiveMind fusion consult on the expert pool.

        Fired only when the user selected
        ``intelligence_preference='hive_preferred'`` AND the draft
        self-delegated to ``hive``.  Runs on the existing
        ``_expert_pool`` so it never contends with the chat hot path;
        the consult result populates ``self._last_hive_consult`` for
        observers (journey tests, admin/diag, future fusion wiring) to
        read.

        Guarantees:
          * Returns ``True`` iff a task was accepted onto the pool.
          * All failures are swallowed (``logger.debug`` only) — the
            chat reply path never blocks or errors on a missing /
            offline HiveMind.
          * No dependency on ``world_model_bridge`` at import time; the
            import is inside the worker so environments without
            hevolveai (pip) still load the dispatcher cleanly.
        """
        def _consult():
            try:
                from integrations.agent_engine.world_model_bridge import (
                    get_world_model_bridge,
                )
                bridge = get_world_model_bridge()
                if not bridge:
                    return
                # 1500ms timeout matches the default hive consult budget
                # in world_model_bridge and is half the chat hot-path
                # ceiling — cannot stall user-visible latency because
                # this runs on a worker thread, not the request thread.
                result = bridge.query_hivemind(
                    prompt, timeout_ms=1500, user_id=user_id,
                )
                if not result:
                    return
                with self._lock:
                    self._last_hive_consult[speculation_id] = {
                        'result': result,
                        'completed_at': time.time(),
                        'user_id': user_id,
                    }
                    # Cap the dict at the same ceiling as _results to
                    # avoid unbounded growth in long-running instances.
                    if len(self._last_hive_consult) > self._results_max:
                        oldest = sorted(
                            self._last_hive_consult.items(),
                            key=lambda kv: kv[1].get('completed_at', 0),
                        )[:len(self._last_hive_consult) - self._results_max]
                        for k, _ in oldest:
                            self._last_hive_consult.pop(k, None)
                logger.info(
                    f"[hive_consult] speculation={speculation_id} "
                    f"user={user_id} fusion returned "
                    f"{str(result)[:160]!r}"
                )
            except Exception as e:
                logger.debug(f"[hive_consult] failed: {e}")

        try:
            self._expert_pool.submit(_consult)
            return True
        except Exception as e:
            logger.debug(f"[hive_consult] pool submit failed: {e}")
            return False

    def _record_interaction_safely(self, **kwargs) -> None:
        """Feed an interaction into HevolveAI via WorldModelBridge. Never
        raises — continual learning is best-effort and the chat path must
        not break if HevolveAI is offline or the bridge is in circuit-open
        mode. WorldModelBridge already handles guardrail + secret redaction
        internally."""
        try:
            from integrations.agent_engine.world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()
            bridge.record_interaction(**kwargs)
        except Exception as e:
            logger.debug(f"record_interaction skipped: {e}")

    # ─── Background expert task ───

    def _expert_background_task(self, speculation_id: str, original_prompt: str,
                                fast_response: str, expert_model, user_id: str,
                                prompt_id: str, goal_id: str, goal_type: str):
        """Background: budget check → expert dispatch → deliver if improved."""
        try:
            # GUARDRAIL: circuit breaker (check again — may have been halted)
            from security.hive_guardrails import HiveCircuitBreaker
            if HiveCircuitBreaker.is_halted():
                return

            expert_prompt = self._build_expert_prompt(original_prompt, fast_response)

            start = time.time()
            expert_response = self._dispatch_to_model(
                expert_model, expert_prompt, user_id, prompt_id,
                goal_type, goal_id)
            elapsed_ms = (time.time() - start) * 1000

            # GUARDRAIL: energy tracking
            self._registry.record_energy(expert_model.model_id, elapsed_ms)
            self._registry.record_latency(expert_model.model_id, elapsed_ms)

            # Check if expert meaningfully improved
            if self._is_meaningful_improvement(fast_response, expert_response):
                # GUARDRAIL: constitutional check on expert output
                from security.hive_guardrails import ConstitutionalFilter
                passed, reason = ConstitutionalFilter.check_prompt(expert_response)
                if passed:
                    self._deliver_expert_response(
                        user_id, prompt_id, speculation_id, expert_response)
                    with self._lock:
                        self._results[speculation_id] = {
                            'response': expert_response,
                            'model': expert_model.model_id,
                            'latency_ms': round(elapsed_ms, 1),
                            'improved': True,
                        }
                        self._evict_old_results()
                else:
                    logger.warning(f"Expert response blocked by guardrail: {reason}")
            else:
                with self._lock:
                    self._results[speculation_id] = {
                        'response': fast_response,
                        'model': expert_model.model_id,
                        'latency_ms': round(elapsed_ms, 1),
                        'improved': False,
                    }
                    self._evict_old_results()

        except Exception as e:
            logger.debug(f"Expert background task failed for {speculation_id}: {e}")
        finally:
            with self._lock:
                self._active.pop(speculation_id, None)

    # ─── Helpers ───

    def _build_expert_prompt(self, original_prompt: str, fast_response: str) -> str:
        """Augment prompt: expert sees original task + fast agent's output."""
        return (
            f"You are an expert reviewer. A fast agent on a hive compute node "
            f"has already responded. Review and improve if needed.\n\n"
            f"## Original Request\n{original_prompt}\n\n"
            f"## Fast Agent's Response\n{fast_response}\n\n"
            f"## Your Task\n"
            f"Improve the response: fix errors, add missing details, improve clarity.\n"
            f"If the response is already excellent, respond with: {_RESPONSE_ADEQUATE}\n"
            f"Every output must be constructive towards humanity's benefit."
        )

    def _is_meaningful_improvement(self, fast_response: str,
                                    expert_response: str) -> bool:
        """Check if expert actually improved on the fast response."""
        if not expert_response:
            return False
        if _RESPONSE_ADEQUATE in expert_response:
            return False
        # Simple word-overlap similarity
        fast_words = set(fast_response.lower().split())
        expert_words = set(expert_response.lower().split())
        if not fast_words or not expert_words:
            return bool(expert_response.strip())
        overlap = len(fast_words & expert_words)
        similarity = overlap / max(len(fast_words | expert_words), 1)
        return similarity < _SIMILARITY_THRESHOLD

    def _dispatch_to_model(self, model: 'ModelBackend', prompt: str,
                           user_id: str, prompt_id: str,
                           goal_type: str, goal_id: str = None) -> str:
        """Send prompt to a specific model via /chat endpoint with config override.

        Always passes ``speculative=False`` and ``draft_first=False`` on the
        inner call so the dispatcher can never recursively re-enter itself
        when HEVOLVE_DRAFT_FIRST or the legacy speculative flag is enabled
        upstream. The outer chat route triggered us, and that's where the
        decision to speculate was made.

        In bundled/in-process mode (Nunba desktop), uses Flask test_client()
        instead of HTTP — port 6777 is never bound in bundled mode.
        """
        payload = {
            'user_id': user_id,
            'prompt_id': f'{goal_type}_{goal_id[:8]}' if goal_id else prompt_id,
            'prompt': prompt,
            'create_agent': True,
            'autonomous': True,
            'casual_conv': False,
            'model_config': model.to_config_list(),
            # Hard no-reentry guard — inner dispatch never speculates
            'speculative': False,
            'draft_first': False,
        }

        # Bundled mode: call the model's llama-server directly on its port.
        # Do NOT use Flask test_client('/chat') — that re-enters the full
        # HARTOS pipeline (autogen, agent creation, etc.) causing re-entrancy.
        _bundled = bool(os.environ.get('NUNBA_BUNDLED') or getattr(__import__('sys'), 'frozen', False))
        if _bundled:
            try:
                # Resolve the model's direct port from the catalog/port_registry
                _port = None
                if hasattr(model, 'port') and model.port:
                    _port = model.port
                if not _port:
                    try:
                        from core.port_registry import get_local_draft_url, get_local_llm_url
                        _url = get_local_draft_url() or get_local_llm_url()
                        if _url:
                            # Extract port from URL like http://127.0.0.1:8081/v1
                            import re as _re
                            _m = _re.search(r':(\d+)', _url)
                            _port = int(_m.group(1)) if _m else 8081
                    except Exception:
                        _port = 8081  # draft default
                import requests as _req
                resp = _req.post(
                    f'http://127.0.0.1:{_port}/v1/chat/completions',
                    json={
                        'model': 'llama',
                        'messages': [{'role': 'user', 'content': prompt}],
                        'max_tokens': 500,
                        'temperature': 0.7,
                    },
                    timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if 'choices' in data:
                        return data['choices'][0]['message']['content']
                    elif 'error' in data:
                        logger.debug(f"Draft model error: {data['error']}")
            except Exception as e:
                logger.debug(f"Direct draft dispatch failed ({model.model_id}): {e}")
            return ''

        # HTTP mode: external HARTOS server
        import requests as req
        base_url = os.environ.get('HEVOLVE_BASE_URL', f'http://localhost:{get_port("backend")}')
        try:
            resp = req.post(f'{base_url}/chat', json=payload, timeout=30)
            if resp.status_code == 200:
                return resp.json().get('response', '')
        except req.RequestException as e:
            logger.debug(f"Model dispatch failed ({model.model_id}): {e}")
        return ''

    def _deliver_expert_response(self, user_id: str, prompt_id: str,
                                  speculation_id: str, response: str):
        """Dual-channel async delivery: Crossbar chat topic + TTS pupit topic.

        Worker-thread safe — uses ``core.safe_hartos_attr`` to read
        hart_intelligence symbols without triggering Python's per-module
        import lock (worker threads racing the canonical loader on the
        langchain_core / transformers import chain caused multi-minute
        agent_daemon freezes; resolving via sys.modules avoids the lock).
        """
        from core.safe_hartos_attr import safe_hartos_attr
        from core.peer_link.message_bus import chat_topic_for

        # 1. Publish text via canonical publish_async (MessageBus → Crossbar)
        try:
            publish_async = safe_hartos_attr('publish_async')
            if publish_async is not None:
                topic = chat_topic_for(user_id)
                publish_async(topic, response)
                logger.info(
                    "Expert chat publish: spec=%s user=%s topic=%s len=%d",
                    speculation_id, user_id, topic, len(response or ''),
                )
            else:
                logger.info(
                    "Expert chat publish skipped: spec=%s user=%s — "
                    "HARTOS publish_async not yet resolvable (loader still "
                    "initialising). Drop the speculative bubble; the main "
                    "reply path will deliver when ready.",
                    speculation_id, user_id,
                )
        except Exception as e:
            logger.warning(
                "Expert chat publish failed: spec=%s user=%s err=%s",
                speculation_id, user_id, e,
            )

        # 2. Synthesize TTS and publish to pupit audio topic — ensures speculative
        #    expert improvements get the SAME audio treatment as regular replies
        #    (users on TTS-enabled sessions hear the improved response).
        try:
            _tts_synthesize_and_publish = safe_hartos_attr(
                '_tts_synthesize_and_publish')
            if _tts_synthesize_and_publish is not None:
                _tts_synthesize_and_publish(
                    response, str(user_id), speculation_id)
                logger.info(
                    "Expert TTS publish: spec=%s user=%s",
                    speculation_id, user_id,
                )
            else:
                logger.info(
                    "Expert TTS publish skipped: spec=%s user=%s — "
                    "HARTOS _tts_synthesize_and_publish not yet resolvable.",
                    speculation_id, user_id,
                )
        except Exception as e:
            logger.debug(f"Expert TTS publish failed: spec={speculation_id} err={e}")

        logger.info(f"Expert enhancement delivered: spec={speculation_id}, "
                     f"user={user_id}")

    def _check_and_reserve_budget(self, user_id: str, goal_id: str,
                                   expert_model) -> bool:
        """Check Spark budget before expert execution (atomic row lock).

        Delegates to shared budget_gate.check_goal_budget() to avoid duplication.
        """
        if not goal_id:
            return True  # No goal = no budget constraint

        try:
            from .budget_gate import check_goal_budget
            cost = expert_model.cost_per_1k_tokens
            allowed, remaining, reason = check_goal_budget(goal_id, cost)
            return allowed
        except ImportError:
            return True  # Allow if budget system unavailable

    def _record_compute_contribution(self, node_id: str, model_id: str,
                                      latency_ms: float):
        """Credit hive node for serving fast response → ad revenue eligibility.

        GUARDRAIL: Only master_key_verified nodes get credit.
        GUARDRAIL: ComputeDemocracy.adjusted_reward() — logarithmic, not linear.
        """
        if not node_id:
            return
        try:
            from integrations.social.models import get_db, PeerNode
            db = get_db()
            try:
                peer = db.query(PeerNode).filter_by(node_id=node_id).first()
                if peer and peer.master_key_verified:
                    peer.agent_count = (peer.agent_count or 0) + 1
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"Compute contribution recording skipped: {e}")

    def _evict_old_results(self):
        """Evict oldest results when over capacity. Must be called under self._lock."""
        if len(self._results) > self._results_max:
            # Remove oldest entries (dict preserves insertion order in Python 3.7+)
            excess = len(self._results) - self._results_max
            for key in list(self._results.keys())[:excess]:
                del self._results[key]

    # ─── Status / results ───

    def get_speculation_status(self, speculation_id: str) -> dict:
        """Get status of a speculative dispatch."""
        with self._lock:
            if speculation_id in self._active:
                return {'status': 'pending', 'speculation_id': speculation_id}
            if speculation_id in self._results:
                result = self._results[speculation_id]
                return {'status': 'completed', **result}
        return {'status': 'unknown', 'speculation_id': speculation_id}

    def get_stats(self) -> dict:
        """Get dispatcher statistics."""
        with self._lock:
            return {
                'active_speculations': len(self._active),
                'completed': len(self._results),
                'total_energy_kwh_24h': round(
                    self._registry.get_total_energy_kwh(24), 4),
            }


# ─── Module-level singleton ───
_dispatcher = None
_dispatcher_lock = threading.Lock()


def get_speculative_dispatcher() -> SpeculativeDispatcher:
    """Get or create the singleton SpeculativeDispatcher."""
    global _dispatcher
    if _dispatcher is None:
        with _dispatcher_lock:
            if _dispatcher is None:
                _dispatcher = SpeculativeDispatcher()
    return _dispatcher
