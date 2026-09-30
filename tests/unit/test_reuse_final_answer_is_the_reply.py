"""A finished REUSE answer reaches the desktop ONCE: as the /chat reply.

Review of 57516f078 (which made REUSE's send_message_to_user1 publish on the
local chat topic on the desktop): get_agent_response's two in-loop answer
branches (message2userfinal and message2) call send_message_to_user1 AND
return the same text, and the return value IS the /chat reply
(hart_intelligence_entry hands it to _chat_reply).  The web client renders
both: Demopage.js appends every text message from
com.hertzai.hevolve.chat.{user_id} (handleDataReceived, no request_id
dedupe) and appends the HTTP reply as a second bubble (the chatApi.chat
branch), and both start TTS.

Measured here by driving the REAL get_agent_response to its answer branch
(fakes only at the boundaries: the autogen agents, the hart_intelligence
publish_async, the HTTP pool and the scheduler).  At 57516f078..62649bc18
the desktop published the finished answer on the chat topic, with request_id
'<the SPA request id>-intermediate-NNNN', and also returned it as the reply.

The canonical rule is the one CREATE already follows (its main-loop answer
returns, create_recipe get_response_group, never calls send_message_to_user1)
and REUSE's own post-loop extractor follows: a turn's finished answer is
RETURNED, not also published.  send_message_to_user1 stays the out-of-band
leg for messages with no HTTP reply to ride on: a mid-turn question from the
send_message_to_user tool, scheduled/timer/visual results, A2A replies.
The mid-turn case is pinned below so the fix cannot take it away.
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_USER = 'desk-final-1'
_PROMPT = 4242
_SPA_REQUEST_ID = 'spa-req-7f3a'
_ANSWER = 'Your report is ready: 3 invoices are overdue.'
_QUESTION = 'Which month should I check?'


@pytest.fixture()
def rr():
    pytest.importorskip('autogen', reason='autogen not installed')
    from hartos import reuse_recipe
    return reuse_recipe


@pytest.fixture()
def publisher(monkeypatch):
    """hart_intelligence.publish_async, as safe_hartos_attr resolves it."""
    pub = MagicMock(name='publish_async')
    monkeypatch.setitem(sys.modules, 'hart_intelligence',
                        SimpleNamespace(publish_async=pub))
    return pub


@pytest.fixture()
def bundled(monkeypatch):
    monkeypatch.setenv('NUNBA_BUNDLED', '1')


@pytest.fixture()
def central(monkeypatch):
    monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
    monkeypatch.delattr(sys, 'frozen', raising=False)


def _chat_texts(pub):
    """Every text published on this user's chat topic, in order."""
    out = []
    for call in pub.call_args_list:
        topic, payload = call[0][0], call[0][1]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if topic == f'com.hertzai.hevolve.chat.{_USER}':
            out.append((payload.get('text') or [''])[0])
    return out


def _drive_turn(rr, monkeypatch, answer_key='message2userfinal',
                mid_turn_question=False):
    """Run the real get_agent_response for one REUSE turn whose group ends
    on the Assistant's answer.  Returns (reply, pooled_post mock)."""
    from flask import Flask

    user_prompt = f'{_USER}_{_PROMPT}'
    action = {'action_id': 1, 'action': 'Report the overdue invoices',
              'can_perform_without_user_input': 'no', 'recipe': []}
    monkeypatch.setattr(rr, 'recipes', {user_prompt: {'actions': [action]}})
    monkeypatch.setattr(rr, 'user_tasks', {user_prompt: rr.Action([action])})
    monkeypatch.setattr(rr, 'user_ledgers', {})
    monkeypatch.setattr(rr, 'request_id_list', {user_prompt: _SPA_REQUEST_ID})
    monkeypatch.setattr(rr, 'request_id_list_sent_intermediate', {})

    group_chat = SimpleNamespace(messages=[], agents=[])
    manager = SimpleNamespace(_oai_messages={})

    def initiate_chat(recipient, message=None, **_kw):
        group_chat.messages.append(
            {'role': 'user', 'name': 'ChatInstructor', 'content': message})
        if mid_turn_question:
            # What the send_message_to_user tool does mid-turn
            # (core.agent_tools: a thread running send_message_to_user1).
            rr.send_message_to_user1(_USER, _QUESTION, '', _PROMPT)
        group_chat.messages.append(
            {'role': 'assistant', 'name': 'Assistant',
             'content': json.dumps({answer_key: _ANSWER})})

    user_proxy = SimpleNamespace(initiate_chat=initiate_chat)
    chat_instructor = SimpleNamespace(initiate_chat=initiate_chat)
    assistant = SimpleNamespace(name='Assistant')
    helper = SimpleNamespace(name='helper')

    with patch.object(rr, 'pooled_post') as post, \
            patch.object(rr, 'scheduler') as sched, \
            Flask('reuse-final-answer').app_context():
        sched.get_job.return_value = None
        reply = rr.get_agent_response(
            assistant, chat_instructor, helper, user_proxy, manager,
            group_chat, 'how many invoices are overdue?', 'user', _USER,
            _PROMPT, _SPA_REQUEST_ID)
    return reply, post


@pytest.mark.parametrize('answer_key', ['message2userfinal', 'message2'])
def test_desktop_delivers_the_finished_answer_only_as_the_reply(
        rr, bundled, publisher, monkeypatch, answer_key):
    reply, post = _drive_turn(rr, monkeypatch, answer_key=answer_key)
    assert reply == _ANSWER
    # THE DEFECT: the same text also went out on the chat topic, so the web
    # client rendered (and spoke) it twice.
    assert _ANSWER not in _chat_texts(publisher), publisher.call_args_list
    post.assert_not_called()


def test_a_mid_turn_question_still_reaches_the_desktop(
        rr, bundled, publisher, monkeypatch):
    """What 57516f078 correctly fixed must survive: a question the agent
    sends DURING the turn has no HTTP reply to ride on, so it is published."""
    reply, _post = _drive_turn(rr, monkeypatch, mid_turn_question=True)
    assert reply == _ANSWER
    assert _chat_texts(publisher) == [_QUESTION]
    payload = publisher.call_args[0][1]
    # The id a client could correlate on, as it is on the wire today.
    assert payload['request_id'].startswith(f'{_SPA_REQUEST_ID}-intermediate-')


def test_central_keeps_its_chatbot_pipeline_leg(
        rr, central, publisher, monkeypatch):
    """Central is not the desktop: its off-box leg for the answer is left as
    it was (the URL is an owner decision); nothing is published locally."""
    reply, post = _drive_turn(rr, monkeypatch)
    assert reply == _ANSWER
    assert post.call_count == 1
    body = json.loads(post.call_args.kwargs['data'])
    assert body['message'] == _ANSWER
    assert _chat_texts(publisher) == []
