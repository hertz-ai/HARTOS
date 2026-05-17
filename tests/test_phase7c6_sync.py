"""Phase 7c.6 — /sync endpoint + cursor advancement tests.

Plan reference: sunny-gliding-eich.md, Part R.3 + Part W.1.b.

Locks the contract:

  Cursor:
    - Cold start (since=None / 0 / '') returns every row from epoch.
    - Subsequent call with returned cursor returns only newer rows.
    - cursor advances monotonically.
    - has_more=True when any kind hits limit.

  Per-kind:
    - conversations: only ones the user is a member of; advances on
      both created_at AND last_message_at changes.
    - messages: only in conversations the user is a member of; advances
      on created_at AND edited_at; includes soft-deletes.
    - friendships: advances on accept + block (status transitions).
    - invites: visible by invitee, inviter, OR matching invitee_email.
    - mentions: only when targeted at the user OR the user owns a
      mentioned agent.
    - memberships: only the user's own rows.

  Privacy:
    - non-members of a conversation never see its messages even when
      cursor would otherwise include them.
    - blocks of OTHER users are not exposed.

  Endpoint flag-gating:
    - flag off → 200 with empty cursor + empty deltas (graceful default).
    - flag on  → real deltas.

  Robustness:
    - malformed cursor falls back to epoch (never raises 500).
    - unknown kind in CSV is silently dropped.
    - one kind's fetcher exception doesn't break the others.
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
    monkeypatch.setenv('HEVOLVE_FLAG_SYNC_V1', 'true')
    monkeypatch.setenv('HEVOLVE_FLAG_CONVERSATIONS', 'true')
    monkeypatch.setenv('HEVOLVE_FLAG_FRIENDS_V2', 'true')
    monkeypatch.setenv('HEVOLVE_FLAG_INVITES_V2', 'true')
    monkeypatch.setenv('HEVOLVE_FLAG_MENTIONS', 'true')
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
    from integrations.social.models import User
    a = User(id=str(uuid.uuid4()), username='alice', display_name='Alice',
             email='alice@x.test', password_hash='x:y', user_type='human')
    b = User(id=str(uuid.uuid4()), username='bob', display_name='Bob',
             email='bob@x.test', password_hash='x:y', user_type='human')
    db.add_all([a, b])
    db.commit()
    return a, b


# ── Cursor + cold start ──────────────────────────────────────────

def test_cold_start_returns_every_row(fresh_db, monkeypatch):
    """since=None must back-fill from epoch — every row the user owns
    or can see appears in the first response."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(
        db, kind='dm', member_ids=[b.id], created_by=a.id)
    ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='hi')

    from integrations.social.sync_service import SyncService
    res = SyncService.deltas(db, user_id=a.id, since=None)

    assert res['cursor'] != '1970-01-01 00:00:00', \
        "cursor must advance when there are deltas"
    assert len(res['deltas']['conversations']) == 1
    assert len(res['deltas']['messages']) == 1
    assert res['deltas']['conversations'][0]['id'] == conv['id']
    assert res['deltas']['messages'][0]['content'] == 'hi'


def test_cursor_advances_monotonically(fresh_db, monkeypatch):
    """Second sync with the returned cursor returns only newer rows."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.conversation_service import ConversationService
    from integrations.social.sync_service import SyncService

    conv = ConversationService.create(
        db, kind='dm', member_ids=[b.id], created_by=a.id)
    ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='m1')
    res1 = SyncService.deltas(db, user_id=a.id, since=None)
    cursor1 = res1['cursor']

    # Second call with no new writes — empty deltas, cursor stable.
    res2 = SyncService.deltas(db, user_id=a.id, since=cursor1)
    assert res2['deltas']['messages'] == []
    assert res2['deltas']['conversations'] == []

    # New write — appears on the next call only.
    import time; time.sleep(1.05)  # SQLite created_at is second-resolution
    ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='m2')
    res3 = SyncService.deltas(db, user_id=a.id, since=cursor1)
    msgs = res3['deltas']['messages']
    assert len(msgs) == 1
    assert msgs[0]['content'] == 'm2'


def test_cursor_includes_edits(fresh_db, monkeypatch):
    """Editing a message should make it reappear in sync after the
    cursor that captured its original create."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.conversation_service import ConversationService
    from integrations.social.sync_service import SyncService
    conv = ConversationService.create(
        db, kind='dm', member_ids=[b.id], created_by=a.id)
    msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='original')
    res1 = SyncService.deltas(db, user_id=a.id, since=None)
    cursor1 = res1['cursor']

    import time; time.sleep(1.05)
    ConversationService.edit_message(
        db, message_id=msg['id'], requester_id=a.id,
        new_content='edited')
    res2 = SyncService.deltas(db, user_id=a.id, since=cursor1)
    msgs = res2['deltas']['messages']
    assert len(msgs) == 1
    assert msgs[0]['content'] == 'edited'
    assert msgs[0]['edited_at'] is not None


def test_cursor_includes_soft_deletes(fresh_db, monkeypatch):
    """Soft-deleted messages MUST appear in /sync with is_deleted=True so
    offline clients can mirror the delete.  C3 fix: soft_delete_message
    now bumps edited_at so the COALESCE(edited_at, created_at) cursor
    picks up the change."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.conversation_service import ConversationService
    from integrations.social.sync_service import SyncService
    conv = ConversationService.create(
        db, kind='dm', member_ids=[b.id], created_by=a.id)
    msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='oops')
    res1 = SyncService.deltas(db, user_id=a.id, since=None)
    cursor1 = res1['cursor']

    import time; time.sleep(1.05)
    ConversationService.soft_delete_message(
        db, message_id=msg['id'], requester_id=a.id)
    res2 = SyncService.deltas(db, user_id=a.id, since=cursor1)
    # Message row appears in messages delta with is_deleted=True.
    msgs = res2['deltas']['messages']
    target = [m for m in msgs if m['id'] == msg['id']]
    assert len(target) == 1, (
        "soft-deleted message must appear in /sync delta so offline "
        "clients can mirror the delete")
    assert target[0]['is_deleted'] is True
    assert target[0]['content'] == '[deleted]'


# ── Privacy: non-member doesn't see other people's messages ──────

def test_non_member_does_not_see_messages(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.models import User
    eve = User(id=str(uuid.uuid4()), username='eve', display_name='Eve',
               email='e@x.test', password_hash='x:y', user_type='human')
    db.add(eve); db.commit()
    from integrations.social.conversation_service import ConversationService
    from integrations.social.sync_service import SyncService

    conv = ConversationService.create(
        db, kind='dm', member_ids=[b.id], created_by=a.id)
    ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='private chat')

    # Eve syncs — sees nothing because she isn't in the conv.
    res = SyncService.deltas(db, user_id=eve.id, since=None)
    assert res['deltas']['conversations'] == []
    assert res['deltas']['messages'] == []


# ── Friendships / invites / mentions ─────────────────────────────

def test_friendship_appears_on_both_sides(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.friend_service import FriendService
    sent = FriendService.send_request(db, a.id, b.id)

    from integrations.social.sync_service import SyncService
    a_res = SyncService.deltas(db, user_id=a.id, since=None,
                                kinds=['friendships'])
    b_res = SyncService.deltas(db, user_id=b.id, since=None,
                                kinds=['friendships'])
    assert len(a_res['deltas']['friendships']) == 1
    assert len(b_res['deltas']['friendships']) == 1
    assert a_res['deltas']['friendships'][0]['id'] == sent['id']


def test_invite_visible_by_email(fresh_db, monkeypatch):
    """Off-platform email invite shows up in sync once the user signs
    up with the matching email — same property list_incoming has."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.models import Community
    c = Community(id=str(uuid.uuid4()), name='test-com',
                  display_name='Test', description='',
                  creator_id=a.id, is_private=True)
    db.add(c); db.commit()
    from integrations.social.invite_service import InviteService
    InviteService.send(db, parent_kind='community', parent_id=c.id,
                       invited_by=a.id, invitee_email='bob@x.test')
    # Bob's email matches — he sees the invite.
    from integrations.social.sync_service import SyncService
    res = SyncService.deltas(db, user_id=b.id, since=None,
                              kinds=['invites'])
    assert len(res['deltas']['invites']) == 1


def test_mention_visible_to_target_and_agent_owner(fresh_db, monkeypatch):
    """Mentioning an agent → both the agent AND its owner see the
    Mention row in their sync (matches the dual-notify contract)."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.models import User, Post, Community
    s = User(id=str(uuid.uuid4()), username='solar-architect',
             display_name='Solar', email='s@x.test',
             password_hash='x:y', user_type='agent')
    s.owner_id = a.id
    com = Community(id=str(uuid.uuid4()), name='test-com',
                    display_name='Test', description='',
                    creator_id=a.id, is_private=False)
    db.add_all([s, com]); db.commit()

    p = Post(id='p1', author_id=b.id, community_id=com.id,
             title='hi', content='@solar-architect please',
             content_type='text')
    db.add(p); db.commit()

    from integrations.social.mention_service import MentionService
    MentionService.parse_and_record(
        db, source_kind='post', source_id='p1',
        content='@solar-architect please', author_id=b.id,
        dispatch_agents=False)

    from integrations.social.sync_service import SyncService
    agent_res = SyncService.deltas(db, user_id=s.id, since=None,
                                    kinds=['mentions'])
    owner_res = SyncService.deltas(db, user_id=a.id, since=None,
                                    kinds=['mentions'])
    assert len(agent_res['deltas']['mentions']) == 1
    assert len(owner_res['deltas']['mentions']) == 1


# ── Robustness ───────────────────────────────────────────────────

def test_malformed_cursor_falls_back_to_epoch(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.conversation_service import ConversationService
    ConversationService.create(db, kind='dm', member_ids=[b.id],
                                created_by=a.id)
    from integrations.social.sync_service import SyncService
    # Garbage cursor — must not raise; falls back to epoch → returns rows.
    res = SyncService.deltas(db, user_id=a.id, since='garbage-cursor')
    assert len(res['deltas']['conversations']) == 1


def test_unknown_kind_silently_dropped(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, _ = _seed(db)
    from integrations.social.sync_service import SyncService
    res = SyncService.deltas(db, user_id=a.id, since=None,
                              kinds=['conversations', 'made-up-kind'])
    assert 'conversations' in res['deltas']
    assert 'made-up-kind' not in res['deltas']


def test_one_kinds_failure_doesnt_break_others(fresh_db, monkeypatch):
    """A buggy fetcher must not poison the whole response — every
    other kind still ships its delta. We patch one fetcher to raise."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.conversation_service import ConversationService
    ConversationService.create(db, kind='dm', member_ids=[b.id],
                                created_by=a.id)
    from integrations.social import sync_service
    monkeypatch.setattr(
        sync_service.SyncService, '_mentions_since',
        staticmethod(lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("synthetic"))))
    res = sync_service.SyncService.deltas(db, user_id=a.id, since=None)
    # mentions kind degrades to []; conversations still shipped.
    assert res['deltas']['mentions'] == []
    assert len(res['deltas']['conversations']) == 1


def test_has_more_when_limit_hit(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(
        db, kind='dm', member_ids=[b.id], created_by=a.id)
    for i in range(5):
        ConversationService.send_message(
            db, conv_id=conv['id'], author_id=a.id, content=f'm{i}')
    from integrations.social.sync_service import SyncService
    # Limit smaller than message count → has_more=True.
    res = SyncService.deltas(db, user_id=a.id, since=None,
                              limit_per_kind=3)
    assert res['has_more'] is True
    assert len(res['deltas']['messages']) == 3


# ── Endpoint flag-gating + round-trip ─────────────────────────────

def test_endpoint_flag_off_returns_empty(fresh_db, monkeypatch):
    """Flag off → graceful empty default, never 503 (sync is read-only
    and clients can poll harmlessly)."""
    db, _ = fresh_db
    a, _ = _seed(db)
    monkeypatch.delenv('HEVOLVE_FLAG_SYNC_V1', raising=False)
    from flask import Flask
    from integrations.social import api, auth
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    client = app.test_client()
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.get('/api/social/sync',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    data = r.get_json()['data']
    assert data['has_more'] is False
    assert data['deltas'] == {}


def test_endpoint_round_trip(app_client, monkeypatch):
    _silence_realtime(monkeypatch)
    client, db = app_client
    a, b = _seed(db)
    from integrations.social import auth
    tok_a = auth.generate_jwt(a.id, a.username, 'flat')

    # Alice creates a conv + message via the API.
    r = client.post('/api/social/conversations',
                    json={'kind': 'dm', 'member_ids': [b.id]},
                    headers={'Authorization': f'Bearer {tok_a}'})
    assert r.status_code == 201
    conv_id = r.get_json()['data']['id']
    client.post(f'/api/social/conversations/{conv_id}/messages',
                json={'content': 'hi via api'},
                headers={'Authorization': f'Bearer {tok_a}'})

    # Alice syncs.
    r = client.get('/api/social/sync',
                   headers={'Authorization': f'Bearer {tok_a}'})
    assert r.status_code == 200
    data = r.get_json()['data']
    assert any(c['id'] == conv_id for c in data['deltas']['conversations'])
    assert any(m['content'] == 'hi via api' for m in data['deltas']['messages'])
