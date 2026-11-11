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
instead ... from=Helper" handed over the same line.  Across builds 70-72 all
five replies taken from the Helper seat were that line.

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
