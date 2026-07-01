"""
Typed Shell<->OS bridge contract (#133 / W3) — the ONE schema-defined surface the
WebView SDK (static/hartOSBridge.js) and the OS server (os_bridge/routes.py) share.

Instead of stringly-typed REST + subprocess shell-outs, every shell<->OS operation
is a TYPED op: ``(domain, op, typed params) -> native handler -> result-checked
{ok, ...}``. The contract is SELF-DESCRIBING (``describe_contract()``) so the SDK /
UI can introspect it — "every customization is an API; the API is the SDK"
(HART OS full-spec rule 15).

FIRST vertical slice (this file): the ``power`` domain, implemented NATIVELY via
logind D-Bus (os_bridge/power.py -> os_bridge/logind.py). The ``disk`` / ``network``
/ ``display`` domains are DECLARED as PLANNED (not yet implemented) in
``_PLANNED_DOMAINS`` so the shape is visible and the SDK can show an honest
"not yet"; they are the documented NEXT slices (udisks2 / NetworkManager /
compositor-IPC).
"""


class OpParam:
    """One typed parameter of an op.

    ``type`` is a JSON-ish type name ('string' | 'bool' | 'int'). ``choices`` (if
    set) constrains a string to a fixed set. This is deliberately small — the power
    domain has no params; the machinery exists for the NEXT domains
    (disk.mount(device=...), network.connect(ssid=..., password=...)).
    """

    __slots__ = ('name', 'type', 'required', 'choices', 'summary')

    def __init__(self, name, type='string', required=True, choices=None, summary=''):
        self.name = name
        self.type = type
        self.required = required
        self.choices = list(choices) if choices else None
        self.summary = summary

    def to_dict(self):
        d = {'name': self.name, 'type': self.type, 'required': self.required}
        if self.choices:
            d['choices'] = self.choices
        if self.summary:
            d['summary'] = self.summary
        return d


class OpSpec:
    """One typed op in a domain."""

    __slots__ = ('name', 'summary', 'params', 'destructive', 'capability')

    def __init__(self, name, summary, params=(), destructive=False, capability=None):
        self.name = name
        self.summary = summary
        self.params = list(params)
        self.destructive = destructive
        # ``capability`` names a runtime gate the HANDLER must check before running
        # (e.g. 'firmware_setup' -> firmware_setup_supported()). The contract only
        # DECLARES it; the handler enforces it.
        self.capability = capability

    def to_dict(self):
        return {
            'name': self.name,
            'summary': self.summary,
            'params': [p.to_dict() for p in self.params],
            'destructive': self.destructive,
            'capability': self.capability,
        }


# ═══════════════════════════════════════════════════════════════════════════
# IMPLEMENTED domain: power (native via logind D-Bus)
# ═══════════════════════════════════════════════════════════════════════════

_POWER_OPS = {
    'reboot': OpSpec('reboot', 'Restart the machine now.'),
    'shutdown': OpSpec('shutdown', 'Power the machine off now.'),
    'suspend': OpSpec('suspend', 'Suspend to RAM (sleep).'),
    'hibernate': OpSpec('hibernate', 'Hibernate to disk.'),
    'lock': OpSpec('lock', 'Lock all sessions.'),
    'firmware_setup': OpSpec(
        'firmware_setup',
        'Restart into the firmware (UEFI) setup on the next boot.',
        capability='firmware_setup'),
}

# ═══════════════════════════════════════════════════════════════════════════
# IMPLEMENTED domain: apps (cross-subsystem app-integration bridge, #117 / W3)
# ═══════════════════════════════════════════════════════════════════════════
#
# The typed surface for the app-integration SDK. ``launch`` routes to the EXISTING
# cross-subsystem dispatch in app_bridge_service (gtk-launch for linux, Wine for
# windows, Android ``am`` for android) — declarative OpSpecs only, NO new IPC.
# ``list`` / ``focus`` / ``close`` reuse the canonical window-manager client
# (hart_wm_client) so there is ONE window-op path (``close`` stays the fail-closed
# constitutional gate). Every op is result-checked — a non-zero launcher exit or a
# refused close surfaces ``ok:false``, never a masked success.

_APPS_OPS = {
    'launch': OpSpec(
        'launch',
        'Launch an installed app by id (cross-subsystem).',
        params=(
            OpParam('app_id', 'string', required=True,
                    summary='Desktop/app id: a gtk .desktop id (linux), a Wine exe '
                            '(windows), or an Android activity/package (android).'),
            OpParam('subsystem', 'string', required=False,
                    choices=('linux', 'windows', 'android'),
                    summary='Which subsystem launcher to use (default: linux).'),
        )),
    'list': OpSpec('list', 'List the currently open app windows.'),
    'focus': OpSpec(
        'focus',
        'Focus/raise an open window by its compositor window id.',
        params=(OpParam('window_id', 'int', required=True,
                        summary='Compositor con_id of the window to focus.'),)),
    'close': OpSpec(
        'close',
        'Close an open window by its compositor window id.',
        params=(OpParam('window_id', 'int', required=True,
                        summary='Compositor con_id of the window to close.'),),
        destructive=True),
}


# The one registry of IMPLEMENTED domains -> {op_name: OpSpec}. The router in
# routes.py dispatches only these; anything else is unknown (400) or planned (501).
_DOMAINS = {
    'power': {
        'native_backend': 'org.freedesktop.login1 (logind D-Bus)',
        'ops': _POWER_OPS,
    },
    'apps': {
        'native_backend': 'app_bridge_service (gtk-launch / Wine / Android am) + '
                          'hart_wm_client (com.hart.Compositor window ops)',
        'ops': _APPS_OPS,
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# PLANNED domains — the documented NEXT slices. DECLARED (not implemented) so the
# SDK/UI can introspect the roadmap and show an honest "not yet" (routes.py returns
# 501 for these). Each will migrate its stringly-REST + subprocess surface
# (shell_system_apis / shell_desktop_apis) onto a native daemon the same way power
# migrated onto logind.
# ═══════════════════════════════════════════════════════════════════════════

_PLANNED_DOMAINS = {
    'disk': {
        'native_backend': 'org.freedesktop.UDisks2 (udisks2 D-Bus)',
        'replaces': 'shell_system_apis: nmcli-free block ops (lsblk/mkfs/fsck/'
                    'fstrim/mount via udisks2 Manager + Block/Filesystem ifaces)',
        'example_ops': ['list', 'mount', 'unmount', 'format', 'fsck', 'trim'],
        'notes': 'udisks2 authorizes through the caller session polkit (same model '
                 'as logind); the destructive ops (format/resize) keep the existing '
                 'confirm + protected-device gates.',
    },
    'network': {
        'native_backend': 'org.freedesktop.NetworkManager (NM D-Bus)',
        'replaces': 'shell_system_apis: the ~20 nmcli shell-outs (wifi status/scan/'
                    'connect/toggle, saved, vpn up/down/import/delete)',
        'example_ops': ['wifi_status', 'wifi_scan', 'wifi_connect', 'wifi_forget',
                        'vpn_up', 'vpn_down'],
        'notes': 'NM AddAndActivateConnection / ActivateConnection over the system '
                 'bus; polkit grant already added in hart-base.nix (#149). Long '
                 'joins stay out-of-band (the bounded-worker discipline).',
    },
    'display': {
        'native_backend': 'com.hart.Compositor IPC (+ wlr-output-management fallback)',
        'replaces': 'shell_system_apis: swaymsg/xrandr display rotation + '
                    'shell_desktop_apis output/DPI ops',
        'example_ops': ['list_outputs', 'set_rotation', 'set_scale', 'set_mode'],
        'notes': 'The compositor already owns the output tree; a typed IPC op is the '
                 'native path (no swaymsg/xrandr subprocess). Keeps the #132 never-'
                 'black / pixman floor invariant.',
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

def get_op(domain, op):
    """Return the ``OpSpec`` for an IMPLEMENTED ``domain.op``, or ``None`` (unknown
    or planned-not-implemented)."""
    d = _DOMAINS.get(domain)
    if not d:
        return None
    return d['ops'].get(op)


def is_implemented(domain):
    """True iff ``domain`` has native handlers wired now."""
    return domain in _DOMAINS


def is_planned(domain):
    """True iff ``domain`` is declared as a planned (not-yet-implemented) NEXT slice."""
    return domain in _PLANNED_DOMAINS


def validate_params(spec, params):
    """Validate ``params`` (a dict) against an ``OpSpec``.

    Returns ``(True, cleaned_dict)`` on success, ``(False, error_str)`` on the first
    violation. Unknown keys are rejected (no silent drift); missing required params
    and bad ``choices`` fail. Power ops declare no params, so ``{}`` passes.
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return False, 'params must be an object'
    declared = {p.name: p for p in spec.params}
    for key in params:
        if key not in declared:
            return False, "unknown param: '{}'".format(key)
    cleaned = {}
    for p in spec.params:
        if p.name not in params:
            if p.required:
                return False, "missing required param: '{}'".format(p.name)
            continue
        val = params[p.name]
        if p.type == 'bool' and not isinstance(val, bool):
            return False, "param '{}' must be a bool".format(p.name)
        if p.type == 'int' and not isinstance(val, int):
            return False, "param '{}' must be an int".format(p.name)
        if p.type == 'string' and not isinstance(val, str):
            return False, "param '{}' must be a string".format(p.name)
        if p.choices is not None and val not in p.choices:
            return False, "param '{}' must be one of {}".format(p.name, p.choices)
        cleaned[p.name] = val
    return True, cleaned


def describe_contract():
    """Self-describing manifest of the whole bridge — implemented + planned. The SDK
    / UI GETs this (/api/os/contract) to introspect what ops exist and their shapes.
    """
    domains = {}
    for name, d in _DOMAINS.items():
        domains[name] = {
            'implemented': True,
            'native_backend': d['native_backend'],
            'ops': [spec.to_dict() for spec in d['ops'].values()],
        }
    for name, p in _PLANNED_DOMAINS.items():
        domains[name] = {
            'implemented': False,
            'native_backend': p['native_backend'],
            'planned': True,
            'replaces': p.get('replaces', ''),
            'example_ops': p.get('example_ops', []),
            'notes': p.get('notes', ''),
        }
    return {'version': 1, 'domains': domains}
