"""Verified-signal LLM health check.

Why this exists
---------------
Shallow-signal health checks lie:
- /health returns 200 when llama-server is up but no model is loaded
- /v1/models returns the catalog entry even if inference is broken
- process-alive is a proxy for "maybe working" — not a guarantee

This module issues a real inference — POST /v1/chat/completions with
a minimal prompt — and asserts the response contains non-empty text.
That is the only "yes it works" signal that matters.

Symptom class
-------------
Mirrors the verified_ready pattern used for TTS (see commit b84437d
on Nunba: runtime verification of Indic Parler). Same root cause:
success at one layer does not imply capability at the next layer.

API
---
    is_llm_inference_verified(url, timeout=5.0) -> bool
    verify_llm(url, timeout=5.0) -> dict  # detail diag

Both accept:
- url: full http://host:port base; defaults to http://127.0.0.1:8080
- timeout: total seconds for the HTTP call

The bool API is a drop-in replacement for is_llm_available().
The dict API returns {ok, reason, http_status, content_snippet,
elapsed_ms} for deeper triage.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Exposed for tests / monkey-patch
DEFAULT_PROBE_PROMPT = "hi"
DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_TIMEOUT = 5.0
# Reasoning models (Qwen3.5 thinking-mode, the shipped default) spend their
# whole budget on reasoning_content before emitting any answer content.  A
# 4-token budget left content='' and made a healthy model look dead, so give
# the probe enough room to emit a real token or two of answer content while
# still staying fast (this runs on a 2–30s poll cadence).  reasoning_content
# is also accepted as proof-of-life below, so this bump is belt-and-suspenders.
DEFAULT_MAX_TOKENS = 16


def _probe_payload(prompt: str, max_tokens: int) -> bytes:
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
    }
    return json.dumps(body).encode("utf-8")


def _extract_content(data: Dict[str, Any]) -> str:
    """Pull text out of an OpenAI-compatible /v1/chat/completions response.

    Handles the common shapes:
    - {'choices': [{'message': {'content': '...'}}]}
    - {'choices': [{'message': {'reasoning_content': '...'}}]}  (reasoning
      models — see below)
    - {'choices': [{'text': '...'}]}  (legacy /v1/completions)

    Returns '' if no content can be found.
    """
    choices = data.get("choices") or []
    if not choices:
        return ""
    first = choices[0] or {}
    msg = first.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content
    # Reasoning models (Qwen3.5 thinking-mode — the shipped default) emit
    # reasoning_content while `content` stays empty under a small max_tokens
    # budget (finish_reason='length').  A non-empty reasoning stream is still
    # proof the model is loaded and generating tokens — which is exactly what
    # this liveness probe asserts.  Without accepting it, a fully healthy
    # reasoning model is reported unavailable and every downstream gate
    # (is_llm_available → /api/llm/status available:false → the React chat
    # send-gate) latches off that false negative and the chat queue strands.
    reasoning = msg.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    # legacy /v1/completions style
    text = first.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return ""


def verify_llm(
    url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    prompt: str = DEFAULT_PROBE_PROMPT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Dict[str, Any]:
    """Verified-signal health check via real inference.

    Returns dict:
    - ok (bool): True iff the LLM produced non-empty content
    - reason (str): human-readable failure class
    - http_status (int | None): HTTP status code if reachable
    - content_snippet (str): first 64 chars of the reply (on ok=True)
    - elapsed_ms (int): end-to-end time
    """
    endpoint = url.rstrip("/") + "/v1/chat/completions"
    payload = _probe_payload(prompt, max_tokens)
    started = time.monotonic()

    result: Dict[str, Any] = {
        "ok": False,
        "reason": "unknown",
        "http_status": None,
        "content_snippet": "",
        "elapsed_ms": 0,
    }

    try:
        req = urllib.request.Request(
            endpoint,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result["http_status"] = resp.status
            if resp.status != 200:
                result["reason"] = f"http_{resp.status}"
                return result
            raw = resp.read()
    except urllib.error.HTTPError as e:
        result["http_status"] = e.code
        result["reason"] = f"http_{e.code}"
        return result
    except urllib.error.URLError as e:
        result["reason"] = f"unreachable:{e.reason}"
        return result
    except Exception as e:  # timeout, socket, malformed URL
        result["reason"] = f"exception:{type(e).__name__}"
        return result
    finally:
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)

    # Parse body
    try:
        data = json.loads(raw)
    except Exception:
        result["reason"] = "malformed_json"
        return result

    content = _extract_content(data)
    if not content:
        result["reason"] = "empty_content"
        return result

    result["ok"] = True
    result["reason"] = "verified"
    result["content_snippet"] = content[:64]
    return result


def is_llm_inference_verified(
    url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Boolean drop-in for is_llm_available().

    Returns True iff a real /v1/chat/completions probe produced
    non-empty content within `timeout` seconds.
    """
    try:
        return verify_llm(url=url, timeout=timeout)["ok"]
    except Exception as exc:  # defense-in-depth — never raise
        logger.debug("verify_llm raised unexpectedly: %s", exc)
        return False


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_PROBE_PROMPT",
    "DEFAULT_TIMEOUT",
    "DEFAULT_MAX_TOKENS",
    "is_llm_inference_verified",
    "verify_llm",
]
