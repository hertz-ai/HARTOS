"""
apps domain — native handlers binding the contract's apps ops to the EXISTING
app-integration dispatch (#117 / W3).

The typed app-integration bridge. Each op returns ``(status_code, payload_dict)``
(the same shape as ``power.invoke_power``), result-checked so a failure is never a
masked success:

  * ``launch`` -> the cross-subsystem dispatch in ``app_bridge_service``
    (gtk-launch for linux, Wine for windows, Android ``am`` for android). ONE
    launcher path, in-process, NO new IPC.
  * ``list`` / ``focus`` / ``close`` -> the canonical window-manager client
    (``hart_wm_client``) so there is ONE window-op path across the shell, agents and
    MCP. ``close`` stays the fail-closed constitutional gate (an agent closing a real
    window is governed exactly like the shell closing one).

This module is DELIBERATELY thin: it maps a typed op to the right canonical home and
tags the result. The contract (contract._APPS_OPS) already validated the params, so
here ``app_id`` / ``window_id`` are present and well-typed.
"""

# The actor recorded for the (audited, fail-closed) window ops the shell drives.
_SHELL_ACTOR = 'shell'


def _tag(res, op):
    """Normalise a handler result into the ``{ok, op, ...}`` envelope."""
    out = dict(res or {})
    out['op'] = op
    out['ok'] = bool(out.get('ok'))
    return out


def invoke_apps(op, params=None):
    """Run an apps op. Returns ``(status_code, payload_dict)``.

    A failed launch / refused close yields a real error + 500 (400 for a bad op),
    never a masked ``{'ok': True}``.
    """
    params = params or {}

    if op == 'launch':
        from integrations.agent_engine.app_bridge_service import get_app_bridge
        res = get_app_bridge().launch_app(
            params.get('app_id', ''), params.get('subsystem', 'linux'))
        return (200 if res.get('ok') else 500), _tag(res, 'launch')

    if op in ('list', 'focus', 'close'):
        from integrations.agent_engine.hart_wm_client import get_wm_client
        wm = get_wm_client()
        if op == 'list':
            res = wm.dispatch_verb('window.list', {}, _SHELL_ACTOR)
        elif op == 'focus':
            res = wm.dispatch_verb(
                'window.focus', {'con_id': params.get('window_id')}, _SHELL_ACTOR)
        else:  # close — fail-closed constitutional gate lives in the WM client
            res = wm.dispatch_verb(
                'window.close', {'con_id': params.get('window_id')}, _SHELL_ACTOR)
        return (200 if res.get('ok') else 500), _tag(res, op)

    return 400, {'ok': False, 'op': op, 'error': 'unknown apps op: {}'.format(op)}
