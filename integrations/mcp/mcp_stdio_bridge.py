"""HARTOS MCP — the STDIO transport, dependency-free.

Claude Code loads an MCP server two ways: a `command` (stdio) or an `http`
URL. HARTOS already had two transports over ONE tool surface (_tool_impls):

  * mcp_server.py     — stdio via FastMCP     (needs `mcp` + pydantic v2)
  * mcp_http_bridge.py— REST at /api/mcp/local (custom shape, not MCP proto)

Neither reaches the resident Claude Code cleanly: FastMCP is absent from the
node's pinned nixpkgs (24.11) AND conflicts with the app's pydantic 1.10.x,
and Claude Code cannot load the bridge's custom REST as an MCP server. So the
copilot — a peer node whose whole point is to WEAR HARTOS and drive it — had
no way to see the 48 tools.

This is the missing transport: a pure-stdlib MCP-protocol server over stdio
that REUSES the bridge's registry verbatim (`_invoke_tool`, `_local_tools`),
adding zero tool logic. Same single source (_tool_impls) as the other two
transports — a third front-end, NOT a parallel path. No FastMCP, no pydantic
v2, no new nix dependency: it runs in the node's own interpreter, which
already imports the bridge (it serves /api/mcp/local live).

Wire it in Claude Code's .mcp.json:

    { "mcpServers": { "hartos": {
        "command": "python",
        "args": ["-m", "integrations.mcp.mcp_stdio_bridge"] } } }

Protocol: JSON-RPC 2.0, newline-delimited, over stdin/stdout (MCP stdio
transport). Only the methods a client actually calls are implemented;
everything else returns a proper JSON-RPC error rather than hanging.
"""
import json
import sys

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "hartos", "version": "1.0.0"}


def _load():
    """Populate + return the ONE shared tool registry. Imported lazily so a
    --selftest / import never drags the backend in before it is needed."""
    from integrations.mcp.mcp_http_bridge import _load_tools, _local_tools
    _load_tools()
    return _local_tools


def _tools_list():
    out = []
    for t in _load():
        out.append({
            "name": t["name"],
            "description": t.get("description", ""),
            # MCP uses `inputSchema`; the bridge stored the same JSON Schema
            # under `parameters`. Same object, MCP's field name.
            "inputSchema": t.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _tools_call(name, arguments):
    # The bridge's execute is the single source of truth for dispatch,
    # arg-alias canonicalization, and error shaping. Reuse it verbatim.
    from integrations.mcp.mcp_http_bridge import _invoke_tool
    body, _status = _invoke_tool(name, arguments or {})
    if body.get("success"):
        payload = body.get("result")
        text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    # A tool error is reported IN-BAND (isError) so the model sees it and can
    # adapt, rather than as a transport-level JSON-RPC error that aborts.
    return {"content": [{"type": "text", "text": body.get("error", "tool error")}],
            "isError": True}


def _handle(msg):
    """Return a JSON-RPC response dict, or None for notifications."""
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    # Notifications (no id) get no response. 'initialized' is the common one.
    if mid is None:
        return None

    try:
        if method == "initialize":
            return _ok(mid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": _tools_list()})
        if method == "tools/call":
            return _ok(mid, _tools_call(params.get("name"), params.get("arguments")))
        # Unknown method: JSON-RPC "method not found".
        return _err(mid, -32601, "method not found: %s" % method)
    except Exception as e:  # never let one bad call kill the transport
        return _err(mid, -32603, "internal error: %s" % e)


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main():
    # Line-delimited JSON-RPC over stdio. Read a line, handle, write a line.
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue  # not our frame; ignore rather than crash the session
        resp = _handle(msg)
        if resp is not None:
            out.write(json.dumps(resp) + "\n")
            out.flush()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        # Prove the registry loads and the three core methods answer, without
        # a live client. Exits non-zero on any failure (CI-usable).
        init = _handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        lst = _handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        n = len(lst["result"]["tools"])
        assert init["result"]["serverInfo"]["name"] == "hartos"
        assert n > 0, "no tools exposed"
        print("selftest OK: %d tools over stdio" % n)
        sys.exit(0)
    main()
