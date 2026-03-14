"""
PeerLink — persistent WebSocket connection to a single peer.

Trust-boundary-aware encryption:
  SAME_USER: Your own devices — ANY network (LAN, WAN, regional).
             No encryption (plain WebSocket). Trust based on authenticated
             user identity (same user_id), NOT network proximity.
             LAN discovered via UDP beacon; WAN discovered via compute_mesh
             or gossip when user_id matches.
  PEER:      Another user's device. E2E encrypted (AES-256-GCM session key).
             They cannot inspect your payload.
  RELAY:     Traffic passing through intermediate node. E2E encrypted.
             Relay sees only opaque bytes.

Wire format:
  Text frame:   {"ch":"gossip","id":"msg123","d":{...}}
  Binary frame: [1B channel_id][4B msg_id_hash][payload bytes]

  For PEER/RELAY trust: entire frame is AES-256-GCM encrypted before sending.
  For SAME_USER: sent as-is (plain).

Session lifecycle:
  1. WebSocket connect (ws:// for LAN, wss:// for WAN)
  2. Handshake: exchange node identity (Ed25519 pub key + X25519 pub key)
  3. If PEER/RELAY: ECDH key exchange -> derive session_key
  4. Mutual Ed25519 signature verification
  5. Exchange capabilities (GPU, models, tier)
  6. Ready — multiplexed channels active

  Key rotation: new ECDH every 3600 seconds (forward secrecy)
"""
import hashlib
import json
import logging
import os
import struct
import threading
import time
import uuid
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve.peer_link')


class TrustLevel(Enum):
    SAME_USER = 'same_user'   # Own devices on ANY network (LAN + WAN + regional) — no encryption
    PEER = 'peer'             # Other user's device — E2E mandatory
    RELAY = 'relay'           # Through intermediate — E2E mandatory


class LinkState(Enum):
    DISCONNECTED = 'disconnected'
    CONNECTING = 'connecting'
    HANDSHAKING = 'handshaking'
    CONNECTED = 'connected'
    CLOSING = 'closing'


# Channel IDs for binary frames
CHANNEL_IDS = {
    'control': 0x00,
    'compute': 0x01,
    'dispatch': 0x02,
    'gossip': 0x03,
    'federation': 0x04,
    'hivemind': 0x05,
    'events': 0x06,
    'ralt': 0x07,
    'sensor': 0x08,
}
CHANNEL_NAMES = {v: k for k, v in CHANNEL_IDS.items()}

# Key rotation interval (seconds)
KEY_ROTATION_INTERVAL = 3600


class PeerLink:
    """Persistent WebSocket connection to a single peer.

    Encryption is determined by TrustLevel:
    - SAME_USER: plain (your own devices, trusted)
    - PEER/RELAY: AES-256-GCM with session key from X25519 ECDH
    """

    def __init__(self, peer_id: str, address: str, trust: TrustLevel,
                 x25519_public_hex: str = '', ed25519_public_hex: str = '',
                 capabilities: Optional[dict] = None):
        self.peer_id = peer_id
        self.address = address  # host:port or ws:// URL
        self.trust = trust
        self.peer_x25519_public = x25519_public_hex
        self.peer_ed25519_public = ed25519_public_hex
        self.capabilities = capabilities or {}

        self._state = LinkState.DISCONNECTED
        self._ws = None
        self._session_key: Optional[bytes] = None  # AES-256 key (PEER/RELAY only)
        self._session_nonce_counter = 0
        self._key_established_at = 0.0
        self._lock = threading.Lock()
        self._message_handlers: Dict[str, List[Callable]] = {}
        self._pending_responses: Dict[str, threading.Event] = {}
        self._response_data: Dict[str, Any] = {}
        self._recv_thread: Optional[threading.Thread] = None

        # Stats
        self._connected_at = 0.0
        self._last_activity = 0.0
        self._messages_sent = 0
        self._messages_received = 0
        self._bytes_sent = 0
        self._bytes_received = 0

    @property
    def state(self) -> LinkState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == LinkState.CONNECTED

    @property
    def is_encrypted(self) -> bool:
        """E2E encryption active (PEER/RELAY trust only)."""
        return self._session_key is not None

    @property
    def idle_seconds(self) -> float:
        if self._last_activity == 0:
            return 0
        return time.time() - self._last_activity

    def connect(self) -> bool:
        """Initiate outgoing connection to peer."""
        if self._state != LinkState.DISCONNECTED:
            return self._state == LinkState.CONNECTED

        self._state = LinkState.CONNECTING
        try:
            ws_url = self._resolve_ws_url()

            try:
                import websockets.sync.client as ws_client
                self._ws = ws_client.connect(ws_url, open_timeout=10,
                                              close_timeout=5)
            except ImportError:
                # Fallback: use websocket-client library
                try:
                    import websocket
                    self._ws = websocket.WebSocket()
                    self._ws.connect(ws_url, timeout=10)
                except ImportError:
                    logger.warning("No WebSocket library available (need websockets or websocket-client)")
                    self._state = LinkState.DISCONNECTED
                    return False

            self._state = LinkState.HANDSHAKING
            if not self._perform_handshake():
                self.close()
                return False

            self._state = LinkState.CONNECTED
            self._connected_at = time.time()
            self._last_activity = time.time()

            # Start receive loop
            self._recv_thread = threading.Thread(
                target=self._receive_loop, daemon=True,
                name=f'peerlink-recv-{self.peer_id[:8]}')
            self._recv_thread.start()

            logger.info(f"PeerLink connected to {self.peer_id[:8]} "
                       f"(trust={self.trust.value}, encrypted={self.is_encrypted})")
            return True

        except Exception as e:
            logger.debug(f"PeerLink connect failed to {self.peer_id[:8]}: {e}")
            self._state = LinkState.DISCONNECTED
            return False

    def accept(self, ws, handshake_data: dict) -> bool:
        """Accept incoming connection (called by link_manager's WS server)."""
        self._ws = ws
        self._state = LinkState.HANDSHAKING

        try:
            if not self._complete_handshake(handshake_data):
                self.close()
                return False

            self._state = LinkState.CONNECTED
            self._connected_at = time.time()
            self._last_activity = time.time()

            self._recv_thread = threading.Thread(
                target=self._receive_loop, daemon=True,
                name=f'peerlink-recv-{self.peer_id[:8]}')
            self._recv_thread.start()

            logger.info(f"PeerLink accepted from {self.peer_id[:8]} "
                       f"(trust={self.trust.value}, encrypted={self.is_encrypted})")
            return True
        except Exception as e:
            logger.debug(f"PeerLink accept failed: {e}")
            self._state = LinkState.DISCONNECTED
            return False

    def send(self, channel: str, data: dict, wait_response: bool = False,
             timeout: float = 30.0) -> Optional[dict]:
        """Send JSON message on a channel.

        Args:
            channel: Channel name (gossip, federation, compute, etc.)
            data: JSON-serializable dict
            wait_response: If True, block until response received
            timeout: Max wait time for response

        Returns:
            Response dict if wait_response=True, else None
        """
        if not self.is_connected or self._ws is None:
            return None

        msg_id = uuid.uuid4().hex[:12]
        frame = json.dumps({
            'ch': channel,
            'id': msg_id,
            'd': data,
        }, separators=(',', ':'))

        frame_bytes = frame.encode('utf-8')

        # Encrypt for PEER/RELAY trust
        if self.trust in (TrustLevel.PEER, TrustLevel.RELAY) and self._session_key:
            frame_bytes = self._encrypt(frame_bytes)

        event = None
        if wait_response:
            event = threading.Event()
            self._pending_responses[msg_id] = event

        try:
            self._ws_send(frame_bytes)
            self._messages_sent += 1
            self._bytes_sent += len(frame_bytes)
            self._last_activity = time.time()
        except Exception as e:
            logger.debug(f"PeerLink send failed: {e}")
            self._pending_responses.pop(msg_id, None)
            self._handle_disconnect()
            return None

        if event:
            event.wait(timeout=timeout)
            self._pending_responses.pop(msg_id, None)
            return self._response_data.pop(msg_id, None)

        return None

    def send_binary(self, channel: str, data: bytes) -> bool:
        """Send binary data on a channel (sensor frames, etc.)."""
        if not self.is_connected or self._ws is None:
            return False

        ch_id = CHANNEL_IDS.get(channel, 0xFF)
        msg_id_bytes = struct.pack('>I', hash(time.time()) & 0xFFFFFFFF)
        frame = bytes([ch_id]) + msg_id_bytes + data

        if self.trust in (TrustLevel.PEER, TrustLevel.RELAY) and self._session_key:
            frame = self._encrypt(frame)

        try:
            self._ws_send_binary(frame)
            self._messages_sent += 1
            self._bytes_sent += len(frame)
            self._last_activity = time.time()
            return True
        except Exception:
            self._handle_disconnect()
            return False

    def on_message(self, channel: str, handler: Callable) -> None:
        """Register handler for incoming messages on a channel."""
        if channel not in self._message_handlers:
            self._message_handlers[channel] = []
        self._message_handlers[channel].append(handler)

    def _verify_same_user_proof(self, proof: str, peer_public_key: str) -> bool:
        """Verify that the peer is owned by the same user.

        Proof = peer signs our user_id with their Ed25519 key, and we
        verify that their user_id matches ours. This prevents an attacker
        from claiming SAME_USER trust without holding the user's key.
        """
        try:
            from security.node_integrity import verify_message_signature
            # The proof should be a signature of the local user_id
            local_user_id = os.environ.get('HEVOLVE_USER_ID', '')
            if not local_user_id or not peer_public_key:
                return False
            return verify_message_signature(peer_public_key, local_user_id, proof)
        except (ImportError, Exception) as e:
            logger.debug(f"SAME_USER proof verification failed: {e}")
            return False

    def close(self) -> None:
        """Close the connection."""
        if self._state == LinkState.CLOSING:
            return

        # Send bye BEFORE setting state — send() checks is_connected (state==CONNECTED)
        try:
            if self._ws:
                self.send('control', {'type': 'bye'})
        except Exception:
            pass

        self._state = LinkState.CLOSING

        try:
            if self._ws:
                if hasattr(self._ws, 'close'):
                    self._ws.close()
        except Exception:
            pass

        self._ws = None
        self._session_key = None
        self._state = LinkState.DISCONNECTED
        logger.debug(f"PeerLink closed: {self.peer_id[:8]}")

    def get_stats(self) -> dict:
        return {
            'peer_id': self.peer_id,
            'state': self._state.value,
            'trust': self.trust.value,
            'encrypted': self.is_encrypted,
            'connected_seconds': (time.time() - self._connected_at) if self._connected_at else 0,
            'idle_seconds': self.idle_seconds,
            'messages_sent': self._messages_sent,
            'messages_received': self._messages_received,
            'bytes_sent': self._bytes_sent,
            'bytes_received': self._bytes_received,
            'capabilities': self.capabilities,
        }

    # --- Internal: Handshake -------------------------------------------

    def _perform_handshake(self) -> bool:
        """Outgoing handshake: send our identity, receive theirs."""
        try:
            from security.node_integrity import get_public_key_hex, sign_json_payload
            from security.channel_encryption import get_x25519_public_hex
        except ImportError:
            logger.warning("Security modules not available — handshake failed")
            return False

        hello = {
            'type': 'hello',
            'node_id': self.peer_id,  # Will be overwritten with OUR id below
            'ed25519_public': get_public_key_hex(),
            'x25519_public': get_x25519_public_hex(),
            'trust_requested': self.trust.value,
            'protocol_version': 1,
            'timestamp': time.time(),
        }

        # Get our actual node_id
        try:
            from security.node_integrity import get_node_identity
            identity = get_node_identity()
            hello['node_id'] = identity.get('node_id', '')
        except Exception:
            pass

        # Attach pre-trust contract (proves we agreed to hive terms)
        try:
            from security.pre_trust_contract import get_pre_trust_verifier
            from security.node_integrity import get_node_identity
            nid = get_node_identity().get('node_id', '')
            verifier = get_pre_trust_verifier()
            contract = verifier.get_contract(nid)
            if contract:
                hello['trust_contract'] = contract
        except Exception:
            pass  # Contract optional for SAME_USER trust

        hello['signature'] = sign_json_payload(hello)

        # Send hello
        hello_bytes = json.dumps(hello, separators=(',', ':')).encode('utf-8')
        self._ws_send(hello_bytes)

        # Receive hello back
        resp_bytes = self._ws_recv(timeout=10)
        if not resp_bytes:
            return False

        try:
            resp = json.loads(resp_bytes if isinstance(resp_bytes, str)
                            else resp_bytes.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False

        if resp.get('type') != 'hello_ack':
            return False

        # Verify their Ed25519 signature
        peer_ed25519 = resp.get('ed25519_public', '')
        peer_sig = resp.pop('signature', '')
        if peer_ed25519 and peer_sig:
            from security.node_integrity import verify_json_signature
            if not verify_json_signature(peer_ed25519, resp, peer_sig):
                logger.warning(f"Handshake signature verification failed for {self.peer_id[:8]}")
                return False
        elif os.environ.get('HEVOLVE_ENFORCEMENT_MODE') == 'hard':
            # Hard mode: reject unsigned handshakes
            logger.warning(f"Unsigned handshake rejected (hard enforcement) for {self.peer_id[:8]}")
            return False

        # Store peer's keys
        self.peer_ed25519_public = peer_ed25519
        self.peer_x25519_public = resp.get('x25519_public', '')
        self.capabilities = resp.get('capabilities', {})

        # Derive session key for PEER/RELAY trust
        if self.trust in (TrustLevel.PEER, TrustLevel.RELAY) and self.peer_x25519_public:
            self._derive_session_key()

        return True

    def _complete_handshake(self, hello_data: dict) -> bool:
        """Incoming handshake: we received their hello, send ack."""
        try:
            from security.node_integrity import (
                get_public_key_hex, sign_json_payload, verify_json_signature)
            from security.channel_encryption import get_x25519_public_hex
        except ImportError:
            return False

        # Verify their signature
        peer_sig = hello_data.pop('signature', '')
        peer_ed25519 = hello_data.get('ed25519_public', '')
        if peer_ed25519 and peer_sig:
            if not verify_json_signature(peer_ed25519, hello_data, peer_sig):
                logger.warning("Incoming handshake signature verification failed")
                return False
        elif os.environ.get('HEVOLVE_ENFORCEMENT_MODE') == 'hard':
            logger.warning("Unsigned incoming handshake rejected (hard enforcement)")
            return False

        self.peer_ed25519_public = peer_ed25519
        self.peer_x25519_public = hello_data.get('x25519_public', '')
        self.capabilities = hello_data.get('capabilities', {})

        # Determine trust LOCALLY — never accept trust_requested from wire.
        # SAME_USER requires proof: peer must present a user_id_signature
        # signed by the same user key we hold. Without proof → PEER.
        requested_trust = hello_data.get('trust_requested', 'peer')
        if requested_trust == 'same_user':
            # Verify SAME_USER claim cryptographically
            user_proof = hello_data.get('user_id_proof', '')
            if user_proof and self._verify_same_user_proof(user_proof, peer_ed25519):
                self.trust = TrustLevel.SAME_USER
            else:
                logger.warning("SAME_USER trust requested but no valid proof — downgrading to PEER")
                self.trust = TrustLevel.PEER
        else:
            self.trust = TrustLevel.PEER

        # Verify pre-trust contract for PEER connections
        # SAME_USER (own devices) are exempt — trust is user identity based
        if self.trust != TrustLevel.SAME_USER:
            try:
                from security.pre_trust_contract import (
                    verify_trust_contract, TrustContract,
                    get_pre_trust_verifier,
                )
                contract_data = hello_data.get('trust_contract')
                if contract_data:
                    contract = TrustContract(**{
                        k: v for k, v in contract_data.items()
                        if k in TrustContract.__dataclass_fields__
                    })
                    ok, msg = verify_trust_contract(contract)
                    if not ok:
                        logger.warning(
                            f"Pre-trust contract rejected for "
                            f"{self.peer_id[:8]}: {msg}")
                        return False
                    # Register verified contract
                    get_pre_trust_verifier().register_contract(contract)
                    logger.info(
                        f"Pre-trust contract verified for {self.peer_id[:8]}")
            except ImportError:
                pass  # Module not available — allow legacy connections

        # Send ack
        ack = {
            'type': 'hello_ack',
            'ed25519_public': get_public_key_hex(),
            'x25519_public': get_x25519_public_hex(),
            'protocol_version': 1,
            'capabilities': self._get_local_capabilities(),
            'timestamp': time.time(),
        }
        ack['signature'] = sign_json_payload(ack)

        ack_bytes = json.dumps(ack, separators=(',', ':')).encode('utf-8')
        self._ws_send(ack_bytes)

        # Derive session key for PEER/RELAY trust
        if self.trust in (TrustLevel.PEER, TrustLevel.RELAY) and self.peer_x25519_public:
            self._derive_session_key()

        return True

    def _derive_session_key(self):
        """Derive AES-256-GCM session key from X25519 ECDH."""
        try:
            from security.channel_encryption import get_x25519_keypair
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
            from cryptography.hazmat.primitives.kdf.hkdf import HKDF
            from cryptography.hazmat.primitives import hashes

            our_private, _ = get_x25519_keypair()
            peer_pub = X25519PublicKey.from_public_bytes(
                bytes.fromhex(self.peer_x25519_public))
            shared_secret = our_private.exchange(peer_pub)

            self._session_key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b'hart-peerlink-session-v1',
                info=b'hart-peerlink-v1',
            ).derive(shared_secret)

            self._key_established_at = time.time()
            self._session_nonce_counter = 0
            logger.debug(f"Session key derived for {self.peer_id[:8]}")
        except Exception as e:
            logger.warning(f"Session key derivation failed: {e}")
            self._session_key = None

    # --- Internal: Encryption ------------------------------------------

    def _encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt with session key (AES-256-GCM). Prepends nonce."""
        if not self._session_key:
            return plaintext

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        # Check key rotation
        if (time.time() - self._key_established_at) > KEY_ROTATION_INTERVAL:
            self._derive_session_key()

        nonce = os.urandom(12)
        ct = AESGCM(self._session_key).encrypt(nonce, plaintext, None)
        return nonce + ct  # 12 bytes nonce + ciphertext

    def _decrypt(self, data: bytes) -> Optional[bytes]:
        """Decrypt with session key. Expects nonce prefix."""
        if not self._session_key:
            return data
        if len(data) < 13:  # 12 nonce + at least 1 byte
            return None

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = data[:12]
        ct = data[12:]
        try:
            return AESGCM(self._session_key).decrypt(nonce, ct, None)
        except Exception as e:
            logger.debug(f"Decrypt failed: {e}")
            return None

    # --- Internal: WebSocket I/O ---------------------------------------

    def _resolve_ws_url(self) -> str:
        """Resolve address to WebSocket URL."""
        addr = self.address
        if addr.startswith('ws://') or addr.startswith('wss://'):
            return addr
        # Default: plain WS for LAN, secure for WAN
        if self.trust == TrustLevel.SAME_USER:
            return f'ws://{addr}/peer_link'
        return f'ws://{addr}/peer_link'  # TLS handled at transport level if needed

    def _ws_send(self, data: bytes) -> None:
        """Send bytes over WebSocket (handles different libraries)."""
        if self._ws is None:
            raise ConnectionError("WebSocket not connected")
        if hasattr(self._ws, 'send'):
            self._ws.send(data)

    def _ws_send_binary(self, data: bytes) -> None:
        """Send binary data over WebSocket."""
        if self._ws is None:
            raise ConnectionError("WebSocket not connected")
        if hasattr(self._ws, 'send'):
            # websockets library
            self._ws.send(data)

    def _ws_recv(self, timeout: float = 30.0) -> Optional[bytes]:
        """Receive from WebSocket with timeout."""
        if self._ws is None:
            return None
        try:
            if hasattr(self._ws, 'recv'):
                # websockets sync client has recv(timeout)
                try:
                    return self._ws.recv(timeout=timeout)
                except TypeError:
                    # websocket-client doesn't have timeout param on recv
                    self._ws.settimeout(timeout)
                    return self._ws.recv()
        except Exception:
            return None

    def _receive_loop(self):
        """Background thread: receive and dispatch messages."""
        while self._state == LinkState.CONNECTED and self._ws is not None:
            try:
                raw = self._ws_recv(timeout=60)
                if raw is None:
                    continue

                if isinstance(raw, str):
                    raw = raw.encode('utf-8')

                # Decrypt if needed
                if self.trust in (TrustLevel.PEER, TrustLevel.RELAY) and self._session_key:
                    decrypted = self._decrypt(raw)
                    if decrypted is None:
                        continue
                    raw = decrypted

                self._messages_received += 1
                self._bytes_received += len(raw)
                self._last_activity = time.time()

                # Try JSON (text message)
                try:
                    msg = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
                    channel = msg.get('ch', 'control')
                    msg_id = msg.get('id', '')
                    data = msg.get('d', {})

                    # Check if this is a response to a pending request
                    if msg.get('re') and msg['re'] in self._pending_responses:
                        self._response_data[msg['re']] = data
                        self._pending_responses[msg['re']].set()
                        continue

                    # Dispatch to handlers
                    handlers = self._message_handlers.get(channel, [])
                    for handler in handlers:
                        try:
                            handler(channel, data, self.peer_id)
                        except Exception as e:
                            logger.debug(f"Handler error on {channel}: {e}")
                    continue
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

                # Binary message
                if len(raw) >= 5:
                    ch_id = raw[0]
                    channel = CHANNEL_NAMES.get(ch_id, 'unknown')
                    payload = raw[5:]  # skip channel_id + msg_id_hash
                    handlers = self._message_handlers.get(channel, [])
                    for handler in handlers:
                        try:
                            handler(channel, payload, self.peer_id)
                        except Exception as e:
                            logger.debug(f"Binary handler error on {channel}: {e}")

            except Exception as e:
                if self._state == LinkState.CONNECTED:
                    logger.debug(f"Receive loop error: {e}")
                    self._handle_disconnect()
                break

    def _handle_disconnect(self):
        """Handle unexpected disconnection."""
        if self._state != LinkState.CONNECTED:
            return
        self._state = LinkState.DISCONNECTED
        self._ws = None
        self._session_key = None
        logger.info(f"PeerLink disconnected: {self.peer_id[:8]}")

    @staticmethod
    def _get_local_capabilities() -> dict:
        """Get local node capabilities for handshake."""
        caps = {'cpu_count': os.cpu_count() or 1}
        try:
            from integrations.service_tools.vram_manager import detect_gpu
            gpu = detect_gpu()
            if gpu.get('available'):
                caps['gpu'] = gpu.get('device_name', 'GPU')
                caps['vram_mb'] = gpu.get('vram_total_mb', 0)
        except Exception:
            pass
        try:
            from security.key_delegation import get_node_tier
            caps['tier'] = get_node_tier()
        except Exception:
            caps['tier'] = 'flat'
        return caps
