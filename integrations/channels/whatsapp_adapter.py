"""
WhatsApp Channel Adapter

Implements WhatsApp messaging using whatsapp-web.js (via REST API).
Supports QR pairing for authentication.

Features:
- Text messages
- Media (images, videos, documents)
- Group chats
- Message reactions
- Read receipts
- QR code authentication
"""

from __future__ import annotations

import asyncio
import logging
import os
import base64
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

from .base import (
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


class WhatsAppAdapter(ChannelAdapter):
    """
    WhatsApp messaging adapter using whatsapp-web.js REST API.

    Usage:
        config = ChannelConfig(
            webhook_url="http://localhost:3000",
            extra={"phone_number": "+1234567890"}
        )
        adapter = WhatsAppAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: ChannelConfig):
        if not HAS_AIOHTTP:
            raise ImportError(
                "aiohttp not installed. "
                "Install with: pip install aiohttp"
            )

        super().__init__(config)
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._phone_number: Optional[str] = config.extra.get("phone_number")
        self._account_id: str = config.extra.get("account_id", "default")
        self._base_url: str = config.webhook_url or "http://localhost:3000"
        self._qr_callback: Optional[Callable[[str], None]] = None
        self._authenticated = False

    @property
    def name(self) -> str:
        return "whatsapp"

    def set_qr_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback for QR code display during authentication."""
        self._qr_callback = callback

    async def connect(self) -> bool:
        """Connect to WhatsApp Web via REST API."""
        try:
            self._session = aiohttp.ClientSession()

            # Check API health
            async with self._session.get(f"{self._base_url}/api/health") as resp:
                if resp.status != 200:
                    logger.error("WhatsApp API not available")
                    return False

            # Initialize session
            async with self._session.post(
                f"{self._base_url}/api/sessions/{self._account_id}/start"
            ) as resp:
                if resp.status not in (200, 201):
                    logger.error(f"Failed to start WhatsApp session: {resp.status}")
                    return False

            # Start WebSocket for events
            asyncio.create_task(self._listen_events())

            self.status = ChannelStatus.CONNECTING
            logger.info(f"WhatsApp connecting for account: {self._account_id}")

            # Wait for authentication or QR code
            await self._wait_for_auth()

            return self._authenticated

        except aiohttp.ClientError as e:
            logger.error(f"Failed to connect to WhatsApp API: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def _wait_for_auth(self, timeout: int = 120) -> None:
        """Wait for WhatsApp authentication."""
        start_time = asyncio.get_event_loop().time()

        while not self._authenticated:
            if asyncio.get_event_loop().time() - start_time > timeout:
                logger.error("WhatsApp authentication timeout")
                self.status = ChannelStatus.ERROR
                return

            # Check session status
            if self._session:
                try:
                    async with self._session.get(
                        f"{self._base_url}/api/sessions/{self._account_id}/status"
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("authenticated"):
                                self._authenticated = True
                                self.status = ChannelStatus.CONNECTED
                                logger.info("WhatsApp authenticated successfully")
                                return
                except aiohttp.ClientError:
                    pass

            await asyncio.sleep(2)

    async def _listen_events(self) -> None:
        """Listen for WhatsApp events via WebSocket."""
        if not self._session:
            return

        try:
            ws_url = self._base_url.replace("http", "ws")
            self._ws = await self._session.ws_connect(
                f"{ws_url}/ws/{self._account_id}"
            )

            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_event(msg.json())
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WhatsApp WebSocket error: {msg.data}")
                    break

        except Exception as e:
            logger.error(f"WhatsApp event listener error: {e}")
        finally:
            self._ws = None

    async def _handle_event(self, event: Dict[str, Any]) -> None:
        """Handle incoming WhatsApp events."""
        event_type = event.get("type")

        if event_type == "qr":
            # QR code for authentication
            qr_data = event.get("qr")
            if qr_data and self._qr_callback:
                self._qr_callback(qr_data)
            logger.info("WhatsApp QR code received - scan to authenticate")

        elif event_type == "authenticated":
            self._authenticated = True
            self.status = ChannelStatus.CONNECTED
            logger.info("WhatsApp authenticated")

        elif event_type == "message":
            message = self._convert_message(event.get("data", {}))
            await self._dispatch_message(message)

        elif event_type == "disconnected":
            self._authenticated = False
            self.status = ChannelStatus.DISCONNECTED
            logger.warning("WhatsApp disconnected")

    def _convert_message(self, wa_message: Dict[str, Any]) -> Message:
        """Convert WhatsApp message to unified Message format."""
        chat = wa_message.get("chat", {})
        sender = wa_message.get("sender", {})

        # Check for media
        media = []
        if wa_message.get("hasMedia"):
            media_type = wa_message.get("type", "document")
            type_map = {
                "image": MessageType.IMAGE,
                "video": MessageType.VIDEO,
                "audio": MessageType.AUDIO,
                "ptt": MessageType.VOICE,
                "document": MessageType.DOCUMENT,
                "sticker": MessageType.STICKER,
            }
            media.append(MediaAttachment(
                type=type_map.get(media_type, MessageType.DOCUMENT),
                file_id=wa_message.get("mediaKey"),
                mime_type=wa_message.get("mimetype"),
                file_name=wa_message.get("filename"),
                caption=wa_message.get("caption"),
            ))

        # Check for bot mention
        is_mentioned = False
        text = wa_message.get("body", "")
        mentions = wa_message.get("mentionedIds", [])
        if self._phone_number and self._phone_number in str(mentions):
            is_mentioned = True

        return Message(
            id=wa_message.get("id", {}).get("_serialized", str(wa_message.get("id"))),
            channel=self.name,
            sender_id=sender.get("id", {}).get("_serialized", str(sender.get("id", ""))),
            sender_name=sender.get("pushname") or sender.get("name"),
            chat_id=chat.get("id", {}).get("_serialized", str(chat.get("id", ""))),
            text=text,
            media=media,
            reply_to_id=wa_message.get("quotedMsgId"),
            timestamp=datetime.fromtimestamp(wa_message.get("timestamp", 0)),
            is_group=chat.get("isGroup", False),
            is_bot_mentioned=is_mentioned,
            raw=wa_message,
        )

    async def disconnect(self) -> None:
        """Disconnect from WhatsApp."""
        if self._ws:
            await self._ws.close()
            self._ws = None

        if self._session:
            # Stop WhatsApp session
            try:
                await self._session.post(
                    f"{self._base_url}/api/sessions/{self._account_id}/stop"
                )
            except Exception:
                pass
            await self._session.close()
            self._session = None

        self._authenticated = False
        self.status = ChannelStatus.DISCONNECTED
        logger.info("WhatsApp disconnected")

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a message to a WhatsApp chat."""
        if not self._session or not self._authenticated:
            return SendResult(success=False, error="Not connected")

        try:
            payload: Dict[str, Any] = {
                "chatId": chat_id,
                "text": text,
            }

            if reply_to:
                payload["quotedMessageId"] = reply_to

            # Handle media
            if media and len(media) > 0:
                first_media = media[0]
                if first_media.file_path:
                    with open(first_media.file_path, "rb") as f:
                        payload["media"] = base64.b64encode(f.read()).decode()
                        payload["mimetype"] = first_media.mime_type
                        payload["filename"] = first_media.file_name
                elif first_media.url:
                    payload["mediaUrl"] = first_media.url

            # Handle buttons (as list message)
            if buttons:
                payload["buttons"] = [
                    {"body": btn["text"], "id": btn.get("callback_data", btn["text"])}
                    for btn in buttons
                ]

            async with self._session.post(
                f"{self._base_url}/api/sessions/{self._account_id}/messages/send",
                json=payload,
            ) as resp:
                if resp.status == 429:
                    data = await resp.json()
                    raise ChannelRateLimitError(retry_after=data.get("retryAfter", 60))

                if resp.status not in (200, 201):
                    error_text = await resp.text()
                    return SendResult(success=False, error=error_text)

                data = await resp.json()
                return SendResult(
                    success=True,
                    message_id=data.get("messageId") or data.get("id"),
                    raw=data,
                )

        except ChannelRateLimitError:
            raise
        except Exception as e:
            logger.error(f"Failed to send WhatsApp message: {e}")
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Edit a WhatsApp message (limited support)."""
        # WhatsApp has limited edit support
        return SendResult(
            success=False,
            error="WhatsApp does not support message editing"
        )

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a WhatsApp message."""
        if not self._session or not self._authenticated:
            return False

        try:
            async with self._session.post(
                f"{self._base_url}/api/sessions/{self._account_id}/messages/delete",
                json={"chatId": chat_id, "messageId": message_id},
            ) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            logger.error(f"Failed to delete WhatsApp message: {e}")
            return False

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator (composing)."""
        if not self._session or not self._authenticated:
            return

        try:
            await self._session.post(
                f"{self._base_url}/api/sessions/{self._account_id}/chats/{chat_id}/composing"
            )
        except Exception:
            pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a WhatsApp chat."""
        if not self._session or not self._authenticated:
            return None

        try:
            async with self._session.get(
                f"{self._base_url}/api/sessions/{self._account_id}/chats/{chat_id}"
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.error(f"Failed to get WhatsApp chat info: {e}")

        return None

    async def send_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji: str,
    ) -> bool:
        """Send a reaction to a message."""
        if not self._session or not self._authenticated:
            return False

        try:
            async with self._session.post(
                f"{self._base_url}/api/sessions/{self._account_id}/messages/react",
                json={
                    "chatId": chat_id,
                    "messageId": message_id,
                    "emoji": emoji,
                },
            ) as resp:
                return resp.status in (200, 201)
        except Exception as e:
            logger.error(f"Failed to send WhatsApp reaction: {e}")
            return False

    async def send_read_receipt(self, chat_id: str, message_id: str) -> bool:
        """Send read receipt for a message."""
        if not self._session or not self._authenticated:
            return False

        try:
            async with self._session.post(
                f"{self._base_url}/api/sessions/{self._account_id}/messages/read",
                json={"chatId": chat_id, "messageId": message_id},
            ) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            logger.error(f"Failed to send WhatsApp read receipt: {e}")
            return False

    async def get_qr_code(self) -> Optional[str]:
        """Get current QR code for authentication."""
        if not self._session:
            return None

        try:
            async with self._session.get(
                f"{self._base_url}/api/sessions/{self._account_id}/qr"
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("qr")
        except Exception as e:
            logger.error(f"Failed to get WhatsApp QR code: {e}")

        return None

    async def download_media(self, message_id: str, destination: str) -> bool:
        """Download media from a message."""
        if not self._session or not self._authenticated:
            return False

        try:
            async with self._session.get(
                f"{self._base_url}/api/sessions/{self._account_id}/messages/{message_id}/media"
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    with open(destination, "wb") as f:
                        f.write(data)
                    return True
        except Exception as e:
            logger.error(f"Failed to download WhatsApp media: {e}")

        return False


def create_whatsapp_adapter(
    api_url: str = None,
    phone_number: str = None,
    account_id: str = "default",
    **kwargs
) -> WhatsAppAdapter:
    """
    Factory function to create WhatsApp adapter.

    Args:
        api_url: WhatsApp Web API URL (or set WHATSAPP_API_URL env var)
        phone_number: Phone number for the account
        account_id: Account identifier for multi-account support
        **kwargs: Additional config options

    Returns:
        Configured WhatsAppAdapter
    """
    api_url = api_url or os.getenv("WHATSAPP_API_URL", "http://localhost:3000")

    config = ChannelConfig(
        webhook_url=api_url,
        extra={
            "phone_number": phone_number,
            "account_id": account_id,
            **kwargs,
        },
    )
    return WhatsAppAdapter(config)
