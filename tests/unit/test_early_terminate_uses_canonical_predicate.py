"""The EARLY-TERMINATE guard must use the canonical terminate predicate.

``state_transition``'s EARLY-TERMINATE guard (create_recipe.py, added in
47183e457) ended the GroupChat round whenever ``'TERMINATE' in
content.upper()`` -- a CASE-INSENSITIVE substring.  The canonical predicate,
``hartos.helper._is_terminate_msg``, is case-SENSITIVE on the token, and every
agent's ``is_termination_msg`` in create_recipe.py already uses it.

Measured live 2026-09-13 on agent 87400889007, action 8 ``return_to_idle``.
The StatusVerifier's recipe reply was valid JSON whose own text says
"... if none detected, terminate idle timer and await new user instruction."
The guard read the word "terminate" as the control token and returned before
the recipe-save branch, every time: 31 [EARLY-TERMINATE] rounds, 31
[AUTO-ADVANCE] re-requests, attempt counter at 31, and no recipe banked.  The
replies that DID bank for actions 5-7 carried zero TERMINATE tokens, so the
token is not what makes a recipe reply valid.

RED before GREEN: against HEAD the guard's test expression calls ``.upper()``
and never calls ``_is_terminate_msg``, so the wiring test fails.

    venv/Scripts/python.exe -m pytest tests/unit/test_early_terminate_uses_canonical_predicate.py --noconftest -q
"""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "hartos" / "create_recipe.py"
_GUARD_LOG = "[EARLY-TERMINATE] last message contains TERMINATE"

# The action-8 reply's fallback_action, verbatim from the live wire.
ACTION_8_REPLY = (
    '{"status": "done", "action": "return_to_idle", '
    '"fallback_action": "Monitor system state for any residual processes; '
    'if none detected, terminate idle timer and await new user instruction.", '
    '"persona": "Status Verification Agent", "action_id": 8, "recipe": []}'
)


def _guard_test_expr():
    """The ``if`` condition of the block that logs the EARLY-TERMINATE line."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body_strings = [c.value for b in node.body for c in ast.walk(b)
                        if isinstance(c, ast.Constant) and isinstance(c.value, str)]
        if any(_GUARD_LOG in s for s in body_strings):
            hits.append(node.test)
    assert len(hits) == 1, "expected exactly one EARLY-TERMINATE guard, found %d" % len(hits)
    return hits[0]


def test_guard_calls_the_canonical_predicate():
    expr = _guard_test_expr()
    calls = {n.func.id for n in ast.walk(expr)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_is_terminate_msg" in calls, (
        "the EARLY-TERMINATE guard must decide with hartos.helper._is_terminate_msg, "
        "the predicate every agent's is_termination_msg already uses")


def test_guard_does_not_case_fold_the_content():
    expr = _guard_test_expr()
    uppers = [n for n in ast.walk(expr)
              if isinstance(n, ast.Attribute) and n.attr in ("upper", "lower", "casefold")]
    assert not uppers, (
        "case-folding the content turns the English word 'terminate' inside a "
        "recipe into the control token (live: 31 recipe replies discarded)")


def test_canonical_predicate_keeps_the_action_8_reply_and_honours_the_token():
    from hartos.helper import _is_terminate_msg

    assert not _is_terminate_msg({"content": ACTION_8_REPLY})
    assert _is_terminate_msg({"content": "TERMINATE"})
    assert _is_terminate_msg({"content": "Done. TERMINATE"})
