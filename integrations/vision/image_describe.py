"""Describe an image file with this node's local vision model.

The one-off description of an image a user attached, on the main model:
Nunba's /upload/vision, /upload/file and /upload/native image uploads, which
import it from here. Book pages do not come here: they go through the node's
vision backend (integrations/vision/lightweight_backend.py, read_document),
the one camera, screen and media captions use.

Moved from Nunba routes/upload_routes.py (_describe_image_via_llm) on
2026-09-13 so that a HARTOS node running without Nunba can read images at all.
Nunba keeps no copy.

What the move changed:
  * Transport: core.http_pool.pooled_post instead of a bare requests.post. A
    local llama completion through pooled_post is admitted by the priority
    scheduler (core.llama_scheduler) instead of competing first-come-first-
    served.
  * Endpoint: core.port_registry.get_local_llm_url(), the canonical resolver,
    which follows the llama-server when it moves port. The old private
    LLAMA_CPP_URL read had no writer anywhere in either repo.
  * Read timeout: http_pool.LLM_COMPLETION_TIMEOUT, the canonical budget for
    an LLM completion, instead of an inline 60 s.

What it did NOT change: the request body (thinking off, 1024-token headroom,
temperature 0.3), the empty-content warning, and the return contract --
stripped text, '' when the model answered with nothing, None on failure.

WHY THINKING IS OFF (measured live 2026-08-04 on the Nunba vision route):
Qwen3.5 is a hybrid reasoning model. It writes chain-of-thought into a separate
`reasoning_content` field and only afterwards fills `content`, which is what is
read here. With the classification prompt, max_tokens=300 ended with
finish=length, content=0 chars, reasoning=1242 chars: an empty answer and no
error at all. Turning thinking off took reasoning 760 -> 0 chars and made the
call faster. `reasoning_effort: "none"` was also tried and is NOT honoured by
this server; do not substitute it. Nunba's spawn-time env var covers only a
llama-server Nunba started; this per-request kwarg travels with the payload,
so it also covers an external, remote or cloud endpoint.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: The prompt when a caller gives none: describe and classify, as JSON.
DEFAULT_PROMPT = (
    "Describe this image concisely. Classify it as one of: "
    "'academic content', 'animated/cartoon', 'art/illustration', "
    "'real-world photograph', 'screenshot', 'diagram/chart', or 'other'. "
    "Respond as JSON: {\"description\": \"...\", \"category\": \"...\"}"
)

#: Headroom, not the fix: 300 suffices once thinking is off, but a future
#: model or a longer prompt should degrade to slow, not empty.
MAX_TOKENS = 1024

_MIME = {'jpg': 'jpeg', 'jpeg': 'jpeg', 'png': 'png',
         'gif': 'gif', 'webp': 'webp', 'bmp': 'bmp'}


def describe_image(image_path, prompt: Optional[str] = None) -> Optional[str]:
    """Send one image file to the local vision model and return its reply.

    Returns the stripped reply; '' when the model answered with no content
    (logged as a warning -- the signature of a spent thinking budget); None
    when the image cannot be read, the model cannot be reached, or it answers
    with a non-200.
    """
    import requests
    from core.constants import LLM_THINKING_OFF_KWARGS
    from core.http_pool import LLM_COMPLETION_TIMEOUT, pooled_post
    from core.port_registry import get_local_llm_url

    try:
        with open(image_path, 'rb') as f:
            img_bytes = f.read()
        ext = Path(image_path).suffix.lower().lstrip('.')
        b64 = base64.b64encode(img_bytes).decode('ascii')
        data_url = f"data:image/{_MIME.get(ext, 'jpeg')};base64,{b64}"
    except Exception as e:
        logger.error(f"Failed to read image for vision: {e}")
        return None

    payload = {
        "model": "qwen",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt or DEFAULT_PROMPT},
                ],
            }
        ],
        "chat_template_kwargs": dict(LLM_THINKING_OFF_KWARGS),
        "max_tokens": MAX_TOKENS,
        "temperature": 0.3,
    }

    url = get_local_llm_url().rstrip('/') + '/chat/completions'
    try:
        resp = pooled_post(url, json=payload, timeout=LLM_COMPLETION_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            choice = (data.get('choices') or [{}])[0]
            message = choice.get('message') or {}
            content = (message.get('content') or '').strip()
            if not content:
                # THE signature of a spent thinking budget. Never silent: an
                # empty description used to surface as
                # {"category":"unknown","description":""} with nothing logged.
                logger.warning(
                    "Vision inference produced EMPTY content "
                    "(finish_reason=%s, reasoning_content=%d chars). The model "
                    "likely spent the whole max_tokens=%s budget thinking; "
                    "raise the budget or keep enable_thinking disabled.",
                    choice.get('finish_reason'),
                    len(message.get('reasoning_content') or ''),
                    payload.get('max_tokens'),
                )
            return content
        logger.warning(f"Vision inference returned {resp.status_code}: {resp.text[:200]}")
    except requests.ConnectionError:
        logger.info("Local vision model not reachable — skipping vision inference")
    except Exception as e:
        logger.warning(f"Vision inference failed: {e}")
    return None
