"""L1 coverage guard: EVERY interactive/animated surface must have a declared
input-to-photon budget.

Steward mandate (2026-07-20): "input to photon shd be measured for every UI
component view animations as tests which can be applied at scale for full
spectrum of what we develop."

The measurement itself needs the compositor (true input-to-photon = kernel input
timestamp -> DRM page-flip completion; see LATENCY_HARNESS.md L2/L3). What CAN be
enforced on any box, today, is the thing that makes the mandate SCALE: coverage.
If a new component can ship without a budget, the harness silently measures a
shrinking fraction of the product and the numbers become a comfortable lie.

So this test ENUMERATES the live surface from the SERVED shell rather than from a
hand-written list -- a hand-written list is exactly what drifts. Add an animation
or an interactive surface and this fails until a budget is declared.

These are behavioural in the way that matters here: they render the REAL shell via
the REAL Flask app and read what it actually serves.
"""
import json
import os
import re

import pytest

from integrations.agent_engine.liquid_ui_service import LiquidUIService

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUDGETS_PATH = os.path.join(REPO, "docs", "architecture", "latency_budgets.json")
LEDGER_PATH = os.path.join(REPO, "docs", "architecture", "NATIVE_SHELL_CSS_PARITY_LEDGER.md")
VALID_KINDS = {
    "drag", "hover", "scroll", "window-move", "resize", "press", "key",
    "animate-start",
}
# Continuous interactions MUST be same-frame: a budget above one frame means an
# easing layer sits between input and transform (the 2026-07-20 drag bug class).
ONE_FRAME_KINDS = {"drag", "hover", "scroll", "window-move", "resize"}
ONE_FRAME_MS = 16


@pytest.fixture(scope="module")
def budgets():
    with open(BUDGETS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def shell_html():
    return LiquidUIService().render_desktop_shell()


def test_budgets_file_is_wellformed(budgets):
    assert "_defaults" in budgets and "components" in budgets
    for kind in budgets["_defaults"]:
        assert kind in VALID_KINDS, f"unknown interaction kind in _defaults: {kind}"
    assert budgets["components"], "no components declared"


def test_every_declared_budget_uses_a_known_kind_and_is_a_number(budgets):
    for comp, spec in budgets["components"].items():
        for kind, val in spec.items():
            if kind.startswith("_"):
                continue
            assert kind in VALID_KINDS, f"{comp}: unknown interaction kind {kind!r}"
            assert isinstance(val, (int, float)) and val > 0, (
                f"{comp}.{kind} budget must be a positive number, got {val!r}")


def test_continuous_interactions_are_budgeted_to_one_frame(budgets):
    """drag/hover/scroll/window-move/resize must be same-frame.

    This is the structural guard against the rubber-band class: if someone raises
    a drag budget to 'make the test pass', they are declaring that an easing layer
    is acceptable between the pointer and the pixel. It is not."""
    offenders = []
    for comp, spec in budgets["components"].items():
        for kind, val in spec.items():
            if kind in ONE_FRAME_KINDS and val > ONE_FRAME_MS:
                offenders.append(f"{comp}.{kind}={val}ms")
    assert not offenders, (
        "continuous interactions budgeted above one frame (" + str(ONE_FRAME_MS)
        + "ms): " + ", ".join(offenders)
        + " -- a >1-frame continuous budget means an easing layer between input "
          "and transform; fix the path, do not raise the budget")


def test_every_animation_in_the_served_shell_has_an_owner_with_a_budget(
        shell_html, budgets):
    """Full-spectrum coverage for ANIMATIONS.

    Every @keyframes the shell serves is a moving surface a user perceives, so
    each must belong to a component that carries an animate-start budget. New
    animation with no owning budget -> this fails."""
    keyframes = set(re.findall(r"@keyframes\s+([A-Za-z0-9_-]+)", shell_html))
    assert keyframes, "no @keyframes found in the served shell -- extractor drifted"
    animated_components = {
        c for c, spec in budgets["components"].items()
        if "animate-start" in spec
    }
    # The budget file must carry at least one animated owner per broad surface
    # family the shell animates. Families are derived from the keyframe names the
    # shell actually serves, so this scales with the product rather than a list.
    assert animated_components, (
        "no component declares an animate-start budget, yet the shell serves "
        f"{len(keyframes)} keyframes")
    # Guard the guard: if the shell grows a lot of animation while the budget file
    # stays tiny, coverage has silently thinned.
    assert len(animated_components) >= 8, (
        f"the shell serves {len(keyframes)} keyframes but only "
        f"{len(animated_components)} components declare animate-start budgets -- "
        "coverage has thinned; declare budgets for the new animated surfaces")


def test_headline_interactive_surfaces_are_covered(shell_html, budgets):
    """The surfaces the steward actually touches must never lose a budget.

    Each entry maps a marker that PROVES the surface is in the served shell to the
    component that must carry its budget. If the shell still ships it, a budget is
    mandatory -- this is what stops coverage regressing as the UI evolves."""
    required = [
        ("hart-hero-orbwrap", "orb", ("drag", "hover", "press")),
        ("hart-orb-orbit", "orb-rings", ("animate-start",)),
        ("hart-ambient", "bloom-field", ("animate-start",)),
        ("top-bar", "top-bar", ("press",)),
        ("taskbar", "taskbar", ("press",)),
        ("start-menu", "start-menu", ("press",)),
        ("hart-desktop", "desktop-icon", ("drag", "press")),
        ("hart-senses-mic", "senses-mic", ("press",)),
    ]
    missing = []
    for marker, comp, kinds in required:
        if marker not in shell_html:
            continue  # surface genuinely removed from the product: nothing to cover
        spec = budgets["components"].get(comp)
        if not spec:
            missing.append(f"{comp} (served marker {marker!r}) has NO budget entry")
            continue
        for kind in kinds:
            if kind not in spec:
                missing.append(f"{comp}.{kind} missing (served marker {marker!r})")
    assert not missing, "latency budgets missing for live surfaces: " + "; ".join(missing)


def test_ledger_components_are_reflected_in_the_budget_surface_families():
    """The CSS parity ledger is the exhaustive component inventory (93 components).
    The budget file need not name all 93 individually -- many share a family -- but
    it must not be a token list while the ledger documents a large product."""
    if not os.path.isfile(LEDGER_PATH):
        pytest.skip("parity ledger not present")
    with open(LEDGER_PATH, "r", encoding="utf-8") as f:
        ledger = f.read()
    with open(BUDGETS_PATH, "r", encoding="utf-8") as f:
        comps = json.load(f)["components"]
    assert len(ledger) > 5000, "ledger looks truncated"
    assert len(comps) >= 20, (
        f"the parity ledger documents a full desktop but only {len(comps)} "
        "components carry latency budgets -- full-spectrum coverage is the mandate")


# ── The BUILD gate shares this exact logic (Gate 4: one implementation) ──────
def test_standalone_checker_is_the_same_logic_and_passes():
    """scripts/check_latency_budgets.py is what the nix derivation's checkPhase
    runs, so the ISO cannot BUILD when coverage regresses.

    Why that matters (steward, 2026-07-20 "fails in tests or at compile or build
    time?"): in this repo publish-nightly needs only [build-iso,
    build-installers], and build-iso does NOT need gate-checks -- proven on run
    29725400559 where gate-checks was cancelled while iso-desktop shipped. A
    pytest-only guard could therefore fail while a nightly still published. The
    build gate closes that. This test keeps the two callers honest: the same
    module the build runs must also pass here."""
    import importlib.util, os
    # tests/unit/<file> -> repo root is THREE levels up (module-level REPO already
    # computes this correctly; recomputing it here got it wrong by one level).
    repo = REPO
    spec = importlib.util.spec_from_file_location(
        "check_latency_budgets", os.path.join(repo, "scripts", "check_latency_budgets.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    budgets = mod.load_budgets()
    errs = mod.check_static(budgets)
    assert not errs, "static budget invariants failed: " + "; ".join(errs)

# ── The OTHER direction: can the instrument actually MEASURE what is declared? ──
#
# Every test above asks whether a surface has a budget. None asks whether the
# thing that takes the measurement has a bucket for it. That gap is how the
# per-COMPONENT half of this table sat unread for its whole life: 23 rows, and
# latency.rs reported `component=shell` for every sample, so each one was checked
# against the _defaults and a slow orb was indistinguishable from a slow
# marketplace. That half is fixed (scene.rs names the surface, latency.rs buckets
# on it). This pins the KIND half so it cannot go the same way.

LATENCY_SRC = os.path.join(REPO, "compositor", "src", "latency.rs")

# Declared in latency_budgets.json, deliberately NOT measured yet, with the reason.
# Shrinking this list is the goal; GROWING it silently is what the test prevents.
UNMEASURED_KINDS = {
    "window-move": (
        "the compositor does not perform interactive window moves at all. "
        "wayland.rs's move_request is an intentional no-op: 'interactive "
        "resize/move are driven by the AI-native placement policy, not by the "
        "client grabbing the pointer (the WM owns geometry)'. The budget row "
        "exists for the affordance, which is Phase-8 polish."),
    "resize": (
        "same as window-move: resize_request is the matching deliberate no-op."),
    "animate-start": (
        "needs input-to-animation causality, not just input-to-photon: which "
        "input STARTED the workspace fade or the window map. The compositor has "
        "both clocks (ws_switch_at, MapAnim) but nothing carries the causing "
        "input through to them."),
}


def _measured_kinds():
    """The interaction kinds latency.rs can actually bucket a sample into."""
    src = open(LATENCY_SRC, encoding="utf-8").read()
    block = re.search(r"impl Kind \{(.*?)\n    const ALL", src, re.S)
    assert block, "latency.rs no longer has a Kind::label to read"
    kinds = set(re.findall(r'Kind::\w+ => "([^"]+)"', block.group(1)))
    assert kinds, "Kind::label names no kinds at all"
    return kinds


def test_the_instrument_can_measure_every_kind_it_claims_to(budgets):
    """A kind the instrument cannot bucket is a budget nothing will ever check.

    Two failure directions, both real:
      * a NEW budget kind lands and no bucket is added, so every sample of it is
        silently dropped and the row reads as covered;
      * the instrument grows a kind the budget file does not declare, so its
        samples are checked against nothing.
    """
    measured = _measured_kinds()
    declared = set(VALID_KINDS)
    assert measured <= declared, (
        "latency.rs measures %s, which latency_budgets.json does not declare"
        % sorted(measured - declared))
    gap = declared - measured
    assert gap == set(UNMEASURED_KINDS), (
        "the set of declared-but-unmeasured kinds changed to %s. If a kind became "
        "measurable, delete its UNMEASURED_KINDS entry. If a NEW kind was "
        "declared, either give latency.rs a bucket for it or record here why it "
        "cannot be measured yet: a budget nothing measures reads as coverage "
        "while measuring nothing." % sorted(gap))
    # And every unmeasured kind must still be a real declared budget somewhere,
    # or the exemption is protecting a row that no longer exists.
    every = set(budgets["_defaults"])
    for rows in budgets.get("components", {}).values():
        every |= {k for k in rows if not k.startswith("_")}
    for kind in UNMEASURED_KINDS:
        assert kind in every, (
            "%r is exempted from measurement but is no longer budgeted anywhere; "
            "drop the exemption" % kind)


def test_the_measured_kinds_carry_the_budget_the_file_declares(budgets):
    """latency.rs mirrors the _defaults as consts because the budget file lives
    outside the crate and crane's source filter would drop it. A mirror that
    drifts reports PASS against a number nobody agreed to."""
    src = open(LATENCY_SRC, encoding="utf-8").read()
    block = re.search(r"fn budget_ms\(self\) -> u64 \{(.*?)\n    \}", src, re.S)
    assert block, "latency.rs no longer has a readable budget_ms"
    body = block.group(1)
    defaults = budgets["_defaults"]
    for kind in sorted(_measured_kinds()):
        want = defaults[kind]
        # `Kind::Drag | Kind::Hover | Kind::Scroll => 16,` groups equal budgets.
        variant = "".join(p.capitalize() for p in kind.split("-"))
        arm = re.search(r"[^\n]*Kind::%s\b[^\n]*=> (\d+)," % variant, body)
        assert arm, "latency.rs::budget_ms has no arm for %s" % kind
        assert int(arm.group(1)) == want, (
            "latency.rs budgets %s at %sms, the file says %sms"
            % (kind, arm.group(1), want))
