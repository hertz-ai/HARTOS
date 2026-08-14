"""Canonical local-shell authentication — ONE implementation for every
``shell_*`` API surface (os / system / desktop / installer).

Historically each surface carried its own copy of this check and they DRIFTED:
``shell_os_apis`` trusted ``0.0.0.0`` as a "local" origin, while
``shell_system_apis`` and ``shell_desktop_apis`` (verbatim copies of each other)
correctly did not. ``0.0.0.0`` is the "bind any interface" sentinel, never a real
loopback *client* source address — trusting it let an unauthenticated request
whose ``remote_addr`` reads ``0.0.0.0`` reach the os-shell routes with no token,
a privilege-escalation gap the other two surfaces did not have.

This module is the single source of truth so the surfaces can never drift again:
``shell_os_apis._require_shell_auth``, ``shell_system_apis._require_system_auth``
and ``shell_desktop_apis._require_desktop_auth`` all alias :func:`require_shell_auth`.
"""
import hmac
import os
from functools import wraps

# Loopback origins that need no token. 0.0.0.0 is DELIBERATELY excluded: it is
# the "bind any interface" sentinel, not a loopback client address, so a request
# whose remote_addr is 0.0.0.0 is anomalous/spoofed and must present a token.
LOCAL_ORIGINS = ('127.0.0.1', '::1', 'localhost')


def shell_auth_ok():
    """Return ``(ok, error_json, status)`` for the current Flask request.

    Authorized when the request originates from a loopback origin OR carries a
    valid ``X-Shell-Token`` header (set at desktop login). On success returns
    ``(True, None, None)``; otherwise ``(False, <json>, 403)``.
    """
    from flask import request, jsonify
    remote = request.remote_addr or ''
    if remote in LOCAL_ORIGINS:
        return True, None, None
    token = request.headers.get('X-Shell-Token', '')
    expected = os.environ.get('HART_SHELL_TOKEN', '')
    if expected and token and _tokens_match(token, expected):
        return True, None, None
    return False, jsonify({'error': 'Shell API: local access only'}), 403


def _tokens_match(token: str, expected: str) -> bool:
    """CONSTANT-TIME token comparison.

    A plain ``token == expected`` short-circuits on the first differing byte, so
    the time it takes to reject leaks how many leading bytes were right. Against
    an attacker who can time responses that turns guessing a shared secret from
    infeasible into a per-byte search. ``hmac.compare_digest`` compares in time
    that does not depend on where the mismatch is.

    Both sides are encoded to bytes first: ``compare_digest`` raises TypeError on
    ``str`` containing non-ASCII, and this token arrives from an attacker-controlled
    HTTP header — so a non-ASCII header would otherwise raise inside the auth check
    rather than simply failing it. Encoding makes a malformed token a clean False.
    """
    try:
        return hmac.compare_digest(token.encode('utf-8'), expected.encode('utf-8'))
    except (AttributeError, UnicodeError):
        # Non-string input of any shape is a failed comparison, never an exception
        # escaping the auth boundary.
        return False


def require_shell_auth(f):
    """Decorator enforcing :func:`shell_auth_ok` on a Flask view."""
    @wraps(f)
    def decorated(*args, **kwargs):
        ok, err, status = shell_auth_ok()
        if not ok:
            return err, status
        return f(*args, **kwargs)
    return decorated
