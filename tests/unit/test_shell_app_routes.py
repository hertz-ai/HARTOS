"""The shell's app routes must call AppInstaller with its REAL signatures, on the
ONE consolidated surface.

Phase-8 consolidation: search / installed / install / uninstall used to be defined
TWICE — once in shell_os_apis on /api/apps/* (with a call-shape bug) and once in
app_installer on /api/shell/apps/*. They are now a SINGLE implementation owned by
``register_app_install_routes``, registered on BOTH the canonical
``/api/shell/apps/*`` prefix and the legacy ``/api/apps/*`` alias. These tests are
behavioural: they register the REAL routes and mock ONLY the installer boundary
(``get_installer``), asserting the correct call shapes and that BOTH prefixes hit
the same implementation (no grep/source-shape assertions).
"""
import flask
import pytest
from unittest.mock import patch, MagicMock

import integrations.agent_engine.app_installer as appinst


@pytest.fixture
def client(monkeypatch):
    # Pass-through the auth decorator BEFORE the routes are defined so the
    # protected install/uninstall routes are reachable in-test. app_installer
    # imports _require_shell_auth from shell_os_apis at register time, so patch
    # there.
    import integrations.agent_engine.shell_os_apis as soa
    monkeypatch.setattr(soa, "_require_shell_auth", lambda f: f, raising=False)
    app = flask.Flask(__name__)
    appinst.register_app_install_routes(app)
    app.testing = True
    return app.test_client()


def _mock_installer(monkeypatch):
    """Replace the get_installer() singleton with a MagicMock for the test."""
    inst = MagicMock()
    monkeypatch.setattr(appinst, "get_installer", lambda: inst)
    return inst


@pytest.mark.parametrize("prefix", ["/api/shell/apps", "/api/apps"])
def test_search_passes_platforms_list(client, monkeypatch, prefix):
    inst = _mock_installer(monkeypatch)
    inst.search.return_value = [{"name": "VLC", "id": "vlc"}]
    # Legacy single `platform` param is normalised to a 1-list (back-compat).
    r = client.get(prefix + "/search?q=vlc&platform=flatpak&limit=5")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["results"] == [{"name": "VLC", "id": "vlc"}]
    inst.search.assert_called_once_with("vlc", ["flatpak"])


@pytest.mark.parametrize("prefix", ["/api/shell/apps", "/api/apps"])
def test_search_passes_comma_platforms(client, monkeypatch, prefix):
    inst = _mock_installer(monkeypatch)
    inst.search.return_value = []
    r = client.get(prefix + "/search?q=x&platforms=nix,flatpak")
    assert r.status_code == 200
    inst.search.assert_called_once_with("x", ["nix", "flatpak"])


@pytest.mark.parametrize("prefix", ["/api/shell/apps", "/api/apps"])
def test_installed_calls_list_installed_with_no_args(client, monkeypatch, prefix):
    inst = _mock_installer(monkeypatch)
    inst.list_installed.return_value = [{"name": "FF", "platform": "nix"}]
    r = client.get(prefix + "/installed")
    assert r.status_code == 200
    inst.list_installed.assert_called_once_with()


@pytest.mark.parametrize("prefix", ["/api/shell/apps", "/api/apps"])
def test_install_builds_InstallRequest_and_serializes_result(client, monkeypatch, prefix):
    from integrations.agent_engine.app_installer import InstallResult
    inst = _mock_installer(monkeypatch)
    inst.install.return_value = InstallResult(
        success=True, platform="flatpak", name="Firefox",
        app_id="org.mozilla.firefox")
    r = client.post(prefix + "/install",
                    json={"source": "firefox", "platform": "flatpak"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["success"] is True and body["app_id"] == "org.mozilla.firefox"
    req = inst.install.call_args.args[0]      # the InstallRequest
    assert req.source == "firefox" and req.platform.value == "flatpak"


@pytest.mark.parametrize("prefix", ["/api/shell/apps", "/api/apps"])
def test_uninstall_calls_installer(client, monkeypatch, prefix):
    from integrations.agent_engine.app_installer import InstallResult
    inst = _mock_installer(monkeypatch)
    inst.uninstall.return_value = InstallResult(
        success=True, platform="nix", name="htop")
    r = client.post(prefix + "/uninstall", json={"app_id": "htop", "platform": "nix"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["success"] is True
    # uninstall() gained a 3rd `options` arg (symmetric-uninstall work, afceff9);
    # the route forwards data.get('options', {}), so an options-less request
    # passes {} positionally.
    inst.uninstall.assert_called_once_with("htop", "nix", {})


def test_install_is_auth_gated_on_canonical_surface():
    """The canonical /api/shell/apps/install MUST pass _require_shell_auth — the
    gate the legacy /api/apps/* surface had but the canonical one previously
    lacked. A non-local request with no shell token is refused 403."""
    app = flask.Flask(__name__)
    appinst.register_app_install_routes(app)  # real (un-patched) auth
    app.testing = True
    c = app.test_client()
    # Spoof a non-local remote addr so _shell_auth_check's localhost allowance
    # does not apply and the token path (no token configured) refuses.
    r = c.post("/api/shell/apps/install", json={"source": "x"},
               environ_overrides={"REMOTE_ADDR": "203.0.113.7"})
    assert r.status_code == 403, r.get_data(as_text=True)


def test_register_is_idempotent():
    """Registering twice on one app must not raise a duplicate-endpoint error."""
    app = flask.Flask(__name__)
    appinst.register_app_install_routes(app)
    appinst.register_app_install_routes(app)  # second call is a no-op
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/shell/apps/install" in rules
    assert "/api/apps/install" in rules
