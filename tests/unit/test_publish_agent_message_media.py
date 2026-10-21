"""publish_agent_message carries a reel, and can keep it off central's crossbar.

Reel Studio (plan WS-8a step 7, decision U4(d)).  A finished reel reaches the
phone as an agent message whose ``media`` lists it (plan 4.2(f)).  Such a push
names her clients' faces and her captions, so it must not travel to central's
crossbar: ``skip_crossbar=True`` keeps it on this node's own legs (LOCAL, SSE
and her own devices over PeerLink, which is how the phone receives EVENTS).

publish_async is the one publisher (amendment A9 e): the flag is ITS flag, so
the push still gets every stamp and leg publish_async gives a chat message
(served_by, node_tier, the task.confirmation copy, the desktop's SSE leg) and
loses only the HTTP crossbar leg.  The flag is passed only when set, so every
other caller reaches any wrapper around publish_async unchanged.  Nunba's
thinking-trace wrapper forwards the flag when set; an older Nunba's takes
(topic, message, timeout) only, and a reel push behind it fails closed.

Boundaries mocked: the hart_intelligence publish_async (as safe_hartos_attr
resolves it), or -- for the real publish_async -- the MessageBus legs, the SSE
fan-out and the crossbar HTTP executor.  Everything else is real.
"""
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.peer_link import message_bus as mb
from core.peer_link.crossbar_publish import publish_agent_message
from tests.conftest import refuse_all_network

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


def test_skip_crossbar_is_publish_asyncs_own_flag(publisher, bus):
    assert publish_agent_message(text='Your reel is ready', user_id='u7',
                                 request_id='rid-1', prompt_id='42',
                                 media=MEDIA, skip_crossbar=True) is True
    topic, payload = _sent(publisher)
    assert topic == 'com.hertzai.hevolve.chat.u7'
    assert payload['media'] == MEDIA
    assert publisher.call_args.kwargs == {'skip_crossbar': True}
    # No second path around the one publisher.
    for leg in bus.legs.values():
        leg.assert_not_called()


def test_without_skip_crossbar_the_call_is_exactly_as_before(publisher):
    """No new keyword for any other caller: a wrapper that takes
    (topic, message, timeout), as an older Nunba's does, keeps working for
    them."""
    publish_agent_message(text='hi', user_id='u7', request_id='r', prompt_id='42',
                          media=MEDIA)
    assert publisher.call_count == 1
    assert publisher.call_args.kwargs == {}


def test_a_raising_publisher_reports_false(monkeypatch):
    pub = MagicMock(side_effect=RuntimeError('bus down'))
    monkeypatch.setitem(sys.modules, 'hart_intelligence', SimpleNamespace(publish_async=pub))
    assert publish_agent_message(text='hi', user_id='u7', request_id='r',
                                 prompt_id='42', skip_crossbar=True) is False


# ── the real publish_async: skip_crossbar drops only the HTTP crossbar leg ──

@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


@pytest.fixture
def real_publisher(hie, bus, monkeypatch):
    """hart_intelligence's publish_async is the real one; its crossbar HTTP
    executor, the SSE fan-out and the bus legs are recorded."""
    refuse_all_network(monkeypatch)
    from core.platform import events
    monkeypatch.setitem(sys.modules, 'hart_intelligence',
                        SimpleNamespace(publish_async=hie.publish_async))
    monkeypatch.setattr(hie, 'client', object())          # crossbar configured
    http = MagicMock(name='crossbar_executor')
    monkeypatch.setattr(hie, 'crossbar_executor', http)
    sse = MagicMock(name='broadcast_sse_safe')
    monkeypatch.setattr(events, 'broadcast_sse_safe', sse)
    return SimpleNamespace(http=http, sse=sse, bus=bus)


def test_the_real_publisher_keeps_a_skip_crossbar_push_off_the_crossbar(real_publisher):
    assert publish_agent_message(text='Your reel is ready', user_id='u7',
                                 request_id='rid-1', prompt_id='42',
                                 media=MEDIA, skip_crossbar=True) is True
    real_publisher.http.submit.assert_not_called()
    real_publisher.bus.legs['_route_crossbar'].assert_not_called()
    # Her phone (PeerLink EVENTS) gets it on the chat topic, with the stamps
    # every chat message gets.
    peer = real_publisher.bus.legs['_route_peerlink']
    sent = [c for c in peer.call_args_list if c.args[0] == 'chat.response']
    assert len(sent) == 1
    data = sent[0].args[1]
    assert data['media'] == MEDIA and data['text'] == ['Your reel is ready']
    assert data['user_id'] == 'u7' and data.get('served_by')
    assert sent[0].kwargs.get('owner_user_id') == 'u7'
    # The desktop's own chat (SSE) gets it too.
    assert real_publisher.sse.call_count == 1
    assert real_publisher.sse.call_args.args[1]['media'] == MEDIA


def test_the_real_publisher_without_the_flag_still_reaches_the_crossbar(real_publisher):
    assert publish_agent_message(text='hi', user_id='u7', request_id='r',
                                 prompt_id='42') is True
    assert real_publisher.http.submit.call_count == 1


def test_behind_a_three_argument_wrapper_a_reel_push_fails_closed(real_publisher, hie,
                                                                   monkeypatch):
    """An older Nunba wraps publish_async as (topic, message, timeout).
    Every other message still goes through that wrapper, and a
    skip_crossbar push is reported undelivered -- never sent to the crossbar
    without the flag."""
    seen = []

    def nunba_wrapper(topic, message, timeout=2.0):
        hie.publish_async(topic, message, timeout)
        seen.append(topic)

    monkeypatch.setitem(sys.modules, 'hart_intelligence',
                        SimpleNamespace(publish_async=nunba_wrapper))
    assert publish_agent_message(text='hi', user_id='u7', request_id='r',
                                 prompt_id='42') is True
    assert seen == ['com.hertzai.hevolve.chat.u7']
    real_publisher.http.submit.reset_mock()
    assert publish_agent_message(text='Your reel is ready', user_id='u7',
                                 request_id='rid-1', prompt_id='42', media=MEDIA,
                                 skip_crossbar=True) is False
    real_publisher.http.submit.assert_not_called()


def test_behind_a_wrapper_that_forwards_the_flag_a_reel_push_stays_off_the_crossbar(
        real_publisher, hie, monkeypatch):
    """Nunba's thinking-trace wrapper (routes/hartos_backend_adapter
    ._capturing_thinking) forwards skip_crossbar when it is set, and calls
    publish_async as (topic, message, timeout) for everything else.  Behind
    that shape, the reel reaches her own legs and not the crossbar, and every
    other message still reaches the crossbar."""
    seen = []

    def nunba_wrapper(topic, message, timeout=2.0, skip_crossbar=False):
        flags = {'skip_crossbar': True} if skip_crossbar else {}
        hie.publish_async(topic, message, timeout, **flags)
        seen.append((topic, skip_crossbar))

    monkeypatch.setitem(sys.modules, 'hart_intelligence',
                        SimpleNamespace(publish_async=nunba_wrapper))
    assert publish_agent_message(text='Your reel is ready', user_id='u7',
                                 request_id='rid-1', prompt_id='42', media=MEDIA,
                                 skip_crossbar=True) is True
    real_publisher.http.submit.assert_not_called()
    peer = real_publisher.bus.legs['_route_peerlink']
    sent = [c.args[1] for c in peer.call_args_list if c.args[0] == 'chat.response']
    assert [d['media'] for d in sent] == [MEDIA]
    assert publish_agent_message(text='hi', user_id='u7', request_id='r',
                                 prompt_id='42') is True
    assert real_publisher.http.submit.call_count == 1
    assert seen == [('com.hertzai.hevolve.chat.u7', True),
                    ('com.hertzai.hevolve.chat.u7', False)]


def test_media_entries_that_are_not_objects_are_dropped(publisher):
    publish_agent_message(text='hi', user_id='u7', request_id='r', prompt_id='42',
                          media=[MEDIA[0], 'junk', None])
    _, payload = _sent(publisher)
    assert payload['media'] == MEDIA
