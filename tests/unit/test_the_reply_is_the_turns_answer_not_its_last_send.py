"""The reply is the turn's answer, not the last thing it sent.

WHAT WENT WRONG (live, source Nunba on the local 4B, 18 Nov 2026 22:26-22:27
IST, research agent 1795014914479, a test account): action 2 gave up and the
synthesis steered a round.  In that round the 4B sent the answer (about
1,350 characters), then "I'm ready to continue gathering more information
..." (146), both declared expects_reply=false.  The reply pick
(_reuse_turn_delivered_send) walked newest first and spoke the narration.

THE RULE (one reader for the synthesis's "the send is the reply" paths):
  * an open question wins, newest first: an ask card still waiting, or a
    send that DECLARED expects_reply=true (lifecycle_hooks.send_declared_ask).
    An undeclared send is not a question here; send_is_ask keeps its
    undeclared-is-an-ask fail-safe for completion, which this does not touch
    (hartos-43's review, 18 Nov 2026);
  * otherwise the longest delivered send the answer rules accept, a tie to
    the newer (lifecycle_hooks.longest_answer, the pick _derive_written_answer
    and _reuse_turn_written_reply make too);
  * after a steer only the round's own sends count, told apart by the
    tool-call ids the log held before the steer, not by a message index or
    the text.

The price of the first rule, pinned below: an answer followed by a send that
declares a question ("Anything else I can help with?") replies with the
question.

    python -m pytest tests/unit/test_the_reply_is_the_turns_answer_not_its_last_send.py -q --noconftest
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

RESEARCH = '5ef553d3-24ef-47ba-b6aa-da0b5c7e33d5_1795014914479'
TURN = 'msg_1795014914479_sim'

ANSWER = ("Here is what I found about replying when someone says 'go ahead':\n"
          "1. 'Thank you, I'll proceed then.' acknowledges the permission.\n"
          "2. 'Great, I'll get started right away.' shows readiness.\n"
          "3. 'Thanks! I'll keep you posted.' promises an update.\n"
          "In a formal setting the first is safest; with colleagues the third "
          "keeps them in the loop.")
NARRATION = ("I'm ready to continue gathering more information whenever you "
             "need it.")
PROGRESS = "Sure, let me look up your order status. One moment."
COURTESY = "Anything else I can help with?"


def _seed(turn):
    return {'role': 'user', 'name': 'ChatInstructor',
            'content': f'Perform this action -> Action #2 for request {turn}'}


_UNDECLARED = object()


def _send(call_id, text, expects_reply=False):
    args = {'text': text}
    if expects_reply is not _UNDECLARED:
        args['expects_reply'] = expects_reply
    return {'role': 'assistant', 'name': 'Assistant', 'content': '',
            'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
                'name': 'send_message_to_user', 'arguments': json.dumps(args)}}]}


def _receipt(call_id, turn=TURN):
    return {'role': 'tool', 'name': 'Assistant', 'tool_call_id': call_id,
            'content': 'Message sent successfully to user with request_id: '
                       + turn}


def _verdict(status='pending', message='Working on it.'):
    return {'role': 'user', 'name': 'StatusVerifier',
            'content': json.dumps({'status': status, 'action_id': 2,
                                   'message': message})}


def _card(call_id, question):
    return {'role': 'assistant', 'name': 'Assistant', 'content': '',
            'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
                'name': 'ask_person', 'arguments': json.dumps({
                    'component': {'title': question,
                                  'fields': [{'label': question}]}})}}]}


def _card_result(call_id, ask_id='ask-1'):
    return {'role': 'tool', 'name': 'Assistant', 'tool_call_id': call_id,
            'content': json.dumps({'status': 'waiting_for_person',
                                   'ask_id': ask_id})}


class _Task:
    def __init__(self, current_action=2, total=3):
        self.current_action = current_action
        self.actions = [{'action': 'step %d' % i,
                         'can_perform_without_user_input': 'yes'}
                        for i in range(1, total + 1)]
        self.evidence_vacuous_action = None


class _Chat:
    def __init__(self, messages):
        self.agents = []
        self.messages = list(messages)


class _Manager:
    def __init__(self):
        self._oai_messages = {}


class _Round:
    """The steering seat: records the steer and plays the live round into
    the group log."""

    def __init__(self, chat, round_messages=()):
        self.chat = chat
        self.round_messages = [dict(m) for m in round_messages]
        self.messages = []

    def initiate_chat(self, recipient=None, message=None, **kw):
        self.messages.append(message)
        self.chat.messages.extend(self.round_messages)


@pytest.fixture
def rr():
    import hartos.reuse_recipe as mod
    return mod


def _run(rr, monkeypatch, history, round_messages=(), state='gave_up'):
    monkeypatch.setattr(rr, '_reuse_turn_start',
                        {RESEARCH: {'seed_text': _seed(TURN)['content']}},
                        raising=True)
    monkeypatch.setattr(rr, 'request_id_list', {RESEARCH: TURN}, raising=True)
    monkeypatch.setattr(rr, '_reuse_outstanding_tools',
                        lambda *a, **k: [], raising=True)
    monkeypatch.setattr(rr, '_reuse_held_action',
                        lambda user_prompt: None, raising=True)
    monkeypatch.setattr(rr, 'ledger_open_ask',
                        lambda user_prompt, action_id, flow: None, raising=True)
    monkeypatch.setattr(rr, '_reuse_action_flow',
                        lambda user_prompt, action_id: 0, raising=True)
    monkeypatch.setattr(rr, '_reuse_action_declares_user_pause',
                        lambda user_prompt, action_id: False, raising=True)
    monkeypatch.setattr(rr, 'get_action_state',
                        lambda user_prompt, action_id: getattr(
                            rr.ActionState, state.upper()), raising=True)
    monkeypatch.setitem(rr.user_tasks, RESEARCH, _Task())
    chat = _Chat(history)
    seat = _Round(chat, round_messages)
    rr._reuse_synthesis_turn(RESEARCH, chat, _Manager(), seat)
    return chat, seat


def _reply(rr, chat):
    """What the synthesis set as the reply: the tail it appended."""
    tail = chat.messages[-1]
    return tail.get('content') if rr._reuse_message_is_user_answer(tail) else None


class TestTheSteeredRoundRepliesWithItsAnswer:

    def test_the_live_research_round_replies_with_the_answer(self, rr, monkeypatch):
        """22:26:27 the answer, 22:26:42 the narration, both declared false.
        RED before the fix: the narration was the reply."""
        chat, seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                          round_messages=[_send('c_ans', ANSWER),
                                          _receipt('c_ans'),
                                          _send('c_nar', NARRATION),
                                          _receipt('c_nar'), _verdict()])
        assert len(seat.messages) == 1, 'the round was steered, as live'
        assert _reply(rr, chat) == ANSWER, _reply(rr, chat)

    def test_an_undeclared_narration_does_not_beat_the_answer(self, rr, monkeypatch):
        """hartos-43: send_is_ask counts an undeclared send as an ask; the
        reply pick does not, or the R2 fix held only when the 4B declared."""
        chat, _seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                           round_messages=[_send('c_ans', ANSWER),
                                           _receipt('c_ans'),
                                           _send('c_nar', NARRATION, _UNDECLARED),
                                           _receipt('c_nar'), _verdict()])
        assert _reply(rr, chat) == ANSWER, _reply(rr, chat)

    def test_a_declared_question_after_the_answer_is_the_reply(self, rr, monkeypatch):
        """The price of reading only a DECLARED question as one: an answer
        followed by a declared courtesy question replies with the question."""
        chat, _seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                           round_messages=[_send('c_ans', ANSWER),
                                           _receipt('c_ans'),
                                           _send('c_q', COURTESY, True),
                                           _receipt('c_q'), _verdict()])
        assert _reply(rr, chat) == COURTESY, _reply(rr, chat)

    def test_an_undeclared_courtesy_line_does_not_beat_the_answer(self, rr, monkeypatch):
        chat, _seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                           round_messages=[_send('c_ans', ANSWER),
                                           _receipt('c_ans'),
                                           _send('c_q', COURTESY, _UNDECLARED),
                                           _receipt('c_q'), _verdict()])
        assert _reply(rr, chat) == ANSWER, _reply(rr, chat)

    def test_a_send_from_before_the_steer_is_not_the_rounds(self, rr, monkeypatch):
        """7188e162e's probe: a progress note went out before the action gave
        up, and the round wrote nothing.  The note is not the reply."""
        chat, seat = _run(rr, monkeypatch,
                          [_seed(TURN), _send('c_pre', PROGRESS),
                           _receipt('c_pre'), _verdict()],
                          round_messages=[_verdict()])
        assert len(seat.messages) == 1
        assert _reply(rr, chat) != PROGRESS

    def test_a_waiting_card_beats_a_narration_sent_after_it(self, rr, monkeypatch):
        """11-17's rule: the card's question is the reply over a later
        narration send."""
        q = 'Which of these replies do you like best?'
        chat, _seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                           round_messages=[_card('c_card', q),
                                           _card_result('c_card'),
                                           _send('c_nar', NARRATION),
                                           _receipt('c_nar'), _verdict()])
        assert _reply(rr, chat) == q, _reply(rr, chat)


def test_longest_answer_takes_the_longest_and_a_tie_goes_to_the_newer():
    from hartos.lifecycle_hooks import longest_answer
    assert longest_answer([]) is None
    assert longest_answer(['short', 'a longer one', 'mid']) == 'a longer one'
    first, second = {'content': 'same'}, {'content': 'same'}
    assert longest_answer([first, second],
                          text_of=lambda m: m['content']) is second


def test_send_declared_ask_reads_only_a_declaration():
    from hartos.lifecycle_hooks import send_declared_ask, send_is_ask
    msgs = [_send('t', 'q?', True), _send('f', 'note', False),
            _send('u', 'note', _UNDECLARED),
            _send('s', 'q?', 'true')]
    assert send_declared_ask(msgs, 't') is True
    assert send_declared_ask(msgs, 's') is True
    assert send_declared_ask(msgs, 'f') is False
    assert send_declared_ask(msgs, 'u') is False
    # completion's fail-safe is untouched: undeclared is still an ask there
    assert send_is_ask(msgs, 'u') is True
