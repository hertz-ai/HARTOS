"""
Zalo Channel Adapter

Implements Zalo Official Account (OA) API integration.
Based on HevolveBot extension patterns for Vietnamese messaging platform.

Features:
- Official Account (OA) API integration
- Text, image, file messaging
- Quick reply buttons
- List templates
- Request user info
- Broadcast messaging
- Webhook signature validation
- User interest tagging
- Follower management
- Reconnection with exponential backoff
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import hmac
import hashlib
import time
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum

try:
    import aiohttp
    HAS_ZALO = True
except ImportError:
    HAS_ZALO = False

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

# Zalo API endpoints
ZALO_OA_API_URL = "https://openapi.zalo.me/v2.0/oa"
ZALO_GRAPH_URL = "https://graph.zalo.me/v2.0"
ZALO_UPLOAD_URL = "https://openapi.zalo.me/v2.0/oa/upload"


class ZaloMessageType(Enum):
    """Zalo message types."""
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    STICKER = "sticker"
    GIF = "gif"
    LIST = "list"
    REQUEST_USER_INFO = "request_user_info"


class ZaloEventType(Enum):
    """Zalo webhook event types."""
    USER_SEND_TEXT = "user_send_text"
    USER_SEND_IMAGE = "user_send_image"
    USER_SEND_FILE = "user_send_file"
    USER_SEND_STICKER = "user_send_sticker"
    USER_SEND_GIF = "user_send_gif"
    USER_SEND_LOCATION = "user_send_location"
    FOLLOW = "follow"
    UNFOLLOW = "unfollow"
    USER_CLICK_BUTTON = "user_click_button"
    USER_SUBMIT_INFO = "user_submit_info"


@dataclass
class ZaloConfig(ChannelConfig):
    """Zalo-specific configuration."""
    app_id: str = ""
    app_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    oa_id: str = ""  # Official Account ID
    webhook_secret: str = ""
    enable_user_info_request: bool = True
    enable_broadcast: bool = False
    reconnect_attempts: int = 5
    reconnect_delay: float = 1.0


@dataclass
class ZaloUser:
    """Zalo user information."""
    user_id: str
    display_name: Optional[str] = None
    avatar: Optional[str] = None
    phone: Optional[str] = None
    is_follower: bool = True
    user_id_by_app: Optional[str] = None
    shared_info: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ZaloQuickReply:
    """Quick reply button."""
    title: str
    payload: str
    image_url: Optional[str] = None


@dataclass
class ZaloListElement:
    """Element in a list template."""
    title: str
    subtitle: Optional[str] = None
    image_url: Optional[str] = None
    default_action: Optional[Dict[str, Any]] = None


class ZaloAdapter(ChannelAdapter):
    """
    Zalo Official Account API adapter.

    Usage:
        config = ZaloConfig(
            app_id="your-app-id",
            app_secret="your-secret",
            access_token="your-token",
            oa_id="your-oa-id",
        )
        adapter = ZaloAdapter(config)
        adapter.on_message(my_handler)
        # Use with webhook endpoint
    """

    def __init__(self, config: ZaloConfig):
        if not HAS_ZALO:
            raise ImportError(
                "aiohttp not installed. "
                "Install with: pip install aiohttp"
            )

        super().__init__(config)
        self.zalo_config: ZaloConfig = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._user_cache: Dict[str, ZaloUser] = {}
        self._follow_handlers: List[Callable] = []
        self._unfollow_handlers: List[Callable] = []
        self._button_handlers: Dict[str, Callable] = {}
        self._user_info_handlers: List[Callable] = []
        self._reconnect_count: int = 0

    @property
    def name(self) -> str:
        return "zalo"

    async def connect(self) -> bool:
        """Initialize Zalo OA API client."""
        if not self.zalo_config.access_token:
            logger.error("Zalo access token required")
            return False

        if not self.zalo_config.oa_id:
            logger.error("Zalo OA ID required")
            return False

        try:
            # Create aiohttp session
            self._session = aiohttp.ClientSession()

            # Verify token by getting OA info
            oa_info = await self._get_oa_info()
            if oa_info:
                logger.info(f"Zalo OA connected: {oa_info.get('name', 'Unknown')}")
                self.status = ChannelStatus.CONNECTED
                self._reconnect_count = 0
                return True
            else:
                logger.error("Failed to verify Zalo OA token")
                self.status = ChannelStatus.ERROR
                return False

        except Exception as e:
            logger.error(f"Failed to connect to Zalo: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect Zalo adapter."""
        if self._session:
            await self._session.close()
            self._session = None

        self._user_cache.clear()
        self.status = ChannelStatus.DISCONNECTED

    async def _get_oa_info(self) -> Optional[Dict[str, Any]]:
        """Get OA information to verify connection."""
        if not self._session:
            return None

        try:
            headers = {
                "access_token": self.zalo_config.access_token,
            }

            async with self._session.get(
                f"{ZALO_OA_API_URL}/getoa",
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("error") == 0:
                        return data.get("data", {})
                    else:
                        logger.error(f"Zalo API error: {data.get('message')}")

        except Exception as e:
            logger.error(f"Failed to get OA info: {e}")

        return None

    def validate_signature(self, body: str, timestamp: str, signature: str) -> bool:
        """Validate webhook signature."""
        if not self.zalo_config.webhook_secret:
            return True  # No secret configured, skip validation

        try:
            # Zalo uses: sha256(app_id + body + timestamp + secret)
            data = f"{self.zalo_config.app_id}{body}{timestamp}{self.zalo_config.webhook_secret}"
            expected = hashlib.sha256(data.encode()).hexdigest()
            return hmac.compare_digest(signature, expected)
        except Exception:
            return False

    async def handle_webhook(self, body: Dict[str, Any]) -> None:
        """
        Handle incoming webhook request from Zalo.
        Should be called from your webhook endpoint.
        """
        event_name = body.get("event_name")
        sender = body.get("sender", {})
        message = body.get("message", {})
        timestamp = body.get("timestamp")

        if not event_name:
            logger.warning("No event_name in Zalo webhook")
            return

        try:
            event_type = ZaloEventType(event_name)
        except ValueError:
            logger.warning(f"Unknown Zalo event: {event_name}")
            return

        # Handle different event types
        if event_type == ZaloEventType.USER_SEND_TEXT:
            await self._handle_text_message(sender, message, timestamp)
        elif event_type == ZaloEventType.USER_SEND_IMAGE:
            await self._handle_image_message(sender, message, timestamp)
        elif event_type == ZaloEventType.USER_SEND_FILE:
            await self._handle_file_message(sender, message, timestamp)
        elif event_type == ZaloEventType.USER_SEND_STICKER:
            await self._handle_sticker_message(sender, message, timestamp)
        elif event_type == ZaloEventType.USER_SEND_GIF:
            await self._handle_gif_message(sender, message, timestamp)
        elif event_type == ZaloEventType.USER_SEND_LOCATION:
            await self._handle_location_message(sender, message, timestamp)
        elif event_type == ZaloEventType.FOLLOW:
            await self._handle_follow(sender, timestamp)
        elif event_type == ZaloEventType.UNFOLLOW:
            await self._handle_unfollow(sender, timestamp)
        elif event_type == ZaloEventType.USER_CLICK_BUTTON:
            await self._handle_button_click(sender, message, timestamp)
        elif event_type == ZaloEventType.USER_SUBMIT_INFO:
            await self._handle_user_info_submit(sender, body.get("info", {}), timestamp)

    async def _handle_text_message(
        self,
        sender: Dict[str, Any],
        message: Dict[str, Any],
        timestamp: int,
    ) -> None:
        """Handle text message."""
        msg = self._convert_message(sender, message, timestamp)
        await self._dispatch_message(msg)

    async def _handle_image_message(
        self,
        sender: Dict[str, Any],
        message: Dict[str, Any],
        timestamp: int,
    ) -> None:
        """Handle image message."""
        msg = self._convert_message(sender, message, timestamp)
        msg.media.append(MediaAttachment(
            type=MessageType.IMAGE,
            url=message.get("url"),
            file_id=message.get("msg_id"),
        ))
        await self._dispatch_message(msg)

    async def _handle_file_message(
        self,
        sender: Dict[str, Any],
        message: Dict[str, Any],
        timestamp: int,
    ) -> None:
        """Handle file message."""
        msg = self._convert_message(sender, message, timestamp)
        msg.media.append(MediaAttachment(
            type=MessageType.DOCUMENT,
            url=message.get("url"),
            file_name=message.get("name"),
            file_size=message.get("size"),
        ))
        await self._dispatch_message(msg)

    async def _handle_sticker_message(
        self,
        sender: Dict[str, Any],
        message: Dict[str, Any],
        timestamp: int,
    ) -> None:
        """Handle sticker message."""
        msg = self._convert_message(sender, message, timestamp)
        msg.text = f"[sticker:{message.get('sticker_id')}]"
        await self._dispatch_message(msg)

    async def _handle_gif_message(
        self,
        sender: Dict[str, Any],
        message: Dict[str, Any],
        timestamp: int,
    ) -> None:
        """Handle GIF message."""
        msg = self._convert_message(sender, message, timestamp)
        msg.media.append(MediaAttachment(
            type=MessageType.IMAGE,
            url=message.get("url"),
        ))
        await self._dispatch_message(msg)

    async def _handle_location_message(
        self,
        sender: Dict[str, Any],
        message: Dict[str, Any],
        timestamp: int,
    ) -> None:
        """Handle location message."""
        msg = self._convert_message(sender, message, timestamp)
        lat = message.get("latitude")
        lon = message.get("longitude")
        msg.text = f"[location:{lat},{lon}]"
        await self._dispatch_message(msg)

    async def _handle_follow(self, sender: Dict[str, Any], timestamp: int) -> None:
        """Handle follow event."""
        user = ZaloUser(
            user_id=sender.get("id", ""),
        )

        logger.info(f"User followed: {user.user_id}")

        for handler in self._follow_handlers:
            try:
                result = handler(user)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Follow handler error: {e}")

    async def _handle_unfollow(self, sender: Dict[str, Any], timestamp: int) -> None:
        """Handle unfollow event."""
        user_id = sender.get("id", "")
        logger.info(f"User unfollowed: {user_id}")

        for handler in self._unfollow_handlers:
            try:
                result = handler(user_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Unfollow handler error: {e}")

    async def _handle_button_click(
        self,
        sender: Dict[str, Any],
        message: Dict[str, Any],
        timestamp: int,
    ) -> None:
        """Handle quick reply button click."""
        payload = message.get("payload", "")

        if payload in self._button_handlers:
            handler = self._button_handlers[payload]
            try:
                result = handler(sender, message)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Button handler error: {e}")
        else:
            # Convert to regular message
            msg = self._convert_message(sender, message, timestamp)
            msg.text = payload
            msg.raw["button_payload"] = payload
            await self._dispatch_message(msg)

    async def _handle_user_info_submit(
        self,
        sender: Dict[str, Any],
        info: Dict[str, Any],
        timestamp: int,
    ) -> None:
        """Handle user info submission."""
        user = ZaloUser(
            user_id=sender.get("id", ""),
            display_name=info.get("name"),
            phone=info.get("phone"),
            avatar=info.get("avatar"),
            shared_info=info,
        )

        self._user_cache[user.user_id] = user

        for handler in self._user_info_handlers:
            try:
                result = handler(user)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"User info handler error: {e}")

    def _convert_message(
        self,
        sender: Dict[str, Any],
        message: Dict[str, Any],
        timestamp: int,
    ) -> Message:
        """Convert Zalo event to unified Message format."""
        user_id = sender.get("id", "")

        return Message(
            id=message.get("msg_id", str(timestamp)),
            channel=self.name,
            sender_id=user_id,
            sender_name=sender.get("name"),
            chat_id=user_id,  # 1:1 chat with user
            text=message.get("text", ""),
            timestamp=datetime.fromtimestamp(timestamp / 1000) if timestamp else datetime.now(),
            is_group=False,
            raw={
                "sender": sender,
                "message": message,
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
        """Send a message to a Zalo user."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            # Handle quick reply buttons
            if buttons:
                return await self._send_with_buttons(chat_id, text, buttons)

            # Handle media
            if media and len(media) > 0:
                for m in media:
                    if m.type == MessageType.IMAGE:
                        await self._send_image(chat_id, m.url, text)
                    elif m.type == MessageType.DOCUMENT:
                        await self._send_file(chat_id, m.url, m.file_name)
                return SendResult(success=True)

            # Send text message
            return await self._send_text(chat_id, text)

        except Exception as e:
            logger.error(f"Failed to send Zalo message: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_text(self, user_id: str, text: str) -> SendResult:
        """Send text message."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            headers = {
                "access_token": self.zalo_config.access_token,
                "Content-Type": "application/json",
            }

            data = {
                "recipient": {"user_id": user_id},
                "message": {"text": text},
            }

            async with self._session.post(
                f"{ZALO_OA_API_URL}/message",
                headers=headers,
                json=data,
            ) as resp:
                result = await resp.json()
                if result.get("error") == 0:
                    return SendResult(
                        success=True,
                        message_id=result.get("data", {}).get("message_id"),
                    )
                else:
                    error_msg = result.get("message", "Unknown error")
                    if result.get("error") == -201:
                        raise ChannelRateLimitError()
                    return SendResult(success=False, error=error_msg)

        except ChannelRateLimitError:
            raise
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def _send_with_buttons(
        self,
        user_id: str,
        text: str,
        buttons: List[Dict],
    ) -> SendResult:
        """Send message with quick reply buttons."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            headers = {
                "access_token": self.zalo_config.access_token,
                "Content-Type": "application/json",
            }

            # Build quick replies
            quick_replies = []
            for btn in buttons:
                qr = {
                    "content_type": "text",
                    "title": btn.get("text", ""),
                    "payload": btn.get("callback_data", btn.get("text", "")),
                }
                if btn.get("image_url"):
                    qr["image_url"] = btn["image_url"]
                quick_replies.append(qr)

            data = {
                "recipient": {"user_id": user_id},
                "message": {
                    "text": text,
                    "quick_replies": quick_replies,
                },
            }

            async with self._session.post(
                f"{ZALO_OA_API_URL}/message",
                headers=headers,
                json=data,
            ) as resp:
                result = await resp.json()
                if result.get("error") == 0:
                    return SendResult(
                        success=True,
                        message_id=result.get("data", {}).get("message_id"),
                    )
                else:
                    return SendResult(success=False, error=result.get("message"))

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def _send_image(
        self,
        user_id: str,
        image_url: str,
        caption: str = "",
    ) -> SendResult:
        """Send image message."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            headers = {
                "access_token": self.zalo_config.access_token,
                "Content-Type": "application/json",
            }

            data = {
                "recipient": {"user_id": user_id},
                "message": {
                    "attachment": {
                        "type": "template",
                        "payload": {
                            "template_type": "media",
                            "elements": [{
                                "media_type": "image",
                                "url": image_url,
                            }],
                        },
                    },
                },
            }

            async with self._session.post(
                f"{ZALO_OA_API_URL}/message",
                headers=headers,
                json=data,
            ) as resp:
                result = await resp.json()
                if result.get("error") == 0:
                    # Send caption as separate message if provided
                    if caption:
                        await self._send_text(user_id, caption)
                    return SendResult(
                        success=True,
                        message_id=result.get("data", {}).get("message_id"),
                    )
                else:
                    return SendResult(success=False, error=result.get("message"))

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def _send_file(
        self,
        user_id: str,
        file_url: str,
        file_name: str = "",
    ) -> SendResult:
        """Send file message."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            headers = {
                "access_token": self.zalo_config.access_token,
                "Content-Type": "application/json",
            }

            data = {
                "recipient": {"user_id": user_id},
                "message": {
                    "attachment": {
                        "type": "file",
                        "payload": {
                            "url": file_url,
                        },
                    },
                },
            }

            async with self._session.post(
                f"{ZALO_OA_API_URL}/message",
                headers=headers,
                json=data,
            ) as resp:
                result = await resp.json()
                if result.get("error") == 0:
                    return SendResult(
                        success=True,
                        message_id=result.get("data", {}).get("message_id"),
                    )
                else:
                    return SendResult(success=False, error=result.get("message"))

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """
        Edit a Zalo message.
        Note: Zalo doesn't support message editing.
        """
        logger.warning("Zalo doesn't support message editing")
        return SendResult(success=False, error="Not supported")

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """
        Delete a Zalo message.
        Note: Zalo doesn't support message deletion.
        """
        logger.warning("Zalo doesn't support message deletion")
        return False

    async def send_typing(self, chat_id: str) -> None:
        """
        Send typing indicator.
        Note: Zalo doesn't support typing indicators.
        """
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get user profile information."""
        return await self.get_user_profile(chat_id)

    # Zalo-specific methods

    async def get_user_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user profile from Zalo."""
        if not self._session:
            return None

        # Check cache
        if user_id in self._user_cache:
            user = self._user_cache[user_id]
            return {
                "user_id": user.user_id,
                "display_name": user.display_name,
                "avatar": user.avatar,
                "phone": user.phone,
            }

        try:
            headers = {
                "access_token": self.zalo_config.access_token,
            }

            async with self._session.get(
                f"{ZALO_OA_API_URL}/getprofile",
                headers=headers,
                params={"user_id": user_id},
            ) as resp:
                result = await resp.json()
                if result.get("error") == 0:
                    data = result.get("data", {})
                    return {
                        "user_id": user_id,
                        "display_name": data.get("display_name"),
                        "avatar": data.get("avatar"),
                        "user_id_by_app": data.get("user_id_by_app"),
                    }

        except Exception as e:
            logger.error(f"Failed to get user profile: {e}")

        return None

    def on_follow(self, handler: Callable[[ZaloUser], Any]) -> None:
        """Register a follow event handler."""
        self._follow_handlers.append(handler)

    def on_unfollow(self, handler: Callable[[str], Any]) -> None:
        """Register an unfollow event handler."""
        self._unfollow_handlers.append(handler)

    def register_button_handler(
        self,
        payload: str,
        handler: Callable[[Dict, Dict], Any],
    ) -> None:
        """Register a button click handler."""
        self._button_handlers[payload] = handler

    def on_user_info(self, handler: Callable[[ZaloUser], Any]) -> None:
        """Register a user info submission handler."""
        self._user_info_handlers.append(handler)

    async def request_user_info(
        self,
        user_id: str,
        title: str = "Share your info",
        subtitle: str = "",
    ) -> SendResult:
        """Request user info (name, phone, etc.)."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        if not self.zalo_config.enable_user_info_request:
            return SendResult(success=False, error="User info request disabled")

        try:
            headers = {
                "access_token": self.zalo_config.access_token,
                "Content-Type": "application/json",
            }

            data = {
                "recipient": {"user_id": user_id},
                "message": {
                    "attachment": {
                        "type": "template",
                        "payload": {
                            "template_type": "request_user_info",
                            "elements": [{
                                "title": title,
                                "subtitle": subtitle,
                                "image_url": "",
                            }],
                        },
                    },
                },
            }

            async with self._session.post(
                f"{ZALO_OA_API_URL}/message",
                headers=headers,
                json=data,
            ) as resp:
                result = await resp.json()
                if result.get("error") == 0:
                    return SendResult(success=True)
                else:
                    return SendResult(success=False, error=result.get("message"))

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_list_template(
        self,
        user_id: str,
        elements: List[ZaloListElement],
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a list template message."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            headers = {
                "access_token": self.zalo_config.access_token,
                "Content-Type": "application/json",
            }

            element_list = []
            for elem in elements:
                el = {
                    "title": elem.title,
                }
                if elem.subtitle:
                    el["subtitle"] = elem.subtitle
                if elem.image_url:
                    el["image_url"] = elem.image_url
                if elem.default_action:
                    el["default_action"] = elem.default_action
                element_list.append(el)

            payload = {
                "template_type": "list",
                "elements": element_list,
            }

            if buttons:
                payload["buttons"] = [
                    {
                        "title": btn.get("text", ""),
                        "type": "oa.open.url" if btn.get("url") else "oa.query.show",
                        "payload": btn.get("url") or btn.get("callback_data", ""),
                    }
                    for btn in buttons
                ]

            data = {
                "recipient": {"user_id": user_id},
                "message": {
                    "attachment": {
                        "type": "template",
                        "payload": payload,
                    },
                },
            }

            async with self._session.post(
                f"{ZALO_OA_API_URL}/message",
                headers=headers,
                json=data,
            ) as resp:
                result = await resp.json()
                if result.get("error") == 0:
                    return SendResult(success=True)
                else:
                    return SendResult(success=False, error=result.get("message"))

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def broadcast_message(
        self,
        text: str,
        target_followers: bool = True,
    ) -> SendResult:
        """Broadcast message to all followers (requires approval)."""
        if not self.zalo_config.enable_broadcast:
            return SendResult(success=False, error="Broadcast disabled")

        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            headers = {
                "access_token": self.zalo_config.access_token,
                "Content-Type": "application/json",
            }

            data = {
                "message": {"text": text},
                "target": {"all_followers": target_followers},
            }

            async with self._session.post(
                f"{ZALO_OA_API_URL}/message/broadcast",
                headers=headers,
                json=data,
            ) as resp:
                result = await resp.json()
                if result.get("error") == 0:
                    return SendResult(success=True)
                else:
                    return SendResult(success=False, error=result.get("message"))

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def tag_user(self, user_id: str, tag_name: str) -> bool:
        """Tag a user with an interest tag."""
        if not self._session:
            return False

        try:
            headers = {
                "access_token": self.zalo_config.access_token,
                "Content-Type": "application/json",
            }

            data = {
                "user_id": user_id,
                "tag_name": tag_name,
            }

            async with self._session.post(
                f"{ZALO_OA_API_URL}/tag/tagfollower",
                headers=headers,
                json=data,
            ) as resp:
                result = await resp.json()
                return result.get("error") == 0

        except Exception as e:
            logger.error(f"Failed to tag user: {e}")
            return False

    async def untag_user(self, user_id: str, tag_name: str) -> bool:
        """Remove tag from a user."""
        if not self._session:
            return False

        try:
            headers = {
                "access_token": self.zalo_config.access_token,
                "Content-Type": "application/json",
            }

            data = {
                "user_id": user_id,
                "tag_name": tag_name,
            }

            async with self._session.post(
                f"{ZALO_OA_API_URL}/tag/rmfollowerfromtag",
                headers=headers,
                json=data,
            ) as resp:
                result = await resp.json()
                return result.get("error") == 0

        except Exception as e:
            logger.error(f"Failed to untag user: {e}")
            return False

    async def refresh_access_token(self) -> bool:
        """Refresh access token using refresh token."""
        if not self.zalo_config.refresh_token:
            return False

        if not self._session:
            return False

        try:
            data = {
                "app_id": self.zalo_config.app_id,
                "app_secret": self.zalo_config.app_secret,
                "refresh_token": self.zalo_config.refresh_token,
                "grant_type": "refresh_token",
            }

            async with self._session.post(
                f"{ZALO_GRAPH_URL}/oa/access_token",
                data=data,
            ) as resp:
                result = await resp.json()
                if "access_token" in result:
                    self.zalo_config.access_token = result["access_token"]
                    if "refresh_token" in result:
                        self.zalo_config.refresh_token = result["refresh_token"]
                    logger.info("Zalo access token refreshed")
                    return True
                else:
                    logger.error(f"Failed to refresh token: {result}")
                    return False

        except Exception as e:
            logger.error(f"Token refresh error: {e}")
            return False


def create_zalo_adapter(
    app_id: str = None,
    app_secret: str = None,
    access_token: str = None,
    oa_id: str = None,
    **kwargs
) -> ZaloAdapter:
    """
    Factory function to create Zalo adapter.

    Args:
        app_id: Zalo app ID (or set ZALO_APP_ID env var)
        app_secret: Zalo app secret (or set ZALO_APP_SECRET env var)
        access_token: OA access token (or set ZALO_ACCESS_TOKEN env var)
        oa_id: Official Account ID (or set ZALO_OA_ID env var)
        **kwargs: Additional config options

    Returns:
        Configured ZaloAdapter
    """
    app_id = app_id or os.getenv("ZALO_APP_ID")
    app_secret = app_secret or os.getenv("ZALO_APP_SECRET")
    access_token = access_token or os.getenv("ZALO_ACCESS_TOKEN")
    oa_id = oa_id or os.getenv("ZALO_OA_ID")

    if not access_token:
        raise ValueError("Zalo access token required")
    if not oa_id:
        raise ValueError("Zalo OA ID required")

    config = ZaloConfig(
        app_id=app_id or "",
        app_secret=app_secret or "",
        access_token=access_token,
        oa_id=oa_id,
        **kwargs
    )
    return ZaloAdapter(config)
