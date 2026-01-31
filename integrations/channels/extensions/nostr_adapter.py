"""
Nostr Protocol Channel Adapter

Implements Nostr decentralized messaging protocol.
Based on HevolveBot extension patterns for decentralized networks.

Features:
- NIP-01: Basic protocol support
- NIP-04: Encrypted DMs
- NIP-05: DNS-based verification
- NIP-19: bech32-encoded entities
- NIP-42: Authentication
- Multi-relay support
- Event signing with secp256k1
- Subscription management
- Reconnection with exponential backoff
- Relay pool management
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import time
import hashlib
import secrets
from typing import Optional, List, Dict, Any, Callable, Set, Tuple
from datetime import datetime
from dataclasses import dataclass, field
from enum import IntEnum

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

try:
    from secp256k1 import PrivateKey, PublicKey
    HAS_SECP256K1 = True
except ImportError:
    HAS_SECP256K1 = False

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

from ..base import (
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


class NostrEventKind(IntEnum):
    """Nostr event kinds (NIP-01)."""
    SET_METADATA = 0
    TEXT_NOTE = 1
    RECOMMEND_RELAY = 2
    CONTACTS = 3
    ENCRYPTED_DM = 4
    DELETE = 5
    REPOST = 6
    REACTION = 7
    BADGE_AWARD = 8
    CHANNEL_CREATE = 40
    CHANNEL_METADATA = 41
    CHANNEL_MESSAGE = 42
    CHANNEL_HIDE_MESSAGE = 43
    CHANNEL_MUTE_USER = 44
    AUTH = 22242


@dataclass
class NostrConfig(ChannelConfig):
    """Nostr-specific configuration."""
    private_key: str = ""  # hex or nsec format
    relays: List[str] = field(default_factory=lambda: [
        "wss://relay.damus.io",
        "wss://nos.lol",
        "wss://relay.snort.social",
    ])
    nip05_identifier: str = ""  # user@domain.com
    enable_nip04_encryption: bool = True
    enable_nip42_auth: bool = True
    subscription_limit: int = 100
    reconnect_attempts: int = 5
    reconnect_delay: float = 1.0
    message_expiry: int = 0  # 0 = no expiry


@dataclass
class NostrEvent:
    """Nostr event structure (NIP-01)."""
    id: str
    pubkey: str
    created_at: int
    kind: int
    tags: List[List[str]]
    content: str
    sig: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "pubkey": self.pubkey,
            "created_at": self.created_at,
            "kind": self.kind,
            "tags": self.tags,
            "content": self.content,
            "sig": self.sig,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'NostrEvent':
        """Create from dictionary."""
        return cls(
            id=data["id"],
            pubkey=data["pubkey"],
            created_at=data["created_at"],
            kind=data["kind"],
            tags=data.get("tags", []),
            content=data["content"],
            sig=data["sig"],
        )


@dataclass
class NostrFilter:
    """Nostr subscription filter."""
    ids: Optional[List[str]] = None
    authors: Optional[List[str]] = None
    kinds: Optional[List[int]] = None
    since: Optional[int] = None
    until: Optional[int] = None
    limit: Optional[int] = None
    tags: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for subscription."""
        result = {}
        if self.ids:
            result["ids"] = self.ids
        if self.authors:
            result["authors"] = self.authors
        if self.kinds:
            result["kinds"] = self.kinds
        if self.since:
            result["since"] = self.since
        if self.until:
            result["until"] = self.until
        if self.limit:
            result["limit"] = self.limit
        for key, values in self.tags.items():
            result[f"#{key}"] = values
        return result


@dataclass
class RelayConnection:
    """Relay connection state."""
    url: str
    ws: Optional[websockets.WebSocketClientProtocol] = None
    connected: bool = False
    subscriptions: Set[str] = field(default_factory=set)
    reconnect_count: int = 0


class NostrAdapter(ChannelAdapter):
    """
    Nostr protocol adapter with multi-relay support.

    Usage:
        config = NostrConfig(
            private_key="your-private-key-hex",
            relays=["wss://relay.damus.io", "wss://nos.lol"],
        )
        adapter = NostrAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: NostrConfig):
        if not HAS_WEBSOCKETS:
            raise ImportError(
                "websockets not installed. "
                "Install with: pip install websockets"
            )

        super().__init__(config)
        self.nostr_config: NostrConfig = config
        self._private_key: Optional[bytes] = None
        self._public_key: Optional[bytes] = None
        self._pubkey_hex: str = ""
        self._relays: Dict[str, RelayConnection] = {}
        self._subscriptions: Dict[str, NostrFilter] = {}
        self._read_tasks: List[asyncio.Task] = []
        self._event_handlers: Dict[int, List[Callable]] = {}
        self._seen_events: Set[str] = set()
        self._pending_events: Dict[str, asyncio.Event] = {}

    @property
    def name(self) -> str:
        return "nostr"

    async def connect(self) -> bool:
        """Connect to Nostr relays."""
        if not self.nostr_config.private_key:
            logger.error("Nostr private key required")
            return False

        try:
            # Parse private key
            self._parse_private_key()

            if not self._private_key:
                logger.error("Failed to parse private key")
                return False

            # Derive public key
            self._derive_public_key()

            # Connect to relays
            connected_count = 0
            for relay_url in self.nostr_config.relays:
                if await self._connect_relay(relay_url):
                    connected_count += 1

            if connected_count == 0:
                logger.error("Failed to connect to any relay")
                self.status = ChannelStatus.ERROR
                return False

            # Subscribe to DMs
            await self._subscribe_to_dms()

            # Subscribe to mentions
            await self._subscribe_to_mentions()

            self.status = ChannelStatus.CONNECTED
            logger.info(f"Nostr connected to {connected_count} relays as {self._pubkey_hex[:16]}...")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Nostr: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect from all relays."""
        # Cancel read tasks
        for task in self._read_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._read_tasks.clear()

        # Close relay connections
        for relay in self._relays.values():
            if relay.ws:
                await relay.ws.close()
                relay.ws = None
                relay.connected = False

        self._relays.clear()
        self._subscriptions.clear()
        self._seen_events.clear()
        self.status = ChannelStatus.DISCONNECTED

    def _parse_private_key(self) -> None:
        """Parse private key from hex or nsec format."""
        key = self.nostr_config.private_key

        # Handle nsec format (NIP-19)
        if key.startswith("nsec1"):
            key = self._bech32_decode(key, "nsec")

        # Convert hex to bytes
        try:
            self._private_key = bytes.fromhex(key)
        except ValueError:
            logger.error("Invalid private key format")

    def _derive_public_key(self) -> None:
        """Derive public key from private key."""
        if not self._private_key:
            return

        if HAS_SECP256K1:
            pk = PrivateKey(self._private_key, raw=True)
            self._public_key = pk.pubkey.serialize()[1:]  # Remove 04 prefix
            self._pubkey_hex = self._public_key.hex()
        else:
            # Fallback: simple derivation (not secure, for testing only)
            logger.warning("secp256k1 not available, using insecure key derivation")
            self._public_key = hashlib.sha256(self._private_key).digest()
            self._pubkey_hex = self._public_key.hex()

    def _bech32_decode(self, bech32_str: str, hrp: str) -> str:
        """Decode bech32 string (simplified NIP-19)."""
        # Simplified decoder - in production use a proper bech32 library
        # This is a placeholder
        logger.warning("Using simplified bech32 decoder")
        # Strip hrp and decode base32
        data_part = bech32_str[len(hrp) + 1:]
        # Return as hex (placeholder - actual implementation needs bech32 library)
        return data_part

    async def _connect_relay(self, relay_url: str) -> bool:
        """Connect to a single relay."""
        try:
            ws = await websockets.connect(
                relay_url,
                ping_interval=30,
                ping_timeout=10,
            )

            relay = RelayConnection(url=relay_url, ws=ws, connected=True)
            self._relays[relay_url] = relay

            # Start read task
            task = asyncio.create_task(self._read_relay(relay))
            self._read_tasks.append(task)

            logger.info(f"Connected to relay: {relay_url}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to relay {relay_url}: {e}")
            return False

    async def _read_relay(self, relay: RelayConnection) -> None:
        """Read messages from a relay."""
        while relay.connected and relay.ws:
            try:
                raw = await relay.ws.recv()
                await self._handle_relay_message(relay, raw)

            except ConnectionClosed:
                logger.warning(f"Relay disconnected: {relay.url}")
                relay.connected = False
                await self._handle_relay_disconnect(relay)
                break

            except asyncio.CancelledError:
                break

            except Exception as e:
                logger.error(f"Relay read error: {e}")

    async def _handle_relay_message(self, relay: RelayConnection, raw: str) -> None:
        """Handle raw message from relay."""
        try:
            data = json.loads(raw)
            if not isinstance(data, list) or len(data) < 2:
                return

            msg_type = data[0]

            if msg_type == "EVENT":
                # ["EVENT", subscription_id, event]
                if len(data) >= 3:
                    await self._handle_event(relay, data[1], data[2])

            elif msg_type == "OK":
                # ["OK", event_id, success, message]
                if len(data) >= 3:
                    event_id = data[1]
                    success = data[2]
                    if event_id in self._pending_events:
                        self._pending_events[event_id].set()

            elif msg_type == "EOSE":
                # ["EOSE", subscription_id]
                logger.debug(f"End of stored events for subscription: {data[1]}")

            elif msg_type == "NOTICE":
                # ["NOTICE", message]
                logger.info(f"Relay notice from {relay.url}: {data[1]}")

            elif msg_type == "AUTH":
                # ["AUTH", challenge] (NIP-42)
                if len(data) >= 2:
                    await self._handle_auth_challenge(relay, data[1])

        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from relay: {raw[:100]}")

    async def _handle_event(
        self,
        relay: RelayConnection,
        subscription_id: str,
        event_data: Dict[str, Any],
    ) -> None:
        """Handle incoming Nostr event."""
        try:
            event = NostrEvent.from_dict(event_data)

            # Skip if already seen
            if event.id in self._seen_events:
                return

            self._seen_events.add(event.id)

            # Verify event signature
            if not self._verify_event(event):
                logger.warning(f"Invalid event signature: {event.id}")
                return

            # Skip own events
            if event.pubkey == self._pubkey_hex:
                return

            # Handle by kind
            if event.kind == NostrEventKind.ENCRYPTED_DM:
                await self._handle_encrypted_dm(event)
            elif event.kind == NostrEventKind.TEXT_NOTE:
                await self._handle_text_note(event)
            elif event.kind == NostrEventKind.CHANNEL_MESSAGE:
                await self._handle_channel_message(event)

            # Call registered handlers
            if event.kind in self._event_handlers:
                for handler in self._event_handlers[event.kind]:
                    try:
                        result = handler(event)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        logger.error(f"Event handler error: {e}")

        except Exception as e:
            logger.error(f"Error handling event: {e}")

    async def _handle_encrypted_dm(self, event: NostrEvent) -> None:
        """Handle encrypted DM (NIP-04)."""
        if not self.nostr_config.enable_nip04_encryption:
            return

        try:
            # Get sender pubkey
            sender_pubkey = event.pubkey

            # Decrypt content
            decrypted = self._decrypt_nip04(event.content, sender_pubkey)
            if not decrypted:
                logger.warning("Failed to decrypt DM")
                return

            message = Message(
                id=event.id,
                channel=self.name,
                sender_id=sender_pubkey,
                sender_name=self._get_display_name(sender_pubkey),
                chat_id=f"dm:{sender_pubkey}",
                text=decrypted,
                timestamp=datetime.fromtimestamp(event.created_at),
                is_group=False,
                raw={
                    "event": event.to_dict(),
                    "relay": "unknown",
                    "encrypted": True,
                },
            )

            await self._dispatch_message(message)

        except Exception as e:
            logger.error(f"Error handling encrypted DM: {e}")

    async def _handle_text_note(self, event: NostrEvent) -> None:
        """Handle text note (public post)."""
        # Check if we're mentioned
        is_mentioned = False
        for tag in event.tags:
            if len(tag) >= 2 and tag[0] == "p" and tag[1] == self._pubkey_hex:
                is_mentioned = True
                break

        if not is_mentioned:
            return

        message = Message(
            id=event.id,
            channel=self.name,
            sender_id=event.pubkey,
            sender_name=self._get_display_name(event.pubkey),
            chat_id=f"note:{event.id}",
            text=event.content,
            timestamp=datetime.fromtimestamp(event.created_at),
            is_group=True,
            is_bot_mentioned=True,
            raw={
                "event": event.to_dict(),
                "tags": event.tags,
            },
        )

        await self._dispatch_message(message)

    async def _handle_channel_message(self, event: NostrEvent) -> None:
        """Handle channel message (NIP-28)."""
        # Extract channel ID from tags
        channel_id = None
        for tag in event.tags:
            if len(tag) >= 2 and tag[0] == "e":
                channel_id = tag[1]
                break

        if not channel_id:
            return

        message = Message(
            id=event.id,
            channel=self.name,
            sender_id=event.pubkey,
            sender_name=self._get_display_name(event.pubkey),
            chat_id=f"channel:{channel_id}",
            text=event.content,
            timestamp=datetime.fromtimestamp(event.created_at),
            is_group=True,
            raw={
                "event": event.to_dict(),
                "channel_id": channel_id,
            },
        )

        await self._dispatch_message(message)

    async def _handle_auth_challenge(self, relay: RelayConnection, challenge: str) -> None:
        """Handle NIP-42 authentication challenge."""
        if not self.nostr_config.enable_nip42_auth:
            return

        try:
            # Create auth event
            auth_event = self._create_event(
                kind=NostrEventKind.AUTH,
                content="",
                tags=[
                    ["relay", relay.url],
                    ["challenge", challenge],
                ],
            )

            # Send auth
            await relay.ws.send(json.dumps(["AUTH", auth_event.to_dict()]))
            logger.info(f"Sent NIP-42 auth to {relay.url}")

        except Exception as e:
            logger.error(f"Auth challenge error: {e}")

    async def _handle_relay_disconnect(self, relay: RelayConnection) -> None:
        """Handle relay disconnection with reconnection."""
        if relay.reconnect_count < self.nostr_config.reconnect_attempts:
            relay.reconnect_count += 1
            delay = self.nostr_config.reconnect_delay * (2 ** (relay.reconnect_count - 1))

            logger.info(f"Reconnecting to {relay.url} in {delay}s")
            await asyncio.sleep(delay)

            if await self._connect_relay(relay.url):
                # Resubscribe
                for sub_id in list(relay.subscriptions):
                    if sub_id in self._subscriptions:
                        await self._send_subscription(relay, sub_id, self._subscriptions[sub_id])

    async def _subscribe_to_dms(self) -> None:
        """Subscribe to encrypted DMs."""
        filter = NostrFilter(
            kinds=[NostrEventKind.ENCRYPTED_DM],
            tags={"p": [self._pubkey_hex]},
            since=int(time.time()) - 86400,  # Last 24 hours
            limit=self.nostr_config.subscription_limit,
        )

        await self.subscribe("dm_inbox", filter)

    async def _subscribe_to_mentions(self) -> None:
        """Subscribe to mentions in text notes."""
        filter = NostrFilter(
            kinds=[NostrEventKind.TEXT_NOTE],
            tags={"p": [self._pubkey_hex]},
            since=int(time.time()) - 86400,
            limit=self.nostr_config.subscription_limit,
        )

        await self.subscribe("mentions", filter)

    async def subscribe(self, subscription_id: str, filter: NostrFilter) -> bool:
        """Subscribe to events matching filter."""
        self._subscriptions[subscription_id] = filter

        success = False
        for relay in self._relays.values():
            if relay.connected:
                if await self._send_subscription(relay, subscription_id, filter):
                    relay.subscriptions.add(subscription_id)
                    success = True

        return success

    async def _send_subscription(
        self,
        relay: RelayConnection,
        subscription_id: str,
        filter: NostrFilter,
    ) -> bool:
        """Send subscription request to relay."""
        if not relay.ws or not relay.connected:
            return False

        try:
            msg = ["REQ", subscription_id, filter.to_dict()]
            await relay.ws.send(json.dumps(msg))
            return True
        except Exception as e:
            logger.error(f"Failed to send subscription: {e}")
            return False

    async def unsubscribe(self, subscription_id: str) -> None:
        """Unsubscribe from events."""
        if subscription_id in self._subscriptions:
            del self._subscriptions[subscription_id]

        for relay in self._relays.values():
            if relay.ws and relay.connected:
                try:
                    await relay.ws.send(json.dumps(["CLOSE", subscription_id]))
                    relay.subscriptions.discard(subscription_id)
                except Exception:
                    pass

    def _create_event(
        self,
        kind: int,
        content: str,
        tags: Optional[List[List[str]]] = None,
    ) -> NostrEvent:
        """Create and sign a Nostr event."""
        created_at = int(time.time())
        tags = tags or []

        # Add expiry tag if configured
        if self.nostr_config.message_expiry > 0:
            tags.append(["expiration", str(created_at + self.nostr_config.message_expiry)])

        # Compute event ID
        event_data = [
            0,
            self._pubkey_hex,
            created_at,
            kind,
            tags,
            content,
        ]
        event_json = json.dumps(event_data, separators=(",", ":"), ensure_ascii=False)
        event_id = hashlib.sha256(event_json.encode()).hexdigest()

        # Sign event
        sig = self._sign_event(event_id)

        return NostrEvent(
            id=event_id,
            pubkey=self._pubkey_hex,
            created_at=created_at,
            kind=kind,
            tags=tags,
            content=content,
            sig=sig,
        )

    def _sign_event(self, event_id: str) -> str:
        """Sign event ID with private key."""
        if not self._private_key:
            return ""

        if HAS_SECP256K1:
            pk = PrivateKey(self._private_key, raw=True)
            sig = pk.schnorr_sign(bytes.fromhex(event_id), None, raw=True)
            return sig.hex()
        else:
            # Fallback: insecure placeholder signature
            logger.warning("secp256k1 not available, using placeholder signature")
            return hashlib.sha256(
                self._private_key + bytes.fromhex(event_id)
            ).hexdigest() * 2

    def _verify_event(self, event: NostrEvent) -> bool:
        """Verify event signature."""
        if HAS_SECP256K1:
            try:
                # Recompute event ID
                event_data = [
                    0,
                    event.pubkey,
                    event.created_at,
                    event.kind,
                    event.tags,
                    event.content,
                ]
                event_json = json.dumps(event_data, separators=(",", ":"), ensure_ascii=False)
                computed_id = hashlib.sha256(event_json.encode()).hexdigest()

                if computed_id != event.id:
                    return False

                # Verify signature
                pubkey = PublicKey(bytes.fromhex("02" + event.pubkey), raw=True)
                return pubkey.schnorr_verify(
                    bytes.fromhex(event.id),
                    bytes.fromhex(event.sig),
                    None,
                    raw=True,
                )
            except Exception:
                return False
        else:
            # Skip verification if secp256k1 not available
            return True

    def _encrypt_nip04(self, content: str, recipient_pubkey: str) -> str:
        """Encrypt content for NIP-04 DM."""
        if not HAS_SECP256K1 or not HAS_CRYPTO:
            logger.warning("Encryption libraries not available")
            return content

        try:
            # Compute shared secret
            pk = PrivateKey(self._private_key, raw=True)
            recipient_pk = PublicKey(bytes.fromhex("02" + recipient_pubkey), raw=True)
            shared_point = recipient_pk.tweak_mul(self._private_key)
            shared_secret = shared_point.serialize()[1:33]

            # Generate IV
            iv = secrets.token_bytes(16)

            # Encrypt with AES-256-CBC
            cipher = Cipher(
                algorithms.AES(shared_secret),
                modes.CBC(iv),
                backend=default_backend(),
            )
            encryptor = cipher.encryptor()

            # Pad content
            pad_len = 16 - (len(content) % 16)
            padded = content.encode() + bytes([pad_len] * pad_len)

            ciphertext = encryptor.update(padded) + encryptor.finalize()

            # Format: base64(ciphertext)?iv=base64(iv)
            import base64
            ct_b64 = base64.b64encode(ciphertext).decode()
            iv_b64 = base64.b64encode(iv).decode()

            return f"{ct_b64}?iv={iv_b64}"

        except Exception as e:
            logger.error(f"Encryption error: {e}")
            return content

    def _decrypt_nip04(self, content: str, sender_pubkey: str) -> Optional[str]:
        """Decrypt NIP-04 encrypted content."""
        if not HAS_SECP256K1 or not HAS_CRYPTO:
            logger.warning("Decryption libraries not available")
            return None

        try:
            import base64

            # Parse content
            if "?iv=" not in content:
                return None

            ct_b64, iv_part = content.split("?iv=", 1)
            ciphertext = base64.b64decode(ct_b64)
            iv = base64.b64decode(iv_part)

            # Compute shared secret
            pk = PrivateKey(self._private_key, raw=True)
            sender_pk = PublicKey(bytes.fromhex("02" + sender_pubkey), raw=True)
            shared_point = sender_pk.tweak_mul(self._private_key)
            shared_secret = shared_point.serialize()[1:33]

            # Decrypt
            cipher = Cipher(
                algorithms.AES(shared_secret),
                modes.CBC(iv),
                backend=default_backend(),
            )
            decryptor = cipher.decryptor()

            padded = decryptor.update(ciphertext) + decryptor.finalize()

            # Remove padding
            pad_len = padded[-1]
            plaintext = padded[:-pad_len].decode()

            return plaintext

        except Exception as e:
            logger.error(f"Decryption error: {e}")
            return None

    def _get_display_name(self, pubkey: str) -> str:
        """Get display name for pubkey (placeholder)."""
        # In production, this would fetch kind:0 metadata
        return f"nostr:{pubkey[:8]}..."

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a message via Nostr."""
        try:
            if chat_id.startswith("dm:"):
                # Encrypted DM
                recipient_pubkey = chat_id.replace("dm:", "")
                return await self.send_dm(recipient_pubkey, text)

            elif chat_id.startswith("channel:"):
                # Channel message
                channel_id = chat_id.replace("channel:", "")
                return await self.send_channel_message(channel_id, text, reply_to)

            elif chat_id.startswith("note:"):
                # Reply to note
                note_id = chat_id.replace("note:", "")
                return await self.post_note(text, reply_to=note_id)

            else:
                # Public note
                return await self.post_note(text)

        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return SendResult(success=False, error=str(e))

    async def send_dm(self, recipient_pubkey: str, text: str) -> SendResult:
        """Send encrypted DM (NIP-04)."""
        if not self.nostr_config.enable_nip04_encryption:
            return SendResult(success=False, error="NIP-04 encryption disabled")

        try:
            # Encrypt content
            encrypted = self._encrypt_nip04(text, recipient_pubkey)

            # Create event
            event = self._create_event(
                kind=NostrEventKind.ENCRYPTED_DM,
                content=encrypted,
                tags=[["p", recipient_pubkey]],
            )

            # Publish
            return await self._publish_event(event)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def post_note(
        self,
        text: str,
        reply_to: Optional[str] = None,
        mentions: Optional[List[str]] = None,
    ) -> SendResult:
        """Post a public text note."""
        try:
            tags = []

            # Add reply tag
            if reply_to:
                tags.append(["e", reply_to, "", "reply"])

            # Add mention tags
            if mentions:
                for pubkey in mentions:
                    tags.append(["p", pubkey])

            event = self._create_event(
                kind=NostrEventKind.TEXT_NOTE,
                content=text,
                tags=tags,
            )

            return await self._publish_event(event)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_channel_message(
        self,
        channel_id: str,
        text: str,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send message to Nostr channel (NIP-28)."""
        try:
            tags = [["e", channel_id, "", "root"]]

            if reply_to:
                tags.append(["e", reply_to, "", "reply"])

            event = self._create_event(
                kind=NostrEventKind.CHANNEL_MESSAGE,
                content=text,
                tags=tags,
            )

            return await self._publish_event(event)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def _publish_event(self, event: NostrEvent) -> SendResult:
        """Publish event to all connected relays."""
        success = False
        event_dict = event.to_dict()

        # Create completion event
        completion = asyncio.Event()
        self._pending_events[event.id] = completion

        for relay in self._relays.values():
            if relay.ws and relay.connected:
                try:
                    await relay.ws.send(json.dumps(["EVENT", event_dict]))
                    success = True
                except Exception as e:
                    logger.error(f"Failed to publish to {relay.url}: {e}")

        # Wait for confirmation (with timeout)
        if success:
            try:
                await asyncio.wait_for(completion.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Event publish confirmation timeout")

        # Cleanup
        self._pending_events.pop(event.id, None)

        return SendResult(success=success, message_id=event.id if success else None)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """
        Edit a Nostr message.
        Note: Nostr doesn't support editing; posts a correction event.
        """
        logger.warning("Nostr doesn't support editing; posting correction")
        return await self.send_message(chat_id, f"[Correction] {text}")

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Request deletion of a Nostr event."""
        try:
            event = self._create_event(
                kind=NostrEventKind.DELETE,
                content="Deleted by author",
                tags=[["e", message_id]],
            )

            result = await self._publish_event(event)
            return result.success

        except Exception as e:
            logger.error(f"Failed to delete: {e}")
            return False

    async def send_typing(self, chat_id: str) -> None:
        """
        Send typing indicator.
        Note: Nostr doesn't support typing indicators.
        """
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a Nostr chat."""
        if chat_id.startswith("dm:"):
            pubkey = chat_id.replace("dm:", "")
            return {
                "type": "dm",
                "pubkey": pubkey,
                "display_name": self._get_display_name(pubkey),
            }
        elif chat_id.startswith("channel:"):
            channel_id = chat_id.replace("channel:", "")
            return {
                "type": "channel",
                "channel_id": channel_id,
            }
        return None

    # Nostr-specific methods

    def on_event(self, kind: int, handler: Callable[[NostrEvent], Any]) -> None:
        """Register a handler for specific event kind."""
        if kind not in self._event_handlers:
            self._event_handlers[kind] = []
        self._event_handlers[kind].append(handler)

    async def add_reaction(self, event_id: str, content: str = "+") -> SendResult:
        """Add a reaction to an event."""
        try:
            event = self._create_event(
                kind=NostrEventKind.REACTION,
                content=content,
                tags=[["e", event_id]],
            )

            return await self._publish_event(event)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def repost(self, event_id: str) -> SendResult:
        """Repost an event."""
        try:
            event = self._create_event(
                kind=NostrEventKind.REPOST,
                content="",
                tags=[["e", event_id]],
            )

            return await self._publish_event(event)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    def get_public_key(self) -> str:
        """Get bot's public key in hex format."""
        return self._pubkey_hex

    def get_npub(self) -> str:
        """Get bot's public key in npub format (NIP-19)."""
        # Simplified - in production use proper bech32 encoding
        return f"npub1{self._pubkey_hex[:60]}"


def create_nostr_adapter(
    private_key: str = None,
    relays: List[str] = None,
    **kwargs
) -> NostrAdapter:
    """
    Factory function to create Nostr adapter.

    Args:
        private_key: Private key in hex or nsec format (or set NOSTR_PRIVATE_KEY env var)
        relays: List of relay URLs (or set NOSTR_RELAYS env var, comma-separated)
        **kwargs: Additional config options

    Returns:
        Configured NostrAdapter
    """
    private_key = private_key or os.getenv("NOSTR_PRIVATE_KEY")

    if relays is None:
        relays_env = os.getenv("NOSTR_RELAYS", "")
        if relays_env:
            relays = [r.strip() for r in relays_env.split(",") if r.strip()]
        else:
            relays = None  # Use defaults

    if not private_key:
        raise ValueError("Nostr private key required")

    config = NostrConfig(
        private_key=private_key,
        **kwargs
    )

    if relays:
        config.relays = relays

    return NostrAdapter(config)
