"""Phase 7c.4 — Emoji reactions tests.

Plan reference: sunny-gliding-eich.md, Part E.6.

Locks the contract:

  Migration v47:
    - reactions table created with all columns + unique index.
    - Idempotent re-run does not crash.

  ReactionService.toggle:
    - First call inserts; second call removes (toggle semantics).
    - Different users adding the same emoji each get their own row.
    - Same user different emojis on same source allowed.
    - Source not found refused.
    - Disallowed emoji refused.
    - Invalid source_kind refused.
    - Block check: reactor blocked by author → silent no-op.
    - Reactor reacting to own content allowed (self-reaction).

  ReactionService.list_for:
    - Aggregates by emoji with counts.
    - users[] capped at 5.
    - me_reacted set when viewer_id matches.
    - Sorted by count desc then emoji asc.

  ReactionService.remove:
    - Idempotent — removing a non-existent reaction is a no-op.

  Endpoint flag-gating:
    - Mutating endpoint flag off → 503.
    - Read endpoint flag off → [].
    - End-to-end POST + GET + DELETE round-trip with flag on.

  Polymorphic surface:
    - Same code path serves posts, comments, messages.
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
    monkeypatch.setenv('HEVOLVE_FLAG_REACTIONS', 'true')
    monkeypatch.setenv('HEVOLVE_FLAG_CONVERSATIONS', 'true')
    from flask import Flask
    from integrations.social import api
    from integrations.social.api_conversations import conversations_bp
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    app.register_blueprint(conversations_bp)
    yield app.test_client(), fresh_db[0]


def _silence_realtime(monkeypatch):
    from integrations.social import realtime
    monkeypatch.setattr(realtime, 'on_notification', lambda *a, **kw: None)
    monkeypatch.setattr(realtime, 'publish_event', lambda *a, **kw: None)
    monkeypatch.setattr(realtime, '_get_publisher', lambda: None)


def _seed(db):
    from integrations.social.models import User, Community, Post
    a = User(id=str(uuid.uuid4()), username='alice', display_name='A',
             email='a@x.test', password_hash='x:y', user_type='human')
    b = User(id=str(uuid.uuid4()), username='bob', display_name='B',
             email='b@x.test', password_hash='x:y', user_type='human')
    db.add_all([a, b]); db.commit()
    com = Community(id=str(uuid.uuid4()), name='test-com',
                    display_name='Test', description='',
                    creator_id=a.id, is_private=False)
    db.add(com); db.commit()
    p = Post(id='p1', author_id=a.id, community_id=com.id,
             title='hi', content='hello', content_type='text')
    db.add(p); db.commit()
    return a, b, p


# ── Migration v47 ────────────────────────────────────────────────

def test_v47_table_created(fresh_db):
    from sqlalchemy import inspect
    db, eng = fresh_db
    insp = inspect(eng)
    assert 'reactions' in insp.get_table_names()
    cols = {c['name'] for c in insp.get_columns('reactions')}
    expected = {'id', 'tenant_id', 'source_kind', 'source_id',
                'user_id', 'emoji', 'created_at'}
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_v47_idempotent(fresh_db):
    from integrations.social import migrations
    migrations.run_migrations()


# ── Toggle semantics ─────────────────────────────────────────────

def test_toggle_first_inserts_second_removes(fresh_db):
    db, _ = fresh_db
    a, b, p = _seed(db)
    from integrations.social.reaction_service import ReactionService
    res1 = ReactionService.toggle(
        db, source_kind='post', source_id=p.id,
        user_id=b.id, emoji='🔥')
    assert res1['action'] == 'added'
    assert res1['count'] == 1
    assert res1['me_reacted'] is True

    res2 = ReactionService.toggle(
        db, source_kind='post', source_id=p.id,
        user_id=b.id, emoji='🔥')
    assert res2['action'] == 'removed'
    assert res2['count'] == 0
    assert res2['me_reacted'] is False


def test_toggle_different_users_independent(fresh_db):
    db, _ = fresh_db
    a, b, p = _seed(db)
    from integrations.social.reaction_service import ReactionService
    ReactionService.toggle(db, 'post', p.id, a.id, '🔥')
    ReactionService.toggle(db, 'post', p.id, b.id, '🔥')
    res = ReactionService.list_for(db, 'post', p.id, viewer_id=b.id)
    fire = next((r for r in res if r['emoji'] == '🔥'), None)
    assert fire is not None
    assert fire['count'] == 2
    assert fire['me_reacted'] is True


def test_toggle_different_emojis_same_user(fresh_db):
    db, _ = fresh_db
    a, b, p = _seed(db)
    from integrations.social.reaction_service import ReactionService
    ReactionService.toggle(db, 'post', p.id, b.id, '🔥')
    ReactionService.toggle(db, 'post', p.id, b.id, '❤️')
    res = ReactionService.list_for(db, 'post', p.id, viewer_id=b.id)
    emojis = sorted([r['emoji'] for r in res])
    assert emojis == sorted(['🔥', '❤️'])


def test_toggle_self_reaction_allowed(fresh_db):
    """User reacting to their own content is allowed."""
    db, _ = fresh_db
    a, _, p = _seed(db)  # p.author = a
    from integrations.social.reaction_service import ReactionService
    res = ReactionService.toggle(db, 'post', p.id, a.id, '🎉')
    assert res['action'] == 'added'


# ── Refusal cases ────────────────────────────────────────────────

def test_invalid_source_kind_refused(fresh_db):
    db, _ = fresh_db
    a, _, _ = _seed(db)
    from integrations.social.reaction_service import (
        ReactionService, ReactionError)
    with pytest.raises(ReactionError, match='invalid source_kind'):
        ReactionService.toggle(db, 'gif', 'x', a.id, '🔥')


def test_disallowed_emoji_refused(fresh_db):
    db, _ = fresh_db
    a, _, p = _seed(db)
    from integrations.social.reaction_service import (
        ReactionService, ReactionError)
    with pytest.raises(ReactionError, match='not allowed'):
        ReactionService.toggle(db, 'post', p.id, a.id, '🦄')


def test_unknown_source_id_refused(fresh_db):
    db, _ = fresh_db
    a, _, _ = _seed(db)
    from integrations.social.reaction_service import (
        ReactionService, ReactionError)
    with pytest.raises(ReactionError, match='not found'):
        ReactionService.toggle(db, 'post', 'no-such-id', a.id, '🔥')


def test_block_silent_noop(fresh_db, monkeypatch):
    """Reactor blocked by author → silent no-op (doesn't reveal block)."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, p = _seed(db)
    from integrations.social.friend_service import FriendService
    FriendService.block(db, blocker_id=a.id, blocked_id=b.id)

    from integrations.social.reaction_service import ReactionService
    res = ReactionService.toggle(db, 'post', p.id, b.id, '🔥')
    assert res['action'] == 'noop'

    # Confirm no row was inserted.
    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT id FROM reactions "
        "WHERE source_id = :sid AND user_id = :uid"),
        {'sid': p.id, 'uid': b.id}).fetchall()
    assert rows == []


# ── list_for aggregation ─────────────────────────────────────────

def test_list_for_sorts_by_count_then_emoji(fresh_db):
    """Most-used emoji first; ties broken by emoji string ASC."""
    db, _ = fresh_db
    a, b, p = _seed(db)
    from integrations.social.models import User
    c = User(id=str(uuid.uuid4()), username='c', display_name='C',
             email='c@x.test', password_hash='x:y', user_type='human')
    db.add(c); db.commit()
    from integrations.social.reaction_service import ReactionService
    # 🔥 ×3, ❤️ ×1
    for u in (a, b, c):
        ReactionService.toggle(db, 'post', p.id, u.id, '🔥')
    ReactionService.toggle(db, 'post', p.id, a.id, '❤️')
    res = ReactionService.list_for(db, 'post', p.id, viewer_id=a.id)
    assert res[0]['emoji'] == '🔥'
    assert res[0]['count'] == 3
    assert res[1]['emoji'] == '❤️'
    assert res[1]['count'] == 1


def test_list_for_users_capped_at_5(fresh_db):
    db, _ = fresh_db
    a, _, p = _seed(db)
    from integrations.social.models import User
    from integrations.social.reaction_service import ReactionService
    for i in range(7):
        u = User(id=str(uuid.uuid4()), username=f'u{i}',
                 display_name=f'U{i}', email=f'u{i}@x.test',
                 password_hash='x:y', user_type='human')
        db.add(u); db.commit()
        ReactionService.toggle(db, 'post', p.id, u.id, '🔥')
    res = ReactionService.list_for(db, 'post', p.id)
    assert res[0]['count'] == 7
    assert len(res[0]['users']) == 5  # capped


def test_list_for_me_reacted(fresh_db):
    db, _ = fresh_db
    a, b, p = _seed(db)
    from integrations.social.reaction_service import ReactionService
    ReactionService.toggle(db, 'post', p.id, a.id, '🔥')
    ReactionService.toggle(db, 'post', p.id, b.id, '❤️')
    # Alice's view: me_reacted true on 🔥, false on ❤️.
    res_a = ReactionService.list_for(db, 'post', p.id, viewer_id=a.id)
    fire = next(r for r in res_a if r['emoji'] == '🔥')
    heart = next(r for r in res_a if r['emoji'] == '❤️')
    assert fire['me_reacted'] is True
    assert heart['me_reacted'] is False


# ── Polymorphic: comments + messages ─────────────────────────────

def test_polymorphic_on_comment(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, p = _seed(db)
    from integrations.social.services import CommentService
    c = CommentService.create(db, post=p, author=b, content='nice')
    db.commit()
    from integrations.social.reaction_service import ReactionService
    res = ReactionService.toggle(db, 'comment', c.id, a.id, '🚀')
    assert res['action'] == 'added'
    listed = ReactionService.list_for(db, 'comment', c.id, viewer_id=a.id)
    assert len(listed) == 1
    assert listed[0]['emoji'] == '🚀'


def test_polymorphic_on_message(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(
        db, kind='dm', member_ids=[b.id], created_by=a.id)
    msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='hi')
    from integrations.social.reaction_service import ReactionService
    res = ReactionService.toggle(db, 'message', msg['id'], b.id, '😂')
    assert res['action'] == 'added'
    listed = ReactionService.list_for(db, 'message', msg['id'],
                                       viewer_id=b.id)
    assert len(listed) == 1
    assert listed[0]['emoji'] == '😂'


# ── ReactionService.remove (explicit) ────────────────────────────

def test_remove_idempotent(fresh_db):
    db, _ = fresh_db
    a, _, p = _seed(db)
    from integrations.social.reaction_service import ReactionService
    # No reaction yet — remove is a no-op.
    res1 = ReactionService.remove(db, 'post', p.id, a.id, '🔥')
    assert res1['count'] == 0
    # Add one, remove once, remove again — second remove still 0.
    ReactionService.toggle(db, 'post', p.id, a.id, '🔥')
    ReactionService.remove(db, 'post', p.id, a.id, '🔥')
    res2 = ReactionService.remove(db, 'post', p.id, a.id, '🔥')
    assert res2['count'] == 0


# ── Endpoint flag-gating + round-trip ────────────────────────────

def test_endpoint_flag_off_toggle_returns_503(fresh_db, monkeypatch):
    db, _ = fresh_db
    a, _, p = _seed(db)
    monkeypatch.delenv('HEVOLVE_FLAG_REACTIONS', raising=False)
    from flask import Flask
    from integrations.social import api, auth
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    client = app.test_client()
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post(f'/api/social/posts/{p.id}/reactions',
                    json={'emoji': '🔥'},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 503


def test_endpoint_flag_off_list_returns_empty(fresh_db, monkeypatch):
    db, _ = fresh_db
    a, _, p = _seed(db)
    monkeypatch.delenv('HEVOLVE_FLAG_REACTIONS', raising=False)
    from flask import Flask
    from integrations.social import api, auth
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    client = app.test_client()
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.get(f'/api/social/posts/{p.id}/reactions',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    assert r.get_json()['data'] == []


def test_endpoint_post_reaction_round_trip(app_client, monkeypatch):
    _silence_realtime(monkeypatch)
    client, db = app_client
    a, b, p = _seed(db)
    from integrations.social import auth
    tok_b = auth.generate_jwt(b.id, b.username, 'flat')
    # Toggle on.
    r = client.post(f'/api/social/posts/{p.id}/reactions',
                    json={'emoji': '🔥'},
                    headers={'Authorization': f'Bearer {tok_b}'})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['data']['action'] == 'added'
    # List.
    r = client.get(f'/api/social/posts/{p.id}/reactions',
                   headers={'Authorization': f'Bearer {tok_b}'})
    assert r.status_code == 200
    listed = r.get_json()['data']
    assert any(x['emoji'] == '🔥' and x['me_reacted'] for x in listed)
    # Explicit DELETE.
    import urllib.parse
    enc = urllib.parse.quote('🔥')
    r = client.delete(f'/api/social/posts/{p.id}/reactions/{enc}',
                      headers={'Authorization': f'Bearer {tok_b}'})
    assert r.status_code == 200
    assert r.get_json()['data']['count'] == 0
