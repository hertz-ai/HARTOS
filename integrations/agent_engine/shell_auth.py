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
    if expected and token and token == expected:
        return True, None, None
    return False, jsonify({'error': 'Shell API: local access only'}), 403


def require_shell_auth(f):
    """Decorator enforcing :func:`shell_auth_ok` on a Flask view."""
    @wraps(f)
    def decorated(*args, **kwargs):
        ok, err, status = shell_auth_ok()
        if not ok:
            return err, status
        return f(*args, **kwargs)
    return decorated
