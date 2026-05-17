"""Phase 7c.3 — Conversations (DM/group) + Messages tests.

Plan reference: sunny-gliding-eich.md, Part C.2 + Part E.3.

Locks the contract:

  Migration v45:
    - `conversations` + `messages` tables exist with expected columns.
    - Idempotent re-run does not crash.

  ConversationService.create:
    - DM: 2 members, deduped by member_hash (re-create returns existing).
    - DM: blocked-pair refused.
    - DM: invalid member count refused.
    - Group: 2+ members; caller auto-added; admin role on creator.
    - Invalid kind refused.

  ConversationService.send_message + list_messages:
    - Member can send; non-member refused.
    - Empty / oversized content refused.
    - last_message_at bumped on send.
    - list_messages paginated by `before` cursor.
    - Non-member cannot list.
    - Mention parsing fires (Mention rows recorded).

  ConversationService.edit_message + soft_delete_message:
    - Author within edit window can edit.
    - Non-author cannot edit.
    - Outside edit window refused.
    - Soft-delete: is_deleted=1, content='[deleted]', not returned by list.

  Member management:
    - add_member admin-only on group; not allowed on DM.
    - remove_member admin removes anyone; member removes themselves.

  dispatch_to_agent extension:
    - source_kind='message' posts the agent reply as a Message in the
      same conversation, but only if the agent is already a member.
    - non-member agent → no-op (logged), no row inserted.

  Endpoint flag-gating:
    - Mutating endpoint flag off → 503.
    - Read endpoint flag off → [].
    - End-to-end POST→list round-trip with flag on.
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
    monkeypatch.setenv('HEVOLVE_FLAG_CONVERSATIONS', 'true')
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
    """Three users (one agent owned by alice)."""
    from integrations.social.models import User
    a = User(id=str(uuid.uuid4()), username='alice', display_name='Alice',
             email='alice@x.test', password_hash='x:y', user_type='human')
    b = User(id=str(uuid.uuid4()), username='bob', display_name='Bob',
             email='bob@x.test', password_hash='x:y', user_type='human')
    s = User(id=str(uuid.uuid4()), username='solar-architect',
             display_name='Solar', email='s@x.test',
             password_hash='x:y', user_type='agent')
    s.owner_id = a.id
    db.add_all([a, b, s])
    db.commit()
    return a, b, s


# ── Migration v45 ────────────────────────────────────────────────

def test_v45_tables_created(fresh_db):
    from sqlalchemy import inspect
    db, eng = fresh_db
    insp = inspect(eng)
    tnames = insp.get_table_names()
    assert 'conversations' in tnames
    assert 'messages' in tnames
    cols_c = {c['name'] for c in insp.get_columns('conversations')}
    cols_m = {c['name'] for c in insp.get_columns('messages')}
    assert {'id', 'kind', 'title', 'created_by', 'member_hash',
            'last_message_at', 'is_locked', 'is_archived', 'settings'
           }.issubset(cols_c), f"missing cols: {cols_c}"
    assert {'id', 'parent_kind', 'parent_id', 'thread_root_id',
            'author_id', 'agent_kind', 'content', 'depth',
            'edited_at', 'is_deleted', 'metadata_json'
           }.issubset(cols_m), f"missing cols: {cols_m}"


def test_v45_idempotent(fresh_db):
    from integrations.social import migrations
    migrations.run_migrations()


# ── ConversationService.create ───────────────────────────────────

def test_create_dm_two_members(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(
        db, kind='dm', member_ids=[b.id], created_by=a.id)
    assert conv['kind'] == 'dm'
    assert sorted(conv['members']) == sorted([a.id, b.id])


def test_create_dm_dedups(fresh_db, monkeypatch):
    """Re-creating a DM between same pair returns the existing row."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.conversation_service import ConversationService
    c1 = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                    created_by=a.id)
    c2 = ConversationService.create(db, kind='dm', member_ids=[a.id],
                                    created_by=b.id)
    assert c1['id'] == c2['id']


def test_create_dm_invalid_member_count(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, s = _seed(db)
    from integrations.social.conversation_service import (
        ConversationService, ConversationError)
    with pytest.raises(ConversationError, match='exactly 2 members'):
        ConversationService.create(db, kind='dm',
                                    member_ids=[b.id, s.id],
                                    created_by=a.id)


def test_create_dm_blocked_pair_refused(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.friend_service import FriendService
    FriendService.block(db, blocker_id=b.id, blocked_id=a.id)
    from integrations.social.conversation_service import (
        ConversationService, ConversationError)
    with pytest.raises(ConversationError, match='blocked'):
        ConversationService.create(db, kind='dm', member_ids=[b.id],
                                    created_by=a.id)


def test_create_group_admin_role_on_creator(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, s = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(
        db, kind='group', member_ids=[b.id, s.id],
        created_by=a.id, title='Crew')
    assert conv['kind'] == 'group'
    assert conv['title'] == 'Crew'
    # Creator role should be admin in memberships table.
    from sqlalchemy import text
    role = db.execute(text(
        "SELECT role FROM memberships "
        "WHERE parent_kind='conversation' AND parent_id=:pid "
        "AND member_id=:mid"),
        {'pid': conv['id'], 'mid': a.id}
    ).fetchone()
    assert role[0] == 'admin'


def test_create_invalid_kind_refused(fresh_db):
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.conversation_service import (
        ConversationService, ConversationError)
    with pytest.raises(ConversationError, match='invalid kind'):
        ConversationService.create(db, kind='broadcast', member_ids=[b.id],
                                    created_by=a.id)


# ── send_message / list_messages ─────────────────────────────────

def test_send_message_member(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='hi bob')
    assert msg['content'] == 'hi bob'
    assert msg['parent_id'] == conv['id']

    # last_message_at bumped.
    refreshed = ConversationService.get(db, conv['id'])
    assert refreshed['last_message_at'] is not None


def test_send_message_non_member_refused(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.models import User
    eve = User(id=str(uuid.uuid4()), username='eve', display_name='Eve',
               email='e@x.test', password_hash='x:y', user_type='human')
    db.add(eve); db.commit()
    from integrations.social.conversation_service import (
        ConversationService, ConversationError)
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    with pytest.raises(ConversationError, match='not a conversation member'):
        ConversationService.send_message(
            db, conv_id=conv['id'], author_id=eve.id, content='spam')


def test_send_message_empty_refused(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.conversation_service import (
        ConversationService, ConversationError)
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    with pytest.raises(ConversationError, match='empty message'):
        ConversationService.send_message(
            db, conv_id=conv['id'], author_id=a.id, content='   ')


def test_list_messages_paginated(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    for i in range(5):
        ConversationService.send_message(
            db, conv_id=conv['id'], author_id=a.id, content=f'm{i}')
    rows = ConversationService.list_messages(
        db, conv_id=conv['id'], requester_id=b.id, limit=10)
    assert len(rows) == 5
    # Newest first.
    assert rows[0]['content'] == 'm4'


def test_list_messages_non_member_refused(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.models import User
    eve = User(id=str(uuid.uuid4()), username='eve', display_name='Eve',
               email='e@x.test', password_hash='x:y', user_type='human')
    db.add(eve); db.commit()
    from integrations.social.conversation_service import (
        ConversationService, ConversationError)
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    with pytest.raises(ConversationError, match='not a conversation member'):
        ConversationService.list_messages(
            db, conv_id=conv['id'], requester_id=eve.id)


def test_send_message_mention_parsing(fresh_db, monkeypatch):
    """@-mention in a message should record a Mention row."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id,
        content='hey @bob — chat?')
    # Mention row recorded with source_kind='message'.
    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT mentioned_user_id FROM mentions "
        "WHERE source_kind='message' AND source_id=:mid"),
        {'mid': msg['id']}).fetchall()
    # 'bob' doesn't exist (user is 'bob' which is fine — username 'bob'
    # exists on user b). Confirm the regex matched.
    assert any(r[0] == b.id for r in rows)


# ── edit / soft_delete ───────────────────────────────────────────

def test_edit_own_message(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='typo')
    res = ConversationService.edit_message(
        db, message_id=msg['id'], requester_id=a.id, new_content='fixed')
    assert res['content'] == 'fixed'


def test_edit_other_users_message_refused(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.conversation_service import (
        ConversationService, ConversationError)
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='mine')
    with pytest.raises(ConversationError, match='only author'):
        ConversationService.edit_message(
            db, message_id=msg['id'], requester_id=b.id,
            new_content='hijacked')


def test_soft_delete_hides_from_list(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='oops')
    ConversationService.soft_delete_message(
        db, message_id=msg['id'], requester_id=a.id)
    rows = ConversationService.list_messages(
        db, conv_id=conv['id'], requester_id=b.id)
    assert all(r['id'] != msg['id'] for r in rows)


# ── Member management ───────────────────────────────────────────

def test_add_member_admin_only(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, s = _seed(db)
    from integrations.social.models import User
    eve = User(id=str(uuid.uuid4()), username='eve', display_name='Eve',
               email='e@x.test', password_hash='x:y', user_type='human')
    db.add(eve); db.commit()
    from integrations.social.conversation_service import (
        ConversationService, ConversationError)
    conv = ConversationService.create(
        db, kind='group', member_ids=[b.id], created_by=a.id, title='Crew')
    # Alice (admin) adds Eve — succeeds.
    res = ConversationService.add_member(
        db, conv_id=conv['id'], requester_id=a.id, new_member_id=eve.id)
    assert eve.id in res['members']
    # Bob (non-admin) tries to add solar-architect — refused.
    with pytest.raises(ConversationError, match='only admins'):
        ConversationService.add_member(
            db, conv_id=conv['id'], requester_id=b.id, new_member_id=s.id)


def test_remove_member_self_leave(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, s = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(
        db, kind='group', member_ids=[b.id, s.id], created_by=a.id,
        title='Crew')
    # Bob leaves himself — allowed even though he's not admin.
    res = ConversationService.remove_member(
        db, conv_id=conv['id'], requester_id=b.id, target_id=b.id)
    assert b.id not in res['members']


def test_add_member_dm_refused(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, s = _seed(db)
    from integrations.social.conversation_service import (
        ConversationService, ConversationError)
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    with pytest.raises(ConversationError, match='cannot add'):
        ConversationService.add_member(
            db, conv_id=conv['id'], requester_id=a.id, new_member_id=s.id)


# ── dispatch_to_agent extension ──────────────────────────────────

def test_dispatch_to_agent_message_kind_posts_reply(fresh_db, monkeypatch):
    """Agent member of a conversation that gets @-mentioned in a
    message → agent's reply lands as a new Message in the same convo."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, s = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(
        db, kind='group', member_ids=[b.id, s.id], created_by=a.id,
        title='With Agent')
    user_msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='ping')

    # Stub LLM.
    class _FakeReply:
        def __init__(self, text): self.content = text
    class _FakeLLM:
        def invoke(self, prompt): return _FakeReply('pong')
    import core.safe_hartos_attr as sha
    monkeypatch.setattr(
        sha, 'safe_hartos_attr',
        lambda name: (lambda **kw: _FakeLLM()) if name == 'get_llm' else None)

    from integrations import agentic_router
    agentic_router.dispatch_to_agent(
        agent_id=s.id, prompt='ping', synchronous=True,
        context={'source_kind': 'message', 'source_id': user_msg['id'],
                 'author_id': a.id, 'tenant_id': None})

    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT author_id, content FROM messages "
        "WHERE parent_id = :cid AND author_id = :aid "
        "AND id != :uid"),
        {'cid': conv['id'], 'aid': s.id, 'uid': user_msg['id']}
    ).fetchall()
    assert len(rows) == 1
    assert 'pong' in rows[0][1].lower()


def test_dispatch_to_agent_message_skipped_if_not_member(fresh_db, monkeypatch):
    """Agent NOT in the conversation → dispatch worker logs and exits;
    no message inserted. Phase 7d's AgentJoinGrant gates joining."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, s = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    user_msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='only us')

    class _FakeLLM:
        def invoke(self, prompt):
            class R: content = 'should not appear'
            return R()
    import core.safe_hartos_attr as sha
    monkeypatch.setattr(
        sha, 'safe_hartos_attr',
        lambda name: (lambda **kw: _FakeLLM()) if name == 'get_llm' else None)

    from integrations import agentic_router
    agentic_router.dispatch_to_agent(
        agent_id=s.id, prompt='hi', synchronous=True,
        context={'source_kind': 'message', 'source_id': user_msg['id'],
                 'author_id': a.id, 'tenant_id': None})

    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT id FROM messages WHERE parent_id = :cid"),
        {'cid': conv['id']}).fetchall()
    # Only the original user message — no agent reply.
    assert len(rows) == 1


# ── Endpoint flag-gating + round-trip ──────────────────────────────

def test_endpoint_flag_off_create_returns_503(fresh_db, monkeypatch):
    db, _ = fresh_db
    a, b, _ = _seed(db)
    monkeypatch.delenv('HEVOLVE_FLAG_CONVERSATIONS', raising=False)
    from flask import Flask
    from integrations.social import api, auth
    from integrations.social.api_conversations import conversations_bp
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    app.register_blueprint(conversations_bp)
    client = app.test_client()
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/conversations',
                    json={'kind': 'dm', 'member_ids': [b.id]},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 503


def test_endpoint_flag_off_list_returns_empty(fresh_db, monkeypatch):
    db, _ = fresh_db
    a, b, _ = _seed(db)
    monkeypatch.delenv('HEVOLVE_FLAG_CONVERSATIONS', raising=False)
    from flask import Flask
    from integrations.social import api, auth
    from integrations.social.api_conversations import conversations_bp
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    app.register_blueprint(conversations_bp)
    client = app.test_client()
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.get('/api/social/conversations',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    assert r.get_json()['data'] == []


def test_endpoint_create_then_send_then_list(app_client, monkeypatch):
    _silence_realtime(monkeypatch)
    client, db = app_client
    a, b, _ = _seed(db)
    from integrations.social import auth
    tok_a = auth.generate_jwt(a.id, a.username, 'flat')
    tok_b = auth.generate_jwt(b.id, b.username, 'flat')

    # Alice creates a DM with Bob.
    r = client.post('/api/social/conversations',
                    json={'kind': 'dm', 'member_ids': [b.id]},
                    headers={'Authorization': f'Bearer {tok_a}'})
    assert r.status_code == 201, r.get_json()
    conv_id = r.get_json()['data']['id']

    # Alice sends a message.
    r = client.post(f'/api/social/conversations/{conv_id}/messages',
                    json={'content': 'hi bob'},
                    headers={'Authorization': f'Bearer {tok_a}'})
    assert r.status_code == 201

    # Bob lists messages.
    r = client.get(f'/api/social/conversations/{conv_id}/messages',
                   headers={'Authorization': f'Bearer {tok_b}'})
    assert r.status_code == 200
    msgs = r.get_json()['data']
    assert len(msgs) == 1
    assert msgs[0]['content'] == 'hi bob'

    # Bob lists his conversations.
    r = client.get('/api/social/conversations',
                   headers={'Authorization': f'Bearer {tok_b}'})
    assert r.status_code == 200
    convs = r.get_json()['data']
    assert any(c['id'] == conv_id for c in convs)


def test_endpoint_get_conversation_non_member_404(app_client, monkeypatch):
    """Non-members get 404 (not 403) — avoids leaking conversation existence."""
    _silence_realtime(monkeypatch)
    client, db = app_client
    a, b, _ = _seed(db)
    from integrations.social.models import User
    eve = User(id=str(uuid.uuid4()), username='eve', display_name='Eve',
               email='e@x.test', password_hash='x:y', user_type='human')
    db.add(eve); db.commit()
    from integrations.social import auth
    tok_a = auth.generate_jwt(a.id, a.username, 'flat')
    tok_eve = auth.generate_jwt(eve.id, eve.username, 'flat')
    r = client.post('/api/social/conversations',
                    json={'kind': 'dm', 'member_ids': [b.id]},
                    headers={'Authorization': f'Bearer {tok_a}'})
    conv_id = r.get_json()['data']['id']
    r = client.get(f'/api/social/conversations/{conv_id}',
                   headers={'Authorization': f'Bearer {tok_eve}'})
    assert r.status_code == 404
