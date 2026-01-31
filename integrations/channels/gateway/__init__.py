"""
Gateway Module

Provides JSON-RPC 2.0 based gateway protocol for inter-service communication.
Designed for Docker container environments with volume-mounted persistence.
"""

from .protocol import (
    GatewayProtocol,
    GatewayConfig,
    GatewayStats,
    JsonRpcRequest,
    JsonRpcResponse,
    JsonRpcError,
    JsonRpcErrorCode,
    MethodInfo,
    NotificationTarget,
    get_gateway,
)

__all__ = [
    "GatewayProtocol",
    "GatewayConfig",
    "GatewayStats",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "JsonRpcError",
    "JsonRpcErrorCode",
    "MethodInfo",
    "NotificationTarget",
    "get_gateway",
]
