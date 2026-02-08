"""
Service Tools - Dynamic HTTP tool registry for external microservices.

Extends the MCP integration pattern to support any HTTP-based tool service
(Crawl4AI, AceStep, etc.) with health checking, auto-discovery, and
autogen/langchain compatible function generation.
"""

from .registry import ServiceToolRegistry, ServiceToolInfo, service_tool_registry
from .crawl4ai_tool import Crawl4AITool
from .acestep_tool import AceStepTool

__all__ = [
    "ServiceToolRegistry",
    "ServiceToolInfo",
    "service_tool_registry",
    "Crawl4AITool",
    "AceStepTool",
]
