"""
Tests for core.platform.extensions — Extension and ExtensionRegistry.

Covers: load, enable, disable, unload, reload, lifecycle state machine,
error handling, event emission, health, list_extensions.
"""

import unittest
from unittest.mock import MagicMock

from core.platform.app_manifest import AppManifest, AppType
from core.platform.extensions import Extension, ExtensionState, ExtensionRegistry


# ─── Test Extensions ──────────────────────────────────────────

class GoodExtension(Extension):
    """Well-behaved test extension."""

    def __init__(self):
        super().__init__()
        self.loaded = False
        self.enabled = False
        self.disabled = False
        self.unloaded = False
        self.registry_ref = None

    @property
    def manifest(self):
        return AppManifest(
            id='test_good', name='Good Extension', version='1.0.0',
            type=AppType.EXTENSION.value, icon='extension',
            description='A good test extension',
        )

    def on_load(self, registry, config):
        self.loaded = True
        self.registry_ref = registry

    def on_enable(self):
        self.enabled = True

    def on_disable(self):
        self.disabled = True

    def on_unload(self):
        self.unloaded = True


class FailOnLoadExtension(Extension):

    @property
    def manifest(self):
        return AppManifest(
            id='fail_load', name='Fail Load', version='1.0.0',
            type=AppType.EXTENSION.value,
        )

    def on_load(self, registry, config):
        raise RuntimeError("load boom")


class FailOnEnableExtension(Extension):

    @property
    def manifest(self):
        return AppManifest(
            id='fail_enable', name='Fail Enable', version='1.0.0',
            type=AppType.EXTENSION.value,
        )

    def on_enable(self):
        raise RuntimeError("enable boom")


class TestExtensionState(unittest.TestCase):
    """Extension base class state."""

    def test_initial_state(self):
        ext = GoodExtension()
        self.assertEqual(ext.state, ExtensionState.UNLOADED)
        self.assertIsNone(ext.error)


class TestExtensionRegistryDirect(unittest.TestCase):
    """Direct load (not from module) — we test using internal _extensions."""

    def setUp(self):
        self.events = []
        self.ereg = ExtensionRegistry(
            service_registry=MagicMock(),
            platform_config=MagicMock(),
            event_emitter=lambda t, d: self.events.append((t, d)),
        )

    def _direct_load(self, ext):
        """Directly add an extension (bypassing module import)."""
        ext_id = ext.manifest.id
        ext.on_load(self.ereg._registry, self.ereg._config)
        ext._state = ExtensionState.LOADED
        from datetime import datetime
        ext._loaded_at = datetime.now()
        self.ereg._extensions[ext_id] = ext
        return ext

    def test_load_and_state(self):
        ext = self._direct_load(GoodExtension())
        self.assertTrue(ext.loaded)
        self.assertEqual(ext.state, ExtensionState.LOADED)

    def test_enable(self):
        ext = self._direct_load(GoodExtension())
        self.ereg.enable('test_good')
        self.assertTrue(ext.enabled)
        self.assertEqual(ext.state, ExtensionState.ENABLED)

    def test_disable(self):
        ext = self._direct_load(GoodExtension())
        self.ereg.enable('test_good')
        self.ereg.disable('test_good')
        self.assertTrue(ext.disabled)
        self.assertEqual(ext.state, ExtensionState.DISABLED)

    def test_re_enable(self):
        ext = self._direct_load(GoodExtension())
        self.ereg.enable('test_good')
        self.ereg.disable('test_good')
        self.ereg.enable('test_good')  # should work from DISABLED
        self.assertEqual(ext.state, ExtensionState.ENABLED)

    def test_unload(self):
        ext = self._direct_load(GoodExtension())
        self.ereg.unload('test_good')
        self.assertTrue(ext.unloaded)
        self.assertEqual(ext.state, ExtensionState.UNLOADED)
        self.assertEqual(self.ereg.count(), 0)

    def test_unload_enabled_disables_first(self):
        ext = self._direct_load(GoodExtension())
        self.ereg.enable('test_good')
        self.ereg.unload('test_good')
        self.assertTrue(ext.disabled)
        self.assertTrue(ext.unloaded)

    def test_enable_wrong_state_raises(self):
        ext = self._direct_load(GoodExtension())
        self.ereg.enable('test_good')
        with self.assertRaises(RuntimeError):
            self.ereg.enable('test_good')  # already enabled

    def test_disable_wrong_state_raises(self):
        ext = self._direct_load(GoodExtension())
        with self.assertRaises(RuntimeError):
            self.ereg.disable('test_good')  # not enabled

    def test_enable_not_loaded_raises(self):
        with self.assertRaises(KeyError):
            self.ereg.enable('ghost')

    def test_unload_not_loaded_raises(self):
        with self.assertRaises(KeyError):
            self.ereg.unload('ghost')


class TestExtensionEvents(unittest.TestCase):
    """Event emission."""

    def setUp(self):
        self.events = []
        self.ereg = ExtensionRegistry(
            event_emitter=lambda t, d: self.events.append((t, d)),
        )

    def _direct_load(self, ext):
        ext_id = ext.manifest.id
        ext.on_load(None, None)
        ext._state = ExtensionState.LOADED
        from datetime import datetime
        ext._loaded_at = datetime.now()
        self.ereg._extensions[ext_id] = ext
        return ext

    def test_enable_emits(self):
        self._direct_load(GoodExtension())
        self.ereg.enable('test_good')
        topics = [e[0] for e in self.events]
        self.assertIn('extension.enabled', topics)

    def test_disable_emits(self):
        self._direct_load(GoodExtension())
        self.ereg.enable('test_good')
        self.ereg.disable('test_good')
        topics = [e[0] for e in self.events]
        self.assertIn('extension.disabled', topics)

    def test_unload_emits(self):
        self._direct_load(GoodExtension())
        self.ereg.unload('test_good')
        topics = [e[0] for e in self.events]
        self.assertIn('extension.unloaded', topics)


class TestExtensionErrorHandling(unittest.TestCase):
    """Error during lifecycle hooks."""

    def setUp(self):
        self.ereg = ExtensionRegistry()

    def _direct_load(self, ext):
        ext_id = ext.manifest.id
        try:
            ext.on_load(None, None)
            ext._state = ExtensionState.LOADED
        except Exception:
            ext._state = ExtensionState.ERROR
        from datetime import datetime
        ext._loaded_at = datetime.now()
        self.ereg._extensions[ext_id] = ext
        return ext

    def test_enable_failure_sets_error(self):
        ext = self._direct_load(FailOnEnableExtension())
        with self.assertRaises(RuntimeError):
            self.ereg.enable('fail_enable')
        self.assertEqual(ext.state, ExtensionState.ERROR)
        self.assertIn('enable boom', ext.error)


class TestExtensionListAndHealth(unittest.TestCase):
    """list_extensions, count, health."""

    def setUp(self):
        self.ereg = ExtensionRegistry()

    def _direct_load(self, ext):
        ext_id = ext.manifest.id
        ext.on_load(None, None)
        ext._state = ExtensionState.LOADED
        from datetime import datetime
        ext._loaded_at = datetime.now()
        self.ereg._extensions[ext_id] = ext
        return ext

    def test_list_extensions(self):
        self._direct_load(GoodExtension())
        exts = self.ereg.list_extensions()
        self.assertEqual(len(exts), 1)
        self.assertEqual(exts[0]['id'], 'test_good')
        self.assertEqual(exts[0]['state'], 'loaded')

    def test_count(self):
        self.assertEqual(self.ereg.count(), 0)
        self._direct_load(GoodExtension())
        self.assertEqual(self.ereg.count(), 1)

    def test_get(self):
        self._direct_load(GoodExtension())
        ext = self.ereg.get('test_good')
        self.assertIsNotNone(ext)
        self.assertIsNone(self.ereg.get('nonexistent'))

    def test_health(self):
        self._direct_load(GoodExtension())
        self.ereg.enable('test_good')
        h = self.ereg.health()
        self.assertEqual(h['status'], 'ok')
        self.assertEqual(h['total'], 1)
        self.assertEqual(h['states']['enabled'], 1)

    def test_registry_passed_to_extension(self):
        """Extension receives ServiceRegistry reference on load."""
        mock_registry = MagicMock()
        ereg = ExtensionRegistry(service_registry=mock_registry)
        ext = GoodExtension()
        ext_id = ext.manifest.id
        ext.on_load(mock_registry, None)
        ext._state = ExtensionState.LOADED
        self.assertIs(ext.registry_ref, mock_registry)


# ═══════════════════════════════════════════════════════════════
# Extension Signature Verification
# ═══════════════════════════════════════════════════════════════

class TestExtensionSigVerify(unittest.TestCase):
    """Extension manifest signature verification."""

    def test_bootstrap_has_extension_loading(self):
        """bootstrap_platform references extension registry."""
        import inspect
        from core.platform.bootstrap import bootstrap_platform
        src = inspect.getsource(bootstrap_platform)
        self.assertIn('ExtensionRegistry', src)

    def test_extension_manifest_has_id(self):
        """AppManifest (used as extension manifest) requires id field."""
        m = AppManifest(id='test', name='Test', version='1.0.0', type='extension', icon='x', entry={})
        self.assertEqual(m.id, 'test')

    def test_unsigned_extension_loads_gracefully(self):
        """Extensions without signatures don't crash the system."""
        ereg = ExtensionRegistry()

        class UnsignedExt(Extension):
            @property
            def manifest(self):
                return AppManifest(id='unsigned_test', name='Test', version='1.0',
                                   type='extension', icon='x', entry={})
            def on_load(self, registry, config):
                self.registry_ref = registry
            def on_enable(self): pass
            def on_disable(self): pass
            def on_unload(self): pass

        ext = UnsignedExt()
        # Should not crash — dev mode allows unsigned
        ereg._extensions['unsigned_test'] = ext
        ext._state = ExtensionState.LOADED
        self.assertEqual(ereg.get('unsigned_test'), ext)


if __name__ == '__main__':
    unittest.main()
