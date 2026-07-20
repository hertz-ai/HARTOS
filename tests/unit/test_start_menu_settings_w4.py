"""W4 (#118) — Windows-style Start menu + Settings aggregator.

The Start menu must surface EVERY manifest group, a curated Pinned row, and the
power items, be searchable, and open each entry through the single-instance
``openPanel`` reuse path. The Settings panel is NOT a new app: it is a
categorized INDEX (``shell_manifest.get_settings_sections``) whose every tile
opens an EXISTING system/customization panel via ``openPanel``.

These are behavioural tests: they call the real composition helpers
(``get_settings_sections`` / ``get_pinned_panels``) and render the real served
glass shell through the Flask ``test_client`` at ``/`` (the exact bytes the
WebView loads), then assert the composition the shell actually ships — not a
source-shape grep. The JS itself runs in WebKit and cannot execute here, so the
proof is: the injected ``SETTINGS_SECTIONS`` / ``PINNED`` consts equal the real
helpers' output AND every id they reference resolves to a real registered panel,
which is exactly what makes a tile openable via ``openPanel``.
"""
import json
import re

from integrations.agent_engine.liquid_ui_service import LiquidUIService
from integrations.agent_engine.shell_manifest import (
    get_settings_sections, get_pinned_panels, get_all_panels,
    SYSTEM_PANELS, PANEL_MANIFEST, PANEL_GROUPS)


def _served_html():
    """The real served shell HTML from the Flask app (GET /), not a bare render."""
    svc = LiquidUIService()
    app = svc._create_flask_app()
    app.testing = True
    r = app.test_client().get('/')
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _const_array(html, name):
    """Parse an injected ``const <name> = [ ... ];`` JSON array from the shell."""
    m = re.search(r'const ' + name + r' = (\[.*?\]);', html)
    assert m, name + ' array const not injected into the served shell'
    return json.loads(m.group(1))


# ─── Settings aggregator = a composed view of the registry ───────────────────

def test_settings_sections_only_reference_existing_panels():
    """Every id in the Settings composition resolves to a real registered panel
    (no dead surface), and the aggregator never lists itself (no recursion)."""
    known = get_all_panels()
    secs = get_settings_sections()
    assert secs, 'Settings aggregator returned no sections'
    all_ids = [i for s in secs for i in s['ids']]
    assert all_ids, 'Settings aggregator has no tiles'
    for pid in all_ids:
        assert pid in known, 'Settings links a non-existent panel: ' + pid
    assert 'settings' not in all_ids, 'Settings must not aggregate itself'


def test_settings_aggregator_covers_expected_categories_and_panels():
    """The aggregator composes the real customization/system panels under the
    expected categories (the behavioural intent of 'compose the registry')."""
    secs = get_settings_sections()
    titles = [s['title'] for s in secs]
    for cat in ['Personalization', 'Network & Internet', 'Privacy & Security',
                'Apps', 'Time & Language']:
        assert cat in titles, 'missing settings category: ' + cat
    all_ids = {i for s in secs for i in s['ids']}
    # A representative id from several existing panel families must be aggregated.
    for pid in ['appearance', 'wallpaper_manager', 'security', 'app_store',
                'datetime', 'display', 'user_accounts']:
        assert pid in all_ids, 'Settings should aggregate ' + pid


def test_settings_registered_as_manifest_entry_not_bespoke_surface():
    """Settings is registered as a first-class SYSTEM_PANELS manifest entry (so
    openPanel('settings') resolves its definition), not an ad-hoc surface."""
    assert 'settings' in SYSTEM_PANELS
    entry = SYSTEM_PANELS['settings']
    assert entry['title'] == 'Settings'
    assert entry.get('group') == 'System'
    # It is an aggregator: it owns no direct API of its own.
    assert entry.get('apis') == []


# ─── Pinned row ──────────────────────────────────────────────────────────────

def test_pinned_panels_exist_and_include_settings():
    """Every pinned id resolves to a real panel and Settings is pinned."""
    known = get_all_panels()
    pins = get_pinned_panels()
    assert pins, 'no pinned panels'
    for pid in pins:
        assert pid in known, 'pinned a non-existent panel: ' + pid
    assert 'settings' in pins


# ─── Served shell: Start menu completeness ───────────────────────────────────

def test_served_start_menu_renders_every_manifest_group():
    """The served shell injects every PANEL_GROUPS group and builds the start
    menu from the manifest grouped by category."""
    html = _served_html()
    groups = _const_array(html, 'GROUPS')
    assert groups == PANEL_GROUPS
    for g in PANEL_GROUPS:
        assert g in groups
    # The grouped build path + the System section are present.
    assert 'function buildStartMenu()' in html
    assert 'v.group===group' in html
    assert '>System<' in html or "'System'" in html


def test_served_start_menu_is_searchable():
    """The start search input drives filterStart over data-title tiles."""
    html = _served_html()
    assert 'filterStart(this.value)' in html
    assert 'function filterStart(q)' in html
    assert 'id="start-search"' in html


def test_served_start_menu_has_power_items():
    """Lock / Sleep / Restart / Shut Down power actions are in the start footer."""
    html = _served_html()
    for action in ['lock', 'suspend', 'restart', 'shutdown']:
        assert ("shellAction('" + action + "')") in html, 'missing power: ' + action


def test_served_start_menu_has_pinned_row_matching_helper():
    """The injected PINNED const equals the real helper output and the start menu
    renders a Pinned section from it."""
    html = _served_html()
    pinned = _const_array(html, 'PINNED')
    assert pinned == get_pinned_panels()
    assert 'Pinned' in html


# ─── Served shell: Settings panel opens + aggregates ─────────────────────────

def test_served_settings_panel_is_dispatched_and_aggregates_real_panels():
    """The 'settings' panel id opens (dispatched to loadSettingsPanel) and the
    injected SETTINGS_SECTIONS composition equals the helper AND references only
    ids that resolve in the shipped manifest/system registries — i.e. every tile
    is a real, openable panel (openPanel single-instance reuse)."""
    html = _served_html()
    # Dispatch wired: opening 'settings' renders the aggregator natively.
    assert "if(id==='settings') loadSettingsPanel(container);" in html
    assert 'function loadSettingsPanel(el)' in html
    # Tiles open through the canonical reuse path, not a bespoke launcher.
    assert 'onclick="openPanel(this.dataset.id)"' in html
    # The composition the shell ships == the real helper output.
    sections = _const_array(html, 'SETTINGS_SECTIONS')
    assert sections == get_settings_sections()
    # Every aggregated id resolves to a registered panel, so each tile is openable.
    registered = set(PANEL_MANIFEST) | set(SYSTEM_PANELS)
    for sec in sections:
        for pid in sec['ids']:
            assert pid in registered, 'settings tile not openable: ' + pid


def test_served_settings_search_filters_tiles():
    """The Settings panel has its own in-place tile filter (mirrors filterStart)."""
    html = _served_html()
    assert 'function filterSettings(q)' in html
    assert 'filterSettings(this.value)' in html
