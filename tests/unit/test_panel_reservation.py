"""The shell tells the compositor how much screen its chrome owns.

WHY THIS EXISTS. The HART shell is ONE fullscreen wlr-layer-shell surface on the
Background layer (Z-ORDER MODEL 1 in hart-layer-shell-host.nix: one WebView, so the
shell JS keeps its window.* globals). The top bar and the bottom taskbar are painted
INSIDE it, which means neither is a surface that could claim an exclusive zone of
its own. The compositor therefore had no way to know they were there, and a
maximized window covered both -- reported on the box 2026-08-29 as "the taskbar
should always stay on top", with the bar unreachable behind a full-screen Firefox
and no way back to Home / Agents / Apps.

So the shell publishes the sizes and comp_core.rs `work_area` subtracts them. Every
placement path -- maximize, all nine snap zones, all five tiling layouts, the
new-window cascade -- resolves through that one function.

The pinning test below is the important one. The top value is parsed from the same
--hart-topbar-height the browser applies, so it cannot drift. The BOTTOM is a Python
constant next to a CSS literal, which can, and a silent drift there puts a band of
covered desktop back exactly where it was.

Run:
  pytest tests/unit/test_panel_reservation.py -v
"""

import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from integrations.agent_engine import liquid_ui_service as L  # noqa: E402

SERVICE_SRC = os.path.join(REPO, "integrations", "agent_engine",
                           "liquid_ui_service.py")


@pytest.fixture()
def published(tmp_path, monkeypatch):
    """Redirect the publish target into tmp and hand back a reader."""
    target = tmp_path / "panel-reservation"
    monkeypatch.setattr(L, "_PANEL_RESERVATION_FILE", str(target))
    return target


# ── the drift guard ─────────────────────────────────────────────────────────

def test_taskbar_constant_matches_the_served_css():
    """TASKBAR_HEIGHT_PX must equal the height in the .taskbar CSS rule.

    Nothing else couples them. If someone restyles the taskbar to 52px and this
    constant stays 44, the compositor reserves 8px too little and the bottom of
    every maximized window sits on top of the bar again -- the exact bug, back,
    silently. This test is the coupling.
    """
    src = open(SERVICE_SRC, encoding="utf-8").read()
    # The rule is inside an f-string, so its braces are doubled in the source.
    m = re.search(r"\.taskbar\{\{[^}]*?height:\s*(\d+)px", src)
    assert m, "could not find the .taskbar height rule in the served CSS"
    assert int(m.group(1)) == L.TASKBAR_HEIGHT_PX, (
        "the .taskbar CSS rule says %spx but TASKBAR_HEIGHT_PX is %s; the "
        "compositor would reserve the wrong amount"
        % (m.group(1), L.TASKBAR_HEIGHT_PX))


def test_the_topbar_fallback_matches_the_theme_load_fallback():
    """If the theme fails to load, render_desktop_shell substitutes a hardcoded
    css_vars block. Our fallback has to be the height in THAT block, or a failed
    theme load also mis-reserves."""
    src = open(SERVICE_SRC, encoding="utf-8").read()
    m = re.search(r"--hart-topbar-height:\s*(\d+)px", src)
    assert m and int(m.group(1)) == L.TOPBAR_HEIGHT_FALLBACK_PX


# ── parsing the top value out of the live css_vars ──────────────────────────

def test_top_comes_from_the_css_the_browser_will_apply(published):
    r = L.publish_panel_reservation(
        ":root { --hart-accent: #00E6C3; --hart-topbar-height: 40px; }")
    assert r == {"top": 40, "bottom": L.TASKBAR_HEIGHT_PX}


def test_a_restyled_bar_moves_the_reservation_with_it(published):
    """The whole point of parsing rather than hardcoding: a theme with a taller
    bar must reserve more, with no code change."""
    r = L.publish_panel_reservation(":root { --hart-topbar-height: 64px; }")
    assert r["top"] == 64


def test_whitespace_variants_still_parse(published):
    for css in (":root{--hart-topbar-height:40px}",
                ":root { --hart-topbar-height:   40px ; }",
                ":root {\n  --hart-topbar-height: 40px;\n}"):
        assert L.publish_panel_reservation(css)["top"] == 40


def test_missing_variable_falls_back_rather_than_reserving_nothing(published):
    """A css_vars block with no topbar height still renders a 40px bar, so
    reserving 0 would put the bug back. Fall back to the known height."""
    assert L.publish_panel_reservation(":root { --hart-accent: #00E6C3; }")["top"] \
        == L.TOPBAR_HEIGHT_FALLBACK_PX
    assert L.publish_panel_reservation("")["top"] == L.TOPBAR_HEIGHT_FALLBACK_PX
    assert L.publish_panel_reservation(None)["top"] == L.TOPBAR_HEIGHT_FALLBACK_PX


# ── what actually lands on disk ─────────────────────────────────────────────

def test_the_file_is_written_in_the_format_the_compositor_parses(published):
    """comp_core.rs parse_panel_reservation reads `key=pixels` lines. This asserts
    the two ends of the bridge agree on the wire format."""
    L.publish_panel_reservation(":root { --hart-topbar-height: 40px; }")
    lines = [l for l in published.read_text().splitlines() if l.strip()]
    assert sorted(lines) == ["bottom=44", "top=40"]
    for line in lines:
        key, _, value = line.partition("=")
        assert key in ("top", "bottom")
        assert value.isdigit() and int(value) > 0


def test_no_partial_file_is_ever_visible(published, tmp_path):
    """Write-then-rename. The compositor reads this file at arbitrary moments, and
    a half-written one would parse as a smaller reservation."""
    L.publish_panel_reservation(":root { --hart-topbar-height: 40px; }")
    assert published.exists()
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, "a .tmp file was left behind: %s" % leftovers


def test_republishing_overwrites_rather_than_appends(published):
    L.publish_panel_reservation(":root { --hart-topbar-height: 40px; }")
    L.publish_panel_reservation(":root { --hart-topbar-height: 64px; }")
    lines = [l for l in published.read_text().splitlines() if l.strip()]
    assert sorted(lines) == ["bottom=44", "top=64"]


# ── it must never take the desktop down ─────────────────────────────────────

def test_an_unwritable_target_does_not_raise(monkeypatch):
    """This runs on the desktop render path. A desktop that fails to draw because
    it could not write a hint file would be far worse than an overlapping bar --
    and on a dev box or a node with no /run/hart, this path is the normal one."""
    monkeypatch.setattr(L, "_PANEL_RESERVATION_FILE",
                        "/definitely/not/a/directory/panel-reservation")
    r = L.publish_panel_reservation(":root { --hart-topbar-height: 40px; }")
    assert r == {"top": 40, "bottom": L.TASKBAR_HEIGHT_PX}


def test_a_permission_error_does_not_raise(published, monkeypatch):
    def boom(*_a, **_kw):
        raise PermissionError("nope")
    monkeypatch.setattr("builtins.open", boom)
    L.publish_panel_reservation(":root { --hart-topbar-height: 40px; }")


def test_the_render_path_publishes(monkeypatch):
    """The call has to be wired into render_desktop_shell, not just defined. It
    sits after BOTH the theme branch and the theme-load fallback, so a failed
    theme load still reserves."""
    src = open(SERVICE_SRC, encoding="utf-8").read()
    assert "publish_panel_reservation(css_vars)" in src, (
        "publish_panel_reservation is never called from the render path")
    body = src.split("def render_desktop_shell", 1)[1]
    call = body.index("publish_panel_reservation(css_vars)")
    fallback = body.index("--hart-topbar-height: 40px")
    assert call > fallback, (
        "the publish must come after the theme-load fallback sets css_vars, "
        "or a failed theme load publishes a stale or unset value")
