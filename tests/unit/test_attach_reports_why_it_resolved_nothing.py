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
    """The block that reports a resolved-nothing attach.

    RE-POINTED 2026-09-11.  It used to be an ``elif _aid:`` inside
    ``get_agent_response``, bounded by the ``# (b) TAGS`` marker.  The whole
    attach was then lifted into ``_attach_named_tools_for_action`` so it could
    also run from ``_advance_or_steer`` -- the one door every action advance
    goes through -- because inline it ran once per CALL while the walk
    advances many actions inside that call (2 attach lines across two live
    drives, both action 1, 6-9 actions walked each time).  The branch is now
    the fall-through after the ``if _named:`` half returns, so the bound is
    ``return _nn`` .. ``except Exception``.  Same code, same contract, one
    caller became two; this guard follows it rather than pinning it in place.

    Bounded by real anchors, not by a blank line or a char budget.  The first
    cut of this helper used ``(.{0,400}?)\\n\\s*\\n``; when the block grew past
    400 chars it matched nothing, ``_empty_branch`` returned '', and
    ``'logger.debug' not in ''`` passed -- the guard reported green while
    measuring an empty string.  A bound that can silently return nothing turns
    every assertion below vacuous, so the callers assert on the capture first.
    """
    m = re.search(r'\breturn _nn\b(.*?)except Exception', src, re.S)
    return m.group(1) if m else ''


def _levels(block):
    """Every logging level the block emits at, both spellings.

    The module logs two ways: ``current_app.logger.<level>(...)`` and
    ``_ctx_safe_log('<level>', ...)`` -- the latter for code that can run off
    a request thread, where the former raises "Working outside of application
    context".  This branch moved onto that path when it was lifted into
    _attach_named_tools_for_action (it is now also called from
    _advance_or_steer, deep inside the autogen walk), so it uses
    _ctx_safe_log like the prompt-narrow beside it.

    Reading only ``logger.<level>`` would have made the DEBUG assertion pass
    against ``_ctx_safe_log('debug', ...)`` -- the precise vacuity this file's
    own header warns about.
    """
    return (re.findall(r'current_app\.logger\.(\w+)\(', block)
            + re.findall(r"_ctx_safe_log\(\s*['\"](\w+)['\"]", block))


class TestResolvedNothingIsVisibleInProduction(unittest.TestCase):
    """RED until the empty-attach branch logs where production can see it."""

    def test_the_branch_exists(self):
        block = _empty_branch(_reuse_src())
        self.assertTrue(
            block,
            'the resolved-nothing branch is gone; the attach can again read '
            'as healthy while resolving nothing (2026-09-07/08). It lives '
            'after the `if _named:` half of _attach_named_tools_for_action')
        # Non-vacuity: a capture that lost the log call would let the two
        # assertions below pass against text that proves nothing.
        self.assertTrue(
            _levels(block),
            'the capture no longer contains the log call, so the level and '
            'store-count assertions below would pass vacuously -- re-point '
            '_empty_branch before trusting this file again')

    def test_it_does_not_log_at_debug(self):
        block = _empty_branch(_reuse_src())
        levels = _levels(block)
        self.assertTrue(levels, 'no log call in the capture; see above')
        self.assertNotIn(
            'debug', levels,
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

    def test_it_names_the_session_it_measured(self):
        """A count with no session key is not attributable on a live box.

        MEASURED 2026-09-11, the first drive that carried this line at INFO:
        five occurrences, every one reading "holds 1 action(s)", while the
        agent under test (88719487304) has NINE actions in BOTH of its flow
        recipes on disk (88719487304_0_recipe.json and _1_recipe.json, 29,907 B
        each, verified by loading them).  Neither writer of the store can
        collapse 9 to 1: `_normalize_flow_recipe` returns a dict whose
        'actions' is already a list unchanged, and `_vlm_merged_actions` only
        replaces in place or appends, so len(out) >= len(existing).

        The surrounding log explains it: a marketing reuse agent for a
        DIFFERENT user (cf125371) was being built in the same window, and 444
        of 880 stored agents are single-action stubs (#758) for which "holds 1"
        is simply correct.  So the five lines were probably never about this
        agent at all -- and with no key in the line there is no way to tell.

        A diagnostic that cannot be attributed reproduces the ambiguity it was
        added to remove, which is why the session key is part of the contract
        and not a nicety.
        """
        block = _empty_branch(_reuse_src())
        # FORMAT NOTE 2026-09-11: the suffix is `for session: {user_prompt}`,
        # matching the convention FAB-GUARD already uses in this same module
        # ('...; unrun=[...] for session: <key>').  The first cut invented a
        # second phrasing '(session <key>)' -- two formats for one concept in
        # one file is the drift this project's rules exist to prevent.
        # Assert on the LOG MESSAGE, not the block.  A bare
        # ``assertIn('user_prompt', block)`` passes vacuously: the block
        # already reads ``recipes.get(user_prompt)`` to compute the count, so
        # the name is present whether or not it is ever LOGGED.  Proven by
        # A/B on 2026-09-11 -- that assertion passed against the reverted,
        # session-less line too.  Match the interpolation inside the f-string.
        self.assertTrue(
            re.search(r'for session: \{user_prompt\}', block),
            'the resolved-nothing line reports a count without naming the '
            'session it measured; on a box with daemon agents and a second '
            'user driving reuse concurrently, that number cannot be attributed '
            'to the agent under test -- exactly how five "holds 1" lines were '
            'nearly read as evidence about a 9-action agent (#828)')


class TestSuccessPathUnchanged(unittest.TestCase):
    """The working half must not regress while fixing the silent half."""

    def test_success_still_logs_at_info_with_names_and_count(self):
        src = _reuse_src()
        m = re.search(r'Tier-1 named attach: action \{_aid\} names \{_named\} "\s*\n'
                      r'\s*f"-> \{_nn\} tools', src)
        self.assertTrue(
            m, 'the success log lost its names/-> N tools shape; both halves '
               'are needed to tell a resolved-nothing round from a real one')

        # FIXED 2026-09-11.  This assertion used to read:
        #     assertIn('logger.info(\n', src.split(...)[0][-60:] + 'logger.info(\n')
        # -- it appended its own needle to the haystack, so it passed against
        # every possible source, including a reverted one.  A guard that
        # cannot fail is not a guard (feedback_vacuous_guards); proven by
        # A/B before replacing it.  Read the level actually used, both
        # spellings, from the text preceding the message.
        levels = _levels(src[:m.start()][-400:])
        self.assertTrue(
            levels,
            'no log call precedes the success message; re-point this guard')
        self.assertEqual(
            levels[-1], 'info',
            'the success half of the attach left INFO. Both halves are one '
            'diagnostic pair -- if either goes to DEBUG it stops reaching '
            'production (0 of 45,599 lines captured at DEBUG on this build) '
            'and a resolved-nothing round becomes indistinguishable from a '
            'round that never ran')


if __name__ == '__main__':
    unittest.main()
