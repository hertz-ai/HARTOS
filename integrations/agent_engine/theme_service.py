"""
HART OS Theme Service — OS-wide theme management.

Manages glass shell + Conky + GTK appearance from a single source of truth.
Theme presets live as JSON files; the active theme is persisted to disk
and propagated to all visual layers (LiquidUI CSS vars, Conky Lua, GTK gsettings).

Agent-driven customization: agents can call apply_theme() or update_custom()
to change the OS appearance on voice command ("make it darker", "bigger fonts").
"""

import json
import logging
import os
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve.theme_service')

# ── Paths ────────────────────────────────────────────────────────

_DATA_DIR = os.environ.get('HEVOLVE_DATA_DIR', os.environ.get(
    'HART_DATA_DIR', os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), 'agent_data')))

_THEME_DIR = os.environ.get('HART_THEME_DIR', os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'nixos', 'assets', 'conky-themes'))

_ACTIVE_THEME_PATH = os.path.join(_DATA_DIR, 'active_theme.json')
_CUSTOM_OVERRIDES_PATH = os.path.join(_DATA_DIR, 'theme_custom.json')


class ThemeService:
    """OS-wide theme management — Glass Shell + Conky + GTK."""

    # ── Preset Management ────────────────────────────────────────

    @staticmethod
    def list_presets() -> List[dict]:
        """Return all available theme presets."""
        presets = []
        if not os.path.isdir(_THEME_DIR):
            logger.warning("Theme directory not found: %s", _THEME_DIR)
            return presets

        for fname in sorted(os.listdir(_THEME_DIR)):
            if not fname.endswith('.json'):
                continue
            path = os.path.join(_THEME_DIR, fname)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    preset = json.load(f)
                presets.append({
                    'id': preset.get('id', fname.replace('.json', '')),
                    'name': preset.get('name', ''),
                    'description': preset.get('description', ''),
                    'category': preset.get('category', 'dark'),
                    'accent': preset.get('colors', {}).get('accent', ''),
                    'background': preset.get('colors', {}).get('background', ''),
                })
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load theme %s: %s", fname, e)
        return presets

    @staticmethod
    def get_preset(theme_id: str) -> Optional[dict]:
        """Load a full theme preset by ID."""
        path = os.path.join(_THEME_DIR, f'{theme_id}.json')
        if not os.path.isfile(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load theme %s: %s", theme_id, e)
            return None

    # ── Active Theme ─────────────────────────────────────────────

    @staticmethod
    def get_active_theme() -> dict:
        """Return the currently active theme (preset + any custom overrides).

        On first call with no active theme, auto-detects hardware and
        selects appropriate theme (potato for low-end, default for standard+).
        """
        theme = ThemeService._load_active_file()
        if theme:
            overrides = ThemeService._load_custom_overrides()
            if overrides:
                theme = ThemeService._deep_merge(theme, overrides)
            return theme

        # Auto-detect on first access (writes active_theme.json)
        recommended = ThemeService.detect_performance_tier()
        if recommended:
            preset = ThemeService.get_preset(recommended)
            if preset:
                return preset

        # Fallback: hart-default
        default = ThemeService.get_preset('hart-default')
        if default:
            return default

        # Ultimate fallback: minimal inline theme
        return {
            'id': 'hart-default',
            'name': 'HART Default',
            'category': 'dark',
            'colors': {
                'background': '0F0E17', 'accent': '6C63FF',
                'active': '00e676', 'text': 'e0e0e0',
                'heading': '6C63FF', 'glass_bg': 'rgba(15,14,23,0.65)',
                'glass_border': 'rgba(108,99,255,0.15)',
                'muted': '78909c', 'surface': '1a1a2e',
            },
            'font': {'family': 'JetBrains Mono', 'size': 13,
                     'heading_size': 18, 'weight': 400, 'heading_weight': 600},
            'shell': {'blur_radius': 20, 'saturation': 180,
                      'border_radius': 16, 'panel_opacity': 0.65},
            'conky': {'heading': '6C63FF', 'active': '00e676',
                      'muted': '78909c', 'default_text': 'b0b0b0'},
            'gtk_prefer_dark': True,
        }

    @staticmethod
    def apply_theme(theme_id: str) -> dict:
        """Apply a theme OS-wide. Returns the applied theme or error."""
        preset = ThemeService.get_preset(theme_id)
        if not preset:
            return {'error': f'Unknown theme: {theme_id}'}

        # 1. Persist active theme file (read by Conky Lua every 5s)
        try:
            os.makedirs(os.path.dirname(_ACTIVE_THEME_PATH), exist_ok=True)
            with open(_ACTIVE_THEME_PATH, 'w', encoding='utf-8') as f:
                json.dump(preset, f, indent=2)
        except OSError as e:
            logger.error("Failed to write active theme: %s", e)
            return {'error': str(e)}

        # 2. Clear custom overrides (new preset = fresh start)
        if os.path.isfile(_CUSTOM_OVERRIDES_PATH):
            try:
                os.remove(_CUSTOM_OVERRIDES_PATH)
            except OSError:
                pass

        # 3. Apply GTK theme via gsettings (Linux only, non-blocking)
        ThemeService._apply_gtk(preset)

        logger.info("Theme applied: %s", theme_id)

        # Single notification path: EventBus → WAMP → all subsystems
        # LiquidUI subscribes to 'theme.changed' on the EventBus
        try:
            from core.platform.events import emit_event
            emit_event('theme.changed', {'theme_id': theme_id, 'preset': preset})
        except Exception:
            pass

        return {'status': 'applied', 'theme_id': theme_id, 'theme': preset}

    # ── Agent-Driven Customization ───────────────────────────────

    @staticmethod
    def update_custom(overrides: dict) -> dict:
        """Apply partial customization on top of the active theme.

        Agents use this for voice-driven tweaks:
          "make fonts bigger" → update_custom({'font': {'size': 16}})
          "more transparency"  → update_custom({'shell': {'panel_opacity': 0.5}})
          "change accent to red" → update_custom({'colors': {'accent': 'f44336'}})
        """
        current = ThemeService._load_custom_overrides() or {}
        merged = ThemeService._deep_merge(current, overrides)

        try:
            os.makedirs(os.path.dirname(_CUSTOM_OVERRIDES_PATH), exist_ok=True)
            with open(_CUSTOM_OVERRIDES_PATH, 'w', encoding='utf-8') as f:
                json.dump(merged, f, indent=2)
        except OSError as e:
            logger.error("Failed to write custom overrides: %s", e)
            return {'error': str(e)}

        # Re-write active theme file with overrides applied
        base = ThemeService._load_active_file()
        if base:
            combined = ThemeService._deep_merge(base, merged)
            try:
                with open(_ACTIVE_THEME_PATH, 'w', encoding='utf-8') as f:
                    json.dump(combined, f, indent=2)
            except OSError:
                pass

        try:
            from core.platform.events import emit_event
            emit_event('theme.custom_updated', {'overrides': merged})
        except Exception:
            pass

        return {'status': 'customized', 'overrides': merged}

    @staticmethod
    def get_font_options() -> List[dict]:
        """Available font families for customization."""
        return [
            {'family': 'JetBrains Mono', 'category': 'monospace'},
            {'family': 'Inter', 'category': 'sans-serif'},
            {'family': 'Fira Code', 'category': 'monospace'},
            {'family': 'IBM Plex Sans', 'category': 'sans-serif'},
            {'family': 'Roboto', 'category': 'sans-serif'},
            {'family': 'Source Code Pro', 'category': 'monospace'},
            {'family': 'Noto Sans', 'category': 'sans-serif'},
            {'family': 'Ubuntu', 'category': 'sans-serif'},
        ]

    # ── Performance Auto-Detection ──────────────────────────────

    @staticmethod
    def detect_performance_tier() -> str:
        """Detect hardware tier and return recommended theme.

        Returns theme ID: 'potato' for OBSERVER/EMBEDDED,
        'minimal' for LITE, None for STANDARD+.
        """
        try:
            from security.system_requirements import get_tier, NodeTierLevel
            tier = get_tier()
            if tier in (NodeTierLevel.EMBEDDED, NodeTierLevel.OBSERVER):
                return 'potato'
            if tier == NodeTierLevel.LITE:
                return 'minimal'
        except Exception:
            pass

        # Fallback: direct hardware check (no system_requirements module)
        try:
            import os
            cores = os.cpu_count() or 1
            # Try psutil for RAM
            try:
                import psutil
                ram_gb = psutil.virtual_memory().total / (1024 ** 3)
            except ImportError:
                ram_gb = 4.0  # conservative default

            if cores <= 2 and ram_gb < 4:
                return 'potato'
            if cores <= 2 or ram_gb < 6:
                return 'minimal'
        except Exception:
            pass

        return None  # Standard+ hardware, use default theme

    @staticmethod
    def auto_select_theme() -> dict:
        """Auto-select theme based on hardware on first boot.

        Only acts when no active_theme.json exists yet.
        Returns the result of apply_theme() or None if no action needed.
        """
        # Don't override existing theme choice
        if os.path.isfile(_ACTIVE_THEME_PATH):
            return None

        recommended = ThemeService.detect_performance_tier()
        if recommended:
            logger.info("Auto-selecting theme '%s' for hardware", recommended)
            return ThemeService.apply_theme(recommended)

        # Default to hart-default for capable hardware
        return ThemeService.apply_theme('hart-default')

    # ── Conky Integration ────────────────────────────────────────

    @staticmethod
    def get_conky_color_overrides() -> dict:
        """Return Conky-specific colors from the active theme."""
        theme = ThemeService.get_active_theme()
        return theme.get('conky', {})

    # ── CSS Variables Export ─────────────────────────────────────

    @staticmethod
    def get_css_variables() -> str:
        """Export active theme as CSS custom properties for the shell."""
        theme = ThemeService.get_active_theme()
        colors = theme.get('colors', {})
        font = theme.get('font', {})
        shell = theme.get('shell', {})

        lines = [':root {']
        # Colors
        for key, val in colors.items():
            css_key = key.replace('_', '-')
            if val.startswith('rgba') or val.startswith('#'):
                lines.append(f'  --hart-{css_key}: {val};')
            else:
                lines.append(f'  --hart-{css_key}: #{val};')
        # Font
        lines.append(f'  --hart-font-family: "{font.get("family", "JetBrains Mono")}";')
        lines.append(f'  --hart-font-size: {font.get("size", 13)}px;')
        lines.append(f'  --hart-heading-size: {font.get("heading_size", 18)}px;')
        lines.append(f'  --hart-font-weight: {font.get("weight", 400)};')
        lines.append(f'  --hart-heading-weight: {font.get("heading_weight", 600)};')
        # Shell
        lines.append(f'  --hart-blur: {shell.get("blur_radius", 20)}px;')
        lines.append(f'  --hart-saturation: {shell.get("saturation", 180)}%;')
        lines.append(f'  --hart-radius: {shell.get("border_radius", 16)}px;')
        lines.append(f'  --hart-panel-opacity: {shell.get("panel_opacity", 0.65)};')
        lines.append(f'  --hart-topbar-height: {shell.get("topbar_height", 40)}px;')
        lines.append(f'  --hart-icon-size: {shell.get("icon_size", 20)}px;')
        lines.append(f'  --hart-titlebar-height: {shell.get("panel_titlebar_height", 32)}px;')
        lines.append(f'  --hart-anim-speed: {shell.get("animation_speed_ms", 200)}ms;')
        lines.append('}')
        return '\n'.join(lines)

    # ── Internal ─────────────────────────────────────────────────

    @staticmethod
    def _load_active_file() -> Optional[dict]:
        if not os.path.isfile(_ACTIVE_THEME_PATH):
            return None
        try:
            with open(_ACTIVE_THEME_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _load_custom_overrides() -> Optional[dict]:
        if not os.path.isfile(_CUSTOM_OVERRIDES_PATH):
            return None
        try:
            with open(_CUSTOM_OVERRIDES_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        """Recursively merge override into base."""
        result = dict(base)
        for key, val in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(val, dict):
                result[key] = ThemeService._deep_merge(result[key], val)
            else:
                result[key] = val
        return result

    @staticmethod
    def _apply_gtk(theme: dict):
        """Apply GTK dark/light preference via gsettings (Linux only)."""
        try:
            dark = theme.get('gtk_prefer_dark', True)
            scheme = 'prefer-dark' if dark else 'default'
            subprocess.Popen(
                ['gsettings', 'set', 'org.gnome.desktop.interface',
                 'color-scheme', scheme],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass  # gsettings not available (Windows dev, etc.)

    @staticmethod
    def _notify_liquid_ui(theme: dict):
        """Push theme update to LiquidUI Flask server."""
        try:
            from core.http_pool import pooled_post
            port = os.environ.get('HART_LIQUID_UI_PORT', '6800')
            pooled_post(
                f'http://localhost:{port}/api/theme',
                json={'theme': theme},
                timeout=2,
            )
        except Exception:
            pass  # LiquidUI not running or requests unavailable
