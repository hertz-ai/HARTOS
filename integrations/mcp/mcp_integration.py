"""
MCP (Model Context Protocol) Integration Module

This module enables the agent system to connect to external user-provided MCP servers
and use their tools within the Autogen framework.

Features:
- Connect to multiple MCP servers
- Discover and register tools from MCP servers
- Convert MCP tools to Autogen-compatible functions
- Automatic error handling and retries
"""

import json
import logging
import requests
from typing import List, Dict, Any, Optional, Callable
from datetime import datetime
import os

logger = logging.getLogger(__name__)


class MCPServerConnector:
    """Connects to an external MCP server and manages tool discovery"""

    def __init__(self, server_name: str, server_url: str, api_key: Optional[str] = None):
        """
        Initialize MCP server connector

        Args:
            server_name: Human-readable name for the server
            server_url: Base URL of the MCP server
            api_key: Optional API key for authentication
        """
        self.server_name = server_name
        self.server_url = server_url.rstrip('/')
        self.api_key = api_key
        self.tools = []
        self.connected = False

    def connect(self) -> bool:
        """
        Connect to the MCP server and verify it's accessible

        Returns:
            True if connection successful, False otherwise
        """
        try:
            headers = {}
            if self.api_key:
                headers['Authorization'] = f'Bearer {self.api_key}'

            # Try health endpoint first
            response = requests.get(
                f"{self.server_url}/health",
                headers=headers,
                timeout=5
            )

            if response.status_code == 200:
                self.connected = True
                logger.info(f"Connected to MCP server: {self.server_name}")
                return True
            else:
                logger.warning(f"MCP server {self.server_name} health check failed: {response.status_code}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to connect to MCP server {self.server_name}: {e}")
            return False

    def discover_tools(self) -> List[Dict[str, Any]]:
        """
        Discover available tools from the MCP server

        Returns:
            List of tool definitions
        """
        if not self.connected:
            logger.warning(f"Cannot discover tools - not connected to {self.server_name}")
            return []

        try:
            headers = {}
            if self.api_key:
                headers['Authorization'] = f'Bearer {self.api_key}'

            response = requests.get(
                f"{self.server_url}/tools/list",
                headers=headers,
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                self.tools = data.get('tools', [])
                logger.info(f"Discovered {len(self.tools)} tools from {self.server_name}")
                return self.tools
            else:
                logger.error(f"Failed to discover tools from {self.server_name}: {response.status_code}")
                return []

        except requests.exceptions.RequestException as e:
            logger.error(f"Error discovering tools from {self.server_name}: {e}")
            return []

    def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a tool on the MCP server

        Args:
            tool_name: Name of the tool to execute
            arguments: Tool arguments

        Returns:
            Tool execution result
        """
        if not self.connected:
            return {
                'success': False,
                'error': f'Not connected to MCP server {self.server_name}'
            }

        try:
            headers = {'Content-Type': 'application/json'}
            if self.api_key:
                headers['Authorization'] = f'Bearer {self.api_key}'

            payload = {
                'tool': tool_name,
                'arguments': arguments
            }

            response = requests.post(
                f"{self.server_url}/tools/execute",
                headers=headers,
                json=payload,
                timeout=30
            )

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Tool execution failed on {self.server_name}: {response.status_code}")
                return {
                    'success': False,
                    'error': f'HTTP {response.status_code}: {response.text}'
                }

        except requests.exceptions.RequestException as e:
            logger.error(f"Error executing tool {tool_name} on {self.server_name}: {e}")
            return {
                'success': False,
                'error': str(e)
            }


class MCPToolRegistry:
    """Registry for managing multiple MCP servers and their tools"""

    def __init__(self):
        """Initialize the MCP tool registry"""
        self.servers: Dict[str, MCPServerConnector] = {}
        self.tools: Dict[str, tuple] = {}  # tool_name -> (server_name, tool_def)

    def add_server(self, server_name: str, server_url: str, api_key: Optional[str] = None) -> bool:
        """
        Add an MCP server to the registry

        Args:
            server_name: Unique name for the server
            server_url: Base URL of the server
            api_key: Optional API key

        Returns:
            True if server added successfully
        """
        if server_name in self.servers:
            logger.warning(f"MCP server {server_name} already exists in registry")
            return False

        connector = MCPServerConnector(server_name, server_url, api_key)
        if connector.connect():
            self.servers[server_name] = connector
            logger.info(f"Added MCP server {server_name} to registry")
            return True
        else:
            logger.error(f"Failed to add MCP server {server_name}")
            return False

    def discover_all_tools(self) -> int:
        """
        Discover tools from all registered servers

        Returns:
            Total number of tools discovered
        """
        total_tools = 0
        self.tools.clear()

        for server_name, connector in self.servers.items():
            tools = connector.discover_tools()
            for tool in tools:
                tool_name = tool.get('name')
                if tool_name:
                    # Prefix tool name with server name to avoid conflicts
                    prefixed_name = f"{server_name}_{tool_name}"
                    self.tools[prefixed_name] = (server_name, tool)
                    total_tools += 1

        logger.info(f"Discovered {total_tools} total tools from {len(self.servers)} MCP servers")
        return total_tools

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """
        Get all tool definitions in Autogen-compatible format

        Returns:
            List of tool definitions
        """
        tool_defs = []

        for prefixed_name, (server_name, tool_def) in self.tools.items():
            autogen_def = {
                'name': prefixed_name,
                'description': tool_def.get('description', f'Tool from {server_name}'),
                'parameters': tool_def.get('parameters', {}),
                'mcp_server': server_name,
                'original_name': tool_def.get('name')
            }
            tool_defs.append(autogen_def)

        return tool_defs

    def create_tool_function(self, tool_name: str) -> Optional[Callable]:
        """
        Create an executable function for a tool

        Args:
            tool_name: Prefixed tool name (server_toolname)

        Returns:
            Callable function that executes the tool
        """
        if tool_name not in self.tools:
            logger.error(f"Tool {tool_name} not found in registry")
            return None

        server_name, tool_def = self.tools[tool_name]
        original_name = tool_def.get('name')

        def tool_executor(**kwargs) -> str:
            """Execute the MCP tool with given arguments"""
            connector = self.servers.get(server_name)
            if not connector:
                return json.dumps({
                    'success': False,
                    'error': f'MCP server {server_name} not available'
                })

            result = connector.execute_tool(original_name, kwargs)
            return json.dumps(result)

        # Set function metadata
        tool_executor.__name__ = tool_name
        tool_executor.__doc__ = tool_def.get('description', 'MCP tool')

        return tool_executor

    def get_all_tool_functions(self) -> Dict[str, Callable]:
        """
        Get all tools as executable functions

        Returns:
            Dictionary mapping tool names to executable functions
        """
        functions = {}

        for tool_name in self.tools.keys():
            func = self.create_tool_function(tool_name)
            if func:
                functions[tool_name] = func

        return functions


# Global registry instance
mcp_registry = MCPToolRegistry()


def load_user_mcp_servers(config_file: str = 'mcp_servers.json') -> int:
    """
    Load user-configured MCP servers from a JSON file

    Args:
        config_file: Path to the MCP servers configuration file

    Returns:
        Number of servers successfully loaded
    """
    if not os.path.exists(config_file):
        logger.info(f"No MCP server configuration found at {config_file}")
        return 0

    try:
        with open(config_file, 'r') as f:
            config = json.load(f)

        servers = config.get('servers', [])
        loaded = 0

        for server in servers:
            server_name = server.get('name')
            server_url = server.get('url')
            api_key = server.get('api_key')
            enabled = server.get('enabled', True)

            if not enabled:
                logger.info(f"Skipping disabled MCP server: {server_name}")
                continue

            if not server_name or not server_url:
                logger.warning(f"Invalid server configuration: {server}")
                continue

            if mcp_registry.add_server(server_name, server_url, api_key):
                loaded += 1

        # Discover all tools after loading servers
        if loaded > 0:
            total_tools = mcp_registry.discover_all_tools()
            logger.info(f"Loaded {loaded} MCP servers with {total_tools} total tools")

        return loaded

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse MCP server configuration: {e}")
        return 0
    except Exception as e:
        logger.error(f"Error loading MCP servers: {e}")
        return 0


def get_mcp_tools_for_autogen() -> List[Callable]:
    """
    Get all MCP tools as Autogen-compatible functions

    Returns:
        List of executable tool functions
    """
    functions = mcp_registry.get_all_tool_functions()
    return list(functions.values())


def get_mcp_tool_descriptions() -> List[Dict[str, Any]]:
    """
    Get MCP tool descriptions for agent configuration

    Returns:
        List of tool descriptions
    """
    return mcp_registry.get_tool_definitions()
