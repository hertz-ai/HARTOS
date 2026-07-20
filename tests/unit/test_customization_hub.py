"""Customization hub — server-side (ThemeService palette extension) + the JS shim.

Two halves:

1. The behavioural JS coverage lives in ``test_customization_hub.mjs`` (drives the
   real hartPersonalize.js / voiceOrbViz.js on a DOM shim: palette applies + persists
   the brand vars, the custom picker, orb-variety switch persists + re-tints, and the
   media backgrounds DEGRADE on the software floor). This wrapper shells out to node
   so pytest/CI runs it too; it skips cleanly when node is absent.

2. Direct Python tests for the ``/api/appearance/apply`` EXTENSION (#161): a
   palette apply carries ``secondary_accent`` + ``custom`` colours; ThemeService
   persists them through the canonical custom-overrides path (reuse, not fork), and
   ``get_css_variables`` emits ``--hart-a2`` (+ its rgb triple) from the persisted
   secondary. A palette-only apply (no preset switch) is accepted, and a truly-empty
   body is still a 400.
"""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_customization_hub.mjs')


# ── 1) JS behavioural harness (shelled out) ────────────────────────────────────
def test_customization_hub_js():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'customization hub behaviour harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


# ── 2) ThemeService palette extension (server persistence) ─────────────────────
_tmp = tempfile.mkdtemp(prefix='hart_customhub_')
_theme_dir = os.path.join(_tmp, 'themes')
_data_dir = os.path.join(_tmp, 'data')
os.makedirs(_theme_dir, exist_ok=True)
os.makedirs(_data_dir, exist_ok=True)

_BASE_PRESET = {
    'id': 'hart-default', 'name': 'HART Default', 'category': 'dark',
    'colors': {'background': '0F0E17', 'accent': '00D4AA', 'text': 'e0e0e0',
               'heading': '00D4AA', 'muted': '78909c', 'surface': '1a1a2e'},
    'font': {'family': 'JetBrains Mono', 'size': 13, 'heading_size': 18,
             'weight': 400, 'heading_weight': 600},
    'shell': {'blur_radius': 20, 'saturation': 180, 'border_radius': 16, 'panel_opacity': 0.65},
    'gtk_prefer_dark': True,
}
with open(os.path.join(_theme_dir, 'hart-default.json'), 'w', encoding='utf-8') as _f:
    json.dump(_BASE_PRESET, _f)


@pytest.fixture(autouse=True)
def _patch_paths():
    import integrations.agent_engine.theme_service as ts
    orig = (ts._THEME_DIR, ts._ACTIVE_THEME_PATH, ts._CUSTOM_OVERRIDES_PATH)
    ts._THEME_DIR = _theme_dir
    ts._ACTIVE_THEME_PATH = os.path.join(_data_dir, 'active_theme.json')
    ts._CUSTOM_OVERRIDES_PATH = os.path.join(_data_dir, 'theme_custom.json')
    for f in ('active_theme.json', 'theme_custom.json'):
        p = os.path.join(_data_dir, f)
        if os.path.exists(p):
            os.remove(p)
    yield
    ts._THEME_DIR, ts._ACTIVE_THEME_PATH, ts._CUSTOM_OVERRIDES_PATH = orig


def _svc():
    from integrations.agent_engine.theme_service import ThemeService
    return ThemeService


def test_palette_only_apply_persists_custom_overrides():
    """A palette apply with NO preset switch (secondary_accent + custom) persists
    the colours through the custom-overrides path and reports 'customized'."""
    svc = _svc()
    res = svc.apply_theme('', secondary_accent='#9B5CFF',
                          custom={'accent': '#00E6C3', 'secondary': '#9B5CFF', 'background': '#05060C'})
    assert res.get('status') == 'customized', res
    colors = res['overrides']['colors']
    assert colors['accent'] == '#00E6C3'
    assert colors['secondary'] == '#9B5CFF'
    assert colors['background'] == '#05060C'
    # It went through the real overrides file (reuse of update_custom).
    import integrations.agent_engine.theme_service as ts
    with open(ts._CUSTOM_OVERRIDES_PATH, encoding='utf-8') as f:
        saved = json.load(f)
    assert saved['colors']['secondary'] == '#9B5CFF'


def test_get_css_variables_emits_hart_a2_from_secondary():
    """The shell reads --hart-a2 / --hart-a2-rgb; a persisted secondary must emit
    them under the canonical var name so a hard reload keeps the duotone."""
    svc = _svc()
    svc.apply_theme('hart-default', secondary_accent='#9B5CFF',
                    custom={'accent': '#00E6C3', 'secondary': '#9B5CFF', 'background': '#05060C'})
    css = svc.get_css_variables()
    assert '--hart-a2: #9B5CFF;' in css, css
    assert '--hart-a2-rgb: 155,92,255;' in css, css
    # The lead accent + background overrides also land.
    assert '--hart-accent: #00E6C3;' in css
    assert '--hart-background: #05060C;' in css


def test_preset_plus_palette_overlay_keeps_both():
    """A preset switch WITH a palette overlay applies the preset as the base AND
    keeps the palette accents on top (the overlay re-applies after the reset)."""
    svc = _svc()
    res = svc.apply_theme('hart-default', secondary_accent='#FF2E9A',
                          custom={'accent': '#3B82F6'})
    assert res.get('status') == 'applied', res
    assert res['theme_id'] == 'hart-default'
    css = svc.get_css_variables()
    assert '--hart-accent: #3B82F6;' in css       # palette accent overlaid the preset
    assert '--hart-a2: #FF2E9A;' in css           # secondary overlaid too


def test_norm_hex_tolerates_missing_hash():
    svc = _svc()
    res = svc.apply_theme('', custom={'accent': '00E6C3'})   # no leading '#'
    assert res['overrides']['colors']['accent'] == '#00E6C3'


def test_route_accepts_palette_only_and_rejects_empty():
    """The /api/appearance/apply route (theme_bp) accepts a palette-only body and
    still rejects a truly-empty one (extend, do not fork the contract)."""
    from flask import Flask
    from integrations.social.api_theme import theme_bp
    app = Flask(__name__)
    app.register_blueprint(theme_bp)
    app.config['TESTING'] = True
    with app.test_client() as c:
        good = c.post('/api/appearance/apply',
                      json={'secondary_accent': '#9B5CFF',
                            'custom': {'accent': '#00E6C3', 'secondary': '#9B5CFF', 'background': '#05060C'}})
        assert good.status_code == 200, good.get_data(as_text=True)
        assert good.get_json().get('status') == 'customized'

        empty = c.post('/api/appearance/apply', json={})
        assert empty.status_code == 400


def test_theme_bp_uses_appearance_namespace_no_social_collision():
    """Regression (audit HIGH#1, the shadowed-palette bug): the OS appearance routes
    (theme_bp/ThemeService) live under /api/appearance/* so they can NEVER be shadowed by
    the social per-user theme routes (/api/social/theme/*, social_bp, @require_auth). The
    palette went dead because both registered /api/social/theme/apply and social_bp won.
    If theme_bp ever re-registers a /api/social/theme/* rule, this fails loudly."""
    from flask import Flask
    from integrations.social.api_theme import theme_bp
    app = Flask(__name__)
    app.register_blueprint(theme_bp)
    rules = {str(r) for r in app.url_map.iter_rules()}
    assert any(r.startswith('/api/appearance/') for r in rules), rules
    assert not any('/api/social/theme/' in r for r in rules), \
        f'theme_bp must NOT register /api/social/theme/* (collides with social_bp): {rules}'


# ── 3) Ambient quad + the two ready-made designs (LIQUID_UI_AGENTIC_FRAMEWORK) ──
# get_css_variables is the keystone that var-drives the ambient field: it must emit
# --hart-amb-1..4 (+ rgb triples), the display font, and the themable glass base so
# the widened liquid surface (hartResponsive.css / .hart-ambient / hartHero) has live
# consumers. The two SHIPPED presets (hart-default + aura) must render DISTINCT, valid
# CSS: HART ambient teal-lead, Aura ambient violet-lead, teal pinned on the functional
# --hart-accent in BOTH (the steward hybrid). These render the REAL shipped preset
# json (decoupled from the fixture theme dir) so the test tracks what actually ships.
from unittest.mock import patch as _patch

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REAL_THEME_DIR = os.path.join(_REPO, 'nixos', 'assets', 'conky-themes')


def _css_for_shipped_preset(preset_id):
    from integrations.agent_engine.theme_service import ThemeService
    with open(os.path.join(_REAL_THEME_DIR, preset_id + '.json'),
              encoding='utf-8') as f:
        preset = json.load(f)
    with _patch.object(ThemeService, 'get_active_theme', return_value=preset):
        return ThemeService.get_css_variables()


def test_css_emits_full_ambient_contract_vars():
    """Every var the widened liquid surface consumes is emitted: the ambient quad
    (--hart-amb-1..4 + rgb triples), the display font, the themable glass base, and
    radius/opacity/glow/density. A missing var = a dead consumer in the shell CSS."""
    css = _css_for_shipped_preset('hart-default')
    for i in range(1, 5):
        assert '--hart-amb-%d:' % i in css, css
        assert '--hart-amb-%d-rgb:' % i in css, css
    assert '--hart-font-display:' in css
    assert '--hart-glass-rgb:' in css          # themable glass base (Opacity slider)
    assert '--hart-radius:' in css
    assert '--hart-panel-opacity:' in css
    assert '--hart-glow:' in css and '--hart-density:' in css


def test_hart_and_aura_presets_are_distinct_and_valid():
    """The two ready-made designs each produce valid CSS and a DISTINCT skin, while
    keeping teal on the functional signifier (steward hybrid)."""
    hart = _css_for_shipped_preset('hart-default')
    aura = _css_for_shipped_preset('aura')
    # Valid: a :root block, balanced braces, every declaration terminated.
    for css in (hart, aura):
        assert css.startswith(':root {') and css.rstrip().endswith('}')
        body = [ln.strip() for ln in css.splitlines()[1:-1] if ln.strip()]
        assert body and all(ln.endswith(';') for ln in body), css
    assert hart != aura                                   # distinct skins
    # HART ambient is TEAL-lead; Aura ambient is VIOLET-lead (--hart-amb-1).
    assert '--hart-amb-1: #00E6C3;' in hart               # teal
    assert '--hart-amb-1: #B182FF;' in aura               # violet
    # BUT --hart-accent (orb core / primary CTA / earnings) stays teal in BOTH.
    assert '--hart-accent: #00E6C3;' in hart
    assert '--hart-accent: #00E6C3;' in aura
    # Aura carries its own display face + a lighter (white) glass base vs HART's dark.
    assert '--hart-font-display: "Space Grotesk";' in aura
    assert '--hart-glass-rgb: 255,255,255;' in aura
    assert '--hart-glass-rgb: 18,19,28;' in hart


def test_list_presets_surfaces_aura_and_high_contrast_with_swatch_colours():
    """G3(a): the desktop theme picker now renders from THIS one source
    (/api/appearance/presets -> list_presets), so Aura + high-contrast must appear with
    the 4 swatch colours a card needs (accent/secondary/background/surface). Was: the
    client hardcoded 8 presets and Aura was unreachable from the desktop."""
    import integrations.agent_engine.theme_service as ts
    ts._THEME_DIR = _REAL_THEME_DIR                 # read the REAL shipped presets
    by_id = {p['id']: p for p in ts.ThemeService.list_presets()}
    assert 'aura' in by_id, 'Aura preset not surfaced by list_presets (G3 gap)'
    assert 'high-contrast' in by_id
    for pid in ('aura', 'hart-default'):
        p = by_id[pid]
        for k in ('accent', 'secondary', 'background', 'surface'):
            assert p.get(k), '%s preset missing %r for the swatch' % (pid, k)


def test_shell_applyPreset_live_swaps_the_theme_with_no_reload():
    """G3(b): the served shell's applyPreset LIVE-swaps the theme :root vars from
    /api/appearance/css into a managed <style> (no reload on the success path); reload
    survives ONLY as the css-fetch fallback. The css it fetches carries the target
    quad (proven by test_hart_and_aura_presets_are_distinct_and_valid)."""
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    html = LiquidUIService(a2ui_enabled=True).render_desktop_shell()
    assert '/api/appearance/css' in html, 'applyPreset does not fetch the live css'
    assert 'hart-theme-live' in html, 'applyPreset does not swap a managed <style>'
    # the keyword router can switch to Aura + high-contrast now (was neither)
    assert "applyPreset('aura'" in html
    assert "applyPreset('high-contrast'" in html


def test_legacy_preset_apply_still_works():
    """Zero-regression: the original single-arg apply_theme(theme_id) still works."""
    svc = _svc()
    res = svc.apply_theme('hart-default')
    assert res.get('status') == 'applied'
    assert res['theme_id'] == 'hart-default'


def test_css_variables_emit_glow_and_density():
    """#170 token engine: get_css_variables emits --hart-glow + --hart-density (with
    sensible defaults so presets that omit them are unaffected) — the shell drives accent
    glow + spacing scale live via these CSS vars. Existing tokens must still emit."""
    css = _svc().get_css_variables()
    assert '--hart-glow:' in css, css[-400:]
    assert '--hart-density:' in css, css[-400:]
    assert '--hart-accent' in css and '--hart-radius:' in css  # existing tokens intact


if __name__ == '__main__':
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    failed = 0

    # Minimal manual runner honoring the autouse fixture.
    import integrations.agent_engine.theme_service as ts
    for fn in fns:
        orig = (ts._THEME_DIR, ts._ACTIVE_THEME_PATH, ts._CUSTOM_OVERRIDES_PATH)
        ts._THEME_DIR = _theme_dir
        ts._ACTIVE_THEME_PATH = os.path.join(_data_dir, 'active_theme.json')
        ts._CUSTOM_OVERRIDES_PATH = os.path.join(_data_dir, 'theme_custom.json')
        for f in ('active_theme.json', 'theme_custom.json'):
            p = os.path.join(_data_dir, f)
            if os.path.exists(p):
                os.remove(p)
        try:
            fn(); print('  OK  ', fn.__name__)
        except pytest.skip.Exception as e:
            print(' SKIP ', fn.__name__, '->', e)
        except Exception as e:
            failed += 1; print(' FAIL ', fn.__name__, '->', repr(e))
        finally:
            ts._THEME_DIR, ts._ACTIVE_THEME_PATH, ts._CUSTOM_OVERRIDES_PATH = orig
    print('RESULT:', 'ALL PASS' if not failed else (str(failed) + ' FAILED'))
    sys.exit(1 if failed else 0)
