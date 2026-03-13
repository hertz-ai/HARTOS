"""
Shell OpenClaw APIs — REST endpoints for OpenClaw integration in Nunba.

Provides:
  - /api/openclaw/skills          — List installed ClawHub skills
  - /api/openclaw/skills/install  — Install a skill by slug
  - /api/openclaw/skills/uninstall — Remove a skill
  - /api/openclaw/skills/search   — Search ClawHub registry
  - /api/openclaw/status          — OpenClaw gateway status
  - /api/openclaw/channels        — Available messaging channels
  - /api/assistant/chat           — Floating assistant chat endpoint
  - /api/assistant/capabilities   — What the assistant can do
"""

import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def register_openclaw_routes(app):
    """Register OpenClaw and floating assistant routes on a Flask app."""

    # ── OpenClaw Skill Management ──────────────────────────────

    @app.route('/api/openclaw/skills', methods=['GET'])
    def _openclaw_list_skills():
        from flask import jsonify
        try:
            from integrations.openclaw.clawhub_adapter import list_installed_skills
            skills = list_installed_skills()
            return jsonify({
                'success': True,
                'skills': [
                    {
                        'name': s.name,
                        'description': s.description,
                        'version': s.version,
                        'emoji': s.emoji,
                        'homepage': s.homepage,
                        'user_invocable': s.user_invocable,
                        'requirements_met': True,  # Already filtered
                    }
                    for s in skills
                ],
                'count': len(skills),
            })
        except Exception as e:
            logger.error("Failed to list skills: %s", e)
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/openclaw/skills/install', methods=['POST'])
    def _openclaw_install_skill():
        from flask import request, jsonify
        data = request.get_json(silent=True) or {}
        slug = data.get('slug', '')
        if not slug:
            return jsonify({'success': False, 'error': 'slug required'}), 400

        try:
            from integrations.openclaw.clawhub_adapter import (
                install_skill, get_clawhub_provider
            )
            skill = install_skill(
                slug,
                version=data.get('version'),
                force=data.get('force', False),
            )
            if skill:
                get_clawhub_provider().invalidate()
                return jsonify({
                    'success': True,
                    'skill': {
                        'name': skill.name,
                        'description': skill.description,
                        'version': skill.version,
                    },
                })
            return jsonify({'success': False, 'error': 'Install failed'}), 404
        except Exception as e:
            logger.error("Install failed: %s", e)
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/openclaw/skills/uninstall', methods=['POST'])
    def _openclaw_uninstall_skill():
        from flask import request, jsonify
        data = request.get_json(silent=True) or {}
        slug = data.get('slug', '')
        if not slug:
            return jsonify({'success': False, 'error': 'slug required'}), 400

        try:
            from integrations.openclaw.clawhub_adapter import (
                uninstall_skill, get_clawhub_provider
            )
            removed = uninstall_skill(slug)
            if removed:
                get_clawhub_provider().invalidate()
            return jsonify({'success': True, 'removed': removed})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/openclaw/skills/search', methods=['GET'])
    def _openclaw_search_skills():
        from flask import request, jsonify
        query = request.args.get('q', '')
        if not query:
            return jsonify({'success': False, 'error': 'q parameter required'}), 400

        try:
            from core.http_pool import pooled_get
        except ImportError:
            import requests
            pooled_get = requests.get

        try:
            resp = pooled_get(
                f"https://registry.clawhub.ai/api/skills/search",
                params={'q': query, 'limit': 20},
                timeout=10,
            )
            if hasattr(resp, 'json'):
                data = resp.json()
            else:
                data = {'results': []}
            return jsonify({'success': True, 'results': data.get('results', [])})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/openclaw/status', methods=['GET'])
    def _openclaw_status():
        from flask import jsonify
        try:
            from integrations.openclaw.gateway_bridge import (
                get_gateway_bridge, is_openclaw_installed, get_openclaw_version
            )
            bridge = get_gateway_bridge()
            return jsonify({
                'success': True,
                'installed': is_openclaw_installed(),
                'version': get_openclaw_version(),
                'gateway': bridge.health(),
            })
        except Exception as e:
            return jsonify({
                'success': True,
                'installed': False,
                'version': None,
                'gateway': {'connected': False},
            })

    @app.route('/api/openclaw/channels', methods=['GET'])
    def _openclaw_channels():
        from flask import jsonify
        channels = [
            {'id': 'whatsapp', 'name': 'WhatsApp', 'icon': 'chat'},
            {'id': 'telegram', 'name': 'Telegram', 'icon': 'send'},
            {'id': 'discord', 'name': 'Discord', 'icon': 'headset'},
            {'id': 'slack', 'name': 'Slack', 'icon': 'tag'},
            {'id': 'signal', 'name': 'Signal', 'icon': 'security'},
            {'id': 'imessage', 'name': 'iMessage', 'icon': 'message'},
            {'id': 'matrix', 'name': 'Matrix', 'icon': 'grid_on'},
            {'id': 'teams', 'name': 'Teams', 'icon': 'groups'},
            {'id': 'feishu', 'name': 'Feishu', 'icon': 'flight'},
            {'id': 'line', 'name': 'LINE', 'icon': 'chat_bubble'},
        ]
        return jsonify({'success': True, 'channels': channels})

    # ── Floating Assistant ─────────────────────────────────────

    @app.route('/api/assistant/chat', methods=['POST'])
    def _assistant_chat():
        """Floating assistant chat — uses ALL HART capabilities.

        This is the universal entry point: the floating chat bubble
        can dispatch to any HART service (recipes, agents, TTS, vision,
        OpenClaw skills, expert agents, etc.)
        """
        from flask import request, jsonify
        data = request.get_json(silent=True) or {}
        message = data.get('message', '')
        user_id = data.get('user_id', '1')
        context = data.get('context', {})

        if not message:
            return jsonify({'success': False, 'error': 'message required'}), 400

        try:
            # Route through the main HART chat pipeline
            from core.http_pool import pooled_post
            resp = pooled_post(
                'http://localhost:6777/chat',
                json={
                    'user_id': user_id,
                    'prompt_id': context.get('prompt_id', '99999'),
                    'prompt': message,
                    'create_agent': context.get('create_agent', False),
                },
                timeout=120,
            )
            if hasattr(resp, 'json'):
                result = resp.json()
            else:
                result = {'response': str(resp)}

            return jsonify({
                'success': True,
                'response': result.get('response', result.get('result', '')),
                'source': 'hart_pipeline',
            })
        except Exception as e:
            logger.error("Assistant chat error: %s", e)
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/assistant/capabilities', methods=['GET'])
    def _assistant_capabilities():
        """What the floating assistant can do."""
        from flask import jsonify
        capabilities = [
            {
                'id': 'chat',
                'name': 'Chat',
                'description': 'General conversation and task execution',
                'icon': 'chat',
            },
            {
                'id': 'recipe',
                'name': 'Recipes',
                'description': 'Execute trained recipes (fast, no LLM)',
                'icon': 'receipt_long',
            },
            {
                'id': 'agents',
                'name': 'Agents',
                'description': 'Create and manage autonomous agents',
                'icon': 'smart_toy',
            },
            {
                'id': 'vision',
                'name': 'Vision',
                'description': 'Analyze images and screenshots',
                'icon': 'visibility',
            },
            {
                'id': 'voice',
                'name': 'Voice',
                'description': 'Text-to-speech and voice cloning (offline)',
                'icon': 'record_voice_over',
            },
            {
                'id': 'expert',
                'name': 'Expert Network',
                'description': 'Query 96 specialized agents',
                'icon': 'psychology',
            },
            {
                'id': 'openclaw',
                'name': 'OpenClaw Skills',
                'description': '3,200+ ClawHub skills',
                'icon': 'extension',
            },
            {
                'id': 'code',
                'name': 'Coding',
                'description': 'Write, edit, and review code',
                'icon': 'code',
            },
            {
                'id': 'remote',
                'name': 'Remote Desktop',
                'description': 'Control remote devices',
                'icon': 'desktop_windows',
            },
            {
                'id': 'channels',
                'name': 'Channels',
                'description': 'Send messages via 30+ platforms',
                'icon': 'forum',
            },
        ]
        return jsonify({'success': True, 'capabilities': capabilities})

    @app.route('/api/assistant/voice', methods=['POST'])
    def _assistant_voice():
        """Voice synthesis from the floating assistant."""
        from flask import request, jsonify
        data = request.get_json(silent=True) or {}
        text = data.get('text', '')
        voice = data.get('voice', 'alba')

        if not text:
            return jsonify({'success': False, 'error': 'text required'}), 400

        try:
            from integrations.audio.tts import get_tts_engine
            engine = get_tts_engine()
            path = engine.synthesize(text, voice=voice)
            return jsonify({'success': True, 'audio_path': path})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    logger.info("Registered OpenClaw + floating assistant API routes")
