"""
os_bridge — the TYPED + NATIVE Shell<->OS bridge for HART OS (#133 / W3).

HART OS *is* the operating system: the shell UI (a WebView) and the OS server code
run on the SAME machine. The steward's directive (2026-06-29) is that every
shell<->OS operation must be a NATIVE, TYPED op executed natively in the OS —
`hartOS.power.reboot()` -> a schema-checked op -> a native OS call (logind D-Bus,
udisks, NetworkManager) that is RESULT-CHECKED — NOT stringly-typed REST that
`subprocess`-shells-out and fire-and-forgets the result.

This package is the FIRST vertical slice of that bridge:

  * ``contract``  — the ONE self-describing op schema shared by the WebView SDK
                    (static/hartOSBridge.js) and the OS server (routes.py).
  * ``logind``    — the canonical NATIVE logind (org.freedesktop.login1) client:
                    a jeepney D-Bus call first, a bounded/result-checked ``busctl``
                    subprocess only as a degrade fallback. No fire-and-forget.
  * ``power``     — the power domain handlers (reboot/shutdown/suspend/hibernate/
                    lock/firmware-setup) binding the contract to ``logind``.
  * ``routes``    — ``register_os_bridge_routes(app)``: one typed bridge endpoint
                    (POST /api/os/invoke) + the self-describing contract + power
                    capabilities. NOT a new magic-string-per-verb REST surface.

The disk / network / display domains are DECLARED (as PLANNED) in ``contract`` so
the shape is visible; they are the documented NEXT slices (udisks2 /
NetworkManager / compositor-IPC). See contract._PLANNED_DOMAINS.
"""
