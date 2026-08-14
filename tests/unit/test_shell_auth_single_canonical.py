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


def test_token_comparison_is_constant_time():
    """A plain `==` short-circuits on the first wrong byte, so rejection time
    leaks how many leading bytes were correct — turning a shared-secret guess
    into a per-byte search for anyone who can time responses. The comparison must
    go through hmac.compare_digest."""
    import hmac as _hmac
    from unittest.mock import patch
    from integrations.agent_engine import shell_auth

    with patch.object(_hmac, 'compare_digest', wraps=_hmac.compare_digest) as cd:
        assert shell_auth._tokens_match('secret', 'secret') is True
        assert cd.called, (
            "token comparison did not use hmac.compare_digest — a plain == leaks "
            "the length of the correct prefix through timing")


def test_token_comparison_rejects_without_raising():
    """The token is attacker-controlled and arrives from an HTTP header. A
    non-ASCII or non-string value must FAIL the check, never raise out of the
    auth boundary (hmac.compare_digest raises TypeError on non-ASCII str)."""
    from integrations.agent_engine.shell_auth import _tokens_match
    assert _tokens_match('wrong', 'secret') is False
    assert _tokens_match('tokén-with-ünicode', 'secret') is False   # no TypeError
    assert _tokens_match('', 'secret') is False
    assert _tokens_match(None, 'secret') is False                   # no AttributeError


def test_a_unicode_token_is_rejected_through_the_real_route(monkeypatch):
    """End to end: the degenerate input above must surface as a clean 403, not a
    500 from an exception escaping the decorator."""
    monkeypatch.setenv('HART_SHELL_TOKEN', 'secret')
    assert _status(_call('10.0.0.5', headers={'X-Shell-Token': 'tokén'})) == 403


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
