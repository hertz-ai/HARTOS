"""A TERMINATE check must not assume the group chat still has messages.

LIVE-REPRODUCED 2026-08-30 12:48 by driving reuse_recipe.chat_agent out of
process against a real llama-server (agent 11470436451, user reuselive1):

    Message count changed: 4 -> 1
    Message count changed: 6 -> 1
    Message count changed: 8 -> 1
    Next speaker: User
    ERROR in reuse_recipe: Error in get_agent_response:
      File reuse_recipe.py, line 3216, in get_agent_response
        if group_chat.messages[-1]['name'] == 'ChatInstructor' and ...
    IndexError: list index out of range
    -> user-visible reply: "Error getting response: list index out of range"

WHY IT EMPTIES.  autogen's transform_messages capability trims history between
turns (that is the "Message count changed" line).  It is allowed to trim to
nothing, and the loops here re-test the condition on every iteration, so the
list can be empty at the top of any pass.

WHY THE EXISTING except DID NOT CATCH IT.  reuse_recipe.py:3216 puts the
subscript in the `if` CONDITION, and the `try:` -- whose `except IndexError:`
logs "Completed ALL ACTIONS" -- only opens on the NEXT line.  So the guard
covers the messages[-2] reads inside the body and cannot see the messages[-1]
read that selects the body.  An except that is one line too late is not a
guard.

THE CANONICAL FIX ALREADY EXISTS IN THIS REPO: create_recipe.py:4385 spells the
same test as

    if group_chat.messages and group_chat.messages[-1]['name'] == 'ChatInstructor'

and :4596 / :4775 use the identical `group_chat.messages and ...` idiom.  This
test asserts every sibling site adopts it, rather than introducing a new
helper for a one-token condition.

    python -m pytest tests/unit/test_groupchat_last_message_guarded.py --noconftest -q
"""
import ast
import io
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
FILES = ('reuse_recipe.py', 'create_recipe.py')

# The TERMINATE dispatch condition: reads the last message to decide whether a
# whole action-advance body runs.  This is the shape that went bang.
_LAST_MSG_READ = re.compile(r"\.messages\[-1\]\[['\"]name['\"]\]")
_TRUTHY_GUARD = re.compile(r"\.messages\s+and\b")


def _unguarded_sites(path):
    """(lineno, source) for every `if` reading .messages[-1]['name'] unguarded."""
    with io.open(path, encoding='utf-8') as fh:
        src = fh.read()
    out = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.If):
            continue
        cond = ast.get_source_segment(src, node.test) or ''
        if _LAST_MSG_READ.search(cond) and not _TRUTHY_GUARD.search(cond):
            out.append((node.lineno, ' '.join(cond.split())[:100]))
    return out


def test_terminate_checks_guard_against_an_empty_group_chat():
    bad = []
    for name in FILES:
        for lineno, cond in _unguarded_sites(os.path.join(ROOT, name)):
            bad.append('  %s:%d  %s' % (name, lineno, cond))
    assert not bad, (
        'these TERMINATE checks index .messages[-1] without first testing that '
        '.messages is non-empty. autogen\'s transform_messages capability trims '
        'history between turns and may trim to zero, so each of these raises '
        'IndexError and the user gets "Error getting response: list index out '
        'of range" instead of an answer (live-reproduced 2026-08-30 at '
        'reuse_recipe.py:3216). Use the idiom already in create_recipe.py:4385 '
        '-- `if group_chat.messages and group_chat.messages[-1][...]`:\n'
        + '\n'.join(bad))


def test_the_canonical_guarded_site_still_exists():
    """Anti-vacuous: the test above passes trivially if the pattern is renamed.

    Pin the exemplar this fix copies, so that if create_recipe.py:4385 is
    rewritten the next reader still learns where the idiom came from -- and so
    a regex that silently stops matching anything is caught.
    """
    with io.open(os.path.join(ROOT, 'create_recipe.py'), encoding='utf-8') as fh:
        src = fh.read()
    assert _LAST_MSG_READ.search(src), (
        'no .messages[-1][\'name\'] read found in create_recipe.py at all -- '
        'the detection regex no longer matches the code it guards, so '
        'test_terminate_checks_guard_against_an_empty_group_chat is vacuous')
    guarded = [ln for ln in src.splitlines()
               if _LAST_MSG_READ.search(ln) and _TRUTHY_GUARD.search(ln)]
    assert guarded, 'the guarded exemplar (create_recipe.py:4385) is gone'
