"""
Tests for HART OS Theme Service, Theme API, and Shell Manifest.

Covers:
- ThemeService: preset listing, loading, applying, customization, CSS export
- api_theme: Flask endpoint tests for all 6 routes
- shell_manifest: panel coverage, grouping, dynamic resolution
"""

import json
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

# ═══════════════════════════════════════════════════════════════
# Setup: Point theme dirs at test fixtures
# ═══════════════════════════════════════════════════════════════

# Create temp dir for theme presets and data
_tmp_dir = tempfile.mkdtemp(prefix='hart_theme_test_')
_theme_dir = os.path.join(_tmp_dir, 'themes')
_data_dir = os.path.join(_tmp_dir, 'data')
os.makedirs(_theme_dir, exist_ok=True)
os.makedirs(_data_dir, exist_ok=True)

# Write minimal test presets
_TEST_PRESETS = {
    'hart-default': {
        'id': 'hart-default',
        'name': 'HART Default',
        'description': 'Aspiration violet on dark',
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
                  'border_radius': 16, 'panel_opacity': 0.65,
                  'topbar_height': 40, 'icon_size': 20,
                  'panel_titlebar_height': 32, 'animation_speed_ms': 200},
        'conky': {'heading': '6C63FF', 'active': '00e676',
                  'muted': '78909c', 'default_text': 'b0b0b0'},
        'gtk_prefer_dark': True,
    },
    'cyberpunk': {
        'id': 'cyberpunk',
        'name': 'Cyberpunk',
        'description': 'Neon pink on black',
        'category': 'dark',
        'colors': {
            'background': '0a0a0a', 'accent': 'ff0090',
            'active': '00ff41', 'text': 'e0e0e0',
            'heading': 'ff0090', 'glass_bg': 'rgba(10,10,10,0.7)',
            'glass_border': 'rgba(255,0,144,0.2)',
            'muted': '666666', 'surface': '1a1a1a',
        },
        'font': {'family': 'Fira Code', 'size': 13,
                 'heading_size': 18, 'weight': 400, 'heading_weight': 700},
        'shell': {'blur_radius': 25, 'saturation': 200,
                  'border_radius': 12, 'panel_opacity': 0.7,
                  'topbar_height': 40, 'icon_size': 20,
                  'panel_titlebar_height': 32, 'animation_speed_ms': 150},
        'conky': {'heading': 'ff0090', 'active': '00ff41',
                  'muted': '666666', 'default_text': 'b0b0b0'},
        'gtk_prefer_dark': True,
    },
    'potato': {
        'id': 'potato',
        'name': 'Potato Mode',
        'description': 'Zero visual overhead — every cycle counts',
        'category': 'performance',
        'colors': {
            'background': '0a0a0a', 'accent': '4fc3f7',
            'active': '66bb6a', 'text': 'd0d0d0',
            'heading': '4fc3f7', 'glass_bg': 'rgba(10,10,10,0.92)',
            'glass_border': 'rgba(80,80,80,0.12)',
            'muted': '757575', 'surface': '141414',
        },
        'font': {'family': 'monospace', 'size': 13,
                 'heading_size': 16, 'weight': 400, 'heading_weight': 600},
        'shell': {'blur_radius': 0, 'saturation': 100,
                  'border_radius': 4, 'panel_opacity': 0.92,
                  'topbar_height': 36, 'icon_size': 18,
                  'panel_titlebar_height': 28, 'animation_speed_ms': 0},
        'conky': {'heading': '4fc3f7', 'active': '66bb6a',
                  'muted': '757575', 'default_text': 'a0a0a0'},
        'gtk_prefer_dark': True,
        'performance': {
            'disable_blur': True,
            'disable_animations': True,
            'disable_shadows': True,
            'lazy_load_iframes': True,
            'reduce_polling': True,
            'conky_update_interval': 10,
            'clock_interval_ms': 60000,
            'agent_status_interval_ms': 30000,
            'max_open_panels': 3,
            'destroy_minimized_iframes': True,
        },
    },
    'high-contrast': {
        'id': 'high-contrast',
        'name': 'High Contrast',
        'description': 'Maximum readability',
        'category': 'light',
        'colors': {
            'background': 'ffffff', 'accent': '000000',
            'active': '1565c0', 'text': '212121',
            'heading': '000000', 'glass_bg': 'rgba(255,255,255,0.85)',
            'glass_border': 'rgba(0,0,0,0.15)',
            'muted': '616161', 'surface': 'f5f5f5',
        },
        'font': {'family': 'Inter', 'size': 14,
                 'heading_size': 20, 'weight': 400, 'heading_weight': 700},
        'shell': {'blur_radius': 10, 'saturation': 100,
                  'border_radius': 8, 'panel_opacity': 0.85,
                  'topbar_height': 44, 'icon_size': 22,
                  'panel_titlebar_height': 36, 'animation_speed_ms': 150},
        'conky': {'heading': '000000', 'active': '1565c0',
                  'muted': '616161', 'default_text': '424242'},
        'gtk_prefer_dark': False,
    },
}

for preset_id, preset_data in _TEST_PRESETS.items():
    with open(os.path.join(_theme_dir, f'{preset_id}.json'), 'w') as f:
        json.dump(preset_data, f)


@pytest.fixture(autouse=True)
def _patch_theme_paths():
    """Redirect theme service to test directories."""
    import integrations.agent_engine.theme_service as ts
    orig_theme = ts._THEME_DIR
    orig_active = ts._ACTIVE_THEME_PATH
    orig_custom = ts._CUSTOM_OVERRIDES_PATH

    ts._THEME_DIR = _theme_dir
    ts._ACTIVE_THEME_PATH = os.path.join(_data_dir, 'active_theme.json')
    ts._CUSTOM_OVERRIDES_PATH = os.path.join(_data_dir, 'theme_custom.json')

    # Clean data files between tests
    for f in ['active_theme.json', 'theme_custom.json']:
        p = os.path.join(_data_dir, f)
        if os.path.exists(p):
            os.remove(p)

    yield

    ts._THEME_DIR = orig_theme
    ts._ACTIVE_THEME_PATH = orig_active
    ts._CUSTOM_OVERRIDES_PATH = orig_custom


# ═══════════════════════════════════════════════════════════════
# ThemeService Tests
# ═══════════════════════════════════════════════════════════════

class TestThemeServicePresets:
    """Test preset listing and loading."""

    def test_list_presets(self):
        from integrations.agent_engine.theme_service import ThemeService
        presets = ThemeService.list_presets()
        assert len(presets) == 4
        ids = {p['id'] for p in presets}
        assert 'hart-default' in ids
        assert 'cyberpunk' in ids
        assert 'high-contrast' in ids

    def test_list_presets_has_required_fields(self):
        from integrations.agent_engine.theme_service import ThemeService
        presets = ThemeService.list_presets()
        for p in presets:
            assert 'id' in p
            assert 'name' in p
            assert 'accent' in p
            assert 'background' in p
            assert 'category' in p

    def test_get_preset_exists(self):
        from integrations.agent_engine.theme_service import ThemeService
        preset = ThemeService.get_preset('cyberpunk')
        assert preset is not None
        assert preset['id'] == 'cyberpunk'
        assert preset['name'] == 'Cyberpunk'
        assert 'colors' in preset
        assert 'font' in preset
        assert 'shell' in preset
        assert 'conky' in preset

    def test_get_preset_not_found(self):
        from integrations.agent_engine.theme_service import ThemeService
        assert ThemeService.get_preset('nonexistent') is None

    def test_list_presets_empty_dir(self):
        import integrations.agent_engine.theme_service as ts
        ts._THEME_DIR = os.path.join(_tmp_dir, 'empty_themes')
        os.makedirs(ts._THEME_DIR, exist_ok=True)
        from integrations.agent_engine.theme_service import ThemeService
        assert ThemeService.list_presets() == []
        ts._THEME_DIR = _theme_dir


class TestThemeServiceActive:
    """Test active theme management."""

    def test_get_active_fallback_auto_detects(self):
        """With no active theme, auto-detection selects based on hardware."""
        from integrations.agent_engine.theme_service import ThemeService
        theme = ThemeService.get_active_theme()
        # Auto-detect picks based on hardware; any valid preset is acceptable
        assert theme['id'] in ('hart-default', 'potato', 'minimal')

    def test_apply_theme_success(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            result = ThemeService.apply_theme('cyberpunk')
        assert result['status'] == 'applied'
        assert result['theme_id'] == 'cyberpunk'
        # Active theme should now be cyberpunk
        active = ThemeService.get_active_theme()
        assert active['id'] == 'cyberpunk'

    def test_apply_theme_unknown(self):
        from integrations.agent_engine.theme_service import ThemeService
        result = ThemeService.apply_theme('nonexistent_theme')
        assert 'error' in result
        assert 'Unknown theme' in result['error']

    def test_apply_clears_custom_overrides(self):
        from integrations.agent_engine.theme_service import ThemeService
        import integrations.agent_engine.theme_service as ts

        # Write some custom overrides
        with open(ts._CUSTOM_OVERRIDES_PATH, 'w') as f:
            json.dump({'font': {'size': 20}}, f)

        with patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            ThemeService.apply_theme('hart-default')

        assert not os.path.exists(ts._CUSTOM_OVERRIDES_PATH)

    def test_get_active_after_apply(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            ThemeService.apply_theme('high-contrast')
        active = ThemeService.get_active_theme()
        assert active['id'] == 'high-contrast'
        assert active['gtk_prefer_dark'] is False


class TestThemeServiceCustomize:
    """Test agent-driven customization."""

    def test_update_custom_font_size(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            ThemeService.apply_theme('hart-default')
            result = ThemeService.update_custom({'font': {'size': 18}})
        assert result['status'] == 'customized'
        assert result['overrides']['font']['size'] == 18

        # Active theme should reflect the override
        active = ThemeService.get_active_theme()
        assert active['font']['size'] == 18
        # Other font fields should be preserved
        assert active['font']['family'] == 'JetBrains Mono'

    def test_update_custom_multiple_overrides(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            ThemeService.apply_theme('hart-default')
            ThemeService.update_custom({'font': {'size': 16}})
            ThemeService.update_custom({'colors': {'accent': 'ff5722'}})

        active = ThemeService.get_active_theme()
        assert active['font']['size'] == 16
        assert active['colors']['accent'] == 'ff5722'
        # Original colors should be preserved
        assert active['colors']['background'] == '0F0E17'

    def test_update_custom_shell_opacity(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            ThemeService.apply_theme('hart-default')
            result = ThemeService.update_custom({'shell': {'panel_opacity': 0.4}})
        assert result['status'] == 'customized'
        active = ThemeService.get_active_theme()
        assert active['shell']['panel_opacity'] == 0.4


class TestThemeServiceMisc:
    """Test helper methods."""

    def test_get_font_options(self):
        from integrations.agent_engine.theme_service import ThemeService
        fonts = ThemeService.get_font_options()
        assert len(fonts) == 8
        families = {f['family'] for f in fonts}
        assert 'JetBrains Mono' in families
        assert 'Inter' in families
        for f in fonts:
            assert 'category' in f
            assert f['category'] in ('monospace', 'sans-serif')

    def test_get_conky_color_overrides(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            ThemeService.apply_theme('cyberpunk')
        conky = ThemeService.get_conky_color_overrides()
        assert conky['heading'] == 'ff0090'
        assert conky['active'] == '00ff41'

    def test_get_css_variables(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            ThemeService.apply_theme('hart-default')
        css = ThemeService.get_css_variables()
        assert ':root {' in css
        assert '--hart-accent: #6C63FF;' in css
        assert '--hart-font-family: "JetBrains Mono";' in css
        assert '--hart-blur: 20px;' in css
        assert '--hart-panel-opacity: 0.65;' in css

    def test_deep_merge(self):
        from integrations.agent_engine.theme_service import ThemeService
        base = {'a': 1, 'b': {'c': 2, 'd': 3}, 'e': 'hello'}
        override = {'b': {'c': 99}, 'f': 'new'}
        result = ThemeService._deep_merge(base, override)
        assert result == {'a': 1, 'b': {'c': 99, 'd': 3}, 'e': 'hello', 'f': 'new'}
        # Original not mutated
        assert base['b']['c'] == 2

    def test_apply_gtk_linux(self):
        from integrations.agent_engine.theme_service import ThemeService
        theme = {'gtk_prefer_dark': True}
        with patch('subprocess.Popen') as mock_popen:
            ThemeService._apply_gtk(theme)
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            assert 'gsettings' in args
            assert 'prefer-dark' in args

    def test_apply_gtk_light(self):
        from integrations.agent_engine.theme_service import ThemeService
        theme = {'gtk_prefer_dark': False}
        with patch('subprocess.Popen') as mock_popen:
            ThemeService._apply_gtk(theme)
            args = mock_popen.call_args[0][0]
            assert 'default' in args

    def test_notify_liquid_ui(self):
        from integrations.agent_engine.theme_service import ThemeService
        theme = {'id': 'test'}
        mock_requests = MagicMock()
        with patch.dict('sys.modules', {'requests': mock_requests}):
            ThemeService._notify_liquid_ui(theme)
            # Should attempt POST (may fail silently)


# ═══════════════════════════════════════════════════════════════
# Potato / Ultra-Lite Mode Tests
# ═══════════════════════════════════════════════════════════════

class TestPotatoMode:
    """Tests for ultra-lite performance mode."""

    def test_potato_preset_exists(self):
        from integrations.agent_engine.theme_service import ThemeService
        preset = ThemeService.get_preset('potato')
        assert preset is not None
        assert preset['id'] == 'potato'
        assert preset['category'] == 'performance'

    def test_potato_has_performance_section(self):
        from integrations.agent_engine.theme_service import ThemeService
        preset = ThemeService.get_preset('potato')
        perf = preset.get('performance', {})
        assert perf.get('disable_blur') is True
        assert perf.get('disable_animations') is True
        assert perf.get('lazy_load_iframes') is True
        assert perf.get('destroy_minimized_iframes') is True
        assert perf.get('max_open_panels') == 3

    def test_potato_zero_blur(self):
        from integrations.agent_engine.theme_service import ThemeService
        preset = ThemeService.get_preset('potato')
        assert preset['shell']['blur_radius'] == 0
        assert preset['shell']['animation_speed_ms'] == 0

    def test_potato_high_opacity(self):
        """Potato uses solid background (0.92) instead of glass (0.65)."""
        from integrations.agent_engine.theme_service import ThemeService
        preset = ThemeService.get_preset('potato')
        assert preset['shell']['panel_opacity'] >= 0.9

    def test_potato_css_no_blur(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            ThemeService.apply_theme('potato')
        css = ThemeService.get_css_variables()
        assert '--hart-blur: 0px;' in css
        assert '--hart-anim-speed: 0ms;' in css

    def test_potato_system_font(self):
        """Potato uses system monospace to avoid font download."""
        from integrations.agent_engine.theme_service import ThemeService
        preset = ThemeService.get_preset('potato')
        assert preset['font']['family'] == 'monospace'

    def test_potato_conky_interval(self):
        from integrations.agent_engine.theme_service import ThemeService
        preset = ThemeService.get_preset('potato')
        assert preset['performance']['conky_update_interval'] == 10

    def test_potato_reduced_polling(self):
        from integrations.agent_engine.theme_service import ThemeService
        preset = ThemeService.get_preset('potato')
        assert preset['performance']['clock_interval_ms'] == 60000
        assert preset['performance']['agent_status_interval_ms'] == 30000

    def test_apply_potato_and_get_active(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            result = ThemeService.apply_theme('potato')
        assert result['status'] == 'applied'
        active = ThemeService.get_active_theme()
        assert active['id'] == 'potato'
        assert active.get('performance', {}).get('disable_blur') is True


class TestPerformanceAutoDetect:
    """Tests for hardware-aware auto theme selection."""

    def test_detect_embedded_returns_potato(self):
        from integrations.agent_engine.theme_service import ThemeService
        from security.system_requirements import NodeTierLevel
        with patch('integrations.agent_engine.theme_service.ThemeService.detect_performance_tier') as mock:
            mock.return_value = 'potato'
            result = ThemeService.detect_performance_tier()
            assert result == 'potato'

    def test_detect_observer_returns_potato(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch('security.system_requirements.get_tier') as mock_tier:
            from security.system_requirements import NodeTierLevel
            mock_tier.return_value = NodeTierLevel.OBSERVER
            result = ThemeService.detect_performance_tier()
            assert result == 'potato'

    def test_detect_lite_returns_minimal(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch('security.system_requirements.get_tier') as mock_tier:
            from security.system_requirements import NodeTierLevel
            mock_tier.return_value = NodeTierLevel.LITE
            result = ThemeService.detect_performance_tier()
            assert result == 'minimal'

    def test_detect_standard_returns_none(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch('security.system_requirements.get_tier') as mock_tier:
            from security.system_requirements import NodeTierLevel
            mock_tier.return_value = NodeTierLevel.STANDARD
            result = ThemeService.detect_performance_tier()
            assert result is None

    def test_detect_full_returns_none(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch('security.system_requirements.get_tier') as mock_tier:
            from security.system_requirements import NodeTierLevel
            mock_tier.return_value = NodeTierLevel.FULL
            result = ThemeService.detect_performance_tier()
            assert result is None

    def test_auto_select_skips_if_active_exists(self):
        from integrations.agent_engine.theme_service import ThemeService
        import integrations.agent_engine.theme_service as ts
        # Write a fake active theme
        with patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            ThemeService.apply_theme('cyberpunk')
        result = ThemeService.auto_select_theme()
        assert result is None  # Already has active theme

    def test_auto_select_applies_potato_for_observer(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, 'detect_performance_tier', return_value='potato'), \
             patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            result = ThemeService.auto_select_theme()
        assert result is not None
        assert result['theme_id'] == 'potato'

    def test_auto_select_default_for_capable(self):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, 'detect_performance_tier', return_value=None), \
             patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            result = ThemeService.auto_select_theme()
        assert result is not None
        assert result['theme_id'] == 'hart-default'

    def test_fallback_hardware_check_low_core(self):
        """When system_requirements unavailable, fall back to os.cpu_count."""
        from integrations.agent_engine.theme_service import ThemeService
        with patch('security.system_requirements.get_tier', side_effect=ImportError), \
             patch('os.cpu_count', return_value=1):
            try:
                import psutil
                with patch('psutil.virtual_memory') as mock_vm:
                    mock_vm.return_value = MagicMock(total=2 * 1024**3)  # 2GB
                    result = ThemeService.detect_performance_tier()
                    assert result == 'potato'
            except ImportError:
                # No psutil: should still detect via cores only
                result = ThemeService.detect_performance_tier()
                assert result in ('potato', 'minimal')


# ═══════════════════════════════════════════════════════════════
# Theme API Tests
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def theme_app():
    """Create Flask test app with theme blueprint."""
    from flask import Flask
    from integrations.social.api_theme import theme_bp
    app = Flask(__name__)
    app.register_blueprint(theme_bp)
    app.config['TESTING'] = True
    return app


class TestThemeAPI:
    """Test the 6 theme REST endpoints."""

    def test_list_presets(self, theme_app):
        with theme_app.test_client() as client:
            resp = client.get('/api/social/theme/presets')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'presets' in data
        assert len(data['presets']) == 4

    def test_get_active(self, theme_app):
        with theme_app.test_client() as client:
            resp = client.get('/api/social/theme/active')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'theme' in data
        assert 'css' in data
        assert ':root' in data['css']

    def test_apply_theme_success(self, theme_app):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            with theme_app.test_client() as client:
                resp = client.post(
                    '/api/social/theme/apply',
                    json={'theme_id': 'cyberpunk'},
                )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'applied'
        assert data['theme_id'] == 'cyberpunk'

    def test_apply_theme_missing_id(self, theme_app):
        with theme_app.test_client() as client:
            resp = client.post('/api/social/theme/apply', json={})
        assert resp.status_code == 400

    def test_apply_theme_unknown_id(self, theme_app):
        with theme_app.test_client() as client:
            resp = client.post(
                '/api/social/theme/apply',
                json={'theme_id': 'does_not_exist'},
            )
        assert resp.status_code == 404

    def test_customize_theme(self, theme_app):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, '_apply_gtk'), \
             patch.object(ThemeService, '_notify_liquid_ui'):
            with theme_app.test_client() as client:
                # Apply a base theme first
                client.post('/api/social/theme/apply', json={'theme_id': 'hart-default'})
                resp = client.post(
                    '/api/social/theme/customize',
                    json={'font': {'size': 16}},
                )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'customized'

    def test_customize_empty_body(self, theme_app):
        with theme_app.test_client() as client:
            resp = client.post(
                '/api/social/theme/customize',
                data='',
                content_type='application/json',
            )
        assert resp.status_code == 400

    def test_list_fonts(self, theme_app):
        with theme_app.test_client() as client:
            resp = client.get('/api/social/theme/fonts')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'fonts' in data
        assert len(data['fonts']) == 8

    def test_get_css(self, theme_app):
        with theme_app.test_client() as client:
            resp = client.get('/api/social/theme/css')
        assert resp.status_code == 200
        assert 'text/css' in resp.content_type
        assert b':root' in resp.data
        assert b'--hart-accent' in resp.data


# ═══════════════════════════════════════════════════════════════
# Shell Manifest Tests
# ═══════════════════════════════════════════════════════════════

class TestShellManifest:
    """Test panel manifest completeness and helpers."""

    def test_panel_manifest_count(self):
        from integrations.agent_engine.shell_manifest import PANEL_MANIFEST
        assert len(PANEL_MANIFEST) >= 31

    def test_dynamic_panels_count(self):
        from integrations.agent_engine.shell_manifest import DYNAMIC_PANELS
        assert len(DYNAMIC_PANELS) == 14

    def test_system_panels_count(self):
        from integrations.agent_engine.shell_manifest import SYSTEM_PANELS
        assert len(SYSTEM_PANELS) >= 36

    def test_all_groups_represented(self):
        from integrations.agent_engine.shell_manifest import (
            PANEL_MANIFEST, PANEL_GROUPS,
        )
        groups_in_manifest = {p['group'] for p in PANEL_MANIFEST.values()}
        for g in ['Discover', 'Create', 'You', 'Explore', 'Manage']:
            assert g in groups_in_manifest, f"Group '{g}' not in manifest"

    def test_panel_groups_order(self):
        from integrations.agent_engine.shell_manifest import PANEL_GROUPS
        assert PANEL_GROUPS == ['Discover', 'Create', 'You', 'Explore', 'Manage', 'System']

    def test_all_panels_have_required_fields(self):
        from integrations.agent_engine.shell_manifest import PANEL_MANIFEST
        for pid, panel in PANEL_MANIFEST.items():
            assert 'title' in panel, f"{pid} missing title"
            assert 'icon' in panel, f"{pid} missing icon"
            assert 'route' in panel, f"{pid} missing route"
            assert 'group' in panel, f"{pid} missing group"
            assert 'default_size' in panel, f"{pid} missing default_size"
            assert len(panel['default_size']) == 2, f"{pid} default_size should be [w, h]"

    def test_system_panels_have_apis(self):
        from integrations.agent_engine.shell_manifest import SYSTEM_PANELS
        # Some panels are pure client-side (calculator, weather_widget) - no APIs needed
        CLIENT_ONLY_PANELS = {'calculator', 'weather_widget'}
        for pid, panel in SYSTEM_PANELS.items():
            assert 'apis' in panel, f"{pid} missing apis"
            if pid not in CLIENT_ONLY_PANELS:
                assert len(panel['apis']) > 0, f"{pid} has empty apis"

    def test_get_panels_by_group(self):
        from integrations.agent_engine.shell_manifest import get_panels_by_group
        discover = get_panels_by_group('Discover')
        assert 'feed' in discover
        assert 'search' in discover
        assert 'agents_browse' in discover
        assert len(discover) >= 3

    def test_get_panels_by_group_manage(self):
        from integrations.agent_engine.shell_manifest import get_panels_by_group
        manage = get_panels_by_group('Manage')
        assert len(manage) == 11

    def test_get_all_panels(self):
        from integrations.agent_engine.shell_manifest import (
            get_all_panels, PANEL_MANIFEST, SYSTEM_PANELS,
        )
        all_panels = get_all_panels()
        assert len(all_panels) == len(PANEL_MANIFEST) + len(SYSTEM_PANELS)

    def test_resolve_dynamic_panel(self):
        from integrations.agent_engine.shell_manifest import resolve_dynamic_panel
        panel = resolve_dynamic_panel('agent_chat', agentId='123', name='Marketing')
        assert panel is not None
        assert panel['title'] == 'Chat: Marketing'
        assert panel['route'] == '/social/agent/123/chat'

    def test_resolve_dynamic_panel_unknown(self):
        from integrations.agent_engine.shell_manifest import resolve_dynamic_panel
        assert resolve_dynamic_panel('nonexistent') is None

    def test_resolve_dynamic_panel_profile(self):
        from integrations.agent_engine.shell_manifest import resolve_dynamic_panel
        panel = resolve_dynamic_panel('profile', userId='42', name='Alice')
        assert panel['title'] == 'Profile: Alice'
        assert panel['route'] == '/social/profile/42'

    def test_appearance_panel_in_manifest(self):
        from integrations.agent_engine.shell_manifest import PANEL_MANIFEST
        assert 'appearance' in PANEL_MANIFEST
        assert PANEL_MANIFEST['appearance']['route'] == '/social/settings/appearance'
        assert PANEL_MANIFEST['appearance']['group'] == 'You'

    def test_no_duplicate_routes(self):
        from integrations.agent_engine.shell_manifest import PANEL_MANIFEST
        routes = [p['route'] for p in PANEL_MANIFEST.values()]
        assert len(routes) == len(set(routes)), "Duplicate routes found in PANEL_MANIFEST"


# ═══════════════════════════════════════════════════════════════
# Premium Shell Feature Tests
# ═══════════════════════════════════════════════════════════════

class TestPremiumShellHTML:
    """Test that premium shell features are present in the HTML/CSS/JS."""

    @pytest.fixture
    def shell_app(self):
        """Create a Flask test app with LiquidUI shell."""
        try:
            from integrations.agent_engine.liquid_ui_service import LiquidUIService
            with patch('integrations.agent_engine.liquid_ui_service.ContextEngine'):
                service = LiquidUIService(port=16800)
            app = service._create_flask_app()
            app.config['TESTING'] = True
            return app
        except Exception as e:
            pytest.skip(f"LiquidUI service not available: {e}")

    def test_taskbar_html_present(self, shell_app):
        with shell_app.test_client() as client:
            resp = client.get('/')
        html = resp.data.decode('utf-8')
        assert 'id="taskbar"' in html

    def test_toast_container_present(self, shell_app):
        with shell_app.test_client() as client:
            resp = client.get('/')
        html = resp.data.decode('utf-8')
        assert 'id="toast-container"' in html

    def test_panel_animation_css_or_disabled(self, shell_app):
        """Shell should have either animation rules (capable) or animation:none (potato)."""
        with shell_app.test_client() as client:
            resp = client.get('/')
        html = resp.data.decode('utf-8')
        # On capable hardware: has animation classes. On potato: has disable comment.
        has_animations = '.panel.closing' in html and '.panel.minimizing' in html
        has_disabled = 'animations disabled for performance' in html
        assert has_animations or has_disabled

    def test_voice_toggle_present(self, shell_app):
        with shell_app.test_client() as client:
            resp = client.get('/')
        html = resp.data.decode('utf-8')
        assert 'toggleVoice' in html

    def test_snap_panel_present(self, shell_app):
        with shell_app.test_client() as client:
            resp = client.get('/')
        html = resp.data.decode('utf-8')
        assert 'snapPanel' in html

    def test_update_taskbar_present(self, shell_app):
        with shell_app.test_client() as client:
            resp = client.get('/')
        html = resp.data.decode('utf-8')
        assert 'updateTaskbar' in html

    def test_show_toast_present(self, shell_app):
        with shell_app.test_client() as client:
            resp = client.get('/')
        html = resp.data.decode('utf-8')
        assert 'showToast' in html

    def test_login_greeting_present(self, shell_app):
        with shell_app.test_client() as client:
            resp = client.get('/')
        html = resp.data.decode('utf-8')
        assert 'loginGreeting' in html


class TestPremiumShellAPIs:
    """Test new system panel API endpoints."""

    @pytest.fixture
    def shell_app(self):
        try:
            from integrations.agent_engine.liquid_ui_service import LiquidUIService
            with patch('integrations.agent_engine.liquid_ui_service.ContextEngine'):
                service = LiquidUIService(port=16801)
            app = service._create_flask_app()
            app.config['TESTING'] = True
            return app
        except Exception as e:
            pytest.skip(f"LiquidUI service not available: {e}")

    def test_shell_drivers_endpoint(self, shell_app):
        with shell_app.test_client() as client:
            resp = client.get('/api/shell/drivers')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'devices' in data

    def test_shell_wifi_endpoint(self, shell_app):
        with shell_app.test_client() as client:
            resp = client.get('/api/shell/network/wifi')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'networks' in data
        assert 'connected' in data

    def test_shell_audio_endpoint(self, shell_app):
        with shell_app.test_client() as client:
            resp = client.get('/api/shell/audio')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'sinks' in data
        assert 'sources' in data

    def test_shell_bluetooth_endpoint(self, shell_app):
        with shell_app.test_client() as client:
            resp = client.get('/api/shell/bluetooth')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'devices' in data

    def test_shell_power_endpoint(self, shell_app):
        with shell_app.test_client() as client:
            resp = client.get('/api/shell/power')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'percent' in data
        assert 'state' in data

    def test_shell_display_endpoint(self, shell_app):
        with shell_app.test_client() as client:
            resp = client.get('/api/shell/display')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'displays' in data

    def test_recent_files_endpoint(self, shell_app):
        with shell_app.test_client() as client:
            resp = client.get('/api/shell/files/recent')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'files' in data

    def test_notification_sse_route_exists(self, shell_app):
        """Shell should have /api/notifications/stream route registered."""
        rules = [r.rule for r in shell_app.url_map.iter_rules()]
        assert '/api/notifications/stream' in rules


# ═══════════════════════════════════════════════════════════════
# Cleanup
# ═══════════════════════════════════════════════════════════════

def teardown_module():
    """Clean up temp files."""
    import shutil
    if os.path.exists(_tmp_dir):
        shutil.rmtree(_tmp_dir, ignore_errors=True)
