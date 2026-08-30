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

@pytest.fixture(scope="module")
def shell_html():
    """The REAL served document, not the source file."""
    return L.LiquidUIService().render_desktop_shell()


def _css_px(html, pattern):
    m = re.search(pattern, html)
    assert m, "pattern not found in the SERVED shell CSS: %s" % pattern
    return int(m.group(1))


def test_the_page_lays_itself_out_with_the_height_the_compositor_reserves(
        shell_html, published):
    """The reservation the compositor is given must equal the height the browser
    actually paints, or a maximized window sits on top of the bar (#52).

    Parsed from the RENDERED page rather than the source file: the taskbar
    height is now DERIVED from TASKBAR_HEIGHT_PX via --hart-taskbar-height, so
    this proves the derivation reaches the browser, and it keeps working if the
    rule moves out of the f-string entirely.
    """
    served = _css_px(shell_html, r"--hart-taskbar-height:\s*(\d+)px")
    reservation = L.publish_panel_reservation(":root{--hart-topbar-height:40px}")
    assert reservation["bottom"] == served == L.TASKBAR_HEIGHT_PX
    assert "bottom=%d" % served in published.read_text()


def test_no_layout_rule_hardcodes_the_bar_heights(shell_html):
    """THE DRIFT CLASS. The taskbar height was written as a literal 44 at five
    sites (three CSS rules and two JS clamps) while the CSS read the top bar
    from the theme -- so a theme with a taller bar mis-snapped every panel and
    could drop a dragged window under the bar where it could not be grabbed.
    Every layout site must now read the vars.
    """
    for pattern, what in (
        (r"\.hart-desktop\s*\{[^}]*?bottom:\s*44px", ".hart-desktop"),
        (r"\.panel-container\s*\{[^}]*?bottom:\s*44px", ".panel-container"),
        (r"\.taskbar\s*\{[^}]*?height:\s*44px", ".taskbar"),
        (r"const\s+topH\s*=\s*40", "snapPanel topH"),
        (r"const\s+taskH\s*=\s*44", "snapPanel taskH"),
        (r"TOP\s*=\s*40\s*,", "drag-clamp TOP"),
    ):
        assert not re.search(pattern, shell_html), (
            "%s still hardcodes a bar height instead of reading the CSS var"
            % what)
    assert shell_html.count("hartBarPx(") >= 5, (
        "the JS layout sites should read the live bar heights via hartBarPx")


def test_a_failed_theme_load_still_reserves_the_bar_it_actually_paints(
        published, monkeypatch):
    """Replaces the fallback-constant grep AND the source index-ordering check.
    Force the real theme-load failure, render the real fallback page, and assert
    the published reservation matches the CSS THAT page carries. If the publish
    moved above the fallback, top would be the theme's value and this fails.
    """
    from integrations.agent_engine.theme_service import ThemeService

    def boom(*_a, **_kw):
        raise RuntimeError("theme store unreadable")

    monkeypatch.setattr(ThemeService, "get_css_variables", staticmethod(boom))
    html = L.LiquidUIService().render_desktop_shell()

    served_top = _css_px(html, r"--hart-topbar-height:\s*(\d+)px")
    served_bottom = _css_px(html, r"--hart-taskbar-height:\s*(\d+)px")
    assert served_top == L.TOPBAR_HEIGHT_FALLBACK_PX
    assert sorted(l for l in published.read_text().splitlines() if l.strip()) \
        == ["bottom=%d" % served_bottom, "top=%d" % served_top]


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


def test_rendering_the_shell_publishes_the_reservation_it_served(published):
    """The honest form of "the render path publishes": render with the target
    redirected into tmp and assert the file LANDED, carrying the same numbers
    the page carries. The old version asserted that a call expression appeared
    in the source text and that its byte offset was greater than another
    string's -- which a refactor breaks and a broken value passes."""
    assert not published.exists()
    html = L.LiquidUIService().render_desktop_shell()
    assert published.exists(), (
        "render_desktop_shell() produced a page but published no reservation")
    got = dict(
        (k, int(v)) for k, _, v in
        (l.partition("=") for l in published.read_text().splitlines()
         if l.strip()))
    assert got["top"] == _css_px(html, r"--hart-topbar-height:\s*(\d+)px")
    assert got["bottom"] == _css_px(html, r"--hart-taskbar-height:\s*(\d+)px")
