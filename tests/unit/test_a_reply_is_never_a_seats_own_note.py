"""A REUSE turn never hands the person a note one seat wrote to another.

THE DEFECT, live on build 72 (2026-11-11 07:46-07:54 IST, agent
1793366231447, a test child account).  From the child's "nooo... dunno"
(msg_1794363359268_sim) on, four of the child's replies were the Helper
seat's own narration, recovered from the group log as "the answer the action
already wrote":

    [SYNTHESIS] the action already wrote the answer - recovered 763 chars from
    the group log, no steer posted (... from=Helper, picker=action,
    head='The StatusVerifier indicates that Action 3 is still pending - ...')

and on the turns where the tail was refused, "delivering the last real one
instead ... from=Helper" handed over the same line.  On the child's agent,
across builds 70-72, all five replies taken from the Helper seat were that
line.  Across every agent on the desktop (11-10 14:26 to 11-11 13:26) the
Helper was delivered 11 times: 10 notes to the group and 1 real report to
the owner (11-11 11:24, the GitHub token); that report is delivered again
by the finaliser's last resort (TestTheReplyIsThisTurnsOwnWords).

The Helper is told (its system message in reuse_recipe) to reach the person
only with '@user' or the send tools, and to talk to @StatusVerifier
otherwise, so its plain prose is a note to the group.  The shared predicate
_reuse_message_is_user_answer now says so, and also refuses prose that names
a seat whose voice is never the answer (StatusVerifier, ChatInstructor), the
way only the group talks.  Every reader of that predicate follows: the
synthesis gate, the two pickers and the finaliser's walk back.

Driven through the real module (pytest.importorskip, as
test_reuse_recovers_the_written_answer.py does).  RED on HEAD: the Helper and
the seat-naming lines read as answers and the picker returns the Helper's.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

RR = 'hartos.reuse_recipe'

# Build 72, msg_1794363359268_sim: the Helper's line as the group log held it
# (the turn log truncates it after 125 words; the head is verbatim).
HELPER_NOTE = (
    "The StatusVerifier indicates that Action 3 is still pending - Arjun "
    "hasn't responded yet to the question \"Arjun, who is this, Amma or "
    "Appa?\". The system is waiting for the child's reply before we can "
    "proceed with the next step (which would be saving the family association "
    "result). I should continue waiting for Arjun's response. Since the child "
    "hasn't answered yet, I'll wait and monitor for their reply.")

# What the person should read: the Assistant's own words to the child.
ASSISTANT_ANSWER = ("Look, Arjun! Who is this in the picture, Amma or Appa? "
                    "Say it with me: Am-ma.")

DISPATCH_3 = {
    'role': 'user', 'name': 'ChatInstructor',
    'content': ("Perform this action -> Action #3:Ask Arjun who is in the "
                "picture, Amma or Appa, and wait for his answer.\n follow "
                "these steps: [{'Ask Arjun': {'tool_name': '', 'code': ''}}]"),
}
VERDICT_PENDING = (
    '{"status": "pending", "action": "Ask Arjun who is in the picture", '
    '"action_id": 3, "message": "Arjun has not answered yet."}')


@pytest.fixture
def rr():
    return pytest.importorskip(RR)


def _msg(name, content, role='user'):
    return {'role': role, 'name': name, 'content': content}


class _Chat:
    def __init__(self, messages):
        self.agents = []
        self.messages = list(messages)


class _Manager:
    def __init__(self):
        self._oai_messages = {}


class _Recorder:
    """Stands in for chat_instructor; keeps the steer instead of sending it."""

    def __init__(self):
        self.messages = []

    def initiate_chat(self, recipient=None, message=None, **kw):
        self.messages.append(message)


class TestWhatTheSeatsWriteIsNotTheReply:

    def test_the_helpers_note_is_not_an_answer(self, rr):
        """THE DEFECT: the build-72 line, from the seat that wrote it."""
        assert rr._reuse_message_is_user_answer(
            _msg('Helper', HELPER_NOTE)) is False

    def test_the_executors_plain_prose_is_not_an_answer(self, rr):
        """The other seat the Assistant hands work to: its prose is execution
        notes for the group."""
        assert rr._reuse_message_is_user_answer(_msg(
            'Executor', "The code ran and printed 3 rows; the file is saved."
        )) is False

    def test_prose_naming_the_verifier_is_not_an_answer_from_any_seat(self, rr):
        """The same line under another seat's name (the #725 sync relabels
        seats): it talks about the pipeline, so it is not to the person."""
        assert rr._reuse_message_is_user_answer(
            _msg('Assistant', HELPER_NOTE, role='assistant')) is False
        assert rr._reuse_message_is_user_answer(_msg(
            'Assistant', "The chatinstructor asked me to perform action 2 "
                         "next.", role='assistant')) is False

    def test_the_helper_tagging_the_person_still_answers(self, rr):
        """'@user' is how the Helper is told to reach the person: kept."""
        assert rr._reuse_message_is_user_answer(_msg(
            'Helper', "@user Well done, Arjun! That is Amma.")) is True

    def test_a_helper_message_with_the_answer_key_is_judged_by_the_key(self, rr):
        """Below the answer key: a key-carrying message is judged as before."""
        assert rr._reuse_message_is_user_answer(_msg(
            'Helper', '{"message2userfinal": "Well done, Arjun! That is '
                      'Amma."}')) is True

    def test_the_assistants_own_words_are_still_an_answer(self, rr):
        """Non-regression: the Assistant's prose to the person stays one."""
        assert rr._reuse_message_is_user_answer(
            _msg('Assistant', ASSISTANT_ANSWER, role='assistant')) is True

    def test_a_person_word_that_contains_no_seat_name_is_kept(self, rr):
        """'verifier' or 'instructor' alone are English; only the seats' own
        one-word names are refused."""
        assert rr._reuse_message_is_user_answer(_msg(
            'Assistant', "Your instructor will verify the answer tomorrow.",
            role='assistant')) is True


class TestThePickersReachTheAssistantsWords:

    def _history(self):
        """Build 72's action-3 window, in order: the dispatch, the
        Assistant's words to the child, the Helper's note, the pending
        verdict as the tail."""
        return [
            _msg('StatusVerifier',
                 '{"status": "completed", "action_id": 2, "message": "ok"}'),
            dict(DISPATCH_3),
            _msg('Assistant', ASSISTANT_ANSWER, role='assistant'),
            _msg('Helper', HELPER_NOTE),
            _msg('StatusVerifier', VERDICT_PENDING),
        ]

    def test_the_action_picker_passes_over_the_helpers_note(self, rr):
        """RED on HEAD: walking back newest first, the Helper's note was the
        first 'answer' and was returned."""
        got = rr._reuse_written_answer(_Chat(self._history()))
        assert got is not None and got.get('content') == ASSISTANT_ANSWER, (
            f"the picker returned {str((got or {}).get('content'))[:80]!r}")

    def test_the_turn_delivers_the_assistants_words(self, rr, monkeypatch):
        """End to end through the real synthesis step: no steer, and the tail
        the extractor reads is the Assistant's words, not the Helper's."""
        monkeypatch.setattr(rr, '_reuse_outstanding_tools',
                            lambda *a, **k: [], raising=True)
        chat = _Chat(self._history())
        rec = _Recorder()
        rr._reuse_synthesis_turn('sess_b72', chat, _Manager(), rec)
        assert rec.messages == []
        assert chat.messages[-1].get('content') == ASSISTANT_ANSWER

    def test_with_only_the_helpers_note_the_turn_asks_for_an_answer(
            self, rr, monkeypatch):
        """When the window holds nothing for the person, the note is not
        recovered: the synthesis steer is posted instead (the existing path
        for a window with no answer)."""
        monkeypatch.setattr(rr, '_reuse_outstanding_tools',
                            lambda *a, **k: [], raising=True)
        history = [m for m in self._history()
                   if m.get('content') != ASSISTANT_ANSWER]
        chat = _Chat(history)
        rec = _Recorder()
        rr._reuse_synthesis_turn('sess_b72', chat, _Manager(), rec)
        assert len(rec.messages) == 1, "no synthesis steer was posted"
        assert chat.messages[-1].get('content') != HELPER_NOTE


# -- What the person receives: the real finaliser -----------------------------
# Review of f9fc16bc6, F1: when the rules above refused a turn's only real
# answer, the finaliser's walk back crossed this request's seed and delivered
# an earlier turn's reply, or left raw verdict JSON or the synthesis steer as
# the reply.  Driven through the real get_agent_response with fakes only at
# the autogen boundary: the reviewer's probe harness
# (rv_f9fc16bc6_probes/probe_finaliser.py), adopted.

_F_USER, _F_PROMPT, _F_REQ = 'reply-seat-user', 4343, 'reply-seat-req-1'

EARLIER_REPLY = ("Yesterday's lesson: page 2 of How Rain Happens is about "
                 "clouds. Well done on the quiz!")
EARLIER_SEED = ("Teach me page 2\n\nPerform this action -> Action #1:Answer "
                "the person's question.")

# The live 2026-11-11 11:24 IST shape (daemon_8ccfa1d5, agent 52425147051):
# the Helper's report to the owner, refused by the Helper rule.
GITHUB_REPORT = (
    "I've identified the issue - the system is missing a valid GitHub token "
    "with the required permissions to access the hertz-ai/Hevolve repository. "
    "You need to generate a personal access token with repo scope and provide "
    "it so I can proceed. Would you like me to wait for the token?")
# A person asked what the verifier is: refused by the seat-name rule.
SEAT_ANSWER = ("The StatusVerifier is the agent that checks each step's "
               "result before the next step runs; it never writes to you "
               "directly.")
# A lesson written into the save call's message: refused by the call rule.
LESSON = ("Great try, Arjun! 'Ball' starts with B. Now say it with me: "
          "Buh-buh-ball. Next, can you find something red in the room?")


def _call(name, call_id):
    return [{'id': call_id, 'type': 'function',
             'function': {'name': name, 'arguments': '{}'}}]


def _assistant(text, **kw):
    return {'role': 'user', 'name': 'Assistant', 'content': text, **kw}


_PENDING = {'role': 'user', 'name': 'StatusVerifier',
            'content': '{"status": "pending", "action_id": 1, "message": "waiting"}'}
_NOTE_CALL = _assistant("I need to find a valid GitHub token. Let me check "
                        "saved tokens.", tool_calls=_call('get_data_by_key', 'c1'))
_NO_TOKEN = {'role': 'tool', 'name': 'Helper', 'content': 'no token stored',
             'tool_call_id': 'c1'}
_LESSON_CALL = _assistant(LESSON, tool_calls=_call('save_data_in_memory', 's1'))
_SAVED = {'role': 'tool', 'name': 'Helper', 'content': 'Saved at teach.1',
          'tool_call_id': 's1'}


def _earlier_turn():
    return [{'role': 'user', 'name': 'ChatInstructor', 'content': EARLIER_SEED},
            _assistant(EARLIER_REPLY),
            {'role': 'user', 'name': 'StatusVerifier',
             'content': '{"status": "completed", "action_id": 1}'}]


class TestTheReplyIsThisTurnsOwnWords:

    @pytest.fixture(autouse=True)
    def _desktop(self, rr, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        pytest.importorskip('autogen', reason='autogen not installed')
        monkeypatch.setenv('NUNBA_BUNDLED', '1')
        monkeypatch.setitem(sys.modules, 'hart_intelligence',
                            SimpleNamespace(publish_async=MagicMock()))
        # Each test records its own seed; none outlives the test.
        monkeypatch.setattr(rr, '_reuse_turn_start', rr.TTLCache(
            ttl_seconds=7200, max_size=500, name='test_reuse_turn_start'))

    def _turn(self, rr, monkeypatch, rounds, history=(),
              action_text="Answer the person's question in your own words."):
        """``rounds[i]``: what the group writes after the i-th post (0 = the
        seed's round, 1 = the synthesis steer's).  A round past the list
        writes nothing, the measured 2026-09-10 14:30:55 case.  Returns
        (the reply, every post after the seed)."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from flask import Flask
        user_prompt = f'{_F_USER}_{_F_PROMPT}'
        action = {'action_id': 1, 'action': action_text,
                  'can_perform_without_user_input': 'no', 'recipe': []}
        monkeypatch.setattr(rr, 'recipes', {user_prompt: {'actions': [action]}})
        monkeypatch.setattr(rr, 'user_tasks', {user_prompt: rr.Action([action])})
        monkeypatch.setattr(rr, 'user_ledgers', {})
        monkeypatch.setattr(rr, 'request_id_list', {user_prompt: _F_REQ})
        monkeypatch.setattr(rr, 'request_id_list_sent_intermediate', {})
        group_chat = SimpleNamespace(messages=[dict(m) for m in history], agents=[])
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
                Flask('reply-seat').app_context():
            sched.get_job.return_value = None
            reply = rr.get_agent_response(
                SimpleNamespace(name='Assistant'), ci,
                SimpleNamespace(name='Helper'), ci,
                SimpleNamespace(_oai_messages={}), group_chat, 'my question',
                'user', _F_USER, _F_PROMPT, _F_REQ)
        return reply, posted[1:]

    def test_the_helpers_report_is_delivered_not_an_earlier_reply(self, rr, monkeypatch):
        """p2, the 11:24 shape with an earlier turn in the log: the report,
        not "Yesterday's lesson ..." (what f9fc16bc6 delivered)."""
        rnd = [_NOTE_CALL, _NO_TOKEN, {'role': 'user', 'name': 'Helper',
                                       'content': GITHUB_REPORT}, _PENDING]
        again = [{'role': 'user', 'name': 'Helper', 'content': GITHUB_REPORT}, _PENDING]
        reply, steers = self._turn(rr, monkeypatch, [rnd, again],
                                   history=_earlier_turn())
        assert reply == GITHUB_REPORT, (reply, steers)

    def test_the_helpers_report_is_delivered_not_the_verdict(self, rr, monkeypatch):
        """p2b, the same with no earlier turn: the report, not the raw
        '{"status": "pending" ...}' left at the tail."""
        rnd = [_NOTE_CALL, _NO_TOKEN, {'role': 'user', 'name': 'Helper',
                                       'content': GITHUB_REPORT}, _PENDING]
        again = [{'role': 'user', 'name': 'Helper', 'content': GITHUB_REPORT}, _PENDING]
        reply, steers = self._turn(rr, monkeypatch, [rnd, again])
        assert reply == GITHUB_REPORT, (reply, steers)

    def test_an_answer_naming_the_verifier_is_delivered_not_an_earlier_reply(
            self, rr, monkeypatch):
        """p1b: the Assistant's answer names the StatusVerifier (the person
        asked about it) and the synthesis round writes nothing; an earlier
        turn is in the log.  This turn's answer, not the earlier one."""
        reply, steers = self._turn(rr, monkeypatch, [[_assistant(SEAT_ANSWER)]],
                                   history=_earlier_turn())
        assert reply == SEAT_ANSWER, (reply, steers)
        assert EARLIER_REPLY not in str(reply)

    def test_a_lesson_written_into_the_save_call_is_delivered_not_the_steer(
            self, rr, monkeypatch):
        """p3c: the lesson rides the save call and the synthesis round
        writes nothing, no earlier turn.  The lesson, not the synthesis
        steer verbatim."""
        reply, steers = self._turn(
            rr, monkeypatch, [[_LESSON_CALL, _SAVED]],
            action_text="Write this turn's reply to the learner yourself.")
        assert reply == LESSON, (reply, steers)
        assert steers and reply not in steers

    def test_a_turn_that_wrote_nothing_for_the_person_says_so(self, rr, monkeypatch):
        """Task #291 (build 69, msg_1794342560815_sim): every Assistant
        message carried a tool call with no words, the tail was the verdict
        {"status": "requires_breakdown", ...}, and the synthesis round wrote
        nothing.  The person gets the one honest failed-turn sentence (what
        is_user_facing_error recognises downstream), never the verdict, the
        steer or an earlier turn's reply."""
        from core.agent_tools import is_user_facing_error
        breakdown = {'role': 'user', 'name': 'StatusVerifier', 'content': (
            '{"status": "requires_breakdown", "action_id": 1, "reason": '
            '"too big", "subtasks": [{"subtask_id": "1.1", "description": '
            '"ask the parent"}]}')}
        rnd = [_assistant('', tool_calls=_call('get_data_by_key', 'c1')),
               {'role': 'tool', 'name': 'Helper', 'content': 'no data',
                'tool_call_id': 'c1'},
               breakdown]
        reply, steers = self._turn(rr, monkeypatch, [rnd], history=_earlier_turn())
        assert is_user_facing_error(reply), (reply, steers)
        assert '"status"' not in str(reply) and EARLIER_REPLY not in str(reply)
        assert reply not in steers

    def test_a_question_sent_through_the_send_tool_is_the_reply(self, rr, monkeypatch):
        """hartos-16's criticism: an ask sent mid-turn through
        send_message_to_user reached the person, then a pending verdict and
        an empty synthesis round.  The turn did reach the person, so the
        reply is the question it sent, not a failed-turn sentence, not the
        verdict, not the earlier turn's reply."""
        from core.agent_tools import is_user_facing_error
        question = "Arjun, who is this in the picture, Amma or Appa?"
        send_call = _assistant('', tool_calls=[{
            'id': 'q1', 'type': 'function',
            'function': {'name': 'send_message_to_user',
                         'arguments': '{"text": "%s"}' % question}}])
        sent = {'role': 'tool', 'name': 'Helper', 'tool_call_id': 'q1',
                'content': 'Message sent successfully to user with request_id: x'}
        reply, steers = self._turn(rr, monkeypatch, [[send_call, sent, _PENDING]],
                                   history=_earlier_turn())
        assert reply == question, (reply, steers)
        assert not is_user_facing_error(reply)

    def test_a_note_on_a_call_after_a_send_that_reached_the_person_is_not_the_reply(
            self, rr, monkeypatch):
        """Live 2026-11-18 17:53 IST, speech agent 1794540972213 on the local
        4B (a test account, a made-up child's name): the turn sent "Arun!",
        which reached the child, then wrote its note to the save call; the
        finaliser handed the child the note ("delivering this turn's own
        words the answer rules refused").  The reply is the send that reached
        them; a lesson on a save call in a turn that sent nothing is still
        delivered (p3c above)."""
        note = ('I see the child responded with "Aaa!" - this is a sound they '
                'made when called. Now I need to save this response to memory. '
                "Let me save the child's response:")
        name_call = _assistant('', tool_calls=[{
            'id': 'n1', 'type': 'function',
            'function': {'name': 'send_message_to_user',
                         'arguments': '{"text": "Arun!", "expects_reply": false}'}}])
        sent = {'role': 'tool', 'name': 'Helper', 'tool_call_id': 'n1',
                'content': 'Message sent successfully to user with request_id: x'}
        rnd = [name_call, sent,
               _assistant(note, tool_calls=_call('save_data_in_memory', 's2')),
               {'role': 'tool', 'name': 'Helper', 'content': 'Saved at name_response',
                'tool_call_id': 's2'},
               _PENDING]
        # The live turn's action gave up ("action 2 gave up without a
        # verified result"), so the incomplete steer ran and its round wrote
        # nothing the finaliser could deliver.
        monkeypatch.setattr(rr, 'get_action_state',
                            lambda up, aid: rr.ActionState.GAVE_UP)
        reply, steers = self._turn(rr, monkeypatch, [rnd], history=_earlier_turn())
        assert steers, 'the incomplete steer ran, as it did live'
        assert reply == 'Arun!', (reply, steers)
        assert note not in str(reply)

    def test_a_report_with_no_call_after_a_send_is_still_the_reply(self, rr, monkeypatch):
        """The other side: the Helper's report to the owner carries no tool
        call, so it is prose for the person, and it is still delivered when
        an earlier send of the turn reached them (the 11-11 11:24 shape)."""
        name_call = _assistant('', tool_calls=[{
            'id': 'n2', 'type': 'function',
            'function': {'name': 'send_message_to_user',
                         'arguments': '{"text": "Checking that now.", "expects_reply": false}'}}])
        sent = {'role': 'tool', 'name': 'Helper', 'tool_call_id': 'n2',
                'content': 'Message sent successfully to user with request_id: x'}
        rnd = [name_call, sent, {'role': 'user', 'name': 'Helper', 'content': GITHUB_REPORT},
               _PENDING]
        monkeypatch.setattr(rr, 'get_action_state',
                            lambda up, aid: rr.ActionState.GAVE_UP)
        reply, steers = self._turn(rr, monkeypatch, [rnd], history=_earlier_turn())
        assert reply == GITHUB_REPORT, (reply, steers)

    def test_a_real_answer_of_this_turn_still_wins(self, rr, monkeypatch):
        """p4, build 72 through the finaliser: the Assistant's words, not the
        Helper's note after them, and no steer."""
        rnd = [_assistant(ASSISTANT_ANSWER), _msg('Helper', HELPER_NOTE), _PENDING]
        reply, steers = self._turn(rr, monkeypatch, [rnd], history=_earlier_turn())
        assert reply == ASSISTANT_ANSWER, (reply, steers)
        assert steers == []
