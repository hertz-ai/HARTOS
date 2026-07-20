#!/usr/bin/env python3
"""Latency-budget coverage checker -- THE single implementation, two callers.

Callers (Gate 4: one implementation, no parallel checker):
  1. tests/unit/test_latency_budget_coverage.py  -- dev loop + CI gate-checks
  2. nixos/packages/hart-app.nix `checkPhase`    -- BUILD gate

Why a build gate and not only a test (steward asked the exact right question,
2026-07-20 "fails in tests or at compile or build time?"): in this repo
`publish-nightly` needs only [build-iso, build-installers], and `build-iso` does
NOT need `gate-checks` -- proven on run 29725400559, where gate-checks was
cancelled while iso-desktop shipped. So a pure pytest guard could fail and a
nightly would still publish. Only `tag-and-sign` is gated on the full suite.
Running this in the derivation's checkPhase makes the ISO itself unbuildable
when coverage regresses, which is the enforcement level the mandate deserves.

Exit codes: 0 = ok, 1 = violation (fails the build), 2 = harness error.
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUDGETS = os.path.join(REPO, "docs", "architecture", "latency_budgets.json")

VALID_KINDS = {
    "drag", "hover", "scroll", "window-move", "resize", "press", "key",
    "animate-start",
}
# Continuous interactions must be same-frame by construction.
ONE_FRAME_KINDS = {"drag", "hover", "scroll", "window-move", "resize"}
ONE_FRAME_MS = 16

# marker proving the surface is SERVED -> (component, required kinds)
REQUIRED = [
    ("hart-hero-orbwrap", "orb", ("drag", "hover", "press")),
    ("hart-orb-orbit", "orb-rings", ("animate-start",)),
    ("hart-ambient", "bloom-field", ("animate-start",)),
    ("top-bar", "top-bar", ("press",)),
    ("taskbar", "taskbar", ("press",)),
    ("start-menu", "start-menu", ("press",)),
    ("hart-desktop", "desktop-icon", ("drag", "press")),
    ("hart-senses-mic", "senses-mic", ("press",)),
]


def load_budgets(path=BUDGETS):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def check_static(budgets):
    """Invariants that need NO render. These are the anti-gaming core, so they
    are always enforced -- including in the build sandbox."""
    errs = []
    if "_defaults" not in budgets or "components" not in budgets:
        return ["latency_budgets.json missing _defaults/components"]
    if not budgets["components"]:
        errs.append("no components declared")
    for kind in budgets["_defaults"]:
        if kind not in VALID_KINDS:
            errs.append("unknown interaction kind in _defaults: %s" % kind)
    for comp, spec in budgets["components"].items():
        for kind, val in spec.items():
            if kind.startswith("_"):
                continue
            if kind not in VALID_KINDS:
                errs.append("%s: unknown interaction kind %r" % (comp, kind))
                continue
            if not isinstance(val, (int, float)) or val <= 0:
                errs.append("%s.%s must be a positive number, got %r" % (comp, kind, val))
                continue
            if kind in ONE_FRAME_KINDS and val > ONE_FRAME_MS:
                errs.append(
                    "%s.%s=%sms exceeds one frame (%dms). A >1-frame continuous "
                    "budget declares an easing layer between pointer and pixel "
                    "(the 2026-07-20 rubber-band class). Fix the path, not the number."
                    % (comp, kind, val, ONE_FRAME_MS))
    if len(budgets["components"]) < 20:
        errs.append("only %d components carry budgets -- full-spectrum coverage is "
                    "the mandate" % len(budgets["components"]))
    return errs


def check_served_coverage(budgets, html):
    """Every LIVE surface must carry a budget. Enumerated from the SERVED shell,
    never a hand-written list (a list is what drifts, and then the harness
    measures a shrinking fraction of the product)."""
    errs = []
    for marker, comp, kinds in REQUIRED:
        if marker not in html:
            continue  # genuinely removed from the product
        spec = budgets["components"].get(comp)
        if not spec:
            errs.append("%s (served marker %r) has NO budget entry" % (comp, marker))
            continue
        for kind in kinds:
            if kind not in spec:
                errs.append("%s.%s missing (served marker %r)" % (comp, kind, marker))
    keyframes = set(re.findall(r"@keyframes\s+([A-Za-z0-9_-]+)", html))
    animated = {c for c, s in budgets["components"].items() if "animate-start" in s}
    if keyframes and len(animated) < 8:
        errs.append("shell serves %d keyframes but only %d components declare "
                    "animate-start budgets -- coverage has thinned"
                    % (len(keyframes), len(animated)))
    return errs


def render_shell():
    """Render the REAL served shell. Returns None if the shell cannot be imported
    (a build-sandbox environment difference), which is reported but does not
    brick the ISO -- the static anti-gaming checks above still hard-fail."""
    try:
        os.environ.setdefault("HART_OS_MODE", "1")
        sys.path.insert(0, REPO)
        from integrations.agent_engine.liquid_ui_service import LiquidUIService
        return LiquidUIService().render_desktop_shell()
    except Exception as e:  # noqa: BLE001 - reported, never silently swallowed
        print("check_latency_budgets: shell render unavailable (%s: %s); "
              "static checks still enforced" % (type(e).__name__, e),
              file=sys.stderr)
        return None


def main():
    try:
        budgets = load_budgets()
    except Exception as e:  # noqa: BLE001
        print("check_latency_budgets: cannot read budgets: %s" % e, file=sys.stderr)
        return 2
    errs = check_static(budgets)
    html = render_shell()
    if html:
        errs += check_served_coverage(budgets, html)
    if errs:
        print("LATENCY BUDGET COVERAGE FAILED (%d):" % len(errs), file=sys.stderr)
        for e in errs:
            print("  - %s" % e, file=sys.stderr)
        return 1
    print("latency budget coverage OK (%d components%s)"
          % (len(budgets["components"]), "" if html else ", static-only"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
