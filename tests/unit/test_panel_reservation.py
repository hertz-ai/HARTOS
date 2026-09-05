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

import json
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


SCENE_SRC = os.path.join(REPO, "compositor", "src", "scene.rs")


def _rust_const_px(name):
    """The value of a `pub const NAME: f32 = N.0;` in the native scene."""
    src = open(SCENE_SRC, encoding="utf-8").read()
    m = re.search(r"pub const %s:\s*f32\s*=\s*([0-9]+)\.?[0-9]*\s*;" % name, src)
    assert m, "%s not found in scene.rs" % name
    return int(m.group(1))


def test_the_native_scene_draws_the_same_strips_the_shell_reserves(published):
    """THE DRIFT CLASS AGAIN, in the language the guard above cannot see.

    The native compositor scene paints its own top bar and taskbar. The shell
    meanwhile publishes the reservation, and every window-placement path
    subtracts THAT. So the moment the two disagree, the native bar and the space
    reserved for it are different sizes: either a dead band of desktop no window
    may use, or windows tucked under a bar that is drawing over them, which is
    the exact 2026-08-29 report this whole contract exists to prevent.

    The two halves are no longer the same KIND of thing, which is the point:

      TOP: both sides now read `shell.topbar_height` out of the active theme, so
      they cannot drift by construction. scene.rs's TOP_BAR_H is the FALLBACK for
      an unreadable theme, and must equal the fallback theme_service publishes for
      the same case. Four of the ten shipped themes move this number, so a fixed
      Rust constant was a live bug, not a hypothetical one.

      BOTTOM: the theme has no key for the taskbar. It is a Python constant beside
      a CSS literal, so it CAN drift, and this is still the only thing stopping it.
    """
    r = L.publish_panel_reservation(":root{--hart-topbar-height:40px}")
    assert _rust_const_px("TOP_BAR_H") == r["top"], (
        "scene.rs TOP_BAR_H and the published top fallback have drifted")
    assert _rust_const_px("TASKBAR_H") == r["bottom"], (
        "scene.rs TASKBAR_H and the published bottom reservation have drifted")


def test_the_native_bar_reads_the_same_theme_key_the_shell_publishes_from():
    """The half that a constant comparison cannot cover.

    theme_service emits `--hart-topbar-height` from `shell.topbar_height`, the
    shell publishes the reservation from that variable, and the native scene now
    sizes its bar from the SAME key rather than a constant that happened to agree.
    Assert both readers by name, since agreeing today is what a hardcoded 40 also
    did.
    """
    theme_src = open(os.path.join(REPO, "integrations", "agent_engine",
                                  "theme_service.py"), encoding="utf-8").read()
    comp = open(os.path.join(REPO, "compositor", "src", "comp_core.rs"),
                encoding="utf-8").read()
    for key, var in (("topbar_height", "--hart-topbar-height"),
                     ("icon_size", "--hart-icon-size"),
                     ("border_radius", "--hart-radius")):
        assert re.search(r"%s:.*shell\.get\(\"%s\"" % (re.escape(var), key),
                         theme_src), (
            "theme_service no longer emits %s from shell.%s" % (var, key))
        assert 'file.num("%s")' % key in comp, (
            "the native scene no longer reads shell.%s, so it is back to a "
            "constant the theme can move out from under it" % key)


def test_no_shipped_theme_is_clamped_by_the_native_scene():
    """The compositor clamps these because they arrive from a file, and a zero
    bar would invert the content band's arithmetic. The bounds have to be wide
    enough that no real theme is silently altered, or the clamp becomes its own
    drift: the browser would render the theme's number and the native scene a
    different one.
    """
    import glob
    # Read the bounds OUT of the Rust rather than restating them here. A copy would
    # let someone tighten the clamp and leave this passing, which is the exact
    # duplicate-number failure every other guard in this file exists to stop.
    scene = open(SCENE_SRC, encoding="utf-8").read()
    fields = {"topbar_height": "top_bar_h", "icon_size": "icon_px",
              "border_radius": "card_radius"}
    bounds = {}
    for key, field in fields.items():
        m = re.search(r"self\.%s = \w+\.clamp\(([0-9.]+), ([0-9.]+)\)"
                      % re.escape(field), scene)
        assert m, "scene.rs no longer clamps %s, so its bounds cannot be read" % field
        bounds[key] = (float(m.group(1)), float(m.group(2)))
    for path in sorted(glob.glob(os.path.join(
            REPO, "nixos", "assets", "conky-themes", "*.json"))):
        shell = json.load(open(path, encoding="utf-8")).get("shell", {})
        for key, (lo, hi) in bounds.items():
            if key not in shell:
                continue
            v = shell[key]
            assert lo <= v <= hi, (
                "%s sets %s=%s, which the native scene clamps to [%s, %s]: the "
                "browser would draw the theme's number and the compositor a "
                "different one" % (os.path.basename(path), key, v, lo, hi))


def _css_decl(css, selector, prop):
    """The value of `prop` in the LAST rule matching `selector` (cascade order)."""
    found = None
    # `^\s*` so a rule nested inside a media block is found too; it is the same
    # selector at the same specificity, just indented.
    for m in re.finditer(r"(?m)^\s*%s\s*\{(.*?)\}" % re.escape(selector), css, re.S):
        d = re.search(r"(?<![-\w])%s:\s*([^;]+);" % re.escape(prop), m.group(1))
        if d:
            found = d.group(1).strip()
    return found


def _css_outside_media(css):
    """The stylesheet with every @media block removed: the BASE cascade.

    A base lookup has to ignore the overrides, or `.hh-amount` resolves to the
    58px a short screen gets and the pin silently checks the wrong number.
    """
    return re.sub(r"@media[^{]*\{.*?\n\}", "", css, flags=re.S)


def test_the_native_home_is_laid_out_at_the_shells_own_scale():
    """The whole native desktop was drawn at about two thirds of the shell's size.

    Every constant in scene.rs that carried a CSS citation was right; every one that
    did not was a first-cut guess, and nothing could see the difference. The hero
    figure was 40px against the shell's 88, the row headings 15 against 23, the
    cards 210x128 against 258x150. Laid side by side at M6 that is not the same
    desktop, and no Rust test could catch it because they all pin RELATIONSHIPS
    (the note follows the label, the See-all clears it) rather than sizes.

    So pin the sizes here, where both languages are readable at once. The values
    the shell makes responsive are pinned as the literals in HomeMetrics; the rest
    as plain consts.
    """
    css = open(os.path.join(REPO, "integrations", "agent_engine", "static",
                            "hartHome.css"), encoding="utf-8").read()
    base = _css_outside_media(css)
    scene = open(SCENE_SRC, encoding="utf-8").read()

    def px(value):
        m = re.search(r"(\d+(?:\.\d+)?)px", value or "")
        assert m, "not a px value: %r" % (value,)
        return float(m.group(1))

    def rust_const(name):
        m = re.search(r"const %s: f32 = ([0-9.]+);" % name, scene)
        assert m, "%s is no longer a plain literal in scene.rs" % name
        return float(m.group(1))

    # ── the fixed scale: one CSS declaration, one Rust const ──
    for const, selector, prop in [
        ("HERO_EYEBROW_PX", ".hh-eyebrow", "font-size"),
        ("HERO_META_PX", ".hh-hero-meta", "font-size"),
        ("HERO_BTN_PX", ".hh-btn", "font-size"),
        ("ROW_LABEL_PX", ".hh-row-title", "font-size"),
        ("ROW_NOTE_PX", ".hh-row-note", "font-size"),
        ("CARD_TITLE_PX", ".hh-card-title", "font-size"),
        ("CARD_META_PX", ".hh-card-meta", "font-size"),
        ("CARD_W", ".hh-card", "width"),
        ("CARD_PROG_H", ".hh-card-prog", "height"),
        ("CARD_ICON_BOX", ".hh-card-ic", "width"),
        ("RANK_PX", ".hh-rank-num", "font-size"),
    ]:
        want = _css_decl(base, selector, prop)
        assert want, "hartHome.css no longer declares %s on %s" % (prop, selector)
        assert rust_const(const) == px(want), (
            "%s is %s but %s { %s } is %s"
            % (const, rust_const(const), selector, prop, want))

    # ── the TOP BAR's cluster, whose CSS lives in the service's inline sheet
    #    rather than hartHome.css: the wordmark rides `.start-btn`'s size, the
    #    tray glyph rides the theme's icon-size variable. All four of these were
    #    wrong (28/15/18/28 against 30/13/20/30) and nothing could see it.
    service = open(SERVICE_SRC, encoding="utf-8").read()
    for const, want in [
        ("ORB_SM", _css_decl(base, ".top-bar-orb", "width")),
        ("AVATAR_D", _css_decl(base, ".top-bar-avatar", "width")),
        ("AVATAR_PX", _css_decl(base, ".top-bar-avatar", "font-size")),
        ("TAB_PX", _css_decl(base, ".tb-tab", "font-size")),
        ("KBD_PX", _css_decl(base, ".top-bar-omni .tbo-kbd", "font-size")),
        ("CARD_ICON_PX", _css_decl(base, ".hh-card-ic .mi", "font-size")),
        ("CARD_CHIP_PX", _css_decl(base, ".hh-card-badge", "font-size")),
    ]:
        assert want, "the home CSS no longer declares the source of %s" % const
        assert rust_const(const) == px(want), (
            "%s is %s but the shell's is %s" % (const, rust_const(const), want))

    # The wordmark takes the bar's own start-btn size, not the hero's.
    startbtn = re.search(r"\.top-bar \.start-btn\{\{[^}]*?font-size:(\d+)px", service, re.S)
    assert startbtn, "the service no longer sizes .top-bar .start-btn"
    assert rust_const("WORDMARK_PX") == float(startbtn.group(1)), (
        "the native wordmark is %s but .start-btn is %spx"
        % (rust_const("WORDMARK_PX"), startbtn.group(1)))

    # The tray button, its gap, and the glyph inside it (a theme variable with a
    # default the service and theme_service must agree on, so read the default).
    traybtn = re.search(r"\.tray-btn\{\{width:(\d+)px", service)
    assert traybtn and rust_const("TRAY_BTN") == float(traybtn.group(1)), (
        "the native tray button drifted from .tray-btn")
    traygap = re.search(r"\.top-bar-right\{\{[^}]*?gap:(\d+)px", service, re.S)
    assert traygap and rust_const("TRAY_GAP") == float(traygap.group(1)), (
        "the native tray gap drifted from .top-bar-right")
    iconsize = re.search(r"--hart-icon-size:\s*(\d+)px", service)
    assert iconsize, "the service no longer defaults --hart-icon-size"
    assert rust_const("TRAY_PX") == float(iconsize.group(1)), (
        "the native tray glyph is %s but --hart-icon-size defaults to %s"
        % (rust_const("TRAY_PX"), iconsize.group(1)))

    # ── the RESPONSIVE four, pinned as the literals HomeMetrics carries ──
    metrics = re.search(r"fn for_output\(.*?\n    \}", scene, re.S)
    assert metrics, "HomeMetrics::for_output is no longer a readable block"
    metrics = metrics.group(0)
    base_gutter = px(re.search(r"--hh-gutter:\s*([^;]+);", base).group(1))
    assert re.search(r"gutter: %s," % base_gutter, metrics), (
        "the base gutter drifted from --hh-gutter (%s)" % base_gutter)
    assert re.search(r"amount_px: %s," % px(_css_decl(base, ".hh-amount", "font-size")),
                     metrics), "the base hero figure drifted from .hh-amount"
    assert re.search(r"unit_px: %s," % px(_css_decl(base, ".hh-amount-unit", "font-size")),
                     metrics), "the hero unit drifted from .hh-amount-unit"
    assert re.search(r"card_h: %s," % px(_css_decl(base, ".hh-card", "height")),
                     metrics), "the base card height drifted from .hh-card"

    # EVERY media block in the home CSS, matched by what it declares rather than by
    # position, and each one's overrides. Four blocks: two scale the content, two
    # reshape the bar. The compositor must carry all four or it is a partial port.
    blocks = re.findall(r"@media \((max-width|max-height): (\d+)px\)\s*\{(.*?)\n\}",
                        css, re.S)
    assert len(blocks) >= 4, "expected the home CSS's four sizing media blocks"
    seen = 0
    for axis_css, bound, block in blocks:
        axis = "output_w" if axis_css == "max-width" else "output_h"
        overrides = [
            ("amount_px", ".hh-amount", "font-size"),
            ("unit_px", ".hh-amount-unit", "font-size"),
            ("card_h", ".hh-card", "height"),
            ("tab_pad_x", ".tb-tab", "padding"),
            ("omnibox_min_w", ".top-bar-omni", "min-width"),
        ]
        wanted = [(f, _css_decl(block, sel, prop)) for f, sel, prop in overrides]
        wanted = [(f, v) for f, v in wanted if v is not None]
        gutter = re.search(r"--hh-gutter:\s*([^;]+);", block)
        if gutter:
            wanted.append(("gutter", gutter.group(1)))
        hides_kbd = ".tbo-kbd" in block and "display: none" in block
        hides_tabs = 'data-tab="earn"' in block
        if not (wanted or hides_kbd or hides_tabs):
            continue
        seen += 1
        assert re.search(r"if %s <= %s\.0 \{" % (axis, bound), metrics), (
            "scene.rs carries no %s <= %s branch for the block that sets %s"
            % (axis, bound, [f for f, _ in wanted] or "the bar's shape"))
        for field, value in wanted:
            assert re.search(r"m\.%s = %s;" % (field, px(value)), metrics), (
                "%s under %s:%s should be %s" % (field, axis_css, bound, value))
        if hides_kbd:
            assert "m.show_kbd = false;" in metrics, (
                "the shortcut hint is hidden at %s but the scene still draws it" % bound)
        if hides_tabs:
            assert "m.nav_tabs = NAV_TABS.len() - 2;" in metrics, (
                "two tabs are hidden at %s but the scene still draws five" % bound)
    assert seen == 4, "matched %d sizing media blocks, expected 4" % seen


def test_the_native_card_art_is_the_shells_own_brand_gradient():
    """THE SAME DRIFT CLASS, across the same language boundary, one layer in.

    Every card on the home desktop is painted as a brand-spectrum hue darkened
    toward ink across two stops. hartBrandArt.js is the single source of that
    art language for the shell, and its own header says why it exists: the home
    cards and the desktop icons had each grown a copy, with different ink,
    different darkening and a different hue order, and they drifted apart.

    The native compositor scene now paints the same tiles from Rust constants,
    which is a THIRD copy in a language neither that module nor any JS test can
    see. So pin it: the ink, both blend factors and the angle list must be the
    literals hartBrandArt.js uses, and the ranked card's art box must be the
    width hartHome.css gives `.hh-rank-inner`. A card that reads darker, flatter
    or differently angled than the shell's is the exact failure this catches,
    and it is invisible to every other test in the tree.
    """
    brand = open(os.path.join(REPO, "integrations", "agent_engine", "static",
                              "hartBrandArt.js"), encoding="utf-8").read()
    home_css = open(os.path.join(REPO, "integrations", "agent_engine", "static",
                                 "hartHome.css"), encoding="utf-8").read()
    scene = open(SCENE_SRC, encoding="utf-8").read()

    ink = re.search(r"var INK = \[(\d+), (\d+), (\d+)\]", brand)
    assert ink, "hartBrandArt.js no longer declares INK as a literal triple"
    rust_ink = re.search(
        r"const ART_INK: Color = Color::rgba\("
        r"(\d+)\.0 / 255\.0, (\d+)\.0 / 255\.0, (\d+)\.0 / 255\.0", scene)
    assert rust_ink, "scene.rs no longer declares ART_INK from 0..255 literals"
    assert rust_ink.groups() == ink.groups(), (
        "the native art ink and hartBrandArt's INK have drifted: "
        "%s vs %s" % (rust_ink.groups(), ink.groups()))

    # The two darkening factors, named in the shell by which stop they make.
    dark = re.search(r"var dark = blend\(base, INK, ([0-9.]+)\)", brand)
    light = re.search(r"var light = blend\(second, INK, ([0-9.]+)\)", brand)
    assert dark and light, "hartBrandArt.js gradient() no longer blends two stops"
    assert re.search(r"base\.mix\(ART_INK, %s\)" % re.escape(dark.group(1)), scene), (
        "the native DARK stop no longer uses the shell's %s" % dark.group(1))
    assert re.search(r"second\.mix\(ART_INK, %s\)" % re.escape(light.group(1)), scene), (
        "the native LIGHT stop no longer uses the shell's %s" % light.group(1))

    angles = re.search(r"var ang = \[(\d+), (\d+), (\d+)\]", brand)
    assert angles, "hartBrandArt.js no longer picks from three literal angles"
    rust_angles = re.search(
        r"const ART_ANGLES: \[f32; 3\] = \[([0-9.]+), ([0-9.]+), ([0-9.]+)\]", scene)
    assert rust_angles, "scene.rs no longer declares ART_ANGLES"
    assert [float(a) for a in rust_angles.groups()] ==         [float(a) for a in angles.groups()], (
        "the native gradient angles and the shell's have drifted")

    # `.hh-card.hh-ranked .hh-rank-inner { width: 174px }`: the art box of a
    # leaderboard card, and the box its title, chip and progress bar sit in.
    inner = re.search(r"\.hh-rank-inner\s*\{[^}]*?width:\s*(\d+)px", home_css,
                      re.S)
    assert inner, "hartHome.css no longer sizes .hh-rank-inner"
    rust_inner = re.search(r"const RANK_INNER_W: f32 = ([0-9.]+);", scene)
    assert rust_inner, "scene.rs no longer declares RANK_INNER_W"
    assert float(rust_inner.group(1)) == float(inner.group(1)), (
        "the ranked card's native art box is %s but the shell's is %s"
        % (rust_inner.group(1), inner.group(1)))


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
