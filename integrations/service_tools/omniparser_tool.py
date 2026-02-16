"""
OmniParser tool wrapper — screen parsing and computer use.

Wraps the existing VLMAgentContext (vlm_agent_integration.py) as a
ServiceTool for unified registry. OmniParser is an external service
the user starts separately.

Service: OmniParser (C:\\Users\\sathi\\PycharmProjects\\OmniParser)
Port 8080: Screen parsing (FastAPI, /parse/, /probe/)
Port 5001: VLM agent RPC (Flask, /execute_action)
"""

from .registry import ServiceToolInfo, service_tool_registry


class OmniParserTool:
    """Thin wrapper to register OmniParser with the ServiceToolRegistry."""

    DEFAULT_PARSER_URL = "http://localhost:8080"
    DEFAULT_VLM_URL = "http://localhost:5001"

    @classmethod
    def create_tool_info(cls, parser_url: str = None,
                         vlm_url: str = None) -> ServiceToolInfo:
        parser = parser_url or cls.DEFAULT_PARSER_URL
        vlm = vlm_url or cls.DEFAULT_VLM_URL
        return ServiceToolInfo(
            name="omniparser",
            description=(
                "Screen parsing and computer use. Parses the user's screen "
                "to identify UI elements, then executes actions (click, type, "
                "scroll, hotkey) to control the computer on the user's behalf."
            ),
            base_url=parser,
            endpoints={
                "parse_screen": {
                    "path": "/parse/",
                    "method": "POST",
                    "description": (
                        "Parse the current screen to identify UI elements. "
                        "Returns list of detected elements with bounding boxes, "
                        "labels, and a screenshot."
                    ),
                    "params_schema": {
                        "include_som": {"type": "boolean", "description": "Include Set-of-Mark overlay", "default": True},
                    },
                },
                "execute_action": {
                    "path": "/execute_action",
                    "method": "POST",
                    "description": (
                        "Execute a computer action via VLM agent. "
                        "Input: 'action' (type/click/scroll/hotkey/etc), "
                        "'parameters' (dict with action-specific params like "
                        "'text', 'x', 'y', 'key'). "
                        "Sent to VLM agent on port 5001."
                    ),
                    "params_schema": {
                        "action": {"type": "string", "description": "Action type: type, left_click, right_click, scroll_up, scroll_down, hotkey, wait"},
                        "parameters": {"type": "object", "description": "Action parameters (text, x, y, key, etc.)"},
                    },
                },
            },
            health_endpoint="/probe",
            tags=["computer-use", "screen", "ui", "automation", "omniparser"],
            timeout=30,
        )

    @classmethod
    def register(cls, parser_url: str = None, vlm_url: str = None) -> bool:
        """Register OmniParser with the global service_tool_registry."""
        tool_info = cls.create_tool_info(parser_url, vlm_url)
        return service_tool_registry.register_tool(tool_info)
