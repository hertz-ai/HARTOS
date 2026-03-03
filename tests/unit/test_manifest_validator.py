"""Tests for core.platform.manifest_validator — Manifest Validation & OS Contracts (WS5)."""

import sys
import os
import math
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.platform.manifest_validator import (
    ManifestValidator,
    KNOWN_PERMISSIONS,
    ENTRY_SCHEMA,
    _VALID_TYPES,
    _VALID_AI_CAPABILITY_TYPES,
)
from core.platform.app_manifest import AppManifest, AppType
from core.platform.app_registry import AppRegistry


# ─── Valid Manifests (one per AppType) ──────────────────────────────

class TestValidManifests(unittest.TestCase):

    def test_nunba_panel(self):
        m = AppManifest(id='feed', name='Feed', version='1.0.0',
                        type='nunba_panel', entry={'route': '/social'})
        valid, errors = ManifestValidator.validate(m)
        self.assertTrue(valid, errors)

    def test_system_panel(self):
        m = AppManifest(id='audio-panel', name='Audio', version='1.0.0',
                        type='system_panel', entry={'loader': 'audio_loader'})
        valid, errors = ManifestValidator.validate(m)
        self.assertTrue(valid, errors)

    def test_dynamic_panel(self):
        m = AppManifest(id='settings-popup', name='Settings', version='1.0.0',
                        type='dynamic_panel', entry={'route': '/settings'})
        valid, errors = ManifestValidator.validate(m)
        self.assertTrue(valid, errors)

    def test_desktop_app(self):
        m = AppManifest(id='rustdesk', name='RustDesk', version='auto',
                        type='desktop_app', entry={'exec': 'rustdesk'})
        valid, errors = ManifestValidator.validate(m)
        self.assertTrue(valid, errors)

    def test_service(self):
        m = AppManifest(id='llama-server', name='LLaMA', version='1.0.0',
                        type='service', entry={'http': 'http://localhost:8080'})
        valid, errors = ManifestValidator.validate(m)
        self.assertTrue(valid, errors)

    def test_agent(self):
        m = AppManifest(id='research-agent', name='Research', version='1.0.0',
                        type='agent', entry={'prompt_id': '123', 'flow_id': '0'})
        valid, errors = ManifestValidator.validate(m)
        self.assertTrue(valid, errors)

    def test_mcp_server(self):
        m = AppManifest(id='mcp-fs', name='Filesystem MCP', version='1.0.0',
                        type='mcp_server', entry={'mcp': 'filesystem'})
        valid, errors = ManifestValidator.validate(m)
        self.assertTrue(valid, errors)

    def test_channel(self):
        m = AppManifest(id='discord-channel', name='Discord', version='1.0.0',
                        type='channel', entry={'adapter': 'discord'})
        valid, errors = ManifestValidator.validate(m)
        self.assertTrue(valid, errors)

    def test_extension(self):
        m = AppManifest(id='my-ext', name='My Extension', version='1.0.0',
                        type='extension', entry={'module': 'extensions.my_ext'})
        valid, errors = ManifestValidator.validate(m)
        self.assertTrue(valid, errors)


# ─── Invalid ID ─────────────────────────────────────────────────────

class TestInvalidID(unittest.TestCase):

    def test_empty_id(self):
        m = AppManifest(id='', name='X', version='1.0.0', type='extension',
                        entry={'module': 'm'})
        valid, errors = ManifestValidator.validate(m)
        self.assertFalse(valid)
        self.assertTrue(any('empty' in e for e in errors))

    def test_special_chars(self):
        m = AppManifest(id='../evil', name='X', version='1.0.0',
                        type='extension', entry={'module': 'm'})
        valid, errors = ManifestValidator.validate(m)
        self.assertFalse(valid)

    def test_too_long(self):
        m = AppManifest(id='a' * 65, name='X', version='1.0.0',
                        type='extension', entry={'module': 'm'})
        valid, errors = ManifestValidator.validate(m)
        self.assertFalse(valid)

    def test_starts_with_hyphen(self):
        m = AppManifest(id='-bad', name='X', version='1.0.0',
                        type='extension', entry={'module': 'm'})
        valid, errors = ManifestValidator.validate(m)
        self.assertFalse(valid)

    def test_spaces(self):
        m = AppManifest(id='my app', name='X', version='1.0.0',
                        type='extension', entry={'module': 'm'})
        valid, errors = ManifestValidator.validate(m)
        self.assertFalse(valid)


# ─── Invalid Type ───────────────────────────────────────────────────

class TestInvalidType(unittest.TestCase):

    def test_nonexistent_type(self):
        m = AppManifest(id='test', name='X', version='1.0.0',
                        type='malicious_type', entry={})
        valid, errors = ManifestValidator.validate(m)
        self.assertFalse(valid)
        self.assertTrue(any('type' in e for e in errors))


# ─── Invalid Version ────────────────────────────────────────────────

class TestInvalidVersion(unittest.TestCase):

    def test_latest(self):
        m = AppManifest(id='test', name='X', version='latest',
                        type='extension', entry={'module': 'm'})
        valid, _ = ManifestValidator.validate(m)
        self.assertFalse(valid)

    def test_partial_semver(self):
        m = AppManifest(id='test', name='X', version='1.0',
                        type='extension', entry={'module': 'm'})
        valid, _ = ManifestValidator.validate(m)
        self.assertFalse(valid)

    def test_text_version(self):
        m = AppManifest(id='test', name='X', version='abc',
                        type='extension', entry={'module': 'm'})
        valid, _ = ManifestValidator.validate(m)
        self.assertFalse(valid)

    def test_auto_is_valid(self):
        ok, msg = ManifestValidator.validate_version('auto')
        self.assertTrue(ok)

    def test_semver_is_valid(self):
        ok, msg = ManifestValidator.validate_version('2.1.0')
        self.assertTrue(ok)


# ─── Invalid Entry ──────────────────────────────────────────────────

class TestInvalidEntry(unittest.TestCase):

    def test_panel_missing_route(self):
        m = AppManifest(id='test', name='X', version='1.0.0',
                        type='nunba_panel', entry={})
        valid, errors = ManifestValidator.validate(m)
        self.assertFalse(valid)
        self.assertTrue(any('route' in e for e in errors))

    def test_desktop_missing_exec(self):
        m = AppManifest(id='test', name='X', version='1.0.0',
                        type='desktop_app', entry={})
        valid, errors = ManifestValidator.validate(m)
        self.assertFalse(valid)
        self.assertTrue(any('exec' in e for e in errors))

    def test_agent_missing_prompt_id(self):
        m = AppManifest(id='test', name='X', version='1.0.0',
                        type='agent', entry={'flow_id': '0'})
        valid, errors = ManifestValidator.validate(m)
        self.assertFalse(valid)

    def test_service_needs_http_or_exec(self):
        m = AppManifest(id='test', name='X', version='1.0.0',
                        type='service', entry={})
        valid, errors = ManifestValidator.validate(m)
        self.assertFalse(valid)
        self.assertTrue(any('any_of' in e or 'at least one' in e
                            for e in errors))

    def test_service_with_exec_is_valid(self):
        m = AppManifest(id='test', name='X', version='1.0.0',
                        type='service', entry={'exec': 'myservice'})
        valid, _ = ManifestValidator.validate(m)
        self.assertTrue(valid)


# ─── Invalid Permissions ────────────────────────────────────────────

class TestInvalidPermissions(unittest.TestCase):

    def test_unknown_permission(self):
        m = AppManifest(id='test', name='X', version='1.0.0',
                        type='extension', entry={'module': 'm'},
                        permissions=['network', 'god_mode'])
        valid, errors = ManifestValidator.validate(m)
        self.assertFalse(valid)
        self.assertTrue(any('god_mode' in e for e in errors))

    def test_valid_permissions(self):
        ok, msg = ManifestValidator.validate_permissions(
            ['network', 'audio', 'display'])
        self.assertTrue(ok)

    def test_empty_permissions_valid(self):
        ok, msg = ManifestValidator.validate_permissions([])
        self.assertTrue(ok)


# ─── Invalid AI Capabilities ───────────────────────────────────────

class TestInvalidAICapabilities(unittest.TestCase):

    def test_invalid_type(self):
        ok, errors = ManifestValidator.validate_ai_capabilities(
            [{'type': 'alien_tech'}])
        self.assertFalse(ok)
        self.assertTrue(any('alien_tech' in e for e in errors))

    def test_negative_accuracy(self):
        ok, errors = ManifestValidator.validate_ai_capabilities(
            [{'type': 'llm', 'min_accuracy': -0.5}])
        self.assertFalse(ok)

    def test_nan_value(self):
        ok, errors = ManifestValidator.validate_ai_capabilities(
            [{'type': 'llm', 'max_latency_ms': float('nan')}])
        self.assertFalse(ok)

    def test_inf_value(self):
        ok, errors = ManifestValidator.validate_ai_capabilities(
            [{'type': 'tts', 'max_cost_spark': float('inf')}])
        self.assertFalse(ok)

    def test_accuracy_over_one(self):
        ok, errors = ManifestValidator.validate_ai_capabilities(
            [{'type': 'llm', 'min_accuracy': 1.5}])
        self.assertFalse(ok)

    def test_valid_capabilities(self):
        ok, errors = ManifestValidator.validate_ai_capabilities([
            {'type': 'llm', 'min_accuracy': 0.8},
            {'type': 'tts', 'required': False},
        ])
        self.assertTrue(ok)

    def test_not_a_dict(self):
        ok, errors = ManifestValidator.validate_ai_capabilities(['llm'])
        self.assertFalse(ok)


# ─── Invalid Size ───────────────────────────────────────────────────

class TestInvalidSize(unittest.TestCase):

    def test_zero_width(self):
        ok, msg = ManifestValidator.validate_size((0, 600))
        self.assertFalse(ok)

    def test_negative_height(self):
        ok, msg = ManifestValidator.validate_size((800, -1))
        self.assertFalse(ok)

    def test_too_large(self):
        ok, msg = ManifestValidator.validate_size((10000, 5000))
        self.assertFalse(ok)

    def test_valid_size(self):
        ok, msg = ManifestValidator.validate_size((1920, 1080))
        self.assertTrue(ok)


# ─── AppRegistry Integration ───────────────────────────────────────

class TestAppRegistryIntegration(unittest.TestCase):

    def test_register_rejects_invalid_manifest(self):
        reg = AppRegistry()
        m = AppManifest(id='../evil', name='X', version='1.0.0',
                        type='unknown', entry={})
        with self.assertRaises(ValueError):
            reg.register(m)

    def test_register_accepts_valid_manifest(self):
        reg = AppRegistry()
        m = AppManifest(id='valid-app', name='Valid', version='1.0.0',
                        type='extension', entry={'module': 'ext.valid'})
        reg.register(m)
        self.assertIsNotNone(reg.get('valid-app'))


# ─── HartApp Builder Integration ───────────────────────────────────

class TestHartAppValidation(unittest.TestCase):

    def test_valid_builder(self):
        from hart_sdk.app_builder import HartApp
        app = HartApp('sdk-valid', version='1.0.0', app_type='extension')
        app.entry(module='ext.sdk')
        m = app.manifest()
        self.assertEqual(m.id, 'sdk-valid')

    def test_invalid_builder_raises(self):
        from hart_sdk.app_builder import HartApp
        app = HartApp('../evil', version='bad', app_type='unknown')
        with self.assertRaises(ValueError):
            app.manifest()


# ─── Constants ──────────────────────────────────────────────────────

class TestConstants(unittest.TestCase):

    def test_all_app_types_have_schema(self):
        for t in AppType:
            self.assertIn(t.value, ENTRY_SCHEMA,
                          f"Missing ENTRY_SCHEMA for {t.value}")

    def test_known_permissions_frozenset(self):
        self.assertIsInstance(KNOWN_PERMISSIONS, frozenset)
        self.assertGreater(len(KNOWN_PERMISSIONS), 10)

    def test_valid_types_matches_enum(self):
        enum_values = {t.value for t in AppType}
        self.assertEqual(_VALID_TYPES, enum_values)


if __name__ == '__main__':
    unittest.main()
