"""The synthesis steer may not hand the model a placeholder to copy.

MEASURED LIVE 2026-09-09 18:25:22, agent 33323830039.  The ENTIRE user-facing
reply of a 313-second turn, 18 characters long:

    http=200 313s len=18
    REPLY: <your answer here>

That string exists in exactly one place in this pipeline -- the two synthesis
steers, both of which end:

    Reply to @user with exactly: {"message2userfinal": "<your answer here>"}

"with exactly" is an instruction to reproduce the literal, and the model
obeyed it.  Same family as the false-premise wording already recorded on
_REUSE_SYNTHESIS_STEER's own comment: "The model was obeying an instruction,
not hallucinating."

WHICH steer fired is settled by the log, not inferred:

    [SYNTHESIS] reply would be raw control JSON - asking for the user-facing
    answer (session: 6c2dc0fc-..._33323830039, 53 msgs, unrun=none)

`unrun=none` selects _REUSE_SYNTHESIS_STEER (the COMPLETE one).  A fix applied
only to the INCOMPLETE steer would have missed the path that actually failed,
so BOTH are asserted here -- and the shape sentence gets ONE home so the next
fix cannot land on only one of them again.

The gate downstream then accepted the template as the answer:

    [SYNTHESIS] round returned (53 -> 54 msgs); still control JSON: False

`_reuse_needs_synthesis` returned False because the KEY 'message2userfinal'
was present; it never read the VALUE.  This file already owns the rule that
an unfilled template is not an answer -- _reuse_group_terminate's
"'<your answer here>' is the steer's own template" branch -- it just was not
shared.  One predicate, both callers.

    python -m pytest tests/unit/test_synthesis_steer_placeholder_not_copyable.py --noconftest -q
"""
import pytest


def _rr():
    return pytest.importorskip('hartos.reuse_recipe')


class _Chat:
    """Minimal stand-in: _reuse_needs_synthesis reads only `.messages`."""

    def __init__(self, *contents):
        self.messages = [{'name': 'Assistant', 'role': 'assistant',
                          'content': c} for c in contents]
        self.agents = []


class TestNoCopyablePlaceholder:
    """The literal the model sent the user must not be in the prompt."""

    def test_complete_steer_carries_no_angle_bracket_placeholder(self):
        rr = _rr()
        assert '<your answer here>' not in rr._REUSE_SYNTHESIS_STEER, (
            "the COMPLETE steer still tells the model to reply with the "
            "literal '<your answer here>' -- this is the steer that fired at "
            "18:25:22 (unrun=none) and put those 18 characters in front of "
            "the user as the whole answer")

    def test_incomplete_steer_carries_no_angle_bracket_placeholder(self):
        rr = _rr()
        assert '<your answer here>' not in rr._REUSE_SYNTHESIS_STEER_INCOMPLETE

    def test_neither_steer_orders_an_exact_reply(self):
        """'with exactly' is the copy instruction, independent of the token."""
        rr = _rr()
        for name in ('_REUSE_SYNTHESIS_STEER',
                     '_REUSE_SYNTHESIS_STEER_INCOMPLETE'):
            assert 'with exactly' not in getattr(rr, name), (
                f"{name} still orders the model to reply 'with exactly' a "
                f"literal; whatever literal follows will be copied")


class TestTheContractIsUnchanged:
    """The wording fix must not break the extractor or the formatter."""

    def test_both_steers_still_name_the_extractor_key(self):
        rr = _rr()
        for name in ('_REUSE_SYNTHESIS_STEER',
                     '_REUSE_SYNTHESIS_STEER_INCOMPLETE'):
            assert 'message2userfinal' in getattr(rr, name), (
                f"{name} must keep naming the key the extractor unwraps "
                f"(get_agent_response); a different shape produces an answer "
                f"nobody reads -- #797/D31")

    def test_incomplete_steer_still_formats(self):
        """Stray single braces in the shared tail would raise here."""
        rr = _rr()
        out = rr._REUSE_SYNTHESIS_STEER_INCOMPLETE.format(unrun='a_tool')
        assert 'a_tool' in out
        assert '{unrun}' not in out

    def test_the_shape_instruction_has_one_home(self):
        """Both steers must SHARE the sentence, not carry two copies.

        D44 was fixable in only one steer precisely because the two constants
        duplicate their wording; this pins the shared half.
        """
        rr = _rr()
        shape = rr._REUSE_SYNTHESIS_ANSWER_SHAPE
        assert shape and shape in rr._REUSE_SYNTHESIS_STEER
        assert shape in rr._REUSE_SYNTHESIS_STEER_INCOMPLETE


class TestWrittenAnswerPredicate:
    """One predicate for 'is this value a real answer', two callers."""

    def test_template_value_is_not_a_written_answer(self):
        assert _rr()._reuse_is_written_answer('<your answer here>') is False

    def test_empty_value_is_not_a_written_answer(self):
        rr = _rr()
        assert rr._reuse_is_written_answer('   ') is False
        assert rr._reuse_is_written_answer(None) is False

    def test_real_text_is_a_written_answer(self):
        assert _rr()._reuse_is_written_answer(
            'The directory listing for Nunba was retrieved.') is True

    def test_group_terminate_still_uses_it(self):
        """The existing caller keeps its measured behaviour."""
        rr = _rr()
        assert rr._reuse_group_terminate(
            {'name': 'Assistant',
             'content': '{"message2userfinal": "<your answer here>"}'}) is False
        assert rr._reuse_group_terminate(
            {'name': 'Assistant',
             'content': '{"message2userfinal": "the listing was retrieved"}'}
        ) is True


class TestSynthesisGateReadsTheValue:
    """`still control JSON: False` on a template was a false negative."""

    def test_template_answer_still_needs_synthesis(self):
        rr = _rr()
        assert rr._reuse_needs_synthesis(
            _Chat('{"message2userfinal": "<your answer here>"}')) is True, (
            "an unfilled template is not 'the answer is already there' -- "
            "measured 18:25:22, the gate said False and the 18-character "
            "placeholder went to the user")

    def test_real_answer_does_not_need_synthesis(self):
        """Anti-vacuity: the gate must still recognise a genuine answer."""
        rr = _rr()
        assert rr._reuse_needs_synthesis(
            _Chat('{"message2userfinal": "The chatterbox_turbo worker '
                  'startup failure was diagnosed from the directory scan."}')
        ) is False

    def test_control_json_still_needs_synthesis(self):
        rr = _rr()
        assert rr._reuse_needs_synthesis(
            _Chat('{"status": "completed", "action_id": 1}')) is True
