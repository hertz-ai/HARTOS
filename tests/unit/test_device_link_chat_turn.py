"""A person's phone asks its desktop for a chat turn over the PeerLink device link.

The phone's DesktopChat sends ``{"type": "chat_request", ...}`` on the
``dispatch`` channel, as a request.  The desktop answers it as one more
inbound channel -- the turn goes to the local /chat
(the agentic CREATE/REUSE door every other channel uses, through
FlaskChannelIntegration.run_turn) with the agent id the phone names, as the
user the device token proved, never the user the body names -- and the /chat
body goes back as the request's reply on the same link.

Opening ``dispatch`` to a phone is typed: only ``chat_request`` frames, and
only as requests.  Embedded nodes take ``device_control`` on the same channel
(embedded_main._register_device_control_handler), which a phone must never
reach.

Real handshake, real receive loop, real link manager registration; the HTTP
call to /chat is the only boundary mocked.
"""
import json
import os
import sys
import threading
from unittest.mock import Mock, patch

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402
import requests  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.peer_link.channels import device_may_receive, device_may_send  # noqa: E402
from core.peer_link.link import LinkState, PeerLink, TrustLevel  # noqa: E402
from core.peer_link.link_manager import get_link_manager  # noqa: E402
from tests.unit.test_peer_link_device_links import (  # noqa: E402,F401
    _accept, _allow, _desktop, _hello, _install_real_verifier, phone,
)

PHONE_USER = '40021'          # the user the phone's device token carries
REQUEST_ID = 'rq-1'


def _integration():
    """A FlaskChannelIntegration with __init__ bypassed, as the inbound
    contract tests build it: run_turn and handle_device_request need only
    the agent URL and the node's device id."""
    from integrations.channels.flask_integration import FlaskChannelIntegration
    fi = FlaskChannelIntegration.__new__(FlaskChannelIntegration)
    fi.agent_api_url = 'http://desktop-test/chat'
    fi.create_mode = False
    fi._device_id = 'desktop-device'
    return fi


@pytest.fixture
def fi(monkeypatch):
    """The process's live integration (the one get_channel_integration
    returns), which the bound device handler answers through."""
    from integrations.channels import flask_integration
    live = _integration()
    monkeypatch.setattr(flask_integration, '_integration', live)
    return live


def _device_link(phone):
    _allow(phone)
    _install_real_verifier()
    return _accept(_hello(phone, phone.token()))


def _chat_request(**payload):
    body = {'text': 'teach me fractions', 'prompt_id': 54,
            'conversation_id': 'c-1', 'request_id': 'r-1',
            'draft_first': False}
    body.update(payload)
    return {'type': 'chat_request', 'payload': body}


class _Socket:
    """Feeds frames to the real receive loop, then holds the socket open
    until the link has answered (or a short wait passes), then drops."""

    def __init__(self, frames, expect_reply=True):
        self.frames = [json.dumps(f) for f in frames]
        self.sent = []
        self.replied = threading.Event()
        self.expect_reply = expect_reply

    def recv(self, timeout=None):
        if self.frames:
            return self.frames.pop(0)
        self.replied.wait(5 if self.expect_reply else 0.3)
        raise ConnectionResetError('done')

    def send(self, data):
        frame = json.loads(data.decode('utf-8') if isinstance(data, bytes) else data)
        self.sent.append(frame)
        if frame.get('re'):
            self.replied.set()

    def close(self):
        pass


def _serve(link, frames, expect_reply=True):
    """Run the link's real receive loop over ``frames``; return the frames
    the desktop sent back."""
    sock = _Socket(frames, expect_reply)
    link._ws = sock
    link._state = LinkState.CONNECTED
    link._receive_loop()
    return sock.sent


def _posted(status=200, body=None):
    calls = []

    def fake_post(url, json=None, timeout=None, headers=None, **kw):
        calls.append({'url': url, 'json': json, 'timeout': timeout,
                      'headers': headers})
        return Mock(status_code=status,
                    json=lambda: body if body is not None else {
                        'text': 'One step: a fraction is a part of a whole. '
                                'What is half of 8?',
                        'agent_id': 54, 'source': 'langchain_local',
                        'success': True})
    return calls, fake_post


# ── the policy: dispatch opens to a phone for chat_request only ────────────


def test_a_phone_may_send_a_chat_request_on_dispatch_and_nothing_else_there():
    assert device_may_send('dispatch', {'type': 'chat_request'})
    assert not device_may_send('dispatch', {'type': 'device_control'})
    assert not device_may_send('dispatch', {'type': 'agent_task'})
    assert not device_may_send('dispatch', {})
    assert not device_may_send('dispatch', b'binary')
    assert not device_may_send('dispatch')          # no frame: refused
    assert not device_may_receive('dispatch')       # never broadcast to it
    assert device_may_send('control')               # untouched
    assert not device_may_send('compute', {'type': 'chat_request'})


# ── the turn ───────────────────────────────────────────────────────────────


def test_a_phones_chat_request_runs_the_agents_turn_and_answers_on_the_link(fi, phone):
    link = _device_link(phone)
    fi._bind_device_link()
    calls, fake_post = _posted()
    mint = Mock(return_value={'Authorization': 'Bearer t'})
    frame = {'ch': 'dispatch', 'id': REQUEST_ID, 'rq': 1,
             'd': _chat_request(user_id='99999')}   # a body naming another user

    with patch('integrations.channels.flask_integration.pooled_post', fake_post), \
            patch('integrations.agent_engine.dispatch._internal_auth_headers', mint):
        sent = _serve(link, [frame])

    assert len(calls) == 1
    posted = calls[0]
    assert posted['url'] == 'http://desktop-test/chat'
    turn = posted['json']
    assert turn['user_id'] == PHONE_USER, 'the token proved the user, not the body'
    assert turn['prompt_id'] == 54, 'the agent the phone named'
    assert turn['text'] == turn['prompt'] == 'teach me fractions'
    assert turn['conversation_id'] == 'c-1'
    assert turn['request_id'] == 'r-1'
    assert turn['draft_first'] is False
    assert posted['headers'] == {'Authorization': 'Bearer t'}
    assert mint.call_args.kwargs == {'user_id': PHONE_USER, 'role': 'user'}

    replies = [f for f in sent if f.get('re') == REQUEST_ID]
    assert len(replies) == 1
    assert replies[0]['ch'] == 'dispatch'
    assert replies[0]['d']['type'] == 'chat_reply'
    assert replies[0]['d']['status'] == 200
    assert replies[0]['d']['body']['text'].startswith('One step')


def test_a_device_control_frame_from_a_phone_reaches_no_handler(fi, phone, caplog):
    """The frame type embedded nodes act on is still closed to a phone."""
    link = _device_link(phone)
    fi._bind_device_link()
    seen = []
    get_link_manager().register_channel_handler(
        'dispatch', lambda channel, data, pid: seen.append(data))
    calls, fake_post = _posted()
    frame = {'ch': 'dispatch', 'id': 'dc-1', 'rq': 1,
             'd': {'type': 'device_control', 'action': 'gpio_on'}}
    with patch('integrations.channels.flask_integration.pooled_post', fake_post), \
            caplog.at_level('WARNING'):
        sent = _serve(link, [frame], expect_reply=False)
    assert seen == [] and calls == [] and sent == []
    assert any('which devices may not' in r.getMessage() for r in caplog.records)


def test_a_chat_request_that_is_not_a_request_starts_no_turn(fi, phone):
    """No 'rq': nobody waits for the answer, so no turn runs inline on the
    receive thread."""
    link = _device_link(phone)
    fi._bind_device_link()
    calls, fake_post = _posted()
    frame = {'ch': 'dispatch', 'id': 'nr-1', 'd': _chat_request()}
    with patch('integrations.channels.flask_integration.pooled_post', fake_post):
        sent = _serve(link, [frame], expect_reply=False)
    assert calls == [] and sent == []


def test_a_nodes_chat_request_is_not_served(fi, phone):
    """The handler answers a person's own device; another node on dispatch
    is node traffic, not a person's turn."""
    # A SAME_USER node (no frame encryption), so the frame reaches the
    # handlers and the handler itself is what declines it.  It carries the
    # phone's user, so the link's kind, not a missing user, is what says no.
    node = PeerLink('node-7', '10.0.0.7:6777', TrustLevel.SAME_USER)
    node._state = LinkState.CONNECTED
    node.user_id = PHONE_USER
    get_link_manager()._links['node-7'] = node
    assert node.kind == 'node'
    fi._bind_device_link()
    calls, fake_post = _posted()
    frame = {'ch': 'dispatch', 'id': 'n-1', 'rq': 1, 'd': _chat_request()}
    with patch('integrations.channels.flask_integration.pooled_post', fake_post):
        sent = _serve(node, [frame], expect_reply=False)
    assert calls == [] and sent == []


def test_a_blank_message_is_refused_without_a_turn(fi, phone):
    link = _device_link(phone)
    fi._bind_device_link()
    calls, fake_post = _posted()
    frame = {'ch': 'dispatch', 'id': REQUEST_ID, 'rq': 1,
             'd': _chat_request(text='   ')}
    with patch('integrations.channels.flask_integration.pooled_post', fake_post):
        sent = _serve(link, [frame])
    assert calls == []
    reply = next(f for f in sent if f.get('re') == REQUEST_ID)['d']
    assert reply['type'] == 'chat_reply' and reply['status'] == 400


@pytest.mark.parametrize('status,body', [
    (500, {'error': 'boom'}),
    (200, {'text': 'Your local AI is busy with another task right now.',
           'error': 'local_llm_starting', 'success': False}),
])
def test_the_chat_answer_goes_back_as_it_came(fi, phone, status, body):
    """/chat's own status and body, unedited: the phone shows what the
    desktop answered, busy or failed included."""
    link = _device_link(phone)
    fi._bind_device_link()
    calls, fake_post = _posted(status, body)
    frame = {'ch': 'dispatch', 'id': REQUEST_ID, 'rq': 1, 'd': _chat_request()}
    with patch('integrations.channels.flask_integration.pooled_post', fake_post):
        sent = _serve(link, [frame])
    reply = next(f for f in sent if f.get('re') == REQUEST_ID)['d']
    assert reply == {'type': 'chat_reply', 'status': status, 'body': body}


def test_a_turn_that_times_out_answers_504(fi, phone):
    link = _device_link(phone)
    fi._bind_device_link()

    def slow_post(*a, **k):
        raise requests.Timeout('turn ran past the budget')

    frame = {'ch': 'dispatch', 'id': REQUEST_ID, 'rq': 1, 'd': _chat_request()}
    with patch('integrations.channels.flask_integration.pooled_post', slow_post):
        sent = _serve(link, [frame])
    reply = next(f for f in sent if f.get('re') == REQUEST_ID)['d']
    assert reply['type'] == 'chat_reply' and reply['status'] == 504


def test_start_binds_the_device_link_once(fi):
    """The handler rides the channel integration's own lifecycle: start()
    binds it, and binding twice registers one handler."""
    fi._thread = None
    fi.registry = Mock()
    fi.registry.get.return_value = None
    with patch.object(type(fi), 'restore_persisted_channels', lambda self: {}), \
            patch.object(type(fi), 'register_channel', lambda self, *a, **k: False), \
            patch('integrations.channels.flask_integration.threading.Thread') as th:
        fi.start()
        th.return_value.start.assert_called_once()
        from integrations.channels.flask_integration import _answer_device_request
        handlers = get_link_manager()._channel_handlers.get('dispatch', [])
        assert handlers.count(_answer_device_request) == 1, 'start() bound it'
        fi._bind_device_link()
    handlers = get_link_manager()._channel_handlers.get('dispatch', [])
    assert handlers.count(_answer_device_request) == 1, 'a second bind is a no-op'


def test_two_started_integrations_answer_one_request_once(fi, phone):
    """init_channels can build and start an integration after an on-demand
    path started another; both bind, and one phone request is still ONE
    agent turn, through the live integration."""
    orphan = _integration()
    link = _device_link(phone)
    orphan._bind_device_link()
    fi._bind_device_link()
    calls, fake_post = _posted()
    frame = {'ch': 'dispatch', 'id': REQUEST_ID, 'rq': 1, 'd': _chat_request()}
    with patch('integrations.channels.flask_integration.pooled_post', fake_post):
        sent = _serve(link, [frame])
    assert len(calls) == 1
    assert [f['re'] for f in sent if f.get('re')] == [REQUEST_ID]


def test_a_phone_that_does_not_say_gets_one_synchronous_answer(fi, phone):
    """The request's reply is the phone's whole answer: without a word from
    the phone the turn runs without the draft-first standby."""
    link = _device_link(phone)
    fi._bind_device_link()
    calls, fake_post = _posted()
    request = _chat_request()
    del request['payload']['draft_first']
    frame = {'ch': 'dispatch', 'id': REQUEST_ID, 'rq': 1, 'd': request}
    with patch('integrations.channels.flask_integration.pooled_post', fake_post):
        _serve(link, [frame])
    assert calls[0]['json']['draft_first'] is False


def _ack_of(link):
    """The hello_ack the desktop sent the phone when it admitted this link."""
    return json.loads(link._ws.sent[0])


def test_a_desktop_that_answers_device_chat_says_so_in_its_hello_ack(fi, phone):
    """A desktop on an older build has no chat_request handler and answers
    nothing, so a phone that asked it anyway would wait out its whole turn
    budget.  A desktop that answers names the device requests it answers in
    the hello_ack; the phone asks over the link only when it sees them."""
    fi._bind_device_link()
    fi._bind_device_link()
    link = _device_link(phone)
    assert _ack_of(link)['capabilities']['device_requests'] == ['chat_request']
    assert get_link_manager().device_requests_answered() == ['chat_request']


def test_a_node_that_answers_no_device_request_advertises_none(phone):
    link = _device_link(phone)
    caps = _ack_of(link)['capabilities']
    assert 'device_requests' not in caps
    assert 'tier' in caps            # the rest of the handshake is unchanged


def test_handle_message_runs_its_turn_through_run_turn():
    """One /chat call for every inbound path: _handle_message (Telegram,
    Discord, ...) and the device link both go through run_turn."""
    fi = _integration()
    fi.default_user_id = 7
    fi.default_prompt_id = 1
    fi.registry = Mock()
    fi.registry.get.return_value = None
    sess = Mock(user_id=None, prompt_id=None)
    fi._session_manager = Mock()
    fi._session_manager.get_session.return_value = sess
    fi._self_chat = Mock()
    fi._self_chat.is_self_message.return_value = False
    fi._response_router = Mock()
    fi._resolve_user_id_for_sender = Mock(return_value=7)
    fi._get_channel_prompt_id = Mock(return_value=None)
    from integrations.channels.base import Message
    msg = Message(id='m1', channel='telegram', sender_id='s1',
                  sender_name='S', chat_id='c1', text='hello')
    with patch.object(type(fi), 'run_turn',
                      autospec=True,
                      return_value=(200, {'text': 'hi back'})) as run_turn:
        reply = fi._handle_message(msg)
    assert reply == 'hi back'
    args = run_turn.call_args
    assert args.args[1:4] == (7, 1, 'hello')
    assert args.kwargs['channel_context']['channel'] == 'telegram'


def test_a_chat_error_that_is_not_json_keeps_its_words(caplog):
    """A proxy's or server's error page is not /chat's JSON.  Its words stay
    in the turn's body, so the channel's error log still says what went
    wrong (the log line _handle_message wrote before run_turn existed) and
    the phone's reply carries them too."""
    fi = _integration()
    fi.default_user_id = 7
    fi.default_prompt_id = 1
    fi.registry = Mock()
    fi.registry.get.return_value = None
    fi._session_manager = Mock()
    fi._session_manager.get_session.return_value = Mock(user_id=None, prompt_id=None)
    fi._self_chat = Mock()
    fi._self_chat.is_self_message.return_value = False
    fi._response_router = Mock()
    fi._resolve_user_id_for_sender = Mock(return_value=7)
    fi._get_channel_prompt_id = Mock(return_value=None)

    def not_json():
        raise ValueError('Expecting value: line 1 column 1 (char 0)')

    page = Mock(status_code=502, json=not_json, text='<html>Bad gateway</html>')
    with patch('integrations.channels.flask_integration.pooled_post',
               return_value=page):
        status, body = fi.run_turn(7, 1, 'hello')
        from integrations.channels.base import Message
        msg = Message(id='m1', channel='telegram', sender_id='s1',
                      sender_name='S', chat_id='c1', text='hello')
        with caplog.at_level('ERROR'):
            reply = fi._handle_message(msg)
    assert (status, body) == (502, {'error': '<html>Bad gateway</html>'})
    assert reply == 'Sorry, I encountered an error processing your request.'
    assert any('Bad gateway' in r.getMessage() for r in caplog.records)
