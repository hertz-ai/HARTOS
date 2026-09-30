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

    TRUSTED_PROXY — the address (or comma-separated addresses) of a reverse
    proxy that APPENDS the real client to X-Forwarded-For.  It decides whose
    budget a rate-limited request is charged to and the gossip vantage
    (client_address).  It never makes a request local: that needs a
    loopback socket peer.  Measure before setting it (client_address
    docstring).  Without it, the socket peer is the client (safe default).

    HARTOS_TRUSTED_ORIGINS — comma-separated list of origins that are
    additionally treated as same-origin for the CSRF check (e.g.
    "https://nunba.local,https://hevolve.ai").  Loopback origins
    (http://localhost, http://127.0.0.1, http://[::1]) are always
    accepted regardless.
"""
from __future__ import annotations

import hmac
import os
import sys
from functools import wraps
from urllib.parse import urlparse

from flask import jsonify, request

# Read once at import time (not per-request) so token rotation requires
# a HARTOS restart — same model as Nunba.
API_TOKEN = os.environ.get('HARTOS_API_TOKEN', '')


def ci_trusts_every_caller() -> bool:
    """True on Nunba's staging container: NUNBA_CI=1 in a build run from source.

    Nunba's docker-compose.staging.yml sets NUNBA_CI=1, and its e2e probe
    reaches the container through Docker's port mapping, so it never arrives
    from 127.0.0.1.  An installed (frozen) build ignores the variable: staging
    always runs from source, so NUNBA_CI in a shipped desktop can only be a
    misconfiguration, and trusting every caller there would open the desktop
    to its network.  The one home of this rule: _is_local_request applies it,
    and Nunba's routes.auth.is_local_environ imports it rather than copying it.
    """
    return os.environ.get('NUNBA_CI', '') == '1' and not getattr(sys, 'frozen', False)


def _token_matches(candidate: str) -> bool:
    """Constant-time comparison of a caller-supplied token against
    ``API_TOKEN``.

    ``hmac.compare_digest`` on two ``str`` operands is ASCII-only and
    raises ``TypeError`` if either side carries a non-ASCII character.
    The candidate comes straight from an attacker-controllable
    ``Authorization: Bearer …`` header, so a junk non-ASCII token would
    otherwise escape the decorator as an unhandled 500 instead of a
    clean 401.  Encoding both sides to bytes sidesteps the ASCII-only
    restriction while keeping the comparison constant-time (bytes
    compare_digest is constant-time and length-safe).

    An unset (empty) ``API_TOKEN`` never matches.
    """
    if not API_TOKEN:
        return False
    return hmac.compare_digest(
        candidate.encode('utf-8', 'surrogatepass'),
        API_TOKEN.encode('utf-8', 'surrogatepass'),
    )


def _norm_ip(value: str) -> str:
    """An address as a comparable string: brackets stripped, IPv4-mapped
    IPv6 (::ffff:127.0.0.1, what a dual-stack server reports) folded to
    its IPv4 form.  A non-address (a hostname) is returned lower-cased."""
    v = (value or '').strip().strip('[]').lower()
    try:
        import ipaddress
        ip = ipaddress.ip_address(v)
        mapped = getattr(ip, 'ipv4_mapped', None)
        return str(mapped or ip)
    except ValueError:
        return v


def _is_loopback(value: str) -> bool:
    v = _norm_ip(value)
    if v == 'localhost':
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(v).is_loopback
    except ValueError:
        return False


def _trusted_proxies() -> set:
    """TRUSTED_PROXY: one address, or several comma-separated."""
    return {_norm_ip(p) for p in os.environ.get('TRUSTED_PROXY', '').split(',')
            if p.strip()}


def _is_forwarder(peer: str) -> bool:
    """Is the socket peer a forwarder this node runs: an address named in
    TRUSTED_PROXY, or loopback (a proxy on this machine)?  A private LAN
    address is NOT one: it is another machine, and its X-Forwarded-For is
    whatever it chose to write (review of d35926896)."""
    peer = _norm_ip(peer)
    if not peer:
        return False
    return peer in _trusted_proxies() or _is_loopback(peer)


def client_address() -> str:
    """The address of the client this request came from.  The ONE rule:
    _is_local_request, the gossip / device-ask rate limiter
    (integrations.social.discovery._rate_client_key) and the announce
    vantage (discovery._observed_ip) all read it.

    The socket peer, unless it is a forwarder we run (_is_forwarder); then
    the LAST X-Forwarded-For hop, the one that forwarder appended (earlier
    hops were written by the client and prove nothing).  A TRUSTED_PROXY
    that sends no header answers '' (nothing to believe: callers fall back
    to the socket peer or fail closed); a loopback peer with no header is
    itself the client.  IPv4-mapped addresses are folded (_norm_ip).

    Before trusting a proxy, MEASURE what it sends.  From a known external
    client, request any route and read, in this node's log or a debug
    route, request.remote_addr and the X-Forwarded-For header:
      - remote_addr is the proxy's address and X-Forwarded-For ENDS with the
        client's real address: set TRUSTED_PROXY to that proxy address;
      - X-Forwarded-For is absent, or ends with the proxy's own address
        (docker's userland proxy on -p, which is what central shows:
        172.21.0.1 for every peer): the proxy does not name clients, and
        TRUSTED_PROXY restores nothing; leave it unset.
    A header never makes a request local either way (_is_local_request).
    """
    return _client_from(request.remote_addr,
                        request.headers.get('X-Forwarded-For'))


def _client_from(remote, forwarded) -> str:
    """client_address over raw values: the socket peer and the
    X-Forwarded-For header, as a Flask request or a WSGI environ has them.
    The only place either is interpreted."""
    peer = _norm_ip(remote or '')
    if not _is_forwarder(peer):
        return peer
    hops = [_norm_ip(h) for h in (forwarded or '').split(',') if h.strip()]
    if hops:
        return hops[-1]
    return '' if peer in _trusted_proxies() else peer


def _local_from(remote, forwarded) -> bool:
    """_is_local_request's rule over raw values (see there)."""
    if ci_trusts_every_caller():
        return True
    if not _is_loopback(remote or ''):
        return False
    return _is_loopback(_client_from(remote, forwarded))


def client_key() -> str:
    """client_address, or the socket peer when a trusted proxy named no
    client: the key rate limiters and caller identities charge.  Never ''
    for a request that has a socket peer (every such request would share one
    empty key)."""
    return client_address() or _norm_ip(request.remote_addr or '')


def is_local_environ(environ) -> bool:
    """_is_local_request for a raw WSGI environ (Nunba's app.py dispatcher
    decides before any Flask app has the request).  Same rule, same code."""
    return _local_from(environ.get('REMOTE_ADDR', ''),
                       environ.get('HTTP_X_FORWARDED_FOR', ''))


def _is_local_request() -> bool:
    """True if the request comes from this machine.

    Local means the SOCKET peer is loopback, and, when that peer is a
    proxy on this machine, the client it names is loopback too.  An address
    taken from a header never makes a request local by itself: behind a
    TRUSTED_PROXY that is another machine, a forwarded request is remote
    whatever it claims (review of 291e548df, F1: a proxy that appends
    nothing let 'X-Forwarded-For: 127.0.0.1' through).

    Nunba's staging container trusts every caller (ci_trusts_every_caller:
    NUNBA_CI=1 in a build run from source), as Nunba's
    routes.auth.is_local_environ does through the same function.
    """
    return _local_from(request.remote_addr,
                       request.headers.get('X-Forwarded-For'))


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


def is_safe_csrf_origin() -> bool:
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
                # _token_matches is constant-time — defends against
                # timing-oracle leaks — and byte-safe against non-ASCII
                # tokens (a raw str compare_digest would raise TypeError
                # → 500 instead of 401 on attacker-supplied junk).
                if _token_matches(token):
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
                if _token_matches(token):
                    return f(*args, **kwargs)
        # Local callers must additionally pass the CSRF check.
        if _is_local_request():
            if is_safe_csrf_origin():
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


# Read from the environment as this node's own configuration or key
# material: a vault or consent-card value must never set these.
# tests/unit/test_env_secrets_declared.py fails on a secret read not
# declared here or in ENV_SECRETS.
ENV_NOT_FROM_VAULT = (
    'HARTOS_API_TOKEN',
)
