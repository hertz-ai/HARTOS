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
    ``prompt_id`` and both request keys.  The body carries a request_id
    (core.chat_client.normalize_chat_body: one a field names passes through,
    else one is minted), so /chat takes it as a person's turn -- ahead of
    the daemons, which yield to it -- and not as background work, which is
    what a turn with no id is (dispatch.is_genuine_user_request; the steward
    rule of 2026-08-09: a conversation-backed turn is priority).

    The headers carry the person's own identity, minted with role 'user' by
    the one internal-auth helper
    (agent_engine.dispatch._internal_auth_headers): /chat trusts the token
    over the body, so a default identity would put every person's turn in
    one shared agent session (its docstring has the 2026-08-06 incident), and
    a person sent for is never an administrator.  A token is minted on every
    tier (or HEVOLVE_API_KEY sent); None only when minting fails (logged):
    central and regional then answer 401.
    """
    from core.chat_client import normalize_chat_body
    payload = {"user_id": user_id, "prompt_id": prompt_id,
               **chat_request_fields(content)}
    payload.update(fields)
    payload = normalize_chat_body(payload)
    try:
        from integrations.agent_engine.dispatch import _internal_auth_headers
        headers = _internal_auth_headers(user_id=str(user_id), role='user')
    except Exception as e:  # never block a turn on this
        logger.warning(
            "internal auth header unavailable, calling /chat "
            "unauthenticated (central/regional will answer 401): %s", e)
        headers = None
    return payload, headers


def chat_turn_answer(status: int, body: Any) -> Optional[str]:
    """The reply in a /chat answer when the turn produced one, else None.

    Not a reply, whatever its words: a non-200; an answer that says it
    failed -- 'success' False or an 'error' (Nunba's busy, starting and
    refusal notices are HTTP 200 with both), still loading ('loading', or
    the adapter's source 'hartos_loading', the notice
    dispatch.local_chat_dispatch also refuses to count), or a route speaking
    for itself (source 'system': Nunba's model-setup card); and a failure
    sentence dressed as a reply (core.agent_tools.is_user_facing_error,
    HARTOS's own).  For callers that must not pass such words off as the
    agent's -- a call speaks the reply in the agent's voice.
    """
    if status != 200 or not isinstance(body, dict):
        return None
    if (body.get('success') is False or body.get('error') or body.get('loading')
            or body.get('source') in ('hartos_loading', 'system')):
        return None
    reply = chat_reply(body).strip()
    if not reply:
        return None
    from core.agent_tools import is_user_facing_error
    return None if is_user_facing_error(reply) else reply


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
