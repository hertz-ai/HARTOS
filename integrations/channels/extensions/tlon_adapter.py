"""
Tlon (Urbit) Channel Adapter

Implements Tlon/Urbit messaging integration.
Based on HevolveBot extension patterns.

Features:
- Urbit API integration
- Graph store messaging
- Groups support
- DMs and channels
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
class TlonConfig(ChannelConfig):
    """Tlon/Urbit-specific configuration."""
    ship_url: str = ""  # e.g., http://localhost:8080
    ship_name: str = ""  # e.g., ~zod
    ship_code: str = ""  # Login code
    default_channel: str = ""
    enable_groups: bool = True
    enable_dms: bool = True
    reconnect_delay: float = 5.0
    max_reconnect_attempts: int = 10

    @classmethod
    def from_env(cls) -> "TlonConfig":
        """Create config from environment variables."""
        return cls(
            ship_url=os.getenv("URBIT_URL", "http://localhost:8080"),
            ship_name=os.getenv("URBIT_SHIP", ""),
            ship_code=os.getenv("URBIT_CODE", ""),
            default_channel=os.getenv("URBIT_CHANNEL", ""),
        )


@dataclass
class TlonGroup:
    """Tlon group information."""
    name: str
    ship: str
    title: str
    description: str = ""
    member_count: int = 0


@dataclass
class TlonChannel:
    """Tlon channel information."""
    name: str
    group: str
    title: str
    channel_type: str = "chat"  # chat, notebook, collection


class TlonAdapter(ChannelAdapter):
    """Tlon/Urbit channel adapter."""

    channel_type = "tlon"

    @property
    def name(self) -> str:
        """Get adapter name."""
        return self.channel_type

    def __init__(self, config: TlonConfig):
        super().__init__(config)
        self.config: TlonConfig = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._cookie: Optional[str] = None
        self._connected = False
        self._sse_task: Optional[asyncio.Task] = None
        self._message_handlers: List[Callable] = []
        self._event_id = 0
        self._channel_id = f"hevolvebot-{datetime.now().timestamp()}"

    async def connect(self) -> bool:
        """Connect to Urbit ship."""
        try:
            self._session = aiohttp.ClientSession()

            # Login to ship
            login_url = f"{self.config.ship_url}/~/login"
            async with self._session.post(
                login_url,
                data={"password": self.config.ship_code}
            ) as resp:
                if resp.status != 204:
                    raise ChannelConnectionError("Failed to login to Urbit ship")

                # Get session cookie
                self._cookie = resp.cookies.get("urbauth-~" + self.config.ship_name.lstrip("~"))

            # Open event channel
            await self._open_channel()

            # Start SSE listener
            self._sse_task = asyncio.create_task(self._sse_loop())

            self._connected = True
            self._status = ChannelStatus.CONNECTED
            logger.info(f"Connected to Urbit ship {self.config.ship_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Urbit: {e}")
            self._status = ChannelStatus.ERROR
            raise ChannelConnectionError(str(e))

    async def disconnect(self) -> None:
        """Disconnect from Urbit ship."""
        self._connected = False

        if self._sse_task:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass

        # Close channel
        if self._session:
            try:
                await self._session.delete(
                    f"{self.config.ship_url}/~/channel/{self._channel_id}"
                )
            except:
                pass
            await self._session.close()
            self._session = None

        self._status = ChannelStatus.DISCONNECTED
        logger.info("Disconnected from Urbit")

    async def _open_channel(self) -> None:
        """Open Urbit event channel."""
        url = f"{self.config.ship_url}/~/channel/{self._channel_id}"

        self._event_id += 1
        poke = {
            "id": self._event_id,
            "action": "poke",
            "ship": self.config.ship_name.lstrip("~"),
            "app": "hood",
            "mark": "helm-hi",
            "json": "HevolveBot connected"
        }

        async with self._session.put(url, json=[poke]) as resp:
            if resp.status != 204:
                raise ChannelConnectionError("Failed to open channel")

    async def _sse_loop(self) -> None:
        """Listen for Server-Sent Events from Urbit."""
        url = f"{self.config.ship_url}/~/channel/{self._channel_id}"

        while self._connected:
            try:
                async with self._session.get(url) as resp:
                    async for line in resp.content:
                        if not self._connected:
                            break

                        line = line.decode().strip()
                        if line.startswith("data:"):
                            data = json.loads(line[5:])
                            await self._handle_event(data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"SSE error: {e}")
                if self._connected:
                    await asyncio.sleep(self.config.reconnect_delay)

    async def _handle_event(self, data: Dict[str, Any]) -> None:
        """Handle incoming Urbit event."""
        try:
            response = data.get("response", "")

            if response == "diff":
                # Chat message
                json_data = data.get("json", {})
                if isinstance(json_data, dict) and "message" in json_data:
                    message = self._parse_message(json_data)
                    if message:
                        for handler in self._message_handlers:
                            asyncio.create_task(handler(message))

            # Acknowledge event
            self._event_id += 1
            ack = {"id": self._event_id, "action": "ack", "event-id": data.get("id", 0)}
            await self._session.put(
                f"{self.config.ship_url}/~/channel/{self._channel_id}",
                json=[ack]
            )

        except Exception as e:
            logger.error(f"Error handling event: {e}")

    def _parse_message(self, data: Dict[str, Any]) -> Optional[Message]:
        """Parse Urbit message to unified Message."""
        try:
            msg = data.get("message", {})
            author = msg.get("author", "")
            content = msg.get("contents", [])

            # Extract text from contents
            text_parts = []
            for item in content:
                if isinstance(item, dict) and "text" in item:
                    text_parts.append(item["text"])

            text = " ".join(text_parts)

            if not text:
                return None

            return Message(
                id=str(msg.get("time-sent", datetime.now().timestamp())),
                channel=self.channel_type,
                chat_id=data.get("resource", ""),
                sender_id=author,
                sender_name=author,
                text=text,
                timestamp=datetime.now(),
                message_type=MessageType.TEXT,
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
        """Send a message to Urbit channel."""
        try:
            self._event_id += 1

            # Parse chat_id as resource path
            # Format: /ship/~ship-name/group-name/channel-name

            poke = {
                "id": self._event_id,
                "action": "poke",
                "ship": self.config.ship_name.lstrip("~"),
                "app": "graph-push-hook",
                "mark": "graph-update-3",
                "json": {
                    "add-nodes": {
                        "resource": {
                            "ship": self.config.ship_name,
                            "name": chat_id
                        },
                        "nodes": {
                            f"/{int(datetime.now().timestamp() * 1000)}": {
                                "post": {
                                    "author": self.config.ship_name,
                                    "index": f"/{int(datetime.now().timestamp() * 1000)}",
                                    "time-sent": int(datetime.now().timestamp() * 1000),
                                    "contents": [{"text": text}],
                                    "hash": None,
                                    "signatures": []
                                },
                                "children": None
                            }
                        }
                    }
                }
            }

            async with self._session.put(
                f"{self.config.ship_url}/~/channel/{self._channel_id}",
                json=[poke]
            ) as resp:
                if resp.status != 204:
                    raise ChannelSendError("Failed to send message")

            return SendResult(
                success=True,
                message_id=str(self._event_id),
                timestamp=datetime.now()
            )

        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            raise ChannelSendError(str(e))

    async def edit_message(self, chat_id: str, message_id: str, text: str, **kwargs) -> bool:
        """Urbit doesn't support message editing."""
        return False

    async def delete_message(self, chat_id: str, message_id: str, **kwargs) -> bool:
        """Delete is not directly supported in graph-store."""
        return False

    async def send_typing(self, chat_id: str, **kwargs) -> None:
        """Urbit doesn't have typing indicators."""
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get channel information."""
        return {
            "id": chat_id,
            "type": "channel",
            "ship": self.config.ship_name,
        }


def create_tlon_adapter(
    ship_url: Optional[str] = None,
    ship_name: Optional[str] = None,
    ship_code: Optional[str] = None,
    **kwargs
) -> TlonAdapter:
    """Factory function to create a Tlon adapter."""
    config = TlonConfig(
        ship_url=ship_url or os.getenv("URBIT_URL", "http://localhost:8080"),
        ship_name=ship_name or os.getenv("URBIT_SHIP", ""),
        ship_code=ship_code or os.getenv("URBIT_CODE", ""),
        **kwargs
    )
    return TlonAdapter(config)
