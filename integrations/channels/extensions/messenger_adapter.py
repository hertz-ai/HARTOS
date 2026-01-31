"""
Facebook Messenger Channel Adapter

Implements Facebook Messenger messaging via Meta Graph API.
Based on HevolveBot extension patterns for Messenger.

Features:
- Send API for all message types
- Message templates (generic, button, receipt, etc.)
- Quick replies
- Persistent menu
- Sender actions (typing indicators)
- Message tags for re-engagement
- One-time notifications
- Handover protocol
- Webhook handling
- Signature verification
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import hashlib
import hmac
from typing import Optional, List, Dict, Any, Callable, Union
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
import aiohttp

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


# Meta Graph API endpoints
GRAPH_API_VERSION = "v18.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


class MessagingType(Enum):
    """Messaging types for send API."""
    RESPONSE = "RESPONSE"
    UPDATE = "UPDATE"
    MESSAGE_TAG = "MESSAGE_TAG"


class MessageTag(Enum):
    """Message tags for re-engagement."""
    CONFIRMED_EVENT_UPDATE = "CONFIRMED_EVENT_UPDATE"
    POST_PURCHASE_UPDATE = "POST_PURCHASE_UPDATE"
    ACCOUNT_UPDATE = "ACCOUNT_UPDATE"
    HUMAN_AGENT = "HUMAN_AGENT"


class SenderAction(Enum):
    """Sender actions."""
    TYPING_ON = "typing_on"
    TYPING_OFF = "typing_off"
    MARK_SEEN = "mark_seen"


@dataclass
class MessengerConfig(ChannelConfig):
    """Messenger-specific configuration."""
    page_access_token: str = ""
    app_secret: str = ""
    verify_token: str = ""
    page_id: Optional[str] = None
    enable_templates: bool = True
    enable_quick_replies: bool = True
    enable_persistent_menu: bool = False
    api_version: str = GRAPH_API_VERSION


@dataclass
class QuickReply:
    """Quick reply button."""
    content_type: str = "text"  # text, location, user_phone_number, user_email
    title: Optional[str] = None
    payload: Optional[str] = None
    image_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to API format."""
        result = {"content_type": self.content_type}
        if self.title:
            result["title"] = self.title[:20]  # Max 20 chars
        if self.payload:
            result["payload"] = self.payload[:1000]  # Max 1000 chars
        if self.image_url:
            result["image_url"] = self.image_url
        return result


@dataclass
class Button:
    """Button for templates."""
    type: str  # web_url, postback, phone_number, etc.
    title: str
    url: Optional[str] = None
    payload: Optional[str] = None
    webview_height_ratio: str = "full"  # compact, tall, full
    messenger_extensions: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to API format."""
        result = {
            "type": self.type,
            "title": self.title[:20],  # Max 20 chars
        }
        if self.type == "web_url":
            result["url"] = self.url
            result["webview_height_ratio"] = self.webview_height_ratio
            result["messenger_extensions"] = self.messenger_extensions
        elif self.type == "postback":
            result["payload"] = self.payload or self.title
        elif self.type == "phone_number":
            result["payload"] = self.payload
        return result


@dataclass
class GenericElement:
    """Generic template element."""
    title: str
    subtitle: Optional[str] = None
    image_url: Optional[str] = None
    default_action: Optional[Dict[str, Any]] = None
    buttons: List[Button] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to API format."""
        result = {"title": self.title[:80]}  # Max 80 chars
        if self.subtitle:
            result["subtitle"] = self.subtitle[:80]
        if self.image_url:
            result["image_url"] = self.image_url
        if self.default_action:
            result["default_action"] = self.default_action
        if self.buttons:
            result["buttons"] = [btn.to_dict() for btn in self.buttons[:3]]  # Max 3 buttons
        return result


@dataclass
class GenericTemplate:
    """Generic template builder."""
    elements: List[GenericElement] = field(default_factory=list)
    image_aspect_ratio: str = "horizontal"  # horizontal, square

    def add_element(
        self,
        title: str,
        subtitle: Optional[str] = None,
        image_url: Optional[str] = None,
        buttons: Optional[List[Button]] = None,
    ) -> 'GenericTemplate':
        """Add an element to the template."""
        self.elements.append(GenericElement(
            title=title,
            subtitle=subtitle,
            image_url=image_url,
            buttons=buttons or [],
        ))
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Convert to API format."""
        return {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "image_aspect_ratio": self.image_aspect_ratio,
                    "elements": [elem.to_dict() for elem in self.elements[:10]],  # Max 10
                }
            }
        }


@dataclass
class ButtonTemplate:
    """Button template builder."""
    text: str
    buttons: List[Button] = field(default_factory=list)

    def add_url_button(self, title: str, url: str, **kwargs) -> 'ButtonTemplate':
        """Add a URL button."""
        self.buttons.append(Button(type="web_url", title=title, url=url, **kwargs))
        return self

    def add_postback_button(self, title: str, payload: str) -> 'ButtonTemplate':
        """Add a postback button."""
        self.buttons.append(Button(type="postback", title=title, payload=payload))
        return self

    def add_call_button(self, title: str, phone: str) -> 'ButtonTemplate':
        """Add a phone call button."""
        self.buttons.append(Button(type="phone_number", title=title, payload=phone))
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Convert to API format."""
        return {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": self.text[:640],  # Max 640 chars
                    "buttons": [btn.to_dict() for btn in self.buttons[:3]],  # Max 3
                }
            }
        }


class MessengerAdapter(ChannelAdapter):
    """
    Facebook Messenger adapter using Meta Graph API.

    Usage:
        config = MessengerConfig(
            page_access_token="your-page-token",
            app_secret="your-app-secret",
            verify_token="your-verify-token",
        )
        adapter = MessengerAdapter(config)
        adapter.on_message(my_handler)
        # Use with webhook endpoint
    """

    def __init__(self, config: MessengerConfig):
        super().__init__(config)
        self.messenger_config: MessengerConfig = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._postback_handlers: Dict[str, Callable] = {}
        self._referral_handlers: Dict[str, Callable] = {}
        self._api_base: str = f"https://graph.facebook.com/{config.api_version}"

    @property
    def name(self) -> str:
        return "messenger"

    async def connect(self) -> bool:
        """Initialize Messenger API connection."""
        if not self.messenger_config.page_access_token:
            logger.error("Messenger page access token required")
            return False

        try:
            # Create HTTP session
            self._session = aiohttp.ClientSession()

            # Verify token by getting page info
            page_info = await self._get_page_info()
            if not page_info:
                logger.error("Failed to verify page access token")
                return False

            self.messenger_config.page_id = page_info.get("id")

            self.status = ChannelStatus.CONNECTED
            page_name = page_info.get("name", "Unknown")
            logger.info(f"Messenger adapter connected to page: {page_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Messenger: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect Messenger adapter."""
        if self._session:
            await self._session.close()
            self._session = None

        self.status = ChannelStatus.DISCONNECTED

    async def _get_page_info(self) -> Optional[Dict[str, Any]]:
        """Get page information to verify token."""
        if not self._session:
            return None

        try:
            url = f"{self._api_base}/me"
            params = {"access_token": self.messenger_config.page_access_token}

            async with self._session.get(url, params=params) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    data = await response.json()
                    logger.error(f"Failed to get page info: {data}")
                    return None

        except Exception as e:
            logger.error(f"Error getting page info: {e}")
            return None

    def verify_webhook(self, mode: str, token: str, challenge: str) -> Optional[str]:
        """
        Verify webhook subscription request.
        Should be called from your webhook endpoint for GET requests.

        Returns challenge string if valid, None if invalid.
        """
        if mode == "subscribe" and token == self.messenger_config.verify_token:
            return challenge
        return None

    def verify_signature(self, body: bytes, signature: str) -> bool:
        """Verify webhook request signature."""
        if not self.messenger_config.app_secret:
            return True  # Skip verification if no secret configured

        if not signature.startswith("sha256="):
            return False

        expected = "sha256=" + hmac.new(
            self.messenger_config.app_secret.encode('utf-8'),
            body,
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    async def handle_webhook(self, body: str, signature: Optional[str] = None) -> None:
        """
        Handle incoming webhook POST request from Messenger.
        Should be called from your webhook endpoint.
        """
        try:
            # Verify signature if provided
            if signature and not self.verify_signature(body.encode('utf-8'), signature):
                logger.error("Invalid webhook signature")
                return

            data = json.loads(body)

            # Verify it's a page webhook
            if data.get("object") != "page":
                return

            # Process each entry
            for entry in data.get("entry", []):
                for messaging in entry.get("messaging", []):
                    await self._process_messaging_event(messaging)

        except Exception as e:
            logger.error(f"Error handling webhook: {e}")

    async def _process_messaging_event(self, event: Dict[str, Any]) -> None:
        """Process a single messaging event."""
        sender_id = event.get("sender", {}).get("id")

        if "message" in event:
            await self._handle_message(event)
        elif "postback" in event:
            await self._handle_postback(event)
        elif "referral" in event:
            await self._handle_referral(event)
        elif "read" in event:
            logger.debug(f"Message read by {sender_id}")
        elif "delivery" in event:
            logger.debug(f"Message delivered to {sender_id}")

    async def _handle_message(self, event: Dict[str, Any]) -> None:
        """Handle incoming message event."""
        # Ignore echo messages
        if event.get("message", {}).get("is_echo"):
            return

        message = self._convert_message(event)
        await self._dispatch_message(message)

    async def _handle_postback(self, event: Dict[str, Any]) -> None:
        """Handle postback event."""
        payload = event.get("postback", {}).get("payload")
        sender_id = event.get("sender", {}).get("id")

        # Check for registered handler
        if payload in self._postback_handlers:
            handler = self._postback_handlers[payload]
            await handler(event)
        else:
            # Convert to message-like event
            message = Message(
                id=f"postback_{int(datetime.now().timestamp() * 1000)}",
                channel=self.name,
                sender_id=sender_id,
                chat_id=sender_id,
                text=f"[postback:{payload}]",
                timestamp=datetime.fromtimestamp(event.get("timestamp", 0) / 1000),
                is_group=False,
                raw={'postback': {'payload': payload}},
            )
            await self._dispatch_message(message)

    async def _handle_referral(self, event: Dict[str, Any]) -> None:
        """Handle referral event (m.me links, ads, etc.)."""
        referral = event.get("referral", {})
        ref = referral.get("ref")
        source = referral.get("source")

        logger.info(f"Referral received: ref={ref}, source={source}")

        if ref in self._referral_handlers:
            handler = self._referral_handlers[ref]
            await handler(event)

    def _convert_message(self, event: Dict[str, Any]) -> Message:
        """Convert Messenger event to unified Message format."""
        sender_id = event.get("sender", {}).get("id", "")
        message_data = event.get("message", {})
        timestamp = event.get("timestamp", int(datetime.now().timestamp() * 1000))

        msg_id = message_data.get("mid", "")
        text = message_data.get("text", "")

        # Process attachments
        media = []
        for attachment in message_data.get("attachments", []):
            att_type = attachment.get("type")
            payload = attachment.get("payload", {})

            if att_type == "image":
                media.append(MediaAttachment(
                    type=MessageType.IMAGE,
                    url=payload.get("url"),
                ))
            elif att_type == "video":
                media.append(MediaAttachment(
                    type=MessageType.VIDEO,
                    url=payload.get("url"),
                ))
            elif att_type == "audio":
                media.append(MediaAttachment(
                    type=MessageType.AUDIO,
                    url=payload.get("url"),
                ))
            elif att_type == "file":
                media.append(MediaAttachment(
                    type=MessageType.DOCUMENT,
                    url=payload.get("url"),
                ))
            elif att_type == "location":
                coords = payload.get("coordinates", {})
                text = f"[location:{coords.get('lat', '')},{coords.get('long', '')}]"
            elif att_type == "fallback":
                # Shared content
                text = f"[shared:{payload.get('url', '')}]"

        # Handle quick reply payload
        quick_reply = message_data.get("quick_reply", {})
        if quick_reply.get("payload"):
            text = text or f"[quick_reply:{quick_reply['payload']}]"

        return Message(
            id=msg_id,
            channel=self.name,
            sender_id=sender_id,
            chat_id=sender_id,  # Messenger uses sender ID as chat ID for 1:1
            text=text,
            media=media,
            timestamp=datetime.fromtimestamp(timestamp / 1000),
            is_group=False,  # Page messaging is 1:1
            raw={
                'nlp': message_data.get('nlp'),
                'reply_to': message_data.get('reply_to'),
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
        """Send a message to a Messenger user."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            # Build message payload
            message_data: Dict[str, Any] = {}

            # Handle media
            if media and len(media) > 0:
                return await self._send_media_message(chat_id, media[0], text)

            # Handle buttons using button template
            if buttons and self.messenger_config.enable_templates:
                template = ButtonTemplate(text=text)
                for btn in buttons:
                    if btn.get("url"):
                        template.add_url_button(btn["text"], btn["url"])
                    else:
                        template.add_postback_button(
                            btn["text"],
                            btn.get("callback_data", btn["text"])
                        )
                message_data = template.to_dict()
            else:
                message_data = {"text": text}

            # Add reply context
            if reply_to:
                message_data["reply_to"] = {"mid": reply_to}

            return await self._send_api_request(chat_id, message_data)

        except Exception as e:
            logger.error(f"Failed to send Messenger message: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_media_message(
        self,
        chat_id: str,
        media: MediaAttachment,
        caption: Optional[str] = None,
    ) -> SendResult:
        """Send a media message."""
        try:
            # Determine attachment type
            if media.type == MessageType.IMAGE:
                att_type = "image"
            elif media.type == MessageType.VIDEO:
                att_type = "video"
            elif media.type == MessageType.AUDIO:
                att_type = "audio"
            else:
                att_type = "file"

            message_data = {
                "attachment": {
                    "type": att_type,
                    "payload": {
                        "url": media.url,
                        "is_reusable": True,
                    }
                }
            }

            result = await self._send_api_request(chat_id, message_data)

            # Send caption as separate message if present
            if caption and result.success:
                await self._send_api_request(chat_id, {"text": caption})

            return result

        except Exception as e:
            logger.error(f"Failed to send media message: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_api_request(
        self,
        recipient_id: str,
        message: Dict[str, Any],
        messaging_type: MessagingType = MessagingType.RESPONSE,
        tag: Optional[MessageTag] = None,
    ) -> SendResult:
        """Send a request to the Send API."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            url = f"{self._api_base}/me/messages"
            params = {"access_token": self.messenger_config.page_access_token}

            payload = {
                "recipient": {"id": recipient_id},
                "messaging_type": messaging_type.value,
                "message": message,
            }

            if tag:
                payload["tag"] = tag.value

            async with self._session.post(url, params=params, json=payload) as response:
                data = await response.json()

                if response.status == 200:
                    return SendResult(
                        success=True,
                        message_id=data.get("message_id"),
                    )
                else:
                    error = data.get("error", {})
                    error_code = error.get("code")
                    error_msg = error.get("message", "Unknown error")

                    # Handle rate limiting
                    if error_code == 613:
                        raise ChannelRateLimitError(60)

                    # Handle user blocking
                    if error_code == 551:
                        return SendResult(success=False, error="User has blocked the page")

                    return SendResult(success=False, error=error_msg)

        except ChannelRateLimitError:
            raise
        except Exception as e:
            logger.error(f"Send API request failed: {e}")
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """
        Edit a message.
        Note: Messenger doesn't support message editing, sends new message.
        """
        logger.warning("Messenger doesn't support message editing, sending new message")
        return await self.send_message(chat_id, text, buttons=buttons)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """
        Delete a message.
        Note: Messenger doesn't support message deletion by bots.
        """
        logger.warning("Messenger doesn't support message deletion")
        return False

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        await self._send_sender_action(chat_id, SenderAction.TYPING_ON)

    async def _send_sender_action(self, recipient_id: str, action: SenderAction) -> bool:
        """Send a sender action."""
        if not self._session:
            return False

        try:
            url = f"{self._api_base}/me/messages"
            params = {"access_token": self.messenger_config.page_access_token}

            payload = {
                "recipient": {"id": recipient_id},
                "sender_action": action.value,
            }

            async with self._session.post(url, params=params, json=payload) as response:
                return response.status == 200

        except Exception:
            return False

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get user profile information."""
        return await self.get_user_profile(chat_id)

    # Messenger-specific methods

    def register_postback_handler(
        self,
        payload: str,
        handler: Callable[[Dict[str, Any]], Any],
    ) -> None:
        """Register a handler for postback events."""
        self._postback_handlers[payload] = handler

    def register_referral_handler(
        self,
        ref: str,
        handler: Callable[[Dict[str, Any]], Any],
    ) -> None:
        """Register a handler for referral events."""
        self._referral_handlers[ref] = handler

    async def get_user_profile(
        self,
        user_id: str,
        fields: List[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get user profile information."""
        if not self._session:
            return None

        try:
            fields = fields or ["id", "name", "first_name", "last_name", "profile_pic"]
            url = f"{self._api_base}/{user_id}"
            params = {
                "access_token": self.messenger_config.page_access_token,
                "fields": ",".join(fields),
            }

            async with self._session.get(url, params=params) as response:
                if response.status == 200:
                    return await response.json()
                return None

        except Exception as e:
            logger.error(f"Error getting user profile: {e}")
            return None

    async def send_quick_replies(
        self,
        chat_id: str,
        text: str,
        quick_replies: List[QuickReply],
    ) -> SendResult:
        """Send a message with quick reply buttons."""
        if not self.messenger_config.enable_quick_replies:
            return await self.send_message(chat_id, text)

        message_data = {
            "text": text,
            "quick_replies": [qr.to_dict() for qr in quick_replies[:13]],  # Max 13
        }

        return await self._send_api_request(chat_id, message_data)

    async def send_generic_template(
        self,
        chat_id: str,
        template: GenericTemplate,
    ) -> SendResult:
        """Send a generic template (carousel)."""
        if not self.messenger_config.enable_templates:
            return SendResult(success=False, error="Templates disabled")

        return await self._send_api_request(chat_id, template.to_dict())

    async def send_button_template(
        self,
        chat_id: str,
        template: ButtonTemplate,
    ) -> SendResult:
        """Send a button template."""
        if not self.messenger_config.enable_templates:
            return SendResult(success=False, error="Templates disabled")

        return await self._send_api_request(chat_id, template.to_dict())

    async def send_with_tag(
        self,
        chat_id: str,
        text: str,
        tag: MessageTag,
    ) -> SendResult:
        """Send a message with a message tag (for re-engagement)."""
        message_data = {"text": text}
        return await self._send_api_request(
            chat_id,
            message_data,
            MessagingType.MESSAGE_TAG,
            tag
        )

    async def mark_seen(self, chat_id: str) -> bool:
        """Mark messages as seen."""
        return await self._send_sender_action(chat_id, SenderAction.MARK_SEEN)

    async def set_persistent_menu(
        self,
        menu_items: List[Dict[str, Any]],
        locale: str = "default",
    ) -> bool:
        """
        Set the persistent menu for the page.

        Args:
            menu_items: List of menu item dicts with keys: type, title, payload/url
            locale: Locale for the menu (default: all locales)
        """
        if not self._session:
            return False

        try:
            url = f"{self._api_base}/me/messenger_profile"
            params = {"access_token": self.messenger_config.page_access_token}

            # Convert menu items to proper format
            call_to_actions = []
            for item in menu_items[:3]:  # Max 3 top-level items
                if item.get("type") == "web_url":
                    call_to_actions.append({
                        "type": "web_url",
                        "title": item["title"][:30],
                        "url": item["url"],
                    })
                elif item.get("type") == "postback":
                    call_to_actions.append({
                        "type": "postback",
                        "title": item["title"][:30],
                        "payload": item.get("payload", item["title"]),
                    })

            payload = {
                "persistent_menu": [{
                    "locale": locale,
                    "composer_input_disabled": False,
                    "call_to_actions": call_to_actions,
                }]
            }

            async with self._session.post(url, params=params, json=payload) as response:
                data = await response.json()
                return data.get("result") == "success"

        except Exception as e:
            logger.error(f"Error setting persistent menu: {e}")
            return False

    async def delete_persistent_menu(self) -> bool:
        """Delete the persistent menu."""
        if not self._session:
            return False

        try:
            url = f"{self._api_base}/me/messenger_profile"
            params = {"access_token": self.messenger_config.page_access_token}

            payload = {"fields": ["persistent_menu"]}

            async with self._session.delete(url, params=params, json=payload) as response:
                data = await response.json()
                return data.get("result") == "success"

        except Exception as e:
            logger.error(f"Error deleting persistent menu: {e}")
            return False

    async def request_one_time_notification(
        self,
        chat_id: str,
        title: str,
        payload: str,
    ) -> SendResult:
        """
        Request permission to send a one-time notification.

        The user will see a "Notify Me" button.
        """
        message_data = {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "one_time_notif_req",
                    "title": title[:65],
                    "payload": payload,
                }
            }
        }

        return await self._send_api_request(chat_id, message_data)

    async def send_one_time_notification(
        self,
        notification_token: str,
        text: str,
    ) -> SendResult:
        """Send a one-time notification using a token."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            url = f"{self._api_base}/me/messages"
            params = {"access_token": self.messenger_config.page_access_token}

            payload = {
                "recipient": {"one_time_notif_token": notification_token},
                "message": {"text": text},
            }

            async with self._session.post(url, params=params, json=payload) as response:
                data = await response.json()

                if response.status == 200:
                    return SendResult(success=True, message_id=data.get("message_id"))
                else:
                    error = data.get("error", {}).get("message", "Unknown error")
                    return SendResult(success=False, error=error)

        except Exception as e:
            logger.error(f"Failed to send one-time notification: {e}")
            return SendResult(success=False, error=str(e))


def create_messenger_adapter(
    page_access_token: str = None,
    app_secret: str = None,
    verify_token: str = None,
    **kwargs
) -> MessengerAdapter:
    """
    Factory function to create Messenger adapter.

    Args:
        page_access_token: Facebook page access token (or set MESSENGER_PAGE_TOKEN env var)
        app_secret: Facebook app secret (or set MESSENGER_APP_SECRET env var)
        verify_token: Webhook verification token (or set MESSENGER_VERIFY_TOKEN env var)
        **kwargs: Additional config options

    Returns:
        Configured MessengerAdapter
    """
    page_access_token = page_access_token or os.getenv("MESSENGER_PAGE_TOKEN")
    app_secret = app_secret or os.getenv("MESSENGER_APP_SECRET")
    verify_token = verify_token or os.getenv("MESSENGER_VERIFY_TOKEN")

    if not page_access_token:
        raise ValueError("Messenger page access token required")

    config = MessengerConfig(
        page_access_token=page_access_token,
        app_secret=app_secret or "",
        verify_token=verify_token or "",
        **kwargs
    )
    return MessengerAdapter(config)
