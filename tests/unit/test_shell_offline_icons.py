"""The glass shell must render its icons OFFLINE (#109).

Every shell icon is <span class="mi material-icons-round">name</span>. Before this
fix the icon font family was defined ONLY by the Google <link> in <head>, so a
fresh offline ISO boot showed literal ligature words ("lock","notifications")
instead of glyphs. The shell now defines the icon font LOCALLY with the `liga`
feature + a fallback onto the bundled material-icons/material-symbols fonts.

These drive the REAL render_desktop_shell() and assert its output contract:
icons are offline-renderable, AND the online Google round-icon family is left
first/intact (zero online regression). Behavioural — it calls the real render
method and checks observable output, not source text.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.agent_engine.liquid_ui_service import LiquidUIService


def _render():
    return LiquidUIService().render_desktop_shell()


def test_icon_font_defined_locally_with_ligatures():
    html = _render()
    # A LOCAL selector + the ligature feature = icons render from a local font
    # without the Google stylesheet (the offline case).
    assert '.mi, .material-icons-round {' in html
    assert "font-feature-settings: 'liga'" in html


def test_icon_font_falls_back_to_bundled_families():
    html = _render()
    # The fallback stack reaches the locally-bundled fonts.
    assert "'Material Icons Round', 'Material Icons'" in html
    assert 'Material Symbols' in html


def test_online_render_unchanged_google_link_kept():
    html = _render()
    # 'Material Icons Round' stays FIRST in the stack and the Google <link> is
    # kept, so the online round-icon render is unchanged (no online regression).
    assert 'fonts.googleapis.com/icon?family=Material+Icons+Round' in html


def test_shell_still_renders_the_icons():
    html = _render()
    # Sanity: the structure that uses the icons is intact and the doc is real.
    assert 'class="mi material-icons-round">notifications' in html
    assert len(html) > 50000
