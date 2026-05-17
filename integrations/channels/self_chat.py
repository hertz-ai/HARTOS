"""Self-chat handler — treat an owner's "Message Yourself" thread
as a private notebook-to-agent channel.

When the Nunba owner messages their own WhatsApp number (sender_id ==
owner_phone), we skip UserChannelBinding lookup and pairing flow
entirely, persist the message to the MemoryGraph, dispatch to the
owner's default agent, and reply in the same thread. Replies are NOT
fanned out to other bound channels — the self-chat is private by
design.

CLAUDE.md gates:
  * Gate 2 (DRY): uses existing MemoryGraph + response router; no
    parallel memory or send path is introduced.
  * Gate 3 (SRP): detection + routing only. Does NOT own the chat
    pipeline, the agent API, or WAMP push — it calls into them.
  * Gate 4 (no parallel paths): one caller (FlaskChannelIntegration).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional, TYPE_CHECKING

import requests
from core.http_pool import pooled_post

if TYPE_CHECKING:  # avoid runtime import cycle
    from .base import Message
    from .registry import ChannelRegistry
    from .session_manager import SessionManager
    from .response.router import ChannelResponseRouter

logger = logging.getLogger(__name__)


# E.164 digits only. Strips "+", "@c.us", "@s.whatsapp.net", spaces, dashes.
_PHONE_STRIP = re.compile(r"[+\-\s]|@[a-z.]+$", re.IGNORECASE)


def normalize_phone(s: Optional[str]) -> str:
    """Reduce a phone / JID to its pure digit form for equality checks.

    Examples:
        "+1 555 123 4567"            -> "15551234567"
        "15551234567@c.us"           -> "15551234567"
        "15551234567@s.whatsapp.net" -> "15551234567"
    """
    if not s:
        return ""
    return _PHONE_STRIP.sub("", str(s)).strip()


class SelfChatHandler:
    """Routes self-chat messages to a private notebook-to-agent flow.

    Usage from FlaskChannelIntegration._handle_message::

        if self._self_chat.is_self_message(message):
            return self._self_chat.handle(message, session)
    """

    def __init__(
        self,
        *,
        agent_api_url: str,
        owner_user_id: int,
        owner_prompt_id: int,
        device_id: Optional[str],
        session_manager: "SessionManager",
        response_router: "ChannelResponseRouter",
        registry: "ChannelRegistry",
        memory_user_id: Optional[str] = None,
        get_loop=None,
    ) -> None:
        self.agent_api_url = agent_api_url
        self.owner_user_id = owner_user_id
        self.owner_prompt_id = owner_prompt_id
        self.device_id = device_id
        self.session_manager = session_manager
        self.response_router = response_router
        self.registry = registry
        # MemoryGraph user key (defaults to owner user_id stringified)
        self.memory_user_id = memory_user_id or str(owner_user_id)
        # Callable returning the async loop that owns the adapters
        # (FlaskChannelIntegration._loop). Deferred because the loop
        # is spawned in a daemon thread after __init__.
        self._get_loop = get_loop or (lambda: None)

    # ── Detection ─────────────────────────────────────────────────
    def is_self_message(self, message: "Message") -> bool:
        """True iff this message's sender matches the owner's phone as
        configured on the adapter *and* the feature is enabled on that
        adapter (``config.extra['enable_self_chat_agent']`` default True).
        """
        adapter = self.registry.get(message.channel)
        if adapter is None:
            return False
        extra = getattr(adapter.config, "extra", None) or {}
        if extra.get("enable_self_chat_agent", True) is False:
            return False
        owner = extra.get("owner_phone") or extra.get("phone_number")
        if not owner:
            return False
        return (
            bool(message.sender_id)
            and normalize_phone(message.sender_id) == normalize_phone(owner)
        )

    # ── Handling ──────────────────────────────────────────────────
    def handle(self, message: "Message", session) -> Optional[str]:
        """Persist → dispatch → reply in-thread. Returns the reply text
        (also already sent via adapter.send_message).

        Returns None on a fatal routing failure (so the outer adapter
        can log + surface a generic error to the user)."""
        # 1. Persist to MemoryGraph (best-effort; never block routing)
        self._persist_note(message)

        # 2. Track in session history (same as normal path)
        if session is not None:
            try:
                session.add_message("user", message.content)
            except Exception:  # noqa: BLE001
                logger.debug("self-chat session.add_message failed", exc_info=True)

        # 3. Dispatch to agent API with is_self_chat marker
        payload = {
            "user_id": self.owner_user_id,
            "prompt_id": self.owner_prompt_id,
            "prompt": message.content,
            "create_agent": False,  # self-chat never needs agent creation
            "device_id": self.device_id,
            "channel_context": {
                "channel": message.channel,
                "sender_id": message.sender_id,
                "sender_name": message.sender_name,
                "chat_id": message.chat_id,
                "is_group": message.is_group,
                "is_self_chat": True,   # lets chat hot path apply notebook heuristics
                "message_id": message.id,
            },
        }
        try:
            resp = pooled_post(self.agent_api_url, json=payload, timeout=120)
        except requests.Timeout:
            logger.error("self-chat agent timeout")
            return "(timed out — try again)"
        except Exception as e:  # noqa: BLE001
            logger.error("self-chat agent call failed: %s", e)
            return None

        if resp.status_code != 200:
            logger.error("self-chat agent %s: %s", resp.status_code, resp.text[:200])
            return None

        try:
            reply = (resp.json() or {}).get("response") or ""
        except Exception:  # noqa: BLE001
            reply = ""
        reply = reply or "✓ noted"

        # 4. Track assistant turn + log user message (no fan-out: private)
        if session is not None:
            try:
                session.add_message("assistant", reply)
            except Exception:  # noqa: BLE001
                logger.debug("self-chat session.add_message assistant failed",
                             exc_info=True)
        try:
            self.response_router.log_user_message(
                self.owner_user_id, message.channel, message.content,
            )
        except Exception:  # noqa: BLE001
            logger.debug("self-chat log_user_message failed", exc_info=True)

        # 5. Reply in the same WhatsApp thread (chat_id = owner @c.us).
        #    We reach into the adapter directly — fan_out=False is
        #    explicit: the self-chat is private and must not leak to
        #    other channels the owner has bound (Telegram, Discord…).
        self._send_reply_in_thread(message, reply)
        return reply

    # ── Internals ─────────────────────────────────────────────────
    def _persist_note(self, message: "Message") -> None:
        """Write the note to MemoryGraph with memory_type='self_note'."""
        try:
            from core.platform_paths import get_memory_graph_dir
            from integrations.memory.memory_graph import MemoryGraph  # type: ignore
        except Exception:  # noqa: BLE001
            logger.debug("self-chat persist: MemoryGraph unavailable", exc_info=True)
            return
        try:
            session_key = f"self_chat:{self.memory_user_id}"
            db_path = get_memory_graph_dir(session_key)
            mg = MemoryGraph(db_path=db_path, user_id=self.memory_user_id)
            mg.register(
                content=message.content,
                metadata={
                    "memory_type": "self_note",
                    "source_agent": "self_chat",
                    "session_id": session_key,
                    "channel": message.channel,
                    "message_id": message.id,
                },
                context_snapshot=f"Self-chat note via {message.channel}",
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("self-chat persist failed: %s", e)

    def _send_reply_in_thread(self, message: "Message", reply: str) -> None:
        """Send ``reply`` via ``registry.send_to_channel`` onto the
        integration's asyncio loop. No fan-out — this is a private thread.
        """
        adapter = self.registry.get(message.channel)
        if adapter is None:
            logger.warning("self-chat: adapter '%s' gone", message.channel)
            return
        chat_id = message.chat_id or message.sender_id
        coro = self.registry.send_to_channel(message.channel, chat_id, reply)

        loop = self._get_loop()
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(coro, loop)
                return
            except Exception as e:  # noqa: BLE001
                logger.error("self-chat schedule on loop failed: %s", e)

        # Fallback: no running adapter loop — create a short-lived one.
        # Rare path; normal Nunba boot attaches a loop via
        # FlaskChannelIntegration.start().
        try:
            asyncio.run(coro)
        except RuntimeError:
            logger.error("self-chat send dropped — no async loop available")
        except Exception as e:  # noqa: BLE001
            logger.error("self-chat direct-send failed: %s", e)
