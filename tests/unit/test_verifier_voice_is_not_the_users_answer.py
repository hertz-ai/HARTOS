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

THE FIX, as first shipped (501cf51fb), was one entry on an existing constant:
`_REUSE_STEER_INITIATOR_NAMES` already carried this semantic for ChatInstructor
-- its own comment says such messages "are instructions TO the group, so they
can never be the group's answer, however they are worded".

THAT CONSTANT HAD A READER ASKING A DIFFERENT QUESTION.  `_reuse_written_answer`
walks back from the tail for the answer the action wrote and STOPS at the first
seat in that tuple -- the tuple is its "this action's dispatch" bound.  The
verdict is the tail whenever an action advances, so from 501cf51fb on the walk
stopped on it and never reached the answer.  Measured 2026-09-13: the
answer-recovery suites pass 38/38 at 501cf51fb^ and fail 4 at 501cf51fb.  The
seats are therefore two names now:

    _REUSE_STEER_INITIATOR_NAMES   ("ChatInstructor",)  the walk-back bound
    _REUSE_NON_ANSWER_SEATS        the above + StatusVerifier, never an answer

This file guards the second set, the two readers that must consult it, and --
by VALUE, not by name -- that the walk-back's bound does not hold the verifier.

WHAT THIS TEST CAN AND CANNOT PROVE.  It is an AST guard, matching this suite's
convention (reuse_recipe.py is far too heavy to import in a unit test), so it
proves the seat is registered and still consulted.  It does NOT prove the
user-visible outcome -- that requires the live re-run on the failing path, per
the standing rule to verify to the answer and not to the furthest line reached.

RED BEFORE GREEN: at 501cf51fb^ no seat set holds the verifier and
test_verifier_seat_is_registered fails; at 501cf51fb the walk-back reads a set
holding the verifier and test_the_walk_back_is_bounded_by_the_steering_seat_only
fails.

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
_CONST = '_REUSE_NON_ANSWER_SEATS'


@pytest.fixture(scope='module')
def tree():
    return ast.parse(_SRC.read_text(encoding='utf-8', errors='replace'))


def _module_assigns(tree):
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value
    return out


def _resolve(assigns, value):
    """Evaluate a seat set the way the module builds it: a tuple literal, a
    name bound to one, or a `+` of those."""
    if isinstance(value, ast.Name):
        return _resolve(assigns, assigns[value.id])
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        return _resolve(assigns, value.left) + _resolve(assigns, value.right)
    return ast.literal_eval(value)


def _seat_names(tree, name=_CONST):
    assigns = _module_assigns(tree)
    if name not in assigns:
        raise AssertionError('%s is not defined in reuse_recipe.py' % name)
    return _resolve(assigns, assigns[name])


def test_verifier_seat_is_registered(tree):
    """RED pre-fix: no set held the verifier, so its prose reached the user
    (2026-09-11 19:09, agent 53298912627)."""
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

    Two consult it, each for "is this the user's answer?":
      _reuse_group_terminate          does this message end the round
      _reuse_message_is_user_answer   is this the user's answer   <- the defect
    A migration that inlines either re-opens the hole for that reader alone,
    which is the exact shape this file's own comments record twice.
    """
    readers = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name) and inner.id == _CONST:
                    readers.add(node.name)
    for expected in ('_reuse_group_terminate',
                     '_reuse_message_is_user_answer'):
        assert expected in readers, (
            '%s no longer reads %s -- the seat refusal is silently dead for '
            'that reader. Readers found: %s'
            % (expected, _CONST, sorted(readers)))


def test_the_walk_back_is_bounded_by_the_steering_seat_only(tree):
    """RED at 501cf51fb: the walk-back read the tuple that held the verifier,
    so the verdict at the tail ended the walk before the answer it follows.

    Resolved by VALUE, not by name, so renaming a set cannot hide it: every
    seat set `_reuse_written_answer` reads must hold the steering seat (its
    dispatch bound) and must not hold the verifier (which it has to step past,
    and which _reuse_message_is_user_answer already refuses as an answer).
    """
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == '_reuse_written_answer'), None)
    assert fn is not None, '_reuse_written_answer is gone'
    assigns = _module_assigns(tree)
    seat_sets = {}
    for inner in ast.walk(fn):
        if not (isinstance(inner, ast.Name) and inner.id in assigns):
            continue
        try:
            value = _resolve(assigns, assigns[inner.id])
        except Exception:
            continue
        if isinstance(value, tuple) and _STEER_SEAT in value:
            seat_sets[inner.id] = value
    assert seat_sets, (
        '_reuse_written_answer reads no seat set holding %r -- its walk-back '
        'has lost the bound at this action\'s dispatch' % _STEER_SEAT)
    for name, seats in seat_sets.items():
        assert _VERIFIER_SEAT not in seats, (
            '_reuse_written_answer stops at every seat in %s = %r, and the '
            'verifier\'s verdict is the tail whenever an action advances -- '
            'the walk ends on it and never reaches the answer the action wrote '
            '(4 tests red at 501cf51fb).' % (name, seats))


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
