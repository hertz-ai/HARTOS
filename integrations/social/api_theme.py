"""
HART OS Theme API — OS-wide appearance management.

GET  /api/appearance/presets        — List all theme presets
GET  /api/appearance/active         — Get active theme (with CSS variables)
POST /api/appearance/apply          — Apply a preset OS-wide
POST /api/appearance/customize      — Agent-driven partial customization
GET  /api/appearance/fonts          — Available font families
GET  /api/appearance/css            — Active theme as CSS custom properties
"""

import logging
from flask import Blueprint, jsonify, request

logger = logging.getLogger('hevolve.api_theme')

theme_bp = Blueprint('theme', __name__)


def _get_service():
    from integrations.agent_engine.theme_service import ThemeService
    return ThemeService


@theme_bp.route('/api/appearance/presets', methods=['GET'])
def list_presets():
    svc = _get_service()
    return jsonify({'presets': svc.list_presets()})


@theme_bp.route('/api/appearance/active', methods=['GET'])
def get_active():
    svc = _get_service()
    theme = svc.get_active_theme()
    css = svc.get_css_variables()
    return jsonify({'theme': theme, 'css': css})


@theme_bp.route('/api/appearance/apply', methods=['POST'])
def apply_theme():
    """Apply a preset OS-wide, and/or overlay a Personalize palette (#161).

    Body: {theme_id?, secondary_accent?, custom?:{accent,secondary,background}}.
    The palette picker posts just secondary_accent + custom (no preset switch);
    this route EXTENDS the same endpoint to carry them (no fork).
    """
    data = request.get_json(force=True, silent=True) or {}
    theme_id = data.get('theme_id', '')
    secondary_accent = data.get('secondary_accent')
    custom = data.get('custom')
    if not theme_id and not (secondary_accent or custom):
        return jsonify({'error': 'theme_id or palette (secondary_accent/custom) required'}), 400

    svc = _get_service()
    result = svc.apply_theme(theme_id, secondary_accent=secondary_accent, custom=custom)
    if 'error' in result:
        return jsonify(result), 404
    return jsonify(result)


@theme_bp.route('/api/appearance/customize', methods=['POST'])
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


@theme_bp.route('/api/appearance/fonts', methods=['GET'])
def list_fonts():
    svc = _get_service()
    return jsonify({'fonts': svc.get_font_options()})


@theme_bp.route('/api/appearance/css', methods=['GET'])
def get_css():
    svc = _get_service()
    css = svc.get_css_variables()
    return css, 200, {'Content-Type': 'text/css'}
