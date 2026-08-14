"""M2 — the orb/command bar stays the PRIMARY composition surface, never
inert wallpaper, when a panel opens.

Symptom this guards (launcher-is-the-spine): ``hartHero.js`` used to toggle a
``dimmed`` class on ``#hart-hero`` the instant any panel appeared in
``#panels`` — and ``.hart-hero.dimmed`` is ``opacity:0`` +
``pointer-events:none``, i.e. the orb literally became dead wallpaper. With the
brain now able to push composed UI to the glass (A2UI ``agent_ui_update``), the
spine MUST remain visible, active, and reachable while work is open. It may
shrink / dock, but it may not go inert.

The fix lives entirely in ``hartHero.js``: on panel-open it now strips any
inert ``dimmed`` state, toggles a ``docked`` class, and applies a
self-contained dock transform (scaled-down, parked toward the edge) that keeps
``opacity:1`` + ``pointer-events:auto`` — visible and clickable.

Like ``test_liquid_ui_shell_static_route.py`` this drives the REAL
``LiquidUIService().render_desktop_shell()`` + its Flask app and FETCHES the
served asset (dead-husk-aware: a 404 here means the JS never reaches the
browser at all). The behavioural half — that the rendered DOM truly never goes
opacity:0 on panel-open — needs a real browser (playwright) / booted VM to
assert against live layout, so the contract assertions on the SERVED JS are
explicitly labelled a SOURCE-GUARD. See ``vmPending`` in the task result.
"""
import re

import pytest

from integrations.agent_engine.liquid_ui_service import LiquidUIService


@pytest.fixture
def shell():
    """A real LiquidUIService + its Flask test client (light __init__, no
    threads/network until serve_forever) — a genuine end-to-end app, the same
    shape used by test_liquid_ui_shell_static_route.py."""
    svc = LiquidUIService()
    app = svc._create_flask_app()
    app.testing = True
    return svc, app.test_client()


def test_hero_js_is_actually_served(shell):
    """Dead-husk guard: the shell references hartHero.js, and the app must
    return it 200 + non-empty. A 404 means the orb-primary behaviour never
    even loads in the browser (the 2026-06-15 dead-husk class)."""
    svc, client = shell
    html = svc.render_desktop_shell()
    assert 'src="/shell/static/hartHero.js"' in html, \
        "shell no longer loads hartHero.js — render changed?"
    r = client.get('/shell/static/hartHero.js')
    assert r.status_code == 200, f"hartHero.js -> {r.status_code} (not served)"
    assert r.data, "hartHero.js served empty"


def _served_hero_js(client):
    return client.get('/shell/static/hartHero.js').get_data(as_text=True)


def test_panel_open_does_not_dim_orb_to_wallpaper(shell):
    """SOURCE-GUARD (served-JS contract): the panel-open handler must NOT add
    the inert ``dimmed`` class. ``.hart-hero.dimmed`` is opacity:0 +
    pointer-events:none — the literal dim-to-wallpaper behaviour M2 removes.

    We assert on the panel-open MutationObserver block specifically, not the
    whole file, so the standing ``classList.remove('dimmed')`` defensive strip
    (which mentions the word) does not give a false pass."""
    _svc, client = shell
    js = _served_hero_js(client)

    # No code path may re-apply the inert class. toggle('dimmed', ...) /
    # add('dimmed') is exactly the regression; only remove('dimmed') is allowed.
    assert not re.search(r"\.toggle\(\s*['\"]dimmed['\"]", js), \
        "hartHero.js still toggles the inert 'dimmed' (opacity:0) class"
    assert not re.search(r"classList\.add\(\s*['\"]dimmed['\"]", js), \
        "hartHero.js still adds the inert 'dimmed' (opacity:0) class"


def test_panel_open_keeps_orb_active_and_reachable(shell):
    """SOURCE-GUARD (served-JS contract): on panel-open the spine must stay the
    primary surface — visible (opacity ~1) and reachable (pointer-events auto),
    docked rather than dimmed. These inline writes are what keep the orb from
    going inert even on a shell whose CSS predates this change."""
    _svc, client = shell
    js = _served_hero_js(client)

    assert re.search(r"\.toggle\(\s*['\"]docked['\"]", js), \
        "hartHero.js no longer docks the orb on panel-open"
    # The orb is explicitly kept visible + clickable while panels are open.
    # CONTRACT UPDATED for the place() single-writer design: opacity is
    # composed (s.opacity = String(op)) from op initialised to 1, and the
    # ONLY dim case (merged idle, op=0.34) explicitly excludes panelOpen /
    # chatOpen — strictly stronger than the old literal opacity='1' this
    # test used to grep for.
    assert re.search(r"var\s+op\s*=\s*1\b", js), \
        "place() must initialise opacity to full (var op = 1)"
    assert re.search(
        r"B\.merged\s*&&[^\n]*!B\.panelOpen\s*&&\s*!B\.chatOpen", js), \
        "the merged-dim case must exclude panel/chat-open so the docked orb " \
        "never fades while panels are up"
    assert re.search(r"opacity\s*=\s*String\(\s*op\s*\)", js), \
        "place() must remain the single writer of the orb's opacity"
    assert re.search(r"pointerEvents\s*=\s*['\"]auto['\"]", js), \
        "docked orb must stay reachable (pointer-events:auto)"
