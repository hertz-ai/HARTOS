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
        for name in rr._REUSE_NON_ANSWER_SEATS:
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
        assert src.count('_REUSE_NON_ANSWER_SEATS = ') == 1, (
            'the non-answer seats must have exactly one definition')
        # RE-POINTED 2026-09-10.  The shape tests moved out of
        # _reuse_needs_synthesis into _reuse_message_is_user_answer so the
        # answer-recovery could ask the same question; the invariant is
        # unchanged, it just lives one function deeper.  Both readers are
        # named, so the guard fails again if either re-spells the seats.
        #
        # RE-POINTED 2026-09-13.  The seats are two names now: the answer
        # predicate reads the non-answer set (steering seat + verifier), the
        # walk-back bound reads the steering seat alone -- 501cf51fb had put
        # the verifier in the bound and 4 answer-recovery tests went red.
        assert '_REUSE_NON_ANSWER_SEATS' in inspect.getsource(
            rr._reuse_message_is_user_answer), (
            'the synthesis gate must read the SHARED constant, not re-spell '
            'the seat names')
        assert '_REUSE_STEER_INITIATOR_NAMES' in inspect.getsource(
            rr._reuse_written_answer), (
            'the answer-recovery bound must read the SHARED constant, not '
            're-spell the seat names')


class TestTheSeedIsAlsoTheProducersOwnText:
    """The dispatch is not always at the START of the message.

    MEASURED LIVE 2026-09-10 10:05:25, agent 88094979291, installed build.
    The user's whole 904-char reply, HTTP 200 in 0 seconds:

        Summarize a given text into exactly three bullet points.

        Perform this action -> Action #1:Receive the input text from the user.
         follow these steps: [{"Extract the user's latest message ... ":
         {'tool_name': '', 'code': "def extract_user_input(): ..."}}]

    `_reuse_seed_message` builds the opening turn as
    ``f"{message}\n\n{_build_reuse_action_message(...)}"`` — the user's own
    words FIRST, the dispatch second.  So the producer's text is in the
    MIDDLE of the message, and the 93fdaac3f refusal, which asks
    ``content.lstrip().startswith(_REUSE_ACTION_MESSAGE_PREFIX)``, does not
    see it: the seed reads as ordinary prose, the synthesis turn is skipped,
    and the extractor returns the dispatch verbatim.

    Same producer, same constant, one composition away from the case that
    was closed.  The question both sites ask is "did THIS MODULE write this
    text", and there is exactly one honest answer to it wherever the
    producer chose to put it.
    """

    LIVE_SEED = (
        "Summarize a given text into exactly three bullet points.\n\n"
        "Perform this action -> Action #1:Receive the input text from the "
        "user.\n follow these steps: [{\"Extract the user's latest message "
        "containing the text to be summarized.\": {'tool_name': '', 'code': "
        "\"def extract_user_input():\n    pass\"}}]"
    )

    def test_the_seed_needs_synthesis(self, rr):
        """THE DEFECT, with the exact 904-char message the user received."""
        assert _tail(rr, {'role': 'user', 'name': 'User',
                          'content': self.LIVE_SEED}) is True, (
            "the opening seed — the user's words plus this module's own "
            "action dispatch — was classified as a real answer, so the "
            "synthesis turn was skipped and the extractor handed the user "
            "the pipeline's internal plumbing (live 2026-09-10 10:05:25)")

    def test_the_walk_back_stops_at_the_seed(self, rr):
        """The answer-recovery bound has the same hole.

        Its content bound also asks `startswith`, so a seed-opened action
        lets the walk run past its own dispatch into an earlier action and
        credit that action's output to this one.
        """
        earlier = {'role': 'assistant', 'name': 'Assistant',
                   'content': 'This was an earlier action answering something '
                              'else entirely.'}
        chat = _Chat(earlier,
                     {'role': 'user', 'name': 'User',
                      'content': self.LIVE_SEED})
        assert rr._reuse_written_answer(chat) is None, (
            "the walk reached past this action's own seeded dispatch")

    def test_ordinary_prose_is_still_an_answer(self, rr):
        """ANTI-VACUITY.  Widening the match must not swallow real answers."""
        assert _tail(rr, {'role': 'user', 'name': 'Assistant',
                          'content': 'Here is the summary you asked for: the '
                                     'Apollo program ran from 1961 to 1972 '
                                     'and landed twelve people on the Moon.'
                          }) is False

    def test_one_place_decides_what_a_dispatch_is(self, rr):
        """DRY: the two readers must not each carry their own matching rule.

        They already share the CONSTANT; before this they did not share the
        MATCH, which is how one of them was fixed and the other kept the
        hole.  One predicate, both callers.
        """
        import inspect
        src = inspect.getsource(rr)
        assert src.count('_REUSE_ACTION_MESSAGE_PREFIX = ') == 1
        assert src.count('def _reuse_is_pipeline_text') == 1, (
            'the "is this our own dispatch" test must have exactly one '
            'implementation')
        for reader in (rr._reuse_message_is_user_answer,
                       rr._reuse_written_answer):
            assert '_reuse_is_pipeline_text' in inspect.getsource(reader), (
                f'{reader.__name__} must ask the shared predicate, not '
                're-implement the match')


class TestTheRefusalSteerIsAlsoOurOwnText:
    """The fabrication gate's re-steer is a THIRD thing this module writes.

    MEASURED LIVE 2026-09-10 10:22:30, agent 88094979291, installed build.
    The user's entire 349-char reply was the re-steer itself:

        Action 1 is NOT complete: it produced no output. This action calls no
        tool — its result IS the text you write — and nothing was written for
        the user in this action. Do not report this action as completed.
        Write the action's actual result now, in full, as your reply. ...

    (`[FABRICATED-COMPLETE] refusing to advance action 1` fired twice in that
    turn; the log line for the drive reads ``held=[1, 1]``.)

    The dispatch and the seed are recognised as this module's own words; the
    re-steer was not, so when a turn ends with a re-steer as the tail — which
    is exactly what a refused action leaves behind — the extractor hands it
    over as the answer.  Both branches of `_reuse_fab_steer_message` open with
    the same sentence, so ONE marker covers the tool branch and the
    no-output branch, and the constant is emitted by the producer rather than
    matched by a copy of its wording.
    """

    LIVE_STEER = (
        "Action 1 is NOT complete: it produced no output. This action calls "
        "no tool — its result IS the text you write — and nothing was written "
        "for the user in this action. Do not report this action as completed. "
        "Write the action's actual result now, in full, as your reply. If you "
        "cannot produce it, say plainly what is missing instead of claiming "
        "success."
    )

    def test_the_live_refusal_steer_needs_synthesis(self, rr):
        """THE DEFECT, with the exact text the user received."""
        assert _tail(rr, {'role': 'user', 'name': 'Assistant',
                          'content': self.LIVE_STEER}) is True, (
            "the fabrication gate's own re-steer was classified as a real "
            'answer and handed to the user (live 2026-09-10 10:22:30)')

    def test_both_steer_branches_are_covered(self, rr, monkeypatch):
        """Not the one sentence — the real output of BOTH branches.

        Built through the producer, so a reworded steer fails this test
        instead of silently re-opening the hole.
        """
        for pending in (['google_search'], [rr._REUSE_NO_OUTPUT_SENTINEL]):
            monkeypatch.setitem(rr._reuse_fab_pending, ('sess_s', 2), pending)
            built = rr._reuse_fab_steer_message('sess_s', 2)
            assert built, 'the producer returned nothing to check'
            assert _tail(rr, {'role': 'user', 'name': 'Assistant',
                              'content': built}) is True, (
                f'a re-steer built for pending={pending!r} reads as an answer')

    def test_the_walk_back_does_not_credit_a_steer_as_output(self, rr):
        """The evidence gate must not accept its own nudge as the output.

        If a re-steer counted as "the action wrote something", the gate would
        clear itself on the next pass — a guard that satisfies its own
        condition verifies nothing.
        """
        chat = _Chat({'role': 'user', 'name': 'Assistant',
                      'content': self.LIVE_STEER})
        assert rr._reuse_written_answer(chat) is None, (
            "the gate's own re-steer was counted as the action's output")

    def test_a_real_answer_that_reports_failure_is_still_an_answer(self, rr):
        """ANTI-VACUITY.  An honest "I could not do it" IS a user answer.

        Measured the same day at 10:21:11, and it must reach the user: "Since
        no specific text was provided in your input, I could not summarize
        anything. Please provide the document ..."
        """
        assert _tail(rr, {'role': 'user', 'name': 'Assistant',
                          'content': 'Since no specific text was provided in '
                                     'your input, I could not summarize '
                                     'anything. Please provide the document '
                                     'or paragraph you would like me to '
                                     'summarize into exactly three bullet '
                                     'points.'}) is False

    def test_one_marker_definition_emitted_by_the_producer(self, rr):
        """DRY: the recogniser reads what the producer emits."""
        import inspect
        src = inspect.getsource(rr)
        assert src.count('_REUSE_NOT_COMPLETE_MARKER = ') == 1
        assert '_REUSE_NOT_COMPLETE_MARKER' in inspect.getsource(
            rr._reuse_fab_steer_message), (
            'the steer producer must emit the shared marker, not its own '
            'copy of the wording')
        assert '_REUSE_NOT_COMPLETE_MARKER' in inspect.getsource(
            rr._reuse_is_pipeline_text), (
            'the recogniser must read the same marker the producer emits')
