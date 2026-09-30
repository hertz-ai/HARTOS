"""A verdict settles only the action it answers (#106).

The owner, 2026-09-14: "how can another action's verdict enter this action".
Three create-loop consumers read the StatusVerifier's action_id and one of
them trusted it: state_transition force-completed the claimed id and wrote the
verdict's action text over that action, and its recipe save named the file,
moved current_action and terminated by the claimed id.  The verifier could also
rewrite one action or the whole plan ('updated', entire_actions).  Now
settled_action_id is the one rule (the verdict settles the posted action and a
differing claim is logged), reuse and create both call it, and no verdict path
writes the plan.  The whole-flow behaviour is pinned by
test_create_loop_end_to_end.py; this file pins the rule and the structure.

    python -m pytest tests/unit/test_verdict_settles_the_posted_action.py --noconftest -q
"""
import ast
import logging
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CREATE = _ROOT / 'hartos' / 'create_recipe.py'
_MUTATORS = {'append', 'extend', 'insert', 'pop', 'remove', 'clear', 'sort',
             'reverse', '__setitem__'}


def _hooks():
    from hartos import lifecycle_hooks
    return lifecycle_hooks


def _tree():
    return ast.parse(_CREATE.read_text(encoding='utf-8'))


@pytest.mark.parametrize('claimed, current, expected', [
    (2, 2, 2), ('2', 2, 2), (2.0, 2, 2), (3, 2, 2), (1, 2, 2),
    (None, 2, 2), ('x', 2, 2)])
def test_the_posted_action_is_the_one_settled(claimed, current, expected):
    assert _hooks().settled_action_id(claimed, current) == expected


def test_a_differing_claim_is_logged_and_a_matching_one_is_not(caplog):
    hooks = _hooks()
    with caplog.at_level(logging.WARNING, logger=hooks.logger.name):
        hooks.settled_action_id(2, 2)
        assert not caplog.records
        hooks.settled_action_id(3, 2)
    assert any('[HALLUCINATION?] LLM claims action_id=3 but pipeline has 2'
               in r.getMessage() for r in caplog.records)


def test_a_posted_action_that_is_not_an_int_is_left_as_it_is():
    assert _hooks().settled_action_id(3, 'a') == 'a'


def test_no_verdict_path_writes_the_plan():
    """The plan belongs to the decomposition; a verdict only sets state.  Every
    write to X.actions in create_recipe was a verdict writing the plan
    (census 2026-09-14, confirmed by hartos-3e), so none may come back."""
    writes = []
    for node in ast.walk(_tree()):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            base = t.value if isinstance(t, ast.Subscript) else t
            if isinstance(base, ast.Attribute) and base.attr == 'actions':
                writes.append(node.lineno)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _MUTATORS
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == 'actions'):
            writes.append(node.lineno)
    assert writes == [], f'create_recipe.py writes the plan at lines {writes}'


def test_the_verifier_is_not_offered_a_plan_rewrite():
    src = _CREATE.read_text(encoding='utf-8')
    assert 'Action Updated' not in src
    assert '"status": "updated"' not in src
    assert "json_obj['updated_action']" not in src
    assert "'entire_actions'" not in src


def test_every_create_consumer_uses_the_one_rule():
    """The outer loop's verdict pickup, state_transition's 'completed' branch
    and its per-action recipe save all call the rule, and create keeps no copy
    of its own."""
    calls = [n for n in ast.walk(_tree())
             if isinstance(n, ast.Call)
             and getattr(n.func, 'id', None) == 'settled_action_id']
    assert len(calls) >= 3, [c.lineno for c in calls]
    assert '[HALLUCINATION?]' not in _CREATE.read_text(encoding='utf-8'), (
        'create_recipe must not carry its own copy of the claimed-id check')
