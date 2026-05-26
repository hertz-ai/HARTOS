"""
Microsoft Teams Channel Adapter

Implements Microsoft Teams messaging using Bot Framework.
Based on HevolveBot extension patterns for Teams.

Features:
- Bot Framework SDK integration
- Adaptive Cards support
- Tabs integration
- Meeting integration
- Channel/Group chat support
- Direct messages (1:1)
- File sharing
- @mentions
- Message reactions
- Conversation reference for proactive messaging
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from dataclasses import dataclass, field
try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False
from urllib.parse import urljoin

try:
    from botbuilder.core import (
        BotFrameworkAdapter,
        BotFrameworkAdapterSettings,
        TurnContext,
        MessageFactory,
        CardFactory,
    )
    from botbuilder.schema import (
        Activity,
        ActivityTypes,
        ChannelAccount,
        ConversationReference,
        Attachment,
        HeroCard,
        CardAction,
        ActionTypes,
        Mention,
    )
    from botbuilder.core.teams import TeamsInfo, TeamsActivityHandler
    HAS_TEAMS = True
except ImportError:
    HAS_TEAMS = False

from ..base import (
    ChannelAdapter,
    ChannelConfig,
    ChannelStatus,
    Message,
    MessageType,
    MediaAttachment,
    SendResult,
    ChannelConnectionError,
    ChannelSendError,
    ChannelRateLimitError,
)

logger = logging.getLogger(__name__)


@dataclass
class TeamsConfig(ChannelConfig):
    """Microsoft Teams-specific configuration."""
    app_id: str = ""
    app_password: str = ""
    tenant_id: Optional[str] = None  # For single-tenant apps
    service_url: str = "https://smba.trafficmanager.net/teams/"
    enable_proactive_messaging: bool = True
    enable_adaptive_cards: bool = True
    enable_tabs: bool = False
    enable_meetings: bool = False


@dataclass
class AdaptiveCard:
    """Adaptive Card builder helper."""
    body: List[Dict[str, Any]] = field(default_factory=list)
    actions: List[Dict[str, Any]] = field(default_factory=list)
    version: str = "1.4"

    def add_text_block(
        self,
        text: str,
        size: str = "medium",
        weight: str = "default",
        wrap: bool = True,
    ) -> 'AdaptiveCard':
        """Add a TextBlock to the card."""
        self.body.append({
            "type": "TextBlock",
            "text": text,
            "size": size,
            "weight": weight,
            "wrap": wrap,
        })
        return self

    def add_image(
        self,
        url: str,
        alt_text: str = "",
        size: str = "auto",
    ) -> 'AdaptiveCard':
        """Add an Image to the card."""
        self.body.append({
            "type": "Image",
            "url": url,
            "altText": alt_text,
            "size": size,
        })
        return self

    def add_action_submit(
        self,
        title: str,
        data: Dict[str, Any],
    ) -> 'AdaptiveCard':
        """Add a submit action button."""
        self.actions.append({
            "type": "Action.Submit",
            "title": title,
            "data": data,
        })
        return self

    def add_action_open_url(
        self,
        title: str,
        url: str,
    ) -> 'AdaptiveCard':
        """Add an open URL action button."""
        self.actions.append({
            "type": "Action.OpenUrl",
            "title": title,
            "url": url,
        })
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Convert to Adaptive Card JSON."""
        return {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": self.version,
            "body": self.body,
            "actions": self.actions,
        }


@dataclass
class ConversationRef:
    """Stored conversation reference for proactive messaging."""
    conversation_id: str
    service_url: str
    channel_id: str
    bot_id: str
    bot_name: str
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    tenant_id: Optional[str] = None
    is_group: bool = False


class TeamsAdapter(ChannelAdapter):
    """
    Microsoft Teams messaging adapter using Bot Framework.

    Usage:
        config = TeamsConfig(
            app_id="your-app-id",
            app_password="your-app-password",
        )
        adapter = TeamsAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: TeamsConfig):
        if not HAS_TEAMS:
            raise ImportError(
                "botbuilder not installed. "
                "Install with: pip install botbuilder-core botbuilder-schema"
            )

        super().__init__(config)
        self.teams_config: TeamsConfig = config
        self._adapter: Optional[BotFrameworkAdapter] = None
        self._conversation_refs: Dict[str, ConversationRef] = {}
        self._activity_handlers: List[Callable] = []
        self._card_action_handlers: Dict[str, Callable] = {}

    @property
    def name(self) -> str:
        return "teams"

    async def connect(self) -> bool:
        """Initialize Bot Framework adapter."""
        if not self.teams_config.app_id or not self.teams_config.app_password:
            logger.error("Teams app ID and password required")
            return False

        try:
            # Create adapter settings
            settings = BotFrameworkAdapterSettings(
                app_id=self.teams_config.app_id,
                app_password=self.teams_config.app_password,
            )

            # Create adapter
            self._adapter = BotFrameworkAdapter(settings)

            # Register error handler
            self._adapter.on_turn_error = self._on_error

            self.status = ChannelStatus.CONNECTED
            logger.info(f"Teams adapter initialized with app ID: {self.teams_config.app_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize Teams adapter: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect Teams adapter."""
        self._adapter = None
        self._conversation_refs.clear()
        self.status = ChannelStatus.DISCONNECTED

    async def _on_error(self, context: TurnContext, error: Exception) -> None:
        """Handle errors in turn processing."""
        logger.error(f"Teams adapter error: {error}")
        self.status = ChannelStatus.ERROR

        # Send error message to user
        try:
            await context.send_activity("Sorry, an error occurred processing your request.")
        except Exception:
            pass

    async def process_activity(self, activity: Activity, auth_header: str = "") -> None:
        """
        Process an incoming activity from Teams.
        This should be called from your webhook endpoint.
        """
        if not self._adapter:
            raise ChannelConnectionError("Adapter not initialized")

        async def turn_callback(turn_context: TurnContext):
            await self._handle_turn(turn_context)

        await self._adapter.process_activity(activity, auth_header, turn_callback)

    async def _handle_turn(self, context: TurnContext) -> None:
        """Handle a turn (incoming activity)."""
        activity = context.activity

        # Store conversation reference for proactive messaging
        if self.teams_config.enable_proactive_messaging:
            self._store_conversation_ref(context)

        # Handle different activity types
        if activity.type == ActivityTypes.message:
            await self._handle_message(context)
        elif activity.type == ActivityTypes.conversation_update:
            await self._handle_conversation_update(context)
        elif activity.type == ActivityTypes.invoke:
            await self._handle_invoke(context)
        elif activity.type == ActivityTypes.message_reaction:
            await self._handle_reaction(context)

    def _store_conversation_ref(self, context: TurnContext) -> None:
        """Store conversation reference for proactive messaging."""
        activity = context.activity
        ref = ConversationRef(
            conversation_id=activity.conversation.id,
            service_url=activity.service_url,
            channel_id=activity.channel_id,
            bot_id=activity.recipient.id,
            bot_name=activity.recipient.name,
            user_id=activity.from_property.id if activity.from_property else None,
            user_name=activity.from_property.name if activity.from_property else None,
            tenant_id=activity.conversation.tenant_id if hasattr(activity.conversation, 'tenant_id') else None,
            is_group=activity.conversation.is_group if hasattr(activity.conversation, 'is_group') else False,
        )
        self._conversation_refs[activity.conversation.id] = ref

    async def _handle_message(self, context: TurnContext) -> None:
        """Handle incoming message activity."""
        activity = context.activity

        # Check for adaptive card action
        if activity.value:
            await self._handle_card_action(context, activity.value)
            return

        # Convert to unified message
        message = self._convert_message(context)

        # Store context for reply
        message.raw['turn_context'] = context

        # Dispatch to handlers
        await self._dispatch_message(message)

    async def _handle_conversation_update(self, context: TurnContext) -> None:
        """Handle conversation update (member added/removed)."""
        activity = context.activity

        # Handle member added
        if activity.members_added:
            for member in activity.members_added:
                if member.id != activity.recipient.id:
                    # New member joined
                    logger.info(f"Member added: {member.name}")

    async def _handle_invoke(self, context: TurnContext) -> None:
        """Handle invoke activities (cards, tabs, etc.)."""
        activity = context.activity

        if activity.name == "adaptiveCard/action":
            # Adaptive card action
            if activity.value:
                await self._handle_card_action(context, activity.value)

    async def _handle_reaction(self, context: TurnContext) -> None:
        """Handle message reaction activity."""
        activity = context.activity

        if activity.reactions_added:
            for reaction in activity.reactions_added:
                logger.info(f"Reaction added: {reaction.type}")

    async def _handle_card_action(self, context: TurnContext, value: Dict[str, Any]) -> None:
        """Handle adaptive card action submission."""
        action_id = value.get('action_id') or value.get('actionId')

        if action_id and action_id in self._card_action_handlers:
            handler = self._card_action_handlers[action_id]
            await handler(context, value)

    def _convert_message(self, context: TurnContext) -> Message:
        """Convert Teams activity to unified Message format."""
        activity = context.activity

        # Check if bot is mentioned
        is_mentioned = False
        text = activity.text or ""

        if activity.entities:
            for entity in activity.entities:
                if entity.type == "mention":
                    mention_data = entity.additional_properties
                    if mention_data.get('mentioned', {}).get('id') == activity.recipient.id:
                        is_mentioned = True
                        # Remove mention from text
                        mention_text = mention_data.get('text', '')
                        text = text.replace(mention_text, '').strip()

        # Process attachments
        media = []
        if activity.attachments:
            for attachment in activity.attachments:
                media_type = MessageType.DOCUMENT
                if attachment.content_type and attachment.content_type.startswith('image/'):
                    media_type = MessageType.IMAGE
                elif attachment.content_type and attachment.content_type.startswith('video/'):
                    media_type = MessageType.VIDEO

                media.append(MediaAttachment(
                    type=media_type,
                    url=attachment.content_url,
                    file_name=attachment.name,
                    mime_type=attachment.content_type,
                ))

        # Determine if group
        is_group = False
        if hasattr(activity.conversation, 'is_group'):
            is_group = activity.conversation.is_group
        elif activity.conversation.conversation_type == "channel":
            is_group = True

        return Message(
            id=activity.id,
            channel=self.name,
            sender_id=activity.from_property.id if activity.from_property else "",
            sender_name=activity.from_property.name if activity.from_property else "",
            chat_id=activity.conversation.id,
            text=text,
            media=media,
            reply_to_id=activity.reply_to_id,
            timestamp=activity.timestamp or datetime.now(),
            is_group=is_group,
            is_bot_mentioned=is_mentioned,
            raw={
                'service_url': activity.service_url,
                'channel_id': activity.channel_id,
                'tenant_id': activity.conversation.tenant_id if hasattr(activity.conversation, 'tenant_id') else None,
            },
        )

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a message to a Teams conversation."""
        if not self._adapter:
            return SendResult(success=False, error="Not connected")

        try:
            # Get conversation reference
            conv_ref = self._conversation_refs.get(chat_id)
            if not conv_ref:
                return SendResult(success=False, error="Conversation not found")

            # Build activity
            if buttons and self.teams_config.enable_adaptive_cards:
                # Use adaptive card for buttons
                activity = self._build_adaptive_card_activity(text, buttons)
            else:
                activity = MessageFactory.text(text)

            # Add attachments for media
            if media:
                activity.attachments = activity.attachments or []
                for m in media:
                    activity.attachments.append(Attachment(
                        content_type=m.mime_type or "application/octet-stream",
                        content_url=m.url,
                        name=m.file_name,
                    ))

            # Send using proactive messaging
            result = await self._send_proactive(conv_ref, activity)
            return result

        except Exception as e:
            logger.error(f"Failed to send Teams message: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_proactive(
        self,
        conv_ref: ConversationRef,
        activity: Activity,
    ) -> SendResult:
        """Send a proactive message using stored conversation reference."""
        if not self._adapter:
            return SendResult(success=False, error="Not connected")

        try:
            # Build conversation reference
            reference = ConversationReference(
                activity_id=None,
                bot=ChannelAccount(id=conv_ref.bot_id, name=conv_ref.bot_name),
                channel_id=conv_ref.channel_id,
                conversation=type('Conversation', (), {
                    'id': conv_ref.conversation_id,
                    'is_group': conv_ref.is_group,
                    'tenant_id': conv_ref.tenant_id,
                })(),
                service_url=conv_ref.service_url,
            )

            result_id = None

            async def send_callback(turn_context: TurnContext):
                nonlocal result_id
                response = await turn_context.send_activity(activity)
                result_id = response.id if response else None

            await self._adapter.continue_conversation(
                reference,
                send_callback,
                self.teams_config.app_id,
            )

            return SendResult(success=True, message_id=result_id)

        except Exception as e:
            logger.error(f"Proactive message failed: {e}")
            return SendResult(success=False, error=str(e))

    def _build_adaptive_card_activity(
        self,
        text: str,
        buttons: List[Dict],
    ) -> Activity:
        """Build activity with adaptive card."""
        card = AdaptiveCard()
        card.add_text_block(text)

        for btn in buttons:
            if btn.get('url'):
                card.add_action_open_url(btn['text'], btn['url'])
            else:
                card.add_action_submit(
                    btn['text'],
                    {'action_id': btn.get('callback_data', btn['text'])}
                )

        attachment = Attachment(
            content_type="application/vnd.microsoft.card.adaptive",
            content=card.to_dict(),
        )

        activity = Activity(type=ActivityTypes.message)
        activity.attachments = [attachment]
        return activity

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Edit an existing Teams message."""
        if not self._adapter:
            return SendResult(success=False, error="Not connected")

        try:
            conv_ref = self._conversation_refs.get(chat_id)
            if not conv_ref:
                return SendResult(success=False, error="Conversation not found")

            if buttons and self.teams_config.enable_adaptive_cards:
                activity = self._build_adaptive_card_activity(text, buttons)
            else:
                activity = MessageFactory.text(text)

            activity.id = message_id

            reference = ConversationReference(
                activity_id=message_id,
                bot=ChannelAccount(id=conv_ref.bot_id, name=conv_ref.bot_name),
                channel_id=conv_ref.channel_id,
                conversation=type('Conversation', (), {
                    'id': conv_ref.conversation_id,
                    'is_group': conv_ref.is_group,
                    'tenant_id': conv_ref.tenant_id,
                })(),
                service_url=conv_ref.service_url,
            )

            async def update_callback(turn_context: TurnContext):
                await turn_context.update_activity(activity)

            await self._adapter.continue_conversation(
                reference,
                update_callback,
                self.teams_config.app_id,
            )

            return SendResult(success=True, message_id=message_id)

        except Exception as e:
            logger.error(f"Failed to edit Teams message: {e}")
            return SendResult(success=False, error=str(e))

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a Teams message."""
        if not self._adapter:
            return False

        try:
            conv_ref = self._conversation_refs.get(chat_id)
            if not conv_ref:
                return False

            reference = ConversationReference(
                activity_id=message_id,
                bot=ChannelAccount(id=conv_ref.bot_id, name=conv_ref.bot_name),
                channel_id=conv_ref.channel_id,
                conversation=type('Conversation', (), {
                    'id': conv_ref.conversation_id,
                    'is_group': conv_ref.is_group,
                    'tenant_id': conv_ref.tenant_id,
                })(),
                service_url=conv_ref.service_url,
            )

            async def delete_callback(turn_context: TurnContext):
                await turn_context.delete_activity(message_id)

            await self._adapter.continue_conversation(
                reference,
                delete_callback,
                self.teams_config.app_id,
            )

            return True

        except Exception as e:
            logger.error(f"Failed to delete Teams message: {e}")
            return False

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        if not self._adapter:
            return

        try:
            conv_ref = self._conversation_refs.get(chat_id)
            if not conv_ref:
                return

            reference = ConversationReference(
                bot=ChannelAccount(id=conv_ref.bot_id, name=conv_ref.bot_name),
                channel_id=conv_ref.channel_id,
                conversation=type('Conversation', (), {
                    'id': conv_ref.conversation_id,
                    'is_group': conv_ref.is_group,
                })(),
                service_url=conv_ref.service_url,
            )

            async def typing_callback(turn_context: TurnContext):
                typing_activity = Activity(type=ActivityTypes.typing)
                await turn_context.send_activity(typing_activity)

            await self._adapter.continue_conversation(
                reference,
                typing_callback,
                self.teams_config.app_id,
            )

        except Exception:
            pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a Teams conversation."""
        conv_ref = self._conversation_refs.get(chat_id)
        if conv_ref:
            return {
                'conversation_id': conv_ref.conversation_id,
                'channel_id': conv_ref.channel_id,
                'is_group': conv_ref.is_group,
                'tenant_id': conv_ref.tenant_id,
                'service_url': conv_ref.service_url,
            }
        return None

    # Teams-specific methods

    def register_card_action(
        self,
        action_id: str,
        handler: Callable[[TurnContext, Dict[str, Any]], Any],
    ) -> None:
        """Register a handler for adaptive card actions."""
        self._card_action_handlers[action_id] = handler

    async def send_adaptive_card(
        self,
        chat_id: str,
        card: AdaptiveCard,
    ) -> SendResult:
        """Send an adaptive card."""
        if not self._adapter:
            return SendResult(success=False, error="Not connected")

        try:
            conv_ref = self._conversation_refs.get(chat_id)
            if not conv_ref:
                return SendResult(success=False, error="Conversation not found")

            attachment = Attachment(
                content_type="application/vnd.microsoft.card.adaptive",
                content=card.to_dict(),
            )

            activity = Activity(type=ActivityTypes.message)
            activity.attachments = [attachment]

            return await self._send_proactive(conv_ref, activity)

        except Exception as e:
            logger.error(f"Failed to send adaptive card: {e}")
            return SendResult(success=False, error=str(e))

    async def get_team_members(self, chat_id: str) -> List[Dict[str, Any]]:
        """Get members of a Teams team/channel."""
        # This requires TeamsInfo.get_team_members which needs turn context
        # For now, return empty list
        logger.warning("get_team_members requires active turn context")
        return []

    async def get_meeting_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get meeting information if in a meeting context."""
        if not self.teams_config.enable_meetings:
            return None

        # Meeting info requires active turn context
        logger.warning("get_meeting_info requires active turn context")
        return None

    async def mention_user(
        self,
        chat_id: str,
        user_id: str,
        user_name: str,
        text: str,
    ) -> SendResult:
        """Send a message with a user mention."""
        if not self._adapter:
            return SendResult(success=False, error="Not connected")

        try:
            conv_ref = self._conversation_refs.get(chat_id)
            if not conv_ref:
                return SendResult(success=False, error="Conversation not found")

            # Create mention entity
            mention = Mention(
                mentioned=ChannelAccount(id=user_id, name=user_name),
                text=f"<at>{user_name}</at>",
            )

            # Create activity with mention
            activity = MessageFactory.text(f"<at>{user_name}</at> {text}")
            activity.entities = [mention]

            return await self._send_proactive(conv_ref, activity)

        except Exception as e:
            logger.error(f"Failed to send mention: {e}")
            return SendResult(success=False, error=str(e))


def create_teams_adapter(
    app_id: str = None,
    app_password: str = None,
    **kwargs
) -> TeamsAdapter:
    """
    Factory function to create Teams adapter.

    Args:
        app_id: Bot app ID (or set TEAMS_APP_ID env var)
        app_password: Bot app password (or set TEAMS_APP_PASSWORD env var)
        **kwargs: Additional config options

    Returns:
        Configured TeamsAdapter
    """
    app_id = app_id or os.getenv("TEAMS_APP_ID")
    app_password = app_password or os.getenv("TEAMS_APP_PASSWORD")

    if not app_id:
        raise ValueError("Teams app ID required")
    if not app_password:
        raise ValueError("Teams app password required")

    config = TeamsConfig(
        app_id=app_id,
        app_password=app_password,
        **kwargs
    )
    return TeamsAdapter(config)
