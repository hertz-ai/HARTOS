"""A person who dismisses an ask on one device has dismissed it everywhere.

Owner, 2026-11-03: "dismissing a request dismisses and answering in one
client device clears everything".  Measured at HEAD that day: nothing could
close an ask without answering it.  The Nunba overlay's X and the phone's
"Later" only hid the card on that screen, and reading the bell row left the
ask open for 24 h (person_ask.py, "Reading the row ... drops nothing").

person_ask.dismiss_ask closes the ask through the same door and the same
rules as an answer (the caller's verified identity, the person asked only,
while it is open): every surface drops its card (ask.answered on the
person's topic), the bell row is marked read, and the waiting agent wakes
to 'dismissed'.

Real NotificationService, realtime fan-out and in-memory social DB; the
harness of test_person_ask.py (Firebase and WAMP recorded at their
boundaries).

    python -m pytest tests/unit/test_a_dismissed_ask_closes_everywhere.py -q
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.unit.test_person_ask import (  # noqa: E402,F401
    CLIPS, POST, _answered_events, _ask, _no_open_asks, _row, _settle, legs)


def test_a_dismissal_closes_the_ask_on_every_surface(legs):
    from integrations.agent_engine import person_ask
    ask_id = _ask(component=dict(POST))['ask_id']
    got = {}

    def wait():
        t0 = time.monotonic()
        got['answer'] = person_ask.wait_for_answer(ask_id, 5)
        got['waited'] = time.monotonic() - t0

    t = threading.Thread(target=wait)
    t.start()
    time.sleep(0.1)
    body, code = person_ask.dismiss_ask(ask_id, '10202')
    t.join(timeout=6)
    _settle()
    assert (code, body) == (200, {'status': 'ok', 'ask_id': ask_id, 'dismissed': True})
    assert got['answer'] is None and got['waited'] < 2      # the agent wakes now
    assert person_ask.ask_state(ask_id, '10202') == {'status': 'dismissed', 'answer': None}
    assert _row(ask_id)['is_read'] is True                  # the bell stops listing it
    assert len(_answered_events(legs, ask_id)) == 1         # every surface drops it


def test_only_the_person_asked_may_dismiss(legs):
    from integrations.agent_engine import person_ask
    ask_id = _ask(component=dict(POST))['ask_id']
    for who in ('777', None, ''):
        body, code = person_ask.dismiss_ask(ask_id, who)
        assert (code, body['reason']) == (403, 'not_yours'), who
    assert person_ask.ask_state(ask_id, '10202')['status'] == 'open'
    assert _row(ask_id)['is_read'] is False
    assert _answered_events(legs, ask_id) == []


def test_a_dismissed_ask_takes_no_answer_and_an_answered_one_no_dismissal(legs):
    from integrations.agent_engine import person_ask
    gone = _ask(component=dict(POST))['ask_id']
    assert person_ask.dismiss_ask(gone, '10202')[0]['dismissed'] is True
    assert person_ask.answer_ask(gone, {'decision': 'approve'}, '10202') == (
        {'status': 'error', 'reason': 'dismissed'}, 409)
    assert person_ask.dismiss_ask(gone, '10202') == (
        {'status': 'error', 'reason': 'dismissed'}, 409)
    assert person_ask.ask_state(gone, '10202')['status'] == 'dismissed'

    kept = _ask(component={'type': 'approval', 'action': 'another'})['ask_id']
    assert person_ask.answer_ask(kept, {'decision': 'deny'}, '10202')[1] == 200
    assert person_ask.dismiss_ask(kept, '10202') == (
        {'status': 'error', 'reason': 'already_answered', 'answer': {'decision': 'deny'}}, 409)
    assert person_ask.ask_state(kept, '10202') == {
        'status': 'answered', 'answer': {'decision': 'deny'}}


def test_a_dismissed_ask_is_no_longer_open_anywhere(legs):
    """The same question asked again is a new ask, and a dismissed file
    request takes no files."""
    from integrations.agent_engine import person_ask
    first = _ask(component=dict(POST))['ask_id']
    person_ask.dismiss_ask(first, '10202')
    again = _ask(component=dict(POST))['ask_id']
    assert again != first
    clips = _ask(component=dict(CLIPS))['ask_id']
    assert person_ask.open_file_request(clips, '10202') is not None
    person_ask.dismiss_ask(clips, '10202')
    assert person_ask.open_file_request(clips, '10202') is None


def test_dismissing_an_ask_this_node_no_longer_holds_drops_its_card(legs):
    """After a restart the card is still on her devices: her dismissal is told
    it came too late, and every surface drops it."""
    from integrations.agent_engine import person_ask
    ask_id = _ask(component=dict(POST))['ask_id']
    with person_ask._lock:
        person_ask._asks.clear()
    assert person_ask.dismiss_ask(ask_id, '777')[1] == 404
    assert _row(ask_id)['is_read'] is False
    assert person_ask.dismiss_ask(ask_id, '10202') == (
        {'status': 'error', 'reason': 'expired'}, 410)
    assert _row(ask_id)['is_read'] is True
    assert len(_answered_events(legs, ask_id)) == 1


def test_the_approval_door_dismisses_as_the_verified_caller(legs, monkeypatch):
    """/api/agent/approval with ``"dismiss": true`` dismisses, by the
    caller's Bearer identity; any other value of it is no dismissal."""
    import integrations.commerce.approvals as approvals
    from integrations.agent_engine import person_ask
    monkeypatch.setattr(approvals, 'approver_from_request', lambda req: '10202')
    ask_id = _ask(component=dict(POST))['ask_id']
    body, code = person_ask.answer_from_request(object(), {'ask_id': ask_id, 'dismiss': 'yes'})
    assert (code, body['reason']) == (400, 'bad_answer')
    assert person_ask.ask_state(ask_id, '10202')['status'] == 'open'
    body, code = person_ask.answer_from_request(object(), {'ask_id': ask_id, 'dismiss': True})
    assert (code, body.get('dismissed')) == (200, True)
    monkeypatch.setattr(approvals, 'approver_from_request', lambda req: '777')
    monkeypatch.setattr(approvals, 'bearer_token', lambda req: None)
    other = _ask(component={'type': 'approval', 'action': 'another'})['ask_id']
    body, code = person_ask.answer_from_request(object(), {'ask_id': other, 'dismiss': True})
    assert (code, body['reason']) == (403, 'not_yours')


def test_the_agent_reads_dismissed_back(legs):
    from integrations.agent_engine import person_ask
    _name, _desc, ask_person = person_ask.ask_person_tool('10202', '342')
    ask_id = _ask(component=dict(POST))['ask_id']
    person_ask.dismiss_ask(ask_id, '10202')
    reply = json.loads(ask_person(component='', ask_id=ask_id))
    assert reply['answered'] is False and reply['status'] == 'dismissed'
    assert reply['answer'] is None
