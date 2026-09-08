"""Finishing the recipe must not be reported to the user as an empty reply.

THE DEFECT, measured live 2026-09-09 on agent 90210554431 (research.local.beacon).

_advance_or_steer (reuse_recipe.py:3439) documents two outcomes:

    True  — the next action's message, or a re-steer, was posted; the
            caller should keep looping.
    False — no next action and nothing to steer; the caller should end
            the turn.

`advanced=False` covers TWO different situations:
  (a) a fabrication refusal -> _reuse_fab_steer_message yields a steer -> True
  (b) THE LAST ACTION JUST FINISHED -> no next action, and nothing to steer
      because the action was not fabricated -> steer_message falsy -> False

Case (b) is SUCCESS.  But all six call sites answer False with `return ''`, so
an agent that completes its whole recipe returns the empty string.

WHAT THAT COSTS, end to end (drive 9, timestamps from gui_app.log):
    00:04:36,073  [FAB-GUARD] action 4 names tool(s) ['save_to_long_term_memory'];
                  executed=['save_to_long_term_memory']; unrun=[]   <- guard PASSES
    00:04:36,492  [FAB-GUARD] watermark for action 5: 20 pre-existing tool call(s)
                  <- advanced PAST the last action of a 4-action recipe
    00:04:36,552  Nunba routes.chatbot_routes WARNING - LangChain returned error or
                  empty: {... 'response': '', 'text': '', '_tier': 'direct'}
419 ms.  The '' then trips Nunba's empty-reply check, which drops the turn to the
Tier-2 raw-llama fallback (chatbot_routes.py:3279 -> :3346, source='llama_local').
Tier-2 sends a bare messages=[{"role":"user","content":text}] with no tools, no
recipe, no memory -- so the user received a training-data answer CONTRADICTING the
tool-grounded summary the pipeline had already produced and saved to the memory
graph, and misstating what was stored.  The agent did its job; the user was told
otherwise.

WHY `break` IS THE FIX, AND WHY IT IS NOT A NEW CODE PATH.  Both loops ALREADY
end with a working extractor that turns the group log into the user's reply:
  - get_agent_response, reuse_recipe.py:4258-4304 -- steps back past a bare
    'TERMINATE', unwraps message2userfinal / message2 via retrieve_json (with a
    regex fallback), strips '@user '/'@userproxy ', ends `return
    last_message['content']`.
  - chat_agent's while2, reuse_recipe.py:5288-5310 -- the same shape.
`break` reaches the extractor that is already there.  No new function, no second
implementation, nothing to keep in sync.

NOT EVERY `return ''` IN THIS FILE IS THE BUG -- do not widen this test.
get_agent_response:4229 and :4238 return '' immediately AFTER
send_message_to_user1(...), i.e. the reply was already delivered out of band and
there is deliberately nothing left to hand back.  Those are correct and this test
must keep passing if they stay.  The guard below is scoped to the
`if not _advance_or_steer(...)` blocks and nothing else.

    python -m pytest tests/unit/test_completion_is_not_an_empty_reply.py --noconftest -q
"""
import ast
import pathlib

import pytest


_SRC = pathlib.Path(__file__).resolve().parents[2] / 'hartos' / 'reuse_recipe.py'


def _tree():
    return ast.parse(_SRC.read_text(encoding='utf-8'))


def _calls_advance_or_steer(node):
    """True when `node` is the test of `if not _advance_or_steer(...)`."""
    if not isinstance(node, ast.UnaryOp) or not isinstance(node.op, ast.Not):
        return False
    call = node.operand
    return (isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == '_advance_or_steer')


def _advance_guard_blocks():
    """Every `if not _advance_or_steer(...):` statement in the module."""
    return [n for n in ast.walk(_tree())
            if isinstance(n, ast.If) and _calls_advance_or_steer(n.test)]


def _returns_empty_string(stmt):
    return (isinstance(stmt, ast.Return)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value == '')


class TestCompletionKeepsTheAnswer:

    def test_the_guard_blocks_exist_at_all(self):
        """If this fails the AST shape changed and the guard below went vacuous."""
        blocks = _advance_guard_blocks()
        assert len(blocks) >= 5, (
            'expected the _advance_or_steer guard blocks to still be present; '
            'found %d. A guard that matches nothing cannot fail -- re-point it '
            'before trusting this file.' % len(blocks))

    def test_no_call_site_answers_completion_with_an_empty_string(self):
        """THE DEFECT: all 6 sites did `return ''`, discarding a finished turn."""
        offenders = [b.lineno for b in _advance_guard_blocks()
                     if any(_returns_empty_string(s) for s in b.body)]
        assert offenders == [], (
            "reuse_recipe.py lines %s answer `_advance_or_steer() is False` with "
            "`return ''`. False means 'no next action and nothing to steer' -- "
            "which is what a SUCCESSFULLY FINISHED recipe looks like. Ending the "
            "turn is right; returning '' throws the agent's answer away and, in "
            "Nunba, silently reroutes the user to the tool-less Tier-2 fallback. "
            "Use `break` so the extractor already sitting after the loop "
            "(:4258-4304 / :5288-5310) produces the reply."
            % offenders)

    @pytest.mark.parametrize('lineno', [b.lineno for b in _advance_guard_blocks()])
    def test_each_call_site_ends_the_turn_by_breaking(self, lineno):
        """Per-site so a failure names the line that regressed."""
        block = next(b for b in _advance_guard_blocks() if b.lineno == lineno)
        assert any(isinstance(s, ast.Break) for s in block.body), (
            'reuse_recipe.py:%d must `break` out to the post-loop extractor so a '
            'completed recipe still returns its final message.' % lineno)


class TestTheExtractorItBreaksToStillExists:
    """`break` is only correct while something after the loop builds the reply."""

    def test_both_loops_are_followed_by_a_content_returning_extractor(self):
        src = _SRC.read_text(encoding='utf-8')
        assert src.count("return last_message['content']") >= 2, (
            "the fix relies on the existing post-loop extractors in "
            "get_agent_response and chat_agent; if they stopped returning "
            "last_message['content'], `break` would yield an empty turn again "
            'and this whole defect returns by another door')
