"""The glass shell must SERVE every asset it references from /shell/static/.

Regression (2026-06-15, first real ISO/USB boot of the voice-hero desktop):
``render_desktop_shell`` loads the HART logo and all ten external scripts from
``/shell/static/...`` (voiceOrbViz.js, hartHero.js, hartDesktop.js,
hartOnboarding.js, ...), but ``_create_flask_app`` built the app as plain
``Flask(__name__)`` — whose built-in static route is ``/static``. So on a real
boot EVERY ``/shell/static/*`` request 404'd and the desktop came up a dead
husk: the voice orb never animated (only the static mic showed), the hero
input + desktop never wired (dead clicks, couldn't type), onboarding never
fired, and the logo rendered as a broken-image "?" in two places.

It was invisible because the feature was "tested via inline render" — which
produces the HTML string but never actually fetches ``/shell/static/``. These
tests close exactly that gap: they start the REAL Flask app, take EXACTLY the
asset URLs the rendered shell asks the browser to load, and prove the server
returns 200 for each. Behavioural (render + fetch), not source-shape.
"""
import re

import pytest

from integrations.agent_engine.liquid_ui_service import LiquidUIService


@pytest.fixture
def shell():
    """A real LiquidUIService + its Flask test client. ``__init__`` is light
    (ContextEngine just stores ports; no threads/network until serve_forever),
    so this is a genuine end-to-end app, not a mock."""
    svc = LiquidUIService()
    app = svc._create_flask_app()
    app.testing = True
    return svc, app.test_client()


def _static_refs(html):
    """Every ``src="/shell/static/..."`` the shell tells the browser to load —
    both ``<script src>`` and ``<img src>``. Sourced from the live render so the
    test can never drift from what the HTML actually requests."""
    return sorted(set(re.findall(r'src="(/shell/static/[^"]+)"', html)))


def test_every_shell_static_asset_the_html_requests_is_served(shell):
    """The whole-desktop guard: take the rendered shell's asset list and prove
    the app serves all of it. This is the assertion that would have caught the
    dead-husk regression on the very first run."""
    svc, client = shell
    refs = _static_refs(svc.render_desktop_shell())
    assert refs, "shell HTML references no /shell/static assets — render changed?"
    broken = []
    for url in refs:
        r = client.get(url)
        if r.status_code != 200 or not r.data:
            broken.append((url, r.status_code))
    assert not broken, f"/shell/static assets did not load (dead-husk): {broken}"


@pytest.mark.parametrize("asset", [
    "voiceOrbViz.js",     # the voice viz that "showed earlier and not anymore"
    "hartHero.js",        # hero input + mic + chips wiring (typing & clicks)
    "hartDesktop.js",     # desktop icon interactivity
    "hartOnboarding.js",  # the "Light your HART" first-run ceremony
    "hevolve-logo.png",   # the Hevolve brand mark (hero + start button)
    "lottie.min.js",      # bundled offline Lottie player (boot splash)
    "hartBootSplash.js",  # the Hevolve brand boot-splash driver
    "hevolve-anim.json",  # the Hevolve hourglass Lottie animation
    # The bundled icon font — referenced by the CSS @font-face (NOT a src="…"),
    # so _static_refs above can't see it. If this 404s, EVERY shell glyph
    # vanishes offline (smart_toy/shield + all tray/dock icons go blank).
    "MaterialSymbolsRounded.woff2",
])
def test_critical_shell_asset_is_fetchable(shell, asset):
    """Each named asset maps 1:1 to a symptom the steward saw on the booted
    ISO, so a future break points straight at the broken behaviour."""
    _svc, client = shell
    r = client.get(f"/shell/static/{asset}")
    assert r.status_code == 200, f"{asset} -> {r.status_code} (blank/dead shell)"
    assert r.data, f"{asset} served empty"


@pytest.mark.parametrize("asset,needles,fix", [
    # overlap/clutter fix (16ce15e5): the webkit-flat solidify gradient is opaque
    # (~0.985 at every stop) so panels do not read see-through on the software floor.
    ("hartResponsive.css", ["0.985", "webkit-flat"],
     "opaque webkit-flat panel (overlap/clutter fix)"),
    # Aura microanimations (b3cf15b2): the composable motion vars + a keyframe.
    ("hartHome.css", ["vBreathe", "--hart-motion-ambient"],
     "Aura microanimations (composable motion layers)"),
    # aura-listing (2cd78f23) + Motion controls (b3cf15b2) in the personalize hub.
    ("hartPersonalize.js", ["id: 'aura'", "HartMotion"],
     "Aura listed in the theme picker + Motion controls"),
])
def test_shell_fix_is_actually_served(shell, asset, needles, fix):
    """The loop's VERIFY leg: a committed shell fix must actually REACH the served
    /shell/static asset, not just return 200. A fix that is on disk but never
    served (wrong path, stale bundle, static route regressed) is a silent
    regression the 200-only guards above cannot catch -- this is the whole reason
    the fix->serve->verify loop fetches the real body. Behavioural: real app +
    real fetch, assert the fix content is in the SERVED response."""
    _svc, client = shell
    r = client.get("/shell/static/{}".format(asset))
    assert r.status_code == 200, "{} -> {} ({} never served)".format(asset, r.status_code, fix)
    body = r.get_data(as_text=True)
    missing = [n for n in needles if n not in body]
    assert not missing, "{} served but MISSING {} -- {} did not reach the shell".format(asset, missing, fix)


def test_static_handler_is_repointed_not_duplicated(shell):
    """The fix REPOINTS Flask's single static handler to /shell/static — it does
    not add a parallel route. Flask's old default ``/static`` prefix must no
    longer serve these (one source of truth for shell assets)."""
    _svc, client = shell
    assert client.get("/static/hartHero.js").status_code == 404
