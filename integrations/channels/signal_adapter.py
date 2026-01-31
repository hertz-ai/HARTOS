"""
Signal Channel Adapter

Implements Signal messaging using signal-cli REST API.
Designed for Docker-compatible deployments.

Features:
- signal-cli REST API integration
- Linked device support
- Group V2 support
- Attachments, reactions, typing indicators

Requirements:
- signal-cli-rest-api running (https://github.com/bbernhard/signal-cli-rest-api)
- Linked Signal account

Docker setup:
    docker run -d --name signal-api -p 8080:8080 \
        -v /path/to/signal-cli-config:/home/.local/share/signal-cli \
        -e MODE=native \
        bbernhard/signal-cli-rest-api
"""

from __future__ import annotations

import asyncio
import logging
import os
import base64
import mimetypes
from typing import Optional, List, Dict, Any
from datetime import datetime
from pathlib import Path

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


class SignalAdapter(ChannelAdapter):
    """
    Signal messaging adapter using signal-cli REST API.

    Usage:
        config = ChannelConfig(
            token="+1234567890",  # Your Signal phone number
            extra={
                "api_url": "http://localhost:8080",
            }
        )
        adapter = SignalAdapter(config)
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
        self._phone_number = config.token  # Phone number as token
        self._api_url = config.extra.get("api_url", "http://localhost:8080")
        self._session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._reconnect_delay = 5  # seconds
        self._max_reconnect_delay = 300  # 5 minutes max
        self._running = False

    @property
    def name(self) -> str:
        return "signal"

    async def connect(self) -> bool:
        """Connect to signal-cli REST API."""
        if not self._phone_number:
            logger.error("Signal phone number not provided")
            return False

        try:
            self._session = aiohttp.ClientSession()

            # Verify API is available and account is linked
            async with self._session.get(
                f"{self._api_url}/v1/about"
            ) as response:
                if response.status != 200:
                    logger.error("Signal API not available")
                    return False

            # Verify phone number is registered
            async with self._session.get(
                f"{self._api_url}/v1/accounts"
            ) as response:
                if response.status == 200:
                    accounts = await response.json()
                    if self._phone_number not in [acc.get("number") for acc in accounts]:
                        logger.warning(f"Phone number {self._phone_number} not found in registered accounts")
                        # Continue anyway - might be registered differently

            # Start polling for messages
            self._running = True
            self._poll_task = asyncio.create_task(self._poll_messages())

            self.status = ChannelStatus.CONNECTED
            logger.info(f"Connected to Signal as {self._phone_number}")
            return True

        except aiohttp.ClientError as e:
            logger.error(f"Failed to connect to Signal API: {e}")
            self.status = ChannelStatus.ERROR
            return False
        except Exception as e:
            logger.error(f"Signal connection error: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect from Signal API."""
        self._running = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        if self._session:
            await self._session.close()
            self._session = None

        self.status = ChannelStatus.DISCONNECTED
        logger.info("Disconnected from Signal")

    async def _poll_messages(self) -> None:
        """Poll for new messages from Signal."""
        reconnect_delay = self._reconnect_delay

        while self._running:
            try:
                async with self._session.get(
                    f"{self._api_url}/v1/receive/{self._phone_number}",
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    if response.status == 200:
                        messages = await response.json()
                        for msg_data in messages:
                            message = self._convert_message(msg_data)
                            if message:
                                await self._dispatch_message(message)

                        # Reset reconnect delay on success
                        reconnect_delay = self._reconnect_delay
                    elif response.status == 204:
                        # No new messages
                        pass
                    else:
                        logger.warning(f"Signal API returned {response.status}")

                # Small delay between polls
                await asyncio.sleep(1)

            except asyncio.CancelledError:
                break
            except aiohttp.ClientError as e:
                logger.error(f"Signal polling error: {e}")
                self.status = ChannelStatus.ERROR

                # Exponential backoff for reconnection
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, self._max_reconnect_delay)

                # Try to reconnect
                await self._reconnect()
            except Exception as e:
                logger.error(f"Unexpected error in Signal polling: {e}")
                await asyncio.sleep(reconnect_delay)

    async def _reconnect(self) -> None:
        """Attempt to reconnect to Signal API."""
        logger.info("Attempting to reconnect to Signal API...")

        if self._session:
            await self._session.close()

        self._session = aiohttp.ClientSession()

        try:
            async with self._session.get(f"{self._api_url}/v1/about") as response:
                if response.status == 200:
                    self.status = ChannelStatus.CONNECTED
                    logger.info("Reconnected to Signal API")
        except Exception as e:
            logger.error(f"Reconnection failed: {e}")

    def _convert_message(self, msg_data: Dict[str, Any]) -> Optional[Message]:
        """Convert Signal message to unified Message format."""
        envelope = msg_data.get("envelope", {})

        # Skip non-data messages
        data_message = envelope.get("dataMessage")
        if not data_message:
            return None

        source = envelope.get("source", "")
        source_name = envelope.get("sourceName", source)
        timestamp = envelope.get("timestamp", 0)

        # Determine chat ID (group or direct)
        group_info = data_message.get("groupInfo", {})
        is_group = bool(group_info)

        if is_group:
            chat_id = group_info.get("groupId", "")
        else:
            chat_id = source

        # Process attachments
        media = []
        attachments = data_message.get("attachments", [])
        for att in attachments:
            media_type = self._get_media_type(att.get("contentType", ""))
            media.append(MediaAttachment(
                type=media_type,
                file_id=att.get("id"),
                file_name=att.get("filename"),
                mime_type=att.get("contentType"),
                file_size=att.get("size"),
            ))

        # Check for mentions
        mentions = data_message.get("mentions", [])
        is_mentioned = any(
            m.get("number") == self._phone_number
            for m in mentions
        )

        return Message(
            id=str(timestamp),
            channel=self.name,
            sender_id=source,
            sender_name=source_name,
            chat_id=chat_id,
            text=data_message.get("message", ""),
            media=media,
            reply_to_id=str(data_message.get("quote", {}).get("id")) if data_message.get("quote") else None,
            timestamp=datetime.fromtimestamp(timestamp / 1000) if timestamp else datetime.now(),
            is_group=is_group,
            is_bot_mentioned=is_mentioned,
            raw=msg_data,
        )

    def _get_media_type(self, content_type: str) -> MessageType:
        """Get MessageType from MIME content type."""
        if content_type.startswith("image/"):
            return MessageType.IMAGE
        elif content_type.startswith("video/"):
            return MessageType.VIDEO
        elif content_type.startswith("audio/"):
            return MessageType.AUDIO
        elif content_type.startswith("application/"):
            return MessageType.DOCUMENT
        else:
            return MessageType.DOCUMENT

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a message via Signal."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            # Determine if group or direct message
            is_group = chat_id.startswith("group.")

            payload = {
                "message": text,
                "number": self._phone_number,
            }

            if is_group:
                payload["recipients"] = []
                # Group ID format: group.BASE64_ID
                group_id = chat_id.replace("group.", "")
                endpoint = f"{self._api_url}/v2/send"
                payload["group_id"] = group_id
            else:
                payload["recipients"] = [chat_id]
                endpoint = f"{self._api_url}/v2/send"

            # Handle quote/reply
            if reply_to:
                payload["quote_timestamp"] = int(reply_to)

            # Handle attachments
            if media:
                attachments = []
                for m in media:
                    att_data = await self._prepare_attachment(m)
                    if att_data:
                        attachments.append(att_data)
                if attachments:
                    payload["base64_attachments"] = attachments

            async with self._session.post(
                endpoint,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status in (200, 201):
                    result = await response.json()
                    return SendResult(
                        success=True,
                        message_id=str(result.get("timestamp", "")),
                        raw=result,
                    )
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to send Signal message: {error_text}")
                    return SendResult(success=False, error=error_text)

        except aiohttp.ClientError as e:
            logger.error(f"Signal send error: {e}")
            return SendResult(success=False, error=str(e))
        except Exception as e:
            logger.error(f"Unexpected error sending Signal message: {e}")
            return SendResult(success=False, error=str(e))

    async def _prepare_attachment(self, attachment: MediaAttachment) -> Optional[str]:
        """Prepare attachment for sending (base64 encode)."""
        try:
            if attachment.file_path:
                path = Path(attachment.file_path)
                if path.exists():
                    content = path.read_bytes()
                    mime_type = attachment.mime_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
                    return f"data:{mime_type};base64,{base64.b64encode(content).decode()}"
            elif attachment.url:
                # Download from URL
                async with self._session.get(attachment.url) as response:
                    if response.status == 200:
                        content = await response.read()
                        mime_type = attachment.mime_type or response.content_type or "application/octet-stream"
                        return f"data:{mime_type};base64,{base64.b64encode(content).decode()}"
        except Exception as e:
            logger.error(f"Failed to prepare attachment: {e}")

        return None

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Edit an existing message (Signal doesn't support this natively)."""
        # Signal doesn't support message editing
        # Send a new message with indication it's an edit
        return await self.send_message(
            chat_id=chat_id,
            text=f"[Edit] {text}",
        )

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a message (Signal remote delete)."""
        if not self._session:
            return False

        try:
            payload = {
                "number": self._phone_number,
                "recipients": [chat_id] if not chat_id.startswith("group.") else [],
                "target_timestamp": int(message_id),
            }

            if chat_id.startswith("group."):
                payload["group_id"] = chat_id.replace("group.", "")

            async with self._session.post(
                f"{self._api_url}/v1/delete",
                json=payload,
            ) as response:
                return response.status in (200, 201, 204)

        except Exception as e:
            logger.error(f"Failed to delete Signal message: {e}")
            return False

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        if not self._session:
            return

        try:
            payload = {
                "number": self._phone_number,
            }

            if chat_id.startswith("group."):
                payload["group_id"] = chat_id.replace("group.", "")
            else:
                payload["recipient"] = chat_id

            await self._session.put(
                f"{self._api_url}/v1/typing-indicator/{self._phone_number}",
                json=payload,
            )
        except Exception as e:
            logger.debug(f"Failed to send typing indicator: {e}")

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a chat."""
        if not self._session:
            return None

        try:
            if chat_id.startswith("group."):
                group_id = chat_id.replace("group.", "")
                async with self._session.get(
                    f"{self._api_url}/v1/groups/{self._phone_number}/{group_id}"
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        return {
                            "id": chat_id,
                            "type": "group",
                            "name": data.get("name"),
                            "members": data.get("members", []),
                        }
            else:
                # Direct chat - return phone info
                async with self._session.get(
                    f"{self._api_url}/v1/identities/{self._phone_number}/{chat_id}"
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        return {
                            "id": chat_id,
                            "type": "direct",
                            "trust_level": data.get("trust_level"),
                        }

        except Exception as e:
            logger.error(f"Failed to get chat info: {e}")

        return None

    async def send_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji: str,
        remove: bool = False,
    ) -> bool:
        """Send a reaction to a message."""
        if not self._session:
            return False

        try:
            payload = {
                "number": self._phone_number,
                "reaction": {
                    "emoji": emoji,
                    "target_author": chat_id if not chat_id.startswith("group.") else "",
                    "target_timestamp": int(message_id),
                    "remove": remove,
                },
            }

            if chat_id.startswith("group."):
                payload["group_id"] = chat_id.replace("group.", "")
            else:
                payload["recipients"] = [chat_id]

            async with self._session.post(
                f"{self._api_url}/v2/send",
                json=payload,
            ) as response:
                return response.status in (200, 201)

        except Exception as e:
            logger.error(f"Failed to send reaction: {e}")
            return False

    async def create_group(
        self,
        name: str,
        members: List[str],
        avatar_path: Optional[str] = None,
    ) -> Optional[str]:
        """Create a new Signal group (Group V2)."""
        if not self._session:
            return None

        try:
            payload = {
                "name": name,
                "members": members,
            }

            if avatar_path and Path(avatar_path).exists():
                content = Path(avatar_path).read_bytes()
                payload["avatar"] = base64.b64encode(content).decode()

            async with self._session.post(
                f"{self._api_url}/v1/groups/{self._phone_number}",
                json=payload,
            ) as response:
                if response.status in (200, 201):
                    data = await response.json()
                    return f"group.{data.get('id', '')}"

        except Exception as e:
            logger.error(f"Failed to create group: {e}")

        return None

    async def download_attachment(
        self,
        attachment_id: str,
        destination: str,
    ) -> bool:
        """Download an attachment from Signal."""
        if not self._session:
            return False

        try:
            async with self._session.get(
                f"{self._api_url}/v1/attachments/{attachment_id}"
            ) as response:
                if response.status == 200:
                    content = await response.read()
                    Path(destination).write_bytes(content)
                    return True

        except Exception as e:
            logger.error(f"Failed to download attachment: {e}")

        return False


def create_signal_adapter(
    phone_number: str = None,
    api_url: str = None,
    **kwargs
) -> SignalAdapter:
    """
    Factory function to create Signal adapter.

    Args:
        phone_number: Signal phone number (or set SIGNAL_PHONE_NUMBER env var)
        api_url: signal-cli REST API URL (default: http://localhost:8080)
        **kwargs: Additional config options

    Returns:
        Configured SignalAdapter
    """
    phone_number = phone_number or os.getenv("SIGNAL_PHONE_NUMBER")
    if not phone_number:
        raise ValueError("Signal phone number required")

    api_url = api_url or os.getenv("SIGNAL_API_URL", "http://localhost:8080")

    config = ChannelConfig(
        token=phone_number,
        extra={"api_url": api_url, **kwargs.get("extra", {})},
        **{k: v for k, v in kwargs.items() if k != "extra"},
    )
    return SignalAdapter(config)
