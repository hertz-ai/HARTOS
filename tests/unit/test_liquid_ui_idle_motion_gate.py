"""Every infinite animation in the shell must be a decision, not an oversight.

WHY THIS TEST EXISTS
--------------------
The Liquid UI shell runs its GTK4 host on GSK_RENDERER=cairo on every rung
except vulkan, which means the host blits the window through the CPU on each
damage event. A CONTINUOUSLY running animation therefore forces a full software
repaint forever, whatever the compositor is doing. liquid_ui_service.py already
carries a measured gate for this: on `body.webkit-flat` it flips the
--hart-motion-* custom properties to `paused` and names the handful of infinite
animations that have no such var.

That gate was written from a survey of the stylesheet, and a survey is a thing
you do once. `.hart-onboarding .hob-orb` was added later and missed it: a 150px
orb carrying `0 0 70px` AND `0 0 150px` box-shadows, scaling to 1.08 and back
forever, on the "Light Your HART" screen that a node with no onboarding state
sits on from boot. That is precisely the shape the gate's own note calls out for
`.hart-hero-orb` ("each breathe frame re-rasterises two big blurs in software"),
and it is worse, because it is the FIRST screen a new user ever meets.

So it belongs in the gate on the gate's own stated criterion, and that is the
whole justification for the one-line fix. It is NOT justified by the CPU burn
measured on the node the same day, and the difference matters:

A CORRECTION, recorded because the first version of this file got it wrong.
The live node showed WebKit's main thread at 487 of 500 jiffies (97.4% of a
core) continuously from boot while hart-comp idled at 2.2%, and this file
originally named the orb as the cause. It is not. Measuring the thread's
context switches settled it:

    webkit  cpu=99%   voluntary=+37   nonvoluntary=+173   (over 10s)

A rAF or CSS-animation loop yields to the event loop about 60 times a second,
so voluntary switches would dominate. Three per second, against seventeen
preemptions, is a SYNCHRONOUS BUSY LOOP that almost never yields -- not an
animation. hart-comp idling at 2.2% says the same thing from the other end: a
60fps animation would have produced frames for it to composite, and there were
none. The busy loop is a separate, still-unidentified defect. Do not let this
fix be mistaken for having addressed it.

So this test replaces the survey with an invariant: enumerate every `infinite`
animation in the stylesheet and require each one to be either GATED on the
software-paint rung or in the documented keep-list below. A new decorative
animation that is neither now fails here instead of on someone's hardware.

Adding an animation? Do one of two things, and say which in the diff:
  * it runs at idle and costs a repaint  -> add its selector to the
    `body.webkit-flat ... {animation:none}` rule in liquid_ui_service.py
  * it is state-driven, or genuinely cheap -> add it to _KEEP below WITH the
    reason, the way the existing entries do
"""

import pathlib
import re

import pytest

_SVC = (pathlib.Path(__file__).resolve().parents[2]
        / "integrations" / "agent_engine" / "liquid_ui_service.py")


def _src() -> str:
    return _SVC.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# The keep-list: infinite animations that are DELIBERATELY left running on the
# software-paint rung. Each entry names WHY, because "it was already there" is
# not a reason and that is precisely how .hob-orb survived.
# ─────────────────────────────────────────────────────────────────────────────
_KEEP = {
    # State-driven: these run for seconds during a real interaction and are what
    # tells the user HART is doing something. The gate's own note keeps them.
    "lg-breathe-ring": "listening ring -- runs only while actually listening",
    "lg-pulse": "is-sensing -- runs only while a sense is active",
    "pulse": "mic recording -- runs only while recording, and is_potato-gated too",
    "lg-comet": "streaming comet -- runs only while a reply streams",
    # Cheap, and carrying real meaning at idle.
    "lg-empty-breathe": (
        "offline empty-state glyph: 28px, NO blur, and it is the signal that the "
        "box is offline -- a state the user needs to notice. State-driven "
        "signalling, kept by the same policy that keeps the listening ring."
    ),
    # Gated by a --hart-motion-* custom property instead of by selector, which
    # the webkit-flat rule flips to `paused` centrally.
    "hart-orbit-spin": "gated via --hart-motion-rings (set to paused on webkit-flat)",
}


def _infinite_animations() -> dict:
    """Map animation-name -> the selector(s) of the rule(s) declaring it.

    The CSS lives inside Python string literals, so this walks the real brace
    structure rather than guessing from line shape: find the `{` that opens the
    rule containing the declaration, then take everything back to the previous
    `}` as the selector (which is how CSS is delimited, whatever the line
    wrapping). Selectors are whitespace-normalised so a rule split across lines
    compares equal to the one-line form used in the gate.
    """
    src = _src()
    found = {}
    for m in re.finditer(r"animation:\s*([A-Za-z0-9_-]+)[^;}]*?infinite", src):
        name = m.group(1)
        brace = src.rfind("{", 0, m.start())
        if brace < 0:
            continue
        prev_close = src.rfind("}", 0, brace)
        selector = src[prev_close + 1:brace]
        # Drop any comment that sits between the rules.
        selector = re.sub(r"/\*.*?\*/", " ", selector, flags=re.S)
        selector = " ".join(selector.split())
        # Trim anything before the last statement boundary (quote, semicolon).
        for boundary in ("'", '"', ";"):
            if boundary in selector:
                selector = selector.rsplit(boundary, 1)[-1].strip()
        if selector:
            found.setdefault(name, set()).add(selector)
    return found


def _gate_rule() -> str:
    """The body.webkit-flat {animation:none} rule, as one string."""
    src = _src()
    start = src.index("sw-paint: idle motion stopped")
    end = src.index("{animation:none}", start) + len("{animation:none}")
    return src[start:end]


def test_the_gate_still_exists():
    """If this fails, the software-paint gate was removed or renamed, and every
    assertion below is meaningless."""
    src = _src()
    assert "sw-paint: idle motion stopped" in src, (
        "the software-paint idle-motion gate is gone from liquid_ui_service.py")
    assert "body.webkit-flat{--hart-motion-ambient:paused;" in src, (
        "the --hart-motion-* pause half of the gate is gone")
    assert "{animation:none}" in _gate_rule(), (
        "the named-selector half of the gate is gone")


def test_the_onboarding_orb_is_gated():
    """The specific regression, pinned by name.

    `.hob-orb` is 150px with `0 0 70px` and `0 0 150px` box-shadows, scaling
    forever, on the first screen a new user sees. It is gated because it matches
    the gate's own criterion, not because it was shown to cause the 97.4% burn
    measured on the node that day -- context-switch counts later showed that
    burn to be a synchronous busy loop, not an animation. See the correction in
    this file's module docstring.
    """
    gate = _gate_rule()
    assert "body.webkit-flat .hart-onboarding .hob-orb" in gate, (
        "the onboarding orb must be in the software-paint gate: two large "
        "box-shadows re-rasterised per frame in software is the single most "
        "expensive idle animation in the shell, and it runs on the FIRST "
        "screen a new user sees")


@pytest.mark.parametrize("name", sorted(_infinite_animations()))
def test_every_infinite_animation_is_gated_or_documented(name):
    """The invariant that replaces the one-time survey."""
    if name in _KEEP:
        assert _KEEP[name].strip(), (
            "a keep-list entry must state WHY, not just list the name")
        return

    selectors = _infinite_animations()[name]
    gate = _gate_rule()

    # Gated if any selector that declares it appears in the webkit-flat rule.
    for sel in selectors:
        # Compare on the distinctive tail of the selector (the gate prefixes
        # every entry with `body.webkit-flat `).
        tail = sel.split(",")[-1].strip()
        if tail and tail in gate:
            return

    pytest.fail(
        "the infinite animation '%s' (declared on %s) is neither gated on the "
        "software-paint rung nor in the documented keep-list.\n\n"
        "On the cairo rung every frame of it is a full software repaint, "
        "forever. Either add its selector to the `body.webkit-flat ... "
        "{animation:none}` rule in liquid_ui_service.py, or add it to _KEEP in "
        "this file WITH the reason it earns its frames.\n\n"
        "This test exists because `.hob-orb` was missed exactly this way and "
        "burned 97.4%% of a core on real hardware from boot."
        % (name, ", ".join(sorted(selectors))))
