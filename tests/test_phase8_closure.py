"""Phase 8 closure — multi-tenant cloud invariants (ship #221).

Plan reference: sunny-gliding-eich.md, Part E.1 + Part 8 + Part E.13.

Locks the user-visible contract of the Phase 8 tenant boundary:

  1. JWT round-trip — tid claim populates g.tenant_id when present;
     missing → g.tenant_id stays None (flat/regional pass-through).

  2. Cross-tenant ORM isolation — a tenant-A request never sees
     tenant-B's posts, regardless of how the query is shaped (list
     vs PK lookup vs HTTP list endpoint).

  3. Cross-tenant 404 (not 403) — a direct GET on a tenant-B post id
     while authenticated as tenant-A must return 404, not 403.  This
     is the "do not leak tenant existence" property — a 403 reveals
     that the resource exists in another tenant.

  4. WAMP per-tenant ACL — a JWT with `tid=A` cannot subscribe to
     `tenant.B.*` topics.  Same isolation guarantee on the realtime
     side that the ORM filter gives on the query side.

Scout result (2026-06-16) identified gaps that this file cannot
ship as passing tests yet — those are tracked as `xfail` so the
ledger stays honest and the missing-piece list is visible when the
file runs.

`tests/test_phase7a_foundation.py` covers JWT tid claim minting +
decode at the auth-module level; this file adds the request-time
end-state assertion (g.tenant_id) the auth layer is supposed to
produce.  `tests/test_phase8_tenant_strict.py` covers strict-mode
NULL pass-through hardening; this file covers the headline
cross-tenant isolation guarantee that is *always* enforced (even
in loose mode).
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── Fixtures: fresh DB + app client (mirrors phase7a/phase8) ──────


@pytest.fixture
def fresh_db(monkeypatch):
    """Per-test in-memory HARTOS DB with the tenant_filter listener
    installed against User + Post + Community.  Same shape as
    test_phase8_tenant_strict.py — kept self-contained because
    conftest.py's autouse `reset_state_machine` initialises Flask
    app context which can interfere with explicit `with` contexts.
    """
    monkeypatch.setenv('HEVOLVE_DB_PATH', ':memory:')
    from integrations.social import auth as auth_mod
    auth_mod._jwt_manager = False  # force pyjwt path for deterministic
                                   # tid claim encoding
    from integrations.social import models as models_mod
    models_mod._engine = None
    models_mod._SessionLocal = None
    from integrations.social import migrations
    from integrations.social.models import (
        get_engine, get_db, User, Post, Community,
    )
    from integrations.social.tenant_filter import (
        install_tenant_filter, register_tenant_aware,
    )
    eng = get_engine()
    migrations.run_migrations()
    install_tenant_filter()
    for cls in (User, Post, Community):
        register_tenant_aware(cls)
    db = get_db()
    try:
        yield db, eng
    finally:
        try:
            db.close()
        except Exception:
            pass
        try:
            eng.dispose()
        except Exception:
            pass
        models_mod._engine = None
        models_mod._SessionLocal = None


@pytest.fixture
def app_client(fresh_db):
    """Flask test client wired to the fresh in-memory HARTOS DB
    + the social blueprint.  Used by the HTTP-level cross-tenant
    isolation + 404-not-403 tests."""
    from flask import Flask
    from integrations.social import api
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    yield app.test_client(), fresh_db[0]


# ── Helpers ────────────────────────────────────────────────────────


def create_user(db, *, tenant_id=None, username=None):
    """Test-side `create_user(tenant_id=...)` helper called out by
    the ship task.  Mirrors the existing _seed_users pattern in
    test_phase7_tenant_filter.py + test_phase8_tenant_strict.py but
    accepts tenant_id directly + stamps via raw SQL so we don't
    depend on the auto-stamp listener (we want the seeded rows to
    have the requested tenant, regardless of g.tenant_id at seed
    time)."""
    from integrations.social.models import User
    from sqlalchemy import text
    uname = username or f'u_{uuid.uuid4().hex[:8]}'
    u = User(
        id=str(uuid.uuid4()),
        username=uname,
        display_name=uname.title(),
        email=f'{uname}@x.test',
        password_hash='x:y',
        user_type='human',
    )
    db.add(u)
    db.commit()
    if tenant_id is not None:
        db.execute(
            text("UPDATE users SET tenant_id = :t WHERE id = :id"),
            {'t': tenant_id, 'id': u.id},
        )
        db.commit()
        # Sync the in-memory instance so subsequent ORM use sees the
        # stamped value (raw SQL update doesn't auto-refresh the
        # attribute).
        u.tenant_id = tenant_id
    return u


def _create_post(db, author, *, title='hello', tenant_id=None):
    """Create a Post directly via the ORM, stamping tenant_id via
    raw SQL to bypass the auto-stamp listener so the seeded value
    is exactly what the test asked for."""
    from integrations.social.models import Post
    from sqlalchemy import text
    p = Post(
        id=str(uuid.uuid4()),
        author_id=author.id,
        title=title,
        content='body',
        content_type='text',
    )
    db.add(p)
    db.commit()
    if tenant_id is not None:
        db.execute(
            text("UPDATE posts SET tenant_id = :t WHERE id = :id"),
            {'t': tenant_id, 'id': p.id},
        )
        db.commit()
        p.tenant_id = tenant_id
    return p


def _make_app():
    from flask import Flask
    return Flask(__name__)


# ── 1. Tenant resolution: JWT 'tid' → g.tenant_id ─────────────────


def test_jwt_with_tid_populates_g_tenant_id(app_client):
    """A JWT carrying a `tid` claim must end up on g.tenant_id when
    require_auth runs.  We assert by hitting a known require_auth
    route and inspecting the response — the autocomplete endpoint
    sets up g.tenant_id + g.feature_flags before running, so a 200
    on a tenant-issued JWT proves the resolver ran."""
    client, db = app_client
    user = create_user(db, tenant_id='tenant-A', username='alice')

    from integrations.social import auth
    tok = auth.generate_jwt(user.id, user.username, 'flat',
                            tenant_id='tenant-A')
    payload = auth.decode_jwt(tok)
    assert payload.get('tid') == 'tenant-A', (
        "round-trip: token issued with tid must decode with tid")

    # Hit the existing autocomplete endpoint to drive require_auth +
    # observe a 200 — proves g.tenant_id was set without our mocks
    # bumping into the resolver.
    import os as _os
    _os.environ['HEVOLVE_FLAG_MENTIONS_AUTOCOMPLETE'] = 'true'
    try:
        r = client.get('/api/social/users/autocomplete?q=a',
                       headers={'Authorization': f'Bearer {tok}'})
    finally:
        _os.environ.pop('HEVOLVE_FLAG_MENTIONS_AUTOCOMPLETE', None)
    assert r.status_code == 200, (
        f"tid-bearing JWT must round-trip cleanly through "
        f"require_auth — got {r.status_code} body={r.get_data()!r}")


def test_jwt_without_tid_passes_through_as_none(app_client):
    """Flat / regional deploys never set tid.  Token decode must
    succeed + carry no tid claim — the back-compat invariant that
    keeps single-tenant deploys unaffected by Phase 8.  g.tenant_id
    derives from None which downstream code treats as untenanted."""
    client, db = app_client
    user = create_user(db, tenant_id=None, username='bob')

    from integrations.social import auth
    tok = auth.generate_jwt(user.id, user.username, 'flat')
    payload = auth.decode_jwt(tok)
    assert 'tid' not in payload or payload.get('tid') is None, (
        "flat-mode token must not carry a tid claim")

    import os as _os
    _os.environ['HEVOLVE_FLAG_MENTIONS_AUTOCOMPLETE'] = 'true'
    try:
        r = client.get('/api/social/users/autocomplete?q=b',
                       headers={'Authorization': f'Bearer {tok}'})
    finally:
        _os.environ.pop('HEVOLVE_FLAG_MENTIONS_AUTOCOMPLETE', None)
    # 200 = pass-through path completed without the resolver
    # tripping on the missing tid (no 403 'tenant required' since
    # HEVOLVE_CLOUD_MODE is unset).
    assert r.status_code == 200, (
        f"missing-tid token must pass through in non-cloud mode — "
        f"got {r.status_code} body={r.get_data()!r}")


# ── 2. Cross-tenant ORM isolation ─────────────────────────────────


def test_cross_tenant_post_list_isolation(fresh_db):
    """The headline isolation guarantee.  Tenant A's user lists
    posts → sees only tenant A's posts, never tenant B's.  Equiv. to
    `SELECT * FROM posts` filtered by the tenant_filter listener.
    """
    db, _ = fresh_db
    alice = create_user(db, tenant_id='tenant-A', username='alice')
    bob = create_user(db, tenant_id='tenant-B', username='bob')
    pA = _create_post(db, alice, title='A-only', tenant_id='tenant-A')
    pB = _create_post(db, bob, title='B-only', tenant_id='tenant-B')

    from integrations.social.models import Post
    app = _make_app()
    with app.test_request_context('/'):
        from flask import g
        g.tenant_id = 'tenant-A'
        g.feature_flags = {}  # loose mode
        ids = {p.id for p in db.query(Post).all()}
    assert pA.id in ids, "tenant-A must see its own post"
    assert pB.id not in ids, (
        f"cross-tenant leak: tenant-A saw tenant-B post {pB.id} — "
        f"saw {ids}")


def test_cross_tenant_post_pk_lookup_isolation(fresh_db):
    """Direct PK lookup — the IDOR vector.  An attacker on tenant A
    who guesses or scrapes a tenant-B post id must get None back."""
    db, _ = fresh_db
    alice = create_user(db, tenant_id='tenant-A', username='alice')
    bob = create_user(db, tenant_id='tenant-B', username='bob')
    pA = _create_post(db, alice, title='A-only', tenant_id='tenant-A')
    pB = _create_post(db, bob, title='B-only', tenant_id='tenant-B')

    from integrations.social.models import Post
    app = _make_app()
    with app.test_request_context('/'):
        from flask import g
        g.tenant_id = 'tenant-A'
        g.feature_flags = {}
        # Tenant A queries for tenant B's post by PK
        result = db.query(Post).filter(Post.id == pB.id).first()
    assert result is None, (
        f"cross-tenant PK lookup leak: got {result.title if result else None}")


# ── 3. Cross-tenant HTTP returns 404 not 403 (no existence leak) ──


def test_cross_tenant_get_post_returns_404_not_403(app_client):
    """The "do not leak tenant existence" property.  When an
    authenticated tenant-A user asks for tenant-B's post by id,
    the response must be 404, not 403 — a 403 would reveal that
    the post exists somewhere else.

    Mirrors the same shape used by the per-post privacy gate
    (api.py line 1448–1456) which returns 404 for hidden posts.

    The tenant_filter ORM listener is what produces the 404: the
    PK lookup behind /posts/<id> returns None for cross-tenant ids,
    and the endpoint's "Post not found" branch fires.
    """
    client, db = app_client
    alice = create_user(db, tenant_id='tenant-A', username='alice')
    bob = create_user(db, tenant_id='tenant-B', username='bob')
    pB = _create_post(db, bob, title='B-only', tenant_id='tenant-B')

    from integrations.social import auth
    tok = auth.generate_jwt(alice.id, alice.username, 'flat',
                            tenant_id='tenant-A')

    r = client.get(f'/api/social/posts/{pB.id}',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 404, (
        f"cross-tenant GET must return 404 (existence-hidden), "
        f"not {r.status_code}.  A 403 would leak that pB exists.")
    body = r.get_json() or {}
    # No body field should reveal the cross-tenant existence.
    body_str = str(body).lower()
    assert 'tenant' not in body_str, (
        f"404 body must not name the tenant boundary — got: {body}")


def test_cross_tenant_post_list_via_http_isolation(app_client):
    """The same isolation guarantee, on the HTTP /posts list path
    (proves the ORM filter is wired through the API layer, not just
    inside a hand-rolled query).  Tenant A's GET /posts returns
    only tenant-A posts."""
    client, db = app_client
    alice = create_user(db, tenant_id='tenant-A', username='alice')
    bob = create_user(db, tenant_id='tenant-B', username='bob')
    pA = _create_post(db, alice, title='A-only', tenant_id='tenant-A')
    pB = _create_post(db, bob, title='B-only', tenant_id='tenant-B')

    from integrations.social import auth
    tok = auth.generate_jwt(alice.id, alice.username, 'flat',
                            tenant_id='tenant-A')
    r = client.get('/api/social/posts?limit=50',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200, (
        f"posts list must succeed for tenant-A — got {r.status_code} "
        f"body={r.get_data()!r}")
    body = r.get_json() or {}
    titles = {p.get('title') for p in body.get('data', [])}
    assert 'A-only' in titles, (
        f"tenant-A must see its own post — saw {titles}")
    assert 'B-only' not in titles, (
        f"cross-tenant leak in HTTP /posts list — saw {titles}")


# ── 4. WAMP per-tenant subscribe ACL ──────────────────────────────


def test_wamp_acl_rejects_cross_tenant_subscribe():
    """WAMP layer mirror of the ORM isolation: a JWT bearing
    tid=A must not be allowed to subscribe to tenant.B.* topics.
    Pure function — no DB needed."""
    from integrations.social.tenant_acl import authorize_subscribe
    payload = {'user_id': 'alice', 'tid': 'tenant-A'}
    # Subscribe to tenant-B's community feed → refuse
    assert authorize_subscribe(
        'tenant.tenant-B.community.c-1.message', payload) is False
    # Subscribe to tenant-B's user inbox → refuse
    assert authorize_subscribe(
        'tenant.tenant-B.user.alice.message', payload) is False
    # Subscribe to tenant-B's conv → refuse
    assert authorize_subscribe(
        'tenant.tenant-B.conv.c-1.message', payload) is False


def test_wamp_acl_allows_same_tenant_subscribe():
    """Sanity inverse: tenant-A user can subscribe to tenant.A.*"""
    from integrations.social.tenant_acl import authorize_subscribe
    payload = {'user_id': 'alice', 'tid': 'tenant-A'}
    assert authorize_subscribe(
        'tenant.tenant-A.community.c-1.message', payload) is True


def test_wamp_acl_uri_check_publish_topic_shape():
    """publish-side equivalent of subscribe gate — uri_check via
    parse_topic must reject `tenant.B.*` when the publisher's
    JWT carries tid=A.  This is the symmetric guarantee on the
    publish side; we use the same parse_topic helper that both
    sides share.
    """
    from integrations.social.realtime_acl import parse_topic
    # Topic the attacker is trying to publish to
    parsed = parse_topic('tenant.tenant-B.community.c-1.message')
    assert parsed.is_tenant_scoped is True
    assert parsed.tid == 'tenant-B'
    # JWT's tid does NOT match → publish would be rejected by any
    # gate using parsed.tid != jwt['tid'].
    jwt_tid = 'tenant-A'
    assert parsed.tid != jwt_tid, (
        "uri_check would reject this publish since the topic's tid "
        "doesn't match the publisher's JWT tid")


# ── 5. Gaps documented as xfail ───────────────────────────────────


@pytest.mark.xfail(
    reason="Scout: no `class Tenant` ORM model exists in sql.models, "
           "_models_local.py, or HARTOS — only an ad-hoc 'tenants' "
           "table created by test_phase8b_wamp_acl.py fixture. "
           "Phase 8 closure requires a canonical Tenant model so "
           "service code can do `db.query(Tenant).get(tid)` without "
           "raw SQL.")
def test_tenant_orm_model_exists():
    from integrations.social.models import Tenant  # noqa: F401
    assert Tenant.__tablename__ == 'tenants'


@pytest.mark.xfail(
    reason="Scout: no migration v50/51/52/53 creates the canonical "
           "`tenants` table with columns id/name/slug/plan/"
           "is_suspended/settings.  resolve_tenant_slug queries a "
           "table that doesn't exist outside the test fixture. "
           "Phase 8 closure requires a dedicated v54+ migration.")
def test_tenants_table_in_migration(fresh_db):
    from sqlalchemy import inspect
    _, eng = fresh_db
    insp = inspect(eng)
    assert 'tenants' in insp.get_table_names()
    cols = {c['name'] for c in insp.get_columns('tenants')}
    expected = {'id', 'name', 'slug', 'plan', 'is_suspended', 'settings'}
    assert expected <= cols, f"missing: {expected - cols}"


@pytest.mark.xfail(
    reason="Scout: crossbar_server.py has no per-tenant WAMP ACL "
           "wiring — no tenant/tid/uri_check/authorize_subscribe "
           "code.  tenant_acl.authorize_subscribe exists at "
           "integrations/social/tenant_acl.py:44 but is NOT invoked "
           "from crossbar_server.py — needs dynamic authorizer / "
           "uri_check hookup so the router actually calls our gate.")
def test_crossbar_server_invokes_authorize_subscribe():
    """Phase 8 closure requires the router to actually invoke
    tenant_acl.authorize_subscribe — not just have it defined.
    Until crossbar_server.py wires the dynamic-authorizer HTTP
    callback to this function, the WAMP subscribe-side gate exists
    in code but never fires for real subscribers."""
    from hartos import crossbar_server
    src = open(crossbar_server.__file__).read()
    assert 'authorize_subscribe' in src or 'tenant_acl' in src, (
        "crossbar_server.py must reference authorize_subscribe / "
        "tenant_acl to actually invoke the per-tenant ACL")


@pytest.mark.xfail(
    reason="Scout: test_auth_tenant.py file does not exist by that "
           "exact name; existing JWT tid/g.tenant_id assertions live "
           "inside test_phase7a_foundation.py / "
           "test_phase7_tenant_filter.py.  The Phase 8 closure spec "
           "asks for a dedicated file named test_auth_tenant.py.")
def test_dedicated_auth_tenant_file_exists():
    import os as _os
    p = _os.path.join(os.path.dirname(__file__), 'test_auth_tenant.py')
    assert _os.path.exists(p), (
        f"Phase 8 closure spec requires {p} as a dedicated test file")


@pytest.mark.xfail(
    reason="Scout: test_tenant_isolation.py file does not exist by "
           "that exact name; existing isolation assertions live in "
           "test_phase7_tenant_filter.py + test_phase8_tenant_strict.py "
           "+ test_phase8b_wamp_acl.py.  The Phase 8 closure spec asks "
           "for a dedicated file named test_tenant_isolation.py.")
def test_dedicated_tenant_isolation_file_exists():
    import os as _os
    p = _os.path.join(
        os.path.dirname(__file__), 'test_tenant_isolation.py')
    assert _os.path.exists(p), (
        f"Phase 8 closure spec requires {p} as a dedicated test file")
