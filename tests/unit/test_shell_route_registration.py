"""Regression test for #18 — app-store / system APIs dropped by duplicate Flask routes.

The canonical shell route modules (shell_os, shell_desktop, shell_system,
app_installer, os_bridge) must all register on ONE Flask app WITHOUT an endpoint
collision. Historically a duplicate-endpoint ``AssertionError`` ("overwriting an
existing endpoint function") aborted a shared registration block mid-way and
silently dropped every route after it — most visibly the app store (/api/apps/*)
and the OS bridge (/api/os/*), which read to the user as "url not working".

liquid_ui_service now registers each module in its OWN try/except so one module's
failure can never cascade into the siblings. This test guards the other half of
the fix: that the canonical modules are collision-free WITH EACH OTHER, so a
clean run drops nothing in the first place. A regression (someone re-introducing
a duplicate endpoint across two of these modules) fails here instead of silently
vanishing at boot.

CI is the oracle for this test (the full Flask + integrations stack is present
there); it skips cleanly in a minimal environment without Flask.
"""
import pytest

flask = pytest.importorskip("flask")


def _canonical_registrars():
    from integrations.agent_engine.shell_os_apis import register_shell_os_routes
    from integrations.agent_engine.shell_desktop_apis import (
        register_shell_desktop_routes)
    from integrations.agent_engine.shell_system_apis import (
        register_shell_system_routes)
    from integrations.agent_engine.app_installer import register_app_install_routes
    from integrations.agent_engine.os_bridge.routes import register_os_bridge_routes
    # Same order liquid_ui_service registers them.
    return [
        register_shell_os_routes,
        register_shell_desktop_routes,
        register_shell_system_routes,
        register_app_install_routes,
        register_os_bridge_routes,
    ]


def test_canonical_shell_routes_register_without_collision():
    """All five canonical shell modules co-register on one app with no clash."""
    app = flask.Flask(__name__)
    for register in _canonical_registrars():
        # A duplicate-endpoint collision raises AssertionError here — which is
        # exactly the #18 failure we must never re-introduce.
        register(app)

    rules = {r.rule for r in app.url_map.iter_rules()}
    # The app store (app_installer) — the most visible #18 casualty — is present.
    assert any(r.startswith('/api/apps') for r in rules), \
        "app-store routes (/api/apps/*) missing after registration"
    # The OS bridge registers LAST; its presence proves the chain never aborted
    # mid-way (the cascade that historically dropped everything after a clash).
    assert '/api/os/invoke' in rules, "os-bridge /api/os/invoke dropped"
    # System APIs (shell_system) survived too.
    assert '/api/shell/tasks/processes' in rules, "shell-system routes dropped"
