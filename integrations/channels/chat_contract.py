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
"""
from __future__ import annotations

from typing import Any, Dict


def chat_request_fields(content: str) -> Dict[str, str]:
    """The dual /chat REQUEST keys — send BOTH so either handler accepts it."""
    return {"prompt": content, "text": content}


def chat_reply(result: Any, default: str = "") -> str:
    """The dual /chat RESPONSE keys — read EITHER ('response' = HARTOS,
    'text' = bundled Nunba chat_route); fall back to ``default``."""
    if not isinstance(result, dict):
        return default
    return result.get("response") or result.get("text") or default
