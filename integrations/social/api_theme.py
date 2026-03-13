"""
HART OS Theme API — OS-wide appearance management.

GET  /api/social/theme/presets        — List all theme presets
GET  /api/social/theme/active         — Get active theme (with CSS variables)
POST /api/social/theme/apply          — Apply a preset OS-wide
POST /api/social/theme/customize      — Agent-driven partial customization
GET  /api/social/theme/fonts          — Available font families
GET  /api/social/theme/css            — Active theme as CSS custom properties
"""

import logging
from flask import Blueprint, jsonify, request

logger = logging.getLogger('hevolve.api_theme')

theme_bp = Blueprint('theme', __name__)


def _get_service():
    from integrations.agent_engine.theme_service import ThemeService
    return ThemeService


@theme_bp.route('/api/social/theme/presets', methods=['GET'])
def list_presets():
    svc = _get_service()
    return jsonify({'presets': svc.list_presets()})


@theme_bp.route('/api/social/theme/active', methods=['GET'])
def get_active():
    svc = _get_service()
    theme = svc.get_active_theme()
    css = svc.get_css_variables()
    return jsonify({'theme': theme, 'css': css})


@theme_bp.route('/api/social/theme/apply', methods=['POST'])
def apply_theme():
    data = request.get_json(force=True, silent=True) or {}
    theme_id = data.get('theme_id', '')
    if not theme_id:
        return jsonify({'error': 'theme_id required'}), 400

    svc = _get_service()
    result = svc.apply_theme(theme_id)
    if 'error' in result:
        return jsonify(result), 404
    return jsonify(result)


@theme_bp.route('/api/social/theme/customize', methods=['POST'])
def customize_theme():
    """Agent-driven partial customization.

    Body examples:
      {"font": {"size": 16}}
      {"colors": {"accent": "f44336"}}
      {"shell": {"panel_opacity": 0.5}}
    """
    data = request.get_json(force=True, silent=True) or {}
    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    svc = _get_service()
    result = svc.update_custom(data)
    if 'error' in result:
        return jsonify(result), 500
    return jsonify(result)


@theme_bp.route('/api/social/theme/fonts', methods=['GET'])
def list_fonts():
    svc = _get_service()
    return jsonify({'fonts': svc.get_font_options()})


@theme_bp.route('/api/social/theme/css', methods=['GET'])
def get_css():
    svc = _get_service()
    css = svc.get_css_variables()
    return css, 200, {'Content-Type': 'text/css'}
