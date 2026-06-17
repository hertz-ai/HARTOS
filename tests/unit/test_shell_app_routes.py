"""The shell's /api/apps/* routes must call AppInstaller with its REAL signatures.

Regression: the routes called install(source, platform=, name=),
search(query, platform=, limit=) and list_installed(platform=) — none of which
match AppInstaller (install(req: InstallRequest), search(query, platforms=),
list_installed()) — and imported the nonexistent get_app_registry(). Every call
raised TypeError/ImportError that a broad `except` swallowed, so the app store
silently returned nothing and install was a no-op that still reported 200.

These are behavioural: they register the REAL routes and mock ONLY the installer
boundary, asserting the correct call shapes (no grep/source-shape assertions).
"""
import flask
import pytest
from unittest.mock import patch

import integrations.agent_engine.shell_os_apis as soa


@pytest.fixture
def client(monkeypatch):
    # Pass-through the auth decorator BEFORE the routes are defined so the
    # protected install route is reachable in-test.
    monkeypatch.setattr(soa, "_require_shell_auth", lambda f: f, raising=False)
    app = flask.Flask(__name__)
    soa.register_shell_os_routes(app)
    app.testing = True
    return app.test_client()


def test_search_passes_platforms_list_not_platform_kwarg(client):
    with patch("integrations.agent_engine.app_installer.AppInstaller") as M:
        M.return_value.search.return_value = [{"name": "VLC", "id": "vlc"}]
        r = client.get("/api/apps/search?q=vlc&platform=flatpak&limit=5")
        assert r.status_code == 200
        assert r.get_json()["results"] == [{"name": "VLC", "id": "vlc"}]
        M.return_value.search.assert_called_once_with("vlc", platforms=["flatpak"])


def test_installed_calls_list_installed_with_no_args(client):
    with patch("integrations.agent_engine.app_installer.AppInstaller") as M:
        M.return_value.list_installed.return_value = [{"name": "FF", "platform": "nix"}]
        r = client.get("/api/apps/installed")
        assert r.status_code == 200
        M.return_value.list_installed.assert_called_once_with()


def test_install_builds_InstallRequest_and_serializes_result(client):
    from integrations.agent_engine.app_installer import InstallResult
    with patch("integrations.agent_engine.app_installer.AppInstaller") as M:
        M.return_value.install.return_value = InstallResult(
            success=True, platform="flatpak", name="Firefox",
            app_id="org.mozilla.firefox")
        r = client.post("/api/apps/install",
                        json={"source": "firefox", "platform": "flatpak"})
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        assert body["success"] is True and body["app_id"] == "org.mozilla.firefox"
        req = M.return_value.install.call_args.args[0]      # the InstallRequest
        assert req.source == "firefox" and req.platform.value == "flatpak"
