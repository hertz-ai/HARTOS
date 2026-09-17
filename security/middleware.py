"""
Security Middleware for Flask
Applies security headers, CORS, CSRF protection, host validation, and API auth.
"""

import ipaddress
import os
import logging
import socket
from functools import wraps
from flask import Flask, request, jsonify, g

logger = logging.getLogger('hevolve_security')


def apply_security_middleware(app: Flask):
    """Apply all security middleware to a Flask app."""

    _apply_security_headers(app)
    _apply_cors(app)
    _apply_csrf_protection(app)
    _apply_host_validation(app)
    _apply_api_auth(app)


def _apply_security_headers(app: Flask):
    """Add security headers to all responses."""

    @app.after_request
    def add_security_headers(response):
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Permissions-Policy'] = (
            'camera=(), microphone=(), geolocation=(), '
            'payment=(), usb=(), magnetometer=()'
        )

        # HSTS only in production
        if os.environ.get('HEVOLVE_ENV') != 'development':
            response.headers['Strict-Transport-Security'] = (
                'max-age=31536000; includeSubDomains; preload'
            )
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self'; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )

        return response


def _apply_cors(app: Flask):
    """CORS with explicit origin allowlist.

    If CORS_ORIGINS is not set, no origins are allowed (fail-closed).
    Set CORS_ORIGINS=* for development only.
    """
    raw_origins = os.environ.get('CORS_ORIGINS', '')
    allowed_origins = set(
        o.strip() for o in raw_origins.split(',')
        if o.strip()
    )
    if not allowed_origins:
        logger.warning(
            "CORS_ORIGINS not configured - no cross-origin requests allowed. "
            "Set CORS_ORIGINS env var for production (comma-separated origins).")

    @app.after_request
    def add_cors_headers(response):
        origin = request.headers.get('Origin', '')

        if origin in allowed_origins:
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Access-Control-Allow-Methods'] = (
                'GET, POST, PUT, DELETE, PATCH, OPTIONS'
            )
            response.headers['Access-Control-Allow-Headers'] = (
                'Content-Type, Authorization, X-API-Key, X-CSRF-Token'
            )
            response.headers['Access-Control-Allow-Credentials'] = 'true'
            response.headers['Access-Control-Max-Age'] = '600'

        return response

    @app.before_request
    def handle_preflight():
        if request.method == 'OPTIONS':
            response = app.make_default_options_response()
            origin = request.headers.get('Origin', '')
            if origin in allowed_origins:
                response.headers['Access-Control-Allow-Origin'] = origin
                response.headers['Access-Control-Allow-Methods'] = (
                    'GET, POST, PUT, DELETE, PATCH, OPTIONS'
                )
                response.headers['Access-Control-Allow-Headers'] = (
                    'Content-Type, Authorization, X-API-Key, X-CSRF-Token'
                )
            return response


def _apply_csrf_protection(app: Flask):
    """CSRF protection for state-changing requests."""

    # Paths exempt from CSRF (API-only endpoints using Bearer auth)
    CSRF_EXEMPT_PREFIXES = (
        '/a2a/', '/api/social/bots/', '/status',
        '/.well-known/',
    )

    @app.before_request
    def csrf_check():
        if request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
            return

        # Skip for exempt paths
        if any(request.path.startswith(p) for p in CSRF_EXEMPT_PREFIXES):
            return

        # Bearer token auth is inherently CSRF-safe
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            return

        # API key auth is also CSRF-safe
        if request.headers.get('X-API-Key'):
            return

        # JSON content type with Origin check provides CSRF protection
        if request.content_type and 'application/json' in request.content_type:
            return

        # For non-API requests (forms), require CSRF token
        csrf_token = request.headers.get('X-CSRF-Token')
        if not csrf_token:
            logger.warning(f"CSRF token missing for {request.method} {request.path}")
            return jsonify({'error': 'CSRF token required'}), 403


def _is_private_address(host: str) -> bool:
    """Is `host` a literal address that can only mean THIS local network?

    Host validation exists to stop an attacker-supplied Host being reflected
    into generated absolute URLs — the password-reset / cache-poisoning class.
    That attack needs a host a VICTIM will later resolve and trust, which means
    a public name. A literal RFC1918/loopback/link-local/ULA address cannot
    serve that purpose: it resolves to nothing outside the LAN it names.

    So accepting these is not a relaxation of the injection defence, and it is
    required by the peer mesh. A HART node is addressed by its LAN IP by every
    peer that discovers it; with the shipped default of
    ALLOWED_HOSTS=localhost,127.0.0.1 the node answered 400 to all of them.
    Verified in a VM: hart-peer-discovery's "Server backend accessible from
    edge" got `{"error":"Invalid host"}` cross-host, and NO nixos module sets
    ALLOWED_HOSTS — only the cloud deploy does (to '*'), which is why this was
    invisible outside the OS.

    A public address or any domain name still falls through to the allowlist.
    """
    try:
        ip = ipaddress.ip_address(host.strip('[]'))     # [::1] → ::1
    except ValueError:
        return False                                   # a NAME, not a literal
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def _apply_host_validation(app: Flask):
    """Prevent Host header injection."""

    allowed_hosts = set(
        h.strip() for h in
        os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
        if h.strip()
    )
    # '*' is the Django-style wildcard: the operator opted into allow-all. The
    # deploy default is exactly this (deploy-hartos-deepbox.yml sets
    # ALLOWED_HOSTS=`secrets.ALLOWED_HOSTS || '*'`), and it is load-bearing:
    # /api/ota/latest is a PUBLIC pointer fleet nodes poll with
    # Host: <central-host>. Without wildcard support a '*' config is treated as a
    # literal host, so every real Host is rejected 400 and OTA delivery silently
    # breaks fleet-wide. Precompute once — allowed_hosts is fixed at app init.
    allow_all_hosts = '*' in allowed_hosts

    # The node's own name. gethostname() is a local syscall — no DNS, no
    # network — so this is safe to compute at init on an offline box, unlike
    # getfqdn()/gethostbyname_ex(), which can block on resolution.
    try:
        _own_hostname = socket.gethostname().split('.')[0].lower()
    except OSError as exc:            # pragma: no cover - gethostname failing
        logger.warning("host validation: gethostname failed (%s) — "
                       "peers addressing this node by name will be rejected",
                       exc)
        _own_hostname = ''

    @app.before_request
    def validate_host():
        if os.environ.get('HEVOLVE_ENV') == 'development':
            return
        if os.environ.get('NUNBA_BUNDLED'):
            return
        if allow_all_hosts:
            return

        host = request.host.split(':')[0]
        if host in allowed_hosts:
            return
        if _own_hostname and host.lower() == _own_hostname:
            return
        if _is_private_address(host):
            return
        logger.warning(f"Rejected request with invalid Host: {host}")
        return jsonify({'error': 'Invalid host'}), 400


#: Admin operations that modify persistent state. ALWAYS require auth
#: regardless of tier — even on a trusted LAN, random devices (IoT,
#: guests, kids' tablets) shouldn't be able to hit admin routes. The
#: only exception is bundled desktop mode (NUNBA_BUNDLED), which is
#: single-user in-process test_client territory with no network exposure.
#:
#: Each entry is a prefix — anything starting with it is considered an
#: admin path. Add new admin routes to this tuple; they inherit the
#: auth gate automatically.
ADMIN_PATHS = ('/api/admin',)

#: User-facing API endpoints. These are guarded only when the deployment
#: is publicly exposed (central tier). Regional deployments live on a
#: trusted LAN or behind a gateway (KONG, etc.) that handles auth; flat
#: deployments are single-user desktop and pre-trusted.
NETWORK_PROTECTED_PATHS = ('/chat', '/time_agent', '/visual_agent',
                           '/add_history', '/prompts', '/zeroshot',
                           '/response_ack',
                           #: voice: gate the routes that WRITE/READ files
                           #: (speak, clone); the read-only, traversal-safe
                           #: audio serve + voices list stay public so a
                           #: browser <audio src> is not broken (#67).
                           '/api/voice/speak', '/api/voice/clone')

#: Legacy alias — some tests still import PROTECTED_PATHS expecting
#: the combined tuple. Keep this as the union so older imports don't
#: break, while the split above drives the new enforcement logic.
PROTECTED_PATHS = ADMIN_PATHS + NETWORK_PROTECTED_PATHS

EXEMPT_PREFIXES = ('/status', '/a2a/', '/api/social/', '/.well-known/',
                   '/prompts/public',
                   # A phone that found this desktop on the LAN GETs /health
                   # before adopting the node (PeerLinkDiscovery.isHealthy;
                   # measured 2026-09-16: 401 here kept every phone on the
                   # cloud).  On HARTOS's own app it is the liveness probe
                   # ({'status': 'alive'}); on Nunba's app, the one a desktop
                   # advertises, it aliases /backend/health: GPU tier, name
                   # and VRAM figures -- no secret, path or identifier, and
                   # the class of facts the node already advertises to peers
                   # in the announce (has_gpu, vram_free_gb).  Reads are
                   # cached, so a LAN caller runs no GPU probe.  /ready,
                   # which reports DB and identity checks, stays gated.
                   '/health')


def _apply_api_auth(app: Flask, register: bool = True):
    """Tier-aware API authentication with a strict admin guard.

    Returns the gate hook; ``register=False`` builds it without registering
    it, for hartos_bootstrap.install_api_gate, which must be able to run the
    same hook in front of an app that no longer accepts before_request.

    Two gates run in order:

      1. ADMIN guard — /api/admin/* ALWAYS requires auth on any tier
         except a desktop's own callers. Admin ops modify persistent state so
         LAN trust is not enough — a compromised IoT device on the
         same network must not be able to drop agents or reconfigure
         TTS engines.
      2. NETWORK guard — /chat, /prompts, /visual_agent, etc. are
         guarded only on central tier (publicly exposed). Regional
         and flat tiers assume LAN trust or gateway-handled auth.

    When HEVOLVE_API_KEY is set, BOTH gates accept X-API-Key. When it
    is unset, BOTH gates accept only a Bearer JWT. Exempt prefixes
    (/status, /a2a/, /api/social/, /.well-known/, /prompts/public)
    bypass both gates so health probes and social-media-facing routes
    stay public.

    Deployment scenarios:
      - Behind KONG:                    KONG handles auth → no key needed,
                                        middleware enforces tier-conditional
                                        only if KONG is bypassed
      - Bundled desktop (NUNBA_BUNDLED): its own machine trusted; another
                                        machine reaches the exempt paths,
                                        and the rest with a credential
      - Regional LAN:                   /chat open, /api/admin gated
      - Central cloud:                  everything gated
    """

    def _path_matches_any(path: str, prefixes: tuple) -> bool:
        return any(path == p or path.startswith(p + '/') for p in prefixes)

    def _is_exempt(path: str) -> bool:
        return any(path.startswith(p) for p in EXEMPT_PREFIXES)

    def _is_admin_path(path: str) -> bool:
        return _path_matches_any(path, ADMIN_PATHS)

    def _is_network_protected(path: str) -> bool:
        return _path_matches_any(path, NETWORK_PROTECTED_PATHS)

    def _require_api_key_or_bearer(expected_key: str):
        """Return None if the request carries a valid credential, else
        a 401 jsonify response. Preference order matches the original
        middleware: X-API-Key if configured, otherwise Bearer token."""
        if expected_key:
            api_key = request.headers.get('X-API-Key')
            if api_key and _constant_time_compare(api_key, expected_key):
                return None
            # Fall through to Bearer check so API-key-configured deploys
            # still accept JWTs (useful for admin UI + k8s probes).
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            # Actually decode and verify the JWT — not just check the prefix.
            # Without this, `Bearer garbage` passes the admin gate.
            _token = auth_header[7:]
            try:
                from integrations.social.auth import decode_jwt
                jwt_payload = decode_jwt(_token)
                if jwt_payload:
                    g.auth_source = 'jwt'
                    g.jwt_payload = jwt_payload
                    return None
            except Exception:
                pass
            # Token invalid/expired — reject
            return jsonify({'error': 'Invalid or expired Bearer token'}), 401
        if expected_key:
            logger.warning(f"Invalid/missing credential for {request.path}")
            return jsonify(
                {'error': 'X-API-Key header or Bearer token required'},
            ), 401
        return jsonify(
            {'error': 'Authentication required (Bearer token)'},
        ), 401

    def _admit_owner_allowed_device(refused):
        """A desktop's second network credential: a token the phone signed
        with its own PeerLink key, admitted when the owner has allowed that
        key (integrations.social.auth.verify_device_jwt; the grant is the
        owner's ``device_access`` consent whose scope names the key).

        ``refused`` is the 401 the key/JWT check already produced; it stands
        for anything that is not a device token.  A device the owner has not
        answered about gets the ask filed for them (ConsentService.
        request_consent, delivered like every consent ask, one card per
        pending row) and a 403 ``consent_pending`` it can retry on; a device
        the owner said no to gets 403 ``consent_denied`` and no new ask.  An
        admitted device acts only as the token's user: a JSON body must
        carry that ``user_id`` and no other (#51), so no route's default
        user can stand in for it.

        Filing is what an unauthenticated peer can trigger, so it is paced
        per address with the gossip announce limiter (discovery.
        _check_announce_rate): past the limit the ask is not filed and the
        answer is still ``consent_pending``, which an honest phone retries.
        """
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return refused
        owner = os.environ.get('HEVOLVE_OWNER_USER_ID')
        if not owner:
            return refused
        token = auth_header[7:]
        try:
            from integrations.social.auth import (
                file_device_access_ask, verify_device_jwt)
            from integrations.social.models import db_session
            with db_session(commit=True) as db:
                verdict = verify_device_jwt(db, token, owner)
                if verdict['status'] == 'pending':
                    from integrations.social.discovery import _check_announce_rate
                    if _check_announce_rate(request.remote_addr or ''):
                        file_device_access_ask(db, owner, verdict['public_key'],
                                               verdict.get('claims') or {})
                    else:
                        logger.warning("device ask from %s not filed: rate limit",
                                       request.remote_addr)
        except Exception:
            logger.warning("device credential check failed; refusing",
                           exc_info=True)
            return refused
        status = verdict['status']
        if status == 'ok':
            payload = verdict['payload']
            body = request.get_json(silent=True) if request.is_json else None
            if isinstance(body, dict):
                asked_as = body.get('user_id')
                if asked_as is None or str(asked_as) != str(payload.get('user_id')):
                    logger.warning("device %s... acting as user %s, token says "
                                   "%s; refused", verdict['public_key'][:16],
                                   asked_as, payload.get('user_id'))
                    return jsonify({'error': 'user_id must be the token\'s user'}), 403
            g.auth_source = 'device'
            g.jwt_payload = payload
            g.device_public_key = verdict['public_key']
            return None
        if status == 'pending':
            return jsonify({'error': 'consent_pending',
                            'message': "Waiting for this desktop's owner to "
                                       "allow this phone"}), 403
        if status == 'denied':
            return jsonify({'error': 'consent_denied',
                            'message': "This desktop's owner has not allowed "
                                       "this phone"}), 403
        return refused


    def _expected_api_key() -> str:
        """HEVOLVE_API_KEY, the one credential both branches below accept."""
        try:
            from security.secrets_manager import get_secret
            return get_secret('HEVOLVE_API_KEY')
        except Exception:
            return os.environ.get('HEVOLVE_API_KEY', '')

    def check_api_auth():
        path = request.path
        # Bundled desktop.  This machine's own callers (the SPA, the tray,
        # in-process test clients) are trusted, as they always were.  But the
        # socket is Nunba's app on 0.0.0.0, the address the desktop advertises
        # to peers (core.port_registry.get_advertisable_base_url), so a caller
        # from another machine reaches only the exempt paths, which carry the
        # peer protocol's HTTP half, and everything else with a credential.
        # Measured 2026-09-14: a device on the same Wi-Fi could drive /chat
        # and read /prompts on an installed desktop.
        if os.environ.get('NUNBA_BUNDLED'):
            from core.auth_local import _is_local_request
            if _is_local_request() or _is_exempt(path):
                return
            refused = _require_api_key_or_bearer(_expected_api_key())
            if refused is None:
                return
            # A person's phone, signed with the key the owner allowed (#111):
            # only after the key and the local JWT have not admitted it, so
            # every caller admitted today is admitted exactly as before.
            return _admit_owner_allowed_device(refused)

        if _is_exempt(path):
            return

        # Resolve the shared credential once — both gates share it.
        expected_key = _expected_api_key()

        # Gate 1: Admin paths. ALWAYS required. Even regional LAN
        # deployments gate admin ops — the tier model is for user-facing
        # APIs, not for operations that mutate persistent state.
        if _is_admin_path(path):
            resp = _require_api_key_or_bearer(expected_key)
            if resp is not None:
                return resp
            return  # Admin path authenticated, skip gate 2

        # Gate 2: User-facing API paths. Only enforced on central tier
        # (publicly exposed) OR whenever HEVOLVE_API_KEY is explicitly set
        # (direct-exposure deployments that opt in to the key layer).
        if not _is_network_protected(path):
            return  # Not in either tuple → public

        node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
        if expected_key:
            resp = _require_api_key_or_bearer(expected_key)
            if resp is not None:
                return resp
            return
        if node_tier == 'central':
            resp = _require_api_key_or_bearer(expected_key='')
            if resp is not None:
                return resp
            return
        # Non-central without API key → LAN-trusted or gateway-auth'd
        return

    if register:
        app.before_request(check_api_auth)
    return check_api_auth


def install_api_gate(app: Flask) -> bool:
    """Put the API gate on an app that other machines reach, once.

    hart_intelligence_entry gets it through apply_security_middleware.  An
    embedder's app gets it here: Nunba's, which a desktop serves on 0.0.0.0
    and advertises to peers.  hartos_bootstrap calls this first inside its
    setup-lock window, and an embedder that serves before bootstrap runs
    calls it when it creates the app.  Measured 2026-09-14: without it a
    device on the same network could drive /chat and read /prompts on an
    installed desktop.

    Never left open: Flask refuses a before_request hook once an app has
    served a request outside the setup-lock window, and then the hook goes
    into before_request_funcs directly, where the decorator puts it, so it
    runs in the request's own dispatch.  The app is marked gated only after
    the hook is confirmed there.  Returns whether the app is gated; False,
    logged CRITICAL, only if the hook could not be placed.
    """
    if getattr(app, '_hartos_api_gate', False):
        return True
    if os.environ.get('NUNBA_CI', '') == '1':
        # Say loudly which way NUNBA_CI went.  core.auth_local trusts every
        # caller under it in a build run from source (Nunba's staging
        # container, the only place that sets it); an installed build
        # ignores it.
        from core.auth_local import ci_trusts_every_caller
        if ci_trusts_every_caller():
            logger.critical("NUNBA_CI=1: every caller is trusted as local, as "
                            "on Nunba's staging container; a production node "
                            "must never set it")
        else:
            logger.critical("NUNBA_CI=1 is set on an installed build and is "
                            "ignored: callers from other machines still need "
                            "a credential")
    hook = _apply_api_auth(app, register=False)
    try:
        app.before_request(hook)
    except Exception as e:
        # The hook reads headers and the remote address only, never the body.
        logger.critical(f"Flask refused the API gate ({e}); adding it to "
                        f"before_request_funcs directly")
        app.before_request_funcs.setdefault(None, []).append(hook)
    if hook not in app.before_request_funcs.get(None, []):
        logger.critical("The API gate is NOT on this app; it is serving UNGATED")
        return False
    app._hartos_api_gate = True
    return True


def _constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())
