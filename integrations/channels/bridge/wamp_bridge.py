"""
WAMP Bridge for Channel Chaining

Integrates HevolveBot channel adapters with Crossbar WAMP for:
- Cross-channel message forwarding
- Unified inbox across channels
- Channel-to-channel relay rules
- Real-time message routing via pub/sub

Uses existing Crossbar infrastructure:
- com.hertzai.hevolve.channel.* topics for channel events
- com.hertzai.hevolve.bridge.* topics for bridge control
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set
from functools import partial

try:
    from autobahn.asyncio.component import Component
    from autobahn.wamp.exception import ApplicationError
    HAS_AUTOBAHN = True
except ImportError:
    HAS_AUTOBAHN = False

from ..base import Message, SendResult
from ..registry import ChannelRegistry

logger = logging.getLogger(__name__)


class RouteType(Enum):
    """Types of message routing."""
    FORWARD = "forward"      # Forward message to another channel
    MIRROR = "mirror"        # Mirror to multiple channels
    BROADCAST = "broadcast"  # Broadcast to all channels
    FILTER = "filter"        # Forward only matching messages
    TRANSFORM = "transform"  # Transform before forwarding


@dataclass
class BridgeRule:
    """Rule for routing messages between channels."""
    id: str
    name: str
    source_channel: str           # Source channel type (telegram, discord, etc.)
    source_chat_id: Optional[str] = None  # Specific chat or None for all
    target_channel: str = ""      # Target channel type
    target_chat_id: Optional[str] = None  # Target chat ID
    route_type: RouteType = RouteType.FORWARD
    enabled: bool = True

    # Filtering options
    filter_pattern: Optional[str] = None  # Regex pattern to match
    filter_sender: Optional[str] = None   # Specific sender ID
    filter_keywords: List[str] = field(default_factory=list)

    # Transform options
    prefix: str = ""              # Prefix to add to forwarded messages
    suffix: str = ""              # Suffix to add
    include_source_info: bool = True  # Include [From: channel/user] header
    strip_mentions: bool = False  # Remove @mentions before forwarding

    # Rate limiting
    rate_limit: int = 0           # Max messages per minute (0 = unlimited)
    cooldown_seconds: int = 0     # Cooldown between forwards

    # Metadata
    created_at: datetime = field(default_factory=datetime.now)
    last_triggered: Optional[datetime] = None
    trigger_count: int = 0


@dataclass
class BridgeConfig:
    """Configuration for channel bridge."""
    # WAMP connection
    crossbar_url: str = ""
    realm: str = "realm1"

    # Topic prefixes
    channel_topic_prefix: str = "com.hertzai.hevolve.channel"
    bridge_topic_prefix: str = "com.hertzai.hevolve.bridge"

    # Behavior
    enable_broadcast: bool = True
    enable_unified_inbox: bool = True
    max_forward_chain: int = 3     # Prevent infinite loops
    forward_timeout: float = 10.0  # Timeout for forward operations

    # Persistence
    rules_file: str = "/app/data/bridge_rules.json"

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        """Create config from environment variables."""
        return cls(
            crossbar_url=os.getenv("CBURL", "ws://localhost:8088/ws"),
            realm=os.getenv("CBREALM", "realm1"),
            rules_file=os.getenv("BRIDGE_RULES_FILE", "/app/data/bridge_rules.json"),
        )


class ChannelBridge:
    """
    Bridge for routing messages between channels via WAMP Crossbar.

    Enables:
    - Forward messages from one channel to another
    - Mirror messages to multiple channels
    - Broadcast to all channels
    - Filter-based routing
    - Transform messages during forwarding

    Example:
        bridge = ChannelBridge(config, registry)
        await bridge.connect()

        # Add a rule to forward Telegram messages to Discord
        bridge.add_rule(BridgeRule(
            id="tg-to-discord",
            name="Telegram to Discord",
            source_channel="telegram",
            target_channel="discord",
            target_chat_id="123456789",
            include_source_info=True,
        ))
    """

    def __init__(
        self,
        config: BridgeConfig,
        registry: ChannelRegistry,
    ):
        self.config = config
        self.registry = registry
        self._rules: Dict[str, BridgeRule] = {}
        self._component: Optional[Component] = None
        self._session = None
        self._connected = False
        self._forward_chain: Dict[str, int] = {}  # Track forward depth
        self._rate_limiters: Dict[str, List[datetime]] = {}
        self._handlers: Dict[str, Callable] = {}

        # Load persisted rules
        self._load_rules()

    def _load_rules(self) -> None:
        """Load rules from persistence file."""
        try:
            if os.path.exists(self.config.rules_file):
                with open(self.config.rules_file, "r") as f:
                    data = json.load(f)
                    for rule_data in data.get("rules", []):
                        rule = BridgeRule(
                            id=rule_data["id"],
                            name=rule_data["name"],
                            source_channel=rule_data["source_channel"],
                            source_chat_id=rule_data.get("source_chat_id"),
                            target_channel=rule_data["target_channel"],
                            target_chat_id=rule_data.get("target_chat_id"),
                            route_type=RouteType(rule_data.get("route_type", "forward")),
                            enabled=rule_data.get("enabled", True),
                            prefix=rule_data.get("prefix", ""),
                            suffix=rule_data.get("suffix", ""),
                            include_source_info=rule_data.get("include_source_info", True),
                        )
                        self._rules[rule.id] = rule
                logger.info(f"Loaded {len(self._rules)} bridge rules")
        except Exception as e:
            logger.warning(f"Could not load bridge rules: {e}")

    def _save_rules(self) -> None:
        """Save rules to persistence file."""
        try:
            os.makedirs(os.path.dirname(self.config.rules_file), exist_ok=True)
            data = {
                "rules": [
                    {
                        "id": r.id,
                        "name": r.name,
                        "source_channel": r.source_channel,
                        "source_chat_id": r.source_chat_id,
                        "target_channel": r.target_channel,
                        "target_chat_id": r.target_chat_id,
                        "route_type": r.route_type.value,
                        "enabled": r.enabled,
                        "prefix": r.prefix,
                        "suffix": r.suffix,
                        "include_source_info": r.include_source_info,
                    }
                    for r in self._rules.values()
                ]
            }
            with open(self.config.rules_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save bridge rules: {e}")

    async def connect(self) -> bool:
        """Connect to Crossbar WAMP router."""
        if not HAS_AUTOBAHN:
            logger.error("autobahn not installed, cannot connect to Crossbar")
            return False

        try:
            self._component = Component(
                transports=self.config.crossbar_url,
                realm=self.config.realm,
            )

            @self._component.on_join
            async def on_join(session, details):
                self._session = session
                self._connected = True
                logger.info("Channel bridge connected to Crossbar")

                # Subscribe to channel events
                await self._setup_subscriptions()

                # Register RPC methods
                await self._register_rpcs()

            @self._component.on_leave
            async def on_leave(session, details):
                self._connected = False
                logger.info("Channel bridge disconnected from Crossbar")

            # Start component in background
            asyncio.create_task(self._run_component())

            # Wait for connection
            for _ in range(50):  # 5 seconds timeout
                if self._connected:
                    return True
                await asyncio.sleep(0.1)

            return False

        except Exception as e:
            logger.error(f"Failed to connect to Crossbar: {e}")
            return False

    async def _run_component(self) -> None:
        """Run the WAMP component."""
        try:
            from autobahn.asyncio.component import run
            await self._component.start()
        except Exception as e:
            logger.error(f"Component error: {e}")

    async def _setup_subscriptions(self) -> None:
        """Subscribe to channel message topics."""
        # Subscribe to all channel message events
        topic = f"{self.config.channel_topic_prefix}.message"
        await self._session.subscribe(self._on_channel_message, topic)
        logger.info(f"Subscribed to {topic}")

        # Subscribe to bridge control topic
        control_topic = f"{self.config.bridge_topic_prefix}.control"
        await self._session.subscribe(self._on_bridge_control, control_topic)
        logger.info(f"Subscribed to {control_topic}")

    async def _register_rpcs(self) -> None:
        """Register RPC methods for bridge control."""
        prefix = self.config.bridge_topic_prefix

        await self._session.register(
            self.add_rule_rpc,
            f"{prefix}.add_rule"
        )
        await self._session.register(
            self.remove_rule_rpc,
            f"{prefix}.remove_rule"
        )
        await self._session.register(
            self.list_rules_rpc,
            f"{prefix}.list_rules"
        )
        await self._session.register(
            self.forward_message_rpc,
            f"{prefix}.forward"
        )

        logger.info("Registered bridge RPC methods")

    async def _on_channel_message(self, message_data: Dict[str, Any]) -> None:
        """Handle incoming channel message event."""
        try:
            # Parse message
            channel = message_data.get("channel", "")
            chat_id = message_data.get("chat_id", "")
            message_id = message_data.get("message_id", "")

            # Check forward chain depth
            chain_key = f"{channel}:{chat_id}:{message_id}"
            depth = self._forward_chain.get(chain_key, 0)

            if depth >= self.config.max_forward_chain:
                logger.warning(f"Max forward chain reached for {chain_key}")
                return

            # Find matching rules
            for rule in self._rules.values():
                if not rule.enabled:
                    continue

                if rule.source_channel != channel:
                    continue

                if rule.source_chat_id and rule.source_chat_id != chat_id:
                    continue

                # Check rate limit
                if not self._check_rate_limit(rule):
                    continue

                # Execute forward
                self._forward_chain[chain_key] = depth + 1
                try:
                    await self._execute_forward(rule, message_data)
                finally:
                    del self._forward_chain[chain_key]

        except Exception as e:
            logger.error(f"Error handling channel message: {e}")

    async def _on_bridge_control(self, control_data: Dict[str, Any]) -> None:
        """Handle bridge control messages."""
        action = control_data.get("action", "")

        if action == "reload_rules":
            self._load_rules()
        elif action == "clear_rules":
            self._rules.clear()
            self._save_rules()

    def _check_rate_limit(self, rule: BridgeRule) -> bool:
        """Check if rule is within rate limit."""
        if rule.rate_limit <= 0:
            return True

        now = datetime.now()
        key = rule.id

        if key not in self._rate_limiters:
            self._rate_limiters[key] = []

        # Clean old entries
        window_start = now.timestamp() - 60
        self._rate_limiters[key] = [
            t for t in self._rate_limiters[key]
            if t.timestamp() > window_start
        ]

        # Check limit
        if len(self._rate_limiters[key]) >= rule.rate_limit:
            return False

        self._rate_limiters[key].append(now)
        return True

    async def _execute_forward(
        self,
        rule: BridgeRule,
        message_data: Dict[str, Any]
    ) -> Optional[SendResult]:
        """Execute a forward operation based on rule."""
        try:
            # Get target adapter
            target_adapter = self.registry.get(rule.target_channel)
            if not target_adapter:
                logger.warning(f"Target channel {rule.target_channel} not found")
                return None

            # Build forwarded message
            text = message_data.get("text", "")

            # Apply transforms
            if rule.include_source_info:
                source_channel = message_data.get("channel", "unknown")
                sender_name = message_data.get("sender_name", "unknown")
                text = f"[From {source_channel}/{sender_name}]\n{text}"

            if rule.prefix:
                text = f"{rule.prefix}{text}"
            if rule.suffix:
                text = f"{text}{rule.suffix}"

            # Determine target chat
            target_chat = rule.target_chat_id
            if not target_chat:
                # Use same chat ID if not specified
                target_chat = message_data.get("chat_id", "")

            # Send to target channel
            result = await target_adapter.send_message(
                chat_id=target_chat,
                text=text,
            )

            # Update rule stats
            rule.last_triggered = datetime.now()
            rule.trigger_count += 1

            # Publish forward event
            if self._session:
                await self._session.publish(
                    f"{self.config.bridge_topic_prefix}.forwarded",
                    {
                        "rule_id": rule.id,
                        "source_channel": rule.source_channel,
                        "target_channel": rule.target_channel,
                        "success": result.success if result else False,
                    }
                )

            return result

        except Exception as e:
            logger.error(f"Forward failed for rule {rule.id}: {e}")
            return None

    # Rule Management

    def add_rule(self, rule: BridgeRule) -> None:
        """Add a bridge rule."""
        self._rules[rule.id] = rule
        self._save_rules()
        logger.info(f"Added bridge rule: {rule.name}")

    def remove_rule(self, rule_id: str) -> bool:
        """Remove a bridge rule."""
        if rule_id in self._rules:
            del self._rules[rule_id]
            self._save_rules()
            logger.info(f"Removed bridge rule: {rule_id}")
            return True
        return False

    def get_rule(self, rule_id: str) -> Optional[BridgeRule]:
        """Get a rule by ID."""
        return self._rules.get(rule_id)

    def list_rules(self) -> List[BridgeRule]:
        """List all rules."""
        return list(self._rules.values())

    def enable_rule(self, rule_id: str) -> bool:
        """Enable a rule."""
        if rule_id in self._rules:
            self._rules[rule_id].enabled = True
            self._save_rules()
            return True
        return False

    def disable_rule(self, rule_id: str) -> bool:
        """Disable a rule."""
        if rule_id in self._rules:
            self._rules[rule_id].enabled = False
            self._save_rules()
            return True
        return False

    # RPC Methods

    async def add_rule_rpc(self, rule_data: Dict[str, Any]) -> Dict[str, Any]:
        """RPC method to add a rule."""
        try:
            rule = BridgeRule(
                id=rule_data.get("id", str(datetime.now().timestamp())),
                name=rule_data["name"],
                source_channel=rule_data["source_channel"],
                source_chat_id=rule_data.get("source_chat_id"),
                target_channel=rule_data["target_channel"],
                target_chat_id=rule_data.get("target_chat_id"),
                route_type=RouteType(rule_data.get("route_type", "forward")),
            )
            self.add_rule(rule)
            return {"success": True, "rule_id": rule.id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def remove_rule_rpc(self, rule_id: str) -> Dict[str, Any]:
        """RPC method to remove a rule."""
        success = self.remove_rule(rule_id)
        return {"success": success}

    async def list_rules_rpc(self) -> List[Dict[str, Any]]:
        """RPC method to list rules."""
        return [
            {
                "id": r.id,
                "name": r.name,
                "source_channel": r.source_channel,
                "target_channel": r.target_channel,
                "enabled": r.enabled,
                "trigger_count": r.trigger_count,
            }
            for r in self._rules.values()
        ]

    async def forward_message_rpc(
        self,
        source_channel: str,
        source_chat_id: str,
        target_channel: str,
        target_chat_id: str,
        text: str,
    ) -> Dict[str, Any]:
        """RPC method to forward a specific message."""
        try:
            target_adapter = self.registry.get(target_channel)
            if not target_adapter:
                return {"success": False, "error": f"Channel {target_channel} not found"}

            result = await target_adapter.send_message(
                chat_id=target_chat_id,
                text=f"[Forwarded from {source_channel}]\n{text}",
            )

            return {"success": result.success if result else False}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Convenience methods

    async def forward_to_all(
        self,
        source_channel: str,
        message: Message,
        exclude_source: bool = True,
    ) -> Dict[str, SendResult]:
        """Forward a message to all connected channels."""
        results = {}

        for channel_name in self.registry.list_channels():
            if exclude_source and channel_name == source_channel:
                continue

            adapter = self.registry.get(channel_name)
            if not adapter:
                continue

            try:
                # Would need target chat IDs from config
                # This is a simplified version
                text = f"[Broadcast from {source_channel}]\n{message.text}"
                # result = await adapter.send_message(chat_id=???, text=text)
                # results[channel_name] = result
            except Exception as e:
                logger.error(f"Broadcast to {channel_name} failed: {e}")

        return results

    async def publish_to_wamp(
        self,
        channel: str,
        message: Message,
    ) -> None:
        """Publish a channel message to WAMP for other services."""
        if not self._session:
            return

        topic = f"{self.config.channel_topic_prefix}.message"
        await self._session.publish(topic, {
            "channel": channel,
            "chat_id": message.chat_id,
            "message_id": message.id,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "text": message.text,
            "timestamp": message.timestamp.isoformat(),
        })

    async def disconnect(self) -> None:
        """Disconnect from Crossbar."""
        self._connected = False
        if self._component:
            try:
                await self._component.stop()
            except:
                pass
        self._session = None
        logger.info("Channel bridge disconnected")


def create_channel_bridge(
    registry: ChannelRegistry,
    crossbar_url: Optional[str] = None,
    realm: Optional[str] = None,
) -> ChannelBridge:
    """Factory function to create a channel bridge.

    Args:
        registry: Channel registry with adapters
        crossbar_url: Crossbar WebSocket URL
        realm: WAMP realm

    Returns:
        Configured ChannelBridge instance
    """
    config = BridgeConfig(
        crossbar_url=crossbar_url or os.getenv("CBURL", "ws://localhost:8088/ws"),
        realm=realm or os.getenv("CBREALM", "realm1"),
    )
    return ChannelBridge(config, registry)
