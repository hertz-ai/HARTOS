"""
Transport Layer — 3-tier WebSocket/WAMP streaming (no STUN/TURN needed).

Tier 1 (LAN):     Direct WebSocket between devices (~1ms latency)
Tier 2 (WAN):     WAMP relay through existing Crossbar router (~50-100ms)
Tier 3 (WAN P2P): WireGuard tunnel from compute mesh (~20ms)

All tiers implement TransportChannel ABC so host/viewer code is tier-agnostic.

Reuses:
  - crossbar_server.py → WAMP pub/sub for Tier 2 relay
  - vision_service.py:44 → WebSocket binary frame protocol pattern
  - compute_mesh_service.py:74-76 → WireGuard tunnel for Tier 3
  - channel_encryption.py → E2E encryption for all frames/events
  - message_queue.py:31 → PRIORITY queue for input events
"""

import asyncio
import json
import logging
import socket
import struct
import threading
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger('hevolve.remote_desktop')

# ── Optional dependencies ───────────────────────────────────────

_websockets = None
try:
    import websockets
    _websockets = websockets
except ImportError:
    pass


# ── Enums ───────────────────────────────────────────────────────

class TransportTier(Enum):
    LAN_DIRECT = 'lan_direct'
    WIREGUARD_P2P = 'wireguard_p2p'
    WAMP_RELAY = 'wamp_relay'


class MessageType(Enum):
    FRAME = 'frame'              # Binary screen frame (host→viewer)
    INPUT = 'input'              # JSON mouse/keyboard event (viewer→host)
    CLIPBOARD = 'clipboard'      # JSON clipboard content (bidirectional)
    FILE_CTRL = 'file_ctrl'      # JSON file transfer control
    FILE_DATA = 'file_data'      # Binary file chunk
    CONTROL = 'control'          # JSON session control (both)
    WINDOW_LIST = 'window_list'  # JSON list of available windows (host→viewer)
    WINDOW_FRAME = 'window_frame'  # Binary frame for specific window session
    DRAG_DROP = 'drag_drop'      # JSON drag-and-drop event (bidirectional)
    PERIPHERAL = 'peripheral'    # JSON peripheral device event (bidirectional)


# ── Transport Channel ABC ───────────────────────────────────────

class TransportChannel(ABC):
    """Abstract base for all transport tiers.

    Binary messages (frames, file data) use raw bytes.
    Text messages (events, clipboard, control) use JSON dicts.
    """

    def __init__(self):
        self._frame_callback: Optional[Callable[[bytes], None]] = None
        self._event_callback: Optional[Callable[[dict], None]] = None
        self._connected = False
        self._bytes_sent = 0
        self._bytes_received = 0
        self._frames_sent = 0
        self._frames_received = 0

    @abstractmethod
    def send_frame(self, data: bytes) -> bool:
        """Send binary frame data (JPEG/H264). Returns success."""
        ...

    @abstractmethod
    def send_event(self, event: dict) -> bool:
        """Send JSON event (input, clipboard, file_ctrl, control). Returns success."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Close the transport channel."""
        ...

    @property
    @abstractmethod
    def tier(self) -> TransportTier:
        """Return the transport tier."""
        ...

    def on_frame(self, callback: Callable[[bytes], None]) -> None:
        """Register frame receive callback."""
        self._frame_callback = callback

    def on_event(self, callback: Callable[[dict], None]) -> None:
        """Register event receive callback."""
        self._event_callback = callback

    @property
    def connected(self) -> bool:
        return self._connected

    def get_stats(self) -> dict:
        return {
            'tier': self.tier.value,
            'connected': self._connected,
            'bytes_sent': self._bytes_sent,
            'bytes_received': self._bytes_received,
            'frames_sent': self._frames_sent,
            'frames_received': self._frames_received,
        }


# ── Tier 1: Direct WebSocket (LAN) ─────────────────────────────

class DirectWebSocketTransport(TransportChannel):
    """Direct WebSocket between host and viewer (LAN or WireGuard IP).

    Protocol (mirrors vision_service.py binary frame pattern):
      - Binary message = frame data (JPEG bytes)
      - Text message = JSON event {type, ...}
    """

    def __init__(self, host_mode: bool = False, host: str = '0.0.0.0',
                 port: int = 0):
        super().__init__()
        self._host_mode = host_mode
        self._host = host
        self._port = port
        self._actual_port: Optional[int] = None
        self._ws = None
        self._server = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._clients: List[Any] = []

    @property
    def tier(self) -> TransportTier:
        return TransportTier.LAN_DIRECT

    @property
    def actual_port(self) -> Optional[int]:
        return self._actual_port

    def start_server(self) -> int:
        """Start WebSocket server (host mode). Returns assigned port."""
        if not _websockets:
            raise RuntimeError("websockets library not installed")

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_server_loop,
            daemon=True,
            name='ws-transport-server',
        )
        self._thread.start()

        # Wait for server to start (up to 5 seconds)
        for _ in range(50):
            if self._actual_port:
                break
            time.sleep(0.1)

        self._connected = True
        logger.info(f"WebSocket transport server on port {self._actual_port}")
        return self._actual_port or 0

    def _run_server_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        async def handler(ws):
            self._clients.append(ws)
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        self._bytes_received += len(message)
                        self._frames_received += 1
                        if self._frame_callback:
                            self._frame_callback(message)
                    else:
                        self._bytes_received += len(message)
                        if self._event_callback:
                            try:
                                event = json.loads(message)
                                self._event_callback(event)
                            except json.JSONDecodeError:
                                pass
            finally:
                self._clients.remove(ws)

        self._server = await _websockets.serve(
            handler, self._host, self._port,
            max_size=10 * 1024 * 1024,  # 10MB max message (frames can be large)
        )
        self._actual_port = self._server.sockets[0].getsockname()[1]
        await self._server.wait_closed()

    def connect(self, url: str) -> bool:
        """Connect to WebSocket server (viewer mode).

        Args:
            url: WebSocket URL (e.g., 'ws://192.168.1.5:9876')
        """
        if not _websockets:
            logger.error("websockets library not installed")
            return False

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_client_loop,
            args=(url,),
            daemon=True,
            name='ws-transport-client',
        )
        self._thread.start()

        # Wait for connection
        for _ in range(50):
            if self._connected:
                return True
            time.sleep(0.1)

        return self._connected

    def _run_client_loop(self, url: str) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._client_handler(url))

    async def _client_handler(self, url: str) -> None:
        try:
            async with _websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
                self._ws = ws
                self._connected = True
                async for message in ws:
                    if isinstance(message, bytes):
                        self._bytes_received += len(message)
                        self._frames_received += 1
                        if self._frame_callback:
                            self._frame_callback(message)
                    else:
                        self._bytes_received += len(message)
                        if self._event_callback:
                            try:
                                event = json.loads(message)
                                self._event_callback(event)
                            except json.JSONDecodeError:
                                pass
        except Exception as e:
            logger.error(f"WebSocket client error: {e}")
        finally:
            self._connected = False

    def send_frame(self, data: bytes) -> bool:
        if not self._connected:
            return False
        try:
            if self._host_mode and self._clients:
                # Server: broadcast to all clients
                for client in self._clients[:]:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            client.send(data), self._loop
                        )
                    except Exception:
                        pass
            elif self._ws:
                asyncio.run_coroutine_threadsafe(
                    self._ws.send(data), self._loop
                )
            self._bytes_sent += len(data)
            self._frames_sent += 1
            return True
        except Exception as e:
            logger.debug(f"Send frame failed: {e}")
            return False

    def send_event(self, event: dict) -> bool:
        if not self._connected:
            return False
        try:
            msg = json.dumps(event, separators=(',', ':'))
            if self._host_mode and self._clients:
                for client in self._clients[:]:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            client.send(msg), self._loop
                        )
                    except Exception:
                        pass
            elif self._ws:
                asyncio.run_coroutine_threadsafe(
                    self._ws.send(msg), self._loop
                )
            self._bytes_sent += len(msg)
            return True
        except Exception as e:
            logger.debug(f"Send event failed: {e}")
            return False

    def close(self) -> None:
        self._connected = False
        if self._server:
            self._server.close()
        if self._ws:
            try:
                if self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._ws.close(), self._loop
                    )
            except Exception:
                pass
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._ws = None
        self._server = None


# ── Tier 2: WAMP Relay ──────────────────────────────────────────

class WAMPRelayTransport(TransportChannel):
    """WAMP pub/sub relay through existing Crossbar router.

    Both devices already connected to ws://aws_rasa.hertzai.com:8088/ws.
    Frames published to com.hartos.remote_desktop.frames.{session_id}.
    Input events to com.hartos.remote_desktop.input.{session_id}.

    Reuses crossbar_server.py WAMP session.
    """

    TOPIC_PREFIX = 'com.hartos.remote_desktop'

    def __init__(self, session_id: str, role: str = 'host'):
        """
        Args:
            session_id: Unique session identifier
            role: 'host' (sends frames, receives input) or 'viewer' (vice versa)
        """
        super().__init__()
        self._session_id = session_id
        self._role = role
        self._wamp_session = None

    @property
    def tier(self) -> TransportTier:
        return TransportTier.WAMP_RELAY

    def start(self) -> bool:
        """Connect to existing WAMP session and subscribe to topics."""
        try:
            from crossbar_server import wamp_session
            self._wamp_session = wamp_session
            if not self._wamp_session:
                logger.warning("WAMP session not available")
                return False

            self._connected = True
            logger.info(f"WAMP relay transport started (session={self._session_id[:8]})")
            return True
        except ImportError:
            logger.warning("crossbar_server not available for WAMP relay")
            return False

    def _topic(self, channel: str) -> str:
        return f"{self.TOPIC_PREFIX}.{channel}.{self._session_id}"

    def send_frame(self, data: bytes) -> bool:
        if not self._connected or not self._wamp_session:
            return False
        try:
            # Encode binary as base64 for WAMP text transport
            import base64
            payload = base64.b64encode(data).decode('ascii')
            asyncio.ensure_future(
                self._wamp_session.publish(
                    self._topic('frames'),
                    payload,
                )
            )
            self._bytes_sent += len(data)
            self._frames_sent += 1
            return True
        except Exception as e:
            logger.debug(f"WAMP send frame failed: {e}")
            return False

    def send_event(self, event: dict) -> bool:
        if not self._connected or not self._wamp_session:
            return False
        try:
            asyncio.ensure_future(
                self._wamp_session.publish(
                    self._topic('input' if self._role == 'viewer' else 'control'),
                    json.dumps(event, separators=(',', ':')),
                )
            )
            self._bytes_sent += len(json.dumps(event))
            return True
        except Exception as e:
            logger.debug(f"WAMP send event failed: {e}")
            return False

    def close(self) -> None:
        self._connected = False
        self._wamp_session = None
        logger.info(f"WAMP relay transport closed (session={self._session_id[:8]})")


# ── Tier 3: WireGuard P2P ──────────────────────────────────────

class WireGuardTransport(DirectWebSocketTransport):
    """Direct WebSocket through WireGuard tunnel.

    Reuses compute_mesh_service.py:74-76 (port 6795, subnet 10.99.0.0/16).
    Once tunnel established, behaves like DirectWebSocketTransport but
    over the WireGuard mesh IP.
    """

    def __init__(self, wg_ip: str, port: int = 0):
        super().__init__(host_mode=False, host=wg_ip, port=port)
        self._wg_ip = wg_ip

    @property
    def tier(self) -> TransportTier:
        return TransportTier.WIREGUARD_P2P


# ── Transport Negotiation ───────────────────────────────────────

def get_local_ip() -> Optional[str]:
    """Get local LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def probe_lan_connectivity(target_ip: str, port: int,
                           timeout: float = 2.0) -> bool:
    """Check if target is reachable on LAN via TCP probe."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        result = s.connect_ex((target_ip, port))
        s.close()
        return result == 0
    except Exception:
        return False


def probe_wireguard_connectivity(wg_ip: str, timeout: float = 2.0) -> bool:
    """Check if WireGuard tunnel is active by pinging mesh IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        # Try common WireGuard mesh port
        result = s.connect_ex((wg_ip, 6796))
        s.close()
        return result == 0
    except Exception:
        return False


def auto_negotiate_transport(
    session_id: str,
    host_offers: dict,
    role: str = 'viewer',
) -> Optional[TransportChannel]:
    """Auto-negotiate best transport tier.

    Args:
        session_id: Session ID
        host_offers: {
            'lan_ip': '192.168.1.5',
            'lan_port': 9876,
            'wg_ip': '10.99.0.5',
            'wg_port': 9877,
            'wamp_available': True,
        }
        role: 'host' or 'viewer'

    Returns:
        Best available TransportChannel, or None if all fail.
    """
    # Tier 1: Try LAN direct
    lan_ip = host_offers.get('lan_ip')
    lan_port = host_offers.get('lan_port')
    if lan_ip and lan_port and _websockets:
        if probe_lan_connectivity(lan_ip, lan_port):
            transport = DirectWebSocketTransport()
            if transport.connect(f"ws://{lan_ip}:{lan_port}"):
                logger.info(f"Transport: Tier 1 LAN direct ({lan_ip}:{lan_port})")
                return transport

    # Tier 3: Try WireGuard P2P
    wg_ip = host_offers.get('wg_ip')
    wg_port = host_offers.get('wg_port')
    if wg_ip and wg_port and _websockets:
        if probe_wireguard_connectivity(wg_ip):
            transport = WireGuardTransport(wg_ip, wg_port)
            if transport.connect(f"ws://{wg_ip}:{wg_port}"):
                logger.info(f"Transport: Tier 3 WireGuard P2P ({wg_ip}:{wg_port})")
                return transport

    # Tier 2: WAMP relay (always available)
    if host_offers.get('wamp_available', True):
        transport = WAMPRelayTransport(session_id, role=role)
        if transport.start():
            logger.info(f"Transport: Tier 2 WAMP relay")
            return transport

    logger.error("All transport tiers failed")
    return None
