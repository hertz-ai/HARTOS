"""An agent card reaches its owner's phone, and their desktop once.

Measured 2026-10-22 (task #914).  Every card a LiquidUIService accepts is
emitted as ``agent.ui.update``, which the EventBus never bridges to WAMP: it
is one person's event and com.hartos.event.agent.ui.update names no user
(test_liquid_ui_wamp_egress.py).  The phone listens on the user's own social
topic, com.hertzai.hevolve.social.<user_id> (AutobahnConnectionManager
.onEventSocial -> AgentUiEnvelope), and only push_agent_ui's callers
(commerce, AP2) published there.  So no qr_pair, pair_code, oauth_link, form,
approval, media, meet_copilot or notification card ever reached the phone.
The cards that did take both legs reached the desktop twice: Nunba's
realtimeService.unwrapAgentUiEnvelope names the agent.ui.update copy by its
envelope's msg_id (EventBus's own uuid) and the chat.social copy by its
card's msg_id, which was absent, plus a push time push_agent_ui stamped
separately.

Contract pinned here:
  * a card the gates accept, whose owner is known, is published once on the
    owner's chat.social topic as {type: 'agent_ui_update', agent_id,
    component}: MessageBus carries it to WAMP com.hertzai.hevolve.social
    .<owner> and to the owner's SSE event chat.social;
  * every copy names the card by one id: the agent.ui.update envelope's
    msg_id is the card's own msg_id, so a client drops the second copy;
  * each push is its own card, with its own id;
  * a card with no known owner, or one the gates refuse, goes nowhere new;
  * push_agent_ui with a shell in this process publishes nothing itself, the
    shell does, once, and only what its gates accept.

Real EventBus, LiquidUIService, MessageBus and realtime.publish_event.
Mocked at the boundaries only: the SSE transport and the Crossbar HTTP
transport (both recorded), the audit DB, the hive breaker, the owner lookup
and the bus's PeerLink leg.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from core.platform.events import EventBus
from core.platform.registry import get_registry, reset_registry

SOCIAL = 'com.hertzai.hevolve.social.'


class _Legs:
    def __init__(self):
        self.sse = []        # (event_type, data, user_id), as sent
        self.crossbar = []   # (uri, payload)


@pytest.fixture()
def legs(tmp_path, monkeypatch):
    monkeypatch.setenv('HEVOLVE_DATA_DIR', str(tmp_path))
    reset_registry()
    from core.peer_link.message_bus import MessageBus
    ebus = EventBus()
    mbus = MessageBus()
    # No PeerLink in a unit test: this bus's leg to linked devices is off.
    mbus._route_peerlink = lambda *a, **k: None
    rec = _Legs()
    mbus.set_http_transport(
        lambda uri, payload: rec.crossbar.append((uri, json.loads(payload))))

    def _sse(event_type, data, user_id=None):
        rec.sse.append((event_type, json.loads(json.dumps(data, default=str)),
                        user_id))
        return True

    with patch('core.platform.events.broadcast_sse_safe', _sse), \
            patch('core.platform.events.emit_event',
                  side_effect=lambda t, d=None, async_=True: ebus.emit(t, d)), \
            patch('core.peer_link.message_bus.get_message_bus',
                  return_value=mbus), \
            patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
                  return_value=False), \
            patch('security.immutable_audit_log.get_audit_log',
                  return_value=MagicMock()), \
            patch('core.event_attribution.owner_user_id', return_value=None):
        yield rec
    reset_registry()


def _shell():
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    svc = LiquidUIService(a2ui_enabled=True)
    svc._register_self()
    return svc


def _on_social(rec, user_id):
    return [p for u, p in rec.crossbar if u == SOCIAL + user_id]


def _card_ids(rec):
    """The id each copy of a card is known by, read the way the clients read
    it (Nunba realtimeService.unwrapAgentUiEnvelope, the phone's
    AgentUiEnvelope): the envelope's msg_id on agent.ui.update, the card's
    own msg_id on the social topic's agent_ui_update."""
    ids = []
    for event_type, data, _ in rec.sse:
        if event_type == 'agent.ui.update':
            ids.append(data.get('msg_id'))
        elif event_type == 'chat.social' and data.get('type') == 'agent_ui_update':
            ids.append(data['component'].get('msg_id'))
    for _, payload in rec.crossbar:
        if payload.get('type') == 'agent_ui_update':
            ids.append(payload['component'].get('msg_id'))
    return ids


LINKED = {'type': 'notification', 'title': 'Linked',
          'message': 'WhatsApp is linked'}


def test_an_accepted_card_reaches_its_owners_own_topic(legs):
    svc = _shell()
    assert svc.agent_ui_update('channels', dict(LINKED), user_id='u-1') is True

    sent = _on_social(legs, 'u-1')
    assert len(sent) == 1, legs.crossbar
    assert sent[0]['type'] == 'agent_ui_update'
    assert sent[0]['agent_id'] == 'channels'
    assert sent[0]['component']['type'] == 'notification'
    assert sent[0]['component']['message'] == 'WhatsApp is linked'
    # ...on that topic and no other.
    assert [u for u, _ in legs.crossbar] == [SOCIAL + 'u-1']
    # The owner's own SSE stream gets the same envelope.
    assert [uid for e, _, uid in legs.sse if e == 'chat.social'] == ['u-1']


def test_every_copy_names_the_card_by_one_id(legs):
    svc = _shell()
    svc.agent_ui_update('channels', dict(LINKED), user_id='u-1')
    ids = _card_ids(legs)
    # SSE agent.ui.update, SSE chat.social, WAMP social: three copies.
    assert len(ids) == 3, legs.sse
    assert ids[0] and len(set(ids)) == 1, ids


def test_each_push_is_its_own_card(legs):
    """A caller that pushes the same dict twice shows two cards, as before:
    the desktop must not drop the second as a copy of the first."""
    svc = _shell()
    card = dict(LINKED)
    assert svc.agent_ui_update('channels', card, user_id='u-1')
    assert svc.agent_ui_update('channels', card, user_id='u-1')
    sent = _on_social(legs, 'u-1')
    assert len(sent) == 2
    assert sent[0]['component']['msg_id'] != sent[1]['component']['msg_id']


def test_a_card_with_no_known_owner_goes_nowhere_new(legs):
    """No user named and none resolved (a node with more than one person):
    the SSE leg refuses it as before, and there is no owner's topic to use."""
    svc = _shell()
    assert svc.agent_ui_update('system', {
        'type': 'notification', 'title': 'Capability Ready',
        'message': 'the model is ready'}) is True
    assert legs.crossbar == []
    assert legs.sse == []


@pytest.mark.parametrize('card', [
    {'type': 'no_such_type'},
    {'type': 'notification', 'message': '<script>alert(1)</script>'},
])
def test_a_card_the_gates_refuse_is_published_nowhere(legs, card):
    svc = _shell()
    assert svc.agent_ui_update('a', card, user_id='u-1') is False
    assert legs.crossbar == [] and legs.sse == []


def test_a_halted_hive_publishes_nothing(legs):
    svc = _shell()
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=True):
        assert svc.agent_ui_update('a', dict(LINKED), user_id='u-1') is False
    assert legs.crossbar == [] and legs.sse == []


def test_a_pair_code_travels_only_on_its_owners_own_uri(legs):
    """A WhatsApp pair_code card holds the live linking code.  It may leave
    the node only on a URI that is its owner's own, by the one egress rule;
    every SSE copy is addressed to the owner."""
    from security.edge_privacy import crossbar_leg_is_users_own
    code = 'K7Q2-9XPL'
    svc = _shell()
    assert svc.agent_ui_update('u-1', {
        'type': 'pair_code', 'channel': 'whatsapp', 'channel_type': 'whatsapp',
        'code': code, 'clipboard_payload': code,
        'deeplink': 'whatsapp://link?code=' + code,
        'instructions': 'enter ' + code}, user_id='u-1') is True

    carrying = [u for u, p in legs.crossbar if code in json.dumps(p)]
    assert carrying == [SOCIAL + 'u-1']
    assert all(crossbar_leg_is_users_own(u, 'u-1') for u in carrying)
    assert {uid for _, d, uid in legs.sse if code in json.dumps(d)} == {'u-1'}


CART = {'type': 'cart', 'items': [], 'total': 0, 'currency': 'INR'}


def test_push_agent_ui_with_a_shell_reaches_the_owner_once_under_one_id(legs):
    """Commerce and AP2 cards: the shell publishes to the owner's topic, so
    push_agent_ui must not publish the same card there again, and the copies
    must carry one id (they reached the desktop twice)."""
    from integrations.agent_engine.liquid_ui_service import push_agent_ui
    _shell()
    assert push_agent_ui('mcgroce', dict(CART), user_id='mcg-1') is True
    assert len(_on_social(legs, 'mcg-1')) == 1, legs.crossbar
    ids = _card_ids(legs)
    assert len(ids) == 3 and len(set(ids)) == 1, ids


def test_a_card_the_shell_refuses_is_not_published_around_it(legs):
    """The shell's gates decide for every leg: a card it refuses (here its
    per-agent rate cap) does not reach the phone by another way."""
    from integrations.agent_engine.liquid_ui_service import push_agent_ui
    svc = _shell()
    with patch.object(svc, '_a2ui_rate_ok', return_value=False):
        assert push_agent_ui('mcgroce', dict(CART), user_id='mcg-1') is False
    assert legs.crossbar == [] and legs.sse == []


def test_push_agent_ui_without_a_shell_still_reaches_the_owner(legs):
    """A node that serves no shell (the cloud gateway behind the McGroce
    embed) has only the owner's topic."""
    from integrations.agent_engine.liquid_ui_service import push_agent_ui
    assert get_registry().get_or_none('LiquidUIService') is None
    assert push_agent_ui('mcgroce', dict(CART), user_id='mcg-1') is True
    sent = _on_social(legs, 'mcg-1')
    assert len(sent) == 1 and sent[0]['component']['type'] == 'cart'
