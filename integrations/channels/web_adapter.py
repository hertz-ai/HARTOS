"""
Web/Browser Channel Adapter

Implements web-based messaging with WebSocket and REST API support.
Designed for Docker-compatible deployments with browser clients.

Features:
- WebSocket real-time communication
- REST API fallback for polling
- Session management
- File upload/download
- Typing indicators
- Read receipts
- Multi-tab support

This adapter creates a WebSocket server that browser clients can connect to.
It also provides REST endpoints for clients that don't support WebSockets.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
import json
import time
import base64
import mimetypes
from typing import Optional, List, Dict, Any, Set
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field

try:
    import aiohttp
    from aiohttp import web, WSMsgType
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


@dataclass
class WebSession:
    """Represents a connected web client session."""
    session_id: str
    user_id: str
    user_name: Optional[str] = None
    connected_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    websockets: Set[web.WebSocketResponse] = field(default_factory=set)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_connected(self) -> bool:
        return len(self.websockets) > 0

    def touch(self) -> None:
        """Update last activity timestamp."""
        self.last_activity = datetime.now()


@dataclass
class PendingMessage:
    """Message waiting to be delivered to a disconnected client."""
    id: str
    session_id: str
    data: Dict[str, Any]
    created_at: datetime = field(default_factory=datetime.now)
    expires_at: datetime = field(default_factory=lambda: datetime.now() + timedelta(hours=24))


class WebAdapter(ChannelAdapter):
    """
    Web/Browser channel adapter with WebSocket and REST API.

    Usage:
        config = ChannelConfig(
            extra={
                "host": "0.0.0.0",
                "port": 8765,
                "upload_dir": "/tmp/uploads",
                "cors_origins": ["*"],
            }
        )
        adapter = WebAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()

    Browser client example:
        const ws = new WebSocket('ws://localhost:8765/ws?session_id=xxx&user_id=yyy');
        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            console.log('Received:', data);
        };
        ws.send(JSON.stringify({type: 'message', text: 'Hello!'}));
    """

    def __init__(self, config: ChannelConfig):
        if not HAS_AIOHTTP:
            raise ImportError(
                "aiohttp not installed. "
                "Install with: pip install aiohttp"
            )

        super().__init__(config)
        self._host = config.extra.get("host", "0.0.0.0")
        self._port = config.extra.get("port", 8765)
        self._upload_dir = Path(config.extra.get("upload_dir", "/tmp/web_adapter_uploads"))
        self._cors_origins = config.extra.get("cors_origins", ["*"])
        self._session_timeout = config.extra.get("session_timeout", 3600)  # 1 hour

        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        self._sessions: Dict[str, WebSession] = {}
        self._pending_messages: Dict[str, List[PendingMessage]] = {}
        self._read_receipts: Dict[str, Set[str]] = {}  # message_id -> set of session_ids
        self._typing_status: Dict[str, datetime] = {}  # session_id -> typing_until

        self._cleanup_task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        return "web"

    async def connect(self) -> bool:
        """Start the WebSocket server."""
        try:
            # Create upload directory
            self._upload_dir.mkdir(parents=True, exist_ok=True)

            # Create aiohttp application
            self._app = web.Application(middlewares=[self._cors_middleware])
            self._setup_routes()

            # Start server
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()

            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()

            # Start cleanup task
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

            self.status = ChannelStatus.CONNECTED
            logger.info(f"Web adapter started on ws://{self._host}:{self._port}")
            return True

        except Exception as e:
            logger.error(f"Failed to start web adapter: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Stop the WebSocket server."""
        # Cancel cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Close all WebSocket connections
        for session in self._sessions.values():
            for ws in list(session.websockets):
                await ws.close()

        # Stop server
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()

        self._app = None
        self._runner = None
        self._site = None
        self.status = ChannelStatus.DISCONNECTED
        logger.info("Web adapter stopped")

    async def _cors_middleware(self, request, handler):
        """CORS middleware for REST endpoints."""
        if request.method == "OPTIONS":
            return web.Response(
                headers={
                    "Access-Control-Allow-Origin": ", ".join(self._cors_origins),
                    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Session-ID",
                    "Access-Control-Max-Age": "86400",
                }
            )

        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = ", ".join(self._cors_origins)
        return response

    def _setup_routes(self) -> None:
        """Set up HTTP/WebSocket routes."""
        self._app.router.add_get("/ws", self._handle_websocket)
        self._app.router.add_get("/health", self._handle_health)

        # REST API endpoints
        self._app.router.add_post("/api/messages", self._handle_rest_message)
        self._app.router.add_get("/api/messages", self._handle_get_messages)
        self._app.router.add_post("/api/upload", self._handle_upload)
        self._app.router.add_get("/api/download/{file_id}", self._handle_download)
        self._app.router.add_post("/api/typing", self._handle_typing)
        self._app.router.add_post("/api/read", self._handle_read_receipt)
        self._app.router.add_get("/api/session", self._handle_session_info)

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({
            "status": "ok",
            "channel": self.name,
            "connections": sum(len(s.websockets) for s in self._sessions.values()),
            "sessions": len(self._sessions),
        })

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """Handle WebSocket connections."""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        # Get session info from query params
        session_id = request.query.get("session_id") or str(uuid.uuid4())
        user_id = request.query.get("user_id", session_id)
        user_name = request.query.get("user_name")

        # Get or create session
        session = self._sessions.get(session_id)
        if not session:
            session = WebSession(
                session_id=session_id,
                user_id=user_id,
                user_name=user_name,
            )
            self._sessions[session_id] = session
        else:
            session.touch()

        session.websockets.add(ws)

        # Send connection confirmation
        await ws.send_json({
            "type": "connected",
            "session_id": session_id,
            "user_id": user_id,
        })

        # Send pending messages
        pending = self._pending_messages.get(session_id, [])
        for pm in pending:
            await ws.send_json(pm.data)
        if session_id in self._pending_messages:
            del self._pending_messages[session_id]

        logger.info(f"WebSocket connected: session={session_id}, user={user_id}")

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_ws_message(session, msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await self._handle_ws_binary(session, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
                    break
        finally:
            session.websockets.discard(ws)
            if not session.websockets:
                logger.info(f"All connections closed for session {session_id}")
            else:
                logger.info(f"WebSocket disconnected: session={session_id} ({len(session.websockets)} remaining)")

        return ws

    async def _handle_ws_message(self, session: WebSession, data: str) -> None:
        """Handle incoming WebSocket message."""
        try:
            payload = json.loads(data)
            msg_type = payload.get("type", "message")

            session.touch()

            if msg_type == "message":
                # Convert to Message and dispatch
                message = Message(
                    id=payload.get("id") or str(uuid.uuid4()),
                    channel=self.name,
                    sender_id=session.user_id,
                    sender_name=session.user_name,
                    chat_id=payload.get("chat_id", session.session_id),
                    text=payload.get("text", ""),
                    reply_to_id=payload.get("reply_to"),
                    timestamp=datetime.now(),
                    is_group=payload.get("is_group", False),
                    raw=payload,
                )

                # Add media if present
                if payload.get("attachments"):
                    for att in payload["attachments"]:
                        message.media.append(MediaAttachment(
                            type=MessageType(att.get("type", "document")),
                            file_id=att.get("file_id"),
                            file_name=att.get("file_name"),
                            mime_type=att.get("mime_type"),
                            url=att.get("url"),
                        ))

                await self._dispatch_message(message)

            elif msg_type == "typing":
                # Handle typing indicator
                self._typing_status[session.session_id] = datetime.now() + timedelta(seconds=5)
                await self._broadcast_typing(session.session_id, session.user_name or session.user_id)

            elif msg_type == "read":
                # Handle read receipt
                message_ids = payload.get("message_ids", [])
                for msg_id in message_ids:
                    if msg_id not in self._read_receipts:
                        self._read_receipts[msg_id] = set()
                    self._read_receipts[msg_id].add(session.session_id)

            elif msg_type == "ping":
                # Respond to ping
                await self._send_to_session(session.session_id, {"type": "pong"})

        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from session {session.session_id}")
        except Exception as e:
            logger.error(f"Error handling WebSocket message: {e}")

    async def _handle_ws_binary(self, session: WebSession, data: bytes) -> None:
        """Handle incoming binary data (file upload via WebSocket)."""
        try:
            # First 4 bytes = metadata length
            meta_len = int.from_bytes(data[:4], "big")
            metadata = json.loads(data[4:4+meta_len].decode())
            file_data = data[4+meta_len:]

            # Save file
            file_id = str(uuid.uuid4())
            file_name = metadata.get("file_name", "upload")
            file_path = self._upload_dir / f"{file_id}_{file_name}"
            file_path.write_bytes(file_data)

            # Send confirmation
            await self._send_to_session(session.session_id, {
                "type": "upload_complete",
                "file_id": file_id,
                "file_name": file_name,
                "size": len(file_data),
            })

        except Exception as e:
            logger.error(f"Error handling binary upload: {e}")
            await self._send_to_session(session.session_id, {
                "type": "upload_error",
                "error": str(e),
            })

    async def _handle_rest_message(self, request: web.Request) -> web.Response:
        """Handle REST API message submission."""
        try:
            session_id = request.headers.get("X-Session-ID")
            if not session_id:
                return web.json_response({"error": "X-Session-ID header required"}, status=400)

            data = await request.json()

            # Get or create session
            session = self._sessions.get(session_id)
            if not session:
                session = WebSession(
                    session_id=session_id,
                    user_id=data.get("user_id", session_id),
                    user_name=data.get("user_name"),
                )
                self._sessions[session_id] = session

            # Create message
            message = Message(
                id=data.get("id") or str(uuid.uuid4()),
                channel=self.name,
                sender_id=session.user_id,
                sender_name=session.user_name,
                chat_id=data.get("chat_id", session_id),
                text=data.get("text", ""),
                reply_to_id=data.get("reply_to"),
                timestamp=datetime.now(),
                is_group=data.get("is_group", False),
                raw=data,
            )

            await self._dispatch_message(message)

            return web.json_response({
                "success": True,
                "message_id": message.id,
            })

        except Exception as e:
            logger.error(f"REST message error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_get_messages(self, request: web.Request) -> web.Response:
        """Handle polling for messages (REST fallback)."""
        session_id = request.headers.get("X-Session-ID")
        if not session_id:
            return web.json_response({"error": "X-Session-ID header required"}, status=400)

        # Get pending messages
        pending = self._pending_messages.get(session_id, [])
        messages = [pm.data for pm in pending]

        if session_id in self._pending_messages:
            del self._pending_messages[session_id]

        return web.json_response({"messages": messages})

    async def _handle_upload(self, request: web.Request) -> web.Response:
        """Handle file upload via REST."""
        try:
            reader = await request.multipart()

            files = []
            async for part in reader:
                if part.filename:
                    file_id = str(uuid.uuid4())
                    file_name = part.filename
                    file_path = self._upload_dir / f"{file_id}_{file_name}"

                    # Save file
                    with open(file_path, "wb") as f:
                        while True:
                            chunk = await part.read_chunk()
                            if not chunk:
                                break
                            f.write(chunk)

                    files.append({
                        "file_id": file_id,
                        "file_name": file_name,
                        "size": file_path.stat().st_size,
                        "mime_type": part.headers.get("Content-Type"),
                    })

            return web.json_response({"files": files})

        except Exception as e:
            logger.error(f"Upload error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_download(self, request: web.Request) -> web.Response:
        """Handle file download."""
        file_id = request.match_info["file_id"]

        # Find file
        for file_path in self._upload_dir.glob(f"{file_id}_*"):
            if file_path.is_file():
                file_name = file_path.name[len(file_id)+1:]
                mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

                return web.FileResponse(
                    file_path,
                    headers={
                        "Content-Disposition": f'attachment; filename="{file_name}"',
                        "Content-Type": mime_type,
                    }
                )

        return web.json_response({"error": "File not found"}, status=404)

    async def _handle_typing(self, request: web.Request) -> web.Response:
        """Handle typing indicator via REST."""
        session_id = request.headers.get("X-Session-ID")
        if not session_id:
            return web.json_response({"error": "X-Session-ID header required"}, status=400)

        session = self._sessions.get(session_id)
        if session:
            self._typing_status[session_id] = datetime.now() + timedelta(seconds=5)
            await self._broadcast_typing(session_id, session.user_name or session.user_id)

        return web.json_response({"success": True})

    async def _handle_read_receipt(self, request: web.Request) -> web.Response:
        """Handle read receipt via REST."""
        session_id = request.headers.get("X-Session-ID")
        if not session_id:
            return web.json_response({"error": "X-Session-ID header required"}, status=400)

        data = await request.json()
        message_ids = data.get("message_ids", [])

        for msg_id in message_ids:
            if msg_id not in self._read_receipts:
                self._read_receipts[msg_id] = set()
            self._read_receipts[msg_id].add(session_id)

        return web.json_response({"success": True})

    async def _handle_session_info(self, request: web.Request) -> web.Response:
        """Get session information."""
        session_id = request.headers.get("X-Session-ID")
        if not session_id:
            return web.json_response({"error": "X-Session-ID header required"}, status=400)

        session = self._sessions.get(session_id)
        if not session:
            return web.json_response({"error": "Session not found"}, status=404)

        return web.json_response({
            "session_id": session.session_id,
            "user_id": session.user_id,
            "user_name": session.user_name,
            "connected_at": session.connected_at.isoformat(),
            "last_activity": session.last_activity.isoformat(),
            "is_connected": session.is_connected,
            "connection_count": len(session.websockets),
        })

    async def _broadcast_typing(self, from_session: str, from_name: str) -> None:
        """Broadcast typing indicator to other sessions."""
        data = {
            "type": "typing",
            "from_session": from_session,
            "from_name": from_name,
        }

        for session_id, session in self._sessions.items():
            if session_id != from_session:
                await self._send_to_session(session_id, data)

    async def _send_to_session(
        self,
        session_id: str,
        data: Dict[str, Any],
        queue_if_offline: bool = True,
    ) -> bool:
        """Send data to a session."""
        session = self._sessions.get(session_id)

        if session and session.websockets:
            # Send to all connected WebSockets
            for ws in list(session.websockets):
                try:
                    await ws.send_json(data)
                except Exception:
                    session.websockets.discard(ws)

            return True

        elif queue_if_offline:
            # Queue for later delivery
            if session_id not in self._pending_messages:
                self._pending_messages[session_id] = []

            self._pending_messages[session_id].append(PendingMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                data=data,
            ))

            return False

        return False

    async def _cleanup_loop(self) -> None:
        """Periodically clean up expired sessions and messages."""
        while True:
            try:
                await asyncio.sleep(60)  # Run every minute

                now = datetime.now()
                timeout = timedelta(seconds=self._session_timeout)

                # Clean up inactive sessions
                expired_sessions = [
                    session_id
                    for session_id, session in self._sessions.items()
                    if not session.is_connected and (now - session.last_activity) > timeout
                ]

                for session_id in expired_sessions:
                    del self._sessions[session_id]
                    if session_id in self._pending_messages:
                        del self._pending_messages[session_id]
                    logger.debug(f"Cleaned up expired session: {session_id}")

                # Clean up expired pending messages
                for session_id, messages in list(self._pending_messages.items()):
                    self._pending_messages[session_id] = [
                        pm for pm in messages if pm.expires_at > now
                    ]
                    if not self._pending_messages[session_id]:
                        del self._pending_messages[session_id]

                # Clean up old read receipts
                if len(self._read_receipts) > 10000:
                    # Keep only most recent 5000
                    self._read_receipts = dict(list(self._read_receipts.items())[-5000:])

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a message to a web client."""
        message_id = str(uuid.uuid4())

        data = {
            "type": "message",
            "id": message_id,
            "text": text,
            "reply_to": reply_to,
            "timestamp": datetime.now().isoformat(),
        }

        if media:
            data["attachments"] = [
                {
                    "type": m.type.value,
                    "file_id": m.file_id,
                    "file_name": m.file_name,
                    "mime_type": m.mime_type,
                    "url": m.url or f"/api/download/{m.file_id}" if m.file_id else None,
                }
                for m in media
            ]

        if buttons:
            data["buttons"] = buttons

        # Send to session
        delivered = await self._send_to_session(chat_id, data)

        return SendResult(
            success=True,
            message_id=message_id,
            raw={"delivered": delivered, "queued": not delivered},
        )

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Edit an existing message."""
        data = {
            "type": "message_edit",
            "id": message_id,
            "text": text,
        }

        if buttons:
            data["buttons"] = buttons

        await self._send_to_session(chat_id, data)

        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a message."""
        data = {
            "type": "message_delete",
            "id": message_id,
        }

        await self._send_to_session(chat_id, data)
        return True

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        await self._send_to_session(chat_id, {
            "type": "typing",
            "from_name": "Bot",
        }, queue_if_offline=False)

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a session."""
        session = self._sessions.get(chat_id)
        if not session:
            return None

        return {
            "id": session.session_id,
            "type": "web",
            "user_id": session.user_id,
            "user_name": session.user_name,
            "is_connected": session.is_connected,
            "connection_count": len(session.websockets),
        }

    def get_active_sessions(self) -> List[Dict[str, Any]]:
        """Get all active sessions."""
        return [
            {
                "session_id": s.session_id,
                "user_id": s.user_id,
                "user_name": s.user_name,
                "is_connected": s.is_connected,
                "last_activity": s.last_activity.isoformat(),
            }
            for s in self._sessions.values()
        ]

    def get_read_receipts(self, message_id: str) -> List[str]:
        """Get list of session IDs that have read a message."""
        return list(self._read_receipts.get(message_id, set()))


def create_web_adapter(
    host: str = None,
    port: int = None,
    **kwargs
) -> WebAdapter:
    """
    Factory function to create Web adapter.

    Args:
        host: Host to bind to (default: 0.0.0.0)
        port: Port to bind to (default: 8765, or WEB_ADAPTER_PORT env var)
        **kwargs: Additional config options

    Returns:
        Configured WebAdapter
    """
    host = host or os.getenv("WEB_ADAPTER_HOST", "0.0.0.0")
    port = port or int(os.getenv("WEB_ADAPTER_PORT", "8765"))

    config = ChannelConfig(
        extra={
            "host": host,
            "port": port,
            **kwargs.get("extra", {}),
        },
        **{k: v for k, v in kwargs.items() if k != "extra"},
    )
    return WebAdapter(config)
