"""
The grounding gate, tested from the attacker's side.

The question is not "does a true fact pass". It is "can a fabricated one get
through", because that is the failure that reaches an audience under the
company's name. This codebase has already published 416 fabricated
"PROOF: 0.0%" posts, so the bar is that an invented claim must be
*unrepresentable* at the publish boundary, not merely discouraged.

Each test below is a way an agent would plausibly smuggle an unsourced claim
into a post.
"""
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, __file__.rsplit('tests', 1)[0])

from integrations.agent_engine.grounded_facts import (  # noqa: E402
    GroundedFact, GroundingError, assert_publishable, verify_all, verify_claim,
)

_OCTOPUS_PAGE = """
Cephalopod neurology. The common octopus has three hearts and nine brains:
one central brain and eight smaller ganglia, one per arm. Two of the hearts
pump blood to the gills. Its blood is blue because it carries oxygen using
haemocyanin rather than haemoglobin.
"""


def _with_source(text):
    """Patch the fetch so tests never touch the network."""
    return patch('integrations.agent_engine.grounded_facts._fetch',
                 return_value=text)


class TestFabricationIsRejected(unittest.TestCase):

    def test_invented_number_cannot_pass_even_with_a_real_source(self):
        """The classic shape: real page, real topic, invented statistic.
        Term overlap would happily pass this, so numbers are matched exactly."""
        with _with_source(_OCTOPUS_PAGE):
            fact, reason = verify_claim(
                'The octopus has 14 brains and three hearts.',
                'https://example.org/octopus')
        self.assertIsNone(fact)
        self.assertIn('14', reason)

    def test_claim_about_a_different_subject_is_rejected(self):
        with _with_source(_OCTOPUS_PAGE):
            fact, reason = verify_claim(
                'Migrating albatrosses sleep while gliding across the ocean.',
                'https://example.org/octopus')
        self.assertIsNone(fact)
        self.assertIn('terms', reason)

    def test_unreachable_source_fails_closed(self):
        """A source that will not load is not a source. Publishing anyway is
        exactly the fabricated-proof failure."""
        with _with_source(None):
            fact, reason = verify_claim(
                'The octopus has nine brains.', 'https://example.org/gone')
        self.assertIsNone(fact)
        self.assertIn('could not be fetched', reason)

    def test_a_claim_with_no_source_never_reaches_a_fetch(self):
        for url in ('', 'not-a-url', 'ftp://x/y', 'source: my training data'):
            fact, reason = verify_claim('The octopus has nine brains.', url)
            self.assertIsNone(fact)
            self.assertIn('source url', reason)

    def test_forty_does_not_satisfy_four_hundred(self):
        with _with_source('The survey covered 40 species.'):
            fact, _ = verify_claim('The survey covered 400 species.',
                                   'https://example.org/s')
        self.assertIsNone(fact)

    def test_a_spelled_out_invented_number_is_caught(self):
        """The hole this nearly shipped with. A digit-only check waves
        "fourteen brains" straight through, and spelled-out quantities are
        most of them in social copy."""
        with _with_source(_OCTOPUS_PAGE):
            fact, reason = verify_claim(
                'The octopus has fourteen brains.',
                'https://example.org/octopus')
        self.assertIsNone(fact)
        self.assertIn('14', reason)

    def test_digits_in_a_claim_match_words_in_the_source(self):
        """The same normalisation has to work in the honest direction, or the
        gate rejects true claims for cosmetic reasons and gets switched off."""
        with _with_source(_OCTOPUS_PAGE):
            fact, reason = verify_claim(
                'The octopus has 9 brains.', 'https://example.org/octopus')
        self.assertIsNotNone(fact, reason)


class TestGroundedClaimPasses(unittest.TestCase):

    def test_a_sourced_claim_verifies_and_keeps_its_evidence(self):
        with _with_source(_OCTOPUS_PAGE):
            fact, reason = verify_claim(
                'The octopus has nine brains and three hearts.',
                'https://example.org/octopus')
        self.assertIsNotNone(fact, reason)
        self.assertEqual(fact.source_url, 'https://example.org/octopus')
        # The evidence window is stored so a human can see WHY it passed
        # without re-fetching.
        self.assertIn('brain', fact.evidence.lower())
        self.assertIn('9', fact.matched_numbers)

    def test_thousands_separators_do_not_break_a_match(self):
        with _with_source('The reef lost 3,200 hectares last year.'):
            fact, reason = verify_claim(
                'The reef lost 3200 hectares last year.',
                'https://example.org/reef')
        self.assertIsNotNone(fact, reason)

    def test_paraphrase_is_allowed_within_the_overlap_threshold(self):
        with _with_source(_OCTOPUS_PAGE):
            fact, reason = verify_claim(
                'Octopus blood is blue, using haemocyanin.',
                'https://example.org/octopus')
        self.assertIsNotNone(fact, reason)


class TestBatchReportsWhatItDropped(unittest.TestCase):

    def test_rejections_are_returned_not_swallowed(self):
        """A run that grounds 1 of 3 must say so. Silently publishing the 1
        hides that research is mostly inventing things."""
        with _with_source(_OCTOPUS_PAGE):
            grounded, rejected = verify_all([
                {'claim': 'The octopus has nine brains.',
                 'source_url': 'https://example.org/octopus'},
                {'claim': 'The octopus has 14 brains.',
                 'source_url': 'https://example.org/octopus'},
                {'claim': 'Penguins migrate 90000 km yearly.',
                 'source_url': ''},
            ])
        self.assertEqual(len(grounded), 1)
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all(r['reason'] for r in rejected))


class TestPublishBoundary(unittest.TestCase):
    """assert_publishable is the last thing between a claim and an audience."""

    def _real_fact(self):
        with _with_source(_OCTOPUS_PAGE):
            fact, _ = verify_claim('The octopus has nine brains.',
                                   'https://example.org/octopus')
        return fact

    def test_a_hand_built_lookalike_is_refused(self):
        """The obvious smuggling route: build something shaped like a fact.
        Only verify_claim can mint one, so a dict is not enough."""
        impostor = {'claim': 'The octopus has 14 brains.',
                    'source_url': 'https://example.org/octopus',
                    'evidence': 'trust me'}
        with self.assertRaises(GroundingError):
            assert_publishable([impostor])

    def test_one_bad_item_fails_the_whole_batch(self):
        with self.assertRaises(GroundingError):
            assert_publishable([self._real_fact(), 'a raw string claim'])

    def test_empty_is_an_error_not_a_quiet_success(self):
        """Nothing survived grounding is a real outcome and must be loud.
        Returning [] invites a caller to fall back to unsourced content."""
        with self.assertRaises(GroundingError):
            assert_publishable([])

    def test_verified_facts_pass_through(self):
        fact = self._real_fact()
        self.assertEqual(assert_publishable([fact]), [fact])

    def test_the_type_cannot_be_mutated_after_verification(self):
        """Frozen, so nothing can verify a safe claim then swap the text."""
        fact = self._real_fact()
        with self.assertRaises(Exception):
            fact.claim = 'The octopus has 14 brains.'


if __name__ == '__main__':
    unittest.main()
