"""Canonical token-count helpers for LLM prompt budgeting.

Single source of truth — replaces inline tiktoken+fallback chains
that were duplicated in:

  * ``integrations.agent_engine.budget_gate.estimate_llm_cost_spark``
    — paid-model cost estimation (gpt-4o, claude-sonnet, etc.).
  * ``core.llm_outbound_logger`` (wire-layer hard left-trim) — local
    llama-server context-overflow guard.
  * ``hart_intelligence_entry`` (multiple sites, lines 1454, 4949,
    5187, 5234) — request/response token bookkeeping.  Pre-existing,
    not migrated in this commit; targeted for follow-up.

Two functions:
  - ``count_tokens_for_text(text, model=None)`` → int
  - ``count_tokens_for_messages(messages, model=None)`` → int

Both prefer the ``tiktoken`` library (accurate for GPT BPE; close enough
for Qwen / Llama-3 BPE since they share most byte-pair patterns).
Fallback to a fast approximation (``chars / 3.5 + 4 overhead/msg``)
when tiktoken is unavailable — the wire-layer chokepoint must work in
cx_Freeze bundles where adding deps is non-trivial; the fallback is
within ~15 % of the tiktoken result for English prose.

Encodings are cached per (model, encoding-name) so successive calls
don't pay the encoding-load cost (~10 ms for cl100k_base).
"""
from __future__ import annotations

import json
from typing import Any, List, Optional

# Tokens per OpenAI message envelope (role + separator).  Matches
# OpenAI's published tokenizer accounting for chat completions.
_TOKENS_PER_MESSAGE_OVERHEAD = 4

# Fast-fallback approximation: avg chars per token across English /
# mixed multilingual content.  Empirical for Qwen and GPT BPE.
# Conservative on purpose — over-estimates by ~10-20 % so budgeting
# leaves headroom.
_CHARS_PER_TOKEN_FALLBACK = 3.5

# Cache the tiktoken encoding object per model.  Loading is ~10ms;
# encoding calls themselves are ~µs so the cache pays off after the
# first call.  ``None`` is a sentinel meaning "tried, tiktoken
# unavailable, use fallback for everything".
_ENC_CACHE: dict = {}
_TIKTOKEN_OK: Optional[bool] = None  # tri-state: None=untested, True/False=tested


def _get_encoding(model: Optional[str] = None):
    """Return a tiktoken encoding object or None if unavailable.

    Single cache for the whole process.  Defaults to ``cl100k_base``
    (GPT-4 / GPT-3.5 turbo's encoding) when no model name is supplied
    or the model is unknown to tiktoken — that's the closest commonly-
    available BPE to what Qwen and Llama use.
    """
    global _TIKTOKEN_OK
    if _TIKTOKEN_OK is False:
        return None
    cache_key = model or '__default__'
    if cache_key in _ENC_CACHE:
        return _ENC_CACHE[cache_key]
    try:
        import tiktoken
        _TIKTOKEN_OK = True
        try:
            enc = tiktoken.encoding_for_model(model) if model else None
        except Exception:
            enc = None
        if enc is None:
            enc = tiktoken.get_encoding('cl100k_base')
        _ENC_CACHE[cache_key] = enc
        return enc
    except Exception:
        _TIKTOKEN_OK = False
        return None


def count_tokens_for_text(text: Any, model: Optional[str] = None) -> int:
    """Count tokens in a single text blob.  Always returns a non-
    negative int; never raises.  Empty / None → 0.
    """
    if not text:
        return 0
    s = text if isinstance(text, str) else str(text)
    enc = _get_encoding(model)
    if enc is not None:
        try:
            return len(enc.encode(s))
        except Exception:
            pass  # fall through to approximation
    return max(0, int(len(s) / _CHARS_PER_TOKEN_FALLBACK))


def _content_to_text(content: Any) -> str:
    """OpenAI message ``content`` can be str OR list-of-parts
    (multimodal).  Extract the text portion only — image parts are
    sent as URL refs (tiny) and don't contribute meaningfully to
    prompt-budget pressure.
    """
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(
            (p.get('text', '') or '') for p in content
            if isinstance(p, dict) and p.get('type') == 'text'
        )
    return str(content)


def count_tokens_for_messages(messages: List[dict],
                              model: Optional[str] = None) -> int:
    """Count tokens for an OpenAI chat-completions ``messages`` array.

    Includes the per-message envelope overhead (4 tokens) plus any
    ``tool_calls`` / ``function_call`` / ``name`` fields that serialize
    into the prompt.  Multimodal-aware (skips image URL bulk).
    """
    total = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        text = _content_to_text(m.get('content'))
        for extra_key in ('tool_calls', 'function_call', 'name'):
            v = m.get(extra_key)
            if v:
                text += json.dumps(v) if not isinstance(v, str) else v
        total += count_tokens_for_text(text, model) + _TOKENS_PER_MESSAGE_OVERHEAD
    return total
