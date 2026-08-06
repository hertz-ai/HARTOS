"""CHAPTER 00 -- THE HARNESS'S OWN CONTRACT: what `fake_os` promises the rest
of this suite, asserted before any chapter relies on it.

Every other file here assumes two things that are easy to break and silent when
broken:

  1. NOTHING REACHES THE HOST. The conftest promises "a poweroff/format/nmcli
     test can never touch the machine running the suite". `subprocess` is not
     the only door -- a handler also ASKS ABOUT the host over D-Bus, and native
     D-Bus never goes near `subprocess`. When os_bridge.logind grew its native
     jeepney transport, that promise quietly became false: on any box with a
     reachable system bus the power tests stopped issuing an observable busctl
     argv and started issuing a REAL org.freedesktop.login1 method call. In CI
     polkit denied it and ten tests went red; on a Linux desktop with an active
     local session polkit's shipped default for power-off is "yes".

  2. THE FAKES BEHAVE LIKE WHAT THEY REPLACE. FakeOS.popen once returned a
     types.SimpleNamespace carrying __enter__/__exit__ as INSTANCE attributes.
     Python resolves dunders on the TYPE, so it was not a context manager, and
     `with subprocess.Popen(...)` raised TypeError. The callers are not all
     ours: the stdlib's ctypes.util.find_library opens Popen with `with`, and
     `mss` -- the screenshot handler's Python fallback -- calls it on Linux. One
     harness bug, an unrelated route red.

These are behavioural: they call the real thing and assert the observable
result, so removing a seal fails here first instead of in a distant chapter.
"""
import subprocess

import pytest


# ═════════════════════════════════════════════════════════════════════════════
# ACT 1 -- THE HOST IS UNREACHABLE
# ═════════════════════════════════════════════════════════════════════════════

def test_native_dbus_system_bus_is_refused_under_fake_os(fake_os):
    """The native transport must not be able to open a system bus.

    Asserts the SEAL, not merely "some error happened": the message is the
    fixture's own marker, so this fails both ways it can regress -- if the seal
    is dropped on Linux the real jeepney connects and raises nothing, and on
    Windows it raises a different error entirely.
    """
    from integrations.agent_engine.os_bridge import logind

    with pytest.raises(ConnectionError) as excinfo:
        logind.open_dbus_connection(bus='SYSTEM')
    assert 'no D-Bus system bus' in str(excinfo.value)


def test_logind_call_lands_its_argv_at_the_observable_boundary(fake_os, monkeypatch):
    """With the bus refused, logind_call falls to its documented busctl path --
    which is the boundary FakeOS records. This is the invariant every power and
    session test in chapter 03 spends its assertions on: the argv is VISIBLE.

    `_NATIVE_AVAILABLE` is forced ON so this runs the native-attempt-then-fall-
    back chain even where jeepney is absent. That gap is the whole story: the
    Windows dev box has no jeepney, so it never executed the branch at all,
    while CI installs jeepney 0.9.0 and executed nothing else. A green local run
    meant nothing about the box that was red.
    """
    from integrations.agent_engine.os_bridge import logind

    monkeypatch.setattr(logind, '_NATIVE_AVAILABLE', True)
    ok, err = logind.logind_call('PowerOff', ('b', 'true'))
    assert ok is True and err is None
    assert ['busctl', 'call', '--system',
            'org.freedesktop.login1', '/org/freedesktop/login1',
            'org.freedesktop.login1.Manager', 'PowerOff', 'b', 'true'] in fake_os.calls


def test_a_denied_logind_call_is_never_a_masked_success(fake_os, monkeypatch):
    """The result-checking half of the same hop (#133): rc != 0 must surface as
    (False, reason). A hermetic harness that swallowed this would let a polkit
    denial read as a successful shutdown."""
    from integrations.agent_engine.os_bridge import logind

    monkeypatch.setattr(logind, '_NATIVE_AVAILABLE', True)
    fake_os.rc_for['busctl'] = 1
    ok, err = logind.logind_call('PowerOff', ('b', 'true'))
    assert ok is False
    assert 'denied or failed' in err


# ═════════════════════════════════════════════════════════════════════════════
# ACT 2 -- THE FAKES BEHAVE LIKE WHAT THEY REPLACE
# ═════════════════════════════════════════════════════════════════════════════

def test_faked_popen_supports_the_context_manager_protocol(fake_os):
    """`with subprocess.Popen(...)` must work. Dunders resolve on the type, so
    an object that merely HAS __enter__ is not a context manager."""
    with subprocess.Popen(['echo', 'hi'], stdout=subprocess.PIPE) as proc:
        assert proc.returncode == 0
    assert ['echo', 'hi'] in fake_os.calls


def test_faked_popen_closes_its_streams_on_exit(fake_os):
    """Real Popen.__exit__ closes the pipes before waiting; the fake must too,
    or a `with` block leaves half-open streams behind for the next test."""
    with subprocess.Popen(['ls'], stdout=subprocess.PIPE) as proc:
        streams = (proc.stdout, proc.stderr, proc.stdin)
        assert not any(s.closed for s in streams)
    assert all(s.closed for s in streams)


def test_faked_popen_still_carries_canned_output_and_rc(fake_os):
    """The context-manager fix must not cost the stdout_for / rc_for knobs the
    rest of the suite drives branches with."""
    fake_os.stdout_for['nmcli'] = 'wlan0:connected'
    fake_os.rc_for['nmcli'] = 3
    proc = subprocess.Popen(['nmcli', 'dev'], stdout=subprocess.PIPE, text=True)
    assert proc.returncode == 3
    assert proc.stdout.read() == 'wlan0:connected'
    assert proc.communicate()[0] == 'wlan0:connected'


# ═════════════════════════════════════════════════════════════════════════════
# ACT 3 -- MACHINE IDENTITY IS DECLARED, NOT INHERITED
# ═════════════════════════════════════════════════════════════════════════════

def test_desktop_session_env_is_not_inherited_from_the_developers_box(fake_os):
    """_is_wayland() reads these; if the suite inherited them, a developer
    running inside a Wayland session would drive a different wallpaper/display
    branch than CI, and neither would be the branch the test names."""
    import os as _os

    assert _os.environ.get('WAYLAND_DISPLAY') is None
    assert _os.environ.get('XDG_SESSION_TYPE') is None


def test_firmware_capability_is_the_declared_one_not_the_hosts(fake_os):
    """The probe is a pure sysfs read, so unpinned it answers True on a UEFI
    Linux runner and False on the BIOS/Windows dev box -- the branch flips
    under the test's feet. It must answer the fake machine's declaration."""
    from integrations.agent_engine import shell_os_apis

    assert shell_os_apis.firmware_setup_supported() is False
    fake_os.uefi_firmware_setup = True
    assert shell_os_apis.firmware_setup_supported() is True
