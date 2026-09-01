"""
Unified Agent Goal Engine - Speculative Dispatcher

Draft-first + expert-takeover dispatcher.

Two entry points, one delivery channel:

  dispatch_draft_first(prompt, ...)
    DRAFT tier (Qwen3.5-0.8B) replies SYNCHRONOUSLY in ~300ms with an
    envelope ``{reply, delegate: none|local|hive, confidence, ...}``.
    The reply is the user's standby answer; ``delegate`` is the draft's
    self-assessment of whether a heavier model needs to take the turn.

    Five guards can promote the turn to expert background regardless of
    the draft's own decision:
      * refusal pattern in the reply → REFUSAL_OVERRIDE
      * delegate=none + confidence < floor → LOW_CONFIDENCE
      * delegate=none + agent_bound prompt → AGENT_BOUND
      * delegate=none + classifier surfaces actionable intent
        (channel_connect / is_create_agent / language_change /
         invite / join_room / memory_query) → ACTIONABLE_INTENT
      * envelope parse failure → PARSE_FAILURE
    The canonical taxonomy lives in
    ``integrations.agent_engine.escalation_reasons.EscalationReason``;
    callers / observers / WorldModelBridge see the reason in the
    return dict and in ``self._active[speculation_id]``.

  dispatch_speculative(prompt, ...)
    Legacy entry. Fast tier replies synchronously, expert runs in
    background. Same delivery channel.

Expert background path
----------------------

When a turn escalates, ``_expert_background_task`` dispatches via
``_dispatch_expert_langchain``:

  - **Local expert** (``is_local=True``): routes through the full HARTOS
    /chat pipeline — full tool registry, full system prompt, full agent
    context. Bundled mode uses an in-process Flask test_client; HTTP
    mode POSTs to the configured backend. Re-entry guarded by
    ``speculative=False, draft_first=False`` in the payload.

  - **Hive expert** (``is_local=False``): registered by
    ``HiveExpertDiscovery`` from ``peer.capability.announce`` gossip,
    routed via OpenAI-compatible POST to the peer's ``base_url`` with
    bearer auth. The hive peer's 27B / fine-tuned model takes the turn
    directly — no "review the draft" wrapper.

When the expert returns, ``_deliver_expert_response`` bubble-replaces
the standby via the existing ``speculation_id`` channel: SSE for the
chat UI + TTS pupit topic for voice. The fast_response stays only when
the expert returned empty or got guardrail-blocked.

Guardrails at every layer
-------------------------
- ``ConstitutionalFilter.check_prompt`` before dispatch and on expert output
- ``HiveCircuitBreaker.is_halted()`` before dispatch and again at task entry
- ``HiveEthos.rewrite_prompt_for_togetherness`` on every prompt
- Budget enforcement via ``_check_and_reserve_budget``
- Energy + latency tracking on every model call
- Per-peer install-validation gate via ``_pass_validation_gate``

Hive expert wiring (both sides shipped, advertising is opt-in)
--------------------------------------------------------------
``HiveExpertDiscovery`` listens for ``peer.capability.announce`` /
``peer.capability.revoke`` on the platform EventBus and auto-registers
reachable, trust-verified peers as ``ModelTier.EXPERT`` backends.
``hive_capability_advertiser`` is the producer, and
``core/platform/bootstrap.py`` attaches both at boot.

Advertising is opt-in per node (``HEVOLVE_HIVE_ADVERTISE=1`` plus
``HEVOLVE_HIVE_PUBLIC_ENDPOINT``), so on a network where no peer has
opted in, ``_pick_expert_for_delegate('hive', ...)`` falls back to the
local fast model and ``served_by`` reads ``local_langchain_bg``.  That
is the default-config path, not a missing feature.
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
from typing import Dict, List, Optional

# Eager-import the history loader so the per-call inline import in
# dispatch_draft_first doesn't hit Python's import lock on every chat
# (sys.modules cache makes subsequent calls fast, but the FIRST call
# of a freshly-warm process still pays the lock cost).  Imported via
# try/except to keep the module load tolerant of test environments
# that mock out the social subtree.
try:
    from integrations.channels.memory.shared_history import (
        seed_autogen_from_shared_history)
except ImportError:
    seed_autogen_from_shared_history = None  # type: ignore[assignment]

from integrations.agent_engine.escalation_reasons import EscalationReason

logger = logging.getLogger('hevolve_social')


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
# Knowledge / data / topic nouns for the "I don't have <intermediate> <NOUN>"
# pattern — catches knowledge-cutoff refusals like
# "I don't have the 2026 IPL table" / "I don't have current IPL data" /
# "I don't have information about live matches" / "I don't have details about
# next year's schedule".  Live evidence: 2026-05-13 09:55:34 IPL turn
# (request 301eeed0) — the existing _REFUSAL_NOUNS list was capability-only
# and did not catch this knowledge-cutoff family.
_REFUSAL_DATA_NOUNS = (
    r"data|info|information|details|knowledge|records?|results?|"
    r"table|standings|schedule|listings?|stats|statistics|figures|"
    r"scores?|rankings?|fixtures?|matches|games"
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
    # "I (don't|do not) have <the/any/access to> <up-to-40-chars> <data-noun>"
    # — knowledge-cutoff refusals.  Limits intermediate-word span to 40 chars
    # so we don't false-match "I don't have a brother — but I do have data on…"
    # The data-noun must follow within the same clause.
    r"I do(?:n'?t| not) have\s+"
    r"(?:the\s+|any\s+|access to\s+(?:the\s+)?|current\s+|live\s+|real-?time\s+|recent\s+)?"
    r"[\w\s\-'\d]{0,40}?"
    r"\b(?:" + _REFUSAL_DATA_NOUNS + r")\b"
    r"|"
    # "I lack <noun>"  e.g. "I lack access to GitHub"
    r"I lack\s+(?:" + _REFUSAL_NOUNS + r")"
    r"|"
    # "I have no <noun>"  e.g. "I have no tools to retrieve…"
    r"I have no\s+(?:access|way|tools?|ability|means|"
    r"data|info|information|knowledge|records?)"
    r"|"
    # "(future|upcoming) (data|events|matches|seasons)" — knowledge-cutoff
    # phrasing where the model frames the absence as a temporal property.
    # e.g. "seasons that far out are just rumors at this point" / "future
    # events are not available yet" / "next year's data hasn't been released"
    r"(?:future|upcoming|next year'?s?|next season'?s?)\s+"
    r"(?:" + _REFUSAL_DATA_NOUNS + r"|events?|seasons?)"
    r"|"
    # "(haven't been|aren't|isn't) (released|published|available) yet"
    # — e.g. "the 2026 schedule hasn't been released yet"
    r"(?:haven'?t been|hasn'?t been|aren'?t|isn'?t|are not|is not)\s+"
    r"(?:released|published|announced|available|out|determined|set|confirmed)"
    r"(?:\s+yet)?"
    r"|"
    # "are just rumors at this point" — the IPL turn's exact phrasing.
    r"(?:are|is)\s+(?:just\s+|only\s+)?rumors?"
    r"|"
    # "predates? my training" / "after my (knowledge|training) cutoff"
    r"(?:predates?|outside of|beyond|after)\s+my\s+(?:training|knowledge)"
    r"|"
    r"my\s+(?:training|knowledge)\s+cut[\s\-]?off"
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

    def dispatch_speculative(self, prompt: str, user_id: str,
                             prompt_id: Optional[str] = None,
                             goal_id: str = None, goal_type: str = 'general',
                             node_id: str = None) -> dict:
        """
        Legacy entry point (pre-dates ``dispatch_draft_first``).

        1. Guardrail-check the prompt
        2. Pick FAST tier model → dispatch synchronously → user gets reply
        3. Record compute contribution for hive node (ad revenue)
        4. Pick EXPERT tier model → schedule background task
        5. Return fast response immediately

        **Post-refactor behavior** (commit cad43c3 onward): the expert
        background task now runs the ORIGINAL prompt through the full
        HARTOS ``/chat`` pipeline (local) or the registered hive peer's
        OpenAI-compat endpoint (remote) via
        ``_run_collapsed_expert_path``.  It NO LONGER wraps the fast
        response as a "review and improve" meta-prompt, and there is
        NO similarity gate — the expert's reply replaces the fast
        response by default (modulo guardrail block / empty response)
        via the ``speculation_id`` bubble-replacement on
        ``_deliver_expert_response``.

        Compared to the pre-refactor contract, callers see two
        differences:
          * The expert may now bind the full tool registry (Create_Agent,
            Connect_Channel, etc.) — previously it could only emit text.
          * The expert's reply is delivered unconditionally; previously
            it was only delivered when the word-overlap similarity to
            the fast response fell below 80%.

        ``dispatch_draft_first`` is the preferred entry point — it
        stamps ``escalation_reason`` on the background task and adds
        five smart-routing guards (refusal override, low confidence,
        agent-bound prompt, actionable intent, parse failure).
        ``dispatch_speculative`` retains the legacy semantics for
        callers that don't need draft-tier classification.

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

    def dispatch_draft_first(self, prompt: str, user_id: str,
                             prompt_id: Optional[str] = None,
                             goal_id: str = None, goal_type: str = 'general',
                             node_id: str = None,
                             agent_persona: Optional[str] = None,
                             preferred_lang: str = 'en',
                             user_pref: str = 'auto',
                             agent_bound: bool = False) -> dict:
        # Tag every LLM call routed through this method (the draft
        # classifier + any nested expert reroute) as ``draft.classify``
        # in llm_outbound.jsonl.  The decorator can't be applied to a
        # bound method's def line directly without import gymnastics
        # at module-load time, so we use the context manager inline.
        # See ``core.llm_outbound_logger.with_source`` rationale.
        from core.llm_outbound_logger import source_context as _src
        with _src('draft.classify'):
            return self._dispatch_draft_first_impl(
                prompt, user_id, prompt_id=prompt_id, goal_id=goal_id,
                goal_type=goal_type, node_id=node_id,
                agent_persona=agent_persona, preferred_lang=preferred_lang,
                user_pref=user_pref, agent_bound=agent_bound,
            )

    def _dispatch_draft_first_impl(self, prompt: str, user_id: str,
                             prompt_id: Optional[str] = None,
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

        # ── 2. Load recent conversation history (best-effort, non-fatal) ──
        # Single source of truth — same seed_autogen_from_shared_history the
        # autogen GroupChat uses.  Without this the draft sees each turn as
        # first-contact and emits generic greetings for follow-ups (witnessed
        # 2026-05-09 09:35 — user asked about WhatsApp at 09:34 then "what's
        # happening?" at 09:35; draft replied with a generic "Nothing
        # unusual…" because it had no memory of the WhatsApp turn 60s prior).
        # Cap at 4 messages — the 0.8B context budget can't fit more without
        # crowding out the answering rules + JSON schema.
        recent_turns: List[Dict] = []
        try:
            if seed_autogen_from_shared_history is None:
                raise ImportError(
                    "shared_history.seed_autogen_from_shared_history "
                    "not importable in this environment")
            recent_turns = seed_autogen_from_shared_history(
                user_id, max_messages=4) or []
            # NO dedup against the current prompt — users genuinely
            # repeat themselves (rephrasing, asking again, "hi" / "hi"
            # in casual back-and-forth).  Dropping a recent turn just
            # because its text matches the current prompt would silently
            # lose legitimate conversation history.  If the writer hook
            # in get_response_group / chatbot_routes persisted the
            # current turn before this dispatch ran, the prompt may
            # appear twice in the LLM's input — that's the correct
            # natural-language signal ("user said X, then said X again")
            # and the model should treat it as such.
        except Exception as _hist_err:
            logger.debug(f"draft history load failed: {_hist_err}")
            recent_turns = []

        # ── 3. Dispatch the draft with the classifier prompt ──
        draft_prompt = self._build_draft_classifier_prompt(
            prompt, agent_persona=agent_persona, preferred_lang=preferred_lang,
            recent_turns=recent_turns)
        start = time.time()
        draft_raw = self._dispatch_to_model(
            draft_model, draft_prompt, user_id, prompt_id, goal_type, goal_id)
        draft_latency_ms = (time.time() - start) * 1000
        self._track_call_telemetry(draft_model, draft_latency_ms, node_id)

        # ── 3. Parse envelope + record draft interaction ──
        parsed = self._parse_draft_envelope(draft_raw)
        draft_reply = parsed.get('reply') or draft_raw.strip()[:500]
        # Track whether the envelope parsed at all — an empty parsed dict
        # means we fell through to the delegate='local' default below,
        # which is materially different from the model emitting an
        # explicit 'local' decision.  Stamp the reason for downstream
        # telemetry / continual-learning before any guard runs.
        parse_failed = not parsed
        delegate = parsed.get('delegate', 'local')  # default on parse fail
        confidence = float(parsed.get('confidence') or 0.0)
        # Canonical reason value — refined by each guard below.  Starts
        # at PARSE_FAILURE when the envelope didn't parse, otherwise the
        # baseline CLASSIFIER_DELEGATE.  Only meaningful when we end up
        # in the delegate in ('local', 'hive') branch — None otherwise.
        escalation_reason: Optional[EscalationReason] = (
            EscalationReason.PARSE_FAILURE if parse_failed
            else EscalationReason.CLASSIFIER_DELEGATE
        )

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
            escalation_reason = EscalationReason.REFUSAL_OVERRIDE

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
            escalation_reason = EscalationReason.LOW_CONFIDENCE

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
            # Same rationale as the ACTIONABLE-INTENT swap below: on an
            # agent-bound turn the draft has no tools, so its in-band
            # reply can only describe work, never do it — the 13:30
            # live turn shipped "saved and confirmed" while nothing had
            # run.  The expert's real answer replaces the standby via
            # the existing speculation_id bubble path (#204).
            draft_reply = _REFUSAL_STANDBY_REPLY
            escalation_reason = EscalationReason.AGENT_BOUND

        # ACTIONABLE-INTENT GUARD: when the draft's own classifier
        # surfaces an actionable intent flag (channel_connect,
        # is_create_agent, language_change), answering in-band would
        # orphan the action — there is no way to invoke the matching
        # tool (Connect_Channel / Create_Agent) from the casual draft
        # path because casual_conv=True skips the full tool registry
        # in hart_intelligence_entry.get_ans (see the is_first=True
        # branch).  Promote delegate=none → 'local' so the expert
        # turn binds the full registry — Connect_Channel for
        # channel-add intents, Create_Agent for agent-build intents —
        # and actually fires the tool the LLM would otherwise have
        # described in free-form text.
        #
        # The draft's reply is also replaced with the standby (same
        # rationale as REFUSAL GUARD above): the draft on the
        # casual path has no tool access, so any in-band reply on
        # an actionable-intent turn is a verified-signal anti-pattern
        # — it claims action while taking none.  The expert reply
        # replaces the standby via the existing speculation_id
        # bubble-replacement path (#204 already shipped).
        # Helper: treat the sentinel string 'none' as "no intent", matching
        # the classifier's own contract (it returns 'none' for *_intent /
        # memory_query when there's no actionable intent).  Without this,
        # the literal 'none'.strip() evaluates truthy and the guard fires
        # on every casual turn → every reply gets replaced with the
        # "Let me check that for you…" placeholder, and the user never
        # receives the actual answer because the expert background task
        # has nothing real to refine (live evidence 2026-05-11 22:36:41
        # request 897fc534 — speculation_id 9a418ac4-1eb, message 'strange').
        def _intent_set(value) -> bool:
            if not value or value is False:
                return False
            v = str(value).strip().lower()
            return bool(v) and v != 'none'

        # A trivial casual utterance ("hii", "hey", "thanks") carries no real
        # recall intent, but the 0.8B draft sometimes HALLUCINATES memory_query
        # on it (live 2026-06-09: 'hii' → memory_query='earlier greetings').  That
        # would needlessly swap the reply for the standby placeholder and escalate
        # to the expert turn, which then loops on a non-existent query and strands
        # the user on "Let me check that for you…".  Suppress ONLY the memory_query
        # escalation for such turns; the other actionable intents (channel_connect
        # / create_agent / language_change / invite / join) can be terse — "connect
        # discord" is two words and genuinely needs its tool — so they stay.
        _trivial_casual = (bool(parsed.get('is_casual'))
                           and len((prompt or '').strip().split()) <= 2)

        if delegate == 'none' and (
            _intent_set(parsed.get('channel_connect'))
            or parsed.get('is_create_agent')
            or _intent_set(parsed.get('language_change'))
            or _intent_set(parsed.get('invite_intent'))
            or _intent_set(parsed.get('join_room_intent'))
            or (_intent_set(parsed.get('memory_query')) and not _trivial_casual)
        ):
            logger.info(
                "draft-first: actionable intent flag set "
                "(channel_connect=%r, is_create_agent=%r, "
                "language_change=%r, invite_intent=%r, "
                "join_room_intent=%r, memory_query=%r) — escalating "
                "delegate=none → 'local' so the expert tool registry "
                "handles the turn.",
                parsed.get('channel_connect'),
                parsed.get('is_create_agent'),
                parsed.get('language_change'),
                parsed.get('invite_intent'),
                parsed.get('join_room_intent'),
                parsed.get('memory_query'),
            )
            draft_reply = _REFUSAL_STANDBY_REPLY
            delegate = 'local'
            escalation_reason = EscalationReason.ACTIONABLE_INTENT

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

        # When the draft path's net effect is "draft answered, no
        # escalation" (delegate=='none' and none of the guards fired),
        # the escalation_reason is meaningless — clear it so the
        # WorldModelBridge sees a clean "this draft was the final
        # answer" record.
        recorded_reason = (
            escalation_reason.value if delegate in ('local', 'hive')
            else None
        )
        self._record_interaction_safely(
            user_pref=user_pref,
            user_id=user_id, prompt_id=prompt_id, prompt=prompt,
            response=draft_reply, model_id=draft_model.model_id,
            latency_ms=draft_latency_ms, node_id=node_id, goal_id=goal_id,
            escalation_reason=recorded_reason,
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
                escalation_reason=escalation_reason,
                user_pref=user_pref,
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
            # Additive field: which guard promoted this turn to expert
            # (or ``None`` when the draft answered as final).  Canonical
            # values come from ``EscalationReason`` (see
            # ``integrations.agent_engine.escalation_reasons``).
            'escalation_reason': recorded_reason,
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
        recent_turns: Optional[List[Dict]] = None,
    ) -> str:
        """Wrap the user prompt with the draft-first classifier instruction.

        The draft's job is twofold: (a) produce a short standby reply fit
        for simple chat, (b) self-assess whether a bigger model is needed.
        The JSON schema is flat so a 0.8B model can reliably emit it.

        If ``agent_persona`` is provided, it's prepended to the instruction
        so the draft's reply is in the voice of the custom / system agent
        the user is talking to instead of a generic first-responder. Used
        for the Path-2 system-agent case (e.g. Nunba personality agent).

        If ``recent_turns`` is provided, prior conversation context is
        rendered as a "Recent conversation" block before the current user
        prompt so the draft can answer follow-ups in context (e.g. user
        asks "what's happening?" 60s after asking about WhatsApp — without
        history the draft would treat each turn as first-contact and emit
        a generic greeting).  Capped at 4 turns to fit the 0.8B context
        budget; oldest first; long messages are truncated to 400 chars.

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

        # Recent conversation context — single source via
        # seed_autogen_from_shared_history (the autogen path uses the same
        # call), formatted into a flat User:/Assistant: transcript so the
        # 0.8B can read follow-ups in context.  Capped at 4 turns + 400
        # chars/turn to fit the draft's context budget.
        history_block = ""
        if recent_turns:
            _hist_lines = []
            for _turn in recent_turns[-4:]:
                _role = _turn.get('role') or ''
                _content = (_turn.get('content') or '').strip()
                if not _content:
                    continue
                if len(_content) > 400:
                    _content = _content[:400] + '…'
                if _role == 'user':
                    _hist_lines.append(f"User: {_content}")
                elif _role == 'assistant':
                    _hist_lines.append(f"Assistant: {_content}")
            if _hist_lines:
                history_block = (
                    "Recent conversation (oldest first) — CONSULT this before "
                    "you reply or act, and continue it in context: you are "
                    "mid-conversation, not starting fresh. Follow-ups like "
                    "'what's happening?' or 'why?' refer back to these turns:\n"
                    + "\n".join(_hist_lines)
                    + "\n\n"
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
            "- You MAY answer directly ONLY for tasks the LLM can fully "
            "complete in this single response — no external system state, "
            "no live data, no per-user persistence beyond this turn. "
            "Specifically: trivial recall, simple math, greetings, "
            "explanations, definitions, single-shot CODE GENERATION (write "
            "a function, explain an algorithm, show a snippet — the user "
            "just wants the code as text), short refactors / code reviews "
            "of pasted text, palindrome / sort / hash / regex one-liners, "
            "translation, summarisation of pasted content.\n"
            "- DELEGATE only when the task genuinely needs runtime access "
            "you cannot have in a single completion.  Set delegate=\"local\" "
            "(or \"hive\" for very large requests) for: live URL fetches, "
            "running / executing code (not writing it), reading or writing "
            "files, current system state, multi-TURN tool use, per-user "
            "memory beyond this turn, anything requiring a persistent "
            "agent that runs over time.  Write a brief standby reply such "
            "as \"Let me check that for you…\", \"Looking that up…\", or "
            "\"One moment…\". The standby is replaced by the authoritative "
            "answer automatically.\n"
            "- HEURISTIC for code-shaped requests: 'write X', 'show me a "
            "function for X', 'how would you implement X', 'explain how to "
            "X' → answer directly with the code/text.  'run this code', "
            "'execute X', 'what does this output', 'set up an agent that X' "
            "→ delegate.  Writing vs running.\n"
            "- Refusals are not your call.  If you ever feel the urge to "
            "refuse: pick a standby instead and delegate.\n\n"
            + history_block
            + f"User: {user_prompt}\n\n"
            "Respond with ONE JSON object on a single line and NOTHING else:\n"
            '{"reply": "<your short reply to the user, 1-3 sentences>", '
            '"delegate": "none" OR "local" OR "hive", '
            '"confidence": <float 0-1>, '
            '"is_correction": true OR false, '
            '"is_casual": true OR false, '
            '"is_create_agent": true OR false, '
            '"channel_connect": "<channel name or empty string>", '
            '"language_change": "<ISO 639-1 code or empty string>", '
            '"invite_intent": "<short context if user wants invite link, or empty>", '
            '"join_room_intent": "<platform + room/url if user wants agent to join, or empty>", '
            '"memory_query": "<short context if user asks about past conversations, or empty>", '
            '"reason": "<why you chose this delegate value>"}\n\n'
            # ── delegate ────────────────────────────────────────────────
            "delegate: Use \"none\" for greetings, small-talk, factual "
            "questions you can fully answer yourself, or anything that needs "
            "no external tools. Never \"none\" for live/current data "
            "(weather, news, prices, scores, anything 'right now') — you "
            "cannot know it; that ALWAYS delegates. Use \"local\" if the request needs tools, "
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
            "supports it.\n\n"
            # ── invite_intent ───────────────────────────────────────────
            "invite_intent: if the user is asking to invite, share, or "
            "refer a friend / colleague / family member to Nunba (e.g. "
            "\"invite a friend\", \"give me an invite link\", \"share "
            "Nunba with my colleague\", \"how do I refer people\"), put "
            "a short freeform context here (e.g. \"work friend\" or "
            "\"family\") — empty string is fine for a generic shareable "
            "link. Otherwise use an empty string \"\". This routes the "
            "turn to the Invite_Friend tool.\n\n"
            # ── join_room_intent ────────────────────────────────────────
            "join_room_intent: if the user is asking the AI to JOIN an "
            "external room / channel / meeting / group as a co-pilot, "
            "note-taker, or participant (e.g. \"join my Discord audio "
            "room\", \"attend my Teams meet\", \"take notes in the "
            "WhatsApp family group\", \"co-pilot my Slack channel\"), "
            "put a short \"<platform> <room or url>\" string here "
            "(e.g. \"discord https://discord.com/channels/123/456\"). "
            "Otherwise use an empty string \"\". This routes the turn "
            "to the Join_External_Room tool, which always gates on "
            "consent and announces the agent's presence in the room.\n\n"
            # ── memory_query ────────────────────────────────────────────
            "memory_query: if the user is asking about something they "
            "discussed previously, what was said in past conversations, "
            "or asking the assistant to recall earlier context (e.g. "
            "\"what did we speak 2 days back\", \"do you remember when "
            "I asked about X\", \"what was that thing we discussed last "
            "week\", \"recall my previous question on Y\", \"what did I "
            "tell you about my project\"), put a short freeform context "
            "string here describing the topic / time window (e.g. "
            "\"conversations from last 2 days\", \"earlier project "
            "discussion\"). Otherwise use an empty string \"\". This "
            "routes the turn to the recall_memory tool which searches "
            "the memory graph with optional time filters."
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
        escalation_reason: Optional['EscalationReason'] = None,
        user_pref: str = 'auto',
    ) -> bool:
        """Schedule the expert-improvement task in the background pool.

        Centralizes the registration into self._active + thread submit so
        both dispatch_draft_first and dispatch_speculative share one code
        path. Returns True if the expert was actually scheduled.

        ``escalation_reason`` is purely observability metadata: it gets
        stamped into ``self._active[speculation_id]`` so admin /diag and
        telemetry can ask "why was this turn escalated?" without
        re-deriving the heuristic.  Optional + defaults to None so the
        legacy dispatch_speculative call site (which has no draft to
        derive a reason from) needs no change.

        Guards:
        - no expert model → nothing to schedule
        - expert_model.model_id == origin_model_id → pointless, skip
        - budget denied → skip
        """
        # Every refusal below leaves the user on the draft's reply (often the
        # "Let me check that for you…" standby) with NOTHING coming — logged
        # at INFO because live 2026-08-26 01:15-01:27 three delegate='local'
        # turns produced zero expert activity and zero lines naming why.
        if expert_model is None:
            logger.info("expert not scheduled for %s: no expert model "
                        "registered (draft reply is final)", speculation_id)
            return False
        if expert_model.model_id == origin_model_id:
            logger.info("expert not scheduled for %s: pick equals origin %s "
                        "(draft reply is final)", speculation_id,
                        origin_model_id)
            return False
        if not self._check_and_reserve_budget(user_id, goal_id, expert_model):
            logger.info("expert not scheduled for %s: budget denied for %s",
                        speculation_id, expert_model.model_id)
            return False

        with self._lock:
            entry = {
                origin_model_role: origin_model_id,
                'expert_model': expert_model.model_id,
                'user_id': user_id,
                'prompt_id': prompt_id,
                'goal_id': goal_id,
                'started_at': time.time(),
                # #224 — propagate so _run_collapsed_expert_path can gate
                # record_interaction on local_only without re-plumbing the
                # parameter through 3 layers of background-task submission.
                'user_pref': user_pref,
            }
            if delegate is not None:
                entry['delegate'] = delegate
            if escalation_reason is not None:
                # Store the canonical string value (Enum's str inheritance
                # makes this safe for JSON / SSE round-trip).
                entry['escalation_reason'] = (
                    escalation_reason.value
                    if hasattr(escalation_reason, 'value')
                    else str(escalation_reason)
                )
            self._active[speculation_id] = entry

        # #162 — capture the request thread's rid (thread-local) so the pool
        # worker can re-bind it.  A ThreadPoolExecutor worker starts with an
        # EMPTY thread-local, so without this the expert's autogen turn runs
        # request_id='' → _is_background_call mis-marks the USER's own work as
        # background and the foreground preempt starves it (witnessed:
        # source=autogen.create, thread=spec_expert_0, thread_local_rid=None).
        try:
            from hartos.threadlocal import thread_local_data as _tl
            _req_rid = _tl.get_request_id() or ''
        except Exception:
            _req_rid = ''
        self._expert_pool.submit(
            self._expert_background_task,
            speculation_id, prompt, fast_response,
            expert_model, user_id, prompt_id, goal_id, goal_type,
            _req_rid,
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

    def _record_interaction_safely(self, user_pref: str = 'auto', **kwargs) -> None:
        """Feed an interaction into HevolveAI via WorldModelBridge. Never
        raises — continual learning is best-effort and the chat path must
        not break if HevolveAI is offline or the bridge is in circuit-open
        mode. WorldModelBridge already handles guardrail + secret redaction
        internally.

        #224 mode gate: when the user is in `local_only` mode, do NOT
        touch WorldModelBridge at all.  The bridge's lazy first-instantiation
        triggers a SHA-256 scan of the hevolveai package (mitigated by
        the world_model_bridge.py short-circuit, but still wasteful work
        for a user who explicitly opted out of hive participation).
        Treating mode as authoritative also matches the user's design
        intent: HevolveAI is loaded on-demand for hive/hybrid modes,
        and is structurally not a prerequisite for local-only operation.
        """
        # Local-only users have opted out of contributing to the hive's
        # learning loop.  Skipping the call here keeps WorldModelBridge
        # uninitialised for them — zero cost, zero side effects.  Mode
        # switch (local→hive) is picked up on the very next chat turn
        # because user_pref is per-request, not cached on the dispatcher.
        if user_pref == 'local_only':
            return
        try:
            from integrations.agent_engine.world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()
            bridge.record_interaction(**kwargs)
        except Exception as e:
            logger.debug(f"record_interaction skipped: {e}")

    # ─── Background expert task ───
    #
    # Single path: expert (local langchain pipeline or hive peer) takes
    # the ORIGINAL turn directly.  No "improve this draft" wrapper, no
    # similarity gate — when the expert is the actual expert (full tool
    # registry locally, 27B or fine-tuned on hive), its reply IS the
    # answer.  The standby (fast_response) was already delivered by the
    # draft-first path; this task replaces it via the existing
    # speculation_id bubble-replacement channel on _deliver_expert_response.

    def _expert_background_task(self, speculation_id: str, original_prompt: str,
                                fast_response: str, expert_model, user_id: str,
                                prompt_id: str, goal_id: str, goal_type: str,
                                request_id: str = ''):
        """Background: dispatch expert → deliver (or fall through to draft
        standby).  Outer try/finally owns the shared invariants —
        circuit-breaker gate, exception swallowing, ``_active`` cleanup —
        so the helper can focus on dispatch + delivery semantics.

        ``request_id`` is the originating user turn's id, captured on the
        request thread at submit time and re-bound HERE (#162) — thread-locals
        don't cross the pool boundary, so this is the one place the expert's
        autogen LLM calls can inherit the user's rid and stay foreground.
        """
        # #162 — re-bind the originating rid on THIS worker thread so every
        # LLM call the expert issues (recipe/autogen via _dispatch_expert_*)
        # is classified foreground, not background-and-preemptible.
        if request_id:
            try:
                from hartos.threadlocal import thread_local_data as _tl
                _tl.set_request_id(request_id=request_id)
            except Exception:
                pass
        try:
            # GUARDRAIL: circuit breaker (check again — may have been halted)
            from security.hive_guardrails import HiveCircuitBreaker
            if HiveCircuitBreaker.is_halted():
                logger.info("expert task %s skipped: hive halted "
                            "(draft reply is final)", speculation_id)
                return

            self._run_collapsed_expert_path(
                speculation_id, original_prompt, fast_response,
                expert_model, user_id, prompt_id, goal_id, goal_type)

        except Exception:
            # warning + traceback, not debug: this swallow is the only place
            # the whole expert leg can die, and at debug it dies invisibly —
            # the user keeps waiting on the standby bubble forever.
            logger.warning("Expert background task failed for %s (user turn "
                           "stays on the draft standby)", speculation_id,
                           exc_info=True)
        finally:
            with self._lock:
                self._active.pop(speculation_id, None)

    def _run_collapsed_expert_path(self, speculation_id: str,
                                   original_prompt: str, fast_response: str,
                                   expert_model, user_id: str, prompt_id: str,
                                   goal_id: str, goal_type: str):
        """Collapsed path: expert takes the ORIGINAL turn through the full
        langchain pipeline (local) or the hive endpoint (remote).  No
        'improve this draft' wrapper.  Reuses ``_dispatch_expert_langchain``
        for routing + ``_deliver_expert_response`` for SSE/TTS fan-out.
        """
        start = time.time()
        expert_response = self._dispatch_expert_langchain(
            expert_model, original_prompt, user_id, prompt_id,
            goal_type, goal_id)
        elapsed_ms = (time.time() - start) * 1000

        # GUARDRAIL: energy + latency telemetry (same instrumentation as
        # the legacy path — the rollout flip must not lose these metrics).
        self._registry.record_energy(expert_model.model_id, elapsed_ms)
        self._registry.record_latency(expert_model.model_id, elapsed_ms)

        # Empty response → the draft standby stays as the final reply.
        # The user already saw it; nothing to deliver.  Record the
        # _results entry so admin /diag can tell the expert ran and
        # returned empty (vs never ran).
        if not expert_response or not expert_response.strip():
            logger.debug(
                "collapsed expert returned empty for %s; "
                "draft standby remains the final reply", speculation_id)
            with self._lock:
                self._results[speculation_id] = {
                    'response': fast_response,
                    'model': expert_model.model_id,
                    'latency_ms': round(elapsed_ms, 1),
                    'improved': False,
                }
                self._evict_old_results()
            return

        # GUARDRAIL: constitutional check on expert output before delivery
        from security.hive_guardrails import ConstitutionalFilter
        passed, reason = ConstitutionalFilter.check_prompt(expert_response)
        if not passed:
            logger.warning(
                "collapsed expert response blocked by guardrail: %s",
                reason)
            return

        # Unconditional delivery: the expert is THE expert here, not a
        # "maybe improvement".  Bubble-replace the standby via the
        # existing speculation_id channel (SSE + TTS — see
        # _deliver_expert_response for the dual-channel contract).
        self._deliver_expert_response(
            user_id, prompt_id, speculation_id, expert_response)

        # Feed continual learning.  Stamp escalation_reason from the
        # _active entry so distillation can weight refusal-overridden
        # turns differently from clean classifier-delegate turns.
        with self._lock:
            active_entry = dict(self._active.get(speculation_id, {}))
        served_by = (
            'hive_langchain_bg' if not expert_model.is_local
            else 'local_langchain_bg'
        )
        self._record_interaction_safely(
            # #224 — honor user_pref stashed by _schedule_expert_background;
            # local_only users skip WorldModelBridge entirely (no HevolveAI
            # touch on the expert background path either).  Falling back
            # to 'auto' preserves current behavior for any legacy active
            # entry that pre-dated the field.
            user_pref=active_entry.get('user_pref', 'auto'),
            user_id=user_id, prompt_id=prompt_id,
            prompt=original_prompt, response=expert_response,
            model_id=expert_model.model_id, latency_ms=elapsed_ms,
            goal_id=goal_id,
            escalation_reason=active_entry.get('escalation_reason'),
        )

        with self._lock:
            self._results[speculation_id] = {
                'response': expert_response,
                'model': expert_model.model_id,
                'latency_ms': round(elapsed_ms, 1),
                'improved': True,
                'served_by': served_by,
            }
            self._evict_old_results()

    def _build_dispatch_payload(self, model, prompt, user_id, prompt_id,
                                goal_id, goal_type) -> dict:
        """The ONE inner-/chat payload shared by _dispatch_to_model and
        _dispatch_expert_langchain (their payloads were char-identical).

        ``speculative``/``draft_first`` are False so the inner /chat reads them
        and skips the dispatcher entirely — hard no-reentry, never recursively
        re-enter. ``prompt_id`` is forwarded ONLY when the caller gave a real
        on-disk agent id (never synthesised from request_id/goal_id — any
        non-empty prompt_id is loaded as ``prompts/{prompt_id}.json``);
        ``goal_id``/``goal_type`` travel separately as telemetry/budget
        metadata, not as a routing key.
        """
        # create_agent/autonomous: ONLY for goal-driven daemon dispatch
        # (goal_id set) — that work IS autonomous creation.  A user
        # conversational turn (goal_id None) must reach the agent as a
        # CONVERSATION: hardcoded True routed the 2026-08-22 13:30 live
        # turn (prompt_id 90916249292, completed agent) into
        # creation-RESUME, which auto-completed the agent's stale plan
        # ("resumed - action complete" ×4) with zero LLM calls and never
        # executed the user's request (recall("BLUEFIN") → count 0).
        # Guard: tests/unit/test_expert_dispatch_mode.py.
        payload = {
            'user_id': user_id,
            'prompt': prompt,
            'create_agent': bool(goal_id),
            'autonomous': bool(goal_id),
            'casual_conv': False,
            'model_config': model.to_config_list(),
            'speculative': False,
            'draft_first': False,
        }
        if prompt_id:
            payload['prompt_id'] = prompt_id
        if goal_id:
            payload['goal_id'] = goal_id
        # #750: the inner /chat runs on a FRESH handler thread — only the
        # payload can carry the originating rid (hie:9099 reads it, :9253
        # binds it).  Without it the whole inner reuse group ran
        # request_id='' -> classified background -> routed to the CLOSABLE
        # bg client -> closed by this very turn's own 0->1 foreground edge
        # -> RuntimeError 'client has been closed' (3x live 2026-09-01;
        # llm_outbound.jsonl rid='' source=autogen.reuse).  The expert
        # worker already rebound the user rid to thread-local (#162,
        # _expert_background_task); daemon dispatches carry their
        # daemon_<goal_id> rid, which is_genuine_user_request still
        # classifies background — #162 behavior preserved.
        try:
            from hartos.threadlocal import thread_local_data as _tl
            _rid = _tl.get_request_id() or ''
        except Exception:
            _rid = ''
        if _rid:
            payload['request_id'] = _rid
        if goal_type and goal_type != 'general':
            payload['goal_type'] = goal_type
        return payload

    def _dispatch_expert_langchain(self, model, prompt: str, user_id: str,
                                   prompt_id: str, goal_type: str,
                                   goal_id: Optional[str]) -> str:
        """Send the expert turn through the right transport for its tier.

        - ``model.is_local=True``:  route through the FULL HARTOS /chat
          pipeline (agent loading, autogen GroupChat, full tool registry)
          so actionable-intent / agent-bound turns actually fire their
          tools.
            * Bundled mode (NUNBA_BUNDLED / sys.frozen): in-process Flask
              ``test_client`` — port 6777 is not bound in bundled Nunba.
            * Non-bundled: HTTP POST to ``HEVOLVE_BASE_URL`` (or the
              port_registry-resolved backend).
          Re-entry is prevented by ``speculative=False, draft_first=False``
          in the payload — the inner /chat handler reads these and skips
          the dispatcher.

        - ``model.is_local=False`` (hive-served expert, registered by
          ``HiveExpertDiscovery``):  OpenAI-compatible POST to
          ``{base_url}/chat/completions`` with the registered auth token.
          The hive peer's 27B / fine-tuned model takes the turn directly.

        Returns the response string, or ``''`` on any failure — caller
        falls back to ``fast_response`` so the user always sees the
        draft's standby.
        """
        if model is None:
            return ''

        # ── Hive path: OpenAI-compatible POST to peer base_url ──
        if not getattr(model, 'is_local', True):
            cfg = getattr(model, 'config_list_entry', {}) or {}
            base_url = (cfg.get('base_url') or '').rstrip('/')
            api_key = cfg.get('api_key') or ''
            inner_model_id = cfg.get('model') or model.model_id
            if not base_url:
                logger.debug(
                    "hive expert %s missing base_url — cannot dispatch",
                    model.model_id)
                return ''
            try:
                import requests as _req
                headers = (
                    {'Authorization': f'Bearer {api_key}'} if api_key else {})
                resp = _req.post(
                    f'{base_url}/chat/completions',
                    headers=headers,
                    json={
                        'model': inner_model_id,
                        'messages': [{'role': 'user', 'content': prompt}],
                        'max_tokens': 1500,
                        'temperature': 0.7,
                    },
                    timeout=60,
                )
                if resp.status_code == 200:
                    data = resp.json() or {}
                    choices = data.get('choices') or []
                    if choices:
                        msg = (choices[0] or {}).get('message') or {}
                        return msg.get('content') or ''
                else:
                    logger.debug(
                        "hive expert %s returned HTTP %s",
                        model.model_id, resp.status_code)
            except Exception as e:
                logger.debug(
                    "hive expert dispatch failed (%s): %s",
                    model.model_id, e)
            return ''

        # ── Local path: full HARTOS /chat pipeline ──
        # prompt_id: ONLY include when the caller gave a real on-disk
        # agent identifier.  NEVER synthesise from request_id, goal_id,
        # node_id, or speculation_id — the inner /chat handler treats
        # any non-empty prompt_id as a literal filename
        # (``prompts/{prompt_id}.json``) and ``_autonomous_gather_info``
        # will mint a duplicate agent JSON keyed by whatever synthetic
        # string it receives.  Live regressions both shapes have caused:
        #   * 2026-05-12 request 66c63859-… spawned a phantom
        #     ``prompts/66c63859-…json`` (request-id-derived).
        #   * Goal-driven daemon dispatch with goal_id='abc-deadbeef'
        #     used to send ``prompt_id='general_abc-dead'`` — the inner
        #     /chat then tries to load ``prompts/general_abc-dead.json``,
        #     fails, and mints yet another agent under that key.
        # Goal/observability identifiers belong in ``goal_id``/
        # ``goal_type`` payload fields (which the inner /chat reads as
        # context metadata, not as a routing key), not in prompt_id.
        payload = self._build_dispatch_payload(
            model, prompt, user_id, prompt_id, goal_id, goal_type)

        import sys as _sys
        _bundled = bool(
            os.environ.get('NUNBA_BUNDLED')
            or getattr(_sys, 'frozen', False)
        )
        if _bundled:
            try:
                # Late import — keeps module-load time independent of
                # hart_intelligence_entry's heavy boot graph.  In bundled
                # Nunba this is cheap (already in sys.modules by the
                # time a chat turn fires).
                from hart_intelligence_entry import app as _app  # type: ignore
                with _app.test_client() as client:
                    resp = client.post('/chat', json=payload)
                    if resp.status_code == 200:
                        data = resp.get_json() or {}
                        return data.get('response') or ''
                    logger.debug(
                        "local expert /chat returned %s in bundled mode",
                        resp.status_code)
            except Exception as e:
                logger.debug(
                    "local expert bundled dispatch failed: %s", e)
            return ''

        # Non-bundled: HTTP POST to the configured backend.
        try:
            import requests as _req
            base = os.environ.get(
                'HEVOLVE_BASE_URL',
                f'http://localhost:{get_port("backend")}',
            )
            resp = _req.post(f'{base}/chat', json=payload, timeout=60)
            if resp.status_code == 200:
                return (resp.json() or {}).get('response') or ''
            logger.debug(
                "local expert /chat HTTP returned %s", resp.status_code)
        except Exception as e:
            logger.debug("local expert HTTP dispatch failed: %s", e)
        return ''

    # ─── Helpers ───

    def _dispatch_to_model(self, model: 'ModelBackend', prompt: str,
                           user_id: str, prompt_id: Optional[str],
                           goal_type: str, goal_id: str = None) -> str:
        """Send prompt to a specific model via /chat endpoint with config override.

        Always passes ``speculative=False`` and ``draft_first=False`` on the
        inner call so the dispatcher can never recursively re-enter itself
        when HEVOLVE_DRAFT_FIRST or the legacy speculative flag is enabled
        upstream. The outer chat route triggered us, and that's where the
        decision to speculate was made.

        ``prompt_id`` is intentionally Optional and only included in the
        payload when truthy — see ``_dispatch_expert_langchain`` for the
        same invariant + the live regression that motivated it.

        In bundled/in-process mode (Nunba desktop), uses Flask test_client()
        instead of HTTP — port 6777 is never bound in bundled mode.
        """
        # prompt_id: only forward when the caller gave a real one;
        # goal_id / goal_type travel separately as observability metadata.
        # See ``_dispatch_expert_langchain`` for the rationale + live
        # regression that motivated this invariant.
        payload = self._build_dispatch_payload(
            model, prompt, user_id, prompt_id, goal_id, goal_type)

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
                # ONE transport (task #10): pooled_post is port-scoped — a
                # true draft-port call passes straight through (the draft
                # server has its own slots), but when no draft server exists
                # and _port fell back to the MAIN llama port, the call is
                # admitted through the slot-aware scheduler like every other
                # main-server call. The old raw requests.post here hit the
                # main server UNSCHEDULED on draft-less boxes — invisible to
                # inflight() and unpreemptable by a user turn (#162 hazard).
                from core.http_pool import pooled_post as _pooled_post
                # Manual log_outbound call here because the requests
                # transport bypasses the global httpx hook installed in
                # ``core.llm_outbound_logger.install()``; this is the only
                # place draft-classifier prompts get a record.
                _draft_body = {
                    'model': 'llama',
                    'messages': [{'role': 'user', 'content': prompt}],
                    'max_tokens': 500,
                    'temperature': 0.7,
                }
                _draft_start = time.time()
                resp = _pooled_post(
                    f'http://127.0.0.1:{_port}/v1/chat/completions',
                    json=_draft_body,
                    timeout=15,
                )
                try:
                    from core.llm_outbound_logger import log_outbound as _log_ob
                    _log_ob(
                        _draft_body,
                        source='dispatcher.draft',
                        response_status=resp.status_code,
                        latency_ms=round((time.time() - _draft_start) * 1000, 1),
                    )
                except Exception:
                    pass
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

        GUARDRAIL: Only nodes with PROVEN integrity get credit
        (integrity_status == 'verified', written only by a passed challenge).
        master_key_verified is derived from a self-reported code_hash, so
        paying on it meant paying on an assertion any node can make.
        GUARDRAIL: ComputeDemocracy.adjusted_reward() — logarithmic, not linear.
        """
        if not node_id:
            return
        try:
            from integrations.social.models import get_db, PeerNode
            db = get_db()
            try:
                peer = db.query(PeerNode).filter_by(node_id=node_id).first()
                if peer and peer.integrity_status == 'verified':
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
