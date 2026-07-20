"""
Power domain — native handlers binding the contract's power ops to logind D-Bus
(#133 / W3).

Each op maps to an ``org.freedesktop.login1.Manager`` method, invoked + RESULT-
CHECKED via ``os_bridge.logind.logind_call`` (native jeepney D-Bus first, bounded
``busctl`` fallback). A polkit denial / failure surfaces as a REAL error payload +
500, NEVER a masked ``{'initiated': True}`` (the bug #133 fixes).

The ``firmware_setup`` op is the two-step "restart into UEFI setup": arm
``SetRebootToFirmwareSetup(true)`` THEN ``Reboot(true)`` — gated on the firmware
capability (a legacy-BIOS box must not get a plain reboot when it asked for setup).
It reuses ``shell_os_apis.firmware_setup_supported`` (the canonical efivar probe) —
no second copy.
"""

from integrations.agent_engine.os_bridge.logind import logind_call

# login1 Manager method per single-step power op. ONE source of truth for the map.
# The interactive boolean is 'true' so logind may consult polkit (the hart-base.nix
# grant then authorizes it without a prompt).
_POWER_METHOD = {
    'reboot': 'Reboot',
    'shutdown': 'PowerOff',
    'suspend': 'Suspend',
    'hibernate': 'Hibernate',
}


def _firmware_supported():
    """The canonical UEFI boot-to-firmware capability probe (efivar read). Lazy
    import to avoid any import-order coupling and to keep the ONE copy in
    shell_os_apis (imported by liquid_ui_service + the shell power route)."""
    from integrations.agent_engine.shell_os_apis import firmware_setup_supported
    return firmware_setup_supported()


def invoke_power(op, params=None):
    """Run a power op natively. Returns ``(status_code, payload_dict)``.

    RESULT-CHECKED: a denial / failure yields ``{'ok': False, ..., 'error': ...}`` +
    500 (or 400 for a capability gate), never a masked success.
    """
    if op in _POWER_METHOD:
        ok, err = logind_call(_POWER_METHOD[op], ('b', 'true'))
        if not ok:
            return 500, {'ok': False, 'op': op, 'error': err}
        return 200, {'ok': True, 'op': op}

    if op == 'lock':
        ok, err = logind_call('LockSessions')
        if not ok:
            return 500, {'ok': False, 'op': op, 'error': err}
        return 200, {'ok': True, 'op': op}

    if op == 'firmware_setup':
        # Never give the user a plain reboot when they asked to enter firmware setup.
        if not _firmware_supported():
            return 400, {'ok': False, 'op': op,
                         'error': 'Reboot to firmware setup is not supported on this '
                                  'system (legacy BIOS or capability not advertised)'}
        ok, err = logind_call('SetRebootToFirmwareSetup', ('b', 'true'))
        if not ok:
            return 500, {'ok': False, 'op': op,
                         'error': 'Could not arm firmware setup: {}'.format(err)}
        # Two-step: only reboot AFTER the flag armed successfully.
        ok, err = logind_call('Reboot', ('b', 'true'))
        if not ok:
            return 500, {'ok': False, 'op': op, 'error': err}
        return 200, {'ok': True, 'op': op}

    return 400, {'ok': False, 'op': op, 'error': 'unknown power op: {}'.format(op)}


def power_capabilities():
    """Which power ops this box offers. reboot/shutdown/suspend/hibernate/lock are
    always OFFERED (real authorization is proven at call time by logind+polkit);
    ``firmware_setup`` is gated on the UEFI efivar probe so a legacy-BIOS box hides
    it. Best-effort + degrade-safe."""
    caps = {op: True for op in _POWER_METHOD}
    caps['lock'] = True
    try:
        caps['firmware_setup'] = bool(_firmware_supported())
    except Exception:
        caps['firmware_setup'] = False
    return caps
