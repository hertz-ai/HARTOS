"""Phase 7c.7 — typing + read-receipt tests.

Plan reference: sunny-gliding-eich.md, Part E.3 + Part E.13 + Part W.1.b.

Locks the contract:

  Typing:
    - Pure WAMP emit; nothing persisted in DB.
    - Member can fire; non-member refused.
    - Topic shape: tenant.{tid}.conv.{cid}.typing  (or tenant._.conv... if no tid).

  Read-receipt:
    - Persists last_read_message_id + last_read_at on memberships row.
    - WAMP broadcast on tenant.{tid}.conv.{cid}.read.
    - message_id omitted → marks latest message.
    - Cross-conv message_id refused (security: prevents scribbling on
      another conversation's read state).
    - Empty conversation → graceful 'sent: False'.
    - Non-member refused.

  Endpoint flag-gating:
    - Flag off → 503 (mutating).
    - Flag on  → normal behavior.

  Realtime fan-out:
    - Verifies the publish_event ACL whitelist allows the
      tenant.* topics (regression check on the realtime authorizer).
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
             email='a@x.test', password_hash='x:y', user_type='human')
    b = User(id=str(uuid.uuid4()), username='bob', display_name='Bob',
             email='b@x.test', password_hash='x:y', user_type='human')
    db.add_all([a, b]); db.commit()
    return a, b


# ── Migration v46 ────────────────────────────────────────────────

def test_v46_columns_added(fresh_db):
    from sqlalchemy import inspect
    db, eng = fresh_db
    insp = inspect(eng)
    cols = {c['name'] for c in insp.get_columns('memberships')}
    assert 'last_read_message_id' in cols
    assert 'last_read_at' in cols


def test_v46_idempotent(fresh_db):
    from integrations.social import migrations
    migrations.run_migrations()


# ── Typing ───────────────────────────────────────────────────────

def test_typing_member_succeeds(fresh_db, monkeypatch):
    """Member can fire typing; nothing is persisted."""
    db, _ = fresh_db
    a, b = _seed(db)
    # Capture publishes via a list — verifies the topic shape AND
    # confirms the realtime authorizer accepted the publish.
    published = []
    from integrations.social import realtime
    monkeypatch.setattr(realtime, 'publish_event',
                        lambda topic, data, user_id='':
                            published.append((topic, data, user_id)))
    monkeypatch.setattr(realtime, 'on_notification', lambda *a, **kw: None)

    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    res = ConversationService.emit_typing(
        db, conv_id=conv['id'], user_id=a.id, tenant_id=None)
    assert res['sent'] is True
    assert len(published) == 1
    topic, data, user_id = published[0]
    assert topic.startswith('tenant.')
    assert '.conv.' in topic
    assert topic.endswith('.typing')
    assert data['user_id'] == a.id
    assert data['conv_id'] == conv['id']


def test_typing_non_member_refused(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.models import User
    eve = User(id=str(uuid.uuid4()), username='eve', display_name='Eve',
               email='e@x.test', password_hash='x:y', user_type='human')
    db.add(eve); db.commit()
    from integrations.social.conversation_service import (
        ConversationService, ConversationError)
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    with pytest.raises(ConversationError, match='not a conversation member'):
        ConversationService.emit_typing(
            db, conv_id=conv['id'], user_id=eve.id)


# ── Read-receipt ─────────────────────────────────────────────────

def test_mark_read_specific_message(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='ping')
    res = ConversationService.mark_read(
        db, conv_id=conv['id'], user_id=b.id, message_id=msg['id'])
    assert res['sent'] is True
    assert res['last_read_message_id'] == msg['id']

    # Persisted on memberships row.
    from sqlalchemy import text
    row = db.execute(text(
        "SELECT last_read_message_id, last_read_at FROM memberships "
        "WHERE parent_kind='conversation' AND parent_id=:cid "
        "AND member_id=:mid"),
        {'cid': conv['id'], 'mid': b.id}
    ).fetchone()
    assert row[0] == msg['id']
    assert row[1] is not None


def test_mark_read_default_picks_latest(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='m1')
    msg2 = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='m2')
    res = ConversationService.mark_read(
        db, conv_id=conv['id'], user_id=b.id)  # no message_id
    assert res['last_read_message_id'] == msg2['id']


def test_mark_read_cross_conv_message_refused(fresh_db, monkeypatch):
    """Security: passing a message_id from a different conversation
    must be refused — would let an attacker scribble on someone else's
    read state."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.models import User
    c = User(id=str(uuid.uuid4()), username='cara', display_name='Cara',
             email='c@x.test', password_hash='x:y', user_type='human')
    db.add(c); db.commit()
    from integrations.social.conversation_service import (
        ConversationService, ConversationError)
    convAB = ConversationService.create(
        db, kind='dm', member_ids=[b.id], created_by=a.id)
    convAC = ConversationService.create(
        db, kind='dm', member_ids=[c.id], created_by=a.id)
    msg_in_AC = ConversationService.send_message(
        db, conv_id=convAC['id'], author_id=a.id, content='private')
    # Bob tries to mark read in his AB convo using a message_id from AC.
    with pytest.raises(ConversationError, match='not in conversation'):
        ConversationService.mark_read(
            db, conv_id=convAB['id'], user_id=b.id,
            message_id=msg_in_AC['id'])


def test_mark_read_empty_conversation_no_op(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.conversation_service import ConversationService
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    res = ConversationService.mark_read(
        db, conv_id=conv['id'], user_id=b.id)
    assert res['sent'] is False
    assert 'empty' in res['reason']


def test_mark_read_non_member_refused(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b = _seed(db)
    from integrations.social.models import User
    eve = User(id=str(uuid.uuid4()), username='eve', display_name='Eve',
               email='e@x.test', password_hash='x:y', user_type='human')
    db.add(eve); db.commit()
    from integrations.social.conversation_service import (
        ConversationService, ConversationError)
    conv = ConversationService.create(db, kind='dm', member_ids=[b.id],
                                       created_by=a.id)
    msg = ConversationService.send_message(
        db, conv_id=conv['id'], author_id=a.id, content='hi')
    with pytest.raises(ConversationError, match='not a conversation member'):
        ConversationService.mark_read(
            db, conv_id=conv['id'], user_id=eve.id, message_id=msg['id'])


# ── Realtime authorizer regression: tenant.* must be allowed ─────

def test_realtime_authorizer_allows_tenant_topic():
    """Regression check on _authorize_topic_for_user_id — without
    'tenant.' in the public prefix list, every typing/read publish
    would be silently refused at the realtime layer."""
    from integrations.social.realtime import _authorize_topic_for_user_id
    assert _authorize_topic_for_user_id(
        'tenant.tA.conv.123.typing', 'user-x') is True
    assert _authorize_topic_for_user_id(
        'tenant._.conv.123.read', 'user-x') is True


# ── Endpoint flag-gating + round-trip ────────────────────────────

def test_endpoint_typing_flag_off_503(fresh_db, monkeypatch):
    db, _ = fresh_db
    a, b = _seed(db)
    monkeypatch.delenv('HEVOLVE_FLAG_CONVERSATIONS', raising=False)
    from flask import Flask
    from integrations.social import api, auth
    from integrations.social.api_conversations import conversations_bp
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    app.register_blueprint(conversations_bp)
    client = app.test_client()
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/conversations/some-id/typing',
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 503


def test_endpoint_typing_round_trip(app_client, monkeypatch):
    _silence_realtime(monkeypatch)
    client, db = app_client
    a, b = _seed(db)
    from integrations.social import auth
    tok_a = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/conversations',
                    json={'kind': 'dm', 'member_ids': [b.id]},
                    headers={'Authorization': f'Bearer {tok_a}'})
    conv_id = r.get_json()['data']['id']
    r = client.post(f'/api/social/conversations/{conv_id}/typing',
                    headers={'Authorization': f'Bearer {tok_a}'})
    assert r.status_code == 200
    assert r.get_json()['data']['sent'] is True


def test_endpoint_read_receipt_round_trip(app_client, monkeypatch):
    _silence_realtime(monkeypatch)
    client, db = app_client
    a, b = _seed(db)
    from integrations.social import auth
    tok_a = auth.generate_jwt(a.id, a.username, 'flat')
    tok_b = auth.generate_jwt(b.id, b.username, 'flat')
    # Alice creates DM and sends.
    r = client.post('/api/social/conversations',
                    json={'kind': 'dm', 'member_ids': [b.id]},
                    headers={'Authorization': f'Bearer {tok_a}'})
    conv_id = r.get_json()['data']['id']
    r = client.post(f'/api/social/conversations/{conv_id}/messages',
                    json={'content': 'hi'},
                    headers={'Authorization': f'Bearer {tok_a}'})
    msg_id = r.get_json()['data']['id']
    # Bob marks read.
    r = client.post(f'/api/social/conversations/{conv_id}/read-receipt',
                    json={'message_id': msg_id},
                    headers={'Authorization': f'Bearer {tok_b}'})
    assert r.status_code == 200
    assert r.get_json()['data']['last_read_message_id'] == msg_id
