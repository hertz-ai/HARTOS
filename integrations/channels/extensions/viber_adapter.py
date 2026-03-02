"""
Viber Channel Adapter

Implements Viber Bot API integration.
Based on HevolveBot extension patterns for Viber.

Features:
- Viber Bot API integration
- Rich keyboards support
- Carousels
- Message types (text, picture, video, file, contact, location, sticker, URL)
- User information
- Broadcast messaging
- Online status
- Webhooks
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import hashlib
import hmac
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from dataclasses import dataclass, field
try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

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


# Viber API endpoints
VIBER_API_BASE = "https://chatapi.viber.com/pa"
VIBER_API_SET_WEBHOOK = f"{VIBER_API_BASE}/set_webhook"
VIBER_API_SEND_MESSAGE = f"{VIBER_API_BASE}/send_message"
VIBER_API_BROADCAST = f"{VIBER_API_BASE}/broadcast_message"
VIBER_API_GET_ACCOUNT_INFO = f"{VIBER_API_BASE}/get_account_info"
VIBER_API_GET_USER_DETAILS = f"{VIBER_API_BASE}/get_user_details"
VIBER_API_GET_ONLINE = f"{VIBER_API_BASE}/get_online"


@dataclass
class ViberConfig(ChannelConfig):
    """Viber-specific configuration."""
    auth_token: str = ""
    bot_name: str = ""
    bot_avatar: Optional[str] = None
    webhook_url: Optional[str] = None
    webhook_events: List[str] = field(default_factory=lambda: [
        "delivered", "seen", "failed", "subscribed",
        "unsubscribed", "conversation_started"
    ])
    enable_keyboard: bool = True
    default_keyboard_bg_color: str = "#FFFFFF"


@dataclass
class ViberUser:
    """Viber user information."""
    id: str
    name: str
    avatar: Optional[str] = None
    country: Optional[str] = None
    language: Optional[str] = None
    api_version: int = 1
    primary_device_os: Optional[str] = None
    viber_version: Optional[str] = None
    device_type: Optional[str] = None


@dataclass
class KeyboardButton:
    """Viber keyboard button."""
    text: str
    action_type: str = "reply"  # reply, open-url, location-picker, share-phone, none
    action_body: str = ""
    bg_color: Optional[str] = None
    text_size: str = "regular"  # small, regular, large
    columns: int = 6  # 1-6 for keyboards
    rows: int = 1  # 1-2 for keyboards
    image: Optional[str] = None
    silent: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to API format."""
        btn = {
            "ActionType": self.action_type,
            "ActionBody": self.action_body or self.text,
            "Text": self.text,
            "TextSize": self.text_size,
            "Columns": self.columns,
            "Rows": self.rows,
            "Silent": self.silent,
        }
        if self.bg_color:
            btn["BgColor"] = self.bg_color
        if self.image:
            btn["Image"] = self.image
        return btn


@dataclass
class Keyboard:
    """Viber keyboard builder."""
    buttons: List[KeyboardButton] = field(default_factory=list)
    bg_color: str = "#FFFFFF"
    default_height: bool = True
    input_field_state: str = "regular"  # regular, minimized, hidden

    def add_button(
        self,
        text: str,
        action_type: str = "reply",
        action_body: str = "",
        **kwargs
    ) -> 'Keyboard':
        """Add a button to the keyboard."""
        self.buttons.append(KeyboardButton(
            text=text,
            action_type=action_type,
            action_body=action_body or text,
            **kwargs
        ))
        return self

    def add_url_button(self, text: str, url: str, **kwargs) -> 'Keyboard':
        """Add a URL button."""
        return self.add_button(text, "open-url", url, **kwargs)

    def add_location_button(self, text: str = "Share Location", **kwargs) -> 'Keyboard':
        """Add a location picker button."""
        return self.add_button(text, "location-picker", "location", **kwargs)

    def add_phone_button(self, text: str = "Share Phone", **kwargs) -> 'Keyboard':
        """Add a share phone button."""
        return self.add_button(text, "share-phone", "phone", **kwargs)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to API format."""
        return {
            "Type": "keyboard",
            "BgColor": self.bg_color,
            "DefaultHeight": self.default_height,
            "InputFieldState": self.input_field_state,
            "Buttons": [btn.to_dict() for btn in self.buttons],
        }


@dataclass
class CarouselItem:
    """Carousel item for rich messages."""
    title: str
    subtitle: Optional[str] = None
    image: Optional[str] = None
    buttons: List[KeyboardButton] = field(default_factory=list)

    def add_button(self, text: str, action_type: str = "reply", action_body: str = "") -> 'CarouselItem':
        """Add a button to this carousel item."""
        self.buttons.append(KeyboardButton(
            text=text,
            action_type=action_type,
            action_body=action_body or text,
            columns=6,
            rows=1,
        ))
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Convert to rich media element format."""
        columns = 6  # Full width

        elements = []

        # Add image if present
        if self.image:
            elements.append({
                "Columns": columns,
                "Rows": 3,
                "ActionType": "none",
                "Image": self.image,
            })

        # Add title
        elements.append({
            "Columns": columns,
            "Rows": 1,
            "ActionType": "none",
            "Text": f"<b>{self.title}</b>",
            "TextSize": "medium",
            "TextVAlign": "middle",
            "TextHAlign": "center",
        })

        # Add subtitle if present
        if self.subtitle:
            elements.append({
                "Columns": columns,
                "Rows": 1,
                "ActionType": "none",
                "Text": self.subtitle,
                "TextSize": "small",
                "TextVAlign": "middle",
                "TextHAlign": "center",
            })

        # Add buttons
        for btn in self.buttons:
            btn_dict = btn.to_dict()
            btn_dict["Columns"] = columns
            elements.append(btn_dict)

        return elements


class ViberAdapter(ChannelAdapter):
    """
    Viber Bot API adapter.

    Usage:
        config = ViberConfig(
            auth_token="your-bot-token",
            bot_name="MyBot",
        )
        adapter = ViberAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: ViberConfig):
        super().__init__(config)
        self.viber_config: ViberConfig = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._user_cache: Dict[str, ViberUser] = {}
        self._callback_handlers: Dict[str, Callable] = {}
        self._account_info: Optional[Dict[str, Any]] = None

    @property
    def name(self) -> str:
        return "viber"

    async def connect(self) -> bool:
        """Initialize Viber Bot connection."""
        if not self.viber_config.auth_token:
            logger.error("Viber auth token required")
            return False

        try:
            # Create HTTP session
            self._session = aiohttp.ClientSession(
                headers={"X-Viber-Auth-Token": self.viber_config.auth_token}
            )

            # Get account info to verify token
            self._account_info = await self._get_account_info()
            if not self._account_info:
                logger.error("Failed to verify Viber bot token")
                return False

            # Set webhook if URL provided
            if self.viber_config.webhook_url:
                webhook_set = await self._set_webhook(self.viber_config.webhook_url)
                if not webhook_set:
                    logger.warning("Failed to set webhook, manual setup required")

            self.status = ChannelStatus.CONNECTED
            bot_name = self._account_info.get("name", "Unknown")
            logger.info(f"Viber adapter connected as: {bot_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Viber: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect Viber adapter."""
        if self._session:
            await self._session.close()
            self._session = None

        self._account_info = None
        self._user_cache.clear()
        self.status = ChannelStatus.DISCONNECTED

    async def _get_account_info(self) -> Optional[Dict[str, Any]]:
        """Get bot account information."""
        if not self._session:
            return None

        try:
            async with self._session.post(VIBER_API_GET_ACCOUNT_INFO, json={}) as response:
                data = await response.json()

                if data.get("status") == 0:
                    return data
                else:
                    logger.error(f"Failed to get account info: {data}")
                    return None

        except Exception as e:
            logger.error(f"Error getting account info: {e}")
            return None

    async def _set_webhook(self, url: str) -> bool:
        """Set webhook URL."""
        if not self._session:
            return False

        try:
            payload = {
                "url": url,
                "event_types": self.viber_config.webhook_events,
                "send_name": True,
                "send_photo": True,
            }

            async with self._session.post(VIBER_API_SET_WEBHOOK, json=payload) as response:
                data = await response.json()
                return data.get("status") == 0

        except Exception as e:
            logger.error(f"Error setting webhook: {e}")
            return False

    def verify_signature(self, body: bytes, signature: str) -> bool:
        """Verify webhook signature."""
        computed = hmac.new(
            self.viber_config.auth_token.encode('utf-8'),
            body,
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(computed, signature)

    async def handle_webhook(self, body: str, signature: Optional[str] = None) -> None:
        """
        Handle incoming webhook request from Viber.
        Should be called from your webhook endpoint.
        """
        try:
            # Verify signature if provided
            if signature:
                if not self.verify_signature(body.encode('utf-8'), signature):
                    logger.error("Invalid webhook signature")
                    return

            data = json.loads(body)
            event_type = data.get("event")

            # Handle different event types
            if event_type == "message":
                await self._handle_message_event(data)
            elif event_type == "conversation_started":
                await self._handle_conversation_started(data)
            elif event_type == "subscribed":
                await self._handle_subscribed(data)
            elif event_type == "unsubscribed":
                await self._handle_unsubscribed(data)
            elif event_type == "delivered":
                await self._handle_delivered(data)
            elif event_type == "seen":
                await self._handle_seen(data)
            elif event_type == "failed":
                await self._handle_failed(data)

        except Exception as e:
            logger.error(f"Error handling webhook: {e}")

    async def _handle_message_event(self, data: Dict[str, Any]) -> None:
        """Handle incoming message event."""
        message = self._convert_message(data)
        await self._dispatch_message(message)

    async def _handle_conversation_started(self, data: Dict[str, Any]) -> None:
        """Handle conversation started event (user opened chat)."""
        user_data = data.get("user", {})
        user_id = user_data.get("id")
        logger.info(f"Conversation started with user: {user_id}")

        # Cache user info
        if user_id:
            self._user_cache[user_id] = self._parse_user(user_data)

        # Check for callback handler
        if "conversation_started" in self._callback_handlers:
            handler = self._callback_handlers["conversation_started"]
            await handler(data)

    async def _handle_subscribed(self, data: Dict[str, Any]) -> None:
        """Handle user subscription event."""
        user_data = data.get("user", {})
        user_id = user_data.get("id")
        logger.info(f"User subscribed: {user_id}")

        if "subscribed" in self._callback_handlers:
            handler = self._callback_handlers["subscribed"]
            await handler(data)

    async def _handle_unsubscribed(self, data: Dict[str, Any]) -> None:
        """Handle user unsubscription event."""
        user_id = data.get("user_id")
        logger.info(f"User unsubscribed: {user_id}")

        # Remove from cache
        if user_id in self._user_cache:
            del self._user_cache[user_id]

        if "unsubscribed" in self._callback_handlers:
            handler = self._callback_handlers["unsubscribed"]
            await handler(data)

    async def _handle_delivered(self, data: Dict[str, Any]) -> None:
        """Handle message delivered event."""
        if "delivered" in self._callback_handlers:
            handler = self._callback_handlers["delivered"]
            await handler(data)

    async def _handle_seen(self, data: Dict[str, Any]) -> None:
        """Handle message seen event."""
        if "seen" in self._callback_handlers:
            handler = self._callback_handlers["seen"]
            await handler(data)

    async def _handle_failed(self, data: Dict[str, Any]) -> None:
        """Handle message failed event."""
        logger.error(f"Message delivery failed: {data}")

        if "failed" in self._callback_handlers:
            handler = self._callback_handlers["failed"]
            await handler(data)

    def _parse_user(self, user_data: Dict[str, Any]) -> ViberUser:
        """Parse user data into ViberUser object."""
        return ViberUser(
            id=user_data.get("id", ""),
            name=user_data.get("name", ""),
            avatar=user_data.get("avatar"),
            country=user_data.get("country"),
            language=user_data.get("language"),
            api_version=user_data.get("api_version", 1),
            primary_device_os=user_data.get("primary_device_os"),
            viber_version=user_data.get("viber_version"),
            device_type=user_data.get("device_type"),
        )

    def _convert_message(self, data: Dict[str, Any]) -> Message:
        """Convert Viber webhook data to unified Message format."""
        sender_data = data.get("sender", {})
        message_data = data.get("message", {})

        sender_id = sender_data.get("id", "")
        sender_name = sender_data.get("name", "")
        message_token = str(data.get("message_token", ""))
        timestamp = data.get("timestamp", int(datetime.now().timestamp() * 1000))

        # Cache user
        if sender_id:
            self._user_cache[sender_id] = self._parse_user(sender_data)

        # Extract content
        text = ""
        media = []
        msg_type = message_data.get("type", "text")

        if msg_type == "text":
            text = message_data.get("text", "")
        elif msg_type == "picture":
            media.append(MediaAttachment(
                type=MessageType.IMAGE,
                url=message_data.get("media"),
                caption=message_data.get("text"),
                file_name=message_data.get("file_name"),
                file_size=message_data.get("size"),
            ))
        elif msg_type == "video":
            media.append(MediaAttachment(
                type=MessageType.VIDEO,
                url=message_data.get("media"),
                file_size=message_data.get("size"),
            ))
        elif msg_type == "file":
            media.append(MediaAttachment(
                type=MessageType.DOCUMENT,
                url=message_data.get("media"),
                file_name=message_data.get("file_name"),
                file_size=message_data.get("size"),
            ))
        elif msg_type == "contact":
            contact = message_data.get("contact", {})
            text = f"[contact:{contact.get('name', '')} - {contact.get('phone_number', '')}]"
        elif msg_type == "location":
            location = message_data.get("location", {})
            text = f"[location:{location.get('lat', '')},{location.get('lon', '')}]"
        elif msg_type == "sticker":
            sticker_id = message_data.get("sticker_id")
            text = f"[sticker:{sticker_id}]"
        elif msg_type == "url":
            text = message_data.get("media", "")

        return Message(
            id=message_token,
            channel=self.name,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_id=sender_id,  # Viber uses user ID as chat ID
            text=text,
            media=media,
            timestamp=datetime.fromtimestamp(timestamp / 1000),
            is_group=False,  # Viber bots are 1:1
            raw={
                'message_type': msg_type,
                'sender': sender_data,
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
        """Send a message to a Viber user."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            # Build message payload
            payload = {
                "receiver": chat_id,
                "min_api_version": 1,
                "sender": {
                    "name": self.viber_config.bot_name or self._account_info.get("name", "Bot"),
                },
            }

            if self.viber_config.bot_avatar:
                payload["sender"]["avatar"] = self.viber_config.bot_avatar

            # Handle media
            if media and len(media) > 0:
                return await self._send_media_message(chat_id, media[0], text, buttons)

            # Text message
            payload["type"] = "text"
            payload["text"] = text

            # Add keyboard if buttons provided
            if buttons and self.viber_config.enable_keyboard:
                keyboard = self._build_keyboard(buttons)
                payload["keyboard"] = keyboard.to_dict()

            async with self._session.post(VIBER_API_SEND_MESSAGE, json=payload) as response:
                data = await response.json()

                if data.get("status") == 0:
                    return SendResult(
                        success=True,
                        message_id=str(data.get("message_token")),
                    )
                else:
                    error_msg = data.get("status_message", "Unknown error")
                    status = data.get("status")

                    if status == 3:  # Rate limited
                        raise ChannelRateLimitError(60)

                    return SendResult(success=False, error=error_msg)

        except ChannelRateLimitError:
            raise
        except Exception as e:
            logger.error(f"Failed to send Viber message: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_media_message(
        self,
        chat_id: str,
        media: MediaAttachment,
        caption: Optional[str] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a media message."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            payload = {
                "receiver": chat_id,
                "min_api_version": 1,
                "sender": {
                    "name": self.viber_config.bot_name or self._account_info.get("name", "Bot"),
                },
            }

            if self.viber_config.bot_avatar:
                payload["sender"]["avatar"] = self.viber_config.bot_avatar

            # Determine message type
            if media.type == MessageType.IMAGE:
                payload["type"] = "picture"
                payload["media"] = media.url
                if caption:
                    payload["text"] = caption
            elif media.type == MessageType.VIDEO:
                payload["type"] = "video"
                payload["media"] = media.url
                payload["size"] = media.file_size or 0
            elif media.type == MessageType.DOCUMENT:
                payload["type"] = "file"
                payload["media"] = media.url
                payload["file_name"] = media.file_name or "file"
                payload["size"] = media.file_size or 0
            elif media.type == MessageType.AUDIO:
                payload["type"] = "file"
                payload["media"] = media.url
                payload["file_name"] = media.file_name or "audio"
                payload["size"] = media.file_size or 0
            else:
                # Fall back to file
                payload["type"] = "file"
                payload["media"] = media.url
                payload["file_name"] = media.file_name or "file"

            # Add keyboard
            if buttons and self.viber_config.enable_keyboard:
                keyboard = self._build_keyboard(buttons)
                payload["keyboard"] = keyboard.to_dict()

            async with self._session.post(VIBER_API_SEND_MESSAGE, json=payload) as response:
                data = await response.json()

                if data.get("status") == 0:
                    return SendResult(
                        success=True,
                        message_id=str(data.get("message_token")),
                    )
                else:
                    return SendResult(
                        success=False,
                        error=data.get("status_message", "Unknown error"),
                    )

        except Exception as e:
            logger.error(f"Failed to send Viber media: {e}")
            return SendResult(success=False, error=str(e))

    def _build_keyboard(self, buttons: List[Dict]) -> Keyboard:
        """Build a keyboard from button definitions."""
        keyboard = Keyboard(bg_color=self.viber_config.default_keyboard_bg_color)

        for btn in buttons:
            text = btn.get("text", "")

            if btn.get("url"):
                keyboard.add_url_button(text, btn["url"])
            elif btn.get("callback_data"):
                keyboard.add_button(
                    text,
                    action_type="reply",
                    action_body=btn["callback_data"],
                )
            else:
                keyboard.add_button(text)

        return keyboard

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """
        Edit a message.
        Note: Viber doesn't support message editing, sends new message.
        """
        logger.warning("Viber doesn't support message editing, sending new message")
        return await self.send_message(chat_id, text, buttons=buttons)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """
        Delete a message.
        Note: Viber doesn't support message deletion.
        """
        logger.warning("Viber doesn't support message deletion")
        return False

    async def send_typing(self, chat_id: str) -> None:
        """
        Send typing indicator.
        Note: Viber doesn't have a typing indicator API.
        """
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get user information."""
        user = await self.get_user_details(chat_id)
        if user:
            return {
                'id': user.id,
                'name': user.name,
                'avatar': user.avatar,
                'country': user.country,
                'language': user.language,
                'device_type': user.device_type,
            }
        return None

    # Viber-specific methods

    def register_event_handler(
        self,
        event_type: str,
        handler: Callable[[Dict[str, Any]], Any],
    ) -> None:
        """Register a handler for Viber events."""
        self._callback_handlers[event_type] = handler

    async def get_user_details(self, user_id: str) -> Optional[ViberUser]:
        """Get detailed user information."""
        # Check cache first
        if user_id in self._user_cache:
            return self._user_cache[user_id]

        if not self._session:
            return None

        try:
            payload = {"id": user_id}

            async with self._session.post(VIBER_API_GET_USER_DETAILS, json=payload) as response:
                data = await response.json()

                if data.get("status") == 0:
                    user_data = data.get("user", {})
                    user = self._parse_user(user_data)
                    self._user_cache[user_id] = user
                    return user

            return None

        except Exception as e:
            logger.error(f"Error getting user details: {e}")
            return None

    async def check_online_status(self, user_ids: List[str]) -> Dict[str, int]:
        """
        Check online status of users.

        Returns dict mapping user_id to online_status:
        0 = offline, 1 = online, 2 = undisclosed
        """
        if not self._session:
            return {}

        try:
            payload = {"ids": user_ids}

            async with self._session.post(VIBER_API_GET_ONLINE, json=payload) as response:
                data = await response.json()

                if data.get("status") == 0:
                    result = {}
                    for user in data.get("users", []):
                        result[user["id"]] = user.get("online_status", 2)
                    return result

            return {}

        except Exception as e:
            logger.error(f"Error checking online status: {e}")
            return {}

    async def broadcast_message(
        self,
        user_ids: List[str],
        text: str,
        media: Optional[MediaAttachment] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> Dict[str, SendResult]:
        """
        Send a broadcast message to multiple users.
        Note: Maximum 500 users per request.
        """
        if not self._session:
            return {}

        results = {}

        # Process in batches of 500
        for i in range(0, len(user_ids), 500):
            batch = user_ids[i:i + 500]

            try:
                payload = {
                    "broadcast_list": batch,
                    "min_api_version": 1,
                    "sender": {
                        "name": self.viber_config.bot_name or self._account_info.get("name", "Bot"),
                    },
                }

                if self.viber_config.bot_avatar:
                    payload["sender"]["avatar"] = self.viber_config.bot_avatar

                if media:
                    if media.type == MessageType.IMAGE:
                        payload["type"] = "picture"
                        payload["media"] = media.url
                        if text:
                            payload["text"] = text
                    else:
                        payload["type"] = "text"
                        payload["text"] = text
                else:
                    payload["type"] = "text"
                    payload["text"] = text

                if buttons and self.viber_config.enable_keyboard:
                    keyboard = self._build_keyboard(buttons)
                    payload["keyboard"] = keyboard.to_dict()

                async with self._session.post(VIBER_API_BROADCAST, json=payload) as response:
                    data = await response.json()

                    if data.get("status") == 0:
                        for uid in batch:
                            results[uid] = SendResult(success=True)
                    else:
                        error = data.get("status_message", "Unknown error")
                        failed_list = data.get("failed_list", [])

                        for uid in batch:
                            if uid in failed_list:
                                results[uid] = SendResult(success=False, error=error)
                            else:
                                results[uid] = SendResult(success=True)

            except Exception as e:
                logger.error(f"Broadcast batch failed: {e}")
                for uid in batch:
                    results[uid] = SendResult(success=False, error=str(e))

        return results

    async def send_keyboard(
        self,
        chat_id: str,
        text: str,
        keyboard: Keyboard,
    ) -> SendResult:
        """Send a message with custom keyboard."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            payload = {
                "receiver": chat_id,
                "min_api_version": 1,
                "type": "text",
                "text": text,
                "sender": {
                    "name": self.viber_config.bot_name or self._account_info.get("name", "Bot"),
                },
                "keyboard": keyboard.to_dict(),
            }

            if self.viber_config.bot_avatar:
                payload["sender"]["avatar"] = self.viber_config.bot_avatar

            async with self._session.post(VIBER_API_SEND_MESSAGE, json=payload) as response:
                data = await response.json()

                if data.get("status") == 0:
                    return SendResult(
                        success=True,
                        message_id=str(data.get("message_token")),
                    )
                else:
                    return SendResult(
                        success=False,
                        error=data.get("status_message", "Unknown error"),
                    )

        except Exception as e:
            logger.error(f"Failed to send keyboard: {e}")
            return SendResult(success=False, error=str(e))

    async def send_carousel(
        self,
        chat_id: str,
        items: List[CarouselItem],
        alt_text: str = "Carousel message",
    ) -> SendResult:
        """Send a carousel message (rich media)."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            # Build rich media content
            elements = []
            for item in items:
                elements.extend(item.to_dict())

            payload = {
                "receiver": chat_id,
                "min_api_version": 7,  # Rich media requires API v7+
                "type": "rich_media",
                "rich_media": {
                    "Type": "rich_media",
                    "ButtonsGroupColumns": 6,
                    "ButtonsGroupRows": 6,
                    "BgColor": "#FFFFFF",
                    "Buttons": elements,
                },
                "sender": {
                    "name": self.viber_config.bot_name or self._account_info.get("name", "Bot"),
                },
                "alt_text": alt_text,
            }

            if self.viber_config.bot_avatar:
                payload["sender"]["avatar"] = self.viber_config.bot_avatar

            async with self._session.post(VIBER_API_SEND_MESSAGE, json=payload) as response:
                data = await response.json()

                if data.get("status") == 0:
                    return SendResult(
                        success=True,
                        message_id=str(data.get("message_token")),
                    )
                else:
                    return SendResult(
                        success=False,
                        error=data.get("status_message", "Unknown error"),
                    )

        except Exception as e:
            logger.error(f"Failed to send carousel: {e}")
            return SendResult(success=False, error=str(e))

    async def send_location(
        self,
        chat_id: str,
        latitude: float,
        longitude: float,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a location message."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            payload = {
                "receiver": chat_id,
                "min_api_version": 1,
                "type": "location",
                "location": {
                    "lat": latitude,
                    "lon": longitude,
                },
                "sender": {
                    "name": self.viber_config.bot_name or self._account_info.get("name", "Bot"),
                },
            }

            if self.viber_config.bot_avatar:
                payload["sender"]["avatar"] = self.viber_config.bot_avatar

            if buttons and self.viber_config.enable_keyboard:
                keyboard = self._build_keyboard(buttons)
                payload["keyboard"] = keyboard.to_dict()

            async with self._session.post(VIBER_API_SEND_MESSAGE, json=payload) as response:
                data = await response.json()

                if data.get("status") == 0:
                    return SendResult(
                        success=True,
                        message_id=str(data.get("message_token")),
                    )
                else:
                    return SendResult(
                        success=False,
                        error=data.get("status_message", "Unknown error"),
                    )

        except Exception as e:
            logger.error(f"Failed to send location: {e}")
            return SendResult(success=False, error=str(e))


def create_viber_adapter(
    auth_token: str = None,
    bot_name: str = None,
    **kwargs
) -> ViberAdapter:
    """
    Factory function to create Viber adapter.

    Args:
        auth_token: Viber bot auth token (or set VIBER_AUTH_TOKEN env var)
        bot_name: Bot display name (or set VIBER_BOT_NAME env var)
        **kwargs: Additional config options

    Returns:
        Configured ViberAdapter
    """
    auth_token = auth_token or os.getenv("VIBER_AUTH_TOKEN")
    bot_name = bot_name or os.getenv("VIBER_BOT_NAME", "Bot")

    if not auth_token:
        raise ValueError("Viber auth token required")

    config = ViberConfig(
        auth_token=auth_token,
        bot_name=bot_name,
        **kwargs
    )
    return ViberAdapter(config)
