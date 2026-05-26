"""End-to-end fan-out tests for Phase 7c social writes.

Reviewer H2 was: "fan-out leg tests prove almost nothing — they call
bus.publish directly, bypassing the realtime → bus chain."  This file
asserts the FULL chain end-to-end through the actual Flask endpoint:

    HTTP POST /api/social/conversations/<id>/messages
        → ConversationService.send_message
        → _notify_members → NotificationService.create
        → event.listen(after_commit, _push_after_commit, once=True)
        → @require_auth's db.commit() fires the after_commit hook
        → realtime.on_notification → realtime.publish_event
        → MessageBus.publish
        → LOCAL leg subscribers fire
        → CROSSBAR HTTP transport called with TOPIC_MAP-translated topic

Without going through `@require_auth`'s commit, the deferred
after_commit hook never fires (NotificationService.create flushes but
doesn't commit; the request decorator owns the commit).
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


def _seed(db):
    from integrations.social.models import User
    a = User(id=str(uuid.uuid4()), username='alice', display_name='A',
             email='a@x.test', password_hash='x:y', user_type='human')
    b = User(id=str(uuid.uuid4()), username='bob', display_name='B',
             email='b@x.test', password_hash='x:y', user_type='human')
    db.add_all([a, b]); db.commit()
    return a, b


# ── E2E LOCAL leg: API send → bus subscriber receives ──────────────

def test_e2e_message_send_reaches_local_subscriber(app_client):
    """A real HTTP send_message must light up a real LOCAL bus
    subscriber — proves the whole chain wires up.  The chain ends at
    realtime.on_notification → publish_event → MessageBus.publish on
    topic 'chat.social' (per realtime.py:172-177)."""
    client, db = app_client
    a, b = _seed(db)
    from core.peer_link.message_bus import (
        get_message_bus, reset_message_bus)
    reset_message_bus()
    bus = get_message_bus()

    received = []
    bus.subscribe('chat.social', lambda topic, data: received.append(
        (topic, data)))

    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    # Create the DM (fires no notifications — alice is the sole creator).
    r = client.post('/api/social/conversations',
                    json={'kind': 'dm', 'member_ids': [b.id]},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201
    conv_id = r.get_json()['data']['id']
    # Send a message — fires NotificationService.create for bob.
    r = client.post(f'/api/social/conversations/{conv_id}/messages',
                    json={'content': 'hello bob'},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201

    assert len(received) > 0, (
        "API send_message → after_commit → realtime → MessageBus chain "
        "delivered nothing to a real LOCAL subscriber. Review the "
        "after_commit hook + on_notification + bus.subscribe wiring.")
    # Payload includes the recipient's user_id (notification target).
    assert any(d.get('user_id') == b.id for _t, d in received), (
        f"published payloads had no user_id={b.id}: {received}")


# ── E2E CROSSBAR leg: API send → injected http transport fires ─────

def test_e2e_message_send_reaches_crossbar_transport(app_client):
    """Same chain but with the CROSSBAR HTTP transport injected.  The
    transport callable must fire with the TOPIC_MAP-translated topic
    (com.hertzai.hevolve.social.<recipient_user_id>) and a JSON
    payload."""
    client, db = app_client
    a, b = _seed(db)
    from core.peer_link.message_bus import (
        get_message_bus, reset_message_bus)
    reset_message_bus()
    bus = get_message_bus()

    transport_calls = []
    bus.set_http_transport(
        lambda topic, payload: transport_calls.append((topic, payload)))

    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/conversations',
                    json={'kind': 'dm', 'member_ids': [b.id]},
                    headers={'Authorization': f'Bearer {tok}'})
    conv_id = r.get_json()['data']['id']
    client.post(f'/api/social/conversations/{conv_id}/messages',
                json={'content': 'ping'},
                headers={'Authorization': f'Bearer {tok}'})

    assert any(
        c[0].startswith('com.hertzai.hevolve.social.') and c[0].endswith(b.id)
        for c in transport_calls
    ), (
        f"crossbar HTTP transport never fired for recipient bob's id. "
        f"Calls: {transport_calls}. "
        f"Either the after_commit hook is not running, "
        f"NotificationService.create is not calling realtime.on_notification, "
        f"or realtime.publish_event is not reaching MessageBus._route_crossbar.")


# ── E2E reaction toggle reflects to fresh observer session ─────────

def test_e2e_reaction_toggle_persists_for_observer(app_client):
    """Reactions don't fan out today, but they DO update observable
    state immediately.  Asserts the toggle is visible to a fresh DB
    session via list_for — a transport-independent coherence floor."""
    client, db = app_client
    a, b = _seed(db)
    from integrations.social.models import Community, Post
    com = Community(id=str(uuid.uuid4()), name='c', display_name='C',
                    description='', creator_id=a.id, is_private=False)
    db.add(com); db.commit()
    p = Post(id='p1', author_id=a.id, community_id=com.id,
             title='hi', content='hi', content_type='text')
    db.add(p); db.commit()

    # Toggle via direct service (reactions has its own flag we don't
    # need to set up here; service-level toggle bypasses the route
    # gate but the read assertion is what matters).
    from integrations.social.reaction_service import ReactionService
    ReactionService.toggle(db, 'post', p.id, b.id, '🔥')

    from integrations.social.models import get_db as _get
    obs = _get()
    try:
        listed = ReactionService.list_for(obs, 'post', p.id, viewer_id=b.id)
        assert any(r['emoji'] == '🔥' and r['count'] == 1
                   for r in listed), (
            f"reaction not visible to fresh session — likely commit "
            f"missing or read-after-write issue. Got: {listed}")
    finally:
        obs.close()


# ── SSE absence does not poison the rest of the chain ─────────────

def test_sse_leg_does_not_break_chain_when_broker_absent(app_client):
    """SSE leg uses core.platform.events.broadcast_sse_safe which
    no-ops gracefully when the broker isn't set up.  An API
    send_message must still complete + commit + crossbar-fan-out."""
    client, db = app_client
    a, b = _seed(db)
    from core.peer_link.message_bus import (
        get_message_bus, reset_message_bus)
    reset_message_bus()
    bus = get_message_bus()

    transport_calls = []
    bus.set_http_transport(
        lambda topic, payload: transport_calls.append((topic, payload)))

    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/conversations',
                    json={'kind': 'dm', 'member_ids': [b.id]},
                    headers={'Authorization': f'Bearer {tok}'})
    conv_id = r.get_json()['data']['id']
    r = client.post(f'/api/social/conversations/{conv_id}/messages',
                    json={'content': 'alive'},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201
    assert any(b.id in c[0] for c in transport_calls)
