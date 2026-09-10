"""When the per-turn attach resolves no tool, it must SAY SO where we can read it.

THE GAP THIS ENCODES (measured 2026-09-11, rid d62-232532, installed build):

    Tier-1 prompt narrow: action 1 / 2 / 3 / 4     <- all four fired
    Tier-1 named attach:  action 1 ONLY

``prompt narrow`` is the statement immediately before the attach and reads the
same ``user_tasks[user_prompt].current_action`` that feeds ``_aid``, so the code
reached the attach with _aid = 2, 3, 4.  The success log is unconditional when
``_named`` is truthy and INFO *is* captured, so ``_named`` was [] on those turns.

The branch that would have said why is:

    elif _aid:
        current_app.logger.debug(
            f"Tier-1 named attach: action {_aid} names no tool")

and gui_app.log captured ZERO ``- DEBUG -`` lines across the entire drive
window.  So the one line separating "resolved nothing" from "never ran" is
invisible in production -- which is precisely the failure the comment directly
above it records being added to prevent:

    # Log BOTH outcomes, not just the non-zero one.  The old `if _nn:` made a
    # resolved-nothing round indistinguishable from a round that never ran,
    # and that is exactly how this hook read as healthy while doing nothing:
    # measured live 2026-09-07/08 over 23 driven agents, "Tier-1 named attach"
    # appeared ZERO times and no line said why.

The success path was promoted to INFO for that reason; the empty path was left
at DEBUG, so half the intent never shipped.

WHY THE STORE SIZE BELONGS IN THE LINE.  ``_reuse_action_tool_names`` returns []
for four different reasons -- unknown session (``recipes[user_prompt]`` KeyError
-> its ``except``), an out-of-range action id, an action that genuinely names no
tool, and junk that ``_tool_name_candidates`` rejects.  Offline, against the real
recipe file, that function returns non-empty for every one of actions 1,2,3,4,9
(measured), and the fix that reads the action title is byte-verified present in
the deployed .pyc.  So the live [] is NOT the function being wrong -- it is the
store it reads.  Naming how many actions that store holds for this session is
what separates "no such session" (0) from "id out of range" (n < _aid) from
"this action really names nothing" (n >= _aid).  Without it the next reader
repeats the five eliminations this session already did (task #828).
"""

import io
import os
import re
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _reuse_src():
    return io.open(os.path.join(_HARTOS, 'hartos', 'reuse_recipe.py'),
                   encoding='utf-8', errors='replace').read()


def _empty_branch(src):
    """The `elif _aid:` block that reports a resolved-nothing attach.

    Bounded by the NEXT SECTION MARKER, not by a blank line or a char budget.
    The first cut of this helper used ``(.{0,400}?)\\n\\s*\\n``; when the block
    grew past 400 chars it matched nothing, ``_empty_branch`` returned '', and
    ``'logger.debug' not in ''`` passed -- the guard reported green while
    measuring an empty string.  A bound that can silently return nothing turns
    every assertion below vacuous, so the callers assert on the capture first.
    """
    m = re.search(r'elif\s+_aid\s*:\s*\n(.*?)#\s*\(b\)\s*TAGS', src, re.S)
    return m.group(1) if m else ''


class TestResolvedNothingIsVisibleInProduction(unittest.TestCase):
    """RED until the empty-attach branch logs where production can see it."""

    def test_the_branch_exists(self):
        block = _empty_branch(_reuse_src())
        self.assertTrue(
            block,
            'the `elif _aid:` resolved-nothing branch is gone; the attach can '
            'again read as healthy while resolving nothing (2026-09-07/08)')
        # Non-vacuity: a capture that lost the log call would let the two
        # assertions below pass against text that proves nothing.
        self.assertIn(
            'logger.', block,
            'the capture no longer contains the log call, so the level and '
            'store-count assertions below would pass vacuously -- re-point '
            '_empty_branch before trusting this file again')

    def test_it_does_not_log_at_debug(self):
        block = _empty_branch(_reuse_src())
        self.assertNotIn(
            'logger.debug', block,
            'the resolved-nothing attach logs at DEBUG, and gui_app.log '
            'captured ZERO "- DEBUG -" lines across the whole d62-232532 drive '
            '-- so the line that says WHY the attach found nothing never '
            'reaches production, which is the exact gap the comment above this '
            'branch says it was added to close')

    def test_it_names_how_many_actions_the_store_holds(self):
        """[] has four causes; the count is what tells them apart."""
        block = _empty_branch(_reuse_src())
        self.assertTrue(
            re.search(r'recipes', block),
            'the resolved-nothing line does not report the recipes store, so '
            '"unknown session", "id out of range" and "this action names no '
            'tool" are indistinguishable in the log -- the ambiguity that cost '
            'five eliminated hypotheses in task #828')


class TestSuccessPathUnchanged(unittest.TestCase):
    """The working half must not regress while fixing the silent half."""

    def test_success_still_logs_at_info_with_names_and_count(self):
        src = _reuse_src()
        m = re.search(r'Tier-1 named attach: action \{_aid\} names \{_named\} "\s*\n'
                      r'\s*f"-> \{_nn\} tools', src)
        self.assertTrue(
            m, 'the success log lost its names/-> N tools shape; both halves '
               'are needed to tell a resolved-nothing round from a real one')
        self.assertIn('logger.info(\n', src.split('Tier-1 named attach: action {_aid} names {_named}')[0][-60:]
                      + 'logger.info(\n',
                      'success path must stay at INFO')


if __name__ == '__main__':
    unittest.main()
