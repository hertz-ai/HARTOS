"""The shell stands down only for chrome the compositor has actually claimed.

WHY THIS BRIDGE EXISTS
  hart-comp composites the aura backdrop (M1) and the breathing orb (M2)
  itself. Both are painted UNDER the shell's layer surface, so while the shell
  ALSO draws them the native ones are invisible and the browser keeps paying for
  animation it does not need to run. Measured on the box 2026-08-28 with
  `strace -c` on WebKitWebProcess while it burned a full core: 0.64s of syscall
  time out of ~6s and ZERO ioctls, i.e. ~5.4s of pure userspace pixel work.

  So the compositor publishes what it owns and the shell stands down for exactly
  those pieces. One verdict file, many consumers — the same contract
  /run/hart/gpu-render already uses, whose own comment says "REUSE the probe's
  verdict; do NOT invent a second probe".

THE DIRECTION THAT MATTERS
  Guessing wrong here does not hang anything, it produces a desktop with no
  background or no orb — and the paint watchdog catches hangs, not wrong-looking
  desktops. So every failure path must land on "the shell draws everything",
  which is byte-for-byte today's behaviour. This file pins that direction.

Run:
  pytest tests/unit/test_native_chrome_bridge.py -v --noconftest
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from integrations.agent_engine import liquid_ui_service as L  # noqa: E402

SHELL_SRC = os.path.join(REPO, "integrations", "agent_engine", "liquid_ui_service.py")


@pytest.fixture
def verdict(tmp_path, monkeypatch):
    """Point the reader at a temp verdict file."""
    f = tmp_path / "native-chrome"
    monkeypatch.setattr(L, "_NATIVE_CHROME_FILE", str(f))
    return f


def test_missing_file_means_the_shell_draws_everything(verdict):
    """THE fail-safe. An absent compositor verdict must never be read as a
    claim: that is how you get a desktop with no wallpaper."""
    assert L.read_native_chrome() == frozenset()


def test_unreadable_file_fails_the_same_way(verdict, monkeypatch):
    def boom(*a, **k):
        raise PermissionError("nope")
    monkeypatch.setattr("builtins.open", boom)
    assert L.read_native_chrome() == frozenset()


def test_a_claim_is_honoured(verdict):
    verdict.write_text("bloom,orb")
    assert L.read_native_chrome() == frozenset({"bloom", "orb"})


def test_partial_claims_are_honoured_independently(verdict):
    """The compositor may own the backdrop but not the orb (M1 shipped before
    M2). Each piece stands down on its own."""
    verdict.write_text("bloom")
    got = L.read_native_chrome()
    assert "bloom" in got and "orb" not in got


def test_unknown_names_are_ignored_not_trusted(verdict):
    """A NEWER compositor claiming `taskbar` must not make an OLDER shell hide a
    taskbar it still owns. Forward compatibility has to fail closed."""
    verdict.write_text("bloom,taskbar,topbar,nonsense")
    assert L.read_native_chrome() == frozenset({"bloom"})


def test_whitespace_and_newlines_are_tolerated(verdict):
    verdict.write_text("  bloom \n orb \n")
    assert L.read_native_chrome() == frozenset({"bloom", "orb"})


def test_empty_file_claims_nothing(verdict):
    verdict.write_text("   \n  ")
    assert L.read_native_chrome() == frozenset()


# ── the consumers ────────────────────────────────────────────────────────────

def _src():
    with open(SHELL_SRC, encoding="utf-8") as fh:
        return fh.read()


def test_the_wallpaper_stands_down_for_a_claimed_bloom():
    """The default wallpaper bottoms out in an OPAQUE linear-gradient. That is
    exactly what has hidden the native bloom since M1, so the claim must make it
    transparent or the bridge does nothing."""
    src = _src()
    i = src.index("native_chrome = read_native_chrome()")
    window = src[i: i + 1200]
    assert "'bloom' in native_chrome" in window
    assert "wp_css = 'transparent'" in window, (
        "a claimed bloom must make the shell's wallpaper transparent")


def test_the_shell_hides_its_own_orb_when_the_compositor_owns_it():
    """Without this there would be TWO orbs, the native one breathing under an
    HTML one breathing on top — and the browser would still pay the per-frame
    cost M2 exists to remove."""
    src = _src()
    i = src.index("native_chrome = read_native_chrome()")
    window = src[i: i + 2000]
    assert "'orb' in native_chrome" in window
    assert "hart-voice-orb" in window, "the HTML orb must be suppressed"
    assert "animation:none" in window, (
        "suppressing the orb must also stop its animation, or the browser keeps "
        "rasterising an invisible element every frame")


def test_the_orb_keeps_its_hit_target():
    """visibility, not display:none. The wrapper must keep its layout box so
    click-to-talk and drag keep working against the same geometry while the
    compositor draws the pixels."""
    src = _src()
    i = src.index("native_orb_css = ")
    window = src[i: i + 600]
    assert "visibility:hidden" in window
    assert "display:none" not in window, (
        "display:none would remove the orb's hit target and break input, which "
        "is a far bigger change than swapping who paints it")
