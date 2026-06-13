"""Desktop icon layer (Phase A): drag-drop app icons on the HART OS desktop.

Behavioural where it counts: drives the REAL render_desktop_shell() and asserts
the drag-drop layer + orchestrator + context-menu wiring are in the emitted
document, and round-trips the session-state persistence route the feature relies
on through the REAL Flask app (POST a blob -> GET it back, with merge).

Local note: this box OOM-kills pytest; verify with the inline runner at the
bottom of the change (python -c importing these asserts). Committed for CI.
"""
import os
import sys
import tempfile

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

STATIC = os.path.join(ROOT, 'integrations', 'agent_engine', 'static')


def _liquid_ui():
    try:
        from integrations.agent_engine.liquid_ui_service import LiquidUIService
    except Exception as e:  # heavy deps absent in a minimal CI runner -> skip
        pytest.skip('LiquidUIService not importable here: ' + str(e))
    return LiquidUIService


def _render():
    return _liquid_ui()().render_desktop_shell()


def test_desktop_layer_and_orchestrator_are_wired():
    html = _render()
    assert 'id="hart-desktop"' in html                    # the drag-drop surface
    assert '.desktop-icon{' in html                        # its stylesheet
    assert '.desktop-icon.dragging{' in html               # drag affordance
    assert '/shell/static/hartDesktop.js' in html          # the orchestrator script


def test_layer_is_click_through_but_icons_are_not():
    """The layer must be pointer-events:none so an empty-desktop right-click still
    reaches the wallpaper context menu; only the icons capture pointer events."""
    html = _render()
    # Scope the assertions to the desktop rules (pointer-events:none appears for
    # other layers too — assert the specific selectors carry the right value).
    assert '.hart-desktop{' in html
    css = html.split('.hart-desktop{', 1)[1]
    assert 'pointer-events:none' in css.split('}', 1)[0]
    icon_css = html.split('.desktop-icon{', 1)[1].split('}', 1)[0]
    assert 'pointer-events:auto' in icon_css


def test_desktop_context_menu_offers_pin_and_arrange():
    html = _render()
    assert "ctxItem('add_to_home_screen','Add app to desktop'" in html
    assert "ctxItem('grid_view','Auto-arrange icons'" in html


def test_session_state_roundtrip_persists_icons_and_merges():
    """The persistence contract the desktop relies on, exercised through the REAL
    routes: POST -> GET returns the icons, and unrelated shell state coexists
    (the client does read-modify-write on one blob)."""
    svc = _liquid_ui()()
    svc._data_dir = tempfile.mkdtemp()  # never touch real shell state
    client = svc._create_flask_app().test_client()

    icons = [{'id': 'app_store', 'x': 24, 'y': 24},
             {'id': 'files', 'x': 24, 'y': 116}]
    r = client.post('/api/shell/session-state',
                    json={'desktop_icons': icons, 'theme': 'midnight'})
    assert r.status_code == 200

    got = client.get('/api/shell/session-state').get_json()
    assert got.get('desktop_icons') == icons
    assert got.get('theme') == 'midnight'  # other shell state is not clobbered


def test_hartdesktop_js_exposes_the_actions_the_menu_calls():
    """Source-shape guard (explicitly labelled): the orchestrator must define, by
    name, every action the context menu invokes — else those menu items silently
    no-op. Also asserts drag is GPU-composited (transform), not layout-thrash."""
    src = open(os.path.join(STATIC, 'hartDesktop.js'), encoding='utf-8').read()
    for fn in ('window.hartPinIcon =', 'window.hartRemoveIcon =',
               'window.hartAutoArrange =', 'window.hartAddAppPicker ='):
        assert fn in src, fn
    assert "el.style.transform = 'translate('" in src     # GPU drag, no reflow
    assert 'HartSession.set' in src and 'HartSession.ready' in src  # shared writer, no clobber


def test_virtual_desktop_switcher_is_wired():
    html = _render()
    assert 'id="hart-ws-switcher"' in html                 # the bottom switcher bar
    assert '.hart-ws-dot{' in html and '.hart-ws-dot.active{' in html
    assert '/shell/static/hartWorkspaces.js' in html        # the manager


def test_workspaces_panel_uses_client_shell_desktops():
    """The Workspaces settings panel reflects the SHELL's client-side virtual
    desktops (hartWorkspaces) — the meaningful layer on the cage kiosk. It does
    NOT fetch the sway compositor route /api/shell/workspaces (a separate layer
    that returns one fallback workspace under cage), and renders switchable
    squares wired to the client manager."""
    html = _render()
    assert "fetch(SHELL+'/api/shell/workspaces'" not in html   # not the compositor layer
    assert 'window.hartWorkspaceInfo' in html
    assert 'class="hart-ws-square' in html
    assert 'hartSwitchWorkspace(' in html


def test_hartworkspaces_js_exposes_api_and_tags_windows():
    """Source-shape guard (labelled): the manager defines the switch API the bar
    and panel call, and tags windows via MutationObserver (no openPanel fork)."""
    src = open(os.path.join(STATIC, 'hartWorkspaces.js'), encoding='utf-8').read()
    assert 'window.hartSwitchWorkspace =' in src
    assert 'window.hartWorkspaceInfo =' in src
    assert 'MutationObserver' in src and "getElementById('panels')" in src
    assert "setAttribute('data-ws'" in src                 # per-window workspace tag


if __name__ == '__main__':
    # Inline runner (pytest OOMs on this box): execute every test_* and report.
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print('  OK  ', fn.__name__)
        except Exception as e:
            failed += 1
            print(' FAIL ', fn.__name__, '->', repr(e))
    print('RESULT:', 'ALL PASS' if not failed else (str(failed) + ' FAILED'))
    sys.exit(1 if failed else 0)
