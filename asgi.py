"""ASGI entry point — the target for any server that must serve /peer_link.

WHY THIS EXISTS
    `/peer_link` is a WebSocket. A server handed the plain WSGI `app`
    (`hart_intelligence_entry:app`) can never serve it, because WSGI has no
    websocket scope. That is how the waitress systemd unit and the
    gunicorn/gevent cloud image both ended up with PeerLink.accept() never
    firing and PeerLinkManager._links permanently empty.

    Pointing those servers here instead gives them the same ASGI stack that
    `_serve_app` builds -- `peer_link_asgi(AsyncioWSGIMiddleware(app))` via the
    one canonical `core.serve.build_asgi_app` -- WITHOUT running `main()`.
    That distinction is the point: `main()` also does boot integrity
    verification, guardrail hash enforcement (which can refuse to boot),
    EventBus bootstrap, Crossbar subscribers, the skill registry and the agent
    daemon supervisor. Those belong to the full-node launcher, not to "serve
    HTTP + websockets", and turning them on is a separate decision.

HOW TO USE IT
    hypercorn:  hypercorn asgi:application --bind 0.0.0.0:6777
    uvicorn:    uvicorn asgi:application --host 0.0.0.0 --port 6777
    gunicorn:   gunicorn -k uvicorn.workers.UvicornWorker asgi:application

    Do NOT point a pure-WSGI server (waitress, gunicorn's default sync or
    plain-gevent workers) at `hart_intelligence_entry:app` if the node is
    expected to accept peers -- it will serve HTTP correctly and drop every
    websocket, logging nothing.

WHY IMPORTING THIS IS ALLOWED TO FAIL
    `build_asgi_app` imports hypercorn's middleware. If that is missing, this
    module raises at import and the server refuses to start. That is
    deliberate: the alternative is `_serve_app`'s ImportError fallback to
    Waitress, which starts fine and silently has no `/peer_link`. A server that
    will not boot is a better failure than a peer that is invisible to the hive.
    See core/peer_link/server.py and the requirements.txt note on hypercorn's
    subtree (the root Dockerfile installs with --no-deps).

EXECUTOR NOTE
    `_serve_app` installs a ThreadPoolExecutor sized by HEVOLVE_WORKER_THREADS
    (default 256) as the loop's default executor; the bare server CLIs do not,
    so sync Flask handlers run on asyncio's default executor
    (min(32, cpu_count + 4)). For a regional/central node that is a different
    concurrency profile from waitress's threads=50, not a strictly smaller one.
    If a deployment needs the larger pool, launch through
    `python hart_intelligence_entry.py` instead, which runs `_serve_app`.
"""
from core.serve import build_asgi_app
from hart_intelligence_entry import app

# The name servers target. Built once at import, against a fully-routed app:
# every register_blueprint()/init_social() call in hart_intelligence_entry runs
# at module level (lines ~1016-1177), well before this import completes, which
# is the same reason `hart_intelligence_entry:app` works for gunicorn today.
application = build_asgi_app(app)

# Alias — uvicorn/gunicorn examples in the wild use either name.
asgi_app = application

__all__ = ['application', 'asgi_app']
