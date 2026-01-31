"""
Open Prose Channel Adapter

Implements Open Prose messaging integration.
Based on HevolveBot extension patterns.

Open Prose is an open-source prose/document collaboration platform.

Features:
- Document collaboration
- Real-time editing
- Comments and discussions
- Docker-compatible
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import aiohttp
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urljoin

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
class OpenProseConfig(ChannelConfig):
    """Open Prose-specific configuration."""
    server_url: str = ""
    api_key: str = ""
    workspace_id: str = ""
    enable_comments: bool = True
    enable_suggestions: bool = True
    poll_interval: float = 2.0
    reconnect_delay: float = 5.0

    @classmethod
    def from_env(cls) -> "OpenProseConfig":
        """Create config from environment variables."""
        return cls(
            server_url=os.getenv("OPENPROSE_URL", ""),
            api_key=os.getenv("OPENPROSE_API_KEY", ""),
            workspace_id=os.getenv("OPENPROSE_WORKSPACE", ""),
        )


@dataclass
class Document:
    """Open Prose document."""
    id: str
    title: str
    content: str
    author_id: str
    created_at: datetime
    updated_at: datetime
    collaborators: List[str] = field(default_factory=list)


@dataclass
class Comment:
    """Document comment/discussion."""
    id: str
    document_id: str
    author_id: str
    author_name: str
    content: str
    thread_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)


class OpenProseAdapter(ChannelAdapter):
    """Open Prose channel adapter."""

    channel_type = "openprose"

    @property
    def name(self) -> str:
        """Get adapter name."""
        return self.channel_type

    def __init__(self, config: OpenProseConfig):
        super().__init__(config)
        self.config: OpenProseConfig = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._connected = False
        self._poll_task: Optional[asyncio.Task] = None
        self._message_handlers: List[Callable] = []
        self._last_check: Dict[str, datetime] = {}

    @property
    def base_url(self) -> str:
        """Get base API URL."""
        return urljoin(self.config.server_url, "/api/v1/")

    def _get_headers(self) -> Dict[str, str]:
        """Get headers for API requests."""
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    async def connect(self) -> bool:
        """Connect to Open Prose server."""
        try:
            self._session = aiohttp.ClientSession()

            # Verify connection
            async with self._session.get(
                urljoin(self.base_url, "health"),
                headers=self._get_headers()
            ) as resp:
                if resp.status != 200:
                    raise ChannelConnectionError("Failed to connect to Open Prose")

            # Start polling for comments
            self._poll_task = asyncio.create_task(self._poll_loop())

            self._connected = True
            self._status = ChannelStatus.CONNECTED
            logger.info("Connected to Open Prose")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Open Prose: {e}")
            self._status = ChannelStatus.ERROR
            raise ChannelConnectionError(str(e))

    async def disconnect(self) -> None:
        """Disconnect from Open Prose."""
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
        logger.info("Disconnected from Open Prose")

    async def _poll_loop(self) -> None:
        """Poll for new comments/discussions."""
        while self._connected:
            try:
                await self._check_comments()
                await asyncio.sleep(self.config.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Poll error: {e}")
                await asyncio.sleep(self.config.reconnect_delay)

    async def _check_comments(self) -> None:
        """Check for new comments."""
        if not self.config.workspace_id:
            return

        url = urljoin(self.base_url, f"workspaces/{self.config.workspace_id}/comments")

        try:
            async with self._session.get(url, headers=self._get_headers()) as resp:
                if resp.status != 200:
                    return

                data = await resp.json()

                for comment_data in data.get("comments", []):
                    comment_id = comment_data.get("id", "")
                    created_at = datetime.fromisoformat(
                        comment_data.get("created_at", datetime.now().isoformat())
                    )

                    # Check if new
                    doc_id = comment_data.get("document_id", "")
                    last_check = self._last_check.get(doc_id, datetime.min)

                    if created_at > last_check:
                        message = self._parse_comment(comment_data)
                        if message:
                            for handler in self._message_handlers:
                                asyncio.create_task(handler(message))

                    self._last_check[doc_id] = datetime.now()

        except Exception as e:
            logger.error(f"Error checking comments: {e}")

    def _parse_comment(self, data: Dict[str, Any]) -> Optional[Message]:
        """Parse comment to unified Message."""
        try:
            return Message(
                id=data.get("id", ""),
                channel=self.channel_type,
                chat_id=data.get("document_id", ""),
                sender_id=data.get("author_id", ""),
                sender_name=data.get("author_name", ""),
                text=data.get("content", ""),
                timestamp=datetime.fromisoformat(
                    data.get("created_at", datetime.now().isoformat())
                ),
                message_type=MessageType.TEXT,
                reply_to=data.get("thread_id"),
                metadata={
                    "document_title": data.get("document_title", ""),
                    "selection": data.get("selection"),
                }
            )
        except Exception as e:
            logger.error(f"Error parsing comment: {e}")
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
        """Add a comment to a document."""
        url = urljoin(self.base_url, f"documents/{chat_id}/comments")

        payload = {
            "content": text,
            "thread_id": reply_to,
        }

        # Add selection if provided
        if "selection" in kwargs:
            payload["selection"] = kwargs["selection"]

        try:
            async with self._session.post(
                url,
                json=payload,
                headers=self._get_headers()
            ) as resp:
                if resp.status not in (200, 201):
                    raise ChannelSendError("Failed to add comment")

                data = await resp.json()
                return SendResult(
                    success=True,
                    message_id=data.get("id", ""),
                    timestamp=datetime.now()
                )

        except Exception as e:
            logger.error(f"Failed to send comment: {e}")
            raise ChannelSendError(str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        **kwargs
    ) -> bool:
        """Edit a comment."""
        url = urljoin(self.base_url, f"comments/{message_id}")

        try:
            async with self._session.patch(
                url,
                json={"content": text},
                headers=self._get_headers()
            ) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"Failed to edit comment: {e}")
            return False

    async def delete_message(self, chat_id: str, message_id: str, **kwargs) -> bool:
        """Delete a comment."""
        url = urljoin(self.base_url, f"comments/{message_id}")

        try:
            async with self._session.delete(url, headers=self._get_headers()) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            logger.error(f"Failed to delete comment: {e}")
            return False

    async def send_typing(self, chat_id: str, **kwargs) -> None:
        """Show typing/composing indicator."""
        # Could implement presence API if available
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get document information."""
        url = urljoin(self.base_url, f"documents/{chat_id}")

        try:
            async with self._session.get(url, headers=self._get_headers()) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()
                return {
                    "id": data.get("id", ""),
                    "title": data.get("title", ""),
                    "author": data.get("author_name", ""),
                    "collaborators": data.get("collaborators", []),
                }
        except Exception as e:
            logger.error(f"Failed to get document info: {e}")
            return None

    async def add_suggestion(
        self,
        document_id: str,
        selection: Dict[str, Any],
        suggested_text: str,
        reason: Optional[str] = None
    ) -> Optional[str]:
        """Add a text suggestion to a document."""
        if not self.config.enable_suggestions:
            return None

        url = urljoin(self.base_url, f"documents/{document_id}/suggestions")

        payload = {
            "selection": selection,
            "suggested_text": suggested_text,
            "reason": reason,
        }

        try:
            async with self._session.post(
                url,
                json=payload,
                headers=self._get_headers()
            ) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    return data.get("id")
                return None
        except Exception as e:
            logger.error(f"Failed to add suggestion: {e}")
            return None


def create_openprose_adapter(
    server_url: Optional[str] = None,
    api_key: Optional[str] = None,
    workspace_id: Optional[str] = None,
    **kwargs
) -> OpenProseAdapter:
    """Factory function to create an Open Prose adapter."""
    config = OpenProseConfig(
        server_url=server_url or os.getenv("OPENPROSE_URL", ""),
        api_key=api_key or os.getenv("OPENPROSE_API_KEY", ""),
        workspace_id=workspace_id or os.getenv("OPENPROSE_WORKSPACE", ""),
        **kwargs
    )
    return OpenProseAdapter(config)
