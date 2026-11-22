"""An answered ask drops from the phone as it drops from every other surface.

Owner, 21 Nov 2026: "All questions via hartos shd reach all my signed in
device and dismiss on any of my response everywhere ... i don't see this
happening for my android phone".  Measured:

  * the drop (person_ask.drop_everywhere, the one drop fan-out) reached only
    the WAMP and SSE legs, so the phone, which is not on this node's router,
    never heard it;
  * a direct Firebase send put every push in a notification block, so a
    consent ask showed as a banner and, with the app in the background,
    never reached MyFirebaseMessagingService (central sends it data-only);
  * a consent grant or "no" dropped nothing: the consent row's card stayed
    on the phone.

    python -m pytest tests/unit/test_an_answered_ask_drops_on_the_phone.py -q
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import fcm_sync  # noqa: E402

UID = '10'
AGENT = '88659566083'


# ── the Firebase message: shaped as central sends it ────────────────────────

def test_a_consent_ask_goes_data_only_so_the_phone_handles_it_in_the_background():
    m = fcm_sync.build_fcm_v1_message(
        'tok', 'Hevolve consent', 'Enter the password',
        data={'type': 'consent_prompt', 'request_id': 'row-7',
              'consent_type': 'credential'})['message']
    assert 'notification' not in m
    assert m['android'] == {'priority': 'high', 'ttl': '86400s'}
    assert m['data']['request_id'] == 'row-7'


def test_a_banner_carries_its_request_as_its_tag_so_the_drop_can_take_it_down():
    m = fcm_sync.build_fcm_v1_message(
        'tok', 'A question', 'Which supplier?',
        data={'type': 'person_ask', 'request_id': 'ask-3'})['message']
    assert m['notification'] == {'title': 'A question', 'body': 'Which supplier?'}
    assert m['android']['notification'] == {'tag': 'ask-3'}
    assert m['android']['priority'] == 'high'


def test_a_banner_without_a_request_has_no_tag():
    m = fcm_sync.build_fcm_v1_message('tok', 'T', 'B', data={'k': 1})['message']
    assert 'notification' not in m['android']


def test_the_drop_push_has_no_banner():
    m = fcm_sync.build_fcm_v1_message(
        'tok', '', '', data={'type': 'ask.answered', 'request_id': 'ask-3'})['message']
    assert 'notification' not in m
    assert m['data']['request_id'] == 'ask-3'


# ── the relay: central would show a data-only push as a banner ──────────────

def test_a_data_only_push_is_not_handed_to_central():
    bus = MagicMock()
    with patch('core.peer_link.message_bus.get_message_bus', return_value=bus):
        ok = fcm_sync.hand_push_to_central(
            '9003054371', '', '', {'type': 'ask.answered', 'request_id': 'a'})
    assert ok is False
    bus.publish.assert_not_called()


def test_a_consent_ask_still_goes_to_central():
    bus = MagicMock()
    bus.get_stats.side_effect = [{'delivered_crossbar': 0},
                                 {'delivered_crossbar': 1}]
    with patch('core.peer_link.message_bus.get_message_bus', return_value=bus), \
            patch.object(fcm_sync, 'resolve_central_id', return_value=None):
        ok = fcm_sync.hand_push_to_central(
            '9003054371', '', '', {'type': 'consent_prompt', 'request_id': 'row-7'})
    assert ok is True
    assert bus.publish.call_args.args[0] == 'task.confirmation'


# ── the node's push leg ─────────────────────────────────────────────────────

def test_a_push_with_no_title_or_body_stays_silent(monkeypatch):
    from integrations.social import realtime
    sent = []
    monkeypatch.setattr(fcm_sync, 'send_fcm_push',
                        lambda uid, title, body, data=None: sent.append(
                            (uid, title, body, data)) or True)
    realtime._send_push(UID, {'data': {'type': 'ask.answered', 'request_id': 'a'}})
    assert sent == [(UID, '', '', {'type': 'ask.answered', 'request_id': 'a'})]


def test_a_push_with_a_body_keeps_its_default_title(monkeypatch):
    from integrations.social import realtime
    sent = []
    monkeypatch.setattr(fcm_sync, 'send_fcm_push',
                        lambda uid, title, body, data=None: sent.append(title) or True)
    realtime._send_push(UID, {'body': 'Which supplier?', 'data': {}})
    assert sent == ['HART']


def test_a_forgotten_push_may_ring_again():
    from integrations.social import realtime
    msg_id = 'consent.request:test-forget'
    realtime.forget_push(msg_id)
    assert realtime._first_push_of(msg_id) is True
    assert realtime._first_push_of(msg_id) is False
    realtime.forget_push(msg_id)
    assert realtime._first_push_of(msg_id) is True
    realtime.forget_push(msg_id)


def test_the_drop_goes_to_the_phone_as_a_data_only_push_naming_the_request(monkeypatch):
    from integrations.agent_engine import person_ask
    from integrations.social import realtime
    calls = []
    monkeypatch.setattr(realtime, 'on_notification',
                        lambda uid, note, push=None: calls.append((uid, note, push)))
    monkeypatch.setattr(person_ask, '_withdraw_card', lambda ask_id: None)

    person_ask.drop_everywhere('row-7', UID)

    (uid, note, push), = calls
    assert uid == UID
    assert note['type'] == person_ask.ANSWERED_TYPE and note['ask_id'] == 'row-7'
    assert 'title' not in push and 'body' not in push
    # Beside these the drop may name the node it happened on, nothing else.
    said = {k: v for k, v in push['data'].items()
            if k not in ('node_id', 'node_tier', 'node_name')}
    assert said == {'type': person_ask.ANSWERED_TYPE,
                    'request_id': 'row-7', 'ask_id': 'row-7'}


# ── a consent answer drops its card ─────────────────────────────────────────

@pytest.fixture
def db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import integrations.social.consent_service as cs
    from integrations.social.models import UserConsent

    engine = create_engine('sqlite://')
    UserConsent.__table__.create(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    for name in ('_emit', '_copilot_switch_from_consent',
                 '_embodied_feed_from_consent', '_mailbox_goals_from_consent'):
        if hasattr(cs, name):
            monkeypatch.setattr(cs, name, lambda *a, **k: None)
    dropped = []
    monkeypatch.setattr(cs, '_drop_ask',
                        lambda user_id, ask_id: dropped.append((user_id, ask_id)))
    session.dropped = dropped
    yield session
    session.close()
    engine.dispose()


def _ask(db, consent_type, scope='*', agent_id=None):
    from integrations.social.consent_service import ConsentService
    row = ConsentService.request_consent(db, UID, consent_type,
                                         scope=scope, agent_id=agent_id)
    db.commit()
    db.dropped.clear()
    return row


def test_a_yes_drops_the_ask_it_answers(db):
    from integrations.social.consent_service import ConsentService
    ask = _ask(db, 'credential', scope='secret:MAILBOX')

    ConsentService.grant_consent(db, UID, 'credential', scope='secret:MAILBOX')
    assert db.dropped == [], 'the drop waits for the commit'
    db.commit()

    assert db.dropped == [(UID, ask.id)]


def test_a_per_agent_yes_drops_the_ask_it_landed_on(db):
    from integrations.social.consent_service import ConsentService
    ask = _ask(db, 'screen_capture', scope='display:1', agent_id=AGENT)

    row = ConsentService.grant_consent(db, UID, 'screen_capture',
                                       scope='display:1', agent_id=AGENT)
    db.commit()

    assert row.id == ask.id                       # promoted in place
    assert db.dropped == [(UID, ask.id)]


def test_a_yes_leaves_the_asks_it_does_not_answer(db):
    from integrations.social.consent_service import ConsentService
    other = _ask(db, 'credential', scope='secret:OTHER')
    db.dropped.clear()

    ConsentService.grant_consent(db, UID, 'credential', scope='secret:MAILBOX')
    db.commit()

    assert (UID, other.id) not in db.dropped


def test_a_no_drops_the_ask(db):
    from integrations.social.consent_service import ConsentService
    ask = _ask(db, 'credential', scope='secret:MAILBOX')

    ConsentService.decline(db, UID, 'credential', scope='secret:MAILBOX')
    db.commit()

    assert db.dropped == [(UID, ask.id)]


def test_a_no_drops_only_its_own_agents_ask(db):
    from integrations.social.consent_service import ConsentService
    mine = _ask(db, 'screen_capture', scope='display:1', agent_id=AGENT)
    other = _ask(db, 'screen_capture', scope='display:1', agent_id='42')

    ConsentService.decline(db, UID, 'screen_capture', scope='display:1',
                           agent_id=AGENT)
    db.commit()

    assert db.dropped == [(UID, mine.id)]
    assert other.id != mine.id
