"""
LINE Channel Adapter

Implements LINE Messaging API integration.
Based on HevolveBot extension patterns for LINE.

Features:
- Messaging API (send/receive messages)
- Rich menus
- Flex Messages (rich UI components)
- LIFF (LINE Front-end Framework) integration
- Quick replies
- Image maps
- Stickers
- Location messages
- Push and reply messaging
- Webhook signature validation
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import hmac
import hashlib
import base64
from typing import Optional, List, Dict, Any, Callable, Union
from datetime import datetime
from dataclasses import dataclass, field
import aiohttp

try:
    from linebot import LineBotApi, WebhookHandler, WebhookParser
    from linebot.models import (
        TextSendMessage,
        ImageSendMessage,
        VideoSendMessage,
        AudioSendMessage,
        LocationSendMessage,
        StickerSendMessage,
        FlexSendMessage,
        TemplateSendMessage,
        QuickReply,
        QuickReplyButton,
        MessageAction,
        URIAction,
        PostbackAction,
        RichMenu,
        RichMenuArea,
        RichMenuBounds,
        RichMenuSize,
        BubbleContainer,
        BoxComponent,
        TextComponent,
        ButtonComponent,
        ImageComponent,
        FlexMessage,
    )
    from linebot.exceptions import LineBotApiError, InvalidSignatureError
    HAS_LINE = True
except ImportError:
    HAS_LINE = False

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
class LINEConfig(ChannelConfig):
    """LINE-specific configuration."""
    channel_access_token: str = ""
    channel_secret: str = ""
    liff_id: Optional[str] = None
    default_rich_menu_id: Optional[str] = None
    enable_push_messages: bool = True
    enable_flex_messages: bool = True


@dataclass
class FlexBubble:
    """Flex Message Bubble builder helper."""
    header: Optional[Dict[str, Any]] = None
    hero: Optional[Dict[str, Any]] = None
    body: Optional[Dict[str, Any]] = None
    footer: Optional[Dict[str, Any]] = None
    size: str = "mega"

    def set_header(self, text: str, **kwargs) -> 'FlexBubble':
        """Set header with text."""
        self.header = {
            "type": "box",
            "layout": "vertical",
            "contents": [{
                "type": "text",
                "text": text,
                "weight": kwargs.get("weight", "bold"),
                "size": kwargs.get("size", "xl"),
            }]
        }
        return self

    def set_hero_image(self, url: str, **kwargs) -> 'FlexBubble':
        """Set hero image."""
        self.hero = {
            "type": "image",
            "url": url,
            "size": kwargs.get("size", "full"),
            "aspectRatio": kwargs.get("aspect_ratio", "20:13"),
            "aspectMode": kwargs.get("aspect_mode", "cover"),
        }
        return self

    def set_body(self, contents: List[Dict[str, Any]]) -> 'FlexBubble':
        """Set body contents."""
        self.body = {
            "type": "box",
            "layout": "vertical",
            "contents": contents,
        }
        return self

    def add_body_text(self, text: str, **kwargs) -> 'FlexBubble':
        """Add text to body."""
        if not self.body:
            self.body = {"type": "box", "layout": "vertical", "contents": []}
        self.body["contents"].append({
            "type": "text",
            "text": text,
            "wrap": kwargs.get("wrap", True),
            "size": kwargs.get("size", "md"),
        })
        return self

    def set_footer(self, buttons: List[Dict[str, Any]]) -> 'FlexBubble':
        """Set footer with buttons."""
        self.footer = {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": buttons,
        }
        return self

    def add_button(
        self,
        label: str,
        action_type: str = "message",
        data: str = None,
        **kwargs
    ) -> 'FlexBubble':
        """Add a button to footer."""
        if not self.footer:
            self.footer = {"type": "box", "layout": "vertical", "spacing": "sm", "contents": []}

        if action_type == "uri":
            action = {"type": "uri", "label": label, "uri": data}
        elif action_type == "postback":
            action = {"type": "postback", "label": label, "data": data}
        else:
            action = {"type": "message", "label": label, "text": data or label}

        self.footer["contents"].append({
            "type": "button",
            "style": kwargs.get("style", "primary"),
            "action": action,
        })
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Convert to Flex Message JSON."""
        bubble = {
            "type": "bubble",
            "size": self.size,
        }
        if self.header:
            bubble["header"] = self.header
        if self.hero:
            bubble["hero"] = self.hero
        if self.body:
            bubble["body"] = self.body
        if self.footer:
            bubble["footer"] = self.footer
        return bubble


@dataclass
class QuickReplyItem:
    """Quick reply button item."""
    label: str
    action_type: str = "message"  # message, postback, uri, datetime, camera, cameraRoll, location
    text: Optional[str] = None
    data: Optional[str] = None
    image_url: Optional[str] = None


class LINEAdapter(ChannelAdapter):
    """
    LINE Messaging API adapter.

    Usage:
        config = LINEConfig(
            channel_access_token="your-token",
            channel_secret="your-secret",
        )
        adapter = LINEAdapter(config)
        adapter.on_message(my_handler)
        # Use with webhook endpoint
    """

    def __init__(self, config: LINEConfig):
        if not HAS_LINE:
            raise ImportError(
                "line-bot-sdk not installed. "
                "Install with: pip install line-bot-sdk"
            )

        super().__init__(config)
        self.line_config: LINEConfig = config
        self._api: Optional[LineBotApi] = None
        self._parser: Optional[WebhookParser] = None
        self._postback_handlers: Dict[str, Callable] = {}
        self._rich_menus: Dict[str, str] = {}  # alias -> rich_menu_id

    @property
    def name(self) -> str:
        return "line"

    async def connect(self) -> bool:
        """Initialize LINE Bot API client."""
        if not self.line_config.channel_access_token:
            logger.error("LINE channel access token required")
            return False

        if not self.line_config.channel_secret:
            logger.error("LINE channel secret required")
            return False

        try:
            # Initialize API client
            self._api = LineBotApi(self.line_config.channel_access_token)

            # Initialize webhook parser
            self._parser = WebhookParser(self.line_config.channel_secret)

            # Verify token by getting bot info
            bot_info = self._api.get_bot_info()
            logger.info(f"LINE connected as: {bot_info.display_name}")

            self.status = ChannelStatus.CONNECTED
            return True

        except LineBotApiError as e:
            logger.error(f"Failed to connect to LINE: {e.message}")
            self.status = ChannelStatus.ERROR
            return False
        except Exception as e:
            logger.error(f"Failed to connect to LINE: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect LINE adapter."""
        self._api = None
        self._parser = None
        self.status = ChannelStatus.DISCONNECTED

    def validate_signature(self, body: str, signature: str) -> bool:
        """Validate webhook signature."""
        if not self.line_config.channel_secret:
            return False

        hash_value = hmac.new(
            self.line_config.channel_secret.encode('utf-8'),
            body.encode('utf-8'),
            hashlib.sha256
        ).digest()

        expected_signature = base64.b64encode(hash_value).decode('utf-8')
        return hmac.compare_digest(signature, expected_signature)

    async def handle_webhook(self, body: str, signature: str) -> None:
        """
        Handle incoming webhook request from LINE.
        Should be called from your webhook endpoint.
        """
        if not self._parser:
            raise ChannelConnectionError("Adapter not initialized")

        # Validate signature
        if not self.validate_signature(body, signature):
            raise InvalidSignatureError("Invalid signature")

        # Parse events
        events = self._parser.parse(body, signature)

        for event in events:
            await self._handle_event(event)

    async def _handle_event(self, event: Any) -> None:
        """Handle LINE webhook event."""
        event_type = event.type

        if event_type == "message":
            await self._handle_message_event(event)
        elif event_type == "postback":
            await self._handle_postback_event(event)
        elif event_type == "follow":
            await self._handle_follow_event(event)
        elif event_type == "unfollow":
            await self._handle_unfollow_event(event)
        elif event_type == "join":
            await self._handle_join_event(event)
        elif event_type == "leave":
            await self._handle_leave_event(event)

    async def _handle_message_event(self, event: Any) -> None:
        """Handle message event."""
        message = self._convert_message(event)
        await self._dispatch_message(message)

    async def _handle_postback_event(self, event: Any) -> None:
        """Handle postback event."""
        data = event.postback.data

        # Check for registered handler
        if data in self._postback_handlers:
            handler = self._postback_handlers[data]
            await handler(event)
        else:
            # Convert to message-like event
            message = Message(
                id=event.timestamp,
                channel=self.name,
                sender_id=event.source.user_id,
                chat_id=self._get_chat_id(event.source),
                text=f"[postback:{data}]",
                timestamp=datetime.fromtimestamp(event.timestamp / 1000),
                is_group=event.source.type != "user",
                raw={'postback': {'data': data}},
            )
            await self._dispatch_message(message)

    async def _handle_follow_event(self, event: Any) -> None:
        """Handle follow event (user added bot)."""
        logger.info(f"User followed: {event.source.user_id}")

    async def _handle_unfollow_event(self, event: Any) -> None:
        """Handle unfollow event (user blocked bot)."""
        logger.info(f"User unfollowed: {event.source.user_id}")

    async def _handle_join_event(self, event: Any) -> None:
        """Handle join event (bot added to group/room)."""
        chat_id = self._get_chat_id(event.source)
        logger.info(f"Bot joined: {chat_id}")

    async def _handle_leave_event(self, event: Any) -> None:
        """Handle leave event (bot removed from group/room)."""
        chat_id = self._get_chat_id(event.source)
        logger.info(f"Bot left: {chat_id}")

    def _get_chat_id(self, source: Any) -> str:
        """Get chat ID from event source."""
        if source.type == "user":
            return source.user_id
        elif source.type == "group":
            return source.group_id
        elif source.type == "room":
            return source.room_id
        return ""

    def _convert_message(self, event: Any) -> Message:
        """Convert LINE event to unified Message format."""
        source = event.source
        msg = event.message

        # Get text content
        text = ""
        media = []

        if msg.type == "text":
            text = msg.text
        elif msg.type == "image":
            media.append(MediaAttachment(
                type=MessageType.IMAGE,
                file_id=msg.id,
            ))
        elif msg.type == "video":
            media.append(MediaAttachment(
                type=MessageType.VIDEO,
                file_id=msg.id,
            ))
        elif msg.type == "audio":
            media.append(MediaAttachment(
                type=MessageType.AUDIO,
                file_id=msg.id,
            ))
        elif msg.type == "location":
            text = f"[location:{msg.latitude},{msg.longitude}]"
        elif msg.type == "sticker":
            text = f"[sticker:{msg.package_id}/{msg.sticker_id}]"

        # Determine chat type
        is_group = source.type != "user"
        chat_id = self._get_chat_id(source)

        return Message(
            id=msg.id,
            channel=self.name,
            sender_id=source.user_id if hasattr(source, 'user_id') else "",
            chat_id=chat_id,
            text=text,
            media=media,
            timestamp=datetime.fromtimestamp(event.timestamp / 1000),
            is_group=is_group,
            raw={
                'reply_token': event.reply_token,
                'source_type': source.type,
                'message_type': msg.type,
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
        """Send a message to a LINE chat."""
        if not self._api:
            return SendResult(success=False, error="Not connected")

        try:
            messages = []

            # Build message based on content
            if buttons and self.line_config.enable_flex_messages:
                # Use flex message for buttons
                flex = self._build_flex_message(text, buttons)
                messages.append(FlexSendMessage(alt_text=text, contents=flex))
            elif media and len(media) > 0:
                # Send media
                for m in media:
                    if m.type == MessageType.IMAGE:
                        messages.append(ImageSendMessage(
                            original_content_url=m.url,
                            preview_image_url=m.url,
                        ))
                    elif m.type == MessageType.VIDEO:
                        messages.append(VideoSendMessage(
                            original_content_url=m.url,
                            preview_image_url=m.url,
                        ))
                    elif m.type == MessageType.AUDIO:
                        messages.append(AudioSendMessage(
                            original_content_url=m.url,
                            duration=60000,  # Default duration
                        ))
                if text:
                    messages.append(TextSendMessage(text=text))
            else:
                messages.append(TextSendMessage(text=text))

            # Use push message if no reply token
            if reply_to:
                self._api.reply_message(reply_to, messages)
            else:
                self._api.push_message(chat_id, messages)

            return SendResult(success=True)

        except LineBotApiError as e:
            if e.status_code == 429:
                raise ChannelRateLimitError()
            logger.error(f"Failed to send LINE message: {e.message}")
            return SendResult(success=False, error=e.message)
        except Exception as e:
            logger.error(f"Failed to send LINE message: {e}")
            return SendResult(success=False, error=str(e))

    def _build_flex_message(
        self,
        text: str,
        buttons: List[Dict],
    ) -> Dict[str, Any]:
        """Build a flex message with buttons."""
        bubble = FlexBubble()
        bubble.add_body_text(text)

        for btn in buttons:
            if btn.get('url'):
                bubble.add_button(
                    btn['text'],
                    action_type="uri",
                    data=btn['url'],
                )
            else:
                bubble.add_button(
                    btn['text'],
                    action_type="postback",
                    data=btn.get('callback_data', btn['text']),
                )

        return bubble.to_dict()

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """
        Edit an existing LINE message.
        Note: LINE doesn't support editing messages, so this sends a new message.
        """
        logger.warning("LINE doesn't support message editing, sending new message")
        return await self.send_message(chat_id, text, buttons=buttons)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """
        Delete a LINE message.
        Note: LINE doesn't support message deletion by bots.
        """
        logger.warning("LINE doesn't support message deletion")
        return False

    async def send_typing(self, chat_id: str) -> None:
        """
        Send typing indicator.
        Note: LINE doesn't have a typing indicator API.
        """
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a LINE chat."""
        if not self._api:
            return None

        try:
            # Try to get group info
            try:
                group = self._api.get_group_summary(chat_id)
                return {
                    'id': chat_id,
                    'type': 'group',
                    'name': group.group_name,
                    'picture_url': group.picture_url,
                }
            except:
                pass

            # Try to get room info (chat rooms don't have names)
            try:
                # Rooms don't have summary API
                return {
                    'id': chat_id,
                    'type': 'room',
                }
            except:
                pass

            # Assume it's a user
            try:
                profile = self._api.get_profile(chat_id)
                return {
                    'id': chat_id,
                    'type': 'user',
                    'name': profile.display_name,
                    'picture_url': profile.picture_url,
                    'status_message': profile.status_message,
                }
            except:
                pass

            return None

        except Exception as e:
            logger.error(f"Failed to get chat info: {e}")
            return None

    # LINE-specific methods

    def register_postback_handler(
        self,
        data: str,
        handler: Callable[[Any], Any],
    ) -> None:
        """Register a handler for postback actions."""
        self._postback_handlers[data] = handler

    async def send_flex_message(
        self,
        chat_id: str,
        bubble: FlexBubble,
        alt_text: str = "Flex Message",
        reply_token: Optional[str] = None,
    ) -> SendResult:
        """Send a flex message."""
        if not self._api:
            return SendResult(success=False, error="Not connected")

        try:
            message = FlexSendMessage(
                alt_text=alt_text,
                contents=bubble.to_dict(),
            )

            if reply_token:
                self._api.reply_message(reply_token, message)
            else:
                self._api.push_message(chat_id, message)

            return SendResult(success=True)

        except LineBotApiError as e:
            logger.error(f"Failed to send flex message: {e.message}")
            return SendResult(success=False, error=e.message)

    async def send_quick_reply(
        self,
        chat_id: str,
        text: str,
        items: List[QuickReplyItem],
        reply_token: Optional[str] = None,
    ) -> SendResult:
        """Send a message with quick reply buttons."""
        if not self._api:
            return SendResult(success=False, error="Not connected")

        try:
            quick_reply_items = []

            for item in items:
                if item.action_type == "message":
                    action = MessageAction(label=item.label, text=item.text or item.label)
                elif item.action_type == "postback":
                    action = PostbackAction(label=item.label, data=item.data or item.label)
                elif item.action_type == "uri":
                    action = URIAction(label=item.label, uri=item.data)
                else:
                    action = MessageAction(label=item.label, text=item.text or item.label)

                quick_reply_items.append(QuickReplyButton(
                    action=action,
                    image_url=item.image_url,
                ))

            message = TextSendMessage(
                text=text,
                quick_reply=QuickReply(items=quick_reply_items),
            )

            if reply_token:
                self._api.reply_message(reply_token, message)
            else:
                self._api.push_message(chat_id, message)

            return SendResult(success=True)

        except LineBotApiError as e:
            logger.error(f"Failed to send quick reply: {e.message}")
            return SendResult(success=False, error=e.message)

    async def send_sticker(
        self,
        chat_id: str,
        package_id: str,
        sticker_id: str,
        reply_token: Optional[str] = None,
    ) -> SendResult:
        """Send a sticker."""
        if not self._api:
            return SendResult(success=False, error="Not connected")

        try:
            message = StickerSendMessage(
                package_id=package_id,
                sticker_id=sticker_id,
            )

            if reply_token:
                self._api.reply_message(reply_token, message)
            else:
                self._api.push_message(chat_id, message)

            return SendResult(success=True)

        except LineBotApiError as e:
            logger.error(f"Failed to send sticker: {e.message}")
            return SendResult(success=False, error=e.message)

    async def send_location(
        self,
        chat_id: str,
        title: str,
        address: str,
        latitude: float,
        longitude: float,
        reply_token: Optional[str] = None,
    ) -> SendResult:
        """Send a location message."""
        if not self._api:
            return SendResult(success=False, error="Not connected")

        try:
            message = LocationSendMessage(
                title=title,
                address=address,
                latitude=latitude,
                longitude=longitude,
            )

            if reply_token:
                self._api.reply_message(reply_token, message)
            else:
                self._api.push_message(chat_id, message)

            return SendResult(success=True)

        except LineBotApiError as e:
            logger.error(f"Failed to send location: {e.message}")
            return SendResult(success=False, error=e.message)

    async def create_rich_menu(
        self,
        name: str,
        chat_bar_text: str,
        areas: List[Dict[str, Any]],
        size: tuple = (2500, 1686),
    ) -> Optional[str]:
        """Create a rich menu."""
        if not self._api:
            return None

        try:
            rich_menu = RichMenu(
                size=RichMenuSize(width=size[0], height=size[1]),
                selected=True,
                name=name,
                chat_bar_text=chat_bar_text,
                areas=[
                    RichMenuArea(
                        bounds=RichMenuBounds(
                            x=area['x'],
                            y=area['y'],
                            width=area['width'],
                            height=area['height'],
                        ),
                        action=self._build_action(area['action']),
                    )
                    for area in areas
                ],
            )

            rich_menu_id = self._api.create_rich_menu(rich_menu)
            self._rich_menus[name] = rich_menu_id
            return rich_menu_id

        except LineBotApiError as e:
            logger.error(f"Failed to create rich menu: {e.message}")
            return None

    def _build_action(self, action: Dict[str, Any]) -> Any:
        """Build action object from dict."""
        action_type = action.get('type', 'message')

        if action_type == 'uri':
            return URIAction(label=action.get('label', ''), uri=action.get('uri', ''))
        elif action_type == 'postback':
            return PostbackAction(label=action.get('label', ''), data=action.get('data', ''))
        else:
            return MessageAction(label=action.get('label', ''), text=action.get('text', ''))

    async def set_rich_menu(self, user_id: str, rich_menu_id: str) -> bool:
        """Link a rich menu to a user."""
        if not self._api:
            return False

        try:
            self._api.link_rich_menu_to_user(user_id, rich_menu_id)
            return True
        except LineBotApiError as e:
            logger.error(f"Failed to set rich menu: {e.message}")
            return False

    async def get_message_content(self, message_id: str) -> Optional[bytes]:
        """Get content of a media message."""
        if not self._api:
            return None

        try:
            content = self._api.get_message_content(message_id)
            return content.content
        except LineBotApiError as e:
            logger.error(f"Failed to get message content: {e.message}")
            return None

    def get_liff_url(self, path: str = "") -> Optional[str]:
        """Get LIFF URL for the configured LIFF app."""
        if not self.line_config.liff_id:
            return None
        return f"https://liff.line.me/{self.line_config.liff_id}{path}"


def create_line_adapter(
    channel_access_token: str = None,
    channel_secret: str = None,
    **kwargs
) -> LINEAdapter:
    """
    Factory function to create LINE adapter.

    Args:
        channel_access_token: LINE channel access token (or set LINE_CHANNEL_ACCESS_TOKEN env var)
        channel_secret: LINE channel secret (or set LINE_CHANNEL_SECRET env var)
        **kwargs: Additional config options

    Returns:
        Configured LINEAdapter
    """
    channel_access_token = channel_access_token or os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    channel_secret = channel_secret or os.getenv("LINE_CHANNEL_SECRET")

    if not channel_access_token:
        raise ValueError("LINE channel access token required")
    if not channel_secret:
        raise ValueError("LINE channel secret required")

    config = LINEConfig(
        channel_access_token=channel_access_token,
        channel_secret=channel_secret,
        **kwargs
    )
    return LINEAdapter(config)
