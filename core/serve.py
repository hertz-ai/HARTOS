"""The ASGI stack every entry point serves Flask through.

Three entry points build this stack: HARTOS `hart_intelligence_entry._serve_app`,
Nunba `app.py:start_flask` (the cx_Freeze/desktop entry) and Nunba `main.py`
(`python main.py`, dev + HART OS daemon).  They were written independently --
`hypercorn.asyncio` first appears in app.py via a documented boot fix
(2026-04-28), in main.py via a commit titled "Dashboards corrected" with an
empty body (2026-04-29), and here via a commit about home-page image hydration
(2026-06-29).  Only the first recorded a reason to exist.  The single commit
that ever touched two of them together was a ruff auto-fix.

That drift already cost one outage: peer_link_asgi was added to _serve_app and
to neither Nunba path, so ws://<node>/peer_link answered 403 on the desktop and
every reader of PeerLinkManager._links saw zero peers.

WHAT LIVES HERE
  The parts that are genuinely identical across all three: the websocket-capable
  ASGI wrapping, and the Config settings none of them vary.

WHAT DELIBERATELY DOES NOT
  The entry points differ in ways that are load-bearing, and flattening them
  would lose behaviour:
    * app.py patches signal.signal because start_flask runs in the NunbaGUI
      worker thread (app.py:7500 spawns it) while pywebview owns the main
      thread -- hypercorn's signal install raises ValueError there.
    * main.py honours NUNBA_FORCE_WAITRESS, which docker-compose.staging.yml
      sets because main.py's Hypercorn answered 404 to every Flask route
      there.  That was blamed on AsyncioWSGIMiddleware; the measured cause
      was main.py's Host allowlist (server_names=['Nunba']), which Hypercorn
      checks before the app runs.  local_server_names() below is the
      allowlist main.py now passes; waitress has no allowlist at all.
    * app.py catches ImportError/NotImplementedError/ValueError/RuntimeError;
      the others catch ImportError alone.
    * bind shapes differ (0.0.0.0:port / unix socket or host:port / host:port),
      as do worker-thread env var and default (NUNBA_WORKER_THREADS=128,
      HEVOLVE_WORKER_THREADS=256), thread_name_prefix, waitress tuning, and the
      Flask-dev-server last resort.
  Those stay at their call sites.  This module owns only what they share.

Imports are lazy on purpose: a caller that reaches these functions inside its
own `try` must still see ImportError when hypercorn is absent, so its existing
waitress fallback fires unchanged.
"""
import sys
from typing import Any, List, Optional, Sequence

# Every entry point set these to the same values before this module existed.
# Named so a reader can tell "shared default" from "this deployment's choice".
KEEP_ALIVE_TIMEOUT = 120                     # SSE-friendly long polls
MAX_INCOMPLETE_SIZE = 16 * 1024 * 1024       # 16MB request bodies
ACCESS_LOG = None                            # each app emits its own access log
ERROR_LOG = '-'

# The Host header HARTOS's Liquid UI reverse proxy sends to Nunba over the
# HART_NUNBA_SOCKET unix socket (integrations/agent_engine/liquid_ui_service.py
# imports it from here).  Capital N on purpose: a browser lowercases the host
# it puts in a Host header, so no web page can produce this exact value.
UNIX_SOCKET_SERVER_NAME = 'Nunba'

# How the loopback interface is spelled in a Host header.  An IPv6 literal is
# bracketed there (RFC 3986 3.2.2), unlike core.auth_local's bare-hostname
# tuple, which compares urlparse().hostname and so cannot be reused here.
LOOPBACK_HOST_NAMES = ('127.0.0.1', 'localhost', '[::1]')

# Binds that listen on every interface: there is no one name a client uses.
_WILDCARD_BIND_HOSTS = ('0.0.0.0', '::', '[::]', '')


def local_server_names(bind: Sequence[str]) -> List[str]:
    """The Host-header allowlist for a server only local clients should reach.

    Hypercorn 0.17.3 (utils.valid_server_name) compares the raw Host header
    to config.server_names with plain `in`: exact, case-sensitive, port
    included.  Anything else gets a 404 before the app runs.  That is the
    DNS-rebinding barrier: a page on evil.example that re-resolves to
    127.0.0.1 still sends `Host: evil.example:<port>`, and is refused.

    So the list holds exactly the Host values a real local client sends:
      * UNIX_SOCKET_SERVER_NAME, for the Liquid UI unix-socket proxy;
      * for each TCP bind `host:port`, `<loopback>:<port>` for every
        LOOPBACK_HOST_NAMES spelling (browsers, curl, Cypress, wait-on), plus
        the bound address itself when it is a concrete one (an IP literal
        cannot be rebound, so this admits no attacker-chosen name);
      * the same names without the port when the port is 80, because clients
        omit a default port from Host.

    A unix-only bind yields [UNIX_SOCKET_SERVER_NAME], the value main.py used
    before this function existed, so daemon mode is unchanged.  A wildcard
    bind (0.0.0.0) admits loopback only: LAN clients are not enumerated here.
    """
    names: List[str] = [UNIX_SOCKET_SERVER_NAME]

    def _add(name: str) -> None:
        if name not in names:
            names.append(name)

    for entry in bind:
        if entry.startswith(('unix:', 'fd://')):
            continue
        host, sep, port = entry.rpartition(':')
        if not sep or not port.isdigit():
            raise ValueError(f'bind {entry!r} is not host:port, unix: or fd://')
        hosts = list(LOOPBACK_HOST_NAMES)
        if host not in _WILDCARD_BIND_HOSTS:
            if ':' in host and not host.startswith('['):
                host = f'[{host}]'
            hosts.extend((host, host.lower()))
        for h in hosts:
            _add(f'{h}:{port}')
            if port == '80':
                _add(h)
    return names


def make_hypercorn_config(bind: Sequence[str],
                          *, server_names: Optional[Sequence[str]] = None) -> Any:
    """A hypercorn Config with the settings all three entry points share.

    `bind` is passed through verbatim -- host:port, unix:<path>, whatever the
    caller resolved.  `server_names` is set only when given, because two of the
    three never set it and adding one would change Host-header handling.
    A caller that wants a Host allowlist passes local_server_names(bind).

    Raises ImportError when hypercorn is missing, which is what each caller's
    waitress fallback is waiting for.
    """
    from hypercorn.config import Config

    config = Config()
    config.bind = list(bind)
    config.keep_alive_timeout = KEEP_ALIVE_TIMEOUT
    config.h11_max_incomplete_size = MAX_INCOMPLETE_SIZE
    config.accesslog = ACCESS_LOG
    config.errorlog = ERROR_LOG
    if server_names:
        config.server_names = list(server_names)
    return config


#: A local caller's request body has no size cap.  Owner, 2026-10-10: "local
#: need not have a cap".  Measured live that day on the desktop: every body
#: over 2 MB was refused before Nunba ran (2.1 MB: an empty 400; 3 MB: the
#: connection dropped), so a book PDF uploaded from the browser, an avatar
#: photo or a voice recording over 2 MB could never arrive.
LOCAL_MAX_BODY_SIZE = sys.maxsize


def max_body_size(is_local: bool) -> int:
    """The request-body cap: none for a local caller, MAX_PAYLOAD_BYTES
    (HEVOLVE_MAX_PAYLOAD_BYTES, 2 MB by default) for every other.

    The ONE rule for both layers that cap a body: the transport
    (build_asgi_app) and Flask (caller_capped_request).  "Local" is
    core.auth_local's rule, read through is_local_environ: the socket peer is
    loopback and, when that peer is a proxy on this machine, so is the client
    it names.  A central node is reached through Docker's proxy, never from
    loopback, so its callers keep the cap.

    No cap means the body is held in memory as it arrives (the WSGI
    middleware buffers it before the app runs), so a local upload is bounded
    by this machine's memory instead.
    """
    from core.constants import MAX_PAYLOAD_BYTES
    return LOCAL_MAX_BODY_SIZE if is_local else MAX_PAYLOAD_BYTES


def _asgi_scope_is_local(scope: Any) -> bool:
    """core.auth_local's locality rule over an ASGI scope: the WSGI environ
    values Hypercorn would build from it (REMOTE_ADDR, and every
    X-Forwarded-For header joined by commas)."""
    from core.auth_local import is_local_environ

    client = scope.get('client') or ('', 0)
    forwarded = ','.join(
        value.decode('latin-1') for name, value in scope.get('headers') or []
        if name.lower() == b'x-forwarded-for')
    return is_local_environ({'REMOTE_ADDR': client[0] or '',
                             'HTTP_X_FORWARDED_FOR': forwarded})


def caller_capped_request(base: Any) -> Any:
    """A Flask request class whose body cap is max_body_size for its caller.

    Flask's MAX_CONTENT_LENGTH is one number for every request; this reads
    the cap per request instead.  A property, not Flask 3.1's per-request
    setter, because requirements.txt pins Flask 2.3, which has no setter:
    Werkzeug reads `max_content_length` when it opens the body stream and
    when it parses a form, on both versions.
    """
    from core.auth_local import is_local_environ

    class CallerCappedRequest(base):
        @property
        def max_content_length(self):
            return max_body_size(is_local_environ(self.environ))

    return CallerCappedRequest


def build_asgi_app(wsgi_app: Any) -> Any:
    """Wrap a WSGI app so `/peer_link` websockets are served and HTTP is not.

    AsyncioWSGIMiddleware is WSGI and cannot see a websocket scope, so on its
    own it leaves /peer_link to fall through and Hypercorn answers 403.
    peer_link_asgi serves that one path and passes every other scope straight
    through, so the HTTP surface is byte-for-byte what the middleware alone
    produced.  See core/peer_link/server.py.

    Callers must not reintroduce a bare AsyncioWSGIMiddleware assignment;
    Nunba's tests/test_peer_link_mounted.py fails the build if they do.
    """
    from hypercorn.middleware import AsyncioWSGIMiddleware

    from core.peer_link.server import peer_link_asgi

    # Without an explicit max_body_size the middleware's library default of
    # 2**16 (64 KB) rejects every larger POST body with an empty 400 before
    # the WSGI app runs — measured live 2026-08-21: 50 KB reached the Flask
    # handler, 200 KB never did, so batch voice transcribe and real uploads
    # were transport-dead while Flask's MAX_CONTENT_LENGTH said 2 MB was
    # fine.  The +1 headroom lets a body at exactly the app cap through the
    # transport so Flask's own 413 (with a JSON body) owns the boundary
    # error instead of the middleware's bare 400.  The middleware takes one
    # size, so a local caller (max_body_size) is served by a second one.
    capped = AsyncioWSGIMiddleware(
        wsgi_app, max_body_size=max_body_size(False) + 1)
    uncapped = AsyncioWSGIMiddleware(
        wsgi_app, max_body_size=max_body_size(True))

    async def by_caller(scope, receive, send):
        app = uncapped if _asgi_scope_is_local(scope) else capped
        await app(scope, receive, send)

    return peer_link_asgi(by_caller)


def shared_config_values() -> dict:
    """The shared settings as data, for tests that pin them without hypercorn."""
    return {
        'keep_alive_timeout': KEEP_ALIVE_TIMEOUT,
        'h11_max_incomplete_size': MAX_INCOMPLETE_SIZE,
        'accesslog': ACCESS_LOG,
        'errorlog': ERROR_LOG,
    }


__all__: List[str] = [
    'KEEP_ALIVE_TIMEOUT',
    'MAX_INCOMPLETE_SIZE',
    'ACCESS_LOG',
    'ERROR_LOG',
    'UNIX_SOCKET_SERVER_NAME',
    'LOOPBACK_HOST_NAMES',
    'LOCAL_MAX_BODY_SIZE',
    'max_body_size',
    'caller_capped_request',
    'local_server_names',
    'make_hypercorn_config',
    'build_asgi_app',
    'shared_config_values',
]
