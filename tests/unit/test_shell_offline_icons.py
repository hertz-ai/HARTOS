"""The glass shell must render its icons OFFLINE (#109) — now via a BUNDLED font.

Every shell icon is <span class="mi material-icons-round">name</span>. The icon
font family used to be defined ONLY by the Google <link> in <head>, so a fresh
offline boot showed literal ligature words ("lock","notifications") — and the
frozen Win/macOS desktop (no Material font at all) showed ZERO icons. The shell
now ships its OWN icon font (integrations/agent_engine/static/
MaterialSymbolsRounded.woff2 — static filled Material Symbols Rounded, every
glyph incl. smart_toy/shield) loaded via an @font-face from /shell/static, so
every glyph renders fully offline on every OS. The Google <link> is kept as a
progressive-enhancement only.

These drive the REAL render_desktop_shell() and assert its output contract.
Behavioural — it calls the real render method and checks observable output.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.agent_engine.liquid_ui_service import LiquidUIService

STATIC_DIR = os.path.join(ROOT, 'integrations', 'agent_engine', 'static')
FONT_PATH = os.path.join(STATIC_DIR, 'MaterialSymbolsRounded.woff2')


def _render():
    return LiquidUIService().render_desktop_shell()


def test_icon_font_defined_locally_with_ligatures():
    html = _render()
    # A LOCAL selector + the ligature feature = icons render from a local font
    # without the Google stylesheet (the offline case).
    assert '.mi, .material-icons-round {' in html
    assert "font-feature-settings: 'liga'" in html


def test_bundled_font_face_points_at_local_woff2():
    html = _render()
    # The shell defines its OWN @font-face served from /shell/static — this is
    # what makes icons render offline on a box with no Material OS font.
    assert '@font-face' in html
    assert '/shell/static/MaterialSymbolsRounded.woff2' in html
    assert "format('woff2')" in html


def test_material_symbols_is_first_in_the_family_stack():
    html = _render()
    # The bundled Material Symbols Rounded must be FIRST so it (not a missing OS
    # font) renders, and it carries the newer glyphs the legacy round font lacks.
    fam = "'Material Symbols Rounded', 'Material Icons Round', 'Material Icons'"
    assert fam in html


def test_online_google_link_kept_as_progressive_enhancement():
    html = _render()
    # The Google <link> stays (online round variant) — but it is no longer the
    # only source of the family, so offline is fully covered by the bundle.
    assert 'fonts.googleapis.com/icon?family=Material+Icons+Round' in html


def test_bundled_font_file_exists_and_covers_required_glyphs():
    # The actual font binary must ship AND contain the ligatures the manifest
    # uses — smart_toy/shield were absent from the legacy round font.
    assert os.path.isfile(FONT_PATH), f"missing bundled icon font: {FONT_PATH}"
    assert os.path.getsize(FONT_PATH) > 100_000  # a real, glyph-complete font
    try:
        from fontTools.ttLib import TTFont
    except Exception:
        return  # fontTools is a build-time dep; skip the deep check if absent
    f = TTFont(FONT_PATH)
    assert f.flavor == 'woff2'
    assert 'GSUB' in f  # ligature table — turns names into glyphs
    glyphs = set(f.getGlyphOrder())
    for name in ('smart_toy', 'shield', 'rss_feed', 'palette', 'terminal'):
        assert name in glyphs, f"bundled font missing glyph: {name}"


def test_shell_still_renders_the_icons():
    html = _render()
    # Sanity: the structure that uses the icons is intact and the doc is real.
    # (Resilient to a11y attributes like aria-hidden on the icon span.)
    assert 'material-icons-round' in html and 'notifications</span>' in html
    assert len(html) > 50000
