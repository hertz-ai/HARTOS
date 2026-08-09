"""Behavioural tests for integrations/mcp/mcp_integration.py.

The unit under test is ``MCPServerConnector.execute_tool`` — the trust
boundary between HARTOS and an *untrusted* external MCP server loaded from
``mcp_servers.json``.  Three sandbox gates protect it:

  * ``validate_server_url``  — SSRF / server-allowlist gate (before the call)
  * ``validate_tool_call``   — injection / DLP gate       (before the call)
  * ``validate_response``    — exfiltration gate           (after the call)

The security-critical invariants these tests pin down (and which a reorder,
a broadened ``except``, or a deleted gate would silently break — with the
legacy test suite still green):

  1. a rejected server URL short-circuits BEFORE the outbound ``pooled_post``;
  2. a rejected tool call short-circuits BEFORE the outbound ``pooled_post``;
  3. a flagged response is dropped and never returned to the caller;
  4. the gates run in the right ORDER relative to the network call;
  5. the ``except ImportError`` blocks fail OPEN (documented, risky behaviour).

Only the boundaries are mocked: the network (``pooled_get`` / ``pooled_post``)
and, where determinism demands it, the sandbox itself.  The *real*
``security.mcp_sandbox.MCPSandbox`` is used for the end-to-end integration
tests so the wiring is proven against the genuine gate logic, not a stub.
"""

import json

import pytest

# Skip cleanly rather than error the whole file if an unrelated dependency of
# the import chain is unavailable in a degraded environment.
mcp_integration = pytest.importorskip("integrations.mcp.mcp_integration")

from unittest import mock  # noqa: E402

import requests  # noqa: E402

MCPServerConnector = mcp_integration.MCPServerConnector
MCPToolRegistry = mcp_integration.MCPToolRegistry


# ── test doubles ─────────────────────────────────────────────────────────────
class _FakeResponse:
    """Minimal stand-in for a requests.Response as execute_tool consumes it."""

    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = {} if json_body is None else json_body
        self.text = text

    def json(self):
        return self._json_body


def _connected_connector(url="http://localhost:9000", api_key=None):
    c = MCPServerConnector("srv", url, api_key)
    c.connected = True  # bypass the network connect() for execute_tool tests
    return c


@pytest.fixture(autouse=True)
def _deterministic_sandbox_env(monkeypatch):
    """Make the REAL MCPSandbox deterministic: no ambient allowlist, so its
    default policy (localhost-only) holds regardless of the host's env."""
    monkeypatch.delenv("MCP_ALLOWED_SERVERS", raising=False)


# ── 1) real-sandbox integration: the gates actually protect ──────────────────
class TestExecuteToolRealSandbox:
    def test_disallowed_url_rejected_before_outbound_post(self):
        """A non-localhost server (no allowlist) is blocked and NO outbound
        request is made — the SSRF gate short-circuits."""
        conn = _connected_connector(url="http://evil.example.com")
        post = mock.Mock()
        with mock.patch.object(mcp_integration, "pooled_post", post):
            result = conn.execute_tool("read", {"q": "hello"})

        post.assert_not_called()
        assert result["success"] is False
        assert "not allowed" in result["error"].lower()
        assert "evil.example.com" in result["error"]

    def test_injection_tool_call_rejected_before_outbound_post(self):
        """Shell-metacharacter args are blocked by validate_tool_call BEFORE
        any request leaves the process."""
        conn = _connected_connector(url="http://localhost:9000")
        post = mock.Mock()
        with mock.patch.object(mcp_integration, "pooled_post", post):
            result = conn.execute_tool("run", {"cmd": "ls; rm -rf /"})

        post.assert_not_called()
        assert result["success"] is False
        assert "rejected" in result["error"].lower()
        assert "shell metacharacter" in result["error"].lower()

    def test_flagged_response_is_dropped(self):
        """A 200 response whose body carries a credential pattern is dropped:
        the caller gets a rejection, never the raw exfiltrated value."""
        conn = _connected_connector(url="http://localhost:9000")
        leaked = "sk-abcdefghijklmnopqrstuvwx"  # matches sandbox credential regex
        post = mock.Mock(return_value=_FakeResponse(200, {"result": leaked}))
        with mock.patch.object(mcp_integration, "pooled_post", post):
            result = conn.execute_tool("read", {"q": "hello"})

        post.assert_called_once()  # the call DID go out
        assert result["success"] is False
        assert "response rejected" in result["error"].lower()
        # the credential value must not survive into what we hand back
        assert leaked not in json.dumps(result)

    def test_clean_call_reaches_server_and_returns_result(self):
        """Allowed URL + clean args + clean response → the server's result is
        returned verbatim and the request targets /tools/execute."""
        conn = _connected_connector(url="http://localhost:9000")
        server_result = {"success": True, "result": {"answer": 42}}
        post = mock.Mock(return_value=_FakeResponse(200, server_result))
        with mock.patch.object(mcp_integration, "pooled_post", post):
            result = conn.execute_tool("read", {"q": "hello"})

        post.assert_called_once()
        assert result == server_result
        # url is positional; payload/method are keyword
        args, kwargs = post.call_args
        assert args[0] == "http://localhost:9000/tools/execute"
        assert kwargs["json"] == {"tool": "read", "arguments": {"q": "hello"}}

    def test_api_key_forwarded_as_bearer_header(self):
        conn = _connected_connector(url="http://localhost:9000", api_key="secret-key")
        post = mock.Mock(return_value=_FakeResponse(200, {"success": True}))
        with mock.patch.object(mcp_integration, "pooled_post", post):
            conn.execute_tool("read", {"q": "hello"})

        _, kwargs = post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer secret-key"


# ── 2) fail-open + error/degrade paths ───────────────────────────────────────
class TestExecuteToolFailOpenAndErrors:
    def test_sandbox_import_error_fails_open(self):
        """Characterise the documented fail-open: when the sandbox module can't
        be imported, execute_tool proceeds to the outbound call AND skips
        response validation. Even a would-be-blocked URL goes through — this is
        the risk the `except ImportError: pass` blocks accept, and the assertion
        that would flip if that behaviour ever changed."""
        conn = _connected_connector(url="http://evil.example.com")
        post = mock.Mock(return_value=_FakeResponse(200, {"ok": True}))
        with mock.patch.dict("sys.modules", {"security.mcp_sandbox": None}), \
                mock.patch.object(mcp_integration, "pooled_post", post):
            result = conn.execute_tool("read", {"q": "hello"})

        post.assert_called_once()  # fail-OPEN: disallowed URL still called out
        assert result == {"ok": True}  # response returned unvalidated

    def test_not_connected_returns_error_without_post(self):
        conn = MCPServerConnector("srv", "http://localhost:9000")
        assert conn.connected is False
        post = mock.Mock()
        with mock.patch.object(mcp_integration, "pooled_post", post):
            result = conn.execute_tool("read", {"q": "hello"})

        post.assert_not_called()
        assert result["success"] is False
        assert "not connected" in result["error"].lower()

    def test_non_200_returns_http_error(self):
        conn = _connected_connector(url="http://localhost:9000")
        post = mock.Mock(return_value=_FakeResponse(500, text="boom"))
        with mock.patch.object(mcp_integration, "pooled_post", post):
            result = conn.execute_tool("read", {"q": "hello"})

        assert result["success"] is False
        assert "HTTP 500" in result["error"]
        assert "boom" in result["error"]

    def test_request_exception_is_caught(self):
        conn = _connected_connector(url="http://localhost:9000")
        post = mock.Mock(
            side_effect=requests.exceptions.ConnectionError("conn refused")
        )
        with mock.patch.object(mcp_integration, "pooled_post", post):
            result = conn.execute_tool("read", {"q": "hello"})

        assert result["success"] is False
        assert "conn refused" in result["error"]


# ── 3) mocked-sandbox: exact wiring, argument fidelity, and ORDER ────────────
class TestExecuteToolSandboxWiring:
    def test_all_three_gates_invoked_with_expected_arguments(self):
        sandbox = mock.Mock()
        sandbox.validate_server_url.return_value = True
        sandbox.validate_tool_call.return_value = (True, "")
        sandbox.validate_response.return_value = (True, "")
        conn = _connected_connector(url="http://localhost:9000")
        body = {"ok": 1}
        post = mock.Mock(return_value=_FakeResponse(200, body))

        with mock.patch("security.mcp_sandbox.MCPSandbox", return_value=sandbox), \
                mock.patch.object(mcp_integration, "pooled_post", post):
            result = conn.execute_tool("read", {"q": "hi"})

        sandbox.validate_server_url.assert_called_once_with("http://localhost:9000")
        sandbox.validate_tool_call.assert_called_once_with("read", {"q": "hi"})
        sandbox.validate_response.assert_called_once_with(body)
        assert result == body

    def test_tool_call_rejection_reason_is_propagated(self):
        """The rejection *reason* the gate returns must reach the caller, and
        the outbound call must not fire — decoupled from the real regex so a
        gate that returns a custom reason is still honoured."""
        sandbox = mock.Mock()
        sandbox.validate_server_url.return_value = True
        sandbox.validate_tool_call.return_value = (False, "custom-reason-xyz")
        conn = _connected_connector(url="http://localhost:9000")
        post = mock.Mock()

        with mock.patch("security.mcp_sandbox.MCPSandbox", return_value=sandbox), \
                mock.patch.object(mcp_integration, "pooled_post", post):
            result = conn.execute_tool("read", {"q": "hi"})

        post.assert_not_called()
        assert result["success"] is False
        assert "custom-reason-xyz" in result["error"]

    def test_gates_run_in_order_around_the_network_call(self):
        """Ordering guard: url + tool-call gates run BEFORE the POST, the
        response gate AFTER. A reorder that lets the request out before
        validation (the exact regression the task warns about) flips this."""
        events = []
        sandbox = mock.Mock()
        sandbox.validate_server_url.side_effect = (
            lambda url: events.append("url") or True
        )
        sandbox.validate_tool_call.side_effect = (
            lambda name, args: events.append("call") or (True, "")
        )
        sandbox.validate_response.side_effect = (
            lambda resp: events.append("resp") or (True, "")
        )

        def _post(*a, **k):
            events.append("post")
            return _FakeResponse(200, {"ok": 1})

        conn = _connected_connector(url="http://localhost:9000")
        with mock.patch("security.mcp_sandbox.MCPSandbox", return_value=sandbox), \
                mock.patch.object(mcp_integration, "pooled_post", _post):
            conn.execute_tool("read", {"q": "hi"})

        assert events == ["url", "call", "post", "resp"]


# ── 4) connect() / discover_tools() boundary behaviour ──────────────────────
class TestConnectAndDiscover:
    def test_connect_success_sets_connected(self):
        conn = MCPServerConnector("srv", "http://localhost:9000")
        get = mock.Mock(return_value=_FakeResponse(200))
        with mock.patch.object(mcp_integration, "pooled_get", get):
            assert conn.connect() is True
        assert conn.connected is True

    def test_connect_non_200_stays_disconnected(self):
        conn = MCPServerConnector("srv", "http://localhost:9000")
        get = mock.Mock(return_value=_FakeResponse(503))
        with mock.patch.object(mcp_integration, "pooled_get", get):
            assert conn.connect() is False
        assert conn.connected is False

    def test_connect_request_exception_is_caught(self):
        conn = MCPServerConnector("srv", "http://localhost:9000")
        get = mock.Mock(side_effect=requests.exceptions.Timeout("slow"))
        with mock.patch.object(mcp_integration, "pooled_get", get):
            assert conn.connect() is False
        assert conn.connected is False

    def test_discover_tools_requires_connection(self):
        conn = MCPServerConnector("srv", "http://localhost:9000")  # not connected
        get = mock.Mock()
        with mock.patch.object(mcp_integration, "pooled_get", get):
            assert conn.discover_tools() == []
        get.assert_not_called()

    def test_discover_tools_returns_server_list(self):
        conn = _connected_connector(url="http://localhost:9000")
        tools = [{"name": "a"}, {"name": "b"}]
        get = mock.Mock(return_value=_FakeResponse(200, {"tools": tools}))
        with mock.patch.object(mcp_integration, "pooled_get", get):
            assert conn.discover_tools() == tools
        assert conn.tools == tools


# ── 5) registry → connector → sandbox end-to-end (public callable) ───────────
class TestRegistryToolFunctionHonoursSandbox:
    def test_generated_tool_executor_enforces_url_gate(self):
        """The callable autogen actually invokes (create_tool_function's
        closure) must route through execute_tool, so the sandbox gate holds for
        a disallowed server and no request goes out. Result is JSON-serialised."""
        registry = MCPToolRegistry()
        conn = _connected_connector(url="http://evil.example.com")
        registry.servers["srv"] = conn
        registry.tools["srv_read"] = ("srv", {"name": "read"})

        func = registry.create_tool_function("srv_read")
        assert func is not None

        post = mock.Mock()
        with mock.patch.object(mcp_integration, "pooled_post", post):
            raw = func(q="hello")

        post.assert_not_called()
        payload = json.loads(raw)  # tool_executor json.dumps the result
        assert payload["success"] is False
        assert "not allowed" in payload["error"].lower()
