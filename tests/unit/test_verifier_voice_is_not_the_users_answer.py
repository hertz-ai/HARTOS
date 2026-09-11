"""The StatusVerifier's own voice must never be delivered as the user's answer.

THE DEFECT, measured live 2026-09-11 19:09 on agent 53298912627, session
6c2dc0fc-7c93-4fe0-973e-f7466ff63f29_53298912627.  The entire reply the user
received was the verifier describing its own role:

    "I have not executed any tools in this conversation.  As a status
     verification agent, I can only report on actions that have been performed
     by the assistant and verified by tool results.  Since there are no tool
     results to review, I cannot generate a detailed recipe for the 'Respond to
     user' action or provide specific steps, fallback strategies, or Python code
     based on actual execution data.  I can only confirm that no actions have
     been completed yet."

Every other signal in that turn was healthy, which is what makes this purely a
DELIVERY defect and not an execution one: the session-attributed pointer
advanced 1 -> 2 (held 8s then 15s, inside the 15-117s band measured the same
day from a session that completed nine actions in 592s), the force/stuck-loop
guard fired zero times, and the turn wrote +144,200 B to memory_graph and
+87,606 B across 64 files in agent_data.  The agent did the work; the user was
handed the verifier's plumbing instead.

WHY THE EXISTING CHECKS ALL MISSED IT.  `_reuse_message_is_user_answer` already
refuses the verifier's VERDICT, but only through its tail test
`retrieve_json(content)` -> dict with a 'status' key.  That sees JSON only.
When the verifier answers in PROSE it:
  * parses to no dict                      -> the verdict test cannot fire
  * carries none of this module's markers   -> `_reuse_is_pipeline_text` declines
    it, CORRECTLY: that predicate's stated contract is "text THIS MODULE wrote
    to steer the group", and the verifier's prose is model-authored
  * names no agent, is not role='tool', is not 'TERMINATE'
so it fell through to the function's final `return True  # prose for the user`.

THE FIX is one entry on an existing constant, not a new predicate:
`_REUSE_STEER_INITIATOR_NAMES` already carries exactly this semantic for
ChatInstructor -- its own comment says such messages "are instructions TO the
group, so they can never be the group's answer, however they are worded" -- and
it is already consulted by every reader that needs it.  Adding the verifier seat
is picked up by all of them at once, which is why this test guards the CONSTANT
and its READERS rather than a new code path.

WHAT THIS TEST CAN AND CANNOT PROVE.  It is an AST guard, matching this suite's
convention (reuse_recipe.py is far too heavy to import in a unit test), so it
proves the seat is registered and still consulted.  It does NOT prove the
user-visible outcome -- that requires the live re-run on the failing path, per
the standing rule to verify to the answer and not to the furthest line reached.

RED BEFORE GREEN: against HEAD~ the constant is ("ChatInstructor",) and
test_verifier_seat_is_registered fails on the membership assertion.

    python -m pytest tests/unit/test_verifier_voice_is_not_the_users_answer.py --noconftest -q
"""
import ast
import pathlib

import pytest

_SRC = (pathlib.Path(__file__).resolve().parents[2]
        / 'hartos' / 'reuse_recipe.py')

# The seat autogen gives the verifier.  Both constructions name it this:
# reuse_recipe.py:1593 (main stack) and :2310 (the second pair).
_VERIFIER_SEAT = 'StatusVerifier'
# The harness's steering UserProxy, already in the set before this guard.
_STEER_SEAT = 'ChatInstructor'
_CONST = '_REUSE_STEER_INITIATOR_NAMES'


@pytest.fixture(scope='module')
def tree():
    return ast.parse(_SRC.read_text(encoding='utf-8', errors='replace'))


def _seat_names(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == _CONST:
                return ast.literal_eval(node.value)
    raise AssertionError('%s is not defined in reuse_recipe.py' % _CONST)


def test_verifier_seat_is_registered(tree):
    """RED pre-fix: the tuple held only ChatInstructor, so the verifier's
    prose reached the user (2026-09-11 19:09, agent 53298912627)."""
    names = _seat_names(tree)
    assert _VERIFIER_SEAT in names, (
        "%s must contain %r -- without it the verifier's PROSE (as opposed to "
        "its verdict JSON, which the tail retrieve_json check already refuses) "
        "falls through _reuse_message_is_user_answer to "
        "`return True  # prose for the user` and is delivered as the whole "
        "reply." % (_CONST, _VERIFIER_SEAT))


def test_steer_seat_is_not_dropped(tree):
    """The verifier seat is ADDED beside ChatInstructor, never instead of it.

    Guards the shape of the fix: this family has a documented history of a
    change closing one producer while opening another, which is why
    `_reuse_is_pipeline_text` was rewritten to ask "did this module write
    this" rather than naming producers one at a time.
    """
    assert _STEER_SEAT in _seat_names(tree), (
        '%s must still contain %r -- the three chat_instructor.initiate_chat '
        'sites make it this file\'s steering initiator.' % (_CONST, _STEER_SEAT))


def test_every_reader_still_consults_the_seat_set(tree):
    """The constant is only worth anything while its readers read it.

    Three consult it today and each is load-bearing for a different question:
      _reuse_group_terminate          does this message end the round
      _reuse_message_is_user_answer   is this the user's answer   <- the defect
      _reuse_written_answer           walking back for a real answer
    A migration that inlines any of them re-opens the hole for that reader
    alone, which is the exact shape this file's own comments record twice.
    """
    readers = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name) and inner.id == _CONST:
                    readers.add(node.name)
    for expected in ('_reuse_group_terminate',
                     '_reuse_message_is_user_answer',
                     '_reuse_written_answer'):
        assert expected in readers, (
            '%s no longer reads %s -- the seat refusal is silently dead for '
            'that reader. Readers found: %s'
            % (expected, _CONST, sorted(readers)))


def test_answer_key_still_outranks_the_seat_check(tree):
    """A steer-seat message carrying the answer key IS the answer.

    `_reuse_message_is_user_answer` deliberately tests message2userfinal ABOVE
    the seat name so a seat that does carry the key is still delivered.  Adding
    the verifier seat must not invert that: measured 2026-09-11, StatusVerifier
    carried message2userfinal 0 times, but the ordering is the safety net if it
    ever does.
    """
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == '_reuse_message_is_user_answer'), None)
    assert fn is not None, '_reuse_message_is_user_answer is gone'

    key_line = seat_line = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and node.value == 'message2userfinal':
            key_line = node.lineno if key_line is None else min(key_line, node.lineno)
        if isinstance(node, ast.Name) and node.id == _CONST:
            seat_line = node.lineno if seat_line is None else min(seat_line, node.lineno)
    assert key_line is not None, 'the message2userfinal branch is gone'
    assert seat_line is not None, 'the seat check is gone'
    assert key_line < seat_line, (
        'the message2userfinal check (line %d) must stay ABOVE the seat check '
        '(line %d): a steer-seat message that carries the answer key is the '
        'answer, and inverting these would swallow it.' % (key_line, seat_line))
