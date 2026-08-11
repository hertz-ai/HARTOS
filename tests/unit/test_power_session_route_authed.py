"""Parallel-path fix #9: ``/api/shell/session/<action>`` (lock / logout / suspend
/ shutdown / restart / firmware) was **UNAUTHENTICATED**, while its sibling
``/api/shell/power/action`` carried ``@_require_shell_auth`` — a drifted power
surface that let any reachable caller power off the box. Both power surfaces now
share the ONE canonical local-shell gate.

Tests:
  1. the gate the route now uses rejects a non-local, tokenless request (403)
  2. the ``shell_session`` route source is decorated with ``@_require_shell_auth``
"""
import re
from pathlib import Path
import pytest

flask = pytest.importorskip("flask")

from integrations.agent_engine.shell_os_apis import _require_shell_auth


def test_gate_rejects_nonlocal_tokenless_request(monkeypatch):
    monkeypatch.delenv('HART_SHELL_TOKEN', raising=False)
    app = flask.Flask(__name__)

    @_require_shell_auth
    def view():
        return "ok", 200

    with app.test_request_context('/', environ_base={'REMOTE_ADDR': '10.0.0.9'}):
        rv = view()
    assert rv[1] == 403  # a power action is refused without local origin / token


def test_session_route_is_decorated_with_auth():
    src = (Path(__file__).resolve().parents[2]
           / 'integrations' / 'agent_engine' / 'liquid_ui_service.py')
    text = src.read_text(encoding='utf-8')
    m = re.search(
        r"@app\.route\('/api/shell/session/<action>'.*?\n\s*(@[\w_]+)\s*\n\s*def shell_session",
        text, re.DOTALL)
    assert m is not None, "session route + its decorator not found"
    assert '_require_shell_auth' in m.group(1), \
        "the power/session route is NOT gated by _require_shell_auth"
