"""The autonomy nudge is a FOURTH pipeline producer, and it reached the user.

MEASURED LIVE 2026-09-10 12:53-12:55, agent 77712340019 ("google_search on
the topic" -> "write a 2-bullet summary and send it to the user"), driven
through the real /chat route as its owner.  The agent did its work:

    12:54:41  [FAB-GUARD] action 1 names ['google_search'];
              executed=['google_search']; unrun=[]      <- the tool really ran
    12:54:55  current_action_id: 2                      <- honest advance
    12:55:07  [BREAKDOWN] action 2 persisted 2 subtask(s) (ok=True)

and then the user got this as the whole reply, HTTP 200 in 117s:

    "You should complete this task independently. Feel free to make
     reasonable assumptions where necessary"

That sentence is not the agent's summary.  It is THIS MODULE's own nudge,
posted into the group chat at reuse_recipe.py:5001, recovered from the group
log by _reuse_written_answer and delivered as the deliverable:

    12:55:21  [SYNTHESIS] the action already wrote the answer — recovered 101
              chars from the group log, no steer posted (... from=Assistant,
              head='You should complete this task independently. ...')

_reuse_is_pipeline_text is the canonical "did this module write this" test
and its own docstring names three producers, adding that it "kept being one
producer behind".  This is the fourth: the nudge ships no marker, so both
callers (_reuse_message_is_user_answer and _reuse_written_answer) read it as
the agent talking to the user.

The fix belongs in that ONE predicate — not a second check at the synthesis
site — because the docstring already records what happens when the two
readers are fixed separately.

    python -m pytest tests/unit/test_reuse_autonomy_nudge_is_pipeline_text.py --noconftest -q
"""
import ast
import io
import os

MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'hartos', 'reuse_recipe.py')

# Verbatim from the live 12:55:21 log line.
NUDGE = ('You should complete this task independently. Feel free to make '
         'reasonable assumptions where necessary')


def _src():
    with io.open(MODULE, encoding='utf-8', errors='replace') as fh:
        return fh.read()


class TestTheNudgeIsRecognisedAsPipelineText:
    """The predicate must claim it, or every caller keeps leaking it."""

    def test_predicate_refuses_the_nudge(self):
        from hartos.reuse_recipe import _reuse_is_pipeline_text
        assert _reuse_is_pipeline_text(NUDGE), (
            'the autonomy nudge is text this module wrote to steer the '
            'group; the predicate does not recognise it, so it reaches the '
            'user as the agent answer (live 2026-09-10 12:55:21)')

    def test_written_answer_does_not_return_the_nudge(self):
        """The exact 77712340019 shape: the nudge is the tail."""
        from hartos.reuse_recipe import _reuse_written_answer

        class _GC:
            messages = [
                {'name': 'Assistant', 'role': 'assistant', 'content': NUDGE},
            ]
        got = _reuse_written_answer(_GC())
        assert got is None, (
            f'recovered the module own nudge as "the answer the action '
            f'already wrote" -- this is what the user read instead of the '
            f'2-bullet summary. Got {got!r}')


class TestOneSourceOfTruth:
    """The producer must emit the same constant the predicate tests.

    A second literal is how this family stayed one producer behind: the
    predicate can only recognise what the producer actually emits.
    """

    def test_nudge_is_a_named_constant_not_an_inline_literal(self):
        """MODULE level only.

        The first cut of this test walked the whole tree and passed on the
        unfixed file, because the defect line is itself an assignment --
        ``message = 'You should complete this task independently...'`` binds
        a local Name to the Constant and satisfies a naive walk.  A guard
        that cannot fail on the broken state is not a guard, so the search
        is pinned to tree.body: a local in one function is exactly the
        drift this is meant to stop.
        """
        tree = ast.parse(_src())
        module_level = set()
        for node in tree.body:                      # NOT ast.walk
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if (isinstance(t, ast.Name)
                            and isinstance(node.value, ast.Constant)
                            and node.value.value == NUDGE):
                        module_level.add(t.id)
        assert module_level, (
            'the nudge is still an inline literal in a function '
            '(reuse_recipe.py:5001). Promote it to a MODULE constant so the '
            'producer and the predicate cannot drift apart.')

    def test_the_text_is_held_in_exactly_one_place(self):
        """Counted over AST constants, not raw source text.

        The first cut asserted ``_src().count(NUDGE) == 1`` and broke the
        moment the constant was written as two adjacent literals across two
        lines -- the joined text then appears nowhere contiguously, so the
        count is 0 and a correct fix looks like a failure.  The invariant is
        "one place in the code holds this string", and implicit
        concatenation is still one place; ast folds it into one Constant.
        """
        nodes = [n for n in ast.walk(ast.parse(_src()))
                 if isinstance(n, ast.Constant) and n.value == NUDGE]
        assert len(nodes) == 1, (
            f'the nudge text is held in {len(nodes)} places -- a surviving '
            f'inline copy will drift from the constant the predicate tests')


class TestPrecisionNoRegression:
    """Refuse the module own markers, never the agent's sentiment.

    The predicate docstring is explicit that an honest failure report must
    still reach the user; over-refusing here would silence real answers.
    """

    def test_honest_failure_report_still_reaches_the_user(self):
        from hartos.reuse_recipe import _reuse_is_pipeline_text
        honest = ('Since no specific text was provided in your input, I '
                  'could not summarize anything.')
        assert not _reuse_is_pipeline_text(honest), (
            'this is the agent talking to the user (measured 10:21:11) and '
            'must not be swallowed')

    def test_a_real_answer_is_not_pipeline_text(self):
        from hartos.reuse_recipe import _reuse_is_pipeline_text
        answer = ('- RISC-V server silicon shipped in volume this year.\n'
                  '- Vendors now target datacentre-class core counts.')
        assert not _reuse_is_pipeline_text(answer)

    def test_empty_and_none_are_not_pipeline_text(self):
        from hartos.reuse_recipe import _reuse_is_pipeline_text
        assert not _reuse_is_pipeline_text('')
        assert not _reuse_is_pipeline_text(None)
