"""A turn whose question went out on an ask_person card replies with that
question, not with the model's narration about the card.

MEASURED LIVE 2026-11-17 on Nunba build 75 (local 4B), the speech therapy
agent 1794540972213, request msg_1794887926342_sim (server.log):

    09:29:30  [REUSE-VERIFY] action 3 ... recording GAVE_UP for retry
    09:29:31  [SYNTHESIS] reply would be raw control JSON - asking for the
              user-facing answer (... unfinished=['action 3 gave up without a
              verified result', 'actions 4 to 4 were not reached'])
    09:29:37  the steered round called ask_person: refused (not_an_object)
    09:29:44  person ask 41e28c4c... (form) to user ...: card, row and push
              sent; the tool answered status 'waiting_for_person'
              (the card asked "What is your mother called in Tamil?")
              then the Assistant wrote "Good! The person is now being asked
              the question. ..." and the verifier a pending verdict
    09:30:33  the Helper called ask_person again: refused (not_an_object)
    09:30:47  [SYNTHESIS] round returned (47 -> 57 msgs); still control JSON:
              True
    09:30:47  [SYNTHESIS] tail is not an answer (Assistant); delivering the
              last real one instead (... head='Good! The person is now being
              asked the question. ...')

The person in the chat read the agent's narration about its own card.  The
reply rules (_reuse_synthesis_turn, the finaliser) already make a send that
asks the person the reply over the group's words about it; they read only
send_message_to_user (_reuse_turn_delivered_send), so the card, the other
way an agent asks, was invisible to them.

The fix: an ask_person card still waiting for the person counts there as a
send that asks, and its question (read off the call's own component, the
card person_ask showed) is the reply.  An answered, dismissed or refused
card is not, nor a call that only reads an earlier ask.

These tests drive the REAL _reuse_synthesis_turn; the steering seat records
the steer and plays the round into the group log, as the sibling suite
test_a_reply_is_the_send_not_its_narration.py does.  What the person reads
is taken in the extractor's own order (get_agent_response, after the
synthesis turn).  Nothing leaves the process: no card is pushed, no row
written, no model called.

    python -m pytest tests/unit/test_a_question_card_is_the_reply_not_its_narration.py -q --noconftest
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

SPEECH = 'f4f3ba0a-591e-4971-80bb-e87ae519940a_1794540972213'
TURN = 'msg_1794887926342_sim'
EARLIER_TURN = 'msg_1794887800000_sim'
ASK_ID = '41e28c4c3fcf45ecb63ad14d8994a879'

# The live texts, from the calls' own arguments in server.log.
QUESTION = 'What is your mother called in Tamil?'
SUPER = 'சூப்பர்!'
NARRATION = ("Good! The person is now being asked the question. The answer "
             "is not yet available, so this step is waiting. According to the "
             "guidelines, I should wait for the person's answer before "
             "proceeding. Since the answer is not yet available, I cannot "
             "complete the current action (Action #3) which requires waiting "
             "for the person's response.")
HONEST = ("I could not finish this step: the family names lesson stopped "
          "before Arjun answered, and the next step was not done.")

WAITING = {'answered': False, 'ask_id': ASK_ID, 'status': 'waiting_for_person',
           'note': ('The question is on their devices and they have not '
                    'answered yet. This step is not done: read the answer '
                    'later with ask_person(ask_id=...).')}
ANSWERED = {'answered': True, 'ask_id': ASK_ID, 'answer': {'answer': 'Amma'},
            'status': 'answered'}
REFUSED = {'answered': False, 'refused': 'not_an_object', 'status': 'refused',
           'note': ('Not sent: component must be a JSON object with a '
                    '"type"; it has no "type".')}

THE_CARD = {'type': 'form', 'description': QUESTION,
            'fields': [{'name': 'answer', 'label': QUESTION, 'type': 'text'}]}


def _seed(turn=TURN):
    return {'role': 'user', 'name': 'ChatInstructor',
            'content': f'Perform this action -> Action #3 for request {turn}'}


def _send(call_id, text, expects_reply=False):
    return {'role': 'assistant', 'name': 'Assistant', 'content': '',
            'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
                'name': 'send_message_to_user',
                'arguments': json.dumps({'text': text,
                                         'expects_reply': expects_reply})}}]}


def _receipt(call_id, turn=TURN):
    return {'role': 'tool', 'name': 'Assistant', 'tool_call_id': call_id,
            'content': 'Message sent successfully to user with request_id: '
                       + turn}


def _card_call(call_id, component, words='', name='Assistant', **args):
    """An ask_person call as the model writes it: the component is a JSON
    string inside the arguments, as in the live log."""
    arguments = {'component': (json.dumps(component)
                               if isinstance(component, dict) else component),
                 'wait_seconds': 20}
    arguments.update(args)
    return {'role': 'assistant', 'name': name, 'content': words,
            'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
                'name': 'ask_person', 'arguments': json.dumps(arguments)}}]}


def _card_result(call_id, reply):
    body = json.dumps(reply)
    return {'role': 'tool', 'name': 'Assistant', 'content': body,
            'tool_responses': [{'tool_call_id': call_id, 'role': 'tool',
                                'content': body}]}


def _verdict(status='pending', message='Waiting for the person.'):
    return {'role': 'user', 'name': 'StatusVerifier',
            'content': json.dumps({'status': status, 'action_id': 3,
                                   'message': message})}


def _prose(text, name='Assistant'):
    return {'role': 'assistant', 'name': name, 'content': text}


class _Task:
    def __init__(self, current_action=3, total=4):
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
    """The steering seat: records the steer and plays the round into the
    group log (the sync then finds nothing newer)."""

    def __init__(self, chat, round_messages=()):
        self.chat = chat
        self.round_messages = [dict(m) for m in round_messages]
        self.messages = []

    def initiate_chat(self, recipient=None, message=None, **kw):
        self.messages.append(message)
        self.chat.messages.extend(self.round_messages)


@pytest.fixture
def rr():
    import hartos.reuse_recipe as mod          # a skip here would be vacuous
    return mod


def _run(rr, monkeypatch, history, round_messages=(), state='in_progress',
         turn=TURN):
    seed = next(m['content'] for m in reversed(history)
                if m.get('name') == 'ChatInstructor'
                and turn in str(m.get('content')))
    monkeypatch.setattr(rr, '_reuse_turn_start',
                        {SPEECH: {'seed_text': seed}}, raising=True)
    monkeypatch.setattr(rr, 'request_id_list', {SPEECH: turn}, raising=True)
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
    monkeypatch.setitem(rr.user_tasks, SPEECH, _Task())
    chat = _Chat(history)
    seat = _Round(chat, round_messages)
    posted = rr._reuse_synthesis_turn(SPEECH, chat, _Manager(), seat)
    return posted, chat, seat


def _what_the_person_reads(rr, chat):
    """The extractor's order after the synthesis turn (get_agent_response):
    the tail when it is an answer; else this turn's newest answer; else this
    turn's prose the answer rules refused; else its newest delivered send."""
    msgs = chat.messages
    if rr._reuse_message_is_user_answer(msgs[-1]):
        return msgs[-1]['content']
    start = rr._reuse_turn_seed_index(SPEECH, msgs)
    for cand in reversed(msgs[(start + 1) if start is not None else 0:]):
        if rr._reuse_message_is_user_answer(cand):
            return cand['content']
    picked = rr._reuse_turn_refused_prose(SPEECH, msgs)
    if picked is not None:
        return picked['content']
    return rr._reuse_turn_delivered_send(SPEECH, msgs)


def _before_the_steer():
    """Action 3's turn up to the synthesis: one delivered send that asks
    nothing ("Super!"), then the verdict the turn ended on."""
    return [_seed(), _send('call_super', SUPER), _receipt('call_super'),
            _verdict('completed', "Message '%s' sent." % SUPER)]


def _the_live_round():
    """The steered round as the 09:29:37-09:30:47 log shows it."""
    return [
        _card_call('call_r1',
                   {'description': 'Please tell me in Tamil who your mother '
                                   'is called',
                    'fields': [{'label': 'Answer', 'name': 'answer',
                                'type': 'text'}],
                    'purpose': 'Testing Tamil language skills'},
                   words='I understand now. I need to ask the user (the child) '
                         'a question about their family members.'),
        _card_result('call_r1', REFUSED),
        _card_call('call_r2', THE_CARD,
                   words='I need to format the component correctly for the '
                         'ask_person tool. Let me try again.'),
        _card_result('call_r2', WAITING),
        _prose(NARRATION),
        _verdict('pending', "The question '%s' has been sent to the child "
                            "(ask_id: %s), but the answer has not been "
                            "received yet." % (QUESTION, ASK_ID)),
        # The Helper put the ask id INSIDE the component: a new ask with no
        # type, refused, so nothing new reached the person.
        _card_call('call_r3',
                   {'description': 'Waiting for answer to family member '
                                   'question',
                    'purpose': 'chat_attachment', 'ask_id': ASK_ID},
                   words='The question has been sent to the child, but the '
                         'answer has not been received yet.',
                   name='Helper', wait_seconds=60),
        _card_result('call_r3', REFUSED),
    ]


def _a_card_turn(*after_the_card, card=None, reply=None):
    """Action 3 still working: its card went out this turn, then whatever
    ``after_the_card`` holds, then a pending verdict."""
    return ([_seed(),
             _card_call('call_card', card or THE_CARD),
             _card_result('call_card', reply or WAITING)]
            + list(after_the_card)
            + [_verdict()])


class TestTheCardsQuestionIsTheReply:

    def test_the_live_turn_replies_with_the_cards_question(
            self, rr, monkeypatch):
        """09:29:31-09:30:47: action 3 gave up, the steered round put the
        question on the child's card and then narrated it.  The person read
        the narration.  RED before the fix."""
        posted, chat, seat = _run(rr, monkeypatch, _before_the_steer(),
                                  round_messages=_the_live_round(),
                                  state='gave_up')
        assert len(seat.messages) == 1, (
            'nothing had asked the person before the steer: the round runs')
        assert posted is True
        assert _what_the_person_reads(rr, chat) == QUESTION, (
            "the person read the model's narration about its own card")
        assert chat.messages[-1]['content'] == QUESTION

    def test_a_card_the_working_action_waits_on_ends_the_turn(
            self, rr, monkeypatch):
        """Action 3 still in progress and its card waits for the person: the
        card's question is what the action waits on, as a send that asks
        would be; no round is steered to describe it.  RED before the fix."""
        posted, chat, seat = _run(rr, monkeypatch,
                                  _a_card_turn(_prose(NARRATION)),
                                  round_messages=[_prose(NARRATION)])
        assert seat.messages == [], (
            'a round was steered for a turn whose question is on the card')
        assert posted is False
        assert _what_the_person_reads(rr, chat) == QUESTION

    def test_the_question_is_the_fields_label_not_the_reason(
            self, rr, monkeypatch):
        """The card's reason says why it asks; the field's label is what it
        asks.  The label goes out, even when the reason ends in a question
        mark too.  RED before the fix."""
        card = {'type': 'form', 'description': 'Can I ask you about your family?',
                'fields': [{'name': 'answer', 'label': QUESTION,
                            'type': 'text'}]}
        _posted, chat, seat = _run(rr, monkeypatch, _a_card_turn(card=card),
                                   round_messages=[_prose(NARRATION)])
        assert seat.messages == []
        assert _what_the_person_reads(rr, chat) == QUESTION

    def test_an_approval_asks_in_its_reason(self, rr, monkeypatch):
        """An approval has no fields: its question is its reason.  RED before
        the fix."""
        ask = 'May I listen to Arjun say Amma now?'
        _posted, chat, seat = _run(
            rr, monkeypatch,
            _a_card_turn(card={'type': 'approval', 'description': ask}),
            round_messages=[_prose(NARRATION)])
        assert seat.messages == []
        assert _what_the_person_reads(rr, chat) == ask

    def test_a_file_ask_says_what_it_asks_for(self, rr, monkeypatch):
        """A file ask need not end in a question mark (person_ask lets it
        out): its reason says what it asks for, and that is the reply.  RED
        before the fix."""
        ask = 'Send a photo of Amma so Arjun can name her.'
        card = {'type': 'file_request', 'purpose': 'chat_attachment',
                'description': ask}
        _posted, chat, seat = _run(rr, monkeypatch, _a_card_turn(card=card),
                                   round_messages=[_prose(NARRATION)])
        assert seat.messages == []
        assert _what_the_person_reads(rr, chat) == ask

    def test_reading_the_card_again_does_not_hide_its_question(
            self, rr, monkeypatch):
        """The model read its card's answer by ask_id and it still waits.
        That call put no new card out; its own component (the live Helper's
        words) is not the question, the card's is.  RED before the fix."""
        read = _card_call('call_read',
                          {'description': 'Waiting for answer to family '
                                          'member question'},
                          ask_id=ASK_ID, wait_seconds=60)
        _posted, chat, seat = _run(
            rr, monkeypatch,
            _a_card_turn(read, _card_result('call_read', WAITING)),
            round_messages=[_prose(NARRATION)])
        assert seat.messages == []
        assert _what_the_person_reads(rr, chat) == QUESTION


class TestWhatStaysAsItWas:

    def test_a_card_answered_in_its_own_call_is_not_the_reply(
            self, rr, monkeypatch):
        """The person answered within the call's wait: nothing waits on the
        card, so the turn goes on as before (the steer, then its answer)."""
        posted, chat, seat = _run(rr, monkeypatch,
                                  _a_card_turn(reply=ANSWERED),
                                  round_messages=[_prose(HONEST)])
        assert len(seat.messages) == 1
        assert posted is True
        assert _what_the_person_reads(rr, chat) == HONEST

    def test_a_card_answered_later_in_the_turn_is_not_the_reply(
            self, rr, monkeypatch):
        """A newer read of the same ask found it answered: the card's
        question is no longer open."""
        read = _card_call('call_read', '', ask_id=ASK_ID)
        posted, chat, seat = _run(
            rr, monkeypatch,
            _a_card_turn(read, _card_result('call_read', ANSWERED)),
            round_messages=[_prose(HONEST)])
        assert len(seat.messages) == 1
        assert posted is True
        assert _what_the_person_reads(rr, chat) == HONEST

    def test_a_refused_card_is_not_the_reply(self, rr, monkeypatch):
        """A refused card reached nobody."""
        posted, chat, seat = _run(rr, monkeypatch,
                                  _a_card_turn(reply=REFUSED),
                                  round_messages=[_prose(HONEST)])
        assert len(seat.messages) == 1
        assert posted is True
        assert _what_the_person_reads(rr, chat) == HONEST

    def test_a_card_before_a_failure_is_not_the_reply(self, rr, monkeypatch):
        """The action gave up after its card went out: the honest report the
        steer asks for is the reply, as for a send that asked before a
        failure (review of 7188e162e)."""
        posted, chat, seat = _run(rr, monkeypatch, _a_card_turn(),
                                  round_messages=[_prose(HONEST)],
                                  state='gave_up')
        assert len(seat.messages) == 1
        assert 'action 3 gave up' in seat.messages[0]
        assert posted is True
        assert _what_the_person_reads(rr, chat) == HONEST

    def test_a_newer_send_that_asks_nothing_is_still_the_newest(
            self, rr, monkeypatch):
        """The newest thing that reached the person was a send that asks
        nothing: the action does not wait on it, so the honest report is
        asked for, as before."""
        posted, chat, seat = _run(
            rr, monkeypatch,
            _a_card_turn(_send('call_late', SUPER), _receipt('call_late')),
            round_messages=[_prose(HONEST)])
        assert len(seat.messages) == 1
        assert posted is True
        assert _what_the_person_reads(rr, chat) == HONEST

    def test_an_earlier_turns_card_is_not_this_turns(self, rr, monkeypatch):
        """The card sits before this request's seed: it belongs to an
        earlier turn, whose reply the person already read."""
        history = ([_seed(EARLIER_TURN), _card_call('call_old', THE_CARD),
                    _card_result('call_old', WAITING), _verdict()]
                   + [_seed(), _verdict()])
        posted, chat, seat = _run(rr, monkeypatch, history,
                                  round_messages=[_prose(HONEST)])
        assert len(seat.messages) == 1
        assert posted is True
        assert _what_the_person_reads(rr, chat) == HONEST
