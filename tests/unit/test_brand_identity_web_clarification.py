"""Owner decision 2026-09-01: 'local' must never read as 'offline'.

Battery on the fresh install proved the edge-first persona suppressed
web-fetch tool use — the model answered "I am designed to operate with
everything staying on your device" with crawl tools attached.  The
clarification (data-local != offline) lives at the canonical identity
and the reuse never-refuse block; this guard fails if either drifts
back to an unqualified local-only stance.

    python -m pytest tests/unit/test_brand_identity_web_clarification.py --noconftest -q
"""
from pathlib import Path
import unittest

from core.constants import NUNBA_BRAND_IDENTITY

_ROOT = Path(__file__).resolve().parents[2]


class BrandIdentityWebClarification(unittest.TestCase):

    def test_identity_distinguishes_data_local_from_offline(self):
        low = NUNBA_BRAND_IDENTITY.lower()
        self.assertIn('public web', low)
        self.assertIn('does not mean offline', low.replace('—', '-'))

    def test_identity_forbids_fabricated_fetches(self):
        """Probe 2026-09-01: with the unlock alone, the TOOL-LESS path
        answered 'I've fetched the live content...' with ZERO calls —
        a fabricated fetch. The identity must pair the permission with
        the never-claim-unperformed-fetch clause."""
        low = NUNBA_BRAND_IDENTITY.lower()
        self.assertIn('never claim a fetch', low)

    def test_reuse_never_refuse_block_carries_the_clarification(self):
        src = (_ROOT / 'hartos' / 'reuse_recipe.py').read_text(
            encoding='utf-8', errors='replace')
        self.assertIn('does NOT', src)
        self.assertIn('forbid fetching PUBLIC web pages', src)


if __name__ == '__main__':
    unittest.main()
