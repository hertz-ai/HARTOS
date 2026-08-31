"""#718 root cause — ONE terminate predicate, not two.

MEASURED across all 12 rotated logs (2026-08-29):

    START: get_response_group                      62
    WHILE LOOP ITERATION #                        462
    inside while                                  458
    ---- every marker below this line: ZERO ----
    lifecycle_hook_check_json_status                0
    [Main Loop] Action                              0
    [FLOW-RECIPE-SAVED]                             0
    [ALL-FLOWS-DONE]                                0

    marker INSIDE the create-loop terminate gate:   2
    [EARLY-TERMINATE] rounds ended:              274
        speaker=ChatInstructor 106 | StatusVerifier 104 | Assistant 64

The create loop runs (462 iterations) and dies at the same place every time.
Everything downstream of the terminate gate — the JSON parse, action
progression, after_all_actions_terminated, _save_flow_recipe, [ALL-FLOWS-DONE]
— lives INSIDE that gate, so none of it ever runs.  That is why no flow recipe
is ever written.

TWO PREDICATES FOR ONE SIGNAL:

  ENDER    create_recipe.py:2255  (state_transition, autogen speaker selector)
           'TERMINATE' in _last_content.upper()      <- any speaker, substring
           Its own log line states the contract it is trying to satisfy:
           "ending GroupChat round so the outer recipe loop can advance."

  CONSUMER create_recipe.py:4385  (the outer recipe loop)
           name == 'ChatInstructor' and content == 'TERMINATE'   <- exact

The ender fires on a substring from ANY speaker; the consumer demands an exact
string from ONE speaker.  So the round ends and the loop never advances.

THE CANONICAL PREDICATE ALREADY EXISTS — this is adoption, not new code.
create_recipe._is_terminate (:4068) strips the injected memory skeleton and
matches with startswith.  The comment above it already documents this exact
failure: "its presence on a terminate message defeats an exact == 'TERMINATE'
comparison (live 2026-08-23 21:52: the user's reply was 'TERMINATE' plus six
accumulated skeleton lines)."

It was adopted at :4967, :5014, :5075 (reuse path) and NOT at :635, :666,
:763, :4385 (create + timer paths).  A predicate adopted at some sites and not
others manufactures exactly this kind of disagreement.
"""
import ast
import os
import re

from hartos import create_recipe

_SRC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'hartos', 'create_recipe.py')


def _src():
    with open(_SRC_PATH, 'r', encoding='utf-8') as f:
        return f.read()


def test_skeleton_suffix_still_reads_as_terminate():
    """The live 2026-08-23 shape: TERMINATE plus accumulated skeleton lines."""
    msg = 'TERMINATE\nMetadata/skeleton of all keys: a, b, c'
    assert create_recipe._is_terminate(msg) is True
    assert msg != 'TERMINATE', 'premise: an exact comparison must miss this'


def test_plain_terminate_still_reads_as_terminate():
    assert create_recipe._is_terminate('TERMINATE') is True


def test_ordinary_reply_is_not_terminate():
    """Widening must not swallow a real user-bound answer."""
    for s in ('The capital of Japan is Tokyo.', '', 'terminating the process'):
        assert create_recipe._is_terminate(s) is False, s


def test_no_exact_terminate_comparison_remains():
    """No site may re-introduce a second predicate for this one signal."""
    offenders = []
    for node in ast.walk(ast.parse(_src())):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(o, ast.Eq) for o in node.ops):
            continue
        for cmp_node in node.comparators:
            if isinstance(cmp_node, ast.Constant) and cmp_node.value == 'TERMINATE':
                offenders.append(cmp_node.lineno)
    assert not offenders, (
        "exact == 'TERMINATE' comparison at line(s) "
        f"{sorted(offenders)} — use the canonical _is_terminate(), which "
        'strips the injected memory skeleton (see the comment above its '
        'definition). An exact match is defeated by the skeleton suffix and '
        'by any TERMINATE the ender at :2255 accepted but this site rejects.')


def test_canonical_predicate_is_actually_used():
    """Guard the guard: the helper must have real callers, not just exist."""
    n = len(re.findall(r'(?<!def )_is_terminate\(', _src()))
    assert n >= 7, (
        f'expected the create, timer and reuse paths to share the predicate; '
        f'found only {n} call sites')
