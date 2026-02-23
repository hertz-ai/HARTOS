"""
Tests for Gateway Protocol (JSON-RPC 2.0)

Tests method registration, request handling, notifications,
error handling, and Docker compatibility features.
"""

import asyncio
import json
import os
import pytest
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

# Configure pytest-asyncio
pytest_plugins = ('pytest_asyncio',)

# Import the gateway module
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.channels.gateway.protocol import (
    GatewayProtocol,
    GatewayConfig,
    GatewayStats,
    JsonRpcRequest,
    JsonRpcResponse,
    JsonRpcError,
    JsonRpcErrorCode,
    MethodInfo,
    NotificationTarget,
)


class TestJsonRpcRequest:
    """Tests for JsonRpcRequest dataclass."""

    def test_create_request(self):
        """Test creating a JSON-RPC request."""
        request = JsonRpcRequest(
            method="test.echo",
            params={"message": "hello"},
            id=1,
        )
        assert request.method == "test.echo"
        assert request.params == {"message": "hello"}
        assert request.id == 1
        assert request.jsonrpc == "2.0"

    def test_notification_check(self):
        """Test notification detection (no id)."""
        notification = JsonRpcRequest(method="event.fire", params={"data": "test"})
        assert notification.is_notification() is True

        request = JsonRpcRequest(method="test.call", id=1)
        assert request.is_notification() is False

    def test_to_dict(self):
        """Test serialization to dict."""
        request = JsonRpcRequest(
            method="test.method",
            params={"key": "value"},
            id="abc-123",
        )
        data = request.to_dict()
        assert data["jsonrpc"] == "2.0"
        assert data["method"] == "test.method"
        assert data["params"] == {"key": "value"}
        assert data["id"] == "abc-123"

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "jsonrpc": "2.0",
            "method": "test.method",
            "params": [1, 2, 3],
            "id": 42,
        }
        request = JsonRpcRequest.from_dict(data)
        assert request.method == "test.method"
        assert request.params == [1, 2, 3]
        assert request.id == 42


class TestJsonRpcResponse:
    """Tests for JsonRpcResponse dataclass."""

    def test_success_response(self):
        """Test creating a success response."""
        response = JsonRpcResponse.success(1, {"result": "ok"})
        assert response.id == 1
        assert response.result == {"result": "ok"}
        assert response.error is None

    def test_error_response(self):
        """Test creating an error response."""
        response = JsonRpcResponse.error_response(
            1,
            JsonRpcErrorCode.METHOD_NOT_FOUND.value,
            "Method not found: test.unknown",
        )
        assert response.id == 1
        assert response.result is None
        assert response.error is not None
        assert response.error.code == -32601
        assert "not found" in response.error.message

    def test_to_dict_success(self):
        """Test serialization of success response."""
        response = JsonRpcResponse.success(1, "hello")
        data = response.to_dict()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert data["result"] == "hello"
        assert "error" not in data

    def test_to_dict_error(self):
        """Test serialization of error response."""
        response = JsonRpcResponse.error_response(1, -32600, "Invalid request")
        data = response.to_dict()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert "error" in data
        assert data["error"]["code"] == -32600
        assert "result" not in data


class TestGatewayProtocol:
    """Tests for the main GatewayProtocol class."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for persistence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def gateway(self, temp_dir):
        """Create a gateway with temp storage."""
        config = GatewayConfig(persistence_path=temp_dir)
        return GatewayProtocol(config)

    def test_init(self, gateway):
        """Test gateway initialization."""
        assert gateway is not None
        assert len(gateway._methods) > 0  # Built-in methods registered
        assert "rpc.discover" in gateway._methods
        assert "gateway.health" in gateway._methods

    def test_register_method(self, gateway):
        """Test registering a custom method."""
        async def my_handler(params):
            return params.get("value", 0) * 2

        gateway.register_method(
            "math.double",
            my_handler,
            description="Double a number",
        )

        assert "math.double" in gateway._methods
        assert gateway._methods["math.double"].description == "Double a number"

    def test_unregister_method(self, gateway):
        """Test unregistering a method."""
        async def handler():
            return True

        gateway.register_method("test.remove", handler)
        assert "test.remove" in gateway._methods

        result = gateway.unregister_method("test.remove")
        assert result is True
        assert "test.remove" not in gateway._methods

        # Unregistering non-existent method
        result = gateway.unregister_method("test.nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_handle_request_success(self, gateway):
        """Test handling a successful request."""
        async def echo_handler(params):
            return {"echo": params.get("message")}

        gateway.register_method("test.echo", echo_handler)

        request = {
            "jsonrpc": "2.0",
            "method": "test.echo",
            "params": {"message": "hello"},
            "id": 1,
        }

        response = await gateway.handle_request(request)
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert response["result"] == {"echo": "hello"}

    @pytest.mark.asyncio
    async def test_handle_request_method_not_found(self, gateway):
        """Test handling request for non-existent method."""
        request = {
            "jsonrpc": "2.0",
            "method": "test.nonexistent",
            "id": 1,
        }

        response = await gateway.handle_request(request)
        assert "error" in response
        assert response["error"]["code"] == JsonRpcErrorCode.METHOD_NOT_FOUND.value

    @pytest.mark.asyncio
    async def test_handle_request_invalid_jsonrpc(self, gateway):
        """Test handling request with invalid JSON-RPC version."""
        request = {
            "jsonrpc": "1.0",
            "method": "test.method",
            "id": 1,
        }

        response = await gateway.handle_request(request)
        assert "error" in response
        assert response["error"]["code"] == JsonRpcErrorCode.INVALID_REQUEST.value

    @pytest.mark.asyncio
    async def test_handle_request_parse_error(self, gateway):
        """Test handling malformed JSON."""
        response = await gateway.handle_request("not valid json{")
        assert "error" in response
        assert response["error"]["code"] == JsonRpcErrorCode.PARSE_ERROR.value

    @pytest.mark.asyncio
    async def test_handle_notification(self, gateway):
        """Test handling a notification (no response expected)."""
        events = []

        async def event_handler(params):
            events.append(params)

        gateway.register_method("test.event", event_handler)

        # Notification has no id
        request = {
            "jsonrpc": "2.0",
            "method": "test.event",
            "params": {"data": "test"},
        }

        response = await gateway.handle_request(request)
        # Notifications return None (no response)
        assert response is None

    @pytest.mark.asyncio
    async def test_handle_batch_request(self, gateway):
        """Test handling batch requests."""
        async def handler(params=None):
            return "ok"

        gateway.register_method("test.method", handler)

        batch = [
            {"jsonrpc": "2.0", "method": "test.method", "id": 1},
            {"jsonrpc": "2.0", "method": "test.method", "id": 2},
            {"jsonrpc": "2.0", "method": "test.method", "id": 3},
        ]

        responses = await gateway.handle_request(batch)
        assert isinstance(responses, list)
        assert len(responses) == 3
        assert all(r["result"] == "ok" for r in responses)

    @pytest.mark.asyncio
    async def test_builtin_discover(self, gateway):
        """Test the built-in rpc.discover method."""
        request = {
            "jsonrpc": "2.0",
            "method": "rpc.discover",
            "id": 1,
        }

        response = await gateway.handle_request(request)
        assert "result" in response
        methods = response["result"]
        assert isinstance(methods, list)
        assert any(m["name"] == "gateway.health" for m in methods)

    @pytest.mark.asyncio
    async def test_builtin_health(self, gateway):
        """Test the built-in gateway.health method."""
        request = {
            "jsonrpc": "2.0",
            "method": "gateway.health",
            "id": 1,
        }

        response = await gateway.handle_request(request)
        assert "result" in response
        health = response["result"]
        assert health["status"] == "healthy"
        assert "uptime_seconds" in health

    @pytest.mark.asyncio
    async def test_builtin_stats(self, gateway):
        """Test the built-in gateway.stats method."""
        # Make some requests first
        async def handler():
            return "ok"

        gateway.register_method("test.method", handler)

        for i in range(5):
            await gateway.handle_request({
                "jsonrpc": "2.0",
                "method": "test.method",
                "id": i,
            })

        request = {
            "jsonrpc": "2.0",
            "method": "gateway.stats",
            "id": 100,
        }

        response = await gateway.handle_request(request)
        assert "result" in response
        stats = response["result"]
        assert stats["total_requests"] >= 5
        assert "methods_called" in stats

    @pytest.mark.asyncio
    async def test_error_in_handler(self, gateway):
        """Test handling errors thrown by method handlers."""
        async def failing_handler(params=None):
            raise ValueError("Something went wrong")

        gateway.register_method("test.fail", failing_handler)

        request = {
            "jsonrpc": "2.0",
            "method": "test.fail",
            "id": 1,
        }

        response = await gateway.handle_request(request)
        assert "error" in response
        assert response["error"]["code"] == JsonRpcErrorCode.INTERNAL_ERROR.value
        assert "went wrong" in response["error"]["message"]

    def test_get_methods(self, gateway):
        """Test getting list of registered methods."""
        methods = gateway.get_methods()
        assert isinstance(methods, list)
        assert "rpc.discover" in methods
        assert "gateway.health" in methods

    def test_get_stats(self, gateway):
        """Test getting gateway statistics."""
        stats = gateway.get_stats()
        assert isinstance(stats, GatewayStats)
        assert stats.uptime_seconds >= 0

    def test_docker_address(self, temp_dir):
        """Test Docker address generation."""
        config = GatewayConfig(
            container_name="hevolvebot-gateway",
            docker_network="hevolvebot-network",
            port=9000,
            persistence_path=temp_dir,
        )
        gateway = GatewayProtocol(config)

        address = gateway.get_docker_address()
        assert address == "http://hevolvebot-gateway:9000"

    def test_docker_address_no_container(self, temp_dir):
        """Test Docker address without container name."""
        config = GatewayConfig(
            host="0.0.0.0",
            port=8080,
            persistence_path=temp_dir,
        )
        gateway = GatewayProtocol(config)

        address = gateway.get_docker_address()
        assert address == "http://0.0.0.0:8080"


class TestGatewayPersistence:
    """Tests for gateway state persistence."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for persistence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.mark.asyncio
    async def test_notification_targets_persist(self, temp_dir):
        """Test that notification targets persist across restarts."""
        config = GatewayConfig(persistence_path=temp_dir)

        # Create gateway and subscribe
        gateway1 = GatewayProtocol(config)
        await gateway1.handle_request({
            "jsonrpc": "2.0",
            "method": "gateway.subscribe",
            "params": {
                "url": "http://service:8080/webhook",
                "methods": ["event.message"],
            },
            "id": 1,
        })

        assert len(gateway1._notification_targets) == 1

        # Create new gateway instance (simulating restart)
        gateway2 = GatewayProtocol(config)
        assert len(gateway2._notification_targets) == 1
        assert gateway2._notification_targets[0].url == "http://service:8080/webhook"


class TestJsonRpcErrorCodes:
    """Tests for JSON-RPC error codes."""

    def test_standard_error_codes(self):
        """Test standard JSON-RPC error codes."""
        assert JsonRpcErrorCode.PARSE_ERROR.value == -32700
        assert JsonRpcErrorCode.INVALID_REQUEST.value == -32600
        assert JsonRpcErrorCode.METHOD_NOT_FOUND.value == -32601
        assert JsonRpcErrorCode.INVALID_PARAMS.value == -32602
        assert JsonRpcErrorCode.INTERNAL_ERROR.value == -32603

    def test_custom_error_codes(self):
        """Test custom application error codes."""
        assert JsonRpcErrorCode.TIMEOUT_ERROR.value == -32000
        assert JsonRpcErrorCode.UNAUTHORIZED.value == -32001
        assert JsonRpcErrorCode.RATE_LIMITED.value == -32002


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
