"""Single source for the dual /chat request+response contract.

Two handlers answer POST /chat depending on topology, and an inbound channel
message must work against EITHER:

  - standalone HARTOS  (hart_intelligence_entry.chat):  request key 'prompt',
    response key 'response'.
  - bundled Nunba desktop (routes/chatbot_routes.chat_route, which shadows
    HARTOS's /chat on :5000):  request key 'text', response key 'text'.

So the bridge sends BOTH request keys and reads EITHER response key.  Both
inbound paths — FlaskChannelIntegration._handle_message and SelfChatHandler —
go through here, so the contract lives in exactly ONE place (no parallel
prompt-only / response-only path that silently breaks on the bundled app).

Verified live against the INSTALLED bundled Nunba: a prompt-only payload 400'd
"Text is required", and a response-only read fell back to the canned reply.

A person's turn sent to this node's own /chat on their behalf -- a channel
message (FlaskChannelIntegration.run_turn), a word spoken in a call
(agentic_router) -- is built by chat_turn_request and read by
chat_turn_result, so every such caller sends the same body and the same
credentials and reads the answer the same way.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: Default seconds a /chat caller waits for a full agent turn.
DEFAULT_AGENT_TURN_TIMEOUT_S = 120


def chat_request_fields(content: str) -> Dict[str, str]:
    """The dual /chat REQUEST keys — send BOTH so either handler accepts it."""
    return {"prompt": content, "text": content}


def chat_reply(result: Any, default: str = "") -> str:
    """The dual /chat RESPONSE keys — read EITHER ('response' = HARTOS,
    'text' = bundled Nunba chat_route); fall back to ``default``."""
    if not isinstance(result, dict):
        return default
    return result.get("response") or result.get("text") or default


def chat_turn_request(user_id, prompt_id, content: str,
                      **fields) -> Tuple[Dict[str, Any], Optional[Dict[str, str]]]:
    """The /chat body and headers of one person's turn, sent for them.

    ``fields`` are the request's other /chat keys (create_agent,
    channel_context, media_mode, ...), added after the person, the agent
    ``prompt_id`` and both request keys.  The headers carry the person's own
    identity, minted with role 'user' by the one internal-auth helper
    (agent_engine.dispatch._internal_auth_headers): /chat trusts the token
    over the body, so a default identity would put every person's turn in
    one shared agent session (its docstring has the 2026-08-06 incident), and
    a person sent for is never an administrator.  None on a flat node, where
    no header is needed, or when minting fails (logged): central and regional
    then answer 401.
    """
    payload = {"user_id": user_id, "prompt_id": prompt_id,
               **chat_request_fields(content)}
    payload.update(fields)
    try:
        from integrations.agent_engine.dispatch import _internal_auth_headers
        headers = _internal_auth_headers(user_id=str(user_id), role='user')
    except Exception as e:  # never block a turn on this
        logger.warning(
            "internal auth header unavailable, calling /chat "
            "unauthenticated (central/regional will answer 401): %s", e)
        headers = None
    return payload, headers


def chat_turn_result(response) -> Tuple[int, Dict[str, Any]]:
    """``(status, body)`` of a /chat answer.  A body that is not /chat's JSON
    object (a proxy's or server's error page) keeps its words, cut at 500
    characters, under 'error'."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {'error': str(getattr(response, 'text', '') or '')[:500]}
    return response.status_code, body


def agent_turn_timeout() -> int:
    """Seconds a caller of /chat waits for a full agent turn.

    ONE budget for every client of the same multi-agent turn (channel inbound,
    self-chat, the speculative dispatcher's local expert re-entry), read at call
    time so HEVOLVE_CHANNEL_AGENT_TIMEOUT applies to all of them alike.
    """
    from core.config_cache import env_int
    return env_int('HEVOLVE_CHANNEL_AGENT_TIMEOUT',
                   DEFAULT_AGENT_TURN_TIMEOUT_S, minimum=1)
