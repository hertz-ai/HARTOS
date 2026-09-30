"""Egress scrub at the ownership boundary (owner ruling 2026-09-26).

The ruling, answering "which outgoing messages count as going to third
parties?": "the ones which actually go to regional nodes ... hive nodes
usage that's not their node".  So a MessageBus publish is scrubbed only on
the legs that reach a node or subscriber the MESSAGE'S USER does not own,
and only in the user-content fields; ids, urls and numbers the protocol
needs travel byte-identical.  The user's own devices, own node, LOCAL and
SSE receive the record raw (standing rule: local records raw, scrub only
egress).

Before this, publish() imported ``security.dlp_engine.redact_pii`` (which
does not exist), swallowed the ImportError, and sent everything raw on
every leg.

Every test drives the real ``MessageBus.publish`` and the real
``PeerLinkManager.broadcast`` / ``PeerLink.owned_by``; the boundaries mocked
are the socket send, the Crossbar HTTP transport and the SSE broker.
"""
import json
import logging
import os
import sys
from unittest.mock import patch

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import pytest  # noqa: E402

from core.peer_link import link_manager as lm_mod  # noqa: E402
from core.peer_link.link import LinkState, PeerLink, TrustLevel  # noqa: E402
from core.peer_link.message_bus import (  # noqa: E402
    MessageBus, RELAY_TOPIC, reset_message_bus)
from security.edge_privacy import scrub_for_egress  # noqa: E402

NODE_OWNER = 'owner-1'      # the user this node belongs to (HEVOLVE_USER_ID)
OTHER_USER = 'u-42'          # a user this node serves who does not own it

EMAIL = 'alice@example.com'
PHONE = '555-123-4567'
PROMPT_ID = '9876543210'     # 10 digits: the phone pattern would eat it
PEER_URL = 'http://203.0.113.7:6777/api'   # the ip pattern would eat it


def _chat_body(user_id):
    """A chat.new-shaped row plus the chat-bubble fields that ride the bus."""
    return {
        'user_id': user_id,
        'prompt_id': PROMPT_ID,
        'request_id': 'req-1',
        'peer_url': PEER_URL,
        'content': f'mail me at {EMAIL} or call {PHONE}',
        'text': [f'reach {EMAIL}'],
        'nested': {'message': f'ring {PHONE}', 'id': PROMPT_ID},
    }


def _assert_ids_intact(d, user_id):
    assert d['user_id'] == user_id
    assert d['prompt_id'] == PROMPT_ID
    assert d['request_id'] == 'req-1'
    assert d['peer_url'] == PEER_URL
    assert d['nested']['id'] == PROMPT_ID


def _assert_scrubbed(d):
    assert EMAIL not in d['content'] and PHONE not in d['content']
    assert '[EMAIL_REDACTED]' in d['content']
    assert '[PHONE_REDACTED]' in d['content']
    assert d['text'] == ['reach [EMAIL_REDACTED]']
    assert d['nested']['message'] == 'ring [PHONE_REDACTED]'


def _assert_raw(d):
    assert d['content'] == f'mail me at {EMAIL} or call {PHONE}'
    assert d['text'] == [f'reach {EMAIL}']
    assert d['nested']['message'] == f'ring {PHONE}'


def _link(peer_id, trust, kind='node', user_id=''):
    link = PeerLink(peer_id, '10.0.0.9:6777', trust)
    link.kind = kind
    link.user_id = user_id
    link._state = LinkState.CONNECTED
    link.sent = []
    link.send = lambda channel, data, **kw: link.sent.append((channel, data))
    return link


@pytest.fixture
def mgr(monkeypatch):
    monkeypatch.setenv('HEVOLVE_USER_ID', NODE_OWNER)
    lm_mod.reset_link_manager()
    manager = lm_mod.get_link_manager()
    manager._links = {}
    monkeypatch.setattr(lm_mod, 'get_link_manager', lambda: manager)
    yield manager
    lm_mod.reset_link_manager()


@pytest.fixture
def bus():
    reset_message_bus()
    b = MessageBus()
    yield b
    reset_message_bus()


def _add(manager, link):
    manager._links[link.peer_id] = link
    return link


# ── edge_privacy.scrub_for_egress: the one egress scrub ─────────────────────

def test_scrub_for_egress_scrubs_content_keeps_ids_and_returns_a_copy():
    original = _chat_body(OTHER_USER)
    before = json.dumps(original, sort_keys=True)
    out = scrub_for_egress(original)
    _assert_scrubbed(out)
    _assert_ids_intact(out, OTHER_USER)
    assert json.dumps(original, sort_keys=True) == before   # never mutates


def test_scrub_for_egress_leaves_non_strings_under_content_keys():
    out = scrub_for_egress({'content': 5551234567, 'text': [None, 3, EMAIL]})
    assert out == {'content': 5551234567, 'text': [None, 3, '[EMAIL_REDACTED]']}


# ── PeerLink.owned_by: whose is the far end ────────────────────────────────

def test_owned_by_names_each_links_owner(monkeypatch):
    monkeypatch.setenv('HEVOLVE_USER_ID', NODE_OWNER)
    node = _link('n', TrustLevel.SAME_USER)
    peer = _link('p', TrustLevel.PEER)
    relay = _link('r', TrustLevel.RELAY)
    phone = _link('d', TrustLevel.SAME_USER, kind='device', user_id=OTHER_USER)
    assert node.owned_by(NODE_OWNER) and node.owned_by('')
    assert not node.owned_by(OTHER_USER)
    assert not peer.owned_by(NODE_OWNER) and not relay.owned_by(NODE_OWNER)
    assert phone.owned_by(OTHER_USER)
    assert not phone.owned_by(NODE_OWNER) and not phone.owned_by('')


def test_owned_by_without_a_provable_identity_owns_nothing_for_a_user(monkeypatch):
    """No HEVOLVE_USER_ID: this node cannot say whose its SAME_USER links
    are, so a named user's message treats them as third parties."""
    monkeypatch.setenv('HEVOLVE_USER_ID', '')
    node = _link('n', TrustLevel.SAME_USER)
    assert not node.owned_by(OTHER_USER)


# ── PeerLink leg ───────────────────────────────────────────────────────────

def test_other_users_message_is_scrubbed_to_the_node_owners_links(bus, mgr):
    """This node serves OTHER_USER; its SAME_USER node links belong to
    NODE_OWNER, so they are third parties for this message.  OTHER_USER's
    own phone gets it raw."""
    owners_node = _add(mgr, _link('node-b', TrustLevel.SAME_USER))
    users_phone = _add(mgr, _link('dev-u42', TrustLevel.SAME_USER,
                                  kind='device', user_id=OTHER_USER))
    with patch('core.platform.events.broadcast_sse_safe', return_value=False):
        bus.publish('chat.new', _chat_body(OTHER_USER), user_id=OTHER_USER,
                    skip_crossbar=True)

    assert len(owners_node.sent) == 1
    third = owners_node.sent[0][1]['data']
    _assert_scrubbed(third)
    _assert_ids_intact(third, OTHER_USER)

    assert len(users_phone.sent) == 1
    own = users_phone.sent[0][1]['data']
    _assert_raw(own)
    _assert_ids_intact(own, OTHER_USER)


def test_node_owners_message_to_own_node_link_is_untouched(bus, mgr):
    own_node = _add(mgr, _link('node-b', TrustLevel.SAME_USER))
    own_phone = _add(mgr, _link('dev-o', TrustLevel.SAME_USER,
                                kind='device', user_id=NODE_OWNER))
    with patch('core.platform.events.broadcast_sse_safe', return_value=False):
        bus.publish('chat.new', _chat_body(NODE_OWNER), user_id=NODE_OWNER,
                    skip_crossbar=True)
    for link in (own_node, own_phone):
        assert len(link.sent) == 1
        _assert_raw(link.sent[0][1]['data'])
        _assert_ids_intact(link.sent[0][1]['data'], NODE_OWNER)


def test_signed_fleet_command_reaches_peers_byte_identical(bus, mgr):
    """The relayed topic carries a signature over its data; a scrub would
    make every hop drop it.  It is node authority, not user content."""
    peer = _add(mgr, _link('peer-x', TrustLevel.PEER))
    cmd = {'cmd_type': 'tts_stream', 'signature': 'ab' * 32,
           'issued_by': 'central0', 'params': {'text': f'call {PHONE}'}}
    expected = json.dumps(cmd, sort_keys=True)
    with patch('core.platform.events.broadcast_sse_safe', return_value=False):
        bus.publish(RELAY_TOPIC, dict(cmd), skip_crossbar=True)
    assert len(peer.sent) == 1
    assert json.dumps(peer.sent[0][1]['data'], sort_keys=True) == expected


def test_signed_fleet_command_naming_another_user_is_not_split(bus, mgr):
    """ui_commands publishes fleet.command with a user_id; even when that
    user does not own this node, the relayed topic is sent unaltered."""
    peer = _add(mgr, _link('peer-x', TrustLevel.PEER))
    cmd = {'cmd_type': 'tts_stream', 'signature': 'ab' * 32,
           'params': {'text': f'call {PHONE}'}, 'message': f'mail {EMAIL}'}
    with patch('core.platform.events.broadcast_sse_safe', return_value=False):
        bus.publish(RELAY_TOPIC, dict(cmd), user_id=OTHER_USER,
                    skip_crossbar=True)
    assert len(peer.sent) == 1
    got = dict(peer.sent[0][1]['data'])
    assert got.pop('user_id') == OTHER_USER
    assert json.dumps(got, sort_keys=True) == json.dumps(cmd, sort_keys=True)


def test_signed_fleet_command_on_crossbar_is_not_scrubbed(bus):
    calls = _crossbar_calls(bus)
    cmd = {'cmd_type': 'tts_stream', 'signature': 'ab' * 32,
           'message': f'mail {EMAIL}'}
    with patch('core.platform.events.broadcast_sse_safe', return_value=False):
        bus.publish(RELAY_TOPIC, dict(cmd), device_id='node-9',
                    skip_peerlink=True)
    assert len(calls) == 1
    assert calls[0][0] == 'com.hertzai.hevolve.fleet.node-9'
    sent = json.loads(calls[0][1])
    assert sent['message'] == f'mail {EMAIL}'
    assert sent['signature'] == 'ab' * 32


# ── Crossbar leg ───────────────────────────────────────────────────────────

def _crossbar_calls(bus):
    calls = []
    bus.set_http_transport(lambda topic, payload: calls.append((topic, payload)))
    return calls


def test_crossbar_topic_other_people_subscribe_to_is_scrubbed(bus):
    """community.message fans out to the community's members."""
    calls = _crossbar_calls(bus)
    body = dict(_chat_body(OTHER_USER), community_id='c-1')
    with patch('core.platform.events.broadcast_sse_safe', return_value=False):
        bus.publish('community.message', body, skip_peerlink=True)
    assert len(calls) == 1
    assert calls[0][0] == 'com.hertzai.hevolve.community.c-1'
    sent = json.loads(calls[0][1])
    _assert_scrubbed(sent)
    _assert_ids_intact(sent, OTHER_USER)
    assert sent['community_id'] == 'c-1'


def test_users_own_crossbar_topic_is_untouched(bus):
    calls = _crossbar_calls(bus)
    with patch('core.platform.events.broadcast_sse_safe', return_value=False):
        bus.publish('chat.social', _chat_body(OTHER_USER), user_id=OTHER_USER,
                    skip_peerlink=True)
    assert len(calls) == 1
    assert calls[0][0] == f'com.hertzai.hevolve.social.{OTHER_USER}'
    sent = json.loads(calls[0][1])
    _assert_raw(sent)
    _assert_ids_intact(sent, OTHER_USER)


# ── LOCAL + SSE stay raw even when a leg is scrubbed ───────────────────────

def test_local_and_sse_receive_raw_while_third_party_legs_are_scrubbed(bus, mgr):
    _add(mgr, _link('node-b', TrustLevel.SAME_USER))
    calls = _crossbar_calls(bus)
    local, sse = [], []
    bus.subscribe('community.message', lambda t, d: local.append(d))

    def _sse(event_type, data, user_id=None):
        sse.append(data)
        return True

    body = dict(_chat_body(OTHER_USER), community_id='c-1')
    with patch('core.platform.events.broadcast_sse_safe', _sse):
        bus.publish('community.message', body, user_id=OTHER_USER)
    assert len(local) == 1 and len(sse) == 1
    _assert_raw(local[0])
    _assert_raw(sse[0])
    _assert_scrubbed(json.loads(calls[0][1]))


# ── a scrub that fails withholds only the third-party legs ─────────────────

class _BrokenDLP:
    def redact(self, text):
        raise RuntimeError('dlp broken')


def test_dlp_failure_drops_third_party_legs_and_keeps_own_legs(bus, mgr, caplog):
    owners_node = _add(mgr, _link('node-b', TrustLevel.SAME_USER))
    users_phone = _add(mgr, _link('dev-u42', TrustLevel.SAME_USER,
                                  kind='device', user_id=OTHER_USER))
    calls = _crossbar_calls(bus)
    local = []
    bus.subscribe('community.message', lambda t, d: local.append(d))
    body = dict(_chat_body(OTHER_USER), community_id='c-1')
    with patch('security.dlp_engine.get_dlp_engine', return_value=_BrokenDLP()), \
            patch('core.platform.events.broadcast_sse_safe', return_value=False), \
            caplog.at_level(logging.WARNING, logger='hevolve_security'):
        bus.publish('community.message', body, user_id=OTHER_USER)

    assert calls == []                       # third-party Crossbar leg withheld
    assert owners_node.sent == []            # third-party PeerLink link withheld
    assert len(users_phone.sent) == 1        # the user's own device still served
    _assert_raw(users_phone.sent[0][1]['data'])
    assert len(local) == 1                   # LOCAL untouched
    assert bus.get_stats()['egress_withheld'] == 2
    assert any('dlp broken' in r.getMessage() for r in caplog.records)


def test_dlp_failure_does_not_touch_an_all_own_publish(bus, mgr):
    """No third party on any leg: the scrub is never consulted."""
    own_node = _add(mgr, _link('node-b', TrustLevel.SAME_USER))
    calls = _crossbar_calls(bus)
    with patch('security.dlp_engine.get_dlp_engine', return_value=_BrokenDLP()), \
            patch('core.platform.events.broadcast_sse_safe', return_value=False):
        bus.publish('chat.social', _chat_body(NODE_OWNER), user_id=NODE_OWNER)
    assert len(own_node.sent) == 1 and len(calls) == 1
    _assert_raw(json.loads(calls[0][1]))
    assert bus.get_stats()['egress_withheld'] == 0
