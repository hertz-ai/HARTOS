"""A credential ask an agent filed is dropped on every surface once it is
answered, on the desktop card or on the phone.

Review of 41357357e, 22 Nov 2026:

  * MUST 1: agents file credential asks under their own id (core.agent_tools
    request_resource, Request_Resource), but both answers grant with no
    agent, and "answered" was decided by check_consent, whose allow-all step
    covers scope '*' only: 0 drops, so the card stayed on every surface.
  * MUST 2: a node with no Firebase credential never sends the phone the
    drop: central's relay carries no data-only push, so it is not handed
    there, and the warning said "no relay accepted it" when no relay was
    tried.  That is a rule of central's relay (fcm_sync.relay_carries), not
    a failure here; the warning now says so.
  * The consent tests of 41357357e stub _drop_ask; these call the real drop
    and read each leg where it leaves: the person's WAMP topic, SSE, the
    Firebase send and the shell's card store.

Real: request_credential, ConsentService, the consent blueprint, the drop
fan-out (consent_service._drop_ask, person_ask.drop_everywhere,
realtime.on_notification).  Mocked: the legs' far ends (publish_event,
broadcast_sse_safe, send_fcm_push, liquid_ui_service.withdraw_agent_ui).
"""
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

import pytest  # noqa: E402

from tests.unit.test_the_phone_answers_a_credential_ask import (  # noqa: E402,F401 (fixtures)
    ASK, NAME, OWNER, SCOPE, _pushed_request_id, phone, world)

TYPED = 'fake-typed-value-5K'
AGENT = '42'


@pytest.fixture
def legs(monkeypatch):
    """What leaves on each leg of the drop, read at its far end."""
    from core import fcm_sync
    from core.platform import events
    from integrations.agent_engine import liquid_ui_service
    from integrations.social import realtime

    out = {'wamp': [], 'sse': [], 'push': [], 'shell': []}
    monkeypatch.setattr(realtime, 'publish_event',
                        lambda topic, data, user_id='': out['wamp'].append(
                            (topic, dict(data), user_id)))
    monkeypatch.setattr(events, 'broadcast_sse_safe',
                        lambda kind, data, user_id=None: out['sse'].append(
                            (kind, dict(data), user_id)) or True)
    monkeypatch.setattr(fcm_sync, 'send_fcm_push',
                        lambda uid, title, body, data=None, **k: out['push'].append(
                            (uid, title, body, dict(data or {}))) or True)
    monkeypatch.setattr(liquid_ui_service, 'withdraw_agent_ui',
                        lambda msg_id: out['shell'].append(msg_id) or 1)
    monkeypatch.setattr(realtime, '_pushes_allowed', lambda: True)

    class _Now:
        """The push leg runs on the caller's thread here."""

        def __init__(self, target=None, args=(), **kw):
            self._run = lambda: target(*args)

        def start(self):
            self._run()
    from types import SimpleNamespace
    monkeypatch.setattr(realtime, 'threading', SimpleNamespace(Thread=_Now))
    return out


def _dropped(out, ask_id):
    """How many times each leg carried the drop of ``ask_id``."""
    from integrations.agent_engine.person_ask import ANSWERED_TYPE
    return {
        'wamp': sum(1 for topic, d, uid in out['wamp']
                    if topic == 'chat.social' and uid == OWNER
                    and d.get('type') == ANSWERED_TYPE and d.get('ask_id') == ask_id),
        'sse': sum(1 for kind, d, uid in out['sse']
                   if kind == 'notification' and uid == OWNER
                   and d.get('type') == ANSWERED_TYPE and d.get('ask_id') == ask_id),
        'push': sum(1 for uid, title, body, d in out['push']
                    if uid == OWNER and not title and not body
                    and d.get('type') == ANSWERED_TYPE
                    and d.get('request_id') == ask_id),
        'shell': out['shell'].count(ask_id),
    }


ONCE = {'wamp': 1, 'sse': 1, 'push': 1, 'shell': 1}


def test_an_agents_ask_answered_on_the_desktop_card_drops_on_every_leg(world, phone, legs):
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id=AGENT)
    rid = _pushed_request_id(world)

    # The card's Accept: Nunba stores the value in its own vault, then
    # grants the ask's type and scope (no agent, no request_id).
    resp = phone('', {'consent_type': 'credential', 'scope': SCOPE})

    assert resp.status_code == 201
    assert _dropped(legs, rid) == ONCE


def test_an_agents_ask_answered_on_the_phone_drops_on_every_leg(world, phone, legs):
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id=AGENT)
    rid = _pushed_request_id(world)

    resp = phone('', {'consent_type': 'credential', 'request_id': rid,
                      'value': TYPED})

    assert resp.status_code == 201
    assert _dropped(legs, rid) == ONCE
    assert not any(TYPED in repr(x) for leg in legs.values() for x in leg)


def test_a_later_grant_does_not_drop_it_again(world, phone, legs):
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id=AGENT)
    rid = _pushed_request_id(world)
    assert phone('', {'consent_type': 'credential', 'scope': SCOPE}).status_code == 201

    assert phone('', {'consent_type': 'credential', 'scope': SCOPE}).status_code == 201

    assert _dropped(legs, rid) == ONCE


# ── a node with no Firebase credential ───────────────────────────────

def test_with_no_firebase_credential_the_drop_is_not_handed_to_central_and_the_log_says_why(
        monkeypatch, caplog):
    from unittest.mock import MagicMock
    from core import fcm_sync
    from integrations.agent_engine.person_ask import ANSWERED_TYPE
    from integrations.social import realtime
    bus = MagicMock()
    monkeypatch.setattr('core.peer_link.message_bus.get_message_bus', lambda: bus)
    monkeypatch.setattr(fcm_sync, '_fcm_credential', lambda: (None, None))

    with caplog.at_level(logging.WARNING, logger='hevolve_social'):
        realtime._send_push(OWNER, {'data': {'type': ANSWERED_TYPE,
                                             'request_id': 'row-9',
                                             'ask_id': 'row-9'}})

    bus.publish.assert_not_called()
    said = ' '.join(r.getMessage() for r in caplog.records
                    if r.levelno >= logging.WARNING)
    assert "central's relay carries no data-only push" in said
    assert 'no relay accepted it' not in said


def test_a_consent_prompt_still_goes_to_central_when_no_credential_is_here(monkeypatch):
    from unittest.mock import MagicMock
    from core import fcm_sync
    bus = MagicMock()
    bus.get_stats.side_effect = [{'delivered_crossbar': 0},
                                 {'delivered_crossbar': 1}]
    monkeypatch.setattr('core.peer_link.message_bus.get_message_bus', lambda: bus)
    monkeypatch.setattr(fcm_sync, '_fcm_credential', lambda: (None, None))
    monkeypatch.setattr(fcm_sync, 'resolve_central_id', lambda uid: '9000000001')

    assert fcm_sync.send_fcm_push(OWNER, 'Agent X needs your OK', 'Enter it',
                                  data={'type': 'consent_prompt',
                                        'request_id': 'row-9'}) is True
    assert bus.publish.call_args.args[0] == 'task.confirmation'
