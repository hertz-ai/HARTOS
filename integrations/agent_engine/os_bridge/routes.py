"""
os_bridge routes — the ONE typed Shell<->OS bridge endpoint (#133 / W3).

Registers, on the shell Flask app:
  * ``GET  /api/os/contract``            — the self-describing op manifest.
  * ``GET  /api/os/power/capabilities``  — which power ops this box offers.
  * ``POST /api/os/invoke``              — the typed dispatcher: ``{domain, op,
    params}`` -> validate against the contract -> native handler -> result-checked
    ``{ok, ...}``.

This is deliberately a SINGLE typed endpoint, not a new magic-string-per-verb REST
surface: the shell calls ``hartOS.power.reboot()`` (SDK) -> ``{domain:'power',
op:'reboot'}`` -> ``invoke_power`` -> ``logind`` D-Bus. Auth + audit reuse the
existing shell helpers (one auth path, one audit log — no parallel path).

Register from the server init alongside the other shell route modules:
    from integrations.agent_engine.os_bridge.routes import register_os_bridge_routes
    register_os_bridge_routes(app)
"""

import logging

logger = logging.getLogger('hevolve.shell.os_bridge')


def register_os_bridge_routes(app):
    """Register the typed Shell<->OS bridge routes on a Flask app."""
    from flask import jsonify, request

    from integrations.agent_engine.os_bridge import contract as _contract
    from integrations.agent_engine.os_bridge.power import (
        invoke_power, power_capabilities)
    # Reuse the shell's local-only auth + audit (DRY — one auth path, one log).
    from integrations.agent_engine.shell_os_apis import (
        _require_shell_auth, _audit_shell_op)

    @app.route('/api/os/contract', methods=['GET'])
    def os_bridge_contract():
        """The self-describing op manifest (implemented + planned domains)."""
        return jsonify(_contract.describe_contract())

    @app.route('/api/os/power/capabilities', methods=['GET'])
    def os_bridge_power_capabilities():
        """Which power ops this box offers (firmware_setup gated on the UEFI probe)."""
        return jsonify({'capabilities': power_capabilities()})

    @app.route('/api/os/invoke', methods=['POST'])
    @_require_shell_auth
    def os_bridge_invoke():
        """Typed dispatcher: validate ``{domain, op, params}`` against the contract,
        then run the native handler. Result-checked (never a masked success)."""
        data = request.get_json(force=True, silent=True) or {}
        domain = data.get('domain', '')
        op = data.get('op', '')
        params = data.get('params', {}) or {}

        spec = _contract.get_op(domain, op)
        if spec is None:
            # Honest 501 for a DECLARED-but-not-yet-implemented domain; 400 for a
            # genuinely unknown op.
            if _contract.is_planned(domain):
                return jsonify({
                    'ok': False, 'domain': domain, 'op': op,
                    'error': "domain '{}' is planned but not yet implemented".format(domain),
                }), 501
            return jsonify({
                'ok': False, 'domain': domain, 'op': op,
                'error': 'unknown op: {}.{}'.format(domain, op),
            }), 400

        ok, cleaned_or_err = _contract.validate_params(spec, params)
        if not ok:
            return jsonify({'ok': False, 'domain': domain, 'op': op,
                            'error': cleaned_or_err}), 400

        try:
            _audit_shell_op('os_bridge_invoke', {'domain': domain, 'op': op})
        except Exception:
            pass

        if domain == 'power':
            status, payload = invoke_power(op, cleaned_or_err)
            return jsonify(payload), status

        # Unreachable: get_op only returns specs for implemented domains, and the
        # only implemented domain is 'power'. Kept as a fail-closed guard.
        return jsonify({'ok': False, 'domain': domain, 'op': op,
                        'error': 'no native handler for domain'}), 500

    logger.info("Registered os_bridge routes (typed Shell<->OS bridge: power)")
