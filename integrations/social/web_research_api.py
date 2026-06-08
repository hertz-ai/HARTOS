"""Flask blueprint for the Web Research admin UI.

Endpoints (all gated by @require_local_or_token, same auth surface as the
existing admin/social blueprints — no parallel auth):

  GET  /api/web-research/probe                      driver health + reachable mode
  GET  /api/web-research/vault                      list configured platforms
  DELETE /api/web-research/vault/<platform>         revoke a platform's stored creds
  GET  /api/web-research/audit?limit=100            paginated audit log tail
  GET  /api/web-research/tools                      what tools are registered

Reuses canonical primitives — no parallel paths:
  - integrations.browser_research.tools.list_tools()
  - integrations.browser_research.audit.read_recent()
  - integrations.browser_research.vault.get_vault()
  - integrations.browser_research.driver.cdp_endpoint_reachable()
"""
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger('web_research_api')

web_research_bp = Blueprint('web_research', __name__, url_prefix='/api/web-research')


def _maybe_require_local_or_token(view_fn):
    """Apply Nunba's local-or-token auth decorator if available.

    Lazy import keeps this blueprint importable on the HARTOS side where the
    decorator may live in Nunba's main.py (cross-repo).  In the worst case
    (decorator unavailable), the blueprint refuses non-local requests via
    `_local_only_fallback` below — never wide-open.
    """
    try:
        from main import require_local_or_token  # Nunba's canonical decorator
        return require_local_or_token(view_fn)
    except ImportError:
        return _local_only_fallback(view_fn)


def _local_only_fallback(view_fn):
    """Last-resort guard: only accept requests from 127.0.0.1 / ::1."""
    from functools import wraps

    @wraps(view_fn)
    def wrapper(*args, **kwargs):
        remote = (request.remote_addr or '').strip()
        if remote not in ('127.0.0.1', '::1', 'localhost'):
            return jsonify({'error': 'local-only endpoint'}), 403
        return view_fn(*args, **kwargs)
    return wrapper


@web_research_bp.route('/probe', methods=['GET'])
@_maybe_require_local_or_token
def probe():
    """Driver health + which mode would be selected right now."""
    try:
        from integrations.browser_research import driver as drv_mod
        b2_reachable = drv_mod.cdp_endpoint_reachable()
        return jsonify({
            'ok': True,
            'b2_cdp_reachable': bool(b2_reachable),
            'effective_mode': 'b2' if b2_reachable else 'b1',
            'connection_mechanism': (
                'obscura_b2_cdp_user_chrome' if b2_reachable
                else 'obscura_b1_headless_profile'
            ),
        })
    except Exception as exc:
        logger.exception('probe failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@web_research_bp.route('/vault', methods=['GET'])
@_maybe_require_local_or_token
def list_vault():
    """List configured platforms (no cookies/credentials surface in the response)."""
    try:
        from integrations.browser_research.vault import get_vault
        v = get_vault()
        platforms = v.list_platforms()
        return jsonify({'ok': True, 'platforms': platforms})
    except Exception as exc:
        logger.exception('vault list failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@web_research_bp.route('/vault/<string:platform>', methods=['DELETE'])
@_maybe_require_local_or_token
def revoke_vault(platform: str):
    """Revoke ALL stored entries for a platform.

    Handle parameter would be required in a full vault, but C1 stub is in-memory
    only — we revoke any entries matching the platform on either handle.
    """
    try:
        from integrations.browser_research.vault import get_vault
        v = get_vault()
        # Snapshot keys then revoke each (stub vault has no list method by handle).
        with v._lock:
            keys = [(p, h) for (p, h) in v._accounts if p == platform]
        for plat, handle in keys:
            v.revoke(plat, handle)
        return jsonify({'ok': True, 'platform': platform, 'revoked': len(keys)})
    except Exception as exc:
        logger.exception('vault revoke failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@web_research_bp.route('/audit', methods=['GET'])
@_maybe_require_local_or_token
def audit_tail():
    """Recent audit-log entries (newest last)."""
    try:
        from integrations.browser_research import audit as audit_mod
        try:
            limit = int(request.args.get('limit', '100'))
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 500))
        return jsonify({'ok': True, 'records': audit_mod.read_recent(limit=limit)})
    except Exception as exc:
        logger.exception('audit tail failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@web_research_bp.route('/tools', methods=['GET'])
@_maybe_require_local_or_token
def list_tools_route():
    """What browser-research tools are wired in this build."""
    try:
        from integrations.browser_research import tools as tools_mod
        return jsonify({'ok': True, 'tools': tools_mod.list_tools()})
    except Exception as exc:
        logger.exception('tools list failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500
