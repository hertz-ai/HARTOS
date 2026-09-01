"""Owner decisions 2026-09-01: 'local' must never read as 'offline',
and the web-fetch policy wording has exactly ONE home ("mirror is
wrong, canonicalisation is correct").

History: the edge-first persona suppressed web-fetch tool use (model
answered "everything stays on your device" with crawl tools attached);
the first fix mirrored clarification text into three homes; the mirror
was then rejected — core.constants.NUNBA_WEB_FETCH_POLICY is now the
single source, NUNBA_BRAND_IDENTITY composes it, and consumer prompts
EMBED the constant instead of restating it.

    python -m pytest tests/unit/test_brand_identity_web_clarification.py --noconftest -q
"""
from pathlib import Path
import unittest

from core.constants import NUNBA_BRAND_IDENTITY, NUNBA_WEB_FETCH_POLICY

_ROOT = Path(__file__).resolve().parents[2]


class BrandIdentityWebClarification(unittest.TestCase):

    def test_policy_distinguishes_data_local_from_offline(self):
        low = NUNBA_WEB_FETCH_POLICY.lower()
        self.assertIn('public web', low)
        self.assertIn('does not mean offline', low.replace('—', '-'))

    def test_policy_forbids_fabricated_fetches(self):
        """Probe 2026-09-01: with the unlock alone, the TOOL-LESS path
        answered 'I've fetched the live content...' with ZERO calls —
        a fabricated fetch. The policy pairs the permission with the
        never-claim-unperformed-fetch clause."""
        self.assertIn('never claim a fetch', NUNBA_WEB_FETCH_POLICY.lower())

    def test_identity_composes_the_policy(self):
        self.assertIn(NUNBA_WEB_FETCH_POLICY, NUNBA_BRAND_IDENTITY)

    def test_reuse_template_embeds_policy_not_a_mirror(self):
        src = (_ROOT / 'hartos' / 'reuse_recipe.py').read_text(
            encoding='utf-8', errors='replace')
        self.assertIn('{NUNBA_WEB_FETCH_POLICY}', src)
        # the wording itself must not be restated in the template
        self.assertNotIn('never claim a fetch', src)
        self.assertNotIn('forbid fetching PUBLIC web pages', src)

    def test_policy_wording_has_one_home(self):
        """No HARTOS source restates the policy sentence — everyone
        embeds the constant."""
        offenders = []
        for pkg in ('core', 'hartos', 'integrations', 'security'):
            for p in (_ROOT / pkg).rglob('*.py'):
                if p.name == 'constants.py' and p.parent.name == 'core':
                    continue
                try:
                    text = p.read_text(encoding='utf-8', errors='replace')
                except OSError:
                    continue
                if 'never claim a fetch' in text:
                    offenders.append(str(p.relative_to(_ROOT)))
        self.assertFalse(
            offenders, f"policy wording restated outside core/constants.py: {offenders}")


if __name__ == '__main__':
    unittest.main()
