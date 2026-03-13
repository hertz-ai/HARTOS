"""
Onboarding API Routes — "Light Your HART" ceremony endpoints.

Provides REST endpoints for any frontend (web, mobile, GTK4) to drive
the onboarding state machine.

  POST /api/onboarding/start    — Start or resume a session
  POST /api/onboarding/advance  — Advance to next phase
  GET  /api/onboarding/status   — Current phase for a user
  GET  /api/onboarding/profile  — Get sealed HART identity
"""

import logging

logger = logging.getLogger(__name__)


def register_onboarding_routes(app):
    """Register onboarding ceremony routes on a Flask app."""

    @app.route('/api/onboarding/start', methods=['POST'])
    def _onboarding_start():
        from flask import request, jsonify
        from hart_onboarding import (
            get_or_create_session, has_hart_name, get_hart_profile,
            CONVERSATION_SCRIPT,
        )

        data = request.get_json(silent=True) or {}
        user_id = data.get('user_id', '1')

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
        from hart_onboarding import get_or_create_session, remove_session

        data = request.get_json(silent=True) or {}
        user_id = data.get('user_id', '1')
        action = data.get('action')
        action_data = data.get('data', {})

        session = get_or_create_session(user_id)
        result = session.advance(action=action, data=action_data)

        # Clean up completed sessions
        if result.get('sealed'):
            remove_session(user_id)

        return jsonify({'success': True, **result})

    @app.route('/api/onboarding/status', methods=['GET'])
    def _onboarding_status():
        from flask import request, jsonify
        from hart_onboarding import has_hart_name, get_hart_profile

        user_id = request.args.get('user_id', '1')

        if has_hart_name(user_id):
            return jsonify({
                'success': True,
                'onboarded': True,
                'profile': get_hart_profile(user_id),
            })

        return jsonify({
            'success': True,
            'onboarded': False,
        })

    @app.route('/api/onboarding/profile', methods=['GET'])
    def _onboarding_profile():
        from flask import request, jsonify
        from hart_onboarding import get_hart_profile

        user_id = request.args.get('user_id', '1')
        profile = get_hart_profile(user_id)

        if profile:
            return jsonify({'success': True, 'profile': profile})
        return jsonify({'success': False, 'error': 'Not onboarded'}), 404

    logger.info("Registered HART onboarding API routes")
