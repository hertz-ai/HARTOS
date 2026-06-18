"""
Per-type HONESTY contract for the cross-platform AppInstaller.

Goal: for EVERY install type the unified installer dispatches to, prove
behaviourally — by mocking the subprocess / filesystem boundary, calling the
REAL handler, and asserting observable behaviour — that it honours the actual
runtime result and NEVER claims success without a positive confirmation.

Contract enforced here (matches the design contract + the just-landed Wine fix):

  REAL  : constructs + invokes the correct install command with the right
          package/app argument AND propagates a genuine failure (boundary fails
          -> handler reports failure, not success) AND requires an affirmative
          post-install probe for success (waydroid app list / darling prefix
          boot / managed-policy id on disk).
  STAGED: a copy-to-disk with NO runtime is success=False + staged=True — a
          staged file is NOT installed/launchable.
  HONEST-UNSUPPORTED: returns success=False with an actionable message instead
          of faking success or silently misrouting (snap, .dmg/.pkg).

Every test imports the real handler, mocks the boundary, calls the real method,
and asserts the returned InstallResult — NO grep / source-shape assertions.

Run directly (pytest OOMs on the dev box):
    C:/Users/sathi/miniconda3/python.exe tests/unit/test_app_installer_per_type.py
"""

import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from integrations.agent_engine.app_installer import (
    InstallerPlatform, InstallRequest, InstallResult, AppInstaller,
    detect_platform,
)


def _tmpfile(suffix, magic=b'\x00'):
    """Create a real temp file (handlers early-return 'File not found'
    otherwise, masking the boundary behaviour under test)."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(magic + b'\x00' * 64)
        return f.name


class _InstallerCase(unittest.TestCase):
    def setUp(self):
        self.installer = AppInstaller()
        self.installer._install_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.installer._install_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# REAL — nix: invokes nix-env with the package, PROPAGATES failure
# ═══════════════════════════════════════════════════════════════════════════

class TestNixIsReal(_InstallerCase):
    """nix is REAL: real command, real exit-code check."""

    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_invokes_nix_env_with_package(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr='')
        res = self.installer._install_nix(InstallRequest(source='nixpkgs.htop'))
        self.assertTrue(res.success)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], 'nix-env')
        self.assertIn('-iA', cmd)
        self.assertIn('nixpkgs.htop', cmd)

    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_propagates_failure(self, mock_run):
        """Boundary FAILS (returncode=1) -> handler must report failure."""
        mock_run.return_value = MagicMock(returncode=1, stderr='attribute missing')
        res = self.installer._install_nix(InstallRequest(source='nixpkgs.nope'))
        self.assertFalse(res.success)
        self.assertIn('attribute missing', res.error)

    @patch('integrations.agent_engine.app_installer.subprocess.run',
           side_effect=FileNotFoundError)
    def test_reports_tool_absent(self, _):
        res = self.installer._install_nix(InstallRequest(source='nixpkgs.htop'))
        self.assertFalse(res.success)
        self.assertIn('not available', res.error)


# ═══════════════════════════════════════════════════════════════════════════
# REAL — flatpak: invokes flatpak install, PROPAGATES failure
# ═══════════════════════════════════════════════════════════════════════════

class TestFlatpakIsReal(_InstallerCase):
    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_invokes_flatpak_install_with_ref(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr='')
        res = self.installer._install_flatpak(
            InstallRequest(source='flathub:org.gimp.GIMP'))
        self.assertTrue(res.success)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], 'flatpak')
        self.assertIn('install', cmd)
        self.assertIn('org.gimp.GIMP', cmd)

    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_propagates_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr='no remote ref found')
        res = self.installer._install_flatpak(
            InstallRequest(source='flathub:no.such.App'))
        self.assertFalse(res.success)
        self.assertIn('no remote ref', res.error)


# ═══════════════════════════════════════════════════════════════════════════
# REAL — appimage: copies + chmods a real file, fails on a real IOError
# ═══════════════════════════════════════════════════════════════════════════

class TestAppImageIsReal(_InstallerCase):
    def test_copies_file_into_install_dir(self):
        src = _tmpfile('.AppImage', magic=b'\x7fELF')
        try:
            res = self.installer._install_appimage(InstallRequest(source=src))
            self.assertTrue(res.success)
            self.assertTrue(os.path.isfile(res.install_path))
            self.assertTrue(res.install_path.endswith(os.path.basename(src)))
        finally:
            os.unlink(src)

    def test_propagates_copy_failure(self):
        src = _tmpfile('.AppImage', magic=b'\x7fELF')
        try:
            with patch('integrations.agent_engine.app_installer.shutil.copy2',
                       side_effect=IOError('disk full')):
                res = self.installer._install_appimage(InstallRequest(source=src))
                self.assertFalse(res.success)
                self.assertIn('disk full', res.error)
        finally:
            os.unlink(src)

    def test_missing_file_fails(self):
        res = self.installer._install_appimage(
            InstallRequest(source='/no/such/x.AppImage'))
        self.assertFalse(res.success)
        self.assertIn('not found', res.error)


# ═══════════════════════════════════════════════════════════════════════════
# REAL — Wine: HONOURS the failure signal (non-zero exit -> failure)
# ═══════════════════════════════════════════════════════════════════════════

class TestWindowsWineFailurePropagation(_InstallerCase):
    """Wine HONOURS the failure signal: a non-zero exit -> success=False with
    the error surfaced, while rc=0 stays a best-effort success."""

    @patch('integrations.agent_engine.app_installer.shutil.which',
           return_value='/usr/bin/wine64')
    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_propagates_failure_when_wine_exits_nonzero(self, mock_run, _which):
        mock_run.return_value = MagicMock(
            returncode=1, stderr='wine: cannot find MZ', stdout='')
        src = _tmpfile('.exe', magic=b'MZ')
        try:
            res = self.installer._install_windows(InstallRequest(source=src))
            self.assertFalse(res.success)
            self.assertIn('exited 1', res.error)
            self.assertEqual(res.platform, 'windows')
        finally:
            os.unlink(src)

    @patch('integrations.agent_engine.app_installer.shutil.which',
           return_value='/usr/bin/wine64')
    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_does_invoke_wine_with_the_exe(self, mock_run, _which):
        mock_run.return_value = MagicMock(returncode=0, stderr='', stdout='')
        src = _tmpfile('.exe', magic=b'MZ')
        try:
            self.installer._install_windows(InstallRequest(source=src))
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[0], '/usr/bin/wine64')
            self.assertIn(src, cmd)
        finally:
            os.unlink(src)

    @patch('integrations.agent_engine.app_installer.shutil.which',
           return_value=None)
    def test_reports_wine_absent(self, _which):
        src = _tmpfile('.exe', magic=b'MZ')
        try:
            res = self.installer._install_windows(InstallRequest(source=src))
            self.assertFalse(res.success)
            self.assertIn('Wine', res.error)
        finally:
            os.unlink(src)


# ═══════════════════════════════════════════════════════════════════════════
# REAL — Android via Waydroid: confirmed by `waydroid app list`, never a copy
# ═══════════════════════════════════════════════════════════════════════════

class TestAndroidWaydroidIsReal(_InstallerCase):
    """Android is REAL via Waydroid:
      * requires `waydroid` on PATH + a live RUNNING session;
      * runs `waydroid app install <apk>`;
      * CONFIRMS by `waydroid app list` containing the parsed package id —
        exit-0 alone is insufficient;
      * with NO waydroid/session, the APK is STAGED (success=False, staged=True),
        NEVER reported as installed (the old fake-copy-success is gone).
    """

    def _patch_pkg(self, pkg='com.foo.bar'):
        return patch.object(self.installer, '_apk_package_name',
                            return_value=pkg)

    def test_install_confirmed_by_app_list(self):
        """waydroid present + session live + install rc=0 + app list shows the
        package -> success=True with the package id as app_id."""
        src = _tmpfile('.apk', magic=b'PK')

        def fake_run(cmd, **kw):
            if cmd[:3] == ['/usr/bin/waydroid', 'app', 'install']:
                return MagicMock(returncode=0, stdout='', stderr='')
            if cmd[:3] == ['/usr/bin/waydroid', 'app', 'list']:
                return MagicMock(
                    returncode=0, stdout='Name: Foo\npackageName: com.foo.bar\n',
                    stderr='')
            return MagicMock(returncode=0, stdout='', stderr='')

        try:
            with self._patch_pkg('com.foo.bar'), \
                 patch('integrations.agent_engine.app_installer.shutil.which',
                       return_value='/usr/bin/waydroid'), \
                 patch.object(self.installer, '_waydroid_session_live',
                              return_value=True), \
                 patch('integrations.agent_engine.app_installer.subprocess.run',
                       side_effect=fake_run):
                res = self.installer._install_android(InstallRequest(source=src))
            self.assertTrue(res.success)
            self.assertEqual(res.app_id, 'com.foo.bar')
            self.assertFalse(res.staged)
        finally:
            os.unlink(src)

    def test_exit_zero_but_not_in_app_list_is_failure(self):
        """The contract's teeth: `waydroid app install` returns 0 but the
        package is NOT in `waydroid app list` -> success=False (exit-0 is not
        proof; some builds print errors on stderr while returning 0)."""
        src = _tmpfile('.apk', magic=b'PK')

        def fake_run(cmd, **kw):
            if cmd[:3] == ['/usr/bin/waydroid', 'app', 'install']:
                return MagicMock(returncode=0, stdout='',
                                 stderr='E: something went wrong')
            if cmd[:3] == ['/usr/bin/waydroid', 'app', 'list']:
                return MagicMock(returncode=0, stdout='(no apps)\n', stderr='')
            return MagicMock(returncode=0, stdout='', stderr='')

        try:
            with self._patch_pkg('com.foo.bar'), \
                 patch('integrations.agent_engine.app_installer.shutil.which',
                       return_value='/usr/bin/waydroid'), \
                 patch.object(self.installer, '_waydroid_session_live',
                              return_value=True), \
                 patch('integrations.agent_engine.app_installer.subprocess.run',
                       side_effect=fake_run):
                res = self.installer._install_android(InstallRequest(source=src))
            self.assertFalse(res.success)
            self.assertIn('did not register', res.error)
        finally:
            os.unlink(src)

    def test_no_waydroid_stages_but_does_not_claim_install(self):
        """No waydroid on PATH -> the APK is copied to disk for later BUT the
        result is success=False + staged=True. A staged file is NOT installed.
        Critically: no install command runs (the old copy-that-claimed-success
        is gone)."""
        src = _tmpfile('.apk', magic=b'PK')
        try:
            with self._patch_pkg('com.foo.bar'), \
                 patch('integrations.agent_engine.app_installer.shutil.which',
                       return_value=None), \
                 patch('integrations.agent_engine.app_installer.subprocess.run',
                       side_effect=AssertionError(
                           'no install command should run without waydroid')):
                res = self.installer._install_android(InstallRequest(source=src))
            self.assertFalse(res.success)        # NOT installed
            self.assertTrue(res.staged)          # but staged to disk
            self.assertTrue(os.path.isfile(res.install_path))
            self.assertIn('hart.subsystems.android', res.error)
        finally:
            os.unlink(src)

    def test_waydroid_present_but_no_session_stages(self):
        """waydroid on PATH but NO live session -> staged, not installed, and
        no install command attempted."""
        src = _tmpfile('.apk', magic=b'PK')
        try:
            with self._patch_pkg('com.foo.bar'), \
                 patch('integrations.agent_engine.app_installer.shutil.which',
                       return_value='/usr/bin/waydroid'), \
                 patch.object(self.installer, '_waydroid_session_live',
                              return_value=False), \
                 patch('integrations.agent_engine.app_installer.subprocess.run',
                       side_effect=AssertionError(
                           'no install command should run without a session')):
                res = self.installer._install_android(InstallRequest(source=src))
            self.assertFalse(res.success)
            self.assertTrue(res.staged)
        finally:
            os.unlink(src)

    def test_missing_file_fails(self):
        res = self.installer._install_android(
            InstallRequest(source='/no/such/app.apk'))
        self.assertFalse(res.success)
        self.assertIn('not found', res.error.lower())


# ═══════════════════════════════════════════════════════════════════════════
# REAL (narrow) — macOS via Darling: runs CLI, refuses .dmg/.pkg + GUI honestly
# ═══════════════════════════════════════════════════════════════════════════

class TestMacOSDarling(_InstallerCase):
    """macOS is a narrow REAL surface via Darling:
      * no darling -> honest absent failure;
      * .dmg/.pkg -> honest 'no headless path' failure (never faked);
      * .app whose prefix boots + binary runs rc=0 -> success;
      * a non-zero darling exit -> failure (GUI mostly broken, surfaced).
    """

    @patch('integrations.agent_engine.app_installer.shutil.which',
           return_value=None)
    def test_without_darling_reports_absent(self, _which):
        res = self.installer._install_macos(InstallRequest(source='app.app'))
        self.assertFalse(res.success)
        self.assertIn('Darling', res.error)

    @patch('integrations.agent_engine.app_installer.subprocess.run',
           side_effect=AssertionError('dmg/pkg must not run any subprocess'))
    @patch('integrations.agent_engine.app_installer.shutil.which',
           return_value='/usr/bin/darling')
    def test_dmg_refused_honestly(self, _which, _run):
        res = self.installer._install_macos(InstallRequest(source='thing.dmg'))
        self.assertFalse(res.success)
        self.assertIn('headless', res.error)

    @patch('integrations.agent_engine.app_installer.shutil.which',
           return_value='/usr/bin/darling')
    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_app_bundle_runs_via_darling_shell(self, mock_run, _which):
        """A .app bundle whose prefix boots + binary exits 0 -> success, and
        the boundary actually invoked `darling shell <binary>`."""
        bundle = tempfile.mkdtemp(suffix='.app')
        macos_dir = os.path.join(bundle, 'Contents', 'MacOS')
        os.makedirs(macos_dir)
        binary = os.path.join(macos_dir, 'MyApp')
        with open(binary, 'wb') as f:
            f.write(b'\xcf\xfa\xed\xfe')  # Mach-O magic

        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
        try:
            res = self.installer._install_macos(InstallRequest(source=bundle))
            self.assertTrue(res.success)
            # The last subprocess call launched the bundle binary via darling.
            last_cmd = mock_run.call_args[0][0]
            self.assertEqual(last_cmd[0], '/usr/bin/darling')
            self.assertEqual(last_cmd[1], 'shell')
            self.assertEqual(last_cmd[2], binary)
        finally:
            import shutil
            shutil.rmtree(bundle, ignore_errors=True)

    @patch('integrations.agent_engine.app_installer.shutil.which',
           return_value='/usr/bin/darling')
    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_nonzero_darling_exit_is_failure(self, mock_run, _which):
        """Prefix boots (rc=0) but the app run exits non-zero -> failure."""
        bundle = tempfile.mkdtemp(suffix='.app')
        macos_dir = os.path.join(bundle, 'Contents', 'MacOS')
        os.makedirs(macos_dir)
        binary = os.path.join(macos_dir, 'MyApp')
        with open(binary, 'wb') as f:
            f.write(b'\xcf\xfa\xed\xfe')

        def fake_run(cmd, **kw):
            if cmd[:3] == ['/usr/bin/darling', 'shell', 'true']:
                return MagicMock(returncode=0, stdout='', stderr='')
            return MagicMock(returncode=5, stdout='',
                             stderr='dyld: Symbol not found')

        mock_run.side_effect = fake_run
        try:
            res = self.installer._install_macos(InstallRequest(source=bundle))
            self.assertFalse(res.success)
            self.assertIn('exited 5', res.error)
        finally:
            import shutil
            shutil.rmtree(bundle, ignore_errors=True)

    @patch('integrations.agent_engine.app_installer.shutil.which',
           return_value='/usr/bin/darling')
    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_prefix_boot_failure_is_failure(self, mock_run, _which):
        """`darling shell true` itself fails -> we never trust anything else."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout='', stderr='cannot init prefix')
        res = self.installer._install_macos(InstallRequest(source='cli-binary'))
        self.assertFalse(res.success)
        self.assertIn('boot', res.error)


# ═══════════════════════════════════════════════════════════════════════════
# HONEST-UNSUPPORTED — Snap: real enum, real handler, honest refusal, no misroute
# ═══════════════════════════════════════════════════════════════════════════

class TestSnapHonestlyUnsupported(_InstallerCase):
    """Snap now has a REAL enum + handler that refuses honestly, and detection
    routes .snap / 'snap:' to SNAP — NOT silently to the nix catch-all."""

    def test_snap_enum_member_exists(self):
        self.assertTrue(any(p.value == 'snap' for p in InstallerPlatform))

    def test_handler_refuses_honestly(self):
        res = self.installer._install_snap(InstallRequest(source='snap:firefox'))
        self.assertFalse(res.success)
        self.assertIn('not supported', res.error.lower())
        self.assertEqual(res.platform, 'snap')

    def test_snap_prefix_routes_to_snap_not_nix(self):
        """install('snap:firefox') reaches _install_snap, NOT _install_nix."""
        with patch.object(self.installer, '_install_nix',
                          side_effect=AssertionError('must not route to nix')):
            res = self.installer.install(InstallRequest(source='snap:firefox'))
        self.assertEqual(res.platform, 'snap')
        self.assertFalse(res.success)

    def test_dot_snap_file_detects_as_snap(self):
        src = _tmpfile('.snap', magic=b'\x00')
        try:
            self.assertEqual(detect_platform(src), InstallerPlatform.SNAP)
        finally:
            os.unlink(src)


# ═══════════════════════════════════════════════════════════════════════════
# REAL — Browser extension (.crx/.xpi) via managed policy, verified on disk
# ═══════════════════════════════════════════════════════════════════════════

class TestBrowserExtensionIsReal(_InstallerCase):
    """A real browser-extension install: write the id into the on-disk managed
    policy and CONFIRM by reading it back. Distinct from .hartpkg EXTENSION."""

    def test_crx_writes_and_verifies_chromium_forcelist(self):
        src = _tmpfile('.crx', magic=b'Cr24')
        policy_dir = tempfile.mkdtemp()
        try:
            with patch('integrations.agent_engine.app_installer.shutil.which',
                       side_effect=lambda b: '/usr/bin/chromium'
                       if b == 'chromium' else None):
                res = self.installer._install_browser_ext(InstallRequest(
                    source=src,
                    options={'id': 'abcdefghijklmnop', 'policy_dir': policy_dir}))
            self.assertTrue(res.success)
            self.assertEqual(res.app_id, 'abcdefghijklmnop')
            # REAL proof: the id is present in the on-disk managed policy file.
            import json
            with open(res.install_path) as f:
                policy = json.load(f)
            ids = [e.split(';')[0]
                   for e in policy['ExtensionInstallForcelist']]
            self.assertIn('abcdefghijklmnop', ids)
        finally:
            os.unlink(src)
            import shutil
            shutil.rmtree(policy_dir, ignore_errors=True)

    def test_xpi_writes_and_verifies_firefox_settings(self):
        src = _tmpfile('.xpi', magic=b'PK')
        policy_dir = tempfile.mkdtemp()
        try:
            with patch('integrations.agent_engine.app_installer.shutil.which',
                       side_effect=lambda b: '/usr/bin/firefox'
                       if b == 'firefox' else None):
                res = self.installer._install_browser_ext(InstallRequest(
                    source=src,
                    options={'id': 'ext@example.com', 'policy_dir': policy_dir}))
            self.assertTrue(res.success)
            import json
            with open(res.install_path) as f:
                doc = json.load(f)
            self.assertIn('ext@example.com',
                          doc['policies']['ExtensionSettings'])
            self.assertEqual(
                doc['policies']['ExtensionSettings']['ext@example.com'][
                    'installation_mode'], 'force_installed')
        finally:
            os.unlink(src)
            import shutil
            shutil.rmtree(policy_dir, ignore_errors=True)

    def test_crx_no_chromium_fails(self):
        src = _tmpfile('.crx', magic=b'Cr24')
        try:
            with patch('integrations.agent_engine.app_installer.shutil.which',
                       return_value=None):
                res = self.installer._install_browser_ext(
                    InstallRequest(source=src))
            self.assertFalse(res.success)
            self.assertIn('Chromium', res.error)
        finally:
            os.unlink(src)

    def test_crx_write_failure_propagates(self):
        """A real write boundary failure -> success=False (no fake success)."""
        src = _tmpfile('.crx', magic=b'Cr24')
        policy_dir = tempfile.mkdtemp()
        try:
            with patch('integrations.agent_engine.app_installer.shutil.which',
                       side_effect=lambda b: '/usr/bin/chromium'
                       if b == 'chromium' else None), \
                 patch('integrations.agent_engine.app_installer.os.makedirs'), \
                 patch('builtins.open', side_effect=PermissionError('read-only')):
                res = self.installer._install_browser_ext(InstallRequest(
                    source=src,
                    options={'id': 'x', 'policy_dir': policy_dir}))
            self.assertFalse(res.success)
            self.assertIn('Failed to write', res.error)
        finally:
            os.unlink(src)
            import shutil
            shutil.rmtree(policy_dir, ignore_errors=True)

    def test_crx_detects_as_browser_ext_not_extension(self):
        """.crx must NOT masquerade as a .hartpkg EXTENSION."""
        src = _tmpfile('.crx', magic=b'Cr24')
        try:
            self.assertEqual(detect_platform(src),
                             InstallerPlatform.BROWSER_EXT)
        finally:
            os.unlink(src)


# ═══════════════════════════════════════════════════════════════════════════
# REAL (in-process) — extension (.hartpkg): loads via the extension registry
# ═══════════════════════════════════════════════════════════════════════════

class TestExtensionIsReal(_InstallerCase):
    """The HART .hartpkg installer is REAL (in-process). NOTE: `.hartpkg` is
    HART's OWN format — it is NOT a browser .crx/.xpi (those go through
    _install_browser_ext / InstallerPlatform.BROWSER_EXT)."""

    def test_loads_via_registry(self):
        ext = MagicMock()
        ext.manifest.id = 'my_ext'
        ext.manifest.version = '2.1.0'
        ext_reg = MagicMock()
        ext_reg.load.return_value = ext
        registry = MagicMock()
        registry.get.return_value = ext_reg
        with patch('core.platform.registry.get_registry', return_value=registry):
            res = self.installer._install_extension(
                InstallRequest(source='thing.hartpkg'))
        self.assertTrue(res.success)
        self.assertEqual(res.app_id, 'my_ext')
        self.assertEqual(res.version, '2.1.0')
        ext_reg.load.assert_called_once_with('thing.hartpkg')

    def test_load_failure_propagates(self):
        ext_reg = MagicMock()
        ext_reg.load.side_effect = ValueError('bad manifest')
        registry = MagicMock()
        registry.get.return_value = ext_reg
        with patch('core.platform.registry.get_registry', return_value=registry):
            res = self.installer._install_extension(
                InstallRequest(source='thing.hartpkg'))
        self.assertFalse(res.success)
        self.assertIn('bad manifest', res.error)

    def test_registry_unavailable_fails(self):
        with patch('core.platform.registry.get_registry', side_effect=ImportError):
            res = self.installer._install_extension(
                InstallRequest(source='thing.hartpkg'))
        self.assertFalse(res.success)

    def test_hartpkg_detects_as_extension_not_browser_ext(self):
        self.assertEqual(detect_platform('a.hartpkg'),
                         InstallerPlatform.EXTENSION)


# ═══════════════════════════════════════════════════════════════════════════
# Dispatch coverage — every declared platform maps to a handler (no orphan)
# ═══════════════════════════════════════════════════════════════════════════

class TestDispatchCoverage(_InstallerCase):
    """Every non-UNKNOWN platform reaches a distinct handler — including the
    NEW snap + browser_ext, so neither falls through to the nix catch-all."""

    def test_each_platform_routes_to_its_handler(self):
        mapping = {
            InstallerPlatform.NIX: '_install_nix',
            InstallerPlatform.FLATPAK: '_install_flatpak',
            InstallerPlatform.APPIMAGE: '_install_appimage',
            InstallerPlatform.WINDOWS: '_install_windows',
            InstallerPlatform.ANDROID: '_install_android',
            InstallerPlatform.MACOS: '_install_macos',
            InstallerPlatform.SNAP: '_install_snap',
            InstallerPlatform.BROWSER_EXT: '_install_browser_ext',
            InstallerPlatform.EXTENSION: '_install_extension',
        }
        for plat, handler_name in mapping.items():
            with patch.object(self.installer, handler_name) as mock_h:
                mock_h.return_value = InstallResult(
                    success=True, platform=plat.value, name='x')
                self.installer.install(
                    InstallRequest(source='x', platform=plat))
                mock_h.assert_called_once()

    def test_every_non_unknown_platform_has_a_handler(self):
        """No orphan enum: each value (except UNKNOWN) must dispatch to a real
        method, so adding an enum without a handler fails this test."""
        for p in InstallerPlatform:
            if p == InstallerPlatform.UNKNOWN:
                continue
            with patch.object(self.installer, '_install_nix') as nix:
                nix.return_value = InstallResult(
                    success=False, platform='nix', name='x')
                res = self.installer.install(
                    InstallRequest(source='x', platform=p))
            # If p had no handler, install() returns the 'No installer for
            # platform' error; assert that never happens.
            self.assertNotIn('No installer for platform', res.error or '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
