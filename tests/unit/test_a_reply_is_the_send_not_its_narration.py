"""A turn whose words reached the person through the send tool replies with
those words, not with the model's description of them.

MEASURED LIVE 2026-11-13 on Nunba build 75 (test account learner1), three
turns whose sends had reached the person (server.log):

    18:48:04  singing agent 1793373034536, local 4B: a send of the rhyme
              reached the person; action 1 did not finish
    18:48:41  [SYNTHESIS] reply would be raw control JSON - asking for the
              user-facing answer (... unfinished=['action 1 did not finish', ...])
    18:49:37  [SYNTHESIS] tail is not an answer (StatusVerifier); delivering
              the last real one instead (... head='I see the system is now
              waiting for Arjun to respond to the question "Sing a rhyme". ...')

    18:49:50  same agent, next turn: action 1 gave up, no send yet; the steer
    18:50:07  the steered round SENT the greeting and "Twinkle, Twinkle"
    18:50:28  [SYNTHESIS] tail is not an answer (Assistant); delivering the
              last real one instead (... head='I\\'ve sent you a warm greeting
              and a fun rhyme about "Twinkle twinkle little star" ...')

    15:56:21-31  Tamil speech agent on Claude (session 10202_343): three sends
              reached the person ("அர்ஜுன்!", "அர்ஜுன், இங்கே பாரு!",
              "அர்ஜுன்! உனக்கு கண்ணாடி தெரிகிறதா?")
    15:56:34  [SYNTHESIS] reply would be raw control JSON - asking ...
    15:56:39  round returned (9 -> 11 msgs); still control JSON: False, and the
              reply was "அர்ஜுனுக்கு மூன்று செய்திகளை அனுப்பினேன்: ..."
              ("I sent Arjun three messages: ...")

The fix in _reuse_synthesis_turn: when this turn's sends reached the person,
the newest of them is the reply (_reuse_turn_delivered_send, the finaliser's
answer for such a turn), with no steer and no recovery of group prose; a send
made in the steered round is the reply over the round's words.  Not when a
tool the step named did not run (the honest report goes out, #808), and the
no-data report still outranks it.

These tests drive the REAL _reuse_synthesis_turn; the steering seat records
the steer and plays the round the live log shows, as the sibling suites do.
What the person reads is then taken in the extractor's own order
(get_agent_response, after the synthesis turn), from the module's own
predicates.

    python -m pytest tests/unit/test_a_reply_is_the_send_not_its_narration.py -q --noconftest
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

SING = 'f4f3ba0a-591e-4971-80bb-e87ae519940a_1793373034536'
SING_TURN_1 = 'msg_1794575855345_sim'
SING_TURN_2 = 'msg_1794575981768_sim'
TAMIL = '10202_343'
TAMIL_TURN = '4e5b5a59-337a-4e8d-99c0-57bf56e37325'

# The live texts, from the calls' own arguments in server.log.
RHYME_SEND = ("Hi Arjun! \U0001f31f Let's sing together!\n\nHere's a fun rhyme "
              "for you:\n\nThe red ball, bouncing high,\nBounces down, oh what "
              "a joy!\nCan you sing the first line, Arjun?\nLet's hear you "
              "sing it with fun!")
WORKING_NOTES = ("I see the system is now waiting for Arjun to respond to the "
                 "question \"Sing a rhyme\". The action to ask him is pending "
                 "his response on his device. I am waiting for Arjun to answer "
                 "the question on his device.")
TWINKLE_SEND = ("Hey there! I'm Singing Agent Rhyme and Sing Along, and I'm "
                "genuinely excited to work with you on this.\n\nLet's sing "
                "\"Twinkle, Twinkle, Little Star\"! \U0001f31f\n\nHere's the "
                "rhyme for you to sing along:\n\nTwinkle, twinkle, little star,"
                "\nHow I wonder what you are!\nUp above the world so high,\n"
                "Like a diamond in the sky.\n\nWould you like to sing the first "
                "line with me?\n\"Twinkle, twinkle, little star\" - ready to try?")
SENT_NARRATION = ("I've sent you a warm greeting and a fun rhyme about "
                  "\"Twinkle twinkle little star\" - ready for you to sing "
                  "along! \U0001f31f\n\nI'm now waiting for your reply - please "
                  "respond with your answer when you're ready to sing the "
                  "first line!")
TAMIL_SENDS = ('அர்ஜுன்!', 'அர்ஜுன், இங்கே பாரு!',
               'அர்ஜுன்! உனக்கு கண்ணாடி தெரிகிறதா?')
# Its head is the reply's head as logged; the log cuts it after the third send.
TAMIL_NARRATION = ("அர்ஜுனுக்கு மூன்று செய்திகளை அனுப்பினேன்: 'அர்ஜுன்!', "
                   "'அர்ஜுன், இங்கே பாரு!', 'அர்ஜுன்! உனக்கு கண்ணாடி தெரிகிறதா?'")
# The speech agent's 05:26:39 turn (10202_1793366231447): the action's own
# prose about its save, recovered as the reply (picker=action) after the send.
SAVE_NOTES = ("Great! I've successfully stored Arjun's correct response "
              "(\"Amma\") for his mother in the memory system. The family "
              "association record now shows:\n- Result: correct\n- Timestamp: "
              "2024-01-15T10:30:00Z\n- Child's response: Amma\n- Target: mother"
              "\n\nThis will help track Arjun's progress in learning to identify "
              "his family members! Would you like to continue with another "
              "family member, or would you prefer to focus on something else "
              "today?")
APPA_SEND = ("Arjun, now let's learn the name of your father. Who is Appa? "
             "Please tell me the word for \"father\" in your language.")


def _seed(turn):
    return {'role': 'user', 'name': 'ChatInstructor',
            'content': f'Perform this action -> Action #1 for request {turn}'}


def _send(call_id, text, expects_reply=True, name='Assistant'):
    return {'role': 'assistant', 'name': name, 'content': '',
            'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
                'name': 'send_message_to_user',
                'arguments': json.dumps({'text': text,
                                         'expects_reply': expects_reply})}}]}


def _receipt(call_id, turn, body=None):
    return {'role': 'tool', 'name': 'Assistant', 'tool_call_id': call_id,
            'content': body or ('Message sent successfully to user with '
                                'request_id: ' + turn)}


def _verdict(status='pending', message='Waiting for the person.'):
    return {'role': 'user', 'name': 'StatusVerifier',
            'content': json.dumps({'status': status, 'action_id': 1,
                                   'message': message})}


def _prose(text, name='Assistant'):
    return {'role': 'assistant', 'name': name, 'content': text}


class _Task:
    def __init__(self, current_action=1, total=8):
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
    """The steering seat: records the steer and plays the round the live log
    shows into the group log (the sync then finds nothing newer)."""

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


def _run(rr, monkeypatch, session, turn, history, round_messages=(),
         state='in_progress', unrun=(), task=None, held=None, ask=None):
    seed = next(m['content'] for m in reversed(history)
                if m.get('name') == 'ChatInstructor'
                and turn in str(m.get('content')))
    monkeypatch.setattr(rr, '_reuse_turn_start',
                        {session: {'seed_text': seed}}, raising=True)
    monkeypatch.setattr(rr, 'request_id_list', {session: turn}, raising=True)
    monkeypatch.setattr(rr, '_reuse_outstanding_tools',
                        lambda *a, **k: list(unrun), raising=True)
    monkeypatch.setattr(rr, '_reuse_held_action',
                        lambda user_prompt: held, raising=True)
    monkeypatch.setattr(rr, 'ledger_open_ask',
                        lambda user_prompt, action_id, flow: ask, raising=True)
    monkeypatch.setattr(rr, '_reuse_action_flow',
                        lambda user_prompt, action_id: 0, raising=True)
    monkeypatch.setattr(rr, '_reuse_action_declares_user_pause',
                        lambda user_prompt, action_id: False, raising=True)
    monkeypatch.setattr(rr, 'get_action_state',
                        lambda user_prompt, action_id: getattr(
                            rr.ActionState, state.upper()), raising=True)
    monkeypatch.setitem(rr.user_tasks, session, task or _Task())
    chat = _Chat(history)
    seat = _Round(chat, round_messages)
    posted = rr._reuse_synthesis_turn(session, chat, _Manager(), seat)
    return posted, chat, seat


def _what_the_person_reads(rr, session, chat):
    """The extractor's order after the synthesis turn (get_agent_response):
    the tail when it is an answer; else this turn's newest answer; else this
    turn's prose the answer rules refused; else its newest delivered send."""
    msgs = chat.messages
    if rr._reuse_message_is_user_answer(msgs[-1]):
        return msgs[-1]['content']
    start = rr._reuse_turn_seed_index(session, msgs)
    for cand in reversed(msgs[(start + 1) if start is not None else 0:]):
        if rr._reuse_message_is_user_answer(cand):
            return cand['content']
    picked = rr._reuse_turn_refused_prose(session, msgs)
    if picked is not None:
        return picked['content']
    return rr._reuse_turn_delivered_send(session, msgs)


def _singing_turn_1():
    return [_seed(SING_TURN_1),
            _send('call_s1', RHYME_SEND), _receipt('call_s1', SING_TURN_1),
            # the model sent it again; the dedupe answered, nothing went out
            _send('call_s2', RHYME_SEND),
            _receipt('call_s2', SING_TURN_1,
                     'Message already sent successfully to user with '
                     'request_id: ' + SING_TURN_1),
            _verdict()]


def _singing_turn_2():
    return _singing_turn_1() + [_prose(WORKING_NOTES), _verdict(),
                                _seed(SING_TURN_2), _verdict('pending')]


def _tamil_turn():
    first = {'role': 'assistant', 'name': 'Assistant', 'content': '',
             'tool_calls': [
                 {'id': 'toolu_a', 'type': 'function', 'function': {
                     'name': 'send_message_to_user',
                     'arguments': json.dumps({'text': TAMIL_SENDS[0],
                                              'expects_reply': True})}},
                 {'id': 'toolu_b', 'type': 'function', 'function': {
                     'name': 'get_data_by_key',
                     'arguments': json.dumps({'key': 'user.name'})}}]}
    first_results = {
        'role': 'tool', 'name': 'Assistant',
        'content': ('Message sent successfully to user with request_id: '
                    + TAMIL_TURN + '\n\n10202\n\nKey not found in stored data.'),
        'tool_responses': [
            {'tool_call_id': 'toolu_a', 'role': 'tool',
             'content': 'Message sent successfully to user with request_id: '
                        + TAMIL_TURN},
            {'tool_call_id': 'toolu_b', 'role': 'tool',
             'content': 'Key not found in stored data.'}]}
    return [_seed(TAMIL_TURN), first, first_results,
            _send('toolu_c', TAMIL_SENDS[1], expects_reply=False),
            _receipt('toolu_c', TAMIL_TURN),
            _verdict(),
            _send('toolu_d', TAMIL_SENDS[2], name='Helper'),
            _receipt('toolu_d', TAMIL_TURN),
            _verdict()]


class TestTheReplyIsTheSendNotItsNarration:

    def test_singing_turn_working_notes_after_a_send(self, rr, monkeypatch):
        """18:48:41-18:49:37: the rhyme reached the person, action 1 did not
        finish, the steer drew the model's working notes and they were the
        reply.  RED before the fix."""
        posted, chat, seat = _run(rr, monkeypatch, SING, SING_TURN_1,
                                  _singing_turn_1(),
                                  round_messages=[_prose(WORKING_NOTES),
                                                  _verdict()])
        assert _what_the_person_reads(rr, SING, chat) == RHYME_SEND, (
            "the person read the model's working notes about the send")
        assert seat.messages == [], (
            'a round was steered for a turn whose words already reached '
            'the person')
        assert posted is False
        assert chat.messages[-1]['content'] == RHYME_SEND

    def test_singing_turn_a_send_made_in_the_steered_round(
            self, rr, monkeypatch):
        """18:49:50-18:50:28: no send before the steer (action 1 gave up);
        the round sent the rhyme, then described it, then called a tool.
        The reply was the description.  RED before the fix."""
        ask_call = {'role': 'assistant', 'name': 'Assistant', 'content': '',
                    'tool_calls': [{'id': 'call_k', 'type': 'function',
                                    'function': {'name': 'ask_person',
                                                 'arguments': '{}'}}]}
        posted, chat, seat = _run(
            rr, monkeypatch, SING, SING_TURN_2, _singing_turn_2(),
            state='gave_up',
            round_messages=[_send('call_t2', TWINKLE_SEND),
                            _receipt('call_t2', SING_TURN_2),
                            _prose(SENT_NARRATION), ask_call])
        assert len(seat.messages) == 1, (
            'no send had gone out before the steer: the round runs as before')
        assert posted is True
        assert _what_the_person_reads(rr, SING, chat) == TWINKLE_SEND, (
            "the person read the model's description of the send it made")

    def test_an_earlier_turns_send_is_not_this_turns(self, rr, monkeypatch):
        """The same turn, bounded at its own seed: turn 1's delivered send
        sits in the group log before it, and is not this turn's words."""
        _posted, chat, seat = _run(rr, monkeypatch, SING, SING_TURN_2,
                                   _singing_turn_2(), state='gave_up')
        assert len(seat.messages) == 1
        assert not any(m.get('content') == RHYME_SEND for m in chat.messages)

    def test_tamil_turn_three_sends_then_their_summary(self, rr, monkeypatch):
        """15:56:21-15:56:39 on Claude: three sends reached the child (one by
        the Helper), the steer drew "I sent Arjun three messages: ...", which
        reads as an answer and was the reply.  RED before the fix."""
        posted, chat, seat = _run(rr, monkeypatch, TAMIL, TAMIL_TURN,
                                  _tamil_turn(),
                                  round_messages=[_prose(TAMIL_NARRATION)])
        assert _what_the_person_reads(rr, TAMIL, chat) == TAMIL_SENDS[2], (
            'the person read a summary of the messages they had just got')
        assert seat.messages == []
        assert posted is False

    def test_a_recovered_note_about_a_save_after_a_send(self, rr, monkeypatch):
        """05:26:39 (10202_1793366231447): every step ran, the action's own
        prose about its save was recovered as the reply although the turn's
        question had gone out by send.  RED before the fix."""
        history = [{'role': 'user', 'name': 'ChatInstructor',
                    'content': 'Perform this action -> Action #5: save the '
                               'answer and ask for Appa ' + 'msg_1794527763725'},
                   {'role': 'assistant', 'name': 'Assistant', 'content': '',
                    'tool_calls': [{'id': 'call_v', 'type': 'function',
                                    'function': {'name': 'save_data_in_memory',
                                                 'arguments': '{}'}}]},
                   {'role': 'tool', 'name': 'Assistant', 'tool_call_id': 'call_v',
                    'content': 'Saved.'},
                   _send('call_p', APPA_SEND),
                   _receipt('call_p', 'msg_1794527763725'),
                   _prose(SAVE_NOTES),
                   _verdict('completed', 'Saved and asked.')]
        posted, chat, seat = _run(rr, monkeypatch, '10202_1793366231447',
                                  'msg_1794527763725', history,
                                  task=_Task(current_action=6, total=5))
        assert seat.messages == []
        assert posted is False
        assert _what_the_person_reads(rr, '10202_1793366231447',
                                      chat) == APPA_SEND, (
            "the person read the agent's note about its own save")


class TestWhatStaysAsItWas:

    def test_a_turn_with_no_send_gets_its_synthesized_answer(
            self, rr, monkeypatch):
        """No send this turn: the steer is posted and the round's answer is
        the reply, as before."""
        answer = json.dumps({'message2userfinal': 'Twinkle means to shine.'})
        history = [_seed(SING_TURN_1), _verdict('completed', 'Done.')]
        posted, chat, seat = _run(
            rr, monkeypatch, SING, SING_TURN_1, history,
            task=_Task(current_action=9, total=8),
            round_messages=[_prose('@user ' + answer)])
        assert len(seat.messages) == 1
        assert seat.messages[0] == rr._REUSE_SYNTHESIS_STEER
        assert posted is True
        assert chat.messages[-1]['content'] == '@user ' + answer

    def test_an_unfinished_turn_with_no_send_steers_the_honest_report(
            self, rr, monkeypatch):
        history = [_seed(SING_TURN_1), _verdict()]
        _posted, chat, seat = _run(rr, monkeypatch, SING, SING_TURN_1, history,
                                   round_messages=[_prose(WORKING_NOTES)])
        assert len(seat.messages) == 1
        assert 'action 1 did not finish' in seat.messages[0]
        assert chat.messages[-1]['content'] == WORKING_NOTES

    def test_a_tool_that_did_not_run_keeps_the_honest_report(
            self, rr, monkeypatch):
        """A send went out, but a tool the step named did not run: the honest
        report is asked for (#808), whatever was sent, and the round's
        answer stays the reply."""
        report = 'I could not look that up: the search did not run.'
        posted, chat, seat = _run(rr, monkeypatch, SING, SING_TURN_1,
                                  _singing_turn_1(), unrun=['google_search'],
                                  round_messages=[_prose(report)])
        assert len(seat.messages) == 1
        assert 'google_search' in seat.messages[0]
        assert posted is True
        assert chat.messages[-1]['content'] == report

    def test_a_send_that_reached_nobody_is_not_the_reply(
            self, rr, monkeypatch):
        history = [_seed(SING_TURN_1), _send('call_f', RHYME_SEND),
                   _receipt('call_f', SING_TURN_1,
                            'Failed to send message to user with request_id: '
                            + SING_TURN_1),
                   _verdict()]
        _posted, chat, seat = _run(rr, monkeypatch, SING, SING_TURN_1, history,
                                   round_messages=[_prose(WORKING_NOTES)])
        assert len(seat.messages) == 1
        assert chat.messages[-1]['content'] == WORKING_NOTES

    def test_the_no_data_report_outranks_a_send(self, rr, monkeypatch):
        task = _Task(current_action=2, total=1)
        task.evidence_vacuous_action = 1
        _posted, chat, seat = _run(rr, monkeypatch, SING, SING_TURN_1,
                                   _singing_turn_1(), task=task)
        assert seat.messages == []
        assert chat.messages[-1]['content'] == rr._REUSE_NO_DATA_REPORT

    def test_a_held_step_still_replies_with_its_question(
            self, rr, monkeypatch):
        """The held step's question, not the turn's newest send (review of
        6e1c93995): a later send in the turn is not the question."""
        later = 'Take your time, Arjun!'
        history = _singing_turn_1()[:3] + [
            _send('call_l', later, expects_reply=False),
            _receipt('call_l', SING_TURN_1), _verdict()]
        ask = {'action_id': 1, 'flow': 0, 'request_id': SING_TURN_1,
               'message_index': 2, 'via': 'send', 'question': RHYME_SEND,
               'answered': False}
        _posted, chat, seat = _run(rr, monkeypatch, SING, SING_TURN_1,
                                   history, held=1, ask=ask)
        assert seat.messages == []
        assert chat.messages[-1]['content'] == RHYME_SEND

    def test_a_tail_that_reads_as_an_answer_is_left_alone(
            self, rr, monkeypatch):
        """The rule lives where the tail is not an answer: a turn that ends
        on prose for the person keeps it, sends or not (a turn may send one
        thing and answer another; only the synthesis asks the model to
        describe what it did)."""
        answer = 'Here is the next line: "How I wonder what you are!"'
        history = _singing_turn_1()[:-1] + [_prose(answer)]
        posted, chat, seat = _run(rr, monkeypatch, SING, SING_TURN_1, history)
        assert seat.messages == []
        assert posted is False
        assert len(chat.messages) == len(history)
        assert chat.messages[-1]['content'] == answer
