"""
Tests for integrations.agent_engine.app_installer — Cross-Platform App Installer.

Covers: platform detection, checksum verification, install/uninstall dispatch,
list_installed, search, history, and all Flask API routes.
"""

import hashlib
import json
import os
import struct
import tempfile
import unittest
import zipfile
from unittest.mock import patch, MagicMock, PropertyMock

from integrations.agent_engine.app_installer import (
    InstallerPlatform, InstallStatus, InstallRequest, InstallResult,
    detect_platform, verify_checksum, AppInstaller, get_installer,
)


def _make_installer_app():
    """Create a Flask test app with app-installer routes."""
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    from integrations.agent_engine.app_installer import register_app_install_routes
    register_app_install_routes(app)
    return app.test_client()


# ═══════════════════════════════════════════════════════════════
# Platform Detection
# ═══════════════════════════════════════════════════════════════

class TestDetectPlatform(unittest.TestCase):
    """Tests for detect_platform()."""

    def test_exe_extension(self):
        self.assertEqual(detect_platform('app.exe'), InstallerPlatform.WINDOWS)

    def test_msi_extension(self):
        self.assertEqual(detect_platform('setup.msi'), InstallerPlatform.WINDOWS)

    def test_apk_extension(self):
        self.assertEqual(detect_platform('game.apk'), InstallerPlatform.ANDROID)

    def test_dmg_extension(self):
        self.assertEqual(detect_platform('app.dmg'), InstallerPlatform.MACOS)

    def test_pkg_extension(self):
        self.assertEqual(detect_platform('installer.pkg'), InstallerPlatform.MACOS)

    def test_flatpakref_extension(self):
        self.assertEqual(detect_platform('app.flatpakref'), InstallerPlatform.FLATPAK)

    def test_appimage_extension_lower(self):
        self.assertEqual(detect_platform('tool.appimage'), InstallerPlatform.APPIMAGE)

    def test_appimage_extension_upper(self):
        self.assertEqual(detect_platform('tool.AppImage'), InstallerPlatform.APPIMAGE)

    def test_hartpkg_extension(self):
        self.assertEqual(detect_platform('mod.hartpkg'), InstallerPlatform.EXTENSION)

    def test_unknown_extension(self):
        self.assertEqual(detect_platform('readme.txt'), InstallerPlatform.UNKNOWN)

    def test_no_extension(self):
        self.assertEqual(detect_platform('binary'), InstallerPlatform.UNKNOWN)

    def test_magic_bytes_mz_pe(self):
        """PE binary (MZ header) detected as Windows."""
        with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
            f.write(b'MZ' + b'\x00' * 100)
            path = f.name
        try:
            self.assertEqual(detect_platform(path), InstallerPlatform.WINDOWS)
        finally:
            os.unlink(path)

    def test_magic_bytes_elf(self):
        """ELF binary detected as AppImage."""
        with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
            f.write(b'\x7fELF' + b'\x00' * 100)
            path = f.name
        try:
            self.assertEqual(detect_platform(path), InstallerPlatform.APPIMAGE)
        finally:
            os.unlink(path)

    def test_magic_bytes_apk_zip(self):
        """ZIP with AndroidManifest.xml detected as Android."""
        with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
            path = f.name
        try:
            with zipfile.ZipFile(path, 'w') as zf:
                zf.writestr('AndroidManifest.xml', '<manifest/>')
            self.assertEqual(detect_platform(path), InstallerPlatform.ANDROID)
        finally:
            os.unlink(path)

    def test_magic_bytes_plain_zip_not_android(self):
        """ZIP without AndroidManifest.xml is NOT detected as Android."""
        with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
            path = f.name
        try:
            with zipfile.ZipFile(path, 'w') as zf:
                zf.writestr('data.txt', 'hello')
            # Plain ZIP → no matching platform → UNKNOWN
            self.assertEqual(detect_platform(path), InstallerPlatform.UNKNOWN)
        finally:
            os.unlink(path)

    def test_unreadable_file(self):
        """IOError during read → UNKNOWN."""
        self.assertEqual(detect_platform('/nonexistent/file.bin'), InstallerPlatform.UNKNOWN)


# ═══════════════════════════════════════════════════════════════
# Checksum Verification
# ═══════════════════════════════════════════════════════════════

class TestVerifyChecksum(unittest.TestCase):
    """Tests for verify_checksum()."""

    def test_empty_expected_passes(self):
        """No expected hash → always True."""
        self.assertTrue(verify_checksum('/any/path', ''))

    def test_correct_checksum(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'test data for checksum')
            path = f.name
        try:
            expected = hashlib.sha256(b'test data for checksum').hexdigest()
            self.assertTrue(verify_checksum(path, expected))
        finally:
            os.unlink(path)

    def test_wrong_checksum(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'test data for checksum')
            path = f.name
        try:
            self.assertFalse(verify_checksum(path, 'badbeef' * 8))
        finally:
            os.unlink(path)


# ═══════════════════════════════════════════════════════════════
# InstallRequest / InstallResult dataclasses
# ═══════════════════════════════════════════════════════════════

class TestDataclasses(unittest.TestCase):
    """Tests for InstallRequest and InstallResult."""

    def test_install_request_defaults(self):
        req = InstallRequest(source='test.exe')
        self.assertEqual(req.platform, InstallerPlatform.UNKNOWN)
        self.assertEqual(req.name, '')
        self.assertEqual(req.sha256, '')
        self.assertIsInstance(req.options, dict)

    def test_install_result_fields(self):
        res = InstallResult(success=True, platform='nix', name='htop')
        self.assertTrue(res.success)
        self.assertEqual(res.platform, 'nix')
        self.assertEqual(res.error, '')

    def test_install_status_enum(self):
        self.assertEqual(InstallStatus.PENDING.value, 'pending')
        self.assertEqual(InstallStatus.COMPLETED.value, 'completed')
        self.assertEqual(InstallStatus.FAILED.value, 'failed')


# ═══════════════════════════════════════════════════════════════
# AppInstaller — Install Dispatch
# ═══════════════════════════════════════════════════════════════

class TestAppInstallerInstall(unittest.TestCase):
    """Tests for AppInstaller.install() dispatch."""

    def setUp(self):
        self.installer = AppInstaller()
        self.installer._install_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.installer._install_dir, ignore_errors=True)

    def test_auto_detect_nix_prefix(self):
        """'nixpkgs.htop' auto-detected as NIX."""
        with patch.object(self.installer, '_install_nix') as mock:
            mock.return_value = InstallResult(success=True, platform='nix', name='htop')
            result = self.installer.install(InstallRequest(source='nixpkgs.htop'))
            mock.assert_called_once()
            self.assertTrue(result.success)

    def test_auto_detect_nix_colon(self):
        """'nix:htop' auto-detected as NIX."""
        with patch.object(self.installer, '_install_nix') as mock:
            mock.return_value = InstallResult(success=True, platform='nix', name='htop')
            self.installer.install(InstallRequest(source='nix:htop'))
            mock.assert_called_once()

    def test_auto_detect_flatpak_prefix(self):
        """'flathub:org.gimp.GIMP' auto-detected as FLATPAK."""
        with patch.object(self.installer, '_install_flatpak') as mock:
            mock.return_value = InstallResult(success=True, platform='flatpak', name='GIMP')
            self.installer.install(InstallRequest(source='flathub:org.gimp.GIMP'))
            mock.assert_called_once()

    def test_auto_detect_file_platform(self):
        """Existing file → detect_platform() called."""
        with tempfile.NamedTemporaryFile(
                suffix='.AppImage', delete=False,
                dir=self.installer._install_dir) as f:
            f.write(b'\x7fELF' + b'\x00' * 100)
            path = f.name
        try:
            with patch.object(self.installer, '_install_appimage') as mock:
                mock.return_value = InstallResult(
                    success=True, platform='appimage', name='tool')
                self.installer.install(InstallRequest(source=path))
                mock.assert_called_once()
        finally:
            os.unlink(path)

    def test_fallback_to_nix(self):
        """Unknown string that's not a file → fallback to NIX."""
        with patch.object(self.installer, '_install_nix') as mock:
            mock.return_value = InstallResult(success=True, platform='nix', name='htop')
            self.installer.install(InstallRequest(source='htop'))
            mock.assert_called_once()

    def test_explicit_platform_overrides_detection(self):
        """Explicit platform= skips auto-detection."""
        with patch.object(self.installer, '_install_flatpak') as mock:
            mock.return_value = InstallResult(success=True, platform='flatpak', name='gimp')
            self.installer.install(InstallRequest(
                source='gimp', platform=InstallerPlatform.FLATPAK))
            mock.assert_called_once()

    def test_checksum_failure_aborts(self):
        """Bad checksum → install never called."""
        with tempfile.NamedTemporaryFile(suffix='.exe', delete=False) as f:
            f.write(b'MZ' + b'\x00' * 100)
            path = f.name
        try:
            result = self.installer.install(InstallRequest(
                source=path, sha256='deadbeef' * 8))
            self.assertFalse(result.success)
            self.assertIn('Checksum', result.error)
        finally:
            os.unlink(path)

    def test_install_records_history(self):
        """Successful install is recorded in history."""
        with patch.object(self.installer, '_install_nix') as mock:
            mock.return_value = InstallResult(success=True, platform='nix', name='htop')
            self.installer.install(InstallRequest(source='nixpkgs.htop'))
            hist = self.installer.history()
            self.assertEqual(len(hist), 1)
            self.assertEqual(hist[0]['name'], 'htop')
            self.assertTrue(hist[0]['success'])

    def test_failed_install_recorded_in_history(self):
        """Failed install is also recorded."""
        with patch.object(self.installer, '_install_nix') as mock:
            mock.return_value = InstallResult(
                success=False, platform='nix', name='bad', error='not found')
            self.installer.install(InstallRequest(source='nixpkgs.bad'))
            hist = self.installer.history()
            self.assertEqual(len(hist), 1)
            self.assertFalse(hist[0]['success'])

    def test_duration_is_set(self):
        """Install result has positive duration."""
        with patch.object(self.installer, '_install_nix') as mock:
            mock.return_value = InstallResult(success=True, platform='nix', name='htop')
            result = self.installer.install(InstallRequest(source='nixpkgs.htop'))
            self.assertGreaterEqual(result.duration_seconds, 0)


# ═══════════════════════════════════════════════════════════════
# AppInstaller — Platform Handlers
# ═══════════════════════════════════════════════════════════════

class TestNixHandler(unittest.TestCase):
    """Tests for _install_nix."""

    def setUp(self):
        self.installer = AppInstaller()

    @patch('subprocess.run')
    def test_nix_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        result = self.installer._install_nix(InstallRequest(source='nixpkgs.htop'))
        self.assertTrue(result.success)
        self.assertEqual(result.platform, 'nix')
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn('nix-env', cmd)

    @patch('subprocess.run')
    def test_nix_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr='package not found')
        result = self.installer._install_nix(InstallRequest(source='nixpkgs.nosuchpkg'))
        self.assertFalse(result.success)
        self.assertIn('not found', result.error)

    @patch('subprocess.run', side_effect=FileNotFoundError)
    def test_nix_not_installed(self, _):
        result = self.installer._install_nix(InstallRequest(source='nixpkgs.htop'))
        self.assertFalse(result.success)
        self.assertIn('not available', result.error)

    @patch('subprocess.run', side_effect=__import__('subprocess').TimeoutExpired('cmd', 300))
    def test_nix_timeout(self, _):
        result = self.installer._install_nix(InstallRequest(source='nixpkgs.big'))
        self.assertFalse(result.success)
        self.assertIn('timed out', result.error)


class TestFlatpakHandler(unittest.TestCase):
    """Tests for _install_flatpak."""

    def setUp(self):
        self.installer = AppInstaller()

    @patch('subprocess.run')
    def test_flatpak_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        result = self.installer._install_flatpak(
            InstallRequest(source='flathub:org.gimp.GIMP'))
        self.assertTrue(result.success)
        self.assertEqual(result.platform, 'flatpak')

    @patch('subprocess.run', side_effect=FileNotFoundError)
    def test_flatpak_not_installed(self, _):
        result = self.installer._install_flatpak(
            InstallRequest(source='flathub:org.gimp.GIMP'))
        self.assertFalse(result.success)
        self.assertIn('not available', result.error)


class TestAppImageHandler(unittest.TestCase):
    """Tests for _install_appimage."""

    def setUp(self):
        self.installer = AppInstaller()
        self.installer._install_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.installer._install_dir, ignore_errors=True)

    def test_appimage_install_success(self):
        with tempfile.NamedTemporaryFile(
                suffix='.AppImage', delete=False) as f:
            f.write(b'\x7fELF' + b'\x00' * 100)
            src = f.name
        try:
            result = self.installer._install_appimage(
                InstallRequest(source=src))
            self.assertTrue(result.success)
            self.assertEqual(result.platform, 'appimage')
            self.assertTrue(os.path.isfile(result.install_path))
        finally:
            os.unlink(src)

    def test_appimage_missing_file(self):
        result = self.installer._install_appimage(
            InstallRequest(source='/no/such/file.AppImage'))
        self.assertFalse(result.success)
        self.assertIn('not found', result.error)


class TestWindowsHandler(unittest.TestCase):
    """Tests for _install_windows."""

    def setUp(self):
        self.installer = AppInstaller()
        self.installer._install_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.installer._install_dir, ignore_errors=True)

    def test_windows_no_wine(self):
        """No Wine binary → error with helpful message."""
        with tempfile.NamedTemporaryFile(suffix='.exe', delete=False) as f:
            f.write(b'MZ' + b'\x00' * 100)
            path = f.name
        try:
            with patch('shutil.which', return_value=None):
                result = self.installer._install_windows(
                    InstallRequest(source=path))
                self.assertFalse(result.success)
                self.assertIn('Wine', result.error)
        finally:
            os.unlink(path)

    @patch('subprocess.run')
    @patch('shutil.which', return_value='/usr/bin/wine64')
    def test_windows_exe_via_wine(self, _which, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        with tempfile.NamedTemporaryFile(suffix='.exe', delete=False) as f:
            f.write(b'MZ' + b'\x00' * 100)
            path = f.name
        try:
            result = self.installer._install_windows(
                InstallRequest(source=path))
            self.assertTrue(result.success)
            self.assertEqual(result.platform, 'windows')
        finally:
            os.unlink(path)

    @patch('subprocess.run')
    @patch('shutil.which', return_value='/usr/bin/wine64')
    def test_windows_msi_via_wine(self, _which, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        with tempfile.NamedTemporaryFile(suffix='.msi', delete=False) as f:
            f.write(b'MZ' + b'\x00' * 100)
            path = f.name
        try:
            result = self.installer._install_windows(
                InstallRequest(source=path))
            self.assertTrue(result.success)
            # Verify msiexec used for .msi
            cmd = mock_run.call_args[0][0]
            self.assertIn('msiexec', cmd)
        finally:
            os.unlink(path)

    def test_windows_missing_file(self):
        result = self.installer._install_windows(
            InstallRequest(source='/no/such/app.exe'))
        self.assertFalse(result.success)
        self.assertIn('not found', result.error)


class TestAndroidHandler(unittest.TestCase):
    """Tests for _install_android."""

    def setUp(self):
        self.installer = AppInstaller()
        self.installer._install_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.installer._install_dir, ignore_errors=True)

    def test_android_no_binder(self):
        """No /dev/binder → error about Android subsystem."""
        with tempfile.NamedTemporaryFile(suffix='.apk', delete=False) as f:
            f.write(b'PK' + b'\x00' * 100)
            path = f.name
        try:
            with patch('os.path.exists', return_value=False):
                result = self.installer._install_android(
                    InstallRequest(source=path))
                self.assertFalse(result.success)
                self.assertIn('Android subsystem', result.error)
        finally:
            os.unlink(path)

    @patch('shutil.which', return_value=None)
    @patch('os.path.exists', side_effect=lambda p: p == '/dev/binder' or os.path.exists.__wrapped__(p) if hasattr(os.path.exists, '__wrapped__') else True)
    def test_android_fallback_copy(self, mock_exists, _which):
        """With binder but no adb → fallback to copy."""
        with tempfile.NamedTemporaryFile(suffix='.apk', delete=False) as f:
            f.write(b'PK' + b'\x00' * 100)
            path = f.name
        try:
            # Patch os.path.exists to return True for /dev/binder, pass-through for others
            def side_effect(p):
                if p == '/dev/binder':
                    return True
                return os.path.isfile(p)

            with patch('integrations.agent_engine.app_installer.os.path.exists', side_effect=side_effect):
                with patch('integrations.agent_engine.app_installer.shutil.which', return_value=None):
                    result = self.installer._install_android(
                        InstallRequest(source=path))
                    self.assertTrue(result.success)
                    self.assertEqual(result.platform, 'android')
        finally:
            os.unlink(path)

    def test_android_missing_file(self):
        result = self.installer._install_android(
            InstallRequest(source='/no/such/game.apk'))
        self.assertFalse(result.success)
        self.assertIn('not found', result.error)


class TestMacOSHandler(unittest.TestCase):
    """Tests for _install_macos."""

    def setUp(self):
        self.installer = AppInstaller()

    @patch('shutil.which', return_value=None)
    def test_macos_no_darling(self, _):
        result = self.installer._install_macos(
            InstallRequest(source='app.dmg'))
        self.assertFalse(result.success)
        self.assertIn('Darling', result.error)

    @patch('shutil.which', return_value='/usr/bin/darling')
    def test_macos_with_darling_not_automated(self, _):
        result = self.installer._install_macos(
            InstallRequest(source='app.dmg'))
        self.assertFalse(result.success)
        self.assertIn('not yet automated', result.error)


class TestExtensionHandler(unittest.TestCase):
    """Tests for _install_extension."""

    def setUp(self):
        self.installer = AppInstaller()

    @patch('integrations.agent_engine.app_installer.get_installer')
    def test_extension_success(self, _):
        mock_ext = MagicMock()
        mock_ext.manifest.id = 'test_ext'
        mock_ext.manifest.version = '1.0.0'
        mock_registry = MagicMock()
        mock_registry.get.return_value = MagicMock(load=MagicMock(return_value=mock_ext))

        with patch('core.platform.registry.get_registry', return_value=mock_registry):
            result = self.installer._install_extension(
                InstallRequest(source='my_extension.hartpkg'))
            self.assertTrue(result.success)
            self.assertEqual(result.app_id, 'test_ext')

    def test_extension_registry_unavailable(self):
        with patch('core.platform.registry.get_registry', side_effect=ImportError):
            result = self.installer._install_extension(
                InstallRequest(source='my_extension.hartpkg'))
            self.assertFalse(result.success)


# ═══════════════════════════════════════════════════════════════
# AppInstaller — Uninstall
# ═══════════════════════════════════════════════════════════════

class TestAppInstallerUninstall(unittest.TestCase):
    """Tests for AppInstaller.uninstall()."""

    def setUp(self):
        self.installer = AppInstaller()
        self.installer._install_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.installer._install_dir, ignore_errors=True)

    @patch('subprocess.run')
    def test_uninstall_nix(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr='')
        result = self.installer.uninstall('htop', 'nix')
        self.assertTrue(result.success)

    @patch('subprocess.run')
    def test_uninstall_flatpak(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr='')
        result = self.installer.uninstall('org.gimp.GIMP', 'flatpak')
        self.assertTrue(result.success)

    def test_uninstall_appimage(self):
        # Create an AppImage file
        appimage_dir = os.path.join(self.installer._install_dir, 'appimages')
        os.makedirs(appimage_dir)
        path = os.path.join(appimage_dir, 'MyApp.AppImage')
        with open(path, 'wb') as f:
            f.write(b'\x7fELF')
        result = self.installer.uninstall('MyApp', 'appimage')
        self.assertTrue(result.success)
        self.assertFalse(os.path.exists(path))

    def test_uninstall_appimage_not_found(self):
        result = self.installer.uninstall('NoSuchApp', 'appimage')
        self.assertFalse(result.success)
        self.assertIn('not found', result.error)

    def test_uninstall_unsupported_platform(self):
        result = self.installer.uninstall('app', 'ios')
        self.assertFalse(result.success)
        self.assertIn('not supported', result.error)

    @patch('subprocess.run')
    def test_uninstall_default_platform_uses_nix(self, mock_run):
        """No platform specified → default to nix."""
        mock_run.return_value = MagicMock(returncode=0, stderr='')
        result = self.installer.uninstall('htop')
        self.assertTrue(result.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn('nix-env', cmd)


# ═══════════════════════════════════════════════════════════════
# AppInstaller — List Installed
# ═══════════════════════════════════════════════════════════════

class TestListInstalled(unittest.TestCase):
    """Tests for AppInstaller.list_installed()."""

    def setUp(self):
        self.installer = AppInstaller()
        self.installer._install_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.installer._install_dir, ignore_errors=True)

    @patch('subprocess.run', side_effect=FileNotFoundError)
    def test_list_graceful_when_no_tools(self, _):
        """No nix-env, no flatpak → empty list (no crash)."""
        installed = self.installer.list_installed()
        self.assertIsInstance(installed, list)

    @patch('subprocess.run')
    def test_list_nix_packages(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({'htop': {'version': '3.2.1'}, 'git': {'version': '2.40'}}))
        installed = self.installer.list_installed()
        nix_apps = [a for a in installed if a['platform'] == 'nix']
        self.assertGreaterEqual(len(nix_apps), 2)

    def test_list_appimages(self):
        """AppImages in install dir are listed."""
        appimage_dir = os.path.join(self.installer._install_dir, 'appimages')
        os.makedirs(appimage_dir)
        with open(os.path.join(appimage_dir, 'VLC.AppImage'), 'wb') as f:
            f.write(b'\x7fELF')
        with patch('subprocess.run', side_effect=FileNotFoundError):
            installed = self.installer.list_installed()
        ai_apps = [a for a in installed if a['platform'] == 'appimage']
        self.assertEqual(len(ai_apps), 1)
        self.assertEqual(ai_apps[0]['name'], 'VLC')

    def test_list_wine_apps(self):
        """Wine .desktop files listed."""
        wine_dir = os.path.join(self.installer._install_dir, 'wine')
        os.makedirs(wine_dir)
        with open(os.path.join(wine_dir, 'Notepad.desktop'), 'w') as f:
            f.write('[Desktop Entry]')
        with patch('subprocess.run', side_effect=FileNotFoundError):
            installed = self.installer.list_installed()
        win_apps = [a for a in installed if a['platform'] == 'windows']
        self.assertEqual(len(win_apps), 1)
        self.assertEqual(win_apps[0]['name'], 'Notepad')


# ═══════════════════════════════════════════════════════════════
# AppInstaller — Search
# ═══════════════════════════════════════════════════════════════

class TestSearch(unittest.TestCase):
    """Tests for AppInstaller.search()."""

    def setUp(self):
        self.installer = AppInstaller()

    @patch('subprocess.run', side_effect=FileNotFoundError)
    def test_search_no_tools(self, _):
        """No package managers → empty results."""
        results = self.installer.search('firefox')
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 0)

    @patch('subprocess.run')
    def test_search_nix(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                'nixpkgs.firefox': {
                    'pname': 'firefox',
                    'version': '120.0',
                    'description': 'Web browser',
                },
            }))
        results = self.installer.search('firefox', ['nix'])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'firefox')
        self.assertEqual(results[0]['platform'], 'nix')

    @patch('subprocess.run')
    def test_search_with_platform_filter(self, mock_run):
        """Only searches specified platforms."""
        mock_run.return_value = MagicMock(returncode=0, stdout='{}')
        self.installer.search('firefox', ['nix'])
        # Only 1 call (nix), not 2 (nix + flatpak)
        self.assertEqual(mock_run.call_count, 1)


# ═══════════════════════════════════════════════════════════════
# AppInstaller — Singleton
# ═══════════════════════════════════════════════════════════════

class TestSingleton(unittest.TestCase):
    """Tests for get_installer() singleton."""

    def test_singleton_returns_same_instance(self):
        import integrations.agent_engine.app_installer as mod
        mod._installer = None
        a = get_installer()
        b = get_installer()
        self.assertIs(a, b)
        mod._installer = None  # clean up


# ═══════════════════════════════════════════════════════════════
# Flask API Routes
# ═══════════════════════════════════════════════════════════════

class TestInstallRoute(unittest.TestCase):
    """Tests for POST /api/shell/apps/install."""

    def test_missing_source(self):
        client = _make_installer_app()
        r = client.post('/api/shell/apps/install',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)
        data = json.loads(r.data)
        self.assertIn('error', data)

    @patch('integrations.agent_engine.app_installer.get_installer')
    def test_install_nix_package(self, mock_get):
        mock_inst = MagicMock()
        mock_inst.install.return_value = InstallResult(
            success=True, platform='nix', name='htop',
            app_id='htop', version='3.2.1', duration_seconds=1.5)
        mock_get.return_value = mock_inst

        client = _make_installer_app()
        r = client.post('/api/shell/apps/install',
                        data=json.dumps({'source': 'nixpkgs.htop', 'platform': 'nix'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['success'])
        self.assertEqual(data['platform'], 'nix')

    @patch('integrations.agent_engine.app_installer.get_installer')
    def test_install_failure_returns_400(self, mock_get):
        mock_inst = MagicMock()
        mock_inst.install.return_value = InstallResult(
            success=False, platform='nix', name='bad',
            error='package not found', duration_seconds=0.5)
        mock_get.return_value = mock_inst

        client = _make_installer_app()
        r = client.post('/api/shell/apps/install',
                        data=json.dumps({'source': 'nixpkgs.bad'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)
        data = json.loads(r.data)
        self.assertFalse(data['success'])


class TestUninstallRoute(unittest.TestCase):
    """Tests for POST /api/shell/apps/uninstall."""

    def test_missing_app_id(self):
        client = _make_installer_app()
        r = client.post('/api/shell/apps/uninstall',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.app_installer.get_installer')
    def test_uninstall_success(self, mock_get):
        mock_inst = MagicMock()
        mock_inst.uninstall.return_value = InstallResult(
            success=True, platform='nix', name='htop')
        mock_get.return_value = mock_inst

        client = _make_installer_app()
        r = client.post('/api/shell/apps/uninstall',
                        data=json.dumps({'app_id': 'htop', 'platform': 'nix'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['success'])


class TestInstalledRoute(unittest.TestCase):
    """Tests for GET /api/shell/apps/installed."""

    @patch('integrations.agent_engine.app_installer.get_installer')
    def test_list_installed(self, mock_get):
        mock_inst = MagicMock()
        mock_inst.list_installed.return_value = [
            {'name': 'htop', 'platform': 'nix', 'version': '3.2'},
        ]
        mock_get.return_value = mock_inst

        client = _make_installer_app()
        r = client.get('/api/shell/apps/installed')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['apps'][0]['name'], 'htop')


class TestSearchRoute(unittest.TestCase):
    """Tests for GET /api/shell/apps/search."""

    def test_missing_query(self):
        client = _make_installer_app()
        r = client.get('/api/shell/apps/search')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.app_installer.get_installer')
    def test_search_success(self, mock_get):
        mock_inst = MagicMock()
        mock_inst.search.return_value = [
            {'name': 'firefox', 'platform': 'nix', 'version': '120.0'},
        ]
        mock_get.return_value = mock_inst

        client = _make_installer_app()
        r = client.get('/api/shell/apps/search?q=firefox')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['query'], 'firefox')

    @patch('integrations.agent_engine.app_installer.get_installer')
    def test_search_with_platform_filter(self, mock_get):
        mock_inst = MagicMock()
        mock_inst.search.return_value = []
        mock_get.return_value = mock_inst

        client = _make_installer_app()
        r = client.get('/api/shell/apps/search?q=test&platforms=nix,flatpak')
        self.assertEqual(r.status_code, 200)
        # Verify platforms passed correctly
        mock_inst.search.assert_called_once_with('test', ['nix', 'flatpak'])


class TestDetectRoute(unittest.TestCase):
    """Tests for POST /api/shell/apps/detect."""

    def test_missing_path(self):
        client = _make_installer_app()
        r = client.post('/api/shell/apps/detect',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_nonexistent_file(self):
        client = _make_installer_app()
        r = client.post('/api/shell/apps/detect',
                        data=json.dumps({'path': '/no/such/file'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_detect_exe_file(self):
        with tempfile.NamedTemporaryFile(suffix='.exe', delete=False) as f:
            f.write(b'MZ' + b'\x00' * 100)
            path = f.name
        try:
            client = _make_installer_app()
            r = client.post('/api/shell/apps/detect',
                            data=json.dumps({'path': path}),
                            content_type='application/json')
            self.assertEqual(r.status_code, 200)
            data = json.loads(r.data)
            self.assertEqual(data['platform'], 'windows')
            self.assertGreater(data['size'], 0)
        finally:
            os.unlink(path)


class TestHistoryRoute(unittest.TestCase):
    """Tests for GET /api/shell/apps/history."""

    @patch('integrations.agent_engine.app_installer.get_installer')
    def test_history(self, mock_get):
        mock_inst = MagicMock()
        mock_inst.history.return_value = [
            {'name': 'htop', 'success': True, 'platform': 'nix'},
        ]
        mock_get.return_value = mock_inst

        client = _make_installer_app()
        r = client.get('/api/shell/apps/history')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['count'], 1)


class TestPlatformsRoute(unittest.TestCase):
    """Tests for GET /api/shell/apps/platforms."""

    def test_platforms_list(self):
        client = _make_installer_app()
        r = client.get('/api/shell/apps/platforms')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        platforms = data['platforms']
        # Should have 7 platforms (all except UNKNOWN)
        self.assertEqual(len(platforms), 7)
        names = [p['platform'] for p in platforms]
        self.assertIn('nix', names)
        self.assertIn('flatpak', names)
        self.assertIn('appimage', names)
        self.assertIn('windows', names)
        self.assertIn('android', names)
        self.assertIn('macos', names)
        self.assertIn('extension', names)

    def test_appimage_always_available(self):
        client = _make_installer_app()
        r = client.get('/api/shell/apps/platforms')
        data = json.loads(r.data)
        appimage = [p for p in data['platforms'] if p['platform'] == 'appimage'][0]
        self.assertTrue(appimage['available'])

    def test_extension_always_available(self):
        client = _make_installer_app()
        r = client.get('/api/shell/apps/platforms')
        data = json.loads(r.data)
        ext = [p for p in data['platforms'] if p['platform'] == 'extension'][0]
        self.assertTrue(ext['available'])


if __name__ == '__main__':
    unittest.main()
