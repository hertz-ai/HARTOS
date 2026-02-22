"""
Gateway Protocol - JSON-RPC 2.0 Based Gateway

Provides a JSON-RPC 2.0 compliant protocol for inter-service communication
in Docker container networks. Handles method registration, request routing,
notifications, and error handling.

Features:
- JSON-RPC 2.0 compliance
- Method registration with handlers
- Async request/response handling
- Notification support (no response expected)
- Docker network addressing support
- Container-friendly persistence
- Error handling with standard codes
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


class JsonRpcErrorCode(Enum):
    """Standard JSON-RPC 2.0 error codes."""
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    # Custom error codes (application-specific)
    TIMEOUT_ERROR = -32000
    UNAUTHORIZED = -32001
    RATE_LIMITED = -32002
    SERVICE_UNAVAILABLE = -32003


@dataclass
class JsonRpcError:
    """JSON-RPC 2.0 error object."""
    code: int
    message: str
    data: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {"code": self.code, "message": self.message}
        if self.data is not None:
            result["data"] = self.data
        return result


@dataclass
class JsonRpcRequest:
    """JSON-RPC 2.0 request object."""
    method: str
    params: Optional[Union[Dict, List]] = None
    id: Optional[Union[str, int]] = None
    jsonrpc: str = "2.0"

    def is_notification(self) -> bool:
        """Check if this is a notification (no id = no response expected)."""
        return self.id is None

    def to_dict(self) -> Dict[str, Any]:
        result = {"jsonrpc": self.jsonrpc, "method": self.method}
        if self.params is not None:
            result["params"] = self.params
        if self.id is not None:
            result["id"] = self.id
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> JsonRpcRequest:
        return cls(
            method=data.get("method", ""),
            params=data.get("params"),
            id=data.get("id"),
            jsonrpc=data.get("jsonrpc", "2.0"),
        )


@dataclass
class JsonRpcResponse:
    """JSON-RPC 2.0 response object."""
    id: Optional[Union[str, int]]
    result: Optional[Any] = None
    error: Optional[JsonRpcError] = None
    jsonrpc: str = "2.0"

    def to_dict(self) -> Dict[str, Any]:
        result = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error is not None:
            result["error"] = self.error.to_dict()
        else:
            result["result"] = self.result
        return result

    @classmethod
    def success(cls, request_id: Union[str, int], result: Any) -> JsonRpcResponse:
        return cls(id=request_id, result=result)

    @classmethod
    def error_response(
        cls,
        request_id: Optional[Union[str, int]],
        code: int,
        message: str,
        data: Optional[Any] = None
    ) -> JsonRpcResponse:
        return cls(id=request_id, error=JsonRpcError(code=code, message=message, data=data))


@dataclass
class GatewayConfig:
    """Configuration for the gateway protocol."""
    host: str = "0.0.0.0"  # Bind to all interfaces for Docker
    port: int = 9000
    # Docker network settings
    docker_network: Optional[str] = None
    container_name: Optional[str] = None
    # Persistence path (should be volume-mounted in Docker)
    persistence_path: Optional[str] = None
    # Timeouts
    request_timeout_ms: int = 30000
    connect_timeout_ms: int = 5000
    # Security
    require_auth: bool = False
    api_keys: List[str] = field(default_factory=list)
    # Rate limiting
    rate_limit_per_second: int = 100

    def get_persistence_path(self) -> str:
        """Get persistence path, defaulting to Docker-friendly location."""
        if self.persistence_path:
            return self.persistence_path
        # Default to volume-mounted data directory
        import sys as _sys
        if os.environ.get('NUNBA_BUNDLED') or getattr(_sys, 'frozen', False):
            _default = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'agent_data', 'gateway')
        elif os.path.exists("/app"):
            _default = "/app/data/gateway"
        else:
            _default = "./agent_data/gateway"
        return os.environ.get("GATEWAY_DATA_PATH", _default)


@dataclass
class MethodInfo:
    """Information about a registered method."""
    name: str
    handler: Callable
    description: str = ""
    params_schema: Optional[Dict[str, Any]] = None
    requires_auth: bool = False


@dataclass
class NotificationTarget:
    """Target for notifications."""
    url: str
    methods: List[str] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class GatewayStats:
    """Gateway statistics."""
    total_requests: int = 0
    total_notifications: int = 0
    total_errors: int = 0
    methods_called: Dict[str, int] = field(default_factory=dict)
    avg_response_time_ms: float = 0.0
    uptime_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class GatewayProtocol:
    """
    JSON-RPC 2.0 based gateway for inter-service communication.

    Designed for Docker container environments with support for:
    - Container network addressing
    - Volume-mounted persistence
    - Health checks
    - Service discovery

    Usage:
        gateway = GatewayProtocol()

        # Register methods
        gateway.register_method("echo", echo_handler)
        gateway.register_method("process", process_handler, requires_auth=True)

        # Handle requests
        response = await gateway.handle_request({"jsonrpc": "2.0", "method": "echo", "params": {"msg": "hi"}, "id": 1})

        # Send notifications
        await gateway.send_notification("event.message", {"channel": "telegram", "text": "Hello"})
    """

    def __init__(self, config: Optional[GatewayConfig] = None):
        self.config = config or GatewayConfig()
        self._methods: Dict[str, MethodInfo] = {}
        self._notification_targets: List[NotificationTarget] = []
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._start_time = time.time()
        self._stats = GatewayStats()
        self._response_times: List[float] = []
        self._running = False

        # Ensure persistence directory exists
        self._ensure_persistence_dir()

        # Load state
        self._load_state()

        # Register built-in methods
        self._register_builtin_methods()

    def _ensure_persistence_dir(self) -> None:
        """Ensure persistence directory exists (for Docker volumes)."""
        path = self.config.get_persistence_path()
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create persistence directory {path}: {e}")

    def _get_state_file(self) -> str:
        """Get path to state file."""
        return os.path.join(self.config.get_persistence_path(), "gateway_state.json")

    def _load_state(self) -> None:
        """Load persisted state."""
        state_file = self._get_state_file()
        try:
            if os.path.exists(state_file):
                with open(state_file, "r") as f:
                    data = json.load(f)
                    # Restore notification targets
                    for target_data in data.get("notification_targets", []):
                        self._notification_targets.append(
                            NotificationTarget(
                                url=target_data["url"],
                                methods=target_data.get("methods", []),
                                headers=target_data.get("headers", {}),
                            )
                        )
                    logger.info(f"Loaded gateway state from {state_file}")
        except Exception as e:
            logger.warning(f"Could not load gateway state: {e}")

    def _save_state(self) -> None:
        """Persist state to disk."""
        state_file = self._get_state_file()
        try:
            data = {
                "notification_targets": [
                    {"url": t.url, "methods": t.methods, "headers": t.headers}
                    for t in self._notification_targets
                ],
                "saved_at": datetime.now().isoformat(),
            }
            with open(state_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save gateway state: {e}")

    def _register_builtin_methods(self) -> None:
        """Register built-in RPC methods."""
        self.register_method(
            "rpc.discover",
            self._rpc_discover,
            description="List available methods",
        )
        self.register_method(
            "rpc.describe",
            self._rpc_describe,
            description="Describe a specific method",
        )
        self.register_method(
            "gateway.health",
            self._gateway_health,
            description="Health check endpoint",
        )
        self.register_method(
            "gateway.stats",
            self._gateway_stats,
            description="Get gateway statistics",
        )
        self.register_method(
            "gateway.subscribe",
            self._gateway_subscribe,
            description="Subscribe to notifications",
        )
        self.register_method(
            "gateway.unsubscribe",
            self._gateway_unsubscribe,
            description="Unsubscribe from notifications",
        )

    async def _rpc_discover(self, params: Optional[Dict] = None) -> List[Dict]:
        """List all available methods."""
        return [
            {
                "name": info.name,
                "description": info.description,
                "requires_auth": info.requires_auth,
            }
            for info in self._methods.values()
        ]

    async def _rpc_describe(self, params: Dict) -> Optional[Dict]:
        """Describe a specific method."""
        method_name = params.get("method")
        if not method_name or method_name not in self._methods:
            return None
        info = self._methods[method_name]
        return {
            "name": info.name,
            "description": info.description,
            "params_schema": info.params_schema,
            "requires_auth": info.requires_auth,
        }

    async def _gateway_health(self, params: Optional[Dict] = None) -> Dict:
        """Health check endpoint."""
        return {
            "status": "healthy",
            "uptime_seconds": time.time() - self._start_time,
            "methods_count": len(self._methods),
            "container": self.config.container_name,
            "network": self.config.docker_network,
        }

    async def _gateway_stats(self, params: Optional[Dict] = None) -> Dict:
        """Get gateway statistics."""
        self._stats.uptime_seconds = time.time() - self._start_time
        if self._response_times:
            self._stats.avg_response_time_ms = sum(self._response_times) / len(self._response_times)
        return self._stats.to_dict()

    async def _gateway_subscribe(self, params: Dict) -> Dict:
        """Subscribe to notifications."""
        url = params.get("url")
        methods = params.get("methods", [])
        headers = params.get("headers", {})

        if not url:
            raise ValueError("url is required")

        target = NotificationTarget(url=url, methods=methods, headers=headers)
        self._notification_targets.append(target)
        self._save_state()

        return {"subscribed": True, "url": url, "methods": methods}

    async def _gateway_unsubscribe(self, params: Dict) -> Dict:
        """Unsubscribe from notifications."""
        url = params.get("url")
        if not url:
            raise ValueError("url is required")

        original_count = len(self._notification_targets)
        self._notification_targets = [t for t in self._notification_targets if t.url != url]
        removed = original_count - len(self._notification_targets)
        self._save_state()

        return {"unsubscribed": True, "url": url, "removed_count": removed}

    def register_method(
        self,
        name: str,
        handler: Callable[..., Coroutine[Any, Any, Any]],
        description: str = "",
        params_schema: Optional[Dict[str, Any]] = None,
        requires_auth: bool = False,
    ) -> None:
        """
        Register a method handler.

        Args:
            name: Method name (e.g., "channel.send", "agent.process")
            handler: Async function to handle the method call
            description: Human-readable description
            params_schema: JSON schema for parameters validation
            requires_auth: Whether this method requires authentication
        """
        self._methods[name] = MethodInfo(
            name=name,
            handler=handler,
            description=description,
            params_schema=params_schema,
            requires_auth=requires_auth,
        )
        logger.debug(f"Registered method: {name}")

    def unregister_method(self, name: str) -> bool:
        """Unregister a method."""
        if name in self._methods:
            del self._methods[name]
            return True
        return False

    def _validate_request(self, data: Dict[str, Any]) -> Optional[JsonRpcError]:
        """Validate JSON-RPC request structure."""
        if not isinstance(data, dict):
            return JsonRpcError(
                code=JsonRpcErrorCode.INVALID_REQUEST.value,
                message="Request must be an object",
            )

        if data.get("jsonrpc") != "2.0":
            return JsonRpcError(
                code=JsonRpcErrorCode.INVALID_REQUEST.value,
                message="jsonrpc must be '2.0'",
            )

        if "method" not in data or not isinstance(data["method"], str):
            return JsonRpcError(
                code=JsonRpcErrorCode.INVALID_REQUEST.value,
                message="method must be a string",
            )

        params = data.get("params")
        if params is not None and not isinstance(params, (dict, list)):
            return JsonRpcError(
                code=JsonRpcErrorCode.INVALID_PARAMS.value,
                message="params must be an object or array",
            )

        return None

    def _check_auth(self, request: Dict[str, Any], method_info: MethodInfo) -> Optional[JsonRpcError]:
        """Check authentication if required."""
        if not method_info.requires_auth:
            return None

        if not self.config.require_auth:
            return None

        # Check for API key in params or headers (passed via metadata)
        api_key = None
        params = request.get("params", {})
        if isinstance(params, dict):
            api_key = params.get("_api_key") or params.get("api_key")

        if not api_key or api_key not in self.config.api_keys:
            return JsonRpcError(
                code=JsonRpcErrorCode.UNAUTHORIZED.value,
                message="Unauthorized: invalid or missing API key",
            )

        return None

    async def handle_request(self, request: Union[Dict, str, bytes]) -> Dict[str, Any]:
        """
        Handle a JSON-RPC request.

        Args:
            request: JSON-RPC request (dict, JSON string, or bytes)

        Returns:
            JSON-RPC response as dictionary
        """
        start_time = time.time()
        self._stats.total_requests += 1

        # Parse if string/bytes
        if isinstance(request, (str, bytes)):
            try:
                request = json.loads(request)
            except json.JSONDecodeError as e:
                self._stats.total_errors += 1
                return JsonRpcResponse.error_response(
                    None,
                    JsonRpcErrorCode.PARSE_ERROR.value,
                    f"Parse error: {e}",
                ).to_dict()

        # Handle batch requests
        if isinstance(request, list):
            if not request:
                return JsonRpcResponse.error_response(
                    None,
                    JsonRpcErrorCode.INVALID_REQUEST.value,
                    "Empty batch request",
                ).to_dict()
            responses = await asyncio.gather(
                *[self._handle_single_request(r) for r in request]
            )
            # Filter out None responses (notifications)
            return [r for r in responses if r is not None]

        # Handle single request
        response = await self._handle_single_request(request)

        # Track response time
        elapsed_ms = (time.time() - start_time) * 1000
        self._response_times.append(elapsed_ms)
        # Keep only last 1000 response times
        if len(self._response_times) > 1000:
            self._response_times = self._response_times[-1000:]

        return response

    async def _handle_single_request(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle a single JSON-RPC request."""
        # Validate structure
        error = self._validate_request(request)
        if error:
            self._stats.total_errors += 1
            return JsonRpcResponse.error_response(
                request.get("id"),
                error.code,
                error.message,
                error.data,
            ).to_dict()

        method_name = request["method"]
        params = request.get("params")
        request_id = request.get("id")
        is_notification = request_id is None

        # Check method exists
        if method_name not in self._methods:
            self._stats.total_errors += 1
            if is_notification:
                return None
            return JsonRpcResponse.error_response(
                request_id,
                JsonRpcErrorCode.METHOD_NOT_FOUND.value,
                f"Method not found: {method_name}",
            ).to_dict()

        method_info = self._methods[method_name]

        # Check authentication
        auth_error = self._check_auth(request, method_info)
        if auth_error:
            self._stats.total_errors += 1
            if is_notification:
                return None
            return JsonRpcResponse.error_response(
                request_id,
                auth_error.code,
                auth_error.message,
                auth_error.data,
            ).to_dict()

        # Track method calls
        self._stats.methods_called[method_name] = (
            self._stats.methods_called.get(method_name, 0) + 1
        )

        # Execute handler
        try:
            if params is None:
                result = await method_info.handler()
            elif isinstance(params, dict):
                result = await method_info.handler(params)
            else:
                result = await method_info.handler(*params)

            if is_notification:
                self._stats.total_notifications += 1
                return None

            return JsonRpcResponse.success(request_id, result).to_dict()

        except Exception as e:
            self._stats.total_errors += 1
            logger.exception(f"Error executing method {method_name}: {e}")
            if is_notification:
                return None
            return JsonRpcResponse.error_response(
                request_id,
                JsonRpcErrorCode.INTERNAL_ERROR.value,
                f"Internal error: {str(e)}",
            ).to_dict()

    async def send_notification(self, method: str, params: Optional[Dict] = None) -> None:
        """
        Send a notification to all subscribed targets.

        Notifications are fire-and-forget (no response expected).

        Args:
            method: Notification method name
            params: Notification parameters
        """
        notification = JsonRpcRequest(
            method=method,
            params=params,
            id=None,  # Notifications have no id
        )

        payload = json.dumps(notification.to_dict())

        # Send to all matching targets
        for target in self._notification_targets:
            # Check if target subscribes to this method
            if target.methods and method not in target.methods:
                continue

            try:
                await self._send_to_target(target, payload)
            except Exception as e:
                logger.warning(f"Failed to send notification to {target.url}: {e}")

    async def _send_to_target(self, target: NotificationTarget, payload: str) -> None:
        """Send notification to a target URL."""
        # Use aiohttp if available, otherwise log the intent
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                headers = {"Content-Type": "application/json"}
                headers.update(target.headers)
                async with session.post(
                    target.url,
                    data=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as response:
                    if response.status >= 400:
                        logger.warning(
                            f"Notification target {target.url} returned {response.status}"
                        )
        except ImportError:
            logger.debug(f"Would send notification to {target.url}: {payload}")

    async def call(
        self,
        target_url: str,
        method: str,
        params: Optional[Dict] = None,
        timeout_ms: Optional[int] = None,
    ) -> Any:
        """
        Make an RPC call to another service.

        Args:
            target_url: URL of the target service
            method: Method to call
            params: Parameters for the method
            timeout_ms: Timeout in milliseconds

        Returns:
            Result from the method call

        Raises:
            Exception: If the call fails or times out
        """
        request_id = str(uuid.uuid4())
        request = JsonRpcRequest(
            method=method,
            params=params,
            id=request_id,
        )

        timeout = (timeout_ms or self.config.request_timeout_ms) / 1000

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    target_url,
                    json=request.to_dict(),
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    data = await response.json()

                    if "error" in data:
                        error = data["error"]
                        raise Exception(f"RPC error {error.get('code')}: {error.get('message')}")

                    return data.get("result")
        except ImportError:
            raise ImportError("aiohttp is required for RPC calls")

    def get_docker_address(self) -> str:
        """
        Get the Docker-friendly address for this gateway.

        Returns address in format suitable for Docker networking.
        """
        if self.config.container_name and self.config.docker_network:
            return f"http://{self.config.container_name}:{self.config.port}"
        return f"http://{self.config.host}:{self.config.port}"

    def get_methods(self) -> List[str]:
        """Get list of registered method names."""
        return list(self._methods.keys())

    def get_stats(self) -> GatewayStats:
        """Get current gateway statistics."""
        self._stats.uptime_seconds = time.time() - self._start_time
        return self._stats


# Singleton instance
_gateway: Optional[GatewayProtocol] = None


def get_gateway(config: Optional[GatewayConfig] = None) -> GatewayProtocol:
    """Get or create the global gateway instance."""
    global _gateway
    if _gateway is None:
        _gateway = GatewayProtocol(config)
    return _gateway
