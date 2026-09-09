"""The loop's own steer must never be handed to the user as the answer.

MEASURED LIVE 2026-09-09, drive 08:52:14-08:53:22, agent 33323830039,
installed build. The user received, verbatim (HTTP 200, 68.8 s, len 185):

    "Perform this action -> Action #2:cd C:\\Users\\sathi\\Documents |  follow
     these steps: [{'cd C:\\\\Users\\\\sathi\\\\Documents': {'tool_name':
     'execute_windows_or_android_command', 'code': None}}]"

That is the pipeline's internal steering message, not an answer. Who wrote it,
measured rather than inferred, from the same window:

    Inside state_transition with message :10 Perform th & last_speaker ChatInstructor
    Message[14]: role=user, name=ChatInstructor        <- the tail

WHY IT GOT THROUGH. _reuse_needs_synthesis enumerates the shapes that are "not
a reply to the user" — empty, TERMINATE, role=='tool', <tool_call>/<function=
markup, an @agent mention, a dict carrying 'status' — and returns False for
anything else, deliberately, so a real answer is never made to burn a group
round. The steer matches NONE of those: it is prose, role='user', carries no
'@' mention, and retrieve_json of its trailing "[{...}]" fragment yields a
list, not a status dict. So it falls to `return False  # prose for the user —
leave it` and the extractor hands it over.

THE RULE ALREADY EXISTS. _REUSE_STEER_INITIATOR_NAMES (reuse_recipe.py:3228)
names the seats this loop steers THROUGH, and _reuse_group_terminate:3307
already uses it for the same semantic — a message in the steer's own voice is
not a terminal answer. A message authored by a steer initiator is internal
plumbing by construction; it is never the reply. Reusing that constant keeps
ONE notion of "the steer's own voice" instead of growing a second.

    python -m pytest tests/unit/test_reuse_steer_text_is_not_an_answer.py \
        --noconftest -q
"""
import pytest


RR = 'hartos.reuse_recipe'

# The live tail, verbatim.
LIVE_STEER_TAIL = (
    "Perform this action -> Action #2:cd C:\\Users\\sathi\\Documents |  follow "
    "these steps: [{'cd C:\\\\Users\\\\sathi\\\\Documents': {'tool_name': "
    "'execute_windows_or_android_command', 'code': None}}]"
)


class _Chat:
    def __init__(self, *messages):
        self.agents = []
        self.messages = list(messages)


@pytest.fixture
def rr():
    return pytest.importorskip(RR)


def _tail(rr, msg):
    return rr._reuse_needs_synthesis(_Chat(
        {'role': 'user', 'name': 'User', 'content': 'do the thing'}, msg))


class TestSteerTextIsNotAnAnswer:

    def test_live_steer_tail_needs_synthesis(self, rr):
        """THE DEFECT, with the exact message the user received. RED first."""
        assert _tail(rr, {'role': 'user', 'name': 'ChatInstructor',
                          'content': LIVE_STEER_TAIL}) is True, (
            "the ChatInstructor steer is classified as a real answer, so the "
            "synthesis turn is skipped and the extractor hands the user the "
            "pipeline's own internal plumbing (measured live 08:53:22)")

    def test_any_steer_authored_tail_needs_synthesis(self, rr):
        """Not keyed on this one sentence — keyed on WHO wrote it.

        Matching the words would break the moment a steer is reworded; the
        durable fact is that this seat only ever speaks to steer.
        """
        for name in rr._REUSE_STEER_INITIATOR_NAMES:
            assert _tail(rr, {'role': 'user', 'name': name,
                              'content': 'Work on subtask: list the folder'}) is True, (
                f'a tail authored by {name!r} is internal steering, never the '
                'reply to the user')

    def test_genuine_prose_from_another_seat_is_left_alone(self, rr):
        """ANTI-VACUITY — the whole point of the predicate's `return False`.

        A false positive burns a group round on every good turn. Prose from a
        non-steer seat is a real answer and must stay one.
        """
        assert _tail(rr, {'role': 'user', 'name': 'Assistant',
                          'content': 'The Nunba folder contains 8 files and 16 '
                                     'directories; tts_chatterbox_turbo.err is '
                                     'not among them.'}) is False, (
            'ordinary prose was flagged as needing synthesis — that costs a '
            'round on every healthy turn')

    def test_an_actual_answer_from_the_steer_seat_is_still_an_answer(self, rr):
        """The message2userfinal check must keep winning.

        If a steer-seat message ever does carry the answer key, it IS the
        answer; the author rule must not override the explicit one above it.
        """
        assert _tail(rr, {'role': 'user', 'name': 'ChatInstructor',
                          'content': '{"message2userfinal": "All done: 8 files."}'
                          }) is False

    def test_the_other_shapes_still_detected(self, rr):
        """Regression: widening must not cost the shapes already covered."""
        assert _tail(rr, {'role': 'user', 'name': 'StatusVerifier',
                          'content': "{'status': 'pending', 'action_id': 2}"}) is True
        assert _tail(rr, {'role': 'user', 'name': 'Assistant',
                          'content': 'TERMINATE'}) is True
        assert _tail(rr, {'role': 'tool', 'name': 'Assistant',
                          'content': 'Directory of C:\\Users'}) is True

    def test_one_definition_of_the_steer_seats(self, rr):
        """Shared with _reuse_group_terminate — no second list."""
        import inspect
        src = inspect.getsource(rr)
        assert src.count('_REUSE_STEER_INITIATOR_NAMES = ') == 1, (
            'the steer-seat names must have exactly one definition; a second '
            'copy drifts from the terminate predicate that also reads it')
        assert '_REUSE_STEER_INITIATOR_NAMES' in inspect.getsource(
            rr._reuse_needs_synthesis), (
            'the synthesis gate must read the SHARED constant, not re-spell '
            'the seat names')
