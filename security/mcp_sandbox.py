"""
MCP Tool Sandboxing
Defends against CVE-2025-6514 (command injection via MCP) and
supply chain attacks through malicious MCP tools/skills.
"""

import os
import re
import logging
from typing import Dict, Any, List, Optional, Set
from urllib.parse import urlparse

logger = logging.getLogger('hevolve_security')

# Shell metacharacters that could enable command injection
_SHELL_METACHAR_PATTERN = re.compile(r'[;&|`${}()\n\r]')

# Path traversal patterns
_PATH_TRAVERSAL_PATTERN = re.compile(r'\.\.[\\/]')

# Dangerous command patterns
_DANGEROUS_CMD_PATTERNS = [
    re.compile(r'\b(rm|del|format|mkfs|dd|chmod|chown)\s', re.I),
    re.compile(r'\beval\s*\(', re.I),
    re.compile(r'\bexec\s*\(', re.I),
    re.compile(r'\b(curl|wget|nc|ncat)\s.*[-]', re.I),
    re.compile(r'\bos\.(system|popen|exec)', re.I),
    re.compile(r'\bsubprocess\.(call|run|Popen)', re.I),
    re.compile(r'\b__import__\s*\(', re.I),
]

# Max response size from MCP tools (1MB)
MAX_RESPONSE_SIZE = 1 * 1024 * 1024

# Max execution timeout (seconds)
MAX_EXECUTION_TIMEOUT = 60


class MCPSandbox:
    """
    Sandbox for MCP tool execution.
    Validates server URLs, tool names, and arguments before execution.
    """

    def __init__(self, allowed_servers: Optional[List[str]] = None,
                 allowed_tools: Optional[Set[str]] = None):
        self.allowed_servers: Set[str] = set(allowed_servers or [])
        self.allowed_tools: Set[str] = allowed_tools or set()

        # Load from environment
        env_servers = os.environ.get('MCP_ALLOWED_SERVERS', '')
        if env_servers:
            self.allowed_servers.update(
                s.strip() for s in env_servers.split(',') if s.strip()
            )

        # Always allow localhost
        self.allowed_servers.update(['localhost', '127.0.0.1'])

    def validate_server_url(self, url: str) -> bool:
        """
        Validate MCP server URL against allowlist.
        Returns True if allowed, False if blocked.
        """
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname or ''

            if not self.allowed_servers:
                # If no allowlist configured, only allow localhost
                return hostname in ('localhost', '127.0.0.1')

            if hostname in self.allowed_servers:
                return True

            logger.warning(f"MCP server blocked: {hostname} not in allowlist")
            return False

        except Exception as e:
            logger.error(f"Failed to parse MCP server URL: {e}")
            return False

    def validate_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> tuple:
        """
        Validate tool name and arguments before execution.
        Returns (is_safe, reason).
        """
        # Check tool allowlist
        if self.allowed_tools and tool_name not in self.allowed_tools:
            return False, f"Tool '{tool_name}' not in allowlist"

        # Check arguments for injection patterns
        for key, value in arguments.items():
            if not isinstance(value, str):
                continue

            # Shell metacharacters
            if _SHELL_METACHAR_PATTERN.search(value):
                logger.warning(
                    f"Shell metacharacter blocked in MCP tool arg '{key}': "
                    f"{value[:50]}..."
                )
                return False, f"Argument '{key}' contains shell metacharacters"

            # Path traversal
            if _PATH_TRAVERSAL_PATTERN.search(value):
                logger.warning(f"Path traversal blocked in MCP tool arg '{key}'")
                return False, f"Argument '{key}' contains path traversal"

            # Dangerous commands
            for pattern in _DANGEROUS_CMD_PATTERNS:
                if pattern.search(value):
                    logger.warning(
                        f"Dangerous command blocked in MCP tool arg '{key}': "
                        f"{value[:50]}..."
                    )
                    return False, f"Argument '{key}' contains dangerous command pattern"

        return True, ""

    def validate_response(self, response: Any) -> tuple:
        """
        Validate MCP tool response for data exfiltration patterns.
        Returns (is_safe, reason).
        """
        if isinstance(response, (str, bytes)):
            size = len(response)
            if size > MAX_RESPONSE_SIZE:
                return False, f"Response size {size} exceeds limit {MAX_RESPONSE_SIZE}"

        # Check for credential patterns in response
        response_str = str(response)
        credential_patterns = [
            re.compile(r'sk-[a-zA-Z0-9]{20,}'),
            re.compile(r'eyJ[a-zA-Z0-9_-]+\.eyJ'),
            re.compile(r'AIzaSy[a-zA-Z0-9_-]{33}'),
            re.compile(r'AKIA[0-9A-Z]{16}'),
        ]
        for pattern in credential_patterns:
            if pattern.search(response_str):
                logger.warning("Credential pattern detected in MCP response")
                return False, "Response contains potential credentials"

        return True, ""

    def get_timeout(self) -> int:
        """Get max execution timeout for MCP tools."""
        return MAX_EXECUTION_TIMEOUT
