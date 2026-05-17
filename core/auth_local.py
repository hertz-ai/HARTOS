"""Localhost-or-token decorator for HARTOS admin/control routes.

Port of the canonical Nunba `routes/auth.py:require_local_or_token`
pattern into HARTOS, where multiple endpoints (vlm_stop, prompts/sync,
diag) need the same "trusted localhost OR valid Bearer token" semantic.

Why this pattern instead of plain @require_auth:
    Nunba's bundled install runs HARTOS on 127.0.0.1:5000 and has the
    desktop tray (Tk indicator window, Python `app.py` / `main.py`)
    POST to /api/vlm/stop directly — without a logged-in JWT context.
    Adding plain @require_auth would break that user flow on every
    Stop-button click.  This decorator preserves the local-trust UX
    while still rejecting remote unauthenticated callers.

Threat model coverage:
    ✓ Remote attacker on the LAN — rejected (remote_addr != localhost)
    ✓ Remote attacker via DNS rebind — rejected (post-rebind remote_addr
      is still the attacker's IP, not localhost)
    ✓ Browser CSRF from same-origin localhost page — accepted (correct;
      that's the intended Nunba SPA flow)
    ✓ Browser CSRF from cross-origin page targeting localhost — REJECTED
      when the destructive endpoint uses ``@require_local_or_token_csrf_safe``
      (Phase 9.5 hardening).  The Origin/Referer header check distinguishes
      a same-origin SPA POST from a cross-origin attack page POST that
      happens to land on remote_addr=127.0.0.1.

Env vars:
    HARTOS_API_TOKEN — optional shared secret.  When set, callers may
    send `Authorization: Bearer <token>` to bypass the localhost check.
    Used by remote ops tooling and inter-node admin calls.

    TRUSTED_PROXY — when HARTOS sits behind a reverse proxy (nginx,
    Traefik), all requests appear as remote_addr=127.0.0.1 by default.
    Setting this env to the proxy's address makes the decorator inspect
    X-Forwarded-For instead.  Without it, only direct-connection
    remote_addr is trusted (safe default).

    HARTOS_TRUSTED_ORIGINS — comma-separated list of origins that are
    additionally treated as same-origin for the CSRF check (e.g.
    "https://nunba.local,https://hevolve.ai").  Loopback origins
    (http://localhost, http://127.0.0.1, http://[::1]) are always
    accepted regardless.
"""
from __future__ import annotations

import hmac
import os
from functools import wraps
from urllib.parse import urlparse

from flask import jsonify, request

# Read once at import time (not per-request) so token rotation requires
# a HARTOS restart — same model as Nunba.
API_TOKEN = os.environ.get('HARTOS_API_TOKEN', '')


def _is_local_request() -> bool:
    """True if the request is from localhost, honouring TRUSTED_PROXY."""
    trusted_proxy = os.environ.get('TRUSTED_PROXY', '')
    if trusted_proxy and request.remote_addr == trusted_proxy:
        forwarded_for = (request.headers.get('X-Forwarded-For', '')
                         .split(',')[0].strip())
        return forwarded_for in ('127.0.0.1', '::1', 'localhost')
    return request.remote_addr in ('127.0.0.1', '::1')


# ── CSRF defense-in-depth (Phase 9.5) ──────────────────────────────


_LOOPBACK_HOSTS = ('localhost', '127.0.0.1', '::1', '[::1]')


def _origin_host(origin_value: str) -> str:
    """Parse an Origin/Referer URL and return just the lowercased host
    (without port).  Returns '' on malformed input."""
    if not origin_value:
        return ''
    try:
        parsed = urlparse(origin_value)
        return (parsed.hostname or '').lower()
    except Exception:
        return ''


def _is_safe_csrf_origin() -> bool:
    """Return True iff the request's Origin/Referer header matches the
    set of trusted same-origin sources for state-changing destructive
    endpoints.

    Decision rules:
      1. Both Origin AND Referer absent → ACCEPT (non-browser client;
         curl, native desktop, Python requests).  Browser-driven CSRF
         attacks always send at least Origin or Referer — they cannot
         be suppressed by an attacker page.
      2. If Origin is present, its host MUST be loopback OR the
         request's own Host OR a HARTOS_TRUSTED_ORIGINS entry.
      3. If only Referer is present (older browsers / some Electron
         paths), apply the same host check to its hostname.

    This closes the same-machine cross-origin browser CSRF gap noted
    in the module docstring.  Wrapping a route with
    ``@require_local_or_token_csrf_safe`` activates this gate; routes
    that keep the original ``@require_local_or_token`` are unchanged.
    """
    origin_raw = request.headers.get('Origin', '').strip()
    referer_raw = request.headers.get('Referer', '').strip()
    if not origin_raw and not referer_raw:
        # Browsers always send at least Origin on cross-origin POST
        # (the spec requires it).  Absence means the request came from
        # a non-browser client — curl, native desktop, server-to-server.
        # require_local_or_token has already established it's localhost
        # or an authenticated token holder.
        return True

    # Build the allowed host set.
    own_host = _origin_host(request.host_url)
    trusted_extra = os.environ.get('HARTOS_TRUSTED_ORIGINS', '')
    extra_hosts = set()
    for entry in trusted_extra.split(','):
        entry = entry.strip()
        if entry:
            host = _origin_host(entry)
            if host:
                extra_hosts.add(host)

    def _host_allowed(host: str) -> bool:
        if not host:
            return False
        if host in _LOOPBACK_HOSTS:
            return True
        if own_host and host == own_host:
            return True
        if host in extra_hosts:
            return True
        return False

    # Origin takes priority — when present, it's the authoritative
    # signal (browsers send a literal "null" string for opaque origins
    # like file://; that fails _host_allowed correctly).
    if origin_raw:
        return _host_allowed(_origin_host(origin_raw))
    # Fall through: Referer-only path.
    return _host_allowed(_origin_host(referer_raw))


def require_local_or_token(f):
    """Allow localhost callers; require Bearer token for remote callers.

    Returns 401 with a clear message when neither condition holds — the
    error body is JSON to match the rest of the HARTOS API surface so
    the React SPA can surface it via its existing error toast pipeline.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if _is_local_request():
            return f(*args, **kwargs)
        if API_TOKEN:
            auth = request.headers.get('Authorization', '')
            if auth.startswith('Bearer '):
                token = auth[7:]
                # hmac.compare_digest is constant-time — defends against
                # timing-oracle leaks on the token comparison.
                if hmac.compare_digest(token, API_TOKEN):
                    return f(*args, **kwargs)
        return jsonify({
            'error': 'unauthorized',
            'message': ('This endpoint requires local access or a '
                        'valid HARTOS_API_TOKEN bearer header.'),
        }), 401
    return decorated


def require_local_or_token_csrf_safe(f):
    """Same gate as ``require_local_or_token`` PLUS an Origin/Referer
    header check that rejects cross-origin browser POSTs targeting
    localhost.

    Use this on DESTRUCTIVE state-changing endpoints (vlm_stop,
    config writes, anything that bulk-mutates server state) — the
    extra check costs one header lookup and closes the same-machine
    browser CSRF gap.

    Read-only / non-destructive endpoints SHOULD keep
    ``require_local_or_token`` to avoid breaking the curl/native
    desktop UX flows that don't send Origin headers.

    Authenticated callers (Bearer token) bypass the CSRF check —
    they've already proven possession of the shared secret, which a
    browser CSRF attacker can't replay.  This preserves the remote
    ops + inter-node admin path.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        # Authenticated bearer-token callers skip the CSRF gate —
        # token possession is itself proof the caller isn't a
        # cross-origin browser context.
        if API_TOKEN:
            auth = request.headers.get('Authorization', '')
            if auth.startswith('Bearer '):
                token = auth[7:]
                if hmac.compare_digest(token, API_TOKEN):
                    return f(*args, **kwargs)
        # Local callers must additionally pass the CSRF check.
        if _is_local_request():
            if _is_safe_csrf_origin():
                return f(*args, **kwargs)
            return jsonify({
                'error': 'forbidden',
                'message': ('Cross-origin browser request rejected. '
                            'This endpoint requires same-origin POST or '
                            'a HARTOS_API_TOKEN bearer header.'),
            }), 403
        return jsonify({
            'error': 'unauthorized',
            'message': ('This endpoint requires local access or a '
                        'valid HARTOS_API_TOKEN bearer header.'),
        }), 401
    return decorated
