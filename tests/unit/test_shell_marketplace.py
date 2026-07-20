"""Free-software marketplace (Phase C).

Drives the REAL render_desktop_shell() for the App Store delegate + marketplace
card CSS + script wiring, and asserts hartMarketplace.js reuses the EXISTING
installer routes (/api/apps/search + /api/apps/install) and the agent dispatch
rather than forking them.

Local note: this box OOMs pytest; run the inline runner at the bottom. CI runs it.
"""
import os
import sys

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


def test_marketplace_script_and_cards_wired():
    html = _render()
    assert '/shell/static/hartMarketplace.js' in html
    assert '.hart-app-card{' in html and '.hart-app-grid{' in html


def test_app_store_delegates_to_marketplace():
    html = _render()
    assert 'window.hartRenderMarketplace(el)' in html
    # the old raw package-search panel body is gone
    assert "placeholder=\"Search packages...\"" not in html


def test_marketplace_js_reuses_installer_and_agent():
    src = open(os.path.join(STATIC, 'hartMarketplace.js'), encoding='utf-8').read()
    assert 'window.hartRenderMarketplace =' in src
    assert "'/api/apps/install'" in src            # reuse the real installer
    assert "'/api/apps/search?q='" in src          # reuse the real search
    assert "'/api/apps/installed'" in src          # reuse the real installed-apps source (no parallel list)
    assert 'window.acSend' in src                  # AI-native fallback via agent
    # A real curated catalog (Flathub ids), not an empty store.
    assert "id: 'org.mozilla.firefox'" in src
    assert src.count("id: '") >= 15                # 15+ curated free apps


def test_marketplace_install_gives_honest_feedback():
    """Install must report off the REAL server response (not a fire-and-forget
    'Installing' toast even on failure). Behaviour is exercised by the .mjs
    harness; this guards that the response branches exist + stay wired."""
    src = open(os.path.join(STATIC, 'hartMarketplace.js'), encoding='utf-8').read()
    assert 'res.success' in src                    # success path read from response
    assert 'res.staged' in src                     # staged (downloaded, not applied) path
    assert "'success'" in src and "'error'" in src  # honest toast severities


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print('  OK  ', fn.__name__)
        except Exception as e:
            failed += 1; print(' FAIL ', fn.__name__, '->', repr(e))
    print('RESULT:', 'ALL PASS' if not failed else (str(failed) + ' FAILED'))
    sys.exit(1 if failed else 0)
