"""The user must get an ANSWER, never the StatusVerifier's control JSON.

MEASURED live 2026-09-09 04:14:22 on the installed build (agent 33323830039).
The entire /chat reply was:

    {"status": "completed", "action": "Action #1: cd C:\\Users\\sathi\\Documents
     && dir Nunba", "action_id": 1, "message": "Action completed successfully.
     The directory listing for 'Nunba' ... was retrieved."}

Mechanism (#799/D33): `_reuse_group_terminate` ends the round ON the verdict,
so `group_chat.messages[-1]` is STRUCTURALLY that verdict; the post-loop
extractor only unwraps `message2userfinal` / `message2` and otherwise returns
`last_message['content']` verbatim.  The `response_format` asks for
`message2userfinal` and the prompt instructs it, but no agent is ever given a
turn to write it after the verdict has closed the round.

Walking back to an earlier message cannot fix it — measured on the same turn
the closing history is tool traffic only ([725-SYNC-COMPOSITION]
User*:n=10,calls=7,answers=0 | Assistant:n=3,calls=1), with no prose synthesis
to recover.  The turn has to be asked for.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

# The exact reply the live drive returned to the user.
LIVE_CONTROL_JSON = (
    '{"status": "completed", "action": "Action #1: cd C:\\\\Users\\\\sathi'
    '\\\\Documents && dir Nunba", "action_id": 1, "message": "Action '
    'completed successfully. The directory listing was retrieved."}'
)


class _FakeGroupChat:
    def __init__(self, messages):
        self.messages = list(messages)


class _RecordingInstructor:
    """Stands in for the ChatInstructor UserProxyAgent."""

    def __init__(self, reply=None):
        self.calls = []
        self._reply = reply

    def initiate_chat(self, recipient=None, message=None, **kw):
        self.calls.append({'recipient': recipient, 'message': message,
                           'kwargs': kw})
        if self._reply is not None:
            recipient.messages.append(self._reply)


@pytest.fixture
def rr():
    return pytest.importorskip('hartos.reuse_recipe')


def test_control_json_reply_is_detected(rr):
    """The live failing reply must be recognised as needing synthesis."""
    gc = _FakeGroupChat([{'name': 'StatusVerifier',
                          'content': LIVE_CONTROL_JSON}])
    assert rr._reuse_needs_synthesis(gc) is True, (
        "the exact control JSON the user received was not flagged; the "
        "extractor would hand it straight to the user")


def test_a_real_answer_is_left_alone(rr):
    """Anti-vacuity: never burn a round when the answer is already there."""
    gc = _FakeGroupChat([{'name': 'Assistant', 'content':
                          '@user {"message2userfinal": "Nunba has 12 files."}'}])
    assert rr._reuse_needs_synthesis(gc) is False
    instructor = _RecordingInstructor()
    posted = rr._reuse_synthesis_turn('u_1', gc, object(), instructor)
    assert posted is False
    assert instructor.calls == [], (
        "posted a synthesis round even though the reply was already "
        "user-facing — that is a wasted group round on every good turn")


def test_prose_reply_is_left_alone(rr):
    """Plain prose is a valid answer and must not trigger a round."""
    gc = _FakeGroupChat([{'name': 'Assistant',
                          'content': 'The Nunba folder has 12 entries.'}])
    assert rr._reuse_needs_synthesis(gc) is False


def test_empty_history_does_not_trigger(rr):
    assert rr._reuse_needs_synthesis(_FakeGroupChat([])) is False


def test_synthesis_asks_the_group_for_the_answer(rr):
    """The fix: one steer, through the canonical initiator, naming the key
    the existing extractor already unwraps."""
    gc = _FakeGroupChat([{'name': 'StatusVerifier',
                          'content': LIVE_CONTROL_JSON}])
    manager = object()
    instructor = _RecordingInstructor()

    posted = rr._reuse_synthesis_turn('u_1', gc, manager, instructor)

    assert posted is True, "no synthesis round was posted for a control-JSON reply"
    assert len(instructor.calls) == 1, "must ask exactly once, never loop"
    call = instructor.calls[0]
    assert call['recipient'] is manager, (
        "steer must go to the group manager, like every other steer in this "
        "file — not to a new recipient")
    assert 'message2userfinal' in call['message'], (
        "the steer must name the response_format key the extractor unwraps, "
        "or the answer still will not be extractable")
    assert call['kwargs'].get('clear_history') is False, (
        "clear_history=False — the synthesis needs the tool results that are "
        "already in the conversation")


def test_after_the_group_answers_the_reply_is_no_longer_control_json(rr):
    """End state: once the group replies, the turn has a real answer."""
    gc = _FakeGroupChat([{'name': 'StatusVerifier',
                          'content': LIVE_CONTROL_JSON}])
    answer = {'name': 'Assistant',
              'content': '@user {"message2userfinal": "Nunba has 12 entries."}'}
    instructor = _RecordingInstructor(reply=answer)

    rr._reuse_synthesis_turn('u_1', gc, gc, instructor)

    assert rr._reuse_needs_synthesis(gc) is False, (
        "still control JSON after the synthesis round — the user would be "
        "answered with the verdict")
    assert 'message2userfinal' in gc.messages[-1]['content']


def test_logging_failure_cannot_skip_the_steer(rr, monkeypatch):
    """Regression guard: the first cut logged via current_app, which RAISES
    outside an app context, so the whole steer was swallowed by the except
    and the control-JSON reply came back silently."""
    gc = _FakeGroupChat([{'name': 'StatusVerifier',
                          'content': LIVE_CONTROL_JSON}])
    instructor = _RecordingInstructor()

    def _boom(*a, **k):
        raise RuntimeError('Working outside of application context')

    monkeypatch.setattr(rr, '_ctx_safe_log', _boom, raising=False)
    # Even with logging hard-failing, the steer must still be attempted.
    rr._reuse_synthesis_turn('u_1', gc, object(), instructor)
    assert instructor.calls, (
        "a logging failure prevented the synthesis steer — the user would "
        "silently get control JSON again")
