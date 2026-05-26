"""
Service Tool Registry — follows MCPToolRegistry pattern (mcp/mcp_integration.py)
but for any HTTP microservice (not just MCP protocol servers).

Design:
- ServiceToolInfo describes a tool's endpoints, auth, and health check
- ServiceToolRegistry manages discovery, health, and function generation
- Global singleton: service_tool_registry (mirrors mcp_registry)
- Uses core.http_pool for connection pooling (same as MCP)
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable

logger = logging.getLogger(__name__)


@dataclass
class ServiceToolInfo:
    """Metadata for a registered service tool."""
    name: str
    description: str
    base_url: str
    endpoints: Dict[str, Dict[str, Any]]  # endpoint_name -> {path, method, description, params_schema}
    health_endpoint: str = "/health"
    version: str = "1.0.0"
    tags: List[str] = field(default_factory=list)
    api_key: Optional[str] = None
    api_key_header: str = "Authorization"
    timeout: int = 30
    is_healthy: bool = False
    registered_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "base_url": self.base_url,
            "endpoints": self.endpoints,
            "health_endpoint": self.health_endpoint,
            "version": self.version,
            "tags": self.tags,
            "api_key_header": self.api_key_header,
            "timeout": self.timeout,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ServiceToolInfo':
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            base_url=data["base_url"],
            endpoints=data.get("endpoints", {}),
            health_endpoint=data.get("health_endpoint", "/health"),
            version=data.get("version", "1.0.0"),
            tags=data.get("tags", []),
            api_key=data.get("api_key"),
            api_key_header=data.get("api_key_header", "Authorization"),
            timeout=data.get("timeout", 30),
        )


class ServiceToolRegistry:
    """
    Registry for HTTP microservice tools.

    Mirrors MCPToolRegistry (mcp/mcp_integration.py:185-315):
    - add_server → register_tool (with health check)
    - create_tool_function → create_endpoint_function
    - get_all_tool_functions → same signature
    - Global singleton: service_tool_registry
    """

    def __init__(self, config_file: str = "service_tools.json"):
        self._tools: Dict[str, ServiceToolInfo] = {}
        self._config_file = config_file

    def register_tool(self, tool_info: ServiceToolInfo) -> bool:
        """Register a tool. Health-checks first; skips if service is down."""
        if tool_info.name in self._tools:
            logger.info(f"Service tool '{tool_info.name}' already registered, skipping")
            return True

        tool_info.is_healthy = self._health_check(tool_info)
        tool_info.registered_at = datetime.now().isoformat()
        self._tools[tool_info.name] = tool_info

        status = "healthy" if tool_info.is_healthy else "unhealthy (registered anyway)"
        logger.info(f"Registered service tool: {tool_info.name} [{status}]")
        return True

    def unregister_tool(self, name: str) -> bool:
        if name in self._tools:
            del self._tools[name]
            logger.info(f"Unregistered service tool: {name}")
            return True
        return False

    def _health_check(self, tool_info: ServiceToolInfo) -> bool:
        """Check if service is reachable."""
        try:
            from core.http_pool import pooled_get
            headers = {}
            if tool_info.api_key:
                headers[tool_info.api_key_header] = f"Bearer {tool_info.api_key}"

            response = pooled_get(
                f"{tool_info.base_url.rstrip('/')}{tool_info.health_endpoint}",
                headers=headers,
                timeout=5,
            )
            return response.status_code == 200
        except Exception as e:
            logger.debug(f"Health check failed for {tool_info.name}: {e}")
            return False

    def health_check(self, name: str) -> bool:
        """Re-check health for a specific tool."""
        tool = self._tools.get(name)
        if not tool:
            return False
        tool.is_healthy = self._health_check(tool)
        return tool.is_healthy

    def health_check_all(self) -> Dict[str, bool]:
        """Re-check health for all registered tools."""
        return {name: self.health_check(name) for name in self._tools}

    def create_endpoint_function(self, tool_name: str, endpoint_name: str) -> Optional[Callable]:
        """
        Create a callable for a specific endpoint.

        Mirrors MCPToolRegistry.create_tool_function (mcp_integration.py:262-295):
        returns a function with __name__ and __doc__ set for autogen registration.
        """
        tool = self._tools.get(tool_name)
        if not tool:
            return None

        endpoint = tool.endpoints.get(endpoint_name)
        if not endpoint:
            return None

        path = endpoint["path"]
        method = endpoint.get("method", "POST").upper()
        description = endpoint.get("description", f"{tool_name} {endpoint_name}")
        timeout = tool.timeout

        # Capture in closure
        base_url = tool.base_url.rstrip("/")
        api_key = tool.api_key
        api_key_header = tool.api_key_header

        # If endpoint has a native handler, use it directly (no HTTP)
        native_handler = endpoint.get("native_handler")

        def endpoint_executor(**kwargs) -> str:
            """Execute the service tool endpoint."""
            try:
                if native_handler is not None:
                    return native_handler(json.dumps(kwargs))

                from core.http_pool import pooled_get, pooled_post

                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers[api_key_header] = f"Bearer {api_key}"

                url = f"{base_url}{path}"

                if method == "GET":
                    resp = pooled_get(url, params=kwargs, headers=headers, timeout=timeout)
                else:
                    resp = pooled_post(url, json=kwargs, headers=headers, timeout=timeout)

                if resp.status_code == 200:
                    return json.dumps(resp.json())
                else:
                    return json.dumps({"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"})
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)})

        # Set function metadata (same as MCPToolRegistry.create_tool_function)
        func_name = f"{tool_name}_{endpoint_name}"
        endpoint_executor.__name__ = func_name
        endpoint_executor.__doc__ = description

        return endpoint_executor

    def get_all_tool_functions(self) -> Dict[str, Callable]:
        """
        Get all tools as executable functions.

        Mirrors MCPToolRegistry.get_all_tool_functions (mcp_integration.py:297-311).
        Creates one function per endpoint for each registered tool.
        """
        functions = {}
        for tool_name, tool in self._tools.items():
            for endpoint_name in tool.endpoints:
                func = self.create_endpoint_function(tool_name, endpoint_name)
                if func:
                    functions[func.__name__] = func
        return functions

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """
        Get tool definitions in autogen-compatible format.

        Mirrors MCPToolRegistry.get_tool_definitions (mcp_integration.py:241-260).
        """
        defs = []
        for tool_name, tool in self._tools.items():
            for ep_name, ep in tool.endpoints.items():
                defs.append({
                    "name": f"{tool_name}_{ep_name}",
                    "description": ep.get("description", f"{tool_name} {ep_name}"),
                    "parameters": ep.get("params_schema", {}),
                    "service_tool": tool_name,
                    "endpoint": ep_name,
                })
        return defs

    def get_langchain_tools(self) -> list:
        """
        Get healthy tools as LangChain Tool() objects for get_tools().

        Plugs into hart_intelligence get_tools().
        LangChain Tool func receives a single string — we route it to
        the first parameter defined in the endpoint's params_schema.
        """
        from langchain.agents import Tool

        tools = []
        for tool_name, tool in self._tools.items():
            if not tool.is_healthy:
                continue
            for ep_name, ep in tool.endpoints.items():
                func = self.create_endpoint_function(tool_name, ep_name)
                if func:
                    # Determine the primary parameter name from params_schema
                    # so the single LangChain string input maps correctly.
                    params = ep.get("params_schema", {})
                    primary_param = next(iter(params), "query") if params else "query"

                    tools.append(Tool(
                        name=func.__name__,
                        func=lambda query, _f=func, _p=primary_param: _f(**{_p: query}),
                        description=ep.get("description", f"{tool_name} {ep_name}"),
                    ))
        return tools

    def save_config(self) -> None:
        """Persist registry to JSON config file."""
        data = {"tools": [t.to_dict() for t in self._tools.values()]}
        try:
            with open(self._config_file, "w") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Saved {len(self._tools)} tools to {self._config_file}")
        except Exception as e:
            logger.warning(f"Failed to save service tools config: {e}")

    def load_config(self) -> int:
        """Load registry from JSON config file. Returns count loaded."""
        if not os.path.exists(self._config_file):
            logger.info(f"No service tools config at {self._config_file}")
            return 0

        try:
            with open(self._config_file, "r") as f:
                data = json.load(f)

            loaded = 0
            for tool_data in data.get("tools", []):
                tool_info = ServiceToolInfo.from_dict(tool_data)
                if self.register_tool(tool_info):
                    loaded += 1

            logger.info(f"Loaded {loaded} service tools from {self._config_file}")
            return loaded
        except Exception as e:
            logger.warning(f"Failed to load service tools config: {e}")
            return 0


# Global singleton (mirrors mcp_registry in mcp/mcp_integration.py:315)
service_tool_registry = ServiceToolRegistry()
