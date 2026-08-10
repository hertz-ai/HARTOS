"""The five self-declared 'Admin:' integrity routes must actually require admin.

Found 2026-08-07 while live-verifying the P2P federation fixes: I unwedged
the LAN peer's expired bans by calling

    POST http://192.168.0.15:5000/api/social/integrity/node/<id>/audit

from this machine with NO credentials, and it returned 200. That worked in
our favour, but the same shape reaches ``/ban``, whose body takes a
``node_id`` from the URL and bans it outright. Banning is a
denial-of-federation primitive: _merge_peer rejects a banned peer's
announces AND receive_inbox rejects its posts, so a handful of anonymous
POSTs can partition a node from the network. ``unban`` is the mirror image
— it clears bans placed for real fraud.

``integrity_alerts`` proves the original author meant to gate these: it
does ``from .auth import require_admin`` INSIDE the function body and never
applies it. An import is not a decorator; that guard could never fire (the
'vacuous guard' family).

THE RULE THIS FILE ENFORCES: a route whose own docstring opens with
"Admin:" must be decorated with the canonical ``require_admin``. Nothing
more — the rest of this blueprint is peer-to-peer protocol that MUST stay
open (a remote peer has no user account here; it authenticates with an
Ed25519 signature instead), and the second test pins exactly that so a
future "secure the blueprint" sweep cannot quietly kill federation.
"""
import ast
import inspect

import pytest
from flask import Flask

import integrations.social.discovery as discovery


# Routes whose docstring declares them admin-only. Kept as data so the
# drift test below can prove the list still matches the source.
ADMIN_ROUTES = [
    ('GET', '/api/social/integrity/alerts'),
    ('PATCH', '/api/social/integrity/alerts/some-alert-id'),
    ('POST', '/api/social/integrity/node/some-node-id/audit'),
    ('POST', '/api/social/integrity/node/some-node-id/ban'),
    ('GET', '/api/social/integrity/dashboard'),
]

# Peer-to-peer protocol surface. These carry their own crypto-based trust
# model and MUST remain reachable without a user session.
PEER_PROTOCOL_ROUTES = [
    ('POST', '/api/social/peers/announce'),
    ('POST', '/api/social/peers/exchange'),
    ('GET', '/api/social/peers/health'),
    ('GET', '/api/social/peers'),
    ('POST', '/api/social/federation/inbox'),
    ('GET', '/api/social/federation/outbox'),
    ('POST', '/api/social/federation/follow-notification'),
    ('GET', '/.well-known/hevolve-social.json'),
]


@pytest.fixture
def client():
    app = Flask(__name__)
    app.config['TESTING'] = True
    # Return 500 rather than re-raising, so a handler that explodes on
    # unrelated state (see below) is still an observable STATUS CODE and our
    # assertions can judge it, instead of aborting the test as an error.
    app.config['PROPAGATE_EXCEPTIONS'] = False
    app.register_blueprint(discovery.discovery_bp)
    return app.test_client()


def _call(client, method, path):
    return getattr(client, method.lower())(
        path, json={} if method in ('POST', 'PATCH') else None)


@pytest.mark.parametrize('method,path', ADMIN_ROUTES)
def test_admin_route_rejects_anonymous(client, method, path):
    """No Authorization header -> 401, and the handler body never runs."""
    resp = _call(client, method, path)
    assert resp.status_code == 401, (
        f'{method} {path} answered {resp.status_code} to an anonymous '
        f'caller; an admin-declared route must reject it')


@pytest.mark.parametrize('method,path', ADMIN_ROUTES)
def test_admin_route_rejects_garbage_bearer(client, method, path):
    """A malformed/forged token NEVER grants access.

    Asserted as "not 2xx" rather than "== 401" on purpose. Rejecting a bad
    token requires a user lookup, so this path touches the DB — and
    tests/unit/test_gossip_security.py does
    ``os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')`` at MODULE level,
    which pytest executes during collection and therefore applies to every
    test in the session. Under that empty DB the lookup raises "no such table:
    users" and the route answers 500 instead of 401 — which made this guard
    pass alone and fail in a full run (measured 2026-08-08).

    A guard whose verdict depends on which other files were collected is not
    a guard. The SECURITY property is "a forged token does not get in", and
    that holds under either condition; the exact rejection code is not the
    claim. test_admin_route_rejects_anonymous still pins 401 exactly — that
    path returns before any DB access, so it is stable.
    """
    resp = getattr(client, method.lower())(
        path,
        json={} if method in ('POST', 'PATCH') else None,
        headers={'Authorization': 'Bearer not-a-real-token'})
    assert not (200 <= resp.status_code < 300), (
        f'{method} {path} ACCEPTED a forged bearer token '
        f'(status {resp.status_code})')


@pytest.mark.parametrize('method,path', PEER_PROTOCOL_ROUTES)
def test_peer_protocol_stays_open(client, method, path):
    """ZERO-REGRESSION PIN: federation must not need a user session.

    These may fail for other reasons in a bare test app (no DB, missing
    body) — that is fine and deliberately not asserted. The ONE thing that
    must never happen is an auth rejection, because that would silently
    disconnect this node from every peer.
    """
    resp = _call(client, method, path)
    assert resp.status_code not in (401, 403), (
        f'{method} {path} is peer-protocol and must never require a user '
        f'session, but answered {resp.status_code}')


def test_every_admin_declared_route_is_decorated():
    """Drift guard: if someone adds a new "Admin:" route, it must be gated.

    Walks the source rather than trusting ADMIN_ROUTES to stay current, so
    a newly-added admin endpoint fails here instead of shipping open.
    """
    src = inspect.getsource(discovery)
    tree = ast.parse(src)
    ungated = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        doc = (ast.get_docstring(node) or '').strip()
        first = doc.splitlines()[0].lower() if doc else ''
        is_route = any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            and d.func.attr == 'route' for d in node.decorator_list)
        if not is_route or not first.startswith('admin'):
            continue
        names = {d.id for d in node.decorator_list if isinstance(d, ast.Name)}
        if 'require_admin' not in names:
            ungated.append(node.name)
    assert not ungated, (
        f'these routes declare themselves Admin: but carry no '
        f'require_admin decorator: {ungated}')


def test_require_admin_is_imported_at_module_level_not_in_a_body():
    """A decorator must resolve at def time.

    integrity_alerts used to do `from .auth import require_admin` INSIDE the
    function — an import that decorates nothing. Pin the real module-level
    import so that shape cannot come back.
    """
    assert getattr(discovery, 'require_admin', None) is not None, \
        'require_admin must be a module-level name in discovery.py'
    src = inspect.getsource(discovery)
    tree = ast.parse(src)
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom):
                assert not any(a.name == 'require_admin' for a in inner.names), (
                    f'{node.name} imports require_admin inside its body — '
                    f'that decorates nothing')
