"""/chat must stamp the thread-local BEFORE any of its early returns.

MEASURED LIVE 2026-09-09 22:45-23:22, agent 18088688973.  136 tool executions
in agent_system.log, and:

  * ZERO carried a session -- core/tool_logging.py's `_session_suffix()` reads
    thread_local_data.get_user_id()/get_prompt_id() and both were None.
  * ZERO `chat.stage` publishes -- `_emit_tool_call_stage` publishes the
    per-tool UI status ONLY when user_id is truthy, so task #509's whole
    feature ("Searching…" instead of a generic "Thinking…") is DEAD on the
    reuse path.  That is a shipped, user-visible capability that never fires.

ROOT CAUSE, read not inferred.  `chat()` begins at hart_intelligence_entry.py
:8986.  Its thread-local stamp lives at :10270 (`set_user_id`) and :10275
(`set_prompt_id`).  But FIFTEEN `return` statements sit above :10270 -- the
whole agent-bound REUSE branch exits at four of them.  So for every reuse turn
the request finishes without the thread-local ever being written, and anything
downstream that reads it sees None.

An earlier hypothesis of mine -- "tools run on a different thread, so the
thread-local doesn't propagate" -- is REFUTED by this: the values are never
written on ANY thread for these paths.  No propagation machinery is needed.

WHY AN AST TEST.  The property is an ORDERING one ("the stamp precedes every
exit"), which is exactly what a syntax tree can state and a runtime test
cannot easily cover across 15 branches.  It is also not vacuous: it fails
today, on the real file, for the real reason.

THE TRAP THIS TEST MUST NOT ENCOURAGE.  Do NOT "fix" this by hoisting the
whole block: `prompt_id` is MUTATED between the reuse branch and :10275 --
the probe / intermediate / else arms each set `prompt_id = 0`.  Hoisting the
setter above that mutation would stamp the real agent id for paths that are
supposed to record 0.  The correct shape is an EARLY stamp (before the first
return) with the EXISTING :10275 stamp left in place as the authority for
paths that reach it.  test_later_stamp_survives below pins that.

    python -m pytest tests/unit/test_chat_seeds_threadlocal_before_returning.py --noconftest -q
"""
import ast
import io
import os

import pytest

_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'hart_intelligence_entry.py')


def _chat_node():
    tree = ast.parse(io.open(_SRC, encoding='utf-8', errors='replace').read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'chat':
            return node
    pytest.fail("no def chat() in hart_intelligence_entry.py -- re-point this "
                "test rather than deleting it")


def _setter_lines(node, attr):
    """Line numbers of every `<something>.<attr>(...)` call inside `node`."""
    out = []
    for n in ast.walk(node):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == attr):
            out.append(n.lineno)
    return sorted(out)


def _return_lines(node):
    return sorted(n.lineno for n in ast.walk(node) if isinstance(n, ast.Return))


def _identity_line(node):
    """Line where `prompt_id` is bound from the request body.

    The stamp cannot precede EVERY return: chat()'s first 10 exits are
    auth / rate-limit / missing-field guards that fire before a user_id or
    prompt_id exists at all, and they run no tools.  Demanding a stamp there
    would be unsatisfiable, and a test that cannot be satisfied gets deleted
    rather than fixed.  The real property is: once identity is KNOWN, no exit
    may happen before it is stamped.  Measured 2026-09-09: 10 returns sit
    above this line, 25 between it and the stamp -- the 25 are the defect.
    """
    for n in ast.walk(node):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id == 'prompt_id'
                and isinstance(n.value, ast.Call)
                and isinstance(n.value.func, ast.Attribute)
                and n.value.func.attr == 'get'):
            return n.lineno
    pytest.fail("could not find `prompt_id = data.get(...)` in chat() -- "
                "re-point this test rather than relaxing it")


class TestTheStampPrecedesEveryExit:

    @pytest.mark.parametrize('setter', ['set_user_id', 'set_prompt_id'])
    def test_first_stamp_is_above_first_return(self, setter):
        chat = _chat_node()
        stamps = _setter_lines(chat, setter)
        returns = _return_lines(chat)

        assert stamps, (
            f"chat() never calls {setter} at all -- the thread-local is the "
            f"only channel core/tool_logging.py and _emit_tool_call_stage have "
            f"for identity")
        assert returns, "chat() has no return; re-point this test"

        ident = _identity_line(chat)
        first_stamp = stamps[0]
        # Exits that happen AFTER identity is known but BEFORE it is stamped.
        unstamped = [ln for ln in returns if ident < ln < first_stamp]
        assert not unstamped, (
            f"{setter} is first called at line {first_stamp}, but identity is "
            f"known from line {ident} and {len(unstamped)} of chat()'s "
            f"{len(returns)} returns fire in between -- e.g. lines "
            f"{unstamped[:5]}. The agent-bound REUSE branch exits through "
            f"those, so the thread-local is never written for a reuse turn. "
            f"Live 2026-09-09: 136 tool executions logged no session and the "
            f"per-tool UI status (#509) published zero times.")


class TestTheLaterStampStillWins:
    """Guards the regression an over-eager hoist would introduce."""

    def test_later_stamp_survives(self):
        """`prompt_id` is zeroed AFTER the reuse branch; that stamp must stay.

        probe / intermediate / else each assign `prompt_id = 0` below the
        reuse branch.  If the early stamp were the ONLY one, those paths would
        leave the real agent id in the thread-local instead of 0.
        """
        chat = _chat_node()
        stamps = _setter_lines(chat, 'set_prompt_id')
        assert len(stamps) >= 2, (
            "expected an EARLY set_prompt_id (before the first return) AND "
            "the original one after the probe/intermediate/else arms that "
            "reset prompt_id to 0. Found %d call(s) at %s -- a single early "
            "call would regress those paths." % (len(stamps), stamps))
