"""Tests for core.platform.extension_sandbox — AST-based extension sandboxing."""

import hashlib
import os
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch, Mock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.platform.extension_sandbox import ExtensionSandbox


# ── TestCleanCode ─────────────────────────────────────────────────


class TestCleanCode(unittest.TestCase):
    """Safe code passes the sandbox."""

    def test_simple_extension_passes(self):
        source = textwrap.dedent("""\
            import json
            class MyExtension:
                def on_load(self):
                    data = json.loads('{}')
                    return data
            def helper():
                return 42
        """)
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertTrue(safe)
        self.assertEqual(violations, [])

    def test_math_stdlib_passes(self):
        source = textwrap.dedent("""\
            import math
            import json
            x = math.sqrt(4)
            y = json.dumps({'a': 1})
        """)
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertTrue(safe)
        self.assertEqual(violations, [])

    def test_empty_source_passes(self):
        safe, violations = ExtensionSandbox.analyze_source('')
        self.assertTrue(safe)
        self.assertEqual(violations, [])


# ── TestBlockedCalls ──────────────────────────────────────────────


class TestBlockedCalls(unittest.TestCase):
    """Blocked built-in calls are caught."""

    def test_eval_blocked(self):
        source = "result = eval('1+1')\n"
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertFalse(safe)
        self.assertTrue(any('eval' in v for v in violations))

    def test_exec_blocked(self):
        source = "exec('print(1)')\n"
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertFalse(safe)
        self.assertTrue(any('exec' in v for v in violations))

    def test_import_builtin_blocked(self):
        source = "__import__('os')\n"
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertFalse(safe)
        self.assertTrue(any('__import__' in v for v in violations))


# ── TestBlockedImports ────────────────────────────────────────────


class TestBlockedImports(unittest.TestCase):
    """Blocked module imports are caught."""

    def test_import_subprocess(self):
        source = "import subprocess\n"
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertFalse(safe)
        self.assertTrue(any('subprocess' in v for v in violations))

    def test_from_subprocess(self):
        source = "from subprocess import run\n"
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertFalse(safe)
        self.assertTrue(any('subprocess' in v for v in violations))

    def test_import_ctypes(self):
        source = "import ctypes\n"
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertFalse(safe)
        self.assertTrue(any('ctypes' in v for v in violations))

    def test_from_os_system(self):
        source = "from os import system\n"
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertFalse(safe)
        self.assertTrue(any('os.system' in v for v in violations))


# ── TestBlockedAttributes ─────────────────────────────────────────


class TestBlockedAttributes(unittest.TestCase):
    """Blocked attribute access patterns are caught."""

    def test_os_system_call(self):
        source = textwrap.dedent("""\
            import os
            os.system('ls')
        """)
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertFalse(safe)
        self.assertTrue(any('os.system' in v for v in violations))

    def test_subprocess_run(self):
        source = textwrap.dedent("""\
            import subprocess
            subprocess.run(['ls'])
        """)
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertFalse(safe)
        self.assertTrue(any('subprocess' in v for v in violations))

    def test_shutil_rmtree(self):
        source = textwrap.dedent("""\
            import shutil
            shutil.rmtree('/tmp')
        """)
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertFalse(safe)
        self.assertTrue(any('shutil.rmtree' in v for v in violations))


# ── TestNestedBlocking ────────────────────────────────────────────


class TestNestedBlocking(unittest.TestCase):
    """Blocked patterns inside functions/classes/lambdas are still caught."""

    def test_blocked_inside_function(self):
        source = textwrap.dedent("""\
            def sneaky():
                return eval('1+1')
        """)
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertFalse(safe)
        self.assertTrue(any('eval' in v for v in violations))

    def test_blocked_inside_class(self):
        source = textwrap.dedent("""\
            import subprocess
            class Bad:
                def run(self):
                    subprocess.run(['ls'])
        """)
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertFalse(safe)
        self.assertTrue(any('subprocess' in v for v in violations))

    def test_blocked_in_lambda(self):
        source = "f = lambda: eval('1')\n"
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertFalse(safe)
        self.assertTrue(any('eval' in v for v in violations))


# ── TestEdgeCases ─────────────────────────────────────────────────


class TestEdgeCases(unittest.TestCase):
    """Edge cases: syntax errors, missing files, binary files."""

    def test_syntax_error(self):
        source = "def foo(:\n"
        safe, violations = ExtensionSandbox.analyze_source(source)
        self.assertFalse(safe)
        self.assertTrue(any('SyntaxError' in v for v in violations))

    def test_file_not_found(self):
        safe, violations = ExtensionSandbox.analyze_file('/nonexistent/path/ext.py')
        self.assertFalse(safe)
        self.assertTrue(any('FileNotFoundError' in v for v in violations))

    def test_binary_file(self):
        with tempfile.NamedTemporaryFile(suffix='.py', delete=False) as f:
            f.write(b'\x80\x81\x82\x83\xff\xfe\xfd')
            tmp_path = f.name
        try:
            safe, violations = ExtensionSandbox.analyze_file(tmp_path)
            self.assertFalse(safe)
            self.assertTrue(len(violations) > 0)
        finally:
            os.unlink(tmp_path)


# ── TestSourceHash ────────────────────────────────────────────────


class TestSourceHash(unittest.TestCase):
    """Source hash computation."""

    def test_compute_hash(self):
        source = "print('hello')"
        expected = hashlib.sha256(source.encode('utf-8')).hexdigest()
        result = ExtensionSandbox.compute_source_hash(source)
        self.assertEqual(result, expected)
        self.assertEqual(len(result), 64)

    def test_different_source_different_hash(self):
        h1 = ExtensionSandbox.compute_source_hash("alpha")
        h2 = ExtensionSandbox.compute_source_hash("beta")
        self.assertNotEqual(h1, h2)


# ── TestPermissionExtraction ──────────────────────────────────────


class TestPermissionExtraction(unittest.TestCase):
    """EXTENSION_PERMISSIONS extraction from source."""

    def test_finds_permissions(self):
        source = textwrap.dedent("""\
            EXTENSION_PERMISSIONS = ['events.theme.*', 'config.read']
            class MyExt:
                pass
        """)
        perms = ExtensionSandbox.check_permission_declarations(source)
        self.assertEqual(perms, ['events.theme.*', 'config.read'])

    def test_no_permissions(self):
        source = textwrap.dedent("""\
            class MyExt:
                pass
        """)
        perms = ExtensionSandbox.check_permission_declarations(source)
        self.assertEqual(perms, [])


# ── TestSignatureVerification ─────────────────────────────────────


class TestSignatureVerification(unittest.TestCase):
    """Ed25519 signature verification (mocked master key)."""

    def test_valid_signature_passes(self):
        with tempfile.NamedTemporaryFile(suffix='.py', delete=False, mode='w') as f:
            f.write("print('hello')")
            tmp_path = f.name
        try:
            content = open(tmp_path, 'rb').read()
            content_hash = hashlib.sha256(content).digest()

            mock_pub = Mock()
            mock_pub.verify = Mock(return_value=None)  # Ed25519 verify returns None on success

            with patch('core.platform.extension_sandbox.ExtensionSandbox.verify_signature') as mock_verify:
                # Direct test: mock the master key function
                pass

            # Test through the real path with mocked master key
            with patch('security.master_key.get_master_public_key', return_value=mock_pub):
                result = ExtensionSandbox.verify_signature(tmp_path, 'aa' * 64)
                self.assertTrue(result)
                mock_pub.verify.assert_called_once()
        finally:
            os.unlink(tmp_path)

    def test_invalid_signature_fails(self):
        with tempfile.NamedTemporaryFile(suffix='.py', delete=False, mode='w') as f:
            f.write("print('hello')")
            tmp_path = f.name
        try:
            mock_pub = Mock()
            mock_pub.verify = Mock(side_effect=Exception("Invalid signature"))

            with patch('security.master_key.get_master_public_key', return_value=mock_pub):
                result = ExtensionSandbox.verify_signature(tmp_path, 'bb' * 64)
                self.assertFalse(result)
        finally:
            os.unlink(tmp_path)

    def test_missing_key_returns_false(self):
        with tempfile.NamedTemporaryFile(suffix='.py', delete=False, mode='w') as f:
            f.write("print('hello')")
            tmp_path = f.name
        try:
            with patch('security.master_key.get_master_public_key',
                       side_effect=Exception("No key")):
                result = ExtensionSandbox.verify_signature(tmp_path, 'cc' * 64)
                self.assertFalse(result)
        finally:
            os.unlink(tmp_path)


# ── TestExtensionRegistryIntegration ──────────────────────────────


class TestExtensionRegistryIntegration(unittest.TestCase):
    """Integration tests: ExtensionRegistry uses sandbox before import."""

    def test_blocked_module_not_loaded(self):
        """A module with subprocess should be blocked at load time."""
        from core.platform.extensions import ExtensionRegistry
        from core.platform.app_manifest import AppManifest

        # Create a temp file with subprocess usage
        with tempfile.NamedTemporaryFile(
            suffix='.py', delete=False, mode='w', dir=os.getcwd()
        ) as f:
            f.write(textwrap.dedent("""\
                import subprocess
                from core.platform.extensions import Extension
                from core.platform.app_manifest import AppManifest

                class BadExtension(Extension):
                    @property
                    def manifest(self):
                        return AppManifest(id='bad_ext', name='Bad', version='1.0')
            """))
            tmp_path = f.name
            module_name = os.path.basename(tmp_path)[:-3]

        try:
            registry = ExtensionRegistry()
            # Mock find_spec to return our temp file
            mock_spec = Mock()
            mock_spec.origin = tmp_path
            with patch('importlib.util.find_spec', return_value=mock_spec):
                with self.assertRaises(ImportError) as ctx:
                    registry.load(module_name)
                self.assertIn('blocked by sandbox', str(ctx.exception))
        finally:
            os.unlink(tmp_path)

    def test_clean_module_loads(self):
        """A clean module should pass sandbox and proceed to import."""
        from core.platform.extensions import ExtensionRegistry

        # Create a temp clean extension
        with tempfile.NamedTemporaryFile(
            suffix='.py', delete=False, mode='w', dir=os.getcwd()
        ) as f:
            f.write(textwrap.dedent("""\
                import json
                from core.platform.extensions import Extension
                from core.platform.app_manifest import AppManifest

                class CleanExtension(Extension):
                    @property
                    def manifest(self):
                        return AppManifest(id='clean_ext', name='Clean', version='1.0')
            """))
            tmp_path = f.name
            module_name = os.path.basename(tmp_path)[:-3]

        try:
            registry = ExtensionRegistry()
            mock_spec = Mock()
            mock_spec.origin = tmp_path

            # The sandbox should pass, then importlib.import_module is called.
            # We mock the actual import to avoid side effects.
            mock_ext_cls = Mock()
            mock_ext_instance = Mock()
            mock_ext_instance.manifest = Mock()
            mock_ext_instance.manifest.id = 'clean_ext'
            mock_ext_instance.manifest.name = 'Clean'
            mock_ext_instance._state = None
            mock_ext_instance._loaded_at = None
            mock_ext_cls.return_value = mock_ext_instance

            mock_module = MagicMock()
            # Make dir(mock_module) return our class name
            mock_module.__dir__ = lambda self: ['CleanExtension']
            mock_module.CleanExtension = mock_ext_cls

            # Ensure CleanExtension passes issubclass check
            from core.platform.extensions import Extension as ExtBase

            with patch('importlib.util.find_spec', return_value=mock_spec):
                with patch('importlib.import_module', return_value=mock_module):
                    with patch('core.platform.extensions.Extension', ExtBase):
                        # Since our mock class won't pass issubclass(attr, Extension),
                        # we verify the sandbox passes by checking no ImportError
                        # from sandbox (import error from no Extension subclass is OK)
                        try:
                            registry.load(module_name)
                        except (TypeError, AttributeError):
                            # Expected: mock module doesn't have a real Extension subclass
                            # The key is that it did NOT raise ImportError from sandbox
                            pass
                        except ImportError as e:
                            if 'blocked by sandbox' in str(e):
                                self.fail("Clean module was blocked by sandbox")
                            # Other import errors are fine (mock module issues)
        finally:
            os.unlink(tmp_path)


if __name__ == '__main__':
    unittest.main()
