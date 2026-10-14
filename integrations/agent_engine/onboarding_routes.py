"""
Onboarding API Routes — "Light Your HART" ceremony endpoints.

Provides REST endpoints for any frontend (web, mobile, GTK4) to drive
the onboarding state machine.

  POST /api/onboarding/start    — Start or resume a session
  POST /api/onboarding/advance  — Advance to next phase
  GET  /api/onboarding/status   — Current phase for a user
  GET  /api/onboarding/profile  — Get sealed HART identity

On HART OS the ceremony itself is Nunba's (static/hartOnboarding.js hosts
Nunba's /local page); the shell asks /status for two things before showing
it: `onboarded` (the desktop owner's seal is on record) and `companion_ready`
(the Nunba daemon answered, so the page can load).
"""

import logging

logger = logging.getLogger(__name__)


def register_onboarding_routes(app, resolve_user=None):
    """Register onboarding ceremony routes on a Flask app.

    ``resolve_user`` names the desktop owner when a request carries no
    user_id: on HART OS that is LiquidUIService._desktop_owner_id (the owner
    Nunba knows, else Nunba's hardware-stable guest id, else None while the
    Nunba daemon is still coming up). Without it the historical default '1'
    stands, which is what the standalone test apps use.
    """

    def _user(params):
        """-> (user_id, companion_ready). companion_ready is False only when
        the resolver was consulted and had nobody to name (Nunba not up)."""
        uid = (params or {}).get('user_id')
        if uid:
            return str(uid), True
        if resolve_user is None:
            return '1', True
        resolved = resolve_user()
        if resolved:
            return str(resolved), True
        return '1', False

    @app.route('/api/onboarding/start', methods=['POST'])
    def _onboarding_start():
        from flask import request, jsonify
        from hartos.hart_onboarding import (
            get_or_create_session, has_hart_name, get_hart_profile,
            CONVERSATION_SCRIPT,
        )

        data = request.get_json(silent=True) or {}
        user_id, _ = _user(data)

        # Already onboarded?
        if has_hart_name(user_id):
            profile = get_hart_profile(user_id)
            return jsonify({
                'success': True,
                'already_onboarded': True,
                'profile': profile,
            })

        session = get_or_create_session(user_id)

        # Return the language prompt in all languages
        return jsonify({
            'success': True,
            'already_onboarded': False,
            'phase': session.phase,
            'language_prompt': CONVERSATION_SCRIPT['language_prompt'],
        })

    @app.route('/api/onboarding/advance', methods=['POST'])
    def _onboarding_advance():
        from flask import request, jsonify
        from hartos.hart_onboarding import (
            get_or_create_session, remove_session,
            has_hart_name, get_hart_profile,
        )

        data = request.get_json(silent=True) or {}
        user_id, _ = _user(data)
        action = data.get('action')
        action_data = data.get('data', {})

        # #167 — "accept the name" must never be a dead no-op. Accepting the
        # generated name seals it into the profile and sends the shell to the
        # desktop. If this user is already sealed (a completed session was
        # cleaned up, or the accept was re-sent / double-clicked), there is no
        # live reveal-phase session to walk — a fresh session would sit at the
        # 'language' phase and swallow the accept. Short-circuit to an
        # idempotent success read straight from the canonical seal readers, so
        # a re-accept resolves to the sealed identity and advances to the
        # desktop instead of silently doing nothing.
        if action == 'accept_name' and has_hart_name(user_id):
            profile = get_hart_profile(user_id)
            remove_session(user_id)
            return jsonify({
                'success': True,
                'phase': 'sealed',
                'name_sealed': True,
                'sealed': True,
                'goto_desktop': True,
                'profile': profile,
                'hart_name': (profile or {}).get('name'),
            })

        session = get_or_create_session(user_id)
        result = session.advance(action=action, data=action_data)

        # A fresh accept that just sealed the name: surface the sealed profile
        # and tell the shell to advance to the desktop. The companion pre-fetch
        # (kicked off by the FSM) keeps running in the background and never
        # blocks the ceremony — so the reveal resolves straight to the desktop.
        if action == 'accept_name' and result.get('name_sealed'):
            profile = get_hart_profile(user_id)
            if profile:
                result['profile'] = profile
            result['goto_desktop'] = True

        # Clean up completed sessions
        if result.get('sealed'):
            remove_session(user_id)

        return jsonify({'success': True, **result})

    @app.route('/api/onboarding/status', methods=['GET'])
    def _onboarding_status():
        from flask import request, jsonify
        from hartos.hart_onboarding import has_hart_name, get_hart_profile

        user_id, companion_ready = _user(request.args)

        if has_hart_name(user_id):
            return jsonify({
                'success': True,
                'onboarded': True,
                'user_id': user_id,
                'companion_ready': companion_ready,
                'profile': get_hart_profile(user_id),
            })

        return jsonify({
            'success': True,
            'onboarded': False,
            'user_id': user_id,
            'companion_ready': companion_ready,
        })

    @app.route('/api/onboarding/profile', methods=['GET'])
    def _onboarding_profile():
        from flask import request, jsonify
        from hartos.hart_onboarding import get_hart_profile

        user_id, _ = _user(request.args)
        profile = get_hart_profile(user_id)

        if profile:
            return jsonify({'success': True, 'profile': profile})
        return jsonify({'success': False, 'error': 'Not onboarded'}), 404

    logger.info("Registered HART onboarding API routes")
