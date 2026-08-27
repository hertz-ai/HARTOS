"""The shell-paint watchdog budget must have exactly ONE source of truth.

Tier-1 is the tier the OS exists to paint. It died on the fleet box for two
months for a reason that had nothing to do with the compositor:

  hart-session-supervisor.nix raised its default 20 -> 120 on 2026-08-12, after
  measuring first paint landing 16 MILLISECONDS after a 45s watchdog fired and
  killed a completely healthy Tier-1 ("WebKit + GTK4 simply take ~45s to load
  off a 22 MB/s stick").

  profiles/desktop.nix still carried `shellPaintTimeoutSeconds = 45` from
  2026-06-24. A plain assignment outranks a module default, so the August fix
  was dead on the desktop profile and every cold boot killed Tier-1 at exactly
  45s.

Measured on the box 2026-08-27, cold boot from the USB stick:

    04:36:51  hart-comp: first real scanout — the display is LIVE
    04:36:51  [glass-shell] render rung = webkit-cairo    (shell starts)
    04:37:12  [glass-shell] GSK = CAIRO / portal OWNED    (+21s, still warming)
    04:37:30  supervisor: HUNG (compositor up, no first paint in 45s)

hart-comp had DRM master and had already page-flipped. The same startup probes
that took 21s cold measure ~300ms warm. That is storage speed deciding whether
the desktop comes up, which the module comment explicitly says a watchdog must
not do.

So: no profile may re-declare this budget, and the module default may not drop
back under the measured cold-boot cost.

Run:
  pytest tests/unit/test_paint_budget_single_source.py -v --noconftest
"""

import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = os.path.join(REPO, "nixos", "modules", "hart-session-supervisor.nix")
PROFILES = os.path.join(REPO, "nixos", "profiles")

# The cold-boot cost the module measured: WebKit + GTK4 off a 22 MB/s stick.
MEASURED_COLD_PAINT_S = 45


def _module_default():
    """The default from the OPTION DECLARATION.

    Anchored on `shellPaintTimeoutSeconds = lib.mkOption {`, not on the first
    mention of the name: the name also appears in prose comments earlier in the
    file, and splitting on that picked up an unrelated option's `default = 3`.
    """
    src = open(MODULE, encoding="utf-8").read()
    m = re.search(r"shellPaintTimeoutSeconds\s*=\s*lib\.mkOption\s*\{", src)
    assert m, "could not find the shellPaintTimeoutSeconds option declaration"
    blk = src[m.end():]
    return int(re.search(r"default\s*=\s*(\d+)\s*;", blk).group(1))


def _profile_overrides():
    hits = []
    for root, _dirs, files in os.walk(PROFILES):
        for fn in files:
            if not fn.endswith(".nix"):
                continue
            p = os.path.join(root, fn)
            for i, line in enumerate(open(p, encoding="utf-8"), 1):
                if line.lstrip().startswith("#"):
                    continue
                if re.search(r"shellPaintTimeoutSeconds\s*=", line):
                    hits.append((os.path.relpath(p, REPO), i, line.strip()))
    return hits


def test_no_profile_redeclares_the_paint_budget():
    """The stale-override bug, as a test."""
    hits = _profile_overrides()
    assert not hits, (
        "a profile re-declares shellPaintTimeoutSeconds, which silently "
        "outranks the module default and is exactly how the 120s fix stayed "
        "dead behind a stale 45: %s" % hits)


def test_module_default_clears_the_measured_cold_boot_cost():
    """A budget under the measured load time does not detect hangs, it causes
    them: the tier is killed while it is legitimately still loading."""
    d = _module_default()
    assert d > MEASURED_COLD_PAINT_S, (
        "module default %ds is at or under the %ds cold-boot load measured on "
        "real hardware; Tier-1 would be killed while healthy"
        % (d, MEASURED_COLD_PAINT_S))


def test_module_default_still_bounds_a_real_hang():
    """The other direction: a watchdog that never fires is not a watchdog.
    120s catches a genuine hang while leaving headroom for slow media."""
    assert _module_default() <= 300, (
        "paint budget is so large that a genuinely hung tier would leave the "
        "screen black for minutes before dropping a rung")


@pytest.mark.parametrize("path", ["nixos/profiles/desktop.nix"])
def test_the_removal_is_explained_where_it_was(path):
    """The next person to hit a slow boot must find the history here, or they
    will simply re-add the override."""
    src = open(os.path.join(REPO, path), encoding="utf-8").read()
    assert "shellPaintTimeoutSeconds" in src, (
        "%s should still MENTION the budget in prose, so the reason it is not "
        "set here is discoverable" % path)
    assert "16 MILLISECONDS" in src or "no paint-budget override" in src.lower(), (
        "%s must record WHY the override was removed" % path)
