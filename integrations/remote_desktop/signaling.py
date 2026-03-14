"""
Signaling — WAMP-based connection negotiation for remote desktop.

How two devices find each other and agree on transport tier:
  1. Viewer sends connect_request to host's signal topic (includes OTP)
  2. Host verifies OTP → sends connect_accept with transport offers
  3. Both sides run auto_negotiate_transport() to pick best tier
  4. Streaming begins on selected transport

Channels:
  WAMP topic: com.hartos.remote_desktop.signal.{device_id}
  HTTP fallback: POST/GET /api/remote-desktop/signal/<device_id>

Reuses:
  - crossbar_server.py WAMP publish/subscribe
  - security/channel_encryption.py encrypt_event() for encrypted signals
  - /api/remote-desktop/signal endpoints (already wired in langchain_gpt_api.py)
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from core.port_registry import get_port
from core.http_pool import pooled_get, pooled_post

logger = logging.getLogger('hevolve.remote_desktop')


class SignalType(Enum):
    CONNECT_REQUEST = 'connect_request'
    CONNECT_ACCEPT = 'connect_accept'
    CONNECT_REJECT = 'connect_reject'
    TRANSPORT_OFFER = 'transport_offer'
    BYE = 'bye'


@dataclass
class SignalingMessage:
    """A signaling message between two devices."""
    msg_type: str
    sender_device_id: str
    target_device_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'SignalingMessage':
        return cls(
            msg_type=data.get('msg_type', ''),
            sender_device_id=data.get('sender_device_id', ''),
            target_device_id=data.get('target_device_id', ''),
            payload=data.get('payload', {}),
            message_id=data.get('message_id', str(uuid.uuid4())[:12]),
            timestamp=data.get('timestamp', time.time()),
        )


class SignalingChannel:
    """WAMP-based signaling with HTTP fallback.

    Subscribes to device's WAMP topic for incoming connection requests.
    Publishes to target device's topic for outgoing signals.
    Falls back to HTTP API when WAMP is unavailable.
    """

    TOPIC_PREFIX = 'com.hartos.remote_desktop.signal'

    def __init__(self, device_id: str, api_base: Optional[str] = None):
        """
        Args:
            device_id: This device's ID (subscribes to signal.{device_id})
            api_base: HTTP API base URL (e.g., 'http://localhost:6777')
        """
        self._device_id = device_id
        self._api_base = api_base or f'http://localhost:{get_port("backend")}'
        self._wamp_session = None
        self._callback: Optional[Callable[[SignalingMessage], None]] = None
        self._pending_signals: List[SignalingMessage] = []
        self._connected = False

    def start(self) -> bool:
        """Start listening for signals on WAMP topic."""
        try:
            from crossbar_server import wamp_session
            self._wamp_session = wamp_session
            if self._wamp_session:
                topic = f"{self.TOPIC_PREFIX}.{self._device_id}"
                self._wamp_session.subscribe(
                    self._on_wamp_signal, topic
                )
                self._connected = True
                logger.info(f"Signaling channel started (WAMP: {topic})")
                return True
        except ImportError:
            pass

        # WAMP not available — use HTTP polling mode
        logger.info("Signaling channel using HTTP fallback")
        self._connected = True
        return True

    def send_signal(self, target_device_id: str,
                    message: SignalingMessage) -> bool:
        """Send a signaling message to another device."""
        message.sender_device_id = self._device_id
        message.target_device_id = target_device_id

        # Try WAMP first
        if self._wamp_session:
            return self._send_wamp(target_device_id, message)

        # Fall back to HTTP
        return self._send_http(target_device_id, message)

    def on_signal(self, callback: Callable[[SignalingMessage], None]) -> None:
        """Register handler for incoming signals."""
        self._callback = callback

    def get_pending(self) -> List[SignalingMessage]:
        """Get and clear pending signals (for HTTP polling)."""
        pending = list(self._pending_signals)
        self._pending_signals.clear()
        return pending

    def close(self) -> None:
        """Close the signaling channel."""
        self._connected = False
        self._wamp_session = None
        self._callback = None
        logger.info("Signaling channel closed")

    # ── WAMP transport ───────────────────────────────────────

    def _send_wamp(self, target_device_id: str,
                   message: SignalingMessage) -> bool:
        """Send signal via WAMP publish."""
        try:
            topic = f"{self.TOPIC_PREFIX}.{target_device_id}"
            payload = json.dumps(message.to_dict(), separators=(',', ':'))

            # Encrypt if possible
            try:
                from integrations.remote_desktop.security import encrypt_event
                encrypted = encrypt_event(message.to_dict(), None)
                if encrypted:
                    payload = json.dumps(encrypted, separators=(',', ':'))
            except Exception:
                pass

            import asyncio
            asyncio.ensure_future(
                self._wamp_session.publish(topic, payload)
            )
            logger.debug(f"Signal sent (WAMP): {message.msg_type} → {target_device_id[:8]}")
            return True
        except Exception as e:
            logger.warning(f"WAMP signal send failed, trying HTTP: {e}")
            return self._send_http(target_device_id, message)

    def _on_wamp_signal(self, payload_str: str) -> None:
        """Handle incoming WAMP signal."""
        try:
            data = json.loads(payload_str)

            # Try decrypt
            try:
                from integrations.remote_desktop.security import decrypt_event
                decrypted = decrypt_event(data)
                if decrypted:
                    data = decrypted
            except Exception:
                pass

            message = SignalingMessage.from_dict(data)

            if self._callback:
                self._callback(message)
            else:
                self._pending_signals.append(message)
        except Exception as e:
            logger.debug(f"Signal parse error: {e}")

    # ── HTTP fallback ────────────────────────────────────────

    def _send_http(self, target_device_id: str,
                   message: SignalingMessage) -> bool:
        """Send signal via HTTP API endpoint."""
        try:
            resp = pooled_post(
                f"{self._api_base}/api/remote-desktop/signal",
                json={
                    'target_device_id': target_device_id,
                    'message': message.to_dict(),
                },
                timeout=5,
            )
            return resp.status_code in (200, 201)
        except Exception as e:
            logger.debug(f"HTTP signal send failed: {e}")
            return False

    def poll_signals_http(self) -> List[SignalingMessage]:
        """Poll for incoming signals via HTTP (fallback for no WAMP)."""
        try:
            resp = pooled_get(
                f"{self._api_base}/api/remote-desktop/signal/{self._device_id}",
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                signals = data.get('signals', [])
                return [SignalingMessage.from_dict(s) for s in signals]
        except Exception as e:
            logger.debug(f"HTTP signal poll failed: {e}")
        return []


# ── Convenience functions ─────────────────────────────────────

def create_connect_request(sender_id: str, target_id: str,
                           password: str, mode: str = 'full_control',
                           session_id: Optional[str] = None) -> SignalingMessage:
    """Create a CONNECT_REQUEST signaling message."""
    return SignalingMessage(
        msg_type=SignalType.CONNECT_REQUEST.value,
        sender_device_id=sender_id,
        target_device_id=target_id,
        payload={
            'password': password,
            'mode': mode,
            'session_id': session_id or str(uuid.uuid4()),
        },
    )


def create_connect_accept(sender_id: str, target_id: str,
                           session_id: str,
                           transport_offers: Optional[dict] = None) -> SignalingMessage:
    """Create a CONNECT_ACCEPT signaling message with transport offers."""
    from integrations.remote_desktop.transport import get_local_ip
    offers = transport_offers or {}

    # Auto-populate LAN offer
    if 'lan_ip' not in offers:
        local_ip = get_local_ip()
        if local_ip:
            offers['lan_ip'] = local_ip

    offers.setdefault('wamp_available', True)

    return SignalingMessage(
        msg_type=SignalType.CONNECT_ACCEPT.value,
        sender_device_id=sender_id,
        target_device_id=target_id,
        payload={
            'session_id': session_id,
            'transport_offers': offers,
        },
    )


def create_connect_reject(sender_id: str, target_id: str,
                           reason: str) -> SignalingMessage:
    """Create a CONNECT_REJECT signaling message."""
    return SignalingMessage(
        msg_type=SignalType.CONNECT_REJECT.value,
        sender_device_id=sender_id,
        target_device_id=target_id,
        payload={'reason': reason},
    )


def create_bye(sender_id: str, target_id: str,
               session_id: str) -> SignalingMessage:
    """Create a BYE signaling message (session end)."""
    return SignalingMessage(
        msg_type=SignalType.BYE.value,
        sender_device_id=sender_id,
        target_device_id=target_id,
        payload={'session_id': session_id},
    )
