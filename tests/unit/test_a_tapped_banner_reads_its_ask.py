"""A person ask's banner, tapped with the app closed, reads the one ask from
the door its answer goes to.

Owner, 23 Nov 2026: "when the app is not open the central or desktop flat
node shd be able to communicate with user via Firebase".  The push opens the
app (click_action OPEN_PERSON_ASK) with the ask's id; the phone then reads
the ask from its desktop, ``GET /api/agent/approval?ask_id=<id>``, on the
same door and the same device link its answer takes (PersonAsk.APPROVAL_PATH,
DesktopRouteInterceptor), modelled on the consent read
(``GET /api/social/consent?request_id=``).

The reader is who the answer door takes as the answerer
(person_ask.answer_from_request: a signed-in person by their token, a phone
as the owner who allowed it).  Only the ask's own person gets its card; for
anyone else, and for an ask no longer open (answered, dismissed, let go,
unknown), it is 404 {"reason": "gone"}, which says nothing about whose it was.

Real hart_intelligence_entry app, real gate, real consent rows (the fixtures
of test_person_ask_from_a_phone).

    python -m pytest tests/unit/test_a_tapped_banner_reads_its_ask.py -q
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

import pytest  # noqa: E402

from tests.unit.test_person_ask_from_a_phone import (  # noqa: E402,F401 (fixtures)
    LAN, OWNER, _allowed, _ask, _call, _desktop, _yes, hie, legs)


def _read(hie, monkeypatch, phone, ask_id, environ=LAN):
    return _call(hie, monkeypatch, phone, method='GET',
                 path=f'/api/agent/approval?ask_id={ask_id}', environ=environ)


GONE = {'status': 'error', 'reason': 'gone'}


@pytest.mark.parametrize('environ', [LAN, None], ids=['lan', 'nunba_hop'])
def test_the_owners_phone_reads_her_open_ask(hie, legs, monkeypatch, environ):
    asked = _ask()
    r = _read(hie, monkeypatch, _allowed(), asked, environ)
    assert r.status_code == 200, r.get_json()
    card = r.get_json()
    assert card['ask_id'] == asked
    assert card['type'] == 'approval'
    assert card['description'] == 'Post the reel today?'
    # the asking agent: the phone shows it in that agent's chat when open
    assert card['prompt_id'] == '342'


def test_another_persons_phone_gets_gone_for_her_ask_and_for_its_own(hie, legs,
                                                                    monkeypatch):
    # the owner allowed this phone as 777: it speaks for nobody's asks here
    # (the answer door's rule, test_person_ask_from_a_phone C2)
    mine, theirs = _ask(), _ask('777')
    other = _allowed(user_id='777')
    for asked in (mine, theirs):
        r = _read(hie, monkeypatch, other, asked)
        assert (r.status_code, r.get_json()) == (404, GONE)


def test_an_answered_ask_is_gone(hie, legs, monkeypatch):
    asked = _ask()
    phone = _allowed()
    assert _call(hie, monkeypatch, phone, _yes(asked)).status_code == 200
    r = _read(hie, monkeypatch, phone, asked)
    assert (r.status_code, r.get_json()) == (404, GONE)


def test_a_dismissed_ask_is_gone(hie, legs, monkeypatch):
    asked = _ask()
    phone = _allowed()
    r = _call(hie, monkeypatch, phone, {'ask_id': asked, 'dismiss': True,
                                        'user_id': OWNER})
    assert r.status_code == 200, r.get_json()
    r = _read(hie, monkeypatch, phone, asked)
    assert (r.status_code, r.get_json()) == (404, GONE)


def test_an_ask_let_go_or_never_asked_is_gone(hie, legs, monkeypatch):
    from integrations.agent_engine import person_ask
    asked = _ask()
    with person_ask._lock:            # a restart, or kept past its time
        person_ask._asks.pop(asked)
    phone = _allowed()
    for ask_id in (asked, 'no-such-ask', ''):
        r = _read(hie, monkeypatch, phone, ask_id)
        assert (r.status_code, r.get_json()) == (404, GONE), ask_id


def test_the_read_never_changes_the_ask(hie, legs, monkeypatch):
    from integrations.agent_engine import person_ask
    asked = _ask()
    for _ in range(2):
        assert _read(hie, monkeypatch, _allowed(), asked).status_code == 200
    assert person_ask.ask_state(asked, OWNER)['status'] == 'open'
    assert not [note for _, note in legs['events']      # no drop went out
                if note.get('type') == person_ask.ANSWERED_TYPE
                and note.get('ask_id') == asked]


def test_the_phones_read_rides_the_device_link_to_this_desktop(hie):
    """The phone sends the read as the cloud call it knows (GET
    https://azurekong.hertzai.com/api/agent/approval?ask_id=, desktop-only);
    this desktop claims it, as it claims the answer."""
    from integrations.mobile_adapter import aliases
    url = f'https://{aliases.CLOUD}/api/agent/approval?ask_id=a1'
    assert aliases.resolve('GET', url) == '/api/agent/approval?ask_id=a1'
    rows = aliases.on_host(hie.app)
    assert f'GET {aliases.CLOUD} /api/agent/approval {aliases.SHORT_WAIT_S}' in \
        aliases.served(rows)


def test_the_reader_is_who_the_answer_door_takes_as_the_answerer(monkeypatch):
    """One identity rule for the read and the answer (answer_from_request):
    a signed-in person by their token, else a phone as the owner who allowed
    it, else nobody."""
    from integrations.agent_engine import person_ask
    import integrations.commerce.approvals as approvals
    import integrations.social.auth as auth
    seen = []
    monkeypatch.setattr(person_ask, 'read_ask',
                        lambda ask_id, reader: seen.append((ask_id, reader)) or ({}, 200))
    monkeypatch.setattr(approvals, 'approver_from_request', lambda req: 'signed-in')
    monkeypatch.setattr(approvals, 'bearer_token', lambda req: 'tok')
    monkeypatch.setattr(auth, 'allowed_device_owner', lambda tok: 'phone-owner')

    class Req:
        args = {'ask_id': ' a1 '}
    person_ask.read_from_request(Req())
    monkeypatch.setattr(approvals, 'approver_from_request', lambda req: None)
    person_ask.read_from_request(Req())
    monkeypatch.setattr(auth, 'allowed_device_owner', lambda tok: None)
    person_ask.read_from_request(Req())
    assert seen == [('a1', 'signed-in'), ('a1', 'phone-owner'), ('a1', None)]


def test_read_ask_gives_the_card_only_to_its_person(legs):
    from integrations.agent_engine import person_ask
    asked = _ask()
    body, code = person_ask.read_ask(asked, OWNER)
    assert code == 200 and body['ask_id'] == asked
    body['title'] = 'changed'                     # a copy: the held card stands
    assert person_ask.read_ask(asked, OWNER)[0]['title'] != 'changed'
    for reader in ('777', None, ''):
        assert person_ask.read_ask(asked, reader) == (GONE, 404)
