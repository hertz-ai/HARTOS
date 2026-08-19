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
      sets because Hypercorn 0.17.3's AsyncioWSGIMiddleware 404s every Flask
      route in that configuration.
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
from typing import Any, List, Optional, Sequence

# Every entry point set these to the same values before this module existed.
# Named so a reader can tell "shared default" from "this deployment's choice".
KEEP_ALIVE_TIMEOUT = 120                     # SSE-friendly long polls
MAX_INCOMPLETE_SIZE = 16 * 1024 * 1024       # 16MB request bodies
ACCESS_LOG = None                            # each app emits its own access log
ERROR_LOG = '-'


def make_hypercorn_config(bind: Sequence[str],
                          *, server_names: Optional[Sequence[str]] = None) -> Any:
    """A hypercorn Config with the settings all three entry points share.

    `bind` is passed through verbatim -- host:port, unix:<path>, whatever the
    caller resolved.  `server_names` is set only when given, because two of the
    three never set it and adding one would change Host-header handling.

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

    return peer_link_asgi(AsyncioWSGIMiddleware(wsgi_app))


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
    'make_hypercorn_config',
    'build_asgi_app',
    'shared_config_values',
]
