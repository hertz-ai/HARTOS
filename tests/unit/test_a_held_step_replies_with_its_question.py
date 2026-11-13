"""A turn that holds for the person's answer replies with the question it sent.

MEASURED LIVE 2026-11-12 21:17:28-21:18:38 IST on Nunba build 72, agent
1793366231447 (speech therapy, family names), a test learner choosing the
Parent Helper persona:

    21:17:43  send_message_to_user "Please tell me Arjun's mother and father names"
    21:17:44  [REUSE-WAIT] action 1 holds for the person's reply; ledger block taken
    21:17:44  [SYNTHESIS] reply would be raw control JSON - asking for the
              user-facing answer
    21:18:38  [SYNTHESIS] round returned (8 -> 18 msgs); still control JSON: True
    reply     {"status": "requires_breakdown", "action": "ask_person", ...}

The step had done its job and was waiting on the person.  The verifier's
verdict was still the tail, so the synthesis gate steered another round, and
that round still had the agent's tools: a second, meta message went out
("Hello! I'm Speech Therapy Flow 2 Family Members ..."), two ask cards were
opened (one refused), 54 s passed, and the reply the person read was the raw
control JSON.

The fix reads what the turn's exit hold already recorded (the held step and
its open ask, put in this turn's request, how it reached the person and the
question's text) and makes that question the reply, with no steer.  These
tests drive the REAL ``_reuse_synthesis_turn``; the held-step and open-ask
readers are replaced at the module boundary (they have their own tests), and
the steering seat records instead of sending, as the sibling suites do.

    python -m pytest tests/unit/test_a_held_step_replies_with_its_question.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

SESSION = '69638511-1b22-4149-852a-357fa816b518_1793366231447'
THIS_TURN = 'msg_1794498442974_sim'
QUESTION = "Please tell me Arjun's mother and father names"

# The live shape of action 1's window: the dispatch, the model's send, the
# send receipt, and the verifier's verdict as the tail.
DISPATCH = {
    'role': 'user', 'name': 'ChatInstructor',
    'content': ("Perform this action -> Action #1:send_message_to_user with "
                "text 'Please tell me Arjun's mother and father names' to "
                "parent"),
}
SEND_CALL = {
    'role': 'assistant', 'name': 'Assistant', 'content': None,
    'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {
        'name': 'send_message_to_user',
        'arguments': '{"text": "%s", "expects_reply": true}' % QUESTION}}],
}
RECEIPT = {
    'role': 'tool', 'tool_call_id': 'call_1', 'name': 'send_message_to_user',
    'content': ('Message sent successfully to user with request_id: '
                + THIS_TURN),
}
VERDICT = {
    'role': 'user', 'name': 'StatusVerifier',
    'content': ('{"status": "completed", "action_id": 1, "message": "The '
                'question was sent to the parent."}'),
}


NARRATION = {
    'role': 'assistant', 'name': 'Assistant',
    'content': ("I see that the conversation is currently waiting for Arjun "
                "(the child) to respond to the question about whether the "
                "person is \"Amma\" or \"Appa.\" Since the child hasn't "
                "responded yet, I should wait for their reply."),
}


def _history():
    return [dict(DISPATCH), dict(SEND_CALL), dict(RECEIPT), dict(VERDICT)]


class _Task:
    def __init__(self):
        self.current_action = 1
        self.actions = [{'action': 'step %d' % i,
                         'can_perform_without_user_input': 'no' if i == 1 else 'yes'}
                        for i in range(1, 6)]
        self.evidence_vacuous_action = None

    def get_action(self, i):
        return self.actions[i]


class _Chat:
    def __init__(self, messages):
        self.agents = []
        self.messages = list(messages)


class _Manager:
    def __init__(self):
        self._oai_messages = {}


class _Recorder:
    """Stands in for chat_instructor; captures the steer instead of sending."""

    def __init__(self):
        self.messages = []

    def initiate_chat(self, recipient=None, message=None, **kw):
        self.messages.append(message)


@pytest.fixture
def rr():
    import hartos.reuse_recipe as mod          # a skip here would be vacuous
    return mod


def _ask(**over):
    ask = {'action_id': 1, 'flow': 0, 'request_id': THIS_TURN,
           'message_index': 2, 'via': 'send', 'question': QUESTION,
           'answered': False}
    ask.update(over)
    return ask


def _run(rr, monkeypatch, held=1, ask=None, this_turn=THIS_TURN, history=None):
    monkeypatch.setattr(rr, '_reuse_outstanding_tools',
                        lambda *a, **k: [], raising=True)
    monkeypatch.setattr(rr, '_reuse_held_action',
                        lambda user_prompt: held, raising=True)
    monkeypatch.setattr(rr, 'ledger_open_ask',
                        lambda user_prompt, action_id, flow: ask, raising=True)
    monkeypatch.setattr(rr, '_reuse_action_flow',
                        lambda user_prompt, action_id: 0, raising=True)
    monkeypatch.setattr(rr, 'request_id_list',
                        {SESSION: this_turn} if this_turn else {}, raising=True)
    monkeypatch.setitem(rr.user_tasks, SESSION, _Task())
    chat = _Chat(history if history is not None else _history())
    rec = _Recorder()
    posted = rr._reuse_synthesis_turn(SESSION, chat, _Manager(), rec)
    return posted, chat, rec


class TestAHeldStepRepliesWithItsQuestion:

    def test_the_question_is_the_reply_and_no_round_is_steered(
            self, rr, monkeypatch):
        """THE DEFECT.  RED before the fix: the gate posted the synthesis
        steer (another round with tools) and the verdict stayed the tail."""
        posted, chat, rec = _run(rr, monkeypatch, ask=_ask())
        assert rec.messages == [], (
            'the step holds for the person with its question out this turn, '
            'and the gate still steered a round (live 21:17:44: 54 s, a '
            'second meta send, two ask cards)')
        assert posted is False
        assert chat.messages[-1]['content'] == QUESTION
        assert chat.messages[-1]['name'] == 'Assistant'
        # The extractor's own gate takes it as the person's answer, so it is
        # delivered rather than walked past.
        assert rr._reuse_message_is_user_answer(chat.messages[-1]) is True

    def test_a_question_returned_as_the_reply_counts_too(self, rr, monkeypatch):
        _posted, chat, rec = _run(rr, monkeypatch, ask=_ask(via='reply'))
        assert rec.messages == []
        assert chat.messages[-1]['content'] == QUESTION

    def test_a_narration_tail_is_replaced_by_the_question(self, rr, monkeypatch):
        """THE SECOND SHAPE, live 22:4x IST on build 74 (18582f23e): the
        tail was the model's narration, which reads as an answer, so the
        gate returned before the held-step branch and the narration was the
        reply.  RED on 18582f23e."""
        history = _history()[:-1] + [dict(NARRATION)]
        posted, chat, rec = _run(rr, monkeypatch, ask=_ask(), history=history)
        assert rr._reuse_needs_synthesis(_Chat(history)) is False, (
            'precondition: the narration reads as an answer to the gate')
        assert rec.messages == []
        assert posted is False
        assert chat.messages[-1]['content'] == QUESTION, (
            'the step holds with its question out this turn, and the reply '
            'was the narration written after it')

    def test_a_long_send_replies_with_its_whole_text(self, rr, monkeypatch):
        """Review of f821c3942, MF-2: the ask record keeps the first 1000
        characters of the question (record_ask_delivered), so a 1313-char
        send whose question comes last replied without the question, and TTS
        spoke the cut text.  A sent question is the send's own text, read
        from this turn's window.  RED on f821c3942."""
        import json
        long_text = ('Today we will practise family names together. ' * 27
                     + 'Arjun, who is this, Amma or Appa?')
        assert len(long_text) > 1000 and not long_text[:1000].endswith('Appa?')
        seed = 'seed of ' + THIS_TURN
        send = dict(SEND_CALL, tool_calls=[{
            'id': 'call_1', 'type': 'function', 'function': {
                'name': 'send_message_to_user',
                'arguments': json.dumps({'text': long_text,
                                         'expects_reply': True})}}])
        history = [{'role': 'user', 'name': 'ChatInstructor', 'content': seed},
                   dict(DISPATCH), send, dict(RECEIPT), dict(VERDICT)]
        monkeypatch.setattr(rr, '_reuse_turn_start',
                            {SESSION: {'seed_text': seed}}, raising=True)
        posted, chat, rec = _run(rr, monkeypatch,
                                 ask=_ask(question=long_text[:1000]),
                                 history=history)
        assert rec.messages == []
        assert posted is False
        assert chat.messages[-1]['content'] == long_text, (
            'the reply is the record\'s 1000-char cut, and the question at '
            'the end of the send is lost')

    def test_with_no_send_in_this_turn_the_record_is_the_reply(
            self, rr, monkeypatch):
        """The record's question stays the answer when this turn's window
        holds no delivered send (no seed recorded: the reader finds none)."""
        monkeypatch.setattr(rr, '_reuse_turn_start', {}, raising=True)
        _posted, chat, rec = _run(rr, monkeypatch, ask=_ask())
        assert rec.messages == []
        assert chat.messages[-1]['content'] == QUESTION

    def test_a_question_returned_as_the_reply_never_takes_a_send(
            self, rr, monkeypatch):
        """Only a question that went out by send is read from the send: a
        question that was the reply itself keeps the record's text, whatever
        else the turn sent."""
        seed = 'seed of ' + THIS_TURN
        history = [{'role': 'user', 'name': 'ChatInstructor', 'content': seed}
                   ] + _history()
        monkeypatch.setattr(rr, '_reuse_turn_start',
                            {SESSION: {'seed_text': seed}}, raising=True)
        returned = 'Arjun, who is this, Amma or Appa?'
        _posted, chat, rec = _run(rr, monkeypatch,
                                  ask=_ask(via='reply', question=returned),
                                  history=history)
        assert rec.messages == []
        assert chat.messages[-1]['content'] == returned

    def test_a_tail_that_already_is_the_question_is_not_doubled(
            self, rr, monkeypatch):
        history = _history()[:-1] + [{'role': 'assistant', 'name': 'Assistant',
                                       'content': QUESTION}]
        _posted, chat, rec = _run(rr, monkeypatch, ask=_ask(), history=history)
        assert rec.messages == []
        assert len(chat.messages) == len(history)
        assert chat.messages[-1]['content'] == QUESTION


class TestEverythingElseSteersAsBefore:

    def _assert_steered(self, posted, chat, rec):
        assert len(rec.messages) == 1, 'the gate should steer as before'
        assert posted is True
        assert not any(m.get('content') == QUESTION for m in chat.messages), (
            'the question was made the reply on a turn that does not hold '
            'with its question out')

    def test_no_held_step(self, rr, monkeypatch):
        self._assert_steered(*_run(rr, monkeypatch, held=None, ask=_ask()))

    def test_an_ask_put_in_an_earlier_turn(self, rr, monkeypatch):
        self._assert_steered(*_run(rr, monkeypatch,
                                   ask=_ask(request_id='msg_earlier_turn')))

    def test_a_turn_with_no_request_id(self, rr, monkeypatch):
        self._assert_steered(*_run(rr, monkeypatch, ask=_ask(request_id=''),
                                   this_turn=''))

    def test_an_ask_that_did_not_reach_the_person(self, rr, monkeypatch):
        for via in ('written', None, 'escalation'):
            self._assert_steered(*_run(rr, monkeypatch, ask=_ask(via=via)))

    def test_an_ask_with_no_question_text(self, rr, monkeypatch):
        for question in (None, '', '   '):
            self._assert_steered(*_run(rr, monkeypatch,
                                       ask=_ask(question=question)))

    def test_no_open_ask(self, rr, monkeypatch):
        self._assert_steered(*_run(rr, monkeypatch, ask=None))

    def test_without_a_held_step_a_written_answer_stays(self, rr, monkeypatch):
        """The branch runs first now; a turn that is not held keeps the
        answer its group wrote."""
        history = _history()[:-1] + [dict(NARRATION)]
        posted, chat, rec = _run(rr, monkeypatch, held=None, ask=_ask(),
                                 history=history)
        assert rec.messages == []
        assert posted is False
        assert chat.messages[-1]['content'] == NARRATION['content']

    def test_a_reader_that_fails_steers_instead_of_raising(
            self, rr, monkeypatch):
        def boom(user_prompt):
            raise RuntimeError('ledger unreadable')
        monkeypatch.setattr(rr, '_reuse_outstanding_tools',
                            lambda *a, **k: [], raising=True)
        monkeypatch.setattr(rr, '_reuse_held_action', boom, raising=True)
        monkeypatch.setitem(rr.user_tasks, SESSION, _Task())
        chat, rec = _Chat(_history()), _Recorder()
        posted = rr._reuse_synthesis_turn(SESSION, chat, _Manager(), rec)
        self._assert_steered(posted, chat, rec)
