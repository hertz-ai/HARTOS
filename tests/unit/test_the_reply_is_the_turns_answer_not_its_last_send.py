"""A turn whose words went out through the send tool replies with its newest
send, in every round, as b06b29405 picks it.

THE LIVE DEFECT, STILL OPEN (source Nunba on the local 4B, 18 Nov 2026
22:26-22:27 IST, research agent 1795014914479, a test account): action 2 gave
up and the synthesis steered a round.  In that round the 4B sent the answer
(about 1,350 characters), then "I'm ready to continue gathering more
information ..." (146), both declared expects_reply=false, and the reply was
the narration.

Two picks that took the answer (98395453b: the longest send everywhere;
971c6049a: the same in the steered round only) were REJECTED (rv_9839,
rv_971c): a crawl text, a search JSON, an earlier lesson's echo or a praise
sent before a short newer send is the same shape, and they replied with it.
The owner, 20 Nov 2026: "There would not be any regression."  So the pick is
b06b29405's again, and the live shape is pinned below as a STRICT xfail for
the owner's option (b): the end-of-turn reply never repeats a send the person
already has and carries only text they have not seen, which needs an
"already shown" state on the page.

Kept from the reworks: the finaliser replies with the newest send (MF1, a
stale "checking now" never beats "Sorry, the order lookup failed."), a
pre-steer text re-sent in the steered round does not replace the report the
round wrote, and every shape rv_9839 and rv_971c measured replies as on
b06b29405.

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


def _run(rr, monkeypatch, history, round_messages=(), state='gave_up',
         task=None):
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
    monkeypatch.setitem(rr.user_tasks, RESEARCH, task or _Task())
    chat = _Chat(history)
    seat = _Round(chat, round_messages)
    rr._reuse_synthesis_turn(RESEARCH, chat, _Manager(), seat)
    return chat, seat


def _reply(rr, chat):
    """What the synthesis set as the reply: the tail it appended."""
    tail = chat.messages[-1]
    return tail.get('content') if rr._reuse_message_is_user_answer(tail) else None


OPTION_B = (
    "the live research narration (18 Nov 2026, agent 1795014914479) stays "
    "open: the owner ruled no regression (20 Nov 2026), and no pick of one "
    "send tells a narration from a short answer.  Open for the owner's "
    "option (b): the end-of-turn reply never repeats a send the person "
    "already has and carries only text they have not seen, which needs an "
    "'already shown' state on the page")


class TestTheLiveNarrationShapeStaysOpen:
    """The steered round sent the answer, then a short line after it.  The
    newest send is the reply, as on b06b29405.  STRICT xfails: one that
    starts passing means the reply rule changed, and the owner decides."""

    @pytest.mark.xfail(strict=True, reason=OPTION_B)
    def test_the_live_research_round_replies_with_the_answer(self, rr, monkeypatch):
        """22:26:27 the answer, 22:26:42 the narration, both declared false."""
        chat, seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                          round_messages=[_send('c_ans', ANSWER),
                                          _receipt('c_ans'),
                                          _send('c_nar', NARRATION),
                                          _receipt('c_nar'), _verdict()])
        assert len(seat.messages) == 1, 'the round was steered, as live'
        assert _reply(rr, chat) == ANSWER, _reply(rr, chat)

    @pytest.mark.xfail(strict=True, reason=OPTION_B)
    def test_an_undeclared_narration_after_the_answer(self, rr, monkeypatch):
        chat, _seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                           round_messages=[_send('c_ans', ANSWER),
                                           _receipt('c_ans'),
                                           _send('c_nar', NARRATION, _UNDECLARED),
                                           _receipt('c_nar'), _verdict()])
        assert _reply(rr, chat) == ANSWER, _reply(rr, chat)

    @pytest.mark.xfail(strict=True, reason=OPTION_B)
    def test_an_undeclared_courtesy_line_after_the_answer(self, rr, monkeypatch):
        chat, _seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                           round_messages=[_send('c_ans', ANSWER),
                                           _receipt('c_ans'),
                                           _send('c_q', COURTESY, _UNDECLARED),
                                           _receipt('c_q'), _verdict()])
        assert _reply(rr, chat) == ANSWER, _reply(rr, chat)

    @pytest.mark.xfail(strict=True, reason=OPTION_B)
    def test_a_waiting_card_before_a_narration_send(self, rr, monkeypatch):
        """The card's question went out first, then a narration send: the
        newest send is the reply, as on b06b29405."""
        q = 'Which of these replies do you like best?'
        chat, _seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                           round_messages=[_card('c_card', q),
                                           _card_result('c_card'),
                                           _send('c_nar', NARRATION),
                                           _receipt('c_nar'), _verdict()])
        assert _reply(rr, chat) == q, _reply(rr, chat)


class TestTheSteeredRoundRepliesWithItsNewestSend:

    def test_a_declared_question_after_the_answer_is_the_reply(self, rr, monkeypatch):
        chat, _seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                           round_messages=[_send('c_ans', ANSWER),
                                           _receipt('c_ans'),
                                           _send('c_q', COURTESY, True),
                                           _receipt('c_q'), _verdict()])
        assert _reply(rr, chat) == COURTESY, _reply(rr, chat)

    def test_a_send_from_before_the_steer_is_not_the_rounds(self, rr, monkeypatch):
        """7188e162e's probe: a progress note went out before the action gave
        up, and the round wrote nothing.  The note is not the reply."""
        chat, seat = _run(rr, monkeypatch,
                          [_seed(TURN), _send('c_pre', PROGRESS),
                           _receipt('c_pre'), _verdict()],
                          round_messages=[_verdict()])
        assert len(seat.messages) == 1
        assert _reply(rr, chat) != PROGRESS

    def test_a_newer_declared_send_after_a_waiting_card(self, rr, monkeypatch):
        """rv_971c's p1: a card, then a newer send that declared a question.
        The newest, the send."""
        q = 'Which of these replies do you like best?'
        ask = "Do you want more detail on any of these?"
        chat, _seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                           round_messages=[_card('c_card', q),
                                           _card_result('c_card'),
                                           _send('c_q', ask, True),
                                           _receipt('c_q'), _verdict()])
        assert _reply(rr, chat) == ask, _reply(rr, chat)


# -- rv_9839's review of 98395453b: the shapes it measured worse -------------
DECLARED_Q = "Do you want more detail on any of these?"
HONEST = ("I could not finish: the order lookup failed, so I do not have your "
          "order status yet, and the remaining steps were not reached.")
CRAWL = ("Go ahead - Cambridge Dictionary. go ahead phrasal verb. to start to "
         "do something: We've received permission to go ahead with the new "
         "building. Go ahead and eat before it gets cold. Synonyms: proceed, "
         "advance, carry on. Examples of go ahead. These examples are from "
         "corpora and from sources on the web. Cookies Settings Accept All "
         "Cookies Privacy and Cookies Policy.") * 2
SHORT_ANSWER = ("Say 'Thank you, I'll proceed then.' - it is the safest reply "
                "in any setting.")
PRAISE = ("Great job, Arjun! You said Amma perfectly - your mother will be so "
          "proud of you. Let's keep going with the next family member now.")
MISDECLARED_Q = "Arjun, who is this, Amma or Appa?"
# Longer than ANSWER, and refused by the answer rules: it names a seat.
SEAT_LINE = "I asked the StatusVerifier to check each of these for you: " + ANSWER
JSON_TOOL_OUT = json.dumps({'results': [
    {'title': 'go ahead - Cambridge Dictionary',
     'url': 'https://dictionary.cambridge.org/dictionary/english/go-ahead',
     'snippet': 'to start to do something: We have received permission to go '
                'ahead with the new building.'},
    {'title': 'Go ahead definition and meaning',
     'url': 'https://www.collinsdictionary.com/dictionary/english/go-ahead',
     'snippet': 'If someone goes ahead with something, they begin to do it or '
                'make it, especially after planning, promising, or asking '
                'permission to do it.'}]})
EARLIER_LESSON = ("Yesterday's lesson: when someone says 'go ahead', thank them "
                  "and say what you will do next.  For example: 'Thanks, I'll "
                  "send the draft by five.'  Practise it with a colleague "
                  "today and tell me how it went.  Remember to keep it short "
                  "and friendly, and to name the next step you will take.")
NEW_SHORT = "Today: try 'Great, I'll start now.' with your manager."


class TestTheShapesTheAnswerPicksGotWorse:
    """rv_9839 (98395453b) and rv_971c (971c6049a): each shape replies as on
    b06b29405, the turn's newest send."""

    def test_an_older_declared_question_does_not_beat_a_newer_answer(
            self, rr, monkeypatch):
        """s10: RED on 98395453b, the older declared question was the
        reply."""
        chat, _seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                           round_messages=[_send('c_q', DECLARED_Q, True),
                                           _receipt('c_q'),
                                           _send('c_ans', ANSWER),
                                           _receipt('c_ans'), _verdict()])
        assert _reply(rr, chat) == ANSWER, _reply(rr, chat)

    def test_a_declared_name_call_does_not_beat_the_newer_prompts(
            self, rr, monkeypatch):
        """s14, the speech agent: "Arjun!" declared, then two newer prompts.
        RED on 98395453b: "Arjun!" was the reply."""
        newest = 'Arjun, can you see the mirror? Look at it.'
        chat, _seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                           round_messages=[_send('c1', 'Arjun!', True),
                                           _receipt('c1'),
                                           _send('c2', 'Arjun, look here!'),
                                           _receipt('c2'),
                                           _send('c3', newest),
                                           _receipt('c3'), _verdict()])
        assert _reply(rr, chat) == newest, _reply(rr, chat)

    @pytest.mark.parametrize('older,newer', [
        (CRAWL, SHORT_ANSWER),               # s06a
        (JSON_TOOL_OUT, SHORT_ANSWER),       # s06b
        (EARLIER_LESSON, NEW_SHORT),         # s07
        (PRAISE, MISDECLARED_Q),             # s13, the speech agent
    ], ids=['s06a_crawl', 's06b_search_json', 's07_earlier_lesson',
            's13_praise_then_question'])
    def test_a_short_newer_send_after_a_longer_one_is_the_reply(
            self, rr, monkeypatch, older, newer):
        """RED on 971c6049a (rv_971c): the longer older send was the reply."""
        chat, _seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                           round_messages=[_send('c1', older), _receipt('c1'),
                                           _send('c2', newer), _receipt('c2'),
                                           _verdict()])
        assert _reply(rr, chat) == newer, _reply(rr, chat)

    def test_a_pre_steer_text_sent_again_does_not_beat_the_rounds_report(
            self, rr, monkeypatch):
        """s09a (MF1): the progress note went out before the steer; the
        round sent it again under a new call id and wrote the honest report.
        The report is the reply.  RED on 98395453b: the progress note.
        (Live, the send tool answers "already sent" to a second identical
        send in one request, which is no delivery; this pins the pick on its
        own.)"""
        chat, _seat = _run(rr, monkeypatch,
                           [_seed(TURN), _send('c_pre', PROGRESS),
                            _receipt('c_pre'), _verdict()],
                           round_messages=[_send('c_re', PROGRESS),
                                           _receipt('c_re'),
                                           {'role': 'user', 'name': 'Assistant',
                                            'content': HONEST}])
        assert _reply(rr, chat) == HONEST, _reply(rr, chat)

    def test_a_newest_send_the_answer_rules_refuse_is_not_the_reply(
            self, rr, monkeypatch):
        """The shape of rv_9839's m04: an answer, then a longer send naming a
        seat.  The newest send is refused, so no send is the reply here, as
        on b06b29405 (971c6049a took the answer)."""
        chat, _seat = _run(rr, monkeypatch, [_seed(TURN), _verdict()],
                           round_messages=[_send('c_ans', ANSWER),
                                           _receipt('c_ans'),
                                           _send('c_seat', SEAT_LINE),
                                           _receipt('c_seat'), _verdict()])
        assert _reply(rr, chat) is None, _reply(rr, chat)
        assert chat.messages[-1].get('content') != SEAT_LINE


class TestAFinishedTurnRepliesWithItsNewestSend:
    """MF2: no steer, every step finished; the newest send is the reply, as
    before 98395453b.  Both RED on 98395453b."""

    def test_a_short_answer_after_a_crawl_text(self, rr, monkeypatch):
        """f02: the crawl text was the reply on 98395453b."""
        chat, seat = _run(rr, monkeypatch,
                          [_seed(TURN), _send('c1', CRAWL), _receipt('c1'),
                           _send('c2', SHORT_ANSWER), _receipt('c2'),
                           _verdict()],
                          task=_Task(current_action=4, total=3))
        assert seat.messages == []
        assert _reply(rr, chat) == SHORT_ANSWER, _reply(rr, chat)

    def test_the_childs_question_after_a_praise(self, rr, monkeypatch):
        """f05: the question the 4B declared false, not the praise."""
        chat, seat = _run(rr, monkeypatch,
                          [_seed(TURN), _send('c1', PRAISE), _receipt('c1'),
                           _send('c2', MISDECLARED_Q), _receipt('c2'),
                           _verdict()],
                          task=_Task(current_action=4, total=3))
        assert seat.messages == []
        assert _reply(rr, chat) == MISDECLARED_Q, _reply(rr, chat)


# -- What the person receives: the real finaliser -----------------------------
# rv_9839's fa/fb/fc/fg/ff through the real get_agent_response, with fakes
# only at the autogen boundary (the harness of
# test_a_reply_is_never_a_seats_own_note.TestTheReplyIsThisTurnsOwnWords).

_F_USER, _F_PROMPT, _F_REQ = 'reply-answer-user', 9839, 'reply-answer-req-1'
EARLIER_SEED = ("Teach me page 2\n\nPerform this action -> Action #1:Answer "
                "the person's question.")
EARLIER_REPLY = "Yesterday's lesson: page 2 is about clouds. Well done!"
LONG_PROGRESS = ('Sure, let me look up your order status for order 4471 placed '
                 'last week. I am checking with the store now, this can take a '
                 'moment.')
SORRY = 'Sorry, the order lookup failed.'
FAIL_NOTE = ('The order lookup returned an error, so I could not find the '
             'order. Let me try another way:')
NOTE = ('I see the child responded with "Aaa!" - this is a sound they made '
        'when called. Now I need to save this response to memory. '
        "Let me save the child's response:")
INTRO = ("Hi Arjun! Today we will practise saying the names of the people in "
         "your family. Listen carefully and say each name after me.")
SEAT_SEND = 'I asked the StatusVerifier to check the order for you.'
_PENDING = {'role': 'user', 'name': 'StatusVerifier',
            'content': '{"status": "pending", "action_id": 1, "message": "waiting"}'}


def _call(name, call_id):
    return [{'id': call_id, 'type': 'function',
             'function': {'name': name, 'arguments': '{}'}}]


def _words(text, **kw):
    return {'role': 'user', 'name': 'Assistant', 'content': text, **kw}


def _lookup_fail(call_id='g1', note=FAIL_NOTE):
    return [_words(note, tool_calls=_call('get_order_status', call_id)),
            {'role': 'tool', 'name': 'Helper', 'tool_call_id': call_id,
             'content': 'Error: order service unavailable'}]


def _note_on_save():
    return [_words(NOTE, tool_calls=_call('save_data_in_memory', 's2')),
            {'role': 'tool', 'name': 'Helper', 'tool_call_id': 's2',
             'content': 'Saved at name_response'}]


def _earlier_turn():
    return [{'role': 'user', 'name': 'ChatInstructor', 'content': EARLIER_SEED},
            _words(EARLIER_REPLY),
            {'role': 'user', 'name': 'StatusVerifier',
             'content': '{"status": "completed", "action_id": 1}'}]


class TestTheFinaliserRepliesWithTheNewestSend:

    @pytest.fixture(autouse=True)
    def _desktop(self, rr, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        pytest.importorskip('autogen', reason='autogen not installed')
        monkeypatch.setenv('NUNBA_BUNDLED', '1')
        monkeypatch.setitem(sys.modules, 'hart_intelligence',
                            SimpleNamespace(publish_async=MagicMock()))
        monkeypatch.setattr(rr, '_reuse_turn_start', rr.TTLCache(
            ttl_seconds=7200, max_size=500, name='test_reply_answer_turn_start'))

    def _turn(self, rr, monkeypatch, rounds, actions=1):
        """``rounds[i]``: what the group writes after the i-th post (0 = the
        seed's round, 1 = the synthesis steer's); every action GAVE_UP.
        Returns (the reply, every post after the seed)."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from flask import Flask
        user_prompt = f'{_F_USER}_{_F_PROMPT}'
        acts = [{'action_id': i + 1,
                 'action': "Answer the person's question in your own words.",
                 'can_perform_without_user_input': 'no', 'recipe': []}
                for i in range(actions)]
        monkeypatch.setattr(rr, 'recipes', {user_prompt: {'actions': acts}})
        monkeypatch.setattr(rr, 'user_tasks', {user_prompt: rr.Action(acts)})
        monkeypatch.setattr(rr, 'user_ledgers', {})
        monkeypatch.setattr(rr, 'request_id_list', {user_prompt: _F_REQ})
        monkeypatch.setattr(rr, 'request_id_list_sent_intermediate', {})
        monkeypatch.setattr(rr, 'get_action_state',
                            lambda up, aid: rr.ActionState.GAVE_UP)
        group_chat = SimpleNamespace(messages=_earlier_turn(), agents=[])
        posted = []

        def initiate_chat(recipient, message=None, **_kw):
            group_chat.messages.append(
                {'role': 'user', 'name': 'ChatInstructor', 'content': message})
            i = len(posted)
            posted.append(message)
            for m in (rounds[i] if i < len(rounds) else []):
                group_chat.messages.append(dict(m))

        ci = SimpleNamespace(initiate_chat=initiate_chat)
        with patch.object(rr, 'pooled_post'), \
                patch('core.http_pool.pooled_post_once',
                      return_value=SimpleNamespace(status_code=200, text='')), \
                patch.object(rr, 'scheduler') as sched, \
                Flask('reply-answer').app_context():
            sched.get_job.return_value = None
            reply = rr.get_agent_response(
                SimpleNamespace(name='Assistant'), ci,
                SimpleNamespace(name='Helper'), ci,
                SimpleNamespace(_oai_messages={}), group_chat, 'my question',
                'user', _F_USER, _F_PROMPT, _F_REQ)
        return reply, posted[1:]

    def test_a_turn_that_gave_up_replies_with_its_failure_note(self, rr, monkeypatch):
        """fa (MF1): a longer progress note, then "Sorry, the order lookup
        failed.", each followed by a note on a lookup call that failed; the
        steered round writes nothing.  The newest failure note, as on
        fff190b25's parent; never the stale "I am checking with the store
        now" (98395453b)."""
        rnd = [_send('p1', LONG_PROGRESS), _receipt('p1'), *_lookup_fail(),
               _send('p2', SORRY), _receipt('p2'), *_lookup_fail('g2'),
               _PENDING]
        reply, steers = self._turn(rr, monkeypatch, [rnd], actions=2)
        assert steers, 'the incomplete steer ran'
        assert reply == FAIL_NOTE, (reply, steers)

    def test_a_progress_send_then_a_failed_call_tells_the_failure(
            self, rr, monkeypatch):
        """rv_66reply's F1 on fff190b25 (p13): "One moment." went out, the
        note on the lookup call says it failed, the action gave up.  The
        failure note, as on fff190b25's parent.  RED on HEAD: "One
        moment." again."""
        rnd = [_send('p1', PROGRESS), _receipt('p1'), *_lookup_fail(),
               _PENDING]
        reply, steers = self._turn(rr, monkeypatch, [rnd], actions=2)
        assert steers, 'the incomplete steer ran'
        assert reply == FAIL_NOTE, (reply, steers)

    def test_the_childs_name_call_after_an_intro(self, rr, monkeypatch):
        """fb, the speech agent: an intro, then "Arun!", then a note on the
        save call.  RED on 98395453b: the intro."""
        rnd = [_send('i1', INTRO), _receipt('i1'), _send('n1', 'Arun!'),
               _receipt('n1'), *_note_on_save(), _PENDING]
        reply, steers = self._turn(rr, monkeypatch, [rnd])
        assert reply == 'Arun!', (reply, steers)

    def test_a_refused_send_still_keeps_the_save_calls_note_out(
            self, rr, monkeypatch):
        """fc (rv_9839 S1): the only send is text the answer rules refuse; it
        reached the person, so the note riding the save call is not the
        reply (fff190b25's fix).  RED on 98395453b: the note."""
        rnd = [_send('q1', SEAT_SEND), _receipt('q1'), *_note_on_save(),
               _PENDING]
        reply, steers = self._turn(rr, monkeypatch, [rnd])
        assert reply != NOTE, (reply, steers)
        assert reply == SEAT_SEND, (reply, steers)

    def test_a_re_sent_progress_note_does_not_replace_the_report(
            self, rr, monkeypatch):
        """fg (MF1): the progress note went out, the action gave up, and the
        steered round sent it again and wrote the honest report.  RED on
        98395453b: the progress note."""
        main = [_send('p1', PROGRESS), _receipt('p1'), *_lookup_fail(),
                _PENDING]
        steer = [_send('p9', PROGRESS), _receipt('p9'), _words(HONEST)]
        reply, steers = self._turn(rr, monkeypatch, [main, steer], actions=2)
        assert reply == HONEST, (reply, steers)

    @pytest.mark.xfail(strict=True, reason=OPTION_B)
    def test_the_live_research_round_end_to_end(self, rr, monkeypatch):
        """ff (rv_9839 S2): the action gave up, the steered round sent the
        answer then the narration.  The narration, end to end, as on
        b06b29405."""
        main = [*_lookup_fail('g1', 'Let me crawl the page:'), _PENDING]
        steer = [_send('a1', ANSWER), _receipt('a1'),
                 _send('a2', NARRATION), _receipt('a2'), _verdict()]
        reply, steers = self._turn(rr, monkeypatch, [main, steer], actions=3)
        assert steers, 'the incomplete steer ran'
        assert reply == ANSWER, (reply, steers)


def test_longest_answer_takes_the_longest_and_a_tie_goes_to_the_newer():
    from hartos.lifecycle_hooks import longest_answer
    assert longest_answer([]) is None
    assert longest_answer(['short', 'a longer one', 'mid']) == 'a longer one'
    first, second = {'content': 'same'}, {'content': 'same'}
    assert longest_answer([first, second],
                          text_of=lambda m: m['content']) is second
