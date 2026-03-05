"""
Tests for core.platform.config — PlatformConfig.

Covers: 3-layer resolution (env > DB > defaults), TTL cache,
change notifications, typed converters, set/reset, thread safety.
"""

import os
import unittest
from unittest.mock import MagicMock

from core.platform.config import PlatformConfig


class DisplayConfig(PlatformConfig):
    """Test config namespace."""
    _namespace = 'display'
    _defaults = {
        'scale': 1.0,
        'brightness': 1.0,
        'refresh_rate': 60,
        'vsync': True,
        'output': 'HDMI-1',
    }
    _env_map = {
        'scale': ('HART_DISPLAY_SCALE', float),
        'refresh_rate': ('HART_DISPLAY_REFRESH', int),
        'vsync': ('HART_DISPLAY_VSYNC', bool),
    }
    _cache_ttl = 1  # short TTL for testing


class AudioConfig(PlatformConfig):
    """Second config namespace for isolation tests."""
    _namespace = 'audio'
    _defaults = {
        'volume': 75,
        'muted': False,
    }
    _env_map = {
        'volume': ('HART_AUDIO_VOLUME', int),
    }


class TestDefaultResolution(unittest.TestCase):
    """Layer 4: default values."""

    def test_returns_default(self):
        cfg = DisplayConfig()
        self.assertEqual(cfg.get('scale'), 1.0)
        self.assertEqual(cfg.get('refresh_rate'), 60)
        self.assertTrue(cfg.get('vsync'))

    def test_unknown_key_returns_none(self):
        cfg = DisplayConfig()
        self.assertIsNone(cfg.get('nonexistent'))

    def test_unknown_key_with_fallback(self):
        cfg = DisplayConfig()
        self.assertEqual(cfg.get('nonexistent', 'fallback'), 'fallback')

    def test_get_all(self):
        cfg = DisplayConfig()
        all_vals = cfg.get_all()
        self.assertEqual(all_vals['scale'], 1.0)
        self.assertEqual(all_vals['brightness'], 1.0)
        self.assertEqual(all_vals['refresh_rate'], 60)
        self.assertTrue(all_vals['vsync'])
        self.assertEqual(all_vals['output'], 'HDMI-1')

    def test_namespace(self):
        cfg = DisplayConfig()
        self.assertEqual(cfg.namespace, 'display')

    def test_repr(self):
        cfg = DisplayConfig()
        self.assertIn('display', repr(cfg))


class TestEnvResolution(unittest.TestCase):
    """Layer 1: environment variable override."""

    def setUp(self):
        # Clean env
        for var in ('HART_DISPLAY_SCALE', 'HART_DISPLAY_REFRESH',
                    'HART_DISPLAY_VSYNC', 'HART_AUDIO_VOLUME'):
            os.environ.pop(var, None)

    def tearDown(self):
        for var in ('HART_DISPLAY_SCALE', 'HART_DISPLAY_REFRESH',
                    'HART_DISPLAY_VSYNC', 'HART_AUDIO_VOLUME'):
            os.environ.pop(var, None)

    def test_env_overrides_default(self):
        os.environ['HART_DISPLAY_SCALE'] = '2.0'
        cfg = DisplayConfig()
        self.assertEqual(cfg.get('scale'), 2.0)

    def test_env_int_conversion(self):
        os.environ['HART_DISPLAY_REFRESH'] = '144'
        cfg = DisplayConfig()
        self.assertEqual(cfg.get('refresh_rate'), 144)

    def test_env_bool_conversion(self):
        os.environ['HART_DISPLAY_VSYNC'] = 'false'
        cfg = DisplayConfig()
        self.assertFalse(cfg.get('vsync'))

        os.environ['HART_DISPLAY_VSYNC'] = '1'
        self.assertTrue(cfg.get('vsync'))

    def test_env_overrides_db(self):
        """Env var takes precedence over DB value."""
        os.environ['HART_DISPLAY_SCALE'] = '3.0'
        db_loader = MagicMock(return_value=2.0)
        cfg = DisplayConfig(db_loader=db_loader)
        self.assertEqual(cfg.get('scale'), 3.0)

    def test_bad_env_value_falls_through(self):
        os.environ['HART_DISPLAY_SCALE'] = 'not_a_number'
        cfg = DisplayConfig()
        # Should fall through to default
        self.assertEqual(cfg.get('scale'), 1.0)

    def test_key_without_env_map(self):
        """Keys not in _env_map don't check env."""
        cfg = DisplayConfig()
        self.assertEqual(cfg.get('output'), 'HDMI-1')


class TestDBResolution(unittest.TestCase):
    """Layer 3: DB loader."""

    def test_db_value_overrides_default(self):
        db_loader = MagicMock(return_value=1.5)
        cfg = DisplayConfig(db_loader=db_loader)
        self.assertEqual(cfg.get('scale'), 1.5)
        db_loader.assert_called_with('display', 'scale')

    def test_db_returns_none_falls_to_default(self):
        db_loader = MagicMock(return_value=None)
        cfg = DisplayConfig(db_loader=db_loader)
        self.assertEqual(cfg.get('scale'), 1.0)

    def test_db_exception_falls_to_default(self):
        db_loader = MagicMock(side_effect=RuntimeError("DB down"))
        cfg = DisplayConfig(db_loader=db_loader)
        self.assertEqual(cfg.get('scale'), 1.0)

    def test_db_cached_with_ttl(self):
        call_count = 0

        def loader(ns, key):
            nonlocal call_count
            call_count += 1
            return 2.0

        cfg = DisplayConfig(db_loader=loader)
        cfg._cache_ttl = 60  # long TTL

        # First call hits DB
        self.assertEqual(cfg.get('scale'), 2.0)
        self.assertEqual(call_count, 1)

        # Second call uses cache
        self.assertEqual(cfg.get('scale'), 2.0)
        self.assertEqual(call_count, 1)  # still 1 — cached

    def test_cache_invalidated_on_set(self):
        db_loader = MagicMock(return_value=2.0)
        cfg = DisplayConfig(db_loader=db_loader)
        cfg.get('scale')  # populate cache

        cfg.set('scale', 3.0)
        # Cache should be invalidated, but set() puts value in overrides
        self.assertEqual(cfg.get('scale'), 3.0)


class TestSetAndOverride(unittest.TestCase):
    """Layer 2: in-memory set() overrides."""

    def test_set_overrides_default(self):
        cfg = DisplayConfig()
        cfg.set('scale', 2.5)
        self.assertEqual(cfg.get('scale'), 2.5)

    def test_set_persists_to_db(self):
        db_saver = MagicMock()
        cfg = DisplayConfig()
        cfg.set_db_saver(db_saver)
        cfg.set('brightness', 0.8)
        db_saver.assert_called_once_with('display', 'brightness', 0.8)

    def test_set_db_saver_failure_still_overrides(self):
        db_saver = MagicMock(side_effect=RuntimeError("DB down"))
        cfg = DisplayConfig()
        cfg.set_db_saver(db_saver)
        cfg.set('brightness', 0.5)
        # Override still works even if DB save fails
        self.assertEqual(cfg.get('brightness'), 0.5)

    def test_reset_removes_override(self):
        cfg = DisplayConfig()
        cfg.set('scale', 5.0)
        self.assertEqual(cfg.get('scale'), 5.0)
        cfg.reset('scale')
        self.assertEqual(cfg.get('scale'), 1.0)  # back to default

    def test_reset_all(self):
        cfg = DisplayConfig()
        cfg.set('scale', 5.0)
        cfg.set('brightness', 0.1)
        cfg.reset_all()
        self.assertEqual(cfg.get('scale'), 1.0)
        self.assertEqual(cfg.get('brightness'), 1.0)

    def test_env_still_wins_over_set(self):
        """Env var > set() override."""
        os.environ['HART_DISPLAY_SCALE'] = '9.0'
        try:
            cfg = DisplayConfig()
            cfg.set('scale', 2.0)
            self.assertEqual(cfg.get('scale'), 9.0)  # env wins
        finally:
            os.environ.pop('HART_DISPLAY_SCALE', None)


class TestChangeNotifications(unittest.TestCase):
    """on_change / off_change."""

    def test_on_change_fires(self):
        cfg = DisplayConfig()
        events = []
        cfg.on_change('scale', lambda k, old, new: events.append((k, old, new)))
        cfg.set('scale', 2.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], ('scale', 1.0, 2.0))

    def test_no_change_no_notification(self):
        cfg = DisplayConfig()
        events = []
        cfg.on_change('scale', lambda k, old, new: events.append(1))
        cfg.set('scale', 1.0)  # same as default
        self.assertEqual(len(events), 0)

    def test_off_change(self):
        cfg = DisplayConfig()
        events = []
        cb = lambda k, old, new: events.append(1)
        cfg.on_change('scale', cb)
        cfg.set('scale', 2.0)
        self.assertEqual(len(events), 1)

        cfg.off_change('scale', cb)
        cfg.set('scale', 3.0)
        self.assertEqual(len(events), 1)  # no new event

    def test_multiple_listeners(self):
        cfg = DisplayConfig()
        a_events = []
        b_events = []
        cfg.on_change('scale', lambda k, o, n: a_events.append(n))
        cfg.on_change('scale', lambda k, o, n: b_events.append(n))
        cfg.set('scale', 5.0)
        self.assertEqual(a_events, [5.0])
        self.assertEqual(b_events, [5.0])

    def test_listener_error_doesnt_break_others(self):
        cfg = DisplayConfig()
        good_events = []

        def bad_listener(k, o, n):
            raise RuntimeError("boom")

        cfg.on_change('scale', bad_listener)
        cfg.on_change('scale', lambda k, o, n: good_events.append(n))
        cfg.set('scale', 2.0)
        # good_events should still fire despite bad_listener
        self.assertEqual(good_events, [2.0])

    def test_reset_fires_notification(self):
        cfg = DisplayConfig()
        cfg.set('scale', 5.0)
        events = []
        cfg.on_change('scale', lambda k, old, new: events.append(new))
        cfg.reset('scale')
        self.assertEqual(events, [1.0])


class TestIsolation(unittest.TestCase):
    """Multiple config namespaces don't interfere."""

    def test_separate_namespaces(self):
        display = DisplayConfig()
        audio = AudioConfig()
        display.set('scale', 2.0)
        audio.set('volume', 50)
        self.assertEqual(display.get('scale'), 2.0)
        self.assertEqual(audio.get('volume'), 50)
        # Cross-namespace keys don't exist
        self.assertIsNone(display.get('volume'))
        self.assertIsNone(audio.get('scale'))


class TestBoolConverter(unittest.TestCase):
    """Bool converter edge cases."""

    def test_string_true_variants(self):
        from core.platform.config import _convert_bool
        for val in ('true', 'True', 'TRUE', '1', 'yes', 'on'):
            self.assertTrue(_convert_bool(val), f"Failed for {val!r}")

    def test_string_false_variants(self):
        from core.platform.config import _convert_bool
        for val in ('false', 'False', '0', 'no', 'off', ''):
            self.assertFalse(_convert_bool(val), f"Failed for {val!r}")


# ═══════════════════════════════════════════════════════════════
# Settings Export / Import (Cloud Sync)
# ═══════════════════════════════════════════════════════════════

class TestConfigExportImport(unittest.TestCase):
    """Settings export and import for cross-device sync."""

    def test_export_contains_namespace(self):
        cfg = DisplayConfig()
        exported = cfg.export_settings()
        self.assertEqual(exported['namespace'], 'display')
        self.assertIn('exported_at', exported)

    def test_export_contains_values(self):
        cfg = DisplayConfig()
        cfg.set('scale', 2.0)
        exported = cfg.export_settings()
        self.assertEqual(exported['values']['scale'], 2.0)

    def test_import_restores_values(self):
        cfg = DisplayConfig()
        data = {'values': {'scale': 1.5, 'brightness': 0.8}}
        count = cfg.import_settings(data)
        self.assertEqual(count, 2)
        self.assertEqual(cfg.get('scale'), 1.5)
        self.assertEqual(cfg.get('brightness'), 0.8)

    def test_import_ignores_unknown_keys(self):
        cfg = DisplayConfig()
        data = {'values': {'scale': 1.5, 'unknown_key': 99}}
        count = cfg.import_settings(data)
        self.assertEqual(count, 1)

    def test_round_trip(self):
        cfg1 = DisplayConfig()
        cfg1.set('scale', 2.5)
        cfg1.set('brightness', 0.5)
        exported = cfg1.export_settings()
        cfg2 = DisplayConfig()
        cfg2.import_settings(exported)
        self.assertEqual(cfg2.get('scale'), 2.5)
        self.assertEqual(cfg2.get('brightness'), 0.5)

    def test_import_empty(self):
        cfg = DisplayConfig()
        count = cfg.import_settings({})
        self.assertEqual(count, 0)


if __name__ == '__main__':
    unittest.main()
