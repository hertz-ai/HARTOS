"""An answered ask is closed; one sent again after its answer is open again.

Reviews of 677a4c795 (MUST 1) and 41357357e (MUST 3), 22 Nov 2026:

  * the phone answered the same request_id twice and both answers were taken
    (201, 201): the second value replaced the first in the vault, and the ask
    row still read "never granted", so it could be answered for ever;
  * for the same reason every later grant of a consent type dropped every
    earlier ask of it again (grants A, B, C gave 1, 2, 3 drops).

An ask is open only while no yes has settled it since it was last sent
(consent_service.open_ask).  A credential asked again after the site refused
its value (request_credential with "rejected", request_consent reask) is sent
under the same row, and that row is open again.

Real: request_credential, ConsentService, the vault and the consent blueprint
on an in-memory database (the fixtures of
test_the_phone_answers_a_credential_ask.py).  Mocked: the ask's delivery
(consent_service._emit), and in the drop counts the drop fan-out
(consent_service._drop_ask, the fixture of
test_an_answered_ask_drops_on_the_phone.py).
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

from tests.unit.test_the_phone_answers_a_credential_ask import (  # noqa: E402,F401 (fixtures)
    ASK, NAME, OWNER, SCOPE, _grants, _pushed_request_id, _value, phone, world)
from tests.unit.test_an_answered_ask_drops_on_the_phone import (  # noqa: E402,F401 (fixture)
    UID, _ask, db)

FIRST = 'first-fake-value-11'
SECOND = 'second-fake-value-22'
REJECTED = json.dumps(dict(json.loads(ASK), rejected=True))


def _answer(phone, rid, value):
    return phone('', {'consent_type': 'credential', 'request_id': rid,
                      'value': value})


def _ask_row():
    from integrations.social.models import UserConsent, db_session
    with db_session() as s:
        row = s.query(UserConsent).filter(
            UserConsent.user_id == OWNER, UserConsent.scope == SCOPE,
            UserConsent.granted_at.is_(None)).one()
        s.expunge(row)
        return row


# ── the phone answers an ask once ─────────────────────────────────────

def test_an_answered_request_id_is_not_taken_a_second_time(world, phone):
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id=None)
    rid = _pushed_request_id(world)
    assert _answer(phone, rid, FIRST).status_code == 201

    again = _answer(phone, rid, SECOND)

    assert again.status_code == 404
    assert _value() == FIRST                  # the vault keeps the first value
    assert len(_grants()) == 1


def test_a_no_to_an_answered_ask_is_refused_and_the_yes_stands(world, phone):
    from hartos.ai_key_vault import request_credential
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import db_session
    request_credential(ASK, agent_id=None)
    rid = _pushed_request_id(world)
    assert _answer(phone, rid, FIRST).status_code == 201

    no = phone('/decline', {'consent_type': 'credential', 'request_id': rid})

    assert no.status_code == 404
    with db_session() as s:
        assert ConsentService.check_consent(s, OWNER, 'credential', scope=SCOPE)
    assert _value() == FIRST


def test_a_credential_asked_again_after_its_value_was_refused_is_open_again(world, phone):
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id=None)
    rid = _pushed_request_id(world)
    assert _answer(phone, rid, FIRST).status_code == 201

    request_credential(REJECTED, agent_id=None)      # the site refused it

    assert _pushed_request_id(world) == rid          # the same row, sent again
    assert _answer(phone, rid, SECOND).status_code == 201
    assert _value() == SECOND
    assert _answer(phone, rid, 'third-fake-value-33').status_code == 404
    assert _value() == SECOND


def test_an_unanswered_ask_sent_again_is_not_written(world):
    """A polling gate re-sends its ask every few seconds: only an ask a yes
    had settled is written when it is sent again."""
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id=None)
    before = _ask_row().updated_at

    request_credential(ASK, agent_id=None)
    request_credential(ASK, agent_id=None)

    assert _ask_row().updated_at == before
    assert len([t for t, _d, _m in world if t == 'consent.request']) == 3


# ── a grant drops only the asks it answers, once ──────────────────────

def test_each_grant_drops_only_the_ask_it_answers(db):
    from integrations.social.consent_service import ConsentService
    asks = {s: _ask(db, 'credential', scope='secret:' + s) for s in 'ABC'}

    drops = []
    for s in 'ABC':
        ConsentService.grant_consent(db, UID, 'credential', scope='secret:' + s)
        db.commit()
        drops.append(list(db.dropped))
        db.dropped.clear()

    assert drops == [[(UID, asks[s].id)] for s in 'ABC']


def test_a_second_grant_for_an_answered_ask_drops_nothing(db):
    from integrations.social.consent_service import ConsentService
    ask = _ask(db, 'credential', scope='secret:MAILBOX')
    ConsentService.grant_consent(db, UID, 'credential', scope='secret:MAILBOX')
    db.commit()
    assert db.dropped == [(UID, ask.id)]
    db.dropped.clear()

    ConsentService.grant_consent(db, UID, 'credential', scope='secret:MAILBOX')
    db.commit()

    assert db.dropped == []


def test_an_ask_sent_again_after_its_grant_drops_again_on_its_next_grant(db):
    from integrations.social.consent_service import ConsentService
    ask = _ask(db, 'credential', scope='secret:MAILBOX')
    ConsentService.grant_consent(db, UID, 'credential', scope='secret:MAILBOX')
    db.commit()
    db.dropped.clear()

    again = ConsentService.request_consent(db, UID, 'credential',
                                           scope='secret:MAILBOX', reask=True)
    db.commit()
    assert again.id == ask.id
    ConsentService.grant_consent(db, UID, 'credential', scope='secret:MAILBOX')
    db.commit()

    assert db.dropped == [(UID, ask.id)]
