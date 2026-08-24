"""The stdio MCP transport reuses the SAME registry the HTTP bridge serves,
so Claude Code can wear all 48 HARTOS tools with zero new deps (no FastMCP,
no pydantic v2 — the reason the mcp_server.py transport could not be wired).
These tests mock the shared registry so they run without a live backend."""
import json
from unittest.mock import patch

import integrations.mcp.mcp_stdio_bridge as sb


_FAKE_TOOLS = [
    {"name": "system_health", "description": "Full system health check",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "dispatch_goal", "description": "Dispatch a goal",
     "parameters": {"type": "object", "properties": {"goal_id": {"type": "string"}},
                    "required": ["goal_id"]}},
]


def _with_fake_registry():
    """Patch the bridge functions the stdio transport imports lazily."""
    import integrations.mcp.mcp_http_bridge as bridge
    p1 = patch.object(bridge, "_load_tools", lambda: None)
    p2 = patch.object(bridge, "_local_tools", _FAKE_TOOLS)
    return p1, p2


def test_initialize_advertises_hartos_and_tools_capability():
    r = sb._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r["result"]["serverInfo"]["name"] == "hartos"
    assert "tools" in r["result"]["capabilities"]
    assert r["result"]["protocolVersion"] == sb.PROTOCOL_VERSION


def test_tools_list_maps_parameters_to_inputschema():
    p1, p2 = _with_fake_registry()
    with p1, p2:
        r = sb._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = r["result"]["tools"]
    assert [t["name"] for t in tools] == ["system_health", "dispatch_goal"]
    # MCP requires inputSchema, not the bridge's internal `parameters` key.
    assert all("inputSchema" in t and "parameters" not in t for t in tools)
    assert tools[1]["inputSchema"]["required"] == ["goal_id"]


def test_tools_call_reuses_invoke_tool_and_wraps_result():
    import integrations.mcp.mcp_http_bridge as bridge
    with patch.object(bridge, "_invoke_tool",
                      return_value=({"success": True, "result": {"ok": 1}}, 200)) as inv:
        r = sb._handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "system_health", "arguments": {}}})
    inv.assert_called_once_with("system_health", {})
    assert r["result"]["isError"] is False
    assert json.loads(r["result"]["content"][0]["text"]) == {"ok": 1}


def test_tool_error_is_in_band_not_transport_error():
    import integrations.mcp.mcp_http_bridge as bridge
    with patch.object(bridge, "_invoke_tool",
                      return_value=({"success": False, "error": "boom"}, 500)):
        r = sb._handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "x", "arguments": {}}})
    # A tool failure must surface as isError content (the model can adapt),
    # NOT a JSON-RPC error that aborts the turn.
    assert "error" not in r
    assert r["result"]["isError"] is True
    assert "boom" in r["result"]["content"][0]["text"]


def test_notification_gets_no_response():
    assert sb._handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_is_method_not_found():
    r = sb._handle({"jsonrpc": "2.0", "id": 9, "method": "nope"})
    assert r["error"]["code"] == -32601
