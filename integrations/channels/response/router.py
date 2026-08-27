"""
Channel Response Router — Routes agent responses to originating and bound channels.

Handles:
1. Reply to the originating channel (where the message came from)
2. Fan-out to user's other active channel bindings (preferred first)
3. WAMP notification to desktop/web clients
4. ConversationEntry logging for unified history
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class ChannelResponseRouter:
    """Routes agent responses back through channels + WAMP + DB logging."""

    def __init__(self, registry=None):
        self._registry = registry
        self._db_session_factory = None

    def _get_registry(self):
        if self._registry is None:
            from integrations.channels.registry import get_registry
            self._registry = get_registry()
        return self._registry

    def _get_db(self):
        if self._db_session_factory is None:
            from integrations.social.models import get_db
            self._db_session_factory = get_db
        return self._db_session_factory()

    @staticmethod
    def _get_send_loop():
        """Return the running asyncio loop channel adapters are on.

        The loop lives on ``FlaskChannelIntegration`` (its
        ``_run_async_loop`` background thread) — NOT on
        ``ChannelRegistry``, which has no ``_loop`` attribute at all.
        A caller on a worker thread has no loop of its own, so
        ``asyncio.get_event_loop()`` can't find it either; the
        integration singleton is the only correct source (same fix
        applied to speculative_dispatcher's channel delivery leg,
        2026-08-26/27).
        """
        try:
            from integrations.channels.flask_integration import (
                get_channel_integration)
            loop = getattr(get_channel_integration(), '_loop', None)
            return loop if loop and loop.is_running() else None
        except Exception:
            return None

    def route_response(
        self,
        user_id,
        response_text: str,
        channel_context: Optional[Dict[str, Any]] = None,
        agent_id: Optional[str] = None,
        fan_out: bool = True,
    ):
        """
        Route an agent response to all relevant destinations.

        Args:
            user_id: The user who sent the original message
            response_text: Agent's response text
            channel_context: Originating channel info (channel, chat_id, sender_id, etc.)
            agent_id: Optional agent ID for conversation logging
            fan_out: Whether to send to other bound channels (not just originating)
        """
        originating_channel = None
        originating_chat_id = None

        if channel_context:
            originating_channel = channel_context.get('channel')
            originating_chat_id = channel_context.get('chat_id')

        # 1. Log the assistant response
        self._log_conversation(
            user_id=user_id,
            channel_type=originating_channel or 'system',
            role='assistant',
            content=response_text,
            agent_id=agent_id,
        )

        # 2. Reply to the originating channel. This was documented at
        # the caller (agentic_router.py's dispatch_to_agent) as already
        # happening here, but never actually existed — route_response
        # only ever logged + fanned-out (explicitly EXCLUDING the
        # originating channel) + WAMP-notified the desktop, so a
        # channel-native reply (Slack/Discord/etc. message, not the
        # desktop app) never got its answer. Found live 2026-08-27:
        # get_ans-routed replies never reached Slack, confirmed via a
        # channel binding, not a Crossbar/desktop client.
        if originating_channel and originating_chat_id:
            self._send_to_originating(
                channel=originating_channel,
                chat_id=originating_chat_id,
                text=response_text,
            )

        # 3. Fan-out to bound channels (async, fire-and-forget)
        if fan_out:
            self._async_fan_out(
                user_id=user_id,
                text=response_text,
                exclude_channel=originating_channel,
                exclude_chat_id=originating_chat_id,
            )

        # 4. WAMP notification to desktop/web
        self._notify_desktop_wamp(
            user_id=user_id,
            text=response_text,
            channel_type=originating_channel,
        )

    def _send_to_originating(self, channel, chat_id, text):
        """Send the reply back to the channel/chat the message came from."""
        loop = self._get_send_loop()
        if not loop:
            logger.info(
                "Originating-channel reply skipped: channel=%s chat_id=%s "
                "— no running event loop", channel, chat_id,
            )
            return
        registry = self._get_registry()
        asyncio.run_coroutine_threadsafe(
            self._send_and_log(registry, channel, chat_id, text), loop,
        )

    @staticmethod
    async def _send_and_log(registry, channel, chat_id, text):
        try:
            result = await registry.send_to_channel(channel, chat_id, text)
            if not result.success:
                logger.warning(
                    "Originating-channel reply failed: channel=%s "
                    "chat_id=%s err=%s", channel, chat_id, result.error,
                )
        except Exception as e:
            logger.warning(
                "Originating-channel reply error: channel=%s chat_id=%s "
                "err=%s", channel, chat_id, e,
            )

    def log_user_message(
        self,
        user_id,
        channel_type: str,
        content: str,
        agent_id: Optional[str] = None,
    ):
        """Log an incoming user message to ConversationEntry."""
        self._log_conversation(user_id, channel_type, 'user', content, agent_id)

    def upsert_binding(
        self,
        user_id,
        channel_type: str,
        sender_id: str,
        chat_id: Optional[str] = None,
    ):
        """Auto-upsert a UserChannelBinding on every incoming channel message."""
        try:
            db = self._get_db()
            try:
                from integrations.social.models import UserChannelBinding
                existing = db.query(UserChannelBinding).filter_by(
                    user_id=str(user_id),
                    channel_type=channel_type,
                    channel_sender_id=sender_id,
                ).first()

                if existing:
                    existing.last_message_at = datetime.utcnow()
                    existing.is_active = True
                    if chat_id:
                        existing.channel_chat_id = chat_id
                else:
                    binding = UserChannelBinding(
                        user_id=str(user_id),
                        channel_type=channel_type,
                        channel_sender_id=sender_id,
                        channel_chat_id=chat_id,
                        is_active=True,
                        is_preferred=False,
                    )
                    db.add(binding)

                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning("Failed to upsert channel binding: %s", e)

    def _log_conversation(self, user_id, channel_type, role, content, agent_id=None):
        """Write a ConversationEntry row."""
        try:
            db = self._get_db()
            try:
                from integrations.social.models import ConversationEntry
                entry = ConversationEntry(
                    user_id=str(user_id),
                    channel_type=channel_type,
                    role=role,
                    content=content[:10000],  # cap at 10k chars
                    agent_id=agent_id,
                )
                db.add(entry)
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.debug("Failed to log conversation entry: %s", e)

    def _async_fan_out(self, user_id, text, exclude_channel=None, exclude_chat_id=None):
        """Fan-out response to all active bindings (fire-and-forget)."""
        try:
            db = self._get_db()
            try:
                from integrations.social.models import UserChannelBinding
                bindings = db.query(UserChannelBinding).filter_by(
                    user_id=str(user_id),
                    is_active=True,
                ).all()

                # Sort: preferred first
                bindings.sort(key=lambda b: (not b.is_preferred, b.channel_type))

                registry = self._get_registry()
                loop = self._get_send_loop()

                for binding in bindings:
                    # Skip the originating channel to avoid double-send
                    if (binding.channel_type == exclude_channel
                            and binding.channel_chat_id == exclude_chat_id):
                        continue
                    if not binding.channel_chat_id:
                        continue

                    # Schedule async send
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._send_to_binding(registry, binding, text),
                            loop,
                        )
                    else:
                        logger.debug("No event loop for fan-out to %s", binding.channel_type)
            finally:
                db.close()
        except Exception as e:
            logger.warning("Fan-out failed: %s", e)

    @staticmethod
    async def _send_to_binding(registry, binding, text):
        """Send response to a single channel binding."""
        try:
            result = await registry.send_to_channel(
                binding.channel_type,
                binding.channel_chat_id,
                text,
            )
            if not result.success:
                logger.debug("Fan-out to %s failed: %s", binding.channel_type, result.error)
        except Exception as e:
            logger.debug("Fan-out to %s error: %s", binding.channel_type, e)

    def _notify_desktop_wamp(self, user_id, text, channel_type=None):
        """Publish to WAMP for desktop/web notification.

        Singleton accessor — see core.safe_hartos_attr for why workers
        must not eager-import hart_intelligence.
        """
        try:
            from core.safe_hartos_attr import safe_hartos_attr
            publish_async = safe_hartos_attr('publish_async')
            if publish_async is None:
                logger.debug(
                    "Channel response WAMP notify skipped: HARTOS "
                    "publish_async unresolvable — user=%s channel=%s",
                    user_id, channel_type,
                )
                return
            notification = {
                "text": [text[:200]],
                "priority": 48,
                "action": "ChannelResponse",
                "channel": channel_type or "system",
                "historical_request_id": [],
                "options": [],
                "newoptions": [],
            }
            payload = json.dumps(notification)
            # Primary chat topic (existing desktop/web subscription)
            from core.peer_link.message_bus import chat_topic_for
            publish_async(chat_topic_for(user_id), payload)
            # Dedicated channel response topic (cross-device)
            publish_async(
                f'com.hertzai.hevolve.channel.response.{user_id}',
                payload,
            )
            logger.debug(
                "Channel response WAMP notify published: user=%s channel=%s",
                user_id, channel_type,
            )
        except Exception as e:
            logger.debug(
                "Channel response WAMP notify failed: user=%s err=%s",
                user_id, e,
            )


# Singleton
_router_instance = None


def get_response_router(registry=None) -> ChannelResponseRouter:
    """Get or create the singleton ChannelResponseRouter."""
    global _router_instance
    if _router_instance is None:
        _router_instance = ChannelResponseRouter(registry=registry)
    return _router_instance
