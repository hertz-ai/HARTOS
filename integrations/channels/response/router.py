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

        # 2. Fan-out to bound channels (async, fire-and-forget)
        if fan_out:
            self._async_fan_out(
                user_id=user_id,
                text=response_text,
                exclude_channel=originating_channel,
                exclude_chat_id=originating_chat_id,
            )

        # 3. WAMP notification to desktop/web
        self._notify_desktop_wamp(
            user_id=user_id,
            text=response_text,
            channel_type=originating_channel,
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
                loop = getattr(registry, '_loop', None) or _get_running_loop()

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
        """Publish to WAMP for desktop/web notification."""
        try:
            from hart_intelligence import publish_async
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
            publish_async(
                f'com.hertzai.hevolve.chat.{user_id}',
                payload,
            )
            # Dedicated channel response topic (cross-device)
            publish_async(
                f'com.hertzai.hevolve.channel.response.{user_id}',
                payload,
            )
        except Exception:
            pass  # WAMP is supplementary


def _get_running_loop():
    """Try to get a running event loop."""
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        return None


# Singleton
_router_instance = None


def get_response_router(registry=None) -> ChannelResponseRouter:
    """Get or create the singleton ChannelResponseRouter."""
    global _router_instance
    if _router_instance is None:
        _router_instance = ChannelResponseRouter(registry=registry)
    return _router_instance
