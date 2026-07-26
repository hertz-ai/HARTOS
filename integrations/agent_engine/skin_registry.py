"""HART OS Skin Registry — Rainmeter-like extensible desktop skins (#170 pillar 3).

A SKIN is a declarative, shareable desktop preset that COMPOSES EXISTING primitives —
it is never a second theming/render engine:

    {id, name, description, palette, widgets, layout, fonts}

  - ``palette``  : a ThemeService theme_id (e.g. ``'electric'``) OR a custom
                   ``{accent, secondary, background}`` trio. Applied by REUSING
                   ``ThemeService.apply_theme`` — the ONE theme engine.
  - ``widgets``  : an ordered list of ``{panel: <id>, ...}`` the shell surfaces via the
                   EXISTING ``openPanel`` gateway (client-side, hartSkins.js). No new
                   window/render engine.
  - ``layout`` / ``fonts`` : hints the client applies through the existing CSS-var tokens
                   (``--hart-density`` / ``--hart-font-family`` …).

Starter skins ship read-only; user/agent skins persist to ``skins_custom.json``. The
agentic Liquid UI can compose a skin live and ``save_skin`` it here (the adaptive hook,
pillar 4) — that composition rides the SAME ``agent_ui_update`` transport ``hartHome``
already renders from.

Reuse, ZERO parallel path: ThemeService (palette), openPanel (surfaces), agent_ui_update
(live composition), the ``/api/appearance/*`` namespace (routes, mirroring theme_bp). This
module owns ONLY the skin descriptor + its persistence.
"""

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve.skin_registry')

# Same data-dir resolution ThemeService uses (custom overrides live beside it).
_DATA_DIR = os.environ.get('HEVOLVE_DATA_DIR', os.environ.get(
    'HART_DATA_DIR', os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), 'agent_data')))
_CUSTOM_SKINS_PATH = os.path.join(_DATA_DIR, 'skins_custom.json')

_lock = threading.RLock()

# Read-only starter skins. ``palette`` is a ThemeService theme_id (str) OR a custom trio
# (dict). ``widgets[].panel`` are canonical openPanel ids the shell already serves.
_STARTER_SKINS: Dict[str, Dict[str, Any]] = {
    'aura-calm': {
        'id': 'aura-calm', 'name': 'Aura Calm',
        'description': 'The ambient default — calm navy, soft glow.',
        'palette': 'hart-default',
        'widgets': [{'panel': 'agents'}, {'panel': 'recipes'}],
        'layout': 'ambient', 'fonts': {'family': 'JetBrains Mono'}, 'builtin': True,
    },
    'focus': {
        'id': 'focus', 'name': 'Focus',
        'description': 'Minimal, high-contrast — one thing at a time.',
        'palette': 'electric',
        'widgets': [{'panel': 'agents'}],
        'layout': 'single', 'fonts': {'family': 'JetBrains Mono'}, 'builtin': True,
    },
    'vibrant': {
        'id': 'vibrant', 'name': 'Vibrant',
        'description': 'Saturated, image-rich, alive.',
        'palette': {'accent': '#00E6C3', 'secondary': '#9B5CFF', 'background': '#05060C'},
        'widgets': [{'panel': 'agents'}, {'panel': 'communities'}, {'panel': 'recipes'}],
        'layout': 'grid', 'fonts': {'family': 'JetBrains Mono'}, 'builtin': True,
    },
}


def _load_custom() -> Dict[str, Dict[str, Any]]:
    try:
        with open(_CUSTOM_SKINS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_custom(skins: Dict[str, Dict[str, Any]]) -> bool:
    """Atomic write of the custom-skins map (one writer, os.replace)."""
    try:
        with _lock:
            os.makedirs(_DATA_DIR, exist_ok=True)
            tmp = _CUSTOM_SKINS_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(skins, f, indent=2)
            os.replace(tmp, _CUSTOM_SKINS_PATH)
        return True
    except OSError as e:
        logger.warning('skin write failed: %s', e)
        return False


def _all_skins() -> Dict[str, Dict[str, Any]]:
    """Starter skins overlaid with any user/agent-saved ones (custom wins on id)."""
    merged = dict(_STARTER_SKINS)
    merged.update(_load_custom())
    return merged


def list_skins() -> List[Dict[str, Any]]:
    """Summaries for the picker (id/name/description/palette/builtin)."""
    out = []
    for s in _all_skins().values():
        out.append({
            'id': s.get('id'), 'name': s.get('name', ''),
            'description': s.get('description', ''),
            'palette': s.get('palette'), 'builtin': bool(s.get('builtin')),
        })
    return sorted(out, key=lambda x: (not x['builtin'], x['name']))


def get_skin(skin_id: str) -> Optional[Dict[str, Any]]:
    """Full descriptor for one skin, or None."""
    return _all_skins().get(skin_id)


def save_skin(descriptor: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a user/agent skin. Returns {'status':'saved','id':...} or {'error':...}.

    A starter-skin id is protected (cannot be overwritten). ``palette`` must be a
    ThemeService theme_id (str) or a custom trio (dict) — the SAME shapes
    ThemeService.apply_theme accepts, so a saved skin can never carry a palette the
    theme engine can't apply.
    """
    if not isinstance(descriptor, dict):
        return {'error': 'skin descriptor must be an object'}
    skin_id = str(descriptor.get('id') or '').strip()
    name = str(descriptor.get('name') or '').strip()
    if not skin_id or not name:
        return {'error': 'id and name are required'}
    if skin_id in _STARTER_SKINS:
        return {'error': f'"{skin_id}" is a built-in skin and cannot be overwritten'}
    palette = descriptor.get('palette')
    if not isinstance(palette, (str, dict)):
        return {'error': 'palette must be a theme_id (str) or a {accent,secondary,background} object'}
    clean = {
        'id': skin_id, 'name': name,
        'description': str(descriptor.get('description') or ''),
        'palette': palette,
        'widgets': descriptor.get('widgets') if isinstance(descriptor.get('widgets'), list) else [],
        'layout': descriptor.get('layout') or 'default',
        'fonts': descriptor.get('fonts') if isinstance(descriptor.get('fonts'), dict) else {},
        'builtin': False,
    }
    custom = _load_custom()
    custom[skin_id] = clean
    if not _write_custom(custom):
        return {'error': 'failed to persist skin'}
    return {'status': 'saved', 'id': skin_id}


def delete_skin(skin_id: str) -> Dict[str, Any]:
    """Remove a user/agent skin (starter skins are protected)."""
    if skin_id in _STARTER_SKINS:
        return {'error': 'cannot delete a built-in skin'}
    custom = _load_custom()
    if skin_id not in custom:
        return {'error': 'skin not found'}
    del custom[skin_id]
    if not _write_custom(custom):
        return {'error': 'failed to persist deletion'}
    return {'status': 'deleted', 'id': skin_id}


def apply_skin(skin_id: str) -> Dict[str, Any]:
    """Apply a skin: run its palette through ThemeService (the ONE theme engine), then
    return the widget/layout descriptor for the client (hartSkins.js) to compose via the
    existing openPanel gateway. The palette is NEVER applied by a second code path.
    """
    skin = get_skin(skin_id)
    if not skin:
        return {'error': f'skin not found: {skin_id}'}

    palette = skin.get('palette')
    theme_result: Dict[str, Any] = {}
    try:
        from integrations.agent_engine.theme_service import ThemeService
        if isinstance(palette, str) and palette:
            theme_result = ThemeService.apply_theme(palette)
        elif isinstance(palette, dict):
            theme_result = ThemeService.apply_theme('', custom=palette)
    except Exception as e:  # pragma: no cover - theme engine optional at import
        logger.warning('apply_skin palette failed: %s', e)
        theme_result = {'error': str(e)}

    return {
        'status': 'applied',
        'skin_id': skin_id,
        'skin': skin,
        'widgets': skin.get('widgets', []),
        'layout': skin.get('layout', 'default'),
        'fonts': skin.get('fonts', {}),
        'theme': theme_result,
    }


def register_skin_routes(app) -> None:
    """Register the skin routes under the SAME /api/appearance/* namespace theme_bp uses
    (no social auth — OS-local appearance). Mirrors register_*_routes in this package.
    """
    from flask import jsonify, request

    @app.route('/api/appearance/skins', methods=['GET'])
    def _skins_list():  # noqa: unused - Flask view
        return jsonify({'skins': list_skins()})

    @app.route('/api/appearance/skins', methods=['POST'])
    def _skins_save():
        data = request.get_json(force=True, silent=True) or {}
        result = save_skin(data)
        return jsonify(result), (200 if 'error' not in result else 400)

    @app.route('/api/appearance/skins/<skin_id>', methods=['GET'])
    def _skins_get(skin_id):
        skin = get_skin(skin_id)
        return (jsonify(skin), 200) if skin else (jsonify({'error': 'skin not found'}), 404)

    @app.route('/api/appearance/skins/<skin_id>/apply', methods=['POST'])
    def _skins_apply(skin_id):
        result = apply_skin(skin_id)
        return jsonify(result), (200 if 'error' not in result else 404)

    @app.route('/api/appearance/skins/<skin_id>', methods=['DELETE'])
    def _skins_delete(skin_id):
        result = delete_skin(skin_id)
        return jsonify(result), (200 if 'error' not in result else 404)

    logger.info('skin routes registered under /api/appearance/skins')
