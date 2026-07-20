"""Phase-0 floor-lock source-guards: the generative `/api/ui` path stays a
terminal/Conky degraded surface and is NEVER wired into the desktop-shell boot.

Migration map (HART_OS_NATIVE_ARCHITECTURE.md §4.1, "Generative & adaptive UI"):
`ContextEngine` + `generate_ui` + `/api/ui` are graded **P (preserved) — L3
terminal/Conky degraded surface only. NOT resurrected as a boot path (zero
desktop frontends fetch /api/ui). A labeled source-guard (Phase 0) freezes this.`

This file IS that labeled source-guard. Two guards:

  (a) test_source_guard_no_desktop_shell_fetches_api_ui
      The desktop glass shell — the served `render_desktop_shell()` HTML AND
      every external module under integrations/agent_engine/static/*.js — must
      NOT fetch `/api/ui` (nor call `generate_ui` client-side). If a future edit
      wired the generative path into the boot desktop, this fails. This is a
      cross-file source-shape guard (per the no-grep-tests rule: explicitly
      labeled `test_source_guard_*`, acceptable ONLY because the regression spans
      many static JS files where a single behavioural call site cannot catch it)
      — but it is anchored to REAL rendered output (`render_desktop_shell()` is
      executed, not greped from disk).

  (b) test_conky_terminal_degraded_surface_still_renders_from_generate_ui
      The Tier-4 degraded surface (terminal/Conky) DOES still render from
      `generate_ui`'s static path with no LLM available — proving the preserved
      fallback is not accidentally deleted. This is a BEHAVIOURAL test: it
      constructs the real LiquidUIService, calls the real `generate_ui`, and
      asserts observable structure (source='static', real components).

Run (dev box, targeted — the full suite OOMs):
    python -m pytest tests/unit/test_source_guards_generative.py \
        --noconftest -p no:capture -q
"""
import os
import re

import pytest

from integrations.agent_engine.liquid_ui_service import LiquidUIService

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "integrations", "agent_engine", "static",
)

# The desktop-shell frontend modules render_desktop_shell loads via
# <script src="/shell/static/*.js">. The generative `/api/ui` path must reach
# NONE of them — it is the terminal/Conky degraded surface only.
_SHELL_JS_MODULES = [
    "hartBloom.js", "hartBootSplash.js", "hartBrandArt.js",
    "hartConnectivity.js", "hartCredits.js", "hartDesktop.js", "hartDock.js",
    "hartEffects.js", "hartFiles.js", "hartFlash.js", "hartHero.js",
    "hartHome.js", "hartMarketplace.js", "hartNav.js", "hartOnboarding.js",
    "hartOSBridge.js", "hartPersonalize.js", "hartSenses.js", "hartSession.js",
    "hartSessionUI.js", "hartStates.js", "hartVisibility.js",
    "hartWorkspaces.js", "voiceOrbViz.js",
]

# Patterns that would prove the desktop boot path consumes the generative
# surface. `/api/ui` is the route; `generate_ui` is the method; `/api/context`
# alone is fine (it is consumed elsewhere), so we only forbid the generative-UI
# entrypoints.
_FORBIDDEN_IN_SHELL = (
    re.compile(r"/api/ui\b"),
    re.compile(r"\bgenerate_ui\b"),
)


@pytest.fixture(scope="module")
def shell_html():
    """The REAL rendered desktop shell HTML (render_desktop_shell is executed,
    not read from disk) — the exact document the cage floor serves."""
    return LiquidUIService().render_desktop_shell()


# ─────────────────────────────────────────────────────────────────────────
# (a) SOURCE-GUARD: generative /api/ui stays OUT of the desktop shell
# ─────────────────────────────────────────────────────────────────────────
def test_source_guard_no_desktop_shell_fetches_api_ui(shell_html):
    """The served glass-shell HTML must not fetch `/api/ui` or call
    `generate_ui`. If a future edit resurrects the generative path as a boot
    surface, this trips."""
    offenders = [p.pattern for p in _FORBIDDEN_IN_SHELL if p.search(shell_html)]
    assert not offenders, (
        "render_desktop_shell() now references the generative surface "
        f"{offenders}; per the migration map /api/ui is terminal/Conky-only "
        "and must NOT be wired into the desktop boot."
    )


def test_source_guard_no_static_js_module_fetches_api_ui():
    """None of the desktop-shell static JS modules may fetch `/api/ui` /
    `generate_ui`. Spans every shell module — exactly the cross-file regression a
    single behavioural call site cannot catch, so a labeled source-guard is the
    right (and only) tool. Anchored to the real on-disk modules the shell loads."""
    offenders = {}
    for name in _SHELL_JS_MODULES:
        path = os.path.join(_STATIC_DIR, name)
        assert os.path.isfile(path), (
            f"shell module {name} missing from {_STATIC_DIR} — the shell loads it "
            "via <script src>; the guard's module list drifted from reality."
        )
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        hits = [p.pattern for p in _FORBIDDEN_IN_SHELL if p.search(text)]
        if hits:
            offenders[name] = hits
    assert not offenders, (
        f"desktop-shell JS now fetches the generative surface: {offenders}; "
        "/api/ui is the terminal/Conky degraded surface, never the desktop boot."
    )


def test_source_guard_static_module_list_covers_every_loaded_script(shell_html):
    """Keep the guard honest: every /shell/static/*.js the rendered shell loads
    must be in the guarded list, so a newly-added module can't smuggle in an
    `/api/ui` fetch unguarded."""
    loaded = set(re.findall(r'src="/shell/static/([^"]+\.js)"', shell_html))
    # Bundled third-party players are not HART shell logic (no generative wiring).
    loaded.discard("lottie.min.js")
    missing = sorted(loaded - set(_SHELL_JS_MODULES))
    assert not missing, (
        f"shell loads JS modules not covered by the /api/ui source-guard: {missing}. "
        "Add them to _SHELL_JS_MODULES so they are frozen too."
    )


# ─────────────────────────────────────────────────────────────────────────
# (b) BEHAVIOURAL: the Conky/terminal degraded surface still renders
# ─────────────────────────────────────────────────────────────────────────
def test_conky_terminal_degraded_surface_still_renders_from_generate_ui():
    """The preserved Tier-4 fallback: with NO model available, `generate_ui`
    must still return a static component tree (this is what /api/ui serves to the
    terminal/Conky degraded surface). Proves the preserved path is intact —
    behavioural: real service, real call, observable structure."""
    svc = LiquidUIService()
    # Force the no-LLM path (the degraded surface) deterministically.
    svc._model_available = False

    ui = svc.generate_ui(context={"system": {"load_1m": 0.5,
                                             "memory_used_percent": 42,
                                             "uptime_hours": 3}})

    assert isinstance(ui, dict), "generate_ui must return a dict envelope"
    assert ui.get("source") == "static", (
        f"with no model, generate_ui must use the static degraded path, "
        f"got source={ui.get('source')!r}"
    )
    components = ui.get("components")
    assert isinstance(components, list) and components, (
        "the degraded surface must still produce at least one component "
        "(the System Status card) — Conky/terminal fallback would be blank otherwise"
    )
    # The static path always emits the System Status card first.
    assert any(c.get("type") == "card" for c in components), (
        "expected at least one 'card' component in the static degraded surface"
    )


def test_api_ui_route_serves_the_degraded_surface_html():
    """End-to-end on the degraded surface: the `/api/ui` route exists and returns
    rendered HTML built from `generate_ui`'s static components — the terminal/
    Conky consumer's real entrypoint, kept alive but NOT on the desktop boot."""
    svc = LiquidUIService()
    svc._model_available = False
    app = svc._create_flask_app()
    app.testing = True
    client = app.test_client()

    r = client.get("/api/ui")
    assert r.status_code == 200, f"/api/ui must serve the degraded surface, got {r.status_code}"
    payload = r.get_json()
    assert payload.get("source") == "static", (
        f"/api/ui degraded surface must be static with no model, got {payload.get('source')!r}"
    )
    assert payload.get("html"), "/api/ui returned empty html — degraded surface is blank"
    assert payload.get("component_count", 0) >= 1, (
        "/api/ui degraded surface rendered zero components"
    )
