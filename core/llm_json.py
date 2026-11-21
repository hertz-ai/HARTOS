"""One stateless question to the node's own model, answered as a JSON object.

The model this node's turns run on (core.autogen_config.llm_http_target), so
it works where there is no local model too, with the reply held to a JSON
schema by the server and thinking off on a local model.  Nothing is posted
into any group chat, so no later turn reads the question.  Same call as
hartos.ai_key_vault._ask_model, which asks about a credential request.

The call goes out through requests (core.http_pool.pooled_post), which the
outbound hook does not see (it patches httpx and urllib), so it is logged
here through that hook's own writer, core.llm_outbound_logger.log_outbound:
one line in the outbound log and one 'llm_call' line on the audit trail
for each SEND (pooled_post can send a local model call twice; the sends are
counted by core.http_pool.counting_sends), like every other model call.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


def ask_model_json(content: str, schema: dict, name: str, max_tokens: int = 160,
                   timeout: float = 30.0, once: bool = False) -> Optional[dict]:
    """The model's reply to ``content`` as the object ``schema`` describes, or
    None when it cannot be asked or the reply is not that object.

    ``once``: the call is sent at most once (pooled_post's ``resend=False``),
    so ``timeout`` bounds the wait for the reply; for a caller that tells
    someone how long it waited (core.persona_registry)."""
    body, status, started, sends = None, None, time.monotonic(), []
    try:
        from core.autogen_config import llm_http_target, llm_stays_on_premises
        from core.http_pool import counting_sends, pooled_post
        url, headers, model = llm_http_target()
        body = {'model': model, 'temperature': 0, 'max_tokens': max_tokens,
                'messages': [{'role': 'user', 'content': content}],
                'response_format': {'type': 'json_schema', 'json_schema': {
                    'name': name, 'strict': True, 'schema': schema}}}
        if llm_stays_on_premises():
            from core.constants import LLM_THINKING_OFF_KWARGS
            body['chat_template_kwargs'] = dict(LLM_THINKING_OFF_KWARGS)
        with counting_sends() as sends:
            if once:
                resp = pooled_post(url, headers=headers, json=body, timeout=timeout,
                                   resend=False)
            else:
                resp = pooled_post(url, headers=headers, json=body, timeout=timeout)
        status = resp.status_code
        _log_earlier_sends(body, name, sends)
        _log_call(body, name, status, started)
        if resp.status_code != 200:
            raise RuntimeError(f'HTTP {resp.status_code}')
        text = ((resp.json().get('choices') or [{}])[0].get('message') or {}).get(
            'content') or ''
    except Exception as e:
        if body is not None and status is None:
            _log_earlier_sends(body, name, sends)
            _log_call(body, name, None, started, error=type(e).__name__)
        logger.warning("%s: the model could not be asked (%s)", name, e)
        return None
    try:
        reply = json.loads(text)
    except (TypeError, ValueError):  # TypeError: content as a list of parts, not text
        return None
    return reply if isinstance(reply, dict) else None


def _log_earlier_sends(body, name, sends) -> None:
    """One record for each send before the last one: a send that failed and
    was sent again inside pooled_post, which answers for the last only."""
    sends = list(sends)
    for sent_at, sent_again_at in zip(sends, sends[1:]):
        _log_call(body, name, None, sent_at, error='sent again after this send failed',
                  ended=sent_again_at)


def _log_call(body, name, status, started, error=None, ended=None) -> None:
    """One outbound-log record for a call ask_model_json made (log_outbound
    never raises; neither does this)."""
    try:
        from core.llm_outbound_logger import log_outbound
        log_outbound(body, response_status=status,
                     latency_ms=round(((ended or time.monotonic()) - started) * 1000.0, 1),
                     source=f'llm_json.{name}', response_error=error)
    except Exception:
        logger.debug("%s: the call was not logged", name, exc_info=True)
