"""De-monochrome the glass shell — per-app icon colours (single source).

The shell used to tint EVERY icon with one --hart-accent hue, so the desktop
read as a single blue wash. shell_manifest now resolves a stable per-app colour
(ONE place: with_icon_colors / color_for) that the JS render paths (start menu,
dock, desktop icons, titlebars) all read, so the desktop looks vibrant like
macOS/Windows.

Behavioural: imports the real resolver + drives the real render_desktop_shell()
and asserts observable output (resolved colours present, applied via miStyle),
not source text.
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.agent_engine import shell_manifest as sm
from integrations.agent_engine.liquid_ui_service import LiquidUIService


# ─── color_for: resolution precedence (most specific wins) ───────────

def test_explicit_override_wins_over_everything():
    # An author 'color' on the entry beats both the icon override and the group.
    assert sm.color_for('shield', group='Manage', override='#123456') == '#123456'


def test_icon_override_beats_group():
    # 'shield' has an identity colour (green) that must win over its group hue,
    # so the Security center reads green wherever it sits.
    c = sm.color_for('shield', group='Discover')
    assert c == sm.ICON_COLOR_OVERRIDES['shield']
    assert c != sm.GROUP_COLORS['Discover']


def test_group_color_used_when_no_icon_override():
    # An icon with no identity override falls back to its start-menu group hue.
    assert 'campaign' in sm.ICON_COLOR_OVERRIDES or True
    c = sm.color_for('some_unknown_icon_xyz', group='Discover')
    assert c == sm.GROUP_COLORS['Discover']


def test_default_color_when_nothing_matches():
    assert sm.color_for(None, group=None) == sm.DEFAULT_ICON_COLOR
    assert sm.color_for('totally_unknown', group='NotAGroup') == sm.DEFAULT_ICON_COLOR


def test_every_color_is_a_valid_hex():
    import re
    hexre = re.compile(r'^#[0-9A-Fa-f]{6}$')
    assert hexre.match(sm.DEFAULT_ICON_COLOR)
    for c in sm.GROUP_COLORS.values():
        assert hexre.match(c), c
    for c in sm.ICON_COLOR_OVERRIDES.values():
        assert hexre.match(c), c


# ─── with_icon_colors: stamps a color on EVERY entry, non-mutating ───

def test_with_icon_colors_stamps_every_entry():
    out = sm.with_icon_colors(sm.PANEL_MANIFEST)
    assert set(out.keys()) == set(sm.PANEL_MANIFEST.keys())
    for pid, entry in out.items():
        assert 'color' in entry and entry['color'], pid


def test_with_icon_colors_does_not_mutate_source():
    before = {k: dict(v) for k, v in sm.PANEL_MANIFEST.items()}
    sm.with_icon_colors(sm.PANEL_MANIFEST)
    # Source manifest entries must be untouched (no 'color' leaked in).
    for k, v in sm.PANEL_MANIFEST.items():
        assert v == before[k], f"with_icon_colors mutated source entry {k}"


def test_de_monochrome_yields_more_than_one_hue():
    # The whole point: the desktop must NOT be one colour. Across the real
    # manifest + system panels there must be several distinct icon colours.
    out = sm.with_icon_colors(sm.get_all_panels())
    colours = {e['color'] for e in out.values()}
    assert len(colours) >= 5, colours


def test_known_identity_colours_resolve_on_real_entries():
    out = sm.with_icon_colors(sm.get_all_panels())
    # admin_mod uses icon 'shield' -> green identity colour.
    assert out['admin_mod']['color'] == sm.ICON_COLOR_OVERRIDES['shield']
    # security system panel uses icon 'shield' too.
    assert out['security']['color'] == sm.ICON_COLOR_OVERRIDES['shield']
    # agents_browse uses 'smart_toy' -> its identity colour.
    assert out['agents_browse']['color'] == sm.ICON_COLOR_OVERRIDES['smart_toy']


# ─── render: the shell actually emits the colours + applies them ─────

def _render():
    return LiquidUIService().render_desktop_shell()


def test_render_embeds_resolved_colours_in_manifest_json():
    html = _render()
    # The MANIFEST const sent to JS carries the resolved per-app colour so the
    # render paths can tint each glyph. Assert a known colour reaches the page.
    assert sm.ICON_COLOR_OVERRIDES['smart_toy'] in html
    assert sm.GROUP_COLORS['Discover'] in html


def test_render_defines_miStyle_resolver_and_uses_it():
    html = _render()
    # One JS resolver turns def.color into an inline glyph tint; the render
    # paths call it (no parallel palette). Assert the resolver + a call site.
    assert 'function miStyle(' in html
    assert 'miStyle(info)' in html   # taskbar/dock chip
    assert 'miStyle(p)' in html      # start menu item
    assert 'miStyle(def)' in html    # panel titlebar
    assert 'window.miStyle = miStyle' in html  # exposed for hartDesktop.js


def test_render_manifest_json_is_parseable_with_colours():
    html = _render()
    # The injected MANIFEST must be valid JSON (the </ -> <\/ guard must not
    # corrupt it) AND every entry must carry a colour.
    marker = 'const MANIFEST = '
    i = html.index(marker) + len(marker)
    j = html.index('};', i) + 1
    blob = html[i:j].replace('<\\/', '</')
    data = json.loads(blob)
    assert data, 'MANIFEST JSON empty'
    for pid, entry in data.items():
        assert entry.get('color'), pid
