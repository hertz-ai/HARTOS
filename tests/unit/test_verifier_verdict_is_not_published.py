"""A group member's message2userfinal reaches the user only when it IS an answer.

Review of 31ea54045: REUSE's speaker selectors (state_transition,
state_transition1 for the timer group, state_transition2 for the visual
group) publish any message2userfinal they see through send_message_to_user1,
which on the desktop is the user's chat topic.  That included the
StatusVerifier's own verdict when it carried the key, and an unfilled
'<your answer here>' template: internal plumbing delivered to the user.

The question "can the user read this?" already has one answer,
_reuse_message_is_user_answer (it refuses the verifier seat, this module's
own steers and a <placeholder> value).  The three selectors now send through
_reuse_speaker_says_to_user, which asks it first.  Pre-existing; fixed in the
same review.

Review of d8fe536b2 (F1, F1b): time_based_execution and
visual_based_execution sent it the same way, and the scheduled run read
the MAIN group's tail instead of the timer group it ran.  The guard now
covers every function that reads message2userfinal, by what it does.
"""
import json
import os
import sys
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture()
def rr():
    pytest.importorskip('autogen', reason='autogen not installed')
    from hartos import reuse_recipe
    return reuse_recipe


def _msg(name, value):
    return {'role': 'assistant', 'name': name,
            'content': json.dumps({'message2userfinal': value})}


def _say(rr, message):
    with patch.object(rr, 'send_message_to_user1') as send:
        sent = rr._reuse_speaker_says_to_user('u7', message, 42)
    return sent, send


def test_the_agents_real_message_is_sent(rr):
    sent, send = _say(rr, _msg('Assistant', 'Which month should I check?'))
    assert sent is True
    send.assert_called_once_with('u7', 'Which month should I check?', '', 42)


@pytest.mark.parametrize('message', [
    _msg('StatusVerifier', 'There are no tool results in this conversation.'),
    _msg('Assistant', '<your answer here>'),
    _msg('Assistant', ''),
    {'role': 'user', 'name': 'ChatInstructor', 'content':
     'Perform this action -> Action #2: {"message2userfinal": "x"}'},
], ids=['verifier-verdict', 'placeholder', 'empty', 'own-steer'])
def test_plumbing_is_never_sent(rr, message):
    sent, send = _say(rr, message)
    assert sent is False
    send.assert_not_called()


# ── the scheduled and visual runs (review of d8fe536b2, F1 / F1b) ────────
#
# time_based_execution and visual_based_execution send the run's
# message2userfinal to the user out of band (they have no /chat reply).
# They are module functions, so they are driven here with fakes only at the
# boundaries: the autogen agents and groups, send_message_to_user1, the
# camera frame and the visual context.

_UID, _PID = 'u9', 77
_TASK = 'remind me to drink water'


def _group(*msgs):
    from types import SimpleNamespace
    return SimpleNamespace(messages=list(msgs))


def _agents(rr, monkeypatch, main_group, timer_group, visual=None):
    from types import SimpleNamespace

    def time_user_chat(recipient, message=None, **_kw):
        timer_group.messages.append(dict(timer_group.pending))

    time_user = SimpleNamespace(initiate_chat=time_user_chat)
    manager_1 = SimpleNamespace(name='manager_1')
    tup = (None, None, main_group, None, None, None, None, time_user,
           timer_group, manager_1, None, visual or {})
    monkeypatch.setattr(rr, 'user_agents', {f'{_UID}_{_PID}': tup})


def _run_timer(rr, monkeypatch, timer_tail, main_tail=None):
    from flask import Flask
    main = _group(main_tail or _msg('Assistant', 'the MAIN conversation answer'))
    timer = _group()
    timer.pending = timer_tail
    _agents(rr, monkeypatch, main, timer)
    with patch.object(rr, 'send_message_to_user1') as send, \
            Flask('timer').app_context():
        rr.time_based_execution(_TASK, _UID, _PID, 1)
    return send


def test_a_scheduled_run_sends_its_own_result(rr, monkeypatch):
    send = _run_timer(rr, monkeypatch,
                      _msg('time_agent', 'Time to drink water.'))
    send.assert_called_once_with(_UID, 'Time to drink water.', _TASK, _PID)


def test_a_scheduled_run_never_sends_the_main_conversation(rr, monkeypatch):
    """F1b: the run happens on manager_1 / group_chat_1; the tail it reads
    must be that group's, not the main chat's."""
    send = _run_timer(rr, monkeypatch,
                      {'role': 'assistant', 'name': 'time_agent',
                       'content': 'working on it'})
    send.assert_not_called()


@pytest.mark.parametrize('tail', [
    _msg('StatusVerifier', 'There are no tool results in this conversation.'),
    _msg('time_agent', '<your answer here>'),
], ids=['verifier-verdict', 'placeholder'])
def test_a_scheduled_run_never_sends_plumbing(rr, monkeypatch, tail):
    send = _run_timer(rr, monkeypatch, tail)
    send.assert_not_called()


def _run_visual(rr, monkeypatch, tail):
    from types import SimpleNamespace
    from flask import Flask
    chat = _group()

    def visual_chat(recipient, message=None, **_kw):
        chat.messages.extend([dict(tail), {'role': 'user', 'name': 'visual_user',
                                            'content': 'TERMINATE'}])

    visual = {'manager_2': SimpleNamespace(), 'group_chat_2': chat,
              'visual_user': SimpleNamespace(initiate_chat=visual_chat)}
    _agents(rr, monkeypatch, _group(), _group(), visual=visual)
    monkeypatch.setattr(rr, 'get_frame', lambda uid: b'frame')
    monkeypatch.setattr(rr.helper_fun, 'get_visual_context',
                        lambda uid, minutes: 'a person at a desk')
    with patch.object(rr, 'send_message_to_user1') as send, \
            Flask('visual').app_context():
        rr.visual_based_execution(_TASK, _UID, _PID)
    return send


def test_a_visual_run_sends_its_real_message(rr, monkeypatch):
    send = _run_visual(rr, monkeypatch,
                       _msg('visual_agent', 'You look tired; take a break.'))
    send.assert_called_once_with(_UID, 'You look tired; take a break.',
                                 _TASK, _PID)


@pytest.mark.parametrize('tail', [
    _msg('StatusVerifier', 'There are no tool results in this conversation.'),
    _msg('visual_agent', '<your answer here>'),
], ids=['verifier-verdict', 'placeholder'])
def test_a_visual_run_never_sends_plumbing(rr, monkeypatch, tail):
    send = _run_visual(rr, monkeypatch, tail)
    send.assert_not_called()


# ── source guard, by behaviour: every sender of message2userfinal ──────

_GATE_OWNERS = {'_reuse_speaker_says_to_user', '_reuse_answer_off_box'}


def ungated_senders(src):
    """(function, line) for every direct send_message_to_user1 call in a
    function (closures counted on their own) that reads message2userfinal.

    Such a function is delivering a group member's message2userfinal, so it
    must hand it to _reuse_speaker_says_to_user (mid-round / out-of-band)
    or return it through _reuse_answer_off_box (the /chat reply), never
    send it itself.  Whatever the function is called."""
    import ast
    out = []

    def own_nodes(fn):
        stack = list(ast.iter_child_nodes(fn))
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            yield n
            stack.extend(ast.iter_child_nodes(n))

    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name in _GATE_OWNERS:
            continue
        nodes = list(own_nodes(fn))
        reads_key = any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                        and 'message2userfinal' in n.value.lower() for n in nodes)
        if not reads_key:
            continue
        for n in nodes:
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == 'send_message_to_user1'):
                out.append((fn.name, n.lineno))
    return out


def test_source_guard_every_message2userfinal_sender_passes_the_gate(rr):
    import inspect
    assert ungated_senders(inspect.getsource(rr)) == []


@pytest.mark.parametrize('snippet', [
    "def anything(m, u, p):\n"
    "    j = retrieve_json(m['content'])\n"
    "    send_message_to_user1(u, j['message2userfinal'], '', p)\n",
    "def outer():\n"
    "    def inner(m, u, p):\n"
    "        if 'message2userfinal' in m['content'].lower():\n"
    "            send_message_to_user1(u, m['content'], '', p)\n",
], ids=['module-function', 'closure'])
def test_source_guard_sees_an_ungated_sender(snippet):
    """Anti-vacuity: the guard above can fail, for a function of any name
    and for a closure."""
    assert len(ungated_senders(snippet)) == 1
