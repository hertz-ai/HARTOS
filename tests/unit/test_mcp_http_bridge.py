"""Tests for MCP HTTP Bridge — REST exposure of local HARTOS MCP tools."""

import json
import sys
import os
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


@pytest.fixture(autouse=True)
def reset_tools():
    """Reset tool registry between tests."""
    from integrations.mcp import mcp_http_bridge
    mcp_http_bridge._tools_loaded = False
    mcp_http_bridge._local_tools.clear()
    yield
    mcp_http_bridge._tools_loaded = False
    mcp_http_bridge._local_tools.clear()


@pytest.fixture
def app():
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    from integrations.mcp.mcp_http_bridge import mcp_local_bp
    app.register_blueprint(mcp_local_bp)
    return app


@pytest.fixture
def client(app):
    """Test client that auto-injects the MCP bearer token on every request.

    The bridge gates /tools/execute behind a bearer token. The token is
    read-or-created lazily via _ensure_mcp_token. We grab it once and
    inject it into every request via a custom test client subclass.
    """
    from integrations.mcp.mcp_http_bridge import _ensure_mcp_token
    token = _ensure_mcp_token()

    class _AuthClient(app.test_client_class or app.test_client().__class__):
        def open(self, *args, **kwargs):
            headers = kwargs.pop('headers', {}) or {}
            if 'Authorization' not in headers:
                headers['Authorization'] = f'Bearer {token}'
            kwargs['headers'] = headers
            return super().open(*args, **kwargs)

    app.test_client_class = _AuthClient
    return app.test_client()


# ── Health endpoint ────────────────────────────────────────────

class TestMCPHealth:
    def test_health_returns_ok(self, client):
        resp = client.get('/api/mcp/local/health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'ok'
        assert 'tools' in data
        assert data['server'] == 'hartos-mcp-local'

    def test_health_reports_tool_count(self, client):
        resp = client.get('/api/mcp/local/health')
        data = resp.get_json()
        assert isinstance(data['tools'], int)
        assert data['tools'] >= 14  # At least 14 tools (grows as new tools are added)


# ── Tools list endpoint ────────────────────────────────────────

class TestMCPToolsList:
    def test_list_returns_tools_array(self, client):
        resp = client.get('/api/mcp/local/tools/list')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'tools' in data
        assert isinstance(data['tools'], list)

    def test_each_tool_has_required_fields(self, client):
        resp = client.get('/api/mcp/local/tools/list')
        data = resp.get_json()
        for tool in data['tools']:
            assert 'name' in tool
            assert 'description' in tool
            assert 'parameters' in tool

    def test_known_tools_present(self, client):
        resp = client.get('/api/mcp/local/tools/list')
        data = resp.get_json()
        tool_names = {t['name'] for t in data['tools']}
        # Core tools that must always exist
        required = {
            'list_agents', 'list_goals', 'agent_status',
            'list_recipes', 'system_health', 'social_query',
            'remember', 'recall',
        }
        assert required.issubset(tool_names), f"Missing: {required - tool_names}"

    def test_tool_parameters_have_schema(self, client):
        resp = client.get('/api/mcp/local/tools/list')
        data = resp.get_json()
        for tool in data['tools']:
            params = tool['parameters']
            assert params['type'] == 'object'
            assert 'properties' in params


# ── Tool execution endpoint ────────────────────────────────────

class TestMCPToolExecution:
    def test_execute_missing_tool_name_returns_400(self, client):
        resp = client.post('/api/mcp/local/tools/execute',
                          data=json.dumps({"arguments": {}}),
                          content_type='application/json')
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False

    def test_execute_unknown_tool_returns_404(self, client):
        resp = client.post('/api/mcp/local/tools/execute',
                          data=json.dumps({"tool": "nonexistent_tool_xyz"}),
                          content_type='application/json')
        assert resp.status_code == 404
        data = resp.get_json()
        assert data['success'] is False
        assert 'available_tools' in data

    def test_execute_empty_body_returns_400(self, client):
        resp = client.post('/api/mcp/local/tools/execute',
                          data='{}', content_type='application/json')
        assert resp.status_code == 400

    def test_execute_list_recipes(self, client):
        resp = client.post('/api/mcp/local/tools/execute',
                          data=json.dumps({"tool": "list_recipes", "arguments": {}}),
                          content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert 'result' in data
        assert 'count' in data['result']
        assert 'recipes' in data['result']

    def test_execute_with_bad_arguments(self, client):
        resp = client.post('/api/mcp/local/tools/execute',
                          data=json.dumps({"tool": "social_query", "arguments": {"bad_arg": 1}}),
                          content_type='application/json')
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False
        assert 'Invalid arguments' in data['error']


# ── Parameter extraction ───────────────────────────────────────

class TestParameterExtraction:
    def test_extract_parameters_basic(self):
        from integrations.mcp.mcp_http_bridge import _extract_parameters

        def sample_fn(name: str, count: int = 5):
            pass

        schema = _extract_parameters(sample_fn)
        assert schema['type'] == 'object'
        assert 'name' in schema['properties']
        assert 'count' in schema['properties']
        assert schema['properties']['count']['default'] == 5
        assert 'name' in schema['required']
        assert 'count' not in schema['required']

    def test_extract_parameters_no_args(self):
        from integrations.mcp.mcp_http_bridge import _extract_parameters
        schema = _extract_parameters(lambda: None)
        assert schema['properties'] == {}

    def test_extract_parameters_none(self):
        from integrations.mcp.mcp_http_bridge import _extract_parameters
        assert _extract_parameters(None) == {}

    def test_extract_parameters_types(self):
        from integrations.mcp.mcp_http_bridge import _extract_parameters

        def typed_fn(name: str, count: int, rate: float, flag: bool):
            pass

        schema = _extract_parameters(typed_fn)
        assert schema['properties']['name']['type'] == 'string'
        assert schema['properties']['count']['type'] == 'number'
        assert schema['properties']['rate']['type'] == 'number'
        assert schema['properties']['flag']['type'] == 'boolean'


# ── Tool loading ───────────────────────────────────────────────

class TestToolLoading:
    def test_loads_tools(self):
        from integrations.mcp.mcp_http_bridge import _load_tools, _local_tools
        _load_tools()
        assert len(_local_tools) >= 14  # Grows as new MCP tools are added

    def test_idempotent(self):
        from integrations.mcp import mcp_http_bridge
        mcp_http_bridge._load_tools()
        count1 = len(mcp_http_bridge._local_tools)
        mcp_http_bridge._load_tools()
        count2 = len(mcp_http_bridge._local_tools)
        assert count1 == count2

    def test_all_tools_have_callables(self):
        from integrations.mcp.mcp_http_bridge import _load_tools, _local_tools
        _load_tools()
        for t in _local_tools:
            assert callable(t['fn']), f"Tool {t['name']} has no callable"


# ── Auto-registration ─────────────────────────────────────────

class TestAutoRegistration:
    def test_auto_register_adds_to_registry(self):
        from integrations.mcp.mcp_http_bridge import auto_register_local_mcp
        from integrations.mcp.mcp_integration import mcp_registry
        mcp_registry.servers.pop('hartos_local', None)
        auto_register_local_mcp()
        assert 'hartos_local' in mcp_registry.servers
        connector = mcp_registry.servers['hartos_local']
        assert connector.connected is True
        assert '127.0.0.1' in connector.server_url

    def test_auto_register_idempotent(self):
        from integrations.mcp.mcp_http_bridge import auto_register_local_mcp
        from integrations.mcp.mcp_integration import mcp_registry
        mcp_registry.servers.pop('hartos_local', None)
        auto_register_local_mcp()
        auto_register_local_mcp()
        assert 'hartos_local' in mcp_registry.servers


# ── Port registry ─────────────────────────────────────────────

class TestPortRegistry:
    def test_mcp_port_registered(self):
        from core.port_registry import get_port
        port = get_port('mcp')
        assert port > 0
        assert port == 6791 or port == 682

    def test_mcp_port_env_override(self):
        from core import port_registry
        old_cache = port_registry._os_mode_cached
        try:
            port_registry._os_mode_cached = False
            with patch.dict(os.environ, {'HART_MCP_PORT': '9999'}):
                port = port_registry.get_port('mcp')
                assert port == 9999
        finally:
            port_registry._os_mode_cached = old_cache


# ── MCPServerConnector compatibility ───────────────────────────

class TestConnectorCompatibility:
    """Verify the REST API contract matches what MCPServerConnector expects."""

    def test_health_contract(self, client):
        """MCPServerConnector checks {url}/health for 200."""
        resp = client.get('/api/mcp/local/health')
        assert resp.status_code == 200

    def test_tools_list_contract(self, client):
        """MCPServerConnector expects {"tools": [...]} from {url}/tools/list."""
        resp = client.get('/api/mcp/local/tools/list')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'tools' in data
        assert isinstance(data['tools'], list)

    def test_tools_execute_contract(self, client):
        """MCPServerConnector sends POST {"tool": "...", "arguments": {...}}."""
        resp = client.post('/api/mcp/local/tools/execute',
                          data=json.dumps({"tool": "list_recipes", "arguments": {}}),
                          content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True


# ── End-to-end roundtrip ──────────────────────────────────────

class TestE2ERoundtrip:
    def test_discover_then_execute(self, client):
        """Simulate what MCPServerConnector does: discover, then execute."""
        # 1. Health check
        resp = client.get('/api/mcp/local/health')
        assert resp.status_code == 200

        # 2. Discover tools
        resp = client.get('/api/mcp/local/tools/list')
        tools = resp.get_json()['tools']
        tool_names = {t['name'] for t in tools}
        assert 'list_recipes' in tool_names

        # 3. Execute a tool
        resp = client.post('/api/mcp/local/tools/execute',
                          data=json.dumps({"tool": "list_recipes", "arguments": {}}),
                          content_type='application/json')
        assert resp.status_code == 200
        result = resp.get_json()
        assert result['success'] is True


# ── JSON-RPC 2.0 surface (#598) ───────────────────────────────
#
# Everything above tests the REST surface, and it all passes — which is
# precisely why this gap survived.  MCP-over-HTTP is not REST: a compliant
# client POSTs JSON-RPC 2.0 to ONE endpoint and never calls GET /tools/list.
#
# What we advertise (.claude/settings.local.json:19, and
# docs/architecture/hive_moe_architecture_map.md:873):
#     {"type": "http", "url": "http://localhost:5000/api/mcp/local"}
# What exists: no rule at that bare prefix at all -> 404 on the client's very
# first request.  Adding a route there is necessary but NOT sufficient: there
# is no "jsonrpc" envelope, no `id` correlation and no `initialize` handshake
# anywhere in the bridge, so a client that got past the 404 still could not
# speak to it.  Fixing only the 404 would be an inert fix.
#
# Third defect, independent of the other two: the descriptor emits
# "parameters", but MCP tools/list requires "inputSchema".  A client that
# tolerated both the URL and the shape would still see zero usable schemas.

RPC_URL = '/api/mcp/local'


def _rpc(client, method, params=None, rid=1):
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(RPC_URL, data=json.dumps(body),
                       content_type='application/json')


class TestJsonRpcSurface:
    def test_bare_prefix_is_routed(self, client):
        """The advertised URL must exist. Live probe returns
        404 {"error":"API endpoint not found","path":"/api/mcp/local"}."""
        resp = _rpc(client, 'initialize')
        assert resp.status_code != 404, (
            'no route at /api/mcp/local — the URL we advertise to MCP clients '
            'in .claude/settings.local.json 404s on the first request')

    def test_initialize_returns_jsonrpc_envelope(self, client):
        resp = _rpc(client, 'initialize', {"protocolVersion": "2024-11-05"}, rid=7)
        body = resp.get_json()
        assert body.get('jsonrpc') == '2.0', f'missing jsonrpc envelope: {body}'
        assert body.get('id') == 7, 'id must be echoed for request correlation'
        assert 'result' in body
        assert 'serverInfo' in body['result']

    def test_tools_list_uses_inputSchema_not_parameters(self, client):
        """MCP names this field inputSchema. The REST projection calls it
        `parameters` (mcp_http_bridge.py ~:876) — a client reading the spec
        finds no schema at all."""
        body = _rpc(client, 'tools/list', rid=2).get_json()
        tools = (body.get('result') or {}).get('tools')
        assert isinstance(tools, list) and tools, f'no tools in result: {body}'
        assert 'inputSchema' in tools[0], (
            f"tool descriptor exposes {sorted(tools[0])} — MCP requires "
            f"'inputSchema'")

    def test_tools_call_returns_the_mcp_content_shape(self, client):
        """Asserts the real CallToolResult contract, not merely 'a result'.

        An earlier draft of this test only checked `'result' in body`, which
        would have passed a non-compliant payload — the same leniency that let
        24 green REST tests sit on top of an unusable feature.
        """
        body = _rpc(client, 'tools/call',
                    {"name": "list_recipes", "arguments": {}}, rid=3).get_json()
        assert body.get('id') == 3
        result = body.get('result')
        assert isinstance(result, dict), f'tools/call returned no result: {body}'
        content = result.get('content')
        assert isinstance(content, list) and content, (
            f"MCP tools/call must return a content[] array, got {result}")
        assert content[0].get('type') == 'text'
        assert isinstance(content[0].get('text'), str)
        assert result.get('isError') is False

    def test_tools_call_unknown_tool_is_a_jsonrpc_error(self, client):
        body = _rpc(client, 'tools/call',
                    {"name": "no_such_tool_xyz", "arguments": {}}, rid=4).get_json()
        assert body.get('id') == 4
        assert (body.get('error') or {}).get('code') == -32602, (
            f'expected -32602 Invalid params for an unknown tool, got {body}')

    def test_unknown_method_is_a_jsonrpc_error(self, client):
        body = _rpc(client, 'no/such/method', rid=9).get_json()
        assert body.get('id') == 9, 'id must be echoed even on error'
        assert (body.get('error') or {}).get('code') == -32601, (
            f'expected JSON-RPC -32601 Method not found, got {body}')

    def test_jsonrpc_endpoint_inherits_the_auth_gate(self, app):
        """SECURITY: the gate is @mcp_local_bp.before_request (bridge :237),
        so a route added to the SAME blueprint is covered automatically.  This
        pins that — a JSON-RPC endpoint registered outside the blueprint (or
        with its own bespoke auth) would be an unauthenticated tool-execution
        hole and a second auth path.
        """
        raw = app.test_client()          # no Authorization header injected
        resp = raw.post(RPC_URL,
                        data=json.dumps({"jsonrpc": "2.0", "id": 1,
                                         "method": "tools/list"}),
                        content_type='application/json')
        assert resp.status_code in (401, 403), (
            f'unauthenticated JSON-RPC call returned {resp.status_code} — '
            f'the endpoint is not behind the blueprint auth gate')


class TestRestSurfaceUnchangedByJsonRpc:
    """Zero-regression: the three REST routes have existing callers and must
    keep their current shape, INCLUDING the `parameters` field name.  The
    inputSchema rename belongs only in the JSON-RPC projection."""

    def test_rest_tools_list_still_returns_parameters(self, client):
        body = client.get('/api/mcp/local/tools/list').get_json()
        assert 'tools' in body and body['tools']
        assert 'parameters' in body['tools'][0]

    def test_rest_execute_still_uses_tool_and_arguments(self, client):
        resp = client.post('/api/mcp/local/tools/execute',
                           data=json.dumps({"tool": "list_recipes",
                                            "arguments": {}}),
                           content_type='application/json')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
