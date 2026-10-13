"""publish_agent_message carries a reel, and can keep it off central's crossbar.

Reel Studio (plan WS-8a step 7, decision U4(d)).  A finished reel reaches the
phone as an agent message whose ``media`` lists it (plan 4.2(f)).  Such a push
names her clients' faces and her captions, so it must not travel to central's
crossbar: ``skip_crossbar=True`` keeps it on this node's own legs (LOCAL, SSE
and her own devices over PeerLink, which is how the phone receives EVENTS).

The HARTOS publish_async that publish_agent_message resolves takes no
skip_crossbar, and Nunba wraps it as (topic, message, timeout), so the
local-only push goes through the MessageBus's own skip_crossbar instead.

Boundaries mocked: the hart_intelligence publish_async (as safe_hartos_attr
resolves it) and the MessageBus legs.  Everything else is real.
"""
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.peer_link import message_bus as mb
from core.peer_link.crossbar_publish import publish_agent_message

XID = 'ab' * 32
MEDIA = [{'kind': 'video', 'xid': XID, 'sha256': XID, 'mime': 'video/mp4',
          'size': 1234, 'reel_id': 'r1', 'prompt_id': '42', 'request_id': 'rid-1',
          'caption': 'Bridal look', 'hashtags': ['#bridal']}]

#: The envelope publish_agent_message sends today, for a message with no media.
TODAY = {'text': ['Your reel is ready'], 'request_id': 'rid-1', 'prompt_id': '42',
         'bot_type': 'Custom GPT', 'options': [], 'newoptions': [],
         'page_image_url': '', 'analogy_image_url': '', 'probe': False, 'inp': ''}


@pytest.fixture
def publisher(monkeypatch):
    pub = MagicMock(name='publish_async')
    monkeypatch.setitem(sys.modules, 'hart_intelligence',
                        SimpleNamespace(publish_async=pub))
    return pub


@pytest.fixture
def bus(monkeypatch):
    """A real MessageBus whose four legs are recorded instead of sent."""
    b = mb.MessageBus()
    legs = {name: MagicMock(name=name) for name in
            ('_route_local', '_route_sse', '_route_peerlink', '_route_crossbar')}
    for name, fake in legs.items():
        monkeypatch.setattr(b, name, fake)
    monkeypatch.setattr(mb, '_bus', b)
    b.legs = legs
    return b


def _sent(pub):
    assert pub.call_count == 1, pub.call_args_list
    topic, payload = pub.call_args[0][0], pub.call_args[0][1]
    return topic, (json.loads(payload) if isinstance(payload, str) else payload)


def test_without_media_the_envelope_is_todays(publisher):
    assert publish_agent_message(text='Your reel is ready', user_id='u7',
                                 request_id='rid-1', prompt_id='42') is True
    topic, payload = _sent(publisher)
    assert topic == 'com.hertzai.hevolve.chat.u7'
    assert payload == TODAY


def test_media_rides_the_message(publisher):
    assert publish_agent_message(text='Your reel is ready', user_id='u7',
                                 request_id='rid-1', prompt_id='42',
                                 media=MEDIA) is True
    _, payload = _sent(publisher)
    assert payload['media'] == MEDIA
    assert {k: v for k, v in payload.items() if k != 'media'} == TODAY


def test_media_without_text_is_not_sent(publisher, bus):
    """A push always carries words (plan finding 28): a bare video is refused."""
    for skip in (False, True):
        assert publish_agent_message(text='', user_id='u7', request_id='r',
                                     prompt_id='42', media=MEDIA,
                                     skip_crossbar=skip) is False
    publisher.assert_not_called()
    bus.legs['_route_local'].assert_not_called()


def test_skip_crossbar_keeps_the_push_off_centrals_crossbar(publisher, bus):
    assert publish_agent_message(text='Your reel is ready', user_id='u7',
                                 request_id='rid-1', prompt_id='42',
                                 media=MEDIA, skip_crossbar=True) is True
    # Not through publish_async: its HTTP leg is the one that reaches central.
    publisher.assert_not_called()
    bus.legs['_route_crossbar'].assert_not_called()
    # The phone's leg (her own devices over PeerLink EVENTS) and this node's
    # own subscribers still get it, on the chat topic the phone listens to.
    peer = bus.legs['_route_peerlink']
    assert peer.call_count == 1
    topic, data = peer.call_args[0][0], peer.call_args[0][1]
    assert topic == 'chat.response'
    assert data['media'] == MEDIA and data['text'] == ['Your reel is ready']
    assert data['user_id'] == 'u7'
    assert peer.call_args.kwargs.get('owner_user_id') == 'u7'
    bus.legs['_route_local'].assert_called_once()


def test_without_skip_crossbar_the_publish_async_path_is_unchanged(publisher, bus):
    publish_agent_message(text='hi', user_id='u7', request_id='r', prompt_id='42',
                          media=MEDIA)
    assert publisher.call_count == 1
    bus.legs['_route_peerlink'].assert_not_called()


def test_a_failing_bus_reports_false(monkeypatch, publisher):
    broken = SimpleNamespace(publish=MagicMock(side_effect=RuntimeError('bus down')))
    monkeypatch.setattr(mb, 'get_message_bus', lambda: broken)
    assert publish_agent_message(text='hi', user_id='u7', request_id='r',
                                 prompt_id='42', skip_crossbar=True) is False


def test_media_entries_that_are_not_objects_are_dropped(publisher):
    publish_agent_message(text='hi', user_id='u7', request_id='r', prompt_id='42',
                          media=[MEDIA[0], 'junk', None])
    _, payload = _sent(publisher)
    assert payload['media'] == MEDIA
