"""One stateless question to the node's own model, answered as a JSON object.

The model this node's turns run on (core.autogen_config.llm_http_target), so
it works where there is no local model too, with the reply held to a JSON
schema by the server and thinking off on a local model.  Nothing is posted
into any group chat, so no later turn reads the question.  Same call as
hartos.ai_key_vault._ask_model, which asks about a credential request.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def ask_model_json(content: str, schema: dict, name: str, max_tokens: int = 160,
                   timeout: float = 30.0) -> Optional[dict]:
    """The model's reply to ``content`` as the object ``schema`` describes, or
    None when it cannot be asked or the reply is not that object."""
    try:
        from core.autogen_config import llm_http_target, llm_stays_on_premises
        from core.http_pool import pooled_post
        url, headers, model = llm_http_target()
        body = {'model': model, 'temperature': 0, 'max_tokens': max_tokens,
                'messages': [{'role': 'user', 'content': content}],
                'response_format': {'type': 'json_schema', 'json_schema': {
                    'name': name, 'strict': True, 'schema': schema}}}
        if llm_stays_on_premises():
            from core.constants import LLM_THINKING_OFF_KWARGS
            body['chat_template_kwargs'] = dict(LLM_THINKING_OFF_KWARGS)
        resp = pooled_post(url, headers=headers, json=body, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f'HTTP {resp.status_code}')
        text = ((resp.json().get('choices') or [{}])[0].get('message') or {}).get(
            'content') or ''
    except Exception as e:
        logger.warning("%s: the model could not be asked (%s)", name, e)
        return None
    try:
        reply = json.loads(text)
    except (TypeError, ValueError):  # TypeError: content as a list of parts, not text
        return None
    return reply if isinstance(reply, dict) else None
