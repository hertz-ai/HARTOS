"""A 'requires_breakdown' verdict must END the group round, like 'completed'.

Measured live 2026-09-06, agent 89555447799 (24-action recipe), installed
build with the breakdown-execution loop already deployed and PROVEN loaded
(py-spy frame `get_agent_response (hartos\\reuse_recipe.py:3662)` matched the
patched copy's line numbering; the unpatched copies put that def at 3280):

    requires_breakdown verdicts .......... 17
    [BREAKDOWN] execution lines ..........  0
    action advances ......................  0
    current_action_id .................... 1, throughout

The breakdown-execution block reads ``group_chat.messages[-1]`` from inside
the w1 loop.  That loop only regains control when ``initiate_chat`` RETURNS,
and ``initiate_chat`` returns when the manager's ``is_termination_msg`` says
the round is over.  ``_reuse_group_terminate`` returned True only for a
literal TERMINATE or ``status == 'completed'`` — so on a requires_breakdown
verdict the group ran to ``max_round=10`` instead, the loop never regained
control, and the execution block was unreachable BY CONSTRUCTION.

This is the same defect the predicate's own docstring records for
'completed' (fixed 2026-09-05 by adding it to the terminal set).
'requires_breakdown' is the second action-terminal verdict and was simply
left out of that set.

Both verdicts are action-terminal in exactly the same sense: the action's
group conversation has produced its answer, and the OUTER loop is the thing
that must act on it (advance, or execute the decomposition).  So this is one
membership test against one canonical set, not a second mechanism.

    python -m pytest tests/unit/test_reuse_group_terminates_on_breakdown.py --noconftest -q
"""
import os

import pytest


_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_REUSE_SRC = os.path.join(_ROOT, 'hartos', 'reuse_recipe.py')


def _source():
    with open(_REUSE_SRC, encoding='utf-8') as fh:
        return fh.read()


def _verdict(status):
    """A StatusVerifier message as the manager actually receives it."""
    return {'name': 'StatusVerifier',
            'content': '{"status": "%s", "action_id": 1}' % status}


class TestRoundTerminalVocabularyIsCanonical:
    """The status vocabulary has one home (core.constants), per Gate 2."""

    def test_round_terminal_set_exists_in_core_constants(self):
        c = pytest.importorskip('core.constants')
        s = getattr(c, 'VERDICT_ROUND_TERMINAL_STATUSES', None)
        assert s is not None, (
            'VERDICT_ROUND_TERMINAL_STATUSES must live in core.constants '
            'beside the other verdict sets — not as a literal in reuse_recipe')
        assert c.VERDICT_COMPLETED in s
        assert c.VERDICT_REQUIRES_BREAKDOWN in s, (
            'requires_breakdown ends the round: the outer loop must regain '
            'control to execute the decomposition')

    def test_pending_is_not_round_terminal(self):
        """A genuine under-report must keep the group working, not end it."""
        c = pytest.importorskip('core.constants')
        assert c.VERDICT_PENDING not in c.VERDICT_ROUND_TERMINAL_STATUSES, (
            "'pending' means the action is still working — ending the round on "
            'it would cut off an action that legitimately has more to do')


class TestPredicateIsBehaviourallyTestable:
    """Lifted to module level so this is a real call, not an AST assertion."""

    def test_predicate_is_importable(self):
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert callable(getattr(rr, '_reuse_group_terminate', None)), (
            '_reuse_group_terminate must be module-level so its contract can '
            'be exercised directly; as a nested closure it could only ever be '
            'guarded by string matching, which is how the requires_breakdown '
            'gap survived')

    def test_terminates_on_requires_breakdown(self):
        """The measured defect. RED before the fix."""
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_group_terminate(_verdict('requires_breakdown')) is True, (
            'a requires_breakdown verdict must end the round so the w1 loop '
            'regains control and runs the breakdown-execution block; without '
            'this the group spins to max_round and 17 verdicts produced 0 '
            'executions on agent 89555447799')

    def test_still_terminates_on_completed(self):
        """No regression on the 2026-09-05 fix."""
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_group_terminate(_verdict('completed')) is True

    def test_does_not_terminate_on_pending(self):
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_group_terminate(_verdict('pending')) is False

    def test_does_not_terminate_on_ordinary_chatter(self):
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_group_terminate(
            {'name': 'Assistant', 'content': 'Let me search for that.'}) is False

    def test_literal_terminate_still_ends_the_round(self):
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_group_terminate(
            {'name': 'ChatInstructor', 'content': 'TERMINATE'}) is True


class TestUserFacingAnswerEndsTheRound:
    """The answer is round-terminal for the SAME reason a verdict is.

    Measured live 2026-09-09, agent 33323830039, installed build.  The
    synthesis steer (#799/D33) asked the group for the user-facing answer and
    the group PRODUCED it:

        04:38:12  last json as {'message2userfinal': "The chatterbox_turbo
                  worker startup failure has been diagnosed... The directory
                  check confirmed the existence of the 'Nunba' folder..."}

    — a real answer, grounded in the `dir` output its own tool had returned.
    Then the round kept going for four more turns (Assistant <-> StatusVerifier
    at 04:38:23 / :27 / :32 / :36 / :40 / :43) and ended on a verdict, so:

        04:38:48  [SYNTHESIS] round returned (23 -> 23 msgs);
                  still control JSON: True

    and the user received the StatusVerifier verdict instead of the answer.
    The answer was TALKED OVER.  The next drive was worse: the round budget
    ran out first (04:50:52 while1 exhausted), get_agent_response returned '',
    and Nunba's empty-reply check fell through to the tool-less raw-llama tier
    (chatbot_routes.py:3346, source='llama_local') which answered the goal from
    training data — a 5,078-char essay diagnosing the agent from its NAME.

    get_agent_response ALREADY treats message2userfinal as turn-ending: its
    in-loop branch unwraps it and returns it as the reply.  It only ever sees
    it when the answer happens to be messages[-1] at the moment initiate_chat
    returns.  Making the predicate terminal on it is not a new rule — it is the
    existing rule, applied at the point that decides when the loop looks.

    Population check before widening (all retained server.log*, 2026-09-09):
    382 'inside reuse while1' rounds against 18 'GOT message2userfinal in
    message' events, in two clusters.  The key is rare and deliberate, so
    ending the round on it does not truncate ordinary multi-action work.
    """

    # Verbatim shape of the 04:38:12 message, as the manager receives it.
    LIVE_ANSWER = {
        'name': 'Assistant',
        'content': ('{"message2userfinal": "The chatterbox_turbo worker '
                    'startup failure has been diagnosed. The directory check '
                    'confirmed the existence of the \'Nunba\' folder."}'),
    }

    def test_terminates_on_user_facing_answer(self):
        """The measured defect. RED before the fix."""
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_group_terminate(self.LIVE_ANSWER) is True, (
            'the answer the group produced at 04:38:12 did not end the round, '
            'so four more turns overwrote it and the user got the verdict')

    def test_terminates_when_answer_is_addressed_to_user(self):
        """The '@user {...}' form the extractor's regex fallback expects."""
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_group_terminate(
            {'name': 'Assistant',
             'content': '@user {"message2userfinal": "12 entries."}'}) is True

    def test_case_is_not_load_bearing(self):
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_group_terminate(
            {'name': 'Assistant',
             'content': '{"Message2UserFinal": "done"}'}) is True

    def test_non_final_message2_does_not_end_the_round(self):
        """Anti-vacuity: message2 is an interim note, not the final answer.

        Ending the round on it would cut off an action that is still working —
        the same mistake 'pending' is excluded for above.
        """
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_group_terminate(
            {'name': 'Assistant',
             'content': '{"message2": "still working on it"}'}) is False

    def test_the_steer_itself_does_not_end_the_round(self):
        """The steer NAMES the key; it is an instruction, not an answer.

        This is not hypothetical.  _REUSE_SYNTHESIS_STEER carries the example
        `{"message2userfinal": "<your answer here>"}`, and the live log shows
        the pipeline's own parser reading it as a message2userfinal object:

            04:37:58  last json as {'message2userfinal': '<your answer here>'}
                      for session: 6c2dc0fc-..._33323830039

        A predicate that ended the round on any parsed message2userfinal would
        therefore terminate on the steer itself and hand the user the literal
        string "<your answer here>".  Two independent guards below.
        """
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_group_terminate(
            {'name': 'ChatInstructor',
             'content': 'Reply with {"message2userfinal": "<your answer here>"}'
                        ' — put the real answer in it.'}) is False

    def test_placeholder_value_is_not_an_answer(self):
        """Guard 1, name-independent: a <...> value is a template, not text."""
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_group_terminate(
            {'name': 'Assistant',
             'content': '{"message2userfinal": "<your answer here>"}'}) is False

    def test_empty_answer_is_not_an_answer(self):
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_group_terminate(
            {'name': 'Assistant',
             'content': '{"message2userfinal": "   "}'}) is False

    def test_the_initiators_own_voice_is_never_an_answer(self):
        """Guard 2, structural: ChatInstructor is the harness's voice.

        Every steering message in this file enters the group as ChatInstructor
        (its three sites say so explicitly).  Whatever it carries is an
        instruction to the group, so it can never be the group's answer.
        """
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_group_terminate(
            {'name': 'ChatInstructor',
             'content': '{"message2userfinal": "a fully formed sentence."}'}
        ) is False


class TestFailsClosed:

    def test_malformed_message_does_not_terminate(self):
        """Uncertainty must not silently end an action's round."""
        rr = pytest.importorskip('hartos.reuse_recipe')
        for bad in (None, {}, {'content': None}, {'content': 'not json {{'},
                    {'content': '[]'}, {'content': '{"status": null}'}):
            assert rr._reuse_group_terminate(bad) is False, (
                f'{bad!r} must not be read as a terminal verdict')


class TestStillWiredToAllThreeManagers:
    """The lift must not orphan a manager (Gate 1 caller audit)."""

    def test_three_managers_still_pass_it(self):
        assert _source().count('is_termination_msg=_reuse_group_terminate,') == 3
