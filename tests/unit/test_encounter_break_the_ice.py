"""
"Break the ice" on a match: the icebreaker endpoints work for GPS
(ProximityMatch) matches as well as BLE ones, and an approved opener
lands as the first message of the DM between the two people.

Runs on the real migrated schema (a per-test SQLite file) so the DM is
created and posted by ConversationService itself, not a stand-in.
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_TOKEN = 'TEST-USER-'
A, B, STRANGER = 'u-alice', 'u-bob', 'u-carol'


@pytest.fixture
def env(monkeypatch, tmp_path):
    """encounter_bp on a migrated DB of its own, auth faked to string
    user ids, `conversations` flag on (flip with env.flags)."""
    from integrations.social import auth as auth_mod
    monkeypatch.setattr(auth_mod, "_jwt_manager", False)
    from integrations.social import models as models_mod
    # models resolves its DB once at import; give this test a fresh one.
    db_file = tmp_path / 'social.db'
    monkeypatch.setattr(models_mod, 'DB_PATH', str(db_file))
    monkeypatch.setattr(models_mod, 'DB_URL', f'sqlite:///{db_file}')
    models_mod._engine = None
    models_mod._SessionLocal = None
    from integrations.social import migrations
    from integrations.social.models import get_engine, get_db
    engine = get_engine()
    migrations.run_migrations()

    from integrations.social import realtime
    monkeypatch.setattr(realtime, 'on_notification', lambda *a, **k: None)
    monkeypatch.setattr(realtime, 'publish_event', lambda *a, **k: None)

    def _fake_user(token):
        if not isinstance(token, str) or not token.startswith(_TOKEN):
            return None, None
        uid = token[len(_TOKEN):]
        return (SimpleNamespace(id=uid, is_admin=False, is_moderator=False),
                get_db())
    monkeypatch.setattr(auth_mod, '_get_user_from_token', _fake_user)

    flags = {'conversations': True}
    from integrations.social import feature_flags
    monkeypatch.setattr(feature_flags, 'get_flags_for_tenant',
                        lambda db, tid: dict(flags))

    from flask import Flask
    from integrations.social import encounter_api
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(encounter_api.encounter_bp)
    yield SimpleNamespace(client=app.test_client(), db=get_db, flags=flags)
    engine.dispose()
    models_mod._engine = None
    models_mod._SessionLocal = None


def _as(uid):
    return {'Authorization': f'Bearer {_TOKEN}{uid}'}


def _gps_match(env, status='matched'):
    from integrations.social.models import ProximityMatch
    db = env.db()
    m = ProximityMatch(
        user_a_id=A, user_b_id=B, lat=12.97, lon=77.59, distance_m=40.0,
        status=status, expires_at=datetime.utcnow() + timedelta(hours=4),
    )
    db.add(m)
    db.commit()
    mid = m.id
    db.close()
    return mid


def _dm_messages(env):
    from sqlalchemy import text
    from integrations.social.conversation_service import ConversationService
    db = env.db()
    try:
        conv = ConversationService.find_dm(db, A, B)
        if conv is None:
            return None, []
        rows = db.execute(text(
            "SELECT author_id, content FROM messages "
            "WHERE parent_kind='conversation' AND parent_id=:c "
            "ORDER BY created_at"), {'c': conv}).fetchall()
        return conv, [tuple(r) for r in rows]
    finally:
        db.close()


def _post(env, path, body, uid=A):
    return env.client.post(f'/api/social/encounter/icebreaker/{path}',
                           json=body, headers=_as(uid))


def test_gps_match_gets_a_draft(env):
    mid = _gps_match(env)
    r = _post(env, 'draft', {'match_id': mid, 'kind': 'proximity'})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()['data']
    assert data['draft']
    assert len(data['alt_drafts']) == 2
    assert data['conversation_id'] is None  # nothing sent yet


def test_pending_gps_match_has_no_icebreaker(env):
    # Until both reveal, the match names nobody.
    mid = _gps_match(env, status='revealed_a')
    r = _post(env, 'draft', {'match_id': mid, 'kind': 'proximity'})
    assert r.status_code == 404


def test_gps_icebreaker_becomes_first_dm_message(env):
    mid = _gps_match(env)
    r = _post(env, 'approve', {'match_id': mid, 'kind': 'proximity',
                               'text': 'Hey! Saw you at the cafe.'})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()['data']
    conv_id, msgs = _dm_messages(env)
    assert data['conversation_id'] == conv_id
    assert msgs == [(A, 'Hey! Saw you at the cafe.')]


def test_gps_icebreaker_only_once(env):
    mid = _gps_match(env)
    _post(env, 'approve', {'match_id': mid, 'kind': 'proximity',
                           'text': 'Hi there'})
    again = _post(env, 'approve', {'match_id': mid, 'kind': 'proximity',
                                   'text': 'Hi again'})
    assert again.status_code == 409
    # And a fresh draft points at the existing chat instead.
    d = _post(env, 'draft', {'match_id': mid, 'kind': 'proximity'})
    conv_id, msgs = _dm_messages(env)
    assert d.get_json()['data']['conversation_id'] == conv_id
    assert len(msgs) == 1


def test_other_side_can_still_break_the_ice(env):
    mid = _gps_match(env)
    _post(env, 'approve', {'match_id': mid, 'kind': 'proximity',
                           'text': 'Hi Bob'}, uid=A)
    r = _post(env, 'approve', {'match_id': mid, 'kind': 'proximity',
                               'text': 'Hi Alice'}, uid=B)
    assert r.status_code == 200
    _, msgs = _dm_messages(env)
    assert msgs == [(A, 'Hi Bob'), (B, 'Hi Alice')]


def test_gps_icebreaker_stranger_404(env):
    mid = _gps_match(env)
    r = _post(env, 'approve', {'match_id': mid, 'kind': 'proximity',
                               'text': 'hi'}, uid=STRANGER)
    assert r.status_code == 404
    assert _dm_messages(env) == (None, [])


def test_gps_icebreaker_needs_conversations_flag(env):
    env.flags['conversations'] = False
    mid = _gps_match(env)
    r = _post(env, 'approve', {'match_id': mid, 'kind': 'proximity',
                               'text': 'hi'})
    assert r.status_code == 503


def test_gps_decline_sends_nothing(env):
    mid = _gps_match(env)
    r = _post(env, 'decline', {'match_id': mid, 'kind': 'proximity',
                               'reason': 'Not feeling it'})
    assert r.status_code == 200
    assert r.get_json()['data']['status'] == 'declined'
    assert _dm_messages(env) == (None, [])


def test_unknown_kind_400(env):
    mid = _gps_match(env)
    r = _post(env, 'draft', {'match_id': mid, 'kind': 'carrier-pigeon'})
    assert r.status_code == 400


def test_kind_defaults_to_ble(env):
    # Older clients send no kind: a GPS match id must not resolve as BLE.
    mid = _gps_match(env)
    r = _post(env, 'draft', {'match_id': mid})
    assert r.status_code == 404


def test_ble_icebreaker_also_lands_in_dm(env):
    from integrations.social.models import Encounter
    db = env.db()
    enc = Encounter(user_a_id=A, user_b_id=B, context_type='ble',
                    context_id='ctx-ble-1')
    db.add(enc)
    db.commit()
    mid = enc.id
    db.close()
    r = _post(env, 'approve', {'match_id': mid, 'text': 'Hello from BLE'})
    assert r.status_code == 200, r.get_json()
    conv_id, msgs = _dm_messages(env)
    assert r.get_json()['data']['conversation_id'] == conv_id
    assert msgs == [(A, 'Hello from BLE')]


# ── Review of #147 ─────────────────────────────────────────────────────

def _ble_match(env):
    from integrations.social.models import Encounter
    db = env.db()
    enc = Encounter(user_a_id=A, user_b_id=B, context_type='ble',
                    context_id='ctx-ble-2')
    db.add(enc)
    db.commit()
    mid = enc.id
    db.close()
    return mid


def _say(env, uid, text, source=None):
    """A message in the A-B DM, posted the way the chat screen posts it."""
    from integrations.social.conversation_service import ConversationService
    db = env.db()
    try:
        conv = ConversationService.create(db, kind='dm', member_ids=[A, B],
                                          created_by=uid)
        ConversationService.send_message(
            db, conv['id'], uid, text,
            metadata={'source': source} if source else None)
    finally:
        db.close()


def test_an_earlier_chat_message_does_not_block_the_icebreaker(env):
    # Only an earlier OPENER counts as "already sent", not small talk.
    mid = _gps_match(env)
    _say(env, A, 'hey, we met at the cafe')
    r = _post(env, 'approve', {'match_id': mid, 'kind': 'proximity',
                               'text': 'Breaking the ice properly'})
    assert r.status_code == 200, r.get_json()
    _, msgs = _dm_messages(env)
    assert msgs == [(A, 'hey, we met at the cafe'),
                    (A, 'Breaking the ice properly')]


def test_an_expired_gps_match_takes_no_opener(env):
    from integrations.social.models import ProximityMatch
    mid = _gps_match(env)
    db = env.db()
    db.query(ProximityMatch).filter_by(id=mid).one().expires_at = (
        datetime.utcnow() - timedelta(minutes=1))
    db.commit()
    db.close()
    for path, body in (('draft', {}), ('approve', {'text': 'hi'})):
        r = _post(env, path, dict(body, match_id=mid, kind='proximity'))
        assert r.status_code == 404, (path, r.get_json())
    assert _dm_messages(env) == (None, [])


def test_a_failing_sent_check_sends_nothing(env, monkeypatch):
    # The old lookup answered "not sent" on any error, so a broken DB
    # read could post the opener a second time.
    from integrations.social.conversation_service import ConversationService
    mid = _gps_match(env)
    _post(env, 'approve', {'match_id': mid, 'kind': 'proximity',
                           'text': 'first'})

    def broken(*a, **k):
        raise RuntimeError('db read failed')
    monkeypatch.setattr(ConversationService, 'has_message_from', broken)
    with pytest.raises(RuntimeError):
        _post(env, 'approve', {'match_id': mid, 'kind': 'proximity',
                               'text': 'second'})
    _, msgs = _dm_messages(env)
    assert msgs == [(A, 'first')]


def test_two_quick_taps_send_one_opener(env, monkeypatch):
    # The second tap arrives while the first is still sending: it waits
    # for the first to finish, then finds the opener and gets 409.
    import threading
    import time as _time
    from integrations.social import encounter_api
    mid = _gps_match(env)
    real_send = encounter_api._send_icebreaker_as_dm
    second = {}

    def tap_again():
        second['r'] = _post(env, 'approve', {'match_id': mid,
                                             'kind': 'proximity',
                                             'text': 'Hi (tap 2)'})

    def slow_send(*a, **k):
        if 'thread' not in second:
            second['thread'] = threading.Thread(target=tap_again)
            second['thread'].start()
            _time.sleep(0.3)
            second['waiting'] = second['thread'].is_alive()
        return real_send(*a, **k)
    monkeypatch.setattr(encounter_api, '_send_icebreaker_as_dm', slow_send)

    first = _post(env, 'approve', {'match_id': mid, 'kind': 'proximity',
                                   'text': 'Hi (tap 1)'})
    second['thread'].join(10)
    assert first.status_code == 200, first.get_json()
    assert second['waiting'], 'the second tap did not wait for the first'
    assert second['r'].status_code == 409
    _, msgs = _dm_messages(env)
    assert msgs == [(A, 'Hi (tap 1)')]


def test_ble_opener_already_in_the_dm_is_recorded_not_resent(env):
    # The DM commits before the match's 'sent' state; if that state write
    # was lost, the retry must not post the opener again.
    from integrations.social.models import Encounter
    mid = _ble_match(env)
    _say(env, A, 'Hello from BLE', source='encounter_icebreaker')
    r = _post(env, 'approve', {'match_id': mid, 'text': 'Hello from BLE'})
    assert r.status_code == 409
    _, msgs = _dm_messages(env)
    assert msgs == [(A, 'Hello from BLE')]
    db = env.db()
    payload = db.query(Encounter).filter_by(id=mid).one().payload
    db.close()
    assert payload['icebreaker_a']['status'] == 'sent'  # A is user_a
