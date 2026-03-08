"""
Gateway Bridge — Connect HART OS to OpenClaw's WebSocket control plane.

OpenClaw runs a Gateway at ws://127.0.0.1:18789. We connect to it
bidirectionally:

  Inbound:  HART agents can send messages through OpenClaw's channels
            (WhatsApp, Telegram, Discord, etc.)
  Outbound: OpenClaw agents can invoke HART recipes, tools, and agents

This makes HART OS the superset — it can use all of OpenClaw's 20+
channel adapters natively, plus its own 30+ channel integrations.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_GATEWAY_URL = 'ws://127.0.0.1:18789'


class OpenClawGatewayBridge:
    """Bidirectional bridge to the OpenClaw Gateway WebSocket.

    Lifecycle:
        bridge = OpenClawGatewayBridge()
        bridge.connect()                    # Connect to running gateway
        bridge.send_message(channel, msg)   # Send via OpenClaw channel
        bridge.on_message(handler)          # Receive from OpenClaw
        bridge.disconnect()
    """

    def __init__(self, gateway_url: Optional[str] = None):
        self._url = gateway_url or os.environ.get(
            'OPENCLAW_GATEWAY_URL', DEFAULT_GATEWAY_URL
        )
        self._ws = None
        self._connected = False
        self._handlers: List[Callable] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def gateway_url(self) -> str:
        return self._url

    def on_message(self, handler: Callable[[Dict[str, Any]], None]):
        """Register a handler for inbound OpenClaw messages."""
        self._handlers.append(handler)

    def connect(self) -> bool:
        """Connect to the OpenClaw Gateway.

        Returns True if connected, False if gateway is unavailable.
        Starts a background thread for the WebSocket event loop.
        """
        if self._connected:
            return True

        try:
            import websockets  # noqa: F401
        except ImportError:
            logger.warning("websockets not installed, OpenClaw bridge unavailable")
            return False

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name='openclaw-gateway'
        )
        self._thread.start()

        # Wait briefly for connection
        for _ in range(20):
            if self._connected:
                return True
            import time
            time.sleep(0.1)

        return self._connected

    def _run_loop(self):
        """Background event loop for WebSocket."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._ws_loop())

    async def _ws_loop(self):
        """WebSocket connection loop with auto-reconnect."""
        try:
            import websockets
        except ImportError:
            return

        while True:
            try:
                async with websockets.connect(self._url) as ws:
                    self._ws = ws
                    self._connected = True
                    logger.info("Connected to OpenClaw gateway at %s", self._url)

                    # Announce HART OS as a tool provider
                    await ws.send(json.dumps({
                        'type': 'hart_announce',
                        'capabilities': [
                            'recipe_execution',
                            'agent_dispatch',
                            'model_bus',
                            'compute_mesh',
                            'vision',
                            'tts',
                        ],
                    }))

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            for handler in self._handlers:
                                handler(msg)
                        except json.JSONDecodeError:
                            logger.debug("Non-JSON from gateway: %s", raw[:100])

            except Exception as e:
                self._connected = False
                self._ws = None
                logger.debug("Gateway connection lost: %s, retrying in 5s", e)
                await asyncio.sleep(5)

    def send_message(self, channel: str, message: str,
                     recipient: Optional[str] = None) -> bool:
        """Send a message through OpenClaw's channel system.

        Args:
            channel: Channel name (whatsapp, telegram, discord, slack, etc.)
            message: Message text
            recipient: Optional recipient ID for DM channels
        """
        if not self._connected or not self._ws:
            logger.warning("Not connected to OpenClaw gateway")
            return False

        payload = {
            'type': 'send_message',
            'channel': channel,
            'message': message,
        }
        if recipient:
            payload['recipient'] = recipient

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._ws.send(json.dumps(payload)), self._loop
            )
            future.result(timeout=5)
            return True
        except Exception as e:
            logger.error("Failed to send via OpenClaw: %s", e)
            return False

    def invoke_tool(self, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
        """Invoke an OpenClaw tool through the gateway.

        This lets HART agents use OpenClaw's built-in and skill tools.
        """
        if not self._connected or not self._ws:
            return None

        payload = {
            'type': 'tool_invoke',
            'tool': tool_name,
            'args': args,
        }

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._ws.send(json.dumps(payload)), self._loop
            )
            future.result(timeout=10)
            return '{"status": "dispatched"}'
        except Exception as e:
            logger.error("Tool invoke failed: %s", e)
            return None

    def disconnect(self):
        """Disconnect from the OpenClaw gateway."""
        self._connected = False
        if self._ws:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._ws.close(), self._loop
                )
            except Exception:
                pass
            self._ws = None
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def health(self) -> Dict[str, Any]:
        """Health check for the gateway connection."""
        return {
            'connected': self._connected,
            'gateway_url': self._url,
            'openclaw_installed': shutil.which('openclaw') is not None,
        }


# ── OpenClaw Process Management ───────────────────────────────────

def is_openclaw_installed() -> bool:
    """Check if OpenClaw is installed on the system."""
    return shutil.which('openclaw') is not None


def get_openclaw_version() -> Optional[str]:
    """Get installed OpenClaw version."""
    if not is_openclaw_installed():
        return None
    try:
        result = subprocess.run(
            ['openclaw', '--version'],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def start_openclaw_gateway(port: int = 18789) -> Optional[subprocess.Popen]:
    """Start the OpenClaw gateway process.

    HART OS manages OpenClaw as a native app — we start/stop it like
    any other service (RustDesk, Sunshine, llama.cpp).
    """
    if not is_openclaw_installed():
        logger.warning("OpenClaw not installed, cannot start gateway")
        return None

    try:
        proc = subprocess.Popen(
            ['openclaw', 'gateway', '--port', str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        logger.info("Started OpenClaw gateway on port %d (PID %d)", port, proc.pid)
        return proc
    except Exception as e:
        logger.error("Failed to start OpenClaw gateway: %s", e)
        return None


# ── Singleton ──────────────────────────────────────────────────────

_bridge: Optional[OpenClawGatewayBridge] = None


def get_gateway_bridge() -> OpenClawGatewayBridge:
    """Get the singleton gateway bridge."""
    global _bridge
    if _bridge is None:
        _bridge = OpenClawGatewayBridge()
    return _bridge
