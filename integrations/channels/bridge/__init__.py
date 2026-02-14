"""
Channel Bridge - WAMP Crossbar Integration for Channel Chaining

Enables message routing between channels via WAMP pub/sub.
"""

from .wamp_bridge import (
    ChannelBridge,
    BridgeConfig,
    BridgeRule,
    RouteType,
    create_channel_bridge,
)

__all__ = [
    "ChannelBridge",
    "BridgeConfig",
    "BridgeRule",
    "RouteType",
    "create_channel_bridge",
]
