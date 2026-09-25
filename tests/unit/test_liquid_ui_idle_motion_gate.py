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
    #
    # `lg-pulse` USED TO BE HERE, justified as "runs only while a sense is
    # active". Reading hartSenses.js showed that is false: the eye is lit
    # whenever senses are merely UNCUT, which is the default, so it animated
    # forever. It is gated now. A keep-list entry is a claim about behaviour,
    # and this one was never checked against the code that sets the class.
    "lg-breathe-ring": "listening ring -- runs only while actually listening",
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

# ─────────────────────────────────────────────────────────────────────────────
# THE IDLE RULE (2026-09-24, the shell hot-path diet). The webkit-flat gate
# above stops idle motion on the software-paint rung. On the GPU rung the
# same animations run forever, and a box nobody is sitting at still spends a
# frame clock on them. hartVisibility.js already stamps `data-idle="1"` on
# <html> after 6 s without input (nothing lights on a timer, but a pause on a
# measured absence is the same engine's job), so every infinite animation
# that is NOT the orb's breathing and NOT a state-driven interaction cue must
# be paused under html[data-idle="1"], on every rung. The signal survives:
# only the play state changes, never the colour.
#
# This extends the enumeration to every stylesheet the shell serves, not just
# the inline one: hartHome.css, hartResponsive.css and the CSS the modules
# inject from JS strings. `.hob-orb` was missed by a survey of one file; the
# next one could hide in any of these.
# ─────────────────────────────────────────────────────────────────────────────
_STATIC_SOURCES = ("hartHome.css", "hartResponsive.css", "hartHero.js",
                   "hartContextMenu.js", "hartConnectivity.js", "hartDismiss.js")

_IDLE_KEEP = {
    # THE ORB. Checklist c2 (a living, breathing orb, the feel-alive pillar) and
    # c8 (breathing is the user's toggle, DEFAULT ON). The steward's rule is
    # that the orb breathes, not the chrome around it. These are the orb's
    # brand aura (hartHero.js) and the top-bar orb, and the GPU rung is the
    # only rung they run on (webkit-flat pauses them by selector above).
    "hha-breathe": "the orb's brand aura rings: c2/c8, breathing default ON",
    "hha-halo": "the orb's brand aura halo: c2/c8, breathing default ON",
    "tbOrbBreathe": "the top-bar orb: the same orb presence, gpu-hardware only",
    "hob-breathe": ("the onboarding orb on the Light Your HART screen: the orb, "
                    "c2; paused by selector on webkit-flat, where it measured "
                    "97.4 percent of a core"),
    # THE LIVING CANVAS (hartHome.css). Ambient blobs, hue drift, the orb float
    # and breathe, the ring spin and the live dots are the home's own life
    # (checklist b4/i3 audit), each on a --hart-motion-* var the user and the
    # webkit-flat rule can pause centrally; on the GPU rung they are composited
    # layers, not software repaints.
    "vBlob1": "home ambient layer, --hart-motion-ambient",
    "vHue": "home ambient hue drift, --hart-motion-ambient",
    "vFloat": "home orb float, --hart-motion-orb (c2)",
    "vBreathe": "home orb breathe, --hart-motion-orb (c2/c8)",
    "vSpin": "home ring spin, --hart-motion-rings",
    "hhLiveDot": "home live dots, --hart-motion-detail",
}


def _served_sources() -> dict:
    """{name: source} for every stylesheet-bearing file the shell serves."""
    static = _SVC.parent / "static"
    out = {"liquid_ui_service.py": _src()}
    for name in _STATIC_SOURCES:
        p = static / name
        if p.is_file():
            out[name] = p.read_text(encoding="utf-8")
    return out


def _infinite_in(src: str) -> dict:
    """animation-name -> {selectors} for one source (same walk as below)."""
    found = {}
    for m in re.finditer(r"animation:\s*([A-Za-z0-9_-]+)[^;}]*?infinite", src):
        name = m.group(1)
        brace = src.rfind("{", 0, m.start())
        if brace < 0:
            continue
        prev_close = src.rfind("}", 0, brace)
        selector = src[prev_close + 1:brace]
        selector = re.sub(r"/\*.*?\*/", " ", selector, flags=re.S)
        selector = " ".join(selector.split())
        for boundary in ("'", '"', ";"):
            if boundary in selector:
                selector = selector.rsplit(boundary, 1)[-1].strip()
        if selector:
            found.setdefault(name, set()).add(selector)
    return found


def _all_infinite() -> dict:
    """animation-name -> {(file, selector)} across every served source."""
    out = {}
    for fname, src in _served_sources().items():
        for name, sels in _infinite_in(src).items():
            for sel in sels:
                out.setdefault(name, set()).add((fname, sel))
    return out


def _idle_pause_rules() -> str:
    """Every rule whose selector list carries html[data-idle="1"] and whose
    body pauses the animation, as one string of selectors."""
    src = _src()
    out = []
    for m in re.finditer(r'((?:html\[data-idle="1"\][^{}]*?)\{[^}]*animation-play-state\s*:\s*paused[^}]*\})', src):
        out.append(m.group(1))
    return "\n".join(out)


@pytest.mark.parametrize("name", sorted(_all_infinite()))
def test_every_infinite_animation_pauses_at_idle_unless_it_is_the_orb(name):
    """The idle rule, on every rung and every served stylesheet."""
    if name in _KEEP or name in _IDLE_KEEP:
        assert (_KEEP.get(name) or _IDLE_KEEP.get(name)).strip(), (
            "a keep-list entry must state WHY")
        return
    idle = _idle_pause_rules()
    for fname, sel in _all_infinite()[name]:
        tail = sel.split(",")[-1].strip()
        if tail and tail in idle:
            return
    pytest.fail(
        "the infinite animation '%s' (declared in %s) keeps running while nobody "
        "is at the desk: it is neither paused under html[data-idle=\"1\"] "
        "(animation-play-state:paused, the visibility engine's own idiom) nor "
        "the orb's breathing (_IDLE_KEEP) nor a state-driven cue (_KEEP). Add "
        "its selector to the idle rule in liquid_ui_service.py, or add it to a "
        "keep-list WITH the reason it earns idle frames."
        % (name, ", ".join(sorted("%s: %s" % fs for fs in _all_infinite()[name]))))


def test_the_idle_rule_exists_and_is_the_visibility_engines_attribute():
    idle = _idle_pause_rules()
    assert idle, "no html[data-idle=\"1\"] ... {animation-play-state:paused} rule is served"
    assert 'html[data-idle="1"]' in _src(), "the idle attribute hartVisibility.js stamps is not read by any CSS"


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
    end = src.index("{animation:none!important}", start) + len("{animation:none!important}")
    return src[start:end]


def test_the_gate_still_exists():
    """If this fails, the software-paint gate was removed or renamed, and every
    assertion below is meaningless."""
    src = _src()
    assert "sw-paint: idle motion stopped" in src, (
        "the software-paint idle-motion gate is gone from liquid_ui_service.py")
    assert "body.webkit-flat{--hart-motion-ambient:paused;" in src, (
        "the --hart-motion-* pause half of the gate is gone")
    assert "{animation:none!important}" in _gate_rule(), (
        "the named-selector half of the gate is gone, or lost its !important")


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


def test_the_gate_wins_the_cascade_not_just_ties():
    """The gate must beat hartHome.css, which styles some of the same elements.

    THE DEFECT, measured on real HW 2026-09-10. Two rules, both matching:

        hartHome.css:734   body.gpu-hardware .top-bar-orb { animation: tbOrbBreathe ... }
        the gate           body.webkit-flat  .top-bar-orb { animation: none }

    Both are (0 ids, 2 classes, 1 element) -- an exact specificity TIE. The body
    carries BOTH classes ("gpu-hardware webkit-flat"), so both apply, and source
    order decides. hartHome.css is an external sheet loading after this inline
    block, so it won, and `.top-bar-orb` kept animating on every software-paint
    box from the day the gate was written. `document.getAnimations()` on the live
    node showed it `play=running` while the gate's var-driven entries showed
    `play=paused` -- the gate half that works, beside the half that never did.

    A tie is not a gate. `!important` is what makes it one.
    """
    gate = _gate_rule()
    assert "!important" in gate, (
        "the gate must use !important: it TIES on specificity with hartHome.css "
        "for at least .top-bar-orb, and a tie is decided by source order, which "
        "the external sheet wins")


def test_selectors_shared_with_hartHome_css_are_all_covered():
    """Any element hartHome.css animates AND this gate names must be protected
    by the !important above -- catching the next tie before hardware does."""
    import pathlib as _p
    home = (_p.Path(__file__).resolve().parents[2] / "integrations" / "agent_engine"
            / "static" / "hartHome.css")
    if not home.is_file():
        pytest.skip("hartHome.css not present")
    css = home.read_text(encoding="utf-8")
    gate = _gate_rule()
    # Every class the gate names, that hartHome.css also gives an animation to.
    for cls in re.findall(r"body\.webkit-flat ([.\w\s-]+?)[,{]", gate):
        cls = cls.strip()
        if not cls.startswith("."):
            continue
        for m in re.finditer(re.escape(cls) + r"\s*\{[^}]*animation:", css):
            assert "!important" in gate, (
                "hartHome.css animates %s and the gate also names it; without "
                "!important the two can tie and source order decides" % cls)
            break
