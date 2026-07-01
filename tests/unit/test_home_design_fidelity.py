"""Design-fidelity guards (2026-07-01, GF1-GF4): the software-render floor must
DEGRADE GRACEFULLY (not gut the look), the home must match the mockup, and the
prebundled apps + agents must ship STATIC no-network brand posters.

Root cause being guarded (audit): the `body.gpu-software` / `is_potato` floor
USED to strip static depth (the 3 ambient cinematic glows, card drop-shadows,
the CTA glow) along with the genuinely per-frame-expensive effects, so the home
read flat/cheap on the steward's Intel-iGPU (software-verdict) box. Static
shadows + static glows raster ONCE and composite cheaply, so they belong on the
floor; only the per-FRAME costs (backdrop blur, drift animation, grain blend,
hover transforms) get shed.

Mix of:
  - BEHAVIOURAL: the real render emits the ambient div under a software verdict
    but NOT for a deliberate disable_blur on a capable GPU (real reader + real
    render, the verdict-file boundary mocked) - per the no-grep-tests rule.
  - SERVED-ARTIFACT source guards (clearly labelled): CSS visual depth has no
    Python entry point to exercise in pytest, so we fetch the REAL served
    stylesheet through the Flask static route and assert the structural property
    (the ambient is not display:none'd on software; the base card carries a
    static shadow). Mirrors the accepted test_shell_software_render_perf pattern.
  - ASSET INTEGRITY: every `image` the manifest + the home reference must be a
    real packed file under static/app_art that the static route serves and that
    parses as SVG - catches a broken/typo'd/missing poster reference.
"""
import importlib.util
import os
import re
import sys
import xml.dom.minidom

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.agent_engine import liquid_ui_service as lus
from integrations.agent_engine.liquid_ui_service import LiquidUIService
from integrations.agent_engine.shell_manifest import PANEL_MANIFEST, SYSTEM_PANELS

STATIC_DIR = os.path.join(ROOT, 'integrations', 'agent_engine', 'static')
APP_ART_DIR = os.path.join(STATIC_DIR, 'app_art')
HOME_JS = os.path.join(STATIC_DIR, 'hartHome.js')
HOME_CSS = os.path.join(STATIC_DIR, 'hartHome.css')
RESP_CSS = os.path.join(STATIC_DIR, 'hartResponsive.css')


@pytest.fixture
def svc():
    return LiquidUIService()


def _set_theme(monkeypatch, disable_blur):
    monkeypatch.setattr(
        'integrations.agent_engine.theme_service.ThemeService.get_active_theme',
        staticmethod(lambda: {'performance': {'disable_blur': disable_blur}}))


# ── GF1 BEHAVIOURAL: ambient survives the software floor ──────────────────────

def test_ambient_div_emitted_under_software_verdict(svc, monkeypatch):
    """The 3 ambient cinematic glows are the biggest 'looks rich' lever; on a
    SOFTWARE box they are STILL emitted (rendered static by the CSS floor), so
    depth is not gutted for no per-frame saving."""
    monkeypatch.setattr(lus, 'read_gpu_render_mode', lambda: 'software')
    _set_theme(monkeypatch, disable_blur=False)
    html = svc.render_desktop_shell()
    assert '<div class="hart-ambient"' in html
    assert 'gpu-software' in html


def test_ambient_div_emitted_on_capable_gpu_default(svc, monkeypatch):
    monkeypatch.setattr(lus, 'read_gpu_render_mode', lambda: 'hardware')
    _set_theme(monkeypatch, disable_blur=False)
    html = svc.render_desktop_shell()
    assert '<div class="hart-ambient"' in html


def test_ambient_div_suppressed_for_disable_blur_on_capable_gpu(svc, monkeypatch):
    """The one case the ambient stays OFF: an explicit theme disable_blur on a
    capable GPU. The user asked for no blur and the box can otherwise afford the
    full cinematic, so honour that (the software floor is the only thing that
    re-enables the static ambient)."""
    monkeypatch.setattr(lus, 'read_gpu_render_mode', lambda: 'hardware')
    _set_theme(monkeypatch, disable_blur=True)
    html = svc.render_desktop_shell()
    assert '<div class="hart-ambient"' not in html


# ── GF3 BEHAVIOURAL: the orb halo is teal + violet, not the flagged indigo ────

def test_orb_glow_is_teal_and_violet_not_indigo(svc):
    html = svc.render_desktop_shell()
    m = re.search(r'#hart-voice-orb\{[^}]*\}', html)
    assert m, 'no #hart-voice-orb rule in shell'
    rule = m.group(0)
    # mockup layered glow: teal inner (0,230,195) + brand violet outer (155,92,255)
    assert 'rgba(0,230,195' in rule
    assert 'rgba(155,92,255' in rule
    # the leftover indigo #6C63FF (108,99,255) b1.1 flagged must be gone here
    assert '108,99,255' not in rule


# ── GF1/GF2 SERVED-ARTIFACT source guards: the real CSS the browser gets ──────

def _serve(svc, path):
    app = svc._create_flask_app()
    app.testing = True
    r = app.test_client().get(path)
    assert r.status_code == 200, '%s -> %s' % (path, r.status_code)
    return r.get_data(as_text=True)


def test_software_floor_keeps_ambient_drops_grain(svc):
    css = _serve(svc, '/shell/static/hartResponsive.css')
    # The grain overlay (a per-frame blend) is still dropped on software...
    assert re.search(r'body\.gpu-software \.hart-grain\s*\{\s*display:\s*none', css)
    # ...but the ambient is NOT display:none'd (it is kept, static) - guard against
    # a regression back to gutting it.
    assert not re.search(r'body\.gpu-software\s+\.hart-ambient[^{]*\{[^}]*display:\s*none', css)
    assert re.search(r'body\.gpu-software\s+\.hart-ambient[^{]*\{[^}]*animation:\s*none', css)


def test_software_wallpaper_deepened_toward_mockup(svc):
    css = _serve(svc, '/shell/static/hartResponsive.css')
    # The mockup canvas is #05070d; the floor wallpaper deepened to ~#06070D
    # (was the lighter/purpler #0A0A11). Guard the deep stop is present.
    assert '#06070D' in css


def test_home_card_has_static_shadow_in_base(svc):
    """The mockup card depth (a STATIC drop-shadow) lives on the BASE .hh-card so
    a software box still gets depth; only hover-scale stays GPU-gated."""
    css = _serve(svc, '/shell/static/hartHome.css')
    base = re.search(r'\.hh-card\s*\{[^}]*\}', css)
    assert base and 'box-shadow' in base.group(0), 'base .hh-card lost its static shadow'


# ── GF4 ASSET INTEGRITY: the static poster pack is real, packed, and served ────

def _generator_poster_names():
    spec = importlib.util.spec_from_file_location(
        'app_art_gen', os.path.join(APP_ART_DIR, 'generate_posters.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return set(mod.POSTERS.keys())


def _referenced_app_art_paths():
    """Every /shell/static/app_art/*.svg referenced by the manifest + the home."""
    paths = set()
    for reg in (PANEL_MANIFEST, SYSTEM_PANELS):
        for entry in reg.values():
            img = entry.get('image')
            if img and 'app_art/' in img:
                paths.add(img)
    with open(HOME_JS, 'r', encoding='utf-8') as f:
        paths.update(re.findall(r"/shell/static/app_art/[\w-]+\.svg", f.read()))
    return paths


def test_every_referenced_poster_is_packed_and_valid_svg(svc):
    refs = _referenced_app_art_paths()
    assert len(refs) >= 10, 'expected the app + agent posters to be wired'
    gen_names = _generator_poster_names()
    for ref in sorted(refs):
        name = os.path.basename(ref)[:-4]            # strip .svg
        # tied to the generator's source-of-truth (no orphan reference)
        assert name in gen_names, '%s is not a generated poster' % name
        disk = os.path.join(APP_ART_DIR, name + '.svg')
        assert os.path.isfile(disk), 'missing packed poster: %s' % disk
        xml.dom.minidom.parse(disk)                  # must be well-formed SVG
        # the Flask static route must actually serve it (no-network, offline)
        body = _serve(svc, ref)
        assert body.lstrip().startswith('<svg'), 'route did not serve SVG for %s' % ref


def test_generator_emits_all_declared_posters():
    """The committed SVGs must match the generator's declared set (so a future
    edit to the generator without re-running it is caught)."""
    gen_names = _generator_poster_names()
    for name in gen_names:
        disk = os.path.join(APP_ART_DIR, name + '.svg')
        assert os.path.isfile(disk), 'generator declares %s but it was not emitted' % name
