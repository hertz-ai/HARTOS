"""A banked 'no' step does not strand REUSE: the person's reply drives it on.

#160 stamps can_perform_without_user_input 'no' (core.constants
.USER_ANSWERS_THIS_STEP) on a banked action the person answered at creation.
hartos-f9's review asked whether a replay could then advance past that step at
all: the three autonomy-gated sites in while1 (the under-report escape, the
autonomy nudge, the [REUSE-NODRIVER] break) all read the flag, and the
no-driver comment near that break says every advance path tests the same
predicate.  Read at HEAD, the two paths a reply travels are NOT gated:

  * the turn-start dispatch: the reuse turn posts the person's message into
    the group once per turn (user_proxy.initiate_chat(manager,
    message=_reuse_seed_message(...))) before while1, under no autonomy test
    and no loop;
  * the completion advance inside while1 ('reuse-w1-completed') advances on a
    terminal 'completed' verdict at messages[-1] plus the one-advance-per-
    action guard, under no autonomy test.

So a 'no' step ends its turn at the ask ([REUSE-NODRIVER]), the reply turn
runs the group with the answer, the verifier closes the step and the walk
advances.  This is the path every model-written recipe already takes: the
VLM-writer note in reuse_recipe measured 'no' in 47 of 47 recipe files on this
box.  No harness in this suite drives reuse's turn behaviourally (every reuse
test reads the source), so the two contracts are pinned at the source, the way
tests/unit/test_reuse_no_driver_does_not_spin.py pins its exit.  If either
contract is ever gated on the flag, this fails before a replay strands.
"""
import ast
import os

SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')
AUTONOMY = '_reuse_action_is_autonomous'


def _tree():
    with open(SRC, encoding='utf-8') as fh:
        return ast.parse(fh.read())


def _parents(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_conditions(parents, node):
    """Source of every `if`/`while` test on the way up from `node`."""
    out = []
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.If, ast.While)):
            out.append(ast.unparse(node.test))
    return out


def _seed_dispatch(tree):
    """The one user_proxy.initiate_chat(..., message=_reuse_seed_message(...))."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == 'initiate_chat'
                and isinstance(fn.value, ast.Name) and fn.value.id == 'user_proxy'):
            continue
        for kw in node.keywords:
            if (kw.arg == 'message' and isinstance(kw.value, ast.Call)
                    and isinstance(kw.value.func, ast.Name)
                    and kw.value.func.id == '_reuse_seed_message'):
                found.append(node)
    return found


def _completion_advance(tree):
    """The `if` that advances on a terminal 'completed' verdict."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        src = ast.unparse(node.test)
        if "'completed'" in src and '_reuse_advanced_actions' in src:
            found.append(node)
    return found


def test_the_reply_turn_reaches_the_group_whatever_the_step_declares():
    tree = _tree()
    parents = _parents(tree)
    dispatches = _seed_dispatch(tree)
    assert len(dispatches) == 1, dispatches
    conditions = _enclosing_conditions(parents, dispatches[0])
    assert not any(AUTONOMY in c for c in conditions), conditions
    assert not any(isinstance(p, ast.While) for p in _ancestors(parents, dispatches[0])), (
        'the seed dispatch runs once per turn, before while1')


def test_a_completed_verdict_advances_the_walk_whatever_the_step_declares():
    tree = _tree()
    parents = _parents(tree)
    advances = _completion_advance(tree)
    assert len(advances) == 1, [ast.unparse(a.test) for a in advances]
    own = ast.unparse(advances[0].test)
    assert AUTONOMY not in own, own
    conditions = _enclosing_conditions(parents, advances[0])
    assert not any(AUTONOMY in c for c in conditions), conditions


def _ancestors(parents, node):
    while node in parents:
        node = parents[node]
        yield node
