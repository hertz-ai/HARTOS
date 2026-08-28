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


# ── the GTK4 host half (bridge part 2) ───────────────────────────────────────

HOST_NIX = os.path.join(REPO, "nixos", "modules", "hart-layer-shell-host.nix")


def _host_python():
    """The program passed to `python -c` in the GTK4 host wrapper."""
    with open(HOST_NIX, encoding="utf-8") as fh:
        src = fh.read()
    lines = src.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("import gi, os"))
    end = next(i for i, l in enumerate(lines[start:], start)
               if l.strip() == "app.run(None)")
    return "\n".join(lines[start:end + 1])


def test_the_embedded_python_has_no_double_quote_or_backslash():
    """THE build-breaker, stated as its own rule.

    That program is passed to `python -c "<body>"` — a Nix DOUBLE-quoted string.
    A single `"` anywhere in it, INCLUDING inside a Python comment, ends the Nix
    string early and no image builds at all. A backslash is consumed as a Nix
    escape before Python ever sees it, so `\n` silently becomes a real newline.

    test_gtk4_host_python_py_compiles already fails when this happens, but it
    reports "extraction failed — body did not contain the host class", which
    reads like a broken test rather than broken source. This says what the rule
    actually is.
    """
    body = _host_python()
    bad_q = [l for l in body.split("\n") if '"' in l]
    assert not bad_q, (
        "double quote inside the `python -c` program (ends the Nix string "
        "early; use single quotes, even in comments): %r" % bad_q[:3])
    bad_b = [l for l in body.split("\n") if "\\" in l]
    assert not bad_b, (
        "backslash inside the `python -c` program (Nix eats it as an escape "
        "before Python sees it; use chr(10) etc.): %r" % bad_b[:3])


def test_the_host_reads_the_same_verdict_as_the_shell():
    """One publisher, several consumers. A second path here would let the host
    and the served shell disagree about who is painting the backdrop, which
    shows up as either a double orb or no background at all."""
    body = _host_python()
    assert "/run/hart/session/native-chrome" in body, (
        "the GTK4 host must read the SAME verdict file as the served shell")
    assert "_native_chrome_claimed" in body


def test_the_host_makes_both_layers_transparent():
    """The WebView paints an opaque page background AND the GTK4 window paints
    its own themed background behind it. Clearing only one leaves the other
    covering the compositor, which is the whole point of the bridge."""
    body = _host_python()
    assert "set_background_color" in body, "the WebView background must be cleared"
    assert "background: transparent" in body, (
        "the GTK4 window background must be cleared too")


def test_the_host_stays_opaque_when_nothing_is_claimed():
    body = _host_python()
    i = body.index("_claimed = _native_chrome_claimed()")
    window = body[i: i + 400]
    assert "if _claimed:" in window, (
        "transparency must be gated on the compositor's claim; unconditional "
        "transparency with no native backdrop is a black desktop")


def test_transparency_failure_never_kills_the_session():
    body = _host_python()
    i = body.index("set_background_color")
    window = body[max(0, i - 400): i + 900]
    assert "except Exception" in window, (
        "a cosmetic handoff must not be able to take the session down; an "
        "opaque shell that paints beats a transparent one that crashed")


# ── the compositor half (bridge part 4): the claim must be EARNED ────────────

COMP_SRC = os.path.join(REPO, "compositor", "src")


def _rust(name):
    with open(os.path.join(COMP_SRC, name), encoding="utf-8") as fh:
        src = fh.read()
    import re as _re
    src = _re.sub(r"/\*.*?\*/", "", src, flags=_re.S)
    return "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("//"))


def test_the_claim_is_published_from_the_flip_path_not_config():
    """THE contract. 'A flag is set' is a promise; 'this element was in the frame
    that reached the screen' is evidence. Only the second is safe to act on,
    because acting wrongly yields a desktop with no background and the paint
    watchdog does not catch wrong-looking desktops."""
    udev = _rust("udev.rs")
    import re as _re
    # The CALL site, not the `fn publish_native_chrome()` definition.
    calls = [m.start() for m in _re.finditer(r"(?<!fn )publish_native_chrome\(\)", udev)]
    assert calls, "publish_native_chrome is never called"
    i = calls[0]
    window = udev[max(0, i - 500): i]
    assert "last_flip_at = Some(now)" in window, (
        "the claim must be published where the frame is recorded as presented, "
        "not from configuration or at startup")


def test_a_failed_element_import_does_not_claim_it():
    """A buffer that failed to import was never drawn. Claiming it anyway would
    make the shell hide its own copy of something nobody is painting."""
    core = _rust("comp_core.rs")
    for elem, mask in (("orb", "NATIVE_CHROME_ORB"),
                       ("bloom", "NATIVE_CHROME_BLOOM")):
        i = core.index("%s: failed to import" % elem)
        # The mask must be set in the Ok arm ABOVE, never in/after the Err arm.
        after_err = core[i: i + 300]
        assert mask not in after_err, (
            "%s sets its claim mask on the failure path" % elem)


def test_the_claim_only_grows_within_a_session():
    """The shell re-reads this on every render. Retracting a claim mid-session
    would flicker the desktop between native and HTML chrome."""
    udev = _rust("udev.rs")
    i = udev.index("fn publish_native_chrome")
    body = udev[i: i + 2200]
    assert "prev | mask" in body, "the published claim must only ever grow"
    assert "if next == prev" in body, "an unchanged claim must not be rewritten"


def test_the_write_is_atomic():
    """The shell polls this file on every render; a torn read would flicker."""
    udev = _rust("udev.rs")
    i = udev.index("fn publish_native_chrome")
    body = udev[i: i + 2200]
    assert "rename" in body, (
        "write-then-rename, or a reader can observe a half-written claim")


def test_the_claim_is_cleared_at_session_start():
    """The dangerous case: hart-comp claims, then the ladder drops to sway/cage
    where NOTHING draws the native backdrop. A stale claim would leave the shell
    transparent over nothing."""
    with open(os.path.join(REPO, "nixos", "modules", "hart-comp.nix"),
              encoding="utf-8") as fh:
        nix = fh.read()
    i = nix.index('writeShellScriptBin "hart-comp-session"')
    head = nix[i: i + 1600]
    assert "rm -f /run/hart/session/native-chrome" in head, (
        "every session must start claiming nothing; hart-comp re-earns the "
        "claim by presenting a frame")
