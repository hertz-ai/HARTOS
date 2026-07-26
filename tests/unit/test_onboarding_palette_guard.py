"""
Source guard: the "Light Your HART" onboarding palette carries NO deprecated
indigo (#6C63FF and its light tints), so the onboarding matches the OS brand
(teal LEAD + violet ACCENT, HOME_DESKTOP_DESIGN_CHECKLIST b1.2 / GF3) instead of
the old indigo.

WHY a source guard (test_source_guard_*): a CSS *color* has no runtime behaviour
to assert - the only regression to catch is "the indigo literal came back into the
onboarding surfaces". Per CLAUDE.md Gate 5 this is an ACCEPTABLE source-shape guard
because it enforces an appearance invariant across two files (the JS companion bar
and the CSS-in-Python .hob-* rules) that a behavioural test cannot cover for the
static CSS half. The JS half additionally has a BEHAVIOURAL companion-gradient
assertion in tests/unit/test_onboarding_companion.mjs, so this is never the ONLY
guard for the JS change.

The onboarding surfaces this covers:
  * integrations/agent_engine/static/hartOnboarding.js       (whole file - onboarding)
  * integrations/agent_engine/liquid_ui_service.py .hob-*    (onboarding CSS block)
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ONBOARDING_JS = os.path.join(ROOT, "integrations", "agent_engine", "static", "hartOnboarding.js")
SHELL_PY = os.path.join(ROOT, "integrations", "agent_engine", "liquid_ui_service.py")

# The deprecated indigo #6C63FF, its rgb form, and the light-indigo tints the old
# onboarding used. All are banned from the onboarding surfaces.
BANNED = (
    "#6c63ff",       # the exact indigo the steward flagged (b1.1)
    "108,99,255",    # ...as an rgb() triple
    "160,150,255",   # the light-indigo tint used on borders/tracks
    "#a78bff",       # the light-indigo end of the old companion gradient
    "#cfc9ff",       # the light-indigo companion message color
)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class OnboardingPaletteGuard(unittest.TestCase):

    def test_source_guard_onboarding_js_has_no_indigo(self):
        """hartOnboarding.js carries none of the deprecated indigo literals."""
        low = _read(ONBOARDING_JS).lower()
        for tok in BANNED:
            self.assertNotIn(
                tok.lower(), low,
                "deprecated indigo '%s' is back in hartOnboarding.js - the onboarding "
                "must use teal #00E6C3 + violet #9B5CFF (b1.2)" % tok)

    def test_source_guard_onboarding_css_hob_rules_have_no_indigo(self):
        """The .hob-* onboarding CSS rules in liquid_ui_service.py carry no indigo.

        Scoped to lines that mention a `hob-` selector so this checks ONLY the
        onboarding component block, not the whole 8000-line shell file."""
        hob_lines = [ln for ln in _read(SHELL_PY).splitlines() if "hob-" in ln]
        # Sanity: the block must exist (guards against the selectors being renamed
        # away silently, which would make this test vacuously pass).
        self.assertTrue(
            any(".hob-orb" in ln for ln in hob_lines),
            ".hob-orb rule not found - the onboarding CSS block moved/renamed; "
            "update this guard so it keeps covering the onboarding surface")
        blob = "\n".join(hob_lines).lower()
        for tok in BANNED:
            self.assertNotIn(
                tok.lower(), blob,
                "deprecated indigo '%s' is back in the .hob-* onboarding CSS - use "
                "teal #00E6C3 core + violet #9B5CFF accents (b1.2 / GF3)" % tok)

    def test_source_guard_onboarding_carries_the_brand_teal(self):
        """Positive check: the rebrand actually landed the brand teal (not merely
        stripped the indigo to nothing)."""
        js = _read(ONBOARDING_JS).lower()
        hob = "\n".join(ln for ln in _read(SHELL_PY).splitlines() if "hob-" in ln).lower()
        self.assertIn("#00e6c3", js, "onboarding JS companion gradient must lead with brand teal")
        self.assertIn("rgba(0,230,195", hob, ".hob-* rules must use the brand teal (rgb 0,230,195)")


if __name__ == "__main__":
    unittest.main()
