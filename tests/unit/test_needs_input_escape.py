"""#688 P1 — the NEEDS-INPUT escape must ask a question, not leak internals.

Live 2026-08-23 21:52-21:56 on the installed build (val-p1-persona,
prompt 20260823101): the recipe build stalled on action 2, took its
designed [NEEDS-INPUT] escape, and the user received the raw group-chat
tail — 'TERMINATE\\n Metadata/skeleton of all keys for retrieving data
from memory:{}' repeated six times — as the reply.  Four follow-up
approval turns each exited at WHILE LOOP ITERATION #1 with the attempt
counter climbing 5->9: `_exec_retries` never reset, so the user's
answers were never fed back to the build.  A HITL escape whose question
is garbage and whose retry gate never re-opens is a permanent wedge.

Three defects, three pins:
  1. the escape returns a user-facing question naming the stuck action
     (via _needs_input_reply) and RESETS the attempt counter;
  2. the TERMINATE dodge tolerates the machine-appended memory-skeleton
     suffix (create_recipe.py:2406/2659/2711 append it to the LAST
     message, so `content == 'TERMINATE'` can never match) — _is_terminate;
  3. text bound for the user is stripped of skeleton lines —
     _strip_memory_skeleton.

Extract-and-exec (importing create_recipe hangs a bare pytest env).

    python -m pytest tests/unit/test_needs_input_escape.py --noconftest -q
"""
import ast
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_SRC_PATH = os.path.join(ROOT, 'hartos/create_recipe.py')
_SRC = open(_SRC_PATH, encoding='utf-8').read()

# Live repro payload: terminate token + two machine-appended skeleton lines.
_LIVE_GARBAGE = ('TERMINATE\n'
                 ' Metadata/skeleton of all keys for retrieving data from memory:{}\n'
                 ' Metadata/skeleton of all keys for retrieving data from memory:{}')


def _exec_helpers():
    """Extract the three module-level helpers and exec them in isolation."""
    tree = ast.parse(_SRC)
    wanted = {'_strip_memory_skeleton', '_is_terminate', '_needs_input_reply'}
    nodes = [n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.Assign))
             and (getattr(n, 'name', None) in wanted
                  or any(getattr(t, 'id', None) == '_MEMORY_SKELETON_PREFIX'
                         for t in getattr(n, 'targets', [])))]
    found = {getattr(n, 'name', '_MEMORY_SKELETON_PREFIX') for n in nodes}
    assert wanted <= found, f'helpers missing from create_recipe: {wanted - found}'
    mod = ast.Module(body=nodes, type_ignores=[])
    ns = {}
    exec(compile(mod, _SRC_PATH, 'exec'), ns)
    return ns


def test_strip_memory_skeleton_removes_all_injected_lines():
    ns = _exec_helpers()
    assert ns['_strip_memory_skeleton'](_LIVE_GARBAGE) == 'TERMINATE'
    mixed = 'Here is your answer.\n Metadata/skeleton of all keys for retrieving data from memory:{"a": 1}'
    assert ns['_strip_memory_skeleton'](mixed) == 'Here is your answer.'
    assert ns['_strip_memory_skeleton']('plain reply') == 'plain reply'


def test_is_terminate_tolerates_skeleton_suffix():
    ns = _exec_helpers()
    assert ns['_is_terminate']('TERMINATE')
    assert ns['_is_terminate'](_LIVE_GARBAGE), (
        'the live terminate message carries the skeleton suffix — the exact '
        "== 'TERMINATE' comparison never matched it, which is how raw "
        'internals reached the user')
    assert not ns['_is_terminate']('The TERMINATE token is documented here')
    assert not ns['_is_terminate']('All done.')


def test_needs_input_reply_names_the_stuck_action():
    ns = _exec_helpers()
    msg = ns['_needs_input_reply'](2, 'Explain the concept using simple language.')
    assert 'input' in msg.lower()
    assert '2' in msg
    assert 'Explain the concept' in msg
    assert 'TERMINATE' not in msg and 'Metadata/skeleton' not in msg


def test_needs_input_branch_resets_counter_and_returns_question():
    """The `_attempt > 3` escape must reset _exec_retries (so the next turn
    actually re-attempts with the user's answer) and return the question
    instead of falling through to the tail-message return."""
    m = re.search(r'if _attempt > 3:\n(.*?)(?=\n\s+actions_prompt =)', _SRC, re.DOTALL)
    assert m, 'NEEDS-INPUT escape block not found'
    block = m.group(1)
    assert re.search(r"_exec_retries\[_ca_pending\]\s*=\s*0", block), (
        'escape must reset the attempt counter — live 2026-08-23 it climbed '
        "5->9 across turns, so the user's answers were never consumed")
    assert 'return _needs_input_reply(' in block, (
        'escape must return the HITL question, not break into the raw '
        'group-chat tail')


def test_terminate_dodge_uses_tolerant_matcher_at_reply_sites():
    """The three reply-extraction sites in get_response_group must use
    _is_terminate, not exact equality that the skeleton suffix defeats."""
    gr = _SRC[_SRC.index('def get_response_group('):]
    assert gr.count("_is_terminate(last_message['content'])") >= 3, (
        'reply extraction still compares == \'TERMINATE\' exactly — the '
        'memory-skeleton suffix makes that never match')


def test_final_raw_return_strips_skeleton():
    gr = _SRC[_SRC.index('def get_response_group('):]
    assert "_strip_memory_skeleton(last_message['content'])" in gr, (
        'the raw fallthrough return must strip machine-injected skeleton '
        'lines before the text reaches the user')
