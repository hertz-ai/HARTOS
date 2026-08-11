"""Parallel-path fix: the local-shell auth decorator was copied across
shell_os_apis / shell_system_apis / shell_desktop_apis and DRIFTED —
``shell_os_apis`` trusted ``0.0.0.0`` as a local origin while the other two did
not, a privilege-escalation gap on the os-shell surface.

These tests prove:
  1. ``0.0.0.0`` is no longer trusted (the gap is closed).
  2. A real loopback still needs no token; a valid ``X-Shell-Token`` still works.
  3. All three surfaces now share the ONE canonical implementation (no drift).
"""
import os
import pytest

flask = pytest.importorskip("flask")

from integrations.agent_engine.shell_auth import require_shell_auth


def _call(remote_addr, headers=None):
    app = flask.Flask(__name__)

    @require_shell_auth
    def view():
        return "ok", 200

    with app.test_request_context(
        '/', environ_base={'REMOTE_ADDR': remote_addr}, headers=headers or {}
    ):
        return view()


def _status(rv):
    # allow → ("ok", 200); reject → (<json response>, 403)
    return rv[1]


def test_0000_is_not_trusted_as_local(monkeypatch):
    monkeypatch.delenv('HART_SHELL_TOKEN', raising=False)
    # remote 0.0.0.0, no token → must be REJECTED (was ALLOWED by shell_os_apis).
    assert _status(_call('0.0.0.0')) == 403


def test_loopback_allowed_without_token(monkeypatch):
    monkeypatch.delenv('HART_SHELL_TOKEN', raising=False)
    assert _call('127.0.0.1') == ("ok", 200)
    assert _call('::1') == ("ok", 200)


def test_token_gates_nonlocal_requests(monkeypatch):
    monkeypatch.setenv('HART_SHELL_TOKEN', 'secret')
    assert _call('10.0.0.5', headers={'X-Shell-Token': 'secret'}) == ("ok", 200)
    assert _status(_call('10.0.0.5', headers={'X-Shell-Token': 'wrong'})) == 403
    assert _status(_call('10.0.0.5')) == 403  # no token


def test_all_three_surfaces_share_one_canonical_decorator():
    """The whole point of the fix: no more parallel paths."""
    from integrations.agent_engine import shell_auth
    from integrations.agent_engine import shell_os_apis
    from integrations.agent_engine import shell_system_apis
    from integrations.agent_engine import shell_desktop_apis

    canonical = shell_auth.require_shell_auth
    assert shell_os_apis._require_shell_auth is canonical
    assert shell_system_apis._require_system_auth is canonical
    assert shell_desktop_apis._require_desktop_auth is canonical
