"""Feature 3 — "Restart into Firmware (UEFI)" power action, end-to-end through
the REAL LiquidUIService Flask app.

The shell power menu gains a "Restart to Firmware (UEFI)" action that runs
`systemctl reboot --firmware-setup` (sets the UEFI OsIndications boot-to-firmware
flag so the next reboot enters BIOS/UEFI setup). It must:
  - appear in the rendered shell (hidden by default; revealed by the capability
    probe), wired to shellAction('firmware'),
  - expose a capability endpoint the JS gates on (so legacy BIOS hides it),
  - POST /api/shell/session/firmware -> spawn `systemctl reboot --firmware-setup`
    on a capable box, and REFUSE (no reboot) on a non-capable box.

Behavioural (real app + real route + mocked subprocess/capability boundary), not
source-shape — per the no-grep-tests rule.
"""
import re
from unittest.mock import patch

import pytest

from integrations.agent_engine.liquid_ui_service import LiquidUIService

_LUS = 'integrations.agent_engine.liquid_ui_service'
_OS_APIS = 'integrations.agent_engine.shell_os_apis'


@pytest.fixture
def shell():
    svc = LiquidUIService()
    app = svc._create_flask_app()
    app.testing = True
    return svc, app.test_client()


def test_power_menu_has_firmware_button_wired_to_firmware_action(shell):
    """The rendered shell must offer a power button that calls
    shellAction('firmware') — the entry point for the UEFI restart."""
    svc, _client = shell
    html = svc.render_desktop_shell()
    assert "shellAction('firmware')" in html, \
        "power menu missing the Restart-to-Firmware button"
    # It is HIDDEN by default (revealed only on a capable UEFI box).
    btn = re.search(r'<div class="power-btn" id="power-btn-firmware"[^>]*>', html)
    assert btn, "firmware power button missing its id"
    assert "display:none" in btn.group(0), \
        "firmware button must be hidden until the capability probe reveals it"
    # The shell wires shellAction to handle the firmware label.
    assert "firmware:'restart into the Firmware (UEFI) setup'" in html


def test_firmware_capable_endpoint_reflects_capability(shell):
    """GET /api/shell/session/firmware-capable returns the canonical probe so the
    JS can SHOW the button only on a capable box."""
    _svc, client = shell
    with patch(f'{_OS_APIS}.firmware_setup_supported', return_value=True):
        r = client.get('/api/shell/session/firmware-capable')
        assert r.status_code == 200
        assert r.get_json()['supported'] is True
    with patch(f'{_OS_APIS}.firmware_setup_supported', return_value=False):
        r = client.get('/api/shell/session/firmware-capable')
        assert r.get_json()['supported'] is False


def test_session_firmware_runs_reboot_firmware_setup_when_capable(shell):
    """POST /api/shell/session/firmware -> `systemctl reboot --firmware-setup`
    on a capable box, via the same privileged path the other power actions use."""
    _svc, client = shell
    with patch(f'{_OS_APIS}.firmware_setup_supported', return_value=True), \
            patch(f'{_LUS}.subprocess.Popen') as popen:
        r = client.post('/api/shell/session/firmware')
    assert r.status_code == 200
    popen.assert_called_once()
    assert popen.call_args[0][0] == ['systemctl', 'reboot', '--firmware-setup']


def test_session_firmware_refused_when_not_capable(shell):
    """On legacy BIOS the firmware action is refused (400) and NEVER reboots — the
    user must not get a plain reboot when they asked for firmware setup."""
    _svc, client = shell
    with patch(f'{_OS_APIS}.firmware_setup_supported', return_value=False), \
            patch(f'{_LUS}.subprocess.Popen') as popen:
        r = client.post('/api/shell/session/firmware')
    assert r.status_code == 400
    popen.assert_not_called()


def test_session_rejects_unknown_action(shell):
    """The session route still rejects unknown actions (no new injection surface)."""
    _svc, client = shell
    r = client.post('/api/shell/session/destroy')
    assert r.status_code == 400
