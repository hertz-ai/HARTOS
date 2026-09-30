"""An action must match its OWN banked recipe, so REUSE injects the proven steps.

THE LIVE FAILURE (drive d69-, 2026-09-11 04:11:12, installed build, agent
88719487304 action 2, session 6c2dc0fc-7c93-4fe0-973e-f7466ff63f29_88719487304).
``similar_instructions`` compared the live instruction against every banked
action and scored, from the log verbatim:

    similarity: 0.75                 <- action 2, ITS OWN recipe
    similarity: 0.2608695652173913   <- execute_coding_task
    similarity: 0.26666666666666666  <- save_to_long_term_memory
    similarity: 0.13333333333333333  <- google_search, and the rest

The threshold is 0.8.  The action's own recipe missed it by 0.05, so
``matching_recipe`` stayed None, ``REUSING command`` logged ZERO times, and no
``Follow these steps from a previous successful execution`` block was ever
built.  The VLM loop then started from nothing, burned 30 iterations in 114.6s,
invented https://www.hartos.com/documentation, exited max_iterations, and the
tool returned a core.constants.TOOL_FAILURE_RESULTS sentinel.  FAB-GUARD was
right to refuse; there was no work.  13 refusals over 17 minutes.

WHY 0.75, computed not guessed:

    live   'Open a web browser and navigate to the top result URL for HART OS documentation'
    stored "execute_windows_or_android_command: 'Open default web browser and
            navigate to the top result URL for HART OS documentation'"

    words1=15  words2=16  overlap=12  ->  12/16 = 0.75   (matches the log exactly)

The denominator is inflated and the overlap diluted by the ``<tool>: '<arg>'``
AUTHORING PREFIX.  Strip it and the same pair scores 12+2 / 15 = 0.9333 — the
two texts then differ by exactly ONE word, 'a' vs 'default'.

THIS MODULE ALREADY KNOWS THAT CONVENTION.  ``_tool_name_candidates``
(reuse_recipe.py) exists precisely because "the authoring model routinely
writes the tool AND its argument into the single field", and splits on ':' to
recover the NAME half.  Nothing recovered the ARGUMENT half, so the matcher
compared raw.  The fix is the complement of a helper that already ships, not a
new idea — and it lives in hartos/helper.py because BOTH create_recipe.py and
reuse_recipe.py carry a byte-identical ``similar_instructions`` and must not
drift.  Same precedent as ``answered_call_ids``, which reuse_recipe's own
comment records being moved to helper.py to stop exactly that duplication.

WHAT THIS GUARD DOES NOT CLAIM: that the agent then reaches its goal, or that
the injected steps are good ones.  It claims an action can match its own banked
recipe, and that unrelated actions still do not.
"""

import io
import os
import re
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The real strings from the 04:11:12 comparison, copied out of server.log.
_LIVE = "Open a web browser and navigate to the top result URL for HART OS documentation"
_OWN_RECIPE = ("execute_windows_or_android_command: 'Open default web browser and "
               "navigate to the top result URL for HART OS documentation'")

# Other banked actions from the same agent, with the score each really got.
# These must STAY below threshold — a normaliser that makes everything match
# is worse than the bug, because it would inject the wrong action's steps.
_OTHERS = {
    "google_search: 'Search the web for HART OS documentation and specifications'": 0.1333,
    "execute_coding_task: 'Write Python script to parse HART OS documentation text'": 0.2609,
    "save_to_long_term_memory: 'Persist the extracted HART OS metrics'": 0.2667,
    "save_data_in_memory: 'Store the parsed JSON payload'": 0.1333,
}

_THRESHOLD = 0.8


def _overlap_ratio(a, b):
    """The metric similar_instructions uses, verbatim (reuse_recipe.py:1811)."""
    w1, w2 = set(a.lower().split()), set(b.lower().split())
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / max(len(w1), len(w2))


def _norm():
    """The canonical normaliser, imported from its real home.

    Imported rather than reimplemented: a local copy would score whatever this
    file says and prove nothing about what the pipeline does.
    """
    from hartos.helper import strip_authored_tool_prefix
    return strip_authored_tool_prefix


class TestTheArithmeticIsWhatTheLogSaid(unittest.TestCase):
    """Pin the measurement before asserting on any fix."""

    def test_raw_comparison_reproduces_the_live_0_75(self):
        got = _overlap_ratio(_LIVE, _OWN_RECIPE)
        self.assertAlmostEqual(
            got, 0.75, places=4,
            msg='the reproduction drifted from the live log (0.75); the strings '
                'or the metric changed, so every number below is about '
                'something else — re-measure before trusting this file')

    def test_raw_comparison_fails_the_threshold(self):
        self.assertLess(
            _overlap_ratio(_LIVE, _OWN_RECIPE), _THRESHOLD,
            'the unnormalised pair now passes; if the metric or threshold '
            'changed, this whole guard needs re-pointing')


class TestActionMatchesItsOwnRecipe(unittest.TestCase):
    """RED until the authoring prefix is stripped before comparing."""

    def test_the_action_matches_its_own_banked_recipe(self):
        n = _norm()
        score = _overlap_ratio(n(_LIVE), n(_OWN_RECIPE))
        self.assertGreaterEqual(
            score, _THRESHOLD,
            'action 2 still cannot match its OWN recipe (%.4f < %.2f), so '
            'REUSE injects no steps and the VLM starts from nothing — the '
            'exact state measured live on 2026-09-11, 30 iterations and an '
            'invented URL' % (score, _THRESHOLD))

    def test_unrelated_actions_still_do_not_match(self):
        """A normaliser that matches everything is worse than the bug."""
        n = _norm()
        for other, live_score in _OTHERS.items():
            score = _overlap_ratio(n(_LIVE), n(other))
            self.assertLess(
                score, _THRESHOLD,
                "'%s' now matches the live instruction at %.4f (was %.4f live). "
                "Normalising must not turn a different action into a match — "
                "that would inject the WRONG action's steps, which is a worse "
                "failure than injecting none." % (other[:50], score, live_score))

    def test_it_keeps_the_argument_not_the_tool_name(self):
        n = _norm()
        out = n(_OWN_RECIPE)
        self.assertNotIn('execute_windows_or_android_command', out,
                         'the tool-name prefix survived the strip')
        self.assertIn('navigate to the top result URL', out,
                      'the strip ate the argument it was supposed to keep')

    def test_plain_prose_is_returned_unchanged(self):
        """Most action texts carry no prefix; they must pass through intact."""
        n = _norm()
        for plain in (_LIVE,
                      'Summarise the findings for the user',
                      'Ratio 3:2 matters here',          # a colon that is NOT a prefix
                      ''):
            self.assertEqual(
                n(plain), plain.strip().strip('\'"'),
                'a text with no <tool>: prefix was altered: %r -> %r'
                % (plain, n(plain)))

    def test_it_never_raises(self):
        """It runs on the dispatch path; it must not be able to kill a turn."""
        n = _norm()
        for junk in (None, 123, [], {}, b'bytes'):
            n(junk)  # must not raise


class TestBothComparatorsUseIt(unittest.TestCase):
    """One contract, two call sites — reuse AND create.

    similar_instructions is duplicated byte-for-byte in reuse_recipe.py:1810
    and create_recipe.py:1285 (only the logger differs). The live measurement
    is from reuse; create carries the identical arithmetic and would fail the
    identical way. Both must normalise through the one helper, or the next
    reader fixes it twice — or once, and it drifts.
    """

    def _src(self, name):
        return io.open(os.path.join(_HARTOS, 'hartos', name),
                       encoding='utf-8', errors='replace').read()

    def _comparator(self, src):
        m = re.search(r'def similar_instructions\(.*?\n((?:.*\n)*?)\s*'
                      r'return similarity >= threshold', src)
        return m.group(0) if m else ''

    def test_reuse_comparator_normalises(self):
        body = self._comparator(self._src('reuse_recipe.py'))
        self.assertTrue(body, 'similar_instructions moved in reuse_recipe.py')
        self.assertIn(
            'strip_authored_tool_prefix', body,
            'the reuse comparator still compares RAW text, so an action whose '
            'stored title carries the <tool>: prefix cannot match its own '
            'recipe (0.75 vs 0.80 threshold, measured live 2026-09-11)')

    def test_create_comparator_normalises(self):
        body = self._comparator(self._src('create_recipe.py'))
        self.assertTrue(body, 'similar_instructions moved in create_recipe.py')
        self.assertIn(
            'strip_authored_tool_prefix', body,
            'create_recipe.py carries the same byte-identical comparator and '
            'the same defect; fixing only reuse leaves the pair drifted')

    def test_neither_reimplements_the_strip(self):
        """DRY: the split logic must live in one place."""
        for name in ('reuse_recipe.py', 'create_recipe.py'):
            body = self._comparator(self._src(name))
            self.assertNotIn(
                're.split', body,
                '%s reimplements the prefix split inside the comparator '
                'instead of calling the shared helper' % name)


if __name__ == '__main__':
    unittest.main()
