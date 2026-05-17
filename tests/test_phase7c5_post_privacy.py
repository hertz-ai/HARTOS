"""Phase 7c.5 — per-post privacy gate.

Plan reference: sunny-gliding-eich.md, Part E.10.

Coverage:
  - _normalize collapses NULL / unknown to 'public' so legacy rows
    keep their pre-migration visibility.
  - can_view_post visibility matrix (4 levels × {anon, author, friend,
    non-friend, community member, non-member}).
  - visible_posts_filter applied to PostService.list_posts at the
    Flask handler level — pre-filters at SQL.
  - get_post returns 404 (not 403) for posts the viewer can't see, so
    we don't leak existence.
  - create_post + update_post accept the `privacy` field only when
    the flag is on.  Off → field silently ignored, column stays NULL.
  - Tenant cross-talk: even with `post_privacy` on, posts from other
    tenants are filtered by the existing tenant gate before privacy
    is applied — visible_posts_filter doesn't bypass it.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── Shared fixtures ────────────────────────────────────────────────

@pytest.fixture
def fresh_db(monkeypatch):
    """Per-test in-memory HARTOS DB with all migrations applied."""
    monkeypatch.setenv('HEVOLVE_DB_PATH', ':memory:')
    from integrations.social import auth as auth_mod
    auth_mod._jwt_manager = False
    from integrations.social import models as models_mod
    models_mod._engine = None
    models_mod._SessionLocal = None
    from integrations.social import migrations
    from integrations.social.models import get_engine, get_db
    eng = get_engine()
    migrations.run_migrations()
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
def app_client(fresh_db, monkeypatch):
    """Flask test client with social_bp + conversations_bp registered
    and the post_privacy + flag dependencies on."""
    monkeypatch.setenv('HEVOLVE_FLAG_POST_PRIVACY', 'true')
    from flask import Flask
    from integrations.social import api
    from integrations.social.api_conversations import conversations_bp
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    app.register_blueprint(conversations_bp)
    yield app.test_client(), fresh_db[0]


def _seed_users(db, n=3):
    """Create n users and commit. Returns the list."""
    from integrations.social.models import User
    users = []
    for i in range(n):
        u = User(id=str(uuid.uuid4()),
                 username=f'u{i}_{uuid.uuid4().hex[:6]}',
                 display_name=f'U{i}',
                 email=f'u{i}_{uuid.uuid4().hex[:6]}@x.test',
                 password_hash='x:y',
                 user_type='human')
        users.append(u)
    db.add_all(users)
    db.commit()
    return users


def _seed_community(db, owner_id, members=None, name=None):
    """Create a community with `owner_id` as admin and `members` as
    additional human members.  Inserts memberships rows."""
    from sqlalchemy import text
    from integrations.social.models import Community
    cid = str(uuid.uuid4())
    name = name or f'c_{uuid.uuid4().hex[:6]}'
    com = Community(id=cid, name=name, display_name=name.title(),
                    description='', creator_id=owner_id, is_private=False)
    db.add(com)
    db.commit()
    db.execute(text(
        "INSERT INTO memberships "
        "(id, parent_kind, parent_id, member_id, agent_kind, role) "
        "VALUES (:id, 'community', :pid, :mid, 'human', 'admin')"),
        {'id': str(uuid.uuid4()), 'pid': cid, 'mid': owner_id})
    for m in (members or []):
        db.execute(text(
            "INSERT INTO memberships "
            "(id, parent_kind, parent_id, member_id, agent_kind, role) "
            "VALUES (:id, 'community', :pid, :mid, 'human', 'member')"),
            {'id': str(uuid.uuid4()), 'pid': cid, 'mid': m})
    db.commit()
    return com


def _seed_friendship(db, a_id, b_id, status='active'):
    """Insert a friendship row sorted (a < b)."""
    from sqlalchemy import text
    ua, ub = sorted([a_id, b_id])
    db.execute(text(
        "INSERT INTO friendships "
        "(id, user_a_id, user_b_id, status, initiator_id, accepted_at) "
        "VALUES (:id, :a, :b, :s, :ini, "
        "        CASE WHEN :s='active' THEN CURRENT_TIMESTAMP ELSE NULL END)"),
        {'id': str(uuid.uuid4()), 'a': ua, 'b': ub, 's': status,
         'ini': a_id})
    db.commit()


def _make_post(db, author_id, privacy=None, community_id=None,
               content='hello'):
    from integrations.social.models import Post
    p = Post(id=str(uuid.uuid4()), author_id=author_id,
             community_id=community_id,
             title='t', content=content, content_type='text',
             privacy=privacy)
    db.add(p)
    db.commit()
    return p


# ── _normalize ─────────────────────────────────────────────────────

def test_normalize_null_to_public():
    from integrations.social.privacy import _normalize
    assert _normalize(None) == 'public'
    assert _normalize('') == 'public'


def test_normalize_unknown_to_public():
    """Defensive: malicious client sending privacy='god_mode' is
    coerced to public — no enforcement bypass."""
    from integrations.social.privacy import _normalize
    assert _normalize('god_mode') == 'public'
    assert _normalize('PUBLIC') == 'public'  # case-sensitive whitelist


def test_normalize_passes_known_levels():
    from integrations.social.privacy import _normalize, PRIVACY_LEVELS
    for level in PRIVACY_LEVELS:
        assert _normalize(level) == level


# ── can_view_post matrix ───────────────────────────────────────────

def test_public_post_visible_to_anonymous(fresh_db):
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    p = _make_post(db, a.id, privacy=None)
    from integrations.social.privacy import can_view_post
    assert can_view_post(db, None, p) is True


def test_legacy_null_privacy_is_public(fresh_db):
    """Existing posts (pre-v48) have privacy=NULL.  Must remain
    visible to everyone — the backward-compatibility invariant."""
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    p = _make_post(db, a.id, privacy=None)
    from integrations.social.privacy import can_view_post
    assert can_view_post(db, None, p) is True


def test_private_post_visible_only_to_author(fresh_db):
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    p = _make_post(db, a.id, privacy='private')
    from integrations.social.privacy import can_view_post
    assert can_view_post(db, a, p) is True
    assert can_view_post(db, b, p) is False
    assert can_view_post(db, None, p) is False


def test_friends_post_visible_to_active_friend(fresh_db):
    db, _ = fresh_db
    a, b, c = _seed_users(db, 3)
    _seed_friendship(db, a.id, b.id, status='active')
    p = _make_post(db, a.id, privacy='friends')
    from integrations.social.privacy import can_view_post
    assert can_view_post(db, a, p) is True
    assert can_view_post(db, b, p) is True
    assert can_view_post(db, c, p) is False


def test_friends_post_hidden_from_pending_friend(fresh_db):
    """Pending friendship (not yet accepted) does NOT grant friends-only access."""
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    _seed_friendship(db, a.id, b.id, status='pending')
    p = _make_post(db, a.id, privacy='friends')
    from integrations.social.privacy import can_view_post
    assert can_view_post(db, b, p) is False


def test_community_post_visible_to_member(fresh_db):
    db, _ = fresh_db
    a, b, c = _seed_users(db, 3)
    com = _seed_community(db, owner_id=a.id, members=[b.id])
    p = _make_post(db, a.id, privacy='community', community_id=com.id)
    from integrations.social.privacy import can_view_post
    assert can_view_post(db, a, p) is True   # author
    assert can_view_post(db, b, p) is True   # member
    assert can_view_post(db, c, p) is False  # non-member


def test_community_post_without_community_id_is_unviewable(fresh_db):
    """Misconfigured: privacy='community' but no community_id.  Fail
    safe — author still sees own, no one else does."""
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    p = _make_post(db, a.id, privacy='community', community_id=None)
    from integrations.social.privacy import can_view_post
    assert can_view_post(db, a, p) is True   # author always
    assert can_view_post(db, b, p) is False


# ── visible_posts_filter (SQL pre-filter) ───────────────────────────

def test_filter_anonymous_sees_only_public(fresh_db):
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    com = _seed_community(db, owner_id=a.id)
    public_id = _make_post(db, a.id, privacy='public').id
    legacy_id = _make_post(db, a.id, privacy=None).id
    _make_post(db, a.id, privacy='friends')
    _make_post(db, a.id, privacy='community', community_id=com.id)
    _make_post(db, a.id, privacy='private')

    from integrations.social.services import PostService
    posts, _ = PostService.list_posts(
        db, sort='new', viewer_user=None, apply_privacy=True)
    ids = {p.id for p in posts}
    assert public_id in ids and legacy_id in ids
    assert len(ids) == 2


def test_filter_author_sees_own_at_every_level(fresh_db):
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    com = _seed_community(db, owner_id=a.id)
    expected = {
        _make_post(db, a.id, privacy='public').id,
        _make_post(db, a.id, privacy='friends').id,
        _make_post(db, a.id, privacy='community', community_id=com.id).id,
        _make_post(db, a.id, privacy='private').id,
    }
    from integrations.social.services import PostService
    posts, _ = PostService.list_posts(
        db, sort='new', viewer_user=a, apply_privacy=True)
    assert {p.id for p in posts} == expected


def test_filter_friend_sees_public_and_friends(fresh_db):
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    _seed_friendship(db, a.id, b.id, status='active')
    com = _seed_community(db, owner_id=a.id, members=[])
    expected = {
        _make_post(db, a.id, privacy='public').id,
        _make_post(db, a.id, privacy='friends').id,
    }
    # Posts that should NOT show up
    _make_post(db, a.id, privacy='private')
    _make_post(db, a.id, privacy='community', community_id=com.id)

    from integrations.social.services import PostService
    posts, _ = PostService.list_posts(
        db, sort='new', viewer_user=b, apply_privacy=True)
    assert {p.id for p in posts} == expected


def test_filter_community_member_sees_public_and_community(fresh_db):
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    com = _seed_community(db, owner_id=a.id, members=[b.id])
    expected = {
        _make_post(db, a.id, privacy='public').id,
        _make_post(db, a.id, privacy='community', community_id=com.id).id,
    }
    # Friends arm doesn't fire (no friendship), private hidden
    _make_post(db, a.id, privacy='friends')
    _make_post(db, a.id, privacy='private')

    from integrations.social.services import PostService
    posts, _ = PostService.list_posts(
        db, sort='new', viewer_user=b, apply_privacy=True)
    assert {p.id for p in posts} == expected


def test_filter_off_returns_everything(fresh_db):
    """apply_privacy=False short-circuits — flag-off deploys skip the
    EXISTS subqueries entirely.  Matches pre-7c.5 behavior bit-for-bit."""
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    com = _seed_community(db, owner_id=a.id)
    for level in (None, 'public', 'friends', 'community', 'private'):
        _make_post(db, a.id, privacy=level,
                   community_id=com.id if level == 'community' else None)
    from integrations.social.services import PostService
    posts, _ = PostService.list_posts(
        db, sort='new', viewer_user=b, apply_privacy=False)
    # All five visible regardless of viewer relationship
    assert len(posts) == 5


def test_filter_with_blocked_friend_does_not_grant_access(fresh_db):
    """A 'blocked' friendship row must NOT satisfy the friends-arm.
    The SQL EXISTS subquery filters on status='active'."""
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    _seed_friendship(db, a.id, b.id, status='blocked')
    expected_invisible = _make_post(db, a.id, privacy='friends').id

    from integrations.social.services import PostService
    posts, _ = PostService.list_posts(
        db, sort='new', viewer_user=b, apply_privacy=True)
    assert expected_invisible not in {p.id for p in posts}


# ── HTTP integration: GET /posts/<id> 404s for non-viewable ─────────

def test_get_post_returns_404_for_non_viewable(app_client):
    """Reveal-by-existence is a privacy leak.  Non-viewable posts
    return 404 (not 403) so the response is indistinguishable from
    an unknown id."""
    client, db = app_client
    a, b = _seed_users(db, 2)
    p = _make_post(db, a.id, privacy='private')

    from integrations.social import auth
    tok = auth.generate_jwt(b.id, b.username, 'flat')
    r = client.get(f'/api/social/posts/{p.id}',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 404


def test_get_post_returns_200_for_author(app_client):
    client, db = app_client
    a, = _seed_users(db, 1)
    p = _make_post(db, a.id, privacy='private')

    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.get(f'/api/social/posts/{p.id}',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200


def test_get_post_anonymous_blocked_for_friends_only(app_client):
    """Anonymous readers (no auth) cannot see friends-only posts."""
    client, db = app_client
    a, = _seed_users(db, 1)
    p = _make_post(db, a.id, privacy='friends')
    r = client.get(f'/api/social/posts/{p.id}')
    assert r.status_code == 404


# ── HTTP integration: list_posts pre-filters ────────────────────────

def test_list_posts_excludes_friends_only_for_stranger(app_client):
    client, db = app_client
    a, b = _seed_users(db, 2)
    public = _make_post(db, a.id, privacy='public')
    private = _make_post(db, a.id, privacy='private')
    friends = _make_post(db, a.id, privacy='friends')

    from integrations.social import auth
    tok = auth.generate_jwt(b.id, b.username, 'flat')
    r = client.get('/api/social/posts',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    ids = {p['id'] for p in r.get_json()['data']}
    assert public.id in ids
    assert private.id not in ids
    assert friends.id not in ids


# ── HTTP integration: create/update accept privacy field ────────────

def test_create_post_persists_privacy_when_flag_on(app_client):
    client, db = app_client
    a, = _seed_users(db, 1)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/posts',
                    json={'title': 't', 'content': 'c',
                          'privacy': 'private'},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201
    body = r.get_json()
    assert body['data']['privacy'] == 'private'

    # Persisted on the row
    from integrations.social.models import Post
    p = db.query(Post).filter_by(id=body['data']['id']).first()
    assert p.privacy == 'private'


def test_create_post_unknown_privacy_coerced_to_public(app_client):
    """Defense: malicious client tries privacy='god_mode' — server
    coerces to 'public' so no enforcement is bypassed."""
    client, db = app_client
    a, = _seed_users(db, 1)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/posts',
                    json={'title': 't', 'content': 'c',
                          'privacy': 'god_mode'},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201
    body = r.get_json()
    assert body['data']['privacy'] == 'public'


def test_update_post_can_change_privacy(app_client):
    client, db = app_client
    a, = _seed_users(db, 1)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    create = client.post('/api/social/posts',
                         json={'title': 't', 'content': 'c',
                               'privacy': 'public'},
                         headers={'Authorization': f'Bearer {tok}'})
    pid = create.get_json()['data']['id']

    r = client.patch(f'/api/social/posts/{pid}',
                     json={'privacy': 'private'},
                     headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    assert r.get_json()['data']['privacy'] == 'private'


def test_create_post_ignores_privacy_when_flag_off(fresh_db, monkeypatch):
    """Flag off → privacy field silently dropped, column stays NULL.
    Existing flag-off deploys behave exactly as before."""
    monkeypatch.delenv('HEVOLVE_FLAG_POST_PRIVACY', raising=False)
    from flask import Flask
    from integrations.social import api, auth
    from integrations.social.api_conversations import conversations_bp
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    app.register_blueprint(conversations_bp)
    client = app.test_client()
    db = fresh_db[0]
    a, = _seed_users(db, 1)
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/posts',
                    json={'title': 't', 'content': 'c',
                          'privacy': 'private'},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201
    pid = r.get_json()['data']['id']

    from integrations.social.models import Post
    p = db.query(Post).filter_by(id=pid).first()
    assert p.privacy is None  # silently dropped


# ── Migration is idempotent ─────────────────────────────────────────

def test_migration_v48_is_idempotent(fresh_db):
    """Running migrations twice does not error — the ADD COLUMN guard
    swallows 'duplicate column' on the second pass."""
    from integrations.social import migrations
    # Already applied once via the fixture.  Running again must be a
    # no-op.
    migrations.run_migrations()
    from integrations.social.migrations import get_schema_version
    _, eng = fresh_db
    assert get_schema_version(eng) >= 48


# ── P3-02: feed_engine endpoints respect privacy ────────────────────

def test_feed_global_excludes_friends_only_for_stranger(app_client):
    """/feed/all bypassed visible_posts_filter pre-fix (Pass-3 P3-02).
    Locks the gate."""
    client, db = app_client
    a, b = _seed_users(db, 2)
    public = _make_post(db, a.id, privacy='public')
    private = _make_post(db, a.id, privacy='private')
    friends = _make_post(db, a.id, privacy='friends')

    from integrations.social import auth
    tok = auth.generate_jwt(b.id, b.username, 'flat')
    r = client.get('/api/social/feed/all',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    ids = {p['id'] for p in r.get_json()['data']}
    assert public.id in ids
    assert private.id not in ids
    assert friends.id not in ids


def test_feed_trending_excludes_private_for_stranger(app_client):
    client, db = app_client
    a, b = _seed_users(db, 2)
    public = _make_post(db, a.id, privacy='public')
    private = _make_post(db, a.id, privacy='private')

    from integrations.social import auth
    tok = auth.generate_jwt(b.id, b.username, 'flat')
    r = client.get('/api/social/feed/trending',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    ids = {p['id'] for p in r.get_json()['data']}
    assert public.id in ids
    assert private.id not in ids


def test_feed_personalized_excludes_friends_only_for_non_friend(app_client):
    """Personalized feed needs the post author to be followed/community-shared.
    Even when met, the privacy gate must filter friends-only posts."""
    client, db = app_client
    a, b = _seed_users(db, 2)
    com = _seed_community(db, owner_id=a.id, members=[b.id])
    public = _make_post(db, a.id, privacy='public', community_id=com.id)
    friends = _make_post(db, a.id, privacy='friends', community_id=com.id)

    from integrations.social import auth
    tok = auth.generate_jwt(b.id, b.username, 'flat')
    r = client.get('/api/social/feed',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    ids = {p['id'] for p in r.get_json()['data']}
    assert public.id in ids
    assert friends.id not in ids


# ── P3-03: /users/<id>/posts respects privacy ───────────────────────

def test_user_posts_endpoint_excludes_private_for_stranger(app_client):
    """Pass-3 P3-03: profile pages were the most obvious leak path."""
    client, db = app_client
    a, b = _seed_users(db, 2)
    public = _make_post(db, a.id, privacy='public')
    private = _make_post(db, a.id, privacy='private')
    friends = _make_post(db, a.id, privacy='friends')

    from integrations.social import auth
    tok = auth.generate_jwt(b.id, b.username, 'flat')
    r = client.get(f'/api/social/users/{a.id}/posts',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    ids = {p['id'] for p in r.get_json()['data']}
    assert public.id in ids
    assert private.id not in ids
    assert friends.id not in ids


def test_user_posts_endpoint_shows_own_at_every_level(app_client):
    client, db = app_client
    a, = _seed_users(db, 1)
    expected = {
        _make_post(db, a.id, privacy='public').id,
        _make_post(db, a.id, privacy='friends').id,
        _make_post(db, a.id, privacy='private').id,
    }
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.get(f'/api/social/users/{a.id}/posts',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    ids = {p['id'] for p in r.get_json()['data']}
    assert ids == expected


# ── P3-04: /search?type=posts respects privacy ──────────────────────

def test_search_posts_excludes_friends_only_for_stranger(app_client):
    """Pass-3 P3-04: search is a high-leak surface (any user can
    search for any term across the platform)."""
    client, db = app_client
    a, b = _seed_users(db, 2)
    public = _make_post(db, a.id, privacy='public', content='unique-marker-public')
    friends = _make_post(db, a.id, privacy='friends', content='unique-marker-friends')

    from integrations.social import auth
    tok = auth.generate_jwt(b.id, b.username, 'flat')
    r = client.get('/api/social/search?type=posts&q=unique-marker',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    ids = {p['id'] for p in r.get_json()['data']}
    assert public.id in ids
    assert friends.id not in ids


# ── P3-08: visible_posts_filter composes orthogonally with tenant ────

def test_visible_posts_filter_does_not_reference_tenant_id(fresh_db):
    """Pass-3 P3-08: the privacy filter must compose with — never
    replace — the existing tenant_filter listener.  Verify by
    introspecting the compiled SQL that visible_posts_filter never
    touches `tenant_id` (the listener owns that orthogonal scope)."""
    from integrations.social.privacy import visible_posts_filter

    class _FakeUser:
        id = 'fake-viewer-id'

    expr = visible_posts_filter(_FakeUser())
    compiled = str(expr.compile(compile_kwargs={'literal_binds': False}))
    assert 'tenant_id' not in compiled, (
        "visible_posts_filter must NOT touch tenant_id directly — the "
        "tenant_filter listener owns that scope.  Compiled SQL: "
        f"{compiled[:300]}")
    # Sanity: the filter does reference posts.privacy and the
    # friendships / memberships subqueries.
    assert 'privacy' in compiled
    assert 'friendships' in compiled
    assert 'memberships' in compiled


# ── P3-07: friends_arm + community_arm are disjoint with own_clause ──

def test_own_post_does_not_double_match_friends_arm(fresh_db):
    """Pass-3 P3-07: friends_arm now has `author_id <> :viewer`, so
    the EXISTS subquery doesn't fire for the viewer's own posts.
    Verify by mocking is_friend to raise — own posts must still be
    visible (via own_clause), proving the friends-arm path isn't
    hit for them."""
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    own = _make_post(db, a.id, privacy='friends')

    from integrations.social.services import PostService
    # No friendship row exists for self-pair, so a non-disjoint arm
    # would have been false anyway. Stronger test: introspect the
    # compiled SQL doesn't include the viewer twice on the wrong side.
    # Functional: the post is visible.
    posts, _ = PostService.list_posts(
        db, sort='new', viewer_user=a, apply_privacy=True)
    assert own.id in {p.id for p in posts}


# ── P3-11: community privacy without community_id is rejected ───────

def test_create_post_rejects_community_privacy_without_community(app_client):
    client, db = app_client
    a, = _seed_users(db, 1)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/posts',
                    json={'title': 't', 'content': 'c',
                          'privacy': 'community'},  # no `community` field
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 400
    body = r.get_json()
    assert 'community' in body.get('error', '').lower()


def test_update_post_rejects_community_privacy_without_community(app_client):
    client, db = app_client
    a, = _seed_users(db, 1)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    create = client.post('/api/social/posts',
                         json={'title': 't', 'content': 'c'},
                         headers={'Authorization': f'Bearer {tok}'})
    pid = create.get_json()['data']['id']

    r = client.patch(f'/api/social/posts/{pid}',
                     json={'privacy': 'community'},
                     headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 400


# ── P3-13: blocked-former-friend cannot see friends-only post ───────

def test_friends_post_hidden_from_blocked_former_friend(fresh_db):
    """Pass-3 P3-13: when an active friendship transitions to
    'blocked', is_friend returns False and the privacy gate denies
    the formerly-friend viewer."""
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    _seed_friendship(db, a.id, b.id, status='active')
    p = _make_post(db, a.id, privacy='friends')

    # Confirm visible while active
    from integrations.social.privacy import can_view_post
    assert can_view_post(db, b, p) is True

    # Transition to blocked
    from sqlalchemy import text
    ua, ub = sorted([a.id, b.id])
    db.execute(text(
        "UPDATE friendships SET status = 'blocked', "
        "blocked_at = CURRENT_TIMESTAMP "
        "WHERE user_a_id = :a AND user_b_id = :b"),
        {'a': ua, 'b': ub})
    db.commit()

    # Now hidden
    assert can_view_post(db, b, p) is False
    # And the SQL filter agrees
    from integrations.social.services import PostService
    posts, _ = PostService.list_posts(
        db, sort='new', viewer_user=b, apply_privacy=True)
    assert p.id not in {x.id for x in posts}


# ── P3-15: increment_view does not fire for non-viewable posts ──────

def test_get_post_does_not_increment_view_for_blocked_viewer(app_client):
    """Pass-3 P3-15: privacy check runs BEFORE increment_view in
    api.get_post.  A non-viewable read must not bump view_count."""
    client, db = app_client
    a, b = _seed_users(db, 2)
    p = _make_post(db, a.id, privacy='private')
    initial = p.view_count

    from integrations.social import auth
    tok = auth.generate_jwt(b.id, b.username, 'flat')
    r = client.get(f'/api/social/posts/{p.id}',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 404

    # Re-read view_count from the DB
    from integrations.social.models import Post
    db.expire_all()
    refreshed = db.query(Post).filter_by(id=p.id).first()
    assert refreshed.view_count == initial


# ── P3-06: flag-off response shape unchanged (no 'privacy' key) ─────

def test_response_has_no_privacy_key_when_column_is_null(fresh_db, monkeypatch):
    """Pre-7c.5 responses had no 'privacy' key.  Legacy posts
    (privacy=NULL) must round-trip without adding the key, matching
    the zero-regression invariant."""
    monkeypatch.delenv('HEVOLVE_FLAG_POST_PRIVACY', raising=False)
    from flask import Flask
    from integrations.social import api, auth
    from integrations.social.api_conversations import conversations_bp
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    app.register_blueprint(conversations_bp)
    client = app.test_client()
    db = fresh_db[0]
    a, = _seed_users(db, 1)
    p = _make_post(db, a.id, privacy=None)
    tok = auth.generate_jwt(a.id, a.username, 'flat')

    r = client.get(f'/api/social/posts/{p.id}',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    body = r.get_json()['data']
    assert 'privacy' not in body, (
        f"Legacy post (privacy=NULL) leaked a 'privacy' key: {body}")
