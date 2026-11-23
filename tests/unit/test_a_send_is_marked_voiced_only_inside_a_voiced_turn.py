"""A send the turn's clip will say carries ``turn_voiced: true``; no other does.

Owner 2026-11-23: audio played twice at once on the desktop.  HARTOS speaks a
person's /chat turn in ONE clip: every message the agent sent during it, then
the reply (crossbar_publish.turn_speech_text, from _chat_reply).  The Nunba
page also spoke each such send as it arrived, so the same words were heard
twice.  The page cannot know which sends the clip will say: a page that did
not start the turn (the tablet chats while the desktop window is open) has
no turn of its own, and a send under a turn's mark is not always voiced.
REUSE sends from a scheduled run or a camera result carry the last /chat
turn's id; CREATE's fallback id is '<user>_<prompt>-intermediate'; a goal
turn's sends are never voiced (review of Nunba 4fef48aa: a reminder and the
CREATE fallback went silent on the desktop).

So the publisher says it: publish_agent_message stamps ``turn_voiced: true``
only on a send published inside its own turn's run (the turn id
with_llm_context binds, core.llm_outbound_logger.turn_request_id) of a
person's turn (dispatch.is_genuine_user_request) whose /chat body did not
ask for text: the same conditions _chat_reply voices the turn by.  Without
the stamp a client speaks the send as before.  Additive: the field is absent
otherwise.

The boundary is the hart_intelligence publish_async, resolved through
sys.modules as safe_hartos_attr does (test_agent_message_reaches_the_desktop
_locally).

    python -m pytest tests/unit/test_a_send_is_marked_voiced_only_inside_a_voiced_turn.py -q --noconftest
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture()
def publisher(monkeypatch):
    pub = MagicMock(name='publish_async')
    monkeypatch.setitem(sys.modules, 'hart_intelligence',
                        SimpleNamespace(publish_async=pub))
    return pub


def _send(request_id, turn=None):
    """Publish one agent message under ``request_id`` while the turn ``turn``
    runs (None: no turn runs, as in a scheduled run); the payload sent."""
    from core.llm_outbound_logger import request_id_context
    from core.peer_link.crossbar_publish import publish_agent_message, turn_sends

    def publish():
        assert publish_agent_message(text='Twinkle twinkle little star', user_id='10202',
                                     request_id=request_id, prompt_id='342') is True
    if turn is None:
        publish()
    else:
        with request_id_context(turn):
            publish()
    turn_sends(str(request_id).split('-intermediate')[0])   # leave no record behind
    return _last_payload()


def _last_payload():
    pub = sys.modules['hart_intelligence'].publish_async
    (_topic, payload), _kw = pub.call_args
    return payload


@pytest.mark.parametrize('request_id', ['msg_T1-intermediate', 'msg_T1-intermediate-4821'])
def test_a_send_inside_its_persons_turn_is_marked_voiced(publisher, request_id):
    """CREATE's mark and REUSE's, inside the turn they name."""
    assert _send(request_id, turn='msg_T1').get('turn_voiced') is True


@pytest.mark.parametrize('request_id,turn,why', [
    ('msg_T1-intermediate-4821', None, 'a scheduled REUSE run: no turn runs'),
    ('msg_T1-intermediate-4821', 'msg_T2', 'a later turn runs; the id is the last turn\'s'),
    ('10202_342-intermediate', 'msg_T1', "CREATE's fallback id names no turn"),
    ('daemon_g1-intermediate', 'daemon_g1', "a goal turn's: never voiced"),
    ('msg_T1', 'msg_T1', 'no turn mark (a notice under the turn\'s id)'),
    ('-intermediate', '', 'a mark with no turn in front'),
    ('', 'msg_T1', 'no request id'),
])
def test_no_other_send_is_marked(publisher, request_id, turn, why):
    assert 'turn_voiced' not in _send(request_id, turn=turn), why


def test_a_turn_the_person_asked_as_text_is_not_marked(publisher):
    """_chat_reply speaks nothing for media_mode 'text', so its sends are not
    the clip's either."""
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context('/chat', method='POST',
                                  json={'text': 'hi', 'media_mode': 'text'}):
        assert 'turn_voiced' not in _send('msg_T1-intermediate', turn='msg_T1')
    with app.test_request_context('/chat', method='POST',
                                  json={'text': 'hi', 'media_mode': 'audio'}):
        assert _send('msg_T1-intermediate', turn='msg_T1').get('turn_voiced') is True


def test_the_rest_of_the_message_is_as_before(publisher):
    """The stamp is the one field added; the envelope is otherwise the same
    with or without it."""
    voiced = _send('msg_T1-intermediate', turn='msg_T1')
    plain = _send('msg_T1-intermediate', turn=None)
    assert {k: v for k, v in voiced.items() if k != 'turn_voiced'} == plain


def test_the_marked_send_is_still_one_the_turns_speech_says(publisher):
    """The stamp changes nothing in what the turn's clip says."""
    from core.llm_outbound_logger import request_id_context
    from core.peer_link.crossbar_publish import publish_agent_message, turn_speech_text
    with request_id_context('msg_T9'):
        publish_agent_message(text='Twinkle twinkle little star', user_id='10202',
                              request_id='msg_T9-intermediate', prompt_id='342')
    assert _last_payload()['turn_voiced'] is True
    assert turn_speech_text('msg_T9', 'Well sung!') == 'Twinkle twinkle little star\n\nWell sung!'
