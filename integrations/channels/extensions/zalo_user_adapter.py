"""
Zalo User Account Adapter

Implements Zalo personal account integration (not Official Account).
Based on HevolveBot extension patterns.

Features:
- Personal account messaging
- Group chats
- File sharing
- Reactions
- Docker-compatible
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False
import hashlib
import time
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from dataclasses import dataclass, field

from ..base import (
    ChannelAdapter,
    ChannelConfig,
    ChannelStatus,
    Message,
    MessageType,
    SendResult,
    ChannelConnectionError,
    ChannelSendError,
)

logger = logging.getLogger(__name__)


@dataclass
class ZaloUserConfig(ChannelConfig):
    """Zalo user account configuration."""
    phone_number: str = ""
    password: str = ""
    imei: str = ""  # Device IMEI for authentication
    session_cookies: str = ""  # Serialized session
    api_base: str = "https://zalo.me/api"
    poll_interval: float = 2.0

    @classmethod
    def from_env(cls) -> "ZaloUserConfig":
        """Create config from environment variables."""
        return cls(
            phone_number=os.getenv("ZALO_PHONE", ""),
            password=os.getenv("ZALO_PASSWORD", ""),
            imei=os.getenv("ZALO_IMEI", ""),
            session_cookies=os.getenv("ZALO_SESSION", ""),
        )


class ZaloUserAdapter(ChannelAdapter):
    """Zalo personal account adapter."""

    channel_type = "zalo_user"

    @property
    def name(self) -> str:
        """Get adapter name."""
        return self.channel_type

    def __init__(self, config: ZaloUserConfig):
        super().__init__(config)
        self.config: ZaloUserConfig = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._connected = False
        self._poll_task: Optional[asyncio.Task] = None
        self._message_handlers: List[Callable] = []
        self._user_id: Optional[str] = None
        self._last_msg_id: Dict[str, str] = {}
        self._access_token: Optional[str] = None

    async def connect(self) -> bool:
        """Connect to Zalo."""
        try:
            self._session = aiohttp.ClientSession()

            # Authenticate
            if self.config.session_cookies:
                # Restore session
                self._access_token = self.config.session_cookies
            else:
                # Login with credentials
                await self._login()

            # Verify session
            if not await self._verify_session():
                raise ChannelConnectionError("Failed to verify Zalo session")

            # Start polling
            self._poll_task = asyncio.create_task(self._poll_loop())

            self._connected = True
            self._status = ChannelStatus.CONNECTED
            logger.info(f"Connected to Zalo as {self._user_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Zalo: {e}")
            self._status = ChannelStatus.ERROR
            raise ChannelConnectionError(str(e))

    async def _login(self) -> None:
        """Login to Zalo with credentials."""
        # Generate device signature
        device_sig = hashlib.md5(
            f"{self.config.imei}{self.config.phone_number}".encode()
        ).hexdigest()

        login_url = f"{self.config.api_base}/login"

        payload = {
            "phone": self.config.phone_number,
            "password": self.config.password,
            "imei": self.config.imei,
            "device_sig": device_sig,
        }

        async with self._session.post(login_url, json=payload) as resp:
            if resp.status != 200:
                raise ChannelConnectionError("Login failed")

            data = await resp.json()
            if data.get("error_code", -1) != 0:
                raise ChannelConnectionError(data.get("error_message", "Login failed"))

            self._access_token = data.get("data", {}).get("access_token")
            self._user_id = data.get("data", {}).get("user_id")

    async def _verify_session(self) -> bool:
        """Verify current session is valid."""
        if not self._access_token:
            return False

        url = f"{self.config.api_base}/profile"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        try:
            async with self._session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._user_id = data.get("data", {}).get("user_id")
                    return True
                return False
        except:
            return False

    async def disconnect(self) -> None:
        """Disconnect from Zalo."""
        self._connected = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._session:
            await self._session.close()
            self._session = None

        self._status = ChannelStatus.DISCONNECTED
        logger.info("Disconnected from Zalo")

    async def _poll_loop(self) -> None:
        """Poll for new messages."""
        while self._connected:
            try:
                await self._fetch_messages()
                await asyncio.sleep(self.config.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Poll error: {e}")
                await asyncio.sleep(5)

    async def _fetch_messages(self) -> None:
        """Fetch new messages from conversations."""
        url = f"{self.config.api_base}/conversations"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        try:
            async with self._session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return

                data = await resp.json()

                for conv in data.get("data", {}).get("conversations", []):
                    conv_id = conv.get("conversation_id", "")
                    last_msg = conv.get("last_message", {})
                    msg_id = last_msg.get("message_id", "")

                    # Check if new message
                    if msg_id and msg_id != self._last_msg_id.get(conv_id):
                        self._last_msg_id[conv_id] = msg_id

                        # Skip own messages
                        if last_msg.get("sender_id") == self._user_id:
                            continue

                        message = self._parse_message(last_msg, conv_id)
                        if message:
                            for handler in self._message_handlers:
                                asyncio.create_task(handler(message))

        except Exception as e:
            logger.error(f"Error fetching messages: {e}")

    def _parse_message(self, data: Dict[str, Any], conv_id: str) -> Optional[Message]:
        """Parse Zalo message to unified Message."""
        try:
            msg_type = MessageType.TEXT
            content = data.get("content", "")

            if data.get("msg_type") == "image":
                msg_type = MessageType.IMAGE
            elif data.get("msg_type") == "video":
                msg_type = MessageType.VIDEO
            elif data.get("msg_type") == "file":
                msg_type = MessageType.FILE

            return Message(
                id=data.get("message_id", ""),
                channel=self.channel_type,
                chat_id=conv_id,
                sender_id=data.get("sender_id", ""),
                sender_name=data.get("sender_name", ""),
                text=content if isinstance(content, str) else json.dumps(content),
                timestamp=datetime.fromtimestamp(data.get("timestamp", time.time()) / 1000),
                message_type=msg_type,
            )
        except Exception as e:
            logger.error(f"Error parsing message: {e}")
            return None

    def on_message(self, handler: Callable) -> None:
        """Register message handler."""
        self._message_handlers.append(handler)

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        **kwargs
    ) -> SendResult:
        """Send a message."""
        url = f"{self.config.api_base}/messages"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        payload = {
            "conversation_id": chat_id,
            "content": text,
            "msg_type": "text",
        }

        if reply_to:
            payload["reply_to"] = reply_to

        try:
            async with self._session.post(url, json=payload, headers=headers) as resp:
                if resp.status not in (200, 201):
                    raise ChannelSendError("Failed to send message")

                data = await resp.json()
                return SendResult(
                    success=True,
                    message_id=data.get("data", {}).get("message_id", ""),
                    timestamp=datetime.now()
                )

        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            raise ChannelSendError(str(e))

    async def edit_message(self, chat_id: str, message_id: str, text: str, **kwargs) -> bool:
        """Zalo doesn't support message editing."""
        return False

    async def delete_message(self, chat_id: str, message_id: str, **kwargs) -> bool:
        """Delete/recall a message."""
        url = f"{self.config.api_base}/messages/{message_id}/recall"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        try:
            async with self._session.post(url, headers=headers) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"Failed to delete message: {e}")
            return False

    async def send_typing(self, chat_id: str, **kwargs) -> None:
        """Send typing indicator."""
        # Zalo may not have public typing API
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get conversation information."""
        url = f"{self.config.api_base}/conversations/{chat_id}"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        try:
            async with self._session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()
                conv = data.get("data", {})
                return {
                    "id": conv.get("conversation_id"),
                    "name": conv.get("name"),
                    "type": conv.get("type"),
                    "member_count": conv.get("member_count", 0),
                }
        except Exception as e:
            logger.error(f"Failed to get chat info: {e}")
            return None

    async def add_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Add reaction to a message."""
        url = f"{self.config.api_base}/messages/{message_id}/reactions"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        try:
            async with self._session.post(
                url,
                json={"reaction": emoji},
                headers=headers
            ) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"Failed to add reaction: {e}")
            return False

    def get_session_token(self) -> str:
        """Get session token for persistence."""
        return self._access_token or ""


def create_zalo_user_adapter(
    phone_number: Optional[str] = None,
    password: Optional[str] = None,
    session_cookies: Optional[str] = None,
    **kwargs
) -> ZaloUserAdapter:
    """Factory function to create a Zalo user adapter."""
    config = ZaloUserConfig(
        phone_number=phone_number or os.getenv("ZALO_PHONE", ""),
        password=password or os.getenv("ZALO_PASSWORD", ""),
        session_cookies=session_cookies or os.getenv("ZALO_SESSION", ""),
        **kwargs
    )
    return ZaloUserAdapter(config)
