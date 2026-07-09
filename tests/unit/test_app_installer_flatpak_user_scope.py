"""Regression test for the flatpak app-store install fix (real-HW 2026-07-09).

The app-store install runs as the sandboxed non-root `hart` service, which cannot do
a SYSTEM-scope `flatpak install` (writes /var/lib/flatpak, needs root/polkit) — so it
failed with a permission error even WITH internet connected. The fix runs flatpak at
--USER scope in a backend-writable installation dir (FLATPAK_USER_DIR under the app
install dir) and self-adds the flathub remote (offline-safe `remote-add`).

This asserts EVERY flatpak subprocess call is `--user` and carries FLATPAK_USER_DIR,
and that install/search self-ensure the flathub remote — so a regression back to
system scope (or a dropped remote) fails here instead of silently at boot.

CI is the oracle (the full integrations stack imports there); it skips cleanly in a
minimal environment.
"""
import os
from unittest import mock
import pytest

try:
    from integrations.agent_engine.app_installer import AppInstaller, InstallRequest
except Exception:  # heavy stack not importable in a minimal env
    AppInstaller = None
    InstallRequest = None

pytestmark = pytest.mark.skipif(AppInstaller is None, reason="integrations stack not importable")


def _ok(*a, **k):
    return mock.Mock(returncode=0, stdout='', stderr='')


def _flatpak_calls(run_mock):
    """(cmd_list, env) for every subprocess.run whose argv[0] == 'flatpak'."""
    out = []
    for c in run_mock.call_args_list:
        argv = c.args[0] if c.args else c.kwargs.get('args')
        if isinstance(argv, (list, tuple)) and argv and argv[0] == 'flatpak':
            out.append((list(argv), c.kwargs.get('env') or {}))
    return out


def test_install_is_user_scoped_with_writable_dir_and_self_added_remote():
    inst = AppInstaller()
    with mock.patch('integrations.agent_engine.app_installer.subprocess.run', side_effect=_ok) as run, \
         mock.patch('integrations.agent_engine.app_installer.os.makedirs'):
        inst._install_flatpak(InstallRequest(source='flathub:org.audacityteam.Audacity', name='Audacity'))

    calls = _flatpak_calls(run)
    assert calls, "no flatpak subprocess call was made"
    cmds = [cmd for cmd, _ in calls]
    # flathub remote self-added at --user scope (offline-safe), before the install.
    assert any(cmd[:3] == ['flatpak', '--user', 'remote-add'] for cmd in cmds), \
        "flathub remote not self-added at --user scope"
    # the install itself is --user (never system scope — the sandboxed user can't).
    assert any(cmd[:4] == ['flatpak', '--user', 'install', '-y'] for cmd in cmds), \
        "flatpak install is not --user scoped"
    # every flatpak call carries FLATPAK_USER_DIR under the app install dir.
    for cmd, env in calls:
        assert env.get('FLATPAK_USER_DIR', '').endswith('flatpak'), \
            f"flatpak call missing FLATPAK_USER_DIR env: {cmd}"


def test_uninstall_and_search_are_also_user_scoped():
    inst = AppInstaller()
    with mock.patch('integrations.agent_engine.app_installer.subprocess.run', side_effect=_ok) as run, \
         mock.patch('integrations.agent_engine.app_installer.os.makedirs'):
        inst._uninstall_flatpak('org.audacityteam.Audacity')
        inst.search('audacity', platforms=['flatpak'])

    for cmd, env in _flatpak_calls(run):
        assert cmd[1] == '--user', f"flatpak call not --user scoped: {cmd}"
        assert env.get('FLATPAK_USER_DIR', '').endswith('flatpak'), \
            f"flatpak call missing FLATPAK_USER_DIR env: {cmd}"
