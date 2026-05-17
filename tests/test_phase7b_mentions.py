"""Phase 7b — @-mentions + agent-as-member integration tests.

Plan reference: sunny-gliding-eich.md, Part E.5 + Part L.

Covers:
  1. MentionService.parse — regex correctness on common edge cases.
  2. MentionService.parse_and_record — Mention rows + Notifications.
  3. Agent mentions dual-notify (agent + owner).
  4. Mention diffing on edit (insert new, delete removed).
  5. Flag-off path: existing post/comment endpoints return identical
     payload — no `mentions` key present.
  6. Flag-on path: post + comment + reply create endpoints attach
     `mentions[]` array to response when @-mentions are present.
  7. Regression: existing post/comment shapes unchanged when no
     @-mentions are in the content.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def fresh_db(monkeypatch):
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
    monkeypatch.setenv('HEVOLVE_FLAG_MENTIONS', 'true')
    from flask import Flask
    from integrations.social import api
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    yield app.test_client(), fresh_db[0]


def _seed(db):
    """Three users: alice (human), solar-architect (agent owned by
    alice), bob_p (human). Plus a community."""
    from integrations.social.models import User, Community
    a = User(id=str(uuid.uuid4()), username='alice', display_name='Alice',
             email='alice@x.test', password_hash='x:y', user_type='human')
    s = User(id=str(uuid.uuid4()), username='solar-architect',
             display_name='Solar Architect', email='sa@x.test',
             password_hash='x:y', user_type='agent')
    s.owner_id = a.id
    b = User(id=str(uuid.uuid4()), username='bob_p', display_name='Bob',
             email='b@x.test', password_hash='x:y', user_type='human')
    db.add_all([a, s, b])
    c = Community(id=str(uuid.uuid4()), name='test-com',
                  display_name='Test', description='desc',
                  creator_id=a.id, is_private=False)
    db.add(c)
    db.commit()
    return a, s, b, c


# ── MentionService.parse ──────────────────────────────────────────

def test_parse_extracts_basic_mentions():
    from integrations.social.mention_service import MentionService
    out = MentionService.parse("hi @alice and @bob_p")
    assert out == ['alice', 'bob_p']


def test_parse_dedupes():
    from integrations.social.mention_service import MentionService
    out = MentionService.parse("@alice and @alice and @ALICE")
    # All three are the same lowercase username
    assert out == ['alice']


def test_parse_ignores_email_at():
    from integrations.social.mention_service import MentionService
    # Email patterns shouldn't be picked up — preceded by a word char
    out = MentionService.parse("contact me at user@example.com")
    assert out == []


def test_parse_handles_punctuation():
    from integrations.social.mention_service import MentionService
    out = MentionService.parse("hey @alice, thanks!")
    assert out == ['alice']


def test_parse_handles_dot_dash_underscore():
    from integrations.social.mention_service import MentionService
    out = MentionService.parse("@solar-architect @v_1 @user.name")
    assert out == ['solar-architect', 'v_1', 'user.name']


def test_parse_empty_input():
    from integrations.social.mention_service import MentionService
    assert MentionService.parse('') == []
    assert MentionService.parse(None) == []


# ── MentionService.parse_and_record ───────────────────────────────

def test_record_inserts_mention_row(fresh_db):
    db, _ = fresh_db
    a, s, b, _ = _seed(db)
    from integrations.social.mention_service import MentionService
    from sqlalchemy import text
    refs = MentionService.parse_and_record(
        db, source_kind='post', source_id='p1',
        content="hello @bob_p", author_id=a.id)
    assert len(refs) == 1
    assert refs[0]['username'] == 'bob_p'
    assert refs[0]['kind'] == 'human'
    rows = db.execute(text(
        "SELECT mentioned_user_id FROM mentions WHERE source_id='p1'"
    )).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == b.id


def test_record_agent_mention_dual_notify(fresh_db):
    """Mentioning an agent fires TWO notifications: agent + owner."""
    db, _ = fresh_db
    a, s, b, _ = _seed(db)
    from integrations.social.mention_service import MentionService
    from sqlalchemy import text
    refs = MentionService.parse_and_record(
        db, source_kind='post', source_id='p2',
        content="@solar-architect please help", author_id=b.id,
        dispatch_agents=False)  # no need to actually run agentic_router
    assert len(refs) == 1
    assert refs[0]['kind'] == 'agent'
    # Two notification rows — for agent (s) and owner (a).
    rows = db.execute(text(
        "SELECT user_id, type FROM notifications "
        "WHERE source_user_id = :uid AND target_id = 'p2'"),
        {'uid': b.id}
    ).fetchall()
    user_ids = {r[0] for r in rows}
    assert s.id in user_ids, "agent must be notified"
    assert a.id in user_ids, "agent owner must be notified"


def test_record_diffs_on_edit(fresh_db):
    """parse_and_record on a re-edited source removes stale mentions."""
    db, _ = fresh_db
    a, s, b, _ = _seed(db)
    from integrations.social.mention_service import MentionService
    from sqlalchemy import text
    # First content mentions bob
    MentionService.parse_and_record(
        db, source_kind='post', source_id='p3',
        content="@bob_p hi", author_id=a.id, dispatch_agents=False)
    # Edit removes bob, adds solar-architect
    MentionService.parse_and_record(
        db, source_kind='post', source_id='p3',
        content="@solar-architect take over", author_id=a.id,
        dispatch_agents=False)
    rows = db.execute(text(
        "SELECT mentioned_user_id FROM mentions WHERE source_id='p3'"
    )).fetchall()
    user_ids = {r[0] for r in rows}
    assert s.id in user_ids
    assert b.id not in user_ids


def test_record_unknown_username_silently_ignored(fresh_db):
    db, _ = fresh_db
    a, _, _, _ = _seed(db)
    from integrations.social.mention_service import MentionService
    from sqlalchemy import text
    refs = MentionService.parse_and_record(
        db, source_kind='post', source_id='p4',
        content="@nobody_at_all here", author_id=a.id,
        dispatch_agents=False)
    assert refs == []
    rows = db.execute(text(
        "SELECT id FROM mentions WHERE source_id='p4'"
    )).fetchall()
    assert rows == []


# ── Endpoint behavior — flag-on ────────────────────────────────────

def test_create_post_with_mention_attaches_mentions_field(app_client):
    client, db = app_client
    a, s, b, c = _seed(db)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/posts',
        json={'title': 'Hey @bob_p',
              'content': 'come check this out',
              'community': 'test-com'},
        headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert 'mentions' in body['data']
    assert body['data']['mentions'][0]['username'] == 'bob_p'


def test_create_comment_with_mention_attaches_mentions_field(app_client):
    client, db = app_client
    a, s, b, c = _seed(db)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    # Make a post first
    r = client.post('/api/social/posts',
        json={'title': 'parent post', 'community': 'test-com'},
        headers={'Authorization': f'Bearer {tok}'})
    pid = r.get_json()['data']['id']
    r = client.post(f'/api/social/posts/{pid}/comments',
        json={'content': 'cc @solar-architect please'},
        headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201
    body = r.get_json()
    assert 'mentions' in body['data']
    assert body['data']['mentions'][0]['kind'] == 'agent'


# ── Endpoint behavior — flag-off (regression) ─────────────────────

def test_create_post_no_mentions_when_flag_off(app_client, monkeypatch):
    client, db = app_client
    a, s, b, c = _seed(db)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    # Disable the flag
    monkeypatch.delenv('HEVOLVE_FLAG_MENTIONS', raising=False)
    r = client.post('/api/social/posts',
        json={'title': 'Hey @bob_p',
              'content': 'come check this out',
              'community': 'test-com'},
        headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201
    body = r.get_json()
    # No mentions key present at all when flag is off
    assert 'mentions' not in body['data']


def test_create_post_no_at_mentions_returns_no_mentions_key(app_client):
    """Even with the flag on, a post without @-mentions doesn't
    add the mentions key — preserves identical shape to pre-7b."""
    client, db = app_client
    a, _, _, _ = _seed(db)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/posts',
        json={'title': 'plain post', 'content': 'no tags here',
              'community': 'test-com'},
        headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201
    body = r.get_json()
    assert 'mentions' not in body['data']


# ── Migration v42 ──────────────────────────────────────────────────

def test_v42_mentions_table_created(fresh_db):
    from sqlalchemy import inspect
    db, eng = fresh_db
    insp = inspect(eng)
    assert 'mentions' in insp.get_table_names()
    cols = {c['name'] for c in insp.get_columns('mentions')}
    expected = {'id', 'tenant_id', 'source_kind', 'source_id',
                'mentioned_user_id', 'mentioned_kind',
                'agent_owner_id', 'created_at', 'notified_at'}
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_v42_idempotent(fresh_db):
    from integrations.social import migrations
    # Re-run migrations — must not fail
    migrations.run_migrations()


# ── dispatch_to_agent — direct-named agent dispatch ───────────────

def test_dispatch_to_agent_exists():
    """Phase 7b C2 — agentic_router exposes dispatch_to_agent for
    MentionService._dispatch_agent. Without it, every agent mention
    silently no-ops via the hasattr fallback."""
    from integrations import agentic_router
    assert hasattr(agentic_router, 'dispatch_to_agent')
    assert callable(agentic_router.dispatch_to_agent)


def test_dispatch_to_agent_no_op_on_empty_args():
    """Empty agent_id or empty prompt is a silent no-op — never raises."""
    from integrations import agentic_router
    # synchronous=True so we observe completion
    agentic_router.dispatch_to_agent('', 'hello', {}, synchronous=True)
    agentic_router.dispatch_to_agent('aid', '', {}, synchronous=True)
    agentic_router.dispatch_to_agent(None, None, None, synchronous=True)


def _silence_realtime(monkeypatch):
    """Block every outbound publish path so tests don't hit real HTTP.

    NotificationService.create registers an after_commit hook that
    calls realtime.on_notification → crossbarhttp HTTP publish.  In a
    test env this can hang on socket timeout for 8s+ per publish.
    Patching at the realtime module level catches every caller.
    """
    from integrations.social import realtime
    monkeypatch.setattr(realtime, 'on_notification', lambda *a, **kw: None)
    monkeypatch.setattr(realtime, 'publish_event', lambda *a, **kw: None)
    monkeypatch.setattr(realtime, '_get_publisher', lambda: None)


def test_dispatch_to_agent_degrades_when_llm_unavailable(fresh_db, monkeypatch):
    """In the test env get_llm is not resolvable. The worker must
    return cleanly without raising and without inserting any Comment."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, s, b, c = _seed(db)
    # Make a post the agent would reply under.
    from integrations.social.models import Post
    post = Post(id='p_dispatch_1', author_id=a.id, community_id=c.id,
                title='hello', content='@solar-architect',
                content_type='text')
    db.add(post)
    db.commit()

    # Force get_llm to return None (test env: this is already the
    # case, but be explicit so the test is robust to env changes).
    import core.safe_hartos_attr as sha
    monkeypatch.setattr(sha, 'safe_hartos_attr', lambda name: None)

    from integrations import agentic_router
    agentic_router.dispatch_to_agent(
        agent_id=s.id, prompt='hi', synchronous=True,
        context={'source_kind': 'post', 'source_id': post.id,
                 'author_id': a.id, 'tenant_id': None})

    # No Comment created — LLM unreachable, worker exits cleanly.
    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT id FROM comments WHERE post_id = :pid"),
        {'pid': post.id}).fetchall()
    assert rows == []


def test_dispatch_to_agent_posts_comment_when_llm_replies(fresh_db, monkeypatch):
    """Happy path: guardrails pass, LLM returns text, reply lands as
    a Comment authored by the agent — same shape any human reply has."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, s, b, c = _seed(db)
    from integrations.social.models import Post
    post = Post(id='p_dispatch_2', author_id=a.id, community_id=c.id,
                title='ask', content='@solar-architect what is 2+2?',
                content_type='text')
    db.add(post)
    db.commit()

    # Stub get_llm with a fake LLM that returns a fixed answer.
    class _FakeReply:
        def __init__(self, text): self.content = text
    class _FakeLLM:
        def invoke(self, prompt): return _FakeReply('The answer is four.')
    import core.safe_hartos_attr as sha
    monkeypatch.setattr(
        sha, 'safe_hartos_attr',
        lambda name: (lambda **kw: _FakeLLM()) if name == 'get_llm' else None)

    from integrations import agentic_router
    agentic_router.dispatch_to_agent(
        agent_id=s.id, prompt='what is 2+2?', synchronous=True,
        context={'source_kind': 'post', 'source_id': post.id,
                 'author_id': a.id, 'tenant_id': None})

    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT author_id, content FROM comments WHERE post_id = :pid"),
        {'pid': post.id}).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == s.id  # authored by the agent
    assert 'four' in rows[0][1].lower()
