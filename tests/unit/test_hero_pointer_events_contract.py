"""The hero must not turn its whole bounding box into a click target.

The shell CSS states the contract deliberately:

    .hart-hero    { pointer-events: none }   /* the 660px box is not a target */
    .hart-hero>*  { pointer-events: auto }   /* the orb / input / chips are   */

The hero is position:fixed at top:46% and hartHero.js raises it to z-index 1450
so it floats above app windows. That is fine ONLY while its container lets
clicks through: it is parked directly across the Home content.

hartHero.js used to write `s.pointerEvents = 'auto'` on the container inside
place(). An inline style outranks the stylesheet, so the whole box became a
click target sitting over the Continue row -- the "orb swallows clicks meant
for other components" report (#32). Observed on the box 2026-08-27, where the
hero renders at opacity 0.34 across the Continue cards.

pointer-events INHERITS, so restoring the contract costs nothing: the direct
children are set to auto and everything inside them inherits it. Nothing the
user can see stops being clickable.

Run:
  pytest tests/unit/test_hero_pointer_events_contract.py -v --noconftest
"""

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERO_JS = os.path.join(REPO, "integrations", "agent_engine", "static", "hartHero.js")
SHELL = os.path.join(REPO, "integrations", "agent_engine", "liquid_ui_service.py")


def _js():
    return open(HERO_JS, encoding="utf-8").read()


def _strip_js_comments(src):
    """Drop // and /* */ comments so the guards read CODE, not the prose that
    legitimately quotes the very thing we want absent."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("//"))


def test_hero_container_is_not_forced_clickable():
    """The #32 bug, as a test."""
    code = _strip_js_comments(_js())
    bad = re.findall(r"pointerEvents\s*=\s*['\"]auto['\"]", code)
    assert not bad, (
        "hartHero.js sets pointerEvents='auto' on the hero container. That is "
        "an inline style, so it outranks .hart-hero{pointer-events:none} and "
        "turns the whole 660px box into a click target parked over the Home "
        "content (#32). Leave the container to the stylesheet.")


def test_css_contract_is_still_declared():
    """The fix only works because the stylesheet owns the decision; if these
    rules go, clearing the inline value would make the hero unclickable."""
    css = open(SHELL, encoding="utf-8").read()
    assert re.search(r"\.hart-hero\{[^}]*pointer-events:\s*none", css), (
        "the hero container must declare pointer-events:none so clicks reach "
        "whatever it floats over")
    assert re.search(r"\.hart-hero>\*\{[^}]*pointer-events:\s*auto", css), (
        "the hero's direct children must declare pointer-events:auto, or the "
        "orb, input and chips become unclickable")


def test_hero_still_floats_above_windows():
    """Guard the OTHER half: this must not be 'fixed' by dropping the hero
    behind app windows, which is a different regression."""
    code = _strip_js_comments(_js())
    assert re.search(r"zIndex\s*=\s*['\"]1450['\"]", code), (
        "the hero is meant to ride above app windows; if its z-index handling "
        "changed, the pointer-events contract above needs re-checking too")
